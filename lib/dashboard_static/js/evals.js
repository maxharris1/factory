
  // --- evals page ------------------------------------------------------------

  var EV = __EVALS__;
  var EVALS = {bundle: null, shown: generated, tried: null, poll: null, busy: false};
  var EV_STATES = ['ok', 'no snapshots', 'no dir'];
  var EV_DAY = /^\d{4}-\d{2}-\d{2}$/, EV_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
  function evNumber(v) { return typeof v === 'number' && isFinite(v) ? v : null; }
  function evObject(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }
  function evDay(v) {
    if (typeof v !== 'string' || !EV_DAY.test(v)) return false;
    var p = v.split('-').map(Number), d = new Date(0);
    // setUTCFullYear, unlike Date.UTC, keeps years 1-99 as written.
    d.setUTCFullYear(p[0], p[1] - 1, p[2]);
    return p[0] >= 1 && d.toISOString().slice(0, 10) === v;
  }
  function evTime(v) { return v === undefined || v === null || typeof v === 'string' && EV_TIME.test(v); }
  function evMetric(v) { return v === null || v === EV.unrecorded || evNumber(v) !== null; }
  function evWeek(w) {
    return evObject(w) && evDay(w.week) && evTime(w.since) && evTime(w.until) && evObject(w.metrics) &&
      Object.keys(w.metrics).every(function (k) { return EV.metric_keys.indexOf(k) >= 0 && evMetric(w.metrics[k]); });
  }
  var EV_CHECKS = [
    function (b) { return b.schema === 1 && EV_STATES.indexOf(b.state) >= 0; },
    function (b) { return Number.isSafeInteger(b.skipped) && b.skipped >= 0; },
    function (b) { return evNumber(b.generated_at_epoch) !== null; },
    function (b) { return Array.isArray(b.weeks) && b.weeks.length <= EV.keep; },
    function (b) { return (b.state === 'ok') === (b.weeks.length > 0); },
    function (b) { return b.state !== 'no dir' || b.skipped === 0; },
    function (b) { return b.weeks.every(function (w, i) { return evWeek(w) && (!i || b.weeks[i - 1].week > w.week); }); },
  ];
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
  // Python's exact round-half-even on the double's binary value; toFixed rounds ties up.
  function evFixed(n, digits) {
    var view = new DataView(new ArrayBuffer(8));
    view.setFloat64(0, n);
    var hi = view.getUint32(0), exp = (hi >>> 20) & 0x7ff;
    var mant = (BigInt(hi & 0xfffff) << 32n) | BigInt(view.getUint32(4));
    if (exp) mant |= 1n << 52n; else exp = 1;
    var v = mant * 10n ** BigInt(digits), shift = BigInt(1075 - exp);
    if (shift <= 0n) v <<= -shift;
    else {
      var q = v >> shift, r = v - (q << shift), half = 1n << (shift - 1n);
      v = r > half || (r === half && (q & 1n)) ? q + 1n : q;
    }
    var s = v.toString().padStart(digits + 1, '0');
    return digits ? s.slice(0, -digits) + '.' + s.slice(-digits) : s;
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
    return evObject(data) && EV_CHECKS.every(function (ok) { return ok(data); }) ? data : null;
  }
  function loadEvals() {
    var epoch = latest.generated_at_epoch;
    if (EVALS.busy || EVALS.poll === accepted || EVALS.tried === epoch) return;
    if (EVALS.bundle && EVALS.shown >= epoch) return;
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
      var at = bundle.generated_at_epoch;
      if (at < EVALS.shown || (at === EVALS.shown && EVALS.bundle)) return;
      EVALS.bundle = bundle;
      EVALS.shown = at;
      render(latest);
    }, fail);
  }
  function renderEvals() {
    loadEvals();
    tickers.push({run: loadEvals});
    if (EVALS.bundle) drawEvals(EVALS.bundle);
  }
