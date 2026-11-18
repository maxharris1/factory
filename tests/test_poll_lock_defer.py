"""#839 ac-1: a held orch.db write lock costs the poll cycle a bounded wait."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import lib.queue
from lib.dispatch import bookkeeping
from lib.dispatch.bookkeeping import LockLedger
from lib.dispatch.cycle_clock import PHASES, CycleClock
from lib.wait import lock_backoff
from tests.test_dispatcher import FakeSource, ticket
from tests.test_feeder import dispatcher_for, issue, make_feeder

TICKETS = ("42", "43")


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    monkeypatch.setattr(lib.queue, "BUSY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(bookkeeping, "BOOKKEEPING_BUSY_SECONDS", 0.01)
    calls: list[float] = []
    monkeypatch.setattr(bookkeeping.time, "sleep", calls.append)
    return calls


def world(tmp_path: Path, source_class=FakeSource):
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(85)]
    dispatcher = dispatcher_for(tmp_path, queue, feeder)
    log: list[str] = []
    dispatcher.log = log.append
    with queue.connect() as connection:
        connection.executemany(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) "
            "VALUES ('owner/repo', ?, 'github', 0)",
            [(ticket_id,) for ticket_id in TICKETS],
        )
    source = source_class(*(ticket(ticket_id=ticket_id) for ticket_id in TICKETS))
    return dispatcher, queue, github, source, log


def hold_lock(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    return connection


def read_markers(queue) -> set[str]:
    with queue.connect() as connection:
        return {
            str(row["ticket_id"])
            for row in connection.execute("SELECT ticket_id FROM ticket_reads")
        }


def feeder_issues(queue) -> set[str]:
    with queue.connect() as connection:
        return {str(row["issue"]) for row in connection.execute("SELECT issue FROM feeder_items")}


def phases_run(cycle_end: str) -> list[str]:
    return re.findall(r"(?:^|, )([a-z-]+)!? \d+\.\ds", cycle_end.split("): ", 1)[1])


def test_a_lock_held_through_the_cycle_defers_bookkeeping_and_every_phase_runs(
    tmp_path: Path, slept: list[float]
) -> None:
    dispatcher, queue, github, source, log = world(tmp_path)
    clock = CycleClock()
    clock.start()
    dispatcher.cycle_clock = clock
    lock = hold_lock(queue.path)

    dispatcher.poll_once(source)
    lock.execute("ROLLBACK")

    assert phases_run(clock.end()) == list(PHASES[:-1])
    (misses,) = [line for line in log if line.startswith("lock misses: ")]
    assert re.search(r"\bfeed \d+ \([^)]*_record_seen", misses), misses
    assert re.search(r"\btracked-read \d+ \([^)]*_record_reads", misses), misses
    assert len(slept) <= bookkeeping.LOCK_MISS_BUDGET * (bookkeeping.BOOKKEEPING_ATTEMPTS - 1)
    assert read_markers(queue) == set()
    assert github.pulls == {}

    before = len(log)
    dispatcher.poll_once(source)

    assert read_markers(queue) == set(TICKETS)
    assert feeder_issues(queue) == {"85"}
    assert not [line for line in log[before:] if line.startswith("lock misses: ")]


def test_a_lock_released_between_attempts_writes_the_read_markers(
    tmp_path: Path, slept: list[float], monkeypatch
) -> None:
    holder: list[sqlite3.Connection] = []

    class LocksDuringTheRead(FakeSource):
        def tracked_tickets(self, refs):
            if not holder:
                holder.append(hold_lock(queue.path))
            return super().tracked_tickets(refs)

    dispatcher, queue, _, source, log = world(tmp_path, LocksDuringTheRead)

    def release(seconds: float) -> None:
        slept.append(seconds)
        holder[0].execute("ROLLBACK")

    monkeypatch.setattr(bookkeeping.time, "sleep", release)

    dispatcher.poll_once(source)

    assert slept == [lock_backoff(1)]
    assert read_markers(queue) == set(TICKETS)
    assert not [line for line in log if line.startswith("lock misses: ")]


def test_a_deferred_intake_record_ends_the_feed_with_an_error_and_no_pr(
    tmp_path: Path, slept: list[float]
) -> None:
    lines: list[str] = []
    feeder, queue, github, _ = make_feeder(tmp_path)
    feeder.log = lines.append
    github.issues["owner/app"] = [issue(85)]
    ledger = LockLedger()
    ledger.phase = "feed"
    feeder.ledger = ledger
    lock = hold_lock(queue.path)

    report = feeder.feed_once()
    lock.execute("ROLLBACK")

    assert (report.fed, report.eligible, report.errors) == ((), 1, 1)
    assert github.pulls == {}
    assert ledger.summary() == "feed 1 (_record_seen)"
    assert len(lines) == 1


def test_an_attention_flag_is_never_dropped_on_a_held_lock(
    tmp_path: Path, slept: list[float]
) -> None:
    dispatcher, queue, _, _, _ = world(tmp_path)
    lock = hold_lock(queue.path)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        dispatcher.flag_attention("owner/repo", "42", "ship-failed", "merge ship failed")
    lock.execute("ROLLBACK")
    dispatcher.flag_attention("owner/repo", "42", "ship-failed", "merge ship failed")

    with queue.connect() as connection:
        raised = connection.execute("SELECT ticket_id, reason FROM needs_attention").fetchall()
    assert [tuple(row) for row in raised] == [("42", "ship-failed")]
