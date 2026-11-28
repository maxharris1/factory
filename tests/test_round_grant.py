from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from lib.dispatch.types import Comment, RoundGrant
from lib.dispatch.verdict.repeat import StopRecord
from lib.rule_engine import Grant, Ruling, finding_id, go_header
from tests.qa_support import Harness
from tests.test_approver_credential import MAX, AppFlow
from tests.test_cold_spill import spill_while_abandoned
from tests.test_dispatcher import job_count
from tests.test_repeat import capped_stop, decisions, repeat_stop
from tests.test_qa_gate_table import blocking
from tests.test_repeat_block import F1, F2, SizedFlow, next_review, repeat_wakes, rounds
from tests.test_t3_convergence import _qa_round
from tests.test_unsatisfiable_criterion import revise


def verdict(flow: AppFlow, comment_id: str) -> Comment:
    return next(item for item in flow.comments if item.id == comment_id)


def fix_keys(flow: AppFlow) -> list[tuple[str, str]]:
    with flow.queue.connect() as connection:
        return [
            (row["source_comment_id"], row["dedupe_key"])
            for row in connection.execute("SELECT source_comment_id, dedupe_key FROM jobs WHERE action = 'fix'")
        ]


def engine_granted(flow: AppFlow, site: str, stop: Comment, *ids: str) -> None:
    ruling = Ruling("first-cap-grant", Grant(str(go_header(4, 4))), ())
    with flow.queue.connect() as connection:
        flow.dispatcher.review.log_ruling(
            connection, flow.snapshot(), site, stop.id, ruling, tuple(finding_id(item) for item in ids)
        )


def drop_repeat_wake(flow: AppFlow, stop: Comment) -> None:
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM jobs WHERE dedupe_key LIKE ?", (f"t3-repeat:%:{stop.id}",))


def test_an_engine_cap_grant_replayed_twice_queues_one_fix(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)
    fixes = job_count(flow.queue, "fix")
    grant = RoundGrant.engine(verdict(flow, "revise-3"))

    first = flow.dispatcher._apply_round_grant(flow.snapshot(), grant)
    again = flow.dispatcher._apply_round_grant(flow.snapshot(), grant)

    assert (first, again) == (("queued-fix", True, False), ("queued-fix", False, False))
    assert job_count(flow.queue, "fix") == fixes + 1
    assert fix_keys(flow)[-1] == ("revise-3", "t1:owner/repo\x1f42:fix:engine:revise-3")
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
    assert decisions(flow) == []


def test_an_engine_repeat_grant_at_the_cap_replayed_twice_resumes_one_fix(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    assert rounds(flow, F2, F2.replace("f-2", "f-3"), F1, last_extra=" repeat=f-1") == "waiting-max-repeat"
    fixes = job_count(flow.queue, "fix")
    stop = verdict(flow, "revise-3")

    first = flow.dispatcher._resume_paused(flow.snapshot(), stop, grant=RoundGrant.engine(stop))
    again = flow.dispatcher._resume_paused(flow.snapshot(), stop, grant=RoundGrant.engine(stop))

    assert (first, again) == ((True, "resumed-fix"), (True, "resumed-fix"))
    assert job_count(flow.queue, "fix") == fixes + 1
    assert fix_keys(flow)[-1] == ("revise-3", "t5:owner/repo\x1f42:revise-3:engine:revise-3")
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
    assert decisions(flow) == []


def test_stop_history_names_an_approver_release_and_an_open_stop(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)
    assert flow.dispatcher._stop_history(flow.snapshot()) == (
        StopRecord("revise-3", "capped", frozenset({"f-3"}), None),
    )

    assert flow.post("go-max", "/go", MAX) == "queued-fix"

    assert flow.dispatcher._stop_history(flow.snapshot()) == (
        StopRecord("revise-3", "capped", frozenset({"f-3"}), "approver"),
    )


def test_stop_history_reads_a_repeat_stops_finding_from_its_wake(tmp_path: Path) -> None:
    flow = repeat_stop(tmp_path)

    assert flow.post("go-max", "/go", MAX) == "resumed-fix"

    assert flow.dispatcher._stop_history(flow.snapshot()) == (
        StopRecord("revise-2", "repeat", frozenset({"f-1"}), "approver"),
    )


def test_a_disputed_repeat_stop_reads_its_finding_from_a_spilled_wake(tmp_path: Path) -> None:
    flow = repeat_stop(tmp_path)
    assert flow.post("go-max", "/go", MAX) == "resumed-fix"
    with flow.queue.connect() as connection:
        wake = connection.execute("SELECT id, detail FROM jobs WHERE dedupe_key LIKE 't3-repeat:%'").fetchone()
        detail = json.loads(wake["detail"])
        detail["detail"] += ", " + "x" * 1100
        connection.execute("UPDATE jobs SET detail = ? WHERE id = ?", (json.dumps(detail), wake["id"]))
    spill_while_abandoned(flow.queue, wake["id"])

    assert flow.dispatcher._stop_disputed(flow.snapshot(), verdict(flow, "revise-2"), "repeat") == frozenset({"f-1"})


def test_stop_history_counts_an_engine_grant_with_no_grok_job(tmp_path: Path) -> None:
    flow = SizedFlow(tmp_path, changed_lines=None)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)
    assert rounds(flow, F1, F1) == "waiting-max-repeat"
    stop = verdict(flow, "revise-2")
    drop_repeat_wake(flow, stop)
    engine_granted(flow, "repeat", stop, "f-1")

    assert flow.dispatcher._resume_paused(flow.snapshot(), stop, grant=RoundGrant.engine(stop)) == (
        True,
        "resumed-fix",
    )

    assert flow.dispatcher._stop_history(flow.snapshot()) == (
        StopRecord("revise-2", "repeat", frozenset({"f-1"}), "engine"),
    )
    next_review(flow, 2)
    assert revise(flow, "3/3", "revise-3", body=F1) == "waiting-max-repeat"
    assert repeat_wakes(flow)[-1].endswith("revise-3, stops=2, for=max")


