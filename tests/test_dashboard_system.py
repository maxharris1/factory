from __future__ import annotations

import functools
import html as htmllib
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from lib import health as component_health
from lib.dashboard import (
    LINEAR_BUDGET_COMPLEXITY_PER_HOUR,
    LINEAR_BUDGET_REQUESTS_PER_HOUR,
    collect_snapshot,
    rules_and_limits,
)
from lib.dashboard_flow import load_chart
from lib.dashboard_page import CSS, render_pages, version_drift
from lib.factory_md import load_factory_md
from tests.test_dashboard import NOW, seeded
from tests import test_dashboard_tickets_board as board
from tests.test_dashboard_layout import BAR, NODE, _no_script, _run_page, _section, _text, needs_node
from tests.test_project_rules import FIXTURES, fixture_world

NEWEST = "aaaaaaaaaaaa"
OLDER = "bbbbbbbbbbbb"
PANELS = ["components", "drift", "health", "log", "rules", "no-waiter"]


def _component(component: str, device: str, *, bot: str = "", state: str = "up",
               version: str = NEWEST, age: int | None = 4, started: float | None = NOW - 600,
               anchor: str | None = None, stale_code: bool = False) -> dict:
    label = f"waiter {bot} @ {device}" if bot else component
    return {
        "component": component, "device": device, "bot": bot, "pid": 10, "state": state,
        "reason": "never seen" if age is None else None,
        "last_seen_et": None if age is None else component_health.et(NOW - age),
        "last_seen_age_seconds": age, "interval_seconds": 15.0, "configured": True,
        "started_at_et": None if started is None else component_health.et(started),
        "started_at": started, "version": version, "stale_code": stale_code, "label": label,
        "anchor": anchor or "health-" + re.sub(r"[^A-Za-z0-9_-]", "-", f"{component}-{bot}-{device}"),
    }


def _snapshot(tmp_path: Path) -> dict:
    snapshot = collect_snapshot(*seeded(tmp_path), now=NOW)
    rows = [
        _component("poller", "box"),
        _component("waiter", "box", bot="claude-bot"),
        _component("waiter", "pc", bot="claude-bot"),
        _component("waiter", "box", bot="codex-bot", version=OLDER, started=NOW - 300, stale_code=True),
        _component("linear-listener", "box", state="down", version="unknown", age=None,
                   started=None),
        _component("device-probe", "pc", version=OLDER),
    ]
    no_waiter = [{"harness": "codex", "bot": "codex-bot", "jobs": ["j1"], "count": 1,
                  "anchor": "health-no-waiter-codex"}]
    snapshot["health"] = {
        "known": True, "error": None, "code_version": NEWEST, "components": rows,
        "no_waiter": no_waiter, "down": 1, "stale_code": 0, "stale": 0,
        "thresholds": {"beat_seconds": 15.0, "probe_seconds": 60.0, "stale_beats": 3,
                       "down_beats": 8},
        "publish_seconds": None,
    }
    down = rows[4]
    snapshot["attention"] = {"count": 3, "items": [
        {"kind": "down", "repo": "", "ticket_id": "", "component": down["label"],
         "link": f"health.html#{down['anchor']}", "detail": "down (never seen)"},
        {"kind": "health", "repo": "", "ticket_id": "", "component": "health storage",
         "link": "health.html#components", "detail": "unknown: x"},
        {"kind": "no-waiter", "repo": "", "ticket_id": "", "component": "waiter codex-bot",
         "link": "health.html#health-no-waiter-codex", "detail": "no waiter for codex (1 queued job)"},
    ]}
    snapshot["poller"]["log"]["tail"] = [
        "2026-11-13 13:08:59 EST poller exit rc=1",
        "2026-11-13 13:09:19 EST poller start",
        "orch poll: another orch poll owns /tmp/orch.db.lock",
        "2026-11-13 13:09:20 EST poller exit rc=0",
        "cycle line",
    ]
    return snapshot


def _ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def _strip_fragments(html: str) -> list[str]:
    return re.findall(r'href="health\.html#([A-Za-z0-9_-]+)"', html)



