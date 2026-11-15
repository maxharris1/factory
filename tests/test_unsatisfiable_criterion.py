from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from lib.dispatch.helpers import ACTED_VERDICT_OUTCOMES
from tests.test_approver_credential import APP_BOT, MAX, AppFlow, grok_wakes
from tests.test_dispatcher import VERDICT, comment, job_count
from tests.test_rounds_and_caps import HEADS
from tests.test_slash_go import jobs

BRIEF = (
    "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->\n"
    "## Acceptance criteria\n"
    "- The CLI exits 1 on a bad flag.\n"
    "- The parser rejects prose.\n"
)


def revise(flow: AppFlow, round_text: str, comment_id: str, extra: str = "", body: str = "") -> str:
    header = VERDICT.format(round=round_text, head=flow.head, verdict="REVISE", risk="high")
    header = header.replace(" -->", f"{extra} -->", 1)
    return flow.post(comment_id, header + (body or "- [blocking] f-1: the parser accepts prose.\n"), "codex-login")


def to_first_review(flow: AppFlow) -> None:
    flow.comments.append(comment("brief-1", BRIEF, author="grok-login", created_at=flow.now[0]))
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex")


def unsat_wakes(flow: AppFlow) -> list[str]:
    return [
        str(json.loads(row["detail"])["detail"])
        for row in grok_wakes(flow, "clarify")
        if "criterion-unsatisfiable" in str(row["detail"])
    ]


def test_an_unsat_revise_parks_with_one_wake_and_no_round(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    fixes = job_count(flow.queue, "fix")

    assert revise(flow, "1/3", "revise-1", " unsat=ac-1") == "waiting-max-unsat"

    state = flow.state()
    assert (state["workflow_state"], state["review_round"], state["cap_extra"]) == ("waiting-max", 0, 0)
    assert (state["paused_action"], state["paused_recipient"], state["paused_question_id"]) == (
        "review",
        "codex",
        "revise-1",
    )
    assert job_count(flow.queue, "fix") == fixes
    wakes = unsat_wakes(flow)
    assert len(wakes) == 1
    assert "ac-1: The CLI exits 1 on a bad flag." in wakes[0]
    assert flow.dispatcher.review_actions.states[-1] == "waiting-max"

    before = jobs(flow)
    flow.dispatcher.process(flow.snapshot())
    assert jobs(flow) == before


def test_an_unsat_id_missing_from_the_brief_is_named_as_such(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)

    assert revise(flow, "1/3", "revise-1", " unsat=ac-9") == "waiting-max-unsat"

    assert "ac-9 (not in the brief)" in unsat_wakes(flow)[0]


def test_an_unsat_revise_at_the_cap_parks_instead_of_capping(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)
    to_first_review(flow)
    for number in (1, 2):
        assert revise(flow, f"{number}/3", f"revise-{number}") == "queued-review-fix"
        flow.finish("claude")
        assert flow.ready(HEADS[number + 1]) == "queued-review"
        flow.finish("codex")

    assert revise(flow, "3/3", "revise-3", " unsat=ac-2") == "waiting-max-unsat"

    state = flow.state()
    assert (state["workflow_state"], state["review_round"], state["cap_extra"]) == ("waiting-max", 2, 0)
    with flow.queue.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM auto_extra_comments").fetchone()[0] == 0


def test_an_unsat_revise_parks_while_ci_is_pending(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, start_before_ci=True)
    to_first_review(flow)
    flow.ci = "pending"

    assert revise(flow, "1/3", "revise-1", " unsat=ac-1") == "waiting-max-unsat"

    assert flow.state()["workflow_state"] == "waiting-max"
    assert flow.state()["gate_count"] == 0


def test_a_body_only_unsatisfiable_takes_the_fix_path(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    fixes = job_count(flow.queue, "fix")

    outcome = revise(flow, "1/3", "revise-1", body="- ac-1: unsatisfiable. No change can meet it.\n")

    assert outcome == "queued-review-fix"
    assert (flow.state()["workflow_state"], flow.state()["review_round"]) == ("fixing", 1)
    assert job_count(flow.queue, "fix") == fixes + 1
    assert unsat_wakes(flow) == []


def test_slash_go_with_a_rewrite_resumes_the_same_review_round(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    revise(flow, "1/3", "revise-1", " unsat=ac-1")

    assert flow.post("slash-1", "/go\nac-1 now reads: the CLI exits 2.", MAX) == "resumed-review"

    state = flow.state()
    assert (state["workflow_state"], state["paused_action"]) == ("review", None)
    review = flow.finish("codex")
    detail = json.loads(review.request.detail or "{}")
    assert (review.request.source_comment_id, detail["round"]) == ("slash-1", "1/3")
    assert detail["instruction"].endswith("slash-1")
    assert revise(flow, "1/3", "revise-2") == "queued-review-fix"


def test_a_grok_go_resumes_the_review_instead_of_a_build(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    revise(flow, "1/3", "revise-1", " unsat=ac-1")
    builds = job_count(flow.queue, "build")

    assert flow.go("go-2", author=MAX) == "resumed-review"

    assert flow.state()["workflow_state"] == "review"
    assert job_count(flow.queue, "build") == builds


def test_a_go_from_a_bot_leaves_the_unsat_pause(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    revise(flow, "1/3", "revise-1", " unsat=ac-1")

    assert flow.go("go-2", author=APP_BOT) == "go-not-from-approver"
    assert flow.post("slash-1", "/go", "claude-login") == "go-not-from-approver"

    assert flow.state()["workflow_state"] == "waiting-max"
    assert flow.state()["paused_action"] == "review"
    assert [row["source_comment_id"] for row in grok_wakes(flow, "go-not-from-approver")] == ["go-2", "slash-1"]


def test_an_unsat_pause_never_suppresses_the_re_review() -> None:
    assert "waiting-max-unsat" not in ACTED_VERDICT_OUTCOMES
