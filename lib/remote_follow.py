"""Follow a detached remote start with short SSH calls (#128).

`orch job run` used to wait on the SSH session that ran the harness, so a
dropped link ended the job. Now lib/remote.py `launch_script` starts the
harness detached on the device, and a `Follower` on the box:

* launches it, sending the same tag and brief on every attempt, so a
  retried or late launch answers `already` and never starts a second one;
* polls the run (state, log tail and exit status in one call), relays the
  log to its own stdout byte for byte (only a whole poll's bytes, from the
  offset it already has: a transfer cut by a drop is fetched again), and
  backs off on an unreachable device (2, 4, 8 ... 60 seconds);
* relays the start's environment report (the device's private `env` file,
  never its log) once, ahead of the log bytes that came with it, and then
  removes it from the device;
* ends only on the device's `exit` file, a worker the device confirms gone
  with no exit status, or `grace` seconds without one certain answer. A
  dropped call is never an end;
* on a cancel (a signal, or the job ended elsewhere: `Control`), stops the
  worker on the device (SIGTERM, then SIGKILL from `KILL_AFTER_SECONDS` on)
  and follows it until it is gone, for at most `STOP_GRACE_SECONDS`.

It never touches the job's state: lib/placed_run.py maps the `FollowEnd`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from lib.remote import (
    FOLLOW_MARK,
    LAUNCH_BUSY,
    LAUNCH_MARK,
    LAUNCH_SHORT,
    LAUNCH_STARTED,
    START_REFUSED,
    WORKER_ENV_MARK,
    ByteTransport,
    Exchanged,
    RemoteTarget,
    _marked,
    env_clear_script,
    poll_script,
    ssh_argv,
    stop_script,
)

FOLLOW_GRACE_SECONDS = 30 * 60.0
KILL_AFTER_SECONDS = 60.0
STOP_GRACE_SECONDS = 120.0
LAUNCH_TTL_SECONDS = 10 * 60
CALL_TIMEOUT_SECONDS = 60.0
FIRST_POLL_SECONDS = 0.25
POLL_SECONDS = 5.0
BACKOFF_FIRST_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
CHUNK_BYTES = 256 * 1024

EXITED = "exited"
GONE = "gone"
LOST = "lost"
REFUSED = "refused"
STOPPED = "stopped"
UNCONFIRMED_STOP = "not confirmed stopped"
BUSY_EXIT = 124

Log = Callable[[str], None]


class Control(Protocol):
    """What the follower reads of `orch job run` (lib/job_run.py `Control`)."""

    @property
    def signum(self) -> int | None: ...

    def wait(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class FollowEnd:
    kind: str
    code: int | None = None
    detail: str = ""
    in_transit: bool = True

    @property
    def worker_ended(self) -> bool:
        """The device saw the worker exit or vanish, so nothing of the run still runs there."""
        return self.kind in (EXITED, GONE) or (self.kind == STOPPED and self.detail != UNCONFIRMED_STOP)


@dataclass(frozen=True)
class _Poll:
    size: int
    state: str
    code: int | None
    data: bytes
    env: str | None = None


def backoff(failures: int) -> float:
    """2, 4, 8 ... seconds, capped at 60, after the nth failure in a row."""
    return min(BACKOFF_FIRST_SECONDS * 2 ** min(failures, 16), BACKOFF_MAX_SECONDS)


class Follower:
    def __init__(
        self,
        transport: ByteTransport,
        target: RemoteTarget,
        sink: BinaryIO,
        *,
        log: Log,
        grace: float = FOLLOW_GRACE_SECONDS,
        call_timeout: float = CALL_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ):
        self.transport = transport
        self.target = target
        self.sink = sink
        self.log = log
        self.grace = grace
        self.call_timeout = call_timeout
        self.clock = clock
        self._sleep = sleep
        self.offset = 0
        self._env_relayed = False
        self._logged: set[bool] = set()

    def run(self, launch: str, brief: bytes, control: Control) -> FollowEnd:
        """Launch (lib/remote.py launch_script) and follow until the run ends."""
        sleep = self._sleep or control.wait
        end = self._launch(launch, brief, control, sleep)
        if end is not None:
            return end
        return self._follow(control, sleep)

    def _call(self, script: str, brief: bytes = b"") -> Exchanged:
        return self.transport.exchange(
            ssh_argv(self.target, script), timeout=self.call_timeout, input=brief
        )

    def _launch(
        self, launch: str, brief: bytes, control: Control, sleep: Callable[[float], None]
    ) -> FollowEnd | None:
        contact = self.clock()
        failures = 0
        unanswered = False
        while control.signum is None:
            result = self._call(launch, brief)
            word = _marked(result.stdout.decode("utf-8", "replace"), LAUNCH_MARK)
            if word is not None:
                head, _, why = word.partition(" ")
                if head in LAUNCH_STARTED:
                    self.log(f"launched on {self.target.device} ({head}); following it")
                    return None
                if head == LAUNCH_BUSY:
                    return FollowEnd(
                        REFUSED, BUSY_EXIT, "another run of the job is live there", unanswered
                    )
                if head != LAUNCH_SHORT:
                    return FollowEnd(REFUSED, START_REFUSED, why or head, unanswered)
            elif result.code != 255:
                return FollowEnd(
                    REFUSED, START_REFUSED, f"the launch exited {result.code}", unanswered
                )
            else:
                unanswered = True
            if self.clock() - contact >= self.grace:
                return FollowEnd(LOST, None, "the launch never got an answer")
            wait = backoff(failures)
            failures += 1
            self.log(
                f"launch on {self.target.device} got no answer "
                f"(exit {result.code}); retrying in {wait:g}s"
            )
            sleep(wait)
        return self._follow(control, sleep)

    def _follow(self, control: Control, sleep: Callable[[float], None]) -> FollowEnd:
        contact = self.clock()
        failures = 0
        pause = FIRST_POLL_SECONDS
        stopped_at: float | None = None
        while True:
            stopping = control.signum is not None
            if stopping and stopped_at is None:
                stopped_at = self.clock()
            elif stopped_at is not None and self.clock() - stopped_at >= STOP_GRACE_SECONDS:
                # lib/placed_run.py finish stops it again; the slot stays held until it is gone.
                return FollowEnd(STOPPED, 128 + (control.signum or 15), UNCONFIRMED_STOP)
            poll = self._poll()
            if poll is not None and poll.state in (EXITED, GONE, "none", "running"):
                contact = self.clock()
            if poll is None:
                if self.clock() - contact >= self.grace:
                    return FollowEnd(LOST, None, f"no answer for {self.grace:g} seconds")
                wait = backoff(failures)
                failures += 1
                self.log(f"lost the link to {self.target.device}; reattaching in {wait:g}s")
                sleep(wait)
                continue
            if failures:
                self.log(f"reattached to {self.target.device} at log byte {self.offset}")
            failures = 0
            if poll.env is not None:
                self._relay_env(poll.env)
            if poll.data:
                self.sink.write(poll.data)
                self.sink.flush()
                self.offset += len(poll.data)
            if self.offset < poll.size:
                continue
            if poll.state == EXITED:
                return FollowEnd(STOPPED if stopping else EXITED, poll.code)
            if poll.state in (GONE, "none"):
                if stopping:
                    return FollowEnd(STOPPED, 128 + (control.signum or 15))
                return FollowEnd(GONE, None, "worker gone without an exit status")
            if self.clock() - contact >= self.grace:
                return FollowEnd(LOST, None, f"no certain answer for {self.grace:g} seconds")
            if stopping and poll.state == "running":
                assert stopped_at is not None
                self._stop(kill=self.clock() - stopped_at >= KILL_AFTER_SECONDS)
            sleep(pause)
            pause = min(pause * 2, POLL_SECONDS)

    def _relay_env(self, report: str) -> None:
        if not self._env_relayed:
            self._env_relayed = True
            self.sink.write(report.encode("ascii", "replace") + b"\n")
            self.sink.flush()
        self.transport.run(
            ssh_argv(self.target, env_clear_script(self.target)), timeout=self.call_timeout
        )

    def _stop(self, *, kill: bool) -> None:
        if kill not in self._logged:
            self._logged.add(kill)
            action = (
                f"killing it: still running {KILL_AFTER_SECONDS:g}s after SIGTERM"
                if kill
                else "stopping it"
            )
            self.log(f"job {self.target.job_id} worker still runs on {self.target.device}; {action}")
        self.transport.run(
            ssh_argv(self.target, stop_script(self.target, kill=kill)), timeout=self.call_timeout
        )

    def _poll(self) -> _Poll | None:
        """One poll from the current offset; None unless the whole answer came."""
        result = self._call(poll_script(self.target, self.offset, CHUNK_BYTES))
        if result.code != 0:
            return None
        word = _marked(result.stderr, FOLLOW_MARK)
        parts = word.split() if word is not None else []
        if len(parts) not in (2, 3) or not parts[0].isdigit():
            return None
        size, state = int(parts[0]), parts[1]
        code = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None
        if state == EXITED and code is None:
            return None
        expected = max(0, min(CHUNK_BYTES, size - self.offset))
        if len(result.stdout) != expected:
            return None
        env = next(
            (line for line in result.stderr.splitlines() if line.startswith(WORKER_ENV_MARK + " ")),
            None,
        )
        return _Poll(size, state, code, result.stdout, env)
