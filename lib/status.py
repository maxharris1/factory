"""Read-only status summary for the tracker and local job list.

A running job is shown with its lib/job_state.py state (claimed, running,
stale or dead) instead of the queue's plain `running` (#75), so a job a bot
claimed and never started, or whose heartbeat stopped, reads as such.
The Health section (#92) lists each component heartbeat (lib/health.py) and
marks a queued job with no live waiter for its harness.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, TextIO

from lib import health as component_health
from lib.device_pool import SLOTS_TABLE, Placement
from lib.job_state import Alive, JobStateConfig, derive, liveness, read_placements
from lib.linear_webhook import WebhookHealth, read_health
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.orch_yml import load_orch_yml
from lib.worktrees import pid_alive

ROOT = Path(__file__).resolve().parent.parent

PAUSED_STATES = frozenset(
    {"waiting-max", "waiting-external", "awaiting-ok", "stuck", "blocked", "capped"}
)
# Issue #86: the dispatcher's ship_holds table names who a held PR waits on.
SHIP_HOLDS_TABLE = "ship_holds"


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
    # Issue #86: the hold approver while the ticket is awaiting-ok.
    waiting_on: str | None = None


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
    # lib/job_state.py's state, set by build_status for a running job.
    job_state: str | None = None


@dataclass(frozen=True)
class DeviceRow:
    device: str
    harnesses: tuple[str, ...] | None
    used: int
    slot_max: int | None
    enabled: bool = True
    placeholder: bool = False
    listed: bool = True


@dataclass(frozen=True)
class Status:
    observed_at: float
    queued: tuple[JobRow, ...]
    box_slots: tuple[JobRow, ...]
    devices: tuple[DeviceRow, ...]
    paused: tuple[TrackerRow, ...]
    device_jobs: Mapping[str, tuple[JobRow, ...]] = field(default_factory=dict)
    # Only when a Linear webhook listener or webhook-mode poller has run on
    # this database (#55).
    webhook: WebhookHealth | None = None
    # Component heartbeats (#92), when build_status was given a reader.
    health: component_health.HealthReport | None = None


class TrackerReader(Protocol):
    def tracker_rows(self) -> tuple[TrackerRow, ...]: ...


class JobReader(Protocol):
    def job_rows(self) -> tuple[JobRow, ...]: ...


class SQLiteStatusReader:
    """Read the existing dispatcher database without initializing or mutating it."""

    def __init__(self, path: Path):
        self.path = path
        self._snapshot: (
            tuple[tuple[TrackerRow, ...], tuple[JobRow, ...], dict[str, str]] | None
        ) = None
        self._slots: dict[str, Placement] = {}

    def tracker_rows(self) -> tuple[TrackerRow, ...]:
        return self._rows()[0]

    def job_rows(self) -> tuple[JobRow, ...]:
        return self._rows()[1]

    def placements(self) -> dict[str, str]:
        """Device-pool slots held, as job id -> device."""
        return self._rows()[2]

    def slots(self) -> dict[str, Placement]:
        """Device-pool slots held, as job id -> the full placement."""
        self._rows()
        return self._slots

    def slot_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for device in self.placements().values():
            counts[device] = counts.get(device, 0) + 1
        return counts

    def webhook_health(self) -> WebhookHealth | None:
        connection = self._connect()
        try:
            return read_health(connection)
        finally:
            connection.close()

    def _rows(
        self,
    ) -> tuple[tuple[TrackerRow, ...], tuple[JobRow, ...], dict[str, str]]:
        if self._snapshot is not None:
            return self._snapshot
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            approvers = _hold_approvers(connection)
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
            has_slots = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (SLOTS_TABLE,),
            ).fetchone()
            placements = (
                {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        f"SELECT job_id, device FROM {SLOTS_TABLE}"
                    )
                }
                if has_slots
                else {}
            )
            self._slots = read_placements(connection) if has_slots else {}
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
                waiting_on=(
                    approvers.get((str(row["repo"]), str(row["ticket_id"])))
                    if row["workflow_state"] == "awaiting-ok"
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
        self._snapshot = (tracker, jobs, placements)
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


def _hold_approvers(connection: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """(repo, ticket id) -> approver of the ticket's latest hold, if any."""
    present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (SHIP_HOLDS_TABLE,),
    ).fetchone()
    if not present:
        return {}
    return {
        (str(row[0]), str(row[1])): str(row[2])
        for row in connection.execute(
            f"SELECT repo, ticket_id, approver FROM {SHIP_HOLDS_TABLE}"
        )
    }


