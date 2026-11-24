from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.dispatch.types import ParentMerged, ParentOpen
from lib.dispatcher import (
    CIResult,
    Dispatcher,
    RetryableShipError,
    ReviewShipper,
    ShipError,
    ShipRequest,
    TicketRef,
    TrackedTicket,
)
from lib.github_actions import LinearReviewActions
from lib.orch_yml import Intake, load_orch_yml
from lib.poll_source import FedPulls, LivePollSource
from lib.queue import SQLiteJobQueue
from lib.waiter import GhContextSource
from tests.merge_queue_support import Board, sha
from tests.regenerate_brief_snapshots import BOX_FACTORY_ROOT
from tests.test_brief import FACTORY_MD
from tests.test_feeder import (
    LINEAR_FACTORY_MD,
    LINEAR_ORCH,
    FakeGitHub,
    FakeLinear,
    dispatcher_for,
    make_feeder,
    rows,
)
from tests.test_feeder import issue as feeder_issue
from tests.test_feeder_stack_parent import PARENT, stacked
from tests.test_feeder_target import LINEAR_REPO as LINEAR_STACK_REPO
from tests.test_feeder_target import linear_ticket, targeted
from tests.test_dispatcher import (
    GO,
    READY,
    TRUSTED_LOGINS,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    factory,
    setup,
    ticket,
)
from tests.test_github_actions import RecordedGh
from tests.test_github_actions import pr as github_pr
from tests.test_linear_multi_project import transport_for
from tests.test_live_poll import FACTORY, FakeGh, config, linear_issue
from tests.test_waiter import FakeRun, contents

FEATURE = "feature/upload-only-intake"
HEAD = "a" * 40


def overridden(factory_ref: str = "main"):
    base = config(linear=True)
    (project,) = base.projects
    return replace(
        base,
        projects=(
            replace(
                project,
                factory_ref=factory_ref,
                linear_project_ids=frozenset({"project-1", "project-2"}),
                intake=Intake(linear_state="In Progress", base_branches={"project-2": FEATURE}),
            ),
        ),
    )


def polled(project: str, *, pr_base: str = "main", factory_ref: str = "main"):
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.pull = {"state": "open", "base": {"ref": pr_base}}
    errors: list = []
    source = LivePollSource(
        overridden(factory_ref),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=transport_for({"AUT-42": linear_issue(project=project)}),
        error_sink=lambda repo, ticket_id, error: errors.append(error),
    )
    found = source.tracked_tickets((TicketRef("acme/widgets", "AUT-42", "linear"),))
    return found, gh, errors


def test_a_ticket_in_the_overridden_project_carries_its_base() -> None:
    (upload,), gh, _ = polled("project-2", pr_base=FEATURE)
    (offer,), _, _ = polled("project-1")

    assert upload.factory.base_branch == FEATURE
    assert upload.pr_base == FEATURE
    assert offer.factory.base_branch == "main"
    assert offer.pr_base == "main"
    factory_reads = [call for call in gh.calls if any("contents/factory.md" in part for part in call)]
    assert factory_reads and all("ref=main" in call for call in factory_reads)


def test_a_factory_md_off_factory_ref_is_still_an_error_with_an_override() -> None:
    found, _, errors = polled("project-2", factory_ref="dev")

    assert found == ()
    assert any("not configured factory_ref dev" in str(error) for error in errors), errors


def test_the_waiter_reads_factory_md_from_factory_ref_with_an_override(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\nlinear:\n  api_key_env: LINEAR_KEY\n"
        "trusted_logins:\n  maxharris1: [grok-bot, claude-bot, codex-bot, cursor-bot]\n"
        "trusted_linear_users:\n  linear:user-1: [grok-bot]\n"
        "projects:\n  - repo: owner/repo\n    factory_ref: dev\n"
        "    linear_project_ids: [project-1, project-2]\n"
        f"    intake:\n      linear_state: In Progress\n      base_branches:\n        project-2: {FEATURE}\n",
        encoding="utf-8",
    )
    orch, findings = load_orch_yml(path)
    assert orch is not None, findings
    run = FakeRun(stdout=contents(FACTORY_MD))

    result = GhContextSource(orch, run=run, environ={}).load("owner/repo")

    assert result.context is not None and result.ref == "dev"
    assert run.calls[0][0][-1] == "ref=dev"
    mismatched = FakeRun(stdout=contents(FACTORY_MD.replace("base_branch: dev", "base_branch: main")))
    refused = GhContextSource(orch, run=mismatched, environ={}).load("owner/repo")
    assert refused.context is None and "not factory_ref dev" in (refused.note or "")
    unread = GhContextSource(orch, run=FakeRun(returncode=1), environ={}).load("owner/repo")
    assert unread.context is None and unread.ref == "dev"


