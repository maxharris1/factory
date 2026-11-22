"""Retire abandoned feeder rows (issue #1299): a fed or done row whose ticket
was abandoned, or whose PR (open, closed or merged, released or not) carries
orch:state/abandoned, leaves fed/done, stops holding its Touches paths and is
never fed again."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import lib.feeder as feeder_module
from lib.dispatch.bookkeeping import bookkeeping_write

from lib.feeder_gates import Progress, RefStatus
from lib.queue import SQLiteJobQueue
from lib.status import SQLiteStatusReader, build_status, render
from tests.test_feeder import dispatcher_for, issue, make_feeder, rows
from tests.test_feeder_gates import CLOSED_PULL, HOME, MERGED, OPEN_PULL, ref
from tests.test_feeder_idle import _job

LABELLED = RefStatus(Progress.OPEN, is_pull=True, abandoned=True)
PATH = "lib/codex_rollout.py"


def _state(tmp_path: Path, number: int, workflow: str, *, split: bool = False) -> None:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state, gate_count, stuck) "
            "VALUES (?, ?, ?, 0, 0) ON CONFLICT(repo, ticket_id) "
            "DO UPDATE SET workflow_state = excluded.workflow_state",
            (HOME, str(number), workflow),
        )
        connection.execute(
            "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
            "VALUES (?, ?, 'plan-check', ?, 1, ?)",
            (HOME, str(number), workflow, "plan-auto-split" if split else "poll"),
        )


def _fed_holder(tmp_path: Path, *, done: bool = True):
    """#1 fed as PR 501 holding lib/x.py; #2 waits on that hold."""
    feeder, queue, github, _ = make_feeder(tmp_path, box=3)
    dispatcher_for(tmp_path, queue)
    github.issues[HOME] = [issue(1, created_at=1.0, body="Touches: lib/x.py"),
                           issue(2, created_at=2.0, body="Touches: lib/x.py")]
    github.statuses = {ref(501): OPEN_PULL}
    (fed,) = feeder.feed_once().fed
    assert fed.ticket_id == "501"
    if done:
        _job(queue, "501")
    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db")["1"]["state"] == ("done" if done else "fed")
    assert rows(tmp_path / "orch.db")["2"]["state"] == "waiting-blocked"
    return feeder, queue, github


def _status(tmp_path: Path) -> str:
    reader = SQLiteStatusReader(tmp_path / "orch.db")
    return render(build_status(reader, reader, clock=lambda: 20_000,
                               feeder=reader.feeder(), attention=reader.attention()))


def test_a_done_auto_split_parent_becomes_split_parent_and_frees_its_paths(tmp_path: Path) -> None:
    feeder, _, _ = _fed_holder(tmp_path)
    _state(tmp_path, 501, "abandoned", split=True)

    report = feeder.feed_once()

    parent = rows(tmp_path / "orch.db")["1"]
    assert (parent["state"], parent["released"]) == ("split-parent", 1)
    assert parent["waiting_reason"] == "split into parts on PR #501"
    assert [item.ticket_id for item in report.fed] == ["502"]
    assert rows(tmp_path / "orch.db")["2"]["state"] == "fed"
    output = _status(tmp_path)
    assert "owner/app#1 split into parts, not fed -> PR #501" in output
    assert "#1 fed, job queued" not in output


def test_a_fed_row_whose_ticket_was_abandoned_is_retired(tmp_path: Path) -> None:
    feeder, _, _ = _fed_holder(tmp_path, done=False)
    _state(tmp_path, 501, "abandoned")

    assert [item.ticket_id for item in feeder.feed_once().fed] == ["502"]

    row = rows(tmp_path / "orch.db")["1"]
    assert (row["state"], row["released"], row["waiting_reason"]) == ("abandoned", 1, "ticket abandoned")
    assert "owner/app#1 abandoned, not fed -> PR #501" in _status(tmp_path)


def test_an_abandoned_ticket_is_never_fed_again(tmp_path: Path) -> None:
    feeder, _, github = _fed_holder(tmp_path)
    _state(tmp_path, 501, "abandoned", split=True)
    feeder.feed_once()
    calls = len(github.calls)
    posted = {key: len(items) for key, items in github.posted.items()}

    for _ in range(2):
        assert feeder.feed_once().fed == ()

    assert [call for call in github.calls[calls:] if call.startswith(("pull:", "branch:"))] == []
    assert {key: len(items) for key, items in github.posted.items()} == posted
    assert rows(tmp_path / "orch.db")["1"]["state"] == "split-parent"


def test_a_done_row_of_a_live_ticket_keeps_its_hold(tmp_path: Path) -> None:
    feeder, _, _ = _fed_holder(tmp_path)
    _state(tmp_path, 501, "building")

    assert feeder.feed_once().fed == ()

    assert (rows(tmp_path / "orch.db")["1"]["state"], rows(tmp_path / "orch.db")["1"]["released"]) == ("done", 0)
    assert rows(tmp_path / "orch.db")["2"]["state"] == "waiting-blocked"


def test_a_pr_labelled_abandoned_releases_its_hold(tmp_path: Path) -> None:
    feeder, _, github = _fed_holder(tmp_path)
    _state(tmp_path, 501, "building")
    github.statuses[ref(501)] = LABELLED

    assert [item.ticket_id for item in feeder.feed_once().fed] == ["502"]

    row = rows(tmp_path / "orch.db")["1"]
    assert (row["state"], row["released"]) == ("abandoned", 1)
    assert "#1 fed, job queued" not in _status(tmp_path)


