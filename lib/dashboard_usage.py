"""Subscription headroom on the dashboard (issue #151, SPEC 3.5 item 5).

`read_usage` reads `usage_readings` (written by `orch job run`,
lib/usage_readings.py) inside the dashboard's read-only transaction. Only
allowlisted columns are read and every value is checked again. The snapshot
is then walked by lib/dashboard.py's `_scrub`.

There is one entry per account, a harness and its bot user. Accounts come
from the enabled machines.yml devices (`devices` keeps their machines.yml
order), then any account that has stored readings with the devices its rows
name. Each window holds the newest reading across the account's devices and
is `ok`, `stale` (the window has reset or the reading is older than
STALE_SECONDS, and `used` is then null) or `no reading`. An account is `not tracked` (Cursor), `no reading` (no chosen
source, or no row yet), `no telemetry` (no table: an older database, or
telemetry never on), `stale` or `ok`. No value is guessed: a missing
utilization stays null.
"""

from __future__ import annotations

import re
import sqlite3
from typing import NamedTuple

from lib import lane_limit
from lib.machines_yml import MachinesConfig
from lib.usage_readings import (
    NOT_TRACKED,
    SOURCES,
    TABLE,
    WINDOWS,
    Reading,
    from_row,
    headroom,
    window_state,
)

HARNESSES = ("claude", "codex", "cursor")
ROW_LIMIT = 500
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")

class Account(NamedTuple):
    harness: str
    bot: str


def _accounts(machines: MachinesConfig | None) -> dict[Account, list[str]]:
    found: dict[Account, list[str]] = {}
    for device in machines.devices if machines is not None else ():
        if not device.enabled or device.placeholder or not _NAME.fullmatch(device.name):
            continue
        for harness in HARNESSES:
            bot = device.harnesses.get(harness)
            if isinstance(bot, str) and _NAME.fullmatch(bot):
                found.setdefault(Account(harness, bot), []).append(device.name)
    return found


def _rows(connection: sqlite3.Connection) -> dict[Account, tuple[list[str], dict[str, Reading]]] | None:
    try:
        rows = connection.execute(
            f"SELECT device, harness, bot, window, used, resets_at, observed_at"
            f" FROM {TABLE} ORDER BY device, harness, bot LIMIT {ROW_LIMIT}"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    stored: dict[Account, tuple[list[str], dict[str, Reading]]] = {}
    for device, harness, bot, *values in rows:
        if harness not in HARNESSES or not all(
            isinstance(name, str) and _NAME.fullmatch(name) for name in (device, bot)
        ):
            continue
        reading = from_row(*values)
        if reading is None:
            continue
        devices, windows = stored.setdefault(Account(harness, bot), ([], {}))
        if device not in devices:
            devices.append(device)
        kept = windows.get(reading.window)
        if kept is None or reading.observed_at > kept.observed_at:
            windows[reading.window] = reading
    return stored


def _window(reading: Reading | None, now: float) -> dict:
    if reading is None:
        return {"used": None, "resets_at": None, "observed_at": None, "state": "no reading"}
    state = window_state(reading, now)
    return {
        "used": reading.used if state == "ok" else None,
        "resets_at": reading.resets_at,
        "observed_at": reading.observed_at,
        "state": state,
    }


def _entry(
    account: Account, devices: list[str], readings: dict[str, Reading] | None, now: float
) -> dict:
    harness, bot = account
    windows = readings or {}
    observed = max((r.observed_at for r in windows.values()), default=None)
    entry = {
        "bot": bot,
        "harness": harness,
        "devices": devices,
        **{window: _window(windows.get(window), now) for window in WINDOWS},
        "headroom": headroom(list(windows.values()), now),
        "observed_at": observed,
        "age_seconds": None if observed is None else int(max(0.0, now - observed)),
    }
    states = {entry[window]["state"] for window in WINDOWS}
    if harness in NOT_TRACKED:
        state = "not tracked"
    elif harness not in SOURCES:
        state = "no reading"
    elif readings is None:
        state = "no telemetry"
    elif "ok" in states:
        state = "ok"
    elif "stale" in states:
        state = "stale"
    else:
        state = "no reading"
    return {**entry, "state": state}


def read_usage(
    connection: sqlite3.Connection, now: float, machines: MachinesConfig | None
) -> dict:
    """The snapshot's `usage`: {state, accounts: [...]}, one entry per account."""
    stored = _rows(connection)
    accounts = _accounts(machines)
    for account in sorted(stored or {}):
        accounts.setdefault(account, stored[account][0])
    limits = {(limit.harness, limit.bot): limit for limit in lane_limit.all_active(connection, now)}
    entries = [
        _limited(
            _entry(
                account,
                devices,
                None if stored is None else stored.get(account, ([], {}))[1],
                now,
            ),
            limits.get(account),
        )
        for account, devices in accounts.items()
    ]
    return {"state": "no telemetry" if stored is None else "ok", "accounts": entries}


def _limited(entry: dict, limit: lane_limit.LaneLimit | None) -> dict:
    """#367: the account's lane pause, as its reset time and display label."""
    return {
        **entry,
        "limited_until": None if limit is None else limit.until,
        "limit_label": None if limit is None else lane_limit.label(limit),
    }
