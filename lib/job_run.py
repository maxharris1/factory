"""`orch job run`: run a job's command under an orch-owned heartbeat (#49).

Bots used to hand-roll `while true; do orch job heartbeat ...; sleep; done`
loops, and one outlived its job by hours. Here the heartbeat lives in the
same process that runs the command:

* the first heartbeat is sent before the command starts, so a job that is
  not running (or not this bot's) is refused and nothing runs;
* it then beats every `interval` seconds on a daemon thread;
* it stops for good on the first QueueError (the job was finished, failed or
  timed out, e.g. by the command's own `orch job done`), and retries
  transient storage errors at the next interval;
* it stops when the command exits, and dies with this process, so no loop
  can outlive the job.

`run` never finishes the job: `orch job done` stays the bot's call.
"""

from __future__ import annotations

import math
import signal
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType

from lib.queue import QueueError

FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
TRANSIENT_ERRORS = (sqlite3.Error, OSError)

Beat = Callable[[], object]
Log = Callable[[str], None]


def heartbeat_interval(timeout_seconds: float, override: float | None) -> float:
    """A third of the job timeout, or a faster explicit override."""
    if not timeout_seconds > 0:
        raise ValueError(f"the job's timeout_seconds ({timeout_seconds:g}) must be positive")
    limit = timeout_seconds / 3
    if override is None:
        return limit
    if not math.isfinite(override) or override <= 0 or override > limit:
        raise ValueError(
            f"--interval must be above 0 and at most a third of the job's "
            f"timeout_seconds ({limit:g})"
        )
    return override


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
    command once it exists. `started` gets the command's pid once it exists
    (the device pool records it as the job's worker).
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
        try:
            child = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env) if env is not None else None,
            )
        except OSError as exc:
            log(f"cannot start {argv[0]}: {_one_line(exc)}")
            return 127
        if started is not None:
            try:
                started(child.pid)
            except Exception as exc:  # recording is best effort; never orphan the command
                log(f"could not record worker pid {child.pid}: {_one_line(exc)}")
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


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__
