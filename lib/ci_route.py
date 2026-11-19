from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

POOL_LABEL = "factory-linux"
SLOTS = ("test-1", "test-2", "test-3", "statechart", "checks", "py311-compile", "macos-pickup")
STALL_AFTER = timedelta(minutes=3)
_CLAIMING = ("queued", "in_progress")
_MATRIX = re.compile(r"test \((\d+)/\d+\)")
HostedSlots = tuple[str, ...]


@dataclass(frozen=True)
class Job:
    run_id: int
    name: str
    status: str
    labels: tuple[str, ...]
    created_at: datetime


def parse_job(raw: dict) -> Job:
    return Job(
        run_id=int(raw["run_id"]),
        name=raw["name"],
        status=raw["status"],
        labels=tuple(raw["labels"]),
        created_at=datetime.fromisoformat(raw["created_at"]).astimezone(timezone.utc),
    )


def _slot(name: str) -> str:
    matrix = _MATRIX.fullmatch(name)
    return f"test-{matrix.group(1)}" if matrix else name


def reserved_slots(own_run_id: int, other_jobs: Iterable[Job]) -> int:
    runs: defaultdict[int, list[Job]] = defaultdict(list)
    for job in other_jobs:
        runs[job.run_id].append(job)
    unlisted = [
        run_id for run_id, jobs in runs.items()
        if run_id < own_run_id
        and any(job.name == "changes" for job in jobs)
        and not any(_slot(job.name) in SLOTS for job in jobs)
    ]
    return len(unlisted) * len(SLOTS)


def decide(slots: Sequence[str], online: int, other_jobs: Iterable[Job], reserved: int,
           now: datetime) -> HostedSlots:
    pool = [job for job in other_jobs if POOL_LABEL in job.labels]
    running = any(job.status == "in_progress" for job in pool)
    stalled = any(job.status == "queued" and now - job.created_at > STALL_AFTER for job in pool)
    claimed = sum(job.status in _CLAIMING for job in pool)
    free = 0 if stalled and not running else max(0, online - claimed - reserved)
    return tuple(slots[free:])
