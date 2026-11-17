"""Helpers every page uses, the base CSS and the script runtime (head and tail)."""

from __future__ import annotations

import html
import re
import urllib.parse

from lib.pr_title import label


CSS_BASE = """
:root{color-scheme:dark;
--bg:#1a1b26;--dim:#13141c;--panel:#1f2230;--panel-hi:#24283b;--field:#0e0e14;--sel:#292e42;
--line:#24283b;--line-hi:#414868;
--text:#c0caf5;--text-2:#a9b1d6;--muted:#8b93b8;
--brand:#9ece6a;--brand-soft:#9ece6a1f;--ink:#0c0e10;
--hdr-text:#ced8ff;--hdr-text-2:#b7bfea;--hdr-brand:#acdc7e;
--accent:#7dcfff;--ok:#9ece6a;--warn:#ff9e64;--bad:#f7768e;
--claude:#e0af68;--codex:#7aa2f7;--cursor:#bb9af7;--grok:#73daca;
--elev:0 0 0 1px rgba(255,255,255,.07);--elev-hi:0 0 0 1px rgba(255,255,255,.13);
--ease:cubic-bezier(.4,0,.2,1);
--sans:"Geist Variable",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:"JetBrains Mono Variable",ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}
html{background:var(--bg);scrollbar-color:rgba(255,255,255,.14) transparent}
body{display:flex;flex-direction:column;margin:0;min-height:100vh;color:var(--text);background:var(--bg);
font:14px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
::selection{background:var(--sel);color:var(--text)}
a{color:inherit;transition:color .15s var(--ease),background-color .15s var(--ease),box-shadow .15s var(--ease)}
"""


CSS_PANEL = """\
.panel{grid-column:span 4;position:relative;min-width:0;padding:14px 16px 16px;
background:var(--panel);box-shadow:var(--elev);transition:box-shadow .15s var(--ease)}
.panel:hover{box-shadow:var(--elev-hi)}
#ticket-filter,#pipeline,#shipped,#tickets,#attention,#jobview,#components,#broken,#waiters{grid-column:1/-1}#lanes{grid-column:span 9}#queue{grid-column:span 3}
.panel h1,.panel h2{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin:0 0 12px;
font:600 13px/1.3 var(--mono);letter-spacing:-.01em;color:var(--text)}
.panel h1>span:first-child::before,.panel h2>span:first-child::before{content:"";display:inline-block;width:7px;height:7px;margin-right:9px;
vertical-align:.12em;background:var(--brand)}
.panel h2 .count{color:var(--muted);font-weight:400;font-size:12px}
"""


