"""Pull-based universal orchestrator queue and lease engine (E3-1).

Backs `orch wait orchestrator` and `orch orchestrator ack|heartbeat|finish`
using lease/pull columns on `driver_inbox`. Any AI harness (Claude, Codex,
Gemini, Grok, Cursor) can block at zero token cost until a wake arrives,
claim it atomically with a random per-claim lease token (stored hashed in
SQLite), renew its lease while working, and record orchestrator token/USD
spend in `job_telemetry` (`bot = 'orchestrator'`) on completion so Monday
weekly evals capture orchestrator cost.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import signal
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from lib import telemetry
from lib.grok_delivery import wake_body
from lib.orch_yml import DriverInbox
from lib.queue import Job
from lib.wait import (
    BUSY_TIMEOUT_SECONDS,
    GUARDED_SIGNALS,
    POLL_SECONDS,
    WaitError,
    is_locked,
    lock_backoff,
    retry_locked,
)

DEFAULT_LEASE_SECONDS = 300.0
MIN_LEASE_SECONDS = 5.0
MAX_LEASE_SECONDS = 3600.0
ORCHESTRATOR_BOT = "orchestrator"

PULL_COLUMNS: dict[str, str] = {
    "logical_key": "TEXT",
    "payload": "TEXT",
    "pull_state": "TEXT",
    "claimed_by": "TEXT",
    "lease_token_hash": "TEXT",
    "claimed_at": "REAL",
    "lease_until": "REAL",
    "acked_at": "REAL",
    "finished_at": "REAL",
    "pull_attempts": "INTEGER NOT NULL DEFAULT 0",
    "result": "TEXT",
}

_DRIVER_INBOX_DDL = """
CREATE TABLE IF NOT EXISTS driver_inbox (
    job_id TEXT PRIMARY KEY,
    inbox_repo TEXT NOT NULL,
    inbox_number INTEGER NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'posted')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    posted_at REAL
)
"""

_LOGICAL_KEY_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS driver_inbox_logical_key
ON driver_inbox (logical_key)
WHERE logical_key IS NOT NULL
"""

_PULL_STATE_INDEX = """
CREATE INDEX IF NOT EXISTS driver_inbox_pull_state
ON driver_inbox (pull_state, created_at)
WHERE pull_state IS NOT NULL
"""


@dataclass(frozen=True)
class ClaimedWake:
    job_id: str
    logical_key: str
    lease_token: str
    claimer: str
    lease_until: float
    attempts: int
    wake: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "logical_key": self.logical_key,
            "lease_token": self.lease_token,
            "claimer": self.claimer,
            "lease_until": self.lease_until,
            "attempts": self.attempts,
            "wake": self.wake,
        }


@dataclass(frozen=True)
class OrchestratorUsage:
    harness: str
    repo: str = ""
    tokens_in: int | None = None
    tokens_cached: int = 0
    tokens_out: int | None = None
    cost_usd: float | None = None
    elapsed_seconds: float = 0.0
    turns: int = 1
    exit_code: int = 0
    exit_reason: str = "done"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_lease(seconds: float) -> float:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        raise ValueError("lease_seconds must be a number")
    if seconds < MIN_LEASE_SECONDS or seconds > MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease_seconds must be between {int(MIN_LEASE_SECONDS)} and {int(MAX_LEASE_SECONDS)}"
        )
    return float(seconds)


