"""The device settings every process places by (#852).

One row in orch.db's `device_live` holds each device's `enabled`, `drain`
and `slot_max` as placement may use them now. Placement reads them from the
row, never from a process's own copy of machines.yml, so no process mixes
fields from two versions of the file.

The row is never wider than machines.yml as it is on disk, and never wider
than the view the poller's DeviceMonitor acts on:

* `narrowed` runs inside every claim's BEGIN IMMEDIATE (and the settings
  applier's). It meets the file into the row: a device is enabled only if
  both say so, drained if either does, and its slot_max is the lower. It
  never widens. A device missing from either side counts as closed. The
  first writer (a claim or the applier) creates the row with every device
  closed, so nothing is placed before the poller adopts.
* `adopted` is called by the poller only, inside its own BEGIN IMMEDIATE. It
  sets the row to the file exactly and returns the parse it wrote, which the
  monitor installs as its view before it next reconciles. It is the only
  way a widening (an enable, an undrain, a higher slot_max) reaches
  placement, so a claim can land on a device only once the poller already
  treats that device as enabled.

`written` counts the file versions the row has seen and `adopted` the last
of them the poller acted on. The settings applier records `written` right
after its write as the change's `version`; the change is applied once
`adopted` reaches it (`peek`, read by the dashboard).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from lib.config_overlay import overlay_path
from lib.machines_yml import MachinesConfig, load_machines_yml

TABLE = "device_live"


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


@dataclass(frozen=True)
class Live:
    switches: Mapping[str, Switch]
    written: int
    adopted: int

    def placement_config(self, config: MachinesConfig) -> MachinesConfig:
        devices = []
        for device in config.devices:
            switch = self.switches.get(device.name, CLOSED)
            devices.append(replace(device, enabled=switch.enabled, drain=switch.drain,
                                   slot_max=switch.slot_max))
        return replace(config, devices=tuple(devices))

    def is_applied(self, version: int) -> bool:
        return self.adopted >= version


def file_key(machines: Path) -> str:
    """Changes whenever machines.yml or its machines.local.yml overlay does."""
    return json.dumps([_stat(machines), _stat(overlay_path(machines))])


def narrowed(connection: sqlite3.Connection, machines: Path) -> Live:
    """Meet machines.yml as it is now into the row. The caller holds BEGIN
    IMMEDIATE. With no row yet, every device starts closed. An invalid file
    writes nothing and leaves the row as it was."""
    _create(connection)
    key = file_key(machines)
    found = _read(connection)
    if found is not None and found[1] == key:
        return found[0]
    row = Live({}, written=0, adopted=0) if found is None else found[0]
    config = _load(machines)
    if config is None:
        return row
    live = Live(_meet(row.switches, _switches(config)), row.written + 1, row.adopted)
    _write(connection, live, key)
    return live


def adopted(connection: sqlite3.Connection, machines: Path) -> tuple[Live, MachinesConfig]:
    """Set the row to machines.yml exactly and return the parse. Poller only;
    the caller holds BEGIN IMMEDIATE. An invalid file raises ValueError and
    leaves the row as it was."""
    _create(connection)
    key = file_key(machines)
    try:
        config, findings = load_machines_yml(machines)
    except OSError as exc:
        raise ValueError(f"{machines}: cannot read: {type(exc).__name__}") from exc
    errors = [item for item in findings if item.severity == "error"]
    if config is None or errors:
        detail = errors[0].message if errors else "invalid machines.yml"
        raise ValueError(f"{machines}: {detail}")
    found = _read(connection)
    if found is None:
        written = 1
    else:
        written = found[0].written + (found[1] != key)
    live = Live(_switches(config), written=written, adopted=written)
    _write(connection, live, key)
    return live, config


def peek(connection: sqlite3.Connection) -> Live | None:
    """The row, read only; None before any process has written it."""
    try:
        found = _read(connection)
    except sqlite3.OperationalError:
        return None
    return None if found is None else found[0]


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


def _meet(row: Mapping[str, Switch], file: Mapping[str, Switch]) -> dict[str, Switch]:
    return {
        name: row.get(name, CLOSED).meet(file.get(name, CLOSED))
        for name in (*row, *(name for name in file if name not in row))
    }


def _create(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            switches TEXT NOT NULL,
            written INTEGER NOT NULL,
            adopted INTEGER NOT NULL,
            file_key TEXT NOT NULL
        )
        """
    )


def _read(connection: sqlite3.Connection) -> tuple[Live, str] | None:
    row = connection.execute(
        f"SELECT switches, written, adopted, file_key FROM {TABLE} WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    switches = {
        str(name): Switch(bool(value["enabled"]), bool(value["drain"]), value["slot_max"])
        for name, value in json.loads(row[0]).items()
    }
    return Live(switches, int(row[1]), int(row[2])), str(row[3])


def _write(connection: sqlite3.Connection, live: Live, key: str) -> None:
    switches = json.dumps(
        {
            name: {"enabled": s.enabled, "drain": s.drain, "slot_max": s.slot_max}
            for name, s in sorted(live.switches.items())
        },
        separators=(",", ":"),
    )
    connection.execute(
        f"""
        INSERT INTO {TABLE} (id, switches, written, adopted, file_key)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET switches = excluded.switches,
            written = excluded.written, adopted = excluded.adopted,
            file_key = excluded.file_key
        """,
        (switches, live.written, live.adopted, key),
    )
