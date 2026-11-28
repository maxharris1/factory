"""Durable dispatcher queue shared with waiting bots.

Consumers use four operations: enqueue, claim, heartbeat, and finish.
Claim is non-blocking; F1-06 may block around it without changing this
interface. A finish with no handoff comment is a failed job for T6.

Claim order (issues #85, #258): a job on a ticket with a priority tier first,
lower tier first; then, inside a tier and among tickets with none, a job that
finishes a ticket already past intake (FINISHING_ACTIONS, decision 1050)
before one that starts a ticket; then a job on a ticket marked urgent, then a
job resuming a paused step (its detail says `"resume": true`), then a job on a
ticket marked bug, then the ticket that unblocks more open intake tickets,
then the oldest. `ticket_priority` holds those marks (the backlog feeder
writes them each poll); a database without that table orders on resume and age
alone, and one without the tier and unblocks columns orders as if no ticket
had them. The tier is the ticket's effective tier (decision 1050): the best of
its own and those of the tickets it gates, with an urgent ticket that has no
tier at tier 0. A queued job on a ticket whose `ticket_state` is abandoned
(issue #109) or handed-off-externally (issue #749) is never claimed.

A handoff that `orch job done` finds does not fit its job (lib/handoff_check.py,
#191) is recorded by record_invalid_handoff: a `submitted-invalid` job event
each time, one `submitted-invalid` needs_attention flag per ticket, and the
MAX_INVALID_HANDOFFS-th one fails the job for T6.

The bulky keys of a job's detail live in a file beside the database
(lib/job_detail.py, #653): enqueue writes it, and every Job read merges it
back, so callers pass the connection the row came from.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from lib import attention, exposure, job_detail, job_times, job_token, lane_limit
from lib.state_backup import (
    RESTORED,
    RESTORED_CLAIM,
    advance_wal_close_mtime,
    db_mtime_ns,
)

ACTIONS = (
    "plan",
    "build",
    "review",
    "fix",
    "update-from-main",
    "clarify",
    "intake-clarify",
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
    "qa",
    "qa-failed",
    "qa-env-failed",
    "qa-skill-drift",
    "qa-head-moved",
    "ci-rerun-refused",
    "ci-stale",
    "stack-parent-closed",
    "account-signin",
)
FINISHING_ACTIONS = ("review", "fix", "update-from-main", "qa")
QA_RECIPIENT = "qa"
RECIPIENTS = ("claude", "codex", "cursor", "gemini", "grok", QA_RECIPIENT)
STATES = ("queued", "running", "done", "failed", "cancelled")
BUSY_TIMEOUT_SECONDS = 30
# Device-pool placements (lib/device_pool.py). The table exists once a
# DevicePool has opened this database; a queued job never holds a row.
SLOTS_TABLE = "device_slots"
# Urgent and bug marks (issue #85), tier and unblocks (#258) per ticket for
# the claim order.
PRIORITY_TABLE = "ticket_priority"
_PRIORITY_COLUMNS = (
    ("tier", "INTEGER"), ("unblocks", "INTEGER NOT NULL DEFAULT 0"), ("own_tier", "INTEGER")
)
SUBMITTED_INVALID = "submitted-invalid"
PRE_READY_FAILED = "pre-ready-failed"
PRE_READY_PASSED = "pre-ready-passed"
LOCAL_GATE_FAILED = "local-gate-failed"
LOCAL_GATE_PASSED = "local-gate-passed"
LOCAL_GATE_UNAVAILABLE = "local-gate-unavailable"
LOCAL_GATE_RETRY = "local-gate-retry"
LOCAL_GATE_EVENTS = (LOCAL_GATE_PASSED, LOCAL_GATE_FAILED, LOCAL_GATE_UNAVAILABLE, LOCAL_GATE_RETRY)
CHECK_FAILED_EVENTS = (PRE_READY_FAILED, LOCAL_GATE_FAILED)
CHECK_PASSED_EVENTS = (PRE_READY_PASSED, LOCAL_GATE_PASSED)
INVALID_HANDOFF_EVENTS = (SUBMITTED_INVALID, *CHECK_FAILED_EVENTS)
_PASSED_DETAIL = re.compile(r"handoff [^:]+: at ([0-9a-f]{40})")
MAX_INVALID_HANDOFFS = 3


def passed_detail(comment_id: str, head: str) -> str:
    return f"handoff {comment_id}: at {head}"


def passed_head(detail: object) -> str | None:
    match = _PASSED_DETAIL.fullmatch(detail) if isinstance(detail, str) else None
    return match.group(1) if match else None


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


class _ClosingConnection(sqlite3.Connection):
    """sqlite3.Connection whose context manager commits/rolls back AND closes."""

    _db_path: Path | None = None

    def __exit__(self, exc_type, exc_val, exc_tb):  # type: ignore[override]
        wrote = exc_type is None and self.total_changes > 0
        path = self._db_path
        prev_mtime_ns = db_mtime_ns(path) if wrote and path is not None else None
        try:
            return super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()
            if wrote and path is not None:
                advance_wal_close_mtime(path, prev_mtime_ns)


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
                        connection.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone(),
                        connection,
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
        detail, spill = job_detail.split(request.detail)
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
                detail,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM jobs WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        assert row is not None
        if spill is not None and row["id"] == job_id:
            job_detail.write(job_detail.root(connection), job_id, spill)
        if (
            row["id"] == job_id
            and _has_table(connection, "needs_attention")
        ):
            connection.execute(
                "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
                (request.repo.lower(), request.ticket_id, attention.DEDUPE_DROPPED),
            )
        return _job(row, connection)

    def retarget_details(
        self, connection: sqlite3.Connection, repo: str, ticket_id: str, old: str, new: str
    ) -> None:
        rows = connection.execute(
            """
            SELECT rowid, detail FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ?
              AND (state IN ('queued', 'running', 'cancelled')
                   OR (state = 'failed' AND failure_handled = 0))
            """,
            (repo.lower(), ticket_id),
        ).fetchall()
        for rowid, detail in rows:
            data = job_detail.as_object(detail)
            if data is None or data.get("base_branch") != old:
                continue
            data["base_branch"] = new
            connection.execute(
                "UPDATE jobs SET detail = ? WHERE rowid = ?",
                (json.dumps(data, separators=(",", ":"), sort_keys=True), rowid),
            )

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
            candidate = next(
                (
                    candidate
                    for candidate in claim_order(connection, recipient, now=now)
                    if candidate.id not in excluded
                ),
                None,
            )
            if candidate is None:
                connection.commit()
                return None
            changed = connection.execute(
                """
                UPDATE jobs
                SET state = 'running', last_activity_at = ?, claim_token_hash = ?
                WHERE id = ? AND state = 'queued'
                """,
                (now, token_hash, candidate.id),
            ).rowcount
            if changed != 1:
                raise QueueError(f"job {candidate.id} is no longer queued")
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (candidate.id,),
            ).fetchone()
            assert updated is not None
            job = _job(updated, connection)
            connection.commit()
        return job

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
            job = _job(updated, connection)
            connection.commit()
        return job

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
            assert row is not None
            job = _job(row, connection)
            connection.commit()
        return job

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
                    job = _job(updated, connection)
                    connection.commit()
                    return job
                raise QueueError("finish requires a running job")
            job = _job(updated, connection)
            connection.commit()
        return job

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
                    "SELECT COUNT(*) FROM job_events WHERE job_id = ? AND event IN (?, ?, ?)",
                    (job_id, *INVALID_HANDOFF_EVENTS),
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
            job = _job(updated, connection)
            connection.commit()
        return count, job

    def record_head_check(
        self,
        job_id: str,
        *,
        event: str,
        comment_id: str,
        head: str,
        reason: str | None = None,
        proof: Proof | None = None,
    ) -> None:
        detail = passed_detail(comment_id, head) + ("" if reason is None else f": {reason}")
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
                (event, detail, self.now(), job_id),
            )
            connection.commit()

    def reserve_gate_retry(
        self, job_id: str, *, comment_id: str, head: str, expected_newest_event_id: int
    ) -> bool:
        prefix = f"handoff {comment_id}:"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_job_events(connection)
            newest = connection.execute(
                f"""
                SELECT MAX(events.id) FROM job_events AS events
                JOIN jobs ON lower(jobs.repo) = events.repo AND jobs.ticket_id = events.ticket_id
                WHERE jobs.id = ? AND events.event IN ({", ".join("?" * len(LOCAL_GATE_EVENTS))})
                  AND substr(events.detail, 1, ?) = ?
                """,
                (job_id, *LOCAL_GATE_EVENTS, len(prefix), prefix),
            ).fetchone()[0]
            if newest != expected_newest_event_id:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
                )
                SELECT id, lower(repo), ticket_id, action, recipient, ?, ?, ?
                FROM jobs WHERE id = ?
                """,
                (LOCAL_GATE_RETRY, f"{prefix} at {head}", self.now(), job_id),
            )
            connection.commit()
        return True

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
            return tuple(_job(row, connection) for row in rows)

    def mark_timed_out(self, job: Job) -> bool:
        """Fail a job the timeout sweep read, unless it changed or may run.

        A running job whose device slot records a remote start is never
        timed out (#87 r3 F2.1): failing it would let T6 start attempt 2
        beside a remote worker that may still run. It leaves running only
        through the poller's dead path, once its runner is gone and the
        device confirms its worker gone (lib/job_state.py), or as lost when
        the device stays unreachable (#662). An autostart claim with no runner
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
            return tuple(_job(row, connection) for row in rows)

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
        connection = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_SECONDS, factory=_ClosingConnection
        )
        connection._db_path = self.path
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
                job_times.install(connection)
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_state_recipient ON jobs(state, recipient)"
                )
                ensure_priority_table(connection)
                exposure.ensure_table(connection)
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
            own_tier INTEGER,
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
    own_tier: int | None = None,
    unblocks: int = 0,
) -> None:
    """Record a ticket's marks for the claim order."""
    connection.execute(
        f"""
        INSERT INTO {PRIORITY_TABLE} (repo, ticket_id, urgent, bug, tier, own_tier, unblocks, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, ticket_id) DO UPDATE SET
            urgent = excluded.urgent, bug = excluded.bug, tier = excluded.tier,
            own_tier = excluded.own_tier, unblocks = excluded.unblocks, updated_at = excluded.updated_at
        """,
        (repo.lower(), ticket_id, int(urgent), int(bug), tier, own_tier, unblocks, at),
    )


