"""F1-04 dispatcher transitions T1, T2 and T6, plus project hooks."""

from __future__ import annotations

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
GROK_WAKE_REASONS = ("blocked", "capped", "refused", "stuck", "unparsed")


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


class Hook(Protocol):
    """A project hook (T7, T8) sees each unhandled comment before T1/T2.

    fields is None when the comment has no parseable orch:v1 header.
    handle returns None when the comment is not the hook's event, or
    (handled, outcome); handled comments go into the ledger.
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
        hooks: Sequence[Hook] = (),
    ):
        if state_path.resolve() != queue.path.resolve():
            raise QueueError("dispatcher state and queue must use one database")
        if not trusted_logins:
            raise ValueError("trusted_logins must not be empty")
        self.state_path = state_path
        self.queue = queue
        self.trusted_logins = dict(trusted_logins)
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
            self.process(ticket)
        self.reconcile_jobs()

    def process(self, ticket: TrackedTicket) -> None:
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
            if kind not in ("go", "ready"):
                continue
            if self._is_stuck(ticket):
                self._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
                self._mark_handled(ticket, comment.id, "ticket-stuck")
                continue
            if kind == "go":
                handled, outcome = self._t1(ticket, comment, fields)
            else:
                handled, outcome = self._t2(ticket, comment, fields)
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
        once_key: str = "-",
        head: str | None = None,
    ) -> None:
        """T7: one Grok wake per (ticket, reason, once_key), across restarts.

        stuck goes through the ticket's stuck flag, which wakes once.
        """
        if reason not in GROK_WAKE_REASONS:
            raise ValueError(f"unknown wake reason {reason!r}")
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
