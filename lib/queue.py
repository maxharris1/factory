"""Durable dispatcher queue shared with waiting bots.

Consumers use four operations: enqueue, claim, heartbeat, and finish.
Claim is non-blocking; F1-06 may block around it without changing this
interface. A finish with no handoff comment is a failed job for T6.

Claim order (issues #85, #258): a job on a ticket with a priority tier first,
lower tier first; then, inside a tier and among tickets with none, a job on
a ticket marked urgent, then a job resuming a paused step (its detail says
`"resume": true`), then a job on a ticket marked bug, then the ticket that
unblocks more open intake tickets, then the oldest. `ticket_priority` holds
those marks (the backlog feeder writes them each poll); a database without
that table orders on resume and age alone, and one without the tier and
unblocks columns orders as if no ticket had them.
A queued job on a ticket whose `ticket_state` is abandoned is never claimed
(issue #109).

A handoff that `orch job done` finds does not fit its job (lib/handoff_check.py,
#191) is recorded by record_invalid_handoff: a `submitted-invalid` job event
each time, one `submitted-invalid` needs_attention flag per ticket, and the
MAX_INVALID_HANDOFFS-th one fails the job for T6.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from lib import attention, job_token, lane_limit
from lib.state_backup import RESTORED, RESTORED_CLAIM

ACTIONS = (
    "plan",
    "build",
    "review",
    "fix",
    "update-from-main",
    "clarify",
    "stuck",
    "blocked",
    "capped",
    "refused",
    "unparsed",
    "ship-failed",
    "rejected",
    "stale",
    "done",
    "awaiting-ok",
    "go-not-from-approver",
)
RECIPIENTS = ("claude", "codex", "cursor", "grok")
STATES = ("queued", "running", "done", "failed", "cancelled")
BUSY_TIMEOUT_SECONDS = 30
# Device-pool placements (lib/device_pool.py). The table exists once a
# DevicePool has opened this database; a queued job never holds a row.
SLOTS_TABLE = "device_slots"
# Urgent and bug marks (issue #85), tier and unblocks (#258) per ticket for
# the claim order.
PRIORITY_TABLE = "ticket_priority"
_PRIORITY_COLUMNS = (("tier", "INTEGER"), ("unblocks", "INTEGER NOT NULL DEFAULT 0"))
SUBMITTED_INVALID = "submitted-invalid"
PRE_READY_FAILED = "pre-ready-failed"
PRE_READY_PASSED = "pre-ready-passed"
MAX_INVALID_HANDOFFS = 3


class QueueError(ValueError):
    """The requested queue operation is invalid."""


class ClaimProofError(QueueError):
    """A write to a running job whose current claim the caller cannot prove (#127)."""


@dataclass(frozen=True)
class Proof:
    """The ORCH_JOB_PROOF a process presents. Each write that takes one checks
    it against the job's current claim inside its own transaction (#127), so a
    proof retired by a release and reclaim never writes to the later claim."""

    token: str | None


@dataclass(frozen=True)
class JobRequest:
    repo: str
    ticket_id: str
    action: str
    recipient: str
    source_comment_id: str
    timeout_seconds: int
    generation: int = 1
    attempt: int = 1
    head: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Job:
    id: str
    dedupe_key: str
    request: JobRequest
    state: str
    created_at: float
    last_activity_at: float
    finished_at: float | None
    handoff_comment_id: str | None


class JobQueue(Protocol):
    def enqueue(
        self,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> Job: ...

    def claim(
        self,
        recipient: str,
        *,
        excluded_job_ids: Collection[str] = (),
        token_hash: str | None = None,
    ) -> Job | None: ...

    def release(self, job_id: str, *, owner: str) -> Job: ...

    def heartbeat(self, job_id: str, *, owner: str, proof: Proof | None = None) -> Job: ...

    def finish(
        self,
        job_id: str,
        *,
        owner: str,
        handoff_comment_id: str | None,
        proof: Proof | None = None,
    ) -> Job: ...


class DispatcherQueue(JobQueue, Protocol):
    def outstanding(self) -> tuple[Job, ...]: ...

    def mark_timed_out(self, job: Job) -> bool: ...

    def unhandled_failures(self) -> tuple[Job, ...]: ...

    def mark_failure_handled(self, job_id: str) -> None: ...


class SQLiteJobQueue:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float],
    ):
        self.path = path
        self.clock = clock
        self._initialize()

    def now(self) -> float:
        return self.clock()

    def connect(self) -> sqlite3.Connection:
        return self._connect()

    def enqueue(
        self,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> Job:
        with self._connect() as connection:
            return self.enqueue_in_transaction(
                connection,
                request,
                dedupe_key=dedupe_key,
            )

    def enqueue_retry(
        self,
        failed_job_id: str,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> tuple[Job, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            job = self.enqueue_in_transaction(
                connection,
                request,
                dedupe_key=dedupe_key,
            )
            inserted = connection.total_changes > before
            if inserted:
                handled = connection.execute(
                    """
                    UPDATE jobs SET failure_handled = 1
                    WHERE id = ? AND state = 'failed' AND failure_handled = 0
                    """,
                    (failed_job_id,),
                ).rowcount
                if handled != 1:
                    raise QueueError("retry requires one unhandled failed job")
                if retire_requeued(connection, job.id, "t6-retry", now=self.now()):
                    job = _job(
                        connection.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
                    )
            connection.commit()
            return job, inserted

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> Job:
        _validate_request(request)
        if not dedupe_key:
            raise QueueError("dedupe_key must not be empty")
        job_id = uuid.uuid4().hex
        now = self.now()
        connection.execute(
            """
            INSERT OR IGNORE INTO jobs (
                id, dedupe_key, repo, ticket_id, action, recipient,
                source_comment_id, timeout_seconds, generation, attempt,
                head, detail, state, created_at, last_activity_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job_id,
                dedupe_key,
                request.repo,
                request.ticket_id,
                request.action,
                request.recipient,
                request.source_comment_id,
                request.timeout_seconds,
                request.generation,
                request.attempt,
                request.head,
                request.detail,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM jobs WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        assert row is not None
        return _job(row)

    def claim(
        self,
        recipient: str,
        *,
        excluded_job_ids: Collection[str] = (),
        token_hash: str | None = None,
    ) -> Job | None:
        if recipient not in RECIPIENTS:
            raise QueueError(f"unknown recipient {recipient!r}")
        now = self.now()
        excluded = frozenset(excluded_job_ids)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = next(
                (
                    candidate
                    for candidate in claim_order(connection, recipient, now=now)
                    if str(candidate["id"]) not in excluded
                ),
                None,
            )
            if row is None:
                connection.commit()
                return None
            changed = connection.execute(
                """
                UPDATE jobs
                SET state = 'running', last_activity_at = ?, claim_token_hash = ?
                WHERE id = ? AND state = 'queued'
                """,
                (now, token_hash, row["id"]),
            ).rowcount
            if changed != 1:
                raise QueueError(f"job {row['id']} is no longer queued")
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
            connection.commit()
        assert updated is not None
        return _job(updated)

    def release(self, job_id: str, *, owner: str) -> Job:
        """Undo a claim whose work never started: running -> queued.

        A requeued job holds no device slot, so a pool placement is freed
        in the same transaction; otherwise the next pool claim of this job
        would collide with its own slot row.

        The job keeps the queue-wait clock it was queued with
        (last_activity_at = created_at). The only caller is a failed Grok
        delivery, a real attempt each poll, and the timeout is what bounds a
        delivery that keeps failing. The poller's release of a claim that
        never started restarts the clock instead (Dispatcher._release_claimed).
        """
        if owner not in RECIPIENTS:
            raise QueueError(f"unknown owner {owner!r}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE jobs SET state = 'queued', last_activity_at = created_at
                WHERE id = ? AND state = 'running' AND recipient = ?
                """,
                (job_id, owner),
            ).rowcount
            if changed == 1 and _has_table(connection, SLOTS_TABLE):
                connection.execute(
                    f"DELETE FROM {SLOTS_TABLE} WHERE job_id = ?",
                    (job_id,),
                )
            if changed == 1:
                retire_requeued(connection, job_id, "released", now=self.now())
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if updated is None:
                raise QueueError(f"unknown job {job_id!r}")
            if changed != 1:
                if updated["recipient"] != owner:
                    raise QueueError(
                        f"job {job_id} belongs to {updated['recipient']}, not {owner}"
                    )
                raise QueueError("release requires a running job")
            connection.commit()
        return _job(updated)

    def heartbeat(self, job_id: str, *, owner: str, proof: Proof | None = None) -> Job:
        if owner not in RECIPIENTS:
            raise QueueError(f"unknown owner {owner!r}")
        now = self.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_proof(connection, job_id, proof)
            changed = connection.execute(
                """
                UPDATE jobs SET last_activity_at = ?
                WHERE id = ? AND state = 'running' AND recipient = ?
                """,
                (now, job_id, owner),
            ).rowcount
            if changed != 1:
                current = connection.execute(
                    "SELECT recipient FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if current is None:
                    raise QueueError(f"unknown job {job_id!r}")
                if current["recipient"] != owner:
                    raise QueueError(
                        f"job {job_id} belongs to {current['recipient']}, not {owner}"
                    )
                raise QueueError("heartbeat requires a running job")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return _job(row)

    def finish(
        self,
        job_id: str,
        *,
        owner: str,
        handoff_comment_id: str | None,
        proof: Proof | None = None,
    ) -> Job:
        if owner not in RECIPIENTS:
            raise QueueError(f"unknown owner {owner!r}")
        now = self.now()
        target = "done" if handoff_comment_id is not None else "failed"
        if handoff_comment_id == "":
            raise QueueError("handoff_comment_id must not be empty")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_proof(connection, job_id, proof)
            changed = connection.execute(
                """
                UPDATE jobs
                SET state = ?, finished_at = ?, last_activity_at = ?,
                    handoff_comment_id = ?
                WHERE id = ? AND state = 'running' AND recipient = ?
                """,
                (target, now, now, handoff_comment_id, job_id, owner),
            ).rowcount
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if updated is None:
                raise QueueError(f"unknown job {job_id!r}")
            if changed != 1:
                if updated["recipient"] != owner:
                    raise QueueError(
                        f"job {job_id} belongs to {updated['recipient']}, not {owner}"
                    )
                if (
                    updated["state"] == target
                    and updated["handoff_comment_id"] == handoff_comment_id
                ):
                    connection.commit()
                    return _job(updated)
                raise QueueError("finish requires a running job")
            connection.commit()
        return _job(updated)

    def record_invalid_handoff(
        self,
        job_id: str,
        *,
        owner: str,
        comment_id: str,
        detail: str,
        proof: Proof | None = None,
        event: str = SUBMITTED_INVALID,
    ) -> tuple[int, Job]:
        """A running job's handoff did not fit it (#191): how many invalid
        handoffs it has now, and the job, failed once that is
        MAX_INVALID_HANDOFFS. The ticket's needs_attention flag is raised
        once and keeps its first detail."""
        if owner not in RECIPIENTS:
            raise QueueError(f"unknown owner {owner!r}")
        now = self.now()
        text = f"handoff {comment_id}: {detail}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_proof(connection, job_id, proof)
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise QueueError(f"unknown job {job_id!r}")
            if row["recipient"] != owner:
                raise QueueError(f"job {job_id} belongs to {row['recipient']}, not {owner}")
            if row["state"] != "running":
                raise QueueError("an invalid handoff can only be recorded on a running job")
            _ensure_job_events(connection)
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, str(row["repo"]).lower(), row["ticket_id"], row["action"],
                    owner, event, text, now,
                ),
            )
            attention.ensure_table(connection)
            connection.execute(
                """
                INSERT INTO needs_attention (repo, ticket_id, reason, detail, raised_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo, ticket_id, reason) DO NOTHING
                """,
                (
                    str(row["repo"]).lower(), row["ticket_id"], SUBMITTED_INVALID,
                    text[: attention.DETAIL_LIMIT], now,
                ),
            )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM job_events WHERE job_id = ? AND event IN (?, ?)",
                    (job_id, SUBMITTED_INVALID, PRE_READY_FAILED),
                ).fetchone()[0]
            )
            if count >= MAX_INVALID_HANDOFFS:
                connection.execute(
                    """
                    UPDATE jobs SET state = 'failed', finished_at = ?, last_activity_at = ?,
                        handoff_comment_id = NULL
                    WHERE id = ?
                    """,
                    (now, now, job_id),
                )
            updated = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            connection.commit()
        return count, _job(updated)

    def record_pre_ready_passed(
        self, job_id: str, *, comment_id: str, head: str, proof: Proof | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_proof(connection, job_id, proof)
            _ensure_job_events(connection)
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
                )
                SELECT id, lower(repo), ticket_id, action, recipient, ?, ?, ?
                FROM jobs WHERE id = ?
                """,
                (PRE_READY_PASSED, f"handoff {comment_id}: at {head}", self.now(), job_id),
            )
            connection.commit()

    def clear_invalid_handoff(self, job_id: str) -> None:
        """Drop the ticket's submitted-invalid flag once its job is done."""
        with self._connect() as connection:
            if not _has_table(connection, "needs_attention"):
                return
            connection.execute(
                """
                DELETE FROM needs_attention
                WHERE reason = ? AND EXISTS (
                    SELECT 1 FROM jobs
                    WHERE jobs.id = ? AND lower(jobs.repo) = needs_attention.repo
                      AND jobs.ticket_id = needs_attention.ticket_id
                )
                """,
                (SUBMITTED_INVALID, job_id),
            )

    def cancel(
        self,
        job_id: str,
        reason: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> Job:
        """queued -> cancelled, with a job_events row naming the reason.

        Only a queued job may be cancelled: a claimed or running one has a
        worker to stop first. The dedupe key stays, so the comment that
        queued the job never queues it again. With a connection the write
        joins the caller's transaction.
        """
        if not reason:
            raise QueueError("cancel needs a reason")
        if connection is not None:
            return self._cancel(connection, job_id, reason)
        with self._connect() as own:
            own.execute("BEGIN IMMEDIATE")
            job = self._cancel(own, job_id, reason)
            own.commit()
        return job

    def _cancel(
        self, connection: sqlite3.Connection, job_id: str, reason: str
    ) -> Job:
        return cancel_queued(connection, job_id, reason, self.now())

    def requeue(
        self, job_id: str, reason: str, *, connection: sqlite3.Connection
    ) -> bool:
        """cancelled -> queued, with a job_events row naming the reason;
        False when the job is no longer cancelled.

        Only for the dispatcher's answered-by cancel (#108), when the answer
        it relied on was then rejected: the step was never done. The write
        joins the caller's transaction. A finished ticket's job is cancelled
        again instead (retire_requeued) and this returns False.
        """
        now = self.now()
        changed = connection.execute(
            """
            UPDATE jobs
            SET state = 'queued', finished_at = NULL, last_activity_at = ?
            WHERE id = ? AND state = 'cancelled'
            """,
            (now, job_id),
        ).rowcount
        if changed != 1 or retire_requeued(connection, job_id, reason, now=now) is not None:
            return False
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
            )
            SELECT id, lower(repo), ticket_id, action, recipient, 'requeued', ?, ?
            FROM jobs WHERE id = ?
            """,
            (reason, now, job_id),
        )
        return True

    def outstanding(self) -> tuple[Job, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state IN ('queued', 'running')
                ORDER BY created_at, id
                """
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def mark_timed_out(self, job: Job) -> bool:
        """Fail a job the timeout sweep read, unless it changed or may run.

        A running job whose device slot records a remote start is never
        timed out (#87 r3 F2.1): failing it would let T6 start attempt 2
        beside a remote worker that may still run. It leaves running only
        through the poller's dead path, once its runner is gone and the
        device confirms its worker gone (lib/job_state.py), or as stuck when
        the device stays offline for an hour. An autostart claim with no runner
        and no heartbeat since its placement is never timed out either (#460):
        its waiter may still be in preflight, and the poller's claimed release
        requeues it with no attempt charged. The checks are in the same
        UPDATE, so a start recorded after the sweep read the job wins. A box
        job keeps the plain timeout.
        """
        now = self.now()
        # Guarded on the clock the sweep read: a queued job's last_activity_at
        # is its queue-wait start and changes when a claim is released, so a
        # sweep that read the job before a claim and release fails nothing.
        if job.state == "queued":
            predicate = "state = 'queued' AND last_activity_at = ?"
            expected = job.last_activity_at
        elif job.state == "running":
            predicate = "state = 'running' AND last_activity_at = ?"
            expected = job.last_activity_at
        else:
            return False
        with self._connect() as connection:
            if job.state == "running":
                predicate += _no_remote_start(connection) + _not_claimed(connection)
            changed = connection.execute(
                f"""
                UPDATE jobs
                SET state = 'failed', finished_at = ?, last_activity_at = ?
                WHERE id = ? AND {predicate}
                """,
                (now, now, job.id, expected),
            ).rowcount
        return changed == 1

    def unhandled_failures(self) -> tuple[Job, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'failed' AND failure_handled = 0
                ORDER BY finished_at, id
                """
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def mark_failure_handled(self, job_id: str) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE jobs SET failure_handled = 1
                WHERE id = ? AND state = 'failed' AND failure_handled = 0
                """,
                (job_id,),
            ).rowcount
        if changed != 1:
            raise QueueError("failure is missing or already handled")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size == 0:
            raise QueueError("database file is empty")
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        repo TEXT NOT NULL,
                        ticket_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        recipient TEXT NOT NULL,
                        source_comment_id TEXT NOT NULL,
                        timeout_seconds INTEGER NOT NULL,
                        generation INTEGER NOT NULL,
                        attempt INTEGER NOT NULL,
                        head TEXT,
                        detail TEXT,
                        state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        last_activity_at REAL NOT NULL,
                        finished_at REAL,
                        handoff_comment_id TEXT,
                        failure_handled INTEGER NOT NULL DEFAULT 0,
                        claim_token_hash TEXT
                    )
                    """
                )
                if "claim_token_hash" not in _columns(connection, "jobs"):
                    connection.execute("ALTER TABLE jobs ADD COLUMN claim_token_hash TEXT")
                ensure_priority_table(connection)
        except sqlite3.DatabaseError as exc:
            raise QueueError("database file is not valid SQLite") from exc


def ensure_priority_table(connection: sqlite3.Connection) -> None:
    """Create ticket_priority, and add the columns a database from before
    issue #258 lacks."""
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PRIORITY_TABLE} (
            repo TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            urgent INTEGER NOT NULL DEFAULT 0,
            bug INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            tier INTEGER,
            unblocks INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (repo, ticket_id)
        )
        """
    )
    present = _columns(connection, PRIORITY_TABLE)
    for name, kind in _PRIORITY_COLUMNS:
        if name not in present:
            connection.execute(f"ALTER TABLE {PRIORITY_TABLE} ADD COLUMN {name} {kind}")


def set_ticket_priority(
    connection: sqlite3.Connection,
    repo: str,
    ticket_id: str,
    *,
    urgent: bool,
    bug: bool,
    at: float,
    tier: int | None = None,
    unblocks: int = 0,
) -> None:
    """Record a ticket's marks for the claim order."""
    connection.execute(
        f"""
        INSERT INTO {PRIORITY_TABLE} (repo, ticket_id, urgent, bug, tier, unblocks, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, ticket_id) DO UPDATE SET
            urgent = excluded.urgent, bug = excluded.bug, tier = excluded.tier,
            unblocks = excluded.unblocks, updated_at = excluded.updated_at
        """,
        (repo.lower(), ticket_id, int(urgent), int(bug), tier, unblocks, at),
    )


