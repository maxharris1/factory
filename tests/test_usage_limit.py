"""#367: a harness usage limit pauses its lane instead of sticking tickets.

A real telemetry Session reads the harness's limit event into a temp
orch.db, the real dispatcher's T6 requeues the dead job as itself, and both
claim paths hold the lane until the reset or a fresh reading below 1.0.
"""

from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from lib import dashboard_usage, lane_limit
from lib.dashboard import collect_snapshot, render_pages
from lib.device_pool import DevicePool
from lib.machines_yml import load_machines_yml
from lib.job_state import REQUEUED_USAGE_LIMIT, USAGE_LIMIT, link_drop_requeues, usage_limit_end
from lib.queue import JobRequest, SQLiteJobQueue
from lib.status import JobRow, SQLiteStatusReader, Status, render
from lib.telemetry import EventLog, Scrubber, Session
from lib.usage_readings import Reading, parse_get_usage, store
from tests.pool_helpers import ROOT
from tests.test_hooks import FakeGrokDelivery, say, setup, ticket
from tests.test_job_state import Poller
from tests.test_link_drop_requeue import BACKOFF, dropped_run, poll, retries

START = 1_800_000_000.0
RESET = int(START) + 5 * 3600
GO = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->\nGo.\n"
CODEX_INCIDENT = (
    "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
    "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again "
    "at Jan 20th, 2027 3:04 AM."
)
CLAUDE_LIMIT = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "result": f"Claude AI usage limit reached|{RESET}",
}


def _feed(db: Path, tmp_path: Path, job_id: str, harness: str, event: dict, now: list[float]) -> Session:
    session = Session(
        connect=lambda: sqlite3.connect(db),
        job_id=job_id,
        bot=f"{harness}-bot",
        repo="owner/repo",
        harness=harness,
        argv=[harness],
        raw_log=EventLog(tmp_path / job_id / "events.jsonl", log=lambda _: None),
        scrub=Scrubber({}),
        log=lambda _: None,
        stdout=io.BytesIO(),
        clock=lambda: now[0],
    )
    session.consume(io.BytesIO((json.dumps(event) + "\n").encode()))
    return session


