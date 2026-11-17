from __future__ import annotations

import re

from lib.dashboard_pages.common import (
    _BOTS, _chip, _e, github_links, _job_hrefs, _panel, _span, _thread_key, _ticket, duration, ticket_href,
    ticket_page_href,
)


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
.rl-filter{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
@media (scripting:none){#relay .rl-filter,#relay .rl-more{display:none}#relay .rl-early{display:contents}}
.rl-btn{height:44px;padding:0 16px;font-family:var(--mono);font-size:12px;cursor:pointer;
border:1px solid var(--line-hi);background:var(--panel);color:var(--text)}
.rl-btn[aria-pressed=true]{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 9%,transparent);color:var(--ok)}
.rl-btn:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.rl-rows{display:flex;flex-direction:column;font-family:var(--mono);font-size:12px}
.rl-row{display:grid;grid-template-columns:104px minmax(0,1fr);align-items:baseline;gap:8px;padding:9px 6px;
border-bottom:1px solid var(--line)}
.rl-tk{font-size:13px;font-weight:600}
.rl-steps{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;min-width:0}
.rl-step{--h:var(--muted);display:inline-flex;flex-wrap:wrap;align-items:baseline;gap:0 6px;max-width:100%;padding:1px 8px;
border:1px solid color-mix(in srgb,var(--h) 40%,transparent);background:color-mix(in srgb,var(--h) 9%,transparent);white-space:nowrap}
.rl-step.b2b{border-style:dashed}
.rl-claude{--h:var(--claude)}.rl-codex{--h:var(--codex)}.rl-cursor{--h:var(--cursor)}.rl-grok{--h:var(--grok)}
.rl-bot{color:var(--h)}.rl-what{color:var(--text)}.rl-when{color:var(--muted)}
.rl-sep{color:var(--muted)}
.rl-early{display:none}
.rl-more{height:22px;padding:0 8px;font-family:inherit;font-size:11px;line-height:1;cursor:pointer;
border:1px solid var(--line-hi);background:var(--bg);color:var(--text-2);white-space:nowrap}
.rl-more:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.rl-verdict{display:inline-flex;flex-wrap:wrap;align-items:baseline;gap:0 6px;max-width:100%;white-space:nowrap}
.rl-out{color:var(--text-2)}
.rl-done{display:block;flex:1 0 100%}
.rl-legend{display:flex;flex-wrap:wrap;align-items:center;gap:6px 20px;margin:12px 0 0;font-family:var(--mono);font-size:12px;
color:var(--muted)}
.rl-key{display:inline-flex;align-items:center;gap:8px}
.rl-sw{display:inline-block;width:28px;height:14px;border:1px solid color-mix(in srgb,var(--muted) 60%,transparent);
background:color-mix(in srgb,var(--muted) 9%,transparent)}
.rl-sw.b2b{border:1px dashed color-mix(in srgb,var(--grok) 60%,transparent);background:color-mix(in srgb,var(--grok) 9%,transparent)}
.rl-note{margin:8px 0 0;font-size:12px;line-height:1.5;color:var(--muted)}
"""


SCRIPT = r"""
  // --- activity page ---------------------------------------------------------

  var ATTENTION = {stuck: 'bad', overdue: 'bad', dead: 'bad', 'needs-attention': 'bad', stale: 'warn', paused: 'warn',
                   down: 'bad', 'no-waiter': 'bad', health: 'bad'};
  var VERDICT = {routed: 'accent', shipped: 'ok', stale: 'muted', rejected: 'warn', stuck: 'bad',
                 blocked: 'bad', waiting: 'warn', unstuck: 'accent', other: 'muted'};
  var TONE_TINT = {warn: '#ff9e64', bad: '#f7768e', muted: '#8b93b8'};
  var DONE_TINT = '#9ece6a';
  var RELAY_FILTERS = [['all', 'all'], ['scripted', 'scripted'], ['wakes', 'grok wakes'], ['shipped', 'shipped']];
  var RELAY_NOTE = 'The relay shows the 12 tickets that moved most recently. A stopped ticket stays listed until it moves again.';
  var RELAY_WINDOW = 6;
  var rlFilter = 'all';
  var rlOpen = {};

  function cmpStep(a, b) {
    return b.at - a.at || (a.type === 'verdict') - (b.type === 'verdict') || a.tie - b.tie;
  }

  function relayThreads(d) {
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
      return {
        key: threadKey(t), ref: t, steps: ordered,
        scripted: ordered.some(function (s) { return s.type === 'handoff' && s.row.kind === 'scripted'; }),
        wakes: ordered.some(function (s) { return s.type === 'handoff' && s.row.kind === 'bot_to_bot'; }),
        shipped: ordered.some(function (s) { return s.type === 'verdict' && s.row.kind === 'shipped'; })
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

  function relayMore(steps, t, hidden) {
    var b = add(steps, el('button', 'rl-more', '+' + hidden + ' earlier'));
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
    var hrefs = jobHrefs(d);
    var bar = add(root, el('div', 'rl-filter'));
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', 'Filter');
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
        add(row, link('rl-tk', hrefs[t.key] ? ticketPageHref(t.ref) : 'job.html')).textContent = ticketOf(t.ref);
        var steps = add(row, el('span', 'rl-steps'));
        var hidden = t.steps.length - RELAY_WINDOW;
        if (hidden > 0) relayMore(steps, t, hidden);
        var shut = hidden > 0 && !rlOpen[t.key] ? hidden : 0;
        t.steps.forEach(function (s, i) {
          if (i < shut) {
            var early = add(steps, el('span', 'rl-early'));
            relayStep(early, s);
            relaySep(early);
            return;
          }
          if (i > shut) relaySep(steps);
          relayStep(steps, s);
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
    add(root, el('p', 'rl-note', RELAY_NOTE));
  }

  function renderAttention(d) {
    var items = attentionItems(d);
    var root = panel('attention', 'Needs attention', items.length + (items.length === 1 ? ' item' : ' items'));
    if (!root) return;
    if (!items.length) { add(root, el('p', 'empty', 'all clear')); return; }
    var hrefs = jobHrefs(d);
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
      add(row, link('at-go', x.component ? healthLink(x.link) : ticketHref(hrefs, x))).textContent =
        x.component ? 'open in System' : 'open ticket';
      if (!x.component) addGh(row, x);
      mark('attention', key, row);
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
_RELAY_FILTERS = (("all", "all"), ("scripted", "scripted"), ("wakes", "grok wakes"), ("shipped", "shipped"))
RELAY_WINDOW = 6
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


def relay_threads(snapshot: dict) -> list[dict]:
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
        found.append({
            "key": _thread_key(item), "ref": item, "steps": ordered,
            "scripted": ("handoff", "scripted") in kinds,
            "wakes": ("handoff", "bot_to_bot") in kinds,
            "shipped": ("verdict", "shipped") in kinds,
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


def _windowed(key: str, items: list[str]) -> str:
    """The RELAY_WINDOW newest items; each older one sits, with the arrow
    after it, in an rl-early box that only script hides."""
    hidden = len(items) - RELAY_WINDOW
    if hidden <= 0:
        return _SEP.join(items)
    more = (
        f'<button class="rl-more" type="button" aria-expanded="false" data-more="{_e(key)}" '
        f'title="show {hidden} earlier step{"" if hidden == 1 else "s"}">+{hidden} earlier</button>'
    )
    early = "".join(f'<span class="rl-early">{item}{_SEP}</span>' for item in items[:hidden])
    return more + early + _SEP.join(items[hidden:])


def _relay(snapshot: dict) -> str:
    threads = relay_threads(snapshot)
    hrefs = _job_hrefs(snapshot)
    buttons = "".join(
        f'<button class="rl-btn" type="button" aria-pressed="{"true" if key == "all" else "false"}" '
        f'data-relay="{key}">{text}</button>'
        for key, text in _RELAY_FILTERS
    )
    rows = "".join(
        f'<div class="rl-row" data-thread="{_e(t["key"])}">'
        f'<a class="rl-tk" href="{_e(ticket_page_href(t["ref"]) if t["key"] in hrefs else "job.html")}">{_e(_ticket(t["ref"]))}</a>'
        f'<span class="rl-steps">{_windowed(t["key"], [_relay_step(step) for step in t["steps"]])}</span></div>'
        for t in threads
    )
    body = f'<div class="rl-rows">{rows}</div>' if rows else '<p class="empty">no handoffs or verdicts yet</p>'
    legend = (
        '<div class="rl-legend">'
        '<span class="rl-key"><span class="rl-sw" aria-hidden="true"></span><span>handoff</span></span>'
        '<span class="rl-key"><span class="rl-sw b2b" aria-hidden="true"></span><span>bot-to-bot (grok wake)</span></span>'
        '<span class="rl-key"><span class="chip accent" aria-hidden="true">routed</span><span>verdict</span></span>'
        '<span class="rl-key"><span class="chip bad" aria-hidden="true">failed</span><span>failed handoff</span></span></div>'
        f'<p class="rl-note">{_RELAY_NOTE}</p>'
    )
    return _panel(
        "relay", "Relay",
        f'<div class="rl-filter" role="group" aria-label="Filter">{buttons}</div>{body}{legend}',
        count=f'{len(threads)} of {snapshot["relay"]["tracked"]} tickets · newest activity first',
    )


def _px():
    from lib.dashboard_pages import shell

    return shell


def _attention_key(row: dict) -> str:
    return f"{row['kind']}:{row.get('component') or str(row['repo']) + '#' + str(row['ticket_id'])}"


def _attention(snapshot: dict) -> str:
    attention = snapshot["attention"]
    hrefs = _job_hrefs(snapshot)

    def draw(row: dict) -> str:
        component = row.get("component")
        key = _attention_key(row)
        tone = _ATTENTION.get(row["kind"], "muted")
        strip = _px().px_strip(f"attention-{key}", effect="static", tint=_TONE_TINT[tone], cell=4, height=24)
        href = _health_link(row.get("link")) if component else ticket_href(hrefs, row)
        return (
            f'<div class="at-row" data-attn="{_e(key)}"><span class="at-px" aria-hidden="true">{strip}</span>'
            + _chip(row["kind"], tone)
            + _span("at-subj", component or _ticket(row))
            + _span("at-what", row["detail"])
            + f'<a class="at-go" href="{_e(href)}">{"open in System" if component else "open ticket"}</a>'
            + ("" if component else github_links(row))
            + "</div>"
        )

    items = attention["items"]
    count = attention["count"]
    inner = f'<div class="at-rows">{"".join(draw(row) for row in items)}</div>' if items else (
        '<p class="empty">all clear</p>'
    )
    return _panel("attention", "Needs attention", inner, count=f"{count} item" + ("" if count == 1 else "s"))
