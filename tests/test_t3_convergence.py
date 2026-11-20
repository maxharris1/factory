from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path

import pytest

from lib.dispatch.verdict.repeat import STOP_OPTIONS
from tests.qa_support import Harness, qa_report, ready, scenarios, verdict
from tests.test_approver_credential import MAX, AppFlow
from tests.test_auto_extra_round import AutoFlow, capped_wakes
from tests.test_dispatcher import GO, VERDICT, job_count
from tests.test_qa_gate_table import blocking
from tests.test_repeat import ABANDONED, decisions, wake_detail
from tests.test_repeat_block import edit
from tests.test_rounds_and_caps import HEADS, round_of
from tests.test_unsatisfiable_criterion import revise, to_first_review


class ConvergingFlow(AutoFlow):
    def __init__(self, tmp_path: Path, stop_on: str) -> None:
        super().__init__(tmp_path, auto_extra_rounds=0)
        self.factory = dataclasses.replace(self.factory, stop_on=stop_on)
        self.next_id = 1

    def revise_with(self, round_text: str, ids: Sequence[str], repeat: str | None = None) -> str:
        body = VERDICT.format(round=round_text, head=self.head, verdict="REVISE", risk="high")
        if repeat is not None:
            body = body.replace(" -->", f" repeat={repeat} -->", 1)
        body += "".join(f"- [blocking] {rule_id}: still open.\n" for rule_id in ids)
        return self.post(f"revise-{round_text}", body, "codex-login")

    def fresh(self, count: int) -> list[str]:
        ids = [f"f-{self.next_id + offset}" for offset in range(count)]
        self.next_id += count
        return ids

    def review(self, number: int, cap: int = 3) -> None:
        assert self.ready(HEADS[number]) == "queued-review"
        assert round_of(self.finish("codex")) == f"{number}/{cap}"

    def falling_to_round_three(self) -> str:
        assert self.go("go-1") == "queued-build"
        self.finish("claude")
        for number, count in ((1, 4), (2, 2)):
            self.review(number)
            assert self.revise_with(f"{number}/3", self.fresh(count)) == "queued-review-fix"
            self.finish("claude")
        self.review(3)
        return self.revise_with("3/3", self.fresh(1))

    def grok_jobs(self) -> int:
        with self.queue.connect() as connection:
            return int(
                connection.execute("SELECT count(*) FROM jobs WHERE recipient = 'grok'").fetchone()[0]
            )


def test_a_converging_round_at_the_cap_gets_a_round(tmp_path: Path) -> None:
    flow = ConvergingFlow(tmp_path, "convergence")
    assert flow.falling_to_round_three() == "queued-review-fix"
    state = flow.state()
    assert (state["workflow_state"], state["cap_extra"]) == ("fixing", 1)
    assert job_count(flow.queue, "fix") == 3
    assert round_of(flow.finish("claude")) == "4/4"
    [notice] = flow.actions.posted
    assert notice.startswith("Extra round 1: this round cut the open blocking findings to a new low")
    assert "`review.stop_on: convergence`" in notice
    assert capped_wakes(flow) == 0
    assert flow.grok_jobs() == 0


def test_rounds_still_caps(tmp_path: Path) -> None:
    flow = ConvergingFlow(tmp_path, "rounds")
    assert flow.falling_to_round_three() == "capped-review"
    state = flow.state()
    assert (state["workflow_state"], state["cap_extra"]) == ("capped", 0)
    assert job_count(flow.queue, "fix") == 2
    assert capped_wakes(flow) == 1
    assert flow.grok_jobs() == 1
    assert flow.actions.posted == []


def test_a_flat_round_gets_one_round_then_a_second_flat_round_caps(tmp_path: Path) -> None:
    flow = ConvergingFlow(tmp_path, "convergence")
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    for number, count in ((1, 2), (2, 1)):
        flow.review(number)
        assert flow.revise_with(f"{number}/3", flow.fresh(count)) == "queued-review-fix"
        flow.finish("claude")
    flow.review(3)
    assert flow.revise_with("3/3", flow.fresh(1)) == "queued-review-fix"
    [notice] = flow.actions.posted
    assert notice.startswith("Extra round 1: this round did not cut the open blocking findings.")
    flow.finish("claude")
    flow.review(4, cap=4)
    assert flow.revise_with("4/4", flow.fresh(1)) == "capped-review"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 1)
    assert capped_wakes(flow) == 1
    assert len(flow.actions.posted) == 1


