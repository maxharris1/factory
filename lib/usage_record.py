"""`orch usage record`: one usage reading for an external account (#772).

No factory process can read Grok's or Cursor's usage, so an operator or a
Grok Bot routine reports it by hand. Only accounts machines.yml declares
with `harness: external` are accepted. The reading is stored in
usage_readings under harness, bot and device `external`, keyed by the
account id, and never replaces a reading with a newer source time.
Only the account id, the window and numbers are written.
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from lib.machines_yml import EXTERNAL as EXTERNAL_HARNESS
from lib.machines_yml import MachinesConfig
from lib.usage_readings import (
    CLOCK_SKEW_SECONDS,
    EXTERNAL,
    EXTERNAL_DEVICE,
    EXTERNAL_JOB,
    RESET_HORIZON_SECONDS,
    TABLE,
    ensure_table,
    reset_time,
    used_fraction,
)

WINDOW_NAMES = {"five_hour": "five_hour", "seven_day": "seven_day", "weekly": "seven_day"}

_UPSERT = f"""
INSERT INTO {TABLE} (harness, bot, device, window, used, resets_at, observed_at, job_id, absent, source_at, account)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
ON CONFLICT (harness, bot, device, window) DO UPDATE SET
    used = excluded.used,
    resets_at = excluded.resets_at,
    observed_at = excluded.observed_at,
    job_id = excluded.job_id,
    absent = 0,
    source_at = excluded.source_at,
    account = excluded.account
WHERE COALESCE(excluded.source_at, excluded.observed_at) > COALESCE({TABLE}.source_at, {TABLE}.observed_at)
"""


class Refused(ValueError):
    """A reading not stored; the message is one line and echoes no refused value."""


@dataclass(frozen=True)
class ExternalReading:
    account: str
    window: str
    used: float
    resets_at: int
    observed_at: float


def parse(
    machines: MachinesConfig,
    *,
    account: str,
    window: str,
    used: str,
    resets_at: str,
    observed_at: str | None,
    now: float,
) -> ExternalReading:
    declared = machines.account(account)
    if declared is None:
        raise Refused("the account is not declared in machines.yml")
    if declared.harness != EXTERNAL_HARNESS:
        raise Refused(f"account {declared.id} is not harness: external in machines.yml")
    stored_window = WINDOW_NAMES.get(window)
    if stored_window is None:
        raise Refused(f"--window must be one of {', '.join(WINDOW_NAMES)}")
    fraction = used_fraction(_float(used))
    if fraction is None:
        raise Refused("--used must be a number from 0 to 1")
    resets = reset_time(_float(resets_at), now)
    if resets is None:
        days = RESET_HORIZON_SECONDS // 86400
        raise Refused(f"--resets-at must be whole epoch seconds within {days} days of now")
    observed = now if observed_at is None else _float(observed_at)
    if observed is None or not math.isfinite(observed) or observed > now + CLOCK_SKEW_SECONDS:
        raise Refused(f"--observed-at must be epoch seconds no more than {CLOCK_SKEW_SECONDS}s ahead of now")
    return ExternalReading(declared.id, stored_window, fraction, resets, observed)


def store(database: Path, reading: ExternalReading) -> bool:
    """Write the reading; False when the stored one is as new or newer."""
    if not database.is_file():
        raise Refused(f"dispatcher database {database} does not exist; run orch init-db first")
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            ensure_table(connection)
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
            if "account" not in columns:
                raise Refused(f"{TABLE} has no account column; run orch usage migrate first")
            written = connection.execute(
                _UPSERT,
                (
                    EXTERNAL,
                    reading.account,
                    EXTERNAL_DEVICE,
                    reading.window,
                    reading.used,
                    reading.resets_at,
                    reading.observed_at,
                    EXTERNAL_JOB,
                    reading.observed_at,
                    reading.account,
                ),
            ).rowcount
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    return written > 0


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None
