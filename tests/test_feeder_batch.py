"""#839 ac-2: a feeder pass writes its per-candidate states in one short
transaction, not one transaction per candidate."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lib.feeder import FEEDER_TABLE
from lib.queue import JobRequest
from tests.test_feeder import issue, make_feeder, rows


class StateTransactions:
    """Counts committed transactions, on any connection, that change at
    least one feeder row's state."""

    def __init__(self, db: Path) -> None:
        self.count = 0
        with sqlite3.connect(db) as connection:
            connection.execute(
                f"CREATE TRIGGER test_state_changed AFTER UPDATE OF state ON {FEEDER_TABLE} "
                "WHEN old.state IS NOT new.state BEGIN SELECT state_changed(); END"
            )

    def watch(self, connection: sqlite3.Connection) -> sqlite3.Connection:
        open_txn = [False]
        changed = [False]

        def state_changed() -> None:
            if open_txn[0]:
                changed[0] = True
            else:
                self.count += 1

        def trace(statement: str) -> None:
            word = (statement.split(None, 1) or [""])[0].upper()
            if word == "BEGIN":
                open_txn[0], changed[0] = True, False
            elif word in ("COMMIT", "END", "ROLLBACK"):
                self.count += word != "ROLLBACK" and changed[0]
                open_txn[0] = changed[0] = False

        connection.create_function("state_changed", 0, state_changed)
        connection.set_trace_callback(trace)
        return connection


def test_twenty_waiting_candidates_take_one_state_transaction(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    queue.enqueue(
        JobRequest(repo="other/repo", ticket_id="q", action="build", recipient="claude",
                   source_comment_id="q", timeout_seconds=600),
        dedupe_key="q",
    )
    github.issues["owner/app"] = [issue(n, created_at=float(n)) for n in range(1, 21)]
    transactions = StateTransactions(tmp_path / "orch.db")
    connect = queue.connect
    queue.connect = lambda: transactions.watch(connect())  # type: ignore[method-assign]

    report = feeder.feed_once()

    assert report.fed == ()
    assert report.waiting == 20
    states = {key: row["state"] for key, row in rows(tmp_path / "orch.db").items()}
    assert states == {str(n): "waiting-capacity" for n in range(1, 21)}
    assert transactions.count == 1


def test_a_failed_feed_still_records_its_error_after_feeding_began(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    github.issues["owner/app"] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    github.fail["pull"] = 1

    report = feeder.feed_once()

    assert report.errors == 1
    assert report.waiting == 1
    states = {key: row["state"] for key, row in rows(tmp_path / "orch.db").items()}
    assert states == {"1": "error", "2": "waiting-capacity"}
    assert "fake GitHub failure in pull" in rows(tmp_path / "orch.db")["1"]["error"]
