from __future__ import annotations

import math

from lib.dashboard_pages.common import _b, _e, _num, _panel, _span, duration
from lib.dashboard_telemetry import rollup_panel

CSS = """\
.ef-row{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:16px}
.ef-row>.panel{flex:2 1 380px}.ef-row>.panel.ef-wide{flex:3 1 560px}
.ef-savings{display:flex;flex-wrap:wrap;align-items:center;gap:24px}
.waffle{display:grid;grid-template-columns:repeat(10,12px);grid-auto-rows:12px;gap:2px;flex:none}
.waffle span{background:var(--grok)}.waffle .s{background:var(--text)}.waffle.none span{background:var(--line-hi)}
.ef-share{display:flex;flex-direction:column;gap:2px}
.ef-share b{font:600 40px/1.1 var(--mono);letter-spacing:-.03em;font-variant-numeric:tabular-nums;color:var(--text)}
.ef-share span{font:12px/1.5 var(--mono);color:var(--muted)}
.ef-counts{display:flex;flex-direction:column;gap:6px;min-width:0}
.ef-counts div{display:flex;align-items:baseline;gap:10px}
.ef-counts b{flex:0 0 40px;text-align:right;font:600 18px/1.25 var(--mono);font-variant-numeric:tabular-nums;color:var(--text)}
.ef-counts b.grok{color:var(--grok)}
.ef-counts span{font-size:12px;color:var(--muted)}
#savings .definition{margin:14px 0 6px;font-size:13px;color:var(--text-2)}
#savings details p{margin:6px 0 0;font-size:12px}
.ef-rounds{display:flex;flex-direction:column;gap:10px}
.ef-rb{display:grid;grid-template-columns:84px minmax(0,1fr) 84px;align-items:center;gap:12px}
.ef-k{font:12px/1.5 var(--mono);color:var(--text)}
.ef-track{display:block;height:14px}
.ef-fill{display:block;height:14px;background:var(--text-2)}.ef-fill.zero{background:var(--ok)}
.ef-n{text-align:right;white-space:nowrap;font:12px/1.5 var(--mono);font-variant-numeric:tabular-nums;color:var(--muted)}
.ef-n b{font-size:13px;font-weight:600;color:var(--text)}.ef-n b.ok{color:var(--ok)}
.ef-foot{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:6px 16px;margin:14px 0 0}
.ef-note{margin:0;font-size:13px;color:var(--text-2)}
.ef-foot a{font:12px/1.5 var(--mono);text-decoration:underline;text-underline-offset:3px}
#jobs-lifetime{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 14px;padding:10px 16px}
#jobs-lifetime h2{margin:0;font:500 11px/1.5 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
#jobs-lifetime h2>span:first-child::before{display:none}
.ef-life{font:13px/1.5 var(--mono);font-variant-numeric:tabular-nums;color:var(--text-2)}.ef-life b{font-weight:600}
.ef-dot{font:13px/1.5 var(--mono);color:var(--muted)}
.ef-pct{margin-left:auto;font-size:12px;color:var(--muted)}
.ef-scroll{overflow-x:auto}
.ef-usage{min-width:860px;margin:0}
.ef-usage td{padding:10px 12px 10px 0}
.ef-usage th:last-child,.ef-usage td:last-child{padding-right:0}
.ef-usage .ef-un td{padding-top:6px;padding-bottom:6px}
.ef-who{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
.ef-name{font-size:12px;color:var(--text-2)}
.ef-dev{margin-top:3px;font:11px/1.5 var(--mono);color:var(--muted)}
#headroom .ef-v{font:12px/1.5 var(--mono);font-variant-numeric:tabular-nums;color:var(--text)}
#headroom .ef-v.muted{color:var(--muted)}
.ef-room{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px}
.ef-hn{flex:0 0 40px;font:600 16px/1.25 var(--mono);font-variant-numeric:tabular-nums}
.ef-hn.neutral{color:var(--text-2)}
.ef-lim{font:12px/1.5 var(--mono)}
.ef-meter{display:grid;grid-template-columns:repeat(20,8px);grid-auto-rows:8px;gap:2px;flex:none}
.ef-meter span{background:var(--line-hi)}
.ef-meter .ok{background:var(--ok)}.ef-meter .warn{background:var(--warn)}.ef-meter .bad{background:var(--bad)}
.ef-meter .neutral{background:var(--text-2)}
.ef-tag{font:11px/1.5 var(--mono);color:var(--muted)}
.ef-hnote{margin:12px 0 0;max-width:720px;font-size:12px;color:var(--muted)}
.ef-accts{display:none;flex-direction:column;gap:8px}
.ef-acct{min-width:0;padding:10px 12px 12px;background:var(--bg);box-shadow:var(--elev)}
.ef-acct .ef-room{margin-top:10px}
.ef-acct .ef-hn{flex:none;font-size:22px;line-height:1.15;letter-spacing:-.03em}
.ef-hl{font:500 12px/1.3 var(--mono);color:var(--muted)}
.ef-wins{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px;padding-top:8px;
border-top:1px solid var(--line)}
.ef-wins>div{min-width:0}
.ef-wk{font:11px/1.5 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.ef-accts .ef-un{display:flex;align-items:baseline;gap:8px}
.ef-acct+.ef-un{padding:2px 0 0}
.ef-hnote-ph{display:none;margin:10px 0 0;font-size:12px;color:var(--muted)}
@media (max-width:699.98px){
body[data-page="efficiency"] main{padding:12px 12px 24px;gap:12px}
.ef-row{gap:12px}.ef-row>.panel,.ef-row>.panel.ef-wide{flex:1 1 100%;padding:12px}
.ef-row>.panel h2{flex-wrap:wrap;gap:4px 8px;margin:0 0 10px}
.ef-rb{grid-template-columns:68px minmax(0,1fr) 66px;gap:8px}.ef-n b{font-size:12px}
.ef-foot{display:block;margin:12px 0 0}.ef-note{margin:0 0 4px}
#jobs-lifetime{gap:2px 10px;padding:10px 12px}#jobs-lifetime h2{flex:1 0 100%}.ef-pct{flex:1 0 100%;margin-left:0}
#headroom .ef-scroll,.ef-hnote{display:none}.ef-accts{display:flex}.ef-hnote-ph{display:block}
}
"""


