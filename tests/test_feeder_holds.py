from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.branch_stack import stack_wait, stacked_pulls
from lib.brief.common import detail_of, facts_of, job_section
from lib.dispatcher import CIResult, Comment, Dispatcher, TrackedTicket
from lib.feeder_gates import Progress, RefStatus, blocker_waits, github_ref, parse_gate_lines
from lib.github_actions import GhResult, GitHubAdapterError
from lib.handoff import Handoff, format_comment
from lib.intake_live import GitHubIntakeClient
from lib.poll_source import LivePollSource
from tests.test_feeder import (
    LINEAR_FACTORY_MD,
    LINEAR_ORCH,
    GITHUB_ORCH,
    FakeGitHub,
    FakeLinear,
    FeederSource,
    dispatcher_for,
    issue,
    linear_issue,
    make_feeder,
    rows,
)
from tests.test_dispatcher import TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions, factory
from tests.test_github_actions import RecordedGh
from tests.test_live_poll import FACTORY, FakeGh, config, linear_transport_for
from tests.test_live_poll import linear_issue as live_linear_issue

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


def test_stacked_pulls_skips_a_pr_whose_base_already_has_the_commit() -> None:
    github = stacked_github()
    github.branch_commits[(HOME, "release")] = (A, B)
    github.branches[(HOME, "factory/issue-120")] = B
    github.branch_commits[(HOME, "factory/issue-120")] = (A, B)
    github.pulls[(HOME, 120)] = {"head": "factory/issue-120", "base": "release"}
    github.pulls[(HOME, 121)] = {"head": "factory/issue-120", "base": "main"}
    assert stacked_pulls(github, HOME, "main", BRANCH, own=None) == (113, 121)


def test_stack_wait_names_other_repos_in_full() -> None:
    assert stack_wait(HOME, f"branch {BRANCH}", (7,)).reason == f"waiting on #7 (branch {BRANCH} carries its commits)"
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


def test_commit_pulls_keeps_open_prs_with_their_base() -> None:
    gh = RecordedGh()
    gh.route(("--cache", "60s", f"{REPO_PATH}/commits/{A}/pulls?per_page=100"), ok([
        {"number": 113, "state": "open", "base": {"ref": "main"}},
        {"number": 114, "state": "open", "base": {"ref": FEATURE}},
        {"number": 90, "state": "closed", "base": {"ref": "main"}},
        {"number": True, "state": "open", "base": {"ref": "main"}},
    ]))
    assert GitHubIntakeClient(gh).commit_pulls(HOME, A) == {113: "main", 114: FEATURE}


@pytest.mark.parametrize("base", [None, {}, {"ref": "-x"}])
def test_commit_pulls_refuses_an_open_pr_with_an_unreadable_base(base: object) -> None:
    gh = RecordedGh()
    gh.route(("--cache", "60s", f"{REPO_PATH}/commits/{A}/pulls?per_page=100"), ok([
        {"number": 113, "state": "open", "base": base},
    ]))
    with pytest.raises(GitHubAdapterError, match="#113 base branch"):
        GitHubIntakeClient(gh).commit_pulls(HOME, A)


@pytest.mark.parametrize(
    ("status", "has"), [("behind", True), ("identical", True), ("ahead", False), ("diverged", False)]
)
def test_branch_has_reads_the_compare_of_branch_and_commit(status: str, has: bool) -> None:
    gh = RecordedGh()
    gh.route(("--cache", "60s", f"{REPO_PATH}/compare/{FEATURE}...{A}"), ok({"status": status}))
    assert GitHubIntakeClient(gh).branch_has(HOME, FEATURE, A) is has


@pytest.mark.parametrize("compared", [{}, {"status": None}, {"status": "unknown"}, []])
def test_branch_has_refuses_an_unreadable_compare(compared: object) -> None:
    gh = RecordedGh()
    gh.route(("--cache", "60s", f"{REPO_PATH}/compare/main...{A}"), ok(compared))
    with pytest.raises(GitHubAdapterError):
        GitHubIntakeClient(gh).branch_has(HOME, "main", A)


@pytest.mark.parametrize(("branch", "sha"), [("-x", A), ("main", "abc")])
def test_branch_has_refuses_unsafe_input(branch: str, sha: str) -> None:
    with pytest.raises(GitHubAdapterError):
        GitHubIntakeClient(RecordedGh()).branch_has(HOME, branch, sha)


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


