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

from lib.dispatcher import CIResult, Dispatcher, TicketRef, TrackedTicket
from lib.factory_md import load_factory_md
from lib.github_actions import GitHubReviewActions
from lib.orch_yml import OrchConfig, Project, load_orch_yml
from lib.poll_source import LivePollSource
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    BRIEF,
    READY,
    VERDICT,
    FakeGrokDelivery,
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


def tracked_rows(queue: SQLiteJobQueue) -> list[tuple[str, str, str]]:
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT repo, ticket_id, tracker FROM tracked_tickets ORDER BY ticket_id"
        ).fetchall()
    return [tuple(row) for row in rows]


class OnePrSource:
    """A PollSource serving one labelled open PR, as github_candidates would."""

    def __init__(self) -> None:
        self.ticket: TrackedTicket | None = None

    def poll(self, dispatcher: Dispatcher, ticket: TrackedTicket) -> None:
        self.ticket = ticket
        dispatcher.poll_once(self)

    def github_candidates(self, label_prefix: str) -> tuple[TrackedTicket, ...]:
        del label_prefix
        return () if self.ticket is None else (self.ticket,)

    def linear_candidates(
        self,
        query: str,
        *,
        exclude: frozenset[str],
        reserve: int,
    ) -> tuple[TrackedTicket, ...]:
        del query, exclude, reserve
        return ()

    def tracked_tickets(self, refs: tuple[TicketRef, ...]) -> tuple[TrackedTicket, ...]:
        if self.ticket is None:
            return ()
        wanted = {(ref.repo.lower(), ref.ticket_id) for ref in refs}
        key = (self.ticket.repo.lower(), self.ticket.ticket_id)
        return (self.ticket,) if key in wanted else ()


# Discovery (Codex r1 F1.1): the documented entry is an orch:state/ label
# plus the builder's trusted ready naming the PR head (factory.md Context,
# README "Poll", SPEC section 7). These run the live poll source against a
# fake gh, through poll_once, so nothing is tracked by hand.

PR_HEAD = "f" * 40
DOCUMENTED_READY = (
    "<!-- orch:v1 kind=ready from=claude-bot role=builder head={head} -->\n"
    "Ready for review.\n"
)


def factory_pr_poll(
    tmp_path: Path,
    body: str,
    *,
    author: str = "maxharris1",
    enabled: bool = True,
    labels: tuple[str, ...] = ("orch:state/intake",),
) -> tuple[SQLiteJobQueue, FakeReviewActions]:
    gh = FakeGh((ROOT / "factory.md").read_text(encoding="utf-8"))
    gh.head = PR_HEAD
    gh.issues = [
        {
            "number": 51,
            "pull_request": {},
            "labels": [{"name": name} for name in labels],
        }
    ]
    gh.comments = [
        {
            "id": 9001,
            "user": {"login": author},
            "body": body,
            "created_at": "2026-11-12T10:00:00Z",
            "html_url": "https://github.test/maxharris1/factory/pull/51#c9001",
        }
    ]
    source = LivePollSource(
        OrchConfig(
            poll_seconds=60,
            projects=(
                Project(
                    FACTORY_REPO,
                    factory_ref="main",
                    ready_starts_review=enabled,
                ),
            ),
        ),
        gh_runner=gh,
        sleep=lambda seconds: None,
    )
    path = tmp_path / "orch.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    actions = FakeReviewActions()
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins={
            "github": {
                # The shipped orch.yml: one login for all of Max's bots.
                "maxharris1": {"grok-bot", "claude-bot", "codex-bot", "cursor-bot"},
            },
            "linear": {},
        },
        review_actions=actions,
        grok_delivery=FakeGrokDelivery(),
    )
    dispatcher.poll_once(source)
    return queue, actions


def test_a_labelled_factory_pr_with_only_a_builder_ready_is_tracked_and_reviewed(
    tmp_path: Path,
) -> None:
    queue, actions = factory_pr_poll(tmp_path, DOCUMENTED_READY.format(head=PR_HEAD))

    assert tracked_rows(queue) == [(FACTORY_REPO, "51", "github")]
    assert outcomes(queue) == {"9001": "queued-review"}
    assert actions.states == ["review"]
    review = queue.claim("codex")
    assert review is not None
    assert (review.request.repo, review.request.ticket_id) == (FACTORY_REPO, "51")
    assert (review.request.action, review.request.head) == ("review", PR_HEAD)
    assert json.loads(str(review.request.detail))["round"] == "1/3"
    assert queue.claim("claude") is None


