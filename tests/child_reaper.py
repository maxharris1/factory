"""Kill the child processes a test leaves behind when pytest-timeout fails it (#238).

Loaded from pytest.ini with `-p tests.child_reaper`. Each test runs with a
fresh FACTORY_TEST_PROC_TAG in its environment. When the setup or call phase
fails on a pytest-timeout timeout, before teardown runs, this kills every
process descended from pytest that was not there when the test started, and
every process whose environment carries the test's tag: workers that detached
and were reparented away from pytest, like a fake-ssh "remote" job. A test
that passes or fails any other way keeps its own cleanup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import uuid
from pathlib import Path

import pytest

TAG = "FACTORY_TEST_PROC_TAG"
PROC = Path("/proc")

_before = pytest.StashKey[set]()
_tag = pytest.StashKey[str]()


def _parents() -> dict[int, int]:
    if PROC.is_dir():
        parents = {}
        for entry in PROC.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            parents[int(entry.name)] = int(stat.rpartition(")")[2].split()[1])
        return parents
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    return {int(pid): int(ppid) for pid, ppid in (line.split() for line in out.splitlines())}


def descendants(root: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, ppid in _parents().items():
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def tagged(tag: str) -> set[int]:
    if not PROC.is_dir():
        return set()
    needle = f"{TAG}={tag}".encode()
    found = set()
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.add(int(entry.name))
    return found


def _kill(pid: int, own_group: int) -> None:
    try:
        group = os.getpgid(pid)
        if group != own_group:
            os.killpg(group, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def reap(before: set[int], tag: str) -> None:
    me = os.getpid()
    own_group = os.getpgid(0)
    for _ in range(3):
        leftovers = (descendants(me) - before) | tagged(tag)
        leftovers.discard(me)
        if not leftovers:
            return
        for pid in leftovers:
            _kill(pid, own_group)


def _timed_out(call: pytest.CallInfo) -> bool:
    excinfo = call.excinfo
    return (
        excinfo is not None
        and excinfo.errisinstance(pytest.fail.Exception)
        and str(excinfo.value).startswith("Timeout (>")
        and "from pytest-timeout" in str(excinfo.value)
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem):
    saved = os.environ.get(TAG)
    tag = uuid.uuid4().hex
    item.stash[_tag] = tag
    item.stash[_before] = descendants(os.getpid())
    os.environ[TAG] = tag
    try:
        return (yield)
    finally:
        if saved is None:
            os.environ.pop(TAG, None)
        else:
            os.environ[TAG] = saved


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> None:
    if call.when in ("setup", "call") and _timed_out(call):
        reap(item.stash[_before], item.stash[_tag])
