"""Tests for E3-1: pull-based universal orchestrator (`orch wait orchestrator`)."""

from __future__ import annotations

import io
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from lib import cli, eval_report
from lib.dispatch.constants import GROK_WAKE_REASONS
from lib.dispatcher import Dispatcher, TrackedTicket
from lib.grok_delivery import PullDriverDelivery, wake_delivery
from lib.orch_yml import DriverInbox, Project, load_orch_yml
from lib.orchestrator_pull import (
    ORCHESTRATOR_BOT,
    OrchestratorUsage,
    ack_wake,
    claim_next,
    enqueue_pull_wake,
    finish_wake,
    heartbeat_wake,
    release_wake,
    wait_for_wake,
)
from lib.pricing import effective_cost_usd
from lib.pstack_pin import ROOT, verify_pin
from lib.queue import Job, JobRequest, SQLiteJobQueue
from tests.test_dispatcher import QUESTIONS, FakeReviewActions, factory
from tests.test_driver_inbox import FlakyGrok, make, wake
from tests.test_driver_wake import flags, poll
from tests.test_resume_plan import ResumeFlow

REPO = "maxharris1/factory"


def _make_job(
    queue: SQLiteJobQueue,
    *,
    action: str = "awaiting-ok",
    ticket_id: str = "42",
    source_comment_id: str = "c-1",
    head: str = "a" * 40,
    dedupe_key: str | None = None,
) -> Job:
    req = JobRequest(
        repo=REPO,
        ticket_id=ticket_id,
        action=action,
        recipient="grok",
        source_comment_id=source_comment_id,
        timeout_seconds=300,
        generation=1,
        attempt=1,
        head=head,
        detail=json.dumps({"link": f"https://github.com/{REPO}/pull/{ticket_id}", "outcome": action}),
    )
    job = queue.enqueue(req, dedupe_key=dedupe_key or f"wake:{ticket_id}:{action}:{source_comment_id}")
    assert job is not None
    return job


def _make_db(tmp_path: Path, now_val: float = 1000.0) -> tuple[Path, SQLiteJobQueue, list[float]]:
    clock = [now_val]
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=lambda: clock[0])
    Dispatcher(
        db,
        queue,
        trusted_logins={"github": {"maxharris1": {"grok-bot", "claude-bot", "codex-bot"}}},
        review_actions=FakeReviewActions(),
        grok_delivery=PullDriverDelivery(),
    )
    return db, queue, clock


def test_orch_yml_accepts_driver_pull_without_webhook_blocks(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\n"
        "driver: pull\n"
        "projects:\n"
        "  - repo: maxharris1/factory\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert findings == []
    assert config is not None
    assert config.driver == "pull"
    assert config.grok is None
    assert config.claude is None
    assert isinstance(wake_delivery(config), PullDriverDelivery)


def test_dispatcher_delivers_wakes_into_driver_inbox_under_pull_driver(tmp_path: Path) -> None:
    clock = [1000.0]
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=lambda: clock[0])
    dispatcher = Dispatcher(
        db,
        queue,
        trusted_logins={"github": {"maxharris1": {"grok-bot", "claude-bot", "codex-bot"}}},
        review_actions=FakeReviewActions(),
        grok_delivery=PullDriverDelivery(),
        projects=(Project(repo=REPO),),
    )
    ticket = TrackedTicket(
        repo=REPO,
        ticket_id="77",
        factory=factory(ship="merge"),
        comments=(),
        current_head="a" * 40,
        ci={},
        risk="high",
        pr_number="77",
        ticket_url=f"https://github.com/{REPO}/pull/77",
    )
    dispatcher.wake_grok_once(
        ticket,
        reason="awaiting-ok",
        source_comment_id="v-1",
        detail=json.dumps({"link": ticket.ticket_url, "outcome": "awaiting-ok"}),
        head="a" * 40,
    )
    dispatcher._deliver_grok_jobs(set())

    claimed = claim_next(db, claimer="gemini-orch", lease_seconds=60, now=lambda: clock[0])
    assert claimed is not None
    assert claimed.wake["action"] == "awaiting-ok"
    assert claimed.wake["repo"] == REPO
    assert claimed.wake["ticket_id"] == "77"
    assert claimed.wake["logical_key"] == claimed.logical_key
    assert claimed.attempts == 1
    assert claim_next(db, claimer="other-orch", lease_seconds=60, now=lambda: clock[0]) is None


