"""Every account on every device, for the dashboard's Settings page (issue #1361).

Every value `read_accounts` publishes comes from an allowlist. It never
reads a sign-in fingerprint, and the doctor's detail text only yields the
ids of other listed accounts.
"""

from __future__ import annotations

import re
import sqlite3
from typing import get_args

from lib import account_doctor, device_pool, epoch, usage_readings
from lib.machines_yml import EXTERNAL, Account, Device, MachinesConfig
from lib.queue import ACTIONS
from lib.schema import HARNESSES

TEXT_MAX = 100
_TICKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
_STATUSES = frozenset(get_args(account_doctor.Status))
_DOCTORED = frozenset(get_args(account_doctor.Harness))
_SHARED = account_doctor.SHARED_IDENTITY.format("")

_SignInRow = tuple[object, object, object, object, object]


def _accounts(machines: MachinesConfig) -> list[Account]:
    ids: dict[str, None] = {}
    for device in machines.devices:
        for harness in HARNESSES:
            login = device.account_for(harness)
            if login is not None:
                ids.setdefault(login)
    for declared in machines.accounts:
        ids.setdefault(declared.id)
    return [account for account_id in ids if (account := machines.account(account_id)) is not None]


def safe_text(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= TEXT_MAX and value.isprintable() else None


def declared_devices(machines: MachinesConfig, account: Account) -> list[Device]:
    return [device for device in machines.devices if account.id in machines.accounts_on(device.name, account.harness)]


def _sign_ins(connection: sqlite3.Connection) -> dict[tuple[str, str], _SignInRow]:
    try:
        rows = connection.execute(
            "SELECT account, device, status, auth_method, subscription_type, checked_at, detail "
            f"FROM {account_doctor.TABLE}"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {(str(account), str(device)): tuple(rest) for account, device, *rest in rows}


def doctor_statuses(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {pair: row[0] for pair, row in _sign_ins(connection).items() if row[0] in _STATUSES}


def _same_login(detail: object, account_id: str, listed: frozenset[str]) -> list[str]:
    if not isinstance(detail, str):
        return []
    named = {
        other
        for part in detail.split("; ")
        if part.startswith(_SHARED)
        for other in part.removeprefix(_SHARED).split(", ")
    }
    return sorted((named & listed) - {account_id})


def _command(machines: MachinesConfig, device: str, account: Account) -> str | None:
    if account.harness not in _DOCTORED:
        return None
    try:
        home = machines.account_home(device, account.harness, account.id)
    except ValueError:
        return None
    return account_doctor.login_command(account.harness, home)


def _signed_in_on(
    machines: MachinesConfig,
    account: Account,
    sign_ins: dict[tuple[str, str], _SignInRow],
    listed: frozenset[str],
    freshness: dict[tuple[str, str], usage_readings.Freshness],
) -> list[dict]:
    entries = []
    tracked = account.harness not in usage_readings.NOT_TRACKED
    methods = account_doctor.AUTH_METHODS.get(account.harness, frozenset())
    for device in declared_devices(machines, account):
        status, method, subscription, checked_at, detail = sign_ins.get(
            (account.id, device.name), (None, None, None, None, None)
        )
        status = status if status in _STATUSES else None
        reading, age = freshness.get((account.id, device.name), usage_readings.UNREAD) if tracked else (None, None)
        entries.append({
            "device": device.name,
            "status": status,
            "auth_method": method if method in methods else None,
            "subscription_type": subscription
            if isinstance(subscription, str) and account_doctor.SUBSCRIPTION.fullmatch(subscription)
            else None,
            "checked_at": epoch.cell(checked_at),
            "same_login_as": _same_login(detail, account.id, listed),
            "command": None if status == "ok" else _command(machines, device.name, account),
            "login": device.login_for(account.harness),
            "reading": reading,
            "reading_age": age,
        })
    return entries


def _stored(connection: sqlite3.Connection) -> list[usage_readings.StoredRow]:
    try:
        return usage_readings.stored(connection) or []
    except sqlite3.OperationalError:
        return []


def _used(reading: usage_readings.Reading | None, now: float) -> float | None:
    if reading is None or usage_readings.window_state(reading, now) != "ok":
        return None
    return reading.used


def _running(connection: sqlite3.Connection, machines: MachinesConfig) -> dict[str, list[dict]]:
    try:
        rows = connection.execute("SELECT id, ticket_id, action FROM jobs WHERE state = 'running'").fetchall()
    except sqlite3.OperationalError:
        return {}
    jobs = {str(job_id): (ticket, action) for job_id, ticket, action in rows}
    running: dict[str, list[dict]] = {}
    pairs = sorted(device_pool.running_pairs(connection, machines), key=lambda pair: (pair.placed_at, pair.job_id))
    for pair in pairs:
        if pair.job_id not in jobs:
            continue
        ticket, action = jobs[pair.job_id]
        running.setdefault(pair.account, []).append({
            "ticket": ticket if isinstance(ticket, str) and _TICKET.fullmatch(ticket) else None,
            "kind": action if action in ACTIONS else "other",
            "device": pair.device,
        })
    return running


def read_accounts(
    connection: sqlite3.Connection, now: float, machines: MachinesConfig | None
) -> list[dict]:
    if machines is None:
        return []
    accounts = _accounts(machines)
    listed = frozenset(account.id for account in accounts)
    declared = frozenset(account.id for account in machines.accounts)
    sign_ins = _sign_ins(connection)
    stored = _stored(connection)
    windows = usage_readings.newest_by_account(stored)
    freshness = usage_readings.freshness_by_pair(stored, now)
    running = _running(connection, machines)
    entries = []
    for account in accounts:
        readings = windows.get(account.id, {})
        if account.harness == EXTERNAL:
            read_only = "external"
        elif account.id not in declared:
            read_only = "default"
        else:
            read_only = None
        entries.append({
            "id": account.id,
            "harness": account.harness,
            "plan": safe_text(account.plan),
            "not_tracked": None if readings else usage_readings.NOT_TRACKED.get(account.harness),
            "max_jobs": account.max_jobs,
            "paused": account.paused,
            "read_only": read_only,
            "signed_in_on": _signed_in_on(machines, account, sign_ins, listed, freshness),
            **{f"{window}_used": _used(readings.get(window), now) for window in usage_readings.WINDOWS},
            "headroom": usage_readings.headroom(list(readings.values()), now),
            "floor": device_pool.FLOOR,
            "running": running.get(account.id, []),
        })
    return entries
