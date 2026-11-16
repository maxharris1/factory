"""`orch job run`: run a job's command under an orch-owned heartbeat (#49).

Bots used to hand-roll `while true; do orch job heartbeat ...; sleep; done`
loops, and one outlived its job by hours. Here the heartbeat lives in the
same process that runs the command:

* the first heartbeat is sent before the command starts, so a job that is
  not running (or not this bot's) is refused and nothing runs;
* it then beats every `interval` seconds on a daemon thread (by default
  every 15 seconds, or a third of the job's timeout if that is shorter; an
  --interval override is capped at 30 seconds and at a third of the
  timeout, so a live job never looks stale: lib/job_state.py);
* it stops for good on the first QueueError (the job was finished, failed or
  timed out, e.g. by the command's own `orch job done`), and retries
  transient storage errors at the next interval;
* it stops when the command exits, and dies with this process, so no loop
  can outlive the job;
* with a `Budget` (#199: the job's timeout_seconds, printed in the brief),
  a command still running when the wall clock runs out is stopped: SIGTERM,
  then SIGKILL after KILL_GRACE_SECONDS, and run returns only once the
  child is reaped, so its death is confirmed before the job can fail and
  T6 can retry it. A remote run's Control is stopped instead, and
  lib/placed_run.py confirms its worker gone on the device; a run past its
  budget returns a failing status even when its command exited 0;
* every process the command started dies with it (#199 r1): the command
  runs with RUN_TAG_ENV set to a fresh tag, run tracks its process tree
  while it runs, and once it has exited run stops (SIGSTOP), then kills
  (SIGKILL), every live process that carries the tag or descends from a
  tracked one, and returns only when none is left;
* death counts only once a look at this machine's processes succeeds and
  finds none of the run left (#199 r4): while `ps` or /proc cannot be read,
  run keeps beating and holds the job and its slot, so T6 cannot retry it;
* on Linux, run is the command's child subreaper while it runs (#199 r2),
  so a descendant orphaned between two looks, even one that dropped the tag
  and left the session, is reparented to run rather than to init, and is
  killed with the rest;
* on macOS the command runs as its own responsible process (#329), which
  every descendant inherits, so a descendant reparented to launchd before
  any look saw its parent is killed with the rest.

`run` never finishes the job: `orch job done` stays the bot's call.
"""

from __future__ import annotations

import ctypes
import errno
import math
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import IO

from lib.job_state import HEARTBEAT_SECONDS, KILL_GRACE_SECONDS, MAX_HEARTBEAT_SECONDS
from lib.queue import QueueError

FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# How long run waits, once the command has exited, for `output` to drain the
# rest of its stdout (a process the command left behind may hold the pipe).
OUTPUT_GRACE_SECONDS = 5.0
TRANSIENT_ERRORS = (sqlite3.Error, OSError)
# #199: how long a command past its budget has between SIGTERM and SIGKILL.
WALL_CLOCK = "wall-clock"
# #199 r1: every process of a local run carries this tag in its environment.
RUN_TAG_ENV = "ORCH_RUN_TAG"
# How often run looks at the command's process tree while it runs.
TRACK_SECONDS = 1.0
# prctl(2) options for a child subreaper (Linux).
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37

Beat = Callable[[], object]
Log = Callable[[str], None]

# With `started`, the command is spawned behind this gate: it waits for
# "go" on the pipe whose read end is fd argv[1], then execs the command
# (same pid). "go" is written only after `started` has recorded the pid, so
# a runner that dies, or cannot record it, closes the pipe first and the
# command never runs. Before the exec it puts back the signal dispositions
# Python changed at its own start-up, as Popen does for a direct command.
# argv[2] is a close-on-exec report pipe: EOF with no byte means the exec
# happened (#99 A2), a byte means the command could not be exec'd.
# On macOS the gate execs the command disclaimed (#329): the command becomes
# its own responsible process, an identity each descendant inherits and
# cannot shed.
_GATE = """\
import os, signal, sys
if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
fd, report = int(sys.argv[1]), int(sys.argv[2])
go = os.read(fd, 3) == b"go\\n"
os.close(fd)
if not go:
    sys.exit(125)
for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
os.set_inheritable(report, False)

def disclaimed_exec(argv):
    import ctypes
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    def check(err, what):
        if err:
            raise OSError(err, f"{what}: {os.strerror(err)}")
    attr = ctypes.c_void_p()
    check(libc.posix_spawnattr_init(ctypes.byref(attr)), "posix_spawnattr_init")
    POSIX_SPAWN_SETEXEC = 0x40
    check(libc.posix_spawnattr_setflags(ctypes.byref(attr), ctypes.c_short(POSIX_SPAWN_SETEXEC)), "setexec")
    check(libc.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), 1), "disclaim")
    args = (ctypes.c_char_p * (len(argv) + 1))(*map(os.fsencode, argv), None)
    env = [key + b"=" + value for key, value in os.environb.items()]
    envp = (ctypes.c_char_p * (len(env) + 1))(*env, None)
    err = libc.posix_spawnp(None, args[0], None, ctypes.byref(attr), args, envp)
    raise OSError(err, f"posix_spawnp: {os.strerror(err)}")

try:
    if sys.platform == "darwin":
        disclaimed_exec(sys.argv[3:])
    os.execvp(sys.argv[3], sys.argv[3:])
except (OSError, AttributeError) as exc:
    os.write(report, b"x")
    print(f"orch job run: cannot start {sys.argv[3]}: {exc}", file=sys.stderr)
    sys.exit(127)
"""


def heartbeat_interval(timeout_seconds: float, override: float | None) -> float:
    """Every 15 seconds or a third of the job timeout, whichever is shorter.

    An explicit override above 0 is capped at MAX_HEARTBEAT_SECONDS (below
    every stale threshold orch.yml allows) and at a third of the timeout.
    """
    if not timeout_seconds > 0:
        raise ValueError(f"the job's timeout_seconds ({timeout_seconds:g}) must be positive")
    limit = min(timeout_seconds / 3, MAX_HEARTBEAT_SECONDS)
    if override is None:
        return min(limit, HEARTBEAT_SECONDS)
    if not math.isfinite(override) or override <= 0:
        raise ValueError("--interval must be a number above 0")
    return min(override, limit)


class Budget:
    """A job's wall-clock budget (#199), from when `orch job run` started it."""

    def __init__(
        self,
        seconds: float,
        *,
        grace: float = KILL_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("the wall-clock budget must be a number of seconds above 0")
        self.seconds = seconds
        self.grace = grace
        self.clock = clock
        self.started = clock()
        self.expired = False
        self.signum: int | None = None

    def remaining(self) -> float:
        return max(0.0, self.started + self.seconds - self.clock())

    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started)

    def describe(self) -> str:
        if self.seconds % 60:
            return f"{self.seconds:g}-second wall-clock budget"
        return f"{self.seconds / 60:g}-minute wall-clock budget"


class Heartbeat:
    def __init__(
        self, beat: Beat, interval: float, log: Log, on_end: Callable[[], None] | None = None
    ):
        self._beat = beat
        self._interval = interval
        self._log = log
        self._on_end = on_end
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="orch-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._beat()
            except QueueError as exc:
                self._log(f"heartbeat stopped: {_one_line(exc)}")
                if self._on_end is not None:
                    self._on_end()
                return
            except TRANSIENT_ERRORS as exc:
                self._log(f"heartbeat failed, retrying: {_one_line(exc)}")


def run(
    argv: Sequence[str],
    *,
    beat: Beat,
    interval: float,
    log: Log,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    setup: Callable[[], Path | None] | None = None,
    teardown: Callable[[], None] | None = None,
    started: Callable[[int], None] | None = None,
    open_gate: Callable[[int, Callable[[], None]], None] | None = None,
    output: Callable[[IO[bytes]], None] | None = None,
    execed: Callable[[], None] | None = None,
    budget: Budget | None = None,
) -> int:
    """Beat once, run argv with a heartbeat, and return its exit status.

    A failing first beat propagates and the command never starts. A command
    killed by a signal returns 128 + the signal number; one that cannot be
    started returns 127. `setup` runs under the heartbeat before the command
    (its path, if any, becomes the working directory); `teardown` runs after
    the heartbeat stops, whenever setup succeeded.

    Signals: SIGTERM and SIGHUP are forwarded to the command. SIGINT is not:
    the command shares this process group, so a Ctrl-C or group SIGINT
    reaches it directly, and this process waits for it to exit. The handlers
    are Python handlers, never SIG_IGN, so the command starts with default
    dispositions (a disposition the caller already set to SIG_IGN is left
    alone and inherited, as for any command). A signal that arrives before
    the command starts is not dropped: during setup it means the command
    never runs (exit 128 + the signal); during the spawn it is sent to the
    command once it exists. `started` gets the command's pid before the
    command may run (the device pool records it as the job's worker): the
    process waits at a gate until `started` returns, so a command never runs
    unrecorded. If `started` raises, the gate is closed, the command never
    runs, and 127 is returned. With `open_gate`, run hands it the pid and a
    callable that writes "go" instead of writing it itself, so the caller can
    check, under its own lock, that the command may still run (#462). If
    `open_gate` raises, the command never runs and 127 is returned, as for
    `started`. `execed` is called once the gated command was
    exec'd (or the gate ended without saying it could not exec), never for
    a command that did not start: its status alone cannot say (#99 A2).

    `output` (issue #93, lib/telemetry.py) takes the command's stdout: when
    given, stdout is a pipe and `output(pipe)` runs on a daemon thread until
    the pipe closes. After the command exits run waits up to
    OUTPUT_GRACE_SECONDS for it, then returns either way.

    `budget` (#199): a command still running when it runs out gets SIGTERM,
    then SIGKILL after budget.grace seconds; run returns once it is reaped,
    with budget.expired set and budget.signum the last signal sent.
    """
    beat()
    heartbeat = Heartbeat(beat, interval, log)
    child: subprocess.Popen[bytes] | None = None
    reader: threading.Thread | None = None
    reaper: bool | None = None
    pending: list[int] = []
    previous: dict[int, object] = {}

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        if child is None:
            pending.append(signum)
        elif signum != signal.SIGINT and child.poll() is None:
            child.send_signal(signum)

    heartbeat.start()
    prepared = False
    try:
        for signum in (*FORWARDED_SIGNALS, signal.SIGINT):
            if signal.getsignal(signum) is not signal.SIG_IGN:
                previous[signum] = signal.signal(signum, on_signal)
        if setup is not None:
            workdir = setup()
            if workdir is not None:
                cwd = workdir
        prepared = True
        if pending:
            log(f"not starting {argv[0]}: got signal {pending[0]}")
            return 128 + pending[0]
        tag = secrets.token_hex(16)
        try:
            others = frozenset({(pid, process.start) for pid, process in processes(tag).items()})
        except ProcessesUnknown as exc:
            log(f"not starting {argv[0]}: {exc}")
            return 127
        reaper = _subreaper(True)
        tagged = {**(dict(env) if env is not None else os.environ), RUN_TAG_ENV: tag}
        spawn = list(argv)
        read_end = write_end = report_read = report_write = -1
        gated = started is not None or sys.platform == "darwin"
        if gated:
            read_end, write_end = os.pipe()
            report_read, report_write = os.pipe()
            spawn = [
                sys.executable, "-I", "-S", "-c", _GATE, str(read_end), str(report_write), *spawn
            ]
        try:
            child = subprocess.Popen(
                spawn,
                cwd=cwd,
                env=tagged,
                pass_fds=(read_end, report_write) if gated else (),
                stdout=subprocess.PIPE if output is not None else None,
            )
        except OSError as exc:
            log(f"cannot start {argv[0]}: {_one_line(exc)}")
            return 127
        finally:
            if gated:
                os.close(read_end)
                os.close(report_write)
                if child is None:
                    os.close(write_end)
                    os.close(report_read)
        if output is not None and child.stdout is not None:
            reader = threading.Thread(
                target=output, args=(child.stdout,), name="orch-output", daemon=True
            )
            reader.start()
        if gated:
            gate = write_end

            def go() -> None:
                try:
                    os.write(gate, b"go\n")
                except BrokenPipeError:
                    pass  # the gate already died (a forwarded signal); its status says so

            step = "could not record worker pid"
            try:
                if started is not None:
                    started(child.pid)
                step = "gate not opened for worker pid"
                if open_gate is not None:
                    open_gate(child.pid, go)
                else:
                    go()
            except Exception as exc:
                os.close(write_end)  # the gate reads EOF: the command never runs
                os.close(report_read)
                log(f"not starting {argv[0]}: {step} {child.pid}: {_one_line(exc)}")
                child.wait()
                return 127
            except BaseException:
                os.close(write_end)
                os.close(report_read)
                raise
            os.close(write_end)
            try:
                failed = os.read(report_read, 1) != b""
            finally:
                os.close(report_read)
            if not failed and execed is not None:
                execed()
        elif execed is not None:
            execed()
        for signum in pending:  # arrived while the command was being spawned
            if child.poll() is None:
                child.send_signal(signum)
        tree = Tree(
            child.pid, tag, adopts=reaper is not None, others=others,
            responsible=_responsible_pid if sys.platform == "darwin" else None,
        )
        try:
            code = _wait(child, budget, argv[0], log, tree)
        finally:
            tree.end(log)
        code = _past_budget(code if code >= 0 else 128 - code, budget)
    finally:
        if reaper is not None:
            _subreaper(reaper)
        if reader is not None:
            reader.join(OUTPUT_GRACE_SECONDS)
        heartbeat.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
        if prepared and teardown is not None:
            teardown()
    return code


def _subreaper(on: bool) -> bool | None:
    """Make this process a child subreaper (or not); its earlier setting, None where it cannot."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None
    was = ctypes.c_int(0)
    if prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(was), 0, 0, 0) != 0:
        return None
    if prctl(PR_SET_CHILD_SUBREAPER, int(on), 0, 0, 0) != 0:
        return None
    return bool(was.value)


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


def _past_budget(code: int, budget: Budget | None) -> int:
    """#199 r1: a run stopped past its budget fails, whatever status its command gave."""
    if budget is None or not budget.expired or code != 0:
        return code
    return 128 + (budget.signum or signal.SIGTERM)


def _wait(
    child: subprocess.Popen[bytes], budget: Budget | None, name: str, log: Log, tree: Tree
) -> int:
    """child.wait(), stopping the child once the budget runs out; returns once it is reaped.

    While it waits it tracks the child's process tree every TRACK_SECONDS.
    """
    while True:
        tree.track_if_listable()
        timeout = TRACK_SECONDS if budget is None else min(TRACK_SECONDS, budget.remaining())
        try:
            return child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        if budget is not None and budget.remaining() <= 0:
            break
    assert budget is not None
    budget.expired = True
    log(f"{name} ran past its {budget.describe()}; stopping pid {child.pid} (SIGTERM)")
    budget.signum = signal.SIGTERM
    tree.track_if_listable()
    child.send_signal(signal.SIGTERM)
    grace = time.monotonic() + budget.grace
    while True:
        tree.track_if_listable()
        try:
            return child.wait(timeout=max(0.0, min(0.2, grace - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= grace:
            break
    log(f"{name} (pid {child.pid}) ignored SIGTERM for {budget.grace:g}s; killing it (SIGKILL)")
    budget.signum = signal.SIGKILL
    tree.track_if_listable()
    child.kill()
    return child.wait()


class ProcessesUnknown(Exception):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    start: str
    tagged: bool


def processes(tag: str) -> dict[int, Process]:
    """This machine's live processes (zombies left out), and which carry `tag`.

    From /proc where there is one, else from `ps` (macOS: no tag is read,
    so only the process tree counts there).
    """
    if os.path.isdir("/proc/self"):
        return _proc_processes(tag)
    return _ps_processes()


def _proc_processes(tag: str) -> dict[int, Process]:
    mark = f"{RUN_TAG_ENV}={tag}".encode()
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
        try:
            with open(f"/proc/{name}/environ", "rb") as handle:
                tagged = mark in handle.read().split(b"\0")
        except OSError:
            tagged = False
        found[int(name)] = Process(int(name), int(fields[1]), fields[19], tagged)
    return found


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
    """

    def __init__(
        self,
        root: int,
        tag: str,
        *,
        scan: Callable[[str], dict[int, Process]] = processes,
        signal_pid: Callable[[int, int], None] = os.kill,
        adopts: bool = False,
        others: frozenset[tuple[int, str]] = frozenset(),
        responsible: Callable[[int], int | None] | None = None,
    ):
        self.root = root
        self.responsible = responsible
        self.tag = tag
        self.adopts = adopts
        self.others = others
        self.scan = scan
        self.signal_pid = signal_pid
        self.known: dict[int, str] = {}
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
        root = self.root not in self.foreign
        if root and self.root in listing and (self.root not in self.known or self.root in members):
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
                members = self._kill_round()
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

    def _kill_round(self) -> dict[int, Process]:
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


class Control:
    """How `follow`'s body learns it must stop: a signal, or the job ended elsewhere."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.signum: int | None = None
        self.reason = ""

    def stop(self, signum: int, reason: str) -> None:
        if self.signum is None:
            self.signum, self.reason = signum, reason
        self._event.set()

    def wait(self, seconds: float) -> None:
        """Sleep up to seconds; return at once when asked to stop."""
        self._event.wait(seconds)


def follow(
    body: Callable[[Control], int],
    *,
    beat: Beat,
    interval: float,
    log: Log,
    setup: Callable[[], object] | None = None,
    teardown: Callable[[], None] | None = None,
    budget: Budget | None = None,
) -> int:
    """Beat once, then run body (a remote run's follower) under a heartbeat (#128).

    There is no local child here: the harness runs detached on its device.
    The heartbeat beats the local database on its own thread whatever the
    link does. SIGTERM, SIGHUP and SIGINT, and the heartbeat stopping on a
    QueueError (the job was finished, failed or timed out), set the body's
    Control, which stops the remote worker. A signal during setup means the
    body never runs (exit 128 + the signal). `teardown` runs after the
    heartbeat stops, whenever setup succeeded. A `budget` that runs out
    while the body runs stops the Control the same way (SIGTERM), with
    budget.expired set; the worker's death is confirmed on the device by
    lib/placed_run.py before its slot is freed or its job retried (#199).
    """
    beat()
    control = Control()
    heartbeat = Heartbeat(
        beat, interval, log, on_end=lambda: control.stop(signal.SIGTERM, "the job ended")
    )
    previous: dict[int, object] = {}
    timer: threading.Timer | None = None

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        control.stop(signum, f"got signal {signum}")

    heartbeat.start()
    prepared = False
    try:
        for signum in (*FORWARDED_SIGNALS, signal.SIGINT):
            if signal.getsignal(signum) is not signal.SIG_IGN:
                previous[signum] = signal.signal(signum, on_signal)
        if setup is not None:
            setup()
        prepared = True
        if control.signum is not None:
            log(f"not starting: {control.reason}")
            return 128 + control.signum
        if budget is not None:
            timer = threading.Timer(budget.remaining(), _out_of_budget, (budget, control, log))
            timer.daemon = True
            timer.start()
        return _past_budget(body(control), budget)
    finally:
        if timer is not None:
            timer.cancel()
        heartbeat.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
        if prepared and teardown is not None:
            teardown()


def _out_of_budget(budget: Budget, control: Control, log: Log) -> None:
    if control.signum is not None:
        return  # already stopping for another reason
    budget.expired = True
    budget.signum = signal.SIGTERM
    log(f"the run passed its {budget.describe()}; stopping its remote worker")
    control.stop(signal.SIGTERM, f"past its {budget.describe()}")


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__
