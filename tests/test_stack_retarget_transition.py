
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from lib.dispatch.types import ParentMerged, ParentOpen, StackParent
from lib.intake_live import StackHeadsUnknown
from lib.update_check import StackUnbounded
from tests.merge_queue_support import sha
from tests.test_dispatcher import READY, FakeReviewActions, comment
from tests.test_ticket_base_branch import (
    CHILD_PR,
    FEATURE,
    PARENT_PR,
    STACK_REPO,
    child_row,
    fed_child,
    polled_child,
    stack_attention,
)
from tests.test_feeder_stack_parent import PARENT
from tests.test_feeder_target import targeted

ONTO = sha(9)
LANDED = ParentMerged(into="main", heads=(sha(4), sha(2)))


class Actions(FakeReviewActions):
    def __init__(self, github, *, crash_after_patch: bool = False) -> None:
        super().__init__()
        self.github = github
        self.crash_after_patch = crash_after_patch
        self.bases: list[str] = []

    def set_pull_base(self, ticket, base: str) -> None:
        self.bases.append(base)
        self.github.pulls[(STACK_REPO, CHILD_PR)]["base"] = base
        if self.crash_after_patch:
            self.crash_after_patch = False
            raise RuntimeError("poller died after the base edit")


class Bounds:
    def __init__(self, result: str | Exception = ONTO) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def __call__(self, repo: str, old_head: str, parent_heads: tuple[str, ...]) -> str:
        self.calls.append((repo, old_head, tuple(parent_heads)))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def wired(tmp_path: Path, *, text: str | None = None, state: str = "handed-off", **actions):
    feeder, queue, github, dispatcher = fed_child(tmp_path, text=text)
    github.landings[(STACK_REPO, PARENT_PR)] = ParentOpen()
    dispatcher.review_actions = Actions(github, **actions)
    dispatcher.stack_bounds = Bounds()
    dispatcher.process(polled_child(tmp_path, github))
    if state != "building":
        build = queue.claim("claude")
        queue.finish(build.id, owner="claude", handoff_comment_id="ready-1")
        with queue.connect() as connection:
            connection.execute(
                "UPDATE ticket_state SET workflow_state = ?, review_round = 1", (state,)
            )
    return queue, github, dispatcher


def land(github, facts: ParentMerged = LANDED) -> None:
    github.landings[(STACK_REPO, PARENT_PR)] = facts
    github.pulls[(STACK_REPO, PARENT_PR)].update(merged=True, state="closed", base=facts.into)


def rebases(queue) -> list[dict]:
    with queue.connect() as connection:
        details = [
            json.loads(row[0])
            for row in connection.execute("SELECT detail FROM jobs WHERE action = 'fix'")
        ]
    return [detail for detail in details if detail.get("kind") == "rebase"]


def landing_reads(github) -> int:
    return sum(call == f"landing:{STACK_REPO}:{PARENT_PR}" for call in github.calls)


def workflow_state(tmp_path: Path) -> str:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        return connection.execute("SELECT workflow_state FROM ticket_state").fetchone()[0]


def test_a_merged_parent_retargets_the_child_and_queues_one_onto_rebase(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path)
    land(github)

    dispatcher.process(polled_child(tmp_path, github))

    assert dispatcher.review_actions.bases == ["main"]
    assert dispatcher.stack_bounds.calls == [(STACK_REPO, sha(3), (sha(2), sha(4)))]
    assert child_row(tmp_path)["base_branch"] == "main"
    (rebase,) = rebases(queue)
    assert rebase["rebase"]["onto_from"] == ONTO
    assert rebase["base_branch"] == "main"
    assert workflow_state(tmp_path) == "fixing"
    assert dispatcher.review_actions.states[-1] == "fixing"
    assert stack_attention(tmp_path) == {}

    reads = landing_reads(github)
    dispatcher.process(polled_child(tmp_path, github))

    assert len(rebases(queue)) == 1
    assert dispatcher.review_actions.bases == ["main"]
    assert landing_reads(github) == reads, "a retargeted child reads its parent no more"


def test_a_crash_after_the_base_edit_queues_one_rebase_on_the_next_poll(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path, crash_after_patch=True)
    land(github)

    dispatcher.process(polled_child(tmp_path, github))

    assert github.pulls[(STACK_REPO, CHILD_PR)]["base"] == "main"
    assert child_row(tmp_path)["base_branch"] == PARENT
    assert rebases(queue) == []

    dispatcher.process(polled_child(tmp_path, github))
    dispatcher.process(polled_child(tmp_path, github))

    assert dispatcher.review_actions.bases == ["main"]
    assert child_row(tmp_path)["base_branch"] == "main"
    assert [rebase["rebase"]["onto_from"] for rebase in rebases(queue)] == [ONTO]


def test_a_base_github_moved_itself_gets_the_same_onto_rebase_and_no_edit(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path)
    land(github)
    github.pulls[(STACK_REPO, CHILD_PR)]["base"] = "main"

    dispatcher.process(polled_child(tmp_path, github))

    assert dispatcher.review_actions.bases == []
    assert child_row(tmp_path)["base_branch"] == "main"
    assert [rebase["rebase"]["onto_from"] for rebase in rebases(queue)] == [ONTO]
    assert stack_attention(tmp_path) == {}


