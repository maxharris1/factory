"""#140: the Lanes page (index.html): status tiles, the queue lane, slot tiles per state,
Just happened and Intake, server render against the page script."""

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot, render_pages
from lib.dashboard_page import TILE_STATE, intake_split, queue_speed
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_layout import BAR, HARNESS, NODE, _no_script, _run_page, _section, needs_node

STATES = {
    "plan": {"job_state": "running", "action": "plan"},
    "build": {"job_state": "running", "action": "build"},
    "review": {"job_state": "running", "action": "review"},
    "fix": {"job_state": "running", "action": "fix"},
    "claimed": {"job_state": "claimed", "action": "build"},
    "stale": {"job_state": "stale", "action": "build"},
    "dead": {"job_state": "dead", "action": "build"},
    "overdue": {"job_state": "running", "action": "build", "overdue": True},
}
EFFECT = {"plan": "field", "build": "fill", "review": "scan", "fix": "patch", "claimed": "spin3",
          "stale": "decay", "dead": "static", "overdue": "static"}


def _base(tmp_path: Path) -> dict:
    return collect_snapshot(*seeded(tmp_path), now=NOW)


def _row(template: dict, name: str, **extra) -> dict:
    row = copy.deepcopy(template)
    row.update({"job": f"t-{name}", "overdue": False, "device": "box", "round": "2/3",
                "elapsed_seconds": 300, "timeout_seconds": 1200, "heartbeat_age_seconds": 4,
                "recipient": "claude"})
    row.update(STATES[name])
    row.update(extra)
    return row


def _tiles(tmp_path: Path) -> dict:
    snapshot = _base(tmp_path)
    template = snapshot["running"][0]
    snapshot["running"] = [_row(template, name) for name in STATES]
    snapshot["slots"]["devices"] = [
        {"name": "box", "slot_max": 9, "held": 0, "enabled": True, "placeholder": False,
         "harnesses": ["claude", "codex"]},
    ]
    return snapshot


def _tile(html: str, name: str) -> str:
    match = re.search(rf'<a class="tile [^"]*" href="job.html#job-t-{name}"[^>]*>.*?</a>', html, re.S)
    assert match, name
    return match.group(0)


@pytest.mark.parametrize("name", list(STATES))
def test_each_slot_state_has_its_strip_chip_and_link(tmp_path: Path, name: str) -> None:
    html = _no_script(render_pages(_tiles(tmp_path))["index.html"])
    tile = _tile(html, name)
    assert f'data-px="{EFFECT[name]}"' in tile
    spec = TILE_STATE[name]
    if spec.get("chip"):
        assert f'<span class="chip {spec["chip"][1]}">{spec["chip"][0]}</span>' in tile
    if name == "overdue":
        assert '<span class="chip bad">OVERDUE</span>' in tile
    if name in ("plan", "build", "review", "fix"):
        assert "chip" not in tile.replace('class="tile', "")
        assert 'class="glyph"' in tile
        assert any(f'aria-label="{verb}…"' in tile for verb in spec["verbs"])
    else:
        assert 'class="glyph"' not in tile
    if name in ("dead", "overdue"):
        assert 'data-tint="#f7768e"' in tile and 'class="tile claude bad"' in tile
    assert "running" not in re.sub(r"<[^>]+>", " ", tile).lower()


def test_build_fill_and_stale_decay_follow_the_job(tmp_path: Path) -> None:
    snapshot = _tiles(tmp_path)
    snapshot["running"][5]["heartbeat_age_seconds"] = 600
    html = _no_script(render_pages(snapshot)["index.html"])
    assert 'data-progress="0.250"' in _tile(html, "build")
    assert 'data-progress="0.500"' in _tile(html, "stale")
    assert 'width:100%' in _tile(html, "dead") and "hb —" in _tile(html, "claimed")


def test_queue_speed_follows_the_bits_anchors() -> None:
    assert [queue_speed(a) for a in (0, 40, 370, 1325)] == [1.0, 1.0, 2.5, 5.0]
    assert queue_speed(100000) == 6.0


