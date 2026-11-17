"""#207: a force-pushed ticket branch gets a range-diff review, never an old approval,
and CI is read on the new head."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.factory_md import parse_factory_context
from lib.queue import Job, _job as queue_job
from lib.update_check import RebaseScope, UpdateClass, rebase_scope
from tests.merge_queue_support import Board, sha
from tests.regenerate_brief_snapshots import BOX_FACTORY_ROOT, FACTORY_MD
from tests.test_hold_merge_queue import held_board
from tests.test_rebase_scope import Repo

OLD_BASE, NEW_BASE = sha(90), sha(91)
REWRITE = UpdateClass(False, "not a merge of the approved head and base")
CONTEXT = parse_factory_context(FACTORY_MD)[0]


def brief(job: Job) -> str:
    return render_brief(job, bot="codex-bot", context=CONTEXT, factory_root=BOX_FACTORY_ROOT)


def board_with_rebases(tmp_path: Path, *, start_before_ci: bool = False) -> tuple[Board, list]:
    board = Board(tmp_path)
    board.verdict = REWRITE
    board.factory = replace(board.factory, start_before_ci=start_before_ci)
    calls: list[tuple[str, str, str, str]] = []

    def rebased(repo: str, base: str, last: str, head: str) -> RebaseScope:
        calls.append((repo, base, last, head))
        return RebaseScope(last, OLD_BASE, NEW_BASE)

    board.dispatcher.rebase_classifier = rebased
    return board, calls


def review_details(board: Board) -> list[dict]:
    return [json.loads(row["detail"]) for row in board.jobs("review")]


def test_a_force_push_after_approve_is_not_covered_and_round_2_gets_a_range_diff(
    tmp_path: Path,
) -> None:
    board, calls = board_with_rebases(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)

    pr.push(sha(2))
    board.poll()
    assert board.actions.merges == []
    assert any(
        f"approval at {sha(1)[:12]} does not cover {sha(2)[:12]}" in line for line in board.log
    )

    ready = board.ready(pr, sha(2))
    board.poll()

    assert board.outcome(ready) == "queued-review"
    assert board.state("42")["approved_head"] is None
    assert calls == [("owner/repo", "main", sha(1), sha(2))]
    round_2 = review_details(board)[-1]
    assert (round_2["head"], round_2["round"]) == (sha(2), "2/3")
    assert round_2["rebase"] == {
        "last_reviewed_head": sha(1), "reviewed_base": OLD_BASE, "base": NEW_BASE,
    }
    assert board.actions.merges == []

    job = board.claim("codex")
    text = brief(job)
    assert (
        f"Review the delta with `git range-diff {OLD_BASE}..{sha(1)} {NEW_BASE}..{sha(2)}`"
        in text
    )
    evidence = json.loads(job.request.detail)["evidence"]["findings"]
    assert (evidence["reviewed_head"], evidence["reviewed_base"]) == (sha(1), OLD_BASE)

    board.queue.finish(job.id, owner="codex", handoff_comment_id=f"handoff-{job.id}")
    board.approve(pr, "2/3")
    board.poll()
    board.poll()
    assert board.actions.merges == [(True, sha(2))]


def test_a_force_push_after_revise_gets_a_range_diff_review(tmp_path: Path) -> None:
    board, _calls = board_with_rebases(tmp_path)
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, sha(1))
    board.poll()
    board.finish("codex")
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")

    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()

    round_2 = review_details(board)[-1]
    assert (round_2["head"], round_2["round"]) == (sha(2), "2/3")
    assert round_2["rebase"]["last_reviewed_head"] == sha(1)


@pytest.mark.job_bound
@pytest.mark.parametrize("third", ["rewrite", "follow-up"])
def test_a_queued_unreviewed_head_is_never_the_range_diff_baseline(
    tmp_path: Path, third: str
) -> None:
    git = Repo(tmp_path / "git")
    reviewed = git.pr_commit("old = 1\n")
    unreviewed = git.pr_commit("old = 2\n")
    if third == "rewrite":
        latest = git.pr_commit("old = 3\n")
    else:
        (git.root / "more.py").write_text("more = 1\n")
        latest = git.commit("follow-up")
    board, _calls = board_with_rebases(tmp_path)
    board.dispatcher.rebase_classifier = lambda _repo, _base, last, head: rebase_scope(
        git.gitdir, last, head, "refs/heads/main"
    )
    snapshot = board.snapshot
    board.snapshot = lambda pr, fresh=False: replace(
        snapshot(pr, fresh=fresh), ready_starts_review=True
    )
    pr = board.open("42", reviewed)
    pr.mergeable = None
    pr.mergeable_state = None
    first = board.ready(pr, reviewed)
    board.poll()
    assert board.outcome(first) == "queued-review"
    review = board.claim("codex")
    board.queue.finish(review.id, owner="codex", handoff_comment_id=board.approve(pr))
    board.poll()
    assert board.state("42")["approved_head"] == reviewed

    for head in (unreviewed, latest):
        pr.push(head)
        ready = board.ready(pr, head)
        board.poll()
        assert board.outcome(ready) == "queued-review"

    rows = board.jobs("review")
    assert [(row["head"], row["state"]) for row in rows] == [
        (reviewed, "done"), (unreviewed, "queued"), (latest, "queued"),
    ]
    detail = json.loads(rows[-1]["detail"])
    assert detail["evidence"]["findings"]["reviewed_head"] == reviewed
    assert detail["rebase"]["last_reviewed_head"] == reviewed
    assert (
        f"git range-diff {git.base}..{reviewed} {git.base}..{latest}"
        in brief(queue_job(rows[-1], board.queue.connect()))
    )


@pytest.mark.job_bound
def test_a_same_round_approval_is_the_range_diff_baseline(tmp_path: Path) -> None:
    git = Repo(tmp_path / "git")
    revised = git.pr_commit("value = 1\n")
    approved = git.pr_commit("value = 2\n")
    git.git("checkout", "-q", "-B", "pr", git.base)
    (git.root / "feature.py").write_text("value = 1\n")
    reverted = git.commit("pr reworded")
    board = held_board(tmp_path)
    board.dispatcher.rebase_classifier = lambda _repo, _base, last, head: rebase_scope(
        git.gitdir, last, head, "refs/heads/main"
    )
    snapshot = board.snapshot
    board.snapshot = lambda pr, fresh=False: replace(
        snapshot(pr, fresh=fresh), ready_starts_review=True
    )

    def build(head: str) -> str:
        job = board.claim("claude")
        ready = board.ready(pr, head)
        board.queue.finish(job.id, owner="claude", handoff_comment_id=ready)
        board.poll()
        return ready

    def review(verdict) -> None:
        job = board.claim("codex")
        board.queue.finish(job.id, owner="codex", handoff_comment_id=verdict())
        board.poll()

    pr = board.open("42", revised)
    pr.changed_files = ("src/view/Button.tsx",)
    pr.mergeable = None
    pr.mergeable_state = None
    board.go(pr, "go-42")
    board.poll()
    assert board.outcome(build(revised)) == "queued-review"
    review(lambda: board.revise(pr, "1/3"))

    pr.push(approved)
    assert board.outcome(build(approved)) == "queued-review"
    review(lambda: board.approve(pr, "2/3"))
    assert board.state("42")["workflow_state"] == "awaiting-ok"

    pr.push(reverted)
    board.poll()
    row = board.jobs("review")[-1]
    detail = json.loads(row["detail"])
    assert (row["head"], row["state"], detail["round"]) == (reverted, "queued", "2/3")
    assert detail["rebase"]["last_reviewed_head"] == approved
    evidence = detail["evidence"]["findings"]
    assert (evidence["reviewed_head"], evidence["reviewed_base"]) == (approved, git.base)
    assert (
        f"git range-diff {git.base}..{approved} {git.base}..{reverted}"
        in brief(queue_job(row, board.queue.connect()))
    )


def test_a_plain_push_and_a_failed_check_keep_the_generic_delta_line(tmp_path: Path) -> None:
    for name, classifier in (
        ("descendant", lambda *_args: None),
        ("broken", lambda *_args: (_ for _ in ()).throw(OSError("no clone"))),
        ("absent", None),
    ):
        board = Board(tmp_path / name)
        board.verdict = REWRITE
        board.dispatcher.rebase_classifier = classifier
        pr = board.open("42", sha(1))
        board.build_and_approve(pr, mergeable=None)
        pr.push(sha(2))
        board.ready(pr, sha(2))
        board.poll()

        round_2 = review_details(board)[-1]
        assert (round_2["head"], round_2["round"]) == (sha(2), "2/3"), name
        assert "rebase" not in round_2, name
        text = brief(board.claim("codex"))
        assert "(`git range-diff` when the branch was rebased)" in text, name
        with board.queue.connect() as connection:
            errors = [row[0] for row in connection.execute("SELECT error FROM poll_errors")]
        assert ("rebase-check:OSError" in errors) == (name == "broken"), name


def test_ci_is_read_on_the_force_pushed_head_not_the_approved_one(tmp_path: Path) -> None:
    board, _calls = board_with_rebases(tmp_path, start_before_ci=True)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    assert pr.ci[sha(1)] == "success"

    pr.push(sha(2), ci="failure")
    red = board.ready(pr, sha(2))
    board.poll()
    assert board.outcome(red) == "queued-ci-fix"
    assert [row["head"] for row in board.jobs("review")] == [sha(1)]
    assert [row["head"] for row in board.jobs("fix")] == [sha(2)]
    with board.queue.connect() as connection:
        ci_heads = {
            row["head"]: row["status"]
            for row in connection.execute("SELECT head, status FROM ci_events")
        }
    assert ci_heads[sha(2)] == "failure"
    board.finish("claude")

    pr.push(sha(3), ci="pending")
    board.ready(pr, sha(3))
    board.poll()
    round_2 = review_details(board)[-1]
    assert (round_2["head"], round_2["round"]) == (sha(3), "2/3")
    board.finish("codex")
    approve = board.approve(pr, "2/3")
    board.poll()
    assert board.outcome(approve) is None
    assert board.actions.merges == []
    assert board.state("42")["approved_head"] is None

    pr.ci[sha(3)] = "success"
    board.poll()
    board.poll()
    assert board.actions.merges == [(True, sha(3))]
