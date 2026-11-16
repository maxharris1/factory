from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.dispatch.plan import _proposed_split
from lib.queue import Job
from tests.test_approver_credential import APP_BOT, MAX, grok_wakes
from tests.test_brief import FACTORY_ROOT
from tests.test_dispatcher import ANSWERS, GO, comment, job_count
from tests.test_resume_plan import PR, ResumeFlow

SPLIT = "## Proposed split\n1. `brief half` (ac-1, ac-2): about 200 lines.\n2. `dispatch half` (ac-3): about 300 lines.\n"
PLAN_BODY = f"## Plan\n- ac-1: the brief. Proof: a snapshot.\n\n{SPLIT}\n## Out of scope\nNothing.\n"


def plan_header(fields: str, plan_round: int = 1) -> str:
    return f"<!-- orch:v1 kind=plan from=claude-bot role=builder round={plan_round}/2 {fields} -->\n"


def brief(criteria: int) -> str:
    items = "".join(f"- Criterion {n}.\n" for n in range(1, criteria + 1))
    return f"<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->\n## Acceptance criteria\n{items}"


class SizeFlow(ResumeFlow):
    def __init__(self, tmp_path: Path, *, criteria: int = 2, max_lines: int = 400, max_criteria: int = 5):
        super().__init__(tmp_path, enabled=True)
        self.factory = replace(self.factory, plan_max_lines=max_lines, plan_max_criteria=max_criteria)
        self.comments.append(comment("brief-1", brief(criteria), author="grok-login", created_at=self.now[0]))
        assert self.say("1", GO.format(state="planning"), "grok-login") == "queued-plan"

    def plan(self, comment_id: str, fields: str, body: str = PLAN_BODY, plan_round: int = 1) -> str:
        self.finish_with(comment_id)
        return self.say(comment_id, plan_header(fields, plan_round) + body, "claude-login")

    def claim(self, action: str) -> Job:
        job = self.queue.claim("claude")
        assert job is not None and job.request.action == action
        return job


def size_wakes(flow: SizeFlow) -> list[dict]:
    return [json.loads(row["detail"]) for row in grok_wakes(flow, "clarify") if row["source_comment_id"] == "500"]


@pytest.mark.parametrize(
    ("lines", "outcome"),
    [("400", "queued-plan-build"), ("401", "waiting-max-size")],
)
def test_the_line_sits_between_400_and_401_expected_lines(tmp_path: Path, lines: str, outcome: str) -> None:
    flow = SizeFlow(tmp_path)

    assert flow.plan("500", f"ac=met lines={lines}") == outcome

    if outcome == "queued-plan-build":
        assert flow.state()["workflow_state"] == "building"
        assert json.loads(flow.claim("build").request.detail)["plan_text"] == PLAN_BODY.strip()
        assert size_wakes(flow) == []
        return
    state = flow.state()
    assert state["workflow_state"] == "waiting-max"
    assert (state["paused_action"], state["paused_question_id"]) == ("build", "500")
    assert job_count(flow.queue, "build") == 0
    [wake] = size_wakes(flow)
    assert wake["detail"] == (
        f"plan-over-size: lines=401 (line 400), criteria=2 (line 5), {PR}500\n{SPLIT.strip()}"
    )


@pytest.mark.parametrize(("criteria", "outcome"), [(5, "queued-plan-build"), (6, "waiting-max-size")])
def test_more_than_five_criteria_waits_at_any_size(tmp_path: Path, criteria: int, outcome: str) -> None:
    flow = SizeFlow(tmp_path, criteria=criteria)

    assert flow.plan("500", "ac=met lines=10") == outcome
    assert job_count(flow.queue, "build") == (outcome == "queued-plan-build")


def test_the_project_sets_its_own_line(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path, max_lines=50, max_criteria=1)

    assert flow.plan("500", "ac=met lines=60", body="Plan.\n") == "waiting-max-size"
    [wake] = size_wakes(flow)
    assert wake["detail"] == (
        f"plan-over-size: lines=60 (line 50), criteria=2 (line 1), {PR}500\nno split proposed"
    )


@pytest.mark.parametrize(
    ("author", "body"),
    [("grok-login", ANSWERS), (MAX, GO.format(state="building")), (MAX, "/go\nBuild it as one PR.")],
    ids=["answers", "approver-go", "slash-go"],
)
def test_answers_or_an_approver_go_build_with_the_plan(tmp_path: Path, author: str, body: str) -> None:
    flow = SizeFlow(tmp_path)
    assert flow.plan("500", "ac=met lines=760") == "waiting-max-size"

    assert flow.say("501", body, author) == "resumed-build"

    assert flow.state()["workflow_state"] == "building"
    build = flow.claim("build")
    assert json.loads(build.request.detail)["plan_text"] == PLAN_BODY.strip()
    task = render_brief(build, bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
    assert "## Proposed split" in task.split("## Plan\n", 1)[1]


def test_a_go_from_a_bot_leaves_the_size_wait(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path)
    assert flow.plan("500", "ac=met lines=760") == "waiting-max-size"

    assert flow.say("501", GO.format(state="building"), APP_BOT) == "go-not-from-approver"

    assert flow.state()["workflow_state"] == "waiting-max"
    assert job_count(flow.queue, "build") == 0


def test_a_replayed_over_size_plan_queues_nothing_new(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path)
    assert flow.plan("500", "ac=met lines=760") == "waiting-max-size"
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = '500'")
        jobs = connection.execute("SELECT count(*) FROM jobs").fetchone()[0]

    flow.dispatcher.process(flow.snapshot())

    with flow.queue.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == jobs
    assert flow.state()["workflow_state"] == "waiting-max"
    assert len(size_wakes(flow)) == 1


def test_a_plan_without_lines_is_a_plan_with_a_gap(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path)

    assert flow.plan("500", "ac=met") == "queued-plan-gap"

    second = flow.claim("plan")
    detail = json.loads(second.request.detail)
    assert (detail["plan_round"], detail["unmapped"], detail["plan_text"]) == (2, "expected size", PLAN_BODY.strip())
    flow.queue.finish(second.id, owner="claude", handoff_comment_id="600")
    assert flow.say("600", plan_header("ac=met", 2) + "Plan.\n", "claude-login") == "stuck-plan-gaps"
    assert job_count(flow.queue, "build") == 0


def test_a_round_2_plan_brief_names_the_unmapped_criterion_and_quotes_the_first_plan(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path)

    assert flow.plan("500", "ac=unmet lines=120 unsat=ac-2") == "queued-plan-gap"

    task = render_brief(flow.claim("plan"), bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
    assert "The first plan left `ac-2` unmapped." in task
    assert "- ac-1: the brief. Proof: a snapshot." in task.split("## First plan\n", 1)[1]


def test_the_split_section_ends_at_the_next_heading_of_its_level() -> None:
    assert _proposed_split(PLAN_BODY) == SPLIT.strip()
    assert _proposed_split("### Proposed split\nA.\n#### Detail\nB.\n## Next\nC.") == (
        "### Proposed split\nA.\n#### Detail\nB."
    )
    assert _proposed_split("Plan.") == "no split proposed"
    cut = _proposed_split("## Proposed split\n" + "x" * 5000)
    assert len(cut) < 4100 and cut.endswith("read the rest in the plan]")
