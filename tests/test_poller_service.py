"""The poller as a service: its own pidfile, SIGTERM cleanup, autostart."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import cli

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "poller-service.sh"

# Runs the real `orch poll` CLI with the runtime and loop stubbed out, so the
# pidfile, lock and signal handling are exercised without GitHub or Linear.
DRIVER = """
import sys, time
sys.path.insert(0, {root!r})
from lib import cli

class Loop:
    def __init__(self, dispatcher, source):
        pass
    def run(self, **_kwargs):
        print("polling", flush=True)
        time.sleep(60)

cli._runtime = lambda path, orch, create: (object(), object())
cli.PollLoop = Loop
raise SystemExit(cli.main(sys.argv[1:]))
"""


def _driver(tmp_path: Path) -> Path:
    path = tmp_path / "driver.py"
    path.write_text(DRIVER.format(root=str(ROOT)), encoding="utf-8")
    return path


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "state" / "orch.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"")
    return database


def _start(tmp_path: Path, *args: str) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k not in ("ORCH_DB", "ORCH_POLL_PIDFILE")}
    proc = subprocess.Popen(
        [sys.executable, str(_driver(tmp_path)), "poll", *args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline() == "polling\n"
    return proc


def test_poll_writes_its_pidfile_beside_the_database_and_removes_it_on_sigterm(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    pidfile = database.parent / "poller.pid"
    proc = _start(tmp_path, "--db", str(database))
    try:
        assert pidfile.read_text(encoding="utf-8") == f"{proc.pid}\n"
        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 143, stderr
    assert "Traceback" not in stderr
    assert not pidfile.exists()


def test_poll_pidfile_flag_overrides_the_default(tmp_path: Path) -> None:
    database = _database(tmp_path)
    pidfile = tmp_path / "run" / "custom.pid"
    proc = _start(tmp_path, "--db", str(database), "--pidfile", str(pidfile))
    try:
        assert pidfile.read_text(encoding="utf-8") == f"{proc.pid}\n"
        assert not (database.parent / "poller.pid").exists()
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 130
    assert not pidfile.exists()


def test_second_poller_is_refused_and_leaves_the_owners_pidfile(tmp_path: Path) -> None:
    database = _database(tmp_path)
    pidfile = database.parent / "poller.pid"
    first = _start(tmp_path, "--db", str(database))
    try:
        second = subprocess.run(
            [sys.executable, str(_driver(tmp_path)), "poll", "--db", str(database)],
            env={k: v for k, v in os.environ.items() if k != "ORCH_DB"},
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert second.returncode == 1
        assert "another orch poll owns" in second.stderr
        assert pidfile.read_text(encoding="utf-8") == f"{first.pid}\n"
    finally:
        first.terminate()
        first.communicate(timeout=10)
    assert not pidfile.exists()


def test_poll_lock_replaces_a_stale_pidfile_and_removes_only_its_own(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    pidfile = tmp_path / "poller.pid"
    pidfile.write_text("999999999\n", encoding="utf-8")

    with cli._poll_lock(database, pidfile):
        assert pidfile.read_text(encoding="utf-8") == f"{os.getpid()}\n"
    assert not pidfile.exists()

    with cli._poll_lock(database, pidfile):
        pidfile.write_text("12345\n", encoding="utf-8")  # someone else's now
    assert pidfile.read_text(encoding="utf-8") == "12345\n"


def test_poll_refused_by_the_lock_does_not_touch_the_pidfile(tmp_path: Path) -> None:
    database = _database(tmp_path)
    pidfile = tmp_path / "poller.pid"
    with cli._poll_lock(database, pidfile):
        with pytest.raises(ValueError, match="another orch poll"):
            with cli._poll_lock(database, tmp_path / "other.pid"):
                pass
        assert not (tmp_path / "other.pid").exists()
        assert pidfile.read_text(encoding="utf-8") == f"{os.getpid()}\n"


# --- deploy/poller-service.sh -----------------------------------------------

FAKE_ORCH = """#!/usr/bin/env python3
# Stands in for `orch poll --pidfile P`: owns P until SIGTERM.
import os, signal, sys, time
from pathlib import Path

