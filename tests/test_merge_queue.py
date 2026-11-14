"""#130: a finish_line: merge ticket stays watched after APPROVE until its
PR merges or closes, and the poller merges approved PRs one at a time,
oldest approval first."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.device_pool import DevicePool
from lib.dispatcher import ShipError
from lib.factory_md import HoldConfig
from lib.queue import cancel_if_finished
from tests.merge_queue_support import Board, sha
from tests.test_remote_start import config_of, machines_file


def test_approve_on_a_handoff_repo_enters_the_merge_queue_and_merges(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    approve = board.build_and_approve(pr, mergeable=None)

    assert board.outcome(approve) == "merge-queue"
    row = board.state("42")
    assert row["workflow_state"] == "merge-queue"
    assert row["approved_head"] == sha(1)
    assert row["approve_comment_id"] == approve
    assert board.actions.handoffs == [sha(1)]
    assert board.actions.merges == []
    assert board.wakes("done") == []
    assert board.actions.states[-1] == "merge-queue"

    pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()

    assert board.actions.merges == [(True, sha(1))]
    assert board.state("42")["workflow_state"] == "merged"
    assert board.actions.states[-1] == "merged"
    [wake] = board.wakes("done")
    assert wake["outcome"] == "merged"


def test_a_watched_ticket_is_polled_every_cycle(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    for _ in range(3):
        board.poll()
        assert [ref.ticket_id for ref in board.last_refs] == ["42"]
    assert board.state("42")["workflow_state"] == "merge-queue"


def test_no_merge_while_ci_is_not_green_or_the_approval_does_not_cover(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)

    pr.mergeable, pr.mergeable_state = True, "clean"
    pr.ci[sha(1)] = "pending"
    board.poll()
    pr.ci[sha(1)] = "failure"
    board.poll()
    assert board.actions.merges == []

    pr.push(sha(2))
    board.poll()
    assert board.actions.merges == []
    assert board.state("42")["workflow_state"] == "merge-queue"


def test_merges_are_serialized_oldest_approval_first(tmp_path: Path) -> None:
    board = Board(tmp_path)
    older = board.open("50", sha(5))
    younger = board.open("43", sha(3))
    board.build_and_approve(older, mergeable=None)
    board.build_and_approve(younger, mergeable=None)
    assert board.state("50")["approved_at"] < board.state("43")["approved_at"]

    for pr in (older, younger):
        pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()
    assert board.actions.merges == [(True, sha(5))]
    assert board.state("43")["workflow_state"] == "merge-queue"

    board.poll()
    assert board.actions.merges == [(True, sha(5)), (True, sha(3))]
    assert board.state("43")["workflow_state"] == "merged"


def test_only_the_queue_head_is_read_fresh(tmp_path: Path) -> None:
    board = Board(tmp_path)
    first = board.open("1", sha(1))
    second = board.open("2", sha(2))
    board.build_and_approve(first, mergeable=None)
    board.build_and_approve(second, mergeable=None)
    board.fresh_reads.clear()
    board.poll()
    assert board.fresh_reads == ["1"]


def test_queues_in_two_repos_each_merge_in_the_same_cycle(tmp_path: Path) -> None:
    board = Board(tmp_path)
    one = board.open("1", sha(1), repo="owner/one")
    two = board.open("2", sha(2), repo="owner/two")
    board.build_and_approve(one, mergeable=None)
    board.build_and_approve(two, mergeable=None)
    for pr in (one, two):
        pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()
    assert sorted(board.actions.merges) == [(True, sha(1)), (True, sha(2))]


def test_ship_merge_merges_through_the_queue_in_the_approve_poll(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="merge", finish_line=None)
    assert board.factory.finish_line == "merge"
    pr = board.open("42", sha(1))
    approve = board.build_and_approve(pr)
    assert board.outcome(approve) == "merge-queue"
    assert board.actions.merges == [(True, sha(1))]
    assert board.state("42")["workflow_state"] == "merged"


def test_ship_merge_waits_behind_an_older_approval(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="merge")
    older = board.open("1", sha(1))
    younger = board.open("2", sha(2))
    assert board.outcome(board.build_and_approve(older, mergeable=False)) == "merge-queue"
    assert board.state("1")["workflow_state"] == "updating"

    assert board.outcome(board.build_and_approve(younger)) == "merge-queue"
    assert board.actions.merges == []
    assert board.state("2")["workflow_state"] == "merge-queue"


def test_hold_still_blocks_a_queued_ship_merge(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="merge", hold=HoldConfig())
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    pr.labels = ("factory:hold",)
    board.poll()
    assert board.state("42")["workflow_state"] == "merge-queue"
    assert board.actions.merges == []

    pr.labels = ()
    board.poll()
    assert board.actions.merges == [(True, sha(2))]


def test_after_approver_is_watched_but_never_merged_by_the_queue(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="after:tracy-site", finish_line="merge")
    pr = board.open("42", sha(1))
    approve = board.build_and_approve(pr)
    assert board.outcome(approve) == "waiting-external"
    assert board.state("42")["approved_head"] == sha(1)
    board.poll()
    assert board.actions.merges == []
    assert board.actions.fast_forwards == []

    pr.conflict()
    board.poll()
    [job] = board.jobs("update-from-main")
    assert job["recipient"] == "claude"
    assert board.state("42")["workflow_state"] == "updating"


def test_ready_for_review_hands_off_and_is_never_merged_or_updated(tmp_path: Path) -> None:
    board = Board(tmp_path, finish_line="ready-for-review")
    pr = board.open("42", sha(1))
    approve = board.build_and_approve(pr)
    assert board.outcome(approve) == "handed-off"
    assert board.state("42")["approved_head"] is None
    [wake] = board.wakes("done")
    assert wake["outcome"] == "handed-off"

    pr.conflict()
    for _ in range(3):
        board.poll()
    pr.push(sha(1))
    board.poll()
    assert board.actions.merges == []
    assert board.jobs("update-from-main") == []
    assert board.state("42")["workflow_state"] == "handed-off"


def test_handoff_default_finish_line_is_ready_for_review(tmp_path: Path) -> None:
    board = Board(tmp_path, finish_line=None)
    assert board.factory.finish_line == "ready-for-review"
    pr = board.open("42", sha(1))
    assert board.outcome(board.build_and_approve(pr)) == "handed-off"
    assert board.actions.merges == []


def test_a_pr_closed_unmerged_ends_the_watch(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    assert board.state("42")["workflow_state"] == "updating"
    assert board.jobs("update-from-main")[0]["state"] == "queued"
    pr.closed = True
    board.poll()

    assert board.state("42")["workflow_state"] == "abandoned"
    assert board.jobs("update-from-main")[0]["state"] == "cancelled"
    assert board.actions.states[-1] == "abandoned"
    board.poll()
    assert board.last_refs == ()


def test_a_closed_pr_never_restarts_its_claimed_update(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pool = DevicePool(board.queue, config_of(machines_file(tmp_path, box_enabled=False)))
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    job, _ = pool.claim("claude")
    assert job.request.action == "update-from-main"
    pr.closed = True
    board.poll()
    assert board.state("42")["workflow_state"] == "abandoned"

    board.now[0] += 301
    board.poll()
    assert pool.claim("claude") is None
    assert board.jobs("update-from-main")[0]["state"] == "cancelled"


def test_an_update_that_fails_after_its_pr_closed_is_not_retried(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    job = board.claim("claude")
    pr.closed = True
    board.poll()
    board.queue.finish(job.id, owner="claude", handoff_comment_id=None)
    board.poll()
    board.poll()

    row = board.state("42")
    assert (row["workflow_state"], row["update_failures"]) == ("abandoned", 0)
    assert [job["state"] for job in board.jobs("update-from-main")] == ["failed"]


@pytest.mark.parametrize("state", ["merged", "done", "abandoned"])
def test_a_released_job_of_a_finished_ticket_is_cancelled(tmp_path: Path, state: str) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    job = board.queue.claim("claude")
    with board.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = ? WHERE ticket_id = '42'", (state,)
        )
        connection.execute("UPDATE jobs SET state = 'queued' WHERE id = ?", (job.id,))
        assert cancel_if_finished(connection, job.id, board.now[0])
    assert board.jobs("update-from-main")[0]["state"] == "cancelled"


def test_a_merge_by_hand_is_observed(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    pr.merged = True
    board.poll()
    assert board.state("42")["workflow_state"] == "merged"
    assert board.actions.merges == []


def test_a_refused_merge_marks_the_head_stuck_and_the_next_moves_up(tmp_path: Path) -> None:
    board = Board(tmp_path)
    first = board.open("1", sha(1))
    second = board.open("2", sha(2))
    board.build_and_approve(first, mergeable=None)
    board.build_and_approve(second, mergeable=None)
    for pr in (first, second):
        pr.mergeable, pr.mergeable_state = True, "clean"
    board.actions.merge_error = ShipError("merge refused: 422")
    board.poll()
    row = board.state("1")
    assert row["stuck"] == 1
    assert row["stuck_reason"] == "merge-failed"

    board.actions.merge_error = None
    board.poll()
    assert board.actions.merges == [(True, sha(2))]


def test_a_retryable_merge_refusal_waits_for_the_next_poll(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    pr.mergeable, pr.mergeable_state = True, "clean"
    error = ShipError("405 not mergeable yet")
    error.retryable = True
    board.actions.merge_error = error
    board.poll()
    assert board.state("42")["workflow_state"] == "merge-queue"
    assert board.state("42")["stuck"] == 0

    board.actions.merge_error = None
    board.poll()
    assert board.actions.merges == [(True, sha(1))]
