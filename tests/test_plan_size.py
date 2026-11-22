from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.evidence import TEXT_LIMIT
from lib.factory_md import padded_estimate
from lib.feeder import parse_intake
from lib.dispatch.types import TrackedTicket
from lib.followups import has_marker, proposed_split, split_link_marker
from lib.queue import Job
from tests.test_approver_credential import APP_BOT, MAX, grok_wakes
from tests.test_brief import FACTORY_ROOT
from tests.test_dispatcher import ANSWERS, GO, comment, handled_outcome, job_count
from tests.test_followup_findings import FollowupActions
from tests.test_resume_plan import PR, ResumeFlow

SPLIT = "## Proposed split\n1. `brief half` (ac-1, ac-2): about 200 lines.\n2. `dispatch half` (ac-3): about 300 lines.\n"
PLAN_BODY = f"## Plan\n- ac-1: the brief. Proof: a snapshot.\n\n{SPLIT}\n## Out of scope\nNothing.\n"


def estimated(estimate: int, body: str = PLAN_BODY) -> str:
    return f"Estimate: {estimate}\n\n{body}"


def plan_header(fields: str, plan_round: int = 1) -> str:
    return f"<!-- orch:v1 kind=plan from=claude-bot role=builder round={plan_round}/2 {fields} -->\n"


def brief(criteria: int, pad: str = "") -> str:
    items = "".join(f"- Criterion {n}.{pad}\n" for n in range(1, criteria + 1))
    return f"<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->\n## Acceptance criteria\n{items}"


class SizeFlow(ResumeFlow):
    def __init__(
        self, tmp_path: Path, *, criteria: int = 2, max_lines: int = 400, max_criteria: int = 5, pad: str = ""
    ):
        super().__init__(tmp_path, enabled=True)
        self.factory = replace(self.factory, plan_max_lines=max_lines, plan_max_criteria=max_criteria)
        self.comments.append(comment("brief-1", brief(criteria, pad), author="grok-login", created_at=self.now[0]))
        assert self.say("1", GO.format(state="planning"), "grok-login") == "queued-plan"

    def plan(self, comment_id: str, fields: str, body: str, plan_round: int = 1) -> str:
        self.finish_with(comment_id)
        return self.say(comment_id, plan_header(fields, plan_round) + body, "claude-login")

    def claim(self, action: str) -> Job:
        job = self.queue.claim("claude")
        assert job is not None and job.request.action == action
        return job


def size_wakes(flow: SizeFlow) -> list[dict]:
    return [json.loads(row["detail"]) for row in grok_wakes(flow, "clarify") if row["source_comment_id"] == "500"]


def test_the_estimate_is_padded_by_1_35_and_rounded_up() -> None:
    assert (padded_estimate(296), padded_estimate(297), padded_estimate(300)) == (400, 401, 405)
    assert (padded_estimate(0), padded_estimate(1)) == (0, 2)


@pytest.mark.parametrize(
    ("estimate", "outcome"),
    [(296, "queued-plan-build"), (297, "waiting-max-size")],
)
def test_the_line_sits_between_estimates_296_and_297(tmp_path: Path, estimate: int, outcome: str) -> None:
    flow = SizeFlow(tmp_path)

    assert flow.plan("500", "ac=met", estimated(estimate)) == outcome

    if outcome == "queued-plan-build":
        assert flow.state()["workflow_state"] == "building"
        assert json.loads(flow.claim("build").request.detail)["plan_text"] == estimated(estimate).strip()
        assert size_wakes(flow) == []
        return
    state = flow.state()
    assert state["workflow_state"] == "waiting-max"
    assert (state["paused_action"], state["paused_question_id"]) == ("build", "500")
    assert job_count(flow.queue, "build") == 0
    [wake] = size_wakes(flow)
    assert wake["detail"] == (
        f"plan-over-size: estimate=297 x1.35=401 (line 400), criteria=2 (line 5), {PR}500\n{SPLIT.strip()}"
    )


@pytest.mark.parametrize(("criteria", "outcome"), [(5, "queued-plan-build"), (6, "waiting-max-size")])
def test_more_than_five_criteria_waits_at_any_size(tmp_path: Path, criteria: int, outcome: str) -> None:
    flow = SizeFlow(tmp_path, criteria=criteria)

    assert flow.plan("500", "ac=met", estimated(10)) == outcome
    assert job_count(flow.queue, "build") == (outcome == "queued-plan-build")


