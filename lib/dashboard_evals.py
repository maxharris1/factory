"""The Monday eval reports, collected for the dashboard as evals.json.

deploy/weekly-eval.sh writes `orch eval-report --json` each week as
weekly-YYYY-MM-DD.json in the evals dir. `collect_weekly` reads the newest
KEEP of them into one bundle:

    {schema: 1, state: "ok" | "no snapshots" | "no dir", skipped,
     weeks: [{week: "YYYY-MM-DD", since, until,
              metrics: {key: number | null | "unrecorded"}}]}

Weeks are newest first. A candidate is a name starting `weekly-` with
`.json` in it. A candidate is skipped and counted in `skipped` when its name
or date is malformed, when it is not one regular, singly linked file opened
without following a link inside the dir, when it is over MAX_SNAPSHOT_BYTES,
or when it is not UTF-8 JSON (NaN and Infinity included) with an object
`metrics`. Inside a kept week only METRIC_KEYS are kept, any value but a
finite number or null becomes "unrecorded", and a `since` or `until` that is
not YYYY-MM-DDTHH:MM:SSZ becomes null. While the bundle is over
MAX_BUNDLE_BYTES, its oldest week is dropped and counted as skipped.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import date
from pathlib import Path

SCHEMA = 1
NAME = re.compile(r"^weekly-(\d{4}-\d{2}-\d{2})\.json$", re.ASCII)
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", re.ASCII)
KEEP = 12
MAX_SNAPSHOT_BYTES = 256 * 1024
MAX_BUNDLE_BYTES = 64 * 1024
UNRECORDED = "unrecorded"
METRIC_KEYS = (
    "tickets-succeeded",
    "first-pass-approve-rate",
    "cap-rate",
    "poll-errors",
    "stuck-jobs",
    "failed-jobs",
    "cycle-time-p50-hours",
    "cycle-time-p90-hours",
    "max-interventions",
    "wasted-wakes",
    "productive-cost-usd",
    "wasted-cost-usd",
)


def collect_weekly(evals_dir: Path) -> dict:
    try:
        dir_fd = os.open(evals_dir, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return _bundle("no dir", [], 0)
    try:
        return _collect(dir_fd)
    finally:
        os.close(dir_fd)


def _collect(dir_fd: int) -> dict:
    with os.scandir(dir_fd) as entries:
        names = [entry.name for entry in entries]
    skipped = 0
    dated: list[str] = []
    for name in names:
        if not name.startswith("weekly-") or ".json" not in name:
            continue
        if _week(name) is None:
            skipped += 1
        else:
            dated.append(name)
    weeks: list[dict] = []
    for name in sorted(dated, reverse=True):
        if len(weeks) == KEEP:
            break
        week = _read(dir_fd, name)
        if week is None:
            skipped += 1
        else:
            weeks.append(week)
    while weeks and len(evals_json(_bundle("ok", weeks, skipped))) > MAX_BUNDLE_BYTES:
        weeks.pop()
        skipped += 1
    return _bundle("ok" if weeks else "no snapshots", weeks, skipped)


def evals_json(bundle: dict | None) -> str:
    """Compact JSON with <, > and & written as JSON escapes, as tickets.json is."""
    text = json.dumps(
        bundle if bundle is not None else _bundle("no dir", [], 0),
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") + "\n"


def _bundle(state: str, weeks: list[dict], skipped: int) -> dict:
    return {"schema": SCHEMA, "state": state, "weeks": list(weeks), "skipped": skipped}


def _week(name: str) -> str | None:
    match = NAME.fullmatch(name)
    if match is None:
        return None
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return None
    return match.group(1)


def _read(dir_fd: int, name: str) -> dict | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        if info.st_size > MAX_SNAPSHOT_BYTES:
            return None
        data = handle.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES:
        return None
    try:
        report = json.loads(data.decode("utf-8"), parse_constant=_reject)
    except (ValueError, RecursionError):
        return None
    if not isinstance(report, dict) or not isinstance(report.get("metrics"), dict):
        return None
    window = report.get("window")
    window = window if isinstance(window, dict) else {}
    metrics = report["metrics"]
    return {
        "week": _week(name),
        "since": _time(window.get("since")),
        "until": _time(window.get("until")),
        "metrics": {key: _value(metrics[key]) for key in METRIC_KEYS if key in metrics},
    }


def _reject(constant: str) -> object:
    raise ValueError(f"{constant} is not a number")


def _value(value: object) -> object:
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return UNRECORDED


def _time(value: object) -> str | None:
    return value if isinstance(value, str) and TIME.fullmatch(value) else None
