"""The shell every page shares (#139): TopBar, header band, attention strip, status line,
the panels per page and the page frame; and PixelStrip (#138, docs/design/mission-control/SPEC.md
section 6): px_strip renders a strip canvas from the PX_STATE table and PX_SCRIPT draws every
strip on the page under one animation loop."""

from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_HALF_UP

from lib.dashboard_telemetry import rollup_panel, TELEMETRY_CSS
from lib.health import CODE_COMPONENTS
from lib.pr_title import label
from lib.dashboard_pages.activity import _ATTENTION, _attention, _handoffs, _health_link, _verdicts
from lib.dashboard_pages.common import _chip, duration, _e, _span, _ticket
from lib.dashboard_pages.efficiency import _headroom, rounds_row, savings_row
from lib.dashboard_pages.lanes import _kpis, lane_devices, _lanes, _queue
from lib.dashboard_pages.system import (
    _BUDGET_STATE, _components, _drift_row, _log_row, _no_waiter, _POLL_STATE,
)
from lib.dashboard_pages.ticket_detail import _jobview
from lib.dashboard_pages.tickets import _board, _tickets


CSS = """\
.topbar{width:100%;background:var(--bg);color:var(--text)}
.top-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px;min-height:56px;padding:8px 24px}
.brand{display:flex;align-items:center;gap:6px;font:600 15px/1 var(--mono);letter-spacing:-.01em;color:var(--hdr-text);white-space:nowrap}
.brand b{font-weight:600;color:var(--hdr-brand)}
.caret{display:block;width:8px}
.link{display:flex;align-items:center;gap:8px;font:500 12px/1 var(--mono);letter-spacing:.04em;color:var(--muted)}
.link .dot{width:8px;height:8px;background:currentColor}
.link.live{color:var(--ok)}.link.stale{color:var(--warn)}.link.lost{color:var(--bad)}.link.boot{color:var(--muted)}
.topbar .meta{margin-left:auto;color:var(--muted);font:12px/1.3 var(--mono)}
.topbar .meta span{color:var(--text-2)}
.attn{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;min-height:36px;padding:6px 24px;
background:color-mix(in srgb,var(--bad) 9%,transparent);border-bottom:1px solid color-mix(in srgb,var(--bad) 45%,transparent);
font:12px/1.5 var(--mono)}
.attn-head{display:flex;align-items:center;gap:8px}
.attn-blk{display:block;width:12px}
.attn-count{font-weight:700;color:var(--bad);text-decoration:none}
.attn-item{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
.attn-who{font-weight:600;color:var(--text);text-decoration:none}
.attn-what{color:var(--text-2)}
.attn a:hover{text-decoration:underline}
.statusline{position:sticky;bottom:0;z-index:2;display:flex;flex-wrap:wrap;align-items:stretch;width:100%;min-height:32px;
background:var(--dim);border-top:1px solid var(--line);font:12px/1.3 var(--mono);color:var(--text-2)}
.sl-brand{display:flex;align-items:center;padding:6px 9px;background:var(--ok);color:var(--ink);font-weight:700;letter-spacing:.04em}
.sl-seg{display:flex;align-items:center;gap:6px;padding:6px 9px;border-right:1px solid var(--line)}
.sl-seg.sl-queue{border-right:0}
.sl-k{color:var(--muted)}.sl-v{color:var(--text)}
.sl-fill{flex:1 1 24px}
.sl-vital{display:flex;align-items:center;gap:7px;padding:6px 9px;border-left:1px solid var(--line);text-decoration:none}
.sl-vital:hover{background:var(--panel-hi)}
.sl-vital .blk{width:8px;height:8px;background:currentColor}
.sl-vital.ok{color:var(--text-2)}.sl-vital.ok .blk{background:var(--ok)}
main{position:relative;z-index:1;flex:1 0 auto;width:100%;display:grid;align-content:start;grid-template-columns:repeat(12,minmax(0,1fr));
gap:16px;max-width:1600px;margin:0 auto;padding:20px 24px 32px}
"""


CSS_NAV = """\
.nav{display:flex;flex-wrap:wrap;gap:2px}
.nav a{padding:6px 12px;color:var(--hdr-text-2);text-decoration:none;font:500 14px/1.2 var(--sans)}
.nav a:hover{background:var(--panel-hi);color:var(--hdr-text)}
.nav a[aria-current=page]{color:var(--hdr-text);background:var(--panel-hi);box-shadow:inset 0 -2px 0 var(--brand)}
"""


