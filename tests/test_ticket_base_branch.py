from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.dispatcher import CIResult, Dispatcher, ReviewShipper, ShipError, ShipRequest, TicketRef
from lib.orch_yml import Intake, load_orch_yml
from lib.poll_source import LivePollSource
from lib.queue import SQLiteJobQueue
from lib.waiter import GhContextSource
from tests.merge_queue_support import Board, sha
from tests.regenerate_brief_snapshots import BOX_FACTORY_ROOT
from tests.test_brief import FACTORY_MD
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
