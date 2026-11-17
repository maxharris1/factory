import io
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType

import pytest

from lib.cli import main as cli_main
from lib.machines_yml import Device, MachinesConfig, load_machines_yml
from lib.status import (
    JobRow,
    SQLiteStatusReader,
    TrackerRow,
    build_status,
    device_rows,
    render,
)
from lib.status import main as status_main
from tests.pool_helpers import LOCAL_BOX_MACHINES, write_target_machines

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
        "Devices (1)\n"
        "  box [?] 1/? slots not-in-machines.yml\n"
        "    z/repo#9 review -> codex running 1h (beat 50s ago) no-placement\n"
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
        "Devices (0)\n"
        "  none\n"
        "\n"
        "Paused (0)\n"
        "  empty\n"
    )


def test_device_rows_show_harnesses_slots_drain_and_placeholders(tmp_path: Path) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(
        """
devices:
  - name: box
    local: true
    drain: true
    slot_max: 3
    harnesses: {claude: claude-bot, codex: codex-bot}
  - name: pc
    enabled: false
    drain: true
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {cursor: cursor-bot}
    workdir_root: /home/work
    ssh_alias: pc-{bot}
    factory_root: /home/{bot}/factory
  - name: mac
    tailscale_name: REPLACE_WITH_MAC_TAILSCALE_NAME
    slot_max: REPLACE_WITH_MAC_SLOT_MAX
    harnesses: REPLACE_WITH_MAC_HARNESSES
    workdir_root: /Users/work
    ssh_alias: REPLACE_WITH_MAC_SSH_ALIAS
    factory_root: /Users/{bot}/factory
""".lstrip(),
        encoding="utf-8",
    )
    config, _ = load_machines_yml(machines)
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
        "  box [claude,codex] 2/3 slots draining\n"
        "  pc [cursor] 0/2 slots disabled draining\n"
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
        [sys.executable, str(ROOT / "bin" / "orch"), "status", "--db", str(path),
         "--machines", str(LOCAL_BOX_MACHINES)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Devices (3)\n" in result.stdout
    assert "Slots — box" not in result.stdout
    assert "  box [claude,codex,cursor] 1/3 slots\n" in result.stdout
    assert "  pc [claude,codex,cursor] 0/3 slots disabled\n" in result.stdout
    assert "  mac [none] 0/2 slots disabled placeholder\n" in result.stdout
    assert directory_snapshot(tmp_path) == before


def test_pool_placed_job_is_shown_under_its_device_not_as_box_occupancy() -> None:
    """A3: a running job that holds a device_slots row is reported under its
    device only; a running job with no placement is listed and counted on the
    box device (issue #90: one slot count per device)."""
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
        "Devices (2)\n"
        "  box [claude] 1/3 slots\n"
        "    o/repo#2 build -> claude running 1m (beat 20s ago) no-placement\n"
        "  pc [codex] 1/3 slots\n"
        "    o/repo#1 build -> codex running 1m (beat 10s ago)\n"
        "\n"
    ) in output


