"""Durable dispatcher queue shared with waiting bots.

Consumers use four operations: enqueue, claim, heartbeat, and finish.
Claim is non-blocking; F1-06 may block around it without changing this
interface. A finish with no handoff comment is a failed job for T6.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

ACTIONS = (
    "plan",
    "build",
    "review",
    "fix",
    "clarify",
    "stuck",
    "blocked",
    "capped",
    "refused",
    "unparsed",
    "ship-failed",
)
RECIPIENTS = ("claude", "codex", "cursor", "grok")
STATES = ("queued", "running", "done", "failed")


class QueueError(ValueError):
    """The requested queue operation is invalid."""


@dataclass(frozen=True)
class JobRequest:
    repo: str
    ticket_id: str
    action: str
    recipient: str
    source_comment_id: str
    timeout_seconds: int
    generation: int = 1
    attempt: int = 1
    head: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Job:
    id: str
    dedupe_key: str
    request: JobRequest
    state: str
    created_at: float
    last_activity_at: float
    finished_at: float | None
    handoff_comment_id: str | None


class JobQueue(Protocol):
    def enqueue(
        self,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> Job: ...

    def claim(self, recipient: str) -> Job | None: ...

    def heartbeat(self, job_id: str, *, owner: str) -> Job: ...

    def finish(
        self,
        job_id: str,
        *,
        owner: str,
        handoff_comment_id: str | None,
    ) -> Job: ...


class DispatcherQueue(JobQueue, Protocol):
    def outstanding(self) -> tuple[Job, ...]: ...

    def mark_timed_out(self, job: Job) -> bool: ...

    def unhandled_failures(self) -> tuple[Job, ...]: ...

    def mark_failure_handled(self, job_id: str) -> None: ...


class SQLiteJobQueue:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float],
    ):
        self.path = path
        self.clock = clock
        self._initialize()

    def now(self) -> float:
        return self.clock()

    def connect(self) -> sqlite3.Connection:
        return self._connect()

    def enqueue(
        self,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> Job:
        with self._connect() as connection:
            return self.enqueue_in_transaction(
                connection,
                request,
                dedupe_key=dedupe_key,
            )

    def enqueue_retry(
        self,
        failed_job_id: str,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> tuple[Job, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            job = self.enqueue_in_transaction(
                connection,
                request,
                dedupe_key=dedupe_key,
            )
            inserted = connection.total_changes > before
            if inserted:
                handled = connection.execute(
                    """
                    UPDATE jobs SET failure_handled = 1
                    WHERE id = ? AND state = 'failed' AND failure_handled = 0
                    """,
                    (failed_job_id,),
                ).rowcount
                if handled != 1:
                    raise QueueError("retry requires one unhandled failed job")
            connection.commit()
            return job, inserted

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> Job:
        _validate_request(request)
        if not dedupe_key:
            raise QueueError("dedupe_key must not be empty")
        job_id = uuid.uuid4().hex
        now = self.now()
        connection.execute(
            """
            INSERT OR IGNORE INTO jobs (
                id, dedupe_key, repo, ticket_id, action, recipient,
                source_comment_id, timeout_seconds, generation, attempt,
                head, detail, state, created_at, last_activity_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job_id,
                dedupe_key,
                request.repo,
                request.ticket_id,
                request.action,
                request.recipient,
                request.source_comment_id,
                request.timeout_seconds,
                request.generation,
                request.attempt,
                request.head,
                request.detail,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM jobs WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        assert row is not None
        return _job(row)

    def claim(self, recipient: str) -> Job | None:
        if recipient not in RECIPIENTS:
            raise QueueError(f"unknown recipient {recipient!r}")
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'queued' AND recipient = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (recipient,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET state = 'running', last_activity_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (now, row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        assert updated is not None
        return _job(updated)

    def heartbeat(self, job_id: str, *, owner: str) -> Job:
        if owner not in RECIPIENTS:
            raise QueueError(f"unknown owner {owner!r}")
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE jobs SET last_activity_at = ?
                WHERE id = ? AND state = 'running' AND recipient = ?
                """,
                (now, job_id, owner),
            ).rowcount
            if changed != 1:
                current = connection.execute(
                    "SELECT recipient FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if current is None:
                    raise QueueError(f"unknown job {job_id!r}")
                if current["recipient"] != owner:
                    raise QueueError(
                        f"job {job_id} belongs to {current['recipient']}, not {owner}"
                    )
                raise QueueError("heartbeat requires a running job")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return _job(row)

    def finish(
        self,
        job_id: str,
        *,
        owner: str,
        handoff_comment_id: str | None,
    ) -> Job:
        if owner not in RECIPIENTS:
            raise QueueError(f"unknown owner {owner!r}")
        now = self.now()
        target = "done" if handoff_comment_id is not None else "failed"
        if handoff_comment_id == "":
            raise QueueError("handoff_comment_id must not be empty")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE jobs
                SET state = ?, finished_at = ?, last_activity_at = ?,
                    handoff_comment_id = ?
                WHERE id = ? AND state = 'running' AND recipient = ?
                """,
                (target, now, now, handoff_comment_id, job_id, owner),
            ).rowcount
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if updated is None:
                raise QueueError(f"unknown job {job_id!r}")
            if changed != 1:
                if updated["recipient"] != owner:
                    raise QueueError(
                        f"job {job_id} belongs to {updated['recipient']}, not {owner}"
                    )
                if (
                    updated["state"] == target
                    and updated["handoff_comment_id"] == handoff_comment_id
                ):
                    connection.commit()
                    return _job(updated)
                raise QueueError("finish requires a running job")
            connection.commit()
        return _job(updated)

    def outstanding(self) -> tuple[Job, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state IN ('queued', 'running')
                ORDER BY created_at, id
                """
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def mark_timed_out(self, job: Job) -> bool:
        now = self.now()
        if job.state == "queued":
            predicate = "state = 'queued' AND created_at = ?"
            expected = job.created_at
        elif job.state == "running":
            predicate = "state = 'running' AND last_activity_at = ?"
            expected = job.last_activity_at
        else:
            return False
        with self._connect() as connection:
            changed = connection.execute(
                f"""
                UPDATE jobs
                SET state = 'failed', finished_at = ?, last_activity_at = ?
                WHERE id = ? AND {predicate}
                """,
                (now, now, job.id, expected),
            ).rowcount
        return changed == 1

    def unhandled_failures(self) -> tuple[Job, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'failed' AND failure_handled = 0
                ORDER BY finished_at, id
                """
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def mark_failure_handled(self, job_id: str) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE jobs SET failure_handled = 1
                WHERE id = ? AND state = 'failed' AND failure_handled = 0
                """,
                (job_id,),
            ).rowcount
        if changed != 1:
            raise QueueError("failure is missing or already handled")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size == 0:
            raise QueueError("database file is empty")
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        repo TEXT NOT NULL,
                        ticket_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        recipient TEXT NOT NULL,
                        source_comment_id TEXT NOT NULL,
                        timeout_seconds INTEGER NOT NULL,
                        generation INTEGER NOT NULL,
                        attempt INTEGER NOT NULL,
                        head TEXT,
                        detail TEXT,
                        state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        last_activity_at REAL NOT NULL,
                        finished_at REAL,
                        handoff_comment_id TEXT,
                        failure_handled INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
        except sqlite3.DatabaseError as exc:
            raise QueueError("database file is not valid SQLite") from exc


def _validate_request(request: JobRequest) -> None:
    if not request.repo or not request.ticket_id or not request.source_comment_id:
        raise QueueError("repo, ticket_id and source_comment_id must not be empty")
    if request.action not in ACTIONS:
        raise QueueError(f"unknown action {request.action!r}")
    if request.recipient not in RECIPIENTS:
        raise QueueError(f"unknown recipient {request.recipient!r}")
    if request.timeout_seconds < 1:
        raise QueueError("timeout_seconds must be positive")
    if request.generation < 1:
        raise QueueError("generation must be positive")
    if request.attempt not in (1, 2):
        raise QueueError("attempt must be 1 or 2")


def _job(row: sqlite3.Row) -> Job:
    request = JobRequest(
        repo=str(row["repo"]),
        ticket_id=str(row["ticket_id"]),
        action=str(row["action"]),
        recipient=str(row["recipient"]),
        source_comment_id=str(row["source_comment_id"]),
        timeout_seconds=int(row["timeout_seconds"]),
        generation=int(row["generation"]),
        attempt=int(row["attempt"]),
        head=str(row["head"]) if row["head"] is not None else None,
        detail=str(row["detail"]) if row["detail"] is not None else None,
    )
    return Job(
        id=str(row["id"]),
        dedupe_key=str(row["dedupe_key"]),
        request=request,
        state=str(row["state"]),
        created_at=float(row["created_at"]),
        last_activity_at=float(row["last_activity_at"]),
        finished_at=(
            float(row["finished_at"]) if row["finished_at"] is not None else None
        ),
        handoff_comment_id=(
            str(row["handoff_comment_id"])
            if row["handoff_comment_id"] is not None
            else None
        ),
    )