CSS_PX = """\
.px{display:block;width:100%;height:16px;background-color:var(--field);
background-image:repeating-linear-gradient(90deg,transparent 0 4px,var(--field) 4px 8px),
repeating-linear-gradient(0deg,color-mix(in srgb,var(--px,var(--ok)) 30%,var(--field)) 0 4px,transparent 4px 8px)}
.px[data-px=off],.px[data-px=twinkle]{background-image:none}
.px[data-load]{background-image:repeating-linear-gradient(90deg,transparent 0 4px,var(--field) 4px 8px),
repeating-linear-gradient(0deg,color-mix(in srgb,var(--px,var(--ok)) calc(6% + var(--l,1)*24%),var(--field)) 0 4px,transparent 4px 8px)}
.px[data-px=fill]{background-image:linear-gradient(90deg,color-mix(in srgb,var(--px,var(--ok)) 62%,var(--field)) 0 calc(var(--p,0)*100%),transparent 0)}
.px-held{position:relative}
.px-held::after{content:"";position:absolute;inset:-1px;pointer-events:none;
background-image:repeating-linear-gradient(90deg,var(--warn) 0 8px,transparent 8px 16px),
repeating-linear-gradient(90deg,var(--warn) 0 8px,transparent 8px 16px),
repeating-linear-gradient(180deg,var(--warn) 0 8px,transparent 8px 16px),
repeating-linear-gradient(180deg,var(--warn) 0 8px,transparent 8px 16px);
background-size:100% 2px,100% 2px,2px 100%,2px 100%;background-repeat:repeat-x,repeat-x,repeat-y,repeat-y;
background-position:0 0,0 100%,100% 0,0 0;animation:px-march 2s steps(4) infinite}
"""