@dataclass(frozen=True)
class PriorityMark:
    urgent: bool = False
    bug: bool = False
    tier: int | None = None
    unblocks: int = 0


def priority_marks(connection: sqlite3.Connection) -> dict[tuple[str, str], PriorityMark]:
    """Every ticket's marks, keyed (repo, ticket_id); empty with no table,
    and a column from after the database was made reads as unset."""
    if not _has_table(connection, PRIORITY_TABLE):
        return {}
    present = _columns(connection, PRIORITY_TABLE)
    tier = "tier" if "tier" in present else "NULL"
    unblocks = "unblocks" if "unblocks" in present else "0"
    return {
        (str(repo), str(ticket_id)): PriorityMark(
            bool(urgent), bool(bug), None if level is None else int(level), int(count)
        )
        for repo, ticket_id, urgent, bug, level, count in connection.execute(
            f"SELECT repo, ticket_id, urgent, bug, {tier}, {unblocks} FROM {PRIORITY_TABLE}"
        )
    }


def rank(
    *, tier: int | None, urgent: bool, resume: bool, bug: bool, unblocks: int
) -> tuple[bool, int, int, int]:
    base = 0 if urgent else 1 if resume else 2 if bug else 3
    return tier is None, tier or 0, base, -unblocks


def is_resume(detail: str | None) -> bool:
    """A job whose detail marks it as resuming a paused step."""
    if not detail:
        return False
    try:
        value = json.loads(detail)
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("resume") is True


