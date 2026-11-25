
  // --- job telemetry (#93): the Efficiency rollup -------

  function usd(v) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    var c = Math.floor(v * 100 + 0.5);
    return '$' + Math.floor(c / 100).toLocaleString('en-US') + '.' + pad(c % 100);
  }
  var TOKEN_UNITS = [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
  function compactCount(n) {
    if (n < 10000) return num(n);
    var divisor = Math.pow(10, String(n).length - 4);
    var q = Math.floor((n + divisor / 2) / divisor);
    if (q === 10000) { q = 1000; divisor *= 10; }
    var value = q * divisor;
    var unit = TOKEN_UNITS.filter(function (u) { return u[0] <= value; })[0];
    var places = Math.max(0, 4 - String(Math.floor(value / unit[0])).length);
    var digits = String(value * Math.pow(10, places) / unit[0]);
    var cut = digits.length - places;
    return (digits.slice(0, cut) + '.' + digits.slice(cut)).replace(/0+$/, '').replace(/\.$/, '') + unit[1];
  }
  function teleTable(root, rows, first, chips) {
    if (!rows.length) { add(root, el('p', 'empty', 'no jobs with telemetry')); return; }
    var table = add(add(root, el('div', 'ef-scroll')), el('table'));
    var head = add(add(table, el('thead')), el('tr'));
    [first, 'jobs', 'tokens in', 'cached', 'tokens out', 'cost'].forEach(function (h) { add(head, el('th', null, h)); });
    var tbody = add(table, el('tbody'));
    rows.forEach(function (r) {
      var tr = add(tbody, el('tr'));
      if (chips) add(add(tr, el('td')), el('span', 'bot ' + r.name, r.name));
      else add(tr, el('td', null, r.name));
      add(tr, el('td', 'num', num(r.jobs)));
      add(tr, el('td', 'num', num(r.tokens_in)));
      add(tr, el('td', 'num', num(r.tokens_cached)));
      add(tr, el('td', 'num', num(r.tokens_out)));
      add(tr, el('td', r.priced_jobs ? 'num' : 'num muted', r.priced_jobs ? usd(r.cost_usd) : 'no cost reported'));
    });
  }
  function cachedLine(box, t) {
    if (!t.tokens_in) return;
    var line = add(box, el('div', 'cached',
      Math.floor((200 * t.tokens_cached + t.tokens_in) / (2 * t.tokens_in)) + '% cached'));
    line.setAttribute('title', compactCount(t.tokens_cached) + ' of ' + compactCount(t.tokens_in) + ' tokens in were cached');
  }
  function renderTokens(d) {
    var r = d.telemetry || {window_days: 7, totals: {jobs: 0}, by_harness: [], by_repo: []};
    var root = panel('tokens', 'Cost, last ' + r.window_days + ' days', 'as reported by each harness');
    if (!root) return;
    var t = r.totals || {jobs: 0};
    if (!t.jobs) { add(root, el('p', 'empty', 'no telemetry')); return; }
    var stats = add(root, el('div', 'stats'));
    [[t.priced_jobs ? usd(t.cost_usd) : null, 'cost', null, null],
     [num(t.jobs), 'jobs', null, null],
     [compactCount(t.tokens_in), 'tokens in', num(t.tokens_in), cachedLine],
     [compactCount(t.tokens_out), 'tokens out', num(t.tokens_out), null]].forEach(function (s) {
      var box = add(stats, el('div', 'stat'));
      if (s[2] !== null) box.setAttribute('title', s[2]);
      add(box, s[0] === null ? el('b', 'none', 'no cost reported') : el('b', null, s[0]));
      add(box, el('span', null, s[1]));
      if (s[3]) s[3](box, t);
    });
    add(root, el('h3', 'tele-head', 'per harness'));
    teleTable(root, r.by_harness || [], 'harness', true);
    var unpriced = (r.by_harness || []).filter(function (h) { return h.jobs > h.priced_jobs; })
      .map(function (h) { return [h.jobs - h.priced_jobs, h.name]; })
      .sort(function (a, b) { return b[0] - a[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0); });
    if (unpriced.length) {
      add(root, el('p', 'muted', unpriced.reduce(function (sum, u) { return sum + u[0]; }, 0) + ' jobs report no cost: ' +
        unpriced.map(function (u) { return u[1] + ' ' + u[0]; }).join(', ')));
    }
    if (t.unmetered_jobs) add(root, el('p', 'muted', t.unmetered_jobs + ' jobs report no token usage (no data)'));
    teleTable(details(root, 'tokens-repo', 'per repo'), r.by_repo || [], 'repo', false);
  }
