"""Tests for E4-1: Mission Control daily cockpit (Needs Max / Product Inbox & Visual QA Gate)."""

from __future__ import annotations

import http.client
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from lib.dashboard import collect_snapshot, render_pages, write_snapshot_pages
from lib.dashboard_page import render_static_assets
from lib.dashboard_pages.cockpit import _needs_max, inbox_tickets, ticket_cockpit_html
from lib.dashboard_pages.qa_evidence import visual_media
from lib.dashboard_pages.ticket_detail import ticket_view
from lib.dispatch.questions import extract_open_questions
from lib.dispatcher import Comment, Dispatcher
from lib.handoff import parse_comment
from lib.max_actions import ACTIONS, applicable, wait_facts, wait_of
from lib.orch_yml import Approvals
from lib.qa_evidence import record_evidence_dir
from lib.queue import SQLiteJobQueue
from tests.qa_support import (
    HEAD_A,
    HEAD_B,
    REPO,
    TICKET,
    Harness,
    qa_config,
    qa_report,
    ready,
    scenarios,
    verdict,
)
from tests.test_dashboard_http import (
    MAX_TOKEN,
    Client,
    _forge,
    actions_site,
    fake_github,
    serve_actions,
    site,
)
from tests.test_dispatcher import (
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    handled_outcome,
    job_count,
    seed_review_job,
    ticket,
    workflow_state,
)

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "deploy" / "factory-dashboard-http.py"
_spec = importlib.util.spec_from_file_location("factory_dashboard_http_cockpit", SERVER_PATH)
assert _spec is not None and _spec.loader is not None
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

RUN_ID = "0123456789abcdef0123456789abcdef"
NOW = 1763500000.0
AT = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)
MAX_TRUST = {
    **TRUSTED_LOGINS,
    "github": {**TRUSTED_LOGINS["github"], "maxharris1": frozenset({"grok-bot"})},
}


def _make_lane_evidence(root: Path, run_id: str = RUN_ID) -> Path:
    run_folder = root / run_id
    lane = run_folder / "lane-1"
    shot = lane / "qa-1" / "01-landing-desktop-1440x900.png"
    clip = lane / "qa-1" / "walkthrough.webm"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")
    clip.write_bytes(b"\x1aE\xdf\xa3fake-webm")
    (lane / "result.json").write_text(
        json.dumps({"scenarios": [{"id": "qa-1", "result": "verified"}]}),
        encoding="utf-8",
    )
    (lane / "manifest.json").write_text(
        json.dumps({
            "files": [
                {"path": "qa-1/01-landing-desktop-1440x900.png"},
                {"path": "qa-1/walkthrough.webm"},
                {"path": "../escape.png"},
            ]
        }),
        encoding="utf-8",
    )
    return run_folder


def _body(action: str, words: str = "Looks good.", *, head: str | None = HEAD_A) -> str:
    facts = server_module.ActFacts(
        REPO, TICKET, int(TICKET), head, None, server_module.words_block(words, AT)
    )
    return server_module.ACTIONS[action].body(facts)


def test_visual_qa_review_wait_offers_approve_changes_and_rerun_actions() -> None:
    wait = wait_of(
        "awaiting-ok",
        False,
        approve_comment_id="55",
        hold_reasons="visual-qa-review",
    )
    assert wait is not None
    assert wait.why == "visual-qa-review"
    assert wait.comment_id == "55"
    assert applicable(wait, hand_off=True) == [
        "ok",
        "request-changes",
        "rerun-qa",
        "close",
        "hand-off",
    ]
    plain = wait_of("awaiting-ok", False, approve_comment_id="55", hold_reasons="label")
    assert plain is not None and plain.why == "awaiting-ok"
    assert applicable(plain, hand_off=True) == ["ok", "close", "hand-off"]


def test_new_cockpit_actions_parse_to_expected_handoff_headers() -> None:
    rc = parse_comment(_body("request-changes", "Fix header spacing.", head=HEAD_A))
    assert rc.as_dict() == {
        "kind": "go",
        "from": "approver",
        "role": "approver",
        "state": "building",
    }
    assert "Visual QA changes requested" in rc.body
    rr = parse_comment(_body("rerun-qa", "Flaky modal animation.", head=HEAD_A))
    assert rr.as_dict() == {"kind": "retry", "from": "approver", "role": "approver"}
    assert "**Re-run QA**" in rr.body