def plan_round(detail: str | None) -> int:
    if not detail:
        return 1
    try:
        value = json.loads(detail)
    except ValueError:
        return 1
    if not isinstance(value, dict):
        return 1
    if value.get("plan_round") in (1, 2):
        return int(value["plan_round"])
    evidence = value.get("evidence")
    instructions = evidence.get("instructions") if isinstance(evidence, dict) else None
    attempt = instructions.get("plan_attempt") if isinstance(instructions, dict) else None
    if attempt in (1, 2):
        return int(attempt)
    return 2 if str(value.get("instruction") or "").startswith("round=2/2") else 1


def claim_order(
    connection: sqlite3.Connection, recipient: str, *, now: float
) -> list[sqlite3.Row]:
    """Queued jobs for recipient in claim order (see the module doc); none
    while recipient's lane is at its usage limit (#367)."""
    if lane_limit.active(connection, recipient, now) is not None:
        return []
    abandoned = (
        """
        AND NOT EXISTS (
            SELECT 1 FROM ticket_state AS ticket
            WHERE ticket.repo = lower(jobs.repo)
              AND ticket.ticket_id = jobs.ticket_id
              AND ticket.workflow_state = 'abandoned'
        )
        """
        if _has_table(connection, "ticket_state")
        else ""
    )
    rows = connection.execute(
        f"""
        SELECT * FROM jobs
        WHERE state = 'queued' AND recipient = ? {abandoned}
        ORDER BY created_at, id
        """,
        (recipient,),
    ).fetchall()
    marks = priority_marks(connection) if rows else {}

    def key(row: sqlite3.Row) -> tuple[bool, int, int, int, float, str]:
        mark = marks.get((str(row["repo"]).lower(), str(row["ticket_id"])), PriorityMark())
        return (
            *rank(tier=mark.tier, urgent=mark.urgent, resume=is_resume(row["detail"]),
                  bug=mark.bug, unblocks=mark.unblocks),
            float(row["created_at"]),
            str(row["id"]),
        )

    return sorted(rows, key=key)


