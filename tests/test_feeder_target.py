"""A ticket's `Target branch:` line names its draft PR's base (issue #1467).

SPEC section 4.4. The feeder tests use the fakes from tests/test_feeder.py;
nothing reaches a real tracker.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.feeder import _pull_body
from tests.test_feeder import (
    BASE_SHA,
    LINEAR_FACTORY_MD,
    LINEAR_ORCH,
    FakeGitHub,
    FakeLinear,
    issue,
    linear_issue,
    make_feeder,
    rows,
)
from tests.test_feeder_base_branch import ORCH as MULTI_PROJECT_ORCH
from tests.test_feeder_base_branch import OVERRIDDEN
from tests.test_feeder_template import body, flags, nothing_written, wakes

HOME = "owner/app"
LINEAR_REPO = "acme/linear-app"
TARGET = "feature/stack-base"
TARGET_SHA = "b" * 40


def github_with_target(repo: str) -> FakeGitHub:
    github = FakeGitHub()
    github.branches[(repo, TARGET)] = TARGET_SHA
    return github


def targeted(line: str = f"Target branch: {TARGET}") -> str:
    return body(extra=line)


def linear_ticket(text: str, project: str | None = "proj-1"):
    return replace(linear_issue("AUT-7"), body=text, linear_project=project)


def pull_on(github: FakeGitHub, branch: str) -> dict:
    (pull,) = [pull for pull in github.pulls.values() if pull["head"] == branch]
    return pull


def test_a_github_target_opens_the_pull_on_it(tmp_path: Path) -> None:
    github = github_with_target(HOME)
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    github.issues[HOME] = [issue(7, body=targeted(), template=False)]

    report = feeder.feed_once()

    assert len(report.fed) == 1
    assert pull_on(github, "factory/issue-7")["base"] == TARGET
    assert [(repo, sha) for repo, sha, _ in github.commits] == [(HOME, TARGET_SHA)]
    row = rows(tmp_path / "orch.db")["7"]
    assert (row["base_branch"], row["target_source"]) == (TARGET, "ticket")
    brief = github.posted[(HOME, 501)][0].body
    assert f"on branch `factory/issue-7` off `{TARGET}`" in brief


@pytest.mark.parametrize(
    ("orch", "project"),
    [(LINEAR_ORCH, "proj-1"), (OVERRIDDEN, "proj-upload"), (MULTI_PROJECT_ORCH, None)],
    ids=["linear-project", "over-base-branches", "no-linear-project"],
)
def test_a_linear_target_opens_the_pull_on_it(tmp_path: Path, orch: str, project) -> None:
    github = github_with_target(LINEAR_REPO)
    linear = FakeLinear()
    feeder, _, _, _ = make_feeder(
        tmp_path, orch=orch, github=github, linear=linear, factory_text=LINEAR_FACTORY_MD
    )
    ticket = linear_ticket(targeted(), project)
    linear.issues = [ticket]

    report = feeder.feed_once()

    assert [ref.ticket_id for ref in report.fed] == ["AUT-7"]
    assert pull_on(github, ticket.branch)["base"] == TARGET
    assert [(repo, sha) for repo, sha, _ in github.commits] == [(LINEAR_REPO, TARGET_SHA)]
    row = rows(tmp_path / "orch.db")["AUT-7"]
    assert (row["base_branch"], row["target_source"]) == (TARGET, "ticket")
    assert f"off `{TARGET}`" in linear.posted["uuid-AUT-7"][0].body


def test_the_base_is_frozen_once_the_feed_begins(tmp_path: Path) -> None:
    github = github_with_target(HOME)
    github.branches[(HOME, "feature/other")] = "c" * 40
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    github.issues[HOME] = [issue(7, body=targeted(), template=False)]
    feeder.feed_once()

    github.issues[HOME] = [issue(7, body=targeted("Target branch: feature/other"), template=False)]
    feeder.feed_once()

    row = rows(tmp_path / "orch.db")["7"]
    assert (row["base_branch"], row["target_source"]) == (TARGET, "ticket")
    assert len(github.pulls) == 1


@pytest.mark.parametrize("value", ["a..b", "two words", "factory/issue-7"])
def test_a_bad_github_target_is_refused_and_wakes_once(tmp_path: Path, value: str) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=targeted(f"Target branch: {value}"), template=False)]

    for _ in range(2):
        report = feeder.feed_once()
        assert report.fed == ()
        assert report.refused == 1

    assert nothing_written(github)
    row = rows(tmp_path / "orch.db")["7"]
    assert row["state"] == "refused-intake"
    (wake,) = wakes(tmp_path)
    assert json.loads(wake["detail"])["defects"] == ["bad-target-branch"]
    assert flags(tmp_path) == {(HOME, "7", "intake-refused")}


def test_a_bad_linear_target_is_refused_and_wakes_once(tmp_path: Path) -> None:
    linear = FakeLinear()
    feeder, _, github, _ = make_feeder(
        tmp_path, orch=LINEAR_ORCH, linear=linear, factory_text=LINEAR_FACTORY_MD
    )
    ticket = linear_ticket(targeted(f"Target branch: {TARGET}\nTarget branch: {TARGET}"))
    linear.issues = [ticket]

    for _ in range(2):
        assert feeder.feed_once().refused == 1

    assert nothing_written(github)
    assert linear.posted == {}
    assert rows(tmp_path / "orch.db")["AUT-7"]["state"] == "refused-intake"
    (wake,) = wakes(tmp_path)
    assert json.loads(wake["detail"])["defects"] == ["bad-target-branch"]
    assert flags(tmp_path) == {(LINEAR_REPO, "AUT-7", "intake-refused")}


def test_a_linear_tickets_own_head_branch_is_refused(tmp_path: Path) -> None:
    linear = FakeLinear()
    feeder, _, github, _ = make_feeder(
        tmp_path, orch=LINEAR_ORCH, linear=linear, factory_text=LINEAR_FACTORY_MD
    )
    head = linear_issue("AUT-7").branch
    linear.issues = [linear_ticket(targeted(f"Target branch: {head}"))]

    assert feeder.feed_once().refused == 1
    (wake,) = wakes(tmp_path)
    assert json.loads(wake["detail"])["defects"] == ["bad-target-branch"]


@pytest.mark.parametrize("extra", [None, "Target branch: default"])
def test_no_line_records_project_and_opens_on_todays_base(tmp_path: Path, extra) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    ticket = issue(7, body=body(extra=extra), template=False)
    github.issues[HOME] = [ticket]

    feeder.feed_once()

    pull = pull_on(github, "factory/issue-7")
    assert pull["base"] == "main"
    assert pull["body"] == _pull_body(ticket)
    assert [(repo, sha) for repo, sha, _ in github.commits] == [(HOME, BASE_SHA)]
    row = rows(tmp_path / "orch.db")["7"]
    assert (row["base_branch"], row["target_source"]) == ("main", "project")


LOOKALIKE = "release_"
LOOKALIKE_SHA = "d" * 40
NEIGHBOUR = "release"
NEIGHBOUR_SHA = "a" * 40
FENCED_TARGET = f"\n````markdown\n```\nTarget branch: {NEIGHBOUR}\n````\n"


def github_with_lookalikes(repo: str) -> FakeGitHub:
    github = FakeGitHub()
    github.branches[(repo, LOOKALIKE)] = LOOKALIKE_SHA
    github.branches[(repo, NEIGHBOUR)] = NEIGHBOUR_SHA
    return github


def fed_base(tmp_path: Path, tracker: str, text: str) -> tuple[str, str]:
    repo = HOME if tracker == "github" else LINEAR_REPO
    github = github_with_lookalikes(repo)
    if tracker == "github":
        feeder, _, _, _ = make_feeder(tmp_path, github=github)
        github.issues[HOME] = [issue(7, body=text, template=False)]
        head = "factory/issue-7"
    else:
        linear = FakeLinear()
        feeder, _, _, _ = make_feeder(
            tmp_path, orch=LINEAR_ORCH, github=github, linear=linear, factory_text=LINEAR_FACTORY_MD
        )
        ticket = linear_ticket(text)
        linear.issues = [ticket]
        head = ticket.branch
    assert len(feeder.feed_once().fed) == 1
    ((_, sha, _),) = github.commits
    return pull_on(github, head)["base"], sha


@pytest.mark.parametrize("tracker", ["github", "linear"])
def test_a_target_with_a_trailing_underscore_opens_on_that_branch(tmp_path: Path, tracker: str) -> None:
    assert fed_base(tmp_path, tracker, targeted(f"Target branch: {LOOKALIKE}")) == (LOOKALIKE, LOOKALIKE_SHA)


@pytest.mark.parametrize("tracker", ["github", "linear"])
def test_a_fenced_target_leaves_todays_base(tmp_path: Path, tracker: str) -> None:
    base, _ = fed_base(tmp_path, tracker, body() + FENCED_TARGET)
    assert base != NEIGHBOUR


def test_a_starred_target_is_refused_and_wakes_once(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=targeted("Target branch: release*"), template=False)]

    for _ in range(2):
        assert feeder.feed_once().refused == 1

    assert nothing_written(github)
    (wake,) = wakes(tmp_path)
    assert json.loads(wake["detail"])["defects"] == ["bad-target-branch"]
