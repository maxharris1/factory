
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
  var accepted = 0;
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
  function anchor(job) { return 'job-' + String(job).replace(/[^A-Za-z0-9_-]/g, '-'); }
  function threadKey(r) { return String(r.repo).toLowerCase() + '#' + r.ticket_id; }
  function ticketAddress(r) { return String(r.repo) + '#' + r.ticket_id; }
  function ticketPageHref(r, openCi) { return 'ticket.html#t=' + encodeURIComponent(ticketAddress(r)) + (openCi ? '&ci=1' : ''); }
  function hashAddress() {
    var hash = String(location.hash || '');
    if (hash.indexOf('#t=') !== 0) return null;
    try { return decodeURIComponent(hash.slice(3).split('&')[0]) || null; } catch (err) { return null; }
  }
  function hashParam(name) {
    var parts = String(location.hash || '').slice(1).split('&');
    for (var i = 1; i < parts.length; i++) {
      var eq = parts[i].indexOf('=');
      if (eq > 0 && parts[i].slice(0, eq) === name) return parts[i].slice(eq + 1);
    }
    return null;
  }
  function modelEffort(x) { return !x ? '' : [x.model, x.effort].filter(function (v) { return v !== 'unknown'; }).join(' · '); }
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
                   dead: ['runner dead', 'bad'], 'runner-gone': ['runner gone, worker alive', 'warn']};
  function stateChip(r) {
    var s = JOB_STATE[r.stale_cause || r.job_state];
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
