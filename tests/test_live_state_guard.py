"""#299: the deploy and waiter-service tests leave the live factory alone.

A box job exports ORCH_DB, ORCH_WAITER_DRAIN and friends at the live
~/.local/state/factory. This runs tests/test_deploy.py and
tests/test_waiter_service.py in a child pytest with those names pointed at a
decoy state dir, then checks that neither the decoy nor the real live dir
changed and that nothing the child started is still running.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
LIVE = REAL_HOME / ".local/state/factory"
GUARDED = ("tests/test_deploy.py", "tests/test_waiter_service.py")


def _pidfiles(state: Path) -> dict[str, bytes]:
    if not state.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in sorted(state.glob("*.pid"))}


def _read(pid: int, name: str) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/{name}").read_bytes()
    except OSError:
        return []
    return raw.decode(errors="replace").split("\0")


def _environ(pid: int) -> dict[str, str]:
    return dict(entry.split("=", 1) for entry in _read(pid, "environ") if "=" in entry)


def _home(pid: int) -> str:
    return _environ(pid).get("HOME", "")


def _processes() -> dict[int, tuple[str, dict[str, str]]]:
    found = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            pid = int(entry.name)
            found[pid] = (" ".join(_read(pid, "cmdline")).strip(), _environ(pid))
    return found


def _strays(before: set[int], inner: Path, decoy: Path) -> dict[int, str]:
    strays = {}
    for pid, (argv, environ) in _processes().items():
        if pid in before or pid == os.getpid():
            continue
        home = environ.get("HOME", "")
        state = environ.get("ORCH_STATE_DIR") or f"{home}/.local/state/factory"
        ours = home.startswith((str(inner), str(decoy))) or str(inner) in argv
        live_supervisor = ("_supervise" in argv and Path(home) != REAL_HOME
                           and (Path(state) == LIVE or state.startswith(str(decoy))))
        if ours or live_supervisor:
            strays[pid] = f"{pid} HOME={home} STATE={state} {argv}"
    return strays


def _live_pidfiles_naming_us(before: dict[str, bytes], inner: Path, decoy: Path) -> dict[str, str]:
    changed = {}
    for name, value in _pidfiles(LIVE).items():
        text = value.decode(errors="replace").strip()
        if before.get(name) != value and text.isdigit():
            if _home(int(text)).startswith((str(inner), str(decoy))):
                changed[name] = text
    return changed


def _kill(pids) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_the_deploy_and_waiter_tests_leave_the_live_state_alone(tmp_path: Path) -> None:
    decoy = tmp_path / "decoy"
    state = decoy / "state"
    state.mkdir(parents=True)
    for name, value in (("waiter-claude-bot.pid", "1599481"), ("waiter-codex-bot.pid", "1599548"),
                        ("poller.pid", "1599600"), ("self-heal.pid", "1599700")):
        (state / name).write_text(f"{value}\n", encoding="utf-8")
    decoy_before = {path.name: path.read_bytes() for path in state.iterdir()}
    live_before = _pidfiles(LIVE)
    inner = Path(tempfile.mkdtemp(prefix="pytest-live-guard-", dir="/tmp"))
    environment = {
        **os.environ,
        "ORCH_STATE_DIR": str(state),
        "ORCH_DB": str(state / "orch.db"),
        "ORCH_WAITER_DRAIN": str(state / "waiter-claude-bot.drain"),
        "XDG_STATE_HOME": str(decoy / "xdg-state"),
        "XDG_CONFIG_HOME": str(decoy / "xdg-config"),
    }
    before = set(_processes())
    strays: dict[int, str] = {}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             f"--basetemp={inner}", *GUARDED],
            cwd=ROOT, env=environment, text=True, capture_output=True, timeout=900, check=False,
        )
        assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]

        deadline = time.monotonic() + 10
        while True:
            strays = _strays(before, inner, decoy)
            if not strays or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        assert strays == {}, "\n".join(strays.values())
        assert {path.name: path.read_bytes() for path in state.iterdir()} == decoy_before
        assert not (decoy / "xdg-state").exists() and not (decoy / "xdg-config").exists()
        assert _live_pidfiles_naming_us(live_before, inner, decoy) == {}
    finally:
        _kill(strays or _strays(before, inner, decoy))
        shutil.rmtree(inner, ignore_errors=True)


ENV_ARGUMENT = re.compile(r"env=\{\*\*os\.environ|env=os\.environ|\bos\.environ\.(?:items|copy)\(\)")


def test_the_guarded_tests_never_hand_the_raw_environment_to_a_subprocess() -> None:
    offenders = [
        f"{name}:{number}: {line.strip()}"
        for name in GUARDED
        for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1)
        if ENV_ARGUMENT.search(line)
    ]
    assert offenders == [], "build the env with tests.conftest.sandbox_env:\n" + "\n".join(offenders)
