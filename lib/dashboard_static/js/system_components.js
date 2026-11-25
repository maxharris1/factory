
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
  function renderPhoneDevices(root, m) {
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
    renderPhoneDevices(root, m);
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
