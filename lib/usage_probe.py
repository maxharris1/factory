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
* Gemini: at a gemini print-mode job's start, `orch job run` runs
  `agy -p /usage --output-format json` (the job command's own first word
  on the box, `agy` from the bot's login PATH on a remote device) as the
  job's bot, with every Google/Gemini override the account doctor unsets
  (GEMINI_UNSET) dropped. agy answers from its slash command: no prompt,
  no model turn, no permission flag. It runs on the same background
  thread as the Claude probe, logs the same one line when it gives no
  numbers, and only the placed account (the device's gemini-bot login,
  the one gemini account a device has) is read.
* Codex on a remote device: when the run ends, lib/codex_rollout.py's own
  source runs there under `python3 -I -S -c` for the job's thread id and
  prints its rate limits and token totals. On the box the session
  reads it in process on each flush (lib/telemetry.py).

Each pass reads every account signed in for the harness on the placed
device (`account_probes`), the placed account first: one Claude probe
thread per account, whose log lines name any account but the placed one,
and, when a Codex run ends, each other account's newest rollout, rate
limits only (`codex_rollout.newest`). An account without a home is
skipped with one line. Each reading is kept under its own account.

Nothing here reads a credential: the CLI and the rollout reader run as
each account, the bot's own login or, for another account,
CLAUDE_CONFIG_DIR or CODEX_HOME set to its home with its token variables
dropped by name (`harness_env.account_environ` on the box), and only
their stdout comes back. The run's proof token and worker tag are never
passed to the probe.
"""

from __future__ import annotations

import functools
import json
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from lib import codex_rollout, telemetry
from lib.harness_env import ACCOUNT_ISOLATION
from lib.harnesses.gemini import AUTH_OVERRIDES
from lib.job_token import PROOF_ENV
from lib.machines_yml import MachinesConfig
from lib.remote import LOGIN_SHELL, TAG_ENV, ByteTransport, RemoteTarget, Transport, ssh_argv
from lib.usage_readings import (
    GET_USAGE_ID,
    MAX_REPLY_BYTES,
    Reading,
    parse_agy_usage,
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
GEMINI_ARGS = ("-p", "/usage", "--output-format", "json")
GEMINI_UNSET = (*AUTH_OVERRIDES, "GEMINI_CLI_HOME")
TIMEOUT_SECONDS = 30.0
LOGIN_NAME = "orch-usage"

Run = Callable[..., subprocess.CompletedProcess]


def claude_binary(flags: telemetry.StreamFlags) -> str | None:
    return flags.argv[0] if flags.harness == "claude" else None


def gemini_binary(flags: telemetry.StreamFlags) -> str | None:
    """agy (or its wrapper) for a gemini job whose stream flags were added."""
    return flags.argv[0] if flags.harness == "gemini" and flags.added else None


class ProbeFailed(Exception):
    pass


def claude_local(
    binary: str,
    environ: Mapping[str, str],
    *,
    run: Run = subprocess.run,
    timeout: float = TIMEOUT_SECONDS,
) -> bytes:
    return _local([binary, *CLAUDE_ARGS], REQUEST, environ, (), run, timeout)


def gemini_local(
    binary: str,
    environ: Mapping[str, str],
    *,
    run: Run = subprocess.run,
    timeout: float = TIMEOUT_SECONDS,
) -> bytes:
    return _local([binary, *GEMINI_ARGS], b"", environ, GEMINI_UNSET, run, timeout)


def _local(
    argv: list[str],
    data: bytes,
    environ: Mapping[str, str],
    dropped: Sequence[str],
    run: Run,
    timeout: float,
) -> bytes:
    unset = {PROOF_ENV, TAG_ENV, *dropped}
    env = {name: value for name, value in environ.items() if name not in unset}
    try:
        completed = run(
            argv,
            input=data,
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


def _as_account(harness: str) -> str:
    isolation = ACCOUNT_ISOLATION[harness]
    dropped = "".join(f"-u {name} " for name in isolation.tokens)
    return f'/usr/bin/env {dropped}"{isolation.home}=$1"'


def claude_remote_script(home: str | None = None) -> str:
    if home is None:
        return _login('cd && exec claude "$@"', CLAUDE_ARGS)
    return _login(f'cd && exec {_as_account("claude")} claude "${{@:2}}"', (home, *CLAUDE_ARGS))


def gemini_remote_script() -> str:
    dropped = "".join(f"-u {name} " for name in GEMINI_UNSET)
    return _login(f'cd && exec /usr/bin/env {dropped}agy "$@"', GEMINI_ARGS)


def codex_remote_script(thread_id: str | None, home: str | None = None) -> str:
    source = Path(codex_rollout.__file__).read_text(encoding="utf-8")
    args = (source,) if thread_id is None else (source, thread_id)
    if home is None:
        return _login(f"exec python3 -I -S -c {_positional(1, len(args))}", args)
    return _login(f'exec {_as_account("codex")} python3 -I -S -c {_positional(2, len(args))}', (home, *args))


def _positional(first: int, count: int) -> str:
    return " ".join(f'"${n}"' for n in range(first, first + count))


def _exchange(transport: Transport, argv: list[str], data: bytes, timeout: float) -> bytes:
    if not isinstance(transport, ByteTransport):
        raise ProbeFailed("no byte transport")
    result = transport.exchange(argv, timeout=timeout, input=data)
    if result.timed_out:
        raise ProbeFailed(f"timed out after {timeout:g}s")
    if result.code != 0:
        raise ProbeFailed(f"ssh exit {result.code}")
    return result.stdout[:MAX_REPLY_BYTES]


def claude_remote(
    transport: Transport,
    target: RemoteTarget,
    *,
    home: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> bytes:
    return _exchange(transport, ssh_argv(target, claude_remote_script(home)), REQUEST, timeout)


def gemini_remote(
    transport: Transport,
    target: RemoteTarget,
    *,
    timeout: float = TIMEOUT_SECONDS,
) -> bytes:
    return _exchange(transport, ssh_argv(target, gemini_remote_script()), b"", timeout)


def codex_remote(
    transport: Transport,
    target: RemoteTarget,
    thread_id: str | None,
    *,
    home: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> object:
    if thread_id is not None and not codex_rollout.THREAD_ID.fullmatch(thread_id):
        return None
    out = _exchange(transport, ssh_argv(target, codex_remote_script(thread_id, home)), b"", timeout)
    lines = out.strip().splitlines()
    try:
        return json.loads(lines[-1]) if lines else None
    except ValueError:
        return None


def account_probes(
    config: MachinesConfig,
    device: str,
    harness: str,
    placed: tuple[str, str | None],
    log: Callable[[str], None],
) -> list[tuple[str, str | None]]:
    probes = [placed]
    for account in config.accounts_on(device, harness):
        if account == placed[0]:
            continue
        try:
            probes.append((account, config.account_home(device, harness, account)))
        except ValueError:
            log(f"usage probe ({account}): skipped (no account home on {device})")
    return probes


def start_claude(
    add: Callable[[list[Reading]], None],
    clock: Callable[[], float],
    probe: Callable[[], bytes],
    log: Callable[[str], None],
    label: str = "usage probe",
    parse: Callable[[bytes, float], list[Reading]] = parse_get_usage,
) -> threading.Thread:
    """The readings are observed when the probe was asked, so a
    `rate_limit_event` the job streams meanwhile stays the newer reading."""

    def work() -> None:
        asked = clock()
        try:
            out = probe()
        except ProbeFailed as failed:
            log(f"{label}: failed ({failed})")
            return
        except Exception as exc:
            log(f"{label}: failed ({type(exc).__name__})")
            return
        readings = parse(out, asked)
        if not readings:
            log(f"{label}: no reply")
        elif all(r.used is None and r.resets_at is None for r in readings):
            log(f"{label}: no rate limits for this login")
        add(readings)

    thread = threading.Thread(target=work, name="orch-usage", daemon=True)
    thread.start()
    return thread


def start_gemini(
    add: Callable[[list[Reading]], None],
    clock: Callable[[], float],
    probe: Callable[[], bytes],
    log: Callable[[str], None],
) -> threading.Thread:
    return start_claude(add, clock, probe, log, parse=parse_agy_usage)


def start_pass(
    probes: Sequence[tuple[str, str | None]],
    add: Callable[[list[Reading], str, str | None], None],
    clock: Callable[[], float],
    probe: Callable[[str | None], bytes],
    log: Callable[[str], None],
) -> list[threading.Thread]:
    return [
        start_claude(
            lambda readings, a=account, h=home: add(readings, a, h),
            clock,
            functools.partial(probe, home),
            log,
            "usage probe" if index == 0 else f"usage probe ({account})",
        )
        for index, (account, home) in enumerate(probes)
    ]


def join_pass(threads: Sequence[threading.Thread], timeout: float = TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))



def remote_codex(
    transport: Transport,
    target: RemoteTarget,
    harness: str,
    thread_id: str | None,
    *,
    home: str | None = None,
) -> object:
    """A finished remote Codex job's rollout read, as lib/codex_rollout.py
    printed it; None for another harness or on any failure."""
    if harness != "codex" or thread_id is None:
        return None
    try:
        return codex_remote(transport, target, thread_id, home=home)
    except Exception:  # a usage read never touches the job's end
        return None


def read_idle_codex(
    config: MachinesConfig,
    device: str,
    harness: str,
    placed: tuple[str, str | None],
    add: Callable[[object, str, str | None], None],
    read: Callable[[str | None], object],
    log: Callable[[str], None],
) -> None:
    if harness != "codex":
        return
    try:
        probes = account_probes(config, device, harness, placed, log)
    except Exception as exc:
        log(f"usage probe: idle codex accounts not read ({type(exc).__name__})")
        return
    for account, home in probes[1:]:
        label = f"usage probe ({account})"
        try:
            found = read(home)
        except ProbeFailed as failed:
            log(f"{label}: failed ({failed})")
            continue
        except Exception as exc:
            log(f"{label}: failed ({type(exc).__name__})")
            continue
        if found is None:
            log(f"{label}: no rollout")
        else:
            add(found, account, home)
