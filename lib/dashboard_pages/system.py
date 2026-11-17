"""health.html (System, #92, #141, #537): Broken now, the components matrix (a device list on a
phone) and process details, version drift when a component is behind, the poll and Linear budget
beside poller.log, rules and limits, and harnesses with no live waiter."""

from __future__ import annotations

import re

from lib.health import CODE_COMPONENTS
from lib.pr_title import label
from lib.dashboard_pages.common import (
    _b, _chip, duration, _e, _feed, _num, _panel, _span, _td, _ticket, _when,
)


CSS = """\
.sys-row{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:16px;min-width:0}
.panel h2 .count.warn{color:var(--warn)}
#health{flex:2 1 520px}#log{flex:3 1 560px}#versions,#rules{grid-column:1/-1}
.sys-scroll{overflow-x:auto}
.sys-table{min-width:560px;margin:0;font:12px/1.5 var(--mono);color:var(--text)}
.sys-table.wide{min-width:860px}
.sys-table tbody tr:last-child td{border-bottom:0}
.sys-table td.sys-kind{font-weight:600}
.sys-cell{display:inline-flex;align-items:center;gap:8px;font-variant-numeric:tabular-nums}
a.sys-cell{text-decoration:none}
.sys-blk{flex:none;width:10px;height:10px;background:var(--muted)}
.sys-blk.ok{background:var(--ok)}.sys-blk.warn{background:var(--warn)}.sys-blk.bad{background:var(--bad)}
.sys-sq{display:inline-block;width:10px;height:10px;vertical-align:-1px;background:var(--muted)}
.sys-sq.ok{background:var(--ok)}
#broken h2>span:first-child::before{background:var(--bad)}
.panel h2 .count.bad{color:var(--bad)}
.sys-table.sys-broken{min-width:820px}
.sys-what{display:inline-flex;align-items:center;gap:10px}
.sys-px{display:block;width:12px}
.sys-name{font-size:13px;font-weight:600;white-space:nowrap}
.sys-broken td.sys-for{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--bad)}
.sys-broken td.sys-seen{white-space:nowrap;color:var(--text-2)}
.sys-broken td.sys-why{font-family:var(--sans);font-size:13px;color:var(--text-2)}
.sys-broken td.muted{white-space:nowrap}
.sys-note{margin:0 0 8px;font:12px/1.5 var(--mono);color:var(--muted)}
.sys-legend{margin:12px 0 0}
.sys-details{margin-top:10px}
.sys-details .sys-scroll{margin-top:6px}
.sys-drift{display:flex;flex-direction:column}
.sys-ver{display:flex;flex-direction:column;gap:6px;padding:10px 0;border-bottom:1px solid var(--line)}
.sys-ver:first-child{padding-top:8px}.sys-ver:last-child{padding-bottom:0;border-bottom:0}
.sys-ver .name{font:600 13px var(--mono);color:var(--text)}
.sys-ver .name.warn{color:var(--warn)}.sys-ver .name.bad{color:var(--bad)}
.sys-ver .blocks{display:flex;flex-wrap:wrap;gap:3px}
.sys-ver .meta{font:12px var(--mono);color:var(--text-2)}
#health .row{margin:0 0 8px;font-size:13px;color:var(--text-2)}
#health .stats{grid-template-columns:repeat(auto-fit,minmax(min(140px,100%),1fr));margin:12px 0 10px}
#health>p.muted{margin:0 0 4px;font:12px var(--mono)}
#health .row>.muted{font:12px var(--mono)}
.sys-log{max-height:300px;overflow:auto;padding:10px 12px;background:var(--field);box-shadow:var(--elev);
font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
.sys-log .start{color:var(--text-2)}
.sys-rules{margin:0;padding:0;list-style:none}
.sys-rules li{display:flex;align-items:baseline;gap:4px 8px;padding:7px 6px;border-bottom:1px solid var(--line)}
.sys-rules li:last-child{border-bottom:0}
.sys-rules .key{flex:none;width:84px;font:500 12px var(--mono);color:var(--muted)}
.sys-rules .text{min-width:0;font-size:13px;color:var(--text-2)}
.sys-sub{margin:16px 0 4px;font:11px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.feed .sys-proj{flex-basis:100%;font:12px var(--mono);color:var(--muted)}
.sys-devices{display:none}
.sys-dev{padding:8px 0;border-bottom:1px solid var(--line)}.sys-dev:last-child{border-bottom:0}
.sys-dev-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:2px}
.sys-dev-head .muted{font:12px var(--mono)}
.sys-dev-row{display:flex;flex-wrap:wrap;align-items:center;gap:2px 10px;padding:3px 0;font:12px/1.5 var(--mono);color:var(--text)}
.sys-dev-row .sys-kind{flex:1 1 auto;min-width:0;overflow-wrap:anywhere}
.sys-dev-row.two .sys-kind{flex-basis:100%;font-weight:600}
.sys-dev-row .sys-seen{color:var(--text-2)}
@media (max-width:699.98px){.sys-matrix{display:none}.sys-devices{display:block}
.sys-table.sys-broken{min-width:0}.sys-broken thead{display:none}
.sys-broken,.sys-broken tbody,.sys-broken tr,.sys-broken td{display:block}
.sys-broken tr{padding:8px 0;border-bottom:1px solid var(--line)}.sys-broken tbody tr:last-child{border-bottom:0}
.sys-broken td{padding:1px 0;border-bottom:0}}
"""


