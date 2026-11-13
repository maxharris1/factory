"""Backlog feeder (issue #85 A): tagged tickets become queued jobs by themselves.

Every test uses a fake GitHub and a fake Linear client. Nothing here reaches
a real tracker, and no test needs a real token or signing secret.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.device_pool import DevicePool
from lib.dispatcher import Comment, Dispatcher, TicketRef, TrackedTicket
from lib.factory_md import parse_factory_context
from lib.feeder import (
    FEEDER_TABLE,
    Feeder,
    IntakeIssue,
    feeder_counts,
    go_state_for,
)
from lib.handoff import parse_comment
from lib.machines_yml import load_machines_yml
from lib.orch_yml import load_orch_yml
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions, factory

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
---

## Context
Project rule for every brief: run the suite with `make test`.
"""

LINEAR_FACTORY_MD = FACTORY_MD.replace("base_branch: main", "base_branch: dev").replace(
    "tickets: github", "tickets: linear"
).replace("ship: merge", "ship: handoff")

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
            ]
    path = tmp_path / "machines.yml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config, findings = load_machines_yml(path, allow_placeholders=True)
    assert config is not None, findings
    return config


class FakeGitHub:
    """Fake GitHub: intake reads plus the writes the feeder makes."""

    def __init__(self) -> None:
        self.issues: dict[str, list[IntakeIssue]] = {}
        self.branches: dict[tuple[str, str], str] = {}
        self.commits: list[tuple[str, str, str]] = []
        self.pulls: dict[tuple[str, int], dict] = {}
        self.labels: dict[tuple[str, int], list[str]] = {}
        self.posted: dict[tuple[str, int], list[Comment]] = {}
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}
        self._next_pr = 500
        self._next_comment = 9000

    def _maybe_fail(self, step: str) -> None:
        left = self.fail.get(step, 0)
        if left:
            self.fail[step] = left - 1
            raise RuntimeError(f"fake GitHub failure in {step}")

    # intake reads
    def github_intake(self, repo: str, label: str) -> list[IntakeIssue]:
        self.calls.append(f"intake:{repo}:{label}")
        return list(self.issues.get(repo, []))

    # writes
    def branch_sha(self, repo: str, branch: str) -> str | None:
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
        for (pull_repo, number), pull in self.pulls.items():
            if pull_repo == repo and pull["head"] == branch:
                return number
        return None

    def open_draft_pull(self, repo: str, *, branch: str, base: str, title: str, body: str) -> int:
        self._maybe_fail("pull")
        assert self.find_pull(repo, branch) is None, "second PR for one branch"
        self._next_pr += 1
        self.pulls[(repo, self._next_pr)] = {
            "head": branch, "base": base, "title": title, "body": body, "draft": True,
        }
        self.calls.append(f"pull:{repo}:{self._next_pr}")
        return self._next_pr

    def add_labels(self, repo: str, number: int, labels) -> None:
        self._maybe_fail("labels")
        self.labels.setdefault((repo, number), [])
        for label in labels:
            if label not in self.labels[(repo, number)]:
                self.labels[(repo, number)].append(label)

    def comments(self, repo: str, number: int) -> list[Comment]:
        return list(self.posted.get((repo, number), []))

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
        self.queries: list[tuple[str, str]] = []
        self._next = 0

    def linear_intake(self, project_id: str, state: str) -> list[IntakeIssue]:
        self.queries.append((project_id, state))
        return list(self.issues)

    def linear_comments(self, issue_id: str) -> list[Comment]:
        return list(self.posted.get(issue_id, []))

    def post_linear_comment(self, issue_id: str, body: str) -> str:
        self._next += 1
        item = Comment(id=f"lin-c-{self._next}", author="max-linear-id", body=body,
                       created_at=float(self._next))
        self.posted.setdefault(issue_id, []).append(item)
        return item.id


def issue(number: int, *, labels=("factory:ready",), applied_by="maxharris1",
          created_at: float = 1000.0, body: str = "Fix the thing in lib/x.py.",
          title: str | None = None, repo: str = "owner/app") -> IntakeIssue:
    return IntakeIssue(
        repo=repo,
        tracker="github",
        key=str(number),
        title=title or f"Issue {number}",
        body=body,
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
        body="Touch src/a.ts only.",
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

    assert linear.queries == [("proj-1", "In Progress")]
    assert report.fed == (TicketRef("acme/linear-app", "AUT-7", "linear"),)
    pull = github.pulls[("acme/linear-app", 501)]
    assert pull["head"] == "max/aut-7-linear-work"  # Linear's own branch name
    assert pull["base"] == "dev"
    assert pull["title"].startswith("AUT-7")
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
                   factory_for=lambda project: context_for(FACTORY_MD), log=lambda line: None)
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
    assert counts == {"eligible": 2, "fed": 1, "waiting_capacity": 1}


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
    assert "Feeder" in pages["index.html"]


# --- the poller: fed issues become queued jobs within one poll --------------


class FeederSource:
    """A poll source whose fresh_ticket reads the fake GitHub's PRs."""

    def __init__(self, github: FakeGitHub):
        self.github_fake = github

    def github_candidates(self, label_prefix: str):
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
