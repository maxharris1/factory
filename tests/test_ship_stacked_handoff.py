from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.types import StackedOn
from tests.merge_queue_support import Board, sha

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

PARENT_BRANCH = "factory/issue-10"
PARENT_NOTE = "Stacked on #10. Merge #10 first."


def stacked(board: Board, base: list[str]) -> None:
    snapshot = board.snapshot

    def based(pr, *, fresh: bool = False):
        ticket = snapshot(pr, fresh=fresh)
        return replace(
            ticket,
            factory=replace(ticket.factory, base_branch=base[0]),
            pr_base=base[0],
            stacked_on=StackedOn(10, PARENT_BRANCH, "main"),
        )

    board.snapshot = based


def test_a_stacked_child_is_handed_off_only_on_the_default_branch_with_green_ci(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, ship="handoff", finish_line=None)
    base = [PARENT_BRANCH]
    stacked(board, base)
    pr = board.open("42", sha(1))
    approve = board.build_and_approve(pr)

    board.poll()
    assert board.actions.handoffs == [], "handed off on its parent's branch"
    assert board.outcome(approve) is None
    assert board.state("42")["workflow_state"] != "handed-off"

    base[0] = "release"
    board.poll()
    assert board.actions.handoffs == [], "handed off on a branch that is not the default"

    base[0] = "main"
    for status in ("not-run", "pending"):
        pr.ci[sha(1)] = status
        board.poll()
        assert board.actions.handoffs == [], f"handed off with CI {status} on the default branch"

    pr.ci[sha(1)] = "success"
    board.poll()
    board.poll()
    assert board.actions.handoffs == [sha(1)]
    assert board.outcome(approve) == "handed-off"
    assert board.state("42")["workflow_state"] == "handed-off"
    [wake] = board.wakes("done")
    assert wake["note"] == PARENT_NOTE


def test_an_unstacked_handoff_carries_no_parent_note(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="handoff", finish_line=None)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    board.poll()

    assert board.actions.handoffs == [sha(1)]
    [wake] = board.wakes("done")
    assert "note" not in wake


def test_a_stacked_child_with_a_merge_finish_line_waits_before_ready_and_the_queue(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, ship="handoff", finish_line="merge")
    base = [PARENT_BRANCH]
    stacked(board, base)
    pr = board.open("42", sha(1))
    approve = board.build_and_approve(pr)

    board.poll()
    assert board.actions.handoffs == []
    assert board.actions.merges == []
    assert board.outcome(approve) is None
    assert board.state("42")["workflow_state"] != "merge-queue"

    base[0] = "main"
    board.poll()
    board.poll()
    assert board.actions.handoffs == [sha(1)]
    assert board.actions.merges == [(True, sha(1))]
    assert board.state("42")["workflow_state"] == "merged"
