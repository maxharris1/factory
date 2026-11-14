"""#130: every merge of a finish_line: merge ticket passes one gate, and
every ship reads the approval the merge queue holds now.

One merge per repo per poll, oldest approval first, whatever path asks for
it (T4, the queue step, a re-drive, an approver's ok). An after:<approver>
ok ships the head the approval was carried to, and nothing once a go or a
re-review has cleared it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.dispatcher import ReviewShipper
from lib.hook_t8 import T8Hook
from tests.merge_queue_support import Board, sha


def approve_together(board: Board, *prs) -> None:
    """Build every PR, then post their APPROVEs in order before one poll."""
    for pr in prs:
        board.go(pr, "go-" + pr.number)
    board.poll()
    for _ in prs:
        board.finish("claude")
    for pr in prs:
        board.ready(pr, pr.head)
    board.poll()
    for _ in prs:
        board.finish("codex")
    for pr in prs:
        board.approve(pr)
    board.poll()


@pytest.mark.parametrize("ship", ["merge", "handoff"])
def test_approvals_in_one_poll_merge_one_at_a_time_oldest_first(
    tmp_path: Path, ship: str
) -> None:
    board = Board(tmp_path, ship=ship)
    older, younger = board.open("2", sha(2)), board.open("1", sha(1))
    approve_together(board, older, younger)
    assert board.actions.merges == [(True, sha(2))]
    assert board.state("1")["workflow_state"] == "merge-queue"

    board.poll()
    assert board.actions.merges == [(True, sha(2)), (True, sha(1))]


def test_a_merge_re_drive_on_a_watched_ticket_goes_through_the_queue(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="merge")
    board.build_and_approve(board.open("1", sha(1)), mergeable=None)
    approve = board.build_and_approve(board.open("2", sha(2)))
    with board.queue.connect() as connection:
        connection.execute(
            "UPDATE ship_intents SET status = 'failed', redrive = 'eligible' "
            "WHERE comment_id = ?",
            (approve,),
        )
        connection.execute(
            "UPDATE ticket_state SET workflow_state = 'ship', approved_head = NULL "
            "WHERE ticket_id = '2'"
        )

    board.poll()

    assert board.actions.merges == []
    row = board.state("2")
    assert (row["workflow_state"], row["approved_head"]) == ("merge-queue", sha(2))


def after_board(tmp_path: Path) -> Board:
    board = Board(tmp_path, ship="after:tracy-site")
    board.dispatcher.trusted_logins["github"]["tracy-login"] = {"tracy-site"}
    hook = T8Hook(ReviewShipper(board.dispatcher, board))
    hook.initialize(board.dispatcher)
    board.dispatcher.hooks = (hook,)
    board.actions.branch_tip = lambda ticket, branch: (
        board.actions.fast_forwards[-1][1] if board.actions.fast_forwards else sha(0)
    )
    return board


def post_ok(board: Board, pr, comment_id: str = "ok") -> str:
    board.post(
        pr,
        comment_id,
        "<!-- orch:v1 kind=ok from=tracy-site role=approver head=" + pr.head + " -->",
        "tracy-login",
    )
    board.poll()
    return comment_id


def test_an_ok_ships_the_head_a_trivial_update_carried_the_approval_to(tmp_path: Path) -> None:
    board = after_board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    assert board.state("42")["approved_head"] == sha(2)
    assert board.actions.approvals[-1] == (sha(2), "tracy-site")

    assert board.outcome(post_ok(board, pr)) == "shipped"
    assert board.actions.fast_forwards[-1][1] == sha(2)
    assert board.state("42")["workflow_state"] == "merged"


def test_an_ok_at_the_old_head_after_a_carry_is_refused(tmp_path: Path) -> None:
    board = after_board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    board.post(
        pr,
        "ok-old",
        "<!-- orch:v1 kind=ok from=tracy-site role=approver head=" + sha(1) + " -->",
        "tracy-login",
    )
    board.poll()
    assert board.outcome("ok-old") == "refused-ok-wrong-sha"
    assert board.actions.fast_forwards == []


def test_a_go_clears_the_approval_an_ok_would_ship(tmp_path: Path) -> None:
    board = after_board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    board.go(pr, "go-again")
    board.poll()
    assert board.state("42")["approved_head"] is None

    assert board.outcome(post_ok(board, pr)) == "rejected-ok-not-waiting"
    assert board.actions.fast_forwards == []
    assert board.state("42")["workflow_state"] == "building"
    assert [row["state"] for row in board.jobs("build")][-1] == "queued"


def test_a_re_review_clears_the_approval_an_ok_would_ship(tmp_path: Path) -> None:
    board = after_board(tmp_path)
    board.verdict = board.verdict.__class__(False, "a hand-edited hunk")
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    assert board.state("42")["workflow_state"] == "review"

    assert board.outcome(post_ok(board, pr)) == "rejected-ok-not-waiting"
    assert board.actions.fast_forwards == []


@pytest.mark.parametrize(("older_number", "younger_number"), [("2", "1"), ("1", "2")])
def test_oks_in_one_poll_ship_the_oldest_approval_first(
    tmp_path: Path, older_number: str, younger_number: str
) -> None:
    board = after_board(tmp_path)
    older = board.open(older_number, sha(int(older_number)))
    younger = board.open(younger_number, sha(int(younger_number)))
    for pr in (older, younger):
        board.go(pr, "go-" + pr.number)
    board.poll()
    for _ in (older, younger):
        board.finish("claude")
    for pr in (older, younger):
        board.ready(pr, pr.head)
    board.poll()
    for _ in (older, younger):
        board.finish("codex")
    for pr in (older, younger):
        board.approve(pr)
    for pr in (older, younger):
        board.post(
            pr,
            "ok-" + pr.number,
            "<!-- orch:v1 kind=ok from=tracy-site role=approver head=" + pr.head + " -->",
            "tracy-login",
        )
    board.poll()
    assert board.actions.fast_forwards == [("main", older.head)]
    assert board.outcome("ok-" + younger.number) is None

    board.poll()
    assert board.actions.fast_forwards == [("main", older.head), ("main", younger.head)]


def test_an_ok_waits_its_turn_behind_an_older_approval(tmp_path: Path) -> None:
    board = after_board(tmp_path)
    older = board.open("1", sha(1))
    board.build_and_approve(older, mergeable=None)
    pr = board.open("2", sha(2))
    board.build_and_approve(pr)

    ok = post_ok(board, pr)
    assert board.outcome(ok) is None
    assert board.actions.fast_forwards == []

    older.closed = True
    board.poll()
    assert board.state("1")["workflow_state"] == "abandoned"
    board.poll()
    assert board.outcome(ok) == "shipped"
    assert board.actions.fast_forwards == [("main", sha(2))]
