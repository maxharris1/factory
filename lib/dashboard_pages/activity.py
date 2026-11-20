from __future__ import annotations

import json
import re

from lib.dashboard_pages.common import (
    _BOTS, _JOB_STATE, _chip, _e, github_links, github_refs, _panel, _span, _thread_key, _ticket, duration,
    ticket_page_href,
)
from lib.max_actions import ACTIONS, Why


CSS = """\
#relay{grid-column:1/-1}
.at-rows{display:flex;flex-direction:column}
.at-row{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:10px 6px;border-bottom:1px solid var(--line)}
.at-row:last-child{border-bottom:0}
.at-px{display:block;flex:0 0 12px;width:12px}
.at-subj{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--text)}
.at-what{flex:1 1 280px;min-width:0;font-size:12px;color:var(--text-2)}
.at-go,.rl-tk{color:var(--text);text-decoration:none}
.at-go:hover,.rl-tk:hover{color:var(--ok)}
.at-go{margin-left:auto;font-family:var(--mono);font-size:12px;white-space:nowrap}
.rl-filter{display:inline-flex;margin:0 0 12px}
@media (scripting:none){#relay .rl-filter,#relay .rl-more{display:none}#relay .rl-early,#relay .rl-early-phone{display:contents}}
.rl-btn{height:28px;padding:0 14px;font-family:var(--mono);font-size:12px;line-height:1;white-space:nowrap;cursor:pointer;
border:1px solid var(--line-hi);background:var(--panel);color:var(--text)}
.rl-btn+.rl-btn{margin-left:-1px}
.rl-btn[aria-pressed=true]{position:relative;z-index:1;border-color:var(--ok);
background:color-mix(in srgb,var(--ok) 9%,transparent);color:var(--ok)}
.rl-btn:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.rl-rows{display:flex;flex-direction:column;font-family:var(--mono);font-size:12px}
.rl-row{display:grid;grid-template-columns:104px minmax(0,1fr);align-items:baseline;gap:8px;padding:9px 6px;
border-bottom:1px solid var(--line)}
.rl-tk{font-size:13px;font-weight:600}
.rl-id{display:contents}
.rl-early-phone{display:contents}
.rl-more-phone{display:none}
.rl-steps{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;min-width:0}
.rl-step{--h:var(--muted);display:inline-flex;flex-wrap:wrap;align-items:baseline;gap:0 6px;max-width:100%;padding:1px 8px;
border:1px solid color-mix(in srgb,var(--h) 40%,transparent);background:color-mix(in srgb,var(--h) 9%,transparent);white-space:nowrap}
.rl-step.b2b{border-style:dashed}
.rl-claude{--h:var(--claude)}.rl-codex{--h:var(--codex)}.rl-cursor{--h:var(--cursor)}.rl-grok{--h:var(--grok)}
.rl-other{--h:var(--muted)}
.rl-bot{color:var(--h)}.rl-what{color:var(--text)}.rl-when{color:var(--muted)}
.rl-sep{color:var(--muted)}
.rl-early{display:none}
.rl-more{height:22px;padding:0 8px;font-family:inherit;font-size:11px;line-height:1;cursor:pointer;
border:1px solid var(--line-hi);background:var(--bg);color:var(--text-2);white-space:nowrap}
.rl-more:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.rl-verdict{display:inline-flex;flex-wrap:wrap;align-items:baseline;gap:0 6px;max-width:100%;white-space:nowrap}
.rl-out{color:var(--text-2)}
.rl-done{display:block;flex:1 0 100%}
.rl-now{display:inline-flex;flex-wrap:wrap;align-items:baseline;gap:0 6px;max-width:100%;white-space:nowrap}
.rl-now.live,.rl-blk{display:inline-flex;flex-wrap:wrap;align-items:center;gap:6px;max-width:100%;padding:1px 8px;
border:1px solid var(--h);background:color-mix(in srgb,var(--h) 9%,transparent);white-space:nowrap}
.rl-now.stale{align-items:center;gap:4px 6px}
.rl-now.queued{padding:1px 8px;border:1px solid var(--line-hi);background:var(--bg)}
.rl-px{display:block;flex:0 0 40px;width:40px}
.rl-run{color:var(--text)}.rl-wait,.rl-say{color:var(--text-2)}.rl-late{color:var(--warn)}
.rl-legend{display:flex;flex-wrap:wrap;align-items:center;gap:6px 20px;margin:12px 0 0;font-family:var(--mono);font-size:12px;
color:var(--muted)}
.rl-key{display:inline-flex;align-items:center;gap:8px}
.rl-sw{display:inline-block;width:28px;height:14px;border:1px solid color-mix(in srgb,var(--muted) 60%,transparent);
background:color-mix(in srgb,var(--muted) 9%,transparent)}
.rl-sw.b2b{border:1px dashed color-mix(in srgb,var(--grok) 60%,transparent);background:color-mix(in srgb,var(--grok) 9%,transparent)}
.rl-sw.now{border:1px solid var(--muted)}
.rl-find{margin-left:auto;white-space:nowrap}
.rl-note{margin:8px 0 0;font-size:12px;line-height:1.5;color:var(--muted)}
@media (max-width:699.98px){
.rl-filter{display:flex}
.rl-btn{flex:1 1 auto;padding:0 6px}
.rl-row{display:flex;flex-direction:column;align-items:stretch;gap:6px;padding:10px 0}
.rl-id{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:28px}
.rl-steps>.rl-more,.rl-early-phone{display:none}
.rl-more-phone{display:inline-block;height:28px}
.rl-steps{gap:0 6px}
.rl-sep{flex:0 0 100%;height:4px;overflow:hidden;visibility:hidden}
.rl-now.live{flex:1 1 auto}
.rl-now.live .rl-px{flex:1 1 24px;min-width:24px;width:auto}
.rl-legend{gap:6px 16px}
.rl-find{margin-left:0}
}
.at-why,.at-acts,.at-box{flex:1 0 100%;padding-left:22px;font-size:12px}
.at-why{order:1;display:flex;flex-wrap:wrap;gap:4px 10px;color:var(--text-2)}
.at-why a{color:var(--accent)}
.at-acts{order:2;display:flex;flex-wrap:wrap;gap:6px}
.at-box,.at-form{display:flex;flex-direction:column;gap:6px}
.at-box{order:3;font-family:var(--mono)}
.at-box[hidden],.at-form[hidden]{display:none}
.at-acts button,.at-box button,.at-box textarea{font:12px var(--mono);color:var(--text);background:var(--field);
border:1px solid var(--line-hi)}
.at-acts button,.at-box button{min-height:28px;padding:0 12px;cursor:pointer;align-self:flex-start}
.at-acts button[aria-expanded=true]{border-color:var(--ok);color:var(--ok)}
.at-box button:disabled{cursor:default;opacity:.5}
.at-box textarea{width:100%;min-height:64px;padding:6px 8px;resize:vertical}
.at-acts button:focus-visible,.at-box button:focus-visible,.at-box textarea:focus-visible{outline:2px solid var(--brand);
outline-offset:2px}
.at-out{color:var(--text-2)}.at-out a{color:var(--accent)}
"""