def test_queue_lane_is_still_when_empty_and_red_without_a_waiter(tmp_path: Path) -> None:
    snapshot = _base(tmp_path)
    queued = snapshot["queued"]
    empty = dict(snapshot, queued=[])
    lane = _section(_no_script(render_pages(empty)["index.html"]), "queue")
    assert 'data-px="conveyor"' in lane and 'data-still="true"' in lane and "queue empty" in lane
    rows = copy.deepcopy(queued)
    rows[0].update(age_seconds=370, no_waiter=None)
    rows[1].update(age_seconds=1325, no_waiter="codex", no_waiter_link="health.html#no-waiter")
    lane = _section(_no_script(render_pages(dict(snapshot, queued=rows[:2]))["index.html"]), "queue")
    head = lane.split("<ul")[0]
    assert 'data-speed="5"' in head and "data-still" not in head
    assert 'data-speed="2.5"' in lane
    assert re.search(r'data-px="static" data-tint="#f7768e"[^>]*data-px-key="queue-' + re.escape(rows[1]["job"]), lane)
    assert 'href="health.html#no-waiter">no waiter for codex</a>' in lane


def test_probe_down_gives_a_still_red_meter(tmp_path: Path) -> None:
    snapshot = _tiles(tmp_path)
    snapshot["health"] = {"components": [{"component": "device-probe", "device": "box", "state": "down"}]}
    lanes = _section(_no_script(render_pages(snapshot)["index.html"]), "lanes")
    meter = re.search(r'<canvas[^>]*data-px-key="meter-box"[^>]*>', lanes).group(0)
    assert 'data-tint="#f7768e"' in meter and 'data-still="true"' in meter
    snapshot["health"] = {"components": []}
    lanes = _section(_no_script(render_pages(snapshot)["index.html"]), "lanes")
    meter = re.search(r'<canvas[^>]*data-px-key="meter-box"[^>]*>', lanes).group(0)
    assert 'data-tint="#9ece6a"' in meter and "data-still" not in meter


def test_just_happened_shows_six_newest_and_marks_grok_wakes(tmp_path: Path) -> None:
    snapshot = _base(tmp_path)
    rows = [
        {"job": f"h{i}", "repo": "o/factory", "ticket_id": str(i), "source": None, "pr": None,
         "action": "review", "recipient": "codex", "state": "done", "kind": "handoff", "age_seconds": i * 10}
        for i in range(8)
    ]
    rows[0].update(kind="bot_to_bot", recipient="grok")
    snapshot["handoff_savings"]["recent"] = rows
    panel = _section(_no_script(render_pages(snapshot)["index.html"]), "just-happened")
    items = re.findall(r"<li>.*?</li>", panel)
    assert len(items) == 6
    assert "grok wake" in items[0] and "grok wake" not in items[1]
    assert "6 newest handoffs" in panel and 'href="activity.html"' in panel


def test_intake_lists_three_newest_fed_and_folds_the_rest(tmp_path: Path) -> None:
    items = [
        {"repo": "o/factory", "issue": str(n), "state": "done", "pr": str(100 + n), "reason": None,
         "fed_age_seconds": age}
        for n, age in ((1, 50), (2, 10), (3, 40), (4, 20), (5, 30))
    ] + [{"repo": "o/factory", "issue": "9", "state": "waiting-blocked", "pr": None,
          "reason": "blocked by #1", "fed_age_seconds": None}]
    top, rest = intake_split(items)
    assert [x["issue"] for x in top] == ["2", "4", "5"]
    assert [x["issue"] for x in rest] == ["1", "3", "9"]
    snapshot = _base(tmp_path)
    snapshot["feeder"] = {"eligible": 6, "fed": 5, "waiting_capacity": 0, "waiting_blocked": 1, "items": items}
    panel = _section(_no_script(render_pages(snapshot)["index.html"]), "intake")
    assert "5 fed · 1 waiting" in panel
    assert panel.index("factory#2") < panel.index("factory#4") < panel.index("<details>")
    assert "<summary>3 more</summary>" in panel and "blocked by #1" in panel


def test_index_has_no_modal_markup(tmp_path: Path) -> None:
    html = render_pages(_base(tmp_path))["index.html"]
    for needle in ("job-modal", "modal-box", "openModal", 'role="dialog"', 'id="flow-template"'):
        assert needle not in html, needle


# --- server render against the page script ---------------------------------------------------


