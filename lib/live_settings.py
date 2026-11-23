"""The device settings every process places by (#852).

One row in orch.db's `device_live` holds what placement may use now for each
device's `enabled`, `drain` and `slot_max`. Placement reads them from the
row, never from a process's own copy of machines.yml, so no process mixes
fields from two versions of the file.

The row keeps the switches the poller's DeviceMonitor adopted and, for each
machines.yml any process read (keyed by its absolute path), that file's
switches as last read. Placement uses their meet, `Live.switches`: a device
is enabled only if the adopted switches and every file say so, drained if
any does, and its slot_max is the lowest. A device the poller has not
adopted is closed, so nothing is placed before the first adopt.

Each declared account's `paused` and `max_jobs` ride the same row, kept in
the sibling `account_live` table and written in the same transaction, with
the same meet over every account any side names: paused if any side says so,
and the lowest max_jobs. A side that does not name an account leaves an
implicit `<harness>-default` one open and closes any other. An entry written
before account switches existed has no say on accounts.

* `recorded` runs inside every claim's BEGIN IMMEDIATE. When the caller's
  file changed since its entry was written, it replaces that entry. A
  narrowing in any file reaches placement at once; an entry that widens lets
  placement widen only as far as the adopted switches and every other file
  allow.
* `adopted` is called by the poller only, inside its own BEGIN IMMEDIATE. It
  sets the adopted switches and the poller's own entry to the file, drops
  every other file's entry but the pinned one, and returns the parse it
  wrote, which the monitor installs as its view before it next reconciles.
  It is the only way a widening reaches the adopted switches, so a claim can
  land on a device only once the poller already treats that device as
  enabled.
* `requested` is the settings applier's `recorded`. It also pins its file's
  entry and unpins every other, so the file of the latest Settings write
  stays in the meet.

`written` counts the file versions the row has seen and `adopted` the last
of them the poller acted on, with `adopted_digest` the digest of the
switches it adopted. The settings applier records `written` right after its
write as the change's `version`; the change is applied once `adopted`
reaches it, the adopted switches hold the requested value and every waiter
it names has recorded `seen` at that version, which a claim does only when
placement uses exactly the adopted switches (`Live.places_by_adopted`).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from lib.config_overlay import overlay_path
from lib.machines_yml import Account, MachinesConfig, default_account, load_machines_yml
from lib.schema import HARNESSES

TABLE = "device_live"
SEEN_TABLE = "device_live_seen"
ACCOUNT_TABLE = "account_live"


@dataclass(frozen=True)
class Switch:
    enabled: bool
    drain: bool
    slot_max: int | None

    def meet(self, other: Switch) -> Switch:
        limits = (self.slot_max, other.slot_max)
        return Switch(
            self.enabled and other.enabled,
            self.drain or other.drain,
            None if None in limits else min(limits),
        )


CLOSED = Switch(enabled=False, drain=True, slot_max=0)
_FIELDS = tuple(item.name for item in fields(Switch))


@dataclass(frozen=True)
class AccountSwitch:
    paused: bool
    max_jobs: int | None

    def meet(self, other: AccountSwitch) -> AccountSwitch:
        caps = [cap for cap in (self.max_jobs, other.max_jobs) if cap is not None]
        return AccountSwitch(self.paused or other.paused, min(caps) if caps else None)


_ACCOUNT_FIELDS = tuple(item.name for item in fields(AccountSwitch))
ACCOUNT_CLOSED = AccountSwitch(paused=True, max_jobs=0)
ACCOUNT_OPEN = AccountSwitch(paused=False, max_jobs=None)
_IMPLICIT = frozenset(default_account(harness) for harness in HARNESSES)


def _absent(account_id: str) -> AccountSwitch:
    return ACCOUNT_OPEN if account_id in _IMPLICIT else ACCOUNT_CLOSED


def _declared(accounts: Mapping[str, AccountSwitch]) -> dict[str, AccountSwitch]:
    return {name: switch for name, switch in accounts.items()
            if not (name in _IMPLICIT and switch == ACCOUNT_OPEN)}


@dataclass(frozen=True)
class Live:
    switches: Mapping[str, Switch]
    written: int
    adopted: int
    adopted_digest: str = ""
    seen: Mapping[str, int] = field(default_factory=dict)
    adopted_switches: Mapping[str, Switch] = field(default_factory=dict)
    accounts: Mapping[str, AccountSwitch] = field(default_factory=dict)
    adopted_accounts: Mapping[str, AccountSwitch] = field(default_factory=dict)

    def placement_config(self, config: MachinesConfig) -> MachinesConfig:
        devices = []
        for device in config.devices:
            switch = self.switches.get(device.name, CLOSED)
            devices.append(replace(device, enabled=switch.enabled, drain=switch.drain,
                                   slot_max=switch.slot_max))
        accounts = []
        for account in config.accounts:
            switch = self.accounts.get(account.id, _absent(account.id))
            accounts.append(replace(account, paused=switch.paused, max_jobs=switch.max_jobs))
        declared = {account.id for account in config.accounts}
        for name, switch in self.accounts.items():
            if name in _IMPLICIT and name not in declared:
                accounts.append(Account(name, name.removesuffix("-default"),
                                        paused=switch.paused, max_jobs=switch.max_jobs))
        return replace(config, devices=tuple(devices), accounts=tuple(accounts))

    def is_applied(self, version: int) -> bool:
        return self.adopted >= version

    def adopted_holds(self, device: str, name: str, value: object) -> bool:
        switch = self.adopted_switches.get(device)
        return switch is not None and name in _FIELDS and getattr(switch, name) == value

    def adopted_account_holds(self, account: str, name: str, value: object) -> bool:
        switch = self.adopted_accounts.get(account)
        return name not in _ACCOUNT_FIELDS or (switch is not None and getattr(switch, name) == value)

    def places_by_adopted(self) -> bool:
        return self.adopted > 0 and _digest(self.switches, self.accounts) == self.adopted_digest


@dataclass(frozen=True)
class _Source:
    key: str
    switches: Mapping[str, Switch]
    pinned: bool
    accounts: Mapping[str, AccountSwitch] | None

    @property
    def has_accounts(self) -> bool:
        return self.accounts is not None


@dataclass(frozen=True)
class _Row:
    adopted_switches: Mapping[str, Switch]
    adopted_accounts: Mapping[str, AccountSwitch] | None
    written: int
    adopted: int
    adopted_digest: str
    sources: Mapping[str, _Source]

    def live(self) -> Live:
        switches = dict(self.adopted_switches)
        for source in self.sources.values():
            switches = {
                name: switch.meet(source.switches.get(name, CLOSED))
                for name, switch in switches.items()
            }
        sides = [self.adopted_accounts or {},
                 *(source.accounts for source in self.sources.values()
                   if source.accounts is not None)]
        accounts = {}
        for name in sorted(set().union(*sides)):
            switch = ACCOUNT_OPEN
            for side in sides:
                switch = switch.meet(side.get(name, _absent(name)))
            accounts[name] = switch
        accounts = _declared(accounts)
        return Live(switches, self.written, self.adopted, self.adopted_digest,
                    adopted_switches=self.adopted_switches, accounts=accounts,
                    adopted_accounts=self.adopted_accounts or {})


EMPTY = _Row({}, {}, written=0, adopted=0, adopted_digest="", sources={})


def file_key(machines: Path) -> str:
    """Changes whenever machines.yml or its machines.local.yml overlay does."""
    return json.dumps([_stat(machines), _stat(overlay_path(machines))])


def recorded(connection: sqlite3.Connection, machines: Path) -> Live:
    """The caller holds BEGIN IMMEDIATE. An invalid file writes nothing and
    leaves the row as it was."""
    return _record(connection, machines, pin=False)


def requested(connection: sqlite3.Connection, machines: Path) -> Live:
    """Settings applier only; `recorded`, and the file's entry is pinned."""
    return _record(connection, machines, pin=True)


