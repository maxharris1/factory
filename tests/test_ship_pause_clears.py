from dataclasses import replace
from pathlib import Path

import pytest

from lib.github_actions import GhResult
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import factory
from tests.test_github_actions import HEAD, MARK_READY, PULL, draft_pull, served
from tests.test_handoff_reconcile import ReviewFlow
from tests.test_ship_redrive import approve_snapshot, github_setup, intent


pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")


def pause(queue: SQLiteJobQueue, state: str, *, stuck: bool) -> None:
    with queue.connect() as connection:
        connection.execute(
            """
            UPDATE ticket_state SET
              workflow_state = ?, paused_action = 'fix', paused_recipient = 'claude',
              paused_head = ?, paused_generation = 1, paused_question_id = 'question-1',
              stuck = ?, stuck_reason = ?
            WHERE repo = 'owner/repo' AND ticket_id = '42'
            """,
            (state, HEAD, int(stuck), "red-ci" if stuck else None),
        )


def settled(queue: SQLiteJobQueue) -> tuple:
    with queue.connect() as connection:
        return tuple(
            connection.execute(
                """
                SELECT workflow_state, paused_action, paused_recipient, paused_head,
                       paused_generation, paused_question_id, stuck
                FROM ticket_state WHERE repo = 'owner/repo' AND ticket_id = '42'
                """
            ).fetchone()
        )


def test_an_approved_handoff_clears_the_paused_question(tmp_path: Path) -> None:
    dispatcher, queue, gh = github_setup(tmp_path)
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    pause(queue, "review", stuck=False)

    dispatcher.process(approve_snapshot())

    assert settled(queue) == ("handed-off", None, None, None, None, None, 0)


def test_a_re_driven_handoff_clears_the_paused_question_and_stuck(tmp_path: Path) -> None:
    dispatcher, queue, gh = github_setup(tmp_path)
    gh.route(PULL, GhResult(1, "", "gh: Server Error (HTTP 502)\n"), draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    snapshot = approve_snapshot()
    dispatcher.process(snapshot)
    assert intent(queue)["status"] == "failed"
    pause(queue, "review", stuck=True)

    dispatcher.process(snapshot)

    assert intent(queue)["status"] == "completed"
    assert settled(queue) == ("handed-off", None, None, None, None, None, 0)


def test_a_reconciled_handoff_clears_the_paused_question(tmp_path: Path) -> None:
    flow = ReviewFlow(tmp_path)
    flow.reviewed_then_drifted()
    pause(flow.queue, "building", stuck=False)

    assert flow.approve("1/3") == "handed-off-reconciled"

    assert settled(flow.queue) == ("handed-off", None, None, None, None, None, 0)


def test_entering_the_merge_queue_clears_the_paused_question(
    tmp_path: Path,
) -> None:
    dispatcher, queue, gh = github_setup(tmp_path)
    gh.route(PULL, served("pull-42-open.json"))
    pause(queue, "review", stuck=False)

    dispatcher.process(
        replace(approve_snapshot(), factory=factory(ship="merge", finish_line="merge"))
    )

    assert settled(queue) == ("merge-queue", None, None, None, None, None, 0)