def linear_dispatcher(tmp_path: Path) -> tuple[Dispatcher, SQLiteJobQueue]:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100)
    trusted = {**TRUSTED_LOGINS, "linear": {"linear:user-1": frozenset({"grok-bot"})}}
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=trusted,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
    )
    return dispatcher, queue


def brief(job) -> str:
    return render_brief(
        job,
        bot=f"{job.request.recipient}-bot",
        context=None,
        context_ref="main",
        context_note="test",
        factory_root=BOX_FACTORY_ROOT,
    )


def test_build_review_and_fix_run_against_the_tickets_base(tmp_path: Path) -> None:
    (upload,), _, _ = polled("project-2", pr_base=FEATURE)
    dispatcher, queue = linear_dispatcher(tmp_path)

    dispatcher.process(upload)
    build = queue.claim("claude")
    assert build is not None
    assert json.loads(build.request.detail or "{}")["base_branch"] == FEATURE
    build_brief = brief(build)
    assert f"git fetch origin {FEATURE}" in build_brief
    assert "origin/main" not in build_brief
    assert "read `factory.md` from branch main of acme/widgets" in build_brief
    queue.finish(build.id, owner="claude", handoff_comment_id="ready")

    ready = comment("ready", READY.format(head=HEAD), author="claude-login", created_at=2)
    dispatcher.process(
        replace(upload, comments=(*upload.comments, ready), current_head=HEAD,
                ci={HEAD: CIResult("success")})
    )
    review = queue.claim("codex")
    assert review is not None
    review_brief = brief(review)
    assert f"against `{FEATURE}`" in review_brief
    assert "against `main`" not in review_brief
    queue.finish(review.id, owner="codex", handoff_comment_id="changes")

    changes = comment(
        "changes",
        VERDICT.format(round="1/3", head=HEAD, verdict="REVISE", risk="high"),
        author="codex-login",
        created_at=3,
    )
    dispatcher.process(
        replace(upload, comments=(*upload.comments, ready, changes), current_head=HEAD,
                ci={HEAD: CIResult("success")})
    )
    fix = queue.claim("claude")
    assert fix is not None and fix.request.action == "fix"
    fix_brief = brief(fix)
    assert f"git fetch origin {FEATURE}" in fix_brief
    assert "origin/main" not in fix_brief


def attention(tmp_path: Path) -> dict[str, str]:
    with sqlite3.connect(tmp_path / "d.db") as connection:
        return dict(connection.execute("SELECT reason, detail FROM needs_attention").fetchall())


def on_feature(**fields):
    return replace(
        ticket(comment("go-1", GO.format(state="building"), author="grok-login")),
        factory=replace(factory(), base_branch=FEATURE),
        pr_number="7",
        **fields,
    )


def test_a_pr_on_another_base_is_flagged_and_starts_nothing_until_retargeted(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)

    dispatcher.process(on_feature(pr_base="main"))

    assert queue.claim("claude") is None
    assert attention(tmp_path) == {
        "base-mismatch": f"PR #7 targets main; the ticket base is {FEATURE}"
    }

    dispatcher.process(on_feature(pr_base=FEATURE))

    job = queue.claim("claude")
    assert job is not None and job.request.action == "build"
    assert attention(tmp_path) == {}