def _record(connection: sqlite3.Connection, machines: Path, *, pin: bool) -> Live:
    _create(connection)
    path = os.path.abspath(machines)
    key = file_key(machines)
    row = _read(connection) or EMPTY
    source = row.sources.get(path)
    pinned = pin or (source is not None and source.pinned)
    if (source is not None and source.key == key and source.pinned == pinned
            and source.has_accounts):
        return row.live()
    config = _load(machines)
    if config is None:
        return row.live()
    sources = {name: replace(item, pinned=item.pinned and not pin)
               for name, item in row.sources.items()}
    row = replace(row, written=row.written + 1,
                  sources={**sources, path: _Source(key, _switches(config), pinned,
                                                    _account_switches(config))})
    _write(connection, row)
    return row.live()


def adopted(connection: sqlite3.Connection, machines: Path) -> tuple[Live, MachinesConfig]:
    """Poller only; the caller holds BEGIN IMMEDIATE. An invalid file raises
    ValueError and leaves the row as it was."""
    _create(connection)
    path = os.path.abspath(machines)
    key = file_key(machines)
    try:
        config, findings = load_machines_yml(machines)
    except OSError as exc:
        raise ValueError(f"{machines}: cannot read: {type(exc).__name__}") from exc
    errors = [item for item in findings if item.severity == "error"]
    if config is None or errors:
        detail = errors[0].message if errors else "invalid machines.yml"
        raise ValueError(f"{machines}: {detail}")
    found = _read(connection) or EMPTY
    prior = found.sources.get(path)
    written = found.written + (prior is None or prior.key != key)
    switches = _switches(config)
    accounts = _account_switches(config)
    kept = {name: item for name, item in found.sources.items() if item.pinned}
    own = _Source(key, switches, prior is not None and prior.pinned, accounts)
    row = _Row(switches, accounts, written=written, adopted=written,
               adopted_digest=_digest(switches, accounts), sources={**kept, path: own})
    _write(connection, row)
    return row.live(), config


