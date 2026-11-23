"""A `kind=rebase` comment parses, but the dispatcher does not route it yet (#1415)."""

from __future__ import annotations

from pathlib import Path

from lib.handoff import parse_comment
from tests.merge_queue_support import Board, sha

REBASE = "<!-- orch:v1 kind=rebase from=grok-bot role=orchestrator -->\nRebase onto main.\n"


def queued(board: Board) -> list[tuple[str, str, str]]:
    return [(job["id"], job["action"], job["state"]) for job in board.jobs()]


def test_a_rebase_comment_is_not_routed_yet(tmp_path: Path) -> None:
    assert parse_comment(REBASE).as_dict()["kind"] == "rebase"
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    assert board.state("42")["workflow_state"] == "review"
    before = queued(board)

    board.post(pr, "rebase-42", REBASE, "grok-login")
    board.poll()

    assert queued(board) == before
    assert board.state("42")["workflow_state"] == "review"
    assert board.outcome("rebase-42") is None
