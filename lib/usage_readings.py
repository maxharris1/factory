"""Subscription usage readings per bot account (issue #151).

Sources, chosen by Max (PR #234, 2026-11-14):

* Claude: the reply to Claude Code's SDK control request `get_usage`,
  which `orch job run` sends once at each job's start (lib/usage_probe.py)
  with no model turn. `parse_get_usage` keeps `rate_limits.five_hour` and
  `rate_limits.seven_day`: `utilization` (0-100) and `resets_at`. While
  the login cannot report limits (`rate_limits_available` false) there is
  no reading. Each `rate_limit_event` in the job's own stream-json output
  (lib/telemetry.py) is read too: `parse_rate_limit_event` keeps
  `rate_limit_info.unifiedWindows.{five_hour,seven_day}`, with
  `utilization` as a fraction (0-1) and `resetsAt`. A window that event
  leaves out or reports invalid gives nothing, so it never clears a reading.
* Codex: the `rate_limits` of the last `token_count` event in that job's
  own session rollout (lib/codex_rollout.py). `parse_codex` maps each of
  `primary` and `secondary` to a window by `window_minutes` (300, 10080),
  never by position, and keeps `used_percent` and the reset time.
* Gemini: the reply to `agy -p /usage --output-format json`, which `orch
  job run` runs once at a gemini job's start (lib/usage_probe.py) with no
  model turn. `parse_agy_usage` keeps only the `Gemini Models` group: its
  `5h` and `weekly` buckets are `five_hour` and `seven_day`, `used` is
  `1 - remaining_fraction`, and `reset_time` is kept only within
  RESET_HORIZON_SECONDS of now (the unused third-party pool reports
  `1970-01-01T00:00:00Z`).

Only the window, the used fraction and the reset time are kept, as numbers.
A missing or invalid used value stays None, never 0. A source that answers
without a window (a null or missing window, a login without limits, a
`token_count` with no `rate_limits`, or a reset time not within
RESET_HORIZON_SECONDS of now) gives that window an explicit no-reading
(`used` and `resets_at` None), which replaces any older numbers. A Codex
reading is observed at its event's own time, so rereading the same event
never makes it fresher. A window the account's readings, on any of its
devices, leave out of its newest NOT_ON_PLAN_READINGS distinct source
observations, ordered by source time (fresh readings that report other
limits: a window the event leaves out), is `not on plan`: the plan has no
such limit. Only harnesses in PLAN_INFERRED (Codex) are read this way. A
Codex event read again is the same observation, and any reading that
reports the window ends the streak.
`Session._write` (lib/telemetry.py) stores them in the same transaction as
the job's telemetry row, on the existing heartbeat flush. A reading is keyed
by account, device and window in `usage_account_readings`, so two accounts
signed in on one device keep their own, and replaces one only when it
was written later (`updated_at`). `usage_readings`, keyed by harness, bot (the login's
OS user) and device, is the table older binaries read and write: only the
device's own login account's readings go there too. `stored` reads both
tables; a `usage_readings` row wins only when written later. Its
`account` is the subscription the placed device's login uses, and readers
and the not-on-plan streak key on it; a NULL account, written by older
code, is `<harness>-default`.

Nothing here opens a file or runs a process: readings come from parsed
CLI output and orch.db rows only. The one exception is `migrate`, which has
lib/state_backup.py copy the database before any write.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, NamedTuple

from lib import epoch, lane_limit, state_backup
from lib.machines_yml import default_account

if TYPE_CHECKING:
    from pathlib import Path

TABLE = "usage_readings"
WINDOWS = ("five_hour", "seven_day")
SOURCES = {
    "claude": "get_usage",
    "codex": "rollout",
    "gemini": "agy /usage",
    "cursor": "usage record",
    "external": "external",
}
# Checked on Cursor CLI 2026.11.22. The factory never reads a credential
# (docs/ACCOUNTS.md), so no probe reads a harness listed here. Its accounts
# take manual readings from `orch usage record` (lib/usage_record.py), and
# an account with none is `not tracked` for this reason.
NOT_TRACKED: Mapping[str, str] = MappingProxyType({
    "cursor": "the Cursor CLI reports no usage without reading its login token; "
              "record its weekly usage with orch usage record",
})
CODEX_WINDOW_MINUTES = {300: "five_hour", 10080: "seven_day"}
AGY_GROUP = "Gemini Models"
AGY_WINDOWS = {"5h": "five_hour", "weekly": "seven_day"}
AGY_BUCKET_IDS = {"gemini-5h": "five_hour", "gemini-weekly": "seven_day"}
GET_USAGE_ID = "orch-usage"
RESET_HORIZON_SECONDS = 30 * 24 * 3600
CLOCK_SKEW_SECONDS = 300
STALE_SECONDS = 24 * 3600
NOT_ON_PLAN_READINGS = 3
PLAN_INFERRED = frozenset({"codex"})
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
    absent INTEGER NOT NULL DEFAULT 0,
    source_at REAL,
    account TEXT,
    PRIMARY KEY (harness, bot, device, window)
)
"""