def record_seen(connection: sqlite3.Connection, harness: str, adopted: int) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SEEN_TABLE} (
            harness TEXT PRIMARY KEY,
            adopted INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        INSERT INTO {SEEN_TABLE} (harness, adopted) VALUES (?, ?)
        ON CONFLICT (harness) DO UPDATE SET adopted = excluded.adopted
        WHERE {SEEN_TABLE}.adopted != excluded.adopted
        """,
        (harness, adopted),
    )


def peek(connection: sqlite3.Connection) -> Live | None:
    """The row, read only; None before any process has written it."""
    try:
        found = _read(connection)
    except sqlite3.OperationalError:
        return None
    if found is None:
        return None
    return replace(found.live(), seen=_seen(connection))


def _stat(path: Path) -> list[int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return [info.st_ino, info.st_mtime_ns, info.st_size]


def _load(machines: Path) -> MachinesConfig | None:
    try:
        config, findings = load_machines_yml(machines)
    except (OSError, ValueError):
        return None
    if any(item.severity == "error" for item in findings):
        return None
    return config


def _switches(config: MachinesConfig) -> dict[str, Switch]:
    return {d.name: Switch(d.enabled, d.drain, d.slot_max) for d in config.devices}


def _account_switches(config: MachinesConfig) -> dict[str, AccountSwitch]:
    return {a.id: AccountSwitch(a.paused, a.max_jobs) for a in config.accounts}


def _plain(switches: Mapping[str, Switch]) -> dict[str, dict]:
    return {
        name: {"enabled": s.enabled, "drain": s.drain, "slot_max": s.slot_max}
        for name, s in sorted(switches.items())
    }


def _parsed(plain: Mapping[str, Mapping]) -> dict[str, Switch]:
    return {
        str(name): Switch(bool(value["enabled"]), bool(value["drain"]), value["slot_max"])
        for name, value in plain.items()
    }


def _plain_accounts(accounts: Mapping[str, AccountSwitch]) -> dict[str, dict]:
    return {
        name: {"paused": s.paused, "max_jobs": s.max_jobs}
        for name, s in sorted(accounts.items())
    }


def _parsed_accounts(plain: Mapping[str, Mapping]) -> dict[str, AccountSwitch]:
    return {
        str(name): AccountSwitch(bool(value["paused"]), value["max_jobs"])
        for name, value in plain.items()
    }


def _digest(switches: Mapping[str, Switch], accounts: Mapping[str, AccountSwitch]) -> str:
    plain: dict = _plain(switches)
    accounts = _declared(accounts)
    if accounts:
        plain = {"devices": plain, "accounts": _plain_accounts(accounts)}
    encoded = json.dumps(plain, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _create(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            adopted_switches TEXT NOT NULL,
            written INTEGER NOT NULL,
            adopted INTEGER NOT NULL,
            adopted_digest TEXT NOT NULL,
            sources TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ACCOUNT_TABLE} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            adopted_accounts TEXT,
            adopted INTEGER NOT NULL,
            sources TEXT NOT NULL
        )
        """
    )