SCRIPT = r"""
  function halfUp(x) { return Math.floor(x + 0.5); }
  function tenths(t) { return Math.floor(t / 10) + '.' + (t % 10); }
  function renderSavings(d) {
    var s = d.handoff_savings;
    var root = panel('savings', 'Handoff savings', 'scripted / (scripted + grok wakes)');
    if (!root) return;
    var lit = s.scripted_share === null ? null : halfUp(s.scripted_share * 100);
    var box = add(root, el('div', 'ef-savings'));
    var waffle = add(box, el('div', lit === null ? 'waffle none' : 'waffle'));
    waffle.setAttribute('role', 'img');
    waffle.setAttribute('aria-label', lit === null ? 'no handoffs yet'
      : lit + ' of 100 squares are scripted handoffs, ' + (100 - lit) + ' are grok wakes');
    for (var i = 0; i < 100; i++) add(waffle, el('span', lit !== null && i < lit ? 's' : null));
    var share = add(box, el('div', 'ef-share'));
    add(share, el('b', null, lit === null ? '—' : lit + '%'));
    add(share, el('span', null, 'scripted share'));
    var counts = add(box, el('div', 'ef-counts'));
    [[s.scripted, 'scripted handoffs', null], [s.scripted_in_flight, 'scripted in flight', null],
     [s.bot_to_bot, 'bot-to-bot (grok wakes)', 'grok'],
     [s.engine_handled, 'engine handled', null], [s.engine_escalated, 'engine escalated', null]].forEach(function (x) {
      var line = add(counts, el('div'));
      add(line, el('b', x[2], num(x[0])));
      add(line, el('span', null, x[1]));
    });
    add(root, el('p', 'definition', s.scripted_share_definition)).setAttribute('id', 'share-definition');
    add(details(root, 'savings-def', 'how each count is made'), el('p', 'muted', s.definition));
  }

  function renderJobsLifetime(d) {
    var t = d.queue.totals;
    var finished = t.done + t.failed;
    var root = panel('jobs-lifetime', 'Lifetime');
    if (!root) return;
    [[t.done, 'ok', ' jobs done'], [t.failed, 'bad', ' failed']].forEach(function (x, i) {
      if (i) add(root, el('span', 'ef-dot', '·')).setAttribute('aria-hidden', 'true');
      var life = add(root, el('span', 'ef-life'));
      add(life, el('b', x[1], num(x[0])));
      add(life, document.createTextNode(x[2]));
    });
    add(root, el('span', 'ef-pct', finished
      ? halfUp(100 * t.done / finished) + '% of ' + num(finished) + ' finished jobs succeeded' : 'no finished jobs yet'));
  }

  var SHIPPED = {merged: true, done: true, 'handed-off': true};
  var ROUND_BUCKETS = ['0 rounds', '1 round', '2 rounds', '3 rounds', '4 or more'];
  function renderRounds(d) {
    var shipped = (d.tickets || []).filter(function (r) { return SHIPPED[r.state]; });
    var root = panel('rounds', 'Revise rounds before ship', shipped.length + ' shipped tickets');
    if (!root) return;
    if (!shipped.length) { add(root, el('p', 'empty', 'no shipped tickets yet')); return; }
    var counts = ROUND_BUCKETS.map(function () { return 0; });
    shipped.forEach(function (r) { counts[Math.min(r.round, ROUND_BUCKETS.length - 1)] += 1; });
    var top = Math.max.apply(null, counts), total = shipped.length;
    var box = add(root, el('div', 'ef-rounds'));
    counts.forEach(function (n, i) {
      var line = add(box, el('div', 'ef-rb'));
      add(line, el('span', 'ef-k', ROUND_BUCKETS[i]));
      var track = add(line, el('span', 'ef-track'));
      track.setAttribute('aria-hidden', 'true');
      add(track, el('span', i ? 'ef-fill' : 'ef-fill zero'))
        .setAttribute('style', 'width:' + tenths(Math.floor((2000 * n + top) / (2 * top))) + '%');
      var count = add(line, el('span', 'ef-n'));
      add(count, el('b', i ? null : 'ok', num(n)));
      add(count, document.createTextNode(' · ' + Math.floor((200 * n + total) / (2 * total)) + '%'));
    });
    var foot = add(root, el('div', 'ef-foot'));
    add(foot, el('p', 'ef-note', counts[0] + ' of ' + total + ' shipped with no revise round'));
    add(foot, link(null, 'job.html')).textContent = 'see shipped tickets';
  }

  var WINDOWS = ['five_hour', 'seven_day'];
  var WINDOW_NAMES = {five_hour: '5-hour', seven_day: '7-day'};
  var METER_CELLS = 20;
  var HEADROOM_NOTE = 'Headroom is what is left in the fullest window.';
  function usageTone(x) { return x >= 0.5 ? 'ok' : x >= 0.2 ? 'warn' : 'bad'; }
  function accountName(e) { return e.label || e.account; }
  function accountAbout(e) {
    return [e.plan, e.source ? 'via ' + e.source : null,
      e.age_seconds === null ? 'never read' : 'read ' + dur(e.age_seconds) + ' ago']
      .filter(function (x) { return x; }).join(' · ');
  }
  function accountJobs(e) {
    return e.running + ' running · ' + (e.max_jobs === null ? 'no max' : 'max ' + e.max_jobs);
  }
  function accountDevices(e) {
    var parts = [e.signed_in.length ? 'signed in on ' + e.signed_in.join(', ') : null,
      e.running_on.length ? 'running on ' + e.running_on.join(', ') : null]
      .filter(function (x) { return x; });
    return parts.length ? parts.join(' · ') : 'none';
  }
  function accountView(e, now) {
    var fresh = WINDOWS.filter(function (w) { return e[w].state === 'ok'; });
    var planned = WINDOWS.filter(function (w) { return e[w].state !== 'not on plan'; });
    var v = {entry: e, limit: e.limit_label, pct: null, tone: null, tag: null, lit: 0};
    if (e.state === 'no telemetry') {
      v.windows = WINDOWS.map(function () { return [e.state, 'muted']; });
      v.resets = [e.state, 'muted'];
    } else {
      v.windows = WINDOWS.map(function (w) {
        if (fresh.indexOf(w) >= 0) return [halfUp(e[w].used * 100) + '% used', null];
        return [e[w].state === 'not on plan' ? 'not on this plan' : e[w].state, 'muted'];
      });
      var ends = fresh.filter(function (w) { return e[w].resets_at !== null; })
        .map(function (w) { return e[w].resets_at; });
      v.resets = ends.length ? ['in ' + dur(Math.min.apply(null, ends) - now), null] : ['no reading', 'muted'];
    }
    if (e.limit_label || !fresh.length) {
      v.label = accountName(e) + ' headroom: ' +
        (e.limit_label || (e.state === 'no telemetry' ? 'no telemetry' : 'no reading'));
      return v;
    }
    var only = fresh.length === 1 && planned.length > 1 ? WINDOW_NAMES[fresh[0]] : null;
    v.pct = halfUp(e.headroom * 100);
    v.lit = Math.floor((2 * v.pct + 5) / 10);
    v.tone = only ? 'neutral' : usageTone(e.headroom);
    v.tag = only ? '(' + only + ' only)' : null;
    v.label = accountName(e) + ' headroom ' + v.pct + ' percent' + (only ? ', from the ' + only + ' window only' : '') +
      ': ' + v.lit + ' of ' + METER_CELLS + ' cells lit';
    return v;
  }
  function usageCell(parent, c, tag) { add(parent, el(tag, c[1] ? 'ef-v ' + c[1] : 'ef-v', c[0])); }
  function accountWho(parent, e) {
    var line = add(parent, el('div', 'ef-who'));
    add(line, el('span', 'bot ' + e.harness, e.harness));
    add(line, el('span', 'ef-name', accountName(e)));
    add(parent, el('div', 'ef-dev', accountAbout(e)));
  }
  function headroomRoom(parent, v, phone) {
    var room = add(parent, el('div', 'ef-room'));
    if (v.limit) add(room, el('span', 'ef-lim bad', v.limit));
    else if (v.pct !== null) add(room, el('span', 'ef-hn ' + v.tone, v.pct + '%'));
    else if (!phone) add(room, el('span', 'ef-hn'));
    var meter = add(room, el('span', 'ef-meter'));
    meter.setAttribute('role', 'img');
    meter.setAttribute('aria-label', v.label);
    for (var i = 0; i < METER_CELLS; i++) add(meter, el('span', i < v.lit ? v.tone : null));
    if (!phone && v.tag) add(room, el('span', 'ef-tag', v.tag));
  }
  function untrackedLine(parent, h, row) {
    if (row) {
      var tr = add(parent, el('tr', 'ef-un'));
      add(add(tr, el('td')), el('span', 'bot ' + h, h));
      add(tr, el('td', 'ef-v muted', 'not tracked')).setAttribute('colspan', '6');
      return;
    }
    var line = add(parent, el('div', 'ef-un'));
    add(line, el('span', 'bot ' + h, h));
    add(line, el('span', 'ef-v muted', 'not tracked'));
  }
  function renderHeadroom(d) {
    var root = panel('headroom', 'Subscription headroom', 'per account');
    if (!root) return;
    var accounts = d.usage.accounts;
    var views = accounts.filter(function (e) { return e.state !== 'not tracked'; })
      .map(function (e) { return accountView(e, d.generated_at_epoch); });
    var untracked = [];
    accounts.filter(function (e) { return e.state === 'not tracked'; })
      .forEach(function (e) { if (untracked.indexOf(e.harness) < 0) untracked.push(e.harness); });
    var table = add(add(root, el('div', 'ef-scroll')), el('table', 'ef-usage'));
    var head = add(add(table, el('thead')), el('tr'));
    ['account', '5-hour window', '7-day window', 'resets', 'headroom', 'jobs', 'devices']
      .forEach(function (h) { add(head, el('th', null, h)); });
    var body = add(table, el('tbody'));
    views.forEach(function (v) {
      var tr = add(body, el('tr'));
      accountWho(add(tr, el('td')), v.entry);
      v.windows.concat([v.resets]).forEach(function (c) { usageCell(tr, c, 'td'); });
      headroomRoom(add(tr, el('td')), v, false);
      usageCell(tr, [accountJobs(v.entry), null], 'td');
      usageCell(tr, [accountDevices(v.entry), null], 'td');
    });
    untracked.forEach(function (h) { untrackedLine(body, h, true); });
    add(root, el('p', 'ef-hnote', HEADROOM_NOTE));
    var list = add(root, el('div', 'ef-accts'));
    views.forEach(function (v) {
      var block = add(list, el('div', 'ef-acct'));
      accountWho(block, v.entry);
      headroomRoom(block, v, true);
      add(block, el('div', 'ef-hl', v.tag ? 'headroom ' + v.tag : 'headroom'));
      var wins = add(block, el('div', 'ef-wins'));
      ['5-hour', '7-day', 'resets'].forEach(function (name, i) {
        var col = add(wins, el('div'));
        add(col, el('div', 'ef-wk', name));
        usageCell(col, v.windows.concat([v.resets])[i], 'div');
      });
      add(block, el('div', 'ef-dev', accountJobs(v.entry) + ' · ' + accountDevices(v.entry)));
    });
    untracked.forEach(function (h) { untrackedLine(list, h, false); });
    add(root, el('p', 'ef-hnote-ph', HEADROOM_NOTE));
  }
"""

