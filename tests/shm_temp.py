"""Put this repo's pytest temp files in RAM (/dev/shm) when it qualifies (#225).

Loaded from pytest.ini with `-p tests.shm_temp`. Each session gets its own
`pytest-factory-<pid>-*` dir, so concurrent suites never share a basetemp.
When /dev/shm is missing, unwritable or short of space, nothing changes.
An explicit `--basetemp` keeps pytest's basetemp; TMPDIR still moves.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import pytest

SHM_ROOT = Path("/dev/shm")
MIN_FREE = 1 << 30
STALE_SECONDS = 3600
PREFIX = "pytest-factory-"
ENV_NAMES = ("TMPDIR", "TEMP", "TMP")
disk_usage = shutil.disk_usage

_session_root = pytest.StashKey[Path]()
_saved_env = pytest.StashKey[dict]()


def qualifies(shm_root: Path, min_free: int, usage=shutil.disk_usage) -> bool:
    try:
        return (
            shm_root.is_dir()
            and os.access(shm_root, os.W_OK | os.X_OK)
            and usage(shm_root).free >= min_free
        )
    except OSError:
        return False


def choose_session_root(shm_root: Path, min_free: int, usage=shutil.disk_usage) -> Path | None:
    if not qualifies(shm_root, min_free, usage):
        return None
    try:
        return Path(tempfile.mkdtemp(prefix=f"{PREFIX}{os.getpid()}-", dir=shm_root))
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_stale(shm_root: Path) -> None:
    pattern = re.compile(rf"{re.escape(PREFIX)}(\d+)-")
    cutoff = time.time() - STALE_SECONDS
    try:
        entries = list(shm_root.iterdir())
    except OSError:
        return
    for entry in entries:
        match = pattern.match(entry.name)
        if not match:
            continue
        try:
            info = entry.lstat()
        except OSError:
            continue
        if info.st_uid != os.getuid() or info.st_mtime > cutoff:
            continue
        if _pid_alive(int(match.group(1))):
            continue
        shutil.rmtree(entry, ignore_errors=True)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput"):
        return
    sweep_stale(SHM_ROOT)
    root = choose_session_root(SHM_ROOT, MIN_FREE, disk_usage)
    if root is None:
        return
    process_tmp = root / "tmp"
    process_tmp.mkdir()
    config.stash[_session_root] = root
    config.stash[_saved_env] = {name: os.environ.get(name) for name in ENV_NAMES}
    for name in ENV_NAMES:
        os.environ[name] = str(process_tmp)
    tempfile.tempdir = None
    if not config.option.basetemp and not os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        config.option.basetemp = str(process_tmp / "basetemp")


def pytest_unconfigure(config: pytest.Config) -> None:
    root = config.stash.get(_session_root, None)
    if root is None:
        return
    for name, value in config.stash[_saved_env].items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    tempfile.tempdir = None
    shutil.rmtree(root, ignore_errors=True)
