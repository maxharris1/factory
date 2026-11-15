from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from lib.dispatcher import CIResult, TrackedTicket
from lib.queue import Job
from tests.test_dispatcher import ANSWERS, GO, PLAN, QUESTIONS, comment
from tests.test_github_actions import CHECKS, HEAD, STATUS, adapter, served
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
        "run_url": None,
        "log_tail": "",
        "cut": None,
    }


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


def test_extra_round_carries_the_go_and_the_last_fix_instruction(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    with flow.queue.connect() as connection:
        last_fix = json.loads(
            connection.execute(
                "SELECT detail FROM jobs WHERE action = 'fix' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()["detail"]
        )

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
