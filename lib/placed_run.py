"""`orch job run` for a job the device pool placed (docs/SPEC.md F2-02).

`orch wait` claims through the device pool, so every harness job it hands
out holds one slot on one device. `orch job run` then:

* refuses (exit 2, nothing started, slot kept) a job that holds no slot,
  whose slot is for another bot user, that an autostart waiter claimed
  when this process is not that waiter's own child (#80), whose device is no longer in
  machines.yml, that another live `orch job run` already runs, or whose
  earlier worker (left by a runner that died) is not confirmed gone: a
  live recorded pid on the box, or a remote check that does not answer
  gone. A worker's identity is never overwritten while it may still run;
* records itself on the slot as the job's runner;
* on the box (the `local: true` device), runs the command as before and
  records its pid as the worker before the command may start
  (lib/job_run.py holds it at a gate until then);
* on a remote device, runs the command there over the box's SSH alias for
  this bot (`ssh -l <bot> -- <alias> <start script>`, lib/remote.py) in
  `<workdir_root>/jobs/<job-id>`, under a fresh worker tag recorded on the
  slot first. `--worktree` is refused for a remote job: worktrees are made
  on the box only;
* when the command ends (done, failed or killed), releases the slot only
  once the worker is confirmed gone: a local command has exited; a remote
  device answers that no process carries the start's worker tag and none
  is left in its recorded group, after a SIGTERM if it was left running. Otherwise the slot stays held and the
  dispatcher's DeviceMonitor confirms it on a later poll;
* for an autostart job, fails the job for T6 (before releasing the slot)
  when the command ended without `orch job done` (#80);
* #91: records how the command ended (`ended`: a run-died or run-exited
  job_events row, with the exit status) while the job is still running, so
  the poller can fail a job whose harness died and say why;
* #91: when the harness never started, requeues the job in the same
  transaction that frees the slot (lib/job_state.py requeue_unstarted, no
  round charged, within the free-requeue budget), instead of leaving a
  running job with no slot. Never started means no worker was recorded
  (setup failed), or the command ended early: a remote start that exited
  125 (the start script refused), 126 or 127 (the harness could not be
  run, e.g. not on the login PATH) or 255 (ssh could not connect), or a
  box command that exited 126 or 127, within EARLY_EXIT_SECONDS of launch.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from lib.device_monitor import remote_target
from lib.device_pool import DevicePool, Placement, read_autostart
from lib.job_state import (
    REQUEUED_EARLY_EXIT,
    RUN_DIED,
    RUN_EXITED,
    exit_reason,
    record_job_event,
    requeue_unstarted,
)
from lib.machines_yml import Device, MachinesConfig
from lib.queue import QueueError, SQLiteJobQueue
from lib.remote import (
    GONE,
    RUNNING,
    RemoteTarget,
    Transport,
    check_worker,
    new_tag,
    ssh_argv,
    start_script,
    stop_worker,
)
from lib.wait import retry_locked
from lib.worktrees import pid_alive, process_start

STOP_CHECKS = 5
STOP_PAUSE_SECONDS = 2.0
# #91: exit statuses that mean the harness never ran (see the module doc),
# and how soon after launch they must come to count as an early exit.
REMOTE_EARLY_EXITS = frozenset({125, 126, 127, 255})
LOCAL_EARLY_EXITS = frozenset({126, 127})
EARLY_EXIT_SECONDS = 120.0

Log = Callable[[str], None]


class PlacementError(Exception):
    """The placed job cannot be started. The message is one line."""


@dataclass
class PlacedRun:
    pool: DevicePool
    placement: Placement
    device: Device
    owner: str
    log: Log
    transport: Transport
    target: RemoteTarget | None = None
    autostart: bool = False
    sleep: Callable[[float], None] = time.sleep
    alive: Callable[[int | None, str | None], bool] = pid_alive
    runner: int | None = None
    clock: Callable[[], float] = time.monotonic
    _worker: list[tuple[int, str | None]] = field(default_factory=list)
    _launched: list[float] = field(default_factory=list)

    @property
    def remote(self) -> bool:
        return self.target is not None

    def argv(self, command: Sequence[str], env: Mapping[str, str]) -> list[str]:
        if self.target is None:
            return list(command)
        return ssh_argv(self.target, start_script(self.target, command, env))

    def before_start(self) -> None:
        """Record a remote start before ssh is spawned (a crash then still checks)."""
        if self.target is not None:
            target = self.target
            retry_locked(
                lambda: self.pool.attach_worker(
                    self.placement.job_id, owner=self.owner, remote=True, tag=target.tag
                )
            )
            self._launched.append(self.clock())
            self.log(
                f"starting job {self.placement.job_id} on {self.device.name} "
                f"over ssh alias {self.target.alias} as {self.target.user}"
            )

    def started(self, pid: int) -> None:
        if self.target is not None:
            return  # the local pid is ssh; the worker is on the device
        start = process_start(pid)
        self._worker.append((pid, start))
        self._launched.append(self.clock())
        retry_locked(
            lambda: self.pool.attach_worker(
                self.placement.job_id,
                owner=self.owner,
                pid=pid,
                start=start,
            )
        )

    def ended(self, code: int | None) -> None:
        """#91: record how the command ended while the job still runs.

        RUN_EXITED for status 0, RUN_DIED otherwise (with the status or
        signal). A job already done or failed records nothing. Never raises.
        """
        job_id = self.placement.job_id
        event = RUN_EXITED if code == 0 else RUN_DIED
        detail = f"orch job run's command on {self.device.name}: {exit_reason(code)}"

        def write() -> None:
            with self.pool.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is not None and row["state"] == "running":
                    record_job_event(connection, job_id, event, detail, self.pool.queue.now())
                connection.commit()

        try:
            retry_locked(write)
        except (QueueError, sqlite3.Error, OSError) as exc:
            self.log(f"could not record how job {job_id}'s command ended: {exc}")

    def unstarted(self, code: int | None) -> bool:
        """#91: the harness never ran (see the module doc)."""
        placement = self.placement
        if not self._launched:
            # Nothing was launched by this run; only a slot no earlier run
            # left a worker on is unstarted.
            return placement.worker_pid is None and not placement.remote_started
        if code is None or self.clock() - self._launched[0] > EARLY_EXIT_SECONDS:
            return False
        return code in (REMOTE_EARLY_EXITS if self.target is not None else LOCAL_EARLY_EXITS)

    def finish(self, code: int | None = None) -> bool:
        """Release the slot once the worker is confirmed gone. True if released.

        #91: with the command's exit status, a job whose harness never
        started is requeued (or failed, budget spent) in the same step.
        """
        job_id = self.placement.job_id
        if self.target is None:
            workers = list(self._worker)
            if self.placement.worker_pid is not None:
                # a worker an earlier, dead runner of this job left behind
                workers.append((self.placement.worker_pid, self.placement.worker_start))
            for pid, start in workers:
                if self.alive(pid, start):
                    self.log(f"worker pid {pid} still runs; slot held")
                    return False
            return self._release(code)
        status = check_worker(self.transport, self.target)
        checks = 0
        while status == RUNNING and checks < STOP_CHECKS:
            if checks == 0:
                self.log(f"job {job_id} worker still runs on {self.device.name}; stopping it")
                stop_worker(self.transport, self.target)
            self.sleep(STOP_PAUSE_SECONDS)
            status = check_worker(self.transport, self.target)
            checks += 1
        if status == GONE:
            return self._release(code)
        self.log(
            f"job {job_id} worker on {self.device.name} not confirmed gone ({status}); "
            "slot held until the dispatcher confirms it"
        )
        return False

    def _release(self, code: int | None = None) -> bool:
        if self.unstarted(code) and self._requeue(code):
            return True
        released = retry_locked(
            lambda: self.pool.release(self.placement.job_id, owner=self.owner)
        )
        if released:
            self.log(f"released {self.device.name} slot of job {self.placement.job_id}")
        return released

    def _requeue(self, code: int | None) -> bool:
        """Requeue the unstarted job and free its slot in one transaction.

        Guarded on the slot still being this run's (owner and runner pid)
        and the job still running. False when the guard did not match (the
        job was finished meanwhile); the caller then frees the slot as usual.
        """
        job_id = self.placement.job_id
        if self._launched:
            reason = (
                f"its command on {self.device.name} ended early ({exit_reason(code)}) "
                "before the harness ran"
            )
        else:
            reason = f"orch job run on {self.device.name} never started its command"

        def write() -> tuple[str, str] | None:
            with self.pool.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                result = requeue_unstarted(
                    connection,
                    job_id,
                    now=self.pool.queue.now(),
                    event=REQUEUED_EARLY_EXIT,
                    reason=reason,
                    slot_guard="harness = ? AND runner_pid IS ?",
                    slot_params=(self.owner, self.runner),
                )
                connection.commit()
                return result

        result = retry_locked(write)
        if result is None:
            return False
        self.log(f"released {self.device.name} slot of job {job_id}; {result[0]}: {result[1]}")
        return True