SCRIPT_LOG = r"""
  // --- system page (#141) ------------------------------------------------------

  var LOG_KINDS = [[/exit rc=(?!0\b)\d+/, 'bad'], [/Traceback/, 'bad'], [/\b\w*(?:Error|Exception):/, 'bad'],
                   [/\b(?:ERROR|CRITICAL)\b/, 'bad'], [/\b(?:WARNING|WARN)\b/, 'warn'], [/another orch poll owns/, 'warn'],
                   [/poller start/, 'start']];
  function logKinds(lines) {
    var traceback = false;
    return lines.map(function (line) {
      if (traceback && /^\s/.test(line)) return 'bad';
      traceback = line.indexOf('Traceback') >= 0;
      for (var i = 0; i < LOG_KINDS.length; i++) if (LOG_KINDS[i][0].test(line)) return LOG_KINDS[i][1];
      return null;
    });
  }
  function renderLog(d) {
    var log = d.poller.log;
    var root = panel('log', 'poller.log', log.present ? 'written ' + dur(log.age_seconds) + ' before the snapshot' : 'absent');
    if (!root) return;
    add(root, el('p', 'sys-note', 'last ' + log.tail.length + ' lines, newest first, redacted'));
    var box = add(root, el('div', 'sys-log'));
    var kinds = logKinds(log.tail);
    log.tail.map(function (line, i) { return [line, kinds[i]]; }).reverse()
      .forEach(function (row) { add(box, el('div', row[1], row[0])); });
  }

  function renderRules(d) {
    var root = panel('rules', 'Rules and limits', 'read-only');
    if (!root) return;
    var rules = d.rules || [];
    if (!rules.length) add(root, el('p', 'empty', 'rules unavailable'));
    else {
      var ul = add(root, el('ul', 'sys-rules'));
      rules.forEach(function (r) {
        var li = add(ul, el('li'));
        add(li, el('span', 'key', r.key));
        add(li, el('span', 'text', r.text));
      });
    }
    add(root, el('div', 'sys-sub', 'projects'));
    var projects = d.projects || [];
    if (!projects.length) { add(root, el('p', 'empty', 'no projects loaded')); return; }
    var feed = add(root, el('ul', 'feed'));
    projects.forEach(function (p) {
      var li = add(feed, el('li'));
      add(li, el('span', 'ticket', p.repo));
      add(li, el('span', 'what', p.tracker));
      add(li, el('span', 'sys-proj', projectLine(p)));
    });
  }
  function projectLine(p) {
    if (p.status !== 'ok' || !p.rounds) return 'ship mode and round cap: unknown (' + (p.reason || 'not read') + ')';
    return 'ship mode ' + p.ship + (p.approver ? ' (approver ' + p.approver + ')' : '') +
      ' · round cap ' + p.rounds.low + ' low, ' + p.rounds.high + ' high';
  }
"""


SCRIPT_HEALTH = r"""
  // --- system page: poll and Linear budget ------------------------------------

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
"""