CSS_PARTS = """\
.bot,.chip,.kind{display:inline-block;padding:0 6px;border:1px solid currentColor;
border-color:color-mix(in srgb,currentColor 40%,transparent);background:color-mix(in srgb,currentColor 9%,transparent);
font:500 11px/1.6 var(--mono);letter-spacing:.02em;white-space:nowrap}
.bot{text-transform:lowercase}
.bot.claude{color:var(--claude)}.bot.codex{color:var(--codex)}.bot.cursor{color:var(--cursor)}
.bot.grok{color:var(--grok)}.bot.other{color:var(--muted)}
.chip{text-transform:lowercase}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.accent{color:var(--accent)}.muted{color:var(--muted)}
.feed{margin:0;padding:0;list-style:none}
.feed li{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;padding:7px 6px;border-bottom:1px solid var(--line);
transition:background-color .15s var(--ease)}
.feed li:hover{background:var(--panel-hi)}
.feed li:last-child{border-bottom:0}
.feed .ticket{font:600 13px/1.25 var(--mono)}.feed .what{color:var(--text-2);font-size:12px}
.feed .when{margin-left:auto;font:12px/1.2 var(--mono);color:var(--muted)}
.more{margin:8px 0 0;font:12px/1.4 var(--mono);color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin:8px 0}
.stat{padding:8px 10px;background:var(--bg);box-shadow:var(--elev)}
.stat b{display:block;font:600 18px/1.25 var(--mono);font-variant-numeric:tabular-nums}.stat span{font-size:12px;color:var(--muted)}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:4px 0}
.row .label{min-width:92px;font:500 12px/1.2 var(--mono);color:var(--muted)}
.empty{margin:4px 0;color:var(--muted);font:12px/1.5 var(--mono)}
.gh{display:inline-flex;gap:8px}
.gh a{font:500 11px/1.6 var(--mono);color:var(--accent);text-decoration:none;white-space:nowrap}
.gh a:hover{text-decoration:underline}
.fresh{animation:flash 2.4s ease-out}
details summary{cursor:pointer;color:var(--text-2);font:500 12px/1.8 var(--mono)}
details summary:hover{color:var(--text)}
details summary:focus-visible,a:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
pre{margin:8px 0 0;padding:10px 12px;max-height:340px;overflow:auto;background:var(--field);box-shadow:var(--elev);
color:var(--text-2);font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;margin:0 0 8px}
th,td{padding:6px 12px 6px 0;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font:500 12px/1.4 var(--mono)}
td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
td.bad{color:var(--bad);font-weight:600}
#static{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:12px 24px 32px}
#static h1{font:600 22px/1.3 var(--mono);letter-spacing:-.02em;color:var(--text)}
#static h2{margin:24px 0 8px;font:600 14px/1.3 var(--mono);color:var(--text)}
#static h2::before{content:"";display:inline-block;width:7px;height:7px;margin-right:9px;vertical-align:.12em;background:var(--brand)}
#static .tiles{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
#static .tile{min-width:120px;padding:10px 14px;background:var(--panel);box-shadow:var(--elev);color:var(--muted)}
#static .tile b{display:block;font:600 22px/1.2 var(--mono);color:var(--text)}
#static .meta{color:var(--muted)}#static .stale{color:var(--bad);font-weight:600}
"""


CSS_MISC = """\
.definition{margin:8px 0 4px;color:var(--text-2)}
[hidden]{display:none !important}
.vh,.skip:not(:focus){position:absolute;width:1px;height:1px;margin:-1px;padding:0;border:0;overflow:hidden;
clip:rect(0 0 0 0);white-space:nowrap}
.skip:focus{position:fixed;top:8px;left:8px;z-index:100;padding:6px 10px;background:var(--panel);
color:var(--text);box-shadow:var(--elev);font:600 13px/1.2 var(--mono)}
#hud:focus{outline:none}
"""


CSS_TAIL = """\
@keyframes shimmer{from{background-position:100% 0}to{background-position:-100% 0}}
@keyframes flash{from{box-shadow:0 0 0 1px var(--brand),0 0 0 4px var(--brand-soft)}to{box-shadow:var(--elev)}}
@media (max-width:1100px){.panel,#lanes,#queue{grid-column:1/-1}}
@media (max-width:640px){.top-row{padding:8px 14px}main{padding:14px 12px 24px;gap:12px}
.topbar .meta{margin-left:0}}
@media (prefers-reduced-motion: reduce){
*,*::before,*::after{animation: none !important;transition: none !important;scroll-behavior:auto !important}
.fresh{outline:2px solid var(--brand);outline-offset:-2px}}
"""


