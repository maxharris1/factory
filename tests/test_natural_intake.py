"""Tests for natural D31 clarifying intake (E3-2)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import Comment, Dispatcher
from lib.evidence.criteria import numbered
from lib.factory_md import load_factory_md, parse_factory_context
from lib.feeder import FEEDER_TABLE, Feeder, IntakeIssue
from lib.handoff import parse_comment
from lib.natural_intake import (
    INTAKE_STATE_LABEL,
    NATURAL_INTAKE_ACTION,
    NaturalIntakeSession,
)
from lib.orch_yml import (
    DEFAULT_NATURAL_INTAKE_LABEL,
    DriverInbox,
    Project,
    load_orch_yml,
)
from lib.orchestrate import ClassifiedQuestion, GoBlocked, QuestionError
from lib.queue import SQLiteJobQueue
from lib.schema import AUT0_REPO
from tests.test_driver_inbox import InboxIssue
from tests.test_feeder import (
    FACTORY_MD,
    LINEAR_FACTORY_MD,
    FakeGitHub,
    FakeLinear,
    machines,
    orch_config,
)
from tests.test_hooks import (
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    FakeSource,
    T7Hook,
    T8Hook,
    ticket,
)

ROOT = Path(__file__).resolve().parents[1]

GITHUB_NATURAL_ORCH = """
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
      natural_label: factory:idea
"""

LINEAR_NATURAL_ORCH = """
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
      natural_linear_state: Triage
      linear_assignees:
        proj-1:
          - 11111111-2222-3333-4444-555555555555
"""

VALID_STRICT_BODY = """
## Goal
Add health check endpoint.

## Acceptance criteria
- GET /health returns HTTP 200 with JSON ok=true.

## Out of scope
- Metrics export.

Risk: low
Plan first: no
Depends on: none
"""


class LabelAwareFakeGitHub(FakeGitHub):
    """FakeGitHub that filters returned issues by requested intake label."""

    def __init__(self) -> None:
        super().__init__()
        self.by_label: dict[tuple[str, str], list[IntakeIssue]] = {}

    def github_intake(self, repo: str, label: str) -> list[IntakeIssue]:
        self.calls.append(f"intake:{repo}:{label}")
        self._maybe_fail(f"intake:{repo}")
        if (repo, label) in self.by_label:
            return list(self.by_label[(repo, label)])
        return [
            issue
            for issue in self.issues.get(repo, [])
            if any(lbl.lower() == label.lower() for lbl in issue.labels)
        ]


class StateAwareFakeLinear(FakeLinear):
    """FakeLinear that separates issues by workflow state."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.by_state: dict[str, list[IntakeIssue]] = {}

    def linear_intake(self, project_ids, state, assignees):
        self.calls.append(f"linear:{state}")
        return list(self.by_state.get(state, []))


def _factory_config():
    config, findings = load_factory_md(ROOT / "examples/cleffy/factory.md")
    assert config is not None and not findings
    return config


def _parse_context(markdown: str):
    context, findings = parse_factory_context(markdown)
    assert context is not None and not findings
    return context


def _rough_github_issue(
    key: str = "42",
    *,
    repo: str = "owner/app",
    title: str = "Make onboarding smoother for mobile users",
    body: str = "Rough idea: onboarding feels slow on phones, let's tighten it up.",
    labels: tuple[str, ...] = ("factory:idea",),
    applied_by: str | None = "maxharris1",
) -> IntakeIssue:
    return IntakeIssue(
        repo=repo,
        tracker="github",
        key=key,
        title=title,
        body=body,
        labels=labels,
        created_at=100.0,
        applied_by=applied_by,
        url=f"https://github.com/{repo}/issues/{key}",
    )


def _rough_linear_issue(
    key: str = "ACM-99",
    *,
    repo: str = "acme/linear-app",
    applied_by: str | None = "max-linear-id",
    assignee: str | None = "11111111-2222-3333-4444-555555555555",
) -> IntakeIssue:
    return IntakeIssue(
        repo=repo,
        tracker="linear",
        key=key,
        title="Rethink billing retry cadence",
        body="Rough note from customer call — no formal template yet.",
        labels=(),
        created_at=100.0,
        applied_by=applied_by,
        url=f"https://linear.app/acme/issue/{key}",
        linear_id="lin-uuid-99",
        linear_project="proj-1",
        assignee=assignee,
    )


