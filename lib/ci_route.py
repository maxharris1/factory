from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
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


def grant_step(slot: str) -> str:
    return f"local {slot}"


@dataclass(frozen=True)
class Job:
    run_id: int
    name: str
    status: str
    labels: tuple[str, ...]
    created_at: datetime
    finished_steps: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Route:
    online: int
    claimed: int
    reserved: int
    free: int
    hosted: HostedSlots


def parse_job(raw: dict) -> Job:
    return Job(
        run_id=int(raw["run_id"]),
        name=raw["name"],
        status=raw["status"],
        labels=tuple(raw["labels"]),
        created_at=datetime.fromisoformat(raw["created_at"]).astimezone(timezone.utc),
        finished_steps=tuple((step["name"], step["conclusion"]) for step in raw.get("steps") or ()
                             if step["status"] == "completed"),
    )


def _slot(name: str) -> str:
    matrix = _MATRIX.fullmatch(name)
    return f"test-{matrix.group(1)}" if matrix else name


def _granted(changes: Job) -> int | None:
    finished = dict(changes.finished_steps)
    outcomes = [finished.get(grant_step(slot)) for slot in SLOTS]
    if None in outcomes:
        return None
    return outcomes.count("success")


def reserved_slots(own_run_id: int, other_jobs: Iterable[Job]) -> int:
    runs: defaultdict[int, list[Job]] = defaultdict(list)
    for job in other_jobs:
        runs[job.run_id].append(job)
    reserved = 0
    for run_id, jobs in runs.items():
        changes = next((job for job in jobs if job.name == "changes"), None)
        if changes is None or any(_slot(job.name) in SLOTS for job in jobs):
            continue
        granted = _granted(changes)
        if granted is not None:
            reserved += granted
        elif run_id < own_run_id:
            reserved += len(SLOTS)
    return reserved


def decide(slots: Sequence[str], online: int, other_jobs: Iterable[Job], reserved: int,
           now: datetime) -> Route:
    pool = [job for job in other_jobs if POOL_LABEL in job.labels]
    running = any(job.status == "in_progress" for job in pool)
    stalled = any(job.status == "queued" and now - job.created_at > STALL_AFTER for job in pool)
    claimed = sum(job.status in _CLAIMING for job in pool)
    free = 0 if stalled and not running else max(0, online - claimed - reserved)
    return Route(online=online, claimed=claimed, reserved=reserved, free=free, hosted=tuple(slots[free:]))


def _http_get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _write_output(hosted: HostedSlots) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"hosted={json.dumps(list(hosted), separators=(',', ':'))}\n")


def main() -> int:
    try:
        token = os.environ["GITHUB_TOKEN"]
        own = int(os.environ["GITHUB_RUN_ID"])
        online = int(json.loads(os.environ["FACTORY_LINUX_POOL"])["online"])
        runs_url = (f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}"
                    f"/repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs")
        run_ids = {run["id"] for status in _CLAIMING
                   for run in _http_get_json(f"{runs_url}?status={status}&per_page=100", token)["workflow_runs"]}
        jobs = [parse_job(raw) for run_id in sorted(run_ids - {own})
                for raw in _http_get_json(f"{runs_url}/{run_id}/jobs?per_page=100", token)["jobs"]]
        route = decide(SLOTS, online, jobs, reserved_slots(own, jobs), datetime.now(timezone.utc))
        _write_output(route.hosted)
        print(f"ci_route: online={route.online} claimed={route.claimed} reserved={route.reserved} "
              f"free={route.free} hosted={list(route.hosted)}")
    except Exception as error:
        if "GITHUB_OUTPUT" in os.environ:
            _write_output(())
        print(f"::warning::ci_route: {type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