SCRIPT_HEAD = r"""
(function () {
  'use strict';
  // The vendored fonts travel inside the page as base64. A FontFace built
  // from bytes makes no request, so nothing is fetched and the static
  // server's CSP needs no font-src. Without FontFace the system fonts stay.
  var FONTS = __FONTS__;
  if (typeof FontFace !== 'function' || !document.fonts || typeof atob !== 'function') return;
  FONTS.forEach(function (font) {
    try {
      var raw = atob(font.data);
      var bytes = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      var face = new FontFace(font.family, bytes, {weight: font.weight, style: 'normal', display: 'swap'});
      document.fonts.add(face);
      var loading = face.load();
      if (loading && loading.catch) loading.catch(function () {});
    } catch (err) { /* keep the fallback font */ }
  });
})();
(function () {
  'use strict';
  var POLL_MS = __POLL_MS__;
  var FLOWMAP = __FLOWMAP__;
  var PX = __PIXELSTRIP__;
  // The shell's link table and the components that carry a code version.
  var SHELL = __SHELL__;
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
  var shownAt = Date.now();
  var everOk = false;
  var resuming = false;
  var failures = 0;
  var inflight = false;
  var timer = null;
  var reloadArmed = false;
  var firstRender = true;
  var latest = null;
  var healthKey = '';
  var tickers = [];
  var prevKeys = {};
  var nextKeys = {};
  var openDetails = {};
  var caretState = 'boot';

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
  // #115: the source issue first, the PR second.
  function prOf(row, first) { return row.pr && String(row.pr) !== String(first) ? ' (PR #' + row.pr + ')' : ''; }
  function ticketIn(repo, row) {
    var first = row.source ? String(row.source).replace(/^#/, '') : row.ticket_id;
    return repo + '#' + first + prOf(row, first);
  }
  function ticketOf(row) { return ticketIn(short(row.repo), row); }
  function ghRefs(r) {
    var repo = String(r.repo === null || r.repo === undefined ? '' : r.repo);
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) return null;
    var issue = /^#[0-9]+$/.test(String(r.source)) ? String(r.source).slice(1) : null;
    var pr = r.pr !== null && r.pr !== undefined && /^[0-9]+$/.test(String(r.pr)) ? String(r.pr) : null;
    return issue || pr ? {repo: repo, issue: issue, pr: pr} : null;
  }
  function ghLinks(r) {
    var refs = ghRefs(r);
    if (!refs) return null;
    var box = el('span', 'gh');
    [['issues', refs.issue, 'issue ↗'], ['pull', refs.pr, 'PR ↗']].forEach(function (g) {
      if (!g[1]) return;
      var a = add(box, link(null, 'https://github.com/' + refs.repo + '/' + g[0] + '/' + g[1]));
      a.setAttribute('rel', 'noopener noreferrer');
      a.setAttribute('target', '_blank');
      a.textContent = g[2];
    });
    return box;
  }
  function addGh(parent, r) { var gh = ghLinks(r); if (gh) add(parent, gh); }
  function jobLabel(r) { return r.tracker === 'linear' ? String(r.ticket_id) + prOf(r, r.ticket_id) : ticketOf(r); }
  function tileLabel(r) {
    if (r.tracker === 'linear') return String(r.ticket_id);
    return short(r.repo) + '#' + (r.source ? String(r.source).replace(/^#/, '') : r.ticket_id);
  }
  function jobKind(r) { return r.tracker === 'github' ? 'PR' : r.tracker === 'linear' ? 'ticket' : null; }
  function anchor(job) { return 'job-' + String(job).replace(/[^A-Za-z0-9_-]/g, '-'); }
  function threadKey(r) { return String(r.repo).toLowerCase() + '#' + r.ticket_id; }
  function jobHrefs(d) {
    var hrefs = {}, best = {};
    (d.running || []).forEach(function (r) {
      var key = threadKey(r);
      if (!best[key] || (r.elapsed_seconds || 0) < (best[key].elapsed_seconds || 0)) best[key] = r;
    });
    (d.queued || []).forEach(function (r) { if (!best[threadKey(r)]) best[threadKey(r)] = r; });
    Object.keys(best).forEach(function (key) { hrefs[key] = 'job.html#' + anchor(best[key].job); });
    return hrefs;
  }
  function ticketHref(hrefs, r) { return hrefs[threadKey(r)] || 'job.html'; }
  function ticketAddress(r) { return String(r.repo) + '#' + r.ticket_id; }
  function ticketPageHref(r) { return 'ticket.html#t=' + encodeURIComponent(ticketAddress(r)); }
  function hashAddress() {
    var hash = String(location.hash || '');
    if (hash.indexOf('#t=') !== 0) return null;
    try { return decodeURIComponent(hash.slice(3)) || null; } catch (err) { return null; }
  }
  function bot(name) { return el('span', 'bot ' + (BOTS[name] ? name : 'other'), name); }
  function chip(text, cls) { return el('span', 'chip ' + (cls || 'muted'), text); }
  function checksNamed(ci) { return ci.checks.join(', ') || 'checks pending'; }
  function checksHref(t) {
    var refs = ghRefs(t);
    return refs && refs.pr ? 'https://github.com/' + refs.repo + '/pull/' + refs.pr + '/checks' : null;
  }
  // #92: only a link into the Health tab, never a value used as a URL.
  function healthLink(href) { return /^health\.html#[A-Za-z0-9_-]+$/.test(String(href)) ? String(href) : 'health.html'; }
  // #75: lib/job_state.py's state of a running job, as a chip.
  var JOB_STATE = {claimed: ['claimed, not started', 'warn'], stale: ['stale heartbeat', 'warn'],
                   dead: ['runner dead', 'bad']};
  function stateChip(r) {
    var s = JOB_STATE[r.job_state];
    return s ? chip(s[0], s[1]) : null;
  }
  function ticking(node, base, prefix, suffix, attr) {
    prefix = prefix || '';
    suffix = suffix || '';
    var put = attr ? function (text) { node.setAttribute(attr, text); } : function (text) { node.textContent = text; };
    if (base === null || base === undefined) { put(prefix + '—'); return node; }
    var item = {run: function (d) { put(prefix + dur(base + d) + suffix); }};
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
  function panel(id, title, count, headingId) {
    var root = byId(id);
    if (!root) return null;
    root.replaceChildren();
    var head = add(root, el('h2'));
    if (headingId) head.setAttribute('id', headingId);
    add(head, el('span', null, title));
    if (count !== undefined && count !== null) add(head, el('span', 'count', count));
    return root;
  }
  function details(root, id, summary, cls) {
    var box = add(root, el('details', cls));
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
"""