DEFAULT_ACCOUNT_SQL = "harness || '-default'"
ACCOUNT_SQL = f"COALESCE(account, {DEFAULT_ACCOUNT_SQL})"
EXTERNAL = "external"
EXTERNAL_DEVICE = "external"
EXTERNAL_JOB = "orch-usage-record"

ACCOUNT_READINGS = "usage_account_readings"
ACCOUNT_READINGS_DDL = f"""
CREATE TABLE IF NOT EXISTS {ACCOUNT_READINGS} (
    account TEXT NOT NULL,
    device TEXT NOT NULL,
    window TEXT NOT NULL,
    harness TEXT NOT NULL,
    bot TEXT NOT NULL,
    used REAL,
    resets_at INTEGER,
    observed_at REAL NOT NULL,
    job_id TEXT NOT NULL,
    absent INTEGER NOT NULL DEFAULT 0,
    source_at REAL,
    PRIMARY KEY (account, device, window)
)
"""

ADDED_COLUMNS = {"absent": "INTEGER NOT NULL DEFAULT 0", "source_at": "REAL"}
BACKUP_LABEL = "usage-accounts"

OBSERVATIONS = "usage_observations"
OBSERVATIONS_DDL = f"""
CREATE TABLE IF NOT EXISTS {OBSERVATIONS} (
    harness TEXT NOT NULL,
    bot TEXT NOT NULL,
    window TEXT NOT NULL,
    source_at REAL NOT NULL,
    absent INTEGER NOT NULL,
    PRIMARY KEY (harness, bot, window, source_at)
)
"""

ACCOUNT_OBSERVATIONS = "usage_account_observations"
ACCOUNT_OBSERVATIONS_DDL = f"""
CREATE TABLE IF NOT EXISTS {ACCOUNT_OBSERVATIONS} (
    account TEXT NOT NULL,
    window TEXT NOT NULL,
    source_at REAL NOT NULL,
    absent INTEGER NOT NULL,
    PRIMARY KEY (account, window, source_at)
)
"""

_UPSERT = f"""
INSERT INTO {TABLE} (harness, bot, device, window, used, resets_at, observed_at, job_id, absent, source_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (harness, bot, device, window) DO UPDATE SET
    used = excluded.used,
    resets_at = excluded.resets_at,
    observed_at = excluded.observed_at,
    job_id = excluded.job_id,
    absent = excluded.absent,
    source_at = excluded.source_at
WHERE COALESCE(excluded.source_at, excluded.observed_at) > COALESCE({TABLE}.source_at, {TABLE}.observed_at)
"""

_ACCOUNT_UPSERT = f"""
INSERT INTO {TABLE} (harness, bot, device, window, used, resets_at, observed_at, job_id, absent, source_at, account)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (harness, bot, device, window) DO UPDATE SET
    used = excluded.used,
    resets_at = excluded.resets_at,
    observed_at = excluded.observed_at,
    job_id = excluded.job_id,
    absent = excluded.absent,
    source_at = excluded.source_at,
    account = excluded.account
WHERE COALESCE(excluded.source_at, excluded.observed_at) > COALESCE({TABLE}.source_at, {TABLE}.observed_at)
"""


def updated_at(row: str = "") -> str:
    """A historical writer's upsert advances observed_at but leaves source_at as it was."""
    prefix = f"{row}." if row else ""
    return f"MAX({prefix}observed_at, COALESCE({prefix}source_at, {prefix}observed_at))"


