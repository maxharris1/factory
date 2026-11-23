from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib import job_detail
from lib.brief.common import decided_section
from lib.dispatch.verdict.repeat import STOP_OPTIONS, _quote
from lib.dispatcher import RetryableStateError
from lib.intake_risk import INTAKE_RISK_SCHEMA
from lib.orchestrate import approver_go_post
from tests.test_approver_credential import MAX, AppFlow, grok_wakes
from tests.test_dispatcher import ANSWERS, GO, READY, VERDICT, job_count
from tests.test_rounds_and_caps import HEADS
from tests.test_unsatisfiable_criterion import revise, to_first_review

F1 = "- [blocking] f-1: the parser accepts prose.\n"
F1_AGAIN = "- [blocking] f-1: the parser still accepts prose in flags.\n"
F2 = "- [blocking] f-2: the help text is stale.\n"
RESPONSE = "Fixed f-1: the parser rejects prose now.\n\nOther notes.\n"
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


def capped_stop(tmp_path: Path, stop_on: str = "rounds", cap: int = 3) -> AppFlow:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, auto_extra_rounds=0, stop_on=stop_on, rounds_high=cap)
    to_first_review(flow)
    bodies = (F1, F2, "- [blocking] f-3: the exit code is 0.\n")
    for number, body in enumerate(bodies[:-1], start=1):
        assert revise(flow, f"{number}/{cap}", f"revise-{number}", body=body) == "queued-review-fix"
        respond(flow, number, f"Fixed f-{number} in round {number}.\n")
    assert revise(flow, f"3/{cap}", "revise-3", body=bodies[-1]) == "capped-review"
    return flow


def stalled_stop(tmp_path: Path) -> AppFlow:
    return capped_stop(tmp_path, stop_on="convergence", cap=4)


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
        (stalled_stop, "capped", "review not converging", "stalled"),
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
    ("body", "outcome", "choice"),
    [
        ("/go", "resumed-fix", "Another round"),
        (
            "/go\nOverrule the finding: prose in flags is allowed.",
            "overruled-finding",
            "Overrule the finding: prose in flags is allowed.",
        ),
    ],
    ids=["plain", "named"],
)
def test_maxs_go_on_a_repeat_stop_records_the_question_and_the_chosen_option(
    tmp_path: Path, body: str, outcome: str, choice: str
) -> None:
    flow = repeat_stop(tmp_path)
    asked = wake_detail(flow, "clarify", "repeat-block")["question"]

    assert flow.post("go-max", body, MAX) == outcome

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


def fix_detail(flow: AppFlow, source: str) -> dict:
    with flow.queue.connect() as connection:
        [row] = connection.execute(
            "SELECT id, detail FROM jobs WHERE action = 'fix' AND source_comment_id = ?", (source,)
        ).fetchall()
        return json.loads(job_detail.load_row(connection, row["id"], row["detail"]))


NUMBERED = "1. [blocking] f-1.\n   Rule: the CLI rejects prose.\n   cli.py:22 accepts prose and returns success.\n"


def test_a_stop_quotes_each_findings_explanation_and_the_whole_response(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=NUMBERED) == "queued-review-fix"
    respond(flow, 1, "Fixed f-1.\nThe parser now returns 2 for prose.\n")
    assert revise(flow, "2/3", "revise-2", body=NUMBERED) == "waiting-max-repeat"

    question = wake_detail(flow, "clarify", "repeat-block")["question"]

    assert question.count("> 1. [blocking] f-1.\n>    Rule: the CLI rejects prose.\n>    cli.py:22 accepts prose") == 2
    assert "> Fixed f-1.\n> The parser now returns 2 for prose." in question


def test_a_quote_keeps_a_findings_continuation_paragraph() -> None:
    quoted = _quote(
        "1. [blocking] f-1.\n   Rule: reject prose.\n\n   cli.py:22 accepts prose and returns success.",
        frozenset({"f-1"}),
    )

    assert "cli.py:22 accepts prose and returns success." in quoted


SPACED = "1. [blocking] f-1.\n   Rule: the CLI rejects prose.\n\n   cli.py:22 accepts prose and returns success.\n"


def test_a_stop_quotes_the_continuation_paragraphs_of_a_finding_and_its_response(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=SPACED + "2. [blocking] f-2.\n\n   help.py:9 is stale.\n") == (
        "queued-review-fix"
    )
    respond(flow, 1, "- Fixed f-1.\n\n  The parser now returns 2 for prose.\n\nOther notes.\n")
    assert revise(flow, "2/3", "revise-2", body=SPACED) == "waiting-max-repeat"

    question = wake_detail(flow, "clarify", "repeat-block")["question"]

    assert question.count(
        "> 1. [blocking] f-1.\n>    Rule: the CLI rejects prose.\n>\n>    cli.py:22 accepts prose and returns success."
    ) == 2
    assert "> - Fixed f-1.\n>\n>   The parser now returns 2 for prose." in question
    assert "help.py:9" not in question
    assert "Other notes." not in question


