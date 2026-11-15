"""The poller as a service: its own pidfile, SIGTERM cleanup, autostart."""

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import cli
from tests.procs import alive
from tests.test_wait import restore_signals  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "poller-service.sh"

# Runs the real `orch poll` CLI with the runtime and loop stubbed out, so the
# pidfile, lock and signal handling are exercised without GitHub or Linear.
DRIVER = """
import sys, time
sys.path.insert(0, {root!r})
from lib import cli

class Loop:
    def __init__(self, dispatcher, source, sleep=time.sleep):
        self.sleep = sleep
    def cycle_state(self):
        return None, None
    def run(self, **_kwargs):
        print("polling", flush=True)
        self.sleep(60)

cli._runtime = lambda path, orch, create, **_: (object(), object())
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
if args[:1] in (["restore-db"], ["init-db"]):
    # #66: the service's database step. FAKE_RESTORE is restore-db's exit.
    with open(os.environ.get("FAKE_ORCH_CALLS", os.devnull), "a") as calls:
        backup = os.environ.get("ORCH_DB_BACKUP")
        calls.write(" ".join(args) + (f" backup={backup}" if backup else "") + "\\n")
    database = Path(args[args.index("--db") + 1])
    code = int(os.environ.get("FAKE_RESTORE", "3")) if args[0] == "restore-db" else 0
    if code == 0:
        database.write_bytes(b"restored" if args[0] == "restore-db" else b"")
    raise SystemExit(code)
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
    if os.environ.get("FAKE_CRONTAB_LIST_ERROR"):
        print(os.environ["FAKE_CRONTAB_LIST_ERROR"], file=sys.stderr)
        raise SystemExit(1)
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


def _service(
    home: Path, *args: str, cron: Path | None = None, service: Path = SERVICE, **extra: str
):
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
        ["bash", str(service), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


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
    assert not alive(pid)
    assert not _pidfile(home).exists()
    status = _service(home, "status")
    assert status.returncode == 3
    assert status.stdout.startswith("stopped (held by")


def test_ensure_restarts_a_dead_poller_but_respects_stop(home: Path) -> None:
    assert _service(home, "ensure").returncode == 0
    first = int(_pidfile(home).read_text())
    os.kill(first, signal.SIGKILL)  # a crash: the pidfile is left behind
    deadline = time.monotonic() + 5
    while alive(first) and time.monotonic() < deadline:
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


@pytest.mark.parametrize("command", ["install-autostart", "remove-autostart"])
def test_a_failing_crontab_read_leaves_the_crontab_and_autostart_alone(
    home: Path, tmp_path: Path, command: str
) -> None:
    # Regression (#52 F1.1): `crontab -l 2>/dev/null || true` turned any read
    # failure into an empty table, so install replaced the whole crontab with
    # the factory lines and remove installed an empty one. Only "no crontab
    # for <user>" means empty.
    table = tmp_path / "crontab.txt"
    service_log = home / ".local/state/factory/poller-service.log"
    job = f"{SERVICE} ensure >>{service_log} 2>&1 # factory-orch-poll"
    original = (
        'MAILTO=""\n0 3 * * * /usr/bin/true # unrelated\n'
        f"@reboot {job}\n*/5 * * * * {job}\n"
    )
    table.write_text(original)
    flag = home / ".config/factory/poller-autostart"
    if command == "remove-autostart":
        flag.write_text("")
    result = _service(
        home,
        command,
        cron=table,
        FAKE_CRONTAB_LIST_ERROR="crontab: cannot open /var/spool/cron/test: Permission denied",
    )
    assert result.returncode != 0
    assert "Permission denied" in result.stderr
    assert table.read_text() == original
    assert flag.exists() == (command == "remove-autostart")


def test_stop_holds_against_an_ensure_already_past_its_checks(
    home: Path, tmp_path: Path
) -> None:
    # Regression (#52 F1.4): ensure went through start, which deleted the
    # stop marker, so a stop landing while ensure was between its checks and
    # the launch was undone. stop and ensure now serialize on a service lock,
    # and ensure never clears the marker.
    import fcntl
    import shutil

    if shutil.which("flock") is None:
        pytest.skip("flock(1) is not installed")
    state = home / ".local/state/factory"
    state.mkdir(parents=True)
    lock = (state / "poller-service.lock").open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)  # a `stop` holding the lock
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "ORCH_POLL_START_WAIT": "10",
        "ORCH_CRONTAB": str(home / "no-such-crontab"),
    }
    ensure = subprocess.Popen(
        ["bash", str(SERVICE), "ensure"], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        time.sleep(1.0)
        assert ensure.poll() is None, "ensure did not wait for the service lock"
        (state / "poller.stopped").write_text("")  # the stop completes
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
        out, err = ensure.communicate(timeout=30)
    finally:
        if ensure.poll() is None:
            ensure.kill()
            ensure.communicate()
    assert ensure.returncode == 0, err
    assert (state / "poller.stopped").exists()
    assert not _pidfile(home).exists()
    log = state / "poller.log"
    assert not log.exists() or "polling as" not in log.read_text()


@pytest.mark.parametrize(
    "extra",
    [["orch poll"], ["/x/bin/orch", "poll"]],
    ids=["one-argument", "later-arguments"],
)
@pytest.mark.parametrize("proc_dir", ["real", "absent"])
def test_a_live_process_merely_mentioning_orch_poll_is_not_the_poller(
    home: Path, tmp_path: Path, extra: list[str], proc_dir: str
) -> None:
    # Regression (#52 F1.5): any cmdline containing "orch poll" counted, and
    # with no /proc any live pid counted, so stop could SIGTERM a stranger.
    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", *extra]
    )
    try:
        pidfile = _pidfile(home)
        pidfile.parent.mkdir(parents=True)
        pidfile.write_text(f"{stranger.pid}\n")
        extra_env = {}
        if proc_dir == "absent":
            extra_env["ORCH_PROC_DIR"] = str(tmp_path / "no-proc")
        status = _service(home, "status", **extra_env)
        assert status.returncode == 3, status.stdout
        stopped = _service(home, "stop", **extra_env)
        assert stopped.returncode == 0, stopped.stderr
        assert "is not running" in stopped.stdout
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


def test_without_proc_the_poller_is_recognized_by_ps(home: Path, tmp_path: Path) -> None:
    no_proc = {"ORCH_PROC_DIR": str(tmp_path / "no-proc")}
    started = _service(home, "start", **no_proc)
    assert started.returncode == 0, started.stderr
    pid = int(_pidfile(home).read_text())
    assert _service(home, "status", **no_proc).stdout == f"running (pid {pid})\n"
    assert _service(home, "stop", **no_proc).returncode == 0
    assert not alive(pid)


# --- #107: SIGTERM waits for the poll cycle boundary --------------------------------------------


class _Dispatcher:
    def __init__(self, during):
        self.during = during
        self.finished = 0

    def poll_once(self, source) -> None:
        self.during()
        self.finished += 1


def _loop_until_exit(dispatcher, stopper) -> int:
    from types import SimpleNamespace

    from lib.dispatcher import PollLoop

    source = SimpleNamespace(config=SimpleNamespace(poll_seconds=30))
    with pytest.raises(SystemExit) as stopped:
        PollLoop(dispatcher, source, sleep=stopper.sleep).run()
    return stopped.value.code


def test_a_sigterm_mid_cycle_lets_the_cycle_finish(restore_signals) -> None:
    from lib.cli import StopAtBoundary

    stopper = StopAtBoundary(sleep=lambda seconds: None)
    signal.signal(signal.SIGTERM, stopper.on_term)
    dispatcher = _Dispatcher(lambda: os.kill(os.getpid(), signal.SIGTERM))

    assert _loop_until_exit(dispatcher, stopper) == 143
    assert dispatcher.finished == 1


def test_a_second_sigterm_mid_cycle_exits_at_once(restore_signals) -> None:
    from lib.cli import StopAtBoundary

    stopper = StopAtBoundary(sleep=lambda seconds: None)
    signal.signal(signal.SIGTERM, stopper.on_term)

    def twice() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)

    dispatcher = _Dispatcher(twice)
    assert _loop_until_exit(dispatcher, stopper) == 143
    assert dispatcher.finished == 0


def test_a_sigterm_between_cycles_exits_at_once(restore_signals) -> None:
    from lib.cli import StopAtBoundary

    stopper = StopAtBoundary(sleep=lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    signal.signal(signal.SIGTERM, stopper.on_term)
    dispatcher = _Dispatcher(lambda: None)

    assert _loop_until_exit(dispatcher, stopper) == 143
    assert dispatcher.finished == 1


# --- #108 Unit 7: a missing database is restored before the poller starts (#66) ---


def _lost_db(home: Path) -> Path:
    return home / ".local/state/factory/orch.db"


@pytest.mark.parametrize("command", ["start", "ensure"])
def test_ensure_restores_a_missing_db_from_backup(home: Path, command: str) -> None:
    database = _lost_db(home)
    calls = home / "calls.txt"

    result = _service(home, command, FAKE_RESTORE="0", FAKE_ORCH_CALLS=str(calls))

    assert result.returncode == 0, result.stderr
    assert "RESTORED orch.db from backup" in result.stderr
    assert "RESTORED orch.db from backup" in (
        home / ".local/state/factory/poller.log"
    ).read_text()
    assert database.read_bytes() == b"restored"
    assert calls.read_text() == f"restore-db --db {database}\n"
    assert _pidfile(home).exists()


def test_ensure_inits_and_warns_with_no_backup(home: Path) -> None:
    database = _lost_db(home)
    calls = home / "calls.txt"

    result = _service(home, "ensure", FAKE_RESTORE="3", FAKE_ORCH_CALLS=str(calls))

    assert result.returncode == 0, result.stderr
    assert (
        "WARNING orch.db was missing and no backup exists; created an empty "
        "database (dedupe now relies on thread markers)"
    ) in result.stderr
    assert calls.read_text().splitlines() == [
        f"restore-db --db {database}",
        f"init-db --db {database}",
    ]
    assert database.exists()
    assert _pidfile(home).exists()


def test_recovery_uses_the_paths_the_poller_loads(home: Path) -> None:
    """run-poller.sh loads orch-poll.env before `orch poll`; recovery must
    resolve ORCH_DB and ORCH_DB_BACKUP from it too, or it prepares another
    database from another backup than the poller then opens."""
    database = home / "custom-state" / "orch.db"
    saved = home / "custom-backup" / "orch.db"
    (home / ".config/factory/orch-poll.env").write_text(
        f'ORCH_DB="{database}"\nORCH_DB_BACKUP="{saved}"\n', encoding="utf-8"
    )
    calls = home / "calls.txt"

    result = _service(home, "ensure", FAKE_RESTORE="0", FAKE_ORCH_CALLS=str(calls))

    assert result.returncode == 0, result.stderr
    assert calls.read_text() == f"restore-db --db {database} backup={saved}\n"
    assert database.read_bytes() == b"restored"
    assert not _lost_db(home).exists()


def test_a_failed_restore_starts_no_poller(home: Path) -> None:
    database = _lost_db(home)

    result = _service(home, "start", FAKE_RESTORE="1")

    assert result.returncode == 1
    assert "orch restore-db exited 1; not starting the poller" in result.stderr
    assert not database.exists()
    assert not _pidfile(home).exists()


def test_an_existing_db_is_never_restored(home: Path) -> None:
    calls = home / "calls.txt"
    database = home / ".local/state/factory/orch.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"")

    assert _service(home, "ensure", FAKE_ORCH_CALLS=str(calls)).returncode == 0

    assert not calls.exists()


# --- issue #134: a poller this shell cannot see -----------------------------


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
def test_ensure_and_start_launch_nothing_while_the_poll_lock_is_held(home: Path) -> None:
    # orch poll in another PID namespace: no usable pidfile, but its lock is held.
    database = home / ".local/state/factory/orch.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"")
    with database.with_name("orch.db.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        ensured = _service(home, "ensure")
        started = _service(home, "start")
    assert ensured.returncode == 0, ensured.stderr
    assert ensured.stdout == ""
    assert started.returncode == 0, started.stderr
    assert "orch poll already running (poll lock held)" in started.stdout
    assert not _pidfile(home).exists()
    log = home / ".local/state/factory/poller.log"
    assert not log.exists() or "polling as" not in log.read_text()


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
def test_poller_ensure_loop_is_a_singleton(home: Path, tmp_path: Path) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    shutil.copy(ROOT / "deploy" / "poller-ensure-loop.sh", deploy)
    calls = tmp_path / "ensure-calls"
    fake = deploy / "poller-service.sh"
    fake.write_text(f"#!/bin/sh\necho \"$1\" >>{calls}\n", encoding="utf-8")
    fake.chmod(0o755)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "ORCH_POLL_ENSURE_SEC": "60"}
    loop = [str(deploy / "poller-ensure-loop.sh")]
    lock = home / ".local/state/factory/poller-ensure-loop.lock"
    pidfile = lock.with_suffix(".pid")

    first = subprocess.Popen(loop, env=env, text=True, stdout=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not calls.exists():
            assert time.monotonic() < deadline, "the first loop never ran ensure"
            time.sleep(0.05)
        assert calls.read_text() == "ensure\n"
        assert pidfile.read_text() == f"{first.pid}\n"

        second = subprocess.run(
            loop, env=env, text=True, capture_output=True, timeout=5, check=False
        )
        assert second.returncode == 0, second.stderr
        assert "already running (lock held)" in second.stdout
        assert calls.read_text() == "ensure\n"
        assert pidfile.read_text() == f"{first.pid}\n"
    finally:
        first.terminate()
        first.communicate(timeout=10)
    assert not pidfile.exists()


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
def test_ensure_probes_the_database_orch_poll_env_names(home: Path, tmp_path: Path) -> None:
    # run-poller.sh sources orch-poll.env, so its ORCH_DB is the one the
    # poller locks, whatever ORCH_DB the caller has.
    configured = tmp_path / "configured.db"
    configured.write_bytes(b"")
    callers = tmp_path / "callers.db"
    callers.write_bytes(b"")
    (home / ".config/factory/orch-poll.env").write_text(
        f"ORCH_DB='{configured}'\n", encoding="utf-8"
    )
    log = home / ".local/state/factory/poller.log"

    with configured.with_name("configured.db.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        runs = [_service(home, "ensure"), _service(home, "ensure", ORCH_DB=str(callers))]
    for run in runs:
        assert run.returncode == 0, run.stderr
        assert run.stdout == ""
    assert not _pidfile(home).exists()
    assert not log.exists() or "polling as" not in log.read_text()

    with callers.with_name("callers.db.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = _service(home, "ensure", ORCH_DB=str(callers))
    assert started.returncode == 0, started.stderr
    assert "started orch poll" in started.stdout
    assert _pidfile(home).exists()


# --- issue #248: box restart with no crontab and a running copy-in ----------


def _deploy_copy(tmp_path: Path, calls: Path) -> Path:
    """deploy/ with the real poller scripts and a waiter-service.sh that only
    records each call."""
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name in ("poller-service.sh", "run-poller.sh", "poller-ensure-loop.sh", "host-pidns.sh"):
        shutil.copy(ROOT / "deploy" / name, deploy)
    fake = deploy / "waiter-service.sh"
    fake.write_text(f'#!/bin/sh\necho "$1 $2" >>{calls}\n', encoding="utf-8")
    fake.chmod(0o755)
    return deploy / "poller-service.sh"


def _stop_loop(home: Path) -> None:
    pidfile = home / ".local/state/factory/poller-ensure-loop.pid"
    try:
        os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
    except (OSError, ValueError):
        pass


def test_poller_ensure_also_ensures_every_autostarted_waiter(
    home: Path, tmp_path: Path
) -> None:
    calls = tmp_path / "waiter-calls"
    service = _deploy_copy(tmp_path, calls)
    config = home / ".config/factory"
    for name in ("waiter-autostart-claude-bot", "waiter-autostart-codex-bot",
                 "waiter-autostart-bogus"):
        (config / name).touch()

    result = _service(home, "ensure", service=service)
    assert result.returncode == 0, result.stderr
    assert sorted(calls.read_text().splitlines()) == ["claude-bot ensure", "codex-bot ensure"]

    assert _service(home, "stop", service=service).returncode == 0
    held = _service(home, "ensure", service=service)
    assert held.returncode == 0, held.stderr
    assert held.stdout == ""
    assert calls.read_text().count("claude-bot ensure") == 2
    assert not _pidfile(home).exists()


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
def test_ensure_without_crontab_starts_the_ensure_loop_that_keeps_waiters_alive(
    home: Path, tmp_path: Path
) -> None:
    calls = tmp_path / "waiter-calls"
    service = _deploy_copy(tmp_path, calls)
    config = home / ".config/factory"
    (config / "poller-autostart").touch()
    (config / "waiter-autostart-claude-bot").touch()
    (config / "waiter-autostart-codex-bot").touch()
    state = home / ".local/state/factory"
    loop_pid = state / "poller-ensure-loop.pid"
    try:
        first = _service(home, "ensure", service=service, ORCH_POLL_ENSURE_SEC="0.2")
        assert first.returncode == 0, first.stderr
        assert "crontab not found" in first.stderr
        deadline = time.monotonic() + 15
        while calls.read_text().count("codex-bot ensure") < 3:
            assert time.monotonic() < deadline, calls.read_text()
            time.sleep(0.1)
        loop = int(loop_pid.read_text())
        assert alive(loop)

        again = _service(home, "ensure", service=service, ORCH_POLL_ENSURE_SEC="0.2")
        assert again.returncode == 0, again.stderr
        assert "crontab not found" not in again.stderr
        assert int(loop_pid.read_text()) == loop
        assert (state / "poller.log").read_text().count("crontab not found") == 1
    finally:
        _stop_loop(home)


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
def test_remove_autostart_ends_the_ensure_loop_that_ensure_started(
    home: Path, tmp_path: Path
) -> None:
    calls = tmp_path / "waiter-calls"
    service = _deploy_copy(tmp_path, calls)
    config = home / ".config/factory"
    (config / "poller-autostart").touch()
    (config / "waiter-autostart-claude-bot").touch()
    loop_pid = home / ".local/state/factory/poller-ensure-loop.pid"
    try:
        started = _service(home, "ensure", service=service, ORCH_POLL_ENSURE_SEC="0.1")
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 15
        while not loop_pid.exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        loop = int(loop_pid.read_text())
        original = int(_pidfile(home).read_text())

        removed = _service(home, "remove-autostart", service=service)
        assert removed.returncode == 0, removed.stderr
        _kill_poller(home)
        deadline = time.monotonic() + 10
        while alive(loop):
            assert time.monotonic() < deadline, "ensure loop outlived poller-autostart"
            time.sleep(0.05)
        waiter_calls = calls.read_text()
        time.sleep(0.5)

        assert not _pidfile(home).exists() or int(_pidfile(home).read_text()) == original
        assert _service(home, "status", service=service).returncode == 3
        assert calls.read_text() == waiter_calls
    finally:
        _stop_loop(home)


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
def test_a_hand_started_ensure_loop_runs_without_poller_autostart(
    home: Path, tmp_path: Path
) -> None:
    calls = tmp_path / "waiter-calls"
    service = _deploy_copy(tmp_path, calls)
    (home / ".config/factory/waiter-autostart-claude-bot").touch()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "ORCH_POLL_ENSURE_SEC": "0.1",
           "ORCH_CRONTAB": str(home / "no-such-crontab")}
    loop = subprocess.Popen([str(service.parent / "poller-ensure-loop.sh")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while not (calls.exists() and calls.read_text().count("claude-bot ensure") >= 3):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert loop.poll() is None
        assert _pidfile(home).exists()
    finally:
        loop.terminate()
        loop.wait(timeout=10)


def test_ensure_with_crontab_starts_no_ensure_loop(home: Path, tmp_path: Path) -> None:
    calls = tmp_path / "waiter-calls"
    service = _deploy_copy(tmp_path, calls)
    (home / ".config/factory/poller-autostart").touch()
    try:
        result = _service(home, "ensure", service=service, cron=tmp_path / "crontab.txt")
        assert result.returncode == 0, result.stderr
        assert "crontab not found" not in result.stderr
        time.sleep(0.5)
        assert not (home / ".local/state/factory/poller-ensure-loop.pid").exists()
    finally:
        _stop_loop(home)


def _copy_in(tmp_path: Path, phase: str) -> Path:
    status = tmp_path / "sand-copy-in-status.json"
    status.write_text(f'{{"phase": "{phase}", "percent": 96}}', encoding="utf-8")
    return status


def test_restore_waits_for_the_copy_in_and_keeps_the_db_it_brings_back(
    home: Path, tmp_path: Path
) -> None:
    database = _lost_db(home)
    calls = home / "calls.txt"
    status = _copy_in(tmp_path, "copying")

    def finish_copy_in() -> None:
        time.sleep(1.5)
        database.parent.mkdir(parents=True, exist_ok=True)
        database.write_bytes(b"copied in")
        status.write_text('{"phase": "done"}', encoding="utf-8")

    import threading

    copier = threading.Thread(target=finish_copy_in)
    copier.start()
    result = _service(
        home, "ensure", FAKE_RESTORE="0", FAKE_ORCH_CALLS=str(calls),
        ORCH_COPY_IN_STATUS=str(status), ORCH_COPY_IN_WAIT="30", ORCH_COPY_IN_POLL_SEC="0.2",
    )
    copier.join()

    assert result.returncode == 0, result.stderr
    assert "box copy-in is still running (phase copying)" in result.stderr
    assert "RESTORED" not in result.stderr
    assert not calls.exists()
    assert database.read_bytes() == b"copied in"
    assert _pidfile(home).exists()


@pytest.mark.parametrize("attempt", range(5))
def test_restore_never_waits_less_than_the_copy_in_bound(
    home: Path, tmp_path: Path, attempt: int
) -> None:
    database = _lost_db(home)
    status = _copy_in(tmp_path, "copying")

    started = time.monotonic()
    result = _service(
        home, "ensure", FAKE_RESTORE="0", FAKE_ORCH_CALLS=str(home / "calls.txt"),
        ORCH_COPY_IN_STATUS=str(status), ORCH_COPY_IN_WAIT="1", ORCH_COPY_IN_POLL_SEC="0.05",
    )

    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started >= 1
    assert database.parent.is_dir()


def test_restore_waits_a_bounded_time_for_a_stuck_copy_in(home: Path, tmp_path: Path) -> None:
    database = _lost_db(home)
    calls = home / "calls.txt"
    status = _copy_in(tmp_path, "copying")

    started = time.monotonic()
    result = _service(
        home, "ensure", FAKE_RESTORE="0", FAKE_ORCH_CALLS=str(calls),
        ORCH_COPY_IN_STATUS=str(status), ORCH_COPY_IN_WAIT="2", ORCH_COPY_IN_POLL_SEC="0.2",
    )

    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started >= 2
    assert "box copy-in did not finish within 2s; restoring orch.db anyway" in result.stderr
    assert "RESTORED orch.db from backup" in result.stderr
    assert calls.read_text() == f"restore-db --db {database}\n"


@pytest.mark.parametrize("phase", ["done", None])
def test_restore_does_not_wait_when_the_copy_in_is_done_or_absent(
    home: Path, tmp_path: Path, phase: str | None
) -> None:
    status = _copy_in(tmp_path, phase) if phase else tmp_path / "no-status.json"

    started = time.monotonic()
    result = _service(
        home, "ensure", FAKE_RESTORE="0", ORCH_COPY_IN_STATUS=str(status),
        ORCH_COPY_IN_WAIT="30", ORCH_COPY_IN_POLL_SEC="0.2",
    )

    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 10
    assert "copy-in" not in result.stderr
    assert "RESTORED orch.db from backup" in result.stderr
