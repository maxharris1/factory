from __future__ import annotations

import re
from pathlib import Path

from lib import health as component_health
from lib.dashboard import (
    LINEAR_BUDGET_COMPLEXITY_PER_HOUR,
    LINEAR_BUDGET_REQUESTS_PER_HOUR,
    collect_snapshot,
    rules_and_limits,
)
from lib.dashboard_flow import load_chart
from lib.dashboard_page import CSS, render_pages
from lib.factory_md import load_factory_md
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_layout import BAR, _no_script, _run_page, _section, _text, needs_node
from tests.test_project_rules import FIXTURES, fixture_world

NEWEST = "aaaaaaaaaaaa"
OLDER = "bbbbbbbbbbbb"
PANELS = ["components", "drift", "health", "log", "rules", "no-waiter"]


def _component(component: str, device: str, *, bot: str = "", state: str = "up",
               version: str = NEWEST, age: int | None = 4, started: float | None = NOW - 600,
               anchor: str | None = None) -> dict:
    label = f"waiter {bot} @ {device}" if bot else component
    return {
        "component": component, "device": device, "bot": bot, "pid": 10, "state": state,
        "reason": "never seen" if age is None else None,
        "last_seen_et": None if age is None else "Nov 14 1:00:00 PM",
        "last_seen_age_seconds": age, "interval_seconds": 15.0, "configured": True,
        "started_at_et": None if started is None else f"started-{int(started)}",
        "started_at": started, "version": version, "stale_code": False, "label": label,
        "anchor": anchor or "health-" + re.sub(r"[^A-Za-z0-9_-]", "-", f"{component}-{bot}-{device}"),
    }


def _snapshot(tmp_path: Path) -> dict:
    snapshot = collect_snapshot(*seeded(tmp_path), now=NOW)
    rows = [
        _component("poller", "box"),
        _component("waiter", "box", bot="claude-bot"),
        _component("waiter", "pc", bot="claude-bot"),
        _component("waiter", "box", bot="codex-bot", version=OLDER, started=NOW - 300),
        _component("linear-listener", "box", state="down", version="unknown", age=None,
                   started=None),
        _component("device-probe", "pc"),
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



def test_drift_colours_newest_older_and_unknown(tmp_path: Path) -> None:
    html = _no_script(render_pages(_snapshot(tmp_path))["health.html"])
    drift = _section(html, "drift")
    assert '<span class="count warn">1 behind</span>' in drift
    names = re.findall(r'<span class="name (\w+)">([^<]+)</span>', drift)
    assert names == [("ok", NEWEST), ("warn", OLDER), ("bad", "unknown")]
    blocks = re.findall(r'<span class="sys-blk (\w+)"></span>', drift)
    assert blocks == ["ok"] * 4 + ["warn"] + ["bad"]
    text = _text(drift)
    assert "4 components" in text
    assert f"waiter codex-bot @ box · started started-{int(NOW - 300)}" in text
    assert "linear-listener" in text


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
    assert "process details" in html and "<details class=\"sys-details\" open>" in html


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
    assert log_kinds == ["bad", "start", "warn"]



def test_poller_log_lines_are_coloured_by_kind(tmp_path: Path) -> None:
    log = _section(_no_script(render_pages(_snapshot(tmp_path))["health.html"]), "log")
    assert '<div class="bad">2026-11-13 13:08:59 EST poller exit rc=1</div>' in log
    assert '<div class="start">2026-11-13 13:09:19 EST poller start</div>' in log
    assert '<div class="warn">orch poll: another orch poll owns /tmp/orch.db.lock</div>' in log
    assert "<div>2026-11-13 13:09:20 EST poller exit rc=0</div>" in log
    assert "<div>cycle line</div>" in log


def test_moved_panels_left_their_old_pages(tmp_path: Path) -> None:
    pages = render_pages(_snapshot(tmp_path))
    assert 'id="health"' not in pages["efficiency.html"]
    assert 'id="log"' not in pages["activity.html"]
    assert "renderHealth, renderLog" in pages["health.html"]
    assert re.search(r"efficiency: \[renderSavings, renderTokens\]", pages["health.html"])



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
