"""Backlog feeder (issue #85 A): tagged tickets become queued jobs by themselves.

Every test uses a fake GitHub and a fake Linear client. Nothing here reaches
a real tracker, and no test needs a real token or signing secret.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from lib import device_sync
from lib.branch_stack import OpenPull
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.dispatch.types import BaseCheck, ParentFacts
from lib.dispatcher import Comment, Dispatcher, TicketRef, TrackedTicket
from lib.factory_md import parse_factory_context
from lib.feeder import (
    BODY_LIMIT,
    CLAIM_SECONDS,
    FEEDER_TABLE,
    Feeder,
    IntakeIssue,
    IssueRecord,
    ensure_tables,
    feeder_counts,
    feeder_items,
    go_state_for,
)
from lib.feeder_gates import Progress, Ref, RefStatus
from lib.followups import has_marker, split_parent
from lib.github_actions import GhResult, GitHubReviewActions
from lib.handoff import Handoff, format_comment, parse_comment
from lib.machines_yml import load_machines_yml
from lib.orch_yml import load_orch_yml
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions, factory
from tests.test_settings_apply import SignalTransport

FACTORY_MD = """---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: none
ship: merge
skills: []
feeder:
  max_holders_per_path: 1
---

## Context
Project rule for every brief: run the suite with `make test`.
"""

LINEAR_FACTORY_MD = FACTORY_MD.replace("base_branch: main", "base_branch: dev").replace(
    "tickets: github", "tickets: linear"
).replace("ship: merge", "ship: handoff")

REQUIRED_MD = FACTORY_MD.replace("feeder:\n", "feeder:\n  required_checks: [\"test (*)\"]\n")

BASE_SHA = "a" * 40


def orch_config(tmp_path: Path, body: str):
    path = tmp_path / "orch.yml"
    path.write_text(body, encoding="utf-8")
    config, findings = load_orch_yml(path, allow_placeholders=True)
    assert config is not None, findings
    return config


GITHUB_ORCH = """
poll_seconds: 60
grok:
  webhook_url_env: FACTORY_WEBHOOK_URL
  webhook_key_env: FACTORY_WEBHOOK_KEY
trusted_logins:
  maxharris1: [grok-bot, claude-bot, codex-bot]
projects:
  - repo: owner/app
    factory_ref: main
    intake:
      label: factory:ready
"""

LINEAR_ORCH = """
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
    linear_project_id: proj-1
    intake:
      linear_state: In Progress
