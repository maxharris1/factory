
  var RT_SOURCE = {roles: 'roles', budget_overrides: 'budget override', inherit: 'inherit', unset: 'not set'};
  var RT_BOTS = {claude: 'claude-bot', codex: 'codex-bot', cursor: 'cursor-bot', gemini: 'gemini-bot'};
  var RT_NOTE = 'The first eligible candidate in each list runs. ' +
    'A roles effort above the budget\'s ceiling is cut to it; a budget override runs as written. ' +
    'Each runs line is the model and effort a new job gets once the bot\'s waiter-<bot>.yml argv is applied; ' +
    'CLI default means the harness picks.';
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
  function routingRuns(item, e) {
    var runs = add(item, el('div', 'rt-runs', 'runs '));
    routingValues(runs, e);
    var flags = ['model', 'effort'].filter(function (k) { return k in e.replaced; })
      .map(function (k) { return 'waiter ' + e.replaced[k] + ' replaced'; });
    if (!e.applied) flags.push('routing.yml flags not added by this waiter');
    flags.forEach(function (f) { add(runs, el('span', 'rt-flag', f)); });
  }
  function routingCandidate(list, c) {
    var item = add(list, el('li'));
    var who = [c.harness, c.model].filter(function (x) { return x; }).join(' · ');
    if (c.effort === null) {
      add(item, document.createTextNode(who));
    } else {
      add(item, document.createTextNode(who + ' · '));
      var clamped = c.configured !== c.effort;
      add(item, el('span', clamped ? 'rt-e clamped' : 'rt-e', clamped ? c.configured + ' → ' + c.effort : c.effort));
    }
    routingRuns(item, c.effective);
  }
  function routingFallback(cell, r) {
    add(cell, el('span', 'muted', 'falls back to factory.md ' + (r.role === 'review' ? 'reviewer' : 'builder')));
    if (!r.fallback.length) return;
    var list = add(cell, el('ul', 'rt-fallback'));
    r.fallback.forEach(function (f) { routingValues(add(list, el('li', null, f.harness + ': ')), f); });
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
      var cell = add(tr, el('td'));
      if (!r.candidates.length) { routingFallback(cell, r); return; }
      var list = add(cell, el('ol', 'rt-pool'));
      r.candidates.forEach(function (c) { routingCandidate(list, c); });
    });
    var unread = Object.keys(RT_BOTS).filter(function (h) { return m.waiters[h] === 'missing'; })
      .map(function (h) { return RT_BOTS[h]; });
    if (unread.length) {
      add(root, el('p', 'rt-waiters',
        'waiter-<bot>.yml not read for ' + unread.join(', ') + '; their unrouted values show unknown'));
    }
    add(root, el('p', 'rt-note', RT_NOTE));
  }