SCRIPT_TELEMETRY = r"""
__TELEMETRY__
"""


SCRIPT_TAIL = r"""
  var PAGES = {
    main: [renderLanes, renderJustHappened, renderIntake],
    job: [renderBoard, renderTickets, renderJobView],
    activity: [renderAttention, renderRelay],
    efficiency: [renderHeadroom, renderTokens, renderRounds, renderSavings, renderJobsLifetime],
    health: [renderBroken, renderComponents, renderDrift, renderHealth, renderLog, renderRules, renderNoWaiter],
    ticket: [renderTicketView]
  };
  var RENDERERS = [renderBand, renderAttentionStrip, renderStatusLine].concat(PAGES[PAGE] || []);

  function nodeKey(node) {
    var parts = [];
    var attrs = node.attributes;
    for (var i = 0; i < attrs.length; i++) {
      var name = attrs[i].name;
      if (name === 'id' || name === 'href' || name.slice(0, 5) === 'data-') parts.push(name + '=' + attrs[i].value);
    }
    if (parts.length) return node.tagName + '[' + parts.sort().join('][') + ']';
    var cls = (node.getAttribute('class') || '').split(' ').filter(function (c) { return c && c !== 'fresh'; });
    return node.tagName + '.' + (cls[0] || '');
  }
  function locator(node) {
    var chain = [], keys = [];
    for (var n = node; n && n !== body; n = n.parentNode) { chain.push(n); keys.push(nodeKey(n)); }
    chain.push(body);
    return {chain: chain, keys: keys, text: node.textContent};
  }
  function twins(parent, keys, k, hits) {
    var kids = parent.children;
    for (var i = 0; i < kids.length; i++) {
      if (nodeKey(kids[i]) !== keys[k]) continue;
      if (k === 0) hits.push(kids[i]); else twins(kids[i], keys, k - 1, hits);
    }
  }
  function keyed(key) { return key.indexOf('[') > 0; }
  function owner(node, root) {
    for (var n = node.parentNode; n !== root; n = n.parentNode) if (keyed(nodeKey(n))) return nodeKey(n);
    return null;
  }
  function owned(node, key, mine, root, hits) {
    var kids = node.children;
    for (var i = 0; i < kids.length; i++) {
      if (nodeKey(kids[i]) === key && owner(kids[i], root) === mine) hits.push(kids[i]);
      owned(kids[i], key, mine, root, hits);
    }
    return hits;
  }
  function locate(at) {
    var i = 0;
    while (!at.chain[i].isConnected) i++;
    if (!i) return {root: at.chain[0], node: at.chain[0]};
    var hits = [];
    twins(at.chain[i], at.keys, i - 1, hits);
    if (!hits.length) {
      var mine = at.keys.slice(1, i).filter(keyed)[0] || null;
      hits = owned(at.chain[i], at.keys[0], mine, at.chain[i], []);
    }
    var same = hits.filter(function (h) { return h.textContent === at.text; });
    return {root: at.chain[i], node: same[0] || (hits.length === 1 ? hits[0] : null)};
  }
  function scrolled(node, out) {
    var kids = node.children;
    for (var i = 0; i < kids.length; i++) {
      var c = kids[i];
      if (c.scrollLeft || c.scrollTop) out.push({at: locator(c), left: c.scrollLeft, top: c.scrollTop});
      scrolled(c, out);
    }
    return out;
  }
  function keepPlace(redraw) {
    var active = document.activeElement;
    var focus = active && active !== body ? locator(active) : null;
    var boxes = scrolled(body, []);
    redraw();
    boxes.forEach(function (b) {
      var box = locate(b.at).node;
      if (box) { box.scrollLeft = b.left; box.scrollTop = b.top; }
    });
    if (!focus) return;
    if (active.isConnected) {
      // A kept node (the GitHub comment box) loses focus while it is detached.
      if (document.activeElement !== active) active.focus({preventScroll: true});
      return;
    }
    var found = locate(focus);
    var target = found.node;
    if (!target) {
      for (target = found.root; target && target !== body && target.hidden; target = target.parentNode) {}
      if (!target || target === body) return;
      if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
    }
    target.focus({preventScroll: true});
  }

  function render(d) {
    latest = d;
    tickers = [];
    nextKeys = {};
    keepPlace(function () {
      RENDERERS.forEach(function (draw) {
        try { draw(d); } catch (err) { if (window.console) console.error('dashboard panel', err); }
      });
      PX.bind();
    });
    prevKeys = nextKeys;
    firstRender = false;
    healthKey = agedHealth(d).key;
  }

  function revealHash() {
    var target = byId(String(location.hash || '').slice(1));
    if (!target) return;
    for (var n = target.parentNode; n; n = n.parentNode) if (n.tagName === 'DETAILS') n.open = true;
    target.scrollIntoView();
  }

  var FRESH = {
    boot: {label: null, bar: false, band: true},
    live: {label: 'LIVE', bar: false, band: true},
    stale: {label: 'STALE', bar: true, band: false},
    lost: {label: 'LINK LOST', bar: true, band: false},
    paused: {label: 'PAUSED · resuming', bar: false, band: false}
  };
  var freshState = '';
  var ET = new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit'});
  function staleSeconds() {
    var h = (latest && latest.health) || {};
    var publish = Number(h.publish_seconds) || 0;
    return publish > 0 ? ((h.thresholds || {}).stale_beats || 3) * publish : STALE_SECONDS;
  }
  function freshness() {
    if (!everOk && !failures) return 'boot';
    if (resuming) return 'paused';
    if (Date.now() - Math.max(lastOk, shownAt) > LINK_LOST_MS) return 'lost';
    if (!everOk) return 'boot';
    return since() > staleSeconds() ? 'stale' : 'live';
  }
  function publishedAt() { return ET.format(new Date(generated * 1000)) + ' ET'; }
  function freshBar(state, age) {
    var bar = byId('fresh-bar');
    if (!bar) return;
    var head = byId('fresh-head');
    var text = byId('fresh-text');
    var say = byId('fresh-say');
    if (!FRESH[state].bar) {
      bar.hidden = true;
      if (say) say.textContent = '';
      return;
    }
    var words = state === 'stale'
      ? ['Snapshot is ' + dur(age) + ' old.',
         'The dashboard writer last published at ' + publishedAt() + '. Everything below is as of then.']
      : ['Cannot reach the dashboard.', 'Showing the last snapshot, from ' + publishedAt() + '.'];
    if (head) head.textContent = words[0];
    if (text) text.textContent = words[1];
    if (say && (bar.hidden || bar.className !== 'fresh-bar ' + state)) say.textContent = words[0] + ' ' + words[1];
    bar.className = 'fresh-bar ' + state;
    bar.hidden = false;
  }
  function statusAge(state, age) {
    var line = byId('statusline');
    var seg = line && Array.prototype.filter.call(line.children, function (n) {
      return n.getAttribute('data-seg') === 'age';
    })[0];
    if (!seg) return;
    var text = seg.children[1];
    var on = FRESH[state].bar;
    seg.hidden = !on;
    seg.className = on ? 'sl-age ' + state : 'sl-age';
    if (!text) return;
    text.textContent = on ? 'as of ' + dur(age) + ' ago' : '';
    text.setAttribute('data-short', on ? dur(age).split(' ')[0] + ' old' : '');
  }

  function updateLink() {
    var link = byId('link');
    var text = byId('link-text');
    var age = since();
    var state = freshness();
    var label = FRESH[state].label || (failures ? 'NO LINK · static view' : 'ACQUIRING LINK');
    var caret = byId('caret');
    if (!document.hidden) {
      body.setAttribute('data-fresh', state);
      if (link) link.className = 'link ' + state;
      if (text) text.textContent = label;
      if (caret && state !== caretState) {
        caretState = state;
        caret.replaceChildren(PX.strip('caret', {state: 'link-' + state, height: 12}));
        PX.bind();
      }
      var moved = FRESH[state].band !== (FRESH[freshState] || FRESH.boot).band;
      freshState = state;
      if (moved) { renderBand(latest); PX.bind(); }
      freshBar(state, age);
      statusAge(state, age);
    }
    var ageNode = byId('age');
    if (ageNode) ageNode.textContent = dur(age) + ' ago';
    var clock = byId('clock');
    if (clock) clock.textContent = new Date(Date.now()).toISOString().slice(11, 19) + ' UTC';
  }

  function tick() {
    var d = since();
    tickers.forEach(function (t) { t.run(d); });
    if (latest && agedHealth(latest).key !== healthKey) render(latest);
    updateLink();
  }

  function accept(d) {
    if (!d || typeof d !== 'object' || d.schema !== 'factory-dashboard/v1') { failed(); return; }
    lastOk = Date.now();
    everOk = true;
    resuming = false;
    failures = 0;
    if (firstRender || d.generated_at_epoch !== generated) {
      var first = firstRender;
      generated = Number(d.generated_at_epoch) || generated;
      render(d);
      if (first) revealHash();
    }
    tick();
  }

  function failed() {
    failures += 1;
    resuming = false;
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
    if (document.hidden) return;
    shownAt = Date.now();
    resuming = everOk;
    updateLink();
    if (timer === null && !inflight) poll();
  });
  var skip = byId('skip');
  if (skip) {
    skip.addEventListener('click', function (event) {
      var main = byId('hud');
      if (!main) return;
      event.preventDefault();
      main.focus();
    });
  }
  if (window.addEventListener) {
    window.addEventListener('hashchange', function () { if (latest) { render(latest); tick(); revealHash(); } });
  }
  setInterval(tick, 1000);
  updateLink();
  PX.bind();
  poll();
})();
"""


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