def _slot_columns(connection: sqlite3.Connection) -> set[str]:
    if not _has_table(connection, SLOTS_TABLE):
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({SLOTS_TABLE})")}


def _not_claimed(connection: sqlite3.Connection) -> str:
    if not {"job_id", "placed_at", "runner_pid", "autostart_pid"} <= _slot_columns(connection):
        return ""
    return (
        f" AND NOT EXISTS (SELECT 1 FROM {SLOTS_TABLE} AS slot"
        " WHERE slot.job_id = jobs.id AND slot.autostart_pid IS NOT NULL"
        " AND slot.runner_pid IS NULL AND jobs.last_activity_at <= slot.placed_at)"
    )


def _no_remote_start(connection: sqlite3.Connection) -> str:
    """SQL that holds when the job's slot records no remote start.

    Empty when there is no slot table, or one from before F2-02 with no
    remote_started column (no remote start was ever recorded in it).
    """
    if not {"job_id", "remote_started"} <= _slot_columns(connection):
        return ""
    return (
        f" AND NOT EXISTS (SELECT 1 FROM {SLOTS_TABLE} AS slot"
        " WHERE slot.job_id = jobs.id AND slot.remote_started = 1)"
    )


def cancel_queued(
    connection: sqlite3.Connection, job_id: str, reason: str, now: float
) -> Job:
    """queued -> cancelled, in the caller's transaction (SQLiteJobQueue.cancel)."""
    changed = connection.execute(
        """
        UPDATE jobs
        SET state = 'cancelled', finished_at = ?, last_activity_at = ?
        WHERE id = ? AND state = 'queued'
        """,
        (now, now, job_id),
    ).rowcount
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise QueueError(f"unknown job {job_id!r}")
    if changed != 1:
        raise QueueError(f"job {job_id} is {row['state']}; only a queued job can be cancelled")
    if _has_table(connection, "job_events"):
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
            ) VALUES (?, ?, ?, ?, ?, 'cancelled', ?, ?)
            """,
            (
                job_id,
                str(row["repo"]).lower(),
                row["ticket_id"],
                row["action"],
                row["recipient"],
                reason,
                now,
            ),
        )
    return _job(row)


def retire_requeued(
    connection: sqlite3.Connection, job_id: str, reason: str, *, now: float
) -> str | None:
    """Cancel job_id, just put back in the queue in the caller's open
    transaction, when it is harness work that must not be claimable (#108).
    Returns the cancel's job_events detail, or None when it stays queued.

    Every write that returns a job to queued (a released claim, an unstarted
    requeue, a step whose answer was rejected, a T6 retry) calls this before
    it commits. A merged, done or abandoned ticket's job is cancelled
    `finished:<reason>`, so a finished ticket never regains claimable work. A step restore() copied
    running (RESTORED_CLAIM) and not held since is cancelled RESTORED, as
    restore() holds a queued step: the backup may predate its answer, and
    the dispatcher's next pass queues it again only if nothing answered it.
    """
    if not _has_table(connection, "ticket_state"):
        return None
    harness = "id = ? AND state = 'queued' AND recipient != 'grok'"
    finished = connection.execute(
        f"""
        SELECT 1 FROM jobs WHERE {harness}
          AND EXISTS (
            SELECT 1 FROM ticket_state
            WHERE ticket_state.repo = lower(jobs.repo)
              AND ticket_state.ticket_id = jobs.ticket_id
              AND ticket_state.workflow_state IN ('merged', 'done', 'abandoned')
          )
        """,
        (job_id,),
    ).fetchone()
    if finished is not None:
        detail = f"finished:{reason}"
    elif connection.execute(
        f"SELECT 1 FROM jobs WHERE {harness}", (job_id,)
    ).fetchone() is not None and restored_claim(connection, job_id):
        detail = RESTORED
    else:
        return None
    connection.execute(
        "UPDATE jobs SET state = 'cancelled', finished_at = ?, last_activity_at = ? "
        "WHERE id = ?",
        (now, now, job_id),
    )
    if _has_table(connection, "job_events"):
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
            )
            SELECT id, lower(repo), ticket_id, action, recipient, 'cancelled', ?, ?
            FROM jobs WHERE id = ?
            """,
            (detail, now, job_id),
        )
    return detail


