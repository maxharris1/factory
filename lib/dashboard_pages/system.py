"""health.html (System, #92, #141): the components matrix and process details, version drift,
the poll and Linear budget, poller.log, rules and limits, and harnesses with no live waiter."""

from __future__ import annotations

import re

from lib.pr_title import label
from lib.dashboard_pages.common import (
    _b, _chip, duration, _e, _feed, _num, _panel, _span, _td, _ticket, _when,
)


CSS = """\
.sys-row{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:16px;min-width:0}
.panel h2 .count.warn{color:var(--warn)}
#drift{flex:1 1 340px}#health{flex:2 1 520px}#log{flex:3 1 560px}#rules{flex:2 1 380px}
.sys-scroll{overflow-x:auto}
.sys-table{min-width:560px;margin:0;font:12px/1.5 var(--mono);color:var(--text)}
.sys-table.wide{min-width:860px}
.sys-table tbody tr:last-child td{border-bottom:0}
.sys-table td.sys-kind{font-weight:600}
.sys-cell{display:inline-flex;align-items:center;gap:8px}
a.sys-cell{color:var(--bad);text-decoration:none}
.sys-blk{flex:none;width:10px;height:10px;background:var(--muted)}
.sys-blk.ok{background:var(--ok)}.sys-blk.warn{background:var(--warn)}.sys-blk.bad{background:var(--bad)}
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
"""


SCRIPT_LOG = r"""
  // --- system page (#141) ------------------------------------------------------

  var LOG_KINDS = [[/exit rc=(?!0\b)\d+/, 'bad'], [/another orch poll owns/, 'warn'], [/poller start/, 'start']];
  function logKind(line) {
    for (var i = 0; i < LOG_KINDS.length; i++) if (LOG_KINDS[i][0].test(line)) return LOG_KINDS[i][1];
    return null;
  }
  function renderLog(d) {
    var log = d.poller.log;
    var root = panel('log', 'poller.log', log.present ? 'written ' + dur(log.age_seconds) + ' before the snapshot' : 'absent');
    if (!root) return;
    add(root, el('p', 'sys-note', 'last ' + log.tail.length + ' lines, redacted'));
    var box = add(root, el('div', 'sys-log'));
    log.tail.forEach(function (line) { add(box, el('div', logKind(line), line)); });
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
  function matrixOf(d, rows) {
    var keys = [], seen = [], cells = {};
    rows.forEach(function (c) {
      var key = c.bot ? c.component + ' · ' + c.bot : c.component;
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
    var tone = c.configured === false ? 'muted' : st[1];
    var box = c.state === 'down' && c.configured !== false ? link('sys-cell bad', '#' + c.anchor) : el('span', 'sys-cell');
    var blk = add(box, el('span', 'sys-blk ' + tone));
    blk.setAttribute('aria-hidden', 'true');
    var text = add(box, el('span'));
    if (c.last_seen_age_seconds === null || c.last_seen_age_seconds === undefined) text.textContent = st[0] + ' never seen';
    else ticking(text, c.last_seen_age_seconds, st[0] + ' ', '');
    return box;
  }
  function renderComponents(d) {
    var h = d.health || {known: false, components: []};
    var rows = agedHealth(d).rows;
    var down = rows.filter(function (c) { return c.state === 'down' && c.configured !== false; }).length;
    var root = panel('components', 'Components', rows.length + ' components · ' + down + ' down');
    if (!root) return;
    if (!h.known) { add(root, el('p', 'empty', h.error ? 'unknown: ' + h.error : 'no heartbeats recorded yet')); return; }
    var m = matrixOf(d, rows);
    var table = add(add(root, el('div', 'sys-scroll')), el('table', 'sys-table'));
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
    var t = h.thresholds || {};
    add(root, el('p', 'sys-note sys-legend', 'up: a beat within ' + (t.stale_beats || 3) + ' intervals (' + (t.beat_seconds || 15) +
      ' s each; device probes every ' + (t.probe_seconds || 60) + ' s) · stale: within ' + (t.down_beats || 8) +
      ' · down: older, stopped or never seen · unknown: this snapshot is too old to tell'));
    var box = add(root, el('details', 'sys-details'));
    box.open = openDetails.process !== false;
    if (box.addEventListener) box.addEventListener('toggle', function () { openDetails.process = box.open; });
    add(box, el('summary', null, 'process details'));
    var ptable = add(add(box, el('div', 'sys-scroll')), el('table', 'sys-table wide'));
    var phead = add(add(ptable, el('thead')), el('tr'));
    ['component', 'bot', 'device', 'state', 'last seen (ET)', 'ago', 'pid', 'version', 'started (ET)'].forEach(function (t) { add(phead, el('th', null, t)); });
    var tbody = add(ptable, el('tbody'));
    rows.forEach(function (c) {
      var tr = add(tbody, el('tr'));
      tr.setAttribute('id', c.anchor);
      add(tr, el('td', null, c.component));
      add(tr, el('td', null, c.bot || '—'));
      add(tr, el('td', null, c.device));
      var cell = add(tr, el('td'));
      var st = HEALTH_STATE[c.state] || ['OTHER', 'muted'];
      add(cell, chip(st[0], st[1]));
      if (c.reason) add(cell, el('span', 'muted', ' ' + c.reason));
      if (c.configured === false) add(cell, el('span', 'muted', ' (not configured)'));
      add(tr, el('td', null, c.last_seen_et || 'never'));
      ticking(add(tr, el('td', 'num')), c.last_seen_age_seconds, '', '');
      add(tr, el('td', 'num', c.pid === null || c.pid === undefined ? '—' : c.pid));
      var ver = add(tr, el('td', null, c.version));
      if (c.stale_code) add(ver, el('span', 'warn', ' stale code'));
      add(tr, el('td', null, c.started_at_et || '—'));
      mark('components', c.anchor + ':' + c.state, tr);
    });
  }

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
    var behind = 0;
    out.forEach(function (g) {
      g.tone = g.version === newest ? 'ok' : g.version === 'unknown' ? 'bad' : 'warn';
      if (g.tone === 'warn') behind += g.rows.length;
    });
    return {groups: out, behind: behind};
  }
  function renderDrift(d) {
    var h = d.health || {known: false, components: []};
    var drift = driftOf(h);
    var root = panel('drift', 'Version drift');
    if (!root) return;
    if (!h.known) { add(root, el('p', 'empty', h.error ? 'unknown: ' + h.error : 'no heartbeats recorded yet')); return; }
    add(root.children[0], el('span', 'count' + (drift.behind ? ' warn' : ''), drift.behind + ' behind'));
    var box = add(root, el('div', 'sys-drift'));
    drift.groups.forEach(function (g) {
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
    var h = d.health || {};
    var rows = h.no_waiter || [];
    var root = panel('no-waiter', 'Queued jobs with no waiter', rows.length ? rows.length + ' harnesses' : 'none');
    if (!root) return;
    if (!rows.length) {
      var aged = agedHealth(d);
      add(root, el('p', 'empty', !h.known ? 'unknown until a component heartbeats'
        : aged.waiterUnknown ? 'unknown: no snapshot for ' + dur(aged.age)
        : 'every queued job has a live waiter'));
      return;
    }
    var ul = add(root, el('ul', 'feed'));
    rows.forEach(function (n) {
      var li = add(ul, el('li'));
      li.setAttribute('id', n.anchor);
      add(li, chip('no waiter', 'bad'));
      add(li, el('span', 'ticket', 'waiter ' + n.bot));
      add(li, el('span', 'what', 'no waiter for ' + n.harness + ': ' + n.count + ' queued job' + (n.count === 1 ? '' : 's') + ' (' + n.jobs.join(', ') + ')'));
      mark('no-waiter', n.anchor, li);
    });
  }
"""