def test_a_long_finding_does_not_crowd_out_the_next_one(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, auto_extra_rounds=0)
    to_first_review(flow)
    for number in (1, 2):
        assert revise(flow, f"{number}/3", f"revise-{number}", body=f"- [blocking] f-{number + 6}: round {number}.\n") == (
            "queued-review-fix"
        )
        respond(flow, number, f"Fixed f-{number + 6} in round {number}.\n")
    long_f1 = "- [blocking] f-1: " + "the parser accepts prose " * 60 + "\n"
    assert revise(flow, "3/3", "revise-3", body=long_f1 + F2) == "capped-review"

    question = wake_detail(flow, "capped", "review round cap reached")["question"]

    assert "> - [blocking] f-2: the help text is stale." in question


def test_a_stop_quotes_the_ready_that_started_the_review_not_a_refused_one(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=F1) == "queued-review-fix"
    flow.finish("claude")
    stale = READY.format(head=HEADS[0]).replace("Ready.\n", "Fixed f-1 at the wrong head.\n")
    assert flow.post("ready-stale", stale, "claude-login") == "stale-ready-head"
    flow.head = HEADS[2]
    ready = READY.format(head=flow.head).replace("Ready.\n", "Fixed f-1 at the right head.\n")
    assert flow.post("ready-1", ready, "claude-login") == "queued-review"
    flow.finish("codex")
    assert revise(flow, "2/3", "revise-2", body=F1_AGAIN) == "waiting-max-repeat"

    question = wake_detail(flow, "clarify", "repeat-block")["question"]

    assert "> Fixed f-1 at the right head." in question
    assert "wrong head" not in question


def answered(question: str, answer: str) -> str:
    return f"**Question**\n{question}\n**Answer**\n{answer}\n"


def test_a_go_that_quotes_the_question_records_only_its_answer(tmp_path: Path) -> None:
    flow = repeat_stop(tmp_path)
    asked = wake_detail(flow, "clarify", "repeat-block")["question"]
    choice = "Overrule the finding: accept this behavior."

    go = GO.format(state="building").replace("Go.\n", answered(asked, choice))

    assert flow.post("go-max", go, MAX) == "overruled-finding"

    [decision] = decisions(flow)
    assert decision["choice"] == choice


def test_answers_that_quote_the_unsat_question_record_only_the_answer(tmp_path: Path) -> None:
    flow = unsat_stop(tmp_path)
    asked = wake_detail(flow, "clarify", "criterion-unsatisfiable")["question"]
    body = ANSWERS.replace("Answers.\n", answered(asked, "ac-1 now reads: the CLI exits 2."))

    assert flow.post("answers-max", body, "grok-login") == "resumed-review"

    [decision] = decisions(flow)
    assert decision["choice"] == "Change the criterion: ac-1 now reads: the CLI exits 2."


ABANDONED = "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator state=abandoned -->\nSplit: file smaller tickets.\n"


@pytest.mark.parametrize(
    ("make", "stop", "verdict"),
    [(repeat_stop, "repeat", "revise-2"), (capped_stop, "capped", "revise-3"), (unsat_stop, "unsat", "revise-1")],
)
def test_split_on_any_stop_records_its_option(tmp_path: Path, make, stop: str, verdict: str) -> None:
    flow = make(tmp_path)

    assert flow.post("split", ABANDONED, "grok-login") == "abandoned"

    [decision] = decisions(flow)
    assert (decision["choice"], decision["question_id"]) == ("Split: file smaller tickets.", verdict)
    assert decision["options"] == [option.name for option in STOP_OPTIONS[stop]]


@pytest.mark.parametrize(
    ("make", "body", "login", "outcome", "choice"),
    [
        (capped_stop, "/go", MAX, "queued-fix", "Another round"),
        (unsat_stop, GO.format(state="building"), MAX, "queued-fix", "Fix round: Go."),
    ],
    ids=["capped", "unsat"],
)
def test_the_fix_a_stop_releases_carries_its_decision(
    tmp_path: Path, make, body: str, login: str, outcome: str, choice: str
) -> None:
    flow = make(tmp_path)

    assert flow.post("go-max", body, login) == outcome

    assert [decision["choice"] for decision in fix_detail(flow, "go-max")["decisions"]] == [choice]


