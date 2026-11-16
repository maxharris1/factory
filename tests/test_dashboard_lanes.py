"""#140: the Lanes page (index.html): status tiles, the queue lane, slot tiles per state,
Just happened and Intake, server render against the page script."""

import base64
import copy
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot, render_pages
from lib.dashboard_page import CSS, TILE_STATE, intake_split, queue_speed
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_layout import BAR, HARNESS, NODE, _no_script, _run_page, _section, needs_node

STATES = {
    "plan": {"job_state": "running", "action": "plan"},
    "build": {"job_state": "running", "action": "build"},
    "review": {"job_state": "running", "action": "review"},
    "fix": {"job_state": "running", "action": "fix"},
    "clarify": {"job_state": "running", "action": "clarify"},
    "update-from-main": {"job_state": "running", "action": "update-from-main"},
    "claimed": {"job_state": "claimed", "action": "build"},
    "stale": {"job_state": "stale", "action": "build"},
    "dead": {"job_state": "dead", "action": "build"},
    "overdue": {"job_state": "running", "action": "build", "overdue": True},
    "stuck": {"job_state": "running", "action": "review", "stuck": True},
}
EFFECT = {"plan": "field", "build": "fill", "review": "scan", "fix": "patch", "clarify": "field",
          "update-from-main": "patch", "claimed": "spin3",
          "stale": "decay", "dead": "static", "overdue": "static", "stuck": "static"}
BROKEN = ("dead", "overdue", "stuck")


def _base(tmp_path: Path) -> dict:
    return collect_snapshot(*seeded(tmp_path), now=NOW)


