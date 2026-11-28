from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby
from typing import Literal

from lib import job_detail
from lib.dispatch.constants import REVISE_ROUND_STATES
from lib.dispatch.decisions import Decision, chosen_words
from lib.dispatch.qa import QaFix, failed_column
from lib.dispatch.helpers import _comment_link, _comment_order, _matching_head, _round_number, _scope
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, PausedStep, RoundGrant, TrackedTicket
from lib.dispatch.verdict.convergence import Convergence, Repeated, RoundFindings, review_step, stop_policy
from lib.handoff import HandoffError, parse_comment, repeat_id
from lib.markdown_items import MARKDOWN
from lib.queue import JobRequest

Stop = Literal["repeat", "capped", "stalled", "unsat"]


@dataclass(frozen=True)
class StopOption:
    name: str
    consequence: str


_SPLIT = StopOption("Split", "Close this PR and file smaller tickets. No more rounds run here.")
OVERRULE = StopOption(
    "Overrule the finding",
    "An approver's /go that names this option records the decision and queues one review of the current head "
    "at this round. No fix is queued and no round is spent. "
    "Later briefs quote the decision, so the reviewer does not block on it again.",
)
_CAPPED_OPTIONS = (
    StopOption("Another round", "An approver's /go adds one round and queues the fix."),
    OVERRULE,
    _SPLIT,
)
STOP_OPTIONS: dict[Stop, tuple[StopOption, ...]] = {
    "repeat": (
        StopOption(
            "Another round",
            "An approver's /go resumes the paused fix on this finding. At the review cap it also adds one round.",
        ),
        OVERRULE,
        _SPLIT,
    ),
    "capped": _CAPPED_OPTIONS,
    "stalled": _CAPPED_OPTIONS,
    "unsat": (
        StopOption(
            "Change the criterion",
            "An answers, a planning go or an approver's /go with the new wording "
            "resumes the review at the same round against it. "
            "No round is spent.",
        ),
        StopOption(
            "Fix round",
            "An approver's go with state=building sends the builder a fix round. The round counts.",
        ),
        _SPLIT,
    ),
}
_STOP_OPENINGS: dict[Stop, str] = {
    "repeat": "The reviewer blocked {ids} again in round {round}.",
    "capped": "Round {round} reached the review cap with {ids} still blocking.",
    "stalled": "Blocking findings did not shrink in rounds {prior} and {round}: {ids} still block.",
    "unsat": "The reviewer called {ids} unsatisfiable in round {round}.",
}
_QUOTE_LIMIT = 800

_ACCEPTED_REVISE = (
    "queued-review-fix",
    "answered-fix",
    "capped-review",
    "waiting-max-repeat",
    "waiting-max-unsat",
)


@dataclass(frozen=True)
class _EarlierRevise:
    round: int
    comment: Comment
    blocking: frozenset[str]


@dataclass(frozen=True)
class StopRecord:
    verdict_id: str
    stop: Literal["capped", "stalled", "repeat"]
    finding_ids: frozenset[str]
    released_by: Literal["approver", "engine"] | None
    complete: bool = True


@dataclass(frozen=True)
class StopExchange:
    round: int
    verdict: Comment
    response: Comment | None
    ids: frozenset[str]


def _handoff(body: str) -> tuple[dict[str, str], str]:
    try:
        handoff = parse_comment(body)
    except HandoffError:
        return {}, body
    return handoff.as_dict(), handoff.body


_ACCEPTED_READY = ("queued-review", "answered-review")
_LIST_OPENS = ("bullet_list_open", "ordered_list_open")


def _blocks(text: str) -> list[list[str]]:
    lines = text.splitlines()
    blocks: list[list[str]] = []
    for token in MARKDOWN.parse(text):
        top = token.level == 0 and token.nesting != -1 and token.type not in (*_LIST_OPENS, "hr")
        if token.map is None or not (top or (token.type == "list_item_open" and token.level == 1)):
            continue
        block = lines[token.map[0] : token.map[1]]
        while block and not block[-1].strip():
            block.pop()
        if block:
            blocks.append(block)
    return blocks