OVERRULE = (
    "<!-- orch:v1 kind=go from=grok-bot role=orchestrator risk=low state=building -->\n"
    "Overrule the finding: f-1 (https://github.com/owner/repo/pull/42#issuecomment-revise-2). "
    "Prose in flags is accepted.\n"
)


def work(flow: AppFlow) -> dict[str, int]:
    row = flow.state()
    return {
        "fix": job_count(flow.queue, "fix"),
        "build": job_count(flow.queue, "build"),
        "review": job_count(flow.queue, "review"),
        "review_round": int(row["review_round"]),
        "cap_extra": int(row["cap_extra"]),
    }


def review_detail(flow: AppFlow, source: str) -> dict:
    with flow.queue.connect() as connection:
        [row] = connection.execute(
            "SELECT id, detail FROM jobs WHERE action = 'review' AND source_comment_id = ?", (source,)
        ).fetchall()
        return json.loads(job_detail.load_row(connection, row["id"], row["detail"]))


def assert_rereviewed(flow: AppFlow, before: dict[str, int], stopped: int, body: str = OVERRULE) -> None:
    assert flow.post("go-overrule", body, MAX) == "overruled-finding"
    flow.dispatcher.process(flow.snapshot())
    assert work(flow) == {**before, "review": before["review"] + 1, "review_round": stopped - 1}
    row = flow.state()
    assert row["workflow_state"] == "review"
    assert row["paused_question_id"] is None
    detail = review_detail(flow, "go-overrule")
    assert detail["round"].startswith(f"{stopped}/")
    assert any(decision["choice"].startswith("Overrule the finding: f-1") for decision in detail["decisions"])
    assert "Overrule the finding: f-1" in decided_section(detail, reviewer=True)


def low_risk_extra_round_used(tmp_path: Path) -> AppFlow:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, rounds_low=1, auto_extra_rounds=0)
    high = flow.snapshot
    flow.snapshot = lambda: replace(high(), risk="low")
    with flow.queue.connect() as connection:
        connection.execute(INTAKE_RISK_SCHEMA)
        connection.execute("INSERT INTO intake_risk VALUES ('owner/repo', '42', 'low')")
        connection.commit()
    to_first_review(flow)
    first = VERDICT.format(round="1/1", head=flow.head, verdict="REVISE", risk="low") + F1
    assert flow.post("revise-1", first, "codex-login") == "capped-review"
    assert flow.post("go-lift", "/go\nOne extra round for f-1 only.", MAX) == "queued-fix"
    assert flow.state()["cap_extra"] == 1
    respond(flow, 1)
    second = VERDICT.format(round="2/2", head=flow.head, verdict="REVISE", risk="low") + F1_AGAIN
    assert flow.post("revise-2", second, "codex-login") in ("capped-review", "waiting-max-repeat")
    return flow


def test_an_overrule_go_after_the_first_extra_round_queues_a_same_round_review(tmp_path: Path) -> None:
    flow = low_risk_extra_round_used(tmp_path)
    before = work(flow)
    assert (before["fix"], before["cap_extra"]) == (1, 1)

    assert_rereviewed(flow, before, stopped=2)


def test_an_overrule_go_on_a_repeat_park_queues_a_same_round_review(tmp_path: Path) -> None:
    flow = repeat_stop(tmp_path)
    before = work(flow)

    assert_rereviewed(flow, before, stopped=2)


def test_an_overrule_go_on_a_capped_stop_queues_a_same_round_review(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)
    before = work(flow)

    assert_rereviewed(flow, before, stopped=3)


def test_an_overrule_go_on_a_stalled_stop_queues_a_same_round_review(tmp_path: Path) -> None:
    flow = stalled_stop(tmp_path)
    before = work(flow)

    assert_rereviewed(flow, before, stopped=3)


def test_the_overrule_review_runs_at_the_current_head(tmp_path: Path) -> None:
    flow = repeat_stop(tmp_path)

    assert flow.post("go-overrule", OVERRULE, MAX) == "overruled-finding"

    with flow.queue.connect() as connection:
        [row] = connection.execute(
            "SELECT head FROM jobs WHERE action = 'review' AND source_comment_id = 'go-overrule'"
        ).fetchall()
    assert row["head"] == flow.head


def test_an_overrule_go_records_its_decision(tmp_path: Path) -> None:
    flow = repeat_stop(tmp_path)
    asked = wake_detail(flow, "clarify", "repeat-block")["question"]

    assert flow.post("go-overrule", OVERRULE, MAX) == "overruled-finding"

    [decision] = decisions(flow)
    assert decision["question"] == asked
    assert "f-1" in decision["question"]
    assert decision["choice"].startswith("Overrule the finding: f-1")
    assert (decision["question_id"], decision["answer_id"]) == ("revise-2", "go-overrule")


