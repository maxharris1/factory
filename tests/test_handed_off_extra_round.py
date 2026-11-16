"""#118: an approver's go on a handed-off ticket at the cap grants one round.

The go raises cap_extra by 1, sets fixing and queues one fix keyed on the go,
and the builder's ready at a new head re-enters review at the next round.
Inside the cap a go still changes nothing (#108).
"""

from __future__ import annotations

from pathlib import Path

from lib.orch_yml import Approvals
from tests.test_dispatcher import GO
from tests.test_lifecycle import HEADS, Flow, attention, jobs, outcome, round_of


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


def test_a_go_inside_the_cap_still_changes_nothing(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    before = observed(flow)

    assert flow.go("go-2") == "go-after-handoff"

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
