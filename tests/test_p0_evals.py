"""PR 0.4 regression tests: unified evals, chart parsing, Codex/Gemini cost pricing, SQL rollup, timer."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from lib import cli, dashboard_telemetry, eval_report, telemetry
from lib.dispatch.eval_log import _handled_token
from lib.queue import JobRequest
from tests.test_dispatcher import setup

ROOT = Path(__file__).resolve().parents[1]


def test_chart_transitions_parsed_directly_from_model_chart_mmd() -> None:
    parsed = eval_report.load_chart_transitions(ROOT / "model" / "chart.mmd")
    assert parsed == eval_report.CHART_TRANSITIONS
    assert ("intake", "questions") in parsed
    assert ("ship", "waiting-max") in parsed
    assert ("waiting-max", "ship") in parsed


def test_eval_log_sanitizes_synthetic_ids_with_colons_for_handled_token() -> None:
    assert _handled_token("102") == "102"
    assert _handled_token("t6:abc123") == "t6-abc123"
    assert _handled_token("ci:DEADBEEF:run-9") == "ci-deadbeef-run-9"
    assert _handled_token("-") is None
    assert _handled_token("") is None


def test_telemetry_estimates_codex_and_gemini_cost_and_tracks_claude_cache_writes() -> None:
    # Codex reports tokens without USD cost -> estimate_cost_usd prices it.
    codex_est = telemetry.estimate_cost_usd(
        "codex", tokens_in=1_000_000, tokens_cached=800_000, tokens_out=100_000
    )
    assert codex_est is not None and codex_est == pytest.approx(1.70, rel=1e-4)

    # Gemini fallback pricing also works.
    gemini_est = telemetry.estimate_cost_usd(
        "gemini", tokens_in=1_000_000, tokens_cached=0, tokens_out=100_000
    )
    assert gemini_est is not None and gemini_est == pytest.approx(2.25, rel=1e-4)

    # Claude cache write tokens tracked separately without changing tokens_in/tokens_cached.
    s = telemetry.Summary("claude", scrub=lambda text: text)
    s.feed(
        {
            "type": "result",
            "subtype": "success",
            "usage": {
                "input_tokens": 1000,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 2000,
                "output_tokens": 400,
            },
            "total_cost_usd": 0.123,
        },
        100.0,
    )
    assert s.tokens_in == 8000
    assert s.tokens_cached == 5000
    assert s.tokens_cache_write == 2000
    assert s.cost_usd == pytest.approx(0.123)


def test_dashboard_telemetry_rollup_does_not_truncate_at_row_limit_and_splits_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dashboard_telemetry, "ROW_LIMIT", 3)
    _dispatcher, queue = setup(tmp_path)
    now = 1_800_000_000.0
    with sqlite3.connect(queue.path) as conn:
        telemetry.ensure_table(conn)
        for i in range(6):
            jid = f"job-{i:04d}"
            state = "failed" if i == 5 else "done"
            harness = "codex" if i % 2 == 0 else "claude"
            cost = None if harness == "codex" else 1.0
            conn.execute(
                "INSERT INTO jobs (id, dedupe_key, repo, ticket_id, action, recipient, "
                "source_comment_id, timeout_seconds, generation, attempt, state, "
                "created_at, last_activity_at) VALUES (?, ?, 'owner/repo', ?, 'build', ?, 'c', 600, 1, 1, ?, ?, ?)",
                (jid, f"k-{i}", str(i), harness, state, now - 60, now - 60),
            )
            conn.execute(
                f"INSERT INTO {telemetry.TABLE} (job_id, bot, harness, repo, started_at, updated_at, "
                "finished_at, elapsed_seconds, events, turns, tool_calls, files_touched, commands, "
                "test_runs, tokens_in, tokens_cached, tokens_out, cost_usd, steps, tokens_unknown) "
                "VALUES (?, 'bot', ?, 'owner/repo', ?, ?, ?, 10, 1, 1, '{}', 0, 0, 0, 100000, 0, 10000, ?, '[]', 0)",
                (jid, harness, now - 60, now - 10 + i, now - 10 + i, cost),
            )
    conn = sqlite3.connect(queue.path)
    conn.row_factory = sqlite3.Row
    try:
        res = dashboard_telemetry.read_telemetry(conn, now)
    finally:
        conn.close()

    # All 6 rows must be included in rollup totals even though ROW_LIMIT was lowered to 3!
    assert res["rollup"]["totals"]["jobs"] == 6
    assert res["rollup"]["totals"]["tokens_in"] == 600_000
    assert res["rollup"]["spend"]["productive_usd"] > 0
    assert res["rollup"]["spend"]["wasted_usd"] == pytest.approx(1.0)
    # Estimated total includes Codex jobs priced via fallback table (> reported $3.00).
    assert res["rollup"]["spend"]["estimated_total_usd"] > res["rollup"]["totals"]["cost_usd"]


def test_eval_metrics_operational_fields_and_baseline_window_flags(
    tmp_path: Path, capsys
) -> None:
    _dispatcher, queue = setup(tmp_path)
    t_early = 1_700_000_000.0
    t_late = 1_700_600_000.0  # ~7 days later
    with sqlite3.connect(queue.path) as conn:
        telemetry.ensure_table(conn)
        conn.execute(
            "INSERT INTO poll_errors (repo, ticket_id, error, recorded_at) VALUES ('owner/repo', '1', 'err', ?)",
            (t_late + 10,),
        )
        conn.execute(
            "INSERT INTO ci_reruns (repo, ticket_id, head, outcome, job_ids, requested_at) "
            "VALUES ('owner/repo', '1', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'rerun', '[]', ?)",
            (t_late + 20,),
        )
        conn.execute(
            "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
            "VALUES ('owner/repo', '1', 'intake', 'building', ?, 'go'), "
            "('owner/repo', '1', 'building', 'merged', ?, 'ship'), "
            "('owner/repo', '2', 'building', 'stuck', ?, 't6')",
            (t_late, t_late + 7200, t_late + 30),
        )
        # Duplicate grok wakes on same source_comment_id -> 1 wasted wake.
        for idx in (1, 2):
            conn.execute(
                "INSERT INTO jobs (id, dedupe_key, repo, ticket_id, action, recipient, "
                "source_comment_id, timeout_seconds, generation, attempt, state, "
                "created_at, last_activity_at) VALUES (?, ?, 'owner/repo', '1', 'clarify', 'grok', 'c1', 600, 1, ?, 'done', ?, ?)",
                (f"w{idx}", f"wk-{idx}", idx, t_late + idx, t_late + idx),
            )
        conn.execute(
            "INSERT INTO jobs (id, dedupe_key, repo, ticket_id, action, recipient, "
            "source_comment_id, timeout_seconds, generation, attempt, state, "
            "created_at, last_activity_at) VALUES ('f1', 'fk-1', 'owner/repo', '2', 'build', 'codex', 'c2', 600, 1, 1, 'failed', ?, ?)",
            (t_late + 5, t_late + 5),
        )
        conn.execute(
            f"INSERT INTO {telemetry.TABLE} (job_id, bot, harness, repo, started_at, updated_at, "
            "elapsed_seconds, events, turns, tool_calls, files_touched, commands, test_runs, "
            "tokens_in, tokens_cached, tokens_out, cost_usd, steps, tokens_unknown) "
            "VALUES ('f1', 'codex-bot', 'codex', 'owner/repo', ?, ?, 60, 1, 1, '{}', 0, 0, 0, 200000, 0, 50000, NULL, '[]', 0)",
            (t_late + 5, t_late + 65),
        )

    from datetime import datetime, timezone

    def iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rc = cli.main(
        [
            "eval-report",
            "--db",
            str(queue.path),
            "--since",
            iso(t_late - 60),
            "--until",
            iso(t_late + 10000),
            "--baseline",
            str(queue.path),
            "--baseline-since",
            iso(t_early - 60),
            "--baseline-until",
            iso(t_early + 3600),
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    m = out["metrics"]
    b = out["baseline"]["metrics"]
    assert m["poll-errors"] == 1 and b["poll-errors"] == 0
    assert m["ci-reruns"] == 1 and b["ci-reruns"] == 0
    assert m["stuck-jobs"] == 1 and b["stuck-jobs"] == 0
    assert m["failed-jobs"] == 1 and b["failed-jobs"] == 0
    assert m["wasted-wakes"] == 1 and b["wasted-wakes"] == 0
    assert m["cycle-time-p50-hours"] == pytest.approx(2.0)
    assert m["cycle-time-p90-hours"] == pytest.approx(2.0)
    assert b["cycle-time-p50-hours"] is None
    assert m["wasted-cost-usd"] > 0 and b["wasted-cost-usd"] == 0.0


def test_monday_eval_systemd_units_exist() -> None:
    timer = (ROOT / "deploy" / "factory-weekly-eval.timer").read_text(encoding="utf-8")
    service = (ROOT / "deploy" / "factory-weekly-eval.service").read_text(encoding="utf-8")
    script = (ROOT / "deploy" / "weekly-eval.sh").read_text(encoding="utf-8")
    assert "OnCalendar=Mon *-*-* 08:00:00 America/New_York" in timer
    assert "weekly-eval.sh" in service
    assert "eval-report --db" in script and "--json" in script


STUB_ORCH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$STUB_LOG"
if [[ "$1" == eval-report ]]; then echo '{}'; fi
if [[ "$1" == dashboard ]]; then exit "${STUB_PUSH_EXIT:-0}"; fi
"""


