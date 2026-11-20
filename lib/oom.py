"""OOM killer order (#1229): job workers die first, `orch job run` last.

A job worker (a 2-4 GB harness) runs at WORKER_ADJ, and its children
inherit it. `orch job run` lowers itself to the lowest score the kernel
accepts and never raises itself. A failed write only logs: the job runs.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path

WORKER_ADJ = 800
SELF = Path("/proc/self/oom_score_adj")


def _write(path: Path, value: int) -> bool:
    try:
        with open(path, "w") as adj:
            adj.write(str(value))
    except OSError:
        return False
    return True


def lower_to_floor(log: Callable[[str], None], path: Path = SELF) -> int | None:
    """Lower path's score toward 0, to the lowest value the kernel accepts; return it."""
    try:
        current = int(path.read_text())
    except (OSError, ValueError) as exc:
        log(f"oom_score_adj left as is: {exc}")
        return None
    if current <= 0:
        return current
    if _write(path, 0):
        return 0
    # An unprivileged process cannot go below its user manager's score
    # (100 under systemd), so search for the lowest value it accepts.
    refused, accepted = 0, current
    while accepted - refused > 1:
        middle = (refused + accepted) // 2
        if _write(path, middle):
            accepted = middle
        else:
            refused = middle
    log(f"oom_score_adj {accepted}, the lowest the kernel allows (not 0)")
    return accepted


def raise_sh(adj: int = WORKER_ADJ) -> str:
    """POSIX sh that sets the current shell's score to adj, or says it could not."""
    skipped = shlex.quote(f"orch: worker oom_score_adj left as is (not {adj})")
    return (
        f"{{ echo {adj} > /proc/$$/oom_score_adj; }} 2>/dev/null "
        f"|| echo {skipped} >&2; "
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "floor" or not sys.argv[2].isdigit():
        sys.exit("usage: oom.py floor <pid>")
    lower_to_floor(
        lambda line: print(line, file=sys.stderr),
        Path(f"/proc/{sys.argv[2]}/oom_score_adj"),
    )
