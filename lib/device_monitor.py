"""Slot release and the lost check (docs/SPEC.md F2-02, #662).

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

Lost. Each remote device still holding a slot is probed every poll:
the worker checks above count, and a device that needed none (its jobs
run, or their runners still live) gets one plain SSH round trip. The
first failed probe after a good one starts the device's offline clock; a
good probe stops it, and a device that holds no slot is forgotten. Once a
device has failed every probe for `lost_after` seconds (orch.yml
job_states.unreachable_lost_minutes, 10 by default; `stranded`), each job
still running on it whose worker started is handed to `on_lost`: the
dispatcher fails it as lost, moves its slot to device_orphans and backs the
device off, and T6 requeues it under the link-drop budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lib.config_overlay import overlay_path
from lib.device_pool import DevicePool, HeldSlot, Placement
from lib.job_state import Liveness, liveness, never_started
from lib.machines_yml import Device, MachinesConfig, load_machines_yml
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

Log = Callable[[str], None]
OnLost = Callable[[str, str, str], None]  # job_id, device, reason
Alive = Callable[[int | None, str | None], bool]
Stamp = tuple[int, int | None]  # st_mtime_ns of machines.yml and of its overlay


@dataclass(frozen=True)
class MonitorResult:
    released: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()
    forgotten: tuple[str, ...] = ()


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


def stranded(
    device: Device, failing_since: float | None, now: float, limit: float
) -> str | None:
    """Why device's running jobs are lost now, or None while they may still run."""
    if not device.enabled:
        return "disabled"
    if failing_since is None or now - failing_since < limit:
        return None
    return f"unreachable {int((now - failing_since) // 60)} min"


def _stamp(path: Path) -> Stamp:
    try:
        overlay: int | None = overlay_path(path).stat().st_mtime_ns
    except FileNotFoundError:
        overlay = None
    return path.stat().st_mtime_ns, overlay


class DeviceMonitor:
    def __init__(
        self,
        pool: DevicePool,
        config: MachinesConfig,
        transport: Transport,
        *,
        alive: Alive = pid_alive,
        machines_path: Path | None = None,
    ):
        self.pool = pool
        self.config = config
        self.transport = transport
        self.alive = alive
        self.machines_path = machines_path
        self._stamp: Stamp | None = None
        if machines_path is not None:
            try:
                self._stamp = _stamp(machines_path)
            except OSError:
                pass

    def reload(self) -> None:
        """Load machines.yml again if it or its overlay changed since the last good load.

        A file that fails to load raises ValueError and keeps the old config
        and stamp, so every poll retries and reports it until it is fixed.
        """
        path = self.machines_path
        if path is None:
            return
        try:
            stamp = _stamp(path)
            if stamp == self._stamp:
                return
            config, findings = load_machines_yml(path, allow_placeholders=True)
        except OSError as exc:
            raise ValueError(f"{path}: cannot read: {type(exc).__name__}") from exc
        errors = [item for item in findings if item.severity == "error"]
        if config is None or errors:
            detail = errors[0].message if errors else "invalid machines.yml"
            raise ValueError(f"{path}: {detail}")
        self.config = self.pool.config = config
        self._stamp = stamp

    def reconcile(self, now: float, lost_after: float, on_lost: OnLost) -> MonitorResult:
        released: list[str] = []
        stopped: list[str] = []
        lost: list[str] = []
        forgotten: list[str] = []
        groups: dict[str, list[HeldSlot]] = {}
        for slot in self.pool.held():
            groups.setdefault(slot.placement.device, []).append(slot)
        for orphan in self.pool.orphans():
            groups.setdefault(orphan.device, [])
        for name, slots in groups.items():
            device = self.config.device(name)
            if device is None:
                continue  # renamed or removed: keep the slot, status shows it
            if device.local:
                for slot in slots:
                    if self._local_gone(slot):
                        self._release(slot, released)
                continue
            self._remote(device, slots, now, lost_after, on_lost, released, stopped, lost)
            self._sweep_orphans(device, stopped, forgotten)
        self.pool.forget_health(keep=self.pool.used().keys())
        return MonitorResult(tuple(released), tuple(stopped), tuple(lost), tuple(forgotten))

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
        lost_after: float,
        on_lost: OnLost,
        released: list[str],
        stopped: list[str],
        lost: list[str],
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
        failing_since = None
        if answered is not None:
            failing_since = self.pool.record_contact(device.name, ok=answered, at=now)
        reason = stranded(device, failing_since, now, lost_after)
        if reason is None:
            return
        for slot in slots:
            if slot.job_state == "running" and not never_started(slot.placement):
                on_lost(slot.placement.job_id, device.name, reason)
                lost.append(slot.placement.job_id)

    def _sweep_orphans(self, device: Device, stopped: list[str], forgotten: list[str]) -> None:
        """Stop each lost job's worker still running on device; forget it once gone.

        Read after the lost step, so a job lost this poll is swept this poll.
        """
        for orphan in self.pool.orphans():
            if orphan.device != device.name:
                continue
            target = remote_target(device, orphan.harness, orphan.job_id, orphan.worker_tag)
            if target is None or orphan.worker_tag is None:
                status = GONE  # no check can ever name its worker, and it holds no slot
            else:
                status = check_worker(self.transport, target)
            if status == UNREACHABLE:
                return  # one failed SSH call per down device per poll
            if status == GONE:
                self.pool.forget_orphan(orphan.job_id)
                forgotten.append(orphan.job_id)
            elif status == RUNNING and stop_worker(self.transport, target):
                stopped.append(orphan.job_id)

    def _release(self, slot: HeldSlot, released: list[str]) -> None:
        if self.pool.release(slot.placement.job_id, owner=slot.placement.harness):
            released.append(slot.placement.job_id)
