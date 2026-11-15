from __future__ import annotations

import math

from lib.dashboard_pages.common import _b, _chip, _e, _num, _panel, _short, _span, duration
from lib.dashboard_pages.lanes import lane_devices

CSS = """\
.ef-row{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:16px}
.ef-row>.panel{flex:2 1 380px}.ef-row>.panel.ef-wide{flex:3 1 560px}
#headroom,#tokens{grid-column:1/-1}
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
.ef-tiles{display:flex;flex-wrap:wrap;gap:12px}
.ef-tile{flex:1 1 140px;padding:10px 14px 12px;background:var(--bg);box-shadow:var(--elev)}
.ef-tile b{display:block;font:600 30px/1.15 var(--mono);letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.ef-tile span{display:block;font:500 12px/1.3 var(--mono);color:var(--muted)}
.ef-bar{display:flex;height:12px;margin:14px 0 8px;background:var(--line-hi)}
.ef-bar .ok{background:var(--ok)}.ef-bar .bad{background:var(--bad)}
.ef-foot{margin:0;font-size:12px;color:var(--muted)}
.ef-groups{display:flex;flex-direction:column;gap:10px}
.ef-group{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px}
.ef-k{flex:0 0 72px;font:12px/1.5 var(--mono);color:var(--text)}
.ef-sq{display:flex;flex-wrap:wrap;gap:3px;flex:0 0 133px}
.ef-sq span{width:14px;height:14px;background:var(--text-2)}.ef-sq.zero span{background:var(--ok)}
.ef-n{flex:0 0 20px;text-align:right;font:600 13px/1.5 var(--mono);font-variant-numeric:tabular-nums;color:var(--text)}
.ef-n.ok{color:var(--ok)}
.ef-refs{flex:1 1 260px;min-width:0;font:11px/1.5 var(--mono);color:var(--muted)}
.ef-note{margin:14px 0 0;font-size:13px;color:var(--text-2)}
.ef-cells{display:flex;flex-wrap:wrap;gap:3px}
.ef-cells span{width:20px;height:20px;background:var(--muted)}
.ef-cells .claude{background:var(--claude)}.ef-cells .codex{background:var(--codex)}
.ef-cells .cursor{background:var(--cursor)}
.ef-cells .held{border:1px dashed var(--warn);background:none}.ef-cells .idle{border:1px dashed var(--line-hi);background:none}
.ef-legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin:10px 0 14px;font:12px/1.5 var(--mono);font-variant-numeric:tabular-nums}
.ef-legend .claude{color:var(--claude)}.ef-legend .codex{color:var(--codex)}.ef-legend .cursor{color:var(--cursor)}
.ef-legend .grok{color:var(--grok)}.ef-legend .held{color:var(--warn)}.ef-legend .idle{color:var(--muted)}
.ef-devs{display:flex;flex-direction:column;gap:8px}
.ef-dev{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px}
.ef-dev b{flex:0 0 48px;font:600 13px/1.5 var(--mono);color:var(--text)}
.ef-meter{display:block;max-width:100%}.ef-meter .px{height:8px}
.ef-u{font:12px/1.5 var(--mono);font-variant-numeric:tabular-nums;color:var(--muted)}
.ef-lede{margin:0 0 12px;max-width:880px;font-size:13px;color:var(--text-2)}
.ef-scroll{overflow-x:auto}
.ef-usage{min-width:640px;margin:0}
.ef-usage .ef-v{font:12px/1.5 var(--mono)}
.ef-usage .ef-v.ok{color:var(--ok)}.ef-usage .ef-v.warn{color:var(--warn)}.ef-usage .ef-v.bad{color:var(--bad)}
.ef-usage .ef-v.muted{color:var(--muted)}
.ef-usage td .muted{margin-left:6px;font:12px/1.5 var(--mono)}
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
    var root = panel('jobs-lifetime', 'Jobs, lifetime', num(finished) + ' finished');
    if (!root) return;
    var tiles = add(root, el('div', 'ef-tiles'));
    [[t.done, 'done', 'ok'], [t.failed, 'failed', 'bad']].forEach(function (x) {
      var tile = add(tiles, el('div', 'ef-tile'));
      add(tile, el('b', x[2], num(x[0])));
      add(tile, el('span', null, x[1]));
    });
    var bar = add(root, el('div', 'ef-bar'));
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', num(t.done) + ' jobs done, ' + num(t.failed) + ' jobs failed');
    if (!finished) { add(root, el('p', 'ef-foot', 'no finished jobs yet')); return; }
    var done = halfUp(1000 * t.done / finished);
    add(bar, el('span', 'ok')).setAttribute('style', 'flex:0 0 ' + tenths(done) + '%');
    add(bar, el('span', 'bad')).setAttribute('style', 'flex:0 0 ' + tenths(1000 - done) + '%');
    add(root, el('p', 'ef-foot', halfUp(100 * t.done / finished) + '% of ' + num(finished) + ' finished jobs succeeded'));
  }

  var SHIPPED = {merged: true, done: true, 'handed-off': true};
  function shortRef(r) { return short(r.repo) + '#' + (r.source ? String(r.source).replace(/^#/, '') : r.ticket_id); }
  function renderRounds(d) {
    var shipped = (d.tickets || []).filter(function (r) { return SHIPPED[r.state]; });
    var root = panel('rounds', 'Revise rounds before ship', shipped.length + ' shipped tickets');
    if (!root) return;
    if (!shipped.length) { add(root, el('p', 'empty', 'no shipped tickets yet')); return; }
    var groups = {};
    shipped.forEach(function (r) { (groups[r.round] = groups[r.round] || []).push(r); });
    var box = add(root, el('div', 'ef-groups'));
    Object.keys(groups).map(Number).sort(function (a, b) { return a - b; }).forEach(function (n) {
      var rows = groups[n];
      var line = add(box, el('div', 'ef-group'));
      add(line, el('span', 'ef-k', n + (n === 1 ? ' round' : ' rounds')));
      var squares = add(line, el('span', n ? 'ef-sq' : 'ef-sq zero'));
      squares.setAttribute('aria-hidden', 'true');
      rows.forEach(function () { add(squares, el('span')); });
      add(line, el('span', n ? 'ef-n' : 'ef-n ok', rows.length));
      add(line, el('span', 'ef-refs', rows.map(shortRef).join(' · ')));
    });
    add(root, el('p', 'ef-note', ((groups[0] || []).length) + ' of ' + shipped.length + ' shipped with no revise round'));
  }

  var WORKING_BOTS = ['claude', 'codex', 'cursor'];
  function renderWorking(d) {
    var lanes = laneDevices(d);
    var devices = [];
    lanes.devices.forEach(function (x) {
      if (x.enabled && !x.placeholder) devices.push(x);
      else if (x.used) devices.push(Object.assign({}, x, {slot_max: x.used}));
    });
    var running = {claude: 0, codex: 0, cursor: 0, other: 0};
    var held = 0, idle = 0, used = 0, slots = 0, known = true;
    devices.forEach(function (x) {
      x.jobs.forEach(function (j) { running[WORKING_BOTS.indexOf(j.recipient) >= 0 ? j.recipient : 'other'] += 1; });
      held += Math.max(0, x.held - x.jobs.length);
      used += x.used;
      if (x.slot_max === null || x.slot_max === undefined) { known = false; return; }
      idle += Math.max(0, x.slot_max - x.used);
      slots += x.slot_max;
    });
    var root = panel('working', 'Who is working now', known ? used + ' of ' + slots + ' slots used' : used + ' slots used');
    if (!root) return;
    var kinds = WORKING_BOTS.concat(['other']);
    var cellCount = held + idle;
    kinds.forEach(function (k) { cellCount += running[k]; });
    var label = kinds.filter(function (k) { return running[k]; }).map(function (k) { return running[k] + ' running ' + k; });
    var cells = add(root, el('div', 'ef-cells'));
    cells.setAttribute('role', 'img');
    cells.setAttribute('aria-label', cellCount + ' slots: ' + label.concat([held + ' held', idle + ' idle']).join(', '));
    kinds.concat(['held', 'idle']).forEach(function (k) {
      var n = k === 'held' ? held : k === 'idle' ? idle : running[k];
      for (var i = 0; i < n; i++) add(cells, el('span', k));
    });
    var legend = add(root, el('div', 'ef-legend'));
    WORKING_BOTS.forEach(function (k) { add(legend, el('span', k, k + ' ' + running[k])); });
    add(legend, el('span', 'grok', 'grok ' + lanes.wakes.length));
    add(legend, el('span', 'held', 'held ' + held));
    add(legend, el('span', 'idle', 'idle ' + idle));
    var list = add(root, el('div', 'ef-devs'));
    devices.forEach(function (x) {
      var line = add(list, el('div', 'ef-dev'));
      add(line, el('b', null, x.name));
      if (x.slot_max === null || x.slot_max === undefined) { add(line, el('span', 'ef-u', x.used + ' slots')); return; }
      var wrap = add(line, el('span', 'ef-meter'));
      wrap.setAttribute('aria-hidden', 'true');
      wrap.setAttribute('style', 'flex:0 0 ' + 16 * x.slot_max + 'px;width:' + 16 * x.slot_max + 'px');
      var meter = add(wrap, PX.strip('meter-' + x.name, {effect: 'meter', tint: '#9ece6a', height: 8}));
      meter.setAttribute('data-used', String(x.used));
      meter.setAttribute('data-slots', String(x.slot_max));
      add(line, el('span', 'ef-u', x.used + ' / ' + x.slot_max + ' slots'));
    });
  }

  var USAGE_HARNESSES = ['claude', 'codex', 'cursor', 'grok'];
  var USAGE_UNTRACKED = {cursor: true, grok: true};
  function pct(x) { return halfUp(x * 100) + '%'; }
  function usageLevel(x) { return x >= 0.5 ? 'ok' : x >= 0.2 ? 'warn' : 'bad'; }
  function usageCells(e, now) {
    if (e.state === 'not tracked' || e.state === 'no telemetry') {
      return [0, 1, 2, 3].map(function () { return [e.state, 'muted']; });
    }
    var cells = ['five_hour', 'seven_day'].map(function (w) {
      return e[w].state === 'ok' ? [pct(e[w].used) + ' used', null] : [e[w].state, 'muted'];
    });
    var resets = ['five_hour', 'seven_day'].filter(function (w) { return e[w].state === 'ok' && e[w].resets_at !== null; })
      .map(function (w) { return e[w].resets_at; });
    cells.push(resets.length ? ['in ' + dur(Math.min.apply(null, resets) - now), null] : ['—', 'muted']);
    if (e.headroom !== null) cells.push([pct(e.headroom), usageLevel(e.headroom)]);
    else if (e.state === 'stale' && e.age_seconds !== null) cells.push(['reading ' + dur(e.age_seconds) + ' old', 'muted']);
    else if (e.age_seconds === null) cells.push(['no reading yet', 'muted']);
    else cells.push([e.state, 'muted']);
    return cells;
  }
  function renderHeadroom(d) {
    var usage = d.usage;
    var root = panel('headroom', 'Subscription headroom');
    if (!root) return;
    add(root.children[0], chip('proposed', 'accent'));
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
WORKING_BOTS = ("claude", "codex", "cursor")
USAGE_HARNESSES = ("claude", "codex", "cursor", "grok")
USAGE_UNTRACKED = ("cursor", "grok")
WINDOWS = ("five_hour", "seven_day")


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
    return _panel("savings", "Handoff savings", inner, count="scripted / (scripted + grok wakes)",
                  extra="ef-wide")


def _jobs_lifetime(snapshot: dict) -> str:
    totals = snapshot["queue"]["totals"]
    done, failed = totals["done"], totals["failed"]
    finished = done + failed
    tiles = "".join(
        f'<div class="ef-tile">{_b(value, cls)}{_span(None, text)}</div>'
        for value, text, cls in ((done, "done", "ok"), (failed, "failed", "bad"))
    )
    label = f"{_num(done)} jobs done, {_num(failed)} jobs failed"
    if finished:
        part = _half_up(1000 * done / finished)
        bar = (
            f'<span class="ok" style="flex:0 0 {_tenths(part)}%"></span>'
            f'<span class="bad" style="flex:0 0 {_tenths(1000 - part)}%"></span>'
        )
        foot = f"{_half_up(100 * done / finished)}% of {_num(finished)} finished jobs succeeded"
    else:
        bar, foot = "", "no finished jobs yet"
    inner = (
        f'<div class="ef-tiles">{tiles}</div>'
        f'<div class="ef-bar" role="img" aria-label="{label}">{bar}</div>'
        f'<p class="ef-foot">{_e(foot)}</p>'
    )
    return _panel("jobs-lifetime", "Jobs, lifetime", inner, count=f"{_num(finished)} finished")


def _short_ref(row: dict) -> str:
    source = row.get("source")
    return f"{_short(row['repo'])}#{str(source).removeprefix('#') if source else row['ticket_id']}"


def _rounds(snapshot: dict) -> str:
    shipped = [row for row in snapshot["tickets"] if row["state"] in SHIPPED]
    count = f"{len(shipped)} shipped tickets"
    if not shipped:
        return _panel("rounds", "Revise rounds before ship",
                      '<p class="empty">no shipped tickets yet</p>', count=count, extra="ef-wide")
    groups: dict[int, list[dict]] = {}
    for row in shipped:
        groups.setdefault(row["round"], []).append(row)
    lines = "".join(
        '<div class="ef-group">'
        + _span("ef-k", f"{n} round" if n == 1 else f"{n} rounds")
        + f'<span class="{"ef-sq" if n else "ef-sq zero"}" aria-hidden="true">{"<span></span>" * len(rows)}</span>'
        + _span("ef-n" if n else "ef-n ok", len(rows))
        + _span("ef-refs", " · ".join(_short_ref(row) for row in rows))
        + "</div>"
        for n, rows in sorted(groups.items())
    )
    note = f"{len(groups.get(0, []))} of {len(shipped)} shipped with no revise round"
    return _panel(
        "rounds", "Revise rounds before ship",
        f'<div class="ef-groups">{lines}</div><p class="ef-note">{note}</p>',
        count=count, extra="ef-wide",
    )


def _meter(name: str, used: int, slots: int) -> str:
    return (
        f'<canvas class="px" data-px="meter" data-tint="#9ece6a" data-px-key="meter-{_e(name)}" '
        f'aria-hidden="true" style="--px:#9ece6a;height:8px" data-used="{used}" data-slots="{slots}"></canvas>'
    )


def _working(snapshot: dict) -> str:
    lanes, wakes = lane_devices(snapshot)
    devices = []
    for device in lanes:
        if device["enabled"] and not device["placeholder"]:
            devices.append(device)
        elif device["used"]:
            devices.append({**device, "slot_max": device["used"]})
    running = {kind: 0 for kind in (*WORKING_BOTS, "other")}
    held = idle = used = slots = 0
    known = True
    for device in devices:
        for job in device["jobs"]:
            running[job["recipient"] if job["recipient"] in WORKING_BOTS else "other"] += 1
        held += max(0, device["held"] - len(device["jobs"]))
        used += device["used"]
        if device["slot_max"] is None:
            known = False
            continue
        idle += max(0, device["slot_max"] - device["used"])
        slots += device["slot_max"]
    counts = {**running, "held": held, "idle": idle}
    total = sum(counts.values())
    label = ", ".join(
        [f"{n} running {kind}" for kind, n in running.items() if n] + [f"{held} held", f"{idle} idle"]
    )
    cells = "".join(f'<span class="{kind}"></span>' * n for kind, n in counts.items())
    legend = (
        "".join(_span(kind, f"{kind} {running[kind]}") for kind in WORKING_BOTS)
        + _span("grok", f"grok {len(wakes)}") + _span("held", f"held {held}") + _span("idle", f"idle {idle}")
    )
    rows = []
    for device in devices:
        name, cap = device["name"], device["slot_max"]
        if cap is None:
            rows.append(f'<div class="ef-dev">{_b(name)}{_span("ef-u", f"{device["used"]} slots")}</div>')
            continue
        rows.append(
            f'<div class="ef-dev"><b>{_e(name)}</b>'
            f'<span class="ef-meter" aria-hidden="true" style="flex:0 0 {16 * cap}px;width:{16 * cap}px">'
            f"{_meter(name, device['used'], cap)}</span>"
            + _span("ef-u", f"{device['used']} / {cap} slots")
            + "</div>"
        )
    inner = (
        f'<div class="ef-cells" role="img" aria-label="{total} slots: {_e(label)}">{cells}</div>'
        f'<div class="ef-legend">{legend}</div><div class="ef-devs">{"".join(rows)}</div>'
    )
    count = f"{used} of {slots} slots used" if known else f"{used} slots used"
    return _panel("working", "Who is working now", inner, count=count)


def _usage_cells(entry: dict, now: float) -> list[tuple[str, str | None]]:
    state = entry["state"]
    if state in ("not tracked", "no telemetry"):
        return [(state, "muted")] * 4
    cells: list[tuple[str, str | None]] = [
        (f"{_half_up(entry[w]['used'] * 100)}% used", None) if entry[w]["state"] == "ok"
        else (entry[w]["state"], "muted")
        for w in WINDOWS
    ]
    resets = [entry[w]["resets_at"] for w in WINDOWS
              if entry[w]["state"] == "ok" and entry[w]["resets_at"] is not None]
    cells.append((f"in {duration(min(resets) - now)}", None) if resets else ("—", "muted"))
    room = entry["headroom"]
    if room is not None:
        level = "ok" if room >= 0.5 else "warn" if room >= 0.2 else "bad"
        cells.append((f"{_half_up(room * 100)}%", level))
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
        f'<section class="panel" id="headroom" aria-label="{title}">'
        f'<h2>{_span(None, title)}{_chip("proposed", "accent")}</h2>'
        f'<p class="ef-lede">{_e(HEADROOM_LEDE)}</p>'
        f'<div class="ef-scroll"><table class="ef-usage"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></section>"
    )


def savings_row(snapshot: dict) -> str:
    return _row(_savings(snapshot), _jobs_lifetime(snapshot))


def rounds_row(snapshot: dict) -> str:
    return _row(_rounds(snapshot), _working(snapshot))
