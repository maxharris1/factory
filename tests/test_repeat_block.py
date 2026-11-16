from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.evidence.findings import blocking_ids
from lib.dispatcher import Dispatcher, TrackedTicket
from lib.handoff import parse_comment, repeat_id
from tests.test_approver_credential import APP_BOT, MAX, AppFlow, approvals, grok_wakes, load, orch_text, trusted
from tests.test_dispatcher import (
    ANSWERS,
    GO,
    READY,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    handled_outcome,
    job_count,
)
from tests.test_hooks import skipped_keys
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
        (AC2 + F1, "- ac-2: met. The previously unmet requirement now passes.\n" + F2),
        ("ac-2 was unmet in an old review.\n" + F1, "ac-2 was unmet in an old review.\n" + F2),
        (AC2, "# | ac-2 | unmet |\n\n" + F2),
        (AC2, "- [blocking] ac-2suffix: a different identifier.\n" + F2),
    ],
    ids=["free-text-same-rule", "not-consecutive", "minor", "code-block", "met", "now-met", "prose", "heading", "id-prefix"],
)
def test_no_repeat_id_or_reused_id_takes_the_fix_path(tmp_path: Path, bodies: tuple[str, ...]) -> None:
    flow = AppFlow(tmp_path, enabled=True)

    assert rounds(flow, *bodies) in ("queued-review-fix", "capped-review")

    assert flow.state()["workflow_state"] in ("fixing", "capped")
    assert repeat_wakes(flow) == []


def test_marked_up_criterion_prefix_does_not_repeat(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)

    assert rounds(flow, AC2, "- [blocking] **ac-2**-other: a different identifier.\n" + F2) == "queued-review-fix"

    assert flow.state()["workflow_state"] == "fixing"
    assert repeat_wakes(flow) == []


@pytest.mark.parametrize(
    "bodies",
    [
        (F1, "- [blocking] **f-1**-other: a different identifier.\n" + F2),
        (F1, "- [blocking] __f-1__-other: a different identifier.\n" + F2),
        (F1, "- [blocking] f-1-other: a different identifier.\n" + F2),
        (AC2, "- [blocking] **ac-2**_other: a different identifier.\n" + F2),
        (AC2, "- [blocking] **ac-2**--other: a different identifier.\n" + F2),
        (AC2, "- [blocking] **ac-2**-_other: a different identifier.\n" + F2),
        (F1, "- [blocking] **f-1**--other: a different identifier.\n" + F2),
        (F1, "- [blocking] **f-1**-_other: a different identifier.\n" + F2),
    ],
    ids=[
        "bold-finding",
        "underscore-finding",
        "bare-finding",
        "criterion-underscore",
        "criterion-double-hyphen",
        "criterion-hyphen-underscore",
        "finding-double-hyphen",
        "finding-hyphen-underscore",
    ],
)
def test_a_suffix_after_id_markup_does_not_repeat(tmp_path: Path, bodies: tuple[str, ...]) -> None:
    flow = AppFlow(tmp_path, enabled=True)

    assert rounds(flow, *bodies) == "queued-review-fix"

    assert repeat_wakes(flow) == []


def test_a_repeat_at_the_cap_parks_instead_of_capping(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)

    assert rounds(flow, F2, F2.replace("f-2", "f-3"), F1, last_extra=" repeat=f-2") == "waiting-max-repeat"

    assert_parked(flow, 2, "revise-3", 3)
    with flow.queue.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auto_extra_comments").fetchone()[0] == 0


def test_a_repeat_after_maxs_release_parks_again(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)
    assert rounds(flow, F1, F1) == "waiting-max-repeat"
    assert flow.post("go-max", "/go", MAX) == "resumed-fix"
    next_review(flow, 2)
    fixes = job_count(flow.queue, "fix")

    assert revise(flow, "3/3", "revise-3", body=F1) == "waiting-max-repeat"

    assert_parked(flow, fixes, "revise-3", 3)
    assert len(repeat_wakes(flow)) == 2


