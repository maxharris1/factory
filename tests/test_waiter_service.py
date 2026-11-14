"""Waiter loops run under a restart supervisor (#80).

deploy/waiter-service.sh runs `orch wait <bot> --autostart` under a small
supervisor loop (as the local Linear webhook service runs its listener), so
a waiter that is killed or crashes comes back by itself. A systemd user
template unit is shipped for hosts with a user session. Nothing here
installs or starts anything outside a temporary HOME.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "waiter-service.sh"
UNIT = ROOT / "deploy" / "orch-waiter@.service"

FAKE_ORCH = """#!/usr/bin/env python3
# Stands in for `orch wait <bot> --autostart`: records each start, runs until
# SIGTERM (exit 0, like the real waiter). Like the real waiter
# (lib/waiter.py hold_waiter_lock) it holds waiter-<bot>.lock beside ORCH_DB
# for its whole life, with its pid in it, and exits 2 if another holds it.
import fcntl, os, signal, sys, time
from pathlib import Path

args = sys.argv[1:]
assert args[0] == "wait" and args[2:] == ["--autostart"], args
starts = Path(os.environ["HOME"]) / f"starts-{args[1]}"
with starts.open("a") as handle:
    handle.write(f"{os.getpid()} {os.environ.get('ORCH_DB', '')} "
                 f"{os.environ.get('ORCH_WAITER_DRAIN', '')}\\n")
if os.environ.get("FAKE_WAITER_FAIL"):
    raise SystemExit(2)
