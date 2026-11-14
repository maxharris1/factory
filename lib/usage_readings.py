"""Subscription usage readings per bot account (issue #151).

Sources, chosen by Max (PR #234, 2026-11-14):

* Claude: the reply to Claude Code's SDK control request `get_usage`,
  which `orch job run` sends once at each job's start (lib/usage_probe.py)
  with no model turn. `parse_get_usage` keeps `rate_limits.five_hour` and
  `rate_limits.seven_day`: `utilization` (0-100) and `resets_at`. While
  the login cannot report limits (`rate_limits_available` false) there is
  no reading.
* Codex: the `rate_limits` of the last `token_count` event in that job's
  own session rollout (lib/codex_rollout.py). `parse_codex` maps each of
  `primary` and `secondary` to a window by `window_minutes` (300, 10080),
  never by position, and keeps `used_percent` and the reset time.

Only the window, the used fraction and the reset time are kept, as numbers.
A missing or invalid used value stays None, never 0. A source that answers
without a window (a null or missing window, a login without limits, a
`token_count` with no `rate_limits`, or a reset time not within
RESET_HORIZON_SECONDS of now) gives that window an explicit no-reading
(`used` and `resets_at` None), which replaces any older numbers. A Codex
reading is observed at its event's own time, so rereading the same event
never makes it fresher. `Session._write`
(lib/telemetry.py) stores them in `usage_readings` in the same transaction
as the job's telemetry row, on the existing heartbeat flush. A reading is
keyed by harness, bot (the account's OS user) and device, and an older
reading never replaces a newer one. Cursor is not tracked.

`TelemetryUsage` is a `lib.usage.UsageSource` over the table. Its report is
verified only when both windows are fresh, so routing keeps the existing
verified-report rule. `orchestrate.choose_route` does not use it yet.

Nothing here opens a file or runs a process: readings come from parsed
CLI output and orch.db rows only.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

from lib.usage import UsageReport

TABLE = "usage_readings"
WINDOWS = ("five_hour", "seven_day")
SOURCES = {"claude": "get_usage", "codex": "rollout"}
NOT_TRACKED = frozenset({"cursor"})
CODEX_WINDOW_MINUTES = {300: "five_hour", 10080: "seven_day"}
GET_USAGE_ID = "orch-usage"
RESET_HORIZON_SECONDS = 30 * 24 * 3600
CLOCK_SKEW_SECONDS = 300
STALE_SECONDS = 24 * 3600
MAX_REPLY_BYTES = 256 * 1024

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    harness TEXT NOT NULL,
    bot TEXT NOT NULL,
    device TEXT NOT NULL,
    window TEXT NOT NULL,
    used REAL,
    resets_at INTEGER,
    observed_at REAL NOT NULL,
    job_id TEXT NOT NULL,
    PRIMARY KEY (harness, bot, device, window)
)
"""

_UPSERT = f"""
INSERT INTO {TABLE} (harness, bot, device, window, used, resets_at, observed_at, job_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (harness, bot, device, window) DO UPDATE SET
    used = excluded.used,
    resets_at = excluded.resets_at,
    observed_at = excluded.observed_at,
    job_id = excluded.job_id
WHERE excluded.observed_at > {TABLE}.observed_at
"""


@dataclass(frozen=True)
class Reading:
    window: str
    used: float | None
    resets_at: int | None
    observed_at: float


