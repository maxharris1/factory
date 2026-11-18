"""#839 ac-5: the poller's cycle and the dashboard snapshot get through while
about 20 other connections keep writing to orch.db."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from lib.dashboard import collect_snapshot, write_snapshot
from lib.dispatch.cycle_clock import PHASES
from lib.dispatch.poll import PollLoop
from tests.test_poll_lock_defer import TICKETS, phases_run, read_markers, world

WRITERS = 20
POLLS = 2


def keep_writing(db: Path, stop: threading.Event, commits: list[int]) -> None:
    connection = sqlite3.connect(db, isolation_level=None, timeout=30)
    try:
        while not stop.is_set():
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO poll_deferrals (detail, recorded_at) VALUES (?, ?)",
                ("concurrent writer", time.time()),
            )
            time.sleep(0.002)
            connection.execute("COMMIT")
            commits.append(1)
            time.sleep(0.001)
    finally:
        connection.close()


def test_the_cycle_finishes_and_the_snapshot_is_written_under_twenty_writers(
    tmp_path: Path, capsys
) -> None:
    dispatcher, queue, _, source, _ = world(tmp_path)
    out = tmp_path / "dashboard"
    snapshots: list[str] = []

    def push() -> None:
        snapshot = collect_snapshot(queue.path, tmp_path / "poller.log", now=time.time())
        write_snapshot(out, snapshot)
        snapshots.append(snapshot["generated_at"])

    stop = threading.Event()
    commits: list[int] = []
    writers = [
        threading.Thread(target=keep_writing, args=(queue.path, stop, commits))
        for _ in range(WRITERS)
    ]
    for writer in writers:
        writer.start()
    poll_errors: list[Exception] = []
    try:
        while len(commits) < WRITERS:
            time.sleep(0.01)
        before = len(commits)
        PollLoop(dispatcher, source, interval_seconds=0).run(
            max_polls=POLLS, on_error=poll_errors.append, after_poll=push
        )
        after = len(commits)
    finally:
        stop.set()
        for writer in writers:
            writer.join()

    assert after > before, "the writers kept committing while the poller ran"
    assert poll_errors == []
    captured = capsys.readouterr()
    cycle_ends = [line for line in captured.out.splitlines() if "): " in line]
    assert len(cycle_ends) == POLLS, captured.out
    assert all(phases_run(line) == list(PHASES) for line in cycle_ends), cycle_ends
    assert len(snapshots) == POLLS
    status = json.loads((out / "status.json").read_text())
    assert status["generated_at"] == snapshots[-1]
    assert read_markers(queue) == set(TICKETS)
    assert "database is locked" not in captured.err
