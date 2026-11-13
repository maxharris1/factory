"""A restart whose stop fails leaves no stop hold of its own (#107 review).

Each service script's `restart` is `stop` then `start`, and `stop` writes the
*.stopped hold before it waits. When that wait times out, the hold must not
stay behind: self-heal and `ensure` read it as an operator's stop and would
never retry or escalate. An operator's own hold is kept.

Nothing here leaves a temporary HOME: `orch` is a fake that ignores SIGTERM
and is SIGKILLed at teardown.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from lib import heal

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
STATE = Path(".local/state/factory")

FAKE_ORCH = """#!/usr/bin/env python3
import os, signal, sys, time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
with (Path(os.environ["HOME"]) / "starts").open("a") as handle:
    handle.write(f"{os.getpid()}\\n")
args = sys.argv[1:]
if args[:2] == ["poll", "--pidfile"]:
    Path(args[2]).write_text(f"{os.getpid()}\\n")
while True:
    time.sleep(0.1)
"""

SERVICES = {
    "poller": ([str(DEPLOY / "poller-service.sh")], "poller.stopped"),
    "waiter": ([str(DEPLOY / "waiter-service.sh"), "codex-bot"], "waiter-codex-bot.stopped"),
    "linear-webhook": ([str(DEPLOY / "linear-webhook-service.sh")], "linear-webhook.stopped"),
}


@pytest.fixture
def home(tmp_path: Path):
    home = tmp_path / "home"
    config = home / ".config" / "factory"
    config.mkdir(parents=True)
    for name in ("webhook.env", "orch-poll.env"):
        (config / name).write_text("", encoding="utf-8")
    orch = home / ".local" / "bin" / "orch"
    orch.parent.mkdir(parents=True)
    orch.write_text(FAKE_ORCH, encoding="utf-8")
    orch.chmod(0o755)
    yield home
    for pid in _starts(home):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "ORCH_CRONTAB": str(home / "no-such-crontab"),
        "ORCH_POLL_STOP_WAIT": "1",
        "ORCH_WAITER_STOP_WAIT": "1",
        "ORCH_LINEAR_STOP_WAIT": "1",
    }


def _service(home: Path, service: str, command: str) -> subprocess.CompletedProcess:
    argv, _ = SERVICES[service]
    return subprocess.run(["bash", *argv, command], env=_env(home), text=True,
                          capture_output=True, timeout=60, check=False)


def _starts(home: Path) -> list[int]:
    path = home / "starts"
    return [int(line) for line in path.read_text().split()] if path.exists() else []


def _wait_for(predicate, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def _hold(home: Path, service: str) -> Path:
    return home / STATE / SERVICES[service][1]


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_a_restart_whose_stop_times_out_leaves_no_hold(home: Path, service: str) -> None:
    assert _service(home, service, "start").returncode == 0
    _wait_for(lambda: _starts(home))

    restarted = _service(home, service, "restart")

    assert restarted.returncode != 0
    assert "did not stop within 1s" in restarted.stderr
    assert not _hold(home, service).exists()


@pytest.mark.parametrize("service", sorted(SERVICES))
def test_a_failed_restart_keeps_an_operators_hold(home: Path, service: str) -> None:
    assert _service(home, service, "start").returncode == 0
    _wait_for(lambda: _starts(home))
    assert _service(home, service, "stop").returncode != 0
    assert _hold(home, service).exists()

    assert _service(home, service, "restart").returncode != 0

    assert _hold(home, service).exists()


def test_self_heal_retries_and_escalates_a_restart_that_cannot_stop(home: Path) -> None:
    config = home / ".config" / "factory"
    state = home / STATE
    (config / "waiter-autostart-codex-bot").touch()
    assert _service(home, "waiter", "start").returncode == 0
    _wait_for(lambda: _starts(home))
    label = "waiter codex-bot @ box"
    report = {"known": True, "code_version": "222222222222", "components": [
        {"component": "waiter", "bot": "codex-bot", "device": "box", "configured": True,
         "label": label, "pid": 1234, "started_at": 0, "state": "up",
         "stale_code": True, "version": "111111111111"}]}
    passes = []
    for now in (1000, 1120, 1240, 1360):
        facts = heal.facts_for("box", config, state, None, now)
        actions = heal.heal(report, facts=facts, state_dir=state, deploy=DEPLOY,
                            webhook=heal.Webhook(), environ=_env(home), now=now)
        passes.append([(a.kind, a.reason) for a in actions])

    assert all((heal.SKIP, "held stopped") not in actions for actions in passes)
    assert [kind for kind, _ in passes[0]] == [heal.RESTART]
    assert heal.ESCALATE in [kind for kind, _ in passes[-1]]
    saved = json.loads((state / heal.STATE_NAME).read_text())["keys"][label]
    assert len(saved["failures"]) == 3
    assert saved["escalated_at"] == 1360
    assert f"escalate {label}" in (state / heal.LOG_NAME).read_text()