SCRIPT_COMPONENTS = r"""
  // --- health page (#92, #141) -------------------------------------------------

  var HEALTH_STATE = {up: ['UP', 'ok'], stale: ['STALE', 'warn'], down: ['DOWN', 'bad'],
                      unknown: ['UNKNOWN', 'muted']};
  var BROKEN_STATES = [['down', 'bad'], ['stale', 'warn']];
  var BROKEN_TONE = {down: 'bad', stale: 'warn'};
  function matrixKey(c) { return c.bot ? c.component + ' · ' + c.bot : c.component; }
  function validAnchor(v) { return typeof v === 'string' && /^[A-Za-z0-9_-]+$/.test(v) ? v : null; }
  function noWaiterText(n) {
    return 'no waiter for ' + n.harness + ': ' + n.count + ' queued job' + (n.count === 1 ? '' : 's') + ' (' + n.jobs.join(', ') + ')';
  }
  function brokenItems(d) {
    var rows = agedHealth(d).rows.filter(function (c) { return c.configured !== false; });
    var items = [];
    BROKEN_STATES.forEach(function (pair) {
      var state = pair[0];
      rows.forEach(function (c) {
        if (c.state !== state) return;
        var age = c.last_seen_age_seconds === undefined ? null : c.last_seen_age_seconds;
        var anchor = validAnchor(c.anchor);
        items.push({kind: state, what: matrixKey(c), device: c.device, tone: pair[1],
                    for_seconds: age, last_seen_et: c.last_seen_et || null,
                    reason: c.reason || (age === null ? 'never seen' : ''), anchor: anchor ? 'broken-' + anchor : null});
      });
    });
    ((d.health || {}).no_waiter || []).forEach(function (n) {
      var anchor = validAnchor(n.anchor);
      items.push({kind: 'no-waiter', what: 'queued job · ' + n.harness, device: null, tone: 'bad',
                  for_seconds: null, last_seen_et: null, reason: noWaiterText(n), anchor: anchor ? 'broken-' + anchor : null});
    });
    return items;
  }
  function brokenCount(d, items) {
    var down = items.filter(function (x) { return x.kind === 'down'; }).length;
    var stale = items.filter(function (x) { return x.kind === 'stale'; }).length;
    var queued = ((d.health || {}).no_waiter || []).reduce(function (n, w) { return n + w.count; }, 0);
    return [[down, down + ' component' + (down === 1 ? '' : 's') + ' down'],
            [stale, stale + ' component' + (stale === 1 ? '' : 's') + ' stale'],
            [queued, queued + ' queued job' + (queued === 1 ? '' : 's') + ' with no waiter']]
      .filter(function (p) { return p[0]; }).map(function (p) { return p[1]; }).join(' · ');
  }
  function renderBroken(d) {
    var root = byId('broken');
    if (!root) return;
    var items = brokenItems(d);
    root.replaceChildren();
    root.hidden = !items.length;
    if (!items.length) return;
    var head = add(root, el('h2'));
    if (!((d.health || {}).no_waiter || []).length) head.setAttribute('id', 'no-waiter');
    add(head, el('span', null, 'Broken now'));
    add(head, el('span', 'count bad', brokenCount(d, items)));
    var table = add(add(root, el('div', 'sys-scroll')), el('table', 'sys-table sys-broken'));
    var hrow = add(add(table, el('thead')), el('tr'));
    ['what', 'device', 'state', 'for', 'last seen', 'why'].forEach(function (t) { add(hrow, el('th', null, t)); });
    var tbody = add(table, el('tbody'));
    items.forEach(function (x) {
      var tr = add(tbody, el('tr'));
      if (x.anchor) tr.setAttribute('id', x.anchor);
      var what = add(add(tr, el('td')), el('span', 'sys-what'));
      var px = add(what, el('span', 'sys-px'));
      px.setAttribute('aria-hidden', 'true');
      add(px, PX.strip('broken-' + x.kind + '-' + x.what + '-' + (x.device || ''), {effect: 'static', tint: '#f7768e', cell: 4, height: 16}));
      add(what, el('span', 'sys-name', x.what));
      add(tr, el('td', null, x.device === null || x.device === undefined ? '—' : x.device));
      add(add(tr, el('td')), chip(x.kind, x.tone));
      if (x.for_seconds === null) add(tr, el('td', 'muted', '—'));
      else ticking(add(tr, el('td', 'sys-for')), x.for_seconds, '', '');
      add(tr, x.last_seen_et === null ? el('td', 'muted', '—') : el('td', 'sys-seen', x.last_seen_et));
      add(tr, el('td', 'sys-why', x.reason));
    });
  }
  function matrixOf(d, rows) {
    var keys = [], seen = [], cells = {};
    rows.forEach(function (c) {
      var key = matrixKey(c);
      if (keys.indexOf(key) < 0) keys.push(key);
      if (seen.indexOf(c.device) < 0) seen.push(c.device);
      cells[key + '\n' + c.device] = c;
    });
    var listed = ((d.slots || {}).devices || []).map(function (x) { return x.name; });
    var devices = listed.filter(function (n) { return seen.indexOf(n) >= 0; })
      .concat(seen.filter(function (n) { return listed.indexOf(n) < 0; }));
    return {keys: keys, devices: devices, cells: cells};
  }
  function matrixCell(c) {
    var st = HEALTH_STATE[c.state] || ['OTHER', 'muted'];
    var word = st[0].toLowerCase();
    var configured = c.configured !== false;
    var age = c.last_seen_age_seconds;
    var never = age === null || age === undefined;
    if (configured && c.state === 'up') {
      var sq = el('span', 'sys-sq ok');
      sq.setAttribute('role', 'img');
      sq.setAttribute('aria-label', 'up');
      return ticking(sq, age, 'up · beat ', ' ago', 'title');
    }
    var tone = configured ? st[1] : 'muted';
    var anchor = validAnchor(c.anchor);
    var box = configured && anchor && BROKEN_TONE[c.state] ? link('sys-cell ' + tone, '#broken-' + anchor) : el('span', 'sys-cell');
    var blk = add(box, el('span', 'sys-blk ' + tone));
    blk.setAttribute('aria-hidden', 'true');
    var text = add(box, el('span'));
    if (never) text.textContent = word + ' · never seen';
    else ticking(text, age, word + ' ', '');
    return box;
  }
  function isUp(c) { return c.configured !== false && c.state === 'up'; }
  function driftShown(h) { return !!h.known && versionDrift(h).behind.length > 0; }
  function componentsCount(h, rows) {
    if (!h.known) return ['unknown', 'count'];
    var configured = rows.filter(function (c) { return c.configured !== false; });
    var drift = versionDrift(h);
    var last = drift.behind.length ? drift.behind.length + ' behind'
      : drift.newest === 'unknown' ? 'version unknown' : 'all on ' + drift.newest.slice(0, 7);
    return [configured.length + ' configured · ' + configured.filter(isUp).length + ' up · ' + last,
            drift.behind.length ? 'count warn' : 'count'];
  }
  function upFirst(rows) {
    return rows.filter(function (c) { return !isUp(c); }).concat(rows.filter(isUp));
  }
  function deviceRow(c) {
    var st = HEALTH_STATE[c.state] || ['OTHER', 'muted'];
    var configured = c.configured !== false;
    var row = el('div', 'sys-dev-row' + (isUp(c) ? '' : ' two'));
    add(row, el('span', 'sys-kind', matrixKey(c)));
    var cell = add(row, el('span', 'sys-cell'));
    add(cell, el('span', 'sys-blk ' + (configured ? st[1] : 'muted'))).setAttribute('aria-hidden', 'true');
    add(cell, el('span', null, st[0].toLowerCase() + (configured ? '' : ' · not configured')));
    var age = c.last_seen_age_seconds;
    if (age === null || age === undefined) add(row, el('span', 'sys-seen', 'never'));
    else ticking(add(row, el('span', 'sys-seen')), age, '', ' ago');
    return row;
  }
  function renderDevices(root, m) {
    var box = add(root, el('div', 'sys-devices'));
    m.devices.forEach(function (device) {
      var rows = m.keys.map(function (key) { return m.cells[key + '\n' + device]; }).filter(Boolean);
      var block = add(box, el('div', 'sys-dev'));
      var head = add(block, el('div', 'sys-dev-head'));
      add(head, el('span', 'sys-name', device));
      add(head, el('span', 'muted', rows.filter(isUp).length + ' of ' + rows.length + ' up'));
      upFirst(rows).forEach(function (c) { add(block, deviceRow(c)); });
    });
  }
  var PROCESS_RANK = {down: 0, stale: 1};
  function processRank(c) { return PROCESS_RANK.hasOwnProperty(c.state) ? PROCESS_RANK[c.state] : 2; }
  function renderComponents(d) {
    var h = d.health || {known: false, components: []};
    var rows = agedHealth(d).rows;
    var root = panel('components', 'Components', null, brokenItems(d).length ? null : 'no-waiter');
    if (!root) return;
    var count = componentsCount(h, rows);
    var countNode = add(root.children[0], el('span', count[1], count[0]));
    if (!driftShown(h)) countNode.setAttribute('id', 'drift');
    if (!h.known) { add(root, el('p', 'empty', h.error ? 'unknown: ' + h.error : 'no heartbeats recorded yet')); return; }
    var m = matrixOf(d, rows);
    var table = add(add(root, el('div', 'sys-scroll sys-matrix')), el('table', 'sys-table'));
    var head = add(add(table, el('thead')), el('tr'));
    ['component'].concat(m.devices).forEach(function (t) { add(head, el('th', null, t)); });
    var mbody = add(table, el('tbody'));
    m.keys.forEach(function (key) {
      var tr = add(mbody, el('tr'));
      add(tr, el('td', 'sys-kind', key));
      m.devices.forEach(function (device) {
        var c = m.cells[key + '\n' + device];
        if (!c) { add(tr, el('td', 'muted', '—')); return; }
        add(add(tr, el('td')), matrixCell(c));
      });
    });
    renderDevices(root, m);
    var t = h.thresholds || {};
    add(root, el('p', 'sys-note sys-legend', 'up: a beat within ' + (t.stale_beats || 3) + ' intervals (' + (t.beat_seconds || 15) +
      ' s each; device probes every ' + (t.probe_seconds || 60) + ' s) · stale: within ' + (t.down_beats || 8) +
      ' · down: older, stopped or never seen · unknown: this snapshot is too old to tell'));
    var box = details(root, 'process', 'process details', 'sys-details');
    var ptable = add(add(box, el('div', 'sys-scroll')), el('table', 'sys-table wide'));
    var phead = add(add(ptable, el('thead')), el('tr'));
    ['component', 'bot', 'device', 'state', 'last seen (ET)', 'ago', 'pid', 'version', 'started (ET)'].forEach(function (t) { add(phead, el('th', null, t)); });
    var tbody = add(ptable, el('tbody'));
    rows.slice().sort(function (a, b) { return processRank(a) - processRank(b); }).forEach(function (c) {
      var tr = add(tbody, el('tr'));
      tr.setAttribute('id', c.anchor);
      add(tr, el('td', null, c.component));
      add(tr, el('td', null, c.bot || '—'));
      add(tr, el('td', null, c.device));
      var cell = add(tr, el('td'));
      var st = HEALTH_STATE[c.state] || ['OTHER', 'muted'];
      add(cell, chip(st[0], st[1]));
      if (c.reason) add(cell, el('span', 'muted', ' ' + c.reason));
      if (typeof c.last_cycle_seconds === 'number') add(cell, el('span', 'muted', ' last cycle ' + Math.floor(c.last_cycle_seconds) + 's'));
      if (c.configured === false) add(cell, el('span', 'muted', ' (not configured)'));
      add(tr, el('td', null, etCell(c.last_seen_et) || 'never'));
      ticking(add(tr, el('td', 'num')), c.last_seen_age_seconds, '', '');
      add(tr, el('td', 'num', c.pid === null || c.pid === undefined ? '—' : c.pid));
      var ver = add(tr, el('td', null, c.version));
      if (c.stale_code) add(ver, el('span', 'warn', ' stale code'));
      add(tr, el('td', null, etCell(c.started_at_et) || '—'));
      mark('components', c.anchor + ':' + c.state, tr);
    });
  }

  function etCell(v) { return v ? String(v).replace(/ ET$/, '') : v; }

  function driftOf(h) {
    var rows = (h.components || []).filter(function (c) { return c.configured !== false; });
    var groups = {}, order = [];
    rows.forEach(function (c) {
      var v = String(c.version || 'unknown');
      if (!groups[v]) { groups[v] = {version: v, rows: [], started: null, startedEt: null}; order.push(v); }
      var g = groups[v];
      g.rows.push(c);
      if (c.started_at !== null && c.started_at !== undefined && (g.started === null || c.started_at > g.started)) {
        g.started = c.started_at; g.startedEt = c.started_at_et;
      }
    });
    var known = order.filter(function (v) { return v !== 'unknown'; });
    var byStart = known.slice().sort(function (a, b) { return (groups[b].started || 0) - (groups[a].started || 0); });
    var newest = h.code_version && h.code_version !== 'unknown' ? String(h.code_version) : byStart[0] || null;
    var out = [];
    if (newest && groups[newest]) out.push(groups[newest]);
    byStart.forEach(function (v) { if (v !== newest) out.push(groups[v]); });
    if (groups.unknown) out.push(groups.unknown);
    out.forEach(function (g) { g.tone = g.version === newest ? 'ok' : g.version === 'unknown' ? 'bad' : 'warn'; });
    return out;
  }
  function renderDrift(d) {
    var root = byId('versions');
    if (!root) return;
    var h = d.health || {known: false, components: []};
    root.replaceChildren();
    root.hidden = !driftShown(h);
    if (root.hidden) return;
    var section = add(root, el('section', 'panel'));
    section.setAttribute('id', 'drift');
    section.setAttribute('aria-label', 'Version drift');
    var head = add(section, el('h2'));
    add(head, el('span', null, 'Version drift'));
    add(head, el('span', 'count warn', versionDrift(h).behind.length + ' behind'));
    var box = add(section, el('div', 'sys-drift'));
    driftOf(h).forEach(function (g) {
      var row = add(box, el('div', 'sys-ver'));
      add(row, el('span', 'name ' + g.tone, g.version));
      var blocks = add(row, el('span', 'blocks'));
      blocks.setAttribute('aria-hidden', 'true');
      g.rows.forEach(function () { add(blocks, el('span', 'sys-blk ' + g.tone)); });
      var labels = g.rows.map(function (c) { return c.label; }).join(', ');
      add(row, el('span', 'meta', g.tone === 'ok' ? g.rows.length + ' component' + (g.rows.length === 1 ? '' : 's')
        : labels + (g.startedEt ? ' · started ' + g.startedEt : '')));
    });
  }

  function renderNoWaiter(d) {
    var root = byId('waiters');
    if (!root) return;
    var rows = (d.health || {}).no_waiter || [];
    root.replaceChildren();
    root.hidden = !rows.length;
    if (!rows.length) return;
    var section = add(root, el('section', 'panel'));
    section.setAttribute('id', 'no-waiter');
    section.setAttribute('aria-label', 'Queued jobs with no waiter');
    var head = add(section, el('h2'));
    add(head, el('span', null, 'Queued jobs with no waiter'));
    add(head, el('span', 'count', rows.length === 1 ? '1 harness' : rows.length + ' harnesses'));
    var ul = add(section, el('ul', 'feed'));
    rows.forEach(function (n) {
      var li = add(ul, el('li'));
      li.setAttribute('id', n.anchor);
      add(li, chip('no waiter', 'bad'));
      add(li, el('span', 'ticket', 'waiter ' + n.bot));
      add(li, el('span', 'what', noWaiterText(n)));
      mark('no-waiter', n.anchor, li);
    });
  }
"""


