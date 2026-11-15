"""PR_SET_CHILD_SUBREAPER makes every process the command starts stay a
descendant of this one, however it detaches (nohup, setsid, a double fork).
Like init, it reaps each orphan as it exits, so `kill -0` sees it gone.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.child_reaper import PR_SET_CHILD_SUBREAPER, descendants

GRACE_SECONDS = 5


def _reap_zombies() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _describe(pid: int) -> str:
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return f"{pid} (gone)"
    home = next((entry[5:].decode(errors="replace") for entry in environ
                 if entry.startswith(b"HOME=")), "")
    return f"{pid} HOME={home} {argv.strip()}"


def main(report: str, command: list[str]) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER)")
    command_pid = subprocess.Popen(command).pid
    while True:
        pid, status = os.waitpid(-1, 0)
        if pid == command_pid:
            returncode = os.waitstatus_to_exitcode(status)
            break
    deadline = time.monotonic() + GRACE_SECONDS
    while True:
        _reap_zombies()
        left = descendants(os.getpid())
        if not left or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    Path(report).write_text(json.dumps(sorted(_describe(pid) for pid in left)), encoding="utf-8")
    while left:
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.1)
        _reap_zombies()
        left = descendants(os.getpid())
    return returncode


if __name__ == "__main__":
    separator = sys.argv.index("--")
    sys.exit(main(sys.argv[1], sys.argv[separator + 1:]))
