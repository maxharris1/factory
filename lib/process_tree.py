"""This machine's processes, and every process of one local run (#199 r1).

lib/job_run.py runs a command and kills its whole tree once it exits;
lib/device_monitor.py stops the tree of a box job lost with its runner.
The dispatcher must not import lib/job_run.py, so the process code both
use lives here.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lib.job_state import KILL_GRACE_SECONDS

# #199 r1: every process of a local run carries this tag in its environment.
RUN_TAG_ENV = "ORCH_RUN_TAG"
CONTAIN_SECONDS = 2.0
CGROUP_ROOT = Path("/sys/fs/cgroup")

Log = Callable[[str], None]


class ProcessesUnknown(Exception):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    start: str
    tagged: bool
    group: int | None = None
    scope: str | None = None
    cgroup: str | None = None


def processes(tag: str | None) -> dict[int, Process]:
    """This machine's live processes (zombies left out), and which carry `tag`.

    From /proc where there is one, else from `ps` (macOS: no tag is read,
    so only the process tree counts there). With no tag, none is tagged.
    Each process's `group` is its session on Linux, its process group on
    macOS (whose `ps` cannot name a session). Its `cgroup` (Linux) is its
    cgroup v2 path, and its `scope` that path's last component.
    """
    if os.path.isdir("/proc/self"):
        return _proc_processes(tag)
    return _ps_processes()


def _proc_processes(tag: str | None) -> dict[int, Process]:
    mark = None if tag is None else f"{RUN_TAG_ENV}={tag}".encode()
    try:
        names = os.listdir("/proc")
    except OSError as exc:
        raise ProcessesUnknown(f"cannot list /proc: {_one_line(exc)}") from exc
    found: dict[int, Process] = {}
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                stat = handle.read().decode(errors="replace")
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) < 20 or fields[0] in ("Z", "X"):
            continue
        tagged = False
        if mark is not None:
            try:
                with open(f"/proc/{name}/environ", "rb") as handle:
                    tagged = mark in handle.read().split(b"\0")
            except OSError:
                pass
        cgroup = cgroup_of(int(name))
        found[int(name)] = Process(
            int(name), int(fields[1]), fields[19], tagged, int(fields[3]), _last(cgroup), cgroup
        )
    return found


def cgroup_of(pid: int) -> str | None:
    """pid's cgroup v2 path (its `0::` line), None when it cannot be read."""
    try:
        with open(f"/proc/{pid}/cgroup", "rb") as handle:
            lines = handle.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            return line[3:].rstrip("/") or None
    return None


def scope_of(pid: int) -> str | None:
    """The last component of pid's cgroup v2 path, None when it cannot be read."""
    return _last(cgroup_of(pid))


def _last(cgroup: str | None) -> str | None:
    return None if cgroup is None else cgroup.rsplit("/", 1)[-1] or None


def cgroup_kill(unit: str, members: Iterable[Process], root: Path = CGROUP_ROOT) -> None:
    """Write 1 to the cgroup.kill of scope `unit`, found from a member's cgroup path.

    The kernel then kills every process in the scope at once. Nothing is
    written when no member is in the scope, the file is absent (a kernel
    before 5.14) or unreachable, or the write fails.
    """
    for process in members:
        if process.scope != unit or process.cgroup is None:
            continue
        kill = root / process.cgroup.lstrip("/") / "cgroup.kill"
        try:
            if kill.exists():
                kill.write_text("1")
        except OSError:
            pass
        return


def scope_unit(tag: str) -> str:
    return f"orch-run-{tag}.scope"


def scope_prefix(tag: str, env: Mapping[str, str], log: Log) -> list[str]:
    """The systemd-run words that exec a run in a new user scope named for it.

    Only on Linux, only while the user has Linger on, so the user manager
    outlives every login, and only once a probe scope with the same options
    starts; otherwise no words, and one log line says why. systemd-run names
    its own process to the user manager, so a run inside a PID namespace is
    contained too (#1140). systemd-run 254+ expands `${VAR}` in a scope's
    argv unless given `--expand-environment=no`, a flag older releases lack.
    """
    if not sys.platform.startswith("linux"):
        return []
    try:
        linger = _output(
            ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"], env
        )
        if linger.strip() != "yes":
            raise _Uncontained(f"Linger is {linger.strip() or 'unset'} for this user")
        try:
            noexpand = "--expand-environment" in _output(["systemd-run", "--help"], env)
        except _Uncontained:
            noexpand = True
        scope = [
            "systemd-run", "--user", "--scope", "--quiet", "--collect",
            *(["--expand-environment=no"] if noexpand else []),
        ]
        _output([*scope, "true"], env)
    except (_Uncontained, OSError, subprocess.SubprocessError) as exc:
        log(f"the run is uncontained (no systemd scope): {_one_line(exc)}")
        return []
    return [*scope, f"--unit={scope_unit(tag)}", "--"]


