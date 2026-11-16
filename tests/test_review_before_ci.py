from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from lib.dispatcher import CIResult
from lib.factory_md import load_factory_md, parse_factory_md
from tests.merge_queue_support import APPROVE, PR, VERDICT, Board, sha

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


def built_with_ci_pending(board: Board, head: str) -> PR:
    pr = board.open("42", head)
    pr.ci[head] = "pending"
    board.go(pr, "go")
    board.poll()
    board.finish("claude")
    return pr


def reviewed_at_pending_ci(board: Board, pr: PR, head: str, verdict_id: str | None = None) -> None:
    pr.push(head, ci="pending")
    board.ready(pr, head)
    board.poll()
    if verdict_id is None:
        board.finish("codex")
        return
    review = board.claim("codex")
    board.queue.finish(review.id, owner="codex", handoff_comment_id=verdict_id)


def instruction(job) -> str:
    return json.loads(job["detail"])["instruction"]


def test_off_a_ready_with_pending_ci_waits(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=False)
    pr = built_with_ci_pending(board, sha(1))
    ready = board.ready(pr, pr.head)
    board.poll()
    board.poll()

    assert board.outcome(ready) is None
    assert board.jobs("review") == []


def test_on_a_ready_with_pending_ci_queues_one_review(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
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
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
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
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
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
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    approve = board.approve(pr)
    board.poll()
    pr.ci[pr.head] = "failure"
    board.poll()

    assert board.outcome(approve) == "approve-ci-red-fix"
    assert len(board.jobs("fix")) == 1
    assert board.state("42")["approved_head"] is None


def test_on_a_revise_with_red_ci_queues_one_fix_carrying_both(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
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
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
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
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, sha(1))
    pr.ci[sha(1)] = "failure"
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")

    reviewed_at_pending_ci(board, pr, sha(2))
    pr.ci[sha(2)] = "failure"
    board.approve(pr, "2/3")
    board.poll()
    board.finish("claude")
    assert board.state("42")["stuck"] == 0

    reviewed_at_pending_ci(board, pr, sha(3))
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
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, sha(1))
    pr.ci[sha(1)] = "failure"
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")

    reviewed_at_pending_ci(board, pr, sha(2))
    pr.ci[sha(2)] = "failure"
    board.approve(pr, "2/3")
    board.poll()
    board.finish("claude")

    reviewed_at_pending_ci(board, pr, sha(3))
    pr.ci[sha(3)] = "success"
    board.revise(pr, "2/3")
    board.poll()
    assert board.state("42")["gate_count"] == 0
    board.finish("claude")

    reviewed_at_pending_ci(board, pr, sha(4))
    pr.ci[sha(4)] = "failure"
    board.approve(pr, "3/3")
    board.poll()

    state = board.state("42")
    assert state["stuck"] == 0
    assert state["gate_count"] == 1
    assert state["workflow_state"] == "fixing"


def test_on_a_reentry_ready_with_pending_ci_queues_the_next_round(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, sha(1))
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