_LOG_KINDS = (
    (re.compile(r"exit rc=(?!0\b)\d+"), "bad"),
    (re.compile(r"Traceback"), "bad"),
    (re.compile(r"\b\w*(?:Error|Exception):"), "bad"),
    (re.compile(r"\b(?:ERROR|CRITICAL)\b"), "bad"),
    (re.compile(r"\b(?:WARNING|WARN)\b"), "warn"),
    (re.compile(r"another orch poll owns"), "warn"),
    (re.compile(r"poller start"), "start"),
)


def _log_kinds(lines: list[str]) -> list[str | None]:
    kinds: list[str | None] = []
    traceback = False
    for line in lines:
        if traceback and line[:1].isspace():
            kinds.append("bad")
            continue
        traceback = "Traceback" in line
        kinds.append(next((kind for pattern, kind in _LOG_KINDS if pattern.search(line)), None))
    return kinds


def _log(snapshot: dict) -> str:
    log = snapshot["poller"]["log"]
    count = (
        f"written {duration(log['age_seconds'])} before the snapshot"
        if log["present"]
        else "absent"
    )
    tail = log["tail"]
    lines = "".join(
        f'<div class="{kind}">{_e(line)}</div>' if kind else f"<div>{_e(line)}</div>"
        for line, kind in reversed(list(zip(tail, _log_kinds(tail))))
    )
    inner = (
        f'<p class="sys-note">last {len(log["tail"])} lines, newest first, redacted</p>'
        f'<div class="sys-log">{lines}</div>'
    )
    return _panel("log", "poller.log", inner, count=count)


