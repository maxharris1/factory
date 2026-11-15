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

On Linux each pytest process is also a child subreaper, so a loop that
detaches with a scrubbed environment stays its descendant. A daemon thread
reaps such an orphan once it exits, so `kill -0` sees it gone, but never a
child this process spawned itself, whose status its Popen still waits for.
"""

from __future__ import annotations

import ctypes
import functools
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from tests.procs import alive

TAG = "FACTORY_TEST_PROC_TAG"
PROC = Path("/proc")
PR_SET_CHILD_SUBREAPER = 36
ORPHAN_POLL_SECONDS = 0.05

_open, _read, _close, _listdir = os.open, os.read, os.close, os.listdir

_spawned: set[int] = set()
_spawning = threading.Lock()

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


def _registered(spawn):
    @functools.wraps(spawn)
    def wrapper(*args, **kwargs):
        with _spawning:
            result = spawn(*args, **kwargs)
            pid = result[0] if isinstance(result, tuple) else result
            if pid:
                _spawned.add(pid)
            return result
    return wrapper


def _proc_text(path: str) -> str:
    fd = _open(path, os.O_RDONLY)
    try:
        chunks = []
        while chunk := _read(fd, 65536):
            chunks.append(chunk)
    finally:
        _close(fd)
    return b"".join(chunks).decode()


def _children() -> set[int]:
    tasks = f"{PROC}/{os.getpid()}/task"
    found: set[int] = set()
    for tid in _listdir(tasks):
        try:
            found.update(int(pid) for pid in _proc_text(f"{tasks}/{tid}/children").split())
        except OSError:
            continue
    return found


def _zombie(pid: int) -> bool:
    try:
        return _proc_text(f"{PROC}/{pid}/stat").rpartition(")")[2].split()[0] == "Z"
    except OSError:
        return False


def reap_orphans() -> None:
    with _spawning:
        children = _children()
        _spawned.intersection_update(children)
        for pid in children - _spawned:
            if _zombie(pid):
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass


def _reap_orphans_forever() -> None:
    signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
    while True:
        reap_orphans()
        threading.Event().wait(ORPHAN_POLL_SECONDS)


def _become_subreaper() -> None:
    if not sys.platform.startswith("linux") or not (PROC / "self/task").is_dir():
        return
    import _posixsubprocess

    for module, name in ((subprocess, "_fork_exec"), (_posixsubprocess, "fork_exec"),
                         (os, "fork"), (os, "forkpty"), (os, "posix_spawn"), (os, "posix_spawnp")):
        if hasattr(module, name):
            setattr(module, name, _registered(getattr(module, name)))
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER)")
    threading.Thread(target=_reap_orphans_forever, name="orphan-reaper", daemon=True).start()


def pytest_configure(config: pytest.Config) -> None:
    _become_subreaper()


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
