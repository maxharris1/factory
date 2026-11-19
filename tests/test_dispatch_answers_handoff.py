"""#891: an approver's answers state=handed-off hands off a ticket that waits
on Max. The factory stops working on it and still watches its PR."""

from pathlib import Path

import pytest

from lib.dispatcher import Dispatcher
from lib.dispatch.types import RetryableStateError
from lib.orch_yml import Approvals
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import (
    BRIEF,
    READY,
    FakeSource,
    CIResult,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    handled_outcome,
    jobs_of,
    queued_harness_jobs,
    rejected_wakes,
    seed_builder_job,
    seed_state,
    ticket,
    ticket_row,
    workflow_state,
)
from tests.test_max_actions import MAX_TRUST, body

HANDOFF = (
    "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator state=handed-off -->\n"
    "I'll QA it myself.\n"
)
MAX = Approvals(logins=frozenset({"maxharris1"}))
HEAD = "b" * 40


def setup(tmp_path: Path, approvals: Approvals = MAX, actions: FakeReviewActions | None = None):
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(path, queue, trusted_logins=MAX_TRUST,
                            review_actions=actions or FakeReviewActions(),
                            grok_delivery=FakeGrokDelivery(), approvals=approvals)
    return dispatcher, queue


def seed(queue: SQLiteJobQueue, state: str, stuck: int = 0, **columns) -> None:
    seed_builder_job(queue)
    seed_state(queue, state=state, stuck=stuck, **columns)
    queue.enqueue(JobRequest(repo="owner/repo", ticket_id="42", action="fix", recipient="claude",
                             source_comment_id="seed-fix", timeout_seconds=5400),
                  dedupe_key="seed-fix:g1")


def hand_off(dispatcher: Dispatcher, author: str = "maxharris1") -> None:
    dispatcher.process(ticket(comment("handoff-1", HANDOFF, author=author, created_at=9)))


def last_transition(queue: SQLiteJobQueue) -> tuple:
    with queue.connect() as connection:
        return tuple(connection.execute(
            "SELECT from_state, to_state, source FROM ticket_transitions ORDER BY id DESC LIMIT 1"
        ).fetchone())


def cancel_reasons(queue: SQLiteJobQueue) -> list[str]:
    with queue.connect() as connection:
        return [row["detail"] for row in connection.execute(
            "SELECT detail FROM job_events WHERE event = 'cancelled' AND action = 'fix'"
        )]


@pytest.mark.parametrize("state,stuck,columns", [
    ("capped", 0, {}),
    ("blocked", 0, {}),
    ("awaiting-ok", 0, {}),
    ("waiting-external", 0, {}),
    ("waiting-max", 0, {"paused_action": "build", "paused_recipient": "claude"}),
    ("fixing", 1, {"stuck_reason": "job-failed-2"}),
])
def test_max_hands_off_a_ticket_that_waits_on_him(tmp_path: Path, state, stuck, columns) -> None:
    dispatcher, queue = setup(tmp_path)
    seed(queue, state, stuck, **columns)

    hand_off(dispatcher)

    row = ticket_row(queue)
    assert (workflow_state(queue), handled_outcome(queue, "handoff-1")) == ("handed-off", "handed-off")
    assert (row["stuck"], row["stuck_reason"], row["paused_action"], row["paused_recipient"]) == (
        0, None, None, None)
    assert [job["state"] for job in jobs_of(queue, "fix")] == ["cancelled"]
    assert cancel_reasons(queue) == ["handed-off"]
    assert last_transition(queue) == (state, "handed-off", "answers-handoff")
    assert jobs_of(queue, "rejected") == [] and rejected_wakes(queue) == []
    assert queue.claim("claude") is None


@pytest.mark.parametrize("author,wakes", [("grok-login", 1), ("claude-login", 1), ("stranger", 0)])
def test_a_hand_off_from_anyone_but_an_approver_changes_nothing(
    tmp_path: Path, author: str, wakes: int
) -> None:
    dispatcher, queue = setup(tmp_path)
    seed(queue, "capped")

    hand_off(dispatcher, author)

    assert (workflow_state(queue), handled_outcome(queue, "handoff-1")) == (
        "capped", "rejected-handoff-author")
    assert len(queued_harness_jobs(queue)) == 1
    assert len(rejected_wakes(queue)) == wakes


