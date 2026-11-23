"""The shared settings row every claim places by (#852, lib/live_settings.py)."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sqlite3
from pathlib import Path

import pytest

from lib import live_settings
from lib.live_settings import ACCOUNT_CLOSED, CLOSED, AccountSwitch, Live, Switch
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
        return live_settings.recorded(connection, machines)
    finally:
        connection.execute("COMMIT")


def request(connection: sqlite3.Connection, machines: Path) -> Live:
    connection.execute("BEGIN IMMEDIATE")
    try:
        return live_settings.requested(connection, machines)
    finally:
        connection.execute("COMMIT")


def checkouts(tmp_path: Path, *names: str) -> list[Path]:
    paths = [tmp_path / name / "machines.yml" for name in names]
    for path in paths:
        path.parent.mkdir()
    return paths


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
def test_recorded_never_widens_past_the_adopted(tmp_path: Path, db, wider: dict) -> None:
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
def test_recorded_applies_a_narrowing_at_once(
    tmp_path: Path, db, narrower: dict, expected: Switch
) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, text())
    adopt(db, machines)
    write(machines, text(**narrower))

    live = narrow(db, machines)

    assert dict(live.switches) == switches(box=expected)
    assert live_settings.peek(db) == live


def test_recorded_with_no_row_closes_every_device_until_the_poller_adopts(
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

    assert narrow(db, machines) == before, "recorded keeps the old row"
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


def test_peek_reads_back_what_each_harness_has_seen(tmp_path: Path, db) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, text())
    live, _ = adopt(db, machines)
    assert live_settings.peek(db).seen == {}, "no seen table yet"

    live_settings.record_seen(db, "claude", 1)
    live_settings.record_seen(db, "codex", 1)
    live_settings.record_seen(db, "claude", 2)

    assert live_settings.peek(db).seen == {"claude": 2, "codex": 1}
    assert live_settings.peek(db).switches == live.switches


def test_recording_an_unchanged_seen_writes_nothing(tmp_path: Path, db) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, text())
    adopt(db, machines)
    live_settings.record_seen(db, "claude", 1)
    before = db.total_changes

    live_settings.record_seen(db, "claude", 1)

    assert db.total_changes == before
    live_settings.record_seen(db, "claude", 2)
    assert db.total_changes == before + 1, "a new version is written"


def test_a_second_file_narrows_until_it_holds_the_adopted_enable(tmp_path: Path, db) -> None:
    poller = tmp_path / "poller" / "machines.yml"
    waiter = tmp_path / "waiter" / "machines.yml"
    poller.parent.mkdir()
    waiter.parent.mkdir()
    write(poller, text())
    write(waiter, text(pc_enabled=False))
    adopted, _ = adopt(db, poller)

    closed = narrow(db, waiter)

    assert adopted.switches["pc"].enabled and adopted.places_by_adopted()
    assert not closed.switches["pc"].enabled
    assert not closed.places_by_adopted()
    write(waiter, text())

    widened = narrow(db, waiter)

    assert widened.switches == adopted.switches
    assert widened.places_by_adopted()
    assert widened.adopted == adopted.adopted


def test_a_device_only_one_file_lists_does_not_hold_placement_off_the_adopted(
    tmp_path: Path, db
) -> None:
    poller = tmp_path / "poller" / "machines.yml"
    waiter = tmp_path / "waiter" / "machines.yml"
    poller.parent.mkdir()
    waiter.parent.mkdir()
    write(poller, BOX_ONLY)
    loaded = write(waiter, text())
    adopt(db, poller)

    live = narrow(db, waiter)

    pc = live.placement_config(loaded).device("pc")
    assert pc is not None and not pc.enabled
    assert live.places_by_adopted()


def test_a_poller_on_another_checkout_keeps_the_requested_file_in_the_meet(
    tmp_path: Path, db
) -> None:
    settings, stale = checkouts(tmp_path, "settings", "stale")
    write(stale, text(box_slots=6))
    loaded = write(settings, text(box_slots=1))
    request(db, settings)

    live, _ = adopt(db, stale)

    assert live.switches["box"] == Switch(True, False, 1), "the Settings limit holds"
    assert live.placement_config(loaded).device("box").slot_max == 1
    assert live.adopted_holds("box", "slot_max", 6)
    assert not live.adopted_holds("box", "slot_max", 1)


def test_requested_pins_a_file_already_recorded_unchanged(tmp_path: Path, db) -> None:
    settings, stale = checkouts(tmp_path, "settings", "stale")
    write(stale, text(box_slots=6))
    write(settings, text(box_slots=1))
    recorded = narrow(db, settings)

    pinned = request(db, settings)

    assert pinned.written == recorded.written + 1, "the pin is a new version"
    assert request(db, settings) == pinned, "pinning again writes nothing"
    assert adopt(db, stale)[0].switches["box"].slot_max == 1


def test_a_later_request_from_another_file_moves_the_pin(tmp_path: Path, db) -> None:
    first, second, stale = checkouts(tmp_path, "first", "second", "stale")
    write(stale, text(box_slots=6))
    write(first, text(box_slots=1))
    write(second, text(box_slots=4))
    request(db, first)
    request(db, second)

    live, _ = adopt(db, stale)

    assert live.switches["box"].slot_max == 4, "only the latest Settings file stays"


@pytest.mark.parametrize("refresh", [narrow, lambda connection, path: adopt(connection, path)[0]],
                         ids=["recorded", "adopted"])
def test_rereading_the_pinned_file_keeps_its_pin_with_the_new_switches(
    tmp_path: Path, db, refresh
) -> None:
    settings, stale = checkouts(tmp_path, "settings", "stale")
    write(stale, text(box_slots=6))
    write(settings, text(box_slots=1))
    request(db, settings)
    write(settings, text(box_slots=4))
    refresh(db, settings)

    live, _ = adopt(db, stale)

    assert live.switches["box"].slot_max == 4


def test_a_row_written_before_pins_reads_every_file_unpinned(tmp_path: Path, db) -> None:
    settings, stale = checkouts(tmp_path, "settings", "stale")
    write(stale, text(box_slots=6))
    write(settings, text(box_slots=1))
    request(db, settings)
    (sources,) = db.execute(f"SELECT sources FROM {live_settings.TABLE}").fetchone()
    legacy = {path: {"key": source["key"], "switches": source["switches"]}
              for path, source in json.loads(sources).items()}
    db.execute(f"UPDATE {live_settings.TABLE} SET sources = ?", (json.dumps(legacy),))

    live, _ = adopt(db, stale)

    assert live.switches["box"].slot_max == 6, "nothing pinned, so only the poller's file is left"


@pytest.mark.parametrize(
    ("device", "name", "value", "holds"),
    [("box", "slot_max", 2, True), ("box", "enabled", True, True), ("box", "drain", False, True),
     ("box", "slot_max", 3, False), ("gone", "slot_max", 2, False), ("box", "harnesses", 2, False)],
)
def test_adopted_holds_reads_one_field_of_the_adopted_switches(
    tmp_path: Path, db, device: str, name: str, value: object, holds: bool
) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, text())
    live, _ = adopt(db, machines)

    assert live.adopted_holds(device, name, value) is holds


ACCOUNTS = """\
devices:
  - name: box
    local: true
    slot_max: 4
    harnesses: {{claude: {{bot: claude-bot, account: max-1}}}}
