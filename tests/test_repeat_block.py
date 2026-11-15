from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.evidence.findings import blocking_ids
from lib.handoff import parse_comment, repeat_id
from tests.test_approver_credential import APP_BOT, MAX, AppFlow, grok_wakes
from tests.test_dispatcher import ANSWERS, GO, READY, VERDICT, job_count
from tests.test_rounds_and_caps import HEADS
from tests.test_slash_go import jobs
from tests.test_unsatisfiable_criterion import revise, to_first_review

F1 = "- [blocking] f-1: the parser accepts prose.\n"
F2 = "- [blocking] f-2: the parser drops flags. This is the same rule as last round.\n"
AC2 = "- ac-2: unmet. The CLI still exits 0.\n"


def repeat_wakes(flow: AppFlow) -> list[str]:
    return [
        str(json.loads(row["detail"])["detail"])
        for row in grok_wakes(flow, "clarify")
        if "repeat-block" in str(row["detail"])
    ]


def next_review(flow: AppFlow, number: int) -> None:
    flow.finish("claude")
    assert flow.ready(HEADS[number + 1]) == "queued-review"
    flow.finish("codex")


def rounds(flow: AppFlow, *bodies: str, last_extra: str = "") -> str:
    to_first_review(flow)
    for number, body in enumerate(bodies[:-1], start=1):
        assert revise(flow, f"{number}/3", f"revise-{number}", body=body) == "queued-review-fix"
        next_review(flow, number)
    number = len(bodies)
    return revise(flow, f"{number}/3", f"revise-{number}", last_extra, body=bodies[-1])


def assert_parked(flow: AppFlow, fixes: int, comment_id: str, round_number: int) -> None:
    state = flow.state()
    assert (state["workflow_state"], state["review_round"], state["cap_extra"]) == (
        "waiting-max",
        round_number,
        0,
    )
    assert (state["paused_action"], state["paused_recipient"], state["paused_question_id"]) == (
        "fix",
        "claude",
        comment_id,
    )
    assert job_count(flow.queue, "fix") == fixes
    assert flow.dispatcher.review_actions.states[-1] == "waiting-max"


@pytest.mark.parametrize(
    ("extra", "body"),
    [(" repeat=ac-2", "Still failing.\n"), ("", "repeat=ac-2\n\nStill failing.\n")],
    ids=["header", "first-body-line"],
)
def test_a_repeat_revise_parks_with_one_wake_naming_both_verdicts(
    tmp_path: Path, extra: str, body: str
) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=AC2) == "queued-review-fix"
    next_review(flow, 1)
    fixes = job_count(flow.queue, "fix")

    header = VERDICT.format(round="2/3", head=flow.head, verdict="REVISE", risk="high")
    header = header.replace(" -->", f"{extra} -->", 1).replace("Verdict.\n", "")
    assert flow.post("revise-2", header + body, "codex-login") == "waiting-max-repeat"

    assert_parked(flow, fixes, "revise-2", 2)
    wakes = repeat_wakes(flow)
    assert len(wakes) == 1
    assert "repeat-block: ac-2" in wakes[0]
    assert wakes[0].index("revise-1") < wakes[0].index("revise-2")
    before = jobs(flow)
    flow.dispatcher.process(flow.snapshot())
    assert jobs(flow) == before
    assert len(repeat_wakes(flow)) == 1


@pytest.mark.parametrize(
    "bodies",
    [(F1, F1), (AC2, AC2), (F1 + AC2, "- [blocking] **ac-2**: still unmet.\n")],
    ids=["finding", "criterion", "marked-up-criterion"],
)
def test_a_reused_blocking_id_in_consecutive_revises_parks(tmp_path: Path, bodies: tuple[str, ...]) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    fixes_before_last = 1

    assert rounds(flow, *bodies) == "waiting-max-repeat"

    assert_parked(flow, fixes_before_last, "revise-2", 2)
    assert len(repeat_wakes(flow)) == 1


@pytest.mark.parametrize(
    "bodies",
    [
        (F1, F2),
        (F1, F2, F1),
        ("- [minor] f-1: naming.\n", "- [minor] f-1: naming.\n"),
        ("```\n- [blocking] f-1: x\n```\n" + F2, "```\n- [blocking] f-1: x\n```\n"),
        ("- ac-2: met.\n", "- ac-2: met.\n"),
    ],
    ids=["free-text-same-rule", "not-consecutive", "minor", "code-block", "met"],
)
def test_no_repeat_id_or_reused_id_takes_the_fix_path(tmp_path: Path, bodies: tuple[str, ...]) -> None:
    flow = AppFlow(tmp_path, enabled=True)

    assert rounds(flow, *bodies) in ("queued-review-fix", "capped-review")

    assert flow.state()["workflow_state"] in ("fixing", "capped")
    assert repeat_wakes(flow) == []


