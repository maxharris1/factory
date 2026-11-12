"""Lessons batch 1, item 2: factory PR review handoffs are scripted.

maxharris1/factory is a GitHub-only project (no Linear). Its PRs are built
outside the queue, so a project can opt in with
projects[].ready_starts_review: a trusted ready from factory.md's builder
then queues the review even with no factory-queued build job. From there
the loop is the one Aut0 uses: review and fix jobs go through the queue
that `orch wait` claims, and an APPROVE with green CI on a ship: handoff
repo hands off (no merge) and wakes the project bot for the merge.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, TrackedTicket
from lib.factory_md import load_factory_md
from lib.github_actions import GitHubReviewActions
from lib.orch_yml import Project, load_orch_yml
from lib.poll_source import LivePollSource
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    READY,
    VERDICT,
    FakeReviewActions,
    comment,
    factory,
    setup,
    ticket,
)
from tests.test_live_poll import FACTORY, FakeGh, config

ROOT = Path(__file__).resolve().parents[1]
FACTORY_REPO = "maxharris1/factory"
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40


# Config: the shipped orch.yml and this repo's own factory.md.


def test_shipped_orch_yml_adds_the_factory_repo_as_a_github_only_project(
    tmp_path: Path,
) -> None:
    configured = tmp_path / "orch.yml"
    configured.write_text(
        (ROOT / "orch.yml").read_text(encoding="utf-8").replace(
            "REPLACE_WITH_LINEAR_PROJECT_ID", "test-linear-project-id"
        ),
        encoding="utf-8",
    )
    config_, findings = load_orch_yml(configured)
    assert findings == []
    assert config_ is not None
    projects = {project.repo: project for project in config_.projects}
    factory_project = projects[FACTORY_REPO]
    assert factory_project.factory_ref == "main"
    assert factory_project.linear_project_id is None
    assert factory_project.linear_ready_for_review is False
    assert factory_project.grok_bot == "grok-bot"
    assert factory_project.ready_starts_review is True
    assert "maxharris1" in config_.trusted_logins
    for repo, project in projects.items():
        if repo != FACTORY_REPO:
            assert project.ready_starts_review is False, repo


def test_this_repos_factory_md_hands_off_instead_of_merging() -> None:
    config_, findings = load_factory_md(ROOT / "factory.md")

    assert [item for item in findings if item.severity == "error"] == []
    assert config_ is not None
    assert config_.tickets == "github"
    assert config_.base_branch == "main"
    assert config_.ship == "handoff"
    assert config_.builder_harness == "claude"
    assert config_.reviewer_harness == "codex"
    assert (config_.rounds_low, config_.rounds_high) == (1, 3)
    assert config_.github_issues is False


def orch_text(extra: str) -> str:
    return (
        "poll_seconds: 60\n"
        "trusted_logins:\n"
        "  someone: [grok-bot]\n"
        "projects:\n"
        "  - repo: acme/widgets\n"
        "    factory_ref: main\n"
        f"{extra}"
    )


@pytest.mark.parametrize(
    ("extra", "value"),
    [("", False), ("    ready_starts_review: true\n", True), ("    ready_starts_review: false\n", False)],
    ids=["default", "true", "false"],
)
def test_ready_starts_review_parses_and_defaults_off(
    tmp_path: Path,
    extra: str,
    value: bool,
) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(orch_text(extra), encoding="utf-8")

    config_, findings = load_orch_yml(path)

    assert findings == []
    assert config_ is not None
    assert config_.projects[0].ready_starts_review is value


@pytest.mark.parametrize("raw", ['"yes"', "1", "null"])
def test_ready_starts_review_must_be_a_boolean(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(orch_text(f"    ready_starts_review: {raw}\n"), encoding="utf-8")

    config_, findings = load_orch_yml(path)

    assert config_ is None
    assert [item.message for item in findings] == [
        "projects[0].ready_starts_review must be true or false"
    ]


# Poll source: the flag reaches the ticket, and the project gets the plain
# GitHub adapter (no Linear writes).


def test_github_ticket_carries_the_projects_ready_starts_review_flag() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    base = config()
    enabled = replace(
        base,
        projects=(Project("acme/widgets", factory_ref="main", ready_starts_review=True),),
    )
    for orch, expected in ((base, False), (enabled, True)):
        source = LivePollSource(orch, gh_runner=gh)
        project = orch.projects[0]
        tracked = source._github_ticket(project, source._factory(project), "12")
        assert tracked.ready_starts_review is expected
        assert type(source.review_actions_by_repo()["acme/widgets"]) is GitHubReviewActions


# Dispatcher: T2 intake, and the full scripted loop.


def ready_ticket(
    *comments,
    head: str = HEAD_1,
    enabled: bool = True,
    ship: str = "handoff",
) -> TrackedTicket:
    return replace(
        ticket(
            *comments,
            head=head,
            ci={head: CIResult("success")},
            factory_config=factory(ship=ship),
        ),
        ready_starts_review=enabled,
    )


def outcomes(queue: SQLiteJobQueue) -> dict[str, str]:
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT comment_id, outcome FROM handled_comments"
        ).fetchall()
    return {str(row["comment_id"]): str(row["outcome"]) for row in rows}


def test_a_trusted_builder_ready_starts_review_when_the_project_opts_in(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)

    dispatcher.process(
        ready_ticket(comment("ready-1", READY.format(head=HEAD_1), author="claude-login"))
    )

    assert outcomes(queue) == {"ready-1": "queued-review"}
    review = queue.claim("codex")
    assert review is not None
    assert (review.request.action, review.request.head) == ("review", HEAD_1)
    assert json.loads(str(review.request.detail))["round"] == "1/3"


def test_without_the_flag_a_ready_with_no_builder_job_is_still_refused(
    tmp_path: Path,
) -> None:
    """Preservation guard: the default intake rule is unchanged."""
    dispatcher, queue = setup(tmp_path)

    dispatcher.process(
        ready_ticket(
            comment("ready-1", READY.format(head=HEAD_1), author="claude-login"),
            enabled=False,
        )
    )

    assert outcomes(queue) == {"ready-1": "rejected-ready-without-builder-job"}
    assert queue.claim("codex") is None


@pytest.mark.parametrize(
    ("author", "body"),
    [
        ("codex-login", READY.format(head=HEAD_1)),
        ("claude-login", READY.format(head=HEAD_1).replace("claude-bot", "codex-bot")),
        ("claude-login", READY.format(head=HEAD_1).replace("role=builder", "role=reviewer")),
        ("mallory", READY.format(head=HEAD_1)),
    ],
    ids=["other-login", "wrong-from", "wrong-role", "untrusted-login"],
)
def test_the_flag_still_needs_the_factory_md_builder_and_a_trusted_login(
    tmp_path: Path,
    author: str,
    body: str,
) -> None:
    dispatcher, queue = setup(tmp_path)

    dispatcher.process(ready_ticket(comment("ready-1", body, author=author)))

    assert outcomes(queue) == {"ready-1": "rejected-ready-author"}
    assert queue.claim("codex") is None


def test_factory_pr_loop_review_fix_review_approve_hands_off_and_wakes_grok(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    ready_1 = comment("ready-1", READY.format(head=HEAD_1), author="claude-login", created_at=1)

    dispatcher.process(ready_ticket(ready_1))
    review_1 = queue.claim("codex")
    assert review_1 is not None and review_1.request.action == "review"
    queue.finish(review_1.id, owner="codex", handoff_comment_id="revise-1")

    revise = comment(
        "revise-1",
        VERDICT.format(round="1/3", head=HEAD_1, verdict="REVISE", risk="high"),
        author="codex-login",
        created_at=2,
    )
    dispatcher.process(ready_ticket(ready_1, revise))
    fix = queue.claim("claude")
    assert fix is not None
    assert (fix.request.action, fix.request.head) == ("fix", HEAD_1)
    queue.finish(fix.id, owner="claude", handoff_comment_id="ready-2")

    ready_2 = comment("ready-2", READY.format(head=HEAD_2), author="claude-login", created_at=3)
    dispatcher.process(ready_ticket(ready_1, revise, ready_2, head=HEAD_2))
    review_2 = queue.claim("codex")
    assert review_2 is not None
    assert (review_2.request.action, review_2.request.head) == ("review", HEAD_2)
    assert json.loads(str(review_2.request.detail))["round"] == "2/3"
    queue.finish(review_2.id, owner="codex", handoff_comment_id="approve-2")

    approve = comment(
        "approve-2",
        VERDICT.format(round="2/3", head=HEAD_2, verdict="APPROVE", risk="high"),
        author="codex-login",
        created_at=4,
    )
    dispatcher.process(ready_ticket(ready_1, revise, ready_2, approve, head=HEAD_2))

    assert outcomes(queue)["approve-2"] == "handed-off"
    assert actions.handoffs == [HEAD_2]
    assert actions.merges == []
    assert actions.fast_forwards == []
    assert actions.states[-1] == "handed-off"
    grok = queue.claim("grok")
    assert grok is not None
    assert grok.request.action == "done"
    detail = json.loads(str(grok.request.detail))
    assert (detail["outcome"], detail["project_bot"]) == ("handed-off", "grok-bot")
