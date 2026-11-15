from __future__ import annotations

from pathlib import Path

import pytest

from tests.merge_queue_support import APPROVE, sha
from tests.test_review_before_ci import (
    board_with,
    built_with_ci_pending,
    reviewed_at_pending_ci,
)


def claims(board) -> list[tuple[str, int]]:
    claimed = []
    while (job := board.queue.claim("claude")) is not None:
        claimed.append((job.request.source_comment_id, job.request.attempt))
    return claimed


def test_a_ready_back_on_an_old_red_head_queues_its_fix(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-a", APPROVE.format(round="1/3", head=sha(1)), "codex-login")
    board.poll()
    assert board.outcome("approve-a") == "approve-ci-red-fix"
    board.finish("claude")
    pr.push(sha(1), ci="failure")
    ready = board.ready(pr, pr.head, "ready-back-to-a")
    board.poll()
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert claims(board) == [(ready, 1)]


def test_a_late_revise_after_a_failed_review_leaves_one_fix(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head)
    board.poll()
    board.fail("codex")
    pr.ci[pr.head] = "failure"
    board.ready(pr, pr.head, "ready-after-review-process-failed")
    board.poll()
    revise = board.revise(pr, "1/3")
    board.poll()

    assert claims(board) == [(revise, 1)]


@pytest.mark.parametrize("verdict", ["revise", "approve"])
def test_a_red_ready_before_t6_retries_a_failed_fix_leaves_one_fix(
    tmp_path: Path, verdict: str
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    pr.ci[pr.head] = "failure"
    verdict_id = getattr(board, verdict)(pr, "1/3")
    board.poll()
    board.fail("claude")
    board.ready(pr, pr.head, "ready-before-fix-t6")
    board.poll()
    board.now[0] += 600
    board.poll()

    assert claims(board) == [(verdict_id, 2)]
