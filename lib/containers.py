"""Remove the containers a job labelled `factory.job=<job id>` (#901).

Every brief asks the agent to label each container or compose stack it
starts with `factory.job=$ORCH_JOB_ID`. The agent's own cleanup can be
killed with it, so the factory removes them too: `orch job run`'s teardown
removes the job's containers on the device it ran on, and the waiter's sweep
removes those of finished jobs whose teardown never ran.

Each call runs one POSIX sh script on one device: `sh -c` on the box, or
`bash -lc` over the job's ssh login on a remote device, whose login PATH
finds `docker` (Docker Desktop on macOS is per user). A device without
`docker` has nothing to remove. Nothing here raises: a failure comes back as
one log line, so a teardown or a waiter step always finishes.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable

from lib.remote import (
    _JOB_ID,
    SSH_UNREACHABLE,
    CaptureTransport,
    Called,
    RemoteTarget,
    _login,
    ssh_argv,
)

LABEL = "factory.job"
TIMEOUT_SECONDS = 60.0

Runner = Callable[[str], Called]

_NO_DOCKER = "command -v docker >/dev/null 2>&1 || exit 0\n"


def remove_script(job_id: str) -> str:
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError(f"job id {job_id!r} is not a plain token")
    label = shlex.quote(f"label={LABEL}={job_id}")
    return (
        _NO_DOCKER
        + f"ids=$(docker ps -aq --filter {label}) || exit\n"
        + '[ -z "$ids" ] || docker rm -f -v $ids >/dev/null\n'
    )


def list_script() -> str:
    """Print `<container id> <label value>` for each container with the label."""
    template = shlex.quote(f'{{{{.ID}}}} {{{{.Label "{LABEL}"}}}}')
    return _NO_DOCKER + f"docker ps -a --filter label={LABEL} --format {template}\n"


def local_runner(timeout: float = TIMEOUT_SECONDS) -> Runner:
    def run(script: str) -> Called:
        try:
            done = subprocess.run(
                ["sh", "-c", script],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return Called(124, "", f"timed out after {timeout:g}s")
        except OSError as exc:
            return Called(127, "", str(exc))
        return Called(done.returncode, done.stdout, done.stderr)

    return run


def remote_runner(
    transport: CaptureTransport, target: RemoteTarget, timeout: float = TIMEOUT_SECONDS
) -> Runner:
    def run(script: str) -> Called:
        try:
            return transport.call(
                ssh_argv(target, _login(script, "orch-containers", ())), timeout=timeout
            )
        except OSError as exc:
            return Called(SSH_UNREACHABLE, "", str(exc))

    return run


def _failed(result: Called) -> str | None:
    if result.code == 0:
        return None
    reason = "unreachable over ssh" if result.code == SSH_UNREACHABLE else f"exit {result.code}"
    detail = " ".join(result.stderr.split())[:200]
    return f"{reason}: {detail}" if detail else reason


def remove(job_id: str, runner: Runner, device: str) -> str | None:
    """Remove job_id's labelled containers. None when done, else a log line."""
    failure = _failed(runner(remove_script(job_id)))
    if failure is None:
        return None
    return f"could not remove job {job_id}'s {LABEL} containers on {device} ({failure})"


def sweep(runner: Runner, finished: Callable[[str], bool | None], device: str) -> list[str]:
    """Remove the labelled containers of finished jobs. Returns log lines.

    finished(job_id) is True for a finished job, False for one that is
    queued or running, and None for an id orch.db does not know. Only a
    finished job's containers are removed. An unknown id, or a label value
    that is not a job id, keeps its containers and gets a log line.
    """
    listed = runner(list_script())
    if (failure := _failed(listed)) is not None:
        return [f"could not list {LABEL} containers on {device} ({failure})"]
    by_job: dict[str, int] = {}
    for line in listed.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if parts:
            value = parts[1].strip() if len(parts) > 1 else ""
            by_job[value] = by_job.get(value, 0) + 1
    lines = []
    for job_id, count in sorted(by_job.items()):
        if not _JOB_ID.fullmatch(job_id):
            lines.append(f"kept {count} container(s) on {device} labelled {LABEL}={job_id!r}: not a job id")
            continue
        state = finished(job_id)
        if state is None:
            lines.append(f"kept {count} container(s) on {device} of job {job_id}: not in orch.db")
        elif state:
            lines.append(
                remove(job_id, runner, device)
                or f"removed {count} container(s) on {device} of finished job {job_id}"
            )
    return lines
