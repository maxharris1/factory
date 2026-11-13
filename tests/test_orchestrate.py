import ast
import re
from dataclasses import fields, replace
from pathlib import Path

import pytest
import yaml

from lib.factory_md import EXAMPLE_PATHS, FactoryConfig
from lib.handoff import labels_for, parse_comment, parse_header_line
from lib.orchestrate import (
    RECOMMENDED_NEXT,
    STOPS,
    ClassifiedQuestion,
    FitChoice,
    GoBlocked,
    QuestionError,
    RouteDecision,
    RouteError,
    RouteRequest,
    StopError,
    StopLedger,
    StopReport,
    TicketFlow,
    answers_comment,
    assert_cross_family,
    brief_comment,
    choose_route,
    go_comment,
    job_for_file_count,
    read_factory,
    route_from_fit,
    stuck_comment,
)
from lib.schema import AUT0_REPO
from lib.usage import StaticUsage, UsageReport

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "grok" / "orchestrate-project" / "SKILL.md"
PROJECTS = tuple(
    relative for relative in EXAMPLE_PATHS if relative.endswith("/factory.md")
)


def reports(*items: UsageReport) -> StaticUsage:
    return StaticUsage({item.harness: item for item in items})


def write_factory(directory: Path, data: dict, body: str = "") -> Path:
    path = directory / "factory.md"
    raw = yaml.safe_dump(data, sort_keys=False)
    path.write_text(f"---\n{raw}---\n{body}", encoding="utf-8")
    return path


def request_for(
    relative: str,
    *,
    risk: str = "low",
    urgent: bool = False,
    summary: str = "Update the docs.",
    likely_files: tuple[str, ...] = ("README.md",),
    scope_changed: bool = False,
) -> RouteRequest:
    result = read_factory(ROOT / relative)
    assert result.project is not None and result.stop is None
    return RouteRequest(
        config=result.project.config,
        risk=risk,
        urgent=urgent,
        summary=summary,
        likely_files=likely_files,
        scope_changed=scope_changed,
    )


def test_read_factory_accepts_examples_and_their_prose() -> None:
    assert len(PROJECTS) == 3
    for relative in PROJECTS:
        result = read_factory(ROOT / relative)
        assert result.stop is None
        assert result.project is not None
        assert result.project.warnings == ()
        assert result.project.review
        assert result.project.ship_notes
        assert result.project.context
    aut0 = read_factory(ROOT / "examples/aut0/factory.md").project
    assert aut0 is not None
    assert aut0.config.tickets == "linear"
    assert aut0.config.ship == "handoff"


def test_invalid_factory_md_is_refused_once(tmp_path: Path) -> None:
    path = write_factory(tmp_path, {"base_branch": "main"})
    result = read_factory(path)
    assert result.project is None
    assert result.stop is not None
    assert result.stop.stop == "refused"
    assert "missing required field" in result.stop.evidence
    assert result.stop.next_step == RECOMMENDED_NEXT["refused"]
    ledger = StopLedger()
    report = ledger.report("owner/repo#1", result.stop)
    assert "**Evidence**" in report
    assert "**Next step**" in report
    assert result.stop.evidence in report
    with pytest.raises(StopError, match="already reported"):
        ledger.report("owner/repo#1", result.stop)
    blocked = StopReport("blocked", "unparsed header", RECOMMENDED_NEXT["blocked"])
    assert ledger.report("owner/repo#1", blocked)


def test_unknown_model_warns_and_does_not_refuse(tmp_path: Path) -> None:
    path = write_factory(
        tmp_path,
        {
            "base_branch": "main",
            "tickets": "github",
            "builder": {"harness": "claude", "model": "mystery-x"},
            "reviewer": {"harness": "codex"},
            "review": {"rounds": {"low": 1, "high": 3}, "verify": "none"},
            "ship": "merge",
            "skills": [],
        },
    )
    result = read_factory(path)
    assert result.stop is None
    assert result.project is not None
    assert result.project.warnings
    assert "mystery-x" in result.project.warnings[0]


def test_read_factory_opens_only_factory_md(tmp_path: Path) -> None:
    for name in (".env", "auth.json", ".runner", "token"):
        path = tmp_path / name
        with pytest.raises(Exception, match="factory.md"):
            read_factory(path)
        assert not path.exists()