def test_a_feature_branch_target_with_no_stack_parent_is_never_retargeted(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path, text=targeted(f"Target branch: {FEATURE}"))
    row = child_row(tmp_path)
    assert (row["base_branch"], row["stack_parent_pr"]) == (FEATURE, None)
    github.pulls[(STACK_REPO, PARENT_PR)]["head"] = FEATURE
    land(github)

    dispatcher.process(polled_child(tmp_path, github))
    dispatcher.process(polled_child(tmp_path, github))

    assert landing_reads(github) == 0
    assert dispatcher.stack_bounds.calls == []
    assert dispatcher.review_actions.bases == []
    assert child_row(tmp_path)["base_branch"] == FEATURE
    assert rebases(queue) == []


def test_an_open_parent_leaves_the_child_where_it_is(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path)

    dispatcher.process(polled_child(tmp_path, github))

    assert dispatcher.review_actions.bases == []
    assert child_row(tmp_path)["base_branch"] == PARENT
    assert rebases(queue) == []


def unproven(tmp_path: Path, queue, github, dispatcher) -> None:
    for _ in range(2):
        dispatcher.process(polled_child(tmp_path, github))

    assert set(stack_attention(tmp_path)) == {"stack-retarget-failed"}
    assert dispatcher.review_actions.bases == []
    assert child_row(tmp_path)["base_branch"] == PARENT
    assert rebases(queue) == []
    assert workflow_state(tmp_path) == "handed-off"


def test_a_child_whose_bounds_cannot_be_proven_raises_attention_and_queues_nothing(
    tmp_path: Path,
) -> None:
    queue, github, dispatcher = wired(tmp_path)
    land(github)
    dispatcher.stack_bounds.result = StackUnbounded("merge abc is not git's own clean merge")

    unproven(tmp_path, queue, github, dispatcher)

    assert "clean merge" in stack_attention(tmp_path)["stack-retarget-failed"]


def test_a_parent_whose_heads_cannot_be_read_in_full_raises_attention_and_queues_nothing(
    tmp_path: Path,
) -> None:
    queue, github, dispatcher = wired(tmp_path)
    land(github)

    def truncated(repo: str, number: int):
        github.calls.append(f"landing:{repo}:{number}")
        raise StackHeadsUnknown(f"PR #{number} has 3 force pushes; read 2")

    github.pull_landing = truncated

    unproven(tmp_path, queue, github, dispatcher)


def test_a_proven_retarget_clears_an_earlier_failure(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path)
    land(github)
    dispatcher.stack_bounds.result = StackUnbounded("no merge base bounds the range")
    dispatcher.process(polled_child(tmp_path, github))
    assert set(stack_attention(tmp_path)) == {"stack-retarget-failed"}

    dispatcher.stack_bounds.result = ONTO
    dispatcher.process(polled_child(tmp_path, github))

    assert stack_attention(tmp_path) == {}
    assert len(rebases(queue)) == 1


def test_a_busy_child_takes_the_new_base_now_and_its_rebase_once_it_is_free(
    tmp_path: Path,
) -> None:
    queue, github, dispatcher = wired(tmp_path, state="building")
    land(github)

    dispatcher.process(polled_child(tmp_path, github))

    assert child_row(tmp_path)["base_branch"] == "main"
    assert rebases(queue) == []
    (build,) = queue.outstanding()
    assert json.loads(build.request.detail)["base_branch"] == "main"

    running = queue.claim("claude")
    queue.finish(running.id, owner="claude", handoff_comment_id="ready-1")
    with queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = 'handed-off', review_round = 1"
        )
    dispatcher.process(polled_child(tmp_path, github))
    dispatcher.process(polled_child(tmp_path, github))

    assert [rebase["rebase"]["onto_from"] for rebase in rebases(queue)] == [ONTO]
    assert dispatcher.review_actions.bases == ["main"]


def test_a_ready_posted_before_the_retarget_rebase_is_superseded_by_it(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path)
    land(github)
    dispatcher.process(polled_child(tmp_path, github))
    (job,) = queue.outstanding()
    child = polled_child(tmp_path, github)
    before = comment("ready-2", READY.format(head=sha(3)), author="claude-bot",
                     created_at=job.created_at - 1)
    after = comment("ready-3", READY.format(head=sha(8)), author="claude-bot",
                    created_at=job.created_at + 1)

    assert dispatcher._superseded_by_rebase(child, before)
    assert not dispatcher._superseded_by_rebase(child, after)


def test_the_rebase_job_no_longer_names_the_parent_as_its_stack(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path)
    land(github)
    child = polled_child(tmp_path, github)

    dispatcher.process(replace(child, stack=StackParent(PARENT_PR, PARENT, sha(2))))

    (rebase,) = rebases(queue)
    assert rebase.get("stack") is None


def test_a_closed_child_is_not_retargeted(tmp_path: Path) -> None:
    queue, github, dispatcher = wired(tmp_path)
    land(github)

    dispatcher.process(replace(polled_child(tmp_path, github), closed=True))

    assert dispatcher.review_actions.bases == []
    assert child_row(tmp_path)["base_branch"] == PARENT
    assert rebases(queue) == []