"""


def machines(tmp_path: Path, *, box: int = 3, pc: int = 0, mac: int = 0):
    lines = [
        "devices:",
        "  - name: box",
        "    local: true",
        f"    slot_max: {box}",
        "    harnesses: {claude: claude-bot, codex: codex-bot}",
    ]
    for name, slots in (("pc", pc), ("mac", mac)):
        if slots:
            lines += [
                f"  - name: {name}",
                f"    tailscale_name: {name}.example.ts.net",
                f"    slot_max: {slots}",
                "    harnesses: {claude: claude-bot, codex: codex-bot}",
                "    workdir_root: /home/{bot}/factory-work",
                f"    ssh_alias: {name}-{{bot}}",
                "    factory_root: /home/{bot}/factory",
            ]
    path = tmp_path / "machines.yml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config, findings = load_machines_yml(path)
    assert config is not None, findings
    return config


class FakeGitHub:
    """Fake GitHub: intake reads plus the writes the feeder makes."""

    def __init__(self) -> None:
        self.issues: dict[str, list[IntakeIssue]] = {}
        self.branches: dict[tuple[str, str], str] = {}
        self.branch_commits: dict[tuple[str, str], tuple[str, ...]] = {}
        self.commits: list[tuple[str, str, str]] = []
        self.pulls: dict[tuple[str, int], dict] = {}
        self.labels: dict[tuple[str, int], list[str]] = {}
        self.posted: dict[tuple[str, int], list[Comment]] = {}
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}
        self.statuses: dict[Ref, RefStatus] = {}
        self.status_reads: list[frozenset[Ref]] = []
        self.main_checks: dict[tuple[str, str], dict[str, BaseCheck]] = {}
        self.check_reads: list[tuple[str, str]] = []
        self.dirs: dict[tuple[str, str], tuple[str, ...]] = {}
        self.dir_reads: list[tuple[str, str, str]] = []
        self.trees: dict[str | tuple[str, str], tuple[tuple[str, ...], bool]] = {}
        self.tree_reads: list[tuple[str, str, str]] = []
        self.filed: dict[tuple[str, int], dict] = {}
        self.run_logs: dict[tuple[str, str], dict[str, str]] = {}
        self.landings: dict[tuple[str, int], ParentFacts] = {}
        self._next_pr = 500
        self._next_issue = 899
        self._next_comment = 9000
        self.clock = lambda: 0.0

    def _maybe_fail(self, step: str) -> None:
        left = self.fail.get(step, 0)
        if left:
            self.fail[step] = left - 1
            raise RuntimeError(f"fake GitHub failure in {step}")

    # intake reads
    def github_intake(self, repo: str, label: str) -> list[IntakeIssue]:
        self.calls.append(f"intake:{repo}:{label}")
        self._maybe_fail(f"intake:{repo}")
        return list(self.issues.get(repo, []))

    # writes
    def branch_sha(self, repo: str, branch: str) -> str | None:
        self._maybe_fail("branch_sha")
        if branch == "main" or branch == "dev":
            return BASE_SHA
        return self.branches.get((repo, branch))

    def create_start_commit(self, repo: str, base_sha: str, message: str) -> str:
        self._maybe_fail("commit")
        sha = f"{len(self.commits) + 1:040x}"
        self.commits.append((repo, base_sha, message))
        self.calls.append(f"commit:{repo}")
        return sha

    def create_branch(self, repo: str, branch: str, sha: str) -> None:
        self._maybe_fail("branch")
        assert (repo, branch) not in self.branches, "branch created twice"
        self.branches[(repo, branch)] = sha
        self.calls.append(f"branch:{repo}:{branch}")

    def find_pull(self, repo: str, branch: str) -> int | None:
        # Like GitHubIntakeClient.find_pull: the oldest open PR on the branch.
        self._maybe_fail("find_pull")
        for (pull_repo, number), pull in sorted(self.pulls.items()):
            if pull_repo == repo and pull["head"] == branch and pull.get("state", "open") == "open":
                return number
        return None

    def branch_pulls(self, repo: str, branch: str) -> tuple[int, ...]:
        self.calls.append(f"branch-pulls:{repo}:{branch}")
        return tuple(
            number for (pull_repo, number), pull in sorted(self.pulls.items())
            if pull_repo == repo and pull["head"] == branch
        )

    def pull_state(self, repo: str, number: int) -> str:
        pull = self.pulls[(repo, number)]
        if pull.get("merged"):
            return "merged"
        return pull.get("state", "open")

    def pull_base(self, repo: str, number: int) -> str:
        return self.pulls[(repo, number)]["base"]

    def pull_landing(self, repo: str, number: int) -> ParentFacts:
        self.calls.append(f"landing:{repo}:{number}")
        return self.landings[(repo, number)]

    def open_draft_pull(self, repo: str, *, branch: str, base: str, title: str, body: str) -> int:
        self._maybe_fail("pull")
        assert self.find_pull(repo, branch) is None, "second PR for one branch"
        self._next_pr += 1
        self.pulls[(repo, self._next_pr)] = {
            "head": branch, "base": base, "title": title, "body": body, "draft": True,
        }
        self.calls.append(f"pull:{repo}:{self._next_pr}")
        return self._next_pr

    def pull_body(self, repo: str, number: int) -> str:
        return self.pulls[(repo, number)].get("body", "")

    def set_pull_body(self, repo: str, number: int, body: str) -> None:
        self.pulls[(repo, number)]["body"] = body

    def add_labels(self, repo: str, number: int, labels) -> None:
        self._maybe_fail("labels")
        self.labels.setdefault((repo, number), [])
        for label in labels:
            if label not in self.labels[(repo, number)]:
                self.labels[(repo, number)].append(label)

    def comments(self, repo: str, number: int) -> list[Comment]:
        return list(self.posted.get((repo, number), []))

    def ahead_commits(self, repo: str, base: str, tip: str) -> tuple[str, ...]:
        self.calls.append(f"ahead:{repo}:{tip}")
        merged = set(self._commits_on(repo, base))
        return tuple(sha for sha in self._commits_on(repo, tip) if sha not in merged)

    def open_pull_commits(self, repo: str) -> dict[int, OpenPull]:
        return {
            number: OpenPull(
                commits=frozenset(self._commits_on(repo, pull["head"]))
                - frozenset(self._commits_on(repo, pull.get("base", "main"))),
                complete=True,
                base=pull.get("base", "main"),
                head=self.branches.get((repo, pull["head"]), pull["head"]),
                branch=pull["head"],
            )
            for (pull_repo, number), pull in self.pulls.items()
            if pull_repo == repo and pull.get("state", "open") == "open" and not pull.get("merged")
        }

    def pull_commits(self, repo: str, base: str, head: str) -> frozenset[str]:
        return frozenset(self._commits_on(repo, head)) - frozenset(self._commits_on(repo, base))

    def _commits_on(self, repo: str, branch: str) -> tuple[str, ...]:
        if (repo, branch) in self.branch_commits:
            return self.branch_commits[(repo, branch)]
        tips = [name for (tip_repo, name), tip in self.branches.items()
                if tip_repo == repo and tip == branch and (repo, name) in self.branch_commits]
        if tips:
            return self.branch_commits[(repo, tips[0])]
        sha = self.branches.get((repo, branch))
        return () if sha is None else (sha,)

    def ref_statuses(self, refs) -> dict[Ref, RefStatus]:
        self._maybe_fail("statuses")
        self.status_reads.append(frozenset(refs))
        return {ref: self.statuses.get(ref, RefStatus(Progress.MISSING)) for ref in refs}

    def base_checks(self, repo: str, branch: str) -> dict[str, BaseCheck]:
        self.check_reads.append((repo, branch))
        self._maybe_fail("base_checks")
        return dict(self.main_checks.get((repo, branch), {}))

    def tree_paths(self, repo: str, sha: str, path: str = "") -> tuple[tuple[str, ...], bool]:
        self.tree_reads.append((repo, sha, path))
        self._maybe_fail("tree_paths")
        return self.trees.get((repo, path) if path else repo, ((), False))

    def dir_names(self, repo: str, path: str, sha: str) -> tuple[str, ...]:
        self.dir_reads.append((repo, path, sha))
        self._maybe_fail("dir_names")
        return self.dirs.get((repo, path), ())

    def file_repo_issue(self, repo: str, title: str, body: str, labels, marker: str,
                        since: float, *, before_post=lambda: None) -> int:
        self._maybe_fail("file_issue")
        found = self.marked_issues(repo, marker, since)
        if found:
            return found[0]
        before_post()
        self._next_issue += 1
        number = self._next_issue
        self.filed[(repo, number)] = {"title": title, "body": body, "labels": tuple(labels),
                                      "state": "open"}
        if "factory:ready" in labels:
            self.issues.setdefault(repo, []).append(
                IntakeIssue(repo=repo, tracker="github", key=str(number), title=title, body=body,
                            labels=tuple(labels), created_at=since, applied_by="maxharris1",
                            url=f"https://github.com/{repo}/issues/{number}")
            )
        self._maybe_fail("file_issue_after")
        return number

    def marked_issues(self, repo: str, marker: str, since: float) -> tuple[int, ...]:
        return tuple(sorted(
            number for (filed_repo, number), item in self.filed.items()
            if filed_repo == repo and has_marker(item["body"], marker)
        ))

    def close_issue(
        self, repo: str, number: int, comment: str | None = None, *, completed: bool = False
    ) -> None:
        if comment is not None:
            self.post_comment(repo, number, comment)
        self._maybe_fail("close")
        self.calls.append(f"close:{repo}#{number}")
        self.filed[(repo, number)].update(
            state="closed", reason="completed" if completed else None, closed_at=self.clock()
        )
        self.issues[repo] = [item for item in self.issues.get(repo, []) if item.key != str(number)]

    def remove_issue_label(self, repo: str, number: int, label: str) -> None:
        self._maybe_fail("unlabel")
        self.calls.append(f"unlabel:{repo}#{number}:{label}")
        item = self.filed[(repo, number)]
        item["labels"] = tuple(name for name in item["labels"] if name != label)

    def _record(self, number: int, item: dict) -> IssueRecord:
        return IssueRecord(number=number, state=item["state"], reason=item.get("reason"),
                           body=item["body"])

    def closed_issues_since(self, repo: str, since: float) -> tuple[IssueRecord, ...]:
        self._maybe_fail("closed_issues")
        self.calls.append(f"closed-since:{repo}:{since}")
        return tuple(
            self._record(number, item) for (filed_repo, number), item in sorted(self.filed.items())
            if filed_repo == repo and item["state"] == "closed" and item.get("closed_at", 0.0) >= since
        )

    def split_children(self, repo: str, parent: int) -> tuple[IssueRecord, ...]:
        self._maybe_fail("split_children")
        self.calls.append(f"split-children:{repo}#{parent}")
        return tuple(
            self._record(number, item) for (filed_repo, number), item in sorted(self.filed.items())
            if filed_repo == repo and split_parent(item["body"]) == parent
        )

    def issue_record(self, repo: str, number: int) -> IssueRecord:
        self.calls.append(f"issue-record:{repo}#{number}")
        return self._record(number, self.filed[(repo, number)])

    def issue_state(self, repo: str, number: int) -> str:
        return self.filed[(repo, number)]["state"]

    def failed_run_logs(self, repo: str, run_id: str) -> dict[str, str]:
        self._maybe_fail("run_logs")
        return dict(self.run_logs.get((repo, run_id), {}))

    def post_issue_comment_once(self, repo: str, number: int, body: str, marker: str) -> str:
        for item in self.posted.get((repo, number), []):
            if has_marker(item.body, marker):
                return item.id
        return self.post_comment(repo, number, body)

    def post_comment(self, repo: str, number: int, body: str) -> str:
        self._maybe_fail("comment")
        self._next_comment += 1
        item = Comment(id=str(self._next_comment), author="maxharris1", body=body,
                       created_at=float(self._next_comment))
        self.posted.setdefault((repo, number), []).append(item)
        return item.id


class FakeLinear:
    def __init__(self) -> None:
        self.issues: list[IntakeIssue] = []
        self.posted: dict[str, list[Comment]] = {}
        self.queries: list[tuple[frozenset[str], str]] = []
        self.assignees: list[dict[str, frozenset[str]]] = []
        self._next = 0

    def linear_intake(
        self, project_ids: frozenset[str], state: str, assignees: Mapping[str, frozenset[str]]
    ) -> list[IntakeIssue]:
        self.queries.append((project_ids, state))
        self.assignees.append(dict(assignees))
        return list(self.issues)

    def linear_comments(self, issue_id: str) -> list[Comment]:
        return list(self.posted.get(issue_id, []))

    def post_linear_comment(self, issue_id: str, body: str) -> str:
        self._next += 1
        item = Comment(id=f"lin-c-{self._next}", author="max-linear-id", body=body,
                       created_at=float(self._next))
        self.posted.setdefault(issue_id, []).append(item)
        return item.id


# A minimal intake template (#204): fixtures append it unless a test opts out.
TEMPLATE = "\n\nRisk: low\nPlan first: no\nDepends on: none\n\n## Acceptance criteria\n\n- [ ] It works.\n"


def issue(number: int, *, labels=("factory:ready",), applied_by="maxharris1",
          created_at: float = 1000.0, body: str = "Fix the thing in lib/x.py.",
          title: str | None = None, repo: str = "owner/app", template: bool = True) -> IntakeIssue:
    return IntakeIssue(
        repo=repo,
        tracker="github",
        key=str(number),
        title=title or f"Issue {number}",
        body=body + TEMPLATE if template else body,
        labels=tuple(labels),
        created_at=created_at,
        applied_by=applied_by,
        url=f"https://github.com/{repo}/issues/{number}",
    )


def linear_issue(identifier: str = "AUT-7", *, applied_by="max-linear-id",
                 priority: int | None = 3, labels=(), created_at: float = 50.0) -> IntakeIssue:
    return IntakeIssue(
        repo="acme/linear-app",
        tracker="linear",
        key=identifier,
        title="Linear work",
        body="Touch src/a.ts only." + TEMPLATE,
        labels=tuple(labels),
        created_at=created_at,
        applied_by=applied_by,
        url=f"https://linear.app/acme/issue/{identifier.lower()}",
        linear_id=f"uuid-{identifier}",
        branch=f"max/{identifier.lower()}-linear-work",
        priority=priority,
    )


def context_for(text: str):
    parsed, findings = parse_factory_context(text)
    assert parsed is not None, findings
    return parsed


def make_feeder(tmp_path: Path, *, orch=GITHUB_ORCH, box=3, pc=0, mac=0,
                github: FakeGitHub | None = None, linear: FakeLinear | None = None,
                factory_text: str = FACTORY_MD, now: list[float] | None = None):
    clock = now if now is not None else [10_000.0]
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: clock[0])
    config = orch_config(tmp_path, orch)
    machine_config = machines(tmp_path, box=box, pc=pc, mac=mac)
    github = github or FakeGitHub()
    linear = linear or FakeLinear()
    feeder = Feeder(
        queue,
        config,
        machine_config,
        github=github,
        linear=linear,
        factory_for=lambda project: context_for(factory_text),
        head=lambda: None,
        log=lambda line: None,
    )
    return feeder, queue, github, linear


def rows(db: Path) -> dict[str, dict]:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        return {
            str(row["issue"]): dict(row)
            for row in connection.execute(f"SELECT * FROM {FEEDER_TABLE}")
        }


def headers(comments) -> list[dict[str, str]]:
    return [parse_comment(item.body).as_dict() for item in comments]


# --- config: the intake signal is per-project config ------------------------


def test_intake_is_per_project_config_with_the_label_default(tmp_path: Path) -> None:
    config = orch_config(tmp_path, GITHUB_ORCH.replace("      label: factory:ready\n", "      {}\n"))
    project = config.projects[0]
    assert project.intake is not None
    assert project.intake.label == "factory:ready"
    assert project.intake.linear_state is None


def test_linear_intake_names_the_state_and_trusted_users(tmp_path: Path) -> None:
    config = orch_config(tmp_path, LINEAR_ORCH)
    project = config.projects[0]
    assert project.intake is not None
    assert project.intake.linear_state == "In Progress"
    assert project.intake.label is None


@pytest.mark.parametrize(
    ("intake", "message"),
    [
        ("      linear_state: In Progress\n", "linear_state needs linear_project_id"),
        ("      label: ''\n", "label"),
        ("      colour: red\n", "unknown field"),
        ("      trusted: maxharris1\n", "trusted must be a list"),
    ],
)
def test_bad_github_intake_config_is_refused(tmp_path: Path, intake: str, message: str) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(GITHUB_ORCH.replace("      label: factory:ready\n", intake), encoding="utf-8")
    config, findings = load_orch_yml(path, allow_placeholders=True)
    assert config is None
    assert any(message in item.message for item in findings), findings


def test_a_linear_project_needs_its_intake_state(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(LINEAR_ORCH.replace("      linear_state: In Progress\n", "      label: x\n"),
                    encoding="utf-8")
    config, findings = load_orch_yml(path, allow_placeholders=True)
    assert config is None
    assert any("linear_state" in item.message for item in findings), findings


def test_shipped_orch_yml_configures_intake_per_project() -> None:
    root = Path(__file__).resolve().parents[1]
    config, findings = load_orch_yml(root / "orch.yml", allow_placeholders=True)
    assert config is not None, findings
    by_repo = {project.repo: project for project in config.projects}
    assert by_repo["maxharris1/factory"].intake.label == "factory:ready"
    assert by_repo["maxharris1/Cleffy"].intake.label == "factory:ready"
    assert by_repo["aut0-code/aut0"].intake.linear_state == "In Progress"
    assert by_repo["maxharris1/tracy-harris-coaching"].intake is None
    coo = by_repo["maxharris1/coo"]
    assert coo.intake.label == "factory:ready"
    assert coo.factory_ref == "main"
    assert coo.grok_bot == "grok-bot"


def test_feeder_code_hardcodes_no_project_or_signal() -> None:
    source = (Path(__file__).resolve().parents[1] / "lib" / "feeder.py").read_text(encoding="utf-8")
    for literal in ("maxharris1", "aut0", "Cleffy", "In Progress", "factory:ready"):
        assert literal not in source, literal


# --- A. intake: GitHub ------------------------------------------------------


def test_a_labelled_issue_from_a_trusted_login_opens_one_draft_pr_with_brief_and_go(
    tmp_path: Path,
) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(85, body="Change lib/feeder.py and its test.")]

    report = feeder.feed_once()

    assert [ref.ticket_id for ref in report.fed] == ["501"]
    pull = github.pulls[("owner/app", 501)]
    assert pull["draft"] is True
    assert pull["head"] == "factory/issue-85"
    assert pull["base"] == "main"
    assert pull["title"] == "#85: Issue 85"
    assert "Fixes #85" in pull["body"]
    assert github.commits == [("owner/app", BASE_SHA, github.commits[0][2])]
    assert "85" in github.commits[0][2]
    assert github.labels[("owner/app", 501)] == ["factory", "orch:state/building"]
    posted = github.posted[("owner/app", 501)]
    fields = headers(posted)
    assert [item["kind"] for item in fields] == ["brief", "go"]
    assert all(item["from"] == "grok-bot" and item["role"] == "orchestrator" for item in fields)
    assert fields[1]["state"] == "building"
    assert fields[1]["risk"] == "low"
    brief = posted[0].body
    assert "Change lib/feeder.py and its test." in brief
    assert "run the suite with `make test`" in brief  # factory.md prose
    item = rows(tmp_path / "orch.db")["85"]
    assert item["state"] == "fed"
    assert item["pr_number"] == "501"


def test_an_untrusted_label_is_ignored(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(1, applied_by="drive-by"), issue(2, applied_by=None)]
    report = feeder.feed_once()
    assert report.fed == ()
    assert github.pulls == {}
    assert rows(tmp_path / "orch.db") == {}


def test_intake_trusts_the_configured_logins_only(tmp_path: Path) -> None:
    orch = GITHUB_ORCH.replace("      label: factory:ready\n",
                               "      label: factory:ready\n      trusted: [teammate]\n")
    feeder, _, github, _ = make_feeder(tmp_path, orch=orch)
    github.issues["owner/app"] = [issue(1, applied_by="maxharris1"), issue(2, applied_by="teammate")]
    report = feeder.feed_once()
    assert [github.pulls[("owner/app", int(ref.ticket_id))]["head"] for ref in report.fed] == [
        "factory/issue-2"
    ]


def test_risk_high_label_and_more_than_three_files_route_to_planning(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    body = "Touch lib/a.py, lib/b.py, docs/SPEC.md and tests/test_a.py."
    github.issues["owner/app"] = [issue(9, labels=("factory:ready", "risk:high"), body=body)]
    feeder.feed_once()
    fields = headers(github.posted[("owner/app", 501)])
    assert fields[1]["state"] == "planning"
    assert fields[1]["risk"] == "high"
    assert fields[0]["risk"] == "high"
    assert github.labels[("owner/app", 501)] == ["factory", "orch:state/planning"]


@pytest.mark.parametrize(
    ("body", "state"),
    [
        ("Fix lib/a.py.", "building"),
        ("Touch lib/a.py, lib/b.py and lib/c.py.", "building"),
        ("Touch lib/a.py, lib/b.py, lib/c.py and README.md.", "planning"),
        ("Mentions lib/a.py twice: lib/a.py, lib/a.py.", "building"),
        ("", "building"),
    ],
)
def test_go_state_counts_distinct_files_the_issue_names(body: str, state: str) -> None:
    assert go_state_for(body) == state


# --- A. intake: Linear ------------------------------------------------------


def test_a_linear_issue_moved_to_the_intake_state_by_a_trusted_user_is_fed(
    tmp_path: Path,
) -> None:
    linear = FakeLinear()
    linear.issues = [linear_issue("AUT-7")]
    feeder, _, github, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, linear=linear,
                                       factory_text=LINEAR_FACTORY_MD)

    report = feeder.feed_once()

    assert linear.queries == [(frozenset({"proj-1"}), "In Progress")]
    assert report.fed == (TicketRef("acme/linear-app", "AUT-7", "linear"),)
    pull = github.pulls[("acme/linear-app", 501)]
    assert pull["head"] == "max/aut-7-linear-work"  # Linear's own branch name
    assert pull["base"] == "dev"
    assert pull["title"] == "AUT-7: Linear work"
    assert "Fixes AUT-7" in pull["body"]
    # tickets: linear: no GitHub labels, and brief and go stay on Linear.
    assert ("acme/linear-app", 501) not in github.labels
    assert ("acme/linear-app", 501) not in github.posted
    fields = headers(linear.posted["uuid-AUT-7"])
    assert [item["kind"] for item in fields] == ["brief", "go"]
    assert linear.posted["uuid-AUT-7"][0].body.startswith("`orch:v1 ")  # visible header form


def test_a_linear_move_by_an_untrusted_user_is_ignored(tmp_path: Path) -> None:
    linear = FakeLinear()
    linear.issues = [linear_issue("AUT-8", applied_by="someone-else")]
    feeder, _, github, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, linear=linear,
                                       factory_text=LINEAR_FACTORY_MD)
    assert feeder.feed_once().fed == ()
    assert github.pulls == {} and linear.posted == {}


def test_an_issue_with_no_applied_by_is_logged_and_skipped(tmp_path: Path) -> None:
    linear = FakeLinear()
    linear.issues = [linear_issue("AUT-2539", applied_by=None)]
    feeder, _, github, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, linear=linear,
                                       factory_text=LINEAR_FACTORY_MD)
    lines: list[str] = []
    feeder.log = lines.append

    assert feeder.feed_once().fed == ()
    assert github.pulls == {} and linear.posted == {}
    assert [line for line in lines if "AUT-2539" in line] == [
        "feeder: acme/linear-app#AUT-2539 skipped: no applied_by"
    ]


def test_started_ticket_back_in_intake_state_is_not_fed(tmp_path: Path) -> None:
    linear = FakeLinear()
    linear.issues = [linear_issue("AUT-2282"), linear_issue("AUT-9")]
    feeder, queue, github, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, linear=linear,
                                           factory_text=LINEAR_FACTORY_MD)
    dispatcher_for(tmp_path, queue)
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.executemany(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state, gate_count, stuck) "
            "VALUES ('acme/linear-app', ?, ?, 0, 0)",
            [("AUT-2282", "building"), ("AUT-9", "questions")],
        )

    report = feeder.feed_once()

    assert report.fed == (TicketRef("acme/linear-app", "AUT-9", "linear"),)
    assert "AUT-2282" not in rows(tmp_path / "orch.db")
    assert "uuid-AUT-2282" not in linear.posted


# --- A. idempotency: one PR per issue, keyed on the issue number -------------


def test_a_repoll_never_opens_a_second_pr(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(85)]
    first = feeder.feed_once()
    second = feeder.feed_once()
    third = feeder.feed_once()
    assert len(first.fed) == 1 and second.fed == () and third.fed == ()
    assert len(github.pulls) == 1
    assert len(github.posted[("owner/app", 501)]) == 2
    assert len(github.commits) == 1


def test_a_feed_that_failed_midway_resumes_without_duplicates(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(85)]
    github.fail["comment"] = 2  # brief fails, then retried brief fails again
    first = feeder.feed_once()
    assert first.fed == ()
    assert rows(tmp_path / "orch.db")["85"]["state"] != "fed"
    second = feeder.feed_once()
    assert second.fed == ()
    third = feeder.feed_once()
    assert [ref.ticket_id for ref in third.fed] == ["501"]
    assert len(github.pulls) == 1 and len(github.commits) == 1
    assert [item["kind"] for item in headers(github.posted[("owner/app", 501)])] == ["brief", "go"]


def test_a_pr_found_on_the_branch_is_reused_after_a_lost_write(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(85)]
    # A crash after GitHub created the branch and PR but before orch.db recorded them.
    github.branches[("owner/app", "factory/issue-85")] = "f" * 40
    github.pulls[("owner/app", 77)] = {"head": "factory/issue-85", "base": "main",
                                       "title": "t", "body": "Fixes #85", "draft": True}
    report = feeder.feed_once()
    assert [ref.ticket_id for ref in report.fed] == ["77"]
    assert github.commits == []
    assert len(github.pulls) == 1


def test_one_pr_per_issue_even_with_two_feeders_on_one_database(tmp_path: Path) -> None:
    feeder, queue, github, linear = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(85)]
    other = Feeder(queue, feeder.config, feeder.machines, github=github, linear=linear,
                   factory_for=lambda project: context_for(FACTORY_MD), head=lambda: None,
                   log=lambda line: None)
    feeder.feed_once()
    other.feed_once()
    assert len(github.pulls) == 1


# --- B. capacity cap ---------------------------------------------------------


def test_feeding_is_capped_at_the_harness_free_slots(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=2, pc=1)
    github.issues["owner/app"] = [issue(n, created_at=float(n)) for n in range(1, 6)]
    report = feeder.feed_once()
    assert len(report.fed) == 3  # 2 box + 1 pc slots free
    states = {key: row["state"] for key, row in rows(tmp_path / "orch.db").items()}
    assert sorted(key for key, value in states.items() if value == "fed") == ["1", "2", "3"]
    assert sorted(key for key, value in states.items() if value == "waiting-capacity") == ["4", "5"]
    # Fed items with no job yet still count, so the next poll feeds nothing.
    assert feeder.feed_once().fed == ()


def test_a_remote_checkout_behind_the_box_head_adds_no_room(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=2, pc=1)
    feeder.head = lambda: "b" * 40
    with queue.connect() as connection:
        device_sync.upsert(
            connection, device="pc", bot="claude-bot", harness="claude",
            checkout="/home/claude-bot/factory", target="a" * 40, sha="a" * 40,
            state=device_sync.SYNCED, detail="", now=1.0,
        )
        connection.commit()
    github.issues["owner/app"] = [issue(n, created_at=float(n)) for n in range(1, 6)]
    assert len(feeder.feed_once().fed) == 2


def test_queued_jobs_for_the_harness_use_up_capacity(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=3)
    for key in ("q1", "q2"):
        queue.enqueue(JobRequest(repo="other/repo", ticket_id=key, action="build",
                                 recipient="claude", source_comment_id=key,
                                 timeout_seconds=600), dedupe_key=key)
    github.issues["owner/app"] = [issue(1), issue(2)]
    assert len(feeder.feed_once().fed) == 1


def test_held_slots_and_disabled_devices_give_no_capacity(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    queue.enqueue(JobRequest(repo="other/repo", ticket_id="r", action="build",
                             recipient="claude", source_comment_id="r", timeout_seconds=600),
                  dedupe_key="r")
    DevicePool(queue, feeder.machines).claim("claude")  # the box's one slot is held
    github.issues["owner/app"] = [issue(1)]
    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db")["1"]["state"] == "waiting-capacity"


def test_an_enable_the_poller_adopts_gives_capacity_without_a_feeder_restart(
    tmp_path: Path,
) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    path = tmp_path / "machines.yml"
    enabled = path.read_text(encoding="utf-8")
    path.write_text(enabled.replace("    local: true\n", "    local: true\n    enabled: false\n"),
                    encoding="utf-8")
    feeder.machines = config = load_machines_yml(path)[0]
    monitor = DeviceMonitor(DevicePool(queue, config, machines_path=path), config,
                            SignalTransport(), machines_path=path)
    monitor.reload()
    github.issues["owner/app"] = [issue(85)]
    assert feeder.feed_once().fed == ()

    path.write_text(enabled, encoding="utf-8")
    monitor.reload()

    assert len(feeder.feed_once().fed) == 1


def test_waiting_items_are_fed_once_a_slot_frees(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    github.issues["owner/app"] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    assert [r.ticket_id for r in feeder.feed_once().fed] == ["501"]
    # Item 1's job is queued, claimed and finished; its slot is released.
    job = queue.enqueue(JobRequest(repo="owner/app", ticket_id="501", action="build",
                                   recipient="claude", source_comment_id="go",
                                   timeout_seconds=600), dedupe_key="t1")
    pool = DevicePool(queue, feeder.machines)
    claimed = pool.claim("claude")
    assert claimed is not None and claimed[0].id == job.id
    assert feeder.feed_once().fed == ()
    queue.finish(job.id, owner="claude", handoff_comment_id="h")
    pool.release(job.id, owner="claude")
    assert [r.ticket_id for r in feeder.feed_once().fed] == ["502"]


def test_a_fed_ticket_whose_job_never_comes_stops_holding_room(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, box=1, now=now)
    github.issues["owner/app"] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    assert len(feeder.feed_once().fed) == 1
    now[0] += 60
    assert feeder.feed_once().fed == ()  # issue 1's job may still be on its way
    now[0] += 600
    assert len(feeder.feed_once().fed) == 1  # no job after 10 minutes: room is back


def test_a_ticket_that_lost_its_label_mid_feed_holds_no_room(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, box=1, now=now)
    github.issues["owner/app"] = [issue(1, created_at=1.0)]
    github.fail["pull"] = 1
    assert feeder.feed_once().fed == ()  # stuck in error after the branch
    github.issues["owner/app"] = [issue(2, created_at=2.0)]  # 1 lost its label
    now[0] += 60
    assert [r.ticket_id for r in feeder.feed_once().fed] == ["501"]
    assert github.pulls[("owner/app", 501)]["head"] == "factory/issue-2"


# --- B. priority: urgent, then bug, then oldest ------------------------------


def test_feed_order_is_urgent_then_bug_then_oldest(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=1)
    github.issues["owner/app"] = [
        issue(1, created_at=1.0),
        issue(2, labels=("factory:ready", "bug"), created_at=5.0),
        issue(3, labels=("factory:ready", "urgent"), created_at=9.0),
        issue(4, created_at=0.5),
    ]
    order = []
    for _ in range(4):
        report = feeder.feed_once()
        for ref in report.fed:
            order.append(github.pulls[("owner/app", int(ref.ticket_id))]["head"])
        with sqlite3.connect(tmp_path / "orch.db") as connection:
            connection.execute(
                f"UPDATE {FEEDER_TABLE} SET state = 'done' WHERE state = 'fed'"
            )
    assert order == ["factory/issue-3", "factory/issue-2", "factory/issue-4", "factory/issue-1"]


def test_linear_urgent_priority_counts_as_urgent(tmp_path: Path) -> None:
    linear = FakeLinear()
    linear.issues = [
        linear_issue("AUT-1", priority=3, created_at=1.0),
        linear_issue("AUT-2", priority=1, created_at=9.0),
    ]
    feeder, _, _, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, linear=linear, box=1,
                                  factory_text=LINEAR_FACTORY_MD)
    assert [ref.ticket_id for ref in feeder.feed_once().fed] == ["AUT-2"]


# --- feeder state for orch status and the dashboard -------------------------


def dispatcher_for(tmp_path: Path, queue: SQLiteJobQueue, feeder=None) -> Dispatcher:
    return Dispatcher(
        tmp_path / "orch.db",
        queue,
        trusted_logins={"github": {"maxharris1": {"grok-bot", "claude-bot", "codex-bot"}},
                        "linear": {}},
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        feeder=feeder,
    )


def test_feeder_counts_eligible_fed_and_waiting(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=1)
    github.issues["owner/app"] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    feeder.feed_once()
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        counts = feeder_counts(connection)
    assert counts == {"eligible": 2, "fed": 1, "waiting_capacity": 1, "waiting_blocked": 0}


def test_orch_status_shows_feeder_state(tmp_path: Path) -> None:
    from lib.status import SQLiteStatusReader, build_status, render

    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    dispatcher_for(tmp_path, queue)  # the poller's tables, as on a live database
    github.issues["owner/app"] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    feeder.feed_once()
    reader = SQLiteStatusReader(tmp_path / "orch.db")
    text = render(build_status(reader, reader, clock=lambda: 20_000.0,
                               feeder=reader.feeder(), attention=reader.attention()))
    assert "Feeder — eligible 2, fed 1, waiting for capacity 1" in text
    assert "owner/app#2 waiting for capacity" in text


def test_dashboard_snapshot_shows_feeder_state(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot, render_pages

    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    dispatcher_for(tmp_path, queue)
    github.issues["owner/app"] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    feeder.feed_once()
    snapshot = collect_snapshot(tmp_path / "orch.db", tmp_path / "none.log", now=20_000.0)
    assert snapshot["feeder"]["eligible"] == 2
    assert snapshot["feeder"]["fed"] == 1
    assert snapshot["feeder"]["waiting_capacity"] == 1
    assert {(item["issue"], item["state"]) for item in snapshot["feeder"]["items"]} == {
        ("1", "fed"), ("2", "waiting-capacity"),
    }
    pages = render_pages(snapshot)
    assert pages["index.html"].count('id="feeder"') == 1


# --- the poller: fed issues become queued jobs within one poll --------------


class FeederSource:
    """A poll source whose fresh_ticket reads the fake GitHub's PRs."""

    def __init__(self, github: FakeGitHub):
        self.github_fake = github

    def github_candidates(self, label_prefix: str, *, exclude=frozenset()):
        return ()

    def linear_candidates(self, query: str, *, exclude, reserve):
        return ()

    def _ticket(self, ref: TicketRef) -> TrackedTicket | None:
        number = int(ref.ticket_id)
        if (ref.repo, number) not in self.github_fake.pulls:
            return None
        return TrackedTicket(
            repo=ref.repo,
            ticket_id=ref.ticket_id,
            factory=factory(),
            comments=tuple(self.github_fake.comments(ref.repo, number)),
            current_head="b" * 40,
            ci={},
            risk="low",
            pr_number=ref.ticket_id,
        )

    def fresh_ticket(self, ref: TicketRef):
        return self._ticket(ref)

    def tracked_tickets(self, refs):
        return tuple(ticket for ref in refs if (ticket := self._ticket(ref)) is not None)