def stack_onto(github: FakeGitHub, branch: str, number: int = 113) -> None:
    github.branches[(HOME, STACKED)] = B
    github.branch_commits[(HOME, STACKED)] = (B,)
    github.pulls[(HOME, number)] = {"head": STACKED, "base": "main", "title": "t", "body": "",
                                    "draft": True}
    github.branch_commits[(HOME, branch)] = (B, C)


def test_a_resumed_intake_rechecks_the_recorded_branch(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(205)]
    github.fail["labels"] = 1
    assert feeder.feed_once().errors == 1
    saved = rows(tmp_path / "orch.db")["205"]
    assert (saved["state"], saved["pr_number"]) == ("error", "501")

    stack_onto(github, BRANCH)
    github.posted.clear()
    report = feeder.feed_once()

    assert report.fed == () and report.blocked == 1
    assert rows(tmp_path / "orch.db")["205"]["state"] == "waiting-blocked"
    assert github.posted == {}, "no brief or go while the branch carries #113"
    assert [json.loads(wake["detail"])["pulls"] for wake in grok_wakes(tmp_path)] == [[113]]


class HeadSource(FeederSource):
    def _ticket(self, ref):
        ticket = super()._ticket(ref)
        if ticket is None:
            return None
        head = self.github_fake.branches[(HOME, BRANCH)]
        return replace(ticket, current_head=head, ci={head: CIResult("success")})


def build_ready(tmp_path: Path):
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(205)]
    source = HeadSource(github)
    dispatcher = dispatcher_for(tmp_path, queue, feeder=feeder)
    dispatcher.poll_once(source)
    build = queue.claim("claude")
    assert build is not None and build.request.action == "build"
    github.branches[(HOME, BRANCH)] = C
    stack_onto(github, BRANCH)
    ready = format_comment(Handoff(
        fields=(("kind", "ready"), ("from", "claude-bot"), ("role", "builder"), ("head", C)),
        body="Ready.",
    ))
    github.posted[(HOME, 501)].append(
        Comment(id="10001", author="maxharris1", body=ready, created_at=10001.0)
    )
    queue.finish(build.id, owner="claude", handoff_comment_id="10001")
    return dispatcher, queue, github, source


def reviews(queue) -> list:
    return [job for job in queue.outstanding() if job.request.action == "review"]


def test_a_branch_stacked_during_its_build_is_held_before_review(tmp_path: Path) -> None:
    dispatcher, queue, github, source = build_ready(tmp_path)

    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    assert reviews(queue) == []
    wakes = grok_wakes(tmp_path)
    assert len(wakes) == 1, "one wake however many polls hold it"
    assert json.loads(wakes[0]["detail"])["pulls"] == [113]

    github.pulls[(HOME, 113)].update(merged=True, state="closed")
    dispatcher.poll_once(source)

    assert [job.request.head for job in reviews(queue)] == [C]


def test_each_stacked_set_reaches_grok_under_its_own_identity(tmp_path: Path) -> None:
    github = stacked_github()
    feeder, queue, _, _ = make_feeder(tmp_path, github=github)
    delivery = FakeGrokDelivery()
    dispatcher = Dispatcher(
        tmp_path / "orch.db", queue, trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(), grok_delivery=delivery,
    )
    feeder.feed_once()
    dispatcher._deliver_grok_jobs(set())
    github.branches[(HOME, "factory/issue-109")] = A
    github.branch_commits[(HOME, "factory/issue-109")] = (A,)
    github.pulls[(HOME, 114)] = {"head": "factory/issue-109", "base": "main", "title": "t",
                                 "body": "", "draft": True}
    feeder.feed_once()
    feeder.feed_once()
    dispatcher._deliver_grok_jobs(set())

    assert [json.loads(job.request.detail)["pulls"] for job in delivery.jobs] == [[113], [113, 114]]
    assert len(set(delivery.logical_keys)) == 2


def ready_for(head: str) -> Comment:
    return Comment(
        id="10001", author="maxharris1", created_at=10001.0,
        body=format_comment(Handoff(
            fields=(("kind", "ready"), ("from", "claude-bot"), ("role", "builder"), ("head", head)),
            body="Ready.",
        )),
    )


