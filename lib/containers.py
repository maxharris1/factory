from __future__ import annotations

import shlex
import subprocess
from collections import Counter
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
FINISHED_STATES = frozenset({"done", "failed", "cancelled"})

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
    failure = _failed(runner(remove_script(job_id)))
    if failure is None:
        return None
    return f"could not remove job {job_id}'s {LABEL} containers on {device} ({failure})"


def sweep(runner: Runner, job_state: Callable[[str], str | None], device: str) -> list[str]:
    listed = runner(list_script())
    if (failure := _failed(listed)) is not None:
        return [f"could not list {LABEL} containers on {device} ({failure})"]
    by_job = Counter(
        (line.split(maxsplit=1)[1:] or [""])[0].strip() for line in listed.stdout.splitlines() if line.strip()
    )
    lines = []
    for job_id, count in sorted(by_job.items()):
        if not _JOB_ID.fullmatch(job_id):
            lines.append(f"kept {count} container(s) on {device} labelled {LABEL}={job_id!r}: not a job id")
            continue
        state = job_state(job_id)
        if state is None:
            lines.append(f"kept {count} container(s) on {device} of job {job_id}: not in orch.db")
        elif state in FINISHED_STATES:
            lines.append(
                remove(job_id, runner, device)
                or f"removed {count} container(s) on {device} of finished job {job_id}"
            )
    return lines
