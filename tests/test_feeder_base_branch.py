from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from lib.brief_comment import FACTORY_MD_HEADING
from lib.dispatcher import TicketRef
from lib.feeder import FEEDER_TABLE
from lib.poll_source import FedPulls, LivePollSource
from tests.test_feeder import (
    LINEAR_FACTORY_MD,
    FakeGitHub,
    FakeLinear,
    linear_issue,
    make_feeder,
    rows,
)
from tests.test_linear_multi_project import transport_for
from tests.test_live_poll import FACTORY, FakeGh, config
from tests.test_live_poll import linear_issue as polled_issue

REPO = "acme/linear-app"
FEATURE = "feature/upload-only-intake"
FEATURE_SHA = "f" * 40

ORCH = """
poll_seconds: 60
linear:
  api_key_env: LINEAR_API_KEY
grok:
  webhook_url_env: FACTORY_WEBHOOK_URL
  webhook_key_env: FACTORY_WEBHOOK_KEY
trusted_logins:
  maxharris1: [grok-bot, claude-bot, codex-bot]
trusted_linear_users:
  max-linear-id: [grok-bot]
projects:
  - repo: acme/linear-app
    factory_ref: dev
    linear_project_ids: [proj-offer, proj-upload]
    intake:
      linear_state: In Progress
"""
OVERRIDDEN = ORCH + f"      base_branches:\n        proj-upload: {FEATURE}\n"


class BaseReadingGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.base_reads: list[str] = []

    def branch_sha(self, repo: str, branch: str) -> str | None:
        self.base_reads.append(branch)
        return super().branch_sha(repo, branch)


def in_project(identifier: str, project: str | None, created_at: float = 50.0):
    return replace(linear_issue(identifier, created_at=created_at), linear_project=project)


def feed(tmp_path: Path, *issues, orch: str = OVERRIDDEN, feature: bool = True, polls: int = 1):
    github = BaseReadingGitHub()
    if feature:
        github.branches[(REPO, FEATURE)] = FEATURE_SHA
    linear = FakeLinear()
    linear.issues = list(issues)
    feeder, _, _, _ = make_feeder(
        tmp_path, orch=orch, github=github, linear=linear, factory_text=LINEAR_FACTORY_MD
    )
    fed = [ref for _ in range(polls) for ref in feeder.feed_once().fed]
    return fed, github, linear


def pull_for(github: FakeGitHub, branch: str) -> dict:
    (pull,) = [pull for pull in github.pulls.values() if pull["head"] == branch]
    return pull


def test_each_project_opens_its_draft_pr_on_its_own_base(tmp_path: Path) -> None:
    upload = in_project("AUT-7", "proj-upload", created_at=10.0)
    offer = in_project("AUT-8", "proj-offer", created_at=20.0)

    fed, github, linear = feed(tmp_path, upload, offer, polls=2)

    assert sorted(ref.ticket_id for ref in fed) == ["AUT-7", "AUT-8"]
    assert pull_for(github, upload.branch)["base"] == FEATURE
    assert pull_for(github, offer.branch)["base"] == "dev"
    parents = {message.split(":")[0]: base for _, base, message in github.commits}
    assert parents == {"Start AUT-7": FEATURE_SHA, "Start AUT-8": "a" * 40}
    brief = linear.posted["uuid-AUT-7"][0].body
    assert f"on branch `{upload.branch}` off `{FEATURE}`" in brief
    assert f"{FACTORY_MD_HEADING}`dev`)" in brief
    assert f"off `dev`" in linear.posted["uuid-AUT-8"][0].body


def test_a_fed_ticket_keeps_its_base_after_the_override_is_removed(tmp_path: Path) -> None:
    upload = in_project("AUT-7", "proj-upload")
    feed(tmp_path, upload)

    refed, _, _ = feed(tmp_path, upload, orch=ORCH)

    assert refed == []
    assert rows(tmp_path / "orch.db")["AUT-7"]["base_branch"] == FEATURE
    base = config(linear=True)
    (project,) = base.projects
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.pull = {"state": "open", "base": {"ref": FEATURE}}
    source = LivePollSource(
        replace(base, projects=(replace(project, repo=REPO, linear_project_ids=frozenset({"proj-upload"})),)),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=transport_for({"AUT-7": polled_issue(project="proj-upload", identifier="AUT-7")}),
        fed_pulls=FedPulls(tmp_path / "orch.db"),
    )

    (ticket,) = source.tracked_tickets((TicketRef(REPO, "AUT-7", "linear"),))

    assert ticket.factory.base_branch == FEATURE
    assert ticket.pr_number == "501"