def restored_claim(connection: sqlite3.Connection, job_id: str) -> bool:
    """True when restore() marked job_id RESTORED_CLAIM (copied running, or
    failed with T6 still to come) and no RESTORED hold has cancelled it since."""
    if not _has_table(connection, "job_events"):
        return False
    return connection.execute(
        """
        SELECT 1 FROM job_events AS mark
        WHERE mark.job_id = ? AND mark.event = ?
          AND NOT EXISTS (
            SELECT 1 FROM job_events AS hold
            WHERE hold.job_id = mark.job_id AND hold.event = 'cancelled'
              AND hold.detail = ? AND hold.id > mark.id
          )
        """,
        (job_id, RESTORED_CLAIM, RESTORED),
    ).fetchone() is not None


def _ensure_job_events(connection: sqlite3.Connection) -> None:
    """The dispatcher's job_events table (lib/dispatcher.py), for a database
    `orch job done` reaches before any poll made it."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            repo TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            action TEXT NOT NULL,
            recipient TEXT NOT NULL,
            event TEXT NOT NULL,
            detail TEXT NOT NULL,
            recorded_at REAL NOT NULL
        )
        """
    )


def require_proof(connection: sqlite3.Connection, job_id: str, proof: Proof | None) -> None:
    """Refuse, in the caller's transaction, a proof that matches neither the
    running job's claim token nor its slot's run token (#96). None is an
    internal caller's write. A job that is not running is left to the write."""
    if proof is None:
        return
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["state"] != "running":
        return
    _require_claim(connection, row, proof)


