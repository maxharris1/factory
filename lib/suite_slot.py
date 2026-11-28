import fcntl
import os
import re
import sys
import time
from pathlib import Path

LIMIT_ENV = "ORCH_TEST_SUITE_MAX"
WAIT_SECONDS = 90
BUSY_EXIT = 75
HELPER = "bin/suite-slot"

_HELPER_RE = re.compile(rf"(?:^|[\s/;&|('\"]){re.escape(Path(HELPER).name)}(?=[\s;&|)'\"]|$)")
_LINE_RE = re.compile(r"^ORCH-SUITE-SLOT: waited (\d+)s, (?:running|busy)\r?$", re.MULTILINE)


def slot_limit(environ):
    try:
        limit = int(environ.get(LIMIT_ENV, ""))
    except ValueError:
        return 1
    return limit if limit >= 1 else 1


def _default_dir():
    run_lock = Path("/run/lock")
    return run_lock if run_lock.is_dir() else Path("/tmp")


def _open_slot(path):
    try:
        return os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDONLY, 0o444)
    except FileExistsError:
        return os.open(path, os.O_RDONLY)
    os.fchmod(fd, 0o444)
    return fd


def _take_slot(slots):
    for path in slots:
        fd = _open_slot(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        return fd
    return None


def _report(start, state):
    print(f"ORCH-SUITE-SLOT: waited {round(time.monotonic() - start)}s, {state}", flush=True)


def _busy(start):
    _report(start, "busy")
    print(
        "suite-slot: this machine is busy running another whole test suite. "
        "Run targeted tests for the files you touched instead, and leave the whole suite to CI.",
        file=sys.stderr,
    )
    return BUSY_EXIT


def main(argv=None, *, slot_dir=None, wait_seconds=WAIT_SECONDS):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        print("usage: suite-slot <command> [args...]", file=sys.stderr)
        return 2
    base = Path(slot_dir) if slot_dir is not None else _default_dir()
    slots = [base / f"orch-suite-slot-{i}.lock" for i in range(slot_limit(os.environ))]
    start = time.monotonic()
    deadline = start + wait_seconds
    while True:
        try:
            fd = _take_slot(slots)
        except OSError:
            return _busy(start)
        if fd is not None:
            break
        if time.monotonic() >= deadline:
            return _busy(start)
        time.sleep(0.1)
    _report(start, "running")
    os.set_inheritable(fd, True)
    try:
        os.execvp(argv[0], argv)
    except OSError as error:
        print(f"suite-slot: {argv[0]}: {error}", file=sys.stderr)
        return 127


def waited(command, output):
    if not _HELPER_RE.search(command):
        return None
    match = _LINE_RE.search(output)
    if match is None:
        return None
    return min(int(match.group(1)), WAIT_SECONDS)
