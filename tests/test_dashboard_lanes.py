"""#140: the Lanes page (index.html): the queue lane, slot tiles per state,
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

from lib.ci_lane import ci_lane
from lib.dashboard import collect_snapshot, render_pages
from lib.dashboard_page import CSS, TILE_STATE, intake_groups, queue_speed
from lib.dashboard_pages.common import ticket_page_href
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_layout import BAR, HARNESS, NODE, _no_script, _run_page, _section, needs_node, run_harness
from tests.merge_queue_support import sha
from tests.test_ready_ci_gate import approved_then_pushed_pending

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


def _intake_item(issue: int, state: str, **extra) -> dict:
    return {"repo": "o/factory", "issue": str(issue), "state": state, "pr": None, "reason": None,
            "fed_age_seconds": None, "tier": None, **extra}


DAY = 86400
INTAKE_ITEMS = [
    _intake_item(1, "done", pr="101", fed_age_seconds=600, tier=1, title="Funnel row"),
    _intake_item(2, "waiting-blocked", reason="waiting on #1", title="Header line"),
    _intake_item(3, "error"),
    _intake_item(4, "waiting-capacity"),
    _intake_item(5, "waiting-blocked", reason="waiting on #2"),
    _intake_item(6, "refused-intake", reason="no acceptance criteria"),
    _intake_item(7, "done", pr="107", fed_age_seconds=2 * DAY),
    _intake_item(8, "waiting-blocked", reason="waiting on #5"),
    _intake_item(9, "waiting-capacity"),
    _intake_item(10, "fed", fed_age_seconds=3600),
    _intake_item(11, "eligible"),
]


def _tiles(tmp_path: Path) -> dict:
    snapshot = _base(tmp_path)
    template = snapshot["running"][0]
    snapshot["running"] = [_row(template, name) for name in STATES]
    snapshot["slots"]["devices"] = [
        {"name": "box", "slot_max": 9, "held": 0, "enabled": True, "placeholder": False,
         "harnesses": ["claude", "codex"]},
    ]
    snapshot["poller"]["linear_webhook"] = {
        "last_received_age_seconds": 30, "last_event": "Issue", "listener_age_seconds": 600, "dirty": 0,
        "warning": None, "note": None,
    }
    snapshot["feeder"] = {"eligible": 11, "fed": 3, "waiting_capacity": 2, "waiting_blocked": 3,
                          "items": copy.deepcopy(INTAKE_ITEMS)}
    return snapshot


def _job_tile(html: str, job: str) -> str:
    match = re.search(rf'<a class="tile [^"]*" href="[^"]*" title="[^"]*\(job {re.escape(job)}\)">.*?</a>', html, re.S)
    assert match, job
    return match.group(0)


def _tile(html: str, name: str) -> str:
    return _job_tile(html, f"t-{name}")


@pytest.mark.parametrize("name", list(STATES))
def test_each_slot_state_has_its_strip_chip_and_link(tmp_path: Path, name: str) -> None:
    snapshot = _tiles(tmp_path)
    html = _no_script(render_pages(snapshot)["index.html"])
    tile = _tile(html, name)
    row = next(r for r in snapshot["running"] if r["job"] == f"t-{name}")
    assert f'href="{ticket_page_href(row)}"' in tile
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


@pytest.mark.parametrize("name", ["stale", "overdue"])
def test_a_tile_whose_runner_is_gone_says_so_in_both_renders(tmp_path: Path, name: str) -> None:
    snapshot = _tiles(tmp_path)
    row = next(r for r in snapshot["running"] if r["job"] == f"t-{name}")
    row.update(job_state="stale", stale_cause="runner-gone")
    renders = [_no_script(render_pages(snapshot)["index.html"])]
    if NODE:
        renders.append(_run_page(tmp_path, snapshot, "main", BAR + ["hud", "lanes"])["html"]["lanes"])
    for html in renders:
        tile = _tile(html, name)
        assert re.findall(r'<span class="chip (\w+)">([^<]*)</span>', tile) == [("warn", "runner gone, worker alive")]
        assert f'data-px="{EFFECT[name]}"' in tile


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


def _model_tiles(tmp_path: Path) -> dict:
    snapshot = _tiles(tmp_path)
    template = snapshot["running"][0]
    assert set(template["exposure"].values()) == {"unknown"}
    snapshot["running"] = [
        _row(template, "build", job="t-full", exposure=dict(template["exposure"], model="claude-opus-5-5",
                                                             effort="medium")),
        _row(template, "build", job="t-model", exposure=dict(template["exposure"], model="claude-opus-5-5")),
        _row(template, "build", job="t-none"),
    ]
    return snapshot


def _assert_model_tiles(html: str) -> None:
    full, model, none = (_job_tile(html, job) for job in ("t-full", "t-model", "t-none"))
    assert _children(_line(full, 3)) == [
        "span.action=build", "span.round=", "span.model=claude-opus-5-5 · medium", "span.t=T+ "
    ]
    assert "· round 2/3 · claude-opus-5-5 · medium (job t-full)\"" in full
    assert _children(_line(model, 3)) == ["span.action=build", "span.round=", "span.model=claude-opus-5-5", "span.t=T+ "]
    assert "· round 2/3 · claude-opus-5-5 (job t-model)\"" in model
    assert _children(_line(none, 3)) == ["span.action=build", "span.round=", "span.t=T+ "]
    assert 'class="model"' not in none and "· round 2/3 (job t-none)\"" in none
    for tile in (full, model, none):
        assert [c.split("=")[0] for c in _children(_line(tile, 1))] == ["span.bot claude", "span.ticket"]
        assert 'class="verb live"' in _line(tile, 2) and 'class="hbt"' in _line(tile, 4)


def test_a_running_tile_shows_its_model_and_effort(tmp_path: Path) -> None:
    _assert_model_tiles(_no_script(render_pages(_model_tiles(tmp_path))["index.html"]))


@needs_node
def test_the_page_script_draws_the_same_model_and_effort(tmp_path: Path) -> None:
    snapshot = _model_tiles(tmp_path)
    _assert_model_tiles(_run_page(tmp_path, snapshot, "main", BAR + ["hud", "lanes"])["html"]["lanes"])


@pytest.mark.parametrize("omitted", [False, True], ids=["unknown", "no-key"])
def test_a_tile_with_no_exposure_row_still_renders_its_lines(tmp_path: Path, omitted: bool) -> None:
    snapshot = _base(tmp_path)
    row = snapshot["running"][0]
    assert row["exposure"]["model"] == "unknown" and row["exposure"]["effort"] == "unknown"
    if omitted:
        del row["exposure"]
    renders = [_no_script(render_pages(snapshot)["index.html"])]
    if NODE:
        renders.append(_run_page(tmp_path, snapshot, "main", BAR + ["hud", "lanes"])["html"]["lanes"])
    for html in renders:
        tile = _job_tile(html, row["job"])
        assert 'class="model"' not in tile
        assert [_line(tile, n) for n in (1, 2, 3, 4)] and '<span class="t">T+ <b>' in _line(tile, 3)


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


def _held_for_ci(snapshot: dict) -> dict:
    snapshot = copy.deepcopy(snapshot)
    snapshot["tickets"][0].update(pr="13", ci_wait={
        "reason": "ci-pending", "checks": ["build", "lint"], "waiting_seconds": 125, "round": 3,
    })
    snapshot["tickets"][2].update(ci_wait={
        "reason": "approved-ci-pending", "checks": [], "waiting_seconds": 3700,
    })
    snapshot["ci_lane"] = ci_lane(snapshot)
    return snapshot


def _ci_cards(lanes: str) -> list[str]:
    devices = re.findall(r'data-device="([^"]+)"', lanes)
    assert devices[-1] == "ci" and devices.count("ci") == 1, devices
    lane = lanes.split('data-device="ci"')[1]
    assert "0 running · 2 queued" in lane and "Queued · no runner yet" in lane
    return re.split(r'<div class="tile ci', lane)[1:]


def _assert_ci_lane(lanes: str, snapshot: dict) -> None:
    pending, approved = _ci_cards(lanes)
    for needle in ("AUT-7", 'href="https://github.com/aut0-code/aut0/pull/13"', "Queueing…", "then review",
                   "round 3", "build, lint", 'href="https://github.com/aut0-code/aut0/pull/13/checks"',
                   "waiting 2m 0"):
        assert needle in pending, needle
    for needle in ("cleffy#13", 'href="https://github.com/maxharris1/cleffy/pull/13"', "then merge",
                   "checks pending", 'href="https://github.com/maxharris1/cleffy/pull/13/checks"', "waiting 1h 01m"):
        assert needle in approved, needle
    assert "round" not in approved


def test_each_ticket_held_for_ci_gets_a_card_in_a_lane_after_the_devices(tmp_path: Path) -> None:
    base = _base(tmp_path)
    bare = _section(_no_script(render_pages(base)["index.html"]), "lanes").split('data-device="ci"')[1]
    assert '<div class="tile ci' not in bare and "0 running · 0 queued" in bare
    snapshot = _held_for_ci(base)
    _assert_ci_lane(_section(_no_script(render_pages(snapshot)["index.html"]), "lanes"), snapshot)


def test_the_ci_lane_follows_the_grok_wakes_lane_too(tmp_path: Path) -> None:
    snapshot = _held_for_ci(_tiles(tmp_path))
    snapshot["running"].append(_row(snapshot["running"][0], "build", job="wake-1", device=None))
    lanes = _section(_no_script(render_pages(snapshot)["index.html"]), "lanes")
    devices = re.findall(r'data-device="([^"]+)"', lanes)
    assert devices[-2:] == ["grok-wakes", "ci"], devices


@needs_node
def test_the_page_script_draws_the_ci_lane_like_the_server(tmp_path: Path) -> None:
    base = _base(tmp_path)
    snapshot = _held_for_ci(base)
    server = _section(_no_script(render_pages(snapshot)["index.html"]), "lanes")
    script = _run_page(tmp_path, snapshot, "main", BAR + ["hud", "lanes"])["html"]["lanes"]
    _assert_ci_lane(script, snapshot)
    assert _shape(script) == _shape(server[server.index(">") + 1 : server.rindex("<")])
    bare = _run_page(tmp_path, base, "main", BAR + ["hud", "lanes"])["html"]["lanes"]
    assert '<div class="tile ci' not in bare.split('data-device="ci"')[1]


def _card_round(lanes: str) -> str:
    return re.search(r'<span class="round">round (\d+)</span>', lanes.split('data-device="ci"')[1]).group(1)


@needs_node
def test_a_ci_card_after_an_approved_round_names_the_round_the_dispatcher_queues(tmp_path: Path) -> None:
    board, pr = approved_then_pushed_pending(tmp_path)
    snapshot = collect_snapshot(board.queue.path, tmp_path / "unused.log", now=board.now[0])
    server = _section(_no_script(render_pages(snapshot)["index.html"]), "lanes")
    script = _run_page(tmp_path, snapshot, "main", BAR + ["hud", "lanes"])["html"]["lanes"]

    pr.ci[sha(2)] = "success"
    board.poll()
    queued = json.loads(board.jobs("review")[-1]["detail"])["round"]
    assert queued == "2/3"
    assert _card_round(server) == _card_round(script) == "2"


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
    found = re.findall(r'data-device="([^"]+)"><div class="lane-head"><div class="lh1"><span class="name">[^<]*</span>'
                       r'<span class="chip ([^"]+)">([^<]*)</span>', lanes)
    assert {name: word for name, _, word in found} == {"box": "PROBE DOWN", "off": "DISABLED",
                                                       "later": "PLACEHOLDER", "ghost": "UNLISTED"}
    assert {name: tone for name, tone, _ in found}["box"] == "bad"
    assert re.search(r'data-device="pc"><div class="lane-head"><div class="lh1"><span class="name">pc</span>'
                     r'<div class="lane-meter"', lanes)


def test_probe_down_chips_but_a_healthy_device_has_none(tmp_path: Path) -> None:
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
    assert intake.count(QUIET) == 1 and "note:" not in intake
    assert f'<div class="intake"><p class="muted">{QUIET}</p>' in intake


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
    rows[1].update(source="#1", pr="9")
    snapshot["handoff_savings"]["recent"] = rows
    panel = _section(_no_script(render_pages(snapshot)["index.html"]), "just-happened")
    items = re.findall(r"<li>.*?</li>", panel)
    assert len(items) == 6
    assert re.findall(r'href="(https://github\.com/[^"]*)"', panel) == [
        "https://github.com/o/factory/issues/1", "https://github.com/o/factory/pull/9"]
    assert "grok wake" in items[0] and "grok wake" not in items[1]
    assert "6 newest handoffs" in panel and 'href="activity.html"' in panel


def _ticket_span(issue: int) -> str:
    return f'<span class="ticket">factory#{issue}</span>'


def _assert_intake_order(intake: str) -> None:
    head, body = intake.split("</h2>", 1)
    assert 'id="linear-webhook"' in head and "waiting" not in head
    t = _ticket_span
    marks = ['id="feeder"', t(3), t(6), "5 waiting", t(2), t(4), t(5),
             "<details><summary>2 more</summary>", t(8), t(9), "<details><summary>2 fed today</summary>", t(1), t(10)]
    at = [body.index(mark) for mark in marks]
    assert at == sorted(at), [marks[i] for i, _ in sorted(enumerate(at), key=lambda x: x[1])]
    assert t(7) not in intake and t(11) not in intake
    assert intake.count("<details>") == 2 and "<details open" not in intake
    assert "what bad" not in intake
    fed = intake[intake.index("2 fed today") :]
    assert "<span>→ PR #101</span>" in fed and "PR #107" not in intake
    assert fed.count('<span class="when">fed ') == 2


def test_intake_order_server(tmp_path: Path) -> None:
    _assert_intake_order(_section(_no_script(render_pages(_tiles(tmp_path))["index.html"]), "intake"))
    assert [[x["issue"] for x in group] for group in intake_groups(INTAKE_ITEMS)] == [
        ["3", "6"], ["2", "4", "5", "8", "9"], ["1", "10"]]


@needs_node
def test_intake_order_script(tmp_path: Path) -> None:
    _assert_intake_order(_run_page(tmp_path, _tiles(tmp_path), "main", BAR + ["hud", "intake"])["html"]["intake"])


def _ids(markup: str) -> list[str]:
    return re.findall(r'\bid="([^"]*)"', markup)


def test_intake_anchors_server(tmp_path: Path) -> None:
    page = _no_script(render_pages(_tiles(tmp_path))["index.html"])
    for anchor in ("intake", "feeder", "linear-webhook"):
        assert _ids(page).count(anchor) == 1, anchor
    assert _ids(_section(page, "intake")) == ["intake", "linear-webhook", "feeder"]


@needs_node
def test_intake_anchors_script(tmp_path: Path) -> None:
    snapshot = _tiles(tmp_path)
    assert _ids(_no_script(render_pages(snapshot)["index.html"])).count("intake") == 1
    out = _run_page(tmp_path, snapshot, "main", BAR + ["hud", "intake"])
    inside_intake = out["html"]["intake"]
    assert _ids(inside_intake) == ["linear-webhook", "feeder"]
    assert "feeder" in out["html"] and "linear-webhook" in out["html"]


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


def _webhook_only(tmp_path: Path) -> dict:
    snapshot = _tiles(tmp_path)
    snapshot["feeder"] = None
    snapshot["poller"]["linear_webhook"].update(warning="no webhook for 2h", note=QUIET)
    return snapshot


def _feeder_only(tmp_path: Path) -> dict:
    snapshot = _tiles(tmp_path)
    del snapshot["poller"]["linear_webhook"]
    return snapshot


@needs_node
@pytest.mark.parametrize(
    ("panel", "build"),
    [("lanes", _tiles), ("just-happened", _tiles), ("intake", _tiles),
     ("intake", _webhook_only), ("intake", _feeder_only)],
    ids=["lanes", "just-happened", "intake", "intake-webhook-only", "intake-feeder-only"],
)
def test_server_render_equals_script_render(tmp_path: Path, panel: str, build) -> None:
    snapshot = build(tmp_path)
    server = _section(_no_script(render_pages(snapshot)["index.html"]), panel)
    inner = server[server.index(">") + 1 : server.rindex("<")]
    out = _run_page(tmp_path, snapshot, "main", BAR + ["hud", panel])
    assert _shape(out["html"][panel]) == _shape(inner)


def _run_series(tmp_path: Path, snapshots: list[dict]) -> list[dict]:
    """The page script fed one snapshot per poll; the strips' state after each."""
    from lib.dashboard_page import page_script
    html = render_pages(snapshots[0])["index.html"]
    assert '<script src="/app.js" defer></script>' in html
    script = page_script(snapshots[0].get("statechart"))
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
    script = script.replace("    PX.bind();\n", "    PX.bind();\n    if (global.__snap) global.__snap();\n")
    ids = BAR + ["hud", "lanes", "just-happened", "intake"]
    return run_harness(tmp_path, harness, script, snapshots, "main", ids, [])["seen"]


