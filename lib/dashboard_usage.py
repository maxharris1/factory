"""Subscription headroom on the dashboard (issue #151, SPEC 3.5 item 5).

`read_usage` reads `usage_readings` (written by `orch job run`,
lib/usage_readings.py) inside the dashboard's read-only transaction. Only
allowlisted columns are read and every value is checked again. The snapshot
is then walked by lib/dashboard.py's `_scrub`.

There is one entry per bot account: each harness and its bot user on every
enabled machines.yml device, plus any account that has stored readings.
Each window is `ok`, `stale` (the window has reset or the reading is older
than STALE_SECONDS, and `used` is then null) or `no reading`. An account is
`not tracked` (Cursor), `no reading` (no chosen source, or no row yet),
`no telemetry` (no table: an older database, or telemetry never on),
`stale` or `ok`. No value is guessed: a missing utilization stays null.
"""

from __future__ import annotations

import re
import sqlite3

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

Account = tuple[str, str, str]  # (device, harness, bot)


def _accounts(machines: MachinesConfig | None) -> list[Account]:
    found: list[Account] = []
    for device in machines.devices if machines is not None else ():
        if not device.enabled or device.placeholder or not _NAME.fullmatch(device.name):
            continue
        for harness in HARNESSES:
            bot = device.harnesses.get(harness)
            if isinstance(bot, str) and _NAME.fullmatch(bot):
                found.append((device.name, harness, bot))
    return found


def _rows(connection: sqlite3.Connection) -> dict[Account, dict[str, Reading]] | None:
    try:
        rows = connection.execute(
            f"SELECT device, harness, bot, window, used, resets_at, observed_at"
            f" FROM {TABLE} ORDER BY device, harness, bot LIMIT {ROW_LIMIT}"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    readings: dict[Account, dict[str, Reading]] = {}
    for device, harness, bot, *values in rows:
        if harness not in HARNESSES or not all(
            isinstance(name, str) and _NAME.fullmatch(name) for name in (device, bot)
        ):
            continue
        reading = from_row(*values)
        if reading is not None:
            readings.setdefault((device, harness, bot), {})[reading.window] = reading
    return readings


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


def _entry(account: Account, readings: dict[str, Reading] | None, now: float) -> dict:
    device, harness, bot = account
    windows = readings or {}
    observed = max((r.observed_at for r in windows.values()), default=None)
    entry = {
        "bot": bot,
        "harness": harness,
        "device": device,
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
    """The snapshot's `usage`: {state, bots: [...]}, one entry per bot account."""
    stored = _rows(connection)
    accounts = _accounts(machines)
    for account in sorted(stored or {}):
        if account not in accounts:
            accounts.append(account)
    bots = [
        _entry(
            account,
            None if stored is None else stored.get(account, {}),
            now,
        )
        for account in accounts
    ]
    return {"state": "no telemetry" if stored is None else "ok", "bots": bots}
