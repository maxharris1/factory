"""Remote job start over the box's SSH aliases (docs/SPEC.md F2-02).

A job the device pool placed on a remote device runs there as that
harness's own bot user. The box reaches the device through the Host alias
named by the device's `ssh_alias` in machines.yml, with `{bot}` resolved to
the bot user (`pc-wsl-{bot}` -> `pc-wsl-codex-bot`). The alias, its host
name and its key live only in the box user's own SSH client config; this
module never names, reads or passes a key, a user@ prefix or a config file.
`-l <bot user>` is always passed as well, so even a mistyped alias cannot
log a job in as another bot.

Three remote POSIX shell scripts are sent as the SSH command:

* start: make `<workdir>/jobs/<job-id>`, record the worker's process group
  in `.orch-worker.pid` there, run the harness command with the job's
  environment, remove the pid file and exit with the command's status.
  sshd runs the command in a new session, so the login shell that runs
  the script leads the process group every child joins;
* check: exit 0 when the worker is gone (no pid file, or no process left
  in its group), 1 while it runs, 3 when it cannot tell; ssh itself exits
  255 when the device cannot be reached. `kill -0 -PGID` is written
  without `--`, which dash refuses, and "gone" is only reported after
  `kill -0` has been seen to work on the check's own shell, so a shell
  whose kill fails never frees a slot;
* stop: send SIGTERM to the worker's process group.

Every ssh call uses the `ssh` found on PATH with BatchMode (never a
password prompt), a connect timeout and server keepalives. Tests replace
it, or put a fake `ssh` first on PATH; nothing here contacts a real host
on its own.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

SSH_BINARY = "ssh"
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=60",
    "-o",
    "ServerAliveCountMax=5",
)
PROBE_TIMEOUT_SECONDS = 60.0
PID_FILE = ".orch-worker.pid"
# The start script's own exit status when it cannot make or enter the job
# directory, or record the worker: the harness never ran.
START_REFUSED = 125
SSH_UNREACHABLE = 255

GONE = "gone"
RUNNING = "running"
UNKNOWN = "unknown"
UNREACHABLE = "unreachable"

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class RemoteError(ValueError):
    """A remote target cannot be built. The message is one line."""


@dataclass(frozen=True)
class RemoteTarget:
    """Where one job's worker runs: device, alias, bot user and directory."""

    device: str
    alias: str
    user: str
    workdir_root: str
    job_id: str

    def __post_init__(self) -> None:
        if not _JOB_ID.fullmatch(self.job_id):
            raise RemoteError(f"job id {self.job_id!r} is not a plain token")
        if not self.alias or self.alias.startswith("-") or "@" in self.alias:
            raise RemoteError(f"ssh alias {self.alias!r} is not a bare Host alias")
        if not self.user or self.user.startswith("-"):
            raise RemoteError(f"bot user {self.user!r} is not a user name")
        if not self.workdir_root.startswith("/"):
            raise RemoteError("workdir_root must be absolute")

    @property
    def job_dir(self) -> str:
        return f"{self.workdir_root.rstrip('/')}/jobs/{self.job_id}"


def ssh_argv(target: RemoteTarget, script: str) -> list[str]:
    """`ssh <options> -l <bot user> -- <alias> <script>`: one argv, no local shell."""
    return [
        SSH_BINARY,
        *SSH_OPTIONS,
        "-l",
        target.user,
        "--",
        target.alias,
        script,
    ]


def start_script(
    target: RemoteTarget,
    command: Sequence[str],
    env: Mapping[str, str],
) -> str:
    """The remote script that runs command as the job's worker."""
    if not command:
        raise RemoteError("no command to run")
    for name in env:
        if not _ENV_NAME.fullmatch(name):
            raise RemoteError(f"environment name {name!r} is not plain")
    assignments = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in sorted(env.items())
    )
    argv = " ".join(shlex.quote(part) for part in command)
    directory = shlex.quote(target.job_dir)
    pid_file = shlex.quote(PID_FILE)
    return (
        f"d={directory}; "
        f'mkdir -p -- "$d" && cd -- "$d" || exit {START_REFUSED}; '
        f"echo $$ > {pid_file} || exit {START_REFUSED}; "
        f"env {assignments} ORCH_JOB_WORKDIR=\"$d\" {argv}; "
        "s=$?; "
        f"rm -f -- {pid_file}; "
        'exit "$s"'
    )


def check_script(target: RemoteTarget) -> str:
    """Exit 0 when the job's worker is gone, 1 while its process group runs."""
    pid_file = shlex.quote(f"{target.job_dir}/{PID_FILE}")
    return (
        f"f={pid_file}; "
        '[ -f "$f" ] || exit 0; '
        'p=$(cat -- "$f") || exit 3; '
        'case "$p" in ""|*[!0-9]*) exit 3;; esac; '
        'kill -0 -"$p" 2>/dev/null && exit 1; '
        # Fail closed: only a shell whose kill -0 works can say "gone".
        'kill -0 "$$" 2>/dev/null || exit 3; '
        "exit 0"
    )


def stop_script(target: RemoteTarget) -> str:
    """SIGTERM the job's worker process group, if it still runs."""
    pid_file = shlex.quote(f"{target.job_dir}/{PID_FILE}")
    return (
        f"f={pid_file}; "
        '[ -f "$f" ] || exit 0; '
        'p=$(cat -- "$f") || exit 3; '
        'case "$p" in ""|*[!0-9]*) exit 3;; esac; '
        'kill -TERM -"$p" 2>/dev/null; '
        "exit 0"
    )


class Transport(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float) -> int: ...


class SshTransport:
    """The real transport for checks: the `ssh` on PATH, argv only, never a shell.

    The start itself is spawned by `orch job run` (lib/job_run.py) with the
    argv from `ssh_argv`, so it is heartbeated and gets forwarded signals.
    """

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return SSH_UNREACHABLE
        except OSError:
            return SSH_UNREACHABLE
        return completed.returncode


def check_worker(transport: Transport, target: RemoteTarget) -> str:
    """GONE (0), RUNNING (1), UNREACHABLE (ssh's 255) or UNKNOWN (anything else).

    Only GONE lets a slot go. UNKNOWN still proves the device answered.
    """
    code = transport.run(ssh_argv(target, check_script(target)), timeout=PROBE_TIMEOUT_SECONDS)
    if code == 0:
        return GONE
    if code == 1:
        return RUNNING
    if code == SSH_UNREACHABLE:
        return UNREACHABLE
    return UNKNOWN


def stop_worker(transport: Transport, target: RemoteTarget) -> bool:
    """Ask the device to SIGTERM the worker. True when the device answered."""
    code = transport.run(ssh_argv(target, stop_script(target)), timeout=PROBE_TIMEOUT_SECONDS)
    return code == 0
