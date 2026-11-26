
  // --- evals page ------------------------------------------------------------

  var EV = __EVALS__;
  var EVALS = {bundle: null, epoch: null, tried: null, poll: null, busy: false};
  var EV_STATES = ['ok', 'no snapshots', 'no dir'];
  function evNumber(v) { return typeof v === 'number' && isFinite(v) ? v : null; }
  function evObject(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }
  function evValue(row, weeks, i) {
    function read(j) { return j < weeks.length ? evNumber(weeks[j].metrics[row.metric]) : null; }
    var now = read(i);
    if (!row.shipped || now === null) return now;
    var before = read(i + 1);
    return before === null ? null : now - before;
  }
  function evHalfUp(x, digits) {
    var p = Math.pow(10, digits), scaled = x * p;
    return isFinite(scaled) ? Math.round(scaled) / p : null;
  }
  function evFixed(n, digits) {
    if (n < 1e21) return n.toFixed(digits);
    return BigInt(n).toString() + (digits ? '.' + new Array(digits + 1).join('0') : '');
  }
  function evFormat(kind, n, suffix, signed) {
    var sign = n < 0 ? '-' : signed && n > 0 ? '+' : '';
    var parts = evFixed(Math.abs(n), kind.digits).split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return sign + kind.prefix + parts.join('.') + suffix;
  }
  function evCell(row, weeks, i) {
    var kind = EV.kinds[row.kind];
    var now = evValue(row, weeks, i), before = evValue(row, weeks, i + 1);
    var td = el('td');
    var shown = now === null ? null : evHalfUp(now * kind.scale, kind.digits);
    add(td, el('span', 'ev-v', shown === null ? 'n/a' : evFormat(kind, shown, kind.suffix, false)));
    add(td, document.createTextNode(' '));
    var delta = now === null || before === null ? null : evHalfUp((now - before) * kind.scale, kind.digits);
    if (delta === null) { add(td, chip('n/a')); return td; }
    var tone = !delta ? 'muted' : (delta > 0) === (row.better === 'up') ? 'ok' : 'bad';
    add(td, chip(evFormat(kind, delta, kind.delta_suffix, true), tone));
    return td;
  }
  function drawEvals(b) {
    var weeks = b.weeks;
    var count = weeks.length ? weeks.length + ' week' + (weeks.length === 1 ? '' : 's') : null;
    var root = panel('evals', EV.texts.title, count);
    if (!root) return;
    if (b.state === 'no dir') add(root, el('p', 'empty', EV.texts.no_dir));
    else if (b.state !== 'ok' || !weeks.length) add(root, el('p', 'empty', EV.texts.no_snapshots));
    else {
      var table = add(add(root, el('div', 'ev-scroll')), el('table', 'ev-table'));
      var head = add(add(table, el('thead')), el('tr'));
      add(head, el('th', null, 'metric'));
      weeks.forEach(function (week) { add(head, el('th', null, week.week)).setAttribute('scope', 'col'); });
      var tbody = add(table, el('tbody'));
      EV.rows.forEach(function (row) {
        var tr = add(tbody, el('tr'));
        add(tr, el('th', null, row.label)).setAttribute('scope', 'row');
        for (var i = 0; i < weeks.length; i++) add(tr, evCell(row, weeks, i));
      });
    }
    if (b.skipped) add(root, el('p', 'ev-skipped', b.skipped + ' report' + (b.skipped === 1 ? '' : 's') + ' skipped.'));
  }
  function evalsBundle(data) {
    if (!evObject(data) || data.schema !== 1 || EV_STATES.indexOf(data.state) < 0) return null;
    if (typeof data.skipped !== 'number' || data.skipped < 0 || data.skipped % 1) return null;
    if (evNumber(data.generated_at_epoch) === null || !Array.isArray(data.weeks)) return null;
    var good = data.weeks.every(function (w) { return evObject(w) && typeof w.week === 'string' && evObject(w.metrics); });
    return good ? data : null;
  }
  function loadEvals() {
    var epoch = latest.generated_at_epoch;
    if (EVALS.busy || EVALS.poll === accepted || EVALS.tried === epoch) return;
    if (EVALS.bundle && EVALS.epoch >= epoch) return;
    EVALS.busy = true;
    function settle() { EVALS.busy = false; EVALS.poll = accepted; }
    function fail() { settle(); EVALS.tried = epoch; }
    fetch('evals.json', {cache: 'no-store', credentials: 'same-origin'}).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      var bundle = evalsBundle(data);
      if (!bundle) { fail(); return; }
      settle();
      if (bundle.generated_at_epoch < epoch) return;
      EVALS.bundle = bundle;
      EVALS.epoch = bundle.generated_at_epoch;
      render(latest);
    }, fail);
  }
  function renderEvals() {
    loadEvals();
    tickers.push({run: loadEvals});
    if (EVALS.bundle) drawEvals(EVALS.bundle);
  }
