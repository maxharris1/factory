from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot
from lib.dispatch.constants import (
    CI_RERUN_SETTLE_SECONDS,
    CI_STALE_REASON,
    CI_STALE_SECONDS,
    STOPPED_POLL_SECONDS,
)
from lib.dispatcher import CIResult, Dispatcher, FailedCheck
from lib.dispatch.types import BaseCheck
from lib.factory_md import HoldConfig
from lib.github_actions import GitHubAdapterError, GitHubReviewActions, GhResult
from lib.queue import SQLiteJobQueue
from lib.update_check import RebaseScope
from tests.merge_queue_support import PR, Board, sha
from tests.test_dispatcher import (
    READY,
    RETRY,
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
)
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
        self.base: dict[str, BaseCheck] = {}
        self.base_reads: list[tuple[str, str]] = []

    def base_red(self, *names: str) -> None:
        self.base = {name: BaseCheck(name, "failure", "900", f"9{name}") for name in names}
        self.dispatcher.ci_base_checks = self.base_checks

    def base_checks(self, repo: str, branch: str) -> dict[str, BaseCheck]:
        self.base_reads.append((repo, branch))
        return self.base

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
        return replace(ticket, ci=ci, pr_base="main")

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
    assert (row["workflow_state"], row["gate_count"]) == ("review", 0)
    assert board.ci_events()[-1] == (sha(2), "success", 0)
    assert [job["head"] for job in board.jobs("review")] == [sha(1), sha(2)]


def fixing(tmp_path: Path) -> tuple[RerunBoard, PR, str]:
    board, pr = built(tmp_path)
    ready = board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")
    assert board.state("42")["workflow_state"] == "fixing"
    return board, pr, ready


def test_an_unannounced_green_head_while_fixing_queues_one_review(tmp_path: Path) -> None:
    board, pr, ready = fixing(tmp_path)
    board.dispatcher.rebase_classifier = lambda _repo, _base, last, _head: RebaseScope(
        last, sha(90), sha(91)
    )
    pr.push(sha(2))
    board.poll()

    reviews = board.jobs("review")
    detail = json.loads(reviews[-1]["detail"])
    assert len(reviews) == 2
    assert (reviews[-1]["head"], reviews[-1]["source_comment_id"]) == (sha(2), ready)
    assert sha(2) in reviews[-1]["dedupe_key"]
    assert (detail["round"], detail["source"]) == ("2/3", "unannounced-green-head")
    assert detail["rebase"] == {
        "last_reviewed_head": sha(1),
        "reviewed_base": sha(90),
        "base": sha(91),
    }
    assert board.state("42")["workflow_state"] == "review"
    assert any("unannounced green" in line and sha(2)[:12] in line for line in board.log)

    board.poll()
    assert [job["state"] for job in board.jobs("review")] == ["done", "queued"]

    board.finish("codex")
    board.poll()
    assert len(board.jobs("review")) == 2


def test_an_open_fix_job_at_the_head_holds_the_unannounced_review(tmp_path: Path) -> None:
    board, pr, _ready = fixing(tmp_path)
    pr.push(sha(2))
    board.red(pr, sha(2), "41")
    board.poll()
    board.red(pr, sha(2), "42")
    board.poll()
    fix = board.jobs("fix")[-1]
    assert (fix["head"], fix["state"]) == (sha(2), "queued")

    pr.ci[sha(2)] = "success"
    board.poll()
    assert [job["head"] for job in board.jobs("review")] == [sha(1)]

    board.finish("claude")
    board.poll()
    assert [job["head"] for job in board.jobs("review")] == [sha(1), sha(2)]


@pytest.mark.parametrize("hold", ["stuck = 1", "workflow_state = 'waiting-max'"])
def test_a_stuck_or_waiting_max_ticket_gets_no_unannounced_review(
    tmp_path: Path, hold: str
) -> None:
    board, pr, _ready = fixing(tmp_path)
    with board.queue.connect() as connection:
        connection.execute(f"UPDATE ticket_state SET {hold} WHERE ticket_id = '42'")
    pr.push(sha(2))
    stopped_poll(board)

    assert [job["head"] for job in board.jobs("review")] == [sha(1)]


def test_an_unannounced_green_head_past_the_cap_flags_and_wakes_once(tmp_path: Path) -> None:
    board, pr, _ready = fixing(tmp_path)
    board.factory = replace(board.factory, rounds_high=1)
    pr.push(sha(2))
    board.poll()
    board.poll()

    assert [job["head"] for job in board.jobs("review")] == [sha(1)]
    with board.queue.connect() as connection:
        rows = connection.execute(
            "SELECT detail FROM needs_attention WHERE reason = 'unannounced-head-after-cap'"
        ).fetchall()
    assert len(rows) == 1
    assert sha(2)[:12] in rows[0]["detail"]
    assert len(board.wakes("capped")) == 1


