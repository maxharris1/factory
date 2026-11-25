

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
      chart.edges.forEach(function (e) { if (BOARD.not_factory_steps.indexOf(e.label) < 0) next[e.from].push(e.to); });
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
    var qa = new Map();
    ((d.qa || {}).runs || []).forEach(function (r) {
      if (!qa.has(joinKey(r))) qa.set(joinKey(r), []);
      qa.get(joinKey(r)).push(r);
    });
    var generated = d.generated_at_epoch;
    var mine = rows.filter(function (r) { return !project || r.repo === project; });
    var cards = mine.filter(function (t) {
      return !(t.state === 'abandoned' && known(t.state_age_seconds) && t.state_age_seconds >= BOARD.stopped_window);
    }).map(function (t) {
      var key = joinKey(t);
      var charted = Object.prototype.hasOwnProperty.call(table.states, t.state);
      var lv = live.get(key) || null, q = queued.get(key) || null;
      return {ticket: t, phase: t.stuck || !charted ? 'Stop' : table.states[t.state],
              live: lv, queued: q, kind: lv ? (lv.job_state === 'stale' ? 'stale' : 'live') : q ? 'queued' : 'idle',
              verdict: verdicts.get(key) || null, reason: reasons.get(key) || null, qa: qa.get(key) || [],
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
      projects: projects, total: rows.length, tracked: mine.length, available: table.columns.length > 0,
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
    var chips = BOARD.chips[live.stale_cause || live.job_state];
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
    if (!noCi.gate_head) return;
    add(line, chip('local gate passed', 'ok'));
    add(line, el('span', 'tk-dev', noCi.gate_head));
  }

  function tkInState(parent, t) {
    var span = add(parent, el('span', 'tk-in'));
    if (t.state_age_seconds === null || t.state_age_seconds === undefined) span.textContent = 'in ' + t.state;
    else ticking(span, t.state_age_seconds, 'in ' + t.state + ' ');
  }

  function qaHref(halves) {
    var run = Array.isArray(halves) && halves.length === 2 ? halves.join('') : '';
    return /^[0-9a-f]{32}$/.test(run) ? '/qa/runs/' + run : null;
  }

  function tkQa(card, main, runs) {
    runs.forEach(function (r) {
      var facts = [r.state, r.scope];
      if (r.lanes) facts.push(r.lanes + (r.lanes === 1 ? ' lane' : ' lanes'));
      if (known(r.passed)) facts.push(r.passed + '/' + r.total + ' scenarios');
      var line = add(main, el('div', 'tk-run'));
      add(line, chip(r.result ? 'QA ' + r.result : 'QA', BOARD.qa_tones[r.result || r.state] || 'muted'));
      add(line, el('span', 'tk-dev', r.runner));
      if (r.arm === 'shootout') add(line, el('span', 'tk-tag', 'shootout'));
      add(line, el('span', 'tk-out', facts.join(' · ')));
      var href = qaHref(r.run);
      if (href) add(line, link(null, href)).textContent = 'evidence';
      var t = add(line, el('span', 'tk-t'));
      if (r.state === 'reported') t.textContent = dur(r.elapsed_seconds);
      else ticking(t, r.elapsed_seconds);
    });
    return card;
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
      return tkQa(card, main, c.qa);
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
        return tkQa(card, main, c.qa);
      }
      add(run, bot(job.recipient));
      if (job.device) add(run, el('span', 'tk-dev', '@ ' + job.device));
      ticking(add(run, el('span', 'tk-t')), job.elapsed_seconds, 'T+ ');
      if (c.kind === 'stale') ticking(add(main, el('div', 'tk-hb')), job.heartbeat_age_seconds, 'no heartbeat for ');
      return tkQa(card, main, c.qa);
    }
    if (ci) tkCi(main, t, ci);
    if (t.no_ci) tkNoCi(main, t.no_ci);
    if (waiting && GH.on && ghRefs(t)) add(main, ghPanel(t, t));
    if (c.verdict && !(waitLine && c.verdict.kind === 'waiting')) {
      var said = add(main, el('div', 'tk-run'));
      add(said, chip(c.verdict.kind, VERDICT[c.verdict.kind] || 'muted'));
      add(said, el('span', 'tk-out', c.verdict.outcome));
    }
    return tkQa(card, main, c.qa);
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
