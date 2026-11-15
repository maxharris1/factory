"""A slow poll cycle shows as "poller slow", and nothing else goes down with it (#440).

Fakes only: a temp health file beside a temp orch.db, a fake clock, a fake
push spawn, a fake SSH transport and a stub for the poll loop's cycle state.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest

from lib import health
from lib.cli import main as cli_main
from lib.dashboard_push import PollPusher
from lib.dispatcher import PollLoop
from lib.status import main as status_main
from tests.test_health import (
    MACHINES,
    NOW,
    ONLY_CODEX,
    ORCH_EXAMPLE,
    FakeTransport,
    machines_config,
    machines_file,
    make_db,
    row,
    store,
)


def poller(**overrides) -> health.Heartbeat:
    return row(component="poller", bot="", pid=101, **overrides)


def only(report: health.HealthReport, component: str) -> health.Component:
    (found,) = [c for c in report.components if c.component == component]
    return found


def test_a_poller_beating_through_a_412s_cycle_is_slow() -> None:
    report = health.build_report([poller(cycle_started_at=NOW - 412)], NOW)
    component = only(report, "poller")
    assert (component.state, component.reason) == ("stale", "poller slow (cycle 412s)")
    assert component.cycle_seconds == 412
    assert report.down == ()


def test_a_poller_200s_into_a_cycle_is_up() -> None:
    report = health.build_report([poller(cycle_started_at=NOW - 200)], NOW)
    assert (only(report, "poller").state, only(report, "poller").reason) == ("up", "")


def test_a_poller_whose_beats_stop_is_down_by_age_not_slow() -> None:
    report = health.build_report(
        [poller(last_seen=NOW - 600, cycle_started_at=NOW - 900)], NOW)
    assert only(report, "poller").state == "down"
    assert only(report, "poller").reason == "no heartbeat for 10m"


def test_only_the_poller_can_be_slow() -> None:
    report = health.build_report([row(cycle_started_at=NOW - 900)], NOW)
    assert only(report, "waiter").state == "up"


def test_orch_status_shows_the_last_cycle_and_a_slow_cycle(tmp_path: Path, monkeypatch) -> None:
    from tests.test_wait import dispatcher_for

    monkeypatch.setattr(health, "ROOT", tmp_path / "no-checkout")
    db, _ = make_db(tmp_path)
    dispatcher_for(db)
    machines = machines_file(tmp_path, ONLY_CODEX)
    args = ["--db", str(db), "--machines", str(machines), "--orch", str(ORCH_EXAMPLE)]

    store(db, NOW - 5).beat("poller", "box", pid=101, version="unknown",
                            last_cycle_seconds=38.4)
    out = io.StringIO()
    status_main(args, stdout=out, clock=lambda: NOW)
    assert "  poller @ box up (last cycle 38s), last seen " in out.getvalue()

    store(db, NOW - 5).beat("poller", "box", pid=101, version="unknown",
                            cycle_started_at=NOW - 412, last_cycle_seconds=38.4)
    out = io.StringIO()
    status_main(args, stdout=out, clock=lambda: NOW)
    assert ("  poller @ box stale (poller slow (cycle 412s), last cycle 38s), last seen "
            in out.getvalue())


def test_orch_health_and_its_json_show_the_slow_poller(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(health, "measure_storage", lambda *_args: ())
    db, _ = make_db(tmp_path)
    now = time.time()
    store(db, now).beat("poller", "box", pid=101, version="unknown",
                        cycle_started_at=now - 412, last_cycle_seconds=38)
    base = ["health", "--db", str(db), "--machines", str(machines_file(tmp_path, ONLY_CODEX))]
    cli_main(base)
    assert "poller @ box stale (poller slow (cycle 412s), last cycle 38s)" in capsys.readouterr().out
    cli_main([*base, "--json"])
    (entry,) = [c for c in json.loads(capsys.readouterr().out)["components"]
                if c["component"] == "poller"]
    assert entry["reason"] == "poller slow (cycle 412s)"
    assert entry["last_cycle_seconds"] == 38
    assert 412 <= entry["cycle_seconds"] < 420


def test_an_old_health_file_gains_the_cycle_columns_and_keeps_its_rows(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    path = health.health_path(db)
    with sqlite3.connect(path) as old:
        old.execute(
            f"CREATE TABLE {health.TABLE} (component TEXT NOT NULL, device TEXT NOT NULL, "
            "bot TEXT NOT NULL DEFAULT '', pid INTEGER, started_at REAL, last_seen REAL, "
            "version TEXT NOT NULL DEFAULT 'unknown', interval_seconds REAL NOT NULL, "
            "stopped_at REAL, checked_at REAL, PRIMARY KEY (component, device, bot, pid))")
        old.execute(f"INSERT INTO {health.TABLE} VALUES "
                    "('waiter', 'box', 'codex-bot', 7, ?, ?, 'unknown', 15.0, NULL, NULL)",
                    (NOW - 60, NOW - 5))
    old.close()

    (before,) = health.read_heartbeats(db)
    assert (before.component, before.cycle_started_at, before.last_cycle_seconds) == (
        "waiter", None, None)

    store(db, NOW).beat("poller", "box", pid=101, version="unknown",
                        cycle_started_at=NOW - 10, last_cycle_seconds=38)
    rows = {r.component: r for r in health.read_heartbeats(db)}
    assert rows["waiter"].last_seen == NOW - 5
    assert (rows["poller"].cycle_started_at, rows["poller"].last_cycle_seconds) == (NOW - 10, 38)
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    check.close()


def _beat_under_open_read(db: Path) -> None:
    beats = store(db)
    beats.beat("poller", "box", pid=101, version="unknown")
    reader = sqlite3.connect(health.health_path(db), isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute(f"SELECT * FROM {health.TABLE}").fetchall()
        beats.beat("waiter", "box", bot="codex-bot", pid=102, version="unknown")
    finally:
        reader.close()


def test_a_beat_lands_while_a_reader_holds_a_read_transaction(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    _beat_under_open_read(db)
    assert {r.component for r in health.read_heartbeats(db)} == {"poller", "waiter"}


def test_without_wal_the_same_beat_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(health.HealthStore, "_prepare",
                        lambda self, connection: connection.execute(health._SCHEMA))
    db, _ = make_db(tmp_path)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        _beat_under_open_read(db)


def test_the_wal_files_share_the_health_files_mode_and_group(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    os.chmod(db, 0o660)
    beats = store(db)
    connection = beats._connect()
    try:
        main = health.health_path(db).stat()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{health.health_path(db)}{suffix}").stat()
            assert stat.S_IMODE(sidecar.st_mode) == stat.S_IMODE(main.st_mode) == 0o660
            assert sidecar.st_gid == main.st_gid
    finally:
        connection.close()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class PushChild:
    """A push child that has already finished: it wrote the writer's heartbeat."""

    def poll(self) -> int:
        return 0


