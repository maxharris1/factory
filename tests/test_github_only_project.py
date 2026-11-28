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

from lib.dashboard import collect_snapshot, write_snapshot
from lib.dispatcher import CIResult, Dispatcher, TicketRef, TrackedTicket
from lib.factory_md import load_factory_md
from lib.github_actions import GitHubReviewActions
from lib.orch_yml import OrchConfig, Project, load_orch_yml
from lib.poll_source import LivePollSource
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    BRIEF,
    GO,
    READY,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    factory,
    setup,
    ticket,
)
from tests.test_live_poll import FACTORY, FakeGh, GhResult, config
from tests.pool_helpers import LOCAL_BOX_MACHINES


pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

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
    assert factory_project.linear_project_ids == frozenset()
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

    def github_candidates(self, label_prefix: str, *, exclude=frozenset()) -> tuple[TrackedTicket, ...]:
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


# Discovery (Codex r1 F1.1): the documented entry is the builder's trusted
# ready naming the PR head, with or without an orch:state/ label (issue #59;
# factory.md Context, README "Poll", SPEC section 7). These run the live poll
# source against a fake gh, through poll_once, so nothing is tracked by hand.

PR_HEAD = "f" * 40
DOCUMENTED_READY = (
    "<!-- orch:v1 kind=ready from=claude-bot role=builder head={head} -->\n"
    "Ready for review.\n"
)


def gh_comment(
    comment_id: int,
    body: str,
    *,
    author: str = "maxharris1",
    minute: int = 0,
) -> dict:
    return {
        "id": comment_id,
        "user": {"login": author},
        "body": body,
        "created_at": f"2026-11-12T10:{minute:02d}:00Z",
        "html_url": f"https://github.test/maxharris1/factory/pull/51#c{comment_id}",
    }