def _ticket(row: dict, *, full: bool = False) -> str:
    repo = str(row["repo"]) if full else _short(row["repo"])
    return label(repo, str(row["ticket_id"]), row.get("source"), row.get("pr"))


_GH_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_GH_NUMBER = re.compile(r"[0-9]+")


def github_refs(row: dict) -> dict | None:
    """The row's GitHub repo, issue and PR numbers (ghRefs in the script), or None."""
    repo, source, pr = str(row.get("repo") or ""), row.get("source"), row.get("pr")
    if not _GH_REPO.fullmatch(repo):
        return None
    issue = source[1:] if isinstance(source, str) and source.startswith("#") and _GH_NUMBER.fullmatch(source[1:]) else None
    pr = str(pr) if pr is not None and _GH_NUMBER.fullmatch(str(pr)) else None
    return {"repo": repo, "issue": issue, "pr": pr} if issue or pr else None


def github_links(row: dict) -> str:
    """The issue and PR links on github.com (ghLinks in the script)."""
    refs = github_refs(row)
    if refs is None:
        return ""
    links = "".join(
        f'<a href="https://github.com/{_e(refs["repo"])}/{path}/{number}" rel="noopener noreferrer" '
        f'target="_blank">{text}</a>'
        for path, number, text in (("issues", refs["issue"], "issue ↗"), ("pull", refs["pr"], "PR ↗"))
        if number
    )
    return f'<span class="gh">{links}</span>'


