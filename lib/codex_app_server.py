"""A Codex account's rate limits, asked of `codex app-server` itself.

`read_rate_limits(deadline)` starts `codex app-server` in its own session
and speaks its newline-delimited JSON protocol on stdin and stdout: it sends
`initialize`, and on that reply `initialized` and one
`account/rateLimits/read` with `excludeResetCreditDetails: true`, and no
other method. Every notification and server request is decoded, then
skipped. Of the read's reply only `rateLimits.primary` and
`rateLimits.secondary` (the 5-hour and weekly windows) are kept, each to
the finite numbers in WINDOW_KEYS, as `{"rateLimits": {"primary": {...}, "secondary": {...}}}`; a
window the reply leaves null or without a number is left out. Any other
outcome is `{"rateLimits": None, "why": <word>}`, the word one of WHY, plus
`"code"` when the server refused with an integer error code. The server gets
DEADLINE_SECONDS in all and MAX_BYTES of output; at the end, whatever
happened, its whole process group is killed and reaped.

The file is stdlib only, has no repo imports, opens no file and starts no
thread: it runs on a remote device as
`python3 -I -S -c <source> [<deadline seconds>]`, and `main` prints one
JSON line, always. A deadline argument shortens DEADLINE_SECONDS, never
lengthens it. The server's stderr is inherited, for the caller to discard.
"""

from __future__ import annotations

import json
import math
import os
import select
import signal
import subprocess
import sys
import time

DEADLINE_SECONDS = 10.0
MAX_BYTES = 256 * 1024
WINDOW_KEYS = ("usedPercent", "windowDurationMins", "resetsAt")
WHY = ("no codex", "timed out", "exited", "refused", "no rate limits", "too much output")
INITIALIZE = {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "factory-usage", "version": "1"}}}
INITIALIZED = {"method": "initialized"}
READ = {"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}}


def _finite(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _no(why: str, code: object = None) -> dict:
    outcome = {"rateLimits": None, "why": WHY[WHY.index(why)]}
    if isinstance(code, int) and not isinstance(code, bool):
        outcome["code"] = code
    return outcome


def _windows(snapshot: object) -> dict:
    kept = {}
    for name in ("primary", "secondary"):
        window = snapshot.get(name) if isinstance(snapshot, dict) else None
        window = window if isinstance(window, dict) else {}
        numbers = {key: window[key] for key in WINDOW_KEYS if _finite(window.get(key))}
        if numbers:
            kept[name] = numbers
    return kept


def _reply(line: bytes) -> dict | None:
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(message, dict) or "method" in message or type(message.get("id")) is not int:
        return None
    return message if message["id"] in (1, 2) else None


def _send(child: subprocess.Popen, message: dict) -> None:
    child.stdin.write(json.dumps(message).encode() + b"\n")
    child.stdin.flush()


def _converse(child: subprocess.Popen, end: float) -> dict:
    fd = child.stdout.fileno()
    _send(child, INITIALIZE)
    pending, total = b"", 0
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return _no("timed out")
        if total >= MAX_BYTES:
            return _no("too much output")
        if not select.select([fd], [], [], left)[0]:
            return _no("timed out")
        chunk = os.read(fd, min(65536, MAX_BYTES - total))
        if not chunk:
            return _no("exited")
        total += len(chunk)
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            message = _reply(line) if line.strip() else None
            if message is None:
                continue
            if "error" in message:
                error = message["error"]
                return _no("refused", error.get("code") if isinstance(error, dict) else None)
            if message["id"] == 1:
                _send(child, INITIALIZED)
                _send(child, READ)
                continue
            result = message.get("result")
            windows = _windows(result.get("rateLimits") if isinstance(result, dict) else None)
            return {"rateLimits": windows} if windows else _no("no rate limits")


def _stop(child: subprocess.Popen) -> None:
    try:
        child.stdin.close()
    except OSError:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    child.stdout.close()
    child.wait()


def read_rate_limits(deadline: float = DEADLINE_SECONDS) -> dict:
    try:
        child = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 start_new_session=True)
    except OSError:
        return _no("no codex")
    end = time.monotonic() + deadline
    try:
        return _converse(child, end)
    except OSError:
        return _no("exited")
    finally:
        _stop(child)


def main(argv: list[str]) -> int:
    deadline = DEADLINE_SECONDS
    if argv:
        try:
            asked = float(argv[0])
        except ValueError:
            asked = math.nan
        if math.isfinite(asked) and asked > 0:
            deadline = min(asked, DEADLINE_SECONDS)
    sys.stdout.write(json.dumps(read_rate_limits(deadline), allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
