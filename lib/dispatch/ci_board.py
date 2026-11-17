from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lib import job_detail
from lib.dispatch.types import CHECK_FAILED, CheckRow, RunJob, TrackedTicket

RETAIN_SECONDS = 24 * 60 * 60
RUN_READS = 3
LOG_READS = 1

_FAILED = "conclusion IN ({})".format(", ".join(f"'{conclusion}'" for conclusion in CHECK_FAILED))
_NOT_FAILED = f"NOT COALESCE({_FAILED}, 0)"
# A rerun at the same head is a new check run (a new job id) under the same
# name: the lookups made for the old attempt no longer hold.
_SAME_JOB = "ci_jobs.job_id IS excluded.job_id"
_UPSERT = f"""
    INSERT INTO ci_jobs (
        repo, ticket_id, head, check_name, pr, run_id, job_id, log_url,
        status, conclusion, started_at, completed_at, seen_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (repo, ticket_id, head, check_name) DO UPDATE SET
        pr = excluded.pr,
        run_id = excluded.run_id,
        job_id = excluded.job_id,
        log_url = excluded.log_url,
        status = excluded.status,
        conclusion = excluded.conclusion,
        completed_at = excluded.completed_at,
        seen_at = excluded.seen_at,
        started_at = CASE WHEN {_SAME_JOB}
            THEN COALESCE(excluded.started_at, ci_jobs.started_at)
            ELSE excluded.started_at END,
        created_at = CASE WHEN {_SAME_JOB} THEN ci_jobs.created_at END,
        runner_name = CASE WHEN {_SAME_JOB} THEN ci_jobs.runner_name END,
        runner_kind = CASE WHEN {_SAME_JOB} THEN ci_jobs.runner_kind END,
        failing_tests = CASE WHEN {_SAME_JOB} THEN ci_jobs.failing_tests END
"""


@dataclass(frozen=True)
class FailedRun:
    repo: str
    ticket_id: str
    head: str
    run_id: str
    checks: tuple[str, ...]


def record(
    connect: Callable[[], sqlite3.Connection],
    tickets: Sequence[TrackedTicket],
    now: float,
    *,
    run_jobs: Callable[[str, str], Sequence[RunJob]] | None,
    ci_logs: Callable[[str, str], Mapping[str, str]] | None,
    log: Callable[[str], None] | None,
) -> None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for ticket in tickets:
            connection.executemany(_UPSERT, _rows(ticket, now))
        _prune(connection, tickets, now)
        connection.commit()
    if run_jobs is not None:
        _read_runners(connect, run_jobs, log)
    if ci_logs is not None:
        _read_failures(connect, ci_logs, log)


def _rows(ticket: TrackedTicket, now: float) -> list[tuple[object, ...]]:
    result = ticket.ci.get(ticket.current_head) if ticket.current_head else None
    if result is None:
        return []
    return [
        (
            ticket.repo.lower(),
            ticket.ticket_id,
            ticket.current_head,
            check.name,
            ticket.pr_number,
            check.run_id,
            check.job_id,
            check.log_url,
            check.status,
            check.conclusion,
            check.started_at,
            check.completed_at,
            now,
        )
        for check in _one_per_name(result.checks)
    ]


def _one_per_name(checks: Sequence[CheckRow]) -> list[CheckRow]:
    kept: dict[str, CheckRow] = {}
    for check in checks:
        held = kept.get(check.name)
        if held is None or _rank(check) > _rank(held):
            kept[check.name] = check
    return list(kept.values())


def _rank(check: CheckRow) -> tuple[bool, float, str]:
    return (check.conclusion in CHECK_FAILED, check.started_at or 0.0, check.job_id or "")


def _prune(
    connection: sqlite3.Connection, tickets: Sequence[TrackedTicket], now: float
) -> None:
    connection.execute("DELETE FROM ci_jobs WHERE seen_at < ?", (now - RETAIN_SECONDS,))
    connection.executemany(
        f"""
        DELETE FROM ci_jobs
        WHERE repo = ? AND ticket_id = ? AND head IS NOT ? AND {_NOT_FAILED}
        """,
        [(ticket.repo.lower(), ticket.ticket_id, ticket.current_head) for ticket in tickets],
    )
    connection.execute(
        f"""
        DELETE FROM ci_jobs
        WHERE {_NOT_FAILED} AND NOT EXISTS (
            SELECT 1 FROM tracked_tickets AS tracked
            LEFT JOIN ticket_state AS state
              ON state.repo = tracked.repo AND state.ticket_id = tracked.ticket_id
            WHERE tracked.repo = ci_jobs.repo AND tracked.ticket_id = ci_jobs.ticket_id
              AND COALESCE(state.workflow_state, '') NOT IN ('merged', 'done', 'abandoned')
        )
        """
    )


