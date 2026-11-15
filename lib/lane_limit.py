"""A harness account's usage limit pauses its recipient's lane (#367).

When a run's stream reports the account's usage limit, telemetry records
the lane as limited until the reset. claim_order (lib/queue.py) claims
nothing for that recipient while the limit is active, on every device,
because every claim reads the box's orch.db. T6 requeues the dead job as
itself with nothing charged.

A limit lifts at `until`. Storing readings (usage_readings.store) keeps
the account's evidence per window: the newest reading of that window on any
device, free at its observation when it shows room, else at its own reset.
An older reading never replaces a window's newer evidence, and a no-reading
is no evidence. Once any window's evidence is newer than a limit, `until`
moves to when every window is free, so a healthy reading of one window never
clears another window still full. A later limit replaces the row with its
own reset. Nothing deletes a row, so recording and reading are idempotent.

Only a fixed source label and a reset time are stored, never the message
(SPEC section 10).
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lib import usage_readings

TABLE = "lane_limits"
DEFAULT_LIMIT_SECONDS = 3600
USAGE_LIMIT_REASON = "usage limit"

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    recipient TEXT PRIMARY KEY,
    harness TEXT NOT NULL,
    bot TEXT NOT NULL,
    limited_at REAL NOT NULL,
    until INTEGER NOT NULL,
    source TEXT NOT NULL,
    job_id TEXT NOT NULL
)
"""

EVIDENCE_TABLE = "lane_limit_evidence"
EVIDENCE_DDL = f"""
CREATE TABLE IF NOT EXISTS {EVIDENCE_TABLE} (
    harness TEXT NOT NULL,
    bot TEXT NOT NULL,
    window TEXT NOT NULL,
    observed_at REAL NOT NULL,
    free_at REAL NOT NULL,
    PRIMARY KEY (harness, bot, window)
)
"""

_PHRASE = re.compile(r"usage limit|limit reached|hit your limit", re.IGNORECASE)
_EPOCH = re.compile(r"\|(\d{10})\b")
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_TRY_AGAIN = re.compile(
    r"try again at (?P<mon>[A-Za-z]{3})[a-z]*\.? (?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,? (?P<year>\d{4}))?"
    r"(?:,? (?:at )?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))? ?(?P<half>[AaPp][Mm]))?",
)
_RESETS = re.compile(
    r"resets (?:at )?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))? ?(?P<half>[AaPp][Mm])"
    r"(?: \((?P<zone>[A-Za-z_]+/[A-Za-z_/]+)\))?",
)


@dataclass(frozen=True)
class UsageLimit:
    resets_at: int | None


@dataclass(frozen=True)
class LaneLimit:
    recipient: str
    harness: str
    bot: str
    limited_at: float
    until: int
    source: str


def detect(harness: str, event: dict, now: float) -> UsageLimit | None:
    """The usage limit one parsed stream event reports, or None."""
    text = _limit_text(harness, event)
    if text is None or not _PHRASE.search(text):
        return None
    return UsageLimit(_parse_reset(text, now))


def _limit_text(harness: str, event: dict) -> str | None:
    kind = event.get("type")
    if harness == "codex":
        if kind == "error":
            message = event.get("message")
        elif kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        else:
            return None
    elif harness == "claude":
        if kind != "result" or not event.get("is_error"):
            return None
        message = event.get("result")
    else:
        return None
    return message if isinstance(message, str) else None


