import json
import re
import shutil
import subprocess
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
var api = new Function(input.script + '\nreturn { journeyBars: journeyBars, drawJourney: drawJourney };')();
var out = { bars: {}, frames: {} };
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
      sig: hash(JSON.stringify(ctx.calls))
    };
  } catch (e) {
    out.frames[name] = { error: String(e && e.stack || e) };
  }
});
process.stdout.write(JSON.stringify(out));
"""


def _model(path=PATH, hops=len(FUTURE)) -> dict:
    visits = [
        {
            "index": i, "state": state, "who": who, "started_at": 1000.0 + i,
            "duration_seconds": dur, "time_known": True, "off_chart": off,
            "reason": None, "current": i == len(path) - 1, "stop": stop,
        }
        for i, (state, who, dur, off, stop) in enumerate(path)
    ]
    return {"visits": visits, "hops_to_shipped": hops, "all_durations_known": True, "terminal": False}


FRAME = {"scale": "even", "selected": 3, "flash": 0, "beat": -1, "reduced": False,
         "still": False, "startClock": "1:47 PM", "nowClock": "3:48 PM"}
FRAMES = {
    "t0": dict(FRAME, t=0),
    "t1_5": dict(FRAME, t=1.5, flash=0.5),
    "t10": dict(FRAME, t=10, beat=0.4),
    "t10_scale": dict(FRAME, t=10, scale="scale"),
    "still": dict(FRAME, t=10, still=True),
    "reduced": dict(FRAME, t=0, reduced=True, flash=1),
}


def _run(tmp_path: Path, model: dict, frames: dict = FRAMES) -> dict:
    data = tmp_path / "input.json"
    data.write_text(json.dumps({"script": SCRIPT, "model": model, "frames": frames}))
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    done = subprocess.run([NODE, str(harness), str(data)], capture_output=True, text=True, timeout=30, check=True)
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
    assert "NOW · 3:48 PM" in texts
    assert "1:47 PM" in texts
    assert f"ahead · {len(FUTURE)}+ hops to shipped" in texts
    assert {"SHIPPED", "SHIP", "REVIEW", "BUILD", "PLAN", "ASK", "STOPPED"} <= set(texts)


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
    assert LANE["merge-queue"] == "ship" and LANE["updating"] == "build"
    shared = json.loads(re.search(r"var JOURNEY = (\{.*?\});\n", SCRIPT).group(1))
    assert shared == {"lane": LANE, "tint": TINT, "stop": STOP}


def test_script_is_safe_to_splice_into_the_page() -> None:
    for needle in ("</script", "url(", "src=", "data:", "http://", "https://", "__JOURNEY__"):
        assert needle not in SCRIPT, needle
