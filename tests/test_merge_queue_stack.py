"""#1482: a stacked child in a ship: merge project never merges into its
parent's branch. It merges once retargeted, with green CI on that base."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.merge_queue_support import Board, sha
from tests.test_merge_queue_gate import after_board, post_ok

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

PARENT_BRANCH = "factory/issue-10"


def stacked_on(board: Board, base: list[str], children: tuple[str, ...] | None = None) -> None:
    snapshot = board.snapshot

    def based(pr, *, fresh: bool = False):
        ticket = snapshot(pr, fresh=fresh)
        if children is not None and pr.number not in children:
            return ticket
        return replace(
            ticket,
            factory=replace(ticket.factory, base_branch=base[0]),
            pr_base=base[0],
            stack_parent_branch=PARENT_BRANCH,
        )

    board.snapshot = based


def test_a_stacked_child_merges_only_after_its_retarget_and_green_ci(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="merge")
    base = [PARENT_BRANCH]
    stacked_on(board, base)
    pr = board.open("42", sha(1))
    approve = board.build_and_approve(pr)
    assert board.outcome(approve) == "merge-queue"

    for _ in range(2):
        board.poll()
    assert board.actions.merges == []
    assert board.state("42")["workflow_state"] == "merge-queue"
    assert any(f"stacked on {PARENT_BRANCH}" in line for line in board.log)

    base[0] = "main"
    pr.ci[sha(1)] = "pending"
    board.poll()
    assert board.actions.merges == []

    pr.ci[sha(1)] = "success"
    board.poll()
    assert board.actions.merges == [(True, sha(1))]
    assert board.state("42")["workflow_state"] == "merged"


def test_an_unstacked_ticket_on_its_own_branch_still_merges(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="merge")
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    board.poll()
    assert board.actions.merges == [(True, sha(1))]


def test_an_approved_parent_merges_ahead_of_its_waiting_child(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="merge")
    stacked_on(board, [PARENT_BRANCH], children=("43",))
    child, parent = board.open("43", sha(3)), board.open("42", sha(2))
    board.build_and_approve(child)
    board.build_and_approve(parent)

    for _ in range(3):
        board.poll()
    assert board.actions.merges == [(True, sha(2))], (
        "approved parent blocked by child waiting for its merge"
    )
    assert board.state("43")["workflow_state"] == "merge-queue"


def test_a_child_waiting_for_its_retarget_holds_back_no_one_after_a_merge_passes_it(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, ship="merge")
    stacked_on(board, [PARENT_BRANCH], children=("43",))
    child = board.open("43", sha(3))
    board.build_and_approve(child)
    board.build_and_approve(board.open("44", sha(4)))
    board.poll()
    assert board.actions.merges == [(True, sha(4))]

    board.build_and_approve(board.open("42", sha(2)))
    for _ in range(2):
        board.poll()
    assert board.actions.merges == [(True, sha(4)), (True, sha(2))]


@pytest.mark.parametrize("status", ["not-run", "pending"])
def test_a_retargeted_child_waits_for_github_ci_on_its_new_base(
    tmp_path: Path, status: str
) -> None:
    board = Board(tmp_path, ship="merge")
    base = [PARENT_BRANCH]
    stacked_on(board, base)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    board.poll()

    base[0] = "release"
    pr.ci[sha(1)] = status
    for _ in range(2):
        board.poll()
    assert board.actions.merges == [], "retargeted child merged without GitHub CI"
    assert any("42" in line and status in line for line in board.log)

    pr.ci[sha(1)] = "success"
    board.poll()
    assert board.actions.merges == [(True, sha(1))]


def test_a_retargeted_child_without_github_ci_lets_the_queue_behind_it_merge(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, ship="merge")
    base = [PARENT_BRANCH]
    stacked_on(board, base, children=("43",))
    child = board.open("43", sha(3))
    board.build_and_approve(child)
    base[0] = "release"
    child.ci[sha(3)] = "not-run"
    board.build_and_approve(board.open("44", sha(4)))

    for _ in range(2):
        board.poll()
    assert board.actions.merges == [(True, sha(4))]


def test_an_ok_never_ships_a_child_still_stacked_on_its_parent(tmp_path: Path) -> None:
    board = after_board(tmp_path)
    base = [PARENT_BRANCH]
    stacked_on(board, base)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)

    ok = post_ok(board, pr)
    board.poll()
    assert board.actions.fast_forwards == []
    assert board.outcome(ok) is None

    base[0] = "main"
    board.poll()
    assert board.outcome(ok) == "shipped"
    assert board.actions.fast_forwards == [("main", sha(1))]


def test_a_child_retargeted_to_a_base_without_github_ci_never_merges_on_retained_green_ci(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, ship="merge")
    board.factory = replace(board.factory, ci_branches=("main",))
    base = [PARENT_BRANCH]
    stacked_on(board, base)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    board.poll()

    base[0] = "release"
    ticket = board.snapshot(pr)
    assert ticket.no_ci_base == "release"
    assert ticket.ci[sha(1)].status == "success"
    for _ in range(2):
        board.poll()
    assert board.actions.merges == [], "child merged on a non-CI base using retained green CI"
    assert any("42" in line and "release" in line for line in board.log)

    base[0] = "main"
    board.poll()
    assert board.actions.merges == [(True, sha(1))]