def _parse_reset(text: str, now: float) -> int | None:
    epoch = _EPOCH.search(text)
    if epoch is not None:
        return usage_readings.reset_time(int(epoch.group(1)), now)
    local = datetime.fromtimestamp(now).astimezone()
    match = _TRY_AGAIN.search(text)
    if match is not None:
        month = _MONTHS.get(match["mon"].lower())
        if month is None:
            return None
        year = int(match["year"]) if match["year"] else local.year
        try:
            at = local.replace(
                year=year,
                month=month,
                day=int(match["day"]),
                hour=_hour(match["hour"], match["half"]),
                minute=int(match["minute"] or 0),
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
        if not match["year"] and at.timestamp() <= now:
            at = at.replace(year=year + 1)
        return usage_readings.reset_time(int(at.timestamp()), now)
    match = _RESETS.search(text)
    if match is not None:
        zone = local.tzinfo
        if match["zone"]:
            try:
                zone = ZoneInfo(match["zone"])
            except (ZoneInfoNotFoundError, ValueError):
                return None
        start = datetime.fromtimestamp(now, zone)
        at = start.replace(
            hour=_hour(match["hour"], match["half"]),
            minute=int(match["minute"] or 0),
            second=0,
            microsecond=0,
        )
        if at.timestamp() <= now:
            at += timedelta(days=1)
        return usage_readings.reset_time(int(at.timestamp()), now)
    return None


def _hour(hour: str | None, half: str | None) -> int:
    if hour is None:
        return 0
    value = int(hour) % 12
    return value + 12 if half and half.lower() == "pm" else value


def until(limit: UsageLimit, readings: list[usage_readings.Reading], now: float) -> tuple[int, str]:
    """When the lane may claim again, and the fixed label of where that came from."""
    if limit.resets_at is not None and limit.resets_at > now:
        return limit.resets_at, "message"
    full = [
        r.resets_at
        for r in readings
        if r.used is not None and r.used >= 1.0 and r.resets_at is not None and r.resets_at > now
    ]
    if full:
        return max(full), "reading"
    return int(now) + DEFAULT_LIMIT_SECONDS, "default"


def _valid(found: list[usage_readings.Reading]) -> list[usage_readings.Reading]:
    checked = (usage_readings.from_row(r.window, r.used, r.resets_at, r.observed_at) for r in found)
    return [r for r in checked if r is not None]


def readings(connection: sqlite3.Connection, harness: str, bot: str) -> list[usage_readings.Reading]:
    if not _has_table(connection, usage_readings.TABLE):
        return []
    rows = connection.execute(
        f"""
        SELECT window, used, resets_at, observed_at FROM {usage_readings.TABLE}
        WHERE harness = ? AND bot = ?
        """,
        (harness, bot),
    ).fetchall()
    return _valid([usage_readings.Reading(*row) for row in rows])


def record(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    harness: str,
    bot: str,
    limit: UsageLimit,
    now: float,
) -> LaneLimit | None:
    """Mark job_id's recipient lane limited in the caller's transaction,
    keeping the later `until` while an earlier limit still holds. None when
    the job row is missing."""
    job = connection.execute("SELECT recipient FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return None
    recipient = str(job[0])
    reset, source = until(limit, readings(connection, harness, bot), now)
    held = active(connection, recipient, now)
    if held is not None and held.until > reset:
        reset, source = held.until, held.source
    connection.execute(DDL)
    connection.execute(
        f"""
        INSERT OR REPLACE INTO {TABLE}
            (recipient, harness, bot, limited_at, until, source, job_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (recipient, harness, bot, now, reset, source, job_id),
    )
    return active(connection, recipient, now)


def reevaluate(
    connection: sqlite3.Connection,
    harness: str,
    bot: str,
    incoming: list[usage_readings.Reading],
) -> None:
    """Fold the incoming and stored readings into the account's per-window
    evidence, then move each of its limits that has newer evidence to when
    every window is free, in the caller's transaction."""
    connection.execute(EVIDENCE_DDL)
    connection.executemany(
        f"""
        INSERT INTO {EVIDENCE_TABLE} (harness, bot, window, observed_at, free_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (harness, bot, window) DO UPDATE SET
            observed_at = excluded.observed_at, free_at = excluded.free_at
        WHERE excluded.observed_at > {EVIDENCE_TABLE}.observed_at
        """,
        [
            (harness, bot, r.window, r.observed_at, r.observed_at if r.used < 1.0 else r.resets_at)
            for r in [*_valid(incoming), *readings(connection, harness, bot)]
            if r.used is not None
        ],
    )
    if not _has_table(connection, TABLE):
        return
    evidence = connection.execute(
        f"SELECT observed_at, free_at FROM {EVIDENCE_TABLE} WHERE harness = ? AND bot = ?",
        (harness, bot),
    ).fetchall()
    if not evidence:
        return
    newest = max(observed for observed, _ in evidence)
    free = max(free_at for _, free_at in evidence)
    all_free = free <= newest
    until_sql = "MIN(until, ?)" if all_free else "?"
    connection.execute(
        f"UPDATE {TABLE} SET until = {until_sql} WHERE harness = ? AND bot = ? AND limited_at < ?",
        (math.ceil(free), harness, bot, newest),
    )


def active(connection: sqlite3.Connection, recipient: str, now: float) -> LaneLimit | None:
    """recipient's lane limit while it holds, or None."""
    if not _has_table(connection, TABLE):
        return None
    row = connection.execute(
        f"""
        SELECT recipient, harness, bot, limited_at, until, source FROM {TABLE}
        WHERE recipient = ? AND until > ?
        """,
        (recipient, now),
    ).fetchone()
    if row is None:
        return None
    return LaneLimit(str(row[0]), str(row[1]), str(row[2]), float(row[3]), int(row[4]), str(row[5]))


def all_active(connection: sqlite3.Connection, now: float) -> list[LaneLimit]:
    if not _has_table(connection, TABLE):
        return []
    rows = connection.execute(f"SELECT recipient FROM {TABLE} ORDER BY recipient").fetchall()
    return [limit for limit in (active(connection, str(r[0]), now) for r in rows) if limit]


def paused_through(connection: sqlite3.Connection, now: float) -> dict[str, float]:
    """Time up to each recipient's value is lane-limit waiting, never lag."""
    if not _has_table(connection, TABLE):
        return {}
    rows = connection.execute(f"SELECT recipient, until FROM {TABLE}").fetchall()
    return {str(recipient): min(float(until), now) for recipient, until in rows}


def label(limit: LaneLimit) -> str:
    """`usage limit until <time>`, in the box's local time."""
    at = datetime.fromtimestamp(limit.until).astimezone()
    return f"{USAGE_LIMIT_REASON} until {at.strftime('%Y-%m-%d %H:%M %Z')}"


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )
