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
  can outlive the job.

`run` never finishes the job: `orch job done` stays the bot's call.
"""

from __future__ import annotations

import math
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType

from lib.job_state import HEARTBEAT_SECONDS, MAX_HEARTBEAT_SECONDS
from lib.queue import QueueError

FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
TRANSIENT_ERRORS = (sqlite3.Error, OSError)

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
try:
    os.execvp(sys.argv[3], sys.argv[3:])
except OSError as exc:
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
    execed: Callable[[], None] | None = None,
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
    runs, and 127 is returned. `execed` is called once the gated command was
    exec'd (or the gate ended without saying it could not exec), never for
    a command that did not start: its status alone cannot say (#99 A2).
    """
    beat()
    heartbeat = Heartbeat(beat, interval, log)
    child: subprocess.Popen[bytes] | None = None
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
        spawn = list(argv)
        read_end = write_end = report_read = report_write = -1
        if started is not None:
            read_end, write_end = os.pipe()
            report_read, report_write = os.pipe()
            spawn = [
                sys.executable, "-I", "-S", "-c", _GATE, str(read_end), str(report_write), *spawn
            ]
        try:
            child = subprocess.Popen(
                spawn,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                pass_fds=(read_end, report_write) if started is not None else (),
            )
        except OSError as exc:
            log(f"cannot start {argv[0]}: {_one_line(exc)}")
            return 127
        finally:
            if started is not None:
                os.close(read_end)
                os.close(report_write)
                if child is None:
                    os.close(write_end)
                    os.close(report_read)
        if started is not None:
            try:
                started(child.pid)
            except Exception as exc:
                os.close(write_end)  # the gate reads EOF: the command never runs
                os.close(report_read)
                log(
                    f"not starting {argv[0]}: could not record worker pid "
                    f"{child.pid}: {_one_line(exc)}"
                )
                child.wait()
                return 127
            except BaseException:
                os.close(write_end)
                os.close(report_read)
                raise
            try:
                os.write(write_end, b"go\n")
            except BrokenPipeError:
                pass  # the gate already died (a forwarded signal); its status says so
            finally:
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
        code = child.wait()
    finally:
        heartbeat.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
        if prepared and teardown is not None:
            teardown()
    return code if code >= 0 else 128 - code


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
) -> int:
    """Beat once, then run body (a remote run's follower) under a heartbeat (#128).

    There is no local child here: the harness runs detached on its device.
    The heartbeat beats the local database on its own thread whatever the
    link does. SIGTERM, SIGHUP and SIGINT, and the heartbeat stopping on a
    QueueError (the job was finished, failed or timed out), set the body's
    Control, which stops the remote worker. A signal during setup means the
    body never runs (exit 128 + the signal). `teardown` runs after the
    heartbeat stops, whenever setup succeeded.
    """
    beat()
    control = Control()
    heartbeat = Heartbeat(
        beat, interval, log, on_end=lambda: control.stop(signal.SIGTERM, "the job ended")
    )
    previous: dict[int, object] = {}

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
        return body(control)
    finally:
        heartbeat.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
        if prepared and teardown is not None:
            teardown()


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__
