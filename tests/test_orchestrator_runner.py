from __future__ import annotations

import contextlib
import io
import json
import shlex
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from lib import cli
from lib.orchestrator_pull import claim_next, enqueue_pull_wake
from lib.orchestrator_runner import run_wakes
from lib.queue import JobRequest, SQLiteJobQueue
from lib.waiter import StopFlag, load_harness_command

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "orch-orchestrator.service"

HARNESS = """\
import json, os, sys, time
root, record, code, pause, finish = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[5] == "finish"
sys.path.insert(0, root)
import sqlite3
def lease():
    with sqlite3.connect(os.environ["ORCH_DB"]) as c:
        return c.execute("select lease_until from driver_inbox").fetchone()[0]
stdin = sys.stdin.read()
first = lease()
time.sleep(pause)
lease_until = [first, lease()]
with open(os.environ["ORCH_WAKE_FILE"], encoding="utf-8") as f:
    wake = json.load(f)
env = {k: v for k, v in os.environ.items() if k.startswith("ORCH_")}
with open(record, "w", encoding="utf-8") as f:
    json.dump({"env": env, "stdin": stdin, "wake": wake, "lease_until": lease_until}, f)
if finish:
    from lib import cli
    sys.exit(cli.main([
        "orchestrator", "finish", env["ORCH_WAKE_JOB_ID"], "--token", env["ORCH_WAKE_LEASE_TOKEN"],
        "--db", env["ORCH_DB"], "--harness", "codex", "--tokens-in", "10", "--tokens-out", "5",
        "--cost-usd", "1.5", "--result", "harness done",
    ]))
sys.exit(code)
"""


@pytest.fixture(autouse=True)
def _restore_signals():
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    yield
    for signum, handler in saved.items():
        signal.signal(signum, handler)


def paid_wake(tmp_path):
    db = tmp_path / "spend.db"
    queue = SQLiteJobQueue(db, clock=lambda: 100)
    job = queue.enqueue(JobRequest("example/repo", "42", "clarify", "grok", "5", 60), dedupe_key="retry")
    queue.claim("grok")
    queue.finish(job.id, owner="grok", handoff_comment_id="pull:spend")
    with queue.connect() as c:
        enqueue_pull_wake(c, job, "spend", now=100)
    return db, job


def harness_config(tmp_path, *, code=0, pause=0.0, finish=False):
    script = tmp_path / "harness.py"
    script.write_text(HARNESS, encoding="utf-8")
    record = tmp_path / "record.json"
    command = [sys.executable, str(script), str(ROOT), str(record), str(code), str(pause),
               "finish" if finish else "exit"]
    config = tmp_path / "waiter-orchestrator.yml"
    config.write_text(json.dumps({"bot": "orchestrator", "command": command}), encoding="utf-8")
    config.chmod(0o600)
    return config, record


def unit_argv():
    unit = UNIT.read_text(encoding="utf-8")
    exec_start = next(line.split("=", 1)[1] for line in unit.splitlines() if line.startswith("ExecStart="))
    return shlex.split(exec_start)[1:]


def run_unit(db, config, *extra):
    stderr = io.StringIO()
    with patch.object(cli, "_retired", return_value=None), contextlib.redirect_stderr(stderr):
        code = cli.main([*unit_argv(), "--db", str(db), "--harness-config", str(config), "--once", *extra])
    return code, stderr.getvalue()


def inbox_row(db):
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        return c.execute("select pull_state, result from driver_inbox").fetchone()


def test_shipped_unit_delivers_wake_to_harness(tmp_path):
    db, job = paid_wake(tmp_path)
    config, record = harness_config(tmp_path)
    code, stderr = run_unit(db, config)
    assert code == 0, stderr
    assert inbox_row(db)["pull_state"] == "done"
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["env"]["ORCH_WAKE_JOB_ID"] == job.id
    assert seen["env"]["ORCH_DB"] == str(db)
    assert seen["wake"]["job_id"] == job.id
    assert seen["wake"]["lease_token"] == seen["env"]["ORCH_WAKE_LEASE_TOKEN"]
    assert "skills/factory/orchestrator/SKILL.md" in seen["stdin"]
    assert "orch orchestrator finish" in seen["stdin"]
    assert job.id in stderr


def test_failed_harness_releases_wake_for_retry(tmp_path):
    db, job = paid_wake(tmp_path)
    config, _ = harness_config(tmp_path, code=3)
    code, stderr = run_unit(db, config)
    assert code == 0, stderr
    assert inbox_row(db)["pull_state"] == "queued"
    again = claim_next(db, claimer="other")
    assert again is not None and again.job_id == job.id


def test_harness_finish_records_spend_once(tmp_path):
    db, job = paid_wake(tmp_path)
    config, _ = harness_config(tmp_path, finish=True)
    code, stderr = run_unit(db, config)
    assert code == 0, stderr
    row = inbox_row(db)
    assert (row["pull_state"], row["result"]) == ("done", "harness done")
    with sqlite3.connect(db) as c:
        spend = c.execute(
            "select bot, harness, cost_usd from job_telemetry where job_id = ?", (job.id,)
        ).fetchall()
    assert spend == [("orchestrator", "codex", 1.5)]


def test_runner_renews_lease_while_harness_runs(tmp_path):
    db, _ = paid_wake(tmp_path)
    config, record = harness_config(tmp_path, pause=2.6)
    code, stderr = run_unit(db, config, "--lease-seconds", "6")
    assert code == 0, stderr
    seen = json.loads(record.read_text(encoding="utf-8"))
    first, last = seen["lease_until"]
    assert last > first
    assert inbox_row(db)["pull_state"] == "done"


def test_once_with_empty_inbox_returns_without_harness(tmp_path):
    db, job = paid_wake(tmp_path)
    claim_next(db, claimer="other")
    config, record = harness_config(tmp_path)
    code, stderr = run_unit(db, config)
    assert code == 0, stderr
    assert not record.exists()


def test_harness_config_needs_autostart(tmp_path):
    db, _ = paid_wake(tmp_path)
    config, _ = harness_config(tmp_path)
    with patch.object(cli, "_retired", return_value=None), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(["wait", "orchestrator", "--db", str(db), "--harness-config", str(config), "--once"])
    assert code == 2


def test_autostart_refuses_missing_harness_config(tmp_path):
    db, _ = paid_wake(tmp_path)
    with patch.object(cli, "_retired", return_value=None), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(["wait", "orchestrator", "--autostart", "--db", str(db),
                         "--harness-config", str(tmp_path / "missing.yml"), "--once"])
    assert code == 2
    assert inbox_row(db)["pull_state"] == "queued"


def test_stopping_the_runner_ends_the_harness_and_requeues_its_wake(tmp_path):
    db, job = paid_wake(tmp_path)
    config, record = harness_config(tmp_path, pause=30.0)
    stop = StopFlag()
    threading.Timer(1.0, stop.set).start()
    started = time.monotonic()
    run_wakes(
        db,
        load_harness_command(config, "orchestrator").command,
        claimer="orch",
        lease_seconds=300,
        poll_seconds=0.1,
        once=True,
        stop=stop,
        log=lambda _line: None,
    )
    assert time.monotonic() - started < 20
    assert not record.exists()
    assert inbox_row(db)["pull_state"] == "queued"
    assert claim_next(db, claimer="next-runner") is not None