@needs_node
def test_beat_only_on_a_real_heartbeat_drop(tmp_path: Path) -> None:
    first = _tiles(tmp_path)
    first["running"][1]["heartbeat_age_seconds"] = 40
    second = copy.deepcopy(first)
    second["running"][1]["heartbeat_age_seconds"] = 45
    third = copy.deepcopy(second)
    third["running"][1]["heartbeat_age_seconds"] = 3
    second["generated_at_epoch"] = first["generated_at_epoch"] + 1
    third["generated_at_epoch"] = first["generated_at_epoch"] + 2
    seen = _run_series(tmp_path, [first, second, third])
    assert len(seen) >= 3, seen
    assert seen[0]["job-t-build"]["beats"] == 0 and seen[1]["job-t-build"]["beats"] == 0
    assert seen[-1]["job-t-build"]["beats"] == 1
    assert seen[-1]["job-t-plan"]["beats"] == 0


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
    from lib.dashboard_page import app_css, page_script
    shim = (
        "<script>window.fetch = function () { return Promise.resolve({ok: true, status: 200,"
        f" json: function () {{ return Promise.resolve({json.dumps(snapshot)}); }}}}); }};</script>"
    )
    return (
        render_pages(snapshot)[page]
        .replace('<link rel="stylesheet" href="/app.css">', f"<style>{app_css()}</style>", 1)
        .replace('<script src="/app.js" defer></script>', shim + f"<script>{page_script(snapshot.get('statechart'))}</script>", 1)
    )


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
        state = self.eval("""new Promise(function (done) { (function wait() {
          var link = document.getElementById('link');
          if (link && !/ boot/.test(' ' + link.className)) document.fonts.ready.then(function () { done(link.className); });
          else setTimeout(wait, 50); })(); })""")
        assert state == "link live", f"the page settled on {state!r}, not 'link live'"

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


