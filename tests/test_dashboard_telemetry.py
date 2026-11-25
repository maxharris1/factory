"""Job telemetry on the dashboard (issue #93, lib/dashboard_telemetry.py).

The job modal's feed and counters, "no telemetry" for a job without a row
(or a database without the table), the Efficiency rollup per harness, repo
and job, value checks and redaction, and the page script drawing the same.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lib import dashboard_telemetry, telemetry
from lib.dashboard import collect_snapshot, redact, render_pages, write_snapshot_pages
from tests.test_dashboard import GH_CLASSIC, NOW, seeded
from tests.test_dashboard_layout import BAR, _no_script, _run_page, _section, needs_node
from tests.test_dashboard_ticket_detail import TICKET_IDS, _quote, _ticket_server
from tests.test_telemetry import readable

CODEX = "maxharris1/cleffy#13"

STEPS = [
    {"at": NOW - 50, "kind": "tool", "status": "ok", "label": "edit 2 files"},
    {"at": NOW - 40, "kind": "command", "status": "error", "label": "command"},
    {"at": NOW - 5, "kind": "tool", "status": "running", "label": "mcp:github.get_pr"},
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
    article = _ticket_server(snapshot, CODEX)
    assert '<span class="t">Live run</span>' in article and '<p class="empty">no telemetry</p>' in article
    assert "lr-tile" not in article
    tokens = _section(_no_script(pages["efficiency.html"]), "tokens")
    assert "Cost, last 7 days" in tokens and "no telemetry" in tokens


def test_a_running_job_gets_its_counters_and_no_step_feed(tmp_path: Path) -> None:
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
    assert [(s["status"], s["label"], s["age_seconds"]) for s in item["steps"]] == [
        ("ok", "edit 2 files", 50),
        ("error", "command", 40),
        ("running", "mcp:github.get_pr", 5),
    ]
    article = _ticket_server(snapshot, CODEX)
    for needle in ("<b>25k</b><span>tokens in · 99% cached</span>", "<b>1.2k</b><span>tokens out</span>",
                   "<b>no data</b><span>cost so far</span>", "<b>2 files · 7 commands</b><span>touched and run</span>"):
        assert needle in article, needle
    for gone in ("shell ×7", "file_change", "mcp:github.get_pr", "edit 2 files", "tele-steps", "tele-tools",
                 "turns", "tests pass / fail"):
        assert gone not in article, gone


def test_a_job_with_a_row_but_no_events_shows_its_tiles(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(db, "r-codex-review", events=0, last_event_at=None, steps="[]", tokens_in=0, tokens_cached=0,
                  tokens_out=0, files_touched=0, commands=0)
    snapshot = collect_snapshot(db, log, now=NOW)
    assert _running(snapshot)["13"]["telemetry"]["events"] == 0
    article = _ticket_server(snapshot, CODEX)
    assert "<b>no data</b><span>tokens in</span>" in article
    assert "<b>0 files · 0 commands</b><span>touched and run</span>" in article


def test_secrets_and_bad_values_in_the_table_never_reach_the_snapshot(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(
        db,
        "r-codex-review",
        tool_calls="{not json",
        steps=json.dumps([
            {"at": NOW - 1, "kind": "evil", "status": "pwned", "label": f"echo {GH_CLASSIC}\n<script>",
             "title": f"$ echo {GH_CLASSIC}"},
            {"at": NOW - 1, "kind": "command", "status": "ok", "label": "deploy --pin $'walr''us'"},
            "not a step",
        ]),
        finished_at=NOW - 2,
        exit_reason=f"error: {GH_CLASSIC}",
        turns=-4,
        tokens_in="many",
        cost_usd=-1,
        harness="aider",
    )
    snapshot = collect_snapshot(db, log, now=NOW)
    text = json.dumps(snapshot)
    assert GH_CLASSIC not in text
    item = _running(snapshot)["13"]["telemetry"]
    assert item["tool_calls"] == [] and item["turns"] == 0 and item["tokens_in"] == 0
    assert item["cost_usd"] is None and item["harness"] == "other"
    assert item["steps"] == [
        {"kind": "tool", "status": "running", "label": "tool", "age_seconds": 1},
        {"kind": "command", "status": "ok", "label": "command", "age_seconds": 1},
    ]
    assert item["exit_reason"] == "finished"
    assert "walr" not in text
    html = render_pages(snapshot)["job.html"]
    assert GH_CLASSIC not in html and "<script>" not in _no_script(html).split("<body", 1)[1]
    article = _ticket_server(snapshot, CODEX)
    assert GH_CLASSIC not in article and "<script>" not in article


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
        steps=json.dumps([{"at": bad, "kind": "tool", "label": "x", "status": "ok"}]),
        subagents=json.dumps([{"kind": "Explore", "description": "d", "status": "finished",
                               "started_at": bad, "finished_at": bad}]),
    )
    snapshot = collect_snapshot(db, log, now=NOW)
    status = write_snapshot_pages(tmp_path / "out", snapshot)[0]
    data = _strict(status.read_text())
    rollup = data["telemetry"]
    assert rollup["totals"]["cost_usd"] == 0.0 and rollup["totals"]["priced_jobs"] == 0
    assert rollup["totals"]["tokens_in"] == 24763
    item = _running(data)["13"]["telemetry"]
    assert item["cost_usd"] is None and item["tokens_in"] == 0
    agent = rollup["recent"][0]["subagents"][0]
    assert agent["started_age_seconds"] is None and agent["finished_age_seconds"] is None


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
        "unmetered_jobs": 0,
    }
    harness = {row["name"]: row for row in rollup["by_harness"]}
    assert harness["codex"]["jobs"] == 1 and harness["codex"]["priced_jobs"] == 0
    assert harness["claude"]["cost_usd"] == 0.4713 and harness["claude"]["tokens_in"] == 6460
    repo = {row["name"]: row for row in rollup["by_repo"]}
    assert repo["maxharris1/cleffy"]["jobs"] == 2 and repo["maxharris1/factory"]["jobs"] == 1
    assert [row["job"] for row in rollup["recent"]] == ["f-claude", "d-claude"]  # newest first
    assert rollup["recent"][0]["exit_reason"] == "max turns reached; exit 1"
    page = _section(_no_script(render_pages(collect_snapshot(db, log, now=NOW))["efficiency.html"]), "tokens")
    for needle in ("last 7 days", "$0.47", "1 jobs report no cost: codex 1", "per harness", "per repo",
                   "maxharris1/factory"):
        assert needle in page, needle


def test_an_older_or_odd_table_reads_as_unreadable_not_an_error(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE job_telemetry (job_id TEXT, updated_at REAL)")
    snapshot = collect_snapshot(db, log, now=NOW)
    assert snapshot["telemetry"]["state"] == "unreadable"
    assert _running(snapshot)["13"]["telemetry"] is None



# --- the live run: plan, current task, last finished, sub-agents (#148) ------------------------

LIVE = ("plan", "current_task", "last_finished", "subagents")
PLAN = [
    {"text": "Read the ticket", "status": "done"},
    {"text": "Write the failing test", "status": "in_progress"},
    {"text": "Push and hand off", "status": "pending"},
]
AGENTS = [
    {"kind": "Explore", "description": "Find the reader", "status": "finished",
     "started_at": NOW - 90, "finished_at": NOW - 30},
    {"kind": "general-purpose", "description": "Run the suite", "status": "running",
     "started_at": NOW - 20, "finished_at": None},
]


def test_an_old_table_without_live_columns_reads_counters_and_no_live_run(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    old = telemetry.DDL
    for name in LIVE:
        old = re.sub(rf",\s*{name} TEXT", "", old)
    assert not any(name in old for name in LIVE)
    row = {"job_id": "r-codex-review", "bot": "codex-bot", "harness": "codex", "repo": "maxharris1/cleffy",
           "started_at": NOW - 300, "updated_at": NOW - 10, "last_event_at": NOW - 5,
           "elapsed_seconds": 290, "events": 42, "turns": 3, "tool_calls": "{}", "files_touched": 2,
           "commands": 7, "test_runs": 0, "tokens_in": 100, "tokens_cached": 0, "tokens_out": 10,
           "steps": json.dumps(STEPS)}
    with sqlite3.connect(db) as connection:
        connection.execute(old)
        connection.execute(
            f"INSERT INTO job_telemetry ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
    snapshot = collect_snapshot(db, log, now=NOW)
    assert snapshot["telemetry"]["state"] == "ok"
    item = _running(snapshot)["13"]["telemetry"]
    assert (item["turns"], item["commands"], item["tokens_in"], len(item["steps"])) == (3, 7, 100, 3)
    assert {name: item[name] for name in LIVE} == dict.fromkeys(LIVE)


def test_a_counters_only_row_has_no_live_run(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(db, "r-codex-review", **dict.fromkeys(LIVE))
    item = _running(collect_snapshot(db, log, now=NOW))["13"]["telemetry"]
    assert (item["turns"], item["tokens_in"]) == (3, 24763)
    assert {name: item[name] for name in LIVE} == dict.fromkeys(LIVE)


def test_a_full_row_gives_plan_current_task_last_finished_and_subagents(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(
        db, "r-codex-review", plan=json.dumps(PLAN), current_task="Write the failing test",
        last_finished="Read the ticket", subagents=json.dumps(AGENTS),
    )
    item = _running(collect_snapshot(db, log, now=NOW))["13"]["telemetry"]
    assert item["plan"] == PLAN
    assert item["current_task"] == "Write the failing test"
    assert item["last_finished"] == "Read the ticket"
    assert item["subagents"] == [
        {"kind": "Explore", "description": "Find the reader", "status": "finished",
         "started_age_seconds": 90, "finished_age_seconds": 30},
        {"kind": "general-purpose", "description": "Run the suite", "status": "running",
         "started_age_seconds": 20, "finished_age_seconds": None},
    ]


def test_an_empty_live_run_reads_as_empty_lists_and_no_task(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(db, "r-codex-review", plan="[]", current_task="", last_finished="", subagents="[]")
    item = _running(collect_snapshot(db, log, now=NOW))["13"]["telemetry"]
    assert (item["plan"], item["current_task"], item["last_finished"], item["subagents"]) == ([], None, None, [])


def test_a_finished_job_keeps_its_live_run_in_the_rollup(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(db, "d-claude-build-1", harness="claude", finished_at=NOW - 100, exit_code=0,
                  exit_reason="success; exit 0", plan=json.dumps(PLAN), last_finished="Read the ticket")
    recent = collect_snapshot(db, log, now=NOW)["telemetry"]["recent"]
    assert recent[0]["plan"] == PLAN and recent[0]["last_finished"] == "Read the ticket"


def test_live_run_values_are_validated_and_capped(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    long = "x" * 300
    plan = [{"text": f"step {n}", "status": "done"} for n in range(20)]
    plan[0] = {"text": "odd\nline\x07here", "status": "completed"}
    plan[1] = {"text": "", "status": "done"}
    plan[2] = "not an item"
    plan[3] = {"text": long, "status": "pending"}
    agents = [{"kind": "Explore", "description": f"agent {n}", "status": "finished",
               "started_at": NOW - 10, "finished_at": NOW - 5} for n in range(12)]
    agents[0] = {"kind": "", "description": "two\nlines", "status": "exploded",
                 "started_at": True, "finished_at": NOW - 1}
    agents[1] = ["not", "an", "agent"]
    agents[2] = {"kind": "k" * 99, "description": long, "status": "failed",
                 "started_at": "soon", "finished_at": None}
    add_telemetry(db, "r-codex-review", plan=json.dumps(plan), current_task=f"a\nb\tc {long}",
                  last_finished="   ", subagents=json.dumps(agents))
    add_telemetry(db, "d-claude-build-1", harness="claude", plan="{not json", finished_at=NOW - 100,
                  subagents=json.dumps({"kind": "Explore"}), current_task=b"\x00blob")
    snapshot = collect_snapshot(db, log, now=NOW)
    item = _running(snapshot)["13"]["telemetry"]
    assert len(item["plan"]) == 12
    assert item["plan"][0] == {"text": "odd linehere", "status": "pending"}
    assert item["plan"][1] == {"text": "x" * 119 + "…", "status": "pending"}
    assert item["plan"][2] == {"text": "step 4", "status": "done"}
    assert item["current_task"].startswith("a b c x") and len(item["current_task"]) == 120
    assert item["current_task"].endswith("…")
    assert item["last_finished"] is None
    assert len(item["subagents"]) == 8
    assert item["subagents"][0] == {"kind": "agent", "description": "two lines", "status": "running",
                                    "started_age_seconds": None, "finished_age_seconds": None}
    assert item["subagents"][1] == {"kind": "k" * 39 + "…", "description": "x" * 119 + "…",
                                    "status": "failed", "started_age_seconds": None,
                                    "finished_age_seconds": None}
    assert item["subagents"][2]["finished_age_seconds"] == 5
    other = snapshot["telemetry"]["recent"][0]
    assert other["job"] == "d-claude"
    assert (other["plan"], other["subagents"], other["current_task"]) == (None, None, None)


def test_secrets_in_live_run_text_never_reach_the_snapshot_or_pages(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    url = "https://deploy:hunter2pass@example.com/hook"
    email = "someone.private@example.org"
    add_telemetry(
        db, "r-codex-review",
        plan=json.dumps([{"text": f"push with {GH_CLASSIC}\n<script>", "status": "in_progress"}]),
        current_task=f"post to {url}",
        last_finished=f"mail {email} <script>alert(1)</script>",
        subagents=json.dumps([{"kind": GH_CLASSIC, "description": f"fetch {url} for {email}",
                               "status": "running", "started_at": NOW - 3, "finished_at": None}]),
    )
    snapshot = collect_snapshot(db, log, now=NOW)
    text = json.dumps(snapshot)
    for secret in (GH_CLASSIC, "hunter2pass", email, "example.com/hook"):
        assert secret not in text, secret
    item = _running(snapshot)["13"]["telemetry"]
    assert item["plan"][0]["text"].startswith("push with [redacted")
    assert item["current_task"].startswith("post to [redacted")
    assert "[redacted-email]" in item["last_finished"]
    assert "[redacted" in item["subagents"][0]["kind"]
    assert "[redacted" in item["subagents"][0]["description"]
    out = tmp_path / "out"
    write_snapshot_pages(out, snapshot)
    for page in out.rglob("*"):
        if page.is_file():
            body = page.read_text(errors="replace")
            for secret in (GH_CLASSIC, "hunter2pass", email):
                assert secret not in body, (page.name, secret)
            if page.suffix == ".html":
                assert "<script>alert" not in _no_script(body)


def _across_cap(limit: int) -> str:
    token = "ghp_" + "A1" * 18
    lead = "work " * ((limit - 12) // 5)
    return lead + "read " + token


def _split_by(control: str) -> object:
    return lambda limit: "read ghp_" + "A1" * 5 + control + "B2" * 13


@pytest.mark.parametrize(
    "secret",
    [_across_cap, *(_split_by(c) for c in ("\x00", "\x07", "\r", "\x1b", "\x7f", "\x9f"))],
    ids=["text-cap", "nul", "bel", "cr", "esc", "del", "c1"],
)
def test_a_secret_is_redacted_whole_before_any_later_transform(tmp_path: Path, secret) -> None:
    assert "ghp_" not in redact(secret(telemetry.TASK_TEXT_LIMIT))
    db, log = seeded(tmp_path)
    task = secret(telemetry.TASK_TEXT_LIMIT)
    kind = secret(telemetry.LABEL_LIMIT)
    repo = secret(100)
    add_telemetry(
        db, "r-codex-review", repo=repo,
        plan=json.dumps([{"text": task, "status": "in_progress"}]),
        current_task=task, last_finished=task,
        subagents=json.dumps([{"kind": kind, "description": task, "status": "running",
                               "started_at": NOW - 3, "finished_at": None}]),
    )
    snapshot = collect_snapshot(db, log, now=NOW)
    item = _running(snapshot)["13"]["telemetry"]
    fields = {
        "plan": item["plan"][0]["text"],
        "current_task": item["current_task"],
        "last_finished": item["last_finished"],
        "kind": item["subagents"][0]["kind"],
        "description": item["subagents"][0]["description"],
        "repo": item["repo"],
    }
    for name, value in fields.items():
        assert "ghp_" not in value and "[redacted" in value, (name, value)
    out = tmp_path / "out"
    write_snapshot_pages(out, snapshot)
    pages = [page for page in out.rglob("*") if page.is_file()]
    assert {"status.json", "efficiency.html"} <= {page.name for page in pages}
    for page in pages:
        assert "ghp_A1" not in page.read_text(errors="replace"), page.name


# --- the page script draws the same ---------------------------------------------------------


@needs_node
def test_script_draws_the_counters_on_the_ticket_page_and_the_rollup(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    add_telemetry(db, "r-codex-review")
    add_telemetry(
        db, "d-claude-build-1", harness="claude", cost_usd=0.4213, finished_at=NOW - 100,
        updated_at=NOW - 100, exit_code=0, exit_reason="success; exit 0",
    )
    snapshot = collect_snapshot(db, log, now=NOW)
    out = _run_page(tmp_path, snapshot, "ticket", TICKET_IDS, hash_="#t=" + _quote(CODEX))
    view = out["text"]["ticketview"]
    for needle in ("Live run", "25k", "tokens in · 99% cached", "1.2k", "2 files · 7 commands"):
        assert needle in view, needle
    assert re.search(r'div\.lr\n', out["dump"]["ticketview"])
    html = out["html"]["ticketview"]
    for gone in ("shell ×7", "file_change", "mcp:github.get_pr", "edit 2 files", "tele-steps", "tele-tools"):
        assert gone not in html, gone
    eff = _run_page(tmp_path, snapshot, "efficiency", BAR + ["hud", "health", "savings", "tokens"])
    tokens = eff["text"]["tokens"]
    for needle in ("Cost, last 7 days", "as reported by each harness", "per harness", "per repo",
                   "$0.42", "1 jobs report no cost: codex 1"):
        assert needle in tokens, needle


@needs_node
def test_script_shows_no_telemetry_for_a_job_without_a_row(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    snapshot = collect_snapshot(db, log, now=NOW)
    out = _run_page(tmp_path, snapshot, "ticket", TICKET_IDS, hash_="#t=" + _quote(CODEX))
    assert "no telemetry" in out["text"]["ticketview"]
    eff = _run_page(tmp_path, snapshot, "efficiency", BAR + ["hud", "health", "savings", "tokens"])
    assert "no telemetry" in eff["text"]["tokens"]


# --- the Tokens and cost panel: phone width, costs, compact tiles, no-cost note (#485) ---------


def _rollup(tmp_path: Path, *rows: dict) -> dict:
    db, log = seeded(tmp_path)
    for n, row in enumerate(rows):
        add_telemetry(db, f"d-job-{n:03d}", **{
            "finished_at": NOW - 100 - n, "updated_at": NOW - 100 - n, "exit_code": 0,
            "exit_reason": "success; exit 0", **row,
        })
    return collect_snapshot(db, log, now=NOW)


def _tokens(snapshot: dict) -> str:
    whole = _section(_no_script(render_pages(snapshot)["efficiency.html"]), "tokens")
    return whole[whole.index(">") + 1 : whole.rindex("<")]


def test_tokens_tables_each_scroll_on_their_own(tmp_path: Path) -> None:
    snapshot = _rollup(tmp_path, {"harness": "claude", "cost_usd": 1.5})
    page = render_pages(snapshot)["efficiency.html"]
    panel = _tokens(snapshot)
    assert panel.count("<table>") == 2
    assert len(re.findall(r'<div class="ef-scroll"><table>.*?</table></div>', panel)) == 2
    assert ".ef-scroll{overflow-x:auto}" in page


COSTS = [1057.64, 0.4213, 0.005, 12345.675]


@needs_node
def test_costs_print_the_same_in_both_renders(tmp_path: Path) -> None:
    snapshot = _rollup(tmp_path, *({"harness": "claude", "repo": f"acme/r{n}", "cost_usd": cost}
                                   for n, cost in enumerate(COSTS)))
    server = _tokens(snapshot)
    script = _run_page(tmp_path, snapshot, "efficiency", ["tokens"])["html"]["tokens"]
    costs = re.findall(r"\$[\d,.]+", server)
    assert "$1,057.64" in costs and "$0.42" in costs and "$0.01" in costs
    assert costs == re.findall(r"\$[\d,.]+", script)
    assert script == server


COMPACT = [
    (9_999, "9,999"),
    (10_000, "10K"),
    (45_200, "45.2K"),
    (99_995, "100K"),
    (999_950, "1M"),
    (812_400_000, "812.4M"),
    (2_635_600_591, "2.636B"),
]


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
@pytest.mark.parametrize(("count", "shown"), COMPACT)
def test_compact_token_counts(tmp_path: Path, render: str, count: int, shown: str) -> None:
    if render == "server":
        assert dashboard_telemetry._compact(count) == shown
        return
    snapshot = _rollup(tmp_path, {"harness": "claude"})
    snapshot["telemetry"]["totals"]["tokens_out"] = count
    html = _run_page(tmp_path, snapshot, "efficiency", ["tokens"])["html"]["tokens"]
    assert f'<div class="stat" title="{count:,}"><b>{shown}</b><span>tokens out</span></div>' in html


def test_token_tiles_are_compact_with_the_exact_count_in_title(tmp_path: Path) -> None:
    snapshot = _rollup(tmp_path, {
        "harness": "claude", "tokens_in": 2_635_600_591, "tokens_cached": 812_400_000, "tokens_out": 45_200,
        "cost_usd": 3.0,
    })
    snapshot["telemetry"]["totals"]["jobs"] = 1234
    panel = _tokens(snapshot)
    for tile in (
        '<div class="stat"><b>1,234</b><span>jobs</span></div>',
        '<div class="stat" title="2,635,600,591"><b>2.636B</b><span>tokens in</span>',
        '<div class="stat" title="45,200"><b>45.2K</b><span>tokens out</span></div>',
        '<div class="stat"><b>$3.00</b><span>cost</span></div>',
    ):
        assert tile in panel, tile
    harness = re.search(r'<tr><td><span class="bot claude">claude</span></td>(.*?)</tr>', panel).group(1)
    assert '<td class="num">2,635,600,591</td>' in harness


NO_COST = [
    {"harness": "codex"}, {"harness": "codex"}, {"harness": "claude"}, {"harness": "claude", "cost_usd": 0.5},
]


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_no_cost_note_counts_per_harness(tmp_path: Path, render: str) -> None:
    snapshot = _rollup(tmp_path, *NO_COST)
    panel = (_tokens(snapshot) if render == "server"
             else _run_page(tmp_path, snapshot, "efficiency", ["tokens"])["html"]["tokens"])
    assert '<p class="muted">3 jobs report no cost: codex 2, claude 1</p>' in panel
    assert "Codex does not" not in panel


# --- no command or argument text is stored or published (#100 r10) ---------------------------

# Max, on the round-9 REVISE: "Hide those fields in the job modal instead". A
# step keeps a fixed label; no command line, argument, pattern, path, query or
# error message is stored, so no quoting of a secret can be read back from it.
DIGITLESS_PROOF = "walrusHarborQuietLanternMeadow-Copper_Fjord"  # a valid mint() shape
LONG_SECRET = "walrusharbor" * 200
ESC_SECRET = "walrus\x1bharbor-pin"


def _ansi_c_chunks(value: str) -> str:
    return "".join("$'" + value[i : i + 4] + "'" for i in range(0, len(value), 4))


def _locale_chunks(value: str) -> str:
    return "".join('$"' + value[i : i + 4] + '"' for i in range(0, len(value), 4))


def _printf_q(value: str) -> str:
    return subprocess.check_output(
        ["/bin/bash", "--noprofile", "--norc", "-c", 'printf %q "$1"', "_", value], env={}
    ).decode()


def _bash_reads(word: str) -> str:
    return subprocess.check_output(
        ["/bin/bash", "--noprofile", "--norc", "-c", "printf %s " + word], env={}
    ).decode()


SHELL_FORMS = {
    "plain": lambda value: value,
    "ansi-c-chunks": _ansi_c_chunks,
    "locale-chunks": _locale_chunks,
    "printf-q": _printf_q,
    "quote-chunks": lambda value: "''".join(value[i : i + 8] for i in range(0, len(value), 8)),
    "json": json.dumps,
}
SECRETS = {
    "proof": ("ORCH_JOB_PROOF", DIGITLESS_PROOF),
    "worker-tag": ("ORCH_WORKER_TAG", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"),
    "registered": ("DEPLOY_PASSWORD", "otter" + "lantern-key"),
    "esc": ("DEPLOY_PASSWORD", ESC_SECRET),
    "long": ("DEPLOY_PASSWORD", LONG_SECRET),
}


def _events(harness: str, word: str) -> list[dict]:
    """One event for every field a harness can put free text in, each carrying `word`."""
    if harness == "claude":
        uses = [
            ("Bash", {"command": f"env ORCH_JOB_PROOF={word} orch job done", "description": word}),
            ("Edit", {"file_path": f"/work/{word}/app.py", "old_string": word, "new_string": word}),
            ("Grep", {"pattern": word, "path": f"/work/{word}"}),
            ("Read", {"file_path": f"src/{word}.py"}),
            ("Task", {"description": word, "prompt": word}),
        ]
        return [
            {"type": "system", "subtype": "init", "cwd": f"/work/{word}"},
            {"type": "assistant", "message": {"id": "m", "content": [
                {"type": "tool_use", "id": f"t{n}", "name": name, "input": data}
                for n, (name, data) in enumerate(uses)]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t0", "is_error": True, "content": word}]}},
            {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": word},
        ]
    items = [
        {"type": "command_execution", "command": f"env ORCH_JOB_PROOF={word} orch job done",
         "aggregated_output": word, "exit_code": 1, "status": "failed"},
        {"type": "file_change", "changes": [{"path": f"/work/{word}/a.py"}, {"path": f"{word}.py"}],
         "status": "completed"},
        {"type": "web_search", "query": word},
        {"type": "mcp_tool_call", "server": "github", "tool": "get_pr", "arguments": {"q": word},
         "status": "completed"},
        {"type": "error", "message": word},
    ]
    return [
        {"type": "turn.started"},
        *({"type": "item.completed", "item": {"id": f"i{n}", **item}} for n, item in enumerate(items)),
        {"type": "turn.failed", "error": {"message": word}},
        {"type": "error", "message": word},
    ]


LABELS = {
    "claude": ["command", "Edit", "Grep", "Read", "Task", "error during execution"],
    "codex": ["command", "edit 2 files", "web_search", "mcp:github.get_pr", "error", "turn failed", "error"],
}
EXIT_REASONS = {"claude": "error during execution; exit 0", "codex": "error; exit 0"}


def _fragments(secret: str) -> set[str]:
    return {secret[i : i + 4] for i in range(len(secret) - 3)}


@pytest.mark.parametrize("form", sorted(SHELL_FORMS))
@pytest.mark.parametrize("which", sorted(SECRETS))
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_no_command_or_argument_text_reaches_the_row_status_json_or_the_job_modal(
    tmp_path: Path, harness: str, which: str, form: str
) -> None:
    name, secret = SECRETS[which]
    word = SHELL_FORMS[form](secret)
    if form != "json":
        assert _bash_reads(word) == secret  # the form really is the secret, as the shell reads it
    db, log = seeded(tmp_path)
    session = telemetry.Session(
        connect=lambda: sqlite3.connect(db), job_id="r-codex-review", bot="codex-bot",
        repo="maxharris1/cleffy", harness=harness, flags=telemetry.stream_flags([harness]), raw_log=None,
        scrub=telemetry.Scrubber({name: secret}), log=lambda _: None,
        stdout=io.BytesIO(), clock=lambda: NOW - 5,
    )
    session.consume(io.BytesIO("".join(json.dumps(e) + "\n" for e in _events(harness, word)).encode()))
    session.flush()
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM job_telemetry").fetchone())
    stored = json.loads(row["steps"])
    assert "files" not in row
    assert [set(step) for step in stored] == [{"at", "kind", "status", "label"}] * len(stored)
    assert [step["label"] for step in stored] == LABELS[harness]

    snapshot = collect_snapshot(db, log, now=NOW)
    status = write_snapshot_pages(tmp_path / "out", snapshot)[0]
    item = _running(_strict(status.read_text()))["13"]["telemetry"]
    assert "files" not in item
    assert [step["label"] for step in item["steps"]] == LABELS[harness][-12:]
    assert [set(step) for step in item["steps"]] == [{"kind", "status", "label", "age_seconds"}] * len(item["steps"])
    session.finish(0)
    with sqlite3.connect(db) as connection:
        reason = connection.execute("SELECT exit_reason FROM job_telemetry").fetchone()[0]
    assert reason == EXIT_REASONS[harness]

    own = [json.dumps(row), json.dumps(item), reason]
    published = [status.read_text(), render_pages(snapshot)["job.html"], _ticket_server(snapshot, CODEX)]
    for text in own:
        assert not any(piece in text for piece in _fragments(secret)), text
    for text in own + published:
        assert secret not in text and word not in text
        assert not readable(secret, text)
