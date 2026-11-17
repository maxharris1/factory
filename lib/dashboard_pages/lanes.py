"""index.html (Lanes, #140): the lanes (queue lane, then one row of slot tiles per device) with
live counts in their header, Just happened and Intake. Copied from Lanes.dc.html, SlotTile.dc.html
and the queue studies in Bits.dc.html."""

from __future__ import annotations

import json
import math

from lib.dashboard_pages.activity import _health_link
from lib.dashboard_pages.common import (
    _bot, checks_link, checks_named, _chip, duration, _e, github_links, _job_label, _panel,
    _short, _span, _ticket, _tile_label, _when, ticket_page_href,
)


CSS = """\
#hud #lanes{grid-column:1/-1}
#lanes h2{flex-wrap:wrap;gap:4px 8px}
#lanes h2 .count{font-variant-numeric:tabular-nums}
#lanes h2 .count b{font-weight:600;color:var(--text)}
#lanes h2 .count b.ok{color:var(--ok)}#lanes h2 .count b.warn{color:var(--warn)}
@media (max-width:640px){#lanes h2 .count{flex:1 1 100%}}
.lanes{display:flex;flex-direction:column;gap:10px}
.lane{display:flex;flex-wrap:wrap;gap:12px 16px;align-items:flex-start;padding:12px;background:var(--bg);box-shadow:var(--elev)}
.lane.disabled{color:var(--muted);box-shadow:none;outline:1px dashed var(--line-hi);outline-offset:-1px;
background:repeating-linear-gradient(135deg,var(--dim) 0 8px,var(--bg) 8px 16px)}
.lane-head{flex:0 0 224px;min-width:0;display:flex;flex-direction:column;gap:6px}
.lh1{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}
.lh2{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px}
.lh2 .sub{font-variant-numeric:tabular-nums}
.lane-head .name{min-width:0;overflow-wrap:anywhere;font:600 14px/1.2 var(--mono);letter-spacing:-.01em;color:var(--text)}
.lane.disabled .name{color:var(--muted)}
.lane-head .sub,.lane-body .sub{font:12px/1.3 var(--mono);color:var(--muted)}
.lane-head .harness{font-size:12px;line-height:1.3;color:var(--muted)}
.lane-meter{display:block;flex:none;max-width:100%}
.lane-meter .px{height:8px}
.lane-body{flex:1 1 400px;min-width:0;display:flex;flex-direction:column;gap:6px}
.lane-body .px{height:12px}
.qrail{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none}
.qrail li{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;font:12px/1.3 var(--mono)}
.qrail a{display:flex;align-items:baseline;gap:8px;color:inherit;text-decoration:none}
.qrail .ticket{font:600 13px/1.25 var(--mono);color:var(--text)}
.qrail a:hover .ticket{color:var(--brand)}
.qrail .what{color:var(--text-2)}.qrail .when{color:var(--muted)}
.qrail .belt{display:block;flex:1 1 120px;min-width:0}
.tiles{flex:1 1 400px;min-width:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px}
.tile{display:block;min-width:0;min-height:124px;background:var(--panel-hi);border:1px solid transparent;color:var(--text);
text-decoration:none;font:12px/1.3 var(--mono)}
a.tile:hover,a.tile:focus-visible{border-color:var(--brand)}
.tile.bad{border-color:var(--bad)}
.tile.claude{--bt:var(--claude)}.tile.codex{--bt:var(--codex)}.tile.cursor{--bt:var(--cursor)}.tile.grok{--bt:var(--grok)}
.tile-body{display:flex;flex-direction:column;gap:6px;padding:8px 10px 10px;container-type:inline-size}
.tile-l1{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 6px;min-width:0}
.tile-l1 .ticket{flex:0 1 auto;min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:600 13px/1.25 var(--mono);color:var(--text)}
.tile-l1 .chip{flex:none}
.tile-l2{display:flex;align-items:center;gap:7px;min-height:18px}
.glyph{display:grid;grid-template-columns:5px 5px;grid-template-rows:5px 5px;gap:1px}
.glyph i{background:var(--line-hi)}.glyph i.on{background:var(--bt,var(--ok))}
.verb{display:flex;white-space:pre;font:500 14px/1.25 var(--mono);color:var(--muted)}
.verb.live{color:var(--text)}.verb.warn{color:var(--warn)}.verb.bad{color:var(--bad)}
.verb .h{color:#ffffff}.verb .n{color:var(--bt,var(--ok))}
.tile-l3{display:flex;flex-wrap:wrap;align-items:center;gap:2px 8px;color:var(--muted)}
.tile-l3 .action{min-width:0;overflow-wrap:anywhere;color:var(--text-2)}
.round{flex:none;display:flex;align-items:center;gap:6px;white-space:nowrap}
.pips{display:flex;gap:2px}
.pips i{width:6px;height:6px;background:var(--line-hi)}.pips i.done{background:var(--text)}.pips i.cur{background:var(--accent)}
.tile-l3 .t{flex:none;margin-left:auto;white-space:nowrap}.tile-l3 .t b{font-weight:600;color:var(--text)}
@container (max-width:175px){.tile-l3{gap:6px}.tile-l3 .action,.tile-l3 .t{font-size:11px}}
.tile-l4{display:flex;align-items:center;gap:8px}
.hbar{flex:1 1 0;height:3px;background:var(--line-hi)}
.hbar i{display:block;height:3px;background:var(--ok)}
.hbar.warn i{background:var(--warn)}.hbar.bad i{background:var(--bad)}
.tile-l4 .hbt{white-space:nowrap;color:var(--muted)}
.tile-l4 .hbt.warn{color:var(--warn)}.tile-l4 .hbt.bad{color:var(--bad)}
.tile.ci{min-height:0}
.tile.ci .tile-l1 .gh{flex:none}
.tile.ci .tile-l4{flex-wrap:wrap;gap:2px 8px;color:var(--text-2)}
.tile.ci .tile-l4 .what{min-width:0;overflow-wrap:anywhere}
.tile.empty{display:flex;flex-direction:column;background:transparent}
.tile.empty .lbl{flex:1 0 auto;display:flex;align-items:center;justify-content:center;
font:500 11px/1 var(--mono);letter-spacing:.12em;color:var(--warn)}
.rail{min-width:0;align-self:center;display:flex;align-items:center;gap:8px;height:24px}
.rail .sq{display:flex;gap:3px}
.rail i{box-sizing:border-box;width:8px;height:8px;border:1px dashed #414868}
.rail .lbl{font:12px/1.3 var(--mono);color:#8b93b8;white-space:nowrap}
.rail.off i{border:0;background:#24283b}
@media (max-width:640px){.lane{flex-direction:column;align-items:stretch;padding:10px;gap:10px}
.lane-head,.tiles,.lane-body{flex:none}
.lane-head{flex-direction:row;flex-wrap:wrap;align-items:center;gap:6px 10px}
.lh1,.lh2{display:contents}
.lh2 .sub{margin-left:auto;white-space:nowrap}
.lh2 .harness{flex-basis:100%}
.tiles{grid-template-columns:1fr}
.rail{align-self:auto}}
.pair{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}
.pair>.panel{min-width:0}
#just-happened{flex:3 1 560px}#intake{flex:2 1 380px}
.wake{padding:0 4px;font-size:10px;color:var(--grok)}
.intake{display:flex;flex-direction:column;gap:10px}
.irow{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
.irow p{flex:1 0 100%;margin:0;font-size:12px}
.ilabel{min-width:108px;font:500 12px/1.3 var(--mono);color:var(--muted)}
.itext{font-size:12px;color:var(--text-2)}
.imeter{display:block;width:176px}.imeter .px{height:8px}
#intake .intake+.feed{margin-top:10px}
#intake .what{min-width:0;overflow-wrap:anywhere}#intake .what.bad{color:var(--bad)}
.igroup{margin:14px 0 2px;font:11px/1.3 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
#intake details{margin-top:4px}
.fed-more{margin:4px 0 0;padding:0;list-style:none;font:12px/1.5 var(--mono);color:var(--text-2)}
.fed-more li{display:flex;flex-wrap:wrap;gap:8px;padding:4px 6px;border-bottom:1px solid var(--line)}
.fed-more li:last-child{border-bottom:0}
.fed-more .ticket{color:var(--text)}.fed-more .when{margin-left:auto;color:var(--muted)}
"""