def _stalled(tmp_path: Path) -> ConvergingFlow:
    flow = ConvergingFlow(tmp_path, "convergence")
    flow.factory = dataclasses.replace(flow.factory, rounds_high=4)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    for number in (1, 2):
        flow.review(number, cap=4)
        assert flow.revise_with(f"{number}/4", flow.fresh(2)) == "queued-review-fix"
        flow.finish("claude")
    flow.review(3, cap=4)
    assert flow.revise_with("3/4", flow.fresh(2)) == "capped-review"
    return flow


def _stalled_question(flow: ConvergingFlow) -> str:
    return wake_detail(flow, "capped", "review not converging")["question"]


def test_two_flat_rounds_below_the_cap_stall(tmp_path: Path) -> None:
    flow = _stalled(tmp_path)
    state = flow.state()
    assert (state["workflow_state"], state["cap_extra"], state["review_round"]) == ("capped", 0, 3)
    assert capped_wakes(flow) == 1
    wake = wake_detail(flow, "capped", "review not converging")
    assert wake["detail"].startswith("review not converging")
    assert wake["question"].startswith("Blocking findings did not shrink in rounds 2 and 3")
    for option in STOP_OPTIONS["stalled"]:
        assert f"- {option.name}: {option.consequence}" in wake["question"]
    assert flow.actions.posted == []


def test_a_carried_id_on_a_flat_round_repeats(tmp_path: Path) -> None:
    flow = ConvergingFlow(tmp_path, "convergence")
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    flow.review(1)
    assert flow.revise_with("1/3", ["f-1", "f-2"]) == "queued-review-fix"
    flow.finish("claude")
    flow.review(2)
    assert flow.revise_with("2/3", ["f-1", "f-3"]) == "waiting-max-repeat"
    assert wake_detail(flow, "clarify", "repeat-block")["detail"].startswith("repeat-block: f-1")


def test_go_lifts_a_stalled_stop(tmp_path: Path) -> None:
    flow = _stalled(tmp_path)
    asked = _stalled_question(flow)
    assert flow.go("go-max") == "queued-fix"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
    [decision] = decisions(flow)
    assert (decision["question"], decision["choice"]) == (asked, "Another round: Go.")
    assert decision["options"] == [option.name for option in STOP_OPTIONS["stalled"]]


def test_abandoning_a_stalled_stop_records_split_on_its_question(tmp_path: Path) -> None:
    flow = _stalled(tmp_path)
    asked = _stalled_question(flow)
    assert flow.post("split", ABANDONED, "grok-login") == "abandoned"
    [decision] = decisions(flow)
    assert (decision["question"], decision["choice"]) == (asked, "Split: file smaller tickets.")


WITHDRAWN = pytest.mark.parametrize("comment_id", ["revise-3/4", "revise-2/4"], ids=["stop", "earlier"])


@WITHDRAWN
def test_go_lifts_a_stalled_stop_after_a_verdict_is_withdrawn(tmp_path: Path, comment_id: str) -> None:
    flow = _stalled(tmp_path)
    edit(flow, comment_id, "Withdrawn.\n")
    assert flow.go("go-max") == "queued-fix"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
    [decision] = decisions(flow)
    assert decision["question"].startswith("Blocking findings did not shrink")
    assert decision["choice"] == "Another round: Go."


@WITHDRAWN
def test_abandoning_a_stalled_stop_after_a_verdict_is_withdrawn(tmp_path: Path, comment_id: str) -> None:
    flow = _stalled(tmp_path)
    edit(flow, comment_id, "Withdrawn.\n")
    assert flow.post("split", ABANDONED, "grok-login") == "abandoned"
    [decision] = decisions(flow)
    assert decision["question"].startswith("Blocking findings did not shrink")
    assert decision["choice"] == "Split: file smaller tickets."


