"""One usage refresh pass over every account and device pair (issue #1837).

`run` runs the account doctor (lib/account_doctor.py), then reads usage for
each pair it judged `ok`, with no job row and no model turn, using the
scripts lib/usage_probe.py sends at a job's start. The reads run in parallel
under one DEADLINE_SECONDS deadline, and nothing here reads a credential
file: the CLI or the rollout reader runs as the account and only its stdout
comes back. Every reading is stored through `usage_readings.store` in one
transaction, under its own account and device, with JOB_ID and the
harness's bot user.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Literal

from lib import account_doctor, harnesses, usage_probe, usage_readings
from lib.machines_yml import MachinesConfig
from lib.remote import ByteTransport
from lib.usage_probe import ProbeFailed
from lib.usage_readings import NOT_TRACKED, SOURCES, Reading

JOB_ID = "orch-usage-refresh"
DEADLINE_SECONDS = 30.0
NO_READING = "no reading in the reply"

Outcome = Literal["read", "signed-out", "mismatch", "unreachable", "not tracked", "probe failed"]


@dataclass(frozen=True)
class Reader:
    script: Callable[[str | None], str]
    stdin: bytes
    parse: Callable[[bytes, float], list[Reading]]


def _parse_rollout(stdout: bytes, now: float) -> list[Reading]:
    lines = stdout.strip().splitlines()
    try:
        found = json.loads(lines[-1]) if lines else None
    except ValueError:
        return []
    return usage_readings.parse_codex(found, now)


READERS: dict[str, Reader] = {
    "claude": Reader(usage_probe.claude_remote_script, usage_probe.REQUEST, usage_readings.parse_get_usage),
    "codex": Reader(lambda home: usage_probe.codex_remote_script(None, home), b"", _parse_rollout),
    "gemini": Reader(lambda _home: usage_probe.gemini_remote_script(), b"", usage_readings.parse_agy_usage),
}


@dataclass(frozen=True)
class Result:
    account: str
    device: str
    harness: str
    outcome: Outcome
    source: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class _Pair:
    account: str
    device: str
    harness: str
    home: str | None
    own_login: bool


def _timed_out() -> str:
    return f"timed out after {DEADLINE_SECONDS:g}s"


def _read(transport: ByteTransport, config: MachinesConfig, pair: _Pair, deadline: float) -> bytes:
    reader = READERS[pair.harness]
    argv = account_doctor.ssh_argv(config.device(pair.device), pair.harness, reader.script(pair.home))
    timeout = max(0.0, deadline - time.monotonic())
    exchanged = transport.exchange(argv, timeout=timeout, input=reader.stdin)
    if exchanged.timed_out:
        raise ProbeFailed(_timed_out())
    if exchanged.code != 0:
        raise ProbeFailed(f"ssh exit {exchanged.code}")
    return exchanged.stdout[: usage_readings.MAX_REPLY_BYTES]


def _not_read(row: account_doctor.SignIn, harness: str) -> Result | None:
    if row.status != "ok":
        return Result(row.account, row.device, harness, row.status, detail=row.detail)
    if harness in NOT_TRACKED:
        return Result(row.account, row.device, harness, "not tracked", detail=NOT_TRACKED[harness])
    return None


def run(
    connection: sqlite3.Connection,
    config: MachinesConfig,
    transport: ByteTransport,
    now: float,
    repo: str | None,
) -> list[Result]:
    rows = account_doctor.run(connection, config, transport, now, repo)
    results: dict[tuple[str, str], Result] = {}
    pairs: list[_Pair] = []
    for row in rows:
        harness = config.account(row.account).harness
        skipped = _not_read(row, harness)
        if skipped is not None:
            results[row.account, row.device] = skipped
            continue
        login = config.accounts_on(row.device, harness)[0]
        home = config.account_home(row.device, harness, row.account)
        pairs.append(_Pair(row.account, row.device, harness, home, row.account == login))

    deadline = time.monotonic() + DEADLINE_SECONDS
    pool = ThreadPoolExecutor(max_workers=max(len(pairs), 1))
    futures = {pool.submit(_read, transport, config, pair, deadline): pair for pair in pairs}
    wait(futures, timeout=DEADLINE_SECONDS)
    pool.shutdown(wait=False, cancel_futures=True)

    stored: list[tuple[_Pair, list[Reading]]] = []
    for future, pair in futures.items():
        key = (pair.account, pair.device)
        try:
            if not future.done():
                raise ProbeFailed(_timed_out())
            stdout = future.result()
        except ProbeFailed as failed:
            results[key] = Result(*key, pair.harness, "probe failed", detail=str(failed))
            continue
        except Exception as exc:
            results[key] = Result(*key, pair.harness, "probe failed", detail=type(exc).__name__)
            continue
        readings = READERS[pair.harness].parse(stdout, now)
        if not readings:
            results[key] = Result(*key, pair.harness, "probe failed", detail=NO_READING)
            continue
        results[key] = Result(*key, pair.harness, "read", source=SOURCES[pair.harness])
        stored.append((pair, readings))

    connection.execute("BEGIN IMMEDIATE")
    try:
        for pair, readings in stored:
            usage_readings.store(
                connection,
                harness=pair.harness,
                bot=harnesses.get(pair.harness).bot_user,
                device=pair.device,
                job_id=JOB_ID,
                readings=readings,
                account=pair.account,
                own_login=pair.own_login,
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return [results[row.account, row.device] for row in rows]
