"""A usage account's limit pauses that account, and its recipient's lane
once every account serving it is paused (#367).

When a run's stream reports the account's usage limit, telemetry records
the account as limited until the reset (lane_account_limits). Placement
(lib/device_pool.py) skips a limited account and places on another account
signed in for the harness, on the same device or another, so the harness
keeps taking jobs while any of its accounts is free. A
recipient's lane is paused (`recipient_paused`) while an older binary's
harness-wide lane_limits row holds, or while every account serving it is
limited; claim_order (lib/queue.py) claims nothing then, on every device,
because every claim reads the box's orch.db. Without the accounts serving
it (no machines.yml), any account limit pauses the lane. T6 requeues the
dead job as itself with nothing charged.

A limit lifts at `until`. Storing readings (usage_readings.store) keeps
the account's evidence per window: the newest reading of that window on any
of its devices, free at its observation when it shows room, else at its own
reset. An older reading never replaces a window's newer evidence, and a
no-reading is no evidence. Once any window's evidence is newer than a limit,
`until` moves to when every window is free, so a healthy reading of one
window never clears another window still full, and one account's reading
never moves another account's limit. An older binary's lane_limits row is
the limit of its harness's default account, and its lane_limit_evidence
is part of that account's evidence, so that account's readings move it the
same way. A later limit replaces the row with its own reset.
Nothing deletes a row, so recording and reading are idempotent.

Only a fixed source label and a reset time are stored, never the message
(SPEC section 10).
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lib import epoch, harnesses, usage_readings

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

ACCOUNT_TABLE = "lane_account_limits"
ACCOUNT_DDL = f"""
CREATE TABLE IF NOT EXISTS {ACCOUNT_TABLE} (
    account TEXT PRIMARY KEY,
    harness TEXT NOT NULL,
    bot TEXT NOT NULL,
    recipient TEXT NOT NULL,
    limited_at REAL NOT NULL,
    until INTEGER NOT NULL,
    source TEXT NOT NULL,
    job_id TEXT NOT NULL
)
"""

ACCOUNT_EVIDENCE_TABLE = "lane_account_evidence"
ACCOUNT_EVIDENCE_DDL = f"""
CREATE TABLE IF NOT EXISTS {ACCOUNT_EVIDENCE_TABLE} (
    account TEXT NOT NULL,
    window TEXT NOT NULL,
    observed_at REAL NOT NULL,
    free_at REAL NOT NULL,
    PRIMARY KEY (account, window)
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
    account: str | None = None


def detect(harness: str, event: dict, now: float) -> UsageLimit | None:
    """The usage limit one parsed stream event reports, or None."""
    text = error_text(harness, event)
    if text is None or not _PHRASE.search(text):
        return None
    return UsageLimit(_parse_reset(text, now))


def error_text(harness: str, event: dict) -> str | None:
    driver = harnesses.find(harness)
    kind = None if driver is None else driver.event_kind(event)
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
    elif harness == "cursor":
        if kind == "result" and event.get("is_error"):
            message = event.get("result")
        elif kind == "error":
            message = event.get("message")
        else:
            return None
    elif harness == "gemini":
        payload = event.get("result")
        if kind != "result" or not isinstance(payload, dict) or payload.get("status") == "SUCCESS":
            return None
        message = payload.get("error")
    else:
        return None
    return message if isinstance(message, str) else None


def _parse_reset(text: str, now: float) -> int | None:
    stamped = _EPOCH.search(text)
    if stamped is not None:
        return usage_readings.reset_time(int(stamped.group(1)), now)
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


def readings(connection: sqlite3.Connection, account: str) -> list[usage_readings.Reading]:
    found = (row.reading() for row in usage_readings.stored(connection, account) or [])
    return [r for r in found if r is not None]


def record(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    harness: str,
    bot: str,
    account: str,
    limit: UsageLimit,
    now: float,
) -> LaneLimit | None:
    """Mark account limited in the caller's transaction, keeping the later
    `until` while an earlier limit still holds. None when the job row is
    missing."""
    job = connection.execute("SELECT recipient FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return None
    reset, source = until(limit, readings(connection, account), now)
    held = account_limits(connection, now).get(account)
    if held is not None and held.until > reset:
        reset, source = held.until, held.source
    connection.execute(ACCOUNT_DDL)
    connection.execute(
        f"""
        INSERT OR REPLACE INTO {ACCOUNT_TABLE}
            (account, harness, bot, recipient, limited_at, until, source, job_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (account, harness, bot, str(job[0]), now, reset, source, job_id),
    )
    return account_limits(connection, now).get(account)


def reevaluate(
    connection: sqlite3.Connection,
    account: str,
    incoming: list[usage_readings.Reading],
) -> None:
    """Fold the incoming and stored readings into the account's per-window
    evidence, in the caller's transaction. A default account's evidence also
    holds an older binary's lane_limit_evidence, the newest of each window
    winning. Each limit older than that evidence moves to when every window
    is free: the account's own, and an older binary's lane_limits row when
    account is its harness's default."""
    connection.execute(ACCOUNT_EVIDENCE_DDL)
    connection.executemany(
        f"""
        INSERT INTO {ACCOUNT_EVIDENCE_TABLE} (account, window, observed_at, free_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (account, window) DO UPDATE SET
            observed_at = excluded.observed_at, free_at = excluded.free_at
        WHERE excluded.observed_at > {ACCOUNT_EVIDENCE_TABLE}.observed_at
        """,
        [
            (account, r.window, r.observed_at, r.observed_at if r.used < 1.0 else r.resets_at)
            for r in [*_valid(incoming), *readings(connection, account)]
            if r.used is not None
        ],
    )
    legacy = (
        f"""UNION ALL SELECT window, observed_at, free_at FROM {EVIDENCE_TABLE}
        WHERE {usage_readings.DEFAULT_ACCOUNT_SQL} = :account"""
        if _has_table(connection, EVIDENCE_TABLE)
        else ""
    )
    evidence: dict[str, tuple[float, float]] = {}
    for window, observed_at, free_at in connection.execute(
        f"""
        SELECT window, observed_at, free_at FROM {ACCOUNT_EVIDENCE_TABLE} WHERE account = :account
        {legacy}
        """,
        {"account": account},
    ):
        evidence[window] = max(evidence.get(window, (observed_at, free_at)), (observed_at, free_at))
    if not evidence:
        return
    newest = max(observed for observed, _ in evidence.values())
    free = max(free_at for _, free_at in evidence.values())
    all_free = free <= newest
    until_sql = "MIN(until, ?)" if all_free else "?"
    for table, owner in ((ACCOUNT_TABLE, "account"), (TABLE, usage_readings.DEFAULT_ACCOUNT_SQL)):
        if _has_table(connection, table):
            connection.execute(
                f"UPDATE {table} SET until = {until_sql} WHERE {owner} = ? AND limited_at < ?",
                (math.ceil(free), account, newest),
            )


def active(connection: sqlite3.Connection, recipient: str, now: float) -> LaneLimit | None:
    """recipient's lane limit while it holds, or None."""
    if not _has_table(connection, TABLE):
        return None
    row = connection.execute(
        f"SELECT recipient, harness, bot, limited_at, until, source, NULL FROM {TABLE} WHERE recipient = ?",
        (recipient,),
    ).fetchone()
    return None if row is None else _holding(row, now)


def all_active(connection: sqlite3.Connection, now: float) -> list[LaneLimit]:
    if not _has_table(connection, TABLE):
        return []
    rows = connection.execute(f"SELECT recipient FROM {TABLE} ORDER BY recipient").fetchall()
    return [limit for limit in (active(connection, str(r[0]), now) for r in rows) if limit]


def account_limits(connection: sqlite3.Connection, now: float) -> dict[str, LaneLimit]:
    """Each account's lane limit while it holds, keyed by account id."""
    if not _has_table(connection, ACCOUNT_TABLE):
        return {}
    rows = connection.execute(
        f"SELECT recipient, harness, bot, limited_at, until, source, account FROM {ACCOUNT_TABLE} ORDER BY account"
    )
    return {limit.account: limit for limit in (_holding(row, now) for row in rows) if limit is not None}


def _holding(row: tuple, now: float) -> LaneLimit | None:
    until = epoch.read(row[4])
    if until <= now:
        return None
    return LaneLimit(
        str(row[0]), str(row[1]), str(row[2]), epoch.read(row[3]), int(until), str(row[5]),
        None if row[6] is None else str(row[6]),
    )


def recipient_paused(
    connection: sqlite3.Connection, recipient: str, now: float, serving: frozenset[str] | None
) -> LaneLimit | None:
    """The limit that pauses recipient's whole lane now, or None.

    An older binary's harness-wide row pauses it. So does a limit on every
    account in `serving` (the accounts on devices that take new work), until
    the first of them lifts. `serving` None means they are not known (no
    machines.yml): then any account limit for the recipient pauses its lane
    until the last one lifts. An empty `serving` pauses nothing by account."""
    legacy = active(connection, recipient, now)
    if legacy is not None:
        return legacy
    limits = account_limits(connection, now)
    if serving is None:
        held = [limit for limit in limits.values() if limit.recipient == recipient]
        return max(held, key=lambda limit: limit.until, default=None)
    if serving and serving <= limits.keys():
        return min((limits[account] for account in serving), key=lambda limit: limit.until)
    return None


def paused_through(
    connection: sqlite3.Connection, now: float, serving: Mapping[str, frozenset[str]]
) -> dict[str, float]:
    """Time up to each recipient's value is lane-limit waiting, never lag:
    the end, at most now, of its latest whole-lane pause, as
    `recipient_paused` decides it. A recipient missing from `serving` has
    its serving accounts unknown."""
    through: dict[str, float] = {}
    if _has_table(connection, TABLE):
        for recipient, until in connection.execute(f"SELECT recipient, until FROM {TABLE}"):
            through[str(recipient)] = min(float(until), now)
    if not _has_table(connection, ACCOUNT_TABLE):
        return through
    spans = {
        str(account): (str(recipient), float(limited_at), float(until))
        for account, recipient, limited_at, until in connection.execute(
            f"SELECT account, recipient, limited_at, until FROM {ACCOUNT_TABLE}"
        )
    }
    for recipient in {span[0] for span in spans.values()} | serving.keys():
        accounts = serving.get(recipient)
        if accounts is None:
            ends = [until for owner, _, until in spans.values() if owner == recipient]
        elif accounts and accounts <= spans.keys():
            start = max(spans[account][1] for account in accounts)
            end = min(spans[account][2] for account in accounts)
            ends = [end] if start < end else []
        else:
            ends = []
        if ends:
            through[recipient] = max(through.get(recipient, 0.0), min(max(ends), now))
    return through


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