def require_proof_while_slot_held(
    connection: sqlite3.Connection, job_id: str, proof: Proof | None
) -> None:
    if proof is None or not _has_table(connection, SLOTS_TABLE):
        return
    row = connection.execute(
        f"SELECT jobs.* FROM jobs JOIN {SLOTS_TABLE} ON {SLOTS_TABLE}.job_id = jobs.id "
        "WHERE jobs.id = ?",
        (job_id,),
    ).fetchone()
    if row is not None:
        _require_claim(connection, row, proof)


def _require_claim(connection: sqlite3.Connection, row: sqlite3.Row, proof: Proof) -> None:
    job_id = row["id"]
    if job_token.matches(proof.token, row["claim_token_hash"]):
        return
    if _has_table(connection, SLOTS_TABLE) and "run_token_hash" in _columns(
        connection, SLOTS_TABLE
    ):
        slot = connection.execute(
            f"SELECT run_token_hash FROM {SLOTS_TABLE} WHERE job_id = ?", (job_id,)
        ).fetchone()
        if slot is not None and job_token.matches(proof.token, slot["run_token_hash"]):
            return
    raise ClaimProofError(
        f"job {job_id} is held by a claim this process's ORCH_JOB_PROOF does "
        "not prove (released and claimed again, or never this process's); nothing changed"
    )


