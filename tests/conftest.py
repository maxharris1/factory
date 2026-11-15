"""Suite-wide guard: no test may reach a real host over SSH (F2-02).

A refusing `ssh` is put first on PATH for the whole run. It exits 255 (an
unreachable device) and touches nothing. Tests that exercise remote start
put tests/fake_ssh.py in front of it instead.

The waiter's skill preflight (lib/skill_preflight.py) is a no-op in this
process unless a test overrides `_skill_preflight`, as
tests/test_skill_preflight.py does. A waiter a test starts as its own
process runs the real preflight, so HOME is a session home holding the
pinned skills, installed as install-skills does.
"""

from __future__ import annotations

import os
import site
import stat
from pathlib import Path

import pytest


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
def _no_local_overlay():
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
def _no_real_db_backup(tmp_path_factory: pytest.TempPathFactory):
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
