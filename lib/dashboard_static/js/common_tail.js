
  var PAGES = {
    main: [renderNeedsMax, renderIntake, renderLanes, renderJustHappened],
    job: [redirectMoved, renderBoard, renderTickets],
    activity: [renderAttention, renderRelay],
    efficiency: [renderHeadroom, renderTokens, renderRounds, renderSavings, renderRouting, renderBalance, renderJobsLifetime],
    evals: [renderEvals],
    health: [renderBroken, renderComponents, renderDrift, renderHealth, renderLog, renderRules, renderNoWaiter],
    ticket: [renderTicketView],
    settings: [renderSettings, renderAccounts]
  };
  var RENDERERS = [renderBand, renderAttentionStrip, renderStatusLine].concat(PAGES[PAGE] || []);

  function nodeKey(node) {
    var parts = [];
    var attrs = node.attributes;
    for (var i = 0; i < attrs.length; i++) {
      var name = attrs[i].name;
      if (name === 'id' || name === 'href' || name.slice(0, 5) === 'data-') parts.push(name + '=' + attrs[i].value);
    }
    if (parts.length) return node.tagName + '[' + parts.sort().join('][') + ']';
    var cls = (node.getAttribute('class') || '').split(' ').filter(function (c) { return c && c !== 'fresh'; });
    return node.tagName + '.' + (cls[0] || '');
  }
  function locator(node) {
    var chain = [], keys = [];
    for (var n = node; n && n !== body; n = n.parentNode) { chain.push(n); keys.push(nodeKey(n)); }
    chain.push(body);
    return {chain: chain, keys: keys, text: node.textContent};
  }
  function twins(parent, keys, k, hits) {
    var kids = parent.children;
    for (var i = 0; i < kids.length; i++) {
      if (nodeKey(kids[i]) !== keys[k]) continue;
      if (k === 0) hits.push(kids[i]); else twins(kids[i], keys, k - 1, hits);
    }
  }
  function keyed(key) { return key.indexOf('[') > 0; }
  function owner(node, root) {
    for (var n = node.parentNode; n !== root; n = n.parentNode) if (keyed(nodeKey(n))) return nodeKey(n);
    return null;
  }
  function owned(node, key, mine, root, hits) {
    var kids = node.children;
    for (var i = 0; i < kids.length; i++) {
      if (nodeKey(kids[i]) === key && owner(kids[i], root) === mine) hits.push(kids[i]);
      owned(kids[i], key, mine, root, hits);
    }
    return hits;
  }
  function locate(at) {
    var i = 0;
    while (!at.chain[i].isConnected) i++;
    if (!i) return {root: at.chain[0], node: at.chain[0]};
    var hits = [];
    twins(at.chain[i], at.keys, i - 1, hits);
    if (!hits.length) {
      var mine = at.keys.slice(1, i).filter(keyed)[0] || null;
      hits = owned(at.chain[i], at.keys[0], mine, at.chain[i], []);
    }
    var same = hits.filter(function (h) { return h.textContent === at.text; });
    return {root: at.chain[i], node: same[0] || (hits.length === 1 ? hits[0] : null)};
  }
  function scrolled(node, out) {
    var kids = node.children;
    for (var i = 0; i < kids.length; i++) {
      var c = kids[i];
      if (c.scrollLeft || c.scrollTop) out.push({at: locator(c), left: c.scrollLeft, top: c.scrollTop});
      scrolled(c, out);
    }
    return out;
  }
  function keepPlace(redraw) {
    var active = document.activeElement;
    var focus = active && active !== body ? locator(active) : null;
    var boxes = scrolled(body, []);
    redraw();
    boxes.forEach(function (b) {
      var box = locate(b.at).node;
      if (box) { box.scrollLeft = b.left; box.scrollTop = b.top; }
    });
    if (!focus) return;
    if (active.isConnected) {
      // A kept node (the GitHub comment box) loses focus while it is detached.
      if (document.activeElement !== active) active.focus({preventScroll: true});
      return;
    }
    var found = locate(focus);
    var target = found.node;
    if (!target) {
      for (target = found.root; target && target !== body && target.hidden; target = target.parentNode) {}
      if (!target || target === body) return;
      if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
    }
    target.focus({preventScroll: true});
  }

  function render(d) {
    latest = d;
    tickers = [];
    nextKeys = {};
    keepPlace(function () {
      RENDERERS.forEach(function (draw) {
        try { draw(d); } catch (err) { if (window.console) console.error('dashboard panel', err); }
      });
      PX.bind();
    });
    prevKeys = nextKeys;
    firstRender = false;
    healthKey = agedHealth(d).key;
  }

  function revealHash() {
    var target = byId(String(location.hash || '').slice(1));
    if (!target) return;
    for (var n = target.parentNode; n; n = n.parentNode) if (n.tagName === 'DETAILS') n.open = true;
    target.scrollIntoView();
  }

  var FRESH = {
    boot: {label: null, bar: false, band: true},
    live: {label: 'LIVE', bar: false, band: true},
    stale: {label: 'STALE', bar: true, band: false},
    lost: {label: 'LINK LOST', bar: true, band: false},
    paused: {label: 'PAUSED · resuming', bar: false, band: false}
  };
  var freshState = '';
  var ET = new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit'});
  function staleSeconds() {
    var h = (latest && latest.health) || {};
    var publish = Number(h.publish_seconds) || 0;
    return publish > 0 ? ((h.thresholds || {}).stale_beats || 3) * publish : STALE_SECONDS;
  }
  function freshness() {
    if (resuming) return 'paused';
    if (!everOk && !failures) return 'boot';
    if (Date.now() - Math.max(lastOk, shownAt) > LINK_LOST_MS) return 'lost';
    if (!everOk) return 'boot';
    return since() > staleSeconds() ? 'stale' : 'live';
  }
  function publishedAt() { return ET.format(new Date(generated * 1000)) + ' ET'; }
  function freshBar(state, age) {
    var bar = byId('fresh-bar');
    if (!bar) return;
    var head = byId('fresh-head');
    var text = byId('fresh-text');
    var say = byId('fresh-say');
    if (!FRESH[state].bar) {
      bar.hidden = true;
      if (say) say.textContent = '';
      return;
    }
    var words = state === 'stale'
      ? ['Snapshot is ' + dur(age) + ' old.',
         'The dashboard writer last published at ' + publishedAt() + '. Everything below is as of then.']
      : ['Cannot reach the dashboard.', 'Showing the last snapshot, from ' + publishedAt() + '.'];
    if (head) head.textContent = words[0];
    if (text) text.textContent = words[1];
    if (say && (bar.hidden || bar.className !== 'fresh-bar ' + state)) say.textContent = words[0] + ' ' + words[1];
    bar.className = 'fresh-bar ' + state;
    bar.hidden = false;
  }
  function statusAge(state, age) {
    var line = byId('statusline');
    var seg = line && Array.prototype.filter.call(line.children, function (n) {
      return n.getAttribute('data-seg') === 'age';
    })[0];
    if (!seg) return;
    var text = seg.children[1];
    var on = FRESH[state].bar;
    seg.hidden = !on;
    seg.className = on ? 'sl-age ' + state : 'sl-age';
    if (!text) return;
    text.textContent = on ? 'as of ' + dur(age) + ' ago' : '';
    text.setAttribute('data-short', on ? dur(age).split(' ')[0] + ' old' : '');
  }

  function updateLink() {
    var link = byId('link');
    var text = byId('link-text');
    var age = since();
    var state = freshness();
    var label = FRESH[state].label || (failures ? 'NO LINK · static view' : 'ACQUIRING LINK');
    var caret = byId('caret');
    if (!document.hidden) {
      body.setAttribute('data-fresh', state);
      if (link) link.className = 'link ' + state;
      if (text) text.textContent = label;
      if (caret && state !== caretState) {
        caretState = state;
        caret.replaceChildren(PX.strip('caret', {state: 'link-' + state, height: 12}));
        PX.bind();
      }
      var moved = FRESH[state].band !== (FRESH[freshState] || FRESH.boot).band;
      freshState = state;
      if (moved) { renderBand(latest); PX.bind(); }
      freshBar(state, age);
      statusAge(state, age);
    }
    var ageNode = byId('age');
    if (ageNode) ageNode.textContent = dur(age) + ' ago';
    var clock = byId('clock');
    if (clock) clock.textContent = new Date(Date.now()).toISOString().slice(11, 19) + ' UTC';
  }

  function tick() {
    var d = since();
    tickers.forEach(function (t) { t.run(d); });
    if (latest && agedHealth(latest).key !== healthKey) render(latest);
    updateLink();
  }

  function accept(d) {
    if (!d || typeof d !== 'object' || d.schema !== 'factory-dashboard/v1') { failed(); return; }
    lastOk = Date.now();
    accepted += 1;
    everOk = true;
    resuming = false;
    failures = 0;
    if (firstRender || d.generated_at_epoch !== generated) {
      var first = firstRender;
      generated = Number(d.generated_at_epoch) || generated;
      render(d);
      if (first) revealHash();
    }
    tick();
  }

  function failed() {
    failures += 1;
    resuming = false;
    if (!everOk && !GH.on && failures >= 3 && !reloadArmed) {
      // status.json cannot be fetched (blocked or missing): behave like a
      // page without script and reload the server-rendered view every
      // refresh_seconds.
      reloadArmed = true;
      setTimeout(function () { location.reload(); }, refreshSeconds * 1000);
    }
    updateLink();
  }

  function schedule() {
    if (timer === null && !inflight && !document.hidden) timer = setTimeout(poll, POLL_MS);
  }

  function poll() {
    timer = null;
    if (inflight || document.hidden) return;
    inflight = true;
    var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    var guard = setTimeout(function () { if (ctrl) ctrl.abort(); }, FETCH_TIMEOUT_MS);
    var options = {cache: 'no-store', credentials: 'same-origin'};
    if (ctrl) options.signal = ctrl.signal;
    var request;
    try { request = fetch(STATUS_URL, options); } catch (err) { request = Promise.reject(err); }
    request
      .then(function (response) {
        if (response.status === 401) {
          if (!GH.started) { GH.started = true; ghStart(); }
          throw new Error('HTTP 401');
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(accept, function () { failed(); })
      .then(function () { clearTimeout(guard); inflight = false; schedule(); },
            function () { clearTimeout(guard); inflight = false; schedule(); });
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    shownAt = Date.now();
    resuming = true;
    updateLink();
    if (timer === null && !inflight) poll();
  });
  var skip = byId('skip');
  if (skip) {
    skip.addEventListener('click', function (event) {
      var main = byId('hud');
      if (!main) return;
      event.preventDefault();
      main.focus();
    });
  }
  if (window.addEventListener) {
    window.addEventListener('hashchange', function () { if (latest) { render(latest); tick(); revealHash(); } });
  }
  setInterval(tick, 1000);
  updateLink();
  PX.bind();
  poll();
})();