def test_extract_open_questions_prioritizes_product_questions_and_options() -> None:
    classified = (
        "<!-- orch:v1 kind=questions from=claude-bot role=builder -->\n\n"
        "- [observable] Does foo.py exist?\n"
        "- [product] Which Gemini auth mode should we use?\n"
        "  - Google sign-in OAuth\n"
        "  - API key\n"
        "- [default-able] Retry backoff seconds?\n"
        "  - Default: 5s\n"
    )
    extracted = extract_open_questions(classified)
    assert extracted == [
        {
            "text": "Which Gemini auth mode should we use?",
            "options": ["Google sign-in OAuth", "API key"],
        }
    ]
    prose = (
        "<!-- orch:v1 kind=questions from=claude-bot role=builder -->\n\n"
        "Should Cursor run as a build harness?\n"
        "- Yes, allow Cursor\n"
        "- No, review only\n"
    )
    assert extract_open_questions(prose) == [
        {
            "text": "Should Cursor run as a build harness?",
            "options": ["Yes, allow Cursor", "No, review only"],
        }
    ]


def test_visual_media_filters_traversals_and_returns_safe_qa_urls(tmp_path: Path) -> None:
    run_folder = _make_lane_evidence(tmp_path)
    media = visual_media(run_folder, RUN_ID)
    assert len(media) == 2
    assert media[0]["kind"] == "image"
    assert media[0]["url"] == f"/qa/runs/{RUN_ID}/lane-1/qa-1/01-landing-desktop-1440x900.png"
    assert media[1]["kind"] == "video"
    assert media[1]["url"] == f"/qa/runs/{RUN_ID}/lane-1/qa-1/walkthrough.webm"


def test_snapshot_carries_redact_proof_visual_qa_media_and_redacted_questions(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    queue = SQLiteJobQueue(db, clock=lambda: NOW)
    dispatcher = Dispatcher(
        db,
        queue,
        trusted_logins=MAX_TRUST,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        approvals=Approvals(logins=frozenset({"maxharris1"})),
    )
    evidence_dir = _make_lane_evidence(tmp_path / "qa-evidence")
    with queue.connect() as conn:
        conn.execute(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) VALUES (?, ?, 'github', ?)",
            (REPO, "42", NOW - 100),
        )
        conn.execute(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) VALUES (?, ?, 'github', ?)",
            (REPO, "43", NOW - 90),
        )
        conn.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state, paused_question_id, paused_question_text) "
            "VALUES (?, '43', 'waiting-max', '77', ?)",
            (
                REPO.lower(),
                "- [product] Use OAuth or secret_token=ghp_123456789012345678901234567890123456?\n"
                "  - OAuth\n  - Key\n",
            ),
        )
        conn.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) VALUES (?, '42', 'awaiting-ok')",
            (REPO.lower(),),
        )
        conn.execute(
            "INSERT INTO ship_holds (repo, ticket_id, head, approver, reasons, approve_comment_id, held_at) "
            "VALUES (?, '42', ?, 'maxharris1', 'visual-qa-review', '55', ?)",
            (REPO.lower(), HEAD_A, NOW - 50),
        )
        conn.execute(
            "INSERT INTO qa_runs (run_id, repo, ticket_id, head, round, attempt, scope, runner, arm, "
            "job_id, state, result, started_at, finished_at, evidence_dir, visual) "
            "VALUES (?, ?, '42', ?, 1, 1, 'full', 'local-docker', 'primary', 'j-qa', 'reported', 'PASS', "
            "?, ?, ?, 1)",
            (RUN_ID, REPO.lower(), HEAD_A, NOW - 80, NOW - 60, str(evidence_dir)),
        )
        conn.commit()
    del dispatcher

    snap = collect_snapshot(db, log, now=NOW)
    by_id = {row["ticket_id"]: row for row in snap["tickets"]}

    # Visual QA ticket: wait.why == visual-qa-review, inline media preserved past _scrub()
    t42 = by_id["42"]
    assert t42["wait"] == {"why": "visual-qa-review", "comment_id": "55", "detail": "visual-qa-review"}
    assert t42["actions"] == ["ok", "request-changes", "rerun-qa", "close", "hand-off"]
    assert t42["visual_qa"]["run"] == [RUN_ID[:16], RUN_ID[16:]]
    assert len(t42["visual_qa"]["media"]) == 2
    assert "".join(t42["visual_qa"]["media"][0]["path_parts"]) == "qa-1/01-landing-desktop-1440x900.png"
    assert "[redacted]" not in json.dumps(t42["visual_qa"])

    # Question ticket: redacted open questions preserved
    t43 = by_id["43"]
    assert t43["wait"] == {"why": "question", "comment_id": "77", "detail": None}
    assert len(t43["questions"]) == 1
    assert "ghp_123456" not in t43["questions"][0]["text"]
    assert t43["questions"][0]["options"] == ["OAuth", "Key"]

    # Server-rendered pages contain Needs Max panel, inline <img>/<video>, and zero inline scripts
    pages = render_pages(snap)
    assert 'id="needs-max"' in pages["index.html"]
    assert f'/qa/runs/{RUN_ID}/lane-1/qa-1/01-landing-desktop-1440x900.png' in pages["index.html"]
    assert f'/qa/runs/{RUN_ID}/lane-1/qa-1/walkthrough.webm' in pages["index.html"]
    assert "<script>" not in pages["index.html"]
    assert "OAuth" in pages["index.html"]

    # Ticket detail server-side cockpit renders on waiting tickets
    detail_html = ticket_view(snap, f"{REPO}#42")
    assert 'id="ticket-cockpit"' in detail_html
    assert f'/qa/runs/{RUN_ID}/lane-1/qa-1/01-landing-desktop-1440x900.png' in detail_html


