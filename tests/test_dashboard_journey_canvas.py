import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from lib.dashboard_pages.journey_canvas import LANE, SCRIPT, STOP, TINT

NODE = shutil.which("node") or ("/usr/bin/node" if Path("/usr/bin/node").exists() else None)
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

PATH = [
    ("intake", "script", 20, False, False),
    ("questions", "script", 70, False, False),
    ("planning", "claude", 360, False, False),
    ("plan-check", "script", 30, False, False),
    ("building", "claude", 1320, False, False),
    ("planning", "claude", 240, True, False),
    ("review", "codex", 540, True, False),
    ("fixing", "claude", 720, False, False),
    ("stuck", "grok", 3720, False, True),
    ("fixing", "cursor", 248, True, False),
]
FUTURE = ["review", "ship", "merge-queue", "merged", "done"]

HARNESS = r"""
var fs = require('fs');
var input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
function stubContext() {
  var calls = [], state = {};
  var ctx = {
    calls: calls,
    fillRect: function (x, y, w, h) { calls.push(['fillRect', state.fillStyle, x, y, w, h]); },
    fillText: function (text, x, y) { calls.push(['fillText', String(text), x, y]); },
    measureText: function (text) { return { width: String(text).length * 6 }; },
    save: function () {}, restore: function () {}, beginPath: function () {},
    rect: function () {}, clip: function () {}
  };
  ['fillStyle', 'font', 'textAlign', 'textBaseline'].forEach(function (k) {
    Object.defineProperty(ctx, k, { get: function () { return state[k]; }, set: function (v) { state[k] = v; } });
  });
  return ctx;
}
function hash(text) {
  var h = 5381;
  for (var i = 0; i < text.length; i++) h = ((h * 33) ^ text.charCodeAt(i)) >>> 0;
  return h;
}
var api = new Function(input.script + '\nreturn { journeyBars: journeyBars, drawJourney: drawJourney, journeyClock: journeyClock };')();
var out = { bars: {}, frames: {}, clocks: (input.clocks || []).map(function (t) { return api.journeyClock(t); }) };
out.bars.even = api.journeyBars(input.model, 'even');
out.bars.scale = api.journeyBars(input.model, 'scale');
Object.keys(input.frames).forEach(function (name) {
  var ctx = stubContext();
  try {
    api.drawJourney(ctx, input.model, input.frames[name]);
    out.frames[name] = {
      error: null,
      texts: ctx.calls.filter(function (c) { return c[0] === 'fillText'; }).map(function (c) { return c[1]; }),
      rects: ctx.calls.filter(function (c) { return c[0] === 'fillRect'; }).length,
      fills: ctx.calls.filter(function (c) { return c[0] === 'fillRect'; }).map(function (c) { return c.slice(1); }),
      sig: hash(JSON.stringify(ctx.calls))
    };
  } catch (e) {
    out.frames[name] = { error: String(e && e.stack || e) };
  }
});
process.stdout.write(JSON.stringify(out));
"""


def _model(path=PATH, hops=len(FUTURE), ended=None) -> dict:
    visits = [
        {
            "index": i, "state": state, "who": who, "started_at": 1000.0 + i,
            "duration_seconds": dur, "time_known": True, "off_chart": off,
            "reason": None, "current": i == len(path) - 1, "stop": stop,
        }
        for i, (state, who, dur, off, stop) in enumerate(path)
    ]
    return {"visits": visits, "hops_to_shipped": hops, "all_durations_known": True, "terminal": False, "ended": ended}


FRAME = {"scale": "even", "selected": 3, "flash": 0, "beat": -1, "reduced": False,
         "still": False, "startClock": "13:47", "nowClock": "15:48"}
FRAMES = {
    "t0": dict(FRAME, t=0),
    "t1_5": dict(FRAME, t=1.5, flash=0.5),
    "t10": dict(FRAME, t=10, beat=0.4),
    "t10_scale": dict(FRAME, t=10, scale="scale"),
    "still": dict(FRAME, t=10, still=True),
    "reduced": dict(FRAME, t=0, reduced=True, flash=1),
}