args = sys.argv[1:]
assert args[:2] == ["poll", "--pidfile"], args
if os.environ.get("FAKE_ORCH_FAIL"):
    print("orch poll: another orch poll owns orch.db.lock", file=sys.stderr)
    raise SystemExit(1)
pidfile = Path(args[2])

def leave(*_):
    if pidfile.read_text().strip() == str(os.getpid()):
        pidfile.unlink()
    raise SystemExit(143)

signal.signal(signal.SIGTERM, leave)
pidfile.write_text(f"{os.getpid()}\\n")
print(f"polling as {os.getpid()}", flush=True)
while True:
    time.sleep(1)
"""

FAKE_CRONTAB = """#!/usr/bin/env python3
import os, sys
from pathlib import Path

table = Path(os.environ["FAKE_CRONTAB_FILE"])
if sys.argv[1:] == ["-l"]:
    if not table.exists():
        print("no crontab for test", file=sys.stderr)
        raise SystemExit(1)
    sys.stdout.write(table.read_text())
elif sys.argv[1:] == ["-"]:
    table.write_text(sys.stdin.read())
else:
    raise SystemExit(f"unexpected crontab arguments {sys.argv[1:]}")
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    config = home / ".config" / "factory"
    config.mkdir(parents=True)
    (config / "webhook.env").write_text("", encoding="utf-8")
    (config / "orch-poll.env").write_text("", encoding="utf-8")
    orch = home / ".local" / "bin" / "orch"
    orch.parent.mkdir(parents=True)
    orch.write_text(FAKE_ORCH, encoding="utf-8")
    orch.chmod(0o755)
    yield home
    _kill_poller(home)


def _pidfile(home: Path) -> Path:
    return home / ".local" / "state" / "factory" / "poller.pid"


