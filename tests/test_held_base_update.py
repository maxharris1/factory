from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lib.dispatch.types import Comment
from lib.github_actions import _created_at
from lib.queue import JobRequest
from lib.state_backup import backup, restore
from tests.merge_queue_support import APPROVE, Board, sha
from tests.test_dispatcher import READY
from tests.test_hold_merge_queue import APPROVER, OK, held_board


def _held(board: Board):
    pr = board.open("42", sha(1))
    pr.changed_files = ("src/view/Button.tsx",)
    approve = board.build_and_approve(pr)
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    return pr, approve


def _merge_only_jobs(board: Board) -> list:
    return [
        row
        for row in board.jobs("fix", "42")
        if json.loads(row["detail"]).get("kind") == "merge-only"
    ]


def _hold_head(board: Board) -> str:
    with board.queue.connect() as connection:
        return connection.execute(
            "SELECT head FROM ship_holds WHERE repo = 'owner/repo' AND ticket_id = '42'"
        ).fetchone()["head"]


@pytest.mark.parametrize("mergeable_state", ["dirty", "behind"])
def test_a_held_pr_that_goes_dirty_or_behind_queues_one_merge_only_job(
    tmp_path: Path, mergeable_state: str
) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    pr.mergeable = mergeable_state != "dirty"
    pr.mergeable_state = mergeable_state

    for _ in range(4):
        board.poll()

    [job] = _merge_only_jobs(board)
    assert job["recipient"] == "claude"
    assert job["head"] == sha(1)
    assert job["source_comment_id"] == approve
    assert job["dedupe_key"] == f"base-update:owner/repo\x1f42:{sha(1)}"
    detail = json.loads(job["detail"])
    assert detail["round"] == "1/3"
    assert detail["instruction"] == (
        f"The approved PR is {mergeable_state} against main while it waits for an "
        f"ok: merge main in."
    )
    assert board.outcome(approve) == "held-base-update"
    assert board.state("42")["workflow_state"] == "fixing"
    assert board.wakes("stale") == []
    assert len(board.wakes("awaiting-ok")) == 1
    assert board.actions.merges == []


def test_a_clean_held_pr_stays_held_with_no_base_update(tmp_path: Path) -> None:
    board = held_board(tmp_path)
    _pr, approve = _held(board)

    for _ in range(4):
        board.poll()

    assert _merge_only_jobs(board) == []
    assert board.outcome(approve) is None
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert len(board.wakes("awaiting-ok")) == 1


def test_the_update_is_re_reviewed_and_held_again_for_an_ok_at_the_new_head(
    tmp_path: Path,
) -> None:
    board = held_board(tmp_path)
    pr, _approve = _held(board)
    cap_extra = board.state("42")["cap_extra"]
    pr.conflict()
    board.poll()

    board.finish("claude")
    board.post(pr, "ok-old", OK.format(head=sha(1)), APPROVER)
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()

    [_first, review] = board.jobs("review", "42")
    assert review["head"] == sha(2)
    assert json.loads(review["detail"])["round"] == "1/3"
    assert board.state("42")["workflow_state"] == "review"
    assert board.state("42")["review_round"] == 0
    assert board.state("42")["cap_extra"] == cap_extra

    board.finish("codex")
    board.approve(pr)
    board.poll()
    board.poll()

    assert board.actions.merges == []
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert _hold_head(board) == sha(2)
    assert [wake["head"] for wake in board.wakes("awaiting-ok")] == [sha(1), sha(2)]
    assert board.wakes("stale") == []

    board.post(pr, "ok-new", OK.format(head=sha(2)), APPROVER)
    board.poll()

    assert board.actions.merges == [(True, sha(2))]
    assert board.state("42")["workflow_state"] == "merged"


def test_a_lost_handled_row_replays_without_a_second_job_or_a_stale_wake(
    tmp_path: Path,
) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    pr.conflict()
    board.poll()
    with board.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = ?", (approve,))

    board.poll()

    assert board.outcome(approve) == "held-base-update"
    assert len(_merge_only_jobs(board)) == 1
    assert board.state("42")["workflow_state"] == "fixing"
    assert board.wakes("stale") == []


def test_a_merge_only_job_already_out_at_the_head_is_not_queued_again(
    tmp_path: Path,
) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    board.queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="fix",
            recipient="claude",
            source_comment_id="restart",
            timeout_seconds=60,
            head=sha(1),
            detail=json.dumps({"kind": "merge-only"}),
        ),
        dedupe_key="earlier",
    )
    pr.conflict()

    board.poll()

    [job] = _merge_only_jobs(board)
    assert job["dedupe_key"] == "earlier"
    assert board.outcome(approve) == "held-base-update"
    assert board.state("42")["workflow_state"] == "fixing"


