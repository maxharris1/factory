"""#1482: a stacked child in a ship: merge project never merges into its
parent's branch. It merges once retargeted, with green CI on that base."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.merge_queue_support import Board, sha

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

PARENT_BRANCH = "factory/issue-10"


def stacked_on(board: Board, base: list[str]) -> None:
    snapshot = board.snapshot

    def based(pr, *, fresh: bool = False):
        ticket = snapshot(pr, fresh=fresh)
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
