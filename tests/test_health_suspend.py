"""A heartbeat's age leaves out time the box was suspended (#544)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from lib import health

NOW = 1_800_000_000.0
BOOT = "8cd10924-c547-44ff-9fc4-1decdc2c833e"
REBOOT = "0f0e0d0c-0b0a-4908-8706-050403020100"
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


def test_after_a_reboot_missing_beats_age_by_the_wall_clock(tmp_path: Path) -> None:
    clocks = Clocks()
    db = tmp_path / "orch.db"
    beat(db, clocks, "poller", "box", health.BEAT_SECONDS)
    clocks.boot = REBOOT
    clocks.wall += SUSPEND
    clocks.monotonic = clocks.monotonic + 2.0
    assert state(db, clocks, "poller", "box")[0] == "down"


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
