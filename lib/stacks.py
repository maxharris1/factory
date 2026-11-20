from __future__ import annotations

import json
import posixpath
import re
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from lib.containers import _CONTAINER_ID, FINISHED_STATES, LABEL
from lib.device_pool import QA_SLOTS_TABLE, _has_table
from lib.queue import SLOTS_TABLE
from lib.remote import _JOB_ID

KINDS = {"compose": "com.docker.compose.project", "supabase": "com.supabase.cli.project"}
WORKING_DIR = "com.docker.compose.project.working_dir"

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_CREATED = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]{1,9})? ([+-][0-9]{4})(?: |$)"
)

Ticket = tuple[str, str]


@dataclass(frozen=True)
class Container:
    id: str
    created: float
    working_dir: str
    labelled: bool


@dataclass(frozen=True)
class Project:
    kind: str
    name: str
    containers: tuple[Container, ...]
    complete: bool = True


@dataclass(frozen=True)
class TicketFacts:
    all_finished: bool
    last_finished_at: float | None
    held_on: frozenset[str]
    live: bool


@dataclass(frozen=True)
class Ledger:
    tickets: Mapping[Ticket, TicketFacts]
    job_ticket: Mapping[str, Ticket]


@dataclass(frozen=True)
class Keep:
    reason: str


@dataclass(frozen=True)
class Remove:
    owner: Ticket


Verdict = Keep | Remove


def stack_name(repo: str, ticket: str, device: str) -> str:
    parts = (repo.rsplit("/", 1)[-1], ticket, device)
    return "-".join(re.sub(r"[^a-z0-9]", "", part.lower()) for part in parts)


def listing_format(kind: str) -> str:
    labels = (KINDS[kind], WORKING_DIR, LABEL)
    fields = ["{{json .ID}}", *(f'{{{{json (.Label "{label}")}}}}' for label in labels), "{{json .CreatedAt}}"]
    return "\t".join(fields)


def _created(value: str) -> float | None:
    match = _CREATED.match(value)
    if match is None:
        return None
    seconds, fraction, offset = match.groups()
    try:
        at = datetime.strptime(f"{seconds} {offset}", "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None
    return at.timestamp() + float(fraction or 0)


def _fields(line: str) -> list[str] | None:
    try:
        fields = [json.loads(field) for field in line.split("\t")]
    except ValueError:
        return None
    if len(fields) != 5 or not all(isinstance(field, str) for field in fields):
        return None
    return fields


def parse_listing(kind: str, stdout: str) -> tuple[list[Project], int]:
    by_name: dict[str, list[Container]] = {}
    incomplete: set[str] = set()
    unreadable = 0
    every_project_incomplete = False
    for line in filter(str.strip, stdout.split("\n")):
        fields = _fields(line)
        if fields is None or not fields[1]:
            unreadable += 1
            every_project_incomplete = True
            continue
        container_id, name, working_dir, job, created_at = fields
        found = by_name.setdefault(name, [])
        created = _created(created_at)
        if created is None or not _CONTAINER_ID.fullmatch(container_id):
            unreadable += 1
            incomplete.add(name)
            continue
        found.append(Container(container_id, created, working_dir, bool(job)))
    return [
        Project(kind, name, tuple(found), not every_project_incomplete and name not in incomplete)
        for name, found in sorted(by_name.items())
    ], unreadable


def _parts(path: str) -> tuple[str, ...] | None:
    if not path.startswith("/") or ".." in path.split("/"):
        return None
    return tuple(part for part in posixpath.normpath(path).split("/") if part)


def job_of(working_dir: str, roots: Collection[str]) -> str | None:
    parts = _parts(working_dir)
    if parts is None:
        return None
    for root in roots:
        base = _parts(root)
        if base is None or parts[: len(base)] != base or len(parts) < len(base) + 2:
            continue
        jobs, job_id = parts[len(base) : len(base) + 2]
        if jobs == "jobs" and _JOB_ID.fullmatch(job_id):
            return job_id
    return None


def read_ledger(connection: sqlite3.Connection, live: Collection[str]) -> Ledger:
    finished = sorted(FINISHED_STATES)
    marks = ", ".join("?" for _ in finished)
    groups = connection.execute(
        f"SELECT repo, ticket_id, MIN(state IN ({marks})), MAX(finished_at) FROM jobs"
        " GROUP BY repo, ticket_id",
        finished,
    ).fetchall()
    job_ticket = {
        str(job_id): (str(repo), str(ticket_id))
        for job_id, repo, ticket_id in connection.execute("SELECT id, repo, ticket_id FROM jobs")
    }
    held: dict[Ticket, set[str]] = {}
    for table in (SLOTS_TABLE, QA_SLOTS_TABLE):
        if _has_table(connection, table):
            rows = connection.execute(
                f"SELECT jobs.repo, jobs.ticket_id, slots.device FROM {table} AS slots"
                " JOIN jobs ON jobs.id = slots.job_id"
            )
            for repo, ticket_id, device in rows:
                held.setdefault((str(repo), str(ticket_id)), set()).add(str(device))
    running = {job_ticket[job_id] for job_id in live if job_id in job_ticket}
    tickets = {}
    for repo, ticket_id, all_finished, last in groups:
        ticket = (str(repo), str(ticket_id))
        tickets[ticket] = TicketFacts(
            bool(all_finished),
            None if last is None else float(last),
            frozenset(held.get(ticket, ())),
            ticket in running,
        )
    return Ledger(tickets, job_ticket)


def decide(project: Project, ledger: Ledger, device: str, roots: Collection[str]) -> Verdict:
    if not project.complete or not project.containers:
        return Keep("a container could not be read")
    if any(container.labelled for container in project.containers):
        return Keep(f"a container is labelled {LABEL}")
    if not _NAME.fullmatch(project.name):
        return Keep("not a compose project name")
    owners = {
        ledger.job_ticket[job_id]
        for container in project.containers
        if (job_id := job_of(container.working_dir, roots)) in ledger.job_ticket
    }
    owners |= {ticket for ticket in ledger.tickets if stack_name(*ticket, device) == project.name}
    if len(owners) != 1:
        return Keep(f"claimed by {len(owners)} tickets" if owners else "no ticket owns it")
    (owner,) = owners
    facts = ledger.tickets[owner]
    ticket = owner[1]
    if not facts.all_finished:
        return Keep(f"{ticket} has an unfinished job")
    if facts.live:
        return Keep(f"{ticket} has a live run")
    if device in facts.held_on:
        return Keep(f"{ticket} holds a slot on {device}")
    if facts.last_finished_at is None:
        return Keep(f"{ticket} has no finish time")
    if max(container.created for container in project.containers) > facts.last_finished_at:
        return Keep(f"newer than {ticket}'s last finish")
    return Remove(owner)
