"""#130: an approved PR that conflicts with base gets a Claude
update-from-main job. A trivial update keeps the approval and merges; any
other update goes back to cross-family review; two failed updates stop and
ask Max."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib.brief import brief_kind
from lib.github_actions import _comment
from lib.queue import _job
from lib.update_check import UpdateClass
from tests.merge_queue_support import Board, sha
from tests.test_dispatcher import READY


def conflicted(tmp_path: Path, **options) -> tuple[Board, object, str]:
    board = Board(tmp_path, **options)
    pr = board.open("42", sha(1))
    approve = board.build_and_approve(pr, mergeable=False)
    return board, pr, approve


def test_a_conflicting_queue_head_gets_one_update_job(tmp_path: Path) -> None:
    board, pr, approve = conflicted(tmp_path)

    [job] = board.jobs("update-from-main")
    assert job["recipient"] == "claude"
    assert job["head"] == sha(1)
    assert job["source_comment_id"] == approve
    assert job["state"] == "queued"
    detail = json.loads(job["detail"])
    assert detail["round"] == "1/3"
    assert detail["base_branch"] == "main"
    assert brief_kind(_job(job)) == "merge-only"
    row = board.state("42")
    assert row["workflow_state"] == "updating"
    assert row["update_from_head"] == sha(1)
    assert board.actions.states[-1] == "updating"

    board.poll()
    board.poll()
    assert len(board.jobs("update-from-main")) == 1


def test_a_behind_queue_head_is_updated_too(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    pr.mergeable, pr.mergeable_state = True, "behind"
    board.poll()
    assert len(board.jobs("update-from-main")) == 1


def test_trivial_update_carries_the_approval_and_merges(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.finish("claude")
    pr.push(sha(2))
    ready = board.ready(pr, sha(2))
    board.poll()

    assert board.outcome(ready) == f"approval-carried:{sha(1)[:12]}->{sha(2)[:12]}"
    assert board.classified == [("owner/repo", "main", sha(1), sha(2))]
    assert [job["action"] for job in board.jobs("review")] == ["review"]
    assert board.actions.merges == [(True, sha(2))]
    row = board.state("42")
    assert row["workflow_state"] == "merged"
    assert row["approved_head"] == sha(2)
    [wake] = board.wakes("done")
    assert wake["outcome"] == "merged"


def test_trivial_update_waits_for_green_ci_before_it_is_classified(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.finish("claude")
    pr.push(sha(2), ci="pending")
    ready = board.ready(pr, sha(2))
    board.poll()
    assert board.outcome(ready) is None
    assert board.classified == []

    pr.ci[sha(2)] = "success"
    board.poll()
    assert board.outcome(ready).startswith("approval-carried:")
    assert board.actions.merges == [(True, sha(2))]


def test_non_trivial_update_is_re_reviewed_at_the_approved_round(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.verdict = UpdateClass(False, "a conflict hunk was not resolved to exactly one side")
    board.finish("claude")
    pr.push(sha(2))
    ready = board.ready(pr, sha(2))
    board.poll()

    assert board.outcome(ready) == "queued-review"
    review = board.jobs("review")[-1]
    assert review["recipient"] == "codex"
    assert review["head"] == sha(2)
    assert json.loads(review["detail"])["round"] == "1/3"
    row = board.state("42")
    assert row["workflow_state"] == "review"
    assert row["approved_head"] is None
    board.poll()
    assert board.actions.merges == []

    board.finish("codex")
    approve = board.approve(pr, "1/3", comment_id="approve-again")
    board.poll()
    assert board.outcome(approve) == "merge-queue"
    assert board.actions.merges == [(True, sha(2))]
    assert board.state("42")["workflow_state"] == "merged"


def test_no_update_check_fails_closed_to_re_review(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path, classifier=False)
    board.finish("claude")
    pr.push(sha(2))
    ready = board.ready(pr, sha(2))
    board.poll()
    assert board.outcome(ready) == "queued-review"
    assert board.actions.merges == []


def test_a_raising_update_check_fails_closed_to_re_review(tmp_path: Path) -> None:
    def broken(*args):
        raise RuntimeError("clone unavailable")

    board, pr, _approve = conflicted(tmp_path, classifier=broken)
    board.finish("claude")
    pr.push(sha(2))
    ready = board.ready(pr, sha(2))
    board.poll()
    assert board.outcome(ready) == "queued-review"


def test_red_ci_on_the_update_goes_to_a_ci_fix_and_leaves_the_queue(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.finish("claude")
    pr.push(sha(2), ci="failure")
    ready = board.ready(pr, sha(2))
    board.poll()
    assert board.outcome(ready) == "queued-ci-fix"
    assert board.classified == []
    row = board.state("42")
    assert row["workflow_state"] == "fixing"
    assert row["approved_head"] is None


def test_two_failed_update_jobs_stop_and_ask_max(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.fail("claude")
    board.poll()
    row = board.state("42")
    assert row["update_failures"] == 1
    assert row["stuck"] == 0
    assert row["workflow_state"] == "merge-queue"
    board.poll()
    row = board.state("42")
    second = board.jobs("update-from-main")
    assert len(second) == 2
    assert second[-1]["state"] == "queued"
    assert row["workflow_state"] == "updating"

    board.fail("claude")
    board.poll()
    row = board.state("42")
    assert row["stuck"] == 1
    assert row["stuck_reason"] == "update-failed-2"
    stuck = [job for job in board.jobs("stuck") if job["recipient"] == "grok"]
    assert len(stuck) == 1
    assert "ask Max" in json.loads(stuck[0]["detail"])["detail"]

    for _ in range(3):
        board.poll()
    assert len(board.jobs("update-from-main")) == 2
    assert [job["attempt"] for job in board.jobs("update-from-main")] == [1, 1]


def test_a_ready_that_still_conflicts_counts_as_a_failed_update(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.finish("claude")
    pr.push(sha(2), state="dirty")
    first = board.ready(pr, sha(2))
    board.poll()
    assert board.outcome(first) == "update-failed-1"
    assert board.state("42")["update_failures"] == 1
    assert len(board.jobs("update-from-main")) == 2

    board.finish("claude")
    pr.push(sha(3), state="dirty")
    second = board.ready(pr, sha(3))
    board.poll()
    assert board.outcome(second) == "stuck-update-failed-2"
    assert board.state("42")["stuck_reason"] == "update-failed-2"
    assert len(board.jobs("update-from-main")) == 2


def test_a_successful_update_resets_the_failure_count(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.fail("claude")
    board.poll()
    assert board.state("42")["update_failures"] == 1
    board.poll()

    board.finish("claude")
    pr.push(sha(2), state="unstable")
    pr.mergeable = None
    board.ready(pr, sha(2))
    board.poll()
    pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()
    row = board.state("42")
    assert row["update_failures"] == 0
    assert row["workflow_state"] == "merged"


def test_replay_an_approved_pr_conflicts_is_updated_and_merges_with_no_human(
    tmp_path: Path,
) -> None:
    """The #111 shape: approved, base moves, the PR conflicts. No human
    comment follows the APPROVE; only bots and the poller act."""
    board = Board(tmp_path)
    pr = board.open("111", sha(11))
    approve = board.build_and_approve(pr, mergeable=None)
    assert board.outcome(approve) == "merge-queue"
    humans = len([c for c in pr.comments if c.author == "grok-login"])

    pr.conflict()
    board.poll()
    job = board.claim("claude")
    assert job.request.action == "update-from-main"
    pr.push(sha(12), ci="pending")
    board.ready(pr, sha(12))
    board.queue.finish(job.id, owner="claude", handoff_comment_id="ready")
    board.poll()
    assert board.state("111")["workflow_state"] == "updating"
    pr.ci[sha(12)] = "success"
    board.poll()

    assert board.actions.merges == [(True, sha(12))]
    assert board.state("111")["workflow_state"] == "merged"
    assert len([c for c in pr.comments if c.author == "grok-login"]) == humans


def finish_with(board: Board, pr, body: str | None) -> None:
    """The update job finishes done; body, when given, is its handoff comment."""
    job = board.claim("claude")
    handoff = f"handoff-{job.id}"
    if body is not None:
        board.post(pr, handoff, body, "claude-login")
    board.queue.finish(job.id, owner="claude", handoff_comment_id=handoff)


def test_a_done_update_with_no_ready_counts_once_the_grace_runs_out(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    finish_with(board, pr, None)
    board.poll()
    assert board.state("42")["update_failures"] == 0
    assert board.state("42")["workflow_state"] == "updating"

    board.now[0] += 600
    board.poll()
    row = board.state("42")
    assert (row["update_failures"], row["workflow_state"]) == (1, "merge-queue")
    board.poll()
    assert len(board.jobs("update-from-main")) == 2

    finish_with(board, pr, None)
    board.now[0] += 600
    board.poll()
    row = board.state("42")
    assert (row["stuck"], row["stuck_reason"]) == (1, "update-failed-2")
    for _ in range(3):
        board.poll()
    assert len(board.jobs("update-from-main")) == 2


def test_a_done_update_whose_handoff_is_not_a_ready_counts_at_once(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    finish_with(board, pr, "Merged main; nothing else to report.")
    board.poll()
    assert board.state("42")["update_failures"] == 1


def test_a_done_update_that_asks_a_question_pauses_and_does_not_count(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    finish_with(
        board, pr, "<!-- orch:v1 kind=questions from=claude-bot role=builder -->\nWhich side?"
    )
    board.now[0] += 3600
    board.poll()
    row = board.state("42")
    assert (row["update_failures"], row["workflow_state"]) == (0, "waiting-max")


def test_a_done_update_whose_ready_was_stale_counts(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    finish_with(board, pr, READY.format(head=sha(9)))
    board.poll()
    assert board.state("42")["update_failures"] == 1


def test_a_done_update_whose_ready_waits_for_ci_does_not_count(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    pr.push(sha(2), ci="pending")
    finish_with(board, pr, READY.format(head=sha(2)))
    board.now[0] += 3600
    board.poll()
    row = board.state("42")
    assert (row["update_failures"], row["workflow_state"]) == (0, "updating")


@pytest.mark.parametrize("exit_before_poll", [False, True])
def test_one_failed_update_counts_once_and_its_retry_still_runs(
    tmp_path: Path, exit_before_poll: bool
) -> None:
    board, pr, _approve = conflicted(tmp_path)
    first = board.claim("claude")
    board.ready(pr, sha(1), "old-ready")
    if exit_before_poll:
        board.queue.finish(first.id, owner="claude", handoff_comment_id=None)
    board.poll()
    if not exit_before_poll:
        board.queue.finish(first.id, owner="claude", handoff_comment_id=None)
        board.poll()
    board.poll()

    jobs = board.jobs("update-from-main")
    row = board.state("42")
    assert [job["state"] for job in jobs] == ["failed", "queued"]
    assert row["update_failures"] == 1
    assert row["stuck"] == 0
    retry = board.queue.claim("claude")
    assert retry is not None and retry.request.action == "update-from-main"


def _github_ready(board: Board, pr, comment_id: str, at: float) -> None:
    """A ready as the GitHub comment parser builds it: whole seconds."""
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pr.comments.append(
        _comment(
            {
                "id": comment_id,
                "user": {"login": "claude-login"},
                "body": READY.format(head=sha(1)),
                "created_at": stamp,
            }
        )
    )


def test_a_ready_in_its_jobs_second_is_that_jobs_failure(tmp_path: Path) -> None:
    """The job is queued at 133.5 and its ready posted at 133.6, which GitHub
    reports as 133: the ready and the failed exit are still one attempt."""
    board = Board(tmp_path)
    board.now[0] = 132.5
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    first = board.claim("claude")
    assert int(first.created_at) < first.created_at
    _github_ready(board, pr, "500", first.created_at + 0.1)
    assert pr.comments[-1].created_at < first.created_at
    board.queue.finish(first.id, owner="claude", handoff_comment_id=None)
    board.poll()
    board.poll()

    row = board.state("42")
    assert (row["update_failures"], row["stuck"]) == (1, 0)
    assert [job["state"] for job in board.jobs("update-from-main")] == ["failed", "queued"]
    assert board.queue.claim("claude").request.action == "update-from-main"


def test_a_late_ready_from_the_first_job_is_not_its_unrun_retrys(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.claim("claude")
    board.ready(pr, sha(1), "first-old-ready")
    board.poll()
    assert [job["state"] for job in board.jobs("update-from-main")] == ["running", "queued"]
    board.ready(pr, sha(1), "first-late-old-ready")
    board.poll()

    row = board.state("42")
    assert (row["update_failures"], row["stuck"]) == (1, 0)
    assert [job["state"] for job in board.jobs("update-from-main")] == ["running", "queued"]


def test_the_retrys_own_old_head_ready_is_the_second_failure(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.claim("claude")
    board.ready(pr, sha(1), "first-old-ready")
    board.poll()
    board.claim("claude")
    board.ready(pr, sha(1), "retry-old-ready")
    board.poll()

    row = board.state("42")
    assert (row["update_failures"], row["stuck"], row["stuck_reason"]) == (
        2,
        1,
        "update-failed-2",
    )


def test_a_retry_that_still_conflicts_stops_with_nothing_left_to_run(tmp_path: Path) -> None:
    board, pr, _approve = conflicted(tmp_path)
    board.fail("claude")
    board.poll()
    board.poll()
    assert board.jobs("update-from-main")[-1]["state"] == "queued"
    board.claim("claude")
    pr.push(sha(2), state="dirty")
    late = board.ready(pr, sha(2))
    board.poll()

    assert board.outcome(late) == "stuck-update-failed-2"
    assert board.queue.claim("claude") is None