def test_stop_history_counts_an_engine_cap_grant(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)
    stop = verdict(flow, "revise-3")
    engine_granted(flow, "cap", stop, "f-3")

    flow.dispatcher._apply_round_grant(flow.snapshot(), RoundGrant.engine(stop))

    assert flow.dispatcher._stop_history(flow.snapshot()) == (
        StopRecord("revise-3", "capped", frozenset({"f-3"}), "engine"),
    )


def engine_released_repeat(flow: AppFlow) -> RoundGrant:
    assert rounds(flow, F1, F1) == "waiting-max-repeat"
    grant = RoundGrant.engine(verdict(flow, "revise-2"))
    assert flow.dispatcher._resume_paused(flow.snapshot(), grant.trigger, grant=grant) == (True, "resumed-fix")
    next_review(flow, 2)
    return grant


def pause(flow: AppFlow) -> tuple:
    state = flow.state()
    return job_count(flow.queue, "fix"), state["workflow_state"], state["cap_extra"], state["paused_question_id"]


def test_a_consumed_engine_grant_does_not_release_a_later_repeat_stop(tmp_path: Path) -> None:
    flow = SizedFlow(tmp_path, changed_lines=None)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)
    grant = engine_released_repeat(flow)
    assert revise(flow, "3/3", "revise-3", body=F1) == "waiting-max-repeat"
    parked = pause(flow)

    assert flow.dispatcher._resume_paused(flow.snapshot(), grant.trigger, grant=grant) is None
    assert pause(flow) == parked


def test_a_grant_consumed_on_a_repeat_stop_does_not_lift_a_later_cap(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, auto_extra_rounds=0)
    grant = engine_released_repeat(flow)
    assert revise(flow, "3/3", "revise-3", body="- [blocking] f-3: the exit code is 0.\n") == "capped-review"
    capped = pause(flow)

    assert flow.dispatcher._apply_round_grant(flow.snapshot(), grant) == ("resumed-fix", False, False)
    assert pause(flow) == capped


def test_an_engine_grant_for_an_older_verdict_does_not_lift_the_cap(tmp_path: Path) -> None:
    flow = capped_stop(tmp_path)
    capped = pause(flow)

    assert flow.dispatcher._apply_round_grant(flow.snapshot(), RoundGrant.engine(verdict(flow, "revise-2"))) is None
    assert pause(flow) == capped


def test_stop_history_keeps_a_capped_stops_qa_blockers(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    run.thread.base = replace(run.thread.base, factory=replace(run.thread.base.factory, rounds_high=1))
    _qa_round(run, 1, ["qa-1"], cap=1)
    assert run.rows("SELECT comment_id, outcome, qa_failed FROM review_events") == [
        ("v1", "capped-review", '["qa-1"]')
    ]

    assert run.dispatcher._stop_history(run.thread.snapshot()) == (
        StopRecord("v1", "capped", frozenset({"qa-1"}), None),
    )