def _project_line(project: dict) -> str:
    rounds = project.get("rounds")
    if project.get("status") != "ok" or not rounds:
        return f"ship mode and round cap: unknown ({project.get('reason') or 'not read'})"
    approver = f" (approver {project['approver']})" if project.get("approver") else ""
    return (
        f"ship mode {project['ship']}{approver} · "
        f"round cap {rounds['low']} low, {rounds['high']} high"
    )


def _rules(snapshot: dict) -> str:
    rules = snapshot.get("rules") or []
    rows = (
        '<ul class="sys-rules">'
        + "".join(
            f"<li>{_span('key', row['key'])}{_span('text', row['text'])}</li>" for row in rules
        )
        + "</ul>"
        if rules
        else '<p class="empty">rules unavailable</p>'
    )
    projects = snapshot.get("projects") or []
    listing = (
        '<ul class="feed">'
        + "".join(
            "<li>"
            + _span("ticket", p["repo"])
            + _span("what", p["tracker"])
            + _span("sys-proj", _project_line(p))
            + "</li>"
            for p in projects
        )
        + "</ul>"
        if projects
        else '<p class="empty">no projects loaded</p>'
    )
    inner = rows + '<div class="sys-sub">projects</div>' + listing
    return _panel("rules", "Rules and limits", inner, count="read-only")


