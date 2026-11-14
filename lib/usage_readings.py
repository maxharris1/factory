"""Subscription usage readings from the CLI's own event stream (issue #151).

Claude Code's `--output-format stream-json` (which `orch job run` already
adds) emits `rate_limit_event` lines. `parse_claude` keeps only the numbers
and enums of the 5-hour and 7-day windows: the used fraction, the reset time
and the CLI's status word. No other text from the event is kept. The
telemetry `Summary` holds the latest reading per window, and
`Session._write` stores them in `usage_readings` in the same transaction as
the job's telemetry row, on the existing heartbeat flush.

A reading is keyed by harness, bot (the account's OS user) and device. An
older reading never replaces a newer one.

SOURCES names the chosen signal per harness. Codex has none until Max picks
one (docs/design/mission-control/STATUS.md), so it reads as no reading.
Cursor is not tracked.

`TelemetryUsage` is a `lib.usage.UsageSource` over the table. Its report is
verified only when both windows are fresh, so routing keeps the existing
verified-report rule. `orchestrate.choose_route` does not use it yet.

Nothing here opens a file or runs a process: readings come from parsed
events and orch.db rows only.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass

from lib.usage import UsageReport

TABLE = "usage_readings"
WINDOWS = ("five_hour", "seven_day")
STATUSES = ("allowed", "allowed_warning", "rejected")
SOURCES = {"claude": "rate_limit_event"}
NOT_TRACKED = frozenset({"cursor"})
RESET_HORIZON_SECONDS = 30 * 24 * 3600
STALE_SECONDS = 24 * 3600

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    harness TEXT NOT NULL,
    bot TEXT NOT NULL,
    device TEXT NOT NULL,
    window TEXT NOT NULL,
    used REAL,
    resets_at INTEGER NOT NULL,
    status TEXT,
    observed_at REAL NOT NULL,
    job_id TEXT NOT NULL,
    PRIMARY KEY (harness, bot, device, window)
)
"""

_UPSERT = f"""
INSERT INTO {TABLE} (harness, bot, device, window, used, resets_at, status, observed_at, job_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (harness, bot, device, window) DO UPDATE SET
    used = excluded.used,
    resets_at = excluded.resets_at,
    status = excluded.status,
    observed_at = excluded.observed_at,
    job_id = excluded.job_id
WHERE excluded.observed_at > {TABLE}.observed_at
"""


@dataclass(frozen=True)
class Reading:
    window: str
    used: float | None
    resets_at: int
    status: str | None
    observed_at: float


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(DDL)


def used_fraction(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > 1:
        return None
    return float(value)


def reset_time(value: object, now: float) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value != int(value):
        return None
    if abs(value - now) > RESET_HORIZON_SECONDS:
        return None
    return int(value)


def status_word(value: object) -> str | None:
    return value if isinstance(value, str) and value in STATUSES else None


def _reading(window: object, info: object, used_key: str, now: float) -> Reading | None:
    if window not in WINDOWS or not isinstance(info, dict):
        return None
    resets_at = reset_time(info.get("resetsAt"), now)
    if resets_at is None:
        return None
    return Reading(
        str(window),
        used_fraction(info.get(used_key)),
        resets_at,
        status_word(info.get("status")),
        now,
    )


def parse_claude(event: dict, now: float) -> list[Reading]:
    """The 5-hour and 7-day readings in one `rate_limit_event`.

    `unifiedWindows` when present, else the top-level `rateLimitType` with
    its optional `utilization`. A missing utilization is kept as None, never
    as 0. Other windows (per model, overage) and invalid values are dropped."""
    info = event.get("rate_limit_info")
    if event.get("type") != "rate_limit_event" or not isinstance(info, dict):
        return []
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict) and windows:
        found = (_reading(name, value, "utilization", now) for name, value in windows.items())
    else:
        found = iter([_reading(info.get("rateLimitType"), info, "utilization", now)])
    return [reading for reading in found if reading is not None]


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
            (harness, bot, device, r.window, r.used, r.resets_at, r.status, r.observed_at, job_id)
            for r in readings
        ],
    )


def from_row(
    window: object, used: object, resets_at: object, status: object, observed_at: object
) -> Reading | None:
    """A stored row, re-validated; None when any key value is not what `store` writes."""
    if window not in WINDOWS:
        return None
    if isinstance(resets_at, bool) or not isinstance(resets_at, int):
        return None
    if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
        return None
    if not math.isfinite(observed_at):
        return None
    return Reading(str(window), used_fraction(used), resets_at, status_word(status), float(observed_at))


def window_state(reading: Reading, now: float) -> str:
    """`ok`; `stale` once the window has reset or the reading is old; else `no reading`."""
    if reading.resets_at <= now or now - reading.observed_at > STALE_SECONDS:
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
                    f"SELECT window, used, resets_at, status, observed_at FROM {TABLE}"
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
