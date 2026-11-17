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
.ef-lede{margin:0 0 12px;max-width:880px;font-size:13px;color:var(--text-2)}
.ef-scroll{overflow-x:auto}
.ef-usage{min-width:640px;margin:0}
.ef-usage .ef-v{font:12px/1.5 var(--mono)}
.ef-usage .ef-v.ok{color:var(--ok)}.ef-usage .ef-v.warn{color:var(--warn)}.ef-usage .ef-v.bad{color:var(--bad)}
.ef-usage .ef-v.muted{color:var(--muted)}
.ef-usage td .muted{margin-left:6px;font:12px/1.5 var(--mono)}
@media (max-width:699.98px){
body[data-page="efficiency"] main{padding:12px 12px 24px;gap:12px}
.ef-row{gap:12px}.ef-row>.panel,.ef-row>.panel.ef-wide{flex:1 1 100%;padding:12px}
.ef-row>.panel h2{flex-wrap:wrap;gap:4px 8px;margin:0 0 10px}
.ef-rb{grid-template-columns:68px minmax(0,1fr) 66px;gap:8px}.ef-n b{font-size:12px}
.ef-foot{display:block;margin:12px 0 0}.ef-note{margin:0 0 4px}
#jobs-lifetime{gap:2px 10px;padding:10px 12px}#jobs-lifetime h2{flex:1 0 100%}.ef-pct{flex:1 0 100%;margin-left:0}
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
     [s.bot_to_bot, 'bot-to-bot (grok wakes)', 'grok']].forEach(function (x) {
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

  var USAGE_HARNESSES = ['claude', 'codex', 'cursor', 'grok'];
  var USAGE_UNTRACKED = {cursor: true, grok: true};
  function pct(x) { return halfUp(x * 100) + '%'; }
  var WINDOW_NAMES = {five_hour: '5-hour', seven_day: '7-day'};
  function usageLevel(x) { return x >= 0.5 ? 'ok' : x >= 0.2 ? 'warn' : 'bad'; }
  function usageCells(e, now) {
    if (e.state === 'not tracked' || e.state === 'no telemetry') {
      return [0, 1, 2].map(function () { return [e.state, 'muted']; })
        .concat([e.limit_label ? [e.limit_label, 'bad'] : [e.state, 'muted']]);
    }
    var cells = ['five_hour', 'seven_day'].map(function (w) {
      return e[w].state === 'ok' ? [pct(e[w].used) + ' used', null] : [e[w].state, 'muted'];
    });
    var resets = ['five_hour', 'seven_day'].filter(function (w) { return e[w].state === 'ok' && e[w].resets_at !== null; })
      .map(function (w) { return e[w].resets_at; });
    cells.push(resets.length ? ['in ' + dur(Math.min.apply(null, resets) - now), null] : ['—', 'muted']);
    if (e.limit_label) cells.push([e.limit_label, 'bad']);
    else if (e.headroom !== null) {
      var fresh = ['five_hour', 'seven_day'].filter(function (w) { return e[w].state === 'ok'; });
      cells.push(fresh.length === 2 ? [pct(e.headroom), usageLevel(e.headroom)]
        : [pct(e.headroom) + ' (' + WINDOW_NAMES[fresh[0]] + ' only)', null]);
    }
    else if (e.state === 'stale' && e.age_seconds !== null) cells.push(['reading ' + dur(e.age_seconds) + ' old', 'muted']);
    else if (e.age_seconds === null) cells.push(['no reading yet', 'muted']);
    else cells.push([e.state, 'muted']);
    return cells;
  }
  function renderHeadroom(d) {
    var usage = d.usage;
    var root = panel('headroom', 'Subscription headroom');
    if (!root) return;
    add(root, el('p', 'ef-lede', "Usage per bot account against each plan's limit, read at the start of every job, " +
      'so it is clear which bot has room before the queue backs up.'));
    var table = add(add(root, el('div', 'ef-scroll')), el('table', 'ef-usage'));
    var head = add(add(table, el('thead')), el('tr'));
    ['bot', '5-hour window', '7-day window', 'resets', 'headroom'].forEach(function (h) { add(head, el('th', null, h)); });
    var body = add(table, el('tbody'));
    var per = {};
    usage.bots.forEach(function (e) { per[e.harness] = (per[e.harness] || 0) + 1; });
    var rows = usage.bots.map(function (e) { return [e.harness, e.bot, per[e.harness] > 1 ? e.device : null, usageCells(e, d.generated_at_epoch)]; });
    USAGE_HARNESSES.forEach(function (h) {
      if (per[h]) return;
      var text = USAGE_UNTRACKED[h] ? 'not tracked' : usage.state === 'no telemetry' ? 'no telemetry' : 'no reading yet';
      rows.push([h, h, null, [0, 1, 2, 3].map(function () { return [text, 'muted']; })]);
    });
    rows.forEach(function (r) {
      var tr = add(body, el('tr'));
      var who = add(tr, el('td'));
      add(who, el('span', 'bot ' + r[0], r[1]));
      if (r[2] !== null) add(who, el('span', 'muted', r[2]));
      r[3].forEach(function (c) { add(tr, el('td', c[1] ? 'ef-v ' + c[1] : 'ef-v', c[0])); });
    });
  }
"""

HEADROOM_LEDE = (
    "Usage per bot account against each plan's limit, read at the start of every job, "
    "so it is clear which bot has room before the queue backs up."
)
SHIPPED = ("merged", "done", "handed-off")
ROUND_BUCKETS = ("0 rounds", "1 round", "2 rounds", "3 rounds", "4 or more")
USAGE_HARNESSES = ("claude", "codex", "cursor", "grok")
USAGE_UNTRACKED = ("cursor", "grok")
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


def _usage_cells(entry: dict, now: float) -> list[tuple[str, str | None]]:
    state = entry["state"]
    if state in ("not tracked", "no telemetry"):
        last = (entry["limit_label"], "bad") if entry.get("limit_label") else (state, "muted")
        return [(state, "muted")] * 3 + [last]
    cells: list[tuple[str, str | None]] = [
        (f"{_half_up(entry[w]['used'] * 100)}% used", None) if entry[w]["state"] == "ok"
        else (entry[w]["state"], "muted")
        for w in WINDOWS
    ]
    resets = [entry[w]["resets_at"] for w in WINDOWS
              if entry[w]["state"] == "ok" and entry[w]["resets_at"] is not None]
    cells.append((f"in {duration(min(resets) - now)}", None) if resets else ("—", "muted"))
    room = entry["headroom"]
    if entry.get("limit_label"):
        cells.append((entry["limit_label"], "bad"))
    elif room is not None:
        fresh = [w for w in WINDOWS if entry[w]["state"] == "ok"]
        if len(fresh) == len(WINDOWS):
            cells.append((f"{_half_up(room * 100)}%", "ok" if room >= 0.5 else "warn" if room >= 0.2 else "bad"))
        else:
            cells.append((f"{_half_up(room * 100)}% ({WINDOW_NAMES[fresh[0]]} only)", None))
    elif state == "stale" and entry["age_seconds"] is not None:
        cells.append((f"reading {duration(entry['age_seconds'])} old", "muted"))
    elif entry["age_seconds"] is None:
        cells.append(("no reading yet", "muted"))
    else:
        cells.append((state, "muted"))
    return cells


def _headroom(snapshot: dict) -> str:
    usage = snapshot["usage"]
    now = snapshot["generated_at_epoch"]
    per: dict[str, int] = {}
    for entry in usage["bots"]:
        per[entry["harness"]] = per.get(entry["harness"], 0) + 1
    rows = [
        (e["harness"], e["bot"], e["device"] if per[e["harness"]] > 1 else None, _usage_cells(e, now))
        for e in usage["bots"]
    ]
    for harness in USAGE_HARNESSES:
        if harness in per:
            continue
        text = (
            "not tracked" if harness in USAGE_UNTRACKED
            else "no telemetry" if usage["state"] == "no telemetry"
            else "no reading yet"
        )
        rows.append((harness, harness, None, [(text, "muted")] * 4))
    body = "".join(
        "<tr><td>"
        + _span(f"bot {harness}", bot)
        + ("" if device is None else _span("muted", device))
        + "</td>"
        + "".join(f'<td class="{f"ef-v {cls}" if cls else "ef-v"}">{_e(text)}</td>' for text, cls in cells)
        + "</tr>"
        for harness, bot, device, cells in rows
    )
    head = "".join(f"<th>{name}</th>" for name in
                   ("bot", "5-hour window", "7-day window", "resets", "headroom"))
    title = "Subscription headroom"
    return (
        f'<section class="panel ef-wide" id="headroom" aria-label="{title}">'
        f"<h2>{_span(None, title)}</h2>"
        f'<p class="ef-lede">{_e(HEADROOM_LEDE)}</p>'
        f'<div class="ef-scroll"><table class="ef-usage"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></section>"
    )


def top_row(snapshot: dict) -> str:
    return _row(_headroom(snapshot), rollup_panel(snapshot))


def middle_row(snapshot: dict) -> str:
    return _row(_rounds(snapshot), _savings(snapshot))