def _job_label(row: dict) -> str:
    if row.get("tracker") == "linear":
        pr = row.get("pr")
        return f"{row['ticket_id']} (PR #{pr})" if pr else str(row["ticket_id"])
    return _ticket(row)


def _tile_label(row: dict) -> str:
    if row.get("tracker") == "linear":
        return str(row["ticket_id"])
    return label(_short(row["repo"]), str(row["ticket_id"]), row.get("source"), None)


def _job_kind(row: dict) -> str | None:
    return {"github": "PR", "linear": "ticket"}.get(str(row.get("tracker")))


def anchor(job: object) -> str:
    return "job-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(job))


def _thread_key(row: dict) -> str:
    return f"{str(row['repo']).lower()}#{row['ticket_id']}"


def _job_hrefs(snapshot: dict) -> dict[str, str]:
    best: dict[str, dict] = {}
    for row in snapshot["running"]:
        key = _thread_key(row)
        if key not in best or (row.get("elapsed_seconds") or 0) < (best[key].get("elapsed_seconds") or 0):
            best[key] = row
    for row in snapshot["queued"]:
        best.setdefault(_thread_key(row), row)
    return {key: f"job.html#{anchor(row['job'])}" for key, row in best.items()}


def ticket_href(hrefs: dict[str, str], row: dict) -> str:
    return hrefs.get(_thread_key(row), "job.html")


