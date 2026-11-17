"""deploy/linear-webhook-service.sh: one listener loop and one tunnel loop per
state directory, however many times `ensure` runs (issue #134)."""

from __future__ import annotations

import fcntl
import itertools
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from lib import heal, health
from lib.queue import SQLiteJobQueue
from tests.procs import alive

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

# Like OpenSSH it closes every inherited descriptor, so the loop's child lock
# (fd 7) does not outlive the loop. The remote forward is an exclusive flock
# on $HOME/remote-forward.lock; its holder's pid is in $HOME/forward-owner.
FAKE_SSH = """#!/usr/bin/env python3
import fcntl, os, subprocess, sys, time
os.closerange(3, 1024)
home = os.environ["HOME"]
tunnel = "-R" in sys.argv and sys.argv[-1] != "true"
with open(os.path.join(home, "ssh-calls.log" if tunnel else "ssh-command-calls.log"), "a") as log:
    log.write(" ".join(sys.argv[1:]).replace("\\n", " ") + "\\n")
if not tunnel and "ss -H" in sys.argv[-1]:
    path = os.path.join(home, "remote-bin") + os.pathsep + os.environ["PATH"]
    run = subprocess.run(["sh", "-c", sys.argv[-1]], env={**os.environ, "PATH": path})
    raise SystemExit(run.returncode)
if not tunnel and sys.argv[-1] != "true":
    raise SystemExit(f"fake ssh: unexpected remote command {sys.argv[-1]!r}")
forward = open(os.path.join(home, "remote-forward.lock"), "a")
try:
    if os.environ.get("FAKE_SSH_FORWARD_FAIL"):
        raise BlockingIOError
    fcntl.flock(forward, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("Error: remote port forwarding failed for listen port 8787", file=sys.stderr)
    raise SystemExit(255)
if not tunnel:
    raise SystemExit(0)
with open(os.path.join(home, "forward-owner"), "w") as owner:
    owner.write(str(os.getpid()))
with open(os.path.join(home, "forward-owner-kind"), "w") as kind:
    kind.write("sshd")
if sys.argv[-1] != "test-host":
    subprocess.run(["sh", "-c", sys.argv[-1].partition("while ")[0]], check=True)
time.sleep(300)
"""

