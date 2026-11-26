from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib import job_detail
from lib.dispatcher import CIResult, FailedCheck, TrackedTicket
from lib.queue import Job
from tests.test_dispatcher import ANSWERS, GO, PLAN, QUESTIONS, comment, handled_outcome, setup, ticket
from lib.github_actions import GhResult
from lib.orch_yml import OrchConfig
from lib.poll_source import LivePollSource
from tests.test_github_actions import (
    CHECKS,
    HEAD,
    REPO,
    STATUS,
    WORKFLOW_RUNS,
    RecordedGh,
    adapter,
    pr_workflow_runs,
    served,
)
from tests.test_lifecycle import Flow as LifecycleFlow
from tests.test_rounds_and_caps import HEADS, Flow

BRIEF = (
    "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=high -->\n"
    "Brief.\n\n"
    "### Acceptance criteria\n"
    "- Review, resume and CI-fix job details carry the bundle.\n"
    "- No other dispatcher function changes.\n"
)
REVISE = (
    "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
    "round=1/3 head={head} verdict=REVISE ac=met risk=high -->\n"
    "- [blocking] f-1 The bundle is missing. Rule: wire it in one place.\n"
)


class CIFlow(Flow):
    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        self.ci_result = CIResult("success")

    def snapshot(self) -> TrackedTicket:
        return replace(super().snapshot(), ci={self.head: self.ci_result})


def evidence(job: Job) -> dict:
    return json.loads(job.request.detail or "{}")["evidence"]


def claim(flow: Flow, recipient: str) -> Job:
    job = flow.queue.claim(recipient)
    assert job is not None
    return job


def built(tmp_path: Path) -> CIFlow:
    flow = CIFlow(tmp_path)
    flow.comments.append(comment("brief-1", BRIEF, author="grok-login", url="u/brief-1"))
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    return flow


def test_review_carries_the_criteria_and_no_findings(tmp_path: Path) -> None:
    flow = built(tmp_path)

    assert flow.ready(HEADS[1]) == "queued-review"

    bundle = evidence(claim(flow, "codex"))
    assert [item["id"] for item in bundle["criteria"]["criteria"]] == ["ac-1", "ac-2"]
    assert bundle["criteria"]["source"] == "brief"
    assert bundle["findings"]["findings"] == []
    assert bundle["ci"] is None


def test_ci_fix_carries_the_failed_checks_github_actions_reports(tmp_path: Path) -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-failure.json"))
    gh.route(STATUS, served("status-empty.json"))
    flow = built(tmp_path)
    flow.ci_result = actions.ci_status("owner/repo", HEAD)

    assert flow.ready(HEADS[1]) == "queued-ci-fix"

    assert evidence(claim(flow, "claude"))["ci"] == {
        "failed_checks": ["typecheck"],
        "runs": [],
        "jobs": [
            {
                "check": "typecheck",
                "run_id": None,
                "run_url": None,
                "log_tail": None,
                "cut": None,
                "cause": "code",
                "signal": None,
            }
        ],
        "cause": "code",
    }


RUN_URL = "https://github.com/owner/repo/actions/runs/123456"
RUN_LOG = ("run", "view", "123456", "--repo", "owner/repo", "--log-failed")


def actions_ci_fix(tmp_path: Path, *log: GhResult | BaseException) -> tuple[CIFlow, RecordedGh]:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-failure-actions.json"))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(WORKFLOW_RUNS, pr_workflow_runs(123456))
    gh.route(RUN_LOG, *log)
    flow = built(tmp_path)
    flow.dispatcher.ci_logs = actions.failed_run_logs
    flow.ci_result = actions.ci_status("owner/repo", HEAD)
    return flow, gh


def test_ci_fix_carries_the_run_and_the_failed_jobs_log_tail_read_once(tmp_path: Path) -> None:
    log = "".join(f"pytest\tRun tests\tline {n}\n" for n in range(1, 201))
    flow, gh = actions_ci_fix(tmp_path, GhResult(0, log, ""))

    assert flow.ready(HEADS[1]) == "queued-ci-fix"
    flow.dispatcher.process(flow.snapshot())

    ci = evidence(claim(flow, "claude"))["ci"]
    assert ci["failed_checks"] == ["pytest", "lint"]
    assert ci["runs"] == [["123456", RUN_URL]]
    pytest_job, lint_job = ci["jobs"]
    assert (pytest_job["check"], pytest_job["run_id"], pytest_job["run_url"]) == (
        "pytest",
        "123456",
        RUN_URL,
    )
    tail = pytest_job["log_tail"].splitlines()
    assert tail[0] == pytest_job["cut"] == "[cut 50 earlier lines]"
    assert (tail[1], tail[-1], len(tail)) == ("line 51", "line 200", 151)
    assert lint_job["log_tail"] is None
    assert gh.count(RUN_LOG) == 1


