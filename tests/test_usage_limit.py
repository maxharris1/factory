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
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.dispatcher import Dispatcher
from lib.machines_yml import load_machines_yml
from lib.job_state import REQUEUED_USAGE_LIMIT, USAGE_LIMIT, link_drop_requeues, usage_limit_end
from lib.queue import JobRequest, SQLiteJobQueue
from lib.status import JobRow, SQLiteStatusReader, Status, render
from lib.telemetry import EventLog, Scrubber, Session, stream_flags
from lib.usage_readings import Reading, migrate, parse_get_usage, store
from tests.pool_helpers import ROOT
from tests.test_hooks import TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions, say, setup, ticket
from tests.test_job_state import Poller
from tests.test_link_drop_requeue import BACKOFF, dropped_run, poll, retries
from tests.test_remote_start import FakeTransport
from tests.test_usage_readings import PRE_ACCOUNT_DDL

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


def _feed(
    db: Path, tmp_path: Path, job_id: str, harness: str, event: dict, now: list[float], account: str | None = None
) -> Session:
    session = Session(
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
        account=account,
    )
    session.consume(io.BytesIO((json.dumps(event) + "\n").encode()))
    return session


def _limit(db: Path, at: float, account: str = "claude-default") -> lane_limit.LaneLimit | None:
    with sqlite3.connect(db) as connection:
        return lane_limit.account_limits(connection, at).get(account)


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
    config, _ = load_machines_yml(ROOT / "machines.yml")
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
        limit = lane_limit.account_limits(connection, START).get("codex-default")
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
        assert lane_limit.account_limits(connection, now[0])["claude-default"].until == RESET

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
        limit = lane_limit.account_limits(connection, now[0])["claude-default"]
        usage = dashboard_usage.read_usage(connection, now[0], _machines())
    label = lane_limit.label(limit)
    assert SQLiteStatusReader(db).lane_limits(now[0]) == {"claude": label, "claude (claude-default)": label}
    assert label.startswith("usage limit until ")

    row = JobRow(job_id, "owner/repo", "42", "build", "claude", "queued", START, START)
    report = render(Status(
        observed_at=now[0], queued=(row,), wakes=(), devices=(), paused=(),
        lane_limits={"claude": label},
    ))
    assert f"claude: {label}" in report
    assert f"[{label}]" in report

    claude = [e for e in usage["accounts"] if e["harness"] == "claude"]
    assert claude and all(e["limited_until"] == RESET and e["limit_label"] == label for e in claude)
    assert all(e["limit_label"] is None for e in usage["accounts"] if e["harness"] != "claude")


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
        limited_at = lane_limit.account_limits(connection, now[0])["claude-default"].limited_at

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
        assert "claude-default" not in lane_limit.account_limits(connection, now[0])
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
        assert lane_limit.account_limits(connection, now[0]) == {}
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


@pytest.mark.parametrize("order", ["full_then_healthy", "healthy_then_full"])
def test_a_newer_no_reading_on_the_same_device_never_hides_an_older_full_window(
    tmp_path: Path, order: str
) -> None:
    now, _, dispatcher, queue, db = _build(tmp_path)
    first = _die_on_limit(tmp_path, now, queue, dispatcher, db)
    week = int(START) + 7 * 86400
    updates = {
        "full": [Reading("five_hour", 0.4, RESET, START + 30),
                 Reading("seven_day", 1.0, week, START + 30)],
        "healthy": [Reading("five_hour", 0.5, RESET, START + 60),
                    Reading("seven_day", None, None, START + 60)],
    }
    sequence = ["full", "healthy"] if order == "full_then_healthy" else ["healthy", "full"]
    with sqlite3.connect(db) as connection:
        for name in sequence:
            store(connection, harness="claude", bot="claude-bot", device="box",
                  job_id=name, readings=updates[name])
    now[0] = START + 61
    assert queue.claim("claude") is None, "the weekly window is still full"
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
        assert "claude-default" not in lane_limit.account_limits(connection, now[0])
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
        bot="codex-bot", repo="owner/repo", harness="codex", flags=stream_flags(["codex"]),
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
    entry = next(e for e in snapshot["usage"]["accounts"] if e["harness"] == "claude")
    assert entry["state"] == "no telemetry"
    page = render_pages(snapshot)["efficiency.html"]
    assert entry["limit_label"] in page.split("<script>")[0]


