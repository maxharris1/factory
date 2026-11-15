"""#323: a held PR on a ship: merge, finish_line: merge repo merges from
the merge queue once the approver's ok covers its head."""

from __future__ import annotations

from pathlib import Path

from lib.factory_md import HoldConfig
from tests.merge_queue_support import Board, sha

APPROVER = "grok-login"
OK = "<!-- orch:v1 kind=ok head={head} -->\nOK after UI QA.\n"


def held_board(tmp_path: Path) -> Board:
    return Board(
        tmp_path,
        ship="merge",
        hold=HoldConfig(approver=APPROVER, paths=("src/**/*.tsx",)),
    )


def open_held(board: Board, number: str = "42", head: str = sha(1)):
    pr = board.open(number, head)
    pr.changed_files = ("src/view/Button.tsx",)
    board.build_and_approve(pr)
    return pr


def test_a_paths_hold_with_an_ok_merges_from_the_queue(tmp_path: Path) -> None:
    board = held_board(tmp_path)
    pr = open_held(board)
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert board.actions.merges == []

    board.post(pr, "ok-1", OK.format(head=pr.head), APPROVER)
    board.poll()

    assert board.actions.merges == [(True, sha(1))]
    assert board.state("42")["workflow_state"] == "merged"