# --- tables shared with the script (LANES) -------------------------------------------------

# A job tile per PX state (shell.PX_STATE carries the strip). `verbs` rotate while the job
# works; `text` is the fixed line 2 otherwise; `tone` colours it; `chip` is the state chip
# (none on healthy work); `hb` is the heartbeat bar's rule; `bad` gives the red border.
TILE_STATE: dict[str, dict] = {
    "plan": {"verbs": ["Pondering", "Sketching", "Scheming"], "tone": "live", "hb": "live"},
    "build": {"verbs": ["Assembling", "Stacking", "Forging"], "tone": "live", "hb": "live"},
    "review": {"verbs": ["Scrutinizing", "Combing", "Squinting"], "tone": "live", "hb": "live"},
    "fix": {"verbs": ["Patching", "Mending", "Tinkering"], "tone": "live", "hb": "live"},
    "clarify": {"verbs": ["Clarifying", "Untangling", "Answering"], "tone": "live", "hb": "live"},
    "update-from-main": {"verbs": ["Merging", "Syncing", "Reconciling"], "tone": "live", "hb": "live"},
    "claimed": {"text": "waiting to start", "tone": "", "chip": ["claimed", "warn"], "hb": "none"},
    "stale": {"text": "no heartbeat", "tone": "warn", "chip": ["stale", "warn"], "hb": "warn"},
    "dead": {"text": "runner dead", "tone": "bad", "chip": ["dead", "bad"], "hb": "bad", "bad": True},
    "overdue": {"text": "overdue", "tone": "bad", "hb": "bad", "bad": True},
    "stuck": {"text": "ticket stuck", "tone": "bad", "chip": ["stuck", "bad"], "hb": "bad", "bad": True},
    "unknown": {"text": None, "tone": "", "hb": "live"},
}

GLYPH_ORDER = [2, 0, 1, 3]

STEP_AFTER_CI = {"ci-pending": "review", "approved-ci-pending": "merge"}

# Queue speed by wait age, through the Bits rail's anchors (0:40 → 1, 6:10 → 2.5, 22:05 → 5).
QUEUE_SPEED = [[40, 1.0], [370, 2.5], [1325, 5.0]]

QUEUE_TINT = "#8b93b8"
BELT_TINT = "#9ece6a"
CI_TINT = "#ff9e64"
_BAD = "#f7768e"

INTAKE_GROUP = {
    "error": "trouble",
    "refused-intake": "trouble",
    "waiting-blocked": "waiting",
    "waiting-capacity": "waiting",
}
FED_TODAY_SECONDS = 86400


def lanes_table() -> dict:
    return {"tiles": TILE_STATE, "glyph": GLYPH_ORDER, "speed": QUEUE_SPEED,
            "queue_tint": QUEUE_TINT, "belt_tint": BELT_TINT, "step_after_ci": STEP_AFTER_CI, "ci_tint": CI_TINT,
            "intake_group": INTAKE_GROUP, "fed_today": FED_TODAY_SECONDS}


def queue_speed(age: object) -> float:
    """Conveyor speed for a wait of `age` seconds: linear between the anchors, flat
    before the first, and on past the last at the last segment's slope (the engine caps 6)."""
    a = float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else 0.0
    pts = QUEUE_SPEED
    if a <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if a <= x1:
            return round(y0 + (y1 - y0) * (a - x0) / (x1 - x0), 3)
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    return round(min(6.0, y1 + (y1 - y0) * (a - x1) / (x1 - x0)), 3)


def verb_at(state: str, secs: int, seed: int) -> str:
    verbs = TILE_STATE[state]["verbs"]
    return verbs[math.floor((secs + seed * 1.7) / 4) % len(verbs)] + "…"


