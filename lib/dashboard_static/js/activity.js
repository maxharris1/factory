
  // --- activity page ---------------------------------------------------------

  var ATTENTION = {stuck: 'bad', overdue: 'bad', dead: 'bad', 'needs-attention': 'bad', stale: 'warn', paused: 'warn',
                   down: 'bad', 'no-waiter': 'bad', health: 'bad'};
  var VERDICT = {routed: 'accent', shipped: 'ok', stale: 'muted', rejected: 'warn', stuck: 'bad',
                 blocked: 'bad', waiting: 'warn', unstuck: 'accent', other: 'muted'};
  var TONE_TINT = {warn: '#ff9e64', bad: '#f7768e', muted: '#8b93b8'};
  var DONE_TINT = '#9ece6a';
  var RELAY_FILTERS = [['all', 'all'], ['needs', 'needs me'], ['shipped', 'shipped'], ['wakes', 'grok wakes']];
  var RELAY_NOTE = 'The relay shows the 12 tickets that moved most recently. A stopped ticket stays listed until it moves again.';
  var RELAY_WINDOW = 6;
  var RELAY_PHONE_WINDOW = 4;
  var NEEDS_ME = {waiting: true, stopped: true, stale: true};
  var rlFilter = 'all';
  var rlOpen = {};

  function cmpStep(a, b) {
    return b.at - a.at || (a.type === 'verdict') - (b.type === 'verdict') || a.tie - b.tie;
  }

  function liveRows(d) {
    var running = {}, queued = {};
    d.running.forEach(function (r) {
      var key = threadKey(r);
      if (!running[key] || (r.elapsed_seconds || 0) < (running[key].elapsed_seconds || 0)) running[key] = r;
    });
    d.queued.forEach(function (r) { if (!queued[threadKey(r)]) queued[threadKey(r)] = r; });
    return {running: running, queued: queued};
  }

  var NOW_TITLE = 'where the ticket is now';

  function nowBox(parent, cls) {
    var box = add(parent, el('span', 'rl-now' + (cls ? ' ' + cls : '')));
    box.setAttribute('title', NOW_TITLE);
    return box;
  }

  function botClass(name) { return 'rl-' + (BOTS[name] ? name : 'other'); }

  function nowJob(box, r, strip) {
    add(box, el('span', 'rl-bot', r.recipient));
    add(box, el('span', 'rl-what', r.action));
    var px = add(box, el('span', 'rl-px'));
    px.setAttribute('aria-hidden', 'true');
    add(px, strip);
  }

  function lasting(parent, cls, lead, age) {
    if (age === null || age === undefined) { if (lead) add(parent, el('span', cls, lead)); return; }
    ticking(add(parent, el('span', cls)), age, (lead ? lead + ' ' : '') + 'for ', '');
  }

  function nowRunning(parent, at) {
    var r = at.running;
    var box = nowBox(parent, botClass(r.recipient) + ' live');
    nowJob(box, r, PX.strip('relay-now-' + r.job, {state: PX.state(r), bot: r.recipient, cell: 2, height: 10}));
    ticking(add(box, el('span', 'rl-run')), r.elapsed_seconds, 'running ', '');
  }

  function nowStale(parent, at) {
    var r = at.running;
    var box = nowBox(parent, 'stale');
    nowJob(add(box, el('span', 'rl-blk ' + botClass(r.recipient))), r,
           PX.strip('relay-now-' + r.job, {state: 'stale', bot: r.recipient, still: true, cell: 2, height: 10}));
    var cause = JOB_STATE[r.stale_cause];
    add(box, chip(cause ? cause[0] : r.job_state, r.job_state === 'stale' ? 'warn' : 'bad'));
    ticking(add(box, el('span', 'rl-late')), r.heartbeat_age_seconds, 'no heartbeat for ', '');
  }

  function nowQueued(parent, at) {
    var r = at.queued;
    var box = nowBox(parent, 'queued ' + botClass(r.recipient));
    add(box, el('span', 'rl-bot', r.recipient));
    add(box, el('span', 'rl-what', r.action));
    ticking(add(box, el('span', 'rl-wait')), r.age_seconds, 'queued ', '');
  }

  function nowStopped(parent, at) {
    var box = nowBox(parent);
    add(box, chip(at.now.state, 'bad'));
    lasting(box, 'rl-say', at.now.stopped_in ? 'in ' + at.now.stopped_in : '', at.now.age_seconds);
  }

  function nowShipped(parent, at) {
    var box = nowBox(parent);
    add(box, chip('shipped', 'ok'));
    add(box, el('span', 'rl-out', at.now.how === 'handed-off' ? 'handed off' : 'merged'));
    if (at.now.age_seconds !== null && at.now.age_seconds !== undefined) {
      ticking(add(box, el('span', 'rl-when')), at.now.age_seconds, '', ' ago');
    }
  }

  function nowWaiting(parent, at) {
    var box = nowBox(parent);
    add(box, chip('waiting', 'warn'));
    lasting(box, 'rl-say', 'on ' + at.now.waiting_on, at.now.age_seconds);
  }

  function nowIdle(parent, at) { lasting(nowBox(parent), 'rl-when', 'in ' + at.now.state, at.now.age_seconds); }

  function liveState(at) { return at.running ? at.running.job_state : null; }
  function rests(at) { return at.now ? at.now.kind : null; }

  var ENDINGS = [
    ['running', function (at) { return liveState(at) === 'running'; }, nowRunning],
    ['stale', function (at) { return liveState(at) === 'stale' || liveState(at) === 'dead'; }, nowStale],
    ['queued', function (at) { return !!at.queued; }, nowQueued],
    ['stopped', function (at) { return rests(at) === 'stopped'; }, nowStopped],
    ['shipped', function (at) { return rests(at) === 'shipped' && !at.said; }, nowShipped],
    ['waiting', function (at) { return rests(at) === 'idle' && !!at.now.waiting_on; }, nowWaiting],
    ['idle', function (at) { return rests(at) === 'idle'; }, nowIdle]
  ];

  function relayThreads(d) {
    var live = liveRows(d);
    return d.relay.threads.map(function (t) {
      var steps = t.handoffs.map(function (r, i) { return {type: 'handoff', row: r, at: r.age_seconds, tie: -i}; });
      var verdicts = t.verdicts.map(function (r) {
        var at = r.age_seconds === null || r.age_seconds === undefined ? null : r.age_seconds;
        return {type: 'verdict', row: r, at: at, tie: r.seq};
      }).sort(function (a, b) { return a.row.seq - b.row.seq; });
      var ordered = steps.concat(verdicts).filter(function (s) { return s.at !== null; }).sort(cmpStep);
      verdicts.forEach(function (v, i) {
        if (v.at === null) ordered.splice(i ? ordered.indexOf(verdicts[i - 1]) + 1 : 0, 0, v);
      });
      var key = threadKey(t);
      var last = ordered[ordered.length - 1];
      var at = {running: live.running[key] || null, queued: live.queued[key] || null, now: t.now || null,
                said: !!last && last.type === 'verdict' && last.row.kind === 'shipped'};
      var rule = ENDINGS.filter(function (e) { return e[1](at); })[0];
      return {
        key: key, ref: t, steps: ordered, at: at, ending: rule ? rule[2] : null,
        needs: !!rule && !!NEEDS_ME[rule[0]],
        wakes: ordered.some(function (s) { return s.type === 'handoff' && s.row.kind === 'bot_to_bot'; }),
        shipped: !!rule && rule[0] === 'shipped' ||
          ordered.some(function (s) { return s.type === 'verdict' && s.row.kind === 'shipped'; })
      };
    });
  }

  function relayCount(shown, tracked) { return shown + ' of ' + tracked + ' tickets · newest activity first'; }

  function stillMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function relayStep(row, s) {
    var r = s.row;
    if (s.type === 'verdict') {
      var said = add(row, el('span', 'rl-verdict'));
      said.setAttribute('title', r.outcome);
      add(said, r.chip ? chip(r.chip, VERDICT[r.chip] || 'muted') : chip(r.kind, 'muted'));
      add(said, el('span', 'rl-out', r.words || r.outcome));
      if (r.age_seconds !== null && r.age_seconds !== undefined) {
        ticking(add(said, el('span', 'rl-when')), r.age_seconds, '', ' ago');
      }
      return;
    }
    var step = add(row, el('span', 'rl-step rl-' + (BOTS[r.recipient] ? r.recipient : 'other') +
                                   (r.kind === 'bot_to_bot' ? ' b2b' : '')));
    add(step, el('span', 'rl-bot', r.recipient));
    add(step, el('span', 'rl-what', r.action));
    ticking(add(step, el('span', 'rl-when')), r.age_seconds, '', ' ago');
    if (r.kind === 'failed') add(row, chip('failed', 'bad'));
  }

  function relayMore(parent, cls, t, hidden) {
    if (hidden <= 0) return;
    var b = add(parent, el('button', cls, '+' + hidden + ' earlier'));
    b.setAttribute('type', 'button');
    b.setAttribute('aria-expanded', rlOpen[t.key] ? 'true' : 'false');
    b.setAttribute('data-more', t.key);
    b.setAttribute('title', 'show ' + hidden + ' earlier step' + (hidden === 1 ? '' : 's'));
    b.addEventListener('click', function () { rlOpen[t.key] = !rlOpen[t.key]; if (latest) render(latest); });
  }

  function relaySep(parent) { add(parent, el('span', 'rl-sep', '→')).setAttribute('aria-hidden', 'true'); }

  function legendKey(legend, swatch, text) {
    var key = add(legend, el('span', 'rl-key'));
    add(key, swatch).setAttribute('aria-hidden', 'true');
    add(key, el('span', null, text));
  }

  function renderRelay(d) {
    var all = relayThreads(d);
    var known = prevKeys.relay || {};
    var seen = nextKeys.relay = {};
    var played = {};
    all.forEach(function (t) {
      t.steps.forEach(function (s) {
        if (s.type !== 'verdict' || s.row.kind !== 'shipped') return;
        var key = t.key + ':' + s.row.seq;
        seen[key] = true;
        if (!firstRender && !known[key]) played[t.key] = key;
      });
    });
    var shown = all.filter(function (t) { return rlFilter === 'all' || t[rlFilter]; });
    var root = panel('relay', 'Relay', relayCount(shown.length, d.relay.tracked));
    if (!root) return;
    var bar = add(root, el('div', 'rl-filter'));
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', 'Filter the relay');
    RELAY_FILTERS.forEach(function (f) {
      var b = add(bar, el('button', 'rl-btn', f[1]));
      b.setAttribute('type', 'button');
      b.setAttribute('aria-pressed', rlFilter === f[0] ? 'true' : 'false');
      b.setAttribute('data-relay', f[0]);
      b.addEventListener('click', function () { rlFilter = f[0]; if (latest) render(latest); });
    });
    if (!all.length) add(root, el('p', 'empty', 'no handoffs or verdicts yet'));
    else if (!shown.length) add(root, el('p', 'empty', 'no ticket matches this filter'));
    else {
      var still = stillMotion();
      var rows = add(root, el('div', 'rl-rows'));
      shown.forEach(function (t) {
        var row = add(rows, el('div', 'rl-row'));
        row.setAttribute('data-thread', t.key);
        var id = add(row, el('span', 'rl-id'));
        add(id, link('rl-tk', ticketPageHref(t.ref))).textContent = ticketOf(t.ref);
        var items = t.steps.map(function (s) { return function (p) { relayStep(p, s); }; });
        if (t.ending) items.push(function (p) { t.ending(p, t.at); });
        relayMore(id, 'rl-more rl-more-phone', t, items.length - RELAY_PHONE_WINDOW);
        var steps = add(row, el('span', 'rl-steps'));
        var hidden = items.length - RELAY_WINDOW;
        relayMore(steps, 'rl-more', t, hidden);
        var shut = rlOpen[t.key] ? 0 : Math.max(items.length - RELAY_PHONE_WINDOW, 0);
        items.forEach(function (draw, i) {
          if (i < shut) {
            var early = add(steps, el('span', i < hidden ? 'rl-early' : 'rl-early-phone'));
            draw(early);
            relaySep(early);
            return;
          }
          if (i > shut) relaySep(steps);
          draw(steps);
        });
        if (played[t.key] && !still) {
          var box = add(steps, el('span', 'rl-done'));
          box.setAttribute('aria-hidden', 'true');
          add(box, PX.strip('relay-' + played[t.key], {effect: 'off', tint: DONE_TINT, done: true, settle: true, cell: 4, height: 8}));
        }
      });
    }
    var legend = add(root, el('div', 'rl-legend'));
    legendKey(legend, el('span', 'rl-sw'), 'handoff');
    legendKey(legend, el('span', 'rl-sw b2b'), 'bot-to-bot (grok wake)');
    legendKey(legend, chip('routed', 'accent'), 'verdict');
    legendKey(legend, chip('failed', 'bad'), 'failed handoff');
    legendKey(legend, el('span', 'rl-sw now'), NOW_TITLE);
    add(legend, link('rl-find', 'job.html')).textContent = 'find any of the ' + d.relay.tracked + ' tickets';
    add(root, el('p', 'rl-note', RELAY_NOTE));
  }

  function renderAttention(d) {
    if (!GH.started) { GH.started = true; ghStart(); }
    var items = attentionItems(d).sort(function (a, b) { return toneRank(a.kind) - toneRank(b.kind); });
    var root = panel('attention', 'Needs attention',
                     items.length + (items.length === 1 ? ' item' : ' items') + (items.length ? ' · worst first' : ''));
    if (!root) return;
    if (!items.length) { add(root, el('p', 'empty', 'all clear')); return; }
    var rows = add(root, el('div', 'at-rows'));
    items.forEach(function (x) {
      var key = attentionKey(x);
      var row = add(rows, el('div', 'at-row'));
      row.setAttribute('data-attn', key);
      var blk = add(row, el('span', 'at-px'));
      blk.setAttribute('aria-hidden', 'true');
      var tone = ATTENTION[x.kind] || 'muted';
      add(blk, PX.strip('attention-' + key, {effect: 'static', tint: TONE_TINT[tone], cell: 4, height: 24}));
      add(row, chip(x.kind, tone));
      add(row, el('span', 'at-subj', x.component || ticketOf(x)));
      add(row, el('span', 'at-what', x.detail));
      if (x.wait) atWhy(row, x);
      add(row, link('at-go', x.component ? healthLink(x.link) : ticketPageHref(x))).textContent =
        x.component ? 'open in System' : 'open ticket';
      if (!x.component) addGh(row, x);
      if (x.wait && GH.on && GH.login) atActs(row, x, key);
      mark('attention', key, row);
    });
  }

  var ACT = __ACT__;
  var FORMS_BY_ATTENTION_KEY = {};

  function commentHref(x) {
    var refs = ghRefs(x);
    var id = String(x.wait.comment_id);
    if (!refs || !/^[0-9]+$/.test(id)) return null;
    return 'https://github.com/' + refs.repo + '/issues/' + (refs.pr || refs.issue) + '#issuecomment-' + id;
  }
  function atWhy(row, x) {
    var why = add(row, el('span', 'at-why'));
    add(why, el('span', null, (ACT.why[x.wait.why] || x.wait.why) + (x.wait.detail ? ': ' + x.wait.detail : '')));
    var href = commentHref(x);
    if (!href) return;
    var a = add(why, link(null, href));
    a.setAttribute('rel', 'noopener noreferrer');
    a.setAttribute('target', '_blank');
    a.textContent = 'comment ↗';
  }
  function atBuild() {
    var p = {x: null, opened: null, action: null, head: null, busy: false};
    p.root = el('div', 'at-box');
    p.form = add(p.root, el('div', 'at-form'));
    p.text = add(p.form, el('textarea'));
    p.text.addEventListener('input', function () { atDraw(p); });
    p.send = ghButton(p.form, 'Send', function () { atSend(p); });
    p.out = add(p.root, el('span', 'at-out'));
    p.out.setAttribute('role', 'status');
    return p;
  }
  function atDraw(p) {
    var bound = ACT.head.indexOf(p.action) >= 0;
    var verb = p.action === 'ok' ? 'OK' : 'Send ' + (ACT.label[p.action] || '');
    p.form.hidden = !p.action;
    p.root.hidden = !p.action && !p.out.textContent;
    if (p.action) p.text.setAttribute('aria-label', 'Your words for ' + ACT.label[p.action]);
    p.send.textContent = !bound ? verb : p.head ? verb + ' ' + p.head : verb + ' (reading head…)';
    p.send.disabled = p.busy || !p.text.value.trim() || (bound && !p.head);
  }
  function atActs(row, x, key) {
    var names = Object.keys(ACT.label).filter(function (name) { return (x.actions || []).indexOf(name) >= 0; });
    var p = FORMS_BY_ATTENTION_KEY[key] || (FORMS_BY_ATTENTION_KEY[key] = atBuild());
    p.x = x;
    if (names.indexOf(p.action) < 0) p.action = null;
    if (names.length) {
      var bar = add(row, el('span', 'at-acts'));
      names.forEach(function (name) {
        var b = ghButton(bar, ACT.label[name], function () { atOpen(p, name); });
        b.setAttribute('data-act', name);
        b.setAttribute('aria-expanded', p.action === name ? 'true' : 'false');
      });
    }
    atDraw(p);
    add(row, p.root);
  }
  function atOpen(p, name) {
    p.action = p.action === name ? null : name;
    p.opened = p.x;
    p.out.textContent = '';
    p.head = null;
    if (ACT.head.indexOf(p.action) >= 0) atHead(p);
    if (latest) render(latest);
    if (p.action) p.text.focus();
  }
  function atHead(p) {
    var refs = ghRefs(p.x);
    if (!refs || !refs.pr) { p.out.textContent = 'error: no-pr'; return; }
    p.busy = true;
    ghPost('/api/head', {repo: refs.repo, pr: Number(refs.pr)}, true).then(function (res) {
      p.busy = false;
      var head = String(res.data.head || '');
      if (res.ok && SHA.test(head)) p.head = head;
      else p.out.textContent = 'error: ' + (res.error || 'bad-head');
      atDraw(p);
    });
  }
  function atSend(p) {
    var x = p.opened;
    var body = {repo: x.repo, ticket_id: x.ticket_id, action: p.action, expect_state: x.state,
                expect_why: x.wait.why, words: p.text.value};
    if (p.action === 'answer') body.expect_comment_id = x.wait.comment_id;
    if (ACT.head.indexOf(p.action) >= 0) body.head = p.head;
    p.busy = true;
    atDraw(p);
    ghPost('/api/act', body, true).then(function (res) {
      p.busy = false;
      ghResult(p.out, res, 'sent');
      var head = String(res.data.head || '');
      if (res.error === 'head-changed' && SHA.test(head)) {
        p.head = head;
        p.out.textContent = 'error: head-changed, the PR head is now ' + head;
      }
      if (res.ok) { p.text.value = ''; p.action = null; }
      if (latest) render(latest);
    });
  }
