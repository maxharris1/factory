"""job.html (Tickets): the Tickets board and, as its table view, the tickets table."""

from __future__ import annotations

import re
from collections import Counter

from lib.dashboard_flow import GLOSS, PHASE_OVERRIDES, PHASES, PIPELINE, SHIPPED, ticket_phase, ticket_phases
from lib.dashboard_pages.activity import _VERDICT
from lib.dashboard_pages.lanes import TILE_STATE
from lib.dashboard_pages.common import (
    anchor, _bot, _chip, duration, _e, _panel, _short, _span, _td, _ticket,
)


CSS = """\
.tk-filter{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-family:var(--mono)}
.tk-group{display:flex;flex-wrap:wrap;gap:8px}
.tk-view{flex-wrap:nowrap}
.tk-grow{flex-grow:1}
.tk-btn{height:44px;padding:0 14px;font-family:inherit;font-size:12px;cursor:pointer;
border:1px solid var(--line-hi);background:var(--panel);color:var(--text)}
.tk-btn[aria-pressed=true]{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 9%,transparent);color:var(--ok)}
.tk-btn:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.tk-board{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(250px,100%),1fr));gap:12px;align-items:start;
font-family:var(--mono)}
.tk-col{display:flex;flex-direction:column;gap:8px;min-width:0}
.tk-head{display:flex;flex-direction:column;gap:4px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.tk-headrow{display:flex;align-items:center;gap:8px}
.tk-glyph{display:block;width:16px}
.tk-name{font-size:13px;font-weight:600;color:var(--text)}
.tk-count{margin-left:auto;font-size:12px;color:var(--muted)}
.tk-states{font-size:11px;color:var(--muted)}
.tk-empty{padding:16px;border:1px dashed var(--line-hi);font-size:12px;color:var(--muted)}
.tk-card{display:flex;flex-direction:column;min-width:0;background:var(--panel-hi);box-shadow:var(--elev)}
.tk-card.tk-stop{border:1px solid color-mix(in srgb,var(--bad) 45%,transparent);box-shadow:none}
.tk-strip{display:block}
.tk-body{display:flex;flex-direction:column;gap:6px;padding:8px 10px 10px}
.tk-id{display:flex;align-items:baseline;gap:8px}
.tk-id a,.tk-tid{font-size:13px;font-weight:600;overflow-wrap:anywhere;color:var(--text);text-decoration:none}
.tk-id a:hover{color:var(--ok)}
.tk-tag{font-size:10px;line-height:1.5;padding:0 4px;border:1px solid var(--line-hi);color:var(--muted)}
.tk-state{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-2)}
.tk-pips{display:flex;gap:2px}
.tk-pips span{width:6px;height:6px;background:var(--line-hi)}
.tk-pips .done{background:var(--text)}
.tk-pips .now{background:var(--accent)}
.tk-round{font-size:11px;color:var(--muted)}
.tk-reason{font-size:12px;color:var(--text-2);overflow-wrap:anywhere}
.tk-wait{font-size:12px;color:var(--warn);overflow-wrap:anywhere}
.tk-run{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;font-size:12px}
.tk-dev{color:var(--muted)}
.tk-t{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.tk-out{color:var(--text-2)}
.tk-none{font-size:12px;color:var(--muted)}
.tk-shelf{display:flex;flex-direction:column;gap:16px;font-family:var(--mono)}
.tk-sub{display:flex;flex-direction:column;gap:8px}
.tk-label{display:flex;align-items:center;gap:8px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.tk-tiles{display:flex;flex-wrap:wrap;gap:6px}
.tk-tile{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;background:var(--bg);box-shadow:var(--elev)}
.tk-blk{width:8px;height:8px;background:var(--ok)}
.tk-tn{font-size:12px;color:var(--text)}
.tk-r{font-size:11px;color:var(--muted)}
.tk-scroll{overflow-x:auto}
.tk-stops{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(250px,100%),1fr));gap:8px;align-items:start}
@media (scripting:none){#tickets[hidden]{display:block !important}}
"""


