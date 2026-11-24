"""Phase 0 (PR 0.1) correctness regression tests."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib import lane_limit
from lib.dispatch.types import RetryableStateError
from lib.dispatcher import CIResult, Comment, Dispatcher, TicketRef, TrackedTicket
from lib.factory_md import parse_factory_md
from lib.intake_live import LINEAR_HISTORY_QUERY, LINEAR_INTAKE_QUERY, LinearIntakeClient
from lib.orch_yml import load_orch_yml, refused_harness_findings
from lib.poll_source import (
    LINEAR_COMMENTS_QUERY,
    LINEAR_ISSUE_QUERY,
    HTTPResult,
    LivePollSource,
)
from lib.queue import SQLiteJobQueue
from tests.qa_support import HEAD_A, REPO, TICKET, Harness, qa_config, qa_report, ready, scenarios, verdict
from tests.test_dispatcher import (
    ANSWERS,
    GO,
    QUESTIONS,
    READY,
    TRUSTED_LOGINS,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    factory,
)
from tests.test_feeder import (
    FACTORY_MD,
    GITHUB_ORCH,
    FakeGitHub,
    issue as feeder_issue,
    make_feeder,
)
from tests.test_intake_live import linear_node
from tests.test_lifecycle import HEADS, Flow
from tests.test_live_poll import (
    FACTORY as POLL_FACTORY,
    FakeGh,
    config as poll_config,
    linear_issue as poll_linear_issue,
)
from tests.test_qa_cloud import Lab, doctor_failed
from tests.test_qa_gate_table import FAIL_BODY, _lane_doctor_fails, _post_newest_qa_comment
from tests.test_rebase_review import handed_off_with_falling_blockers, rebase_then_ready, revise_with


# ---------------------------------------------------------------------------
# 1. QA-parked ticket resume via answers and /go
# ---------------------------------------------------------------------------


def test_qa_parked_ticket_resumes_via_answers_and_ships(tmp_path: Path) -> None:
    lab = _lane_doctor_fails(tmp_path, "APPROVE")
    run = lab.run
    _post_newest_qa_comment(lab, "q1")
    _post_newest_qa_comment(lab, "q2")
    assert run.rows("SELECT workflow_state, paused_action FROM ticket_state") == [
        ("waiting-max", "qa")
    ]

    # Fix the environment and resume via kind=answers.
    lab.broken.clear()
    run.post(
        "ans-1",
        "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator -->\nQA env fixed; rerun QA.",
    )
    run.poll(2)

    assert run.rows("SELECT workflow_state, paused_action FROM ticket_state") == [
        ("review", None)
    ]
    assert run.rows(
        "SELECT state, reason FROM qa_runs WHERE head = ? AND arm = 'primary' ORDER BY rowid",
        HEAD_A,
    ) == [
        ("cancelled", "resumed-qa"),
        ("cancelled", "resumed-qa"),
        ("queued", None),
    ]
    assert run.rows("SELECT state FROM jobs WHERE action = 'qa' ORDER BY rowid") == [
        ("done",),
        ("done",),
        ("queued",),
    ]

    # Resumed QA lane runs, posts PASS, and ticket ships cleanly.
    lab.tick(4)
    body = list(lab.posted.values())[-1][1]
    assert "result=PASS" in body
    run.post("q3", body)
    run.poll(3)

    assert run.actions.handoffs == [HEAD_A]
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("handed-off",)]


def test_qa_parked_ticket_resumes_via_slash_go_and_resets_env_retry_budget(
    tmp_path: Path,
) -> None:
    lab = _lane_doctor_fails(tmp_path, "APPROVE")
    run = lab.run
    _post_newest_qa_comment(lab, "q1")
    _post_newest_qa_comment(lab, "q2")
    assert run.rows("SELECT workflow_state, paused_action FROM ticket_state") == [
        ("waiting-max", "qa")
    ]

    # Resume via /go from approver while doctor is STILL broken:
    # must get a fresh EnvFailed retry budget (retry once, park on second).
    run.post("go-1", "/go", author="maxharris1")
    run.poll(2)
    assert run.rows("SELECT workflow_state, paused_action FROM ticket_state") == [
        ("review", None)
    ]

    _post_newest_qa_comment(lab, "q3")
    assert run.rows("SELECT workflow_state, paused_action FROM ticket_state") == [
        ("review", None)
    ]
    assert len(run.qa_jobs()) == 4

    _post_newest_qa_comment(lab, "q4")
    assert run.rows("SELECT workflow_state, paused_action FROM ticket_state") == [
        ("waiting-max", "qa")
    ]


# ---------------------------------------------------------------------------
# 2. SQLite lock raises RetryableStateError for all comment kinds; bugs re-raise
# ---------------------------------------------------------------------------


def test_sqlite_lock_in_comments_raises_retryable_for_all_kinds(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=lambda: 1000.0)
    dispatcher = Dispatcher(
        db,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
    )

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    dispatcher._t5_answers = locked  # type: ignore[method-assign]
    ticket = TrackedTicket(
        repo="owner/app",
        ticket_id="42",
        factory=factory(),
        comments=(
            Comment(id="a1", author="grok-login", body=ANSWERS, created_at=10.0),
        ),
        current_head=HEAD_A,
        ci={HEAD_A: CIResult("success")},
        risk="high",
    )

    with pytest.raises(RetryableStateError):
        dispatcher.process(ticket)

    with queue.connect() as conn:
        handled = conn.execute("SELECT * FROM handled_comments").fetchall()
    assert handled == []


def test_programming_error_in_comment_dispatch_is_not_swallowed(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=lambda: 1000.0)
    dispatcher = Dispatcher(
        db,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
    )

    def buggy(*args, **kwargs):
        raise KeyError("missing_column")

    dispatcher._t5_answers = buggy  # type: ignore[method-assign]
    ticket = TrackedTicket(
        repo="owner/app",
        ticket_id="42",
        factory=factory(),
        comments=(
            Comment(id="a1", author="grok-login", body=ANSWERS, created_at=10.0),
        ),
        current_head=HEAD_A,
        ci={HEAD_A: CIResult("success")},
        risk="high",
    )

    with pytest.raises(KeyError, match="missing_column"):
        dispatcher.process(ticket)

    with queue.connect() as conn:
        handled = conn.execute("SELECT * FROM handled_comments").fetchall()
    assert handled == []


# ---------------------------------------------------------------------------
# 3. _redrive_handoffs respects qa.gate: block
# ---------------------------------------------------------------------------


def test_redrive_handoff_respects_blocking_qa_gate(tmp_path: Path) -> None:
    run = Harness(tmp_path, replace(qa_config(), gate="block"))
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    run.poll()

    # Simulate an eligible failed ship_intent while QA is still queued/running.
    with run.queue.connect() as conn:
        conn.execute(
            """
            INSERT INTO ship_intents
                (repo, ticket_id, comment_id, policy, head, status, redrive)
            VALUES (?, ?, 'v1', 'handoff', ?, 'failed', 'eligible')
            """,
            (REPO.lower(), TICKET, HEAD_A),
        )
        conn.commit()

    # While QA is running, redrive must NOT ship and keeps redrive eligible.
    run.poll()
    assert run.actions.handoffs == []
    assert run.rows("SELECT redrive FROM ship_intents WHERE comment_id = 'v1'") == [
        ("eligible",)
    ]

    # When QA fails, redrive must NOT ship and spends the redrive.
    run.post("q1", qa_report(HEAD_A, "FAIL", body=FAIL_BODY, cause="product"))
    run.poll()
    assert run.actions.handoffs == []
    assert run.rows("SELECT redrive FROM ship_intents WHERE comment_id = 'v1'") == [
        ("spent",)
    ]


# ---------------------------------------------------------------------------
# 4. rule_engine=on does not bypass #1408 rebase-review cap refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory_change",
    [
        {"auto_extra_rounds": 1, "rule_engine": "on"},
        {"stop_on": "convergence", "rule_engine": "on"},
    ],
    ids=["rounds-rule-engine-on", "convergence-rule-engine-on"],
)
def test_rule_engine_on_does_not_grant_extra_round_on_rebase_review(
    tmp_path: Path, factory_change: dict
) -> None:
    flow = Flow(tmp_path)
    flow.factory = replace(flow.factory, **factory_change)
    handed_off_with_falling_blockers(flow)
    assert rebase_then_ready(flow, HEADS[4]) == "queued-review"
    flow.finish("codex", "revise-3/3")

    assert revise_with(flow, 3, ["f-9"]) == "capped-review"
    row = flow.state()
    assert (row["workflow_state"], row["review_round"], row["cap_extra"]) == ("capped", 3, 0)


# ---------------------------------------------------------------------------
# 5. fable model prefix maps to Anthropic family
# ---------------------------------------------------------------------------


def test_fable_model_prefix_is_anthropic_family() -> None:
    same_family = """---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: cursor
  model: fable-1