db = Path(os.environ["ORCH_DB"])
if db.parent.is_dir():
    lock = os.open(db.parent / f"waiter-{args[1]}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        with (Path(os.environ["HOME"]) / f"refused-{args[1]}").open("a") as handle:
            handle.write(f"{os.getpid()}\\n")
        raise SystemExit(2)
    os.ftruncate(lock, 0)
    os.write(lock, f"{os.getpid()}\\n".encode())
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.2)
"""


@pytest.fixture
def home(tmp_path: Path):
    home = tmp_path / "home"
    orch = home / ".local" / "bin" / "orch"
    orch.parent.mkdir(parents=True)
    orch.write_text(FAKE_ORCH, encoding="utf-8")
    orch.chmod(0o755)
    (home / ".config" / "factory").mkdir(parents=True)
    yield home
    for bot in ("claude-bot", "codex-bot"):
        _service(home, bot, "stop")
    for pid in _all_starts(home):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _service(home: Path, *args: str, cron: Path | None = None, **extra: str):
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "ORCH_WAITER_RESTART_SEC": "0.2",
        "ORCH_WAITER_START_WAIT": "10",
        "ORCH_WAITER_STOP_WAIT": "10",
        "ORCH_CRONTAB": str(cron) if cron is not None else str(home / "no-such-crontab"),
        **extra,
    }
    return subprocess.run(
        ["bash", str(SERVICE), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _starts(home: Path, bot: str = "claude-bot") -> list[int]:
    path = home / f"starts-{bot}"
    if not path.exists():
        return []
    return [int(line.split()[0]) for line in path.read_text().splitlines() if line.strip()]


def _all_starts(home: Path) -> list[int]:
    return [pid for bot in ("claude-bot", "codex-bot") for pid in _starts(home, bot)]


def _wait_for(predicate, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def _supervisor(home: Path, bot: str = "claude-bot") -> int:
    return int((home / ".local/state/factory" / f"waiter-{bot}.pid").read_text().strip())


def test_a_killed_waiter_is_restarted_by_its_supervisor(home: Path) -> None:
    started = _service(home, "claude-bot", "start")
    assert started.returncode == 0, started.stderr
    first = _wait_for(lambda: _starts(home))[0]
    supervisor = _supervisor(home)
    assert alive(first) and alive(supervisor)

    os.kill(first, signal.SIGKILL)
    second = _wait_for(lambda: len(_starts(home)) >= 2 and _starts(home)[1])
    assert second != first and alive(second)
    assert _supervisor(home) == supervisor  # the same supervisor brought it back

    # It keeps doing so.
    os.kill(second, signal.SIGKILL)
    third = _wait_for(lambda: len(_starts(home)) >= 3 and _starts(home)[2])
    assert alive(third)
    log = (home / ".local/state/factory/waiter-claude-bot.log").read_text()
    assert "restarting" in log


def test_stop_ends_the_supervisor_and_the_waiter_without_a_restart(home: Path) -> None:
    assert _service(home, "claude-bot", "start").returncode == 0
    waiter = _wait_for(lambda: _starts(home))[0]
    supervisor = _supervisor(home)
    stopped = _service(home, "claude-bot", "stop")
    assert stopped.returncode == 0, stopped.stderr
    _wait_for(lambda: not alive(waiter) and not alive(supervisor))
    time.sleep(0.6)
    assert len(_starts(home)) == 1
    status = _service(home, "claude-bot", "status")
    assert status.returncode == 3
    assert "stopped" in status.stdout
    # ensure respects a stop; start clears it.
    assert _service(home, "claude-bot", "ensure").returncode == 0
    time.sleep(0.4)
    assert len(_starts(home)) == 1
    assert _service(home, "claude-bot", "start").returncode == 0
    _wait_for(lambda: len(_starts(home)) == 2)


def test_start_is_single_instance_per_bot(home: Path) -> None:
    assert _service(home, "claude-bot", "start").returncode == 0
    _wait_for(lambda: _starts(home))
    again = _service(home, "claude-bot", "start")
    assert again.returncode == 0
    assert "already running" in again.stdout
    assert _service(home, "codex-bot", "start").returncode == 0
    _wait_for(lambda: _starts(home, "codex-bot"))
    time.sleep(0.4)
    assert len(_starts(home)) == 1
    assert _service(home, "claude-bot", "status").returncode == 0
    assert _service(home, "codex-bot", "status").returncode == 0


def test_ensure_brings_back_a_killed_supervisor(home: Path) -> None:
    assert _service(home, "claude-bot", "ensure").returncode == 0
    waiter = _wait_for(lambda: _starts(home))[0]
    supervisor = _supervisor(home)
    os.kill(supervisor, signal.SIGKILL)
    os.kill(waiter, signal.SIGKILL)
    _wait_for(lambda: not alive(supervisor))
    assert _service(home, "claude-bot", "status").returncode == 3
    assert _service(home, "claude-bot", "ensure").returncode == 0
    _wait_for(lambda: len(_starts(home)) == 2)


def test_a_supervisor_only_kill_leaves_one_waiter_and_stop_still_reaches_it(
    home: Path,
) -> None:
    # Codex PR #82 r1 F1.6: SIGKILL only the supervisor. Its waiter lives on
    # holding the bot's lock; the next supervisor's waiter is refused (there
    # is never a second running waiter), and stop still stops the orphan.
    assert _service(home, "claude-bot", "start").returncode == 0
    orphan = _wait_for(lambda: _starts(home))[0]
    supervisor = _supervisor(home)
    os.kill(supervisor, signal.SIGKILL)
    _wait_for(lambda: not alive(supervisor))
    assert alive(orphan)

    assert _service(home, "claude-bot", "ensure").returncode == 0
    _wait_for(lambda: len(_starts(home)) >= 2)
    refused = home / "refused-claude-bot"
    _wait_for(lambda: refused.exists() and refused.read_text().strip())
    time.sleep(0.5)
    live = [pid for pid in _starts(home) if alive(pid)]
    assert live == [orphan]
    lock = home / ".local/state/factory/waiter-claude-bot.lock"
    assert lock.read_text().strip() == str(orphan)

    stopped = _service(home, "claude-bot", "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert "stopped orphaned waiter" in stopped.stdout
    _wait_for(lambda: not alive(orphan))
    time.sleep(0.6)
    assert [pid for pid in _starts(home) if alive(pid)] == []


def test_stop_never_signals_a_lock_pid_that_is_not_this_bots_waiter(home: Path) -> None:
    state = home / ".local/state/factory"
    state.mkdir(parents=True)
    (state / "waiter-claude-bot.db").write_text(f"{state}/orch.db\n", encoding="utf-8")
    bystander = subprocess.Popen(["sleep", "30"])
    try:
        (state / "waiter-claude-bot.lock").write_text(f"{bystander.pid}\n", encoding="utf-8")
        assert _service(home, "claude-bot", "stop").returncode == 0
        time.sleep(0.3)
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_a_waiter_that_keeps_failing_is_retried_with_backoff(home: Path) -> None:
    assert _service(home, "claude-bot", "start", FAKE_WAITER_FAIL="1").returncode == 0
    _wait_for(lambda: len(_starts(home)) >= 3, 20)
    log = (home / ".local/state/factory/waiter-claude-bot.log").read_text()
    assert "exited 2" in log


def test_the_supervisor_passes_orch_db_from_waiter_env(home: Path) -> None:
    (home / ".config/factory/waiter.env").write_text(
        "ORCH_DB=/srv/factory/orch.db\n", encoding="utf-8"
    )
    assert _service(home, "claude-bot", "start").returncode == 0
    _wait_for(lambda: _starts(home))
    line = (home / "starts-claude-bot").read_text().splitlines()[0]
    assert line.split()[1] == "/srv/factory/orch.db"


def test_the_default_orch_db_is_the_shared_state_database(home: Path) -> None:
    assert _service(home, "claude-bot", "start").returncode == 0
    _wait_for(lambda: _starts(home))
    line = (home / "starts-claude-bot").read_text().splitlines()[0]
    assert line.split()[1] == str(home / ".local/state/factory/orch.db")


def test_start_clears_the_drain_marker_and_names_it_to_the_waiter(home: Path) -> None:
    """#219: self-heal's restart after a drain (stop, then start) clears the
    marker, and the waiter is told where self-heal writes it."""
    state = home / ".local/state/factory"
    state.mkdir(parents=True)
    marker = state / "waiter-claude-bot.drain"
    marker.write_text("0123456789ab\n", encoding="utf-8")
    assert _service(home, "claude-bot", "start").returncode == 0
    _wait_for(lambda: _starts(home))
    assert not marker.exists()
    line = (home / "starts-claude-bot").read_text().splitlines()[0]
    assert line.split()[2] == str(marker)


@pytest.mark.parametrize("bot", ["grok-bot", "claude", "", "../x", "claude-bot;id"])
def test_only_a_harness_bot_can_be_supervised(home: Path, bot: str) -> None:
    result = _service(home, bot, "start")
    assert result.returncode == 2
    assert _starts(home) == []


def test_install_autostart_adds_tagged_cron_lines_per_bot_and_remove_drops_them(
    home: Path, tmp_path: Path
) -> None:
    table = tmp_path / "crontab.txt"
    table.write_text("0 1 * * * keep-me\n", encoding="utf-8")
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
    for _ in range(2):
        result = _service(home, "claude-bot", "install-autostart", cron=crontab)
        assert result.returncode == 0, result.stderr
    assert _service(home, "codex-bot", "install-autostart", cron=crontab).returncode == 0
    lines = table.read_text().splitlines()
    assert "0 1 * * * keep-me" in lines
    claude = [line for line in lines if line.endswith("# factory-orch-waiter-claude-bot")]
    codex = [line for line in lines if line.endswith("# factory-orch-waiter-codex-bot")]
    assert len(claude) == 2 and len(codex) == 2
    assert any(line.startswith("@reboot ") for line in claude)
    assert any(line.startswith("*/5 * * * * ") for line in claude)
    assert all("waiter-service.sh claude-bot ensure" in line for line in claude)
    assert (home / ".config/factory/waiter-autostart-claude-bot").exists()

    assert _service(home, "claude-bot", "remove-autostart", cron=crontab).returncode == 0
    lines = table.read_text().splitlines()
    assert not [line for line in lines if "factory-orch-waiter-claude-bot" in line]
    assert len([line for line in lines if "factory-orch-waiter-codex-bot" in line]) == 2
    assert "0 1 * * * keep-me" in lines
    assert not (home / ".config/factory/waiter-autostart-claude-bot").exists()


def test_systemd_unit_restarts_the_waiter_and_spares_running_jobs() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    assert "ExecStart=%h/.local/bin/orch wait %i --autostart" in unit
    assert "Restart=always" in unit
    assert "RestartSec=5" in unit
    # A restart kills the waiter only, never the `orch job run` it started.
    assert "KillMode=process" in unit
    assert "Environment=PYTHONUNBUFFERED=1" in unit
    assert "EnvironmentFile=-%h/.config/factory/waiter.env" in unit
    assert "webhook.env" not in unit


def test_deploy_files_hold_no_credentials_and_install_nothing_on_their_own() -> None:
    for path in (SERVICE, UNIT, ROOT / "config" / "waiter.example.yml"):
        text = path.read_text(encoding="utf-8").lower()
        for word in ("token=", "password", "secret", "api_key", "auth.json", "webhook.env"):
            assert word not in text, (path, word)
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "waiter-service.sh" in install  # only the profile hook names it
    assert "systemctl" not in install


def test_install_profile_hook_ensures_each_autostarted_waiter(tmp_path: Path) -> None:
    from tests.test_deploy import _install, _install_environment

    environment = _install_environment(tmp_path, "venv")
    assert _install(environment).returncode == 0
    home = Path(environment["HOME"])
    profile = (home / ".config/factory/profile.sh").read_text(encoding="utf-8")
    assert "waiter-autostart-" in profile
    assert "waiter-service.sh" in profile
    marker = home / "ensured"
    fake = tmp_path / "fake-service.sh"
    fake.write_text(f"#!/bin/sh\necho \"$1 $2\" >> {marker}\n", encoding="utf-8")
    fake.chmod(0o755)
    patched = profile.replace(str(ROOT / "deploy" / "waiter-service.sh"), str(fake))
    (home / ".config/factory/waiter-autostart-codex-bot").touch()
    (home / ".config/factory/waiter-autostart-bogus").touch()
    subprocess.run(["sh", "-c", patched + "\nwait\nsleep 0.3\n"],
                   env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, check=True, timeout=30)
    _wait_for(lambda: marker.exists())
    assert marker.read_text().split("\n")[0] == "codex-bot ensure"
    assert "bogus" not in marker.read_text()
