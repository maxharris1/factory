"""The rate limits and token total in one Codex job's session rollouts.

Codex CLI writes each session to
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl`, and
its `token_count` events carry `rate_limits.{primary,secondary}` and
`info.total_token_usage`, Codex's own running total for the session.
`latest(home, thread_id)` opens the rollout named by the job's own thread
id (from the `thread.started` event of `codex exec --json`), reads at most
MAX_BYTES from its end, and returns the last `token_count` event's
`rate_limits` with only the numeric fields in KEPT, plus that event's
timestamp (`rate_limits` None when that event's is null or missing: no
reading, never an older event's). `tokens` is the last `total_token_usage`
that holds a number, kept to the numeric fields in TOKEN_KEYS, summed with
the same total of every helper agent the thread spawned (#1015). A helper
is a rollout no older than the job's whose first line, its `session_meta`,
names the job's thread, or another helper's, as its parent; only that
first line of the other rollouts is read. Every other key and line is
dropped unread: a tail line is decoded only when it holds `"token_count"`.
A symlink, a non-regular file or a path outside `sessions/` is never
opened, and no other file is.

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
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
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


def _read(path: str, *, tail: bool) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return b""
        start = max(0, info.st_size - MAX_BYTES) if tail else 0
        os.lseek(fd, start, os.SEEK_SET)
        chunks = []
        left = min(info.st_size - start, MAX_BYTES)
        while left > 0:
            chunk = os.read(fd, min(left, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
            if not tail and b"\n" in chunk:
                break
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if not tail:
        return data.split(b"\n", 1)[0]
    return data.split(b"\n", 1)[1] if start and b"\n" in data else data


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _numbers(source: object, keys: tuple[str, ...]) -> dict | None:
    if not isinstance(source, dict):
        return None
    return {name: source[name] for name in keys if _finite(source.get(name))}


def _reading(line: bytes) -> dict | None:
    if len(line) > MAX_LINE_BYTES or b'"token_count"' not in line:
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
    info = payload.get("info")
    tokens = _numbers(info.get("total_token_usage"), TOKEN_KEYS) if isinstance(info, dict) else None
    tokens = tokens or None
    limits = payload.get("rate_limits")
    kept = None
    if isinstance(limits, dict):
        kept = {}
        for key in ("primary", "secondary"):
            window = _numbers(limits.get(key), KEPT)
            if window is not None:
                kept[key] = window
    return {"rate_limits": kept, "tokens": tokens, "timestamp": stamp}


def _parent(line: bytes) -> tuple[str, str] | None:
    """(thread id, parent thread id) from a helper rollout's `session_meta` line."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    payload = event.get("payload") if isinstance(event, dict) and event.get("type") == "session_meta" else None
    if not isinstance(payload, dict):
        return None
    parent = payload.get("parent_thread_id")
    if parent is None:
        spawn = payload.get("source")
        for key in ("subagent", "thread_spawn"):
            spawn = spawn.get(key) if isinstance(spawn, dict) else None
        parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    thread = payload.get("id")
    if not (isinstance(thread, str) and isinstance(parent, str)):
        return None
    if THREAD_ID.fullmatch(thread) is None or THREAD_ID.fullmatch(parent) is None:
        return None
    return thread, parent


def _helpers(home: str, path: str, thread_id: str) -> list[str]:
    """Every rollout descended from the job's thread through `session_meta` parents."""
    sessions = os.path.realpath(os.path.join(home, "sessions"))
    day = os.path.relpath(os.path.realpath(os.path.dirname(path)), sessions).split(os.sep)
    oldest = os.path.basename(path)[: -len(f"-{thread_id}.jsonl")]
    children: dict[str, list[tuple[str, str]]] = {}
    for folder in sorted(glob.glob(os.path.join(glob.escape(sessions), "*", "*", "*"))):
        real = os.path.realpath(folder)
        if not real.startswith(sessions + os.sep) or os.path.relpath(real, sessions).split(os.sep) < day:
            continue
        for candidate in sorted(glob.glob(os.path.join(glob.escape(folder), "rollout-*.jsonl"))):
            if os.path.basename(candidate) < oldest or os.path.basename(candidate).endswith(f"-{thread_id}.jsonl"):
                continue
            try:
                found = _parent(_read(candidate, tail=False))
            except OSError:
                continue
            if found is not None:
                children.setdefault(found[1], []).append((found[0], candidate))
    helpers, queue, seen = [], [thread_id], {thread_id}
    while queue:
        for child, candidate in children.get(queue.pop(), []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
                helpers.append(candidate)
    return helpers


def _tokens(data: bytes) -> dict | None:
    for line in reversed(data.splitlines()):
        found = _reading(line)
        if found is not None and found["tokens"] is not None:
            return found["tokens"]
    return None


def latest(home: str, thread_id: str) -> dict | None:
    """The thread's last `token_count` rate limits and its session token total, helpers included, or None."""
    path = rollout_path(home, thread_id)
    if path is None:
        return None
    try:
        data = _read(path, tail=True)
    except OSError:
        return None
    found = next(filter(None, map(_reading, reversed(data.splitlines()))), None)
    if found is None:
        return None
    totals = [_tokens(data)]
    for helper in _helpers(home, path, thread_id):
        try:
            totals.append(_tokens(_read(helper, tail=True)))
        except OSError:
            continue
    present = [total for total in totals if total is not None]
    found["tokens"] = {
        key: sum(total[key] for total in present if key in total)
        for key in TOKEN_KEYS
        if any(key in total for total in present)
    } or None
    return found


def main(argv: list[str]) -> int:
    found = latest(default_home(), argv[0]) if len(argv) == 1 else None
    if found is not None:
        sys.stdout.write(json.dumps(found, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