def test_weekly_eval_script_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "bin").mkdir()
    script = shutil.copy(ROOT / "deploy" / "weekly-eval.sh", repo / "deploy" / "weekly-eval.sh")
    stub = repo / "bin" / "orch"
    stub.write_text(STUB_ORCH, encoding="utf-8")
    stub.chmod(0o755)
    state = tmp_path / "state"
    evals = state / "evals"
    evals.mkdir(parents=True)
    seeded = [(date(2000, 1, 3) + timedelta(weeks=n)).isoformat() for n in range(14)]
    for day in seeded:
        (evals / f"weekly-{day}.json").write_text("{}", encoding="utf-8")
        (evals / f"weekly-{day}.txt").write_text("", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (evals / "weekly-2000-01-01.json").symlink_to(outside)
    (evals / "notes.json").write_text("{}", encoding="utf-8")
    log = tmp_path / "orch.log"
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("ORCH_")
    }
    env.update(HOME=str(tmp_path / "home"), ORCH_STATE_DIR=str(state), STUB_LOG=str(log))

    result = subprocess.run(["bash", script], env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    stamp = re.fullmatch(r"weekly-(\d{4}-\d{2}-\d{2})\.json", os.readlink(evals / "latest.json"))
    assert stamp is not None and stamp.group(1) not in seeded
    kept = sorted([stamp.group(1), *seeded], reverse=True)[:12]
    for ext in ("json", "txt"):
        regular = [p.name for p in evals.glob(f"weekly-*.{ext}") if not p.is_symlink()]
        assert sorted(regular) == sorted(f"weekly-{day}.{ext}" for day in kept)
    assert os.readlink(evals / "latest.txt") == f"weekly-{stamp.group(1)}.txt"
    assert (evals / "weekly-2000-01-01.json").is_symlink() and outside.exists()
    assert (evals / "notes.json").exists()
    assert not [p.name for p in evals.iterdir() if p.name.endswith(".tmp")]
    assert log.read_text(encoding="utf-8").splitlines()[-1] == (
        f"dashboard push --once --db {state / 'orch.db'} --evals-dir {evals}"
    )

    env["STUB_PUSH_EXIT"] = "1"
    again = subprocess.run(["bash", script], env=env, capture_output=True, text=True, check=False)
    assert again.returncode == 0, again.stderr
