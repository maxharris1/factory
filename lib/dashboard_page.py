"""The mission-control page: inline CSS, the HUD skeleton and its script.

`lib.dashboard.render_html` puts these around the server-rendered tables.
Everything is inline (no external assets, nothing from a CDN), so the page
works offline on the tailnet and under the static server's CSP. The script
fetches the relative `status.json` every POLL_MS (1 to 2 seconds) and
rebuilds the panels in place with createElement/textContent only, never
HTML strings, so no value from the snapshot is ever parsed as markup. If
status.json cannot be fetched at all, the tables under the HUD stay and the
page reloads every refresh_seconds, like the old page.
"""

from __future__ import annotations

import html

POLL_MS = 1500

CSS = """
:root{color-scheme:dark;
--bg:#050a12;--panel:#0b1522;--panel-hi:#10202f;--dim:#0e131a;--line:#1f3b55;
--text:#e6f4ff;--muted:#9fb6c9;--accent:#3fe0ff;--ok:#4ade80;--warn:#fbbf24;--bad:#ff8a8a;
--claude:#f6b48f;--codex:#7dd3fc;--cursor:#c9b8ff;--grok:#f9a8d4;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;
background:radial-gradient(ellipse at 50% -20%,#0f2740 0%,var(--bg) 60%) fixed;min-height:100vh}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
background:repeating-linear-gradient(0deg,rgba(63,224,255,.035) 0 1px,transparent 1px 32px),
repeating-linear-gradient(90deg,rgba(63,224,255,.035) 0 1px,transparent 1px 32px)}
.bar{position:sticky;top:0;z-index:2;display:flex;flex-wrap:wrap;align-items:center;gap:8px 22px;
padding:10px 18px;border-bottom:1px solid var(--line);overflow:hidden;
background:linear-gradient(180deg,rgba(16,32,47,.97),rgba(5,10,18,.97))}
.bar::after{content:"";position:absolute;top:0;bottom:0;left:-30%;width:30%;pointer-events:none;
background:linear-gradient(90deg,transparent,rgba(63,224,255,.13),transparent);animation:sweep 7s linear infinite}
.brand{font:700 15px/1 var(--mono);letter-spacing:.22em;text-transform:uppercase;color:var(--text)}
.brand b{color:var(--accent)}
.link{display:flex;align-items:center;gap:8px;font:600 12px/1 var(--mono);letter-spacing:.16em}
.link .dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}
.link.live{color:var(--ok)}.link.live .dot{background:var(--ok);animation:pulse 1.6s ease-out infinite}
.link.stale{color:var(--warn)}.link.stale .dot{background:var(--warn)}
.link.lost{color:var(--bad)}.link.lost .dot{background:var(--bad);animation:pulse 1s ease-out infinite}
.link.boot{color:var(--muted)}
.bar .meta{margin-left:auto;color:var(--muted);font:12px/1.3 var(--mono)}
.bar .meta span{color:var(--text)}
main{position:relative;z-index:1;display:grid;grid-template-columns:repeat(12,minmax(0,1fr));
gap:14px;padding:16px 18px 28px}
.panel{grid-column:span 4;position:relative;min-width:0;padding:12px 14px 14px;border:1px solid var(--line);
border-radius:10px;background:linear-gradient(180deg,var(--panel-hi),var(--panel))}
.panel::before,.panel::after{content:"";position:absolute;width:14px;height:14px;border:2px solid var(--accent);
opacity:.8;pointer-events:none}
.panel::before{top:-1px;left:-1px;border-right:0;border-bottom:0;border-top-left-radius:10px}
.panel::after{bottom:-1px;right:-1px;border-left:0;border-top:0;border-bottom-right-radius:10px}
#kpis,#tickets,#log{grid-column:1/-1}#lanes,#attention{grid-column:span 8}
.panel h2{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin:0 0 10px;
font:700 12px/1.2 var(--mono);letter-spacing:.2em;text-transform:uppercase;color:var(--accent)}
.panel h2 .count{color:var(--muted);letter-spacing:.08em}
.kpis{display:flex;flex-wrap:wrap;gap:10px}
.kpi{flex:1 1 120px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.kpi b{display:block;font:600 28px/1.15 var(--mono);color:var(--text)}
.kpi span{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.kpi.accent b{color:var(--accent)}.kpi.ok b{color:var(--ok)}.kpi.warn b{color:var(--warn)}.kpi.bad b{color:var(--bad)}
.devices{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.device{padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.device.disabled{border-style:dashed;color:var(--muted);
background:repeating-linear-gradient(135deg,var(--dim) 0 10px,var(--panel) 10px 20px)}
.device.disabled .name{color:var(--muted)}
.device header{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.device .name{font:700 15px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase}
.device .cap{margin-left:auto;font:12px/1 var(--mono);color:var(--muted)}
.device .harness{margin:0 0 8px;font-size:12px;color:var(--muted)}
.slots{display:grid;gap:6px}
.slot{min-height:54px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel-hi)}
.slot.idle{display:flex;align-items:center;justify-content:center;border-style:dashed;background:transparent;
font:600 11px/1 var(--mono);letter-spacing:.24em;text-transform:uppercase;color:var(--muted)}
.job .top{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px}
.job .ticket{font:600 13px/1.2 var(--mono);color:var(--text);overflow-wrap:anywhere}
.job .action{font-size:12px;color:var(--muted)}
.job .times{display:flex;justify-content:space-between;gap:8px;margin:4px 0 5px;font:12px/1.2 var(--mono);color:var(--muted)}
.job .times b{font-weight:600;color:var(--text)}
.job.overdue{border-color:var(--bad);box-shadow:inset 0 0 0 1px var(--bad)}
.hb{height:4px;border-radius:2px;background:var(--line);overflow:hidden}
.hb i{display:block;height:100%;width:0;background-color:var(--ok);
background-image:linear-gradient(90deg,transparent 0,rgba(255,255,255,.45) 50%,transparent 100%);
background-size:200% 100%;animation:shimmer 2.2s linear infinite;transition:width .9s linear}
.hb.warn i{background-color:var(--warn)}.hb.bad i{background-color:var(--bad)}
.bot{display:inline-block;padding:0 5px;border:1px solid currentColor;border-radius:4px;
font:700 11px/1.5 var(--mono);letter-spacing:.1em;text-transform:uppercase}
.bot.claude{color:var(--claude)}.bot.codex{color:var(--codex)}.bot.cursor{color:var(--cursor)}
.bot.grok{color:var(--grok)}.bot.other{color:var(--muted)}
.chip{display:inline-block;padding:1px 7px;border:1px solid currentColor;border-radius:999px;
font:700 11px/1.4 var(--mono);letter-spacing:.1em;text-transform:uppercase;white-space:nowrap}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.accent{color:var(--accent)}.muted{color:var(--muted)}
.feed{margin:0;padding:0;list-style:none}
.feed li{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;padding:6px 4px;border-bottom:1px solid var(--line);border-radius:4px}
.feed li:last-child{border-bottom:0}
.feed .ticket{font:600 13px/1.2 var(--mono)}.feed .what{color:var(--muted);font-size:12px}
.feed .when{margin-left:auto;font:12px/1.2 var(--mono);color:var(--muted)}
.more{margin:6px 0 0;font-size:12px;color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin:8px 0}
.stat{padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel)}
.stat b{display:block;font:600 18px/1.2 var(--mono)}.stat span{font-size:12px;color:var(--muted)}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:4px 0}
.row .label{min-width:92px;font:700 12px/1.2 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.gauge{display:flex;align-items:center;gap:16px}
.ring{--p:0;display:grid;flex:none;place-items:center;width:100px;height:100px;border-radius:50%;
background:conic-gradient(var(--accent) calc(var(--p) * 1%),var(--line) 0)}
.ring span{display:grid;place-items:center;width:76px;height:76px;border-radius:50%;background:var(--panel);
font:700 20px/1 var(--mono);color:var(--text)}
.meter{display:flex;height:8px;margin:8px 0;border-radius:4px;background:var(--line);overflow:hidden}
.meter i{display:block;height:100%;transition:width .6s ease}
.meter .scripted{background:var(--accent)}.meter .bots{background:var(--grok)}
.empty{margin:4px 0;color:var(--muted)}
.fresh{animation:flash 2.4s ease-out}
details summary{cursor:pointer;color:var(--muted);font:600 12px/1.6 var(--mono);letter-spacing:.12em;text-transform:uppercase}
details summary:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
pre{margin:8px 0 0;padding:8px;max-height:340px;overflow:auto;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--text);font:12px/1.4 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;margin:0 0 8px}
th,td{padding:4px 10px 4px 0;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font:600 12px/1.4 var(--mono);letter-spacing:.08em;text-transform:uppercase}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.bad{color:var(--bad);font-weight:600}
#static{position:relative;z-index:1;padding:8px 18px 28px;max-width:1200px}
#static h1{font:700 18px/1.3 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}
#static h2{margin:22px 0 8px;font:700 13px/1.3 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}
#static .tiles{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}
#static .tile{min-width:120px;padding:8px 12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--muted)}
#static .tile b{display:block;font:600 20px/1.2 var(--mono);color:var(--text)}
#static .meta{color:var(--muted)}#static .stale{color:var(--bad);font-weight:600}
[hidden]{display:none !important}
@keyframes sweep{to{left:100%}}
@keyframes pulse{0%{box-shadow:0 0 0 0 currentColor}100%{box-shadow:0 0 0 9px transparent}}
@keyframes shimmer{from{background-position:100% 0}to{background-position:-100% 0}}
@keyframes flash{from{box-shadow:0 0 0 2px var(--accent),0 0 18px var(--accent)}to{box-shadow:0 0 0 0 transparent}}
@media (max-width:1100px){.panel,#lanes,#attention{grid-column:1/-1}}
@media (prefers-reduced-motion: reduce){
*,*::before,*::after{animation: none !important;transition: none !important;scroll-behavior:auto !important}
.fresh{outline:2px solid var(--accent);outline-offset:-2px}}
"""

