
  var DETAIL = {verbs: __VERBS__, hb: __HB__, order: {normal: 0, human: 1, stop: 2},
                first: 'ticket arrives', stop: '#f7768e', hop: '#c0caf5', who: ['script', 'claude', 'codex', 'cursor', 'grok'],
                chartSub: __CHARTSUB__, trackers: __TRACKERS__,
                kinds: __KINDS__, liveKinds: __LIVEKINDS__, recordFlags: __RECORDFLAGS__, rowFromTicket: __ROWFROMTICKET__,
                panels: __PANELS__, chartKeys: __CHARTKEYS__, endTint: __ENDTINT__, how: __HOW__,
                endPhases: __ENDPHASES__, readOnly: __READONLY__,
                phone: __PHONE__, journeySub: __JOURNEYSUB__, journeyTap: __JOURNEYTAP__,
                exposure: __EXPOSURE__, exposureSub: __EXPOSURESUB__};
  var SEL = {};
  var chartScroll = {};
  var journeys = [];
  var reducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var phoneQuery = window.matchMedia ? window.matchMedia(DETAIL.phone) : null;
  var journeyResize = typeof ResizeObserver === 'function' ? new ResizeObserver(fitJourneys) : null;
  if (phoneQuery && phoneQuery.addEventListener) phoneQuery.addEventListener('change', fitJourneys);

  function whoTint(w) { return JOURNEY.tint[w] || JOURNEY.tint.script; }
  function detailModel(r, chart) {
    var flow = r.flow || {};
    var ended = DETAIL.recordFlags.filter(function (f) { return flow[f]; })[0] || null;
    var phases = chart ? phaseTable(chart).states : {};
    var visits = (flow.visits || []).map(function (v, i) {
      var o = {};
      Object.keys(v).forEach(function (k) { o[k] = v[k]; });
      o.index = i;
      o.stop = phases[v.state] === 'Stop';
      return o;
    });
    var current = visits.filter(function (v) { return v.current; });
    var state = flow.state === undefined ? null : flow.state;
    var seen = {};
    visits.forEach(function (v) { seen[v.state] = (seen[v.state] || 0) + 1; });
    function kind(to) { return phases[to] === 'Stop' ? 'stop' : WAITING[to] ? 'human' : 'normal'; }
    var nexts = ((chart && chart.edges) || []).filter(function (e) { return state !== null && e.from === state; })
      .map(function (e, i) { return {to: e.to, label: e.label, kind: kind(e.to), count: seen[e.to] || 0, i: i}; });
    nexts.sort(function (a, b) { return (DETAIL.order[a.kind] - DETAIL.order[b.kind]) || (a.i - b.i); });
    var waysOut = ((ended && flow[ended].ways_out) || []).map(function (w) {
      return {to: w.to, label: w.label, end: DETAIL.endPhases.indexOf(phases[w.to]) >= 0};
    });
    return {
      visits: visits, selected: current.length ? current[0].index : visits.length - 1, state: state,
      terminal: !!flow.terminal, next: nexts, ways_out: waysOut, hops: Math.max(0, visits.length - 1),
      off_chart_count: visits.filter(function (v) { return v.off_chart; }).length,
      hops_to_shipped: flow.hops_to_shipped === undefined ? null : flow.hops_to_shipped,
      all_durations_known: !!flow.all_durations_known,
      state_age_seconds: flow.state_age_seconds === undefined ? null : flow.state_age_seconds,
      ended: ended, end: ended ? flow[ended] : null
    };
  }
  function endText(m) {
    var end = m.end, visits = m.visits, n = visits.length;
    if (m.ended === 'shipped') {
      var known = end.intake_to_ship_seconds !== null && end.intake_to_ship_seconds !== undefined;
      return 'shipped · ' + m.hops + (m.hops === 1 ? ' hop' : ' hops') + (known ? ' · ' + dur(end.intake_to_ship_seconds) : '');
    }
    var where = end.stopped_in;
    if (!where) where = n > 1 && visits[n - 1].stop ? visits[n - 2].state : n ? visits[n - 1].state : end.state;
    return 'stopped in ' + where + (m.state_age_seconds === null ? '' : ' · ' + dur(m.state_age_seconds) + ' ago');
  }
  function flowModel(m) {
    var visits = m.visits, state = m.state, next = m.next.map(function (n) { return n.to; });
    if (m.ended === 'stopped') {
      state = m.end.state;
      if (!visits.length || visits[visits.length - 1].state !== state) {
        visits = visits.concat([{index: visits.length, state: state, who: 'script', off_chart: false, current: true, stop: true}]);
      }
      next = (m.end.ways_out || []).map(function (w) { return w.to; });
    } else if (m.ended === 'shipped') next = [];
    return {visits: visits, state: state, next: next};
  }
  function roundOf(r) {
    var m = /^\s*(\d+)\s*\/\s*(\d+)\s*$/.exec(String(r.round === null || r.round === undefined ? '' : r.round));
    return m ? [Number(m[1]), Number(m[2])] : null;
  }
  function span(parent, cls, text) { return add(parent, el('span', cls, text)); }
  function crumb(label, right) {
    var nav = el('nav', 'jd-crumb');
    nav.setAttribute('aria-label', 'Breadcrumb');
    var a = add(nav, link(null, 'job.html'));
    a.textContent = 'Tickets';
    span(nav, null, '/').setAttribute('aria-hidden', 'true');
    span(nav, 'here', label);
    if (right !== null) span(nav, 'job', right);
    return nav;
  }
  function firstStart(m) {
    var starts = m.visits.map(function (v) { return v.started_at; }).filter(function (t) { return t !== null && t !== undefined; });
    return starts.length ? Math.min.apply(null, starts) : null;
  }
  function intakeAt(m) {
    var end = m.end;
    return known(end.at) && known(end.intake_to_ship_seconds) ? end.at - end.intake_to_ship_seconds : null;
  }
  var ET = null;
  function etClock(epoch) {
    ET = ET || new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit', hour12: true});
    var p = {};
    ET.formatToParts(new Date(epoch * 1000)).forEach(function (part) { p[part.type] = part.value; });
    return p.hour + ':' + p.minute + ' ' + p.dayPeriod + ' ET';
  }
  var ETDAY = null;
  function etDay(epoch) {
    ETDAY = ETDAY || new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York', weekday: 'short', month: 'short', day: 'numeric'});
    var p = {};
    ETDAY.formatToParts(new Date(epoch * 1000)).forEach(function (part) { p[part.type] = part.value; });
    return p.weekday + ' ' + p.month + ' ' + p.day + ', ' + etClock(epoch);
  }
  function stopAge(m) { return known(m.end.at) ? m.now - m.end.at : null; }
  function stopFacts(m) {
    var states = m.visits.map(function (v) { return v.state; });
    var path = states.length && states[states.length - 1] === m.end.state ? states.slice(0, -1) : states;
    return {hops_before: Math.max(0, path.length - 1), path: path};
  }
  function hopsText(n) { return n + (n === 1 ? ' hop' : ' hops'); }
  function known(x) { return x !== null && x !== undefined; }
  function reading(x, fmt) { return known(x) ? fmt(x) : 'no reading'; }
  function howOf(m) { return DETAIL.how[m.end.how]; }
  function tile(parent, value, label, cls) {
    var t = add(parent, el('div', 'jd-tile'));
    var b = add(t, el('b', cls || null));
    if (typeof value === 'string') b.textContent = value; else add(b, value);
    span(t, null, label);
    return b;
  }
  function motionOf(r, m) { return WAITING[m.state] ? 'waiting' : PX.state(r); }
  function holdsStill(motion) {
    return !PX.table.effects[(PX.table.states[motion] || PX.table.states.unknown).effect];
  }
  var TILES = {
    elapsed: function (box, r) { ticking(tile(box, '', r.device ? 'T+ on ' + r.device : 'T+ on this device'), r.elapsed_seconds); },
    heartbeat: function (box, r) {
      var hb = r.heartbeat_age_seconds;
      var tone = DETAIL.hb[PX.state(r)];
      if (hb === null || hb === undefined) tile(box, 'not started', 'last heartbeat', tone);
      else ticking(tile(box, '', 'last heartbeat', tone), hb, '', ' ago');
    },
    waiting: function (box, r) { ticking(tile(box, '', 'waiting'), r.age_seconds); },
    not_started: function (box) { tile(box, 'not started', 'last heartbeat'); },
    round: function (box, r) {
      var rnd = roundOf(r);
      if (!rnd) { tile(box, 'no round', 'revise round'); return; }
      var b = tile(box, rnd[0] + ' / ' + rnd[1], 'revise round');
      var pips = add(b, el('span', 'jd-pips'));
      pips.setAttribute('aria-hidden', 'true');
      for (var i = 0; i < rnd[1]; i++) span(pips, i < rnd[0] - 1 ? 'done' : i === rnd[0] - 1 ? 'now' : null);
    },
    record_round: function (box, r) { tile(box, r.flow.round === null ? 'no round' : String(r.flow.round), 'revise round'); },
    in_state: function (box, r, m) { ageTile(box, m.state_age_seconds, 'in ' + m.state); },
    stopped_for: function (box, r, m) {
      var age = stopAge(m);
      if (age === null) tile(box, 'no reading', 'time stopped');
      else ticking(tile(box, '', 'time stopped', 'bad'), age);
    },
    hops_before: function (box, r, m) { tile(box, hopsText(stopFacts(m).hops_before), 'before it stopped'); },
    stop_round: function (box, r) {
      var n = r.flow.round;
      if (!known(n)) tile(box, 'no round', 'revise round');
      else if (n) tile(box, String(n), 'revise round');
      else tile(box, 'none', 'revise round', 'soft');
    },
    hops: function (box, r, m) { tile(box, m.hops + ' hops', m.off_chart_count + ' off-chart'); },
    total: function (box, r, m) { tile(box, reading(m.end.intake_to_ship_seconds, dur), 'total, intake to ' + howOf(m)[1]); },
    rounds_done: function (box, r, m) {
      var n = m.end.revise_rounds;
      if (!known(n)) { tile(box, 'no round', 'revise rounds'); return; }
      var pips = add(tile(box, String(n), 'revise rounds'), el('span', 'jd-pips'));
      pips.setAttribute('aria-hidden', 'true');
      for (var i = 0; i < n; i++) span(pips, 'done');
    },
    cost: function (box, r, m) {
      var end = m.end;
      if (known(end.cost_usd)) tile(box, dollars(end.cost_usd), 'cost, ' + end.jobs + (end.jobs === 1 ? ' job' : ' jobs'));
    }
  };
  function ageTile(box, age, label) {
    if (age === null || age === undefined) tile(box, 'no reading', label);
    else ticking(tile(box, '', label), age);
  }
  function troubleChips(title, r) {
    var jobState = stateChip(r);
    if (jobState) add(title, jobState);
    if (r.overdue) add(title, chip('OVERDUE', 'bad'));
    if (r.retries) add(title, chip('retried ' + r.retries + 'x'));
  }
  function trackerText(r) {
    var source = String(r.source), pr = String(r.pr);
    var first = r.tracker === 'linear' ? String(r.ticket_id) : /^#[0-9]+$/.test(source) ? 'issue ' + source : '';
    var refs = [first, /^[0-9]+$/.test(pr) ? 'PR #' + pr : ''].filter(Boolean).join(' · ');
    return [DETAIL.trackers[r.tracker], refs].filter(Boolean).join(' ');
  }
  function trackerSpan(title, r, tail) {
    var text = trackerText(r);
    if (tail === 'queued') {
      ticking(span(title, 'tracker'), r.age_seconds, [text, 'queued for ' + r.recipient].filter(Boolean).join(' · ') + ' · ');
      return;
    }
    text = [text, tail === 'device' && r.device ? 'on ' + r.device : ''].filter(Boolean).join(' · ');
    if (text) span(title, 'tracker', text);
  }
  function stateLine(who, r, m, motion) {
    var action = String(r.action || '');
    var line = add(who, el('p', 'line'));
    var parts = 0;
    function sep() { if (parts++) line.appendChild(document.createTextNode(' · ')); }
    if (m.state) { sep(); line.appendChild(document.createTextNode('now ')); add(line, el('b', null, m.state)); }
    var rnd = roundOf(r);
    if (rnd) { sep(); line.appendChild(document.createTextNode('round ' + rnd[0] + ' of ' + rnd[1])); }
    if (m.state && m.state_age_seconds !== null) { sep(); ticking(add(line, el('span')), m.state_age_seconds, '', ' in ' + m.state); }
    if (!parts) line.textContent = 'state not recorded';
    var verb = add(who, el('div', 'jd-verb'));
    var glyph = span(verb, 'glyph');
    glyph.setAttribute('aria-hidden', 'true');
    [0, 1, 2, 3].forEach(function (i) { span(glyph, i === 2 ? 'lit' : null); });
    var verbs = PX.table.actions.hasOwnProperty(motion) ? DETAIL.verbs[motion] : null;
    if (verbs) span(verb, 'verb', verbs[Math.floor(Date.now() / 4000) % verbs.length] + '…');
    if (action) span(verb, 'phase', action.toUpperCase());
  }
  function startedClock(m) {
    var start = firstStart(m);
    return start === null ? null : 'started ' + etClock(start);
  }
  function endedClock(m) { return known(m.end.at) ? howOf(m)[0] + ' ' + etClock(m.end.at) : null; }
  function shippedLine(who, r, m) {
    var end = m.end, how = howOf(m), n = end.revise_rounds, took = end.intake_to_ship_seconds;
    var line = add(who, el('p', 'line'));
    add(line, el('b', 'ok', how[0]));
    if (known(end.at)) {
      line.appendChild(document.createTextNode(' '));
      ticking(add(line, el('span')), m.now - end.at, '', ' ago');
    }
    [known(n) ? n + ' revise round' + (n === 1 ? '' : 's') : '',
     known(took) ? dur(took) + ' from intake to ' + how[1] : ''].forEach(function (p) {
      if (p) line.appendChild(document.createTextNode(' · ' + p));
    });
    var when = add(who, el('div', 'jd-when'));
    span(when, 'mark').setAttribute('aria-hidden', 'true');
    var start = intakeAt(m);
    span(when, null, [start === null ? '' : 'intake ' + etClock(start), endedClock(m)].filter(Boolean).join(' · '));
  }
  function stoppedClock(m) { return known(m.end.at) ? 'stopped ' + etDay(m.end.at) : null; }
  function stoppedLine(who, r, m) {
    var end = m.end, age = stopAge(m);
    var line = add(who, el('p', 'line'));
    add(line, el('b', 'bad', end.state));
    if (end.stopped_in) line.appendChild(document.createTextNode(' in ' + end.stopped_in));
    if (age !== null) {
      line.appendChild(document.createTextNode(' for '));
      ticking(add(line, el('span')), age);
    }
    var when = add(who, el('div', 'jd-when stop'));
    span(when, 'mark').setAttribute('aria-hidden', 'true');
    span(when, null, ['no job running', stoppedClock(m)].filter(Boolean).join(' · '));
  }
  var LINES = {
    state: {crumb: startedClock, chip: function () {}, body: stateLine},
    shipped: {crumb: endedClock, chip: function (title, m) { add(title, chip(howOf(m)[0], 'ok')); }, body: shippedLine},
    stopped: {crumb: stoppedClock, chip: function (title, m) { add(title, chip(m.end.state, 'bad')); }, body: stoppedLine}
  };
  function hero(c, strip, label, tags, ticket, tiles, line) {
    var r = c.r, m = c.m;
    var sec = el('section', 'jd-hero');
    add(sec, strip);
    var body = add(sec, el('div', 'body'));
    var who = add(body, el('div', 'who'));
    var title = add(who, el('div', 'title'));
    add(title, el('h1', null, label));
    if ('recipient' in r) add(title, bot(r.recipient));
    LINES[line].chip(title, m);
    tags(title);
    if (GH.on && ghRefs(r)) add(who, ghPanel(r, ticket));
    LINES[line].body(who, r, m, c.motion);
    var box = add(body, el('div', 'jd-tiles'));
    tiles.forEach(function (name) { TILES[name](box, r, m); });
    return sec;
  }
  function ghBuild() {
    var p = {root: el('div', 'jd-gh'), head: null, busy: false, sent: false};
    p.auth = add(p.root, el('div', 'gh-row'));
    p.form = add(p.root, el('div', 'gh-form'));
    var row = add(p.form, el('div', 'gh-row'));
    p.target = add(row, el('select'));
    p.target.setAttribute('aria-label', 'Comment on');
    p.post = ghButton(row, 'Post', function () { ghComment(p); });
    p.text = add(p.form, el('textarea'));
    p.text.setAttribute('aria-label', 'Comment');
    p.out = add(p.form, el('div', 'gh-out'));
    p.ok = add(p.root, el('details', 'gh-okbox'));
    add(p.ok, el('summary', null, 'Give the hold OK'));
    var okRow = add(p.ok, el('div', 'gh-row'));
    p.okButton = ghButton(okRow, 'OK', function () { ghOk(p); }, 'gh-ok');
    p.okOut = add(okRow, el('span', 'gh-out'));
    p.ok.addEventListener('toggle', function () { if (p.ok.open) ghHead(p); });
    return p;
  }
  function ghDraw(p) {
    var signedIn = !!GH.login;
    ghAuth(p.auth);
    var refs = ghRefs(p.r);
    var options = refs ? [['issue', refs.issue, 'issue #'], ['pull', refs.pr, 'PR #']].filter(function (o) { return o[1]; }) : [];
    var want = options.map(function (o) { return o[0] + ':' + o[1]; }).join(',');
    if (p.options !== want) {
      var keep = p.target.value;
      p.target.replaceChildren();
      options.forEach(function (o) {
        var opt = add(p.target, el('option', null, o[2] + o[1]));
        opt.value = o[0] + ':' + o[1];
      });
      if (want.split(',').indexOf(keep) >= 0) p.target.value = keep;
      p.options = want;
    }
    p.form.hidden = !signedIn || !options.length;
    var t = p.t;
    var eligible = signedIn && !!t && t.state === 'awaiting-ok' && !!(refs && refs.pr) &&
      String(t.waiting_on || '').toLowerCase() === GH.login.toLowerCase();
    p.ok.hidden = !eligible;
    if (!eligible) { p.ok.open = false; p.head = null; p.okOut.textContent = ''; }
    p.okButton.disabled = !p.head || p.busy;
    p.okButton.textContent = p.sent ? 'OK sent' : p.head ? 'OK ' + p.head : 'OK (reading head…)';
  }
  function ghPanel(r, t) {
    var key = threadKey(r);
    var p = GH.panels[key] || (GH.panels[key] = ghBuild());
    p.r = r;
    p.t = t;
    ghDraw(p);
    return p.root;
  }
  function ghComment(p) {
    var refs = ghRefs(p.r);
    var pick = String(p.target.value).split(':');
    var body = p.text.value;
    if (!refs || pick.length !== 2 || !/^[0-9]+$/.test(pick[1]) || !body.trim()) return;
    p.post.disabled = true;
    ghPost('/api/comment', {repo: refs.repo, kind: pick[0], number: Number(pick[1]), body: body}, true).then(function (res) {
      p.post.disabled = false;
      if (res.ok) p.text.value = '';
      ghResult(p.out, res, 'posted');
    });
  }
  function ghHead(p) {
    var refs = ghRefs(p.r);
    if (!refs || !refs.pr || p.busy) return;
    p.busy = true;
    p.head = null;
    p.sent = false;
    ghDraw(p);
    ghPost('/api/head', {repo: refs.repo, pr: Number(refs.pr)}, true).then(function (res) {
      p.busy = false;
      var head = String(res.data.head || '');
      if (res.ok && SHA.test(head)) p.head = head;
      else p.okOut.textContent = 'error: ' + (res.error || 'bad-head');
      ghDraw(p);
    });
  }
  function ghOk(p) {
    var refs = ghRefs(p.r);
    var head = p.head;
    if (!refs || !refs.pr || !head || p.busy) return;
    p.busy = true;
    ghDraw(p);
    ghPost('/api/ok', {repo: refs.repo, pr: Number(refs.pr), head: head}, true).then(function (res) {
      p.busy = false;
      if (res.status === 409 && res.data.error === 'head-changed') {
        p.okOut.textContent = 'head changed to ' + String(res.data.head || '');
        ghHead(p);
        return;
      }
      if (res.ok) { p.head = null; p.sent = true; }
      ghResult(p.okOut, res, 'OK posted');
      ghDraw(p);
    });
  }
  function jdPanel(cls, slot, title, count, id) {
    var sec = el('section', 'jd-panel' + (cls ? ' ' + cls : ''));
    if (id) sec.setAttribute('id', id);
    sec.setAttribute('data-sec', slot);
    var h = add(sec, el('h2'));
    span(h, 't', title);
    span(h, 'count', count);
    return sec;
  }
  function keepOpen(box, key) {
    box.setAttribute('data-key', key);
    box.open = !!openDetails[key];
    box.addEventListener('toggle', function () { openDetails[key] = box.open; });
  }
  function runlog(key, m, buttons) {
    var sec = el('details', 'jd-panel jd-runlog');
    sec.setAttribute('data-sec', 'runlog');
    keepOpen(sec, 'runlog:' + key);
    var h = add(add(sec, el('summary')), el('h2'));
    span(h, 't', 'Run log');
    span(h, 'count', m.visits.length + ' visits · oldest first');
    if (!m.visits.length) add(sec, el('p', 'empty', 'no visits recorded'));
    else {
      var ol = add(sec, el('ol', 'runlog'));
      m.visits.forEach(function (v) {
        var tint = whoTint(v.who);
        var tag = v.current ? (m.ended === 'shipped' ? 'shipped' : 'now') : '';
        var btn = add(add(ol, el('li')), el('button', ['visit', tag, v.stop ? 'stop' : ''].filter(Boolean).join(' ')));
        buttons.push(btn);
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-visit', String(v.index));
        btn.setAttribute('data-state', v.state);
        btn.setAttribute('aria-pressed', v.index === m.selected ? 'true' : 'false');
        btn.setAttribute('style', '--m:' + tint);
        span(btn, 'num', pad(v.index + 1));
        var mk = span(btn, 'mark' + (v.off_chart ? ' off' : ''));
        mk.setAttribute('aria-hidden', 'true');
        mk.setAttribute('style', '--m:' + (v.stop ? DETAIL.stop : DETAIL.endTint[tag] || tint));
        var main = span(btn, 'main');
        var top = span(main, 'top');
        span(top, 'name', v.state + (tag ? ' · ' + tag : ''));
        span(top, 'who', v.who);
        var d = span(top, 'dur');
        if (tag === 'shipped') d.textContent = shippedDur(v);
        else if (v.current) nowDur(m, d);
        else d.textContent = v.duration_seconds === null || v.duration_seconds === undefined ? '—' : dur(v.duration_seconds);
        if (v.off_chart) span(top, 'offchip', 'off-chart');
        span(main, 'note', reasonOf(v));
        var jobs = v.jobs || [];
        if (!jobs.length) return;
        var line = add(btn.parentNode, el('div', 'exp-jobs'));
        jobs.forEach(function (j) {
          var item = add(line, el('span', 'exp-job'));
          add(item, el('span', 'job', j.job));
          add(item, el('span', 'act', j.action));
          if (j.exposure.round !== 'unknown') add(item, el('span', 'rnd', 'round ' + j.exposure.round));
          add(item, el('span', 'model', modelEffort(j.exposure) || 'unknown'));
          if (j.suite_wait_seconds !== null && j.suite_wait_seconds !== undefined) add(item, el('span', 'wait', 'suite wait ' + j.suite_wait_seconds + 's'));
        });
        var more = add(btn.parentNode, el('details', 'exp-more'));
        keepOpen(more, 'runlog-jobs:' + key + ':' + v.index);
        add(more, el('summary', null, jobs.length + ' job' + (jobs.length === 1 ? '' : 's') + ' · metadata'));
        jobs.forEach(function (j) {
          var table = add(more, el('table', 'exp'));
          add(table, el('caption', null, j.job + ' · ' + j.action));
          exposureRows(add(table, el('tbody')), j.exposure);
        });
      });
    }
    add(sec, el('p', 'jd-foot', 'Pick a step here or on the chart; both follow.'));
    return sec;
  }
  var LIVE = {fields: ['current_task', 'last_finished', 'plan', 'subagents'],
              planCls: {done: 'done', in_progress: 'now', pending: 'pending'},
              planTag: {done: 'done', in_progress: 'now', pending: ''}};
  function liveMode(t) {
    if (!t) return 'none';
    return LIVE.fields.some(function (k) { return t[k] !== null && t[k] !== undefined; }) ? 'live' : 'counters';
  }
  function compact(n) {
    var tenths;
    if (n < 1000) return String(n);
    if (n < 10000) { tenths = Math.floor((n * 10 + 500) / 1000); return Math.floor(tenths / 10) + '.' + (tenths % 10) + 'k'; }
    if (n < 1000000) return Math.floor((n + 500) / 1000) + 'k';
    tenths = Math.floor((n * 10 + 500000) / 1000000);
    return Math.floor(tenths / 10) + '.' + (tenths % 10) + 'M';
  }
  function dollars(c) {
    var cents = Math.floor(c * 100 + 0.5);
    return '$' + Math.floor(cents / 100) + '.' + pad(cents % 100);
  }
  function lrTileRow(tiles) {
    var box = el('div', 'lr-tiles');
    tiles.forEach(function (s) {
      var tile = add(box, el('div', 'lr-tile'));
      add(tile, el('b', null, s[0]));
      span(tile, null, s[1]);
    });
    return box;
  }
  function lrTiles(t) {
    var known = t.tokens_known !== false;
    var inLabel = 'tokens in';
    if (known && t.tokens_in) inLabel += ' · ' + Math.floor((200 * t.tokens_cached + t.tokens_in) / (2 * t.tokens_in)) + '% cached';
    return lrTileRow([[known ? compact(t.tokens_in) : 'no data', inLabel],
                      [known ? compact(t.tokens_out) : 'no data', 'tokens out'],
                      [t.cost_usd === null || t.cost_usd === undefined ? 'no data' : dollars(t.cost_usd), 'cost so far'],
                      [t.files_touched + ' files · ' + t.commands + ' commands', 'touched and run']]);
  }
  function lrHead(r, t, moving) {
    var finished = !!t.finished, task = t.current_task, last = t.last_finished;
    var head = el('div', 'lr-head');
    var spin = span(head, 'lr-spin');
    spin.setAttribute('aria-hidden', 'true');
    add(spin, PX.strip('live-head-' + r.job, {effect: moving && task && !finished ? 'spin3' : 'off', tint: 'bot', bot: r.recipient,
                                              seed: 71, still: !moving, cell: 4, done: finished, height: 12}));
    if (finished) {
      span(head, 'lr-label', 'finished');
      span(head, last ? 'lr-task ok' : 'lr-task muted', last || 'nothing recorded');
    } else {
      span(head, 'lr-label', 'working on');
      span(head, task ? 'lr-task' : 'lr-task muted', task || 'no task in progress');
    }
    var lastEl = span(head, 'lr-last', 'last finished: ');
    span(lastEl, null, last || 'nothing yet');
    return head;
  }
  function lrCol(title) {
    var col = el('div', 'lr-col');
    var head = add(col, el('div', 'lr-colhead'));
    span(head, 'lr-label', title);
    return {col: col, head: head};
  }
  function lrPlan(r, plan, moving) {
    var c = lrCol('Its plan');
    if (!plan.length) { add(c.col, el('p', 'empty', 'no plan yet')); return c.col; }
    var done = plan.filter(function (p) { return p.status === 'done'; }).length;
    span(c.head, 'lr-count', done + ' of ' + plan.length + ' done');
    add(c.col, PX.strip('live-plan-' + r.job, {effect: 'fill', tint: 'bot', bot: r.recipient, progress: done / plan.length,
                                               seed: 72, still: !moving, cell: 4, done: done === plan.length, height: 8}));
    var ol = add(c.col, el('ol', 'lr-plan'));
    plan.forEach(function (p) {
      var li = add(ol, el('li', LIVE.planCls[p.status]));
      span(li, 'lr-mark').setAttribute('aria-hidden', 'true');
      span(li, 'lr-text', p.text);
      span(li, 'lr-tag', LIVE.planTag[p.status]);
    });
    return c.col;
  }
  function lrAgents(r, agents, moving) {
    var c = lrCol('Sub-agents');
    if (!agents) { add(c.col, el('p', 'empty', 'no sub-agent data')); return c.col; }
    var running = agents.filter(function (a) { return a.status === 'running'; }).length;
    span(c.head, 'lr-count', agents.length + ' spawned · ' + running + ' running');
    if (!agents.length) { add(c.col, el('div', 'lr-alone', 'working alone so far')); return c.col; }
    agents.forEach(function (a, i) {
      var card = add(c.col, el('div', 'lr-agent'));
      var effect = {running: moving ? 'scan' : 'off', finished: 'off', failed: 'static'}[a.status];
      add(card, PX.strip('live-agent-' + r.job + '-' + i, {effect: effect, tint: a.status === 'failed' ? '#f7768e' : 'bot',
                                                           bot: r.recipient, seed: 80 + i, still: !moving, cell: 4, done: a.status === 'finished', height: 8}));
      var body = add(card, el('div', 'lr-agent-body'));
      span(body, 'lr-kind', a.kind);
      span(body, 'lr-desc', a.description);
      span(body, 'lr-status ' + a.status, a.status);
    });
    return c.col;
  }
  function liveRun(r, queued) {
    if (queued) return el('p', 'empty', 'not started yet');
    var t = r.telemetry;
    var mode = liveMode(t);
    if (mode === 'none') return el('p', 'empty', 'no telemetry');
    var box = el('div', 'lr');
    if (PX.table.bots.hasOwnProperty(r.recipient)) box.setAttribute('style', '--lr:var(--' + r.recipient + ')');
    if (mode === 'live') {
      var moving = PX.table.actions.hasOwnProperty(PX.state(r));
      add(box, lrHead(r, t, moving));
      var cols = add(box, el('div', 'lr-cols'));
      if (t.plan !== null && t.plan !== undefined) add(cols, lrPlan(r, t.plan, moving));
      add(cols, lrAgents(r, t.subagents, moving));
    }
    add(box, lrTiles(t));
    return box;
  }
  function liverun(r, started) {
    var sec = jdPanel(null, 'liverun', 'Live run', r.recipient + ' · job ' + r.job, 'liverun-' + anchor(r.job));
    add(sec, liveRun(r, !started));
    return sec;
  }
  function timeByWho(m) {
    var rows = new Map(), end = m.end.at;
    m.visits.forEach(function (v) {
      var took = v.duration_seconds, start = v.started_at;
      if (known(took) && known(end) && known(start)) took = start < end ? Math.floor(Math.min(took, end - start)) : null;
      if (!known(took)) return;
      var who = String(v.who);
      if (!rows.has(who)) rows.set(who, {seconds: 0, states: new Map()});
      var row = rows.get(who);
      row.seconds += took;
      row.states.set(v.state, (row.states.get(v.state) || 0) + 1);
    });
    return Array.from(rows, function (e) {
      var states = Array.from(e[1].states, function (s) { return s[1] === 1 ? s[0] : s[1] + '× ' + s[0]; });
      return [e[0], e[1].seconds, states.join(', ')];
    }).sort(function (a, b) { return b[1] - a[1]; });
  }
  function timeByWhoBlock(m) {
    var rows = timeByWho(m);
    var group = el('div', 'sum-who');
    var head = add(group, el('div', 'lr-colhead'));
    span(head, 'lr-label', 'Time by who');
    span(head, 'lr-count', reading(rows.length ? rows.reduce(function (a, w) { return a + w[1]; }, 0) : null, dur));
    if (!rows.length) return group;
    var bar = add(group, el('div', 'sum-bar'));
    bar.setAttribute('aria-hidden', 'true');
    var key = add(group, el('div', 'sum-key'));
    rows.forEach(function (w) {
      var tint = whoTint(w[0]);
      span(bar, null).setAttribute('style', 'flex:' + w[1] + ' 1 0;background:' + tint);
      var item = span(key, null);
      add(item, el('i')).setAttribute('style', 'background:' + tint);
      add(item, el('b', null, w[0])).setAttribute('style', 'color:' + tint);
      item.appendChild(document.createTextNode(' ' + dur(w[1]) + ' · ' + w[2]));
    });
    return group;
  }
  function summaryPanel(m) {
    var end = m.end, start = intakeAt(m);
    var run = [start === null ? '' : 'intake ' + etClock(start),
               known(end.at) ? howOf(m)[1] + ' ' + etClock(end.at) : ''].filter(Boolean).join(' to ');
    var sec = jdPanel('jd-sum', 'summary', 'Run summary', ['the whole run', run].filter(Boolean).join(' · '));
    var box = add(sec, el('div', 'lr'));
    add(box, lrTileRow([[reading(end.jobs, String), 'jobs'], [reading(end.tokens_in, compact), 'tokens in'],
                        [reading(end.tokens_out, compact), 'tokens out'], [reading(end.cost_usd, dollars), 'cost']]));
    add(box, timeByWhoBlock(m));
    add(box, el('p', 'sum-foot', 'Totals over every job this ticket ran. Nothing is running now.'));
    return sec;
  }
  function endedPanel(m) {
    var at = m.end.at, word = howOf(m)[0];
    var sec = jdPanel('jd-end', 'ended', 'Ended', 'end state · nothing comes next');
    var row = add(sec, el('div', 'jd-ended'));
    span(row, 'mark').setAttribute('aria-hidden', 'true');
    span(row, 'how', word);
    if (!known(at)) { span(row, 'at', 'no reading'); return sec; }
    span(row, 'at', word + ' at ' + etClock(at));
    ticking(span(row, 'ago'), m.now - at, '', ' ago');
    return sec;
  }
  function whyPanel(m) {
    var end = m.end, facts = stopFacts(m), age = stopAge(m);
    var where = end.stopped_in ? 'in ' + end.stopped_in : '';
    var sec = el('section', 'jd-panel jd-why');
    var h = add(sec, el('h2'));
    span(h, 't', 'Why it stopped');
    var count = span(h, 'count', where);
    if (age !== null) ticking(count, age, 'stopped ', ' ago' + (where ? ' · ' + where : ''));
    var body = add(sec, el('div', 'why-body'));
    var left = add(body, el('div', 'why-col'));
    span(left, 'label', 'Reason');
    add(left, el('div', 'why-reason', end.reason || 'no reason recorded'));
    var tbody = add(add(left, el('table', 'why-facts')), el('tbody'));
    function fact(name) {
      var tr = add(tbody, el('tr'));
      add(tr, el('th', null, name)).setAttribute('scope', 'row');
      return add(tr, el('td'));
    }
    var when = fact('when');
    if (age === null) when.textContent = 'no reading';
    else ticking(when, age, etDay(end.at) + ' · ', ' ago');
    var path = facts.path.length ? hopsText(facts.hops_before) + ' in (' + facts.path.join(' → ') + ')' : '';
    fact('where').textContent = [where, path].filter(Boolean).join(', ') || 'no reading';
    fact('job').textContent = 'none running';
    var right = add(body, el('div', 'why-col'));
    var ways = m.ways_out;
    span(right, 'label', 'Ways out · ' + ways.length + ', from the chart');
    if (!ways.length) add(right, el('p', 'empty', 'no way out on the chart'));
    else {
      var grid = add(right, el('div', 'jd-next'));
      ways.forEach(function (w) {
        var card = add(grid, el('div', w.end ? 'card end' : 'card'));
        var head = add(card, el('div', 'head'));
        span(head, 'to', w.to);
        if (w.end) span(head, 'tag', 'end state');
        span(card, 'why', w.label);
      });
    }
    add(right, el('p', 'why-ro', DETAIL.readOnly[Math.min(ways.length, DETAIL.readOnly.length - 1)]));
    return sec;
  }
  function exposurePanel(r) {
    var sec = jdPanel('jd-exp', 'exposure', 'Exposure', DETAIL.exposureSub);
    exposureRows(add(add(sec, el('table', 'why-facts')), el('tbody')), r.exposure);
    return sec;
  }
  function exposureRows(tbody, found) {
    DETAIL.exposure.forEach(function (f) {
      var v = found[f[0]];
      var tr = add(tbody, el('tr'));
      add(tr, el('th', null, f[1])).setAttribute('scope', 'row');
      add(tr, el('td', null, typeof v === 'string' ? v : v.join(f[2]) || 'none'));
    });
  }
  function legendItem(parent, cls, tint, text, bg) {
    var s = span(parent, null);
    var i = add(s, el('i', cls || null));
    if (tint) i.setAttribute('style', '--m:' + tint);
    if (bg) i.setAttribute('style', 'background:' + bg);
    s.appendChild(document.createTextNode(text));
    return s;
  }
  function shippedDur(v) { return known(v.started_at) ? 'at ' + etClock(v.started_at) : '—'; }
  function nowDur(m, node) {
    if (m.state_age_seconds === null) node.textContent = 'in progress';
    else ticking(node, m.state_age_seconds, '', ' so far');
    return node;
  }
  function stepName(v, m) {
    var took;
    if (v.current) took = m.state_age_seconds === null ? 'in progress' : dur(m.state_age_seconds) + ' so far';
    else took = v.duration_seconds === null || v.duration_seconds === undefined ? 'time unknown' : dur(v.duration_seconds);
    return 'Step ' + (v.index + 1) + ' of ' + m.visits.length + ': ' + v.state + ', ' + v.who + ', ' + took;
  }
  function legendWho(m) {
    var seen = {};
    m.visits.forEach(function (v) { seen[JOURNEY.tint.hasOwnProperty(v.who) ? v.who : 'script'] = true; });
    return DETAIL.who.filter(function (w) { return seen[w]; });
  }
  function reasonOf(v) {
    return v.reason !== null && v.reason !== undefined ? v.reason : (v.index === 0 ? DETAIL.first : '');
  }
  function fillCaption(cap, m, i) {
    cap.replaceChildren();
    var v = m.visits[i];
    var tag = v && v.current ? m.ended || 'now' : '';
    cap.className = 'jd-cap' + (tag ? ' ' + tag : '');
    if (!v) return;
    if (m.ended) span(cap, 'end', endText(m)).setAttribute('style', '--m:' + DETAIL.endTint[m.ended]);
    span(cap, 'idx', pad(v.index + 1) + ' of ' + m.visits.length);
    span(cap, 'st', v.state + (tag ? ' · ' + tag : ''));
    span(cap, 'who', v.who).setAttribute('style', '--m:' + (JOURNEY.tint[v.who] || JOURNEY.tint.script));
    var d = span(cap, 'dur');
    if (v.current && m.ended === 'shipped') d.textContent = shippedDur(v);
    else if (v.current && m.ended === 'stopped') d.textContent = m.state_age_seconds === null ? '—' : 'for ' + dur(m.state_age_seconds);
    else if (v.current) nowDur(m, d);
    else d.textContent = v.duration_seconds === null || v.duration_seconds === undefined ? '—' : 'took ' + dur(v.duration_seconds);
    span(cap, 'note', reasonOf(v));
  }
  function journeyPanel(r, m, hits, scales, scale) {
    var sec = jdPanel(null, 'journey', 'Journey', '');
    var count = sec.children[0].children[1];
    span(count, 'desk', DETAIL.journeySub);
    span(count, 'tap', DETAIL.journeyTap);
    var box = add(sec, el('div', 'jd-box'));
    var canvas = add(box, el('canvas', 'jd-journey'));
    canvas.setAttribute('width', '1000');
    canvas.setAttribute('height', '336');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'Journey: ' + (m.visits.map(function (v) { return v.state; }).join(' → ') || 'no visits') +
      (m.ended ? '; ended: ' + endText(m) : '; now ' + (m.state || 'unknown')));
    var ol = add(box, el('ol', 'journey'));
    m.visits.forEach(function (v) {
      var li = add(ol, el('li'));
      var b = add(li, el('button'));
      hits.push(b);
      b.setAttribute('type', 'button');
      b.setAttribute('data-visit', String(v.index));
      b.setAttribute('data-state', v.state);
      b.setAttribute('aria-label', stepName(v, m));
      b.setAttribute('aria-pressed', v.index === m.selected ? 'true' : 'false');
      span(li, 'step', stepName(v, m));
    });
    var cap = add(sec, el('div', 'jd-cap'));
    cap.setAttribute('aria-live', 'polite');
    fillCaption(cap, m, m.selected);
    var ctl = add(sec, el('div', 'jd-ctl'));
    var axis = add(ctl, el('div', 'axis'));
    axis.setAttribute('role', 'group');
    axis.setAttribute('aria-label', 'Time axis');
    function ctlButton(parent, text, attr, val, pressed) {
      var b = add(parent, el('button', null, text));
      b.setAttribute('type', 'button');
      b.setAttribute(attr, val);
      if (pressed !== null) { b.setAttribute('aria-pressed', pressed ? 'true' : 'false'); scales.push(b); }
      return b;
    }
    ctlButton(axis, 'even steps', 'data-scale', 'even', scale === 'even');
    if (m.all_durations_known) ctlButton(axis, 'to scale', 'data-scale', 'scale', scale === 'scale');
    if (m.ended !== 'stopped') ctlButton(ctl, 'replay', 'data-replay', '1', null);
    if (m.ended) span(ctl, 'endnote', endText(m)).setAttribute('style', '--m:' + DETAIL.endTint[m.ended]);
    span(ctl, 'gap');
    legendWho(m).forEach(function (w) { legendItem(ctl, null, JOURNEY.tint[w], w).className = 'key'; });
    legendItem(ctl, null, DETAIL.stop, 'stopped').className = 'key stop';
    legendItem(ctl, 'off', DETAIL.hop, 'off-chart hop').className = 'key hop';
    return {sec: sec, box: box, canvas: canvas, cap: cap};
  }
  function nextPanel(m) {
    var sub = m.terminal || !m.state ? 'no way out' : m.next.length + ' ways out of ' + m.state;
    var sec = jdPanel(null, 'next', 'Can come next', sub);
    if (m.terminal) { add(sec, el('p', 'empty', 'no way out: this ticket is finished')); return sec; }
    if (!m.state) { add(sec, el('p', 'empty', 'state not recorded')); return sec; }
    var grid = add(sec, el('div', 'jd-next'));
    m.next.forEach(function (n, i) {
      var tag = '';
      if (n.kind === 'stop') tag = n.count ? 'stop state · been here ' + (n.count === 1 ? 'once' : n.count + ' times') : 'stop state · not yet';
      else if (n.kind === 'human') tag = 'needs a human';
      else if (i === 0) tag = 'the normal way on';
      var card = add(grid, el('div', 'card ' + n.kind));
      var head = add(card, el('div', 'head'));
      span(head, 'to', n.to);
      if (tag) span(head, 'tag', tag);
      span(card, 'why', n.label);
    });
    return sec;
  }
  function chartPanel(id, m, chart) {
    var box = el('details', 'jd-chart');
    box.setAttribute('data-sec', 'chart');
    var key = 'full-chart:' + id;
    if (!chart) { add(box, el('summary', null, 'Full state chart')); add(box, el('p', 'empty', 'statechart unavailable')); return {sec: box}; }
    box.setAttribute('data-key', key);
    box.open = !!openDetails[key];
    var sum = add(box, el('summary', null, 'Full state chart '));
    span(sum, 'count', DETAIL.chartSub.replace('{n}', String(chart.states.length)));
    var wrap = add(box, el('div', 'jd-box'));
    var canvas = add(wrap, el('canvas', 'jd-flowmap'));
    canvas.setAttribute('width', '1000');
    canvas.setAttribute('height', String(FLOWMAP ? FLOWMAP.grid.h * FLOWMAP.grid.cell : 416));
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'Full state chart; path: ' + m.visits.map(function (v) { return v.state; }).join(' → '));
    var legend = add(box, el('div', 'jd-legend'));
    legendWho(m).forEach(function (w) { legendItem(legend, 'line', null, 'path taken · ' + w, JOURNEY.tint[w]); });
    legendItem(legend, 'line', null, 'visited', '#5386a6');
    legendItem(legend, 'off', JOURNEY.tint.script, 'off-chart jump');
    DETAIL.chartKeys[m.ended || 'live'].forEach(function (k) { legendItem(legend, k[0], null, k[2], k[1]); });
    var auto = 0;
    function place() {
      if (!wrap.clientWidth) return;
      auto = chartLeft(m, wrap);
      wrap.scrollLeft = key in chartScroll ? chartScroll[key] : auto;
    }
    wrap.addEventListener('scroll', function () {
      if (Math.abs(wrap.scrollLeft - auto) < 1) delete chartScroll[key];
      else chartScroll[key] = wrap.scrollLeft;
    });
    box.addEventListener('toggle', function () {
      openDetails[key] = box.open;
      if (box.open) place();
      else delete chartScroll[key];
    });
    if (typeof ResizeObserver === 'function') new ResizeObserver(place).observe(wrap);
    return {sec: box, canvas: canvas};
  }
  function chartLeft(m, wrap) {
    var state = flowModel(m).state;
    var node = FLOWMAP && FLOWMAP.nodes.filter(function (n) { return n.name === state; })[0];
    if (!node) return 0;
    var centre = (node.x + FLOWMAP.node.w / 2) * FLOWMAP.grid.cell;
    return Math.round(Math.max(0, Math.min(centre - wrap.clientWidth / 2, wrap.scrollWidth - wrap.clientWidth)));
  }

  function selection(key, r, m) {
    var s = SEL[key];
    var beatAt = typeof r.heartbeat_at === 'number' ? r.heartbeat_at : null;
    if (!s) {
      s = SEL[key] = {selected: m.selected, picked: false, tPlay: Date.now(), beatAt: beatAt, beatStart: null,
                        flashAt: null, scale: 'even'};
      return s;
    }
    if (!s.picked || s.selected >= m.visits.length) { s.selected = m.selected; s.picked = false; }
    if (beatAt !== null && beatAt !== s.beatAt) s.beatStart = Date.now();
    s.beatAt = beatAt;
    if (s.scale === 'scale' && !m.all_durations_known) s.scale = 'even';
    return s;
  }
  function pick(j, i, fromChart) {
    var s = SEL[j.key];
    s.selected = i;
    s.picked = true;
    s.flashAt = Date.now();
    [j.logButtons, j.hitButtons].forEach(function (buttons) {
      buttons.forEach(function (b) {
        var on = Number(b.getAttribute('data-visit')) === i;
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (on && fromChart && !j.g.compact && buttons === j.logButtons && b.scrollIntoView) b.scrollIntoView({block: 'nearest'});
      });
    });
    fillCaption(j.cap, j.m, i);
    drawOne(j, Date.now());
  }
  function fitJourney(j) {
    var g = phoneQuery && phoneQuery.matches ? JOURNEY_GEOM.compact((j.box.clientWidth || 327) / 4) : JOURNEY_GEOM.wide;
    if (g === j.g) return;
    j.g = g;
    j.canvas.width = g.w * 4;
    j.canvas.height = g.h * 4;
    placeHits(j);
    drawOne(j, Date.now());
  }
  function fitJourneys() {
    journeys.forEach(function (j) { if (j.canvas.isConnected) fitJourney(j); });
  }
  function placeHits(j) {
    var bars = journeyBars(j.m, SEL[j.key].scale, j.g);
    var buttons = j.hitButtons;
    for (var k = 0; k < buttons.length && k < bars.length; k++) {
      var b = bars[k], st = buttons[k].style;
      st.left = b.x + 'px'; st.top = b.y + 'px'; st.width = Math.max(4, b.w) + 'px'; st.height = Math.max(4, b.h) + 'px';
    }
  }
  function drawOne(j, now) {
    var s = SEL[j.key];
    if (j.canvas.getContext) {
      var ctx = j.canvas.getContext('2d');
      if (ctx) {
        var flash = s.flashAt === null || reducedMotion ? 0 : Math.max(0, 1 - (now - s.flashAt) / 260);
        var beat = s.beatStart === null || reducedMotion ? -1 : (now - s.beatStart) / 1000;
        drawJourney(ctx, j.m, {g: j.g, t: reducedMotion ? 1e6 : (now - s.tPlay) / 1000, scale: s.scale, selected: s.selected,
          flash: flash, beat: beat >= 0 && beat <= 1 ? beat : -1, reduced: reducedMotion, still: j.still,
          startClock: j.startClock, nowClock: j.nowClock, endClock: j.endClock});
      }
    }
    if (j.flowCanvas && j.flow.open && j.flowCanvas.getContext && FLOWMAP) {
      var fctx = j.flowCanvas.getContext('2d');
      if (fctx) drawFlowMap(fctx, FLOWMAP, flowModel(j.m), {t: now / 1000, reduced: reducedMotion || j.still || !!j.m.ended,
                                                            tint: j.m.ended === 'shipped' ? DETAIL.endTint.shipped : null});
    }
  }
  function journeyFrame() {
    journeys = journeys.filter(function (j) {
      if (!j.canvas.isConnected && journeyResize) journeyResize.unobserve(j.box);
      return j.canvas.isConnected;
    });
    if (!journeys.length) return false;
    var now = Date.now();
    journeys.forEach(function (j) { drawOne(j, now); });
    return !reducedMotion;
  }

  var SECTIONS = {
    liverun: function (c) { return liverun(c.r, c.started); },
    journey: function (c) { c.jp = journeyPanel(c.r, c.m, c.hitButtons, c.scales, c.s.scale); return c.jp.sec; },
    next: function (c) { return nextPanel(c.m); },
    chart: function (c) { c.cp = chartPanel(c.key, c.m, c.chart); return c.cp.sec; },
    summary: function (c) { return summaryPanel(c.m); },
    ended: function (c) { return endedPanel(c.m); },
    why: function (c) { return whyPanel(c.m); },
    exposure: function (c) { return exposurePanel(c.r); }
  };
  function detailContext(key, r, d, started) {
    var chart = d.statechart || null;
    var m = detailModel(r, chart);
    var s = selection(key, r, m);
    m.selected = s.selected;
    return {key: key, r: r, m: m, s: s, chart: chart, started: started, motion: motionOf(r, m),
            generated: Number(d.generated_at_epoch) || Date.now() / 1000, logButtons: [], hitButtons: [], scales: []};
  }
  function detailCols(art, c, sections) {
    var s = c.s, scales = c.scales, hitButtons = c.hitButtons;
    var cols = add(art, el('div', 'jd-cols'));
    var log = runlog(c.key, c.m, c.logButtons);
    var after = sections.indexOf('next') >= 0 ? 'next' : 'journey';
    sections.forEach(function (name) {
      add(cols, SECTIONS[name](c));
      if (name === after) add(cols, log);
    });
    var jp = c.jp, cp = c.cp, start = firstStart(c.m);
    var endAt = c.m.end ? c.m.end.at : null;
    var j = {key: c.key, r: c.r, m: c.m, still: !c.m.ended && (!!c.still || holdsStill(c.motion)), logButtons: c.logButtons,
             hitButtons: hitButtons, box: jp.box, canvas: jp.canvas, cap: jp.cap, flow: cp.sec, flowCanvas: cp.canvas,
             startClock: start === null ? '' : journeyClock(start), nowClock: journeyClock(c.generated),
             endClock: endAt === null || endAt === undefined ? '' : journeyClock(endAt)};
    art.addEventListener('click', function (ev) {
      var t = ev.target.closest('button');
      if (!t) return;
      if (t.hasAttribute('data-visit')) pick(j, Number(t.getAttribute('data-visit')), hitButtons.indexOf(t) >= 0);
      else if (t.hasAttribute('data-scale')) {
        s.scale = t.getAttribute('data-scale');
        s.tPlay = Date.now();
        scales.forEach(function (b) { b.setAttribute('aria-pressed', b === t ? 'true' : 'false'); });
        placeHits(j);
      } else if (t.hasAttribute('data-replay')) s.tPlay = Date.now();
      else return;
      drawOne(j, Date.now());
    });
    if (cp.canvas) cp.sec.addEventListener('toggle', function () { drawOne(j, Date.now()); });
    fitJourney(j);
    journeys.push(j);
    if (journeyResize) journeyResize.observe(j.box);
    PX.onFrame(journeyFrame);
  }

  function ticketRow(d, r, record) {
    record = record || r.record;
    var same = record != null ? function (t) { return t.record === record; }
      : function (t) { return threadKey(t) === threadKey(r); };
    return (d.tickets || []).filter(same)[0] || null;
  }
  var PICK = {
    running: function (rows) { return rows.reduce(function (a, b) { return (b.elapsed_seconds || 0) < (a.elapsed_seconds || 0) ? b : a; }); },
    queued: function (rows) { return rows[0]; }
  };
  function recordRow(rec, t) {
    var r = {repo: rec.repo, ticket_id: rec.ticket_id, flow: rec};
    DETAIL.rowFromTicket.forEach(function (k) { if (t && k in t) r[k] = t[k]; });
    return r;
  }
  function sameTicket(address) {
    var cut = address.indexOf('#');
    var repo = (cut < 0 ? address : address.slice(0, cut)).toLowerCase(), id = cut < 0 ? '' : address.slice(cut + 1);
    return function (r) { return String(r.repo).toLowerCase() === repo && String(r.ticket_id) === id; };
  }
  function resolveTicket(d, address, records) {
    var keyed = records && Object.prototype.hasOwnProperty.call(records, address);
    var anyCase = sameTicket(address);
    var rules = [function (r) { return r.record === address; }, function (r) { return ticketAddress(r) === address; }, anyCase];
    for (var i = 0; i < rules.length; i++) {
      for (var k = 0; k < DETAIL.liveKinds.length; k++) {
        var kind = DETAIL.liveKinds[k];
        var rows = (d[kind] || []).filter(rules[i]);
        if (!rows.length) continue;
        var row = PICK[kind](rows);
        return {kind: kind, address: rules[i] === anyCase ? ticketAddress(row) : address, row: row, ticket: ticketRow(d, row)};
      }
    }
    if (!records) return null;
    var key = keyed ? address
      : Object.keys(records).sort().filter(function (k) { return anyCase(records[k]); })[0];
    if (key === undefined) return null;
    var rec = records[key], t = ticketRow(d, rec, key);
    var flag = DETAIL.recordFlags.filter(function (f) { return rec[f]; })[0];
    return {kind: flag || 'idle', address: key, row: recordRow(rec, t), ticket: t};
  }
  function ticketArticle(found, d) {
    var spec = DETAIL.kinds[found.kind];
    var r = found.row;
    var c = detailContext(found.address, r, d, spec.started);
    c.still = spec.still;
    c.m.now = c.generated;
    var line = spec.line || 'state';
    var art = el('article', 'jobpage ticketpage');
    art.setAttribute('data-ticket', found.address);
    add(art, crumb(tileLabel(r), LINES[line].crumb(c.m)));
    var strip = PX.strip('ticket-hero-' + found.address, Object.assign({state: c.motion, bot: r.recipient, still: spec.still,
                                                                        cell: 4, height: 40, seed: 2}, spec.strip));
    add(art, hero(c, strip, tileLabel(r), function (title) {
      trackerSpan(title, r, spec.tail);
      troubleChips(title, r);
      ((found.ticket || {}).ok_outcomes || []).forEach(function (o) { add(title, chip('ok: ' + o, 'muted ok')); });
    }, found.ticket, spec.tiles, line));
    (spec.lead || []).forEach(function (name) { add(art, SECTIONS[name](c)); });
    detailCols(art, c, spec.sections);
    return art;
  }
  function ticketView(d, address, records) {
    var found = address === null ? null : resolveTicket(d, address, records);
    if (found) return ticketArticle(found, d);
    var panel = DETAIL.panels[address === null ? 'none' : records && !recordPending(d, address) ? 'missing' : 'loading'];
    var sec = el('section', 'panel');
    add(add(sec, el('h1')), el('span', null, panel[0]));
    var p = add(sec, el('p', 'empty'));
    if (panel[1]) p.appendChild(document.createTextNode(panel[1].replace('{}', function () { return address; })));
    p.appendChild(document.createTextNode('Pick a ticket on the '));
    add(p, link(null, 'job.html')).textContent = 'Tickets board';
    p.appendChild(document.createTextNode('.'));
    return sec;
  }

  function redirectMoved(d) {
    var hash = String(location.hash || '');
    if (hash.indexOf('#job-') !== 0) return;
    var hit = activeJobs(d).filter(function (j) { return '#' + anchor(j.row.job) === hash; })[0];
    if (hit) location.replace(ticketPageHref(hit.row));
  }

  var RECORDS = {tickets: null, epoch: null, asked: null, poll: null, busy: false};
  function recordPending(d, address) {
    return RECORDS.tickets !== null && RECORDS.epoch < d.generated_at_epoch
      && (d.tickets || []).some(sameTicket(address)) && !resolveTicket(d, address, RECORDS.tickets);
  }
  function loadRecords(d, address) {
    var epoch = d.generated_at_epoch;
    var retry = address !== null && RECORDS.poll !== accepted && recordPending(d, address);
    if (RECORDS.busy || (!retry && (RECORDS.asked === epoch || (RECORDS.tickets && RECORDS.epoch >= epoch)))) return;
    RECORDS.busy = true;
    RECORDS.asked = epoch;
    RECORDS.poll = accepted;
    fetch('tickets.json', {cache: 'no-store', credentials: 'same-origin'}).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      RECORDS.busy = false;
      if (!data || !data.tickets || typeof data.tickets !== 'object') return;
      RECORDS.tickets = data.tickets;
      RECORDS.epoch = data.generated_at_epoch;
      if (latest) render(latest);
    }, function () { RECORDS.busy = false; });
  }
  function renderTicketView(d) {
    if (!GH.started) { GH.started = true; ghStart(); }
    var address = hashAddress();
    loadRecords(d, address);
    tickers.push({run: function () { loadRecords(latest, hashAddress()); }});
    var view = byId('ticketview');
    view.replaceChildren(ticketView(d, address, RECORDS.tickets));
    var found = address === null ? null : resolveTicket(d, address, RECORDS.tickets);
    if (found) {
      renderTicketCockpit(view, d, found);
      ciExtras(view, d, found.address, found.row, hashParam('ci') === '1');
    }
  }
