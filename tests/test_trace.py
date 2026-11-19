import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lib import cli, trace
from lib.queue import SQLiteJobQueue

REPO = "maxharris1/factory"
TICKET = "1056"
URL = f"https://github.com/{REPO}/pull/{TICKET}"


def _dispatcher_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
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
        );
        CREATE TABLE IF NOT EXISTS handled_comments (
            repo TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            comment_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            PRIMARY KEY (repo, ticket_id, comment_id)
        );
        CREATE TABLE IF NOT EXISTS handled_times (
            repo TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            comment_id TEXT NOT NULL,
            handled_at REAL NOT NULL,
            PRIMARY KEY (repo, ticket_id, comment_id)
        );
        CREATE TABLE IF NOT EXISTS comment_links (
            repo TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            comment_id TEXT NOT NULL,
            link TEXT,
            PRIMARY KEY (repo, ticket_id, comment_id)
        );
        """
    )


class Db:
    def __init__(self, path: Path):
        self.path = path
        SQLiteJobQueue(path, clock=lambda: 0.0)
        with sqlite3.connect(path) as connection:
            _dispatcher_tables(connection)

    def job(
        self,
        job_id: str,
        *,
        created_at: float,
        state: str = "done",
        attempt: int = 1,
        action: str = "build",
        recipient: str = "claude",
        handoff: str | None = None,
        ticket: str = TICKET,
    ) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, dedupe_key, repo, ticket_id, action, recipient, source_comment_id,
                    timeout_seconds, generation, attempt, head, detail, state, created_at,
                    last_activity_at, finished_at, handoff_comment_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'c0', 7200, 1, ?, NULL, '{}', ?, ?, ?, ?, ?)
                """,
                (
                    job_id, job_id, REPO, ticket, action, recipient, attempt, state,
                    created_at, created_at + 60, created_at + 60, handoff,
                ),
            )

    def event(self, job_id: str, event: str, detail: str, at: float) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
                ) SELECT id, lower(repo), ticket_id, action, recipient, ?, ?, ?
                FROM jobs WHERE id = ?
                """,
                (event, detail, at, job_id),
            )

    def handled(self, comment_id: str, outcome: str, at: float) -> None:
        with sqlite3.connect(self.path) as connection:
            for sql, value in (
                ("INSERT INTO handled_comments VALUES (?, ?, ?, ?)", outcome),
                ("INSERT INTO handled_times VALUES (?, ?, ?, ?)", at),
                ("INSERT INTO comment_links VALUES (?, ?, ?, ?)", f"{URL}#issuecomment-{comment_id}"),
            ):
                connection.execute(sql, (REPO, TICKET, comment_id, value))


@pytest.fixture
def db(tmp_path: Path) -> Db:
    return Db(tmp_path / "orch.db")


def _by_job(db: Db) -> dict[str, trace.Attempt]:
    return {item.job_id: item for item in trace.attempts(db.path, REPO, TICKET)}


def test_clean_exit_with_an_accepted_ready(db: Db) -> None:
    db.job("j-clean", created_at=100.0, handoff="501")
    db.event("j-clean", "run-exited", "orch job run's command on pc: exit 0", 150.0)
    db.handled("501", "queued-review", 170.0)

    item = _by_job(db)["j-clean"]

    assert (item.host, item.exit_code, item.exit) == ("pc", 0, "exit 0")
    assert item.comment_id == "501"
    assert item.comment_url == f"{URL}#issuecomment-501"
    assert (item.result, item.reason) == ("accepted", "queued-review")


def test_clean_exit_whose_ready_was_rejected_as_unregistered(db: Db) -> None:
    db.job("j-unreg", created_at=100.0, state="failed")
    db.event("j-unreg", "run-exited", "orch job run's command on box: exit 0", 150.0)
    db.handled("600", "queued-build", 50.0)
    db.handled("601", "rejected-unregistered-ready", 180.0)

    item = _by_job(db)["j-unreg"]

    assert (item.host, item.exit_code) == ("box", 0)
    assert item.comment_id == "601"
    assert item.comment_url == f"{URL}#issuecomment-601"
    assert (item.result, item.reason) == ("rejected", "rejected-unregistered-ready")


def test_each_unregistered_ready_goes_to_its_own_attempt(db: Db) -> None:
    db.job("j-1", created_at=100.0, state="failed", attempt=1)
    db.job("j-2", created_at=300.0, state="failed", attempt=2)
    db.handled("701", "rejected-unregistered-ready", 200.0)
    db.handled("702", "rejected-unregistered-ready", 400.0)

    found = _by_job(db)

    assert [item.job_id for item in trace.attempts(db.path, REPO, TICKET)] == ["j-1", "j-2"]
    assert (found["j-1"].comment_id, found["j-2"].comment_id) == ("701", "702")


def test_an_unregistered_verdict_is_not_tied_to_a_build(db: Db) -> None:
    db.job("j-build", created_at=100.0, state="failed")
    db.job("j-review", created_at=150.0, state="failed", action="review", recipient="codex")
    db.handled("901", "rejected-unregistered-verdict", 200.0)

    found = _by_job(db)

    assert found["j-build"].comment_id is None
    assert found["j-review"].comment_id == "901"
    assert found["j-review"].reason == "rejected-unregistered-verdict"


def test_handoff_refused_by_the_check(db: Db) -> None:
    db.job("j-invalid", created_at=100.0, state="failed")
    db.event("j-invalid", "run-exited", "orch job run's command on pc: exit 0", 150.0)
    db.event("j-invalid", "submitted-invalid", "handoff 801: head names abc, the PR is at def", 151.0)

    item = _by_job(db)["j-invalid"]

    assert item.comment_id == "801"
    assert item.result == "rejected"
    assert item.reason == "submitted-invalid: head names abc, the PR is at def"


def test_killed_run_without_a_handoff(db: Db) -> None:
    db.job("j-killed", created_at=100.0, state="failed")
    db.event("j-killed", "run-died", "orch job run's command on pc: killed by SIGTERM (exit 143)", 150.0)

    item = _by_job(db)["j-killed"]

    assert (item.host, item.exit_code, item.exit) == ("pc", 143, "killed by SIGTERM (exit 143)")
    assert item.comment_id is None
    assert (item.result, item.reason) == ("none", "no-handoff")


def test_other_tickets_are_left_out(db: Db) -> None:
    db.job("j-mine", created_at=100.0)
    db.job("j-other", created_at=100.0, ticket="999")

    assert [item.job_id for item in trace.attempts(db.path, REPO, TICKET)] == ["j-mine"]


def test_the_database_is_opened_read_only(db: Db, monkeypatch: pytest.MonkeyPatch) -> None:
    db.job("j-clean", created_at=100.0, handoff="501")
    db.handled("501", "queued-review", 170.0)
    opened: list[str] = []
    real = sqlite3.connect

    def spy(database: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("trace ran a subprocess"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("trace ran a subprocess"))
    before = db.path.read_bytes()

    assert cli.main(["trace", f"{REPO}#{TICKET}", "--db", str(db.path)]) == 0

    assert opened and all(name.endswith("?mode=ro") for name in opened)
    assert db.path.read_bytes() == before


def test_a_missing_database_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"

    assert cli.main(["trace", f"{REPO}#{TICKET}", "--db", str(path)]) == 1
    assert not path.exists()


def test_cli_prints_each_attempt(db: Db, capsys: pytest.CaptureFixture[str]) -> None:
    db.job("j-clean", created_at=100.0, handoff="501")
    db.event("j-clean", "run-exited", "orch job run's command on pc: exit 0", 150.0)
    db.handled("501", "queued-review", 170.0)

    assert cli.main(["trace", URL, "--db", str(db.path)]) == 0

    out = capsys.readouterr().out
    for part in ("j-clean", "pc", "exit 0", f"{URL}#issuecomment-501", "accepted", "queued-review"):
        assert part in out


def test_cli_json(db: Db, capsys: pytest.CaptureFixture[str]) -> None:
    db.job("j-unreg", created_at=100.0, state="failed")
    db.event("j-unreg", "run-exited", "orch job run's command on box: exit 0", 150.0)
    db.handled("601", "rejected-unregistered-ready", 180.0)

    assert cli.main(["trace", f"{REPO}#{TICKET}", "--db", str(db.path), "--json"]) == 0

    [row] = json.loads(capsys.readouterr().out)
    assert row["job_id"] == "j-unreg"
    assert row["host"] == "box"
    assert row["exit_code"] == 0
    assert row["comment_url"] == f"{URL}#issuecomment-601"
    assert (row["result"], row["reason"]) == ("rejected", "rejected-unregistered-ready")


def test_cli_rejects_a_bad_ticket(db: Db) -> None:
    assert cli.main(["trace", "not-a-ticket", "--db", str(db.path)]) == 2