review:
  rounds:
    low: 1
    high: 3
  verify: none
ship: merge
skills: []
---
"""
    parsed, findings = parse_factory_md(same_family)
    assert parsed is None
    assert any("same family" in f.message.lower() or "anthropic" in f.message.lower() for f in findings)

    cross_family = same_family.replace("harness: claude", "harness: codex")
    parsed_ok, findings_ok = parse_factory_md(cross_family)
    assert parsed_ok is not None, findings_ok


# ---------------------------------------------------------------------------
# 6. refused_harnesses enforced in check-config, T1, T2, and Feeder
# ---------------------------------------------------------------------------


def test_refused_harnesses_enforced_in_config_dispatcher_and_feeder(tmp_path: Path) -> None:
    orch_text = GITHUB_ORCH + "\nrefused_harnesses: [claude]\n"
    orch_path = tmp_path / "orch.yml"
    orch_path.write_text(orch_text, encoding="utf-8")
    cfg, findings = load_orch_yml(orch_path, allow_placeholders=True)
    assert cfg is not None, findings

    # check-config finding
    parsed_factory, _ = parse_factory_md(FACTORY_MD)
    assert parsed_factory is not None
    refused_findings = refused_harness_findings(cfg, parsed_factory)
    assert len(refused_findings) == 1
    assert refused_findings[0].severity == "error"
    assert "claude" in refused_findings[0].message

    # Dispatcher T1 rejects go with refused builder/reviewer harness
    db = tmp_path / "disp.db"
    queue = SQLiteJobQueue(db, clock=lambda: 1000.0)
    dispatcher = Dispatcher(
        db,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        refused_harnesses=cfg.refused_harnesses,
    )
    ticket = TrackedTicket(
        repo="owner/app",
        ticket_id="42",
        factory=factory(),
        comments=(
            Comment(id="g1", author="grok-login", body=GO.format(state="building"), created_at=1.0),
        ),
        current_head=HEAD_A,
        ci={HEAD_A: CIResult("success")},
        risk="low",
    )
    dispatcher.process(ticket)
    with queue.connect() as conn:
        outcomes = conn.execute("SELECT comment_id, outcome FROM handled_comments").fetchall()
        jobs = conn.execute("SELECT * FROM jobs").fetchall()
    assert [(r["comment_id"], r["outcome"]) for r in outcomes] == [
        ("g1", "rejected-go-refused-harness")
    ]
    assert jobs == []

    # Dispatcher T2 rejects ready when reviewer/builder is refused (e.g. ready_starts_review)
    ready_ticket = TrackedTicket(
        repo="owner/app",
        ticket_id="43",
        factory=factory(),
        comments=(
            Comment(id="r1", author="claude-login", body=READY.format(head=HEAD_A), created_at=2.0),
        ),
        current_head=HEAD_A,
        ci={HEAD_A: CIResult("success")},
        risk="low",
        ready_starts_review=True,
    )
    dispatcher.process(ready_ticket)
    with queue.connect() as conn:
        r_out = conn.execute(
            "SELECT outcome FROM handled_comments WHERE ticket_id = '43'"
        ).fetchone()
    assert r_out is not None and r_out["outcome"] == "rejected-ready-refused-harness"

    # Feeder refuses to open PR for refused harness
    gh = FakeGitHub()
    gh.issues["owner/app"] = [feeder_issue(7)]
    feeder, fqueue, _, _ = make_feeder(tmp_path / "feeder", orch=orch_text, github=gh)
    report = feeder.feed_once()
    assert report.fed == ()
    assert report.blocked == 1
    assert gh.pulls == {}


# ---------------------------------------------------------------------------
# 7. Feeder _feed exception recovery & lane_limit pause in _room()
# ---------------------------------------------------------------------------


def test_feeder_exception_drops_path_holds(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.issues["owner/app"] = [
        feeder_issue(10, body="Touches: lib/a.py\n"),
        feeder_issue(11, body="Touches: lib/a.py\n"),
    ]
    gh.fail["pull"] = 1  # fail first open_draft_pull, succeed on second
    # Default FACTORY_MD sets feeder.max_holders_per_path: 1.
    feeder, fqueue, _, _ = make_feeder(tmp_path, box=2, github=gh)
    report = feeder.feed_once()

    # Candidate 10 failed -> its path hold on lib/a.py is dropped so
    # Candidate 11 feeds in the same cycle (while box=1 still reserves error retry room).
    assert report.errors == 1
    assert len(gh.pulls) == 1
    assert any("11" in pull["title"] or "#11" in pull["body"] for pull in gh.pulls.values())


def test_feeder_room_is_zero_when_harness_paused_by_lane_limit(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.issues["owner/app"] = [feeder_issue(20)]
    now = [10_000.0]
    feeder, fqueue, _, _ = make_feeder(tmp_path, box=2, github=gh, now=now)
    with fqueue.connect() as conn:
        conn.execute(lane_limit.DDL)
        conn.execute(
            f"""
            INSERT INTO {lane_limit.TABLE}
                (recipient, harness, bot, limited_at, until, source, job_id)
            VALUES ('claude', 'claude', 'claude-bot', ?, ?, 'message', 'j-1')
            """,
            (now[0], int(now[0] + 600.0)),
        )
        conn.commit()
    feeder.feed_once()
    assert gh.pulls == {}


# ---------------------------------------------------------------------------
# 8. Linear pagination: history > 25 (intake_live) and comments > 250 (poll_source)
# ---------------------------------------------------------------------------


def test_linear_intake_paginates_history_beyond_25_events() -> None:
    node = linear_node(99)
    node["creator"] = {"id": "creator-id"}
    node["history"] = {
        "nodes": [
            {"createdAt": "2026-11-12T22:00:00Z", "actor": {"id": "bot"}, "toState": None}
        ],
        "pageInfo": {"hasNextPage": True, "endCursor": "hist-cursor-1"},
    }
    queries: list[dict] = []

    class PagedHistorySource:
        def _linear_request(self, document: dict) -> dict:
            queries.append(document)
            if document["query"] == LINEAR_INTAKE_QUERY:
                return {
                    "data": {
                        "issues": {
                            "nodes": [node],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            assert document["query"] == LINEAR_HISTORY_QUERY
            assert document["variables"] == {"issue": node["id"], "after": "hist-cursor-1"}
            return {
                "data": {
                    "issue": {
                        "history": {
                            "nodes": [
                                {
                                    "createdAt": "2026-11-12T20:00:00Z",
                                    "actor": {"id": "max"},
                                    "toState": {"name": "In Progress"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }

    found = LinearIntakeClient(PagedHistorySource(), {"proj-1": "acme/app"}).linear_intake(
        frozenset({"proj-1"}), "In Progress", {}
    )
    assert [(item.key, item.applied_by) for item in found] == [("AUT-99", "max")]
    assert len(queries) == 2


def test_linear_poll_paginates_comments_beyond_250() -> None:
    gh = FakeGh(POLL_FACTORY.format(tickets="linear", ship="handoff"))
    issue_doc = poll_linear_issue()
    issue_doc["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "c-page-2"}
    requests: list[dict] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        req = json.loads(body)
        requests.append(req)
        if req["query"] == LINEAR_ISSUE_QUERY:
            return HTTPResult(200, json.dumps({"data": {"issue": issue_doc}}))
        assert req["query"] == LINEAR_COMMENTS_QUERY
        assert req["variables"] == {"id": issue_doc["id"], "after": "c-page-2"}
        return HTTPResult(
            200,
            json.dumps(
                {
                    "data": {
                        "issue": {
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "c-251",
                                        "body": "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator -->\nPage 2.",
                                        "createdAt": "2026-11-12T12:05:00Z",
                                        "editedAt": None,
                                        "url": "https://linear.app/acme/issue/AUT-42#comment-251",
                                        "user": {"id": "linear-grok"},
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            ),
        )

    source = LivePollSource(
        poll_config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=transport,
    )
    tickets = source.tracked_tickets((TicketRef("acme/widgets", "AUT-42", "linear"),))
    assert len(tickets) == 1
    assert [c.id for c in tickets[0].comments] == ["comment-id", "c-251"]
    assert len(requests) == 2


# ---------------------------------------------------------------------------
# 9. Round-1 Codex review regression suite (f-1 through f-8)
# ---------------------------------------------------------------------------


def test_feeder_retains_holds_and_room_after_go_published_sqlite_error(tmp_path: Path) -> None:
    from lib.handoff import parse_comment
    from tests.test_feeder import FeederSource, dispatcher_for

    gh = FakeGitHub()
    gh.issues["owner/app"] = [
        feeder_issue(n, created_at=float(n), body="Touches: lib/x.py\n") for n in (1, 2)
    ]
    feeder, fqueue, _, _ = make_feeder(tmp_path, box=1, github=gh)
    record, failed = feeder._record, [False]

    def save(candidate, **values):
        if "go_comment_id" in values and not failed[0]:
            failed[0] = True
            raise sqlite3.OperationalError("database is locked")
        return record(candidate, **values)

    feeder._record = save  # type: ignore[method-assign]
    feeder.feed_once()
    pulls = [
        n
        for (_, n), cs in gh.posted.items()
        if any(parse_comment(c.body).as_dict().get("kind") == "go" for c in cs)
    ]
    dispatcher, source = dispatcher_for(tmp_path, fqueue), FeederSource(gh)
    for n in pulls:
        dispatcher.process(source._ticket(TicketRef("owner/app", str(n), "github")))
    builds = sorted(j.request.ticket_id for j in fqueue.outstanding() if j.request.action == "build")
    assert len(pulls) == 1 and len(builds) == 1


def test_refused_harnesses_blocks_refused_reviewer_intake_and_device_pool_claim(tmp_path: Path) -> None:
    from lib.device_pool import DevicePool
    from lib.orch_yml import ProjectPins
    from lib.queue import JobRequest

    d1 = tmp_path / "reviewer"
    d1.mkdir()
    gh = FakeGitHub()
    gh.issues["owner/app"] = [feeder_issue(85)]
    feeder, _, _, _ = make_feeder(d1, orch=GITHUB_ORCH + "\nrefused_harnesses: [codex]\n", github=gh)
    report = feeder.feed_once()
    assert not report.fed and not gh.pulls and report.blocked == 1

    d2 = tmp_path / "pool"
    d2.mkdir()
    feeder2, fqueue2, _, _ = make_feeder(d2, orch=GITHUB_ORCH + "\nrefused_harnesses: [claude]\n")
    pool = DevicePool(fqueue2, feeder2.machines, pins=ProjectPins(d2 / "orch.yml"))
    fqueue2.enqueue(
        JobRequest(
            repo="owner/app",
            ticket_id="85",
            action="build",
            recipient="claude",
            source_comment_id="go",
            timeout_seconds=60,
        ),
        dedupe_key="probe-refused",
    )
    assert pool.claim("claude") is None


def test_redrive_handoffs_executes_blocking_qa_fix_action(tmp_path: Path) -> None:
    from lib.github_actions import GitHubAdapterError

    r = Harness(tmp_path, replace(qa_config(), gate="report"))
    r.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    r.poll()
    r.finish("codex", "v1")
    original = r.actions.handoff

    def unavailable(*args, **kwargs):
        exc = GitHubAdapterError("transient handoff failure")
        exc.write_confirmed = False
        raise exc

    r.actions.handoff = unavailable  # type: ignore[method-assign]
    r.post("v1", verdict(HEAD_A, "APPROVE"))
    r.poll()
    r.actions.handoff = original  # type: ignore[method-assign]
    r.thread.base = replace(
        r.thread.base,
        factory=replace(r.thread.base.factory, qa=replace(qa_config(), gate="block")),
    )
    r.post("q1", qa_report(HEAD_A, "FAIL", body=FAIL_BODY, cause="product"))
    r.poll(3)
    assert r.rows("SELECT action FROM jobs WHERE action='fix'") == [("fix",)]
    assert r.rows("SELECT workflow_state FROM ticket_state") == [("fixing",)]


def test_factory_md_lenient_ci_and_qa_preserve_valid_fields_on_unknown_keys() -> None:
    from lib.factory_md import _ci, _qa

    ci_findings: list = []
    _ci({"ci": {"branches": ["main"], "future": 1, "local_gate": {"command": "true", "timeout_seconds": 60, "extra": 1}}}, ci_findings, "warning")
    assert ci_findings and all(item.severity == "warning" for item in ci_findings), ci_findings

    qa_data = {
        "enabled": True,
        "gate": "block",
        "profile": "none",
        "runners": ["claude-cloud"],
        "claude_cloud": {"environment": "test", "extra_runner_key": True},
        "lane": {"boot": "true", "base_url": "http://localhost:8080", "extra_lane_key": 1},
        "fixtures": {"extra_fixture_key": 1},
        "evidence": {"extra_evidence_key": 1},
        "future": 1,
    }
    parsed, findings = _qa({"qa": qa_data}, False)
    assert parsed is not None and parsed.gate == "block"
    assert findings and all(item.severity == "warning" for item in findings), findings


def test_first_cap_grant_repeat_site_includes_auto_extra_comments(tmp_path: Path) -> None:
    from lib.dispatch.core import DispatcherBase
    from lib.dispatch.types import Registration
    from tests.test_first_cap_grant import F3, engine_flow, to_last_round
    from tests.test_repeat import respond
    from tests.test_unsatisfiable_criterion import revise

    original_reg = DispatcherBase._registration
    DispatcherBase._registration = lambda *a, **k: Registration.REGISTERED  # type: ignore[method-assign]
    try:
        f = engine_flow(tmp_path)
        f.factory = replace(f.factory, auto_extra_rounds=1)
        to_last_round(f)
        revise(f, "3/3", "revise-3", body=F3)
        respond(f, 3, "Fixed f-3.\n")
        revise(f, "4/4", "revise-4", body=F3)
        assert f.state()["workflow_state"] == "waiting-max"
    finally:
        DispatcherBase._registration = original_reg  # type: ignore[method-assign]


def test_brief_skills_includes_kit_deslop_and_inline_avoids_duplicate_ac_prefix() -> None:
    from lib.brief.common import TicketText
    from lib.brief.inline import ticket_section
    from lib.brief.skills import builder_files
    from lib.pstack_pin import ROOT

    paths = [item.path for item in builder_files("build", None)]
    assert "skills/cursor-team-kit/deslop/SKILL.md" in paths

    text = TicketText(
        brief=None,
        brief_source="",
        criteria_text=None,
        criteria_source="ticket #1",
        criteria=(("ac-1", "ac-1: Already numbered."), ("ac-2", "Plain criterion.")),
        fields={},
    )
    rendered = ticket_section(text, review=False, criteria_url="https://example.test/1")
    assert rendered is not None
    assert "ac-1: Already numbered." in rendered
    assert "ac-1: ac-1:" not in rendered
    assert "ac-2: Plain criterion." in rendered

    builder_skill = (ROOT / "skills" / "factory" / "factory-builder" / "SKILL.md").read_text(encoding="utf-8")
    assert "-trail.tsv" not in builder_skill