HEADROOM_NOTE = "Headroom is what is left in the fullest window."
SHIPPED = ("merged", "done", "handed-off")
ROUND_BUCKETS = ("0 rounds", "1 round", "2 rounds", "3 rounds", "4 or more")
METER_CELLS = 20
WINDOWS = ("five_hour", "seven_day")
WINDOW_NAMES = {"five_hour": "5-hour", "seven_day": "7-day"}


def _half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _tenths(value: int) -> str:
    return f"{value // 10}.{value % 10}"


def _row(*panels: str) -> str:
    return f'<div class="ef-row">{"".join(panels)}</div>'


def _savings(snapshot: dict) -> str:
    savings = snapshot["handoff_savings"]
    share = savings["scripted_share"]
    lit = None if share is None else _half_up(share * 100)
    cells = "".join('<span class="s"></span>' if lit is not None and i < lit else "<span></span>"
                    for i in range(100))
    label = (
        "no handoffs yet" if lit is None
        else f"{lit} of 100 squares are scripted handoffs, {100 - lit} are grok wakes"
    )
    counts = "".join(
        f"<div>{_b(value, cls)}{_span(None, text)}</div>"
        for value, text, cls in (
            (savings["scripted"], "scripted handoffs", ""),
            (savings["scripted_in_flight"], "scripted in flight", ""),
            (savings["bot_to_bot"], "bot-to-bot (grok wakes)", "grok"),
            (savings["engine_handled"], "engine handled", ""),
            (savings["engine_escalated"], "engine escalated", ""),
        )
    )
    inner = (
        '<div class="ef-savings">'
        f'<div class="{"waffle none" if lit is None else "waffle"}" role="img" aria-label="{_e(label)}">{cells}</div>'
        f'<div class="ef-share"><b>{"—" if lit is None else f"{lit}%"}</b><span>scripted share</span></div>'
        f'<div class="ef-counts">{counts}</div></div>'
        f'<p class="definition" id="share-definition">{_e(savings["scripted_share_definition"])}</p>'
        "<details><summary>how each count is made</summary>"
        f'<p class="muted">{_e(savings["definition"])}</p></details>'
    )
    return _panel("savings", "Handoff savings", inner, count="scripted / (scripted + grok wakes)")


