"""The two usage reads that run a process (issue #151).

* Claude: at each job's start, `orch job run` sends Claude Code's SDK
  control request `get_usage` alone on stdin to
  `claude -p --input-format stream-json --output-format stream-json --verbose`,
  as the job's own bot on the job's own device (the box, with the job
  command's own `claude` binary, or the remote device, with `claude` from
  the bot's login PATH over its SSH alias). A job whose command is not
  `claude` itself is not probed. The CLI answers
  and exits at the end of stdin: no prompt, no model turn. It runs on a
  background thread, so it never delays or fails the job, and its readings
  are written by the session's next flush.
* Codex on a remote device: when the run ends, lib/codex_rollout.py's own
  source runs there under `python3 -I -S -c` for the job's thread id and
  prints that rollout's rate limits. On the box the session reads it in
  process on each flush (lib/telemetry.py).

Nothing here reads a credential: the CLI and the rollout reader run with
the bot's own login, and only their stdout comes back. The run's proof
token and worker tag are never passed to the probe.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from lib import codex_rollout
from lib.job_token import PROOF_ENV
from lib.remote import LOGIN_SHELL, TAG_ENV, ByteTransport, RemoteTarget, Transport, ssh_argv
from lib.usage_readings import (
    GET_USAGE_ID,
    MAX_REPLY_BYTES,
    Reading,
    parse_codex,
    parse_get_usage,
)

CLAUDE_ARGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
REQUEST = (
    json.dumps(
        {
            "type": "control_request",
            "request_id": GET_USAGE_ID,
            "request": {"subtype": "get_usage", "skip_behaviors": True},
        }
    ).encode()
    + b"\n"
)
TIMEOUT_SECONDS = 30.0
LOGIN_NAME = "orch-usage"

Run = Callable[..., subprocess.CompletedProcess]


def claude_binary(command: Sequence[str]) -> str | None:
    """The job command's own `claude`, or None when the command is not Claude Code."""
    return command[0] if command and os.path.basename(command[0]) == "claude" else None


def claude_local(
    binary: str,
    environ: Mapping[str, str],
    *,
    run: Run = subprocess.run,
    timeout: float = TIMEOUT_SECONDS,
) -> bytes:
    env = {name: value for name, value in environ.items() if name not in (PROOF_ENV, TAG_ENV)}
    try:
        completed = run(
            [binary, *CLAUDE_ARGS],
            input=REQUEST,
            capture_output=True,
            timeout=timeout,
            env=env,
            cwd=str(Path.home()),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return b""
    return completed.stdout[:MAX_REPLY_BYTES]


def _login(script: str, args: Sequence[str]) -> str:
    quoted = " ".join(shlex.quote(arg) for arg in args)
    return f"exec {LOGIN_SHELL} -lc {shlex.quote(script)} {LOGIN_NAME} {quoted}"


def claude_remote_script() -> str:
    return _login('cd && exec claude "$@"', CLAUDE_ARGS)


def codex_remote_script(thread_id: str) -> str:
    source = Path(codex_rollout.__file__).read_text(encoding="utf-8")
    return _login('exec python3 -I -S -c "$1" "$2"', (source, thread_id))


def _exchange(transport: Transport, argv: list[str], data: bytes, timeout: float) -> bytes:
    if not isinstance(transport, ByteTransport):
        return b""
    result = transport.exchange(argv, timeout=timeout, input=data)
    return result.stdout[:MAX_REPLY_BYTES] if result.code == 0 else b""


def claude_remote(transport: Transport, target: RemoteTarget, *, timeout: float = TIMEOUT_SECONDS) -> bytes:
    return _exchange(transport, ssh_argv(target, claude_remote_script()), REQUEST, timeout)


def codex_remote(
    transport: Transport, target: RemoteTarget, thread_id: str, *, timeout: float = TIMEOUT_SECONDS
) -> object:
    if not codex_rollout.THREAD_ID.fullmatch(thread_id):
        return None
    out = _exchange(transport, ssh_argv(target, codex_remote_script(thread_id)), b"", timeout)
    lines = out.strip().splitlines()
    try:
        return json.loads(lines[-1]) if lines else None
    except ValueError:
        return None


def start_claude(
    add: Callable[[list[Reading]], None],
    clock: Callable[[], float],
    probe: Callable[[], bytes],
) -> threading.Thread:
    """Run `probe` on a daemon thread and hand its readings to `add`."""

    def work() -> None:
        try:
            out = probe()
        except Exception:  # a usage read never touches the job
            return
        add(parse_get_usage(out, clock()))

    thread = threading.Thread(target=work, name="orch-usage", daemon=True)
    thread.start()
    return thread



def remote_codex(
    transport: Transport, target: RemoteTarget, harness: str, thread_id: str | None, now: float
) -> list[Reading]:
    """A finished remote Codex job's rollout readings; none for another harness or on any failure."""
    if harness != "codex" or thread_id is None:
        return []
    try:
        return parse_codex(codex_remote(transport, target, thread_id), now)
    except Exception:  # a usage read never touches the job's end
        return []
