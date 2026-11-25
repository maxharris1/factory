
var JOURNEY = __JOURNEY__;
var JOURNEY_GEOM = {
  wide: {
    compact: false, w: 250, h: 84, x0: 24, xr: 248, ahead: 49, plot: 176, plotEnded: 192, endW: 12, minW: 6, maxW: 24,
    laneY: { shipped: 4, ship: 13, review: 22, build: 31, plan: 40, ask: 49, stop: 62 },
    bo: 2, bh: 5, rules: [12, 21, 30, 39, 48, 57],
    glow0: 4, glow1: 12, rail: 60, floor0: 62, floor1: 71, axis: 74, top: 4, aim: 7, pole: 7,
    rulerTop: 304, hitH: 300, textX: 10, laneText: 10, laneFont: '11px ', laneTrack: '0.88px',
    lanes: [['SHIPPED', 'shipped', '#9ece6a'], ['SHIP', 'ship', '#8b93b8'], ['REVIEW', 'review', '#8b93b8'], ['BUILD', 'build', '#8b93b8'], ['PLAN', 'plan', '#8b93b8'], ['ASK', 'ask', '#8b93b8'], ['STOPPED', 'stop', '#f7768e']]
  },
  cells: {},
  compact: function (cells) {
    cells = Math.max(56, Math.min(250, Math.floor(cells) || 0));
    return this.cells[cells] || (this.cells[cells] = {
      compact: true, w: cells, h: 60, x0: 9, xr: cells - 1, ahead: 14, plot: cells - 28, plotEnded: cells - 15, endW: 8, minW: 3, maxW: 12,
      laneY: { shipped: 2, ship: 9, review: 16, build: 23, plan: 30, ask: 37, stop: 46 },
      bo: 1, bh: 4, rules: [8, 15, 22, 29, 36],
      glow0: 2, glow1: 8, rail: 44, floor0: 46, floor1: 53, axis: 54, top: 2, aim: 4, pole: 6,
      rulerTop: 224, hitH: 220, textX: 4, laneText: 5, laneFont: '10px ', laneTrack: '0.4px',
      lanes: [['SHP', 'shipped', '#9ece6a'], ['SHIP', 'ship', '#8b93b8'], ['REV', 'review', '#8b93b8'], ['BLD', 'build', '#8b93b8'], ['PLN', 'plan', '#8b93b8'], ['ASK', 'ask', '#8b93b8'], ['STOP', 'stop', '#f7768e']]
    });
  }
};
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
  if (h >= 24) return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
  m = m % 60;
  return h + 'h ' + (m < 10 ? '0' : '') + m + 'm';
}

var JOURNEY_ET = null;

function journeyClock(epoch) {
  if (epoch === null || epoch === undefined || !isFinite(epoch)) return '';
  if (!JOURNEY_ET) {
    JOURNEY_ET = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  }
  return JOURNEY_ET.format(new Date(epoch * 1000));
}

function journeyEnded(model) {
  var e = model && model.ended;
  return e === 'shipped' || e === 'stopped' ? e : '';
}

