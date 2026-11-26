from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

import lib.queue
from lib.dispatch import bookkeeping
from lib.dispatcher import RetryableStateError
from lib.wait import retry_locked
from tests.test_approver_credential import MAX, AppFlow
from tests.test_dispatcher import handled_outcome
from tests.test_repeat import OVERRULE, capped_stop, repeat_stop, work

DECIDING = ("_held_stop", "_capped_stop", "_may_lift_cap", "_stop_decision")


@pytest.fixture
def short_waits(monkeypatch) -> list[float]:
    monkeypatch.setattr(lib.queue, "BUSY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(bookkeeping, "BOOKKEEPING_BUSY_SECONDS", 0.01)
    calls: list[float] = []
    monkeypatch.setattr(bookkeeping.time, "sleep", calls.append)
    return calls


def hold_lock(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    return connection


def lock_on_overrule(flow: AppFlow, monkeypatch) -> list[sqlite3.Connection]:
    held: list[sqlite3.Connection] = []
    original = flow.dispatcher._overrule_stop

    def locked_first(ticket, comment):
        held.append(hold_lock(flow.queue.path))
        return original(ticket, comment)

    monkeypatch.setattr(flow.dispatcher, "_overrule_stop", locked_first)
    return held


def overrule(flow: AppFlow) -> str:
    return flow.post("go-overrule", OVERRULE, MAX)


def test_an_overrule_go_is_consumed_in_the_cycle_its_lock_lifts(
    tmp_path: Path, monkeypatch, short_waits: list[float]
) -> None:
    flow = capped_stop(tmp_path)
    before = work(flow)
    held = lock_on_overrule(flow, monkeypatch)

    def release(seconds: float) -> None:
        short_waits.append(seconds)
        if held and held[0].in_transaction:
            held[0].execute("ROLLBACK")

    monkeypatch.setattr(bookkeeping.time, "sleep", release)

    assert overrule(flow) == "overruled-finding"
    assert work(flow) == {**before, "review": before["review"] + 1, "review_round": 2}
    assert len(short_waits) == 1
    assert flow.dispatcher.lock_ledger.misses == 0


def test_an_overrule_lock_held_through_every_attempt_counts_a_miss_and_waits_for_the_next_cycle(
    tmp_path: Path, monkeypatch, short_waits: list[float]
) -> None:
    flow = capped_stop(tmp_path)
    before = work(flow)
    held = lock_on_overrule(flow, monkeypatch)

    with pytest.raises(RetryableStateError):
        overrule(flow)

    assert len(short_waits) == bookkeeping.BOOKKEEPING_ATTEMPTS - 1
    assert "_overrule_stop" in flow.dispatcher.lock_ledger.summary()
    held[0].execute("ROLLBACK")
    with flow.queue.connect() as connection:
        assert connection.execute("SELECT 1 FROM handled_comments WHERE comment_id = 'go-overrule'").fetchone() is None
    assert work(flow) == before

    monkeypatch.undo()
    flow.dispatcher.process(flow.snapshot())

    assert handled_outcome(flow.queue, "go-overrule") == "overruled-finding"
    assert work(flow)["review"] == before["review"] + 1


@pytest.mark.parametrize("make", [capped_stop, repeat_stop])
def test_the_overrule_opens_no_connection_and_decides_nothing_inside_its_write_transaction(
    tmp_path: Path, monkeypatch, make
) -> None:
    flow = make(tmp_path)
    opened: list[sqlite3.Connection] = []
    nested: list[str] = []
    inside = [False]

    def writing() -> bool:
        live = []
        for connection in opened:
            try:
                live.append(connection.in_transaction)
            except sqlite3.ProgrammingError:
                continue
        return any(live)

    connect = flow.queue.connect

    def tracked_connect():
        if inside[0] and writing():
            nested.append("connect")
        connection = connect()
        opened.append(connection)
        return connection

    monkeypatch.setattr(flow.queue, "connect", tracked_connect)
    for name in DECIDING:
        original = getattr(flow.dispatcher, name)

        def deciding(*args, _name=name, _original=original, **kwargs):
            if writing():
                nested.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(flow.dispatcher, name, deciding)
    overrule_stop = flow.dispatcher._overrule_stop

    def watched(ticket, comment):
        inside[0] = True
        try:
            return overrule_stop(ticket, comment)
        finally:
            inside[0] = False

    monkeypatch.setattr(flow.dispatcher, "_overrule_stop", watched)

    assert overrule(flow) == "overruled-finding"
    assert nested == []


def test_process_pass_writes_survive_concurrent_waiter_writes(tmp_path: Path, monkeypatch) -> None:
    flow = capped_stop(tmp_path)
    before = work(flow)
    with flow.queue.connect() as connection:
        connection.execute("CREATE TABLE waiter_writes (writer INTEGER, at REAL)")
    stop = threading.Event()
    writing = threading.Event()
    errors: list[BaseException] = []
    writes = [0]

    def waiter(number: int) -> None:
        while not stop.is_set():
            def write() -> None:
                connection = sqlite3.connect(flow.queue.path, timeout=5, isolation_level=None)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    writing.set()
                    connection.execute("INSERT INTO waiter_writes VALUES (?, ?)", (number, time.time()))
                    time.sleep(0.05)
                    connection.execute("COMMIT")
                finally:
                    connection.close()

            try:
                retry_locked(write)
                writes[0] += 1
            except BaseException as exc:
                errors.append(exc)
                return
            time.sleep(0.01)

    threads = [threading.Thread(target=waiter, args=(number,)) for number in range(3)]
    original = flow.dispatcher._overrule_stop

    def under_load(ticket, comment):
        assert writing.wait(5)
        return original(ticket, comment)

    monkeypatch.setattr(flow.dispatcher, "_overrule_stop", under_load)
    for thread in threads:
        thread.start()
    try:
        outcome = overrule(flow)
    finally:
        stop.set()
        for thread in threads:
            thread.join(10)

    assert errors == []
    assert writes[0] > 0
    assert outcome == "overruled-finding"
    assert work(flow) == {**before, "review": before["review"] + 1, "review_round": 2}