def test_a_ship_refuses_a_pr_on_another_base(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    with queue.connect() as connection:
        connection.execute("INSERT INTO tracked_tickets VALUES ('owner/repo', '42', 'github', 0)")

    def shipper(pr_base: str) -> ReviewShipper:
        class Source:
            def tracked_tickets(self, wanted):
                return (on_feature(pr_base=pr_base),)

        return ReviewShipper(dispatcher, Source())

    request = ShipRequest("owner/repo", "42", HEAD, "max", "ok-1", "verdict-1")
    with pytest.raises(ShipError, match=f"PR #7 targets main; the ticket base is {FEATURE}"):
        shipper("main")._ticket(request)
    assert shipper(FEATURE)._ticket(request).factory.base_branch == FEATURE


def test_a_new_head_is_rebase_checked_against_the_tickets_base(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.factory = replace(board.factory, base_branch=FEATURE)
    checked: list[tuple[str, str, str, str]] = []
    board.dispatcher.rebase_classifier = lambda *args: checked.append(args)
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, sha(1))
    board.poll()
    board.finish("codex")
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")

    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()

    assert checked == [("owner/repo", FEATURE, sha(1), sha(2))]


@pytest.mark.usefixtures("every_wake_reaches_grok")
def test_a_conflicting_approved_pr_is_updated_from_the_tickets_base(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.factory = replace(board.factory, base_branch=FEATURE)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)

    [update] = board.jobs("update-from-main")
    assert json.loads(update["detail"])["base_branch"] == FEATURE
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()

    assert board.classified == [("owner/repo", FEATURE, sha(1), sha(2))]


def on_live_base(board: Board, live) -> None:
    board.factory = replace(board.factory, base_branch=FEATURE)
    snapshot = board.snapshot
    board.snapshot = lambda pr, fresh=False: live(pr, snapshot(pr, fresh=fresh), fresh)


def bases_of(board: Board) -> dict[str, str]:
    bases: dict[str, str] = {}
    on_live_base(
        board, lambda pr, read, fresh: replace(read, pr_base=bases.get(pr.number, FEATURE))
    )
    return bases


def poll_errors(tmp_path: Path) -> list[str]:
    with sqlite3.connect(tmp_path / "d.db") as connection:
        return [row[0] for row in connection.execute("SELECT error FROM poll_errors")]


def approved_in_queue(board: Board, number: str):
    pr = board.open(number, sha(int(number)))
    board.build_and_approve(pr, mergeable=None)
    assert board.state(number)["workflow_state"] == "merge-queue"
    return pr


@pytest.mark.usefixtures("every_wake_reaches_grok")
def test_a_queued_pr_moved_to_another_base_merges_only_once_retargeted(tmp_path: Path) -> None:
    board = Board(tmp_path)
    bases = bases_of(board)
    pr = approved_in_queue(board, "42")

    bases["42"] = "dev"
    pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()

    assert not pr.merged
    assert attention(tmp_path)["base-mismatch"] == (
        f"PR #42 targets dev; the ticket base is {FEATURE}"
    )
    assert poll_errors(tmp_path) == []

    bases["42"] = FEATURE
    board.poll()

    assert pr.merged
    assert "base-mismatch" not in attention(tmp_path)


@pytest.mark.usefixtures("every_wake_reaches_grok")
def test_a_conflicting_queued_pr_on_another_base_gets_no_update(tmp_path: Path) -> None:
    board = Board(tmp_path)
    bases = bases_of(board)
    pr = approved_in_queue(board, "42")

    bases["42"] = "dev"
    pr.conflict()
    board.poll()

    assert board.jobs("update-from-main") == []
    assert poll_errors(tmp_path) == []

    bases["42"] = FEATURE
    board.poll()

    assert len(board.jobs("update-from-main")) == 1


@pytest.mark.usefixtures("every_wake_reaches_grok")
def test_an_entry_on_another_base_never_merges_ahead_of_a_waiting_head(tmp_path: Path) -> None:
    board = Board(tmp_path)
    bases = bases_of(board)
    head = approved_in_queue(board, "1")
    behind = approved_in_queue(board, "2")
    head.conflict()
    behind.mergeable, behind.mergeable_state = True, "clean"

    bases["2"] = "dev"
    board.poll()

    assert board.actions.merges == []
    assert board.state("2")["workflow_state"] == "merge-queue"
    assert poll_errors(tmp_path) == []

    bases["2"] = FEATURE
    board.poll()

    assert board.actions.merges == [(True, behind.head)]


def test_a_ready_whose_fresh_read_shows_another_base_starts_no_review(tmp_path: Path) -> None:
    board = Board(tmp_path)
    live = ["dev"]

    def lagging(pr, read, fresh):
        if fresh:
            return replace(read, pr_base=live[0])
        return replace(read, pr_base=FEATURE, current_head=sha(1))

    on_live_base(board, lagging)
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    pr.push(sha(2))
    ready = board.ready(pr, sha(2))
    board.poll()

    assert board.jobs("review") == []
    assert board.outcome(ready) is None
    assert poll_errors(tmp_path) == []

    live[0] = FEATURE
    board.poll()

    assert len(board.jobs("review")) == 1


def test_a_fresh_base_mismatch_stops_later_comments_in_the_same_poll(tmp_path: Path) -> None:
    board = Board(tmp_path)
    on_live_base(board, lambda pr, read, fresh: replace(read, pr_base=FEATURE))
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, sha(1))
    board.poll()
    board.finish("codex")
    on_live_base(
        board,
        lambda pr, read, fresh: replace(
            read,
            pr_base="dev" if fresh else FEATURE,
            current_head=sha(2) if fresh else sha(1),
        ),
    )
    pr.push(sha(2))
    ready = board.ready(pr, sha(2))
    board.post(
        pr,
        "late-verdict",
        VERDICT.format(round="1/3", head=sha(1), verdict="REVISE", risk="high"),
        "codex-login",
    )
    board.poll()

    assert board.outcome(ready) is None
    assert board.outcome("late-verdict") is None
    assert board.jobs("fix") == []
    assert attention(tmp_path)["base-mismatch"] == (
        f"PR #42 targets dev; the ticket base is {FEATURE}"
    )


