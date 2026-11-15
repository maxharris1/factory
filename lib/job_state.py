"""What a running job is really doing: claimed, running, stale or dead (#75).

The queue only knows `running`: a bot's `orch wait` claimed the job. That
is also true of a job whose bot never started it, of one whose heartbeat
stopped, and of one whose `orch job run` died. `derive` tells them apart,
and it is the one place that does: the poller (lib/dispatcher.py), the
dashboard (lib/dashboard.py) and `orch status` (lib/status.py) all call it.

* claimed: the job holds a device slot, no `orch job run` has recorded
  itself on it (no runner_pid), and nothing has heartbeated since the
  claim. The bot claimed it and never started it.
* dead: a runner_pid was recorded, that process is gone, and so is its
  worker (the local command's pid, or a remote worker the device confirms
  gone over SSH with lib/remote.py check_worker). A worker that may still
  run is not dead: its heartbeat has stopped, so it turns stale.
* stale: no heartbeat for more than stale_seconds (orch.yml
  job_states.stale_minutes, default 2 minutes).
* running: the heartbeat is fresh.

Order: dead, then claimed, then stale, then running. A job with no slot
(a grok wake, or a claim made without the device pool) is running or
stale by its heartbeat alone. A job that heartbeated after its claim with
no runner recorded (`orch job heartbeat` by hand, or `orch job run`
between its first heartbeat and recording itself) is not claimed: it is
running or stale by its heartbeat, so the poller never requeues work a bot
may have started.

The poller requeues a job that stays claimed longer than
release_claimed_seconds (orch.yml job_states.release_claimed_minutes,
default 5 minutes): the same job row goes back to queued, so the review
round and the T6 attempt are reused, not charged. A dead job takes the
existing failure path (failed, then T6).

#91: a job whose recorded runner is confirmed alive is running, never
stale: `orch job run` beats on its own thread while the command runs,
silent or not, so a late beat (a busy database) with a live runner is not a
dead job. A placed job whose worker never started (no worker pid, no remote
start) and that no live runner holds is released like a claim that never
started (`unstarted_too_long`), with a free-requeue budget per job row
(MAX_FREE_REQUEUES, shared with `orch job run`'s requeue of a harness that never started) after
which it is failed for T6 instead, so a start that can never work does not
loop. `requeue_unstarted` is that one guarded write: job back to queued (or
failed once the budget is spent), slot freed and a job_events row, in the
caller's transaction.

`liveness` gathers the process facts `derive` needs. It only runs checks
the caller supplies: the dashboard and `orch status` pass the box's pid
check and no remote check (they never SSH), so a remote job whose runner
died is stale there rather than dead until the poller confirms it.
"""

from __future__ import annotations

import signal
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from lib.device_pool import LINK_DROP_TAG, Placement
from lib.device_sync import SYNC_GRACE_SECONDS
from lib.queue import SLOTS_TABLE, retire_requeued
from lib.remote_follow import LAUNCH_TTL_SECONDS

CLAIMED = "claimed"
RUNNING = "running"
STALE = "stale"
DEAD = "dead"
JOB_STATES = (CLAIMED, RUNNING, STALE, DEAD)

DEFAULT_STALE_MINUTES = 2
DEFAULT_RELEASE_CLAIMED_MINUTES = 5
# `orch job run`'s default heartbeat (lib/job_run.py), well inside the
# default 2-minute stale threshold.
HEARTBEAT_SECONDS = 15.0
# The most an `orch job run --interval` override may beat: half the smallest
# stale threshold orch.yml allows (1 minute), so even a beat that lands late
# never shows a live job as stale, and far below the 2-minute default.
MAX_HEARTBEAT_SECONDS = 30.0
KILL_GRACE_SECONDS = 30.0

Alive = Callable[[int | None, str | None], bool]
# True: the remote worker is confirmed gone. False: it still runs.
# None: the device could not tell (unreachable, or no tag to name it).
RemoteGone = Callable[[Placement], bool | None]


@dataclass(frozen=True)
class JobStateConfig:
    stale_seconds: float = DEFAULT_STALE_MINUTES * 60.0
    release_claimed_seconds: float = DEFAULT_RELEASE_CLAIMED_MINUTES * 60.0


@dataclass(frozen=True)
class Liveness:
    """runner_alive: None when no runner was recorded or it was not checked.
    worker_gone: None unless the runner is gone and the worker was checked
    (True gone, False still running, None unknown)."""

    runner_alive: bool | None = None
    worker_gone: bool | None = None


