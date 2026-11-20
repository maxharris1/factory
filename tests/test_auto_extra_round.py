from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from lib.brief.rules import review_rules
from lib.dispatcher import Dispatcher
from lib.factory_md import parse_factory_md
from tests.merge_queue_support import Board, sha
from tests.test_dispatcher import (
    TRUSTED_LOGINS,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    handled_outcome,
    job_count,
)
from tests.test_rounds_and_caps import HEADS, Flow, attention, round_of

REPO = Path(__file__).resolve().parents[1]


class PostingActions(FakeReviewActions):
    def __init__(self) -> None:
        super().__init__()
        self.posted: list[str] = []
        self.fail_next = False

    def comment_once(self, ticket, body, marker) -> None:
        del ticket
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("comment post failed")
        if any(marker in existing.splitlines() for existing in self.posted):
            return
        self.posted.append(body)


class AutoFlow(Flow):
    def __init__(self, tmp_path: Path, auto_extra_rounds: int = 2) -> None:
        super().__init__(tmp_path)
        self.actions = PostingActions()
        self.dispatcher = self._dispatcher(tmp_path)
        self.factory = dataclasses.replace(
            self.factory, auto_extra_rounds=auto_extra_rounds
        )

    def _dispatcher(self, tmp_path: Path) -> Dispatcher:
        return Dispatcher(
            tmp_path / "d.db",
            self.queue,
            trusted_logins=TRUSTED_LOGINS,
            review_actions=self.actions,
            grok_delivery=FakeGrokDelivery(),
        )

    def to_round_three(self) -> None:
        assert self.go("go-1") == "queued-build"
        self.finish("claude")
        for number in (1, 2):
            assert self.ready(HEADS[number]) == "queued-review"
            self.finish("codex")
            assert self.revise(f"{number}/3") == "queued-review-fix"
            self.finish("claude")
        assert self.ready(HEADS[3]) == "queued-review"
        assert round_of(self.finish("codex")) == "3/3"

    def granted(self, number: int, k: int) -> None:
        assert self.revise(f"{number}/{number}") == "queued-review-fix"
        state = self.state()
        assert (state["workflow_state"], state["cap_extra"]) == ("fixing", k)
        fix = self.finish("claude")
        assert round_of(fix) == f"{number + 1}/{number + 1}"
        assert self.actions.posted[-1].startswith(f"Auto extra round {k} of 2 ")
        assert self.ready(HEADS[number + 1]) == "queued-review"
        assert round_of(self.finish("codex")) == f"{number + 1}/{number + 1}"


def capped_wakes(flow: Flow) -> int:
    return job_count(flow.queue, "capped")


def auto_rows(flow: Flow) -> list[tuple[str, str]]:
    with flow.queue.connect() as connection:
        return [
            (str(row["comment_id"]), str(row["status"]))
            for row in connection.execute(
                "SELECT comment_id, status FROM auto_extra_comments ORDER BY comment_id"
            )
        ]


def test_without_the_key_the_cap_parks_the_ticket_as_before(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path, auto_extra_rounds=0)
    flow.to_cap()
    assert flow.state()["cap_extra"] == 0
    assert capped_wakes(flow) == 1
    assert flow.actions.posted == []
    assert auto_rows(flow) == []


def test_two_caps_are_granted_then_the_third_waits(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path)
    flow.to_round_three()
    flow.granted(3, 1)
    flow.granted(4, 2)
    assert capped_wakes(flow) == 0
    assert flow.revise("5/5") == "capped-review"
    state = flow.state()
    assert (state["workflow_state"], state["cap_extra"]) == ("capped", 2)
    assert capped_wakes(flow) == 1
    assert len(flow.actions.posted) == 2
    assert job_count(flow.queue, "fix") == 4
    assert flow.go("go-max") == "queued-fix"
    assert flow.state()["cap_extra"] == 3


def test_grant_survives_replays_and_restarts(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path)
    flow.to_round_three()
    assert flow.revise("3/3") == "queued-review-fix"
    before = (job_count(flow.queue, "fix"), flow.state()["cap_extra"], len(flow.actions.posted))
    flow.dispatcher.process(flow.snapshot())
    flow.dispatcher.process(flow.snapshot())
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'revise-3/3'")
    flow.dispatcher.process(flow.snapshot())
    assert handled_outcome(flow.queue, "revise-3/3") == "queued-review-fix"
    flow.dispatcher = flow._dispatcher(tmp_path)
    flow.dispatcher.process(flow.snapshot())
    after = (job_count(flow.queue, "fix"), flow.state()["cap_extra"], len(flow.actions.posted))
    assert after == before == (3, 1, 1)
    assert flow.state()["workflow_state"] == "fixing"
    assert auto_rows(flow) == [("revise-3/3", "posted")]


