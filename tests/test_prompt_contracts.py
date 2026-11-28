"""#208 (T24): prompt contract tests."""

from __future__ import annotations

import importlib
import itertools
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

import lib.brief.build as build_brief
import lib.brief.plan as plan_brief
import lib.brief.review as review_brief
import lib.brief.skills as brief_skills
from lib.brief import render_brief
from lib.brief.common import GH
from lib.brief.inline import quoted_section
from lib.evidence.findings import blocking_ids, findings_evidence
from lib.factory_md import parse_factory_context
from lib.dispatch.helpers import _REGISTERING_ACTIONS
from lib.dispatcher import Comment
from lib.handoff import HandoffError, parse_comment
from lib.journeys import QaPlan
from lib.orchestrate import APPROVAL_TOKEN_VARIABLES, answers_for, go_comment
from lib.pstack_pin import FACTORY_DIR, KIT_DIR, PSTACK_DIR, ROOT, load_pin
from lib.queue import Job
from tests.regenerate_brief_snapshots import CASES, FACTORY_MD, _job, render_snapshot
from tests.test_approver_credential import MAX, AppFlow
from tests.test_orchestrate import choose_route, request_for
from tests.test_orchestrate_helpers import go_for, to_questions
from tests.test_dispatcher import comment, handled_outcome
from tests.test_rounds_and_caps import APPROVE, HEADS, Flow, outcome

HEADER = re.compile(r"<!-- orch:v1 [^\n`]*?-->")
CHOICE = re.compile(r"<([A-Za-z]+(?:\|[A-Za-z]+)+)>")
PLACEHOLDER = re.compile(r"<[a-z][^<>\n]*>")
brief_rules = importlib.import_module("lib.brief.rules")
FIELDS_LEFT_OUT_WHEN_UNUSED = ("repeat", "unsat")
BOT_LOGINS = {"claude-bot": "claude-login", "codex-bot": "codex-login"}



def brief_for(job: Job) -> str:
    return render_brief(job, bot=f"{job.request.recipient}-bot", context=None, factory_root=str(ROOT))


def headers(brief: str) -> list[str]:
    return HEADER.findall(brief)


def handoff_header(brief: str) -> str:
    section = brief[brief.index("## Handoff comment") :]
    found = headers(section[: section.index("\n## ", 1)])
    assert found, "the handoff section renders no orch:v1 header"
    return found[0]


def filled(header: str, head: str, **choices: str) -> str:
    for field in FIELDS_LEFT_OUT_WHEN_UNUSED:
        header = header.replace(f" {field}=<id>", "")
    header = header.replace("<pushed-head-sha>", head).replace("<reviewed-head-sha>", head)

    def pick(match: re.Match[str]) -> str:
        options = match.group(1).split("|")
        key = header[: match.start()].rsplit(" ", 1)[-1].rstrip("=")
        choice = choices.get(key, options[0])
        assert choice in options, f"{choice!r} is not one of {options} for {key}"
        return choice

    header = CHOICE.sub(pick, header)
    assert PLACEHOLDER.search(header) is None, f"unfilled placeholder in {header}"
    return header


def every_choice(header: str) -> list[dict[str, str]]:
    fields = {
        header[: match.start()].rsplit(" ", 1)[-1].rstrip("="): match.group(1).split("|")
        for match in CHOICE.finditer(header)
    }
    return [dict(zip(fields, combo)) for combo in itertools.product(*fields.values())]


FORBIDDEN_BY_THE_RULES = ({"verdict": "APPROVE", "ac": "unmet"},)


def claim(flow: Flow, recipient: str, action: str) -> Job:
    job = flow.queue.claim(recipient)
    assert job is not None and job.request.action == action
    return job


def post_header(flow: Flow, job: Job, comment_id: str, header: str, body: str = "Done.\n") -> str:
    login = BOT_LOGINS[f"{job.request.recipient}-bot"]
    flow.now[0] += 10
    flow.comments.append(comment(comment_id, f"{header}\n{body}", author=login, created_at=flow.now[0]))
    flow.dispatcher.process(flow.snapshot())
    if parse_comment(header).as_dict()["kind"] in _REGISTERING_ACTIONS:
        assert outcome(flow.queue, comment_id) is None, f"acted on {header} before its job registered"
    flow.queue.finish(job.id, owner=job.request.recipient, handoff_comment_id=comment_id)
    flow.dispatcher.process(flow.snapshot())
    return handled_outcome(flow.queue, comment_id)