SCRIPT = r"""
  // --- main page (#140): lanes and slot tiles, Just happened, Intake -------

  var LANES = __LANES__;
  var LANES_MQ = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function lanesStill() { return !!(LANES_MQ && LANES_MQ.matches); }

  function queueSpeed(age) {
    var pts = LANES.speed;
    var a = Number(age) || 0;
    if (a <= pts[0][0]) return pts[0][1];
    for (var i = 1; i < pts.length; i++) {
      if (a <= pts[i][0]) return Math.round((pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * (a - pts[i - 1][0]) / (pts[i][0] - pts[i - 1][0])) * 1000) / 1000;
    }
    var p0 = pts[pts.length - 2], p1 = pts[pts.length - 1];
    return Math.round(Math.min(6, p1[1] + (p1[1] - p0[1]) * (a - p1[0]) / (p1[0] - p0[0])) * 1000) / 1000;
  }

  function probeDown(d, name) {
    return agedHealth(d).rows.some(function (c) {
      return c.component === 'device-probe' && c.device === name && c.state === 'down';
    });
  }

  function deviceChip(dev, down) {
    if (!dev.enabled) return chip('DISABLED', 'muted');
    if (dev.placeholder) return chip('PLACEHOLDER', 'warn');
    if (!dev.listed) return chip('UNLISTED', 'warn');
    if (down) return chip('PROBE DOWN', 'bad');
    return null;
  }

  function verbAt(name, secs, seed) {
    var verbs = LANES.tiles[name].verbs;
    return verbs[Math.floor((secs + seed * 1.7) / 4) % verbs.length] + '…';
  }

  function letters(node, text) {
    node.replaceChildren();
    node.setAttribute('aria-label', text);
    for (var i = 0; i < text.length; i++) add(node, el('span', null, text.charAt(i)));
  }

  var tileAnims = [];
  var tileTick = 0;
  function tileFrame(a) {
    var secs = Math.floor(tileTick / 8);
    var lit = LANES.glyph[Math.floor(tileTick / 2) % 4];
    a.cells.forEach(function (c, q) { c.className = q === lit ? 'on' : ''; });
    var text = verbAt(a.state, secs, a.seed);
    if (text !== a.verb.getAttribute('aria-label')) letters(a.verb, text);
    var pos = (tileTick % (text.length + 10)) - 2;
    Array.prototype.forEach.call(a.verb.children, function (s, i) {
      var dist = Math.abs(i - pos);
      s.className = dist === 0 ? 'h' : dist === 1 ? 'n' : '';
    });
  }
  setInterval(function () {
    if (lanesStill() || !tileAnims.length) return;
    tileTick += 1;
    tileAnims.forEach(function (a) { if (a.verb.isConnected !== false) tileFrame(a); });
  }, 125);

  function slotTile(r, seed) {
    var name = PX.state(r);
    var spec = LANES.tiles[name] || LANES.tiles.unknown;
    var who = BOTS[r.recipient] ? r.recipient : 'other';
    var tile = link('tile ' + who + (spec.bad ? ' bad' : ''), ticketPageHref(r));
    tile.title = ticketIn(r.repo, r) + ' · ' + r.action + (r.round ? ' · round ' + r.round : '') + ' (job ' + r.job + ')';
    add(tile, PX.jobStrip(r, 16));
    var bodyEl = add(tile, el('div', 'tile-body'));
    var l1 = add(bodyEl, el('div', 'tile-l1'));
    add(l1, bot(r.recipient));
    add(l1, el('span', 'ticket', tileLabel(r)));
    var badge = name === 'overdue' ? (LANES.tiles[r.job_state] || {}).chip : spec.chip;
    if (badge) add(l1, chip(badge[0], badge[1]));
    var l2 = add(bodyEl, el('div', 'tile-l2'));
    var verb;
    if (spec.verbs) {
      var glyph = add(l2, el('span', 'glyph'));
      glyph.setAttribute('aria-hidden', 'true');
      var cells = [];
      for (var q = 0; q < 4; q++) cells.push(add(glyph, el('i', q === LANES.glyph[0] ? 'on' : null)));
      verb = add(l2, el('span', 'verb ' + spec.tone));
      verb.setAttribute('role', 'img');
      letters(verb, verbAt(name, 0, seed));
      tileAnims.push({state: name, seed: seed, cells: cells, verb: verb});
    } else {
      verb = add(l2, el('span', 'verb' + (spec.tone ? ' ' + spec.tone : '')));
      verb.setAttribute('role', 'img');
      letters(verb, spec.text === null ? r.action : spec.text);
    }
    var l3 = add(bodyEl, el('div', 'tile-l3'));
    if (spec !== LANES.tiles.unknown) add(l3, el('span', 'action', r.action));
    var m = /^(\d+)\s*\/\s*(\d+)$/.exec(String(r.round === null || r.round === undefined ? '' : r.round));
    if (m) {
      var rnd = add(l3, el('span', 'round'));
      rnd.title = 'round ' + r.round;
      var pips = add(rnd, el('span', 'pips'));
      pips.setAttribute('aria-hidden', 'true');
      var cur = Number(m[1]), cap = Math.min(6, Number(m[2]));
      for (var k = 1; k <= cap; k++) add(pips, el('i', k < cur ? 'done' : k === cur ? 'cur' : null));
      add(rnd, el('span', null, String(r.round)));
    }
    var t = add(l3, el('span', 't', 'T+ '));
    ticking(add(t, el('b')), r.elapsed_seconds);
    var l4 = add(bodyEl, el('div', 'tile-l4'));
    var bar = add(l4, el('div', 'hbar' + (spec.hb === 'warn' || spec.hb === 'bad' ? ' ' + spec.hb : '')));
    bar.setAttribute('aria-hidden', 'true');
    var fill = add(bar, el('i'));
    var hbt = add(l4, el('span', 'hbt' + (spec.hb === 'warn' || spec.hb === 'bad' ? ' ' + spec.hb : '')));
    if (spec.hb === 'none') {
      fill.setAttribute('style', 'width:0%');
      hbt.textContent = 'hb —';
    } else if (spec.hb === 'live') {
      var base = r.heartbeat_age_seconds;
      var item = {run: function (dt) {
        var age = Math.max(0, Math.floor((Number(base) || 0) + dt));
        fill.setAttribute('style', 'width:' + Math.max(8, 100 - age * 6) + '%');
        hbt.textContent = 'hb ' + dur(age);
      }};
      tickers.push(item);
      item.run(since());
    } else {
      fill.setAttribute('style', 'width:100%');
      ticking(hbt, r.heartbeat_age_seconds, 'hb ');
    }
    return mark('lanes', r.job, tile);
  }

  function heldTile(key, seed) {
    var tile = el('div', 'tile empty px-held');
    add(tile, PX.strip(key, {state: 'held', seed: seed, height: 16}));
    add(tile, el('div', 'lbl', 'held'));
    return tile;
  }

  function idleRail(count, off) {
    var rail = el('div', 'rail' + (off ? ' off' : ''));
    var sq = add(rail, el('span', 'sq'));
    sq.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < count; i++) add(sq, el('i'));
    add(rail, el('span', 'lbl', count + (off ? ' off' : ' idle')));
    return rail;
  }

  function queueLane(d) {
    var rows = d.queued || [];
    var lane = el('div', 'lane');
    lane.setAttribute('id', 'queue');
    lane.setAttribute('data-device', 'queue');
    var head = add(lane, el('div', 'lane-head'));
    add(head, el('div', 'name', 'queue'));
    add(head, el('div', 'sub', rows.length + ' waiting'));
    var laneBody = add(lane, el('div', 'lane-body'));
    var oldest = rows.reduce(function (m, r) { return Math.max(m, Number(r.age_seconds) || 0); }, 0);
    add(laneBody, PX.strip('queue', {effect: 'conveyor', tint: LANES.queue_tint, seed: 20, height: 12,
                                     speed: queueSpeed(oldest), still: !rows.length}));
    if (!rows.length) { add(laneBody, el('div', 'sub', 'queue empty')); return lane; }
    var ul = add(laneBody, el('ul', 'qrail'));
    rows.forEach(function (r, i) {
      var li = add(ul, el('li'));
      var a = add(li, link(null, ticketPageHref(r)));
      add(a, bot(r.recipient));
      add(a, el('span', 'ticket', jobLabel(r)));
      tierChip(a, r.tier);
      add(li, el('span', 'what', r.action));
      ticking(add(li, el('span', 'when')), r.age_seconds, 'waiting ');
      if (r.no_waiter) add(li, link('chip bad', healthLink(r.no_waiter_link))).textContent = 'no waiter for ' + r.no_waiter;
      var belt = add(li, el('span', 'belt'));
      belt.setAttribute('aria-hidden', 'true');
      if (r.no_waiter) add(belt, PX.strip('queue-' + r.job, {effect: 'static', tint: '#f7768e', seed: 41 + i, height: 12}));
      else add(belt, PX.strip('queue-' + r.job, {effect: 'conveyor', tint: LANES.belt_tint, seed: 41 + i, height: 12,
                                                 speed: queueSpeed(r.age_seconds)}));
      mark('queue', r.job, li);
    });
    return lane;
  }

  function ciCard(r) {
    var ci = r.ci_wait;
    var step = LANES.step_after_ci.hasOwnProperty(ci.reason) ? LANES.step_after_ci[ci.reason] : null;
    var card = el('div', 'tile ci');
    add(card, PX.strip('ci-' + r.repo + '#' + r.ticket_id, {effect: 'static', tint: LANES.ci_tint, height: 8}));
    var bodyEl = add(card, el('div', 'tile-body'));
    var l1 = add(bodyEl, el('div', 'tile-l1'));
    add(l1, el('span', 'ticket', ticketOf(r)));
    addGh(l1, r);
    add(add(bodyEl, el('div', 'tile-l2')), el('span', 'verb warn', 'waiting on CI'));
    var l3 = add(bodyEl, el('div', 'tile-l3'));
    if (step) add(l3, el('span', 'action', 'then ' + step));
    if (step === 'review' && ci.round != null) add(l3, el('span', 'round', 'round ' + ci.round));
    ticking(add(l3, el('span', 't')), ci.waiting_seconds, 'waiting ');
    var l4 = add(bodyEl, el('div', 'tile-l4'));
    add(l4, el('span', 'what', checksNamed(ci)));
    var href = checksHref(r);
    if (href) add(l4, link(null, href)).textContent = 'checks';
    return card;
  }

  function ciLane(rows) {
    var lane = el('div', 'lane');
    lane.setAttribute('data-device', 'ci-wait');
    var head = add(lane, el('div', 'lane-head'));
    add(head, el('div', 'name', 'waiting on CI'));
    add(head, el('div', 'sub', rows.length + (rows.length === 1 ? ' ticket' : ' tickets')));
    var grid = add(lane, el('div', 'tiles'));
    rows.forEach(function (r) { add(grid, ciCard(r)); });
    return lane;
  }

  function renderLanes(d) {
    var running = d.running || [];
    var lanes = laneDevices(d);
    var root = panel('lanes', 'Lanes and slots');
    if (!root) return;
    var count = add(root.children[0], el('span', 'count'));
    var heldTiles = 0, idleTiles = 0;
    tileAnims = [];
    var box = add(root, el('div', 'lanes'));
    add(box, queueLane(d));
    lanes.devices.forEach(function (dev, index) {
      var row = add(box, el('div', 'lane' + (dev.enabled ? '' : ' disabled')));
      row.setAttribute('data-device', dev.name);
      var head = add(row, el('div', 'lane-head'));
      var lh1 = add(head, el('div', 'lh1'));
      add(lh1, el('span', 'name', dev.name));
      var down = probeDown(d, dev.name);
      var devChip = deviceChip(dev, down);
      if (devChip) add(lh1, devChip);
      var slots = dev.slot_max === null || dev.slot_max === undefined ? Math.max(1, dev.used) : Math.max(1, dev.slot_max);
      var meter = add(lh1, el('div', 'lane-meter'));
      meter.setAttribute('aria-hidden', 'true');
      meter.setAttribute('style', 'width:' + (12 * slots) + 'px');
      add(meter, PX.strip('meter-' + dev.name, {effect: 'meter', tint: down ? '#f7768e' : '#9ece6a', used: dev.used,
                                                  slots: slots, seed: 21 + index, height: 8,
                                                  still: down || !dev.enabled}));
      var lh2 = add(head, el('div', 'lh2'));
      add(lh2, el('span', 'sub', dev.used + ' / ' + (dev.slot_max === null || dev.slot_max === undefined ? '?' : dev.slot_max) + ' slots'));
      if (dev.harnesses.length) add(lh2, el('span', 'harness', dev.harnesses.join(' · ')));
      var grid = add(row, el('div', 'tiles'));
      var seed = 0;
      dev.jobs.forEach(function (r) { seed += 1; add(grid, slotTile(r, seed)); });
      var held = dev.used - dev.jobs.length;
      for (var h = 0; h < held; h++) { seed += 1; add(grid, heldTile('slot-' + dev.name + '-' + seed, seed)); }
      var free = Math.max(0, (dev.slot_max || 0) - dev.used);
      if (!dev.used && !free) free = 1;
      heldTiles += held;
      if (dev.enabled) idleTiles += free;
      if (free) add(grid, idleRail(free, !dev.enabled));
    });
    if (lanes.wakes.length) {
      var row = add(box, el('div', 'lane'));
      row.setAttribute('data-device', 'grok-wakes');
      var head = add(row, el('div', 'lane-head'));
      add(head, el('div', 'name', 'grok wakes'));
      add(head, chip('BOT-TO-BOT', 'accent'));
      var grid = add(row, el('div', 'tiles'));
      lanes.wakes.forEach(function (r, i) { add(grid, slotTile(r, i + 1)); });
    }
    var waiting = (d.tickets || []).filter(function (t) { return t.ci_wait; });
    if (waiting.length) add(box, ciLane(waiting));
    [[running.length, 'ok', ' running · '], [heldTiles, heldTiles ? 'warn' : null, ' held · '],
     [idleTiles, null, ' idle · '], [(d.queued || []).length, null, ' queued']].forEach(function (c) {
      add(count, el('b', c[1], c[0]));
      add(count, document.createTextNode(c[2]));
    });
  }

  function renderJustHappened(d) {
    var rows = ((d.handoff_savings || {}).recent || []).slice(0, 6);
    var root = panel('just-happened', 'Just happened', rows.length + ' newest handoffs');
    if (!root) return;
    if (!rows.length) { add(root, el('p', 'empty', 'no finished jobs yet')); }
    else {
      var ul = add(root, el('ul', 'feed'));
      rows.forEach(function (r) {
        var li = add(ul, el('li'));
        var wake = r.kind === 'bot_to_bot';
        add(li, bot(r.recipient));
        add(li, el('span', 'ticket', ticketOf(r)));
        addGh(li, r);
        add(li, el('span', 'what', wake ? 'done' : r.action));
        if (wake) add(li, chip('grok wake', 'wake'));
        if (r.state === 'failed') add(li, chip('failed', 'bad'));
        ticking(add(li, el('span', 'when')), r.age_seconds, '', ' ago');
        mark('just-happened', r.job + ':' + r.state, li);
      });
    }
    var more = add(root, el('p', 'more'));
    add(more, link(null, 'activity.html')).textContent = 'all activity';
  }

  var FEEDER_STATE = {'eligible': 'eligible', 'waiting-capacity': 'waiting for capacity',
    'waiting-blocked': 'waiting on a blocker', 'refused-intake': 'refused at intake', 'feeding': 'feeding',
    'error': 'feed error, retrying', 'fed': 'fed', 'done': 'fed, job queued', 'closed': 'PR closed, not fed'};

  function tierChip(parent, tier) {
    if (tier !== null && tier !== undefined) add(parent, chip('tier ' + tier, 'accent'));
  }

  function intakeGroups(items) {
    var groups = {trouble: [], waiting: [], fed: []};
    items.forEach(function (x) {
      var age = x.fed_age_seconds;
      var group = LANES.intake_group[x.state] || (age !== null && age !== undefined && age < LANES.fed_today ? 'fed' : null);
      if (group) groups[group].push(x);
    });
    return groups;
  }

  function intakeRow(ul, x, cls) {
    var li = add(ul, el('li'));
    add(li, el('span', 'ticket', short(x.repo) + '#' + x.issue));
    tierChip(li, x.tier);
    add(li, el('span', cls || 'what', x.reason || FEEDER_STATE[x.state] || x.state));
    return li;
  }

  function renderIntake(d) {
    var w = d.poller && d.poller.linear_webhook;
    var f = d.feeder;
    var g = f ? intakeGroups(f.items || []) : null;
    var root = panel('intake', 'Intake', g ? g.waiting.length + ' waiting · ' + g.fed.length + ' fed today' : null);
    if (!root) return;
    var box = add(root, el('div', 'intake'));
    if (w) {
      var received;
      if (w.last_received_age_seconds !== null && w.last_received_age_seconds !== undefined) {
        received = dur(w.last_received_age_seconds) + ' ago' + (w.last_event ? ' (' + w.last_event + ')' : '');
      } else if (w.listener_age_seconds === null || w.listener_age_seconds === undefined) {
        received = 'never (listener has not started)';
      } else {
        received = 'never';
      }
      var hook = add(box, el('div', 'irow'));
      hook.setAttribute('id', 'linear-webhook');
      add(hook, el('span', 'ilabel', 'Linear' + ' webhook'));
      add(hook, el('span', 'itext', 'last received ' + received + ' · dirty queue ' + w.dirty));
      if (w.warning !== null && w.warning !== undefined) add(hook, el('p', 'bad', 'warning: ' + w.warning));
      if (w.note !== null && w.note !== undefined) add(hook, el('p', 'muted', w.note));
    }
    if (f) {
      var feed = add(box, el('div', 'irow'));
      feed.setAttribute('id', 'feeder');
      add(feed, el('span', 'ilabel', 'Feeder'));
      add(feed, el('span', 'itext', 'eligible ' + f.eligible + ' · fed ' + f.fed +
        ' · waiting for capacity ' + f.waiting_capacity +
        ' · waiting on blockers or files ' + (f.waiting_blocked || 0)));
      var meter = add(box, el('div', 'imeter'));
      meter.setAttribute('aria-hidden', 'true');
      add(meter, PX.strip('intake-meter', {effect: 'meter', tint: '#9ece6a', used: f.fed,
                                           slots: Math.max(1, f.eligible), seed: 24, height: 8, still: true}));
    }
    if (!w && !f) { add(box, el('p', 'empty', 'no listener or feeder has run')); return; }
    if (!g) return;
    if (g.trouble.length) {
      var trouble = add(root, el('ul', 'feed'));
      g.trouble.forEach(function (x) { intakeRow(trouble, x, 'what bad'); });
    }
    if (g.waiting.length) {
      add(root, el('div', 'igroup', g.waiting.length + ' waiting'));
      var waiting = add(root, el('ul', 'feed'));
      g.waiting.slice(0, 3).forEach(function (x) { intakeRow(waiting, x); });
      if (g.waiting.length > 3) {
        var rest = add(details(root, 'intake-waiting-more', (g.waiting.length - 3) + ' more'), el('ul', 'feed'));
        g.waiting.slice(3).forEach(function (x) { intakeRow(rest, x); });
      }
    }
    if (g.fed.length) {
      var fed = add(details(root, 'intake-fed', g.fed.length + ' fed today'), el('ul', 'fed-more'));
      g.fed.forEach(function (x) {
        var li = intakeRow(fed, x);
        if (x.pr) add(li, el('span', null, '→ PR #' + x.pr));
        ticking(add(li, el('span', 'when')), x.fed_age_seconds, 'fed ', ' ago');
      });
    }
  }
"""