def test_a_failed_post_retries_next_poll_and_leaves_the_fix(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path)
    flow.to_round_three()
    flow.actions.fail_next = True
    assert flow.revise("3/3") == "queued-review-fix"
    assert flow.actions.posted == []
    assert auto_rows(flow) == [("revise-3/3", "pending")]
    assert (job_count(flow.queue, "fix"), flow.state()["cap_extra"]) == (3, 1)
    flow.dispatcher.process(flow.snapshot())
    flow.dispatcher.process(flow.snapshot())
    assert len(flow.actions.posted) == 1
    assert auto_rows(flow) == [("revise-3/3", "posted")]
    assert (job_count(flow.queue, "fix"), flow.state()["cap_extra"]) == (3, 1)


def test_a_hand_granted_round_counts_toward_n(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path)
    flow.to_round_three()
    with flow.queue.connect() as connection:
        connection.execute("UPDATE ticket_state SET cap_extra = 1 WHERE ticket_id = '42'")
    assert flow.revise("3/4") == "queued-review-fix"
    assert flow.state()["workflow_state"] == "fixing"
    assert flow.state()["review_round"] == 3
    flow.finish("claude")
    assert flow.ready(HEADS[4]) == "queued-review"
    flow.finish("codex")
    assert flow.revise("4/4") == "queued-review-fix"
    assert flow.state()["cap_extra"] == 2
    assert flow.actions.posted[-1].startswith("Auto extra round 2 of 2 ")
    flow.finish("claude")
    assert flow.ready(HEADS[5]) == "queued-review"
    flow.finish("codex")
    assert flow.revise("5/5") == "capped-review"
    assert len(flow.actions.posted) == 1


def test_blocked_at_the_cap_is_never_granted(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path)
    flow.to_round_three()
    body = VERDICT.format(round="3/3", head=flow.head, verdict="BLOCKED", risk="high")
    flow.now[0] += 10
    flow.comments.append(comment("blocked-3", body, author="codex-login", created_at=flow.now[0]))
    flow.dispatcher.process(flow.snapshot())
    assert flow.state()["workflow_state"] == "blocked"
    assert flow.state()["cap_extra"] == 0
    assert job_count(flow.queue, "fix") == 2
    assert flow.actions.posted == []
    assert auto_rows(flow) == []


def test_repeat_parks_instead_of_a_grant(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path)
    flow.to_round_three()
    body = VERDICT.format(round="3/3", head=flow.head, verdict="REVISE", risk="high")
    body = body.replace(" -->", " repeat=f-1 -->", 1)
    assert flow.post("revise-flagged", body, "codex-login") == "waiting-max-repeat"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("waiting-max", 0)
    assert capped_wakes(flow) == 0
    assert flow.actions.posted == []
    assert auto_rows(flow) == []


def test_a_stuck_ticket_is_never_granted(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path)
    flow.to_round_three()
    with flow.queue.connect() as connection:
        connection.execute("UPDATE ticket_state SET stuck = 1 WHERE ticket_id = '42'")
    assert flow.revise("3/3") == "ticket-stuck"
    assert flow.state()["cap_extra"] == 0
    assert job_count(flow.queue, "fix") == 2
    assert auto_rows(flow) == []


def test_stuck_row_refuses_the_grant_even_inside_t3(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path)
    flow.to_round_three()
    with flow.queue.connect() as connection:
        connection.execute("UPDATE ticket_state SET stuck = 1 WHERE ticket_id = '42'")
    flow.dispatcher._is_stuck = lambda ticket: False
    assert flow.revise("3/3") == "capped-review"
    assert flow.state()["cap_extra"] == 0
    assert auto_rows(flow) == []


def test_a_ready_after_the_used_up_cap_is_still_rejected(tmp_path: Path) -> None:
    flow = AutoFlow(tmp_path)
    flow.to_round_three()
    flow.granted(3, 1)
    flow.granted(4, 2)
    assert flow.revise("5/5") == "capped-review"
    assert flow.ready(HEADS[6]) == "capped-ready"
    assert "ready-after-cap" in attention(flow.queue)
    assert job_count(flow.queue, "review") == 5
    body = VERDICT.format(round="6/5", head=flow.head, verdict="REVISE", risk="high")
    assert flow.post("over-cap", body, "codex-login") == "rejected-over-cap"


