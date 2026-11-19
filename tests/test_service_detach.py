from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests import test_linear_webhook_service as linear
from tests import test_self_heal_sh as self_heal
from tests import test_waiter_service as waiter
from tests.fake_path import link_path_without
from tests.procs import alive
from tests.test_runner_pool_sync import sync_loops

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
STATE = Path(".local/state/factory")

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="reads sessions and fds from /proc"
)

FAKE_POLLER = f"""#!{sys.executable}
import fcntl, os, signal, sys, time
from pathlib import Path

args = sys.argv[1:]
assert args[:2] == ["poll", "--pidfile"], args
pidfile = Path(args[2])
with open(Path(os.environ["HOME"]) / "poller-starts", "a") as starts:
    starts.write(f"{{os.getpid()}}\\n")
lock = open(pidfile.with_name("orch.db.lock"), "a")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)

def leave(*_):
    pidfile.unlink(missing_ok=True)
    raise SystemExit(143)

signal.signal(signal.SIGTERM, leave)
pidfile.write_text(f"{{os.getpid()}}\\n")
while True:
    time.sleep(1)
"""

FAKE_GH = """#!/bin/sh
case "$*" in
  *actions/runners*) echo '4 1' ;;
  *) echo '["self-hosted","Linux","X64","factory-linux"]' ;;
esac
"""


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _locked(path: Path) -> bool:
    return subprocess.run(["flock", "-n", str(path), "true"]).returncode != 0


def _wait_for(predicate, what: str, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while not (value := predicate()):
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)
    return value


@dataclass
class Service:
    argv: list[str]
    env: dict[str, str]
    home: Path
    start: str = "start"
    pidfiles: tuple[str, ...] = ()
    autostart: Path | None = None

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*self.argv, *args], env=self.env, text=True,
                              capture_output=True, timeout=60, check=False)

    def pids(self) -> list[int]:
        if not self.pidfiles:
            return sync_loops(self.home)
        found = self._listed()
        return found if len(found) == len(self.pidfiles) and all(map(alive, found)) else []

    def _listed(self) -> list[int]:
        found = []
        for name in self.pidfiles:
            try:
                found.append(int((self.home / STATE / name).read_text().split()[0]))
            except (OSError, ValueError, IndexError):
                pass
        return found

    def status_ok(self) -> bool:
        if self.pidfiles:
            return self.run("status").returncode == 0
        return _locked(self.home / STATE / "runner-pool-sync.lock")

    def stop(self) -> None:
        if self.autostart:
            self.autostart.unlink(missing_ok=True)
        if self.pidfiles:
            self.run("stop")
            for pid in self._listed():
                if alive(pid):
                    os.kill(pid, signal.SIGTERM)
            return
        for pid in self.pids():
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                os.kill(pid, signal.SIGKILL)


def _poller(home: Path) -> Service:
    config = home / ".config/factory"
    config.mkdir(parents=True)
    (config / "webhook.env").write_text("")
    (config / "orch-poll.env").write_text("")
    (home / STATE).mkdir(parents=True)
    (home / STATE / "orch.db").write_bytes(b"")
    _executable(home / ".local/bin/orch", FAKE_POLLER)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "ORCH_POLL_START_WAIT": "10",
           "ORCH_POLL_STOP_WAIT": "10", "ORCH_CRONTAB": str(home / "no-such-crontab")}
    return Service(["bash", str(DEPLOY / "poller-service.sh")], env, home,
                   pidfiles=("poller.pid",))


def _poller_watchdog(home: Path) -> Service:
    service = _poller(home)
    service.autostart = home / ".config/factory/poller-autostart"
    service.autostart.write_text("")
    service.env["ORCH_POLL_ENSURE_SEC"] = "600"
    service.start = "ensure"
    service.pidfiles = ("poller.pid", "poller-ensure-loop.pid")
    return service


def _waiter(home: Path) -> Service:
    waiter._make_home(home)
    return Service(["bash", str(DEPLOY / "waiter-service.sh"), "claude-bot"],
                   waiter._env(home), home, pidfiles=("waiter-claude-bot.pid",))


def _linear(home: Path) -> Service:
    for path, source in ((home / ".local/bin/orch", linear.FAKE_ORCH),
                         (home / ".local/bin/ssh", linear.FAKE_SSH),
                         (home / "remote-bin/ss", linear.FAKE_SS)):
        _executable(path, source)
    return Service(["bash", str(DEPLOY / "linear-webhook-service.sh")], linear._env(home), home,
                   pidfiles=("linear-webhook-listener.pid", "linear-webhook-tunnel.pid"))


def _self_heal(home: Path) -> Service:
    _executable(home / ".local/bin/orch", self_heal.FAKE_ORCH)
    _executable(home / ".local/bin/git", self_heal.FAKE_GIT)
    _executable(home / "deploy/waiter-service.sh", self_heal.FAKE_SERVICE)
    (home / ".config/factory").mkdir(parents=True)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "ORCH_HEAL_START_WAIT": "10",
           "ORCH_HEAL_STOP_WAIT": "10", "ORCH_SYSTEMCTL": str(home / "no-such-systemctl"),
           "ORCH_CRONTAB": str(home / "no-such-crontab")}
    return Service(["bash", str(DEPLOY / "self-heal.sh")], env, home, pidfiles=("self-heal.pid",))