def check_autostart(
    queue: SQLiteJobQueue,
    *,
    job_id: str,
    bot: str,
    parent_pid: int | None = None,
) -> tuple[int, str | None] | None:
    """Refuse a run of an autostart claim unless its parent is the claiming waiter.

    Codex PR #82 r2 F2.2: `orch job run` calls this before its first
    heartbeat, and it only reads. A run refused here leaves the job exactly
    as claimed (no heartbeat since the claim, no runner), so the poller's
    claimed-job release requeues it (lib/job_state.py) instead of it
    waiting out the full timeout. That covers a bot turn's run and a
    waiter's own child orphaned before this check (the waiter exited
    between spawning it and here). Returns the claiming waiter, for
    prepare(checked_autostart=...), or None for a job no autostart waiter
    claimed.
    """
    with queue.connect() as connection:
        claimer = read_autostart(connection, job_id)
    if claimer is not None:
        _refuse_unless_child(job_id, bot, claimer, parent_pid)
    return claimer


def _refuse_unless_child(
    job_id: str,
    bot: str,
    claimer: tuple[int, str | None],
    parent_pid: int | None,
) -> None:
    pid, start = claimer
    parent = os.getppid() if parent_pid is None else parent_pid
    if parent != pid or (start is not None and process_start(parent) != start):
        raise PlacementError(
            f"job {job_id} was claimed by {bot}'s autostart waiter "
            f"(pid {pid}), which starts it itself; a bot turn "
            "must not run it, so nothing was started"
        )


