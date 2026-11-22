from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from lib.dispatcher import Dispatcher
from tests.test_dispatcher import (
    GO,
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    comment,
    factory,
    job_count,
    ticket,
)
from tests.test_feeder import issue, make_feeder, rows
from tests.test_feeder_gates import HOME, OPEN_PULL, ref
from tests.test_feeder_retire import _fed_holder, _state
from tests.test_followup_findings import FollowupActions
from tests.test_plan_size import SPLIT_BODY, brief, estimated, plan_header

PARENT = 1
LONG_AGO = 10_000.0 - 86_400


def part(number: int, *, state: str = "closed", reason: str | None = "completed") -> dict:
    return {
        "title": f"Part {number}",
        "body": f"Split from #{PARENT} (plan https://github.com/{HOME}/pull/501#issuecomment-500).\n",
        "state": state, "reason": reason, "closed_at": LONG_AGO, "labels": (),
    }


def file_parent(github) -> dict:
    github.filed[(HOME, PARENT)] = {
        "title": "Parent", "body": "x\n", "state": "open",
        "labels": ("factory:ready", "orch:state/abandoned"),
    }
    return github.filed[(HOME, PARENT)]


def searches(github, since: int = 0) -> int:
    return sum(call.startswith(f"split-children:{HOME}#{PARENT}") for call in github.calls[since:])


def test_a_retired_parent_whose_parts_closed_before_the_cursor_closes(tmp_path: Path) -> None:
    feeder, _, github = _fed_holder(tmp_path)
    parent = file_parent(github)
    github.filed[(HOME, 11)] = part(11)
    github.filed[(HOME, 12)] = part(12)
    _state(tmp_path, 501, "abandoned", split=True)

    feeder.feed_once()

    assert rows(tmp_path / "orch.db")["1"]["state"] == "split-parent"
    assert (parent["state"], parent["reason"]) == ("closed", "completed")
    assert "factory:ready" not in parent["labels"]
    assert "orch:state/abandoned" not in parent["labels"]
    [closing] = [item.body for item in github.posted[(HOME, PARENT)] if "orch-split-closed" in item.body]
    assert "#11" in closing and "#12" in closing


def test_a_parent_retired_before_the_upgrade_closes_on_the_next_poll(tmp_path: Path) -> None:
    feeder, queue, github = _fed_holder(tmp_path)
    parent = file_parent(github)
    github.filed[(HOME, 11)] = part(11)
    with queue.connect() as connection:
        connection.execute(
            "UPDATE feeder_items SET state = 'split-parent', released = 1 WHERE issue = '1'"
        )

    feeder.feed_once()

    assert parent["state"] == "closed"


def test_a_retired_parent_with_an_open_part_is_checked_once_then_waits(tmp_path: Path) -> None:
    feeder, _, github = _fed_holder(tmp_path)
    fed = len(github.calls)
    parent = file_parent(github)
    github.filed[(HOME, 11)] = part(11)
    github.filed[(HOME, 12)] = part(12, state="open", reason=None)
    _state(tmp_path, 501, "abandoned", split=True)

    feeder.feed_once()
    feeder.feed_once()

    assert parent["state"] == "open"
    assert searches(github, fed) == 1


def test_a_retired_parent_with_no_findable_parts_stays_open_and_is_not_searched_again(
    tmp_path: Path,
) -> None:
    feeder, queue, github = _fed_holder(tmp_path)
    fed = len(github.calls)
    parent = file_parent(github)
    _state(tmp_path, 501, "abandoned", split=True)
    clock = [10_000.0]
    queue.clock = lambda: clock[0]

    for _ in range(4):
        clock[0] += 600
        feeder.feed_once()

    assert parent["state"] == "open"
    assert [item for item in github.posted.get((HOME, PARENT), []) if "orch-split-closed" in item.body] == []
    assert searches(github, fed) == 1


def test_feed_plan_auto_split_then_feed_once_frees_the_parent_and_closes_it(tmp_path: Path) -> None:
    clock = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, box=3, now=clock)
    actions = FollowupActions()
    dispatcher = Dispatcher(
        queue.path, queue, trusted_logins=TRUSTED_LOGINS,
        review_actions=actions, grok_delivery=FakeGrokDelivery(),
    )
    github.issues[HOME] = [issue(1, created_at=1.0, body="Touches: lib/x.py"),
                           issue(2, created_at=2.0, body="Touches: lib/x.py")]
    parent = file_parent(github)
    github.statuses = {ref(501): OPEN_PULL}
    (fed,) = feeder.feed_once().fed
    assert fed.ticket_id == "501"

    thread = []
    sized = replace(factory(), plan_max_lines=400, plan_max_criteria=5)

    def say(comment_id: str, body: str, author: str) -> str:
        clock[0] += 10
        thread.append(comment(comment_id, body, author=author, created_at=clock[0]))
        snapshot = ticket(*thread, repo=HOME, ticket_id="501", factory_config=sized)
        dispatcher.process(replace(snapshot, intake_label="factory:ready", source_issue=f"#{PARENT}"))
        with queue.connect() as connection:
            [outcome] = connection.execute(
                "SELECT outcome FROM handled_comments WHERE repo = ? AND ticket_id = '501' AND comment_id = ?",
                (HOME, comment_id),
            ).fetchone()
        return outcome

    thread.append(comment("brief-1", brief(2), author="grok-login", created_at=clock[0]))
    assert say("1", GO.format(state="planning"), "grok-login") == "queued-plan"
    plan = queue.claim("claude")
    assert plan is not None and plan.request.action == "plan"
    queue.finish(plan.id, owner="claude", handoff_comment_id="500")
    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db")["1"]["state"] == "done"
    assert rows(tmp_path / "orch.db")["2"]["state"] == "waiting-blocked"

    assert say("500", plan_header("ac=met") + estimated(300, SPLIT_BODY), "claude-login") == "auto-split"
    assert list(actions.issues) == [300, 301]
    for number, (title, body, labels) in actions.issues.items():
        github.filed[(HOME, number)] = {"title": title, "body": body, "state": "open", "labels": labels}

    clock[0] += 60
    report = feeder.feed_once()

    row = rows(tmp_path / "orch.db")["1"]
    assert (row["state"], row["released"]) == ("split-parent", 1)
    assert [item.ticket_id for item in report.fed] == ["502"]
    assert job_count(queue, "build") == 0
    assert parent["state"] == "open"

    for number in (300, 301):
        clock[0] += 60
        github.filed[(HOME, number)].update(state="closed", reason="completed", closed_at=clock[0])
    clock[0] += 60
    feeder.feed_once()

    assert (parent["state"], parent["reason"]) == ("closed", "completed")
    assert job_count(queue, "build") == 0