def _carried(tmp_path: Path, stop_on: str) -> str:
    tmp_path.mkdir()
    flow = ConvergingFlow(tmp_path, stop_on)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    flow.review(1)
    assert flow.revise_with("1/3", ["f-1", "f-2", "f-3"]) == "queued-review-fix"
    flow.finish("claude")
    flow.review(2)
    return flow.revise_with("2/3", ["f-1", "f-4"], repeat="f-1")


def test_a_carried_id_on_a_new_low_continues(tmp_path: Path) -> None:
    assert _carried(tmp_path / "convergence", "convergence") == "queued-review-fix"
    assert _carried(tmp_path / "rounds", "rounds") == "waiting-max-repeat"


def test_a_superseded_unsat_verdict_does_not_set_the_low(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = dataclasses.replace(flow.factory, stop_on="convergence")
    to_first_review(flow)
    unsat = "- ac-1: unsatisfiable. Owner: review. Needs a rewrite.\n"
    assert revise(flow, "1/3", "unsat", " unsat=ac-1", body=unsat) == "waiting-max-unsat"
    assert flow.post("rewrite", "/go\nac-1 now reads: the CLI exits 2.", MAX) == "resumed-review"
    flow.finish("codex")
    for number, count in ((1, 4), (2, 2), (3, 1)):
        if number > 1:
            flow.finish("claude")
            assert flow.ready(HEADS[number]) == "queued-review"
            flow.finish("codex")
        body = "".join(f"- [blocking] f-{i}: still open.\n" for i in range(1, count + 1))
        assert revise(flow, f"{number}/3", f"v{number}", body=body) == "queued-review-fix"
    assert flow.state()["cap_extra"] == 1


def _qa_round(run: Harness, number: int, failed: Sequence[str], cap: int = 3) -> None:
    head = str(number) * 40
    if number > 1:
        run.finish("claude", f"r{number}")
    run.post(f"r{number}", ready(head, f"{number}/{cap}", body=scenarios("qa-1", "qa-2")), head=head)
    run.poll()
    run.finish("codex", f"v{number}")
    run.post(f"v{number}", verdict(head, "APPROVE", f"{number}/{cap}"))
    rows = "".join(
        f"| {qa_id} | ac-1 | {'Failed' if qa_id in failed else 'Passed'} | https://example.test/run/1 |\n"
        for qa_id in ("qa-1", "qa-2")
    )
    body = "- [blocking] f-1 saved draft disappears\nEvidence: https://example.test/run/1\n" + rows
    run.post(f"q{number}", qa_report(head, "FAIL", body=body, cause="product"))
    run.poll(3)


def _converging_qa(tmp_path: Path) -> Harness:
    run = Harness(tmp_path, blocking())
    run.thread.base = dataclasses.replace(
        run.thread.base, factory=dataclasses.replace(run.thread.base.factory, stop_on="convergence")
    )
    return run


def test_a_resumed_qa_repeat_keeps_its_failed_scenarios(tmp_path: Path) -> None:
    run = _converging_qa(tmp_path)
    _qa_round(run, 1, ["qa-1", "qa-2"])
    _qa_round(run, 2, ["qa-1", "qa-2"])
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("waiting-max",)]
    run.post("go-again", "/go")
    run.poll()
    _qa_round(run, 3, ["qa-1"])
    assert run.rows("SELECT workflow_state, review_round, cap_extra FROM ticket_state") == [("fixing", 3, 1)]


def test_a_capped_round_keeps_its_failed_scenarios(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    for number, failed in enumerate((["qa-1"], ["qa-2"], ["qa-1"]), start=1):
        _qa_round(run, number, failed)
    assert run.rows("SELECT workflow_state, review_round FROM ticket_state") == [("capped", 3)]
    run.post("go-again", GO.format(state="building"))
    run.now[0] += 3600
    run.poll()
    _qa_round(run, 4, ["qa-1"], cap=4)
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("waiting-max",)]


def test_an_upgraded_database_recovers_failed_scenarios_from_fix_jobs(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    _qa_round(run, 1, ["qa-2", "qa-1"])
    with run.queue.connect() as connection:
        connection.execute("ALTER TABLE review_events DROP COLUMN qa_failed")
    upgraded = Harness(tmp_path, blocking())
    assert upgraded.rows("SELECT comment_id, qa_failed FROM review_events") == [("v1", '["qa-1","qa-2"]')]