def _one_row(snapshot: dict) -> dict:
    for row in snapshot["running"]:
        if row["job_state"] != "running" or row["overdue"] or row["action"] not in ("plan", "build", "review", "fix"):
            row.update(STATES["build"], overdue=False, stuck=False)
    return snapshot


@needs_chrome
def test_fifteen_slots_fit_above_the_fold_at_1440x900(tmp_path: Path, browser: Browser) -> None:
    board = fifteen_slot_snapshot(tmp_path)
    browser.open(_page_with_status_stub(_one_row(copy.deepcopy(board)), "index.html"), 1440)
    rows = browser.eval("""[].map.call(document.querySelectorAll('#lanes .lane[data-device]:not(#queue):not([data-device="ci"])'), function (lane) {
      return {device: lane.getAttribute('data-device'), tiles: [].map.call(lane.querySelectorAll('.tile'),
        function (t) { var b = t.getBoundingClientRect(); return [b.top, b.bottom]; })};
    })""")
    assert {row["device"]: len(row["tiles"]) for row in rows} == {"box": 5, "pc": 6, "mac": 4}
    for row in rows:
        assert len({top for top, _ in row["tiles"]}) <= 2, f"{row['device']} wraps onto a second row"
    wrapped = browser.eval("""[].filter.call(document.querySelectorAll('#lanes a.tile .tile-l1, #lanes a.tile .tile-l3'),
      function (line) {
        var boxes = [].map.call(line.children, function (c) { return c.getBoundingClientRect(); });
        return Math.max.apply(null, boxes.map(function (b) { return b.top; }))
          >= Math.min.apply(null, boxes.map(function (b) { return b.bottom; }));
      }).map(function (line) { return line.closest('a.tile').getAttribute('href') + ' ' + line.className; })""")
    assert not wrapped, f"the fold holds only for one-row tiles, and these wrap: {wrapped}"
    assert browser.eval("innerHeight") == FOLD
    browser.eval("document.getElementById('lanes').scrollIntoView()")
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
    browser.open(_page_with_status_stub(board, "index.html"), 1440)
    browser.eval("document.getElementById('lanes').scrollIntoView()")
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


