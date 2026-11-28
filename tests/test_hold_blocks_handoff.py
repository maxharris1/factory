import dataclasses
import json
from pathlib import Path

import pytest

from lib.dispatcher import ShipError, TrackedTicket
from lib.factory_md import HoldConfig
from lib.hook_t8 import T8Hook
from tests.merge_queue_support import Board, sha
from tests.test_dispatcher import VERDICT, FakeReviewActions, comment
from tests.test_factory_hold import root_config
from tests.test_github_actions import HEAD as GH_HEAD
from tests.test_github_actions import (
    MARK_READY,
    MERGE,
    PULL,
    adapter,
    draft_pull,
    served,
)
from tests.test_hold_merge_queue import APPROVER as QUEUE_APPROVER
from tests.test_hold_merge_queue import GitHubMergeActions, open_held, relabel
from tests.test_ship_hold import (
    HEAD,
    NEW_HEAD,
    OK,
    TRUSTED,
    approve,
    assert_held,
    grok_jobs,
    hold_factory,
    labeled,
    ok,
    ready,
    setup,
    snapshot,
    workflow_state,
)
from tests.test_hooks import skip_facts

MAX = "maxharris1"
HANDOFF = hold_factory(ship="handoff")
QUEUE_HOLD = HoldConfig(approver=QUEUE_APPROVER, paths=("src/**/*.tsx",))


def root_snapshot(*comments, head: str = HEAD) -> TrackedTicket:
    config, _ = root_config()
    return snapshot(*comments, head=head, factory=config, files=("orch.yml",))


def assert_root_held(queue, actions: FakeReviewActions, head: str = HEAD) -> None:
    assert actions.handoffs == [], "no ready-mark while held"
    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"
    assert "merge-queue" not in actions.states
    wakes = [row for row in grok_jobs(queue) if row["action"] == "awaiting-ok"]
    assert wakes[-1]["head"] == head
    assert json.loads(wakes[-1]["detail"])["approver"] == MAX
    assert not any(row["action"] == "done" for row in grok_jobs(queue))