_SCRIPT = r"""
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
    var p = {x: null, action: null, head: null, busy: false};
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
    var ok = p.action === 'ok';
    p.form.hidden = !p.action;
    p.root.hidden = !p.action && !p.out.textContent;
    if (p.action) p.text.setAttribute('aria-label', 'Your words for ' + ACT.label[p.action]);
    p.send.textContent = !ok ? 'Send ' + (ACT.label[p.action] || '') : p.head ? 'OK ' + p.head : 'OK (reading head…)';
    p.send.disabled = p.busy || !p.text.value.trim() || (ok && !p.head);
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
    p.out.textContent = '';
    p.head = null;
    if (p.action === 'ok') atHead(p);
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
    var x = p.x;
    var body = {repo: x.repo, ticket_id: x.ticket_id, action: p.action, expect_state: x.state,
                expect_why: x.wait.why, words: p.text.value};
    if (p.action === 'ok') body.head = p.head;
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
"""


# --- activity page -----------------------------------------------------------------


_ATTENTION = {
    "stuck": "bad", "overdue": "bad", "dead": "bad", "needs-attention": "bad",
    "stale": "warn", "paused": "warn", "down": "bad", "no-waiter": "bad", "health": "bad",
}


_VERDICT = {"routed": "accent", "shipped": "ok", "stale": "muted", "rejected": "warn",
            "stuck": "bad", "blocked": "bad", "waiting": "warn", "unstuck": "accent", "other": "muted"}


