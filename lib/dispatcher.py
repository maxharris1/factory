"""Dispatcher transitions T1 through T6, plus project hooks."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lib.factory_md import FactoryConfig
from lib.handoff import HandoffError, parse_comment
from lib.queue import Job, JobRequest, QueueError, SQLiteJobQueue

POLL_SECONDS = 60
GITHUB_STATE_LABEL_PREFIX = "orch:state/"
LINEAR_HANDOFF_QUERY = "orch:v1 kind=brief OR orch:v1 kind=go"
GROK_WAKE_REASONS = (
    "blocked",
    "capped",
    "refused",
    "stuck",
    "unparsed",
    "ship-failed",
)
CORE_KINDS = ("go", "ready")


@dataclass(frozen=True)
class Comment:
    id: str
    author: str
    body: str
    created_at: float
    url: str | None = None


@dataclass(frozen=True)
class CIResult:
    status: str
    log: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("pending", "success", "failure"):
            raise ValueError(f"unknown CI status {self.status!r}")


@dataclass(frozen=True)
class TrackedTicket:
    repo: str
    ticket_id: str
    factory: FactoryConfig
    comments: tuple[Comment, ...]
    current_head: str | None
    ci: Mapping[str, CIResult]
    risk: str
    job_timeout_minutes: int = 90

    def __post_init__(self) -> None:
        if not self.repo or not self.ticket_id:
            raise ValueError("repo and ticket_id must not be empty")
        if self.risk not in ("low", "high"):
            raise ValueError("risk must be low or high")
        if self.job_timeout_minutes < 1:
            raise ValueError("job_timeout_minutes must be positive")


@dataclass(frozen=True)
class TicketRef:
    repo: str
    ticket_id: str
    tracker: str


class PollSource(Protocol):
    def github_candidates(self, label_prefix: str) -> tuple[TrackedTicket, ...]: ...

    def linear_candidates(self, query: str) -> tuple[TrackedTicket, ...]: ...

    def tracked_tickets(
        self,
        tickets: tuple[TicketRef, ...],
    ) -> tuple[TrackedTicket, ...]: ...


class ReviewActions(Protocol):
    def set_state(self, ticket: TrackedTicket, state: str) -> None: ...

    def stable_patch_id(self, ticket: TrackedTicket, head: str) -> str: ...

    def is_merged(self, ticket: TrackedTicket, head: str) -> bool:
        """True only when the PR was merged with exactly this head commit."""
        ...

    def merge(
        self,
        ticket: TrackedTicket,
        *,
        squash: bool,
        match_head_commit: str,
    ) -> None: ...

    def handoff(self, ticket: TrackedTicket, head: str) -> None: ...

    def await_approver(
        self,
        ticket: TrackedTicket,
        head: str,
        approver: str,
    ) -> None: ...


class RetryableStateError(RuntimeError):
    pass


class ShipError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShipRequest:
    repo: str
    ticket_id: str
    head: str
    approver: str
    source_comment_id: str


class Hook(Protocol):
    """A project hook (T7, T8) sees each unhandled comment before T1/T2.

    fields is None when the comment has no parseable orch:v1 header.
    handle returns None when the comment is not the hook's event, or
    (handled, outcome); handled comments go into the ledger. Hooks run in
    order and the first answer wins, so (False, outcome) leaves the
    comment for the next poll without offering it to later hooks or the
    core rows. kind=go and kind=ready are never offered to hooks.
    """

    def initialize(self, dispatcher: Dispatcher) -> None: ...

    def handle(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str] | None,
    ) -> tuple[bool, str] | None: ...


class Dispatcher:
    def __init__(
        self,
        state_path: Path,
        queue: SQLiteJobQueue,
        *,
        trusted_logins: Mapping[str, str],
        review_actions: ReviewActions,
        hooks: Sequence[Hook] = (),
    ):
        if state_path.resolve() != queue.path.resolve():
            raise QueueError("dispatcher state and queue must use one database")
        if not trusted_logins:
            raise ValueError("trusted_logins must not be empty")
        if review_actions is None:
            raise ValueError("review_actions must be configured")
        self.state_path = state_path
        self.queue = queue
        self.trusted_logins = dict(trusted_logins)
        self.review_actions = review_actions
        self.hooks = tuple(hooks)
        self._initialize()
        for hook in self.hooks:
            hook.initialize(self)

    def poll_once(self, source: PollSource) -> None:
        self._discover(
            source.github_candidates(GITHUB_STATE_LABEL_PREFIX),
            tracker="github",
        )
        self._discover(
            source.linear_candidates(LINEAR_HANDOFF_QUERY),
            tracker="linear",
        )
        for ticket in source.tracked_tickets(self._tracked_refs()):
            try:
                self.process(ticket)
            except Exception as exc:
                self._record_poll_error(ticket, type(exc).__name__)
        self.reconcile_jobs()

    def process(self, ticket: TrackedTicket) -> None:
        self._settle_external_ships(ticket)
        comments = sorted(ticket.comments, key=lambda item: (item.created_at, item.id))
        for comment in comments:
            if self._is_handled(ticket, comment.id):
                continue
            fields: dict[str, str] | None
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                fields = None
            hooked = self._run_hooks(ticket, comment, fields)
            if hooked is not None:
                handled, outcome = hooked
                if handled:
                    self._mark_handled(ticket, comment.id, outcome)
                continue
            if fields is None:
                continue
            kind = fields["kind"]
            if kind not in ("go", "ready", "verdict", "questions", "answers"):
                continue
            if self._is_stuck(ticket):
                self._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
                self._mark_handled(ticket, comment.id, "ticket-stuck")
                continue
            try:
                if kind == "go":
                    handled, outcome = self._t1(ticket, comment, fields)
                elif kind == "ready":
                    handled, outcome = self._t2(ticket, comment, fields)
                elif kind == "verdict":
                    verdict = fields["verdict"]
                    if verdict == "REVISE":
                        handled, outcome = self._t3(ticket, comment, fields)
                    elif verdict == "APPROVE":
                        handled, outcome = self._t4(ticket, comment, fields)
                    else:
                        continue
                elif kind == "questions":
                    handled, outcome = self._t5_questions(ticket, comment, fields)
                else:
                    handled, outcome = self._t5_answers(ticket, comment, fields)
            except Exception as exc:
                if isinstance(exc, RetryableStateError):
                    raise
                if kind not in ("verdict", "questions", "answers"):
                    raise
                if self._mark_ship_intent_failed(
                    ticket,
                    comment.id,
                    type(exc).__name__,
                ):
                    self._wake_ship_failed(
                        ticket,
                        comment,
                        f"ship raised {type(exc).__name__}: {exc}",
                    )
                self._mark_handled(
                    ticket,
                    comment.id,
                    f"failed-{kind}-{type(exc).__name__}",
                )
                continue
            if handled:
                self._mark_handled(ticket, comment.id, outcome)

    def reconcile_jobs(self) -> None:
        now = self.queue.now()
        for job in self.queue.outstanding():
            activity = (
                job.created_at if job.state == "queued" else job.last_activity_at
            )
            if now - activity >= job.request.timeout_seconds:
                self.queue.mark_timed_out(job)
        for job in self.queue.unhandled_failures():
            self._t6(job)
        self._ensure_all_stuck_wakes()

    def gate_count(self, ticket: TrackedTicket) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT gate_count FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        return int(row["gate_count"]) if row is not None else 0

    def is_stuck(self, ticket: TrackedTicket) -> bool:
        return self._is_stuck(ticket)

    def wake_grok_once(
        self,
        *,
        repo: str,
        ticket_id: str,
        reason: str,
        source_comment_id: str,
        timeout_seconds: int,
        detail: str,
        head: str | None = None,
    ) -> None:
        """T7: one Grok wake per stop, across restarts.

        refused wakes once per ticket. blocked and capped wake once per
        ticket and head. unparsed and ship-failed wake once per comment.
        stuck goes through the ticket's stuck flag, which wakes once.
        """
        if reason not in GROK_WAKE_REASONS:
            raise ValueError(f"unknown wake reason {reason!r}")
        if reason in ("unparsed", "ship-failed"):
            once_key = source_comment_id
        elif reason in ("blocked", "capped"):
            once_key = head or "-"
        else:
            once_key = "-"
        if reason == "stuck":
            self._mark_stuck_and_wake(
                repo=repo,
                ticket_id=ticket_id,
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                reason="t7-stuck",
                detail=detail,
            )
            return
        self.queue.enqueue(
            JobRequest(
                repo=repo,
                ticket_id=ticket_id,
                action=reason,
                recipient="grok",
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                generation=self._generation(repo, ticket_id),
                head=head,
                detail=detail,
            ),
            dedupe_key=f"t7:{_scope(repo, ticket_id)}:{reason}:{once_key}",
        )

    def _run_hooks(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str] | None,
    ) -> tuple[bool, str] | None:
        if fields is not None and fields["kind"] in CORE_KINDS:
            return None
        for hook in self.hooks:
            result = hook.handle(self, ticket, comment, fields)
            if result is not None:
                return result
        return None

    def _t1(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted(comment, fields, "grok-bot", role="orchestrator"):
            return True, "rejected-go-author"
        state = fields.get("state")
        action = {"planning": "plan", "building": "build"}.get(state)
        generation = self._generation(ticket.repo, ticket.ticket_id)
        if action is None:
            self.queue.enqueue(
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="clarify",
                    recipient="grok",
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=generation,
                    detail="kind=go must set state=planning or state=building",
                ),
                dedupe_key=(
                    f"t1-clarify:{_scope(ticket.repo, ticket.ticket_id)}:"
                    f"g{generation}"
                ),
            )
            return True, "queued-go-clarification"
        self.queue.enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action=action,
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
            ),
            dedupe_key=(
                f"t1:{_scope(ticket.repo, ticket.ticket_id)}:{action}:"
                f"g{generation}"
            ),
        )
        self._set_workflow_state(ticket, state)
        return True, f"queued-{action}"

    def _t2(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        builder_harness = self._job_builder_harness(ticket)
        if builder_harness is None:
            return True, "rejected-ready-without-builder-job"
        expected = f"{builder_harness}-bot"
        if not self._trusted(comment, fields, expected, role="builder"):
            return True, "rejected-ready-author"
        reported_head = fields["head"]
        head = _matching_head(reported_head, ticket.current_head)
        if head is None:
            return True, "stale-ready-head"
        result = ticket.ci.get(head) or ticket.ci.get(reported_head)
        if result is None or result.status == "pending":
            return False, "ci-pending"
        timeout_seconds = ticket.job_timeout_minutes * 60
        recorded_status, count = self._record_ci_result(ticket, head, result.status)
        generation = self._generation(ticket.repo, ticket.ticket_id)
        if recorded_status == "success":
            self._queue_review(
                ticket,
                source_comment_id=comment.id,
                head=head,
                generation=generation,
            )
            self._set_workflow_state(ticket, "review")
            return True, "queued-review"
        if count >= 3:
            self._mark_stuck_and_wake(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                source_comment_id=comment.id,
                timeout_seconds=timeout_seconds,
                reason="ci-red-3",
                detail=f"CI failed three times in a row; latest head {head}",
            )
            return True, "stuck-ci-red-3"
        self.queue.enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="fix",
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=timeout_seconds,
                generation=generation,
                head=head,
                detail=result.log or f"CI failed on {head}",
            ),
            dedupe_key=(
                f"t2:{_scope(ticket.repo, ticket.ticket_id)}:{head}:"
                f"g{generation}"
            ),
        )
        return True, "queued-ci-fix"

    def _t3(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-revise-author"
        verdict_head = fields.get("head")
        if _matching_head(verdict_head or "", ticket.current_head) is None:
            return True, "stale-revise"
        round_field = fields.get("round")
        reported_round = (
            int(round_field.split("/", 1)[0]) if round_field is not None else None
        )
        cap = (
            ticket.factory.rounds_low
            if ticket.risk == "low"
            else ticket.factory.rounds_high
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
            if prior is not None:
                connection.commit()
                outcome = str(prior["outcome"])
                replay_state = (
                    "capped" if outcome == "capped-review" else "fixing"
                )
                if replay_state == "capped":
                    self._wake_capped(ticket, comment)
                self._write_state_label(ticket, replay_state)
                return True, outcome
            connection.execute(
                """
                INSERT OR IGNORE INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, generation
                ) VALUES (?, ?, 0, 0, 1)
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
            state = connection.execute(
                """
                SELECT workflow_state, review_round, generation
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            assert state is not None
            round_number = int(state["review_round"]) + 1
            if (
                state["workflow_state"] != "review"
                or reported_round != round_number
            ):
                connection.commit()
                return True, "stale-revise"
            next_state = "capped" if round_number >= cap else "fixing"
            if next_state == "fixing":
                self.queue.enqueue_in_transaction(
                    connection,
                    JobRequest(
                        repo=ticket.repo,
                        ticket_id=ticket.ticket_id,
                        action="fix",
                        recipient=ticket.factory.builder_harness,
                        source_comment_id=comment.id,
                        timeout_seconds=ticket.job_timeout_minutes * 60,
                        generation=int(state["generation"]),
                        head=ticket.current_head,
                        detail=_comment_link(comment),
                    ),
                    dedupe_key=(
                        f"t3:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
                    ),
                )
            connection.execute(
                """
                UPDATE ticket_state
                SET review_round = ?, workflow_state = ?
                WHERE repo = ? AND ticket_id = ?
                """,
                (
                    round_number,
                    next_state,
                    ticket.repo.lower(),
                    ticket.ticket_id,
                ),
            )
            outcome = (
                "capped-review"
                if next_state == "capped"
                else "queued-review-fix"
            )
            connection.execute(
                """
                INSERT INTO review_events (
                    repo, ticket_id, comment_id, outcome
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment.id,
                    outcome,
                ),
            )
            connection.commit()
        if next_state == "capped":
            self._wake_capped(ticket, comment)
        self._write_state_label(ticket, next_state)
        return True, outcome

    def _wake_capped(self, ticket: TrackedTicket, comment: Comment) -> None:
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="capped",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=f"review round cap reached, {_comment_link(comment)}",
            head=ticket.current_head,
        )

    def _t4(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-approve-author"
        current_head = ticket.current_head
        if current_head is None:
            return False, "pr-head-unavailable"
        ship = ticket.factory.ship
        intent = self._ship_intent(ticket, comment.id)
        if intent is not None:
            return self._replay_ship_intent(
                ticket,
                comment,
                current_head,
                ship,
                intent,
            )
        approved_head = fields["head"]
        state, review_round = self._review_context(ticket)
        round_field = fields.get("round")
        reported_round = (
            int(round_field.split("/", 1)[0]) if round_field is not None else None
        )
        review_head = self._latest_review_head(ticket)
        if (
            state != "review"
            or reported_round != review_round + 1
            or review_head is None
            or _matching_head(approved_head, review_head) is None
        ):
            return True, "stale-approve"
        if not _is_full_sha(current_head):
            self._wake_ship_failed(
                ticket,
                comment,
                f"PR head {current_head!r} is not a lowercase 40-character SHA",
            )
            return True, "refused-ship-head"
        result = ticket.ci.get(current_head)
        if result is None or result.status == "pending":
            return False, "approve-ci-not-green"
        if result.status == "failure":
            return True, "approve-ci-red"
        covered = _matching_head(approved_head, current_head) is not None
        if not covered:
            try:
                approved_patch = self.review_actions.stable_patch_id(
                    ticket,
                    approved_head,
                )
                current_patch = self.review_actions.stable_patch_id(
                    ticket,
                    current_head,
                )
            except Exception:
                return self._queue_patch_review(ticket, comment, current_head)
            covered = (
                _is_patch_id(approved_patch)
                and _is_patch_id(current_patch)
                and approved_patch.lower() == current_patch.lower()
            )
        if not covered:
            return self._queue_patch_review(ticket, comment, current_head)
        intent_status, replay = self._prepare_ship_intent(
            ticket,
            comment.id,
            current_head,
            ship,
        )
        if replay:
            intent = self._ship_intent(ticket, comment.id)
            assert intent is not None
            return self._replay_ship_intent(
                ticket,
                comment,
                current_head,
                ship,
                intent,
            )
        assert intent_status == "pending"
        if ship == "merge":
            if self.review_actions.is_merged(ticket, current_head):
                self._complete_ship_intent(ticket, comment.id, "merged")
                self._write_state_label(ticket, "merged")
                return True, "merge-confirmed"
            self.review_actions.merge(
                ticket,
                squash=True,
                match_head_commit=current_head,
            )
            self._complete_ship_intent(ticket, comment.id, "merged")
            self._write_state_label(ticket, "merged")
            return True, "merged"
        if ship == "handoff":
            self.review_actions.handoff(ticket, current_head)
            self._complete_ship_intent(ticket, comment.id, "handed-off")
            self._write_state_label(ticket, "handed-off")
            return True, "handed-off"
        approver = ship.removeprefix("after:")
        self.review_actions.await_approver(ticket, current_head, approver)
        self._complete_ship_intent(ticket, comment.id, "waiting-external")
        self._write_state_label(ticket, "waiting-external")
        return True, "waiting-external"

    def _t5_questions(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_session(comment, fields):
            return True, "rejected-questions-author"
        step = self._latest_step(ticket)
        if step is None:
            action = "clarify"
            recipient = "grok"
            head = ticket.current_head
            generation = self._generation(ticket.repo, ticket.ticket_id)
        else:
            action, recipient, head, generation = step
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            waiting = connection.execute(
                """
                SELECT workflow_state, paused_question_id FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if waiting is not None and waiting["workflow_state"] == "waiting-max":
                connection.commit()
                if waiting["paused_question_id"] == comment.id:
                    self._write_state_label(ticket, "waiting-max")
                    return True, "waiting-max"
                return True, "already-waiting-max"
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, generation,
                    workflow_state, paused_action, paused_recipient,
                    paused_head, paused_generation, paused_question_id
                ) VALUES (?, ?, 0, 0, ?, 'waiting-max', ?, ?, ?, ?, ?)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET
                    workflow_state = 'waiting-max',
                    paused_action = excluded.paused_action,
                    paused_recipient = excluded.paused_recipient,
                    paused_head = excluded.paused_head,
                    paused_generation = excluded.paused_generation,
                    paused_question_id = excluded.paused_question_id
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    generation,
                    action,
                    recipient,
                    head,
                    generation,
                    comment.id,
                ),
            )
            self.queue.enqueue_in_transaction(
                connection,
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="clarify",
                    recipient="grok",
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=generation,
                    head=head,
                    detail=_comment_link(comment),
                ),
                dedupe_key=(
                    f"t5-wait:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
                ),
            )
            connection.commit()
        self._write_state_label(ticket, "waiting-max")
        return True, "waiting-max"

    def _t5_answers(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted(comment, fields, "grok-bot", role="orchestrator"):
            return True, "rejected-answers-author"
        with self._connect() as connection:
            paused = connection.execute(
                """
                SELECT workflow_state, paused_action, paused_recipient,
                       paused_head, paused_generation, paused_question_id
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
            return True, "rejected-answers-without-question"
        action = str(paused["paused_action"])
        recipient = str(paused["paused_recipient"])
        generation = int(paused["paused_generation"])
        self.queue.enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action=action,
                recipient=recipient,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                head=(
                    str(paused["paused_head"])
                    if paused["paused_head"] is not None
                    else None
                ),
                detail=_comment_link(comment),
            ),
            dedupe_key=(
                f"t5:{_scope(ticket.repo, ticket.ticket_id)}:"
                f"{paused['paused_question_id']}:{comment.id}"
            ),
        )
        resumed_state = _state_for_action(action)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ticket_state
                SET workflow_state = ?, paused_action = NULL,
                    paused_recipient = NULL, paused_head = NULL,
                    paused_generation = NULL, paused_question_id = NULL
                WHERE repo = ? AND ticket_id = ?
                """,
                (resumed_state, ticket.repo.lower(), ticket.ticket_id),
            )
        self._write_state_label(ticket, resumed_state)
        return True, f"resumed-{action}"

    def _trusted_reviewer(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> bool:
        return self._trusted(
            comment,
            fields,
            f"{ticket.factory.reviewer_harness}-bot",
            role="reviewer",
        )

    def _trusted_session(
        self,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> bool:
        sender = fields.get("from")
        return sender is not None and self.trusted_logins.get(comment.author) == sender

    def _latest_step(
        self,
        ticket: TrackedTicket,
    ) -> tuple[str, str, str | None, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT action, recipient, head, generation
                FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND action IN ('plan', 'build', 'review', 'fix', 'clarify')
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        return (
            str(row["action"]),
            str(row["recipient"]),
            str(row["head"]) if row["head"] is not None else None,
            int(row["generation"]),
        )

    def _queue_review(
        self,
        ticket: TrackedTicket,
        *,
        source_comment_id: str,
        head: str,
        generation: int,
        detail: str | None = None,
    ) -> None:
        _state, completed_rounds = self._review_context(ticket)
        review_round = completed_rounds + 1
        self.queue.enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="review",
                recipient=ticket.factory.reviewer_harness,
                source_comment_id=source_comment_id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                head=head,
                detail=detail,
            ),
            dedupe_key=(
                f"review:{_scope(ticket.repo, ticket.ticket_id)}:"
                f"{head}:r{review_round}"
            ),
        )

    def _queue_patch_review(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        current_head: str,
    ) -> tuple[bool, str]:
        self._queue_review(
            ticket,
            source_comment_id=comment.id,
            head=current_head,
            generation=self._generation(ticket.repo, ticket.ticket_id),
            detail=f"Approval does not cover current head {current_head}",
        )
        self._set_workflow_state(ticket, "review")
        return True, "queued-patch-review"

    def _review_context(self, ticket: TrackedTicket) -> tuple[str | None, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT workflow_state, review_round FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None, 0
        state = (
            str(row["workflow_state"])
            if row["workflow_state"] is not None
            else None
        )
        return state, int(row["review_round"])

    def _latest_review_head(self, ticket: TrackedTicket) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT head FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None or row["head"] is None:
            return None
        return str(row["head"])

    def _ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
    ) -> tuple[str, str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT head, policy, status FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["head"]), str(row["policy"]), str(row["status"])

    def _replay_ship_intent(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        current_head: str,
        policy: str,
        intent: tuple[str, str, str],
    ) -> tuple[bool, str]:
        intent_head, intent_policy, status = intent
        if intent_head != current_head or intent_policy != policy:
            raise RuntimeError("ship intent does not match approval")
        if status == "completed":
            self._write_state_label(ticket, _ship_state(policy))
            return True, f"ship-already-completed-{policy}"
        if status == "pending":
            if policy == "merge" and self.review_actions.is_merged(
                ticket,
                current_head,
            ):
                self._complete_ship_intent(ticket, comment.id, "merged")
                self._write_state_label(ticket, "merged")
                return True, "merge-confirmed"
            self._hold_ship_intent(ticket, comment.id)
            self._wake_ship_failed(ticket, comment, f"{policy} ship is unconfirmed")
            return True, "ship-unconfirmed"
        if status == "unconfirmed":
            self._wake_ship_failed(ticket, comment, f"{policy} ship is unconfirmed")
            return True, "ship-unconfirmed"
        if status == "failed":
            self._wake_ship_failed(ticket, comment, f"{policy} ship failed")
            return True, "ship-failed"
        raise RuntimeError(f"unknown ship intent status {status!r}")

    def _prepare_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        head: str,
        policy: str,
    ) -> tuple[str, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT head, policy, status FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
            if existing is not None:
                if existing["head"] != head or existing["policy"] != policy:
                    raise RuntimeError("ship intent does not match approval")
                connection.commit()
                return str(existing["status"]), True
            connection.execute(
                """
                INSERT INTO ship_intents (
                    repo, ticket_id, comment_id, head, policy, status
                ) VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment_id,
                    head,
                    policy,
                ),
            )
            connection.execute(
                """
                UPDATE ticket_state SET workflow_state = 'ship'
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
            connection.commit()
        return "pending", False

    def _hold_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ship_intents SET status = 'unconfirmed'
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'pending'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            )
            connection.execute(
                """
                UPDATE ticket_state SET workflow_state = 'ship'
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
            connection.commit()

    def _complete_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        state: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ship_intents
                SET status = 'completed', error = NULL
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            )
            connection.execute(
                """
                UPDATE ticket_state SET workflow_state = ?
                WHERE repo = ? AND ticket_id = ?
                """,
                (state, ticket.repo.lower(), ticket.ticket_id),
            )
            connection.commit()

    def _mark_ship_intent_failed(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        error: str,
    ) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE ship_intents SET status = 'failed', error = ?
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'pending'
                """,
                (error, ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).rowcount
        return changed == 1

    def _wake_ship_failed(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        detail: str,
    ) -> None:
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="ship-failed",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=detail,
        )

    def _has_completed_ship_intent(
        self,
        repo: str,
        ticket_id: str,
        head: str,
        policy: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND head = ? AND policy = ?
                  AND status = 'completed'
                """,
                (repo.lower(), ticket_id, head, policy),
            ).fetchone()
        return row is not None

    def _tracker(self, repo: str, ticket_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT tracker FROM tracked_tickets
                WHERE repo = ? AND ticket_id = ?
                """,
                (repo.lower(), ticket_id),
            ).fetchone()
        return str(row["tracker"]) if row is not None else None

    def _prepare_external_ship(self, request: ShipRequest) -> tuple[str, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT head, status FROM external_ship_intents
                WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                """,
                (request.repo.lower(), request.ticket_id, request.source_comment_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                if existing["head"] != request.head:
                    raise ShipError("ship intent does not match the request head")
                return str(existing["status"]), False
            connection.execute(
                """
                INSERT INTO external_ship_intents (
                    repo, ticket_id, source_comment_id, head, status
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    request.repo.lower(),
                    request.ticket_id,
                    request.source_comment_id,
                    request.head,
                ),
            )
            connection.commit()
        return "pending", True

    def _finish_external_ship(
        self,
        request: ShipRequest,
        status: str,
        error: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE external_ship_intents SET status = ?, error = ?
                WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                  AND status = 'pending'
                """,
                (
                    status,
                    error,
                    request.repo.lower(),
                    request.ticket_id,
                    request.source_comment_id,
                ),
            )
            connection.commit()

    def _settle_external_ship(
        self,
        ticket: TrackedTicket,
        source_comment_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ticket_state SET workflow_state = 'merged'
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
        self._write_state_label(ticket, "merged")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE external_ship_intents SET settled = 1
                WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, source_comment_id),
            )

    def _settle_external_ships(self, ticket: TrackedTicket) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_comment_id FROM external_ship_intents
                WHERE repo = ? AND ticket_id = ?
                  AND status = 'completed' AND settled = 0
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        for row in rows:
            self._settle_external_ship(ticket, str(row["source_comment_id"]))

    def _write_state_label(self, ticket: TrackedTicket, state: str) -> None:
        try:
            self.review_actions.set_state(ticket, state)
        except Exception as exc:
            raise RetryableStateError(state) from exc

    def _set_workflow_state(
        self,
        ticket: TrackedTicket,
        state: str,
    ) -> None:
        self._write_state_label(ticket, state)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, workflow_state
                ) VALUES (?, ?, 0, 0, ?)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET workflow_state = excluded.workflow_state
                """,
                (ticket.repo.lower(), ticket.ticket_id, state),
            )

    def _job_builder_harness(self, ticket: TrackedTicket) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT recipient FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND action IN ('plan', 'build', 'fix')
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["recipient"])

    def _t6(self, job: Job) -> None:
        if job.request.action == "stuck":
            self.queue.mark_failure_handled(job.id)
            return
        if self._is_stuck_identity(job.request.repo, job.request.ticket_id):
            self._ensure_stuck_wake(job.request.repo, job.request.ticket_id)
            self.queue.mark_failure_handled(job.id)
            return
        if job.request.attempt == 1:
            generation = job.request.generation + 1
            request = JobRequest(
                repo=job.request.repo,
                ticket_id=job.request.ticket_id,
                action=job.request.action,
                recipient=job.request.recipient,
                source_comment_id=job.request.source_comment_id,
                timeout_seconds=job.request.timeout_seconds,
                generation=generation,
                attempt=2,
                head=job.request.head,
                detail=job.request.detail,
            )
            self.queue.enqueue_retry(
                job.id,
                request,
                dedupe_key=(
                    f"t6-retry:{job.id}:g{generation}"
                ),
            )
            return
        self._mark_stuck_and_wake(
            repo=job.request.repo,
            ticket_id=job.request.ticket_id,
            source_comment_id=job.request.source_comment_id,
            timeout_seconds=job.request.timeout_seconds,
            reason="job-failed-2",
            detail=f"{job.request.action} job failed twice",
        )
        self.queue.mark_failure_handled(job.id)

    def _mark_stuck_and_wake(
        self,
        *,
        repo: str,
        ticket_id: str,
        source_comment_id: str,
        timeout_seconds: int,
        reason: str,
        detail: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, stuck_reason,
                    stuck_source_comment_id, timeout_seconds, generation
                ) VALUES (?, ?, 0, 1, ?, ?, ?, 1)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET
                    stuck = 1,
                    stuck_reason = excluded.stuck_reason,
                    stuck_source_comment_id = excluded.stuck_source_comment_id,
                    timeout_seconds = excluded.timeout_seconds
                """,
                (
                    repo.lower(),
                    ticket_id,
                    reason,
                    source_comment_id,
                    timeout_seconds,
                ),
            )
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                detail=detail,
            )
            connection.commit()

    def _discover(
        self,
        candidates: tuple[TrackedTicket, ...],
        *,
        tracker: str,
    ) -> None:
        for ticket in candidates:
            if ticket.factory.tickets != tracker:
                continue
            for item in sorted(
                ticket.comments,
                key=lambda value: (value.created_at, value.id),
            ):
                try:
                    fields = parse_comment(item.body).as_dict()
                except HandoffError:
                    continue
                if fields["kind"] not in ("brief", "go"):
                    continue
                if not self._trusted(
                    item,
                    fields,
                    "grok-bot",
                    role="orchestrator",
                ):
                    continue
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO tracked_tickets (
                            repo, ticket_id, tracker, tracked_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            ticket.repo.lower(),
                            ticket.ticket_id,
                            tracker,
                            self.queue.now(),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO ticket_state (
                            repo, ticket_id, gate_count, stuck, generation
                        ) VALUES (?, ?, 0, 0, 1)
                        """,
                        (ticket.repo.lower(), ticket.ticket_id),
                    )
                break

    def _tracked_refs(self) -> tuple[TicketRef, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT repo, ticket_id, tracker
                FROM tracked_tickets
                ORDER BY repo, ticket_id
                """
            ).fetchall()
        return tuple(
            TicketRef(
                repo=str(row["repo"]),
                ticket_id=str(row["ticket_id"]),
                tracker=str(row["tracker"]),
            )
            for row in rows
        )

    def _trusted(
        self,
        comment: Comment,
        fields: Mapping[str, str],
        expected_bot: str,
        *,
        role: str,
    ) -> bool:
        return (
            fields.get("from") == expected_bot
            and fields.get("role") == role
            and self.trusted_logins.get(comment.author) == expected_bot
        )

    def _is_handled(self, ticket: TrackedTicket, comment_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM handled_comments
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
        return row is not None

    def _mark_handled(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        outcome: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO handled_comments (
                    repo, ticket_id, comment_id, outcome
                ) VALUES (?, ?, ?, ?)
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id, outcome),
            )

    def _record_poll_error(self, ticket: TrackedTicket, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO poll_errors (
                    repo, ticket_id, error, recorded_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    error,
                    self.queue.now(),
                ),
            )

    def _is_stuck(self, ticket: TrackedTicket) -> bool:
        return self._is_stuck_identity(ticket.repo, ticket.ticket_id)

    def _is_stuck_identity(self, repo: str, ticket_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT stuck FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (repo.lower(), ticket_id),
            ).fetchone()
        return row is not None and bool(row["stuck"])

    def _generation(self, repo: str, ticket_id: str) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, generation
                ) VALUES (?, ?, 0, 0, 1)
                """,
                (repo.lower(), ticket_id),
            )
            row = connection.execute(
                """
                SELECT generation FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (repo.lower(), ticket_id),
            ).fetchone()
        assert row is not None
        return int(row["generation"])

    def _ensure_all_stuck_wakes(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT repo, ticket_id FROM ticket_state
                WHERE stuck = 1
                """
            ).fetchall()
        for row in rows:
            self._ensure_stuck_wake(str(row["repo"]), str(row["ticket_id"]))

    def _ensure_stuck_wake(self, repo: str, ticket_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                detail=None,
            )
            connection.commit()

    def _ensure_stuck_wake_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        repo: str,
        ticket_id: str,
        detail: str | None,
    ) -> None:
        state = connection.execute(
            """
            SELECT stuck, stuck_reason, stuck_source_comment_id,
                   timeout_seconds, generation, stuck_wake_job_id
            FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
            """,
            (repo.lower(), ticket_id),
        ).fetchone()
        if state is None or not bool(state["stuck"]):
            return
        wake_job_id = state["stuck_wake_job_id"]
        if wake_job_id is not None:
            wake = connection.execute(
                "SELECT state FROM jobs WHERE id = ?",
                (wake_job_id,),
            ).fetchone()
            if wake is None or str(wake["state"]) != "failed":
                return
        generation = int(state["generation"]) + 1
        reason = detail or str(state["stuck_reason"] or "ticket is stuck")
        queued = self.queue.enqueue_in_transaction(
            connection,
            JobRequest(
                repo=repo,
                ticket_id=ticket_id,
                action="stuck",
                recipient="grok",
                source_comment_id=str(state["stuck_source_comment_id"]),
                timeout_seconds=int(state["timeout_seconds"]),
                generation=generation,
                detail=reason,
            ),
            dedupe_key=f"stuck:{_scope(repo, ticket_id)}:g{generation}",
        )
        connection.execute(
            """
            UPDATE ticket_state
            SET generation = ?, stuck_wake_job_id = ?
            WHERE repo = ? AND ticket_id = ?
            """,
            (generation, queued.id, repo.lower(), ticket_id),
        )

    def _record_ci_result(
        self,
        ticket: TrackedTicket,
        head: str,
        status: str,
    ) -> tuple[str, int]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT status, gate_count_after FROM ci_events
                WHERE repo = ? AND ticket_id = ? AND head = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, head),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return str(existing["status"]), int(existing["gate_count_after"])
            row = connection.execute(
                """
                SELECT gate_count FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            previous = int(row["gate_count"]) if row is not None else 0
            count = previous + 1 if status == "failure" else 0
            connection.execute(
                """
                INSERT INTO ci_events (
                    repo, ticket_id, head, status, gate_count_after
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (ticket.repo.lower(), ticket.ticket_id, head, status, count),
            )
            connection.execute(
                """
                INSERT INTO ticket_state (repo, ticket_id, gate_count, stuck)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET gate_count = excluded.gate_count
                """,
                (ticket.repo.lower(), ticket.ticket_id, count),
            )
            connection.commit()
        return status, count

    def _connect(self) -> sqlite3.Connection:
        return self.queue.connect()

    def _initialize(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handled_comments (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_state (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    gate_count INTEGER NOT NULL DEFAULT 0,
                    stuck INTEGER NOT NULL DEFAULT 0,
                    workflow_state TEXT,
                    review_round INTEGER NOT NULL DEFAULT 0,
                    paused_action TEXT,
                    paused_recipient TEXT,
                    paused_head TEXT,
                    paused_generation INTEGER,
                    paused_question_id TEXT,
                    stuck_reason TEXT,
                    stuck_source_comment_id TEXT,
                    stuck_wake_job_id TEXT,
                    timeout_seconds INTEGER,
                    generation INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(ticket_state)"
                ).fetchall()
            }
            if "stuck_wake_job_id" not in columns:
                connection.execute(
                    "ALTER TABLE ticket_state ADD COLUMN stuck_wake_job_id TEXT"
                )
            migrations = {
                "workflow_state": "TEXT",
                "review_round": "INTEGER NOT NULL DEFAULT 0",
                "paused_action": "TEXT",
                "paused_recipient": "TEXT",
                "paused_head": "TEXT",
                "paused_generation": "INTEGER",
                "paused_question_id": "TEXT",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE ticket_state ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ci_events (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    status TEXT NOT NULL,
                    gate_count_after INTEGER NOT NULL,
                    PRIMARY KEY (repo, ticket_id, head)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked_tickets (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    tracker TEXT NOT NULL,
                    tracked_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ship_intents (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS external_ship_intents (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    source_comment_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    settled INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (repo, ticket_id, source_comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_events (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS poll_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    error TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                )
                """
            )


class ReviewShipper:
    """F1-05's ship for T8's after:<approver> ok (the hook_t8 Shipper).

    ship raises unless F1-05 completed an after:<approver> ship intent on
    request.head, CI on request.head is green, and request.head is a full
    SHA that is still the PR head. Then it squash-merges exactly that head,
    or confirms it is already merged. Its own intent is keyed on the ok
    comment, so one request never merges twice. The ticket is re-read
    from source. Once the merge is recorded, ship returns even if the
    merged state or label write fails; Dispatcher.process retries those.
    """

    def __init__(self, dispatcher: Dispatcher, source: PollSource):
        self.dispatcher = dispatcher
        self.source = source

    def ship(self, request: ShipRequest) -> None:
        dispatcher = self.dispatcher
        head = request.head
        policy = f"after:{request.approver}"
        if not _is_full_sha(head):
            raise ShipError(f"ship head {head!r} is not a 40-character SHA")
        if not dispatcher._has_completed_ship_intent(
            request.repo,
            request.ticket_id,
            head,
            policy,
        ):
            raise ShipError(f"no completed {policy} ship intent for {head}")
        ticket = self._ticket(request)
        if ticket.factory.ship != policy:
            raise ShipError(f"ticket ship policy is {ticket.factory.ship}")
        if ticket.current_head != head:
            raise ShipError(f"PR head is {ticket.current_head}, not {head}")
        result = ticket.ci.get(head)
        if result is None or result.status != "success":
            raise ShipError(f"CI on {head} is not green")
        status, fresh = dispatcher._prepare_external_ship(request)
        if status == "pending":
            if dispatcher.review_actions.is_merged(ticket, head):
                pass
            elif not fresh:
                raise ShipError(f"ship of {head} has no recorded result")
            else:
                try:
                    dispatcher.review_actions.merge(
                        ticket,
                        squash=True,
                        match_head_commit=head,
                    )
                except Exception as exc:
                    dispatcher._finish_external_ship(
                        request,
                        "failed",
                        type(exc).__name__,
                    )
                    raise
            dispatcher._finish_external_ship(request, "completed", None)
        elif status != "completed":
            raise ShipError(f"ship of {head} is {status}; not shipping again")
        try:
            dispatcher._settle_external_ship(ticket, request.source_comment_id)
        except Exception:
            # The merge is recorded; Dispatcher.process retries the settle.
            pass

    def _ticket(self, request: ShipRequest) -> TrackedTicket:
        tracker = self.dispatcher._tracker(request.repo, request.ticket_id)
        if tracker is None:
            raise ShipError(f"{request.repo}#{request.ticket_id} is not tracked")
        found = [
            item
            for item in self.source.tracked_tickets(
                (TicketRef(request.repo, request.ticket_id, tracker),)
            )
            if item.repo.lower() == request.repo.lower()
            and item.ticket_id == request.ticket_id
        ]
        if len(found) != 1:
            raise ShipError(f"{request.repo}#{request.ticket_id} was not found")
        return found[0]


class PollLoop:
    def __init__(
        self,
        dispatcher: Dispatcher,
        source: PollSource,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.dispatcher = dispatcher
        self.source = source
        self.sleep = sleep

    def run(self, *, max_polls: int | None = None) -> None:
        if max_polls is not None and max_polls < 1:
            raise ValueError("max_polls must be positive")
        polls = 0
        while True:
            self.dispatcher.poll_once(self.source)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return
            self.sleep(POLL_SECONDS)


def _scope(repo: str, ticket_id: str) -> str:
    return f"{repo.lower()}\x1f{ticket_id}"


def _matching_head(reported: str, current: str | None) -> str | None:
    if current is None or min(len(reported), len(current)) < 7:
        return None
    if current.startswith(reported) or reported.startswith(current):
        return current
    return None


def _comment_link(comment: Comment) -> str:
    return comment.url or f"comment:{comment.id}"


def _is_patch_id(value: str) -> bool:
    return re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


def _is_full_sha(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _ship_state(policy: str) -> str:
    if policy == "merge":
        return "merged"
    if policy == "handoff":
        return "handed-off"
    return "waiting-external"


def _state_for_action(action: str) -> str:
    return {
        "plan": "planning",
        "build": "building",
        "review": "review",
        "fix": "fixing",
        "clarify": "questions",
    }[action]