def test_a_repeat_at_the_cap_parks_instead_of_capping(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)

    assert rounds(flow, F2, F2.replace("f-2", "f-3"), F1, last_extra=" repeat=f-2") == "waiting-max-repeat"

    assert_parked(flow, 2, "revise-3", 3)
    with flow.queue.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auto_extra_comments").fetchone()[0] == 0


def test_a_repeat_parks_while_ci_is_pending(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, start_before_ci=True)
    to_first_review(flow)
    flow.ci = "pending"

    assert revise(flow, "1/3", "revise-1", " repeat=f-1") == "waiting-max-repeat"

    assert flow.state()["workflow_state"] == "waiting-max"
    assert repeat_wakes(flow)[0].startswith("repeat-block: f-1, no earlier verdict, ")


def parked(tmp_path: Path) -> AppFlow:
    flow = AppFlow(tmp_path, enabled=True)
    assert rounds(flow, F1, F1) == "waiting-max-repeat"
    return flow


@pytest.mark.parametrize(
    ("author", "outcome", "wake"),
    [
        ("claude-login", "rejected-answers-author", "rejected"),
        (APP_BOT, "answers-while-repeat-block", "go-not-from-approver"),
        ("grok-login", "answers-while-repeat-block", "go-not-from-approver"),
    ],
    ids=["worker", "factory-app", "factory-login"],
)
def test_an_answers_does_not_leave_the_repeat_block(tmp_path: Path, author: str, outcome: str, wake: str) -> None:
    flow = parked(tmp_path)
    before = jobs(flow)

    assert flow.post("answers-1", ANSWERS, author) == outcome

    assert flow.state()["workflow_state"] == "waiting-max"
    assert flow.state()["paused_action"] == "fix"
    added = [job for job in jobs(flow) if job not in before]
    assert added == [(wake, "grok", "answers-1")]


def test_a_factory_go_or_a_builder_ready_does_not_leave_the_repeat_block(tmp_path: Path) -> None:
    flow = parked(tmp_path)
    fixes = job_count(flow.queue, "fix")

    assert flow.go("go-2", author=APP_BOT) == "go-not-from-approver"
    assert flow.post("slash-1", "/go", "claude-login") == "go-not-from-approver"
    flow.head = HEADS[4]
    assert flow.post("ready-x", READY.format(head=flow.head), "claude-login") == "ready-while-repeat"

    assert flow.state()["workflow_state"] == "waiting-max"
    assert job_count(flow.queue, "fix") == fixes


@pytest.mark.parametrize("release", ["go", "slash"])
def test_maxs_go_releases_the_repeat_block_into_one_fix(tmp_path: Path, release: str) -> None:
    flow = parked(tmp_path)
    fixes = job_count(flow.queue, "fix")

    if release == "go":
        outcome = flow.post("go-max", GO.format(state="building"), MAX)
    else:
        outcome = flow.post("go-max", "/go\nKeep the strict parser.", MAX)

    assert outcome == "resumed-fix"
    state = flow.state()
    assert (state["workflow_state"], state["paused_action"], state["cap_extra"]) == ("fixing", None, 0)
    assert job_count(flow.queue, "fix") == fixes + 1
    fix = flow.queue.claim("claude")
    assert fix is not None and fix.request.source_comment_id == "go-max"
    assert json.loads(fix.request.detail)["round"] == "3/3"
    before = jobs(flow)
    flow.dispatcher.process(flow.snapshot())
    assert jobs(flow) == before


def test_maxs_go_at_the_cap_also_grants_the_next_round(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    assert rounds(flow, F2, F2.replace("f-2", "f-3"), F1, last_extra=" repeat=f-1") == "waiting-max-repeat"

    assert flow.post("go-max", "/go", MAX) == "resumed-fix"

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
    flow.dispatcher.process(flow.snapshot())
    assert flow.state()["cap_extra"] == 1


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (F1 + AC2, {"f-1", "ac-2"}),
        ("| id | status |\n| --- | --- |\n| ac-3 | unmet |\n", {"ac-3"}),
        ("- [closed] f-1: fixed.\n- ac-10: met.\n- Note: ac-1 was unmet.\n", set()),
        ("    - [blocking] f-4: indented code\n", set()),
    ],
)
def test_blocking_ids(body: str, expected: set[str]) -> None:
    assert blocking_ids(body) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<!-- orch:v1 kind=verdict verdict=REVISE repeat=f-2 -->\nrepeat=ac-1\n", "f-2"),
        ("<!-- orch:v1 kind=verdict verdict=REVISE -->\n\n  repeat=ac-1  \nmore\n", "ac-1"),
        ("<!-- orch:v1 kind=verdict verdict=REVISE -->\nNote.\nrepeat=ac-1\n", None),
        ("<!-- orch:v1 kind=verdict verdict=REVISE -->\nrepeat=the parser rule\n", None),
    ],
)
def test_repeat_id(text: str, expected: str | None) -> None:
    assert repeat_id(parse_comment(text)) == expected