def _runner_pool_sync(home: Path) -> Service:
    _executable(home / "bin/gh", FAKE_GH)
    (home / ".config/factory").mkdir(parents=True)
    env = {"HOME": str(home), "PATH": f"{home / 'bin'}:/usr/bin:/bin", "EVERY": "600",
           "LOG": str(home / "sync.log")}
    return Service(["bash", str(DEPLOY / "runner-pool-sync.sh")], env, home, start="ensure")


SERVICES = {"poller": _poller, "poller-watchdog": _poller_watchdog, "waiter": _waiter,
            "linear-webhook": _linear, "self-heal": _self_heal,
            "runner-pool-sync": _runner_pool_sync}


@pytest.fixture
def make(tmp_path: Path):
    made: list[Service] = []

    def build(name: str) -> Service:
        made.append(SERVICES[name](tmp_path / ("home" if not made else f"home-{len(made)}")))
        return made[-1]

    yield build
    for service in made:
        service.stop()
        _wait_for(lambda: not service.pids(), "the service to stop")


def _run_in_killed_group(service: Service, verb: str, tmp_path: Path,
                         kill_on_return: bool = False) -> None:
    exit_file = tmp_path / f"exit-{time.monotonic_ns()}"
    after = "kill -KILL 0" if kill_on_return else "sleep 60"
    with (tmp_path / "caller.log").open("a") as log:
        caller = subprocess.Popen(
            ["bash", "-c", f'"$@"; echo $? >"$EXIT_FILE"; {after}', "caller", *service.argv, verb],
            env={**service.env, "EXIT_FILE": str(exit_file)}, process_group=0,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    try:
        _wait_for(lambda: exit_file.exists() and exit_file.read_text().strip(), f"{verb} to return")
        assert exit_file.read_text().strip() == "0", (tmp_path / "caller.log").read_text()
        _wait_for(service.pids, "the service to run")
    finally:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(caller.pid, sig)
            except ProcessLookupError:
                pass
        caller.wait(timeout=10)


@pytest.mark.parametrize("name", list(SERVICES))
def test_a_service_outlives_the_process_group_that_started_it(
    make, name: str, tmp_path: Path
) -> None:
    service = make(name)
    _run_in_killed_group(service, service.start, tmp_path)
    time.sleep(0.2)

    pids = service.pids()
    assert pids, "the caller's process group took the service down"
    for pid in pids:
        assert os.getsid(pid) == pid, f"pid {pid} stayed in the caller's session"
        assert os.readlink(f"/proc/{pid}/fd/0") == "/dev/null"
    assert service.status_ok()

    service.stop()
    _wait_for(lambda: not any(map(alive, pids)), "stop to end every service pid", 10)


@pytest.mark.parametrize("name", list(SERVICES))
def test_a_service_survives_its_callers_group_dying_the_moment_the_launch_returns(
    make, name: str, tmp_path: Path
) -> None:
    for _ in range(5):
        service = make(name)
        _run_in_killed_group(service, service.start, tmp_path, kill_on_return=True)

        pids = service.pids()
        for pid in pids:
            assert os.getsid(pid) == pid, f"pid {pid} stayed in the caller's session"
        assert service.status_ok()
        service.stop()
        _wait_for(lambda: not any(map(alive, pids)), "stop to end every service pid", 10)


def _poller_starts(home: Path) -> list[int]:
    path = home / "poller-starts"
    return [int(line) for line in path.read_text().split()] if path.exists() else []


def test_restart_from_a_killed_group_leaves_exactly_one_poller(make, tmp_path: Path) -> None:
    service = make("poller")
    assert service.run("start").returncode == 0
    _run_in_killed_group(service, "restart", tmp_path)
    time.sleep(0.2)

    (pid,) = service.pids()
    assert [p for p in _poller_starts(service.home) if alive(p)] == [pid]
    assert _locked(service.home / STATE / "orch.db.lock")


def test_start_from_two_killed_groups_launches_one_poller(make, tmp_path: Path) -> None:
    service = make("poller")
    _run_in_killed_group(service, "start", tmp_path)
    _run_in_killed_group(service, "start", tmp_path)

    (pid,) = service.pids()
    assert _poller_starts(service.home) == [pid]


@pytest.mark.parametrize("hidden,new_session", [({"setsid"}, True), ({"setsid", "perl"}, False)],
                         ids=["perl", "plain-nohup"])
def test_without_setsid_the_launch_falls_back(tmp_path: Path, hidden: set[str],
                                              new_session: bool) -> None:
    bare = link_path_without(tmp_path / "bare", ["/usr/bin", "/bin"], hidden)
    pidfile = tmp_path / "pid"
    launch = (f'. "{DEPLOY}/host-pidns.sh"; factory_detach sleep 60 >/dev/null 2>&1 </dev/null; '
              f'echo $! >"{pidfile}"; sleep 60')
    caller = subprocess.Popen(["sh", "-c", launch], env={"PATH": str(bare)}, process_group=0)
    try:
        pid = int(_wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), "the launch"))
        cmdline = Path(f"/proc/{pid}/cmdline")
        _wait_for(lambda: cmdline.read_bytes().split(b"\0")[:2] == [b"sleep", b"60"],
                  "$! to name the launched command")
        assert (os.getsid(pid) == pid) is new_session
    finally:
        os.killpg(caller.pid, signal.SIGKILL)
        caller.wait(timeout=10)
    if new_session:
        assert alive(pid), "the perl fallback left the launch in the caller's group"
        os.kill(pid, signal.SIGKILL)
