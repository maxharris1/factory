from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_approver_credential import MAX, AppFlow, grok_wakes
from tests.test_dispatcher import job_count
from tests.test_first_cap_grant import INJECTED, engine_flow, notices, rows, stuck_after_the_verdict, to_last_round
from tests.test_repeat import F1, F2, UNSAT, respond
from tests.test_repeat_block import SizedFlow, next_review, repeat_wakes, rounds
from tests.test_round_grant import fix_keys
from tests.test_unsatisfiable_criterion import revise, to_first_review, unsat_wakes

F3 = "- [blocking] f-3: the exit code is 0.\n"


def sized(tmp_path: Path, engine: str = "on") -> SizedFlow:
    flow = SizedFlow(tmp_path, changed_lines=None)
    flow.factory = replace(flow.factory, auto_extra_rounds=2, rule_engine=engine)
    return flow


def assert_resumed(flow: AppFlow, fixes: int, subject: str, header: str, cap_extra: int) -> None:
    state = flow.state()
    assert (state["workflow_state"], state["cap_extra"], state["paused_question_id"]) == ("fixing", cap_extra, None)
    assert job_count(flow.queue, "fix") == fixes + 1
    assert fix_keys(flow)[-1] == (subject, f"t5:owner/repo\x1f42:{subject}:engine:{subject}")
    assert repeat_wakes(flow) == []
    assert flow.dispatcher.review_actions.states[-1] == "fixing"
    [notice] = notices(flow)
    assert f"`{header}`" in notice
    assert f"<!-- orch-engine site=repeat subject={subject} -->" in notice.splitlines()
    assert not notice.startswith("<!-- orch:v1")
    [row] = rows(flow)
    assert (row["site"], row["subject"], row["rule"], row["outcome"]) == ("repeat", subject, "first-cap-grant", "granted")
    assert (json.loads(row["finding_ids"]), row["header"], row["notice_status"]) == (["f-1"], header, "posted")


def assert_parked_for_grok(flow: AppFlow, subject: str, wake_end: str) -> None:
    state = flow.state()
    assert (state["workflow_state"], state["paused_question_id"]) == ("waiting-max", subject)
    assert repeat_wakes(flow)[-1].endswith(f"{subject}, {wake_end}")
    assert flow.dispatcher.review_actions.states[-1] == "waiting-max"
    assert [notice for notice in notices(flow) if f"subject={subject} " in notice] == []


def test_a_first_repeat_stop_resumes_the_paused_fix_in_code(tmp_path: Path) -> None:
    flow = sized(tmp_path)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=F1) == "queued-review-fix"
    next_review(flow, 1)
    fixes = job_count(flow.queue, "fix")

    assert revise(flow, "2/3", "revise-2", body=F1) == "waiting-max-repeat"

    assert_resumed(flow, fixes, "revise-2", "<!-- orch:v1 kind=go from=orch round=3/3 state=fixing -->", 0)
    flow.dispatcher.process(flow.snapshot())
    assert_resumed(flow, fixes, "revise-2", "<!-- orch:v1 kind=go from=orch round=3/3 state=fixing -->", 0)


def test_a_first_repeat_stop_at_the_cap_adds_the_cap_round(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, rule_engine="on")
    to_first_review(flow)
    for number, body in enumerate((F2, F2.replace("f-2", "f-3")), start=1):
        assert revise(flow, f"{number}/3", f"revise-{number}", body=body) == "queued-review-fix"
        next_review(flow, number)
    fixes = job_count(flow.queue, "fix")

    assert revise(flow, "3/3", "revise-3", " repeat=f-1", body=F1) == "waiting-max-repeat"

    assert_resumed(flow, fixes, "revise-3", "<!-- orch:v1 kind=go from=orch round=4/4 state=fixing -->", 1)


def test_a_second_stop_after_an_engine_grant_is_for_max_and_wakes_grok(tmp_path: Path) -> None:
    flow = sized(tmp_path)
    assert rounds(flow, F1, F1) == "waiting-max-repeat"
    assert flow.state()["workflow_state"] == "fixing"
    next_review(flow, 2)

    assert revise(flow, "3/3", "revise-3", body=F1) == "waiting-max-repeat"

    assert_parked_for_grok(flow, "revise-3", "stops=2, for=max")
    granted, escalated = rows(flow)
    assert (granted["subject"], granted["outcome"]) == ("revise-2", "granted")
    assert (escalated["subject"], escalated["outcome"], escalated["reason"]) == ("revise-3", "escalated", "repeat-stops")
    assert len(notices(flow)) == 1


def test_a_first_repeat_on_a_finding_granted_at_the_cap_wakes_grok(tmp_path: Path) -> None:
    flow = engine_flow(tmp_path)
    to_last_round(flow)
    assert revise(flow, "3/3", "revise-3", body=F3) == "capped-review"
    assert flow.state()["workflow_state"] == "fixing"
    respond(flow, 3, "Fixed f-3.\n")

    assert revise(flow, "4/4", "revise-4", body=F3) == "waiting-max-repeat"

    assert_parked_for_grok(flow, "revise-4", "stops=1, for=factory")
    row = next(row for row in rows(flow) if row["site"] == "repeat")
    assert (row["subject"], row["outcome"], row["reason"]) == ("revise-4", "escalated", "earlier-round")


