"""T5 questions and answers, plus cancel and requeue of answered jobs."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace

from lib.dispatch.constants import FINISHED_STATES, HANDED_OFF_EXTERNALLY
from lib.dispatch.decisions import Decision, answer_blocks, answer_text, record_decisions
from lib.dispatch.helpers import (
    _ANSWERS,
    _REGISTERING_ACTIONS,
    _REJECTED_OUTCOMES,
    _asks_questions,
    _comment_link,
    _comment_order,
    _detail_round_number,
    _matching_head,
    _retry_of,
    _round_number,
    _scope,
    _state_for_action,
    grant_open,
    red_checks,
)
from lib.dispatch.plan import gap_of
from lib.dispatch.rebase import RebaseStep
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, PausedStep, Registration, RoundGrant, TrackedTicket
from lib.handoff import HandoffError, parse_comment
from lib.queue import Job, JobRequest, _job
from lib.queue import plan_round as _plan_round
from lib.state_backup import RESTORED
from lib.update_check import UpdateScope

_REFUSED_VERDICT_OUTCOMES = (
    "rejected-",
    "stale-",
    "superseded-",
    "refused-",
    "woke-grok-unparsed",
)
_LIST_ITEM = re.compile(r"^(\s*)(?:[-*]|[0-9]+[.)])\s+(\S.*?)\s*$")
_CLASSIFIED = re.compile(r"\[(observable|default-able|product)\]\s+(\S.*)")
_DEFAULT = re.compile(r"^(\s*)Default:\s*(\S.*?)\s*$")
_STOP_DEFAULTS = {"repeat": "Another round", "unsat": "Change the criterion"}


@dataclass(frozen=True)
class _Offer:
    text: str
    classified: bool
    depth: int
    options: tuple[str, ...]
    tag: str | None = None


def decisions_of(question: Comment | None, answer: Comment) -> list[Decision]:
    body = answer_text(answer.body)
    asked = _handoff_body(question)
    offers = _offers(asked)
    pairs = answer_blocks(body)
    entries = (
        [(text, _options_for(offers, text), choice) for text, choice in pairs]
        if pairs
        else [(asked.strip(), _every_option(offers), body)]
    )
    return [
        Decision(text, options, choice, answer.author, answer.created_at, answer.url, answer.id, position)
        for position, (text, options, choice) in enumerate(entries)
    ]


def _handoff_body(comment: Comment | None) -> str:
    if comment is None:
        return ""
    try:
        return parse_comment(comment.body).body
    except HandoffError:
        return ""


def _offers(asked: str) -> list[_Offer]:
    lines = asked.splitlines()
    offers: list[_Offer] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        item = _LIST_ITEM.match(line)
        classified = _CLASSIFIED.fullmatch(item.group(2)) if item else None
        text = classified.group(2) if classified else item.group(2) if item else line.strip()
        depth = len(item.group(1)) if item else -1
        tag = classified.group(1) if classified else None
        offers.append(_Offer(text, classified is not None, depth, _options_after(lines[index + 1 :], depth), tag))
    return offers


def extract_open_questions(raw_body: str, *, limit: int = 8) -> list[dict[str, object]]:
    """Structured open product questions parsed from a paused `kind=questions` body."""
    if limit <= 0 or not raw_body or not raw_body.strip():
        return []
    try:
        body = parse_comment(raw_body).body
    except HandoffError:
        body = raw_body
    offers = _offers(body)
    if not offers:
        return []
    product = [offer for offer in offers if offer.tag == "product"]
    if product:
        chosen = product[:limit]
    else:
        top_bullets = [offer for offer in offers if offer.depth == 0 and offer.tag is None]
        prose = [offer for offer in offers if offer.depth < 0]
        if top_bullets and any("?" in offer.text or offer.options for offer in top_bullets):
            chosen = top_bullets[:limit]
        elif prose:
            opts = tuple(offer.text for offer in top_bullets)
            chosen = [_Offer(prose[0].text, False, -1, prose[0].options or opts)]
        else:
            chosen = [offer for offer in offers if offer.tag is None][:limit]
    return [
        {"text": offer.text, "options": list(offer.options)}
        for offer in chosen
        if offer.text.strip()
    ]


def _options_after(lines: list[str], depth: int) -> tuple[str, ...]:
    options: list[str] = []
    for line in lines:
        item = _LIST_ITEM.match(line)
        default = _DEFAULT.match(line)
        found = item or default
        if found is None:
            if line.strip():
                break
            continue
        if len(found.group(1)) <= depth or (item and _CLASSIFIED.fullmatch(item.group(2))):
            break
        options.append(found.group(2))
    return tuple(options)


def _options_for(offers: list[_Offer], question: str) -> tuple[str, ...]:
    for offer in offers:
        if offer.text == question:
            return offer.options
    if any(offer.classified for offer in offers):
        return ()
    return tuple(offer.text for offer in offers if offer.depth >= 0)


def _every_option(offers: list[_Offer]) -> tuple[str, ...]:
    loose = [offer.text for offer in offers if offer.depth >= 0 and not offer.classified]
    nested = [option for offer in offers if offer.classified for option in offer.options]
    return tuple(dict.fromkeys(loose + nested))


class QuestionsMixin:
    def _t5_questions(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_session(ticket, comment, fields):
            return True, "rejected-questions-author"
        if not _asks_questions(fields, parse_comment(comment.body).body):
            return True, "no-questions"
        step = self._latest_step(ticket)
        if step is None:
            action = "clarify"
            recipient = "grok"
            head = ticket.current_head
            generation = self._generation(ticket.repo, ticket.ticket_id)
        else:
            action, recipient, head, generation = step
        outcome = self._pause_for_questions(
            ticket, comment, action, recipient, head, generation
        )
        if outcome == "waiting-max":
            self._write_state_label(ticket, "waiting-max")
        return True, outcome

    def _pause_for_questions(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        action: str,
        recipient: str,
        head: str | None,
        generation: int,
    ) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            waiting = connection.execute(
                """
                SELECT workflow_state, paused_question_id FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if waiting is not None and waiting["workflow_state"] == "blocked":
                connection.commit()
                return "questions-while-blocked"
            if waiting is not None and waiting["workflow_state"] == "waiting-max":
                connection.commit()
                if waiting["paused_question_id"] == comment.id:
                    return "waiting-max"
                return "already-waiting-max"
            paused = {
                "paused_action": action,
                "paused_recipient": recipient,
                "paused_head": head,
                "paused_generation": generation,
                "paused_question_id": comment.id,
                "paused_question_text": _handoff_body(comment).strip() or None,
                "paused_plan_text": None,
            }
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "waiting-max",
                source="t5-questions",
                at=self.queue.now(),
                also=paused,
                insert={
                    "gate_count": 0,
                    "stuck": 0,
                    "generation": generation,
                    **paused,
                },
            )
            self._rebase_conflicted(connection, ticket, comment)
            self._cancel_paused_step(connection, ticket, comment, action, recipient)
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
                        head=head,
                        detail=self._project_wake_detail(
                            ticket,
                            _comment_link(comment),
                        ),
                    )
                ),
                dedupe_key=(
                    f"t5-wait:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
                ),
            )
            connection.commit()
        return "waiting-max"

    def _cancel_paused_step(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        action: str,
        recipient: str,
    ) -> None:
        if recipient == "grok":
            return
        rows = connection.execute(
            """
            SELECT id FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
              AND action = ? AND recipient = ?
            """,
            (ticket.repo.lower(), ticket.ticket_id, action, recipient),
        ).fetchall()
        for row in rows:
            self.queue.cancel(row["id"], f"paused-by:{comment.id}", connection=connection)
            if self.log is not None:
                self.log(
                    f"skipped {action}: paused by {comment.id} "
                    f"({ticket.repo}#{ticket.ticket_id})"
                )

    def _t5_answers(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted(
            ticket, comment, fields, "grok-bot", role="orchestrator"
        ):
            return True, "rejected-answers-author"
        if self._parked_for_max(ticket) == "repeat":
            self._wake_comment_problem(
                ticket, comment, "go-not-from-approver", "answers-while-repeat-block"
            )
            return True, "answers-while-repeat-block"
        resumed = self._resume_paused(ticket, comment)
        return (True, "rejected-answers-without-question") if resumed is None else resumed

    def _resume_paused(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        *,
        grant: RoundGrant | None = None,
    ) -> tuple[bool, str] | None:
        with self._connect() as connection:
            paused = connection.execute(
                """
                SELECT workflow_state, paused_action, paused_recipient,
                       paused_head, paused_generation, paused_question_id,
                       paused_plan_text, plan_gap_count
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            resumed = connection.execute(
                """
                SELECT action FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND source_comment_id = ? AND dedupe_key LIKE 't5:%'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
        if (
            paused is not None
            and paused["workflow_state"] != "waiting-max"
            and resumed is not None
        ):
            resumed_state = str(paused["workflow_state"])
            self._write_state_label(ticket, resumed_state)
            return True, f"resumed-{resumed['action']}"
        if (
            paused is None
            or paused["workflow_state"] != "waiting-max"
            or paused["paused_action"] is None
        ):
            return None
        action = str(paused["paused_action"])
        with self._connect() as connection:
            prior = self._resumed_step_detail(
                connection, ticket, comment, action, paused["paused_question_id"]
            )
        question = next(
            (item for item in ticket.comments if item.id == paused["paused_question_id"]),
            None,
        )
        stop = self._parked_for_max(ticket)
        if grant is not None and grant.released_by == "engine":
            decisions = []
        elif question is not None and stop in _STOP_DEFAULTS:
            decisions = [self._stop_decision(ticket, question, stop, comment, _STOP_DEFAULTS[stop])]
        else:
            decisions = decisions_of(question, comment)
        paused_plan = gap_of(question) if question is not None else None
        if action == "build" and paused["paused_plan_text"] is not None:
            plan_text = paused["paused_plan_text"]
        elif action == "build" and paused_plan is not None:
            plan_text = paused_plan[0]
        elif action in ("build", "plan"):
            plan_text = (prior or {}).get("plan_text")
        else:
            plan_text = None
        unmapped = (prior or {}).get("unmapped") if action == "plan" else None
        step = PausedStep(
            question_id=str(paused["paused_question_id"]),
            action=action,
            recipient=str(paused["paused_recipient"]),
            generation=int(paused["paused_generation"]),
            head=str(paused["paused_head"]) if paused["paused_head"] is not None else None,
            round_number=None if action == "plan" else self._review_context(ticket)[1] + 1,
            plan_text=plan_text if isinstance(plan_text, str) and plan_text else None,
            plan_round=int(paused["plan_gap_count"]) + 1 if action == "plan" else None,
            unmapped=unmapped if isinstance(unmapped, str) and unmapped else None,
            prior=prior,
        )
        fresh = self._fresh_issue(ticket)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT workflow_state, paused_question_id FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if (
                current is None
                or current["workflow_state"] != "waiting-max"
                or current["paused_question_id"] != paused["paused_question_id"]
                or (grant is not None and not grant_open(connection, ticket, grant, current["paused_question_id"]))
            ):
                connection.commit()
                return None
            resumed_state = self._resume_step(connection, fresh, comment, step, decisions, grant)
            connection.commit()
        self._write_state_label(fresh, resumed_state)
        return True, f"resumed-{action}"

    def _resume_step(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        step: PausedStep,
        decisions: list[Decision],
        grant: RoundGrant | None,
    ) -> str:
        record_decisions(connection, ticket, step.question_id, decisions)
        prior = step.prior or {}
        rebase = prior.get("rebase") if prior.get("kind") == "rebase" else None
        rebase_step = RebaseStep(**rebase) if isinstance(rebase, dict) else None
        if grant is not None:
            connection.execute(
                """
                UPDATE ticket_state SET cap_extra = cap_extra + 1
                WHERE repo = ? AND ticket_id = ? AND review_round >= ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, self._effective_cap(ticket, connection)),
            )
        dedupe_key = (
            f"t5:{_scope(ticket.repo, ticket.ticket_id)}:{step.question_id}:"
            f"{comment.id if grant is None else grant.key}"
        )
        if step.action == "qa":
            self._resume_qa_in_transaction(
                connection, ticket, comment, step.head, step.generation, dedupe_key
            )
        else:
            self._enqueue_step(
                connection,
                ticket,
                comment,
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action=step.action,
                    recipient=step.recipient,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=step.generation,
                    head=step.head,
                    detail=self._job_detail(
                        ticket,
                        action=step.action,
                        head=step.head,
                        round_number=step.round_number,
                        instruction=_comment_link(comment),
                        plan_text=step.plan_text,
                        resume=True,
                        cap=self._effective_cap(ticket, connection),
                        update=UpdateScope.from_detail(prior, step.head),
                        plan_round=step.plan_round,
                        ci_failure=(
                            red_checks(ticket, step.head)
                            if step.action == "fix" and rebase_step is None
                            else ()
                        ),
                        rebase_step=rebase_step,
                        unmapped=step.unmapped,
                        connection=connection,
                    ),
                ),
                dedupe_key=dedupe_key,
            )
        if grant is not None:
            connection.execute(
                """
                INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                VALUES (?, ?, ?, ?)
                """,
                (ticket.repo.lower(), ticket.ticket_id, grant.key, f"resumed-{step.action}"),
            )
        resumed_state = _state_for_action(step.action)
        write_workflow_state(
            connection,
            ticket.repo,
            ticket.ticket_id,
            resumed_state,
            source="t5-answers",
            at=self.queue.now(),
            also={
                "paused_action": None,
                "paused_recipient": None,
                "paused_head": None,
                "paused_generation": None,
                "paused_question_id": None,
                "paused_question_text": None,
                "paused_plan_text": None,
            },
        )
        return resumed_state

    def _decisions(
        self, ticket: TrackedTicket, connection: sqlite3.Connection | None = None
    ) -> list[dict[str, object]]:
        if connection is None:
            with self._connect() as connection:
                return self._decisions(ticket, connection)
        rows = connection.execute(
            """
            SELECT question, options, choice, answerer, answered_at, url,
                   answer_id, position
            FROM decisions
            WHERE repo = ? AND ticket_id = ?
            ORDER BY answered_at, answer_id, position
            """,
            (ticket.repo.lower(), ticket.ticket_id),
        ).fetchall()
        return [{**dict(row), "options": json.loads(row["options"])} for row in rows]

    def _answered_by(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        action: str,
        recipient: str,
        *,
        queued_at: float,
        head: str | None = None,
        round_number: int | None = None,
        plan_round: int | None = None,
        unhandled_plan_by_round: bool = False,
    ) -> Comment | None:
        """The first later trusted orch:v1 comment that already answered the
        action job comment queues for recipient, or None (#108, #66).

        With an intact database no such answer exists yet when a comment is
        handled; it is found on a replay (a lost or restored database) or when
        both comments landed while the poller was down.
        """
        kind, role = _ANSWERS[action]
        after = _comment_order(comment)
        try:
            source_head = parse_comment(comment.body).as_dict().get("head")
        except HandoffError:
            source_head = None
        for item in sorted(ticket.comments, key=_comment_order):
            if _comment_order(item) <= after:
                continue
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            if fields["kind"] != kind:
                continue
            if not self._trusted(ticket, item, fields, f"{recipient}-bot", role=role):
                continue
            reported = fields.get("head", "")
            if action == "review":
                if (
                    _matching_head(reported, head) is None
                    or _round_number(fields.get("round")) != round_number
                    or fields.get("verdict") not in ("REVISE", "APPROVE")
                ):
                    # #71: T3 and T4 act on no other verdict (BLOCKED), so
                    # it answers no review (Codex PR #112 r5 F1).
                    continue
            elif head is not None and (
                _matching_head(reported, head) is not None
                or (
                    _matching_head(reported, source_head) is not None
                    and self._predates(ticket, item, queued_at)
                )
            ):
                continue
            handled = self._handled_outcome(ticket, item.id)
            if (handled is None and item.edited_at is not None) or (
                handled is not None and handled.startswith(_REJECTED_OUTCOMES)
            ):
                continue
            if action == "plan" and (
                _round_number(fields.get("round")) != plan_round
                or not (
                    (unhandled_plan_by_round and handled is None)
                    or self._t1p_accepted_or_accepts_plan(ticket, item, plan_round)
                )
            ):
                continue
            if (
                kind in _REGISTERING_ACTIONS
                and self._registration(ticket, item, kind) is Registration.UNREGISTERED
            ):
                continue
            return item
        return None

    def _first_queued_at(
        self, job: Job, *, connection: sqlite3.Connection | None = None
    ) -> float:
        if connection is None:
            with self._connect() as conn:
                return self._first_queued_at(job, connection=conn)
        queued_at = job.created_at
        retry_of = _retry_of(job.request.detail)
        while retry_of is not None:
            row = connection.execute(
                "SELECT created_at, detail FROM jobs WHERE id = ?", (retry_of,)
            ).fetchone()
            if row is None:
                break
            queued_at = min(queued_at, float(row["created_at"]))
            retry_of = _retry_of(row["detail"])
        return queued_at

    def _cancel_answered_jobs(self, ticket: TrackedTicket) -> None:
        """Cancel this ticket's queued steps that a later trusted comment
        already answered (#108).

        Catches a job queued before its answer was read: one restored from
        a backup taken before the builder or reviewer posted, whose
        triggering comment is already handled and so is never replayed.
        """
        ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
        sources = {comment.id: comment for comment in ticket.comments}
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
                  AND recipient != 'grok'
                  AND action IN ({", ".join("?" for _ in _ANSWERS)})
                ORDER BY created_at, id
                """,
                (ticket.repo.lower(), ticket.ticket_id, *_ANSWERS),
            ).fetchall()
            jobs = [_job(row, connection) for row in rows]
        answered: list[tuple[Job, Comment]] = []
        for job in jobs:
            source = sources.get(job.request.source_comment_id)
            if source is None:
                continue
            answer = self._answered_by(
                ticket,
                source,
                job.request.action,
                job.request.recipient,
                queued_at=self._first_queued_at(job),
                head=job.request.head,
                round_number=_detail_round_number(job.request.detail),
                plan_round=_plan_round(job.request.detail),
            )
            if answer is not None:
                answered.append((job, answer))
        if not answered:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for job, answer in answered:
                current = connection.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()
                if current is not None and current["state"] == "queued":
                    self._cancel_answered(connection, job, job.request, answer)
            connection.commit()

    def _requeue_unanswered_jobs(self, ticket: TrackedTicket) -> None:
        """Queue again a step cancelled as answered when that answer was then
        handled as rejected, or held by a restore (state_backup.RESTORED),
        when no trusted answer stands (#108).

        The cancel relied on that answer being accepted. Without this, a
        replay that met a plan of another round, or a ready at a head the PR
        does not have, left the ticket with no runnable work. A held step
        whose source comment the read lacks is queued again: nothing shows
        it was answered. A step that waits on Max, or that his answers
        resumed, stays cancelled: only those answers run it again (#449).
        """
        ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
        if self._is_stuck(ticket) or self._review_context(ticket)[0] in FINISHED_STATES:
            return
        if self._handed_off_by_max(ticket.repo, ticket.ticket_id):
            return
        sources = {comment.id: comment for comment in ticket.comments}
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT jobs.*, (
                    SELECT detail FROM job_events
                    WHERE job_id = jobs.id AND event = 'cancelled'
                    ORDER BY id DESC LIMIT 1
                ) AS cancel_reason
                FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'cancelled'
                  AND recipient != 'grok'
                  AND action IN ({", ".join("?" for _ in _ANSWERS)})
                ORDER BY created_at, id
                """,
                (ticket.repo.lower(), ticket.ticket_id, *_ANSWERS),
            ).fetchall()
            candidates = [(row, _job(row, connection)) for row in rows]
        unanswered: list[tuple[Job, str]] = []
        for row, job in candidates:
            reason = row["cancel_reason"] or ""
            source = sources.get(row["source_comment_id"])
            if reason == RESTORED:
                requeue_reason = "restored-unanswered"
            elif reason == HANDED_OFF_EXTERNALLY:
                requeue_reason = "resumed-from-linear"
            elif reason.startswith("answered-by:") and source is not None:
                answer_id = reason.removeprefix("answered-by:")
                outcome = self._handled_outcome(ticket, answer_id)
                if outcome is None or not outcome.startswith(_REJECTED_OUTCOMES):
                    continue
                requeue_reason = f"answer-rejected:{answer_id}"
            else:
                continue
            if job.request.action == "review" and self._open_fix_for_head(job):
                continue
            if self._resumed_by_answers(job) or self._waiting_max(job):
                continue
            if source is None or self._answered_by(
                ticket,
                source,
                job.request.action,
                job.request.recipient,
                queued_at=self._first_queued_at(job),
                head=job.request.head,
                round_number=_detail_round_number(job.request.detail),
                plan_round=_plan_round(job.request.detail),
            ) is None:
                unanswered.append((job, requeue_reason))
        if not unanswered:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            requeued = [
                (job, reason)
                for job, reason in unanswered
                if self.queue.requeue(job.id, reason, connection=connection)
            ]
            connection.commit()
        if self.log is not None:
            for job, reason in requeued:
                self.log(
                    f"requeued {job.request.action} ({reason}) "
                    f"({ticket.repo}#{ticket.ticket_id})"
                )

    def _requeue_refused_reviews(self, ticket: TrackedTicket) -> None:
        ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
        state, review_round = self._review_context(ticket)
        if (
            self._is_stuck(ticket)
            or state not in ("review", "awaiting-ok")
            or ticket.current_head is None
        ):
            return
        sources = {comment.id: comment for comment in ticket.comments}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                  AND recipient != 'grok'
                ORDER BY created_at, id
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
            jobs = [_job(row, connection) for row in rows]
        retry_prefix = f"review-refused:{_scope(ticket.repo, ticket.ticket_id)}:"
        owed: tuple[Job, Comment] | None = None
        for job in jobs:
            if job.state in ("queued", "running"):
                return
            if (
                _detail_round_number(job.request.detail) != review_round + 1
                or _matching_head(job.request.head or "", ticket.current_head) is None
            ):
                continue
            if job.dedupe_key.startswith(retry_prefix):
                return
            source = sources.get(job.request.source_comment_id)
            outcome = self._handled_outcome(ticket, job.handoff_comment_id or "")
            if (
                job.state != "done"
                or source is None
                or outcome is None
                or not outcome.startswith(_REFUSED_VERDICT_OUTCOMES)
                or self._answered_by(
                    ticket,
                    source,
                    job.request.action,
                    job.request.recipient,
                    queued_at=self._first_queued_at(job),
                    head=job.request.head,
                    round_number=review_round + 1,
                )
                is not None
            ):
                continue
            owed = (job, source)
        if owed is None or self._open_fix_for_head(owed[0]):
            return
        job, source = owed
        ticket = self._fresh_issue(ticket)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._enqueue_step(
                connection,
                ticket,
                source,
                replace(job.request, attempt=1),
                dedupe_key=f"{retry_prefix}{job.handoff_comment_id}",
            )
            connection.commit()
        if self.log is not None:
            self.log(
                f"requeued review (verdict-refused:{job.handoff_comment_id}) "
                f"({ticket.repo}#{ticket.ticket_id})"
            )

    def _cancel_answered(
        self,
        connection: sqlite3.Connection,
        job: Job,
        request: JobRequest,
        answered: Comment,
    ) -> None:
        """Cancel the job this comment just queued: answered already."""
        if job.state != "queued" or job.request.source_comment_id != request.source_comment_id:
            return
        self.queue.cancel(job.id, f"answered-by:{answered.id}", connection=connection)
        if self.log is not None:
            self.log(
                f"skipped {request.action}: answered by {answered.id} "
                f"({request.repo}#{request.ticket_id})"
            )