def test_brief_has_the_five_parts_and_round_trips() -> None:
    text = brief_comment(
        risk="high",
        goal="Ship the skill.",
        acceptance_criteria=("The brief names the risk.", "CI is green."),
        likely_files=("grok/orchestrate-project/SKILL.md", "lib/orchestrate.py"),
        done_evidence="pytest passes",
    )
    parsed = parse_comment(text)
    assert parsed.as_dict() == {
        "kind": "brief",
        "from": "grok-bot",
        "role": "orchestrator",
        "risk": "high",
    }
    for heading in (
        "**Goal**",
        "**Acceptance criteria**",
        "**Likely files**",
        "**Risk**",
        "**Done-evidence**",
    ):
        assert heading in parsed.body
    assert "\nhigh\n" in parsed.body
    assert labels_for(parsed, tickets="github", repo="maxharris1/Cleffy") == (
        "orch:risk/high",
    )
    assert labels_for(parsed, tickets="github", repo=AUT0_REPO) == ()
    assert labels_for(parsed, tickets="linear", repo=AUT0_REPO) == ()
    with pytest.raises(Exception, match="low or high"):
        brief_comment(
            risk="medium",
            goal="x",
            acceptance_criteria=("y",),
            likely_files=(),
            done_evidence="z",
        )


def test_questions_step_classifies_and_records_defaults() -> None:
    flow = TicketFlow()
    text = flow.post_questions(
        "Max wants the skill on the spec path.",
        (
            ClassifiedQuestion("Which files does the brief list?", "observable"),
            ClassifiedQuestion(
                "Keep the round cap.",
                "default-able",
                default="low 1, high 3",
            ),
            ClassifiedQuestion("Should this ship to production?", "product"),
        ),
    )
    parsed = parse_comment(text)
    assert parsed.as_dict()["kind"] == "questions"
    assert parsed.as_dict()["state"] == "waiting-max"
    assert "[observable] Which files does the brief list?" in parsed.body
    assert "[default-able] Keep the round cap." in parsed.body
    assert "Default: low 1, high 3" in parsed.body
    assert "[product] Should this ship to production?" in parsed.body
    assert labels_for(parsed, tickets="linear", repo=AUT0_REPO) == ()
    with pytest.raises(QuestionError, match="recorded default"):
        ClassifiedQuestion("Pick a timeout.", "default-able")
    with pytest.raises(QuestionError, match="observable, default-able, or product"):
        ClassifiedQuestion("Pick a timeout.", "technical")
    with pytest.raises(QuestionError, match="questions step already ran"):
        flow.post_questions("Again.", ())


def test_product_questions_are_answered_one_at_a_time_before_go() -> None:
    request = request_for("examples/cleffy/factory.md")
    decision = choose_route(request)
    flow = TicketFlow()
    with pytest.raises(GoBlocked, match="questions step has not run"):
        flow.post_go(decision, "none")
    flow.post_questions(
        "Two product calls.",
        (
            ClassifiedQuestion("Ship to production?", "product"),
            ClassifiedQuestion("Which color?", "product"),
            ClassifiedQuestion("Use the existing helper.", "default-able", default="yes"),
        ),
    )
    with pytest.raises(GoBlocked, match="product questions are open"):
        flow.post_go(decision, "none")
    with pytest.raises(QuestionError, match="open product question first"):
        flow.post_answer("Which color?", "blue")
    first = parse_comment(flow.post_answer("Ship to production?", "No."))
    assert first.as_dict()["kind"] == "answers"
    assert "**Answer**\nNo." in first.body
    with pytest.raises(GoBlocked, match="product questions are open"):
        flow.post_go(decision, "none")
    flow.post_answer("Which color?", "blue")
    go = parse_comment(flow.post_go(decision, request.config.verify))
    assert go.as_dict()["kind"] == "go"
    assert go.as_dict()["builder"] == "claude"
    assert go.as_dict()["state"] == "building"
    assert go.as_dict()["risk"] == "low"
    with pytest.raises(GoBlocked, match="already posted"):
        flow.post_go(decision, "none")


def test_go_is_allowed_when_no_product_question_is_open() -> None:
    request = request_for("examples/cleffy/factory.md")
    decision = choose_route(request)
    flow = TicketFlow()
    text = flow.post_questions(
        "Nothing for Max to decide.",
        (
            ClassifiedQuestion("The empty state is visible.", "observable"),
            ClassifiedQuestion("Keep the helper.", "default-able", default="keep it"),
        ),
    )
    parsed = parse_comment(text)
    assert "state" not in parsed.as_dict()
    assert parsed.body.endswith("**Questions**\n- [observable] The empty state is visible.\n- [default-able] Keep the helper.\n  Default: keep it\n")
    go = parse_comment(flow.post_go(decision, "none"))
    assert go.as_dict()["kind"] == "go"
    empty = TicketFlow()
    none = parse_comment(empty.post_questions("Restated.", ()))
    assert "**Questions**\nNone." in none.body
    assert "state" not in none.as_dict()
    assert parse_comment(empty.post_go(decision, "none")).as_dict()["kind"] == "go"