SCRIPT_TICKETS = r"""

  var BOARD = __BOARD__;
  var WAITING = {'awaiting-ok': 1, 'waiting-max': 1};
  var WAIT_TINT = '#ff9e64';
  var BOARD_EMPTY = {Ask: 'nothing waiting on questions'};
  var tkProject = tkLoad('tk-project') || '';
  var tkView = tkLoad('tk-view') === 'table' ? 'table' : 'board';

  function tkLoad(key) {
    try { return window.sessionStorage.getItem(key); } catch (e) { return null; }
  }
  function tkSave(key, value) {
    try { window.sessionStorage.setItem(key, value); } catch (e) {}
  }

  function activeJobs(d) {
    return (d.running || []).map(function (r) { return {row: r, state: 'running'}; })
      .concat((d.queued || []).map(function (r) { return {row: r, state: 'queued'}; }));
  }

  function ticketKey(r) { return r.repo + '#' + r.ticket_id; }
  function joinKey(r) { return ticketKey(r).toLowerCase(); }
  function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }
  function listed(names) {
    if (!names.length) return 'stopped';
    return names.length === 1 ? names[0] : names.slice(0, -1).join(', ') + ' or ' + names[names.length - 1];
  }

  function phaseTable(chart) {
    var phases = {}, order = [];
    if (chart) {
      var legend = {}, groups = {}, next = {};
      chart.legend.forEach(function (k) { legend[k.group] = k.name; });
      chart.states.forEach(function (st) { groups[st.name] = st.group; next[st.name] = []; });
      chart.edges.forEach(function (e) { next[e.from].push(e.to); });
      order = [chart.start];
      for (var i = 0; i < order.length; i++) {
        next[order[i]].forEach(function (n) { if (order.indexOf(n) < 0) order.push(n); });
      }
      Object.keys(groups).forEach(function (n) { if (order.indexOf(n) < 0) order.push(n); });
      order.forEach(function (n) {
        var phase = BOARD.overrides[n] || legend[groups[n]];
        phases[n] = BOARD.phases.indexOf(phase) >= 0 ? phase : 'Stop';
      });
    }
    function states(phase) { return order.filter(function (n) { return phases[n] === phase; }); }
    return {
      states: phases,
      columns: order.length ? BOARD.pipeline.map(function (phase) {
        return {phase: phase, states: states(phase), glyph: BOARD.glyphs[phase]};
      }) : [],
      shelf: BOARD.shipped.filter(function (n) { return phases[n] === 'Shipped'; }).map(function (n) {
        return {state: n, label: BOARD.gloss.hasOwnProperty(n) ? n + ' · ' + BOARD.gloss[n] : n};
      }),
      stop: {states: states('Stop'), glyph: BOARD.glyphs.Stop}
    };
  }

  function boardOf(d, project) {
    var table = phaseTable(d.statechart);
    var rows = d.tickets || [];
    var counts = new Map();
    rows.forEach(function (r) { counts.set(r.repo, (counts.get(r.repo) || 0) + 1); });
    var projects = [];
    counts.forEach(function (n, repo) { projects.push({repo: repo, short: short(repo), count: n}); });
    projects.sort(function (a, b) { return b.count - a.count || cmp(a.short, b.short) || cmp(a.repo, b.repo); });
    var live = new Map(), queued = new Map(), verdicts = new Map(), reasons = new Map();
    (d.running || []).forEach(function (r) {
      var best = live.get(joinKey(r));
      if (!best || (r.elapsed_seconds || 0) < (best.elapsed_seconds || 0)) live.set(joinKey(r), r);
    });
    function first(map, list, value) {
      (list || []).forEach(function (r) { if (!map.has(joinKey(r))) map.set(joinKey(r), value(r)); });
    }
    first(queued, d.queued, function (r) { return r; });
    first(verdicts, d.poller ? d.poller.verdicts : null, function (r) { return r; });
    first(reasons, d.stuck ? d.stuck.tickets : null, function (r) { return r.reason; });
    var cards = rows.filter(function (r) { return !project || r.repo === project; }).map(function (t) {
      var key = joinKey(t);
      var known = Object.prototype.hasOwnProperty.call(table.states, t.state);
      return {ticket: t, phase: t.stuck || !known ? 'Stop' : table.states[t.state],
              live: live.get(key) || null, queued: queued.get(key) || null,
              verdict: verdicts.get(key) || null, reason: reasons.get(key) || null};
    });
    var columns = table.columns.map(function (col) {
      return {phase: col.phase, glyph: col.glyph, states: col.states,
              cards: cards.filter(function (c) { return c.phase === col.phase; })};
    });
    return {
      projects: projects, total: rows.length, tracked: cards.length, available: table.columns.length > 0,
      inFlight: columns.reduce(function (n, col) { return n + col.cards.length; }, 0),
      columns: columns,
      shelf: table.shelf.map(function (g) {
        return {state: g.state, label: g.label,
                tiles: cards.filter(function (c) { return c.phase === 'Shipped' && c.ticket.state === g.state; })};
      }),
      stopped: cards.filter(function (c) { return c.phase === 'Stop'; }),
      stop: table.stop
    };
  }

  function tkButton(parent, text, attr, value, on, pick) {
    var b = add(parent, el('button', 'tk-btn', text));
    b.setAttribute('type', 'button');
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.setAttribute(attr, value);
    b.addEventListener('click', function () { pick(); if (latest) render(latest); });
  }

  function tkGroup(root, cls, label) {
    var group = add(root, el('div', cls));
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', label);
    return group;
  }

  function tkGlyph(key, glyph) {
    var span = el('span', 'tk-glyph');
    span.setAttribute('aria-hidden', 'true');
    add(span, PX.strip(key, glyph));
    return span;
  }

  function tkRound(parent, t, job) {
    var rnd = job ? job.round : null;
    var m = /^([0-9]+)\/([0-9]+)$/.exec(rnd || '');
    if (m) {
      var n = Number(m[1]);
      var pips = add(parent, el('span', 'tk-pips'));
      pips.setAttribute('aria-hidden', 'true');
      for (var i = 0; i < Number(m[2]); i++) add(pips, el('span', i < n - 1 ? 'done' : i === n - 1 ? 'now' : null));
      add(parent, el('span', 'tk-round', 'round ' + rnd));
    } else if (job) add(parent, el('span', 'tk-round', 'round —'));
    else if (t.round) add(parent, el('span', 'tk-round', 'round ' + t.round));
  }

  function tkChips(parent, live) {
    if (!live) return;
    var chips = BOARD.chips[live.job_state];
    if (chips) add(parent, chip(chips[0], chips[1]));
    if (live.overdue) add(parent, chip(BOARD.chips.overdue[0], BOARD.chips.overdue[1]));
  }

  function tkCard(c) {
    var t = c.ticket, job = c.live || c.queued;
    var stop = c.phase === 'Stop', waiting = WAITING.hasOwnProperty(t.state);
    var card = el('div', stop ? 'tk-card tk-stop' : 'tk-card');
    card.setAttribute('data-ticket', ticketKey(t));
    var key = 'ticket-' + ticketKey(t);
    var strip = stop ? PX.strip(key, {state: 'stuck', height: 8})
      : waiting ? PX.strip(key, {effect: 'static', tint: WAIT_TINT, height: 8})
      : c.live ? PX.jobStrip(c.live, 8) : null;
    if (strip) {
      var box = add(card, el('div', 'tk-strip'));
      box.setAttribute('aria-hidden', 'true');
      add(box, strip);
    }
    var main = add(card, el('div', 'tk-body'));
    var id = add(main, el('div', 'tk-id'));
    if (job) add(id, link(null, 'job.html#' + anchor(job.job))).textContent = ticketOf(t);
    else add(id, el('span', 'tk-tid', ticketOf(t)));
    add(id, el('span', 'tk-tag', t.tracker));
    var state = add(main, el('div', 'tk-state'));
    add(state, el('span', null, t.state));
    tkChips(state, c.live);
    if (stop) {
      if (t.stuck) add(state, chip('stuck', 'bad'));
      if (c.reason) add(main, el('div', 'tk-reason', c.reason));
      return card;
    }
    tkRound(state, t, job);
    var waitLine = waiting && t.waiting_on;
    if (waitLine) add(main, el('div', 'tk-wait', 'waiting on ' + t.waiting_on));
    if (job) {
      var run = add(main, el('div', 'tk-run'));
      add(run, bot(job.recipient));
      if (!c.live) add(run, el('span', 'tk-dev', 'queued'));
      else if (job.device) add(run, el('span', 'tk-dev', '@ ' + job.device));
      ticking(add(run, el('span', 'tk-t')), c.live ? job.elapsed_seconds : job.age_seconds, 'T+ ');
      return card;
    }
    if (BOARD.idle.indexOf(c.phase) >= 0) add(main, el('div', 'tk-none', 'no job running'));
    if (c.verdict && !(waitLine && c.verdict.kind === 'waiting')) {
      var said = add(main, el('div', 'tk-run'));
      add(said, chip(c.verdict.kind, VERDICT[c.verdict.kind] || 'muted'));
      add(said, el('span', 'tk-out', c.verdict.outcome));
    }
    return card;
  }

  function tkColumn(col) {
    var box = el('div', 'tk-col');
    box.setAttribute('data-phase', col.phase);
    var head = add(box, el('div', 'tk-head'));
    var row = add(head, el('div', 'tk-headrow'));
    add(row, tkGlyph('phase-' + col.phase, col.glyph));
    add(row, el('span', 'tk-name', col.phase));
    add(row, el('span', 'tk-count', col.cards.length));
    add(head, el('span', 'tk-states', col.states.join(' · ')));
    var empty = BOARD_EMPTY[col.phase] || 'nothing in ' + col.phase.toLowerCase();
    if (!col.cards.length) add(box, el('div', 'tk-empty', empty));
    col.cards.forEach(function (c) { add(box, tkCard(c)); });
    return box;
  }

  function renderBoard(d) {
    if (tkProject && !(d.tickets || []).some(function (r) { return r.repo === tkProject; })) {
      tkProject = '';
      tkSave('tk-project', '');
    }
    var b = boardOf(d, tkProject);

    var filter = byId('ticket-filter');
    if (filter) {
      filter.replaceChildren();
      var group = tkGroup(filter, 'tk-group', 'Project');
      tkButton(group, 'all ' + b.total, 'data-project', '', !tkProject, function () { tkProject = ''; tkSave('tk-project', ''); });
      b.projects.forEach(function (p) {
        tkButton(group, p.short + ' ' + p.count, 'data-project', p.repo, tkProject === p.repo,
                 function () { tkProject = p.repo; tkSave('tk-project', p.repo); });
      });
      add(filter, el('span', 'tk-grow'));
      var view = tkGroup(filter, 'tk-group tk-view', 'View');
      ['board', 'table'].forEach(function (name) {
        tkButton(view, name, 'data-view', name, tkView === name, function () { tkView = name; tkSave('tk-view', name); });
      });
    }

    var pipeline = panel('pipeline', 'Pipeline',
                         b.tracked + ' tracked' + (b.available ? ' · ' + b.inFlight + ' in flight' : ''));
    if (pipeline) {
      pipeline.hidden = tkView !== 'board';
      if (!b.available) add(pipeline, el('p', 'empty', 'statechart unavailable'));
      else {
        var grid = add(pipeline, el('div', 'tk-board'));
        b.columns.forEach(function (col) { add(grid, tkColumn(col)); });
      }
    }

    var tiles = b.shelf.reduce(function (n, g) { return n + g.tiles.length; }, 0);
    var shipped = panel('shipped', 'Shipped', b.available ? tiles + ' tickets' : null);
    if (!shipped) return;
    shipped.hidden = tkView !== 'board';
    if (!b.available) { add(shipped, el('p', 'empty', 'statechart unavailable')); return; }
    var shelf = add(shipped, el('div', 'tk-shelf'));
    b.shelf.forEach(function (g) {
      var sub = add(shelf, el('div', 'tk-sub'));
      sub.setAttribute('data-state', g.state);
      add(add(sub, el('div', 'tk-label')), el('span', null, g.label + ' (' + g.tiles.length + ')'));
      if (!g.tiles.length) { add(sub, el('div', 'tk-none', 'nothing ' + g.state)); return; }
      var row = add(sub, el('div', 'tk-tiles'));
      g.tiles.forEach(function (c) {
        var tile = add(row, el('span', 'tk-tile'));
        tile.setAttribute('data-ticket', ticketKey(c.ticket));
        add(tile, el('span', 'tk-blk')).setAttribute('aria-hidden', 'true');
        add(tile, el('span', 'tk-tn', ticketOf(c.ticket)));
        add(tile, el('span', 'tk-r', 'r' + c.ticket.round));
      });
    });
    var stop = add(shelf, el('div', 'tk-sub'));
    stop.setAttribute('data-phase', 'Stop');
    var label = add(stop, el('div', 'tk-label'));
    add(label, tkGlyph('phase-Stop', b.stop.glyph));
    add(label, el('span', null, 'stopped (' + b.stopped.length + ')'));
    if (!b.stopped.length) { add(stop, el('div', 'tk-none', 'nothing ' + listed(b.stop.states))); return; }
    var cards = add(stop, el('div', 'tk-stops'));
    b.stopped.forEach(function (c) { add(cards, tkCard(c)); });
  }

  function renderTickets(d) {
    var rows = (d.tickets || []).filter(function (r) { return !tkProject || r.repo === tkProject; });
    var root = panel('tickets', 'Tickets', rows.length + ' tracked');
    if (!root) return;
    root.hidden = tkView !== 'table';
    if (!rows.length) { add(root, el('p', 'empty', 'no tracked tickets')); return; }
    var table = add(add(root, el('div', 'tk-scroll')), el('table'));
    var head = add(add(table, el('thead')), el('tr'));
    ['ticket', 'tracker', 'state', 'round', 'gates', 'stuck'].forEach(function (h) { add(head, el('th', null, h)); });
    var tbody = add(table, el('tbody'));
    rows.forEach(function (r) {
      var tr = add(tbody, el('tr'));
      add(tr, el('td', null, ticketIn(r.repo, r)));
      add(tr, el('td', null, r.tracker));
      add(tr, el('td', r.stuck ? 'bad' : null, r.state));
      add(tr, el('td', 'num', r.round));
      add(tr, el('td', 'num', r.gate_count));
      add(tr, el('td', r.stuck ? 'bad' : null, r.stuck ? 'yes' : 'no'));
    });
  }
"""