def _quote(body: str, ids: frozenset[str]) -> str:
    blocks = _blocks(_handoff(body)[1])
    token = re.compile(rf"(?<![\w-])(?:{'|'.join(map(re.escape, sorted(ids)))})(?![\w-])") if ids else None
    named = [block for block in blocks if token is not None and token.search("\n".join(block))]
    quoted = []
    for block in named or blocks[:1]:
        text = "\n".join(block)
        if len(text) > _QUOTE_LIMIT:
            text = text[:_QUOTE_LIMIT].rstrip() + " ..."
        quoted.append("\n".join(f"> {line}".rstrip() for line in text.splitlines()))
    return "\n>\n".join(quoted)


def stop_question(
    stop: Stop, disputed: frozenset[str], round_number: int, exchanges: Sequence[StopExchange]
) -> str:
    named = ", ".join(sorted(disputed)) or "findings"
    opening = _STOP_OPENINGS[stop].format(ids=named, round=round_number, prior=round_number - 1)
    parts = [f"{opening} Which option settles it?"]
    for exchange in exchanges:
        parts.append(
            f"Round {exchange.round} verdict ({_comment_link(exchange.verdict)}):\n"
            f"{_quote(exchange.verdict.body, exchange.ids)}"
        )
        if exchange.response is not None:
            parts.append(
                f"Builder's response ({_comment_link(exchange.response)}):\n"
                f"{_quote(exchange.response.body, exchange.ids)}"
            )
    options = "\n".join(f"- {option.name}: {option.consequence}" for option in STOP_OPTIONS[stop])
    parts.append(f"Options:\n{options}")
    return "\n\n".join(parts)


def _stop_choice(stop: Stop, words: str, default: str) -> str:
    names = [option.name for option in STOP_OPTIONS[stop]]
    leading = [name for name in names if words.lower().startswith(name.lower())]
    named = leading or [name for name in names if name.lower() in words.lower()]
    name = named[0] if len(named) == 1 else default
    if not words:
        return name
    return words if words.lower().startswith(name.lower()) else f"{name}: {words}"


def _stopped_rule(wake: str) -> str:
    return str(json.loads(wake)["detail"]).split(", ", 1)[0].removeprefix("repeat-block: ")


def _repeat_finding(connection: sqlite3.Connection, ticket: TrackedTicket, verdict_id: str) -> frozenset[str]:
    wake = connection.execute(
        "SELECT id, detail FROM jobs WHERE dedupe_key = ?",
        (f"t3-repeat:{_scope(ticket.repo, ticket.ticket_id)}:{verdict_id}",),
    ).fetchone()
    if wake is not None:
        return frozenset({_stopped_rule(str(job_detail.load_row(connection, wake["id"], wake["detail"])))})
    ruled = connection.execute(
        """
        SELECT finding_ids FROM engine_decisions
        WHERE lower(repo) = ? AND ticket_id = ? AND site = 'repeat' AND subject = ?
        """,
        (ticket.repo.lower(), ticket.ticket_id, verdict_id),
    ).fetchone()
    return frozenset() if ruled is None else frozenset(json.loads(ruled["finding_ids"]))


def _released_by(
    connection: sqlite3.Connection, ticket: TrackedTicket, verdict_id: str, site: Literal["cap", "repeat"]
) -> Literal["approver", "engine"] | None:
    if connection.execute(
        """
        SELECT 1 FROM engine_decisions
        WHERE lower(repo) = ? AND ticket_id = ? AND site = ? AND subject = ? AND outcome = 'granted'
        """,
        (ticket.repo.lower(), ticket.ticket_id, site, verdict_id),
    ).fetchone() is not None:
        return "engine"
    if connection.execute(
        "SELECT 1 FROM decisions WHERE repo = ? AND ticket_id = ? AND question_id = ?",
        (ticket.repo.lower(), ticket.ticket_id, verdict_id),
    ).fetchone() is not None:
        return "approver"
    return None


