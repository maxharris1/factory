import re
from pathlib import Path

import pytest

from tests.merge_queue_support import Board, sha

SPEC = Path(__file__).resolve().parents[1] / "docs/SPEC.md"


def _approved_head_rule() -> str:
    spec = SPEC.read_text(encoding="utf-8")
    match = re.search(r"^- \*\*A `ready` at an approved head .*$", spec, re.MULTILINE)
    assert match is not None
    return match.group(0)


@pytest.mark.parametrize(
    ("finish_line", "shipped_state"), [("ready-for-review", "handed-off"), ("merge", "merge-queue")]
)
def test_ready_after_handoff_skips_only_the_approved_commit(tmp_path, finish_line, shipped_state):
    board = Board(tmp_path, finish_line=finish_line)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    assert board.state("42")["workflow_state"] == shipped_state

    same = board.ready(pr, sha(1), "ready-again")
    board.now[0] += 1000
    board.poll()
    assert board.outcome(same) == "ready-already-shipped"
    assert len(board.jobs("review")) == 1

    board.actions.patch_ids = {sha(1): "a" * 40, sha(2): "a" * 40}
    pr.push(sha(2))
    assert board.dispatcher._approval_covers(board.snapshot(pr), sha(1), sha(2))
    equal_patch = board.ready(pr, sha(2))
    board.now[0] += 1000
    board.poll()
    assert board.outcome(equal_patch) == "queued-review"
    assert board.state("42")["workflow_state"] == "review"
    assert len(board.jobs("review")) == 2


def test_spec_states_equal_patch_ready_after_handoff_is_reviewed():
    rule = _approved_head_rule()
    assert "a ready at the shipped or approved commit is `ready-already-shipped`" in rule
    assert "re-enters review as T2 describes, even when its stable patch id equals" in rule
    assert "Only the CI-fix path skips the review of an equal patch." in rule
    assert "A ready never queues a second review" not in rule
