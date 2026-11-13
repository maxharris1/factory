"""Job telemetry on the dashboard (issue #93, lib/dashboard_telemetry.py).

The job modal's feed and counters, "no telemetry" for a job without a row
(or a database without the table), the Efficiency rollup per harness, repo
and job, value checks and redaction, and the page script drawing the same.
"""

from __future__ import annotations

import io
import json
import re
import shlex
import sqlite3
from pathlib import Path

import pytest

from lib import telemetry
from lib.dashboard import collect_snapshot, render_pages, write_snapshot_pages
from tests.test_dashboard import GH_CLASSIC, NOW, seeded
from tests.test_dashboard_layout import BAR, _no_script, _run_page, _section, needs_node
from tests.test_telemetry import ESCAPED_SECRETS, QUOTING_ONLY_SECRETS, readable

STEPS = [
    {"at": NOW - 50, "kind": "tool", "title": "Read lib/app.py", "status": "ok"},
    {"at": NOW - 40, "kind": "command", "title": "$ python3 -m pytest -q", "status": "error"},
    {"at": NOW - 5, "kind": "command", "title": "$ git status", "status": "running"},
]


def add_telemetry(db: Path, job_id: str, **values) -> None:
    row = {
        "job_id": job_id,
        "bot": "codex-bot",
        "harness": "codex",
        "repo": "maxharris1/cleffy",
        "started_at": NOW - 300,
        "updated_at": NOW - 10,
        "last_event_at": NOW - 5,
        "finished_at": None,
        "elapsed_seconds": 290.4,
        "events": 42,
        "turns": 3,
        "tool_calls": json.dumps({"shell": 7, "file_change": 2}),
        "files_touched": 2,
        "files": json.dumps(["lib/app.py", "tests/test_app.py"]),
        "commands": 7,
        "test_runs": 2,
        "tests_passed": 40,
        "tests_failed": 1,
        "tokens_in": 24763,
        "tokens_cached": 24448,
        "tokens_out": 1220,
        "cost_usd": None,
        "exit_code": None,
        "exit_reason": None,
        "steps": json.dumps(STEPS),
    }
    row.update(values)
    with sqlite3.connect(db) as connection:
        telemetry.ensure_table(connection)
        connection.execute(
            f"INSERT OR REPLACE INTO job_telemetry ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )


def _running(snapshot: dict) -> dict:
    return {row["ticket_id"]: row for row in snapshot["running"]}


def test_a_database_without_the_table_shows_no_telemetry(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    snapshot = collect_snapshot(db, log, now=NOW)
    assert all(row["telemetry"] is None for row in snapshot["running"] + snapshot["queued"])
    assert snapshot["telemetry"]["state"] == "none"
    assert snapshot["telemetry"]["totals"]["jobs"] == 0
    pages = render_pages(snapshot)
    job = _no_script(pages["job.html"])
    article = _section(job, "job-r-codex-")
    assert '<h3 class="tele-head">telemetry</h3><p class="empty">no telemetry</p>' in article
    tokens = _section(_no_script(pages["efficiency.html"]), "tokens")
    assert "Tokens and cost" in tokens and "no telemetry" in tokens


def test_a_running_job_gets_its_counters_and_recent_steps(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(db, "r-codex-review")
    snapshot = collect_snapshot(db, log, now=NOW)
    item = _running(snapshot)["13"]["telemetry"]
    assert _running(snapshot)["14"]["telemetry"] is None  # a job without a row
    assert item["job"] == "r-codex-" and item["harness"] == "codex"
    assert (item["turns"], item["commands"], item["files_touched"]) == (3, 7, 2)
    assert item["tool_calls"] == [{"name": "shell", "count": 7}, {"name": "file_change", "count": 2}]
    assert item["tool_calls_total"] == 9
    assert (item["tests_passed"], item["tests_failed"]) == (40, 1)
    assert (item["tokens_in"], item["tokens_cached"], item["tokens_out"]) == (24763, 24448, 1220)
    assert item["cost_usd"] is None
    assert item["elapsed_seconds"] == 290
    assert item["last_activity_age_seconds"] == 5 and item["updated_age_seconds"] == 10
    assert item["finished"] is False and item["exit_reason"] is None
    assert [(s["status"], s["title"], s["age_seconds"]) for s in item["steps"]] == [
        ("ok", "Read lib/app.py", 50),
        ("error", "$ python3 -m pytest -q", 40),
        ("running", "$ git status", 5),
    ]
    article = _section(_no_script(render_pages(snapshot)["job.html"]), "job-r-codex-")
    for needle in ("codex · last activity 5s ago", "<b>3</b><span>turns</span>",
                   '<b class="bad">40 / 1</b><span>tests pass / fail</span>',
                   "<b>24,763 / 1,220</b><span>tokens in / out</span>", "shell ×7",
                   "$ git status", "4m 50s"):
        assert needle in article, needle
    # The newest step first.
    assert article.index("$ git status") < article.index("Read lib/app.py")


def test_a_job_with_a_row_but_no_events_says_no_telemetry_yet(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(db, "r-codex-review", events=0, last_event_at=None, steps="[]")
    snapshot = collect_snapshot(db, log, now=NOW)
    assert _running(snapshot)["13"]["telemetry"]["events"] == 0
    article = _section(_no_script(render_pages(snapshot)["job.html"]), "job-r-codex-")
    assert "codex · no events yet" in article and "no telemetry yet" in article


def test_secrets_and_bad_values_in_the_table_never_reach_the_snapshot(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(
        db,
        "r-codex-review",
        tool_calls="{not json",
        files=json.dumps([f"token={GH_CLASSIC}"]),
        steps=json.dumps([
            {"at": NOW - 1, "kind": "evil", "status": "pwned", "title": f"$ echo {GH_CLASSIC}\n<script>"},
            "not a step",
        ]),
        turns=-4,
        tokens_in="many",
        cost_usd=-1,
        harness="gemini",
    )
    snapshot = collect_snapshot(db, log, now=NOW)
    text = json.dumps(snapshot)
    assert GH_CLASSIC not in text
    item = _running(snapshot)["13"]["telemetry"]
    assert item["tool_calls"] == [] and item["turns"] == 0 and item["tokens_in"] == 0
    assert item["cost_usd"] is None and item["harness"] == "other"
    assert item["steps"] == [
        {"kind": "tool", "status": "running", "title": item["steps"][0]["title"], "age_seconds": 1}
    ]
    assert "\n" not in item["steps"][0]["title"]
    html = render_pages(snapshot)["job.html"]
    assert GH_CLASSIC not in html and "<script>" not in _no_script(html).split("<body", 1)[1]


def _strict(text: str) -> object:
    def refuse(name: str) -> object:
        raise ValueError(f"nonstandard JSON number {name}")

    return json.loads(text, parse_constant=refuse)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
def test_nonfinite_numbers_in_the_table_never_reach_published_json(tmp_path: Path, bad: float) -> None:
    db, log = seeded(tmp_path)
    numbers = ("cost_usd", "tokens_in", "tokens_cached", "tokens_out", "turns", "events",
               "elapsed_seconds", "tests_passed", "last_event_at", "updated_at")
    add_telemetry(db, "r-codex-review", **{name: bad for name in numbers if name != "updated_at"})
    add_telemetry(
        db, "d-claude-build-1", harness="claude", cost_usd=bad,
        finished_at=NOW - 100, exit_code=0, exit_reason="success; exit 0",
        steps=json.dumps([{"at": bad, "kind": "tool", "title": "x", "status": "ok"}]),
    )
    snapshot = collect_snapshot(db, log, now=NOW)
    status = write_snapshot_pages(tmp_path / "out", snapshot)[0]
    data = _strict(status.read_text())
    rollup = data["telemetry"]
    assert rollup["totals"]["cost_usd"] == 0.0 and rollup["totals"]["priced_jobs"] == 0
    assert rollup["totals"]["tokens_in"] == 24763
    item = _running(data)["13"]["telemetry"]
    assert item["cost_usd"] is None and item["tokens_in"] == 0


def test_efficiency_rolls_up_tokens_and_cost_per_harness_repo_and_job(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(db, "r-codex-review")  # running codex job, no cost
    add_telemetry(
        db, "d-claude-build-1", bot="claude-bot", harness="claude", repo="maxharris1/factory",
        tokens_in=5460, tokens_cached=5000, tokens_out=300, cost_usd=0.4213,
        finished_at=NOW - 100, updated_at=NOW - 100, exit_code=0, exit_reason="success; exit 0",
    )
    add_telemetry(
        db, "f-claude-build", bot="claude-bot", harness="claude", repo="maxharris1/cleffy",
        tokens_in=1000, tokens_cached=0, tokens_out=100, cost_usd=0.05,
        finished_at=NOW - 50, updated_at=NOW - 50, exit_code=1, exit_reason="max turns reached; exit 1",
    )
    add_telemetry(  # older than the window: left out
        db, "d-codex-review-1", tokens_in=10**9, finished_at=NOW - 8 * 86400,
        updated_at=NOW - 8 * 86400, exit_code=0, exit_reason="completed; exit 0",
    )
    rollup = collect_snapshot(db, log, now=NOW)["telemetry"]
    assert rollup["state"] == "ok" and rollup["window_days"] == 7
    assert rollup["totals"] == {
        "jobs": 3, "tokens_in": 24763 + 5460 + 1000, "tokens_cached": 24448 + 5000,
        "tokens_out": 1220 + 300 + 100, "cost_usd": 0.4713, "priced_jobs": 2,
    }
    harness = {row["name"]: row for row in rollup["by_harness"]}
    assert harness["codex"]["jobs"] == 1 and harness["codex"]["priced_jobs"] == 0
    assert harness["claude"]["cost_usd"] == 0.4713 and harness["claude"]["tokens_in"] == 6460
    repo = {row["name"]: row for row in rollup["by_repo"]}
    assert repo["maxharris1/cleffy"]["jobs"] == 2 and repo["maxharris1/factory"]["jobs"] == 1
    assert [row["job"] for row in rollup["recent"]] == ["f-claude", "d-claude"]  # newest first
    assert rollup["recent"][0]["exit_reason"] == "max turns reached; exit 1"
    page = _section(_no_script(render_pages(collect_snapshot(db, log, now=NOW))["efficiency.html"]), "tokens")
    for needle in ("last 7 days", "$0.47", "1 jobs report no cost", "per harness", "per repo",
                   "recent jobs", '<td class="bad">max turns reached; exit 1</td>',
                   "<td>success; exit 0</td>", "maxharris1/factory"):
        assert needle in page, needle


def test_an_older_or_odd_table_reads_as_unreadable_not_an_error(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE job_telemetry (job_id TEXT, updated_at REAL)")
    snapshot = collect_snapshot(db, log, now=NOW)
    assert snapshot["telemetry"]["state"] == "unreadable"
    assert _running(snapshot)["13"]["telemetry"] is None


# --- the page script draws the same ---------------------------------------------------------


@needs_node
def test_script_draws_the_feed_on_the_job_page_and_the_rollup(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(db, "r-codex-review")
    add_telemetry(
        db, "d-claude-build-1", harness="claude", cost_usd=0.4213, finished_at=NOW - 100,
        updated_at=NOW - 100, exit_code=0, exit_reason="success; exit 0",
    )
    snapshot = collect_snapshot(db, log, now=NOW)
    out = _run_page(tmp_path, snapshot, "job", BAR + ["hud", "jobs", "jobview", "flow-template"],
                    hash_="#job-r-codex-")
    view = out["text"]["jobview"]
    for needle in ("telemetry", "last activity", "turns", "tool calls", "40 / 1", "24,763 / 1,220",
                   "shell ×7", "$ python3 -m pytest -q", "$ git status"):
        assert needle in view, needle
    dump = out["dump"]["jobview"]
    assert re.search(r'section\.telemetry', dump) and "span.chip.bad" in dump
    assert view.index("$ git status") < view.index("Read lib/app.py")
    eff = _run_page(tmp_path, snapshot, "efficiency", BAR + ["hud", "health", "savings", "tokens"])
    tokens = eff["text"]["tokens"]
    for needle in ("Tokens and cost", "last 7 days", "per harness", "per repo", "recent jobs",
                   "$0.42", "success; exit 0", "1 jobs report no cost"):
        assert needle in tokens, needle


@needs_node
def test_script_shows_no_telemetry_for_a_job_without_a_row(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    snapshot = collect_snapshot(db, log, now=NOW)
    out = _run_page(tmp_path, snapshot, "job", BAR + ["hud", "jobs", "jobview", "flow-template"],
                    hash_="#job-r-codex-")
    assert "no telemetry" in out["text"]["jobview"]
    eff = _run_page(tmp_path, snapshot, "efficiency", BAR + ["hud", "health", "savings", "tokens"])
    assert "no telemetry" in eff["text"]["tokens"]


@pytest.mark.parametrize(
    "secret, command",
    [(secret, shlex.join(["deploy", "--pin", secret])) for secret in ["walrus'" + "harbor", *QUOTING_ONLY_SECRETS]]
    + [(secret, shlex.join(["deploy", "--pin", json.dumps({"pin": secret})])) for secret in ESCAPED_SECRETS],
)
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_quoted_or_escaped_env_secret_never_reaches_status_json(
    tmp_path: Path, harness: str, secret: str, command: str
) -> None:
    if harness == "claude":
        event = {"type": "assistant", "message": {"id": "m", "content": [
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": command}}]}}
    else:
        event = {"type": "item.completed", "item": {
            "id": "t", "type": "command_execution", "command": command, "exit_code": 0, "status": "completed"}}
    db, log = seeded(tmp_path)
    session = telemetry.Session(
        connect=lambda: sqlite3.connect(db), job_id="r-codex-review", bot="codex-bot",
        repo="maxharris1/cleffy", harness=harness, argv=[harness], raw_log=None,
        scrub=telemetry.Scrubber({"DEPLOY_PASSWORD": secret}), log=lambda _: None,
        stdout=io.BytesIO(), clock=lambda: NOW - 5,
    )
    session.consume(io.BytesIO((json.dumps(event) + "\n").encode()))
    session.flush()
    status = write_snapshot_pages(tmp_path / "out", collect_snapshot(db, log, now=NOW))[0]
    title = _running(_strict(status.read_text()))["13"]["telemetry"]["steps"][-1]["title"]
    assert title.startswith("$ deploy --pin") and "[redacted-env]" in title
    assert not readable(secret, title), title


DIGITLESS_PROOF = "walrusHarborQuietLanternMeadow-Copper_Fjord"  # a valid mint() shape


@pytest.mark.parametrize("name, value", [
    ("ORCH_JOB_PROOF", DIGITLESS_PROOF),
    ("ORCH_JOB_PROOF", "k7"),
    ("ORCH_JOB_PROOF", "A" * 43),
    ("ORCH_WORKER_TAG", "0123456789abcdef0123456789abcdef"),
])
@pytest.mark.parametrize("form", ["plain", "chunks", "quoted", "json"])
def test_a_per_run_secret_orch_hands_the_harness_never_reaches_status_json(
    tmp_path: Path, name: str, value: str, form: str
) -> None:
    """#100 r8: the run's proof and worker tag are secret whatever their value."""
    word = {
        "plain": value,
        "chunks": "''".join(value[i : i + 8] for i in range(0, len(value), 8)),
        "quoted": shlex.quote(value + "'"),
        "json": json.dumps(value),
    }[form]
    event = {"type": "item.completed", "item": {
        "id": "x", "type": "command_execution", "status": "completed", "exit_code": 0,
        "command": f"env {name}={word} orch job done"}}
    db, log = seeded(tmp_path)
    session = telemetry.Session(
        connect=lambda: sqlite3.connect(db), job_id="r-codex-review", bot="codex-bot",
        repo="maxharris1/cleffy", harness="codex", argv=["codex"], raw_log=None,
        scrub=telemetry.Scrubber({name: value}), log=lambda _: None,
        stdout=io.BytesIO(), clock=lambda: NOW - 5,
    )
    session.consume(io.BytesIO((json.dumps(event) + "\n").encode()))
    session.flush()
    status = write_snapshot_pages(tmp_path / "out", collect_snapshot(db, log, now=NOW))[0]
    title = _running(_strict(status.read_text()))["13"]["telemetry"]["steps"][-1]["title"]
    assert title.startswith(f"$ env {name}=") and "[redacted-env]" in title, title
    assert not readable(value, title), title