def test_an_unplaced_job_is_counted_on_the_orch_host(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)

    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "status", "--db", str(path),
         "--machines", str(write_target_machines(tmp_path))],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "  pc [claude,codex,cursor] 1/10 slots\n    owner/repo#43 review -> codex" in (
        result.stdout
    )
    assert "no-placement" in result.stdout.split("  box ")[0]
    assert "  box [claude,codex,cursor] 0/6 slots\n" in result.stdout


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
        [sys.executable, str(ROOT / "bin" / "orch"), "status", "--db", str(path),
         "--machines", str(LOCAL_BOX_MACHINES)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Slots — box" not in result.stdout
    assert "  box [claude,codex,cursor] 0/3 slots\n" in result.stdout
    assert (
        "  pc [claude,codex,cursor] 1/3 slots disabled\n"
        "    owner/repo#43 review -> codex stale "  # no heartbeat for years (#75)
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


AUDIT_HOOK = """
import os
import sys

_log = open(os.environ["STATUS_AUDIT_LOG"], "a", buffering=1)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _hook(event, args):
    if event == "open":
        path, mode, flags = args
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            _log.write(f"write {path}\\n")
        elif mode is None and flags & _WRITE_FLAGS:
            _log.write(f"write {path}\\n")
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.replace"):
        _log.write(f"{event} {args[0]}\\n")
    elif event == "sqlite3.connect":
        _log.write(f"connect {args[0]}\\n")


sys.addaudithook(_hook)
"""


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_status_command_writes_nothing_anywhere(tmp_path: Path, journal: str) -> None:
    state = tmp_path / "state"
    path = state / "db" / "queue.sqlite3"
    path.parent.mkdir(parents=True)
    create_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(f"PRAGMA journal_mode = {journal}").fetchone() == (journal,)
    connection.close()
    home = state / "home"
    temp = state / "tmp"
    home.mkdir()
    temp.mkdir()
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(AUDIT_HOOK, encoding="utf-8")
    log = tmp_path / "audit.log"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(temp),
        "PYTHONPATH": str(site),
        "PYTHONDONTWRITEBYTECODE": "1",
        "STATUS_AUDIT_LOG": str(log),
    }
    before = directory_snapshot(state)

    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "status", "--db", str(path),
         "--machines", str(LOCAL_BOX_MACHINES)],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Queue (1)" in result.stdout
    assert "  box [claude,codex,cursor] 1/3 slots\n" in result.stdout
    assert "Paused (1)" in result.stdout
    assert directory_snapshot(state) == before
    events = log.read_text(encoding="utf-8").splitlines()
    connects = [line for line in events if line.startswith("connect ")]
    assert connects
    assert all("mode=ro" in line for line in connects), connects
    assert [line for line in events if not line.startswith("connect ")] == []


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
        # A first reader of a WAL database writes its read mark into -shm;
        # prime it so the snapshot below sees only what status itself does.
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


def test_a_stuck_ticket_shows_its_accepted_retries(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            UPDATE ticket_state SET workflow_state = 'fixing', stuck = 1,
                stuck_reason = 'job-failed-2', paused_action = NULL;
            CREATE TABLE ticket_retries (
                repo TEXT NOT NULL, ticket_id TEXT NOT NULL, comment_id TEXT NOT NULL,
                stuck_reason TEXT, resumed_action TEXT, job_id TEXT,
                outcome TEXT NOT NULL, recorded_at REAL NOT NULL,
                PRIMARY KEY (repo, ticket_id, comment_id)
            );
            INSERT INTO ticket_retries VALUES
                ('owner/repo', '42', 'c1', 'ci-red-3', 'fix', 'j1', 'queued-retry-fix', 400),
                ('owner/repo', '42', 'c2', 'job-failed-2', 'fix', 'j2', 'queued-retry-fix', 760),
                ('owner/repo', '42', 'c3', NULL, NULL, NULL, 'rejected-retry-job-active', 900),
                ('owner/repo', '43', 'c4', 't7-stuck', NULL, NULL, 'retried-no-job', 900);
            """
        )
    reader = SQLiteStatusReader(path)

    output = render(build_status(reader, reader, clock=lambda: 1000))

    assert (
        "  owner/repo#42 stuck (job-failed-2) retried 2x (job-failed-2 -> fix, 4m ago) [github]\n"
        in output
    )


def test_a_resumed_tickets_job_shows_its_retries(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            UPDATE ticket_state SET workflow_state = 'fixing', paused_action = NULL;
            CREATE TABLE ticket_retries (
                repo TEXT NOT NULL, ticket_id TEXT NOT NULL, comment_id TEXT NOT NULL,
                stuck_reason TEXT, resumed_action TEXT, job_id TEXT,
                outcome TEXT NOT NULL, recorded_at REAL NOT NULL,
                PRIMARY KEY (repo, ticket_id, comment_id)
            );
            INSERT INTO ticket_retries VALUES
                ('owner/repo', '42', 'c1', 'job-failed-2', 'build', 'queued', 'queued-retry-build', 760);
            """
        )
    reader = SQLiteStatusReader(path)

    output = render(build_status(reader, reader, clock=lambda: 1000))

    assert "Paused (0)" in output
    assert (
        "  owner/repo#42 build -> claude queued 16m retried 1x (job-failed-2 -> build, 4m ago)"
        in output
    )


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_status_connections_cannot_write(tmp_path: Path, journal: str) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    writer = sqlite3.connect(path)
    try:
        assert writer.execute(f"PRAGMA journal_mode = {journal}").fetchone() == (journal,)
        if journal == "wal":
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            writer.execute("UPDATE jobs SET last_activity_at = 31 WHERE id = 'running'")
            writer.commit()
            assert Path(f"{path}-wal").exists()
        connection = SQLiteStatusReader(path)._connect()
        try:
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("UPDATE jobs SET state = 'done'")
            connection.execute("PRAGMA query_only = OFF")
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("UPDATE jobs SET state = 'done'")
        finally:
            connection.close()
    finally:
        writer.close()


