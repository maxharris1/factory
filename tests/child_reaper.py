"""Kill the child processes a test leaves behind when pytest-timeout fails it (#238).

Loaded from pytest.ini with `-p tests.child_reaper`. Each test runs with a
fresh FACTORY_TEST_PROC_TAG in its environment. When pytest-timeout's SIGALRM
fires, in setup, call or teardown, this kills every process descended from
pytest that was not there when the test started, and every process whose
environment carries the test's tag: workers that detached and were reparented
away from pytest, like a fake-ssh "remote" job. It does so inside the signal
handler, before the timeout's exception unwinds through code such as
`Popen.__exit__` that would wait on a live child. A whole process group is
killed only when none of its members predates the test. A test that passes or
fails any other way keeps its own cleanup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from tests.procs import alive

TAG = "FACTORY_TEST_PROC_TAG"
PROC = Path("/proc")

_before = pytest.StashKey[set]()
_tag = pytest.StashKey[str]()


def _table() -> dict[int, tuple[int, int]]:
    if PROC.is_dir():
        table = {}
        for entry in PROC.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            fields = stat.rpartition(")")[2].split()
            table[int(entry.name)] = (int(fields[1]), int(fields[2]))
        return table
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True).stdout
    return {int(pid): (int(ppid), int(pgid)) for pid, ppid, pgid in (line.split() for line in out.splitlines())}


def descendants(root: int, table: dict[int, tuple[int, int]] | None = None) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in (table if table is not None else _table()).items():
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


def _environ(pid: int) -> list[bytes]:
    try:
        return (PROC / str(pid) / "environ").read_bytes().split(b"\0")
    except OSError:
        return []


def leaked(before: set[int], tag: str, root: Path) -> set[int]:
    table = _table()
    mine = descendants(os.getpid(), table)
    needle = f"{TAG}={tag}".encode()
    prefixes = tuple(f"{name}={root}/".encode() for name in ("HOME", "ORCH_STATE_DIR"))
    found = set()
    for pid in table.keys() - before - {os.getpid()}:
        environ = _environ(pid)
        if pid in mine or any(entry == needle or entry.startswith(prefixes) for entry in environ):
            found.add(pid)
    return {pid for pid in found if alive(pid)}


def kill(leftovers: set[int], protected: set[int], table: dict[int, tuple[int, int]]) -> None:
    shared = {table[pid][1] for pid in protected if pid in table}
    for pid in leftovers & table.keys():
        group = table[pid][1]
        try:
            if group in shared:
                os.kill(pid, signal.SIGKILL)
            else:
                os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def reap(before: set[int], tag: str) -> None:
    me = os.getpid()
    protected = before | {me}
    for _ in range(3):
        table = _table()
        leftovers = ((descendants(me, table) | tagged(tag)) & table.keys()) - protected
        if not leftovers:
            return
        kill(leftovers, protected, table)


@pytest.hookimpl(wrapper=True, tryfirst=True)
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


@pytest.hookimpl(wrapper=True)
def pytest_timeout_set_timer(item: pytest.Item, settings):
    result = yield
    if threading.current_thread() is not threading.main_thread():
        return result
    timeout_handler = signal.getsignal(signal.SIGALRM)
    if not callable(timeout_handler):
        return result

    def handler(signum, frame):
        __tracebackhide__ = True
        reap(item.stash.get(_before, set()), item.stash.get(_tag, ""))
        timeout_handler(signum, frame)

    signal.signal(signal.SIGALRM, handler)
    return result
