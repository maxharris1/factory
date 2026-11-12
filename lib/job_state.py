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

`liveness` gathers the process facts `derive` needs. It only runs checks
the caller supplies: the dashboard and `orch status` pass the box's pid
check and no remote check (they never SSH), so a remote job whose runner
died is stale there rather than dead until the poller confirms it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from lib.device_pool import Placement
from lib.queue import SLOTS_TABLE

CLAIMED = "claimed"
RUNNING = "running"
STALE = "stale"
DEAD = "dead"
JOB_STATES = (CLAIMED, RUNNING, STALE, DEAD)

DEFAULT_STALE_MINUTES = 2
DEFAULT_RELEASE_CLAIMED_MINUTES = 5
# `orch job run`'s default heartbeat (lib/job_run.py), well inside the
# smallest stale threshold orch.yml allows (1 minute).
HEARTBEAT_SECONDS = 30.0

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