@needs_chrome
@pytest.mark.parametrize("width", [1440, 900, 600])
def test_needs_max_and_intake_are_full_width_rows(tmp_path: Path, browser: Browser, width: int) -> None:
    browser.open(_page_with_status_stub(_tiles(tmp_path), "index.html"), width)
    box = browser.eval("""(function () {
      var hud = document.getElementById('hud'), style = getComputedStyle(hud), b = hud.getBoundingClientRect();
      var out = {left: b.left + parseFloat(style.paddingLeft), right: b.right - parseFloat(style.paddingRight),
                 scroll: document.documentElement.scrollWidth, viewport: innerWidth};
      ['needs-max', 'intake', 'lanes', 'just-happened'].forEach(function (id) {
        var r = document.getElementById(id).getBoundingClientRect();
        out[id] = {left: r.left, right: r.right, top: r.top, bottom: r.bottom};
      });
      return out;
    })()""")
    for ident in ("needs-max", "intake", "just-happened"):
        assert abs(box[ident]["left"] - box["left"]) <= 0.5, (ident, box)
        assert abs(box[ident]["right"] - box["right"]) <= 0.5, (ident, box)
    assert box["needs-max"]["bottom"] <= box["intake"]["top"], box
    assert box["intake"]["bottom"] <= box["lanes"]["top"], box
    assert box["scroll"] <= box["viewport"], box