def test_n_eligible_issues_become_n_queued_jobs_within_one_poll_capped_at_free_slots(
    tmp_path: Path,
) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=2, pc=1)
    github.issues["owner/app"] = [issue(n, created_at=float(n)) for n in range(1, 6)]
    dispatcher = Dispatcher(
        tmp_path / "orch.db",
        queue,
        trusted_logins={"github": {"maxharris1": {"grok-bot", "claude-bot", "codex-bot"}},
                        "linear": {}},
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        feeder=feeder,
    )
    source = FeederSource(github)

    dispatcher.poll_once(source)

    queued = [job for job in queue.outstanding() if job.request.recipient == "claude"]
    assert sorted(job.request.ticket_id for job in queued) == ["501", "502", "503"]
    assert {job.request.action for job in queued} == {"build"}
    assert len(github.pulls) == 3

    dispatcher.poll_once(source)
    assert len(github.pulls) == 3  # still one PR per issue, and still capped
    assert len([job for job in queue.outstanding() if job.request.recipient == "claude"]) == 3


def test_a_feeder_error_never_stops_the_poll(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)

    def broken():
        raise RuntimeError("boom")

    feeder.feed_once = broken  # type: ignore[method-assign]
    dispatcher = Dispatcher(
        tmp_path / "orch.db",
        queue,
        trusted_logins={"github": {"maxharris1": {"grok-bot"}}, "linear": {}},
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        feeder=feeder,
    )
    dispatcher.poll_once(FeederSource(github))
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        errors = connection.execute("SELECT repo, error FROM poll_errors").fetchall()
    assert ("feeder", "RuntimeError") in errors