TWO_CLAUDE_ACCOUNTS = """
accounts:
  - {{id: claude-a, harness: claude}}
  - {{id: claude-b, harness: claude}}
devices:
  - name: box
    local: true
    slot_max: 3
    harnesses: {{claude: {{bot: claude-bot, account: claude-a}}, codex: codex-bot}}
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 3
    harnesses: {{claude: {{bot: claude-bot, account: claude-b}}, codex: codex-bot}}
    workdir_root: {root}/remote/{{bot}}/work
    ssh_alias: fakepc-{{bot}}
    factory_root: /srv/factory
"""


def _two_accounts(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    machines = tmp_path / "machines.yml"
    machines.write_text(TWO_CLAUDE_ACCOUNTS.format(root=tmp_path), encoding="utf-8")
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    now = [START]
    db = tmp_path / "d.db"
    queue = SQLiteJobQueue(db, clock=lambda: now[0])
    pool = DevicePool(queue, config)
    dispatcher = Dispatcher(
        db, queue, trusted_logins=TRUSTED_LOGINS, review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=DeviceMonitor(pool, config, FakeTransport(check=0), alive=lambda *_: False),
    )
    jobs = [
        queue.enqueue(JobRequest("owner/repo", str(n), "build", "claude", f"c{n}", 3600), dedupe_key=f"build:{n}").id
        for n in (1, 2)
    ]
    return now, queue, pool, dispatcher, jobs, db


def test_a_limit_on_one_account_places_its_harness_on_the_other_accounts_device(tmp_path: Path) -> None:
    _, _, control, _, jobs, _ = _two_accounts(tmp_path / "control")
    assert [control.claim("claude")[1].device for _ in jobs] == ["pc", "box"]

    now, _, pool, _, jobs, db = _two_accounts(tmp_path / "limited")
    _feed(db, tmp_path, jobs[0], "claude", CLAUDE_LIMIT, now, account="claude-a")
    assert _limit(db, now[0], "claude-a").until == RESET
    assert [pool.claim("claude")[1].device for _ in jobs] == ["pc", "pc"]


@pytest.mark.parametrize(
    ("limited", "state"), [(("claude-a",), "failed"), (("claude-a", "claude-b"), "queued")]
)
def test_the_queue_waits_only_while_every_serving_account_is_limited(
    tmp_path: Path, limited: tuple[str, ...], state: str
) -> None:
    now, _, pool, dispatcher, (ran, waiting), db = _two_accounts(tmp_path)
    for account in limited:
        _feed(db, tmp_path, ran, "claude", CLAUDE_LIMIT, now, account=account)
    if len(limited) == 2:
        assert pool.claim("claude") is None
    now[0] = START + 3600 + 1
    assert now[0] < RESET
    dispatcher.reconcile_jobs()
    assert _job(db, waiting)[0] == state, "a queued job times out only while some serving account is free"


def test_a_healthy_reading_of_one_account_never_shortens_anothers_pause(tmp_path: Path) -> None:
    now, _, _, _, jobs, db = _two_accounts(tmp_path)
    _feed(db, tmp_path, jobs[0], "claude", CLAUDE_LIMIT, now, account="claude-a")
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="pc", job_id="b", account="claude-b",
              readings=[Reading("five_hour", 0.2, RESET, START + 60)])
    assert _limit(db, START + 61, "claude-a").until == RESET
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="a", account="claude-a",
              readings=[Reading("five_hour", 0.2, RESET, START + 90)])
    assert _limit(db, START + 91, "claude-a") is None


