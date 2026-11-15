from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
GUARDED = ("tests/test_deploy.py", "tests/test_waiter_service.py")
SENTINELS = {"waiter-claude-bot.pid": "1599481", "waiter-codex-bot.pid": "1599548",
             "poller.pid": "1599600", "self-heal.pid": "1599700"}


@dataclass(frozen=True)
class GuardedRun:
    returncode: int
    output: str
    changed: dict[str, tuple[bytes | None, bytes | None]]
    strays: list[str]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _run_guarded(tests: tuple[str, ...], scratch: Path) -> GuardedRun:
    home = scratch / "home"
    live = home / ".local/state/factory"
    live.mkdir(parents=True)
    for name, pid in SENTINELS.items():
        (live / name).write_text(f"{pid}\n", encoding="utf-8")
    before = _snapshot(home)
    basetemp = Path(tempfile.mkdtemp(prefix="pytest-live-guard-", dir="/tmp"))
    report = scratch / "strays.json"
    environment = {
        **os.environ,
        "HOME": str(home),
        "ORCH_STATE_DIR": str(live),
        "ORCH_DB": str(live / "orch.db"),
        "ORCH_WAITER_DRAIN": str(live / "waiter-claude-bot.drain"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "tests.subreaper", str(report), "--",
             sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"--basetemp={basetemp}", *tests],
            cwd=ROOT, env=environment, text=True, capture_output=True, timeout=900, check=False,
        )
    finally:
        shutil.rmtree(basetemp, ignore_errors=True)
    after = _snapshot(home)
    changed = {name: (before.get(name), after.get(name))
               for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    return GuardedRun(result.returncode, result.stdout[-4000:] + result.stderr[-2000:],
                      changed, json.loads(report.read_text(encoding="utf-8")))


@pytest.mark.timeout(900)
def test_the_deploy_and_waiter_tests_leave_the_live_state_alone(tmp_path: Path) -> None:
    run = _run_guarded(GUARDED, tmp_path)

    assert run.returncode == 0, run.output
    assert run.changed == {}
    assert run.strays == []


LIVE_STATE = "Path.home() / '.local/state/factory'"
MUTATIONS = {
    "delete": (f"({LIVE_STATE} / 'poller.pid').unlink()", "poller.pid"),
    "overwrite": (f"({LIVE_STATE} / 'poller.pid').write_text('999999999\\n')", "poller.pid"),
    "add": (f"({LIVE_STATE} / 'new-supervisor.pid').write_text('999999999\\n')",
            "new-supervisor.pid"),
}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_the_guard_fails_a_run_that_touches_a_live_pidfile(tmp_path: Path, mutation: str) -> None:
    probe = tmp_path / f"test_{mutation}.py"
    probe.write_text("from pathlib import Path\n\n\ndef test_mutate():\n"
                     f"    {MUTATIONS[mutation][0]}\n", encoding="utf-8")

    run = _run_guarded((str(probe),), tmp_path)

    assert run.returncode == 0, run.output
    assert list(run.changed) == [f".local/state/factory/{MUTATIONS[mutation][1]}"]


def test_the_guard_fails_a_run_that_leaves_a_supervisor_under_the_real_home(tmp_path: Path) -> None:
    probe = tmp_path / "test_leak.py"
    probe.write_text(
        "import os, pwd, subprocess\n\n\ndef test_leak():\n"
        "    subprocess.Popen(['/bin/bash', '-c', 'sleep 60 & wait', 'deploy/waiter-service.sh',"
        " 'claude-bot', '_supervise'], start_new_session=True,"
        " env={'HOME': pwd.getpwuid(os.getuid()).pw_dir, 'PATH': '/usr/bin:/bin'})\n",
        encoding="utf-8")

    run = _run_guarded((str(probe),), tmp_path)

    assert run.returncode == 0, run.output
    supervisors = [stray for stray in run.strays if "_supervise" in stray]
    assert len(supervisors) == 1, run.strays
    assert f"HOME={REAL_HOME} " in supervisors[0]
    assert not alive(int(supervisors[0].split()[0]))


ENV_ARGUMENT = re.compile(r"env=\{\*\*os\.environ|env=os\.environ|\bos\.environ\.(?:items|copy)\(\)")


def test_the_guarded_tests_never_hand_the_raw_environment_to_a_subprocess() -> None:
    offenders = [
        f"{name}:{number}: {line.strip()}"
        for name in GUARDED
        for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1)
        if ENV_ARGUMENT.search(line)
    ]
    assert offenders == [], "build the env with tests.conftest.sandbox_env:\n" + "\n".join(offenders)
