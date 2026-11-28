from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import lib.queue
from lib.dispatch import bookkeeping
from lib.dispatch.constants import CI_SILENT_SECONDS
from tests.merge_queue_support import sha
from tests.test_poll_lock_defer import hold_lock
from tests.test_ready_ci_gate import ci_silent, on_base
from tests.test_review_before_ci import board_with, built_with_ci_pending


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    monkeypatch.setattr(lib.queue, "BUSY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(bookkeeping, "BOOKKEEPING_BUSY_SECONDS", 0.01)
    calls: list[float] = []
    monkeypatch.setattr(bookkeeping.time, "sleep", calls.append)
    return calls


def lock_during_watch(board) -> None:
    watch = board.dispatcher._watch_ci_silence

    def locked(ticket) -> None:
        lock = hold_lock(board.queue.path)
        try:
            watch(ticket)
        finally:
            lock.execute("ROLLBACK")
            lock.close()

    board.dispatcher._watch_ci_silence = locked


def watch_errors(board) -> list[str]:
    with board.queue.connect() as connection:
        rows = connection.execute("SELECT error, detail FROM poll_errors").fetchall()
    return [row["error"] for row in rows if "_watch_ci_silence" in (row["detail"] or "")]


def watch_misses(board) -> list[str]:
    return [
        line
        for line in board.log
        if line.startswith("lock misses: ") and "_watch_ci_silence" in line
    ]


def test_a_silent_ticket_under_a_held_lock_defers_its_flag_to_the_next_cycle(
    tmp_path: Path, slept: list[float]
) -> None:
    board = board_with(tmp_path, on=False)
    on_base(board, "dev")
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head)
    board.poll()
    board.now[0] += CI_SILENT_SECONDS
    watch = board.dispatcher._watch_ci_silence
    lock_during_watch(board)

    board.poll()

    assert watch_errors(board) == []
    assert len(watch_misses(board)) == 1
    assert ci_silent(board) == []

    board.dispatcher._watch_ci_silence = watch
    board.poll()

    assert ci_silent(board) == [
        ("42", f"no check run or status on {sha(1)[:12]} after 10 minutes on CI base dev")
    ]


def test_a_flagged_ticket_writes_only_when_ci_reports(
    tmp_path: Path, slept: list[float]
) -> None:
    board = board_with(tmp_path, on=False)
    reported = on_base(board, "dev")
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head)
    board.poll()
    board.now[0] += CI_SILENT_SECONDS
    board.poll()
    assert len(ci_silent(board)) == 1
    watch = board.dispatcher._watch_ci_silence
    lock_during_watch(board)

    for _ in range(2):
        board.poll()

    assert watch_misses(board) == []
    assert slept == []
    assert len(ci_silent(board)) == 1

    reported.add(pr.head)
    board.poll()

    assert watch_errors(board) == []
    assert len(watch_misses(board)) == 1
    assert len(ci_silent(board)) == 1

    board.dispatcher._watch_ci_silence = watch
    board.poll()

    assert ci_silent(board) == []


def test_a_never_silent_ticket_takes_no_write_lock(
    tmp_path: Path, slept: list[float]
) -> None:
    board = board_with(tmp_path, on=False)
    on_base(board, "dev")
    built_with_ci_pending(board, sha(1))
    lock_during_watch(board)

    board.poll()

    assert watch_errors(board) == []
    assert watch_misses(board) == []
    assert slept == []