def test_a_labelled_pr_retires_a_done_row_that_holds_nothing(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=3)
    dispatcher_for(tmp_path, queue)
    github.issues[HOME] = [issue(1, body="No paths named.")]
    (fed,) = feeder.feed_once().fed
    _job(queue, fed.ticket_id)
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["1"]["state"] == "done"
    github.statuses = {ref(int(fed.ticket_id)): LABELLED}

    feeder.feed_once()

    assert rows(tmp_path / "orch.db")["1"]["state"] == "abandoned"


PARENTS = {1191: 1195, 1192: 1200, 1206: 1214, 1207: 1213, 1208: 1215, 1254: 1258, 1276: 1284, 1064: 1099}


def _seed_parents(queue: SQLiteJobQueue) -> None:
    with queue.connect() as connection:
        for number, pull in PARENTS.items():
            connection.execute(
                "INSERT INTO feeder_items (repo, issue, tracker, title, harness, state, risk,"
                " issue_created_at, branch, pr_number, ticket_id, first_seen_at, seen_at, fed_at,"
                " touches, released) VALUES (?, ?, 'github', 't', 'claude', 'done', 'high', 1, ?, ?, ?,"
                " 1, 1, 1, ?, 0)",
                (HOME, str(number), f"factory/issue-{number}", str(pull), str(pull), f'["{PATH}"]'),
            )
            connection.execute(
                "INSERT INTO ticket_state (repo, ticket_id, workflow_state, gate_count, stuck)"
                " VALUES (?, ?, 'abandoned', 0, 0)",
                (HOME, str(pull)),
            )
            connection.execute(
                "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source)"
                " VALUES (?, ?, 'plan-check', 'abandoned', 1, 'plan-auto-split')",
                (HOME, str(pull)),
            )


def test_eight_abandoned_parents_free_a_blocked_ticket_on_the_next_poll(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=3)
    dispatcher_for(tmp_path, queue)
    _seed_parents(queue)
    github.issues[HOME] = [issue(number, created_at=1.0, body=f"Touches: {PATH}") for number in PARENTS]
    github.issues[HOME].append(issue(1290, created_at=2.0, body=f"Touches: {PATH}"))
    github.statuses = {ref(pull): OPEN_PULL for pull in PARENTS.values()}

    report = feeder.feed_once()

    found = rows(tmp_path / "orch.db")
    assert {str(n): (found[str(n)]["state"], found[str(n)]["released"]) for n in PARENTS} == {
        str(n): ("split-parent", 1) for n in PARENTS
    }
    assert len(report.fed) == 1
    assert found["1290"]["state"] == "fed"
    assert [call for call in github.calls if call.startswith("pull:")] == [f"pull:{HOME}:{report.fed[0].ticket_id}"]
    output = _status(tmp_path)
    assert "fed, job queued" not in output
    for number, pull in PARENTS.items():
        assert f"owner/app#{number} split into parts, not fed -> PR #{pull}" in output


def _done_row(tmp_path: Path):
    feeder, queue, github, _ = make_feeder(tmp_path, box=3)
    dispatcher_for(tmp_path, queue)
    github.issues[HOME] = [issue(1, body="No paths named.")]
    (fed,) = feeder.feed_once().fed
    _job(queue, fed.ticket_id)
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["1"]["state"] == "done"
    return feeder, github, ref(int(fed.ticket_id))


def test_a_closed_pr_labelled_abandoned_retires_its_row(tmp_path: Path) -> None:
    feeder, github, pull = _done_row(tmp_path)
    github.statuses = {pull: replace(CLOSED_PULL, abandoned=True)}

    feeder.feed_once()

    assert rows(tmp_path / "orch.db")["1"]["state"] == "abandoned"
    assert "#1 fed, job queued" not in _status(tmp_path)


def test_a_row_released_on_an_earlier_poll_is_retired_when_its_pr_is_labelled(tmp_path: Path) -> None:
    feeder, github, pull = _done_row(tmp_path)
    github.statuses = {pull: CLOSED_PULL}
    feeder.feed_once()
    assert (rows(tmp_path / "orch.db")["1"]["state"], rows(tmp_path / "orch.db")["1"]["released"]) == ("done", 1)
    github.statuses = {pull: LABELLED}

    feeder.feed_once()

    assert rows(tmp_path / "orch.db")["1"]["state"] == "abandoned"


@pytest.mark.parametrize("workflow", ["merged", "done"])
def test_a_released_row_of_a_finished_ticket_is_retired_when_its_pr_is_labelled(
    tmp_path: Path, workflow: str
) -> None:
    feeder, github, pull = _done_row(tmp_path)
    github.statuses = {pull: MERGED}
    feeder.feed_once()
    _state(tmp_path, int(pull.key), workflow)
    github.statuses = {pull: replace(MERGED, abandoned=True)}

    feeder.feed_once()
    feeder.feed_once()

    assert rows(tmp_path / "orch.db")["1"]["state"] == "abandoned"
    assert "#1 fed, job queued" not in _status(tmp_path)


def test_a_labelled_retirement_lost_to_a_lock_lands_on_the_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feeder, github, pull = _done_row(tmp_path)
    github.statuses = {pull: LABELLED}
    armed: list[bool] = []
    read = github.ref_statuses

    def reading(refs):
        if not armed:
            armed.append(True)
        return read(refs)

    def locked(connect, work, **kwargs):
        if armed == [True]:
            armed.append(False)
            return False
        return bookkeeping_write(connect, work, **kwargs)

    monkeypatch.setattr(github, "ref_statuses", reading)
    monkeypatch.setattr(feeder_module, "bookkeeping_write", locked)
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["1"]["state"] == "done"

    feeder.feed_once()

    assert rows(tmp_path / "orch.db")["1"]["state"] == "abandoned"