def job_events(tmp_path: Path, event: str) -> list[str]:
    with sqlite3.connect(tmp_path / "d.db") as connection:
        return [
            row[0]
            for row in connection.execute("SELECT job_id FROM job_events WHERE event = ?", (event,))
        ]


@pytest.mark.parametrize(("recipient", "action"), [("claude", "build"), ("codex", "review")])
def test_a_failed_job_on_another_base_is_retried_only_once_retargeted(
    tmp_path: Path, recipient: str, action: str
) -> None:
    board = Board(tmp_path)
    bases = bases_of(board)
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    if action == "review":
        board.finish("claude")
        board.ready(pr, pr.head)
        board.poll()

    bases["42"] = "dev"
    failed = board.fail(recipient)
    board.poll()
    board.poll()

    assert [job["attempt"] for job in board.jobs(action)] == [1]
    assert job_events(tmp_path, "base-mismatch") == [failed.id]

    bases["42"] = FEATURE
    board.poll()

    assert [job["attempt"] for job in board.jobs(action)] == [1, 2]


def test_a_linear_handoff_whose_fresh_read_shows_another_base_writes_nothing() -> None:
    gh = RecordedGh()
    linear_writes: list[str] = []
    linked = replace(
        github_pr("handoff", tickets="linear", ticket_id="AUT-42"),
        pr_number="42",
        pr_base="main",
        linear_ready_for_review=True,
        linear_state_phase="before-review",
    )
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=lambda ticket: linear_writes.append(ticket.ticket_id),
        linear_reopen=lambda ticket: None,
        fresh_ticket=lambda ref: replace(linked, pr_base="dev"),
        runner=gh,
    )

    with pytest.raises(RetryableShipError, match="PR #42 targets dev; the ticket base is main"):
        actions.handoff(linked, HEAD)

    assert gh.calls == []
    assert linear_writes == []


STACK_REPO = "owner/app"
PARENT_PR = 113
CHILD_PR = 501


class CountingGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.parent_reads: list[int] = []

    def pull_landing(self, repo: str, number: int):
        self.parent_reads.append(number)
        return super().pull_landing(repo, number)