def test_a_short_ready_head_that_prefixes_the_pr_head_is_tracked(
    tmp_path: Path,
) -> None:
    queue, _actions = factory_pr_poll(tmp_path, DOCUMENTED_READY.format(head=PR_HEAD[:12]))

    assert tracked_rows(queue) == [(FACTORY_REPO, "51", "github")]
    review = queue.claim("codex")
    assert review is not None and review.request.head == PR_HEAD


@pytest.mark.parametrize(
    ("body", "author", "enabled"),
    [
        (DOCUMENTED_READY.format(head=PR_HEAD), "maxharris1", False),
        (DOCUMENTED_READY.format(head=PR_HEAD), "mallory", True),
        (
            DOCUMENTED_READY.format(head=PR_HEAD).replace("claude-bot", "codex-bot"),
            "maxharris1",
            True,
        ),
        (
            DOCUMENTED_READY.format(head=PR_HEAD).replace("role=builder", "role=reviewer"),
            "maxharris1",
            True,
        ),
        (DOCUMENTED_READY.format(head="e" * 40), "maxharris1", True),
        (DOCUMENTED_READY.format(head=PR_HEAD[:6]), "maxharris1", True),
    ],
    ids=[
        "flag-off",
        "untrusted-author",
        "not-the-factory-md-builder",
        "wrong-role",
        "other-head",
        "head-too-short",
    ],
)
def test_a_ready_that_fails_any_intake_rule_does_not_start_tracking(
    tmp_path: Path,
    body: str,
    author: str,
    enabled: bool,
) -> None:
    queue, actions = factory_pr_poll(tmp_path, body, author=author, enabled=enabled)

    assert tracked_rows(queue) == []
    assert outcomes(queue) == {}
    assert actions.states == []
    assert queue.claim("codex") is None


def test_without_an_orch_state_label_a_ready_pr_is_not_discovered(
    tmp_path: Path,
) -> None:
    """Preservation guard: discovery stays label-based."""
    queue, _actions = factory_pr_poll(
        tmp_path,
        DOCUMENTED_READY.format(head=PR_HEAD),
        labels=("bug",),
    )

    assert tracked_rows(queue) == []
    assert queue.claim("codex") is None


def test_with_the_flag_off_a_trusted_grok_brief_still_starts_tracking(
    tmp_path: Path,
) -> None:
    """Preservation guard: the brief/go entry is unchanged."""
    queue, _actions = factory_pr_poll(tmp_path, BRIEF, enabled=False)

    assert tracked_rows(queue) == [(FACTORY_REPO, "51", "github")]
    assert queue.claim("codex") is None


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
    """Driven through poll_once, so discovery tracks the PR from the ready."""
    actions = FakeReviewActions()
    delivery = FakeGrokDelivery()
    dispatcher, queue = setup(tmp_path, review_actions=actions, grok_delivery=delivery)
    source = OnePrSource()
    ready_1 = comment("ready-1", READY.format(head=HEAD_1), author="claude-login", created_at=1)

    source.poll(dispatcher, ready_ticket(ready_1))
    assert tracked_rows(queue) == [("owner/repo", "42", "github")]
    review_1 = queue.claim("codex")
    assert review_1 is not None and review_1.request.action == "review"
    queue.finish(review_1.id, owner="codex", handoff_comment_id="revise-1")

    revise = comment(
        "revise-1",
        VERDICT.format(round="1/3", head=HEAD_1, verdict="REVISE", risk="high"),
        author="codex-login",
        created_at=2,
    )
    source.poll(dispatcher, ready_ticket(ready_1, revise))
    fix = queue.claim("claude")
    assert fix is not None
    assert (fix.request.action, fix.request.head) == ("fix", HEAD_1)
    queue.finish(fix.id, owner="claude", handoff_comment_id="ready-2")

    ready_2 = comment("ready-2", READY.format(head=HEAD_2), author="claude-login", created_at=3)
    source.poll(dispatcher, ready_ticket(ready_1, revise, ready_2, head=HEAD_2))
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
    source.poll(dispatcher, ready_ticket(ready_1, revise, ready_2, approve, head=HEAD_2))

    assert outcomes(queue)["approve-2"] == "handed-off"
    assert actions.handoffs == [HEAD_2]
    assert actions.merges == []
    assert actions.fast_forwards == []
    assert actions.states[-1] == "handed-off"
    # poll_once's reconcile step delivers the wake through the Grok webhook.
    assert [job.request.action for job in delivery.jobs] == ["done"]
    detail = json.loads(str(delivery.jobs[0].request.detail))
    assert (detail["outcome"], detail["project_bot"]) == ("handed-off", "grok-bot")