def test_a_replayed_overrule_queues_one_review(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)
    before = work(flow)

    assert_rereviewed(flow, before, stopped=3)
    flow.dispatcher.process(flow.snapshot())

    assert work(flow) == {**before, "review": before["review"] + 1, "review_round": 2}
    assert [decision["answer_id"] for decision in decisions(flow)] == ["go-overrule"]


@pytest.mark.parametrize(
    ("make", "stopped"),
    [(repeat_stop, 2), (capped_stop, 3), (stalled_stop, 3)],
    ids=["repeat", "capped", "stalled"],
)
def test_a_replayed_overrule_repairs_a_failed_review_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make, stopped: int
) -> None:
    flow = make(tmp_path)
    before = work(flow)
    actions = flow.dispatcher.review_actions
    write = actions.set_state
    failures = ["review"]

    def set_state(ticket, state: str) -> None:
        if state in failures:
            failures.remove(state)
            raise RuntimeError("transient label failure")
        write(ticket, state)

    monkeypatch.setattr(actions, "set_state", set_state)
    with pytest.raises(RetryableStateError):
        flow.post("go-overrule", OVERRULE, MAX)
    for _ in range(3):
        flow.dispatcher.process(flow.snapshot())

    assert work(flow) == {**before, "review": before["review"] + 1, "review_round": stopped - 1}
    assert flow.state()["workflow_state"] == "review"
    assert actions.states[-1] == "review"


def test_another_round_after_an_overrule_has_nothing_to_release(tmp_path: Path) -> None:
    flow = repeat_stop(tmp_path)
    assert flow.post("go-overrule", OVERRULE, MAX) == "overruled-finding"

    assert flow.post("go-max", "/go\nAnother round.", MAX) == "slash-go-no-target"
    assert flow.state()["workflow_state"] == "review"


@pytest.mark.parametrize(
    ("make", "round_text", "outcome"),
    [(repeat_stop, "2/3", "queued-review-fix"), (capped_stop, "3/3", "capped-review")],
    ids=["repeat", "capped"],
)
def test_a_same_head_revise_after_an_overrule_is_not_a_duplicate(
    tmp_path: Path, make, round_text: str, outcome: str
) -> None:
    flow = make(tmp_path)
    assert flow.post("go-overrule", OVERRULE, MAX) == "overruled-finding"
    flow.finish("codex")

    assert revise(flow, round_text, "revise-again", body="- [blocking] f-9: the docs are missing.\n") == outcome


def test_an_approve_at_the_stopped_round_after_an_overrule_is_inside_the_cap(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)
    assert flow.post("go-overrule", OVERRULE, MAX) == "overruled-finding"
    flow.finish("codex")

    approve = VERDICT.format(round="3/3", head=flow.head, verdict="APPROVE", risk="high")

    assert flow.post("approve-3", approve, "codex-login") not in ("rejected-over-cap", "duplicate-verdict")
    assert flow.state()["cap_extra"] == 0


def test_an_overrule_from_a_login_that_may_not_lift_the_cap_changes_nothing(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)
    before = work(flow)

    assert flow.post("go-overrule", OVERRULE, "grok-login") != "overruled-finding"

    assert work(flow) == before
    assert decisions(flow) == []


STOP_QUESTION = (
    "f-1 repeats. Which option?\n"
    "- Another round: an approver's /go adds one round and queues the fix.\n"
    "- Overrule the finding: no fix is queued.\n"
    "- Split: close this PR.\n"
)


def max_quoted_overrule() -> str:
    return approver_go_post(
        "go",
        answered(STOP_QUESTION, "Overrule the finding: f-1. Prose in flags is accepted."),
        {},
        app_trust=False,
        gh_config_dir=None,
    ).body


@pytest.mark.parametrize(
    ("make", "stopped"),
    [(repeat_stop, 2), (capped_stop, 3), (low_risk_extra_round_used, 2)],
    ids=["repeat", "capped", "low-risk-extra-used"],
)
def test_an_overrule_answer_quoted_under_maxs_words_queues_a_review(tmp_path: Path, make, stopped: int) -> None:
    flow = make(tmp_path)
    before = work(flow)
    earlier = decisions(flow)

    assert_rereviewed(flow, before, stopped, body=max_quoted_overrule())
    [decision] = decisions(flow)[len(earlier):]
    assert decision["choice"].startswith("Overrule the finding: f-1")
