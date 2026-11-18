"""job.html (Tickets): the Tickets board and, as its table view, the tickets table."""

from __future__ import annotations

import re
from collections import Counter

from lib.dashboard_flow import PHASE_OVERRIDES, PHASES, PIPELINE, ticket_phase, ticket_phases
from lib.dispatch.constants import HANDED_OFF_EXTERNALLY
from lib.dashboard_pages.activity import _VERDICT
from lib.dashboard_pages.lanes import TILE_STATE
from lib.dashboard_pages.common import (
    _bot, checks_link, checks_named, _chip, duration, _e, github_links, _panel, _short, _span, _td, _ticket,
    ticket_page_href,
)


CSS = """\
.tk-filter{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-family:var(--mono)}
.tk-tools{display:flex;flex:1 1 auto;flex-wrap:wrap;align-items:center;gap:8px;min-width:0}
.tk-find{position:relative;display:flex;align-items:center;gap:8px;margin-left:8px;min-width:0}
.tk-find label{font-size:12px;color:var(--muted);white-space:nowrap}
.tk-find input{box-sizing:border-box;width:180px;height:44px;margin:0;padding:0 12px;font-family:inherit;font-size:12px;
background:var(--field);color:var(--text);border:1px solid var(--line-hi)}
.tk-find input:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.tk-open{display:inline-flex;align-items:center;box-sizing:border-box;height:44px;padding:0 14px;font-size:12px;
border:1px solid var(--line-hi);background:var(--panel)}
.tk-open[aria-disabled=true]{color:var(--muted)}
.tk-matches{position:absolute;top:100%;left:0;right:0;z-index:5;max-width:100%;margin:4px 0 0;padding:0;list-style:none;
background:var(--panel-hi);border:1px solid var(--line-hi);box-shadow:var(--elev)}
.tk-matches a{display:block;padding:12px;font-size:12px;overflow-wrap:anywhere;color:var(--text);text-decoration:none}
.tk-matches a:hover,.tk-matches [aria-selected=true] a{background:var(--sel);color:var(--ok)}
@media (max-width:640px){.tk-find{flex:1 1 100%;flex-wrap:wrap;align-items:flex-end;gap:4px 6px;margin-left:0}
.tk-find label{flex:1 1 100%;font-size:11px;line-height:1.3;letter-spacing:0.08em;text-transform:uppercase}
.tk-find input{flex:1 1 0;width:auto;min-width:0;padding:0 10px;font-size:16px}.tk-open{padding:0 10px}}
.tk-group{display:flex;flex-wrap:wrap;gap:8px}
.tk-view{flex-wrap:nowrap}
.tk-grow{flex-grow:1}
.tk-btn{height:44px;padding:0 14px;font-family:inherit;font-size:12px;cursor:pointer;
border:1px solid var(--line-hi);background:var(--panel);color:var(--text)}
.tk-btn[aria-pressed=true]{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 9%,transparent);color:var(--ok)}
.tk-btn:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.tk-board{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(200px,100%),1fr));gap:12px;align-items:start;
font-family:var(--mono)}
.tk-col{display:flex;flex-direction:column;gap:8px;min-width:0}
.tk-head{display:flex;flex-direction:column;gap:4px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.tk-headrow{display:flex;align-items:center;gap:8px}
.tk-glyph{display:block;width:16px}
.tk-name{font-size:13px;font-weight:600;color:var(--text)}
.tk-count{margin-left:auto;font-size:12px;color:var(--muted)}
.tk-count.tk-stopped{font-weight:600;color:var(--bad)}
.tk-states{display:block;min-height:34px;font-size:11px;line-height:17px;color:var(--muted)}
.tk-empty{padding:16px;border:1px dashed var(--line-hi);font-size:12px;color:var(--muted)}
.tk-card{display:flex;flex-direction:column;min-width:0;background:var(--panel-hi);box-shadow:var(--elev)}
.tk-card.tk-stop{border:1px solid color-mix(in srgb,var(--bad) 45%,transparent);box-shadow:none}
.tk-card.tk-stale{border:1px solid color-mix(in srgb,var(--warn) 45%,transparent);box-shadow:none}
.tk-more{min-width:0}
.tk-more>summary{padding:6px 10px;border:1px dashed var(--line-hi)}
.tk-more-list{display:flex;flex-direction:column;gap:8px;margin-top:8px}
.tk-strip{display:block}
.tk-body{display:flex;flex-direction:column;gap:6px;padding:8px 10px 10px}
.tk-id{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px}
.tk-id a{font-size:13px;font-weight:600;overflow-wrap:anywhere;color:var(--text);text-decoration:none}
.tk-id a:hover{color:var(--ok)}
.tk-tag{font-size:10px;line-height:1.5;padding:0 4px;border:1px solid var(--line-hi);color:var(--muted)}
.tk-state{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-2)}
.tk-pips{display:flex;gap:2px}
.tk-pips span{width:6px;height:6px;background:var(--line-hi)}
.tk-pips .done{background:var(--text)}
.tk-pips .now{background:var(--accent)}
.tk-round,.tk-q{font-size:11px;color:var(--muted)}
.tk-in{font-variant-numeric:tabular-nums}
.tk-reason{font-size:12px;color:var(--text-2);overflow-wrap:anywhere}
.tk-wait{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.tk-wait span{color:var(--text-2)}
.tk-hb{font-size:12px;color:var(--warn);font-variant-numeric:tabular-nums}
.tk-run,.tk-ci{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;font-size:12px}
.tk-dev{color:var(--muted)}
.tk-t{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.tk-out{color:var(--text-2)}
.tk-sub{margin:0 0 12px;font-family:var(--mono);font-size:12px;line-height:1.5;color:var(--muted)}
.tk-shelf{display:flex;flex-direction:column;gap:12px;font-family:var(--mono)}
.tk-tiles{display:flex;flex-wrap:wrap;gap:6px}
.tk-tile{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;background:var(--bg);box-shadow:var(--elev);text-decoration:none}
.tk-tile:hover .tk-tn{color:var(--ok)}
.tk-blk{width:8px;height:8px;background:var(--ok)}
.tk-tn{font-size:12px;font-weight:600;color:var(--text)}
.tk-r{font-size:11px;color:var(--muted)}
.tk-ago{font-size:11px;color:var(--text-2);font-variant-numeric:tabular-nums}
.tk-drawer{display:flex;flex-direction:column;gap:10px;margin-top:8px}
.tk-page{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.tk-note{font-size:12px;color:var(--muted)}
.tk-scroll{overflow-x:auto}
.tk-phases{display:none}
.tk-ph{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;min-width:0;height:48px;
padding:0;font-family:inherit;cursor:pointer;border:1px solid var(--line-hi);background:var(--panel-hi);
color:var(--text-2)}
.tk-ph[aria-pressed=true]{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 9%,transparent);color:var(--ok)}
.tk-ph:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.tk-phn{font-size:11px;line-height:1.3}
.tk-phc{font-size:13px;line-height:1.2;font-weight:600;font-variant-numeric:tabular-nums;color:var(--text)}
.tk-phc.tk-none{color:var(--muted)}
.tk-phc.tk-stopped{color:var(--bad)}
@media (min-width:700px){.tk-narrow{display:none}}
@media (max-width:699.98px){
.tk-live .tk-phases{display:grid;gap:4px;margin-bottom:12px;
grid-template-columns:minmax(0,4fr) minmax(0,4fr) minmax(0,5fr) minmax(0,6fr) minmax(0,4fr) minmax(0,7fr)}
.tk-board{grid-template-columns:minmax(0,1fr)}
.tk-live .tk-col:not(.tk-picked){display:none}
.tk-wide{display:none}
.tk-tiles{flex-direction:column;gap:6px}
.tk-tile{display:flex;box-sizing:border-box;min-height:44px;padding:0 10px;gap:8px}
.tk-tn{font-size:13px}
.tk-ago{margin-left:auto;font-size:12px}
.tk-shelf>.tk-tiles>.tk-tile:nth-child(n+6){display:none}
.tk-drawer{gap:6px}
.tk-earlier>summary{box-sizing:border-box;min-height:44px;padding:10px;border:1px dashed var(--line-hi)}
}
@media (scripting:none){#tickets[hidden]{display:block !important}#ticket-filter{display:none}}
@media (scripting:none){.tk-earlier .tk-btn{display:none}}
"""


