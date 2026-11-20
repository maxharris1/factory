from __future__ import annotations

import posixpath
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

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
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")


def remove_script(job_id: str) -> str:
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError(f"job id {job_id!r} is not a plain token")
    label = shlex.quote(f"label={LABEL}={job_id}")
    return (
        _NO_DOCKER
        + f"ids=$(docker ps -aq --filter {label}) || exit\n"
        + '[ -z "$ids" ] || docker rm -f -v $ids >/dev/null\n'
    )


def remove_ids_script(ids: Sequence[str]) -> str:
    return _NO_DOCKER + f"docker rm -f -v {' '.join(map(shlex.quote, ids))} >/dev/null\n"


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


def _by_job(listing: str) -> tuple[dict[str, list[str]], int]:
    by_job: dict[str, list[str]] = {}
    unreadable = 0
    for line in filter(str.strip, listing.splitlines()):
        container, _, job_id = line.strip().partition(" ")
        if _CONTAINER_ID.fullmatch(container):
            by_job.setdefault(job_id.strip(), []).append(container)
        else:
            unreadable += 1
    return by_job, unreadable


def sweep(runner: Runner, job_state: Callable[[str], str | None], device: str) -> list[str]:
    listed = runner(list_script())
    if (failure := _failed(listed)) is not None:
        return [f"could not list {LABEL} containers on {device} ({failure})"]
    by_job, unreadable = _by_job(listed.stdout)
    lines = [f"kept {unreadable} {LABEL} container(s) on {device}: not a container id"] if unreadable else []
    for job_id, ids in sorted(by_job.items()):
        count = len(ids)
        if not _JOB_ID.fullmatch(job_id):
            lines.append(f"kept {count} container(s) on {device} labelled {LABEL}={job_id!r}: not a job id")
            continue
        state = job_state(job_id)
        if state is None:
            lines.append(f"kept {count} container(s) on {device} of job {job_id}: not in orch.db")
        elif state in FINISHED_STATES:
            failure = _failed(runner(remove_ids_script(ids)))
            lines.append(
                f"could not remove {count} container(s) on {device} of finished job {job_id} ({failure})"
                if failure is not None
                else f"removed {count} container(s) on {device} of finished job {job_id}"
            )
    return lines


KINDS = {"compose": "com.docker.compose.project", "supabase": "com.supabase.cli.project"}
_COMPOSE_WORKDIR = "com.docker.compose.project.working_dir"
_SUPABASE_WORKDIR = "com.supabase.cli.workdir"
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_FIELDS = (LABEL, *KINDS.values(), _COMPOSE_WORKDIR)


@dataclass(frozen=True)
class Stack:
    kind: str
    project: str
    working_dirs: frozenset[str]
    ids: tuple[str, ...]
    newest: float


@dataclass(frozen=True)
class Ticket:
    repo: str
    ticket_id: str
    all_finished: bool
    last_finish: float | None
    held_on: frozenset[str]
    live: bool


@dataclass(frozen=True)
class Ledger:
    tickets: Mapping[tuple[str, str], Ticket]
    job_ticket: Mapping[str, tuple[str, str]]


def stack_name(repo: str, ticket_id: str, device: str) -> str:
    ticket = "".join(c for c in ticket_id.lower() if c.isascii() and c.isalnum())
    return f"{repo.rsplit('/', 1)[-1].lower()}-{ticket}-{device}"


def stacks_list_script() -> str:
    fields = ["{{.ID}}", *(f'{{{{.Label "{key}"}}}}' for key in _FIELDS),
              "{{.CreatedAt}}", f'{{{{.Label "{_SUPABASE_WORKDIR}"}}}}']
    return _NO_DOCKER + f"docker ps -a --format {shlex.quote(chr(9).join(fields))}\n"