# --- #87 review F1.3, F1.6: recovery reuses only what is really ours -----------


def forged(kind: str, author: str, comment_id: str, *, visible: bool = False) -> Comment:
    fields = [("kind", kind), ("from", "grok-bot"), ("role", "orchestrator"), ("risk", "low")]
    if kind == "go":
        fields.append(("state", "building"))
    body = format_comment(Handoff(fields=tuple(fields), body=f"forged {kind}"), visible=visible)
    return Comment(id=comment_id, author=author, body=body, created_at=1.0)


def test_a_forged_go_on_the_pr_does_not_suppress_the_real_one(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(85)]
    # A crash after GitHub opened the PR; then an untrusted commenter posts a
    # parseable orchestrator brief and go on it.
    github.branches[("owner/app", "factory/issue-85")] = "f" * 40
    github.pulls[("owner/app", 77)] = {"head": "factory/issue-85", "base": "main",
                                       "title": "t", "body": "Fixes #85", "draft": True}
    github.posted[("owner/app", 77)] = [forged("brief", "drive-by", "1"),
                                        forged("go", "drive-by", "2")]

    report = feeder.feed_once()

    assert [ref.ticket_id for ref in report.fed] == ["77"]
    ours = [item for item in github.posted[("owner/app", 77)] if item.author == "maxharris1"]
    assert [fields["kind"] for fields in headers(ours)] == ["brief", "go"]
    item = rows(tmp_path / "orch.db")["85"]
    assert item["brief_comment_id"] == ours[0].id
    assert item["go_comment_id"] == ours[1].id