def fed_child(tmp_path: Path, *, target: str | None = PARENT, text: str | None = None):
    github = CountingGitHub()
    github.branches[(STACK_REPO, PARENT)] = sha(2)
    github.branch_commits[(STACK_REPO, PARENT)] = (sha(1), sha(2))
    github.branches[(STACK_REPO, FEATURE)] = sha(6)
    github.pulls[(STACK_REPO, PARENT_PR)] = {"head": PARENT, "base": "main", "title": "t", "body": ""}
    github.landings[(STACK_REPO, PARENT_PR)] = ParentOpen()
    feeder, queue, _, _ = make_feeder(tmp_path, github=github)
    text = text or (stacked(target, PARENT_PR) if target else targeted(""))
    github.issues[STACK_REPO] = [feeder_issue(7, body=text, template=False)]
    assert len(feeder.feed_once().fed) == 1
    github.parent_reads.clear()
    dispatcher = dispatcher_for(tmp_path, queue, feeder=feeder)
    dispatcher.stack_bounds = lambda repo, old_head, parent_heads: sha(9)
    return feeder, queue, github, dispatcher


def merge_parent(github: FakeGitHub, *, into: str = "main") -> None:
    github.pulls[(STACK_REPO, PARENT_PR)].update(merged=True, state="closed", base=into)
    github.landings[(STACK_REPO, PARENT_PR)] = ParentMerged(into=into, heads=(sha(2),))
    del github.branches[(STACK_REPO, PARENT)]


def child_row(tmp_path: Path) -> dict:
    return rows(tmp_path / "orch.db")["7"]


def polled_child(tmp_path: Path, github: FakeGitHub) -> TrackedTicket:
    return TrackedTicket(
        repo=STACK_REPO,
        ticket_id=str(CHILD_PR),
        factory=replace(factory(), base_branch=child_row(tmp_path)["base_branch"]),
        comments=tuple(github.comments(STACK_REPO, CHILD_PR)),
        current_head=sha(3),
        ci={},
        risk="low",
        pr_number=str(CHILD_PR),
        pr_base=github.pulls[(STACK_REPO, CHILD_PR)]["base"],
    )


def stack_attention(tmp_path: Path) -> dict[str, str]:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        return dict(connection.execute("SELECT reason, detail FROM needs_attention").fetchall())


def stack_poll_errors(tmp_path: Path) -> list[str]:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        return [row[0] for row in connection.execute("SELECT error FROM poll_errors")]


def test_a_stacked_child_retargeted_after_its_parent_merged_runs_on_the_new_base(
    tmp_path: Path,
) -> None:
    _, queue, github, dispatcher = fed_child(tmp_path)
    assert child_row(tmp_path)["base_branch"] == PARENT
    merge_parent(github)
    github.pulls[(STACK_REPO, CHILD_PR)]["base"] = "main"

    dispatcher.process(polled_child(tmp_path, github))

    assert stack_attention(tmp_path) == {}
    (job,) = queue.outstanding()
    assert json.loads(job.request.detail)["base_branch"] == "main"
    assert child_row(tmp_path)["base_branch"] == "main"
    assert github.parent_reads == [PARENT_PR]

    dispatcher.process(polled_child(tmp_path, github))

    assert github.parent_reads == [PARENT_PR], "an accepted base costs no further parent reads"
    assert stack_attention(tmp_path) == {}


def test_an_earlier_base_mismatch_clears_once_the_retarget_is_accepted(tmp_path: Path) -> None:
    _, queue, github, dispatcher = fed_child(tmp_path)
    github.pulls[(STACK_REPO, CHILD_PR)]["base"] = "main"

    dispatcher.process(polled_child(tmp_path, github))

    assert set(stack_attention(tmp_path)) == {"base-mismatch"}
    assert queue.outstanding() == ()

    merge_parent(github)
    dispatcher.process(polled_child(tmp_path, github))

    assert stack_attention(tmp_path) == {}
    assert [json.loads(job.request.detail)["base_branch"] for job in queue.outstanding()] == ["main"]