def test_an_older_binarys_harness_wide_pause_still_holds_every_account(tmp_path: Path) -> None:
    now, queue, pool, dispatcher, jobs, db = _two_accounts(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(lane_limit.DDL)
        connection.execute(
            "INSERT INTO lane_limits VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("claude", "claude", "claude-bot", START, RESET, "message", jobs[0]),
        )
    assert pool.claim("claude") is None
    assert queue.claim("claude") is None
    now[0] = START + 3601
    dispatcher.reconcile_jobs()
    assert [_job(db, job_id)[0] for job_id in jobs] == ["queued", "queued"]
    now[0] = RESET + 1
    assert pool.claim("claude") is not None


def _legacy_pause(tmp_path: Path, migrated: bool) -> tuple[Path, SQLiteJobQueue]:
    db = tmp_path / "state" / "orch.db"
    db.parent.mkdir()
    queue = SQLiteJobQueue(db, clock=lambda: 160.0)
    job = queue.enqueue(JobRequest("owner/repo", "1", "build", "claude", "go", 3600), dedupe_key="j")
    with sqlite3.connect(db) as connection:
        connection.execute(PRE_ACCOUNT_DDL)
        connection.execute(lane_limit.DDL)
        connection.execute(
            "INSERT INTO lane_limits VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("claude", "claude", "claude-bot", 100, 18100, "message", job.id),
        )
    if migrated:
        migrate(db, lambda: sqlite3.connect(db), directory=tmp_path / "manual")
    return db, queue


def _legacy_until(db: Path, reading: Reading, account: str | None = None) -> int:
    with sqlite3.connect(db) as connection:
        store(connection, harness="claude", bot="claude-bot", device="box", job_id="probe",
              readings=[reading], account=account)
        return connection.execute("SELECT until FROM lane_limits WHERE recipient = 'claude'").fetchone()[0]


@pytest.mark.parametrize("migrated", [True, False], ids=["migrated", "unmigrated"])
def test_a_healthy_default_account_reading_lifts_an_older_binarys_pause(tmp_path: Path, migrated: bool) -> None:
    db, queue = _legacy_pause(tmp_path, migrated)
    assert queue.claim("claude") is None
    assert _legacy_until(db, Reading("five_hour", 0.2, 18100, 150)) == 150
    assert queue.claim("claude") is not None


def test_another_accounts_healthy_reading_never_moves_an_older_binarys_pause(tmp_path: Path) -> None:
    db, queue = _legacy_pause(tmp_path, migrated=True)
    assert _legacy_until(db, Reading("five_hour", 0.2, 18100, 150), account="claude-work") == 18100
    assert queue.claim("claude") is None


def test_a_full_default_account_reading_moves_an_older_binarys_pause_to_its_reset(tmp_path: Path) -> None:
    db, queue = _legacy_pause(tmp_path, migrated=True)
    assert _legacy_until(db, Reading("five_hour", 1.0, 20000, 150)) == 20000
    assert queue.claim("claude") is None


def test_status_names_the_account_of_each_pause(tmp_path: Path) -> None:
    now, _, _, _, jobs, db = _two_accounts(tmp_path)
    _feed(db, tmp_path, jobs[0], "claude", CLAUDE_LIMIT, now, account="claude-a")
    label = lane_limit.label(_limit(db, now[0], "claude-a"))
    serving = {"claude": frozenset({"claude-a", "claude-b"})}
    assert SQLiteStatusReader(db, serving=serving).lane_limits(now[0]) == {"claude (claude-a)": label}
    _feed(db, tmp_path, jobs[1], "claude", CLAUDE_LIMIT, now, account="claude-b")
    assert SQLiteStatusReader(db, serving=serving).lane_limits(now[0]) == {
        "claude": label, "claude (claude-a)": label, "claude (claude-b)": label,
    }
