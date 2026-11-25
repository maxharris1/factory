
  function halfUp(x) { return Math.floor(x + 0.5); }
  function tenths(t) { return Math.floor(t / 10) + '.' + (t % 10); }
  function renderSavings(d) {
    var s = d.handoff_savings;
    var root = panel('savings', 'Handoff savings', 'scripted / (scripted + grok wakes)');
    if (!root) return;
    var lit = s.scripted_share === null ? null : halfUp(s.scripted_share * 100);
    var box = add(root, el('div', 'ef-savings'));
    var waffle = add(box, el('div', lit === null ? 'waffle none' : 'waffle'));
    waffle.setAttribute('role', 'img');
    waffle.setAttribute('aria-label', lit === null ? 'no handoffs yet'
      : lit + ' of 100 squares are scripted handoffs, ' + (100 - lit) + ' are grok wakes');
    for (var i = 0; i < 100; i++) add(waffle, el('span', lit !== null && i < lit ? 's' : null));
    var share = add(box, el('div', 'ef-share'));
    add(share, el('b', null, lit === null ? '—' : lit + '%'));
    add(share, el('span', null, 'scripted share'));
    var counts = add(box, el('div', 'ef-counts'));
    [[s.scripted, 'scripted handoffs', null], [s.scripted_in_flight, 'scripted in flight', null],
     [s.bot_to_bot, 'bot-to-bot (grok wakes)', 'grok'],
     [s.engine_handled, 'engine handled', null], [s.engine_escalated, 'engine escalated', null]].forEach(function (x) {
      var line = add(counts, el('div'));
      add(line, el('b', x[2], num(x[0])));
      add(line, el('span', null, x[1]));
    });
    add(root, el('p', 'definition', s.scripted_share_definition)).setAttribute('id', 'share-definition');
    add(details(root, 'savings-def', 'how each count is made'), el('p', 'muted', s.definition));
  }

  function renderJobsLifetime(d) {
    var t = d.queue.totals;
    var finished = t.done + t.failed;
    var root = panel('jobs-lifetime', 'Lifetime');
    if (!root) return;
    [[t.done, 'ok', ' jobs done'], [t.failed, 'bad', ' failed']].forEach(function (x, i) {
      if (i) add(root, el('span', 'ef-dot', '·')).setAttribute('aria-hidden', 'true');
      var life = add(root, el('span', 'ef-life'));
      add(life, el('b', x[1], num(x[0])));
      add(life, document.createTextNode(x[2]));
    });
    add(root, el('span', 'ef-pct', finished
      ? halfUp(100 * t.done / finished) + '% of ' + num(finished) + ' finished jobs succeeded' : 'no finished jobs yet'));
  }

  var SHIPPED = {merged: true, done: true, 'handed-off': true};
  var ROUND_BUCKETS = ['0 rounds', '1 round', '2 rounds', '3 rounds', '4 or more'];
  function renderRounds(d) {
    var shipped = (d.tickets || []).filter(function (r) { return SHIPPED[r.state]; });
    var root = panel('rounds', 'Revise rounds before ship', shipped.length + ' shipped tickets');
    if (!root) return;
    if (!shipped.length) { add(root, el('p', 'empty', 'no shipped tickets yet')); return; }
    var counts = ROUND_BUCKETS.map(function () { return 0; });
    shipped.forEach(function (r) { counts[Math.min(r.round, ROUND_BUCKETS.length - 1)] += 1; });
    var top = Math.max.apply(null, counts), total = shipped.length;
    var box = add(root, el('div', 'ef-rounds'));
    counts.forEach(function (n, i) {
      var line = add(box, el('div', 'ef-rb'));
      add(line, el('span', 'ef-k', ROUND_BUCKETS[i]));
      var track = add(line, el('span', 'ef-track'));
      track.setAttribute('aria-hidden', 'true');
      add(track, el('span', i ? 'ef-fill' : 'ef-fill zero'))
        .setAttribute('style', 'width:' + tenths(Math.floor((2000 * n + top) / (2 * top))) + '%');
      var count = add(line, el('span', 'ef-n'));
      add(count, el('b', i ? null : 'ok', num(n)));
      add(count, document.createTextNode(' · ' + Math.floor((200 * n + total) / (2 * total)) + '%'));
    });
    var foot = add(root, el('div', 'ef-foot'));
    add(foot, el('p', 'ef-note', counts[0] + ' of ' + total + ' shipped with no revise round'));
    add(foot, link(null, 'job.html')).textContent = 'see shipped tickets';
  }

  var WINDOWS = ['five_hour', 'seven_day'];
  var WINDOW_NAMES = {five_hour: '5-hour', seven_day: '7-day'};
  var METER_CELLS = 20;
  var HEADROOM_NOTE = 'Headroom is what is left in the fullest window.';
  function usageTone(x) { return x >= 0.5 ? 'ok' : x >= 0.2 ? 'warn' : 'bad'; }
  function accountName(e) { return e.label || e.account; }
  function accountAbout(e) {
    return [e.plan, e.source ? 'via ' + e.source : null,
      e.age_seconds === null ? 'never read' : 'read ' + dur(e.age_seconds) + ' ago']
      .filter(function (x) { return x; }).join(' · ');
  }
  function accountJobs(e) {
    return e.running + ' running · ' + (e.max_jobs === null ? 'no max' : 'max ' + e.max_jobs);
  }
  function accountDevices(e) {
    var parts = [e.signed_in.length ? 'signed in on ' + e.signed_in.join(', ') : null,
      e.running_on.length ? 'running on ' + e.running_on.join(', ') : null]
      .filter(function (x) { return x; });
    return parts.length ? parts.join(' · ') : 'none';
  }
  function accountView(e, now) {
    var fresh = WINDOWS.filter(function (w) { return e[w].state === 'ok'; });
    var planned = WINDOWS.filter(function (w) { return e[w].state !== 'not on plan'; });
    var v = {entry: e, limit: e.limit_label, pct: null, tone: null, tag: null, lit: 0};
    if (e.state === 'no telemetry') {
      v.windows = WINDOWS.map(function () { return [e.state, 'muted']; });
      v.resets = [e.state, 'muted'];
    } else {
      v.windows = WINDOWS.map(function (w) {
        if (fresh.indexOf(w) >= 0) return [halfUp(e[w].used * 100) + '% used', null];
        return [e[w].state === 'not on plan' ? 'not on this plan' : e[w].state, 'muted'];
      });
      var ends = fresh.filter(function (w) { return e[w].resets_at !== null; })
        .map(function (w) { return e[w].resets_at; });
      v.resets = ends.length ? ['in ' + dur(Math.min.apply(null, ends) - now), null] : ['no reading', 'muted'];
    }
    if (e.limit_label || !fresh.length) {
      v.label = accountName(e) + ' headroom: ' +
        (e.limit_label || (e.state === 'no telemetry' ? 'no telemetry' : 'no reading'));
      return v;
    }
    var only = fresh.length === 1 && planned.length > 1 ? WINDOW_NAMES[fresh[0]] : null;
    v.pct = halfUp(e.headroom * 100);
    v.lit = Math.floor((2 * v.pct + 5) / 10);
    v.tone = only ? 'neutral' : usageTone(e.headroom);
    v.tag = only ? '(' + only + ' only)' : null;
    v.label = accountName(e) + ' headroom ' + v.pct + ' percent' + (only ? ', from the ' + only + ' window only' : '') +
      ': ' + v.lit + ' of ' + METER_CELLS + ' cells lit';
    return v;
  }
  function usageCell(parent, c, tag) { add(parent, el(tag, c[1] ? 'ef-v ' + c[1] : 'ef-v', c[0])); }
  function accountWho(parent, e) {
    var line = add(parent, el('div', 'ef-who'));
    add(line, el('span', 'bot ' + e.harness, e.harness));
    add(line, el('span', 'ef-name', accountName(e)));
    add(parent, el('div', 'ef-dev', accountAbout(e)));
  }
  function headroomRoom(parent, v, phone) {
    var room = add(parent, el('div', 'ef-room'));
    if (v.limit) add(room, el('span', 'ef-lim bad', v.limit));
    else if (v.pct !== null) add(room, el('span', 'ef-hn ' + v.tone, v.pct + '%'));
    else if (!phone) add(room, el('span', 'ef-hn'));
    var meter = add(room, el('span', 'ef-meter'));
    meter.setAttribute('role', 'img');
    meter.setAttribute('aria-label', v.label);
    for (var i = 0; i < METER_CELLS; i++) add(meter, el('span', i < v.lit ? v.tone : null));
    if (!phone && v.tag) add(room, el('span', 'ef-tag', v.tag));
  }
  function untrackedLine(parent, h, row) {
    if (row) {
      var tr = add(parent, el('tr', 'ef-un'));
      add(add(tr, el('td')), el('span', 'bot ' + h, h));
      add(tr, el('td', 'ef-v muted', 'not tracked')).setAttribute('colspan', '6');
      return;
    }
    var line = add(parent, el('div', 'ef-un'));
    add(line, el('span', 'bot ' + h, h));
    add(line, el('span', 'ef-v muted', 'not tracked'));
  }
  function renderHeadroom(d) {
    var root = panel('headroom', 'Subscription headroom', 'per account');
    if (!root) return;
    var accounts = d.usage.accounts;
    var views = accounts.filter(function (e) { return e.state !== 'not tracked'; })
      .map(function (e) { return accountView(e, d.generated_at_epoch); });
    var untracked = [];
    accounts.filter(function (e) { return e.state === 'not tracked'; })
      .forEach(function (e) { if (untracked.indexOf(e.harness) < 0) untracked.push(e.harness); });
    var table = add(add(root, el('div', 'ef-scroll')), el('table', 'ef-usage'));
    var head = add(add(table, el('thead')), el('tr'));
    ['account', '5-hour window', '7-day window', 'resets', 'headroom', 'jobs', 'devices']
      .forEach(function (h) { add(head, el('th', null, h)); });
    var body = add(table, el('tbody'));
    views.forEach(function (v) {
      var tr = add(body, el('tr'));
      accountWho(add(tr, el('td')), v.entry);
      v.windows.concat([v.resets]).forEach(function (c) { usageCell(tr, c, 'td'); });
      headroomRoom(add(tr, el('td')), v, false);
      usageCell(tr, [accountJobs(v.entry), null], 'td');
      usageCell(tr, [accountDevices(v.entry), null], 'td');
    });
    untracked.forEach(function (h) { untrackedLine(body, h, true); });
    add(root, el('p', 'ef-hnote', HEADROOM_NOTE));
    var list = add(root, el('div', 'ef-accts'));
    views.forEach(function (v) {
      var block = add(list, el('div', 'ef-acct'));
      accountWho(block, v.entry);
      headroomRoom(block, v, true);
      add(block, el('div', 'ef-hl', v.tag ? 'headroom ' + v.tag : 'headroom'));
      var wins = add(block, el('div', 'ef-wins'));
      ['5-hour', '7-day', 'resets'].forEach(function (name, i) {
        var col = add(wins, el('div'));
        add(col, el('div', 'ef-wk', name));
        usageCell(col, v.windows.concat([v.resets])[i], 'div');
      });
      add(block, el('div', 'ef-dev', accountJobs(v.entry) + ' · ' + accountDevices(v.entry)));
    });
    untracked.forEach(function (h) { untrackedLine(list, h, false); });
    add(root, el('p', 'ef-hnote-ph', HEADROOM_NOTE));
  }
