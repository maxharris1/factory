
  // --- main page (#140): lanes and slot tiles, Just happened, Intake -------

  var LANES = __LANES__;
  var LANES_MQ = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function lanesStill() { return !!(LANES_MQ && LANES_MQ.matches); }

  function queueSpeed(age) {
    var pts = LANES.speed;
    var a = Number(age) || 0;
    if (a <= pts[0][0]) return pts[0][1];
    for (var i = 1; i < pts.length; i++) {
      if (a <= pts[i][0]) return Math.round((pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * (a - pts[i - 1][0]) / (pts[i][0] - pts[i - 1][0])) * 1000) / 1000;
    }
    var p0 = pts[pts.length - 2], p1 = pts[pts.length - 1];
    return Math.round(Math.min(6, p1[1] + (p1[1] - p0[1]) * (a - p1[0]) / (p1[0] - p0[0])) * 1000) / 1000;
  }

  function probeDown(d, name) {
    return agedHealth(d).rows.some(function (c) {
      return c.component === 'device-probe' && c.device === name && c.state === 'down';
    });
  }

  function deviceChip(dev, down) {
    if (!dev.enabled) return chip('DISABLED', 'muted');
    if (dev.placeholder) return chip('PLACEHOLDER', 'warn');
    if (!dev.listed) return chip('UNLISTED', 'warn');
    if (down) return chip('PROBE DOWN', 'bad');
    return null;
  }

  function verbAt(name, secs, seed) {
    var verbs = LANES.tiles[name].verbs;
    return verbs[Math.floor((secs + seed * 1.7) / 4) % verbs.length] + '…';
  }

  function letters(node, text) {
    node.replaceChildren();
    node.setAttribute('aria-label', text);
    for (var i = 0; i < text.length; i++) add(node, el('span', null, text.charAt(i)));
  }

  var tileAnims = [];
  var tileTick = 0;
  function tileFrame(a) {
    var secs = Math.floor(tileTick / 8);
    var lit = LANES.glyph[Math.floor(tileTick / 2) % 4];
    a.cells.forEach(function (c, q) { c.className = q === lit ? 'on' : ''; });
    var text = verbAt(a.state, secs, a.seed);
    if (text !== a.verb.getAttribute('aria-label')) letters(a.verb, text);
    var pos = (tileTick % (text.length + 10)) - 2;
    Array.prototype.forEach.call(a.verb.children, function (s, i) {
      var dist = Math.abs(i - pos);
      s.className = dist === 0 ? 'h' : dist === 1 ? 'n' : '';
    });
  }
  setInterval(function () {
    if (lanesStill() || !tileAnims.length) return;
    tileTick += 1;
    tileAnims.forEach(function (a) { if (a.verb.isConnected !== false) tileFrame(a); });
  }, 125);

  function slotTile(r, seed) {
    var name = PX.state(r);
    var spec = LANES.tiles[name] || LANES.tiles.unknown;
    var who = BOTS[r.recipient] ? r.recipient : 'other';
    var tile = link('tile ' + who + (spec.bad ? ' bad' : ''), ticketPageHref(r));
    var model = modelEffort(r.exposure);
    tile.title = ticketIn(r.repo, r) + ' · ' + r.action + (r.round ? ' · round ' + r.round : '') +
      (model ? ' · ' + model : '') + ' (job ' + r.job + ')';
    add(tile, PX.jobStrip(r, 16));
    var bodyEl = add(tile, el('div', 'tile-body'));
    var l1 = add(bodyEl, el('div', 'tile-l1'));
    add(l1, bot(r.recipient));
    add(l1, el('span', 'ticket', tileLabel(r)));
    var badge = JOB_STATE[r.stale_cause] || (name === 'overdue' ? (LANES.tiles[r.job_state] || {}).chip : spec.chip);
    if (badge) add(l1, chip(badge[0], badge[1]));
    var l2 = add(bodyEl, el('div', 'tile-l2'));
    var verb;
    if (spec.verbs) {
      var glyph = add(l2, el('span', 'glyph'));
      glyph.setAttribute('aria-hidden', 'true');
      var cells = [];
      for (var q = 0; q < 4; q++) cells.push(add(glyph, el('i', q === LANES.glyph[0] ? 'on' : null)));
      verb = add(l2, el('span', 'verb ' + spec.tone));
      verb.setAttribute('role', 'img');
      letters(verb, verbAt(name, 0, seed));
      tileAnims.push({state: name, seed: seed, cells: cells, verb: verb});
    } else {
      verb = add(l2, el('span', 'verb' + (spec.tone ? ' ' + spec.tone : '')));
      verb.setAttribute('role', 'img');
      letters(verb, spec.text === null ? r.action : spec.text);
    }
    var l3 = add(bodyEl, el('div', 'tile-l3'));
    if (spec !== LANES.tiles.unknown) add(l3, el('span', 'action', r.action));
    var m = /^(\d+)\s*\/\s*(\d+)$/.exec(String(r.round === null || r.round === undefined ? '' : r.round));
    if (m) {
      var rnd = add(l3, el('span', 'round'));
      rnd.title = 'round ' + r.round;
      var pips = add(rnd, el('span', 'pips'));
      pips.setAttribute('aria-hidden', 'true');
      var cur = Number(m[1]), cap = Math.min(6, Number(m[2]));
      for (var k = 1; k <= cap; k++) add(pips, el('i', k < cur ? 'done' : k === cur ? 'cur' : null));
      add(rnd, el('span', null, String(r.round)));
    }
    if (model) add(l3, el('span', 'model', model));
    var t = add(l3, el('span', 't', 'T+ '));
    ticking(add(t, el('b')), r.elapsed_seconds);
    var l4 = add(bodyEl, el('div', 'tile-l4'));
    var bar = add(l4, el('div', 'hbar' + (spec.hb === 'warn' || spec.hb === 'bad' ? ' ' + spec.hb : '')));
    bar.setAttribute('aria-hidden', 'true');
    var fill = add(bar, el('i'));
    var hbt = add(l4, el('span', 'hbt' + (spec.hb === 'warn' || spec.hb === 'bad' ? ' ' + spec.hb : '')));
    if (spec.hb === 'none') {
      fill.setAttribute('style', 'width:0%');
      hbt.textContent = 'hb —';
    } else if (spec.hb === 'live') {
      var base = r.heartbeat_age_seconds;
      var item = {run: function (dt) {
        var age = Math.max(0, Math.floor((Number(base) || 0) + dt));
        fill.setAttribute('style', 'width:' + Math.max(8, 100 - age * 6) + '%');
        hbt.textContent = 'hb ' + dur(age);
      }};
      tickers.push(item);
      item.run(since());
    } else {
      fill.setAttribute('style', 'width:100%');
      ticking(hbt, r.heartbeat_age_seconds, 'hb ');
    }
    return mark('lanes', r.job, tile);
  }

  function heldTile(key, seed) {
    var tile = el('div', 'tile empty px-held');
    add(tile, PX.strip(key, {state: 'held', seed: seed, height: 16}));
    add(tile, el('div', 'lbl', 'held'));
    return tile;
  }

  function idleTile(key, off, seed, height) {
    var tile = el('div', 'tile empty free');
    add(tile, PX.strip(key, {state: off ? 'held' : 'idle', seed: seed, height: height || 16}));
    add(tile, el('div', 'lbl', off ? 'off' : 'idle'));
    return tile;
  }

  function queueLane(d) {
    var rows = d.queued || [];
    var lane = el('div', 'lane');
    lane.setAttribute('id', 'queue');
    lane.setAttribute('data-device', 'queue');
    var head = add(lane, el('div', 'lane-head'));
    add(head, el('div', 'name', 'queue'));
    add(head, el('div', 'sub', rows.length + ' waiting'));
    var laneBody = add(lane, el('div', 'lane-body'));
    var oldest = rows.reduce(function (m, r) { return Math.max(m, Number(r.age_seconds) || 0); }, 0);
    add(laneBody, PX.strip('queue', {effect: 'conveyor', tint: LANES.queue_tint, seed: 20, height: 12,
                                     speed: queueSpeed(oldest), still: !rows.length}));
    if (!rows.length) { add(laneBody, el('div', 'sub', 'queue empty')); return lane; }
    var ul = add(laneBody, el('ul', 'qrail'));
    rows.forEach(function (r, i) {
      var li = add(ul, el('li'));
      var a = add(li, link(null, ticketPageHref(r)));
      add(a, bot(r.recipient));
      add(a, el('span', 'ticket', jobLabel(r)));
      tierChip(a, r.tier, r.own_tier);
      add(li, el('span', 'what', r.action));
      ticking(add(li, el('span', 'when')), r.age_seconds, 'waiting ');
      if (r.no_waiter) add(li, link('chip bad', healthLink(r.no_waiter_link))).textContent = 'no waiter for ' + r.no_waiter;
      var belt = add(li, el('span', 'belt'));
      belt.setAttribute('aria-hidden', 'true');
      if (r.no_waiter) add(belt, PX.strip('queue-' + r.job, {effect: 'static', tint: '#f7768e', seed: 41 + i, height: 12}));
      else add(belt, PX.strip('queue-' + r.job, {effect: 'conveyor', tint: LANES.belt_tint, seed: 41 + i, height: 12,
                                                 speed: queueSpeed(r.age_seconds)}));
      mark('queue', r.job, li);
    });
    return lane;
  }

  function qaLane(d) {
    var qa = d.qa || {}, runners = qa.runners || [], queued = qa.queued || [], cap = qa.cap;
    if (!runners.some(function (r) { return r.open; }) && !queued.length) return null;
    var lane = el('div', 'lane');
    lane.setAttribute('data-device', 'qa');
    var head = add(lane, el('div', 'lane-head'));
    add(head, el('div', 'name', 'qa'));
    add(head, el('div', 'sub', queued.reduce(function (n, r) { return n + r.count; }, 0) + ' waiting'));
    var ul = add(add(lane, el('div', 'lane-body')), el('ul', 'qrail'));
    runners.forEach(function (r) {
      var li = add(ul, el('li'));
      add(li, el('span', 'ticket', r.runner));
      add(li, el('span', 'what', r.open + ' / ' + (cap === null || cap === undefined ? '?' : cap) + ' lanes'));
      if (cap !== null && cap !== undefined && r.open >= cap) add(li, chip('at cap', 'warn'));
    });
    queued.forEach(function (r) {
      var li = add(ul, el('li'));
      add(li, el('span', 'ticket', r.repo));
      add(li, el('span', 'what', r.count + ' waiting'));
    });
    return lane;
  }

  function renderLanes(d) {
    var running = d.running || [];
    var lanes = laneDevices(d);
    var root = panel('lanes', 'Lanes and slots');
    if (!root) return;
    var count = add(root.children[0], el('span', 'count'));
    var heldTiles = 0, idleTiles = 0;
    tileAnims = [];
    var box = add(root, el('div', 'lanes'));
    add(box, queueLane(d));
    var qa = qaLane(d);
    if (qa) add(box, qa);
    lanes.devices.forEach(function (dev, index) {
      var row = add(box, el('div', 'lane' + (dev.enabled ? '' : ' disabled')));
      row.setAttribute('data-device', dev.name);
      var head = add(row, el('div', 'lane-head'));
      var lh1 = add(head, el('div', 'lh1'));
      add(lh1, el('span', 'name', dev.name));
      var down = probeDown(d, dev.name);
      var devChip = deviceChip(dev, down);
      if (devChip) add(lh1, devChip);
      var slots = dev.slot_max === null || dev.slot_max === undefined ? Math.max(1, dev.used) : Math.max(1, dev.slot_max);
      var meter = add(lh1, el('div', 'lane-meter'));
      meter.setAttribute('aria-hidden', 'true');
      meter.setAttribute('style', 'width:' + (12 * slots) + 'px');
      add(meter, PX.strip('meter-' + dev.name, {effect: 'meter', tint: down ? '#f7768e' : '#9ece6a', used: dev.used,
                                                  slots: slots, seed: 21 + index, height: 8,
                                                  still: down || !dev.enabled}));
      var lh2 = add(head, el('div', 'lh2'));
      add(lh2, el('span', 'sub', dev.used + ' / ' + (dev.slot_max === null || dev.slot_max === undefined ? '?' : dev.slot_max) + ' slots'));
      if (dev.harnesses.length) add(lh2, el('span', 'harness', dev.harnesses.join(' · ')));
      var grid = add(row, el('div', 'tiles'));
      var seed = 0;
      dev.jobs.forEach(function (r) { seed += 1; add(grid, slotTile(r, seed)); });
      var held = dev.used - dev.jobs.length;
      for (var h = 0; h < held; h++) { seed += 1; add(grid, heldTile('slot-' + dev.name + '-' + seed, seed)); }
      var free = Math.max(0, (dev.slot_max || 0) - dev.used);
      if (!dev.used && !free) free = 1;
      heldTiles += held;
      if (dev.enabled) idleTiles += free;
      for (var i = 0; i < free; i++) { seed += 1; add(grid, idleTile('slot-' + dev.name + '-' + seed, !dev.enabled, seed)); }
    });
    if (lanes.wakes.length) {
      var row = add(box, el('div', 'lane'));
      row.setAttribute('data-device', 'grok-wakes');
      var head = add(row, el('div', 'lane-head'));
      add(head, el('div', 'name', 'grok wakes'));
      add(head, chip('BOT-TO-BOT', 'accent'));
      var grid = add(row, el('div', 'tiles'));
      lanes.wakes.forEach(function (r, i) { add(grid, slotTile(r, i + 1)); });
    }
    add(box, ciLaneEl(d.ci_lane));
    [[running.length, 'ok', ' running · '], [heldTiles, heldTiles ? 'warn' : null, ' held · '],
     [idleTiles, null, ' idle · '], [(d.queued || []).length, null, ' queued']].forEach(function (c) {
      add(count, el('b', c[1], c[0]));
      add(count, document.createTextNode(c[2]));
    });
  }

  function renderJustHappened(d) {
    var rows = ((d.handoff_savings || {}).recent || []).slice(0, 6);
    var root = panel('just-happened', 'Just happened', rows.length + ' newest handoffs');
    if (!root) return;
    if (!rows.length) { add(root, el('p', 'empty', 'no finished jobs yet')); }
    else {
      var ul = add(root, el('ul', 'feed'));
      rows.forEach(function (r) {
        var li = add(ul, el('li'));
        var wake = r.kind === 'bot_to_bot';
        add(li, bot(r.recipient));
        add(li, el('span', 'ticket', ticketOf(r)));
        addGh(li, r);
        add(li, el('span', 'what', wake ? 'done' : r.action));
        if (wake) add(li, chip('grok wake', 'wake'));
        if (r.state === 'failed') add(li, chip('failed', 'bad'));
        ticking(add(li, el('span', 'when')), r.age_seconds, '', ' ago');
        mark('just-happened', r.job + ':' + r.state, li);
      });
    }
    var more = add(root, el('p', 'more'));
    add(more, link(null, 'activity.html')).textContent = 'all activity';
  }

  var FEEDER_STATE = {'eligible': 'eligible', 'waiting-capacity': 'waiting for capacity',
    'waiting-blocked': 'waiting on a blocker', 'refused-intake': 'refused at intake', 'feeding': 'feeding',
    'error': 'feed error, retrying', 'fed': 'fed', 'done': 'fed, job queued', 'closed': 'PR closed, not fed',
    'split-parent': 'split into parts, not fed', 'abandoned': 'abandoned, not fed'};

  function tierChip(parent, tier, own) {
    if (tier === null || tier === undefined) return;
    if (own === undefined) own = null;
    add(parent, chip('tier ' + tier + (own !== tier ? ' (own ' + (own === null ? 'none' : own) + ')' : ''), 'accent'));
  }

  function intakeGroups(items) {
    var groups = {trouble: [], next: [], blocked: [], fed: []};
    items.forEach(function (x) {
      var age = x.fed_age_seconds;
      var group = LANES.intake_group[x.state] || (age !== null && age !== undefined && age < LANES.fed_today ? 'fed' : null);
      if (group) groups[group].push(x);
    });
    groups.fed.sort(function (a, b) { return a.fed_age_seconds - b.fed_age_seconds; });
    return groups;
  }

  function intakeTicket(li, x) { add(li, el('span', 'ticket', short(x.repo) + '#' + x.issue)); }
  function intakeWhat(li, x) { add(li, el('span', 'what', x.reason || FEEDER_STATE[x.state] || x.state)); }

  function intakeRow(ul, x) {
    var li = add(ul, el('li'));
    intakeTicket(li, x);
    if (x.title) add(li, el('span', 'ititle', x.title)).setAttribute('title', x.title);
    tierChip(li, x.tier, x.own_tier);
    intakeWhat(li, x);
    return li;
  }

  function fedRow(ul, x) {
    var li = intakeRow(ul, x);
    if (x.pr) add(li, el('span', null, '→ PR #' + x.pr));
    ticking(add(li, el('span', 'when')), x.fed_age_seconds, 'fed ', ' ago');
    return li;
  }

  function intakeRows(box, col, items, draw) {
    var rows = LANES.intake.rows;
    var ul = add(box, el('ul', 'feed'));
    items.slice(0, rows).forEach(function (x) { (draw || intakeRow)(ul, x); });
    if (items.length > rows) add(box, el('p', 'more', '+' + (items.length - rows) + ' ' + col.more));
  }

  var INTAKE_BODY = {next: intakeRows, blocked: intakeRows,
    fed: function (box, col, items) { intakeRows(box, col, items, fedRow); }};

  function intakeColumn(parent, col, items) {
    var box = add(parent, el('div', 'icol'));
    add(box, el('div', 'igroup', col.label));
    if (items.length) INTAKE_BODY[col.key](box, col, items);
    else add(box, el('p', 'empty', col.empty));
  }

  function renderIntake(d) {
    var w = d.poller && d.poller.linear_webhook;
    var f = d.feeder;
    var g = f ? intakeGroups(f.items || []) : null;
    var line = null;
    if (w) {
      var received;
      if (w.last_received_age_seconds !== null && w.last_received_age_seconds !== undefined) {
        received = dur(w.last_received_age_seconds) + ' ago' + (w.last_event ? ' (' + w.last_event + ')' : '');
      } else if (w.listener_age_seconds === null || w.listener_age_seconds === undefined) {
        received = 'never (listener has not started)';
      } else {
        received = 'never';
      }
      line = 'Linear' + ' webhook · last received ' + received + ' · dirty queue ' + w.dirty;
    }
    var root = panel('intake', 'Intake', line);
    if (!root) return;
    if (line) root.children[0].children[1].setAttribute('id', 'linear-webhook');
    var box = add(root, el('div', 'intake'));
    if (w) {
      if (w.warning !== null && w.warning !== undefined) add(box, el('p', 'bad', 'warning: ' + w.warning));
      if (w.note !== null && w.note !== undefined) add(box, el('p', 'muted', w.note));
    }
    if (f) {
      var funnel = add(box, el('ol', 'funnel'));
      funnel.setAttribute('id', 'feeder');
      var numbers = Object.assign({}, f, {fed_today: g.fed.length});
      var num = function (key) { return numbers[key] || 0; };
      LANES.funnel.forEach(function (stage) {
        var parts = stage.parts || [];
        var count = parts.length ? parts.reduce(function (sum, p) { return sum + num(p.key); }, 0) : num(stage.key);
        var li = add(funnel, el('li', 'fstage'));
        var body = stage.href ? add(li, link(null, stage.href)) : li;
        add(body, el('span', 'flabel', stage.label));
        add(body, el('b', 'fnum', count));
        if (!parts.length) return;
        var bar = add(body, el('span', 'fbar'));
        bar.setAttribute('aria-hidden', 'true');
        var split = add(body, el('span', 'fsplit'));
        parts.forEach(function (p) {
          add(bar, el('i', p.cls)).setAttribute('style', 'flex-grow:' + num(p.key));
          add(split, el('span', null, num(p.key) + ' ' + p.label));
        });
      });
    }
    if (!f) { add(box, el('p', 'empty', w ? 'no feeder has run' : 'no listener or feeder has run')); return; }
    if (g.trouble.length) {
      var trouble = add(box, el('ul', 'itrouble'));
      g.trouble.forEach(function (x) {
        var li = add(trouble, el('li'));
        intakeTicket(li, x);
        intakeWhat(li, x);
      });
    }
    var cols = add(root, el('div', 'icols'));
    LANES.intake.columns.forEach(function (col) { intakeColumn(cols, col, g[col.key]); });
  }
