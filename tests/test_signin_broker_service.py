from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import reap_home, sandbox_env
from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "signin-broker-service.sh"
STATE = Path(".local/state/factory")

linux = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="finds the lock holder through /proc"
)
needs_flock = pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")

FAKE_ORCH = """#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path

args = sys.argv[1:]
with open(Path(os.environ["HOME"]) / "broker-starts", "a") as starts:
    starts.write(json.dumps([os.getpid(), args]) + "\\n")
assert args == ["accounts", "signin-broker", "--state", os.environ["ORCH_STATE_DIR"]], args
if os.environ.get("FAKE_BROKER_FAIL"):
    raise SystemExit(1)
if os.environ.get("FAKE_BROKER_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.2)
"""


@pytest.fixture
def home(tmp_path: Path):
    home = tmp_path / "home"
    orch = home / ".local/bin/orch"
    orch.parent.mkdir(parents=True)
    orch.write_text(FAKE_ORCH, encoding="utf-8")
    orch.chmod(0o755)
    (home / ".config/factory").mkdir(parents=True)
    try:
        yield home
    finally:
        try:
            for state in {home / STATE, *(Path(args[3]) for _, args in _starts(home))}:
                _service(home, "stop", ORCH_STATE_DIR=str(state))
        finally:
            for pid in _pids(home):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            reap_home(home)


