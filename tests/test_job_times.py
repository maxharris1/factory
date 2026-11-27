import sqlite3
from pathlib import Path

import pytest

from lib.queue import SQLiteJobQueue
from tests.job_times_support import storing_bad_times

NOW = 1_800_000_000.25
COLUMNS = ("created_at", "last_activity_at", "finished_at")
BAD = {
    "iso text": "2026-11-27T10:00:00Z",
    "blob": b"\x01\x02",
    "infinity": float("inf"),
    "minus infinity": float("-inf"),
}
GOOD = {"float": NOW, "int": int(NOW), "numeric text": str(NOW)}
INSERTS = ("INSERT", "INSERT OR IGNORE")

INSERT_TRIGGER = """CREATE TRIGGER jobs_times_insert BEFORE INSERT ON jobs BEGIN
SELECT RAISE(ABORT, 'jobs.created_at takes epoch seconds') WHERE (typeof(NEW.created_at) NOT IN ('integer', 'real') OR abs(NEW.created_at) = 9e999);
SELECT RAISE(ABORT, 'jobs.last_activity_at takes epoch seconds') WHERE (typeof(NEW.last_activity_at) NOT IN ('integer', 'real') OR abs(NEW.last_activity_at) = 9e999);
SELECT RAISE(ABORT, 'jobs.finished_at takes epoch seconds') WHERE (typeof(NEW.finished_at) NOT IN ('integer', 'real', 'null') OR abs(NEW.finished_at) = 9e999);
END"""
UPDATE_TRIGGER = """CREATE TRIGGER jobs_times_update BEFORE UPDATE OF created_at, last_activity_at, finished_at ON jobs BEGIN
SELECT RAISE(ABORT, 'jobs.created_at takes epoch seconds') WHERE NEW.created_at IS NOT OLD.created_at AND (typeof(NEW.created_at) NOT IN ('integer', 'real') OR abs(NEW.created_at) = 9e999);
SELECT RAISE(ABORT, 'jobs.last_activity_at takes epoch seconds') WHERE NEW.last_activity_at IS NOT OLD.last_activity_at AND (typeof(NEW.last_activity_at) NOT IN ('integer', 'real') OR abs(NEW.last_activity_at) = 9e999);
SELECT RAISE(ABORT, 'jobs.finished_at takes epoch seconds') WHERE NEW.finished_at IS NOT OLD.finished_at AND (typeof(NEW.finished_at) NOT IN ('integer', 'real', 'null') OR abs(NEW.finished_at) = 9e999);
END"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "orch.db"
    SQLiteJobQueue(path, clock=lambda: NOW)
    return path


def insert(connection: sqlite3.Connection, job_id: str = "j1", *, verb: str = "INSERT",
           sql: dict[str, str] | None = None, **times: object) -> None:
    values = {"created_at": NOW, "last_activity_at": NOW, "finished_at": None, **times}
    sql = sql or {}
    placeholders = ", ".join(sql.get(name, "?") for name in COLUMNS)
    connection.execute(
        f"""
        {verb} INTO jobs (id, dedupe_key, repo, ticket_id, action, recipient, source_comment_id,
            timeout_seconds, generation, attempt, state, created_at, last_activity_at, finished_at)
        VALUES (?, ?, 'Owner/Repo', '1', 'build', 'claude', 'c1', 60, 1, 1, 'queued', {placeholders})
        """,
        (job_id, job_id, *(values[name] for name in COLUMNS if name not in sql)),
    )


def stored(db: Path, job_id: str = "j1") -> tuple:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT typeof(created_at), typeof(last_activity_at), typeof(finished_at) FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()


def refused(column: str):
    return pytest.raises(sqlite3.IntegrityError, match=rf"^jobs\.{column} takes epoch seconds$")


@pytest.mark.parametrize("verb", INSERTS)
@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("value", BAD.values(), ids=BAD.keys())
def test_an_insert_of_a_bad_time_is_refused(db: Path, verb: str, column: str, value: object) -> None:
    with sqlite3.connect(db) as connection, refused(column):
        insert(connection, verb=verb, **{column: value})
    assert stored(db) is None


@pytest.mark.parametrize("verb", INSERTS)
@pytest.mark.parametrize("column", COLUMNS)
def test_an_insert_of_sqlite_datetime_now_is_refused(db: Path, verb: str, column: str) -> None:
    with sqlite3.connect(db) as connection, refused(column):
        insert(connection, verb=verb, sql={column: "datetime('now')"})
    assert stored(db) is None


@pytest.mark.parametrize("verb", INSERTS)
@pytest.mark.parametrize("column", ["created_at", "last_activity_at"])
def test_an_insert_of_a_null_required_time_is_refused_not_skipped(db: Path, verb: str, column: str) -> None:
    with sqlite3.connect(db) as connection, refused(column):
        insert(connection, verb=verb, **{column: None})
    assert stored(db) is None


@pytest.mark.parametrize("verb", INSERTS)
@pytest.mark.parametrize("value", GOOD.values(), ids=GOOD.keys())
def test_an_insert_of_epoch_seconds_passes(db: Path, verb: str, value: object) -> None:
    with sqlite3.connect(db) as connection:
        insert(connection, verb=verb, created_at=value, last_activity_at=value, finished_at=value)
    assert stored(db) == ("real", "real", "real")


def test_an_insert_with_a_null_finished_at_passes(db: Path) -> None:
    with sqlite3.connect(db) as connection:
        insert(connection, finished_at=None)
    assert stored(db) == ("real", "real", "null")


@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("value", BAD.values(), ids=BAD.keys())
def test_an_update_to_a_bad_time_is_refused(db: Path, column: str, value: object) -> None:
    with sqlite3.connect(db) as connection:
        insert(connection, finished_at=NOW)
    with sqlite3.connect(db) as connection, refused(column):
        connection.execute(f"UPDATE jobs SET {column} = ?", (value,))
    assert stored(db) == ("real", "real", "real")


@pytest.mark.parametrize("column", COLUMNS)
def test_an_update_to_sqlite_datetime_now_is_refused(db: Path, column: str) -> None:
    with sqlite3.connect(db) as connection:
        insert(connection, finished_at=NOW)
    with sqlite3.connect(db) as connection, refused(column):
        connection.execute(f"UPDATE jobs SET {column} = datetime('now')")
    assert stored(db) == ("real", "real", "real")


@pytest.mark.parametrize("column", ["created_at", "last_activity_at"])
def test_an_update_of_a_required_time_to_null_is_refused(db: Path, column: str) -> None:
    with sqlite3.connect(db) as connection:
        insert(connection)
    with sqlite3.connect(db) as connection, refused(column):
        connection.execute(f"UPDATE jobs SET {column} = NULL")


@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("value", [*GOOD.values(), None], ids=[*GOOD.keys(), "null"])
def test_an_update_to_epoch_seconds_passes(db: Path, column: str, value: object) -> None:
    if value is None and column != "finished_at":
        pytest.skip("only finished_at takes NULL")
    with sqlite3.connect(db) as connection:
        insert(connection, finished_at=NOW)
        connection.execute(f"UPDATE jobs SET {column} = ?", (value,))
    assert stored(db)[COLUMNS.index(column)] == ("null" if value is None else "real")


@pytest.mark.parametrize("column", COLUMNS)
def test_an_update_checks_a_time_only_when_it_changes(db: Path, column: str) -> None:
    with storing_bad_times(sqlite3.connect(db)) as connection:
        insert(connection, **{"finished_at": NOW, column: "2026-11-27T10:00:00Z"})
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET state = 'running'")
        for other in COLUMNS:
            if other != column:
                connection.execute(f"UPDATE jobs SET {other} = ?", (NOW + 1,))
        connection.execute(f"UPDATE jobs SET {column} = {column}")
        with refused(column):
            connection.execute(f"UPDATE jobs SET {column} = 'later'")
        assert stored(db)[COLUMNS.index(column)] == "text"
        connection.execute(f"UPDATE jobs SET {column} = ?", (NOW,))
    assert stored(db) == ("real", "real", "real")


def test_switching_triggers_off_on_one_connection_leaves_the_file_guarded(db: Path) -> None:
    with storing_bad_times(sqlite3.connect(db)) as connection:
        insert(connection, finished_at="2026-11-27T10:00:00Z")
    with sqlite3.connect(db) as second, refused("finished_at"):
        insert(second, "j2", finished_at="2026-11-27T10:00:00Z")
    assert stored(db, "j2") is None


def triggers(db: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()


def test_a_second_queue_over_the_same_file_leaves_two_triggers(db: Path) -> None:
    SQLiteJobQueue(db, clock=lambda: NOW)
    assert [name for name, _ in triggers(db)] == ["jobs_times_insert", "jobs_times_update"]


def test_the_trigger_text_is_pinned(db: Path) -> None:
    assert triggers(db) == [("jobs_times_insert", INSERT_TRIGGER), ("jobs_times_update", UPDATE_TRIGGER)], (
        "the jobs time triggers changed. CREATE TRIGGER IF NOT EXISTS means an existing orch.db keeps "
        "the old trigger, so rename both triggers and drop the old names in the same change, "
        "then update this pin"
    )