def ticket_address(row: dict) -> str:
    return f"{row['repo']}#{row['ticket_id']}"


ENCODE_URI_COMPONENT_SAFE = "-_.!~*'()"


def ticket_page_href(row: dict) -> str:
    return "ticket.html#t=" + urllib.parse.quote(ticket_address(row), safe=ENCODE_URI_COMPONENT_SAFE)


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


def checks_named(ci: dict) -> str:
    return ", ".join(ci["checks"]) or "checks pending"


def checks_link(ticket: dict) -> str:
    refs = github_refs(ticket)
    if refs is None or refs["pr"] is None:
        return ""
    return f'<a href="https://github.com/{_e(refs["repo"])}/pull/{refs["pr"]}/checks">checks</a>'


def _panel(
    ident: str, title: str, inner: str, *, count: object = None, count_html: str | None = None,
    extra: str = "", hidden: bool = False, heading_id: str | None = None, count_cls: str = "count",
    count_id: str | None = None,
) -> str:
    count_attr = f' id="{count_id}"' if count_id else ""
    head = _span(None, title) + ("" if count is None else f'<span class="{count_cls}"{count_attr}>{_e(count)}</span>')
    if count_html is not None:
        head += f'<span class="count">{count_html}</span>'
    classes = f"panel {extra}".strip()
    heading = f'<h2 id="{heading_id}">' if heading_id else "<h2>"
    return (
        f'<section class="{classes}" id="{ident}" aria-label="{_e(title)}"{" hidden" if hidden else ""}>'
        f"{heading}{head}</h2>{inner}</section>"
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


# #75: lib/job_state.py's state of a running job, as a chip (JOB_STATE in the script).
_JOB_STATE = {
    "claimed": ("claimed, not started", "warn"),
    "stale": ("stale heartbeat", "warn"),
    "dead": ("runner dead", "bad"),
}


def _state_chip(row: dict) -> str:
    found = _JOB_STATE.get(row.get("job_state"))  # type: ignore[arg-type]
    return _chip(found[0], found[1]) if found else ""


def _td(value: object, cls: str | None = None) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<td{attr}>{_e(value)}</td>"