@pytest.mark.parametrize("pull_ref", ["actual/pr-branch", "actual/pr+branch", None])
def test_a_linked_linear_pr_is_held_on_its_reviewed_head(tmp_path: Path, pull_ref: str | None) -> None:
    repo, suggested = "acme/widgets", "suggested/aut-42"
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.head = C
    gh.pull = {"state": "open", "title": "Fix login",
               "head": {"ref": pull_ref} if pull_ref else {}}
    item = live_linear_issue()
    item["gitBranchName"] = suggested
    source = LivePollSource(
        config(linear=True), environ={"LINEAR_KEY": "test-value"},
        gh_runner=gh, linear_transport=linear_transport_for(item),
    )
    source.begin_cycle()
    ticket = source._linear_ticket(source.config.projects[0], item)
    assert ticket.current_head == C

    pr_branch = pull_ref or "linked/pr"
    github = FakeGitHub()
    github.branches[(repo, suggested)] = A
    github.branches[(repo, pr_branch)] = C
    github.branch_commits[(repo, pr_branch)] = (B, C)
    github.branches[(repo, "upstream/pr-113")] = B
    github.pulls[(repo, 12)] = {"head": pr_branch, "base": "main"}
    github.pulls[(repo, 113)] = {"head": "upstream/pr-113", "base": "main"}
    feeder, queue, _, _ = make_feeder(
        tmp_path, github=github, orch=GITHUB_ORCH.replace("owner/app", repo)
    )
    dispatcher = dispatcher_for(tmp_path, queue, feeder=feeder)
    dispatcher.process(replace(ticket, comments=(ready_for(C),), factory_md=ticket.factory))

    assert reviews(queue) == []
    assert [json.loads(wake["detail"])["pulls"] for wake in grok_wakes(tmp_path)] == [[113]]


def test_a_fork_pr_is_held_on_its_pinned_head_not_a_same_named_base_branch(tmp_path: Path) -> None:
    repo = "acme/widgets"
    cfg = config()
    project = replace(cfg.projects[0], ready_starts_review=True)
    cfg = replace(cfg, projects=(project,))
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.head = C
    gh.pull = {"state": "open", "title": "Fix login",
               "head": {"ref": "feature", "label": "contributor:feature",
                        "repo": {"full_name": "contributor/widgets"}}}
    source = LivePollSource(cfg, gh_runner=gh)
    source.begin_cycle()
    ticket = source._github_ticket(project, source._factory(project), "12")

    reads = RecordedGh()
    prefix = f"repos/{repo}"
    reads.route((f"{prefix}/compare/main...feature",),
                ok({"total_commits": 1, "commits": [{"sha": A}]}))
    reads.route((f"{prefix}/compare/main...{C}",),
                ok({"total_commits": 2, "commits": [{"sha": B}, {"sha": C}]}))
    reads.route((f"{prefix}/pulls/12",), ok({"base": {"ref": "main"}}))
    for sha, pulls in ((A, []), (B, [{"number": 113, "state": "open", "base": {"ref": "main"}}]),
                       (C, [{"number": 12, "state": "open", "base": {"ref": "main"}}])):
        reads.route(("--cache", "60s", f"{prefix}/commits/{sha}/pulls?per_page=100"), ok(pulls))
    reads.route(("--cache", "60s", f"{prefix}/compare/main...{B}"), ok({"status": "ahead"}))
    feeder, queue, _, _ = make_feeder(tmp_path, orch=GITHUB_ORCH.replace("owner/app", repo))
    feeder.github = GitHubIntakeClient(reads)
    dispatcher = dispatcher_for(tmp_path, queue, feeder=feeder)
    dispatcher.process(replace(ticket, comments=(ready_for(C),)))

    assert reviews(queue) == []
    assert [json.loads(wake["detail"])["pulls"] for wake in grok_wakes(tmp_path)] == [[113]]
    assert not [call for call in reads.calls if "compare/main...feature" in str(call)]


LINEAR_REPO = "acme/linear-app"
FEATURE = "feature/upload-only-intake"
AUT = linear_issue("AUT-2231")
F1, F2, OTHER, OWN = "1" * 40, "2" * 40, "3" * 40, "4" * 40
DEV = "7" * 40
BASE_NOTE = (
    f"feeder: {LINEAR_REPO}#AUT-2231 recorded base dev differs from PR base {FEATURE}; "
    "using the PR base"
)


