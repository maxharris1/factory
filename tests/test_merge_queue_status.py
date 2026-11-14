"""#130: orch status and the dashboard show the watched merge-queue states."""

from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import GLOSS, JOB_STATES
from lib.status import SQLiteStatusReader, build_status, render
from tests.merge_queue_support import Board, sha


def test_status_lists_the_merge_queue_in_order_with_positions(tmp_path: Path) -> None:
    board = Board(tmp_path)
    older = board.open("50", sha(5))
    younger = board.open("43", sha(3))
    board.build_and_approve(older, mergeable=None)
    board.build_and_approve(younger, mergeable=None)
    older.conflict()
    board.poll()

    reader = SQLiteStatusReader(board.queue.path)
    output = render(build_status(reader, reader, clock=lambda: board.now[0]))

    assert "Merge queue (2)" in output
    section = output.split("Merge queue (2)\n", 1)[1].splitlines()[:2]
    assert section == [
        "  owner/repo#50 updating 1 of 2 [github]",
        "  owner/repo#43 merge-queue 2 of 2 [github]",
    ]


def test_status_has_no_merge_queue_section_when_nothing_is_queued(tmp_path: Path) -> None:
    board = Board(tmp_path, finish_line="ready-for-review")
    board.build_and_approve(board.open("42", sha(1)))
    reader = SQLiteStatusReader(board.queue.path)
    assert "Merge queue" not in render(build_status(reader, reader, clock=lambda: 0))


def test_dashboard_maps_the_update_job_and_names_the_new_states() -> None:
    assert JOB_STATES["update-from-main"] == "updating"
    assert GLOSS["merge-queue"] == "approved, waiting to merge"
    assert GLOSS["updating"] == "merging base in"


def test_status_lists_an_approval_waiting_on_its_external_approver(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="after:max")
    board.build_and_approve(board.open("42", sha(1)), mergeable=None)
    assert board.state("42")["workflow_state"] == "waiting-external"

    reader = SQLiteStatusReader(board.queue.path)
    queue = build_status(reader, reader, clock=lambda: board.now[0]).merge_queue
    assert [(row.ticket_id, row.state) for row in queue] == [("42", "waiting-external")]
