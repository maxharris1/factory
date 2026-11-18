"""Subscription headroom on the dashboard (issue #151, SPEC 3.5 item 5).

`read_usage` reads `usage_readings` (written by `orch job run` and `orch
usage record`, lib/usage_readings.py) inside the dashboard's read-only
transaction. Only allowlisted columns are read and every value is checked
again. The snapshot is then walked by lib/dashboard.py's `_scrub`.

There is one entry per account: a subscription, named in machines.yml
(lib/machines_yml.py). A row with no account belongs to its harness's
`<harness>-default` account. Entries come from the enabled machines.yml
devices in machines.yml order (`devices` and `bots` name the devices that
log in to the account and their bot users), then declared accounts no
device uses (external ones included), then any account that only stored
readings name, with the devices its rows name. An external account has no
devices or bots: its rows' reserved device and bot are not shown. Each
window holds the newest reading across the account's rows and is `ok`,
`stale` (the window has reset or the reading is older than STALE_SECONDS,
and `used` is then null), `not on plan` (Codex only: left out of
NOT_ON_PLAN_READINGS fresh readings in a row that report other limits) or
`no reading`. An account is `not tracked` (Cursor, while machines.yml
declares no Cursor account), `no reading` (no row yet), `no telemetry` (no
table: an older database, or telemetry never on), `stale` or `ok`. Its
limit is its own lane pause, else an older recipient-wide pause on its
harness. No value is guessed: a missing utilization stays null.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from lib import lane_limit
from lib.machines_yml import EXTERNAL, MachinesConfig
from lib.usage_readings import (
    ACCOUNT_SQL,
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
ROW_HARNESSES = (*HARNESSES, EXTERNAL)
ROW_LIMIT = 500
TEXT_MAX = 100
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass
class _Account:
    harness: str
    devices: list[str] = field(default_factory=list)
    bots: list[str] = field(default_factory=list)
    windows: dict[str, Reading] = field(default_factory=dict)

    def used_by(self, device: str, bot: str) -> None:
        if device not in self.devices:
            self.devices.append(device)
        if bot not in self.bots:
            self.bots.append(bot)


def _names(*values: object) -> bool:
    return all(isinstance(value, str) and _NAME.fullmatch(value) for value in values)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= TEXT_MAX and value.isprintable() else None


def _configured(machines: MachinesConfig | None) -> dict[str, _Account]:
    found: dict[str, _Account] = {}
    if machines is None:
        return found
    for device in machines.devices:
        if not device.enabled or device.placeholder:
            continue
        for harness in HARNESSES:
            account, bot = device.account_for(harness), device.harnesses.get(harness)
            if _names(device.name, account, bot):
                found.setdefault(account, _Account(harness)).used_by(device.name, bot)
    for declared in machines.accounts:
        if _names(declared.id):
            found.setdefault(declared.id, _Account(declared.harness))
    return found


def _rows(connection: sqlite3.Connection) -> dict[str, _Account] | None:
    try:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
        account = ACCOUNT_SQL if "account" in columns else "harness || '-default'"
        absent = "absent" if "absent" in columns else "0"
        rows = connection.execute(
            f"SELECT {account}, device, harness, bot, window, used, resets_at, observed_at, {absent}"
            f" FROM {TABLE} ORDER BY 1, device, harness, bot LIMIT {ROW_LIMIT}"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    stored: dict[str, _Account] = {}
    for account_id, device, harness, bot, *values in rows:
        if harness not in ROW_HARNESSES or not _names(account_id, device, bot):
            continue
        reading = from_row(*values)
        if reading is None:
            continue
        account = stored.setdefault(account_id, _Account(harness))
        if harness != EXTERNAL:
            account.used_by(device, bot)
        kept = account.windows.get(reading.window)
        if kept is None or reading.observed_at > kept.observed_at:
            account.windows[reading.window] = reading
    return stored


def _window(reading: Reading | None, now: float, harness: str) -> dict:
    if reading is None:
        return {"used": None, "resets_at": None, "observed_at": None, "state": "no reading"}
    state = window_state(reading, now, harness)
    return {
        "used": reading.used if state == "ok" else None,
        "resets_at": reading.resets_at,
        "observed_at": reading.observed_at,
        "state": state,
    }


def _entry(
    account_id: str,
    account: _Account,
    readings: dict[str, Reading] | None,
    now: float,
    machines: MachinesConfig | None,
) -> dict:
    harness = account.harness
    windows = readings or {}
    observed = max((r.observed_at for r in windows.values()), default=None)
    declared = None if machines is None else machines.account(account_id)
    entry = {
        "account": account_id,
        "plan": None if declared is None else _text(declared.plan),
        "label": None if declared is None else _text(declared.label),
        "source": SOURCES.get(harness),
        "bot": account.bots[0] if account.bots else None,
        "bots": account.bots,
        "harness": harness,
        "devices": account.devices,
        **{window: _window(windows.get(window), now, harness) for window in WINDOWS},
        "headroom": headroom(list(windows.values()), now),
        "observed_at": observed,
        "age_seconds": None if observed is None else int(max(0.0, now - observed)),
    }
    states = {entry[window]["state"] for window in WINDOWS}
    tracked = machines is not None and any(item.harness == harness for item in machines.accounts)
    if harness in NOT_TRACKED and not tracked:
        state = "not tracked"
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
    accounts = _configured(machines)
    for account_id in sorted(stored or {}):
        accounts.setdefault(account_id, stored[account_id])
    limits = lane_limit.account_limits(connection, now)
    legacy: dict[str, lane_limit.LaneLimit] = {}
    for limit in lane_limit.all_active(connection, now):
        legacy.setdefault(limit.harness, limit)
    entries = [
        _limited(
            _entry(
                account_id,
                account,
                None if stored is None else stored.get(account_id, _Account(account.harness)).windows,
                now,
                machines,
            ),
            limits.get(account_id) or legacy.get(account.harness),
        )
        for account_id, account in accounts.items()
    ]
    return {"state": "no telemetry" if stored is None else "ok", "accounts": entries}


def _limited(entry: dict, limit: lane_limit.LaneLimit | None) -> dict:
    return {
        **entry,
        "limited_until": None if limit is None else limit.until,
        "limit_label": None if limit is None else lane_limit.label(limit),
    }
