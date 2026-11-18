from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib.dispatch import bookkeeping
from lib.dispatch.bookkeeping import LockLedger, bookkeeping_write
from lib.wait import lock_backoff


@pytest.fixture(autouse=True)
def short_busy_wait(monkeypatch) -> None:
    monkeypatch.setattr(bookkeeping, "BOOKKEEPING_BUSY_SECONDS", 0.01)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "b.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE marks (key TEXT PRIMARY KEY, n INTEGER)")
    return path


def holder(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    return connection


def marks(db: Path) -> dict[str, int]:
    with sqlite3.connect(db) as connection:
        return dict(connection.execute("SELECT key, n FROM marks"))


def mark(key: str):
    def work(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO marks (key, n) VALUES (?, 1)", (key,))

    return work


def test_a_write_commits_and_returns_true(db: Path) -> None:
    ledger = LockLedger()

    assert bookkeeping_write(lambda: sqlite3.connect(db), mark("a"), site="s", ledger=ledger)

    assert marks(db) == {"a": 1}
    assert ledger.misses == 0


def test_a_returning_write_hands_its_result_out_through_the_closure(db: Path) -> None:
    counts: list[int] = []

    def work(connection: sqlite3.Connection) -> None:
        counts.append(
            connection.execute(
                "INSERT INTO marks (key, n) VALUES ('a', 7) RETURNING n"
            ).fetchone()[0]
        )

    assert bookkeeping_write(lambda: sqlite3.connect(db), work, site="s", ledger=None)

    assert counts == [7]
    assert marks(db) == {"a": 7}


def test_a_held_lock_released_between_attempts_is_retried(db: Path) -> None:
    lock = holder(db)
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        lock.execute("ROLLBACK")

    ledger = LockLedger()
    written = bookkeeping_write(
        lambda: sqlite3.connect(db), mark("a"), site="s", ledger=ledger, sleep=sleep
    )

    assert written
    assert slept == [lock_backoff(1)]
    assert marks(db) == {"a": 1}
    assert ledger.misses == 0


def test_a_lock_held_through_every_attempt_defers_under_the_open_phase(db: Path) -> None:
    lock = holder(db)
    slept: list[float] = []
    ledger = LockLedger()
    ledger.phase = "tracked-read"

    written = bookkeeping_write(
        lambda: sqlite3.connect(db), mark("a"), site="_record_reads", ledger=ledger,
        sleep=slept.append,
    )
    lock.execute("ROLLBACK")

    assert not written
    assert slept == [lock_backoff(n) for n in range(1, bookkeeping.BOOKKEEPING_ATTEMPTS)]
    assert marks(db) == {}
    assert ledger.misses == 1
    assert ledger.summary() == "tracked-read 1 (_record_reads)"


def test_a_spent_ledger_defers_without_opening_the_database(monkeypatch) -> None:
    monkeypatch.setattr(bookkeeping, "LOCK_MISS_BUDGET", 2)
    ledger = LockLedger()
    ledger.phase = "feed"
    ledger.missed("_record_seen")
    ledger.missed("_record_seen")
    assert ledger.spent()

    def connect() -> sqlite3.Connection:
        raise AssertionError("a spent ledger must not touch the database")

    assert not bookkeeping_write(connect, mark("a"), site="_set_state", ledger=ledger)
    assert ledger.misses == 3
    assert ledger.summary() == "feed 3 (_record_seen, _set_state)"


def test_a_non_lock_error_rolls_back_and_raises(db: Path) -> None:
    def work(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO marks (key, n) VALUES ('a', 1)")
        connection.execute("INSERT INTO missing (x) VALUES (1)")

    ledger = LockLedger()
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        bookkeeping_write(lambda: sqlite3.connect(db), work, site="s", ledger=ledger)

    assert marks(db) == {}
    assert ledger.misses == 0
    holder(db).execute("ROLLBACK")


def test_the_summary_counts_each_phase_in_first_miss_order() -> None:
    ledger = LockLedger()
    ledger.phase = "tracked-read"
    for _ in range(3):
        ledger.missed("_record_reads")
    ledger.phase = "feed"
    ledger.missed("_record_seen")
    ledger.missed("_record_priority")
    ledger.phase = "tracked-read"
    ledger.missed("phase")

    assert ledger.summary() == (
        "tracked-read 4 (_record_reads, phase), feed 2 (_record_seen, _record_priority)"
    )

    ledger.reset()
    assert (ledger.misses, ledger.summary()) == (0, "")