SCRIPT = r"""
  // --- every page ---------------------------------------------------------

  // The lanes the page draws: devices with their running jobs and the slots
  // in use, plus grok wakes. Mirrors lane_devices in lib/dashboard_page.py.
  function laneDevices(d) {
    var slots = d.slots || {};
    var box = slots.box || {};
    var devices = (slots.devices || []).map(function (x) {
      return {name: x.name, slot_max: x.slot_max, held: x.held || 0, enabled: x.enabled,
              placeholder: x.placeholder, harnesses: x.harnesses || [], listed: true, jobs: []};
    });
    var byName = {};
    devices.forEach(function (x) { byName[x.name] = x; });
    var wakes = [];
    (d.running || []).forEach(function (r) {
      if (r.device === null || r.device === undefined) { wakes.push(r); return; }
      if (!Object.prototype.hasOwnProperty.call(byName, r.device)) {
        var dev = {name: r.device, slot_max: r.device === 'box' ? box.slot_max : null, held: 0, enabled: true,
                   placeholder: false, harnesses: [], listed: r.device === 'box' && !devices.length, jobs: []};
        devices.push(dev);
        byName[r.device] = dev;
      }
      byName[r.device].jobs.push(r);
    });
    if (!devices.length) {
      devices.push({name: 'box', slot_max: box.slot_max, held: 0, enabled: true, placeholder: false,
                    harnesses: [], listed: true, jobs: []});
    }
    // #63 FU-1: a slot held in device_slots is in use even when its job is
    // not (or no longer) running, so a lane counts max(held, running).
    devices.forEach(function (dev) { dev.used = Math.max(dev.held, dev.jobs.length); });
    return {devices: devices, wakes: wakes};
  }

  // Running / total slots over the devices that can take work (header_load).
  function headerLoad(d) {
    var used = 0;
    var total = 0;
    laneDevices(d).devices.forEach(function (dev) {
      if (dev.enabled && !dev.placeholder && dev.slot_max) { used += dev.used; total += dev.slot_max; }
    });
    return total ? Math.max(0, Math.min(1, used / total)) : 0;
  }

  function renderBand(d) {
    var root = byId('band');
    if (root) root.replaceChildren(PX.strip('band', {state: 'band', load: headerLoad(d), height: 24}));
  }

  function attentionItems(d) {
    var a = d.attention || {items: []};
    return (a.items || []).concat(agedHealth(d).extra);
  }

  // The attention strip (#139): every item the count covers, hidden when clear.
  function renderAttentionStrip(d) {
    var root = byId('attention-strip');
    if (!root) return;
    var items = attentionItems(d);
    root.replaceChildren();
    root.hidden = !items.length;
    if (!items.length) return;
    var head = add(root, el('div', 'attn-head'));
    var blk = add(head, el('span', 'attn-blk'));
    blk.setAttribute('aria-hidden', 'true');
    add(blk, PX.strip('attention', {state: 'attention', height: 16}));
    var count = add(head, link('attn-count', 'activity.html#attention'));
    add(count, el('b', null, items.length)).setAttribute('id', 'attention-count');
    add(count, el('span', null, items.length === 1 ? ' needs attention' : ' need attention'));
    items.forEach(function (x) {
      var row = add(root, el('div', 'attn-item'));
      add(row, chip(x.kind, ATTENTION[x.kind] || 'muted'));
      add(row, link('attn-who', x.component ? healthLink(x.link) : 'job.html')).textContent =
        x.component ? x.component : ticketOf(x);
      add(row, el('span', 'attn-what', x.detail));
    });
  }

  // "behind": configured code components not on the checkout's version
  // (lib/health.py stale_code). Mirrors version_drift.
  function versionDrift(h) {
    h = h || {};
    var rows = (h.components || []).filter(function (c) {
      return c.configured !== false && SHELL.code.indexOf(c.component) >= 0;
    });
    return {
      newest: String(h.code_version || 'unknown'),
      behind: rows.filter(function (c) { return !!c.stale_code; }).map(function (c) { return c.label; }),
      unknown: rows.filter(function (c) { return c.version === 'unknown'; }).map(function (c) { return c.label; })
    };
  }

  function segment(key, text, state, label, age, prefix, href) {
    return {key: key, label: label === undefined ? null : label, text: text,
            state: state === undefined ? null : state,
            href: href || (Object.prototype.hasOwnProperty.call(SHELL.links, key) ? SHELL.links[key] : null),
            age: age === undefined ? null : age, prefix: prefix === undefined ? null : prefix};
  }
  function present(v) { return v !== null && v !== undefined; }

  // The status line's segments, each from the snapshot. Mirrors status_segments.
  function statusSegments(d) {
    var out = [];
    laneDevices(d).devices.forEach(function (dev) {
      var value = !dev.enabled || dev.placeholder ? 'off'
        : dev.used + '/' + (present(dev.slot_max) ? dev.slot_max : '—');
      out.push(segment('device:' + dev.name, value, null, dev.name));
    });
    out.push(segment('queue', num(((d.queue || {}).totals || {}).queued || 0), null, 'queue'));
    var p = d.poller || {};
    var h = p.health || {state: 'unknown'};
    var ps = POLL_STATE[h.state] || POLL_STATE.unknown;
    var poll = 'poll ' + ps[0].toLowerCase();
    var cycle = h.last_cycle_age_seconds;
    if (present(cycle)) out.push(segment('poll', poll + ' · ' + dur(cycle), ps[1], null, cycle, poll + ' · '));
    else out.push(segment('poll', poll, ps[1]));
    var errors = (p.errors || {}).last_hour || 0;
    out.push(segment('poll-errors', num(errors) + ' poll error' + (errors === 1 ? '' : 's') + ' / 1 h', errors ? 'bad' : 'ok'));
    var b = p.linear_budget || {state: 'ok'};
    var bs = BUDGET_STATE[b.state] || ['OTHER', 'muted'];
    out.push(segment('linear', 'linear ' + bs[0].toLowerCase(), bs[1]));
    var w = p.linear_webhook;
    if (!w) {
      out.push(segment('webhook', 'no webhook', 'muted', undefined, undefined, undefined, SHELL.absent));
    } else if (!present(w.last_received_age_seconds)) {
      out.push(segment('webhook', 'webhook never', present(w.warning) ? 'bad' : 'warn'));
    } else {
      out.push(segment('webhook', 'webhook ' + dur(w.last_received_age_seconds), present(w.warning) ? 'bad' : 'ok',
                       null, w.last_received_age_seconds, 'webhook '));
    }
    var f = d.feeder;
    if (!f) out.push(segment('feeder', 'no feeder', 'muted', undefined, undefined, undefined, SHELL.absent));
    else {
      var failing = (f.items || []).some(function (x) { return x.state === 'error'; });
      out.push(segment('feeder', 'feeder ' + num(f.fed) + ' fed', failing ? 'bad' : 'ok'));
    }
    var hs = d.health || {};
    if (!hs.known) {
      out.push(segment('components', 'components unknown', hs.error ? 'bad' : 'muted'));
    } else {
      var rows = agedHealth(d).rows.filter(function (c) { return c.configured !== false; });
      var up = rows.filter(function (c) { return c.state === 'up'; }).length;
      var down = rows.some(function (c) { return c.state === 'down'; });
      out.push(segment('components', up + ' / ' + rows.length + ' up', down ? 'bad' : up < rows.length ? 'warn' : 'ok'));
    }
    var drift = versionDrift(hs);
    if (drift.newest === 'unknown') out.push(segment('version', 'version unknown', 'warn'));
    else {
      out.push(segment('version', drift.newest.slice(0, 7) + ' · ' + drift.behind.length + ' behind',
                       drift.behind.length ? 'warn' : 'ok'));
    }
    return out;
  }

  function renderStatusLine(d) {
    var root = byId('statusline');
    if (!root) return;
    root.replaceChildren();
    add(root, el('span', 'sl-brand', 'factory'));
    var filled = false;
    statusSegments(d).forEach(function (s) {
      if (s.state === null) {
        var plain = add(root, el('span', 'sl-seg' + (s.key === 'queue' ? ' sl-queue' : '')));
        plain.setAttribute('data-seg', s.key);
        add(plain, el('span', 'sl-k', s.label));
        add(plain, el('span', 'sl-v', s.text));
        return;
      }
      if (!filled) { add(root, el('span', 'sl-fill')); filled = true; }
      var a = add(root, link('sl-vital ' + s.state, s.href));
      a.setAttribute('data-seg', s.key);
      add(a, el('span', 'blk')).setAttribute('aria-hidden', 'true');
      var text = add(a, el('span', null, s.text));
      if (s.age !== null) ticking(text, s.age, s.prefix);
    });
  }

  // #92: a snapshot vouches for the states it observed until its next
  // publication is overdue (stale_beats publication intervals; a one-off
  // snapshot vouches for none). Past that, a component that is not still
  // inside its up window is unknown, not down: the page cannot see it. Only
  // the dashboard writer, whose beats are the publications, ages to stale
  // and down; with no writer row that can alert, the overdue snapshot is one
  // attention item.
  function agedHealth(d) {
    var h = d.health || {};
    var t = h.thresholds || {};
    var staleBeats = t.stale_beats || 3;
    var downBeats = t.down_beats || 8;
    var age = since();
    var vouched = age <= staleBeats * (Number(h.publish_seconds) || 0);
    var extra = [];
    var writer = false;
    var unknown = false;
    var waiterUnknown = false;
    var rows = (h.components || []).map(function (c) {
      var isWriter = c.component === 'dashboard';
      writer = writer || (isWriter && c.configured !== false);
      var base = c.last_seen_age_seconds;
      if (vouched || c.state === 'down' || base === null || base === undefined) return c;
      var seen = base + age;
      var interval = Math.max(Number(c.interval_seconds) || 15, 1);
      var state = seen > downBeats * interval ? 'down' : seen > staleBeats * interval ? 'stale' : c.state;
      if (!isWriter && (state !== 'up' || c.state !== 'up')) {
        unknown = true;
        waiterUnknown = waiterUnknown || c.component === 'waiter';
        return Object.assign({}, c, {state: 'unknown', reason: 'no snapshot for ' + dur(age)});
      }
      if (state === c.state) return c;
      var reason = 'no heartbeat for ' + dur(seen);
      if (state === 'down' && c.configured !== false) {
        extra.push({kind: 'down', repo: '', ticket_id: '', component: c.label,
                    link: 'health.html#' + c.anchor, detail: 'down (' + reason + ')'});
      }
      return Object.assign({}, c, {state: state, reason: reason});
    });
    if (unknown && !writer) {
      extra.push({kind: 'health', repo: '', ticket_id: '', component: 'health snapshot',
                  link: 'health.html#components', detail: 'unknown: no snapshot for ' + dur(age)});
    }
    return {rows: rows, extra: extra, waiterUnknown: waiterUnknown, age: age,
            key: rows.map(function (c) { return c.state; }).join(',')};
  }
"""


