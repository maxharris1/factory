import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from lib.status import (
    JobRow,
    SQLiteStatusReader,
    TrackerRow,
    build_status,
    device_rows,
    render,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeStatusSource:
    def __init__(
        self,
        *,
        tracker: tuple[TrackerRow, ...] = (),
        jobs: tuple[JobRow, ...] = (),
    ):
        self.tracker = tracker
        self.jobs = jobs
        self.read_calls = 0
        self.write_calls: list[tuple[str, str]] = []

    def tracker_rows(self) -> tuple[TrackerRow, ...]:
        self.read_calls += 1
        return self.tracker

    def job_rows(self) -> tuple[JobRow, ...]:
        self.read_calls += 1
        return self.jobs

    def set_state(self, ticket_id: str, state: str) -> None:
        self.write_calls.append((ticket_id, state))


def job(
    job_id: str,
    *,
    repo: str,
    ticket_id: str,
    state: str,
    created_at: float,
    last_activity_at: float,
    action: str = "build",
    recipient: str = "claude",
) -> JobRow:
    return JobRow(
        id=job_id,
        repo=repo,
        ticket_id=ticket_id,
        action=action,
        recipient=recipient,
        state=state,
        created_at=created_at,
        last_activity_at=last_activity_at,
    )


def test_status_is_stable_and_summarizes_queue_box_slots_and_paused_items() -> None:
    source = FakeStatusSource(
        tracker=(
            TrackerRow("z/repo", "9", "github", "building"),
            TrackerRow("b/repo", "2", "linear", "waiting-max", "fix"),
            TrackerRow("A/repo", "10", "github", "stuck"),
        ),
        jobs=(
            job(
                "running",
                repo="z/repo",
                ticket_id="9",
                state="running",
                created_at=20,
                last_activity_at=7150,
                action="review",
                recipient="codex",
            ),
            job(
                "later",
                repo="b/repo",
                ticket_id="2",
                state="queued",
                created_at=100,
                last_activity_at=100,
            ),
            job(
                "earlier",
                repo="a/repo",
                ticket_id="1",
                state="queued",
                created_at=10,
                last_activity_at=10,
                action="plan",
                recipient="cursor",
            ),
        ),
    )

    output = render(build_status(source, source, clock=lambda: 7200))

    assert output == (
        "orch status 1970-01-01T02:00:00Z\n"
        "\n"
        "Queue (2)\n"
        "  a/repo#1 plan -> cursor queued 1h\n"
        "  b/repo#2 build -> claude queued 1h\n"
        "\n"
        "Slots — box (1 occupied)\n"
        "  z/repo#9 review -> codex running 50s\n"
        "\n"
        "Paused (2)\n"
        "  A/repo#10 stuck [github]\n"
        "  b/repo#2 waiting-max [linear] paused=fix\n"
    )
    assert source.read_calls == 2
    assert source.write_calls == []


def test_status_empty_state() -> None:
    source = FakeStatusSource()

    assert render(build_status(source, source, clock=lambda: 0)) == (
        "orch status 1970-01-01T00:00:00Z\n"
        "\n"
        "Queue (0)\n"
        "  empty\n"
        "\n"
        "Slots — box (0 occupied)\n"
        "  idle\n"
        "\n"
        "Paused (0)\n"
        "  empty\n"
    )


def test_device_rows_are_empty_until_f2_01() -> None:
    source = FakeStatusSource()

    assert device_rows() == ()
    assert build_status(source, source, clock=lambda: 0).device_slots == ()


def test_status_command_changes_no_database_bytes_or_mtimes(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    before = directory_snapshot(tmp_path)

    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "status", "--db", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Queue (1)" in result.stdout
    assert "Slots — box (1 occupied)" in result.stdout
    assert "Paused (1)" in result.stdout
    assert directory_snapshot(tmp_path) == before


def test_read_only_proof_detects_a_mutant_tracker_write(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    tracker = FakeStatusSource(
        tracker=(TrackerRow("owner/repo", "42", "github", "waiting-max"),)
    )
    jobs = SQLiteStatusReader(path)

    assert_read_only(
        tmp_path,
        tracker,
        lambda: render(build_status(tracker, jobs, clock=lambda: 100)),
    )

    with pytest.raises(AssertionError):
        assert_read_only(
            tmp_path,
            tracker,
            lambda: (
                build_status(tracker, jobs, clock=lambda: 100),
                tracker.set_state("42", "building"),
            ),
        )


def assert_read_only(
    directory: Path,
    tracker: FakeStatusSource,
    action: Callable[[], object],
) -> None:
    before_files = directory_snapshot(directory)
    before_writes = tuple(tracker.write_calls)
    action()
    assert directory_snapshot(directory) == before_files
    assert tuple(tracker.write_calls) == before_writes


def directory_snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tracked_tickets (
                repo TEXT NOT NULL,
                ticket_id TEXT NOT NULL,
                tracker TEXT NOT NULL,
                tracked_at REAL NOT NULL
            );
            CREATE TABLE ticket_state (
                repo TEXT NOT NULL,
                ticket_id TEXT NOT NULL,
                workflow_state TEXT,
                paused_action TEXT
            );
            CREATE TABLE jobs (
                id TEXT NOT NULL,
                repo TEXT NOT NULL,
                ticket_id TEXT NOT NULL,
                action TEXT NOT NULL,
                recipient TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_activity_at REAL NOT NULL
            );
            INSERT INTO tracked_tickets
            VALUES ('owner/repo', '42', 'github', 1);
            INSERT INTO ticket_state
            VALUES ('owner/repo', '42', 'waiting-max', 'build');
            INSERT INTO jobs
            VALUES ('queued', 'owner/repo', '42', 'build', 'claude',
                    'queued', 10, 10);
            INSERT INTO jobs
            VALUES ('running', 'owner/repo', '43', 'review', 'codex',
                    'running', 20, 30);
            """
        )
