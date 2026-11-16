from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import health
from lib.cli import StopAtBoundary, _AfterPoll
from lib.dashboard_push import ROOT, PollPusher, PushError, SshTransport
from lib.dispatch.cycle_clock import CycleClock
from lib.dispatch.poll import PollLoop, sleep_on_either_clock
from tests.test_health import make_db, machines_file

INTERVAL = 0.5
SLACK = 1.5


class Dispatcher:
    def __init__(self) -> None:
        self.cycle_clock: CycleClock | None = None

    def poll_once(self, source: object) -> None:
        with self.cycle_clock.phase("feed"):
            pass


def _stalling_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids = tmp_path / "ssh.pids"
    ssh = bin_dir / "ssh"
    ssh.write_text(
        f"#!/bin/sh\n[ -e {pids}.released ] && exit 255\necho $$ >> {pids}\nexec sleep 600\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return pids


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not (path.exists() and path.read_text().strip()):
        assert time.monotonic() < deadline, "the stalling ssh never ran"
        time.sleep(0.05)


def _kill_all(path: Path) -> None:
    Path(f"{path}.released").touch()
    if not path.exists():
        return
    for pid in path.read_text().split():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _cycle_starts(after_poll=None, before=None) -> list[float]:
    starts: list[float] = []
    clock = CycleClock()
    start = clock.start

    def stamped() -> str:
        starts.append(time.monotonic())
        if before is not None and len(starts) == 1:
            before()
        return start()

    clock.start = stamped
    loop = PollLoop(Dispatcher(), object(), interval_seconds=INTERVAL)
    loop.clock = clock
    loop.run(max_polls=4, after_poll=after_poll)
    return starts


def _assert_on_time(starts: list[float]) -> None:
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    assert len(gaps) == 3
    assert all(gap <= INTERVAL + SLACK for gap in gaps), gaps


def test_a_hanging_dashboard_push_does_not_delay_the_next_cycle(tmp_path, monkeypatch) -> None:
    pids = _stalling_ssh(tmp_path, monkeypatch)
    lines: list[str] = []
    pusher = PollPusher(["ssh", "pc-wsl-cursor-bot", "true"], dict(os.environ), 0,
                        log=lines.append)
    try:
        starts = _cycle_starts(_AfterPoll(lambda: None, pusher))
        _wait_for(pids)
    finally:
        _kill_all(pids)
    _assert_on_time(starts)
    assert pusher._child is not None


def test_a_hanging_device_probe_does_not_delay_the_next_cycle(tmp_path, monkeypatch) -> None:
    pids = _stalling_ssh(tmp_path, monkeypatch)
    db, _ = make_db(tmp_path)
    beats = health.poller_beats(db, machines_file(tmp_path), interval=3600.0,
                                log=lambda line: None)
    try:
        starts = _cycle_starts(before=lambda: (beats.start(), _wait_for(pids)))
    finally:
        _kill_all(pids)
        beats.stop()
    _assert_on_time(starts)


def hanging_scp(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids = tmp_path / "copy.pids"
    (bin_dir / "ssh").write_text(f"#!/bin/sh\necho $$ >> {pids}\nexec sleep 600\n")
    (bin_dir / "scp").write_text(
        f'#!/bin/sh\necho $$ >> {pids}\n"$(dirname "$0")/ssh" "$@" &\nwait\n'
    )
    for name in ("ssh", "scp"):
        (bin_dir / name).chmod(0o755)
    return bin_dir, pids


def running(pids: Path) -> list[int]:
    alive = []
    for pid in map(int, pids.read_text().split()):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            continue
        if state not in ("Z", "X"):
            alive.append(pid)
    return alive


def test_a_timed_out_upload_leaves_no_scp_or_ssh_running(tmp_path, monkeypatch) -> None:
    bin_dir, pids = hanging_scp(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    page = tmp_path / "status.json"
    page.write_text("{}")
    transport = SshTransport("pc-wsl-cursor-bot", "/srv/d", method="scp", timeout=1)
    try:
        with pytest.raises(PushError, match="scp to pc-wsl-cursor-bot timed out after 1s"):
            transport.send([page])
        assert len(pids.read_text().split()) == 2
        assert running(pids) == []
    finally:
        for pid in running(pids):
            os.kill(pid, signal.SIGKILL)


def test_a_poll_cycle_push_that_times_out_keeps_cycles_on_time_and_leaves_no_ssh(
    tmp_path,
) -> None:
    bin_dir, pids = hanging_scp(tmp_path)
    page = tmp_path / "status.json"
    page.write_text("{}")
    push = (
        "import sys\n"
        "from pathlib import Path\n"
        "from lib.dashboard_push import SshTransport, run_push\n"
        "transport = SshTransport('pc-wsl-cursor-bot', '/srv/d', method='scp', timeout=2)\n"
        f"sys.exit(run_push(transport=transport, snapshot=lambda: [Path({str(page)!r})],"
        " interval=30, once=True, sleep=None, log=lambda line: print(line, file=sys.stderr)))\n"
    )
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(ROOT),
    }
    children: list[subprocess.Popen] = []

    def spawn(*args, **kwargs) -> subprocess.Popen:
        children.append(subprocess.Popen(*args, **kwargs))
        return children[-1]

    pusher = PollPusher([sys.executable, "-c", push], env, 0, spawn=spawn, log=print)
    starts: list[float] = []
    clock = CycleClock()
    start = clock.start

    def stamped() -> str:
        starts.append(time.monotonic())
        return start()

    clock.start = stamped
    loop = PollLoop(Dispatcher(), object(), interval_seconds=1.0)
    loop.clock = clock
    try:
        loop.run(max_polls=5, after_poll=_AfterPoll(lambda: None, pusher))
        codes = [child.wait(timeout=30) for child in children]
    finally:
        for pid in running(pids) if pids.exists() else []:
            os.kill(pid, signal.SIGKILL)
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    assert len(gaps) == 4 and all(gap <= 1.0 + SLACK for gap in gaps), gaps
    assert codes and set(codes) == {1}
    assert len(pids.read_text().split()) == 2 * len(children)
    assert running(pids) == []


class PausedClock:
    def __init__(self) -> None:
        self.wall = 1_700_000_000.0
        self.mono = 100.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.mono += seconds
        self.wall += seconds + (450.0 if len(self.slept) == 1 else 0.0)


def test_a_paused_sleep_starts_the_next_cycle_at_once_and_says_where_the_time_went(capsys) -> None:
    clock = PausedClock()
    loop = PollLoop(
        Dispatcher(), object(), interval_seconds=30,
        sleep=lambda seconds: sleep_on_either_clock(
            seconds, wall=lambda: clock.wall, monotonic=lambda: clock.mono, sleep=clock.sleep
        ),
    )
    loop.clock = CycleClock(wall=lambda: clock.wall, monotonic=lambda: clock.mono)

    loop.run(max_polls=2)

    assert clock.slept == [1.0]
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(":")[1].split()[0] for line in lines] == [
        "cycle", "cycle", "between", "cycle", "cycle",
    ]
    assert lines[2] == (
        "orch poll: between cycles 451.0s: sleep 451.0s "
        "(asked 30s, monotonic 1.0s, clock jumped 450.0s)"
    )


def test_an_on_time_gap_logs_no_between_cycles_line(capsys) -> None:
    clock = PausedClock()
    clock.sleep = lambda seconds: (setattr(clock, "mono", clock.mono + seconds),
                                   setattr(clock, "wall", clock.wall + seconds))
    loop = PollLoop(
        Dispatcher(), object(), interval_seconds=30,
        sleep=lambda seconds: sleep_on_either_clock(
            seconds, wall=lambda: clock.wall, monotonic=lambda: clock.mono, sleep=clock.sleep
        ),
    )
    loop.clock = CycleClock(wall=lambda: clock.wall, monotonic=lambda: clock.mono)

    loop.run(max_polls=3)

    assert "between cycles" not in capsys.readouterr().out


def test_a_slow_sleep_hidden_by_a_wall_clock_stepped_back_is_still_named(capsys) -> None:
    clock = PausedClock()

    def delayed(seconds: float) -> None:
        clock.slept.append(seconds)
        clock.mono += 45.0
        clock.wall += 25.0

    loop = PollLoop(
        Dispatcher(), object(), interval_seconds=1,
        sleep=lambda seconds: sleep_on_either_clock(
            seconds, wall=lambda: clock.wall, monotonic=lambda: clock.mono, sleep=delayed
        ),
    )
    loop.clock = CycleClock(wall=lambda: clock.wall, monotonic=lambda: clock.mono)

    loop.run(max_polls=2)

    assert clock.slept == [1.0]
    assert capsys.readouterr().out.splitlines()[2] == (
        "orch poll: between cycles 45.0s: sleep 45.0s "
        "(asked 1s, monotonic 45.0s, clock stepped back 20.0s)"
    )


def test_a_wall_clock_stepped_back_still_ends_the_sleep_on_the_monotonic_clock() -> None:
    mono = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        mono[0] += seconds

    sleep_on_either_clock(3, wall=lambda: 5_000.0 - mono[0] * 100, monotonic=lambda: mono[0], sleep=sleep)

    assert slept == [1.0, 1.0, 1.0]


def test_a_slow_after_poll_step_is_named_on_the_cycle_end_line(capsys) -> None:
    now = [1_700_000_000.0]
    loop = PollLoop(Dispatcher(), object(), interval_seconds=30, sleep=lambda _: None)
    loop.clock = CycleClock(wall=lambda: now[0], monotonic=lambda: now[0])

    def slow_push() -> None:
        now[0] += 12.3

    loop.run(max_polls=1, after_poll=_AfterPoll(lambda: None, slow_push, lambda: None))

    end = capsys.readouterr().out.splitlines()[-1]
    assert end.endswith(
        "after-poll 12.3s [backup 0.0s, dashboard-push 12.3s, self-deploy 0.0s]"
    )


def test_a_term_during_a_sliced_sleep_exits_at_once() -> None:
    def sleep(seconds: float) -> None:
        stopper.on_term(signal.SIGTERM, None)

    stopper = StopAtBoundary(sleep=lambda seconds: sleep_on_either_clock(seconds, sleep=sleep))

    with pytest.raises(SystemExit) as stopped:
        stopper.sleep(30)

    assert stopped.value.code == 143