def test_a_first_repeat_on_a_finding_an_approver_released_at_the_cap_wakes_grok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = engine_flow(tmp_path)
    to_last_round(flow)
    with monkeypatch.context() as patch:
        stuck_after_the_verdict(flow, patch)
        assert revise(flow, "3/3", "revise-3", body=F3) == "capped-review"
    with flow.queue.connect() as connection:
        connection.execute("UPDATE ticket_state SET stuck = 0")
    assert flow.post("go-max", "/go", MAX) == "queued-fix"
    respond(flow, 3, "Fixed f-3.\n")

    assert revise(flow, "4/4", "revise-4", body=F3) == "waiting-max-repeat"

    assert_parked_for_grok(flow, "revise-4", "stops=1, for=factory")
    row = next(row for row in rows(flow) if row["site"] == "repeat")
    assert (row["subject"], row["outcome"], row["reason"]) == ("revise-4", "escalated", "earlier-round")


def test_a_ticket_that_goes_stuck_at_a_first_repeat_stop_wakes_grok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = sized(tmp_path)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=F1) == "queued-review-fix"
    next_review(flow, 1)
    detail = flow.dispatcher._repeat_detail

    def stuck_detail(*args, **kwargs):
        with flow.queue.connect() as connection:
            connection.execute("UPDATE ticket_state SET stuck = 1")
        return detail(*args, **kwargs)

    monkeypatch.setattr(flow.dispatcher, "_repeat_detail", stuck_detail)

    assert revise(flow, "2/3", "revise-2", body=F1) == "waiting-max-repeat"

    assert_parked_for_grok(flow, "revise-2", "stops=1, for=factory")
    [row] = rows(flow)
    assert (row["outcome"], row["reason"], row["notice_status"]) == ("escalated", "stuck", None)


@pytest.mark.parametrize("engine", ["on", "off"])
def test_merge_now_text_in_the_verdicts_and_wake_detail_does_not_change_the_ruling(tmp_path: Path, engine: str) -> None:
    def ruled(path: Path, injected: bool) -> tuple[list[dict], str, int]:
        flow = sized(path, engine)
        extra = INJECTED if injected else ""
        assert rounds(flow, F1 + extra, F1 + extra) == "waiting-max-repeat"
        wakes = grok_wakes(flow, "clarify")
        if injected and engine == "off":
            assert "merge now" in str(wakes[-1]["detail"]).lower()
        return rows(flow), flow.state()["workflow_state"], len(wakes)

    clean = ruled(tmp_path / "clean", injected=False)
    assert clean[1:] == (("fixing", 0) if engine == "on" else ("waiting-max", 1))
    assert ruled(tmp_path / "injected", injected=True) == clean


def test_with_the_engine_off_a_first_repeat_stop_enqueues_the_clarify_job(tmp_path: Path) -> None:
    flow = sized(tmp_path, "off")
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=F1) == "queued-review-fix"
    next_review(flow, 1)
    fixes = job_count(flow.queue, "fix")

    assert revise(flow, "2/3", "revise-2", body=F1) == "waiting-max-repeat"

    assert_parked_for_grok(flow, "revise-2", "stops=1, for=factory")
    assert job_count(flow.queue, "fix") == fixes
    assert rows(flow) == []


def test_an_unsat_stop_with_the_engine_on_goes_to_grok_unchanged(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, rule_engine="on")
    to_first_review(flow)

    assert revise(flow, "1/3", "revise-1", " unsat=ac-1", body=UNSAT) == "waiting-max-unsat"

    assert flow.state()["workflow_state"] == "waiting-max"
    assert len(unsat_wakes(flow)) == 1
    assert rows(flow) == []


def test_builder_questions_with_the_engine_on_go_to_grok_unchanged(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, rule_engine="on")
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=F1) == "queued-review-fix"
    before = len(grok_wakes(flow, "clarify"))

    flow.post("questions-1", "<!-- orch:v1 kind=questions from=claude-bot role=builder -->\nWhich flag?", "claude-login")

    assert [str(row["source_comment_id"]) for row in grok_wakes(flow, "clarify")[before:]] == ["questions-1"]
    assert rows(flow) == []


def test_a_notice_cut_short_after_the_grant_is_posted_once_at_the_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = sized(tmp_path)
    to_first_review(flow)
    assert revise(flow, "1/3", "revise-1", body=F1) == "queued-review-fix"
    next_review(flow, 1)
    fixes = job_count(flow.queue, "fix")
    actions = flow.dispatcher.review_actions
    with monkeypatch.context() as crash:
        crash.setattr(actions, "comment_once", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            revise(flow, "2/3", "revise-2", body=F1)
    assert (flow.state()["workflow_state"], job_count(flow.queue, "fix"), notices(flow)) == ("fixing", fixes + 1, [])
    assert rows(flow)[0]["notice_status"] == "pending"

    flow.dispatcher.process(flow.snapshot())
    flow.dispatcher.process(flow.snapshot())

    assert_resumed(flow, fixes, "revise-2", "<!-- orch:v1 kind=go from=orch round=3/3 state=fixing -->", 0)
