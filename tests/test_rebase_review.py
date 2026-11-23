"""#1408: the ready after a rebase-only fix sends the rebased head to review at no round's cost."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.types import RetryableStateError
from lib.update_check import RebaseScope
from tests.merge_queue_support import Board, sha
from tests.test_dispatcher import READY, VERDICT
from tests.test_lifecycle import HEADS, Flow, jobs, outcome
from tests.test_rebase_kind import REBASE, in_review, set_state


def rebase_then_ready(flow: Flow, head: str) -> str | None:
    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"
    flow.finish("claude", "ready-after-rebase")
    flow.head = head
    return flow.post("ready-after-rebase", READY.format(head=head), "claude-login")


def capped_at_cap(flow: Flow) -> None:
    assert flow.go() == "queued-build"
    flow.finish("claude", f"ready-{HEADS[1][:6]}")
    for number in range(1, 4):
        assert flow.ready(HEADS[number]) == "queued-review"
        flow.finish("codex", f"revise-{number}/3")
        if number < 3:
            assert flow.revise(f"{number}/3") == "queued-review-fix"
            flow.finish("claude", f"ready-{HEADS[number + 1][:6]}")
    assert flow.revise("3/3") == "capped-review"
    assert (flow.state()["workflow_state"], flow.state()["review_round"]) == ("capped", 3)


def revise_with(flow: Flow, number: int, blockers: list[str]) -> str | None:
    body = VERDICT.format(round=f"{number}/3", head=flow.head, verdict="REVISE", risk="high")
    body += "".join(f"- [blocking] {finding}: still open.\n" for finding in blockers)
    return flow.post(f"revise-{number}/3", body, "codex-login")


def handed_off_with_falling_blockers(flow: Flow) -> None:
    assert flow.go() == "queued-build"
    flow.finish("claude", f"ready-{HEADS[1][:6]}")
    for number, blockers in ((1, ["f-1", "f-2", "f-3", "f-4"]), (2, ["f-5", "f-6"])):
        assert flow.ready(HEADS[number]) == "queued-review"
        flow.finish("codex", f"revise-{number}/3")
        assert revise_with(flow, number, blockers) == "queued-review-fix"
        flow.finish("claude", f"ready-{HEADS[number + 1][:6]}")
    assert flow.ready(HEADS[3]) == "queued-review"
    flow.finish("codex", "approve-3/3")
    assert flow.approve("3/3") == "handed-off"


def newest_review(flow: Flow) -> dict:
    return json.loads(jobs(flow.queue, "review")[-1]["detail"])


def spent(flow: Flow) -> tuple:
    row = flow.state()
    return row["workflow_state"], row["review_round"], row["cap_extra"]


def test_a_rebase_from_capped_at_the_cap_is_reviewed_at_the_cap(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    capped_at_cap(flow)
    flow.dispatcher.rebase_classifier = lambda repo, base, last, head: RebaseScope(last, HEADS[7], HEADS[8])

    assert rebase_then_ready(flow, HEADS[4]) == "queued-review"

    review = newest_review(flow)
    assert (review["round"], review["source"]) == ("3/3", "rebase")
    assert review["rebase"] == {
        "last_reviewed_head": HEADS[3],
        "reviewed_base": HEADS[7],
        "base": HEADS[8],
    }
    assert spent(flow) == ("review", 2, 0)


def test_a_rebase_from_fixing_under_the_cap_is_reviewed_at_the_next_round(tmp_path: Path) -> None:
    flow = in_review(tmp_path)
    set_state(flow, "fixing", 1)

    assert rebase_then_ready(flow, HEADS[3]) == "queued-review"

    review = newest_review(flow)
    assert (review["round"], review["source"]) == ("2/3", "rebase")
    assert spent(flow) == ("review", 1, 0)


def test_a_rebase_from_review_is_reviewed_at_the_owed_round(tmp_path: Path) -> None:
    flow = in_review(tmp_path)

    assert rebase_then_ready(flow, HEADS[3]) == "queued-review"

    review = newest_review(flow)
    assert (review["round"], review["source"]) == ("1/3", "rebase")
    assert spent(flow) == ("review", 0, 0)


def test_a_rebase_from_handed_off_at_the_cap_is_reviewed_at_the_approved_round(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)

    assert rebase_then_ready(flow, HEADS[4]) == "queued-review"

    review = newest_review(flow)
    assert (review["round"], review["source"]) == ("3/3", "rebase")
    assert spent(flow) == ("review", 2, 0)
    assert flow.state()["approved_head"] is None


@pytest.mark.parametrize(
    "factory_change",
    [{"auto_extra_rounds": 1}, {"stop_on": "convergence"}],
    ids=["rounds", "convergence"],
)
def test_a_revise_on_a_rebase_review_at_the_cap_takes_no_extra_round(
    tmp_path: Path, factory_change: dict
) -> None:
    flow = Flow(tmp_path)
    flow.factory = replace(flow.factory, **factory_change)
    handed_off_with_falling_blockers(flow)
    assert rebase_then_ready(flow, HEADS[4]) == "queued-review"
    flow.finish("codex", "revise-3/3")

    assert revise_with(flow, 3, ["f-9"]) == "capped-review"

    assert spent(flow) == ("capped", 3, 0)


def test_a_ready_at_the_unchanged_head_puts_the_state_back(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    capped_at_cap(flow)
    before = len(jobs(flow.queue))

    assert rebase_then_ready(flow, HEADS[3]) == "rebase-noop"

    assert spent(flow) == ("capped", 3, 0)
    assert len(jobs(flow.queue)) == before + 1
    assert flow.actions.states[-1] == "capped"
    with flow.queue.connect() as connection:
        source = connection.execute(
            "SELECT source FROM ticket_transitions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()["source"]
    assert source == "rebase-noop"


def test_a_noop_rebase_from_the_merge_queue_restores_the_approval(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    approval = {
        column: board.state("42")[column]
        for column in ("approved_head", "approved_at", "approve_comment_id")
    }
    board.post(pr, "rebase-42", REBASE, "grok-login")
    board.poll()
    assert board.state("42")["approved_head"] is None
    board.finish("claude")
    before = len(board.jobs())

    board.ready(pr, pr.head, "ready-after-rebase")
    board.poll()

    assert board.outcome("ready-after-rebase") == "rebase-noop"
    row = board.state("42")
    assert row["workflow_state"] == "merge-queue"
    assert {column: row[column] for column in approval} == approval
    assert len(board.jobs()) == before


def test_a_rebase_review_is_queued_after_the_cap_was_lowered(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    capped_at_cap(flow)
    flow.factory = replace(flow.factory, rounds_high=2)
    assert rebase_then_ready(flow, HEADS[4]) == "queued-review"
    assert newest_review(flow)["round"] == "3/2"
    assert flow.state()["cap_extra"] == 0


@pytest.mark.parametrize(
    ("verdict", "answered", "after"),
    [("APPROVE", "handed-off", ("handed-off", 2, 0)), ("REVISE", "capped-review", ("capped", 3, 0))],
)
def test_a_verdict_on_a_rebase_review_above_a_lowered_cap_is_acted_on(
    tmp_path: Path, verdict: str, answered: str, after: tuple
) -> None:
    flow = Flow(tmp_path)
    capped_at_cap(flow)
    flow.factory = replace(flow.factory, rounds_high=2)
    assert rebase_then_ready(flow, HEADS[4]) == "queued-review"
    flow.finish("codex", "verdict-3/2")
    body = VERDICT.format(round="3/2", head=flow.head, verdict=verdict, risk="high")
    if verdict == "REVISE":
        body += "- [blocking] f-40: still open.\n"

    assert flow.post("verdict-3/2", body, "codex-login") == answered

    assert spent(flow) == after


def test_a_verdict_above_a_lowered_cap_at_a_head_no_rebase_review_covers_is_rejected(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    capped_at_cap(flow)
    flow.factory = replace(flow.factory, rounds_high=2)
    assert rebase_then_ready(flow, HEADS[4]) == "queued-review"
    flow.finish("codex", "verdict-3/2")
    flow.head = HEADS[5]
    body = VERDICT.format(round="3/2", head=flow.head, verdict="APPROVE", risk="high")

    assert flow.post("verdict-3/2", body, "codex-login") == "rejected-over-cap"


def test_a_rebase_review_reusing_round_one_carries_the_range_diff(tmp_path: Path) -> None:
    flow = Flow(tmp_path, rounds_high=1)
    assert flow.go() == "queued-build"
    flow.finish("claude", f"ready-{HEADS[1][:6]}")
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex", "revise-1/1")
    assert flow.revise("1/1") == "capped-review"
    flow.dispatcher.rebase_classifier = lambda *args: RebaseScope(HEADS[1], HEADS[4], HEADS[5])
    assert rebase_then_ready(flow, HEADS[2]) == "queued-review"
    assert newest_review(flow).get("rebase") == {
        "last_reviewed_head": HEADS[1],
        "reviewed_base": HEADS[4],
        "base": HEADS[5],
    }


def test_a_noop_ready_naming_a_head_the_pr_left_is_stale(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    old_head = flow.head
    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"
    flow.finish("claude", "ready-after-rebase")
    flow.head = HEADS[4]
    ready = READY.format(head=old_head)
    assert flow.post("ready-after-rebase", ready, "claude-login") == "stale-ready-head"
    assert flow.state()["workflow_state"] != "handed-off"
    assert flow.state()["approved_head"] is None


def test_a_noop_restore_finishes_after_a_failed_label_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = Flow(tmp_path)
    capped_at_cap(flow)
    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"
    flow.finish("claude", "ready-after-rebase")
    original = flow.actions.set_state
    failures, written = ["capped"], []

    def fail_once(ticket, state):
        if state in failures:
            failures.remove(state)
            raise RuntimeError("transient state label failure")
        written.append(state)
        return original(ticket, state)

    monkeypatch.setattr(flow.actions, "set_state", fail_once)
    with pytest.raises(RetryableStateError):
        flow.post("ready-after-rebase", READY.format(head=flow.head), "claude-login")
    for _ in range(3):
        flow.dispatcher.process(flow.snapshot())
    assert outcome(flow.queue, "ready-after-rebase") == "rebase-noop"
    assert written[-1] == "capped"
    assert flow.state()["workflow_state"] == "capped"


@pytest.mark.parametrize("policy", [{"auto_extra_rounds": 1}, {"stop_on": "convergence"}])
def test_an_ordinary_review_after_a_rebase_review_takes_the_extra_round(
    tmp_path: Path, policy: dict
) -> None:
    flow = Flow(tmp_path)
    flow.factory = replace(flow.factory, **policy)
    handed_off_with_falling_blockers(flow)
    assert rebase_then_ready(flow, HEADS[4]) == "queued-review"
    flow.finish("codex", "revise-3/3")
    assert revise_with(flow, 3, ["f-9"]) == "capped-review"
    go = (
        "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->\n"
        "Overrule the finding: f-9. Accepted.\n"
    )
    assert flow.post("go-overrule", go, "grok-login") == "overruled-finding"
    assert newest_review(flow).get("source") is None
    flow.finish("codex", "revise-overrule")
    body = VERDICT.format(round="3/3", head=flow.head, verdict="REVISE", risk="high")
    body += "- [blocking] f-10: new issue.\n"
    assert flow.post("revise-overrule", body, "codex-login") == "queued-review-fix"
    assert flow.state()["cap_extra"] == 1