def test_choose_route_uses_route_job_and_keeps_a_cross_family_reviewer() -> None:
    for relative in PROJECTS:
        request = request_for(relative)
        decision = choose_route(request)
        assert decision.builder_harness == request.config.builder_harness
        assert decision.builder_model == request.config.builder_model
        assert decision.reviewer_harness == request.config.reviewer_harness
        assert decision.reviewer_model == request.config.reviewer_model
        assert decision.builder_family != decision.reviewer_family
        assert decision.job == "build"
        assert decision.file_count == 1
        assert decision.urgent is False
    urgent = choose_route(request_for("examples/tracy/factory.md", urgent=True))
    plain = choose_route(request_for("examples/tracy/factory.md"))
    assert urgent.builder_harness == plain.builder_harness
    assert urgent.reviewer_harness == plain.reviewer_harness
    assert urgent.urgent is True


def test_choose_route_builds_fit_from_the_route_job_result() -> None:
    base = request_for("examples/cleffy/factory.md", likely_files=("a", "b", "c", "d"))
    request = replace(
        base,
        config=replace(
            base.config,
            reviewer_harness="cursor",
            reviewer_model="gpt-5",
        ),
    )
    source = reports(UsageReport("cursor", 0.0, True))
    decision = choose_route(request, usage=source)
    assert decision.builder_harness == "claude"
    assert decision.builder_model is None
    assert decision.reviewer_harness == "codex"
    assert decision.reviewer_model is None
    assert decision.builder_family == "anthropic"
    assert decision.reviewer_family == "openai"
    assert decision.job == "plan"
    assert decision.file_count == 4
    assert decision.reviewer_harness != request.config.reviewer_harness
    urgent = choose_route(replace(request, urgent=True), usage=source)
    assert urgent.reviewer_harness == "cursor"
    assert urgent.reviewer_model == "gpt-5"
    assert urgent.urgent is True


def test_choose_route_raises_when_the_route_is_not_ok() -> None:
    request = request_for("examples/cleffy/factory.md")
    source = reports(
        UsageReport("claude", 0.0, True),
        UsageReport("codex", 0.0, True),
        UsageReport("cursor", 0.0, True),
    )
    with pytest.raises(RouteError, match="^reserve$"):
        choose_route(request, usage=source)
    decision = choose_route(replace(request, urgent=True), usage=source)
    assert decision.builder_harness == "claude"
    assert decision.reviewer_harness == "codex"
    assert decision.builder_family != decision.reviewer_family


def test_same_family_reviewer_is_refused() -> None:
    request = request_for("examples/cleffy/factory.md")
    with pytest.raises(RouteError, match="same-family"):
        route_from_fit(request, FitChoice("claude", None, "claude", None))
    with pytest.raises(RouteError, match="not established"):
        route_from_fit(request, FitChoice("cursor", None, "codex", None))
    with pytest.raises(RouteError, match="contradicts"):
        route_from_fit(request, FitChoice("claude", "gpt-5", "codex", None))
    for changes, message in (
        (
            {"builder_harness": "codex", "reviewer_harness": "codex"},
            "^same-family reviewer: builder family 'openai' and "
            "reviewer family 'openai'$",
        ),
        (
            {"builder_harness": "cursor", "builder_model": None},
            "^same-family reviewer: builder family not established",
        ),
        (
            {"builder_harness": "claude", "builder_model": "gpt-5"},
            "^builder model hint 'gpt-5' contradicts claude harness$",
        ),
    ):
        config = replace(request.config, **changes)
        with pytest.raises(RouteError, match=message):
            choose_route(replace(request, config=config))
    families = assert_cross_family("cursor", "claude-opus", "codex", None)
    assert families == ("anthropic", "openai")