class RepeatBlockMixin:
    def _convergence(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_round: int | None,
        qa_ids: frozenset[str] = frozenset(),
    ) -> Convergence:
        from lib.evidence.findings import blocking_ids

        handoff = parse_comment(comment.body)
        named = repeat_id(handoff)
        earlier = [
            next(revises)
            for _, revises in groupby(self._accepted_revises(ticket, reported_round), key=lambda item: item.round)
        ]
        current = RoundFindings(
            reported_round or 0,
            frozenset(blocking_ids(handoff.body) | qa_ids | ({named} if named else set())),
            named,
        )
        return review_step(
            stop_policy(ticket.factory), [RoundFindings(item.round, item.blocking, None) for item in earlier], current
        )

    def _accepted_revises(
        self, ticket: TrackedTicket, reported_round: int | None
    ) -> list[_EarlierRevise]:
        from lib.evidence.findings import blocking_ids

        with self._connect() as connection:
            accepted = {
                str(row["comment_id"]): set(json.loads(row["qa_failed"] or "[]"))
                for row in connection.execute(
                    f"""
                    SELECT comment_id, qa_failed FROM review_events
                    WHERE repo = ? AND ticket_id = ?
                      AND outcome IN ({",".join("?" * len(_ACCEPTED_REVISE))})
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, *_ACCEPTED_REVISE),
                )
            }
        found = []
        for item in ticket.comments:
            if item.id not in accepted:
                continue
            try:
                handoff = parse_comment(item.body)
            except HandoffError:
                continue
            number = _round_number(handoff.as_dict().get("round"))
            if number is not None and number < (reported_round or 0):
                named = repeat_id(handoff)
                blocking = blocking_ids(handoff.body) | ({named} if named else set()) | accepted[item.id]
                found.append(_EarlierRevise(number, item, blocking))
        return sorted(found, key=lambda entry: (entry.round, _comment_order(entry.comment)), reverse=True)

    def _stop_question(
        self, ticket: TrackedTicket, verdict: Comment, stop: Stop, disputed: frozenset[str]
    ) -> str:
        round_number = _round_number(_handoff(verdict.body)[0].get("round")) or 0
        scoped = stop not in ("capped", "stalled") and bool(disputed)
        rounds = [
            (item.round, item.comment, disputed if scoped else item.blocking)
            for item in reversed(self._accepted_revises(ticket, round_number))
            if not scoped or item.blocking & disputed
        ]
        rounds.append((round_number, verdict, disputed))
        exchanges = [
            StopExchange(
                number,
                comment,
                self._builder_response(ticket, comment, rounds[index + 1][1])
                if index + 1 < len(rounds)
                else None,
                ids,
            )
            for index, (number, comment, ids) in enumerate(rounds)
        ]
        return stop_question(stop, disputed, round_number, exchanges)

    def _builder_response(self, ticket: TrackedTicket, verdict: Comment, before: Comment) -> Comment | None:
        builder = f"{ticket.factory.builder_harness}-bot"
        reviewed = _handoff(before.body)[0].get("head")
        readies = [
            item
            for item in sorted(ticket.comments, key=_comment_order)
            if _comment_order(verdict) < _comment_order(item) < _comment_order(before)
            and (fields := _handoff(item.body)[0]).get("kind") == "ready"
            and self._trusted(ticket, item, fields, builder, role="builder")
        ]
        accepted = [item for item in readies if self._handled_outcome(ticket, item.id) in _ACCEPTED_READY]
        if not accepted:
            accepted = [
                item for item in readies if _matching_head(_handoff(item.body)[0].get("head") or "", reviewed)
            ]
        return accepted[-1] if accepted else None

    def _stop_disputed(self, ticket: TrackedTicket, verdict: Comment, stop: Stop) -> frozenset[str]:
        from lib.evidence.findings import blocking_ids

        fields, body = _handoff(verdict.body)
        if stop in ("capped", "stalled"):
            return blocking_ids(body)
        if stop == "unsat":
            return frozenset({fields["unsat"]} if fields.get("unsat") else ())
        with self._connect() as connection:
            return _repeat_finding(connection, ticket, verdict.id)

    def _stop_decision(
        self, ticket: TrackedTicket, verdict: Comment, stop: Stop, answer: Comment, default: str
    ) -> Decision:
        words = chosen_words(answer.body)
        return Decision(
            self._stop_question(ticket, verdict, stop, self._stop_disputed(ticket, verdict, stop)),
            tuple(option.name for option in STOP_OPTIONS[stop]),
            _stop_choice(stop, "" if words == "/go" else words, default),
            answer.author,
            answer.created_at,
            answer.url,
            answer.id,
            0,
        )

    def _capped_stop(self, ticket: TrackedTicket, verdict: Comment) -> Literal["capped", "stalled"]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT stop FROM review_events WHERE repo = ? AND ticket_id = ? AND comment_id = ?",
                (ticket.repo.lower(), ticket.ticket_id, verdict.id),
            ).fetchone()
        return "stalled" if row is not None and row["stop"] == "stalled" else "capped"

    def _held_stop(self, ticket: TrackedTicket) -> tuple[Stop, Comment] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT review_events.outcome, review_events.comment_id FROM ticket_state
                JOIN review_events
                  ON review_events.repo = ticket_state.repo
                 AND review_events.ticket_id = ticket_state.ticket_id
                WHERE ticket_state.repo = ? AND ticket_state.ticket_id = ?
                  AND (
                    (ticket_state.workflow_state = 'waiting-max'
                     AND review_events.comment_id = ticket_state.paused_question_id
                     AND review_events.outcome IN ('waiting-max-repeat', 'waiting-max-unsat'))
                    OR (ticket_state.workflow_state = 'capped'
                        AND review_events.outcome = 'capped-review')
                  )
                ORDER BY review_events.rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        verdict = next((item for item in ticket.comments if item.id == row["comment_id"]), None)
        if verdict is None:
            return None
        held: dict[str, Stop] = {"waiting-max-repeat": "repeat", "waiting-max-unsat": "unsat"}
        return held.get(str(row["outcome"])) or self._capped_stop(ticket, verdict), verdict

    def _stop_history(self, ticket: TrackedTicket) -> tuple[StopRecord, ...]:
        from lib.evidence.findings import blocking_ids

        bodies = {item.id: item.body for item in ticket.comments}
        history = []
        with self._connect() as connection:
            for row in connection.execute(
                """
                SELECT comment_id, outcome, stop, qa_failed, finding_ids FROM review_events
                WHERE repo = ? AND ticket_id = ? AND outcome IN ('capped-review', 'waiting-max-repeat')
                ORDER BY rowid
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall():
                verdict_id = str(row["comment_id"])
                body = bodies.get(verdict_id)
                qa_failed = frozenset(json.loads(row["qa_failed"] or "[]"))
                if row["finding_ids"] is not None:
                    blocking = frozenset(json.loads(row["finding_ids"]))
                else:
                    blocking = frozenset() if body is None else blocking_ids(_handoff(body)[1])
                if row["outcome"] == "waiting-max-repeat":
                    history.append(
                        StopRecord(
                            verdict_id,
                            "repeat",
                            blocking
                            if row["finding_ids"] is not None
                            else blocking | qa_failed | _repeat_finding(connection, ticket, verdict_id),
                            _released_by(connection, ticket, verdict_id, "repeat"),
                            row["finding_ids"] is not None or body is not None,
                        )
                    )
                    continue
                history.append(
                    StopRecord(
                        verdict_id,
                        "stalled" if row["stop"] == "stalled" else "capped",
                        blocking | qa_failed,
                        _released_by(connection, ticket, verdict_id, "cap"),
                        row["finding_ids"] is not None or body is not None,
                    )
                )
        return tuple(history)

    def _repeat_stops(self, ticket: TrackedTicket, comment: Comment, rule_id: str) -> int:
        return 1 + sum(
            1
            for stop in self._stop_history(ticket)
            if stop.stop == "repeat" and stop.verdict_id != comment.id and rule_id in stop.finding_ids
        )

    def _repeat_detail(
        self, ticket: TrackedTicket, comment: Comment, repeated: Repeated, reported_round: int | None, stops: int
    ) -> str:
        same_round = [
            item
            for item in self._accepted_revises(ticket, reported_round)
            if item.round == repeated.earlier_round
        ]
        holding = next(
            (item for item in same_round if repeated.rule_id in item.blocking),
            same_round[0] if same_round else None,
        )
        earlier = "no earlier verdict" if holding is None else _comment_link(holding.comment)
        fields = [
            f"repeat-block: {repeated.rule_id}",
            earlier,
            _comment_link(comment),
            f"stops={stops}",
            f"for={'factory' if stops == 1 else 'max'}",
        ]
        if ticket.changed_lines is not None:
            fields.append(f"changed_lines={ticket.changed_lines}")
        return ", ".join(fields)

    def _t3_repeat(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        repeated: Repeated,
        reported_round: int | None,
        *,
        qa: QaFix | None,
        states: tuple[str, ...] = REVISE_ROUND_STATES,
    ) -> tuple[bool, str]:
        from lib.dispatch.review import prior_grants
        from lib.evidence.findings import blocking_ids

        key = (ticket.repo.lower(), ticket.ticket_id)
        stops = self._repeat_stops(ticket, comment, repeated.rule_id)
        detail = self._repeat_detail(ticket, comment, repeated, reported_round, stops)
        question = self._stop_question(ticket, comment, "repeat", frozenset({repeated.rule_id}))
        engine = ticket.factory.rule_engine == "on"
        prior_grant = prior_grants(self._stop_history(ticket), comment.id) if engine else frozenset()
        repeated_ids = frozenset(
            {repeated.rule_id, *blocking_ids(_handoff(comment.body)[1]), *(qa.failed if qa is not None else ())}
        )
        if engine:
            ticket = self._fresh_issue(ticket)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (*key, comment.id),
            ).fetchone()
            if prior is not None:
                granted = _released_by(connection, ticket, comment.id, "repeat") == "engine"
                connection.commit()
                if granted:
                    self.review.apply_engine_grants(ticket)
                else:
                    self._write_state_label(ticket, "waiting-max")
                return True, str(prior["outcome"])
            state = connection.execute(
                """
                SELECT workflow_state, review_round, generation FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                key,
            ).fetchone()
            if (
                state is None
                or state["workflow_state"] not in states
                or reported_round != int(state["review_round"]) + 1
            ):
                connection.commit()
                self._wake_comment_problem(ticket, comment, "stale", "stale-revise")
                return True, "stale-revise"
            generation = int(state["generation"])
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "waiting-max",
                source="t3-repeat",
                at=self.queue.now(),
                also={
                    "review_round": reported_round,
                    "paused_action": "fix",
                    "paused_recipient": ticket.factory.builder_harness,
                    "paused_head": ticket.current_head,
                    "paused_generation": generation,
                    "paused_question_id": comment.id,
                },
            )
            connection.execute(
                """
                INSERT INTO review_events (repo, ticket_id, comment_id, outcome, qa_failed, finding_ids)
                VALUES (?, ?, ?, 'waiting-max-repeat', ?, ?)
                """,
                (*key, comment.id, failed_column(qa), json.dumps(sorted(repeated_ids))),
            )
            granted = engine and self.review.on_repeat(
                connection, ticket, comment, repeated_ids, stops, prior_grant, reported_round
            )
            if granted:
                self._resume_step(
                    connection,
                    ticket,
                    comment,
                    PausedStep(
                        question_id=comment.id,
                        action="fix",
                        recipient=ticket.factory.builder_harness,
                        generation=generation,
                        head=ticket.current_head,
                        round_number=reported_round + 1,
                        prior=self._resumed_step_detail(connection, ticket, comment, "fix", comment.id),
                    ),
                    [],
                    RoundGrant.engine(comment),
                )
                connection.commit()
                self.review.apply_engine_grants(ticket)
                return True, "waiting-max-repeat"
            self.queue.enqueue_in_transaction(
                connection,
                self._grok_request(
                    JobRequest(
                        repo=ticket.repo,
                        ticket_id=ticket.ticket_id,
                        action="clarify",
                        recipient="grok",
                        source_comment_id=comment.id,
                        timeout_seconds=ticket.job_timeout_minutes * 60,
                        generation=generation,
                        head=ticket.current_head,
                        detail=self._project_wake_detail(ticket, detail, question=question),
                    )
                ),
                dedupe_key=f"t3-repeat:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}",
            )
            connection.commit()
        self._write_state_label(ticket, "waiting-max")
        return True, "waiting-max-repeat"
