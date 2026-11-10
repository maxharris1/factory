"""Blocking `orch wait` and the `orch job` helpers (SPEC F1-06).

The waiter sleeps on F1-04's queue (lib/queue.py) until `claim` returns a
job, prints it, and exits. It calls no model and never execs a process:
the owning bot that ran the shell call starts its own harness afterwards.

An owning bot is `<recipient>-bot`, the name the dispatcher already
expects on a `ready` comment. Only the three harness bots may wait.

The waiter only opens a database the dispatcher already created: every
connection is `mode=rw`, and a file without the jobs table is refused,
so nothing is created or written into an unrelated database.

SIGINT and SIGTERM are blocked from each claim until its output is
flushed. A job is failed for T6 only when its delivery did not complete.
"""

from __future__ import annotations

import json
import signal
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from lib.queue import Job, JobQueue, QueueError, SQLiteJobQueue

POLL_SECONDS = 1.0
BUSY_TIMEOUT_SECONDS = 30.0
GUARDED_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM})

OWNING_BOTS = {
    "claude-bot": "claude",
    "codex-bot": "codex",
    "cursor-bot": "cursor",
}

JOB_COLUMNS = frozenset(
    {
        "id",
        "dedupe_key",
        "repo",
        "ticket_id",
        "action",
        "recipient",
        "source_comment_id",
        "timeout_seconds",
        "generation",
        "attempt",
        "head",
        "detail",
        "state",
        "created_at",
        "last_activity_at",
        "finished_at",
        "handoff_comment_id",
        "failure_handled",
    }
)

STORAGE_ERRORS = (QueueError, sqlite3.Error, OSError)


class WaitError(Exception):
    """The waiter cannot use the queue. The message is one line for stderr."""


class ExistingJobQueue(SQLiteJobQueue):
    """F1-04's queue, restricted to a database the dispatcher already made."""

    def _connect(self) -> sqlite3.Connection:
        uri = self.path.absolute().as_uri() + "?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            rows = connection.execute("PRAGMA table_info(jobs)").fetchall()
        finally:
            connection.close()
        if not rows:
            raise QueueError("database has no jobs table")
        missing = JOB_COLUMNS - {str(row["name"]) for row in rows}
        if missing:
            raise QueueError(f"jobs table is missing {', '.join(sorted(missing))}")


def recipient_for(bot: str) -> str:
    try:
        return OWNING_BOTS[bot]
    except KeyError:
        allowed = ", ".join(OWNING_BOTS)
        raise WaitError(f"unknown owning bot {bot!r}; expected one of {allowed}") from None


def open_queue(path: Path) -> ExistingJobQueue:
    try:
        return ExistingJobQueue(path, clock=time.time)
    except STORAGE_ERRORS as exc:
        raise WaitError(f"cannot open dispatcher database {path}: {_one_line(exc)}") from exc


def wait_for_job(
    queue: JobQueue,
    recipient: str,
    out: TextIO,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Job:
    """Claim the next job for recipient, print it, and return it."""
    while True:
        signal.pthread_sigmask(signal.SIG_BLOCK, GUARDED_SIGNALS)
        leaving = True
        try:
            job = _claim(queue, recipient)
            if job is not None:
                deliver(queue, job, out)
                return job
            leaving = False
        finally:
            if leaving:
                for signum in GUARDED_SIGNALS:
                    signal.signal(signum, signal.SIG_IGN)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, GUARDED_SIGNALS)
        sleep(POLL_SECONDS)


def deliver(queue: JobQueue, job: Job, out: TextIO) -> None:
    """Print a claimed job. If it does not reach the bot, fail it so T6 retries now."""
    try:
        out.write(format_job(job))
        out.flush()
    except Exception as exc:
        reason = _one_line(exc)
        try:
            queue.finish(job.id, owner=job.request.recipient, handoff_comment_id=None)
        except Exception as finish_exc:
            raise WaitError(
                f"could not deliver job {job.id} ({reason}) or fail it "
                f"({_one_line(finish_exc)}); T6 will time it out"
            ) from finish_exc
        raise WaitError(f"could not deliver job {job.id} ({reason}); failed it for T6") from exc


def format_job(job: Job) -> str:
    payload = {
        "id": job.id,
        "bot": f"{job.request.recipient}-bot",
        **asdict(job.request),
    }
    return json.dumps(payload, sort_keys=True) + "\n"


def owned_job(queue: SQLiteJobQueue, bot: str, job_id: str) -> None:
    recipient = recipient_for(bot)
    try:
        with queue.connect() as connection:
            row = connection.execute(
                "SELECT recipient FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
    except STORAGE_ERRORS as exc:
        raise WaitError(f"cannot read job {job_id}: {_one_line(exc)}") from exc
    if row is None:
        raise WaitError(f"unknown job {job_id!r}")
    if row["recipient"] != recipient:
        raise WaitError(f"job {job_id} belongs to {row['recipient']}-bot, not {bot}")


def _claim(queue: JobQueue, recipient: str) -> Job | None:
    try:
        return queue.claim(recipient)
    except (*STORAGE_ERRORS, TypeError, ValueError) as exc:
        raise WaitError(f"claim failed: {_one_line(exc)}") from exc


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text or type(exc).__name__
