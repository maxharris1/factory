from __future__ import annotations

from pathlib import Path

import pytest

from lib.feeder import _pull_body
from tests.test_feeder import LINEAR_FACTORY_MD, LINEAR_ORCH, FakeGitHub, FakeLinear, issue, make_feeder, rows
from tests.test_feeder_holds import grok_wakes
from tests.test_feeder_target import LINEAR_REPO, linear_ticket, pull_on, targeted

HOME = "owner/app"
PARENT = "factory/issue-6"
CHILD = "factory/issue-7"
A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "e" * 40


def with_parent(repo: str, **pull: object) -> FakeGitHub:
    github = FakeGitHub()
    github.branches[(repo, PARENT)] = B
    github.branch_commits[(repo, PARENT)] = (A, B)
    github.pulls[(repo, 113)] = {"head": PARENT, "base": "main", "title": "t", "body": "", "draft": True, **pull}
    return github


def parent_columns(row: dict) -> tuple:
    return row["stack_parent_pr"], row["stack_parent_branch"], row["stack_parent_head_at_feed"]


def test_a_github_child_records_and_names_its_parent(tmp_path: Path) -> None:
    github = with_parent(HOME)
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    github.issues[HOME] = [issue(7, body=targeted(f"Target branch: {PARENT}"), template=False)]

    assert len(feeder.feed_once().fed) == 1

    assert parent_columns(rows(tmp_path / "orch.db")["7"]) == (113, PARENT, B)
    pull = pull_on(github, CHILD)
    assert pull["base"] == PARENT
    assert "Stacked on #113. Merge #113 first." in pull["body"]
    brief = github.posted[(HOME, 501)][0].body
    assert f"- PR: #501 on branch `{CHILD}` off `{PARENT}` (stacked on #113 at `{B}`)" in brief


def test_a_linear_child_records_and_names_its_parent(tmp_path: Path) -> None:
    github = with_parent(LINEAR_REPO)
    linear = FakeLinear()
    feeder, _, _, _ = make_feeder(
        tmp_path, orch=LINEAR_ORCH, github=github, linear=linear, factory_text=LINEAR_FACTORY_MD
    )
    ticket = linear_ticket(targeted(f"Target branch: {PARENT}"))
    linear.issues = [ticket]

    assert len(feeder.feed_once().fed) == 1

    assert parent_columns(rows(tmp_path / "orch.db")["AUT-7"]) == (113, PARENT, B)
    assert "Stacked on #113. Merge #113 first." in pull_on(github, ticket.branch)["body"]
    assert f"(stacked on #113 at `{B}`)" in linear.posted["uuid-AUT-7"][0].body


@pytest.mark.parametrize("pull", [{"state": "closed"}, {"merged": True, "state": "closed"}])
def test_a_target_whose_pr_is_not_open_has_no_parent(tmp_path: Path, pull: dict) -> None:
    github = with_parent(HOME, **pull)
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    ticket = issue(7, body=targeted(f"Target branch: {PARENT}"), template=False)
    github.issues[HOME] = [ticket]

    assert len(feeder.feed_once().fed) == 1

    assert parent_columns(rows(tmp_path / "orch.db")["7"]) == (None, None, None)
    assert pull_on(github, CHILD)["body"] == _pull_body(ticket)
    assert "stacked on" not in github.posted[(HOME, 501)][0].body


def test_a_project_base_that_heads_an_open_pr_is_no_parent(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.pulls[(HOME, 113)] = {"head": "main", "base": "release", "title": "t", "body": "", "draft": True}
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    github.issues[HOME] = [issue(7, template=True)]

    assert len(feeder.feed_once().fed) == 1

    assert parent_columns(rows(tmp_path / "orch.db")["7"]) == (None, None, None)


def test_the_parent_head_at_feed_stays_the_first_one_read(tmp_path: Path) -> None:
    github = with_parent(HOME)
    github.fail["pull"] = 1
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    github.issues[HOME] = [issue(7, body=targeted(f"Target branch: {PARENT}"), template=False)]
    assert feeder.feed_once().fed == ()

    github.branches[(HOME, PARENT)] = D
    github.branch_commits[(HOME, PARENT)] = (A, B, D)
    assert len(feeder.feed_once().fed) == 1

    assert parent_columns(rows(tmp_path / "orch.db")["7"]) == (113, PARENT, B)
    assert f"(stacked on #113 at `{B}`)" in github.posted[(HOME, 501)][0].body


def with_child_branch() -> FakeGitHub:
    github = with_parent(HOME)
    github.branches[(HOME, CHILD)] = C
    github.branch_commits[(HOME, CHILD)] = (A, B, C)
    return github


def test_a_child_targeting_its_parent_branch_is_not_held(tmp_path: Path) -> None:
    github = with_child_branch()
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    github.issues[HOME] = [issue(7, body=targeted(f"Target branch: {PARENT}"), template=False)]

    report = feeder.feed_once()

    assert len(report.fed) == 1 and report.blocked == 0
    assert grok_wakes(tmp_path) == []
    assert pull_on(github, CHILD)["base"] == PARENT
    assert github.commits == [], "the existing child branch is reused"


def test_a_child_targeting_the_default_branch_with_its_parents_commits_is_held(tmp_path: Path) -> None:
    github = with_child_branch()
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    github.issues[HOME] = [issue(7, body=targeted("Target branch: default"), template=False)]

    report = feeder.feed_once()

    assert report.fed == () and report.blocked == 1
    row = rows(tmp_path / "orch.db")["7"]
    assert row["state"] == "waiting-blocked"
    assert row["waiting_reason"] == f"waiting on #113 (branch {CHILD} carries its commits)"
    assert [wake["action"] for wake in grok_wakes(tmp_path)] == ["blocked"]
