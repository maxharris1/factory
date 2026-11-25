
(function () {
  var TABLE = __PX__;
  var FPS = 20;
  var DATA = ['progress', 'load', 'seed', 'cell', 'speed', 'used', 'slots', 'rate', 'done', 'burst', 'still'];
  var STRIPS = new Map();
  var HOOKS = [];
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  var sysStill = !!(mq && mq.matches);
  var io = typeof IntersectionObserver === 'function' ? new IntersectionObserver(seen) : null;
  var raf = 0;
  var last = -1000;

  function clamp(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

  function num(v) {
    if (v === null || v === undefined || v === '') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function tintOf(v) {
    if (TABLE.bots.hasOwnProperty(v)) return TABLE.bots[v];
    return /^#[0-9a-f]{6}$/.test(String(v)) ? String(v) : TABLE.fallback;
  }

  function readProps(el, beats) {
    var effect = el.getAttribute('data-px');
    var p = {
      effect: TABLE.effects.hasOwnProperty(effect) ? effect : 'off',
      tint: tintOf(el.getAttribute('data-tint')),
      tintMix: 1, cell: 4, fps: FPS, still: effect === 'static', beat: beats
    };
    DATA.forEach(function (k) {
      var v = el.getAttribute('data-' + k);
      if (v !== null) p[k] = v;
    });
    return p;
  }

  function Strip(el) {
    this._el = el;
    this._ctx = null;
    this._sig = null;
    this.props = readProps(el, 0);
    this.beats = 0;
    this.hb = num(el.getAttribute('data-hb'));
    this.onScreen = true;
    this._dead = false;
    this._t0 = performance.now();
    this._beatSeen = 0;
    this._burstSeen = this._num(this.props.burst, 0);
    this._beatAt = -1000;
    this._burstAt = -1000;
    this._doneAt = this._bool(this.props.done) && el.getAttribute('data-settle') === null ? -1000 : null;
    this._sysStill = sysStill;
  }

  Strip.prototype._num = function (v, d) {
    if (v === null || v === undefined || v === '') return d;
    var n = Number(v);
    return isFinite(n) ? n : d;
  };

  Strip.prototype._bool = function (v) {
    return v === true || v === 'true';
  };

  Strip.prototype._h = function (x, y) {
    var n = Math.sin(x * 127.1 + y * 311.7 + this._seed) * 43758.5453;
    return n - Math.floor(n);
  };

  Strip.prototype._noise = function (x, y) {
    var xi = Math.floor(x), yi = Math.floor(y);
    var xf = x - xi, yf = y - yi;
    var u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    var a = this._h(xi, yi), b = this._h(xi + 1, yi);
    var c = this._h(xi, yi + 1), d = this._h(xi + 1, yi + 1);
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  };

  Strip.prototype._bayer = function () {
    if (this._bay) return this._bay;
    var m = [0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26,
      12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22,
      3, 35, 11, 43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25,
      15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21];
    var out = [];
    for (var i = 0; i < 64; i++) out.push((m[i] + 0.5) / 64);
    this._bay = out;
    return out;
  };

  Strip.prototype._hex = function (s) {
    var m = /^#?([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(String(s || '').trim());
    if (!m) return null;
    var h = m[1];
    if (h.length === 3) h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2);
    return [parseInt(h.substr(0, 2), 16), parseInt(h.substr(2, 2), 16), parseInt(h.substr(4, 2), 16)];
  };

  Strip.prototype._mix = function (a, b, k) {
    return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
  };

  Strip.prototype._css = function (c) {
    return 'rgb(' + Math.round(c[0]) + ',' + Math.round(c[1]) + ',' + Math.round(c[2]) + ')';
  };

  /* 5-tone palette: tint mixed into the near-black field, plus a crest. */
  Strip.prototype._pal = function (tint, mixK) {
    var key = String(tint) + '|' + mixK;
    if (!this._pals) this._pals = {};
    if (this._pals[key]) return this._pals[key];
    var bg = [14, 14, 20], green = [158, 206, 106];
    var tc = this._hex(tint) || green;
    var k = mixK < 0 ? 0 : mixK > 1 ? 1 : mixK;
    var c = this._mix(green, tc, k);
    var pal = [null,
      this._css(this._mix(bg, c, 0.3)),
      this._css(this._mix(bg, c, 0.62)),
      this._css(c),
      this._css(this._mix(c, [255, 255, 255], 0.45))];
    this._pals[key] = pal;
    return pal;
  };

  /* Per-frame values each effect needs, computed once. */
  Strip.prototype._prep = function (effect, t, C, R, p) {
    var f = { t: t, C: C, R: R };
    var ph, k;
    if (effect === 'field' || effect === 'decay') {
      f.ft = effect === 'decay' ? 3.1 : t;
      f.gain = 0.2 + 0.8 * Math.max(0, Math.min(1, this._num(p.load, 1)));
      if (effect === 'field') {
        k = Math.floor(t / 2.6);
        f.rAge = t - k * 2.6;
        f.rx = this._h(k, 7.3) * C;
        f.ry = this._h(k, 9.1) * R;
      } else {
        f.frac = Math.min(0.85, Math.floor(Math.max(0, Math.min(1, this._num(p.progress, 0))) * 10) * 0.085);
      }
    } else if (effect === 'fill') {
      f.front = Math.max(0, Math.min(1, this._num(p.progress, 0.5))) * C;
    } else if (effect === 'scan') {
      ph = (t / 3.6) % 1;
      f.dir = ph < 0.5 ? 1 : -1;
      f.head = Math.floor((ph < 0.5 ? ph * 2 : 2 - ph * 2) * (C - 1));
    } else if (effect === 'patch') {
      f.pass = Math.floor(t / 3.4);
      ph = (t / 3.4) % 1;
      f.head = Math.floor(Math.min(1, ph / 0.82) * (C + 2)) - 1;
    } else if (effect === 'spin3') {
      f.step = Math.floor(t * 6) % 8;
      f.s = R >= 6 ? 2 : 1;
      f.y0 = Math.max(0, Math.floor((R - 3 * f.s) / 2));
    } else if (effect === 'twinkle') {
      k = Math.floor(t / 3.3);
      f.env = Math.sin(Math.PI * ((t / 3.3) % 1));
      f.ax = Math.floor(this._h(k, 1.1) * C);
      f.ay = Math.floor(this._h(k, 2.2) * R);
      f.bx = Math.floor(this._h(k, 3.3) * C);
      f.by = Math.floor(this._h(k, 4.4) * R);
    } else if (effect === 'conveyor') {
      f.off = Math.floor(t * Math.max(0.5, Math.min(6, this._num(p.speed, 1))));
      f.row = Math.floor(R / 2);
    } else if (effect === 'meter') {
      f.slots = Math.max(1, Math.round(this._num(p.slots, 8)));
      f.used = Math.round(this._num(p.used, 0));
      f.bw = Math.max(2, Math.floor(C / f.slots));
    } else if (effect === 'caret') {
      var rate = this._num(p.rate, 1);
      f.on = rate <= 0 || this._bool(p.still) || this._sysStill ? true : ((t * rate) % 1) < 0.5;
    }
    return f;
  };

  /* Intensity 0..1 for one cell. */
  Strip.prototype._val = function (effect, f, c, r) {
    var v, d, rb, hc, colH, code, inLine, gx, gy, idx, blk, cyc;
    switch (effect) {
      case 'field':
      case 'decay':
        v = this._noise(c * 0.16 + f.ft * 0.22, r * 0.3 - f.ft * 0.07) * 0.85
          + (0.5 + 0.5 * Math.sin(f.ft * 1.05 + this._h(c, r) * 6.283)) * 0.2 - 0.14;
        if (effect === 'field') {
          if (f.rAge < 0.9) {
            d = Math.sqrt((c - f.rx) * (c - f.rx) + (r - f.ry) * (r - f.ry));
            d = 1 - Math.abs(d - f.rAge * 16) / 1.6;
            if (d > 0) v += d * (1 - f.rAge / 0.9) * 0.7;
          }
          return v * f.gain;
        }
        if (this._h(c + 50.5, r + 50.5) < f.frac) v = Math.min(v, 0.16);
        return v;

      case 'fill':
        colH = (f.front - c) / 7 + (this._h(c, 3.7) - 0.5) * 0.3;
        colH = colH < 0 ? 0 : colH > 1 ? 1 : colH;
        hc = Math.round(colH * f.R);
        rb = f.R - 1 - r;
        if (rb < hc) return (rb === hc - 1 && hc < f.R) ? 0.86 : 0.5 + 0.22 * this._h(c, r);
        if (hc < f.R && c < f.front + 2 && c > f.front - 9) {
          cyc = Math.floor(f.t / 1.2 + this._h(c, 5.9));
          if (this._h(c + cyc * 7.7, 8.8) < 0.55) {
            d = Math.floor(((f.t / 1.2 + this._h(c, 5.9)) % 1) * (f.R - hc + 2));
            if (r === d) return 1;
          }
        }
        return rb === 0 ? 0.2 : 0;

      case 'scan':
      case 'patch':
        code = (r % 2) === 1;
        inLine = code
          && c >= Math.floor(this._h(r, 13.1) * 3) * 2
          && c < (0.4 + 0.58 * this._h(r, 11.3)) * f.C
          && this._h(Math.floor(c / 6), r + 20.2) > 0.2;
        v = inLine ? 0.27 : 0;
        if (effect === 'scan') {
          d = (f.head - c) * f.dir;
          if (d >= 0 && d < 2) v += code ? 1 : 0.5;
          else if (d >= 2 && d < 8) v += (code ? 0.8 : 0.4) * (1 - (d - 2) / 6);
          return v;
        }
        if (inLine && this._h(c + f.pass * 31.7, r + f.pass * 17.3) < 0.16) v = c <= f.head ? 0.86 : 0;
        d = f.head - c;
        if (d >= 0 && d < 2) v = Math.max(v, code ? 1 : 0.45);
        return v;

      case 'spin3':
        if (c < 1 || r < f.y0) return 0;
        gx = Math.floor((c - 1) / f.s);
        gy = Math.floor((r - f.y0) / f.s);
        if (gx > 2 || gy > 2 || (gx === 1 && gy === 1)) return 0;
        idx = gy === 0 ? gx : gx === 2 ? 2 + gy : gy === 2 ? 6 - gx : 7;
        d = (f.step - idx + 8) % 8;
        return d === 0 ? 1 : d === 1 ? 0.75 : d === 2 ? 0.5 : 0.25;

      case 'static':
        return 0.375;

      case 'twinkle':
        return ((c === f.ax && r === f.ay) || (c === f.bx && r === f.by)) ? f.env * 0.62 : 0;

      case 'conveyor':
        if (r !== f.row) return 0;
        return ((c + f.off) % 4) === 0 ? 0.75 : 0.1;

      case 'meter':
        blk = Math.floor(c / f.bw);
        if (blk >= f.slots || (c % f.bw) === f.bw - 1) return 0;
        return blk < f.used ? 0.75 + 0.2 * Math.sin(f.t * 2.2 + blk * 0.9) : 0.25;

      case 'caret':
        return f.on ? 0.75 : 0;

      default:
        return 0;
    }
  };

  Strip.prototype._draw = function (now) {
    var el = this._el || (this._cid ? document.getElementById(this._cid) : null);
    if (!el || !el.getContext) return;
    var p = this.props;
    var cssW = el.clientWidth, cssH = el.clientHeight;
    if (!cssW || !cssH) return;

    var cell = Math.max(2, Math.min(16, Math.round(this._num(p.cell, 4))));
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    var W = Math.round(cssW * dpr), H = Math.round(cssH * dpr);
    if (el.width !== W || el.height !== H) {
      el.width = W;
      el.height = H;
      this._sig = null;
    }
    var ctx = this._ctx || (this._ctx = el.getContext('2d'));
    if (!ctx) return;

    var C = Math.ceil(cssW / cell), R = Math.max(1, Math.round(cssH / cell));
    var effect = String(p.effect || 'field');
    var still = this._bool(p.still) || this._sysStill;
    var seed = this._num(p.seed, 1);
    this._seed = seed * 17.31;
    var t = still ? 2.4 : (now - this._t0) / 1000 + seed * 1.37;

    /* one-shot triggers: a prop that changes is a real event */
    var beat = this._num(p.beat, 0), burst = this._num(p.burst, 0), done = this._bool(p.done);
    if (beat !== this._beatSeen) { this._beatSeen = beat; this._beatAt = t; }
    if (burst !== this._burstSeen) { this._burstSeen = burst; this._burstAt = t; }
    if (done && this._doneAt === null) this._doneAt = t;
    if (!done) this._doneAt = null;

    var beatAge = still ? 99 : t - this._beatAt;
    var burstAge = still ? 99 : t - this._burstAt;
    var doneAge = this._doneAt === null ? -1 : (still ? 99 : t - this._doneAt);

    /* skip the frame when nothing on this strip can have changed */
    var oneShot = beatAge < 0.7 || burstAge < 0.5 || (doneAge >= 0 && doneAge < 0.8);
    var loops = !(effect === 'static' || effect === 'off' || effect === 'decay' || doneAge >= 0.8
      || (effect === 'caret' && this._num(p.rate, 1) <= 0));
    var animated = !still && (loops || oneShot);
    var sig = [effect, p.tint, p.tintMix, p.progress, p.used, p.slots, p.load, cell, W, H, done, still].join('|');
    if (!animated && sig === this._sig) return;
    this._sig = animated ? null : sig;

    var pal = this._pal(p.tint, this._num(p.tintMix, 1));
    var palDone = this._pal('#9ece6a', 1);
    var bay = this._bayer();
    var f = this._prep(effect, t, C, R, p);
    var px = cell * dpr;
    var size = Math.ceil(px);
    var doneFront = doneAge >= 0 ? (doneAge / 0.6) * (C + 3) : -1;
    var rk = beatAge / 0.6;
    var rippleFront = (1 - (1 - rk) * (1 - rk)) * C * 0.7;
    var last = null;
    var r, c, v, usePal, d, s, b, lvl, col;

    ctx.fillStyle = '#0e0e14';
    ctx.fillRect(0, 0, W, H);

    for (r = 0; r < R; r++) {
      for (c = 0; c < C; c++) {
        usePal = pal;
        if (doneAge >= 0 && c < doneFront) {
          /* done settles: a green sweep, then still */
          usePal = palDone;
          v = (doneFront - c < 3) ? 1 : 0.44;
        } else {
          v = this._val(effect, f, c, r);
          if (beatAge < 0.6) {
            /* heartbeat ripple from the left edge */
            d = rippleFront - c;
            if (d >= 0 && d < 4) v = Math.max(v, (1 - rk) * (1.15 - d / 4));
          }
          if (burstAge < 0.45 && this._h(c + this._burstSeen * 13.7, r + 5.1) < 0.4) {
            v = Math.max(v, 1 - burstAge / 0.45);
          }
        }
        if (v <= 0) continue;
        if (v > 1) v = 1;
        s = v * 4;
        b = Math.floor(s);
        lvl = b + ((s - b) > bay[((r & 7) << 3) | (c & 7)] ? 1 : 0);
        if (lvl <= 0) continue;
        if (lvl > 4) lvl = 4;
        col = usePal[lvl];
        if (col !== last) { ctx.fillStyle = col; last = col; }
        ctx.fillRect(Math.round(c * px), Math.round(r * px), size, size);
      }
    }
  };

  function paint(s, now) {
    if (s._dead || !s.onScreen || document.hidden) return;
    try { s._draw(now); } catch (e) { s._dead = true; }
  }

  function start() {
    if (!raf && !document.hidden && window.requestAnimationFrame) raf = window.requestAnimationFrame(tick);
  }

  function tick(now) {
    raf = 0;
    if (document.hidden) return;
    if (now - last < 1000 / FPS - 3) { start(); return; }
    last = now;
    var live = false;
    STRIPS.forEach(function (s) {
      if (s._dead || !s.onScreen) return;
      paint(s, now);
      if (!s._dead && s._sig === null) live = true;
    });
    HOOKS.forEach(function (fn) { if (fn(now)) live = true; });
    if (live) start();
  }

  function onFrame(fn) {
    if (HOOKS.indexOf(fn) < 0) HOOKS.push(fn);
    start();
  }

  function seen(entries) {
    entries.forEach(function (entry) {
      var s = STRIPS.get(entry.target.getAttribute('data-px-key'));
      if (s && s._el === entry.target) s.onScreen = !!entry.isIntersecting;
    });
    start();
  }

  function bind() {
    var found = document.querySelectorAll ? document.querySelectorAll('canvas.px') : [];
    var next = new Map();
    for (var i = 0; i < found.length; i++) {
      var el = found[i];
      var key = el.getAttribute('data-px-key');
      if (!key || next.has(key)) continue;
      var s = STRIPS.get(key);
      if (!s) {
        s = new Strip(el);
        if (io) io.observe(el);
      } else {
        var hb = num(el.getAttribute('data-hb'));
        if (hb !== null && s.hb !== null && hb < s.hb) s.beats += 1;
        s.hb = hb;
        if (s._el !== el) {
          if (io) { io.unobserve(s._el); io.observe(el); }
          s._el = el;
          s._ctx = null;
          s._sig = null;
        }
        s.props = readProps(el, s.beats);
      }
      next.set(key, s);
      paint(s, performance.now());
    }
    STRIPS.forEach(function (s, key) { if (!next.has(key) && io) io.unobserve(s._el); });
    STRIPS = next;
    start();
  }

  function state(row) {
    if (!row) return 'unknown';
    if (row.job_state === 'dead') return 'dead';
    if (row.overdue) return 'overdue';
    if (row.stuck) return 'stuck';
    if (row.job_state === 'stale') return 'stale';
    if (row.job_state === 'claimed') return 'claimed';
    if (row.job_state === 'running' && TABLE.actions.hasOwnProperty(row.action)) return TABLE.actions[row.action];
    return 'unknown';
  }

  function progressOf(name, row) {
    var timeout = Number(row.timeout_seconds) || 1;
    if (name === 'build') return clamp((Number(row.elapsed_seconds) || 0) / timeout);
    if (name === 'stale') return clamp((Number(row.heartbeat_age_seconds) || 0) / timeout);
    return null;
  }

  function strip(key, opts) {
    var entry = TABLE.states[opts.state] || TABLE.states.unknown;
    var effect = opts.effect || entry.effect;
    var tint = opts.tint || entry.tint;
    if (tint === 'bot') tint = TABLE.bots.hasOwnProperty(opts.bot) ? opts.bot : TABLE.fallback;
    if (!TABLE.bots.hasOwnProperty(tint) && !/^#[0-9a-f]{6}$/.test(String(tint))) tint = TABLE.fallback;
    var height = Math.max(2, Math.min(96, Math.round(Number(opts.height) || 16)));
    var el = document.createElement('canvas');
    el.setAttribute('class', 'px');
    el.setAttribute('data-px', TABLE.effects.hasOwnProperty(effect) ? effect : 'off');
    el.setAttribute('data-tint', tint);
    var style = '--px:' + (TABLE.bots.hasOwnProperty(tint) ? 'var(--' + tint + ')' : tint) + ';';
    if (opts.progress !== null && opts.progress !== undefined) {
      var p = clamp(Number(opts.progress) || 0).toFixed(3);
      el.setAttribute('data-progress', p);
      style += '--p:' + p + ';';
    }
    if (opts.heartbeat !== null && opts.heartbeat !== undefined) el.setAttribute('data-hb', String(Math.max(0, Math.floor(Number(opts.heartbeat) || 0))));
    if (opts.load !== null && opts.load !== undefined) {
      var l = clamp(Number(opts.load) || 0).toFixed(3);
      el.setAttribute('data-load', l);
      style += '--l:' + l + ';';
    }
    var rate = opts.rate !== null && opts.rate !== undefined ? opts.rate : entry.rate;
    if (rate !== null && rate !== undefined) el.setAttribute('data-rate', String(Number(rate)));
    var seed = opts.seed !== null && opts.seed !== undefined ? opts.seed : entry.seed;
    if (seed !== null && seed !== undefined) el.setAttribute('data-seed', String(Number(seed)));
    ['speed', 'used', 'slots', 'burst'].forEach(function (k) {
      if (opts[k] !== null && opts[k] !== undefined) el.setAttribute('data-' + k, String(Number(opts[k])));
    });
    if (opts.still) el.setAttribute('data-still', 'true');
    if (opts.cell !== null && opts.cell !== undefined) el.setAttribute('data-cell', String(Math.max(2, Math.min(16, Math.floor(Number(opts.cell) || 4)))));
    if (opts.done) el.setAttribute('data-done', 'true');
    if (opts.settle) el.setAttribute('data-settle', 'true');
    el.setAttribute('data-px-key', String(key));
    el.setAttribute('aria-hidden', 'true');
    el.setAttribute('style', style + 'height:' + height + 'px');
    return el;
  }

  function jobStrip(row, height) {
    var name = state(row);
    return strip('job-' + row.job, {
      state: name, bot: row.recipient, progress: progressOf(name, row),
      heartbeat: name === 'claimed' ? null : row.heartbeat_age_seconds, height: height
    });
  }

  function frame(name) { return (TABLE.states[name] || {}).frame || ''; }

  document.addEventListener('visibilitychange', function () { if (!document.hidden) start(); });
  if (mq) {
    var onMotion = function () {
      sysStill = !!mq.matches;
      STRIPS.forEach(function (s) { s._sysStill = sysStill; s._sig = null; paint(s, performance.now()); });
      start();
    };
    if (mq.addEventListener) mq.addEventListener('change', onMotion);
    else if (mq.addListener) mq.addListener(onMotion);
  }

  return {bind: bind, onFrame: onFrame, state: state, strip: strip, jobStrip: jobStrip, frame: frame, strips: function () { return STRIPS; }, Strip: Strip, table: TABLE};
})()