GLYPH_TINT = "#a9b1d6"
PHASE_GLYPH = {
    "Ask": {"effect": "twinkle", "seed": 71},
    "Plan": {"effect": "field", "seed": 72},
    "Build": {"effect": "fill", "progress": 0.6, "seed": 73},
    "Review": {"effect": "scan", "seed": 74},
    "Ship": {"effect": "off", "done": True, "seed": 75},
    "Stop": {"effect": "static", "tint": "#f7768e", "seed": 76},
}
WAITING = ("awaiting-ok", "waiting-max")
IDLE = ("Plan", "Build", "Review")
JOB_CHIPS = {
    **{name: TILE_STATE[name]["chip"] for name in ("claimed", "stale", "dead")},
    "overdue": ["overdue", "bad"],
}
_WAIT_TINT = "#ff9e64"
_EMPTY = {"Ask": "nothing waiting on questions"}
_ROUND = re.compile(r"([0-9]+)/([0-9]+)")


def _phase_glyph(phase: str) -> dict:
    return {"tint": GLYPH_TINT, **PHASE_GLYPH[phase], "cell": 2, "height": 16}


def board_constants() -> dict:
    return {
        "pipeline": PIPELINE, "phases": PHASES, "overrides": PHASE_OVERRIDES, "shipped": SHIPPED, "gloss": GLOSS,
        "idle": IDLE, "chips": JOB_CHIPS,
        "glyphs": {phase: _phase_glyph(phase) for phase in PHASE_GLYPH},
    }


