from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.verdict.repeat import STOP_OPTIONS
from tests.test_approver_credential import MAX, AppFlow, grok_wakes
from tests.test_dispatcher import ANSWERS, GO, READY
from tests.test_rounds_and_caps import HEADS
from tests.test_unsatisfiable_criterion import revise, to_first_review

F1 = "- [blocking] f-1: the parser accepts prose.\n"
F1_AGAIN = "- [blocking] f-1: the parser still accepts prose in flags.\n"
F2 = "- [blocking] f-2: the help text is stale.\n"
RESPONSE = "Fixed f-1: the parser rejects prose now.\nOther notes.\n"
UNSAT = "- ac-1: unsatisfiable. It needs PR #7 merged first.\n"


def respond(flow: AppFlow, number: int, body: str = RESPONSE) -> None:
    flow.finish("claude")
    flow.head = HEADS[number + 1]
    ready = READY.format(head=flow.head).replace("Ready.\n", body)
    assert flow.post(f"ready-{number}", ready, "claude-login") == "queued-review"
    flow.finish("codex")


def wake_detail(flow: AppFlow, action: str, marker: str) -> dict:
    [row] = [row for row in grok_wakes(flow, action) if marker in str(row["detail"])]
    return json.loads(row["detail"])


def decisions(flow: AppFlow) -> list[dict]:
    with flow.queue.connect() as connection:
        rows = connection.execute("SELECT * FROM decisions ORDER BY answered_at, position").fetchall()
    return [{**dict(row), "options": json.loads(row["options"])} for row in rows]


def repeat_stop(tmp_path: Path) -> AppFlow:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=F1 + F2) == "queued-review-fix"
    respond(flow, 1)
    assert revise(flow, "2/3", "revise-2", body=F1_AGAIN) == "waiting-max-repeat"
    return flow


def capped_stop(tmp_path: Path) -> AppFlow:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, auto_extra_rounds=0)
    to_first_review(flow)
    bodies = (F1, F2, "- [blocking] f-3: the exit code is 0.\n")
    for number, body in enumerate(bodies[:-1], start=1):
        assert revise(flow, f"{number}/3", f"revise-{number}", body=body) == "queued-review-fix"
        respond(flow, number, f"Fixed f-{number} in round {number}.\n")
    assert revise(flow, "3/3", "revise-3", body=bodies[-1]) == "capped-review"
    return flow


def unsat_stop(tmp_path: Path) -> AppFlow:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", " unsat=ac-1", body=UNSAT) == "waiting-max-unsat"
    return flow


def test_a_repeat_stop_quotes_the_finding_of_each_round_and_the_builders_response(tmp_path: Path) -> None:
    flow = repeat_stop(tmp_path)

    question = wake_detail(flow, "clarify", "repeat-block")["question"]

    assert question.startswith("The reviewer blocked f-1 again in round 2.")
    assert "> - [blocking] f-1: the parser accepts prose." in question
    assert "f-2" not in question
    assert "> Fixed f-1: the parser rejects prose now." in question
    assert "Other notes." not in question
    assert "> - [blocking] f-1: the parser still accepts prose in flags." in question
    assert question.index("Round 1") < question.index("Builder's response") < question.index("Round 2")


def test_a_response_that_names_no_disputed_id_is_quoted_from_its_first_paragraph(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=F1) == "queued-review-fix"
    respond(flow, 1, "Reworked the parser.\nSecond line.\n\nUnrelated paragraph.\n")
    assert revise(flow, "2/3", "revise-2", body=F1_AGAIN) == "waiting-max-repeat"

    question = wake_detail(flow, "clarify", "repeat-block")["question"]

    assert "> Reworked the parser.\n> Second line." in question
    assert "Unrelated paragraph." not in question


def test_a_capped_stop_quotes_each_rounds_findings_and_responses(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)

    question = wake_detail(flow, "capped", "review round cap reached")["question"]

    assert question.startswith("Round 3 reached the review cap with f-3 still blocking.")
    for quoted in (
        "> - [blocking] f-1: the parser accepts prose.",
        "> Fixed f-1 in round 1.",
        "> - [blocking] f-2: the help text is stale.",
        "> Fixed f-2 in round 2.",
        "> - [blocking] f-3: the exit code is 0.",
    ):
        assert quoted in question


