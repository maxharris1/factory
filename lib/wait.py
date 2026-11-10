"""Blocking `orch wait` and the `orch job` helpers (SPEC F1-06).

The waiter sleeps on F1-04's queue (lib/queue.py) until `claim` returns a
job, prints it, and exits. It calls no model and never execs a process:
the owning bot that ran the shell call starts its own harness afterwards.

An owning bot is `<recipient>-bot`, the name the dispatcher already
expects on a `ready` comment. Only the three harness bots may wait.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from lib.queue import Job, JobQueue, QueueError, SQLiteJobQueue

POLL_SECONDS = 1.0

OWNING_BOTS = {
    "claude-bot": "claude",
    "codex-bot": "codex",
    "cursor-bot": "cursor",
}


class WaitError(Exception):
    """The waiter cannot use the queue. The message is for stderr."""


def recipient_for(bot: str) -> str:
    try:
        return OWNING_BOTS[bot]
    except KeyError:
        allowed = ", ".join(OWNING_BOTS)
        raise WaitError(f"unknown owning bot {bot!r}; expected one of {allowed}") from None


def open_queue(path: Path) -> SQLiteJobQueue:
    if not path.is_file():
        raise WaitError(f"no dispatcher database at {path}")
    try:
        return SQLiteJobQueue(path, clock=time.time)
    except (QueueError, sqlite3.Error) as exc:
        raise WaitError(f"cannot open dispatcher database {path}: {exc}") from exc


def wait_for_job(
    queue: JobQueue,
    recipient: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Job:
    while True:
        try:
            job = queue.claim(recipient)
        except (QueueError, sqlite3.Error, TypeError, ValueError) as exc:
            raise WaitError(f"claim failed: {exc}") from exc
        if job is not None:
            return job
        sleep(POLL_SECONDS)


def deliver(queue: JobQueue, job: Job, out: TextIO) -> None:
    """Print a claimed job. If it cannot reach the bot, fail it so T6 retries now."""
    try:
        out.write(format_job(job))
        out.flush()
    except BaseException:
        queue.finish(job.id, handoff_comment_id=None)
        raise


def format_job(job: Job) -> str:
    payload = {
        "id": job.id,
        "bot": f"{job.request.recipient}-bot",
        **asdict(job.request),
    }
    return json.dumps(payload, sort_keys=True) + "\n"


def owned_job(queue: SQLiteJobQueue, bot: str, job_id: str) -> None:
    recipient = recipient_for(bot)
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT recipient FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise WaitError(f"unknown job {job_id!r}")
    if row["recipient"] != recipient:
        raise WaitError(f"job {job_id} belongs to {row['recipient']}-bot, not {bot}")
