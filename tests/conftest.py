"""Suite-wide guard: no test may reach a real host over SSH (F2-02), or
GitHub through gh (#191).

A refusing `ssh` is put first on PATH for the whole run. It exits 255 (an
unreachable device) and touches nothing. Tests that exercise remote start
put tests/fake_ssh.py in front of it instead.

The waiter's skill preflight (lib/skill_preflight.py) is a no-op in this
process unless a test overrides `_skill_preflight`, as
tests/test_skill_preflight.py does. A waiter a test starts as its own
process runs the real preflight, so HOME is a session home holding the
pinned skills, installed as install-skills does.

No `ORCH_*`, `FACTORY_*`, `LINEAR_WEBHOOK_*` or `XDG_*` name from the
calling shell reaches a test (#299): a box job exports ORCH_DB and friends
at the live state dir, and a deploy script a test runs would act on it.
"""

from __future__ import annotations

import os
import shutil
import signal
import site
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRUBBED_PREFIXES = ("ORCH_", "FACTORY_", "LINEAR_WEBHOOK_", "XDG_")


@pytest.fixture(autouse=True, scope="session")
def _a_clean_factory_env():
    scrubbed = {name: os.environ.pop(name) for name in list(os.environ)
                if name.startswith(SCRUBBED_PREFIXES)}
    yield
    os.environ.update(scrubbed)


def sandbox_env(home: Path, **extra: str) -> dict[str, str]:
    """The scrubbed environment with HOME, the XDG dirs and the factory state
    dir all under `home`, then `extra`."""
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_RUNTIME_DIR": str(home / ".run"),
        "ORCH_STATE_DIR": str(home / ".local/state/factory"),
        **extra,
    }


def reap_state(home: Path) -> None:
    """Kill every process a pidfile or lock in home's factory state dir names,
    and its process group when it leads one, but only while that process's
    HOME is still `home`: a reused pid is never signalled."""
    state = home / ".local/state/factory"
    pids = set()
    for path in (*state.glob("*.pid"), *state.glob("*.lock")):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text.isdigit():
            pids.add(int(text))
    ours = [pid for pid in pids if _home_of(pid) == str(home)]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in ours:
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
            except OSError:
                pass
        if sig == signal.SIGTERM and ours:
            time.sleep(0.2)


def _home_of(pid: int) -> str | None:
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    for entry in environ:
        if entry.startswith(b"HOME="):
            return entry[5:].decode(errors="replace")
    return None


@pytest.fixture(autouse=True, scope="session")
def _no_real_ssh(tmp_path_factory: pytest.TempPathFactory):
    guard = tmp_path_factory.mktemp("no-real-ssh")
    script = guard / "ssh"
    script.write_text(
        "#!/bin/sh\necho 'tests: real ssh is disabled' >&2\nexit 255\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{guard}{os.pathsep}{previous}"
    yield Path(guard)
    os.environ["PATH"] = previous


@pytest.fixture(autouse=True, scope="session")
def _no_real_gh(_no_real_ssh: Path, tmp_path_factory: pytest.TempPathFactory):
    """No test reaches GitHub through `gh` (#191: `orch job done` reads its
    handoff comment). tests/fake_gh.py answers from $FAKE_GH_STORE and
    refuses without one; tests/handoff_store.py writes a store. The basetemp
    may be on a noexec /dev/shm, where PATH would skip the script; then it
    goes in its own directory under /tmp."""
    guard = tmp_path_factory.mktemp("no-real-gh")
    script = _write_gh(guard)
    fallback = None
    if not os.access(script, os.X_OK):
        fallback = Path(tempfile.mkdtemp(prefix="pytest-no-real-gh-", dir="/tmp"))
        guard, script = fallback, _write_gh(fallback)
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{guard}{os.pathsep}{previous}"
    yield script
    os.environ["PATH"] = previous
    if fallback is not None:
        shutil.rmtree(fallback, ignore_errors=True)


def _write_gh(directory: Path) -> Path:
    script = directory / "gh"
    script.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).with_name("fake_gh.py")}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture(autouse=True)
def _fake_gh_store(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Each test's fake gh answers from its own store, empty until a test
    serves a comment (tests/handoff_store.py); a fake harness posts one with
    $FAKE_POST_HANDOFF."""
    store = tmp_path_factory.mktemp("fake-gh") / "fake-gh.json"
    store.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("FAKE_GH_STORE", str(store))
    monkeypatch.setenv("FAKE_POST_HANDOFF", str(Path(__file__).with_name("post_handoff.py")))
    monkeypatch.setenv("FAKE_GH_HANDOFFS", str(Path(__file__).with_name("fake_gh.py")))
    yield store


@pytest.fixture(autouse=True, scope="session")
def _no_local_overlay(_a_clean_factory_env):
    """No host's orch.local.yml or machines.local.yml leaks into a test (#107).
    Overlay tests clear ORCH_NO_LOCAL_OVERLAY with monkeypatch.delenv."""
    previous = os.environ.get("ORCH_NO_LOCAL_OVERLAY")
    os.environ["ORCH_NO_LOCAL_OVERLAY"] = "1"
    yield
    if previous is None:
        os.environ.pop("ORCH_NO_LOCAL_OVERLAY", None)
    else:
        os.environ["ORCH_NO_LOCAL_OVERLAY"] = previous


@pytest.fixture(autouse=True, scope="session")
def _no_real_db_backup(_a_clean_factory_env, tmp_path_factory: pytest.TempPathFactory):
    """#66: `orch poll` backs up after every cycle; never to the real
    ~/.local/share/factory/backup. Tests that need a path set their own."""
    backup = tmp_path_factory.mktemp("db-backup") / "orch.db"
    previous = os.environ.get("ORCH_DB_BACKUP")
    os.environ["ORCH_DB_BACKUP"] = str(backup)
    yield backup
    if previous is None:
        os.environ.pop("ORCH_DB_BACKUP", None)
    else:
        os.environ["ORCH_DB_BACKUP"] = previous


@pytest.fixture(autouse=True)
def _a_ssh_control_dir():
    """#222: each test gets its own short ControlMaster socket directory, so
    no fake master outlives its test and a socket path stays bindable. It
    takes no monkeypatch, so a test's own patches are undone before this
    teardown runs. pytest orders same-scope autouse fixtures by name: the
    `_a_` prefix keeps this one ahead of any autouse fixture that requests
    monkeypatch (such as `_skill_preflight`), so monkeypatch is always set up
    after it and undone before its teardown."""
    directory = tempfile.mkdtemp(prefix="orch-ssh-", dir="/tmp")
    previous = os.environ.get("ORCH_SSH_CONTROL_DIR")
    os.environ["ORCH_SSH_CONTROL_DIR"] = directory
    yield directory
    if previous is None:
        os.environ.pop("ORCH_SSH_CONTROL_DIR", None)
    else:
        os.environ["ORCH_SSH_CONTROL_DIR"] = previous
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def _skill_preflight(monkeypatch: pytest.MonkeyPatch):
    from lib import skill_preflight

    monkeypatch.setattr(skill_preflight, "preflight", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True, scope="session")
def _skills_home(tmp_path_factory: pytest.TempPathFactory):
    from lib.pstack_pin import install_skills

    home = tmp_path_factory.mktemp("skills-home")
    errors = install_skills(Path(__file__).resolve().parents[1], home, "local")
    assert errors == [], errors
    saved = {name: os.environ.get(name) for name in ("HOME", "PYTHONUSERBASE")}
    os.environ["PYTHONUSERBASE"] = site.getuserbase()  # keep user-site packages found
    os.environ["HOME"] = str(home)
    yield home
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