@dataclass(frozen=True)
class PriorityMark:
    urgent: bool = False
    bug: bool = False
    tier: int | None = None
    unblocks: int = 0
    own_tier: int | None = None


@dataclass(frozen=True)
class Candidate:
    id: str
    repo: str
    ticket_id: str
    action: str
    created_at: float
    resume: bool


def priority_marks(connection: sqlite3.Connection) -> dict[tuple[str, str], PriorityMark]:
    """Every ticket's marks, keyed (repo, ticket_id); empty with no table,
    and a column from after the database was made reads as unset."""
    if not _has_table(connection, PRIORITY_TABLE):
        return {}
    present = _columns(connection, PRIORITY_TABLE)
    tier = "tier" if "tier" in present else "NULL"
    own_tier = "own_tier" if "own_tier" in present else "NULL"
    unblocks = "unblocks" if "unblocks" in present else "0"
    return {
        (str(repo), str(ticket_id)): PriorityMark(
            bool(urgent), bool(bug), None if level is None else int(level), int(count),
            None if own is None else int(own),
        )
        for repo, ticket_id, urgent, bug, level, count, own in connection.execute(
            f"SELECT repo, ticket_id, urgent, bug, {tier}, {unblocks}, {own_tier} FROM {PRIORITY_TABLE}"
        )
    }


