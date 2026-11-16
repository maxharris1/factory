from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.merge_queue_support import Board, sha


def board_on_base(tmp_path: Path, bases: dict[str, str]) -> Board:
    board = Board(tmp_path)
    board.factory = replace(board.factory, ci_branches=("main",))
    snapshot = board.snapshot
    board.snapshot = lambda pr, *, fresh=False: replace(
        snapshot(pr, fresh=fresh), pr_base=bases.get(pr.number)
    )
    return board


def no_ci_bases(board: Board) -> list[tuple[str, str, str]]:
    with board.queue.connect() as connection:
        return [tuple(row) for row in connection.execute("SELECT repo, ticket_id, base FROM no_ci_bases")]


def test_a_polled_ticket_records_its_base_without_ci_until_it_moves_to_a_ci_base(tmp_path: Path) -> None:
    bases = {"42": "release/1.x", "43": "main"}
    board = board_on_base(tmp_path, bases)
    board.open("42", sha(1), repo="Owner/Repo")
    board.open("43", sha(2))
    board.poll()
    assert no_ci_bases(board) == [("owner/repo", "42", "release/1.x")]

    bases["42"] = "release/2.x"
    board.poll()
    assert no_ci_bases(board) == [("owner/repo", "42", "release/2.x")]

    bases["42"] = "main"
    board.poll()
    assert no_ci_bases(board) == []
