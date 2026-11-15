"""#202 (T21b): Grok's extra-round go and one answers per questions comment.

Each helper's comment is posted into the real Dispatcher in the state it is
for: the go on a capped ticket, the answers at waiting-max after a builder's
questions.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import lib.orchestrate as orchestrate
from lib.handoff import parse_comment
from lib.orchestrate import (
    ClassifiedQuestion,
    OrchestrateError,
    QuestionError,
    TicketFlow,
    answers_for,
    extra_round_go,
)
from tests.test_approver_credential import APP_BOT, FAKE_TOKENS, MAX, AppFlow, approvals_for
from tests.test_rounds_and_caps import Flow

WORDS = "Yes, give #42 one more round.\nFix the flaky test first."
INSTRUCTION = "Fix the flaky retry test named in the round-3 verdict, nothing else."
QUESTIONS = (
    "<!-- orch:v1 kind=questions from=claude-bot role=builder -->\n"
    "**Questions**\n- Keep the old flag?\n- Which log level?\n"
)


def go_for(tmp_path: Path, *, enabled: bool = True, **changes: object):
    profile = tmp_path / "grok" / "gh-approver"
    switch = approvals_for(tmp_path, enabled=enabled, profile=str(profile))
    arguments: dict[str, object] = {
        "ticket": "#42",
        "max_words": WORDS,
        "instruction": INSTRUCTION,
        **changes,
    }
    return extra_round_go(
        arguments["ticket"],  # type: ignore[arg-type]
        arguments["max_words"],  # type: ignore[arg-type]
        arguments["instruction"],  # type: ignore[arg-type]
        {"PATH": "/usr/bin", "GH_CONFIG_DIR": "/home/grok/.config/gh-app", **FAKE_TOKENS},
        app_trust=switch.app_trust,
        gh_config_dir=switch.gh_config_dir,
    )


# --- the extra-round go ------------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_the_extra_round_go_lifts_the_cap_in_the_dispatcher(tmp_path: Path, enabled: bool) -> None:
    flow = AppFlow(tmp_path, enabled=enabled)
    flow.to_cap()

    assert flow.post("go-2", go_for(tmp_path, enabled=enabled).body, MAX) == "queued-fix"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)


def test_the_extra_round_go_from_the_app_bot_lifts_nothing(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()

    assert flow.post("go-2", go_for(tmp_path).body, APP_BOT) == "go-not-from-approver"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)


def test_the_extra_round_go_holds_the_ticket_max_words_and_the_instruction(tmp_path: Path) -> None:
    go = parse_comment(go_for(tmp_path).body)

    assert go.as_dict() == {"kind": "go", "from": "grok-bot", "role": "orchestrator", "state": "building"}
    assert go.body == (
        "**Ticket**\n#42\n\n"
        "**Max's words**\n> Yes, give #42 one more round.\n> Fix the flaky test first.\n\n"
        f"**Instruction**\n{INSTRUCTION}\n"
    )


def test_the_extra_round_go_keeps_blank_lines_in_max_words_quoted(tmp_path: Path) -> None:
    go = parse_comment(go_for(tmp_path, max_words="One more.\n\nThen stop.").body)

    assert "**Max's words**\n> One more.\n>\n> Then stop.\n\n" in go.body


def test_the_extra_round_go_posts_only_through_max_approver_profile(tmp_path: Path) -> None:
    assert go_for(tmp_path).env == {
        "PATH": "/usr/bin",
        "GH_CONFIG_DIR": str(tmp_path / "grok" / "gh-approver"),
    }


def test_the_extra_round_go_with_the_switch_off_only_drops_tokens(tmp_path: Path) -> None:
    assert go_for(tmp_path, enabled=False).env == {
        "PATH": "/usr/bin",
        "GH_CONFIG_DIR": "/home/grok/.config/gh-app",
    }


def test_the_extra_round_go_refuses_with_no_approver_profile(tmp_path: Path) -> None:
    switch = approvals_for(tmp_path, enabled=True)
    with pytest.raises(OrchestrateError, match="by hand"):
        extra_round_go(
            "#42",
            WORDS,
            INSTRUCTION,
            {"PATH": "/usr/bin", **FAKE_TOKENS},
            app_trust=switch.app_trust,
            gh_config_dir=switch.gh_config_dir,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"ticket": ""}, "ticket"),
        ({"ticket": "#42\n#43"}, "ticket"),
        ({"max_words": "  \n "}, "Max's exact words"),
        ({"max_words": None}, "Max's exact words"),
        ({"instruction": ""}, "instruction"),
        ({"instruction": None}, "instruction"),
    ],
)
def test_the_extra_round_go_refuses_a_missing_part(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    with pytest.raises(OrchestrateError, match=message):
        go_for(tmp_path, **changes)


def test_there_is_no_ok_helper() -> None:
    names = [name for name in vars(orchestrate) if "ok" in name.lower().split("_")]
    assert names == []
    assert "kind" not in inspect.signature(extra_round_go).parameters


# --- one answers per questions comment ----------------------------------------------


def to_questions(tmp_path: Path) -> Flow:
    flow = Flow(tmp_path)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    assert flow.post("questions-1", QUESTIONS, "claude-login") == "waiting-max"
    flow.finish("grok")
    return flow


def test_one_answers_holding_every_answer_resumes_the_step(tmp_path: Path) -> None:
    flow = to_questions(tmp_path)
    body = answers_for((("Keep the old flag?", "No."), ("Which log level?", "info")))

    assert flow.post("answers-1", body, "grok-login") == "resumed-build"
    assert flow.state()["workflow_state"] == "building"
    assert flow.finish("claude").request.action == "build"


def test_a_second_answers_to_the_same_questions_is_rejected(tmp_path: Path) -> None:
    flow = to_questions(tmp_path)
    flow.post("answers-1", answers_for((("Keep the old flag?", "No."),)), "grok-login")

    second = answers_for((("Which log level?", "info"),))
    assert flow.post("answers-2", second, "grok-login") == "rejected-answers-without-question"


def test_answers_for_renders_every_answer_under_one_header() -> None:
    parsed = parse_comment(
        answers_for(
            (
                ("Keep the old flag?", "  No.\nDrop it in this PR.  "),
                ("Which log level?", "info"),
            )
        )
    )

    assert parsed.as_dict() == {"kind": "answers", "from": "grok-bot", "role": "orchestrator"}
    assert parsed.body == (
        "**Question**\nKeep the old flag?\n\n"
        "**Answer**\nNo.\nDrop it in this PR.\n\n"
        "**Question**\nWhich log level?\n\n"
        "**Answer**\ninfo\n"
    )


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ((), "at least one answer"),
        ((("Keep it?", "Yes."), (" Keep it? ", "No.")), "answered twice"),
        ((("Keep\nit?", "Yes."),), "question text"),
        ((("Keep it?", " "),), "answer"),
        ((("Keep it?",),), "question and answer"),
        ("Keep it?", "question and answer"),
    ],
)
def test_answers_for_refuses_a_bad_answer_set(answers: object, message: str) -> None:
    with pytest.raises(QuestionError, match=message):
        answers_for(answers)  # type: ignore[arg-type]


def test_ticket_flow_posts_one_answers_for_every_product_question() -> None:
    flow = TicketFlow()
    flow.post_questions(
        "Two product calls.",
        (
            ClassifiedQuestion("Ship to production?", "product"),
            ClassifiedQuestion("Use the helper.", "default-able", default="yes"),
            ClassifiedQuestion("Which color?", "product"),
        ),
    )

    body = flow.post_answers((("Which color?", "blue"), ("Ship to production?", "No.")))

    parsed = parse_comment(body)
    assert parsed.as_dict()["kind"] == "answers"
    assert parsed.body == (
        "**Question**\nShip to production?\n\n**Answer**\nNo.\n\n"
        "**Question**\nWhich color?\n\n**Answer**\nblue\n"
    )
    assert [question.answer for question in flow.questions] == ["No.", None, "blue"]
    assert orchestrate.product_questions_cleared(flow.questions)


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ((("Ship to production?", "No."),), "every open product question"),
        ((("Ship to production?", "No."), ("Use the helper.", "no")), "every open product question"),
    ],
)
def test_ticket_flow_answers_must_cover_exactly_the_open_product_questions(
    answers: tuple[tuple[str, str], ...], message: str
) -> None:
    flow = TicketFlow()
    flow.post_questions(
        "Two product calls.",
        (
            ClassifiedQuestion("Ship to production?", "product"),
            ClassifiedQuestion("Use the helper.", "default-able", default="yes"),
            ClassifiedQuestion("Which color?", "product"),
        ),
    )
    with pytest.raises(QuestionError, match=message):
        flow.post_answers(answers)
    assert flow.questions[0].answer is None


def test_ticket_flow_answers_need_the_questions_step_and_an_open_question() -> None:
    flow = TicketFlow()
    with pytest.raises(QuestionError, match="questions step has not run"):
        flow.post_answers((("Ship?", "No."),))
    flow.post_questions("One call.", (ClassifiedQuestion("Ship?", "product"),))
    flow.post_answers((("Ship?", "No."),))
    with pytest.raises(QuestionError, match="no open product question"):
        flow.post_answers((("Ship?", "Yes."),))
