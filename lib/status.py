"""Read-only status summary for the tracker and local job list."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, TextIO

PAUSED_STATES = frozenset(
    {"waiting-max", "waiting-external", "stuck", "blocked", "capped"}
)


class StatusError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrackerRow:
    repo: str
    ticket_id: str
    tracker: str
    state: str
    paused_action: str | None = None
    stuck: bool = False
    stuck_reason: str | None = None


@dataclass(frozen=True)
class JobRow:
    id: str
    repo: str
    ticket_id: str
    action: str
    recipient: str
    state: str
    created_at: float
    last_activity_at: float


@dataclass(frozen=True)
class DeviceRow:
    device: str
    recipient: str
    repo: str
    ticket_id: str
    action: str
    started_at: float


@dataclass(frozen=True)
class Status:
    observed_at: float
    queued: tuple[JobRow, ...]
    box_slots: tuple[JobRow, ...]
    device_slots: tuple[DeviceRow, ...]
    paused: tuple[TrackerRow, ...]


class TrackerReader(Protocol):
    def tracker_rows(self) -> tuple[TrackerRow, ...]: ...


class JobReader(Protocol):
    def job_rows(self) -> tuple[JobRow, ...]: ...


class DeviceRowsProvider(Protocol):
    def __call__(self) -> Sequence[DeviceRow]: ...


class SQLiteStatusReader:
    """Read the existing dispatcher database without initializing or mutating it."""

    def __init__(self, path: Path):
        self.path = path
        self._snapshot: tuple[tuple[TrackerRow, ...], tuple[JobRow, ...]] | None = None

    def tracker_rows(self) -> tuple[TrackerRow, ...]:
        return self._rows()[0]

    def job_rows(self) -> tuple[JobRow, ...]:
        return self._rows()[1]

    def _rows(self) -> tuple[tuple[TrackerRow, ...], tuple[JobRow, ...]]:
        if self._snapshot is not None:
            return self._snapshot
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            tracker_rows = connection.execute(
                """
                SELECT tracked.repo, tracked.ticket_id, tracked.tracker,
                       COALESCE(state.workflow_state, 'intake') AS workflow_state,
                       state.paused_action, COALESCE(state.stuck, 0) AS stuck,
                       state.stuck_reason
                FROM tracked_tickets AS tracked
                LEFT JOIN ticket_state AS state
                  ON state.repo = tracked.repo
                 AND state.ticket_id = tracked.ticket_id
                """
            ).fetchall()
            job_rows = connection.execute(
                """
                SELECT id, repo, ticket_id, action, recipient, state,
                       created_at, last_activity_at
                FROM jobs
                WHERE state IN ('queued', 'running')
                """
            ).fetchall()
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        tracker = tuple(
            TrackerRow(
                repo=str(row["repo"]),
                ticket_id=str(row["ticket_id"]),
                tracker=str(row["tracker"]),
                state=str(row["workflow_state"]),
                paused_action=(
                    str(row["paused_action"])
                    if row["paused_action"] is not None
                    else None
                ),
                stuck=bool(row["stuck"]),
                stuck_reason=(
                    str(row["stuck_reason"])
                    if row["stuck_reason"] is not None
                    else None
                ),
            )
            for row in tracker_rows
        )
        jobs = tuple(
            JobRow(
                id=str(row["id"]),
                repo=str(row["repo"]),
                ticket_id=str(row["ticket_id"]),
                action=str(row["action"]),
                recipient=str(row["recipient"]),
                state=str(row["state"]),
                created_at=float(row["created_at"]),
                last_activity_at=float(row["last_activity_at"]),
            )
            for row in job_rows
        )
        self._snapshot = (tracker, jobs)
        return self._snapshot

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise StatusError(f"database does not exist: {self.path}")
        wal_path = Path(f"{self.path}-wal")
        query = "?mode=ro" if wal_path.exists() else "?mode=ro&immutable=1"
        uri = self.path.resolve().as_uri() + query
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise StatusError(f"cannot read database: {exc}") from exc


def device_rows() -> tuple[DeviceRow, ...]:
    """F2-01 will replace this empty provider with device inventory rows."""
    return ()


def build_status(
    tracker: TrackerReader,
    jobs: JobReader,
    *,
    clock: Callable[[], float] = time.time,
    device_rows_provider: DeviceRowsProvider = device_rows,
) -> Status:
    observed_at = clock()
    job_rows = jobs.job_rows()
    tracker_rows = tracker.tracker_rows()
    return Status(
        observed_at=observed_at,
        queued=tuple(
            sorted(
                (row for row in job_rows if row.state == "queued"),
                key=lambda row: (row.created_at, row.id),
            )
        ),
        box_slots=tuple(
            sorted(
                (row for row in job_rows if row.state == "running"),
                key=lambda row: (
                    row.recipient,
                    row.repo.casefold(),
                    row.ticket_id,
                    row.id,
                ),
            )
        ),
        device_slots=tuple(
            sorted(
                device_rows_provider(),
                key=lambda row: (
                    row.device.casefold(),
                    row.recipient,
                    row.repo.casefold(),
                    row.ticket_id,
                ),
            )
        ),
        paused=tuple(
            sorted(
                (
                    row
                    for row in tracker_rows
                    if row.stuck or row.state in PAUSED_STATES
                ),
                key=lambda row: (
                    row.repo.casefold(),
                    row.ticket_id,
                    row.tracker,
                ),
            )
        ),
    )


def render(status: Status) -> str:
    timestamp = datetime.fromtimestamp(
        status.observed_at,
        tz=timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"orch status {timestamp}", "", f"Queue ({len(status.queued)})"]
    lines.extend(
        _job_line(row, status.observed_at, row.created_at) for row in status.queued
    )
    if not status.queued:
        lines.append("  empty")

    lines.extend(("", f"Slots — box ({len(status.box_slots)} occupied)"))
    lines.extend(
        _job_line(row, status.observed_at, row.last_activity_at)
        for row in status.box_slots
    )
    if not status.box_slots:
        lines.append("  idle")
    for row in status.device_slots:
        lines.append(
            f"  {row.device} {row.recipient} {row.repo}#{row.ticket_id} "
            f"{row.action} {_age(status.observed_at, row.started_at)}"
        )

    lines.extend(("", f"Paused ({len(status.paused)})"))
    for row in status.paused:
        action = f" paused={row.paused_action}" if row.paused_action else ""
        state = row.state
        if row.stuck:
            reason = f" ({row.stuck_reason})" if row.stuck_reason else ""
            state = f"stuck{reason}"
        lines.append(
            f"  {row.repo}#{row.ticket_id} {state} [{row.tracker}]{action}"
        )
    if not status.paused:
        lines.append("  empty")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="orch status")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    path = args.db
    if path is None:
        value = os.environ.get("ORCH_DB")
        if not value:
            parser.error("pass --db or set ORCH_DB to the dispatcher database")
        path = Path(value)
    reader = SQLiteStatusReader(path)
    try:
        report = render(build_status(reader, reader))
    except (sqlite3.Error, StatusError) as exc:
        print(f"orch status: {exc}", file=sys.stderr)
        return 1
    print(report, end="", file=stdout)
    return 0


def _job_line(row: JobRow, observed_at: float, since: float) -> str:
    return (
        f"  {row.repo}#{row.ticket_id} {row.action} -> {row.recipient} "
        f"{row.state} {_age(observed_at, since)}"
    )


def _age(observed_at: float, since: float) -> str:
    seconds = max(0, int(observed_at - since))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