# --- lanes --------------------------------------------------------------------------------


def lane_devices(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """The lanes the script draws: (devices with their running jobs, grok
    wakes). Mirrors laneDevices in the page script."""
    slots = snapshot["slots"]
    host = slots["box"]["name"]
    devices = [
        {
            "name": row["name"],
            "slot_max": row["slot_max"],
            "held": row.get("held") or 0,
            "enabled": row["enabled"],
            "placeholder": row["placeholder"],
            "harnesses": row["harnesses"],
            "listed": True,
            "jobs": [],
        }
        for row in slots["devices"]
    ]
    by_name = {device["name"]: device for device in devices}
    wakes: list[dict] = []
    for row in snapshot["running"]:
        if row["device"] is None:
            wakes.append(row)
            continue
        if row["device"] not in by_name:
            device = {
                "name": row["device"],
                "slot_max": slots["box"]["slot_max"] if row["device"] == host else None,
                "held": 0,
                "enabled": True,
                "placeholder": False,
                "harnesses": [],
                "listed": row["device"] == host and not devices,
                "jobs": [],
            }
            devices.append(device)
            by_name[row["device"]] = device
        by_name[row["device"]]["jobs"].append(row)
    if not devices:
        devices.append(
            {"name": host, "slot_max": slots["box"]["slot_max"], "held": 0, "enabled": True,
             "placeholder": False, "harnesses": [], "listed": True, "jobs": []}
        )
    for device in devices:
        # #63 FU-1: a slot held in device_slots is in use even when its job
        # is not (or no longer) running, so a lane counts max(held, running).
        device["used"] = max(device["held"], len(device["jobs"]))
    return devices, wakes


def probe_down(snapshot: dict, name: str) -> bool:
    health = snapshot.get("health") or {}
    return any(
        c.get("component") == "device-probe" and c.get("device") == name and c.get("state") == "down"
        for c in health.get("components") or []
    )


def _device_chip(device: dict, down: bool) -> str:
    if not device["enabled"]:
        return _chip("DISABLED", "muted")
    if device["placeholder"]:
        return _chip("PLACEHOLDER", "warn")
    if not device["listed"]:
        return _chip("UNLISTED", "warn")
    if down:
        return _chip("PROBE DOWN", "bad")
    return ""


def _letters(text: str) -> str:
    return "".join(f"<span>{_e(ch)}</span>" for ch in text)


def _tone(cls: str) -> str:
    return f" {cls}" if cls else ""


def _slot(row: dict, seed: int = 1) -> str:
    from lib.dashboard_pages.shell import px_job_strip, px_state

    name = px_state(row)
    spec = TILE_STATE.get(name, TILE_STATE["unknown"])
    who = row["recipient"] if row["recipient"] in ("claude", "codex", "cursor", "grok") else "other"
    title = (
        f"{_ticket(row, full=True)} · {row['action']}"
        + (f" · round {row['round']}" if row.get("round") else "")
        + f" (job {row['job']})"
    )
    badge = TILE_STATE.get(str(row.get("job_state")), {}).get("chip") if name == "overdue" else spec.get("chip")
    line1 = _bot(row["recipient"]) + _span("ticket", _tile_label(row)) + (_chip(badge[0], badge[1]) if badge else "")
    if spec.get("verbs"):
        text = verb_at(name, 0, seed)
        cells = "".join(f'<i class="on"></i>' if q == GLYPH_ORDER[0] else "<i></i>" for q in range(4))
        line2 = (
            f'<span class="glyph" aria-hidden="true">{cells}</span>'
            f'<span class="verb{_tone(spec["tone"])}" role="img" aria-label="{_e(text)}">{_letters(text)}</span>'
        )
    else:
        text = row["action"] if spec["text"] is None else spec["text"]
        line2 = f'<span class="verb{_tone(spec["tone"])}" role="img" aria-label="{_e(text)}">{_letters(str(text))}</span>'
    rnd = str(row.get("round") or "")
    pips = ""
    parts = rnd.split("/")
    if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
        cur, cap = int(parts[0]), min(6, int(parts[1]))
        cells = "".join(
            f'<i class="{"done" if k < cur else "cur"}"></i>' if k <= cur else "<i></i>"
            for k in range(1, cap + 1)
        )
        pips = (
            f'<span class="round" title="round {_e(rnd)}"><span class="pips" aria-hidden="true">{cells}</span>'
            f"{_span(None, rnd)}</span>"
        )
    line3 = (
        ("" if name == "unknown" else _span("action", row["action"])) + pips
        + f'<span class="t">T+ <b>{duration(row["elapsed_seconds"])}</b></span>'
    )
    hb = spec["hb"]
    level = _tone(hb if hb in ("warn", "bad") else "")
    age = row.get("heartbeat_age_seconds")
    if hb == "none":
        width, hb_text = 0, "hb —"
    elif hb == "live":
        safe = max(0, int(age or 0))
        width, hb_text = max(8, 100 - safe * 6), f"hb {duration(safe)}"
    else:
        width, hb_text = 100, f"hb {duration(age)}"
    line4 = (
        f'<div class="hbar{level}" aria-hidden="true"><i style="width:{width}%"></i></div>'
        f'<span class="hbt{level}">{_e(hb_text)}</span>'
    )
    return (
        f'<a class="tile {who}{" bad" if spec.get("bad") else ""}" href="{_e(ticket_page_href(row))}" '
        f'title="{_e(title)}">{px_job_strip(row, 16)}<div class="tile-body">'
        f'<div class="tile-l1">{line1}</div><div class="tile-l2">{line2}</div>'
        f'<div class="tile-l3">{line3}</div><div class="tile-l4">{line4}</div></div></a>'
    )


def _held_tile(key: str, seed: int) -> str:
    from lib.dashboard_pages.shell import px_strip

    strip = px_strip(key, state="held", seed=seed, height=16)
    return f'<div class="tile empty px-held">{strip}<div class="lbl">held</div></div>'


def _idle_rail(count: int, off: bool) -> str:
    return (
        f'<div class="rail{" off" if off else ""}"><span class="sq" aria-hidden="true">{"<i></i>" * count}</span>'
        f'<span class="lbl">{_e(f"{count} off" if off else f"{count} idle")}</span></div>'
    )


def _queue_lane(snapshot: dict) -> str:
    from lib.dashboard_pages.shell import px_strip

    rows = snapshot["queued"]
    oldest = max([row["age_seconds"] or 0 for row in rows], default=0)
    belt = px_strip("queue", effect="conveyor", tint=QUEUE_TINT, seed=20, height=12,
                    speed=queue_speed(oldest), still=not rows)
    if not rows:
        inner = belt + '<div class="sub">queue empty</div>'
    else:
        items = []
        for i, row in enumerate(rows):
            if row.get("no_waiter"):
                strip = px_strip(f"queue-{row['job']}", effect="static", tint=_BAD, seed=41 + i, height=12)
                chip = (
                    f'<a class="chip bad" href="{_e(_health_link(row.get("no_waiter_link")))}">'
                    f'no waiter for {_e(row["no_waiter"])}</a>'
                )
            else:
                strip = px_strip(f"queue-{row['job']}", effect="conveyor", tint=BELT_TINT, seed=41 + i,
                                 height=12, speed=queue_speed(row["age_seconds"]))
                chip = ""
            items.append(
                f'<li><a href="{_e(ticket_page_href(row))}">{_bot(row["recipient"])}'
                f'{_span("ticket", _job_label(row))}{_tier_chip(row.get("tier"))}</a>'
                f'{_span("what", row["action"])}'
                f'{_when(row["age_seconds"], "waiting ")}{chip}'
                f'<span class="belt" aria-hidden="true">{strip}</span></li>'
            )
        inner = belt + f'<ul class="qrail">{"".join(items)}</ul>'
    return (
        '<div class="lane" id="queue" data-device="queue"><div class="lane-head">'
        f'<div class="name">queue</div><div class="sub">{len(rows)} waiting</div></div>'
        f'<div class="lane-body">{inner}</div></div>'
    )


def _ci_card(row: dict) -> str:
    from lib.dashboard_pages.shell import px_strip

    ci = row["ci_wait"]
    step = STEP_AFTER_CI.get(ci["reason"])
    strip = px_strip(f"ci-{row['repo']}#{row['ticket_id']}", effect="static", tint=CI_TINT, height=8)
    line3 = (
        (_span("action", f"then {step}") if step else "")
        + (_span("round", f"round {ci['round']}") if step == "review" and ci.get("round") is not None else "")
        + _span("t", "waiting " + duration(ci["waiting_seconds"]))
    )
    return (
        f'<div class="tile ci">{strip}<div class="tile-body">'
        f'<div class="tile-l1">{_span("ticket", _ticket(row))}{github_links(row)}</div>'
        '<div class="tile-l2"><span class="verb warn">waiting on CI</span></div>'
        f'<div class="tile-l3">{line3}</div>'
        f'<div class="tile-l4">{_span("what", checks_named(ci))}{checks_link(row)}</div></div></div>'
    )


def _ci_lane(snapshot: dict) -> list[str]:
    rows = [row for row in snapshot["tickets"] if row.get("ci_wait")]
    if not rows:
        return []
    return [
        '<div class="lane" data-device="ci-wait"><div class="lane-head"><div class="name">waiting on CI</div>'
        f'<div class="sub">{len(rows)} ticket{"" if len(rows) == 1 else "s"}</div></div>'
        f'<div class="tiles">{"".join(_ci_card(row) for row in rows)}</div></div>'
    ]


def _lanes(snapshot: dict) -> str:
    from lib.dashboard_pages.shell import px_strip

    devices, wakes = lane_devices(snapshot)
    held_tiles = idle_tiles = 0
    rows = [_queue_lane(snapshot)]
    for index, device in enumerate(devices):
        down = probe_down(snapshot, device["name"])
        cap = device["slot_max"]
        slots = max(1, device["used"]) if cap is None else max(1, cap)
        meter = px_strip(
            f"meter-{device['name']}", effect="meter", tint=_BAD if down else "#9ece6a",
            used=device["used"], slots=slots, seed=21 + index, height=8,
            still=down or not device["enabled"],
        )
        harness = (
            f'<span class="harness">{_e(" · ".join(device["harnesses"]))}</span>'
            if device["harnesses"] else ""
        )
        tiles = []
        seed = 0
        for row in device["jobs"]:
            seed += 1
            tiles.append(_slot(row, seed))
        for _ in range(device["used"] - len(device["jobs"])):
            seed += 1
            tiles.append(_held_tile(f"slot-{device['name']}-{seed}", seed))
        free = max(0, (cap or 0) - device["used"])
        if not device["used"] and not free:
            free = 1
        held_tiles += device["used"] - len(device["jobs"])
        if device["enabled"]:
            idle_tiles += free
        if free:
            tiles.append(_idle_rail(free, not device["enabled"]))
        rows.append(
            f'<div class="lane{"" if device["enabled"] else " disabled"}" data-device="{_e(device["name"])}">'
            f'<div class="lane-head"><div class="lh1"><span class="name">{_e(device["name"])}</span>'
            f'{_device_chip(device, down)}'
            f'<div class="lane-meter" aria-hidden="true" style="width:{12 * slots}px">{meter}</div></div>'
            f'<div class="lh2"><span class="sub">{device["used"]} / {"?" if cap is None else cap} slots</span>'
            f'{harness}</div></div>'
            f'<div class="tiles">{"".join(tiles)}</div></div>'
        )
    if wakes:
        rows.append(
            '<div class="lane" data-device="grok-wakes"><div class="lane-head">'
            f'<div class="name">grok wakes</div>{_chip("BOT-TO-BOT", "accent")}</div>'
            f'<div class="tiles">{"".join(_slot(row, i + 1) for i, row in enumerate(wakes))}</div></div>'
        )
    rows.extend(_ci_lane(snapshot))
    warn = ' class="warn"' if held_tiles else ""
    count = (
        f'<b class="ok">{len(snapshot["running"])}</b> running · <b{warn}>{held_tiles}</b> held · '
        f"<b>{idle_tiles}</b> idle · <b>{len(snapshot['queued'])}</b> queued"
    )
    return _panel("lanes", "Lanes and slots", f'<div class="lanes">{"".join(rows)}</div>', count_html=count)


# --- Just happened and Intake -------------------------------------------------------------


def _just_happened(snapshot: dict) -> str:
    rows = (snapshot.get("handoff_savings") or {}).get("recent", [])[:6]
    items = []
    for row in rows:
        wake = row["kind"] == "bot_to_bot"
        items.append(
            "<li>" + _bot(row["recipient"]) + _span("ticket", _ticket(row)) + github_links(row)
            + _span("what", "done" if wake else row["action"])
            + (_chip("grok wake", "wake") if wake else "")
            + (_chip("failed", "bad") if row["state"] == "failed" else "")
            + _when(row["age_seconds"], suffix=" ago") + "</li>"
        )
    inner = f'<ul class="feed">{"".join(items)}</ul>' if items else '<p class="empty">no finished jobs yet</p>'
    inner += '<p class="more"><a href="activity.html">all activity</a></p>'
    return _panel("just-happened", "Just happened", inner, count=f"{len(rows)} newest handoffs")


_FEEDER_STATE = {
    "eligible": "eligible",
    "waiting-capacity": "waiting for capacity",
    "waiting-blocked": "waiting on a blocker",
    "refused-intake": "refused at intake",
    "feeding": "feeding",
    "error": "feed error, retrying",
    "fed": "fed",
    "done": "fed, job queued",
    "closed": "PR closed, not fed",
}


def _linear_webhook(snapshot: dict) -> str:
    """Linear webhook health (#56), Intake's first line; only when a listener or
    webhook-mode poller has run on this database."""
    webhook = snapshot["poller"].get("linear_webhook")
    if webhook is None:
        return ""
    if webhook["last_received_age_seconds"] is not None:
        received = f'{duration(webhook["last_received_age_seconds"])} ago' + (
            f' ({webhook["last_event"]})' if webhook["last_event"] else ""
        )
    elif webhook["listener_age_seconds"] is None:
        received = "never (listener has not started)"
    else:
        received = "never"
    extra = f'<p class="bad">warning: {_e(webhook["warning"])}</p>' if webhook["warning"] is not None else ""
    if webhook.get("note") is not None:
        extra += f'<p class="muted">{_e(webhook["note"])}</p>'
    return (
        '<div class="irow" id="linear-webhook"><span class="ilabel">Linear webhook</span>'
        f'<span class="itext">last received {_e(received)} · dirty queue {_e(webhook["dirty"])}</span>'
        f"{extra}</div>"
    )


def intake_groups(items: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    groups: dict[str, list[dict]] = {"trouble": [], "waiting": [], "fed": []}
    for item in items:
        age = item.get("fed_age_seconds")
        group = INTAKE_GROUP.get(item["state"]) or ("fed" if age is not None and age < FED_TODAY_SECONDS else None)
        if group:
            groups[group].append(item)
    return groups["trouble"], groups["waiting"], groups["fed"]


def _feeder(snapshot: dict) -> str:
    """The backlog feeder (#85), Intake's second line and its meter; only once a feeder has run."""
    from lib.dashboard_pages.shell import px_strip

    feeder = snapshot.get("feeder")
    if not feeder:
        return ""
    meter = px_strip("intake-meter", effect="meter", tint="#9ece6a", used=feeder["fed"],
                     slots=max(1, feeder["eligible"]), seed=24, height=8, still=True)
    return (
        '<div class="irow" id="feeder"><span class="ilabel">Feeder</span>'
        f'<span class="itext">eligible {_e(feeder["eligible"])} · fed {_e(feeder["fed"])} · '
        f'waiting for capacity {_e(feeder["waiting_capacity"])} · '
        f'waiting on blockers or files {_e(feeder.get("waiting_blocked", 0))}</span></div>'
        f'<div class="imeter" aria-hidden="true">{meter}</div>'
    )


def _tier_chip(tier: object) -> str:
    return "" if tier is None else _chip(f"tier {tier}", "accent")


def _intake_row(item: dict, cls: str = "what", tail: str = "") -> str:
    state = item.get("reason") or _FEEDER_STATE.get(item["state"], item["state"])
    ticket = f"{_short(item['repo'])}#{item['issue']}"
    return f'<li>{_span("ticket", ticket)}{_tier_chip(item.get("tier"))}{_span(cls, state)}{tail}</li>'


def _fed_row(item: dict) -> str:
    pr = _span(None, f"→ PR #{item['pr']}") if item.get("pr") else ""
    return _intake_row(item, tail=pr + _when(item["fed_age_seconds"], prefix="fed ", suffix=" ago"))


def _intake(snapshot: dict) -> str:
    feeder = snapshot.get("feeder")
    lines = _linear_webhook(snapshot) + _feeder(snapshot)
    if not lines:
        return _panel("intake", "Intake", '<div class="intake"><p class="empty">no listener or feeder has run</p></div>')
    inner = f'<div class="intake">{lines}</div>'
    if not feeder:
        return _panel("intake", "Intake", inner)
    trouble, waiting, fed = intake_groups(feeder["items"])
    if trouble:
        inner += f'<ul class="feed">{"".join(_intake_row(x, "what bad") for x in trouble)}</ul>'
    if waiting:
        inner += (
            f'<div class="igroup">{len(waiting)} waiting</div>'
            f'<ul class="feed">{"".join(_intake_row(x) for x in waiting[:3])}</ul>'
        )
    if waiting[3:]:
        inner += (
            f"<details><summary>{len(waiting) - 3} more</summary>"
            f'<ul class="feed">{"".join(_intake_row(x) for x in waiting[3:])}</ul></details>'
        )
    if fed:
        inner += (
            f"<details><summary>{len(fed)} fed today</summary>"
            f'<ul class="fed-more">{"".join(_fed_row(x) for x in fed)}</ul></details>'
        )
    return _panel("intake", "Intake", inner, count=f"{len(waiting)} waiting · {len(fed)} fed today")


def _just_happened_and_intake(snapshot: dict) -> str:
    return f'<div class="pair">{_just_happened(snapshot)}{_intake(snapshot)}</div>'


def lanes_script(script: str) -> str:
    return script.replace("__LANES__", json.dumps(lanes_table(), separators=(",", ":")))
