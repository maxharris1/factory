"""#1434: a trusted `kind=rebase` queues one rebase-only fix that spends no round."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import brief_kind, render_brief
from lib.brief.common import detail_of
from lib.dispatch.types import CIResult
from lib.orch_yml import Approvals
from lib.queue import JobRequest
from tests.merge_queue_support import PR, READY, Board, sha
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


def ready_waiting_on_ci(tmp_path: Path) -> Flow:
    flow = Flow(tmp_path)
    assert flow.go() == "queued-build"
    flow.finish("claude", f"ready-{HEADS[1][:6]}")
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex", "revise-1/3")
    assert flow.revise("1/3") == "queued-review-fix"
    flow.finish("claude", f"ready-{HEADS[2][:6]}")
    flow.ci = "pending"
    assert flow.ready(HEADS[2]) is None
    assert flow.queue.outstanding() == ()
    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"
    return flow


@pytest.mark.parametrize("claim_rebase", [False, True])
def test_a_ready_waiting_on_ci_does_not_overtake_the_rebase(
    tmp_path: Path, claim_rebase: bool
) -> None:
    flow = ready_waiting_on_ci(tmp_path)
    if claim_rebase:
        assert flow.queue.claim("claude") is not None
    flow.ci = "success"

    flow.dispatcher.process(flow.snapshot())

    assert [job.request.action for job in flow.queue.outstanding()] == ["fix"]
    assert detail_of(flow.queue.outstanding()[0])["kind"] == "rebase"
    assert flow.state()["workflow_state"] == "fixing"
    assert outcome(flow.queue, f"ready-{HEADS[2][:6]}") == "ready-superseded-by-rebase"


def test_the_rebase_fix_ready_still_starts_review(tmp_path: Path) -> None:
    flow = ready_waiting_on_ci(tmp_path)
    flow.ci = "success"
    flow.dispatcher.process(flow.snapshot())
    flow.finish("claude", "ready-after-rebase")

    assert flow.post("ready-after-rebase", READY.format(head=HEADS[2]), "claude-login") == (
        "queued-review"
    )

    assert [job.request.action for job in flow.queue.outstanding()] == ["review"]
    assert flow.state()["workflow_state"] == "review"


def approved(tmp_path: Path, ship: str) -> tuple[Board, PR]:
    board = Board(tmp_path, ship=ship)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    return board, pr


def queue_update(board: Board, pr: PR) -> str:
    return board.queue.enqueue(
        JobRequest(
            repo=pr.repo,
            ticket_id=pr.number,
            action="update-from-main",
            recipient="claude",
            source_comment_id="approve-42",
            timeout_seconds=3600,
            head=pr.head,
        ),
        dedupe_key="t-update:42",
    ).id


@pytest.mark.parametrize(
    ("ship", "state"), [("handoff", "merge-queue"), ("after:max", "waiting-external")]
)
def test_rebase_from_the_merge_queue_clears_the_approval_and_cancels_the_update(
    tmp_path: Path, ship: str, state: str
) -> None:
    board, pr = approved(tmp_path, ship)
    assert board.state("42")["workflow_state"] == state
    assert board.state("42")["approved_head"] == pr.head
    spent = board.state("42")["review_round"]
    queue_update(board, pr)

    board.post(pr, "rebase-42", REBASE, "grok-login")
    board.poll()

    assert board.outcome("rebase-42") == "queued-rebase"
    row = board.state("42")
    assert (row["workflow_state"], row["approved_head"], row["approve_comment_id"]) == (
        "fixing",
        None,
        None,
    )
    assert [job["state"] for job in board.jobs("update-from-main")] == ["cancelled"]
    [fix] = board.jobs("fix")
    assert (fix["state"], fix["head"]) == ("queued", pr.head)
    detail = json.loads(fix["detail"])
    assert detail["kind"] == "rebase"
    assert detail["rebase"] == {"from_state": state, "from_head": pr.head, "round": spent}
    pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()
    assert board.actions.merges == []


def test_a_running_update_refuses_the_rebase_and_keeps_the_approval(tmp_path: Path) -> None:
    board, pr = approved(tmp_path, "handoff")
    queue_update(board, pr)
    board.claim("claude")

    board.post(pr, "rebase-42", REBASE, "grok-login")
    board.poll()

    assert board.outcome("rebase-42") == "rejected-rebase-job-active"
    row = board.state("42")
    assert (row["workflow_state"], row["approved_head"]) == ("merge-queue", pr.head)
    assert [job["state"] for job in board.jobs("update-from-main")] == ["running"]
    assert board.jobs("fix") == []


@pytest.mark.parametrize(
    "state",
    [
        "planning",
        "plan-check",
        "building",
        "questions",
        "blocked",
        "updating",
        "waiting-max",
        "handed-off-externally",
    ],
)
def test_a_rebase_in_any_other_state_is_refused_with_no_job(tmp_path: Path, state: str) -> None:
    flow = in_review(tmp_path)
    set_state(flow, state, 1)
    before = len(harness_jobs(flow))

    assert flow.post("rebase-42", REBASE, "grok-login") == "rejected-rebase-state"

    assert len(harness_jobs(flow)) == before
    assert flow.state()["workflow_state"] == state
    assert decisions(flow) == []


def test_a_rebase_on_a_pr_with_no_head_is_refused_with_no_job(tmp_path: Path) -> None:
    flow = in_review(tmp_path)
    flow.head = None
    before = len(harness_jobs(flow))

    assert flow.post("rebase-42", REBASE, "grok-login") == "rejected-rebase-no-pr"

    assert len(harness_jobs(flow)) == before
    assert flow.state()["workflow_state"] == "review"
    assert decisions(flow) == []


def harness_jobs(flow: Flow) -> list:
    return [row for row in jobs(flow.queue) if row["recipient"] != "grok"]


CONFLICT = (
    "<!-- orch:v1 kind=questions from=claude-bot role=builder -->\n"
    "The rebase onto origin/main stopped on conflicts I cannot resolve without changing behavior:\n"
    "\n"
    "- `lib/dispatch/questions.py`\n"
    "- `tests/test_rebase_kind.py`\n"
    "\n"
    "HEAD is back at the PR head and nothing was pushed. Which side should win?\n"
)
ANSWERS = "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator -->\nKeep main's side.\n"


def conflicted(tmp_path: Path) -> tuple[Flow, dict]:
    flow = in_review(tmp_path)
    assert flow.post("rebase-42", REBASE, "grok-login") == "queued-rebase"
    rebase = detail_of(flow.finish("claude", "conflict-1"))["rebase"]
    assert flow.post("conflict-1", CONFLICT, "claude-login") == "waiting-max"
    return flow, rebase


def test_a_rebase_conflict_parks_on_max_at_the_unchanged_head(tmp_path: Path) -> None:
    flow, _ = conflicted(tmp_path)

    row = flow.state()
    assert (row["workflow_state"], row["paused_action"], row["paused_question_id"]) == (
        "waiting-max",
        "fix",
        "conflict-1",
    )
    assert row["paused_head"] == HEADS[1] == flow.head
    assert flow.actions.states[-1] == "waiting-max"
    [wake] = [job for job in jobs(flow.queue, "clarify") if job["source_comment_id"] == "conflict-1"]
    assert wake["head"] == HEADS[1]


def test_a_rebase_conflict_records_the_outcome_with_its_files(tmp_path: Path) -> None:
    flow, _ = conflicted(tmp_path)

    assert decisions(flow)[-1] == (
        "conflict-1",
        "conflict-1",
        "rebase-conflict:conflict-1",
        "Conflict: waiting on Max; files: lib/dispatch/questions.py, tests/test_rebase_kind.py",
        "claude-login",
    )


def test_a_question_after_an_ordinary_fix_records_no_conflict(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    assert flow.go() == "queued-build"
    flow.finish("claude", f"ready-{HEADS[1][:6]}")
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex", "revise-1/3")
    assert flow.revise("1/3") == "queued-review-fix"
    flow.finish("claude", "conflict-1")

    assert flow.post("conflict-1", CONFLICT, "claude-login") == "waiting-max"

    assert [row for row in decisions(flow) if row[2].startswith("rebase-conflict:")] == []


def test_maxs_answer_resumes_the_rebase_not_a_ci_fix(tmp_path: Path) -> None:
    flow, rebase = conflicted(tmp_path)
    red = CIResult("failure", log="failed: pytest")
    snapshot = flow.snapshot
    flow.snapshot = lambda: replace(snapshot(), ci={HEADS[1]: red})
    before = len(fixes(flow))

    assert flow.post("answers-1", ANSWERS, "grok-login") == "resumed-fix"

    [fix] = fixes(flow)[before:]
    detail = detail_of_row(fix)
    assert (fix["head"], detail["kind"], detail["rebase"]) == (HEADS[1], "rebase", rebase)
    assert "ci_failure" not in detail
    assert flow.state()["workflow_state"] == "fixing"
    resumed = flow.finish("claude")
    assert brief_kind(resumed) == "rebase"
    brief = render_brief(resumed, bot="claude-bot", context=None)
    assert "git rebase --abort" in brief and "resumes a paused step" in brief
