import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType

import pytest

from lib.machines_yml import Device, MachinesConfig, load_machines_yml
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
        "Devices (0)\n"
        "  none\n"
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
        "Devices (0)\n"
        "  none\n"
        "\n"
        "Paused (0)\n"
        "  empty\n"
    )


def test_device_rows_show_harnesses_slots_and_placeholders(tmp_path: Path) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(
        """
devices:
  - name: box
    local: true
    slot_max: 3
    harnesses: {claude: claude-bot, codex: codex-bot}
  - name: pc
    enabled: false
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {cursor: cursor-bot}
    workdir_root: /home/work
  - name: mac
    tailscale_name: REPLACE_WITH_MAC_TAILSCALE_NAME
    slot_max: REPLACE_WITH_MAC_SLOT_MAX
    harnesses: REPLACE_WITH_MAC_HARNESSES
    workdir_root: /Users/work
""".lstrip(),
        encoding="utf-8",
    )
    config, _ = load_machines_yml(machines, allow_placeholders=True)
    assert config is not None
    source = FakeStatusSource()

    output = render(
        build_status(
            source,
            source,
            clock=lambda: 0,
            devices=device_rows(config, {"box": 2, "retired": 1}),
        )
    )

    assert (
        "Devices (4)\n"
        "  box [claude,codex] 2/3 slots\n"
        "  pc [cursor] 0/2 slots disabled\n"
        "  mac [?] 0/? slots placeholder\n"
        "  retired [?] 1/? slots not-in-machines.yml\n"
        "\n"
        "Paused (0)\n"
    ) in output


def test_status_command_counts_held_device_slots(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE device_slots (
                job_id TEXT PRIMARY KEY, device TEXT NOT NULL,
                harness TEXT NOT NULL, os_user TEXT NOT NULL,
                placed_at REAL NOT NULL
            );
            INSERT INTO device_slots VALUES ('running', 'box', 'codex', 'codex-bot', 20);
            """
        )
    before = directory_snapshot(tmp_path)

    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "status", "--db", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Devices (3)\n" in result.stdout
    assert "Slots — box (0 occupied)\n  idle\n" in result.stdout
    assert "  box [claude,codex,cursor] 1/3 slots\n" in result.stdout
    assert "  pc [claude,codex,cursor] 0/3 slots disabled\n" in result.stdout
    assert "  mac [none] 0/2 slots disabled placeholder\n" in result.stdout
    assert directory_snapshot(tmp_path) == before


def test_pool_placed_job_is_shown_under_its_device_not_as_box_occupancy() -> None:
    """A3: a running job that holds a device_slots row is reported under its
    device only; `Slots — box` lists only Phase 1 claims with no placement."""
    source = FakeStatusSource(
        jobs=(
            job(
                "on-pc",
                repo="o/repo",
                ticket_id="1",
                state="running",
                created_at=10,
                last_activity_at=90,
                recipient="codex",
            ),
            job(
                "phase1",
                repo="o/repo",
                ticket_id="2",
                state="running",
                created_at=10,
                last_activity_at=80,
            ),
        ),
    )
    machines = MachinesConfig(
        devices=(
            Device(
                name="box",
                slot_max=3,
                harnesses=MappingProxyType({"claude": "claude-bot"}),
                local=True,
            ),
            Device(
                name="pc",
                slot_max=3,
                harnesses=MappingProxyType({"codex": "codex-bot"}),
                tailscale_name="pc.example.ts.net",
                workdir_root="/home/work",
            ),
        )
    )

    output = render(
        build_status(
            source,
            source,
            clock=lambda: 100,
            devices=device_rows(machines, {"pc": 1}),
            placements={"on-pc": "pc"},
        )
    )

    assert (
        "Slots — box (1 occupied)\n"
        "  o/repo#2 build -> claude running 20s\n"
        "\n"
        "Devices (2)\n"
        "  box [claude] 0/3 slots\n"
        "  pc [codex] 1/3 slots\n"
        "    o/repo#1 build -> codex running 10s\n"
        "\n"
    ) in output


def test_status_command_counts_a_pc_job_once(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE device_slots (
                job_id TEXT PRIMARY KEY, device TEXT NOT NULL,
                harness TEXT NOT NULL, os_user TEXT NOT NULL,
                placed_at REAL NOT NULL
            );
            INSERT INTO device_slots VALUES ('running', 'pc', 'codex', 'codex-bot', 20);
            """
        )

    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "status", "--db", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Slots — box (0 occupied)\n  idle\n" in result.stdout
    assert "  box [claude,codex,cursor] 0/3 slots\n" in result.stdout
    assert (
        "  pc [claude,codex,cursor] 1/3 slots disabled\n"
        "    owner/repo#43 review -> codex running "
    ) in result.stdout


def test_status_command_refuses_an_invalid_machines_file(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    machines = tmp_path / "machines.yml"
    machines.write_text("devices: []\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "orch"),
            "status",
            "--db",
            str(path),
            "--machines",
            str(machines),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "devices must list at least one device" in result.stderr


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


def test_status_reads_uncheckpointed_wal_without_changing_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            """
            INSERT INTO jobs
            VALUES ('wal-queued', 'owner/repo', '44', 'fix', 'cursor',
                    'queued', 40, 40)
            """
        )
        writer.commit()
        assert Path(f"{path}-wal").exists()
        prime = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        prime.execute("SELECT COUNT(*) FROM jobs").fetchone()
        prime.close()
        before = directory_snapshot(tmp_path)
        reader = SQLiteStatusReader(path)

        status = build_status(reader, reader, clock=lambda: 100)

        assert [row.id for row in status.queued] == ["queued", "wal-queued"]
        assert directory_snapshot(tmp_path) == before
    finally:
        writer.close()


def test_status_clean_wal_database_creates_no_sidecar_files(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    connection.close()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
    before = directory_snapshot(tmp_path)
    reader = SQLiteStatusReader(path)

    build_status(reader, reader, clock=lambda: 100)

    assert directory_snapshot(tmp_path) == before
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_dispatcher_stuck_flag_is_rendered_as_paused(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE ticket_state
            SET workflow_state = 'building', stuck = 1,
                stuck_reason = 'ci-red-3'
            WHERE repo = 'owner/repo' AND ticket_id = '42'
            """
        )
    reader = SQLiteStatusReader(path)

    output = render(build_status(reader, reader, clock=lambda: 100))

    assert "Paused (1)" in output
    assert "owner/repo#42 stuck (ci-red-3) [github]" in output


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
                paused_action TEXT,
                stuck INTEGER NOT NULL DEFAULT 0,
                stuck_reason TEXT
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
            VALUES ('owner/repo', '42', 'waiting-max', 'build', 0, NULL);
            INSERT INTO jobs
            VALUES ('queued', 'owner/repo', '42', 'build', 'claude',
                    'queued', 10, 10);
            INSERT INTO jobs
            VALUES ('running', 'owner/repo', '43', 'review', 'codex',
                    'running', 20, 30);
            """
        )
