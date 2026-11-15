from __future__ import annotations

import json
from pathlib import Path

from lib.update_check import UpdateClass
from tests.merge_queue_support import Board, sha


def test_a_revised_update_spends_a_normal_round_then_ships(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    assert len(board.jobs("update-from-main")) == 1

    board.verdict = UpdateClass(False, "a conflict hunk was not resolved to exactly one side")
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    rereview = board.jobs("review")[-1]
    assert json.loads(rereview["detail"])["round"] == "1/3"

    board.finish("codex")
    revise = board.revise(pr, "1/3")
    board.poll()
    assert board.outcome(revise) == "queued-review-fix"
    [fix] = board.jobs("fix")
    assert json.loads(fix["detail"])["round"] == "2/3"

    board.finish("claude")
    pr.push(sha(3))
    ready = board.ready(pr, sha(3))
    board.poll()
    assert board.outcome(ready) == "queued-review"
    review = board.jobs("review")[-1]
    assert review["head"] == sha(3)
    assert json.loads(review["detail"])["round"] == "2/3"

    board.finish("codex")
    approve = board.approve(pr, "2/3", comment_id="approve-round-2")
    board.poll()
    assert board.outcome(approve) == "merge-queue"
    assert board.actions.merges == [(True, sha(3))]
    assert len(board.jobs("update-from-main")) == 1