_PANELS = (
    ("kpis", "Status"),
    ("lanes", "Lanes and slots"),
    ("health", "Poll and Linear budget"),
    ("queue", "Queue"),
    ("handoffs", "Recent handoffs"),
    ("verdicts", "Recent verdicts"),
    ("savings", "Handoff savings"),
    ("attention", "Needs attention"),
    ("tickets", "Tickets"),
    ("log", "poller.log"),
)

SCRIPT = r"""
(function () {
  'use strict';
  var POLL_MS = __POLL_MS__;
  var STATUS_URL = 'status.json';
  var FETCH_TIMEOUT_MS = 5000;
  var LINK_LOST_MS = 6000;
  var STALE_SECONDS = 180;
  var LIST_MAX = 12;
  var BOTS = {claude: 1, codex: 1, cursor: 1, grok: 1};
  var body = document.body;
  var refreshSeconds = Number(body.getAttribute('data-refresh')) || 20;
  var generated = Number(body.getAttribute('data-generated')) || 0;
  function byId(id) { return document.getElementById(id); }
  var hud = byId('hud');
  var fallback = byId('static');
  var lastOk = 0;
  var everOk = false;
  var failures = 0;
  var inflight = false;
  var timer = null;
  var reloadArmed = false;
  var firstRender = true;
  var tickers = [];
  var prevKeys = {};
  var nextKeys = {};
  var openDetails = {};

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function add(parent, child) { parent.appendChild(child); return child; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dur(s) {
    if (s === null || s === undefined || !isFinite(s)) return '—';
    s = Math.max(0, Math.floor(s));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + pad(s % 60) + 's';
    if (s < 86400) return Math.floor(s / 3600) + 'h ' + pad(Math.floor(s % 3600 / 60)) + 'm';
    return Math.floor(s / 86400) + 'd ' + Math.floor(s % 86400 / 3600) + 'h';
  }
  function num(n) {
    if (n === null || n === undefined) return '—';
    return typeof n === 'number' ? n.toLocaleString('en-US') : String(n);
  }
  function since() { return Math.max(0, Date.now() / 1000 - generated); }
  function short(repo) {
    var text = String(repo === null || repo === undefined ? '' : repo);
    var cut = text.lastIndexOf('/');
    return cut >= 0 ? text.slice(cut + 1) : text;
  }
  function ticketOf(row) { return short(row.repo) + '#' + row.ticket_id; }
  function bot(name) { return el('span', 'bot ' + (BOTS[name] ? name : 'other'), name); }
  function chip(text, cls) { return el('span', 'chip ' + (cls || 'muted'), text); }
  function ticking(node, base, prefix, suffix) {
    prefix = prefix || '';
    suffix = suffix || '';
    if (base === null || base === undefined) { node.textContent = prefix + '—'; return node; }
    var item = {run: function (d) { node.textContent = prefix + dur(base + d) + suffix; }};
    tickers.push(item);
    item.run(since());
    return node;
  }
  function mark(panel, key, node) {
    var keys = nextKeys[panel] || (nextKeys[panel] = {});
    keys[key] = true;
    if (!firstRender && !(prevKeys[panel] || {})[key]) node.classList.add('fresh');
    return node;
  }
  function panel(id, title, count) {
    var root = byId(id);
    if (!root) return null;
    root.replaceChildren();
    var head = add(root, el('h2'));
    add(head, el('span', null, title));
    if (count !== undefined && count !== null) add(head, el('span', 'count', count));
    return root;
  }
  function details(root, id, summary) {
    var box = add(root, el('details'));
    box.open = !!openDetails[id];
    if (box.addEventListener) box.addEventListener('toggle', function () { openDetails[id] = box.open; });
    add(box, el('summary', null, summary));
    return box;
  }
  function list(root, rows, emptyText, draw) {
    if (!rows || !rows.length) { add(root, el('p', 'empty', emptyText)); return; }
    var ul = add(root, el('ul', 'feed'));
    rows.slice(0, LIST_MAX).forEach(function (row) { add(ul, draw(row)); });
    if (rows.length > LIST_MAX) add(root, el('p', 'more', '+' + (rows.length - LIST_MAX) + ' more'));
  }

  function renderKpis(d) {
    var root = panel('kpis', 'Factory status');
    var t = d.queue.totals;
    var stuck = d.stuck.tickets.length + d.stuck.overdue_jobs.length;
    var share = d.handoff_savings.scripted_share;
    var row = add(root, el('div', 'kpis'));
    [
      ['running', t.running, 'accent'],
      ['queued', t.queued, t.queued ? 'warn' : ''],
      ['done', t.done, 'ok'],
      ['failed', t.failed, t.failed ? 'bad' : ''],
      ['stuck / overdue', stuck, stuck ? 'bad' : 'ok'],
      ['scripted share', share === null ? '—' : Math.round(share * 100) + '%', 'accent']
    ].forEach(function (k) {
      var tile = add(row, el('div', 'kpi ' + k[2]));
      add(tile, el('b', null, num(k[1])));
      add(tile, el('span', null, k[0]));
    });
  }

  function jobCard(r) {
    var card = el('div', 'slot job' + (r.overdue ? ' overdue' : ''));
    var top = add(card, el('div', 'top'));
    add(top, bot(r.recipient));
    var ticket = add(top, el('span', 'ticket', ticketOf(r)));
    ticket.title = r.repo + '#' + r.ticket_id + ' (job ' + r.job + ')';
    add(top, el('span', 'action', r.action));
    if (r.overdue) add(top, chip('OVERDUE', 'bad'));
    var times = add(card, el('div', 'times'));
    var elapsed = add(times, el('span'));
    elapsed.title = r.elapsed_from === 'placed' ? 'since it was placed on this device' : 'since it was queued';
    add(elapsed, el('span', null, 'T+ '));
    ticking(add(elapsed, el('b')), r.elapsed_seconds);
    var beat = add(times, el('span'));
    beat.title = 'last heartbeat';
    add(beat, el('span', null, 'hb '));
    ticking(add(beat, el('b')), r.heartbeat_age_seconds, '', ' ago');
    var bar = add(card, el('div', 'hb'));
    var fill = add(bar, el('i'));
    var base = r.heartbeat_age_seconds;
    var timeout = r.timeout_seconds || 1;
    var item = {run: function (dt) {
      var frac = Math.min(1, (base + dt) / timeout);
      fill.style.width = Math.max(2, Math.round(frac * 100)) + '%';
      bar.className = 'hb' + (frac >= 1 ? ' bad' : frac >= 0.6 ? ' warn' : '');
    }};
    tickers.push(item);
    item.run(since());
    return mark('lanes', r.job, card);
  }

  function renderLanes(d) {
    var running = d.running || [];
    var root = panel('lanes', 'Lanes and slots', running.length + ' running');
    var devices = (d.slots.devices || []).map(function (x) {
      return {name: x.name, slot_max: x.slot_max, enabled: x.enabled, placeholder: x.placeholder,
              harnesses: x.harnesses || [], listed: true};
    });
    var known = {};
    devices.forEach(function (x) { known[x.name] = true; });
    var lanes = {};
    var wakes = [];
    running.forEach(function (r) {
      if (r.device === null || r.device === undefined) { wakes.push(r); return; }
      (lanes[r.device] || (lanes[r.device] = [])).push(r);
      if (!known[r.device]) {
        known[r.device] = true;
        devices.push({name: r.device, slot_max: r.device === 'box' ? d.slots.box.slot_max : null,
                      enabled: true, placeholder: false, harnesses: [], listed: r.device === 'box' && !devices.length});
      }
    });
    if (!devices.length) {
      devices.push({name: 'box', slot_max: d.slots.box.slot_max, enabled: true, placeholder: false,
                    harnesses: [], listed: true});
    }
    var grid = add(root, el('div', 'devices'));
    devices.forEach(function (dev) {
      var jobs = lanes[dev.name] || [];
      var card = add(grid, el('section', 'device' + (dev.enabled ? '' : ' disabled')));
      var head = add(card, el('header'));
      add(head, el('span', 'name', dev.name));
      if (!dev.enabled) add(head, chip('DISABLED', 'muted'));
      else if (dev.placeholder) add(head, chip('PLACEHOLDER', 'warn'));
      else if (!dev.listed) add(head, chip('UNLISTED', 'warn'));
      else add(head, chip('ONLINE', 'ok'));
      add(head, el('span', 'cap', jobs.length + ' / ' + (dev.slot_max === null || dev.slot_max === undefined ? '?' : dev.slot_max) + ' slots'));
      if (dev.harnesses.length) add(card, el('p', 'harness', dev.harnesses.join(' · ')));
      var slots = add(card, el('div', 'slots'));
      jobs.forEach(function (r) { add(slots, jobCard(r)); });
      var free = Math.max(0, (dev.slot_max || 0) - jobs.length);
      if (!jobs.length && !free) free = 1;
      for (var i = 0; i < free; i++) add(slots, el('div', 'slot idle', dev.enabled ? 'idle' : 'off'));
    });
    if (wakes.length) {
      var card = add(grid, el('section', 'device'));
      var head = add(card, el('header'));
      add(head, el('span', 'name', 'grok wakes'));
      add(head, chip('BOT-TO-BOT', 'accent'));
      var slots = add(card, el('div', 'slots'));
      wakes.forEach(function (r) { add(slots, jobCard(r)); });
    }
  }

  function renderQueue(d) {
    var rows = d.queued || [];
    var root = panel('queue', 'Queue', rows.length + ' waiting');
    list(root, rows, 'queue empty', function (r) {
      var li = el('li');
      add(li, bot(r.recipient));
      add(li, el('span', 'ticket', ticketOf(r)));
      add(li, el('span', 'what', r.action));
      ticking(add(li, el('span', 'when')), r.age_seconds, 'waiting ');
      return mark('queue', r.job, li);
    });
  }

  var HANDOFF = {scripted: ['scripted', 'ok'], bot_to_bot: ['bot-to-bot', 'accent'],
                 failed: ['failed', 'bad'], done: ['done', 'muted']};
  function renderHandoffs(d) {
    var rows = d.handoff_savings.recent || [];
    var root = panel('handoffs', 'Recent handoffs');
    list(root, rows, 'no finished jobs yet', function (r) {
      var li = el('li');
      var kind = HANDOFF[r.kind] || [r.kind, 'muted'];
      add(li, chip(kind[0], kind[1]));
      add(li, bot(r.recipient));
      add(li, el('span', 'ticket', ticketOf(r)));
      add(li, el('span', 'what', r.action));
      ticking(add(li, el('span', 'when')), r.age_seconds, '', ' ago');
      return mark('handoffs', r.job + ':' + r.state, li);
    });
  }

  var VERDICT = {routed: 'accent', shipped: 'ok', stale: 'muted', rejected: 'warn', stuck: 'bad',
                 blocked: 'bad', waiting: 'warn', other: 'muted'};
  function renderVerdicts(d) {
    var rows = d.poller.verdicts;
    var root = panel('verdicts', 'Recent verdicts');
    if (rows === null || rows === undefined) { add(root, el('p', 'empty', 'no handled comments recorded yet')); return; }
    list(root, rows, 'no verdicts yet', function (r) {
      var li = el('li');
      add(li, chip(r.kind, VERDICT[r.kind] || 'muted'));
      add(li, el('span', 'ticket', ticketOf(r)));
      add(li, el('span', 'what', r.outcome));
      return mark('verdicts', r.repo + '#' + r.ticket_id + ':' + r.outcome, li);
    });
  }

  var POLL_STATE = {ok: ['OK', 'ok'], late: ['LATE', 'warn'], stale: ['STALE', 'bad'], unknown: ['UNKNOWN', 'muted']};
  var BUDGET_STATE = {ok: ['OK', 'ok'], deferring: ['DEFERRING', 'warn'], exhausted: ['EXHAUSTED', 'bad'],
                      rate_limited: ['RATE LIMITED', 'bad']};
  function renderHealth(d) {
    var p = d.poller;
    var h = p.health || {state: 'unknown'};
    var b = p.linear_budget || {state: 'ok'};
    var root = panel('health', 'Poll and Linear budget');
    var row = add(root, el('div', 'row'));
    add(row, el('span', 'label', 'poll'));
    var ps = POLL_STATE[h.state] || POLL_STATE.unknown;
    add(row, chip(ps[0], ps[1]));
    if (h.last_cycle_age_seconds === null || h.last_cycle_age_seconds === undefined) {
      add(row, el('span', 'muted', 'cycle time unknown (live push off)'));
    } else {
      ticking(add(row, el('span')), h.last_cycle_age_seconds, 'last cycle ', ' ago');
    }
    var row2 = add(root, el('div', 'row'));
    add(row2, el('span', 'label', 'linear'));
    var bs = BUDGET_STATE[b.state] || ['OTHER', 'muted'];
    add(row2, chip(bs[0], bs[1]));
    add(row2, el('span', 'muted', num(b.requests_per_hour) + ' req/h · ' + num(b.complexity_per_hour) + ' complexity/h'));
    var stats = add(root, el('div', 'stats'));
    [
      [p.errors.last_hour, 'poll errors, 1 h', p.errors.last_hour ? 'bad' : ''],
      [p.errors.last_day, 'poll errors, 24 h', ''],
      [b.deferrals_last_hour, 'Linear deferrals, 1 h', b.deferrals_last_hour ? 'warn' : ''],
      [(b.exhausted_last_hour || 0) + (b.rate_limited_last_hour || 0), 'Linear limit hits, 1 h',
       (b.exhausted_last_hour || b.rate_limited_last_hour) ? 'bad' : '']
    ].forEach(function (s) {
      var box = add(stats, el('div', 'stat'));
      add(box, el('b', s[2], num(s[0])));
      add(box, el('span', null, s[1]));
    });
    var activity = add(root, el('p', 'muted'));
    ticking(activity, p.last_activity_age_seconds, 'last poller activity ', ' ago');
    var recent = (p.errors.recent || []).slice(0, 3);
    if (recent.length) {
      list(root, recent, '', function (r) {
        var li = el('li');
        add(li, el('span', 'ticket', r.repo === 'poll' || r.ticket_id === '-' ? r.repo : ticketOf(r)));
        add(li, el('span', 'what', r.error));
        ticking(add(li, el('span', 'when')), r.age_seconds, '', ' ago');
        return li;
      });
    }
  }

  function renderSavings(d) {
    var s = d.handoff_savings;
    var root = panel('savings', 'Handoff savings');
    var gauge = add(root, el('div', 'gauge'));
    var ring = add(gauge, el('div', 'ring'));
    var pct = s.scripted_share === null ? null : Math.round(s.scripted_share * 100);
    ring.style.setProperty('--p', pct === null ? 0 : pct);
    add(ring, el('span', null, pct === null ? '—' : pct + '%'));
    var stats = add(gauge, el('div'));
    [[s.scripted, 'scripted handoffs', 'accent'], [s.scripted_in_flight, 'scripted in flight', ''],
     [s.bot_to_bot, 'bot-to-bot (grok wakes)', '']].forEach(function (x) {
      var line = add(stats, el('div', 'row'));
      add(line, el('b', x[2], num(x[0])));
      add(line, el('span', 'muted', x[1]));
    });
    var total = (s.scripted || 0) + (s.bot_to_bot || 0);
    var meter = add(root, el('div', 'meter'));
    if (total) {
      add(meter, el('i', 'scripted')).style.width = (100 * s.scripted / total) + '%';
      add(meter, el('i', 'bots')).style.width = (100 * s.bot_to_bot / total) + '%';
    }
    add(details(root, 'savings-def', 'definition'), el('p', 'muted', s.definition));
  }

  function renderAttention(d) {
    var stuck = d.stuck.tickets || [];
    var overdue = d.stuck.overdue_jobs || [];
    var paused = d.paused || [];
    var root = panel('attention', 'Needs attention', (stuck.length + overdue.length) + ' stuck · ' + paused.length + ' paused');
    var rows = [];
    stuck.forEach(function (r) { rows.push({kind: ['stuck', 'bad'], row: r, what: r.state + (r.reason ? ': ' + r.reason : '')}); });
    overdue.forEach(function (r) { rows.push({kind: ['overdue', 'bad'], row: r, what: r.recipient + ' ' + r.action + ', ' + dur(r.age_seconds)}); });
    paused.forEach(function (r) { rows.push({kind: ['paused', 'warn'], row: r, what: r.state + (r.paused_action ? ' (' + r.paused_action + ')' : '')}); });
    list(root, rows, 'all clear', function (x) {
      var li = el('li');
      add(li, chip(x.kind[0], x.kind[1]));
      add(li, el('span', 'ticket', ticketOf(x.row)));
      add(li, el('span', 'what', x.what));
      return mark('attention', x.kind[0] + ':' + x.row.repo + '#' + x.row.ticket_id, li);
    });
  }

  function renderTickets(d) {
    var rows = d.tickets || [];
    var root = panel('tickets', 'Tickets', rows.length + ' tracked');
    var box = details(root, 'tickets', 'show every tracked ticket');
    if (!rows.length) { add(box, el('p', 'empty', 'no tracked tickets')); return; }
    var table = add(box, el('table'));
    var head = add(add(table, el('thead')), el('tr'));
    ['ticket', 'tracker', 'state', 'round', 'gates', 'stuck'].forEach(function (h) { add(head, el('th', null, h)); });
    var tbody = add(table, el('tbody'));
    rows.forEach(function (r) {
      var tr = add(tbody, el('tr'));
      add(tr, el('td', null, r.repo + '#' + r.ticket_id));
      add(tr, el('td', null, r.tracker));
      add(tr, el('td', r.stuck ? 'bad' : null, r.state));
      add(tr, el('td', 'num', r.round));
      add(tr, el('td', 'num', r.gate_count));
      add(tr, el('td', r.stuck ? 'bad' : null, r.stuck ? 'yes' : 'no'));
    });
  }

  function renderLog(d) {
    var log = d.poller.log;
    var root = panel('log', 'poller.log', log.present ? 'written ' + dur(log.age_seconds) + ' before the snapshot' : 'absent');
    var box = details(root, 'log', 'last ' + log.tail.length + ' lines, redacted');
    add(box, el('pre', null, log.tail.join('\n')));
  }

  var RENDERERS = [renderKpis, renderLanes, renderHealth, renderQueue, renderHandoffs,
                   renderVerdicts, renderSavings, renderAttention, renderTickets, renderLog];

  function render(d) {
    tickers = [];
    nextKeys = {};
    RENDERERS.forEach(function (draw) {
      try { draw(d); } catch (err) { if (window.console) console.error('dashboard panel', err); }
    });
    prevKeys = nextKeys;
    firstRender = false;
  }

  function updateLink() {
    var link = byId('link');
    var text = byId('link-text');
    var age = since();
    var state;
    var label;
    if (!everOk) { state = 'boot'; label = failures ? 'NO LINK · static view' : 'ACQUIRING LINK'; }
    else if (Date.now() - lastOk > LINK_LOST_MS) { state = 'lost'; label = 'LINK LOST · retrying'; }
    else if (age > STALE_SECONDS) { state = 'stale'; label = 'STALE SNAPSHOT'; }
    else { state = 'live'; label = 'LIVE'; }
    if (link) link.className = 'link ' + state;
    if (text) text.textContent = label;
    var ageNode = byId('age');
    if (ageNode) ageNode.textContent = dur(age) + ' ago';
    var clock = byId('clock');
    if (clock) clock.textContent = new Date(Date.now()).toISOString().slice(11, 19) + ' UTC';
  }

  function tick() {
    var d = since();
    tickers.forEach(function (t) { t.run(d); });
    updateLink();
  }

  function accept(d) {
    if (!d || typeof d !== 'object' || d.schema !== 'factory-dashboard/v1') { failed(); return; }
    lastOk = Date.now();
    everOk = true;
    failures = 0;
    if (firstRender || d.generated_at_epoch !== generated) {
      generated = Number(d.generated_at_epoch) || generated;
      render(d);
    }
    if (hud) hud.hidden = false;
    if (fallback) fallback.hidden = true;
    tick();
  }

  function failed() {
    failures += 1;
    if (!everOk && failures >= 3 && !reloadArmed) {
      // status.json cannot be fetched (blocked or missing): behave like the
      // old page and reload the server-rendered view every refresh_seconds.
      reloadArmed = true;
      setTimeout(function () { location.reload(); }, refreshSeconds * 1000);
    }
    updateLink();
  }

  function schedule() {
    if (timer === null && !inflight && !document.hidden) timer = setTimeout(poll, POLL_MS);
  }

  function poll() {
    timer = null;
    if (inflight || document.hidden) return;
    inflight = true;
    var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    var guard = setTimeout(function () { if (ctrl) ctrl.abort(); }, FETCH_TIMEOUT_MS);
    var options = {cache: 'no-store', credentials: 'same-origin'};
    if (ctrl) options.signal = ctrl.signal;
    var request;
    try { request = fetch(STATUS_URL, options); } catch (err) { request = Promise.reject(err); }
    request
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(accept, function () { failed(); })
      .then(function () { clearTimeout(guard); inflight = false; schedule(); },
            function () { clearTimeout(guard); inflight = false; schedule(); });
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && timer === null && !inflight) poll();
  });
  setInterval(tick, 1000);
  updateLink();
  poll();
})();
"""


def page_script() -> str:
    return SCRIPT.replace("__POLL_MS__", str(POLL_MS))


def page_head(generated_at: str) -> str:
    """The HUD bar. Without script it shows the snapshot time as text."""
    return (
        '<header class="bar">'
        '<div class="brand">Factory <b>Mission Control</b></div>'
        '<div class="link boot" id="link" role="status" aria-live="polite">'
        '<span class="dot" aria-hidden="true"></span>'
        f'<span id="link-text">STATIC SNAPSHOT</span></div>'
        f'<div class="meta">snapshot <span id="age">{html.escape(generated_at)}</span>'
        ' · <span id="clock"></span> · read-only, redacted</div>'
        "</header>"
    )


def page_hud() -> str:
    """The empty panels the script fills; hidden until the first fetch."""
    panels = "".join(
        f'<section class="panel" id="{panel}" aria-label="{html.escape(title)}"></section>'
        for panel, title in _PANELS
    )
    return f'<main id="hud" hidden>{panels}</main>'
