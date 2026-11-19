from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from lib.dispatch.constants import REVISE_ROUND_STATES
from lib.dispatch.helpers import _comment_link, _comment_order, _round_number, _scope
from lib.dispatch.questions import Decision, _answer_text
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, TrackedTicket
from lib.handoff import HandoffError, parse_comment, repeat_id
from lib.queue import JobRequest

Stop = Literal["repeat", "capped", "unsat"]


@dataclass(frozen=True)
class StopOption:
    name: str
    consequence: str


_SPLIT = StopOption("Split", "Close this PR and file smaller tickets. No more rounds run here.")
STOP_OPTIONS: dict[Stop, tuple[StopOption, ...]] = {
    "repeat": (
        StopOption(
            "Another round",
            "An approver's /go resumes the paused fix on this finding. At the review cap it also adds one round.",
        ),
        StopOption(
            "Overrule the finding",
            "An approver's /go that names this option resumes the fix. "
            "Later briefs quote the decision, so the reviewer does not block on it again.",
        ),
        _SPLIT,
    ),
    "capped": (
        StopOption("Another round", "An approver's /go adds one round and queues the fix."),
        StopOption(
            "Overrule the finding",
            "An approver's /go that names this option adds the round too. "
            "Later briefs quote the decision, so the reviewer does not block on it again.",
        ),
        _SPLIT,
    ),
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
class RepeatBlock:
    rule_id: str
    earlier: Comment | None


@dataclass(frozen=True)
class _EarlierRevise:
    round: int
    comment: Comment
    blocking: frozenset[str]


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


def _quote(body: str, ids: frozenset[str]) -> str:
    text = _handoff(body)[1]
    token = re.compile(rf"(?<![\w-])(?:{'|'.join(map(re.escape, sorted(ids)))})(?![\w-])") if ids else None
    lines = [line for line in text.splitlines() if token is not None and token.search(line)]
    if not lines:
        paragraph = text.strip().split("\n\n", 1)[0]
        lines = paragraph.splitlines()
    quoted = "\n".join(lines)
    if len(quoted) > _QUOTE_LIMIT:
        quoted = quoted[:_QUOTE_LIMIT].rstrip() + " ..."
    return "\n".join(f"> {line}".rstrip() for line in quoted.splitlines())


def stop_question(
    stop: Stop, disputed: frozenset[str], round_number: int, exchanges: Sequence[StopExchange]
) -> str:
    named = ", ".join(sorted(disputed)) or "findings"
    parts = [f"{_STOP_OPENINGS[stop].format(ids=named, round=round_number)} Which option settles it?"]
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
    named = [option.name for option in STOP_OPTIONS[stop] if option.name.lower() in words.lower()]
    name = named[0] if len(named) == 1 else default
    if not words:
        return name
    return words if words.lower().startswith(name.lower()) else f"{name}: {words}"


def _qa_failed(connection: sqlite3.Connection, ticket: TrackedTicket, comment_id: str) -> set[str]:
    row = connection.execute(
        "SELECT detail FROM jobs WHERE dedupe_key = ?",
        (f"t3:{_scope(ticket.repo, ticket.ticket_id)}:{comment_id}",),
    ).fetchone()
    findings = json.loads(row["detail"] or "{}").get("qa_findings") if row else None
    return set(findings.get("failed") or ()) if isinstance(findings, dict) else set()


def _stopped_rule(wake: str) -> str:
    return str(json.loads(wake)["detail"]).split(", ", 1)[0].removeprefix("repeat-block: ")


class RepeatBlockMixin:
    def _repeat_trigger(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_round: int | None,
        qa_ids: frozenset[str] = frozenset(),
    ) -> RepeatBlock | None:
        from lib.evidence.findings import blocking_ids

        handoff = parse_comment(comment.body)
        named = repeat_id(handoff)
        earlier = self._accepted_revises(ticket, reported_round)
        previous = next((item for item in earlier if item.round == (reported_round or 0) - 1), None)
        if named is not None:
            holding = next((item for item in earlier if named in item.blocking), previous)
            return RepeatBlock(named, None if holding is None else holding.comment)
        if previous is None:
            return None
        shared = (blocking_ids(handoff.body) | qa_ids) & previous.blocking
        return RepeatBlock(min(shared), previous.comment) if shared else None

    def _accepted_revises(
        self, ticket: TrackedTicket, reported_round: int | None
    ) -> list[_EarlierRevise]:
        from lib.evidence.findings import blocking_ids

        with self._connect() as connection:
            accepted = {
                str(row["comment_id"])
                for row in connection.execute(
                    f"""
                    SELECT comment_id FROM review_events
                    WHERE repo = ? AND ticket_id = ?
                      AND outcome IN ({",".join("?" * len(_ACCEPTED_REVISE))})
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, *_ACCEPTED_REVISE),
                )
            }
            qa_failed = {
                comment_id: _qa_failed(connection, ticket, comment_id) for comment_id in accepted
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
                blocking = blocking_ids(handoff.body) | ({named} if named else set()) | qa_failed[item.id]
                found.append(_EarlierRevise(number, item, blocking))
        return sorted(found, key=lambda entry: (entry.round, _comment_order(entry.comment)), reverse=True)

    def _stop_question(
        self, ticket: TrackedTicket, verdict: Comment, stop: Stop, disputed: frozenset[str]
    ) -> str:
        round_number = _round_number(_handoff(verdict.body)[0].get("round")) or 0
        scoped = stop != "capped" and bool(disputed)
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
        for item in sorted(ticket.comments, key=_comment_order):
            fields = _handoff(item.body)[0]
            if (
                _comment_order(verdict) < _comment_order(item) < _comment_order(before)
                and fields.get("kind") == "ready"
                and self._trusted(ticket, item, fields, builder, role="builder")
            ):
                return item
        return None

    def _stop_disputed(self, ticket: TrackedTicket, verdict: Comment, stop: Stop) -> frozenset[str]:
        from lib.evidence.findings import blocking_ids

        fields, body = _handoff(verdict.body)
        if stop == "capped":
            return blocking_ids(body)
        if stop == "unsat":
            return frozenset({fields["unsat"]} if fields.get("unsat") else ())
        with self._connect() as connection:
            wake = connection.execute(
                "SELECT detail FROM jobs WHERE dedupe_key = ?",
                (f"t3-repeat:{_scope(ticket.repo, ticket.ticket_id)}:{verdict.id}",),
            ).fetchone()
        return frozenset() if wake is None else frozenset({_stopped_rule(str(wake["detail"]))})

    def _stop_decision(
        self, ticket: TrackedTicket, verdict: Comment, stop: Stop, answer: Comment, default: str
    ) -> Decision:
        words = _answer_text(answer.body).strip()
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

    def _repeat_stops(self, ticket: TrackedTicket, comment: Comment, rule_id: str) -> int:
        with self._connect() as connection:
            wakes = connection.execute(
                """
                SELECT jobs.detail FROM review_events AS stop
                JOIN jobs ON jobs.dedupe_key = 't3-repeat:' || ? || ':' || stop.comment_id
                WHERE stop.repo = ? AND stop.ticket_id = ? AND stop.comment_id != ?
                  AND stop.outcome = 'waiting-max-repeat'
                """,
                (_scope(ticket.repo, ticket.ticket_id), ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchall()
        return 1 + sum(_stopped_rule(str(row["detail"])) == rule_id for row in wakes)

    def _repeat_detail(self, ticket: TrackedTicket, comment: Comment, block: RepeatBlock) -> str:
        earlier = "no earlier verdict" if block.earlier is None else _comment_link(block.earlier)
        stops = self._repeat_stops(ticket, comment, block.rule_id)
        fields = [
            f"repeat-block: {block.rule_id}",
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
        block: RepeatBlock,
        reported_round: int | None,
        *,
        states: tuple[str, ...] = REVISE_ROUND_STATES,
    ) -> tuple[bool, str]:
        key = (ticket.repo.lower(), ticket.ticket_id)
        detail = self._repeat_detail(ticket, comment, block)
        question = self._stop_question(ticket, comment, "repeat", frozenset({block.rule_id}))
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
                connection.commit()
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
                INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                VALUES (?, ?, ?, 'waiting-max-repeat')
                """,
                (*key, comment.id),
            )
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
