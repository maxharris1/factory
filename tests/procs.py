"""Process liveness for tests that poll a pid while it may exit and be reaped."""

from __future__ import annotations

import os
from pathlib import Path


def alive(pid: int) -> bool:
    """True while pid runs. A zombie, or a pid reaped mid-check, is not alive.

    Without /proc (macOS) the signal probe alone decides.
    """
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat").read_text()
    except ProcessLookupError:
        return False
    except FileNotFoundError:
        return not Path("/proc/self/stat").exists()
    return stat.rpartition(")")[2].split()[0] != "Z"


def open_files(pid: int) -> set[str]:
    """The paths pid holds open, skipping an fd that closes mid-scan."""
    found: set[str] = set()
    for fd in os.listdir(f"/proc/{pid}/fd"):
        try:
            found.add(os.readlink(f"/proc/{pid}/fd/{fd}"))
        except FileNotFoundError:
            continue
    return found
