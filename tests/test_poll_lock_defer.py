"""#839 ac-1: a held orch.db write lock costs the poll cycle a bounded wait."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import lib.queue
from lib.dispatch import bookkeeping
from lib.dispatch.bookkeeping import LockLedger
from lib.dispatch.cycle_clock import (
    CALM_CYCLES,
    LOCK_ERRORS,
    LOCKED_CYCLES,
    PHASES,
    POLLER_REPO,
    POLLER_TICKET,
    SLOW_CYCLE,
    SLOW_CYCLE_SECONDS,
    CycleClock,
)
from lib.dispatch.poll import PollLoop
from lib.wait import lock_backoff
from tests.test_dispatcher import (
    FakeSource,
    _attention,
    _done_build,
    _handoff_events,
    setup,
    ticket,
)
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


def test_an_attention_flag_raised_under_a_held_lock_lands_once_the_lock_is_gone(
    tmp_path: Path, slept: list[float]
) -> None:
    dispatcher, queue, _, source, _ = world(tmp_path)
    lock = hold_lock(queue.path)

    dispatcher.flag_attention("owner/repo", "42", "ship-failed", "merge ship failed")
    lock.execute("ROLLBACK")
    assert attention(queue) == []

    dispatcher.poll_once(source)

    assert attention(queue) == [("42", "ship-failed")]
    assert dispatcher.attention_outbox == {}


def test_a_clear_after_a_queued_flag_wins(tmp_path: Path, slept: list[float]) -> None:
    dispatcher, queue, _, source, _ = world(tmp_path)
    dispatcher.flag_attention("owner/repo", "42", "ship-failed", "merge ship failed")
    lock = hold_lock(queue.path)

    dispatcher.clear_attention("owner/repo", "42", "ship-failed")
    dispatcher.flag_attention("owner/repo", "43", "ship-failed", "merge ship failed")
    lock.execute("ROLLBACK")
    dispatcher.poll_once(source)

    assert attention(queue) == [("43", "ship-failed")]


def test_an_unverified_handoff_flag_survives_a_lock_released_just_before_the_audit(
    tmp_path: Path, slept: list[float]
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    _done_build(queue, "ready-x")
    with queue.connect() as connection:
        connection.executemany(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) "
            "VALUES ('owner/repo', ?, 'github', 0)",
            [(ticket_id,) for ticket_id in TICKETS],
        )
    now[0] = 200.0
    holder: list[sqlite3.Connection] = []

    class LocksThroughTheProcessPhase(FakeSource):
        def tracked_tickets(self, refs):
            if not holder:
                holder.append(hold_lock(queue.path))
            return super().tracked_tickets(refs)

    audit = dispatcher._verify_handoffs

    def release_then_audit(source, read_from):
        holder[0].execute("ROLLBACK")
        assert dispatcher.lock_ledger.spent()
        audit(source, read_from)

    dispatcher._verify_handoffs = release_then_audit
    source = LocksThroughTheProcessPhase(*(ticket(ticket_id=ticket_id) for ticket_id in TICKETS))

    dispatcher.poll_once(source)

    assert _handoff_events(queue) == ["handoff-unverified"]
    assert _attention(queue) == ["unverified-handoff"]


def test_a_cycle_over_the_threshold_flags_its_slowest_phase(tmp_path: Path) -> None:
    dispatcher, queue, _, _, _ = world(tmp_path)
    wall = [1000.0]

    class SlowRead(FakeSource):
        def tracked_tickets(self, refs):
            wall[0] += 400
            return super().tracked_tickets(refs)

    loop = PollLoop(dispatcher, SlowRead(*(ticket(ticket_id=t) for t in TICKETS)),
                    interval_seconds=0)
    loop.clock = CycleClock(wall=lambda: wall[0])

    loop.run(max_polls=1)

    assert loop.clock.last_cycle_seconds == 400.0
    (detail,) = attention_detail(queue, SLOW_CYCLE)
    assert "tracked-read 400s" in detail, detail


def test_a_slow_flag_clears_after_calm_cycles(tmp_path: Path) -> None:
    dispatcher, queue, _, source, _ = world(tmp_path)
    clock = CycleClock(wall=lambda: 0.0)
    clock.start()
    clock.end()
    clock.last_cycle_seconds = SLOW_CYCLE_SECONDS + 1
    dispatcher.watch_cycle(clock)
    assert attention_detail(queue, SLOW_CYCLE)

    clock.last_cycle_seconds = 1.0
    for _ in range(CALM_CYCLES):
        dispatcher.watch_cycle(clock)

    assert attention_detail(queue, SLOW_CYCLE) == []


def test_locked_cycles_in_a_row_flag_the_phase_once_the_lock_lifts(
    tmp_path: Path, slept: list[float]
) -> None:
    dispatcher, queue, _, source, _ = world(tmp_path)
    loop = PollLoop(dispatcher, source, sleep=lambda seconds: None, interval_seconds=0)
    lock = hold_lock(queue.path)

    loop.run(max_polls=LOCKED_CYCLES + 1)
    lock.execute("ROLLBACK")
    assert attention_detail(queue, LOCK_ERRORS) == []
    loop.run(max_polls=1)

    (detail,) = attention_detail(queue, LOCK_ERRORS)
    assert f"{LOCKED_CYCLES + 1} poll cycles in a row" in detail, detail
    assert re.search(r"most in phase [a-z-]+ \(", detail), detail


def attention(queue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [
            (str(row["ticket_id"]), str(row["reason"]))
            for row in connection.execute(
                "SELECT ticket_id, reason FROM needs_attention ORDER BY ticket_id"
            )
        ]


def attention_detail(queue, reason: str) -> list[str]:
    with queue.connect() as connection:
        return [
            str(row["detail"])
            for row in connection.execute(
                "SELECT detail FROM needs_attention WHERE repo = ? AND ticket_id = ? "
                "AND reason = ?",
                (POLLER_REPO, POLLER_TICKET, reason),
            )
        ]
