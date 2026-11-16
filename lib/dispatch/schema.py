"""Dispatcher state DB schema: connect, initialize and migrations."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping

from lib import attention, project_rules
from lib.intake_risk import INTAKE_RISK_SCHEMA
from lib.pr_title import SOURCES_TABLE


class SchemaMixin:
    def _connect(self) -> sqlite3.Connection:
        return self.queue.connect()

    def _migrate_ship_intent_redrive(self) -> None:
        """Add ship_intents.redrive. Once, mark legacy handoff failures eligible.

        Before issue #47 every handoff failure was recorded by class name
        only. None had a confirmed write: draft -> ready went to a REST
        route GitHub does not have, so it never succeeded, and an
        unconfirmed Linear move confirmed nothing. Those rows get the one
        re-drive. The column add and the backfill share one transaction.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(ship_intents)"
                ).fetchall()
            }
            if "redrive" not in columns:
                connection.execute("ALTER TABLE ship_intents ADD COLUMN redrive TEXT")
                connection.execute(
                    """
                    UPDATE ship_intents SET redrive = 'eligible'
                    WHERE policy = 'handoff' AND status = 'failed'
                      AND error IN ('GitHubAdapterError', 'HeadMismatchError')
                    """
                )
            connection.commit()

    def _initialize(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handled_comments (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_state (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    gate_count INTEGER NOT NULL DEFAULT 0,
                    stuck INTEGER NOT NULL DEFAULT 0,
                    workflow_state TEXT,
                    review_round INTEGER NOT NULL DEFAULT 0,
                    plan_gap_count INTEGER NOT NULL DEFAULT 0,
                    plan_check_comment_id TEXT,
                    paused_action TEXT,
                    paused_recipient TEXT,
                    paused_head TEXT,
                    paused_generation INTEGER,
                    paused_question_id TEXT,
                    stuck_reason TEXT,
                    stuck_source_comment_id TEXT,
                    stuck_wake_job_id TEXT,
                    timeout_seconds INTEGER,
                    generation INTEGER NOT NULL DEFAULT 1,
                    cap_extra INTEGER NOT NULL DEFAULT 0,
                    approved_head TEXT,
                    approved_at REAL,
                    approve_comment_id TEXT,
                    update_failures INTEGER NOT NULL DEFAULT 0,
                    update_from_head TEXT,
                    update_failed_job TEXT,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS comment_links (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    link TEXT,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            verdict_heads_existed = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'verdict_heads'"
            ).fetchone()
            connection.execute(
                "CREATE TABLE IF NOT EXISTS verdict_heads_floor (handled_rowid INTEGER NOT NULL)"
            )
            if verdict_heads_existed is None or connection.execute(
                "SELECT 1 FROM verdict_heads_floor"
            ).fetchone() is None:
                connection.execute(
                    """
                    INSERT INTO verdict_heads_floor
                    SELECT COALESCE(MAX(rowid), 0) FROM handled_comments
                    """
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS approve_rule_floor (since REAL NOT NULL)"
            )
            if connection.execute("SELECT 1 FROM approve_rule_floor").fetchone() is None:
                in_use = connection.execute("SELECT 1 FROM handled_comments LIMIT 1").fetchone()
                connection.execute(
                    "INSERT INTO approve_rule_floor VALUES (?)",
                    (time.time() if in_use else 0.0,),
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS verdict_heads (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    review_round INTEGER,
                    posted_at REAL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handled_times (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    handled_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.commit()
            _add_missing_columns(connection, "verdict_heads", {"posted_at": "REAL"})
            _add_missing_columns(
                connection,
                "ticket_state",
                {
                    "stuck_wake_job_id": "TEXT",
                    "workflow_state": "TEXT",
                    "review_round": "INTEGER NOT NULL DEFAULT 0",
                    "plan_gap_count": "INTEGER NOT NULL DEFAULT 0",
                    "plan_check_comment_id": "TEXT",
                    "paused_action": "TEXT",
                    "paused_recipient": "TEXT",
                    "paused_head": "TEXT",
                    "paused_generation": "INTEGER",
                    "paused_question_id": "TEXT",
                    "cap_extra": "INTEGER NOT NULL DEFAULT 0",
                    "approved_head": "TEXT",
                    "approved_at": "REAL",
                    "approve_comment_id": "TEXT",
                    "update_failures": "INTEGER NOT NULL DEFAULT 0",
                    "update_from_head": "TEXT",
                    "update_failed_job": "TEXT",
                },
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    at REAL NOT NULL,
                    source TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ticket_transitions_ticket
                ON ticket_transitions (repo, ticket_id, at, id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ci_events (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    status TEXT NOT NULL,
                    gate_count_after INTEGER NOT NULL,
                    PRIMARY KEY (repo, ticket_id, head)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked_tickets (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    tracker TEXT NOT NULL,
                    tracked_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            project_rules.ensure_table(connection)
            connection.execute(INTAKE_RISK_SCHEMA)
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SOURCES_TABLE} (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    pr_number TEXT,
                    titled_at REAL,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ship_holds (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    approve_comment_id TEXT NOT NULL,
                    held_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ship_intents (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    redrive TEXT,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS external_ship_intents (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    source_comment_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    settled INTEGER NOT NULL DEFAULT 0,
                    settle_failures INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (repo, ticket_id, source_comment_id)
                )
                """
            )
            _add_missing_columns(
                connection,
                "external_ship_intents",
                {
                    "settled": "INTEGER NOT NULL DEFAULT 0",
                    "settle_failures": "INTEGER NOT NULL DEFAULT 0",
                },
            )
        self._migrate_ship_intent_redrive()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plan_events (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_events (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS followup_filings (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    labels TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('pending', 'filed', 'commented')),
                    issue_number INTEGER,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auto_extra_comments (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'posted')),
                    created_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_retries (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    stuck_reason TEXT,
                    resumed_action TEXT,
                    job_id TEXT,
                    outcome TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS poll_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    error TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    detail TEXT
                )
                """
            )
            if "detail" not in {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(poll_errors)").fetchall()
            }:
                connection.execute("ALTER TABLE poll_errors ADD COLUMN detail TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS poll_errors_recorded_at "
                "ON poll_errors (recorded_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_reads (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    read_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS poll_error_streaks (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS poll_deferrals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    detail TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                )
                """
            )
            # #83/#84: tickets that need a person (a ship that failed and
            # will not be retried, a job dedupe dropped). Shown by orch
            # status and the dashboard; cleared when the ticket moves on.
            attention.ensure_table(connection)
            # #75: what the poller did to a job because of its state
            # (released-claimed, dead; #94 requeued-unstarted from orch job run), one row each, for the log and audit.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS eval_log_cursor (
                    source TEXT PRIMARY KEY,
                    last INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS eval_log_pending (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    offset INTEGER NOT NULL,
                    text TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS eval_log_repos (
                    repo_key TEXT PRIMARY KEY,
                    repo TEXT NOT NULL
                )
                """
            )
            # #545: an existing database starts dispatcher.log at its current
            # rows rather than dumping its whole history into one cycle.
            for source in ("ticket_transitions", "jobs", "handled_times"):
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO eval_log_cursor (source, last)
                    SELECT '{source}', COALESCE(MAX(rowid), 0) FROM {source}
                    """
                )


def _add_missing_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: Mapping[str, str],
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    present = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, declaration in columns.items():
        if name not in present:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    connection.commit()
