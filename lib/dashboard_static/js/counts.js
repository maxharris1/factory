
  var COUNTS = __COUNTS__;
  function countsOf(d) {
    var found = {};
    boardOf(d, '').columns.forEach(function (col) { found[col.phase] = col.cards.length; });
    found.ci = d.ci_lane.running + d.ci_lane.queued;
    found.blocked = ((d.feeder || {}).items || []).filter(function (item) { return item.state === COUNTS.blocked; }).length;
    return found;
  }
  function renderCounts(d) {
    var found = countsOf(d);
    var root = panel('counts', COUNTS.title);
    if (!root) return;
    COUNTS.cells.forEach(function (cell) {
      var n = found[cell.key];
      var a = add(root, link('jc-cell', cell.href));
      a.setAttribute('data-cell', cell.key);
      add(a, el('b', !n ? 'jc-zero' : COUNTS.bad.indexOf(cell.key) >= 0 ? 'jc-bad' : null, n === undefined ? '—' : n));
      add(a, document.createTextNode(' ' + cell.label));
    });
  }
