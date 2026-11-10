"""Local job-list seam for the F1-04 dispatcher (SPEC sections 1, 4.2, 15).

The dispatcher queues a job. The owning bot blocks in `orch wait <role>`
until `claim` returns it, then marks it with `orch job done`. F1-04 is
not in this tree. `open_queue` returns `StubJobQueue`, a file-backed
stand-in for that local list. F1-04 drops in by making `open_queue`
return its queue. These methods stay, in this order:

- enqueue(role, step, comment, note="") -> Job
- claim(role) -> Job | None
- complete(job_id) -> Job

`role` is the waiter (the owning bot). It must be a role token the
orch:v1 parser accepts. `step` is the action the transition table
queues: build, plan, review, or fix. `comment` is one orch:v1 handoff.
`note` is the failing log or link the table attaches, or "". `claim`
returns the oldest queued job for that role and marks it claimed.
`complete` accepts only a claimed job.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from lib.handoff import (
    HandoffError,
    format_comment,
    format_header,
    parse_comment,
    parse_header_line,
)

STEPS = ("build", "plan", "review", "fix")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


class JobQueueError(Exception):
    """The local job list refused the operation."""


@dataclass(frozen=True)
class Job:
    id: str
    role: str
    step: str
    comment: str
    note: str


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(self, role: str, step: str, comment: str, note: str = "") -> Job: ...

    def claim(self, role: str) -> Job | None: ...

    def complete(self, job_id: str) -> Job: ...


def require_role(role: str) -> str:
    parsed = parse_header_line(format_header({"kind": "go", "role": role}))
    if parsed.get("role") != role:
        raise HandoffError(f"bad role {role!r}")
    return role


def is_job_id(job_id: str) -> bool:
    return _JOB_ID.fullmatch(job_id) is not None


def default_queue_path() -> Path:
    override = os.environ.get("ORCH_JOB_QUEUE")
    if override:
        return Path(override)
    return Path.home() / ".factory" / "job-queue"


def open_queue(path: Path) -> JobQueue:
    """Return the stub queue. F1-04 replaces this function."""
    return StubJobQueue(path)


class StubJobQueue:
    def __init__(self, root: Path) -> None:
        self.root = root

    def enqueue(self, role: str, step: str, comment: str, note: str = "") -> Job:
        role = require_role(role)
        if step not in STEPS:
            raise JobQueueError(f"unknown step {step!r}")
        job = Job(
            id=uuid.uuid4().hex,
            role=role,
            step=step,
            comment=format_comment(parse_comment(comment)),
            note=note,
        )
        with self._lock():
            seq = self._next_seq()
            self._write(self.root / "queued" / f"{seq:016d}-{job.id}.json", job)
        return job

    def claim(self, role: str) -> Job | None:
        role = require_role(role)
        with self._lock():
            queued = self.root / "queued"
            if not queued.is_dir():
                return None
            for path in sorted(queued.glob("[0-9]*-*.json")):
                job = self._read(path)
                if job.role != role:
                    continue
                os.replace(path, self.root / "claimed" / f"{job.id}.json")
                return job
        return None

    def complete(self, job_id: str) -> Job:
        if not is_job_id(job_id):
            raise JobQueueError(f"bad job id {job_id!r}")
        with self._lock():
            src = self.root / "claimed" / f"{job_id}.json"
            if not src.is_file():
                raise JobQueueError(f"job {job_id} is not claimed")
            job = self._read(src)
            os.replace(src, self.root / "done" / f"{job_id}.json")
            return job

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("queued", "claimed", "done"):
            (self.root / name).mkdir(exist_ok=True)
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _next_seq(self) -> int:
        path = self.root / "next-seq"
        current = int(path.read_text(encoding="utf-8")) if path.exists() else 0
        nxt = current + 1
        path.write_text(f"{nxt}\n", encoding="utf-8")
        return nxt

    def _write(self, path: Path, job: Job) -> None:
        payload = {
            "id": job.id,
            "role": job.role,
            "step": job.step,
            "comment": job.comment,
            "note": job.note,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)

    def _read(self, path: Path) -> Job:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Job(
            id=data["id"],
            role=data["role"],
            step=data["step"],
            comment=format_comment(parse_comment(data["comment"])),
            note=data["note"],
        )