def _created(text: str) -> float | None:
    try:
        return datetime.strptime(" ".join(text.split(" ")[:3]), "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def _stacks(listing: str) -> tuple[list[Stack], int]:
    rows: dict[tuple[str, str], list[tuple[str, str, float]]] = {}
    unreadable = 0
    for line in filter(None, listing.split("\n")):
        fields = line.split("\t")
        if len(fields) != 7:
            unreadable += 1
            continue
        container, job, compose, supabase, compose_wd, created_at, supabase_wd = fields
        if job or not (compose or supabase):
            continue
        kind, project = ("compose", compose) if compose else ("supabase", supabase)
        created = _created(created_at)
        if not _CONTAINER_ID.fullmatch(container) or not _PROJECT.fullmatch(project) or created is None:
            unreadable += 1
            continue
        rows.setdefault((kind, project), []).append((container, compose_wd or supabase_wd, created))
    stacks = [
        Stack(kind, project, frozenset(wd for _, wd, _ in members if wd),
              tuple(c for c, _, _ in members), max(t for _, _, t in members))
        for (kind, project), members in sorted(rows.items())
    ]
    return stacks, unreadable


def owner(stack: Stack, device: str, ledger: Ledger, job_roots: Sequence[str]) -> Ticket | str:
    by_dir: set[tuple[str, str]] = set()
    prefixes = [root.rstrip("/") + "/jobs/" for root in job_roots]
    for working_dir in sorted(stack.working_dirs):
        path = posixpath.normpath(working_dir)
        for prefix in prefixes:
            if not path.startswith(prefix):
                continue
            job_id = path[len(prefix):].split("/", 1)[0]
            if not _JOB_ID.fullmatch(job_id):
                continue
            if job_id not in ledger.job_ticket:
                return f"working_dir {working_dir} is under job {job_id}, not in orch.db"
            by_dir.add(ledger.job_ticket[job_id])
    if len(by_dir) > 1:
        return "conflicting owners: its working_dirs are under jobs of different tickets"
    by_name = {key for key, t in ledger.tickets.items()
               if stack_name(t.repo, t.ticket_id, device) == stack.project}
    if len(by_name) > 1:
        return "ambiguous owner: its name matches more than one ticket"
    if by_dir and by_name and by_dir != by_name:
        return "conflicting owners: its working_dir and its name name different tickets"
    tied = by_dir or by_name
    if not tied:
        return f"no ticket's job directory or stack name on {device}"
    return ledger.tickets[tied.pop()]


def removable(stack: Stack, ticket: Ticket, device: str) -> str | None:
    if not ticket.all_finished:
        return "not every job of the ticket is finished"
    if device in ticket.held_on:
        return f"a job of the ticket holds a slot on {device}"
    if ticket.live:
        return "a run of the ticket is live in this waiter"
    if ticket.last_finish is None:
        return "a finished job of the ticket has no finish time"
    if stack.newest > ticket.last_finish:
        return "a container is newer than the ticket's last finish"
    return None


def remove_stack_script(stack: Stack) -> str:
    lines = [
        f"docker rm -f -v {' '.join(map(shlex.quote, stack.ids))} >/dev/null || exit",
        "status=0",
    ]
    for kind_label in KINDS.values():
        label = shlex.quote(f"label={kind_label}={stack.project}")
        for noun in ("network", "volume"):
            lines += [
                f"found=$(docker {noun} ls -q --filter {label}) || status=1",
                f'[ -z "$found" ] || docker {noun} rm $found >/dev/null || status=1',
            ]
    return _NO_DOCKER + "\n".join([*lines, "exit $status"]) + "\n"


def sweep_stacks(runner: Runner, ledger: Ledger, device: str, job_roots: Sequence[str]) -> list[str]:
    listed = runner(stacks_list_script())
    if (failure := _failed(listed)) is not None:
        return [f"could not list compose/supabase containers on {device} ({failure})"]
    stacks, unreadable = _stacks(listed.stdout)
    lines = [f"kept {unreadable} compose/supabase container(s) on {device}: unreadable row"] if unreadable else []
    for stack in stacks:
        name = f"{stack.kind} stack {stack.project} on {device}"
        ticket = owner(stack, device, ledger, job_roots)
        reason = ticket if isinstance(ticket, str) else removable(stack, ticket, device)
        if reason is not None or isinstance(ticket, str):
            lines.append(f"kept {name}: {reason}")
        elif (failure := _failed(runner(remove_stack_script(stack)))) is not None:
            lines.append(f"could not remove {name} ({failure})")
        else:
            lines.append(
                f"removed {name} ({len(stack.ids)} container(s), its networks and volumes)"
                f" of finished ticket {ticket.repo}#{ticket.ticket_id}"
            )
    return lines