def test_a_trusted_go_already_on_the_pr_is_reused(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(85)]
    github.branches[("owner/app", "factory/issue-85")] = "f" * 40
    github.pulls[("owner/app", 77)] = {"head": "factory/issue-85", "base": "main",
                                       "title": "t", "body": "Fixes #85", "draft": True}
    github.posted[("owner/app", 77)] = [forged("brief", "maxharris1", "1"),
                                        forged("go", "maxharris1", "2")]

    feeder.feed_once()

    assert len(github.posted[("owner/app", 77)]) == 2  # nothing posted twice
    item = rows(tmp_path / "orch.db")["85"]
    assert (item["brief_comment_id"], item["go_comment_id"]) == ("1", "2")


def test_a_forged_go_on_a_linear_issue_does_not_suppress_the_real_one(tmp_path: Path) -> None:
    linear = FakeLinear()
    linear.issues = [linear_issue("AUT-7")]
    linear.posted["uuid-AUT-7"] = [forged("brief", "intruder-id", "lin-x", visible=True),
                                   forged("go", "intruder-id", "lin-y", visible=True)]
    feeder, _, _, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, linear=linear,
                                  factory_text=LINEAR_FACTORY_MD)

    feeder.feed_once()

    ours = [item for item in linear.posted["uuid-AUT-7"] if item.author == "max-linear-id"]
    assert [fields["kind"] for fields in headers(ours)] == ["brief", "go"]
    assert rows(tmp_path / "orch.db")["AUT-7"]["go_comment_id"] == ours[1].id


