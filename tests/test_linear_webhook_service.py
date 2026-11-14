"""deploy/linear-webhook-service.sh: one listener loop and one tunnel loop per
state directory, however many times `ensure` runs (issue #134)."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from lib.queue import SQLiteJobQueue
from tests.procs import alive, open_files

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "linear-webhook-service.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None or not Path("/proc/self/cmdline").exists(),
    reason="needs flock(1) and /proc",
)

FAKE_ORCH = """#!/usr/bin/env python3
import os, sys, time
with open(os.path.join(os.environ["HOME"], "orch-calls.log"), "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
assert sys.argv[1:3] == ["linear-webhook", "serve"], sys.argv
time.sleep(300)
"""

FAKE_SSH = """#!/usr/bin/env python3
import os, sys, time
with open(os.path.join(os.environ["HOME"], "ssh-calls.log"), "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_SSH_FORWARD_FAIL"):
    print("Error: remote port forwarding failed for listen port 8787", file=sys.stderr)
    raise SystemExit(255)
time.sleep(300)
"""

MODES = ("_listener-loop", "_tunnel-loop")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    return dict(
        item.split("=", 1) for item in raw.decode(errors="replace").split("\0") if "=" in item
    )


def _argv(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return raw.decode(errors="replace").split("\0")[:-1]


def _processes_of(home: Path) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            if _environ(int(entry.name)).get("HOME") == str(home) and alive(int(entry.name)):
                found.append(int(entry.name))
    return found


def _loops(home: Path, mode: str) -> list[int]:
    return [
        pid
        for pid in _processes_of(home)
        if any(
            arg.endswith("linear-webhook-service.sh") and argv[i + 1 : i + 2] == [mode]
            for argv in [_argv(pid)]
            for i, arg in enumerate(argv)
        )
    ]


def _calls(home: Path, name: str) -> list[str]:
    path = home / f"{name}-calls.log"
    return path.read_text().splitlines() if path.exists() else []


def _state(home: Path) -> Path:
    return home / ".local" / "state" / "factory"


def _wait_for(predicate, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


@contextmanager
def _held(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _lock_free(path: Path) -> bool:
    with path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True


@pytest.fixture
def home(tmp_path: Path):
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name, source in (("orch", FAKE_ORCH), ("ssh", FAKE_SSH)):
        (bin_dir / name).write_text(source, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    yield home
    _service(home, "stop")
    for pid in _processes_of(home):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _env(home: Path, **extra: str) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LINEAR_WEBHOOK_PORT": str(extra.pop("port", "") or _port_of(home)),
        "LINEAR_WEBHOOK_TUNNEL_HOST": "test-host",
        "LINEAR_WEBHOOK_START_WAIT": "10",
        "LINEAR_WEBHOOK_STOP_WAIT": "10",
        **extra,
    }


def _port_of(home: Path) -> int:
    path = home / "port"
    if not path.exists():
        path.write_text(str(_free_port()))
    return int(path.read_text())


def _service(home: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SERVICE), *args],
        env=_env(home, **extra),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_concurrent_ensure_starts_exactly_one_listener_and_one_tunnel(home: Path) -> None:
    runs = [
        subprocess.Popen(
            ["bash", str(SERVICE), "ensure"],
            env=_env(home),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(8)
    ]
    for run in runs:
        out, err = run.communicate(timeout=60)
        assert run.returncode == 0, out + err

    _wait_for(lambda: _calls(home, "orch") and _calls(home, "ssh"), what="serve and ssh")
    time.sleep(1.0)
    assert len(_loops(home, "_listener-loop")) == 1
    assert len(_loops(home, "_tunnel-loop")) == 1
    port = _port_of(home)
    assert _calls(home, "orch") == [f"linear-webhook serve --bind 127.0.0.1:{port}"]
    assert len(_calls(home, "ssh")) == 1
    assert f"-R 127.0.0.1:{port}:127.0.0.1:{port} test-host" in _calls(home, "ssh")[0]
    assert "ExitOnForwardFailure=yes" in _calls(home, "ssh")[0]

    status = _service(home, "status")
    assert status.returncode == 0, status.stdout + status.stderr
    assert "listener: running" in status.stdout
    assert "tunnel: running" in status.stdout

    stopped = _service(home, "stop")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert _processes_of(home) == []
    assert _service(home, "status").returncode == 3


@pytest.mark.parametrize("mode,lock", [
    ("_listener-loop", "linear-webhook-listener.lock"),
    ("_tunnel-loop", "linear-webhook-tunnel.lock"),
])
def test_a_second_loop_exits_0_without_spawning_while_the_lock_is_held(
    home: Path, mode: str, lock: str
) -> None:
    state = _state(home)
    pidfile = state / lock.replace(".lock", ".pid")
    with _held(state / lock):
        pidfile.write_text("4242\n")
        result = subprocess.run(
            ["bash", str(SERVICE), mode],
            env=_env(home),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "already running (lock held)" in result.stdout
    assert _calls(home, "orch") == []
    assert _calls(home, "ssh") == []
    assert pidfile.read_text() == "4242\n"


def test_listener_never_starts_serve_while_the_port_has_a_listener(home: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        result = _service(
            home, "ensure", port=str(port), LINEAR_WEBHOOK_TUNNEL_HOST="",
            LINEAR_WEBHOOK_RETRY_SEC="0.2", LINEAR_WEBHOOK_RETRY_MAX_SEC="0.4",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        log = _state(home) / "linear-webhook-listener.log"
        _wait_for(lambda: log.exists() and "already has a listener" in log.read_text(),
                  what="the busy-port line")
        time.sleep(1.5)
        assert _calls(home, "orch") == []
        assert log.read_text().count("already has a listener") == 1
        assert _loops(home, "_tunnel-loop") == []
    _wait_for(lambda: _calls(home, "orch"), what="serve once the port is free")
    assert _calls(home, "orch") == [f"linear-webhook serve --bind 127.0.0.1:{port}"]


def test_refused_forward_backs_off_doubling_to_the_cap(home: Path, tmp_path: Path) -> None:
    out = tmp_path / "tunnel.out"
    with out.open("w") as sink:
        loop = subprocess.Popen(
            ["bash", str(SERVICE), "_tunnel-loop"],
            env=_env(
                home, FAKE_SSH_FORWARD_FAIL="1",
                LINEAR_WEBHOOK_RETRY_SEC="0.25", LINEAR_WEBHOOK_RETRY_MAX_SEC="1",
            ),
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for(lambda: out.read_text().count("forward refused") >= 4, timeout=15,
                      what="four refused forwards")
        finally:
            loop.terminate()
            loop.wait(timeout=10)
    delays = re.findall(r"forward refused: port \d+ already forwarded on test-host; retry in (\S+)s",
                        out.read_text())
    assert delays[:4] == ["0.25", "0.5", "1", "1"]
    assert loop.returncode in (0, 143)


def test_stop_ends_every_loop_including_old_copies_and_their_children(
    home: Path, tmp_path: Path
) -> None:
    old = tmp_path / "old" / "linear-webhook-service.sh"
    old.parent.mkdir()
    old.write_text("#!/usr/bin/env bash\nsleep 300 &\nwait\n")
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    olds = [
        subprocess.Popen(["bash", str(old), mode], env=env, start_new_session=True)
        for mode in ("_listener-loop", "_listener-loop", "_tunnel-loop")
    ]
    foreign = subprocess.Popen(
        ["bash", str(old), "_listener-loop"],
        env={"HOME": str(other_home), "PATH": "/usr/bin:/bin"},
        start_new_session=True,
    )
    try:
        assert _service(home, "ensure").returncode == 0
        _wait_for(lambda: _calls(home, "orch") and _calls(home, "ssh"), what="serve and ssh")
        _wait_for(lambda: len(_loops(home, "_listener-loop")) == 3, what="the loops")
        children = [pid for pid in _processes_of(home) if _argv(pid)[:1] == ["sleep"]]
        assert len(children) == 3

        stopped = _service(home, "stop")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        for proc in olds:
            proc.wait(timeout=5)
        assert all(not alive(pid) for pid in children)
        assert _processes_of(home) == []
        state = _state(home)
        assert _lock_free(state / "linear-webhook-listener.lock")
        assert _lock_free(state / "linear-webhook-tunnel.lock")
        assert foreign.poll() is None, "stop must leave another state dir's loops alone"

        held = _service(home, "ensure")
        assert held.returncode == 0 and held.stdout == ""
        time.sleep(0.5)
        assert _processes_of(home) == []
    finally:
        foreign.kill()
        foreign.wait()


def test_stop_fails_while_a_lock_is_held_by_a_process_it_cannot_see(
    home: Path, tmp_path: Path
) -> None:
    # A loop in another PID namespace: it holds the lock, but this shell's
    # /proc (here an empty one) does not list it.
    lock = _state(home) / "linear-webhook-listener.lock"
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(["flock", str(lock), "sleep", "60"])
    try:
        _wait_for(lambda: not _lock_free(lock), what="the holder")
        empty = tmp_path / "proc"
        empty.mkdir()
        result = _service(home, "stop", ORCH_PROC_DIR=str(empty))
        assert holder.poll() is None
    finally:
        holder.kill()
        holder.wait()
    assert result.returncode == 1
    assert f"{lock} is still held" in result.stderr


def test_start_clears_a_stop_and_ensure_restarts_a_killed_loop(home: Path) -> None:
    assert _service(home, "stop").returncode == 0
    assert (_state(home) / "linear-webhook.stopped").exists()
    started = _service(home, "start")
    assert started.returncode == 0, started.stdout + started.stderr
    assert "started _listener-loop" in started.stdout
    assert not (_state(home) / "linear-webhook.stopped").exists()
    again = _service(home, "start")
    assert "_listener-loop already running" in again.stdout

    (listener,) = _loops(home, "_listener-loop")
    os.kill(listener, signal.SIGKILL)
    _wait_for(lambda: not alive(listener), what="the killed loop")
    assert _lock_free(_state(home) / "linear-webhook-listener.lock")
    assert _service(home, "ensure").returncode == 0
    assert len(_loops(home, "_listener-loop")) == 1
    assert len(_loops(home, "_tunnel-loop")) == 1


def test_autostart_flag(home: Path) -> None:
    flag = home / ".config" / "factory" / "linear-webhook-autostart"
    assert _service(home, "install-autostart").returncode == 0
    assert flag.is_file()
    assert _service(home, "remove-autostart").returncode == 0
    assert not flag.exists()


@pytest.mark.parametrize("extra", [
    {"LINEAR_WEBHOOK_TUNNEL_HOST": "-oProxyCommand=evil"},
    {"port": "87a"},
    {"LINEAR_WEBHOOK_RETRY_SEC": "soon"},
])
def test_ensure_refuses_bad_settings(home: Path, extra: dict[str, str]) -> None:
    result = _service(home, "ensure", **extra)
    assert result.returncode == 2
    assert _processes_of(home) == []


def test_unknown_command(home: Path) -> None:
    result = _service(home, "bogus")
    assert result.returncode == 2
    assert "usage:" in result.stderr


def _real_orch(home: Path) -> None:
    wrapper = home / ".local" / "bin" / "orch"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'bin' / 'orch'} \"$@\"\n")
    wrapper.chmod(0o755)


def _accepts(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
    except OSError:
        return False
    return True


def test_listener_serves_the_orch_config_named_by_orch_config(
    home: Path, tmp_path: Path
) -> None:
    _real_orch(home)
    secret = tmp_path / "linear-webhook.secret"
    secret.write_text("s" * 40, encoding="utf-8")
    secret.chmod(0o600)
    config = tmp_path / "custom-orch.yml"
    config.write_text(
        "poll_seconds: 60\n"
        "linear:\n  api_key_env: LINEAR_KEY\n"
        "projects:\n"
        "  - repo: acme/widgets\n"
        "    factory_ref: main\n"
        "    linear_project_id: project-1\n"
        "linear_webhook:\n"
        "  bind: 127.0.0.1:8787\n"
        f"  secret_file: {secret}\n",
        encoding="utf-8",
    )
    database = tmp_path / "orch.db"
    SQLiteJobQueue(database, clock=time.time)

    result = _service(
        home, "ensure", LINEAR_WEBHOOK_TUNNEL_HOST="",
        ORCH_CONFIG=str(config), ORCH_DB=str(database),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _wait_for(lambda: _accepts(_port_of(home)), timeout=20, what="serve on the port")


def test_stop_spares_a_process_that_only_has_a_lock_file_open(home: Path) -> None:
    lock = _state(home) / "linear-webhook-listener.lock"
    lock.parent.mkdir(parents=True)
    lock.touch()
    reader = subprocess.Popen(["tail", "-f", str(lock)], stdout=subprocess.DEVNULL)
    try:
        _wait_for(
            lambda: str(lock.resolve()) in open_files(reader.pid),
            what="the reader to open the lock file",
        )
        result = _service(home, "stop")
        time.sleep(0.3)
        assert reader.poll() is None, "stop killed a process that holds no lock"
    finally:
        reader.kill()
        reader.wait()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no linear webhook loop is running" in result.stdout
