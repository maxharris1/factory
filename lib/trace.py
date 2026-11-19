"""Every job attempt on one ticket and how the dispatcher judged its handoff (issue #1016).

`orch trace` reads jobs, job_events and device_slots, plus the dispatcher's
handled_comments, handled_times and comment_links, all from orch.db. A table a
young database lacks reads as empty. The database is opened with mode=ro and
nothing calls GitHub or starts a subprocess, so it is safe beside a live poller.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from lib.device_pool import SLOTS_TABLE
from lib.dispatch.helpers import _REGISTERING_ACTIONS
from lib.job_state import RUN_DIED, RUN_EXITED
from lib.queue import PRE_READY_FAILED, PRE_READY_PASSED, SUBMITTED_INVALID

HANDOFF_EVENTS = (SUBMITTED_INVALID, PRE_READY_PASSED, PRE_READY_FAILED)
UNREGISTERED = "rejected-unregistered-"

_HANDOFF = re.compile(r"handoff (?P<comment>\S+): (?P<reason>.*)", re.S)
_HOST = re.compile(r" on (?P<host>[^\s:]+)")
_EXIT_CODE = re.compile(r"\bexit (?P<code>-?\d+)")


@dataclass(frozen=True)
class Attempt:
    job_id: str
    action: str
    recipient: str
    attempt: int
    state: str
    created_at: float
    host: str | None
    exit: str | None
    exit_code: int | None
    comment_id: str | None
    comment_url: str | None
    linked_by: str | None
    result: str
    reason: str


def attempts(db: Path, repo: str, ticket_id: str) -> list[Attempt]:
    uri = db.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

        def rows(table: str, sql: str) -> list[sqlite3.Row]:
            if table not in tables:
                return []
            return connection.execute(sql, (repo.lower(), ticket_id)).fetchall()

        on_ticket = "lower(repo) = ? AND ticket_id = ?"
        jobs = rows(
            "jobs",
            "SELECT id, action, recipient, attempt, state, created_at, handoff_comment_id "
            f"FROM jobs WHERE {on_ticket} ORDER BY created_at, id",
        )
        if not jobs:
            return []
        ticket_jobs = f"job_id IN (SELECT id FROM jobs WHERE {on_ticket})"
        events = rows(
            "job_events",
            f"SELECT job_id, event, detail FROM job_events WHERE {ticket_jobs} "
            "ORDER BY recorded_at, id",
        )
        slots = dict(
            rows(SLOTS_TABLE, f"SELECT job_id, device FROM {SLOTS_TABLE} WHERE {ticket_jobs}")
        )

        def by_comment(table: str, column: str) -> dict[str, object]:
            return dict(rows(table, f"SELECT comment_id, {column} FROM {table} WHERE {on_ticket}"))

        outcomes = by_comment("handled_comments", "outcome")
        times = by_comment("handled_times", "handled_at")
        links = by_comment("comment_links", "link")

    ended: dict[str, str] = {}
    handed: dict[str, str] = {}
    invalid: dict[tuple[str, str], str] = {}
    for job_id, event, detail in events:
        if event in (RUN_EXITED, RUN_DIED):
            ended[job_id] = detail
        handoff = _HANDOFF.fullmatch(detail) if event in HANDOFF_EVENTS else None
        if handoff is not None:
            handed[job_id] = handoff["comment"]
            if event == SUBMITTED_INVALID:
                invalid[(job_id, handoff["comment"])] = handoff["reason"]

    linked: dict[str, tuple[str, str]] = {}
    for job in jobs:
        if job["handoff_comment_id"] is not None:
            linked[job["id"]] = (str(job["handoff_comment_id"]), "job")
        elif job["id"] in handed:
            linked[job["id"]] = (handed[job["id"]], "event")
    claimed = {comment for comment, _ in linked.values()}
    unregistered = sorted(
        (times[comment], comment)
        for comment, outcome in outcomes.items()
        if outcome.startswith(UNREGISTERED) and comment not in claimed and comment in times
    )
    for index, job in enumerate(jobs):
        if job["id"] in linked:
            continue
        kinds = {kind for kind, actions in _REGISTERING_ACTIONS.items() if job["action"] in actions}
        same = [later for later in jobs[index + 1 :] if later["action"] == job["action"]]
        end = same[0]["created_at"] if same else None
        found = next(
            (
                (at, comment)
                for at, comment in unregistered
                if outcomes[comment].removeprefix(UNREGISTERED) in kinds
                and job["created_at"] <= at
                and (end is None or at < end)
            ),
            None,
        )
        if found is not None:
            unregistered.remove(found)
            linked[job["id"]] = (found[1], "time")

    result = []
    for job in jobs:
        job_id = job["id"]
        detail = ended.get(job_id, "")
        host = _HOST.search(detail)
        ran = (detail.partition(": ")[2] or detail) or None
        code = _EXIT_CODE.search(detail)
        comment, linked_by = linked.get(job_id, (None, None))
        verdict, reason = _verdict(
            outcomes.get(comment), invalid.get((job_id, comment)), comment, job["state"]
        )
        result.append(
            Attempt(
                job_id=job_id,
                action=job["action"],
                recipient=job["recipient"],
                attempt=job["attempt"],
                state=job["state"],
                created_at=job["created_at"],
                host=host["host"] if host else slots.get(job_id),
                exit=ran,
                exit_code=int(code["code"]) if code else None,
                comment_id=comment,
                comment_url=links.get(comment),
                linked_by=linked_by,
                result=verdict,
                reason=reason,
            )
        )
    return result


def _verdict(
    outcome: str | None, invalid: str | None, comment: str | None, state: str
) -> tuple[str, str]:
    if outcome is not None:
        refused = outcome.startswith(("rejected-", "refused-"))
        return ("rejected" if refused else "accepted"), outcome
    if invalid is not None:
        return "rejected", f"{SUBMITTED_INVALID}: {invalid}"
    if comment is not None:
        return "pending", "not-yet-handled"
    if state in ("queued", "running"):
        return state, state
    return "none", "no-handoff"