def test_choose_route_records_reserve_and_fallback() -> None:
    base = request_for("examples/cleffy/factory.md")
    request = replace(
        base,
        config=replace(base.config, reviewer_harness="cursor", reviewer_model="gpt-5"),
    )
    source = reports(UsageReport("cursor", 0.0, True))
    fallback = choose_route(request, usage=source)
    assert (
        fallback.builder_using_reserve,
        fallback.reviewer_using_reserve,
        fallback.builder_fallback,
        fallback.reviewer_fallback,
    ) == (False, False, False, True)
    body = parse_comment(_go_after_questions(fallback, "none")).body
    assert body.endswith(
        "**Routing**\n"
        "Reviewer is a fallback from factory.md's reviewer.\n"
        "For information; the dispatcher runs factory.md's pair.\n"
    )
    reserve = choose_route(replace(request, urgent=True), usage=source)
    assert (
        reserve.builder_using_reserve,
        reserve.reviewer_using_reserve,
        reserve.builder_fallback,
        reserve.reviewer_fallback,
    ) == (False, True, False, False)
    body = parse_comment(_go_after_questions(reserve, "none")).body
    assert "**Routing**\nReviewer is using its usage reserve.\n" in body
    builder_side = choose_route(
        replace(
            base,
            config=replace(
                base.config,
                builder_harness="cursor",
                builder_model="gpt-5",
                reviewer_harness="claude",
            ),
        ),
        usage=source,
    )
    assert (builder_side.builder_harness, builder_side.reviewer_harness) == (
        "claude",
        "codex",
    )
    assert (builder_side.builder_fallback, builder_side.reviewer_fallback) == (True, True)
    body = parse_comment(_go_after_questions(builder_side, "none")).body
    assert body.endswith(
        "**Routing**\n"
        "Builder is a fallback from factory.md's builder.\n"
        "Reviewer is a fallback from factory.md's reviewer.\n"
        "For information; the dispatcher runs factory.md's pair.\n"
    )
    plain = choose_route(base)
    assert (
        plain.builder_using_reserve,
        plain.reviewer_using_reserve,
        plain.builder_fallback,
        plain.reviewer_fallback,
    ) == (False, False, False, False)
    assert "**Routing**" not in parse_comment(_go_after_questions(plain, "none")).body


def test_choose_route_takes_the_builder_that_built() -> None:
    request = replace(
        request_for("examples/cleffy/factory.md"), built_by=("cursor", "gpt-5")
    )
    decision = choose_route(request)
    assert decision.built_by == ("cursor", "gpt-5")
    assert (decision.builder_harness, decision.builder_model) == ("cursor", "gpt-5")
    assert decision.builder_family == "openai"
    assert decision.reviewer_harness == "claude"
    assert decision.reviewer_family == "anthropic"
    assert choose_route(request_for("examples/cleffy/factory.md")).built_by is None


def test_go_comment_rechecks_family_and_job() -> None:
    decision = choose_route(request_for("examples/cleffy/factory.md"))
    assert parse_comment(go_comment(decision, "none")).as_dict()["kind"] == "go"
    for changes, message in (
        (
            {"reviewer_harness": "claude", "reviewer_family": "anthropic"},
            "same-family reviewer",
        ),
        ({"builder_family": "openai"}, "builder family"),
        ({"reviewer_harness": "grok"}, "reviewer harness"),
        ({"file_count": 4}, "job 'build' does not match"),
        ({"scope_changed": True}, "job 'build' does not match"),
    ):
        with pytest.raises(RouteError, match=message):
            go_comment(replace(decision, **changes), "none")


def test_route_request_refuses_a_string_for_likely_files() -> None:
    base = request_for("examples/cleffy/factory.md")
    with pytest.raises(RouteError, match="likely_files"):
        RouteRequest(
            config=base.config,
            risk="low",
            urgent=False,
            summary="Docs.",
            likely_files="README.md",  # type: ignore[arg-type]
        )


def test_multi_line_answer_round_trips() -> None:
    flow = TicketFlow()
    flow.post_questions("One call.", (ClassifiedQuestion("Ship it?", "product"),))
    text = flow.post_answer("Ship it?", "  Yes, on Friday.\nNot before QA signs off.  ")
    parsed = parse_comment(text)
    assert parsed.as_dict() == {"kind": "answers", "from": "grok-bot", "role": "orchestrator"}
    assert parsed.body == (
        "**Question**\nShip it?\n\n"
        "**Answer**\nYes, on Friday.\nNot before QA signs off.\n"
    )
    assert flow.questions[0].answer == "Yes, on Friday.\nNot before QA signs off."
    with pytest.raises(QuestionError, match="question text"):
        ClassifiedQuestion("Ship\nit?", "product")
    with pytest.raises(QuestionError, match="answer"):
        answers_comment("Ship it?", "  \n ")
    with pytest.raises(QuestionError, match="question text"):
        answers_comment("Ship\nit?", "Yes.")


