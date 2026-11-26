from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib import account_doctor
from lib.job_state import REQUEUED_SIGNED_OUT, SIGNED_OUT, signout_end
from lib.queue import JobRequest, SQLiteJobQueue
from lib.telemetry import EventLog, Scrubber, Session, stream_flags
from tests.test_hooks import FakeGrokDelivery, say, setup, ticket
from tests.test_usage_limit import CLAUDE_LIMIT, GO, START, _pool

OAUTH_EXPIRED = "OAuth session expired and could not be refreshed · Please run /login"
CLAUDE_AUTH_FAILED = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": OAUTH_EXPIRED}]},
    "error": "authentication_failed",
}
CLAUDE_RESULT = {"type": "result", "subtype": "success", "is_error": True, "result": OAUTH_EXPIRED}
CODEX_REFRESH = (
    "Your access token could not be refreshed because your refresh token has expired. "
    "Please log out and sign in again."
)
CODEX_PERMANENT = (
    "Your access token could not be refreshed because your refresh token was already used. "
    "Please log out and sign in again.",
    "Your access token could not be refreshed because your refresh token was revoked. "
    "Please log out and sign in again.",
    "Your access token could not be refreshed. Please log out and sign in again.",
    "Your access token could not be refreshed because you have since logged out or signed in to "
    "another account. Please sign in again.",
)
GEMINI_SIGNED_OUT = {
    "type": "result",
    "status": "error",
    "error": {"message": "Error: Please sign in to view available models. Launch the CLI without arguments to sign in."},
}
CODEX_TRANSIENT = {"type": "turn.failed", "error": {"message": "Failed to refresh token: 503 Service Unavailable"}}


def _session(db: Path, tmp_path: Path, job_id: str, harness: str, now: list[float], **placed: str) -> Session:
    return Session(
        connect=lambda: sqlite3.connect(db),
        job_id=job_id,
        bot=f"{harness}-bot",
        repo="owner/repo",
        harness=harness,
        flags=stream_flags([harness]),
        raw_log=EventLog(tmp_path / job_id / "events.jsonl", log=lambda _: None),
        scrub=Scrubber({}),
        log=lambda _: None,
        stdout=io.BytesIO(),
        clock=lambda: now[0],
        **placed,
    )


def _feed(session: Session, *events: dict) -> None:
    session.consume(io.BytesIO(b"".join((json.dumps(e) + "\n").encode() for e in events)))