def _jobs_lifetime(snapshot: dict) -> str:
    totals = snapshot["queue"]["totals"]
    done, failed = totals["done"], totals["failed"]
    finished = done + failed
    rate = (
        f"{_half_up(100 * done / finished)}% of {_num(finished)} finished jobs succeeded" if finished
        else "no finished jobs yet"
    )
    inner = (
        f'<span class="ef-life">{_b(done, "ok")} jobs done</span>'
        '<span class="ef-dot" aria-hidden="true">·</span>'
        f'<span class="ef-life">{_b(failed, "bad")} failed</span>'
        + _span("ef-pct", rate)
    )
    return _panel("jobs-lifetime", "Lifetime", inner)


def _rounds(snapshot: dict) -> str:
    shipped = [row for row in snapshot["tickets"] if row["state"] in SHIPPED]
    total = len(shipped)
    count = f"{total} shipped tickets"
    if not shipped:
        return _panel("rounds", "Revise rounds before ship",
                      '<p class="empty">no shipped tickets yet</p>', count=count, extra="ef-wide")
    counts = [0] * len(ROUND_BUCKETS)
    for row in shipped:
        counts[min(row["round"], len(ROUND_BUCKETS) - 1)] += 1
    top = max(counts)
    bars = "".join(
        '<div class="ef-rb">'
        + _span("ef-k", label)
        + '<span class="ef-track" aria-hidden="true">'
        + f'<span class="{"ef-fill" if i else "ef-fill zero"}" style="width:{_tenths((2000 * n + top) // (2 * top))}%">'
        + "</span></span>"
        + f'<span class="ef-n">{_b(n, "" if i else "ok")} · {(200 * n + total) // (2 * total)}%</span>'
        + "</div>"
        for i, (label, n) in enumerate(zip(ROUND_BUCKETS, counts))
    )
    note = f"{counts[0]} of {total} shipped with no revise round"
    return _panel(
        "rounds", "Revise rounds before ship",
        f'<div class="ef-rounds">{bars}</div>'
        f'<div class="ef-foot"><p class="ef-note">{note}</p><a href="job.html">see shipped tickets</a></div>',
        count=count, extra="ef-wide",
    )