# -- orch.yml schema validation ------------------------------------------------


def test_orch_yml_natural_label_and_state_schema(tmp_path: Path) -> None:
    cfg = orch_config(tmp_path, GITHUB_NATURAL_ORCH)
    intake = cfg.projects[0].intake
    assert intake is not None
    assert intake.label == "factory:ready"
    assert intake.natural_label == "factory:idea"
    assert intake.natural_linear_state is None

    # Empty/null natural_label resolves to DEFAULT_NATURAL_INTAKE_LABEL.
    empty_default = GITHUB_NATURAL_ORCH.replace("natural_label: factory:idea", "natural_label:")
    cfg_default = orch_config(tmp_path, empty_default)
    assert cfg_default.projects[0].intake is not None
    assert cfg_default.projects[0].intake.natural_label == DEFAULT_NATURAL_INTAKE_LABEL

    # Omitted natural_label stays None (opt-in, zero extra reads).
    omitted = GITHUB_NATURAL_ORCH.replace("      natural_label: factory:idea\n", "")
    cfg_omitted = orch_config(tmp_path, omitted)
    assert cfg_omitted.projects[0].intake is not None
    assert cfg_omitted.projects[0].intake.natural_label is None

    # Linear natural_linear_state accepted.
    lcfg = orch_config(tmp_path, LINEAR_NATURAL_ORCH)
    lintake = lcfg.projects[0].intake
    assert lintake is not None
    assert lintake.linear_state == "In Progress"
    assert lintake.natural_linear_state == "Triage"
    assert lintake.natural_label is None


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (
            GITHUB_NATURAL_ORCH.replace("natural_label: factory:idea", "natural_label: FACTORY:READY"),
            "natural_label must differ from",
        ),
        (
            GITHUB_NATURAL_ORCH.replace("natural_label: factory:idea", "natural_label: true"),
            "natural_label must be a GitHub label name",
        ),
        (
            GITHUB_NATURAL_ORCH.replace("natural_label: factory:idea", "natural_linear_state: Backlog"),
            "natural_linear_state needs linear_project_id",
        ),
        (
            LINEAR_NATURAL_ORCH.replace("natural_linear_state: Triage", "natural_label: factory:idea"),
            "natural_label is for GitHub projects",
        ),
        (
            LINEAR_NATURAL_ORCH.replace("natural_linear_state: Triage", "natural_linear_state: in progress"),
            "natural_linear_state must differ from",
        ),
        (
            LINEAR_NATURAL_ORCH.replace("natural_linear_state: Triage", "natural_linear_state: 123"),
            "natural_linear_state must name a Linear workflow state",
        ),
    ],
)
def test_orch_yml_rejects_invalid_natural_intake_config(
    tmp_path: Path, body: str, expected_error: str
) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(body, encoding="utf-8")
    cfg, findings = load_orch_yml(path, allow_placeholders=True)
    assert cfg is None
    assert any(expected_error in f.message for f in findings), findings


# -- Feeder natural intake pickup, deduplication & strict fast-path ------------