def test_dispatcher_request_changes_and_rerun_qa_end_to_end(tmp_path: Path) -> None:
    qa = replace(qa_config(), gate="block", visual_hold=True)
    run = Harness(tmp_path, qa, ship="merge")

    # Reach visual-qa-review awaiting-ok hold
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    [(run_id,)] = run.rows("SELECT run_id FROM qa_runs WHERE head = ? AND arm = 'primary'", HEAD_A)
    folder = _make_lane_evidence(tmp_path / "qa-evidence", run_id)
    with run.queue.connect() as conn:
        record_evidence_dir(conn, run_id, folder)
        conn.commit()
    run.post("q1", qa_report(HEAD_A, "PASS", body="| qa-1 | Verified | ok |\n"))
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    run.finish("codex", "v1")
    run.poll()
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    assert run.rows("SELECT reasons FROM ship_holds") == [("visual-qa-review",)]

    # 1) Re-run QA keeps ticket in awaiting-ok and queues a fresh primary QA run at HEAD_A
    run.post("rr-1", _body("rerun-qa", "Re-check mobile drawer.", head=HEAD_A))
    run.poll()
    assert run.rows("SELECT outcome FROM handled_comments WHERE comment_id = 'rr-1'") == [("queued-rerun-qa",)]
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]
    qa_rows = run.rows("SELECT state, reason, attempt FROM qa_runs WHERE head = ? ORDER BY rowid", HEAD_A)
    assert qa_rows == [("cancelled", "rerun-qa", 1), ("queued", None, 2)]

    # Duplicate active re-run QA while one is already queued is cleanly refused
    run.post("rr-2", _body("rerun-qa", "Again.", head=HEAD_A))
    run.poll()
    assert run.rows("SELECT outcome FROM handled_comments WHERE comment_id = 'rr-2'") == [("rejected-retry-job-active",)]

    # 2) Request Changes clears ship_holds, supersedes v1, cancels queued QA, moves to building + queues builder
    run.post("rc-1", _body("request-changes", "Align the primary CTA button.", head=HEAD_A))
    run.poll()
    assert run.rows("SELECT outcome FROM handled_comments WHERE comment_id = 'rc-1'") == [("queued-hold-changes",)]
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("building",)]
    assert run.rows("SELECT 1 FROM ship_holds") == []
    assert run.rows("SELECT outcome FROM handled_comments WHERE comment_id = 'v1'") == [("superseded-hold-changes",)]
    assert run.rows("SELECT state, reason FROM qa_runs WHERE head = ? ORDER BY rowid DESC LIMIT 1", HEAD_A) == [
        ("cancelled", "hold-changes")
    ]
    assert run.rows("SELECT action, recipient, state FROM jobs WHERE source_comment_id = 'rc-1'") == [
        ("build", "claude", "queued")
    ]

    # Next poll while builder hasn't pushed a new head MUST stay in building (v1 stays superseded)
    run.poll()
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("building",)]


