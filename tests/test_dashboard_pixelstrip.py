"""The PixelStrip engine and its state-to-effect table (#138, SPEC section 6).

The node tests run the page script's PixelStrip module against a fake canvas,
clock, requestAnimationFrame, IntersectionObserver and matchMedia, and run the
artboard's own class (docs/design/mission-control/reference/artboards/
PixelStrip.dc.html) beside it to prove the drawing code is the same.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import lib.dashboard_page as page
from lib.dashboard import collect_snapshot, render_pages
from tests.test_dashboard import NOW, seeded

ROOT = Path(__file__).resolve().parents[1]
ARTBOARD = ROOT / "docs" / "design" / "mission-control" / "reference" / "artboards" / "PixelStrip.dc.html"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

ANIMATED = {"field", "fill", "scan", "patch", "spin3", "twinkle", "conveyor", "meter", "caret"}
STILL = {"decay", "static", "off"}
BROKEN = ("stuck", "blocked", "dead", "overdue", "capped", "refused", "no_waiter", "down")


def _artboard_class() -> str:
    source = ARTBOARD.read_text(encoding="utf-8")
    start = source.index("class Component extends DCLogic {")
    return source[start : source.index("</script>", start)]


HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const pxSource = fs.readFileSync(process.argv[2], 'utf8');
const artboard = fs.readFileSync(process.argv[3], 'utf8');
const opts = JSON.parse(process.argv[4]);
let clock = 1000;
const rafQueue = [];
const docListeners = {};
const mqListeners = [];
const observers = [];
let found = [];
global.window = global;
global.performance = {now: () => clock};
global.devicePixelRatio = opts.dpr || 1;
global.requestAnimationFrame = (fn) => { rafQueue.push(fn); return rafQueue.length; };
global.matchMedia = (q) => ({matches: !!opts.reduced, addEventListener: (t, fn) => mqListeners.push(fn)});
global.IntersectionObserver = class {
  constructor(cb) { this.cb = cb; this.watched = new Set(); observers.push(this); }
  observe(el) { this.watched.add(el); }
  unobserve(el) { this.watched.delete(el); }
};
function canvas(attrs, w, h) {
  const ops = [];
  const ctx = {_fs: '', set fillStyle(v) { this._fs = v; ops.push(['s', v]); }, get fillStyle() { return this._fs; },
    fillRect(x, y, w2, h2) { ops.push(['r', x, y, w2, h2]); }};
  return {tagName: 'canvas', attrs: Object.assign({}, attrs), order: Object.keys(attrs), ops,
    clientWidth: w || 160, clientHeight: h || 16, width: 300, height: 150,
    getContext() { return ctx; },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { if (!(k in this.attrs)) this.order.push(k); this.attrs[k] = String(v); }};
}
function esc(v) { return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#x27;'); }
function serialize(el) { return '<canvas ' + el.order.map(k => k + '="' + esc(el.attrs[k]) + '"').join(' ') + '></canvas>'; }
global.document = {hidden: false,
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  querySelectorAll: (sel) => { if (sel !== 'canvas.px') throw new Error(sel); return found; },
  createElement: (tag) => canvas({}, 160, 16)};
const PX = vm.runInThisContext(pxSource);
class DCLogic {}
const Component = vm.runInThisContext('(function (DCLogic) { ' + artboard + '; return Component; })')(DCLogic);
function frame(at) { clock = at; const q = rafQueue.splice(0); q.forEach(fn => fn(at)); return q.length; }
function draws(el) { return el.ops.filter(o => o[0] === 'r' && o[1] === 0 && o[2] === 0 && o[3] === el.width).length; }
function rects(el) { return el.ops.filter(o => o[0] === 'r').length; }
const out = {};
(function () { SCENARIO })();
process.stdout.write(JSON.stringify(out));
"""


