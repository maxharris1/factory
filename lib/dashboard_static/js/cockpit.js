
  // --- Mission Control daily cockpit (E4-1): Needs Max / Product Inbox & Visual QA Gate ---

  var CK_STATE = {};
  var NEEDS_MAX = __NEEDS_MAX__;
  var CK_QA_PATH = /^\/qa\/runs\/[A-Za-z0-9._-]{1,64}(?:\/lane-[1-9][0-9]*\/[^\s"'<>]+)?$/;

  function ckInboxGroup(t) {
    var why = (t.wait && t.wait.why) || '';
    for (var i = 0; i < NEEDS_MAX.groups.length; i++) {
      var g = NEEDS_MAX.groups[i];
      if ((g.why === null || g.why === why) && (g.state === null || g.state === t.state)) return i;
    }
  }

  function ckInboxTickets(d) {
    var rows = (d && d.tickets) || [];
    var items = rows.filter(function (t) {
      return t && (t.state === 'waiting-max' || t.state === 'awaiting-ok');
    });
    return items.slice().sort(function (a, b) {
      return ckInboxGroup(a) - ckInboxGroup(b) || String(a.repo).localeCompare(String(b.repo)) || String(a.ticket_id).localeCompare(String(b.ticket_id));
    });
  }

  function ckKey(prefix, t) {
    return prefix + ':' + String(t.repo || '').toLowerCase() + '#' + String(t.ticket_id || '');
  }

  function ckCommentHref(t) {
    var refs = ghRefs(t);
    var id = t.wait && t.wait.comment_id ? String(t.wait.comment_id) : '';
    if (!refs || !/^[0-9]+$/.test(id)) return null;
    return 'https://github.com/' + refs.repo + '/issues/' + (refs.pr || refs.issue) + '#issuecomment-' + id;
  }

  function ckSafeQaUrl(url) {
    if (typeof url !== 'string' || url.indexOf('..') >= 0 || url.indexOf('//') >= 0) return null;
    return CK_QA_PATH.test(url) ? url : null;
  }

  function ckRunUrl(vqa) {
    if (!vqa || typeof vqa !== 'object') return null;
    var direct = ckSafeQaUrl(vqa.run_url);
    if (direct) return direct;
    if (Array.isArray(vqa.run) && vqa.run.length) return ckSafeQaUrl('/qa/runs/' + vqa.run.join(''));
    return null;
  }

  function ckMediaUrl(vqa, m) {
    if (!m || typeof m !== 'object') return null;
    var direct = ckSafeQaUrl(m.url);
    if (direct) return direct;
    var runUrl = ckRunUrl(vqa);
    if (!runUrl || typeof m.lane !== 'number' || m.lane < 1 || !Array.isArray(m.path_parts) || !m.path_parts.length) return null;
    return ckSafeQaUrl(runUrl + '/lane-' + m.lane + '/' + m.path_parts.join(''));
  }

  function ckMediaGallery(parent, vqa) {
    if (!vqa || typeof vqa !== 'object') return;
    var items = Array.isArray(vqa.media) ? vqa.media : [];
    if (!items.length) {
      var runUrl = ckRunUrl(vqa);
      var empty = add(parent, el('p', 'empty', 'Visual QA evidence '));
      if (runUrl) {
        var a = add(empty, link(null, runUrl));
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
        a.textContent = 'open QA run';
      } else {
        empty.appendChild(document.createTextNode('pending upload'));
      }
      return;
    }
    var grid = add(parent, el('div', 'ck-gallery'));
    items.forEach(function (m) {
      var url = ckMediaUrl(vqa, m);
      if (!url) return;
      var label = String(m.scenario || (Array.isArray(m.path_parts) ? m.path_parts.join('') : m.path) || 'evidence');
      var fig = add(grid, el('figure', 'ck-media'));
      if (m.kind === 'video') {
        var vid = add(fig, el('video'));
        vid.setAttribute('controls', '');
        vid.setAttribute('preload', 'metadata');
        vid.setAttribute('src', url);
      } else {
        var img = add(fig, el('img'));
        img.setAttribute('src', url);
        img.setAttribute('alt', label);
        img.setAttribute('loading', 'lazy');
      }
      var cap = add(fig, el('figcaption'));
      var a = add(cap, link(null, url));
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
      a.textContent = label;
      if (m.result) add(cap, chip(String(m.result), m.result === 'verified' ? 'ok' : m.result === 'failed' ? 'bad' : 'muted'));
    });
  }

  function ckQuestionsBlock(parent, t) {
    var qs = Array.isArray(t.questions) ? t.questions : [];
    if (!qs.length) return;
    var ul = add(parent, el('ul', 'ck-qlist'));
    qs.forEach(function (q) {
      if (!q || !q.text) return;
      var li = add(ul, el('li', 'ck-qitem'));
      add(li, el('div', 'ck-qtext', String(q.text)));
      if (Array.isArray(q.options) && q.options.length) {
        var ol = add(li, el('ul', 'ck-qopts'));
        q.options.forEach(function (opt) { add(ol, el('li', null, String(opt))); });
      }
    });
  }

  function ckStatusMsg(st, res, okText) {
    st.out.replaceChildren();
    st.out.className = 'ck-status';
    if (res.ok) {
      st.out.className = 'ck-status ck-ok';
      ghResult(st.out, res, okText);
      return;
    }
    if (res.status === 409) {
      st.out.className = 'ck-status ck-conflict';
      var why = res.error === 'head-changed'
        ? 'PR head moved' + (res.data && res.data.head ? ' to ' + String(res.data.head).slice(0, 7) : '') + ' — refresh before approving.'
        : res.error === 'already-answered'
          ? 'This question was already answered — refresh to see the latest state.'
          : 'Ticket state changed — refresh to see the latest state.';
      add(st.out, el('span', null, why + ' '));
      ghButton(st.out, 'Refresh', function () { poll(); });
      return;
    }
    st.out.textContent = 'error: ' + res.error;
  }

  function ckEnsureState(key) {
    if (!CK_STATE[key]) {
      var box = el('div', 'ck-form');
      var text = add(box, el('textarea'));
      var row = add(box, el('div', 'ck-form-row'));
      var send = ghButton(row, 'Submit', function () { ckSubmit(CK_STATE[key]); });
      var out = add(row, el('span', 'ck-status'));
      out.setAttribute('role', 'status');
      CK_STATE[key] = {
        key: key, ticket: null, opened: null, action: null, head: null, busy: false,
        box: box, text: text, send: send, out: out
      };
      text.addEventListener('input', function () { ckDrawForm(CK_STATE[key]); });
    }
    return CK_STATE[key];
  }

  function ckHeadBound(action) {
    return ACT.head.indexOf(action) >= 0;
  }

  function ckReadHead(st) {
    var refs = ghRefs(st.ticket);
    if (!refs || !refs.pr) { st.out.textContent = 'error: no-pr'; return; }
    st.busy = true;
    ckDrawForm(st);
    ghPost('/api/head', {repo: refs.repo, pr: refs.pr}, true).then(function (res) {
      st.busy = false;
      st.head = res.ok && SHA.test(String(res.data.head || '')) ? String(res.data.head) : null;
      if (!st.head) st.out.textContent = 'error: ' + (res.error || 'bad-head');
      ckDrawForm(st);
    });
  }

  function ckActionTitle(action, head) {
    var shortSha = head ? ' ' + head.slice(0, 7) : '';
    if (action === 'ok') return head ? 'Approve & Ship' + shortSha : 'Approve & Ship (reading head…)';
    if (action === 'request-changes') return head ? 'Send changes request' + shortSha : 'Request Changes (reading head…)';
    if (action === 'rerun-qa') return head ? 'Re-run QA' + shortSha : 'Re-run QA (reading head…)';
    if (action === 'answer') return 'Post Answer';
    return 'Submit ' + (ACT.label[action] || action || '');
  }

  function ckDrawForm(st) {
    var needHead = ckHeadBound(st.action);
    st.box.hidden = !st.action && !st.out.textContent;
    st.text.hidden = !st.action;
    st.send.hidden = !st.action;
    if (st.action) {
      var ph = st.action === 'answer'
        ? 'Type your answer to the open product question…'
        : st.action === 'request-changes'
          ? 'Describe the visual/UX changes needed…'
          : st.action === 'rerun-qa'
            ? 'Reason to re-run QA (e.g. flake or env glitch)…'
            : 'Approval note (e.g. Visual QA looks good, ship it)…';
      st.text.setAttribute('placeholder', ph);
      st.text.setAttribute('aria-label', ph);
      st.send.textContent = ckActionTitle(st.action, st.head);
      st.send.disabled = st.busy || !st.text.value.trim() || (needHead && !st.head);
    }
  }

  function ckOpenAction(st, action) {
    st.action = st.action === action ? null : action;
    st.opened = st.ticket;
    st.out.replaceChildren();
    st.out.className = 'ck-status';
    st.head = null;
    if (st.action === 'ok' && !st.text.value.trim()) st.text.value = 'Visual QA approved from Mission Control.';
    if (st.action === 'rerun-qa' && !st.text.value.trim()) st.text.value = 'Re-run live QA verification on this head.';
    if (st.action && ckHeadBound(st.action)) ckReadHead(st);
    ckDrawForm(st);
    if (st.action) st.text.focus();
  }

  function ckSubmit(st) {
    var t = st.opened;
    if (!t || !st.action || st.busy) return;
    var payload = {
      repo: t.repo,
      ticket_id: t.ticket_id,
      action: st.action,
      expect_state: t.state,
      expect_why: t.wait ? t.wait.why : null,
      words: st.text.value
    };
    if (st.action === 'answer' && t.wait && 'comment_id' in t.wait) {
      payload.expect_comment_id = t.wait.comment_id;
    }
    if (ckHeadBound(st.action)) payload.head = st.head;
    st.busy = true;
    ckDrawForm(st);
    ghPost('/api/act', payload, true).then(function (res) {
      st.busy = false;
      if (res.ok) {
        st.text.value = '';
        st.action = null;
        st.head = null;
      } else if (res.status === 409 && res.error === 'head-changed' && res.data && SHA.test(String(res.data.head || ''))) {
        st.head = String(res.data.head);
      }
      ckStatusMsg(st, res, 'Posted');
      ckDrawForm(st);
    });
  }

  function ckActionBar(card, t, key) {
    if (!GH.on || !GH.login || !t.wait) return;
    var listed = Array.isArray(t.actions) ? t.actions : [];
    var st = ckEnsureState(key);
    st.ticket = t;
    if (st.action && listed.indexOf(st.action) < 0) st.action = null;
    var bar = add(card, el('div', 'ck-actions'));
    if (listed.indexOf('ok') >= 0) {
      var bOk = ghButton(bar, t.wait.why === 'visual-qa-review' ? 'Approve & Ship' : 'Merge OK',
                         function () { ckOpenAction(st, 'ok'); }, 'ck-approve');
      bOk.setAttribute('data-ck-act', 'ok');
    }
    if (listed.indexOf('request-changes') >= 0) {
      var bChg = ghButton(bar, 'Request Changes', function () { ckOpenAction(st, 'request-changes'); }, 'ck-changes');
      bChg.setAttribute('data-ck-act', 'request-changes');
    }
    if (listed.indexOf('rerun-qa') >= 0) {
      var bQa = ghButton(bar, 'Re-run QA', function () { ckOpenAction(st, 'rerun-qa'); }, 'ck-rerun');
      bQa.setAttribute('data-ck-act', 'rerun-qa');
    }
    if (listed.indexOf('answer') >= 0) {
      var bAns = ghButton(bar, 'Answer Question', function () { ckOpenAction(st, 'answer'); }, 'ck-approve');
      bAns.setAttribute('data-ck-act', 'answer');
    }
    ckDrawForm(st);
    add(card, st.box);
  }

  function ckCard(t, prefix, showTicketLink) {
    var why = (t.wait && t.wait.why) || t.state;
    var isVisual = why === 'visual-qa-review';
    var isQ = why === 'question';
    var card = el('article', 'ck-card' + (isVisual ? ' ck-visual' : isQ ? ' ck-question' : ''));
    card.setAttribute('data-cockpit-ticket', ticketAddress(t));
    var head = add(card, el('div', 'ck-head'));
    add(head, chip(isVisual ? 'visual-qa-review' : t.state, isVisual ? 'accent' : 'warn'));
    if (showTicketLink) {
      add(head, link('ck-tk', ticketPageHref(t))).textContent = ticketOf(t);
    } else {
      add(head, el('span', 'ck-tk', ticketOf(t)));
    }
    add(head, el('span', 'ck-meta', (ACT.why[why] || why)));
    var links = add(head, el('span', 'ck-links'));
    var chref = ckCommentHref(t);
    if (chref) {
      var ca = add(links, link(null, chref));
      ca.setAttribute('target', '_blank');
      ca.setAttribute('rel', 'noopener noreferrer');
      ca.textContent = 'comment ↗';
    }
    var runUrl = ckRunUrl(t.visual_qa);
    if (runUrl) {
      var ra = add(links, link(null, runUrl));
      ra.setAttribute('target', '_blank');
      ra.setAttribute('rel', 'noopener noreferrer');
      ra.textContent = 'full QA report ↗';
    }
    if (t.visual_qa) ckMediaGallery(card, t.visual_qa);
    ckQuestionsBlock(card, t);
    ckActionBar(card, t, ckKey(prefix, t));
    return card;
  }

  function ckInboxCard(t) {
    var why = (t.wait && t.wait.why) || t.state;
    var isVisual = why === 'visual-qa-review';
    var isQ = why === 'question';
    var card = el('article', 'ck-card ck-inbox' + (isVisual ? ' ck-visual' : isQ ? ' ck-question' : ''));
    card.setAttribute('data-cockpit-ticket', ticketAddress(t));
    var main = add(card, el('div', 'ck-main'));
    var head = add(main, el('div', 'ck-head'));
    add(head, chip(isVisual ? 'visual-qa-review' : t.state, isVisual ? 'accent' : 'warn'));
    add(head, link('ck-tk', ticketPageHref(t))).textContent = ticketOf(t);
    var links = add(head, el('span', 'ck-links'));
    var chref = ckCommentHref(t);
    if (chref) {
      var ca = add(links, link(null, chref));
      ca.setAttribute('target', '_blank');
      ca.setAttribute('rel', 'noopener noreferrer');
      ca.textContent = 'comment ↗';
    }
    var runUrl = ckRunUrl(t.visual_qa);
    if (runUrl) {
      var ra = add(links, link(null, runUrl));
      ra.setAttribute('target', '_blank');
      ra.setAttribute('rel', 'noopener noreferrer');
      ra.textContent = 'full QA report ↗';
    }
    var line = add(main, el('div', 'ck-why'));
    add(line, el('span', null, ACT.why[why] || why));
    if (t.state_age_seconds !== null && t.state_age_seconds !== undefined) {
      ticking(add(line, el('span', 'when')), t.state_age_seconds, 'waited ');
    }
    if (t.title) add(main, el('div', 'ck-title', String(t.title)));
    if (t.visual_qa) ckMediaGallery(main, t.visual_qa);
    ckQuestionsBlock(main, t);
    ckActionBar(card, t, ckKey('index', t));
    return card;
  }

  function renderNeedsMax(d) {
    var items = ckInboxTickets(d);
    var root = panel('needs-max', 'Needs Max · Product Inbox',
                     items.length ? items.length + ' waiting on you' : 'all clear');
    if (!root) return;
    if (items.length && !GH.started) { GH.started = true; ghStart(); }
    if (!items.length) {
      add(root, el('p', 'empty', 'No open product questions or visual QA holds waiting on you.'));
      return;
    }
    NEEDS_MAX.groups.forEach(function (g, i) {
      var rows = items.filter(function (t) { return ckInboxGroup(t) === i; });
      if (!rows.length) return;
      var box = add(root, el('div', 'ck-group'));
      box.setAttribute('data-ck-group', g.key);
      var head = add(box, el('h3', 'ck-ghead'));
      add(head, el('span', null, g.label));
      add(head, el('span', 'count', String(rows.length)));
      var grid = add(box, el('div', 'ck-grid'));
      rows.slice(0, NEEDS_MAX.limit).forEach(function (t) { add(grid, ckInboxCard(t)); });
      var more = rows.slice(NEEDS_MAX.limit);
      if (!more.length) return;
      var fold = details(box, 'needs-max-more-' + g.key, 'show ' + more.length + ' more', 'ck-more');
      var rest = add(fold, el('div', 'ck-grid'));
      more.forEach(function (t) { add(rest, ckInboxCard(t)); });
      // A poll can push the card Max is typing in past the limit; keep it in view.
      if (more.some(function (t) { var st = CK_STATE[ckKey('index', t)]; return st && st.action; })) fold.open = true;
    });
  }

  function ckHasTicketCockpit(t) {
    if (!t || (t.state !== 'waiting-max' && t.state !== 'awaiting-ok')) return false;
    return !!(t.visual_qa || (Array.isArray(t.questions) && t.questions.length) ||
              (t.wait && t.wait.why === 'visual-qa-review'));
  }

  function renderTicketCockpit(view, d, found) {
    if (!found || !ckHasTicketCockpit(found.ticket)) return;
    var t = found.ticket;
    var sec = el('section', 'jd-panel ck-ticket');
    sec.setAttribute('id', 'ticket-cockpit');
    var h2 = add(sec, el('h2'));
    add(h2, el('span', 't', 'Needs Max · Product & Visual QA Gate'));
    add(h2, el('span', 'count', (t.wait && t.wait.why) || t.state));
    add(sec, ckCard(t, 'ticket', false));
    add(view, sec);
  }