def unaccepted(tmp_path: Path, github: FakeGitHub, queue, dispatcher, moved_to: str) -> None:
    before = child_row(tmp_path)
    github.pulls[(STACK_REPO, CHILD_PR)]["base"] = moved_to

    dispatcher.process(polled_child(tmp_path, github))

    recorded = before["base_branch"]
    assert stack_attention(tmp_path) == {
        "base-mismatch": f"PR #{CHILD_PR} targets {moved_to}; the ticket base is {recorded}"
    }
    assert queue.outstanding() == ()
    assert child_row(tmp_path) == before


def test_a_human_retarget_of_an_unstacked_pr_stays_base_mismatch_with_the_feeder_wired(
    tmp_path: Path,
) -> None:
    _, queue, github, dispatcher = fed_child(tmp_path, target=None)
    assert child_row(tmp_path)["stack_parent_pr"] is None
    merge_parent(github)

    unaccepted(tmp_path, github, queue, dispatcher, "release")

    assert github.parent_reads == []
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO tracked_tickets VALUES (?, ?, 'github', 0)", (STACK_REPO, str(CHILD_PR))
        )

    class Source:
        def tracked_tickets(self, wanted):
            return (polled_child(tmp_path, github),)

    request = ShipRequest(STACK_REPO, str(CHILD_PR), HEAD, "max", "ok-1", "verdict-1")
    with pytest.raises(ShipError, match=f"PR #{CHILD_PR} targets release; the ticket base is main"):
        ReviewShipper(dispatcher, Source())._ticket(request)


def test_a_stacked_child_moved_while_its_parent_is_open_stays_base_mismatch(tmp_path: Path) -> None:
    _, queue, github, dispatcher = fed_child(tmp_path)

    unaccepted(tmp_path, github, queue, dispatcher, "main")


def test_a_child_moved_off_its_parents_merge_target_stays_base_mismatch(tmp_path: Path) -> None:
    _, queue, github, dispatcher = fed_child(tmp_path)
    merge_parent(github, into="release")

    unaccepted(tmp_path, github, queue, dispatcher, "main")


def test_an_unreadable_parent_stays_base_mismatch_and_records_the_error(tmp_path: Path) -> None:
    _, queue, github, dispatcher = fed_child(tmp_path)
    del github.landings[(STACK_REPO, PARENT_PR)]

    unaccepted(tmp_path, github, queue, dispatcher, "main")

    assert stack_poll_errors(tmp_path) == ["KeyError"]


def test_a_stacked_child_reads_its_merged_parents_heads_feed_head_first(tmp_path: Path) -> None:
    feeder, _, github, _ = fed_child(tmp_path)
    github.landings[(STACK_REPO, PARENT_PR)] = ParentMerged(into="main", heads=(sha(4), sha(2), sha(5)))

    assert feeder.stack_parent_facts(polled_child(tmp_path, github)) == ParentMerged(
        into="main", heads=(sha(2), sha(4), sha(5))
    )


def test_an_open_stacked_parent_reads_as_open(tmp_path: Path) -> None:
    feeder, _, github, _ = fed_child(tmp_path)
    github.landings[(STACK_REPO, PARENT_PR)] = ParentOpen()

    assert feeder.stack_parent_facts(polled_child(tmp_path, github)) == ParentOpen()


def test_an_unstacked_ticket_has_no_parent_facts_and_reads_nothing(tmp_path: Path) -> None:
    feeder, _, github, _ = fed_child(tmp_path, target=None)

    assert feeder.stack_parent_facts(polled_child(tmp_path, github)) is None
    assert not [call for call in github.calls if call.startswith("landing:")]


