
  var RT_SOURCE = {roles: 'roles', budget_overrides: 'budget override', inherit: 'inherit', unset: 'not set'};
  var RT_NOTE = 'The first eligible candidate in each list runs. ' +
    'A roles effort above the budget\'s ceiling is cut to it; a budget override runs as written.';
  function routingCandidate(list, c) {
    var item = add(list, el('li'));
    var who = [c.harness, c.model].filter(function (x) { return x; }).join(' · ');
    if (c.effort === null) { item.textContent = who; return; }
    add(item, document.createTextNode(who + ' · '));
    var clamped = c.configured !== c.effort;
    add(item, el('span', clamped ? 'rt-e clamped' : 'rt-e', clamped ? c.configured + ' → ' + c.effort : c.effort));
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
      if (!r.candidates.length) {
        add(cell, el('span', 'muted', 'falls back to factory.md ' + (r.role === 'review' ? 'reviewer' : 'builder')));
        return;
      }
      var list = add(cell, el('ol', 'rt-pool'));
      r.candidates.forEach(function (c) { routingCandidate(list, c); });
    });
    add(root, el('p', 'rt-note', RT_NOTE));
  }