def test_rough_github_issue_enters_intake_and_deduplicates_wakes(tmp_path: Path) -> None:
    cfg = orch_config(tmp_path, GITHUB_NATURAL_ORCH)
    gh = LabelAwareFakeGitHub()
    issue = _rough_github_issue("42")
    gh.issues["owner/app"] = [issue]
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000.0)
    context = _parse_context(FACTORY_MD)
    feeder = Feeder(
        queue,
        cfg,
        machines(tmp_path),
        github=gh,
        linear=None,
        factory_for=lambda _p: context,
        head=lambda: "h" * 40,
    )

    first = feeder.feed_once()
    assert first.natural_queued == 1
    assert first.refused == 0
    assert first.fed == ()
    assert first.errors == 0
    [(_repo, number)] = gh.pulls
    assert gh.labels[("owner/app", number)] == ["factory", INTAKE_STATE_LABEL]
    # No refused-intake row or intake-refused needs_attention.
    with queue.connect() as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {FEEDER_TABLE}").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM needs_attention").fetchone()[0] == 0
        jobs = conn.execute("SELECT * FROM jobs").fetchall()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["action"] == NATURAL_INTAKE_ACTION
    assert job["recipient"] == "grok"
    assert job["ticket_id"] == str(number)
    detail = json.loads(job["detail"])
    assert detail["natural_intake"] is True
    assert detail["signal"] == "factory:idea"
    assert detail["link"] == f"https://github.com/owner/app/pull/{number}"
    assert detail["ticket_url"] == "https://github.com/owner/app/issues/42"
    assert detail["title"] == issue.title

    gh.calls.clear()
    second = feeder.feed_once()
    assert second.natural_queued == 0
    assert second.errors == 0
    assert len(gh.pulls) == 1
    assert not any(call.startswith(("commit:", "branch:", "pull:")) for call in gh.calls)
    with queue.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_natural_intake_ignores_untrusted_and_started_and_retries_label_failure(
    tmp_path: Path,
) -> None:
    cfg = orch_config(tmp_path, GITHUB_NATURAL_ORCH)
    gh = LabelAwareFakeGitHub()
    gh.issues["owner/app"] = [
        _rough_github_issue("10", applied_by="stranger"),
        _rough_github_issue("11", applied_by=None),
        _rough_github_issue("12", applied_by="maxharris1"),
    ]
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000.0)
    context = _parse_context(FACTORY_MD)
    feeder = Feeder(
        queue,
        cfg,
        machines(tmp_path),
        github=gh,
        linear=None,
        factory_for=lambda _p: context,
        head=lambda: "h" * 40,
    )

    gh.fail["labels"] = 1
    first = feeder.feed_once()
    assert first.natural_queued == 0
    assert first.errors == 1
    with queue.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    second = feeder.feed_once()
    assert second.natural_queued == 1
    assert second.errors == 0
    assert [pull["head"] for pull in gh.pulls.values()] == ["factory/issue-12"]
    [(_repo, number)] = gh.pulls
    assert gh.labels[("owner/app", number)] == ["factory", INTAKE_STATE_LABEL]

    gh.issues["owner/app"].append(_rough_github_issue("13", applied_by="maxharris1"))
    with queue.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ticket_state (repo TEXT, ticket_id TEXT, workflow_state TEXT)"
        )
        conn.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) VALUES (?, ?, ?)",
            ("owner/app", "13", "building"),
        )
    third = feeder.feed_once()
    assert third.natural_queued == 0
    assert len(gh.pulls) == 1


def test_linear_natural_intake_applies_no_github_labels_and_respects_assignees(
    tmp_path: Path,
) -> None:
    cfg = orch_config(tmp_path, LINEAR_NATURAL_ORCH)
    gh = LabelAwareFakeGitHub()
    lin = StateAwareFakeLinear()
    lin.by_state["Triage"] = [
        _rough_linear_issue("ACM-99"),
        _rough_linear_issue("ACM-100", assignee="99999999-9999-9999-9999-999999999999"),
    ]
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000.0)
    context = _parse_context(LINEAR_FACTORY_MD)
    feeder = Feeder(
        queue,
        cfg,
        machines(tmp_path),
        github=gh,
        linear=lin,
        factory_for=lambda _p: context,
        head=lambda: "h" * 40,
    )

    report = feeder.feed_once()
    assert report.natural_queued == 1
    assert report.errors == 0
    assert gh.labels == {}
    with queue.connect() as conn:
        rows = conn.execute("SELECT ticket_id, action, recipient, detail FROM jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == "ACM-99"
    assert rows[0]["action"] == NATURAL_INTAKE_ACTION
    assert rows[0]["recipient"] == "grok"
    detail = json.loads(rows[0]["detail"])
    assert detail["tracker"] == "linear"
    assert detail["signal"] == "Triage"


def test_aut0_natural_intake_omits_issue_title_from_job_detail(tmp_path: Path) -> None:
    aut0_orch = LINEAR_NATURAL_ORCH.replace("acme/linear-app", AUT0_REPO)
    cfg = orch_config(tmp_path, aut0_orch)
    gh = LabelAwareFakeGitHub()
    lin = StateAwareFakeLinear()
    lin.by_state["Triage"] = [_rough_linear_issue("AUT-55", repo=AUT0_REPO)]
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000.0)
    context = _parse_context(LINEAR_FACTORY_MD)
    feeder = Feeder(
        queue,
        cfg,
        machines(tmp_path),
        github=gh,
        linear=lin,
        factory_for=lambda _p: context,
        head=lambda: "h" * 40,
    )

    report = feeder.feed_once()
    assert report.natural_queued == 1
    with queue.connect() as conn:
        detail = json.loads(conn.execute("SELECT detail FROM jobs").fetchone()[0])
    assert "title" not in detail
    assert detail["link"] == "https://linear.app/acme/issue/AUT-55"