def test_plan_starts_over_three_files_or_when_scope_changes() -> None:
    assert job_for_file_count(3) == "build"
    assert job_for_file_count(4) == "plan"
    assert job_for_file_count(1, scope_changed=True) == "plan"
    request = request_for(
        "examples/aut0/factory.md",
        likely_files=("a", "b", "c", "d"),
        scope_changed=False,
    )
    decision = choose_route(request)
    go = parse_comment(_go_after_questions(decision, request.config.verify))
    assert go.as_dict()["state"] == "planning"
    assert go.as_dict()["builder"] == "claude"
    assert go.body == (
        "**Job**\nplan\n\n"
        "**Builder**\nclaude\n\n"
        "**Builder model**\nnone\n\n"
        "**Reviewer**\ncodex\n\n"
        "**Reviewer model**\nnone\n\n"
        "**Verify**\nverify-aut0\n\n"
        "**Files**\n4\n"
    )
    assert labels_for(go, tickets="github", repo="maxharris1/Cleffy") == (
        "orch:state/planning",
        "orch:risk/low",
        "orch:builder/claude",
    )
    assert labels_for(go, tickets="linear", repo=AUT0_REPO) == ()
    assert labels_for(go, tickets="github", repo=AUT0_REPO) == ()


def _go_after_questions(decision: RouteDecision, verify: str) -> str:
    flow = TicketFlow()
    flow.post_questions("Restated.", ())
    return flow.post_go(decision, verify)


def test_each_stop_is_one_report_with_evidence_and_a_next_step() -> None:
    assert tuple(RECOMMENDED_NEXT) == STOPS
    ledger = StopLedger()
    for stop in STOPS:
        report = StopReport(stop, f"evidence for {stop}", RECOMMENDED_NEXT[stop])
        text = ledger.report("issue-7", report)
        assert f"**Stop**\n{stop}\n" in text
        assert f"**Evidence**\nevidence for {stop}\n" in text
        assert f"**Next step**\n{RECOMMENDED_NEXT[stop]}\n" in text
        with pytest.raises(StopError, match="already reported"):
            ledger.report("issue-7", report)
    stuck = StopReport("stuck", "no heartbeat", RECOMMENDED_NEXT["stuck"])
    parsed = parse_comment(stuck_comment(stuck))
    assert parsed.as_dict() == {
        "kind": "stuck",
        "from": "grok-bot",
        "role": "orchestrator",
        "state": "stuck",
    }
    assert "**Evidence**\nno heartbeat\n" in parsed.body
    with pytest.raises(StopError, match="only for stop stuck"):
        stuck_comment(StopReport("blocked", "no", RECOMMENDED_NEXT["blocked"]))
    with pytest.raises(StopError, match="unknown stop"):
        StopReport("paused", "evidence", "wait")


def test_skill_matches_the_spec_path_and_the_interface() -> None:
    spec = (ROOT / "docs/SPEC.md").read_text(encoding="utf-8")
    assert "grok/orchestrate-project/SKILL.md" in spec
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "factory.md",
        "**Goal**",
        "**Acceptance criteria**",
        "**Likely files**",
        "**Risk**",
        "**Done-evidence**",
        "observable",
        "default-able",
        "product",
        "one at a time",
        "orch:go",
        "cross-family",
        "evidence",
        "recommended next step",
        "base branch",
        "claude",
        "codex",
        "cursor-agent",
        ".env",
        "auth.json",
        ".runner",
        "GH_TOKEN",
        "lib.usage.route_job(config, *, usage=None, resumed_paused_step=False, past_ship=False, marked_urgent=False, built_by=None) -> JobRoute",
        "route_job(config, marked_urgent=urgent)",
    ):
        assert phrase in text
    assert "choose_fit" not in text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "lib.usage.route_job(config, marked_urgent=urgent)" in readme
    assert "choose_fit" not in readme
    source = (ROOT / "lib" / "orchestrate.py").read_text(encoding="utf-8")
    assert "choose_fit" not in source
    assert _named_fields(text, "RouteRequest") == tuple(
        field.name for field in fields(RouteRequest)
    )
    assert _named_fields(text, "FitChoice") == tuple(
        field.name for field in fields(FitChoice)
    )
    assert _named_fields(text, "RouteDecision") == tuple(
        field.name for field in fields(RouteDecision)
    )
    headers = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("<!-- orch:")
    ]
    assert headers
    for header in headers:
        assert parse_header_line(header)["from"] == "grok-bot"
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                modules.add(node.module)
    assert modules <= {
        "__future__",
        "importlib",
        "collections.abc",
        "dataclasses",
        "pathlib",
        "lib.factory_md",
        "lib.handoff",
        "lib.load",
        "lib.schema",
    }


def _named_fields(text: str, label: str) -> tuple[str, ...]:
    prefix = f"{label} fields: "
    line = next(item for item in text.splitlines() if item.startswith(prefix))
    return tuple(re.findall(r"`([A-Za-z0-9_]+)`", line))