def directory_snapshot(directory: Path) -> dict[str, tuple[bytes | None, int]]:
    return {
        str(path.relative_to(directory)): (
            None if path.is_dir() else path.read_bytes(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(directory.rglob("*"))
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


def test_a_running_job_ages_from_its_claim_and_shows_its_last_beat() -> None:
    """#107 k: the age counts from the slot's placed_at, not the last heartbeat."""
    from lib.device_pool import Placement

    source = FakeStatusSource(
        jobs=(
            job("placed", repo="o/repo", ticket_id="12", state="running",
                created_at=0, last_activity_at=9_988, recipient="claude"),
            job("unplaced", repo="o/repo", ticket_id="13", state="running",
                created_at=9_000, last_activity_at=9_999, recipient="claude"),
        ),
    )
    placement = Placement(job_id="placed", device="box", harness="claude",
                          os_user="claude-bot", placed_at=10_000 - 42 * 60)

    output = render(build_status(source, source, clock=lambda: 10_000,
                                 placements={"placed": "box"}, slots={"placed": placement}))

    assert "    o/repo#12 build -> claude running 42m (beat 12s ago)\n" in output
    assert "    o/repo#13 build -> claude running 16m (beat 1s ago) no-placement\n" in output
    assert "running 0s" not in output


def test_status_on_a_queue_only_db_shows_the_empty_state(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    sqlite3.connect(path).execute("CREATE TABLE unrelated (x)").connection.close()
    stdout = io.StringIO()

    code = status_main(
        ["--db", str(path), "--machines", str(LOCAL_BOX_MACHINES)],
        stdout=stdout,
        clock=lambda: 0,
    )

    assert code == 0
    assert "Queue (0)\n  empty\n" in stdout.getvalue()
    assert "Paused (0)\n  empty\n" in stdout.getvalue()


def test_status_sorts_ticket_ids_naturally() -> None:
    source = FakeStatusSource(
        tracker=tuple(
            TrackerRow("o/repo", ticket_id, "github", "stuck", stuck=True)
            for ticket_id in ("10", "ENG-2", "9", "abc")
        ),
        jobs=tuple(
            job(f"j{ticket_id}", repo="o/repo", ticket_id=ticket_id, state="running",
                created_at=0, last_activity_at=0)
            for ticket_id in ("10", "9")
        ),
    )

    status = build_status(source, source, clock=lambda: 0)

    assert [row.ticket_id for row in status.paused] == ["9", "10", "abc", "ENG-2"]
    assert [row.ticket_id for row in status.device_jobs["box"]] == ["9", "10"]


def test_status_closes_every_connection_and_reads_rows_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)
    opened: list[TrackedConnection] = []
    statements: list[str] = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, factory=TrackedConnection, **kwargs)
        connection.set_trace_callback(statements.append)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)

    code = status_main(["--db", str(path)], stdout=io.StringIO(), clock=lambda: 100)

    assert code == 0
    assert opened
    assert all(connection.closed for connection in opened)
    assert sum("FROM tracked_tickets" in sql for sql in statements) == 1


class TrackedConnection(sqlite3.Connection):
    closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


def test_help_lists_status(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["--help"]) == 0
    assert "\n  status " in capsys.readouterr().out


def test_cli_routes_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "queue.sqlite3"
    create_database(path)

    assert cli_main(["status", "--db", str(path), "--machines", str(LOCAL_BOX_MACHINES)]) == 0
    assert "Queue (1)" in capsys.readouterr().out
    assert "status_main" not in (ROOT / "bin" / "orch").read_text(encoding="utf-8")