def test_with_no_approver_logins_every_hand_off_is_refused(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, Approvals())
    seed(queue, "capped")

    hand_off(dispatcher)

    assert (workflow_state(queue), handled_outcome(queue, "handoff-1")) == (
        "capped", "rejected-handoff-author")
    assert len(rejected_wakes(queue)) == 1


@pytest.mark.parametrize("state,columns", [
    ("building", {}),
    ("waiting-max", {"paused_action": None}),
    ("waiting-max", {"paused_action": "clarify", "paused_recipient": "grok"}),
])
def test_a_ticket_with_work_in_flight_or_not_past_go_is_not_handed_off(
    tmp_path: Path, state: str, columns: dict
) -> None:
    dispatcher, queue = setup(tmp_path)
    seed(queue, state, **columns)
    before = dict(ticket_row(queue))

    hand_off(dispatcher)

    assert handled_outcome(queue, "handoff-1") == "rejected-handoff-state"
    assert dict(ticket_row(queue)) == before
    assert len(queued_harness_jobs(queue)) == 1


@pytest.mark.parametrize("ends", ["fails", "released"])
def test_a_hand_off_is_refused_while_a_job_runs(tmp_path: Path, ends: str) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_state(queue, state="awaiting-ok")
    queue.enqueue(JobRequest(repo="owner/repo", ticket_id="42", action="fix", recipient="claude",
                             source_comment_id="approve-1", timeout_seconds=5400),
                  dedupe_key="held-fix")
    running = queue.claim("claude")
    assert running is not None

    hand_off(dispatcher)

    assert (workflow_state(queue), handled_outcome(queue, "handoff-1")) == (
        "awaiting-ok", "rejected-handoff-state")
    if ends == "fails":
        queue.finish(running.id, owner="claude", handoff_comment_id=None)
        dispatcher.reconcile_jobs()
    else:
        queue.release(running.id, owner="claude")
    assert workflow_state(queue) == "awaiting-ok"


def test_the_first_questions_before_go_are_not_handed_off(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    brief = comment("brief-1", BRIEF, author="grok-login", created_at=1)
    question = comment(
        "question-1",
        "<!-- orch:v1 kind=questions from=grok-bot role=orchestrator state=waiting-max -->\n"
        "**Questions**\n- [product] Which offer tier ships first?\n",
        author="grok-login", created_at=2,
    )
    current = ticket(brief, question)
    dispatcher.poll_once(FakeSource(current, github=(current,)))
    assert (workflow_state(queue), ticket_row(queue)["paused_action"]) == ("waiting-max", "clarify")

    handoff = comment("hand-off-1", body("hand-off", "I'll QA it myself."),
                      author="maxharris1", created_at=3)
    dispatcher.process(ticket(brief, question, handoff))

    assert (workflow_state(queue), handled_outcome(queue, "hand-off-1")) == (
        "waiting-max", "rejected-handoff-state")
    assert ticket_row(queue)["paused_action"] == "clarify"


def test_a_hand_off_whose_label_write_failed_settles_on_the_next_read(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    actions.state_failures.add(("owner/repo", "handed-off"))
    dispatcher, queue = setup(tmp_path, actions=actions)
    seed(queue, "capped")
    with pytest.raises(RetryableStateError):
        hand_off(dispatcher)
    actions.state_failures.clear()

    hand_off(dispatcher)

    assert (workflow_state(queue), handled_outcome(queue, "handoff-1")) == ("handed-off", "handed-off")
    assert actions.states[-1] == "handed-off" and rejected_wakes(queue) == []


def test_a_builder_ready_after_the_hand_off_re_enters_review(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed(queue, "capped")
    hand_off(dispatcher)

    dispatcher.process(ticket(
        comment("handoff-1", HANDOFF, author="maxharris1", created_at=9),
        comment("ready-seed", READY.format(head=HEAD), author="claude-login", created_at=10),
        head=HEAD, ci={HEAD: CIResult("success")},
    ))

    assert (workflow_state(queue), handled_outcome(queue, "ready-seed")) == ("review", "queued-review")


def test_the_poll_seeing_the_merge_moves_a_handed_off_ticket_to_merged(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed(queue, "capped")
    hand_off(dispatcher)

    dispatcher.record_terminal("owner/repo", "42", "merged")

    assert last_transition(queue) == ("handed-off", "merged", "enter-merged")
