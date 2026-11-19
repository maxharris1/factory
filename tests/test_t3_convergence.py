from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path

from tests.test_auto_extra_round import AutoFlow, capped_wakes
from tests.test_dispatcher import VERDICT, job_count
from tests.test_rounds_and_caps import HEADS, round_of


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