accounts:
  - {{id: max-1, harness: claude{max_1}}}
"""


def accounts_text(**max_1: object) -> str:
    fields = "".join(f", {key}: {str(value).lower()}" for key, value in max_1.items())
    return ACCOUNTS.format(max_1=fields)


@pytest.mark.parametrize(
    ("first", "second", "met"),
    [
        (AccountSwitch(False, 3), AccountSwitch(False, 3), AccountSwitch(False, 3)),
        (AccountSwitch(False, 3), AccountSwitch(True, 3), AccountSwitch(True, 3)),
        (AccountSwitch(False, 6), AccountSwitch(False, 1), AccountSwitch(False, 1)),
        (AccountSwitch(False, None), AccountSwitch(False, 4), AccountSwitch(False, 4)),
        (AccountSwitch(False, None), AccountSwitch(False, None), AccountSwitch(False, None)),
        (AccountSwitch(False, 4), ACCOUNT_CLOSED, ACCOUNT_CLOSED),
        (AccountSwitch(False, None, frozenset({"box", "pc"})),
         AccountSwitch(False, None, frozenset({"pc"})), AccountSwitch(False, None, frozenset({"pc"}))),
        (AccountSwitch(False, None, frozenset({"pc"})), AccountSwitch(False, None),
         AccountSwitch(False, None, frozenset({"pc"}))),
    ],
)
def test_account_meet_is_paused_if_either_side_is_with_the_lower_cap_and_common_devices(
    first: AccountSwitch, second: AccountSwitch, met: AccountSwitch
) -> None:
    assert first.meet(second) == met
    assert second.meet(first) == met, "meet is symmetric"


def test_placement_config_takes_paused_and_max_jobs_from_the_row(tmp_path: Path, db) -> None:
    machines = tmp_path / "machines.yml"
    loaded = write(machines, accounts_text(max_jobs=6))
    write(machines, accounts_text(paused=True, max_jobs=2))
    live, _ = adopt(db, machines)

    account = live.placement_config(loaded).account("max-1")

    assert account is not None and (account.paused, account.max_jobs) == (True, 2)
    assert dict(live.accounts) == {"max-1": AccountSwitch(True, 2, frozenset())}


@pytest.mark.parametrize(
    ("narrower", "expected"),
    [({"paused": True}, AccountSwitch(True, 3, frozenset())),
     ({"max_jobs": 1}, AccountSwitch(False, 1, frozenset()))],
    ids=["pause", "lower-max-jobs"],
)
def test_recorded_applies_an_account_narrowing_at_once(
    tmp_path: Path, db, narrower: dict, expected: AccountSwitch
) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, accounts_text(max_jobs=3))
    adopt(db, machines)
    write(machines, accounts_text(**{"max_jobs": 3, **narrower}))

    live = narrow(db, machines)

    assert dict(live.accounts) == {"max-1": expected}
    assert not live.places_by_adopted()
    assert live_settings.peek(db) == live


@pytest.mark.parametrize(
    ("loaded", "wider"),
    [({"paused": True, "max_jobs": 3}, {"max_jobs": 3}), ({"max_jobs": 1}, {"max_jobs": 3}),
     ({"max_jobs": 1}, {})],
    ids=["resume", "raise-max-jobs", "drop-max-jobs"],
)
def test_an_account_widening_waits_for_the_pollers_adopt(
    tmp_path: Path, db, loaded: dict, wider: dict
) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, accounts_text(**loaded))
    before, _ = adopt(db, machines)
    write(machines, accounts_text(**wider))

    after = narrow(db, machines)

    assert after.accounts == before.accounts, "a widening waits for the poller's adopt"
    adopted, _ = adopt(db, machines)
    assert dict(adopted.accounts) == {
        "max-1": AccountSwitch(False, wider.get("max_jobs"), frozenset())
    }


def test_an_account_missing_from_the_row_is_closed_until_the_poller_adopts(
    tmp_path: Path, db
) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, BOX_ONLY)
    adopt(db, machines)
    loaded = write(machines, accounts_text())

    live = narrow(db, machines)

    account = live.placement_config(loaded).account("max-1")
    assert account is not None and account.paused and account.max_jobs == 0
    adopted, _ = adopt(db, machines)
    account = adopted.placement_config(loaded).account("max-1")
    assert account is not None and not account.paused and account.max_jobs is None


def test_a_device_row_written_without_its_account_row_closes_accounts_until_rerecorded(
    tmp_path: Path, db
) -> None:
    machines = tmp_path / "machines.yml"
    loaded = write(machines, accounts_text())
    adopt(db, machines)
    db.execute(f"DROP TABLE {live_settings.ACCOUNT_TABLE}")

    stale = live_settings.peek(db)

    assert stale is not None
    assert stale.placement_config(loaded).account("max-1").paused, (
        "an older binary wrote device_live alone"
    )
    adopted, _ = adopt(db, machines)
    assert not adopted.placement_config(loaded).account("max-1").paused


def test_the_requested_files_pause_holds_through_another_checkouts_adopt(
    tmp_path: Path, db
) -> None:
    settings, stale = checkouts(tmp_path, "settings", "stale")
    write(stale, accounts_text(max_jobs=6))
    write(settings, accounts_text(paused=True, max_jobs=6))
    request(db, settings)

    live, _ = adopt(db, stale)

    assert live.accounts["max-1"] == AccountSwitch(True, 6, frozenset()), "the Settings pause holds"


def test_the_shipped_config_keeps_its_digest() -> None:
    shipped = Path(__file__).resolve().parents[1] / "machines.yml"
    config, findings = load_machines_yml(shipped)
    assert config is not None, findings
    devices = {
        d.name: {"enabled": d.enabled, "drain": d.drain, "slot_max": d.slot_max}
        for d in sorted(config.devices, key=lambda item: item.name)
    }
    before = hashlib.sha256(json.dumps(devices, separators=(",", ":")).encode("utf-8")).hexdigest()
    connection = sqlite3.connect(":memory:", isolation_level=None)

    live, _ = adopt(connection, shipped)

    assert live.adopted_digest == before, "the digest #852 shipped, for a file with no accounts"
    assert live.places_by_adopted()


def test_an_adopted_signed_in_on_holds_only_the_devices_the_poller_adopted(
    tmp_path: Path, db
) -> None:
    machines = tmp_path / "machines.yml"
    write(machines, accounts_text().replace("local: true", "local: true\n    home: /home/{bot}")
          + "  - {id: work, harness: claude, signed_in_on: [box]}\n")

    live, _ = adopt(db, machines)

    assert live.adopted_account_holds("work", "signed_in_on", ["box"])
    assert not live.adopted_account_holds("work", "signed_in_on", ["box", "pc"])