# The PixelStrip engine (#138, docs/design/mission-control/SPEC.md section 6):
# the drawing methods of reference/artboards/PixelStrip.dc.html, copied onto
# Strip.prototype, under one shared requestAnimationFrame loop. These lines
# differ from the artboard: decay's amount follows the strip's progress
# (heartbeat age / timeout) instead of a timer, and decay is therefore still;
# a caret with rate 0 is still too (#139: the lost link is static), and a
# still caret is always lit, so a 0.4 Hz caret never freezes dark.
PX_SCRIPT = r"""
(function () {
  var TABLE = __PX__;
  var FPS = 20;
  var DATA = ['progress', 'load', 'seed', 'cell', 'speed', 'used', 'slots', 'rate', 'done', 'burst'];
  var STRIPS = new Map();
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
    this._doneAt = this._bool(this.props.done) ? -1000 : null;
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
    if (live) start();
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
    if (opts.cell !== null && opts.cell !== undefined) el.setAttribute('data-cell', String(Math.max(2, Math.min(16, Math.floor(Number(opts.cell) || 4)))));
    if (opts.done) el.setAttribute('data-done', 'true');
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

  return {bind: bind, state: state, strip: strip, jobStrip: jobStrip, frame: frame, strips: function () { return STRIPS; }, Strip: Strip, table: TABLE};
})()
"""