def _read(connection: sqlite3.Connection) -> _Row | None:
    row = connection.execute(
        f"SELECT adopted_switches, written, adopted, adopted_digest, sources FROM {TABLE}"
        " WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    adopted = int(row[2])
    try:
        account_row = connection.execute(
            f"SELECT adopted_accounts, adopted, sources FROM {ACCOUNT_TABLE} WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        account_row = None
    adopted_accounts = None
    account_sources: dict = {}
    if account_row is not None:
        written_with_this_row = int(account_row[1]) == adopted
        if account_row[0] is not None and written_with_this_row:
            adopted_accounts = _parsed_accounts(json.loads(account_row[0]))
        account_sources = json.loads(account_row[2])
    sources = {}
    for path, source in json.loads(row[4]).items():
        key = str(source["key"])
        entry = account_sources.get(path) or {}
        found = entry.get("accounts") if entry.get("key") == key else None
        sources[str(path)] = _Source(
            key, _parsed(source["switches"]), bool(source.get("pinned", False)),
            None if found is None else _parsed_accounts(found),
        )
    return _Row(_parsed(json.loads(row[0])), adopted_accounts, int(row[1]), adopted,
                str(row[3]), sources)


def _seen(connection: sqlite3.Connection) -> dict[str, int]:
    try:
        rows = connection.execute(f"SELECT harness, adopted FROM {SEEN_TABLE}").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(harness): int(adopted) for harness, adopted in rows}


def _write(connection: sqlite3.Connection, row: _Row) -> None:
    sources = {
        path: {"key": source.key, "switches": _plain(source.switches), "pinned": source.pinned}
        for path, source in sorted(row.sources.items())
    }
    connection.execute(
        f"""
        INSERT INTO {TABLE} (id, adopted_switches, written, adopted, adopted_digest, sources)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET adopted_switches = excluded.adopted_switches,
            written = excluded.written, adopted = excluded.adopted,
            adopted_digest = excluded.adopted_digest, sources = excluded.sources
        """,
        (json.dumps(_plain(row.adopted_switches), separators=(",", ":")), row.written,
         row.adopted, row.adopted_digest, json.dumps(sources, separators=(",", ":"))),
    )
    account_sources = {
        path: {"key": source.key,
               "accounts": None if source.accounts is None else _plain_accounts(source.accounts)}
        for path, source in sorted(row.sources.items())
    }
    adopted_accounts = (None if row.adopted_accounts is None
                        else json.dumps(_plain_accounts(row.adopted_accounts), separators=(",", ":")))
    connection.execute(
        f"""
        INSERT INTO {ACCOUNT_TABLE} (id, adopted_accounts, adopted, sources) VALUES (1, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET adopted_accounts = excluded.adopted_accounts,
            adopted = excluded.adopted, sources = excluded.sources
        """,
        (adopted_accounts, row.adopted, json.dumps(account_sources, separators=(",", ":"))),
    )