_TONE_TINT = {"warn": "#ff9e64", "bad": "#f7768e", "muted": "#8b93b8"}
_RELAY_FILTERS = (("all", "all"), ("needs", "needs me"), ("shipped", "shipped"), ("wakes", "grok wakes"))
RELAY_WINDOW = 6
RELAY_PHONE_WINDOW = 4
_NEEDS_ME = {"waiting", "stopped", "stale"}
_RELAY_NOTE = "The relay shows the 12 tickets that moved most recently. A stopped ticket stays listed until it moves again."


_HEALTH_LINK = re.compile(r"health\.html#[A-Za-z0-9_-]+")


def _health_link(href: object) -> str:
    """Only a link into the Health tab (#92); anything else is the tab itself."""
    return str(href) if isinstance(href, str) and _HEALTH_LINK.fullmatch(href) else "health.html"


def _place_untimed(ordered: list[dict], verdicts: list[dict]) -> None:
    for index, step in enumerate(verdicts):
        if step["at"] is not None:
            continue
        previous = verdicts[index - 1] if index else None
        at = next(i for i, s in enumerate(ordered) if s is previous) + 1 if previous else 0
        ordered.insert(at, step)


_NOW_TITLE = ' title="where the ticket is now"'


def _bot_class(name: object) -> str:
    return f"rl-{name if name in _BOTS else 'other'}"


def _now_job(row: dict, strip: str) -> str:
    return (
        _span("rl-bot", row["recipient"]) + _span("rl-what", row["action"])
        + f'<span class="rl-px" aria-hidden="true">{strip}</span>'
    )


def _lasting(cls: str, lead: str, age: object) -> str:
    if age is None:
        return _span(cls, lead) if lead else ""
    return _span(cls, (f"{lead} " if lead else "") + "for " + duration(age))


def _now_running(at: dict) -> str:
    row = at["running"]
    px = _px()
    strip = px.px_strip(f"relay-now-{row['job']}", state=px.px_state(row), bot=row["recipient"], cell=2, height=10)
    return (
        f'<span class="rl-now {_bot_class(row["recipient"])} live"{_NOW_TITLE}>{_now_job(row, strip)}'
        f'{_span("rl-run", "running " + duration(row["elapsed_seconds"]))}</span>'
    )


def _now_stale(at: dict) -> str:
    row = at["running"]
    strip = _px().px_strip(f"relay-now-{row['job']}", state="stale", bot=row["recipient"], still=True, cell=2,
                           height=10)
    cause = _JOB_STATE.get(str(row.get("stale_cause")))
    return (
        f'<span class="rl-now stale"{_NOW_TITLE}><span class="rl-blk {_bot_class(row["recipient"])}">'
        f'{_now_job(row, strip)}</span>'
        + _chip(cause[0] if cause else row["job_state"], "warn" if row["job_state"] == "stale" else "bad")
        + _span("rl-late", "no heartbeat for " + duration(row["heartbeat_age_seconds"]))
        + "</span>"
    )


