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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from lib.job_state import KILL_GRACE_SECONDS

# #199 r1: every process of a local run carries this tag in its environment.
RUN_TAG_ENV = "ORCH_RUN_TAG"

Log = Callable[[str], None]


class ProcessesUnknown(Exception):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    start: str
    tagged: bool


def processes(tag: str | None) -> dict[int, Process]:
    """This machine's live processes (zombies left out), and which carry `tag`.

    From /proc where there is one, else from `ps` (macOS: no tag is read,
    so only the process tree counts there). With no tag, none is tagged.
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
        found[int(name)] = Process(int(name), int(fields[1]), fields[19], tagged)
    return found


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
            ["ps", "-A", "-o", "pid=,ppid=,stat=,lstart="],
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
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        if parts[2].startswith("Z"):
            continue
        found[int(parts[0])] = Process(int(parts[0]), int(parts[1]), parts[3].strip(), False)
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
    belongs to it. `responsible` returns None for a process that is
    gone or not this user's, and raises ProcessesUnknown when it cannot say.
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
        grown = True
        while grown:
            grown = False
            for process in listing.values():
                if process.pid in members or process.pid == me or process.pid in self.foreign:
                    continue
                if (
                    process.tagged
                    or process.ppid in members
                    or self._adopted(process, me)
                    or self._disowned(process)
                ):
                    members[process.pid] = process
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