# --- system page: poll and Linear budget ----------------------------------------------


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


# --- health page (#92, #141) ---------------------------------------------------------


_HEALTH_STATE = {"up": ("UP", "ok"), "stale": ("STALE", "warn"), "down": ("DOWN", "bad"),
                 "unknown": ("UNKNOWN", "muted")}


_ANCHOR = re.compile(r"[A-Za-z0-9_-]+")


def _anchor_attr(value: object) -> str:
    return f' id="{value}"' if isinstance(value, str) and _ANCHOR.fullmatch(value) else ""


def _matrix_key(row: dict) -> str:
    return f"{row['component']} · {row['bot']}" if row.get("bot") else row["component"]


def _valid_anchor(value: object) -> str | None:
    return value if isinstance(value, str) and _ANCHOR.fullmatch(value) else None


_BROKEN_STATES = (("down", "bad"), ("stale", "warn"))
_BROKEN_TONE = dict(_BROKEN_STATES)


def broken_items(health: dict) -> list[dict]:
    rows = [row for row in health.get("components") or [] if row.get("configured") is not False]
    items = []
    for state, tone in _BROKEN_STATES:
        for row in rows:
            if row["state"] != state:
                continue
            age = row.get("last_seen_age_seconds")
            anchor = _valid_anchor(row.get("anchor"))
            items.append({
                "kind": state, "what": _matrix_key(row), "device": row["device"], "tone": tone,
                "for_seconds": age, "last_seen_et": row.get("last_seen_et"),
                "reason": row.get("reason") or ("never seen" if age is None else ""),
                "anchor": f"broken-{anchor}" if anchor else None,
            })
    for row in health.get("no_waiter") or []:
        anchor = _valid_anchor(row.get("anchor"))
        items.append({
            "kind": "no-waiter", "what": f"queued job · {row['harness']}", "device": None,
            "tone": "bad", "for_seconds": None, "last_seen_et": None, "reason": _no_waiter_text(row),
            "anchor": f"broken-{anchor}" if anchor else None,
        })
    return items


def _broken_count(health: dict, items: list[dict]) -> str:
    down = sum(item["kind"] == "down" for item in items)
    stale = sum(item["kind"] == "stale" for item in items)
    queued = sum(row["count"] for row in health.get("no_waiter") or [])
    parts = [
        (down, f"{down} component{'' if down == 1 else 's'} down"),
        (stale, f"{stale} component{'' if stale == 1 else 's'} stale"),
        (queued, f"{queued} queued job{'' if queued == 1 else 's'} with no waiter"),
    ]
    return " · ".join(text for n, text in parts if n)


def _px():
    from lib.dashboard_pages import shell

    return shell


def _broken_row(item: dict) -> str:
    key = f"broken-{item['kind']}-{item['what']}-{item['device'] or ''}"
    strip = _px().px_strip(key, effect="static", tint="#f7768e", cell=4, height=16)
    seconds, seen = item["for_seconds"], item["last_seen_et"]
    return (
        f'<tr{_anchor_attr(item["anchor"])}>'
        f'<td><span class="sys-what"><span class="sys-px" aria-hidden="true">{strip}</span>'
        f'{_span("sys-name", item["what"])}</span></td>'
        + _td(item["device"])
        + f"<td>{_chip(item['kind'], item['tone'])}</td>"
        + (_td(None, "muted") if seconds is None else _td(duration(seconds), "sys-for"))
        + (_td(None, "muted") if seen is None else _td(seen, "sys-seen"))
        + _td(item["reason"], "sys-why")
        + "</tr>"
    )


def _broken(snapshot: dict) -> str:
    health = snapshot.get("health") or {}
    items = broken_items(health)
    if not items:
        return '<section class="panel" id="broken" aria-label="Broken now" hidden></section>'
    heading_id = "" if health.get("no_waiter") else ' id="no-waiter"'
    heads = ("what", "device", "state", "for", "last seen", "why")
    return (
        '<section class="panel" id="broken" aria-label="Broken now">'
        f'<h2{heading_id}>{_span(None, "Broken now")}{_span("count bad", _broken_count(health, items))}</h2>'
        '<div class="sys-scroll"><table class="sys-table sys-broken"><thead><tr>'
        + "".join(f"<th>{text}</th>" for text in heads)
        + "</tr></thead><tbody>"
        + "".join(_broken_row(item) for item in items)
        + "</tbody></table></div></section>"
    )