def require_latest_claim(connection: sqlite3.Connection, job_id: str, proof: Proof | None) -> None:
    if proof is None:
        return
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is not None:
        _require_claim(connection, row, proof)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _validate_request(request: JobRequest) -> None:
    if not request.repo or not request.ticket_id or not request.source_comment_id:
        raise QueueError("repo, ticket_id and source_comment_id must not be empty")
    if request.action not in ACTIONS:
        raise QueueError(f"unknown action {request.action!r}")
    if request.recipient not in RECIPIENTS:
        raise QueueError(f"unknown recipient {request.recipient!r}")
    if request.timeout_seconds < 1:
        raise QueueError("timeout_seconds must be positive")
    if request.generation < 1:
        raise QueueError("generation must be positive")
    if request.attempt not in (1, 2):
        raise QueueError("attempt must be 1 or 2")


def job_from_row(row: sqlite3.Row) -> Job:
    return _job(row)


def _job(row: sqlite3.Row) -> Job:
    request = JobRequest(
        repo=str(row["repo"]),
        ticket_id=str(row["ticket_id"]),
        action=str(row["action"]),
        recipient=str(row["recipient"]),
        source_comment_id=str(row["source_comment_id"]),
        timeout_seconds=int(row["timeout_seconds"]),
        generation=int(row["generation"]),
        attempt=int(row["attempt"]),
        head=str(row["head"]) if row["head"] is not None else None,
        detail=str(row["detail"]) if row["detail"] is not None else None,
    )
    return Job(
        id=str(row["id"]),
        dedupe_key=str(row["dedupe_key"]),
        request=request,
        state=str(row["state"]),
        created_at=float(row["created_at"]),
        last_activity_at=float(row["last_activity_at"]),
        finished_at=(
            float(row["finished_at"]) if row["finished_at"] is not None else None
        ),
        handoff_comment_id=(
            str(row["handoff_comment_id"])
            if row["handoff_comment_id"] is not None
            else None
        ),
    )