class FactoryPrWorld:
    """The live poll source over a fake gh, one open factory PR (#51)."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        enabled: bool = True,
        labels: tuple[str, ...] = ("orch:state/intake",),
    ) -> None:
        self.gh = FakeGh((ROOT / "factory.md").read_text(encoding="utf-8"))
        self.gh.head = PR_HEAD
        self.gh.issues = [
            {
                "number": 51,
                "pull_request": {},
                "labels": [{"name": name} for name in labels],
            }
        ]
        self.source = LivePollSource(
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
            gh_runner=self.gh,
            sleep=lambda seconds: None,
        )
        self.path = tmp_path / "orch.db"
        self.queue = SQLiteJobQueue(self.path, clock=lambda: 100.0)
        self.actions = FakeReviewActions()
        self.dispatcher = Dispatcher(
            self.path,
            self.queue,
            trusted_logins={
                "github": {
                    # The shipped orch.yml: one login for all of Max's bots.
                    "maxharris1": {
                        "grok-bot",
                        "claude-bot",
                        "codex-bot",
                        "cursor-bot",
                    },
                },
                "linear": {},
            },
            review_actions=self.actions,
            grok_delivery=FakeGrokDelivery(),
        )

    def poll(self) -> None:
        self.dispatcher.poll_once(self.source)

    def comment_reads(self) -> int:
        return sum(
            any(part.endswith("/comments?per_page=100") for part in call)
            for call in self.gh.calls
        )

    def jobs(self) -> list[tuple[str, str, str | None, str]]:
        with self.queue.connect() as connection:
            rows = connection.execute(
                "SELECT action, recipient, head, state FROM jobs "
                "ORDER BY created_at, rowid"
            ).fetchall()
        return [tuple(row) for row in rows]


def factory_pr_poll(
    tmp_path: Path,
    body: str,
    *,
    author: str = "maxharris1",
    enabled: bool = True,
    labels: tuple[str, ...] = ("orch:state/intake",),
) -> tuple[SQLiteJobQueue, FakeReviewActions]:
    world = FactoryPrWorld(tmp_path, enabled=enabled, labels=labels)
    world.gh.comments = [gh_comment(9001, body, author=author)]
    world.poll()
    return world.queue, world.actions


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
@pytest.mark.parametrize(
    "labels",
    [("orch:state/intake",), ()],
    ids=["labelled", "unlabelled"],
)
def test_a_ready_that_fails_any_intake_rule_does_not_start_tracking(
    tmp_path: Path,
    body: str,
    author: str,
    enabled: bool,
    labels: tuple[str, ...],
) -> None:
    queue, actions = factory_pr_poll(
        tmp_path, body, author=author, enabled=enabled, labels=labels
    )

    assert tracked_rows(queue) == []
    assert outcomes(queue) == {}
    assert actions.states == []
    assert queue.claim("codex") is None


# Discovery without a label (issue #59): a ready_starts_review project's PRs
# are built outside the queue, so nothing writes orch:state/ before tracking
# starts. An open PR with a parseable kind=ready comment is a candidate with
# no label; _starts_tracking still applies the trust and head rules.


@pytest.mark.parametrize("labels", [(), ("bug",)], ids=["no-labels", "other-label"])
def test_an_unlabelled_factory_pr_with_a_trusted_ready_is_tracked_and_reviewed(
    tmp_path: Path,
    labels: tuple[str, ...],
) -> None:
    queue, actions = factory_pr_poll(
        tmp_path,
        DOCUMENTED_READY.format(head=PR_HEAD),
        labels=labels,
    )

    assert tracked_rows(queue) == [(FACTORY_REPO, "51", "github")]
    assert outcomes(queue) == {"9001": "queued-review"}
    # The poller writes the state label once tracking has started.
    assert actions.states == ["review"]
    review = queue.claim("codex")
    assert review is not None
    assert (review.request.repo, review.request.ticket_id) == (FACTORY_REPO, "51")
    assert (review.request.action, review.request.head) == ("review", PR_HEAD)
    assert json.loads(str(review.request.detail))["round"] == "1/3"
    assert queue.claim("claude") is None


def test_an_unlabelled_pr_with_a_ready_from_an_untrusted_author_is_ignored(
    tmp_path: Path,
) -> None:
    world = FactoryPrWorld(tmp_path, labels=())
    world.gh.comments = [
        gh_comment(9001, DOCUMENTED_READY.format(head=PR_HEAD), author="mallory")
    ]

    world.poll()
    world.poll()

    assert tracked_rows(world.queue) == []
    assert outcomes(world.queue) == {}
    assert world.actions.states == []
    assert world.jobs() == []


def test_without_the_flag_an_unlabelled_pr_is_not_discovered_or_read(
    tmp_path: Path,
) -> None:
    """Preservation guard: projects that did not opt in stay label-based,
    and their unlabelled PRs cost no comment reads."""
    world = FactoryPrWorld(tmp_path, enabled=False, labels=())
    world.gh.comments = [gh_comment(9001, DOCUMENTED_READY.format(head=PR_HEAD))]

    world.poll()

    assert tracked_rows(world.queue) == []
    assert world.jobs() == []
    assert world.comment_reads() == 0


@pytest.mark.parametrize(
    "body",
    [
        "Looks good to me.\n",
        BRIEF,
        "<!-- orch:v1 kind=ready from=claude-bot -->\n",
    ],
    ids=["plain-comment", "brief-only", "unparseable-ready"],
)
def test_an_unlabelled_pr_without_a_parseable_ready_is_not_discovered(
    tmp_path: Path,
    body: str,
) -> None:
    """A Grok brief/go entry still needs the label; only ready skips it."""
    world = FactoryPrWorld(tmp_path, labels=())
    world.gh.comments = [gh_comment(9001, body)]

    world.poll()

    assert tracked_rows(world.queue) == []
    assert world.jobs() == []
    # The comments were read once to look for a ready; no pull or CI reads.
    assert not any(
        any("/pulls/" in part or "/check-runs" in part for part in call)
        for call in world.gh.calls
    )


def test_the_same_ready_comment_or_head_never_queues_a_second_review(
    tmp_path: Path,
) -> None:
    world = FactoryPrWorld(tmp_path, labels=())
    world.gh.comments = [gh_comment(9001, DOCUMENTED_READY.format(head=PR_HEAD))]

    world.poll()
    # The PR is still unlabelled on GitHub (say the label write failed), so
    # the next cycles discover it again from the same ready.
    world.poll()
    world.poll()
    # A second ready naming the same head, while round 1 is still queued.
    world.gh.comments.append(
        gh_comment(9002, DOCUMENTED_READY.format(head=PR_HEAD), minute=5)
    )
    world.poll()

    assert tracked_rows(world.queue) == [(FACTORY_REPO, "51", "github")]
    assert world.jobs() == [("review", "codex", PR_HEAD, "queued")]
    assert outcomes(world.queue)["9001"] == "queued-review"
    assert world.queue.claim("codex") is not None
    assert world.queue.claim("codex") is None


def test_a_ready_naming_a_new_head_queues_a_new_review(tmp_path: Path) -> None:
    new_head = "e" * 40
    world = FactoryPrWorld(tmp_path, labels=())
    world.gh.comments = [gh_comment(9001, DOCUMENTED_READY.format(head=PR_HEAD))]
    world.poll()
    first = world.queue.claim("codex")
    assert first is not None and first.request.head == PR_HEAD
    world.queue.finish(first.id, owner="codex", handoff_comment_id="9002")
    world.gh.comments.append(
        gh_comment(
            9002,
            VERDICT.format(round="1/3", head=PR_HEAD, verdict="REVISE", risk="high"),
            minute=5,
        )
    )
    world.poll()
    fix = world.queue.claim("claude")
    assert fix is not None and fix.request.head == PR_HEAD
    world.queue.finish(fix.id, owner="claude", handoff_comment_id="9003")

    # The builder pushes the fix and posts a ready for the new tip.
    world.gh.head = new_head
    world.gh.comments.append(
        gh_comment(9003, DOCUMENTED_READY.format(head=new_head), minute=10)
    )
    world.poll()
    world.poll()

    assert outcomes(world.queue) == {
        "9001": "queued-review",
        "9002": "queued-review-fix",
        "9003": "queued-review",
    }
    assert world.jobs() == [
        ("review", "codex", PR_HEAD, "done"),
        ("fix", "claude", PR_HEAD, "done"),
        ("review", "codex", new_head, "queued"),
    ]
    second = world.queue.claim("codex")
    assert second is not None
    assert json.loads(str(second.request.detail))["round"] == "2/3"


def test_a_stale_ready_does_not_track_but_a_ready_for_the_new_tip_does(
    tmp_path: Path,
) -> None:
    new_head = "e" * 40
    world = FactoryPrWorld(tmp_path, labels=())
    world.gh.head = new_head
    world.gh.comments = [gh_comment(9001, DOCUMENTED_READY.format(head=PR_HEAD))]
    world.poll()
    assert tracked_rows(world.queue) == []

    world.gh.comments.append(
        gh_comment(9002, DOCUMENTED_READY.format(head=new_head), minute=5)
    )
    world.poll()

    assert tracked_rows(world.queue) == [(FACTORY_REPO, "51", "github")]
    assert outcomes(world.queue) == {
        "9001": "stale-ready-head",
        "9002": "queued-review",
    }
    assert world.jobs() == [("review", "codex", new_head, "queued")]


# Codex PR #64 r1 F1.1: an unlabelled PR is admitted only through the #59
# ready exception, so only a trusted builder ready naming the PR head may
# start tracking it. A Grok brief or go on the same PR still needs the label
# (README "Poll"), so pairing it with an untrusted, stale or wrong-role ready
# must start nothing: no tracking row, no job, no label.

TRUSTED_GO = GO.format(state="building")


@pytest.mark.parametrize("go_first", [True, False], ids=["go-first", "ready-first"])
@pytest.mark.parametrize(
    ("ready_body", "ready_author"),
    [
        (DOCUMENTED_READY.format(head=PR_HEAD), "mallory"),
        (DOCUMENTED_READY.format(head="e" * 40), "maxharris1"),
        (
            DOCUMENTED_READY.format(head=PR_HEAD).replace(
                "role=builder", "role=reviewer"
            ),
            "maxharris1",
        ),
        (
            DOCUMENTED_READY.format(head=PR_HEAD).replace("claude-bot", "codex-bot"),
            "maxharris1",
        ),
    ],
    ids=["untrusted-ready", "stale-head-ready", "wrong-role-ready", "wrong-builder-ready"],
)
def test_an_unlabelled_pr_with_a_grok_go_and_a_failing_ready_starts_nothing(
    tmp_path: Path,
    ready_body: str,
    ready_author: str,
    go_first: bool,
) -> None:
    world = FactoryPrWorld(tmp_path, labels=())
    go = gh_comment(9001, TRUSTED_GO, minute=0 if go_first else 5)
    ready = gh_comment(9002, ready_body, author=ready_author, minute=5 if go_first else 0)
    world.gh.comments = [go, ready] if go_first else [ready, go]

    world.poll()
    world.poll()

    assert tracked_rows(world.queue) == []
    assert outcomes(world.queue) == {}
    assert world.actions.states == []
    assert world.jobs() == []


def test_an_unlabelled_pr_with_only_a_trusted_matching_ready_is_still_tracked(
    tmp_path: Path,
) -> None:
    """Positive guard for F1.1: the trusted builder ready naming the PR head
    still admits an unlabelled PR and queues only the round-1 review."""
    world = FactoryPrWorld(tmp_path, labels=())
    world.gh.comments = [gh_comment(9002, DOCUMENTED_READY.format(head=PR_HEAD))]

    world.poll()

    assert tracked_rows(world.queue) == [(FACTORY_REPO, "51", "github")]
    assert outcomes(world.queue) == {"9002": "queued-review"}
    assert world.actions.states == ["review"]
    assert world.jobs() == [("review", "codex", PR_HEAD, "queued")]


def test_a_labelled_pr_with_a_grok_go_is_still_tracked_on_the_go(
    tmp_path: Path,
) -> None:
    """Preservation guard for F1.1: the labelled entry is unchanged, so a
    trusted Grok go on a labelled PR still starts tracking and the build."""
    world = FactoryPrWorld(tmp_path)
    world.gh.comments = [
        gh_comment(9001, TRUSTED_GO),
        gh_comment(9002, DOCUMENTED_READY.format(head=PR_HEAD), author="mallory", minute=5),
    ]

    world.poll()

    assert tracked_rows(world.queue) == [(FACTORY_REPO, "51", "github")]
    assert outcomes(world.queue)["9001"] == "queued-build"
    assert ("build", "claude", None, "queued") in world.jobs()


def test_review_and_fix_work_from_an_unlabelled_pr_hold_a_slot_on_the_dashboard(
    tmp_path: Path,
) -> None:
    """Issue #59: the slots showed idle while factory PR work ran. The
    review (codex) and the fix build (claude) are queue jobs, so each holds
    a box slot while running and shows on the read-only, redacted page."""
    world = FactoryPrWorld(tmp_path, labels=())
    ready_body = DOCUMENTED_READY.format(head=PR_HEAD)
    world.gh.comments = [gh_comment(9001, ready_body)]
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")

    def snapshot() -> dict:
        return collect_snapshot(
            world.path,
            log,
            now=160.0,
            machines_path=LOCAL_BOX_MACHINES,
        )

    world.poll()
    idle = snapshot()
    assert idle["running"] == []
    assert idle["slots"]["box"] == {"name": "box", "running": 0, "slot_max": 3}
    assert [(item["repo"], item["ticket_id"], item["action"]) for item in idle["queued"]] == [
        (FACTORY_REPO, "51", "review")
    ]

    review = world.queue.claim("codex")
    assert review is not None
    reviewing = snapshot()
    assert reviewing["slots"]["box"] == {"name": "box", "running": 1, "slot_max": 3}
    assert [
        (item["repo"], item["ticket_id"], item["action"], item["recipient"], item["device"])
        for item in reviewing["running"]
    ] == [(FACTORY_REPO, "51", "review", "codex", "box")]
    assert {
        (item["repo"], item["ticket_id"], item["tracker"], item["state"])
        for item in reviewing["tickets"]
    } == {(FACTORY_REPO, "51", "github", "review")}
    # pc and mac stay disabled in the shipped machines.yml.
    devices = {item["name"]: item["enabled"] for item in reviewing["slots"]["devices"]}
    assert (devices["pc"], devices["mac"]) == (False, False)

    world.queue.finish(review.id, owner="codex", handoff_comment_id="9002")
    world.gh.comments.append(
        gh_comment(
            9002,
            VERDICT.format(round="1/3", head=PR_HEAD, verdict="REVISE", risk="high"),
            minute=10,
        )
    )
    world.poll()
    fix = world.queue.claim("claude")
    assert fix is not None
    assert (fix.request.action, fix.request.head) == ("fix", PR_HEAD)
    building = snapshot()
    assert building["slots"]["box"] == {"name": "box", "running": 1, "slot_max": 3}
    assert [
        (item["repo"], item["ticket_id"], item["action"], item["recipient"])
        for item in building["running"]
    ] == [(FACTORY_REPO, "51", "fix", "claude")]

    # Redacted: no head or comment text reaches the page; the PR link is github.com's.
    out = tmp_path / "site"
    status_path, page_path = write_snapshot(out, building)
    for path in (status_path, page_path):
        text = path.read_text(encoding="utf-8")
        assert FACTORY_REPO in text
        assert PR_HEAD not in text
        assert PR_HEAD[:12] not in text
        assert "Ready for review" not in text
        assert "github.test" not in text
    assert "https://github.com/maxharris1/factory/pull/51" in page_path.read_text(encoding="utf-8")


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


@pytest.mark.parametrize(
    ("pull", "expected"),
    [({"additions": 30, "deletions": 12}, 42), ({}, None), ({"additions": 3}, None)],
    ids=["read", "absent", "partial"],
)
def test_github_ticket_carries_the_prs_changed_line_count(pull: dict, expected: int | None) -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.pull = pull
    source = LivePollSource(config(), gh_runner=gh)
    project = config().projects[0]

    assert source._github_ticket(project, source._factory(project), "12").changed_lines == expected


class ChecksGh(FakeGh):
    def __init__(self, factory: str, runs: list[dict]) -> None:
        super().__init__(factory)
        self.runs = runs

    def __call__(self, argv, timeout):
        if any(part.endswith("/check-runs?per_page=100") for part in argv):
            page = {"total_count": len(self.runs), "check_runs": self.runs}
            return GhResult(0, json.dumps(page), "")
        return super().__call__(argv, timeout)


RUNNING = [{"name": "test", "status": "in_progress", "conclusion": None}]
PASSED = [{"name": "test", "status": "completed", "conclusion": "success"}]
CI_ON_DEV_AND_MAIN = "skills: []\nci:\n  branches: [dev, main]\n"


@pytest.mark.parametrize(
    ("ci_listed", "base", "runs", "expected"),
    [
        (True, "feature/upload-only-intake", [], CIResult("not-run", reported=False)),
        (True, "feature/upload-only-intake", RUNNING, CIResult("pending", pending=("test",))),
        (True, "feature/upload-only-intake", PASSED, CIResult("success")),
        (True, "dev", [], CIResult("pending", reported=False)),
        (False, "feature/upload-only-intake", [], CIResult("pending", reported=False)),
    ],
    ids=["unreported-off-ci", "reported-pending", "success", "ci-base", "ci-absent"],
)
def test_only_an_unreported_head_on_a_base_without_ci_is_not_run(
    ci_listed: bool, base: str, runs: list[dict], expected: CIResult
) -> None:
    text = FACTORY.format(tickets="github", ship="handoff")
    if ci_listed:
        text = text.replace("skills: []\n", CI_ON_DEV_AND_MAIN)
    gh = ChecksGh(text, runs)
    gh.pull = {"base": {"ref": base}}
    source = LivePollSource(config(), gh_runner=gh)
    project = config().projects[0]

    tracked = source._github_ticket(project, source._factory(project), "12")
    assert tracked.pr_base == base
    assert tracked.ci == {gh.head: expected}