def test_the_factory_repo_holds_a_trust_path_pr_without_a_ready_mark(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()

    dispatcher.process(root_snapshot(approve()))
    assert_root_held(queue, actions)

    for _ in range(3):
        dispatcher.process(root_snapshot(approve()))
        dispatcher.reconcile_jobs()
    assert_root_held(queue, actions)
    assert actions.states == ["awaiting-ok"], "the state is written once"
    assert [row["action"] for row in grok_jobs(queue)] == ["awaiting-ok"]


@pytest.mark.parametrize(
    "files",
    [
        ("skills/pstack/skills/x/SKILL.md", "skills/skills.lock"),
        ("grok/prompt.md", ".claude/skills/a/SKILL.md"),
        ("factory.md", "skills/factory/SKILL.md"),
    ],
    ids=["skills", "grok", "factory-md-prose"],
)
def test_the_factory_repo_ships_a_skills_grok_or_prose_pr_without_an_ok(
    tmp_path: Path,
    files: tuple[str, ...],
) -> None:
    config, _ = root_config()
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(snapshot(approve(), factory=config, files=files, trust=False))

    assert actions.handoffs == [HEAD]
    assert workflow_state(queue) == "merge-queue"
    assert not any(row["action"] == "awaiting-ok" for row in grok_jobs(queue))


def test_the_factory_repo_holds_a_factory_md_trust_change_until_max_oks_it(
    tmp_path: Path,
) -> None:
    config, _ = root_config()
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()

    def trust_edit(*comments):
        return snapshot(*comments, factory=config, files=("factory.md",), trust=True)

    dispatcher.process(trust_edit(approve()))
    assert_root_held(queue, actions)

    dispatcher.process(trust_edit(approve(), ok(HEAD, author=MAX)))
    assert actions.handoffs == [HEAD]
    assert workflow_state(queue) == "merge-queue"


def test_an_unread_factory_md_comparison_holds_the_factory_repo(tmp_path: Path) -> None:
    config, _ = root_config()
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(snapshot(approve(), factory=config, files=("factory.md",), trust=None))
    assert actions.handoffs == []
    assert workflow_state(queue) == "review"

    dispatcher.process(snapshot(approve(), factory=config, files=("factory.md",), trust=False))
    assert actions.handoffs == [HEAD]


def test_a_label_only_hold_blocks_a_handoff(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()

    dispatcher.process(
        snapshot(
            approve(),
            factory=hold_factory(ship="handoff", paths=()),
            labels=("factory:hold",),
        )
    )

    assert_held(queue, actions)
    assert actions.handoffs == []


@pytest.mark.parametrize("unread", ["labels", "files", "events"])
def test_an_unread_hold_input_on_a_handoff_repo_fails_closed(
    tmp_path: Path,
    unread: str,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(snapshot(approve(), factory=HANDOFF, **{unread: None}))
    assert actions.handoffs == [], f"unread {unread} holds"
    assert workflow_state(queue) == "review"

    dispatcher.process(snapshot(approve(), factory=HANDOFF))
    assert actions.handoffs == [HEAD], "a full read with no hold hands off"


@pytest.mark.parametrize("ci", ["failure", "pending"])
def test_red_ci_while_held_on_a_handoff_repo_keeps_the_approve(
    tmp_path: Path,
    ci: str,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    files = ("src/view/Board.tsx",)
    dispatcher.process(snapshot(approve(), factory=HANDOFF, files=files))
    assert workflow_state(queue) == "awaiting-ok"

    dispatcher.process(snapshot(approve(), ok(HEAD), factory=HANDOFF, files=files, ci=ci))
    assert actions.handoffs == []
    assert workflow_state(queue) == "awaiting-ok"

    for _ in range(2):
        dispatcher.process(snapshot(approve(), ok(HEAD), factory=HANDOFF, files=files))
    assert actions.handoffs == [HEAD], "green CI and the ok hand off on the same APPROVE"


class FailingHandoffActions(FakeReviewActions):
    def __init__(self) -> None:
        super().__init__()
        self.handoff_error: ShipError | None = None

    def handoff(self, ticket: TrackedTicket, head: str) -> None:
        if self.handoff_error is not None:
            raise self.handoff_error
        super().handoff(ticket, head)


def test_a_handoff_re_drive_waits_while_the_hold_applies_again(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path, FailingHandoffActions())
    ready(dispatcher, queue)
    error = ShipError("mark ready failed (HTTP 502)")
    error.write_confirmed = False
    actions.handoff_error = error
    cleared = (approve(), ok(HEAD))
    dispatcher.process(snapshot(*cleared, factory=HANDOFF))
    assert actions.handoffs == []
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT status, redrive FROM ship_intents WHERE ticket_id = '42'"
        ).fetchone()
    assert (row["status"], row["redrive"]) == ("failed", "eligible")

    actions.handoff_error = None
    relabeled = (labeled("grok-login", "e5", created_at=30),)
    for _ in range(2):
        dispatcher.process(
            snapshot(*cleared, factory=HANDOFF, labels=("factory:hold",), events=relabeled)
        )
    assert actions.handoffs == [], "a label put back after the ok holds the re-drive"
    dispatcher.process(snapshot(*cleared, factory=HANDOFF, labels=None))
    assert actions.handoffs == [], "an unread label list holds the re-drive"

    dispatcher.process(snapshot(*cleared, factory=HANDOFF))
    assert actions.handoffs == [HEAD]



@pytest.mark.parametrize("hooks", [(), (T8Hook(),)], ids=["no-hook", "t8-hook"])
def test_a_maxharris1_ok_at_the_head_hands_off_into_the_merge_queue(
    tmp_path: Path,
    hooks: tuple,
) -> None:
    dispatcher, queue, actions = setup(tmp_path, hooks=hooks)
    ready(dispatcher, queue)
    dispatcher.process(root_snapshot(approve()))
    assert_root_held(queue, actions)

    dispatcher.process(root_snapshot(approve(), ok(HEAD, author=MAX)))

    assert actions.handoffs == [HEAD]
    assert workflow_state(queue) == "merge-queue"
    assert not any(row["action"] == "rejected" for row in grok_jobs(queue))


def test_the_queue_merges_the_cleared_factory_repo_pr_at_the_head(tmp_path: Path) -> None:
    board = Board(tmp_path, trusted_logins=TRUSTED)
    board.factory, _ = root_config()
    pr = board.open("42", sha(1))
    pr.changed_files = ("orch.yml",)
    board.build_and_approve(pr)
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert board.actions.handoffs == []

    board.post(pr, "ok-1", OK.format(head=pr.head), MAX)
    board.poll()

    assert board.actions.handoffs == [sha(1)]
    assert board.actions.merges == [(True, sha(1))]
    assert board.state("42")["workflow_state"] == "merged"
    assert skip_facts(board.queue, "done"), "the done notice follows the merge"


@pytest.mark.parametrize(
    "held_ok",
    [
        ok(HEAD, author=MAX),
        ok(NEW_HEAD, author="grok-login"),
        ok(NEW_HEAD[:7], author=MAX),
    ],
    ids=["older-sha", "other-trusted-login", "short-sha"],
)
def test_an_ok_that_does_not_name_the_head_from_the_approver_stays_held(
    tmp_path: Path,
    held_ok,
) -> None:
    dispatcher, queue, actions = setup(
        tmp_path, FakeReviewActions({HEAD: "1" * 40, NEW_HEAD: "2" * 40})
    )
    ready(dispatcher, queue, NEW_HEAD)

    dispatcher.process(root_snapshot(approve(NEW_HEAD), held_ok, head=NEW_HEAD))

    assert_root_held(queue, actions, NEW_HEAD)


def test_a_new_head_after_the_ok_needs_a_new_review_and_a_new_ok(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(
        tmp_path, FakeReviewActions({HEAD: "1" * 40, NEW_HEAD: "2" * 40})
    )
    ready(dispatcher, queue)
    dispatcher.process(root_snapshot(approve()))
    assert workflow_state(queue) == "awaiting-ok"

    first_ok = ok(HEAD, author=MAX)
    dispatcher.process(root_snapshot(approve(), first_ok, head=NEW_HEAD))
    assert actions.handoffs == [], "a different patch after the ok is re-reviewed"
    assert workflow_state(queue) == "review"
    review = queue.claim("codex")
    assert review is not None and review.request.head == NEW_HEAD
    queue.finish(review.id, owner="codex", handoff_comment_id="approve-2")

    second = comment(
        "approve-2",
        VERDICT.format(round=1, head=NEW_HEAD, verdict="APPROVE", risk="high"),
        author="codex-login",
        created_at=30,
    )
    dispatcher.process(root_snapshot(approve(), first_ok, second, head=NEW_HEAD))
    assert actions.handoffs == [], "the old ok does not cover the new head"
    assert_root_held(queue, actions, NEW_HEAD)

    new_ok = comment("ok-2", OK.format(head=NEW_HEAD), author=MAX, created_at=40)
    dispatcher.process(root_snapshot(approve(), first_ok, second, new_ok, head=NEW_HEAD))
    assert actions.handoffs == [NEW_HEAD]
    assert workflow_state(queue) == "merge-queue"



@pytest.mark.parametrize(
    ("ship", "merges", "handoffs", "shipped"),
    [
        ("merge", [(True, HEAD)], [], "merged"),
        ("handoff", [], [HEAD], "handed-off"),
    ],
)
def test_a_held_pr_waits_then_ships_on_an_ok_at_the_head(
    tmp_path: Path,
    ship: str,
    merges: list,
    handoffs: list,
    shipped: str,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()
    factory = hold_factory(ship=ship)
    files = ("src/view/Board.tsx",)

    dispatcher.process(snapshot(approve(), factory=factory, files=files))
    assert_held(queue, actions)
    assert actions.handoffs == []

    dispatcher.process(snapshot(approve(), ok(HEAD), factory=factory, files=files))
    assert actions.merges == merges
    assert actions.handoffs == handoffs
    assert workflow_state(queue) == shipped



class GitHubShipActions(GitHubMergeActions):
    def handoff(self, ticket: TrackedTicket, head: str) -> None:
        self.github.handoff(ticket, head)
        super().handoff(ticket, head)


def test_a_held_draft_is_not_marked_ready_until_the_ok_then_merges_from_the_queue(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, hold=QUEUE_HOLD)
    github, gh = adapter()
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    gh.route(MERGE, served("merge-squash-200.json"))
    board.actions = board.dispatcher.review_actions = GitHubShipActions(board, github)

    pr = open_held(board, head=GH_HEAD)
    for _ in range(2):
        board.poll()
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert gh.count(MARK_READY) == 0, "no ready-mark while held"
    assert board.actions.merges == []

    board.post(pr, "ok-1", OK.format(head=GH_HEAD), QUEUE_APPROVER)
    board.poll()

    assert MARK_READY in gh.keys()
    assert gh.keys()[-1] == MERGE
    assert f"sha={GH_HEAD}" in gh.calls[-1]
    assert board.state("42")["workflow_state"] == "merged"


def test_a_label_put_back_in_the_merge_queue_holds_a_handoff_repo_until_a_new_ok(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, hold=HoldConfig(approver=QUEUE_APPROVER))
    pr = board.open("42", sha(1))
    pr.labels = ("factory:hold",)
    pr.label_events = (relabel(board, "e1"),)
    board.build_and_approve(pr, mergeable=None)
    assert board.state("42")["workflow_state"] == "awaiting-ok"

    board.post(pr, "ok-1", OK.format(head=pr.head), QUEUE_APPROVER)
    board.poll()
    assert board.state("42")["workflow_state"] == "merge-queue"
    assert board.actions.handoffs == [sha(1)]
    pr.label_events += (relabel(board, "e2"),)
    pr.mergeable, pr.mergeable_state = True, "clean"
    for _ in range(2):
        board.poll()
    assert board.actions.merges == []
    assert board.log[-1] == f"merge-queue: owner/repo#42 waits at {sha(1)[:12]}: held (label)"

    board.post(pr, "ok-2", OK.format(head=pr.head), QUEUE_APPROVER)
    board.poll()
    assert board.actions.merges == [(True, sha(1))]
    assert board.actions.handoffs == [sha(1)], "the queue does not hand off twice"


def test_a_ready_for_review_finish_line_holds_then_hands_off_on_the_ok(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, finish_line="ready-for-review", hold=QUEUE_HOLD)
    pr = open_held(board)
    board.poll()
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert board.actions.handoffs == []

    board.post(pr, "ok-1", OK.format(head=pr.head), QUEUE_APPROVER)
    board.poll()

    assert board.actions.handoffs == [sha(1)]
    assert board.actions.merges == []
    assert board.state("42")["workflow_state"] == "handed-off"


def edited(item):
    return dataclasses.replace(item, edited_at=item.created_at + 1)


@pytest.mark.parametrize("ship", ["handoff", "merge"])
def test_an_edited_ok_at_the_head_stays_held(tmp_path: Path, ship: str) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    config, _ = root_config()
    factory = dataclasses.replace(config, ship=ship)
    held = edited(ok(HEAD, author=MAX))

    dispatcher.process(snapshot(approve(), held, factory=factory, files=("orch.yml",)))

    assert actions.handoffs == []
    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"

    clean = ok(HEAD, author=MAX, comment_id="ok-2")
    dispatcher.process(snapshot(approve(), held, clean, factory=factory, files=("orch.yml",)))

    assert workflow_state(queue) != "awaiting-ok"


def test_the_queue_does_not_merge_on_a_stale_ok_edited_to_the_head(tmp_path: Path) -> None:
    board = Board(tmp_path, trusted_logins=TRUSTED)
    board.factory, _ = root_config()
    pr = board.open("42", sha(1))
    pr.changed_files = ("orch.yml",)
    board.build_and_approve(pr)
    board.post(pr, "ok-1", OK.format(head=sha(1)), MAX)
    pr.comments[-1] = edited(pr.comments[-1])
    board.poll()

    assert board.actions.handoffs == []
    assert board.actions.merges == []
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    assert not board.wakes("done")