@pytest.mark.parametrize("ci", ["pending", "failure", None])
@pytest.mark.parametrize("mergeable_state", ["dirty", "behind"])
def test_a_held_pr_queues_its_base_update_whatever_the_old_head_ci_says(
    tmp_path: Path, mergeable_state: str, ci: str | None
) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    pr.mergeable = mergeable_state != "dirty"
    pr.mergeable_state = mergeable_state
    pr.ci = {} if ci is None else {sha(1): ci}

    for _ in range(4):
        board.poll()

    [job] = _merge_only_jobs(board)
    assert job["source_comment_id"] == approve
    assert board.outcome(approve) == "held-base-update"
    assert board.state("42")["workflow_state"] == "fixing"
    assert board.wakes("stale") == []
    assert board.actions.merges == []


def _crash_before_handled(board: Board, approve: str):
    mark = board.dispatcher._mark_handled

    def crash(ticket, comment_id, outcome):
        if comment_id == approve:
            raise SystemExit("crash before the handled row")
        return mark(ticket, comment_id, outcome)

    board.dispatcher._mark_handled = crash
    with pytest.raises(SystemExit):
        board.poll()
    board.dispatcher._mark_handled = mark


@pytest.mark.parametrize("reused", [False, True])
def test_a_crash_after_the_transition_replays_the_approve_as_held_not_stale(
    tmp_path: Path, reused: bool
) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    if reused:
        board.queue.enqueue(
            JobRequest(
                repo="owner/repo",
                ticket_id="42",
                action="fix",
                recipient="claude",
                source_comment_id="restart",
                timeout_seconds=60,
                head=sha(1),
                detail=json.dumps({"kind": "merge-only"}),
            ),
            dedupe_key="earlier",
        )
    pr.conflict()
    _crash_before_handled(board, approve)

    board.poll()

    assert board.outcome(approve) == "held-base-update"
    assert len(_merge_only_jobs(board)) == 1
    assert board.state("42")["workflow_state"] == "fixing"
    assert board.wakes("stale") == []


def test_a_failed_fixing_label_is_written_again_on_the_next_poll(tmp_path: Path) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    pr.conflict()
    board.actions.state_failures.add(("owner/repo", "fixing"))

    board.poll()

    assert board.state("42")["workflow_state"] == "fixing"
    assert board.outcome(approve) is None
    board.actions.state_failures.clear()

    board.poll()

    assert board.outcome(approve) == "held-base-update"
    assert board.actions.states[-1] == "fixing"
    assert len(_merge_only_jobs(board)) == 1
    assert board.wakes("stale") == []


@pytest.mark.parametrize("mergeable_state", ["dirty", "behind"])
def test_a_held_pr_whose_head_moved_without_a_ready_updates_the_new_head(
    tmp_path: Path, mergeable_state: str
) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    cap_extra = board.state("42")["cap_extra"]
    pr.push(sha(2), state=mergeable_state)
    del pr.ci[sha(2)]

    for _ in range(4):
        board.poll()

    [job] = _merge_only_jobs(board)
    assert job["head"] == sha(2)
    assert job["source_comment_id"] == approve
    assert job["dedupe_key"] == f"base-update:owner/repo\x1f42:{sha(2)}"
    assert json.loads(job["detail"])["round"] == "1/3"
    assert board.outcome(approve) == "held-base-update"
    assert board.state("42")["workflow_state"] == "fixing"
    assert board.state("42")["review_round"] == 0
    assert board.state("42")["cap_extra"] == cap_extra
    assert board.wakes("stale") == []
    assert board.actions.merges == []

    board.finish("claude")
    board.post(pr, "ok-old", OK.format(head=sha(1)), APPROVER)
    pr.push(sha(3))
    board.ready(pr, sha(3))
    board.poll()
    board.finish("codex")
    board.approve(pr)
    board.poll()
    board.poll()

    assert board.actions.merges == []
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert _hold_head(board) == sha(3)

    board.post(pr, "ok-new", OK.format(head=sha(3)), APPROVER)
    board.poll()

    assert board.actions.merges == [(True, sha(3))]


def test_a_reposted_approve_after_the_held_update_is_a_duplicate_not_stale(
    tmp_path: Path,
) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    duplicate = board.approve(pr, comment_id="approve-reposted")
    pr.conflict()

    board.poll()

    assert board.outcome(approve) == "held-base-update"
    assert board.outcome(duplicate) == "duplicate-verdict"
    assert board.wakes("stale") == []
    assert len(_merge_only_jobs(board)) == 1


def test_a_ready_accepted_before_the_update_was_needed_does_not_cancel_it(
    tmp_path: Path,
) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    ready = board.ready(pr, sha(1), comment_id="ready-repost")
    board.poll()
    board.poll()
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert board.outcome(ready) is not None

    pr.push(sha(2), state="dirty")
    del pr.ci[sha(2)]
    for _ in range(4):
        board.poll()

    assert board.outcome(approve) == "held-base-update"
    assert [(job["head"], job["state"]) for job in _merge_only_jobs(board)] == [
        (sha(2), "queued")
    ]
    assert board.state("42")["workflow_state"] == "fixing"