def _run(tmp_path: Path, model: dict, frames: dict = FRAMES, clocks: list = (), tz: str = "UTC") -> dict:
    data = tmp_path / "input.json"
    data.write_text(json.dumps({"script": SCRIPT, "model": model, "frames": frames, "clocks": list(clocks)}))
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    done = subprocess.run(
        [NODE, str(harness), str(data)], capture_output=True, text=True, timeout=30, check=True,
        env={**os.environ, "TZ": tz},
    )
    return json.loads(done.stdout)


@needs_node
def test_one_hit_rect_per_visit_in_order_and_even_steps_are_equal(tmp_path: Path) -> None:
    out = _run(tmp_path, _model())
    for mode in ("even", "scale"):
        bars = out["bars"][mode]
        assert [b["index"] for b in bars] == list(range(len(PATH))), mode
        lefts = [b["x"] for b in bars]
        assert lefts == sorted(lefts), mode
        for left, right in zip(bars, bars[1:]):
            assert left["x"] + left["w"] <= right["x"], mode
        assert all(0 <= b["x"] and b["x"] + b["w"] <= 1000 and b["y"] >= 0 and b["y"] + b["h"] <= 336 for b in bars)
    assert len({b["w"] for b in out["bars"]["even"]}) == 1
    scale = {b["index"]: b["w"] for b in out["bars"]["scale"]}
    assert scale[8] > scale[4] > scale[0], "to scale follows duration_seconds"


@needs_node
def test_long_journeys_stay_on_the_canvas_with_equal_steps(tmp_path: Path) -> None:
    path = [("fixing" if i % 2 else "review", "claude", 60, False, False) for i in range(40)]
    bars = _run(tmp_path, _model(path))["bars"]["even"]
    assert len(bars) == 40
    assert len({b["w"] for b in bars}) == 1
    assert bars[-1]["x"] + bars[-1]["w"] <= 1000


@needs_node
def test_every_frame_draws_without_throwing(tmp_path: Path) -> None:
    frames = _run(tmp_path, _model())["frames"]
    for name, frame in frames.items():
        assert frame["error"] is None, (name, frame["error"])
        assert frame["rects"] > 0, name


@needs_node
def test_bars_up_to_now_carry_their_names_and_nothing_ahead_is_named(tmp_path: Path) -> None:
    frames = _run(tmp_path, _model())["frames"]
    past = {state for state, *_ in PATH}
    ahead = set(FUTURE) - past
    for name, frame in frames.items():
        for text in frame["texts"]:
            for state in ahead:
                assert not re.search(r"(?<![\w-])" + re.escape(state) + r"(?![\w-])", text), (name, text, state)
    assert past <= set(frames["t10"]["texts"])
    assert not past & set(frames["t0"]["texts"])
    texts = frames["t10"]["texts"]
    assert "NOW · 15:48 ET" in texts
    assert "13:47" in texts
    assert [text for text in texts if re.search(r"\bET\b", text)] == ["NOW · 15:48 ET"]
    assert f"ahead · {len(FUTURE)}+ hops to shipped" in texts
    assert {"SHIPPED", "SHIP", "REVIEW", "BUILD", "PLAN", "ASK", "STOPPED"} <= set(texts)


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


CLOCKS = {
    "2026-11-16T15:04:00+00:00": "10:04",
    "2026-07-01T15:04:00+00:00": "11:04",
    "2026-11-16T04:05:00+00:00": "23:05",
    "2026-11-16T05:07:00+00:00": "00:07",
}


@needs_node
@pytest.mark.parametrize("tz", ["UTC", "Asia/Tokyo"])
def test_journey_clock_reads_us_eastern_whatever_the_host_zone(tmp_path: Path, tz: str) -> None:
    clocks = [_epoch(iso) for iso in CLOCKS] + [None]
    out = _run(tmp_path, _model(), {}, clocks, tz)
    assert out["clocks"] == [*CLOCKS.values(), ""]


@needs_node
def test_now_tag_without_a_clock_names_no_zone(tmp_path: Path) -> None:
    texts = _run(tmp_path, _model(), {"t10": dict(FRAME, t=10, nowClock="")})["frames"]["t10"]["texts"]
    assert "NOW · " in texts
    assert not any(re.search(r"\bET\b", text) for text in texts)


