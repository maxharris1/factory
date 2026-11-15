"""#149: the Live run panel on the ticket page (RunTape.dc.html).

Tiles come from #100's counters, the headline, plan and sub-agents from
#148's live fields. No telemetry says so; a counters-only row shows the
tiles and no task text; nothing from the artboard's sample is ever shown.
The page script builds the same markup as the server.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot, render_pages
from lib.dashboard_pages.ticket_detail import compact
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_layout import JOB, _history, _no_script, _run_page, _section, needs_node
from tests.test_dashboard_telemetry import add_telemetry

PANEL = "liverun-job-h-review"
LIVE = ("plan", "current_task", "last_finished", "subagents")
PLAN = [
    {"text": "Read the codex verdict", "status": "done"},
    {"text": "Rerun the golden digests", "status": "done"},
    {"text": "Port the tile grid to classes", "status": "in_progress"},
    {"text": "Escape the plan text", "status": "pending"},
    {"text": "Render both screenshots", "status": "pending"},
    {"text": "Post the ready handoff", "status": "pending"},
]
AGENTS = [
    {"kind": "comment-sicko", "description": "Sweep the diff for narration", "status": "running",
     "started_at": NOW - 40, "finished_at": None},
]
LIVE_ROW = {
    "tokens_in": 184321, "tokens_cached": 151143, "tokens_out": 6890, "cost_usd": 0.4217,
    "files_touched": 3, "commands": 6, "plan": json.dumps(PLAN),
    "current_task": "Port the tile grid to classes", "last_finished": "Rerun the golden digests",
    "subagents": json.dumps(AGENTS),
}
FIXTURES = {
    "none": None,
    "counters": dict.fromkeys(LIVE),
    "empty-lists": {"plan": "[]", "subagents": "[]", "current_task": None, "last_finished": None},
    "live": LIVE_ROW,
    "tokens-unknown": {**dict.fromkeys(LIVE), "tokens_in": 0, "tokens_cached": 0, "tokens_out": 0, "tokens_unknown": 1},
    "no-cost": {**LIVE_ROW, "cost_usd": None, "subagents": None},
}
SAMPLE = ("Read the review", "Reproduce the failing test", "Fixing finding", "find every caller",
          "run the slow tests", "Explore", "184k", "6.9k", "$0.42", "82%", "Sample content", "Handed off")


def _snapshot(tmp_path: Path, row: dict | None) -> dict:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _history(connection)
    if row is not None:
        add_telemetry(db, "h-review2", **row)
    return collect_snapshot(db, log, now=NOW)


def _server(snapshot: dict) -> str:
    return _section(_no_script(render_pages(snapshot)["job.html"]), PANEL)


def _script(tmp_path: Path, snapshot: dict) -> str:
    out = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review")
    return _section(out["html"]["jobview"], PANEL)


def _text(markup: str) -> str:
    return re.sub(r"<[^>]+>", "", markup)


def _body(markup: str) -> str:
    return markup.split("</h2>", 1)[1]


def test_tiles_and_live_text_come_from_the_telemetry_row(tmp_path: Path) -> None:
    text = _text(_server(_snapshot(tmp_path, LIVE_ROW)))
    for needle in ("184k", "tokens in · 82% cached", "6.9k", "tokens out", "$0.42", "cost so far",
                   "3 files · 6 commands", "touched and run", "working on", "Port the tile grid to classes",
                   "last finished: Rerun the golden digests", "2 of 6 done", "1 spawned · 1 running",
                   "comment-sicko", "Sweep the diff for narration", "running"):
        assert needle in text, needle


@pytest.mark.parametrize(
    "field, value, before, after",
    [
        ("tokens_in", 2_500_000, "184k", "2.5M"),
        ("tokens_out", 999, "6.9k", "999"),
        ("cost_usd", 1.0061, "$0.42", "$1.01"),
        ("commands", 11, "6 commands", "11 commands"),
        ("current_task", "Render both screenshots", 'lr-task">Port the tile grid', 'lr-task">Render both screenshots'),
    ],
)
def test_each_tile_and_line_follows_its_own_field(tmp_path: Path, field, value, before, after) -> None:
    base = _server(_snapshot(tmp_path / "a", LIVE_ROW))
    changed = _server(_snapshot(tmp_path / "b", {**LIVE_ROW, field: value}))
    assert before in base and before not in changed and after in changed
    assert base.count('class="lr-tile"') == changed.count('class="lr-tile"') == 4


def test_no_telemetry_row_says_no_telemetry(tmp_path: Path) -> None:
    assert _body(_server(_snapshot(tmp_path, None))) == '<p class="empty">no telemetry</p></section>'


def test_counters_only_row_shows_tiles_and_no_task_text(tmp_path: Path) -> None:
    panel = _server(_snapshot(tmp_path, FIXTURES["counters"]))
    assert panel.count('class="lr-tile"') == 4
    assert "25k" in panel and "tokens in · 99% cached" in panel and "1.2k" in panel
    assert "no data</b><span>cost so far" in panel and "2 files · 7 commands" in panel
    for gone in ("working on", "last finished", "Its plan", "Sub-agents", "working alone so far",
                 "no task in progress", "<li", "lr-kind", "lr-head", "<canvas"):
        assert gone not in panel, gone


@pytest.mark.parametrize("name", FIXTURES)
def test_nothing_invented(tmp_path: Path, name: str) -> None:
    panel = _server(_snapshot(tmp_path, FIXTURES[name]))
    allowed = {"live": {"184k", "6.9k", "$0.42", "82%"}, "no-cost": {"184k", "6.9k", "82%"}}.get(name, set())
    for sample in SAMPLE:
        if sample not in allowed:
            assert sample not in panel, sample
    if name == "no-cost":
        assert "working alone so far" not in panel and "no sub-agent data" in panel
    if name == "empty-lists":
        assert "working alone so far" in panel and "no plan yet" in panel and "no task in progress" in panel
        assert "nothing yet" in panel
    if name == "tokens-unknown":
        tiles = re.findall(r"<b>([^<]*)</b>", panel)
        assert tiles[:3] == ["no data", "no data", "no data"]


@needs_node
@pytest.mark.parametrize("name", FIXTURES)
def test_script_renders_the_same_panel(tmp_path: Path, name: str) -> None:
    snapshot = _snapshot(tmp_path, FIXTURES[name])
    assert _script(tmp_path, snapshot) == _server(snapshot)


def test_hostile_live_text_is_escaped(tmp_path: Path) -> None:
    evil = "<img onerror=alert(1)>"
    panel = _server(_snapshot(tmp_path, {
        **LIVE_ROW, "current_task": evil,
        "plan": json.dumps([{"text": evil, "status": "in_progress"}]),
        "subagents": json.dumps([{"kind": evil, "description": evil, "status": "failed"}]),
    }))
    assert "<img" not in panel and panel.count("&lt;img onerror=alert(1)&gt;") == 4


@pytest.mark.parametrize("change", ["dead", "stale", "overdue"])
def test_broken_jobs_hold_the_head_and_agent_strips_still(tmp_path: Path, change: str) -> None:
    snapshot = _snapshot(tmp_path, LIVE_ROW)
    assert {k: v for v, k in re.findall(r'data-px="([a-z0-9]+)"[^>]*data-px-key="live-(head|agent)', _server(snapshot))} == {
        "head": "spin3", "agent": "scan"}
    row = next(r for r in snapshot["running"] if r["job"] == "h-review")
    if change == "overdue":
        row["overdue"] = True
    else:
        row["job_state"] = change
    panel = _server(snapshot)
    assert {k: v for v, k in re.findall(r'data-px="([a-z0-9]+)"[^>]*data-px-key="live-(head|agent)', panel)} == {
        "head": "off", "agent": "off"}
    assert re.search(r'data-px="fill"[^>]*data-progress="0.333"[^>]*data-px-key="live-plan-h-review"', panel)


def test_compact_rounds_half_up_like_the_script() -> None:
    assert [compact(n) for n in (0, 999, 1000, 1250, 6890, 9999, 10000, 184321, 999499, 1_250_000)] == [
        "0", "999", "1.0k", "1.3k", "6.9k", "10.0k", "10k", "184k", "999k", "1.3M"]


def test_live_run_screenshots_are_committed() -> None:
    shots = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
    for width in (1440, 375):
        assert (shots / f"issue-149-ticket-detail-{width}.png").stat().st_size > 0, width