_READINGS_COLUMNS = "account, device, window, harness, bot, used, resets_at, observed_at, job_id, absent, source_at"
_READINGS_CONFLICT = f"""
ON CONFLICT (account, device, window) DO UPDATE SET
    harness = excluded.harness,
    bot = excluded.bot,
    used = excluded.used,
    resets_at = excluded.resets_at,
    observed_at = excluded.observed_at,
    job_id = excluded.job_id,
    absent = excluded.absent,
    source_at = excluded.source_at
WHERE {updated_at("excluded")} > {updated_at(ACCOUNT_READINGS)}
"""
ACCOUNT_READINGS_UPSERT = f"""
INSERT INTO {ACCOUNT_READINGS} ({_READINGS_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
{_READINGS_CONFLICT}
"""
_SEED_READINGS = f"""
INSERT INTO {ACCOUNT_READINGS} ({_READINGS_COLUMNS})
SELECT {_READINGS_COLUMNS} FROM {TABLE} WHERE true
{_READINGS_CONFLICT}
"""
_MARK_READINGS = f"UPDATE {ACCOUNT_READINGS} SET absent = MAX(?, 1) WHERE account = ? AND window = ? AND absent > 0"


@dataclass(frozen=True)
class _Keyed:
    adopt: str
    retained: str
    observe: str
    newest: str
    prune: str
    mark: str


def _keyed(observations: str, key: str, row_key: str) -> _Keyed:
    marks = ", ".join("?" for _ in key.split(","))
    match = f"({row_key}) = ({marks}) AND window = ?"
    conflict = f"ON CONFLICT ({key}, window, source_at) DO UPDATE SET absent = MIN({observations}.absent, excluded.absent)"
    return _Keyed(
        adopt=f"UPDATE {TABLE} SET source_at = observed_at, absent = 0 WHERE {match} AND observed_at > source_at",
        retained=f"""
            INSERT INTO {observations} ({key}, window, source_at, absent)
            SELECT {row_key}, window, COALESCE(source_at, observed_at), absent > 0 FROM {TABLE}
            WHERE {match}
            {conflict}
        """,
        observe=f"INSERT INTO {observations} ({key}, window, source_at, absent) VALUES ({marks}, ?, ?, ?) {conflict}",
        newest=f"""
            SELECT source_at, absent FROM {observations} WHERE ({key}) = ({marks}) AND window = ?
            ORDER BY source_at DESC LIMIT {NOT_ON_PLAN_READINGS}
        """,
        prune=f"DELETE FROM {observations} WHERE ({key}) = ({marks}) AND window = ? AND source_at < ?",
        mark=f"UPDATE {TABLE} SET absent = MAX(?, 1) WHERE {match} AND absent > 0",
    )


_BY_HARNESS = _keyed(OBSERVATIONS, "harness, bot", "harness, bot")
_BY_ACCOUNT = _keyed(ACCOUNT_OBSERVATIONS, "account", ACCOUNT_SQL)


@dataclass(frozen=True)
class Reading:
    window: str
    used: float | None
    resets_at: int | None
    observed_at: float
    absent: int = 0
    source_at: float | None = None


def missing(window: str, observed_at: float, absent: int = 0, source_at: float | None = None) -> Reading:
    return Reading(window, None, None, observed_at, absent, source_at)


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(DDL)
    connection.execute(OBSERVATIONS_DDL)
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
    for name, kind in ADDED_COLUMNS.items():
        if name in columns:
            continue
        try:
            connection.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {kind}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")}


def tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _migrated(connection: sqlite3.Connection) -> bool:
    successors = {ACCOUNT_READINGS, ACCOUNT_OBSERVATIONS, lane_limit.ACCOUNT_EVIDENCE_TABLE, lane_limit.ACCOUNT_TABLE}
    return (
        "account" in _columns(connection)
        and successors <= tables(connection)
        and connection.execute(f"SELECT 1 FROM {TABLE} WHERE account IS NULL LIMIT 1").fetchone() is None
    )