def _now_queued(at: dict) -> str:
    row = at["queued"]
    return (
        f'<span class="rl-now queued {_bot_class(row["recipient"])}"{_NOW_TITLE}>'
        f'{_span("rl-bot", row["recipient"])}{_span("rl-what", row["action"])}'
        f'{_span("rl-wait", "queued " + duration(row["age_seconds"]))}</span>'
    )


def _now_stopped(at: dict) -> str:
    now = at["now"]
    lead = f"in {now['stopped_in']}" if now["stopped_in"] else ""
    return f'<span class="rl-now"{_NOW_TITLE}>{_chip(now["state"], "bad")}{_lasting("rl-say", lead, now["age_seconds"])}</span>'


def _now_shipped(at: dict) -> str:
    now = at["now"]
    age = now["age_seconds"]
    return (
        f'<span class="rl-now"{_NOW_TITLE}>{_chip("shipped", "ok")}'
        + _span("rl-out", "handed off" if now["how"] == "handed-off" else "merged")
        + ("" if age is None else _span("rl-when", duration(age) + " ago"))
        + "</span>"
    )


def _now_waiting(at: dict) -> str:
    now = at["now"]
    return (
        f'<span class="rl-now"{_NOW_TITLE}>{_chip("waiting", "warn")}'
        + _lasting("rl-say", f"on {now['waiting_on']}", now["age_seconds"]) + "</span>"
    )


def _now_idle(at: dict) -> str:
    now = at["now"]
    return f'<span class="rl-now"{_NOW_TITLE}>' + _lasting("rl-when", f"in {now['state']}", now["age_seconds"]) + "</span>"


def _live_state(at: dict) -> object:
    return at["running"]["job_state"] if at["running"] else None


def _rests(at: dict) -> object:
    return at["now"]["kind"] if at["now"] else None


_ENDINGS = (
    ("running", lambda at: _live_state(at) == "running", _now_running),
    ("stale", lambda at: _live_state(at) in ("stale", "dead"), _now_stale),
    ("queued", lambda at: at["queued"] is not None, _now_queued),
    ("stopped", lambda at: _rests(at) == "stopped", _now_stopped),
    ("shipped", lambda at: _rests(at) == "shipped" and not at["said"], _now_shipped),
    ("waiting", lambda at: _rests(at) == "idle" and bool(at["now"]["waiting_on"]), _now_waiting),
    ("idle", lambda at: _rests(at) == "idle", _now_idle),
)


