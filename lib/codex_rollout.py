"""The rate limits in one Codex job's own session rollout (issue #151, option B).

Codex CLI writes each session to
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl`, and
its `token_count` events carry `rate_limits.{primary,secondary}`.
`latest(home, thread_id)` opens only the rollout named by the job's own
thread id (from the `thread.started` event of `codex exec --json`), reads at
most MAX_BYTES from its end, and returns the last `token_count` event's
`rate_limits` with only the numeric fields in KEPT, plus the event's
timestamp (`rate_limits` None when that event has none: no reading, never
an older event's). Every other key and line is dropped unread: a line is decoded
only when it holds both `"token_count"` and `"rate_limits"`. A symlink, a
non-regular file or a path outside `sessions/` is never opened, and no
other file is.

The file is stdlib only and has no repo imports: for a job on a remote
device, lib/usage_probe.py sends this source to the device as
`python3 -I -S -c <source> <thread id>`, and `main` prints one JSON line
(or nothing). CODEX_HOME is the device's own (default `~/.codex`).
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import stat
import sys

MAX_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
KEPT = ("used_percent", "window_minutes", "resets_at", "resets_in_seconds")
THREAD_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def default_home(environ: dict | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def rollout_path(home: str, thread_id: str) -> str | None:
    """The job's own rollout file, or None. Only directory names are listed."""
    if not THREAD_ID.fullmatch(thread_id):
        return None
    sessions = os.path.realpath(os.path.join(home, "sessions"))
    pattern = os.path.join(glob.escape(sessions), "*", "*", "*", f"rollout-*-{thread_id}.jsonl")
    for path in sorted(glob.glob(pattern), reverse=True):
        parent = os.path.realpath(os.path.dirname(path))
        if parent.startswith(sessions + os.sep):
            return path
    return None


def _tail(path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return b""
        start = max(0, info.st_size - MAX_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        chunks = []
        left = info.st_size - start
        while left > 0:
            chunk = os.read(fd, min(left, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    return data.split(b"\n", 1)[1] if start and b"\n" in data else data


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _limits(line: bytes) -> dict | None:
    if len(line) > MAX_LINE_BYTES or b'"token_count"' not in line or b'"rate_limits"' not in line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    payload = event.get("payload") if isinstance(event, dict) else None
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    stamp = event.get("timestamp")
    stamp = stamp if isinstance(stamp, str) and len(stamp) <= 64 else None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return {"rate_limits": None, "timestamp": stamp}
    kept = {}
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if isinstance(window, dict):
            kept[key] = {
                name: window[name]
                for name in KEPT
                if _finite(window.get(name))
            }
    return {"rate_limits": kept, "timestamp": stamp}


def latest(home: str, thread_id: str) -> dict | None:
    """The last `token_count` rate limits in the thread's rollout, or None."""
    path = rollout_path(home, thread_id)
    if path is None:
        return None
    try:
        data = _tail(path)
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        found = _limits(line)
        if found is not None:
            return found
    return None


def main(argv: list[str]) -> int:
    found = latest(default_home(), argv[0]) if len(argv) == 1 else None
    if found is not None:
        sys.stdout.write(json.dumps(found, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