def test_redelivering_the_same_job_queues_one_wake(tmp_path: Path) -> None:
    db, queue, clock = _make_db(tmp_path)
    job = _make_job(queue)
    with queue.connect() as conn:
        enqueue_pull_wake(conn, job, "lk", now=clock[0])
        conn.commit()
    first = claim_next(db, claimer="orch-a", now=lambda: clock[0])
    assert first is not None
    assert finish_wake(db, job.id, lease_token=first.lease_token, now=lambda: clock[0])
    with queue.connect() as conn:
        enqueue_pull_wake(conn, job, "lk", now=clock[0])
        conn.commit()
    assert claim_next(db, claimer="orch-b", now=lambda: clock[0]) is None


def test_a_legacy_inbox_row_becomes_claimable_after_switching_to_pull(tmp_path: Path) -> None:
    d, issue, _ = make(tmp_path, grok=FlakyGrok(failures=1))
    wake(d)
    d._deliver_grok_jobs(set())
    with sqlite3.connect(d.state_path) as conn:
        [(legacy_job,)] = conn.execute(
            "SELECT job_id FROM driver_inbox WHERE status = 'pending'"
        ).fetchall()
    restarted = Dispatcher(
        d.state_path,
        d.queue,
        trusted_logins=d.trusted_logins,
        review_actions=issue,
        grok_delivery=PullDriverDelivery(),
        projects=tuple(d.wake_routes.values()),
    )
    restarted.reconcile_jobs()
    claimed = claim_next(d.state_path, claimer="new-driver")
    assert claimed is not None
    assert claimed.job_id == legacy_job


def test_pull_backstop_rewake_is_claimable_then_flags(tmp_path: Path) -> None:
    flow = ResumeFlow(tmp_path, enabled=True)
    flow.dispatcher.grok_delivery = PullDriverDelivery()
    flow.start_build(planned=False)
    flow.finish_with("10")
    assert flow.say("10", QUESTIONS, "claude-login") == "waiting-max"
    flow.dispatcher.reconcile_jobs()
    start = flow.now[0]
    first = claim_next(flow.queue.path, claimer="orch", now=lambda: start)
    assert first is not None
    assert finish_wake(flow.queue.path, first.job_id, lease_token=first.lease_token, now=lambda: start)
    poll(flow, start + 1200)
    second = claim_next(flow.queue.path, claimer="orch", now=lambda: flow.now[0])
    assert second is not None
    assert second.job_id != first.job_id
    assert second.logical_key == first.logical_key
    assert second.wake["attempt"] == 2
    for minutes in (40, 60, 120):
        poll(flow, start + minutes * 60)
    assert len(flags(flow)) == 1


def test_lease_expiry_requeues_and_rejects_stale_token(tmp_path: Path) -> None:
    db, queue, clock = _make_db(tmp_path, now_val=2000.0)
    job = _make_job(queue, action="capped")
    with queue.connect() as conn:
        enqueue_pull_wake(conn, job, "lk-expire", now=clock[0])
        conn.commit()

    first = claim_next(db, claimer="crashed-orch", lease_seconds=30, now=lambda: clock[0])
    assert first is not None
    assert first.attempts == 1
    assert ack_wake(db, first.job_id, lease_token=first.lease_token, lease_seconds=30, now=lambda: clock[0]) is True

    # Heartbeat extends the lease while alive.
    clock[0] += 20.0
    assert heartbeat_wake(db, first.job_id, lease_token=first.lease_token, lease_seconds=30, now=lambda: clock[0]) is True
    # Still active at +25s -> second worker gets nothing.
    clock[0] += 25.0
    assert claim_next(db, claimer="backup-orch", lease_seconds=30, now=lambda: clock[0]) is None

    # Past lease expiry -> stale token fails, backup worker reclaims cleanly.
    clock[0] += 10.0
    assert ack_wake(db, first.job_id, lease_token=first.lease_token, lease_seconds=30, now=lambda: clock[0]) is False
    assert finish_wake(db, first.job_id, lease_token=first.lease_token, now=lambda: clock[0]) is False

    second = claim_next(db, claimer="backup-orch", lease_seconds=30, now=lambda: clock[0])
    assert second is not None
    assert second.job_id == first.job_id
    assert second.attempts == 2
    assert second.lease_token != first.lease_token

    # Old claimer still cannot hijack or finish the new lease.
    assert finish_wake(db, first.job_id, lease_token=first.lease_token, now=lambda: clock[0]) is False
    assert finish_wake(db, second.job_id, lease_token=second.lease_token, result="split", now=lambda: clock[0]) is True
    # Done wake is not reclaimed after time passes.
    clock[0] += 1000.0
    assert claim_next(db, claimer="third-orch", lease_seconds=30, now=lambda: clock[0]) is None