def px_script() -> str:
    return PX_SCRIPT.strip().replace("__PX__", json.dumps(px_table(), separators=(",", ":")))


# --- server-side rendering ----------------------------------------------------------
#
# Each page's panels are rendered on the server with the same markup the script
# builds, so a page reads the same without script (it then reloads every
# refresh_seconds) and the script only has to replace their contents.

# In nav order (#78).
PAGES = ("index.html", "job.html", "activity.html", "efficiency.html", "health.html")


_PAGE_NAMES = {
    "index.html": ("main", "Lanes"),
    "job.html": ("job", "Tickets"),
    "activity.html": ("activity", "Activity"),
    "efficiency.html": ("efficiency", "Efficiency"),
    "health.html": ("health", "System"),
}


# --- PixelStrip (#138, SPEC section 6) ----------------------------------------------
#
# One table maps a state to its strip effect and tint; the page script gets
# the same table (px_table) so the server and the script cannot drift. An
# effect is animated when it changes from frame to frame.
PX_EFFECTS = {
    "field": True,
    "fill": True,
    "scan": True,
    "patch": True,
    "spin3": True,
    "decay": False,
    "static": False,
    "twinkle": True,
    "conveyor": True,
    "meter": True,
    "caret": True,
    "off": False,
}


PX_ONESHOTS = {"beat": 600, "burst": 450, "done": 600}


PX_BOT_TINTS = {"claude": "#e0af68", "codex": "#7aa2f7", "cursor": "#bb9af7", "grok": "#73daca"}


PX_FALLBACK = "#9ece6a"


_PX_BAD = "#f7768e"


PX_BROKEN = ("dead", "overdue", "stuck", "blocked", "capped", "refused", "no_waiter", "down")


_PX_LINK = {"live": ("#9ece6a", 1), "stale": ("#ff9e64", 0.4), "lost": (_PX_BAD, 0), "boot": ("#8b93b8", 0)}


PX_STATE: dict[str, dict[str, str | float]] = {
    "plan": {"effect": "field", "tint": "bot"},
    "build": {"effect": "fill", "tint": "bot"},
    "review": {"effect": "scan", "tint": "bot"},
    "fix": {"effect": "patch", "tint": "bot"},
    "claimed": {"effect": "spin3", "tint": "bot"},
    "stale": {"effect": "decay", "tint": "bot"},
    **{name: {"effect": "static", "tint": _PX_BAD} for name in PX_BROKEN},
    "idle": {"effect": "twinkle", "tint": PX_FALLBACK},
    "held": {"effect": "off", "tint": PX_FALLBACK, "frame": "px-held"},
    "unknown": {"effect": "off", "tint": PX_FALLBACK},
    # #139: the brand caret per link state (rate 1 blinks at 1 Hz, 0 is solid).
    **{f"link-{name}": {"effect": "caret", "tint": tint, "rate": rate} for name, (tint, rate) in _PX_LINK.items()},
    "band": {"effect": "field", "tint": PX_FALLBACK, "seed": 21},
    "attention": {"effect": "static", "tint": _PX_BAD, "seed": 5},
}


PX_ACTIONS = {"plan": "plan", "build": "build", "review": "review", "fix": "fix"}


_PX_HEX = re.compile(r"#[0-9a-f]{6}")


def px_table() -> dict:
    return {
        "effects": PX_EFFECTS,
        "oneshots": PX_ONESHOTS,
        "states": PX_STATE,
        "actions": PX_ACTIONS,
        "bots": PX_BOT_TINTS,
        "fallback": PX_FALLBACK,
    }


def px_state(row: dict) -> str:
    """The PX_STATE key for a running job row of the snapshot. Broken rows win
    over the action, and anything the table does not know is "unknown" (still)."""
    job_state = row.get("job_state")
    if job_state == "dead":
        return "dead"
    if row.get("overdue"):
        return "overdue"
    if job_state in ("stale", "claimed"):
        return str(job_state)
    if job_state == "running" and row.get("action") in PX_ACTIONS:
        return PX_ACTIONS[row["action"]]
    return "unknown"


def _px_progress(state: str, row: dict) -> float | None:
    timeout = row.get("timeout_seconds") or 1
    if state == "build":
        return (row.get("elapsed_seconds") or 0) / timeout
    if state == "stale":
        return (row.get("heartbeat_age_seconds") or 0) / timeout
    return None