def upload_only_intake(
    github: FakeGitHub, *, beyond_base: tuple[str, ...] = (), dev_merge: bool = False
) -> None:
    on_dev = (DEV,) if dev_merge else ()
    pulls = [(1400, FEATURE, (F1, F2)), (1452, "max/aut-2200", (F1,)),
             (1470, "max/aut-2240", (F1, F2, OTHER, *on_dev))]
    if dev_merge:
        pulls.append((1475, "max/aut-2250", (DEV, "8" * 40)))
    for number, head, commits in pulls:
        github.branches[(LINEAR_REPO, head)] = commits[-1]
        github.branch_commits[(LINEAR_REPO, head)] = commits
        github.pulls[(LINEAR_REPO, number)] = {"head": head, "base": "dev"}
    github.branch_commits[(LINEAR_REPO, "dev")] = on_dev
    github.branches[(LINEAR_REPO, AUT.branch)] = OWN
    github.branch_commits[(LINEAR_REPO, AUT.branch)] = (F1, F2, *beyond_base, OWN)


def linear_feeder(tmp_path: Path, github: FakeGitHub):
    linear = FakeLinear()
    linear.issues = [AUT]
    feeder, queue, _, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, github=github, linear=linear,
                                      factory_text=LINEAR_FACTORY_MD)
    logs: list[str] = []
    feeder.log = logs.append
    return feeder, queue, linear, logs


def review_after_retarget(
    tmp_path: Path, *, beyond_base: tuple[str, ...] = (), dev_merge: bool = False
):
    github = FakeGitHub()
    github._next_pr = 1460
    feeder, queue, _, logs = linear_feeder(tmp_path, github)
    assert [ref.ticket_id for ref in feeder.feed_once().fed] == ["AUT-2231"]
    assert rows(tmp_path / "orch.db")["AUT-2231"]["base_branch"] == "dev"
    upload_only_intake(github, beyond_base=beyond_base, dev_merge=dev_merge)
    github.pulls[(LINEAR_REPO, 1461)]["base"] = FEATURE
    ticket = TrackedTicket(
        repo=LINEAR_REPO, ticket_id="AUT-2231",
        factory=replace(factory(tickets="linear", ship="handoff"), base_branch="dev"),
        comments=(ready_for(OWN),), current_head=OWN, ci={OWN: CIResult("success")},
        risk="low", pr_number="1461", ready_starts_review=True,
    )
    return dispatcher_for(tmp_path, queue, feeder=feeder), queue, ticket, logs


def test_a_pr_on_a_feature_base_is_reviewed_against_that_base(tmp_path: Path) -> None:
    dispatcher, queue, ticket, logs = review_after_retarget(tmp_path)

    dispatcher.process(ticket)

    queued = reviews(queue)
    assert [job.request.head for job in queued] == [OWN]
    assert grok_wakes(tmp_path) == []
    assert logs.count(BASE_NOTE) == 1
    assert rows(tmp_path / "orch.db")["AUT-2231"]["base_branch"] == FEATURE
    assert json.loads(queued[0].request.detail)["base_branch"] == FEATURE
    brief = job_section(queued[0], "codex-bot", "review", facts_of(queued[0], detail_of(queued[0]), None), None)
    assert f"- base branch: {FEATURE}" in brief.splitlines()


def test_a_head_carrying_another_pr_beyond_its_pr_base_is_still_held(tmp_path: Path) -> None:
    dispatcher, queue, ticket, logs = review_after_retarget(tmp_path, beyond_base=(OTHER,))

    dispatcher.process(ticket)
    dispatcher.process(ticket)

    assert reviews(queue) == []
    assert [json.loads(wake["detail"])["pulls"] for wake in grok_wakes(tmp_path)] == [[1470]]
    assert logs.count(BASE_NOTE) == 1, "noted once per head however many polls hold it"
    assert rows(tmp_path / "orch.db")["AUT-2231"]["base_branch"] == FEATURE