def _kill_poller(home: Path) -> None:
    try:
        pid = int(_pidfile(home).read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _service(home: Path, *args: str, cron: Path | None = None, **extra: str):
    # Tests never reach a real crontab: ORCH_CRONTAB is always a fake or
    # a path that does not exist.
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "ORCH_POLL_START_WAIT": "10",
        "ORCH_POLL_STOP_WAIT": "10",
        "ORCH_CRONTAB": str(home / "no-such-crontab"),
        **extra,
    }
    if cron is not None:
        fake_bin = home / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        crontab = fake_bin / "crontab"
        crontab.write_text(FAKE_CRONTAB, encoding="utf-8")
        crontab.chmod(0o755)
        env["ORCH_CRONTAB"] = str(crontab)
        env["FAKE_CRONTAB_FILE"] = str(cron)
    return subprocess.run(
        ["bash", str(SERVICE), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not stat.exists() or stat.read_text().rpartition(")")[2].split()[0] != "Z"


def test_service_start_is_single_instance_and_stop_cleans_up(home: Path) -> None:
    started = _service(home, "start")
    assert started.returncode == 0, started.stderr
    pid = int(_pidfile(home).read_text())
    assert f"started orch poll (pid {pid})" in started.stdout
    assert _service(home, "status").stdout == f"running (pid {pid})\n"

    again = _service(home, "start")
    assert again.returncode == 0
    assert f"already running (pid {pid})" in again.stdout
    assert int(_pidfile(home).read_text()) == pid
    log = home / ".local/state/factory/poller.log"
    assert log.read_text().count("polling as") == 1

    stopped = _service(home, "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert f"stopped orch poll (pid {pid})" in stopped.stdout
    assert not _alive(pid)
    assert not _pidfile(home).exists()
    status = _service(home, "status")
    assert status.returncode == 3
    assert status.stdout.startswith("stopped (held by")


def test_ensure_restarts_a_dead_poller_but_respects_stop(home: Path) -> None:
    assert _service(home, "ensure").returncode == 0
    first = int(_pidfile(home).read_text())
    os.kill(first, signal.SIGKILL)  # a crash: the pidfile is left behind
    deadline = time.monotonic() + 5
    while _alive(first) and time.monotonic() < deadline:
        time.sleep(0.05)

    revived = _service(home, "ensure")
    assert revived.returncode == 0, revived.stderr
    second = int(_pidfile(home).read_text())
    assert second != first

    assert _service(home, "stop").returncode == 0
    held = _service(home, "ensure")
    assert held.returncode == 0
    assert held.stdout == ""
    assert not _pidfile(home).exists()

    assert _service(home, "start").returncode == 0
    assert not (home / ".local/state/factory/poller.stopped").exists()


def test_a_pidfile_naming_another_live_process_is_not_the_poller(home: Path) -> None:
    pidfile = _pidfile(home)
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text(f"{os.getpid()}\n")  # this pytest process, not orch poll
    assert _service(home, "status").returncode == 3
    started = _service(home, "start")
    assert started.returncode == 0, started.stderr
    assert int(pidfile.read_text()) != os.getpid()


def test_start_reports_a_poller_that_exits_at_startup(home: Path) -> None:
    result = _service(home, "start", FAKE_ORCH_FAIL="1")
    assert result.returncode == 1
    assert "orch poll exited at startup" in result.stderr
    assert "another orch poll owns" in (home / ".local/state/factory/poller.log").read_text()


def test_install_autostart_adds_tagged_cron_lines_once_and_remove_drops_them(
    home: Path, tmp_path: Path
) -> None:
    table = tmp_path / "crontab.txt"
    table.write_text("MAILTO=\"\"\n0 3 * * * /usr/bin/true # unrelated\n")
    for _ in range(2):
        result = _service(home, "install-autostart", cron=table)
        assert result.returncode == 0, result.stderr
    lines = table.read_text().splitlines()
    assert lines[:2] == ['MAILTO=""', "0 3 * * * /usr/bin/true # unrelated"]
    service_log = home / ".local/state/factory/poller-service.log"
    job = f"{SERVICE} ensure >>{service_log} 2>&1 # factory-orch-poll"
    assert lines[2:] == [f"@reboot {job}", f"*/5 * * * * {job}"]
    assert (home / ".config/factory/poller-autostart").is_file()

    removed = _service(home, "remove-autostart", cron=table)
    assert removed.returncode == 0, removed.stderr
    assert table.read_text().splitlines() == lines[:2]
    assert not (home / ".config/factory/poller-autostart").exists()


def test_install_autostart_into_an_empty_crontab(home: Path, tmp_path: Path) -> None:
    table = tmp_path / "missing-crontab.txt"
    assert _service(home, "install-autostart", cron=table).returncode == 0
    assert _service(home, "install-autostart", cron=table).returncode == 0
    lines = table.read_text().splitlines()
    assert [line.split(" ", 1)[0] for line in lines] == ["@reboot", "*/5"]


def test_install_autostart_without_cron_uses_the_profile_hook(home: Path) -> None:
    result = _service(home, "install-autostart")
    assert result.returncode == 0, result.stderr
    assert "crontab not found" in result.stdout
    assert (home / ".config/factory/poller-autostart").is_file()


def test_service_rejects_an_unknown_command(home: Path) -> None:
    result = _service(home, "bogus")
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_service_keeps_the_callers_path_ahead_of_system_directories(
    home: Path, tmp_path: Path
) -> None:
    # Regression: the script used to put /usr/bin before the caller's PATH,
    # so a caller's own tools (e.g. a crontab or gh) were shadowed.
    first = tmp_path / "first-bin"
    first.mkdir()
    fake_date = first / "date"
    fake_date.write_text("#!/bin/sh\necho FAKE-DATE\n", encoding="utf-8")
    fake_date.chmod(0o755)
    result = _service(home, "stop", PATH=f"{first}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FAKE-DATE poller-service: orch poll is not running\n"
