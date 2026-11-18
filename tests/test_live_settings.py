"""The shared settings row every claim places by (#852, lib/live_settings.py)."""

from __future__ import annotations

import itertools
import os
import sqlite3
from pathlib import Path

import pytest

from lib import live_settings
from lib.live_settings import CLOSED, Live, Switch
from lib.machines_yml import MachinesConfig, load_machines_yml

PC = """\
devices:
  - name: box
    local: true
    enabled: {box_enabled}
    drain: {box_drain}
    slot_max: {box_slots}
    harnesses: {{claude: claude-bot}}
  - name: pc
    tailscale_name: pc.example.ts.net
    enabled: {pc_enabled}
    slot_max: 3
    harnesses: {{claude: claude-bot}}
    workdir_root: /srv/work/{{bot}}
    ssh_alias: pc-{{bot}}
    factory_root: /srv/factory
"""

BOX_ONLY = """\
devices:
  - name: box
    local: true
    slot_max: 2
    harnesses: {claude: claude-bot}
"""


def text(*, box_enabled=True, box_drain=False, box_slots=2, pc_enabled=True) -> str:
    return PC.format(box_enabled=str(box_enabled).lower(), box_drain=str(box_drain).lower(),
                     box_slots=box_slots, pc_enabled=str(pc_enabled).lower())


STAMPS = itertools.count(10**18, 10**9)


def write_fresh(machines: Path, body: str) -> None:
    machines.write_text(body, encoding="utf-8")
    stamp = next(STAMPS)
    os.utime(machines, ns=(stamp, stamp))


def write(machines: Path, body: str) -> MachinesConfig:
    write_fresh(machines, body)
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    return config


def switches(**overrides: Switch) -> dict[str, Switch]:
    return {"box": Switch(True, False, 2), "pc": Switch(True, False, 3), **overrides}


@pytest.fixture
def db(tmp_path: Path):
    connection = sqlite3.connect(tmp_path / "orch.db", isolation_level=None)
    yield connection
    connection.close()


def adopt(connection: sqlite3.Connection, machines: Path) -> tuple[Live, MachinesConfig]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        return live_settings.adopted(connection, machines)
    finally:
        connection.execute("COMMIT")


def narrow(connection: sqlite3.Connection, machines: Path) -> Live:
    connection.execute("BEGIN IMMEDIATE")
    try:
        return live_settings.narrowed(connection, machines)
    finally:
        connection.execute("COMMIT")


@pytest.mark.parametrize(
    ("first", "second", "met"),
    [
        (Switch(True, False, 3), Switch(True, False, 3), Switch(True, False, 3)),
        (Switch(True, False, 3), Switch(False, False, 3), Switch(False, False, 3)),
        (Switch(False, False, 3), Switch(True, False, 3), Switch(False, False, 3)),
        (Switch(True, False, 3), Switch(True, True, 3), Switch(True, True, 3)),
        (Switch(True, True, 3), Switch(True, False, 3), Switch(True, True, 3)),
        (Switch(True, False, 6), Switch(True, False, 1), Switch(True, False, 1)),
        (Switch(True, False, 1), Switch(True, False, 6), Switch(True, False, 1)),
        (Switch(True, False, None), Switch(True, False, 4), Switch(True, False, None)),
        (Switch(True, False, 4), Switch(True, False, None), Switch(True, False, None)),
        (Switch(True, False, 4), CLOSED, CLOSED),
    ],
)
def test_meet_keeps_the_stricter_of_each_field(first: Switch, second: Switch, met: Switch) -> None:
    assert first.meet(second) == met
    assert second.meet(first) == met, "meet is symmetric"


def test_peek_is_none_before_any_process_writes_the_row(tmp_path: Path, db) -> None:
    assert live_settings.peek(db) is None, "no table yet"
    machines = tmp_path / "machines.yml"
    write(machines, text())

    live, _ = adopt(db, machines)

    assert live_settings.peek(db) == live


def test_adopted_sets_the_row_to_the_file_exactly(tmp_path: Path, db) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, text())
    adopt(db, machines)
    write(machines, text(box_enabled=False, box_drain=True, box_slots=1, pc_enabled=False))
    narrow(db, machines)
    wider = write(machines, text(box_slots=6))

    live, config = adopt(db, machines)

    assert config == wider, "the parse the monitor acts on is the file on disk"
    assert dict(live.switches) == switches(box=Switch(True, False, 6))
    assert live.adopted == live.written, "the poller has acted on the newest version"
    assert live_settings.peek(db) == live


