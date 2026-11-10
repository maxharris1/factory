"""F1-04 dispatcher transitions T1, T2 and T6.

Poll sources supply already-tracked tickets in chronological comment
order. They must read policy from the trusted base branch and provide
the plan/build decision because orch:v1 kind=go does not encode it.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lib.factory_md import FactoryConfig
from lib.handoff import HandoffError, parse_comment
from lib.queue import DispatcherQueue, Job, JobRequest

POLL_SECONDS = 60


@dataclass(frozen=True)
class Comment:
    id: str
    author: str
    body: str
    created_at: float


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
    requires_plan: bool = False
    job_timeout_minutes: int = 90

    def __post_init__(self) -> None:
        if not self.repo or not self.ticket_id:
            raise ValueError("repo and ticket_id must not be empty")
        if self.job_timeout_minutes < 1:
            raise ValueError("job_timeout_minutes must be positive")


class PollSource(Protocol):
    def tracked_tickets(self) -> tuple[TrackedTicket, ...]: ...


class Dispatcher:
    def __init__(
        self,
        state_path: Path,
        queue: DispatcherQueue,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.state_path = state_path
        self.queue = queue
        self.clock = clock
        self._initialize()

    def poll_once(self, source: PollSource) -> None:
        for ticket in source.tracked_tickets():
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
            if kind not in ("go", "ready"):
                continue
            if self._is_stuck(ticket):
                self._mark_handled(ticket, comment.id, "ticket-stuck")
                continue
            if kind == "go":
                handled, outcome = self._t1(ticket, comment, fields)
            else:
                handled, outcome = self._t2(ticket, comment, fields)
            if handled:
                self._mark_handled(ticket, comment.id, outcome)

    def reconcile_jobs(self) -> None:
        now = self.clock()
        for job in self.queue.running():
            if now - job.last_activity_at >= job.request.timeout_seconds:
                self.queue.mark_timed_out(job, now=now)
        for job in self.queue.unhandled_failures():
            self._t6(job, now)

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
        if (
            fields.get("from") != "grok-bot"
            or fields.get("role") != "orchestrator"
            or comment.author != fields["from"]
        ):
            return True, "rejected-go-author"
        action = "plan" if ticket.requires_plan else "build"
        self.queue.enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action=action,
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
            ),
            dedupe_key=f"t1:{_scope(ticket.repo, ticket.ticket_id)}",
            now=self.clock(),
        )
        return True, f"queued-{action}"

    def _t2(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        sender = fields.get("from")
        if (
            sender is None
            or comment.author != sender
            or fields.get("role") != "builder"
        ):
            return True, "rejected-ready-author"
        head = fields["head"]
        if ticket.current_head is None or head != ticket.current_head:
            return True, "stale-ready-head"
        result = ticket.ci.get(head)
        if result is None or result.status == "pending":
            return False, "ci-pending"
        timeout_seconds = ticket.job_timeout_minutes * 60
        recorded_status, count = self._record_ci_result(ticket, head, result.status)
        if recorded_status == "success":
            self.queue.enqueue(
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="review",
                    recipient=ticket.factory.reviewer_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=timeout_seconds,
                    head=head,
                ),
                dedupe_key=f"t2:{_scope(ticket.repo, ticket.ticket_id)}:{head}",
                now=self.clock(),
            )
            return True, "queued-review"
        if count >= 3:
            self._mark_stuck(ticket, "ci-red-3")
            self._enqueue_stuck(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                source_comment_id=comment.id,
                timeout_seconds=timeout_seconds,
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
                head=head,
                detail=result.log or f"CI failed on {head}",
            ),
            dedupe_key=f"t2:{_scope(ticket.repo, ticket.ticket_id)}:{head}",
            now=self.clock(),
        )
        return True, "queued-ci-fix"

    def _t6(self, job: Job, now: float) -> None:
        if job.request.action == "stuck":
            self.queue.mark_failure_handled(job.id)
            return
        if job.request.attempt == 1:
            request = JobRequest(
                repo=job.request.repo,
                ticket_id=job.request.ticket_id,
                action=job.request.action,
                recipient=job.request.recipient,
                source_comment_id=job.request.source_comment_id,
                timeout_seconds=job.request.timeout_seconds,
                attempt=2,
                head=job.request.head,
                detail=job.request.detail,
            )
            self.queue.enqueue(
                request,
                dedupe_key=f"t6-retry:{job.id}",
                now=now,
            )
            self.queue.mark_failure_handled(job.id)
            return
        self._mark_stuck_by_identity(
            job.request.repo,
            job.request.ticket_id,
            "job-failed-2",
        )
        self._enqueue_stuck(
            repo=job.request.repo,
            ticket_id=job.request.ticket_id,
            source_comment_id=job.request.source_comment_id,
            timeout_seconds=job.request.timeout_seconds,
            detail=f"{job.request.action} job failed twice",
        )
        self.queue.mark_failure_handled(job.id)

    def _enqueue_stuck(
        self,
        *,
        repo: str,
        ticket_id: str,
        source_comment_id: str,
        timeout_seconds: int,
        detail: str,
    ) -> None:
        self.queue.enqueue(
            JobRequest(
                repo=repo,
                ticket_id=ticket_id,
                action="stuck",
                recipient="grok",
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                detail=detail,
            ),
            dedupe_key=f"stuck:{_scope(repo, ticket_id)}",
            now=self.clock(),
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
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT stuck FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        return row is not None and bool(row["stuck"])

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

    def _mark_stuck(self, ticket: TrackedTicket, reason: str) -> None:
        self._mark_stuck_by_identity(ticket.repo, ticket.ticket_id, reason)

    def _mark_stuck_by_identity(
        self,
        repo: str,
        ticket_id: str,
        reason: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, stuck_reason
                ) VALUES (?, ?, 0, 1, ?)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET stuck = 1, stuck_reason = excluded.stuck_reason
                """,
                (repo.lower(), ticket_id, reason),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

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
                    stuck_reason TEXT,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
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
