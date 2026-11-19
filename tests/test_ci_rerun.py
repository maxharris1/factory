from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.constants import CI_RERUN_SETTLE_SECONDS, STOPPED_POLL_SECONDS
from lib.dispatcher import CIResult, Dispatcher, FailedCheck
from lib.factory_md import HoldConfig
from lib.github_actions import GitHubAdapterError, GitHubReviewActions, GhResult, RerunOutcome
from lib.queue import SQLiteJobQueue
from tests.merge_queue_support import PR, Board, sha
from tests.test_dispatcher import TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions
from tests.test_hold_merge_queue import APPROVER

RUN = "777"
RUN_URL = f"https://github.com/owner/repo/actions/runs/{RUN}"


class RerunBoard(Board):
    def __init__(self, tmp_path: Path, **options):
        super().__init__(tmp_path, **options)
        self.results: dict[str, CIResult] = {}
        self.reruns: list[tuple[str, str]] = []
        self.refusal: BaseException | None = None
        self.run_status = "completed"
        self.dispatcher.ci_rerun = self.rerun

    def rerun(self, repo: str, run_ids) -> RerunOutcome:
        if self.run_status != "completed":
            return "running"
        for run_id in run_ids:
            self.reruns.append((repo, run_id))
            if self.refusal is not None:
                raise self.refusal
        return "requested"

    def snapshot(self, pr: PR, *, fresh: bool = False):
        ticket = super().snapshot(pr, fresh=fresh)
        ci = {
            head: self.results.get(head, result) if result.status == "failure" else result
            for head, result in ticket.ci.items()
        }
        return replace(ticket, ci=ci)

    def red(
        self, pr: PR, head: str, *job_ids: str, run_id: str | None = RUN, cause: str = "unknown"
    ) -> None:
        pr.ci[head] = "failure"
        self.results[head] = CIResult(
            "failure",
            "failed: " + ", ".join(f"pytest-{job}" for job in job_ids),
            failed=tuple(
                FailedCheck(f"pytest-{job}", run_id, job, RUN_URL if run_id else None, cause)
                for job in job_ids
            ),
        )

    def reruns_table(self) -> list[tuple[str, str, list[str], str | None]]:
        with self.queue.connect() as connection:
            return [
                (row["head"], row["outcome"], json.loads(row["job_ids"]), row["detail"])
                for row in connection.execute("SELECT * FROM ci_reruns ORDER BY requested_at")
            ]

    def ci_events(self) -> list[tuple[str, str, int]]:
        with self.queue.connect() as connection:
            return [
                (row["head"], row["status"], row["gate_count_after"])
                for row in connection.execute("SELECT * FROM ci_events ORDER BY rowid")
            ]

    def ci_wait(self) -> list[str]:
        with self.queue.connect() as connection:
            return [str(row["reason"]) for row in connection.execute("SELECT reason FROM ci_waits")]

    def attention(self, reason: str) -> list[str]:
        with self.queue.connect() as connection:
            return [
                row["detail"]
                for row in connection.execute(
                    "SELECT detail FROM needs_attention WHERE reason = ?", (reason,)
                )
            ]


def built(tmp_path: Path, **options) -> tuple[RerunBoard, PR]:
    board = RerunBoard(tmp_path, **options)
    pr = board.open("42", sha(1))
    board.go(pr, "go")
    board.poll()
    board.finish("claude")
    return board, pr


def red_ready(tmp_path: Path) -> tuple[RerunBoard, PR, str]:
    board, pr = built(tmp_path)
    board.red(pr, pr.head, "11", "12")
    ready = board.ready(pr, pr.head)
    board.poll()
    return board, pr, ready


def test_a_first_red_reruns_the_failed_jobs_and_charges_nothing(tmp_path: Path) -> None:
    board, pr, ready = red_ready(tmp_path)

    assert board.outcome(ready) is None
    assert board.reruns == [("owner/repo", RUN)]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []
    assert board.ci_events() == []
    assert board.reruns_table() == [(pr.head, "requested", ["11", "12"], None)]
    assert board.ci_wait() == ["ci-rerun"]
    assert any(f"at {pr.head[:12]}" in line and "rerun" in line for line in board.log)