def _service(home: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["bash", str(SERVICE), *args],
        env=sandbox_env(home, **{
            "PATH": "/usr/bin:/bin",
            "ORCH_SIGNIN_BROKER_START_WAIT": "10",
            "ORCH_SIGNIN_BROKER_STOP_WAIT": "10",
            **extra,
        }),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    started = re.search(r"started sign-in broker \(pid (\d+)\)", result.stdout)
    if started:
        _wait_for(lambda: int(started[1]) in _pids(home))
    return result


def _starts(home: Path) -> list[tuple[int, list[str]]]:
    path = home / "broker-starts"
    if not path.exists():
        return []
    return [tuple(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def _pids(home: Path) -> list[int]:
    return [pid for pid, _ in _starts(home)]


def _live(home: Path) -> list[int]:
    return [pid for pid in _pids(home) if alive(pid)]


def _wait_for(predicate, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_start_runs_one_broker_for_the_state_dir(home: Path) -> None:
    started = _service(home, "start")
    assert started.returncode == 0, started.stdout + started.stderr
    [(pid, args)] = _starts(home)
    assert args == ["accounts", "signin-broker", "--state", str(home / STATE)]
    assert alive(pid)
    assert (home / STATE / "signin-broker.pid").read_text().strip() == str(pid)
    status = _service(home, "status")
    assert status.returncode == 0
    assert status.stdout.strip() == f"running (pid {pid})"

    again = _service(home, "start")
    assert again.returncode == 0, again.stderr
    assert "already running" in again.stdout
    assert _service(home, "ensure").returncode == 0
    time.sleep(0.5)
    assert _pids(home) == [pid]


@needs_flock
def test_concurrent_ensures_start_exactly_one_broker(home: Path) -> None:
    env = sandbox_env(home, PATH="/usr/bin:/bin", ORCH_SIGNIN_BROKER_START_WAIT="10")
    racers = [
        subprocess.Popen(["bash", str(SERVICE), "ensure"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(6)
    ]
    assert [racer.wait(timeout=60) for racer in racers] == [0] * 6
    time.sleep(0.5)
    assert len(_pids(home)) == 1
    assert len(_live(home)) == 1


@linux
@needs_flock
def test_a_lost_pidfile_still_finds_the_broker_through_its_lock(home: Path) -> None:
    assert _service(home, "start").returncode == 0
    [pid] = _pids(home)
    (home / STATE / "signin-broker.pid").unlink()

    status = _service(home, "status")
    assert status.returncode == 0
    assert status.stdout.strip() == f"running (pid {pid})"
    for command in ("ensure", "start"):
        assert _service(home, command).returncode == 0
    time.sleep(0.5)
    assert _pids(home) == [pid]

    assert _service(home, "stop").returncode == 0
    assert not alive(pid)


def test_another_state_dir_runs_its_own_broker(home: Path) -> None:
    other = home / "other-state"
    assert _service(home, "start").returncode == 0
    assert _service(home, "start", ORCH_STATE_DIR=str(other)).returncode == 0
    assert sorted(args[3] for _, args in _starts(home)) == sorted([str(home / STATE), str(other)])
    assert len(_live(home)) == 2

    assert _service(home, "stop", ORCH_STATE_DIR=str(other)).returncode == 0
    assert len(_live(home)) == 1
    assert _service(home, "status").returncode == 0


def test_stop_ends_the_broker_and_holds_it_until_start(home: Path) -> None:
    assert _service(home, "start").returncode == 0
    [pid] = _pids(home)

    stopped = _service(home, "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert not alive(pid)
    assert not (home / STATE / "signin-broker.pid").exists()
    assert (home / STATE / "signin-broker.stopped").exists()
    status = _service(home, "status")
    assert status.returncode == 3
    assert status.stdout.startswith("stopped (held by ")

    assert _service(home, "ensure").returncode == 0
    time.sleep(0.5)
    assert _pids(home) == [pid]

    assert _service(home, "start").returncode == 0
    assert not (home / STATE / "signin-broker.stopped").exists()
    assert len(_pids(home)) == 2
    assert _live(home) == [_pids(home)[1]]


def test_status_and_stop_when_nothing_runs(home: Path) -> None:
    status = _service(home, "status")
    assert (status.returncode, status.stdout.strip()) == (3, "not running")
    stopped = _service(home, "stop")
    assert stopped.returncode == 0
    assert "not running" in stopped.stdout


def test_restart_replaces_the_broker(home: Path) -> None:
    assert _service(home, "start").returncode == 0
    [old] = _pids(home)

    restarted = _service(home, "restart")
    assert restarted.returncode == 0, restarted.stderr
    assert not alive(old)
    assert len(_pids(home)) == 2
    new = _pids(home)[1]
    assert _live(home) == [new]
    assert _service(home, "status").stdout.strip() == f"running (pid {new})"
    assert not (home / STATE / "signin-broker.stopped").exists()


@pytest.mark.parametrize("held_before", [False, True], ids=["no-hold", "operator-hold"])
def test_a_restart_whose_stop_times_out_keeps_only_an_earlier_hold(
    home: Path, held_before: bool
) -> None:
    assert _service(home, "start", FAKE_BROKER_IGNORE_TERM="1").returncode == 0
    [pid] = _pids(home)
    hold = home / STATE / "signin-broker.stopped"
    if held_before:
        hold.touch()

    restarted = _service(home, "restart", ORCH_SIGNIN_BROKER_STOP_WAIT="1")
    assert restarted.returncode == 1
    assert "did not stop" in restarted.stderr
    assert alive(pid)
    assert hold.exists() is held_before
    assert _pids(home) == [pid]
    os.kill(pid, signal.SIGKILL)
    _wait_for(lambda: not alive(pid))


def test_a_broker_that_exits_at_startup_fails_start(home: Path) -> None:
    started = _service(home, "start", FAKE_BROKER_FAIL="1")
    assert started.returncode == 1
    assert "exited at startup" in started.stderr
    assert str(home / STATE / "signin-broker.log") in started.stderr
    assert len(_pids(home)) == 1
    assert _service(home, "status").returncode == 3


def test_a_retired_host_starts_nothing(home: Path) -> None:
    (home / ".config/factory/orch-retired").write_text("moved\n", encoding="utf-8")
    for command in ("start", "ensure", "restart"):
        result = _service(home, command)
        assert result.returncode == 79, command
    assert _pids(home) == []


def test_install_and_remove_autostart_toggle_the_marker(home: Path) -> None:
    marker = home / ".config/factory/signin-broker-autostart"
    installed = _service(home, "install-autostart")
    assert installed.returncode == 0, installed.stderr
    assert marker.is_file()
    assert str(marker) in installed.stdout
    assert _service(home, "install-autostart").returncode == 0

    removed = _service(home, "remove-autostart")
    assert removed.returncode == 0
    assert not marker.exists()
    assert _pids(home) == []


def test_an_unknown_command_prints_usage(home: Path) -> None:
    result = _service(home, "bogus")
    assert result.returncode == 2
    assert "usage:" in result.stderr
