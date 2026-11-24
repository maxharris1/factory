"""A capped PR frees its Touches: paths while it waits for a go (issue #270).

The feeder reads `ticket_state.workflow_state` (read-only) when it builds its
holds. A holder in `capped` keeps only its Conflicts with edges; it takes its
paths back on the poll after the round resumes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib.feeder import FEEDER_TABLE, ensure_tables
from tests.test_feeder import issue, make_feeder, rows
from tests.test_feeder_gates import CLOSED_PULL, HOME, OPEN_PULL, fed_heads, ref, states

TICKET_STATE = """
CREATE TABLE IF NOT EXISTS ticket_state (
    repo TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    stuck INTEGER NOT NULL DEFAULT 0,
    workflow_state TEXT,
    cap_extra INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, ticket_id)
)
"""


def set_state(tmp_path: Path, ticket_id: str, state: str | None, *, stuck: int = 0,
              cap_extra: int = 0) -> None:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute(TICKET_STATE)
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, stuck, workflow_state, cap_extra) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (repo, ticket_id) DO UPDATE SET "
            "stuck = excluded.stuck, workflow_state = excluded.workflow_state, "
            "cap_extra = excluded.cap_extra",
            (HOME, ticket_id, stuck, state, cap_extra),
        )


def fed_holder(tmp_path: Path, body: str = "Touches: lib/x.py"):
    """#1 fed as PR 501 (open), then #2 on lib/x.py waiting on it."""
    now = [10_000.0]
    feeder, queue, github, linear = make_feeder(tmp_path, box=3, now=now)
    log: list[str] = []
    feeder.log = log.append
    github.issues[HOME] = [issue(1, created_at=1.0, body=body)]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    github.statuses = {ref(501): OPEN_PULL}
    return feeder, github, log, now


def test_a_capped_holder_frees_its_paths_next_poll(tmp_path: Path) -> None:
    feeder, github, log, now = fed_holder(tmp_path)
    github.issues[HOME] = [issue(2, created_at=2.0, body="Touches: lib/x.py")]
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/x.py held by #1")
    set_state(tmp_path, "501", "capped")
    now[0] += 60
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]
    feeder.feed_once()
    assert log.count("feeder: released locks of owner/app#1 (capped)") == 1
    assert rows(tmp_path / "orch.db")["1"]["capped_release"] == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Touches: lib/x.py", "Touches: docs/a.md\nConflicts with: #1"),
        ("Touches: lib/x.py", "Touches: docs/a.md\nConflicts with: PR #501"),
        ("Touches: lib/x.py\nConflicts with: #2", "Touches: docs/a.md"),
    ],
)
def test_conflicts_with_a_capped_ticket_still_waits(tmp_path: Path, first, second) -> None:
    feeder, github, log, _ = fed_holder(tmp_path, body=first)
    set_state(tmp_path, "501", "capped")
    github.issues[HOME] = [issue(2, created_at=2.0, body=second)]
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on #1 (conflicts with it)")


def test_a_resumed_round_re_holds_and_a_third_ticket_waits(tmp_path: Path) -> None:
    feeder, github, log, now = fed_holder(tmp_path)
    set_state(tmp_path, "501", "capped")
    github.issues[HOME] = [issue(2, created_at=2.0, body="Touches: lib/x.py")]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]
    github.statuses[ref(502)] = OPEN_PULL
    pulls = len(github.pulls)
    set_state(tmp_path, "501", "fixing", cap_extra=1)
    github.issues[HOME].append(issue(3, created_at=3.0, body="Touches: lib/x.py"))
    now[0] += 60
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"][0] in ("fed", "done")
    assert states(tmp_path)["3"] == ("waiting-blocked", "waiting on file lib/x.py held by #1")
    assert len(github.pulls) == pulls
    feeder.feed_once()
    assert log.count("feeder: re-held locks of owner/app#1") == 1
    assert rows(tmp_path / "orch.db")["1"]["capped_release"] == 0