SCRIPT_TICKETS = r"""

  var BOARD = __BOARD__;
  var WAITING = {'awaiting-ok': 1, 'waiting-max': 1};
  var WAIT_TINT = '#ff9e64';
  var BOARD_EMPTY = {Ask: 'nothing waiting on questions', Stop: 'nothing stopped'};
  var tkProject = tkLoad('tk-project') || '';
  var tkView = tkLoad('tk-view') === 'table' ? 'table' : 'board';
  var tkPhase = tkLoad('tk-phase');
  if (BOARD.columns.indexOf(tkPhase) < 0) tkPhase = null;
  var tkShelfShown = BOARD.shelf_page;
  var tkFind = {query: '', active: null, open: false, moving: false, rows: [], found: []};

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
      columns: order.length ? BOARD.columns.map(function (phase) {
        return {phase: phase, name: BOARD.names[phase] || phase, states: states(phase), glyph: BOARD.glyphs[phase]};
      }) : []
    };
  }

  function known(v) { return v !== null && v !== undefined; }

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
    var generated = d.generated_at_epoch;
    var cards = rows.filter(function (r) { return !project || r.repo === project; }).map(function (t) {
      var key = joinKey(t);
      var charted = Object.prototype.hasOwnProperty.call(table.states, t.state);
      var lv = live.get(key) || null, q = queued.get(key) || null;
      return {ticket: t, phase: t.stuck || !charted ? 'Stop' : table.states[t.state],
              live: lv, queued: q, kind: lv ? (lv.job_state === 'stale' ? 'stale' : 'live') : q ? 'queued' : 'idle',
              verdict: verdicts.get(key) || null, reason: reasons.get(key) || null,
              ago: known(t.shipped_at) && known(generated) ? generated - t.shipped_at : null};
    });
    function rank(c) {
      var k = BOARD.kinds[c.kind], age = c[k.source][k.age];
      var unknown = age === null || age === undefined;
      return [k.group, unknown ? 1 : 0, unknown ? 0 : -age];
    }
    function byRank(a, b) {
      var x = rank(a), y = rank(b);
      return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
    }
    var columns = table.columns.map(function (col) {
      var ranked = cards.filter(function (c) { return c.phase === col.phase; }).sort(byRank);
      return {phase: col.phase, name: col.name, glyph: col.glyph, states: col.states, cards: ranked,
              shown: ranked.slice(0, BOARD.cap), more: ranked.slice(BOARD.cap)};
    });
    function byShip(a, b) {
      var x = a.ticket.shipped_at, y = b.ticket.shipped_at;
      return !known(x) - !known(y) || (known(y) ? y : 0) - (known(x) ? x : 0) ||
        cmp(ticketKey(a.ticket), ticketKey(b.ticket));
    }
    var busy = columns.filter(function (col) {
      return col.cards.some(function (c) { return c.kind === 'live' || c.kind === 'stale'; });
    });
    var filled = columns.filter(function (col) { return col.cards.length; });
    var tiles = cards.filter(function (c) { return c.phase === 'Shipped'; }).sort(byShip);
    var handed = tiles.filter(function (c) { return c.ticket.shipped_how === 'handed-off'; }).length;
    return {
      projects: projects, total: rows.length, tracked: cards.length, available: table.columns.length > 0,
      open: columns.reduce(function (n, col) { return n + col.cards.length; }, 0),
      shipped: tiles.length,
      columns: columns,
      start: busy.concat(filled).map(function (col) { return col.phase; }).concat(BOARD.columns)[0],
      shelf: {merged: tiles.length - handed, handed: handed, tiles: tiles,
              newest: tiles.slice(0, BOARD.shelf_newest), earlier: tiles.slice(BOARD.shelf_newest)}
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

  function tkCi(parent, t, ci) {
    var line = add(parent, el('div', 'tk-ci'));
    add(line, chip('waiting on CI', 'warn'));
    add(line, el('span', 'tk-out', checksNamed(ci)));
    var href = checksHref(t);
    if (href) add(line, link(null, href)).textContent = 'checks';
    ticking(add(line, el('span', 'tk-t')), ci.waiting_seconds, 'waiting ');
  }

  function tkNoCi(parent, noCi) {
    var line = add(parent, el('div', 'tk-ci'));
    add(line, chip('no CI on this base', 'muted'));
    add(line, el('span', 'tk-out', noCi.base));
  }

  function tkInState(parent, t) {
    var span = add(parent, el('span', 'tk-in'));
    if (t.state_age_seconds === null || t.state_age_seconds === undefined) span.textContent = 'in ' + t.state;
    else ticking(span, t.state_age_seconds, 'in ' + t.state + ' ');
  }

  function tkCard(c) {
    var t = c.ticket, job = c.live || c.queued;
    var stop = c.phase === 'Stop', waiting = WAITING.hasOwnProperty(t.state);
    var ci = stop || job ? null : t.ci_wait;
    var card = el('div', stop ? 'tk-card tk-stop' : c.kind === 'stale' ? 'tk-card tk-stale' : 'tk-card');
    card.setAttribute('data-ticket', ticketKey(t));
    if (ci) card.setAttribute('data-wait', 'ci');
    var key = 'ticket-' + ticketKey(t);
    var strip = stop ? PX.strip(key, {state: 'stuck', height: 8})
      : waiting || ci ? PX.strip(key, {effect: 'static', tint: WAIT_TINT, height: 8})
      : c.kind === 'queued' ? PX.strip(key, {effect: 'conveyor', tint: 'bot', bot: job.recipient, still: true, height: 8})
      : c.live ? PX.jobStrip(c.live, 8) : null;
    if (strip) {
      var box = add(card, el('div', 'tk-strip'));
      box.setAttribute('aria-hidden', 'true');
      add(box, strip);
    }
    var main = add(card, el('div', 'tk-body'));
    var id = add(main, el('div', 'tk-id'));
    add(id, link(null, ticketPageHref(t))).textContent = ticketOf(t);
    add(id, el('span', 'tk-tag', t.tracker));
    addGh(id, t);
    var state = add(main, el('div', 'tk-state'));
    if (stop) {
      if (t.stuck) add(state, chip('stuck', 'bad'));
      tkInState(state, t);
      tkChips(state, c.live);
      if (c.reason) add(main, el('div', 'tk-reason', c.reason));
      return card;
    }
    if (c.kind === 'idle') tkInState(state, t);
    else add(state, el('span', null, t.state));
    if (c.kind === 'queued') add(state, el('span', 'tk-q', 'queued'));
    else tkChips(state, c.live);
    tkRound(state, t, job);
    var waitLine = waiting && t.waiting_on;
    if (waitLine) add(add(main, el('div', 'tk-wait', 'waits on ')), el('span', null, t.waiting_on));
    if (job) {
      var run = add(main, el('div', 'tk-run'));
      if (c.kind === 'queued') {
        add(run, el('span', 'tk-dev', 'for'));
        add(run, bot(job.recipient));
        ticking(add(run, el('span', 'tk-t')), job.age_seconds, 'waiting ');
        return card;
      }
      add(run, bot(job.recipient));
      if (job.device) add(run, el('span', 'tk-dev', '@ ' + job.device));
      ticking(add(run, el('span', 'tk-t')), job.elapsed_seconds, 'T+ ');
      if (c.kind === 'stale') ticking(add(main, el('div', 'tk-hb')), job.heartbeat_age_seconds, 'no heartbeat for ');
      return card;
    }
    if (ci) tkCi(main, t, ci);
    if (t.no_ci) tkNoCi(main, t.no_ci);
    if (waiting && GH.on && ghRefs(t)) add(main, ghPanel(t, t));
    if (c.verdict && !(waitLine && c.verdict.kind === 'waiting')) {
      var said = add(main, el('div', 'tk-run'));
      add(said, chip(c.verdict.kind, VERDICT[c.verdict.kind] || 'muted'));
      add(said, el('span', 'tk-out', c.verdict.outcome));
    }
    return card;
  }

  function tkPhases(root, columns) {
    var bar = tkGroup(root, 'tk-phases', 'Phase');
    columns.forEach(function (col) {
      var n = col.cards.length;
      var b = add(bar, el('button', 'tk-ph'));
      b.setAttribute('type', 'button');
      b.setAttribute('data-pick', col.phase);
      b.setAttribute('aria-pressed', col.phase === tkPhase ? 'true' : 'false');
      b.setAttribute('aria-label', col.name + ', ' + n + (n === 1 ? ' ticket' : ' tickets'));
      add(b, el('span', 'tk-phn', col.name));
      add(b, el('span', !n ? 'tk-phc tk-none' : col.phase === 'Stop' ? 'tk-phc tk-stopped' : 'tk-phc', n));
      b.addEventListener('click', function () {
        tkPhase = col.phase;
        tkSave('tk-phase', col.phase);
        if (latest) render(latest);
      });
    });
  }

  function tkColumn(col) {
    var box = el('div', col.phase === tkPhase ? 'tk-col tk-picked' : 'tk-col');
    box.setAttribute('data-phase', col.phase);
    var head = add(box, el('div', 'tk-head'));
    var row = add(head, el('div', 'tk-headrow'));
    add(row, tkGlyph('phase-' + col.phase, col.glyph));
    add(row, el('span', 'tk-name', col.name));
    var count = col.phase === 'Stop' && col.cards.length ? 'tk-count tk-stopped' : 'tk-count';
    add(row, el('span', count, col.cards.length));
    add(head, el('span', 'tk-states', col.states.join(' · ')));
    var empty = BOARD_EMPTY[col.phase] || 'nothing in ' + col.phase.toLowerCase();
    if (!col.cards.length) add(box, el('div', 'tk-empty', empty));
    col.shown.forEach(function (c) { add(box, tkCard(c)); });
    if (col.more.length) {
      var list = add(details(box, 'tk-more-' + col.phase, '+' + col.more.length + ' more', 'tk-more'),
                     el('div', 'tk-more-list'));
      col.more.forEach(function (c) { add(list, tkCard(c)); });
    }
    return box;
  }

  function renderBoard(d) {
    if (!GH.started) { GH.started = true; ghStart(); }
    if (tkProject && !(d.tickets || []).some(function (r) { return r.repo === tkProject; })) {
      tkProject = '';
      tkSave('tk-project', '');
    }
    var b = boardOf(d, tkProject);
    if (!tkPhase && b.available) tkPhase = b.start;

    var filter = byId('ticket-filter');
    if (filter) {
      var tools = el('div', 'tk-tools');
      tkFind.moving = true;
      filter.replaceChildren(tools, tkFind.box || tkFindBox());
      tkFind.moving = false;
      var group = tkGroup(tools, 'tk-group', 'Project');
      tkButton(group, 'all ' + b.total, 'data-project', '', !tkProject, function () { tkProject = ''; tkSave('tk-project', ''); });
      b.projects.forEach(function (p) {
        tkButton(group, p.short + ' ' + p.count, 'data-project', p.repo, tkProject === p.repo,
                 function () { tkProject = p.repo; tkSave('tk-project', p.repo); });
      });
      add(tools, el('span', 'tk-grow'));
      var view = tkGroup(tools, 'tk-group tk-view', 'View');
      ['board', 'table'].forEach(function (name) {
        tkButton(view, name, 'data-view', name, tkView === name, function () { tkView = name; tkSave('tk-view', name); });
      });
      tkFind.rows = d.tickets || [];
      tkFindDraw();
    }

    var pipeline = panel('pipeline', 'Pipeline',
                         b.tracked + ' tracked' + (b.available ? ' · ' + b.open + ' open · ' + b.shipped + ' shipped' : ''));
    if (pipeline) {
      pipeline.hidden = tkView !== 'board';
      pipeline.classList.toggle('tk-live', b.available);
      if (!b.available) add(pipeline, el('p', 'empty', 'statechart unavailable'));
      else {
        tkPhases(pipeline, b.columns);
        var grid = add(pipeline, el('div', 'tk-board'));
        b.columns.forEach(function (col) { add(grid, tkColumn(col)); });
      }
    }

    var s = b.shelf;
    var shipped = panel('shipped', 'Shipped', b.available ? s.tiles.length : null);
    if (!shipped) return;
    shipped.hidden = tkView !== 'board';
    if (!b.available) { add(shipped, el('p', 'empty', 'statechart unavailable')); return; }
    if (!s.tiles.length) { add(shipped, el('p', 'empty', 'nothing shipped')); return; }
    add(shipped, el('p', 'tk-sub', s.merged + ' merged · ' + s.handed + ' handed off'));
    var shelf = add(shipped, el('div', 'tk-shelf'));
    tkTiles(shelf, s.newest);
    var again = s.newest.slice(BOARD.shelf_phone);
    if (!again.length) return;
    var box = details(shelf, 'tk-earlier', null, s.earlier.length ? 'tk-earlier' : 'tk-earlier tk-narrow');
    add(box.children[0], el('span', 'tk-wide', s.earlier.length + ' earlier'));
    add(box.children[0], el('span', 'tk-narrow', s.earlier.length + again.length + ' earlier'));
    var drawer = add(box, el('div', 'tk-drawer'));
    tkTiles(add(drawer, el('div', 'tk-narrow')), again);
    if (!s.earlier.length) return;
    tkTiles(drawer, s.earlier.slice(0, tkShelfShown));
    var left = s.earlier.length - tkShelfShown;
    if (left <= 0) return;
    var page = add(drawer, el('div', 'tk-page'));
    var more = add(page, el('button', 'tk-btn', 'show ' + BOARD.shelf_page + ' more'));
    more.setAttribute('type', 'button');
    more.addEventListener('click', function () { tkShelfShown += BOARD.shelf_page; if (latest) render(latest); });
    add(page, el('span', 'tk-note', left + ' older not shown · newest first'));
  }

  function tkTier(names, q) {
    if (!q) return -1;
    names = names.map(function (n) { return String(n).toLowerCase(); });
    return names.indexOf(q) >= 0 ? 0
      : names.some(function (n) { return n.indexOf(q) === 0; }) ? 1
      : names.some(function (n) { return n.indexOf(q) > 0; }) ? 2 : -1;
  }

  function tkMatches(rows, query) {
    var q = query.trim().toLowerCase(), qid = q.replace(/^#/, '');
    if (!q) return [];
    var found = [];
    rows.forEach(function (r, i) {
      var ids = [r.ticket_id];
      if (r.source) ids.push(String(r.source).replace(/^#/, ''));
      if (r.pr) ids.push(r.pr);
      var id = tkTier(ids, qid);
      var label = tkTier([tileLabel(r), short(r.repo) + '#' + r.ticket_id, ticketOf(r), ticketKey(r)], q);
      var rank = id >= 0 ? id : label >= 0 ? 3 + label : -1;
      if (rank >= 0) found.push({rank: rank, at: i, key: ticketKey(r), label: ticketOf(r), href: ticketPageHref(r)});
    });
    found.sort(function (a, b) { return a.rank - b.rank || a.at - b.at; });
    return found.slice(0, BOARD.find_max);
  }

  function tkFindAt() {
    for (var i = 0; i < tkFind.found.length; i++) if (tkFind.found[i].key === tkFind.active) return i;
    return -1;
  }

  function tkFindDraw() {
    var f = tkFind;
    f.found = tkMatches(f.rows, f.query);
    var at = tkFindAt();
    if (at < 0) f.active = null;
    f.list.replaceChildren();
    f.found.forEach(function (m, n) {
      var li = add(f.list, el('li'));
      li.setAttribute('role', 'option');
      li.setAttribute('id', 'find-ticket-' + n);
      li.setAttribute('aria-selected', n === at ? 'true' : 'false');
      var a = add(li, link(null, m.href));
      a.setAttribute('tabindex', '-1');
      a.textContent = m.label;
    });
    var shown = f.open && f.found.length > 0;
    f.list.hidden = !shown;
    f.input.setAttribute('aria-expanded', shown ? 'true' : 'false');
    if (shown && at >= 0) f.input.setAttribute('aria-activedescendant', 'find-ticket-' + at);
    else f.input.removeAttribute('aria-activedescendant');
    var target = f.found[Math.max(at, 0)];
    if (target) {
      f.link.removeAttribute('aria-disabled');
      f.link.setAttribute('href', target.href);
    } else {
      f.link.removeAttribute('href');
      f.link.setAttribute('aria-disabled', 'true');
    }
  }

  function tkFindKey(ev) {
    var f = tkFind, n = f.found.length, at = tkFindAt();
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      if (!n) return;
      f.open = true;
      f.active = f.found[ev.key === 'ArrowDown' ? (at + 1) % n : at <= 0 ? n - 1 : at - 1].key;
      tkFindDraw();
    } else if (ev.key === 'Enter' && !ev.isComposing) {
      var target = f.found[Math.max(at, 0)];
      if (!target) return;
      ev.preventDefault();
      location.href = target.href;
    } else if (ev.key === 'Escape' && f.open) {
      ev.preventDefault();
      f.open = false;
      f.active = null;
      tkFindDraw();
    }
  }

  function tkFindBox() {
    var before = byId('find-ticket');
    var box = el('div', 'tk-find');
    box.setAttribute('role', 'search');
    add(box, el('label', null, 'Find a ticket')).setAttribute('for', 'find-ticket');
    var focused = before === document.activeElement;
    var input = add(box, before || el('input'));
    [['id', 'find-ticket'], ['type', 'text'], ['name', 'ticket'], ['placeholder', 'factory#470'],
     ['autocomplete', 'off'], ['spellcheck', 'false'], ['role', 'combobox'], ['aria-autocomplete', 'list'],
     ['aria-controls', 'find-ticket-list'], ['aria-expanded', 'false']].forEach(function (a) {
      input.setAttribute(a[0], a[1]);
    });
    var open = add(box, el('a', 'tk-open', 'open'));
    open.setAttribute('aria-disabled', 'true');
    var list = add(box, el('ul', 'tk-matches'));
    list.setAttribute('id', 'find-ticket-list');
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', 'Matching tickets');
    list.hidden = true;
    input.addEventListener('input', function () {
      tkFind.query = input.value;
      tkFind.open = true;
      tkFind.active = null;
      tkFindDraw();
    });
    input.addEventListener('keydown', tkFindKey);
    box.addEventListener('focusout', function (ev) {
      if (tkFind.moving || !tkFind.open) return;
      for (var n = ev.relatedTarget; n; n = n.parentNode) if (n === box) return;
      tkFind.open = false;
      tkFind.active = null;
      tkFindDraw();
    });
    if (input.value) {
      tkFind.query = input.value;
      tkFind.open = focused;
    }
    tkFind.box = box;
    tkFind.input = input;
    tkFind.link = open;
    tkFind.list = list;
    return box;
  }

  function tkTiles(parent, cards) {
    var row = add(parent, el('div', 'tk-tiles'));
    cards.forEach(function (c) {
      var t = c.ticket, name = ticketOf(t), external = t.state === 'handed-off-externally';
      var how = external ? 'handed off in Linear' : t.shipped_how === 'handed-off' ? 'handed off' : 'merged';
      var where = external && t.linear_state ? ' · ' + t.linear_state : '';
      var rounds = t.round === 1 ? '1 revise round' : t.round + ' revise rounds';
      var tile = add(row, link('tk-tile', ticketPageHref(t)));
      tile.setAttribute('data-ticket', ticketKey(t));
      if (known(c.ago)) ticking(tile, c.ago, name + ' · ' + how + ' ', ' ago' + where + ' · ' + rounds, 'title');
      else tile.setAttribute('title', name + where + ' · ' + rounds);
      add(tile, el('span', 'tk-blk')).setAttribute('aria-hidden', 'true');
      add(tile, el('span', 'tk-tn', name));
      add(tile, el('span', 'tk-r', 'r' + t.round));
      if (known(c.ago)) ticking(add(tile, el('span', 'tk-ago')), c.ago, how === 'merged' ? '' : how + ' ', ' ago');
    });
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
CARD_KINDS = {
    "live": {"group": 0, "source": "live", "age": "elapsed_seconds"},
    "stale": {"group": 0, "source": "live", "age": "elapsed_seconds"},
    "queued": {"group": 1, "source": "queued", "age": "age_seconds"},
    "idle": {"group": 2, "source": "ticket", "age": "state_age_seconds"},
}
CAP = 6
SHELF_NEWEST = 10
SHELF_PHONE = 5
SHELF_PAGE = 50
FIND_MAX = 8
FIND = (
    '<div class="tk-find" role="search"><label for="find-ticket">Find a ticket</label>'
    '<input id="find-ticket" type="text" name="ticket" placeholder="factory#470" autocomplete="off" spellcheck="false" '
    'role="combobox" aria-autocomplete="list" aria-controls="find-ticket-list" aria-expanded="false">'
    '<a class="tk-open" aria-disabled="true">open</a>'
    '<ul class="tk-matches" id="find-ticket-list" role="listbox" aria-label="Matching tickets" hidden></ul></div>'
)
JOB_CHIPS = {
    **{name: TILE_STATE[name]["chip"] for name in ("claimed", "stale", "dead")},
    "overdue": ["overdue", "bad"],
}
_WAIT_TINT = "#ff9e64"
_EMPTY = {"Ask": "nothing waiting on questions", "Stop": "nothing stopped"}
COLUMNS = (*PIPELINE, "Stop")
COLUMN_NAMES = {"Stop": "Stopped"}
_ROUND = re.compile(r"([0-9]+)/([0-9]+)")


def _phase_glyph(phase: str) -> dict:
    return {"tint": GLYPH_TINT, **PHASE_GLYPH[phase], "cell": 2, "height": 16}


def board_constants() -> dict:
    return {
        "columns": COLUMNS, "names": COLUMN_NAMES, "phases": PHASES, "overrides": PHASE_OVERRIDES,
        "chips": JOB_CHIPS, "kinds": CARD_KINDS, "cap": CAP, "shelf_newest": SHELF_NEWEST, "shelf_page": SHELF_PAGE,
        "shelf_phone": SHELF_PHONE, "find_max": FIND_MAX,
        "glyphs": {phase: _phase_glyph(phase) for phase in PHASE_GLYPH},
    }


def phase_table(chart: dict | None) -> dict:
    phases = ticket_phases(chart)

    def states(phase: str) -> list[str]:
        return [name for name, found in phases.items() if found == phase]

    return {
        "states": phases,
        "columns": [
            {
                "phase": phase, "name": COLUMN_NAMES.get(phase, phase), "states": states(phase),
                "glyph": _phase_glyph(phase),
            }
            for phase in COLUMNS
        ] if phases else [],
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


def _kind(live: dict | None, queued: dict | None) -> str:
    if live:
        return "stale" if live.get("job_state") == "stale" else "live"
    return "queued" if queued else "idle"


def _rank(card: dict) -> tuple:
    kind = CARD_KINDS[card["kind"]]
    age = card[kind["source"]].get(kind["age"])
    return kind["group"], age is None, -(age or 0)


def _ship_rank(card: dict) -> tuple:
    at = card["ticket"].get("shipped_at")
    return at is None, -(at or 0), _key(card["ticket"])


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
    generated = snapshot.get("generated_at_epoch")
    cards = [
        {
            "ticket": row,
            "phase": ticket_phase(table["states"], row["state"], row["stuck"]),
            "live": live.get(_join(row)),
            "queued": queued.get(_join(row)),
            "kind": _kind(live.get(_join(row)), queued.get(_join(row))),
            "verdict": verdicts.get(_join(row)),
            "reason": reasons.get(_join(row)),
            "ago": None if row.get("shipped_at") is None or generated is None else generated - row["shipped_at"],
        }
        for row in rows
        if project in (None, row["repo"])
    ]
    columns = []
    for col in table["columns"]:
        ranked = sorted((c for c in cards if c["phase"] == col["phase"]), key=_rank)
        columns.append({**col, "cards": ranked, "shown": ranked[:CAP], "more": ranked[CAP:]})
    busy = [col["phase"] for col in columns if any(c["kind"] in ("live", "stale") for c in col["cards"])]
    filled = [col["phase"] for col in columns if col["cards"]]
    tiles = sorted((c for c in cards if c["phase"] == "Shipped"), key=_ship_rank)
    handed = sum(c["ticket"].get("shipped_how") == "handed-off" for c in tiles)
    return {
        "projects": sorted(counts.items(), key=lambda item: (-item[1], _short(item[0]), item[0])),
        "total": len(rows),
        "tracked": len(cards),
        "available": bool(table["columns"]),
        "open": sum(len(col["cards"]) for col in columns),
        "shipped": len(tiles),
        "columns": columns,
        "start": (busy + filled + [COLUMNS[0]])[0],
        "shelf": {
            "merged": len(tiles) - handed, "handed": handed, "tiles": tiles,
            "newest": tiles[:SHELF_NEWEST], "earlier": tiles[SHELF_NEWEST:],
        },
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


def _in_state(ticket: dict) -> str:
    age = ticket.get("state_age_seconds")
    return _span("tk-in", f"in {ticket['state']}" + ("" if age is None else f" {duration(age)}"))


def _card(card: dict) -> str:
    ticket, live, kind = card["ticket"], card["live"], card["kind"]
    job = live or card["queued"]
    stop = card["phase"] == "Stop"
    waiting = ticket["state"] in WAITING
    ci = None if stop or job else ticket.get("ci_wait")
    key = f"ticket-{_key(ticket)}"
    px = _px()
    if stop:
        strip = px.px_strip(key, state="stuck", height=8)
    elif waiting or ci:
        strip = px.px_strip(key, effect="static", tint=_WAIT_TINT, height=8)
    elif kind == "queued":
        strip = px.px_strip(key, effect="conveyor", tint="bot", bot=job["recipient"], still=True, height=8)
    else:
        strip = px.px_job_strip(live, height=8) if live else ""
    ident = f'<a href="{_e(ticket_page_href(ticket))}">{_e(_ticket(ticket))}</a>'
    lines = ""
    if stop:
        state = (_chip("stuck", "bad") if ticket["stuck"] else "") + _in_state(ticket) + _chips(live)
        lines = f'<div class="tk-reason">{_e(card["reason"])}</div>' if card["reason"] else ""
    else:
        state = (
            _in_state(ticket) if kind == "idle"
            else _span(None, ticket["state"]) + (_span("tk-q", "queued") if kind == "queued" else _chips(live))
        ) + _round(ticket, job)
        wait_line = waiting and ticket.get("waiting_on")
        if wait_line:
            lines += f'<div class="tk-wait">waits on {_span(None, ticket["waiting_on"])}</div>'
        if kind == "queued":
            lines += (
                f'<div class="tk-run">{_span("tk-dev", "for")}{_bot(job["recipient"])}'
                f'{_span("tk-t", "waiting " + duration(job.get("age_seconds")))}</div>'
            )
        elif live:
            where = _span("tk-dev", f"@ {live['device']}") if live.get("device") else ""
            since = _span("tk-t", "T+ " + duration(live["elapsed_seconds"]))
            lines += f'<div class="tk-run">{_bot(live["recipient"])}{where}{since}</div>'
            if kind == "stale":
                hb = duration(live.get("heartbeat_age_seconds"))
                lines += f'<div class="tk-hb">{_e("no heartbeat for " + hb)}</div>'
        else:
            if ci:
                lines += (
                    f'<div class="tk-ci">{_chip("waiting on CI", "warn")}{_span("tk-out", checks_named(ci))}'
                    f'{checks_link(ticket)}{_span("tk-t", "waiting " + duration(ci["waiting_seconds"]))}</div>'
                )
            if ticket.get("no_ci"):
                lines += (
                    f'<div class="tk-ci">{_chip("no CI on this base", "muted")}'
                    f'{_span("tk-out", ticket["no_ci"]["base"])}</div>'
                )
            verdict = card["verdict"]
            if verdict and not (wait_line and verdict["kind"] == "waiting"):
                lines += (
                    f'<div class="tk-run">{_chip(verdict["kind"], _VERDICT.get(verdict["kind"], "muted"))}'
                    f'{_span("tk-out", verdict["outcome"])}</div>'
                )
    cls = "tk-card tk-stop" if stop else "tk-card tk-stale" if kind == "stale" else "tk-card"
    return (
        f'<div class="{cls}" data-ticket="{_e(_key(ticket))}"'
        + (' data-wait="ci">' if ci else ">")
        + (f'<div class="tk-strip" aria-hidden="true">{strip}</div>' if strip else "")
        + f'<div class="tk-body"><div class="tk-id">{ident}{_span("tk-tag", ticket["tracker"])}{github_links(ticket)}</div>'
        f'<div class="tk-state">{state}</div>{lines}</div></div>'
    )


def _phases(columns: list[dict], picked: str) -> str:
    buttons = ""
    for col in columns:
        n = len(col["cards"])
        count = "tk-phc tk-none" if not n else "tk-phc tk-stopped" if col["phase"] == "Stop" else "tk-phc"
        label = f"{col['name']}, {n} ticket" + ("" if n == 1 else "s")
        buttons += (
            f'<button class="tk-ph" type="button" data-pick="{_e(col["phase"])}" '
            f'aria-pressed="{"true" if col["phase"] == picked else "false"}" aria-label="{_e(label)}">'
            f'{_span("tk-phn", col["name"])}{_span(count, n)}</button>'
        )
    return f'<div class="tk-phases" role="group" aria-label="Phase">{buttons}</div>'


def _column(col: dict, picked: bool) -> str:
    phase = col["phase"]
    cards = "".join(_card(card) for card in col["shown"]) or (
        f'<div class="tk-empty">{_e(_EMPTY.get(phase, f"nothing in {phase.lower()}"))}</div>'
    )
    if col["more"]:
        cards += (
            f'<details class="tk-more"><summary>+{len(col["more"])} more</summary>'
            f'<div class="tk-more-list">{"".join(_card(card) for card in col["more"])}</div></details>'
        )
    count = "tk-count tk-stopped" if phase == "Stop" and col["cards"] else "tk-count"
    return (
        f'<div class="{"tk-col tk-picked" if picked else "tk-col"}" data-phase="{_e(phase)}"><div class="tk-head">'
        '<div class="tk-headrow">'
        f'{_glyph(f"phase-{phase}", col["glyph"])}{_span("tk-name", col["name"])}{_span(count, len(col["cards"]))}'
        f'</div>{_span("tk-states", " · ".join(col["states"]))}</div>{cards}</div>'
    )


def _button(text: str, attr: str, value: str, on: bool) -> str:
    return (
        f'<button class="tk-btn" type="button" aria-pressed="{"true" if on else "false"}" '
        f'{attr}="{_e(value)}">{_e(text)}</button>'
    )


def _tiles(cards: list[dict]) -> str:
    tiles = ""
    for card in cards:
        ticket, ago = card["ticket"], card["ago"]
        name, external = _ticket(ticket), ticket["state"] == HANDED_OFF_EXTERNALLY
        how = "handed off in Linear" if external else (
            "handed off" if ticket.get("shipped_how") == "handed-off" else "merged"
        )
        where = f" · {ticket['linear_state']}" if external and ticket.get("linear_state") else ""
        rnd = ticket["round"]
        rounds = "1 revise round" if rnd == 1 else f"{rnd} revise rounds"
        when = "" if ago is None else f" · {how} {duration(ago)} ago"
        age = "" if ago is None else _span("tk-ago", ("" if how == "merged" else f"{how} ") + f"{duration(ago)} ago")
        tiles += (
            f'<a class="tk-tile" href="{_e(ticket_page_href(ticket))}" data-ticket="{_e(_key(ticket))}" '
            f'title="{_e(f"{name}{when}{where} · {rounds}")}"><span class="tk-blk" aria-hidden="true"></span>'
            f'{_span("tk-tn", name)}{_span("tk-r", f"r{rnd}")}{age}</a>'
        )
    return f'<div class="tk-tiles">{tiles}</div>'


def _shelf(shelf: dict) -> str:
    if not shelf["tiles"]:
        return '<p class="empty">nothing shipped</p>'
    earlier, again, drawer = shelf["earlier"], shelf["newest"][SHELF_PHONE:], ""
    if again:
        left = len(earlier) - SHELF_PAGE
        page = (
            f'<div class="tk-page"><button class="tk-btn" type="button">show {SHELF_PAGE} more</button>'
            f'{_span("tk-note", f"{left} older not shown · newest first")}</div>'
        ) if left > 0 else ""
        drawer = (
            f'<details class="{"tk-earlier" if earlier else "tk-earlier tk-narrow"}"><summary>'
            f'{_span("tk-wide", f"{len(earlier)} earlier")}{_span("tk-narrow", f"{len(earlier) + len(again)} earlier")}'
            f'</summary><div class="tk-drawer"><div class="tk-narrow">{_tiles(again)}</div>'
            f'{_tiles(earlier[:SHELF_PAGE]) if earlier else ""}{page}</div></details>'
        )
    sub = _e(f"{shelf['merged']} merged · {shelf['handed']} handed off")
    return f'<p class="tk-sub">{sub}</p><div class="tk-shelf">{_tiles(shelf["newest"])}{drawer}</div>'


def _board(snapshot: dict) -> str:
    found = board(snapshot)
    buttons = _button(f"all {found['total']}", "data-project", "", True) + "".join(
        _button(f"{_short(repo)} {n}", "data-project", repo, False) for repo, n in found["projects"]
    )
    views = _button("board", "data-view", "board", True) + _button("table", "data-view", "table", False)
    unavailable = '<p class="empty">statechart unavailable</p>'
    columns = "".join(_column(col, col["phase"] == found["start"]) for col in found["columns"])
    return (
        '<div class="tk-filter" id="ticket-filter"><div class="tk-tools">'
        f'<div class="tk-group" role="group" aria-label="Project">{buttons}</div><span class="tk-grow"></span>'
        f'<div class="tk-group tk-view" role="group" aria-label="View">{views}</div></div>{FIND}</div>'
        + _panel(
            "pipeline", "Pipeline",
            _phases(found["columns"], found["start"]) + f'<div class="tk-board">{columns}</div>'
            if found["available"] else unavailable,
            count=f"{found['tracked']} tracked"
            + (f" · {found['open']} open · {found['shipped']} shipped" if found["available"] else ""),
        )
        + _panel(
            "shipped", "Shipped", _shelf(found["shelf"]) if found["available"] else unavailable,
            count=len(found["shelf"]["tiles"]) if found["available"] else None,
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
