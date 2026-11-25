
  // --- evals page ------------------------------------------------------------

  var EV = __EVALS__;
  var EVALS = {bundle: null, asked: null, busy: false};
  function evNumber(v) { return typeof v === 'number' ? v : null; }
  function evValue(row, weeks, i) {
    function read(j) { return j < weeks.length ? evNumber(weeks[j].metrics[row.metric]) : null; }
    var now = read(i);
    if (!row.shipped || now === null) return now;
    var before = read(i + 1);
    return before === null ? null : now - before;
  }
  function evHalfUp(x, digits) { var p = Math.pow(10, digits); return Math.floor(x * p + 0.5) / p; }
  function evFormat(kind, n, suffix, signed) {
    var sign = n < 0 ? '-' : signed && n > 0 ? '+' : '';
    var parts = Math.abs(n).toFixed(kind.digits).split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return sign + kind.prefix + parts.join('.') + suffix;
  }
  function evCell(row, weeks, i) {
    var kind = EV.kinds[row.kind];
    var now = evValue(row, weeks, i), before = evValue(row, weeks, i + 1);
    var td = el('td');
    var text = now === null ? 'n/a' : evFormat(kind, evHalfUp(now * kind.scale, kind.digits), kind.suffix, false);
    add(td, el('span', 'ev-v', text));
    add(td, document.createTextNode(' '));
    if (now === null || before === null) { add(td, chip('n/a')); return td; }
    var delta = evHalfUp((now - before) * kind.scale, kind.digits);
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
    if (!data || typeof data.state !== 'string' || typeof data.skipped !== 'number') return null;
    if (!Array.isArray(data.weeks)) return null;
    var good = data.weeks.every(function (w) { return w && w.metrics && typeof w.metrics === 'object'; });
    return good ? data : null;
  }
  function loadEvals(d) {
    var epoch = d.generated_at_epoch;
    if (EVALS.busy || EVALS.asked === epoch) return;
    EVALS.busy = true;
    EVALS.asked = epoch;
    fetch('evals.json', {cache: 'no-store', credentials: 'same-origin'}).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      EVALS.busy = false;
      var bundle = evalsBundle(data);
      if (!bundle) return;
      EVALS.bundle = bundle;
      if (latest) render(latest);
    }, function () { EVALS.busy = false; });
  }
  function renderEvals(d) {
    loadEvals(d);
    if (EVALS.bundle) drawEvals(EVALS.bundle);
  }