FACTORY_MD = """---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: none
{extra}ship: handoff
skills: []
---
"""


def test_absent_key_reads_as_zero() -> None:
    config, findings = parse_factory_md(FACTORY_MD.format(extra=""), strict=True)
    assert config is not None and config.auto_extra_rounds == 0
    assert findings == []


def test_key_reads_its_value() -> None:
    text = FACTORY_MD.format(extra="  auto_extra_rounds: 2\n")
    config, findings = parse_factory_md(text, strict=True)
    assert config is not None and config.auto_extra_rounds == 2
    assert findings == []


@pytest.mark.parametrize("value", ["-1", '"2"', "true", "1.5"])
def test_bad_values_are_refused_strictly_and_read_as_zero_leniently(value: str) -> None:
    text = FACTORY_MD.format(extra=f"  auto_extra_rounds: {value}\n")
    config, findings = parse_factory_md(text, strict=True)
    assert config is None
    assert [f.message for f in findings] == [
        "review.auto_extra_rounds must be a non-negative integer"
    ]
    config, findings = parse_factory_md(text, strict=False)
    assert config is not None and config.auto_extra_rounds == 0
    assert [f.severity for f in findings] == ["warning"]


def test_the_factory_repo_turns_it_on() -> None:
    config, findings = parse_factory_md((REPO / "factory.md").read_text(), strict=True)
    assert config is not None and config.auto_extra_rounds == 2
    assert config.stop_on == "convergence"
    assert not [f for f in findings if f.severity == "error"]


def _facts(auto_extra: int) -> dict:
    return {"followup_from": None, "round_number": 1, "auto_extra": auto_extra, "block_on": "any-failure", "stop_on": "rounds"}


def test_review_rules_name_the_policy_only_when_it_is_on() -> None:
    assert "every extra round needs Max's own words" in review_rules(_facts(0))
    assert "auto_extra_rounds" not in review_rules(_facts(0))
    text = review_rules(_facts(2))
    assert "the poller grants up to 2 more itself under factory.md `review.auto_extra_rounds`" in text
    assert "each round past those needs Max's own words" in text


class OutageBoard(Board):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.factory = dataclasses.replace(self.factory, auto_extra_rounds=2)
        self.posted: list[str] = []
        self.outage = True
        self.actions.comment_once = self._comment_once

    def _comment_once(self, ticket, body, marker) -> None:
        del ticket
        if self.outage:
            raise RuntimeError("comment endpoint outage")
        if not any(marker in existing for existing in self.posted):
            self.posted.append(body)

    def approve_after_one_auto_grant(self, *, mergeable: str = "clean"):
        pr = self.open("42", sha(1))
        self.go(pr, "go-42")
        self.poll()
        self.finish("claude")
        for number in (1, 2, 3):
            pr.push(sha(number + 1))
            self.ready(pr, pr.head)
            self.poll()
            self.finish("codex")
            self.revise(pr, f"{number}/3")
            self.poll()
            self.finish("claude")
        pr.push(sha(5), state=mergeable)
        self.ready(pr, pr.head)
        self.poll()
        self.finish("codex")
        self.approve(pr, "4/4")
        self.poll()
        assert self.state("42")["cap_extra"] == 1
        return pr

    def notices(self) -> list[str]:
        with self.queue.connect() as connection:
            return [str(row["status"]) for row in connection.execute(
                "SELECT status FROM auto_extra_comments"
            )]


def test_a_pending_notice_posts_once_after_the_pr_merges(tmp_path: Path) -> None:
    board = OutageBoard(tmp_path)
    pr = board.approve_after_one_auto_grant()
    assert pr.merged
    assert board.notices() == ["pending"]
    board.outage = False
    board.poll()
    board.poll()
    [notice] = [body for body in board.posted if "orch-auto-extra" in body]
    assert notice.startswith("Auto extra round 1 of 2 ")
    assert board.notices() == ["posted"]
    board.poll()
    assert board.dispatcher._tracked_refs() == ()


def test_a_pending_notice_posts_once_after_the_pr_closes(tmp_path: Path) -> None:
    board = OutageBoard(tmp_path)
    pr = board.approve_after_one_auto_grant(mergeable="dirty")
    assert board.state("42")["workflow_state"] == "updating"
    pr.closed = True
    board.poll()
    assert board.state("42")["workflow_state"] == "abandoned"
    assert board.notices() == ["pending"]
    board.outage = False
    board.poll()
    board.poll()
    assert len(board.posted) == 1
    assert board.notices() == ["posted"]