@pytest.mark.parametrize(
    ("state", "stuck"),
    [
        ("waiting-max", 0),
        ("stuck", 0),
        ("stuck", 1),
        ("fixing", 0),
        ("review", 0),
        ("awaiting-ok", 0),
        (None, 0),
        ("capped", 1),
    ],
)
def test_non_capped_states_keep_holding(tmp_path: Path, state, stuck) -> None:
    feeder, github, log, _ = fed_holder(tmp_path)
    set_state(tmp_path, "501", state, stuck=stuck)
    github.issues[HOME] = [issue(2, created_at=2.0, body="Touches: lib/x.py")]
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/x.py held by #1")
    assert not [line for line in log if "locks of" in line]


def test_a_holder_with_no_ticket_state_row_keeps_holding(tmp_path: Path) -> None:
    feeder, github, log, _ = fed_holder(tmp_path)
    set_state(tmp_path, "999", "capped")
    github.issues[HOME] = [issue(2, created_at=2.0, body="Touches: lib/x.py")]
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/x.py held by #1")


def test_no_ticket_state_table_reads_as_today(tmp_path: Path) -> None:
    feeder, github, log, _ = fed_holder(tmp_path)
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'ticket_state'"
        ).fetchone()
    github.issues[HOME] = [issue(2, created_at=2.0, body="Touches: lib/x.py")]
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/x.py held by #1")
    assert not [line for line in log if "locks of" in line]


def test_ticket_state_without_workflow_state_reads_as_today(tmp_path: Path) -> None:
    feeder, github, log, _ = fed_holder(tmp_path)
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute("CREATE TABLE ticket_state (repo TEXT, ticket_id TEXT, stuck INTEGER)")
        connection.execute("INSERT INTO ticket_state VALUES (?, '501', 0)", (HOME,))
    github.issues[HOME] = [issue(2, created_at=2.0, body="Touches: lib/x.py")]
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/x.py held by #1")


def test_existing_feeder_table_gains_capped_release(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        ensure_tables(connection)
        connection.execute(f"ALTER TABLE {FEEDER_TABLE} DROP COLUMN capped_release")
        ensure_tables(connection)
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({FEEDER_TABLE})")}
    assert "capped_release" in columns


@pytest.mark.parametrize("stalled", ["error", "feeding"])
def test_a_capped_holder_left_in_flight_frees_its_paths(tmp_path: Path, stalled) -> None:
    feeder, github, log, now = fed_holder(tmp_path, body="Touches: lib/x.py\nConflicts with: #3")
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute(f"UPDATE {FEEDER_TABLE} SET state = ? WHERE issue = '1'", (stalled,))
    set_state(tmp_path, "501", "capped")
    github.issues[HOME] += [
        issue(2, created_at=2.0, body="Touches: lib/x.py"),
        issue(3, created_at=3.0, body="Touches: docs/a.md"),
    ]
    now[0] += 60
    assert "factory/issue-2" in fed_heads(github, feeder.feed_once())
    assert states(tmp_path)["3"] == ("waiting-blocked", "waiting on #1 (conflicts with it)")
    assert log.count("feeder: released locks of owner/app#1 (capped)") == 1


def test_a_pr_the_factory_closed_to_restart_ci_keeps_its_hold(tmp_path: Path) -> None:
    feeder, github, log, now = fed_holder(tmp_path)
    set_state(tmp_path, "501", "review")
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute("ALTER TABLE ticket_state ADD COLUMN own_close_step TEXT")
        connection.execute("ALTER TABLE ticket_state ADD COLUMN own_close_at REAL")
        connection.execute("UPDATE ticket_state SET own_close_step = 'closing', own_close_at = ?", (now[0],))
    github.statuses = {ref(501): CLOSED_PULL}
    github.issues[HOME] = [issue(2, created_at=2.0, body="Touches: lib/x.py")]
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/x.py held by #1")
    assert rows(tmp_path / "orch.db")["1"]["released"] == 0

    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute("UPDATE ticket_state SET own_close_step = 'reopened', own_close_at = ?", (now[0],))
    now[0] += 30
    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db")["1"]["released"] == 0

    now[0] += 31
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]