def test_a_released_named_repeat_still_blocks_the_next_round(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", " repeat=f-1", body="Still failing.\n") == "waiting-max-repeat"
    assert flow.post("go-max", "/go", MAX) == "resumed-fix"
    next_review(flow, 1)
    fixes = job_count(flow.queue, "fix")

    assert revise(flow, "2/3", "revise-2", body=F1) == "waiting-max-repeat"

    assert_parked(flow, fixes, "revise-2", 2)
    assert len(repeat_wakes(flow)) == 2


@pytest.mark.parametrize(
    ("last", "expected", "wakes"),
    [
        (F1, "queued-review-fix", []),
        (F2, "waiting-max-repeat", ["repeat-block: f-2, comment:102, comment:103, stops=1, for=factory"]),
    ],
    ids=["superseded-id", "final-id"],
)
def test_the_last_of_tied_round_one_verdicts_is_the_predecessor(
    tmp_path: Path, last: str, expected: str, wakes: list[str]
) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    flow.now[0] += 10
    post = flow.post

    def post_in_the_same_second(*args: str) -> str:
        flow.now[0] -= 10
        return post(*args)

    flow.post = post_in_the_same_second
    assert revise(flow, "1/3", "100", " unsat=ac-1", body=F1) == "waiting-max-unsat"
    assert flow.post("101", "/go", MAX) == "resumed-review"
    flow.finish("codex")
    assert revise(flow, "1/3", "102", body=F2) == "queued-review-fix"
    assert {item.created_at for item in flow.comments if item.id in {"100", "101", "102"}} == {flow.now[0]}
    flow.post = post
    next_review(flow, 1)

    assert revise(flow, "2/3", "103", body=last) == expected

    assert repeat_wakes(flow) == wakes


def withdraw_first_verdict(flow: AppFlow) -> None:
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=F1) == "queued-review-fix"
    next_review(flow, 1)
    flow.comments = [
        replace(item, body="Withdrawn.\n", edited_at=flow.now[0]) if item.id == "revise-1" else item
        for item in flow.comments
    ]


def test_an_unreadable_earlier_verdict_still_lets_a_new_finding_fix(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    withdraw_first_verdict(flow)

    assert revise(flow, "2/3", "revise-2", body=F2) == "queued-review-fix"


def test_an_unreadable_earlier_verdict_still_lets_a_named_repeat_park(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    withdraw_first_verdict(flow)

    assert revise(flow, "2/3", "revise-2", " repeat=f-1", body=F1) == "waiting-max-repeat"

    assert repeat_wakes(flow)[0].startswith("repeat-block: f-1, no earlier verdict, ")


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
        ("claude-login", "rejected-answers-author", None),
        (APP_BOT, "answers-while-repeat-block", "go-not-from-approver"),
        ("grok-login", "answers-while-repeat-block", "go-not-from-approver"),
    ],
    ids=["worker", "factory-app", "factory-login"],
)
def test_an_answers_does_not_leave_the_repeat_block(
    tmp_path: Path, author: str, outcome: str, wake: str | None
) -> None:
    flow = parked(tmp_path)
    before = jobs(flow)

    assert flow.post("answers-1", ANSWERS, author) == outcome

    assert flow.state()["workflow_state"] == "waiting-max"
    assert flow.state()["paused_action"] == "fix"
    added = [job for job in jobs(flow) if job not in before]
    assert added == ([] if wake is None else [(wake, "grok", "answers-1")])
    assert skipped_keys(flow.queue) == ([("rejected", "answers-1")] if wake is None else [])


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
        ("- ac-2, unmet.\n- ac-3: **unmet**\n- ac-4 | unmet\n", {"ac-2", "ac-3", "ac-4"}),
        ("- ac-2: met. The previously unmet requirement now passes.\n", set()),
        ("- ac-2 was unmet last round and is met now.\n", set()),
        ("ac-2: unmet.\n", set()),
        ("## ac-2: unmet\n", set()),
        ("> - ac-2: unmet\n> - [blocking] f-1: quoted.\n", set()),
        ("| ac-3 | met | was unmet |\n", set()),
        ("id | status\n--- | ---\nac-3 | unmet\n", {"ac-3"}),
        ("| ac-3 | unmet |\n| --- | --- |\n| ac-4 | met |\n", set()),
        ("# | ac-2 | unmet |\n", set()),
        ("- [blocking] ac-2suffix: other.\n- ac-3-b: unmet\n- **ac-4**x: unmet\n", set()),
        ("- [blocking] **ac-2**: x.\n- __ac-3__: unmet\n", {"ac-2", "ac-3"}),
        ("- [blocking] **ac-2**-other: x.\n- **ac-3**_b: unmet\n- `ac-4`-b: unmet\n", set()),
        ("- [blocking] **f-1**-other: x.\n- [blocking] __f-2__-b: x.\n- [blocking] f-3-b: x.\n", set()),
        ("- [blocking] **f-1**--other: x.\n- [blocking] **f-2**-_b: x.\n- **ac-3**--b: unmet\n- `ac-4`-_b: unmet\n", set()),
        ("- [blocking] **f-1** -- x.\n- [blocking] **f-2**-- x.\n- **ac-3**- unmet\n", {"f-1", "f-2", "ac-3"}),
        ("- [blocking] **f-1**: x.\n- [blocking] **f-2** - x.\n- **ac-3** - unmet\n", {"f-1", "f-2", "ac-3"}),
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