def migrate(
    db: Path,
    connect: Callable[[], sqlite3.Connection],
    *,
    directory: Path | None = None,
) -> bool:
    """Key db's usage readings on the account; True when it wrote.

    Already migrated (the column, no unnamed row, the four account tables):
    no backup and no write. Otherwise it first takes the BACKUP_LABEL
    labelled backup into directory (default manual/ beside the rolling
    backup); a failed backup raises before any write. Then one transaction
    creates any missing table, adds the column, names every unnamed row
    `<harness>-default`, creates the account tables and seeds them from the
    harness-keyed ones, keeping the newest reading of each account, device
    and window. No table is rebuilt. Rows written meanwhile by older code
    are named by the next run."""
    with closing(connect()) as connection:
        if _migrated(connection):
            return False
    state_backup.labelled_backup(
        db,
        directory or state_backup.default_backup_path().parent / "manual",
        BACKUP_LABEL,
        datetime.now(UTC),
    )
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            ensure_table(connection)
            try:
                connection.execute(f"ALTER TABLE {TABLE} ADD COLUMN account TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
            for ddl in (
                ACCOUNT_READINGS_DDL,
                ACCOUNT_OBSERVATIONS_DDL,
                lane_limit.ACCOUNT_EVIDENCE_DDL,
                lane_limit.ACCOUNT_DDL,
            ):
                connection.execute(ddl)
            connection.execute(f"UPDATE {TABLE} SET account = {DEFAULT_ACCOUNT_SQL} WHERE account IS NULL")
            connection.execute(_SEED_READINGS)
            connection.execute(
                f"""
                INSERT OR IGNORE INTO {ACCOUNT_OBSERVATIONS} (account, window, source_at, absent)
                SELECT {DEFAULT_ACCOUNT_SQL}, window, source_at, absent FROM {OBSERVATIONS}
                """
            )
            if lane_limit.EVIDENCE_TABLE in tables(connection):
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO {lane_limit.ACCOUNT_EVIDENCE_TABLE} (account, window, observed_at, free_at)
                    SELECT {DEFAULT_ACCOUNT_SQL}, window, observed_at, free_at FROM {lane_limit.EVIDENCE_TABLE}
                    """
                )
    return True


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


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
            return [missing(window, now, 0, now) for window in WINDOWS]
        found = []
        for window in WINDOWS:
            info = limits.get(window)
            resets = iso_time(info.get("resets_at")) if isinstance(info, dict) else None
            resets_at = reset_time(int(resets), now) if resets is not None else None
            if resets_at is None:
                found.append(missing(window, now, 0, now))
            else:
                found.append(Reading(window, used_percent(info.get("utilization")), resets_at, now, 0, now))
        return found
    return []


def parse_rate_limit_event(event: object, now: float) -> list[Reading]:
    if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
        return []
    info = event.get("rate_limit_info")
    windows = info.get("unifiedWindows") if isinstance(info, dict) else None
    if not isinstance(windows, dict):
        return []
    found = []
    for window in WINDOWS:
        entry = windows.get(window)
        if not isinstance(entry, dict):
            continue
        used, resets_at = used_fraction(entry.get("utilization")), reset_time(entry.get("resetsAt"), now)
        if used is not None and resets_at is not None:
            found.append(Reading(window, used, resets_at, now))
    return found


def parse_codex(found: object, now: float) -> list[Reading]:
    """The windows in one Codex rollout `token_count` reading.

    `found` is lib/codex_rollout.py's output: `rate_limits` and the event's
    `timestamp`. Every reading is observed at the event's time, never now,
    and an event without a time (or stamped after now plus
    CLOCK_SKEW_SECONDS) gives nothing. A reset is `resets_at` (epoch
    seconds), else the event time plus `resets_in_seconds`. A window the
    event does not report, or reports without a valid reset, is a
    no-reading, counted as absent only when the event reports another
    window; null `rate_limits` makes both windows no-readings."""
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
            missing(window, observed, 0, emitted)
            if resets_at is None
            else Reading(window, used_percent(info.get("used_percent")), resets_at, observed, 0, emitted)
        )
    return [
        readings.get(window) or missing(window, observed, 1 if readings else 0, emitted) for window in WINDOWS
    ]


def parse_agy_usage(payload: object, now: float) -> list[Reading]:
    """The 5-hour and weekly readings in agy's `/usage` reply.

    `payload` is the reply's stdout (bytes, the JSON document after any
    warning lines) or the parsed document. Only the `Gemini Models` group
    is read; each bucket maps to a window by `window`, else by `id`. A
    remaining fraction outside 0-1 leaves `used` None. A bucket whose reset
    time is not an ISO time within RESET_HORIZON_SECONDS of now, and a
    window the group leaves out, is a no-reading. A reply that is not a
    successful `usage` command, or has no `Gemini Models` group, is no
    observation at all."""
    reply = _agy_reply(payload)
    command = reply.get("command") if isinstance(reply, dict) and reply.get("status") == "SUCCESS" else None
    data = command.get("data") if isinstance(command, dict) and command.get("name") == "usage" else None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return []
    group = next((g for g in groups if isinstance(g, dict) and g.get("name") == AGY_GROUP), None)
    buckets = group.get("buckets") if group is not None else None
    if not isinstance(buckets, list):
        return []
    readings: dict[str, Reading] = {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        key, ident = bucket.get("window"), bucket.get("id")
        window = (AGY_WINDOWS.get(key) if isinstance(key, str) else None) or (
            AGY_BUCKET_IDS.get(ident) if isinstance(ident, str) else None
        )
        if window is None or window in readings:
            continue
        resets = iso_time(bucket.get("reset_time"))
        resets_at = None if resets is None else reset_time(int(resets), now)
        if resets_at is None:
            readings[window] = missing(window, now, 0, now)
            continue
        remaining = used_fraction(bucket.get("remaining_fraction"))
        used = None if remaining is None else round(1.0 - remaining, 6)
        readings[window] = Reading(window, used, resets_at, now, 0, now)
    return [readings.get(window) or missing(window, now, 0, now) for window in WINDOWS]


def _agy_reply(payload: object) -> object:
    if not isinstance(payload, (bytes, bytearray)):
        return payload
    text = bytes(payload)
    if len(text) > MAX_REPLY_BYTES:
        return None
    start = 0
    while start < len(text):
        if text[start:].lstrip()[:1] == b"{":
            try:
                return json.loads(text[start:])
            except ValueError:
                pass
        newline = text.find(b"\n", start)
        if newline < 0:
            return None
        start = newline + 1
    return None


def store(
    connection: sqlite3.Connection,
    *,
    harness: str,
    bot: str,
    device: str,
    job_id: str,
    readings: list[Reading],
    account: str | None = None,
    own_login: bool = True,
) -> None:
    """Store readings for account (None: `<harness>-default`). Only an
    own_login account's row goes in TABLE. Before the migration adds the
    account column, the row and its streak keep the harness-and-bot shape
    and a side account's readings are not stored. After it, every account's row goes in ACCOUNT_READINGS once
    `migrate` has created it. This never adds the column or that table."""
    if not readings:
        return
    account = account or default_account(harness)
    if not (own_login or "account" in _columns(connection)):
        return
    ensure_table(connection)
    keyed = "account" in _columns(connection)
    by_account = keyed and ACCOUNT_READINGS in tables(connection)
    if keyed:
        connection.execute(ACCOUNT_OBSERVATIONS_DDL)
    statements, key = (_BY_ACCOUNT, (account,)) if keyed else (_BY_HARNESS, (harness, bot))
    for r in readings:
        source = r.observed_at if r.source_at is None else r.source_at
        absent = 1 if r.absent else 0
        scope = (*key, r.window)
        connection.execute(statements.adopt, scope)
        connection.execute(statements.retained, scope)
        connection.execute(statements.observe, (*scope, source, absent))
        newest = connection.execute(statements.newest, scope).fetchall()
        connection.execute(statements.prune, (*scope, newest[-1][0]))
        values = (r.used, r.resets_at, r.observed_at, job_id, absent, source)
        if not keyed:
            connection.execute(_UPSERT, (harness, bot, device, r.window, *values))
        elif own_login:
            connection.execute(_ACCOUNT_UPSERT, (harness, bot, device, r.window, *values, account))
        if by_account:
            connection.execute(ACCOUNT_READINGS_UPSERT, (account, device, r.window, harness, bot, *values))
        run = _omissions(newest)
        connection.execute(statements.mark, (run, *scope))
        if by_account:
            connection.execute(_MARK_READINGS, (run, account, r.window))
    lane_limit.reevaluate(connection, account, readings)


def _omissions(newest: list[tuple[float, int]]) -> int:
    run, newer = 0, None
    for source_at, absent in newest:
        if not absent or (newer is not None and newer - source_at > STALE_SECONDS):
            break
        run, newer = run + 1, source_at
    return run


def from_row(
    window: object, used: object, resets_at: object, observed_at: object, absent: object = 0
) -> Reading | None:
    """A stored row, re-validated; None when any key value is not what `store` writes."""
    if window not in WINDOWS:
        return None
    observed = epoch.cell(observed_at)
    if observed is None:
        return None
    if resets_at is None:
        count = absent if type(absent) is int and absent >= 0 else 0
        return missing(str(window), observed, count) if used is None else None
    resets = epoch.cell(resets_at)
    if resets is None or resets != int(resets):
        return None
    return Reading(str(window), used_fraction(used), int(resets), observed)


@dataclass(frozen=True)
class StoredRow:
    """One stored reading row, its values as stored and not yet validated."""

    account: object
    device: object
    harness: object
    bot: object
    window: object
    used: object
    resets_at: object
    observed_at: object
    absent: object
    source_at: object

    def reading(self) -> Reading | None:
        return from_row(self.window, self.used, self.resets_at, self.observed_at, self.absent)


def stored(
    connection: sqlite3.Connection, account: str | None = None, limit: int | None = None
) -> list[StoredRow] | None:
    """The stored rows (account's only, when given), one per account, device
    and window, ordered by account, device, harness and bot: the
    ACCOUNT_READINGS row, unless TABLE holds one written later (`updated_at`).
    None when neither table exists."""
    present = tables(connection)
    parts = []
    if ACCOUNT_READINGS in present:
        parts.append(
            "SELECT account, device, harness, bot, window, used, resets_at, observed_at, absent, source_at,"
            f" 0 AS rank FROM {ACCOUNT_READINGS}"
        )
    if TABLE in present:
        columns = _columns(connection)
        named = ACCOUNT_SQL if "account" in columns else DEFAULT_ACCOUNT_SQL
        absent = "absent" if "absent" in columns else "0"
        source = "source_at" if "source_at" in columns else "NULL"
        parts.append(
            f"SELECT {named} AS account, device, harness, bot, window, used, resets_at, observed_at,"
            f" {absent} AS absent, {source} AS source_at, 1 AS rank FROM {TABLE}"
        )
    if not parts:
        return None
    epoch.register(connection)
    observed, source = epoch.sql("observed_at"), epoch.sql("source_at")
    newest = f"MAX({observed}, COALESCE({source}, {observed}))"
    rows = connection.execute(
        f"""
        SELECT account, device, harness, bot, window, used, resets_at, observed_at, absent, source_at FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY account, device, window ORDER BY {newest} DESC, rank
            ) AS n
            FROM ({" UNION ALL ".join(parts)}) WHERE :account IS NULL OR account = :account
        ) WHERE n = 1 ORDER BY account, device, harness, bot LIMIT :limit
        """,
        {"account": account, "limit": -1 if limit is None else limit},
    ).fetchall()
    return [StoredRow(*row) for row in rows]


def window_state(reading: Reading, now: float, harness: str | None = None) -> str:
    """`stale` once the window has reset or the reading is old; else `ok`;
    `not on plan` for a PLAN_INFERRED harness once absent from
    NOT_ON_PLAN_READINGS readings; else `no reading`."""
    if (reading.resets_at is not None and reading.resets_at <= now) or (
        now - reading.observed_at > STALE_SECONDS
    ):
        return "stale"
    if reading.used is not None:
        return "ok"
    if harness in PLAN_INFERRED and reading.resets_at is None and reading.absent >= NOT_ON_PLAN_READINGS:
        return "not on plan"
    return "no reading"


def headroom(readings: list[Reading], now: float) -> float | None:
    """The least `1 - used` over fresh windows, or None when none is fresh."""
    fresh = [1.0 - r.used for r in readings if r.used is not None and window_state(r, now) == "ok"]
    return round(min(fresh), 6) if fresh else None


def newest_by_account(rows: Iterable[StoredRow]) -> dict[str, dict[str, Reading]]:
    """Each account's newest valid reading per window, across its devices."""
    found = [(str(row.account), row.reading()) for row in rows]
    newest: dict[str, dict[str, Reading]] = {}
    for account, reading in sorted(
        ((a, r) for a, r in found if r is not None), key=lambda pair: pair[1].observed_at
    ):
        newest.setdefault(account, {})[reading.window] = reading
    return newest


class Freshness(NamedTuple):
    state: Literal["ok", "stale", "no reading"]
    age: int | None


UNREAD = Freshness("no reading", None)


def freshness_by_pair(rows: Iterable[StoredRow], now: float) -> dict[tuple[str, str], Freshness]:
    found: dict[tuple[str, str], list[Reading]] = {}
    for row in rows:
        reading = row.reading()
        if reading is not None and reading.used is not None and isinstance(row.account, str) \
                and isinstance(row.device, str):
            found.setdefault((row.account, row.device), []).append(reading)
    return {
        pair: Freshness(
            "ok" if any(window_state(r, now) == "ok" for r in readings) else "stale",
            int(max(0.0, now - max(r.observed_at for r in readings))),
        )
        for pair, readings in found.items()
    }
