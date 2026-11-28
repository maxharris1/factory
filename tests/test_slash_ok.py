import dataclasses
from pathlib import Path

import pytest

from lib.factory_md import HoldConfig
from lib.hold import decide
from tests.test_dispatcher import FakeReviewActions, comment
from tests.test_hold_blocks_handoff import MAX, assert_root_held, root_snapshot
from tests.test_ship_hold import (
    HEAD,
    NEW_HEAD,
    approve,
    ready,
    setup,
    workflow_state,
)


def slash_ok(body: str, *, author: str = MAX, comment_id: str = "ok-1"):
    return comment(comment_id, body, author=author, created_at=20)


def held_by_paths(*comments, approver_allowed: bool = True):
    return decide(
        HoldConfig(approver=MAX, paths=("orch.yml",)),
        labels=(),
        changed_files=("orch.yml",),
        label_events=(),
        comments=comments,
        approver_allowed=approver_allowed,
        covers=lambda head: head == HEAD,
        factory_md_trust_changed=False,
    )


def test_a_maxharris1_slash_ok_at_the_head_hands_off_on_the_factory_repo(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    dispatcher.process(root_snapshot(approve()))
    assert_root_held(queue, actions)

    dispatcher.process(root_snapshot(approve(), slash_ok(f"/ok {HEAD}")))

    assert actions.handoffs == [HEAD]
    assert workflow_state(queue) == "merge-queue"


@pytest.mark.parametrize(
    "body",
    [f"/ok {HEAD}", f"  /ok   {HEAD}  \r\nShipping after a look at orch.yml.\n"],
    ids=["bare", "with-prose"],
)
def test_a_slash_ok_naming_the_head_clears_the_hold(body: str) -> None:
    decision = held_by_paths(slash_ok(body))

    assert decision.cleared == "ok"
    assert not decision.blocks


@pytest.mark.parametrize(
    "held_ok",
    [
        slash_ok(f"/ok {NEW_HEAD}"),
        slash_ok(f"/ok {HEAD}", author="grok-login"),
        slash_ok(f"/ok {HEAD}", author="claude-login"),
        slash_ok(f"/ok {HEAD[:7]}"),
        slash_ok(f"/ok {HEAD.upper()}"),
        slash_ok("/ok"),
        slash_ok(f"/okay {HEAD}"),
        slash_ok(f"Ready? /ok {HEAD}"),
        slash_ok(f"/ok {HEAD} after QA"),
        slash_ok(f"/ok {HEAD}{HEAD[:1]}"),
    ],
    ids=[
        "stale-sha",
        "grok-bot",
        "claude-bot",
        "short-sha",
        "upper-case",
        "no-sha",
        "other-command",
        "not-first",
        "trailing-words",
        "41-chars",
    ],
)
def test_a_slash_ok_that_is_not_the_approvers_at_the_head_stays_held(held_ok) -> None:
    decision = held_by_paths(held_ok)

    assert decision.cleared is None
    assert decision.blocks


def test_a_slash_ok_does_not_clear_when_the_approver_may_not_approve() -> None:
    assert held_by_paths(slash_ok(f"/ok {HEAD}"), approver_allowed=False).blocks


def test_an_edited_slash_ok_stays_held() -> None:
    edited = dataclasses.replace(slash_ok(f"/ok {HEAD}"), edited_at=21)

    assert held_by_paths(edited).blocks


@pytest.mark.parametrize(
    ("held_ok", "head"),
    [
        (slash_ok(f"/ok {HEAD}"), NEW_HEAD),
        (slash_ok(f"/ok {NEW_HEAD}", author="grok-login"), NEW_HEAD),
    ],
    ids=["stale-sha", "bot-login"],
)
def test_the_factory_repo_stays_held_on_a_slash_ok_that_does_not_count(
    tmp_path: Path,
    held_ok,
    head: str,
) -> None:
    dispatcher, queue, actions = setup(
        tmp_path, FakeReviewActions({HEAD: "1" * 40, NEW_HEAD: "2" * 40})
    )
    ready(dispatcher, queue, head)

    dispatcher.process(root_snapshot(approve(head), held_ok, head=head))

    assert_root_held(queue, actions, head)