def phase_table(chart: dict | None) -> dict:
    phases = ticket_phases(chart)

    def states(phase: str) -> list[str]:
        return [name for name, found in phases.items() if found == phase]

    return {
        "states": phases,
        "columns": [
            {"phase": phase, "states": states(phase), "glyph": _phase_glyph(phase)} for phase in PIPELINE
        ] if phases else [],
        "shelf": [
            {"state": name, "label": f"{name} · {GLOSS[name]}" if name in GLOSS else name}
            for name in SHIPPED if phases.get(name) == "Shipped"
        ],
        "stop": {"states": states("Stop"), "glyph": _phase_glyph("Stop")},
    }


def _key(row: dict) -> str:
    return f"{row['repo']}#{row['ticket_id']}"


def _join(row: dict) -> str:
    return _key(row).casefold()


def _first(rows: list | None) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for row in rows or ():
        found.setdefault(_join(row), row)
    return found


def board(snapshot: dict, project: str | None = None) -> dict:
    table = phase_table(snapshot.get("statechart"))
    rows = snapshot["tickets"]
    counts = Counter(row["repo"] for row in rows)
    live: dict[str, dict] = {}
    for row in snapshot["running"]:
        best = live.get(_join(row))
        if best is None or (row.get("elapsed_seconds") or 0) < (best.get("elapsed_seconds") or 0):
            live[_join(row)] = row
    queued = _first(snapshot["queued"])
    verdicts = _first(snapshot["poller"].get("verdicts"))
    reasons = {key: row.get("reason") for key, row in _first(snapshot["stuck"]["tickets"]).items()}
    cards = [
        {
            "ticket": row,
            "phase": ticket_phase(table["states"], row["state"], row["stuck"]),
            "live": live.get(_join(row)),
            "queued": queued.get(_join(row)),
            "verdict": verdicts.get(_join(row)),
            "reason": reasons.get(_join(row)),
        }
        for row in rows
        if project in (None, row["repo"])
    ]
    columns = [{**col, "cards": [c for c in cards if c["phase"] == col["phase"]]} for col in table["columns"]]
    return {
        "projects": sorted(counts.items(), key=lambda item: (-item[1], _short(item[0]), item[0])),
        "total": len(rows),
        "tracked": len(cards),
        "available": bool(table["columns"]),
        "in_flight": sum(len(col["cards"]) for col in columns),
        "columns": columns,
        "shelf": [
            {**group, "tiles": [c for c in cards if c["phase"] == "Shipped" and c["ticket"]["state"] == group["state"]]}
            for group in table["shelf"]
        ],
        "stopped": [c for c in cards if c["phase"] == "Stop"],
        "stop": table["stop"],
    }