function journeyLayout(model, scale, g) {
  g = g || JOURNEY_GEOM.wide;
  var visits = (model && model.visits) || [];
  var ended = journeyEnded(model), live = !ended;
  var n = visits.length, LX0 = g.x0, PW = live ? g.plot : g.plotEnded, endW = g.endW;
  var timed = live ? n : Math.max(0, n - 1);
  var bg = [14, 14, 20], white = [255, 255, 255];
  var i, d, v, lane, hex, rgb, x, last;
  var durs = [], total = 0;
  for (i = 0; i < n; i++) {
    d = Number(visits[i].duration_seconds);
    durs.push(isFinite(d) && d > 0 ? d : 0);
    if (i < timed) total += durs[i];
  }
  var toScale = !g.compact && scale === 'scale' && !!model.all_durations_known && total > 0;
  var even = Math.max(g.minW, Math.min(g.maxW, Math.floor(PW / Math.max(1, n))));
  if (even * n > PW) even = Math.max(1, Math.floor(PW / n));
  var budget = PW - (n - timed) * endW;
  var least = Math.max(1, Math.min(3, Math.floor(budget / Math.max(1, timed))));
  var w = [], sum = 0, big;
  for (i = 0; i < n; i++) {
    w.push(!toScale ? even : i < timed ? Math.max(least, Math.round(budget * durs[i] / total)) : endW);
    sum += w[i];
  }
  while (toScale && sum > PW) {
    big = -1;
    for (i = 0; i < timed; i++) if (w[i] > least && (big < 0 || w[i] > w[big])) big = i;
    if (big < 0) break;
    w[big] -= 1;
    sum -= 1;
  }

  var cur = n - 1;
  for (i = 0; i < n; i++) if (visits[i].current) cur = i;

  var bars = [], ticks = [], ruler = [];
  x = LX0;
  for (i = 0; i < n; i++) {
    v = visits[i];
    last = ended && i === n - 1;
    lane = !last ? (JOURNEY.lane[v.state] || 'ask') : ended === 'shipped' ? 'shipped' : 'stop';
    hex = v.stop || lane === 'stop' ? JOURNEY.stop : (JOURNEY.tint[v.who] || JOURNEY.tint.script);
    rgb = journeyRgb(hex);
    bars.push({
      index: v.index === undefined ? i : v.index, name: String(v.state),
      x: x, w: w[i], y0: g.laneY[lane], lane: lane,
      offChart: !!v.off_chart, stop: !!v.stop, end: last,
      lit: hex, dim: journeyMix(bg, rgb, 0.3), crest: journeyMix(rgb, white, 0.5), ink: journeyMix(rgb, white, 0.35)
    });
    if (!toScale) {
      ticks.push(x);
      if (!last && !g.compact) ruler.push({ label: journeyFmt(v.duration_seconds), left: x * 4, width: w[i] * 4, color: live && i === cur ? '#7dcfff' : '#8b93b8' });
    }
    x += w[i];
  }
  var nowX = x;
  ticks.push(nowX);

  if (toScale) {
    var acc = 0, k = 1, tx;
    for (i = 0; i < timed; i++) {
      while (durs[i] > 0 && k * 900 <= acc + durs[i]) {
        tx = Math.round(bars[i].x + (k * 900 - acc) / durs[i] * bars[i].w);
        ticks.push(tx);
        if (nowX - tx > 10) ruler.push({ label: '+' + journeyFmt(k * 900), left: tx * 4 - 30, width: 60, color: '#8b93b8' });
        k += 1;
      }
      acc += durs[i];
    }
    if (timed < n) ticks.push(bars[n - 1].x);
  }

  return {
    g: g, bars: bars, cur: cur, nowX: nowX, endX: live ? nowX + g.ahead : nowX + 1, ticks: ticks, ruler: ruler, ended: ended,
    green: journeyMix(bg, journeyRgb('#9ece6a'), 0.34), red: journeyMix(bg, journeyRgb(JOURNEY.stop), 0.3)
  };
}

function journeyBars(model, scale, g) {
  var L = journeyLayout(model, scale, g), bars = L.bars, out = [];
  for (var i = 0; i < bars.length; i++) out.push({ index: bars[i].index, x: bars[i].x * 4, y: 0, w: bars[i].w * 4, h: L.g.hitH });
  return out;
}