def test_a_merge_of_dev_that_open_dev_prs_carry_does_not_hold_review(tmp_path: Path) -> None:
    dispatcher, queue, ticket, _ = review_after_retarget(tmp_path, beyond_base=(DEV,), dev_merge=True)

    dispatcher.process(ticket)

    queued = reviews(queue)
    assert [job.request.head for job in queued] == [OWN]
    assert json.loads(queued[0].request.detail)["base_branch"] == FEATURE
    assert grok_wakes(tmp_path) == []


def test_intake_feeds_a_branch_carrying_a_merge_of_dev(tmp_path: Path) -> None:
    github = FakeGitHub()
    upload_only_intake(github, beyond_base=(DEV,), dev_merge=True)
    github.pulls[(LINEAR_REPO, 1461)] = {"head": AUT.branch, "base": FEATURE}
    feeder, _, _, _ = linear_feeder(tmp_path, github)

    report = feeder.feed_once()

    assert [ref.ticket_id for ref in report.fed] == ["AUT-2231"] and report.blocked == 0
    assert grok_wakes(tmp_path) == []


def test_intake_holds_on_a_feature_pr_carrying_a_commit_dev_already_has(tmp_path: Path) -> None:
    github = FakeGitHub()
    upload_only_intake(github, beyond_base=(DEV,), dev_merge=True)
    github.branches[(LINEAR_REPO, "max/aut-2260")] = "9" * 40
    github.branch_commits[(LINEAR_REPO, "max/aut-2260")] = (F1, F2, DEV, "9" * 40)
    github.pulls[(LINEAR_REPO, 1480)] = {"head": "max/aut-2260", "base": FEATURE}
    github.pulls[(LINEAR_REPO, 1461)] = {"head": AUT.branch, "base": FEATURE}
    feeder, _, _, _ = linear_feeder(tmp_path, github)

    report = feeder.feed_once()

    assert report.fed == () and report.blocked == 1
    assert rows(tmp_path / "orch.db")["AUT-2231"]["waiting_reason"] == (
        f"waiting on #1480 (branch {AUT.branch} carries its commits)"
    )


def test_intake_reuses_an_open_pr_against_its_own_base(tmp_path: Path) -> None:
    github = FakeGitHub()
    upload_only_intake(github)
    github.pulls[(LINEAR_REPO, 1461)] = {"head": AUT.branch, "base": FEATURE}
    feeder, _, linear, logs = linear_feeder(tmp_path, github)

    first = feeder.feed_once()
    feeder.feed_once()

    assert [ref.ticket_id for ref in first.fed] == ["AUT-2231"] and first.blocked == 0
    assert grok_wakes(tmp_path) == []
    assert logs.count(BASE_NOTE) == 1
    assert rows(tmp_path / "orch.db")["AUT-2231"]["base_branch"] == FEATURE
    assert f"on branch `{AUT.branch}` off `{FEATURE}`" in linear.posted[AUT.linear_id][0].body


def test_intake_holds_an_open_pr_carrying_another_pr_beyond_its_base(tmp_path: Path) -> None:
    github = FakeGitHub()
    upload_only_intake(github, beyond_base=(OTHER,))
    github.pulls[(LINEAR_REPO, 1461)] = {"head": AUT.branch, "base": FEATURE}
    feeder, _, linear, logs = linear_feeder(tmp_path, github)

    report = feeder.feed_once()

    assert report.fed == () and report.blocked == 1
    row = rows(tmp_path / "orch.db")["AUT-2231"]
    assert row["waiting_reason"] == f"waiting on #1470 (branch {AUT.branch} carries its commits)"
    assert row["base_branch"] == FEATURE
    assert logs.count(BASE_NOTE) == 1
    assert linear.posted == {}


def test_a_held_branch_is_noted_again_on_each_new_head(tmp_path: Path) -> None:
    github = FakeGitHub()
    upload_only_intake(github, beyond_base=(OTHER,))
    github.pulls[(LINEAR_REPO, 1461)] = {"head": AUT.branch, "base": FEATURE}
    feeder, _, _, logs = linear_feeder(tmp_path, github)

    assert feeder.feed_once().blocked == 1
    assert feeder.feed_once().blocked == 1
    assert logs.count(BASE_NOTE) == 1
    head = "5" * 40
    github.branches[(LINEAR_REPO, AUT.branch)] = head
    github.branch_commits[(LINEAR_REPO, AUT.branch)] += (head,)
    assert feeder.feed_once().blocked == 1
    assert feeder.feed_once().blocked == 1

    assert logs.count(BASE_NOTE) == 2