def test_a_ready_accepted_in_the_updates_own_second_does_not_cancel_it(
    tmp_path: Path,
) -> None:
    board = held_board(tmp_path)
    pr, approve = _held(board)
    board.now[0] += 10
    second = int(board.now[0])
    pr.comments.append(
        Comment(
            id="ready-repost",
            author="claude-login",
            body=READY.format(head=sha(1)),
            created_at=float(second),
        )
    )
    board.now[0] = second + 0.1
    board.dispatcher.poll_once(board)
    board.now[0] = second + 0.2
    board.dispatcher.poll_once(board)
    assert board.outcome("ready-repost") == "queued-review"
    assert board.state("42")["workflow_state"] == "awaiting-ok"

    pr.push(sha(2), state="dirty")
    del pr.ci[sha(2)]
    for offset in (0.3, 0.4, 1.4, 2.4):
        board.now[0] = second + offset
        board.dispatcher.poll_once(board)

    assert board.outcome(approve) == "held-base-update"
    assert [(job["head"], job["state"]) for job in _merge_only_jobs(board)] == [
        (sha(2), "queued")
    ]


def _red_fix(tmp_path: Path, fraction: float = 0.0):
    board = Board(tmp_path / "state", finish_line=None)
    board.factory = replace(board.factory, start_before_ci=True)
    pr = board.open("42", sha(1))
    board.go(pr, "go")
    board.poll()
    board.finish("claude")
    board.ready(pr, sha(1))
    board.poll()
    board.finish("codex")
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-A", APPROVE.format(round="1/3", head=sha(1)), "codex-login")
    board.now[0] += fraction
    board.poll()
    assert board.outcome("approve-A") == "approve-ci-red-fix"
    [fix] = board.jobs("fix")
    assert fix["head"] == sha(2)
    return board, pr, fix


def test_a_ready_at_the_approved_head_posted_after_a_restored_fix_still_settles_it(
    tmp_path: Path,
) -> None:
    board, pr, _ = _red_fix(tmp_path)
    saved = tmp_path / "backup.db"
    backup(board.queue.path, saved)
    board.finish("claude")
    pr.push(sha(1))
    board.ready(pr, sha(1), "ready-rollback")
    shutil.rmtree(board.queue.path.parent)
    restore(saved, board.queue.path)
    board.poll()
    board.poll()

    assert board.outcome("ready-rollback") == "queued-review"
    assert [job["state"] for job in board.jobs("fix")] == ["cancelled"]


def test_a_ready_github_dates_to_the_fixs_own_second_still_settles_it(
    tmp_path: Path,
) -> None:
    board, pr, fix = _red_fix(tmp_path, fraction=0.25)
    saved = tmp_path / "backup.db"
    backup(board.queue.path, saved)
    board.finish("claude")
    pr.push(sha(1))
    board.now[0] += 0.5
    reported = _created_at(
        datetime.fromtimestamp(board.now[0], UTC).isoformat(timespec="seconds")
    )
    pr.comments.append(
        Comment(
            id="ready-rollback",
            author="claude-login",
            body=READY.format(head=sha(1)),
            created_at=reported,
        )
    )
    assert reported < fix["created_at"] < board.now[0]
    shutil.rmtree(board.queue.path.parent)
    restore(saved, board.queue.path)
    board.poll()
    board.poll()

    assert board.outcome("ready-rollback") == "queued-review"
    assert [job["state"] for job in board.jobs("fix")] == ["cancelled"]


def test_a_ready_the_cached_read_missed_settles_the_failed_fix_without_a_retry(
    tmp_path: Path,
) -> None:
    board, pr, _ = _red_fix(tmp_path)
    fix = board.claim("claude")
    read = board.tracked_tickets

    def snapshot_then_worker_finishes(refs):
        cached = read(refs)
        pr.push(sha(1))
        board.ready(pr, sha(1), "ready-rollback")
        board.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
        board.now[0] += 1
        return cached

    board.tracked_tickets = snapshot_then_worker_finishes
    board.poll()
    board.tracked_tickets = read
    board.poll()
    board.poll()

    assert board.outcome("ready-rollback") == "queued-review"
    assert [(job["attempt"], job["state"]) for job in board.jobs("fix")] == [(1, "done")]
    assert board.queue.claim("claude") is None


def test_a_failed_fix_whose_ready_github_dates_to_its_own_second_is_done(
    tmp_path: Path,
) -> None:
    board, pr, fix = _red_fix(tmp_path, fraction=0.25)
    board.claim("claude")
    pr.push(sha(1))
    board.now[0] += 0.5
    pr.comments.append(
        Comment(
            id="ready-rollback",
            author="claude-login",
            body=READY.format(head=sha(1)),
            created_at=float(int(board.now[0])),
        )
    )
    assert int(board.now[0]) < fix["created_at"]
    board.queue.finish(fix["id"], owner="claude", handoff_comment_id=None)
    board.poll()
    board.poll()

    assert board.outcome("ready-rollback") == "queued-review"
    assert [(job["attempt"], job["state"]) for job in board.jobs("fix")] == [(1, "done")]