class CycleStub:
    """What `PollLoop.cycle_state` reports, for a cycle that blocks until released."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.started_at: float | None = None
        self.release = threading.Event()
        self.entered = threading.Event()

    def cycle_state(self) -> tuple[float | None, float | None]:
        return self.started_at, None

    def poll_once(self, _source) -> None:
        self.started_at = self.clock()
        self.entered.set()
        assert self.release.wait(30), "the test released the cycle"


def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.005)


def test_a_six_minute_cycle_leaves_the_pushes_and_other_rows_up(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    machines = machines_config(tmp_path, MACHINES)
    clock = Clock(NOW)
    beats = health.HealthStore(db, devices=health.known_devices(machines), clock=clock)
    every = 30.0
    pushes: list[float] = []

    def spawn(argv, **_kwargs) -> PushChild:
        pushes.append(clock.now)
        beats.beat("dashboard", "box", pid=201, version="unknown", interval=every)
        return PushChild()

    pusher = PollPusher(["orch", "dashboard", "push", "--once"], {}, 0, spawn=spawn,
                        clock=clock, log=lambda line: None,
                        record=lambda ok: beats.beat("dashboard-push", "box", pid=101,
                                                     version="unknown", interval=every))
    cycle = CycleStub(clock)
    poller_beat = health.Beater(db, "poller", devices=["box"], store=beats, pid=101,
                                version="unknown", cycle=cycle.cycle_state, log=lambda line: None)
    waiter_beat = health.Beater(db, "waiter", devices=["box"], bot="codex-bot", store=beats,
                                pid=102, version="unknown", log=lambda line: None)
    prober = health.DeviceProber(beats, health.probe_targets(machines), FakeTransport({}),
                                 pid=101, version="unknown", log=lambda line: None)
    timer = health._Loop("test-push-timer", 0.005, lambda: pusher.on_timer(every),
                         block_signals=False)

    loop = PollLoop(cycle, object(), interval_seconds=1)
    polling = threading.Thread(target=lambda: loop.run(max_polls=1, after_poll=pusher))
    polling.start()
    timer.start()
    try:
        assert cycle.entered.wait(10)
        for _ in range(12):
            clock.now += every
            seen = len(pushes)
            _until(lambda: len(pushes) > seen, f"a push started by {clock.now - NOW:g}s")
            poller_beat.beat_now()
            waiter_beat.beat_now()
            prober.probe_once()
        report = health.build_report(health.read_heartbeats(db), clock.now)
    finally:
        cycle.release.set()
        polling.join(10)
        timer.stop()

    assert len(pushes) >= 12
    states = {c.component: (c.state, c.reason) for c in report.components}
    assert states == {
        "poller": ("stale", "poller slow (cycle 360s)"),
        "waiter": ("up", ""),
        "dashboard": ("up", ""),
        "dashboard-push": ("up", ""),
        "device-probe": ("up", ""),
    }


def test_the_push_timer_starts_no_push_within_the_interval_of_the_last() -> None:
    clock = Clock(NOW)
    started: list[float] = []

    def spawn(argv, **_kwargs) -> PushChild:
        started.append(clock.now)
        return PushChild()

    pusher = PollPusher(["orch"], {}, 0, spawn=spawn, clock=clock, log=lambda line: None)
    pusher()
    clock.now += 10
    pusher.on_timer(30)
    clock.now += 25
    pusher.on_timer(30)
    assert started == [NOW, NOW + 35]