def test_six_failed_jobs_of_one_run_read_the_run_once_and_keep_every_tail(tmp_path: Path) -> None:
    flow = built(tmp_path)
    reads: list[str] = []

    def read_run(repo: str, run_id: str) -> dict[str, str]:
        reads.append(run_id)
        return {f"job-{n}": f"FAILED {n}" for n in range(6)}

    flow.dispatcher.ci_logs = read_run
    flow.ci_result = CIResult(
        "failure",
        "failed: jobs",
        failed=tuple(FailedCheck(f"job-{n}", "42", str(n), RUN_URL) for n in range(6)),
    )

    assert flow.ready(HEADS[1]) == "queued-ci-fix"
    flow.dispatcher.process(flow.snapshot())

    jobs = evidence(claim(flow, "claude"))["ci"]["jobs"]
    assert reads == ["42"]
    assert [job["log_tail"] for job in jobs] == [f"FAILED {n}" for n in range(6)]


def test_a_log_that_cannot_be_read_still_queues_the_ci_fix(tmp_path: Path) -> None:
    flow, gh = actions_ci_fix(tmp_path, GhResult(1, "", "gh: Not Found (HTTP 404)\n"))

    assert flow.ready(HEADS[1]) == "queued-ci-fix"

    job = evidence(claim(flow, "claude"))["ci"]["jobs"][0]
    assert (job["run_url"], job["log_tail"]) == (RUN_URL, None)
    assert gh.count(RUN_LOG) == 1