def entered_scope(pid: int, tag: str, log: Log) -> str | None:
    """The run's scope unit if pid, started behind scope_prefix, is in it.

    systemd-run moves itself into the scope before it execs the run, so a
    started run is already there; else None and the uncontained line.
    """
    unit = scope_unit(tag)
    if scope_of(pid) == unit:
        return unit
    log(f"the run is uncontained (no systemd scope): pid {pid} did not enter {unit}")
    return None


class _Uncontained(Exception):
    pass


def _output(argv: list[str], env: Mapping[str, str]) -> str:
    done = subprocess.run(
        argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CONTAIN_SECONDS,
        env=env,
    )
    if done.returncode != 0:
        detail = " ".join(done.stderr.split())
        raise _Uncontained(f"{argv[0]} exited {done.returncode}: {detail or 'no detail'}")
    return done.stdout


def run_tag(pid: int) -> str | None:
    """The RUN_TAG_ENV value pid carries, None when it has none or it cannot be read."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            environ = handle.read().split(b"\0")
    except OSError:
        return None
    prefix = f"{RUN_TAG_ENV}=".encode()
    for entry in environ:
        if entry.startswith(prefix):
            return entry[len(prefix):].decode(errors="replace")
    return None


def _ps_processes() -> dict[int, Process]:
    try:
        ps = subprocess.Popen(
            ["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,lstart="],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessesUnknown(f"cannot run ps: {_one_line(exc)}") from exc
    try:
        stdout, stderr = ps.communicate(timeout=30)
    except subprocess.TimeoutExpired as exc:
        ps.kill()
        ps.communicate()
        raise ProcessesUnknown(f"cannot run ps: {_one_line(exc)}") from exc
    if ps.returncode != 0:
        raise ProcessesUnknown(f"ps exited {ps.returncode}: {' '.join(stderr.split())}")
    found: dict[int, Process] = {}
    for line in stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or not all(part.isdigit() for part in parts[:3]):
            continue
        if parts[3].startswith("Z"):
            continue
        found[int(parts[0])] = Process(
            int(parts[0]), int(parts[1]), parts[4].strip(), False, int(parts[2])
        )
    found.pop(ps.pid, None)
    return found


class Tree:
    """Every process of one local run (#199 r1): its tag, and its process tree.

    A process belongs to the run when it carries the run's tag, or its parent
    belongs to it; once seen it stays a member (by pid and start time) after
    its parent exits. `others` (pid and start time) names every process live
    before the run, which is never adopted or disowned into it. With `adopts` (run
    is the subreaper), a child of this process belongs to it too. With
    `responsible` (macOS), an orphan whose responsible process is the root
    belongs to it: the root is the run's anchor (#846, lib/job_run.py), which
    outlives every process that names it until the kill round. `responsible` returns None for a process that is
    gone or not this user's, and raises ProcessesUnknown when it cannot say.
    With `scope`, every process in that systemd scope belongs to it, and each
    kill round first writes the scope's cgroup.kill under `cgroup_root` (#1210).
    With `session`, a process whose `group` it is belongs to it while a member
    is in that session: a session number is reused once its last process
    exits, so it names the run only alongside a member proven otherwise.
    The root is taken by pid only on the first look; `known` (pid and start
    time) seeds the members instead, for a run this process did not start.
    After that a pid counts only by its start time, so a reused pid never
    joins the run.
    """

    def __init__(
        self,
        root: int,
        tag: str | None,
        *,
        scan: Callable[[str | None], dict[int, Process]] = processes,
        signal_pid: Callable[[int, int], None] = os.kill,
        adopts: bool = False,
        others: frozenset[tuple[int, str]] = frozenset(),
        responsible: Callable[[int], int | None] | None = None,
        known: Mapping[int, str] | None = None,
        session: int | None = None,
        scope: str | None = None,
        cgroup_root: Path = CGROUP_ROOT,
    ):
        self.root = root
        self.responsible = responsible
        self.tag = tag
        self.adopts = adopts
        self.others = others
        self.scan = scan
        self.signal_pid = signal_pid
        self.known: dict[int, str] = dict(known or {})
        self.looked = known is not None
        self.foreign: set[int] = set()
        self.session = session
        self.scope = scope
        self.cgroup_root = cgroup_root

    def track(self) -> dict[int, Process]:
        """Look again: the run's live processes, the root's own process included."""
        listing = self.scan(self.tag)
        me = os.getpid()
        members = {
            pid: listing[pid]
            for pid, start in self.known.items()
            if pid in listing and listing[pid].start == start and pid not in self.foreign
        }
        if not self.looked and self.root in listing:
            members[self.root] = listing[self.root]
        sessions = {process.group for process in members.values()}
        grown = True
        while grown:
            grown = False
            for process in listing.values():
                if process.pid in members or process.pid == me or process.pid in self.foreign:
                    continue
                if (
                    process.tagged
                    or (self.scope is not None and process.scope == self.scope)
                    or (self.session is not None and process.group == self.session
                        and self.session in sessions)
                    or process.ppid in members
                    or self._adopted(process, me)
                    or self._disowned(process)
                ):
                    members[process.pid] = process
                    sessions.add(process.group)
                    grown = True
        self.known = {pid: process.start for pid, process in members.items()}
        self.looked = True
        return members

    def track_if_listable(self) -> None:
        try:
            self.track()
        except ProcessesUnknown:
            pass

    def _adopted(self, process: Process, me: int) -> bool:
        return (
            self.adopts and process.ppid == me and (process.pid, process.start) not in self.others
        )

    def _disowned(self, process: Process) -> bool:
        return (
            self.responsible is not None
            and process.ppid == 1
            and (process.pid, process.start) not in self.others
            and self.responsible(process.pid) == self.root
        )

    def end(self, log: Log) -> None:
        """Kill every live process of the run; return once none is left.

        Each round stops every member (SIGSTOP: none can fork), looks again
        until no new member shows up, then kills them all (SIGKILL). A
        process this user may not signal is not the run's.
        """
        said: float | None = None
        while True:
            try:
                members = self.kill_round()
            except ProcessesUnknown as exc:
                if said is None or time.monotonic() - said >= KILL_GRACE_SECONDS:
                    log(f"{exc}; holding the job until the run's processes are confirmed gone")
                    said = time.monotonic()
                time.sleep(0.5)
                continue
            if not members:
                return
            if said is None:
                said = time.monotonic()
            elif time.monotonic() - said >= KILL_GRACE_SECONDS:
                log(f"processes {sorted(members)} of the run still live after SIGKILL; waiting")
                said = time.monotonic()
            time.sleep(0.05)

    def kill_round(self) -> dict[int, Process]:
        """Stop every member, look again until none is new, kill them all; the members."""
        members = self.track()
        if self.scope is not None:
            cgroup_kill(self.scope, members.values(), self.cgroup_root)
        stopped: set[int] = set()
        while set(members) - stopped:
            for pid in set(members) - stopped:
                self._send(pid, signal.SIGSTOP)
                stopped.add(pid)
            members = self.track()
        for pid in members:
            self._send(pid, signal.SIGKILL)
        self._reap(members)
        return members

    def _reap(self, members: dict[int, Process]) -> None:
        """Collect the killed processes this process adopted, so none is left a zombie."""
        me = os.getpid()
        for process in members.values():
            if process.pid == self.root or not self._adopted(process, me):
                continue
            try:
                os.waitpid(process.pid, 0)
            except ChildProcessError:
                pass

    def _send(self, pid: int, signum: int) -> None:
        try:
            self.signal_pid(pid, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            self.foreign.add(pid)
            self.known.pop(pid, None)


def _responsible_pid(pid: int) -> int | None:
    try:
        call = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        responsible_for = call.responsibility_get_pid_responsible_for_pid
    except (OSError, AttributeError) as exc:
        raise ProcessesUnknown(f"cannot read responsible processes: {_one_line(exc)}") from exc
    ctypes.set_errno(0)
    found = responsible_for(pid)
    if found > 0:
        return found
    err = ctypes.get_errno()
    if err in (errno.ESRCH, errno.EPERM):
        return None
    raise ProcessesUnknown(
        f"cannot read the responsible process of pid {pid}: {os.strerror(err) if err else found}"
    )


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__
