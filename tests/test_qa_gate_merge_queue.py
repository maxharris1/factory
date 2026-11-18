from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.merge_queue_support import Board, sha
from tests.qa_support import qa_config, qa_report

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")


def test_an_update_that_keeps_the_approval_merges_only_once_qa_covers_the_new_head(tmp_path: Path) -> None:
    board = Board(tmp_path, ship="merge")
    board.factory = replace(board.factory, qa=replace(qa_config(), gate="block"))
    board.actions.patch_ids.update({sha(1): "1" * 40, sha(2): "2" * 40})
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    board.post(pr, "qa-1", qa_report(sha(1), "PASS"), "grok-login")
    board.finish("qa")
    pr.conflict()
    board.approve(pr)
    board.poll()
    assert board.state("42")["workflow_state"] == "updating"
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, pr.head)
    board.poll()
    board.poll()

    assert board.actions.merges == []
    assert [job["head"] for job in board.jobs("qa") if job["state"] == "queued"] == [sha(2)]

    board.post(pr, "qa-2", qa_report(sha(2), "PASS"), "grok-login")
    board.finish("qa")
    board.poll()
    board.poll()

    assert board.actions.merges == [(True, sha(2))]