def to_build(flow: Flow) -> Job:
    assert flow.go("go-1") == "queued-build"
    return claim(flow, "claude", "build")


def to_review(flow: Flow) -> Job:
    build = to_build(flow)
    ready = filled(handoff_header(brief_for(build)), HEADS[1])
    flow.head = HEADS[1]
    assert post_header(flow, build, "ready-1", ready) == "queued-review"
    return claim(flow, "codex", "review")


def to_second_review(flow: Flow) -> Job:
    review = to_review(flow)
    revise = filled(handoff_header(brief_for(review)), flow.head, verdict="REVISE", ac="unmet")
    assert post_header(flow, review, "verdict-1", revise, REVISE_BODY) == "queued-review-fix"
    fix = claim(flow, "claude", "fix")
    flow.head = HEADS[2]
    assert post_header(flow, fix, "ready-2", filled(handoff_header(brief_for(fix)), HEADS[2])) == "queued-review"
    return claim(flow, "codex", "review")


REVISE_BODY = "1. [blocking] f-1. Rule: tests first. lib/x.py:1. Fails.\n"
NEW_FINDING = "1. [blocking] f-2. Rule: docs match code. lib/y.py:2. Fails.\n"


@pytest.mark.job_bound
def test_the_build_ready_is_accepted_in_building(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    build = to_build(flow)
    assert flow.state()["workflow_state"] == "building"
    flow.head = HEADS[1]
    header = filled(handoff_header(brief_for(build)), HEADS[1])
    assert post_header(flow, build, "ready-1", header) == "queued-review"


VERDICT_OUTCOMES = {
    ("REVISE", "unmet"): (NEW_FINDING, "queued-review-fix"),
    ("REVISE", "met"): (NEW_FINDING, "queued-review-fix"),
    ("APPROVE", "met"): ("No findings.\n", "merged"),
    ("BLOCKED", "met"): ("Need repo access.\n", "blocked-review"),
    ("BLOCKED", "unmet"): ("Need repo access.\n", "blocked-review"),
}


@pytest.mark.job_bound
@pytest.mark.parametrize("to_stage", [to_review, to_second_review], ids=["round-1", "round-2"])
def test_every_advertised_verdict_is_accepted_in_review(tmp_path: Path, to_stage: Callable[[Flow], Job]) -> None:
    template = handoff_header(brief_for(to_stage(Flow(tmp_path / "template"))))
    choices = [choice for choice in every_choice(template) if choice not in FORBIDDEN_BY_THE_RULES]
    assert {(choice["verdict"], choice["ac"]) for choice in choices} == set(VERDICT_OUTCOMES)
    for index, choice in enumerate(choices):
        flow = Flow(tmp_path / str(index))
        review = to_stage(flow)
        assert flow.state()["workflow_state"] == "review"
        body, outcome = VERDICT_OUTCOMES[(choice["verdict"], choice["ac"])]
        header = filled(handoff_header(brief_for(review)), flow.head, **choice)
        got = post_header(flow, review, "verdict-x", header, body)
        assert got.startswith(outcome), (got, choice)


@pytest.mark.job_bound
def test_the_fix_ready_is_accepted_in_fixing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    review = to_review(flow)
    revise = filled(handoff_header(brief_for(review)), flow.head, verdict="REVISE", ac="unmet")
    post_header(flow, review, "verdict-1", revise, "1. [blocking] f-1. Rule: r. x:1. Fails.\n")
    fix = claim(flow, "claude", "fix")
    assert flow.state()["workflow_state"] == "fixing"
    flow.head = HEADS[2]
    header = filled(handoff_header(brief_for(fix)), HEADS[2])
    assert post_header(flow, fix, "ready-2", header) == "queued-review"


PLAN_GO = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=planning -->\nGo.\n"


def plan_body(brief: str, text: str) -> str:
    assert "as `Estimate: <n>`" in brief, "the plan brief asks for no Estimate line"
    return f"Estimate: 40\n{text}"


def to_plan(flow: Flow) -> Job:
    assert flow.post("go-1", PLAN_GO, "grok-login") == "queued-plan"
    return claim(flow, "claude", "plan")


def to_second_plan(flow: Flow) -> Job:
    first = to_plan(flow)
    brief = brief_for(first)
    unmet = filled(handoff_header(brief), flow.head, ac="unmet")
    assert post_header(flow, first, "plan-1", unmet, plan_body(brief, "ac-1: unmapped.\n")) == "queued-plan-gap"
    return claim(flow, "claude", "plan")


PLAN_OUTCOMES = {
    (1, "met"): "queued-plan-build",
    (1, "unmet"): "queued-plan-gap",
    (2, "met"): "queued-plan-build",
    (2, "unmet"): "stuck-plan-gaps",
}


@pytest.mark.job_bound
@pytest.mark.parametrize(("to_stage", "plan_round"), [(to_plan, 1), (to_second_plan, 2)], ids=["round-1", "round-2"])
def test_every_advertised_plan_is_accepted_in_planning(
    tmp_path: Path, to_stage: Callable[[Flow], Job], plan_round: int
) -> None:
    template = handoff_header(brief_for(to_stage(Flow(tmp_path / "template"))))
    assert f"round={plan_round}/2" in template
    choices = every_choice(template)
    assert {choice["ac"] for choice in choices} == {"met", "unmet"}
    for index, choice in enumerate(choices):
        flow = Flow(tmp_path / str(index))
        plan = to_stage(flow)
        assert flow.state()["workflow_state"] == "planning"
        brief = brief_for(plan)
        header = filled(handoff_header(brief), flow.head, **choice)
        got = post_header(flow, plan, "plan-x", header, plan_body(brief, f"ac-1: {choice['ac']}.\n"))
        assert got == PLAN_OUTCOMES[(plan_round, choice["ac"])], (got, choice)


@pytest.mark.job_bound
@pytest.mark.parametrize("stage", ["build", "review"])
def test_the_questions_header_pauses_the_job_that_rendered_it(tmp_path: Path, stage: str) -> None:
    flow = Flow(tmp_path)
    job = to_build(flow) if stage == "build" else to_review(flow)
    brief = brief_for(job)
    questions = [header for header in headers(brief) if "kind=questions" in header]
    assert questions, "the brief renders no questions header"
    for header in questions:
        assert parse_comment(f"{header}\nQ.\n").as_dict()["kind"] == "questions"
    outcome = post_header(flow, job, "questions-1", questions[-1], "**Questions**\n- Keep the old flag?\n")
    assert outcome == "waiting-max"


def test_every_brief_kind_renders_only_headers_the_parser_accepts() -> None:
    for kind in CASES:
        for header in headers(render_snapshot(kind)):
            for choice in every_choice(header):
                comment = f"{filled(header, HEADS[0], **choice)}\nBody.\n"
                if choice in FORBIDDEN_BY_THE_RULES:
                    with pytest.raises(HandoffError):
                        parse_comment(comment)
                    continue
                fields = parse_comment(comment).as_dict()
                assert fields["from"] in ("claude-bot", "codex-bot"), header
                assert all(fields[key] == value for key, value in choice.items()), (header, choice)


def test_grok_go_is_accepted_on_a_new_ticket(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    decision = choose_route(request_for("examples/cleffy/factory.md"))
    assert decision.job == "build"
    assert flow.post("go-1", go_comment(decision, "none"), "grok-login") == "queued-build"


def test_grok_extra_round_go_is_accepted_at_the_cap(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()
    assert flow.post("go-2", go_for(tmp_path).body, MAX) == "queued-fix"


def test_grok_answers_are_accepted_at_waiting_max(tmp_path: Path) -> None:
    flow = to_questions(tmp_path)
    assert flow.state()["workflow_state"] == "waiting-max"
    assert flow.post("answers-1", answers_for((("Keep the old flag?", "No."),)), "grok-login") == "resumed-build"


@pytest.mark.job_bound
def test_a_broken_ready_header_fails_the_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        build_brief,
        "ready_handoff",
        lambda bot: (f"<!-- orch:v1 kind=ready from={bot} role=reviewer head=<pushed-head-sha> -->", "x"),
    )
    with pytest.raises(AssertionError):
        test_the_build_ready_is_accepted_in_building(tmp_path)


def test_a_broken_verdict_choice_fails_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    real = review_brief.handoff
    monkeypatch.setattr(
        review_brief, "handoff", lambda bot, facts: tuple(part.replace("|BLOCKED>", "|BROKEN>") for part in real(bot, facts))
    )
    with pytest.raises(HandoffError):
        test_every_brief_kind_renders_only_headers_the_parser_accepts()


@pytest.mark.job_bound
def test_a_broken_later_round_header_fails_the_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = review_brief.handoff

    def builder_after_round_1(bot: str, facts: dict) -> tuple[str, str]:
        header, example = real(bot, facts)
        return (header.replace("role=reviewer", "role=builder") if facts["round_number"] > 1 else header), example

    monkeypatch.setattr(review_brief, "handoff", builder_after_round_1)
    with pytest.raises(AssertionError, match="rejected"):
        test_every_advertised_verdict_is_accepted_in_review(tmp_path, to_second_review)



@pytest.mark.job_bound
def test_a_broken_second_plan_header_fails_the_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = plan_brief.handoff

    def reviewer_in_round_2(bot: str, facts: dict) -> tuple[str, str]:
        header, example = real(bot, facts)
        return (header.replace("role=builder", "role=reviewer") if facts["plan_round"] == 2 else header), example

    monkeypatch.setattr(plan_brief, "handoff", reviewer_in_round_2)
    test_every_advertised_plan_is_accepted_in_planning(tmp_path / "round-1", to_plan, 1)
    with pytest.raises(AssertionError, match="rejected"):
        test_every_advertised_plan_is_accepted_in_planning(tmp_path / "round-2", to_second_plan, 2)


def rendered(kinds: tuple[str, ...] = tuple(CASES), *, side: str = "both") -> list[str]:
    texts = []
    for kind in kinds:
        box, remote = render_snapshot(kind).split("===== remote (")
        texts.extend({"both": [box, remote], "box": [box], "remote": [remote]}[side])
    return texts


REVIEW_KINDS = tuple(kind for kind in CASES if CASES[kind][0] == "review")


def carried(rule_id: str, kinds: tuple[str, ...] | None = None, *, side: str = "both") -> None:
    phrase = brief_rules.RULE_PHRASES[rule_id]
    if kinds is None:
        kinds = REVIEW_KINDS if rule_id.startswith("review-") else tuple(CASES)
    for text in rendered(kinds, side=side):
        assert phrase in text, f"{rule_id} is missing from a brief"


def wording(rule_id: str) -> Callable[[], None]:
    return lambda: carried(rule_id)


def wording_gh() -> None:
    carried("rule-gh")
    for variable in APPROVAL_TOKEN_VARIABLES:
        assert f"-u {variable}" in GH


def wording_secrets() -> None:
    carried("rule-secrets")
    for name in ("credentials", "runner tokens", ".env", "auth.json", "FACTORY_WEBHOOK_"):
        assert name in brief_rules.SECRET_RULE


FOLLOW_UP_KINDS = ("review-followups",)


def wording_follow_up_rounds(rule_id: str) -> None:
    later = {"followup_from": 2, "round_number": 2, "auto_extra": 0, "block_on": "any-failure", "stop_on": "rounds"}
    first = {"followup_from": 2, "round_number": 1, "auto_extra": 0, "block_on": "any-failure", "stop_on": "rounds"}
    phrase = brief_rules.RULE_PHRASES[rule_id]
    assert phrase in brief_rules.review_rules(later)
    assert phrase not in brief_rules.review_rules(first)
    carried(rule_id, FOLLOW_UP_KINDS, side="box")


def in_a_fresh_flow(check: Callable[[Path], None]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        check(Path(tmp))


BUILDER_VERDICT = "<!-- orch:v1 kind=verdict from=claude-bot role=reviewer round=1/3 head={head} verdict=APPROVE ac=met -->\nNo findings.\n"


def refuses_posts_outside_the_job(tmp: Path) -> None:
    flow = Flow(tmp)
    go = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->\nGo.\n"
    assert flow.post("go-forged", go, "claude-login") == "rejected-go-author"
    flow = Flow(tmp / "answers")
    to_build(flow)
    answers = answers_for((("Keep the old flag?", "No."),)).replace("from=grok-bot", "from=claude-bot")
    assert flow.post("answers-forged", answers, "claude-login") == "rejected-answers-author"
    assert flow.post("verdict-forged", BUILDER_VERDICT.format(head=flow.head), "claude-login") == "rejected-approve-author"
    assert flow.state()["workflow_state"] == "building"


def refuses_a_verdict_from_the_builder(tmp: Path) -> None:
    flow = Flow(tmp)
    to_review(flow)
    assert flow.post("verdict-builder", BUILDER_VERDICT.format(head=flow.head), "claude-login") == "rejected-approve-author"
    assert flow.state()["workflow_state"] == "review"


def refuses_an_agent_go_at_the_cap(tmp: Path) -> None:
    flow = AppFlow(tmp, enabled=True)
    flow.to_cap()
    assert flow.post("go-agent", go_for(tmp).body, "grok-login") == "go-not-from-approver"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)


def refuses_a_fourth_high_risk_round(tmp: Path) -> None:
    flow = Flow(tmp)
    flow.to_cap()
    fourth = APPROVE.format(round="4/3", head=flow.head)
    assert flow.post("verdict-fourth", fourth, "codex-login") == "rejected-over-cap"
    assert (flow.state()["workflow_state"], flow.state()["review_round"]) == ("capped", 3)


def refuses_approve_with_unmet_criteria() -> None:
    only_follow_ups = "1. [follow-up] f-4. Rule: tests first. lib/x.py:9. Fails.\n2. [minor] f-5. Rule: names. lib/x.py:3. Odd.\n"
    assert blocking_ids(only_follow_ups) == frozenset()
    approve = f"<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round=3/3 head={HEADS[0]} verdict=APPROVE"
    assert parse_comment(f"{approve} ac=met -->\n{only_follow_ups}").as_dict()["verdict"] == "APPROVE"
    with pytest.raises(HandoffError):
        parse_comment(f"{approve} ac=unmet -->\n{only_follow_ups}")


def refused(rule_id: str, check: Callable[[Path], None]) -> Callable[[], None]:
    return lambda: (carried(rule_id), in_a_fresh_flow(check))


USER_IMPACT_KINDS = ("review-user-impact", "review-user-impact-round-2")
ANY_FAILURE_KINDS = ("review-evidence", "review-round-2-evidence")
USER_IMPACT_MD = FACTORY_MD.replace("review:\n", "review:\n  block_on: user-impact\n")


def wording_user_impact(rule_id: str) -> None:
    phrase = brief_rules.RULE_PHRASES[rule_id]
    carried(rule_id, USER_IMPACT_KINDS, side="box")
    for text in rendered(ANY_FAILURE_KINDS, side="box"):
        assert phrase not in text, f"{rule_id} is in a block_on: any-failure brief"


def brief_under(factory_md: str, kind: str) -> str:
    context, findings = parse_factory_context(factory_md)
    assert context is not None, findings
    action, recipient, attempt, head, extra = CASES[kind]
    job = _job(action, recipient, attempt, head, extra)
    return render_brief(job, bot=f"{recipient}-bot", context=context, factory_root=str(ROOT))


def rules_section(text: str) -> str:
    return text.split("\n## Rules\n", 1)[1].split("\n## ", 1)[0]


BUILDER_KINDS = ("plan", "build", "fix", "ci-fix", "merge-only", "rebase")


def wording_no_follow_up_fixes() -> None:
    phrase = brief_rules.RULE_PHRASES["rule-no-follow-up-fixes"]
    for text in rendered(FOLLOW_UP_KINDS + USER_IMPACT_KINDS, side="box"):
        assert text.count(phrase) == 1
    for kind in BUILDER_KINDS:
        assert f"- {phrase}" in rules_section(brief_under(USER_IMPACT_MD, kind)), kind
        assert phrase not in brief_under(FACTORY_MD, kind), kind
    for text in rendered(("review", *ANY_FAILURE_KINDS), side="box"):
        assert phrase not in text


ENFORCED: dict[str, Callable[[], None]] = {
    "rule-post-only": refused("rule-post-only", refuses_posts_outside_the_job),
    "review-one-reviewer": refused("review-one-reviewer", refuses_a_verdict_from_the_builder),
    "review-agent-approval": refused("review-agent-approval", refuses_an_agent_go_at_the_cap),
    "review-high-risk-rounds": refused("review-high-risk-rounds", refuses_a_fourth_high_risk_round),
    "review-follow-ups-approve": lambda: (
        wording_follow_up_rounds("review-follow-ups-approve"),
        refuses_approve_with_unmet_criteria(),
    ),
}

GUIDANCE: dict[str, Callable[[], None]] = {
    "finish-done": lambda: carried("finish-done", side="box"),
    "finish-failed": wording("finish-failed"),
    "finish-proof": lambda: carried("finish-proof", side="box"),
    "finish-remote-handoff": lambda: carried("finish-remote-handoff", side="remote"),
    "finish-remote-no-db": lambda: carried("finish-remote-no-db", side="remote"),
    "finish-remote-waiter": lambda: carried("finish-remote-waiter", side="remote"),
    "finish-no-heartbeat": wording("finish-no-heartbeat"),
    "review-blocking-ran": wording("review-blocking-ran"),
    "review-no-judge": wording("review-no-judge"),
    "review-low-risk-rounds": wording("review-low-risk-rounds"),
    "review-gate-retries": wording("review-gate-retries"),
    "review-follow-ups": lambda: wording_follow_up_rounds("review-follow-ups"),
    "review-user-impact-bar": lambda: wording_user_impact("review-user-impact-bar"),
    "review-user-impact-approve": lambda: wording_user_impact("review-user-impact-approve"),
    "review-ship-pinned": wording("review-ship-pinned"),
    "review-ac-met": wording("review-ac-met"),
    "review-ac-unmet": wording("review-ac-unmet"),
    "review-approve": wording("review-approve"),
    "review-revise": wording("review-revise"),
    "review-blocked": wording("review-blocked"),
    "rule-gh": wording_gh,
    "rule-secrets": wording_secrets,
    "rule-scratch": lambda: carried("rule-scratch", side="box"),
    "rule-containers": wording("rule-containers"),
    "rule-load": wording("rule-load"),
    "rule-suite-slot": wording("rule-suite-slot"),
    "rule-no-follow-up-fixes": wording_no_follow_up_fixes,
    "rule-job-text": wording("rule-job-text"),
}

CONTRACT = {**ENFORCED, **GUIDANCE}


def test_every_rule_id_is_enforced_or_guidance() -> None:
    assert set(ENFORCED).isdisjoint(GUIDANCE)
    assert set(CONTRACT) == set(brief_rules.RULE_PHRASES)


def test_every_rule_line_in_a_brief_has_an_id() -> None:
    phrases = brief_rules.RULE_PHRASES.values()
    for text in [*rendered(), brief_under(USER_IMPACT_MD, "build")]:
        for heading in ("## Finish the job", "## Review rules", "## Rules"):
            if heading == "## Review rules" and heading not in text:
                continue
            section = text[text.index(heading) :]
            end = section.find("\n## ", 1)
            for line in (section if end < 0 else section[:end]).splitlines()[1:]:
                if line.startswith("- "):
                    assert any(phrase in line for phrase in phrases), f"rule without an id: {line}"


FAKE_REGISTRATION = {"review-agent-approval", "review-high-risk-rounds"}


@pytest.mark.parametrize(
    "rule_id",
    [rule_id if rule_id in FAKE_REGISTRATION else pytest.param(rule_id, marks=pytest.mark.job_bound) for rule_id in sorted(CONTRACT)],
)
def test_each_rule_is_carried_and_enforced_rules_refuse_a_breaking_handoff(rule_id: str) -> None:
    CONTRACT[rule_id]()


def test_a_broken_rule_fails_its_contract_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brief_rules, "SECRET_RULE", "Never read credentials.")
    with pytest.raises(AssertionError):
        CONTRACT["rule-secrets"]()
    monkeypatch.setattr(brief_rules, "RULE_PHRASES", {**brief_rules.RULE_PHRASES, "rule-new": "x"})
    with pytest.raises(AssertionError):
        test_every_rule_id_is_enforced_or_guidance()


BOX_ROOT = "/srv/orch/factory"
ABSOLUTE = re.compile(r"`(/[^`\s]+)`")
FENCED = re.compile(r"^(`{3,})[^\n]*\n.*?^\1`*[ \t]*$", re.M | re.S)
NAMED = re.compile(r"`([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+\.(?:md|py|yml|lock))(?::[0-9]+)?`")


def pinned_paths() -> set[str]:
    pin = load_pin(ROOT)
    return {
        *(f"{PSTACK_DIR.as_posix()}/{rel}" for rel in pin.hashes()),
        *(f"{FACTORY_DIR.as_posix()}/{rel}" for rel in pin.factory_hashes()),
        *(f"{KIT_DIR.as_posix()}/{rel}" for rel in pin.kit_hashes()),
    }


def assert_pinned_under(text: str, root: str) -> list[str]:
    pinned = pinned_paths()
    named = ABSOLUTE.findall(FENCED.sub("", text))
    for path in named:
        assert path.startswith(f"{root}/"), f"{path} is outside the deployment root {root}"
        rel = path.removeprefix(f"{root}/")
        assert rel in pinned, f"{path} is not in skills/skills.lock"
        assert (ROOT / rel).is_file(), f"{path} is pinned but missing"
    return named


@pytest.mark.parametrize("kind", sorted(CASES))
def test_every_absolute_path_a_brief_names_is_pinned_and_present(kind: str) -> None:
    box, remote = rendered((kind,))
    remote_root = f"/home/{CASES[kind][1]}-bot/factory"
    named = assert_pinned_under(box, BOX_ROOT) + assert_pinned_under(remote, remote_root)
    assert named or kind in ("merge-only", "rebase")


def test_the_pin_check_skips_quoted_input_but_not_the_brief_text() -> None:
    pinned = f"{BOX_ROOT}/{sorted(pinned_paths())[0]}"
    quoted = quoted_section("Plan", "Link `/signup?plan=free`.\n````\n`/etc/x`\n````", None, 10_000)
    assert assert_pinned_under(f"Read `{pinned}`.\n{quoted}\n", BOX_ROOT) == [pinned]
    with pytest.raises(AssertionError, match="outside the deployment root"):
        assert_pinned_under(f"{quoted}\nRead `/signup?plan=free`.\n", BOX_ROOT)


def test_a_missing_remote_file_fails_the_pin_check(monkeypatch: pytest.MonkeyPatch) -> None:
    real = brief_skills.Pinned.under

    def broken(self: brief_skills.Pinned, root: str) -> str:
        path = real(self, root)
        return path.replace("/harness/codex.md", "/harness/does-not-exist.md") if root.startswith("/home/codex-bot/") else path

    monkeypatch.setattr(brief_skills.Pinned, "under", broken)
    test_every_absolute_path_a_brief_names_is_pinned_and_present("build")
    with pytest.raises(AssertionError, match="does-not-exist"):
        test_every_absolute_path_a_brief_names_is_pinned_and_present("review")


def promised_files(brief_text: object) -> set[str]:
    plan = QaPlan.parse(brief_text if isinstance(brief_text, str) else None)
    new = [journey.id for journey in plan.journeys if journey.new] if plan else []
    return {f"features/{journey}.md" for journey in new} | ({"features/README.md"} if new else set())


def assert_named_files_exist(kind: str, text: str, promised: set[str]) -> None:
    roots = (ROOT, ROOT / PSTACK_DIR, ROOT / FACTORY_DIR, ROOT / KIT_DIR)
    for rel in NAMED.findall(FENCED.sub("", text)):
        if rel.startswith(".claude/") or rel in promised:
            continue
        assert any((root / rel).is_file() for root in roots), f"{kind} names missing {rel}"


@pytest.mark.parametrize("kind", sorted(CASES))
def test_every_relative_file_a_brief_names_exists(kind: str) -> None:
    promised = promised_files(CASES[kind][4].get("brief_text"))
    for text in rendered((kind,)):
        assert_named_files_exist(kind, text, promised)


def test_the_file_check_skips_quoted_input_and_promised_journey_files_only() -> None:
    promised = promised_files(CASES["plan-qa"][4]["brief_text"])
    assert promised == {"features/start-free.md", "features/README.md"}
    quoted = quoted_section("Ticket", "Edit `lib/gone.py`.", None, 10_000)
    assert_named_files_exist("x", f"Add `features/start-free.md`.\n{quoted}\n", promised)
    with pytest.raises(AssertionError, match="names missing features/other.md"):
        assert_named_files_exist("x", "Add `features/other.md`.", promised)
    with pytest.raises(AssertionError, match="names missing lib/gone.py"):
        assert_named_files_exist("x", f"{quoted}\nEdit `lib/gone.py`.", promised)


REVIEW_FACTS = {"round": "2/3", "cap": 3, "head": HEADS[0], "risk": "low", "followup_from": None, "round_number": 2, "block_on": "any-failure"}


def template(kind: str) -> str:
    example = review_brief.handoff("codex-bot", REVIEW_FACTS)[1]
    forms = re.findall(r"`(1\. \[[a-z-]+\] f-<n>\.[^`]*)`", example)
    return next(form for form in forms if f"[{kind}]" in form)


def test_the_rendered_blocking_form_is_read_as_a_blocking_id() -> None:
    item = template("blocking").replace("<n>", "3")
    for placeholder in PLACEHOLDER.findall(item):
        item = item.replace(placeholder, "x")
    assert blocking_ids(item) == {"f-3"}


def test_the_rendered_criterion_line_is_read_as_a_blocking_id() -> None:
    example = review_brief.handoff("codex-bot", REVIEW_FACTS)[1]
    assert "each criterion by `ac-<n>` with its state" in example
    assert blocking_ids("- ac-2: unmet. The CLI still exits 0.\n") == {"ac-2"}
    assert blocking_ids("- ac-2: met. Passes.\n") == frozenset()


def test_the_rendered_closed_form_closes_the_finding() -> None:
    blocking = template("blocking").replace("<n>", "1").replace("<the rule it breaks>", "tests first")
    closed = template("closed").replace("<n>", "1")
    verdict = "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round={r} head={h} verdict=REVISE -->\n"
    comments = [
        Comment(id=f"v{r}", author="codex-login", body=verdict.format(r=f"{r}/3", h=HEADS[0]) + body, created_at=float(r))
        for r, body in ((1, blocking + "\n"), (2, closed + "\n"))
    ]
    trusted = {"github": {"codex-login": frozenset({"codex-bot"})}}
    open_after_1 = findings_evidence(comments=comments[:1], trusted_logins=trusted, reviewers={"codex-bot"}, current_round=2, bases={})
    assert [(item.id, item.rule) for item in open_after_1.findings] == [("f-1", "tests first")]
    open_after_2 = findings_evidence(comments=comments, trusted_logins=trusted, reviewers={"codex-bot"}, current_round=3, bases={})
    assert open_after_2.findings == ()
    assert blocking_ids(closed) == frozenset()


def test_a_broken_id_form_fails_the_match(monkeypatch: pytest.MonkeyPatch) -> None:
    real = review_brief.handoff

    def broken(bot: str, facts: dict) -> tuple[str, str]:
        header, example = real(bot, facts)
        return header, example.replace("f-<n>", "F<n>")

    monkeypatch.setattr(review_brief, "handoff", broken)
    with pytest.raises(StopIteration):
        test_the_rendered_blocking_form_is_read_as_a_blocking_id()
