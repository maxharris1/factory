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
from typing import IO

from lib.job_state import HEARTBEAT_SECONDS, MAX_HEARTBEAT_SECONDS
from lib.queue import QueueError

FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# How long run waits, once the command has exited, for `output` to drain the
# rest of its stdout (a process the command left behind may hold the pipe).
OUTPUT_GRACE_SECONDS = 5.0
TRANSIENT_ERRORS = (sqlite3.Error, OSError)

Beat = Callable[[], object]
Log = Callable[[str], None]

# With `started`, the command is spawned behind this gate: it waits for
# "go" on the pipe whose read end is fd argv[1], then execs the command
# (same pid). "go" is written only after `started` has recorded the pid, so
# a runner that dies, or cannot record it, closes the pipe first and the
# command never runs. Before the exec it puts back the signal dispositions
# Python changed at its own start-up, as Popen does for a direct command.
_GATE = """\
import os, signal, sys
if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
fd = int(sys.argv[1])
go = os.read(fd, 3) == b"go\\n"
os.close(fd)
if not go:
    sys.exit(125)
for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
try:
    os.execvp(sys.argv[2], sys.argv[2:])
except OSError as exc:
    print(f"orch job run: cannot start {sys.argv[2]}: {exc}", file=sys.stderr)
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
    def __init__(self, beat: Beat, interval: float, log: Log):
        self._beat = beat
        self._interval = interval
        self._log = log
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
    output: Callable[[IO[bytes]], None] | None = None,
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
    runs, and 127 is returned.

    `output` (issue #93, lib/telemetry.py) takes the command's stdout: when
    given, stdout is a pipe and `output(pipe)` runs on a daemon thread until
    the pipe closes. After the command exits run waits up to
    OUTPUT_GRACE_SECONDS for it, then returns either way.
    """
    beat()
    heartbeat = Heartbeat(beat, interval, log)
    child: subprocess.Popen[bytes] | None = None
    reader: threading.Thread | None = None
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
        read_end = write_end = -1
        if started is not None:
            read_end, write_end = os.pipe()
            spawn = [sys.executable, "-I", "-S", "-c", _GATE, str(read_end), *spawn]
        try:
            child = subprocess.Popen(
                spawn,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                pass_fds=(read_end,) if started is not None else (),
                stdout=subprocess.PIPE if output is not None else None,
            )
        except OSError as exc:
            log(f"cannot start {argv[0]}: {_one_line(exc)}")
            return 127
        finally:
            if started is not None:
                os.close(read_end)
                if child is None:
                    os.close(write_end)
        if output is not None and child.stdout is not None:
            reader = threading.Thread(
                target=output, args=(child.stdout,), name="orch-output", daemon=True
            )
            reader.start()
        if started is not None:
            try:
                started(child.pid)
            except Exception as exc:
                os.close(write_end)  # the gate reads EOF: the command never runs
                log(
                    f"not starting {argv[0]}: could not record worker pid "
                    f"{child.pid}: {_one_line(exc)}"
                )
                child.wait()
                return 127
            except BaseException:
                os.close(write_end)
                raise
            try:
                os.write(write_end, b"go\n")
            except BrokenPipeError:
                pass  # the gate already died (a forwarded signal); its status says so
            finally:
                os.close(write_end)
        for signum in pending:  # arrived while the command was being spawned
            if child.poll() is None:
                child.send_signal(signum)
        code = child.wait()
    finally:
        if reader is not None:
            reader.join(OUTPUT_GRACE_SECONDS)
        heartbeat.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
        if prepared and teardown is not None:
            teardown()
    return code if code >= 0 else 128 - code


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__