def px_frame(state: str) -> str:
    """The class for the box around a strip in this state ("px-held": the
    marching dashed border), or ""."""
    return PX_STATE.get(state, {}).get("frame", "")


def _fixed3(value: float) -> str:
    """0..1 to three places, rounded half up like the script's toFixed(3)."""
    return str(Decimal(max(0.0, min(1.0, float(value)))).quantize(Decimal("0.001"), ROUND_HALF_UP))


def px_strip(
    key: object,
    *,
    state: str = "unknown",
    effect: str | None = None,
    tint: str | None = None,
    bot: object = None,
    progress: float | None = None,
    heartbeat: float | None = None,
    load: float | None = None,
    rate: float | None = None,
    seed: float | None = None,
    cell: int | None = None,
    done: bool = False,
    height: int = 16,
) -> str:
    """A PixelStrip canvas (the script draws it; without script its CSS
    gradients are the still frame). `state` picks the effect, tint, rate and
    seed from PX_STATE; the keywords override them. Same markup as PX.strip."""
    entry = PX_STATE.get(state, PX_STATE["unknown"])
    effect = str(effect or entry["effect"])
    tint = tint or entry["tint"]
    rate = entry.get("rate") if rate is None else rate
    seed = entry.get("seed") if seed is None else seed
    if tint == "bot":
        tint = str(bot) if bot in PX_BOT_TINTS else PX_FALLBACK
    if tint not in PX_BOT_TINTS and not _PX_HEX.fullmatch(str(tint)):
        tint = PX_FALLBACK
    height = max(2, min(96, round(height)))
    attrs = [
        ("class", "px"),
        ("data-px", effect if effect in PX_EFFECTS else "off"),
        ("data-tint", tint),
    ]
    style = f"--px:{f'var(--{tint})' if tint in PX_BOT_TINTS else tint};"
    if progress is not None:
        value = _fixed3(progress)
        attrs.append(("data-progress", value))
        style += f"--p:{value};"
    if heartbeat is not None:
        attrs.append(("data-hb", str(max(0, int(heartbeat)))))
    if load is not None:
        value = _fixed3(load)
        attrs.append(("data-load", value))
        style += f"--l:{value};"
    if rate is not None:
        attrs.append(("data-rate", f"{float(rate):g}"))
    if seed is not None:
        attrs.append(("data-seed", f"{float(seed):g}"))
    if cell is not None:
        attrs.append(("data-cell", str(max(2, min(16, int(cell))))))
    if done:
        attrs.append(("data-done", "true"))
    attrs += [("data-px-key", str(key)), ("aria-hidden", "true"), ("style", f"{style}height:{height}px")]
    return "<canvas " + " ".join(f'{name}="{_e(value)}"' for name, value in attrs) + "></canvas>"


def px_job_strip(row: dict, height: int = 16) -> str:
    """The strip of a running job row, keyed by its job id (PX.jobStrip)."""
    state = px_state(row)
    return px_strip(
        f"job-{row.get('job')}",
        state=state,
        bot=row.get("recipient"),
        progress=_px_progress(state, row),
        heartbeat=None if state == "claimed" else row.get("heartbeat_age_seconds"),
        height=height,
    )


# --- the shell on every page (#139) -----------------------------------------------------
#
# The TopBar (brand and its caret, nav, link state, snapshot meta), the header
# band, the attention strip and the status line. Each has a script twin in
# SCRIPT that builds the same markup from status.json.


def _topbar(snapshot: dict, page: str) -> str:
    current = ' aria-current="page"'
    links = "".join(
        f'<a href="{name}"{current if name == page else ""}>{label}</a>'
        for name, (_, label) in _PAGE_NAMES.items()
    )
    return (
        '<header class="topbar" id="topbar"><div class="top-row">'
        '<div class="brand"><span>Factory <b>Mission Control</b></span>'
        f'<span class="caret" id="caret" aria-hidden="true">{px_strip("caret", state="link-boot", height=12)}'
        "</span></div>"
        f'<nav class="nav" id="nav" aria-label="Pages">{links}</nav>'
        '<div class="link boot" id="link" role="status" aria-live="polite">'
        '<span class="dot" aria-hidden="true"></span>'
        '<span id="link-text">STATIC SNAPSHOT</span></div>'
        f'<div class="meta">snapshot <span id="age">{_e(snapshot["generated_at"])}</span>'
        ' · <span id="clock"></span> · read-only, redacted</div>'
        "</div>"
        f'<div class="band" id="band">{_band(snapshot)}</div>'
        f"{_attention_strip(snapshot)}"
        "</header>"
    )