def derive(
    *,
    last_activity_at: float,
    now: float,
    placement: Placement | None,
    stale_seconds: float,
    liveness: Liveness = Liveness(),
) -> str:
    """The state of a job the queue holds as running. See the module doc."""
    if placement is not None and placement.runner_pid is not None:
        if liveness.runner_alive is False and liveness.worker_gone is True:
            return DEAD
        if liveness.runner_alive is True:
            return RUNNING  # #91: a live runner beats while its command runs
    elif placement is not None and last_activity_at <= placement.placed_at:
        return CLAIMED
    if now - last_activity_at > stale_seconds:
        return STALE
    return RUNNING


def liveness(
    placement: Placement | None,
    alive: Alive | None,
    remote_gone: RemoteGone | None = None,
) -> Liveness:
    """Check the recorded runner and, once it is gone, its worker.

    alive None checks nothing. A remote start (remote_started) is only
    confirmed gone by remote_gone; without it the worker is unknown. A
    remote slot whose start was never attempted has no worker to wait for.
    """
    if placement is None or placement.runner_pid is None or alive is None:
        return Liveness()
    if alive(placement.runner_pid, placement.runner_start):
        return Liveness(runner_alive=True)
    if placement.remote_started:
        gone = remote_gone(placement) if remote_gone is not None else None
        return Liveness(runner_alive=False, worker_gone=gone)
    return Liveness(
        runner_alive=False,
        worker_gone=not alive(placement.worker_pid, placement.worker_start),
    )


_PLACEMENT_COLUMNS = (
    "job_id",
    "device",
    "harness",
    "os_user",
    "placed_at",
    "runner_pid",
    "runner_start",
    "worker_pid",
    "worker_start",
    "remote_started",
    "worker_tag",
)


def read_placements(connection: sqlite3.Connection) -> dict[str, Placement]:
    """Every device-pool slot, as job id -> Placement, without writing.

    Tolerates a database from before F2-02 (a missing column reads as
    NULL) or with no slot table. A row without placed_at is left out.
    """
    present = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({SLOTS_TABLE})")
    }
    if "job_id" not in present:
        return {}
    select = ", ".join(
        name if name in present else f"NULL AS {name}" for name in _PLACEMENT_COLUMNS
    )
    placements: dict[str, Placement] = {}
    for row in connection.execute(f"SELECT {select} FROM {SLOTS_TABLE}"):
        values = dict(zip(_PLACEMENT_COLUMNS, tuple(row)))
        placed_at = values["placed_at"]
        if not isinstance(placed_at, (int, float)) or isinstance(placed_at, bool):
            continue
        job_id = str(values["job_id"])
        placements[job_id] = Placement(
            job_id=job_id,
            device=str(values["device"]),
            harness=str(values["harness"]),
            os_user=str(values["os_user"]),
            placed_at=float(placed_at),
            runner_pid=_int(values["runner_pid"]),
            runner_start=_text(values["runner_start"]),
            worker_pid=_int(values["worker_pid"]),
            worker_start=_text(values["worker_start"]),
            remote_started=bool(values["remote_started"]),
            worker_tag=_text(values["worker_tag"]),
        )
    return placements


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return str(value) if value is not None else None


def claimed_too_long(
    state: str,
    placement: Placement | None,
    now: float,
    release_claimed_seconds: float,
) -> bool:
    """A claimed job the poller should requeue now."""
    return (
        state == CLAIMED
        and placement is not None
        and now - placement.placed_at > release_claimed_seconds
    )


# --- #91: a worker that never started, and the free-requeue budget ------------

# The job_events a free requeue of an unstarted job writes. Each job row gets
# MAX_FREE_REQUEUES of them; the next unstarted end fails it for T6 instead.
RELEASED_UNSTARTED = "released-unstarted"
REQUEUED_EARLY_EXIT = "requeued-early-exit"
FAILED_UNSTARTED = "failed-unstarted"
FREE_REQUEUE_EVENTS = (RELEASED_UNSTARTED, REQUEUED_EARLY_EXIT)
MAX_FREE_REQUEUES = 3
# `orch job run` records how its command ended while the job still runs:
# RUN_DIED for a non-zero status or a signal, RUN_EXITED for 0.
RUN_DIED = "run-died"
RUN_EXITED = "run-exited"
JOB_EVENTS_TABLE = "job_events"


def never_started(placement: Placement | None) -> bool:
    """No worker was ever recorded on the slot: no local pid, no remote start.

    A local worker's pid is recorded before its command may run, and a
    remote start before ssh is spawned (lib/placed_run.py), so a slot with
    neither never ran the harness.
    """
    return (
        placement is not None
        and placement.worker_pid is None
        and not placement.remote_started
    )