def test_factory_ready_keeps_strict_204_validation_and_takes_precedence(tmp_path: Path) -> None:
    cfg = orch_config(tmp_path, GITHUB_NATURAL_ORCH)
    gh = LabelAwareFakeGitHub()
    # Issue 1: valid strict factory:ready -> fed directly with zero intake-clarify wake.
    # Issue 2: malformed factory:ready AND factory:idea -> strict path refuses it (#204), natural skips it.
    # Issue 3: rough factory:idea only -> enters natural intake cleanly without refused-intake.
    gh.issues["owner/app"] = [
        _rough_github_issue("1", body=VALID_STRICT_BODY, labels=("factory:ready",)),
        _rough_github_issue("2", body="Missing template fields", labels=("factory:ready", "factory:idea")),
        _rough_github_issue("3", body="Rough idea without template", labels=("factory:idea",)),
    ]
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000.0)
    context = _parse_context(FACTORY_MD)
    feeder = Feeder(
        queue,
        cfg,
        machines(tmp_path),
        github=gh,
        linear=None,
        factory_for=lambda _p: context,
        head=lambda: "h" * 40,
    )

    report = feeder.feed_once()
    assert len(report.fed) == 1
    assert report.refused == 1
    assert report.natural_queued == 1
    natural = next(n for (_r, n), pull in gh.pulls.items() if pull["head"] == "factory/issue-3")
    assert gh.labels[("owner/app", natural)] == ["factory", INTAKE_STATE_LABEL]
    with queue.connect() as conn:
        actions = {
            (row["ticket_id"], row["action"])
            for row in conn.execute("SELECT ticket_id, action FROM jobs").fetchall()
        }
        states = {
            row["issue"]: row["state"]
            for row in conn.execute(f"SELECT issue, state FROM {FEEDER_TABLE}").fetchall()
        }
    assert ("2", "refused") in actions
    assert {ticket for ticket, action in actions if action == NATURAL_INTAKE_ACTION} == {str(natural)}
    assert states["1"] == "fed"
    assert states["2"] == "refused-intake"
    assert "3" not in states


def test_natural_intake_wake_delivers_to_driver_inbox_with_its_pull(tmp_path: Path) -> None:
    cfg = orch_config(
        tmp_path,
        GITHUB_NATURAL_ORCH
        + "    driver_inbox: owner/app#7\n    grok_webhook: false\n",
    )
    gh = LabelAwareFakeGitHub()
    gh.issues["owner/app"] = [_rough_github_issue("42")]
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=lambda: 1000.0)
    context = _parse_context(FACTORY_MD)
    feeder = Feeder(
        queue,
        cfg,
        machines(tmp_path),
        github=gh,
        linear=None,
        factory_for=lambda _p: context,
        head=lambda: "h" * 40,
    )
    assert feeder.feed_once().natural_queued == 1

    inbox_issue = InboxIssue()
    dispatcher = Dispatcher(
        db,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions={"owner/app": inbox_issue},
        grok_delivery=FakeGrokDelivery(),
        projects=(Project("owner/app", driver_inbox=DriverInbox("owner/app", 7), grok_webhook=False),),
        hooks=(T7Hook(), T8Hook(None)),
    )
    dispatcher.reconcile_jobs()

    assert len(inbox_issue.posts) == 1
    repo, number, body = inbox_issue.posts[0]
    assert (repo, number) == ("owner/app", 7)
    lines = body.splitlines()
    [(_repo, number)] = gh.pulls
    assert lines[0] == f"**Wake: intake-clarify** on owner/app {number}"
    assert lines[1] == f"PR: https://github.com/owner/app/pull/{number}"
    assert lines[2] == f"Cause: https://github.com/owner/app/pull/{number}"
    assert "natural intake (factory:idea)" in body


# -- D31 question discipline & Dispatcher pause/resume round-trip --------------