def github_pr_on_a_feature_base(tmp_path: Path):
    github = FakeGitHub()
    github.issues[HOME] = [issue(205)]
    github.branches[(HOME, BRANCH)] = OWN
    github.branch_commits[(HOME, BRANCH)] = (OWN,)
    github.branches[(HOME, FEATURE)] = "6" * 40
    github.pulls[(HOME, 77)] = {"head": BRANCH, "base": FEATURE}
    feeder, _, _, _ = make_feeder(tmp_path, github=github)
    logs: list[str] = []
    feeder.log = logs.append
    assert len(feeder.feed_once().fed) == 1
    assert rows(tmp_path / "orch.db")["205"]["base_branch"] == FEATURE
    ticket = TrackedTicket(repo=HOME, ticket_id="77", factory=factory(), pr_number="77",
                           comments=(), current_head=OWN, ci={}, risk="low")
    return feeder, github, ticket, logs


def test_one_head_is_noted_once_from_intake_to_review(tmp_path: Path) -> None:
    feeder, _, ticket, logs = github_pr_on_a_feature_base(tmp_path)

    assert feeder.review_base_unless_stacked(ticket, OWN) == FEATURE
    assert feeder.review_base_unless_stacked(ticket, OWN) == FEATURE

    assert [line for line in logs if "differs from PR base" in line] == [
        f"feeder: {HOME}#205 recorded base main differs from PR base {FEATURE}; using the PR base"
    ]


def test_a_pr_retargeted_to_the_configured_base_corrects_its_row(tmp_path: Path) -> None:
    feeder, github, ticket, logs = github_pr_on_a_feature_base(tmp_path)
    github.pulls[(HOME, 77)]["base"] = "main"

    assert feeder.review_base_unless_stacked(ticket, OWN) == "main"

    assert rows(tmp_path / "orch.db")["205"]["base_branch"] == "main"
    assert logs[-1] == (
        f"feeder: {HOME}#77 recorded base {FEATURE} differs from PR base main; using the PR base"
    )


def test_a_branch_with_no_pr_is_read_against_the_recorded_base(tmp_path: Path) -> None:
    github = FakeGitHub()
    upload_only_intake(github)
    feeder, _, _, logs = linear_feeder(tmp_path, github)

    report = feeder.feed_once()

    assert report.fed == () and report.blocked == 1
    assert rows(tmp_path / "orch.db")["AUT-2231"]["waiting_reason"] == (
        f"waiting on #1400, #1452, #1470 (branch {AUT.branch} carries their commits)"
    )
    assert not [line for line in logs if "differs from PR base" in line]


def test_pull_base_reads_the_open_prs_base_ref() -> None:
    gh = RecordedGh()
    gh.route((f"{REPO_PATH}/pulls/1461",), ok({"state": "open", "base": {"ref": FEATURE}}))
    assert GitHubIntakeClient(gh).pull_base(HOME, 1461) == FEATURE


@pytest.mark.parametrize("pull", [{"state": "open"}, {"base": {}}, {"base": {"ref": "-x"}}])
def test_pull_base_refuses_an_unreadable_base(pull: dict) -> None:
    gh = RecordedGh()
    gh.route((f"{REPO_PATH}/pulls/1461",), ok(pull))
    with pytest.raises(GitHubAdapterError, match="base branch"):
        GitHubIntakeClient(gh).pull_base(HOME, 1461)


def test_intake_uses_the_tracked_pr_when_its_branch_has_two_open_prs(tmp_path: Path) -> None:
    github = FakeGitHub()
    upload_only_intake(github, beyond_base=(DEV,), dev_merge=True)
    github.pulls[(LINEAR_REPO, 1460)] = {"head": AUT.branch, "base": "dev"}
    github.pulls[(LINEAR_REPO, 1461)] = {"head": AUT.branch, "base": FEATURE}
    feeder, queue, _, logs = linear_feeder(tmp_path, github)
    dispatcher_for(tmp_path, queue, feeder)
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute(
            "INSERT INTO ticket_sources(repo, ticket_id, source, pr_number) VALUES (?, ?, ?, ?)",
            (LINEAR_REPO, "AUT-2231", "AUT-2231", "1461"),
        )

    feeder.feed_once()
    feeder.feed_once()

    row = rows(tmp_path / "orch.db")["AUT-2231"]
    assert row["base_branch"] == FEATURE
    assert row["waiting_reason"] == f"waiting on #1460 (branch {AUT.branch} carries its commits)"
    assert logs.count(BASE_NOTE) == 1


