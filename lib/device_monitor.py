"""Slot release, the lost check and the orphan sweep (docs/SPEC.md F2-02,
#662, #679).

The dispatcher runs `DeviceMonitor.reconcile` once per poll, before T6
looks at timeouts. First it calls `DeviceMonitor.reload`, which loads
machines.yml again on the first poll (the config the monitor was built
with was read before it could stamp the file) and then whenever it or its
machines.local.yml overlay has changed since the last good load. The load
is an adopt (lib/live_settings.py, #852): in one BEGIN IMMEDIATE it sets
the adopted switches of the shared settings row every claim places by to
the file, and the monitor acts on that same parse, so no claim lands on a
device this monitor still holds disabled. A file that fails to load raises
and the monitor keeps the last good config, so the next poll tries again. `reconcile` then walks each
device that holds a slot or an orphan. A remote device gets all three
steps below. On the box (a local device) a finished job's slot is
released as below; when machines.yml sets it `enabled: false`, each
running job whose worker started is handed to `on_lost` as below. A box
orphan names its whole process tree (lib/process_tree.py Tree): its `orch
job run`, its worker, every descendant of either, and on Linux every
process carrying the run's tag, which the slot recorded with the worker.
Each poll rebuilds the tree from the row alone, so a restarted monitor
finds it too: the tag, the recorded parents, every member an earlier poll
saw (pid and start time, kept on the row), and either every process in the
run's systemd scope (#817, a contained run) or, for an uncontained run,
every process in the runner's session while a member found another way is
in it too. On the poll that loses it, it is sent SIGTERM, through
the runner or through the worker once that
runner is gone; each later poll stops, then kills (SIGKILL), the whole
tree. Its row is forgotten only once no process of the run is left.

Release, only once the worker is confirmed gone. `orch job run` releases
its own slot when its command exits; this catches the rest, for a slot
whose job is no longer running (done, failed or timed out) and whose
`orch job run` process is gone:

* a slot no worker was ever started for (no runner, or a local runner
  that died before its command, or a remote start never attempted) is
  released;
* on the box, the slot is released once the run's tree (its tag, scope
  and recorded worker) has no live member. For a finished job whose
  runner is dead the tree is killed first (cgroup.kill, then the kill
  rounds), one round per poll. A running job's tree is never scanned or
  signalled;
* on a remote device, the device is asked over SSH, by the start's worker
  tag (lib/remote.py). The check first fences the tag's launch, so one
  still in transit from a runner since lost can never start after it.
  Gone releases the slot. A worker still running
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
job_states.unreachable_lost_minutes, 10 by default; `stranded`), or at once
when machines.yml sets it `enabled: false` (answering or not), each job
still running on it whose worker started is handed to `on_lost`: the
dispatcher fails it as lost, moves its slot to device_orphans and backs the
device off, and T6 requeues it under the link-drop budget.

Orphans. After the lost step, each device_orphans row on the device is
checked by its worker tag, so a job lost this poll is swept this poll. A
worker still running is sent SIGTERM on the poll that loses it, SIGKILL on
each later poll, and its row kept; a gone worker's row
is forgotten, as is a row whose worker no check can name. An unreachable
device ends its sweep for the poll after one failed check. A requeue
placed back on the same device carries a new tag and is never stopped by
mistake.

Sign-ins. After reconcile, `check_sign_ins` reads two clocks. The usage
clock (lib/usage_refresh.py `due`) is due once per `SLOT_SECONDS` slot of
the wall clock, by the start time the last pass stored, so a restart does
not rerun a pass. When it is due the whole usage refresh pass runs: the
account doctor (lib/account_doctor.py), then the usage reads. Otherwise the
doctor's clock (`account_doctor.due`) runs the doctor alone on the first
poll, at once on the poll after a new sign-in hold appears, every
`HELD_INTERVAL_SECONDS` while any hold is open, every `INTERVAL_SECONDS`
otherwise, and once on the poll after reload adopts a changed
machines.yml. After either, the sign-in wake (lib/signin_wake.py) queues
one Grok wake for each drop not yet announced, even when the pass raised.

Memory. After reconcile and before `check_sign_ins`, `check_memory` reads
/proc/meminfo and /proc/pressure/memory on each enabled device not
draining (#1226): on the orch host directly, on any other device in one SSH
call, all before it opens the database. A device over lib/device_memory.py's
limits is held `memory-wait` for `HOLD_SECONDS` from when the reads finish,
renewed each poll; any other reading clears the hold. A read that fails, or
a device with no /proc (macOS), fails open.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lib import account_doctor, live_settings, usage_refresh
from lib.device_memory import HOLD_SECONDS, READ_TIMEOUT_SECONDS, over_limit
from lib.device_pool import (
    DevicePool,
    HeldSlot,
    Orphan,
    Placement,
    clear_memory_hold,
    hold_memory,
    memory_held,
)
from lib.job_state import Liveness, liveness, never_started
from lib.machines_yml import Device, MachinesConfig
from lib.process_tree import (
    CGROUP_ROOT,
    Process,
    ProcessesUnknown,
    Tree,
    _responsible_pid,
    processes,
    run_tag,
)
from lib.remote import (
    GONE,
    RUNNING,
    UNREACHABLE,
    CaptureTransport,
    RemoteError,
    RemoteTarget,
    Transport,
    _sh,
    check_worker,
    probe_device,
    ssh_argv,
    stop_worker,
)
from lib.worktrees import pid_alive

Log = Callable[[str], None]
OnLost = Callable[[str, str, str], None]  # job_id, device, reason
Alive = Callable[[int | None, str | None], bool]
SignIns = Callable[[MachinesConfig, float, bool], object]
Reading = tuple[str | None, str | None]
MemoryRead = Callable[[Device, bool], Reading]

_MEMORY_FILES = ("meminfo", "pressure/memory")
_MEMORY_MARK = "#orch-memory"


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
            user=device.login_for(placement_harness),
            workdir_root=workdir,
            job_id=job_id,
            tag=tag,
        )
    except RemoteError:
        return None


def read_memory(
    transport: CaptureTransport, device: Device, on_host: bool, proc: Path = Path("/proc")
) -> Reading:
    """Device's /proc/meminfo and /proc/pressure/memory, each None when missing."""
    if on_host:
        return _read_text(proc / "meminfo"), _read_text(proc / "pressure/memory")
    target = next(
        filter(None, (remote_target(device, harness, "memory") for harness in device.harnesses)),
        None,
    )
    if target is None:
        raise RemoteError(f"no ssh target names {device.name}")
    script = "; ".join(
        f'echo "{_MEMORY_MARK} {name}"; cat /proc/{name} 2>/dev/null'
        f' || echo "{_MEMORY_MARK} missing"'
        for name in _MEMORY_FILES
    )
    result = transport.call(ssh_argv(target, _sh(script)), timeout=READ_TIMEOUT_SECONDS)
    if result.code != 0:
        last = (result.stderr.strip().splitlines() or [""])[-1]
        raise ConnectionError(f"exited {result.code}: {last}")
    texts: dict[str, str | None] = {}
    current = ""
    for line in result.stdout.splitlines(keepends=True):
        word = line[len(_MEMORY_MARK):].strip() if line.startswith(_MEMORY_MARK) else None
        if word in _MEMORY_FILES:
            current = word
            texts[current] = ""
        elif word == "missing":
            texts[current] = None
        elif (text := texts.get(current)) is not None:
            texts[current] = text + line
    return texts.get("meminfo"), texts.get("pressure/memory")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
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