def test_on_a_ci_only_fix_at_the_same_head_ships_the_approval(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    pr.ci[pr.head] = "failure"
    board.approve(pr)
    board.poll()
    board.finish("claude")

    pr.ci[pr.head] = "success"
    ready = board.ready(pr, pr.head, "ready-ci-rerun")
    board.poll()
    board.poll()

    assert board.outcome(ready) == "approval-covers:merge-queue"
    assert len(board.jobs("review")) == 1
    assert board.queue.claim("codex") is None
    assert board.actions.merges == [(True, pr.head)]


def test_on_a_ci_only_fix_after_a_retried_review_ships_the_approval(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head)
    board.poll()
    board.fail("codex")
    board.poll()
    board.now[0] += 600
    board.poll()
    assert board.finish("codex").request.attempt == 2
    pr.ci[pr.head] = "failure"
    approve = board.approve(pr)
    board.poll()
    assert board.outcome(approve) == "approve-ci-red-fix"
    board.finish("claude")

    pr.ci[pr.head] = "success"
    ready = board.ready(pr, pr.head, "ready-after-retried-review-ci-fix")
    board.poll()
    board.poll()

    assert board.outcome(ready) == "approval-covers:merge-queue"
    assert board.queue.claim("codex") is None
    assert board.actions.merges == [(True, pr.head)]


def test_on_a_reposted_ready_after_a_red_revise_queues_no_second_fix(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    pr.ci[pr.head] = "failure"
    board.revise(pr, "1/3")
    board.poll()
    ready = board.ready(pr, pr.head, "ready-reposted")
    board.poll()

    assert board.outcome(ready) == "ci-red-in-flight"
    assert [job["source_comment_id"] for job in board.jobs("fix")] == ["revise-42-1/3"]


def test_on_a_ready_reposted_during_the_review_leaves_red_ci_to_the_verdict(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    pr.push(sha(1), ci="pending")
    board.ready(pr, sha(1))
    board.poll()
    pr.ci[sha(1)] = "failure"
    ready = board.ready(pr, sha(1), "ready-reposted")
    board.poll()

    assert board.outcome(ready) == "ci-red-in-flight"
    assert board.jobs("fix") == []

    board.finish("codex")
    board.revise(pr, "1/3")
    board.poll()
    [fix] = board.jobs("fix")
    assert fix["source_comment_id"] == "revise-42-1/3"
    assert CI_LOG in instruction(fix)


def test_on_the_third_red_at_the_round_cap_marks_the_ticket_stuck(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    for number in (1, 2, 3):
        reviewed_at_pending_ci(board, pr, sha(number))
        pr.ci[pr.head] = "failure"
        board.revise(pr, f"{number}/3")
        board.poll()
        if number < 3:
            board.finish("claude")

    state = board.state("42")
    assert (state["gate_count"], state["stuck"]) == (3, 1)
    assert board.outcome("revise-42-3/3") == "stuck-ci-red-3"


def test_on_a_green_revise_at_the_round_cap_resets_the_red_count(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    for number, ci in ((1, "failure"), (2, "failure"), (3, "pending")):
        reviewed_at_pending_ci(board, pr, sha(number))
        pr.ci[pr.head] = ci
        board.revise(pr, f"{number}/3")
        board.poll()
        if number < 3:
            board.finish("claude")

    assert board.outcome("revise-42-3/3") is None
    pr.ci[pr.head] = "success"
    board.poll()

    state = board.state("42")
    assert board.outcome("revise-42-3/3") == "capped-review"
    assert (state["workflow_state"], state["gate_count"], state["stuck"]) == ("capped", 0, 0)


def test_on_a_red_ready_before_the_revise_leaves_one_combined_fix(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    pr.ci[pr.head] = "failure"
    ready = board.ready(pr, pr.head, "ready-red-before-verdict")
    board.poll()

    assert board.outcome(ready) == "ci-red-in-flight"
    assert board.jobs("fix") == []

    board.revise(pr, "1/3")
    board.poll()
    [fix] = board.jobs("fix")
    assert fix["source_comment_id"] == "revise-42-1/3"
    assert CI_LOG in instruction(fix)


@pytest.mark.parametrize("retried_review", [False, True])
def test_on_each_red_approve_queues_a_runnable_ci_fix(
    tmp_path: Path, retried_review: bool
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head)
    board.poll()
    if retried_review:
        board.fail("codex")
        board.poll()
        board.now[0] += 600
        board.poll()
    board.finish("codex")
    for cycle in (1, 2):
        pr.ci[pr.head] = "failure"
        approve = board.approve(pr, "1/3", f"approve-cycle-{cycle}")
        board.poll()
        assert board.outcome(approve) == "approve-ci-red-fix"
        fix = board.queue.claim("claude")
        assert fix is not None, f"cycle {cycle}: the red APPROVE left no runnable CI fix"
        assert fix.request.source_comment_id == approve
        board.queue.finish(fix.id, owner="claude", handoff_comment_id=f"fix-handoff-{cycle}")
        pr.push(sha(1 + cycle), ci="success")
        ready = board.ready(pr, pr.head, f"ready-cycle-{cycle}")
        board.poll()
        assert board.outcome(ready) == "queued-review"
        review = board.finish("codex")
        assert json.loads(review.request.detail)["round"].startswith("1/")
    assert [job["state"] for job in board.jobs("fix")] == ["done", "done"]


def test_on_a_red_ready_back_at_an_older_reviewed_head_queues_a_runnable_ci_fix(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-older", APPROVE.format(round="1/3", head=older), "codex-login")
    board.poll()
    [moved_fix] = board.jobs("fix")
    assert moved_fix["head"] == sha(2)
    board.finish("claude")

    pr.push(older, ci="failure")
    ready = board.ready(pr, older, "ready-back-at-older-head")
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    fix = board.queue.claim("claude")
    assert fix is not None, "the ready back at the older head left no runnable CI fix"
    assert fix.request.head == older
    assert CI_LOG in json.loads(fix.request.detail)["instruction"]


def test_a_red_ready_back_at_an_older_approved_head_queues_a_runnable_ci_fix_and_no_review(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    pr.push(older, ci="pending")
    board.ready(pr, older, "pending-review")
    board.poll()
    board.finish("codex")
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-older", APPROVE.format(round="1/3", head=older), "codex-login")
    board.poll()
    board.finish("claude")

    pr.push(older, ci="failure")
    ready = board.ready(pr, older, "ready-back-at-older-head")
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    fix = board.queue.claim("claude")
    assert fix is not None, "the return left no runnable CI fix"
    assert fix.request.head == older
    assert CI_LOG in json.loads(fix.request.detail)["instruction"]
    board.queue.finish(fix.id, owner="claude", handoff_comment_id="ci-fix")

    pr.push(older, ci="pending")
    waiting = board.ready(pr, older, "pending-ready-at-the-approved-head")
    board.poll()
    assert board.outcome(waiting) is None
    assert len(board.jobs("review")) == 1


def test_a_reposted_red_ready_after_a_finished_ci_fix_queues_no_second_fix(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=False)
    pr = built_with_ci_pending(board, sha(1))
    pr.push(sha(1), ci="failure")
    board.ready(pr, sha(1), "red-ready")
    board.poll()
    board.finish("claude")

    board.ready(pr, sha(1), "red-ready-again")
    board.poll()

    assert [job["state"] for job in board.jobs("fix")] == ["done"]
    assert board.queue.claim("claude") is None


def test_a_red_ready_before_a_finished_reviews_verdict_is_read_waits_for_one_combined_fix(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    board.ready(pr, older)
    board.poll()
    older_review = board.claim("codex")
    pr.push(sha(2), ci="pending")
    board.ready(pr, sha(2))
    board.poll()
    board.post(
        pr,
        "revise-older",
        VERDICT.format(round="1/3", head=older, verdict="REVISE", risk="high"),
        "codex-login",
    )
    unread_verdict = pr.comments.pop()
    board.queue.finish(older_review.id, owner="codex", handoff_comment_id="revise-older")

    pr.push(older, ci="failure")
    ready = board.ready(pr, older, "ready-back-at-older-head")
    board.poll()
    assert board.outcome(ready) == "ci-red-in-flight"

    pr.comments.append(unread_verdict)
    board.poll()

    [fix] = [job for job in board.jobs("fix") if job["state"] != "cancelled"]
    assert fix["source_comment_id"] == "revise-older"
    assert CI_LOG in instruction(fix)
    board.queue.finish(board.claim("claude").id, owner="claude", handoff_comment_id="fix")
    assert board.queue.claim("claude") is None


def test_a_red_ready_after_an_edited_verdict_waits_for_the_corrected_verdicts_combined_fix(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    revise = VERDICT.format(round="1/3", head=pr.head, verdict="REVISE", risk="high")
    board.post(pr, "revise-edited", revise, "codex-login")
    pr.comments[-1] = replace(pr.comments[-1], edited_at=board.now[0] + 1)
    board.poll()
    assert board.outcome("revise-edited") == "rejected-edited"

    board.post(pr, "revise-corrected", revise, "codex-login")
    unread_verdict = pr.comments.pop()
    pr.ci[pr.head] = "failure"
    ready = board.ready(pr, pr.head, "ready-red-before-corrected-verdict")
    board.poll()
    assert board.outcome(ready) == "ci-red-in-flight"

    pr.comments.append(unread_verdict)
    board.poll()

    [fix] = [job for job in board.jobs("fix") if job["state"] != "cancelled"]
    assert fix["source_comment_id"] == "revise-corrected"
    assert CI_LOG in instruction(fix)


def test_a_reposted_red_ready_after_a_return_to_the_approved_head_queues_no_extra_fix(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    pr.push(older, ci="pending")
    board.ready(pr, older, "pending-review")
    board.poll()
    board.finish("codex")
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-older", APPROVE.format(round="1/3", head=older), "codex-login")
    board.poll()
    board.finish("claude")
    pr.push(older, ci="failure")
    board.ready(pr, older, "ready-back-at-older-head")
    board.poll()
    board.finish("claude")
    fixes = len(board.jobs("fix"))

    for repost in (1, 2, 3):
        board.ready(pr, older, f"red-ready-again-{repost}")
        board.poll()
        assert len(board.jobs("fix")) == fixes, f"repost {repost} queued another fix"
        assert board.queue.claim("claude") is None


def test_a_red_ready_back_at_an_older_head_queues_a_ci_fix_while_the_handled_verdict_is_unseen(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-older", APPROVE.format(round="1/3", head=older), "codex-login")
    board.poll()
    assert board.outcome("approve-older") == "approve-ci-red-fix"
    board.finish("claude")

    pr.push(older, ci="failure")
    handled_verdict = next(item for item in pr.comments if item.id == "approve-older")
    pr.comments.remove(handled_verdict)
    board.ready(pr, older, "ready-back-at-older-head")
    board.poll()
    pr.comments.append(handled_verdict)
    board.poll()

    fix = board.queue.claim("claude")
    assert fix is not None, "the ready back at the older head left no runnable CI fix"
    assert fix.request.head == older
    assert CI_LOG in json.loads(fix.request.detail)["instruction"]


def test_remembering_verdict_heads_keeps_handled_comments_four_columns_wide(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    with board.queue.connect() as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(handled_comments)")]
        connection.execute(
            "INSERT INTO handled_comments VALUES ('owner/repo', '9', 'c9', 'handled')"
        )
    assert columns == ["repo", "ticket_id", "comment_id", "outcome"]


def test_a_remembered_verdict_keeps_the_changed_lines_the_poll_saw(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    head = sha(1)
    pr = built_with_ci_pending(board, head)
    snapshot = board.snapshot
    board.snapshot = lambda pr, **kwargs: replace(snapshot(pr, **kwargs), changed_lines=320)
    reviewed_at_pending_ci(board, pr, head)
    board.post(pr, "approve-sized", APPROVE.format(round="1/3", head=head), "codex-login")
    board.poll()
    with board.queue.connect() as connection:
        rows = connection.execute(
            "SELECT comment_id, changed_lines FROM verdict_heads"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("approve-sized", 320)]
