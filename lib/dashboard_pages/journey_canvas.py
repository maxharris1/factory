"""Journey canvas for the ticket page, ported from JourneyClimb.dc.html."""

from __future__ import annotations

import json

from lib.dashboard_flow import FLOWMAP_TINT, STOP_RED

LANE = {
    "intake": "ask",
    "questions": "ask",
    "waiting-max": "ask",
    "planning": "plan",
    "plan-check": "plan",
    "building": "build",
    "fixing": "build",
    "updating": "build",
    "review": "review",
    "capped": "review",
    "ship": "ship",
    "awaiting-ok": "ship",
    "waiting-external": "ship",
    "merge-queue": "ship",
    "merged": "shipped",
    "post-merge": "shipped",
    "done": "shipped",
    "handed-off": "shipped",
    "refused": "stop",
    "abandoned": "stop",
    "stuck": "stop",
    "blocked": "stop",
}

TINT = FLOWMAP_TINT
STOP = STOP_RED

_SCRIPT = r"""
var JOURNEY = __JOURNEY__;
var JOURNEY_LANE_Y = { shipped: 4, ship: 13, review: 22, build: 31, plan: 40, ask: 49, stop: 62 };
var JOURNEY_LANE_LABELS = [['shipped', 4, '#9ece6a'], ['ship', 13, '#8b93b8'], ['review', 22, '#8b93b8'], ['build', 31, '#8b93b8'], ['plan', 40, '#8b93b8'], ['ask', 49, '#8b93b8'], ['stopped', 62, '#f7768e']];
var JOURNEY_FONT = '"JetBrains Mono", ui-monospace, Menlo, monospace';
var JOURNEY_BAYER = (function () {
  var m = [0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26,
    12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22,
    3, 35, 11, 43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25,
    15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21];
  var out = [];
  for (var i = 0; i < 64; i++) out.push((m[i] + 0.5) / 64);
  return out;
})();

function journeyRgb(hex) {
  var h = String(hex).replace('#', '');
  return [parseInt(h.substr(0, 2), 16), parseInt(h.substr(2, 2), 16), parseInt(h.substr(4, 2), 16)];
}

function journeyMix(a, b, k) {
  return 'rgb(' + Math.round(a[0] + (b[0] - a[0]) * k) + ',' + Math.round(a[1] + (b[1] - a[1]) * k) + ',' + Math.round(a[2] + (b[2] - a[2]) * k) + ')';
}

function journeyFmt(sec) {
  if (sec === null || sec === undefined || !isFinite(Number(sec))) return '—';
  sec = Math.round(Number(sec));
  if (sec < 60) return sec + 's';
  var m = Math.floor(sec / 60), s = sec % 60;
  if (sec < 3600) return (s && m < 10) ? m + 'm ' + (s < 10 ? '0' : '') + s + 's' : m + 'm';
  var h = Math.floor(m / 60);
  m = m % 60;
  return h + 'h ' + (m < 10 ? '0' : '') + m + 'm';
}

function journeyLayout(model, scale) {
  var visits = (model && model.visits) || [];
  var n = visits.length, LX0 = 24, PW = 176;
  var bg = [14, 14, 20], white = [255, 255, 255];
  var i, d, v, lane, hex, rgb, x;
  var durs = [], total = 0;
  for (i = 0; i < n; i++) {
    d = Number(visits[i].duration_seconds);
    durs.push(isFinite(d) && d > 0 ? d : 0);
    total += durs[i];
  }
  var toScale = scale === 'scale' && !!model.all_durations_known && total > 0;
  var even = Math.max(6, Math.min(24, Math.floor(PW / Math.max(1, n))));
  if (even * n > PW) even = Math.max(1, Math.floor(PW / n));
  var w = [], sum = 0, big = 0;
  for (i = 0; i < n; i++) {
    w.push(toScale ? Math.max(3, Math.round(PW * durs[i] / total)) : even);
    sum += w[i];
    if (w[i] > w[big]) big = i;
  }
  if (sum > PW) w[big] -= (sum - PW);

  var cur = n - 1;
  for (i = 0; i < n; i++) if (visits[i].current) cur = i;

  var bars = [], ticks = [], ruler = [];
  x = LX0;
  for (i = 0; i < n; i++) {
    v = visits[i];
    lane = JOURNEY.lane[v.state] || 'ask';
    hex = v.stop ? JOURNEY.stop : (JOURNEY.tint[v.who] || JOURNEY.tint.script);
    rgb = journeyRgb(hex);
    bars.push({
      index: v.index === undefined ? i : v.index,
      x: x, w: w[i], y0: JOURNEY_LANE_Y[lane], lane: lane,
      offChart: !!v.off_chart, stop: !!v.stop,
      lit: hex, dim: journeyMix(bg, rgb, 0.3), crest: journeyMix(rgb, white, 0.5)
    });
    if (!toScale) {
      ticks.push(x);
      ruler.push({ label: journeyFmt(v.duration_seconds), left: x * 4, width: w[i] * 4, color: i === cur ? '#7dcfff' : '#8b93b8' });
    }
    x += w[i];
  }
  var nowX = x;
  ticks.push(nowX);

  if (toScale) {
    var acc = 0, k = 1, tx;
    for (i = 0; i < n; i++) {
      while (durs[i] > 0 && k * 900 <= acc + durs[i]) {
        tx = Math.round(bars[i].x + (k * 900 - acc) / durs[i] * bars[i].w);
        ticks.push(tx);
        if (nowX - tx > 10) ruler.push({ label: '+' + journeyFmt(k * 900), left: tx * 4 - 30, width: 60, color: '#8b93b8' });
        k += 1;
      }
      acc += durs[i];
    }
  }

  return {
    bars: bars, cur: cur, nowX: nowX, endX: nowX + 49, ticks: ticks, ruler: ruler,
    green: journeyMix(bg, journeyRgb('#9ece6a'), 0.34), red: journeyMix(bg, journeyRgb(JOURNEY.stop), 0.3)
  };
}

function journeyBars(model, scale) {
  var bars = journeyLayout(model, scale).bars, out = [];
  for (var i = 0; i < bars.length; i++) out.push({ index: bars[i].index, x: bars[i].x * 4, y: 0, w: bars[i].w * 4, h: 300 });
  return out;
}

function drawJourney(ctx, model, opts) {
  opts = opts || {};
  var L = journeyLayout(model, opts.scale);
  var bars = L.bars, n = bars.length, cur = L.cur, bay = JOURNEY_BAYER;
  var reduced = !!opts.reduced;
  var still = reduced || !!model.terminal || (n > 0 && bars[cur].stop);
  var lively = !still && !opts.calm;
  var t = Math.max(0, Number(opts.t) || 0);
  var frac = reduced ? 1 : Math.min(1, t / 3);
  var arrived = frac >= 1;
  var hops = Number(model.hops_to_shipped) || 0;
  var beat = opts.beat === undefined || opts.beat === null ? -1 : Number(opts.beat);
  var beatX = (lively && arrived && beat >= 0) ? 24 + beat * (L.nowX + 6 - 24) : -99;
  var marchK = Math.floor(t * 8);
  var slow = Math.floor(t * 3);
  var block = function (bx, by, bw, bh) { ctx.fillRect(bx * 4, by * 4, bw * 4, bh * 4); };
  var i, x, y, v, nx, k, a, b, step, bw, prevMid, ph;

  ctx.fillStyle = '#0e0e14';
  ctx.fillRect(0, 0, 1000, 336);

  ctx.fillStyle = '#1c1f2e';
  var rules = [12, 21, 30, 39, 48, 57];
  for (i = 0; i < rules.length; i++) {
    for (x = 24; x < 248; x += 3) block(x, rules[i], 1, 1);
  }
  ctx.fillStyle = L.green;
  for (y = 4; y < 12; y++) {
    for (x = L.nowX + 1; x < 248; x++) {
      if ((lively ? 0.15 + 0.11 * Math.sin(t * 0.9 + ((x * 73 + y * 151) % 64) * 0.098) : 0.16) > bay[((y & 7) << 3) | (x & 7)]) block(x, y, 1, 1);
    }
  }
  ctx.fillStyle = L.red;
  for (y = 62; y < 71; y++) {
    for (x = 24; x < 248; x++) {
      if (0.11 > bay[((y & 7) << 3) | (x & 7)]) block(x, y, 1, 1);
    }
  }
  ctx.fillStyle = '#24283b';
  for (x = 24; x < 248; x += 2) block(x, 60, 1, 1);

  ctx.fillStyle = '#414868';
  block(24, 74, L.nowX - 24, 1);
  for (x = L.nowX + 2; x < 248; x += 3) block(x, 74, 1, 1);
  for (i = 0; i < L.ticks.length; i++) block(L.ticks[i], 73, 1, 3);

  if (arrived) {
    ctx.fillStyle = '#3c6f8c';
    for (y = 4; y < 74; y++) {
      if (lively ? ((y + slow) % 4) < 2 : y % 2 === 0) block(L.nowX, y, 1, 1);
    }
    prevMid = n ? bars[cur].y0 + 4 : 53;
    if (hops > 0) {
      var tx0 = L.nowX + 4, tx1 = L.endX - 2;
      for (x = tx0, k = 0; x <= tx1; x++, k++) {
        if ((lively ? (((k - slow) % 3) + 3) % 3 : k % 3) !== 0) continue;
        y = Math.round(prevMid + (7 - prevMid) * (k / Math.max(1, tx1 - tx0)));
        ctx.fillStyle = y < 13 ? '#5c7a45' : '#565d80';
        block(x, y, 1, 1);
      }
      ctx.fillStyle = '#9ece6a';
      block(L.endX, 4, 1, 7);
      if (lively && Math.floor(t * 2) % 2 === 1) {
        block(L.endX + 1, 4, 2, 3);
        block(L.endX + 3, 5, 1, 2);
      } else {
        block(L.endX + 1, 4, 3, 3);
      }
    }
  }

  var revealX = arrived ? L.nowX : 24 + frac * (L.nowX - 24);
  var tokenX = 24, tokenY = n ? bars[0].y0 : 49;
  for (i = 0; i < n; i++) {
    v = bars[i];
    if (v.x >= revealX) break;
    bw = Math.max(1, Math.min(v.w - 1, Math.ceil(revealX - v.x)));
    tokenX = v.x + bw;
    tokenY = v.y0;
    if (arrived && i === cur) {
      ctx.fillStyle = v.lit;
      block(v.x, v.y0 + 2, bw, 5);
      for (x = v.x; x < v.x + bw; x++) {
        ctx.fillStyle = (still || ((((x - marchK) % 6) + 6) % 6) < 3) ? v.crest : v.dim;
        block(x, v.y0 + 6, 1, 1);
      }
    } else {
      ctx.fillStyle = v.dim;
      block(v.x, v.y0 + 2, bw, 5);
      ctx.fillStyle = v.lit;
      block(v.x, v.y0 + 2, bw, 1);
      if (!arrived && lively && revealX >= v.x + v.w - 1 && revealX - (v.x + v.w - 1) < 7) {
        ctx.fillStyle = v.crest;
        block(v.x, v.y0 + 2, bw, 5);
      }
      if (beatX >= v.x && beatX < v.x + bw + 6) {
        ctx.fillStyle = v.crest;
        for (x = Math.max(v.x, Math.floor(beatX) - 5); x < Math.min(v.x + bw, Math.floor(beatX) + 1); x++) block(x, v.y0 + 2, 1, 1);
      }
    }
    if (i < n - 1 && revealX >= bars[i + 1].x) {
      nx = bars[i + 1];
      ctx.fillStyle = (beatX >= v.x + v.w - 3 && beatX < v.x + v.w + 5) ? nx.crest : nx.lit;
      a = v.y0 + 4;
      b = nx.y0 + 4;
      step = b > a ? 1 : -1;
      x = v.x + v.w - 1;
      if (a === b) block(x, a, 1, 1);
      for (y = a, k = 0; y !== b + step && a !== b; y += step, k++) {
        if (!(nx.offChart && k % 2 === 1)) block(x, y, 1, 1);
      }
    }
  }

  var sel = Number(opts.selected);
  if (!(sel >= 0 && sel < n)) sel = cur;
  if (arrived && n && sel !== cur) {
    v = bars[sel];
    ctx.fillStyle = '#c0caf5';
    block(v.x - 1, v.y0 + 1, v.w + 1, 1);
    block(v.x - 1, v.y0 + 7, v.w + 1, 1);
    block(v.x - 1, v.y0 + 1, 1, 7);
    block(v.x + v.w - 1, v.y0 + 1, 1, 7);
  }
  if (!reduced && n && Number(opts.flash) > 0) {
    v = bars[sel];
    ctx.fillStyle = v.crest;
    block(v.x, v.y0 + 2, v.w - 1, 5);
  }

  if (n) {
    if (arrived) {
      v = bars[cur];
      ctx.fillStyle = '#7dcfff';
      block(v.x - 1, v.y0 + 1, v.w + 1, 1);
      block(v.x - 1, v.y0 + 7, v.w + 1, 1);
      block(v.x - 1, v.y0 + 1, 1, 7);
      tokenX = L.nowX - 1;
      tokenY = v.y0;
      if (lively) {
        ctx.fillStyle = '#7dcfff';
        for (k = 0; k < 3; k++) {
          ph = (t * 0.9 + k * 0.37) % 1;
          if (ph < 0.75) block(L.nowX + 2 + k * 2, v.y0 + 1 - Math.floor(ph * 7), 1, 1);
        }
      }
    }
    ctx.fillStyle = (still || Math.floor(t * 2) % 2 === 0) ? '#b8e5ff' : '#7dcfff';
    block(tokenX, tokenY + 1, 2, 7);
  }

  journeyText(ctx, L, model, opts, hops);
}

function journeyText(ctx, L, model, opts, hops) {
  var i, r, tag, tw, spaced = 'letterSpacing' in ctx;
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';
  ctx.font = '11px ' + JOURNEY_FONT;
  if (spaced) ctx.letterSpacing = '0.88px';
  for (i = 0; i < JOURNEY_LANE_LABELS.length; i++) {
    ctx.fillStyle = JOURNEY_LANE_LABELS[i][2];
    ctx.fillText(JOURNEY_LANE_LABELS[i][0].toUpperCase(), 10, JOURNEY_LANE_LABELS[i][1] * 4 + 10 + 7);
  }
  if (spaced) ctx.letterSpacing = '0px';

  ctx.font = '10px ' + JOURNEY_FONT;
  ctx.textAlign = 'center';
  for (i = 0; i < L.ruler.length; i++) {
    r = L.ruler[i];
    ctx.save();
    ctx.beginPath();
    ctx.rect(r.left, 304, r.width, 14);
    ctx.clip();
    ctx.fillStyle = r.color;
    ctx.fillText(r.label, r.left + r.width / 2, 311);
    ctx.restore();
  }
  ctx.textAlign = 'left';
  if (opts.startClock) {
    ctx.fillStyle = '#a9b1d6';
    ctx.fillText(String(opts.startClock), 10, 311);
  }
  if (hops > 0) {
    ctx.fillStyle = '#8b93b8';
    ctx.fillText('ahead · ' + hops + '+ hops to shipped', L.nowX * 4 + 12, 311);
  }

  tag = 'NOW · ' + (opts.nowClock || '');
  ctx.font = '700 10px ' + JOURNEY_FONT;
  tw = ctx.measureText(tag).width;
  ctx.fillStyle = '#7dcfff';
  ctx.fillRect(L.nowX * 4 - 20, 0, tw + 8, 14);
  ctx.fillStyle = '#0c0e10';
  ctx.fillText(tag, L.nowX * 4 - 16, 7);
}
"""

SCRIPT = _SCRIPT.replace(
    "__JOURNEY__", json.dumps({"lane": LANE, "tint": TINT, "stop": STOP}, sort_keys=True)
)