def _tone(room: float) -> str:
    return "ok" if room >= 0.5 else "warn" if room >= 0.2 else "bad"


def _view(entry: dict, now: float) -> dict:
    state = entry["state"]
    fresh = [w for w in WINDOWS if entry[w]["state"] == "ok"]
    planned = [w for w in WINDOWS if entry[w]["state"] != "not on plan"]
    if state == "no telemetry":
        windows = [(state, "muted")] * len(WINDOWS)
        resets = (state, "muted")
    else:
        windows = [(f"{_half_up(entry[w]['used'] * 100)}% used", None) if w in fresh
                   else ("not on this plan" if entry[w]["state"] == "not on plan" else entry[w]["state"], "muted")
                   for w in WINDOWS]
        ends = [entry[w]["resets_at"] for w in fresh if entry[w]["resets_at"] is not None]
        resets = (f"in {duration(min(ends) - now)}", None) if ends else ("no reading", "muted")
    view = {"entry": entry, "windows": windows, "resets": resets, "limit": entry["limit_label"],
            "pct": None, "tone": None, "tag": None, "lit": 0}
    if entry["limit_label"] or not fresh:
        reason = entry["limit_label"] or ("no telemetry" if state == "no telemetry" else "no reading")
        return {**view, "label": f"{_name(entry)} headroom: {reason}"}
    only = WINDOW_NAMES[fresh[0]] if len(fresh) == 1 and len(planned) > 1 else None
    pct = _half_up(entry["headroom"] * 100)
    lit = (2 * pct + 5) // 10
    window = "" if only is None else f", from the {only} window only"
    return {
        **view, "pct": pct, "lit": lit, "tone": "neutral" if only else _tone(entry["headroom"]),
        "tag": None if only is None else f"({only} only)",
        "label": f"{_name(entry)} headroom {pct} percent{window}: {lit} of {METER_CELLS} cells lit",
    }


