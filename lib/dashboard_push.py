"""Pluggable outbound push for the dashboard snapshot.

The box cannot take inbound tailnet connections, so it pushes. A transport
gets the snapshot files and sends them somewhere. `none` (the default) is
`NoopTransport`: `orch dashboard push` logs one line and does nothing.
`ssh` is `SshTransport`: rsync (default) or scp over an SSH host alias, for
example `pc-wsl-cursor-bot` (the pc-wsl-bots node as cursor-bot). It runs
with BatchMode, so it never prompts, and it relies entirely on the SSH login
Max provisioned: optionally `-F <ssh_config>` names his ssh_config file. No
key path, password or token is read, passed or configured here.

Both methods publish the pair as one step. Every file is first uploaded to
a dot-prefixed temp name in the served directory; only after all uploads
succeed does one `ssh host 'cd -- dir && mv -f -- .a.push-tmp a && mv -f --
.b.push-tmp b'` rename them over the live names, back to back on the remote
host. An interrupted upload leaves the published pair as it was, and no
network round-trip separates the two renames, so a reader never sees a
half-written file or (outside the instant between two local renames) a
mismatched pair.

`PollPusher` is how `orch poll` (deploy/poller-service.sh) pushes: after a
poll cycle, at most every `every_minutes`, it starts `orch dashboard push
--once` as a separate process with a scrubbed environment and does not wait
for it. A push failure is that child's redacted log line; it never reaches
the poll loop.
"""

from __future__ import annotations

import re
import subprocess
import sys
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
# Names of the files sent (status.json, index.html): plain, no leading '-'.
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
    log: Callable[[str], None],
) -> PollPusher | None:
    """The poll loop's push hook, or None when dashboard.push is not set."""
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
    return PollPusher(argv, env, every, spawn=spawn, clock=clock, log=log)