def header_load(snapshot: dict) -> float:
    """Running / total slots over the enabled, non-placeholder devices that
    have a slot_max (the header band's brightness), 0 with no slots."""
    devices, _ = lane_devices(snapshot)
    used = total = 0
    for device in devices:
        if device["enabled"] and not device["placeholder"] and device["slot_max"]:
            used += device["used"]
            total += device["slot_max"]
    return max(0.0, min(1.0, used / total)) if total else 0.0


def _band(snapshot: dict) -> str:
    return px_strip("band", state="band", load=header_load(snapshot), height=24)


def _attention_strip(snapshot: dict) -> str:
    """Every item behind the attention count, inline with its chip and link;
    empty and hidden when there is none."""
    items = (snapshot.get("attention") or {}).get("items") or []
    if not items:
        return '<div class="attn" id="attention-strip" hidden></div>'

    def item(row: dict) -> str:
        if row.get("component"):
            href, who = _health_link(row.get("link")), row["component"]
        else:
            href, who = "job.html", _ticket(row)
        return (
            '<div class="attn-item">'
            + _chip(row["kind"], _ATTENTION.get(row["kind"], "muted"))
            + f'<a class="attn-who" href="{_e(href)}">{_e(who)}</a>'
            + _span("attn-what", row["detail"])
            + "</div>"
        )

    need = " needs attention" if len(items) == 1 else " need attention"
    return (
        '<div class="attn" id="attention-strip">'
        '<div class="attn-head"><span class="attn-blk" aria-hidden="true">'
        f'{px_strip("attention", state="attention", height=16)}</span>'
        '<a class="attn-count" href="activity.html#attention">'
        f'<b id="attention-count">{len(items)}</b>{_span(None, need)}</a></div>'
        + "".join(item(row) for row in items)
        + "</div>"
    )


# Where each status-line segment links (System anchors or Lanes).
STATUS_LINKS = {
    "poll": "health.html#health",
    "poll-errors": "health.html#health",
    "linear": "health.html#health",
    "webhook": "index.html#linear-webhook",
    "feeder": "index.html#feeder",
    "components": "health.html#components",
    "version": "health.html#components",
}


# A webhook or feeder segment with no block of its own links to the panel that would hold it.
STATUS_ABSENT_LINK = "index.html#kpis"


def shell_table() -> dict:
    """What the page script's shell needs from here (SHELL in SCRIPT)."""
    return {"links": STATUS_LINKS, "absent": STATUS_ABSENT_LINK, "code": sorted(CODE_COMPONENTS)}


def version_drift(health: dict | None) -> dict:
    """newest: the checkout's version; behind: configured code components on
    another known version (lib/health.py stale_code); unknown: configured code
    components whose version is unknown, not counted as behind."""
    health = health or {}
    rows = [
        row for row in health.get("components") or []
        if row.get("configured") is not False and row.get("component") in CODE_COMPONENTS
    ]
    return {
        "newest": str(health.get("code_version") or "unknown"),
        "behind": [row["label"] for row in rows if row.get("stale_code")],
        "unknown": [row["label"] for row in rows if row.get("version") == "unknown"],
    }


def _segment(key: str, text: str, state: str | None = None, label: str | None = None,
             age: object = None, prefix: str | None = None, href: str | None = None) -> dict:
    return {"key": key, "label": label, "text": text, "state": state,
            "href": href or STATUS_LINKS.get(key), "age": age, "prefix": prefix}


