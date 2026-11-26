"""`orch usage record`: one usage reading for an account no probe reads.

No factory process can read Grok's or Cursor's usage, so an operator or a
Grok Bot routine reports it by hand. Only accounts machines.yml declares
with `harness: external`, or with a harness that has no automatic source
(NOT_TRACKED: cursor), are accepted; an implicit `<harness>-default` account
that machines.yml does not list is refused; Claude, Codex and Gemini are read at
each job's start and are refused. The reading is stored in usage_readings
under the account's own harness (`external` or `cursor`), bot the account
id and the reserved device `external`, keyed by the account id, and in
usage_account_readings once `orch usage migrate` has created it. It never
replaces a reading with a newer source time.
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
    ACCOUNT_READINGS,
    ACCOUNT_READINGS_UPSERT,
    CLOCK_SKEW_SECONDS,
    EXTERNAL_DEVICE,
    EXTERNAL_JOB,
    NOT_TRACKED,
    RESET_HORIZON_SECONDS,
    TABLE,
    ensure_table,
    reset_time,
    tables,
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
    harness: str
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
    declared = next((item for item in machines.accounts if item.id == account), None)
    if declared is None:
        raise Refused("the account is not declared in machines.yml")
    if declared.harness != EXTERNAL_HARNESS and declared.harness not in NOT_TRACKED:
        raise Refused(
            f"account {declared.id} is harness {declared.harness}, whose usage is read automatically;"
            f" only harness: external or {', '.join(sorted(NOT_TRACKED))} accounts are recorded"
        )
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
    return ExternalReading(declared.id, declared.harness, stored_window, fraction, resets, observed)


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
                    reading.harness,
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
            ).rowcount > 0
            if ACCOUNT_READINGS in tables(connection):
                values = (reading.used, reading.resets_at, reading.observed_at, EXTERNAL_JOB, 0, reading.observed_at)
                keyed = (reading.account, EXTERNAL_DEVICE, reading.window, reading.harness, reading.account, *values)
                written = connection.execute(ACCOUNT_READINGS_UPSERT, keyed).rowcount > 0 and written
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    return written


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None
