"""Blocking `orch wait` and the `orch job` helpers (SPEC F1-06, F2-02).

The waiter sleeps on F1-04's queue (lib/queue.py) until a claim returns a
job, prints it, and exits. It calls no model and never execs a process:
the owning bot that ran the shell call starts its own harness afterwards.
`orch wait <bot> --autostart` (lib/waiter.py, #80) reuses `claim_once` here
and starts the claimed job under `orch job run` itself; this module still
starts nothing.

Since F2-02 the claim goes through the device pool (lib/device_pool.py):
the job moves to running and holds a slot on the first eligible device
in one transaction, and the printed line names the device. With no free
eligible slot the job stays queued and the waiter keeps waiting.

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
from typing import TextIO, TypeVar

from lib.device_pool import DevicePool, Placement
from lib.queue import Job, JobQueue, QueueError, SQLiteJobQueue

POLL_SECONDS = 1.0
BUSY_TIMEOUT_SECONDS = 5.0
LOCK_BACKOFF_SECONDS = 0.25
LOCK_BACKOFF_MAX_SECONDS = 5.0
LOCK_ATTEMPTS = 8
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

T = TypeVar("T")


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
    pool: DevicePool | None = None,
) -> Job:
    """Claim the next job for recipient, print it, and return it.

    With a pool, the claim holds a device slot (F2-02). A locked database
    means the poller is writing; keep waiting with backoff.
    """
    locked = 0
    while True:
        signal.pthread_sigmask(signal.SIG_BLOCK, GUARDED_SIGNALS)
        leaving = True
        try:
            try:
                claimed = claim_once(queue, recipient, pool)
            except Locked:
                claimed = None
                locked += 1
            else:
                locked = 0
            if claimed is not None:
                job, placement = claimed
                deliver(queue, job, out, pool=pool, placement=placement)
                return job
            leaving = False
        finally:
            if leaving:
                for signum in GUARDED_SIGNALS:
                    signal.signal(signum, signal.SIG_IGN)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, GUARDED_SIGNALS)
        sleep(lock_backoff(locked) if locked else POLL_SECONDS)


def retry_locked(
    operation: Callable[[], T],
    *,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Run a queue write, retrying a locked database up to LOCK_ATTEMPTS times."""
    pause = sleep if sleep is not None else time.sleep
    attempt = 1
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_locked(exc) or attempt >= LOCK_ATTEMPTS:
                raise
        pause(lock_backoff(attempt))
        attempt += 1


def is_locked(exc: BaseException) -> bool:
    """SQLITE_BUSY or SQLITE_LOCKED, the transient errors of a held write."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text


def lock_backoff(attempt: int) -> float:
    """Doubling from LOCK_BACKOFF_SECONDS, capped; never computes past the cap."""
    delay = LOCK_BACKOFF_SECONDS
    for _ in range(max(0, attempt - 1)):
        if delay >= LOCK_BACKOFF_MAX_SECONDS:
            break
        delay *= 2
    return min(LOCK_BACKOFF_MAX_SECONDS, delay)


def deliver(
    queue: JobQueue,
    job: Job,
    out: TextIO,
    *,
    pool: DevicePool | None = None,
    placement: Placement | None = None,
) -> None:
    """Print a claimed job. If it does not reach the bot, fail it so T6 retries now.

    An undelivered job never reached a bot, so no worker can exist for it:
    its device slot is released with it.
    """
    try:
        out.write(format_job(job, placement=placement, pool=pool))
        out.flush()
    except Exception as exc:
        reason = _one_line(exc)
        try:
            queue.finish(job.id, owner=job.request.recipient, handoff_comment_id=None)
            if pool is not None and placement is not None:
                pool.release(job.id, owner=job.request.recipient)
        except Exception as finish_exc:
            raise WaitError(
                f"could not deliver job {job.id} ({reason}) or fail it "
                f"({_one_line(finish_exc)}); T6 will time it out"
            ) from finish_exc
        raise WaitError(f"could not deliver job {job.id} ({reason}); failed it for T6") from exc


def format_job(
    job: Job,
    *,
    placement: Placement | None = None,
    pool: DevicePool | None = None,
) -> str:
    payload: dict[str, object] = {
        "id": job.id,
        "bot": f"{job.request.recipient}-bot",
        **asdict(job.request),
    }
    if placement is not None:
        device = pool.config.device(placement.device) if pool is not None else None
        remote = device is None or not device.local
        payload["device"] = placement.device
        payload["remote"] = remote
        if remote and device is not None:
            payload["workdir"] = (
                f"{device.workdir_for(placement.harness)}/jobs/{job.id}"
            )
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


class Locked(Exception):
    """The poller holds a write: try the claim again after a backoff."""


def claim_once(
    queue: JobQueue,
    recipient: str,
    pool: DevicePool | None,
    *,
    autostart: tuple[int, str | None] | None = None,
) -> tuple[Job, Placement | None] | None:
    try:
        if pool is not None:
            if autostart is not None:
                return pool.claim(recipient, autostart=autostart)
            return pool.claim(recipient)
        job = queue.claim(recipient)
        return None if job is None else (job, None)
    except (*STORAGE_ERRORS, TypeError, ValueError) as exc:
        if is_locked(exc):
            raise Locked from exc
        raise WaitError(f"claim failed: {_one_line(exc)}") from exc


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text or type(exc).__name__
