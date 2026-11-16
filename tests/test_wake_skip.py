from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.constants import GROK_WAKE_REASONS, GROK_WAKE_SKIP
from lib.dispatcher import Dispatcher, TrackedTicket
from tests.test_approver_credential import AppFlow
from tests.test_hooks import (
    HEAD_A,
    HEAD_B,
    FakeBot,
    FakeReviewActions,
    approve,
    in_review,
    park_followup,
    restart,
    setup,
    skipped_keys,
    ticket,
)
from tests.test_repeat_block import F1, repeat_wakes, rounds
from tests.test_wake_payload import on_pr

JUDGMENT = ("capped", "stuck", "blocked", "awaiting-ok", "ship-failed", "go-not-from-approver")


def wake(dispatcher: Dispatcher, reason: str, comment_id: str = "c-1", head: str = HEAD_A) -> None:
    dispatcher.wake_grok_once(
        on_pr(),
        reason=reason,
        source_comment_id=comment_id,
        detail='{"outcome":"' + reason + '-outcome","project_bot":"grok-bot"}',
        head=head,
    )


def notices(dispatcher: Dispatcher) -> list[str]:
    actions = dispatcher.review_actions
    assert isinstance(actions, FakeReviewActions)
    return actions.notices


def test_the_default_skip_set_is_the_ticket_list_and_names_real_reasons() -> None:
    assert GROK_WAKE_SKIP == {"done", "refused", "rejected", "unparsed", "stale"}
    assert GROK_WAKE_SKIP <= set(GROK_WAKE_REASONS)


@pytest.mark.parametrize("reason", sorted(GROK_WAKE_SKIP))
def test_a_skipped_reason_posts_one_notice_and_queues_no_grok_job(
    tmp_path: Path, reason: str
) -> None:
    dispatcher, queue = setup(tmp_path)

    wake(dispatcher, reason)
    dispatcher.process(on_pr())
    dispatcher.process(on_pr())

    assert FakeBot(queue, "grok").drain() == []
    [notice] = notices(dispatcher)
    assert f"`{reason}`" in notice
    assert f"- outcome: {reason}-outcome" in notice.splitlines()
    assert "orch:v1" not in notice
    marker = notice.splitlines()[-1]
    once_key = "-" if reason == "refused" else HEAD_A if reason == "done" else "c-1"
    assert marker == f"<!-- orch-wake-skip {reason}:{once_key} -->"


def test_a_merged_pr_with_nothing_to_triage_gets_a_notice_not_a_done_wake(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    in_review(queue, HEAD_A)
    snapshot = on_pr(approve("905", HEAD_A, 1), ship="merge")

    dispatcher.process(snapshot)
    dispatcher.process(snapshot)

    assert [job.request.action for job in FakeBot(queue, "grok").drain()] == []
    assert skipped_keys(queue) == [("done", HEAD_A)]
    [notice] = notices(dispatcher)
    assert "- outcome: merged" in notice.splitlines()


@pytest.mark.parametrize("reason", JUDGMENT)
def test_a_judgment_reason_still_wakes_grok_and_posts_no_notice(
    tmp_path: Path, reason: str
) -> None:
    dispatcher, queue = setup(tmp_path)

    wake(dispatcher, reason)
    dispatcher.process(on_pr())

    assert [job.request.action for job in FakeBot(queue, "grok").drain()] == [reason]
    assert notices(dispatcher) == []
    assert skipped_keys(queue) == []


def test_a_done_wake_with_a_parked_follow_up_still_wakes_grok(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    park_followup(queue)

    wake(dispatcher, "done")
    dispatcher.process(on_pr())

    assert [job.request.action for job in FakeBot(queue, "grok").drain()] == ["done"]
    assert notices(dispatcher) == []


def test_a_repeat_stop_still_wakes_grok_and_posts_no_notice(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)

    assert rounds(flow, F1, F1) == "waiting-max-repeat"

    assert len(repeat_wakes(flow)) == 1
    assert skipped_keys(flow.queue) == []


def test_a_linear_ticket_with_no_pr_still_wakes_grok(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    linear = ticket()
    linear = replace(linear, factory=replace(linear.factory, tickets="linear"))

    dispatcher.wake_grok_once(
        linear, reason="unparsed", source_comment_id="c-1", detail="bad header"
    )

    assert [job.request.action for job in FakeBot(queue, "grok").drain()] == ["unparsed"]
    assert skipped_keys(queue) == []


def test_a_test_can_override_the_skip_set(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.grok_wake_skip = frozenset({"capped", "blocked"})

    wake(dispatcher, "capped")
    wake(dispatcher, "blocked")
    wake(dispatcher, "unparsed")

    assert [job.request.action for job in FakeBot(queue, "grok").drain()] == ["unparsed"]
    assert sorted(reason for reason, _key in skipped_keys(queue)) == ["blocked", "capped"]


def test_skipped_wakes_are_counted_across_a_restart(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now=now)
    wake(dispatcher, "unparsed", "c-1")
    now[0] = 200.0
    wake(dispatcher, "rejected", "c-2")

    rebuilt, queue = restart(tmp_path)
    rebuilt.process(on_pr())

    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT reason, repo, ticket_id, skipped_at, status FROM skipped_wakes"
            " ORDER BY skipped_at"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("unparsed", "owner/repo", "42", 100.0, "posted"),
        ("rejected", "owner/repo", "42", 200.0, "posted"),
    ]


def test_a_skip_then_a_cap_on_the_same_ticket_still_wakes_grok(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)

    wake(dispatcher, "unparsed", "c-1")
    wake(dispatcher, "capped", "c-2")
    wake(dispatcher, "capped", "c-3")

    assert [job.request.action for job in FakeBot(queue, "grok").drain()] == ["capped"]
    assert skipped_keys(queue) == [("unparsed", "c-1")]


def test_a_skipped_once_key_never_becomes_a_grok_wake_later(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    wake(dispatcher, "done")

    park_followup(queue)
    wake(dispatcher, "done")
    rebuilt = restart(tmp_path)[0]
    rebuilt.grok_wake_skip = frozenset()
    wake(rebuilt, "done")

    assert FakeBot(queue, "grok").drain() == []
    assert skipped_keys(queue) == [("done", HEAD_A)]
    wake(rebuilt, "done", head=HEAD_B)
    assert [job.request.action for job in FakeBot(queue, "grok").drain()] == ["done"]


def test_a_once_key_grok_already_had_is_not_also_skipped(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.grok_wake_skip = frozenset()
    wake(dispatcher, "unparsed", "c-1")

    rebuilt = restart(tmp_path)[0]
    wake(rebuilt, "unparsed", "c-1")
    rebuilt.process(on_pr())

    assert [job.request.action for job in FakeBot(queue, "grok").drain()] == ["unparsed"]
    assert skipped_keys(queue) == []
    assert notices(rebuilt) == []


def test_a_failed_post_stays_pending_and_posts_on_the_next_read(tmp_path: Path) -> None:
    class FailOnce(FakeReviewActions):
        failed = False

        def comment_once(self, ticket: TrackedTicket, body: str, marker: str) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("comment post failed")
            super().comment_once(ticket, body, marker)

    actions = FailOnce()
    dispatcher, queue = setup(tmp_path, actions=actions)
    wake(dispatcher, "stale")

    dispatcher.process(on_pr())
    assert actions.notices == []
    dispatcher.process(on_pr())

    assert len(actions.notices) == 1
    assert FakeBot(queue, "grok").drain() == []