def _count(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


def status_segments(snapshot: dict) -> list[dict]:
    """The status line, one segment per figure, each computed from the
    snapshot: devices and queue (plain), then the vitals with a state
    (ok, warn, bad, muted) and a link. Mirrors statusSegments in SCRIPT."""
    out = []
    devices, _ = lane_devices(snapshot)
    for device in devices:
        if not device["enabled"] or device["placeholder"]:
            value = "off"
        else:
            value = f'{device["used"]}/{"—" if device["slot_max"] is None else device["slot_max"]}'
        out.append(_segment(f'device:{device["name"]}', value, label=str(device["name"])))
    out.append(_segment("queue", _count(snapshot["queue"]["totals"]["queued"]), label="queue"))
    poller = snapshot["poller"]
    health = poller.get("health") or {"state": "unknown"}
    label, cls = _POLL_STATE.get(health["state"], _POLL_STATE["unknown"])
    poll = f"poll {label.lower()}"
    cycle = health.get("last_cycle_age_seconds")
    if cycle is None:
        out.append(_segment("poll", poll, cls))
    else:
        out.append(_segment("poll", f"{poll} · {duration(cycle)}", cls, age=cycle, prefix=f"{poll} · "))
    errors = (poller.get("errors") or {}).get("last_hour") or 0
    plural = "" if errors == 1 else "s"
    out.append(_segment("poll-errors", f"{_count(errors)} poll error{plural} / 1 h", "bad" if errors else "ok"))
    budget = poller.get("linear_budget") or {"state": "ok"}
    blabel, bcls = _BUDGET_STATE.get(budget["state"], ("OTHER", "muted"))
    out.append(_segment("linear", f"linear {blabel.lower()}", bcls))
    webhook = poller.get("linear_webhook")
    if not webhook:
        out.append(_segment("webhook", "no webhook", "muted", href=STATUS_ABSENT_LINK))
    elif webhook.get("last_received_age_seconds") is None:
        out.append(_segment("webhook", "webhook never",
                            "bad" if webhook.get("warning") is not None else "warn"))
    else:
        age = webhook["last_received_age_seconds"]
        out.append(_segment("webhook", f"webhook {duration(age)}",
                            "bad" if webhook.get("warning") is not None else "ok",
                            age=age, prefix="webhook "))
    feeder = snapshot.get("feeder")
    if not feeder:
        out.append(_segment("feeder", "no feeder", "muted", href=STATUS_ABSENT_LINK))
    else:
        failing = any(item.get("state") == "error" for item in feeder.get("items") or [])
        out.append(_segment("feeder", f"feeder {_count(feeder['fed'])} fed", "bad" if failing else "ok"))
    components = snapshot.get("health") or {}
    if not components.get("known"):
        out.append(_segment("components", "components unknown",
                            "bad" if components.get("error") else "muted"))
    else:
        rows = [row for row in components.get("components") or [] if row.get("configured") is not False]
        up = sum(1 for row in rows if row["state"] == "up")
        down = any(row["state"] == "down" for row in rows)
        out.append(_segment("components", f"{up} / {len(rows)} up",
                            "bad" if down else "warn" if up < len(rows) else "ok"))
    drift = version_drift(components)
    if drift["newest"] == "unknown":
        out.append(_segment("version", "version unknown", "warn"))
    else:
        behind = len(drift["behind"])
        out.append(_segment("version", f'{drift["newest"][:7]} · {behind} behind',
                            "warn" if behind else "ok"))
    return out


def _statusline(snapshot: dict) -> str:
    parts = ['<span class="sl-brand">factory</span>']
    filled = False
    for seg in status_segments(snapshot):
        if seg["state"] is None:
            cls = "sl-seg sl-queue" if seg["key"] == "queue" else "sl-seg"
            parts.append(
                f'<span class="{cls}" data-seg="{_e(seg["key"])}">'
                f'{_span("sl-k", seg["label"])}{_span("sl-v", seg["text"])}</span>'
            )
            continue
        if not filled:
            parts.append('<span class="sl-fill"></span>')
            filled = True
        parts.append(
            f'<a class="sl-vital {seg["state"]}" href="{_e(seg["href"])}" data-seg="{_e(seg["key"])}">'
            f'<span class="blk" aria-hidden="true"></span>{_span(None, seg["text"])}</a>'
        )
    return f'<footer class="statusline" id="statusline">{"".join(parts)}</footer>'


_PANELS = {
    "index.html": (_kpis, _lanes, _queue),
    "activity.html": (_attention, _handoffs, _verdicts),
    "efficiency.html": (savings_row, rounds_row, _headroom, rollup_panel),
    "health.html": (_components, _drift_row, _log_row, _no_waiter),
    "job.html": (_board, _tickets, _jobview),
}


def _page(snapshot: dict, name: str, *, css: str, script: str) -> str:
    page, label = _PAGE_NAMES[name]
    refresh = int(snapshot["refresh_seconds"])
    panels = "".join(draw(snapshot) for draw in _PANELS[name])
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            # Without script the page reloads itself; with script it polls
            # status.json and never reloads.
            f'<noscript><meta http-equiv="refresh" content="{refresh}"></noscript>',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<meta name="robots" content="noindex">',
            '<meta name="color-scheme" content="dark">',
            f"<title>factory mission control · {label.lower()}</title>",
            f"<style>{css}{TELEMETRY_CSS}</style></head>",
            f'<body data-page="{page}" '
            f'data-generated="{float(snapshot["generated_at_epoch"]):.3f}" '
            f'data-refresh="{refresh}">',
            _topbar(snapshot, name),
            f'<main id="hud">{panels}</main>',
            _statusline(snapshot),
            f"<script>{script}</script>",
            "</body></html>",
        ]
    ) + "\n"