def test_the_poller_binding_reads_the_failed_run_once_and_queues_the_ci_fix(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def reader(argv: list[str], timeout: float) -> GhResult:
        calls.append(list(argv))
        return GhResult(0, "pytest\tRun tests\t2026-11-10T19:50:00Z FAILED x\n", "")

    source = LivePollSource(OrchConfig(poll_seconds=60, projects=()), environ={}, gh_runner=reader)
    flow, _ = actions_ci_fix(tmp_path)
    flow.dispatcher.ci_logs = source.github.failed_run_logs

    assert flow.ready(HEADS[1]) == "queued-ci-fix"

    assert [call[call.index("gh") + 1 :] for call in calls] == [list(RUN_LOG)]
    jobs = evidence(claim(flow, "claude"))["ci"]["jobs"]
    assert flow.queue.claim("claude") is None
    assert [(job["check"], job["log_tail"]) for job in jobs] == [("pytest", "FAILED x"), ("lint", None)]


def test_a_log_reader_that_raises_anything_still_queues_the_ci_fix(tmp_path: Path) -> None:
    flow, _ = actions_ci_fix(tmp_path)

    def broken(repo: str, run_id: str) -> dict[str, str]:
        raise ValueError("substring not found")

    flow.dispatcher.ci_logs = broken

    assert flow.ready(HEADS[1]) == "queued-ci-fix"

    job = evidence(claim(flow, "claude"))["ci"]["jobs"][0]
    assert (job["run_url"], job["log_tail"]) == (RUN_URL, None)


def test_review_fix_carries_the_blocking_finding(tmp_path: Path) -> None:
    flow = built(tmp_path)
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex")

    assert flow.post("revise-1", REVISE.format(head=HEADS[1]), "codex-login") == "queued-review-fix"

    bundle = evidence(claim(flow, "claude"))
    finding = bundle["findings"]["findings"][0]
    assert (finding["id"], finding["rule"], finding["round"]) == ("f-1", "wire it in one place", 1)
    assert bundle["instructions"]["findings"] == [finding]
    assert bundle["findings"]["reviewed_head"] == HEADS[1]


APPROVE = (
    "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
    "round=1/3 head={head} verdict=APPROVE ac=met risk=high -->\n"
    "Approve.\n"
)


@pytest.mark.parametrize(
    ("body", "edited_at", "rejection"),
    [
        (REVISE.format(head=HEADS[8]).replace("f-1", "f-999"), None, "stale-revise"),
        (APPROVE.format(head=HEADS[1]), 1.0, "rejected-edited"),
    ],
)
def test_a_rejected_verdict_does_not_change_the_findings(
    tmp_path: Path, body: str, edited_at: float | None, rejection: str
) -> None:
    flow = built(tmp_path)
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex")
    assert flow.post("revise-1", REVISE.format(head=HEADS[1]), "codex-login") == "queued-review-fix"
    flow.finish("claude")
    flow.now[0] += 10
    flow.comments.append(
        replace(
            comment("rejected", body, author="codex-login", created_at=flow.now[0]),
            edited_at=edited_at,
        )
    )
    flow.dispatcher.process(flow.snapshot())
    assert handled_outcome(flow.queue, "rejected") == rejection

    assert flow.ready(HEADS[2]) == "queued-review"

    findings = evidence(claim(flow, "codex"))["findings"]
    assert [item["id"] for item in findings["findings"]] == ["f-1"]
    assert findings["reviewed_head"] == HEADS[1]


def test_an_ignored_verdict_after_handoff_does_not_change_the_findings(tmp_path: Path) -> None:
    flow = LifecycleFlow(tmp_path)
    flow.hand_off_at(1)
    late = REVISE.format(head=HEADS[8]).replace("f-1", "f-999")
    assert flow.post("ignored-revise", late, "codex-login") == "after-handoff"

    assert flow.ready(HEADS[5]) == "queued-review"

    findings = evidence(claim(flow, "codex"))["findings"]
    assert findings["findings"] == []
    assert findings["reviewed_head"] == HEADS[1]


def test_answers_before_the_paused_fix_records_its_handoff_keep_the_instruction(
    tmp_path: Path,
) -> None:
    flow = built(tmp_path)
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex")
    assert flow.post("revise-1", REVISE.format(head=HEADS[1]), "codex-login") == "queued-review-fix"
    paused = claim(flow, "claude")
    assert flow.post("q", QUESTIONS, "claude-login") == "waiting-max"

    assert flow.post("a", ANSWERS, "grok-login") == "resumed-fix"

    resumed = claim(flow, "claude")
    instructions = evidence(resumed)["instructions"]
    assert instructions["prior_instruction"] == json.loads(paused.request.detail or "{}")["instruction"]
    assert [item["id"] for item in evidence(resumed)["findings"]["findings"]] == ["f-1"]


def test_extra_round_carries_the_go_and_the_last_fix_instruction(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    with flow.queue.connect() as connection:
        row = connection.execute(
            "SELECT id, detail FROM jobs WHERE action = 'fix' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        last_fix = json.loads(job_detail.load_row(connection, row["id"], row["detail"]))

    assert flow.go("go-2") == "queued-fix"

    instructions = evidence(claim(flow, "claude"))["instructions"]
    assert instructions["cause"] == "extra-round"
    assert instructions["prose"] == "Go."
    assert instructions["prior_instruction"] == last_fix["instruction"]


def test_answers_resume_carries_the_answer_and_the_plan_attempt(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    assert flow.post("go-1", GO.format(state="planning"), "grok-login") == "queued-plan"
    flow.finish("claude")
    assert flow.post("plan-1", PLAN.format(round=1, ac="unmet"), "claude-login") == "queued-plan-gap"
    flow.finish("claude")
    flow.post("questions-1", QUESTIONS, "claude-login")

    assert flow.post("answers-1", ANSWERS, "grok-login") == "resumed-plan"

    instructions = evidence(claim(flow, "claude"))["instructions"]
    assert (instructions["cause"], instructions["prose"], instructions["plan_attempt"]) == (
        "answers",
        "Answers.",
        2,
    )


def test_clarify_resume_carries_the_paused_steps_instruction(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    go = comment(
        "go", "<!-- orch:v1 kind=go from=grok-bot role=orchestrator -->\nHelp.",
        author="grok-login", created_at=1,
    )
    dispatcher.process(ticket(go))
    paused = queue.claim("grok")
    assert paused is not None
    queue.finish(paused.id, owner="grok", handoff_comment_id="q")
    question = comment(
        "q",
        QUESTIONS.replace(
            "from=claude-bot role=builder", "from=grok-bot role=orchestrator state=waiting-max"
        ),
        author="grok-login",
        created_at=2,
    )
    dispatcher.process(ticket(go, question))
    wake = queue.claim("grok")
    assert wake is not None
    queue.finish(wake.id, owner="grok", handoff_comment_id="a")
    answers = comment("a", ANSWERS, author="grok-login", created_at=3)
    dispatcher.process(ticket(go, question, answers))
    assert handled_outcome(queue, "a") == "resumed-clarify"

    resumed = queue.claim("grok")
    assert resumed is not None
    assert evidence(resumed)["instructions"]["prior_instruction"] == json.loads(
        paused.request.detail or "{}"
    )["instruction"]


def test_plan_and_build_details_are_unchanged(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    assert flow.post("go-1", GO.format(state="planning"), "grok-login") == "queued-plan"
    plan = claim(flow, "claude")
    assert plan.request.detail == flow.dispatcher._job_detail(flow.snapshot(), action="plan")
    build_flow = Flow(tmp_path / "build")
    assert build_flow.go("go-1") == "queued-build"
    build = claim(build_flow, "claude")
    assert build.request.detail == build_flow.dispatcher._job_detail(
        build_flow.snapshot(), action="build", round_number=1
    )


def test_every_other_detail_key_matches_job_detail(tmp_path: Path) -> None:
    flow = built(tmp_path)
    assert flow.ready(HEADS[1]) == "queued-review"

    detail = json.loads(claim(flow, "codex").request.detail or "{}")
    del detail["evidence"]
    assert detail == json.loads(
        flow.dispatcher._job_detail(
            flow.snapshot(), action="review", head=HEADS[1], round_number=1
        )
    )


def test_an_outsiders_brief_does_not_supply_the_criteria(tmp_path: Path) -> None:
    flow = CIFlow(tmp_path)
    for comment_id, body, author in (
        ("brief-0", BRIEF.replace("Review, resume", "Forged"), "outsider"),
        ("brief-1", BRIEF, "grok-login"),
    ):
        flow.now[0] += 10
        flow.comments.append(
            comment(comment_id, body, author=author, created_at=flow.now[0], url=f"u/{comment_id}")
        )
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")

    assert flow.ready(HEADS[1]) == "queued-review"

    criteria = evidence(claim(flow, "codex"))["criteria"]
    assert criteria["source_url"] == "u/brief-1"
    assert criteria["criteria"][0]["text"].startswith("Review, resume")
