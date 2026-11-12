"""`orch job run` for a job the device pool placed (docs/SPEC.md F2-02).

`orch wait` claims through the device pool, so every harness job it hands
out holds one slot on one device. `orch job run` then:

* refuses (exit 2, nothing started, slot kept) a job that holds no slot,
  whose slot is for another bot user, whose device is no longer in
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
  dispatcher's DeviceMonitor confirms it on a later poll.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from lib.device_monitor import remote_target
from lib.device_pool import DevicePool, Placement
from lib.machines_yml import Device, MachinesConfig
from lib.queue import QueueError
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
    sleep: Callable[[float], None] = time.sleep
    alive: Callable[[int | None, str | None], bool] = pid_alive
    _worker: list[tuple[int, str | None]] = field(default_factory=list)

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
            self.log(
                f"starting job {self.placement.job_id} on {self.device.name} "
                f"over ssh alias {self.target.alias} as {self.target.user}"
            )

    def started(self, pid: int) -> None:
        if self.target is not None:
            return  # the local pid is ssh; the worker is on the device
        start = process_start(pid)
        self._worker.append((pid, start))
        retry_locked(
            lambda: self.pool.attach_worker(
                self.placement.job_id,
                owner=self.owner,
                pid=pid,
                start=start,
            )
        )

    def finish(self) -> bool:
        """Release the slot once the worker is confirmed gone. True if released."""
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
            return self._release()
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
            return self._release()
        self.log(
            f"job {job_id} worker on {self.device.name} not confirmed gone ({status}); "
            "slot held until the dispatcher confirms it"
        )
        return False

    def _release(self) -> bool:
        released = retry_locked(
            lambda: self.pool.release(self.placement.job_id, owner=self.owner)
        )
        if released:
            self.log(f"released {self.device.name} slot of job {self.placement.job_id}")
        return released


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
) -> PlacedRun:
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
        alive=alive,
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