def _long_intake_reasons(tmp_path: Path) -> dict:
    from lib.feeder_gates import Holds, github_ref, parse_gate_lines

    lines = parse_gate_lines("Touches: tests/test_factory_dashboard_preserves_existing_intake_navigation_anchors.py",
                             "o/factory")
    holds = Holds()
    holds.take(github_ref("o/factory", 4), (), lines)
    reason = holds.collision(github_ref("o/factory", 2), lines).reason
    snapshot = _tiles(tmp_path)
    for item in snapshot["feeder"]["items"]:
        if item["issue"] in ("1", "2", "6", "9", "10"):
            item["reason"] = reason
    return snapshot


def _open_rendered(browser: Browser, snapshot: dict, render: str, width: int) -> None:
    if render == "script":
        browser.open(_page_with_status_stub(snapshot, "index.html"), width)
        return
    from lib.dashboard_page import app_css
    page = Path(browser.tmp.name) / "page.html"
    page.write_text(
        _no_script(render_pages(snapshot)["index.html"]).replace(
            '<link rel="stylesheet" href="/app.css">', f"<style>{app_css()}</style>", 1
        ),
        encoding="utf-8",
    )
    browser.call("Emulation.setDeviceMetricsOverride", width=width, height=FOLD, deviceScaleFactor=1, mobile=False)
    browser.call("Page.navigate", url=page.as_uri())
    browser.eval("""new Promise(function (done) { (function wait() {
      if (document.readyState === 'complete' && document.getElementById('intake')) done(true);
      else setTimeout(wait, 50); })(); })""")


@needs_chrome
@pytest.mark.parametrize("render", ["script", "server"])
def test_long_intake_reasons_wrap_on_a_phone(tmp_path: Path, browser: Browser, render: str) -> None:
    _open_rendered(browser, _long_intake_reasons(tmp_path), render, 375)
    browser.eval("[].forEach.call(document.querySelectorAll('#intake details'), function (d) { d.open = true; })")
    shown = browser.eval("""[].filter.call(document.querySelectorAll('#intake .what'),
      function (e) { return e.textContent.indexOf('test_factory_dashboard') >= 0 && e.getClientRects().length; }).length""")
    assert shown == 5, f"expected the long reason on five visible Intake rows, saw {shown}"
    width = browser.eval("document.documentElement.scrollWidth")
    assert width <= 375, f"phone page scrolls sideways: scrollWidth={width}"



@needs_chrome
@pytest.mark.parametrize("render", ["script", "server"])
@pytest.mark.parametrize("width", [375, 560, 700])
@pytest.mark.parametrize("webhook", [
    {"last_event": "Issue"},
    {"last_received_age_seconds": None, "listener_age_seconds": None},
    {"last_event": "x" * 64},
])
def test_the_intake_heading_wraps_the_webhook_line_under_its_title(
    tmp_path: Path, browser: Browser, render: str, width: int, webhook: dict
) -> None:
    snapshot = _tiles(tmp_path)
    snapshot["poller"]["linear_webhook"].update(webhook)
    _open_rendered(browser, snapshot, render, width)
    heading = browser.eval("""(function () {
      var title = document.querySelector('#intake h2 > span:first-child');
      return {text: title.textContent, height: title.getBoundingClientRect().height,
              lineHeight: parseFloat(getComputedStyle(title).lineHeight),
              count: document.getElementById('linear-webhook').textContent,
              scroll: document.documentElement.scrollWidth};
    })()""")
    assert heading["text"] == "Intake" and heading["count"].startswith("Linear webhook"), heading
    assert heading["height"] <= heading["lineHeight"] + 1, f"the Intake title splits across lines at {width}px: {heading}"
    assert heading["scroll"] <= width, f"the page scrolls sideways at {width}px: {heading}"

def write_screenshots(out: Path) -> list[Path]:
    html = _page_with_status_stub(_held_for_ci(fifteen_slot_snapshot(Path(tempfile.mkdtemp()))), "index.html")
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


