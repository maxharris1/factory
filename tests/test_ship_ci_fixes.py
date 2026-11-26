"""Issue #83 (ship merge on a draft PR, retry) and #84 (CI rerun at one head).

These run the real GitHub adapter against the RecordedGh seam (a fake gh),
so the argv is the one production sends. Nothing reaches GitHub.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult
from lib.github_actions import GhResult, GitHubAdapterError, HeadMismatchError
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import (
    READY,
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    seed_builder_job,
    setup,
    ticket,
)
from tests.test_github_actions import (
    HEAD,
    MARK_READY,
    MERGE,
    PULL,
    STATUS,
    CHECKS,
    WORKFLOW_RUNS,
    adapter,
    draft_pull,
    pr,
    pr_workflow_runs,
    served,
)
from tests.test_ship_redrive import (
    approve_snapshot,
    github_setup,
    grok_wakes,
    intent,
)


pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

NOT_MERGEABLE_405 = GhResult(
    1, '{"message": "Pull Request is not mergeable"}', "gh: Pull Request is not mergeable (HTTP 405)\n"
)
HEAD_MODIFIED_409 = GhResult(
    1, '{"message": "Head branch was modified"}', "gh: Head branch was modified (HTTP 409)\n"
)


def attention(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT ticket_id, reason FROM needs_attention ORDER BY ticket_id, reason"
            )
        ]


def workflow(queue: SQLiteJobQueue, ticket_id: str = "42") -> str | None:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT workflow_state FROM ticket_state WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
    return None if row is None else row[0]


# --- #83: ship merge marks a draft PR ready first ----------------------------


def test_merge_marks_a_draft_pr_ready_before_merging() -> None:
    actions, gh = adapter()
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    gh.route(MERGE, served("merge-squash-200.json"))

    actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)

    assert gh.keys() == [PULL, MARK_READY, MERGE]


def test_merge_of_a_pr_that_is_not_a_draft_sends_no_mutation() -> None:
    actions, gh = adapter()
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, served("merge-squash-200.json"))

    actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)

    assert gh.keys() == [PULL, MERGE]


def test_a_405_merge_failure_is_retryable_and_a_409_is_not() -> None:
    actions, gh = adapter()
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, NOT_MERGEABLE_405)
    with pytest.raises(GitHubAdapterError) as raised:
        actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)
    assert raised.value.retryable is True

    actions, gh = adapter()
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, HEAD_MODIFIED_409)
    with pytest.raises(HeadMismatchError) as mismatch:
        actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)
    assert mismatch.value.retryable is False


def test_a_mark_ready_failure_before_merge_is_retryable_and_never_merges() -> None:
    actions, gh = adapter()
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, GhResult(1, "", "gh: Server Error (HTTP 502)\n"))
    with pytest.raises(GitHubAdapterError) as raised:
        actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)
    assert raised.value.retryable is True
    assert MERGE not in gh.keys()


def merge_setup(tmp_path: Path):
    dispatcher, queue, gh = github_setup(tmp_path)
    return dispatcher, queue, gh, approve_snapshot(ship="merge")


def test_a_retryable_merge_failure_is_retried_and_merges(tmp_path: Path) -> None:
    dispatcher, queue, gh, snapshot = merge_setup(tmp_path)
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    gh.route(MERGE, NOT_MERGEABLE_405, served("merge-squash-200.json"))

    dispatcher.process(snapshot)

    row = intent(queue)
    assert (row["status"], row["redrive"]) == ("failed", "eligible")
    assert attention(queue) == []  # a retry is pending: not flagged yet

    dispatcher.process(snapshot)

    row = intent(queue)
    assert (row["status"], row["error"], row["redrive"]) == ("completed", None, "spent")
    assert gh.count(MERGE) == 2
    assert workflow(queue) == "merged"
    assert [action for action, _ in grok_wakes(queue)] == ["ship-failed", "done"]
    assert attention(queue) == []

    dispatcher.process(snapshot)
    assert gh.count(MERGE) == 2  # never merged twice


def test_a_merge_that_fails_again_on_retry_is_flagged_needs_attention(tmp_path: Path) -> None:
    dispatcher, queue, gh, snapshot = merge_setup(tmp_path)
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, NOT_MERGEABLE_405)

    for _ in range(4):
        dispatcher.process(snapshot)

    assert gh.count(MERGE) == 2  # the first try and one retry
    row = intent(queue)
    assert (row["status"], row["redrive"]) == ("failed", "spent")
    assert attention(queue) == [("42", "ship-failed")]


def test_a_merge_that_fails_with_a_non_retryable_error_is_flagged_at_once(tmp_path: Path) -> None:
    dispatcher, queue, gh, snapshot = merge_setup(tmp_path)
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, HEAD_MODIFIED_409)

    dispatcher.process(snapshot)
    dispatcher.process(snapshot)

    assert gh.count(MERGE) == 1
    row = intent(queue)
    assert row["status"] == "failed" and row["redrive"] is None
    assert attention(queue) == [("42", "ship-failed")]


def test_a_merge_retry_finds_the_pr_already_merged_and_completes(tmp_path: Path) -> None:
    dispatcher, queue, gh, snapshot = merge_setup(tmp_path)
    merged = served("pull-42-open.json").stdout.replace('"merged": false', '"merged": true')
    gh.route(PULL, served("pull-42-open.json"), served("pull-42-open.json"),
             GhResult(0, merged, ""))
    gh.route(MERGE, GhResult(1, "", "gh: Server Error (HTTP 502)\n"))

    dispatcher.process(snapshot)
    dispatcher.process(snapshot)

    assert gh.count(MERGE) == 1
    assert intent(queue)["status"] == "completed"
    assert workflow(queue) == "merged"


# --- #87 review r2 F2.5: the merge preflight read is classified like the merge


SERVER_ERROR_502 = GhResult(1, "", "gh: Server Error (HTTP 502)\n")
TRANSIENT_READS = [
    pytest.param(SERVER_ERROR_502, id="502"),
    pytest.param(subprocess.TimeoutExpired("gh", 60), id="timeout"),
]


@pytest.mark.parametrize("failure", TRANSIENT_READS)
def test_a_transient_is_merged_read_failure_is_retryable(failure) -> None:
    actions, gh = adapter()
    gh.route(PULL, failure)
    with pytest.raises(GitHubAdapterError) as raised:
        actions.is_merged(pr("merge"), HEAD)
    assert raised.value.retryable is True


def test_a_refused_is_merged_read_is_not_retryable() -> None:
    actions, gh = adapter()
    gh.route(PULL, GhResult(1, "", "gh: Not Found (HTTP 404)\n"))
    with pytest.raises(GitHubAdapterError) as raised:
        actions.is_merged(pr("merge"), HEAD)
    assert raised.value.retryable is False


@pytest.mark.parametrize("failure", TRANSIENT_READS)
def test_a_transient_merge_preflight_failure_is_retried_and_merges(
    tmp_path: Path, failure
) -> None:
    dispatcher, queue, gh, snapshot = merge_setup(tmp_path)
    gh.route(PULL, failure, served("pull-42-open.json"))
    gh.route(MERGE, served("merge-squash-200.json"))

    for _ in range(3):
        dispatcher.process(snapshot)

    row = intent(queue)
    assert (row["status"], row["redrive"]) == ("completed", "spent")
    assert gh.count(MERGE) == 1
    assert workflow(queue) == "merged"
    assert attention(queue) == []


# --- #84: the latest attempt of each check -----------------------------------


def check_runs(*runs: tuple[int, str, str, str], workflow_run: int | None = 11) -> GhResult:
    """Attempts of checks in one workflow run (or, with None, no identity)."""
    if workflow_run is not None:
        return workflow_runs(*(run + (workflow_run,) for run in runs))
    body = {
        "total_count": len(runs),
        "check_runs": [
            {
                "id": run_id,
                "head_sha": HEAD,
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "started_at": started,
                "completed_at": started,
                "app": {"slug": "github-actions", "id": 15368},
            }
            for run_id, name, conclusion, started in runs
        ],
    }
    return GhResult(0, json.dumps(body), "")


def test_a_passing_rerun_supersedes_the_failed_attempt_of_the_same_check() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, check_runs(
        (1, "test", "failure", "2026-11-12T19:00:00Z"),
        (2, "build", "success", "2026-11-12T19:00:00Z"),
        (3, "test", "success", "2026-11-12T19:30:00Z"),
    ))
    gh.route(STATUS, served("status-empty.json"))
    assert actions.ci_status("owner/repo", HEAD).status == "success"


def test_a_failing_rerun_supersedes_an_earlier_pass() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, check_runs(
        (3, "test", "failure", "2026-11-12T19:30:00Z"),
        (1, "test", "success", "2026-11-12T19:00:00Z"),
    ))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(WORKFLOW_RUNS, pr_workflow_runs(11))
    result = actions.ci_status("owner/repo", HEAD)
    assert result.status == "failure" and "test" in (result.log or "")


def test_distinct_checks_with_one_failure_still_fail() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, check_runs(
        (1, "lint", "failure", "2026-11-12T19:00:00Z"),
        (2, "test", "success", "2026-11-12T19:30:00Z"),
    ))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(WORKFLOW_RUNS, pr_workflow_runs(11))
    assert actions.ci_status("owner/repo", HEAD).status == "failure"


SHA = "c" * 40


def ready(comment_id: str, created_at: float) -> object:
    return comment(comment_id, READY.format(head=SHA), author="claude-login", created_at=created_at)


def test_a_green_rerun_at_the_same_head_clears_the_recorded_failure(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    first = ready("ready-1", 1)
    dispatcher.process(ticket(first, head=SHA, ci={SHA: CIResult("failure", "port in use")}))
    fix = queue.claim("claude")
    assert fix is not None and fix.request.action == "fix"
    queue.finish(fix.id, owner="claude", handoff_comment_id="ready-2")

    # The CI fix job reran the workflow at the same head and it went green.
    second = ready("ready-2", 2)
    dispatcher.process(ticket(first, second, head=SHA, ci={SHA: CIResult("success")}))

    reviews = [job for job in queue.outstanding() if job.request.action == "review"]
    assert len(reviews) == 1 and reviews[0].request.head == SHA
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT status FROM ci_events WHERE head = ?", (SHA,)
        ).fetchone()[0] == "success"
        assert connection.execute(
            "SELECT gate_count FROM ticket_state WHERE ticket_id = '42'"
        ).fetchone()[0] == 0
    assert attention(queue) == []


def test_when_dedupe_drops_a_job_the_ticket_is_flagged_needs_attention(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    first = ready("ready-1", 1)
    dispatcher.process(ticket(first, head=SHA, ci={SHA: CIResult("failure", "boom")}))
    fix = queue.claim("claude")
    assert fix is not None
    queue.finish(fix.id, owner="claude", handoff_comment_id="ready-2")
    jobs_before = len(queue.outstanding())

    # Still red at the same head: the t2 key for this head is spent.
    second = ready("ready-2", 2)
    dispatcher.process(ticket(first, second, head=SHA, ci={SHA: CIResult("failure", "boom")}))

    assert len(queue.outstanding()) == jobs_before  # nothing new was queued ...
    assert attention(queue) == [("42", "dedupe-dropped")]  # ... and it is not silent


def jobs_by_action(queue: SQLiteJobQueue) -> list[tuple[str, str | None]]:
    with queue.connect() as connection:
        return [
            (str(row[0]), row[1])
            for row in connection.execute("SELECT action, head FROM jobs ORDER BY rowid")
        ]


def dropped_ready_at_red_head(tmp_path: Path) -> tuple[object, SQLiteJobQueue, list[object]]:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    first = ready("ready-1", 1)
    dispatcher.process(ticket(first, head=SHA, ci={SHA: CIResult("failure", "runner refused")}))
    fix = queue.claim("claude")
    assert fix is not None and fix.request.action == "fix"
    queue.finish(fix.id, owner="claude", handoff_comment_id="ready-2")
    second = ready("ready-2", 2)
    dispatcher.process(
        ticket(first, second, head=SHA, ci={SHA: CIResult("failure", "runner refused")})
    )
    assert attention(queue) == [("42", "dedupe-dropped")]
    return dispatcher, queue, [first, second]


def test_issue_452_a_green_rerun_after_a_dropped_ready_queues_one_review(tmp_path: Path) -> None:
    dispatcher, queue, readies = dropped_ready_at_red_head(tmp_path)
    before = jobs_by_action(queue)

    dispatcher.process(ticket(*readies, head=SHA, ci={SHA: CIResult("success")}))

    assert jobs_by_action(queue) == [*before, ("review", SHA)]
    review = next(job for job in queue.outstanding() if job.request.action == "review")
    assert review.request.source_comment_id == "ready-2"
    assert workflow(queue) == "review"
    assert attention(queue) == []

    for _ in range(2):
        dispatcher.process(ticket(*readies, head=SHA, ci={SHA: CIResult("success")}))
    assert jobs_by_action(queue) == [*before, ("review", SHA)]


def test_issue_452_red_ci_at_the_head_queues_no_review_and_no_extra_fix(tmp_path: Path) -> None:
    dispatcher, queue, readies = dropped_ready_at_red_head(tmp_path)
    before = jobs_by_action(queue)

    for _ in range(2):
        dispatcher.process(
            ticket(*readies, head=SHA, ci={SHA: CIResult("failure", "runner refused")})
        )
        dispatcher.process(ticket(*readies, head=SHA, ci={SHA: CIResult("pending")}))

    assert jobs_by_action(queue) == before
    assert [action for action, _head in before].count("fix") == 1


def test_issue_452_no_review_while_the_ci_fix_is_still_open(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    first = ready("ready-1", 1)
    dispatcher.process(ticket(first, head=SHA, ci={SHA: CIResult("failure", "runner refused")}))
    before = jobs_by_action(queue)

    dispatcher.process(ticket(first, head=SHA, ci={SHA: CIResult("success")}))

    assert jobs_by_action(queue) == before


def test_issue_452_an_edited_handled_ready_cannot_queue_a_review_of_its_new_head(
    tmp_path: Path,
) -> None:
    dispatcher, queue, (first, second) = dropped_ready_at_red_head(tmp_path)
    before = jobs_by_action(queue)
    new_head = "d" * 40
    edited = replace(second, body=READY.format(head=new_head), edited_at=3)

    dispatcher.process(ticket(first, edited, head=new_head, ci={new_head: CIResult("success")}))

    assert jobs_by_action(queue) == before


def test_issue_452_a_headless_revise_beside_a_green_ready_is_stale(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    first = ready("ready-1", 1)
    dispatcher.process(ticket(first, head=SHA, ci={SHA: CIResult("success")}))
    review = queue.claim("codex")
    assert review is not None
    queue.finish(review.id, owner="codex", handoff_comment_id="verdict-1")
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="fix",
            recipient="claude",
            source_comment_id="ci-fix",
            timeout_seconds=5400,
            head=SHA,
        ),
        dedupe_key="second-builder",
    )
    fix = queue.claim("claude")
    assert fix is not None
    queue.finish(fix.id, owner="claude", handoff_comment_id="ready-2")
    second = ready("ready-2", 2)
    verdict = comment(
        "verdict-1",
        "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round=1 verdict=REVISE -->\nNeeds work.\n",
        author="codex-login",
        created_at=3,
    )
    snapshot = ticket(second, verdict, head=SHA, ci={SHA: CIResult("success")})

    dispatcher.process(snapshot)

    assert dispatcher._handled_outcome(snapshot, verdict.id) == "stale-revise"
    assert dispatcher._handled_outcome(snapshot, second.id) == "queued-review"


def test_needs_attention_shows_in_orch_status_and_the_dashboard(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from lib.status import SQLiteStatusReader, build_status, render

    dispatcher, queue = setup(tmp_path)
    dispatcher.flag_attention("owner/repo", "42", "ship-failed", "merge failed twice")
    db = tmp_path / "d.db"
    reader = SQLiteStatusReader(db)
    text = render(build_status(reader, reader, clock=lambda: 200.0,
                               feeder=reader.feeder(), attention=reader.attention()))
    assert "Needs attention (1)" in text
    assert "owner/repo#42 ship-failed" in text
    snapshot = collect_snapshot(db, tmp_path / "none.log", now=200.0)
    kinds = [(item["kind"], item["ticket_id"]) for item in snapshot["attention"]["items"]]
    assert ("needs-attention", "42") in kinds


# --- F1.1 (#87 review): a rerun is the same check in the same workflow run ----


def workflow_runs(*runs: tuple[int, str, str, str, int]) -> GhResult:
    """Check runs as GitHub Actions reports them: each job's check suite and
    its details_url, which names the workflow run (stable across reruns)."""
    body = {
        "total_count": len(runs),
        "check_runs": [
            {
                "id": run_id,
                "head_sha": HEAD,
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "started_at": started,
                "completed_at": started,
                "app": {"slug": "github-actions", "id": 15368},
                "check_suite": {"id": 900 + workflow_run},
                "details_url": (
                    f"https://github.com/owner/repo/actions/runs/{workflow_run}/job/{run_id}"
                ),
            }
            for run_id, name, conclusion, started, workflow_run in runs
        ],
    }
    return GhResult(0, json.dumps(body), "")


def test_two_workflows_with_one_job_name_keep_the_failure() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, workflow_runs(
        (1, "test", "failure", "2026-11-12T19:00:00Z", 11),  # workflow A
        (2, "test", "success", "2026-11-12T19:30:00Z", 22),  # workflow B, later
    ))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(WORKFLOW_RUNS, pr_workflow_runs(11, 22))
    result = actions.ci_status("owner/repo", HEAD)
    assert result.status == "failure" and "test" in (result.log or "")


def test_a_rerun_in_the_same_workflow_run_still_supersedes_the_failure() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, workflow_runs(
        (1, "test", "failure", "2026-11-12T19:00:00Z", 11),
        (2, "lint", "success", "2026-11-12T19:00:00Z", 11),
        (3, "test", "success", "2026-11-12T19:30:00Z", 11),  # attempt 2 of run 11
    ))
    gh.route(STATUS, served("status-empty.json"))
    assert actions.ci_status("owner/repo", HEAD).status == "success"


def test_runs_with_no_workflow_identity_never_supersede_each_other() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, check_runs(
        (1, "test", "failure", "2026-11-12T19:00:00Z"),
        (3, "test", "success", "2026-11-12T19:30:00Z"),
        workflow_run=None,
    ))
    gh.route(STATUS, served("status-empty.json"))
    assert actions.ci_status("owner/repo", HEAD).status == "failure"