@pytest.mark.parametrize(
    "wider",
    [
        {"box_enabled": True},
        {"box_drain": False},
        {"box_slots": 6},
    ],
    ids=["enable", "undrain", "raise-slot-max"],
)
def test_narrowed_never_widens_the_row(tmp_path: Path, db, wider: dict) -> None:
    machines = tmp_path / "machines.yml"
    narrowest = {"box_enabled": False, "box_drain": True, "box_slots": 1}
    write(machines, text(**narrowest))
    before, _ = adopt(db, machines)
    write(machines, text(**{**narrowest, **wider}))

    after = narrow(db, machines)

    assert after.switches == before.switches, "a widening waits for the poller's adopt"
    assert after.written == before.written + 1, "the row saw a new file version"
    assert after.adopted == before.adopted
    assert not after.is_applied(after.written), "the widening is not applied yet"


@pytest.mark.parametrize(
    ("narrower", "expected"),
    [
        ({"box_enabled": False}, Switch(False, False, 2)),
        ({"box_drain": True}, Switch(True, True, 2)),
        ({"box_slots": 1}, Switch(True, False, 1)),
    ],
    ids=["disable", "drain", "lower-slot-max"],
)
def test_narrowed_applies_a_narrowing_at_once(
    tmp_path: Path, db, narrower: dict, expected: Switch
) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, text())
    adopt(db, machines)
    write(machines, text(**narrower))

    live = narrow(db, machines)

    assert dict(live.switches) == switches(box=expected)
    assert live_settings.peek(db) == live


def test_narrowed_with_no_row_closes_every_device_until_the_poller_adopts(
    tmp_path: Path, db
) -> None:
    machines = tmp_path / "machines.yml"
    loaded = write(machines, text())

    live = narrow(db, machines)

    assert (live.written, live.adopted) == (1, 0)
    assert not any(d.enabled for d in live.placement_config(loaded).devices)
    adopted, _ = adopt(db, machines)
    assert all(d.enabled for d in adopted.placement_config(loaded).devices)
    assert adopted.is_applied(live.written)


def test_an_unchanged_file_leaves_the_row_as_it_was(tmp_path: Path, db) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, text())
    adopted, _ = adopt(db, machines)

    assert narrow(db, machines) == adopted
    assert adopt(db, machines)[0] == adopted, "no new version"


def test_an_invalid_file_leaves_the_row_unchanged(tmp_path: Path, db) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, text(box_enabled=False))
    before, _ = adopt(db, machines)
    write_fresh(machines, text().replace("slot_max: 3", "slot_max: 0"))

    assert narrow(db, machines) == before, "narrowed keeps the old row"
    with pytest.raises(ValueError):
        adopt(db, machines)
    assert live_settings.peek(db) == before, "adopted keeps the old row"


def test_an_invalid_file_with_no_row_writes_nothing_and_closes_every_device(
    tmp_path: Path, db
) -> None:
    machines = tmp_path / "machines.yml"
    loaded = write(machines, text())
    write_fresh(machines, "devices: [\n")

    live = narrow(db, machines)

    assert not any(d.enabled for d in live.placement_config(loaded).devices)
    assert live_settings.peek(db) is None


def test_a_device_missing_from_the_file_is_closed_for_placement(tmp_path: Path, db) -> None:
    machines = tmp_path / "machines.yml"
    loaded = write(machines, text())
    adopt(db, machines)
    write(machines, BOX_ONLY)

    live = narrow(db, machines)

    pc = live.placement_config(loaded).device("pc")
    assert pc is not None and not pc.enabled, "pc left the file: no placement there"
    adopted, _ = adopt(db, machines)
    pc = adopted.placement_config(loaded).device("pc")
    assert pc is not None and not pc.enabled, "nor once the poller adopts the file"


def test_placement_config_takes_each_switch_from_the_row(tmp_path: Path, db) -> None:
    machines = tmp_path / "machines.yml"
    loaded = write(machines, text(box_slots=6))
    write(machines, text(box_drain=True, box_slots=1, pc_enabled=False))
    live, _ = adopt(db, machines)

    placed = live.placement_config(loaded)

    box, pc = placed.device("box"), placed.device("pc")
    assert box is not None and (box.enabled, box.drain, box.slot_max) == (True, True, 1)
    assert pc is not None and not pc.enabled
    assert box.harnesses == loaded.device("box").harnesses, "everything else is the loaded config"
