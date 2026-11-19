from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.constants import CI_RERUN_SETTLE_SECONDS, STOPPED_POLL_SECONDS
from lib.dispatcher import CIResult, Dispatcher, FailedCheck
from lib.dispatch.types import BaseCheck
from lib.factory_md import HoldConfig
from lib.github_actions import GitHubAdapterError, GitHubReviewActions, GhResult
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
        self.dispatcher.ci_rerun = self.rerun

    def rerun(self, repo: str, run_id: str) -> None:
        self.reruns.append((repo, run_id))
        if self.refusal is not None:
            raise self.refusal

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


@pytest.mark.parametrize(
    ("refusal", "detail"),
    [
        (GitHubAdapterError("HTTP 403"), "GitHubAdapterError HTTP 403"),
        (RuntimeError("gh printed a token here"), "RuntimeError"),
    ],
)
def test_a_refused_rerun_charges_in_the_same_poll_and_is_never_retried(
    tmp_path: Path, refusal: BaseException, detail: str
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

    board.poll()
    board.poll()
    assert board.reruns == [("owner/repo", RUN)]
    assert len(board.jobs("fix")) == 1


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


def test_the_adapter_reruns_only_the_failed_jobs_of_a_run() -> None:
    calls: list[list[str]] = []

    def gh(argv, timeout: float) -> GhResult:
        calls.append(list(argv))
        return GhResult(0, "", "")

    GitHubReviewActions(gh).rerun_failed_jobs("owner/repo", RUN)

    assert calls[0][calls[0].index("api") :] == [
        "api",
        "-X",
        "POST",
        f"repos/owner/repo/actions/runs/{RUN}/rerun-failed-jobs",
    ]
    with pytest.raises(GitHubAdapterError):
        GitHubReviewActions(gh).rerun_failed_jobs("owner/repo", "../x")
    assert len(calls) == 1


def test_the_base_branch_is_read_once_per_poll_for_every_ticket_on_it(tmp_path: Path) -> None:
    board = RerunBoard(tmp_path)
    red = {"test (3/3)": BaseCheck("test (3/3)", "failure", "900", "9001")}
    reads: list[tuple[str, str]] = []

    def base_checks(repo: str, base: str) -> dict[str, BaseCheck]:
        reads.append((repo, base))
        if base == "broken":
            raise GitHubAdapterError("HTTP 502")
        return red

    board.dispatcher.ci_base_checks = base_checks
    board.poll()
    read = board.dispatcher._base_checks_for

    assert [read("owner/repo", "main"), read("Owner/Repo", "main")] == [red, red]
    assert [read("owner/repo", "broken"), read("owner/repo", "broken")] == [None, None]
    assert reads == [("owner/repo", "main"), ("owner/repo", "broken")]
    assert any("ci base: owner/repo broken unavailable" in line for line in board.log)

    board.poll()
    read("owner/repo", "main")

    assert reads[2:] == [("owner/repo", "main")]