def _read_runners(
    connect: Callable[[], sqlite3.Connection],
    run_jobs: Callable[[str, str], Sequence[RunJob]],
    log: Callable[[str], None] | None,
) -> None:
    with connect() as connection:
        runs = connection.execute(
            """
            SELECT repo, run_id FROM ci_jobs
            WHERE run_id IS NOT NULL AND job_id IS NOT NULL
              AND status != 'queued' AND runner_kind IS NULL
            GROUP BY repo, run_id
            ORDER BY MAX(COALESCE(started_at, seen_at)) DESC, repo, run_id
            LIMIT ?
            """,
            (RUN_READS,),
        ).fetchall()
    for repo, run_id in runs:
        try:
            jobs = run_jobs(repo, run_id)
        except Exception as exc:
            _say(log, f"ci board: {repo} run {run_id} jobs unavailable: {type(exc).__name__}")
            continue
        with connect() as connection:
            connection.executemany(
                """
                UPDATE ci_jobs SET
                    created_at = COALESCE(?, created_at),
                    started_at = COALESCE(started_at, ?),
                    runner_name = COALESCE(?, runner_name),
                    runner_kind = COALESCE(?, runner_kind)
                WHERE repo = ? AND run_id = ? AND job_id = ?
                """,
                [
                    (
                        job.created_at,
                        job.started_at,
                        job.runner_name,
                        job.runner_kind,
                        repo,
                        run_id,
                        job.job_id,
                    )
                    for job in jobs
                ],
            )


def _read_failures(
    connect: Callable[[], sqlite3.Connection],
    ci_logs: Callable[[str, str], Mapping[str, str]],
    log: Callable[[str], None] | None,
) -> None:
    """gh run view --log-failed answers only once the whole run is finished."""
    from lib.evidence.ci import failing_tests

    reads = 0
    with connect() as connection:
        runs = _failed_runs(connection)
    for run in runs:
        with connect() as connection:
            logs = _evidence_logs(connection, run)
        if not set(run.checks) <= logs.keys():
            if reads == LOG_READS:
                continue
            reads += 1
            try:
                logs = ci_logs(run.repo, run.run_id)
            except Exception as exc:
                _say(
                    log,
                    f"ci board: {run.repo} run {run.run_id} log unavailable: {type(exc).__name__}",
                )
                continue
        with connect() as connection:
            connection.executemany(
                """
                UPDATE ci_jobs SET failing_tests = ?
                WHERE repo = ? AND ticket_id = ? AND head = ? AND run_id = ?
                  AND check_name = ? AND failing_tests IS NULL
                """,
                [
                    (
                        json.dumps(failing_tests(logs.get(name) or "")),
                        run.repo,
                        run.ticket_id,
                        run.head,
                        run.run_id,
                        name,
                    )
                    for name in run.checks
                ],
            )


def _failed_runs(connection: sqlite3.Connection) -> list[FailedRun]:
    rows = connection.execute(
        f"""
        SELECT repo, ticket_id, head, run_id,
               json_group_array(check_name) FILTER (
                   WHERE {_FAILED} AND failing_tests IS NULL
               ) AS checks
        FROM ci_jobs
        WHERE run_id IS NOT NULL
        GROUP BY repo, ticket_id, head, run_id
        HAVING SUM({_FAILED} AND failing_tests IS NULL) > 0
           AND SUM(status != 'completed') = 0
        ORDER BY MAX(completed_at) DESC, repo, ticket_id, run_id
        """
    ).fetchall()
    return [
        FailedRun(
            repo=row["repo"],
            ticket_id=row["ticket_id"],
            head=row["head"],
            run_id=row["run_id"],
            checks=tuple(json.loads(row["checks"])),
        )
        for row in rows
    ]


def _evidence_logs(connection: sqlite3.Connection, run: FailedRun) -> dict[str, str]:
    logs: dict[str, str] = {}
    for row in connection.execute(
        """
        SELECT id, detail FROM jobs
        WHERE lower(repo) = ? AND ticket_id = ? AND action = 'fix' AND head = ?
        ORDER BY created_at DESC, rowid DESC
        """,
        (run.repo, run.ticket_id, run.head),
    ):
        detail = json.loads(job_detail.load_row(connection, str(row["id"]), row["detail"]) or "{}")
        ci = (detail.get("evidence") or {}).get("ci") or {}
        for job in ci.get("jobs") or ():
            if job.get("run_id") == run.run_id and isinstance(job.get("log_tail"), str):
                logs.setdefault(str(job.get("check")), job["log_tail"])
    return logs


def _say(log: Callable[[str], None] | None, line: str) -> None:
    if log is not None:
        log(line)
