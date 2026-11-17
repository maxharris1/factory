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
* records itself on the slot as the job's runner, with the hash of the
  run's secret token for an autostart job (#96, lib/job_token.py);
* on the box (the `local: true` device), runs the command as before and
  records its pid as the worker before the command may start
  (lib/job_run.py holds it at a gate until then);
* on a remote device, launches the command there detached (#128) over the
  box's SSH alias for this bot (`ssh -l <bot> -- <alias> <launch script>`,
  lib/remote.py) in `<workdir_root>/jobs/<job-id>`, under the bot's login
  shell (`bash -lc`) and a fresh worker tag recorded on the slot first, and
  follows it with short SSH calls (lib/remote_follow.py) through dropped
  links. The tag names the launch, so a retried launch never starts a
  second harness. `run-died` is recorded for a remote run only when its
  device has an exit status for it, confirms the worker gone without one, or
  answered nothing certain for the follow's grace window (`ended`); a
  dropped call is never enough. A codex command runs
  by its bare name, found on the device's login PATH, and each of its
  `--add-dir` directories is given only if the device has it (`test -d`,
  lib/codex_sandbox.py `for_device`); box paths never reach the device's
  argv unguarded;
* with `--worktree` on a remote device (#94), first checks over the same
  alias that git, gh and the harness binary exist under `bash -lc`, then
  clones or fetches the job's checkout into that directory at the job's
  head (and branch), copying the brief beside it. Any failure there
  (a missing tool, an unreachable device, a failed clone) starts nothing:
  the job is requeued and its slot freed in one transaction, with no round
  or attempt charged (`requeue`). The box keeps its git worktrees
  (lib/worktrees.py). Harnesses never build a remote command themselves;
* when the command ends (done, failed or killed), releases the slot only
  once the worker is confirmed gone: a local command has exited; a remote
  device answers that no process carries the start's worker tag and none
  is left in its recorded group, after a SIGTERM if it was left running. Otherwise the slot stays held and the
  dispatcher's DeviceMonitor confirms it on a later poll;
* for an autostart job on the box, fails the job for T6 when the command ended
  without `orch job done` (#80): after the unstarted check, so a harness
  that never started is requeued instead, but before the slot (with the
  run's token) is deleted, so no other process can finish the job
  in between (`finish(settle=...)`). A remote autostart job is finished
  by its waiter from the run's exit status and final ORCH-HANDOFF line
  (lib/waiter.py);
* #91: records how the command ended (`ended`: a run-died or run-exited
  job_events row, with the exit status) while the job is still running, so
  the poller can fail a job whose harness died and say why;
* #91: when the harness never started, requeues the job in the same
  transaction that frees the slot (lib/job_state.py requeue_unstarted, no
  round charged, within the free-requeue budget), instead of leaving a
  running job with no slot. An exit status never proves that (#99 A2): a
  harness that ran may exit 127 too. Never started means no earlier run
  left a worker and this run has evidence it reached no harness: nothing
  was spawned (setup failed), the box command was never exec'd
  (lib/job_run.py's gate reports the exec), or the device answers that
  this start wrote no launch record (lib/remote.py `harness_launched`:
  the start refused, or the harness was not on the login PATH). A device
  that cannot answer counts as started.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, BinaryIO

from lib.codex_sandbox import for_device
from lib.device_monitor import remote_target
from lib.device_pool import DevicePool, Placement, link_drop_detail, read_autostart
from lib.job_run import OUTPUT_GRACE_SECONDS, Budget
from lib.job_state import (
    LINK_DROP,
    REQUEUED_EARLY_EXIT,
    RUN_DIED,
    RUN_EXITED,
    exit_reason,
    record_job_event,
    requeue_unstarted,
)
from lib.machines_yml import Device, MachinesConfig
from lib.process_tree import run_tag
from lib.queue import SLOTS_TABLE, Proof, QueueError, SQLiteJobQueue, require_proof
from lib.remote import (
    ENV_FILE,
    GONE,
    RUNNING,
    ByteTransport,
    CheckoutError,
    RemoteError,
    RemoteTarget,
    Transport,
    brief_path,
    check_worker,
    checkout,
    harness_launched,
    launch_script,
    new_tag,
    plain_branch,
    preflight,
    run_file,
    start_script,
    stop_worker,
)
from lib.remote_follow import (
    FOLLOW_GRACE_SECONDS,
    LAUNCH_TTL_SECONDS,
    LOST,
    REFUSED,
    STOPPED,
    Control,
    FollowEnd,
    Follower,
)
from lib.remote_follow import GONE as WORKER_GONE
from lib.wait import retry_locked
from lib.worktrees import WorktreeError, check_remote_url, pid_alive, process_start

STOP_CHECKS = 5
STOP_PAUSE_SECONDS = 2.0
KILL_CHECK = 2

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
    proof: Proof | None = None
    sleep: Callable[[float], None] = time.sleep
    alive: Callable[[int | None, str | None], bool] = pid_alive
    runner: int | None = None
    requeued: bool = False
    spawned: bool = False
    exec_seen: bool = False
    grace: float = FOLLOW_GRACE_SECONDS
    follow_end: FollowEnd | None = None
    _worker: list[tuple[int, str | None]] = field(default_factory=list)

    @property
    def remote(self) -> bool:
        return self.target is not None

    def remote_env(self, *, worktree: bool, brief: bool) -> dict[str, str]:
        """The checkout's remote paths for the harness (#94); empty on the box."""
        if self.target is None or not worktree:
            return {}
        env = {"ORCH_JOB_WORKTREE": self.target.job_dir}
        if brief:
            env["ORCH_JOB_BRIEF"] = brief_path(self.target)
        return env

    def remote_checkout(
        self,
        *,
        harness: str,
        repo: str,
        head: str | None,
        detail: str | None,
        url_template: str,
        brief: bytes | None,
    ) -> None:
        """Preflight, then make the job's checkout on its remote device (#94).

        Raises CheckoutError (one line) when the job cannot start there;
        nothing has been started and no worker recorded, so `requeue` may
        put it back.
        """
        assert self.target is not None
        try:
            url = url_template.format(repo=repo)
            check_remote_url(url)
        except (WorktreeError, KeyError, IndexError, ValueError) as exc:
            raise CheckoutError(_line(exc)) from exc
        base, branch = _branches(detail)
        # the binary the device will run: a codex command's bare name
        preflight(self.transport, self.target, for_device([harness]).argv[0])
        try:
            sha, reused = checkout(
                self.transport,
                self.target,
                url=url,
                head=head,
                base_branch=base,
                branch=branch,
                brief=brief,
            )
        except RemoteError as exc:
            raise CheckoutError(_line(exc)) from exc
        self.log(
            f"job checkout {self.target.job_dir} on {self.device.name} at {sha}"
            f"{' (reused)' if reused else ''}"
        )

    def requeue(self, reason: str) -> bool:
        """Put a job whose start failed back in the queue; free its slot (#94).

        Nothing ran and no worker was recorded, so no round or attempt is
        charged. True when requeued; False if the job or slot changed first
        (then `finish` handles the slot as usual).
        """
        assert self.runner is not None
        requeued = retry_locked(
            lambda: self.pool.requeue_unstarted(
                self.placement.job_id,
                owner=self.owner,
                runner=self.runner,
                reason=reason,
                proof=self.proof,
            )
        )
        self.requeued = bool(requeued)
        return self.requeued

    def follow(
        self,
        command: Sequence[str],
        env: Mapping[str, str],
        brief: bytes,
        control: Control,
        *,
        report_env: bool = False,
        output: Callable[[IO[bytes]], None] | None = None,
        budget: Budget | None = None,
    ) -> int:
        """Launch the command detached on the device and follow it (#128).

        Its output is relayed to stdout (the waiter's run.log). Returns
        the device's exit status for it; 125 (124 when another run of the job
        is live there) for a refused launch, which ran nothing; 255 when the
        worker is gone without a status or the device went quiet for the
        grace window; for a stop, the status the device recorded.

        With report_env, the start writes the WORKER_ENV_MARK line (#93) to
        its private `env` file, which the follower relays ahead of the log.
        `output` (lib/telemetry.py) then takes the relayed output in place
        of stdout, as job_run.run's `output` does for a local command.

        With a `budget` (#199 r1), the launch carries what is left of it, so
        the device stops the run past it even when this runner is gone.
        """
        assert self.target is not None
        # Factory, Nov 13: the waiter config is shared by every device, so a
        # codex command runs by its bare name (the device's login PATH finds
        # it) and keeps each --add-dir only where the device has it.
        device = for_device(command)
        start = start_script(
            self.target,
            device.argv,
            env,
            guarded_dirs=device.guarded_dirs,
            insert_at=device.insert_at,
            cd=device.cd,
            report_env=report_env,
            env_file=run_file(self.target, ENV_FILE) if report_env else None,
        )
        limits = (
            {"budget": max(1, math.ceil(budget.remaining())), "grace": math.ceil(budget.grace)}
            if budget is not None
            else {}
        )
        launch = launch_script(
            self.target,
            start,
            size=len(brief),
            not_after=int(time.time()) + LAUNCH_TTL_SECONDS,
            **limits,
        )
        assert isinstance(self.transport, ByteTransport)
        sink: BinaryIO = sys.stdout.buffer
        reader: threading.Thread | None = None
        if output is not None:
            read_end, write_end = os.pipe()
            sink = os.fdopen(write_end, "wb")
            reader = threading.Thread(
                target=output, args=(os.fdopen(read_end, "rb"),), name="orch-output", daemon=True
            )
            reader.start()
        follower = Follower(
            self.transport,
            self.target,
            sink,
            log=self.log,
            grace=self.grace,
        )
        try:
            end = follower.run(launch, brief, control)
        finally:
            if reader is not None:
                try:
                    sink.close()  # the reader sees EOF once it has the rest
                except OSError:
                    pass
                reader.join(OUTPUT_GRACE_SECONDS)
        self.follow_end = end
        if end.kind in (LOST, WORKER_GONE):
            self.log(f"job {self.placement.job_id} on {self.device.name}: {end.detail}")
            return 255
        if end.kind == REFUSED:
            self.log(f"job {self.placement.job_id}: {self.device.name} refused the launch: {end.detail}")
        return end.code if end.code is not None else 255

    def before_start(self) -> None:
        """Record a remote start before ssh is spawned (a crash then still checks)."""
        if self.target is not None:
            target = self.target
            retry_locked(
                lambda: self.pool.attach_worker(
                    self.placement.job_id,
                    owner=self.owner,
                    remote=True,
                    tag=target.tag,
                    runner=self.runner,
                    proof=self.proof,
                )
            )
            self.spawned = True
            self.log(
                f"starting job {self.placement.job_id} on {self.device.name} "
                f"over ssh alias {self.target.alias} as {self.target.user}"
            )

    def started(self, pid: int) -> None:
        if self.target is not None:
            return  # the local pid is ssh; the worker is on the device
        start = process_start(pid)
        self._worker.append((pid, start))
        self.spawned = True
        retry_locked(
            lambda: self.pool.attach_worker(
                self.placement.job_id,
                owner=self.owner,
                pid=pid,
                start=start,
                run_tag=run_tag(pid),
                runner=self.runner,
                proof=self.proof,
            )
        )

    def open_gate(self, pid: int, go: Callable[[], None]) -> None:
        """Let the box command run only while this run still holds the job (#462)."""
        job_id = self.placement.job_id

        def check_then_go() -> None:
            with self.pool.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                require_proof(connection, job_id, self.proof)
                slot = connection.execute(
                    f"SELECT runner_pid, worker_pid FROM {SLOTS_TABLE} WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if slot is None or (slot["runner_pid"], slot["worker_pid"]) != (self.runner, pid):
                    raise QueueError(
                        f"job {job_id}'s slot no longer names runner {self.runner} "
                        f"and worker {pid} (released and claimed again)"
                    )
                go()
                connection.commit()

        retry_locked(check_then_go)

    def ended(self, code: int | None) -> None:
        """#91: record how the command ended while the job still runs.

        RUN_EXITED for status 0, RUN_DIED otherwise (with the status or
        signal), and LINK_DROP after it when the follow lost the device
        (#218). A job already done or failed records nothing. Never raises.
        """
        job_id = self.placement.job_id
        event = RUN_EXITED if code == 0 else RUN_DIED
        detail = f"orch job run's command on {self.device.name}: {exit_reason(code)}"
        end = self.follow_end
        minutes = f"{self.grace / 60:g} minutes"
        drop = (
            link_drop_detail(self.device.name, minutes, self.target.tag)
            if end is not None and end.kind == LOST and self.target is not None
            else None
        )
        if end is not None and end.kind == WORKER_GONE:
            detail = f"worker gone on {self.device.name} without an exit status"
        elif drop is not None:
            detail = (
                f"lost contact with {self.device.name} for {minutes}; "
                "worker not confirmed gone"
            )

        def write() -> None:
            with self.pool.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                require_proof(connection, job_id, self.proof)
                row = connection.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is not None and row["state"] == "running":
                    now = self.pool.queue.now()
                    record_job_event(connection, job_id, event, detail, now)
                    if drop is not None:
                        record_job_event(connection, job_id, LINK_DROP, drop, now)
                connection.commit()

        try:
            retry_locked(write)
        except (QueueError, sqlite3.Error, OSError) as exc:
            self.log(f"could not record how job {job_id}'s command ended: {exc}")

    def execed(self) -> None:
        """lib/job_run.py: the box command was exec'd, so the harness ran."""
        self.exec_seen = True

    def unstarted(self, code: int | None) -> bool:
        """#91: the harness never ran, by evidence, not by status (see the module doc).

        Remote, asks the device; call only once its worker is confirmed gone.
        """
        placement = self.placement
        if placement.worker_pid is not None or placement.remote_started:
            return False  # an earlier run's worker ran this job
        if not self.spawned:
            return True
        if self.target is None:
            return not self.exec_seen
        if code == 0:
            return False
        return harness_launched(self.transport, self.target) is False

    def live_worker(self) -> int | None:
        """A box worker of this job that still runs (its pid), or None."""
        workers = list(self._worker)
        if self.placement.worker_pid is not None:
            # a worker an earlier, dead runner of this job left behind
            workers.append((self.placement.worker_pid, self.placement.worker_start))
        for pid, start in workers:
            if self.alive(pid, start):
                return pid
        return None

    def finish(
        self, code: int | None = None, settle: Callable[[], None] | None = None
    ) -> bool:
        """Release the slot once the worker is confirmed gone. True if released.

        #91: with the command's exit status, a job whose harness never
        started is requeued (or failed, budget spent) in the same step.
        Unless the job was requeued, settle runs once: before the slot is
        deleted, since the slot holds the autostart claim and run token
        that keep other callers from finishing the job (#99 round 2), or
        while it stays held.
        """
        job_id = self.placement.job_id
        if self.requeued:
            return True  # the requeue freed the slot; nothing was started
        if self.target is None:
            pid = self.live_worker()
            if pid is not None:
                self.log(f"worker pid {pid} still runs; slot held")
                return _held(settle)
            return self._release(code, settle)
        end = self.follow_end
        fence = end is None or end.kind != REFUSED or end.in_transit
        status = check_worker(self.transport, self.target, fence=fence)
        cancelled = end is not None and end.kind == STOPPED
        checks = 0
        while status == RUNNING and checks < STOP_CHECKS:
            if checks == 0:
                self.log(f"job {job_id} worker still runs on {self.device.name}; stopping it")
                stop_worker(self.transport, self.target)
            elif cancelled and checks == KILL_CHECK:
                self.log(f"job {job_id} worker ignored SIGTERM on {self.device.name}; killing it")
                stop_worker(self.transport, self.target, kill=True)
            self.sleep(STOP_PAUSE_SECONDS)
            status = check_worker(self.transport, self.target, fence=fence)
            checks += 1
        if status == GONE:
            return self._release(code, settle)
        self.log(
            f"job {job_id} worker on {self.device.name} not confirmed gone ({status}); "
            "slot held until the dispatcher confirms it"
        )
        return _held(settle)

    def _release(self, code: int | None, settle: Callable[[], None] | None) -> bool:
        if self.unstarted(code) and self._requeue(code):
            return True
        _held(settle)
        released = retry_locked(
            lambda: self.pool.release(self.placement.job_id, owner=self.owner, proof=self.proof)
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
        if self.spawned:
            reason = (
                f"its command on {self.device.name} ended ({exit_reason(code)}) "
                "without reaching the harness"
            )
        else:
            reason = f"orch job run on {self.device.name} never started its command"

        def write() -> tuple[str, str] | None:
            with self.pool.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                require_proof(connection, job_id, self.proof)
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


def _held(settle: Callable[[], None] | None) -> bool:
    """Settle the job while its slot is still held. False: nothing released."""
    if settle is not None:
        settle()
    return False


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
    token_hash: str | None = None,
    proof: Proof | None = None,
) -> PlacedRun:
    """Record this process as the job's runner on its slot.

    token_hash (#96) is the hash of the token this run minted; it is
    recorded with the runner, so only this run can `orch job done` the job.

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
        target = remote_target(device, placement.harness, job_id, new_tag())
        if target is None:
            raise PlacementError(
                f"device {device.name} has no usable ssh_alias or workdir_root "
                f"for {placement.harness}"
            )
        mapped = device.harnesses.get(placement.harness)
        if mapped != bot:
            raise PlacementError(f"device {device.name} maps {owner} to {mapped}, not {bot}")
    runner = os.getpid() if pid is None else pid
    try:
        placement = retry_locked(
            lambda: pool.attach_runner(
                job_id,
                owner=owner,
                pid=runner,
                start=process_start(runner),
                alive=alive,
                token_hash=token_hash,
                proof=proof,
            )
        )
    except QueueError as exc:
        raise PlacementError(str(exc)) from exc
    # #70 F2.1: takeover is decided on the row attach_runner read while it
    # held the slot, never on the read above: a runner that recorded its
    # worker and died in between left that worker on this row.
    _earlier_worker_gone(placement, device, transport, alive)
    if placement.worker_pid is not None or placement.remote_started:
        try:
            retry_locked(
                lambda: pool.forget_worker(
                    job_id, owner=owner, runner=runner, earlier=placement, proof=proof
                )
            )
        except QueueError as exc:
            raise PlacementError(str(exc)) from exc
    return PlacedRun(
        pool=pool,
        placement=placement,
        device=device,
        owner=owner,
        log=log,
        transport=transport,
        target=target,
        autostart=placement.autostarted,
        proof=proof,
        alive=alive,
        runner=runner,
    )


def _branches(detail: str | None) -> tuple[str | None, str | None]:
    """The job's base branch and its own branch from its detail JSON, if named."""
    try:
        data = json.loads(detail) if detail else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return _plain_branch(data.get("base_branch")), _plain_branch(data.get("gitBranchName"))


def _plain_branch(value: object) -> str | None:
    """A branch name safe to pass to git, else None (an odd name is ignored)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return plain_branch(value) or None
    except RemoteError:
        return None


def _line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__


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
