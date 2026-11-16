"""A heartbeat's age leaves out time the box was suspended (#544)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from lib import heal, health

NOW = 1_800_000_000.0
BOOT = "8cd10924-c547-44ff-9fc4-1decdc2c833e"
REBOOT = "0f0e0d0c-0b0a-4908-8706-050403020100"
THIRD_BOOT = "33333333-3333-4333-8333-333333333333"
DEVICES = {"box", "pc"}
SUSPEND = 360.0


class Clocks:
    def __init__(self) -> None:
        self.wall = NOW
        self.monotonic = 5_000.0
        self.boot = BOOT

    def advance(self, wall: float, monotonic: float) -> None:
        self.wall += wall
        self.monotonic += monotonic

    def uptime(self) -> health.Uptime:
        return health.Uptime(self.boot, self.monotonic)


CASES = [
    ("poller", "box", health.BEAT_SECONDS),
    ("device-probe", "pc", health.PROBE_SECONDS),
    ("dashboard-push", "box", 60.0),
]


def beat(db: Path, clocks: Clocks, component: str, device: str, interval: float) -> None:
    store = health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                               uptime=clocks.uptime)
    store.beat(component, device, pid=101, interval=interval)


def state(db: Path, clocks: Clocks, component: str, device: str) -> tuple[str, str]:
    report = health.build_report(health.read_heartbeats(db), clocks.wall,
                                 uptime=clocks.uptime())
    (item,) = [c for c in report.components if c.component == component and c.device == device]
    return item.state, item.reason


@pytest.mark.parametrize(("component", "device", "interval"), CASES)
def test_a_suspend_does_not_age_a_fresh_beat(tmp_path: Path, component: str, device: str,
                                             interval: float) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    beat(db, clocks, component, device, interval)
    clocks.advance(wall=SUSPEND, monotonic=2.0)
    assert state(db, clocks, component, device) == ("up", "")
    (row,) = [r for r in health.read_heartbeats(db) if r.component == component]
    assert health.classify(row, clocks.wall)[0] != "up", "wall age alone would not be up"


@pytest.mark.parametrize(("component", "device", "interval"), CASES)
def test_a_real_hang_is_still_down(tmp_path: Path, component: str, device: str,
                                   interval: float) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    beat(db, clocks, component, device, interval)
    hang = max(SUSPEND, health.DOWN_BEATS * interval + 1)
    clocks.advance(wall=hang, monotonic=hang)
    current, reason = state(db, clocks, component, device)
    assert current == "down"
    assert reason.startswith("no heartbeat for ")


def test_a_poller_hung_six_minutes_is_down(tmp_path: Path) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    beat(db, clocks, "poller", "box", health.BEAT_SECONDS)
    clocks.advance(wall=SUSPEND, monotonic=SUSPEND)
    assert state(db, clocks, "poller", "box") == ("down", "no heartbeat for 6m")


def test_after_a_reboot_a_missing_beat_is_down(tmp_path: Path) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    beat(db, clocks, "poller", "box", health.BEAT_SECONDS)
    clocks.boot = REBOOT
    clocks.wall += SUSPEND
    clocks.monotonic = clocks.monotonic + 2.0
    assert state(db, clocks, "poller", "box")[0] == "down"


def test_a_previous_boot_beat_is_down_before_its_wall_age_runs_out(tmp_path: Path) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    beat(db, clocks, "poller", "box", health.BEAT_SECONDS)
    clocks.boot = REBOOT
    clocks.wall += 30.0
    clocks.monotonic = 10.0
    assert state(db, clocks, "poller", "box") == ("down", "never seen since reboot")


def test_another_process_beat_keeps_a_suspend_fresh_probe_of_another_device(
        tmp_path: Path) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    store = health.HealthStore(db, devices={"box", "pc", "mac"}, clock=lambda: clocks.wall,
                               uptime=clocks.uptime)
    for device in ("pc", "mac"):
        store.beat("device-probe", device, pid=101, interval=health.PROBE_SECONDS)
    clocks.advance(wall=150.0 + SUSPEND, monotonic=150.0 + 2.0)
    store.beat("device-probe", "pc", pid=202, interval=health.PROBE_SECONDS)
    assert state(db, clocks, "device-probe", "mac") == ("up", "")


def test_another_process_beat_still_clears_a_hung_probe(tmp_path: Path) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    store = health.HealthStore(db, devices={"box", "pc", "mac"}, clock=lambda: clocks.wall,
                               uptime=clocks.uptime)
    store.beat("device-probe", "mac", pid=101, interval=health.PROBE_SECONDS)
    hang = health.DOWN_BEATS * health.PROBE_SECONDS + 1
    clocks.advance(wall=hang, monotonic=hang)
    store.beat("device-probe", "pc", pid=202, interval=health.PROBE_SECONDS)
    assert [r.device for r in health.read_heartbeats(db)] == ["pc"]


def test_a_monotonic_stamp_ahead_of_the_clock_ages_by_the_wall_clock() -> None:
    row = health.Heartbeat("poller", "box", "", 101, NOW, NOW, "unknown", 15.0, None, NOW,
                           seen_boot=BOOT, seen_monotonic=9_000.0)
    assert health.classify(row, NOW + SUSPEND, health.Uptime(BOOT, 10.0))[0] == "down"


def test_a_failed_probe_after_a_suspend_ages_from_the_last_reach(tmp_path: Path) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    beat(db, clocks, "device-probe", "pc", health.PROBE_SECONDS)
    clocks.advance(wall=SUSPEND, monotonic=2.0)
    health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                       uptime=clocks.uptime).checked(
        "device-probe", "pc", pid=202, interval=health.PROBE_SECONDS)
    assert state(db, clocks, "device-probe", "pc") == ("stale", "unreachable, last reached 2s ago")


def test_a_newer_process_check_keeps_the_newest_reach_monotonic_stamp(tmp_path: Path) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    beat(db, clocks, "device-probe", "pc", health.PROBE_SECONDS)
    clocks.advance(wall=30.0, monotonic=30.0)
    health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                       uptime=clocks.uptime).beat(
        "device-probe", "pc", pid=202, interval=health.PROBE_SECONDS)
    clocks.advance(wall=SUSPEND, monotonic=2.0)
    assert state(db, clocks, "device-probe", "pc") == ("up", "")


def test_a_fixed_test_clock_has_no_monotonic_partner() -> None:
    assert health.read_uptime(lambda: NOW) is None


@pytest.mark.skipif(not health.BOOT_ID.is_file(), reason="no Linux boot id")
def test_beats_from_the_real_clocks_record_this_boot(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    health.HealthStore(db, devices=DEVICES).beat("poller", "box", pid=101)
    (row,) = health.read_heartbeats(db)
    uptime = health.read_uptime()
    assert uptime is not None
    assert row.seen_boot == uptime.boot
    assert 0 <= uptime.monotonic - row.seen_monotonic < 5
    assert abs(row.last_seen - time.time()) < 5


CHECK_CASES = [
    ("tunnel", "box", health.BEAT_SECONDS),
    ("device-probe", "pc", health.PROBE_SECONDS),
    ("dashboard-push", "box", 60.0),
]


def reboot_with_wall_behind(clocks: Clocks) -> None:
    clocks.boot = REBOOT
    clocks.wall -= 60.0
    clocks.monotonic = 10.0


@pytest.mark.parametrize(("component", "device", "interval"), CHECK_CASES)
def test_a_current_boot_check_outranks_a_previous_boot_row_ahead_on_the_wall(
        tmp_path: Path, component: str, device: str, interval: float) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    store = health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                               uptime=clocks.uptime)
    store.beat(component, device, pid=101, interval=interval)
    reboot_with_wall_behind(clocks)
    store.beat(component, device, pid=202, interval=interval)
    report = health.build_report(health.read_heartbeats(db), clocks.wall,
                                 uptime=clocks.uptime())
    (item,) = [c for c in report.components if c.component == component]
    assert (item.state, item.reason, item.pid) == ("up", "", 202)


def test_a_current_boot_tunnel_check_requests_no_restart(tmp_path: Path) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    store = health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                               uptime=clocks.uptime)
    store.beat("tunnel", "box", pid=101, interval=health.BEAT_SECONDS)
    reboot_with_wall_behind(clocks)
    store.beat("tunnel", "box", pid=202, interval=health.BEAT_SECONDS)
    expected = health.Expected((health.Slot("tunnel", ("box",)),), frozenset({"tunnel"}))
    report = health.build_report(health.read_heartbeats(db), clocks.wall,
                                 uptime=clocks.uptime(), expected=expected)
    actions = heal.decide(health.report_json(report, clocks.wall), {},
                          heal.Facts("box", frozenset({"linear-webhook"})), clocks.wall)
    assert actions == []


@pytest.mark.parametrize(("component", "device", "interval"), CHECK_CASES)
def test_a_failed_current_boot_check_leaves_a_previous_boot_success_down(
        tmp_path: Path, component: str, device: str, interval: float) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    store = health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                               uptime=clocks.uptime)
    store.beat(component, device, pid=101, interval=interval)
    reboot_with_wall_behind(clocks)
    store.checked(component, device, pid=202, interval=interval)
    assert state(db, clocks, component, device)[0] == "down"


@pytest.mark.parametrize(("component", "device", "interval"), CHECK_CASES)
def test_a_failed_check_on_a_pid_reused_from_an_earlier_boot_stays_visible(
        tmp_path: Path, component: str, device: str, interval: float) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    store = health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                               uptime=clocks.uptime)
    store.beat(component, device, pid=303, interval=interval)
    clocks.boot = REBOOT
    clocks.wall += 30.0
    clocks.monotonic = 10.0
    store.beat(component, device, pid=202, interval=interval)
    clocks.advance(wall=1.0, monotonic=1.0)
    store.checked(component, device, pid=303, interval=interval)
    report = health.build_report(health.read_heartbeats(db), clocks.wall,
                                 uptime=clocks.uptime())
    (item,) = [c for c in report.components if c.component == component]
    assert (item.state, item.pid) == ("stale", 303)
    assert item.reason.endswith(" 1s ago")


def without_checked_boot(db: Path) -> None:
    with sqlite3.connect(health.health_path(db)) as connection:
        connection.execute(f"ALTER TABLE {health.TABLE} DROP COLUMN checked_boot")


@pytest.mark.parametrize(("component", "device", "interval"), CHECK_CASES)
def test_a_previous_release_failed_check_after_its_current_boot_success_stays_visible(
        tmp_path: Path, component: str, device: str, interval: float) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    store = health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                               uptime=clocks.uptime)
    store.beat(component, device, pid=101, interval=interval)
    clocks.advance(wall=1.0, monotonic=1.0)
    store.checked(component, device, pid=202, interval=interval)
    without_checked_boot(db)
    report = health.build_report(health.read_heartbeats(db), clocks.wall,
                                 uptime=clocks.uptime())
    (item,) = [c for c in report.components if c.component == component]
    assert (item.state, item.pid) == ("stale", 202)
    assert item.reason.endswith(" 1s ago")


@pytest.mark.parametrize("written_before_upgrade", [False, True])
@pytest.mark.parametrize(("component", "device", "interval"), CHECK_CASES)
def test_a_current_boot_success_outranks_an_earlier_boot_failure_ahead_on_the_wall(
        tmp_path: Path, component: str, device: str, interval: float,
        written_before_upgrade: bool) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    store = health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                               uptime=clocks.uptime)
    store.beat(component, device, pid=303, interval=interval)
    clocks.boot = REBOOT
    clocks.wall += 30.0
    clocks.monotonic = 10.0
    store.checked(component, device, pid=303, interval=interval)
    if written_before_upgrade:
        without_checked_boot(db)
        store = health.HealthStore(db, devices=DEVICES, clock=lambda: clocks.wall,
                                   uptime=clocks.uptime)
    clocks.boot = THIRD_BOOT
    clocks.wall = NOW - 30.0
    clocks.monotonic = 10.0
    store.beat(component, device, pid=202, interval=interval)
    report = health.build_report(health.read_heartbeats(db), clocks.wall,
                                 uptime=clocks.uptime())
    (item,) = [c for c in report.components if c.component == component]
    assert (item.state, item.reason, item.pid) == ("up", "", 202)