def _live_rows(snapshot: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    running: dict[str, dict] = {}
    for row in snapshot["running"]:
        key = _thread_key(row)
        if key not in running or (row.get("elapsed_seconds") or 0) < (running[key].get("elapsed_seconds") or 0):
            running[key] = row
    queued: dict[str, dict] = {}
    for row in snapshot["queued"]:
        queued.setdefault(_thread_key(row), row)
    return running, queued


def relay_threads(snapshot: dict) -> list[dict]:
    running, queued = _live_rows(snapshot)
    found = []
    for item in snapshot["relay"]["threads"]:
        handoffs = [{"type": "handoff", "row": row, "at": row["age_seconds"], "tie": -index}
                    for index, row in enumerate(item["handoffs"])]
        verdicts = sorted(({"type": "verdict", "row": row, "at": row.get("age_seconds"), "tie": row["seq"]}
                           for row in item["verdicts"]), key=lambda s: s["row"]["seq"])
        ordered = sorted(
            (step for step in [*handoffs, *verdicts] if step["at"] is not None),
            key=lambda step: (-step["at"], step["type"] == "verdict", step["tie"]),
        )
        _place_untimed(ordered, verdicts)
        kinds = {(step["type"], step["row"]["kind"]) for step in ordered}
        key = _thread_key(item)
        at = {"running": running.get(key), "queued": queued.get(key), "now": item.get("now"),
              "said": bool(ordered) and (ordered[-1]["type"], ordered[-1]["row"]["kind"]) == ("verdict", "shipped")}
        name, ending = next(((name, draw) for name, when, draw in _ENDINGS if when(at)), (None, None))
        found.append({
            "key": key, "ref": item, "steps": ordered, "at": at, "ending": ending,
            "needs": name in _NEEDS_ME,
            "wakes": ("handoff", "bot_to_bot") in kinds,
            "shipped": name == "shipped" or ("verdict", "shipped") in kinds,
        })
    return found


def _relay_step(step: dict) -> str:
    row = step["row"]
    if step["type"] == "verdict":
        age = row.get("age_seconds")
        chip = _chip(row["chip"], _VERDICT.get(row["chip"], "muted")) if row["chip"] else _chip(row["kind"])
        return (
            f'<span class="rl-verdict" title="{_e(row["outcome"])}">{chip}'
            f'{_span("rl-out", row["words"] or row["outcome"])}'
            + ("" if age is None else _span("rl-when", duration(age) + " ago"))
            + "</span>"
        )
    bot = row["recipient"] if row["recipient"] in _BOTS else "other"
    b2b = " b2b" if row["kind"] == "bot_to_bot" else ""
    return (
        f'<span class="rl-step rl-{bot}{b2b}">{_span("rl-bot", row["recipient"])}{_span("rl-what", row["action"])}'
        f'{_span("rl-when", duration(row["age_seconds"]) + " ago")}</span>'
        + (_chip("failed", "bad") if row["kind"] == "failed" else "")
    )


_SEP = '<span class="rl-sep" aria-hidden="true">→</span>'


def _items(thread: dict) -> list[str]:
    ending = [thread["ending"](thread["at"])] if thread["ending"] else []
    return [*(_relay_step(step) for step in thread["steps"]), *ending]


def _more(cls: str, key: str, hidden: int) -> str:
    if hidden <= 0:
        return ""
    return (
        f'<button class="{cls}" type="button" aria-expanded="false" data-more="{_e(key)}" '
        f'title="show {hidden} earlier step{"" if hidden == 1 else "s"}">+{hidden} earlier</button>'
    )


def _windowed(key: str, items: list[str]) -> str:
    hidden = max(len(items) - RELAY_WINDOW, 0)
    phone = max(len(items) - RELAY_PHONE_WINDOW, 0)
    early = "".join(f'<span class="rl-early">{item}{_SEP}</span>' for item in items[:hidden])
    early_phone = "".join(f'<span class="rl-early-phone">{item}{_SEP}</span>' for item in items[hidden:phone])
    return _more("rl-more", key, hidden) + early + early_phone + _SEP.join(items[phone:])


def _relay_row(thread: dict) -> str:
    items = _items(thread)
    href = ticket_page_href(thread["ref"])
    return (
        f'<div class="rl-row" data-thread="{_e(thread["key"])}"><span class="rl-id">'
        f'<a class="rl-tk" href="{_e(href)}">{_e(_ticket(thread["ref"]))}</a>'
        f'{_more("rl-more rl-more-phone", thread["key"], len(items) - RELAY_PHONE_WINDOW)}</span>'
        f'<span class="rl-steps">{_windowed(thread["key"], items)}</span></div>'
    )


def _relay(snapshot: dict) -> str:
    threads = relay_threads(snapshot)
    buttons = "".join(
        f'<button class="rl-btn" type="button" aria-pressed="{"true" if key == "all" else "false"}" '
        f'data-relay="{key}">{text}</button>'
        for key, text in _RELAY_FILTERS
    )
    rows = "".join(_relay_row(t) for t in threads)
    body = f'<div class="rl-rows">{rows}</div>' if rows else '<p class="empty">no handoffs or verdicts yet</p>'
    legend = (
        '<div class="rl-legend">'
        '<span class="rl-key"><span class="rl-sw" aria-hidden="true"></span><span>handoff</span></span>'
        '<span class="rl-key"><span class="rl-sw b2b" aria-hidden="true"></span><span>bot-to-bot (grok wake)</span></span>'
        '<span class="rl-key"><span class="chip accent" aria-hidden="true">routed</span><span>verdict</span></span>'
        '<span class="rl-key"><span class="chip bad" aria-hidden="true">failed</span><span>failed handoff</span></span>'
        '<span class="rl-key"><span class="rl-sw now" aria-hidden="true"></span><span>where the ticket is now</span></span>'
        f'<a class="rl-find" href="job.html">find any of the {_e(snapshot["relay"]["tracked"])} tickets</a></div>'
        f'<p class="rl-note">{_RELAY_NOTE}</p>'
    )
    return _panel(
        "relay", "Relay",
        f'<div class="rl-filter" role="group" aria-label="Filter the relay">{buttons}</div>{body}{legend}',
        count=f'{len(threads)} of {snapshot["relay"]["tracked"]} tickets · newest activity first',
    )


def _px():
    from lib.dashboard_pages import shell

    return shell


def _attention_key(row: dict) -> str:
    return f"{row['kind']}:{row.get('component') or str(row['repo']) + '#' + str(row['ticket_id'])}"


_WHY: dict[Why, str] = {
    "awaiting-ok": "waits for your merge OK",
    "plan-size": "plan is over the size limit",
    "question": "asks you a question",
    "repeat-stop": "review repeated an earlier finding",
    "unsat": "review calls a criterion unsatisfiable",
    "capped": "hit the review round cap",
    "blocked": "blocked",
    "stuck": "stuck",
    "waiting-external": "waits on someone outside the factory",
}


def _comment_href(row: dict) -> str | None:
    """The comment that explains the wait (commentHref in the script)."""
    refs = github_refs(row)
    comment_id = str(row["wait"].get("comment_id"))
    if refs is None or not re.fullmatch(r"[0-9]+", comment_id):
        return None
    return f"https://github.com/{refs['repo']}/issues/{refs['pr'] or refs['issue']}#issuecomment-{comment_id}"


def _wait(row: dict) -> str:
    wait = row["wait"]
    href = _comment_href(row)
    link = "" if href is None else (
        f'<a href="{_e(href)}" rel="noopener noreferrer" target="_blank">comment ↗</a>'
    )
    text = _WHY.get(wait["why"], wait["why"]) + (f": {wait['detail']}" if wait.get("detail") else "")
    return f'<span class="at-why">{_span(None, text)}{link}</span>'


def _attention(snapshot: dict) -> str:
    attention = snapshot["attention"]

    def draw(row: dict) -> str:
        component = row.get("component")
        key = _attention_key(row)
        tone = _ATTENTION.get(row["kind"], "muted")
        strip = _px().px_strip(f"attention-{key}", effect="static", tint=_TONE_TINT[tone], cell=4, height=24)
        href = _health_link(row.get("link")) if component else ticket_page_href(row)
        return (
            f'<div class="at-row" data-attn="{_e(key)}"><span class="at-px" aria-hidden="true">{strip}</span>'
            + _chip(row["kind"], tone)
            + _span("at-subj", component or _ticket(row))
            + _span("at-what", row["detail"])
            + (_wait(row) if row.get("wait") else "")
            + f'<a class="at-go" href="{_e(href)}">{"open in System" if component else "open ticket"}</a>'
            + ("" if component else github_links(row))
            + "</div>"
        )

    items = sorted(attention["items"], key=lambda row: _px()._tone_rank(row["kind"]))
    count = attention["count"]
    inner = f'<div class="at-rows">{"".join(draw(row) for row in items)}</div>' if items else (
        '<p class="empty">all clear</p>'
    )
    label = f"{count} item" + ("" if count == 1 else "s") + (" · worst first" if items else "")
    return _panel("attention", "Needs attention", inner, count=label)


SCRIPT = _SCRIPT.replace("__ACT__", json.dumps(
    {"label": {name: spec.label for name, spec in ACTIONS.items()}, "why": _WHY},
))