def test_criteria_cut_from_a_long_brief_still_count(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path, criteria=6, pad=" " + "x" * (TEXT_LIMIT // 5))

    assert flow.plan("500", "ac=met", estimated(40)) == "waiting-max-size"
    assert job_count(flow.queue, "build") == 0
    [wake] = size_wakes(flow)
    assert "criteria=6 (line 5)" in wake["detail"]


def test_the_project_sets_its_own_line(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path, max_lines=50, max_criteria=1)

    assert flow.plan("500", "ac=met", estimated(40, "Plan.\n")) == "waiting-max-size"
    [wake] = size_wakes(flow)
    assert wake["detail"] == (
        f"plan-over-size: estimate=40 x1.35=54 (line 50), criteria=2 (line 1), {PR}500\nno split proposed"
    )


@pytest.mark.parametrize(
    ("author", "body"),
    [("grok-login", ANSWERS), (MAX, GO.format(state="building")), (MAX, "/go\nBuild it as one PR.")],
    ids=["answers", "approver-go", "slash-go"],
)
def test_answers_or_an_approver_go_build_with_the_plan(tmp_path: Path, author: str, body: str) -> None:
    flow = SizeFlow(tmp_path)
    assert flow.plan("500", "ac=met", estimated(760)) == "waiting-max-size"

    assert flow.say("501", body, author) == "resumed-build"

    assert flow.state()["workflow_state"] == "building"
    build = flow.claim("build")
    assert json.loads(build.request.detail)["plan_text"] == estimated(760).strip()
    task = render_brief(build, bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
    assert "## Proposed split" in task.split("## Plan\n", 1)[1]


@pytest.mark.parametrize(
    ("author", "body"), [("grok-login", ANSWERS), (MAX, "/go\nBuild it.")], ids=["answers", "slash-go"]
)
def test_an_edit_after_the_size_wait_does_not_change_the_plan_that_builds(
    tmp_path: Path, author: str, body: str
) -> None:
    flow = SizeFlow(tmp_path)
    assert flow.plan("500", "ac=met", estimated(760)) == "waiting-max-size"
    flow.comments[-1] = replace(
        flow.comments[-1],
        body=plan_header("ac=met") + estimated(760, "REPLACEMENT plan not present in the size wake."),
        edited_at=flow.now[0] + 1,
    )

    assert flow.say("501", body, author) == "resumed-build"

    assert json.loads(flow.claim("build").request.detail)["plan_text"] == estimated(760).strip()
    assert flow.state()["paused_plan_text"] is None


@pytest.mark.parametrize(
    ("estimate", "outcome"),
    [("9" * 4301, "queued-plan-gap"), ("999999999", "waiting-max-size")],
    ids=["past-nine-digits", "nine-digits"],
)
def test_an_estimate_past_nine_digits_is_no_estimate_and_nine_digits_park(
    tmp_path: Path, estimate: str, outcome: str
) -> None:
    flow = SizeFlow(tmp_path)

    assert flow.plan("500", "ac=met", f"Estimate: {estimate}\n\n{PLAN_BODY}") == outcome

    assert job_count(flow.queue, "build") == 0
    if outcome == "queued-plan-gap":
        assert json.loads(flow.claim("plan").request.detail)["unmapped"] == "expected size"


@pytest.mark.parametrize("estimate", ["1,000", "1 000", "1.000", "1k"])
def test_a_malformed_estimate_never_builds_on_its_prefix(tmp_path: Path, estimate: str) -> None:
    flow = SizeFlow(tmp_path)

    assert flow.plan("500", "ac=met", f"Estimate: {estimate}\n\n{PLAN_BODY}") == "queued-plan-gap"

    assert job_count(flow.queue, "build") == 0
    assert json.loads(flow.claim("plan").request.detail)["unmapped"] == "expected size"


def test_a_go_from_a_bot_leaves_the_size_wait(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path)
    assert flow.plan("500", "ac=met", estimated(760)) == "waiting-max-size"

    assert flow.say("501", GO.format(state="building"), APP_BOT) == "go-not-from-approver"

    assert flow.state()["workflow_state"] == "waiting-max"
    assert job_count(flow.queue, "build") == 0


def test_a_replayed_over_size_plan_queues_nothing_new(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path)
    assert flow.plan("500", "ac=met", estimated(760)) == "waiting-max-size"
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = '500'")
        jobs = connection.execute("SELECT count(*) FROM jobs").fetchone()[0]

    flow.dispatcher.process(flow.snapshot())

    with flow.queue.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == jobs
    assert flow.state()["workflow_state"] == "waiting-max"
    assert len(size_wakes(flow)) == 1


def test_a_plan_without_an_estimate_is_a_plan_with_a_gap(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path)

    assert flow.plan("500", "ac=met", PLAN_BODY) == "queued-plan-gap"

    second = flow.claim("plan")
    detail = json.loads(second.request.detail)
    assert (detail["plan_round"], detail["unmapped"], detail["plan_text"]) == (2, "expected size", PLAN_BODY.strip())
    flow.queue.finish(second.id, owner="claude", handoff_comment_id="600")
    assert flow.say("600", plan_header("ac=met", 2) + "Plan.\n", "claude-login") == "stuck-plan-gaps"
    assert job_count(flow.queue, "build") == 0


def test_a_round_2_plan_brief_names_the_unmapped_criterion_and_quotes_the_first_plan(tmp_path: Path) -> None:
    flow = SizeFlow(tmp_path)

    assert flow.plan("500", "ac=unmet unsat=ac-2", estimated(120)) == "queued-plan-gap"

    task = render_brief(flow.claim("plan"), bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
    assert "The first plan left `ac-2` unmapped." in task
    assert "- ac-1: the brief. Proof: a snapshot." in task.split("## First plan\n", 1)[1]


def test_the_split_section_ends_at_the_next_heading_of_its_level() -> None:
    assert proposed_split(PLAN_BODY) == SPLIT.strip()
    assert proposed_split("### Proposed split\nA.\n#### Detail\nB.\n## Next\nC.") == (
        "### Proposed split\nA.\n#### Detail\nB."
    )
    assert proposed_split("Plan.") == "no split proposed"
    cut = proposed_split("## Proposed split\n" + "x" * 5000)
    assert len(cut) < 4100 and cut.endswith("read the rest in the plan]")


PARTS = (
    "## Proposed split\n\n"
    "### Brief half\n- ac-1: the brief renders the parts.\n\nEstimate: 140\n\n"
    "### Dispatch half\n- ac-2: the dispatcher files the parts.\n\nEstimate: 160\n"
)
SPLIT_BODY = f"## Plan\n- ac-1: the brief.\n- ac-2: the dispatcher.\n\n{PARTS}\n## Out of scope\nNothing.\n"


class FlakyCreates(FollowupActions):
    def __init__(self, fail_at: int = 0) -> None:
        super().__init__()
        self.fail_at = fail_at

    def file_issue(self, *args) -> int:
        if self.fail_at and self.creates + 1 == self.fail_at:
            self.fail_at = 0
            raise RuntimeError("transient GitHub failure")
        return super().file_issue(*args)


class SplitFlow(SizeFlow):
    def __init__(
        self, tmp_path: Path, *, fail_at: int = 0, tickets: str = "github", intake_label: str | None = "factory:ready"
    ):
        self.intake_label = intake_label
        self.closed = False
        super().__init__(tmp_path)
        self.actions = FlakyCreates(fail_at)
        self.dispatcher.review_actions = self.actions
        self.factory = replace(self.factory, tickets=tickets)

    def snapshot(self) -> TrackedTicket:
        return replace(super().snapshot(), intake_label=self.intake_label, source_issue="#1117", closed=self.closed)

    def poll(self) -> None:
        self.now[0] += 10
        self.dispatcher.process(self.snapshot())

    def links(self) -> list[str]:
        return [text for text in self.actions.pr_comments if has_marker(text, split_link_marker("500"))]

    def transitions(self, to_state: str) -> list[tuple[str, str]]:
        with self.queue.connect() as connection:
            rows = connection.execute(
                "SELECT from_state, source FROM ticket_transitions WHERE ticket_id = '42' AND to_state = ?",
                (to_state,),
            ).fetchall()
        return [(row["from_state"], row["source"]) for row in rows]


def test_an_over_size_parsed_split_files_its_parts_and_abandons(tmp_path: Path) -> None:
    flow = SplitFlow(tmp_path)

    assert flow.plan("500", "ac=met", estimated(300, SPLIT_BODY)) == "auto-split"

    assert list(flow.actions.issues) == [300, 301]
    (first_title, first, first_labels), (second_title, second, second_labels) = flow.actions.issues.values()
    assert (first_title, second_title) == ("Brief half", "Dispatch half")
    assert first_labels == second_labels == ("factory:ready",)
    for body in (first, second):
        assert body.startswith(f"Split from #1117 (plan {PR}500).\n")
        assert "Risk: high\nPlan first: no\n" in body
    assert "Depends on: none" in first and "- ac-1: the brief renders the parts." in first
    assert "Depends on: #300" in second and "Estimate: 160" in second
    [link] = flow.links()
    assert "#300" in link and "#301" in link
    state = flow.state()
    assert (state["workflow_state"], state["paused_plan_text"], state["plan_check_comment_id"]) == (
        "abandoned", None, None
    )
    assert flow.transitions("abandoned") == [("plan-check", "plan-auto-split")]
    assert job_count(flow.queue, "build") == 0
    assert size_wakes(flow) == []


class RecordingActions(FlakyCreates):
    def __init__(self) -> None:
        super().__init__()
        self.called: list[str] = []

    def __getattribute__(self, name: str):
        value = super().__getattribute__(name)
        if callable(value) and not name.startswith("_"):
            super().__getattribute__("called").append(name)
        return value


def test_an_auto_split_leaves_the_parent_issue_open(tmp_path: Path) -> None:
    flow = SplitFlow(tmp_path)
    flow.actions = RecordingActions()
    flow.dispatcher.review_actions = flow.actions

    assert flow.plan("500", "ac=met", estimated(300, SPLIT_BODY)) == "auto-split"

    assert list(flow.actions.issues) == [300, 301]
    assert flow.state()["workflow_state"] == "abandoned"
    assert "file_issue" in flow.actions.called
    assert not [name for name in flow.actions.called if "close" in name]
    assert 1117 not in flow.actions.closed
    assert 1117 not in flow.actions.issue_comments


def test_a_failed_filing_leaves_the_ticket_at_plan_check_and_the_next_poll_finishes(tmp_path: Path) -> None:
    flow = SplitFlow(tmp_path, fail_at=2)

    assert flow.plan("500", "ac=met", estimated(300, SPLIT_BODY)) == "auto-split"

    assert list(flow.actions.issues) == [300]
    assert flow.links() == []
    assert flow.state()["workflow_state"] == "plan-check"
    assert any(ref.ticket_id == "42" for ref in flow.dispatcher._tracked_refs(flow.now[0]))

    flow.poll()

    assert list(flow.actions.issues) == [300, 301]
    assert len(flow.links()) == 1
    assert flow.state()["workflow_state"] == "abandoned"


def test_a_crash_after_the_link_comment_abandons_without_writing_again(tmp_path: Path) -> None:
    flow = SplitFlow(tmp_path)
    flow.actions.crash_after_post = True

    with pytest.raises(SystemExit):
        flow.plan("500", "ac=met", estimated(300, SPLIT_BODY))
    assert flow.state()["workflow_state"] == "plan-check"
    flow.actions.crash_after_post = False
    flow.poll()

    assert flow.actions.creates == 2
    assert len(flow.links()) == 1
    assert flow.transitions("abandoned") == [("plan-check", "plan-auto-split")]


def test_a_replayed_auto_split_plan_never_builds(tmp_path: Path) -> None:
    flow = SplitFlow(tmp_path, fail_at=1)
    assert flow.plan("500", "ac=met", estimated(300, SPLIT_BODY)) == "auto-split"
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = '500'")

    flow.poll()

    assert handled_outcome(flow.queue, "500") == "auto-split"
    assert flow.transitions("building") == []
    assert list(flow.actions.issues) == [300, 301]
    assert flow.state()["workflow_state"] == "abandoned"
    assert job_count(flow.queue, "build") == 0


def test_an_edit_after_the_split_decision_does_not_change_the_parts(tmp_path: Path) -> None:
    flow = SplitFlow(tmp_path, fail_at=1)
    assert flow.plan("500", "ac=met", estimated(300, SPLIT_BODY)) == "auto-split"
    flow.comments[-1] = replace(
        flow.comments[-1],
        body=plan_header("ac=met") + estimated(300, SPLIT_BODY.replace("Brief half", "REPLACEMENT")),
        edited_at=flow.now[0] + 1,
    )

    flow.poll()

    assert [title for title, _, _ in flow.actions.issues.values()] == ["Brief half", "Dispatch half"]


def test_max_closing_the_pr_mid_split_stops_the_drain(tmp_path: Path) -> None:
    flow = SplitFlow(tmp_path, fail_at=2)
    assert flow.plan("500", "ac=met", estimated(300, SPLIT_BODY)) == "auto-split"

    flow.closed = True
    flow.poll()
    flow.closed = False
    flow.poll()

    assert list(flow.actions.issues) == [300]
    assert flow.links() == []
    assert flow.transitions("abandoned") == [("plan-check", "closed-unmerged")]


def test_estimate_296_with_a_parsed_split_still_queues_build(tmp_path: Path) -> None:
    flow = SplitFlow(tmp_path)

    assert flow.plan("500", "ac=met", estimated(296, SPLIT_BODY)) == "queued-plan-build"

    assert flow.actions.issues == {}
    assert flow.state()["workflow_state"] == "building"


@pytest.mark.parametrize(
    ("body", "tickets", "intake_label"),
    [
        (PLAN_BODY, "github", "factory:ready"),
        (SPLIT_BODY.split("### Dispatch half")[0], "github", "factory:ready"),
        (SPLIT_BODY, "linear", "factory:ready"),
        (SPLIT_BODY, "github", None),
    ],
    ids=["numbered-list", "one-part", "linear", "no-intake-label"],
)
def test_a_split_that_does_not_parse_or_a_linear_or_unlabelled_repo_still_parks(
    tmp_path: Path, body: str, tickets: str, intake_label: str | None
) -> None:
    flow = SplitFlow(tmp_path, tickets=tickets, intake_label=intake_label)

    assert flow.plan("500", "ac=met", estimated(300, body)) == "waiting-max-size"

    assert flow.actions.issues == {}
    assert flow.state()["workflow_state"] == "waiting-max"
    assert len(size_wakes(flow)) == 1


WRAPPED_PARTS = (
    "## Proposed split\n"
    "### Parser\n- ac-1: The parser returns values.\n  Invalid inputs must return HTTP 400.\nEstimate: 100\nDepends on: none\n"
    "### Integration\n- ac-2: The app uses the parser.\nEstimate: 100\n"
)


def test_a_split_files_each_wrapped_criterion_whole(tmp_path: Path) -> None:
    flow = SplitFlow(tmp_path)

    assert flow.plan("500", "ac=met", estimated(600, WRAPPED_PARTS)) == "auto-split"

    assert flow.state()["workflow_state"] == "abandoned"
    (_, first, _), (_, second, _) = flow.actions.issues.values()
    assert "- ac-1: The parser returns values. Invalid inputs must return HTTP 400.\n" in first
    assert "Estimate: 100" in first and "Depends on: none" in first
    assert "Depends on: #300" in second


INTAKE_HEAD = "Risk: high\nPlan first: no\nDepends on: none\n\n## Acceptance criteria\n"
FIELD_CONTINUATION = (
    "- ac-1: The summary renders these literal lines.\n"
    "  Estimate: unavailable when the input is missing.\n"
    "  Invalid inputs must return HTTP 400.\n"
)
WRAPPED = "- ac-1: The parser returns values.\n  Invalid inputs must return HTTP 400.\n"
SECOND = "- ac-2: Use the summary.\n"


@pytest.mark.parametrize("criterion", [WRAPPED, FIELD_CONTINUATION], ids=["wrapped", "field-continuation"])
def test_each_child_carries_the_criteria_intake_reads_from_the_plan(tmp_path: Path, criterion: str) -> None:
    plan = (
        "## Proposed split\n### Summary\n" + criterion + "Estimate: 100\nDepends on: none\n"
        "### Integration\n" + SECOND + "Estimate: 100\n"
    )
    flow = SplitFlow(tmp_path)

    assert flow.plan("500", "ac=met", estimated(600, plan)) == "auto-split"

    assert flow.state()["workflow_state"] == "abandoned"
    children = [parse_intake(body, labels) for _, body, labels in flow.actions.issues.values()]
    parents = [parse_intake(INTAKE_HEAD + text, ("factory:ready",)) for text in (criterion, SECOND)]
    assert [child.defects for child in children] == [(), ()]
    assert [child.criteria for child in children] == [parent.criteria for parent in parents]
    assert "Estimate: 100" in flow.actions.issues[300][1]


def test_a_split_whose_child_intake_would_drop_a_criterion_parks_instead(tmp_path: Path) -> None:
    plan = (
        "## Proposed split\n### Restart\n- ac-1: The operator restarts the service on the box.\nEstimate: 100\n"
        "### Integration\n" + SECOND + "Estimate: 100\n"
    )
    flow = SplitFlow(tmp_path)

    assert flow.plan("500", "ac=met", estimated(600, plan)) == "waiting-max-size"

    assert flow.actions.issues == {}
    assert flow.state()["workflow_state"] == "waiting-max"
    assert len(size_wakes(flow)) == 1