def _events(db: Path, job_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as connection:
        return [
            (str(e), str(d))
            for e, d in connection.execute(
                "SELECT event, detail FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
            )
        ]


def _job(db: Path, job_id: str) -> tuple[str, int]:
    with sqlite3.connect(db) as connection:
        state, attempt = connection.execute(
            "SELECT state, attempt FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return str(state), int(attempt)


def _machines():
    config, _ = load_machines_yml(ROOT / "machines.yml", allow_placeholders=True)
    assert config is not None
    return config


def _pool(queue: SQLiteJobQueue) -> DevicePool:
    return DevicePool(queue, _machines())


def _build(tmp_path: Path):
    now = [START]
    delivery = FakeGrokDelivery()
    dispatcher, queue = setup(tmp_path, now=now, grok_delivery=delivery)
    _pool(queue)
    dispatcher.process(replace(ticket(say("904", GO, "grok-login", 1)), pr_number="42"))
    return now, delivery, dispatcher, queue, tmp_path / "d.db"


def _die_on_limit(tmp_path, now, queue, dispatcher, db, event=CLAUDE_LIMIT) -> str:
    job = queue.claim("claude")
    assert job is not None, "the build is claimable"
    session = _feed(db, tmp_path, job.id, "claude", event, now)
    session.finish(1)
    queue.finish(job.id, owner="claude", handoff_comment_id=None)
    now[0] += 5
    dispatcher.reconcile_jobs()
    return job.id


# --- detect and until -----------------------------------------------------------


def test_detect_reads_the_incidents_codex_error_and_its_reset() -> None:
    limit = lane_limit.detect("codex", {"type": "error", "message": CODEX_INCIDENT}, START)
    assert limit is not None
    assert limit.resets_at == int(datetime(2027, 1, 20, 3, 4).astimezone().timestamp())


def test_detect_reads_claude_limit_results() -> None:
    assert lane_limit.detect("claude", CLAUDE_LIMIT, START) == lane_limit.UsageLimit(RESET)
    later = {**CLAUDE_LIMIT, "result": "5-hour limit reached ∙ resets 3pm (America/New_York)"}
    found = lane_limit.detect("claude", later, START)
    assert found is not None and found.resets_at is not None and START < found.resets_at <= START + 86400
    bare = {**CLAUDE_LIMIT, "result": "Claude usage limit reached."}
    assert lane_limit.detect("claude", bare, START) == lane_limit.UsageLimit(None)


def test_detect_ignores_ordinary_errors() -> None:
    assert lane_limit.detect("codex", {"type": "error", "message": "stream disconnected"}, START) is None
    assert lane_limit.detect("claude", {**CLAUDE_LIMIT, "result": "API Error: 500"}, START) is None
    assert lane_limit.detect("claude", {**CLAUDE_LIMIT, "is_error": False}, START) is None
    assert lane_limit.detect("cursor", {"type": "error", "message": CODEX_INCIDENT}, START) is None


def test_until_prefers_the_message_then_a_full_reading_then_an_hour() -> None:
    full = [Reading("seven_day", 1.0, RESET + 60, START - 10)]
    assert lane_limit.until(lane_limit.UsageLimit(RESET), full, START) == (RESET, "message")
    assert lane_limit.until(lane_limit.UsageLimit(None), full, START) == (RESET + 60, "reading")
    half = [Reading("seven_day", 0.5, RESET, START - 10)]
    assert lane_limit.until(lane_limit.UsageLimit(None), half, START) == (
        int(START) + lane_limit.DEFAULT_LIMIT_SECONDS,
        "default",
    )


def test_a_reset_past_the_30_day_horizon_is_refused() -> None:
    far = {**CLAUDE_LIMIT, "result": f"Claude AI usage limit reached|{int(START) + 40 * 86400}"}
    assert lane_limit.detect("claude", far, START) == lane_limit.UsageLimit(None)


# --- ac-1: the job requeues, nothing is charged, the lane is limited -------------


def test_a_codex_session_fed_the_incident_line_limits_the_codex_lane(tmp_path: Path) -> None:
    db = tmp_path / "d.db"
    now = [START]
    queue = SQLiteJobQueue(db, clock=lambda: now[0])
    job = queue.enqueue(
        JobRequest("owner/repo", "7", "review", "codex", "c1", 3600, head="a" * 40),
        dedupe_key="review:7",
    )
    setup(tmp_path, now=now)
    _feed(db, tmp_path, job.id, "codex", {"type": "error", "message": CODEX_INCIDENT}, now)
    with sqlite3.connect(db) as connection:
        limit = lane_limit.active(connection, "codex", START)
        dump = "\n".join(str(row) for row in connection.iterdump())
    assert limit is not None and limit.source in ("message", "default")
    assert [e for e, _ in _events(db, job.id)] == [USAGE_LIMIT]
    assert "usage limit until" in _events(db, job.id)[0][1]
    assert "Upgrade to Pro" not in dump and "chatgpt.com" not in dump, "no message text is stored"


def test_a_usage_limited_build_requeues_as_itself_and_never_sticks(tmp_path: Path) -> None:
    now, delivery, dispatcher, queue, db = _build(tmp_path)

    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    assert _job(db, first) == ("queued", 1)
    assert REQUEUED_USAGE_LIMIT in [e for e, _ in _events(db, first)]
    with sqlite3.connect(db) as connection:
        assert lane_limit.active(connection, "claude", now[0]).until == RESET

    now[0] = RESET + 1
    second = _die_on_limit(tmp_path, now, queue, dispatcher, db, {
        **CLAUDE_LIMIT, "result": f"Claude AI usage limit reached|{RESET + 3600}"
    })
    assert second == first, "the same row runs again"
    assert _job(db, first) == ("queued", 1)
    assert delivery.jobs == [], "no stuck wake"
    with sqlite3.connect(db) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE dedupe_key LIKE 't6-retry:%'"
        ).fetchone()[0]
    assert count == 0, "no T6 retry row"


def test_a_plain_failure_after_a_limit_requeue_still_follows_t6(tmp_path: Path) -> None:
    now, delivery, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    now[0] = RESET + 1
    job = queue.claim("claude")
    assert job is not None and job.id == first
    queue.finish(job.id, owner="claude", handoff_comment_id=None)
    now[0] += 5
    dispatcher.reconcile_jobs()
    retry = queue.claim("claude")
    assert retry is not None and retry.id != first and retry.request.attempt == 2


# --- ac-2: no device claims for a limited lane; status and dashboard show it -----


def test_no_device_claims_a_limited_lane_while_another_lane_still_claims(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    _die_on_limit(tmp_path, now, queue, dispatcher, db)
    review = queue.enqueue(
        JobRequest("owner/repo", "8", "review", "codex", "c2", 3600, head="b" * 40),
        dedupe_key="review:8",
    )
    pool = _pool(queue)

    assert queue.claim("claude") is None
    assert pool.claim("claude") is None
    claimed = pool.claim("codex")
    assert claimed is not None and claimed[0].id == review.id


def test_status_and_dashboard_show_the_limit(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    job_id = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id=job_id,
              readings=[Reading("five_hour", 1.0, RESET, START - 60)])
        limit = lane_limit.active(connection, "claude", now[0])
        usage = dashboard_usage.read_usage(connection, now[0], _machines())
    label = lane_limit.label(limit)
    assert SQLiteStatusReader(db).lane_limits(now[0]) == {"claude": label}
    assert label.startswith("usage limit until ")

    row = JobRow(job_id, "owner/repo", "42", "build", "claude", "queued", START, START)
    report = render(Status(
        observed_at=now[0], queued=(row,), wakes=(), devices=(), paused=(),
        lane_limits={"claude": label},
    ))
    assert f"claude: {label}" in report
    assert f"[{label}]" in report

    claude = [e for e in usage["bots"] if e["harness"] == "claude"]
    assert claude and all(e["limited_until"] == RESET and e["limit_label"] == label for e in claude)
    assert all(e["limit_label"] is None for e in usage["bots"] if e["harness"] != "claude")


# --- ac-3: the lane resumes by itself ------------------------------------------


def test_the_lane_resumes_after_the_reset(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    now[0] = RESET - 1
    assert queue.claim("claude") is None
    now[0] = RESET + 1
    job = queue.claim("claude")
    assert job is not None and job.id == first


def test_a_fresh_reading_below_one_lifts_the_limit_and_an_older_one_does_not(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    with sqlite3.connect(db) as connection:
        limited_at = lane_limit.active(connection, "claude", now[0]).limited_at

    def reading(observed_at: float) -> None:
        with sqlite3.connect(db) as connection:
            store(connection, harness="claude", bot="claude-bot", device="box", job_id="probe",
                  readings=[Reading("five_hour", 0.4, RESET, observed_at)])

    reading(limited_at - 1)
    now[0] += 60
    assert queue.claim("claude") is None, "a reading from before the limit never lifts it"

    reading(limited_at + 30)
    job = queue.claim("claude")
    assert job is not None and job.id == first


def test_a_limited_queued_job_waits_without_spending_attempts_or_sticking(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    original = next(job for job in queue.outstanding() if job.id == first)
    for _ in range(2):
        now[0] += original.request.timeout_seconds + 1
        assert now[0] < RESET
        dispatcher.reconcile_jobs()
    with sqlite3.connect(db) as connection:
        jobs = connection.execute("SELECT id, state FROM jobs WHERE recipient = 'claude'").fetchall()
        stuck = connection.execute("SELECT stuck FROM ticket_state WHERE ticket_id = '42'").fetchall()
    assert jobs == [(first, "queued")]
    assert stuck == [(0,)]
    now[0] = RESET - 1
    dispatcher.reconcile_jobs()
    now[0] = RESET + original.request.timeout_seconds - 2
    dispatcher.reconcile_jobs()
    assert _job(db, first) == ("queued", 1)
    assert queue.claim("claude").id == first


def test_a_new_limit_after_an_early_resume_obeys_its_own_reset(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    now[0] = START + 60
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="probe",
              readings=[Reading("five_hour", 0.4, RESET, now[0])])
        assert lane_limit.active(connection, "claude", now[0]) is None
    new_reset = int(now[0]) + 3600
    second = _die_on_limit(tmp_path, now, queue, dispatcher, db,
                           {**CLAUDE_LIMIT, "result": f"Claude AI usage limit reached|{new_reset}"})
    assert second == first
    assert queue.claim("claude") is None
    now[0] = new_reset + 1
    assert queue.claim("claude").id == first


def test_an_early_resume_holds_after_its_reading_ages_out(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    long_reset = int(START) + 7 * 86400
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db,
                          {**CLAUDE_LIMIT, "result": f"Claude AI usage limit reached|{long_reset}"})
    now[0] = START + 60
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="probe",
              readings=[Reading("five_hour", 0.4, int(START) + 3600, now[0])])
    resumed = queue.claim("claude")
    assert resumed is not None and resumed.id == first
    queue.release(resumed.id, owner="claude")
    now[0] = START + 3601
    with sqlite3.connect(db) as connection:
        assert lane_limit.all_active(connection, now[0]) == []
    assert queue.claim("claude").id == first


def test_the_newest_reading_of_a_window_on_any_device_decides(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    week = int(START) + 7 * 86400
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="older",
              readings=[Reading("five_hour", 1.0, RESET, START + 30),
                        Reading("seven_day", 0.1, week, START + 30)])
        store(connection, harness="claude", bot="claude-bot", device="remote", job_id="newer",
              readings=[Reading("five_hour", 0.4, RESET, START + 60),
                        Reading("seven_day", 0.1, week, START + 60)])
    now[0] = START + 61
    assert queue.claim("claude").id == first


def test_a_newer_full_reading_keeps_the_lane_paused(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    _die_on_limit(tmp_path, now, queue, dispatcher, db)
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="older",
              readings=[Reading("five_hour", 0.4, RESET, START + 30)])
        store(connection, harness="claude", bot="claude-bot", device="remote", job_id="newer",
              readings=[Reading("five_hour", 1.0, RESET, START + 60)])
    now[0] = START + 61
    assert queue.claim("claude") is None


@pytest.mark.parametrize("order", ["full_then_healthy", "healthy_then_full"])
def test_a_healthy_window_never_clears_another_window_still_full(tmp_path: Path, order: str) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    week = int(START) + 7 * 86400
    updates = {
        "full": ("remote", [Reading("five_hour", 0.4, RESET, START + 30),
                            Reading("seven_day", 1.0, week, START + 30)]),
        "healthy": ("box", [Reading("five_hour", 0.5, RESET, START + 60),
                            Reading("seven_day", None, None, START + 60)]),
    }
    sequence = ["full", "healthy"] if order == "full_then_healthy" else ["healthy", "full"]
    with sqlite3.connect(db) as connection:
        for name in sequence:
            device, readings = updates[name]
            store(connection, harness="claude", bot="claude-bot", device=device,
                  job_id=name, readings=readings)
    now[0] = START + 61
    assert queue.claim("claude") is None, "the weekly window is still full"
    now[0] = week - 1
    assert queue.claim("claude") is None
    now[0] = week + 1
    assert queue.claim("claude").id == first


def test_the_first_poll_after_a_reset_missed_by_downtime_charges_nothing(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    original = next(job for job in queue.outstanding() if job.id == first)
    now[0] = RESET + 1
    dispatcher.reconcile_jobs()
    assert _job(db, first) == ("queued", 1)
    now[0] = RESET + original.request.timeout_seconds - 2
    dispatcher.reconcile_jobs()
    assert _job(db, first) == ("queued", 1)
    assert queue.claim("claude").id == first


def test_the_first_poll_after_a_reading_lift_missed_by_downtime_charges_nothing(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    original = next(job for job in queue.outstanding() if job.id == first)
    lift = START + 2 * original.request.timeout_seconds
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="probe",
              readings=[Reading("five_hour", 0.4, RESET, lift)])
    now[0] = lift + 10
    dispatcher.reconcile_jobs()
    assert _job(db, first) == ("queued", 1)
    with sqlite3.connect(db) as connection:
        stuck = connection.execute("SELECT stuck FROM ticket_state WHERE ticket_id = '42'").fetchall()
    assert stuck == [(0,)]


def _unavailable_usage(at: float) -> list[Reading]:
    reply = {"type": "control_response", "response": {
        "request_id": "orch-usage", "subtype": "success",
        "response": {"rate_limits_available": False}}}
    return parse_get_usage(json.dumps(reply).encode(), at)


def test_a_no_reading_never_restores_a_lifted_limit(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    now[0] = START + 60
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="probe",
              readings=[Reading("five_hour", 0.4, RESET, now[0])])
    resumed = queue.claim("claude")
    assert resumed is not None and resumed.id == first
    queue.release(first, owner="claude")
    now[0] = START + 120
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="probe2",
              readings=_unavailable_usage(now[0]))
        assert lane_limit.active(connection, "claude", now[0]) is None
    assert queue.claim("claude").id == first


def test_an_older_full_reading_on_another_device_never_restores_a_lifted_limit(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="remote", job_id="older",
              readings=[Reading("five_hour", 1.0, RESET, START + 30)])
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="newer",
              readings=[Reading("five_hour", 0.4, RESET, START + 60)])
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="missing",
              readings=_unavailable_usage(START + 90))
    now[0] = START + 91
    assert queue.claim("claude").id == first


def _feed_codex(poller, job_id: str, event: dict) -> None:
    session = Session(
        connect=lambda: sqlite3.connect(poller.path), job_id=job_id,
        bot="codex-bot", repo="owner/repo", harness="codex", argv=["codex"],
        raw_log=None, scrub=Scrubber({}), log=lambda _: None, stdout=io.BytesIO(),
        clock=lambda: poller.now[0],
    )
    session.consume(io.BytesIO((json.dumps(event) + "\n").encode()))
    session.finish(1)


def test_a_limit_then_a_link_drop_in_the_same_run_is_still_a_free_retry(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=False, check=0)
    job_id = poller.queue.enqueue(
        JobRequest("owner/repo", "42", "build", "codex", "go-1", 3600), dedupe_key="build-42"
    ).id
    for round_number in range(7):
        assert poller.pool.claim("codex") is not None
        reset = int(poller.now[0] + 5 * 3600) // 60 * 60
        reset_text = datetime.fromtimestamp(reset).strftime("%b %d, %Y %I:%M %p")
        _feed_codex(poller, job_id, {"type": "error", "message":
            f"You've hit your usage limit. Please try again at {reset_text}."})
        dropped_run(poller, job_id)
        failed_at = poller.now[0] + 5
        poll(poller, failed_at)
        poll(poller, failed_at + BACKOFF[min(round_number, 5)])
        with poller.queue.connect() as connection:
            assert link_drop_requeues(connection, job_id) == 0
            assert usage_limit_end(connection, job_id) is None
        poller.now[0] = reset + 1
    assert retries(poller) == []
    assert [(row[1], row[2]) for row in poller.jobs()] == [("queued", 1)]


def test_the_dashboard_shows_a_limit_before_any_reading(tmp_path: Path) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    _die_on_limit(tmp_path, now, queue, dispatcher, db)
    log = tmp_path / "orch.log"
    log.write_text("")
    snapshot = collect_snapshot(db, log, now=now[0], machines_path=ROOT / "machines.yml")
    entry = next(e for e in snapshot["usage"]["bots"] if e["harness"] == "claude")
    assert entry["state"] == "no telemetry"
    page = render_pages(snapshot)["efficiency.html"]
    assert entry["limit_label"] in page.split("<script>")[0]