def prepare(
    pool: DevicePool,
    config: MachinesConfig,
    *,
    job_id: str,
    bot: str,
    owner: str,
    worktree: bool,
    log: Log,
    transport: Transport,
    alive: Callable[[int | None, str | None], bool] = pid_alive,
    pid: int | None = None,
    parent_pid: int | None = None,
    checked_autostart: tuple[int, str | None] | None = None,
) -> PlacedRun:
    """Record this process as the job's runner on its slot.

    checked_autostart is what check_autostart returned before the run's
    first heartbeat: this run's parent was then the claiming waiter. The
    slot must still name that waiter; the parent is not looked at again, so
    a waiter that exits in between (this run is reparented) does not strand
    the job it already started.
    """
    placement = pool.placement(job_id)
    if placement is None:
        raise PlacementError(
            f"job {job_id} holds no device slot (released, or claimed without "
            "the device pool); fail it with orch job done --failed"
        )
    if placement.harness != owner or placement.os_user != bot:
        raise PlacementError(
            f"job {job_id}'s slot is for {placement.os_user}, not {bot}"
        )
    if placement.autostarted:
        assert placement.autostart_pid is not None
        claimer = (placement.autostart_pid, placement.autostart_start)
        if checked_autostart is None:
            _refuse_unless_child(job_id, bot, claimer, parent_pid)
        elif checked_autostart != claimer:
            raise PlacementError(
                f"job {job_id} was claimed again by {bot}'s autostart waiter "
                f"(pid {placement.autostart_pid}) after this run checked its parent; "
                "nothing was started"
            )
    device = config.device(placement.device)
    if device is None:
        raise PlacementError(
            f"job {job_id} is placed on {placement.device!r}, "
            "which machines.yml no longer lists"
        )
    target: RemoteTarget | None = None
    if not device.local:
        if worktree:
            raise PlacementError(
                f"job {job_id} is placed on remote device {device.name}; "
                "--worktree is box-only, run it without --worktree"
            )
        target = remote_target(device, placement.harness, job_id, new_tag())
        if target is None:
            raise PlacementError(
                f"device {device.name} has no usable ssh_alias or workdir_root "
                f"for {placement.harness}"
            )
        if target.user != bot:
            raise PlacementError(f"device {device.name} maps {owner} to {target.user}, not {bot}")
    runner = os.getpid() if pid is None else pid
    try:
        retry_locked(
            lambda: pool.attach_runner(
                job_id,
                owner=owner,
                pid=runner,
                start=process_start(runner),
                alive=alive,
            )
        )
    except QueueError as exc:
        raise PlacementError(str(exc)) from exc
    _earlier_worker_gone(placement, device, transport, alive)
    return PlacedRun(
        pool=pool,
        placement=placement,
        device=device,
        owner=owner,
        log=log,
        transport=transport,
        target=target,
        autostart=placement.autostarted,
        alive=alive,
        runner=runner,
    )


def _earlier_worker_gone(
    placement: Placement,
    device: Device,
    transport: Transport,
    alive: Callable[[int | None, str | None], bool],
) -> None:
    """Refuse to start while a worker an earlier, dead runner left may still run.

    Its identity (pid or worker tag) is on the slot; starting another
    worker would overwrite it, and the slot could then be released while
    the earlier one still runs.
    """
    job_id = placement.job_id
    if device.local:
        if alive(placement.worker_pid, placement.worker_start):
            raise PlacementError(
                f"job {job_id}'s earlier worker pid {placement.worker_pid} still runs; "
                "slot kept, nothing started"
            )
        return
    if not placement.remote_started:
        return
    earlier = remote_target(device, placement.harness, job_id, placement.worker_tag)
    status = (
        check_worker(transport, earlier)
        if earlier is not None and placement.worker_tag is not None
        else "unknown"
    )
    if status != GONE:
        raise PlacementError(
            f"job {job_id}'s earlier worker on {device.name} is not confirmed gone "
            f"({status}); slot kept, nothing started"
        )
