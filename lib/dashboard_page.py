"""The dashboard pages: inline CSS, the server-rendered panels and the script.

Four pages share one bar (navigation and the needs-attention badge):

- index.html (main): factory status (with Linear webhook health when a
  listener has run), lanes and slots, the queue rail;
- job.html: every running or queued job with the ticket statechart
  (model/chart.mmd, see lib/dashboard_flow.py), its path and current state
  highlighted; job.html#job-<id> shows one job;
- activity.html: needs attention, recent handoffs and verdicts, tickets, log;
- efficiency.html: poll and Linear budget, handoff savings.

Every panel is rendered server-side here, so a page reads without script
(and then reloads every refresh_seconds). Everything is inline (no external
assets, nothing from a CDN), so the pages work offline on the tailnet and
under the static server's CSP. The script fetches the relative
`status.json` every POLL_MS (1 to 2 seconds) and rebuilds the current
page's panels in place with createElement/textContent only, never HTML
strings, so no value from the snapshot is ever parsed as markup. On the job
page it copies the server-drawn diagram and only moves the highlight. If
status.json cannot be fetched at all, the server-rendered page stays and
reloads every refresh_seconds.
"""

from __future__ import annotations

import html
import re

from lib.dashboard_flow import render_svg

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
#kpis,#tickets,#log,#attention,#jobs,#jobview{grid-column:1/-1}#lanes{grid-column:span 9}#queue{grid-column:span 3}
#handoffs,#verdicts,#health,#savings{grid-column:span 6}
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
.nav{display:flex;flex-wrap:wrap;gap:6px}
.nav a{padding:4px 10px;border:1px solid var(--line);border-radius:6px;color:var(--muted);text-decoration:none;
font:600 12px/1.2 var(--mono);letter-spacing:.14em;text-transform:uppercase}
.nav a[aria-current=page]{color:var(--accent);border-color:var(--accent)}
.badge{display:inline-flex;align-items:baseline;gap:6px;padding:3px 10px;border:1px solid var(--bad);border-radius:999px;
color:var(--bad);text-decoration:none;font:600 12px/1.4 var(--mono);letter-spacing:.08em}
.badge b{font-size:14px}
.badge.clear{border-color:var(--line);color:var(--muted)}
a.slot,a.queue-item,.jobs a{color:inherit;text-decoration:none}
a.slot{display:block}
a.slot:hover,a.slot:focus-visible{border-color:var(--accent)}
#lanes .devices{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
#lanes .slot{min-height:84px}
.slot.held{display:flex;align-items:center;justify-content:center;border-style:dashed;background:transparent;
font:600 11px/1 var(--mono);letter-spacing:.24em;text-transform:uppercase;color:var(--warn)}
.job .step{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:2px;font-size:12px;color:var(--muted)}
.job .step b{font-weight:600;color:var(--text)}
.kind{display:inline-block;padding:0 4px;border:1px solid var(--line);border-radius:4px;
font:700 10px/1.5 var(--mono);letter-spacing:.1em;color:var(--muted)}
.queue-item{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;width:100%}
.queue-item:hover .ticket,.jobs a:hover .ticket{color:var(--accent)}
#jobview{display:grid;gap:14px}
.jobpage .row{margin:0 0 6px}
.jobpage .where{margin:6px 0;color:var(--muted)}.jobpage .where b{color:var(--accent)}
.jobpage .path{margin:4px 0 8px;font:12px/1.4 var(--mono);color:var(--muted);overflow-wrap:anywhere}
.jobpage:target{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
#jobview:has(.jobpage:target) .jobpage:not(:target){display:none}
.flowbox{overflow-x:auto;margin:6px 0 8px;padding:4px 0;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.flow{display:block;font:600 11px/1 var(--mono)}
.flow text{fill:var(--text);text-anchor:middle}
.flow .gloss{fill:var(--muted);font-size:9px;font-weight:400}
.flow .node rect{fill:var(--panel);stroke:var(--g,var(--line));stroke-width:1.5}
.flow .node:not(.visited){opacity:.6}
.flow .node.end rect{stroke-width:2.5}
.flow .node.visited rect{fill:var(--panel-hi);stroke-width:2.5}
.flow .node.current rect{fill:var(--panel-hi);stroke:var(--accent);stroke-width:4}
.flow .node.current text{fill:var(--accent)}
.flow .edge path,.flow .entry path{fill:none;stroke:var(--line);stroke-width:1.4}
.flow .edge polygon,.flow .entry polygon,.flow .entry circle{fill:var(--line)}
.flow .edge.taken path,.flow .entry.taken path{stroke:var(--accent);stroke-width:2.6}
.flow .edge.taken polygon,.flow .entry.taken polygon,.flow .entry.taken circle{fill:var(--accent)}
.g-blue{--g:var(--codex)}.g-purple{--g:var(--cursor)}.g-orange{--g:var(--claude)}.g-yellow{--g:var(--warn)}
.g-teal{--g:var(--ok)}.g-tan{--g:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin:0;font-size:12px;color:var(--muted)}
.legend .key{color:var(--g,var(--muted))}
.legend .key::before{content:"";display:inline-block;width:9px;height:9px;margin-right:5px;border:2px solid currentColor;border-radius:3px}
.legend .key.path{color:var(--accent)}
.definition{margin:8px 0 4px;color:var(--text)}
[hidden]{display:none !important}
@keyframes sweep{to{left:100%}}
@keyframes pulse{0%{box-shadow:0 0 0 0 currentColor}100%{box-shadow:0 0 0 9px transparent}}
@keyframes shimmer{from{background-position:100% 0}to{background-position:-100% 0}}
@keyframes flash{from{box-shadow:0 0 0 2px var(--accent),0 0 18px var(--accent)}to{box-shadow:0 0 0 0 transparent}}
@media (max-width:1100px){.panel,#lanes,#queue,#handoffs,#verdicts,#health,#savings{grid-column:1/-1}}
@media (prefers-reduced-motion: reduce){
*,*::before,*::after{animation: none !important;transition: none !important;scroll-behavior:auto !important}
.fresh{outline:2px solid var(--accent);outline-offset:-2px}}
"""

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
  var PAGE = body.getAttribute('data-page') || 'main';
  var refreshSeconds = Number(body.getAttribute('data-refresh')) || 20;
  var generated = Number(body.getAttribute('data-generated')) || 0;
  function byId(id) { return document.getElementById(id); }
  var lastOk = 0;
  var everOk = false;
  var failures = 0;
  var inflight = false;
  var timer = null;
  var reloadArmed = false;
  var firstRender = true;
  var latest = null;
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
  function link(cls, href) { var a = el('a', cls); a.setAttribute('href', href); return a; }
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
  function jobLabel(r) { return r.tracker === 'linear' ? String(r.ticket_id) : ticketOf(r); }
  function jobKind(r) { return r.tracker === 'github' ? 'PR' : r.tracker === 'linear' ? 'ticket' : null; }
  function anchor(job) { return 'job-' + String(job).replace(/[^A-Za-z0-9_-]/g, '-'); }
  function bot(name) { return el('span', 'bot ' + (BOTS[name] ? name : 'other'), name); }
  function chip(text, cls) { return el('span', 'chip ' + (cls || 'muted'), text); }
  // #75: lib/job_state.py's state of a running job, as a chip.
  var JOB_STATE = {claimed: ['claimed, not started', 'warn'], running: ['running', 'ok'],
                   stale: ['stale heartbeat', 'warn'], dead: ['runner dead', 'bad']};
  function stateChip(r) {
    var s = JOB_STATE[r.job_state];
    return s ? chip(s[0], s[1]) : null;
  }
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

  // --- every page ---------------------------------------------------------

  function renderBadge(d) {
    var a = d.attention || {count: 0};
    var count = byId('attention-count');
    if (count) count.textContent = String(a.count);
    var badge = byId('attention-badge');
    if (badge) badge.className = 'badge' + (a.count ? '' : ' clear');
  }

  // --- main page: status, lanes and slots, the queue rail ------------------

  function renderKpis(d) {
    var root = panel('kpis', 'Factory status');
    if (!root) return;
    var t = d.queue.totals;
    var stuck = d.stuck.tickets.length + d.stuck.overdue_jobs.length;
    var row = add(root, el('div', 'kpis'));
    [
      ['running', t.running, 'accent'],
      ['queued', t.queued, t.queued ? 'warn' : ''],
      ['done', t.done, 'ok'],
      ['failed', t.failed, t.failed ? 'bad' : ''],
      ['stuck / overdue', stuck, stuck ? 'bad' : 'ok']
    ].forEach(function (k) {
      var tile = add(row, el('div', 'kpi ' + k[2]));
      add(tile, el('b', null, num(k[1])));
      add(tile, el('span', null, k[0]));
    });
  }

  // Webhook health (#56): a small block appended to the Factory status
  // panel that renderKpis has just rebuilt, so it is redrawn from
  // status.json on every poll; absent when no listener has run. The heading
  // is split so a page without a listener never contains the phrase
  // (tests/test_linear_webhook_health.py).
  function renderLinearWebhook(d) {
    var root = byId('kpis');
    var w = d.poller && d.poller.linear_webhook;
    if (!root || !w) return;
    var received;
    if (w.last_received_age_seconds !== null && w.last_received_age_seconds !== undefined) {
      received = dur(w.last_received_age_seconds) + ' ago' + (w.last_event ? ' (' + w.last_event + ')' : '');
    } else if (w.listener_age_seconds === null || w.listener_age_seconds === undefined) {
      received = 'never (listener has not started)';
    } else {
      received = 'never';
    }
    var box = add(root, el('div'));
    box.setAttribute('id', 'linear-webhook');
    add(box, el('h2', null, 'Linear' + ' webhook'));
    add(box, el('p', null, 'last received ' + received + ' · dirty queue ' + w.dirty));
    if (w.warning !== null && w.warning !== undefined) add(box, el('p', 'bad', 'warning: ' + w.warning));
  }

  function slotCard(r) {
    var card = link('slot job' + (r.overdue ? ' overdue' : ''), 'job.html#' + anchor(r.job));
    card.title = r.repo + '#' + r.ticket_id + ' (job ' + r.job + ')';
    var top = add(card, el('div', 'top'));
    add(top, bot(r.recipient));
    var kind = jobKind(r);
    if (kind) add(top, el('span', 'kind', kind));
    add(top, el('span', 'ticket', jobLabel(r)));
    var state = stateChip(r);
    if (state) add(top, state);
    if (r.overdue) add(top, chip('OVERDUE', 'bad'));
    var step = add(card, el('div', 'step'));
    add(step, el('span', 'action', r.action));
    add(step, el('span', 'round', 'round ' + (r.round === null || r.round === undefined ? '—' : r.round)));
    var times = add(card, el('div', 'times'));
    var elapsed = add(times, el('span'));
    if (r.job_state === 'claimed') {
      // Claimed and never started: no runner, so no T+ or heartbeat bar.
      elapsed.title = 'no orch job run has started this job';
      add(elapsed, el('span', null, 'claimed '));
      ticking(add(elapsed, el('b')), r.elapsed_seconds, '', ' ago');
      add(times, el('span', 'warn', 'not started'));
      return mark('lanes', r.job, card);
    }
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
    if (!root) return;
    var devices = (d.slots.devices || []).map(function (x) {
      return {name: x.name, slot_max: x.slot_max, held: x.held || 0, enabled: x.enabled,
              placeholder: x.placeholder, harnesses: x.harnesses || [], listed: true};
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
        devices.push({name: r.device, slot_max: r.device === 'box' ? d.slots.box.slot_max : null, held: 0,
                      enabled: true, placeholder: false, harnesses: [], listed: r.device === 'box' && !devices.length});
      }
    });
    if (!devices.length) {
      devices.push({name: 'box', slot_max: d.slots.box.slot_max, held: 0, enabled: true, placeholder: false,
                    harnesses: [], listed: true});
    }
    var grid = add(root, el('div', 'devices'));
    devices.forEach(function (dev) {
      var jobs = lanes[dev.name] || [];
      // #63 FU-1: a slot held in device_slots is in use even when its job is
      // not (or no longer) running, so a lane counts max(held, running).
      var used = Math.max(dev.held, jobs.length);
      var card = add(grid, el('section', 'device' + (dev.enabled ? '' : ' disabled')));
      card.setAttribute('data-device', dev.name);
      var head = add(card, el('header'));
      add(head, el('span', 'name', dev.name));
      if (!dev.enabled) add(head, chip('DISABLED', 'muted'));
      else if (dev.placeholder) add(head, chip('PLACEHOLDER', 'warn'));
      else if (!dev.listed) add(head, chip('UNLISTED', 'warn'));
      else add(head, chip('ONLINE', 'ok'));
      add(head, el('span', 'cap', used + ' / ' + (dev.slot_max === null || dev.slot_max === undefined ? '?' : dev.slot_max) + ' slots'));
      if (dev.harnesses.length) add(card, el('p', 'harness', dev.harnesses.join(' · ')));
      var slots = add(card, el('div', 'slots'));
      jobs.forEach(function (r) { add(slots, slotCard(r)); });
      var held = used - jobs.length;
      for (var h = 0; h < held; h++) add(slots, el('div', 'slot held', 'held'));
      var free = Math.max(0, (dev.slot_max || 0) - used);
      if (!used && !free) free = 1;
      for (var i = 0; i < free; i++) add(slots, el('div', 'slot idle', dev.enabled ? 'idle' : 'off'));
    });
    if (wakes.length) {
      var card = add(grid, el('section', 'device'));
      card.setAttribute('data-device', 'grok-wakes');
      var head = add(card, el('header'));
      add(head, el('span', 'name', 'grok wakes'));
      add(head, chip('BOT-TO-BOT', 'accent'));
      var slots = add(card, el('div', 'slots'));
      wakes.forEach(function (r) { add(slots, slotCard(r)); });
    }
  }

  function renderQueue(d) {
    var rows = d.queued || [];
    var root = panel('queue', 'Queue', rows.length + ' waiting');
    if (!root) return;
    list(root, rows, 'queue empty', function (r) {
      var li = el('li');
      var a = add(li, link('queue-item', 'job.html#' + anchor(r.job)));
      add(a, bot(r.recipient));
      add(a, el('span', 'ticket', jobLabel(r)));
      add(a, el('span', 'what', r.action));
      ticking(add(a, el('span', 'when')), r.age_seconds, 'waiting ');
      return mark('queue', r.job, li);
    });
  }

  // --- activity page ---------------------------------------------------------

  var ATTENTION = {stuck: 'bad', overdue: 'bad', dead: 'bad', stale: 'warn', paused: 'warn'};
  function renderAttention(d) {
    var a = d.attention || {count: 0, items: []};
    var root = panel('attention', 'Needs attention', a.count + (a.count === 1 ? ' item' : ' items'));
    if (!root) return;
    list(root, a.items, 'all clear', function (x) {
      var li = el('li');
      add(li, chip(x.kind, ATTENTION[x.kind] || 'muted'));
      add(li, el('span', 'ticket', ticketOf(x)));
      add(li, el('span', 'what', x.detail));
      return mark('attention', x.kind + ':' + x.repo + '#' + x.ticket_id, li);
    });
  }

  var HANDOFF = {scripted: ['scripted', 'ok'], bot_to_bot: ['bot-to-bot', 'accent'],
                 failed: ['failed', 'bad'], done: ['done', 'muted']};
  function renderHandoffs(d) {
    var rows = d.handoff_savings.recent || [];
    var root = panel('handoffs', 'Recent handoffs');
    if (!root) return;
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
    if (!root) return;
    if (rows === null || rows === undefined) { add(root, el('p', 'empty', 'no handled comments recorded yet')); return; }
    list(root, rows, 'no verdicts yet', function (r) {
      var li = el('li');
      add(li, chip(r.kind, VERDICT[r.kind] || 'muted'));
      add(li, el('span', 'ticket', ticketOf(r)));
      add(li, el('span', 'what', r.outcome));
      return mark('verdicts', r.repo + '#' + r.ticket_id + ':' + r.outcome, li);
    });
  }

  function renderTickets(d) {
    var rows = d.tickets || [];
    var root = panel('tickets', 'Tickets', rows.length + ' tracked');
    if (!root) return;
    if (!rows.length) { add(root, el('p', 'empty', 'no tracked tickets')); return; }
    var table = add(root, el('table'));
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
    if (!root) return;
    add(root, el('p', 'muted', 'last ' + log.tail.length + ' lines, redacted'));
    add(root, el('pre', null, log.tail.join('\n')));
  }

  // --- efficiency page --------------------------------------------------------

  var POLL_STATE = {ok: ['OK', 'ok'], late: ['LATE', 'warn'], stale: ['STALE', 'bad'], unknown: ['UNKNOWN', 'muted']};
  var BUDGET_STATE = {ok: ['OK', 'ok'], deferring: ['DEFERRING', 'warn'], exhausted: ['EXHAUSTED', 'bad'],
                      rate_limited: ['RATE LIMITED', 'bad']};
  function renderHealth(d) {
    var p = d.poller;
    var h = p.health || {state: 'unknown'};
    var b = p.linear_budget || {state: 'ok'};
    var root = panel('health', 'Poll and Linear budget');
    if (!root) return;
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
    if (!root) return;
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
    add(root, el('p', 'definition', s.scripted_share_definition)).setAttribute('id', 'share-definition');
    add(details(root, 'savings-def', 'how each count is made'), el('p', 'muted', s.definition));
  }

  // --- job page -----------------------------------------------------------------

  function activeJobs(d) {
    return (d.running || []).map(function (r) { return {row: r, state: 'running'}; })
      .concat((d.queued || []).map(function (r) { return {row: r, state: 'queued'}; }));
  }

  function renderJobs(d) {
    var jobs = activeJobs(d);
    var root = panel('jobs', 'Jobs', (d.running || []).length + ' running · ' + (d.queued || []).length + ' queued');
    if (!root) return;
    list(root, jobs, 'no running or queued jobs', function (j) {
      var li = el('li');
      var a = add(li, link('queue-item', '#' + anchor(j.row.job)));
      add(a, bot(j.row.recipient));
      add(a, el('span', 'ticket', jobLabel(j.row)));
      add(a, el('span', 'what', j.row.action));
      add(a, chip(j.state, j.state === 'running' ? 'accent' : 'warn'));
      return li;
    });
  }

  function walk(node, fn) {
    fn(node);
    var kids = node.children || [];
    for (var i = 0; i < kids.length; i++) walk(kids[i], fn);
  }
  function classes(node, drop, extra) {
    var kept = String(node.getAttribute('class') || '').split(/\s+/).filter(function (c) {
      return c && drop.indexOf(c) < 0;
    });
    node.setAttribute('class', kept.concat(extra).join(' '));
  }
  // The diagram is the server-rendered statechart (model/chart.mmd) in
  // #flow-template; a job gets a copy with its path and state marked.
  function highlight(svg, flow) {
    var path = flow.path || [];
    var seen = {};
    var taken = {};
    path.forEach(function (s, i) {
      seen[s] = true;
      if (i) taken[path[i - 1] + '\n' + s] = true;
    });
    walk(svg, function (n) {
      if (!n.getAttribute) return;
      var state = n.getAttribute('data-state');
      var from = n.getAttribute('data-from');
      var cls = String(n.getAttribute('class') || '');
      if (state !== null) {
        classes(n, ['visited', 'current'], (seen[state] ? ['visited'] : []).concat(state === flow.state ? ['current'] : []));
      } else if (from !== null) {
        classes(n, ['taken'], taken[from + '\n' + n.getAttribute('data-to')] ? ['taken'] : []);
      } else if (/(^|\s)entry(\s|$)/.test(cls)) {
        classes(n, ['taken'], path.length ? ['taken'] : []);
      }
    });
    return svg;
  }

  function jobArticle(r, state, d) {
    var art = el('article', 'panel jobpage');
    art.setAttribute('id', anchor(r.job));
    var head = add(art, el('h2'));
    add(head, el('span', null, jobLabel(r)));
    add(head, el('span', 'count', state + ' · job ' + r.job));
    var row = add(art, el('div', 'row'));
    add(row, bot(r.recipient));
    var kind = jobKind(r);
    if (kind) add(row, el('span', 'kind', kind));
    add(row, el('span', 'ticket', jobLabel(r)));
    add(row, el('span', 'what', 'step ' + r.action));
    if (state === 'running') ticking(add(row, el('span', 'muted')), r.elapsed_seconds, 'T+ ');
    else ticking(add(row, el('span', 'muted')), r.age_seconds, 'waiting ');
    var jobState = stateChip(r);
    if (jobState) add(row, jobState);
    if (r.overdue) add(row, chip('OVERDUE', 'bad'));
    var flow = r.flow || {state: null, path: [], state_age_seconds: null};
    var where = add(art, el('p', 'where'));
    add(where, el('span', null, 'now '));
    add(where, el('b', null, flow.state || 'state not recorded'));
    add(where, el('span', null, ' · round ' + (r.round === null || r.round === undefined ? '—' : r.round)));
    if (flow.state && flow.state_age_seconds !== null && flow.state_age_seconds !== undefined) {
      add(where, el('span', null, ' · '));
      ticking(add(where, el('span')), flow.state_age_seconds, '', ' in ' + flow.state);
    }
    add(art, el('p', 'path', 'path: ' + ((flow.path || []).length ? flow.path.join(' → ') : '—')));
    var box = add(art, el('div', 'flowbox'));
    var tpl = byId('flow-template');
    var src = tpl && tpl.children ? tpl.children[0] : null;
    if (src && d.statechart) add(box, highlight(src.cloneNode(true), flow));
    else add(box, el('p', 'empty', 'statechart unavailable'));
    if (d.statechart) {
      var legend = add(art, el('p', 'legend'));
      add(legend, el('span', 'key path', 'path taken'));
      (d.statechart.legend || []).forEach(function (k) { add(legend, el('span', 'key g-' + k.group, k.name)); });
    }
    return art;
  }

  function renderJobView(d) {
    var root = byId('jobview');
    if (!root) return;
    root.replaceChildren();
    var jobs = activeJobs(d);
    var hash = String(location.hash || '');
    if (hash.indexOf('#job-') === 0) {
      jobs = jobs.filter(function (j) { return '#' + anchor(j.row.job) === hash; });
      if (!jobs.length) {
        var gone = add(root, el('section', 'panel'));
        add(add(gone, el('h2')), el('span', null, hash.slice(1)));
        add(gone, el('p', 'empty', 'That job is no longer running or queued. Pick one from the list above.'));
        return;
      }
    }
    if (!jobs.length) { add(root, el('p', 'empty', 'no running or queued jobs')); return; }
    jobs.forEach(function (j) { add(root, jobArticle(j.row, j.state, d)); });
  }

  var PAGES = {
    main: [renderKpis, renderLinearWebhook, renderLanes, renderQueue],
    activity: [renderAttention, renderHandoffs, renderVerdicts, renderTickets, renderLog],
    efficiency: [renderHealth, renderSavings],
    job: [renderJobs, renderJobView]
  };
  var RENDERERS = [renderBadge].concat(PAGES[PAGE] || []);

  function render(d) {
    latest = d;
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
    tick();
  }

  function failed() {
    failures += 1;
    if (!everOk && failures >= 3 && !reloadArmed) {
      // status.json cannot be fetched (blocked or missing): behave like a
      // page without script and reload the server-rendered view every
      // refresh_seconds.
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
  if (window.addEventListener) {
    window.addEventListener('hashchange', function () { if (latest) { render(latest); tick(); } });
  }
  setInterval(tick, 1000);
  updateLink();
  poll();
})();
"""


def page_script() -> str:
    return SCRIPT.replace("__POLL_MS__", str(POLL_MS))


# --- server-side rendering ----------------------------------------------------------
#
# Each page's panels are rendered here with the same markup the script
# builds, so a page reads the same without script (it then reloads every
# refresh_seconds) and the script only has to replace their contents.

PAGES = ("index.html", "activity.html", "efficiency.html", "job.html")
_PAGE_NAMES = {
    "index.html": ("main", "Lanes"),
    "activity.html": ("activity", "Activity"),
    "efficiency.html": ("efficiency", "Efficiency"),
    "job.html": ("job", "Jobs"),
}
LIST_MAX = 12
_BOTS = ("claude", "codex", "cursor", "grok")


def _e(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return html.escape(str(value), quote=True)


def duration(seconds: object) -> str:
    """Like the script's dur(): 45s, 3m 05s, 2h 07m, 1d 4h."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def _num(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return _e(value)


def _short(repo: object) -> str:
    return str(repo).rsplit("/", 1)[-1]


def _ticket(row: dict) -> str:
    return f"{_short(row['repo'])}#{row['ticket_id']}"


def _job_label(row: dict) -> str:
    return str(row["ticket_id"]) if row.get("tracker") == "linear" else _ticket(row)


def _job_kind(row: dict) -> str | None:
    return {"github": "PR", "linear": "ticket"}.get(str(row.get("tracker")))


def anchor(job: object) -> str:
    return "job-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(job))


def _b(value: object, cls: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<b{attr}>{_num(value)}</b>"


def _span(cls: str | None, text: object) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<span{attr}>{_e(text)}</span>"


def _bot(name: object) -> str:
    return _span(f"bot {name if name in _BOTS else 'other'}", name)


def _chip(text: object, cls: str = "muted") -> str:
    return _span(f"chip {cls}", text)


def _panel(ident: str, title: str, inner: str, *, count: object = None, extra: str = "") -> str:
    head = _span(None, title) + ("" if count is None else _span("count", count))
    classes = f"panel {extra}".strip()
    return (
        f'<section class="{classes}" id="{ident}" aria-label="{_e(title)}">'
        f"<h2>{head}</h2>{inner}</section>"
    )


def _feed(rows: list, empty: str, draw) -> str:
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    items = "".join(f"<li>{draw(row)}</li>" for row in rows[:LIST_MAX])
    more = (
        f'<p class="more">+{len(rows) - LIST_MAX} more</p>' if len(rows) > LIST_MAX else ""
    )
    return f'<ul class="feed">{items}</ul>{more}'


def _when(seconds: object, prefix: str = "", suffix: str = "") -> str:
    return _span("when", prefix + duration(seconds) + suffix)


# --- the bar on every page -------------------------------------------------------------


def _bar(snapshot: dict, page: str) -> str:
    current = ' aria-current="page"'
    links = "".join(
        f'<a href="{name}"{current if name == page else ""}>{label}</a>'
        for name, (_, label) in _PAGE_NAMES.items()
    )
    count = int((snapshot.get("attention") or {}).get("count", 0))
    return (
        '<header class="bar">'
        '<div class="brand">Factory <b>Mission Control</b></div>'
        f'<nav class="nav" id="nav" aria-label="Pages">{links}</nav>'
        f'<a class="badge{"" if count else " clear"}" id="attention-badge" '
        'href="activity.html#attention" '
        'title="stuck tickets, overdue, stale or dead jobs and paused tickets">'
        f'<b id="attention-count">{count}</b> needs attention</a>'
        '<div class="link boot" id="link" role="status" aria-live="polite">'
        '<span class="dot" aria-hidden="true"></span>'
        '<span id="link-text">STATIC SNAPSHOT</span></div>'
        f'<div class="meta">snapshot <span id="age">{_e(snapshot["generated_at"])}</span>'
        ' · <span id="clock"></span> · read-only, redacted</div>'
        "</header>"
    )


# --- main page -------------------------------------------------------------------------


def _kpis(snapshot: dict) -> str:
    totals = snapshot["queue"]["totals"]
    stuck = len(snapshot["stuck"]["tickets"]) + len(snapshot["stuck"]["overdue_jobs"])
    tiles = [
        ("running", totals["running"], "accent"),
        ("queued", totals["queued"], "warn" if totals["queued"] else ""),
        ("done", totals["done"], "ok"),
        ("failed", totals["failed"], "bad" if totals["failed"] else ""),
        ("stuck / overdue", stuck, "bad" if stuck else "ok"),
    ]
    inner = "".join(
        f'<div class="kpi {cls}"><b>{_num(value)}</b>{_span(None, label)}</div>'
        for label, value, cls in tiles
    )
    return _panel(
        "kpis", "Factory status", f'<div class="kpis">{inner}</div>' + _linear_webhook(snapshot)
    )


def _linear_webhook(snapshot: dict) -> str:
    """Linear webhook health (#56), a small block inside Factory status; only
    when a listener or webhook-mode poller has run on this database. The
    script's renderLinearWebhook builds the same markup from status.json."""
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
    warning = (
        f'<p class="bad">warning: {_e(webhook["warning"])}</p>'
        if webhook["warning"] is not None
        else ""
    )
    return (
        '<div id="linear-webhook"><h2>Linear webhook</h2>'
        f"<p>last received {_e(received)} · dirty queue {_e(webhook['dirty'])}</p>"
        f"{warning}</div>"
    )


# #75: lib/job_state.py's state of a running job, as a chip (JOB_STATE in the script).
_JOB_STATE = {
    "claimed": ("claimed, not started", "warn"),
    "running": ("running", "ok"),
    "stale": ("stale heartbeat", "warn"),
    "dead": ("runner dead", "bad"),
}


def _state_chip(row: dict) -> str:
    found = _JOB_STATE.get(row.get("job_state"))  # type: ignore[arg-type]
    return _chip(found[0], found[1]) if found else ""


def _slot(row: dict) -> str:
    kind = _job_kind(row)
    rnd = "—" if row.get("round") is None else row["round"]
    age = row["heartbeat_age_seconds"]
    frac = min(1.0, age / (row["timeout_seconds"] or 1))
    level = " bad" if frac >= 1 else " warn" if frac >= 0.6 else ""
    since = (
        "since it was placed on this device"
        if row.get("elapsed_from") == "placed"
        else "since it was queued"
    )
    if row.get("job_state") == "claimed":
        # Claimed and never started: no runner, so no T+ or heartbeat bar.
        times = (
            '<div class="times"><span title="no orch job run has started this job">'
            f'<span>claimed </span><b>{duration(row["elapsed_seconds"])} ago</b></span>'
            + _span("warn", "not started")
            + "</div>"
        )
    else:
        times = (
            f'<div class="times"><span title="{since}"><span>T+ </span>'
            f'<b>{duration(row["elapsed_seconds"])}</b></span>'
            f'<span title="last heartbeat"><span>hb </span><b>{duration(age)} ago</b></span></div>'
            f'<div class="hb{level}"><i style="width:{max(2, round(frac * 100))}%"></i></div>'
        )
    return (
        f'<a class="slot job{" overdue" if row["overdue"] else ""}" '
        f'href="job.html#{anchor(row["job"])}" '
        f'title="{_e(row["repo"])}#{_e(row["ticket_id"])} (job {_e(row["job"])})">'
        f'<div class="top">{_bot(row["recipient"])}'
        + (_span("kind", kind) if kind else "")
        + _span("ticket", _job_label(row))
        + _state_chip(row)
        + (_chip("OVERDUE", "bad") if row["overdue"] else "")
        + "</div>"
        f'<div class="step">{_span("action", row["action"])}{_span("round", f"round {rnd}")}</div>'
        + times
        + "</a>"
    )


def lane_devices(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """The lanes the script draws: (devices with their running jobs, grok
    wakes). Mirrors renderLanes in the page script."""
    slots = snapshot["slots"]
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
                "slot_max": slots["box"]["slot_max"] if row["device"] == "box" else None,
                "held": 0,
                "enabled": True,
                "placeholder": False,
                "harnesses": [],
                "listed": row["device"] == "box" and not devices,
                "jobs": [],
            }
            devices.append(device)
            by_name[row["device"]] = device
        by_name[row["device"]]["jobs"].append(row)
    if not devices:
        devices.append(
            {"name": "box", "slot_max": slots["box"]["slot_max"], "held": 0, "enabled": True,
             "placeholder": False, "harnesses": [], "listed": True, "jobs": []}
        )
    for device in devices:
        # #63 FU-1: a slot held in device_slots is in use even when its job
        # is not (or no longer) running, so a lane counts max(held, running).
        device["used"] = max(device["held"], len(device["jobs"]))
    return devices, wakes


def _lanes(snapshot: dict) -> str:
    devices, wakes = lane_devices(snapshot)
    cards = []
    for device in devices:
        if not device["enabled"]:
            state = _chip("DISABLED", "muted")
        elif device["placeholder"]:
            state = _chip("PLACEHOLDER", "warn")
        elif not device["listed"]:
            state = _chip("UNLISTED", "warn")
        else:
            state = _chip("ONLINE", "ok")
        cap = "?" if device["slot_max"] is None else device["slot_max"]
        used = device["used"]
        held = used - len(device["jobs"])
        free = max(0, (device["slot_max"] or 0) - used)
        if not used and not free:
            free = 1
        slots = "".join(_slot(row) for row in device["jobs"])
        slots += '<div class="slot held">held</div>' * held
        slots += f'<div class="slot idle">{"idle" if device["enabled"] else "off"}</div>' * free
        harness = (
            f'<p class="harness">{_e(" · ".join(device["harnesses"]))}</p>'
            if device["harnesses"]
            else ""
        )
        cards.append(
            f'<section class="device{"" if device["enabled"] else " disabled"}" '
            f'data-device="{_e(device["name"])}">'
            f'<header>{_span("name", device["name"])}{state}'
            f'{_span("cap", f"{used} / {cap} slots")}</header>'
            f'{harness}<div class="slots">{slots}</div></section>'
        )
    if wakes:
        cards.append(
            '<section class="device" data-device="grok-wakes"><header>'
            f'{_span("name", "grok wakes")}{_chip("BOT-TO-BOT", "accent")}</header>'
            f'<div class="slots">{"".join(_slot(row) for row in wakes)}</div></section>'
        )
    return _panel(
        "lanes",
        "Lanes and slots",
        f'<div class="devices">{"".join(cards)}</div>',
        count=f"{len(snapshot['running'])} running",
    )


def _queue(snapshot: dict) -> str:
    rows = snapshot["queued"]

    def draw(row: dict) -> str:
        return (
            f'<a class="queue-item" href="job.html#{anchor(row["job"])}">'
            f'{_bot(row["recipient"])}{_span("ticket", _job_label(row))}'
            f'{_span("what", row["action"])}{_when(row["age_seconds"], "waiting ")}</a>'
        )

    return _panel(
        "queue", "Queue", _feed(rows, "queue empty", draw),
        count=f"{len(rows)} waiting", extra="rail",
    )


# --- activity page -----------------------------------------------------------------


_ATTENTION = {"stuck": "bad", "overdue": "bad", "dead": "bad", "stale": "warn", "paused": "warn"}


def _attention(snapshot: dict) -> str:
    attention = snapshot["attention"]

    def draw(row: dict) -> str:
        return (
            _chip(row["kind"], _ATTENTION.get(row["kind"], "muted"))
            + _span("ticket", _ticket(row))
            + _span("what", row["detail"])
        )

    count = attention["count"]
    return _panel(
        "attention", "Needs attention", _feed(attention["items"], "all clear", draw),
        count=f"{count} item" + ("" if count == 1 else "s"),
    )


_HANDOFF = {"scripted": ("scripted", "ok"), "bot_to_bot": ("bot-to-bot", "accent"),
            "failed": ("failed", "bad"), "done": ("done", "muted")}


def _handoffs(snapshot: dict) -> str:
    def draw(row: dict) -> str:
        label, cls = _HANDOFF.get(row["kind"], (row["kind"], "muted"))
        return (
            _chip(label, cls) + _bot(row["recipient"]) + _span("ticket", _ticket(row))
            + _span("what", row["action"]) + _when(row["age_seconds"], suffix=" ago")
        )

    rows = snapshot["handoff_savings"]["recent"]
    return _panel("handoffs", "Recent handoffs", _feed(rows, "no finished jobs yet", draw))


_VERDICT = {"routed": "accent", "shipped": "ok", "stale": "muted", "rejected": "warn",
            "stuck": "bad", "blocked": "bad", "waiting": "warn", "other": "muted"}


def _verdicts(snapshot: dict) -> str:
    rows = snapshot["poller"]["verdicts"]
    if rows is None:
        inner = '<p class="empty">no handled comments recorded yet</p>'
    else:
        inner = _feed(
            rows,
            "no verdicts yet",
            lambda row: _chip(row["kind"], _VERDICT.get(row["kind"], "muted"))
            + _span("ticket", _ticket(row)) + _span("what", row["outcome"]),
        )
    return _panel("verdicts", "Recent verdicts", inner)


def _td(value: object, cls: str | None = None) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<td{attr}>{_e(value)}</td>"


def _tickets(snapshot: dict) -> str:
    rows = snapshot["tickets"]
    if not rows:
        inner = '<p class="empty">no tracked tickets</p>'
    else:
        head = "".join(
            f"<th>{name}</th>"
            for name in ("ticket", "tracker", "state", "round", "gates", "stuck")
        )
        body = "".join(
            "<tr>"
            + _td(f"{row['repo']}#{row['ticket_id']}")
            + _td(row["tracker"])
            + _td(row["state"], "bad" if row["stuck"] else None)
            + _td(row["round"], "num")
            + _td(row["gate_count"], "num")
            + _td("yes" if row["stuck"] else "no", "bad" if row["stuck"] else None)
            + "</tr>"
            for row in rows
        )
        inner = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    return _panel("tickets", "Tickets", inner, count=f"{len(rows)} tracked")


def _log(snapshot: dict) -> str:
    log = snapshot["poller"]["log"]
    count = (
        f"written {duration(log['age_seconds'])} before the snapshot"
        if log["present"]
        else "absent"
    )
    inner = (
        f'<p class="muted">last {len(log["tail"])} lines, redacted</p>'
        "<pre>" + "\n".join(_e(line) for line in log["tail"]) + "</pre>"
    )
    return _panel("log", "poller.log", inner, count=count)


# --- efficiency page ----------------------------------------------------------------


_POLL_STATE = {"ok": ("OK", "ok"), "late": ("LATE", "warn"), "stale": ("STALE", "bad"),
               "unknown": ("UNKNOWN", "muted")}
_BUDGET_STATE = {"ok": ("OK", "ok"), "deferring": ("DEFERRING", "warn"),
                 "exhausted": ("EXHAUSTED", "bad"), "rate_limited": ("RATE LIMITED", "bad")}


def _health(snapshot: dict) -> str:
    poller = snapshot["poller"]
    health = poller["health"] or {"state": "unknown"}
    budget = poller["linear_budget"] or {"state": "ok"}
    label, cls = _POLL_STATE.get(health["state"], _POLL_STATE["unknown"])
    cycle = health.get("last_cycle_age_seconds")
    cycle_text = (
        _span("muted", "cycle time unknown (live push off)")
        if cycle is None
        else _span(None, f"last cycle {duration(cycle)} ago")
    )
    blabel, bcls = _BUDGET_STATE.get(budget["state"], ("OTHER", "muted"))
    errors = poller["errors"]
    limit_hits = (budget.get("exhausted_last_hour") or 0) + (budget.get("rate_limited_last_hour") or 0)
    stats = [
        (errors["last_hour"], "poll errors, 1 h", "bad" if errors["last_hour"] else ""),
        (errors["last_day"], "poll errors, 24 h", ""),
        (budget.get("deferrals_last_hour"), "Linear deferrals, 1 h",
         "warn" if budget.get("deferrals_last_hour") else ""),
        (limit_hits, "Linear limit hits, 1 h", "bad" if limit_hits else ""),
    ]
    stat_html = "".join(
        f'<div class="stat">{_b(v, c)}{_span(None, text)}</div>'
        for v, text, c in stats
    )
    recent = errors["recent"][:3]
    recent_html = (
        _feed(
            recent,
            "",
            lambda row: _span(
                "ticket",
                row["repo"] if row["repo"] == "poll" or row["ticket_id"] == "-" else _ticket(row),
            )
            + _span("what", row["error"])
            + _when(row["age_seconds"], suffix=" ago"),
        )
        if recent
        else ""
    )
    inner = (
        f'<div class="row">{_span("label", "poll")}{_chip(label, cls)}{cycle_text}</div>'
        f'<div class="row">{_span("label", "linear")}{_chip(blabel, bcls)}'
        + _span(
            "muted",
            f"{_num(budget.get('requests_per_hour'))} req/h · "
            f"{_num(budget.get('complexity_per_hour'))} complexity/h",
        )
        + "</div>"
        f'<div class="stats">{stat_html}</div>'
        f'<p class="muted">last poller activity {duration(poller["last_activity_age_seconds"])} ago</p>'
        + recent_html
    )
    return _panel("health", "Poll and Linear budget", inner)


def _savings(snapshot: dict) -> str:
    savings = snapshot["handoff_savings"]
    share = savings["scripted_share"]
    pct = None if share is None else round(share * 100)
    lines = "".join(
        f'<div class="row">{_b(v, c)}{_span("muted", text)}</div>'
        for v, text, c in (
            (savings["scripted"], "scripted handoffs", "accent"),
            (savings["scripted_in_flight"], "scripted in flight", ""),
            (savings["bot_to_bot"], "bot-to-bot (grok wakes)", ""),
        )
    )
    total = (savings["scripted"] or 0) + (savings["bot_to_bot"] or 0)
    meter = (
        f'<i class="scripted" style="width:{100 * savings["scripted"] / total:g}%"></i>'
        f'<i class="bots" style="width:{100 * savings["bot_to_bot"] / total:g}%"></i>'
        if total
        else ""
    )
    inner = (
        f'<div class="gauge"><div class="ring" style="--p:{0 if pct is None else pct}">'
        f'<span>{"—" if pct is None else f"{pct}%"}</span></div><div>{lines}</div></div>'
        f'<div class="meter">{meter}</div>'
        f'<p class="definition" id="share-definition">{_e(savings["scripted_share_definition"])}</p>'
        "<details><summary>how each count is made</summary>"
        f'<p class="muted">{_e(savings["definition"])}</p></details>'
    )
    return _panel("savings", "Handoff savings", inner)


# --- job page ---------------------------------------------------------------------


def _active_jobs(snapshot: dict) -> list[tuple[dict, str]]:
    return [(row, "running") for row in snapshot["running"]] + [
        (row, "queued") for row in snapshot["queued"]
    ]


def _jobs(snapshot: dict) -> str:
    def draw(item: tuple[dict, str]) -> str:
        row, state = item
        return (
            f'<a class="queue-item" href="#{anchor(row["job"])}">'
            f'{_bot(row["recipient"])}{_span("ticket", _job_label(row))}'
            f'{_span("what", row["action"])}{_chip(state, "accent" if state == "running" else "warn")}</a>'
        )

    count = f"{len(snapshot['running'])} running · {len(snapshot['queued'])} queued"
    return _panel(
        "jobs", "Jobs", _feed(_active_jobs(snapshot), "no running or queued jobs", draw),
        count=count,
    )


def _legend(chart: dict) -> str:
    keys = _span("key path", "path taken") + "".join(
        _span(f"key g-{item['group']}", item["name"]) for item in chart["legend"]
    )
    return f'<p class="legend">{keys}</p>'


def _job_article(row: dict, state: str, chart: dict | None) -> str:
    flow = row.get("flow") or {"state": None, "path": [], "state_age_seconds": None}
    kind = _job_kind(row)
    rnd = "—" if row.get("round") is None else row["round"]
    timing = (
        _span("muted", f"T+ {duration(row['elapsed_seconds'])}")
        if state == "running"
        else _span("muted", f"waiting {duration(row['age_seconds'])}")
    )
    where = (
        f'<p class="where"><span>now </span><b>{_e(flow["state"] or "state not recorded")}</b>'
        f"<span> · round {_e(rnd)}</span>"
        + (
            f"<span> · </span><span>{duration(flow['state_age_seconds'])} in {_e(flow['state'])}</span>"
            if flow["state"] and flow["state_age_seconds"] is not None
            else ""
        )
        + "</p>"
    )
    path = " → ".join(flow["path"]) if flow["path"] else "—"
    if chart is None:
        diagram = '<div class="flowbox"><p class="empty">statechart unavailable</p></div>'
        legend = ""
    else:
        diagram = (
            '<div class="flowbox">'
            + render_svg(chart, flow["path"], flow["state"], label=f"{_job_label(row)} on the ticket statechart")
            + "</div>"
        )
        legend = _legend(chart)
    count = f"{state} · job {row['job']}"
    return (
        f'<article class="panel jobpage" id="{anchor(row["job"])}">'
        f'<h2>{_span(None, _job_label(row))}{_span("count", count)}</h2>'
        f'<div class="row">{_bot(row["recipient"])}'
        + (_span("kind", kind) if kind else "")
        + _span("ticket", _job_label(row))
        + _span("what", f"step {row['action']}")
        + timing
        + _state_chip(row)
        + (_chip("OVERDUE", "bad") if row.get("overdue") else "")
        + "</div>"
        + where
        + f'<p class="path">path: {_e(path)}</p>'
        + diagram
        + legend
        + "</article>"
    )


def _jobview(snapshot: dict) -> str:
    chart = snapshot.get("statechart")
    jobs = _active_jobs(snapshot)
    inner = (
        "".join(_job_article(row, state, chart) for row, state in jobs)
        if jobs
        else '<p class="empty">no running or queued jobs</p>'
    )
    return f'<div id="jobview">{inner}</div>'


def _flow_template(snapshot: dict) -> str:
    chart = snapshot.get("statechart")
    if chart is None:
        return ""
    return f'<div id="flow-template" hidden>{render_svg(chart)}</div>'


_PANELS = {
    "index.html": (_kpis, _lanes, _queue),
    "activity.html": (_attention, _handoffs, _verdicts, _tickets, _log),
    "efficiency.html": (_health, _savings),
    "job.html": (_jobs, _jobview),
}


def _page(snapshot: dict, name: str) -> str:
    page, label = _PAGE_NAMES[name]
    refresh = int(snapshot["refresh_seconds"])
    panels = "".join(draw(snapshot) for draw in _PANELS[name])
    extra = _flow_template(snapshot) if name == "job.html" else ""
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
            f"<style>{CSS}</style></head>",
            f'<body data-page="{page}" '
            f'data-generated="{float(snapshot["generated_at_epoch"]):.3f}" '
            f'data-refresh="{refresh}">',
            _bar(snapshot, name),
            f'<main id="hud">{panels}</main>',
            extra,
            f"<script>{page_script()}</script>",
            "</body></html>",
        ]
    ) + "\n"


def render_pages(snapshot: dict) -> dict[str, str]:
    """Every page, by file name. Every value is escaped and already redacted."""
    return {name: _page(snapshot, name) for name in PAGES}