def retry_stranded(device: Device, failing_since: float | None, now: float) -> str | None:
    if not device.enabled:
        return "disabled"
    return stranded(device, failing_since, now, limit=0.0)


class DeviceMonitor:
    def __init__(
        self,
        pool: DevicePool,
        config: MachinesConfig,
        transport: Transport,
        *,
        alive: Alive = pid_alive,
        kill: Callable[[int, int], None] = os.kill,
        scan: Callable[[str | None], dict[int, Process]] = processes,
        machines_path: Path | None = None,
        sign_ins: SignIns | None = None,
        memory: MemoryRead | None = None,
        cgroup_root: Path = CGROUP_ROOT,
    ):
        self.pool = pool
        self.config = config
        self.transport = transport
        self.alive = alive
        self.kill = kill
        self.scan = scan
        self.machines_path = machines_path
        self.sign_ins = sign_ins
        self.memory = memory
        self.cgroup_root = cgroup_root
        self._memory_held: set[str] = set()
        self._memory_notes: dict[str, str] = {}
        self._stamp: str | None = None
        self._signed: tuple[float, str | None] | None = None

    def reload(self) -> None:
        """Adopt machines.yml again if it or its overlay changed since the last
        good load: write it to the shared settings row and act on that same
        parse from now on (lib/live_settings.py, #852)."""
        path = self.machines_path
        if path is None:
            return
        stamp = live_settings.file_key(path)
        if stamp == self._stamp:
            return
        with self.pool.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _, config = live_settings.adopted(connection, path)
            connection.commit()
        self.config = self.pool.config = config
        self._stamp = stamp

    def check_sign_ins(self, now: float) -> bool:
        """Run the account doctor when due; an error waits for the next due time."""
        if self.sign_ins is None:
            return False
        last = self._signed[0] if self._signed is not None and self._signed[1] == self._stamp else None
        with contextlib.closing(self.pool.queue.connect()) as connection:
            newest = account_doctor.newest_hold(connection)
            usage = usage_refresh.due(connection, now)
        if not (usage or account_doctor.due(last, newest, now)):
            return False
        self._signed = (now, self._stamp)
        self.sign_ins(self.config, now, usage)
        return True

    def check_memory(self, log: Log) -> tuple[tuple[str, str], ...]:
        """Hold each over-limit device `memory-wait`, clear the rest; the held, in order."""
        if self.memory is None:
            return ()
        judged: list[tuple[str, str | None]] = []
        for device in self.config.devices:
            if not device.enabled or device.drain:
                continue
            try:
                meminfo, pressure = self.memory(device, device.name == self.config.orch_host)
            except Exception as exc:
                self._memory_note(device.name, f"{type(exc).__name__}: {exc}", log)
                judged.append((device.name, None))
                continue
            missing = [
                f"/proc/{name}"
                for name, text in zip(_MEMORY_FILES, (meminfo, pressure))
                if text is None
            ]
            problem = " and ".join(missing) + " missing" if missing else None
            self._memory_note(device.name, problem, log)
            judged.append((device.name, over_limit(meminfo, pressure)))
        if not judged:
            return ()
        now = self.pool.queue.now()
        with self.pool.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            was_held = {
                name
                for name, _ in judged
                if name in self._memory_held or memory_held(connection, name, now)
            }
            for name, reason in judged:
                if reason is None:
                    clear_memory_hold(connection, name, now)
                else:
                    hold_memory(connection, name, until=now + HOLD_SECONDS, reason=reason)
            connection.commit()
        held = tuple((name, reason) for name, reason in judged if reason is not None)
        for name, reason in judged:
            if reason is not None and name not in was_held:
                log(f"memory-wait on {name}: {reason}")
            elif reason is None and name in was_held:
                log(f"memory-wait off {name}")
        self._memory_held = {name for name, _ in held}
        return held

    def _memory_note(self, name: str, problem: str | None, log: Log) -> None:
        """Log a device's read problem once, until it changes or a good read ends it."""
        if problem is None:
            self._memory_notes.pop(name, None)
        elif self._memory_notes.get(name) != problem:
            self._memory_notes[name] = problem
            log(f"memory read {name}: {problem}")

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
                self._local(device, slots, now, on_lost, released, stopped, lost, forgotten)
                continue
            self._remote(device, slots, now, lost_after, on_lost, released, stopped, lost)
            self._sweep_orphans(device, now, stopped, forgotten)
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

    def _local(
        self,
        device: Device,
        slots: list[HeldSlot],
        now: float,
        on_lost: OnLost,
        released: list[str],
        stopped: list[str],
        lost: list[str],
        forgotten: list[str],
    ) -> None:
        for slot in slots:
            if self._local_slot_free(slot, stopped):
                self._release(slot, released)
            elif (
                not device.enabled
                and slot.job_state == "running"
                and not never_started(slot.placement)
            ):
                on_lost(slot.placement.job_id, device.name, "disabled")
                lost.append(slot.placement.job_id)
        for orphan in self.pool.orphans():
            if orphan.device == device.name:
                self._stop_local(orphan, now, stopped, forgotten)

    def _stop_local(
        self, orphan: Orphan, now: float, stopped: list[str], forgotten: list[str]
    ) -> None:
        try:
            tree = self._tree(orphan, orphan.members)
            members = tree.track() if tree is not None else {}
            if tree is not None and members:
                self.pool.remember_run(orphan.job_id, tree.tag, tree.known)
                if orphan.lost_at < now:
                    tree.kill_round()
                    self.pool.remember_run(orphan.job_id, tree.tag, tree.known)
        except ProcessesUnknown:
            return
        if tree is None or not members:
            self.pool.forget_orphan(orphan.job_id)
            forgotten.append(orphan.job_id)
            return
        if orphan.lost_at >= now:
            gentle = [pid for pid in (orphan.runner_pid, orphan.worker_pid) if pid in members]
            for pid in gentle[:1] or members:
                try:
                    self.kill(pid, signal.SIGTERM)
                except OSError:
                    continue
        stopped.append(orphan.job_id)

    def _tree(self, run: Orphan | Placement, members: Mapping[int, str]) -> Tree | None:
        """The run as its row names it: its tag, every member seen so far,
        each recorded parent still live, and its scope, else the runner's
        session; None when nothing names it."""
        listing = self.scan(run.run_tag)
        known = dict(members)
        for pid, start in (
            (run.runner_pid, run.runner_start),
            (run.worker_pid, run.worker_start),
        ):
            if start is not None and pid in listing and listing[pid].start == start:
                known[pid] = start
        tag = run.run_tag
        worker = run.worker_pid
        if tag is None and worker in known and known[worker] == run.worker_start:
            tag = run_tag(worker)
        root = worker if worker is not None else run.runner_pid
        proven_root = root in listing and known.get(root) == listing[root].start
        scope = run.run_scope
        session = None if scope else _runner_session(run.runner_pid, listing, known)
        if root is None or (tag is None and not known and session is None and scope is None):
            return None
        return Tree(
            root,
            tag,
            scan=self.scan,
            signal_pid=self.kill,
            responsible=_responsible_pid if sys.platform == "darwin" and proven_root else None,
            known=known,
            session=session,
            scope=scope,
            cgroup_root=self.cgroup_root,
        )

    def _local_slot_free(self, slot: HeldSlot, stopped: list[str]) -> bool:
        if slot.job_state == "running":
            return False
        placement = slot.placement
        if self.alive(placement.runner_pid, placement.runner_start):
            return False
        try:
            tree = self._tree(placement, {})
            if tree is not None and tree.track():
                tree.kill_round()
                stopped.append(placement.job_id)
                if tree.track():
                    return False
        except ProcessesUnknown:
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

    def _sweep_orphans(
        self, device: Device, now: float, stopped: list[str], forgotten: list[str]
    ) -> None:
        """Stop each lost job's worker still running on device; forget it once gone."""
        for orphan in self.pool.orphans():
            if orphan.device != device.name:
                continue
            target = remote_target(device, orphan.harness, orphan.job_id, orphan.worker_tag)
            if target is None or orphan.worker_tag is None:
                self.pool.forget_orphan(orphan.job_id)
                forgotten.append(orphan.job_id)
                continue
            status = check_worker(self.transport, target)
            if status == UNREACHABLE:
                return
            if status == GONE:
                self.pool.forget_orphan(orphan.job_id)
                forgotten.append(orphan.job_id)
            elif status == RUNNING and stop_worker(
                self.transport, target, kill=orphan.lost_at < now
            ):
                stopped.append(orphan.job_id)

    def _release(self, slot: HeldSlot, released: list[str]) -> None:
        if self.pool.release(slot.placement.job_id, owner=slot.placement.harness):
            released.append(slot.placement.job_id)


def _runner_session(
    pid: int | None, listing: dict[int, Process], known: dict[int, str]
) -> int | None:
    """The session the waiter started the runner in, which its pid names, while a
    proven process of the run is in it: one carrying the run's tag (the
    keeper, lib/job_run.py), or a known member with its recorded start time.

    A session number is not reused while a process is in it, but once the
    last proven one leaves, the number names nothing of the run (#827 f-5).
    """
    for process in listing.values():
        if process.group == pid and (process.tagged or known.get(process.pid) == process.start):
            return pid
    return None
