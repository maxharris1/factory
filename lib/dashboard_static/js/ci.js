
  var CI_RUN = {in_progress: ['running', 'accent'], queued: ['queued', 'warn']};
  var CI_JOB = {in_progress: ['running', 'accent'], queued: ['queued', 'warn']};
  var CI_DONE = {success: ['passed', 'ok'], failure: ['failed', 'bad'], timed_out: ['timed out', 'bad'],
                 cancelled: ['cancelled', 'bad'], action_required: ['action required', 'bad'],
                 startup_failure: ['startup failure', 'bad'], stale: ['stale', 'bad']};
  var CI_RUNNER = {'github-hosted': 'GitHub-hosted', none: 'no runner'};
  function ciLook(j) {
    if (CI_JOB[j.status]) return CI_JOB[j.status];
    if (CI_DONE[j.conclusion]) return CI_DONE[j.conclusion];
    return [j.conclusion ? String(j.conclusion).replace(/_/g, ' ') : 'done', 'muted'];
  }
  function ciRepo(r) { var repo = String(r.repo); return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) ? repo : null; }
  function ciLink(parent, cls, repo, path, text) {
    if (!repo) return;
    var a = add(parent, link(cls, 'https://github.com/' + repo + '/' + path));
    a.setAttribute('rel', 'noopener noreferrer');
    a.setAttribute('target', '_blank');
    a.textContent = text;
  }
  function ciLog(parent, repo, x) {
    if (x.run_id && x.job_id) ciLink(parent, 'ci-a log', repo, 'actions/runs/' + x.run_id + '/job/' + x.job_id, 'log ↗');
  }
  function ciRunner(j) {
    if (!j.started_at && j.status !== 'completed') return 'waiting for runner';
    if (j.runner_kind === 'self-hosted') return 'self-hosted' + (j.runner_name ? ' · ' + j.runner_name : '');
    return CI_RUNNER[j.runner_kind] || 'runner unknown';
  }
  function ciJob(repo, j) {
    var look = ciLook(j);
    var li = el('li', 'ci-job' + (j.status === 'in_progress' ? ' live' : ''));
    add(li, el('span', 'ci-dot ' + look[1])).setAttribute('aria-hidden', 'true');
    add(li, el('span', 'ci-name', j.name));
    add(li, chip(look[0], look[1]));
    var t = add(li, el('span', 'ci-t'));
    if (j.status === 'in_progress') ticking(t, j.elapsed_seconds);
    else t.textContent = dur(j.elapsed_seconds);
    var runner = add(li, el('span', 'ci-runner'));
    var who = ciRunner(j);
    if (j.queue_seconds === null || j.queue_seconds === undefined) runner.textContent = who;
    else if (!j.started_at && j.status !== 'completed') ticking(runner, j.queue_seconds, who + ' · queued ');
    else runner.textContent = who + ' · queued ' + dur(j.queue_seconds);
    ciLog(li, repo, j);
    return li;
  }
  function ciFail(f) {
    var repo = ciRepo(f);
    var look = CI_DONE[f.conclusion] || ['failed', 'bad'];
    var li = el('li');
    add(li, chip(look[0], look[1]));
    add(li, link('ticket', ticketPageHref(f))).textContent = tileLabel(f);
    add(li, el('span', 'what', f.name));
    if (f.head_short) ciLink(li, 'ci-a', repo, 'commit/' + f.head_short, f.head_short);
    ciLog(li, repo, f);
    ticking(add(li, el('span', 'when')), f.age_seconds, '', ' ago');
    if (f.tests.length) {
      var tests = add(li, el('ul', 'ci-tests'));
      f.tests.forEach(function (name) {
        var item = add(tests, el('li'));
        if (repo) ciLink(item, null, repo, 'issues?q=' + encodeURIComponent('is:issue "' + name + '"'), name);
        else item.textContent = name;
      });
      if (f.more_tests) add(tests, el('li', 'more', '+' + f.more_tests + ' more'));
    }
    return li;
  }
  var CI = __CI__;
  function ciVerb(l2, name, seed) {
    var spec = LANES.tiles[name];
    var verb;
    if (spec.verbs) {
      var glyph = add(l2, el('span', 'glyph'));
      glyph.setAttribute('aria-hidden', 'true');
      var cells = [];
      for (var q = 0; q < 4; q++) cells.push(add(glyph, el('i', q === LANES.glyph[0] ? 'on' : null)));
      verb = add(l2, el('span', 'verb ' + spec.tone));
      verb.setAttribute('role', 'img');
      letters(verb, verbAt(name, 0, seed));
      tileAnims.push({state: name, seed: seed, cells: cells, verb: verb});
    } else {
      verb = add(l2, el('span', 'verb' + (spec.tone ? ' ' + spec.tone : '')));
      verb.setAttribute('role', 'img');
      letters(verb, spec.text);
    }
  }
  function ciCardEl(key, c, seed) {
    var repo = ciRepo(c);
    var look = CI.strip[c.status];
    var card = el('div', 'tile ci ' + c.status + (c.status === 'failed' ? ' bad' : ''));
    card.setAttribute('data-ci', key);
    add(card, link('cover', ticketPageHref(c, true))).setAttribute('aria-label', 'CI for ' + tileLabel(c));
    add(card, PX.strip('ci-' + key, {effect: look[0], tint: look[1], seed: seed, height: CI.strip_h}));
    var bodyEl = add(card, el('div', 'tile-body'));
    var l1 = add(bodyEl, el('div', 'tile-l1'));
    add(l1, el('span', 'ticket', tileLabel(c)));
    if (c.pr) ciLink(l1, 'ci-a', repo, 'pull/' + c.pr, 'PR #' + c.pr + ' ↗');
    if (c.head_short) ciLink(l1, 'ci-a', repo, 'commit/' + c.head_short, c.head_short);
    var l2 = add(bodyEl, el('div', 'tile-l2'));
    ciVerb(l2, 'ci-' + c.status, seed);
    var w = c.wait;
    if (w) {
      if (w.step) add(l2, el('span', 'action', 'then ' + w.step));
      if (w.round !== null && w.round !== undefined) add(l2, el('span', 'round', 'round ' + w.round));
      ticking(add(l2, el('span', 't')), w.waiting_seconds, 'waiting ');
    } else if (c.age_seconds !== null && c.age_seconds !== undefined) {
      ticking(add(l2, el('span', 't')), c.age_seconds, CI.since[c.status], ' ago');
    }
    var l3 = add(bodyEl, el('div', 'tile-l3'));
    var p = c.progress;
    if (p.total) {
      var bar = add(l3, el('div', 'cibar'));
      bar.setAttribute('aria-hidden', 'true');
      CI.segments.forEach(function (n) { if (p[n]) add(bar, el('i', n)).setAttribute('style', 'flex:' + p[n]); });
      add(l3, el('span', 'cibt', c.progress_text));
    } else add(l3, el('span', 'what', w ? checksNamed(w) : 'no checks yet'));
    if (c.pr) ciLink(l3, 'ci-a', repo, 'pull/' + c.pr + '/checks', 'checks');
    return mark('lanes', 'ci-' + key, card);
  }
  function ciSlotEl(s, lane, seed) {
    var box = el('div', 'ci-slot');
    box.setAttribute('data-runner', s.runner);
    add(box, el('span', 'ci-rn', s.runner));
    var tile;
    if (s.state === 'lead') add(box, ciCardEl(s.card, lane.cards[s.card], seed));
    else if (s.state === 'cont') {
      tile = add(box, el('div', 'tile empty ci-cont'));
      add(tile, PX.strip('ci-' + s.runner, {effect: 'static', tint: CI.tint, seed: seed, height: CI.strip_h}));
      add(tile, el('span', 'lbl', '↳ ' + tileLabel(lane.cards[s.card]) + ' · ' + s.job.name));
    } else if (s.state === 'busy') {
      tile = add(box, el('div', 'tile empty px-held'));
      add(tile, PX.strip('ci-' + s.runner, {state: 'held', seed: seed, height: CI.strip_h}));
      add(tile, el('div', 'lbl', 'busy'));
    } else add(box, idleTile('ci-' + s.runner, s.state === 'offline', seed, CI.strip_h));
    return box;
  }
  function ciPoolText(pool) {
    if (pool.state === 'live') return 'pool live';
    if (pool.state === 'stale') return 'pool list ' + dur(pool.age_seconds) + ' old';
    return 'pool unread';
  }
  function ciLaneEl(lane) {
    var row = el('div', 'lane');
    row.setAttribute('data-device', 'ci');
    var head = add(row, el('div', 'lane-head'));
    var lh1 = add(head, el('div', 'lh1'));
    add(lh1, el('span', 'name', 'CI'));
    if (lane.failed_24h) add(lh1, chip(lane.failed_24h + ' failed in 24 h', 'bad'));
    if (lane.base_red) add(lh1, chip('base red ' + lane.base_red, 'warn'));
    if (lane.stale) add(lh1, chip('stale ' + lane.stale, 'bad'));
    var n = Math.max(1, lane.slots.length);
    var used = lane.slots.filter(function (s) { return s.state === 'lead' || s.state === 'cont' || s.state === 'busy'; }).length;
    var meter = add(lh1, el('div', 'lane-meter'));
    meter.setAttribute('aria-hidden', 'true');
    meter.setAttribute('style', 'width:' + (12 * n) + 'px');
    add(meter, PX.strip('meter-ci', {effect: 'meter', tint: '#9ece6a', used: used, slots: n, seed: 30, height: 8,
                                     still: !lane.slots.length}));
    var lh2 = add(head, el('div', 'lh2'));
    add(lh2, el('span', 'sub', lane.running + ' running · ' + lane.queued + ' queued'));
    add(lh2, el('span', 'harness', ciPoolText(lane.pool)));
    var bodyEl = add(row, el('div', 'ci-body'));
    var grid = add(bodyEl, el('div', 'tiles'));
    lane.slots.forEach(function (s, i) { add(grid, ciSlotEl(s, lane, i + 1)); });
    var seed = lane.slots.length;
    lane.groups.forEach(function (g) {
      var box = add(bodyEl, el('div', 'ci-group'));
      box.setAttribute('data-group', g.name);
      add(box, el('span', 'igroup', CI.groups[g.name]));
      var tiles = add(box, el('div', 'tiles'));
      g.cards.forEach(function (key) { seed += 1; add(tiles, ciCardEl(key, lane.cards[key], seed)); });
    });
    return row;
  }
  function ciPopRuns(d, address) {
    var ci = d.ci_runs || {runs: [], failures: []};
    var want = String(address).toLowerCase();
    function mine(r) { return ticketAddress(r).toLowerCase() === want; }
    return [ci.runs.filter(mine), ci.failures.filter(mine)];
  }
  function ciPopRun(r) {
    var repo = ciRepo(r);
    var look = CI_RUN[r.status] || ['other', 'muted'];
    var box = el('div', 'ci-run');
    box.setAttribute('data-run', r.head_short || '');
    var head = add(box, el('div', 'ci-head'));
    add(head, chip(look[0], look[1]));
    if (r.head_short) ciLink(head, 'ci-a', repo, 'commit/' + r.head_short, r.head_short);
    if (r.age_seconds !== null && r.age_seconds !== undefined) ticking(add(head, el('span', 'when')), r.age_seconds, 'started ', ' ago');
    var ul = add(box, el('ul', 'ci-jobs'));
    r.jobs.forEach(function (j) { add(ul, ciJob(repo, j)); });
    return box;
  }
  var CI_CLOSE = null;
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && CI_CLOSE && byId('ci-pop')) location.replace(CI_CLOSE);
  });
  function ciPopup(d, address, row) {
    var found = ciPopRuns(d, address);
    var dlg = el('dialog', 'ci-pop');
    dlg.setAttribute('id', 'ci-pop');
    dlg.setAttribute('open', 'open');
    var head = add(dlg, el('div', 'ci-pop-head'));
    add(head, el('span', 'ci-pop-t', 'CI · ' + tileLabel(row)));
    CI_CLOSE = ticketPageHref(row);
    var x = add(head, link('ci-x', CI_CLOSE));
    x.setAttribute('aria-label', 'close');
    x.textContent = '✕';
    if (!found[0].length) add(dlg, el('p', 'empty', 'no CI queued or running'));
    found[0].forEach(function (r) { add(dlg, ciPopRun(r)); });
    add(dlg, el('h3', 'ci-sub', 'Recent failures (24 h)'));
    if (!found[1].length) add(dlg, el('p', 'empty', 'no CI failures in the last 24 h'));
    else {
      var ul = add(dlg, el('ul', 'feed ci-fails'));
      found[1].forEach(function (f) { add(ul, ciFail(f)); });
    }
    return dlg;
  }
  function ciExtras(view, d, address, row, open) {
    var found = ciPopRuns(d, address);
    if (!open && !found[0].length && !found[1].length) return;
    add(view, link('ci-open', ticketPageHref(row, true))).textContent = 'CI';
    if (open) add(view, ciPopup(d, address, row));
  }
