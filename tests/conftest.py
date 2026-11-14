"""Suite-wide guard: no test may reach a real host over SSH (F2-02).

A refusing `ssh` is put first on PATH for the whole run. It exits 255 (an
unreachable device) and touches nothing. Tests that exercise remote start
put tests/fake_ssh.py in front of it instead.
"""

from __future__ import annotations

import os
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