def test_every_health_attention_link_lands_on_its_row(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    html = render_pages(snapshot)["health.html"]
    fragments = set(_strip_fragments(html))
    assert {"health-linear-listener--box", "components", "health-no-waiter-codex"} <= fragments
    ids = _ids(html)
    for fragment in fragments:
        assert fragment in ids, fragment
    assert re.search(r'<tr id="health-linear-listener--box">', html)
    assert re.search(r'<li id="health-no-waiter-codex">', html)


@needs_node
def test_script_keeps_the_rows_the_attention_links_name(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    out = _run_page(tmp_path, snapshot, "health", BAR + ["hud", *PANELS])
    drawn = "".join(out["html"][panel] for panel in PANELS)
    ids = _ids(drawn) | set(PANELS)
    fragments = set(_strip_fragments(out["html"]["attention-strip"]))
    assert {"health-linear-listener--box", "components", "health-no-waiter-codex"} <= fragments
    for fragment in fragments:
        assert fragment in ids, fragment


def test_status_line_links_land_on_system_panels(tmp_path: Path) -> None:
    pages = render_pages(_snapshot(tmp_path))
    for href in re.findall(r'class="sl-vital[^"]*" href="([^"]+)"', pages["health.html"]):
        page, _, fragment = href.partition("#")
        assert fragment in _ids(pages[page]), href



def _version_href(statusline: str) -> list[str]:
    return re.findall(r'href="([^"]+)" data-seg="version"', statusline)


def test_version_segment_links_to_the_drift_panel(tmp_path: Path) -> None:
    html = render_pages(_snapshot(tmp_path))["health.html"]
    assert _version_href(_no_script(html)) == ["health.html#drift"]
    assert 'id="drift"' in _no_script(html)


@needs_node
def test_script_version_segment_links_to_the_drift_panel(tmp_path: Path) -> None:
    out = _run_page(tmp_path, _snapshot(tmp_path), "health", BAR + ["hud"])
    assert _version_href(out["html"]["statusline"]) == ["health.html#drift"]


def test_drift_colours_newest_older_and_unknown(tmp_path: Path) -> None:
    html = _no_script(render_pages(_snapshot(tmp_path))["health.html"])
    drift = _section(html, "drift")
    assert '<span class="count warn">1 behind</span>' in drift
    names = re.findall(r'<span class="name (\w+)">([^<]+)</span>', drift)
    assert names == [("ok", NEWEST), ("warn", OLDER), ("bad", "unknown")]
    blocks = re.findall(r'<span class="sys-blk (\w+)"></span>', drift)
    assert blocks == ["ok"] * 3 + ["warn"] * 2 + ["bad"]
    text = _text(drift)
    assert "3 components" in text
    assert "waiter codex-bot @ box, device-probe · started Jan 15 2:55:00 AM ET" in text
    assert "linear-listener" in text


def test_drift_and_status_line_count_the_same_stale_code_components(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    html = _no_script(render_pages(snapshot)["health.html"])
    assert version_drift(snapshot["health"])["behind"] == ["waiter codex-bot @ box"]
    drift = _section(html, "drift")
    assert '<span class="count warn">1 behind</span>' in drift
    assert "<span>aaaaaaa · 1 behind</span>" in html
    assert "waiter codex-bot @ box, device-probe · started Jan 15 2:55:00 AM ET" in _drift_meta(drift)


@needs_node
def test_script_drift_and_status_line_count_the_same_stale_code_components(tmp_path: Path) -> None:
    out = _run_page(tmp_path, _snapshot(tmp_path), "health", BAR + ["hud", "drift"])
    assert '<span class="count warn">1 behind</span>' in out["html"]["drift"]
    assert "<span>aaaaaaa · 1 behind</span>" in out["html"]["statusline"]
    assert "waiter codex-bot @ box, device-probe · started Jan 15 2:55:00 AM ET" in _drift_meta(out["html"]["drift"])


def test_down_component_is_a_static_red_block_linking_its_row(tmp_path: Path) -> None:
    html = _no_script(render_pages(_snapshot(tmp_path))["health.html"])
    components = _section(html, "components")
    cell = re.search(r'<a class="sys-cell bad" href="#health-linear-listener--box">(.*?)</a>', components)
    assert cell
    assert '<span class="sys-blk bad" aria-hidden="true"></span>' in cell.group(1)
    assert "DOWN never seen" in _text(cell.group(1))
    assert "canvas" not in components and "data-px" not in components
    for rule in re.findall(r"\.sys-blk[^{]*\{([^}]*)\}", CSS):
        assert "animation" not in rule and "transition" not in rule


def test_matrix_rows_by_kind_and_columns_by_device(tmp_path: Path) -> None:
    html = _no_script(render_pages(_snapshot(tmp_path))["health.html"])
    matrix = re.search(r'<table class="sys-table">(.*?)</table>', _section(html, "components"), re.S)
    assert matrix
    heads = re.findall(r"<th>([^<]+)</th>", matrix.group(1))
    assert heads == ["component", "box", "pc"]
    kinds = re.findall(r'<td class="sys-kind">([^<]+)</td>', matrix.group(1))
    assert kinds == ["poller", "waiter · claude-bot", "waiter · codex-bot", "linear-listener",
                     "device-probe"]
    assert "UP 4s" in _text(matrix.group(1))
    assert '<details class="sys-details"><summary>process details</summary>' in html


@needs_node
def test_script_draws_the_same_matrix_drift_and_log(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    out = _run_page(tmp_path, snapshot, "health", BAR + ["hud", *PANELS])
    server = _no_script(render_pages(snapshot)["health.html"])
    drift = out["html"]["drift"]
    assert re.findall(r'<span class="name (\w+)">([^<]+)</span>', drift) == [
        ("ok", NEWEST), ("warn", OLDER), ("bad", "unknown")]
    assert '<span class="count warn">1 behind</span>' in drift
    assert re.findall(r'class="sys-blk (\w+)"', drift) == re.findall(
        r'class="sys-blk (\w+)"', _section(server, "drift"))
    components = out["html"]["components"]
    assert '<a class="sys-cell bad" href="#health-linear-listener--box">' in components
    assert re.findall(r'<td class="sys-kind">([^<]+)</td>', components) == re.findall(
        r'<td class="sys-kind">([^<]+)</td>', _section(server, "components"))
    log_kinds = re.findall(r'<div class="(\w+)">', out["html"]["log"])
    assert log_kinds == re.findall(r'<div class="(\w+)">', _section(server, "log"))
    assert log_kinds == ["warn", "start", "bad"]



def test_poller_log_lines_are_coloured_by_kind(tmp_path: Path) -> None:
    log = _section(_no_script(render_pages(_snapshot(tmp_path))["health.html"]), "log")
    assert '<div class="bad">2026-11-13 13:08:59 EST poller exit rc=1</div>' in log
    assert '<div class="start">2026-11-13 13:09:19 EST poller start</div>' in log
    assert '<div class="warn">orch poll: another orch poll owns /tmp/orch.db.lock</div>' in log
    assert "<div>2026-11-13 13:09:20 EST poller exit rc=0</div>" in log
    assert "<div>cycle line</div>" in log


def _cells(markup: str, anchor: str) -> list[str]:
    row = re.search(rf'<tr id="{anchor}">(.*?)</tr>', markup, re.S)
    assert row, anchor
    return [_text(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)]


def _drift_meta(markup: str) -> list[str]:
    return re.findall(r'<span class="meta">([^<]+)</span>', markup)


def test_process_table_times_drop_the_et_their_header_already_names(tmp_path: Path) -> None:
    html = _no_script(render_pages(_snapshot(tmp_path))["health.html"])
    components = _section(html, "components")
    assert re.findall(r"<th>([^<]+ \(ET\))</th>", components) == ["last seen (ET)", "started (ET)"]
    cells = _cells(components, "health-poller--box")
    assert (cells[4], cells[8]) == ("Jan 15 2:59:56 AM", "Jan 15 2:50:00 AM")
    assert _cells(components, "health-linear-listener--box")[4::4] == ["never", "—"]
    assert "waiter codex-bot @ box, device-probe · started Jan 15 2:55:00 AM ET" in _drift_meta(_section(html, "drift"))


@needs_node
def test_script_process_table_times_drop_the_et_their_header_already_names(tmp_path: Path) -> None:
    out = _run_page(tmp_path, _snapshot(tmp_path), "health", BAR + ["hud", *PANELS])
    cells = _cells(out["html"]["components"], "health-poller--box")
    assert (cells[4], cells[8]) == ("Jan 15 2:59:56 AM", "Jan 15 2:50:00 AM")
    assert _cells(out["html"]["components"], "health-linear-listener--box")[4::4] == ["never", "—"]
    assert "waiter codex-bot @ box, device-probe · started Jan 15 2:55:00 AM ET" in _drift_meta(out["html"]["drift"])


DETAILS_HARNESS = board.HARNESS.replace(
    "    } else if (kind === 'record') {",
    "    } else if (kind === 'details') {\n"
    "      const node = pick({cls: 'sys-details'});\n"
    "      if (arg === 'toggle') { node.open = !node.open; fire(node, 'toggle'); }\n"
    "      steps.push({open: !!node.open, kept: node === global.lastDetails});\n"
    "      global.lastDetails = node;\n"
    "    } else if (kind === 'record') {",
)


def _details_steps(tmp_path: Path, actions: list) -> list[dict]:
    snapshot = _snapshot(tmp_path)
    (script,) = re.findall(r"<script>(.*?)</script>", render_pages(snapshot)["health.html"], flags=re.S)
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    (tmp_path / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (tmp_path / "harness.js").write_text(DETAILS_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(tmp_path / "page.js"), str(tmp_path / "status.json"),
         "health", "", json.dumps(BAR + ["hud", "components"]), json.dumps(actions)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return [step for step in json.loads(result.stdout)["steps"] if "open" in step]


def test_process_details_start_closed(tmp_path: Path) -> None:
    components = _section(_no_script(render_pages(_snapshot(tmp_path))["health.html"]), "components")
    assert re.findall(r"<details[^>]*>", components) == ['<details class="sys-details">']


@needs_node
def test_script_process_details_start_closed_and_stay_open_once_opened(tmp_path: Path) -> None:
    left = _details_steps(tmp_path / "left", [["details", "look"], ["hashchange", None], ["details", "look"]])
    assert left == [{"open": False, "kept": False}, {"open": False, "kept": False}]
    opened = _details_steps(tmp_path / "opened", [["details", "look"], ["details", "toggle"],
                                                  ["hashchange", None], ["details", "look"]])
    assert opened == [{"open": False, "kept": False}, {"open": True, "kept": True},
                      {"open": True, "kept": False}]


def _with_no_waiters(snapshot: dict, count: int) -> dict:
    snapshot["health"]["no_waiter"] = [
        {"harness": f"h{n}", "bot": f"bot{n}", "jobs": [f"j{n}"], "count": 1,
         "anchor": f"health-no-waiter-h{n}"}
        for n in range(count)
    ]
    return snapshot


@pytest.mark.parametrize(("count", "shown"), [(0, "0 harnesses"), (1, "1 harness"), (2, "2 harnesses")])
def test_no_waiter_count_names_harness_in_the_singular_for_one(
        tmp_path: Path, count: int, shown: str) -> None:
    snapshot = _with_no_waiters(_snapshot(tmp_path), count)
    panel = _section(_no_script(render_pages(snapshot)["health.html"]), "no-waiter")
    assert f'<span class="count">{shown}</span>' in panel


@needs_node
@pytest.mark.parametrize(("count", "shown"), [(0, "0 harnesses"), (1, "1 harness"), (2, "2 harnesses")])
def test_script_no_waiter_count_names_harness_in_the_singular_for_one(
        tmp_path: Path, count: int, shown: str) -> None:
    snapshot = _with_no_waiters(_snapshot(tmp_path), count)
    out = _run_page(tmp_path, snapshot, "health", BAR + ["hud", "no-waiter"])
    assert f'<span class="count">{shown}</span>' in out["html"]["no-waiter"]


CLASSIFIED_TAIL = [
    ("2026-11-13 13:08:59 EST poller exit rc=1", "bad"),
    ("2026-11-13 13:09:19 EST poller start", "start"),
    ("orch poll: another orch poll owns /tmp/orch.db.lock", "warn"),
    ("Traceback (most recent call last):", "bad"),
    ('  File "lib/orch.py", line 9, in poll', "bad"),
    ("    raise ValueError(x)", "bad"),
    ("ValueError: x", "bad"),
    ("cycle line after the traceback", ""),
    ("    indented after a plain line", ""),
    ("2026-11-13 13:10:00 EST ERROR linear: HTTP 500", "bad"),
    ("2026-11-13 13:10:01 EST CRITICAL disk full", "bad"),
    ("2026-11-13 13:10:02 EST WARNING slow cycle", "warn"),
    ("2026-11-13 13:10:03 EST WARN retrying", "warn"),
    ("lib.linear.RateLimitException: slow down", "bad"),
    ("2026-11-13 13:10:04 EST poller exit rc=0", ""),
    ("RuntimeError: poller start failed", "bad"),
    ("ERROR another orch poll owns lock", "bad"),
    ("2026-11-13 13:10:05 EST CRITICAL poller start aborted", "bad"),
    ("WARNING another orch poll owns lock", "warn"),
    ("WARNING poller start delayed", "warn"),
    ("WARN poller start retrying", "warn"),
]


def _classified(tmp_path: Path) -> dict:
    snapshot = _snapshot(tmp_path)
    snapshot["poller"]["log"]["tail"] = [line for line, _ in CLASSIFIED_TAIL]
    return snapshot


def test_poller_log_colours_errors_tracebacks_and_warnings(tmp_path: Path) -> None:
    log = _section(_no_script(render_pages(_classified(tmp_path))["health.html"]), "log")
    assert _log_rows(log) == [(kind, line) for line, kind in reversed(CLASSIFIED_TAIL)]


@needs_node
def test_script_poller_log_colours_errors_tracebacks_and_warnings(tmp_path: Path) -> None:
    log = _run_page(tmp_path, _classified(tmp_path), "health", BAR + ["hud", "log"])["html"]["log"]
    assert _log_rows(log) == [(kind, line) for line, kind in reversed(CLASSIFIED_TAIL)]


def _log_rows(markup: str) -> list[tuple[str, str]]:
    box = markup[markup.index('<div class="sys-log">') + len('<div class="sys-log">'):]
    return [(kind, htmllib.unescape(line)) for kind, line in re.findall(r'<div(?: class="(\w+)")?>([^<]*)</div>', box)]


NEWEST_FIRST = [
    "cycle line",
    "2026-11-13 13:09:20 EST poller exit rc=0",
    "orch poll: another orch poll owns /tmp/orch.db.lock",
    "2026-11-13 13:09:19 EST poller start",
    "2026-11-13 13:08:59 EST poller exit rc=1",
]


def test_poller_log_shows_the_newest_line_first(tmp_path: Path) -> None:
    log = _section(_no_script(render_pages(_snapshot(tmp_path))["health.html"]), "log")
    assert [line for _, line in _log_rows(log)] == NEWEST_FIRST
    assert '<p class="sys-note">last 5 lines, newest first, redacted</p>' in log


@needs_node
def test_script_poller_log_shows_the_newest_line_first(tmp_path: Path) -> None:
    log = _run_page(tmp_path, _snapshot(tmp_path), "health", BAR + ["hud", "log"])["html"]["log"]
    assert [line for _, line in _log_rows(log)] == NEWEST_FIRST
    assert '<p class="sys-note">last 5 lines, newest first, redacted</p>' in log


def test_moved_panels_left_their_old_pages(tmp_path: Path) -> None:
    pages = render_pages(_snapshot(tmp_path))
    assert 'id="health"' not in pages["efficiency.html"]
    assert 'id="log"' not in pages["activity.html"]
    assert "renderHealth, renderLog" in pages["health.html"]
    assert re.search(r"efficiency: \[renderSavings, renderJobsLifetime, renderRounds, renderWorking, renderHeadroom, renderTokens\]",
                     pages["health.html"])



def test_rules_read_their_values_from_health_budget_and_chart() -> None:
    chart = load_chart()
    assert chart is not None
    rules = {row["key"]: row["text"] for row in rules_and_limits(chart)}
    assert list(rules) == ["heartbeat", "liveness", "stuck", "plan-check", "rounds", "ship",
                           "approver", "linear"]
    assert rules["heartbeat"] == (f"every {component_health.BEAT_SECONDS:g} s (device probes "
                                  f"every {component_health.PROBE_SECONDS:g} s)")
    assert str(component_health.STALE_BEATS) in rules["liveness"]
    assert str(component_health.DOWN_BEATS) in rules["liveness"]
    labels = {(e["from"], e["to"]): e["label"] for e in chart["edges"]}
    assert rules["stuck"] == labels[("building", "stuck")]
    assert labels[("plan-check", "planning")] in rules["plan-check"]
    assert labels[("review", "capped")] in rules["rounds"]
    assert rules["ship"] == "merge, handoff, or after an approver"
    assert rules["approver"].startswith(labels[("waiting-external", "stuck")])
    assert f"{LINEAR_BUDGET_REQUESTS_PER_HOUR:,} req/h" in rules["linear"]
    assert f"{LINEAR_BUDGET_COMPLEXITY_PER_HOUR:,} complexity/h" in rules["linear"]


def test_rules_without_a_chart_say_so() -> None:
    rules = {row["key"]: row["text"] for row in rules_and_limits(None)}
    for key in ("stuck", "plan-check", "rounds", "ship", "approver"):
        assert rules[key] == "chart unavailable"
    assert rules["heartbeat"].startswith("every ")


def _fixture_projects(tmp_path: Path) -> tuple[dict, list[tuple[str, str]]]:
    world = fixture_world(tmp_path)
    world.poll()
    snapshot = collect_snapshot(world.path, world.log, now=112.0, orch=world.orch)
    expected = []
    for repo, (_ref, path) in FIXTURES.items():
        config, _ = load_factory_md(path)
        assert config is not None
        ship, _, approver = config.ship.partition(":")
        line = (f"ship mode {ship}" + (f" (approver {approver})" if approver else "")
                + f" · round cap {config.rounds_low} low, {config.rounds_high} high")
        expected.append((repo, line))
    return snapshot, expected


def _project_lines(rules: str) -> list[tuple[str, str]]:
    found = re.findall(r'<span class="ticket">([^<]+)</span><span class="what">[^<]+</span>'
                       r'<span class="sys-proj">([^<]+)</span>', rules)
    return [(repo, _text(line)) for repo, line in found]


def test_ship_mode_and_round_cap_match_factory_md(tmp_path: Path) -> None:
    snapshot, expected = _fixture_projects(tmp_path)
    rules = _section(_no_script(render_pages(snapshot)["health.html"]), "rules")
    assert _project_lines(rules) == expected
    assert "not shown" not in rules
    assert "after (approver tracy-site)" in _text(rules)


@needs_node
def test_script_ship_mode_and_round_cap_match_factory_md(tmp_path: Path) -> None:
    snapshot, expected = _fixture_projects(tmp_path)
    (tmp_path / "page").mkdir()
    rules = _run_page(tmp_path / "page", snapshot, "health", BAR + ["hud", "rules"])["html"]["rules"]
    assert _project_lines(rules) == expected


def test_an_unknown_project_shows_its_reason(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["projects"] = [{"repo": "o/r", "status": "unknown", "tracker": "unknown",
                             "ship": "unknown", "approver": None, "rounds": None,
                             "reason": "factory.md missing", "read_age_seconds": 3}]
    rules = _text(_section(_no_script(render_pages(snapshot)["health.html"]), "rules"))
    assert "ship mode and round cap: unknown (factory.md missing)" in rules


CHROME = next(filter(None, map(shutil.which, ("google-chrome", "chromium", "chromium-browser"))), None)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class TargetNavigated(Exception):
    pass


class _Chrome:
    """Headless Chrome driven over --remote-debugging-pipe: NUL-framed CDP JSON on fds 3 and 4."""

    def __init__(self, profile: Path) -> None:
        to_chrome_r, self._to_chrome = os.pipe()
        self._from_chrome, from_chrome_w = os.pipe()
        self._proc = subprocess.Popen(
            ["bash", "-c", f'exec "$0" "$@" 3<&{to_chrome_r} 4>&{from_chrome_w}', CHROME,
             "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
             "--remote-debugging-pipe", f"--user-data-dir={profile}", "--window-size=1440,900",
             "about:blank"],
            pass_fds=(to_chrome_r, from_chrome_w), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._buf = b""
        self._next = 0
        target = self.send("Target.createTarget", url="about:blank")["targetId"]
        self.session = self.send("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]

    def send(self, method: str, session: str | None = None, **params: object) -> dict:
        self._next += 1
        message: dict = {"id": self._next, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        os.write(self._to_chrome, json.dumps(message).encode() + b"\0")
        while True:
            while b"\0" not in self._buf:
                chunk = os.read(self._from_chrome, 1 << 16)
                assert chunk, "chrome closed the debugging pipe"
                self._buf += chunk
            raw, self._buf = self._buf.split(b"\0", 1)
            reply = json.loads(raw)
            if reply.get("id") == self._next:
                if reply.get("error", {}).get("message") == "Inspected target navigated or closed":
                    raise TargetNavigated(reply)
                assert "error" not in reply, reply
                return reply["result"]

    def run(self, expression: str) -> object:
        result = self.send("Runtime.evaluate", self.session, expression=expression,
                           awaitPromise=True, returnByValue=True)
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def wait_for(self, expression: str, what: str) -> None:
        deadline = time.monotonic() + 30
        while True:
            try:
                if self.run(expression):
                    return
            except TargetNavigated:
                pass
            assert time.monotonic() < deadline, f"never showed {what}"
            time.sleep(0.05)

    def close(self) -> None:
        self._proc.kill()
        self._proc.wait()
        os.close(self._to_chrome)
        os.close(self._from_chrome)


LAND_ON_EVERY_LINK = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 100 && /ACQUIRING|STATIC/.test(document.getElementById('link-text').textContent); i++) await sleep(50);
  const hrefs = [...new Set([...document.querySelectorAll('a[href^="health.html#"], a[href^="#"]')]
    .map(a => a.getAttribute('href')))].filter(h => h.split('#')[1]);
  const out = [];
  for (const href of hrefs) {
    history.replaceState(null, '', location.pathname);
    window.scrollTo(0, 0);
    document.querySelectorAll('details').forEach(d => { d.open = false; });
    await sleep(50);
    document.querySelector('a[href="' + href + '"]').click();
    await sleep(200);
    const target = document.getElementById(href.split('#')[1]);
    const box = target.getBoundingClientRect();
    out.push({href, hash: location.hash, focus: document.activeElement.id, visible: target.checkVisibility(),
              inView: box.bottom > 0 && box.top < innerHeight});
  }
  return out;
})()
"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_every_link_into_the_page_reveals_its_target_in_a_browser(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    snapshot = _snapshot(tmp_path)
    for name, html in render_pages(snapshot).items():
        (site / name).write_text(html, encoding="utf-8")
    (site / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    handler = functools.partial(QuietHandler, directory=str(site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chrome = _Chrome(tmp_path / "profile")
    try:
        chrome.send("Page.navigate", chrome.session,
                    url=f"http://127.0.0.1:{server.server_address[1]}/health.html")
        chrome.wait_for("location.pathname === '/health.html' && document.readyState === 'complete'",
                        "health.html")
        landed = chrome.run(LAND_ON_EVERY_LINK)
    finally:
        chrome.close()
        server.shutdown()
    assert isinstance(landed, list)
    hrefs = {row["href"] for row in landed}
    assert {"health.html#health-linear-listener--box", "#health-linear-listener--box",
            "health.html#health-no-waiter-codex", "health.html#components"} <= hrefs, hrefs
    for row in landed:
        if row["href"] == "#hud":
            assert (row["hash"], row["focus"]) == ("", "hud"), row
            continue
        assert row["hash"] == "#" + row["href"].split("#")[1], row
        assert row["visible"] and row["inView"], row
