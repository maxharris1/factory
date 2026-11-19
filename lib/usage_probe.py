"""The two usage reads that run a process (issue #151).

* Claude: at each job's start, `orch job run` sends Claude Code's SDK
  control request `get_usage` alone on stdin to
  `claude -p --input-format stream-json --output-format stream-json --verbose`,
  as the job's own bot on the job's own device (the box, with the job
  command's own first word, `claude` or claude-bot's `claude-harness`
  wrapper, or the remote device, with `claude` from the bot's login PATH
  over its SSH alias). Only a job whose stream flags name Claude
  (`telemetry.stream_flags`) is probed; any other job logs why not. The
  CLI answers and exits at the end of stdin: no prompt, no model turn. It
  runs on a background thread, so it never delays or fails the job; the
  run waits for it (at most TIMEOUT_SECONDS) before its last flush. A
  probe without numbers logs one fixed line: its exit, a timeout, no
  reply, or a login without rate limits. The job's own `rate_limit_event`
  lines fill in the numbers when `get_usage` has none (lib/usage_readings.py).
* Codex on a remote device: when the run ends, lib/codex_rollout.py's own
  source runs there under `python3 -I -S -c` for the job's thread id and
  prints that rollout's rate limits and session token total. On the box the session
  reads it in process on each flush (lib/telemetry.py).

Nothing here reads a credential: the CLI and the rollout reader run with
the bot's own login, and only their stdout comes back. The run's proof
token and worker tag are never passed to the probe.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from lib import codex_rollout, telemetry
from lib.job_token import PROOF_ENV
from lib.remote import LOGIN_SHELL, TAG_ENV, ByteTransport, RemoteTarget, Transport, ssh_argv
from lib.usage_readings import (
    GET_USAGE_ID,
    MAX_REPLY_BYTES,
    Reading,
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


def claude_binary(flags: telemetry.StreamFlags) -> str | None:
    return flags.argv[0] if flags.harness == "claude" else None


class ProbeFailed(Exception):
    pass


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
    except subprocess.TimeoutExpired:
        raise ProbeFailed(f"timed out after {timeout:g}s") from None
    except OSError as exc:
        raise ProbeFailed(f"could not start ({type(exc).__name__})") from None
    if completed.returncode != 0:
        raise ProbeFailed(f"exit {completed.returncode}")
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
        raise ProbeFailed("no byte transport")
    result = transport.exchange(argv, timeout=timeout, input=data)
    if result.timed_out:
        raise ProbeFailed(f"timed out after {timeout:g}s")
    if result.code != 0:
        raise ProbeFailed(f"ssh exit {result.code}")
    return result.stdout[:MAX_REPLY_BYTES]


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
    log: Callable[[str], None],
) -> threading.Thread:
    """The readings are observed when the probe was asked, so a
    `rate_limit_event` the job streams meanwhile stays the newer reading."""

    def work() -> None:
        asked = clock()
        try:
            out = probe()
        except ProbeFailed as failed:
            log(f"usage probe: failed ({failed})")
            return
        except Exception as exc:
            log(f"usage probe: failed ({type(exc).__name__})")
            return
        readings = parse_get_usage(out, asked)
        if not readings:
            log("usage probe: no reply")
        elif all(r.used is None and r.resets_at is None for r in readings):
            log("usage probe: no rate limits for this login")
        add(readings)

    thread = threading.Thread(target=work, name="orch-usage", daemon=True)
    thread.start()
    return thread



def remote_codex(transport: Transport, target: RemoteTarget, harness: str, thread_id: str | None) -> object:
    """A finished remote Codex job's rollout read, as lib/codex_rollout.py
    printed it; None for another harness or on any failure."""
    if harness != "codex" or thread_id is None:
        return None
    try:
        return codex_remote(transport, target, thread_id)
    except Exception:  # a usage read never touches the job's end
        return None