def test_release_and_broken_stdout_requeue_wake(tmp_path: Path) -> None:
    db, queue, clock = _make_db(tmp_path)
    job = _make_job(queue, action="clarify")
    with queue.connect() as conn:
        enqueue_pull_wake(conn, job, "lk-release", now=clock[0])
        conn.commit()

    class BrokenOut:
        def write(self, _s: str) -> int:
            raise BrokenPipeError("stdout closed")

        def flush(self) -> None:
            pass

    with pytest.raises(Exception, match="released"):
        wait_for_wake(db, BrokenOut(), claimer="orch-1", once=True, now=lambda: clock[0])

    # Because stdout broke, wake was immediately released back to queued.
    buf = io.StringIO()
    claimed = wait_for_wake(db, buf, claimer="orch-2", once=True, now=lambda: clock[0])
    assert claimed is not None
    parsed = json.loads(buf.getvalue())
    assert parsed["job_id"] == job.id
    assert parsed["wake"]["action"] == "clarify"
    assert release_wake(db, claimed.job_id, lease_token=claimed.lease_token, now=lambda: clock[0]) is True

    reclaimed = claim_next(db, claimer="orch-3", now=lambda: clock[0])
    assert reclaimed is not None and reclaimed.job_id == job.id


def test_concurrent_claimers_only_one_wins(tmp_path: Path) -> None:
    db, queue, clock = _make_db(tmp_path)
    job = _make_job(queue, action="blocked")
    with queue.connect() as conn:
        enqueue_pull_wake(conn, job, "lk-race", now=clock[0])
        conn.commit()

    barrier = threading.Barrier(4)
    results: list[object] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait()
        got = claim_next(db, claimer=f"orch-{idx}", now=lambda: clock[0])
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].job_id == job.id


def test_finish_records_orchestrator_telemetry_and_weekly_eval_spend(tmp_path: Path) -> None:
    db, queue, clock = _make_db(tmp_path, now_val=1_800_000_000.0)
    job = _make_job(queue, action="clarify")
    # Mark the delivery job done as _deliver_grok_jobs does.
    claimed_job = queue.claim("grok")
    assert claimed_job is not None
    queue.finish(claimed_job.id, owner="grok", handoff_comment_id="pull:lk-tel")
    with queue.connect() as conn:
        enqueue_pull_wake(conn, job, "lk-tel", now=clock[0])
        conn.commit()

    wake = claim_next(db, claimer="claude-orch", now=lambda: clock[0])
    assert wake is not None
    clock[0] += 12.5
    ok = finish_wake(
        db,
        wake.job_id,
        lease_token=wake.lease_token,
        result="answered",
        usage=OrchestratorUsage(
            harness="claude",
            repo=REPO,
            tokens_in=10_000,
            tokens_cached=4_000,
            tokens_out=1_500,
            cost_usd=0.42,
            turns=3,
        ),
        now=lambda: clock[0],
    )
    assert ok is True

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM job_telemetry WHERE job_id = ?", (job.id,)).fetchone()
    assert row is not None
    assert row["bot"] == ORCHESTRATOR_BOT
    assert row["harness"] == "claude"
    assert row["tokens_in"] == 10_000
    assert row["tokens_cached"] == 4_000
    assert row["tokens_out"] == 1_500
    assert row["cost_usd"] == pytest.approx(0.42)
    assert row["tokens_unknown"] == 0

    # Verify eval_report spend split captures orchestrator spend.
    with sqlite3.connect(db) as conn:
        tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        productive, wasted = eval_report._spend_split(conn, tables, eval_report.MetricWindow())
    assert productive == pytest.approx(0.42)
    assert wasted == pytest.approx(0.0)