def _connect_rw(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise WaitError(f"dispatcher database {str(db_path)!r} does not exist")
    uri = db_path.absolute().as_uri() + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_inbox_columns(connection: sqlite3.Connection) -> None:
    """Create driver_inbox if needed and add pull/lease columns idempotently."""
    connection.execute(_DRIVER_INBOX_DDL)
    present = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(driver_inbox)")
    }
    for name, ddl in PULL_COLUMNS.items():
        if name in present:
            continue
        try:
            connection.execute(f"ALTER TABLE driver_inbox ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    connection.execute(_LOGICAL_KEY_INDEX)
    connection.execute(_PULL_STATE_INDEX)


def enqueue_pull_wake(
    connection: sqlite3.Connection,
    job: Job,
    logical_key: str,
    *,
    now: float,
    inbox: DriverInbox | None = None,
    body: str = "",
) -> bool:
    """Insert a pull-claimable wake into driver_inbox, deduplicated by logical_key.

    Returns True when a new row was inserted, False when a row for job.id or
    logical_key already existed.
    """
    ensure_inbox_columns(connection)
    payload_json = wake_body(job, logical_key).decode("utf-8")
    inbox_repo = inbox.repo if inbox is not None else ""
    inbox_number = inbox.number if inbox is not None else 0
    status = "pending" if inbox is not None else "posted"
    posted_at = None if inbox is not None else now
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO driver_inbox (
            job_id, inbox_repo, inbox_number, body, status, created_at, posted_at,
            logical_key, payload, pull_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
        """,
        (
            job.id,
            inbox_repo,
            inbox_number,
            body if inbox is not None else "",
            status,
            now,
            posted_at,
            logical_key,
            payload_json,
        ),
    )
    return cursor.rowcount == 1


def reclaim_expired(connection: sqlite3.Connection, *, now: float) -> int:
    """Return expired claimed/acked wakes to 'queued' so another orchestrator can pick them up."""
    cursor = connection.execute(
        """
        UPDATE driver_inbox
        SET pull_state = 'queued',
            claimed_by = NULL,
            lease_token_hash = NULL,
            claimed_at = NULL,
            lease_until = NULL
        WHERE pull_state IN ('claimed', 'acked')
          AND lease_until IS NOT NULL
          AND lease_until <= ?
        """,
        (now,),
    )
    return cursor.rowcount


def claim_next(
    db_path: Path,
    *,
    claimer: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: Callable[[], float] = time.time,
) -> ClaimedWake | None:
    """Atomically reclaim expired leases and claim the oldest queued pull wake."""
    if not claimer or not claimer.strip():
        raise ValueError("claimer must be non-empty")
    lease = _valid_lease(lease_seconds)

    def _op() -> ClaimedWake | None:
        connection = _connect_rw(db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ensure_inbox_columns(connection)
            ts = float(now())
            reclaim_expired(connection, now=ts)
            row = connection.execute(
                """
                SELECT job_id, logical_key, payload, pull_attempts
                FROM driver_inbox
                WHERE pull_state = 'queued'
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            token = secrets.token_hex(24)
            lease_until = ts + lease
            attempts = int(row["pull_attempts"] or 0) + 1
            connection.execute(
                """
                UPDATE driver_inbox
                SET pull_state = 'claimed',
                    claimed_by = ?,
                    lease_token_hash = ?,
                    claimed_at = ?,
                    lease_until = ?,
                    pull_attempts = ?
                WHERE job_id = ? AND pull_state = 'queued'
                """,
                (claimer.strip(), _token_hash(token), ts, lease_until, attempts, row["job_id"]),
            )
            connection.commit()
            raw_payload = row["payload"] or "{}"
            try:
                wake = json.loads(raw_payload)
            except json.JSONDecodeError:
                wake = {"raw": raw_payload}
            if not isinstance(wake, dict):
                wake = {"raw": wake}
            return ClaimedWake(
                job_id=str(row["job_id"]),
                logical_key=str(row["logical_key"] or ""),
                lease_token=token,
                claimer=claimer.strip(),
                lease_until=lease_until,
                attempts=attempts,
                wake=wake,
            )
        finally:
            connection.close()

    return retry_locked(_op)


def _check_active_lease(
    connection: sqlite3.Connection,
    job_id: str,
    lease_token: str,
    ts: float,
) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT job_id, pull_state, lease_token_hash, lease_until, claimed_at, payload
        FROM driver_inbox
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None or row["pull_state"] not in ("claimed", "acked"):
        return None
    stored_hash = row["lease_token_hash"]
    if not isinstance(stored_hash, str) or not hmac.compare_digest(
        stored_hash, _token_hash(lease_token)
    ):
        return None
    lease_until = row["lease_until"]
    if lease_until is not None and float(lease_until) <= ts:
        return None
    return row


def ack_wake(
    db_path: Path,
    job_id: str,
    *,
    lease_token: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: Callable[[], float] = time.time,
) -> bool:
    """Acknowledge receipt of a claimed wake and extend its lease."""
    lease = _valid_lease(lease_seconds)

    def _op() -> bool:
        connection = _connect_rw(db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ensure_inbox_columns(connection)
            ts = float(now())
            row = _check_active_lease(connection, job_id, lease_token, ts)
            if row is None:
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE driver_inbox
                SET pull_state = 'acked',
                    acked_at = COALESCE(acked_at, ?),
                    lease_until = ?
                WHERE job_id = ?
                """,
                (ts, ts + lease, job_id),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    return retry_locked(_op)


def heartbeat_wake(
    db_path: Path,
    job_id: str,
    *,
    lease_token: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: Callable[[], float] = time.time,
) -> bool:
    """Extend the lease on an active claimed/acked wake."""
    lease = _valid_lease(lease_seconds)

    def _op() -> bool:
        connection = _connect_rw(db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ensure_inbox_columns(connection)
            ts = float(now())
            row = _check_active_lease(connection, job_id, lease_token, ts)
            if row is None:
                connection.commit()
                return False
            connection.execute(
                "UPDATE driver_inbox SET lease_until = ? WHERE job_id = ?",
                (ts + lease, job_id),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    return retry_locked(_op)


def release_wake(
    db_path: Path,
    job_id: str,
    *,
    lease_token: str,
    now: Callable[[], float] = time.time,
) -> bool:
    """Release an active claimed/acked wake back to 'queued' for immediate retry."""
    return finish_wake(db_path, job_id, lease_token=lease_token, release=True, now=now)


def finish_wake(
    db_path: Path,
    job_id: str,
    *,
    lease_token: str,
    result: str | None = None,
    release: bool = False,
    usage: OrchestratorUsage | None = None,
    now: Callable[[], float] = time.time,
) -> bool:
    """Mark a wake done (or release it to queued) and optionally record orchestrator telemetry."""

    def _op() -> bool:
        connection = _connect_rw(db_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ensure_inbox_columns(connection)
            ts = float(now())
            row = _check_active_lease(connection, job_id, lease_token, ts)
            if row is None:
                connection.commit()
                return False
            if release:
                connection.execute(
                    """
                    UPDATE driver_inbox
                    SET pull_state = 'queued',
                        claimed_by = NULL,
                        lease_token_hash = NULL,
                        claimed_at = NULL,
                        lease_until = NULL,
                        result = ?
                    WHERE job_id = ?
                    """,
                    (result, job_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE driver_inbox
                    SET pull_state = 'done',
                        lease_token_hash = NULL,
                        lease_until = NULL,
                        finished_at = ?,
                        result = ?
                    WHERE job_id = ?
                    """,
                    (ts, result, job_id),
                )
            if usage is not None:
                _record_orchestrator_telemetry(
                    connection,
                    job_id=job_id,
                    row=row,
                    usage=usage,
                    finished_at=ts,
                )
            connection.commit()
            return True
        finally:
            connection.close()

    return retry_locked(_op)


def _record_orchestrator_telemetry(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    row: sqlite3.Row,
    usage: OrchestratorUsage,
    finished_at: float,
) -> None:
    telemetry.ensure_table(connection)
    repo = usage.repo
    if not repo:
        try:
            wake = json.loads(row["payload"] or "{}")
            if isinstance(wake, dict) and isinstance(wake.get("repo"), str):
                repo = wake["repo"]
        except json.JSONDecodeError:
            repo = ""
    claimed_at = float(row["claimed_at"]) if row["claimed_at"] is not None else finished_at
    elapsed = (
        float(usage.elapsed_seconds)
        if isinstance(usage.elapsed_seconds, (int, float))
        and math.isfinite(usage.elapsed_seconds)
        and usage.elapsed_seconds > 0
        else max(0.0, finished_at - claimed_at)
    )
    tokens_unknown = 1 if (usage.tokens_in is None and usage.tokens_out is None) else 0
    tokens_in = max(0, int(usage.tokens_in or 0))
    tokens_cached = max(0, min(tokens_in, int(usage.tokens_cached or 0)))
    tokens_out = max(0, int(usage.tokens_out or 0))
    cost_usd = usage.cost_usd
    if cost_usd is not None and (
        not isinstance(cost_usd, (int, float))
        or isinstance(cost_usd, bool)
        or not math.isfinite(cost_usd)
        or cost_usd < 0
    ):
        cost_usd = None
    connection.execute(
        f"""
        INSERT INTO {telemetry.TABLE} (
            job_id, bot, harness, repo, started_at, updated_at, last_event_at,
            finished_at, elapsed_seconds, events, turns, tool_calls,
            files_touched, commands, test_runs, tests_passed, tests_failed,
            tokens_in, tokens_cached, tokens_out, cost_usd, exit_code,
            exit_reason, steps, tokens_unknown
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            bot = excluded.bot,
            harness = excluded.harness,
            repo = excluded.repo,
            updated_at = excluded.updated_at,
            last_event_at = excluded.last_event_at,
            finished_at = excluded.finished_at,
            elapsed_seconds = excluded.elapsed_seconds,
            turns = excluded.turns,
            tokens_in = excluded.tokens_in,
            tokens_cached = excluded.tokens_cached,
            tokens_out = excluded.tokens_out,
            cost_usd = excluded.cost_usd,
            exit_code = excluded.exit_code,
            exit_reason = excluded.exit_reason,
            tokens_unknown = excluded.tokens_unknown
        """,
        (
            job_id,
            ORCHESTRATOR_BOT,
            usage.harness,
            repo,
            claimed_at,
            finished_at,
            finished_at,
            finished_at,
            elapsed,
            max(1, int(usage.turns)),
            max(0, int(usage.turns)),
            "{}",
            0,
            0,
            0,
            None,
            None,
            tokens_in,
            tokens_cached,
            tokens_out,
            None if cost_usd is None else float(cost_usd),
            int(usage.exit_code),
            usage.exit_reason,
            "[]",
            tokens_unknown,
        ),
    )


def default_claimer() -> str:
    host = os.environ.get("HOSTNAME") or os.uname().nodename if hasattr(os, "uname") else "host"
    return f"orchestrator@{host}:{os.getpid()}"


def wait_for_wake(
    db_path: Path,
    out: TextIO,
    *,
    claimer: str | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    once: bool = False,
    keep_ignored: bool = False,
) -> ClaimedWake | None:
    """Block with zero tokens until a pull wake is available, claim it, and print one JSON line.

    When `once=True`, checks once and returns None without sleeping if the inbox is empty.
    If printing fails, releases the lease back to 'queued' so the wake is never dropped.
    """
    owner = claimer or default_claimer()
    _valid_lease(lease_seconds)
    handlers = {signum: signal.getsignal(signum) for signum in GUARDED_SIGNALS}
    locked = 0
    while True:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, GUARDED_SIGNALS)
        leaving = True
        try:
            try:
                claimed = claim_next(
                    db_path, claimer=owner, lease_seconds=lease_seconds, now=now
                )
            except sqlite3.OperationalError as exc:
                if not is_locked(exc):
                    raise WaitError(f"orchestrator wait failed: {exc}") from exc
                claimed = None
                locked += 1
            else:
                locked = 0
            if claimed is not None:
                try:
                    out.write(json.dumps(claimed.as_dict(), sort_keys=True) + "\n")
                    out.flush()
                except Exception as exc:
                    release_wake(
                        db_path, claimed.job_id, lease_token=claimed.lease_token, now=now
                    )
                    raise WaitError(
                        f"could not deliver orchestrator wake {claimed.job_id} ({exc}); released"
                    ) from exc
                return claimed
            if once:
                return None
            leaving = False
        finally:
            if leaving:
                for signum in GUARDED_SIGNALS:
                    signal.signal(signum, signal.SIG_IGN)
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            if leaving and not keep_ignored:
                for signum, handler in handlers.items():
                    if handler is not None:
                        signal.signal(signum, handler)
        sleep(lock_backoff(locked) if locked else poll_seconds)