def _px():
    from lib.dashboard_pages import shell

    return shell


def _glyph(key: str, glyph: dict) -> str:
    return f'<span class="tk-glyph" aria-hidden="true">{_px().px_strip(key, **glyph)}</span>'


def _round(ticket: dict, job: dict | None) -> str:
    rnd = job.get("round") if job else None
    match = _ROUND.fullmatch(rnd or "")
    if match:
        done = int(match.group(1))
        pips = "".join(
            '<span class="done"></span>' if i < done - 1 else '<span class="now"></span>' if i == done - 1
            else "<span></span>"
            for i in range(int(match.group(2)))
        )
        return f'<span class="tk-pips" aria-hidden="true">{pips}</span>{_span("tk-round", f"round {rnd}")}'
    if job:
        return _span("tk-round", "round —")
    return _span("tk-round", f"round {ticket['round']}") if ticket["round"] else ""


def _chips(live: dict | None) -> str:
    if not live:
        return ""
    chips = [JOB_CHIPS.get(str(live.get("job_state")))] + [JOB_CHIPS["overdue"] if live.get("overdue") else None]
    return "".join(_chip(text, tone) for text, tone in filter(None, chips))


def _card(card: dict) -> str:
    ticket, live = card["ticket"], card["live"]
    job = live or card["queued"]
    stop = card["phase"] == "Stop"
    waiting = ticket["state"] in WAITING
    key = f"ticket-{_key(ticket)}"
    px = _px()
    if stop:
        strip = px.px_strip(key, state="stuck", height=8)
    elif waiting:
        strip = px.px_strip(key, effect="static", tint=_WAIT_TINT, height=8)
    else:
        strip = px.px_job_strip(live, height=8) if live else ""
    name = _ticket(ticket)
    ident = f'<a href="job.html#{anchor(job["job"])}">{_e(name)}</a>' if job else _span("tk-tid", name)
    state = _span(None, ticket["state"]) + _chips(live)
    lines = ""
    if stop:
        state += _chip("stuck", "bad") if ticket["stuck"] else ""
        lines = f'<div class="tk-reason">{_e(card["reason"])}</div>' if card["reason"] else ""
    else:
        state += _round(ticket, job)
        wait_line = waiting and ticket.get("waiting_on")
        if wait_line:
            lines += f'<div class="tk-wait">{_e("waiting on " + str(ticket["waiting_on"]))}</div>'
        if job:
            where = _span("tk-dev", "queued") if not live else (
                _span("tk-dev", f"@ {job['device']}") if job.get("device") else ""
            )
            since = _span("tk-t", "T+ " + duration(job["elapsed_seconds"] if live else job.get("age_seconds")))
            lines += f'<div class="tk-run">{_bot(job["recipient"])}{where}{since}</div>'
        else:
            lines += '<div class="tk-none">no job running</div>' if card["phase"] in IDLE else ""
            verdict = card["verdict"]
            if verdict and not (wait_line and verdict["kind"] == "waiting"):
                lines += (
                    f'<div class="tk-run">{_chip(verdict["kind"], _VERDICT.get(verdict["kind"], "muted"))}'
                    f'{_span("tk-out", verdict["outcome"])}</div>'
                )
    return (
        f'<div class="{"tk-card tk-stop" if stop else "tk-card"}" data-ticket="{_e(_key(ticket))}">'
        + (f'<div class="tk-strip" aria-hidden="true">{strip}</div>' if strip else "")
        + f'<div class="tk-body"><div class="tk-id">{ident}{_span("tk-tag", ticket["tracker"])}</div>'
        f'<div class="tk-state">{state}</div>{lines}</div></div>'
    )