def device_rows(
    config: MachinesConfig,
    used: dict[str, int],
) -> tuple[DeviceRow, ...]:
    """machines.yml order, then held slots on devices no longer listed."""
    rows = [
        DeviceRow(
            device=device.name,
            harnesses=(
                None
                if "harnesses" in device.placeholders
                else tuple(device.harnesses)
            ),
            used=used.get(device.name, 0),
            slot_max=device.slot_max,
            enabled=device.enabled,
            placeholder=device.placeholder,
        )
        for device in config.devices
    ]
    listed = {device.name for device in config.devices}
    rows.extend(
        DeviceRow(device=name, harnesses=None, used=count, slot_max=None, listed=False)
        for name, count in sorted(used.items())
        if name not in listed
    )
    return tuple(rows)


def build_status(
    tracker: TrackerReader,
    jobs: JobReader,
    *,
    clock: Callable[[], float] = time.time,
    devices: Sequence[DeviceRow] = (),
    placements: Mapping[str, str] | None = None,
    webhook: WebhookHealth | None = None,
    slots: Mapping[str, Placement] | None = None,
    job_states: JobStateConfig | None = None,
    alive: Alive | None = None,
    health: Callable[[float, list[tuple[str, str]]], component_health.HealthReport]
    | None = None,
) -> Status:
    """`placements` maps job id -> device for held device-pool slots. A
    running job with a placement is listed under its device; only running
    jobs without one (Phase 1 claims) occupy `Slots — box`. Each running
    job gets its job_state from `slots` (the full placements), the stale
    threshold in `job_states`, and `alive` (a local pid check, or None).
    `health(now, queued)` builds the Health section from (job id,
    recipient) for each queued job."""
    observed_at = clock()
    config = job_states if job_states is not None else JobStateConfig()
    held = slots or {}
    job_rows = tuple(
        replace(
            row,
            job_state=derive(
                last_activity_at=row.last_activity_at,
                now=observed_at,
                placement=held.get(row.id),
                stale_seconds=config.stale_seconds,
                liveness=liveness(held.get(row.id), alive),
            ),
        )
        if row.state == "running"
        else row
        for row in jobs.job_rows()
    )
    tracker_rows = tracker.tracker_rows()
    placed = placements or {}
    running = sorted(
        (row for row in job_rows if row.state == "running"),
        key=lambda row: (
            row.recipient,
            row.repo.casefold(),
            row.ticket_id,
            row.id,
        ),
    )
    report = (
        health(
            observed_at,
            [(row.id, row.recipient) for row in job_rows if row.state == "queued"],
        )
        if health is not None
        else None
    )
    device_jobs: dict[str, tuple[JobRow, ...]] = {}
    for row in running:
        if row.id in placed:
            name = placed[row.id]
            device_jobs[name] = (*device_jobs.get(name, ()), row)
    return Status(
        observed_at=observed_at,
        queued=tuple(
            sorted(
                (row for row in job_rows if row.state == "queued"),
                key=lambda row: (row.created_at, row.id),
            )
        ),
        webhook=webhook,
        health=report,
        box_slots=tuple(row for row in running if row.id not in placed),
        devices=tuple(devices),
        device_jobs=device_jobs,
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
        _job_line(row, status.observed_at, row.created_at) + _no_waiter(status, row)
        for row in status.queued
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

    lines.extend(("", f"Devices ({len(status.devices)})"))
    for device in status.devices:
        lines.append(_device_line(device))
        lines.extend(
            "  " + _job_line(row, status.observed_at, row.last_activity_at)
            for row in status.device_jobs.get(device.device, ())
        )
    if not status.devices:
        lines.append("  none")

    lines.extend(("", f"Paused ({len(status.paused)})"))
    for row in status.paused:
        action = f" paused={row.paused_action}" if row.paused_action else ""
        state = row.state
        if row.waiting_on:
            state = f"{state} waiting on {row.waiting_on}"
        if row.stuck:
            reason = f" ({row.stuck_reason})" if row.stuck_reason else ""
            state = f"stuck{reason}"
        lines.append(
            f"  {row.repo}#{row.ticket_id} {state} [{row.tracker}]{action}"
        )
    if not status.paused:
        lines.append("  empty")
    if status.webhook is not None:
        lines.extend(("", "Linear webhook"))
        lines.append(_webhook_line(status.webhook, status.observed_at))
        warning = status.webhook.silence_warning(status.observed_at)
        if warning is not None:
            lines.append(f"  warning: {warning}")
    if status.health is not None:
        lines.append("")
        lines.extend(component_health.render_lines(status.health, status.observed_at))
    return "\n".join(lines) + "\n"


def _no_waiter(status: Status, row: JobRow) -> str:
    flag = status.health.no_waiter_for(row.recipient) if status.health else None
    return f" [no waiter for {flag.harness}]" if flag is not None else ""


def _webhook_line(health: WebhookHealth, observed_at: float) -> str:
    if health.last_received_at is not None:
        event = f" ({health.last_event})" if health.last_event else ""
        received = f"{_age(observed_at, health.last_received_at)} ago{event}"
    elif health.started_at is not None:
        received = f"never (listener started {_age(observed_at, health.started_at)} ago)"
    else:
        received = "never (listener has not started)"
    return f"  last received {received}, dirty queue {health.dirty}"


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    clock: Callable[[], float] = time.time,
) -> int:
    parser = argparse.ArgumentParser(prog="orch status")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--machines", type=Path, default=ROOT / "machines.yml")
    parser.add_argument("--orch", type=Path)
    args = parser.parse_args(argv)
    path = args.db
    if path is None:
        value = os.environ.get("ORCH_DB")
        if not value:
            parser.error("pass --db or set ORCH_DB to the dispatcher database")
        path = Path(value)
    machines, findings = load_machines_yml(args.machines, allow_placeholders=True)
    if machines is None:
        errors = [item for item in findings if item.severity == "error"]
        detail = errors[0].message if errors else "invalid machines.yml"
        print(f"orch status: {args.machines}: {detail}", file=sys.stderr)
        return 1
    # job_states.stale_minutes: an explicit --orch must be valid; the
    # checkout's orch.yml is optional, and if it cannot be read the default
    # threshold is used with a warning, so status keeps working.
    orch_path = args.orch if args.orch is not None else ROOT / "orch.yml"
    job_states = JobStateConfig()
    if args.orch is not None or orch_path.is_file():
        orch, orch_findings = load_orch_yml(orch_path, allow_placeholders=True)
        if orch is not None:
            job_states = orch.job_states
        else:
            errors = [item for item in orch_findings if item.severity == "error"]
            detail = errors[0].message if errors else "invalid orch.yml"
            if args.orch is not None:
                print(f"orch status: {orch_path}: {detail}", file=sys.stderr)
                return 1
            print(
                f"orch status: warning: {orch_path}: {detail}; "
                "using the default job_states",
                file=sys.stderr,
            )
    reader = SQLiteStatusReader(path)
    try:
        report = render(
            build_status(
                reader,
                reader,
                devices=device_rows(machines, reader.slot_counts()),
                placements=reader.placements(),
                clock=clock,
                webhook=reader.webhook_health(),
                slots=reader.slots(),
                job_states=job_states,
                alive=pid_alive,
                health=lambda now, queued: component_health.load_report(
                    path, now, queued=queued, machines=machines
                ),
            )
        )
    except (sqlite3.Error, StatusError) as exc:
        print(f"orch status: {exc}", file=sys.stderr)
        return 1
    print(report, end="", file=stdout)
    return 0


def _job_line(row: JobRow, observed_at: float, since: float) -> str:
    return (
        f"  {row.repo}#{row.ticket_id} {row.action} -> {row.recipient} "
        f"{row.job_state or row.state} {_age(observed_at, since)}"
    )


def _device_line(row: DeviceRow) -> str:
    if row.harnesses is None:
        harnesses = "?"
    else:
        harnesses = ",".join(row.harnesses) or "none"
    slot_max = "?" if row.slot_max is None else str(row.slot_max)
    flags = []
    if not row.listed:
        flags.append("not-in-machines.yml")
    if not row.enabled:
        flags.append("disabled")
    if row.placeholder:
        flags.append("placeholder")
    suffix = "".join(f" {flag}" for flag in flags)
    return f"  {row.device} [{harnesses}] {row.used}/{slot_max} slots{suffix}"


def _age(observed_at: float, since: float) -> str:
    seconds = max(0, int(observed_at - since))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