def test_spend_counts_every_released_and_finished_attempt(tmp_path: Path) -> None:
    db, queue, clock = _make_db(tmp_path, now_val=1_800_000_000.0)
    job = _make_job(queue, action="clarify")
    assert queue.claim("grok") is not None
    queue.finish(job.id, owner="grok", handoff_comment_id="pull:lk-retry")
    with queue.connect() as conn:
        enqueue_pull_wake(conn, job, "lk-retry", now=clock[0])
        conn.commit()
    attempts = [
        (OrchestratorUsage(harness="codex", tokens_in=100, tokens_out=10, cost_usd=1.0), True),
        (OrchestratorUsage(harness="codex", tokens_in=50_000, tokens_out=5_000), True),
        (OrchestratorUsage(harness="codex", tokens_in=200, tokens_out=20, cost_usd=2.0), False),
    ]
    for usage, release in attempts:
        wake = claim_next(db, claimer="orch", now=lambda: clock[0])
        assert wake is not None
        clock[0] += 1
        assert finish_wake(
            db, job.id, lease_token=wake.lease_token, release=release, usage=usage, now=lambda: clock[0]
        )
    unreported = effective_cost_usd("codex", None, tokens_in=50_000, tokens_cached=0, tokens_out=5_000)
    assert unreported is not None and unreported > 0

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM job_telemetry WHERE job_id = ?", (job.id,)).fetchone()
        tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        productive, wasted = eval_report._spend_split(conn, tables, eval_report.MetricWindow())
    assert (row["tokens_in"], row["tokens_out"], row["turns"]) == (50_300, 5_030, 3)
    assert productive == pytest.approx(round(3.0 + unreported, 4))
    assert wasted == 0.0


def test_cli_wait_orchestrator_and_lifecycle_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, queue, clock = _make_db(tmp_path)
    # Empty inbox with --once prints nothing and exits 0.
    assert cli.main(["wait", "orchestrator", "--db", str(db), "--once"]) == 0
    assert capsys.readouterr().out == ""

    job = _make_job(queue, action="awaiting-ok")
    with queue.connect() as conn:
        enqueue_pull_wake(
            conn,
            job,
            "lk-cli",
            now=clock[0],
            inbox=DriverInbox(REPO, 99),
            body="mirror body",
        )
        conn.commit()

    assert cli.main(["wait", "orchestrator", "--db", str(db), "--claimer", "codex-orch", "--once"]) == 0
    out = capsys.readouterr().out
    claimed = json.loads(out)
    job_id = claimed["job_id"]
    token = claimed["lease_token"]

    assert cli.main(["orchestrator", "ack", job_id, "--token", token, "--db", str(db)]) == 0
    assert cli.main(["orchestrator", "heartbeat", job_id, "--token", token, "--db", str(db)]) == 0
    assert (
        cli.main(
            [
                "orchestrator",
                "finish",
                job_id,
                "--token",
                token,
                "--harness",
                "codex",
                "--tokens-in",
                "5000",
                "--tokens-out",
                "800",
                "--cost-usd",
                "0.11",
                "--result",
                "notified-max",
                "--db",
                str(db),
            ]
        )
        == 0
    )
    # Second finish on completed wake fails cleanly with exit 1.
    assert cli.main(["orchestrator", "finish", job_id, "--token", token, "--db", str(db)]) == 1


def test_orchestrator_skill_pinned_and_matches_full_wake_table() -> None:
    assert verify_pin(ROOT) == []
    skill_path = ROOT / "skills" / "factory" / "orchestrator" / "SKILL.md"
    assert skill_path.is_file()
    text = skill_path.read_text(encoding="utf-8")
    table_actions = set(re.findall(r"^\|\s*`([^`]+)`\s*\|", text, re.MULTILINE))
    assert set(GROK_WAKE_REASONS) <= table_actions
    assert "clarify" in table_actions
    assert "repeat-block" in table_actions
    assert "criterion-unsatisfiable" in table_actions
    signin = next(line for line in text.splitlines() if line.startswith("| `account-signin` |"))
    for phrase in ("`<account>@<device>`", "Tell Max once", "arrive together", "Settings",
                   "Never run a login or copy one from another device"):
        assert phrase in signin

    unit = ROOT / "deploy" / "orch-orchestrator.service"
    assert unit.is_file()
    unit_text = unit.read_text(encoding="utf-8")
    assert "orch wait orchestrator" in unit_text
    assert "Restart=always" in unit_text
