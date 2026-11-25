
  // --- every page ---------------------------------------------------------

  // The lanes the page draws: devices with their running jobs and the slots
  // in use, plus grok wakes. Mirrors lane_devices in lib/dashboard_page.py.
  function laneDevices(d) {
    var slots = d.slots || {};
    var box = slots.box || {};
    var host = box.name || 'box';
    var devices = (slots.devices || []).map(function (x) {
      return {name: x.name, slot_max: x.slot_max, held: x.held || 0, enabled: x.enabled,
              placeholder: x.placeholder, harnesses: x.harnesses || [], listed: true, jobs: []};
    });
    var byName = {};
    devices.forEach(function (x) { byName[x.name] = x; });
    var wakes = [];
    (d.running || []).forEach(function (r) {
      if (r.device === null || r.device === undefined) { wakes.push(r); return; }
      if (!Object.prototype.hasOwnProperty.call(byName, r.device)) {
        var dev = {name: r.device, slot_max: r.device === host ? box.slot_max : null, held: 0, enabled: true,
                   placeholder: false, harnesses: [], listed: r.device === host && !devices.length, jobs: []};
        devices.push(dev);
        byName[r.device] = dev;
      }
      byName[r.device].jobs.push(r);
    });
    if (!devices.length) {
      devices.push({name: host, slot_max: box.slot_max, held: 0, enabled: true, placeholder: false,
                    harnesses: [], listed: true, jobs: []});
    }
    // #63 FU-1: a slot held in device_slots is in use even when its job is
    // not (or no longer) running, so a lane counts max(held, running).
    devices.forEach(function (dev) { dev.used = Math.max(dev.held, dev.jobs.length); });
    return {devices: devices, wakes: wakes};
  }

  // Running / total slots over the devices that can take work (header_load).
  function headerLoad(d) {
    var used = 0;
    var total = 0;
    laneDevices(d).devices.forEach(function (dev) {
      if (dev.enabled && !dev.placeholder && dev.slot_max) { used += dev.used; total += dev.slot_max; }
    });
    return total ? Math.max(0, Math.min(1, used / total)) : 0;
  }

  function renderBand(d) {
    var root = byId('band');
    if (!root) return;
    var shown = root.children[0];
    var load = d ? headerLoad(d) : Number(shown && shown.getAttribute('data-load')) || 0;
    var opts = {state: 'band', load: load, height: 24};
    if (!(FRESH[freshState] || FRESH.boot).band) { opts.tint = '#8b93b8'; opts.still = true; }
    root.replaceChildren(PX.strip('band', opts));
  }

  function attentionItems(d) {
    var a = d.attention || {items: []};
    return (a.items || []).concat(agedHealth(d).extra);
  }
  function attentionKey(x) { return x.kind + ':' + (x.component || x.repo + '#' + x.ticket_id); }

  var TONE_RANK = {bad: 0, warn: 1};
  function toneRank(kind) {
    var rank = TONE_RANK[ATTENTION[kind]];
    return rank === undefined ? 2 : rank;
  }
  function stripModel(items) {
    var count = items.length;
    var need = count === 1 ? ' needs attention' : ' need attention';
    var tally = [];
    var at = {};
    items.forEach(function (x) {
      if (!Object.prototype.hasOwnProperty.call(at, x.kind)) {
        at[x.kind] = tally.length;
        tally.push({kind: x.kind, n: 0, tone: ATTENTION[x.kind] || 'muted', first: tally.length});
      }
      tally[at[x.kind]].n += 1;
    });
    tally.sort(function (a, b) { return toneRank(a.kind) - toneRank(b.kind) || b.n - a.n || a.first - b.first; });
    var worst = items.map(function (x, i) {
      var age = x.age_seconds;
      return {x: x, i: i, age: typeof age === 'number' && isFinite(age) ? age : null};
    }).sort(function (a, b) {
      return toneRank(a.x.kind) - toneRank(b.x.kind) || (a.age === null) - (b.age === null) ||
        (b.age || 0) - (a.age || 0) || a.i - b.i;
    }).slice(0, 2).map(function (p) { return p.x; });
    function named(t) { return t.n + ' ' + t.kind; }
    var largest = tally.slice().sort(function (a, b) { return b.n - a.n; }).slice(0, 2);
    var rest = count - largest.reduce(function (sum, t) { return sum + t.n; }, 0);
    return {count: count, need: need, tally: tally, worst: worst, more: count - worst.length,
            short: largest.map(named).concat(rest ? ['+' + rest] : []).join(' · '),
            label: count + need + ': ' + tally.map(named).join(', ') + '. Open Activity.'};
  }

  function renderAttentionStrip(d) {
    var root = byId('attention-strip');
    if (!root) return;
    var items = attentionItems(d);
    root.replaceChildren();
    root.hidden = !items.length;
    if (!items.length) return;
    var model = stripModel(items);
    function blk(parent, key) {
      var span = add(parent, el('span', 'attn-blk'));
      span.setAttribute('aria-hidden', 'true');
      add(span, PX.strip(key, {state: 'attention', height: 16}));
    }
    function count(parent, href) {
      var a = add(parent, link('attn-count', href));
      add(a, el('b', null, model.count)).setAttribute('id', 'attention-count');
      add(a, el('span', null, model.need));
    }
    if (PAGE === 'activity') {
      var here = add(root, el('div', 'attn-here'));
      blk(here, 'attention');
      count(here, '#attention');
      add(here, el('span', 'attn-below', 'listed below'));
      return;
    }
    var row = add(root, el('div', 'attn-row'));
    var head = add(row, el('div', 'attn-head'));
    blk(head, 'attention');
    count(head, 'activity.html#attention');
    var tally = add(row, el('div', 'attn-tally'));
    model.tally.forEach(function (t, i) {
      if (i) add(tally, el('span', null, '·')).setAttribute('aria-hidden', 'true');
      add(tally, el('span', 'attn-n ' + t.tone, t.n + ' ' + t.kind));
    });
    add(row, el('span', 'attn-rule')).setAttribute('aria-hidden', 'true');
    model.worst.forEach(function (x) {
      var item = add(row, el('div', 'attn-item'));
      item.setAttribute('data-attn', attentionKey(x));
      add(item, chip(x.kind, ATTENTION[x.kind] || 'muted'));
      add(item, link('attn-who', x.component ? healthLink(x.link) : ticketPageHref(x))).textContent =
        x.component ? x.component : ticketOf(x);
      add(item, el('span', 'attn-what', x.detail));
    });
    if (model.more) add(row, link('attn-more', 'activity.html#attention')).textContent = '+' + model.more + ' more →';
    var phone = add(root, link('attn-phone', 'activity.html#attention'));
    phone.setAttribute('aria-label', model.label);
    blk(phone, 'attention-phone');
    add(phone, el('b', null, model.count + model.need));
    add(phone, el('span', 'attn-short', model.short));
    add(phone, el('span', 'attn-go', '→')).setAttribute('aria-hidden', 'true');
  }

  // "behind": configured code components not on the checkout's version
  // (lib/health.py stale_code). Mirrors version_drift.
  function versionDrift(h) {
    h = h || {};
    var rows = (h.components || []).filter(function (c) {
      return c.configured !== false && SHELL.code.indexOf(c.component) >= 0;
    });
    return {
      newest: String(h.code_version || 'unknown'),
      behind: rows.filter(function (c) { return !!c.stale_code; }).map(function (c) { return c.label; }),
      unknown: rows.filter(function (c) { return c.version === 'unknown'; }).map(function (c) { return c.label; })
    };
  }

  function segment(key, text, state, label, age, prefix, href) {
    return {key: key, label: label === undefined ? null : label, text: text,
            state: state === undefined ? null : state,
            href: href || (Object.prototype.hasOwnProperty.call(SHELL.links, key) ? SHELL.links[key] : null),
            age: age === undefined ? null : age, prefix: prefix === undefined ? null : prefix};
  }
  function present(v) { return v !== null && v !== undefined; }

  // The status line's segments, each from the snapshot. Mirrors status_segments.
  function statusSegments(d) {
    var out = [];
    var devices = laneDevices(d).devices;
    devices.forEach(function (dev) {
      var value = !dev.enabled || dev.placeholder ? 'off'
        : dev.used + '/' + (present(dev.slot_max) ? dev.slot_max : '—');
      out.push(segment('device:' + dev.name, value, null, dev.name));
    });
    var live = devices.filter(function (dev) { return dev.enabled && !dev.placeholder; });
    var total = live.length && live.every(function (dev) { return present(dev.slot_max); })
      ? live.reduce(function (n, dev) { return n + dev.slot_max; }, 0) : '—';
    out.push(segment('slots', live.reduce(function (n, dev) { return n + dev.used; }, 0) + '/' + total, null, 'slots'));
    out.push(segment('queue', num(((d.queue || {}).totals || {}).queued || 0), null, 'queue'));
    var p = d.poller || {};
    var h = p.health || {state: 'unknown'};
    var ps = POLL_STATE[h.state] || POLL_STATE.unknown;
    var poll = 'poll ' + ps[0].toLowerCase();
    var cycle = h.last_cycle_age_seconds;
    if (present(cycle)) out.push(segment('poll', poll + ' · ' + dur(cycle), ps[1], null, cycle, poll + ' · '));
    else out.push(segment('poll', poll, ps[1]));
    var errors = (p.errors || {}).last_hour || 0;
    out.push(segment('poll-errors', num(errors) + ' poll error' + (errors === 1 ? '' : 's') + ' / 1 h', errors ? 'bad' : 'ok'));
    var b = p.linear_budget || {state: 'ok'};
    var bs = BUDGET_STATE[b.state] || ['OTHER', 'muted'];
    out.push(segment('linear', 'linear ' + bs[0].toLowerCase(), bs[1]));
    var w = p.linear_webhook;
    if (!w) {
      out.push(segment('webhook', 'no webhook', 'muted', undefined, undefined, undefined, SHELL.absent));
    } else if (!present(w.last_received_age_seconds)) {
      out.push(segment('webhook', 'webhook never', present(w.warning) ? 'bad' : 'warn'));
    } else {
      out.push(segment('webhook', 'webhook ' + dur(w.last_received_age_seconds), present(w.warning) ? 'bad' : 'ok',
                       null, w.last_received_age_seconds, 'webhook '));
    }
    var f = d.feeder;
    if (!f) out.push(segment('feeder', 'no feeder', 'muted', undefined, undefined, undefined, SHELL.absent));
    else {
      var failing = (f.items || []).some(function (x) { return x.state === 'error'; });
      out.push(segment('feeder', 'feeder ' + num(f.fed) + ' fed', failing ? 'bad' : 'ok'));
    }
    var hs = d.health || {};
    if (!hs.known) {
      out.push(segment('components', 'components unknown', hs.error ? 'bad' : 'muted'));
    } else {
      var rows = agedHealth(d).rows.filter(function (c) { return c.configured !== false; });
      var up = rows.filter(function (c) { return c.state === 'up'; }).length;
      var down = rows.some(function (c) { return c.state === 'down'; });
      out.push(segment('components', up + ' / ' + rows.length + ' up', down ? 'bad' : up < rows.length ? 'warn' : 'ok'));
    }
    var drift = versionDrift(hs);
    if (drift.newest === 'unknown') out.push(segment('version', 'version unknown', 'warn'));
    else {
      out.push(segment('version', drift.newest.slice(0, 7) + ' · ' + drift.behind.length + ' behind',
                       drift.behind.length ? 'warn' : 'ok'));
    }
    var ok = out.filter(function (s) { return s.state === 'ok'; }).length;
    if (ok) out.push(segment('ok-count', ok + ' ok', 'ok'));
    return out;
  }

  function renderStatusLine(d) {
    var root = byId('statusline');
    if (!root) return;
    root.replaceChildren();
    add(root, el('span', 'sl-brand', 'factory'));
    var filled = false;
    statusSegments(d).forEach(function (s) {
      if (s.state === null) {
        var plain = add(root, el('span', 'sl-seg' + (s.key === 'queue' ? ' sl-queue' : '')));
        plain.setAttribute('data-seg', s.key);
        var k = add(plain, el('span', 'sl-k', s.label));
        if (s.key === 'queue') k.setAttribute('data-short', 'q');
        add(plain, el('span', 'sl-v', s.text));
        return;
      }
      if (!filled) {
        add(root, el('span', 'sl-fill'));
        var age = add(root, el('span', 'sl-age'));
        age.setAttribute('data-seg', 'age');
        age.hidden = true;
        add(age, el('span', 'blk')).setAttribute('aria-hidden', 'true');
        add(age, el('span', 'sl-t')).setAttribute('data-short', '');
        filled = true;
      }
      var a = add(root, link('sl-vital ' + s.state, s.href));
      a.setAttribute('data-seg', s.key);
      add(a, el('span', 'blk')).setAttribute('aria-hidden', 'true');
      var text = add(a, el('span', null, s.text));
      if (s.age !== null) ticking(text, s.age, s.prefix);
      add(a, el('span', 'vh', ', ' + s.state));
    });
  }

  // #92: a snapshot vouches for the states it observed until its next
  // publication is overdue (stale_beats publication intervals; a one-off
  // snapshot vouches for none). Past that, a component that is not still
  // inside its up window is unknown, not down: the page cannot see it. Only
  // the dashboard writer, whose beats are the publications, ages to stale
  // and down; with no writer row that can alert, the overdue snapshot is one
  // attention item.
  function agedHealth(d) {
    var h = d.health || {};
    var t = h.thresholds || {};
    var staleBeats = t.stale_beats || 3;
    var downBeats = t.down_beats || 8;
    var age = since();
    var vouched = age <= staleBeats * (Number(h.publish_seconds) || 0);
    var extra = [];
    var writer = false;
    var unknown = false;
    var rows = (h.components || []).map(function (c) {
      var isWriter = c.component === 'dashboard';
      writer = writer || (isWriter && c.configured !== false);
      var base = c.last_seen_age_seconds;
      if (vouched || c.state === 'down' || base === null || base === undefined) return c;
      var seen = base + age;
      var interval = Math.max(Number(c.interval_seconds) || 15, 1);
      var state = seen > downBeats * interval ? 'down' : seen > staleBeats * interval ? 'stale' : c.state;
      if (!isWriter && (state !== 'up' || c.state !== 'up')) {
        unknown = true;
        return Object.assign({}, c, {state: 'unknown', reason: 'no snapshot for ' + dur(age)});
      }
      if (state === c.state) return c;
      var reason = 'no heartbeat for ' + dur(seen);
      if (state === 'down' && c.configured !== false) {
        extra.push({kind: 'down', repo: '', ticket_id: '', component: c.label,
                    link: 'health.html#' + c.anchor, detail: 'down (' + reason + ')'});
      }
      return Object.assign({}, c, {state: state, reason: reason});
    });
    if (unknown && !writer) {
      extra.push({kind: 'health', repo: '', ticket_id: '', component: 'health snapshot',
                  link: 'health.html#components', detail: 'unknown: no snapshot for ' + dur(age)});
    }
    return {rows: rows, extra: extra, key: rows.map(function (c) { return c.state; }).join(',')};
  }
