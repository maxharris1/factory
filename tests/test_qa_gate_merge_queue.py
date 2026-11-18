from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.merge_queue_support import Board, sha
from tests.qa_support import qa_config, qa_report

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")


def _carried_approval(tmp_path: Path) -> tuple[Board, object]:
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
    return board, pr


def test_an_update_that_keeps_the_approval_merges_only_once_qa_covers_the_new_head(tmp_path: Path) -> None:
    board, pr = _carried_approval(tmp_path)

    assert board.actions.merges == []
    assert [job["head"] for job in board.jobs("qa") if job["state"] == "queued"] == [sha(2)]

    board.post(pr, "qa-2", qa_report(sha(2), "PASS"), "grok-login")
    board.finish("qa")
    board.poll()
    board.poll()

    assert board.actions.merges == [(True, sha(2))]


QA_FAILURE = "- [blocking] f-1 broken\n| qa-1 | ac-1 | Failed | https://example.test/qa |\n"


def _qa_reports(board: Board, pr, comment_id: str, result: str) -> None:
    board.post(pr, comment_id, qa_report(sha(2), result, body=QA_FAILURE), "grok-login")
    board.finish("qa")
    for _ in range(4):
        board.poll()


def test_a_carried_approval_retries_qa_once_at_the_new_head_on_an_environment_failure(tmp_path: Path) -> None:
    board, pr = _carried_approval(tmp_path)
    _qa_reports(board, pr, "qa-2", "BLOCKED")

    assert [(job["head"], job["attempt"]) for job in board.jobs("qa")] == [
        (sha(1), 1),
        (sha(2), 1),
        (sha(2), 2),
    ]
    assert (board.state("42")["workflow_state"], board.state("42")["review_round"]) == ("merge-queue", 0)
    assert board.jobs("fix") == []
    assert board.actions.merges == []


def test_a_carried_approval_parks_once_on_a_second_environment_failure(tmp_path: Path) -> None:
    board, pr = _carried_approval(tmp_path)
    _qa_reports(board, pr, "qa-2", "BLOCKED")
    _qa_reports(board, pr, "qa-3", "BLOCKED")

    state = board.state("42")
    assert (state["workflow_state"], state["review_round"], state["paused_action"]) == ("waiting-max", 0, "qa")
    assert len(board.jobs("qa-env-failed")) == 1
    assert len(board.jobs("qa")) == 3
    assert board.jobs("fix") == []
    assert board.actions.merges == []


def test_a_carried_approval_spends_the_round_on_a_qa_fail(tmp_path: Path) -> None:
    board, pr = _carried_approval(tmp_path)
    _qa_reports(board, pr, "qa-2", "FAIL")

    fixes = board.jobs("fix")
    assert [job["head"] for job in fixes] == [sha(2)]
    assert json.loads(fixes[0]["detail"])["qa_findings"]["failed"] == ["qa-1"]
    state = board.state("42")
    assert (state["workflow_state"], state["review_round"], state["approved_head"]) == ("fixing", 1, None)
    assert board.actions.merges == []