def _node(tmp_path: Path, scenario: str, **opts: object) -> dict:
    (tmp_path / "px.js").write_text(page.px_script(), encoding="utf-8")
    (tmp_path / "artboard.js").write_text(_artboard_class(), encoding="utf-8")
    (tmp_path / "harness.js").write_text(HARNESS.replace("SCENARIO", scenario), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(tmp_path / "px.js"), str(tmp_path / "artboard.js"),
         json.dumps(opts)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- the drawing code is the artboard's ---------------------------------------------

EQUALITY = r"""
const base = {_t0: 0, _beatSeen: 0, _burstSeen: 0, _beatAt: -1000, _burstAt: -1000, _doneAt: null, _sysStill: false};
function make(cls, props, w, h) {
  const s = cls === 'A' ? new Component() : Object.create(PX.Strip.prototype);
  Object.assign(s, base, {props: Object.assign({}, props), _el: canvas({}, w, h), _ctx: null, _sig: null});
  return s;
}
function run(props, w, h, times) {
  const a = make('A', props, w, h), b = make('B', props, w, h);
  times.forEach(t => { a._draw(t); b._draw(t); });
  return [JSON.stringify(a._el.ops), JSON.stringify(b._el.ops), a._el.ops.length, a._el.width, b._el.width];
}
const effects = ['field', 'fill', 'scan', 'patch', 'spin3', 'static', 'twinkle', 'conveyor', 'meter', 'caret', 'off'];
const tints = ['#9ece6a', '#e0af68', '#f7768e', '#7aa2f7'];
const sizes = [[160, 16], [640, 40], [200, 8]];
const times = [500, 1700, 3300, 7900, 12100];
const extra = {progress: '0.42', load: '0.7', used: '3', slots: '8', speed: '2', rate: '1', seed: '1', cell: 4, tintMix: 1, beat: 0};
out.cases = 0; out.bad = []; out.ops = 0;
function check(label, props, w, h, ts) {
  const [a, b, n, wa, wb] = run(props, w, h, ts);
  out.cases += 1; out.ops += n;
  if (a !== b || wa !== wb) out.bad.push(label);
}
effects.forEach(effect => tints.forEach(tint => sizes.forEach(([w, h]) => [false, true].forEach(still => {
  check([effect, tint, w, h, still].join('|'), Object.assign({}, extra, {effect, tint, still}), w, h, times);
}))));
// decay: the artboard's timer-driven amount equals the production amount
// whenever progress names the same 0.085 step.
sizes.forEach(([w, h]) => times.concat([20000, 23000]).forEach(t => {
  const k = Math.floor((t / 1000 + 1.37) / 2) % 12;
  const progress = String(k >= 10 ? 1 : (k + 0.5) / 10);
  check('decay|' + w + '|' + t, Object.assign({}, extra, {effect: 'decay', tint: '#e0af68', progress}), w, h, [t]);
}));
// one-shots over any effect, sampled through their whole run.
const shots = [1000, 1100, 1250, 1400, 1550, 1700, 1900, 2500];
['field', 'fill', 'static', 'off'].forEach(effect => {
  check('beat|' + effect, Object.assign({}, extra, {effect, tint: '#e0af68', beat: 1}), 640, 40, shots);
  check('burst|' + effect, Object.assign({}, extra, {effect, tint: '#7aa2f7', burst: 1}), 640, 40, shots);
  check('done|' + effect, Object.assign({}, extra, {effect, tint: '#bb9af7', done: true}), 640, 40, shots);
});
"""


@needs_node
def test_drawing_code_matches_the_artboard_call_for_call(tmp_path: Path) -> None:
    out = _node(tmp_path, EQUALITY)
    assert out["bad"] == []
    assert out["cases"] > 280
    assert out["ops"] > 100000  # the comparison saw real drawing


@needs_node
def test_dpr_is_capped_at_two_and_every_cell_is_an_integer_rect(tmp_path: Path) -> None:
    out = _node(tmp_path, r"""
      const s = Object.create(PX.Strip.prototype);
      Object.assign(s, {props: {effect: 'field', tint: '#e0af68', cell: 4}, _el: canvas({}, 160, 16), _t0: 0,
        _beatSeen: 0, _burstSeen: 0, _beatAt: -1000, _burstAt: -1000, _doneAt: null, _sysStill: false});
      s._draw(1500);
      out.size = [s._el.width, s._el.height];
      const r = s._el.ops.filter(o => o[0] === 'r');
      out.ints = r.every(o => o.slice(1).every(Number.isInteger));
      out.cells = Array.from(new Set(r.slice(1).map(o => o[3] + 'x' + o[4])));
    """, dpr=3)
    assert out["size"] == [320, 32]
    assert out["ints"] is True
    assert out["cells"] == ["8x8"]


def _mix(a: tuple, b: tuple, k: float) -> tuple:
    return tuple(x + (y - x) * k for x, y in zip(a, b))


def _rgb(c: tuple) -> str:
    # JavaScript's Math.round (half up), as the artboard's _css uses.
    return "rgb(" + ",".join(str(int(v + 0.5)) for v in c) + ")"


@needs_node
def test_palette_and_bayer_follow_spec_section_6(tmp_path: Path) -> None:
    out = _node(tmp_path, r"""
      const s = Object.create(PX.Strip.prototype);
      out.pal = s._pal('#e0af68', 1);
      out.bad = s._pal('#f7768e', 1);
      out.bayer = s._bayer();
    """)
    field = (14, 14, 20)
    for tint, got in (((224, 175, 104), out["pal"]), ((247, 118, 142), out["bad"])):
        want = [None, _rgb(_mix(field, tint, 0.3)), _rgb(_mix(field, tint, 0.62)), _rgb(tint),
                _rgb(_mix(tint, (255, 255, 255), 0.45))]
        assert got == want
    assert len(out["bayer"]) == 64 and len(set(out["bayer"])) == 64
    assert all(0 < v < 1 for v in out["bayer"])


# --- the shared loop -------------------------------------------------------------------

def _strips(effect: str, n: int, extra: str = "") -> str:
    return (
        f"found = Array.from({{length: {n}}}, (_, i) => canvas({{class: 'px', 'data-px': '{effect}', "
        f"'data-tint': 'claude', 'data-px-key': 'k' + i{extra}}}, 160, 16));"
    )


@needs_node
def test_one_raf_loop_for_every_strip_with_the_20_fps_cap(tmp_path: Path) -> None:
    out = _node(tmp_path, _strips("fill", 10) + r"""
      PX.bind();
      out.pending = rafQueue.length;
      out.atBind = found.map(draws);
      frame(2000);
      const a = found.map(draws);
      frame(2030);
      const b = found.map(draws);
      frame(2047);
      const c = found.map(draws);
      out.pendingAfter = rafQueue.length;
      out.steps = [a[0], b[0], c[0]];
      PX.bind(); PX.bind();
      out.pendingRebind = rafQueue.length;
    """)
    assert out["pending"] == 1
    assert out["atBind"] == [1] * 10
    assert out["steps"] == [2, 2, 3]  # 30 ms after a frame: skipped; 47 ms: drawn
    assert out["pendingAfter"] == 1
    assert out["pendingRebind"] == 1


@needs_node
def test_loop_pauses_while_the_document_is_hidden(tmp_path: Path) -> None:
    out = _node(tmp_path, _strips("scan", 2) + r"""
      PX.bind();
      frame(2000);
      const before = draws(found[0]);
      document.hidden = true;
      frame(2100);
      out.hidden = [draws(found[0]) - before, rafQueue.length];
      document.hidden = false;
      (docListeners.visibilitychange || []).forEach(fn => fn());
      out.resumed = rafQueue.length;
      frame(2200);
      out.drawnAgain = draws(found[0]) - before;
    """)
    assert out["hidden"] == [0, 0]
    assert out["resumed"] == 1
    assert out["drawnAgain"] == 1


@needs_node
def test_off_screen_strips_are_not_drawn(tmp_path: Path) -> None:
    out = _node(tmp_path, _strips("field", 2) + r"""
      PX.bind();
      out.watched = observers[0].watched.size;
      observers[0].cb([{target: found[0], isIntersecting: false}]);
      const before = found.map(draws);
      frame(2000); frame(2100);
      out.delta = found.map((el, i) => draws(el) - before[i]);
      observers[0].cb([{target: found[1], isIntersecting: false}]);
      frame(2200);
      out.stopped = rafQueue.length;
      observers[0].cb([{target: found[0], isIntersecting: true}]);
      out.restarted = rafQueue.length;
    """)
    assert out["watched"] == 2
    assert out["delta"] == [0, 2]
    assert out["stopped"] == 0
    assert out["restarted"] == 1


@needs_node
def test_a_still_strip_draws_once_and_the_loop_stops(tmp_path: Path) -> None:
    out = _node(tmp_path, _strips("static", 3) + r"""
      PX.bind();
      for (let t = 2000; t <= 4000; t += 50) frame(t);
      out.draws = found.map(draws);
      out.pending = rafQueue.length;
    """)
    assert out["draws"] == [1, 1, 1]
    assert out["pending"] == 0


@needs_node
def test_a_strip_that_throws_does_not_stop_the_others(tmp_path: Path) -> None:
    out = _node(tmp_path, _strips("fill", 2) + r"""
      found[0].getContext = () => { throw new Error('boom'); };
      PX.bind();
      frame(2000); frame(2100);
      out.draws = [rects(found[0]), draws(found[1])];
    """)
    assert out["draws"] == [0, 3]


# --- registry and one-shots ----------------------------------------------------------

@needs_node
def test_rebuild_keeps_the_animation_and_beat_fires_only_on_a_heartbeat_drop(tmp_path: Path) -> None:
    out = _node(tmp_path, r"""
      const el = (hb) => canvas({class: 'px', 'data-px': 'fill', 'data-tint': 'codex', 'data-progress': '0.3',
        'data-hb': String(hb), 'data-px-key': 'job-ab12'}, 160, 16);
      found = [el(10)];
      PX.bind();
      const rec = PX.strips().get('job-ab12');
      const t0 = rec._t0;
      out.firstPaint = [rec.beats, rec._beatAt];
      clock = 5000; found = [el(12)]; PX.bind();
      out.older = [PX.strips().get('job-ab12') === rec, rec._t0 === t0, rec._el === found[0], rec.beats];
      clock = 6500; found = [el(12)]; PX.bind();
      out.same = rec.beats;
      clock = 8000; found = [el(2)]; PX.bind();
      out.dropped = [rec.beats, rec.props.beat, rec._beatAt > 0];
      out.observed = observers[0].watched.has(found[0]) && observers[0].watched.size === 1;
      found = []; PX.bind();
      out.gone = [PX.strips().size, observers[0].watched.size];
    """)
    assert out["firstPaint"] == [0, -1000]
    assert out["older"] == [True, True, True, 0]
    assert out["same"] == 0
    assert out["dropped"][:2] == [1, 1] and out["dropped"][2] is True
    assert out["observed"] is True
    assert out["gone"] == [0, 0]


@needs_node
def test_done_sweeps_once_then_the_strip_is_still(tmp_path: Path) -> None:
    out = _node(tmp_path, r"""
      const el = (done) => canvas(Object.assign({class: 'px', 'data-px': 'scan', 'data-tint': 'claude', 'data-px-key': 't1'},
        done ? {'data-done': 'true'} : {}), 160, 16);
      found = [el(false)]; PX.bind();
      found = [el(true)]; PX.bind();
      for (let t = 1050; t <= 1900; t += 50) frame(t);
      const settled = draws(found[0]);
      for (let t = 1950; t <= 3000; t += 50) frame(t);
      out.after = draws(found[0]) - settled;
      out.pending = rafQueue.length;
    """)
    assert out["after"] == 0
    assert out["pending"] == 0


SETTLE = r"""
  const make = (key, opts) => PX.strip(key, Object.assign({effect: 'off', tint: '#9ece6a', done: true, cell: 4, height: 8}, opts));
  const settling = make('new', {settle: true}), historic = make('old', {});
  found = [settling, historic]; PX.bind();
  for (let t = 1050; t <= 1900; t += 50) frame(t);
  out.sweep = draws(settling);
  out.historic = draws(historic);
  const settled = draws(settling);
  for (let t = 1950; t <= 3000; t += 50) frame(t);
  out.after = draws(settling) - settled;
  out.pending = rafQueue.length;
"""


@needs_node
def test_a_strip_born_settling_sweeps_once_and_a_historic_done_strip_does_not(tmp_path: Path) -> None:
    out = _node(tmp_path, SETTLE)
    assert out["sweep"] > 5
    assert out["historic"] == 1
    assert out["after"] == 0
    assert out["pending"] == 0
    still = _node(tmp_path, SETTLE, reduced=True)
    assert still["sweep"] == 1 and still["historic"] == 1


@needs_node
def test_reduced_motion_gives_one_still_frame_and_no_one_shots(tmp_path: Path) -> None:
    out = _node(tmp_path, r"""
      const el = (hb) => canvas({class: 'px', 'data-px': 'fill', 'data-tint': 'claude', 'data-progress': '0.5',
        'data-hb': String(hb), 'data-px-key': 'j'}, 160, 16);
      found = [el(10)]; PX.bind();
      const first = JSON.stringify(found[0].ops);
      const keep = found[0];
      found = [keep]; keep.attrs['data-hb'] = '1'; PX.bind();
      for (let t = 1050; t <= 3000; t += 50) frame(t);
      out.draws = draws(keep);
      out.pending = rafQueue.length;
      const still = new Component();
      Object.assign(still, {props: {effect: 'fill', tint: '#e0af68', progress: '0.5', still: true}, _el: canvas({}, 160, 16),
        _t0: 0, _beatSeen: 0, _burstSeen: 0, _beatAt: -1000, _burstAt: -1000, _doneAt: null, _sysStill: false});
      still._draw(1234);
      out.matchesArtboardStill = first === JSON.stringify(still._el.ops);
    """, reduced=True)
    assert out["draws"] == 1
    assert out["pending"] == 0
    assert out["matchesArtboardStill"] is True


@needs_node
def test_no_broken_state_ever_animates(tmp_path: Path) -> None:
    out = _node(tmp_path, r"""
      const states = """ + json.dumps(list(BROKEN)) + r""";
      found = states.map(s => PX.strip('k-' + s, {state: s, bot: 'claude'}));
      PX.bind();
      for (let t = 1050; t <= 3050; t += 50) frame(t);
      out.draws = found.map(draws);
      out.effects = found.map(el => el.getAttribute('data-px'));
      out.pending = rafQueue.length;
    """)
    assert out["draws"] == [1] * len(BROKEN)
    assert set(out["effects"]) == {"static"}
    assert out["pending"] == 0


@needs_node
def test_a_broken_strip_never_moves_whatever_one_shot_arrives(tmp_path: Path) -> None:
    out = _node(tmp_path, r"""
      const shots = [
        {'data-hb': '1'}, {'data-burst': '1'}, {'data-done': 'true'},
        {'data-hb': '1', 'data-burst': '2', 'data-done': 'true'},
      ];
      const before = {'data-hb': '20', 'data-burst': '0'};
      out.images = [];
      out.pending = [];
      ['fill', 'static'].forEach((from, i) => shots.forEach((shot, j) => {
        const key = 'k' + i + j;
        const base = (px) => ({class: 'px', 'data-px': px, 'data-tint': '#f7768e', 'data-progress': '0.5', 'data-px-key': key});
        found = [canvas(Object.assign(base(from), before), 160, 16)]; PX.bind();
        frame(clock + 50);
        found = [canvas(Object.assign(base('static'), before, shot), 160, 16)]; PX.bind();
        const el = found[0], seen = new Set([JSON.stringify(el.ops)]);
        for (let n = 0; n < 30; n++) { const start = el.ops.length; frame(clock + 50); seen.add(JSON.stringify(el.ops.slice(start))); }
        seen.delete('[]');
        out.images.push(seen.size);
        out.pending.push(rafQueue.length);
        found = []; PX.bind(); frame(clock + 50);
      }));
      const row = {job: 'ab12', job_state: 'running', action: 'build', recipient: 'codex',
        overdue: false, elapsed_seconds: 225, timeout_seconds: 3600, heartbeat_age_seconds: 20};
      found = [PX.jobStrip(row)]; PX.bind();
      clock += 1500; row.job_state = 'dead'; row.heartbeat_age_seconds = 1;
      found = [PX.jobStrip(row)]; PX.bind();
      const dead = found[0], images = new Set([JSON.stringify(dead.ops)]);
      for (let n = 0; n < 12; n++) { const start = dead.ops.length; frame(clock + 50); images.add(JSON.stringify(dead.ops.slice(start))); }
      images.delete('[]');
      out.deadJob = images.size;
    """)
    assert out["images"] == [1] * 8
    assert out["pending"] == [0] * 8
    assert out["deadJob"] == 1


@needs_node
@pytest.mark.parametrize("how", ["hidden", "offscreen"])
@pytest.mark.parametrize(("path", "canvas"), [("bind", "same"), ("bind", "replaced"), ("motion", "same")])
def test_no_paint_path_draws_a_hidden_or_off_screen_strip(tmp_path: Path, how: str, canvas: str, path: str) -> None:
    out = _node(tmp_path, r"""
      const el = (p) => canvas({class: 'px', 'data-px': 'fill', 'data-tint': 'claude', 'data-progress': p,
        'data-px-key': 'j'}, 160, 16);
      found = [el('0.5')]; PX.bind(); frame(2000);
      const how = '""" + how + r"""', replaced = """ + str(canvas == "replaced").lower() + r""";
      if (how === 'hidden') document.hidden = true;
      else observers[0].cb([{target: found[0], isIntersecting: false}]);
      frame(2100);
      const old = found[0], before = draws(old);
      clock = 2500;
      if (replaced) found = [el('0.6')]; else old.attrs['data-progress'] = '0.6';
      if ('""" + path + r"""' === 'bind') PX.bind();
      else mqListeners.forEach(fn => fn());
      for (let t = 2550; t <= 3000; t += 50) frame(t);
      out.extra = draws(found[0]) - (found[0] === old ? before : 0);
      if (how === 'hidden') { document.hidden = false; (docListeners.visibilitychange || []).forEach(fn => fn()); }
      else observers[0].cb([{target: found[0], isIntersecting: true}]);
      frame(3100);
      out.resumed = draws(found[0]) - (found[0] === old ? before : 0);
    """)
    assert out["extra"] == 0
    assert out["resumed"] >= 1


PROGRESS = sorted({k / 3600 for k in range(3601)} | {k / 16 for k in range(17)} | {k / 2000 for k in range(2001)}
                  | {k / 7 for k in range(8)} | {0.0625, 0.1875, 0.0005, 0.9995, 1 / 3, 2 / 3})


@needs_node
def test_server_and_script_round_progress_the_same(tmp_path: Path) -> None:
    out = _node(tmp_path, "out.html = " + json.dumps(PROGRESS)
                + ".map(p => serialize(PX.strip('k', {state: 'build', bot: 'codex', progress: p})));")
    server = [page.px_strip("k", state="build", bot="codex", progress=p) for p in PROGRESS]
    mismatched = [p for p, a, b in zip(PROGRESS, server, out["html"]) if a != b]
    assert mismatched == []
    assert 'data-progress="0.063"' in page.px_strip("k", state="build", progress=225 / 3600)


# --- the table ---------------------------------------------------------------------------

def test_effects_and_one_shots() -> None:
    assert set(page.PX_EFFECTS) == ANIMATED | STILL
    assert {name for name, moving in page.PX_EFFECTS.items() if moving} == ANIMATED
    assert page.PX_ONESHOTS == {"beat": 600, "burst": 450, "done": 600}


@pytest.mark.parametrize(
    ("state", "effect", "tint"),
    [
        ("plan", "field", "bot"),
        ("build", "fill", "bot"),
        ("review", "scan", "bot"),
        ("fix", "patch", "bot"),
        ("claimed", "spin3", "bot"),
        ("stale", "decay", "bot"),
        ("dead", "static", "#f7768e"),
        ("overdue", "static", "#f7768e"),
        ("stuck", "static", "#f7768e"),
        ("idle", "twinkle", "#9ece6a"),
        ("held", "off", "#9ece6a"),
    ],
)
def test_state_to_effect_table(state: str, effect: str, tint: str) -> None:
    assert page.PX_STATE[state]["effect"] == effect
    assert page.PX_STATE[state]["tint"] == tint


@pytest.mark.parametrize("state", BROKEN)
def test_broken_states_map_to_a_still_effect(state: str) -> None:
    effect = page.PX_STATE[state]["effect"]
    assert page.PX_EFFECTS[effect] is False
    assert effect == "static" and page.PX_STATE[state]["tint"] == "#f7768e"


def test_unknown_states_and_actions_never_animate() -> None:
    assert page.PX_EFFECTS[page.PX_STATE["unknown"]["effect"]] is False
    row = {"job_state": "running", "action": "no-such-action", "overdue": False}
    assert page.px_state(row) == "unknown"
    assert page.px_state({"job_state": "zombie", "action": "build"}) == "unknown"
    assert 'data-px="off"' in page.px_strip("x", state="no-such-state")
    assert 'data-px="off"' in page.px_strip("x", effect="wobble")


ROWS = [
    {"job_state": js, "action": action, "overdue": overdue}
    for js, action, overdue in itertools.product(
        ("running", "claimed", "stale", "dead", None, "zombie"),
        ("plan", "build", "review", "fix", "update-from-main", "clarify", "stuck", None),
        (False, True),
    )
]


def test_broken_rows_win_over_the_action() -> None:
    for row in ROWS:
        state = page.px_state(row)
        if row["job_state"] == "dead" or row["overdue"]:
            assert state in ("dead", "overdue")
            assert page.PX_EFFECTS[page.PX_STATE[state]["effect"]] is False


@needs_node
def test_script_resolver_matches_the_server_resolver(tmp_path: Path) -> None:
    out = _node(tmp_path, "out.states = " + json.dumps(ROWS) + ".map(PX.state);")
    assert out["states"] == [page.px_state(row) for row in ROWS]


def test_page_script_carries_the_server_table() -> None:
    script = page.page_script(None)
    assert page.px_script() in script
    (blob,) = re.findall(r"var TABLE = (\{.*?\});\n", page.px_script())
    assert json.loads(blob) == page.px_table()
    assert json.loads(blob)["states"] == page.PX_STATE


# --- markup ------------------------------------------------------------------------------

def test_strip_markup_and_attributes() -> None:
    row = {"job": "ab12cd34", "job_state": "running", "action": "build", "recipient": "claude",
           "overdue": False, "elapsed_seconds": 900, "timeout_seconds": 3600, "heartbeat_age_seconds": 7}
    assert page.px_job_strip(row) == (
        '<canvas class="px" data-px="fill" data-tint="claude" data-progress="0.250" data-hb="7" '
        'data-px-key="job-ab12cd34" aria-hidden="true" style="--px:var(--claude);--p:0.250;height:16px"></canvas>'
    )
    stale = dict(row, job_state="stale", heartbeat_age_seconds=1800)
    assert 'data-px="decay" data-tint="claude" data-progress="0.500"' in page.px_job_strip(stale)
    dead = dict(row, job_state="dead")
    assert 'data-px="static" data-tint="#f7768e"' in page.px_job_strip(dead, height=8)
    assert "height:8px" in page.px_job_strip(dead, height=8)
    claimed = dict(row, job_state="claimed")
    assert "data-hb" not in page.px_job_strip(claimed)


def test_strip_escapes_and_clamps_its_input() -> None:
    hostile = page.px_strip('"><script>x</script>', state="build", bot="evil", tint=None, progress=7)
    assert "<script>" not in hostile and "&quot;&gt;&lt;script&gt;" in hostile
    assert 'data-tint="#9ece6a"' in hostile
    assert 'data-progress="1.000"' in hostile
    assert 'data-tint="#9ece6a"' in page.px_strip("k", tint="red;background:url(x)")
    assert 'data-progress="0.000"' in page.px_strip("k", state="build", progress=-3)
    assert "height:96px" in page.px_strip("k", height=500)


def test_held_strip_frame_is_the_marching_dashed_border() -> None:
    assert page.px_frame("held") == "px-held"
    assert page.px_frame("build") == ""
    css = page.CSS
    rule = css[css.index(".px-held::after{") :]
    rule = rule[: rule.index("}") + 1]
    assert rule.count("repeating-linear-gradient(90deg,var(--warn) 0 8px,transparent 8px 16px)") == 2
    assert rule.count("repeating-linear-gradient(180deg,var(--warn) 0 8px,transparent 8px 16px)") == 2
    assert "animation:px-march 2s steps(4) infinite" in rule
    assert "@keyframes px-march{to{background-position:16px 0,-16px 100%,100% 16px,0 -16px}}" in css


@needs_node
def test_script_strip_is_the_server_strip(tmp_path: Path) -> None:
    rows = [
        {"job": "ab12", "job_state": "running", "action": "build", "recipient": "codex", "overdue": False,
         "elapsed_seconds": 1234, "timeout_seconds": 3600, "heartbeat_age_seconds": 12},
        {"job": "cd34", "job_state": "stale", "action": "review", "recipient": "grok", "overdue": False,
         "elapsed_seconds": 50, "timeout_seconds": 600, "heartbeat_age_seconds": 333},
        {"job": "ef56", "job_state": "claimed", "action": "plan", "recipient": "cursor", "overdue": False,
         "elapsed_seconds": 5, "timeout_seconds": 600, "heartbeat_age_seconds": 0},
        {"job": "'\"<x>", "job_state": "running", "action": "fix", "recipient": "nobody", "overdue": True,
         "elapsed_seconds": 900, "timeout_seconds": 600, "heartbeat_age_seconds": 700},
    ]
    calls = [
        ("idle", {"state": "idle"}),
        ("held", {"state": "held", "height": 24}),
        ("meter", {"effect": "meter", "tint": "#7dcfff", "progress": 0.5, "height": 8}),
        ("bad", {"state": "down", "tint": "javascript:1"}),
    ]
    out = _node(tmp_path, "out.jobs = " + json.dumps(rows) + ".map(r => serialize(PX.jobStrip(r)));"
                "out.calls = " + json.dumps(calls) + ".map(c => serialize(PX.strip(c[0], c[1])));")
    assert out["jobs"] == [page.px_job_strip(row) for row in rows]
    assert out["calls"] == [page.px_strip(key, **kwargs) for key, kwargs in calls]


# --- page-level guarantees -----------------------------------------------------------

def _pages(tmp_path: Path) -> dict[str, str]:
    return render_pages(collect_snapshot(*seeded(tmp_path), now=NOW))


def test_every_page_shows_only_the_shell_strips(tmp_path: Path) -> None:
    for name, html in _pages(tmp_path).items():
        keys = re.findall(r'<canvas class="px"[^>]*data-px-key="([^"]+)"', html)
        assert keys[:3] == ["caret", "band", "attention"], name
        if name == "index.html":
            # #140: the Lanes page adds its own strips after the shell's three.
            assert {"kpi-0", "queue", "meter-box"} <= set(keys[3:]), keys
            continue
        board = [key for key in keys[3:] if key.startswith(("phase-", "job-", "ticket-"))]
        rows = [key for key in keys[3:] if key.startswith("attention-")]
        assert keys[3:] == {"job.html": board, "activity.html": rows}.get(name, []), name
        assert len(rows) == (html.count('class="at-row"') if name == "activity.html" else 0), name
        assert 'class="px-held"' not in html, name


def test_strip_css_is_gradients_only() -> None:
    rules = re.findall(r"\.px[^{]*\{[^}]*\}", page.CSS)
    assert len(rules) >= 4
    for rule in rules:
        assert "url(" not in rule and "data:" not in rule
    assert "linear-gradient" in "".join(rules)


def test_reduced_motion_stills_the_held_border_and_the_script_reads_it() -> None:
    block = page.CSS[page.CSS.index("@media (prefers-reduced-motion: reduce)") :]
    assert ".px-held::after{animation:none}" in block
    assert "matchMedia('(prefers-reduced-motion: reduce)')" in page.px_script()


def test_one_animation_frame_call_site_with_the_fps_cap_and_hidden_check() -> None:
    script = page.page_script(None)
    assert script.count("requestAnimationFrame(") == 1
    assert "var FPS = 20;" in script
    assert "if (now - last < 1000 / FPS - 3)" in script
    assert "if (document.hidden) return;" in script
    assert "IntersectionObserver" in script
    assert "new Map()" in script


def test_csp_header_is_unchanged() -> None:
    source = (ROOT / "deploy" / "factory-dashboard-http.py").read_text(encoding="utf-8")
    assert (
        '"default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; "\n'
        '        "connect-src \'self\'; base-uri \'none\'; form-action \'none\'; frame-ancestors \'none\'"'
    ) in source
