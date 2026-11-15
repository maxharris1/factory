from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lib.branch_stack import stack_wait, stacked_pulls
from lib.feeder_gates import Progress, RefStatus, blocker_waits, github_ref, parse_gate_lines
from lib.github_actions import GhResult, GitHubAdapterError
from lib.intake_live import GitHubIntakeClient
from tests.test_feeder import (
    LINEAR_FACTORY_MD,
    LINEAR_ORCH,
    FakeGitHub,
    FakeLinear,
    issue,
    linear_issue,
    make_feeder,
    rows,
)
from tests.test_github_actions import RecordedGh

HOME = "owner/app"
BRANCH = "factory/issue-205"
STACKED = "factory/issue-108"
A, B, C = "a" * 40, "b" * 40, "c" * 40


def stacked_github() -> FakeGitHub:
    github = FakeGitHub()
    github.issues[HOME] = [issue(205)]
    github.branches[(HOME, BRANCH)] = C
    github.branch_commits[(HOME, BRANCH)] = (A, B, C)
    github.branches[(HOME, STACKED)] = B
    github.branch_commits[(HOME, STACKED)] = (A, B)
    github.pulls[(HOME, 113)] = {"head": STACKED, "base": "main", "title": "t", "body": "", "draft": True}
    return github


def grok_wakes(tmp_path: Path) -> list[dict]:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM jobs WHERE recipient = 'grok' AND action = 'blocked'"
            )
        ]


def test_a_branch_carrying_an_open_prs_commits_waits_with_one_wake(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, github=stacked_github())

    first = feeder.feed_once()
    second = feeder.feed_once()

    assert first.fed == () and second.fed == ()
    assert (first.blocked, second.blocked) == (1, 1)
    row = rows(tmp_path / "orch.db")["205"]
    assert row["state"] == "waiting-blocked"
    assert row["waiting_reason"] == f"waiting on #113 (branch {BRANCH} carries its commits)"
    assert [pull["head"] for pull in github.pulls.values()] == [STACKED]
    assert github.posted == {}
    wakes = grok_wakes(tmp_path)
    assert len(wakes) == 1, "one wake however many polls hold it"
    detail = json.loads(wakes[0]["detail"])
    assert detail["pulls"] == [113]
    assert "#113" in detail["detail"] and BRANCH in detail["detail"]


def test_the_held_ticket_feeds_on_its_own_branch_once_the_pr_merges(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, github=stacked_github())
    assert feeder.feed_once().fed == ()

    github.pulls[(HOME, 113)]["merged"] = True
    github.pulls[(HOME, 113)]["state"] = "closed"
    report = feeder.feed_once()

    assert len(report.fed) == 1
    number = int(report.fed[0].ticket_id)
    assert github.pulls[(HOME, number)]["head"] == BRANCH
    assert github.commits == [], "the existing branch is reused, not restarted"
    assert rows(tmp_path / "orch.db")["205"]["state"] == "fed"
    assert len(grok_wakes(tmp_path)) == 1


def test_a_new_stacked_pr_wakes_again(tmp_path: Path) -> None:
    github = stacked_github()
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    feeder.feed_once()

    github.branches[(HOME, "factory/issue-109")] = A
    github.branch_commits[(HOME, "factory/issue-109")] = (A,)
    github.pulls[(HOME, 114)] = {"head": "factory/issue-109", "base": "main", "title": "t",
                                 "body": "", "draft": True}
    feeder.feed_once()
    feeder.feed_once()

    assert sorted(json.loads(wake["detail"])["pulls"] for wake in grok_wakes(tmp_path)) == [
        [113], [113, 114]
    ]
    assert rows(tmp_path / "orch.db")["205"]["waiting_reason"] == (
        f"waiting on #113, #114 (branch {BRANCH} carries their commits)"
    )