if __name__ == "__main__":
    if sys.argv[1:2] != ["--shots"] or len(sys.argv) != 3 or CHROME is None:
        sys.exit("usage: python -m tests.test_dashboard_lanes --shots <dir>  (needs Chrome or Chromium)")
    for written in write_screenshots(Path(sys.argv[2])):
        print(written)


def _counted(tmp_path: Path, held: int = 3) -> dict:
    snapshot = _base(tmp_path)
    template = snapshot["running"][0]
    snapshot["running"] = [_row(template, "build"), _row(template, "review")]
    snapshot["queued"] = [dict(snapshot["queued"][0], job=f"q-{i}") for i in range(3)]
    snapshot["queue"]["totals"]["queued"] = 3
    snapshot["slots"]["devices"] = [
        {"name": "box", "slot_max": 4, "held": held, "enabled": True, "placeholder": False, "harnesses": ["claude"]},
        {"name": "pc", "slot_max": 3, "held": 0, "enabled": True, "placeholder": False, "harnesses": ["codex"]},
        {"name": "mac", "slot_max": 2, "held": 0, "enabled": False, "placeholder": False, "harnesses": []},
    ]
    return snapshot


def _header_counts(lanes: str) -> list[tuple[str, str, str]]:
    head = re.search(r"<h2>(.*?)</h2>", lanes, re.S).group(1)
    count = re.search(r'<span class="count">(.*?)</span>', head, re.S).group(1)
    return re.findall(r'<b(?: class="([a-z]+)")?>(\d+)</b> (running|held|idle|queued)', count)


def _server_lanes(snapshot: dict) -> str:
    return _section(_no_script(render_pages(snapshot)["index.html"]), "lanes")


HELD = [("ok", "2", "running"), ("warn", "1", "held"), ("", "4", "idle"), ("", "3", "queued")]
NONE_HELD = [("ok", "2", "running"), ("", "0", "held"), ("", "5", "idle"), ("", "3", "queued")]


def test_header_counts_server(tmp_path: Path) -> None:
    lanes = _server_lanes(_counted(tmp_path))
    assert _header_counts(lanes) == HELD
    assert ">2</b> running · <b" in lanes and "</b> queued</span></h2>" in lanes
    assert _header_counts(_server_lanes(_counted(tmp_path / "none", held=0))) == NONE_HELD


@needs_node
def test_header_counts_script(tmp_path: Path) -> None:
    out = _run_page(tmp_path / "held", _counted(tmp_path / "held"), "main", BAR + ["hud", "lanes"])
    assert _header_counts(out["html"]["lanes"]) == HELD
    assert out["text"]["lanes"].startswith("Lanes and slots2 running · 1 held · 4 idle · 3 queued")
    none = _counted(tmp_path / "none", held=0)
    out = _run_page(tmp_path / "none", none, "main", BAR + ["hud", "lanes"])
    assert _header_counts(out["html"]["lanes"]) == NONE_HELD


def test_kpi_row_is_gone(tmp_path: Path) -> None:
    html = render_pages(_counted(tmp_path))["index.html"]
    assert 'id="kpis"' not in html and "data-kpi" not in html and "Factory status" not in html
    assert "renderKpis" not in html and ".kpi{" not in html and "#kpis" not in html


def test_header_counts_colours_and_wrap_come_from_the_artboard() -> None:
    assert "#lanes h2{flex-wrap:wrap;gap:4px 8px}" in CSS
    assert "#lanes h2 .count{font-variant-numeric:tabular-nums}" in CSS
    assert "#lanes h2 .count b{font-weight:600;color:var(--text)}" in CSS
    assert "#lanes h2 .count b.ok{color:var(--ok)}#lanes h2 .count b.warn{color:var(--warn)}" in CSS
    phone = CSS[CSS.index("@media (max-width:640px){#lanes h2 .count") :]
    assert phone.startswith("@media (max-width:640px){#lanes h2 .count{flex:1 1 100%}}")


def _still_held(lanes: str, page: str) -> None:
    from lib.dashboard_page import app_css
    assert lanes.count('class="tile empty px-held"') == 1
    assert '<link rel="stylesheet" href="/app.css">' in page
    style = app_css()
    assert ".px-held::after{" in style
    assert "px-march" not in style and not re.search(r"px-held[^{}]*\{[^}]*animation", style)


def test_held_tile_is_still_server(tmp_path: Path) -> None:
    snapshot = _counted(tmp_path)
    _still_held(_server_lanes(snapshot), render_pages(snapshot)["index.html"])


@needs_node
def test_held_tile_is_still_script(tmp_path: Path) -> None:
    snapshot = _counted(tmp_path)
    out = _run_page(tmp_path, snapshot, "main", BAR + ["hud", "lanes"])
    _still_held(out["html"]["lanes"], render_pages(snapshot)["index.html"])


def _idle_lanes(tmp_path: Path) -> dict:
    snapshot = _base(tmp_path)
    template = snapshot["running"][0]
    snapshot["running"] = [_row(template, "build", device="pc", job="pc-build"),
                           _row(template, "stale", device="pc", job="pc-stale")]
    snapshot["slots"]["devices"] = [
        {"name": "box", "slot_max": 4, "held": 0, "enabled": True, "placeholder": False, "harnesses": ["claude"]},
        {"name": "pc", "slot_max": 5, "held": 3, "enabled": True, "placeholder": False,
         "harnesses": ["claude", "codex"]},
        {"name": "mac", "slot_max": 3, "held": 0, "enabled": False, "placeholder": False, "harnesses": []},
    ]
    snapshot["health"] = {"components": [], "counts": {}}
    return snapshot