def test_http_api_act_supports_cockpit_actions_and_409_guards_with_audit(
    serve_actions, fake_github, actions_site: Path, tmp_path: Path
) -> None:
    snap = {
        "schema": "factory-dashboard/v1",
        "tickets": [
            {
                "repo": "maxharris1/factory",
                "ticket_id": "42",
                "pr": "42",
                "source": "#42",
                "state": "awaiting-ok",
                "wait": {"why": "visual-qa-review", "comment_id": "55", "detail": "visual-qa-review"},
                "actions": ["ok", "request-changes", "rerun-qa", "close", "hand-off"],
            },
            {
                "repo": "maxharris1/factory",
                "ticket_id": "43",
                "pr": "43",
                "source": "#43",
                "state": "waiting-max",
                "wait": {"why": "question", "comment_id": "77", "detail": None},
                "actions": ["answer", "close"],
            },
        ],
    }
    (actions_site / "status.json").write_text(json.dumps(snap), encoding="utf-8")
    fake_github.heads[("maxharris1/factory", 42)] = HEAD_A
    audit = tmp_path / "actions-audit.jsonl"
    client = serve_actions(audit=audit)
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"

    # CSP on dashboard allows inline img-src/media-src 'self' with strict script-src 'self'
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    csp = resp.getheader("Content-Security-Policy") or ""
    conn.close()
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "img-src 'self'" in csp
    assert "media-src 'self'" in csp

    # Stale head on request-changes -> HTTP 409 head-changed + audited
    status, data = client.post("/api/act", {
        "repo": "maxharris1/factory",
        "ticket_id": "42",
        "action": "request-changes",
        "expect_state": "awaiting-ok",
        "expect_why": "visual-qa-review",
        "head": HEAD_B,
        "words": "Fix spacing",
    })
    assert status == 409
    assert data == {"error": "head-changed", "head": HEAD_A}

    # Matching head on request-changes -> HTTP 201 and posts kind=go
    status, data = client.post("/api/act", {
        "repo": "maxharris1/factory",
        "ticket_id": "42",
        "action": "request-changes",
        "expect_state": "awaiting-ok",
        "expect_why": "visual-qa-review",
        "head": HEAD_A,
        "words": "Fix spacing",
    })
    assert status == 201
    assert "kind=go from=approver role=approver state=building" in fake_github.posts[-1][2]

    # Matching head on rerun-qa -> HTTP 201 and posts kind=retry
    status, data = client.post("/api/act", {
        "repo": "maxharris1/factory",
        "ticket_id": "42",
        "action": "rerun-qa",
        "expect_state": "awaiting-ok",
        "expect_why": "visual-qa-review",
        "head": HEAD_A,
        "words": "Re-run after staging warm-up",
    })
    assert status == 201
    assert "kind=retry from=approver role=approver" in fake_github.posts[-1][2]

    # Stale expect_comment_id on answer -> HTTP 409 already-answered
    status, data = client.post("/api/act", {
        "repo": "maxharris1/factory",
        "ticket_id": "43",
        "action": "answer",
        "expect_state": "waiting-max",
        "expect_why": "question",
        "expect_comment_id": "76",
        "words": "Use OAuth",
    })
    assert status == 409
    assert data == {"error": "already-answered"}

    # Matching expect_comment_id on answer -> HTTP 201
    status, data = client.post("/api/act", {
        "repo": "maxharris1/factory",
        "ticket_id": "43",
        "action": "answer",
        "expect_state": "waiting-max",
        "expect_why": "question",
        "expect_comment_id": "77",
        "words": "Use OAuth",
    })
    assert status == 201
    assert "kind=answers from=approver role=approver" in fake_github.posts[-1][2]

    # Non-approver is rejected with 403
    non_approver = serve_actions(audit=audit)
    _forge(non_approver, "someone-else")
    status, data = non_approver.post("/api/act", {
        "repo": "maxharris1/factory",
        "ticket_id": "42",
        "action": "request-changes",
        "expect_state": "awaiting-ok",
        "expect_why": "visual-qa-review",
        "head": HEAD_A,
        "words": "Nope",
    })
    assert status == 403
    assert data["error"] == "not-approver"

    # Audit log captured both conflicts, rejections, and posted actions
    lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    outcomes = {(row["action"], row["result"]) for row in lines}
    assert ("request-changes", "head-changed") in outcomes
    assert ("request-changes", "posted") in outcomes
    assert ("rerun-qa", "posted") in outcomes
    assert ("answer", "already-answered") in outcomes
    assert ("answer", "posted") in outcomes
    assert ("request-changes", "not-approver") in outcomes