def test_an_unsat_stop_quotes_the_criterion_finding(tmp_path: Path) -> None:
    flow = unsat_stop(tmp_path)

    question = wake_detail(flow, "clarify", "criterion-unsatisfiable")["question"]

    assert question.startswith("The reviewer called ac-1 unsatisfiable in round 1.")
    assert "> - ac-1: unsatisfiable. It needs PR #7 merged first." in question


@pytest.mark.parametrize(
    ("make", "action", "marker", "stop"),
    [
        (repeat_stop, "clarify", "repeat-block", "repeat"),
        (capped_stop, "capped", "review round cap reached", "capped"),
        (unsat_stop, "clarify", "criterion-unsatisfiable", "unsat"),
    ],
)
def test_each_stop_offers_two_or_three_named_options_with_their_consequence(
    tmp_path: Path, make, action: str, marker: str, stop: str
) -> None:
    flow = make(tmp_path)

    question = wake_detail(flow, action, marker)["question"]

    options = STOP_OPTIONS[stop]
    assert 2 <= len(options) <= 3
    assert len({option.name for option in options}) == len(options)
    for option in options:
        assert option.consequence
        assert f"- {option.name}: {option.consequence}" in question
    assert question.index("Options:") > question.rindex("> ")


@pytest.mark.parametrize(
    ("body", "choice"),
    [
        ("/go", "Another round"),
        ("/go\nOverrule the finding: prose in flags is allowed.", "Overrule the finding: prose in flags is allowed."),
    ],
    ids=["plain", "named"],
)
def test_maxs_go_on_a_repeat_stop_records_the_question_and_the_chosen_option(
    tmp_path: Path, body: str, choice: str
) -> None:
    flow = repeat_stop(tmp_path)
    asked = wake_detail(flow, "clarify", "repeat-block")["question"]

    assert flow.post("go-max", body, MAX) == "resumed-fix"

    [decision] = decisions(flow)
    assert decision["question"] == asked
    assert decision["options"] == [option.name for option in STOP_OPTIONS["repeat"]]
    assert decision["choice"] == choice
    assert (decision["answerer"], decision["answer_id"], decision["question_id"]) == (MAX, "go-max", "revise-2")


def test_maxs_go_on_a_capped_stop_records_another_round(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)
    asked = wake_detail(flow, "capped", "review round cap reached")["question"]

    assert flow.post("go-max", "/go", MAX) == "queued-fix"
    flow.dispatcher.process(flow.snapshot())

    [decision] = decisions(flow)
    assert (decision["question"], decision["choice"], decision["question_id"]) == (asked, "Another round", "revise-3")
    assert decision["options"] == [option.name for option in STOP_OPTIONS["capped"]]


@pytest.mark.parametrize(
    ("post", "outcome", "choice"),
    [
        (lambda flow: flow.post("go-max", GO.format(state="building"), MAX), "queued-fix", "Fix round: Go."),
        (
            lambda flow: flow.post("go-max", "/go\nac-1 now reads: the CLI exits 2.", MAX),
            "resumed-review",
            "Change the criterion: ac-1 now reads: the CLI exits 2.",
        ),
        (lambda flow: flow.post("go-max", ANSWERS, "grok-login"), "resumed-review", "Change the criterion: Answers."),
    ],
    ids=["building-go", "slash-go", "answers"],
)
def test_the_release_of_an_unsat_stop_records_its_option(tmp_path: Path, post, outcome: str, choice: str) -> None:
    flow = unsat_stop(tmp_path)
    asked = wake_detail(flow, "clarify", "criterion-unsatisfiable")["question"]

    assert post(flow) == outcome

    [decision] = decisions(flow)
    assert (decision["question"], decision["choice"], decision["question_id"]) == (asked, choice, "revise-1")