def test_a_stale_read_of_the_attempt_before_the_rerun_still_waits(tmp_path: Path) -> None:
    board, pr, ready = red_ready(tmp_path)
    board.poll()
    board.poll()

    assert board.outcome(ready) is None
    assert board.reruns == [("owner/repo", RUN)]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []


def test_a_rerun_that_fails_again_charges_the_gate_and_queues_one_fix(tmp_path: Path) -> None:
    board, pr, ready = red_ready(tmp_path)
    board.red(pr, pr.head, "21", "22")
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert board.state("42")["gate_count"] == 1
    assert [job["head"] for job in board.jobs("fix")] == [pr.head]
    assert board.ci_events() == [(pr.head, "failure", 1)]
    assert board.reruns == [("owner/repo", RUN)]

    board.poll()
    assert len(board.jobs("fix")) == 1
    assert board.reruns == [("owner/repo", RUN)]


def test_the_ci_fix_carries_the_cause_its_fetched_logs_classify(tmp_path: Path) -> None:
    board, pr, ready = red_ready(tmp_path)
    reads: list[str] = []

    def logs(repo: str, run_id: str) -> dict[str, str]:
        reads.append(run_id)
        return {
            "pytest-21": "build\tSet up job\t2026-11-17T10:00:00.0000000Z The runner has received a shutdown signal.\n",
            "pytest-22": "test\tRun tests\t2026-11-17T10:00:00.0000000Z FAILED tests/test_x.py::test_y - assert 1\n",
        }

    board.dispatcher.ci_logs = logs
    board.red(pr, pr.head, "21", "22")
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    [fix] = board.jobs("fix")
    ci = json.loads(fix["detail"])["evidence"]["ci"]
    assert [(job["check"], job["cause"], job["signal"]) for job in ci["jobs"]] == [
        ("pytest-21", "infra", "runner lost"),
        ("pytest-22", "unknown", None),
    ]
    assert reads == [RUN]


def test_the_red_the_dispatcher_acts_on_is_classified_from_its_fetched_logs(
    tmp_path: Path,
) -> None:
    board, pr = built(tmp_path)
    reads: list[str] = []

    def logs(repo: str, run_id: str) -> dict[str, str]:
        reads.append(run_id)
        return {
            name: "build\tSet up job\t2026-11-17T10:00:00.0000000Z The runner has received a shutdown signal.\n"
            for name in ("pytest-11", "pytest-21")
        }

    charged: list[CIResult] = []
    queue_ci_fix = board.dispatcher._queue_ci_fix

    def capture(ticket, comment, head, result, *args, **kwargs):
        charged.append(result)
        return queue_ci_fix(ticket, comment, head, result, *args, **kwargs)

    board.dispatcher.ci_logs = logs
    board.dispatcher._queue_ci_fix = capture
    board.red(pr, pr.head, "11", cause="code")
    ready = board.ready(pr, pr.head)
    board.poll()
    board.poll()

    assert board.outcome(ready) is None
    [rerun_line] = [line for line in board.log if "rerunning the failed jobs" in line]
    assert "(cause infra)" in rerun_line
    assert reads == [RUN]

    board.red(pr, pr.head, "21", cause="code")
    board.poll()
    board.poll()
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert [result.cause for result in charged] == ["infra"]
    [fix] = board.jobs("fix")
    assert json.loads(fix["detail"])["evidence"]["ci"]["cause"] == "infra"
    assert reads == [RUN, RUN]


def test_the_same_failed_jobs_past_the_settle_window_charge(tmp_path: Path) -> None:
    board, pr, ready = red_ready(tmp_path)
    board.now[0] += CI_RERUN_SETTLE_SECONDS
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert board.state("42")["gate_count"] == 1
    assert len(board.jobs("fix")) == 1
    assert board.reruns == [("owner/repo", RUN)]