def test_a_linear_child_takes_its_parents_merge_target(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.branches[(LINEAR_STACK_REPO, PARENT)] = sha(2)
    github.branch_commits[(LINEAR_STACK_REPO, PARENT)] = (sha(1), sha(2))
    github.pulls[(LINEAR_STACK_REPO, PARENT_PR)] = {"head": PARENT, "base": "dev", "title": "t", "body": ""}
    linear = FakeLinear()
    linear.issues = [linear_ticket(stacked(PARENT, PARENT_PR))]
    feeder, _, _, _ = make_feeder(
        tmp_path, orch=LINEAR_ORCH, github=github, linear=linear, factory_text=LINEAR_FACTORY_MD
    )
    assert len(feeder.feed_once().fed) == 1
    github.landings[(LINEAR_STACK_REPO, PARENT_PR)] = ParentMerged(into="dev", heads=(sha(4),))
    child = TrackedTicket(
        repo=LINEAR_STACK_REPO, ticket_id="AUT-7",
        factory=replace(factory(tickets="linear"), base_branch=PARENT),
        comments=(), current_head=sha(3), ci={}, risk="low", pr_number=str(CHILD_PR), pr_base="dev",
    )

    assert feeder.stack_parent_facts(child) == ParentMerged(into="dev", heads=(sha(2), sha(4)))
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        assert feeder.move_stack_base(connection, child, "dev", sha(9))
        assert not feeder.move_stack_base(connection, child, "dev", sha(9))
        assert feeder.stack_onto(connection, child) == sha(9)
    assert FedPulls(tmp_path / "orch.db").base(LINEAR_STACK_REPO, "AUT-7") == "dev"
    assert feeder.stack_parent_facts(child) is None


def github_poll(tmp_path: Path, number: int, *, factory_ref: str = "main") -> TrackedTicket:
    base = config()
    (project,) = base.projects
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff").replace("main", factory_ref))
    gh.pull = {"state": "open", "base": {"ref": "main"}, "head": {"ref": "x"}}
    source = LivePollSource(
        replace(base, projects=(replace(project, repo=STACK_REPO, factory_ref=factory_ref),)),
        gh_runner=gh,
        fed_pulls=FedPulls(tmp_path / "orch.db"),
    )
    source.begin_cycle()
    (found,) = source.tracked_tickets((TicketRef(STACK_REPO, str(number), "github"),))
    return found


def test_a_github_ticket_fed_on_a_target_branch_polls_with_its_recorded_base(
    tmp_path: Path,
) -> None:
    fed_child(tmp_path)

    assert github_poll(tmp_path, CHILD_PR).factory.base_branch == PARENT


def test_a_github_ticket_fed_on_the_project_base_polls_with_factory_ref(tmp_path: Path) -> None:
    fed_child(tmp_path, target=None)

    assert child_row(tmp_path)["base_branch"] == "main"
    assert github_poll(tmp_path, CHILD_PR, factory_ref="dev").factory.base_branch == "dev"


def retargeted_after(tmp_path: Path, github: FakeGitHub, dispatcher) -> None:
    merge_parent(github)
    github.pulls[(STACK_REPO, CHILD_PR)]["base"] = "main"
    dispatcher.process(polled_child(tmp_path, github))
    assert child_row(tmp_path)["base_branch"] == "main"
    assert stack_attention(tmp_path) == {}


def test_a_build_that_failed_before_the_retarget_retries_on_the_new_base(tmp_path: Path) -> None:
    _, queue, github, dispatcher = fed_child(tmp_path)
    dispatcher.process(polled_child(tmp_path, github))
    failed = queue.claim("claude")
    queue.finish(failed.id, owner="claude", handoff_comment_id=None)

    retargeted_after(tmp_path, github, dispatcher)
    dispatcher.reconcile_jobs()

    retry = queue.claim("claude")
    assert retry.request.attempt == 2
    assert json.loads(retry.request.detail)["base_branch"] == "main"


def test_a_build_queued_before_the_retarget_runs_on_the_new_base(tmp_path: Path) -> None:
    _, queue, github, dispatcher = fed_child(tmp_path)
    dispatcher.process(polled_child(tmp_path, github))

    retargeted_after(tmp_path, github, dispatcher)

    job = queue.claim("claude")
    assert json.loads(job.request.detail)["base_branch"] == "main"


def test_a_build_running_across_the_retarget_retries_on_the_new_base(tmp_path: Path) -> None:
    _, queue, github, dispatcher = fed_child(tmp_path)
    dispatcher.process(polled_child(tmp_path, github))
    running = queue.claim("claude")

    retargeted_after(tmp_path, github, dispatcher)
    queue.finish(running.id, owner="claude", handoff_comment_id=None)
    dispatcher.reconcile_jobs()

    retry = queue.claim("claude")
    assert retry.request.attempt == 2
    assert json.loads(retry.request.detail)["base_branch"] == "main"
