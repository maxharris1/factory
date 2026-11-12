"""Pluggable outbound push for the dashboard snapshot.

The box cannot take inbound tailnet connections, so it pushes. A transport
gets the snapshot files and sends them somewhere. `none` (the default) is
`NoopTransport`: `orch dashboard push` logs one line and does nothing.
`ssh` is `SshTransport`: rsync (default) or scp over an SSH host alias, for
example `pc-wsl-cursor-bot` (the pc-wsl-bots node as cursor-bot). It runs
with BatchMode, so it never prompts, and it relies entirely on the SSH login
Max provisioned: optionally `-F <ssh_config>` names his ssh_config file. No
key path, password or token is read, passed or configured here.

Both methods publish the files (status.json and the pages) as one step. Every file is first uploaded to
a dot-prefixed temp name in the served directory; only after all uploads
succeed does one `ssh host 'cd -- dir && mv -f -- .a.push-tmp a && mv -f --
.b.push-tmp b && ...'` rename them over the live names, back to back on the
remote host. An interrupted upload leaves the published set as it was, and
no network round-trip separates the renames, so a reader never sees a
half-written file or (outside the instants between local renames) a
mismatched set.

`PollPusher` is how `orch poll` (deploy/poller-service.sh) pushes: after a
poll cycle, at most every `every_minutes`, it starts `orch dashboard push
--once` as a separate process with a scrubbed environment and does not wait
for it. A push failure is that child's redacted log line; it never reaches
the poll loop.

`LivePusher` (`dashboard.push.live: true`) adds real-time pushes on top. A
watcher thread stats orch.db and its WAL every WATCH_SECONDS; any committed
change (a job claimed, a heartbeat, a job finished, a verdict recorded, by
the poller or by any other process) and the end of every poll cycle start
`orch dashboard push --once --status-only`: status.json alone, uploaded and
renamed exactly like the full set. Pushes start at most once every
LIVE_MIN_SECONDS, never while the previous one runs, and a change that
arrives meanwhile is pushed as soon as both allow, so the last change is
never dropped. The full set (the pages too) still goes out at the
`PollPusher` cadence.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from lib.dashboard import redact
from lib.dashboard_config import (
    METHODS,
    PushConfig,
    in_range,
    looks_like_key,
    valid_host,
    valid_remote_dir,
    valid_ssh_config,
)

NOT_CONFIGURED = (
    "dashboard push: not configured (dashboard.push.transport is none); "
    "nothing pushed"
)
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
SEND_TIMEOUT_SECONDS = 60
TEMP_SUFFIX = ".push-tmp"
# Names of the files sent (status.json and the pages): plain, no leading '-'.
FILE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
ROOT = Path(__file__).resolve().parent.parent


class PushError(RuntimeError):
    pass


class Transport(Protocol):
    def send(self, paths: Sequence[Path]) -> None: ...


class NoopTransport:
    """The default: push is not configured, so nothing is sent anywhere."""

    def send(self, paths: Sequence[Path]) -> None:
        return None


Runner = Callable[..., subprocess.CompletedProcess]


class SshTransport:
    def __init__(
        self,
        host: str,
        remote_dir: str,
        *,
        method: str = "rsync",
        ssh_config: str | None = None,
        runner: Runner = subprocess.run,
        timeout: int = SEND_TIMEOUT_SECONDS,
    ):
        if not valid_host(host):
            raise ValueError("host must be an SSH host alias or tailnet name")
        if not valid_remote_dir(remote_dir):
            raise ValueError("remote_dir must be a plain path")
        if method not in METHODS:
            raise ValueError("method must be rsync or scp")
        if ssh_config is not None and (
            not valid_ssh_config(ssh_config) or looks_like_key(ssh_config)
        ):
            raise ValueError("ssh_config must be a plain absolute ssh_config path")
        self.host = host
        self.remote_dir = remote_dir
        self.method = method
        self.ssh_config = ssh_config
        self.runner = runner
        self.timeout = timeout

    def _remote(self, name: str) -> str:
        return f"{self.remote_dir.rstrip('/')}/{name}"

    def _ssh_options(self) -> list[str]:
        config = ["-F", self.ssh_config] if self.ssh_config else []
        return [*config, *SSH_OPTIONS]

    def commands(self, paths: Sequence[Path]) -> list[list[str]]:
        """The argv lists one send runs, in order: every upload to a temp
        name, then one ssh that renames them all over the live names."""
        names = [path.name for path in paths]
        if not names or any(FILE_NAME.fullmatch(name) is None for name in names):
            raise ValueError("files to push must have plain names")
        options = self._ssh_options()
        temps = [f".{name}{TEMP_SUFFIX}" for name in names]
        if self.method == "rsync":
            upload = [
                "rsync",
                "--quiet",
                "--timeout=30",
                "--chmod=F644",
                "-e",
                "ssh " + " ".join(options),
                "--",
            ]
        else:
            upload = ["scp", "-q", "-B", *options, "--"]
        uploads = [
            [*upload, str(path), f"{self.host}:{self._remote(temp)}"]
            for path, temp in zip(paths, temps)
        ]
        # remote_dir and the names are validated to a conservative character
        # set with no leading '-', so the remote shell sees only plain words.
        renames = " && ".join(
            [f"cd -- {self.remote_dir}"]
            + [f"mv -f -- {temp} {name}" for temp, name in zip(temps, names)]
        )
        return uploads + [["ssh", *options, "--", self.host, renames]]

    def send(self, paths: Sequence[Path]) -> None:
        for argv in self.commands(paths):
            self._run(argv)

    def _run(self, argv: list[str]) -> None:
        program = argv[0]
        try:
            result = self.runner(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise PushError(
                f"{program} to {self.host} timed out after {self.timeout}s"
            ) from None
        except FileNotFoundError:
            raise PushError(f"{program} is not installed") from None
        if result.returncode != 0:
            detail = next(
                (line.strip() for line in (result.stderr or "").splitlines() if line.strip()),
                "",
            )
            message = f"{program} to {self.host} failed (exit {result.returncode})"
            if detail:
                message += f": {redact(detail)[:200]}"
            raise PushError(message)


def transport_for(config: PushConfig, *, runner: Runner = subprocess.run) -> Transport:
    if config.transport == "none":
        return NoopTransport()
    if config.transport == "ssh":
        assert config.host is not None and config.remote_dir is not None
        return SshTransport(
            config.host,
            config.remote_dir,
            method=config.method,
            ssh_config=config.ssh_config,
            runner=runner,
        )
    raise ValueError(f"unknown transport {config.transport!r}")


def run_push(
    *,
    transport: Transport,
    snapshot: Callable[[], Sequence[Path]],
    interval: int,
    once: bool,
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> int:
    """Snapshot then send, every `interval` seconds (15 to 30), or once.

    A failed snapshot or send logs one redacted line; the loop keeps going.
    With --once the exit status is 1 after a failure.
    """
    if isinstance(transport, NoopTransport):
        log(NOT_CONFIGURED)
        return 0
    if not in_range(interval):
        raise ValueError("push interval must be 15 to 30 seconds")
    while True:
        try:
            transport.send(snapshot())
            ok = True
        except Exception as exc:  # keep pushing after any one failure
            text = " ".join(str(exc).split()) or type(exc).__name__
            log(f"dashboard push: {redact(text)}")
            ok = False
        if once:
            return 0 if ok else 1
        sleep(interval)


# --- pushing from the poll loop -----------------------------------------------

# The only variables a poll-cycle push child inherits. The poller's own
# environment holds the Grok webhook values, GitHub and Linear tokens; the
# push needs none of them. SSH_AUTH_SOCK is passed so an agent-held login
# still works; it names a socket, not a secret.
CHILD_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "SSH_AUTH_SOCK",
    "ORCH_DB",
    "ORCH_POLL_LOG",
    "ORCH_DASHBOARD_DIR",
)
STILL_RUNNING = "dashboard push: previous push still running; skipped this cycle"

Spawn = Callable[..., object]


class PollPusher:
    """Called by `PollLoop` after every poll cycle.

    At most every `every_seconds` (0: every call) it starts `argv` (`orch
    dashboard push --once ...`) as a separate process and returns at once.
    While the previous push is still running it skips. It never raises: a
    failure to start is one redacted log line, and a failed push is the
    child's own redacted line on the shared stderr (poller.log).
    """

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        every_seconds: int,
        *,
        spawn: Spawn = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None],
    ):
        self.argv = list(argv)
        self.env = dict(env)
        self.every_seconds = every_seconds
        self.spawn = spawn
        self.clock = clock
        self.log = log
        self._child: object | None = None
        self._last: float | None = None

    def __call__(self) -> None:
        try:
            self._tick()
        except Exception as exc:  # a push never breaks polling
            text = " ".join(str(exc).split()) or type(exc).__name__
            self.log(f"dashboard push: {redact(text)}")

    def _tick(self) -> None:
        if self._child is not None and self._child.poll() is None:
            self.log(STILL_RUNNING)
            return
        self._child = None
        now = self.clock()
        if self._last is not None and now - self._last < self.every_seconds:
            return
        self._last = now
        self._child = self.spawn(
            self.argv,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            close_fds=True,
            shell=False,
        )


def poll_pusher_for(
    config: PushConfig,
    *,
    db: Path,
    orch: Path | None,
    environ: Mapping[str, str],
    spawn: Spawn = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], float] = time.time,
    fingerprint: "Fingerprint | None" = None,
    log: Callable[[str], None],
) -> PollPusher | None:
    """The poll loop's push hook, or None when dashboard.push is not set.

    With `live: true` it is a `LivePusher`; `orch poll` starts its watcher.
    """
    if config.transport == "none":
        return None
    argv = [
        sys.executable,
        str(ROOT / "bin" / "orch"),
        "dashboard",
        "push",
        "--once",
        "--db",
        str(db),
    ]
    if orch is not None:
        argv += ["--orch", str(orch)]
    env = {name: environ[name] for name in CHILD_ENV if name in environ}
    env["PYTHONUNBUFFERED"] = "1"
    every = (config.every_minutes or 0) * 60
    if config.live:
        return LivePusher(
            argv,
            env,
            every,
            db=db,
            spawn=spawn,
            clock=clock,
            wall=wall,
            fingerprint=fingerprint if fingerprint is not None else db_fingerprint,
            log=log,
        )
    return PollPusher(argv, env, every, spawn=spawn, clock=clock, log=log)


# --- real-time pushes -------------------------------------------------------------

LIVE_MIN_SECONDS = 1.0
WATCH_SECONDS = 0.25
LIVE_ERROR_LOG_SECONDS = 60.0

Fingerprint = Callable[[Path], object]


def _stat_key(path: Path) -> tuple[int, int, int] | None:
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def db_fingerprint(path: Path) -> tuple:
    """What changes when anyone commits to the SQLite database at `path`.

    A WAL-mode commit appends to orch.db-wal (its size or mtime changes); a
    checkpoint or a rollback-journal commit rewrites orch.db. A read-only
    reader writes neither: it may create an empty -wal, so an empty WAL
    counts the same as none, and the push child's own snapshot never looks
    like a change. Only stat() is used; no SQLite connection is opened.
    """
    wal = _stat_key(Path(f"{path}-wal"))
    if wal is not None and wal[1] == 0:
        wal = None
    return (_stat_key(path), wal)


class LivePusher(PollPusher):
    """`PollPusher` plus a status.json push on every database change.

    `__call__` (the poll loop's after_poll) marks the end of a cycle: a
    change, and the time `--last-cycle` reports for poll health; the full
    set is due on the first cycle and then every `every_seconds`. `tick()`
    (the watcher, every WATCH_SECONDS) compares the database fingerprint and
    starts whatever push is pending when the rate limit and the previous
    child allow. Neither ever raises; failures are one redacted log line at
    most every LIVE_ERROR_LOG_SECONDS.
    """

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        every_seconds: int,
        *,
        db: Path,
        spawn: Spawn = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        fingerprint: Fingerprint = db_fingerprint,
        log: Callable[[str], None],
        min_interval: float = LIVE_MIN_SECONDS,
    ):
        super().__init__(argv, env, every_seconds, spawn=spawn, clock=clock, log=log)
        self.db = db
        self.wall = wall
        self.fingerprint = fingerprint
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._pending_status = False
        self._pending_full = False
        self._last_full: float | None = None
        self._last_cycle: float | None = None
        self._last_error_log: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            self._seen: object = self.fingerprint(self.db)
        except Exception:  # the first tick logs it
            self._seen = None

    # The poll loop's hook: the end of a cycle.
    def __call__(self) -> None:
        with self._lock:
            try:
                self._last_cycle = self.wall()
                self._pending_status = True
                now = self.clock()
                if self._last_full is None or now - self._last_full >= self.every_seconds:
                    self._pending_full = True
                self._pump()
            except Exception as exc:  # a push never breaks polling
                self._error(exc)

    def tick(self) -> None:
        with self._lock:
            try:
                current = self.fingerprint(self.db)
                if current != self._seen:
                    self._seen = current
                    self._pending_status = True
                self._pump()
            except Exception as exc:  # a push never breaks polling
                self._error(exc)

    def _pump(self) -> None:
        if not (self._pending_status or self._pending_full):
            return
        if self._child is not None and self._child.poll() is None:
            return
        self._child = None
        now = self.clock()
        if self._last is not None and now - self._last < self.min_interval:
            return
        full = self._pending_full
        argv = list(self.argv)
        if not full:
            argv.append("--status-only")
        if self._last_cycle is not None:
            argv += ["--last-cycle", f"{self._last_cycle:.3f}"]
        # Cleared before the spawn: a failed start is retried on the next
        # change or cycle, not every tick.
        self._pending_status = False
        self._pending_full = False
        self._last = now
        if full:
            self._last_full = now
        self._child = self.spawn(
            argv,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            close_fds=True,
            shell=False,
        )

    def _error(self, exc: BaseException) -> None:
        now = self.clock()
        if (
            self._last_error_log is not None
            and now - self._last_error_log < LIVE_ERROR_LOG_SECONDS
        ):
            return
        self._last_error_log = now
        text = " ".join(str(exc).split()) or type(exc).__name__
        self.log(f"dashboard push: {redact(text)}")

    # The watcher thread.
    @property
    def watching(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, interval: float = WATCH_SECONDS) -> None:
        if self.watching:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch,
            args=(interval,),
            name="dashboard-live-watch",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _watch(self, interval: float) -> None:
        while not self._stop.wait(interval):
            self.tick()