def test_d31_observable_and_defaultable_auto_proceed_without_waiting_max(tmp_path: Path) -> None:
    session = NaturalIntakeSession(_factory_config())
    assert session.state == "intake"

    # Promote before questions step raises GoBlocked.
    with pytest.raises(GoBlocked, match="questions step"):
        session.promote(
            goal="Tighten mobile onboarding",
            acceptance_criteria=["First paint < 1s on mobile viewport"],
            likely_files=["lib/onboarding.py"],
            risk="low",
            done_evidence="pytest tests/test_onboarding.py",
        )

    step = session.classify_and_post(
        "Tighten mobile onboarding layout and trim redundant API calls.",
        [
            ClassifiedQuestion("Which endpoint renders onboarding?", "observable"),
            ClassifiedQuestion(
                "What timeout should the retry helper use?",
                "default-able",
                default="5 seconds with exponential backoff",
            ),
        ],
    )
    assert step.state == "ready-to-promote"
    assert step.open_product_questions == ()
    assert step.next_product_question is None
    assert step.recorded_defaults == (
        ("What timeout should the retry helper use?", "5 seconds with exponential backoff"),
    )
    assert step.observable_questions == ("Which endpoint renders onboarding?",)

    parsed_q = parse_comment(step.comment)
    assert parsed_q.as_dict()["kind"] == "questions"
    assert "state" not in parsed_q.as_dict()
    assert "Default: 5 seconds with exponential backoff" in parsed_q.body

    # Dispatcher does NOT pause in waiting-max for observable/default-able only.
    db = tmp_path / "d.db"
    queue = SQLiteJobQueue(db, clock=lambda: 100.0)
    actions = FakeReviewActions()
    dispatcher = Dispatcher(
        db,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=actions,
        grok_delivery=FakeGrokDelivery(),
        hooks=(T7Hook(), T8Hook(None)),
    )
    q_comment = Comment(
        id="101",
        author="grok-login",
        body=step.comment,
        created_at=100.0,
        url="https://github.com/owner/repo/pull/42#issuecomment-101",
    )
    dispatcher.process(replace(ticket(), comments=(q_comment,)))
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT outcome FROM handled_comments WHERE comment_id = '101'"
        ).fetchone()
    assert row is not None and row[0] == "no-questions"

    # Promote immediately succeeds and emits structured brief (with Out of scope) + go.
    artifacts = session.promote(
        goal="Tighten mobile onboarding",
        acceptance_criteria=[
            "First paint < 1s on mobile viewport",
            "Retry helper caps backoff at 5s",
        ],
        likely_files=["lib/onboarding.py", "tests/test_onboarding.py"],
        out_of_scope=["Desktop redesign", "Auth flow changes"],
        risk="low",
        done_evidence="pytest tests/test_onboarding.py passes",
    )
    assert session.state == "promoted"
    brief_parsed = parse_comment(artifacts.brief_comment)
    assert brief_parsed.as_dict()["kind"] == "brief"
    assert brief_parsed.as_dict()["risk"] == "low"
    for heading in (
        "**Goal**",
        "**Acceptance criteria**",
        "**Likely files**",
        "**Out of scope**",
        "**Risk**",
        "**Done-evidence**",
    ):
        assert heading in brief_parsed.body
    assert "- Desktop redesign" in brief_parsed.body

    go_parsed = parse_comment(artifacts.go_comment)
    assert go_parsed.as_dict()["kind"] == "go"
    assert go_parsed.as_dict()["state"] == "building"
    assert go_parsed.as_dict()["risk"] == "low"

    # Second promote fails closed.
    with pytest.raises(GoBlocked, match="already posted"):
        session.promote(
            goal="Duplicate",
            acceptance_criteria=["x"],
            likely_files=["a.py"],
            risk="low",
            done_evidence="pytest",
        )