def _matrix(snapshot: dict, rows: list[dict]) -> tuple[list[str], list[str], dict]:
    """Rows by component kind (a waiter per bot), columns by device in lane
    order, then any device only the heartbeats name (matrixOf in the script)."""
    keys: list[str] = []
    seen: list[str] = []
    cells: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = _matrix_key(row)
        if key not in keys:
            keys.append(key)
        if row["device"] not in seen:
            seen.append(row["device"])
        cells[(key, row["device"])] = row
    listed = [device["name"] for device in (snapshot.get("slots") or {}).get("devices") or []]
    devices = [name for name in listed if name in seen] + [name for name in seen if name not in listed]
    return keys, devices, cells


def _matrix_cell(row: dict) -> str:
    label, tone = _HEALTH_STATE.get(row["state"], ("OTHER", "muted"))
    word = label.lower()
    configured = row.get("configured") is not False
    age = row.get("last_seen_age_seconds")
    if _is_up(row):
        return (f'<td><span class="sys-sq ok" role="img" aria-label="up" '
                f'title="up · beat {duration(age)} ago"></span></td>')
    if not configured:
        tone = "muted"
    text = f"{word} · never seen" if age is None else f"{word} {duration(age)}"
    inner = f'<span class="sys-blk {tone}" aria-hidden="true"></span>{_span(None, text)}'
    anchor = _valid_anchor(row.get("anchor"))
    if configured and anchor and row["state"] in _BROKEN_TONE:
        return f'<td><a class="sys-cell {tone}" href="#broken-{anchor}">{inner}</a></td>'
    return f'<td><span class="sys-cell">{inner}</span></td>'


def _is_up(row: dict) -> bool:
    return row.get("configured") is not False and row["state"] == "up"


def _drift_shown(health: dict) -> bool:
    return bool(health.get("known")) and bool(version_drift(health)["behind"])


def _components_count(health: dict) -> tuple[str, str]:
    if not health.get("known"):
        return "unknown", "count"
    rows = [row for row in health.get("components") or [] if row.get("configured") is not False]
    drift = version_drift(health)
    behind = len(drift["behind"])
    last = (
        f"{behind} behind" if behind
        else "version unknown" if drift["newest"] == "unknown"
        else f"all on {drift['newest'][:7]}"
    )
    up = sum(_is_up(row) for row in rows)
    return f"{len(rows)} configured · {up} up · {last}", "count warn" if behind else "count"


def _device_row(row: dict) -> str:
    label, tone = _HEALTH_STATE.get(row["state"], ("OTHER", "muted"))
    configured = row.get("configured") is not False
    age = row.get("last_seen_age_seconds")
    word = label.lower() + ("" if configured else " · not configured")
    return (
        f'<div class="sys-dev-row{"" if _is_up(row) else " two"}">'
        + _span("sys-kind", _matrix_key(row))
        + f'<span class="sys-cell"><span class="sys-blk {tone if configured else "muted"}" '
        f'aria-hidden="true"></span>{_span(None, word)}</span>'
        + _span("sys-seen", "never" if age is None else f"{duration(age)} ago")
        + "</div>"
    )


def _devices(keys: list[str], devices: list[str], cells: dict) -> str:
    """The matrix as one block per device, abnormal rows first (renderDevices
    in the script); CSS shows it instead of the matrix on a phone."""
    blocks = []
    for device in devices:
        rows = [cells[(key, device)] for key in keys if (key, device) in cells]
        ordered = [row for row in rows if not _is_up(row)] + [row for row in rows if _is_up(row)]
        blocks.append(
            '<div class="sys-dev"><div class="sys-dev-head">'
            + _span("sys-name", device)
            + _span("muted", f"{sum(map(_is_up, rows))} of {len(rows)} up")
            + "</div>"
            + "".join(_device_row(row) for row in ordered)
            + "</div>"
        )
    return f'<div class="sys-devices">{"".join(blocks)}</div>'


_PROCESS_RANK = {"down": 0, "stale": 1}


def _et_cell(value: str | None) -> str | None:
    return value.removesuffix(" ET") if value else value


