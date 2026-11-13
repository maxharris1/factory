"""deploy/linear-webhook-service.sh supervises the listener and its tunnel (#107).

Nothing here installs or starts anything outside a temporary HOME: `orch`
and `ssh` are fakes that record their argv and run until SIGTERM.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "linear-webhook-service.sh"
STATE = Path(".local/state/factory")

FAKE = """#!/usr/bin/env python3
import os, signal, sys, time
from pathlib import Path

name = Path(sys.argv[0]).name
with (Path(os.environ["HOME"]) / f"starts-{name}").open("a") as handle:
    handle.write(f"{os.getpid()}\\t{os.environ.get('ORCH_DB', '')}\\t{' '.join(sys.argv[1:])}\\n")
if os.environ.get("FAKE_FAIL") == name:
    raise SystemExit(2)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.2)
"""


@pytest.fixture
def home(tmp_path: Path):
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("orch", "ssh"):
        fake = bin_dir / name
        fake.write_text(FAKE, encoding="utf-8")
        fake.chmod(0o755)
    (home / ".config" / "factory").mkdir(parents=True)
    yield home
    _service(home, "stop")
    for name in ("orch", "ssh"):
        for pid in _starts(home, name):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _service(home: Path, *args: str, cron: Path | None = None, **extra: str):
    env = {
        "HOME": str(home),
        "PATH": f"{home / '.local' / 'bin'}:/usr/bin:/bin",
        "ORCH_LINEAR_RESTART_SEC": "0.2",
        "ORCH_LINEAR_START_WAIT": "10",
        "ORCH_LINEAR_STOP_WAIT": "10",
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


def _records(home: Path, name: str) -> list[list[str]]:
    path = home / f"starts-{name}"
    if not path.exists():
        return []
    return [line.split("\t") for line in path.read_text().splitlines() if line.strip()]


def _starts(home: Path, name: str = "orch") -> list[int]:
    return [int(record[0]) for record in _records(home, name)]


def _wait_for(predicate, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def _supervisor(home: Path) -> int:
    return int((home / STATE / "linear-webhook.pid").read_text().strip())


def _tunnel_env(home: Path, text: str) -> None:
    (home / ".config/factory/linear-webhook.env").write_text(text, encoding="utf-8")


def test_start_runs_the_listener_and_status_reports_it(home: Path) -> None:
    started = _service(home, "start")
    assert started.returncode == 0, started.stderr
    record = _wait_for(lambda: _records(home, "orch"))[0]
    assert record[2] == "linear-webhook serve"
    assert record[1] == str(home / STATE / "orch.db")
    assert alive(int(record[0])) and alive(_supervisor(home))
    status = _service(home, "status")
    assert status.returncode == 0
    assert status.stdout.startswith("running (pid ")
    again = _service(home, "start")
    assert again.returncode == 0
    assert "already running" in again.stdout
    time.sleep(0.4)
    assert len(_starts(home)) == 1


def test_a_killed_listener_is_restarted_by_its_supervisor(home: Path) -> None:
    assert _service(home, "start").returncode == 0
    first = _wait_for(lambda: _starts(home))[0]
    supervisor = _supervisor(home)
    os.kill(first, signal.SIGKILL)
    second = _wait_for(lambda: len(_starts(home)) >= 2 and _starts(home)[1])
    assert second != first and alive(second)
    assert _supervisor(home) == supervisor
    assert "restarting in" in (home / STATE / "linear-webhook.log").read_text()


def test_stop_ends_everything_and_holds_it_until_start(home: Path) -> None:
    _tunnel_env(home, "LINEAR_TUNNEL_SSH_ALIAS=relay\nLINEAR_TUNNEL_REMOTE_PORT=9443\n")
    assert _service(home, "start").returncode == 0
    listener = _wait_for(lambda: _starts(home))[0]
    tunnel = _wait_for(lambda: _starts(home, "ssh"))[0]
    supervisor = _supervisor(home)
    stopped = _service(home, "stop")
    assert stopped.returncode == 0, stopped.stderr
    _wait_for(lambda: not any(alive(pid) for pid in (listener, tunnel, supervisor)))
    time.sleep(0.6)
    assert len(_starts(home)) == 1 and len(_starts(home, "ssh")) == 1
    status = _service(home, "status")
    assert status.returncode == 3
    assert "stopped" in status.stdout
    held = _service(home, "ensure")
    assert (held.returncode, held.stdout, held.stderr) == (0, "", "")
    time.sleep(0.4)
    assert len(_starts(home)) == 1
    assert _service(home, "start").returncode == 0
    _wait_for(lambda: len(_starts(home)) == 2)


def test_restart_replaces_the_supervisor_and_its_listener(home: Path) -> None:
    assert _service(home, "start").returncode == 0
    first = _wait_for(lambda: _starts(home))[0]
    supervisor = _supervisor(home)
    restarted = _service(home, "restart")
    assert restarted.returncode == 0, restarted.stderr
    _wait_for(lambda: len(_starts(home)) == 2)
    assert not alive(first) and not alive(supervisor)
    assert _supervisor(home) != supervisor
    assert not (home / STATE / "linear-webhook.stopped").exists()


def test_ensure_is_silent_when_running_and_starts_it_when_not(home: Path) -> None:
    first = _service(home, "ensure")
    assert first.returncode == 0, first.stderr
    assert "started" in first.stdout
    _wait_for(lambda: _starts(home))
    for _ in range(3):
        quiet = _service(home, "ensure")
        assert (quiet.returncode, quiet.stdout, quiet.stderr) == (0, "", "")
    assert len(_starts(home)) == 1
    supervisor = _supervisor(home)
    os.kill(supervisor, signal.SIGKILL)
    _wait_for(lambda: not alive(supervisor))
    assert _service(home, "ensure").returncode == 0
    _wait_for(lambda: _supervisor(home) != supervisor and alive(_supervisor(home)))


def _lose_the_supervisor(home: Path) -> tuple[int, int]:
    _tunnel_env(home, "LINEAR_TUNNEL_SSH_ALIAS=relay\nLINEAR_TUNNEL_REMOTE_PORT=9443\n")
    before = len(_starts(home))
    assert _service(home, "start").returncode == 0
    listener = _wait_for(lambda: _starts(home)[before:])[0]
    tunnel = _wait_for(lambda: _starts(home, "ssh"))[0]
    supervisor = _supervisor(home)
    os.kill(supervisor, signal.SIGKILL)
    _wait_for(lambda: not alive(supervisor))
    return listener, tunnel


@pytest.fixture
def bystander(home: Path):
    """A listener this service did not start, in a process group of its own."""
    proc = subprocess.Popen([str(home / ".local/bin/orch"), "linear-webhook", "serve"],
                            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                            start_new_session=True)
    _wait_for(lambda: proc.pid in _starts(home))
    yield proc.pid
    proc.kill()
    proc.wait()


@pytest.mark.parametrize("command", ["ensure", "start", "restart"])
def test_a_lost_supervisors_children_are_replaced_not_doubled(
    home: Path, bystander: int, command: str
) -> None:
    listener, tunnel = _lose_the_supervisor(home)

    replaced = _service(home, command)

    assert replaced.returncode == 0, replaced.stderr
    assert not alive(listener) and not alive(tunnel)
    _wait_for(lambda: len(_starts(home, "ssh")) == 2)
    live = [pid for pid in _starts(home) if alive(pid)]
    assert sorted(live) == sorted([bystander, _starts(home)[-1]])
    assert alive(bystander)


def test_stop_ends_a_lost_supervisors_children(home: Path, bystander: int) -> None:
    listener, tunnel = _lose_the_supervisor(home)

    stopped = _service(home, "stop")

    assert stopped.returncode == 0, stopped.stderr
    assert not alive(listener) and not alive(tunnel)
    time.sleep(0.6)
    assert [pid for pid in _starts(home) if alive(pid)] == [bystander]
    assert _starts(home, "ssh") == [tunnel]


def test_no_tunnel_without_both_settings(home: Path) -> None:
    _tunnel_env(home, "LINEAR_TUNNEL_SSH_ALIAS=relay\n")
    assert _service(home, "start").returncode == 0
    _wait_for(lambda: _starts(home))
    time.sleep(0.5)
    assert _starts(home, "ssh") == []


def test_the_tunnel_runs_with_the_exact_ssh_argv(home: Path) -> None:
    _tunnel_env(home, "LINEAR_TUNNEL_SSH_ALIAS=relay\nLINEAR_TUNNEL_REMOTE_PORT=9443\n")
    assert _service(home, "start").returncode == 0
    record = _wait_for(lambda: _records(home, "ssh"))[0]
    assert record[2] == (
        "-N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "
        "-R 9443:127.0.0.1:8787 relay"
    )
    tunnel = int(record[0])
    os.kill(tunnel, signal.SIGKILL)
    _wait_for(lambda: len(_starts(home, "ssh")) >= 2)
    assert len(_starts(home)) == 1


def test_the_tunnel_bind_can_be_set(home: Path) -> None:
    _tunnel_env(
        home,
        "LINEAR_TUNNEL_SSH_ALIAS=relay\nLINEAR_TUNNEL_REMOTE_PORT=9443\n"
        "LINEAR_TUNNEL_BIND=127.0.0.1:9000\n",
    )
    assert _service(home, "start").returncode == 0
    record = _wait_for(lambda: _records(home, "ssh"))[0]
    assert record[2].endswith("-R 9443:127.0.0.1:9000 relay")


def test_a_malformed_tunnel_setting_is_refused_without_echoing_it(home: Path) -> None:
    _tunnel_env(home, "LINEAR_TUNNEL_SSH_ALIAS='relay;id'\nLINEAR_TUNNEL_REMOTE_PORT=9443\n")
    assert _service(home, "start").returncode == 0
    _wait_for(lambda: _starts(home))
    log = _wait_for(lambda: "tunnel not started" in (home / STATE / "linear-webhook.log").read_text()
                    and (home / STATE / "linear-webhook.log").read_text())
    assert "relay;id" not in log
    assert _starts(home, "ssh") == []


def test_every_line_the_script_writes_is_utc_timestamped(home: Path) -> None:
    started = _service(home, "start", FAKE_FAIL="orch")
    assert started.returncode == 0, started.stderr
    _wait_for(lambda: len(_starts(home)) >= 3, 20)
    assert _service(home, "stop").returncode == 0
    stamp = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ linear-webhook-service: ")
    log = (home / STATE / "linear-webhook.log").read_text().splitlines()
    assert any("exited 2; restarting" in line for line in log)
    assert log and all(stamp.match(line) for line in log), log
    assert stamp.match(started.stdout)


def test_install_autostart_adds_tagged_cron_lines_and_remove_drops_them(
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
        result = _service(home, "install-autostart", cron=crontab)
        assert result.returncode == 0, result.stderr
    lines = table.read_text().splitlines()
    tagged = [line for line in lines if line.endswith("# factory-linear-webhook")]
    assert len(tagged) == 2
    assert any(line.startswith("@reboot ") for line in tagged)
    assert any(line.startswith("*/5 * * * * ") for line in tagged)
    assert all("linear-webhook-service.sh ensure" in line for line in tagged)
    assert "0 1 * * * keep-me" in lines
    assert (home / ".config/factory/linear-webhook-autostart").exists()

    assert _service(home, "remove-autostart", cron=crontab).returncode == 0
    lines = table.read_text().splitlines()
    assert lines == ["0 1 * * * keep-me"]
    assert not (home / ".config/factory/linear-webhook-autostart").exists()


def test_an_unreadable_crontab_changes_nothing(home: Path, tmp_path: Path) -> None:
    crontab = tmp_path / "crontab"
    crontab.write_text("#!/bin/sh\necho 'crontab: permission denied' >&2\nexit 1\n",
                       encoding="utf-8")
    crontab.chmod(0o755)
    result = _service(home, "install-autostart", cron=crontab)
    assert result.returncode == 1
    assert "cannot read the crontab" in result.stderr
    assert not (home / ".config/factory/linear-webhook-autostart").exists()


def test_an_unknown_command_is_a_usage_error(home: Path) -> None:
    result = _service(home, "serve")
    assert result.returncode == 2
    assert "usage:" in result.stderr
