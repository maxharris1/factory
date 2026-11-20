"""#118: an approver's go on a handed-off ticket at the cap grants one round.

The go raises cap_extra by 1, sets fixing and queues one fix keyed on the go,
and the builder's ready at a new head re-enters review at the next round.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief.common import detail_of
from lib.dispatch.types import LinearState, PullReview, RetryableStateError
from lib.orch_yml import Approvals
from lib.dispatch.helpers import _comment_link
from tests.test_dispatch_answers_handoff import HANDOFF
from tests.test_dispatcher import GO
from tests.test_lifecycle import HEADS, REPO, TICKET, Flow, attention, jobs, outcome, round_of


def fixes(flow: Flow) -> list:
    return jobs(flow.queue, "fix")


def observed(flow: Flow) -> tuple:
    row = flow.state()
    return (
        row["workflow_state"],
        row["cap_extra"],
        row["review_round"],
        sum(row["recipient"] != "grok" for row in jobs(flow.queue)),
        attention(flow.queue),
    )


def test_approver_go_at_the_cap_raises_cap_extra_and_queues_one_fix(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)
    before = len(fixes(flow))

    assert flow.go("go-2") == "queued-fix"

    row = flow.state()
    assert (row["workflow_state"], row["cap_extra"], row["approved_head"]) == ("fixing", 1, None)
    [fix] = fixes(flow)[before:]
    assert (fix["source_comment_id"], fix["head"], round_of(fix)) == ("go-2", HEADS[3], "4/4")


def test_a_replayed_go_queues_nothing_more(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)
    assert flow.go("go-2") == "queued-fix"
    after = len(jobs(flow.queue))
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'go-2'")

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "go-2") == "queued-fix"
    assert len(jobs(flow.queue)) == after
    assert flow.state()["cap_extra"] == 1


def test_a_second_handoff_at_the_raised_cap_takes_a_second_go(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)
    assert flow.go("go-2") == "queued-fix"
    flow.finish("claude", f"ready-{HEADS[5][:6]}")
    assert flow.ready(HEADS[5]) == "queued-review"
    flow.finish("codex", "approve-4/4")
    assert flow.approve("4/4") == "handed-off"
    before = len(fixes(flow))

    assert flow.go("go-3") == "queued-fix"

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 2)
    [fix] = fixes(flow)[before:]
    assert (fix["source_comment_id"], round_of(fix)) == ("go-3", "5/5")


def test_an_untrusted_go_changes_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)
    before = observed(flow)

    assert flow.go("go-2", author="stranger") == "rejected-go-author"

    assert observed(flow) == before


def test_with_the_app_switch_only_an_approver_go_grants_the_round(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)
    flow.dispatcher.approvals = Approvals(app_trust=True, logins=frozenset({"max-login"}))
    before = observed(flow)
    fixes_before = len(fixes(flow))

    assert flow.go("go-2") == "go-not-from-approver"

    assert observed(flow) == before
    assert [row["source_comment_id"] for row in jobs(flow.queue, "go-not-from-approver")] == [
        "go-2"
    ]

    assert flow.go("go-3", author="max-login") == "queued-fix"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
    assert [row["source_comment_id"] for row in fixes(flow)[fixes_before:]] == ["go-3"]


def test_a_planning_go_inside_the_cap_still_changes_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    before = observed(flow)

    assert flow.post("go-2", GO.format(state="planning"), "grok-login") == "go-after-handoff"

    assert observed(flow) == before
    assert flow.actions.linear_reopens == []


def test_approver_go_under_the_cap_reopens_with_one_fix(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    before = len(fixes(flow))

    assert flow.go("go-2") == "queued-fix"

    row = flow.state()
    assert (
        row["workflow_state"],
        row["cap_extra"],
        row["approved_head"],
        row["approve_comment_id"],
        row["review_round"],
        row["reopen_hold"],
    ) == ("fixing", 0, None, None, 2, "-")
    [fix] = fixes(flow)[before:]
    assert (fix["source_comment_id"], fix["head"], round_of(fix)) == ("go-2", HEADS[2], "3/3")
    assert flow.actions.linear_reopens == [TICKET]
    assert flow.actions.states[-1] == "fixing"
    claimed = flow.finish("claude")
    assert (claimed.request.source_comment_id, detail_of(claimed)["kind"]) == ("go-2", "reopen")
    assert detail_of(claimed)["instruction"] == _comment_link(flow.comments[-1])


def test_a_reopen_holds_the_linear_state_it_saw_and_a_stale_handoff_read_keeps_the_fix(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    with flow.queue.connect() as connection:
        connection.execute(
            "INSERT INTO linear_states (repo, ticket_id, state_name, state_type, read_at)"
            " VALUES (?, ?, 'Ready for Review', 'started', 1)",
            (REPO.lower(), TICKET),
        )

    assert flow.go("go-2") == "queued-fix"
    flow.dispatcher._linear_fresh = {
        (REPO.lower(), TICKET): LinearState("Ready for Review", "started", True)
    }
    flow.dispatcher._apply_linear_moves()

    row = flow.state()
    assert (row["workflow_state"], row["reopen_hold"]) == ("fixing", "Ready for Review")
    assert fixes(flow)[-1]["state"] == "queued"


def test_the_reopened_round_reenters_review_and_its_approve_hands_off_again(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    assert flow.go("go-2") == "queued-fix"
    flow.finish("claude", f"ready-{HEADS[5][:6]}")

    assert flow.ready(HEADS[5]) == "queued-review"

    review = jobs(flow.queue, "review")[-1]
    assert (review["head"], round_of(review)) == (HEADS[5], "3/3")
    assert (flow.state()["workflow_state"], flow.state()["reopen_hold"]) == ("review", "-")
    flow.finish("codex", "approve-3/3")
    assert flow.approve("3/3") == "handed-off"
    row = flow.state()
    assert (row["workflow_state"], row["reopen_hold"]) == ("handed-off", None)


def test_a_replayed_under_cap_go_queues_nothing_more(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    assert flow.go("go-2") == "queued-fix"
    after = len(jobs(flow.queue))
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'go-2'")

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "go-2") == "queued-fix"
    assert len(jobs(flow.queue)) == after
    assert (flow.state()["review_round"], flow.state()["cap_extra"]) == (2, 0)
    assert flow.actions.linear_reopens == [TICKET]


def test_an_untrusted_go_under_the_cap_changes_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    before = observed(flow)

    assert flow.go("go-2", author="stranger") == "rejected-go-author"

    assert observed(flow) == before
    assert flow.actions.linear_reopens == []


def test_with_the_app_switch_a_non_approver_go_under_the_cap_is_go_not_from_approver(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    flow.dispatcher.approvals = Approvals(app_trust=True, logins=frozenset({"max-login"}))
    before = observed(flow)

    assert flow.go("go-2") == "go-not-from-approver"

    assert observed(flow)[:4] == before[:4]
    assert flow.actions.linear_reopens == []
    assert flow.go("go-3", author="max-login") == "queued-fix"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 0)


def test_a_late_factory_verdict_after_handoff_stays_after_handoff(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    before = observed(flow)

    assert flow.revise("2/3") == "after-handoff"

    assert observed(flow) == before


def test_a_go_at_the_cap_without_a_state_changes_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)
    before = observed(flow)

    body = GO.format(state="building").replace(" state=building", "")
    assert flow.post("go-2", body, "grok-login") == "go-after-handoff"

    assert observed(flow) == before


def test_after_the_go_the_builders_ready_reenters_review_at_the_next_round(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(3)
    assert flow.ready(HEADS[5]) == "ready-after-cap"
    assert attention(flow.queue) == ["ready-after-cap"]

    assert flow.go("go-2") == "queued-fix"
    assert attention(flow.queue) == []
    flow.finish("claude", f"ready-{HEADS[6][:6]}")

    assert flow.ready(HEADS[6]) == "queued-review"

    review = jobs(flow.queue, "review")[-1]
    assert (review["head"], round_of(review)) == (HEADS[6], "4/4")
    assert flow.state()["workflow_state"] == "review"
    assert attention(flow.queue) == []


def test_a_go_replayed_after_a_failed_label_write_reopens_linear_once(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.factory = replace(flow.factory, tickets="linear")
    with flow.queue.connect() as connection:
        connection.execute("UPDATE tracked_tickets SET tracker = 'linear'")
    flow.hand_off_at(2)
    before = len(fixes(flow))
    original = flow.actions.set_state

    def fail_fixing(ticket, state):
        if state == "fixing":
            raise RuntimeError("transient label failure")
        original(ticket, state)

    flow.actions.set_state = fail_fixing
    with pytest.raises(RetryableStateError):
        flow.go("go-2")
    flow.actions.set_state = original
    flow.dispatcher.process(flow.snapshot())
    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "go-2") == "queued-fix"
    assert flow.actions.linear_reopens == [TICKET]
    assert [fix["source_comment_id"] for fix in fixes(flow)[before:]] == ["go-2"]
    assert flow.actions.states[-1] == "fixing"


def capped_after_a_reopen_then_handed_off(tmp_path: Path) -> Flow:
    """APPROVE round 1, reopen, REVISE rounds 2 and 3 to the cap, then a manual handoff."""
    flow = Flow(tmp_path)
    flow.dispatcher.approvals = Approvals(logins=frozenset({"grok-login"}))
    flow.hand_off_at(1)
    assert flow.go("go-reopen") == "queued-fix"
    for number in (2, 3):
        flow.finish("claude", f"ready-{HEADS[number][:6]}")
        assert flow.ready(HEADS[number]) == "queued-review"
        flow.finish("codex", f"revise-{number}/3")
        flow.revise(f"{number}/3")
    assert flow.state()["review_round"] == 3
    assert flow.post("manual-handoff", HANDOFF, "grok-login") == "handed-off"
    return flow


def test_a_go_after_a_capped_reopen_is_handed_off_grants_the_round_past_the_cap(
    tmp_path: Path,
) -> None:
    flow = capped_after_a_reopen_then_handed_off(tmp_path)

    assert flow.go("go-after-cap-handoff") == "queued-fix"

    row = flow.state()
    actual = (row["review_round"], row["cap_extra"], round_of(fixes(flow)[-1]))
    assert actual == (3, 1, "4/4")


def test_a_ready_after_a_capped_reopen_is_handed_off_waits_for_a_go(tmp_path: Path) -> None:
    flow = capped_after_a_reopen_then_handed_off(tmp_path)
    reviews = len(jobs(flow.queue, "review"))

    assert flow.ready(HEADS[4]) == "ready-after-cap"

    assert len(jobs(flow.queue, "review")) == reviews
    assert attention(flow.queue) == ["ready-after-cap"]


REVIEWER = "olivermillard"


def reviewed(flow: Flow, review_id: str, author: str = REVIEWER, **fields) -> None:
    flow.now[0] += 10
    flow.reviews.append(
        PullReview(
            id=review_id,
            author=author,
            state=fields.get("state", "CHANGES_REQUESTED"),
            submitted_at=fields.get("submitted_at", flow.now[0]),
            url=f"https://github.com/{REPO}/pull/{TICKET}#pullrequestreview-{review_id}",
        )
    )
    flow.dispatcher.process(flow.snapshot())


def review_outcomes(flow: Flow) -> list[tuple[str, str]]:
    with flow.queue.connect() as connection:
        return [
            (str(row["comment_id"]), str(row["outcome"]))
            for row in connection.execute(
                "SELECT comment_id, outcome FROM review_events"
                " WHERE comment_id LIKE 'review:%' ORDER BY rowid"
            )
        ]


def with_human_reviewer(flow: Flow) -> Flow:
    flow.factory = replace(flow.factory, human_reviewers=(REVIEWER,))
    return flow


def test_a_trusted_changes_requested_review_under_the_cap_reopens_like_a_go(
    tmp_path: Path,
) -> None:
    flow = with_human_reviewer(Flow(tmp_path))
    flow.hand_off_at(2)
    before = len(fixes(flow))

    reviewed(flow, "901", author="OliverMillard")

    row = flow.state()
    assert (
        row["workflow_state"],
        row["cap_extra"],
        row["approved_head"],
        row["review_round"],
        row["reopen_hold"],
    ) == ("fixing", 0, None, 2, "-")
    [fix] = fixes(flow)[before:]
    assert (fix["source_comment_id"], fix["head"], round_of(fix)) == ("review:901", HEADS[2], "3/3")
    assert review_outcomes(flow) == [("review:901", "queued-fix")]
    assert flow.actions.linear_reopens == [TICKET]
    assert flow.actions.states[-1] == "fixing"
    claimed = flow.finish("claude", f"ready-{HEADS[5][:6]}")
    assert (detail_of(claimed)["kind"], detail_of(claimed)["source"]) == ("reopen", "review")
    assert detail_of(claimed)["instruction"] == flow.reviews[-1].url
    assert flow.ready(HEADS[5]) == "queued-review"
    assert round_of(jobs(flow.queue, "review")[-1]) == "3/3"


def test_an_approvers_changes_requested_review_reopens(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    flow.dispatcher.approvals = Approvals(logins=frozenset({"max-login"}))

    reviewed(flow, "902", author="max-login")

    assert flow.state()["workflow_state"] == "fixing"
    assert [fix["source_comment_id"] for fix in fixes(flow)][-1] == "review:902"


@pytest.mark.parametrize(
    ("author", "state"),
    [
        ("stranger", "CHANGES_REQUESTED"),
        ("grok-login", "CHANGES_REQUESTED"),
        (REVIEWER, "APPROVED"),
        (REVIEWER, "COMMENTED"),
        (REVIEWER, "DISMISSED"),
    ],
)
def test_an_untrusted_or_other_review_changes_nothing(
    tmp_path: Path, author: str, state: str
) -> None:
    flow = with_human_reviewer(Flow(tmp_path))
    flow.hand_off_at(2)
    before = observed(flow)

    reviewed(flow, "903", author=author, state=state)

    assert observed(flow) == before
    assert review_outcomes(flow) == []
    assert flow.actions.linear_reopens == []


def test_a_changes_requested_review_from_before_the_approval_changes_nothing(
    tmp_path: Path,
) -> None:
    flow = with_human_reviewer(Flow(tmp_path))
    flow.hand_off_at(2)
    approved_at = next(item for item in flow.comments if item.id == "approve-2/3").created_at
    before = observed(flow)

    reviewed(flow, "904", submitted_at=approved_at - 1)

    assert observed(flow) == before
    assert review_outcomes(flow) == []


def test_a_changes_requested_review_on_a_ticket_not_handed_off_changes_nothing(
    tmp_path: Path,
) -> None:
    flow = with_human_reviewer(Flow(tmp_path))
    assert flow.go() == "queued-build"
    before = observed(flow)

    reviewed(flow, "905")

    assert observed(flow) == before
    assert review_outcomes(flow) == []


def test_a_trusted_changes_requested_review_at_the_cap_flags_once_and_queues_nothing(
    tmp_path: Path,
) -> None:
    flow = with_human_reviewer(Flow(tmp_path))
    flow.hand_off_at(3)
    before = len(jobs(flow.queue))
    round_before = flow.state()["review_round"]

    reviewed(flow, "906")
    reviewed(flow, "907")

    row = flow.state()
    assert (row["workflow_state"], row["cap_extra"], row["review_round"]) == ("handed-off", 0, round_before)
    assert len(jobs(flow.queue)) == before
    assert attention(flow.queue) == ["changes-requested-after-cap"]
    assert review_outcomes(flow) == [
        ("review:906", "changes-requested-after-cap"),
        ("review:907", "changes-requested-after-cap"),
    ]
    assert flow.actions.linear_reopens == []


def test_a_replayed_review_at_the_cap_raises_no_second_flag(tmp_path: Path) -> None:
    flow = with_human_reviewer(Flow(tmp_path))
    flow.hand_off_at(3)
    reviewed(flow, "908")
    flow.dispatcher.clear_attention(REPO, TICKET, "changes-requested-after-cap")
    flow.dispatcher.process(flow.snapshot())
    assert attention(flow.queue) == []

    flow.dispatcher.process(flow.snapshot())

    assert attention(flow.queue) == []
    assert review_outcomes(flow) == [("review:908", "changes-requested-after-cap")]


def test_a_replayed_review_queues_nothing_more_and_keeps_the_round(tmp_path: Path) -> None:
    flow = with_human_reviewer(Flow(tmp_path))
    flow.hand_off_at(2)
    reviewed(flow, "909")
    after = len(jobs(flow.queue))

    flow.dispatcher.process(flow.snapshot())
    with flow.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = 'handed-off', reopen_hold = NULL"
            " WHERE repo = ? AND ticket_id = ?",
            (REPO, TICKET),
        )
    flow.dispatcher.process(flow.snapshot())

    assert len(jobs(flow.queue)) == after
    assert (flow.state()["review_round"], flow.state()["cap_extra"]) == (2, 0)
    assert review_outcomes(flow) == [("review:909", "queued-fix")]
    assert flow.actions.linear_reopens == [TICKET]
