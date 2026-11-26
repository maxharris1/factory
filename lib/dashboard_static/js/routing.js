
  var RT_SOURCE = {roles: 'roles', budget_overrides: 'budget override', inherit: 'inherit', unset: 'not set'};
  var RT_NOTE = 'The first eligible candidate in each list runs. ' +
    'A roles effort above the budget\'s ceiling is cut to it; a budget override runs as written. ' +
    'Each runs line is the model and effort a new job gets once the bot\'s waiter-<bot>.yml argv is applied; ' +
    'CLI default means the harness picks. ' +
    'A repository\'s factory.md routing.slots override runs as written and is not shown.';
  var RT_UNROUTED = 'A repository whose factory.md has no routing: block, or a factory.md slot naming no model, ' +
    'runs each waiter\'s own values: ';
  function routingValue(node, value, source) {
    if (value === null) { add(node, el('span', 'rt-v default', source)); return; }
    add(node, el('span', 'rt-v', value));
    add(node, el('span', 'rt-src', source));
  }
  function routingValues(node, found) {
    routingValue(node, found.model, found.model_source);
    add(node, document.createTextNode(' · '));
    routingValue(node, found.effort, found.effort_source);
  }
  function routingRuns(node, found, flags) {
    var runs = add(node, el('div', 'rt-runs', 'runs '));
    routingValues(runs, found);
    flags.forEach(function (f) { add(runs, el('span', 'rt-flag', f)); });
  }
  function routingFlags(c, builder) {
    var e = c.effective;
    var flags = ['model', 'effort'].filter(function (k) { return k in e.replaced; })
      .map(function (k) { return 'waiter ' + e.replaced[k] + ' replaced'; });
    if (e.applied === false) flags.push('routing.yml flags not added by this waiter');
    if (c.on_builder === false) flags.push('skipped: the builder runs on ' + builder);
    return flags;
  }
  function routingHead(item, c) {
    var who = [c.harness, c.model].filter(function (x) { return x; }).join(' · ');
    if (c.effort === null) {
      add(item, document.createTextNode(who));
      return;
    }
    add(item, document.createTextNode(who + ' · '));
    var clamped = c.configured !== c.effort;
    add(item, el('span', clamped ? 'rt-e clamped' : 'rt-e', clamped ? c.configured + ' → ' + c.effort : c.effort));
  }
  function routingPool(cell, r, each) {
    var list = add(cell, el('ol', 'rt-pool'));
    r.candidates.forEach(function (c) {
      var item = add(list, el('li'));
      routingHead(item, c);
      each(item, c);
    });
  }
  function routingDispatched(cell, r) {
    if (!r.candidates.length) {
      var side = r.role === 'review' ? 'reviewer' : 'builder';
      add(cell, el('span', 'muted', 'falls back to factory.md ' + side + ': its model, else the waiter\'s'));
      return;
    }
    routingPool(cell, r, function (item, c) { routingRuns(item, c.effective, routingFlags(c, r.builder)); });
    if (r.plans === 'build') {
      add(cell, el('div', 'rt-plans', 'no plan candidate on ' + r.builder + ': the build pick plans'));
    }
  }
  function routingLaunched(cell, r) {
    if (r.launch === 'dispatcher') { routingDispatched(cell, r); return; }
    routingPool(cell, r, function () {});
    if (r.launch === null) { add(cell, el('span', 'muted', 'no factory launcher')); return; }
    routingRuns(cell, r.runs, ['routing.yml not applied']);
  }
  function routingWaiters(root, waiters) {
    var bots = Object.keys(waiters);
    var missing = bots.filter(function (b) { return waiters[b] === 'missing'; });
    var opaque = bots.filter(function (b) { return waiters[b] === 'opaque'; });
    if (missing.length) {
      add(root, el('p', 'rt-waiters',
        'waiter-<bot>.yml not read for ' + missing.join(', ') + '; their unrouted values show unknown'));
    }
    if (opaque.length) {
      add(root, el('p', 'rt-waiters', 'waiter-<bot>.yml not interpreted for ' + opaque.join(', ') + '; ' +
        (opaque.length > 1 ? 'their' : 'its') + ' values show unknown'));
    }
  }
  function routingUnrouted(root, unrouted) {
    if (!unrouted.length) return;
    var line = add(root, el('p', 'rt-unrouted', RT_UNROUTED));
    unrouted.forEach(function (u, i) {
      if (i) add(line, document.createTextNode('; '));
      routingValues(add(line, el('span', null, u.harness + ': ')), u);
    });
  }
  function renderRouting(d) {
    var m = d.routing;
    var count = m.state === 'ok' ? m.budget + ' budget · effort up to ' + m.ceiling : null;
    var root = panel('routing', 'Role routing', count);
    if (!root) return;
    if (m.state === 'missing') { add(root, el('p', 'empty', 'config/routing.yml not found')); return; }
    if (m.state === 'invalid') {
      add(root, el('p', 'empty', 'config/routing.yml is invalid:'));
      var errors = add(root, el('ul', 'rt-errors'));
      m.errors.forEach(function (e) { add(errors, el('li', null, e)); });
      return;
    }
    var table = add(add(root, el('div', 'rt-scroll')), el('table', 'rt-table'));
    var head = add(add(table, el('thead')), el('tr'));
    ['role', 'source', 'preference order'].forEach(function (h) { add(head, el('th', null, h)); });
    var rows = add(table, el('tbody'));
    m.roles.forEach(function (r) {
      var tr = add(rows, el('tr'));
      add(tr, el('th', null, r.role)).setAttribute('scope', 'row');
      add(tr, el('td', null, RT_SOURCE[r.source]));
      routingLaunched(add(tr, el('td')), r);
    });
    routingWaiters(root, m.waiters);
    routingUnrouted(root, m.unrouted);
    add(root, el('p', 'rt-note', RT_NOTE));
  }