def _holds(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as connection:
        account_doctor.held(connection)
        return connection.execute(
            "SELECT account, device, since, source, reason FROM signin_holds ORDER BY account, device"
        ).fetchall()


def _events(db: Path, job_id: str) -> list[str]:
    with sqlite3.connect(db) as connection:
        return [str(e) for (e,) in connection.execute(
            "SELECT event FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
        )]


def _jobs(db: Path) -> list[tuple[str, str, int]]:
    with sqlite3.connect(db) as connection:
        return [
            (str(i), str(s), int(a))
            for i, s, a in connection.execute("SELECT id, state, attempt FROM jobs ORDER BY rowid")
        ]


def _build(tmp_path: Path):
    now = [START]
    delivery = FakeGrokDelivery()
    dispatcher, queue = setup(tmp_path, now=now, grok_delivery=delivery)
    _pool(queue)
    dispatcher.process(replace(ticket(say("904", GO, "grok-login", 1)), pr_number="42"))
    return now, delivery, dispatcher, queue, tmp_path / "d.db"


def _run(tmp_path, now, queue, dispatcher, db, *events: dict) -> str:
    job = queue.claim("claude")
    assert job is not None, "the build is claimable"
    session = _session(db, tmp_path, job.id, "claude", now, device="box", account="claude-default")
    _feed(session, *events)
    session.finish(1)
    queue.finish(job.id, owner="claude", handoff_comment_id=None)
    now[0] += 5
    dispatcher.reconcile_jobs()
    return job.id


def _no_retry_rows(db: Path) -> bool:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE dedupe_key LIKE 't6-retry:%'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("harness, event", [
    ("claude", CLAUDE_AUTH_FAILED),
    ("claude", CLAUDE_RESULT),
    ("claude", {**CLAUDE_RESULT, "result": "Not logged in · Please run /login"}),
    ("codex", {"type": "error", "message": "Not logged in"}),
    ("codex", {"type": "error", "message": "unexpected status 401 Unauthorized: Missing bearer"}),
    ("codex", {"type": "turn.failed", "error": {"message": CODEX_REFRESH}}),
    *(("codex", {"type": "turn.failed", "error": {"message": m}}) for m in CODEX_PERMANENT),
    ("gemini", GEMINI_SIGNED_OUT),
    ("gemini", {"type": "error", "message": GEMINI_SIGNED_OUT["error"]["message"]}),
], ids=["claude-error-field", "claude-oauth-result", "claude-not-logged-in", "codex-not-logged-in",
        "codex-401", "codex-refresh-token", "codex-refresh-reused", "codex-refresh-revoked",
        "codex-refresh-unknown", "codex-refresh-account-changed", "gemini-result", "gemini-error"])
def test_detect_signout_reads_each_harness_auth_failure(harness: str, event: dict) -> None:
    assert account_doctor.detect_signout(harness, event)


@pytest.mark.parametrize("harness, event", [
    ("claude", CLAUDE_LIMIT),
    ("claude", {**CLAUDE_RESULT, "is_error": False}),
    ("claude", {**CLAUDE_RESULT, "result": "API Error: 500"}),
    ("claude", {**CLAUDE_AUTH_FAILED, "error": "rate_limit"}),
    ("claude", {"type": "assistant", "message": {"content": [{"type": "text", "text": OAUTH_EXPIRED}]}}),
    ("codex", {"type": "error", "message": "stream disconnected before completion"}),
    ("codex", {"type": "item.completed", "item": {"text": "Not logged in"}}),
    ("codex", CODEX_TRANSIENT),
    ("codex", {"type": "error", "message": "Failed to refresh token: 500 Internal Server Error"}),
    ("cursor", {"type": "error", "message": "stream disconnected before completion"}),
    ("gemini", {**GEMINI_SIGNED_OUT, "error": {"message": "Quota exceeded for this model. Try again later."}}),
    ("gemini", {"type": "message", "role": "assistant", "content": GEMINI_SIGNED_OUT["error"]["message"]}),
    ("unknown", {"type": "error", "message": "Not logged in"}),
], ids=["claude-usage-limit", "claude-not-error", "claude-500", "claude-other-error", "claude-plain-text",
        "codex-disconnect", "codex-agent-text", "codex-refresh-503", "codex-refresh-500", "cursor-disconnect",
        "gemini-quota", "gemini-agent-text", "other-harness"])
def test_detect_signout_ignores_other_failures_and_agent_text(harness: str, event: dict) -> None:
    assert not account_doctor.detect_signout(harness, event)


def test_a_claude_run_that_dies_signed_out_holds_its_pair(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    now = [START]
    queue = SQLiteJobQueue(db, clock=lambda: now[0])
    job = queue.enqueue(JobRequest("owner/repo", "7", "build", "claude", "c1", 3600), dedupe_key="build:7")
    setup(tmp_path, now=now)
    session = _session(db, tmp_path, job.id, "claude", now, device="pc", account="claude-b")
    _feed(session, CLAUDE_AUTH_FAILED, CLAUDE_RESULT)
    session.finish(1)
    assert _holds(db) == [("claude-b", "pc", START, "runtime", account_doctor.AUTHENTICATION_FAILED)]
    assert _events(db, job.id) == [SIGNED_OUT]
    with sqlite3.connect(db) as connection:
        dump = "\n".join(str(row) for row in connection.iterdump())
    assert "OAuth session" not in dump, "no message text is stored"


def test_a_gemini_run_that_dies_signed_out_holds_its_pair(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    now = [START]
    queue = SQLiteJobQueue(db, clock=lambda: now[0])
    job = queue.enqueue(JobRequest("owner/repo", "7", "build", "gemini", "c1", 3600), dedupe_key="build:7")
    setup(tmp_path, now=now)
    session = _session(db, tmp_path, job.id, "gemini", now, device="pc", account="gemini-ultra")
    _feed(session, GEMINI_SIGNED_OUT)
    session.finish(1)
    assert _holds(db) == [("gemini-ultra", "pc", START, "runtime", account_doctor.AUTHENTICATION_FAILED)]
    assert _events(db, job.id) == [SIGNED_OUT]


def test_a_codex_run_on_the_default_account_holds_the_default_pair(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    now = [START]
    queue = SQLiteJobQueue(db, clock=lambda: now[0])
    job = queue.enqueue(JobRequest("owner/repo", "7", "review", "codex", "c1", 3600, head="a" * 40), dedupe_key="r:7")
    setup(tmp_path, now=now)
    _feed(_session(db, tmp_path, job.id, "codex", now), {"type": "error", "message": "Not logged in"})
    assert _holds(db) == [("codex-default", "box", START, "runtime", account_doctor.AUTHENTICATION_FAILED)]
    assert _events(db, job.id) == [SIGNED_OUT]


def test_a_run_that_never_signs_out_writes_no_hold(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    now = [START]
    queue = SQLiteJobQueue(db, clock=lambda: now[0])
    job = queue.enqueue(JobRequest("owner/repo", "7", "build", "claude", "c1", 3600), dedupe_key="build:7")
    session = _session(db, tmp_path, job.id, "claude", now)
    _feed(session, {**CLAUDE_RESULT, "result": "API Error: 500"})
    session.finish(1)
    assert _holds(db) == []
    with sqlite3.connect(db) as connection:
        assert signout_end(connection, job.id) is None


def test_a_codex_run_whose_token_refresh_hits_a_transient_error_writes_no_hold(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    now = [START]
    queue = SQLiteJobQueue(db, clock=lambda: now[0])
    job = queue.enqueue(JobRequest("owner/repo", "7", "review", "codex", "c1", 3600, head="a" * 40), dedupe_key="r:7")
    setup(tmp_path, now=now)
    session = _session(db, tmp_path, job.id, "codex", now)
    _feed(session, CODEX_TRANSIENT)
    session.finish(1)
    assert _holds(db) == []
    with sqlite3.connect(db) as connection:
        assert signout_end(connection, job.id) is None, "T6 sees an ordinary failure"


def test_a_signed_out_build_requeues_as_itself_and_never_sticks(tmp_path: Path) -> None:
    now, delivery, dispatcher, queue, db = _build(tmp_path)
    first = _run(tmp_path, now, queue, dispatcher, db, CLAUDE_AUTH_FAILED, CLAUDE_RESULT)
    assert _jobs(db) == [(first, "queued", 1)]
    assert REQUEUED_SIGNED_OUT in _events(db, first)

    second = _run(tmp_path, now, queue, dispatcher, db, CLAUDE_AUTH_FAILED, CLAUDE_RESULT)
    assert second == first, "the same row runs again"
    assert _jobs(db) == [(first, "queued", 1)]
    assert _no_retry_rows(db), "no T6 retry row"
    assert delivery.jobs == [], "no stuck wake"


def test_a_signed_out_second_attempt_never_sticks_job_failed_2(tmp_path: Path) -> None:
    now, delivery, dispatcher, queue, db = _build(tmp_path)
    first = _run(tmp_path, now, queue, dispatcher, db, {**CLAUDE_RESULT, "result": "API Error: 500"})
    (_, *first_state), (retry, *retry_state) = _jobs(db)
    assert (first_state, retry_state) == (["failed", 1], ["queued", 2])

    again = _run(tmp_path, now, queue, dispatcher, db, CLAUDE_AUTH_FAILED, CLAUDE_RESULT)
    assert again == retry
    assert _jobs(db) == [(first, "failed", 1), (retry, "queued", 2)]
    assert delivery.jobs == [], "no job-failed-2 wake"


def test_a_plain_second_failure_still_sticks_job_failed_2(tmp_path: Path) -> None:
    now, delivery, dispatcher, queue, db = _build(tmp_path)
    _run(tmp_path, now, queue, dispatcher, db, {**CLAUDE_RESULT, "result": "API Error: 500"})
    _run(tmp_path, now, queue, dispatcher, db, {**CLAUDE_RESULT, "result": "API Error: 500"})
    assert delivery.jobs != [], "the job-failed-2 wake the signed-out run never sends"


def test_a_plain_failure_after_a_signout_requeue_still_follows_t6(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _run(tmp_path, now, queue, dispatcher, db, CLAUDE_AUTH_FAILED, CLAUDE_RESULT)
    _run(tmp_path, now, queue, dispatcher, db, {**CLAUDE_RESULT, "result": "API Error: 500"})
    retry = queue.claim("claude")
    assert retry is not None and retry.id != first and retry.request.attempt == 2