def _column(col: dict) -> str:
    phase = col["phase"]
    cards = "".join(_card(card) for card in col["cards"]) or (
        f'<div class="tk-empty">{_e(_EMPTY.get(phase, f"nothing in {phase.lower()}"))}</div>'
    )
    return (
        f'<div class="tk-col" data-phase="{_e(phase)}"><div class="tk-head"><div class="tk-headrow">'
        f'{_glyph(f"phase-{phase}", col["glyph"])}{_span("tk-name", phase)}{_span("tk-count", len(col["cards"]))}'
        f'</div>{_span("tk-states", " · ".join(col["states"]))}</div>{cards}</div>'
    )


def _button(text: str, attr: str, value: str, on: bool) -> str:
    return (
        f'<button class="tk-btn" type="button" aria-pressed="{"true" if on else "false"}" '
        f'{attr}="{_e(value)}">{_e(text)}</button>'
    )


def _listed(names: list[str]) -> str:
    if not names:
        return "stopped"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _shelf(found: dict) -> str:
    groups = ""
    for group in found["shelf"]:
        tiles = "".join(
            f'<span class="tk-tile" data-ticket="{_e(_key(c["ticket"]))}"><span class="tk-blk" aria-hidden="true">'
            f'</span>{_span("tk-tn", _ticket(c["ticket"]))}{_span("tk-r", "r" + str(c["ticket"]["round"]))}</span>'
            for c in group["tiles"]
        )
        body = (
            f'<div class="tk-tiles">{tiles}</div>' if tiles
            else f'<div class="tk-none">{_e("nothing " + group["state"])}</div>'
        )
        label = _span(None, f"{group['label']} ({len(group['tiles'])})")
        groups += (
            f'<div class="tk-sub" data-state="{_e(group["state"])}"><div class="tk-label">{label}</div>{body}</div>'
        )
    stopped = found["stopped"]
    cards = "".join(_card(card) for card in stopped)
    body = (
        f'<div class="tk-stops">{cards}</div>' if cards
        else f'<div class="tk-none">{_e("nothing " + _listed(found["stop"]["states"]))}</div>'
    )
    return (
        f'<div class="tk-shelf">{groups}<div class="tk-sub" data-phase="Stop"><div class="tk-label">'
        f'{_glyph("phase-Stop", found["stop"]["glyph"])}{_span(None, f"stopped ({len(stopped)})")}</div>'
        f"{body}</div></div>"
    )


