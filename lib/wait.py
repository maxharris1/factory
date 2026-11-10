"""Blocking `orch wait` (SPEC F1-06).

The process sleeps on the local job list until `claim` returns a job.
It does not call a model, and it does not start a harness. The owning
bot is the process that invoked the shell call; that bot starts the
harness after this process exits.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from lib.dispatcher_queue import Job, JobQueue, require_role

POLL_SECONDS = 0.05


def wait_for_job(
    queue: JobQueue,
    role: str,
    *,
    pause: Callable[[float], None] = time.sleep,
) -> Job:
    require_role(role)
    while True:
        job = queue.claim(role)
        if job is not None:
            return job
        pause(POLL_SECONDS)


def format_wait_output(job: Job) -> str:
    meta = json.dumps(
        {"id": job.id, "note": job.note, "role": job.role, "step": job.step},
        separators=(",", ":"),
        sort_keys=True,
    )
    return meta + "\n" + job.comment