def _cell(text: str, cls: str | None, tag: str = "td") -> str:
    return f'<{tag} class="{f"ef-v {cls}" if cls else "ef-v"}">{_e(text)}</{tag}>'


def _name(entry: dict) -> str:
    return entry["label"] or entry["account"]


def _about(entry: dict) -> str:
    age = entry["age_seconds"]
    return " · ".join(part for part in (
        entry["plan"],
        entry["source"] and f"via {entry['source']}",
        "never read" if age is None else f"read {duration(age)} ago",
    ) if part)


def _jobs(entry: dict) -> str:
    cap = "no max" if entry["max_jobs"] is None else f"max {entry['max_jobs']}"
    return f"{entry['running']} running · {cap}"


def _devices(entry: dict) -> str:
    parts = [
        f"{verb} {', '.join(entry[key])}"
        for verb, key in (("signed in on", "signed_in"), ("running on", "running_on"))
        if entry[key]
    ]
    return " · ".join(parts) or "none"


def _who(entry: dict) -> str:
    harness = entry["harness"]
    return (
        f'<div class="ef-who">{_span("bot " + harness, harness)}{_span("ef-name", _name(entry))}</div>'
        f'<div class="ef-dev">{_e(_about(entry))}</div>'
    )


def _number(view: dict, *, phone: bool) -> str:
    if view["limit"]:
        return _span("ef-lim bad", view["limit"])
    if view["pct"] is not None:
        return _span(f"ef-hn {view['tone']}", f"{view['pct']}%")
    return "" if phone else _span("ef-hn", "")