def rank(
    *, tier: int | None, urgent: bool, resume: bool, bug: bool, unblocks: int, finishing: bool = False
) -> tuple[bool, int, bool, int, int]:
    base = 0 if urgent else 1 if resume else 2 if bug else 3
    return tier is None, tier or 0, not finishing, base, -unblocks


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
    connection: sqlite3.Connection,
    recipient: str,
    *,
    now: float,
    serving: frozenset[str] | None = None,
) -> list[Candidate]:
    """Queued jobs for recipient in claim order (see the module doc); none
    while recipient's lane is paused by usage limits (#367): every account
    in `serving` is limited, or, with serving unknown (None), any is."""
    if lane_limit.recipient_paused(connection, recipient, now, serving) is not None:
        return []
    abandoned = (
        """
        AND NOT EXISTS (
            SELECT 1 FROM ticket_state AS ticket
            WHERE ticket.repo = lower(jobs.repo)
              AND ticket.ticket_id = jobs.ticket_id
              AND ticket.workflow_state IN ('abandoned', 'handed-off-externally')
        )
        """
        if _has_table(connection, "ticket_state")
        else ""
    )
    candidates = [
        Candidate(
            str(job_id), str(repo), str(ticket_id), str(action), float(created_at), is_resume(detail)
        )
        for job_id, repo, ticket_id, action, created_at, detail in connection.execute(
            f"""
            SELECT id, repo, ticket_id, action, created_at, detail FROM jobs
            WHERE state = 'queued' AND recipient = ? {abandoned}
            ORDER BY created_at, id
            """,
            (recipient,),
        )
    ]
    marks = priority_marks(connection) if candidates else {}

    def key(candidate: Candidate) -> tuple[bool, int, bool, int, int, float, str]:
        mark = marks.get((candidate.repo.lower(), candidate.ticket_id), PriorityMark())
        return (
            *rank(tier=mark.tier, urgent=mark.urgent, resume=candidate.resume, bug=mark.bug,
                  unblocks=mark.unblocks, finishing=candidate.action in FINISHING_ACTIONS),
            candidate.created_at,
            candidate.id,
        )

    return sorted(candidates, key=key)


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
    return _job(row, connection)


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
    finished_states = ", ".join("?" * len(job_detail.FINISHED_TICKET_STATES))
    finished = connection.execute(
        f"""
        SELECT 1 FROM jobs WHERE {harness}
          AND EXISTS (
            SELECT 1 FROM ticket_state
            WHERE ticket_state.repo = lower(jobs.repo)
              AND ticket_state.ticket_id = jobs.ticket_id
              AND ticket_state.workflow_state IN ({finished_states})
          )
        """,
        (job_id, *job_detail.FINISHED_TICKET_STATES),
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


def job_from_row(row: sqlite3.Row, connection: sqlite3.Connection) -> Job:
    return _job(row, connection)


def _job(row: sqlite3.Row, connection: sqlite3.Connection) -> Job:
    detail = job_detail.load_row(connection, str(row["id"]), row["detail"])
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
        detail=str(detail) if detail is not None else None,
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