PR_URL = "https://github.com/owner/repo/pull/42#issuecomment-"


class SizedFlow(AppFlow):
    def __init__(self, tmp_path: Path, changed_lines: int | None):
        super().__init__(tmp_path, enabled=True)
        self.tmp_path = tmp_path
        self.changed_lines = changed_lines

    def snapshot(self) -> TrackedTicket:
        return replace(super().snapshot(), changed_lines=self.changed_lines)

    def post(self, comment_id: str, body: str, author: str) -> str:
        self.now[0] += 10
        self.comments.append(
            comment(comment_id, body, author=author, created_at=self.now[0], url=PR_URL + comment_id)
        )
        self.dispatcher.process(self.snapshot())
        return handled_outcome(self.queue, comment_id)

    def restart(self) -> None:
        config, _ = load(self.tmp_path, orch_text(enabled=True))
        self.dispatcher = Dispatcher(
            self.queue.path,
            self.queue,
            trusted_logins=trusted(config),
            review_actions=FakeReviewActions(),
            grok_delivery=FakeGrokDelivery(),
            approvals=approvals(config),
        )


def release_and_review(flow: SizedFlow, number: int) -> None:
    assert flow.post(f"go-max-{number}", "/go", MAX) == "resumed-fix"
    next_review(flow, number)


def test_the_first_repeat_stop_carries_count_links_size_and_the_factory(tmp_path: Path) -> None:
    flow = SizedFlow(tmp_path, changed_lines=42)

    assert rounds(flow, F1, F1) == "waiting-max-repeat"

    assert repeat_wakes(flow) == [
        f"repeat-block: f-1, {PR_URL}revise-1, {PR_URL}revise-2, stops=1, for=factory, changed_lines=42"
    ]


def test_the_wake_leaves_out_a_size_the_poll_did_not_read(tmp_path: Path) -> None:
    flow = SizedFlow(tmp_path, changed_lines=None)

    assert rounds(flow, F1, F1) == "waiting-max-repeat"

    assert repeat_wakes(flow) == [f"repeat-block: f-1, {PR_URL}revise-1, {PR_URL}revise-2, stops=1, for=factory"]


def test_the_second_stop_on_one_id_goes_to_max_across_a_restart(tmp_path: Path) -> None:
    flow = SizedFlow(tmp_path, changed_lines=7)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)
    assert rounds(flow, F1, F1) == "waiting-max-repeat"
    flow.restart()
    release_and_review(flow, 2)

    assert revise(flow, "3/3", "revise-3", body=F1) == "waiting-max-repeat"

    assert_parked(flow, job_count(flow.queue, "fix"), "revise-3", 3)
    assert repeat_wakes(flow)[-1] == (
        f"repeat-block: f-1, {PR_URL}revise-2, {PR_URL}revise-3, stops=2, for=max, changed_lines=7"
    )


def test_a_stop_on_another_id_counts_from_one(tmp_path: Path) -> None:
    flow = SizedFlow(tmp_path, changed_lines=None)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)
    assert rounds(flow, F1, F1) == "waiting-max-repeat"
    release_and_review(flow, 2)

    assert revise(flow, "3/3", "revise-3", " repeat=f-2", body=F2) == "waiting-max-repeat"

    assert repeat_wakes(flow)[-1].endswith(f"{PR_URL}revise-3, stops=1, for=factory")


def edit(flow: AppFlow, comment_id: str, body: str) -> None:
    flow.comments = [
        replace(item, body=body, edited_at=flow.now[0]) if item.id == comment_id else item
        for item in flow.comments
    ]


@pytest.mark.parametrize(
    ("comment_id", "body"),
    [
        ("revise-1", None),
        ("revise-1", "Withdrawn.\n"),
        ("revise-2", None),
        ("revise-2", "Withdrawn.\n"),
    ],
    ids=["earlier-id-edited", "earlier-withdrawn", "stopped-id-edited", "stopped-withdrawn"],
)
def test_an_edited_history_keeps_the_count_of_accepted_stops(
    tmp_path: Path, comment_id: str, body: str | None
) -> None:
    flow = SizedFlow(tmp_path, changed_lines=7)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)
    assert rounds(flow, F1, F1) == "waiting-max-repeat"
    release_and_review(flow, 2)
    original = next(item.body for item in flow.comments if item.id == comment_id)
    edit(flow, comment_id, body or original.replace("f-1", "f-2"))
    flow.restart()

    assert revise(flow, "3/3", "revise-3", " repeat=f-1", body=F1) == "waiting-max-repeat"

    assert repeat_wakes(flow)[-1].endswith(f"{PR_URL}revise-3, stops=2, for=max, changed_lines=7")
