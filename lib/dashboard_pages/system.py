"""health.html (System, #92): component heartbeats and harnesses with no live waiter; also the
poll and Linear budget panel (on efficiency.html) and the log (on activity.html)."""

from __future__ import annotations

import re

from lib.pr_title import label
from lib.dashboard_pages.common import (
    _b, _chip, duration, _e, _feed, _num, _panel, _span, _td, _ticket, _when,
)


SCRIPT_LOG = r"""
  function renderLog(d) {
    var log = d.poller.log;
    var root = panel('log', 'poller.log', log.present ? 'written ' + dur(log.age_seconds) + ' before the snapshot' : 'absent');
    if (!root) return;
    add(root, el('p', 'muted', 'last ' + log.tail.length + ' lines, redacted'));
    add(root, el('pre', null, log.tail.join('\n')));
  }
"""


SCRIPT_HEALTH = r"""
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
"""


SCRIPT_COMPONENTS = r"""
  // --- health page (#92) -------------------------------------------------------

  var HEALTH_STATE = {up: ['UP', 'ok'], stale: ['STALE', 'warn'], down: ['DOWN', 'bad'],
                      unknown: ['UNKNOWN', 'muted']};
  function renderComponents(d) {
    var h = d.health || {known: false, components: []};
    var rows = agedHealth(d).rows;
    var down = rows.filter(function (c) { return c.state === 'down' && c.configured !== false; }).length;
    var root = panel('components', 'Components', rows.length + ' components · ' + down + ' down');
    if (!root) return;
    if (!h.known) { add(root, el('p', 'empty', h.error ? 'unknown: ' + h.error : 'no heartbeats recorded yet')); return; }
    var table = add(root, el('table'));
    var head = add(add(table, el('thead')), el('tr'));
    ['component', 'bot', 'device', 'state', 'last seen (ET)', 'ago', 'pid', 'version', 'started (ET)'].forEach(function (t) { add(head, el('th', null, t)); });
    var tbody = add(table, el('tbody'));
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
    var t = h.thresholds || {};
    add(root, el('p', 'muted', 'up: a beat within ' + (t.stale_beats || 3) + ' intervals (' + (t.beat_seconds || 15) +
      ' s each; device probes every ' + (t.probe_seconds || 60) + ' s) · stale: within ' + (t.down_beats || 8) +
      ' · down: older, stopped or never seen · unknown: this snapshot is too old to tell'));
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


# --- health page (#92) ---------------------------------------------------------------


_HEALTH_STATE = {"up": ("UP", "ok"), "stale": ("STALE", "warn"), "down": ("DOWN", "bad")}


_ANCHOR = re.compile(r"[A-Za-z0-9_-]+")


def _anchor_attr(value: object) -> str:
    return f' id="{value}"' if isinstance(value, str) and _ANCHOR.fullmatch(value) else ""


def _components(snapshot: dict) -> str:
    health = snapshot.get("health") or {"known": False, "components": []}
    rows = health.get("components") or []
    count = f"{len(rows)} components · {health.get('down') or 0} down"
    if not health.get("known"):
        text = f"unknown: {health['error']}" if health.get("error") else "no heartbeats recorded yet"
        return _panel("components", "Components", f'<p class="empty">{_e(text)}</p>', count=count)
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
        f"({_e(thresholds.get('beat_seconds', 15))} s each; device probes every "
        f"{_e(thresholds.get('probe_seconds', 60))} s) · stale: within "
        f"{thresholds.get('down_beats', 8)} · down: older, stopped or never seen"
    )
    inner = (
        "<table><thead><tr>"
        + "".join(f"<th>{_e(text)}</th>" for text in heads)
        + "</tr></thead><tbody>"
        + "".join(body)
        + f'</tbody></table><p class="muted">{_e(note)}</p>'
    )
    return _panel("components", "Components", inner, count=count)


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