def missing(window: str, observed_at: float) -> Reading:
    return Reading(window, None, None, observed_at)


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(DDL)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def used_fraction(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 1 else None


def used_percent(value: object) -> float | None:
    number = _number(value)
    return round(number / 100, 6) if number is not None and 0 <= number <= 100 else None


def reset_time(value: object, now: float) -> int | None:
    number = _number(value)
    if number is None or number != int(number) or abs(number - now) > RESET_HORIZON_SECONDS:
        return None
    return int(number)


def iso_time(value: object) -> float | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def parse_get_usage(stdout: bytes, now: float) -> list[Reading]:
    """The 5-hour and 7-day readings in Claude Code's `get_usage` reply.

    A null window or a bad reset gives that window a no-reading, and a
    login without limits (`rate_limits_available` not true) gives both.
    An error reply, or no reply, is no observation at all."""
    for line in stdout[:MAX_REPLY_BYTES].splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "control_response":
            continue
        response = event.get("response")
        if not isinstance(response, dict) or response.get("request_id") != GET_USAGE_ID:
            continue
        reply = response.get("response")
        if response.get("subtype") != "success" or not isinstance(reply, dict):
            return []
        limits = reply.get("rate_limits")
        if reply.get("rate_limits_available") is not True or not isinstance(limits, dict):
            return [missing(window, now) for window in WINDOWS]
        found = []
        for window in WINDOWS:
            info = limits.get(window)
            resets = iso_time(info.get("resets_at")) if isinstance(info, dict) else None
            resets_at = reset_time(int(resets), now) if resets is not None else None
            if resets_at is None:
                found.append(missing(window, now))
            else:
                found.append(Reading(window, used_percent(info.get("utilization")), resets_at, now))
        return found
    return []


def parse_codex(found: object, now: float) -> list[Reading]:
    """The windows in one Codex rollout `token_count` reading.

    `found` is lib/codex_rollout.py's output: `rate_limits` and the event's
    `timestamp`. Every reading is observed at the event's time, never now,
    and an event without a time (or stamped after now plus
    CLOCK_SKEW_SECONDS) gives nothing. A reset is `resets_at` (epoch
    seconds), else the event time plus `resets_in_seconds`. A window the
    event does not report, or reports without a valid reset, is a
    no-reading; null `rate_limits` makes both windows no-readings."""
    if not isinstance(found, dict):
        return []
    emitted = iso_time(found.get("timestamp"))
    if emitted is None or emitted > now + CLOCK_SKEW_SECONDS:
        return []
    observed = min(emitted, now)
    limits = found.get("rate_limits")
    readings: dict[str, Reading] = {}
    for key in ("primary", "secondary"):
        info = limits.get(key) if isinstance(limits, dict) else None
        if not isinstance(info, dict):
            continue
        minutes = info.get("window_minutes")
        window = CODEX_WINDOW_MINUTES.get(minutes) if type(minutes) is int else None
        if window is None or window in readings:
            continue
        resets_at = reset_time(info.get("resets_at"), now)
        seconds = _number(info.get("resets_in_seconds"))
        if resets_at is None and seconds is not None and seconds >= 0:
            resets_at = reset_time(round(emitted + seconds), now)
        readings[window] = (
            missing(window, observed)
            if resets_at is None
            else Reading(window, used_percent(info.get("used_percent")), resets_at, observed)
        )
    return [readings.get(window) or missing(window, observed) for window in WINDOWS]


def store(
    connection: sqlite3.Connection,
    *,
    harness: str,
    bot: str,
    device: str,
    job_id: str,
    readings: list[Reading],
) -> None:
    if not readings:
        return
    ensure_table(connection)
    connection.executemany(
        _UPSERT,
        [
            (harness, bot, device, r.window, r.used, r.resets_at, r.observed_at, job_id)
            for r in readings
        ],
    )


def from_row(window: object, used: object, resets_at: object, observed_at: object) -> Reading | None:
    """A stored row, re-validated; None when any key value is not what `store` writes."""
    if window not in WINDOWS:
        return None
    observed = _number(observed_at)
    if observed is None:
        return None
    if resets_at is None:
        return missing(str(window), observed) if used is None else None
    if isinstance(resets_at, bool) or not isinstance(resets_at, int):
        return None
    return Reading(str(window), used_fraction(used), resets_at, observed)


def window_state(reading: Reading, now: float) -> str:
    """`ok`; `stale` once the window has reset or the reading is old; else `no reading`."""
    if (reading.resets_at is not None and reading.resets_at <= now) or (
        now - reading.observed_at > STALE_SECONDS
    ):
        return "stale"
    return "ok" if reading.used is not None else "no reading"


def headroom(readings: list[Reading], now: float) -> float | None:
    """The least `1 - used` over fresh windows, or None when none is fresh."""
    fresh = [1.0 - r.used for r in readings if r.used is not None and window_state(r, now) == "ok"]
    return round(min(fresh), 6) if fresh else None


class TelemetryUsage:
    """`UsageSource` over `usage_readings` for one bot account on one device."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        bot_for: Callable[[str], str | None],
        device: str,
        clock: Callable[[], float] = time.time,
    ):
        self.connect = connect
        self.bot_for = bot_for
        self.device = device
        self.clock = clock

    def read(self, harness: str) -> UsageReport | None:
        bot = self.bot_for(harness)
        if harness not in SOURCES or bot is None:
            return None
        try:
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    f"SELECT window, used, resets_at, observed_at FROM {TABLE}"
                    " WHERE harness = ? AND bot = ? AND device = ?",
                    (harness, bot, self.device),
                ).fetchall()
        except sqlite3.Error:
            return None
        now = self.clock()
        readings = {r.window: r for r in (from_row(*row) for row in rows) if r is not None}
        remaining = headroom(list(readings.values()), now)
        if remaining is None:
            return None
        verified = all(
            window in readings and window_state(readings[window], now) == "ok" for window in WINDOWS
        )
        return UsageReport(harness, remaining, verified)