def test_a_green_rerun_reviews_and_never_charges(tmp_path: Path) -> None:
    board, pr, ready = red_ready(tmp_path)
    pr.ci[pr.head] = "pending"
    board.poll()
    assert board.outcome(ready) is None

    pr.ci[pr.head] = "success"
    board.poll()

    assert board.outcome(ready) == "queued-review"
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []
    assert board.ci_events() == [(pr.head, "success", 0)]
    assert board.reruns == [("owner/repo", RUN)]


def test_a_red_whose_run_is_still_in_progress_waits_without_a_rerun_or_a_charge(
    tmp_path: Path,
) -> None:
    board, pr = built(tmp_path)
    reads: list[str] = []
    board.dispatcher.ci_logs = lambda repo, run_id: reads.append(run_id) or {}
    board.run_status = "in_progress"
    board.red(pr, pr.head, "11", "12")
    ready = board.ready(pr, pr.head)
    board.poll()
    board.poll()

    assert board.outcome(ready) is None
    assert board.ci_wait() == ["ci-rerun"]
    assert board.reruns == []
    assert board.reruns_table() == []
    assert board.state("42")["gate_count"] == 0
    assert board.ci_events() == []
    assert board.jobs("fix") == []
    assert reads == []


def test_a_run_that_completes_after_a_wait_gets_one_rerun(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    board.run_status = "in_progress"
    board.red(pr, pr.head, "11", "12")
    ready = board.ready(pr, pr.head)
    board.poll()
    assert board.reruns == []

    board.run_status = "completed"
    board.poll()
    board.poll()

    assert board.outcome(ready) is None
    assert board.reruns == [("owner/repo", RUN)]
    assert board.reruns_table() == [(pr.head, "requested", ["11", "12"], None)]
    assert board.ci_wait() == ["ci-rerun"]
    assert board.state("42")["gate_count"] == 0
    assert board.ci_events() == []
    assert board.jobs("fix") == []


@pytest.mark.parametrize(
    ("refusal", "detail", "why"),
    [
        (
            GitHubAdapterError(
                "gh exited 1", http_status=403, api_message="Resource not accessible by integration"
            ),
            "GitHubAdapterError HTTP 403 Resource not accessible by integration",
            "refused (HTTP 403): Resource not accessible by integration",
        ),
        (RuntimeError("gh printed a token here"), "RuntimeError", "refused: RuntimeError"),
    ],
)
def test_a_refused_rerun_charges_in_the_same_poll_and_is_never_retried(
    tmp_path: Path, refusal: BaseException, detail: str, why: str
) -> None:
    board, pr = built(tmp_path)
    board.refusal = refusal
    board.red(pr, pr.head, "11")
    ready = board.ready(pr, pr.head)
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert board.state("42")["gate_count"] == 1
    assert len(board.jobs("fix")) == 1
    assert board.reruns_table() == [(pr.head, "refused", ["11"], detail)]
    assert any("refused" in line for line in board.log)
    assert not any("token" in line for line in board.log)
    [flagged] = board.attention("ci-rerun-refused")
    assert flagged == f"owner/repo#42 head {pr.head[:12]}: rerun-failed-jobs for run {RUN} {why}"
    assert "token" not in flagged
    [wake] = board.jobs("ci-rerun-refused")
    assert (wake["recipient"], wake["head"]) == ("grok", pr.head)

    board.poll()
    board.poll()
    assert board.reruns == [("owner/repo", RUN)]
    assert len(board.jobs("fix")) == 1
    assert len(board.attention("ci-rerun-refused")) == 1
    assert len(board.jobs("ci-rerun-refused")) == 1


def test_a_green_rerun_after_a_refusal_still_clears_the_recorded_failure(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    board.refusal = GitHubAdapterError("HTTP 403")
    board.red(pr, pr.head, "11")
    board.ready(pr, pr.head)
    board.poll()
    assert board.state("42")["gate_count"] == 1
    board.finish("claude")

    pr.ci[pr.head] = "success"
    board.poll()

    assert board.state("42")["gate_count"] == 0
    assert board.ci_events() == [(pr.head, "success", 0)]
    assert board.reruns == [("owner/repo", RUN)]


def test_a_red_with_no_actions_run_charges_at_once(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    board.red(pr, pr.head, "11", run_id=None)
    ready = board.ready(pr, pr.head)
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert board.state("42")["gate_count"] == 1
    assert board.reruns == []
    assert board.reruns_table() == [(pr.head, "unavailable", ["11"], None)]


def test_a_third_red_head_after_its_rerun_marks_the_ticket_stuck(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    for number in (1, 2, 3):
        head = sha(number)
        pr.push(head)
        board.red(pr, head, f"{number}1")
        ready = board.ready(pr, head)
        board.poll()
        assert board.outcome(ready) is None
        board.red(pr, head, f"{number}2")
        board.poll()
        if number < 3:
            assert board.outcome(ready) == "queued-ci-fix"
            board.finish("claude")

    assert board.outcome(ready) == "stuck-ci-red-3"
    assert board.state("42")["stuck"] == 1
    assert len(board.reruns) == 3


def handed_off(tmp_path: Path) -> tuple[RerunBoard, PR]:
    board = RerunBoard(tmp_path, finish_line=None)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    assert board.state("42")["workflow_state"] == "handed-off"
    return board, pr


def stopped_poll(board: Board) -> None:
    board.now[0] += STOPPED_POLL_SECONDS
    board.poll()


def test_a_red_push_after_handoff_with_no_ready_gets_the_rerun_then_the_fix(tmp_path: Path) -> None:
    board, pr = handed_off(tmp_path)
    pr.push(sha(2))
    board.red(pr, sha(2), "31")
    stopped_poll(board)

    assert board.reruns == [("owner/repo", RUN)]
    assert board.reruns_table() == [(sha(2), "requested", ["31"], None)]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []

    stopped_poll(board)
    assert board.reruns == [("owner/repo", RUN)]
    assert board.jobs("fix") == []

    board.red(pr, sha(2), "32")
    stopped_poll(board)

    row = board.state("42")
    assert (row["workflow_state"], row["gate_count"]) == ("handed-off", 1)
    assert [job["head"] for job in board.jobs("fix")] == [sha(2)]
    assert board.ci_events()[-1] == (sha(2), "failure", 1)
    assert any("unannounced red" in line for line in board.log)

    stopped_poll(board)
    assert len(board.jobs("fix")) == 1
    assert board.reruns == [("owner/repo", RUN)]


def test_a_green_rerun_after_an_unannounced_red_clears_it_once_handed_off(
    tmp_path: Path,
) -> None:
    board, pr = handed_off(tmp_path)
    pr.push(sha(2))
    board.red(pr, pr.head, "31")
    stopped_poll(board)
    board.red(pr, pr.head, "32")
    stopped_poll(board)
    assert board.state("42")["gate_count"] == 1
    board.finish("claude")

    pr.ci[pr.head] = "success"
    stopped_poll(board)

    assert board.state("42")["gate_count"] == 0
    assert board.ci_events()[-1] == (sha(2), "success", 0)


def test_a_red_push_after_handoff_is_charged_at_once_with_reruns_off(tmp_path: Path) -> None:
    board, pr = handed_off(tmp_path)
    board.dispatcher.ci_rerun = None
    pr.push(sha(2))
    board.red(pr, sha(2), "31")
    stopped_poll(board)

    assert board.state("42")["gate_count"] == 1
    assert [job["head"] for job in board.jobs("fix")] == [sha(2)]
    assert board.reruns_table() == []


def test_a_red_push_while_the_builder_works_is_left_to_its_ready(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    board.revise(pr, "1/3")
    board.poll()
    assert board.state("42")["workflow_state"] == "fixing"

    pr.push(sha(2))
    board.red(pr, sha(2), "41")
    board.poll()

    assert board.reruns == []
    assert board.ci_events() == [(sha(1), "success", 0)]
    assert len(board.jobs("fix")) == 1


def test_a_green_rerun_after_an_unannounced_red_clears_it_while_fixing(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")
    assert board.state("42")["workflow_state"] == "fixing"

    pr.push(sha(2))
    board.red(pr, sha(2), "41")
    board.poll()
    board.red(pr, sha(2), "42")
    board.poll()
    assert board.state("42")["gate_count"] == 1
    assert board.ci_events()[-1] == (sha(2), "failure", 1)
    assert [job["head"] for job in board.jobs("fix")][-1] == sha(2)
    board.finish("claude")

    pr.ci[sha(2)] = "success"
    board.poll()

    row = board.state("42")
    assert (row["workflow_state"], row["gate_count"]) == ("fixing", 0)
    assert board.ci_events()[-1] == (sha(2), "success", 0)


def before_ci(tmp_path: Path) -> tuple[RerunBoard, PR]:
    board = RerunBoard(tmp_path)
    board.factory = replace(board.factory, start_before_ci=True)
    pr = board.open("42", sha(1))
    pr.ci[pr.head] = "pending"
    board.go(pr, "go")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    return board, pr


def test_an_approve_on_red_ci_waits_on_the_rerun_before_its_fix(tmp_path: Path) -> None:
    board, pr = before_ci(tmp_path)
    board.red(pr, pr.head, "51")
    approve = board.approve(pr)
    board.poll()

    assert board.outcome(approve) is None
    assert board.reruns == [("owner/repo", RUN)]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []

    board.red(pr, pr.head, "52")
    board.poll()
    assert board.outcome(approve) == "approve-ci-red-fix"
    assert board.state("42")["gate_count"] == 1
    assert len(board.jobs("fix")) == 1


def test_a_held_approve_on_red_ci_waits_on_the_rerun_before_its_fix(tmp_path: Path) -> None:
    board = RerunBoard(
        tmp_path, ship="merge", hold=HoldConfig(approver=APPROVER, paths=("src/**/*.tsx",))
    )
    board.factory = replace(board.factory, start_before_ci=True)
    pr = board.open("42", sha(1))
    pr.changed_files = ("src/view/Button.tsx",)
    pr.ci[sha(1)] = "pending"
    board.go(pr, "go")
    board.poll()
    board.finish("claude")
    board.ready(pr, sha(1))
    board.poll()
    board.finish("codex")
    board.red(pr, sha(1), "81")
    approve = board.approve(pr)
    for _ in range(3):
        board.poll()

    assert board.reruns == [("owner/repo", RUN)]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []

    board.red(pr, sha(1), "82")
    board.poll()
    assert board.outcome(approve) is None
    assert board.state("42")["workflow_state"] == "fixing"
    assert board.state("42")["gate_count"] == 1
    assert [job["head"] for job in board.jobs("fix")] == [sha(1)]


def test_a_revise_on_red_ci_waits_on_the_rerun_before_charging(tmp_path: Path) -> None:
    board, pr = before_ci(tmp_path)
    board.red(pr, pr.head, "61")
    revise = board.revise(pr, "1/3")
    board.poll()

    assert board.outcome(revise) is None
    assert board.reruns == [("owner/repo", RUN)]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []

    board.red(pr, pr.head, "62")
    board.poll()
    assert board.outcome(revise) == "queued-review-fix"
    assert board.state("42")["gate_count"] == 1
    assert len(board.jobs("fix")) == 1


def test_a_dead_updates_red_head_waits_on_the_rerun_in_the_merge_queue(tmp_path: Path) -> None:
    board = RerunBoard(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    job = board.claim("claude")
    pr.push(sha(2))
    board.red(pr, sha(2), "71")
    board.queue.finish(job.id, owner="claude", handoff_comment_id=None)
    for _ in range(3):
        board.poll()

    assert board.reruns == [("owner/repo", RUN)]
    assert board.state("42")["workflow_state"] == "merge-queue"
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []

    board.red(pr, sha(2), "72")
    board.poll()

    row = board.state("42")
    assert (row["workflow_state"], row["approved_head"], row["gate_count"]) == ("fixing", None, 1)
    assert [job["head"] for job in board.jobs("fix")] == [sha(2)]
    assert board.reruns == [("owner/repo", RUN)]


def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}


def dispatcher_at(path: Path) -> Dispatcher:
    return Dispatcher(
        path,
        SQLiteJobQueue(path, clock=lambda: 100.0),
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
    )


def test_the_schema_adds_the_reruns_table_to_new_and_existing_databases(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.db"
    dispatcher_at(fresh)
    assert "ci_reruns" in tables(fresh)

    existing = tmp_path / "existing.db"
    dispatcher_at(existing)
    with sqlite3.connect(existing) as connection:
        connection.execute("DROP TABLE ci_reruns")
    assert "ci_reruns" not in tables(existing)
    dispatcher_at(existing)
    assert "ci_reruns" in tables(existing)


RUN_PATH = f"repos/owner/repo/actions/runs/{RUN}"
ALREADY_RUNNING = GhResult(
    1,
    json.dumps({"message": "This workflow is already running", "status": "403"}),
    "gh: This workflow is already running (HTTP 403)",
)
FORBIDDEN = GhResult(
    1,
    json.dumps({"message": "Resource not accessible by integration", "status": "403"}),
    "gh: Resource not accessible by integration (HTTP 403)",
)


def rerun_adapter(
    *statuses: str, post: GhResult = GhResult(0, "", "")
) -> tuple[GitHubReviewActions, list[list[str]]]:
    calls: list[list[str]] = []
    reads = iter(statuses)

    def gh(argv, timeout: float) -> GhResult:
        api = list(argv)[list(argv).index("api") :]
        calls.append(api)
        if "POST" in api:
            return post
        return GhResult(0, json.dumps({"id": int(RUN), "status": next(reads)}), "")

    return GitHubReviewActions(gh, cache_ttl="20s"), calls


def posts(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if "POST" in call]


def test_the_adapter_reruns_only_the_failed_jobs_of_a_completed_run() -> None:
    adapter, calls = rerun_adapter("completed")

    assert adapter.rerun_failed_jobs("owner/repo", [RUN]) == "requested"
    assert calls == [["api", RUN_PATH], ["api", "-X", "POST", f"{RUN_PATH}/rerun-failed-jobs"]]
    with pytest.raises(GitHubAdapterError):
        adapter.rerun_failed_jobs("owner/repo", ["../x"])
    assert len(calls) == 2


def test_the_adapter_posts_nothing_while_a_run_is_in_progress() -> None:
    adapter, calls = rerun_adapter("in_progress")

    assert adapter.rerun_failed_jobs("owner/repo", [RUN]) == "running"
    assert calls == [["api", RUN_PATH]]


@pytest.mark.parametrize(
    ("post", "statuses"),
    [(ALREADY_RUNNING, ("completed",)), (FORBIDDEN, ("completed", "in_progress"))],
)
def test_the_adapter_reads_a_refusal_of_a_running_workflow_as_running(
    post: GhResult, statuses: tuple[str, ...]
) -> None:
    adapter, calls = rerun_adapter(*statuses, post=post)

    assert adapter.rerun_failed_jobs("owner/repo", [RUN]) == "running"
    assert len(posts(calls)) == 1
    assert not any("--cache" in call for call in calls)


def test_the_adapter_raises_a_refusal_of_a_completed_run() -> None:
    adapter, calls = rerun_adapter("completed", "completed", post=FORBIDDEN)

    with pytest.raises(GitHubAdapterError) as raised:
        adapter.rerun_failed_jobs("owner/repo", [RUN])

    assert raised.value.http_status == 403
    assert raised.value.api_message == "Resource not accessible by integration"
    assert "not accessible" not in raised.value.safe_detail
    assert calls[-1] == ["api", RUN_PATH]