def _shape(markup: str) -> list[str]:
    """Tags, classes and link targets in document order; text and inline style left out."""
    return [
        f"{tag}.{'.'.join(sorted(cls.split()))}|{href}"
        for tag, cls, href in re.findall(
            r'<(\w+)(?=[^>]*?(?:class="([^"]*)")?)(?=[^>]*?(?:href="([^"]*)")?)[^>]*>', markup
        )
    ]


@needs_node
@pytest.mark.parametrize("panel", ["kpis", "lanes", "just-happened", "intake"])
def test_server_render_equals_script_render(tmp_path: Path, panel: str) -> None:
    snapshot = _tiles(tmp_path)
    server = _section(_no_script(render_pages(snapshot)["index.html"]), panel)
    inner = server[server.index(">") + 1 : server.rindex("<")]
    out = _run_page(tmp_path, snapshot, "main", BAR + ["hud", panel])
    assert _shape(out["html"][panel]) == _shape(inner)


def _run_series(tmp_path: Path, snapshots: list[dict]) -> list[dict]:
    """The page script fed one snapshot per poll; the strips' state after each."""
    html = render_pages(snapshots[0])["index.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    harness = HARNESS.replace(
        "const status = JSON.parse(statusText);",
        "const series = JSON.parse(statusText); let status = series[0];",
    ).replace(
        "return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(status)}); };",
        "const s = series[Math.min(fetches - 1, series.length - 1)]; status = s;"
        " return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(s)}); };",
    ).replace("}, 200);\n", "}, 2500 * " + str(len(snapshots)) + ");\n").replace(
        "  createElement: (tag) => new Node(tag),",
        "  createElement: (tag) => new Node(tag),\n  querySelectorAll: (sel) => { const out = [];"
        " (function walk(n) { if (n.tagName === 'canvas') out.push(n); n.children.forEach(walk); })(body);"
        " return out; },",
    )
    harness = harness.replace(
        "eval(script);",
        "global.requestAnimationFrame = () => 0; global.performance = global.performance || {now: () => 0};"
        " eval(script.replace('return {bind: bind,', 'global.__strips = () => STRIPS; return {bind: bind,'));"
        " const seen = [];"
        " global.__snap = () => { const out = {}; global.__strips().forEach((s, k) => {"
        " out[k] = {beats: s.beats, burst: s.props.burst}; }); seen.push(out); };",
    )
    harness = harness.replace(
        "process.stdout.write(JSON.stringify({fetches, steps,",
        "process.stdout.write(JSON.stringify({fetches, steps, seen,",
    )
    (tmp_path / "page.js").write_text(
        script.replace("    PX.bind();\n", "    PX.bind();\n    if (global.__snap) global.__snap();\n"),
        encoding="utf-8",
    )
    (tmp_path / "status.json").write_text(json.dumps(snapshots), encoding="utf-8")
    (tmp_path / "harness.js").write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(tmp_path / "page.js"), str(tmp_path / "status.json"),
         "main", "", json.dumps(BAR + ["hud", "kpis", "lanes", "just-happened", "intake"]), "[]"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["seen"]


@needs_node
def test_beat_only_on_a_real_heartbeat_drop_and_burst_only_on_a_change(tmp_path: Path) -> None:
    first = _tiles(tmp_path)
    first["running"][1]["heartbeat_age_seconds"] = 40
    second = copy.deepcopy(first)
    second["running"][1]["heartbeat_age_seconds"] = 45
    third = copy.deepcopy(second)
    third["running"][1]["heartbeat_age_seconds"] = 3
    third["queue"]["totals"]["queued"] += 1
    second["generated_at_epoch"] = first["generated_at_epoch"] + 1
    third["generated_at_epoch"] = first["generated_at_epoch"] + 2
    seen = _run_series(tmp_path, [first, second, third])
    assert len(seen) >= 3, seen
    assert seen[0]["job-t-build"]["beats"] == 0 and seen[1]["job-t-build"]["beats"] == 0
    assert seen[-1]["job-t-build"]["beats"] == 1
    assert seen[-1]["job-t-plan"]["beats"] == 0
    assert all(str(s["burst"]) == "0" for k, s in seen[0].items() if k.startswith("kpi-"))
    assert str(seen[-1]["kpi-1"]["burst"]) == "1" and str(seen[-1]["kpi-0"]["burst"]) == "0"
