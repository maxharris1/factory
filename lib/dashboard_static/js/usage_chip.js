
  function chipSegment(harness, views, now) {
    var fresh = views.filter(function (v) { return v.entry.headroom !== null; });
    if (!fresh.length) return {harness: harness, pct: null};
    var view = fresh[0];
    fresh.forEach(function (v) { if (v.entry.headroom < view.entry.headroom) view = v; });
    var entry = view.entry;
    var fullest = null;
    WINDOWS.forEach(function (w) {
      if (entry[w].state === 'ok' && (fullest === null || entry[w].used > fullest.used)) fullest = entry[w];
    });
    return {
      harness: harness,
      pct: halfUp(entry.headroom * 100),
      tone: view.limit ? 'bad' : view.tone,
      resets: fullest.resets_at === null ? null : dur(fullest.resets_at - now)
    };
  }
  function chipLine(entry, view) {
    var limit = entry.limit_label ? ' · ' + entry.limit_label : '';
    if (view === null) return entry.harness + ' ' + entry.account + ': not tracked' + limit;
    return entry.harness + ' ' + accountName(entry) + ': 5-hour ' + view.windows[0][0] +
      ', 7-day ' + view.windows[1][0] + limit;
  }
  function chipModel(d) {
    var now = d.generated_at_epoch;
    var accounts = d.usage.accounts;
    var views = accounts.map(function (e) { return e.state === 'not tracked' ? null : accountView(e, now); });
    var tracked = views.filter(function (v) { return v !== null; });
    var order = [];
    var pools = Object.create(null);
    tracked.forEach(function (v) {
      var harness = v.entry.harness;
      if (!pools[harness]) { pools[harness] = []; order.push(harness); }
      pools[harness].push(v);
    });
    return {
      segments: order.map(function (harness) { return chipSegment(harness, pools[harness], now); }),
      phone: tracked.length ? chipSegment('usage', tracked, now) : null,
      title: ['Subscription headroom'].concat(accounts.map(function (e, i) { return chipLine(e, views[i]); })).join('\n')
    };
  }
  function chipSegmentEl(segment, phone) {
    var cls = phone ? 'uc-seg uc-phone' : 'uc-seg';
    var seg = el('span', cls + ' ' + (segment.pct === null ? 'stale' : segment.tone));
    add(seg, el('span', phone ? 'uc-name' : 'uc-name ' + segment.harness, segment.harness));
    if (segment.pct === null) {
      add(seg, el('span', 'uc-stale', 'stale'));
      return seg;
    }
    var bar = add(seg, el('span', 'uc-bar'));
    bar.setAttribute('aria-hidden', 'true');
    add(bar, el('i')).setAttribute('style', 'width:' + segment.pct + '%');
    add(seg, el('b', null, segment.pct + '%'));
    if (segment.resets !== null) add(seg, el('span', 'uc-reset', segment.resets));
    return seg;
  }
  function renderUsageChip(d) {
    var chip = byId('usage-chip');
    if (!chip) return;
    var model = chipModel(d);
    if (!model.segments.length) {
      chip.hidden = true;
      chip.removeAttribute('title');
      chip.removeAttribute('aria-label');
      chip.replaceChildren();
      return;
    }
    chip.hidden = false;
    chip.setAttribute('title', model.title);
    chip.setAttribute('aria-label', model.title);
    var nodes = model.segments.map(function (s) { return chipSegmentEl(s, false); });
    nodes.push(chipSegmentEl(model.phone, true));
    chip.replaceChildren.apply(chip, nodes);
  }