def _row(template: dict, name: str, **extra) -> dict:
    row = copy.deepcopy(template)
    row.update({"job": f"t-{name}", "overdue": False, "stuck": False, "device": "box", "round": "2/3",
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


def _job_tile(html: str, job: str) -> str:
    match = re.search(rf'<a class="tile [^"]*" href="job.html#job-{re.escape(job)}"[^>]*>.*?</a>', html, re.S)
    assert match, job
    return match.group(0)


def _tile(html: str, name: str) -> str:
    return _job_tile(html, f"t-{name}")


@pytest.mark.parametrize("name", list(STATES))
def test_each_slot_state_has_its_strip_chip_and_link(tmp_path: Path, name: str) -> None:
    html = _no_script(render_pages(_tiles(tmp_path))["index.html"])
    tile = _tile(html, name)
    assert f'data-px="{EFFECT[name]}"' in tile
    spec = TILE_STATE[name]
    if spec.get("chip"):
        assert f'<span class="chip {spec["chip"][1]}">{spec["chip"][0]}</span>' in tile
    if name == "overdue":
        assert "OVERDUE" not in tile and 'aria-label="overdue"' in tile
    if spec.get("verbs"):
        assert "chip" not in tile.replace('class="tile', "")
        assert 'class="glyph"' in tile
        assert any(f'aria-label="{verb}…"' in tile for verb in spec["verbs"])
    else:
        assert 'class="glyph"' not in tile
    if name in BROKEN:
        assert 'data-tint="#f7768e"' in tile and 'class="tile claude bad"' in tile
    assert "running" not in re.sub(r"<[^>]+>", " ", tile).lower()


def _line(tile: str, n: int) -> str:
    return re.search(rf'<div class="tile-l{n}">(.*?)</div>(?=<div class="tile-l{n + 1}">|</div></a>)', tile, re.S).group(1)


def _children(line: str) -> list[str]:
    """Each top-level element of a tile line as `tag.class=text`."""
    out, depth = [], 0
    for tag, close, attrs, text in re.findall(r"<(/?)(\w+)([^>]*)>([^<]*)", line) and [
        (m.group(2), m.group(1), m.group(3), m.group(4)) for m in re.finditer(r"<(/?)(\w+)([^>]*)>([^<]*)", line)
    ]:
        if close:
            depth -= 1
            continue
        if depth == 0:
            cls = re.search(r'class="([^"]*)"', attrs)
            out.append(f"{tag}.{cls.group(1) if cls else ''}={text}")
        depth += 1
    return out


GITHUB = {"tracker": "github", "repo": "maxharris1/factory", "ticket_id": "471", "source": "#470", "pr": "471"}
LINEAR = {"tracker": "linear", "repo": "maxharris1/factory", "ticket_id": "ENG-12", "source": None, "pr": "88"}


def _label_tiles(tmp_path: Path) -> dict:
    snapshot = _tiles(tmp_path)
    template = snapshot["running"][0]
    snapshot["running"] = [
        _row(template, "claimed", job="t-gh", **GITHUB),
        _row(template, "build", job="t-lin", **LINEAR),
        _row(template, "overdue", job="t-late", **GITHUB),
        _row(template, "build", job="t-odd", action="frobnicate"),
    ]
    return snapshot


def _assert_label_tiles(html: str) -> None:
    gh, lin, late, odd = (_job_tile(html, job) for job in ("t-gh", "t-lin", "t-late", "t-odd"))
    assert _children(_line(gh, 1)) == [
        "span.bot claude=claude", "span.ticket=factory#470", "span.chip warn=claimed"
    ]
    assert _children(_line(lin, 1)) == ["span.bot claude=claude", "span.ticket=ENG-12"]
    assert _children(_line(late, 1)) == ["span.bot claude=claude", "span.ticket=factory#470"]
    for tile in (gh, lin, late, odd):
        assert 'class="kind"' not in tile and "PR #" not in _line(tile, 1) and "OVERDUE" not in tile
    assert 'title="maxharris1/factory#470 (PR #471) · build' in gh
    assert 'title="ENG-12' not in lin and "(PR #88)" in re.search(r'title="([^"]*)"', lin).group(1)
    assert 'data-px="static" data-tint="#f7768e"' in late and "tile claude bad" in late
    assert 'aria-label="overdue"' in late
    text = re.sub(r"<[^>]+>", "", odd)
    assert 'data-px="off"' in odd and 'class="glyph"' not in odd
    assert text.count("frobnicate") == 1 and 'class="action"' not in _line(odd, 3)
    assert '<span class="action">build</span>' in _line(gh, 3)


def test_tile_line_one_is_bot_short_ticket_and_state_chip(tmp_path: Path) -> None:
    _assert_label_tiles(_no_script(render_pages(_label_tiles(tmp_path))["index.html"]))


@needs_node
def test_the_page_script_draws_the_same_line_one(tmp_path: Path) -> None:
    snapshot = _label_tiles(tmp_path)
    _assert_label_tiles(_run_page(tmp_path, snapshot, "main", BAR + ["hud", "lanes"])["html"]["lanes"])


@pytest.mark.parametrize("name", ["clarify", "update-from-main"])
def test_clarify_and_update_from_main_draw_like_plan_and_fix(tmp_path: Path, name: str) -> None:
    verbs = {"clarify": ["Clarifying", "Untangling", "Answering"],
             "update-from-main": ["Merging", "Syncing", "Reconciling"]}[name]
    assert TILE_STATE[name]["verbs"] == verbs
    tile = _tile(_no_script(render_pages(_tiles(tmp_path))["index.html"]), name)
    assert f'data-px="{EFFECT[name]}" data-tint="claude"' in tile and 'class="glyph"' in tile
    assert any(f'aria-label="{verb}…"' in tile for verb in verbs)


@needs_node
@pytest.mark.parametrize("name", ["clarify", "update-from-main"])
def test_the_page_script_draws_clarify_and_update_from_main(tmp_path: Path, name: str) -> None:
    lanes = _run_page(tmp_path, _tiles(tmp_path), "main", BAR + ["hud", "lanes"])["html"]["lanes"]
    tile = _tile(lanes, name)
    assert f'data-px="{EFFECT[name]}"' in tile and 'data-tint="claude"' in tile and 'class="glyph"' in tile
    assert any(f'aria-label="{verb}…"' in tile for verb in TILE_STATE[name]["verbs"])


def _rule(selector: str) -> str:
    match = re.search(r"(?:^|[}\n])" + re.escape(selector) + r"\{([^}]*)\}", CSS)
    assert match, selector
    return match.group(1)


def test_tile_lines_wrap_instead_of_cutting() -> None:
    assert "flex-wrap:wrap" in _rule(".tile-l1")
    ticket = _rule(".tile-l1 .ticket")
    assert "flex:0 1 auto" in ticket and "max-width:100%" in ticket
    assert "flex:none" in _rule(".tile-l1 .chip")
    assert "flex-wrap:wrap" in _rule(".tile-l3")
    action = _rule(".tile-l3 .action")
    assert "ellipsis" not in action and "nowrap" not in action and "overflow-wrap:anywhere" in action
    assert ".kind{" not in CSS.split(".wake{")[0].split(".tile-l1{")[1]


def _stuck_review(tmp_path: Path) -> tuple[dict, dict]:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE ticket_state SET stuck = 1 WHERE ticket_id = '13'")
    snapshot = collect_snapshot(db, log, now=NOW)
    row = next(r for r in snapshot["running"] if r["ticket_id"] == "13")
    assert row["job_state"] == "running" and row["action"] == "review"
    return snapshot, row


def _assert_static_red(tile: str) -> None:
    assert 'data-px="static" data-tint="#f7768e"' in tile
    assert 'class="glyph"' not in tile and "Scrutinizing" not in tile
    assert '<span class="chip bad">stuck</span>' in tile and 'class="tile codex bad"' in tile


def test_a_stuck_tickets_running_job_is_static_red(tmp_path: Path) -> None:
    snapshot, row = _stuck_review(tmp_path)
    assert row["stuck"] is True
    assert {r["ticket_id"]: r["stuck"] for r in snapshot["running"]}["14"] is False
    html = _no_script(render_pages(snapshot)["index.html"])
    _assert_static_red(_job_tile(html, row["job"]))


@needs_node
def test_the_page_script_draws_a_stuck_tickets_job_static_red(tmp_path: Path) -> None:
    snapshot, row = _stuck_review(tmp_path)
    lanes = _run_page(tmp_path, snapshot, "main", BAR + ["hud", "lanes"])["html"]["lanes"]
    _assert_static_red(_job_tile(lanes, row["job"]))


def test_build_fill_and_stale_decay_follow_the_job(tmp_path: Path) -> None:
    snapshot = _tiles(tmp_path)
    next(r for r in snapshot["running"] if r["job"] == "t-stale")["heartbeat_age_seconds"] = 600
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


def _probe_lanes(tmp_path: Path) -> dict:
    snapshot = _tiles(tmp_path)
    snapshot["running"] = [dict(snapshot["running"][0], device="ghost")]
    snapshot["slots"]["devices"] = [
        {"name": name, "slot_max": 1, "held": 0, "enabled": name != "off", "placeholder": name == "later",
         "harnesses": []}
        for name in ("box", "pc", "off", "later")
    ]
    snapshot["health"] = {"components": [
        {"component": "device-probe", "device": name, "state": "down"} for name in ("box", "off", "later", "ghost")
    ]}
    return snapshot


def _assert_probe_chips(lanes: str) -> None:
    chips = dict(re.findall(r'data-device="([^"]+)"><div class="lane-head"><div class="name">[^<]*</div>'
                            r'<span class="chip ([^"]+)">', lanes))
    words = dict(re.findall(r'data-device="([^"]+)"><div class="lane-head"><div class="name">[^<]*</div>'
                            r'<span class="chip [^"]+">([^<]*)</span>', lanes))
    assert words == {"box": "PROBE DOWN", "pc": "ONLINE", "off": "DISABLED", "later": "PLACEHOLDER",
                     "ghost": "UNLISTED"}
    assert chips["box"] == "bad" and chips["pc"] == "ok"


def test_probe_down_replaces_online_but_not_disabled_placeholder_or_unlisted(tmp_path: Path) -> None:
    _assert_probe_chips(_section(_no_script(render_pages(_probe_lanes(tmp_path))["index.html"]), "lanes"))


@needs_node
def test_the_page_script_shows_probe_down_the_same_way(tmp_path: Path) -> None:
    _assert_probe_chips(_run_page(tmp_path, _probe_lanes(tmp_path), "main", BAR + ["hud", "lanes"])["html"]["lanes"])


QUIET = "quiet: no Linear webhook for 2h 10m (nothing missed by the last reconcile)"


def _quiet_webhook(tmp_path: Path) -> dict:
    snapshot = _base(tmp_path)
    snapshot["poller"]["linear_webhook"] = {
        "last_received_age_seconds": None, "last_event": None, "listener_age_seconds": 60, "dirty": 0,
        "warning": None, "note": QUIET,
    }
    return snapshot


def _assert_quiet_once(intake: str) -> None:
    hook = re.search(r'<div class="irow" id="linear-webhook">.*?</div>', intake, re.S).group(0)
    assert hook.count(QUIET) == 1 and "note:" not in hook and f'<p class="muted">{QUIET}</p>' in hook


def test_the_webhook_quiet_line_has_no_note_prefix(tmp_path: Path) -> None:
    _assert_quiet_once(_section(_no_script(render_pages(_quiet_webhook(tmp_path))["index.html"]), "intake"))


@needs_node
def test_the_page_script_prints_the_quiet_line_once(tmp_path: Path) -> None:
    _assert_quiet_once(_run_page(tmp_path, _quiet_webhook(tmp_path), "main", BAR + ["hud", "intake"])["html"]["intake"])


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


ROOT = Path(__file__).resolve().parents[1]
CHROME = next(
    (
        path
        for path in (
            *(shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser", "chrome")),
            *sorted(glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        if path and Path(path).is_file()
    ),
    None,
)
needs_chrome = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium found")
FOLD = 900
SHOTS = ("issue-140-lanes-1440.png", "issue-140-lanes-375.png")


def fifteen_slot_snapshot(tmp_path: Path) -> dict:
    snapshot = _base(tmp_path)
    template = snapshot["running"][0]
    snapshot["slots"]["devices"] = [
        {"name": name, "slot_max": cap, "held": held, "enabled": True, "placeholder": False,
         "harnesses": ["claude", "codex", "cursor"]}
        for name, cap, held in (("box", 5, 5), ("pc", 6, 6), ("mac", 4, 3))
    ]
    snapshot["running"] = [
        _row(template, name, device=device, job=f"{device}-{i}", recipient="codex" if name == "review" else "claude")
        for device, names in (
            ("box", ["build", "fix", "review", "stale", "dead"]),
            ("pc", ["plan", "build", "review", "fix", "claimed", "overdue"]),
            ("mac", ["update-from-main", "review"]),
        )
        for i, name in enumerate(names)
    ]
    snapshot["queue"]["totals"]["running"] = len(snapshot["running"])
    snapshot["queued"] = snapshot["queued"][:1]
    snapshot["queued"][0].update(no_waiter=None, no_waiter_link=None)
    snapshot["stuck"] = {"tickets": [], "overdue_jobs": []}
    snapshot["health"] = {"components": [], "counts": {}}
    return snapshot


def _page_with_status_stub(snapshot: dict, page: str) -> str:
    shim = (
        "<script>window.fetch = function () { return Promise.resolve({ok: true, status: 200,"
        f" json: function () {{ return Promise.resolve({json.dumps(snapshot)}); }}}}); }};</script>"
    )
    return render_pages(snapshot)[page].replace("<script>", shim + "<script>", 1)


class Browser:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        to_chrome, self._send = os.pipe()
        self._recv, from_chrome = os.pipe()

        def pipes() -> None:
            os.dup2(to_chrome, 3)
            os.dup2(from_chrome, 4)

        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
             "--remote-debugging-pipe", f"--user-data-dir={self.tmp.name}", "about:blank"],
            pass_fds=(3, 4), preexec_fn=pipes,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(to_chrome)
        os.close(from_chrome)
        self._buffer = b""
        self._id = 0
        target = self._call("Target.createTarget", url="about:blank")["targetId"]
        self.session = self._call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        self.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])

    def _call(self, method: str, session: str | None = None, **params: object) -> dict:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        os.write(self._send, json.dumps(message).encode() + b"\0")
        while True:
            while b"\0" not in self._buffer:
                chunk = os.read(self._recv, 1 << 20)
                assert chunk, "Chrome closed its DevTools pipe"
                self._buffer += chunk
            raw, self._buffer = self._buffer.split(b"\0", 1)
            reply = json.loads(raw)
            if reply.get("id") == self._id:
                assert "error" not in reply, reply["error"]
                return reply["result"]

    def call(self, method: str, **params: object) -> dict:
        return self._call(method, self.session, **params)

    def eval(self, expression: str) -> object:
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in result, result["exceptionDetails"]
        return result["result"].get("value")

    def open(self, html: str, width: int, height: int = FOLD) -> None:
        page = Path(self.tmp.name) / "page.html"
        page.write_text(html, encoding="utf-8")
        # Chrome will not size a window below 500px wide.
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
        self.call("Page.navigate", url=page.as_uri())
        self.eval("""new Promise(function (done) { (function wait() {
          var link = document.getElementById('link');
          if (link && / live/.test(' ' + link.className)) document.fonts.ready.then(function () { done(true); });
          else setTimeout(wait, 50); })(); })""")

    def screenshot(self, path: Path, width: int, full_page: bool = True) -> None:
        if full_page:
            height = self.eval("document.documentElement.scrollHeight")
            self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
        self.eval("new Promise(function (done) { requestAnimationFrame(function () { requestAnimationFrame(done); }); })")
        path.write_bytes(base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"]))

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()
        os.close(self._send)
        os.close(self._recv)
        self.tmp.cleanup()


@pytest.fixture
def browser():
    chrome = Browser()
    yield chrome
    chrome.close()


@needs_chrome
def test_fifteen_slots_fit_above_the_fold_at_1440x900(tmp_path: Path, browser: Browser) -> None:
    browser.open(_page_with_status_stub(fifteen_slot_snapshot(tmp_path), "index.html"), 1440)
    rows = browser.eval("""[].map.call(document.querySelectorAll('#lanes .lane[data-device]:not(#queue)'), function (lane) {
      return {device: lane.getAttribute('data-device'), tiles: [].map.call(lane.querySelectorAll('.tile'),
        function (t) { var b = t.getBoundingClientRect(); return [b.top, b.bottom]; })};
    })""")
    assert {row["device"]: len(row["tiles"]) for row in rows} == {"box": 5, "pc": 6, "mac": 4}
    for row in rows:
        assert len({top for top, _ in row["tiles"]}) <= 2, f"{row['device']} wraps onto a second row"
    assert browser.eval("innerHeight") == FOLD
    visible = browser.eval("""(function () {
      var floor = document.getElementById('statusline').getBoundingClientRect().top;
      return [].map.call(document.querySelectorAll('#lanes .tile'), function (tile) {
        var b = tile.getBoundingClientRect(), x = b.left + b.width / 2, y = b.bottom - 4;
        var hit = document.elementFromPoint(x, y);
        return {device: tile.closest('[data-device]').getAttribute('data-device'), bottom: b.bottom, floor: floor,
                own: !!hit && tile.contains(hit)};
      });
    })()""")
    hidden = [t for t in visible if t["bottom"] > t["floor"] or not t["own"]]
    assert not hidden, f"slots under the status line or off screen at 1440x{FOLD}: {hidden}"
    clipped = browser.eval("""[].filter.call(document.querySelectorAll('#lanes .tile-l3 .action, #lanes .tile-l3 .t'),
      function (e) { return e.scrollWidth > e.clientWidth; }).map(function (e) { return e.textContent; })""")
    assert not clipped, f"line 3 cuts the job's action or elapsed time: {clipped}"
    rounds = browser.eval("""[].map.call(document.querySelectorAll('#lanes a.tile .tile-l3'), function (l3) {
      var line = l3.getBoundingClientRect(), r = l3.querySelector('.round');
      var pips = r.querySelector('.pips').getBoundingClientRect(), label = r.lastElementChild.getBoundingClientRect();
      var x = label.left + label.width / 2, y = label.top + label.height / 2, hit = document.elementFromPoint(x, y);
      return {job: l3.closest('a.tile').getAttribute('href'), label: r.lastElementChild.textContent,
              shown: label.width > 0 && label.left >= line.left && label.right <= line.right + 0.5
                && label.top >= line.top && label.bottom <= line.bottom + 0.5 && !!hit && r.contains(hit),
              oneRow: Math.abs((pips.top + pips.bottom) - (label.top + label.bottom)) < 2};
    })""")
    assert len(rounds) == 13
    broken = [r for r in rounds if not (r["shown"] and r["oneRow"] and r["label"] == "2/3")]
    assert not broken, f"round pips and their n/m label must both show on one row: {broken}"


def write_screenshots(out: Path) -> list[Path]:
    html = _page_with_status_stub(fifteen_slot_snapshot(Path(tempfile.mkdtemp())), "index.html")
    chrome = Browser()
    try:
        paths = []
        for name, width in zip(SHOTS, (1440, 375)):
            chrome.open(html, width)
            paths.append(out.resolve() / name)
            chrome.screenshot(paths[-1], width, full_page=width < 1440)
        return paths
    finally:
        chrome.close()


def test_review_screenshots_are_committed() -> None:
    for name in SHOTS:
        assert (ROOT / "docs" / "screenshots" / name).stat().st_size > 0, name


if __name__ == "__main__":
    if sys.argv[1:2] != ["--shots"] or len(sys.argv) != 3 or CHROME is None:
        sys.exit("usage: python -m tests.test_dashboard_lanes --shots <dir>  (needs Chrome or Chromium)")
    for written in write_screenshots(Path(sys.argv[2])):
        print(written)