def test_d31_product_questions_pause_in_waiting_max_and_promote_after_answers(
    tmp_path: Path,
) -> None:
    session = NaturalIntakeSession(_factory_config())
    step = session.classify_and_post(
        "Support soft-deleting workspaces with retention.",
        [
            ClassifiedQuestion("Where is workspace deletion handled?", "observable"),
            ClassifiedQuestion("Column naming convention?", "default-able", default="deleted_at UTC"),
            ClassifiedQuestion("How many days before permanent purge?", "product"),
            ClassifiedQuestion("Should admins be notified by email on purge?", "product"),
        ],
    )
    assert step.state == "waiting-max"
    assert len(step.open_product_questions) == 2
    assert step.next_product_question is not None
    assert step.next_product_question.text == "How many days before permanent purge?"

    # Product question sets state=waiting-max on the questions handoff.
    parsed_q = parse_comment(step.comment)
    assert parsed_q.as_dict()["state"] == "waiting-max"

    # Cannot promote while product questions are open.
    with pytest.raises(GoBlocked, match="product questions are open"):
        session.promote(
            goal="Soft-delete workspaces",
            acceptance_criteria=["Deleted workspaces hide from active queries"],
            likely_files=["a.py", "b.py", "c.py", "d.py"],
            risk="high",
            done_evidence="pytest tests/test_workspaces.py",
        )

    # Out-of-order product answer is rejected (one product question at a time).
    with pytest.raises(QuestionError, match="open product question first"):
        session.answer_product_question(
            "Should admins be notified by email on purge?", "Yes, 24h before"
        )

    # Answer #1 -> still waiting-max on #2.
    step2 = session.answer_product_question("How many days before permanent purge?", "30 days")
    assert step2.state == "waiting-max"
    assert step2.next_product_question is not None
    assert step2.next_product_question.text == "Should admins be notified by email on purge?"
    with pytest.raises(GoBlocked, match="product questions are open"):
        session.promote(
            goal="Soft-delete workspaces",
            acceptance_criteria=["Purge after 30 days"],
            likely_files=["a.py"],
            risk="high",
            done_evidence="pytest",
        )

    # Answer #2 -> cleared -> ready-to-promote.
    step3 = session.answer_product_question(
        "Should admins be notified by email on purge?", "Yes, email workspace owners 24h prior"
    )
    assert step3.state == "ready-to-promote"
    assert step3.next_product_question is None

    # >3 files routes to planning.
    artifacts = session.promote(
        goal="Soft-delete workspaces with 30-day retention",
        acceptance_criteria=["Soft delete sets deleted_at", "Purge job runs after 30 days"],
        likely_files=["a.py", "b.py", "c.py", "d.py"],
        out_of_scope=["Billing refunds"],
        risk="high",
        done_evidence="pytest tests/test_workspaces.py",
    )
    assert artifacts.decision.job == "plan"
    assert parse_comment(artifacts.go_comment).as_dict()["state"] == "planning"


def test_dispatcher_full_waiting_max_pause_resume_and_go_launch(tmp_path: Path) -> None:
    session = NaturalIntakeSession(_factory_config())
    q_step = session.classify_and_post(
        "Add workspace archive toggle.",
        [ClassifiedQuestion("Can archived workspaces be unarchived by editors?", "product")],
    )
    a_step = session.answer_all_product_questions(
        [("Can archived workspaces be unarchived by editors?", "Owners only.")]
    )
    assert a_step.state == "ready-to-promote"
    artifacts = session.promote(
        goal="Add workspace archive toggle",
        acceptance_criteria=["Only owners can unarchive"],
        likely_files=["lib/workspace.py"],
        out_of_scope=["Bulk archive"],
        risk="low",
        done_evidence="pytest tests/test_workspace.py",
    )

    db = tmp_path / "d.db"
    queue = SQLiteJobQueue(db, clock=lambda: 200.0)
    actions = FakeReviewActions()
    grok = FakeGrokDelivery()
    dispatcher = Dispatcher(
        db,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=actions,
        grok_delivery=grok,
        hooks=(T7Hook(), T8Hook(None)),
    )

    c_questions = Comment("201", "grok-login", q_step.comment, 200.0, url="https://github.com/owner/repo/pull/42#c201")
    dispatcher.process(replace(ticket(), comments=(c_questions,)))
    with sqlite3.connect(db) as conn:
        state_waiting = conn.execute(
            "SELECT workflow_state FROM ticket_state WHERE ticket_id = '42'"
        ).fetchone()[0]
    assert state_waiting == "waiting-max"

    c_answers = Comment("202", "grok-login", a_step.comment, 210.0, url="https://github.com/owner/repo/pull/42#c202")
    dispatcher.process(replace(ticket(), comments=(c_questions, c_answers)))
    with sqlite3.connect(db) as conn:
        state_resumed = conn.execute(
            "SELECT workflow_state FROM ticket_state WHERE ticket_id = '42'"
        ).fetchone()[0]
        decisions = conn.execute(
            "SELECT question, choice FROM decisions WHERE ticket_id = '42'"
        ).fetchall()
    assert state_resumed == "questions"
    assert decisions == [("Can archived workspaces be unarchived by editors?", "Owners only.")]

    c_brief = Comment("203", "grok-login", artifacts.brief_comment, 220.0, url="https://github.com/owner/repo/pull/42#c203")
    c_go = Comment("204", "grok-login", artifacts.go_comment, 221.0, url="https://github.com/owner/repo/pull/42#c204")
    dispatcher.process(replace(ticket(), comments=(c_questions, c_answers, c_brief, c_go)))
    claimed = queue.claim("claude")
    assert claimed is not None
    assert claimed.request.action == "build"


