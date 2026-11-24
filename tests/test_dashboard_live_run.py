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

from lib.dashboard import collect_snapshot
from lib.dashboard_pages.ticket_detail import compact
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_lanes import Browser, _page_with_status_stub, needs_chrome
from tests.test_dashboard_layout import _history, _section, needs_node
from tests.test_dashboard_telemetry import add_telemetry
from tests.test_dashboard_ticket_detail import _quote, _ticket_script, _ticket_server

PANEL = "liverun-job-h-review"
ADDRESS = "maxharris1/cleffy#40"
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
    return _section(_ticket_server(snapshot, ADDRESS), PANEL)


def _script(tmp_path: Path, snapshot: dict) -> str:
    return _section(_ticket_script(tmp_path, snapshot, "#t=" + _quote(ADDRESS)), PANEL)


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


def _strips(panel: str) -> dict[str, tuple[str, bool]]:
    return {key: (effect, 'data-still="true"' in tag)
            for tag, effect, key in re.findall(r'(<canvas [^>]*data-px="([a-z0-9]+)"[^>]*data-px-key="live-([a-z]+)-[^"]*"[^>]*>)', panel)}


def _break(snapshot: dict, change: str) -> None:
    row = next(r for r in snapshot["running"] if r["job"] == "h-review")
    if change == "overdue":
        row["overdue"] = True
    else:
        row["job_state"] = change


@pytest.mark.parametrize("change", ["dead", "stale", "overdue"])
def test_broken_jobs_hold_every_live_strip_still(tmp_path: Path, change: str) -> None:
    snapshot = _snapshot(tmp_path, LIVE_ROW)
    assert _strips(_server(snapshot)) == {"head": ("spin3", False), "plan": ("fill", False), "agent": ("scan", False)}
    _break(snapshot, change)
    panel = _server(snapshot)
    assert _strips(panel) == {"head": ("off", True), "plan": ("fill", True), "agent": ("off", True)}
    assert re.search(r'data-px="fill"[^>]*data-progress="0.333"[^>]*data-px-key="live-plan-h-review"', panel)


@needs_node
@pytest.mark.parametrize("change", ["dead", "stale", "overdue"])
def test_script_holds_broken_jobs_still_like_the_server(tmp_path: Path, change: str) -> None:
    snapshot = _snapshot(tmp_path, LIVE_ROW)
    _break(snapshot, change)
    assert _script(tmp_path, snapshot) == _server(snapshot)


LONG = "Verify every dashboard renderer against the current snapshot schema and record the results for the next review round."
UNBROKEN = "x" * 120


@needs_chrome
def test_live_text_wraps_inside_the_panel_at_375px(tmp_path: Path) -> None:
    assert len(LONG) <= 120
    row = {**LIVE_ROW, "current_task": UNBROKEN, "last_finished": LONG,
           "plan": json.dumps([{"text": UNBROKEN, "status": "in_progress"}, {"text": LONG, "status": "pending"}]),
           "subagents": json.dumps([{**AGENTS[0], "kind": UNBROKEN, "description": UNBROKEN}])}
    browser = Browser()
    try:
        browser.open(_page_with_status_stub(_snapshot(tmp_path, row), "ticket.html"), 375)
        browser.eval("""new Promise(function (done) { location.hash = '#t=' + encodeURIComponent('maxharris1/cleffy#40'); (function wait() {
          if (document.querySelector('#ticketview .lr-last')) done(true); else setTimeout(wait, 50); })(); })""")
        assert browser.eval("document.documentElement.scrollWidth") == 375
        spill = browser.eval("""(function () {
          var panel = document.querySelector('#ticketview .lr'), box = panel.getBoundingClientRect();
          return [].filter.call(panel.querySelectorAll('*'), function (e) {
            var b = e.getBoundingClientRect(); return b.width && (b.left < box.left - 0.5 || b.right > box.right + 0.5);
          }).map(function (e) { return e.className; });
        })()""")
        assert not spill, spill
    finally:
        browser.close()


ARTBOARD_LINE_HEIGHT = {"lr-task": 1.35, "lr-kind": 1.6, "lr-tile b": 1.25}


@needs_chrome
def test_live_text_keeps_the_artboard_line_heights(tmp_path: Path) -> None:
    browser = Browser()
    try:
        browser.open(_page_with_status_stub(_snapshot(tmp_path, LIVE_ROW), "ticket.html"), 1440)
        browser.eval("""new Promise(function (done) { location.hash = '#t=' + encodeURIComponent('maxharris1/cleffy#40'); (function wait() {
          if (document.querySelector('#ticketview .lr-desc')) done(true); else setTimeout(wait, 50); })(); })""")
        rows = browser.eval("""[].filter.call(document.querySelectorAll('#ticketview .lr *'), function (e) {
            return [].some.call(e.childNodes, function (n) { return n.nodeType === 3 && n.data.trim(); });
          }).map(function (e) {
            var s = getComputedStyle(e), owner = e.closest('[class]');
            var name = owner.className.split(' ')[0] + (owner === e ? '' : ' ' + e.localName);
            return [name, s.lineHeight, parseFloat(s.fontSize)];
          })""")
    finally:
        browser.close()
    assert {"lr-text", "lr-desc", "lr-task", "lr-kind", "lr-tile b"} <= {cls for cls, _, _ in rows}
    wrong = [(cls, height, size) for cls, height, size in rows
             if height == "normal" or abs(float(height[:-2]) / size - ARTBOARD_LINE_HEIGHT.get(cls, 1.5)) > 0.01]
    assert not wrong, wrong


def test_compact_rounds_half_up_like_the_script() -> None:
    assert [compact(n) for n in (0, 999, 1000, 1250, 6890, 9999, 10000, 184321, 999499, 1_250_000)] == [
        "0", "999", "1.0k", "1.3k", "6.9k", "10.0k", "10k", "184k", "999k", "1.3M"]
