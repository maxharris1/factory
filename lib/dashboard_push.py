"""Pluggable outbound push for the dashboard snapshot.

The box cannot take inbound tailnet connections, so it pushes. A transport
gets the snapshot files and sends them somewhere. `none` (the default) is
`NoopTransport`: `orch dashboard push` logs one line and does nothing.
`ssh` is `SshTransport`: rsync (default) or scp over an SSH host alias, for
example the pc-wsl-bots node. It runs with BatchMode, so it never prompts,
and it relies entirely on the SSH login Max provisioned in ~/.ssh/config.
No key path, password or token is read, passed or configured here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from lib.dashboard import redact
from lib.dashboard_config import (
    METHODS,
    PushConfig,
    in_range,
    valid_host,
    valid_remote_dir,
)

NOT_CONFIGURED = (
    "dashboard push: not configured (dashboard.push.transport is none); "
    "nothing pushed"
)
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
SEND_TIMEOUT_SECONDS = 60


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
        runner: Runner = subprocess.run,
        timeout: int = SEND_TIMEOUT_SECONDS,
    ):
        if not valid_host(host):
            raise ValueError("host must be an SSH host alias or tailnet name")
        if not valid_remote_dir(remote_dir):
            raise ValueError("remote_dir must be a plain absolute path")
        if method not in METHODS:
            raise ValueError("method must be rsync or scp")
        self.host = host
        self.remote_dir = remote_dir
        self.method = method
        self.runner = runner
        self.timeout = timeout

    def argv(self, paths: Sequence[Path]) -> list[str]:
        destination = f"{self.host}:{self.remote_dir.rstrip('/')}/"
        sources = [str(path) for path in paths]
        if self.method == "rsync":
            # rsync writes each file to a temp name and renames it into place.
            return [
                "rsync",
                "--quiet",
                "--timeout=30",
                "--chmod=F644",
                "-e",
                "ssh " + " ".join(SSH_OPTIONS),
                "--",
                *sources,
                destination,
            ]
        return ["scp", "-q", "-B", *SSH_OPTIONS, "--", *sources, destination]

    def send(self, paths: Sequence[Path]) -> None:
        argv = self.argv(paths)
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
                f"{self.method} to {self.host} timed out after {self.timeout}s"
            ) from None
        except FileNotFoundError:
            raise PushError(f"{self.method} is not installed") from None
        if result.returncode != 0:
            detail = next(
                (line.strip() for line in (result.stderr or "").splitlines() if line.strip()),
                "",
            )
            message = f"{self.method} to {self.host} failed (exit {result.returncode})"
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