FAKE_SS = """#!/usr/bin/env python3
import fcntl, os, sys
home = os.environ["HOME"]
with open(os.path.join(home, "remote-forward.lock"), "a") as forward:
    try:
        fcntl.flock(forward, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise SystemExit(0)
port = sys.argv[-1].rpartition(":")[2]
line = f"LISTEN 0      128      127.0.0.1:{port}      0.0.0.0:*"
with open(os.path.join(home, "forward-owner-kind")) as kind:
    name = kind.read().strip()
if "p" in sys.argv[1] and name:
    with open(os.path.join(home, "forward-owner")) as owner:
        pid = owner.read().strip()
    line += f'    users:(("{name}",pid={pid},fd=3))'
print(line)
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
    for path, source in ((bin_dir / "orch", FAKE_ORCH), (bin_dir / "ssh", FAKE_SSH),
                         (home / "remote-bin" / "ss", FAKE_SS)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
    yield home
    _service(home, "stop")
    deadline = time.monotonic() + 10
    while (survivors := _processes_of(home)) and time.monotonic() < deadline:
        for pid in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.05)


def _env(home: Path, **extra: str) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LINEAR_WEBHOOK_PORT": str(extra.pop("port", "") or _port_of(home)),
        "LINEAR_WEBHOOK_TUNNEL_HOST": "test-host",
        "LINEAR_WEBHOOK_START_WAIT": "10",
        "LINEAR_WEBHOOK_STOP_WAIT": "10",
        "ORCH_CRONTAB": str(home / "no-such-crontab"),
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
                home, FAKE_SSH_FORWARD_FAIL="1", LINEAR_WEBHOOK_REFUSED_LIMIT="10",
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
        os.killpg(foreign.pid, signal.SIGKILL)
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


def _invisible_listener_lock(home: Path) -> subprocess.Popen:
    lock = _state(home) / "linear-webhook-listener.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(["flock", str(lock), "sleep", "60"])
    _wait_for(lambda: not _lock_free(lock), what="the holder")
    return holder


def test_a_restart_whose_stop_fails_leaves_no_hold(home: Path, tmp_path: Path) -> None:
    # #107 review: self-heal and ensure read a leftover hold as an operator's
    # stop and would never retry or escalate.
    holder = _invisible_listener_lock(home)
    empty = tmp_path / "proc"
    empty.mkdir()
    try:
        result = _service(home, "restart", ORCH_PROC_DIR=str(empty))
    finally:
        holder.kill()
        holder.wait()
    assert result.returncode == 1
    assert "is still held" in result.stderr
    assert not (_state(home) / "linear-webhook.stopped").exists()


def test_a_failed_restart_keeps_an_operators_hold(home: Path, tmp_path: Path) -> None:
    assert _service(home, "stop").returncode == 0
    holder = _invisible_listener_lock(home)
    empty = tmp_path / "proc"
    empty.mkdir()
    try:
        result = _service(home, "restart", ORCH_PROC_DIR=str(empty))
    finally:
        holder.kill()
        holder.wait()
    assert result.returncode == 1
    assert (_state(home) / "linear-webhook.stopped").exists()


def _fake_crontab(tmp_path: Path, table: Path) -> Path:
    crontab = tmp_path / "crontab"
    crontab.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\nfrom pathlib import Path\n"
        f"table = Path({str(table)!r})\n"
        "if sys.argv[1:] == ['-l']:\n"
        "    sys.stdout.write(table.read_text()) if table.exists() else sys.exit(1)\n"
        "elif sys.argv[1:] == ['-']:\n"
        "    table.write_text(sys.stdin.read())\n",
        encoding="utf-8",
    )
    crontab.chmod(0o755)
    return crontab


def test_install_autostart_adds_tagged_cron_lines_and_remove_drops_them(
    home: Path, tmp_path: Path
) -> None:
    table = tmp_path / "crontab.txt"
    table.write_text("0 1 * * * keep-me\n", encoding="utf-8")
    crontab = str(_fake_crontab(tmp_path, table))
    for _ in range(2):
        result = _service(home, "install-autostart", ORCH_CRONTAB=crontab)
        assert result.returncode == 0, result.stderr
    lines = table.read_text().splitlines()
    tagged = [line for line in lines if line.endswith("# factory-linear-webhook")]
    assert len(tagged) == 2
    assert any(line.startswith("@reboot ") for line in tagged)
    assert any(line.startswith("*/5 * * * * ") for line in tagged)
    assert all("linear-webhook-service.sh ensure" in line for line in tagged)
    assert "0 1 * * * keep-me" in lines
    assert (home / ".config/factory/linear-webhook-autostart").exists()

    assert _service(home, "remove-autostart", ORCH_CRONTAB=crontab).returncode == 0
    assert table.read_text().splitlines() == ["0 1 * * * keep-me"]
    assert not (home / ".config/factory/linear-webhook-autostart").exists()


def test_an_unreadable_crontab_changes_nothing(home: Path, tmp_path: Path) -> None:
    crontab = tmp_path / "crontab"
    crontab.write_text("#!/bin/sh\necho 'crontab: permission denied' >&2\nexit 1\n",
                       encoding="utf-8")
    crontab.chmod(0o755)
    result = _service(home, "install-autostart", ORCH_CRONTAB=str(crontab))
    assert result.returncode == 1
    assert "cannot read the crontab" in result.stderr
    assert not (home / ".config/factory/linear-webhook-autostart").exists()


@pytest.mark.parametrize("extra", [
    {"LINEAR_WEBHOOK_TUNNEL_HOST": "-oProxyCommand=evil"},
    {"port": "87a"},
    {"LINEAR_WEBHOOK_RETRY_SEC": "soon"},
    {"LINEAR_WEBHOOK_HELD_RETRY_SEC": "0"},
    {"LINEAR_WEBHOOK_HELD_RETRY_SEC": "0.5"},
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


def _open_files(pid: int) -> set[str]:
    found = set()
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            found.add(os.readlink(entry))
        except OSError:
            continue
    return found


def test_stop_spares_a_process_that_only_has_a_lock_file_open(home: Path) -> None:
    lock = _state(home) / "linear-webhook-listener.lock"
    lock.parent.mkdir(parents=True)
    lock.touch()
    reader = subprocess.Popen(["tail", "-f", str(lock)], stdout=subprocess.DEVNULL)
    try:
        _wait_for(
            lambda: str(lock.resolve()) in _open_files(reader.pid),
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


def _children(home: Path) -> dict[str, int]:
    found = {}
    for pid in _processes_of(home):
        argv = _argv(pid)
        if argv[-2:] == ["linear-webhook", "serve"] or argv[2:4] == ["linear-webhook", "serve"]:
            found["serve"] = pid
        elif any(arg.endswith("/ssh") for arg in argv[:2]):
            found["ssh"] = pid
    return found


def _port_owner(port: int, home: Path) -> subprocess.Popen:
    script = (
        "import socket, time\n"
        f"s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen()\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", script], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE, text=True,
    )
    assert owner.stdout.readline() == "ready\n"
    return owner


@pytest.mark.parametrize("command", ["restart", "stop"])
def test_restart_and_stop_end_children_orphaned_by_a_killed_loop(
    home: Path, tmp_path: Path, command: str
) -> None:
    assert _service(home, "ensure").returncode == 0
    _wait_for(lambda: set(_children(home)) == {"serve", "ssh"}, what="serve and ssh")
    orphans = _children(home)
    for mode in MODES:
        for loop in _loops(home, mode):
            os.kill(loop, signal.SIGKILL)
    _wait_for(lambda: not _loops(home, MODES[0]) and not _loops(home, MODES[1]), what="loops dead")
    assert all(alive(pid) for pid in orphans.values()), "the children outlive their loops"

    assert _service(home, "ensure").returncode == 0
    _wait_for(lambda: len(_loops(home, MODES[0])) == 1, what="a new listener loop")

    unrelated_home = tmp_path / "unrelated"
    unrelated_home.mkdir()
    unrelated = _port_owner(_free_port(), unrelated_home)
    try:
        result = _service(home, command)
        assert result.returncode == 0, result.stdout + result.stderr
        _wait_for(lambda: not any(alive(pid) for pid in orphans.values()), what="orphans gone")
        assert unrelated.poll() is None, "an unrelated port owner was killed"
    finally:
        unrelated.kill()
        unrelated.wait()
    if command == "restart":
        _wait_for(lambda: set(_children(home)) == {"serve", "ssh"}, what="new serve and ssh")
        assert not set(_children(home).values()) & set(orphans.values())
    else:
        assert _processes_of(home) == []


def _forward_owner(home: Path) -> int | None:
    path = home / "forward-owner"
    return int(path.read_text()) if path.exists() and path.read_text() else None


def _ssh_children(home: Path) -> list[int]:
    return [pid for pid in _processes_of(home) if any(a.endswith("/ssh") for a in _argv(pid)[:2])]


def _orphan_an_ssh_that_closed_its_fds(home: Path) -> int:
    assert _service(home, "ensure").returncode == 0
    _wait_for(lambda: "ssh" in _children(home), what="ssh")
    orphan = _children(home)["ssh"]
    _wait_for(lambda: _forward_owner(home) == orphan, what="the forward")
    (loop,) = _loops(home, MODES[1])
    os.kill(loop, signal.SIGKILL)
    _wait_for(lambda: not alive(loop), what="the killed tunnel loop")
    assert alive(orphan)
    assert not Path(f"/proc/{orphan}/fd/7").exists()
    assert _lock_free(_state(home) / "linear-webhook-tunnel-child.lock")
    return orphan


def test_ensure_and_self_heal_replace_an_orphaned_ssh_that_closed_its_fds(home: Path) -> None:
    orphan = _orphan_an_ssh_that_closed_its_fds(home)
    state = _state(home)

    assert _service(home, "ensure").returncode == 0
    actions = heal.heal(
        {"known": True, "components": [{
            "component": "tunnel", "bot": "", "device": "box", "configured": True,
            "label": "tunnel @ box", "state": "down", "reason": "probe failed",
            "pid": orphan, "started_at": 0,
        }]},
        facts=heal.Facts("box", frozenset({"linear-webhook"})), state_dir=state,
        deploy=SERVICE.parent, webhook=heal.Webhook(), environ=_env(home), now=time.time(),
    )
    assert [action.kind for action in actions] == [heal.RESTART]
    assert "restart tunnel @ box (down: probe failed) exit=0" in (state / "self-heal.log").read_text()

    _wait_for(lambda: not alive(orphan), what="the orphaned ssh to be ended")
    _wait_for(lambda: len(_ssh_children(home)) == 1
              and _forward_owner(home) == _ssh_children(home)[0], what="the new forward")
    assert _ssh_children(home) != [orphan]
    assert "forward refused" not in (state / "linear-webhook-tunnel.log").read_text()


def test_stop_ends_an_orphaned_ssh_that_closed_its_fds(home: Path) -> None:
    orphan = _orphan_an_ssh_that_closed_its_fds(home)
    result = _service(home, "stop")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not alive(orphan)
    assert _lock_free(home / "remote-forward.lock")
    assert _processes_of(home) == []


NO_SUCH_PID = 4194305  # above Linux's largest pid_max, so no process has it
PID_NS = "pid:[4026531836]"


def _fake_proc(tmp_path: Path, pid: int, start: int, argv: list[str]) -> Path:
    proc = tmp_path / "proc"
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 30
    (entry / "stat").write_text(f"{pid} (ssh) {' '.join(fields)}\n")
    (entry / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
    (proc / "self" / "ns").mkdir(parents=True)
    os.symlink(PID_NS, proc / "self" / "ns" / "pid")
    return proc


@pytest.mark.parametrize("command", ["stop", "restart"])
def test_stop_and_restart_fail_while_an_owned_child_survives(
    home: Path, tmp_path: Path, command: str
) -> None:
    # The fake /proc shows the recorded ssh alive whatever is sent to it.
    proc = _fake_proc(tmp_path, NO_SUCH_PID, 777,
                      ["ssh", "-N", "-R", "127.0.0.1:1:127.0.0.1:1", "test-host"])
    record = _state(home) / "linear-webhook-tunnel-child.pid"
    record.parent.mkdir(parents=True)
    record.write_text(f"{NO_SUCH_PID} 777 {PID_NS}\n")
    result = _service(home, command, ORCH_PROC_DIR=str(proc), LINEAR_WEBHOOK_STOP_WAIT="1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"tunnel child pid {NO_SUCH_PID} is still running after SIGKILL" in result.stderr
    if command == "restart":
        assert not (_state(home) / "linear-webhook.stopped").exists()


def test_stop_fails_on_a_child_record_from_another_pid_namespace(home: Path) -> None:
    record = _state(home) / "linear-webhook-tunnel-child.pid"
    record.parent.mkdir(parents=True)
    record.write_text("4242 777 pid:[1]\n")
    result = _service(home, "stop")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{record} names a process in another PID namespace" in result.stderr
    assert record.exists()


def test_ensure_starts_no_ssh_over_a_child_record_from_another_pid_namespace(home: Path) -> None:
    record = _state(home) / "linear-webhook-tunnel-child.pid"
    record.parent.mkdir(parents=True)
    record.write_text("4242 777 pid:[1]\n")
    assert _service(home, "ensure").returncode == 0
    log = _state(home) / "linear-webhook-tunnel.log"
    _wait_for(lambda: log.exists() and "another PID namespace" in log.read_text(),
              what="the tunnel loop to refuse the unverifiable record")
    time.sleep(0.5)
    assert _calls(home, "ssh") == []
    assert record.read_text() == "4242 777 pid:[1]\n"
    result = _service(home, "stop")
    assert result.returncode == 1, result.stdout + result.stderr
    assert record.exists()


def _pid_namespaces_work() -> bool:
    if shutil.which("unshare") is None:
        return False
    probe = subprocess.run(["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc", "true"],
                           capture_output=True, check=False)
    return probe.returncode == 0


INNER_ENSURE_THEN_STOP = """
bash "$1" ensure; echo "ensure=$?"
for _ in $(seq 100); do
    grep -qE 'forward refused|another PID namespace' "$2" 2>/dev/null && break
    sleep 0.1
