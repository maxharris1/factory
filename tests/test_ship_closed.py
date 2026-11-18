from __future__ import annotations

from pathlib import Path

import pytest

from lib.dashboard import _read_db
from lib.github_actions import GitHubAdapterError
from tests.merge_queue_support import APPROVE, PR, Board, sha
from tests.test_dispatcher import (
    BASE_705,
    READY,
    BranchActions705,
    _jobs_705,
    _outcomes,
    _pr_705,
    _running_build_705,
    comment,
    setup,
)

UNSAT = APPROVE.replace("verdict=APPROVE", "verdict=REVISE unsat=ac-1")


def reviewed_once(board: Board) -> PR:
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    return pr


def waiting_on_max(board: Board) -> PR:
    pr = reviewed_once(board)
    board.post(pr, "revise-1", UNSAT.format(round="1/3", head=pr.head), "codex-login")
    board.poll()
    assert board.state("42")["workflow_state"] == "waiting-max"
    assert board.state("42")["approved_head"] is None
    return pr


def closed_state(board: Board) -> tuple[str, str]:
    with board.queue.connect() as connection:
        row = connection.execute(
            """
            SELECT to_state, source FROM ticket_transitions
            WHERE repo = 'owner/repo' AND ticket_id = '42'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    return str(row["to_state"]), str(row["source"])


def ship_failed_rows(board: Board) -> list[dict]:
    with board.queue.connect() as connection:
        flags = _read_db(connection, board.now[0])["attention_flags"]
    return [flag for flag in flags if flag["reason"] == "ship-failed"]


def assert_closed_out(board: Board) -> None:
    assert closed_state(board) == ("abandoned", "closed-unmerged")
    assert [job for job in board.jobs() if job["state"] == "queued"] == []
    assert board.actions.handoffs == []
    assert board.actions.merges == []
    assert ship_failed_rows(board) == []


def test_a_pr_closed_while_waiting_on_max_ends_its_ticket(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = waiting_on_max(board)
    pr.closed = True
    board.poll()

    assert_closed_out(board)
    assert board.actions.states[-1] == "abandoned"


def test_an_approve_on_a_pr_closed_while_waiting_on_max_ships_nothing(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    pr = waiting_on_max(board)
    pr.closed = True
    board.approve(pr)
    board.poll()
    board.poll()

    assert_closed_out(board)


def test_an_approve_on_a_pr_closed_during_review_ships_nothing(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = reviewed_once(board)
    pr.closed = True
    board.approve(pr)
    board.poll()
    board.poll()

    assert_closed_out(board)


def test_a_pr_closed_while_shipping_clears_the_failed_ship(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = reviewed_once(board)
    handoff = board.actions.handoff

    def closed_under_the_ship(ticket, head):
        pr.closed = True
        raise GitHubAdapterError("cannot hand off a pull request that is not open")

    board.actions.handoff = closed_under_the_ship
    board.approve(pr)
    board.poll()
    assert ship_failed_rows(board)

    board.actions.handoff = handoff
    board.poll()

    assert_closed_out(board)


@pytest.mark.job_bound
def test_a_pr_github_closed_for_an_emptied_branch_keeps_building(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, review_actions=BranchActions705(BASE_705, ahead=0))
    build = _running_build_705(queue)
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) "
            "VALUES ('owner/repo', '42', 'building')"
        )
    ready = comment("ready-base", READY.format(head=BASE_705), author="claude-login")
    queue.finish(build.id, owner="claude", handoff_comment_id=ready.id)
    dispatcher.process(_pr_705(ready, head=BASE_705, closed=True, commits=0))
    dispatcher.reconcile_jobs()

    with queue.connect() as connection:
        state = connection.execute("SELECT workflow_state FROM ticket_state").fetchone()[0]
    assert state == "building"
    assert _outcomes(queue)["ready-base"] == "rejected-ready-at-base"
    assert _jobs_705(queue) == [("build", "done", 1), ("build", "queued", 1)]
