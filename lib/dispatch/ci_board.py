from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence

from lib.dispatch.types import CHECK_FAILED, CheckRow, PoolRunner, RunJob, TrackedTicket

RETAIN_SECONDS = 24 * 60 * 60
RUN_READS = 3
LOG_READS = 1

_FAILED = "conclusion IN ({})".format(", ".join(f"'{conclusion}'" for conclusion in CHECK_FAILED))
_NOT_FAILED = f"NOT COALESCE({_FAILED}, 0)"
_NOT_SEEN_THIS_CYCLE = "seen_at < ?"
# A rerun at the same head is a new check run (a new job id) under the same
# name: the lookups made for the old attempt no longer hold.
_SAME_JOB = "ci_jobs.job_id IS excluded.job_id"
_UPSERT = f"""
    INSERT INTO ci_jobs (
        repo, ticket_id, head, workflow, check_name, pr, run_id, job_id, log_url,
        status, conclusion, started_at, completed_at, seen_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (repo, ticket_id, head, workflow, check_name) DO UPDATE SET
        pr = excluded.pr,
        run_id = excluded.run_id,
        job_id = excluded.job_id,
        log_url = excluded.log_url,
        status = excluded.status,
        conclusion = excluded.conclusion,
        seen_at = excluded.seen_at,
        started_at = CASE WHEN {_SAME_JOB}
            THEN COALESCE(excluded.started_at, ci_jobs.started_at)
            ELSE excluded.started_at END,
        completed_at = CASE WHEN {_SAME_JOB} AND excluded.completed_at IS NOT NULL
            THEN COALESCE(ci_jobs.completed_at, excluded.completed_at)
            ELSE excluded.completed_at END,
        created_at = CASE WHEN {_SAME_JOB} THEN ci_jobs.created_at END,
        runner_name = CASE WHEN {_SAME_JOB} THEN ci_jobs.runner_name END,
        runner_kind = CASE WHEN {_SAME_JOB} THEN ci_jobs.runner_kind END,
        failing_tests = CASE WHEN {_SAME_JOB} THEN ci_jobs.failing_tests END
"""


def record(
    connect: Callable[[], sqlite3.Connection],
    tickets: Sequence[TrackedTicket],
    now: float,
    *,
    run_jobs: Callable[[str, str], Sequence[RunJob]] | None,
    job_log: Callable[[str, str], str] | None,
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
    if job_log is not None:
        _read_failures(connect, job_log, log)


def _rows(ticket: TrackedTicket, now: float) -> list[tuple[object, ...]]:
    result = ticket.ci.get(ticket.current_head) if ticket.current_head else None
    if result is None:
        return []
    return [
        (
            ticket.repo.lower(),
            ticket.ticket_id,
            ticket.current_head,
            check.workflow,
            check.name,
            ticket.pr_number,
            check.run_id,
            check.job_id,
            check.log_url,
            check.status,
            check.conclusion,
            check.started_at,
            _completed_at(check, now),
            now,
        )
        for check in result.checks
    ]


def _completed_at(check: CheckRow, now: float) -> float | None:
    if check.status == "completed" and check.completed_at is None:
        return now
    return check.completed_at


def _prune(
    connection: sqlite3.Connection, tickets: Sequence[TrackedTicket], now: float
) -> None:
    connection.execute("DELETE FROM ci_jobs WHERE seen_at < ?", (now - RETAIN_SECONDS,))
    connection.execute(
        f"DELETE FROM ci_jobs WHERE {_FAILED} AND completed_at < ? AND {_NOT_SEEN_THIS_CYCLE}",
        (now - RETAIN_SECONDS, now),
    )
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
    job_log: Callable[[str, str], str],
    log: Callable[[str], None] | None,
) -> None:
    from lib.evidence.ci import failing_tests

    with connect() as connection:
        failed = connection.execute(
            f"""
            SELECT repo, ticket_id, head, workflow, check_name, job_id FROM ci_jobs
            WHERE status = 'completed' AND {_FAILED}
              AND job_id IS NOT NULL AND failing_tests IS NULL
            ORDER BY completed_at DESC, repo, ticket_id, job_id
            LIMIT ?
            """,
            (LOG_READS,),
        ).fetchall()
    for repo, ticket_id, head, workflow, name, job_id in failed:
        try:
            text = job_log(repo, job_id)
        except Exception as exc:
            _say(log, f"ci board: {repo} job {job_id} log unavailable: {type(exc).__name__}")
            text = ""
        with connect() as connection:
            connection.execute(
                """
                UPDATE ci_jobs SET failing_tests = ?
                WHERE repo = ? AND ticket_id = ? AND head = ? AND workflow = ?
                  AND check_name = ? AND job_id = ? AND failing_tests IS NULL
                """,
                (json.dumps(failing_tests(text)), repo, ticket_id, head, workflow, name, job_id),
            )


def read_pool(
    connect: Callable[[], sqlite3.Connection],
    repos: Sequence[str],
    runners: Callable[[str], Sequence[PoolRunner]],
    now: float,
    log: Callable[[str], None] | None,
) -> None:
    for repo in repos:
        try:
            pool = runners(repo)
        except Exception as exc:
            _say(log, f"ci board: {repo} runners unavailable: {type(exc).__name__}")
            continue
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM ci_runners WHERE repo = ?", (repo,))
            connection.executemany(
                "INSERT OR REPLACE INTO ci_runners VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        repo,
                        runner.name,
                        runner.os,
                        runner.status,
                        int(runner.busy),
                        json.dumps(list(runner.labels)),
                        now,
                    )
                    for runner in pool
                ],
            )
            connection.commit()
    with connect() as connection:
        connection.execute("DELETE FROM ci_runners WHERE seen_at < ?", (now - RETAIN_SECONDS,))


def _say(log: Callable[[str], None] | None, line: str) -> None:
    if log is not None:
        log(line)