def test_an_unfed_ticket_follows_a_config_change(tmp_path: Path) -> None:
    upload = in_project("AUT-7", "proj-upload")
    feed(tmp_path, upload, feature=False)

    fed, github, _ = feed(tmp_path, upload, orch=ORCH)

    assert [ref.ticket_id for ref in fed] == ["AUT-7"]
    assert pull_for(github, upload.branch)["base"] == "dev"


def test_a_missing_base_branch_holds_the_ticket_and_writes_nothing(tmp_path: Path) -> None:
    upload = in_project("AUT-7", "proj-upload")

    fed, github, linear = feed(tmp_path, upload, feature=False)

    assert fed == []
    row = rows(tmp_path / "orch.db")["AUT-7"]
    assert row["state"] == "waiting-blocked"
    assert FEATURE in row["waiting_reason"] and REPO in row["waiting_reason"]
    assert (github.commits, github.pulls, github.posted, linear.posted) == ([], {}, {}, {})
    assert {branch for (_, branch) in github.branches} == set()
    assert "dev" not in github.base_reads


def test_a_ticket_with_no_linear_project_is_held_when_the_repo_has_overrides(
    tmp_path: Path,
) -> None:
    fed, github, _ = feed(tmp_path, in_project("AUT-9", None))

    assert fed == []
    row = rows(tmp_path / "orch.db")["AUT-9"]
    assert row["state"] == "waiting-blocked"
    assert "Linear project" in row["waiting_reason"]
    assert github.pulls == {} and "dev" not in github.base_reads


def test_without_an_override_a_ticket_opens_on_factory_ref(tmp_path: Path) -> None:
    fed, github, _ = feed(tmp_path, in_project("AUT-7", None), orch=ORCH)

    assert [ref.ticket_id for ref in fed] == ["AUT-7"]
    assert github.pulls[(REPO, 501)]["base"] == "dev"


def partly_fed(tmp_path: Path):
    github, linear = BaseReadingGitHub(), FakeLinear()
    github.branches[(REPO, FEATURE)] = FEATURE_SHA
    linear.issues = [in_project("AUT-7", "proj-upload")]

    def feeder(orch: str):
        return make_feeder(tmp_path, orch=orch, github=github, linear=linear, factory_text=LINEAR_FACTORY_MD)[0]

    post = linear.post_linear_comment

    def unavailable(*args):
        raise RuntimeError("temporary comment failure")

    linear.post_linear_comment = unavailable
    assert feeder(OVERRIDDEN).feed_once().errors == 1
    linear.post_linear_comment = post
    del github.branches[(REPO, FEATURE)]
    assert feeder(OVERRIDDEN).feed_once().blocked == 1
    return github, linear, feeder


def test_a_partly_fed_ticket_keeps_its_base_and_stays_held_after_the_override_is_removed(
    tmp_path: Path,
) -> None:
    github, linear, feeder = partly_fed(tmp_path)

    result = feeder(ORCH).feed_once()

    row = rows(tmp_path / "orch.db")["AUT-7"]
    assert (list(result.fed), result.blocked) == ([], 1)
    assert row["base_branch"] == github.pulls[(REPO, 501)]["base"] == FEATURE
    assert row["state"] == "waiting-blocked"
    assert FEATURE in row["waiting_reason"]
    assert linear.posted.get("uuid-AUT-7", []) == []


def test_a_partly_fed_ticket_that_returns_after_losing_its_signal_keeps_its_base(
    tmp_path: Path,
) -> None:
    github, linear, feeder = partly_fed(tmp_path)
    issue = linear.issues
    linear.issues = []
    feeder(ORCH).feed_once()
    linear.issues = issue
    github.branches[(REPO, FEATURE)] = FEATURE_SHA

    result = feeder(ORCH).feed_once()

    assert [ref.ticket_id for ref in result.fed] == ["AUT-7"]
    assert rows(tmp_path / "orch.db")["AUT-7"]["base_branch"] == FEATURE
    assert len(github.pulls) == 1
    (brief, *_) = linear.posted["uuid-AUT-7"]
    assert f"off `{FEATURE}`" in brief.body


def test_fed_pulls_reads_a_stacked_tickets_parent_branch(tmp_path: Path) -> None:
    feed(tmp_path, in_project("AUT-7", "proj-upload"))
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute(f"UPDATE {FEEDER_TABLE} SET stack_parent_pr = 10, stack_parent_branch = 'factory/issue-10'")

    fed_pulls = FedPulls(tmp_path / "orch.db")

    assert fed_pulls.stack_parent(REPO, "AUT-7") == (10, "factory/issue-10")
    assert fed_pulls.stack_parent(REPO, "AUT-8") is None
