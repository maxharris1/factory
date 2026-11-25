
  // --- system page (#141) ------------------------------------------------------

  var LOG_KINDS = [[/exit rc=(?!0\b)\d+/, 'bad'], [/Traceback/, 'bad'], [/\b\w*(?:Error|Exception):/, 'bad'],
                   [/\b(?:ERROR|CRITICAL)\b/, 'bad'], [/\b(?:WARNING|WARN)\b/, 'warn'], [/another orch poll owns/, 'warn'],
                   [/poller start/, 'start']];
  function logKinds(lines) {
    var traceback = false;
    return lines.map(function (line) {
      if (traceback && /^\s/.test(line)) return 'bad';
      traceback = line.indexOf('Traceback') >= 0;
      for (var i = 0; i < LOG_KINDS.length; i++) if (LOG_KINDS[i][0].test(line)) return LOG_KINDS[i][1];
      return null;
    });
  }
  function renderLog(d) {
    var log = d.poller.log;
    var root = panel('log', 'poller.log', log.present ? 'written ' + dur(log.age_seconds) + ' before the snapshot' : 'absent');
    if (!root) return;
    add(root, el('p', 'sys-note', 'last ' + log.tail.length + ' lines, newest first, redacted'));
    var box = add(root, el('div', 'sys-log'));
    var kinds = logKinds(log.tail);
    log.tail.map(function (line, i) { return [line, kinds[i]]; }).reverse()
      .forEach(function (row) { add(box, el('div', row[1], row[0])); });
  }

  function renderRules(d) {
    var root = panel('rules', 'Rules and limits', 'read-only');
    if (!root) return;
    var rules = d.rules || [];
    if (!rules.length) add(root, el('p', 'empty', 'rules unavailable'));
    else {
      var ul = add(root, el('ul', 'sys-rules'));
      rules.forEach(function (r) {
        var li = add(ul, el('li'));
        add(li, el('span', 'key', r.key));
        add(li, el('span', 'text', r.text));
      });
    }
    add(root, el('div', 'sys-sub', 'projects'));
    var projects = d.projects || [];
    if (!projects.length) { add(root, el('p', 'empty', 'no projects loaded')); return; }
    var feed = add(root, el('ul', 'feed'));
    projects.forEach(function (p) {
      var li = add(feed, el('li'));
      add(li, el('span', 'ticket', p.repo));
      add(li, el('span', 'what', p.tracker));
      add(li, el('span', 'sys-proj', projectLine(p)));
    });
  }
  function projectLine(p) {
    if (p.status !== 'ok' || !p.rounds) return 'ship mode and round cap: unknown (' + (p.reason || 'not read') + ')';
    return 'ship mode ' + p.ship + (p.approver ? ' (approver ' + p.approver + ')' : '') +
      ' · round cap ' + p.rounds.low + ' low, ' + p.rounds.high + ' high';
  }
