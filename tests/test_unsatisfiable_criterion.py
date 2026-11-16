from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.dispatch.helpers import ACTED_VERDICT_OUTCOMES
from lib.dispatcher import RetryableStateError
from tests.test_approver_credential import APP_BOT, MAX, WITHOUT_MAX_GROK_TRUST, AppFlow, grok_wakes
from tests.test_dispatcher import GO, READY, VERDICT, comment, handled_outcome, job_count
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

    assert "ac-9 (not in the criteria)" in unsat_wakes(flow)[0]


def test_an_unsat_revise_at_the_cap_parks_instead_of_capping(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.factory = replace(flow.factory, auto_extra_rounds=2)
    to_first_review(flow)
    for number in (1, 2):
        body = f"- [blocking] f-{number}: the parser accepts prose.\n"
        assert revise(flow, f"{number}/3", f"revise-{number}", body=body) == "queued-review-fix"
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


def test_an_approvers_building_go_queues_a_fix_round_at_the_parked_head(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    revise(flow, "1/3", "revise-1", " unsat=ac-1")
    before = jobs(flow)

    assert flow.go("go-2", author=MAX) == "queued-fix"

    assert jobs(flow) == [*before, ("fix", "claude", "go-2")]
    fix = flow.queue.claim("claude")
    assert fix is not None
    detail = json.loads(fix.request.detail or "{}")
    assert (fix.request.head, detail["round"]) == (HEADS[1], "2/3")
    assert detail["instruction"].endswith("revise-1")
    state = flow.state()
    assert (state["workflow_state"], state["review_round"], state["cap_extra"], state["paused_action"]) == (
        "fixing",
        1,
        0,
        None,
    )
    assert flow.dispatcher.review_actions.states[-1] == "fixing"


def test_a_building_go_on_an_unsat_park_at_the_cap_grants_the_fix_round(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    for number in (1, 2):
        body = f"- [blocking] f-{number}: the parser accepts prose.\n"
        assert revise(flow, f"{number}/3", f"revise-{number}", body=body) == "queued-review-fix"
        flow.finish("claude")
        assert flow.ready(HEADS[number + 1]) == "queued-review"
        flow.finish("codex")
    revise(flow, "3/3", "revise-3", " unsat=ac-2")

    assert flow.go("go-2", author=MAX) == "queued-fix"

    state = flow.state()
    assert (state["workflow_state"], state["review_round"], state["cap_extra"]) == ("fixing", 3, 1)
    fix = flow.queue.claim("claude")
    assert fix is not None
    assert (fix.request.source_comment_id, json.loads(fix.request.detail or "{}")["round"]) == ("go-2", "4/4")


def test_a_replayed_building_go_after_a_failed_label_write_queues_one_fix(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    revise(flow, "1/3", "revise-1", " unsat=ac-1")
    flow.dispatcher.review_actions.state_failures.add(("owner/repo", "fixing"))
    with pytest.raises(RetryableStateError):
        flow.go("go-2", author=MAX)
    before = jobs(flow)
    flow.dispatcher.review_actions.state_failures.clear()

    flow.dispatcher.process(flow.snapshot())

    assert handled_outcome(flow.queue, "go-2") == "queued-fix"
    assert jobs(flow) == before
    assert [row for row in before if row[0] == "fix"] == [("fix", "claude", "go-2")]
    assert flow.dispatcher.review_actions.states[-1] == "fixing"


def test_a_go_from_a_bot_leaves_the_unsat_pause(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    revise(flow, "1/3", "revise-1", " unsat=ac-1")

    assert flow.go("go-2", author=APP_BOT) == "go-not-from-approver"
    assert flow.post("slash-1", "/go", "claude-login") == "go-not-from-approver"

    assert flow.state()["workflow_state"] == "waiting-max"
    assert flow.state()["paused_action"] == "review"
    assert job_count(flow.queue, "fix") == 0
    assert [row["source_comment_id"] for row in grok_wakes(flow, "go-not-from-approver")] == ["go-2", "slash-1"]


def test_an_unsat_pause_never_suppresses_the_re_review() -> None:
    assert "waiting-max-unsat" not in ACTED_VERDICT_OUTCOMES


def park_with_ready_starts_review(tmp_path: Path, text: str | None = None) -> AppFlow:
    flow = AppFlow(tmp_path, enabled=True, text=text)
    snapshot = flow.snapshot
    flow.snapshot = lambda: replace(snapshot(), ready_starts_review=True)
    flow.comments.append(comment("brief-1", BRIEF, author="grok-login", created_at=flow.now[0]))
    assert flow.ready(HEADS[1]) == "queued-review"
    review = flow.queue.claim("codex")
    assert review is not None
    flow.queue.finish(review.id, owner="codex", handoff_comment_id="unsat-1")
    assert revise(flow, "1/3", "unsat-1", " unsat=ac-1") == "waiting-max-unsat"
    return flow


@pytest.mark.job_bound
@pytest.mark.parametrize("changed_head", [False, True])
def test_a_builder_ready_leaves_the_unsat_pause(tmp_path: Path, changed_head: bool) -> None:
    flow = park_with_ready_starts_review(tmp_path)
    before = jobs(flow)
    if changed_head:
        flow.head = HEADS[2]
    assert flow.post("new-ready", READY.format(head=flow.head), "claude-login") == "ready-while-unsat"
    assert flow.state()["workflow_state"] == "waiting-max"
    assert jobs(flow) == before
    assert flow.dispatcher.review_actions.merges == []


@pytest.mark.job_bound
def test_a_replayed_go_after_a_failed_label_write_queues_nothing_more(tmp_path: Path) -> None:
    flow = park_with_ready_starts_review(tmp_path)
    flow.dispatcher.review_actions.state_failures.add(("owner/repo", "review"))
    with pytest.raises(RetryableStateError):
        flow.post("go-resume", GO.format(state="planning"), MAX)
    before = jobs(flow)
    flow.dispatcher.review_actions.state_failures.clear()
    flow.dispatcher.process(flow.snapshot())
    assert handled_outcome(flow.queue, "go-resume") == "resumed-review"
    assert jobs(flow) == before
    assert flow.state()["workflow_state"] == "review"


@pytest.mark.job_bound
def test_an_approvers_building_go_carries_the_rewrite_into_the_fix_brief(tmp_path: Path) -> None:
    flow = park_with_ready_starts_review(tmp_path, text=WITHOUT_MAX_GROK_TRUST)
    rewrite = "ac-1 now reads: the CLI exits 2."
    assert flow.post("go-rewrite", GO.format(state="building") + "\n" + rewrite, MAX) == "queued-fix"
    assert flow.queue.claim("codex") is None
    fix = flow.queue.claim("claude")
    assert fix is not None
    assert rewrite in render_brief(fix, bot="claude-bot", context=None)


@pytest.mark.job_bound
def test_a_building_go_fix_brief_targets_the_revise_and_quotes_the_go(tmp_path: Path) -> None:
    flow = park_with_ready_starts_review(tmp_path, text=WITHOUT_MAX_GROK_TRUST)
    snapshot = flow.snapshot
    flow.snapshot = lambda: replace(
        snapshot(),
        pr_number=42,
        comments=tuple(
            replace(item, url=f"https://github.com/owner/repo/pull/42#issuecomment-{100 + index}")
            for index, item in enumerate(flow.comments)
        ),
    )
    revise_url = flow.snapshot().comments[[item.id for item in flow.comments].index("unsat-1")].url
    rewrite = "ac-1 now reads: the CLI exits 2."
    assert flow.ci == "success"

    assert flow.post("go-fix", GO.format(state="building") + "\n" + rewrite, MAX) == "queued-fix"

    fix = flow.queue.claim("claude")
    assert fix is not None
    brief = render_brief(fix, bot="claude-bot", context=None, factory_root="/srv/orch/factory")
    task = next(line for line in brief.splitlines() if line.startswith("Fix the "))
    assert task.startswith(f"Fix the `[blocking]` findings of the REVISE verdict at {revise_url}.")
    assert "f-1: the parser accepts prose." in brief
    assert rewrite in brief


def test_f2_an_unsat_id_keeps_the_meaning_the_review_was_given(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_first_review(flow)
    changed = BRIEF.replace("The CLI exits 1 on a bad flag.", "The CLI always exits 0.")
    flow.comments.append(comment("brief-2", changed, author="grok-login", created_at=flow.now[0] + 1))

    assert revise(flow, "1/3", "revise-1", " unsat=ac-1") == "waiting-max-unsat"

    wakes = unsat_wakes(flow)
    assert len(wakes) == 1
    assert "criterion-unsatisfiable: ac-1: The CLI exits 1 on a bad flag., " in wakes[0]
    assert "always exits 0" not in wakes[0]