_LOG_KINDS = (
    (re.compile(r"exit rc=(?!0\b)\d+"), "bad"),
    (re.compile(r"another orch poll owns"), "warn"),
    (re.compile(r"poller start"), "start"),
)


def _log_kind(line: str) -> str | None:
    return next((kind for pattern, kind in _LOG_KINDS if pattern.search(line)), None)


def _log(snapshot: dict) -> str:
    log = snapshot["poller"]["log"]
    count = (
        f"written {duration(log['age_seconds'])} before the snapshot"
        if log["present"]
        else "absent"
    )
    lines = "".join(
        f'<div class="{kind}">{_e(line)}</div>' if (kind := _log_kind(line)) else f"<div>{_e(line)}</div>"
        for line in log["tail"]
    )
    inner = (
        f'<p class="sys-note">last {len(log["tail"])} lines, redacted</p>'
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


_HEALTH_STATE = {"up": ("UP", "ok"), "stale": ("STALE", "warn"), "down": ("DOWN", "bad")}


_ANCHOR = re.compile(r"[A-Za-z0-9_-]+")


def _anchor_attr(value: object) -> str:
    return f' id="{value}"' if isinstance(value, str) and _ANCHOR.fullmatch(value) else ""


def _matrix(snapshot: dict, rows: list[dict]) -> tuple[list[str], list[str], dict]:
    """Rows by component kind (a waiter per bot), columns by device in lane
    order, then any device only the heartbeats name (matrixOf in the script)."""
    keys: list[str] = []
    seen: list[str] = []
    cells: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = f"{row['component']} · {row['bot']}" if row.get("bot") else row["component"]
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
    if row.get("configured") is False:
        tone = "muted"
    age = row.get("last_seen_age_seconds")
    text = f"{label} never seen" if age is None else f"{label} {duration(age)}"
    inner = f'<span class="sys-blk {tone}" aria-hidden="true"></span>{_span(None, text)}'
    if row["state"] == "down" and row.get("configured") is not False and _anchor_attr(row.get("anchor")):
        return f'<td><a class="sys-cell bad" href="#{row["anchor"]}">{inner}</a></td>'
    return f'<td><span class="sys-cell">{inner}</span></td>'


def _components(snapshot: dict) -> str:
    health = snapshot.get("health") or {"known": False, "components": []}
    rows = health.get("components") or []
    count = f"{len(rows)} components · {health.get('down') or 0} down"
    if not health.get("known"):
        text = f"unknown: {health['error']}" if health.get("error") else "no heartbeats recorded yet"
        return _panel("components", "Components", f'<p class="empty">{_e(text)}</p>', count=count)
    keys, devices, cells = _matrix(snapshot, rows)
    matrix = (
        '<div class="sys-scroll"><table class="sys-table"><thead><tr>'
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
    )
    heads = ("component", "bot", "device", "state", "last seen (ET)", "ago", "pid", "version",
             "started (ET)")
    body = []
    for row in rows:
        label, cls = _HEALTH_STATE.get(row["state"], ("OTHER", "muted"))
        reason = _span("muted", " " + row["reason"]) if row.get("reason") else ""
        if row.get("configured") is False:
            reason += _span("muted", " (not configured)")
        body.append(
            f'<tr{_anchor_attr(row.get("anchor"))}>'
            + _td(row["component"])
            + _td(row["bot"] or None)
            + _td(row["device"])
            + f"<td>{_chip(label, cls)}{reason}</td>"
            + _td(row["last_seen_et"] or "never")
            + _td(duration(row["last_seen_age_seconds"]), "num")
            + _td(row["pid"], "num")
            + (f'<td>{_e(row["version"])}{_span("warn", " stale code")}</td>'
               if row.get("stale_code") else _td(row["version"]))
            + _td(row["started_at_et"])
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
        '<details class="sys-details" open><summary>process details</summary>'
        '<div class="sys-scroll"><table class="sys-table wide"><thead><tr>'
        + "".join(f"<th>{_e(text)}</th>" for text in heads)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div></details>"
    )
    inner = matrix + f'<p class="sys-note sys-legend">{_e(note)}</p>' + details
    return _panel("components", "Components", inner, count=count)


def version_groups(health: dict) -> tuple[list[dict], int]:
    """Version drift (SPEC 9: group components by version): the newest
    version first (the checkout's, else the latest started), then the other
    known versions by latest start, then unknown; and how many components are
    on a known version other than the newest (driftOf in the script)."""
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
    behind = sum(len(group["rows"]) for group in ordered if group["tone"] == "warn")
    return ordered, behind


def _drift(snapshot: dict) -> str:
    health = snapshot.get("health") or {"known": False, "components": []}
    if not health.get("known"):
        text = f"unknown: {health['error']}" if health.get("error") else "no heartbeats recorded yet"
        return _panel("drift", "Version drift", f'<p class="empty">{_e(text)}</p>')
    groups, behind = version_groups(health)
    items = []
    for group in groups:
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
    count = _span("count warn" if behind else "count", f"{behind} behind")
    return (
        '<section class="panel" id="drift" aria-label="Version drift">'
        f'<h2>{_span(None, "Version drift")}{count}</h2>'
        f'<div class="sys-drift">{"".join(items)}</div></section>'
    )


def _drift_row(snapshot: dict) -> str:
    return f'<div class="sys-row">{_drift(snapshot)}{_health(snapshot)}</div>'


def _log_row(snapshot: dict) -> str:
    return f'<div class="sys-row">{_log(snapshot)}{_rules(snapshot)}</div>'


def _no_waiter(snapshot: dict) -> str:
    health = snapshot.get("health") or {}
    rows = health.get("no_waiter") or []
    if not rows:
        text = (
            "every queued job has a live waiter"
            if health.get("known")
            else "unknown until a component heartbeats"
        )
        return _panel("no-waiter", "Queued jobs with no waiter", f'<p class="empty">{_e(text)}</p>',
                      count="none")
    items = "".join(
        f'<li{_anchor_attr(row.get("anchor"))}>'
        + _chip("no waiter", "bad")
        + _span("ticket", f"waiter {row['bot']}")
        + _span(
            "what",
            f"no waiter for {row['harness']}: {row['count']} queued job"
            + ("" if row["count"] == 1 else "s")
            + f" ({', '.join(row['jobs'])})",
        )
        + "</li>"
        for row in rows
    )
    return _panel("no-waiter", "Queued jobs with no waiter", f'<ul class="feed">{items}</ul>',
                  count=f"{len(rows)} harnesses")
