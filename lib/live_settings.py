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
reaches it, the adopted switches hold the requested value
(`Live.adopted_holds`; both read by the dashboard through `peek`) and every
waiter it names has recorded `seen` at that version, which a claim does only
when placement uses exactly the adopted switches (`Live.places_by_adopted`).
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
from lib.machines_yml import MachinesConfig, load_machines_yml

TABLE = "device_live"
SEEN_TABLE = "device_live_seen"


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
class Live:
    switches: Mapping[str, Switch]
    written: int
    adopted: int
    adopted_digest: str = ""
    seen: Mapping[str, int] = field(default_factory=dict)
    adopted_switches: Mapping[str, Switch] = field(default_factory=dict)

    def placement_config(self, config: MachinesConfig) -> MachinesConfig:
        devices = []
        for device in config.devices:
            switch = self.switches.get(device.name, CLOSED)
            devices.append(replace(device, enabled=switch.enabled, drain=switch.drain,
                                   slot_max=switch.slot_max))
        return replace(config, devices=tuple(devices))

    def is_applied(self, version: int) -> bool:
        return self.adopted >= version

    def adopted_holds(self, device: str, name: str, value: object) -> bool:
        switch = self.adopted_switches.get(device)
        return switch is not None and name in _FIELDS and getattr(switch, name) == value

    def places_by_adopted(self) -> bool:
        return self.adopted > 0 and _digest(self.switches) == self.adopted_digest


@dataclass(frozen=True)
class _Source:
    key: str
    switches: Mapping[str, Switch]
    pinned: bool = False


@dataclass(frozen=True)
class _Row:
    adopted_switches: Mapping[str, Switch]
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
        return Live(switches, self.written, self.adopted, self.adopted_digest,
                    adopted_switches=self.adopted_switches)


EMPTY = _Row({}, written=0, adopted=0, adopted_digest="", sources={})


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
    if source is not None and source.key == key and source.pinned == pinned:
        return row.live()
    config = _load(machines)
    if config is None:
        return row.live()
    sources = {name: replace(item, pinned=item.pinned and not pin)
               for name, item in row.sources.items()}
    row = replace(row, written=row.written + 1,
                  sources={**sources, path: _Source(key, _switches(config), pinned)})
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
    kept = {name: item for name, item in found.sources.items() if item.pinned}
    own = _Source(key, switches, prior is not None and prior.pinned)
    row = _Row(switches, written=written, adopted=written, adopted_digest=_digest(switches),
               sources={**kept, path: own})
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


def _digest(switches: Mapping[str, Switch]) -> str:
    encoded = json.dumps(_plain(switches), separators=(",", ":"))
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


def _read(connection: sqlite3.Connection) -> _Row | None:
    row = connection.execute(
        f"SELECT adopted_switches, written, adopted, adopted_digest, sources FROM {TABLE}"
        " WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    sources = {
        str(path): _Source(str(source["key"]), _parsed(source["switches"]),
                           bool(source.get("pinned", False)))
        for path, source in json.loads(row[4]).items()
    }
    return _Row(_parsed(json.loads(row[0])), int(row[1]), int(row[2]), str(row[3]), sources)


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
