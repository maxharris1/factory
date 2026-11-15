"""Helpers every page uses, the base CSS and the script runtime (head and tail)."""

from __future__ import annotations

import html
import re

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
#kpis,#ticket-filter,#pipeline,#shipped,#tickets,#attention,#jobview,#components,#no-waiter{grid-column:1/-1}#lanes{grid-column:span 9}#queue{grid-column:span 3}
#handoffs,#verdicts{grid-column:span 6}
.panel h2{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin:0 0 12px;
font:600 13px/1.3 var(--mono);letter-spacing:-.01em;color:var(--text)}
.panel h2>span:first-child::before{content:"";display:inline-block;width:7px;height:7px;margin-right:9px;
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
"""


CSS_TAIL = """\
@keyframes px-march{to{background-position:16px 0,-16px 100%,100% 16px,0 -16px}}
@keyframes shimmer{from{background-position:100% 0}to{background-position:-100% 0}}
@keyframes flash{from{box-shadow:0 0 0 1px var(--brand),0 0 0 4px var(--brand-soft)}to{box-shadow:var(--elev)}}
@media (max-width:1100px){.panel,#lanes,#queue,#handoffs,#verdicts{grid-column:1/-1}}
@media (max-width:640px){.top-row{padding:8px 14px}.attn{padding:6px 14px}main{padding:14px 12px 24px;gap:12px}
.topbar .meta{margin-left:0}}
@media (prefers-reduced-motion: reduce){
*,*::before,*::after{animation: none !important;transition: none !important;scroll-behavior:auto !important}
.fresh{outline:2px solid var(--brand);outline-offset:-2px}
.px-held::after{animation:none}}
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
  var everOk = false;
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
  var modal = null;
  var trails = [];
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
  function jobLabel(r) { return r.tracker === 'linear' ? String(r.ticket_id) + prOf(r, r.ticket_id) : ticketOf(r); }
  function jobKind(r) { return r.tracker === 'github' ? 'PR' : r.tracker === 'linear' ? 'ticket' : null; }
  function anchor(job) { return 'job-' + String(job).replace(/[^A-Za-z0-9_-]/g, '-'); }
  function bot(name) { return el('span', 'bot ' + (BOTS[name] ? name : 'other'), name); }
  function chip(text, cls) { return el('span', 'chip ' + (cls || 'muted'), text); }
  // #92: only a link into the Health tab, never a value used as a URL.
  function healthLink(href) { return /^health\.html#[A-Za-z0-9_-]+$/.test(String(href)) ? String(href) : 'health.html'; }
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
    main: [renderKpis, renderLinearWebhook, renderFeeder, renderLanes, renderQueue],
    job: [renderBoard, renderTickets, renderJobView],
    activity: [renderAttention, renderHandoffs, renderVerdicts],
    efficiency: [renderSavings, renderJobsLifetime, renderRounds, renderWorking, renderHeadroom, renderTokens],
    health: [renderComponents, renderDrift, renderHealth, renderLog, renderRules, renderNoWaiter]
  };
  var RENDERERS = [renderBand, renderAttentionStrip, renderStatusLine].concat(PAGES[PAGE] || []);

  function render(d) {
    latest = d;
    tickers = [];
    nextKeys = {};
    RENDERERS.forEach(function (draw) {
      try { draw(d); } catch (err) { if (window.console) console.error('dashboard panel', err); }
    });
    if (modal) {
      try { fillModal(); } catch (err) { if (window.console) console.error('dashboard modal', err); }
    }
    scrollTrails();
    PX.bind();
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

  function updateLink() {
    var link = byId('link');
    var text = byId('link-text');
    var age = since();
    var state;
    var label;
    if (!everOk) { state = 'boot'; label = failures ? 'NO LINK · static view' : 'ACQUIRING LINK'; }
    else if (Date.now() - lastOk > LINK_LOST_MS) { state = 'lost'; label = 'LINK LOST'; }
    else if (age > STALE_SECONDS) { state = 'stale'; label = 'STALE'; }
    else { state = 'live'; label = 'LIVE'; }
    if (link) link.className = 'link ' + state;
    if (text) text.textContent = label;
    var caret = byId('caret');
    if (caret && state !== caretState) {
      caretState = state;
      caret.replaceChildren(PX.strip('caret', {state: 'link-' + state, height: 12}));
      PX.bind();
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


def _job_label(row: dict) -> str:
    if row.get("tracker") == "linear":
        pr = row.get("pr")
        return f"{row['ticket_id']} (PR #{pr})" if pr else str(row["ticket_id"])
    return _ticket(row)


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


def _panel(
    ident: str, title: str, inner: str, *, count: object = None, extra: str = "", hidden: bool = False
) -> str:
    head = _span(None, title) + ("" if count is None else _span("count", count))
    classes = f"panel {extra}".strip()
    return (
        f'<section class="{classes}" id="{ident}" aria-label="{_e(title)}"{" hidden" if hidden else ""}>'
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


def _td(value: object, cls: str | None = None) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<td{attr}>{_e(value)}</td>"
