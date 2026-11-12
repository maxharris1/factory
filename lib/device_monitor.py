"""Slot release and the 60-minute offline check (docs/SPEC.md F2-02).

The dispatcher runs `DeviceMonitor.reconcile` once per poll, before T6
looks at timeouts. It does two things with the device pool's held slots.

Release, only once the worker is confirmed gone. `orch job run` releases
its own slot when its command exits; this catches the rest, for a slot
whose job is no longer running (done, failed or timed out) and whose
`orch job run` process is gone:

* a slot no worker was ever started for (no runner, or a local runner
  that died before its command, or a remote start never attempted) is
  released;
* on the box, the slot is released once the recorded command pid is gone;
* on a remote device, the device is asked over SSH, by the start's worker
  tag (lib/remote.py). Gone releases the slot. A worker still running
  there with no runner on the box is an orphan: it is sent SIGTERM and the
  slot is kept until a later poll finds it gone. An unreachable device, a
  check that cannot tell, or a remote start with no recorded tag keeps
  the slot.

Offline. Each remote device still holding a slot is probed every poll:
the worker checks above count, and a device that needed none (its jobs
run, or their runners still live) gets one plain SSH round trip. The
first failed probe after a good one starts the device's offline clock; a
good probe stops it, and a device that holds no slot is forgotten. Once a device has failed every probe for
OFFLINE_SECONDS (60 minutes), each job still running on it is failed and
its ticket marked stuck, which wakes Grok Bot once (the ticket's stuck
flag). Its slot stays held: the worker is not confirmed gone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lib.device_pool import DevicePool, HeldSlot, Placement
from lib.job_state import Liveness, liveness
from lib.machines_yml import Device, MachinesConfig
from lib.remote import (
    GONE,
    RUNNING,
    UNREACHABLE,
    RemoteError,
    RemoteTarget,
    Transport,
    check_worker,
    probe_device,
    stop_worker,
)
from lib.worktrees import pid_alive

OFFLINE_SECONDS = 3600.0

Log = Callable[[str], None]
OnOffline = Callable[[str, str, float], None]  # job_id, device, offline seconds
Alive = Callable[[int | None, str | None], bool]


@dataclass(frozen=True)
class MonitorResult:
    released: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    offline: tuple[str, ...] = ()


def remote_target(
    device: Device,
    placement_harness: str,
    job_id: str,
    tag: str | None = None,
) -> RemoteTarget | None:
    """The job's SSH target on a remote device, or None if config cannot name one."""
    if device.local:
        return None
    alias = device.ssh_alias_for(placement_harness)
    workdir = device.workdir_for(placement_harness)
    if alias is None or workdir is None:
        return None
    try:
        return RemoteTarget(
            device=device.name,
            alias=alias,
            user=device.harnesses[placement_harness],
            workdir_root=workdir,
            job_id=job_id,
            tag=tag,
        )
    except RemoteError:
        return None


class DeviceMonitor:
    def __init__(
        self,
        pool: DevicePool,
        config: MachinesConfig,
        transport: Transport,
        *,
        alive: Alive = pid_alive,
        offline_seconds: float = OFFLINE_SECONDS,
    ):
        self.pool = pool
        self.config = config
        self.transport = transport
        self.alive = alive
        self.offline_seconds = offline_seconds

    def reconcile(self, now: float, on_offline: OnOffline) -> MonitorResult:
        released: list[str] = []
        stopped: list[str] = []
        offline: list[str] = []
        groups: dict[str, list[HeldSlot]] = {}
        for slot in self.pool.held():
            groups.setdefault(slot.placement.device, []).append(slot)
        for name, slots in groups.items():
            device = self.config.device(name)
            if device is None:
                continue  # renamed or removed: keep the slot, status shows it
            if device.local:
                for slot in slots:
                    if self._local_gone(slot):
                        self._release(slot, released)
                continue
            self._remote(device, slots, now, on_offline, released, stopped, offline)
        self.pool.forget_health(keep=self.pool.used().keys())
        return MonitorResult(tuple(released), tuple(stopped), tuple(offline))

    def liveness(self, placement: Placement) -> Liveness:
        """Is the job's recorded runner gone, and then its worker? (job_state)"""
        return liveness(placement, self.alive, self._remote_gone)

    def _remote_gone(self, placement: Placement) -> bool | None:
        device = self.config.device(placement.device)
        if device is None or device.local or placement.worker_tag is None:
            return None
        target = remote_target(
            device, placement.harness, placement.job_id, placement.worker_tag
        )
        if target is None:
            return None
        status = check_worker(self.transport, target)
        if status == GONE:
            return True
        if status == RUNNING:
            return False
        return None

    def _local_gone(self, slot: HeldSlot) -> bool:
        if slot.job_state == "running":
            return False
        placement = slot.placement
        if self.alive(placement.runner_pid, placement.runner_start):
            return False
        return not self.alive(placement.worker_pid, placement.worker_start)

    def _remote(
        self,
        device: Device,
        slots: list[HeldSlot],
        now: float,
        on_offline: OnOffline,
        released: list[str],
        stopped: list[str],
        offline: list[str],
    ) -> None:
        answered: bool | None = None
        probe: RemoteTarget | None = None
        held = 0
        for slot in slots:
            placement = slot.placement
            target = remote_target(
                device, placement.harness, placement.job_id, placement.worker_tag
            )
            finished = slot.job_state != "running"
            if finished and not placement.remote_started and not self.alive(
                placement.runner_pid, placement.runner_start
            ):
                self._release(slot, released)  # no worker was ever started
                continue
            held += 1
            if probe is None and target is not None:
                probe = target
            if not finished or self.alive(placement.runner_pid, placement.runner_start):
                continue  # running, or its runner releases the slot when it ends
            if target is None or placement.worker_tag is None:
                continue  # the worker cannot be named: keep the slot
            status = check_worker(self.transport, target)
            reached = status != UNREACHABLE
            answered = reached if answered is None else (answered or reached)
            if status == GONE:
                self._release(slot, released)
                held -= 1
            elif status == RUNNING and stop_worker(self.transport, target):
                stopped.append(placement.job_id)
        if answered is None and held and probe is not None:
            answered = probe_device(self.transport, probe)
        if answered is None:
            return
        failing_since = self.pool.record_contact(device.name, ok=answered, at=now)
        if failing_since is None or now - failing_since < self.offline_seconds:
            return
        for slot in slots:
            if slot.job_state == "running":
                on_offline(slot.placement.job_id, device.name, now - failing_since)
                offline.append(slot.placement.job_id)

    def _release(self, slot: HeldSlot, released: list[str]) -> None:
        if self.pool.release(slot.placement.job_id, owner=slot.placement.harness):
            released.append(slot.placement.job_id)