def test_the_tickets_own_pr_on_its_branch_does_not_hold_it(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.issues[HOME] = [issue(205)]
    github.branches[(HOME, BRANCH)] = C
    github.branch_commits[(HOME, BRANCH)] = (A, C)
    github.pulls[(HOME, 77)] = {"head": BRANCH, "base": "main", "title": "t", "body": "", "draft": True}
    feeder, _, _, _ = make_feeder(tmp_path, github=github)

    report = feeder.feed_once()

    assert [ref.ticket_id for ref in report.fed] == ["77"]
    assert grok_wakes(tmp_path) == []


def test_a_new_branch_is_never_read_for_stacked_commits(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.issues[HOME] = [issue(205)]
    feeder, _, _, _ = make_feeder(tmp_path, github=github)

    assert len(feeder.feed_once().fed) == 1
    assert not [call for call in github.calls if call.startswith("ahead:")]


def test_a_linear_branch_carrying_an_open_prs_commits_waits(tmp_path: Path) -> None:
    repo = "acme/linear-app"
    linear = FakeLinear()
    item = linear_issue("AUT-7")
    linear.issues = [item]
    github = FakeGitHub()
    github.branches[(repo, item.branch)] = C
    github.branch_commits[(repo, item.branch)] = (A, C)
    github.branches[(repo, "max/aut-6")] = A
    github.pulls[(repo, 40)] = {"head": "max/aut-6", "base": "dev", "title": "t", "body": "", "draft": True}
    feeder, _, _, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, github=github, linear=linear,
                                  factory_text=LINEAR_FACTORY_MD)

    report = feeder.feed_once()

    assert report.fed == () and report.blocked == 1
    assert rows(tmp_path / "orch.db")["AUT-7"]["waiting_reason"] == (
        f"waiting on #40 (branch {item.branch} carries its commits)"
    )
    assert linear.posted == {}
    assert len(grok_wakes(tmp_path)) == 1


def test_stacked_pulls_skips_the_own_pr_and_sorts() -> None:
    github = stacked_github()
    github.pulls[(HOME, 99)] = {"head": BRANCH, "base": "main", "title": "t", "body": "", "draft": True}
    assert stacked_pulls(github, HOME, "main", BRANCH, own=99) == (113,)
    assert stacked_pulls(github, HOME, "main", BRANCH, own=None) == (99, 113)


def test_stack_wait_names_other_repos_in_full() -> None:
    assert stack_wait(HOME, BRANCH, (7,)).reason == f"waiting on #7 (branch {BRANCH} carries its commits)"
    assert stack_wait(HOME, BRANCH, (7, 8)).attention is None


REPO_PATH = "repos/owner/app"
COMPARE = (f"{REPO_PATH}/compare/main...{BRANCH}",)


def ok(value: object) -> GhResult:
    return GhResult(0, json.dumps(value), "")


def test_ahead_commits_reads_the_compare_of_base_and_branch() -> None:
    gh = RecordedGh()
    gh.route(COMPARE, ok({"total_commits": 2, "commits": [{"sha": A}, {"sha": B}]}))
    assert GitHubIntakeClient(gh).ahead_commits(HOME, "main", BRANCH) == (A, B)


def test_ahead_commits_refuses_a_partial_list() -> None:
    gh = RecordedGh()
    gh.route(COMPARE, ok({"total_commits": 300, "commits": [{"sha": A}]}))
    with pytest.raises(GitHubAdapterError, match="300 commits"):
        GitHubIntakeClient(gh).ahead_commits(HOME, "main", BRANCH)


def test_commit_pulls_keeps_open_prs_only() -> None:
    gh = RecordedGh()
    gh.route(("--cache", "60s", f"{REPO_PATH}/commits/{A}/pulls?per_page=100"), ok([
        {"number": 113, "state": "open"},
        {"number": 90, "state": "closed"},
        {"number": True, "state": "open"},
    ]))
    assert GitHubIntakeClient(gh).commit_pulls(HOME, A) == frozenset({113})


@pytest.mark.parametrize(
    ("line", "numbers"),
    [
        ("Depends on: #4", {4}),
        ("Depends on #4 and owner/app#5", {4, 5}),
        ("**Depends on:** #156 (merged). T22a (#204) dropped", {156, 204}),
        ("- depends ON: https://github.com/owner/app/pull/9", {9}),
        ("Depends on: none", set()),
    ],
)
def test_depends_on_parses_as_a_blocker(line: str, numbers: set[int]) -> None:
    assert parse_gate_lines(line, HOME).blocked_by == {github_ref(HOME, n) for n in numbers}


def test_depends_on_in_fenced_code_or_prose_is_not_a_gate() -> None:
    body = "```\nDepends on: #4\n```\nThis depends on: #5\n"
    assert not parse_gate_lines(body, HOME)


def test_depends_on_and_blocked_by_share_one_blocker_graph() -> None:
    gated = {
        github_ref(HOME, 1): parse_gate_lines("Depends on: #2", HOME),
        github_ref(HOME, 2): parse_gate_lines("Blocked by #1", HOME),
    }
    waits = blocker_waits(gated, {})
    assert {wait.attention for wait in waits.values()} == {"blocker-cycle"}


def depends_issue(number: int, on: int):
    body = (
        "Fix the thing.\n\nRisk: low\nPlan first: no\n"
        f"Depends on: #{on}\n\n## Acceptance criteria\n\n- [ ] It works.\n"
    )
    return issue(number, body=body, template=False)


def test_depends_on_waits_then_feeds_after_the_merge(tmp_path: Path) -> None:
    github = FakeGitHub()
    github.issues[HOME] = [depends_issue(205, 156)]
    github.statuses[github_ref(HOME, 156)] = RefStatus(Progress.OPEN)
    feeder, _, _, _ = make_feeder(tmp_path, github=github)

    held = feeder.feed_once()
    assert held.fed == () and held.blocked == 1
    assert rows(tmp_path / "orch.db")["205"]["waiting_reason"] == "waiting on #156"

    github.statuses[github_ref(HOME, 156)] = RefStatus(Progress.DONE)
    assert len(feeder.feed_once().fed) == 1