def _components(snapshot: dict) -> str:
    health = snapshot.get("health") or {"known": False, "components": []}
    rows = health.get("components") or []
    count, count_cls = _components_count(health)
    head = {"count": count, "count_cls": count_cls,
            "count_id": None if _drift_shown(health) else "drift",
            "heading_id": None if broken_items(health) else "no-waiter"}
    if not health.get("known"):
        text = f"unknown: {health['error']}" if health.get("error") else "no heartbeats recorded yet"
        return _panel("components", "Components", f'<p class="empty">{_e(text)}</p>', **head)
    keys, devices, cells = _matrix(snapshot, rows)
    matrix = (
        '<div class="sys-scroll sys-matrix"><table class="sys-table"><thead><tr>'
        + "".join(f"<th>{_e(text)}</th>" for text in ["component", *devices])
        + "</tr></thead><tbody>"
        + "".join(
            f'<tr><td class="sys-kind">{_e(key)}</td>'
            + "".join(
                _matrix_cell(cells[(key, device)]) if (key, device) in cells
                else '<td class="muted">—</td>'
                for device in devices
            )
            + "</tr>"
            for key in keys
        )
        + "</tbody></table></div>"
        + _devices(keys, devices, cells)
    )
    heads = ("component", "bot", "device", "state", "last seen (ET)", "ago", "pid", "version",
             "started (ET)")
    body = []
    for row in sorted(rows, key=lambda row: _PROCESS_RANK.get(row["state"], 2)):
        label, cls = _HEALTH_STATE.get(row["state"], ("OTHER", "muted"))
        reason = _span("muted", " " + row["reason"]) if row.get("reason") else ""
        if row.get("last_cycle_seconds") is not None:
            reason += _span("muted", f" last cycle {int(row['last_cycle_seconds'])}s")
        if row.get("configured") is False:
            reason += _span("muted", " (not configured)")
        body.append(
            f'<tr{_anchor_attr(row.get("anchor"))}>'
            + _td(row["component"])
            + _td(row["bot"] or None)
            + _td(row["device"])
            + f"<td>{_chip(label, cls)}{reason}</td>"
            + _td(_et_cell(row["last_seen_et"]) or "never")
            + _td(duration(row["last_seen_age_seconds"]), "num")
            + _td(row["pid"], "num")
            + (f'<td>{_e(row["version"])}{_span("warn", " stale code")}</td>'
               if row.get("stale_code") else _td(row["version"]))
            + _td(_et_cell(row["started_at_et"]))
            + "</tr>"
        )
    thresholds = health.get("thresholds") or {}
    note = (
        f"up: a beat within {thresholds.get('stale_beats', 3)} intervals "
        f"({thresholds.get('beat_seconds', 15):g} s each; device probes every "
        f"{thresholds.get('probe_seconds', 60):g} s) · stale: within "
        f"{thresholds.get('down_beats', 8)} · down: older, stopped or never seen"
        " · unknown: this snapshot is too old to tell"
    )
    details = (
        '<details class="sys-details"><summary>process details</summary>'
        '<div class="sys-scroll"><table class="sys-table wide"><thead><tr>'
        + "".join(f"<th>{_e(text)}</th>" for text in heads)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div></details>"
    )
    inner = matrix + f'<p class="sys-note sys-legend">{_e(note)}</p>' + details
    return _panel("components", "Components", inner, **head)


def version_groups(health: dict) -> list[dict]:
    """Version drift (SPEC 9: group components by version): the newest
    version first (the checkout's, else the latest started), then the other
    known versions by latest start, then unknown (driftOf in the script)."""
    groups: dict[str, dict] = {}
    for row in health.get("components") or []:
        if row.get("configured") is False:
            continue
        version = str(row.get("version") or "unknown")
        group = groups.setdefault(version, {"version": version, "rows": [], "started": None,
                                            "started_et": None})
        group["rows"].append(row)
        started = row.get("started_at")
        if started is not None and (group["started"] is None or started > group["started"]):
            group["started"], group["started_et"] = started, row.get("started_at_et")
    by_start = sorted(
        (v for v in groups if v != "unknown"),
        key=lambda v: -(groups[v]["started"] or 0),
    )
    code = health.get("code_version")
    newest = str(code) if code and code != "unknown" else (by_start[0] if by_start else None)
    ordered = [groups[newest]] if newest in groups else []
    ordered += [groups[v] for v in by_start if v != newest]
    if "unknown" in groups:
        ordered.append(groups["unknown"])
    for group in ordered:
        version = group["version"]
        group["tone"] = "ok" if version == newest else "bad" if version == "unknown" else "warn"
    return ordered


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


def _drift(health: dict) -> str:
    items = []
    for group in version_groups(health):
        rows = group["rows"]
        tone = group["tone"]
        if tone == "ok":
            meta = f"{len(rows)} component" + ("" if len(rows) == 1 else "s")
        else:
            meta = ", ".join(str(row["label"]) for row in rows)
            if group["started_et"]:
                meta += f" · started {group['started_et']}"
        blocks = "".join(f'<span class="sys-blk {tone}"></span>' for _ in rows)
        items.append(
            '<div class="sys-ver">'
            + _span(f"name {tone}", group["version"])
            + f'<span class="blocks" aria-hidden="true">{blocks}</span>'
            + _span("meta", meta)
            + "</div>"
        )
    count = _span("count warn", f"{len(version_drift(health)['behind'])} behind")
    return (
        '<section class="panel" id="drift" aria-label="Version drift">'
        f'<h2>{_span(None, "Version drift")}{count}</h2>'
        f'<div class="sys-drift">{"".join(items)}</div></section>'
    )


def _versions(snapshot: dict) -> str:
    health = snapshot.get("health") or {}
    if not _drift_shown(health):
        return '<div id="versions" hidden></div>'
    return f'<div id="versions">{_drift(health)}</div>'


def _log_row(snapshot: dict) -> str:
    return f'<div class="sys-row">{_health(snapshot)}{_log(snapshot)}</div>'


def _no_waiter_text(row: dict) -> str:
    return (
        f"no waiter for {row['harness']}: {row['count']} queued job"
        + ("" if row["count"] == 1 else "s")
        + f" ({', '.join(row['jobs'])})"
    )


def _no_waiter(snapshot: dict) -> str:
    rows = (snapshot.get("health") or {}).get("no_waiter") or []
    if not rows:
        return '<div id="waiters" hidden></div>'
    items = "".join(
        f'<li{_anchor_attr(row.get("anchor"))}>'
        + _chip("no waiter", "bad")
        + _span("ticket", f"waiter {row['bot']}")
        + _span("what", _no_waiter_text(row))
        + "</li>"
        for row in rows
    )
    panel = _panel("no-waiter", "Queued jobs with no waiter", f'<ul class="feed">{items}</ul>',
                   count="1 harness" if len(rows) == 1 else f"{len(rows)} harnesses")
    return f'<div id="waiters">{panel}</div>'
