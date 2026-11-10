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
from typing import Protocol

ACTIONS = ("plan", "build", "review", "fix", "stuck")
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
        now: float,
    ) -> Job: ...

    def claim(self, recipient: str, *, now: float) -> Job | None: ...

    def heartbeat(self, job_id: str, *, now: float) -> Job: ...

    def finish(
        self,
        job_id: str,
        *,
        handoff_comment_id: str | None,
        now: float,
    ) -> Job: ...


class DispatcherQueue(JobQueue, Protocol):
    def running(self) -> tuple[Job, ...]: ...

    def mark_timed_out(self, job: Job, *, now: float) -> bool: ...

    def unhandled_failures(self) -> tuple[Job, ...]: ...

    def mark_failure_handled(self, job_id: str) -> None: ...


class SQLiteJobQueue:
    def __init__(self, path: Path):
        self.path = path
        self._initialize()

    def enqueue(
        self,
        request: JobRequest,
        *,
        dedupe_key: str,
        now: float,
    ) -> Job:
        _validate_request(request)
        if not dedupe_key:
            raise QueueError("dedupe_key must not be empty")
        job_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    id, dedupe_key, repo, ticket_id, action, recipient,
                    source_comment_id, timeout_seconds, attempt, head, detail,
                    state, created_at, last_activity_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
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

    def claim(self, recipient: str, *, now: float) -> Job | None:
        if recipient not in RECIPIENTS:
            raise QueueError(f"unknown recipient {recipient!r}")
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

    def heartbeat(self, job_id: str, *, now: float) -> Job:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE jobs SET last_activity_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (now, job_id),
            ).rowcount
            if changed != 1:
                raise QueueError("heartbeat requires a running job")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        assert row is not None
        return _job(row)

    def finish(
        self,
        job_id: str,
        *,
        handoff_comment_id: str | None,
        now: float,
    ) -> Job:
        target = "done" if handoff_comment_id is not None else "failed"
        if handoff_comment_id == "":
            raise QueueError("handoff_comment_id must not be empty")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise QueueError(f"unknown job {job_id!r}")
            if row["state"] == target and row["handoff_comment_id"] == handoff_comment_id:
                return _job(row)
            if row["state"] != "running":
                raise QueueError("finish requires a running job")
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, finished_at = ?, last_activity_at = ?,
                    handoff_comment_id = ?
                WHERE id = ?
                """,
                (target, now, now, handoff_comment_id, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        assert updated is not None
        return _job(updated)

    def running(self) -> tuple[Job, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE state = 'running' ORDER BY created_at, id"
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def mark_timed_out(self, job: Job, *, now: float) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE jobs
                SET state = 'failed', finished_at = ?, last_activity_at = ?
                WHERE id = ? AND state = 'running' AND last_activity_at = ?
                """,
                (now, now, job.id, job.last_activity_at),
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


def _validate_request(request: JobRequest) -> None:
    if not request.repo or not request.ticket_id or not request.source_comment_id:
        raise QueueError("repo, ticket_id and source_comment_id must not be empty")
    if request.action not in ACTIONS:
        raise QueueError(f"unknown action {request.action!r}")
    if request.recipient not in RECIPIENTS:
        raise QueueError(f"unknown recipient {request.recipient!r}")
    if request.timeout_seconds < 1:
        raise QueueError("timeout_seconds must be positive")
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
