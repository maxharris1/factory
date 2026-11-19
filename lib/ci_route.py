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
        reserved = reserved_slots(own, jobs)
        hosted = decide(SLOTS, online, jobs, reserved, datetime.now(timezone.utc))
        _write_output(hosted)
        print(f"ci_route: online={online} reserved={reserved} hosted={list(hosted)}")
    except Exception as error:
        if "GITHUB_OUTPUT" in os.environ:
            _write_output(())
        print(f"::warning::ci_route: {type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