def test_intake_finds_the_branch_pr_when_the_tracked_pr_closed(tmp_path: Path) -> None:
    github = FakeGitHub()
    upload_only_intake(github)
    github.pulls[(LINEAR_REPO, 1450)] = {"head": AUT.branch, "base": "dev", "state": "closed"}
    github.pulls[(LINEAR_REPO, 1461)] = {"head": AUT.branch, "base": FEATURE}
    feeder, queue, _, logs = linear_feeder(tmp_path, github)
    dispatcher_for(tmp_path, queue, feeder)
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute(
            "INSERT INTO ticket_sources(repo, ticket_id, source, pr_number) VALUES (?, ?, ?, ?)",
            (LINEAR_REPO, "AUT-2231", "AUT-2231", "1450"),
        )

    report = feeder.feed_once()

    assert [ref.ticket_id for ref in report.fed] == ["AUT-2231"]
    row = rows(tmp_path / "orch.db")["AUT-2231"]
    assert (row["pr_number"], row["base_branch"]) == ("1461", FEATURE)
    assert logs.count(BASE_NOTE) == 1


RELEASE = "release+stable"


def test_a_pr_on_a_git_valid_base_outside_the_factory_names_still_holds(tmp_path: Path) -> None:
    gh = RecordedGh()
    for sha in (A, B, C):
        pulls = [{"number": 113, "state": "open", "base": {"ref": RELEASE}}] if sha in (A, B) else []
        gh.route(("--cache", "60s", f"{REPO_PATH}/commits/{sha}/pulls?per_page=100"), ok(pulls))
    github = stacked_github()
    github.pulls[(HOME, 113)]["base"] = RELEASE
    github.branch_commits[(HOME, RELEASE)] = (A,)
    github.commit_pulls = GitHubIntakeClient(gh).commit_pulls
    feeder, _, _, _ = make_feeder(tmp_path, github=github)

    report = feeder.feed_once()

    assert (report.blocked, report.errors) == (1, 0)
    assert rows(tmp_path / "orch.db")["205"]["state"] == "waiting-blocked"
    assert [json.loads(wake["detail"])["pulls"] for wake in grok_wakes(tmp_path)] == [[113]]


def test_branch_reads_accept_a_git_valid_name_and_encode_it() -> None:
    gh = RecordedGh()
    encoded = "release%2Bstable"
    gh.route(("--cache", "60s", f"{REPO_PATH}/commits/{A}/pulls?per_page=100"),
             ok([{"number": 113, "state": "open", "base": {"ref": RELEASE}}]))
    gh.route(("--cache", "60s", f"{REPO_PATH}/compare/{encoded}...{A}"), ok({"status": "behind"}))
    gh.route((f"{REPO_PATH}/pulls/1461",), ok({"state": "open", "base": {"ref": RELEASE}}))
    gh.route((f"{REPO_PATH}/git/ref/heads/{encoded}",), ok({"object": {"sha": A}}))
    gh.route((f"{REPO_PATH}/compare/{encoded}...{BRANCH}",), ok({"total_commits": 1, "commits": [{"sha": B}]}))
    client = GitHubIntakeClient(gh)

    assert client.commit_pulls(HOME, A) == {113: RELEASE}
    assert client.branch_has(HOME, RELEASE, A) is True
    assert client.pull_base(HOME, 1461) == RELEASE
    assert client.branch_sha(HOME, RELEASE) == A
    assert client.ahead_commits(HOME, RELEASE, BRANCH) == (B,)


@pytest.mark.parametrize("name", ["-x", "a..b", "a b", "a:b", "a.lock", "x/", "a@{1}", "@", ""])
def test_branch_reads_refuse_a_name_git_refuses(name: str) -> None:
    client = GitHubIntakeClient(RecordedGh())
    with pytest.raises(GitHubAdapterError, match="not safe"):
        client.branch_sha(HOME, name)
    with pytest.raises(GitHubAdapterError, match="not safe"):
        client.branch_has(HOME, name, A)