def _board(snapshot: dict) -> str:
    found = board(snapshot)
    buttons = _button(f"all {found['total']}", "data-project", "", True) + "".join(
        _button(f"{_short(repo)} {n}", "data-project", repo, False) for repo, n in found["projects"]
    )
    views = _button("board", "data-view", "board", True) + _button("table", "data-view", "table", False)
    unavailable = '<p class="empty">statechart unavailable</p>'
    columns = "".join(_column(col) for col in found["columns"])
    return (
        '<div class="tk-filter" id="ticket-filter">'
        f'<div class="tk-group" role="group" aria-label="Project">{buttons}</div><span class="tk-grow"></span>'
        f'<div class="tk-group tk-view" role="group" aria-label="View">{views}</div></div>'
        + _panel(
            "pipeline", "Pipeline",
            f'<div class="tk-board">{columns}</div>' if found["available"] else unavailable,
            count=f"{found['tracked']} tracked"
            + (f" · {found['in_flight']} in flight" if found["available"] else ""),
        )
        + _panel(
            "shipped", "Shipped", _shelf(found) if found["available"] else unavailable,
            count=f"{sum(len(g['tiles']) for g in found['shelf'])} tickets" if found["available"] else None,
        )
    )


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
            + _td(_ticket(row, full=True))
            + _td(row["tracker"])
            + _td(row["state"], "bad" if row["stuck"] else None)
            + _td(row["round"], "num")
            + _td(row["gate_count"], "num")
            + _td("yes" if row["stuck"] else "no", "bad" if row["stuck"] else None)
            + "</tr>"
            for row in rows
        )
        inner = f'<div class="tk-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    return _panel("tickets", "Tickets", inner, count=f"{len(rows)} tracked", hidden=True)


def _active_jobs(snapshot: dict) -> list[tuple[dict, str]]:
    return [(row, "running") for row in snapshot["running"]] + [
        (row, "queued") for row in snapshot["queued"]
    ]
