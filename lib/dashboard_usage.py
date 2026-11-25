"""Subscription headroom on the dashboard (issue #151, SPEC 3.5 item 5).

`read_usage` reads the stored readings (written by `orch job run` and `orch
usage record`) inside the dashboard's read-only transaction. Only
allowlisted columns are read and every value is checked again. The
snapshot is then walked by lib/dashboard.py's `_scrub`.

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
harness. No value is guessed: a missing utilization stays null. Each entry
also carries what placement reads (lib/device_pool.py account_loads): its
`running` jobs, `max_jobs`, the devices it is `signed_in` on and those it is
`running_on` now.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from lib import device_pool, lane_limit
from lib.machines_yml import EXTERNAL, MachinesConfig
from lib.usage_readings import (
    NOT_TRACKED,
    NOT_TRACKED_REASONS,
    SOURCES,
    WINDOWS,
    Reading,
    headroom,
    stored,
    window_state,
)

HARNESSES = ("claude", "codex", "cursor", "gemini")
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
        rows = stored(connection, limit=ROW_LIMIT)
    except sqlite3.OperationalError:
        return None
    if rows is None:
        return None
    found: dict[str, _Account] = {}
    for row in rows:
        if row.harness not in ROW_HARNESSES or not _names(row.account, row.device, row.bot):
            continue
        reading = row.reading()
        if reading is None:
            continue
        account = found.setdefault(str(row.account), _Account(str(row.harness)))
        if row.harness != EXTERNAL:
            account.used_by(str(row.device), str(row.bot))
        kept = account.windows.get(reading.window)
        if kept is None or reading.observed_at > kept.observed_at:
            account.windows[reading.window] = reading
    return found


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
    loads: device_pool.AccountLoads,
    signed_in: dict[str, list[str]],
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
        "running": loads.running.get(account_id, 0),
        "max_jobs": None if declared is None else declared.max_jobs,
        "signed_in": signed_in.get(account_id, []),
        "running_on": [device for device in loads.running_on.get(account_id, ()) if _names(device)],
    }
    states = {entry[window]["state"] for window in WINDOWS}
    tracked = machines is not None and any(item.harness == harness for item in machines.accounts)
    if harness == "gemini" and not windows:
        state = "not tracked"
        entry["reason"] = NOT_TRACKED_REASONS["gemini"]
    elif harness in NOT_TRACKED and not tracked:
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
    loads = device_pool.account_loads(connection, machines, now)
    signed_in = {} if machines is None else loads.signed_in_devices(machines)
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
                loads,
                signed_in,
            ),
            limits.get(account_id) or legacy.get(account.harness),
        )
        for account_id, account in accounts.items()
    ]
    entries = _with_balance(entries, loads)
    return {"state": "no telemetry" if stored is None else "ok", "accounts": entries}


def _with_balance(entries: list[dict], loads: device_pool.AccountLoads) -> list[dict]:
    """Attach intra-harness rolling placement counts, effective headroom score,
    placement share, and skew indicator to each account entry (E2-5)."""
    by_harness: dict[str, list[dict]] = {}
    for entry in entries:
        by_harness.setdefault(str(entry["harness"]), []).append(entry)
    out: list[dict] = []
    for entry in entries:
        account_id = str(entry["account"])
        peers = by_harness.get(str(entry["harness"]), [])
        count = loads.recent_placements.get(account_id, 0)
        total = sum(loads.recent_placements.get(str(peer["account"]), 0) for peer in peers)
        share = round(count / total, 4) if total > 0 else None
        if len(peers) < 2 or total < device_pool.SKEW_MIN_PLACEMENTS or share is None:
            skew = "none"
        else:
            even = 1.0 / len(peers)
            if share - even > device_pool.SKEW_THRESHOLD:
                skew = "high"
            elif even - share > device_pool.SKEW_THRESHOLD:
                skew = "low"
            else:
                skew = "balanced"
        out.append(
            {
                **entry,
                "recent_placements": count,
                "effective_score": loads.score_of(account_id),
                "placement_share": share,
                "skew": skew,
            }
        )
    return out


def _limited(entry: dict, limit: lane_limit.LaneLimit | None) -> dict:
    return {
        **entry,
        "limited_until": None if limit is None else limit.until,
        "limit_label": None if limit is None else lane_limit.label(limit),
    }
