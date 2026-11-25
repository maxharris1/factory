
  // --- system page: poll and Linear budget ------------------------------------

  var POLL_STATE = {ok: ['OK', 'ok'], late: ['LATE', 'warn'], stale: ['STALE', 'bad'], unknown: ['UNKNOWN', 'muted']};
  var BUDGET_STATE = {ok: ['OK', 'ok'], deferring: ['DEFERRING', 'warn'], exhausted: ['EXHAUSTED', 'bad'],
                      rate_limited: ['RATE LIMITED', 'bad']};
  function renderHealth(d) {
    var p = d.poller;
    var h = p.health || {state: 'unknown'};
    var b = p.linear_budget || {state: 'ok'};
    var root = panel('health', 'Poll and Linear budget');
    if (!root) return;
    var row = add(root, el('div', 'row'));
    add(row, el('span', 'label', 'poll'));
    var ps = POLL_STATE[h.state] || POLL_STATE.unknown;
    add(row, chip(ps[0], ps[1]));
    if (h.last_cycle_age_seconds === null || h.last_cycle_age_seconds === undefined) {
      add(row, el('span', 'muted', 'cycle time unknown (live push off)'));
    } else {
      ticking(add(row, el('span')), h.last_cycle_age_seconds, 'last cycle ', ' ago');
    }
    var row2 = add(root, el('div', 'row'));
    add(row2, el('span', 'label', 'linear'));
    var bs = BUDGET_STATE[b.state] || ['OTHER', 'muted'];
    add(row2, chip(bs[0], bs[1]));
    add(row2, el('span', 'muted', num(b.requests_per_hour) + ' req/h · ' + num(b.complexity_per_hour) + ' complexity/h'));
    var stats = add(root, el('div', 'stats'));
    [
      [p.errors.last_hour, 'poll errors, 1 h', p.errors.last_hour ? 'bad' : ''],
      [p.errors.last_day, 'poll errors, 24 h', ''],
      [b.deferrals_last_hour, 'Linear deferrals, 1 h', b.deferrals_last_hour ? 'warn' : ''],
      [(b.exhausted_last_hour || 0) + (b.rate_limited_last_hour || 0), 'Linear limit hits, 1 h',
       (b.exhausted_last_hour || b.rate_limited_last_hour) ? 'bad' : '']
    ].forEach(function (s) {
      var box = add(stats, el('div', 'stat'));
      add(box, el('b', s[2], num(s[0])));
      add(box, el('span', null, s[1]));
    });
    var activity = add(root, el('p', 'muted'));
    ticking(activity, p.last_activity_age_seconds, 'last poller activity ', ' ago');
    var recent = (p.errors.recent || []).slice(0, 3);
    if (recent.length) {
      list(root, recent, '', function (r) {
        var li = el('li');
        add(li, el('span', 'ticket', r.repo === 'poll' || r.ticket_id === '-' ? r.repo : ticketOf(r)));
        add(li, el('span', 'what', r.error));
        ticking(add(li, el('span', 'when')), r.age_seconds, '', ' ago');
        return li;
      });
    }
  }