done
bash "$1" stop; echo "stop=$?"
"""


@pytest.mark.skipif(not _pid_namespaces_work(), reason="needs unprivileged user and PID namespaces")
def test_another_pid_namespace_keeps_the_record_of_an_orphaned_ssh_it_cannot_verify(home: Path) -> None:
    orphan = _orphan_an_ssh_that_closed_its_fds(home)
    listener = _loops(home, MODES[0]) + [_children(home)["serve"]]
    for pid in listener:
        os.kill(pid, signal.SIGKILL)
    _wait_for(lambda: not any(alive(pid) for pid in listener), what="the listener to die")
    record = _state(home) / "linear-webhook-tunnel-child.pid"
    original = record.read_text()

    inner = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
         "bash", "-c", INNER_ENSURE_THEN_STOP, "inner", str(SERVICE),
         str(_state(home) / "linear-webhook-tunnel.log")],
        env=_env(home), text=True, capture_output=True, timeout=60, check=False,
    )
    assert "ensure=0" in inner.stdout, inner.stdout + inner.stderr
    assert "stop=1" in inner.stdout, inner.stdout + inner.stderr
    assert f"{record} names a process in another PID namespace" in inner.stderr
    assert record.read_text() == original
    assert len(_calls(home, "ssh")) == 1

    result = _service(home, "stop")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not alive(orphan)
    assert _lock_free(home / "remote-forward.lock")


def test_child_records_name_the_running_serve_and_ssh(home: Path) -> None:
    state = _state(home)

    def recorded(mode: str) -> int | None:
        path = state / f"linear-webhook-{mode}-child.pid"
        return int(path.read_text().split()[0]) if path.exists() else None

    assert _service(home, "ensure").returncode == 0
    _wait_for(lambda: set(_children(home)) == {"serve", "ssh"}, what="serve and ssh")
    children = _children(home)
    _wait_for(lambda: recorded("listener") == children["serve"]
              and recorded("tunnel") == children["ssh"], what="the child records")
    result = _service(home, "stop")
    assert result.returncode == 0, result.stdout + result.stderr
    assert recorded("listener") is None and recorded("tunnel") is None


def _start_time(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()[19])


@pytest.mark.parametrize("mismatch", ["start time", "argv"])
def test_stop_spares_a_reused_pid(home: Path, mismatch: str) -> None:
    tunnel_argv = ["-R", "127.0.0.1:1:127.0.0.1:1"]
    argv = (["sleep", "60"] if mismatch == "argv"
            else [sys.executable, "-c", "import time; time.sleep(60)", *tunnel_argv])
    stranger = subprocess.Popen(argv)
    try:
        _wait_for(lambda: tunnel_argv[1] in _argv(stranger.pid) or mismatch == "argv",
                  what="the stranger's argv")
        start = _start_time(stranger.pid) + (1 if mismatch == "start time" else 0)
        ns = os.readlink("/proc/self/ns/pid")
        record = _state(home) / "linear-webhook-tunnel-child.pid"
        record.parent.mkdir(parents=True)
        record.write_text(f"{stranger.pid} {start} {ns}\n")
        result = _service(home, "stop")
        time.sleep(0.3)
        assert stranger.poll() is None, "stop killed a process that only reuses the pid"
    finally:
        stranger.kill()
        stranger.wait()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not record.exists()


def test_ensure_is_silent_while_everything_runs(home: Path) -> None:
    assert _service(home, "start").returncode == 0
    _wait_for(lambda: set(_children(home)) == {"serve", "ssh"}, what="serve and ssh")
    result = _service(home, "ensure")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def _ssh_calls_settle(home: Path, count: int) -> None:
    _wait_for(lambda: len(_calls(home, "ssh")) == count, timeout=20, what=f"{count} ssh calls")
    time.sleep(0.5)
    assert len(_calls(home, "ssh")) == count


def test_a_refused_forward_gives_up_after_the_limit_as_port_held_by_another_owner(
    home: Path,
) -> None:
    port = _port_of(home)
    settings = dict(FAKE_SSH_FORWARD_FAIL="1", LINEAR_WEBHOOK_REFUSED_LIMIT="2",
                    LINEAR_WEBHOOK_RETRY_SEC="0.1", LINEAR_WEBHOOK_RETRY_MAX_SEC="0.2",
                    LINEAR_WEBHOOK_HELD_RETRY_SEC="3600")
    held = _state(home) / "linear-webhook-tunnel.held"
    assert _service(home, "ensure", **settings).returncode == 0

    _wait_for(held.exists, timeout=20, what="the held record")
    assert held.read_text() == f"{port} test-host\n"
    _ssh_calls_settle(home, 2)
    assert all("ExitOnForwardFailure=yes" in call for call in _calls(home, "ssh"))
    log = (_state(home) / "linear-webhook-tunnel.log").read_text()
    assert f"forward refused 2 times: port {port} on test-host is held by another owner" in log
    status = _service(home, "status", **settings)
    assert f"port {port} on test-host held by another owner" in status.stdout

    assert _service(home, "restart", **settings).returncode == 0
    _ssh_calls_settle(home, 2)
    assert held.exists(), "restart keeps the held record"

    assert _service(home, "start", **settings).returncode == 0
    _wait_for(lambda: len(_calls(home, "ssh")) > 2, timeout=20, what="ssh after start")


def test_a_tunnel_on_another_port_drops_the_held_record(home: Path) -> None:
    held = _state(home) / "linear-webhook-tunnel.held"
    held.parent.mkdir(parents=True)
    held.write_text("1 test-host\n")
    assert _service(home, "ensure").returncode == 0
    _wait_for(lambda: _forward_owner(home) is not None, what="the forward")
    assert not held.exists()


def test_the_device_port_file_sets_both_ends_of_the_tunnel_and_the_listener(home: Path) -> None:
    port = _free_port()
    config = home / ".config" / "factory"
    config.mkdir(parents=True)
    (config / "linear-webhook.port").write_text(f"{port}\n")
    env = _env(home)
    del env["LINEAR_WEBHOOK_PORT"]
    result = subprocess.run(["bash", str(SERVICE), "ensure"], env=env, text=True,
                            capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    _wait_for(lambda: _calls(home, "ssh") and _calls(home, "orch"), what="ssh and serve")
    assert f"-R 127.0.0.1:{port}:127.0.0.1:{port} test-host" in _calls(home, "ssh")[0]
    assert _calls(home, "orch")[0] == f"linear-webhook serve --bind 127.0.0.1:{port}"


def test_the_held_record_lands_where_health_reads_it_under_an_orch_db_override(
    home: Path, tmp_path: Path,
) -> None:
    db = tmp_path / "database" / "orch.db"
    db.parent.mkdir()
    settings = dict(FAKE_SSH_FORWARD_FAIL="1", LINEAR_WEBHOOK_REFUSED_LIMIT="1",
                    LINEAR_WEBHOOK_RETRY_SEC="0.1", LINEAR_WEBHOOK_RETRY_MAX_SEC="0.2",
                    ORCH_DB=str(db))
    assert _service(home, "ensure", **settings).returncode == 0
    _wait_for(lambda: health.read_tunnel_held(db) is not None, timeout=20,
              what="health to read the held record")
    assert health.read_tunnel_held(db) == f"{_port_of(home)} on test-host"
    assert _service(home, "start", **settings).returncode == 0
    _wait_for(lambda: len(_calls(home, "ssh")) > 1, timeout=20, what="ssh after start")


HELD_RETRY = 1
HELD_SETTINGS = dict(LINEAR_WEBHOOK_REFUSED_LIMIT="2", LINEAR_WEBHOOK_RETRY_SEC="0.1",
                     LINEAR_WEBHOOK_RETRY_MAX_SEC="0.2",
                     LINEAR_WEBHOOK_HELD_RETRY_SEC=str(HELD_RETRY))


TUNNEL_ID = "0123456789abcdef"


def _remote_holder(home: Path, process_name: str, claimed_by: str | None = None,
                   pid_hidden: bool = False, claim_shell_of: str | None = None,
                   reused_pid: bool = False) -> subprocess.Popen:
    claim = home / ".cache" / "factory" / f"linear-webhook-tunnel.{claimed_by}"
    claim_shell_of = claim_shell_of or claimed_by
    script = (
        "import fcntl, os, subprocess, time\n"
        "home = os.environ['HOME']\n"
        "forward = open(os.path.join(home, 'remote-forward.lock'), 'a')\n"
        "fcntl.flock(forward, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "with open(os.path.join(home, 'forward-owner'), 'w') as owner:\n"
        "    owner.write(str(os.getpid()))\n"
        "with open(os.path.join(home, 'forward-owner-kind'), 'w') as name:\n"
        f"    name.write({'' if pid_hidden else process_name!r})\n"
        f"if {claimed_by is not None!r}:\n"
        f"    os.makedirs({str(claim.parent)!r}, exist_ok=True)\n"
        f"    with open({str(claim)!r}, 'w') as claim:\n"
        "        claim.write(f'{os.getpid()}\\n')\n"
        f"    if {reused_pid!r}:\n"
        f"        os.utime({str(claim)!r}, (time.time() - 3600,) * 2)\n"
        f"if {claim_shell_of is not None!r}:\n"
        "    subprocess.Popen(['sh', '-c', 'while kill -0 $PPID 2>/dev/null; do sleep 0.2; done',\n"
        f"                      'linear-webhook-tunnel.{claim_shell_of}'])\n"
        "print('ready', flush=True)\n"
        "time.sleep(300)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout.readline() == "ready\n"
    return holder


def _tunnel_holds_the_forward(home: Path) -> bool:
    owner = _forward_owner(home)
    argv = _argv(owner) if owner is not None else []
    return alive(owner) and "-R" in argv and argv[-1] != "true" if owner else False


def _probes(home: Path) -> list[str]:
    return [call for call in _calls(home, "ssh-command") if call.endswith(" true")]


def _tunnel_log(home: Path) -> str:
    return (_state(home) / "linear-webhook-tunnel.log").read_text()


def _own_tunnel_id(home: Path, state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "linear-webhook-tunnel.id").write_text(f"{TUNNEL_ID}\n")


@pytest.mark.parametrize("pid_hidden", [False, True], ids=["pid-shown", "pid-hidden"])
def test_a_held_tunnel_ends_its_own_stale_sshd_session_and_comes_back_without_start(
    home: Path, pid_hidden: bool,
) -> None:
    port = _port_of(home)
    _own_tunnel_id(home, _state(home))
    stale = _remote_holder(home, "sshd", claimed_by=TUNNEL_ID, pid_hidden=pid_hidden)
    assert _service(home, "ensure", **HELD_SETTINGS).returncode == 0

    assert stale.wait(timeout=20) == -signal.SIGTERM
    _wait_for(lambda: _tunnel_holds_the_forward(home), timeout=20,
              what="the tunnel to hold the forward")
    assert health.read_tunnel_held(_state(home) / "orch.db") is None
    log = _tunnel_log(home)
    assert f"forward refused 2 times: port {port} on test-host is held by another owner" in log
    if pid_hidden:
        assert f"ending sshd {stale.pid}" in log
        assert "held by another user" not in log
    else:
        assert f'users:(("sshd",pid={stale.pid},fd=3))' in log
    reclaim, probe = _calls(home, "ssh-command")
    assert "ss -Hltnp" in reclaim
    assert probe.endswith(f"-R 127.0.0.1:{port}:127.0.0.1:{port} test-host true")
    assert "ExitOnForwardFailure=yes" in probe
    assert "held by another owner" not in _service(home, "status").stdout


@pytest.mark.parametrize("process_name, claimed_by, pid_hidden, claim_shell_of, reused_pid", [
    ("python3", None, False, None, False),
    ("sshd", None, False, None, False),
    ("sshd", "fedcba9876543210", False, None, False),
    ("sshd", None, True, None, False),
    ("sshd", "fedcba9876543210", True, None, False),
    ("sshd", TUNNEL_ID, True, "fedcba9876543210", False),
    ("sshd", TUNNEL_ID, False, None, True),
    ("sshd", TUNNEL_ID, True, None, True),
])
def test_a_foreign_holder_is_left_alone_and_the_tunnel_returns_within_the_held_retry(
    home: Path, process_name: str, claimed_by: str | None, pid_hidden: bool,
    claim_shell_of: str | None, reused_pid: bool,
) -> None:
    port = _port_of(home)
    held = _state(home) / "linear-webhook-tunnel.held"
    _own_tunnel_id(home, _state(home))
    foreign = _remote_holder(home, process_name, claimed_by=claimed_by, pid_hidden=pid_hidden,
                             claim_shell_of=claim_shell_of, reused_pid=reused_pid)
    users = "another user" if pid_hidden else f'users:(("{process_name}",pid={foreign.pid},fd=3))'
    assert _service(home, "ensure", **HELD_SETTINGS).returncode == 0

    _wait_for(lambda: len(_probes(home)) >= 1, timeout=20, what="a refused probe")
    entered = held.stat().st_mtime_ns
    _wait_for(lambda: len(_probes(home)) >= 3, timeout=20, what="three refused probes")
    assert foreign.poll() is None, "a holder this device never claimed was signalled"
    assert "ending sshd" not in _tunnel_log(home)
    assert held.read_text() == f"{port} test-host\n"
    assert held.stat().st_mtime_ns == entered, "a held retry rewrote the record health dates from"
    assert f"held by {users}" in _tunnel_log(home)
    assert f"next reclaim and probe in {HELD_RETRY}s" in _tunnel_log(home)
    status = _service(home, "status", **HELD_SETTINGS)
    assert f"port {port} on test-host held by another owner" in status.stdout
    assert users in status.stdout

    foreign.kill()
    foreign.wait()
    _wait_for(lambda: _tunnel_holds_the_forward(home), timeout=HELD_RETRY + 3,
              what="the tunnel back within the held retry")
    assert not held.exists()


def test_the_held_record_stays_until_a_probe_succeeds(home: Path) -> None:
    db = _state(home) / "orch.db"
    foreign = _remote_holder(home, "python3")
    samples: list[str | None] = []
    done = threading.Event()

    def sample() -> None:
        while not done.is_set():
            samples.append(health.read_tunnel_held(db))
            time.sleep(0.002)

    sampler = threading.Thread(target=sample)
    sampler.start()
    try:
        assert _service(home, "ensure", **HELD_SETTINGS).returncode == 0
        _wait_for(lambda: len(_probes(home)) >= 3, timeout=20, what="three refused probes")
        foreign.kill()
        foreign.wait()
        _wait_for(lambda: _tunnel_holds_the_forward(home), timeout=20,
                  what="the tunnel to hold the forward")
    finally:
        done.set()
        sampler.join()
    runs = [held for held, _ in itertools.groupby(sample is not None for sample in samples)]
    assert runs == [False, True, False], "health saw the held record vanish before the probe won"


@pytest.mark.timeout(30)
def test_two_devices_sharing_the_tunnel_user_never_end_each_others_live_tunnel(
    home: Path, tmp_path: Path,
) -> None:
    other = tmp_path / "other-device-state"
    assert _service(home, "ensure").returncode == 0
    _wait_for(lambda: _tunnel_holds_the_forward(home), timeout=20, what="device A's tunnel")
    first = _forward_owner(home)

    assert _service(home, "ensure", ORCH_STATE_DIR=str(other), **HELD_SETTINGS).returncode == 0
    other_log = other / "linear-webhook-tunnel.log"
    _wait_for(lambda: other_log.exists() and other_log.read_text().count("next reclaim and probe") >= 3,
              timeout=20, what="three held retries on device B")
    assert _forward_owner(home) == first and alive(first), "device B ended device A's live tunnel"
    assert "ending sshd" not in other_log.read_text()
    assert f'held by users:(("sshd",pid={first},fd=3))' in other_log.read_text()
    assert "ending sshd" not in _tunnel_log(home)
    assert (other / "linear-webhook-tunnel.held").exists()
    assert not (_state(home) / "linear-webhook-tunnel.held").exists()

    stopped = _service(home, "stop", ORCH_STATE_DIR=str(other))
    assert stopped.returncode == 0, f"device B stop failed: {stopped.stdout}{stopped.stderr}"
    stopped = _service(home, "stop")
    assert stopped.returncode == 0, f"device A stop failed: {stopped.stdout}{stopped.stderr}"
    assert _processes_of(home) == [], "a fake ssh or loop outlived both devices' stop"


def _retire(home: Path) -> Path:
    retired = home / ".config" / "factory" / "orch-retired"
    retired.parent.mkdir(parents=True, exist_ok=True)
    retired.write_text("pc 2026-11-17T13:00:00Z\nsecond line\n", encoding="utf-8")
    return retired


@pytest.mark.parametrize("command", ["start", "restart", "ensure", "_listener-loop", "_tunnel-loop"])
def test_a_retired_host_starts_no_listener_and_no_tunnel(home: Path, command: str) -> None:
    retired = _retire(home)

    result = _service(home, command)

    assert result.returncode == 79, result.stdout + result.stderr
    assert f"{retired}: pc 2026-11-17T13:00:00Z" in result.stderr
    assert "second line" not in result.stderr
    time.sleep(0.5)
    assert _loops(home, "_listener-loop") == []
    assert _loops(home, "_tunnel-loop") == []
    assert _calls(home, "orch") == []
    assert _calls(home, "ssh") == []


def test_a_retired_host_can_still_see_and_stop_its_listener(home: Path) -> None:
    started = _service(home, "start")
    assert started.returncode == 0, started.stdout + started.stderr
    _wait_for(lambda: _calls(home, "orch") and _calls(home, "ssh"), what="serve and ssh")
    _retire(home)

    status = _service(home, "status")
    assert status.returncode == 0, status.stdout + status.stderr
    assert "listener: running" in status.stdout
    stopped = _service(home, "stop")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert _processes_of(home) == []
    assert _service(home, "status").returncode == 3