def test_app_js_passes_node_syntax_check_and_has_no_unsafe_dom() -> None:
    assets = render_static_assets({"statechart": None})
    js = assets["app.js"]
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in js
    node = shutil.which("node")
    if node is None:
        return
    proc = subprocess.run(
        [node, "--check"],
        input=js,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


RUN_B = "fedcba9876543210fedcba9876543210"


def _held_visual_db(tmp_path: Path, held_head: str, runs: list[tuple]) -> tuple[Path, Path]:
    db = tmp_path / "orch.db"
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    queue = SQLiteJobQueue(db, clock=lambda: NOW)
    Dispatcher(
        db,
        queue,
        trusted_logins=MAX_TRUST,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        approvals=Approvals(logins=frozenset({"maxharris1"})),
    )
    with queue.connect() as conn:
        conn.execute(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) VALUES (?, '42', 'github', ?)",
            (REPO, NOW - 100),
        )
        conn.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) VALUES (?, '42', 'awaiting-ok')",
            (REPO.lower(),),
        )
        conn.execute(
            "INSERT INTO ship_holds (repo, ticket_id, head, approver, reasons, approve_comment_id, held_at) "
            "VALUES (?, '42', ?, 'maxharris1', 'visual-qa-review', '55', ?)",
            (REPO.lower(), held_head, NOW - 50),
        )
        for at, (run_id, head, scope, reason, evidence_dir, visual) in enumerate(runs):
            conn.execute(
                "INSERT INTO qa_runs (run_id, repo, ticket_id, head, round, attempt, scope, runner, arm, "
                "job_id, state, result, reason, started_at, finished_at, evidence_dir, visual) "
                "VALUES (?, ?, '42', ?, 1, 1, ?, 'local-docker', 'primary', ?, 'reported', 'PASS', ?, ?, ?, ?, ?)",
                (run_id, REPO.lower(), head, scope, None if scope == "none" else f"j-{run_id[:8]}",
                 reason, NOW - 80 + at, NOW - 70 + at,
                 None if evidence_dir is None else str(evidence_dir), visual),
            )
        conn.commit()
    return db, log


@pytest.mark.parametrize(
    "secret_name",
    [
        "Bearer secret123456789012345678901234567890.png",
        "ghp_123456789012345678901234567890123456.png",
        "password=hunter2hunter2hunter2.png",
        "max@example.com.png",
    ],
)
def test_chunked_qa_paths_never_publish_credential_shaped_names(tmp_path: Path, secret_name: str) -> None:
    evidence_dir = _make_lane_evidence(tmp_path / "qa-evidence")
    lane = evidence_dir / "lane-1"
    (lane / "qa-1" / secret_name).write_bytes(b"\x89PNG\r\n\x1a\nfake-png")
    manifest = json.loads((lane / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"].append({"path": f"qa-1/{secret_name}"})
    (lane / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    db, log = _held_visual_db(tmp_path, HEAD_A, [(RUN_ID, HEAD_A, "full", None, evidence_dir, 1)])

    snap = collect_snapshot(db, log, now=NOW)
    [t42] = [row for row in snap["tickets"] if row["ticket_id"] == "42"]
    index = render_pages(snap)["index.html"]

    secret = secret_name.removesuffix(".png")
    tail = secret[-12:]
    assert tail not in json.dumps(snap)
    assert tail not in index
    assert tail not in "".join(
        "".join(entry["path_parts"]) for entry in t42["visual_qa"]["media"]
    )
    assert [
        "".join(entry["path_parts"]) for entry in t42["visual_qa"]["media"]
    ] == ["qa-1/01-landing-desktop-1440x900.png", "qa-1/walkthrough.webm"]


def test_gallery_shows_the_held_head_over_an_older_visual_run(tmp_path: Path) -> None:
    old = _make_lane_evidence(tmp_path / "qa-a", RUN_ID)
    new = _make_lane_evidence(tmp_path / "qa-b", RUN_B)
    db, log = _held_visual_db(
        tmp_path,
        HEAD_B,
        [(RUN_ID, HEAD_A, "full", None, old, 1), (RUN_B, HEAD_B, "full", None, new, 0)],
    )

    snap = collect_snapshot(db, log, now=NOW)
    [t42] = [row for row in snap["tickets"] if row["ticket_id"] == "42"]

    assert t42["visual_qa"]["run"] == [RUN_B[:16], RUN_B[16:]]
    assert f"/qa/runs/{RUN_B}/lane-1/" in render_pages(snap)["index.html"]


def test_gallery_follows_the_held_heads_carried_source(tmp_path: Path) -> None:
    newer = _make_lane_evidence(tmp_path / "qa-newer", RUN_B)
    source = _make_lane_evidence(tmp_path / "qa-src", RUN_ID)
    other = "1111111111111111" * 2
    db, log = _held_visual_db(
        tmp_path,
        HEAD_B,
        [
            (RUN_ID, HEAD_A, "full", None, source, 1),
            (RUN_B, "c" * 40, "full", None, newer, 1),
            (other, HEAD_B, "none", f"carried:{RUN_ID}", None, 0),
        ],
    )

    snap = collect_snapshot(db, log, now=NOW)
    [t42] = [row for row in snap["tickets"] if row["ticket_id"] == "42"]

    assert t42["visual_qa"]["run"] == [RUN_ID[:16], RUN_ID[16:]]