function drawJourney(ctx, model, opts) {
  opts = opts || {};
  var L = journeyLayout(model, opts.scale, opts.g), g = L.g, BO = g.bo, BH = g.bh, MID = g.bo + 2;
  var bars = L.bars, n = bars.length, cur = L.cur, bay = JOURNEY_BAYER;
  var shipped = L.ended === 'shipped', stopped = L.ended === 'stopped', live = !L.ended;
  var reduced = !!opts.reduced;
  var still = reduced || !!opts.still || stopped || (live && (!!model.terminal || (n > 0 && bars[cur].stop)));
  var drawIn = !still;
  var lively = drawIn && live;
  var t = Math.max(0, Number(opts.t) || 0);
  var frac = (reduced || stopped) ? 1 : Math.min(1, t / 3);
  var arrived = frac >= 1;
  var hops = live ? Number(model.hops_to_shipped) || 0 : 0;
  var beat = opts.beat === undefined || opts.beat === null ? -1 : Number(opts.beat);
  var beatX = (lively && arrived && beat >= 0) ? g.x0 + beat * (L.nowX + 6 - g.x0) : -99;
  var st = (shipped && arrived && !still) ? Math.min(1, Math.max(0, (t - 3) / 1.4)) : 1;
  var front = g.x0 + (st / 0.7) * (g.xr - g.x0 + 8);
  var marchK = Math.floor(t * 8);
  var slow = Math.floor(t * 3);
  var block = function (bx, by, bw, bh) { ctx.fillRect(bx * 4, by * 4, bw * 4, bh * 4); };
  var i, x, y, v, nx, k, a, b, step, bw, prevMid, ph, hot, dens;

  ctx.fillStyle = '#0e0e14';
  ctx.fillRect(0, 0, g.w * 4, g.h * 4);

  ctx.fillStyle = '#1c1f2e';
  for (i = 0; i < g.rules.length; i++) {
    for (x = g.x0; x < g.xr; x += 3) block(x, g.rules[i], 1, 1);
  }
  if (live) {
    ctx.fillStyle = L.green;
    for (y = g.glow0; y < g.glow1; y++) {
      for (x = L.nowX + 1; x < g.xr; x++) {
        if ((lively ? 0.15 + 0.11 * Math.sin(t * 0.9 + ((x * 73 + y * 151) % 64) * 0.098) : 0.16) > bay[((y & 7) << 3) | (x & 7)]) block(x, y, 1, 1);
      }
    }
  } else if (shipped && arrived) {
    for (y = g.glow0; y < g.glow1; y++) {
      for (x = g.x0; x < g.xr; x++) {
        if (x > front) continue;
        hot = st < 1 && front - x < 8;
        dens = hot ? 0.85 : 0.16 + 0.3 * (1 - st);
        if (dens > bay[((y & 7) << 3) | (x & 7)]) {
          ctx.fillStyle = hot ? '#9ece6a' : L.green;
          block(x, y, 1, 1);
        }
      }
    }
  }
  ctx.fillStyle = L.red;
  for (y = g.floor0; y < g.floor1; y++) {
    for (x = g.x0; x < g.xr; x++) {
      if (0.11 > bay[((y & 7) << 3) | (x & 7)]) block(x, y, 1, 1);
    }
  }
  ctx.fillStyle = '#24283b';
  for (x = g.x0; x < g.xr; x += 2) block(x, g.rail, 1, 1);

  ctx.fillStyle = '#414868';
  block(g.x0, g.axis, L.nowX - g.x0, 1);
  if (live) {
    for (x = L.nowX + 2; x < g.xr; x += 3) block(x, g.axis, 1, 1);
  }
  for (i = 0; i < L.ticks.length; i++) block(L.ticks[i], g.axis - 1, 1, 3);

  if (arrived && live) {
    ctx.fillStyle = '#3c6f8c';
    for (y = g.top; y < g.axis; y++) {
      if (lively ? ((y + slow) % 4) < 2 : y % 2 === 0) block(L.nowX, y, 1, 1);
    }
    prevMid = n ? bars[cur].y0 + MID : g.laneY.ask + MID;
    if (hops > 0) {
      var tx0 = L.nowX + 4, tx1 = L.endX - 2;
      for (x = tx0, k = 0; x <= tx1; x++, k++) {
        if ((lively ? (((k - slow) % 3) + 3) % 3 : k % 3) !== 0) continue;
        y = Math.round(prevMid + (g.aim - prevMid) * (k / Math.max(1, tx1 - tx0)));
        ctx.fillStyle = y < g.laneY.ship ? '#5c7a45' : '#565d80';
        block(x, y, 1, 1);
      }
      ctx.fillStyle = '#9ece6a';
      block(L.endX, g.top, 1, g.pole);
      if (lively && Math.floor(t * 2) % 2 === 1) {
        block(L.endX + 1, g.top, 2, 3);
        block(L.endX + 3, g.top + 1, 1, 2);
      } else {
        block(L.endX + 1, g.top, 3, 3);
      }
    }
  }
  if (arrived && shipped) {
    ctx.fillStyle = (st < 1 && Math.floor(st * 8) % 2 === 0) ? '#d6efbf' : '#9ece6a';
    block(L.endX, g.top, 1, g.pole);
    block(L.endX + 1, g.top, 3, 3);
  }

  var revealX = arrived ? L.nowX : g.x0 + frac * (L.nowX - g.x0);
  var tokenX = g.x0, tokenY = n ? bars[0].y0 : g.laneY.ask;
  for (i = 0; i < n; i++) {
    v = bars[i];
    if (v.x >= revealX) break;
    bw = Math.max(1, Math.min(v.w - 1, Math.ceil(revealX - v.x)));
    tokenX = v.x + bw;
    tokenY = v.y0;
    if (arrived && live && i === cur) {
      ctx.fillStyle = v.lit;
      block(v.x, v.y0 + BO, bw, BH);
      for (x = v.x; x < v.x + bw; x++) {
        ctx.fillStyle = (still || ((((x - marchK) % 6) + 6) % 6) < 3) ? v.crest : v.dim;
        block(x, v.y0 + BO + BH - 1, 1, 1);
      }
    } else if (stopped && v.end) {
      ctx.fillStyle = v.dim;
      block(v.x, v.y0 + BO, bw, BH);
      ctx.fillStyle = v.lit;
      for (y = v.y0 + BO; y < v.y0 + BO + BH; y++) {
        for (x = v.x; x < v.x + bw; x++) {
          if (y === v.y0 + BO || 0.42 > bay[((y & 7) << 3) | (x & 7)]) block(x, y, 1, 1);
        }
      }
    } else {
      ctx.fillStyle = v.dim;
      block(v.x, v.y0 + BO, bw, BH);
      ctx.fillStyle = v.lit;
      block(v.x, v.y0 + BO, bw, 1);
      if (!arrived && drawIn && revealX >= v.x + v.w - 1 && revealX - (v.x + v.w - 1) < 7) {
        ctx.fillStyle = v.crest;
        block(v.x, v.y0 + BO, bw, BH);
      }
      if (beatX >= v.x && beatX < v.x + bw + 6) {
        ctx.fillStyle = v.crest;
        for (x = Math.max(v.x, Math.floor(beatX) - 5); x < Math.min(v.x + bw, Math.floor(beatX) + 1); x++) block(x, v.y0 + BO, 1, 1);
      }
      if (shipped && arrived && st < 1 && front >= v.x && front - 8 < v.x + bw) {
        ctx.fillStyle = '#9ece6a';
        for (x = Math.max(v.x, Math.floor(front) - 7); x < Math.min(v.x + bw, Math.floor(front) + 1); x++) block(x, v.y0 + BO, 1, 1);
      }
    }
    if (i < n - 1 && revealX >= bars[i + 1].x) {
      nx = bars[i + 1];
      ctx.fillStyle = (beatX >= v.x + v.w - 3 && beatX < v.x + v.w + 5) ? nx.crest : nx.lit;
      a = v.y0 + MID;
      b = nx.y0 + MID;
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
  if (arrived && n && (!live || sel !== cur)) {
    v = bars[sel];
    ctx.fillStyle = '#c0caf5';
    block(v.x - 1, v.y0 + BO - 1, v.w + 1, 1);
    block(v.x - 1, v.y0 + BO + BH, v.w + 1, 1);
    block(v.x - 1, v.y0 + BO - 1, 1, BH + 2);
    block(v.x + v.w - 1, v.y0 + BO - 1, 1, BH + 2);
  }
  if (!reduced && !stopped && n && Number(opts.flash) > 0) {
    v = bars[sel];
    ctx.fillStyle = v.crest;
    block(v.x, v.y0 + BO, v.w - 1, BH);
  }

  if (n && live) {
    if (arrived) {
      v = bars[cur];
      ctx.fillStyle = '#7dcfff';
      block(v.x - 1, v.y0 + BO - 1, v.w + 1, 1);
      block(v.x - 1, v.y0 + BO + BH, v.w + 1, 1);
      block(v.x - 1, v.y0 + BO - 1, 1, BH + 2);
      tokenX = L.nowX - 1;
      tokenY = v.y0;
      if (lively) {
        ctx.fillStyle = '#7dcfff';
        for (k = 0; k < 3; k++) {
          ph = (t * 0.9 + k * 0.37) % 1;
          if (ph < 0.75) block(L.nowX + 2 + k * 2, v.y0 + BO - 1 - Math.floor(ph * 7), 1, 1);
        }
      }
    }
    ctx.fillStyle = (still || Math.floor(t * 2) % 2 === 0) ? '#b8e5ff' : '#7dcfff';
    block(tokenX, tokenY + BO - 1, 2, BH + 2);
  } else if (n && shipped && !arrived) {
    ctx.fillStyle = '#d6efbf';
    block(tokenX, tokenY + BO - 1, 2, BH + 2);
  }

  journeyText(ctx, L, opts, hops, arrived ? L.nowX : revealX, arrived);
}

function journeyText(ctx, L, opts, hops, revealX, arrived) {
  var g = L.g, i, r, v, tag, tw, tail, spaced = 'letterSpacing' in ctx;
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';
  ctx.font = g.laneFont + JOURNEY_FONT;
  if (spaced) ctx.letterSpacing = g.laneTrack;
  for (i = 0; i < g.lanes.length; i++) {
    ctx.fillStyle = g.lanes[i][2];
    ctx.fillText(g.lanes[i][0], g.textX, g.laneY[g.lanes[i][1]] * 4 + g.laneText + 7);
  }
  if (spaced) ctx.letterSpacing = '0px';

  for (i = 0; i < L.bars.length; i++) {
    v = L.bars[i];
    if (g.compact || v.x + v.w > revealX || v.name.length * 6 + 4 > (v.w - 1) * 4) continue;
    var now = !L.ended && arrived && i === L.cur;
    var mark = L.ended === 'stopped' && v.end;
    ctx.font = (now || mark ? '700' : '500') + ' 10px ' + JOURNEY_FONT;
    ctx.save();
    ctx.beginPath();
    ctx.rect(v.x * 4, v.y0 * 4 + 8, (v.w - 1) * 4, 20);
    ctx.clip();
    if (mark) {
      ctx.fillStyle = v.dim;
      ctx.fillRect(v.x * 4 + 3, v.y0 * 4 + 11, ctx.measureText(v.name).width + 4, 14);
    }
    ctx.fillStyle = now ? '#0c0e10' : v.ink;
    ctx.fillText(v.name, v.x * 4 + 5, v.y0 * 4 + 18);
    ctx.restore();
  }

  ctx.font = '10px ' + JOURNEY_FONT;
  ctx.textAlign = 'center';
  for (i = 0; i < L.ruler.length; i++) {
    r = L.ruler[i];
    ctx.save();
    ctx.beginPath();
    ctx.rect(r.left, g.rulerTop, r.width, 14);
    ctx.clip();
    ctx.fillStyle = r.color;
    ctx.fillText(r.label, r.left + r.width / 2, g.rulerTop + 7);
    ctx.restore();
  }
  ctx.textAlign = 'left';
  if (opts.startClock) {
    ctx.fillStyle = '#a9b1d6';
    ctx.fillText(String(opts.startClock), g.textX, g.rulerTop + 7);
  }
  tail = null;
  if (L.ended === 'shipped' && L.bars.length) tail = ['#9ece6a', L.bars[L.bars.length - 1].name];
  else if (L.ended === 'stopped') tail = ['#f7768e', 'stopped'];
  else if (hops > 0 && !g.compact) tail = ['#8b93b8', 'ahead · ' + hops + '+ hops to shipped'];
  if (tail) {
    ctx.fillStyle = tail[0];
    if (g.compact) ctx.textAlign = 'right';
    ctx.fillText(L.ended && opts.endClock ? tail[1] + ' ' + opts.endClock : tail[1], g.compact ? g.w * 4 - 4 : L.nowX * 4 + 12, g.rulerTop + 7);
    ctx.textAlign = 'left';
  }
  if (L.ended) return;

  tag = 'NOW · ' + (opts.nowClock ? opts.nowClock + ' ET' : '');
  ctx.font = '700 10px ' + JOURNEY_FONT;
  tw = ctx.measureText(tag).width;
  var left = L.nowX * 4 - 20;
  if (g.compact) left = Math.max(0, Math.min(left, g.w * 4 - tw - 8));
  ctx.fillStyle = '#7dcfff';
  ctx.fillRect(left, 0, tw + 8, 14);
  ctx.fillStyle = '#0c0e10';
  ctx.fillText(tag, left + 4, 7);
}