def test_recovery_reuses_the_open_pr_not_an_older_merged_one(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues["owner/app"] = [issue(85)]
    github.branches[("owner/app", "factory/issue-85")] = "f" * 40
    github.pulls[("owner/app", 12)] = {"head": "factory/issue-85", "base": "main", "title": "old",
                                       "body": "", "draft": False, "state": "closed"}
    github.pulls[("owner/app", 90)] = {"head": "factory/issue-85", "base": "main", "title": "t",
                                       "body": "Fixes #85", "draft": True}

    report = feeder.feed_once()

    assert [ref.ticket_id for ref in report.fed] == ["90"]
    assert ("owner/app", 12) not in github.posted
    assert [fields["kind"] for fields in headers(github.posted[("owner/app", 90)])] == ["brief", "go"]


def test_a_running_job_with_no_placement_uses_up_box_capacity(tmp_path: Path) -> None:
    """Issue #90: a claim made with no device still occupies a box slot."""
    feeder, queue, github, _ = make_feeder(tmp_path, box=3)
    queue.enqueue(JobRequest(repo="other/repo", ticket_id="old", action="build",
                             recipient="claude", source_comment_id="old",
                             timeout_seconds=600), dedupe_key="old")
    queue.claim("claude")  # an older `orch wait`, with no device placement
    github.issues["owner/app"] = [issue(n, created_at=float(n)) for n in range(1, 4)]
    assert len(feeder.feed_once().fed) == 2


# --- #87 review r2: recovery keeps every bound new intake has -----------------


def claude_backlog(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    return sorted(
        (job.request.repo, job.request.ticket_id)
        for job in queue.outstanding()
        if job.request.recipient == "claude"
    )


def test_a_resumed_feed_waits_when_other_work_took_the_capacity(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    github.issues["owner/app"] = [issue(501)]
    github.fail["pull"] = 1
    assert feeder.feed_once().fed == ()  # stuck in error after the branch
    queue.enqueue(JobRequest(repo="other/app", ticket_id="88", action="build",
                             recipient="claude", source_comment_id="x",
                             timeout_seconds=600), dedupe_key="other")

    dispatcher_for(tmp_path, queue, feeder=feeder).poll_once(FeederSource(github))

    assert claude_backlog(queue) == [("other/app", "88")]
    assert github.pulls == {}
    assert rows(tmp_path / "orch.db")["501"]["state"] == "error"


def test_a_resumed_feed_is_fed_first_once_there_is_room(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    github.issues["owner/app"] = [issue(1, created_at=1.0)]
    github.fail["pull"] = 1
    feeder.feed_once()
    github.issues["owner/app"] = [issue(1, created_at=1.0),
                                  issue(2, labels=("factory:ready", "urgent"), created_at=0.0)]
    report = feeder.feed_once()
    assert [ref.ticket_id for ref in report.fed] == ["501"]
    assert github.pulls[("owner/app", 501)]["head"] == "factory/issue-1"
    assert rows(tmp_path / "orch.db")["2"]["state"] == "waiting-capacity"


def test_restoring_a_lost_signal_never_feeds_past_capacity(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, box=1, now=now)
    github.issues["owner/app"] = [issue(1, created_at=1.0)]
    github.fail["pull"] = 1
    feeder.feed_once()
    github.issues["owner/app"] = [issue(2, created_at=2.0)]
    now[0] += 60
    assert len(feeder.feed_once().fed) == 1
    github.issues["owner/app"] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    now[0] += 60
    assert feeder.feed_once().fed == ()
    assert len(github.pulls) == 1


@pytest.mark.parametrize("ended", [{"state": "closed"}, {"state": "closed", "merged": True}])
@pytest.mark.parametrize("failing", ["labels", "comment"])
def test_a_saved_pr_that_is_no_longer_open_gets_no_new_work(
    tmp_path: Path, ended: dict, failing: str
) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    github.issues["owner/app"] = [issue(1)]
    github.fail[failing] = 1
    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db")["1"]["pr_number"] == "501"
    posted = list(github.posted.get(("owner/app", 501), []))
    github.pulls[("owner/app", 501)].update(ended)

    dispatcher = dispatcher_for(tmp_path, queue, feeder=feeder)
    dispatcher.poll_once(FeederSource(github))
    dispatcher.poll_once(FeederSource(github))

    assert github.posted.get(("owner/app", 501), []) == posted
    assert list(github.pulls) == [("owner/app", 501)]
    assert claude_backlog(queue) == []
    assert rows(tmp_path / "orch.db")["1"]["state"] == "closed"


def test_a_closed_feed_holds_no_room(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=1)
    github.issues["owner/app"] = [issue(1, created_at=1.0)]
    github.fail["labels"] = 1
    feeder.feed_once()
    github.pulls[("owner/app", 501)]["state"] = "closed"
    github.issues["owner/app"] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    assert [ref.ticket_id for ref in feeder.feed_once().fed] == ["502"]


@pytest.mark.parametrize(
    ("labels", "urgent", "bug"),
    [(("factory:ready", "urgent"), 1, 0), (("factory:ready", "bug"), 0, 1)],
)
def test_a_priority_change_after_feeding_reaches_the_claim_order(
    tmp_path: Path, labels: tuple[str, ...], urgent: int, bug: int
) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, box=2, now=now)
    github.issues["owner/app"] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    feeder.feed_once()
    for key in ("501", "502"):
        now[0] += 1
        queue.enqueue(JobRequest(repo="owner/app", ticket_id=key, action="build",
                                 recipient="claude", source_comment_id=key,
                                 timeout_seconds=600), dedupe_key=key)
    github.issues["owner/app"] = [issue(1, created_at=1.0),
                                  issue(2, labels=labels, created_at=2.0)]
    now[0] += 1
    feeder.feed_once()
    with queue.connect() as connection:
        priority = {
            row[0]: (row[1], row[2])
            for row in connection.execute("SELECT ticket_id, urgent, bug FROM ticket_priority")
        }
    assert priority == {"501": (0, 0), "502": (urgent, bug)}
    claimed = DevicePool(queue, feeder.machines).claim("claude")
    assert claimed is not None and claimed[0].request.ticket_id == "502"


def test_an_empty_intake_poll_shows_zero_eligible(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, now=now)
    github.issues["owner/app"] = [issue(1)]
    feeder.feed_once()
    github.issues["owner/app"] = []
    now[0] += 60
    assert feeder.feed_once().eligible == 0
    with queue.connect() as connection:
        assert feeder_counts(connection) == {
            "eligible": 0, "fed": 0, "waiting_capacity": 0, "waiting_blocked": 0
        }
        assert feeder_items(connection) == []


def test_status_and_dashboard_show_an_empty_latest_poll(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from lib.status import SQLiteStatusReader, build_status, render

    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, now=now)
    dispatcher_for(tmp_path, queue)
    github.issues["owner/app"] = [issue(1)]
    feeder.feed_once()
    github.issues["owner/app"] = []
    now[0] += 60
    feeder.feed_once()

    reader = SQLiteStatusReader(tmp_path / "orch.db")
    text = render(build_status(reader, reader, clock=lambda: 20_000.0,
                               feeder=reader.feeder(), attention=reader.attention()))
    assert "Feeder — eligible 0, fed 0, waiting for capacity 0" in text
    assert "owner/app#1" not in text
    snapshot = collect_snapshot(tmp_path / "orch.db", tmp_path / "none.log", now=20_000.0)
    assert snapshot["feeder"] == {
        "eligible": 0, "fed": 0, "waiting_capacity": 0, "waiting_blocked": 0, "items": []
    }


def _review(queue: SQLiteJobQueue, recipient: str) -> None:
    queue.enqueue(JobRequest(repo="owner/app", ticket_id="1", action="review", recipient=recipient,
                             source_comment_id="1", timeout_seconds=600), dedupe_key="review")


def test_a_queued_review_on_a_device_the_builder_shares_takes_its_room(tmp_path: Path) -> None:
    feeder, queue, _, _ = make_feeder(tmp_path, box=2)
    _review(queue, "codex")
    assert feeder._room({"claude"}, 10_000.0) == {"claude": 1}


def test_a_queued_review_only_another_device_can_run_leaves_the_builder_room(tmp_path: Path) -> None:
    feeder, queue, _, _ = make_feeder(tmp_path)
    path = tmp_path / "split.yml"
    path.write_text(
        "devices:\n"
        "  - name: box\n    local: true\n    slot_max: 2\n    harnesses: {claude: claude-bot}\n"
        "  - name: pc\n    tailscale_name: pc.example.ts.net\n    slot_max: 1\n"
        "    harnesses: {codex: codex-bot}\n    workdir_root: /home/{bot}/factory-work\n"
        "    ssh_alias: pc-{bot}\n    factory_root: /home/{bot}/factory\n",
        encoding="utf-8",
    )
    feeder.machines, findings = load_machines_yml(path)
    assert feeder.machines is not None, findings
    _review(queue, "codex")
    assert feeder._room({"claude", "codex"}, 10_000.0) == {"claude": 2, "codex": 0}


RED_SHA = "c0ffee12" + "0" * 32
MAIN = ("owner/app", "main")
RED_LOG = "\n".join(
    ["collected 3 items", *(f"step {i}" for i in range(300)), "FAILED tests/test_x.py::test_y - boom"]
)


def red_main(github: FakeGitHub, head: str = RED_SHA, run_id: str = "900") -> None:
    github.main_checks[MAIN] = {"test (2/3)": BaseCheck("test (2/3)", "failure", run_id, "1", head)}
    github.run_logs[("owner/app", run_id)] = {"test (2/3)": RED_LOG}


def main_fix_rows(db: Path) -> list[tuple[str, int | None]]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT state, issue FROM feeder_main_fix ORDER BY filed_at"
        ).fetchall()


def main_fixes(github: FakeGitHub) -> list[int]:
    return sorted(number for _, number in github.filed)


def test_a_red_main_files_one_main_fix_that_feeds_as_one_build_job(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    github.issues["owner/app"] = [issue(1), issue(2)]
    red_main(github)
    dispatcher = dispatcher_for(tmp_path, queue, feeder)
    source = FeederSource(github)

    dispatcher.poll_once(source)

    assert main_fixes(github) == [900]
    assert github.filed[MAIN[0], 900]["labels"] == ("factory:ready", "factory:main-fix")
    jobs = [job for job in queue.outstanding() if job.request.recipient == "claude"]
    assert [(job.request.ticket_id, job.request.action) for job in jobs] == [("501", "build")]
    assert github.pulls[MAIN[0], 501]["head"] == "factory/issue-900"
    assert {"factory:main-fix", "orch:state/building"} <= set(github.labels[MAIN[0], 501])
    brief, go = github.posted[MAIN[0], 501]
    assert "FAILED tests/test_x.py::test_y - boom" in brief.body
    assert parse_comment(go.body).as_dict()["state"] == "building"
    assert [rows(tmp_path / "orch.db")[key]["state"] for key in ("1", "2")] == ["waiting-blocked"] * 2

    dispatcher.poll_once(source)

    assert main_fixes(github) == [900]
    assert len([job for job in queue.outstanding() if job.request.recipient == "claude"]) == 1


def test_a_closed_main_fix_with_main_still_red_files_one_new_issue(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    red_main(github)
    feeder.feed_once()
    github.close_issue("owner/app", 900)
    for _ in range(2):
        now[0] += 60
        feeder.feed_once()

    assert main_fixes(github) == [900, 901]
    first, second = (github.filed[MAIN[0], n]["body"].splitlines()[0] for n in (900, 901))
    assert first != second
    assert main_fix_rows(tmp_path / "orch.db") == [("closed", 900), ("open", 901)]


@pytest.mark.parametrize("step", ["file_issue", "file_issue_after"])
def test_a_crash_around_the_post_leaves_one_issue_after_the_next_poll(
    tmp_path: Path, step: str
) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    red_main(github)
    github.fail[step] = 1
    assert feeder.feed_once().errors == 1
    assert main_fix_rows(tmp_path / "orch.db") == [("filing", None)]
    now[0] += 60

    assert feeder.feed_once().errors == 0

    assert main_fixes(github) == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def test_two_feeders_on_one_database_file_one_issue(tmp_path: Path) -> None:
    feeder, queue, github, linear = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    other = Feeder(queue, feeder.config, feeder.machines, github=github, linear=linear,
                   factory_for=feeder.factory_for, head=lambda: None)
    red_main(github)
    post = github.file_repo_issue

    def racing(*args, **kwargs):
        github.file_repo_issue = post
        other.feed_once()
        return post(*args, **kwargs)

    github.file_repo_issue = racing
    feeder.feed_once()

    assert main_fixes(github) == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]
    assert len(github.pulls) == 1


def test_one_base_has_one_active_main_fix_row(tmp_path: Path) -> None:
    insert = (
        "INSERT INTO feeder_main_fix "
        "(repo, base, marker, state, issue, last_head, filed_at, title, body) "
        "VALUES ('owner/app', ?, ?, ?, NULL, 'h', 1, 't', 'b')"
    )
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        ensure_tables(connection)
        connection.execute(insert, ("main", "a", "open"))
        connection.execute(insert, ("main", "b", "closed"))
        connection.execute(insert, ("dev", "c", "filing"))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, ("main", "d", "filing"))


def test_a_failed_main_read_files_and_retires_nothing_and_green_ends_the_streak(
    tmp_path: Path,
) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    red_main(github)

    def poll(*, fail: bool = False) -> int:
        now[0] += 60
        github.fail["base_checks"] = int(fail)
        return feeder.feed_once().errors

    assert poll(fail=True) == 1
    assert main_fixes(github) == []
    poll()
    assert poll(fail=True) == 1
    poll()
    assert main_fixes(github) == [900]
    github.main_checks[MAIN] = {"test (2/3)": BaseCheck("test (2/3)", "success", "950", "1", "d" * 40)}
    poll()
    assert main_fix_rows(tmp_path / "orch.db") == [("closed", 900)]
    red_main(github, head="e" * 40, run_id="960")
    poll()
    assert main_fixes(github) == [900, 901]


UNTRUSTED_ORCH = GITHUB_ORCH.replace(
    "      label: factory:ready\n", "      label: factory:ready\n      trusted: [teammate]\n"
)


def green_main(github: FakeGitHub) -> None:
    github.main_checks[MAIN] = {"test (2/3)": BaseCheck("test (2/3)", "success", "950", "1", "d" * 40)}


def unfed_main_fix(tmp_path: Path, **kwargs):
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(
        tmp_path, orch=UNTRUSTED_ORCH, factory_text=REQUIRED_MD, now=now, **kwargs
    )

    def poll() -> int:
        now[0] += 60
        return feeder.feed_once().errors

    red_main(github)
    assert poll() == 0
    assert main_fixes(github) == [900]
    assert "900" not in rows(tmp_path / "orch.db")
    green_main(github)
    return github, poll


def test_green_main_closes_a_never_fed_main_fix_with_one_comment(tmp_path: Path) -> None:
    github, poll = unfed_main_fix(tmp_path)

    assert poll() == 0
    assert poll() == 0

    assert github.filed[MAIN[0], 900]["state"] == "closed"
    [comment] = issue_comments(github)
    assert "`main` is green" in comment
    assert main_fix_rows(tmp_path / "orch.db") == [("closed-unfed", 900)]


def test_green_main_closes_a_main_fix_still_waiting_for_capacity(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, box=1, factory_text=REQUIRED_MD, now=now)
    queue.enqueue(JobRequest(repo="other/repo", ticket_id="r", action="build",
                             recipient="claude", source_comment_id="r", timeout_seconds=600),
                  dedupe_key="r")
    DevicePool(queue, feeder.machines).claim("claude")
    red_main(github)
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["900"]["state"] == "waiting-capacity"
    green_main(github)
    now[0] += 60

    assert feeder.feed_once().errors == 0

    assert github.filed[MAIN[0], 900]["state"] == "closed"
    assert len(issue_comments(github)) == 1
    assert main_fix_rows(tmp_path / "orch.db") == [("closed-unfed", 900)]


def test_green_main_leaves_a_fed_main_fix_open_with_no_comment(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    red_main(github)
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["900"]["state"] == "fed"
    green_main(github)
    for _ in range(2):
        now[0] += 60
        assert feeder.feed_once().errors == 0

    assert github.filed[MAIN[0], 900]["state"] == "open"
    assert issue_comments(github) == []
    assert main_fix_rows(tmp_path / "orch.db") == [("closed", 900)]


def test_an_unreadable_issue_state_on_green_closes_nothing_until_it_reads(tmp_path: Path) -> None:
    github, poll = unfed_main_fix(tmp_path)

    def unreadable(repo: str, number: int) -> str:
        raise RuntimeError("issue unreadable")

    github.issue_state = unreadable
    poll()
    assert github.filed[MAIN[0], 900]["state"] == "open"
    assert issue_comments(github) == []
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]
    del github.issue_state

    assert poll() == 0
    assert github.filed[MAIN[0], 900]["state"] == "closed"
    assert main_fix_rows(tmp_path / "orch.db") == [("closed-unfed", 900)]


def test_a_failure_between_the_close_comment_and_the_close_leaves_one_comment(
    tmp_path: Path,
) -> None:
    github, poll = unfed_main_fix(tmp_path)
    close = github.close_issue

    def comment_posted_then_fail(*args: object) -> None:
        raise RuntimeError("lost the PATCH")

    github.close_issue = comment_posted_then_fail
    assert poll() == 1
    assert len(issue_comments(github)) == 1
    assert main_fix_rows(tmp_path / "orch.db") == [("closing", 900)]
    github.close_issue = close

    assert poll() == 0
    assert poll() == 0

    assert github.filed[MAIN[0], 900]["state"] == "closed"
    assert len(issue_comments(github)) == 1
    assert main_fix_rows(tmp_path / "orch.db") == [("closed-unfed", 900)]


def two_feeders_on_a_green_main_fix(tmp_path: Path):
    now = [10_000.0]
    closer, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    feeder = another_feeder(closer, queue, github)
    red_main(github)
    red, _ = closer._main_reads(closer._contexts()[0])
    assert closer._file_main_fixes(red, now[0]) == 0
    green_main(github)
    green, _ = closer._main_reads(closer._contexts()[0])
    feeder._main_reads = lambda contexts: (red, 0)
    return closer, feeder, github, green, now


def fed_and_closed(tmp_path: Path, github: FakeGitHub) -> tuple[str, str]:
    return rows(tmp_path / "orch.db")["900"]["state"], github.filed[MAIN[0], 900]["state"]


def test_a_feed_that_finishes_while_green_reads_the_issue_keeps_it_open(tmp_path: Path) -> None:
    closer, feeder, github, green, now = two_feeders_on_a_green_main_fix(tmp_path)
    read_state = github.issue_state

    def feed_then_read(repo: str, number: int) -> str:
        assert feeder.feed_once().errors == 0
        return read_state(repo, number)

    github.issue_state = feed_then_read

    assert closer._file_main_fixes(green, now[0]) == 0

    assert fed_and_closed(tmp_path, github) == ("fed", "open")
    assert issue_comments(github) == []


def test_a_feed_that_starts_while_green_closes_the_issue_does_not_begin(tmp_path: Path) -> None:
    closer, feeder, github, green, now = two_feeders_on_a_green_main_fix(tmp_path)
    post_once = github.post_issue_comment_once

    def feed_then_comment(*args):
        feeder.feed_once()
        return post_once(*args)

    github.post_issue_comment_once = feed_then_comment

    assert closer._file_main_fixes(green, now[0]) == 0

    assert fed_and_closed(tmp_path, github) == ("waiting-blocked", "closed")
    assert ("owner/app", "factory/issue-900") not in github.branches
    assert len(issue_comments(github)) == 1


def test_a_feed_from_a_list_read_before_the_green_close_does_not_begin(tmp_path: Path) -> None:
    closer, feeder, github, green, now = two_feeders_on_a_green_main_fix(tmp_path)
    listed = list(github.issues[MAIN[0]])
    assert closer._file_main_fixes(green, now[0]) == 0
    github.issues[MAIN[0]] = listed

    feeder.feed_once()

    assert fed_and_closed(tmp_path, github) == ("waiting-blocked", "closed")
    assert ("owner/app", "factory/issue-900") not in github.branches


def test_stale_retire_cannot_remove_the_close_feed_barrier(tmp_path: Path) -> None:
    closer, feeder, github, green, now = two_feeders_on_a_green_main_fix(tmp_path)
    late_closer = another_feeder(closer, closer.queue, github)
    read_state = github.issue_state
    feed = feeder._feed

    def close_during_the_read(repo: str, number: int) -> str:
        github.issue_state = read_state
        assert late_closer._file_main_fixes(green, now[0]) == 0
        assert main_fix_rows(tmp_path / "orch.db") == [("closed-unfed", 900)]
        return read_state(repo, number)

    def stale_green_then_feed(candidate, *args):
        github.issue_state = close_during_the_read
        assert closer._file_main_fixes(green, now[0]) == 0
        return feed(candidate, *args)

    feeder._feed = stale_green_then_feed

    assert feeder.feed_once().errors == 0

    assert main_fix_rows(tmp_path / "orch.db") == [("closed-unfed", 900)]
    assert not github.pulls
    assert ("owner/app", "factory/issue-900") not in github.branches
    assert fed_and_closed(tmp_path, github) == ("waiting-blocked", "closed")
    assert len(issue_comments(github)) == 1


def test_a_late_filer_cannot_reopen_a_main_fix_closed_on_green(tmp_path: Path) -> None:
    now = [10_000.0]
    filer, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    resumer = another_feeder(filer, queue, github)
    closer = another_feeder(filer, queue, github)
    red_main(github)
    red, _ = filer._main_reads(filer._contexts()[0])
    green_main(github)
    green, _ = filer._main_reads(filer._contexts()[0])
    file_issue = github.file_repo_issue

    def lapse_resume_and_close_then_return(*args, **kwargs) -> int:
        github.file_repo_issue = file_issue
        number = file_issue(*args, **kwargs)
        now[0] += CLAIM_SECONDS + 1
        assert resumer._file_main_fixes(red, now[0]) == 0
        assert closer._file_main_fixes(green, now[0]) == 0
        assert main_fix_rows(tmp_path / "orch.db") == [("closed-unfed", 900)]
        return number

    github.file_repo_issue = lapse_resume_and_close_then_return

    assert filer._file_main_fixes(red, now[0]) == 0

    assert main_fix_rows(tmp_path / "orch.db") == [("closed-unfed", 900)]
    assert github.filed[MAIN[0], 900]["state"] == "closed"


def red_head(github: FakeGitHub, n: int) -> str:
    head = f"{n:x}" * 40
    red_main(github, head=head[:40], run_id=str(900 + n))
    github.run_logs[("owner/app", str(900 + n))] = {
        "test (2/3)": f"collected 1 item\nFAILED tests/test_x.py::test_{n} - HEAD-{n}"
    }
    return head[:40]


def issue_comments(github: FakeGitHub, number: int = 900) -> list[str]:
    return [item.body for item in github.posted.get((MAIN[0], number), [])]


def test_each_later_red_head_adds_its_log_to_the_open_main_fix_once(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)

    def poll() -> None:
        now[0] += 60
        assert feeder.feed_once().errors == 0

    red_main(github)
    poll()
    assert main_fixes(github) == [900]
    assert issue_comments(github) == []

    second = red_head(github, 2)
    for _ in range(3):
        poll()
    [comment] = issue_comments(github)
    assert second in comment
    assert "FAILED tests/test_x.py::test_2 - HEAD-2" in comment
    assert RED_SHA not in comment

    third = red_head(github, 3)
    poll()
    poll()
    assert len(issue_comments(github)) == 2
    assert third in issue_comments(github)[1]
    assert "HEAD-3" in issue_comments(github)[1]
    assert main_fixes(github) == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def test_a_failure_after_the_head_comment_posts_leaves_one_comment(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    red_main(github)
    feeder.feed_once()
    red_head(github, 2)
    post = github.post_issue_comment_once

    def post_then_fail(*args: object) -> str:
        post(*args)
        raise RuntimeError("lost the response")

    github.post_issue_comment_once = post_then_fail
    now[0] += 60
    assert feeder.feed_once().errors == 1
    del github.post_issue_comment_once
    for _ in range(2):
        now[0] += 60
        assert feeder.feed_once().errors == 0

    assert len(issue_comments(github)) == 1


def test_a_second_feeder_adds_no_head_comment_while_the_first_holds_the_row(
    tmp_path: Path,
) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    other = another_feeder(feeder, queue, github)
    red_main(github)
    feeder.feed_once()
    red_head(github, 2)
    post = github.post_issue_comment_once
    raced: list[int] = []

    def racing(*args: object) -> str:
        raced.append(other.feed_once().errors)
        return post(*args)

    github.post_issue_comment_once = racing
    now[0] += 60
    assert feeder.feed_once().errors == 0

    assert raced == [0]
    assert len(issue_comments(github)) == 1


def test_a_later_head_whose_log_read_fails_adds_its_log_on_a_later_poll(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    red_main(github)
    feeder.feed_once()
    second = red_head(github, 2)
    github.fail["run_logs"] = 1

    now[0] += 60
    assert feeder.feed_once().errors == 1
    assert issue_comments(github) == []
    for _ in range(3):
        now[0] += 60
        assert feeder.feed_once().errors == 0

    [comment] = issue_comments(github)
    assert second in comment
    assert "FAILED tests/test_x.py::test_2 - HEAD-2" in comment


def test_a_stale_filing_completion_keeps_the_head_comment_claim(tmp_path: Path) -> None:
    now = [10_000.0]
    stale, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    recovering = another_feeder(stale, queue, github)
    competing = another_feeder(stale, queue, github)
    red_main(github)
    line = stale._main_reads(stale._contexts()[0])[0][MAIN]
    assert line.read.red is not None
    claimed = stale._claim_main_fix(line, *MAIN, line.read.red, now[0])
    assert claimed is not None
    now[0] += CLAIM_SECONDS + 1
    assert recovering.feed_once().errors == 0
    assert main_fixes(github) == [900]
    red_head(github, 2)
    raced: list[int] = []

    def stale_read_then_post(repo: str, number: int, body: str, marker: str) -> str:
        del github.post_issue_comment_once
        stale._post_main_fix(line, *claimed)
        raced.append(competing.feed_once().errors)
        return github.post_comment(repo, number, body)

    github.post_issue_comment_once = stale_read_then_post
    now[0] += 60
    assert recovering.feed_once().errors == 0

    assert raced == [0]
    assert len(issue_comments(github)) == 1
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def another_feeder(feeder: Feeder, queue, github: FakeGitHub) -> Feeder:
    return Feeder(queue, feeder.config, feeder.machines, github=github, linear=None,
                  factory_for=feeder.factory_for, head=lambda: None)


def test_a_second_feeder_never_posts_while_the_first_is_filing(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    other = another_feeder(feeder, queue, github)
    red_main(github)
    lines, _ = feeder._main_reads(feeder._contexts()[0])
    issues: list[dict] = []
    adapter = object.__new__(GitHubReviewActions)
    adapter._login = lambda: "feeder-bot"
    paused = [False]

    def pages(endpoint: str) -> list[object]:
        snapshot = list(issues)
        if not paused[0]:
            paused[0] = True
            assert other._file_main_fixes(lines, 10_001.0) == 0
        return [snapshot]

    def post(*args: str) -> dict:
        assert args[:3] == ("-X", "POST", "repos/owner/app/issues")
        body = next(arg[5:] for arg in args if arg.startswith("body="))
        item = {"number": len(issues) + 900, "body": body, "user": {"login": "feeder-bot"}}
        issues.append(item)
        return item

    adapter._pages = pages
    adapter._json = post
    github.file_repo_issue = adapter.file_repo_issue

    assert feeder._file_main_fixes(lines, 10_000.0) == 0

    assert [item["number"] for item in issues] == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def test_a_crashed_filer_is_resumed_by_another_feeder_once_its_claim_lapses(
    tmp_path: Path,
) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    red_main(github)
    lines, _ = feeder._main_reads(feeder._contexts()[0])

    def crash(*args: object, **kwargs: object) -> int:
        raise KeyboardInterrupt

    github.file_repo_issue = crash
    with pytest.raises(KeyboardInterrupt):
        feeder._file_main_fixes(lines, now[0])
    del github.file_repo_issue
    restarted = another_feeder(feeder, queue, github)

    now[0] += 60
    assert restarted.feed_once().errors == 0
    assert main_fixes(github) == []
    now[0] += 900

    assert restarted.feed_once().errors == 0
    assert main_fixes(github) == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def test_a_retried_filing_carries_the_first_red_head_and_its_log(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    red_main(github)
    github.run_logs[MAIN[0], "900"] = {"test (2/3)": "FIRST_HEAD_LOG"}
    github.fail["file_issue"] = 1
    assert feeder.feed_once().errors == 1
    red_main(github, head="d" * 40, run_id="901")
    github.run_logs[MAIN[0], "901"] = {"test (2/3)": "SECOND_HEAD_LOG"}

    assert another_feeder(feeder, queue, github).feed_once().errors == 0

    filed = github.filed[MAIN[0], 900]
    assert RED_SHA in filed["body"] and "FIRST_HEAD_LOG" in filed["body"]
    assert "SECOND_HEAD_LOG" not in filed["body"]
    assert filed["title"].startswith(f"Main is red at {RED_SHA[:8]}")


def test_long_failing_test_ids_leave_every_log_tail_in_the_brief(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    checks, logs = {}, {}
    for job in range(3):
        name = f"test ({job + 1}/3)"
        checks[name] = BaseCheck(name, "failure", "900", str(job + 1), RED_SHA)
        ids = [f"tests/test_payload.py::test_payload[{'abc'[job]}{'defghijklm'[case]}{'x' * 700}]"
               for case in range(10)]
        logs[name] = "\n".join(
            [*(f"FAILED {test_id} - AssertionError" for test_id in ids), f"TAIL_SENTINEL_JOB_{job}"]
        )
    github.main_checks[MAIN] = checks
    github.run_logs[MAIN[0], "900"] = logs

    dispatcher_for(tmp_path, queue, feeder).poll_once(FeederSource(github))

    assert len(github.filed[MAIN[0], 900]["body"]) <= BODY_LIMIT
    brief = github.posted[MAIN[0], 501][0].body
    assert all(f"TAIL_SENTINEL_JOB_{job}" in brief for job in range(3))


def overlap(tmp_path: Path, *, advance: float = 0, slow_logs: bool = False,
            during_post: bool = False, lose: Literal["crash", "response"] | None = None):
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    red_main(github)
    if slow_logs:
        github.main_checks[MAIN] = {
            f"test ({i})": BaseCheck(f"test ({i})", "failure", str(100 + i), str(i), RED_SHA)
            for i in range(16)
        }

        def read_logs(repo: str, run_id: str) -> dict[str, str]:
            now[0] += 59
            return {f"test ({int(run_id) - 100})": "FIRST_HEAD_LOG\nFIRST_TAIL"}

        github.failed_run_logs = read_logs
    other = another_feeder(feeder, queue, github)
    remote: list[dict] = []
    triggered = [False]

    def interleave() -> None:
        triggered[0] = True
        now[0] += advance
        assert other.feed_once().errors == 0

    def runner(argv, timeout):
        args = list(argv)[list(argv).index("api") + 1:]
        if "--paginate" in args:
            snapshot = list(remote)
            if not during_post and not triggered[0]:
                interleave()
            return GhResult(0, json.dumps(snapshot), "")
        if args == ["user"]:
            return GhResult(0, json.dumps({"login": "maxharris1"}), "")
        assert args[:2] == ["-X", "POST"], args
        stale = during_post and not triggered[0]
        if stale:
            interleave()
        fields = dict(arg.split("=", 1) for arg in args if "=" in arg)
        number = 900 + len(remote)
        remote.append({"number": number, "body": fields["body"], "user": {"login": "maxharris1"}})
        github.filed[("owner/app", number)] = {**fields, "state": "open"}
        github.issues.setdefault("owner/app", []).append(
            issue(number, body=fields["body"], title=fields["title"],
                  labels=("factory:ready", "factory:main-fix"), template=False))
        if stale and lose == "crash":
            raise KeyboardInterrupt
        if stale and lose == "response":
            return GhResult(1, "", "connection reset")
        return GhResult(0, json.dumps({"number": number}), "")

    github.file_repo_issue = GitHubReviewActions(runner=runner).file_repo_issue
    if lose == "crash":
        with pytest.raises(KeyboardInterrupt):
            feeder.feed_once()
    else:
        assert feeder.feed_once().errors == (1 if lose == "response" else 0)
    return remote, github


def open_main_fixes(github: FakeGitHub) -> list[int]:
    return sorted(n for (_, n), item in github.filed.items() if item["state"] == "open")


def test_review_overlapping_feeders(tmp_path: Path) -> None:
    remote, github = overlap(tmp_path)
    assert [item["number"] for item in remote] == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def test_review_overlapping_feeders_after_claim_expiry(tmp_path: Path) -> None:
    remote, github = overlap(tmp_path, advance=CLAIM_SECONDS + 1)
    assert [item["number"] for item in remote] == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def test_review_claim_already_expired_after_log_reads(tmp_path: Path) -> None:
    remote, github = overlap(tmp_path, slow_logs=True)
    assert [item["number"] for item in remote] == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def test_a_post_in_flight_past_its_lease_is_closed_as_a_duplicate_of_the_lowest(
    tmp_path: Path,
) -> None:
    remote, github = overlap(tmp_path, advance=CLAIM_SECONDS + 1, during_post=True)

    assert [item["number"] for item in remote] == [900, 901]
    assert open_main_fixes(github) == [900]
    assert "#900" in github.posted["owner/app", 901][-1].body
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def test_review_crash_after_duplicate_post(tmp_path, monkeypatch):
    captured = {}
    reconcile = Feeder._close_main_fix_duplicates

    def crash(self, repo, row, posted):
        if posted == 901:
            captured["feeder"] = self
            raise KeyboardInterrupt
        return reconcile(self, repo, row, posted)

    with monkeypatch.context() as patch:
        patch.setattr(Feeder, "_close_main_fix_duplicates", crash)
        with pytest.raises(KeyboardInterrupt):
            overlap(tmp_path, advance=CLAIM_SECONDS + 1, during_post=True)

    feeder = captured["feeder"]
    for _ in range(3):
        assert feeder.feed_once().errors == 0
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]
    assert open_main_fixes(feeder.github) == [900]


def assert_one_open_main_fix(tmp_path: Path, github: FakeGitHub) -> None:
    assert open_main_fixes(github) == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]
    assert [comment.body for comment in github.posted.get(("owner/app", 901), [])] == [
        "Closing as a duplicate of #900, the main-fix issue for this red streak."
    ]
    assert sorted(pull["head"] for pull in github.pulls.values()) == ["factory/issue-900"]


@pytest.mark.parametrize("lose", ["crash", "response"])
def test_a_stale_owner_post_that_lands_unreconciled_leaves_one_open_main_fix(
    tmp_path: Path, lose: Literal["crash", "response"]
) -> None:
    remote, github = overlap(tmp_path, advance=CLAIM_SECONDS + 1, during_post=True, lose=lose)
    assert [item["number"] for item in remote] == [900, 901]
    restarted, queue, _, _ = make_feeder(
        tmp_path, github=github, factory_text=REQUIRED_MD, now=[10_000.0 + CLAIM_SECONDS + 60]
    )
    other = another_feeder(restarted, queue, github)

    for feeder in (restarted, other, restarted):
        assert feeder.feed_once().errors == 0
        assert_one_open_main_fix(tmp_path, github)


def test_a_stale_owner_whose_post_response_is_lost_closes_its_duplicate_in_the_same_poll(
    tmp_path: Path,
) -> None:
    remote, github = overlap(tmp_path, advance=CLAIM_SECONDS + 1, during_post=True, lose="response")

    assert [item["number"] for item in remote] == [900, 901]
    assert_one_open_main_fix(tmp_path, github)


MIXED_ORCH = GITHUB_ORCH.replace("grok:", "linear:\n  api_key_env: LINEAR_API_KEY\ngrok:") + """\
  - repo: other/app
    factory_ref: main
    intake:
      label: factory:ready
  - repo: acme/linear-app
    factory_ref: dev
    linear_project_id: proj-1
    intake:
      linear_state: In Progress
trusted_linear_users:
  max-linear-id: [grok-bot]
"""


def test_a_ticket_quoting_an_active_main_fix_in_another_project_still_feeds(tmp_path: Path) -> None:
    feeder, _, github, linear = make_feeder(tmp_path, orch=MIXED_ORCH, factory_text=REQUIRED_MD)
    red_main(github)
    assert feeder.feed_once().errors == 0
    [number] = main_fixes(github)
    assert main_fix_rows(tmp_path / "orch.db") == [("open", number)]
    quoted = f"Seen during the outage:\n\n```\n{github.filed[('owner/app', number)]['body']}\n```\n"
    github.issues["owner/app"].append(issue(43, body=quoted))
    github.issues["other/app"] = [
        issue(42, repo="other/app", body=quoted),
        issue(44, repo="other/app", body=quoted, labels=("factory:ready", "factory:main-fix")),
    ]
    linear.issues = [replace(linear_issue(labels=("factory:main-fix",)), body=quoted + TEMPLATE)]

    candidates, _, errors = feeder._candidates(feeder._contexts()[0])

    assert errors == 0
    assert {(c.issue.repo, c.issue.key) for c in candidates} >= {
        ("owner/app", "43"), ("other/app", "42"), ("other/app", "44"), ("acme/linear-app", "AUT-7")
    }
    assert ("owner/app", 43) not in github.posted
    assert ("other/app", 42) not in github.posted
    assert ("other/app", 44) not in github.posted
    assert open_main_fixes(github) == [number]


def test_an_owner_whose_claim_lapsed_before_the_post_does_not_post(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, now=now)
    red_main(github)
    post = github.file_repo_issue

    def stalled(*args, before_post):
        github.file_repo_issue = post
        now[0] += CLAIM_SECONDS + 1
        before_post()
        raise AssertionError("posted after its claim lapsed")

    github.file_repo_issue = stalled

    assert feeder.feed_once().errors == 0
    assert main_fixes(github) == []
    assert main_fix_rows(tmp_path / "orch.db") == [("filing", None)]

    now[0] += 60
    assert feeder.feed_once().errors == 0
    assert main_fixes(github) == [900]
    assert main_fix_rows(tmp_path / "orch.db") == [("open", 900)]


def test_main_fix_pr_names_the_fed_main_fix_and_no_ordinary_pr(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    github.issues["owner/app"] = [issue(1)]
    github.main_checks[MAIN] = {"test (2/3)": BaseCheck("test (2/3)", "success", "899", "1", RED_SHA)}
    dispatcher = dispatcher_for(tmp_path, queue, feeder)
    source = FeederSource(github)
    dispatcher.poll_once(source)
    red_main(github)
    dispatcher.poll_once(source)

    pulls = {github.pulls[key]["head"]: str(key[1]) for key in github.pulls}
    assert set(pulls) == {"factory/issue-1", "factory/issue-900"}
    assert feeder.main_fix_pr("Owner/App", pulls["factory/issue-900"])
    assert not feeder.main_fix_pr("owner/app", pulls["factory/issue-1"])
    assert not feeder.main_fix_pr("owner/app", "999")