@pytest.mark.parametrize("out_of_scope", [(), ("UI redesign",)])
def test_promoted_brief_keeps_acceptance_criteria_after_an_html_goal(
    out_of_scope: tuple[str, ...],
) -> None:
    session = NaturalIntakeSession(_factory_config())
    session.classify_and_post("Strip script tags from rendered notes.", [])
    artifacts = session.promote(
        goal="<script> tags must be removed.",
        acceptance_criteria=["Scripts are removed.", "Safe HTML remains."],
        likely_files=["lib/a.py"],
        out_of_scope=out_of_scope,
        risk="low",
        done_evidence="Tests pass.",
        visible=False,
    )
    body = parse_comment(artifacts.brief_comment).body
    assert [c.text for c in numbered(body)] == ["Scripts are removed.", "Safe HTML remains."]
    assert ("- UI redesign" in body) == bool(out_of_scope)


def test_rough_github_issue_opens_a_tracked_pull_that_pauses_then_launches(
    tmp_path: Path,
) -> None:
    cfg = orch_config(tmp_path, GITHUB_NATURAL_ORCH)
    gh = LabelAwareFakeGitHub()
    gh.issues["owner/app"] = [_rough_github_issue("31")]
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=lambda: 1000.0)
    context = _parse_context(FACTORY_MD)
    feeder = Feeder(
        queue,
        cfg,
        machines(tmp_path),
        github=gh,
        linear=None,
        factory_for=lambda _p: context,
        head=lambda: "h" * 40,
    )
    assert feeder.feed_once().natural_queued == 1
    [(repo, number)] = gh.pulls
    assert repo == "owner/app"
    assert gh.pulls[(repo, number)]["head"] == "factory/issue-31"
    assert gh.pulls[(repo, number)]["draft"] is True
    assert INTAKE_STATE_LABEL in gh.labels[(repo, number)]
    with queue.connect() as conn:
        [job] = conn.execute("SELECT ticket_id, detail FROM jobs").fetchall()
    assert job["ticket_id"] == str(number)
    pull_url = f"https://github.com/owner/app/pull/{number}"
    assert json.loads(job["detail"])["link"] == pull_url
    assert json.loads(job["detail"])["ticket_url"] == "https://github.com/owner/app/issues/31"

    dispatcher = Dispatcher(
        db,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        hooks=(T7Hook(), T8Hook(None)),
    )
    comments: list[Comment] = []

    def post(body: str) -> str | None:
        comments.append(
            Comment(str(300 + len(comments)), "grok-login", body, 1000.0 + len(comments),
                    url=f"{pull_url}#c{len(comments)}")
        )
        pull = replace(ticket(*comments, ticket_id=str(number)), repo="owner/app")
        dispatcher.poll_once(FakeSource(pull))
        with queue.connect() as conn:
            row = conn.execute(
                "SELECT workflow_state FROM ticket_state WHERE ticket_id = ?", (str(number),)
            ).fetchone()
        return None if row is None else row[0]

    session = NaturalIntakeSession(_factory_config())
    asked = session.classify_and_post(
        "Improve onboarding.", [ClassifiedQuestion("Allow guests?", "product")]
    )
    assert post(asked.comment) == "waiting-max"
    answered = session.answer_all_product_questions([("Allow guests?", "No.")])
    assert post(answered.comment) == "questions"
    artifacts = session.promote(
        goal="Improve onboarding",
        acceptance_criteria=["Owners can onboard"],
        likely_files=["app.py"],
        risk="low",
        done_evidence="pytest passes",
    )
    post(artifacts.brief_comment)
    post(artifacts.go_comment)
    claimed = queue.claim("claude")
    assert claimed is not None
    assert (claimed.request.action, claimed.request.ticket_id) == ("build", str(number))
