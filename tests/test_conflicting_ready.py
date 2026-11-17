"""#255: a builder's ready on a PR that conflicts with its base gets no CI
(GitHub skips pull_request workflows on a CONFLICTING PR). After a short
grace with no check run on the ready's head, the dispatcher queues one
merge-only base-update job at that head, charging no round."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.dispatcher import BASE_UPDATE_GRACE_SECONDS
from lib.device_pool import DevicePool
from lib.job_state import LINK_DROP, LINK_DROP_BACKOFF_SECONDS, record_job_event
from lib.machines_yml import load_machines_yml
from tests.merge_queue_support import Board, sha
from tests.test_claim_placement import box_machines


def _built(board: Board, head: str):
    pr = board.open("42", head)
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    return pr


def _merge_only_jobs(board: Board) -> list:
    return [
        row
        for row in board.jobs("fix", "42")
        if json.loads(row["detail"]).get("kind") == "merge-only"
    ]


def test_ready_on_a_conflicting_pr_with_no_ci_queues_one_merge_only_job(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    pr = _built(board, sha(1))
    pr.conflict()
    del pr.ci[sha(1)]  # GitHub ran nothing on the conflicting head
    ready = board.ready(pr, sha(1))

    board.poll()
    assert _merge_only_jobs(board) == []  # still inside the grace period
    assert board.outcome(ready) is None

    board.now[0] += BASE_UPDATE_GRACE_SECONDS
    board.poll()
    board.poll()

    [job] = _merge_only_jobs(board)
    assert job["recipient"] == "claude"
    assert job["head"] == sha(1)
    assert job["source_comment_id"] == ready
    assert job["dedupe_key"] == f"base-update:owner/repo\x1f42:{sha(1)}"
    detail = json.loads(job["detail"])
    assert detail["round"] == "1/3"  # the next round, not charged
    assert detail["instruction"] == (
        f"The PR conflicts with main, so GitHub ran no CI on {sha(1)}: merge main in."
    )
    assert board.jobs("review", "42") == []
    assert board.state("42")["review_round"] == 0

    # The merge-only job pushes a merge; its fresh ready gets CI and review.
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    [review] = board.jobs("review", "42")
    assert review["head"] == sha(2)
    assert board.outcome(ready) == "stale-ready-head"
    assert len(_merge_only_jobs(board)) == 1


def test_no_base_update_while_ci_runs_or_the_pr_merges_cleanly(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = _built(board, sha(1))

    # Clean PR, CI not reported yet: just ci-pending.
    del pr.ci[sha(1)]
    ready = board.ready(pr, sha(1))
    board.now[0] += BASE_UPDATE_GRACE_SECONDS
    board.poll()
    assert _merge_only_jobs(board) == []

    # Conflicting, but a check run exists on the head: CI is coming.
    pr.conflict()
    pr.ci[sha(1)] = "pending"
    board.poll()
    assert _merge_only_jobs(board) == []
    assert board.outcome(ready) is None


def _update_jobs(board: Board) -> list:
    return [
        (row["action"], row["head"])
        for row in board.jobs(number="42")
        if row["state"] == "queued" and row["action"] in ("fix", "update-from-main")
    ]


@pytest.mark.parametrize("ship", ["handoff", "after:Max"])
def test_a_conflicting_ready_on_an_approved_pr_queues_one_update(
    tmp_path: Path, ship: str
) -> None:
    board = Board(tmp_path, ship=ship)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    pr.push(sha(2), state="dirty")
    del pr.ci[sha(2)]
    board.ready(pr, sha(2))
    board.now[0] += BASE_UPDATE_GRACE_SECONDS
    board.poll()

    assert _update_jobs(board) == [("update-from-main", sha(2))]
    assert _merge_only_jobs(board) == []


def test_the_merge_queue_skips_a_head_with_a_base_update_out(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = _built(board, sha(1))
    pr.conflict()
    del pr.ci[sha(1)]
    board.ready(pr, sha(1))
    board.now[0] += BASE_UPDATE_GRACE_SECONDS
    board.poll()
    assert len(_merge_only_jobs(board)) == 1

    with board.queue.connect() as connection:
        connection.execute(
            """
            UPDATE ticket_state
            SET workflow_state = 'merge-queue', approved_head = ?, approved_at = 1
            WHERE ticket_id = '42'
            """,
            (sha(1),),
        )
        connection.commit()
    board.poll()

    assert _update_jobs(board) == [("fix", sha(1))]
    assert board.state("42")["workflow_state"] == "merge-queue"


def _active_updates(board: Board) -> list:
    return [
        (row["action"], row["attempt"], row["head"])
        for row in board.jobs(number="42")
        if row["state"] in ("queued", "running")
        and row["action"] in ("fix", "update-from-main")
    ]


def test_a_failed_update_on_an_approved_after_max_ticket_leaves_one_update(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, ship="after:Max")
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    assert board.state("42")["workflow_state"] == "waiting-external"
    pr.push(sha(2), state="dirty")
    del pr.ci[sha(2)]
    board.ready(pr, sha(2))
    board.now[0] += 120
    board.poll()
    board.fail("claude")
    board.poll()

    assert len(_active_updates(board)) <= 1
    assert _merge_only_jobs(board) == []


def _join_the_queue(board: Board, state: str, head: str) -> None:
    with board.queue.connect() as connection:
        connection.execute(
            """
            UPDATE ticket_state
            SET workflow_state = ?, approved_head = ?, approved_at = 1
            WHERE ticket_id = '42'
            """,
            (state, head),
        )
        connection.commit()


def _base_update_queued(board: Board):
    pr = _built(board, sha(1))
    pr.conflict()
    del pr.ci[sha(1)]
    board.ready(pr, sha(1))
    board.now[0] += BASE_UPDATE_GRACE_SECONDS
    board.poll()
    assert len(_merge_only_jobs(board)) == 1
    return pr


@pytest.mark.parametrize("state", ["merge-queue", "waiting-external"])
def test_a_base_update_that_fails_once_queued_gets_no_retry_beside_the_queue_update(
    tmp_path: Path, state: str
) -> None:
    board = Board(tmp_path)
    _base_update_queued(board)
    _join_the_queue(board, state, sha(1))

    board.fail("claude")
    board.poll()

    assert _active_updates(board) == [("update-from-main", 1, sha(1))]


def test_a_link_dropped_base_update_is_not_requeued_beside_the_queue_update(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    config, findings = load_machines_yml(box_machines(tmp_path))
    assert config is not None, findings
    DevicePool(board.queue, config)
    _base_update_queued(board)
    _join_the_queue(board, "waiting-external", sha(1))

    job = board.fail("claude")
    with board.queue.connect() as connection:
        record_job_event(connection, job.id, LINK_DROP, "ssh link dropped", board.now[0])
        connection.commit()
    board.poll()
    board.now[0] += max(LINK_DROP_BACKOFF_SECONDS)
    board.poll()

    assert _active_updates(board) == [("update-from-main", 1, sha(1))]


def test_the_merge_queue_counts_a_retried_base_update(tmp_path: Path) -> None:
    board = Board(tmp_path)
    _base_update_queued(board)
    board.fail("claude")
    board.poll()
    assert _active_updates(board) == [("fix", 2, sha(1))]

    _join_the_queue(board, "waiting-external", sha(1))
    board.poll()

    assert _active_updates(board) == [("fix", 2, sha(1))]