@needs_node
def test_ahead_text_is_empty_without_hops(tmp_path: Path) -> None:
    for hops in (0, None):
        texts = _run(tmp_path, _model(hops=hops))["frames"]["t10"]["texts"]
        assert not any("ahead" in text for text in texts), hops


@needs_node
def test_reduced_motion_is_one_still_frame(tmp_path: Path) -> None:
    frames = {"a": FRAMES["reduced"], "b": dict(FRAMES["reduced"], t=7.3), "c": dict(FRAMES["reduced"], t=0.2, beat=0.5)}
    out = _run(tmp_path, _model(), frames)["frames"]
    assert out["a"]["sig"] == out["b"]["sig"] == out["c"]["sig"]


@needs_node
def test_after_draw_in_the_frame_keeps_moving_unless_reduced(tmp_path: Path) -> None:
    frames = {"a": dict(FRAME, t=10), "b": dict(FRAME, t=10.4)}
    out = _run(tmp_path, _model(), frames)["frames"]
    assert out["a"]["sig"] != out["b"]["sig"]


def test_tint_is_keyed_by_who_with_a_separate_stop_colour() -> None:
    assert set(TINT) == {"script", "claude", "codex", "cursor", "grok", "human"}
    assert STOP == "#f7768e" and STOP not in TINT.values()
    assert LANE["merge-queue"] == "ship" and LANE["updating"] == "ship"
    shared = json.loads(re.search(r"var JOURNEY = (\{.*?\});\n", SCRIPT).group(1))
    assert shared == {"lane": LANE, "tint": TINT, "stop": STOP}


def test_script_is_safe_to_splice_into_the_page() -> None:
    for needle in ("</script", "url(", "src=", "data:", "http://", "https://", "__JOURNEY__"):
        assert needle not in SCRIPT, needle


SHIPPED = [
    ("intake", "script", 20, False, False),
    ("planning", "claude", 90000, False, False),
    ("building", "claude", 1480, False, False),
    ("review", "codex", 400, False, False),
    ("merge-queue", "script", 1500, False, False),
    ("merged", "script", 259200, False, False),
]
STOPPED = [
    ("intake", "script", 20, False, False),
    ("questions", "script", 180, False, False),
    ("planning", "claude", 1200, False, False),
    ("refused", "script", 266400, False, True),
]
ENDED = {"scale": "even", "selected": -1, "flash": 0, "beat": -1, "reduced": False, "still": False,
         "startClock": "06:04", "nowClock": "10:10", "endClock": "07:56"}
LANE_TOP = {"shipped": 4, "stop": 62}


def _frame_rects(frame: dict, colour: str) -> list[list]:
    return [f[1:] for f in frame["fills"] if f[0] == colour]


def _ended_frames(**extra) -> dict:
    return {
        "t0": dict(ENDED, t=0), "t1_5": dict(ENDED, t=1.5, flash=0.5), "t3_5": dict(ENDED, t=3.5),
        "t3_9": dict(ENDED, t=3.9), "t10": dict(ENDED, t=10, beat=0.4), "t10_4": dict(ENDED, t=10.4),
        "reduced": dict(ENDED, t=0, reduced=True, flash=1), "scale": dict(ENDED, t=10, scale="scale"),
        **extra,
    }


@needs_node
def test_a_shipped_journey_draws_in_settles_green_once_then_never_moves(tmp_path: Path) -> None:
    frames = _run(tmp_path, _model(SHIPPED, hops=0, ended="shipped"), _ended_frames())["frames"]
    assert all(f["error"] is None for f in frames.values())
    assert frames["t0"]["sig"] != frames["t10"]["sig"]
    assert _frame_rects(frames["t1_5"], "#d6efbf"), "the draw-in carries a green token"
    assert frames["t3_5"]["sig"] != frames["t3_9"]["sig"], "the green settle plays after the draw-in"
    assert _frame_rects(frames["t3_5"], "#9ece6a")
    assert frames["t10"]["sig"] == frames["t10_4"]["sig"], "nothing moves once settled"
    assert frames["reduced"]["sig"] == frames["t10"]["sig"], "reduced motion shows the settled frame"
    assert not _frame_rects(frames["t10"], "#d6efbf")
    assert not _frame_rects(frames["t10"], "#7dcfff"), "no current bar, NOW tag or sparks"
    assert not _frame_rects(frames["t10"], "#3c6f8c"), "no NOW line"


