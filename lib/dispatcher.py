"""Dispatcher transitions T1 through T6."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lib.factory_md import FactoryConfig
from lib.handoff import HandoffError, parse_comment
from lib.queue import Job, JobRequest, QueueError, SQLiteJobQueue

POLL_SECONDS = 60
GITHUB_STATE_LABEL_PREFIX = "orch:state/"
LINEAR_HANDOFF_QUERY = "orch:v1 kind=brief OR orch:v1 kind=go"


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
    job_timeout_minutes: int = 90

    def __post_init__(self) -> None:
        if not self.repo or not self.ticket_id:
            raise ValueError("repo and ticket_id must not be empty")
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


class _MissingReviewActions:
    def set_state(self, ticket: TrackedTicket, state: str) -> None:
        del ticket, state

    def stable_patch_id(self, ticket: TrackedTicket, head: str) -> str:
        del ticket, head
        raise RuntimeError("review actions are not configured")

    def merge(
        self,
        ticket: TrackedTicket,
        *,
        squash: bool,
        match_head_commit: str,
    ) -> None:
        del ticket, squash, match_head_commit
        raise RuntimeError("review actions are not configured")

    def handoff(self, ticket: TrackedTicket, head: str) -> None:
        del ticket, head
        raise RuntimeError("review actions are not configured")

    def await_approver(
        self,
        ticket: TrackedTicket,
        head: str,
        approver: str,
    ) -> None:
        del ticket, head, approver
        raise RuntimeError("review actions are not configured")


class Dispatcher:
    def __init__(
        self,
        state_path: Path,
        queue: SQLiteJobQueue,
        *,
        trusted_logins: Mapping[str, str],
        review_actions: ReviewActions | None = None,
    ):
        if state_path.resolve() != queue.path.resolve():
            raise QueueError("dispatcher state and queue must use one database")
        if not trusted_logins:
            raise ValueError("trusted_logins must not be empty")
        self.state_path = state_path
        self.queue = queue
        self.trusted_logins = dict(trusted_logins)
        self.review_actions = review_actions or _MissingReviewActions()
        self._initialize()

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
            self.process(ticket)
        self.reconcile_jobs()

    def process(self, ticket: TrackedTicket) -> None:
        comments = sorted(ticket.comments, key=lambda item: (item.created_at, item.id))
        for comment in comments:
            if self._is_handled(ticket, comment.id):
                continue
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                continue
            kind = fields["kind"]
            if kind not in ("go", "ready", "verdict", "questions", "answers"):
                continue
            if self._is_stuck(ticket):
                self._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
                self._mark_handled(ticket, comment.id, "ticket-stuck")
                continue
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
                handled, outcome = self._t5_questions(ticket, comment)
            else:
                handled, outcome = self._t5_answers(ticket, comment, fields)
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
            self.queue.enqueue(
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="review",
                    recipient=ticket.factory.reviewer_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=timeout_seconds,
                    generation=generation,
                    head=head,
                ),
                dedupe_key=(
                    f"t2:{_scope(ticket.repo, ticket.ticket_id)}:{head}:"
                    f"g{generation}"
                ),
            )
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
        risk = fields.get("risk")
        round_field = fields.get("round")
        if risk not in ("low", "high") or round_field is None:
            return True, "rejected-revise-without-risk-or-round"
        round_number = int(round_field.split("/", 1)[0])
        cap = (
            ticket.factory.rounds_low
            if risk == "low"
            else ticket.factory.rounds_high
        )
        self._record_review_round(ticket, round_number)
        if round_number >= cap:
            self._set_workflow_state(ticket, "capped")
            return True, "capped-review"
        generation = self._generation(ticket.repo, ticket.ticket_id)
        self.queue.enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="fix",
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                head=ticket.current_head,
                detail=_comment_link(comment),
            ),
            dedupe_key=(
                f"t3:{_scope(ticket.repo, ticket.ticket_id)}:"
                f"r{round_number}:g{generation}"
            ),
        )
        self._set_workflow_state(ticket, "fixing")
        return True, "queued-review-fix"

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
        approved_head = fields["head"]
        covered = _matching_head(approved_head, current_head) is not None
        if not covered:
            covered = (
                self.review_actions.stable_patch_id(ticket, approved_head)
                == self.review_actions.stable_patch_id(ticket, current_head)
            )
        if not covered:
            generation = self._generation(ticket.repo, ticket.ticket_id)
            self.queue.enqueue(
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="review",
                    recipient=ticket.factory.reviewer_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=generation,
                    head=current_head,
                    detail=(
                        f"Approved head {approved_head} differs from "
                        f"current head {current_head}"
                    ),
                ),
                dedupe_key=(
                    f"t4-review:{_scope(ticket.repo, ticket.ticket_id)}:"
                    f"{current_head}:g{generation}"
                ),
            )
            self._set_workflow_state(ticket, "review")
            return True, "queued-patch-review"
        ship = ticket.factory.ship
        if ship == "merge":
            self.review_actions.merge(
                ticket,
                squash=True,
                match_head_commit=current_head,
            )
            self._set_workflow_state(ticket, "merged")
            return True, "merged"
        if ship == "handoff":
            self.review_actions.handoff(ticket, current_head)
            self._set_workflow_state(ticket, "handed-off")
            return True, "handed-off"
        approver = ship.removeprefix("after:")
        self.review_actions.await_approver(ticket, current_head, approver)
        self._set_workflow_state(ticket, "waiting-external")
        return True, "waiting-external"

    def _t5_questions(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str]:
        step = self._latest_step(ticket)
        if step is None:
            action = "clarify"
            recipient = "grok"
            head = ticket.current_head
            generation = self._generation(ticket.repo, ticket.ticket_id)
        else:
            action, recipient, head, generation = step
        with self._connect() as connection:
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
        self.review_actions.set_state(ticket, "waiting-max")
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
                SELECT paused_action, paused_recipient, paused_head,
                       paused_generation, paused_question_id
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                  AND workflow_state = 'waiting-max'
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if paused is None or paused["paused_action"] is None:
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
        self.review_actions.set_state(ticket, resumed_state)
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

    def _record_review_round(
        self,
        ticket: TrackedTicket,
        round_number: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, review_round
                ) VALUES (?, ?, 0, 0, ?)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET review_round = max(review_round, excluded.review_round)
                """,
                (ticket.repo.lower(), ticket.ticket_id, round_number),
            )

    def _set_workflow_state(
        self,
        ticket: TrackedTicket,
        state: str,
    ) -> None:
        self.review_actions.set_state(ticket, state)
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


def _state_for_action(action: str) -> str:
    return {
        "plan": "planning",
        "build": "building",
        "review": "review",
        "fix": "fixing",
        "clarify": "questions",
    }[action]