def _lane(lanes: str, name: str) -> str:
    match = re.search(rf'<div class="lane[^"]*" data-device="{name}">.*?(?=<div class="lane[ "]|$)', lanes, re.S)
    assert match, name
    return match.group(0)


def _cells(lane: str) -> list[str]:
    tiles = lane[lane.index('<div class="tiles">') :]
    return re.findall(r'class="(tile [^"]*)"', tiles)


def _idle_labels(lane: str) -> list[str]:
    return re.findall(r'<div class="tile empty free"><canvas [^>]*></canvas><div class="lbl">([^<]*)</div></div>',
                      lane)


def _assert_no_work_lane(lanes: str) -> None:
    box = _lane(lanes, "box")
    assert _cells(box) == ["tile empty free"] * 4
    assert _idle_labels(box) == ["idle"] * 4


def _assert_mixed_lane(lanes: str) -> None:
    pc = _lane(lanes, "pc")
    assert _cells(pc) == ["tile claude", "tile claude", "tile empty px-held", "tile empty free", "tile empty free"]
    assert pc.index("job-pc-build") < pc.index("job-pc-stale")
    assert _idle_labels(pc) == ["idle", "idle"]


def _assert_disabled_lane(lanes: str) -> None:
    mac = _lane(lanes, "mac")
    assert _cells(mac) == ["tile empty free"] * 3
    assert _idle_labels(mac) == ["off"] * 3
    assert "idle" not in mac and ">DISABLED</span>" in mac
    assert ("", "6", "idle") in _header_counts(lanes)


def _assert_no_rail(lanes: str) -> None:
    assert not re.search(r'class="[^"]*\brail\b', lanes)
    assert not re.search(r"\d+ (idle|off)<", lanes)


def _assert_two_line_heads(lanes: str) -> None:
    heads = {m[0]: m[1:] for m in re.findall(
        r'data-device="([^"]+)"><div class="lane-head"><div class="lh1"><span class="name">\1</span>(.*?)'
        r'<div class="lane-meter" aria-hidden="true" style="width:(\d+)px">.*?</div></div>'
        r'<div class="lh2">(.*?)</div></div>', lanes, re.S)}
    assert heads == {
        "box": ("", "48", '<span class="sub">0 / 4 slots</span><span class="harness">claude</span>'),
        "pc": ("", "60", '<span class="sub">3 / 5 slots</span><span class="harness">claude · codex</span>'),
        "mac": ('<span class="chip muted">DISABLED</span>', "36", '<span class="sub">0 / 3 slots</span>'),
    }, heads


IDLE_CHECKS = (_assert_no_work_lane, _assert_mixed_lane, _assert_disabled_lane, _assert_no_rail,
               _assert_two_line_heads)


@pytest.mark.parametrize("check", IDLE_CHECKS, ids=lambda c: c.__name__)
def test_each_idle_slot_is_its_own_tile_server(tmp_path: Path, check) -> None:
    check(_server_lanes(_idle_lanes(tmp_path)))


@needs_node
@pytest.mark.parametrize("check", IDLE_CHECKS, ids=lambda c: c.__name__)
def test_each_idle_slot_is_its_own_tile_script(tmp_path: Path, check) -> None:
    check(_run_page(tmp_path, _idle_lanes(tmp_path), "main", BAR + ["hud", "lanes"])["html"]["lanes"])


def test_a_healthy_device_has_no_online_chip(tmp_path: Path) -> None:
    html = render_pages(_idle_lanes(tmp_path))["index.html"]
    assert "ONLINE" not in html
    pc = _lane(_section(_no_script(html), "lanes"), "pc")
    assert 'class="chip' not in pc[: pc.index('<div class="tiles">')]


def test_head_rules_come_from_the_artboard_and_no_rail_rules_remain() -> None:
    for rule in (
        ".lane-head{flex:0 0 224px;min-width:0;display:flex;flex-direction:column;gap:6px}",
        ".lh1{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}",
        ".lh2{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px}",
    ):
        assert rule in CSS, rule
    assert not re.search(r"(?<![\w-])\.rail\b", CSS)
    phone = CSS[CSS.index("@media (max-width:640px){.lane{") :]
    phone = phone[: phone.index("}}") + 2]
    for rule in (".lane{flex-direction:column;align-items:stretch;padding:10px;gap:10px}",
                 ".tiles{grid-template-columns:1fr}"):
        assert rule in phone, (rule, phone)