@needs_node
def test_a_shipped_journey_ends_in_the_shipped_lane_at_the_flag_with_the_end_time(tmp_path: Path) -> None:
    out = _run(tmp_path, _model(SHIPPED, hops=0, ended="shipped"), _ended_frames())
    frame = out["frames"]["t10"]
    texts = frame["texts"]
    assert not any(t.startswith("NOW") for t in texts)
    assert not any("ahead" in t for t in texts)
    assert texts[-1] == "merged 07:56", "the ruler ends with the end time"
    assert "1d 1h" in texts, "a day-long step reads in days"
    assert "3d 0h" not in texts, "the end bar carries no duration"
    last = out["bars"]["even"][-1]
    frame_top = [r for r in _frame_rects(frame, "#c0caf5") if r[1] == (LANE_TOP["shipped"] + 1) * 4]
    assert frame_top and frame_top[0][0] == last["x"] - 4, "the selected last bar is framed white in the shipped lane"
    pole = [r for r in _frame_rects(frame, "#9ece6a") if r[3] == 28 and r[1] == 16]
    assert [r[0] for r in pole] == [last["x"] + last["w"] + 4], "the flag stands right after the last bar"
    assert out["bars"]["scale"][-1]["w"] == 48, "to scale, the end bar is a fixed end mark"


@needs_node
def test_a_stopped_journey_lies_on_the_red_floor_and_never_moves(tmp_path: Path) -> None:
    out = _run(tmp_path, _model(STOPPED, hops=3, ended="stopped"), _ended_frames())
    frames = out["frames"]
    assert all(f["error"] is None for f in frames.values())
    sigs = {name: f["sig"] for name, f in frames.items() if name != "scale"}
    assert len(set(sigs.values())) == 1, sigs
    frame = frames["t10"]
    texts = frame["texts"]
    assert not any(t.startswith("NOW") for t in texts)
    assert not any("ahead" in t for t in texts)
    assert texts[-1] == "stopped 07:56"
    assert not _frame_rects(frame, "#7dcfff") and not _frame_rects(frame, "#3c6f8c")
    last = out["bars"]["even"][-1]
    red = [r for r in _frame_rects(frame, STOP) if last["x"] <= r[0] < last["x"] + last["w"]]
    rows = {r[1] // 4 for r in red}
    assert rows and min(rows) == LANE_TOP["stop"] + 2 and max(rows) <= LANE_TOP["stop"] + 6
    assert len(red) > last["w"] // 4, "a dithered red end bar, not one lit row"
    frame_top = [r for r in _frame_rects(frame, "#c0caf5") if r[1] == (LANE_TOP["stop"] + 1) * 4]
    assert frame_top and frame_top[0][0] == last["x"] - 4
    assert out["bars"]["scale"][-1]["w"] == 48


@needs_node
def test_a_stuck_flag_puts_the_last_step_on_the_floor(tmp_path: Path) -> None:
    path = STOPPED[:3]
    out = _run(tmp_path, _model(path, hops=3, ended="stopped"), {"t": dict(ENDED, t=10)})
    last = out["bars"]["even"][-1]
    red = [r for r in _frame_rects(out["frames"]["t"], STOP) if last["x"] <= r[0] < last["x"] + last["w"]]
    assert {r[1] // 4 for r in red} >= {LANE_TOP["stop"] + 2}


@needs_node
def test_a_live_journey_frames_only_a_picked_earlier_step(tmp_path: Path) -> None:
    frames = {"own": dict(FRAME, t=10, selected=len(PATH) - 1), "picked": dict(FRAME, t=10, selected=2)}
    out = _run(tmp_path, _model(), frames)["frames"]
    assert not _frame_rects(out["own"], "#c0caf5")
    assert _frame_rects(out["picked"], "#c0caf5")