def _meter(view: dict) -> str:
    lit = view["lit"]
    cells = f'<span class="{view["tone"]}"></span>' * lit + "<span></span>" * (METER_CELLS - lit)
    return f'<span class="ef-meter" role="img" aria-label="{_e(view["label"])}">{cells}</span>'


def _account_row(view: dict) -> str:
    tag = "" if view["tag"] is None else _span("ef-tag", view["tag"])
    return (
        f"<tr><td>{_who(view['entry'])}</td>"
        + "".join(_cell(text, cls) for text, cls in (*view["windows"], view["resets"]))
        + f'<td><div class="ef-room">{_number(view, phone=False)}{_meter(view)}{tag}</div></td>'
        + _cell(_jobs(view["entry"]), None)
        + _cell(_devices(view["entry"]), None)
        + "</tr>"
    )


def _account_block(view: dict) -> str:
    label = "headroom" if view["tag"] is None else f"headroom {view['tag']}"
    columns = "".join(
        f'<div><div class="ef-wk">{name}</div>{_cell(text, cls, "div")}</div>'
        for name, (text, cls) in zip(("5-hour", "7-day", "resets"), (*view["windows"], view["resets"]))
    )
    return (
        f'<div class="ef-acct">{_who(view["entry"])}'
        f'<div class="ef-room">{_number(view, phone=True)}{_meter(view)}</div>'
        f'<div class="ef-hl">{_e(label)}</div><div class="ef-wins">{columns}</div>'
        f'<div class="ef-dev">{_e(_jobs(view["entry"]) + " · " + _devices(view["entry"]))}</div></div>'
    )


def _headroom(snapshot: dict) -> str:
    accounts = snapshot["usage"]["accounts"]
    views = [_view(e, snapshot["generated_at_epoch"]) for e in accounts if e["state"] != "not tracked"]
    untracked = list(dict.fromkeys(e["harness"] for e in accounts if e["state"] == "not tracked"))
    head = "".join(f"<th>{name}</th>" for name in
                   ("account", "5-hour window", "7-day window", "resets", "headroom", "jobs", "devices"))
    rows = "".join(_account_row(view) for view in views) + "".join(
        f'<tr class="ef-un"><td>{_span("bot " + h, h)}</td>'
        '<td class="ef-v muted" colspan="6">not tracked</td></tr>'
        for h in untracked
    )
    blocks = "".join(_account_block(view) for view in views) + "".join(
        f'<div class="ef-un">{_span("bot " + h, h)}{_span("ef-v muted", "not tracked")}</div>'
        for h in untracked
    )
    inner = (
        f'<div class="ef-scroll"><table class="ef-usage"><thead><tr>{head}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
        f'<p class="ef-hnote">{HEADROOM_NOTE}</p>'
        f'<div class="ef-accts">{blocks}</div>'
        f'<p class="ef-hnote-ph">{HEADROOM_NOTE}</p>'
    )
    return _panel("headroom", "Subscription headroom", inner, count="per account", extra="ef-wide")


def top_row(snapshot: dict) -> str:
    return _row(_headroom(snapshot), rollup_panel(snapshot))


def middle_row(snapshot: dict) -> str:
    return _row(_rounds(snapshot), _savings(snapshot))