@needs_chrome
def test_idle_tiles_are_job_tile_sized_at_1440(tmp_path: Path, browser: Browser) -> None:
    browser.open(_page_with_status_stub(_idle_lanes(tmp_path), "index.html"), 1440)
    lanes = browser.eval("""[].map.call(document.querySelectorAll('#lanes .lane[data-device]:not(#queue):not([data-device="ci"])'), function (lane) {
      return {device: lane.getAttribute('data-device'), tiles: [].map.call(lane.querySelectorAll('.tiles > *'),
        function (t) { var b = t.getBoundingClientRect(), cs = getComputedStyle(t);
          return {cls: t.className, width: b.width, height: b.height, border: cs.borderTopStyle,
                  label: t.querySelector('.lbl') ? t.querySelector('.lbl').textContent : null}; })};
    })""")
    tiles = {lane["device"]: lane["tiles"] for lane in lanes}
    print("tiles at 1440:", tiles)
    jobs = [t for t in tiles["pc"] if t["cls"] == "tile claude"]
    idle = [t for t in tiles["pc"] if t["cls"] == "tile empty free"]
    assert len(jobs) == 2 and len(idle) == 2, tiles["pc"]
    for tile in idle:
        assert abs(tile["width"] - jobs[0]["width"]) < 1 and abs(tile["height"] - jobs[0]["height"]) < 1, tiles["pc"]
    free = [t for lane in tiles.values() for t in lane if t["cls"] == "tile empty free"]
    assert len(free) == 9 and all(t["border"] == "dashed" and t["height"] >= 124 for t in free), free
    assert [t["label"] for t in tiles["mac"]] == ["off"] * 3, tiles["mac"]


@needs_chrome
def test_every_tile_runs_full_width_and_stacks_at_375(tmp_path: Path, browser: Browser) -> None:
    snapshot = _idle_lanes(tmp_path)
    snapshot["queued"] = [dict(snapshot["queued"][0], job=f"q-{i}", no_waiter=None, no_waiter_link=None)
                          for i in range(2)]
    browser.open(_page_with_status_stub(snapshot, "index.html"), 375)
    assert browser.eval("document.documentElement.scrollWidth") <= 375
    lanes = browser.eval("""[].map.call(document.querySelectorAll('#lanes .lane[data-device]:not(#queue):not([data-device="ci"])'), function (lane) {
      var cs = getComputedStyle(lane), box = lane.getBoundingClientRect();
      var rect = function (e) { var b = e.getBoundingClientRect(); return {top: b.top, bottom: b.bottom, left: b.left, width: b.width}; };
      return {device: lane.getAttribute('data-device'),
              inner: box.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
              innerLeft: box.left + parseFloat(cs.paddingLeft),
              head: rect(lane.querySelector('.lane-head')),
              tiles: [].map.call(lane.querySelectorAll('.tiles > *'), rect)};
    })""")
    assert [(lane["device"], len(lane["tiles"])) for lane in lanes] == [("box", 4), ("pc", 5), ("mac", 3)]
    for lane in lanes:
        top = lane["head"]["bottom"]
        for tile in lane["tiles"]:
            assert abs(tile["left"] - lane["innerLeft"]) < 1 and abs(tile["width"] - lane["inner"]) < 1, lane
            assert tile["top"] >= top, lane
            top = tile["bottom"]
    rows = browser.eval("""[].map.call(document.querySelectorAll('#queue ul.qrail > li'), function (li) {
      var b = li.getBoundingClientRect(); return [b.top, b.bottom]; })""")
    assert len(rows) == 2 and rows[1][0] >= rows[0][1], rows


def _crowded_heads(tmp_path: Path) -> dict:
    snapshot = _idle_lanes(tmp_path)
    snapshot["running"] = [dict(snapshot["running"][0], device="box"), dict(snapshot["running"][1], device="ghost")]
    snapshot["slots"]["devices"] = [
        {"name": "box", "slot_max": 10, "held": 0, "enabled": True, "placeholder": False, "harnesses": ["claude"]},
        {"name": "pc", "slot_max": 24, "held": 0, "enabled": True, "placeholder": True, "harnesses": ["codex"]},
        {"name": "mac", "slot_max": 16, "held": 0, "enabled": False, "placeholder": False, "harnesses": []},
        {"name": "build-runner-with-a-long-hostname", "slot_max": 12, "held": 0, "enabled": True,
         "placeholder": False, "harnesses": ["claude", "codex", "grok"]},
    ]
    snapshot["health"] = {"components": [
        {"component": "device-probe", "device": name, "state": "down"}
        for name in ("box", "build-runner-with-a-long-hostname", "ghost")
    ], "counts": {}}
    return snapshot


@needs_chrome
def test_device_heads_stay_clear_of_their_tiles_at_1440(tmp_path: Path, browser: Browser) -> None:
    browser.open(_page_with_status_stub(_crowded_heads(tmp_path), "index.html"), 1440)
    heads = browser.eval("""[].map.call(document.querySelectorAll('#lanes .lane[data-device]'), function (lane) {
      var head = lane.querySelector('.lane-head'), box = head.getBoundingClientRect();
      var first = lane.querySelector('.tiles > *, .lane-body');
      var spill = [].filter.call(head.querySelectorAll('*'), function (e) {
        var b = e.getBoundingClientRect();
        return b.width && (b.left < box.left - 0.5 || b.right > box.right + 0.5);
      }).map(function (e) { return e.className + ' ' + e.getBoundingClientRect().right; });
      return {device: lane.getAttribute('data-device'), right: box.right, spill: spill,
              chip: !!head.querySelector('.chip'),
              next: first ? first.getBoundingClientRect().left : null};
    })""")
    devices = {h["device"]: h for h in heads}
    assert {"box", "pc", "mac", "ghost", "build-runner-with-a-long-hostname"} <= set(devices), heads
    assert all(devices[d]["chip"] for d in ("box", "pc", "mac", "ghost", "build-runner-with-a-long-hostname")), heads
    for head in heads:
        assert not head["spill"], head
        assert head["next"] is None or head["right"] <= head["next"], head