def unstarted_too_long(
    state: str,
    placement: Placement | None,
    facts: Liveness,
    *,
    last_activity_at: float,
    now: float,
    release_claimed_seconds: float,
) -> bool:
    """A running job whose worker never started and that no live runner holds.

    Claimed jobs have their own release (`claimed_too_long`). Otherwise: a
    recorded runner confirmed dead releases it now (it can never start the
    worker); with no runner recorded (a run refused or killed after its
    first heartbeat) it is released once nothing has heartbeated for
    release_claimed_seconds.
    """
    if state == CLAIMED or not never_started(placement):
        return False
    assert placement is not None
    if placement.runner_pid is not None:
        return facts.runner_alive is False
    return now - last_activity_at > release_claimed_seconds


def exit_reason(code: int | None) -> str:
    """How a command ended, for a job_events detail."""
    if code is None:
        return "no exit status (orch job run hit an error)"
    if code > 128 and code - 128 < 65:
        number = code - 128
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = f"signal {number}"
        return f"killed by {name} (exit {code})"
    return f"exit {code}"


def has_job_events(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (JOB_EVENTS_TABLE,),
        ).fetchone()
        is not None
    )


def record_job_event(
    connection: sqlite3.Connection,
    job_id: str,
    event: str,
    detail: str,
    now: float,
) -> bool:
    """Add a job_events row for job_id. False (nothing written) when the job
    or the table (made by the dispatcher) is missing."""
    if not has_job_events(connection):
        return False
    row = connection.execute(
        "SELECT repo, ticket_id, action, recipient FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return False
    connection.execute(
        f"""
        INSERT INTO {JOB_EVENTS_TABLE} (
            job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (job_id, str(row[0]).lower(), row[1], row[2], row[3], event, detail, now),
    )
    return True


def free_requeues(connection: sqlite3.Connection, job_id: str) -> int:
    if not has_job_events(connection):
        return 0
    marks = ", ".join("?" for _ in FREE_REQUEUE_EVENTS)
    row = connection.execute(
        f"SELECT COUNT(*) FROM {JOB_EVENTS_TABLE} WHERE job_id = ? AND event IN ({marks})",
        (job_id, *FREE_REQUEUE_EVENTS),
    ).fetchone()
    return int(row[0])


def last_run_end(
    connection: sqlite3.Connection, job_id: str, since: float
) -> tuple[str, str] | None:
    """The latest RUN_DIED / RUN_EXITED (event, detail) recorded at or after since."""
    if not has_job_events(connection):
        return None
    row = connection.execute(
        f"""
        SELECT event, detail FROM {JOB_EVENTS_TABLE}
        WHERE job_id = ? AND event IN (?, ?) AND recorded_at >= ?
        ORDER BY id DESC LIMIT 1
        """,
        (job_id, RUN_DIED, RUN_EXITED, since),
    ).fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def _free_unstarted_slot(
    connection: sqlite3.Connection,
    job_id: str,
    slot_guard: str,
    slot_params: tuple[object, ...],
    *,
    last_activity_at: float | None = None,
) -> bool:
    """Delete the slot of a running job whose slot matches slot_guard. False,
    with nothing changed, when either guard misses."""
    activity = "" if last_activity_at is None else " AND last_activity_at = ?"
    params: tuple[object, ...] = (job_id,) if last_activity_at is None else (
        job_id, last_activity_at
    )
    running = connection.execute(
        f"SELECT 1 FROM jobs WHERE id = ? AND state = 'running'{activity}", params
    ).fetchone()
    if running is None:
        return False
    slot = connection.execute(
        f"SELECT 1 FROM {SLOTS_TABLE} WHERE job_id = ? AND {slot_guard}",
        (job_id, *slot_params),
    ).fetchone()
    if slot is None:
        return False
    connection.execute(f"DELETE FROM {SLOTS_TABLE} WHERE job_id = ?", (job_id,))
    return True


def requeue_unstarted(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    now: float,
    event: str,
    reason: str,
    slot_guard: str,
    slot_params: tuple[object, ...],
    last_activity_at: float | None = None,
) -> tuple[str, str] | None:
    """Requeue (or, budget spent, fail) a running job that never started, and
    free its slot, in the caller's open write transaction.

    Guarded: the job is still running (and, when given, has not heartbeated
    since last_activity_at), and its slot matches slot_guard (SQL over the
    slot row, with slot_params). Requeued: the same job row goes back to
    queued with its queue-wait clock restarted, so no round and no T6
    attempt is charged. Once the row has used MAX_FREE_REQUEUES it is failed
    instead, for T6. A finished ticket's job, or a restored claim, is
    cancelled, not requeued (retire_requeued, #108). Returns (event,
    detail), or None when the guard did not match and nothing changed.
    """
    if not _free_unstarted_slot(
        connection, job_id, slot_guard, slot_params, last_activity_at=last_activity_at
    ):
        return None
    used = free_requeues(connection, job_id)
    if used < MAX_FREE_REQUEUES:
        connection.execute(
            "UPDATE jobs SET state = 'queued', last_activity_at = ? WHERE id = ?",
            (now, job_id),
        )
        retired = retire_requeued(connection, job_id, reason, now=now)
        if retired is not None:
            return "cancelled", retired
        outcome = event
        detail = (
            f"{reason}; requeued, no round charged "
            f"(free requeue {used + 1} of {MAX_FREE_REQUEUES})"
        )
    else:
        connection.execute(
            """
            UPDATE jobs SET state = 'failed', finished_at = ?, last_activity_at = ?
            WHERE id = ?
            """,
            (now, now, job_id),
        )
        outcome = FAILED_UNSTARTED
        detail = (
            f"{reason}; already requeued {used} times without starting, failed for T6"
        )
    record_job_event(connection, job_id, outcome, detail, now)
    return outcome, detail


LINK_DROP = "link-drop"
REQUEUED_LINK_DROP = "requeued-link-drop"
MAX_LINK_DROP_REQUEUES = 6
LINK_DROP_BACKOFF_SECONDS = (60, 120, 300, 600, 600, 600)
DEVICE_CLOCK_SKEW_SECONDS = 4.5 * 60


def link_drop_end(connection: sqlite3.Connection, job_id: str) -> tuple[str, float] | None:
    """(detail, recorded_at) when the row's latest run end since its last
    REQUEUED_LINK_DROP is a link drop of the run its slot still names."""
    if not has_job_events(connection):
        return None
    row = connection.execute(
        f"""
        SELECT event, detail, recorded_at FROM {JOB_EVENTS_TABLE}
        WHERE job_id = ? AND event IN (?, ?, ?, ?)
        ORDER BY id DESC LIMIT 1
        """,
        (job_id, RUN_DIED, RUN_EXITED, LINK_DROP, REQUEUED_LINK_DROP),
    ).fetchone()
    if row is None or row[0] != LINK_DROP:
        return None
    detail = str(row[1])
    slot = connection.execute(
        f"SELECT worker_tag FROM {SLOTS_TABLE} WHERE job_id = ?", (job_id,)
    ).fetchone()
    if slot is not None and detail.rpartition(LINK_DROP_TAG)[2] != slot[0]:
        return None
    return detail, float(row[2])


def release_link_dropped(connection: sqlite3.Connection, job_id: str) -> None:
    """Free a failed, unhandled link-dropped row's slot in the caller's write
    transaction."""
    if link_drop_end(connection, job_id) is None:
        return
    connection.execute(
        f"""
        DELETE FROM {SLOTS_TABLE} WHERE job_id = ? AND EXISTS (
            SELECT 1 FROM jobs WHERE id = ? AND state = 'failed' AND failure_handled = 0
        )
        """,
        (job_id, job_id),
    )


def link_drop_requeues(connection: sqlite3.Connection, job_id: str) -> int:
    if not has_job_events(connection):
        return 0
    row = connection.execute(
        f"SELECT COUNT(*) FROM {JOB_EVENTS_TABLE} WHERE job_id = ? AND event = ?",
        (job_id, REQUEUED_LINK_DROP),
    ).fetchone()
    return int(row[0])


def link_drop_settled_at(recorded_at: float, timeout_seconds: float) -> float:
    """When the device has killed the dropped run's worker on its own (#199 r1)."""
    return (
        recorded_at
        + LAUNCH_TTL_SECONDS
        + timeout_seconds
        + KILL_GRACE_SECONDS
        + DEVICE_CLOCK_SKEW_SECONDS
    )


def requeue_link_drop(
    connection: sqlite3.Connection, job_id: str, *, now: float, reason: str
) -> tuple[str, str] | None:
    """Requeue a failed, link-dropped job row in the caller's write transaction.

    Guarded: still failed with its failure unhandled. The same row goes back
    to queued with action, head, generation, attempt and detail kept, so no
    round and no T6 attempt is charged, and its slot is freed. A finished
    ticket's job is cancelled instead (retire_requeued). Returns (event,
    detail), or None when the guard did not match.
    """
    used = link_drop_requeues(connection, job_id)
    return _requeue_failed(
        connection,
        job_id,
        now=now,
        reason=reason,
        event=REQUEUED_LINK_DROP,
        detail=(
            f"{reason}; requeued, no round or attempt charged "
            f"(link-drop requeue {used + 1} of {MAX_LINK_DROP_REQUEUES})"
        ),
    )


def _requeue_failed(
    connection: sqlite3.Connection, job_id: str, *, now: float, reason: str, event: str, detail: str
) -> tuple[str, str] | None:
    """Put a failed, unhandled row back to queued as itself and free its slot,
    recording event; a finished ticket's job is cancelled instead."""
    changed = connection.execute(
        """
        UPDATE jobs SET state = 'queued', finished_at = NULL, last_activity_at = ?
        WHERE id = ? AND state = 'failed' AND failure_handled = 0
        """,
        (now, job_id),
    ).rowcount
    if changed != 1:
        return None
    connection.execute(f"DELETE FROM {SLOTS_TABLE} WHERE job_id = ?", (job_id,))
    retired = retire_requeued(connection, job_id, reason, now=now)
    if retired is not None:
        return "cancelled", retired
    record_job_event(connection, job_id, event, detail, now)
    return event, detail


# --- #357: a start refused because a factory checkout lags -------------------

PREFLIGHT_LAGGING = "preflight-lagging"
REQUEUED_LAGGING = "requeued-lagging"
LAGGING_BACKOFF_SECONDS = (30, 60, 120, 240)
LAGGING_WAIT_SECONDS = SYNC_GRACE_SECONDS


@dataclass(frozen=True)
class LaggingEnd:
    detail: str
    first_refused_at: float
    requeues: int

    def backoff(self) -> int:
        return LAGGING_BACKOFF_SECONDS[min(self.requeues, len(LAGGING_BACKOFF_SECONDS) - 1)]


def fail_lagging(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    now: float,
    reason: str,
    slot_guard: str,
    slot_params: tuple[object, ...],
) -> tuple[str, str] | None:
    """Fail a running job the waiter's preflight refused for a lagging
    checkout, unhandled, so T6 requeues it after a backoff; no free requeue
    is charged. Guarded as requeue_unstarted; None when the guard missed."""
    if not _free_unstarted_slot(connection, job_id, slot_guard, slot_params):
        return None
    connection.execute(
        """
        UPDATE jobs SET state = 'failed', finished_at = ?, last_activity_at = ?,
            failure_handled = 0
        WHERE id = ?
        """,
        (now, now, job_id),
    )
    record_job_event(connection, job_id, PREFLIGHT_LAGGING, reason, now)
    return PREFLIGHT_LAGGING, reason


def requeue_lagging(
    connection: sqlite3.Connection, job_id: str, *, now: float, reason: str
) -> tuple[str, str] | None:
    """Requeue a failed, unhandled lagging-refused row as itself in the
    caller's write transaction: no round, attempt or free requeue charged."""
    return _requeue_failed(
        connection,
        job_id,
        now=now,
        reason=reason,
        event=REQUEUED_LAGGING,
        detail=f"{reason}; requeued, no round or attempt charged",
    )


def lagging_end(connection: sqlite3.Connection, job_id: str) -> LaggingEnd | None:
    """The row's refusal when its latest end is a lagging preflight, or None."""
    if not has_job_events(connection):
        return None
    ends = (PREFLIGHT_LAGGING, REQUEUED_LAGGING, RUN_DIED, RUN_EXITED, LINK_DROP,
            REQUEUED_LINK_DROP)
    marks = ", ".join("?" for _ in ends)
    rows = connection.execute(
        f"""
        SELECT event, detail, recorded_at FROM {JOB_EVENTS_TABLE}
        WHERE job_id = ? AND event IN ({marks}) ORDER BY id
        """,
        (job_id, *ends),
    ).fetchall()
    if not rows or rows[-1][0] != PREFLIGHT_LAGGING:
        return None
    first_refused_at = float(rows[-1][2])
    requeues = 0
    for event, _detail, recorded_at in reversed(rows):
        if event in (RUN_DIED, RUN_EXITED):
            break
        if event == PREFLIGHT_LAGGING:
            first_refused_at = float(recorded_at)
        elif event == REQUEUED_LAGGING:
            requeues += 1
    return LaggingEnd(str(rows[-1][1]), first_refused_at, requeues)
