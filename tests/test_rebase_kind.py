"""#1434: a trusted `kind=rebase` queues one rebase-only fix that spends no round."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import brief_kind
from lib.brief.common import detail_of
from lib.orch_yml import Approvals
from tests.test_lifecycle import HEADS, Flow, jobs, outcome

REBASE = "<!-- orch:v1 kind=rebase from=grok-bot role=orchestrator -->\nRebase onto the base.\n"
BUILDER_REBASE = "<!-- orch:v1 kind=rebase from=claude-bot role=builder -->\nRebase.\n"


def in_review(tmp_path: Path) -> Flow:
    flow = Flow(tmp_path)
    assert flow.go() == "queued-build"
    flow.finish("claude", f"ready-{HEADS[1][:6]}")
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex", "review-held")
    assert flow.state()["workflow_state"] == "review"
    return flow


def set_state(flow: Flow, state: str, review_round: int) -> None:
    with flow.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = ?, review_round = ?",
            (state, review_round),
        )


def fixes(flow: Flow) -> list:
    return jobs(flow.queue, "fix")


def counts(flow: Flow) -> tuple:
    row = flow.state()
    return row["cap_extra"], row["review_round"]


def decisions(flow: Flow) -> list[tuple]:
    with flow.queue.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT answer_id, question_id, question, choice, answerer FROM decisions"
            )
        ]


def test_trusted_orchestrator_rebase_queues_one_rebase_fix(tmp_path: Path) -> None:
    flow = in_review(tmp_path)
    before, spent = len(fixes(flow)), counts(flow)

    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"

    [fix] = fixes(flow)[before:]
    detail = json.loads(fix["detail"])
    assert (fix["head"], fix["source_comment_id"]) == (HEADS[1], "rebase-42")
    assert detail["kind"] == "rebase"
    assert detail["rebase"] == {"from_state": "review", "from_head": HEADS[1], "round": spent[1]}
    assert flow.state()["workflow_state"] == "fixing"
    assert counts(flow) == spent
    assert flow.actions.states[-1] == "fixing"
    assert brief_kind(flow.finish("claude")) == "rebase"


@pytest.mark.parametrize(
    ("state", "review_round"),
    [("review", 1), ("fixing", 2), ("awaiting-ok", 2), ("capped", 3), ("capped", 5)],
)
def test_rebase_is_accepted_from_every_listed_state_at_any_round(
    tmp_path: Path, state: str, review_round: int
) -> None:
    flow = in_review(tmp_path)
    set_state(flow, state, review_round)
    before, spent = len(fixes(flow)), counts(flow)

    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"

    [fix] = fixes(flow)[before:]
    assert detail_of_row(fix)["rebase"] == {
        "from_state": state,
        "from_head": HEADS[1],
        "round": review_round,
    }
    assert (flow.state()["workflow_state"], counts(flow)) == ("fixing", spent)
    with flow.queue.connect() as connection:
        source = connection.execute(
            "SELECT source FROM ticket_transitions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()["source"]
    assert source == ("set-state" if state == "fixing" else "rebase-queued")


def detail_of_row(row) -> dict:
    return json.loads(row["detail"])


@pytest.mark.parametrize(
    ("author", "body"),
    [("stranger-login", REBASE), ("claude-login", BUILDER_REBASE)],
)
def test_untrusted_rebase_is_refused_with_no_job(tmp_path: Path, author: str, body: str) -> None:
    flow = in_review(tmp_path)
    before = len(jobs(flow.queue))

    assert flow.post("rebase-42", body, author) == "rejected-rebase-author"

    assert [row for row in jobs(flow.queue) if row["recipient"] != "grok"] == [
        row for row in jobs(flow.queue)[:before] if row["recipient"] != "grok"
    ]
    assert flow.state()["workflow_state"] == "review"
    assert decisions(flow) == []


def test_approver_rebase_queues_with_the_app_switch_on(tmp_path: Path) -> None:
    flow = in_review(tmp_path)
    flow.dispatcher.approvals = Approvals(app_trust=True, logins=frozenset({"max-login"}))
    before = len(fixes(flow))

    assert flow.post("rebase-42", REBASE, "max-login") == "queued-rebase"

    [fix] = fixes(flow)[before:]
    assert detail_of_row(fix)["kind"] == "rebase"


def test_replayed_rebase_queues_once(tmp_path: Path) -> None:
    flow = in_review(tmp_path)
    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"
    after = len(jobs(flow.queue))
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'rebase-42'")

    flow.dispatcher.process(flow.snapshot())

    assert outcome(flow.queue, "rebase-42") == "queued-rebase"
    assert len(jobs(flow.queue)) == after
    assert len(decisions(flow)) == 1


def test_rebase_with_a_job_active_is_refused(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    assert flow.go() == "queued-build"
    flow.finish("claude", f"ready-{HEADS[1][:6]}")
    assert flow.ready(HEADS[1]) == "queued-review"
    before = len(fixes(flow))

    assert flow.post("rebase-42", REBASE, "grok-login") == "rejected-rebase-job-active"

    assert len(fixes(flow)) == before
    assert flow.state()["workflow_state"] == "review"


def test_rebase_in_building_is_refused(tmp_path: Path) -> None:
    flow = in_review(tmp_path)
    set_state(flow, "building", 0)

    assert flow.post("rebase-42", REBASE, "grok-login") == "rejected-rebase-state"

    assert flow.state()["workflow_state"] == "building"


def test_rebase_fix_on_a_ticket_with_a_range_diff_review_does_not_raise(tmp_path: Path) -> None:
    flow = in_review(tmp_path)
    scope = {"last_reviewed_head": HEADS[0], "reviewed_base": HEADS[4], "base": HEADS[5]}
    with flow.queue.connect() as connection:
        review = connection.execute(
            "SELECT id, detail FROM jobs WHERE action = 'review' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        connection.execute(
            "UPDATE jobs SET detail = ? WHERE id = ?",
            (json.dumps({**json.loads(review["detail"]), "rebase": scope}), review["id"]),
        )

    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"

    assert detail_of_row(fixes(flow)[-1])["rebase"] == {
        "from_state": "review",
        "from_head": HEADS[1],
        "round": 0,
    }


def test_rebase_on_handed_off_under_cap_is_not_a_reopen(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.hand_off_at(2)
    before, spent = len(fixes(flow)), counts(flow)

    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"

    row = flow.state()
    assert (row["workflow_state"], row["reopen_hold"]) == ("fixing", None)
    assert counts(flow) == spent
    assert flow.actions.linear_reopens == []
    [fix] = fixes(flow)[before:]
    assert fix["head"] == HEADS[2]
    claimed = flow.finish("claude")
    assert detail_of(claimed)["kind"] == "rebase"
    assert detail_of(claimed)["rebase"] == {
        "from_state": "handed-off",
        "from_head": HEADS[2],
        "round": spent[1],
    }


def test_rebase_records_the_trigger_decision(tmp_path: Path) -> None:
    flow = in_review(tmp_path)

    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"

    assert decisions(flow) == [
        ("rebase-42", "rebase-42", "rebase:rebase-42", "Rebase onto origin/main queued", "grok-login")
    ]


def test_the_decision_names_the_project_base(tmp_path: Path) -> None:
    flow = in_review(tmp_path)
    flow.factory = replace(flow.factory, base_branch="dev")

    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"

    assert [row[3] for row in decisions(flow)] == ["Rebase onto origin/dev queued"]
