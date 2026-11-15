"""#262: review.start_before_ci starts the review when a ready lands and
gates only the ship on CI."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from lib.dispatcher import CIResult
from lib.factory_md import load_factory_md, parse_factory_md
from tests.merge_queue_support import PR, Board, sha

ROOT = Path(__file__).resolve().parents[1]
VALID = {
    "base_branch": "main",
    "tickets": "github",
    "builder": {"harness": "claude"},
    "reviewer": {"harness": "codex"},
    "review": {"rounds": {"low": 1, "high": 3}, "verify": "none"},
    "ship": "handoff",
    "skills": [],
}
BAD_VALUE = "review.start_before_ci must be true or false"


def factory_text(value: object = None) -> str:
    data = copy.deepcopy(VALID)
    if value is not None:
        data["review"]["start_before_ci"] = value
    return f"---\n{yaml.safe_dump(data, sort_keys=False)}---\n"


@pytest.mark.parametrize("value", [True, False])
def test_start_before_ci_parses_a_boolean(value: bool) -> None:
    config, findings = parse_factory_md(factory_text(value))
    assert findings == []
    assert config is not None and config.start_before_ci is value


def test_start_before_ci_is_off_when_absent() -> None:
    config, findings = parse_factory_md(factory_text())
    assert findings == []
    assert config is not None and config.start_before_ci is False


@pytest.mark.parametrize("value", ["yes", 1, "true"])
def test_start_before_ci_refuses_anything_else(value: object) -> None:
    config, findings = parse_factory_md(factory_text(value))
    assert config is None
    assert [(f.severity, f.message) for f in findings] == [("error", BAD_VALUE)]

    lenient, lenient_findings = parse_factory_md(factory_text(value), strict=False)
    assert lenient is not None and lenient.start_before_ci is False
    assert [(f.severity, f.message) for f in lenient_findings] == [("warning", BAD_VALUE)]


def test_the_factory_repo_starts_reviews_before_ci() -> None:
    config, findings = load_factory_md(ROOT / "factory.md")
    assert [f for f in findings if f.severity == "error"] == []
    assert config is not None and config.start_before_ci is True


CI_LOG = "FAILED tests/test_x.py::test_y - AssertionError"


def board_with(tmp_path: Path, *, on: bool) -> Board:
    board = Board(tmp_path, ship="handoff", finish_line="merge")
    board.factory = replace(board.factory, start_before_ci=on)
    snapshot = board.snapshot

    def with_logs(pr: PR, *, fresh: bool = False):
        ticket = snapshot(pr, fresh=fresh)
        ci = {
            head: CIResult(result.status, CI_LOG if result.status == "failure" else None)
            for head, result in ticket.ci.items()
        }
        return replace(ticket, ci=ci)

    board.snapshot = with_logs
    return board


def built(board: Board, head: str) -> PR:
    """An open PR whose build finished, with CI still running at head."""
    pr = board.open("42", head)
    pr.ci[head] = "pending"
    board.go(pr, "go")
    board.poll()
    board.finish("claude")
    return pr


def reviewed(board: Board, pr: PR, head: str) -> str:
    """Push head with CI pending, post ready, and finish the review it queues."""
    pr.push(head, ci="pending")
    ready = board.ready(pr, head)
    board.poll()
    board.finish("codex")
    return ready


def instruction(job) -> str:
    return json.loads(job["detail"])["instruction"]


def test_off_a_ready_with_pending_ci_waits(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=False)
    pr = built(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()
    board.poll()

    assert board.outcome(ready) is None
    assert board.jobs("review") == []


def test_on_a_ready_with_pending_ci_queues_one_review(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()

    assert board.outcome(ready) == "queued-review"
    assert len(board.jobs("review")) == 1
    assert board.state("42")["workflow_state"] == "review"

    pr.ci[pr.head] = "success"
    board.poll()
    assert len(board.jobs("review")) == 1
    assert board.outcome(ready) == "queued-review"

    pr.ci[pr.head] = "failure"
    board.poll()
    assert len(board.jobs("review")) == 1
    assert board.jobs("fix") == []


def test_on_an_approve_with_red_ci_queues_the_ci_fix_and_ships_nothing(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built(board, sha(1))
    reviewed(board, pr, pr.head)
    pr.ci[pr.head] = "failure"
    approve = board.approve(pr)
    board.poll()

    assert board.outcome(approve) == "approve-ci-red-fix"
    [fix] = board.jobs("fix")
    assert fix["head"] == pr.head
    assert CI_LOG in instruction(fix)
    state = board.state("42")
    assert state["workflow_state"] == "fixing"
    assert state["review_round"] == 0
    assert state["approved_head"] is None
    assert board.actions.handoffs == []
    assert board.wakes("blocked") == []

    board.poll()
    assert len(board.jobs("fix")) == 1


def test_on_an_approve_waits_for_pending_ci_then_ships_on_green(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built(board, sha(1))
    reviewed(board, pr, pr.head)
    approve = board.approve(pr)
    board.poll()
    assert board.outcome(approve) is None

    pr.ci[pr.head] = "success"
    board.poll()
    assert board.outcome(approve) == "merge-queue"
    assert board.state("42")["workflow_state"] == "merged"
    assert board.actions.merges == [(True, pr.head)]


def test_on_an_approve_waits_for_pending_ci_then_fixes_on_red(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built(board, sha(1))
    reviewed(board, pr, pr.head)
    approve = board.approve(pr)
    board.poll()
    pr.ci[pr.head] = "failure"
    board.poll()

    assert board.outcome(approve) == "approve-ci-red-fix"
    assert len(board.jobs("fix")) == 1
    assert board.state("42")["approved_head"] is None


def test_on_a_revise_with_red_ci_queues_one_fix_carrying_both(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built(board, sha(1))
    reviewed(board, pr, pr.head)
    pr.ci[pr.head] = "failure"
    board.revise(pr, "1/3")
    board.poll()
    board.poll()

    [fix] = board.jobs("fix")
    text = instruction(fix)
    assert "#issuecomment-revise-42-1/3" in text or "revise-42-1/3" in text
    assert f"CI failed on {pr.head}" in text
    assert CI_LOG in text
    assert board.outcome("revise-42-1/3") == "queued-review-fix"
    assert board.state("42")["review_round"] == 1
    assert board.state("42")["gate_count"] == 1


def test_on_a_revise_waits_for_pending_ci_then_queues_one_fix(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built(board, sha(1))
    reviewed(board, pr, pr.head)
    board.revise(pr, "1/3")
    board.poll()
    assert board.outcome("revise-42-1/3") is None
    assert board.jobs("fix") == []

    pr.ci[pr.head] = "success"
    board.poll()
    board.poll()
    [fix] = board.jobs("fix")
    assert "CI failed" not in instruction(fix)
    assert board.outcome("revise-42-1/3") == "queued-review-fix"


def test_on_three_red_heads_in_a_row_mark_the_ticket_stuck(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built(board, sha(1))
    reviewed(board, pr, sha(1))
    pr.ci[sha(1)] = "failure"
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")

    reviewed(board, pr, sha(2))
    pr.ci[sha(2)] = "failure"
    board.approve(pr, "2/3")
    board.poll()
    board.finish("claude")
    assert board.state("42")["stuck"] == 0

    reviewed(board, pr, sha(3))
    pr.ci[sha(3)] = "failure"
    board.revise(pr, "2/3")
    board.poll()

    state = board.state("42")
    assert state["stuck"] == 1
    assert state["gate_count"] == 3
    assert board.outcome("revise-42-2/3") == "stuck-ci-red-3"
    assert len(board.jobs("fix")) == 2
    assert len(board.wakes("stuck")) == 1


def test_on_a_green_head_resets_the_red_count(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built(board, sha(1))
    reviewed(board, pr, sha(1))
    pr.ci[sha(1)] = "failure"
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")

    reviewed(board, pr, sha(2))
    pr.ci[sha(2)] = "failure"
    board.approve(pr, "2/3")
    board.poll()
    board.finish("claude")

    reviewed(board, pr, sha(3))
    pr.ci[sha(3)] = "success"
    board.revise(pr, "2/3")
    board.poll()
    assert board.state("42")["gate_count"] == 0
    board.finish("claude")

    reviewed(board, pr, sha(4))
    pr.ci[sha(4)] = "failure"
    board.approve(pr, "3/3")
    board.poll()

    state = board.state("42")
    assert state["stuck"] == 0
    assert state["gate_count"] == 1
    assert state["workflow_state"] == "fixing"


def test_on_a_reentry_ready_with_pending_ci_queues_the_next_round(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built(board, sha(1))
    reviewed(board, pr, sha(1))
    pr.ci[sha(1)] = "success"
    pr.mergeable, pr.mergeable_state = None, None
    board.approve(pr)
    board.poll()
    assert board.state("42")["workflow_state"] == "merge-queue"

    pr.push(sha(2), ci="pending")
    ready = board.ready(pr, sha(2))
    board.poll()

    assert board.outcome(ready) == "queued-review"
    reviews = board.jobs("review")
    assert len(reviews) == 2
    assert reviews[-1]["head"] == sha(2)
    assert json.loads(reviews[-1]["detail"])["round"].startswith("2/")
    assert board.state("42")["workflow_state"] == "review"