def test_an_unannounced_green_head_after_the_build_queues_the_first_review(
    tmp_path: Path,
) -> None:
    board, pr = built(tmp_path)
    board.poll()
    assert board.jobs("review") == []

    pr.push(sha(2))
    board.poll()

    reviews = board.jobs("review")
    assert [job["head"] for job in reviews] == [sha(2)]
    assert json.loads(reviews[0]["detail"])["round"] == "1/3"
    assert board.state("42")["workflow_state"] == "review"


def test_a_failed_state_write_after_the_unannounced_review_is_retried(tmp_path: Path) -> None:
    board, pr, _ready = fixing(tmp_path)
    pr.push(sha(2))
    set_state = board.actions.set_state
    failed: list[str] = []

    def flaky(ticket, state: str) -> None:
        if state == "review" and not failed:
            failed.append(state)
            raise RuntimeError("transient tracker failure")
        set_state(ticket, state)

    board.actions.set_state = flaky
    board.poll()
    assert failed == ["review"]
    board.poll()
    assert board.state("42")["workflow_state"] == "review"

    board.finish("codex")
    verdict = board.revise(pr, "2/3")
    board.poll()
    assert board.outcome(verdict) == "queued-review-fix"
    assert [job["head"] for job in board.jobs("review")] == [sha(1), sha(2)]


def test_an_untrusted_ready_naming_the_head_does_not_hold_its_review(tmp_path: Path) -> None:
    board, pr, _ready = fixing(tmp_path)
    pr.push(sha(2))
    board.post(pr, "outsider-ready", READY.format(head=pr.head), "outsider-login")
    with board.queue.connect() as connection:
        connection.execute("UPDATE ticket_state SET stuck = 1 WHERE ticket_id = '42'")
    stopped_poll(board)
    assert [job["head"] for job in board.jobs("review")] == [sha(1)]

    board.post(pr, "retry", RETRY.format(reason="Runner restored."), "grok-login")
    stopped_poll(board)
    assert board.state("42")["stuck"] == 0
    board.finish("claude")
    board.poll()

    assert [job["head"] for job in board.jobs("review")] == [sha(1), sha(2)]


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


def base_red_ready(tmp_path: Path) -> tuple[RerunBoard, PR, str]:
    board, pr = built(tmp_path)
    board.base_red("pytest-11")
    board.red(pr, pr.head, "11", cause="code")
    ready = board.ready(pr, pr.head)
    board.poll()
    return board, pr, ready


def test_a_red_the_base_shares_holds_with_no_charge_rerun_or_fix(tmp_path: Path) -> None:
    board, pr, ready = base_red_ready(tmp_path)
    board.now[0] += CI_RERUN_SETTLE_SECONDS + 1
    board.poll()

    assert board.outcome(ready) is None
    assert board.ci_wait() == ["ci-base-red"]
    assert board.reruns == []
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []
    assert board.ci_events() == []
    assert board.base_reads == [("owner/repo", "main"), ("owner/repo", "main")]


def test_a_green_base_reruns_the_failed_jobs_then_charges_a_red_that_remains(
    tmp_path: Path,
) -> None:
    board, pr, ready = base_red_ready(tmp_path)
    board.base = {"pytest-11": BaseCheck("pytest-11", "success", "902", "903")}
    board.poll()

    assert board.reruns == [("owner/repo", RUN)]
    assert board.ci_wait() == ["ci-rerun"]
    assert board.state("42")["gate_count"] == 0

    board.red(pr, pr.head, "21", cause="code")
    board.base = {"pytest-21": BaseCheck("pytest-21", "success", "904", "905")}
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert board.state("42")["gate_count"] == 1
    assert [job["head"] for job in board.jobs("fix")] == [pr.head]


def test_a_rerun_that_meets_a_red_base_again_holds_without_charging(tmp_path: Path) -> None:
    board, pr, ready = base_red_ready(tmp_path)
    board.base = {}
    board.poll()
    assert board.reruns == [("owner/repo", RUN)]

    board.base_red("pytest-21")
    board.red(pr, pr.head, "21", cause="code")
    board.poll()

    assert board.outcome(ready) is None
    assert board.ci_wait() == ["ci-base-red"]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []


def test_a_red_on_a_check_the_base_passes_is_charged_as_before(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    board.base_red("lint")
    board.red(pr, pr.head, "11", cause="code")
    ready = board.ready(pr, pr.head)
    board.poll()
    assert board.ci_wait() == ["ci-rerun"]
    board.red(pr, pr.head, "12", cause="code")
    board.poll()

    assert board.outcome(ready) == "queued-ci-fix"
    assert board.state("42")["gate_count"] == 1


def test_an_unannounced_red_the_base_shares_is_held(tmp_path: Path) -> None:
    board, pr = handed_off(tmp_path)
    board.base_red("pytest-31")
    pr.push(sha(2))
    board.red(pr, sha(2), "31")
    stopped_poll(board)
    board.now[0] += CI_RERUN_SETTLE_SECONDS
    stopped_poll(board)

    assert board.reruns == []
    assert board.reruns_table() == [(sha(2), "ci-base-red", ["31"], "main")]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []
    assert board.ci_wait() == ["ci-base-red"]


def test_a_third_red_with_a_green_base_still_sticks(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    board.dispatcher.ci_base_checks = board.base_checks
    for number in (1, 2, 3):
        head = sha(number)
        pr.push(head)
        board.base = {f"pytest-{number}2": BaseCheck(f"pytest-{number}2", "success", "9", "9")}
        board.red(pr, head, f"{number}1")
        ready = board.ready(pr, head)
        board.poll()
        board.red(pr, head, f"{number}2")
        board.poll()
        if number < 3:
            board.finish("claude")

    assert board.outcome(ready) == "stuck-ci-red-3"
    assert board.state("42")["stuck"] == 1


def test_a_base_red_hold_is_cleared_when_the_ticket_ends(tmp_path: Path) -> None:
    board, pr = handed_off(tmp_path)
    board.dispatcher.ci_base_checks = board.base_checks
    pr.push(sha(2))
    board.red(pr, sha(2), "31")
    stopped_poll(board)
    board.base_red("pytest-41")
    pr.push(sha(3))
    board.red(pr, sha(3), "41")
    stopped_poll(board)
    assert [row[:2] for row in board.reruns_table()] == [
        (sha(2), "requested"),
        (sha(3), "ci-base-red"),
    ]

    pr.closed = True
    stopped_poll(board)

    assert board.state("42")["workflow_state"] == "abandoned"
    assert [row[:2] for row in board.reruns_table()] == [(sha(2), "requested")]
    assert board.ci_wait() == []


def stale_rows(board: RerunBoard) -> list[tuple[str, str]]:
    with board.queue.connect() as connection:
        return [
            (row["reason"], row["detail"])
            for row in connection.execute(
                "SELECT * FROM needs_attention WHERE reason = ?", (CI_STALE_REASON,)
            )
        ]


def test_a_stuck_red_ticket_raises_one_stale_driver_item(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    for number in (1, 2, 3):
        head = sha(number)
        pr.push(head)
        board.red(pr, head, f"{number}1")
        board.ready(pr, head)
        board.poll()
        board.red(pr, head, f"{number}2")
        board.poll()
        if number < 3:
            board.finish("claude")
    assert board.state("42")["stuck_reason"] == "ci-red-3"
    assert stale_rows(board) == []
    before = len(board.jobs())

    board.now[0] += CI_STALE_SECONDS + 1
    board.poll()
    board.poll()

    assert [job["action"] for job in board.jobs()[before:]] == [CI_STALE_REASON]
    [(reason, detail)] = stale_rows(board)
    assert detail.startswith(f"CI ci-red-3 on {sha(3)[:12]}")


def test_a_base_red_hold_goes_stale_and_resets_when_the_head_moves(tmp_path: Path) -> None:
    board, pr, _ready = base_red_ready(tmp_path)
    board.now[0] += CI_STALE_SECONDS - 100
    board.poll()
    assert stale_rows(board) == []

    board.now[0] += 200
    board.poll()
    board.poll()
    assert [detail[:15] for _reason, detail in stale_rows(board)] == ["CI ci-base-red "]
    assert len(board.jobs(CI_STALE_REASON)) == 1

    pr.push(sha(2))
    board.red(pr, sha(2), "11", cause="code")
    board.ready(pr, sha(2))
    board.poll()
    assert stale_rows(board) == []
    assert len(board.jobs(CI_STALE_REASON)) == 1


def test_an_approve_on_a_red_the_base_shares_holds_before_its_fix(tmp_path: Path) -> None:
    board, pr = before_ci(tmp_path)
    board.base_red("pytest-51")
    board.red(pr, pr.head, "51")
    approve = board.approve(pr)
    board.poll()
    board.now[0] += CI_RERUN_SETTLE_SECONDS + 1
    board.poll()

    assert board.outcome(approve) is None
    assert board.reruns == []
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []
    assert board.ci_wait() == ["ci-base-red"]
    snapshot = collect_snapshot(tmp_path / "d.db", tmp_path / "no-log", now=board.now[0])
    assert [ticket["ci_wait"]["reason"] for ticket in snapshot["tickets"]] == ["ci-base-red"]

    board.base = {}
    board.poll()
    assert board.reruns == [("owner/repo", RUN)]
    assert board.ci_wait() == []
    assert board.state("42")["gate_count"] == 0


def test_a_revise_on_a_red_the_base_shares_holds_without_charging(tmp_path: Path) -> None:
    board, pr = before_ci(tmp_path)
    board.base_red("pytest-61")
    board.red(pr, pr.head, "61")
    revise = board.revise(pr, "1/3")
    board.poll()
    board.now[0] += CI_RERUN_SETTLE_SECONDS + 1
    board.poll()

    assert board.outcome(revise) is None
    assert board.reruns == []
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []
    assert board.ci_wait() == ["ci-base-red"]

    board.base = {}
    board.poll()
    assert board.reruns == [("owner/repo", RUN)]
    assert board.ci_wait() == []


def test_a_dead_updates_red_head_the_base_shares_holds_in_the_merge_queue(tmp_path: Path) -> None:
    board = RerunBoard(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    job = board.claim("claude")
    pr.push(sha(2))
    board.base_red("pytest-71")
    board.red(pr, sha(2), "71")
    board.queue.finish(job.id, owner="claude", handoff_comment_id=None)
    for _ in range(3):
        board.poll()

    assert board.reruns == []
    assert board.state("42")["workflow_state"] == "merge-queue"
    assert board.state("42")["gate_count"] == 0
    assert board.ci_wait() == ["ci-base-red"]

    board.base = {}
    board.poll()
    assert board.reruns == [("owner/repo", RUN)]
    assert board.ci_wait() == []


def pytest_failures(*names: str) -> str:
    return "\n".join(f"FAILED tests/test_widget.py::{name} - AssertionError" for name in names)


def test_a_pr_failure_past_the_tenth_base_failure_is_still_held(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    shared = [f"test_common_{n}" for n in range(10)]
    board.base = {"pytest-11": BaseCheck("pytest-11", "failure", "900", "9001")}
    board.dispatcher.ci_base_checks = board.base_checks
    board.dispatcher.ci_logs = lambda repo, run: {
        "pytest-11": pytest_failures(*shared, "test_eleventh")
        if run == "900"
        else pytest_failures("test_eleventh")
    }
    board.red(pr, pr.head, "11", cause="code")
    board.ready(pr, pr.head)
    board.poll()
    board.now[0] += CI_RERUN_SETTLE_SECONDS
    board.poll()

    assert board.ci_wait() == ["ci-base-red"]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []
    assert board.reruns == []


def test_an_eleventh_pr_failure_the_base_lacks_is_not_held(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    shared = [f"test_common_{n}" for n in range(10)]
    board.base = {"pytest-11": BaseCheck("pytest-11", "failure", "900", "9001")}
    board.dispatcher.ci_base_checks = board.base_checks
    board.dispatcher.ci_logs = lambda repo, run: {
        "pytest-11": pytest_failures(*shared)
        if run == "900"
        else pytest_failures(*shared, "test_new_regression")
    }
    board.red(pr, pr.head, "11", cause="code")
    board.ready(pr, pr.head)
    board.poll()

    assert board.ci_wait() == ["ci-rerun"]
    assert board.reruns == [("owner/repo", RUN)]


def test_a_base_failure_on_the_second_page_of_jobs_holds_the_red(tmp_path: Path) -> None:
    def gh(argv, timeout: float) -> GhResult:
        path = next(arg for arg in argv if arg.startswith("repos/"))
        if "/actions/runs?" in path:
            return GhResult(0, json.dumps({"workflow_runs": [{"id": 900, "workflow_id": 1}]}), "")
        assert "/actions/runs/900/jobs?" in path
        jobs = [{"id": 1000 + n, "name": f"other-{n}", "conclusion": "success"} for n in range(100)]
        jobs.append({"id": 1100, "name": "pytest-11", "conclusion": "failure"})
        page = 2 if "page=2" in path else 1
        listed = jobs[100 * (page - 1) : 100 * page]
        return GhResult(0, json.dumps({"total_count": 101, "jobs": listed}), "")

    board, pr = built(tmp_path)
    board.dispatcher.ci_base_checks = GitHubReviewActions(gh).base_checks
    board.red(pr, pr.head, "11", cause="code")
    board.ready(pr, pr.head)
    board.poll()
    board.results[pr.head] = CIResult(
        "failure", failed=(FailedCheck("pytest-11", RUN, "21", cause="code"),)
    )
    board.poll()

    assert board.ci_wait() == ["ci-base-red"]
    assert board.state("42")["gate_count"] == 0
    assert board.jobs("fix") == []
