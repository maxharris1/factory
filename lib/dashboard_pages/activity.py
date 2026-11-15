"""activity.html (Activity): Needs attention and the Relay threads (Activity.dc.html)."""

from __future__ import annotations

import re

from lib.dashboard_pages.common import _BOTS, _chip, _e, _panel, _span, _ticket, anchor, duration


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
.rl-btn{height:44px;padding:0 16px;font-family:var(--mono);font-size:12px;cursor:pointer;
border:1px solid var(--line-hi);background:var(--panel);color:var(--text)}
.rl-btn[aria-pressed=true]{border-color:var(--ok);background:color-mix(in srgb,var(--ok) 9%,transparent);color:var(--ok)}
.rl-btn:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.rl-rows{display:flex;flex-direction:column;font-family:var(--mono);font-size:12px}
.rl-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;padding:9px 6px;border-bottom:1px solid var(--line)}
.rl-tk{flex:0 0 104px;font-size:13px;font-weight:600}
.rl-step{--h:var(--muted);display:inline-flex;align-items:baseline;gap:6px;padding:1px 8px;
border:1px solid color-mix(in srgb,var(--h) 40%,transparent);background:color-mix(in srgb,var(--h) 9%,transparent);white-space:nowrap}
.rl-step.b2b{border-style:dashed}
.rl-claude{--h:var(--claude)}.rl-codex{--h:var(--codex)}.rl-cursor{--h:var(--cursor)}.rl-grok{--h:var(--grok)}
.rl-bot{color:var(--h)}.rl-what{color:var(--text)}.rl-when{color:var(--muted)}
.rl-sep{color:var(--muted)}
.rl-verdict{display:inline-flex;align-items:baseline;gap:6px;white-space:nowrap}
.rl-out{color:var(--text-2)}
.rl-done{display:block;flex:1 0 100%}
.rl-legend{display:flex;flex-wrap:wrap;align-items:center;gap:6px 20px;margin:12px 0 0;font-family:var(--mono);font-size:12px;
color:var(--muted)}
.rl-key{display:inline-flex;align-items:center;gap:8px}
.rl-sw{display:inline-block;width:28px;height:14px;border:1px solid color-mix(in srgb,var(--muted) 60%,transparent);
background:color-mix(in srgb,var(--muted) 9%,transparent)}
.rl-sw.b2b{border:1px dashed color-mix(in srgb,var(--grok) 60%,transparent);background:color-mix(in srgb,var(--grok) 9%,transparent)}
"""


SCRIPT = r"""
  // --- activity page ---------------------------------------------------------

  var ATTENTION = {stuck: 'bad', overdue: 'bad', dead: 'bad', 'needs-attention': 'bad', stale: 'warn', paused: 'warn',
                   down: 'bad', 'no-waiter': 'bad', health: 'bad'};
  var VERDICT = {routed: 'accent', shipped: 'ok', stale: 'muted', rejected: 'warn', stuck: 'bad',
                 blocked: 'bad', waiting: 'warn', other: 'muted'};
  var ATTENTION_TINT = '#f7768e';
  var DONE_TINT = '#9ece6a';
  var RELAY_FILTERS = [['all', 'all'], ['scripted', 'scripted'], ['wakes', 'grok wakes'], ['shipped', 'shipped']];
  var rlFilter = 'all';

  function threadKey(r) { return String(r.repo).toLowerCase() + '#' + r.ticket_id; }

  function jobHrefs(d) {
    var hrefs = {}, best = {};
    (d.running || []).forEach(function (r) {
      var key = threadKey(r);
      if (!best[key] || (r.elapsed_seconds || 0) < (best[key].elapsed_seconds || 0)) best[key] = r;
    });
    (d.queued || []).forEach(function (r) { if (!best[threadKey(r)]) best[threadKey(r)] = r; });
    Object.keys(best).forEach(function (key) { hrefs[key] = 'job.html#' + anchor(best[key].job); });
    return hrefs;
  }

  function cmpStep(a, b) {
    return b.at - a.at || (a.type === 'verdict') - (b.type === 'verdict') || a.tie - b.tie;
  }

  function relayThreads(d) {
    var byKey = new Map();
    function thread(r) {
      var key = threadKey(r);
      if (!byKey.has(key)) byKey.set(key, {key: key, ref: r, steps: []});
      return byKey.get(key);
    }
    (d.handoff_savings.recent || []).forEach(function (r, i) {
      thread(r).steps.push({type: 'handoff', row: r, at: r.age_seconds, tie: -i});
    });
    (d.poller.verdicts || []).forEach(function (r) {
      var at = r.age_seconds === null || r.age_seconds === undefined ? null : r.age_seconds;
      thread(r).steps.push({type: 'verdict', row: r, at: at, tie: r.seq});
    });
    var threads = [];
    byKey.forEach(function (t) {
      var ordered = t.steps.filter(function (s) { return s.at !== null; }).sort(cmpStep);
      var verdicts = t.steps.filter(function (s) { return s.type === 'verdict'; })
        .sort(function (a, b) { return a.row.seq - b.row.seq; });
      verdicts.forEach(function (v, i) {
        if (v.at === null) ordered.splice(i ? ordered.indexOf(verdicts[i - 1]) + 1 : 0, 0, v);
      });
      var timed = ordered.filter(function (s) { return s.at !== null; }).map(function (s) { return s.at; });
      threads.push({
        key: t.key, ref: t.ref, steps: ordered,
        newest: timed.length ? Math.min.apply(null, timed) : null,
        oldest: timed.length ? Math.max.apply(null, timed) : null,
        top: verdicts.length ? verdicts[verdicts.length - 1].row.seq : -1,
        scripted: ordered.some(function (s) { return s.type === 'handoff' && s.row.kind === 'scripted'; }),
        wakes: ordered.some(function (s) { return s.type === 'handoff' && s.row.kind === 'bot_to_bot'; }),
        shipped: ordered.some(function (s) { return s.type === 'verdict' && s.row.kind === 'shipped'; })
      });
    });
    return threads.sort(function (a, b) {
      return (a.newest === null) - (b.newest === null) || (a.newest || 0) - (b.newest || 0) || b.top - a.top ||
        (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
    });
  }

  function relayCount(threads) {
    var oldest = threads.reduce(function (m, t) { return t.oldest === null ? m : Math.max(m, t.oldest); }, -1);
    return threads.length + (threads.length === 1 ? ' ticket' : ' tickets') + (oldest < 0 ? '' : ' · last ' + dur(oldest));
  }

  function stillMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function relayStep(row, s) {
    var r = s.row;
    if (s.type === 'verdict') {
      var said = add(row, el('span', 'rl-verdict'));
      add(said, chip(r.kind, VERDICT[r.kind] || 'muted'));
      add(said, el('span', 'rl-out', r.outcome));
      return;
    }
    var step = add(row, el('span', 'rl-step rl-' + (BOTS[r.recipient] ? r.recipient : 'other') +
                                   (r.kind === 'bot_to_bot' ? ' b2b' : '')));
    add(step, el('span', 'rl-bot', r.recipient));
    add(step, el('span', 'rl-what', r.action));
    ticking(add(step, el('span', 'rl-when')), r.age_seconds, '', ' ago');
    if (r.kind === 'failed') add(row, chip('failed', 'bad'));
  }

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
        if (!firstRender && !known[key]) played[t.key] = true;
      });
    });
    var shown = all.filter(function (t) { return rlFilter === 'all' || t[rlFilter]; });
    var root = panel('relay', 'Relay', relayCount(shown));
    if (!root) return;
    var focused = document.activeElement;
    var bar = add(root, el('div', 'rl-filter'));
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', 'Filter');
    RELAY_FILTERS.forEach(function (f) {
      var b = add(bar, el('button', 'rl-btn', f[1]));
      b.setAttribute('type', 'button');
      b.setAttribute('aria-pressed', rlFilter === f[0] ? 'true' : 'false');
      b.setAttribute('data-relay', f[0]);
      b.addEventListener('click', function () { rlFilter = f[0]; if (latest) render(latest); });
      if (focused && focused.getAttribute('data-relay') === f[0]) b.focus();
    });
    if (!all.length) add(root, el('p', 'empty', 'no handoffs or verdicts yet'));
    else if (!shown.length) add(root, el('p', 'empty', 'no ticket matches this filter'));
    else {
      var hrefs = jobHrefs(d);
      var still = stillMotion();
      var rows = add(root, el('div', 'rl-rows'));
      shown.forEach(function (t) {
        var row = add(rows, el('div', 'rl-row'));
        row.setAttribute('data-thread', t.key);
        add(row, link('rl-tk', hrefs[t.key] || 'job.html')).textContent = ticketOf(t.ref);
        t.steps.forEach(function (s, i) {
          if (i) add(row, el('span', 'rl-sep', '→')).setAttribute('aria-hidden', 'true');
          relayStep(row, s);
        });
        if (played[t.key] && !still) {
          var box = add(row, el('span', 'rl-done'));
          box.setAttribute('aria-hidden', 'true');
          add(box, PX.strip('relay-' + t.key, {effect: 'off', tint: DONE_TINT, done: true, cell: 4, height: 8}));
        }
      });
    }
    var legend = add(root, el('div', 'rl-legend'));
    legendKey(legend, el('span', 'rl-sw'), 'scripted handoff');
    legendKey(legend, el('span', 'rl-sw b2b'), 'bot-to-bot (grok wake)');
    legendKey(legend, chip('routed', 'accent'), 'verdict');
  }

  function renderAttention(d) {
    var items = attentionItems(d);
    var root = panel('attention', 'Needs attention', items.length + (items.length === 1 ? ' item' : ' items'));
    if (!root) return;
    if (!items.length) { add(root, el('p', 'empty', 'all clear')); return; }
    var hrefs = jobHrefs(d);
    var rows = add(root, el('div', 'at-rows'));
    items.forEach(function (x) {
      var key = x.kind + ':' + (x.component || x.repo + '#' + x.ticket_id);
      var row = add(rows, el('div', 'at-row'));
      var blk = add(row, el('span', 'at-px'));
      blk.setAttribute('aria-hidden', 'true');
      add(blk, PX.strip('attention-' + key, {effect: 'static', tint: ATTENTION_TINT, cell: 4, height: 24}));
      add(row, chip(x.kind, ATTENTION[x.kind] || 'muted'));
      add(row, el('span', 'at-subj', x.component || ticketOf(x)));
      add(row, el('span', 'at-what', x.detail));
      add(row, link('at-go', x.component ? healthLink(x.link) : hrefs[threadKey(x)] || 'job.html')).textContent =
        x.component ? 'open in System' : 'open ticket';
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
            "stuck": "bad", "blocked": "bad", "waiting": "warn", "other": "muted"}


_ATTENTION_TINT = "#f7768e"
_RELAY_FILTERS = (("all", "all"), ("scripted", "scripted"), ("wakes", "grok wakes"), ("shipped", "shipped"))


_HEALTH_LINK = re.compile(r"health\.html#[A-Za-z0-9_-]+")


def _health_link(href: object) -> str:
    """Only a link into the Health tab (#92); anything else is the tab itself."""
    return str(href) if isinstance(href, str) and _HEALTH_LINK.fullmatch(href) else "health.html"


def _thread_key(row: dict) -> str:
    return f"{str(row['repo']).lower()}#{row['ticket_id']}"


def _job_hrefs(snapshot: dict) -> dict[str, str]:
    """Each ticket's job view: its running job with the least elapsed time, else its first queued job."""
    best: dict[str, dict] = {}
    for row in snapshot["running"]:
        key = _thread_key(row)
        if key not in best or (row.get("elapsed_seconds") or 0) < (best[key].get("elapsed_seconds") or 0):
            best[key] = row
    for row in snapshot["queued"]:
        best.setdefault(_thread_key(row), row)
    return {key: f"job.html#{anchor(row['job'])}" for key, row in best.items()}


def relay_threads(snapshot: dict) -> list[dict]:
    """Every handoff and verdict in the snapshot, grouped into one thread per ticket.

    The ordering rule (the script's relayThreads is the same rule):

    1. Each handoff and verdict lands in exactly one thread, keyed by repo (any case) and ticket id.
    2. In a thread, timed steps go oldest first. On a tie a handoff goes before a verdict, because the
       poller handles a comment after the job that posted it; two verdicts tie-break by handled order
       (`seq`), two handoffs by the snapshot's order.
    3. A verdict with no recorded time goes directly after the verdict the poller handled just before it
       in the thread, or first when it is the thread's first verdict.
    4. Threads go newest timed step first. Threads with no timed step go last, newest handled verdict first.
    """
    threads: dict[str, dict] = {}

    def thread(row: dict) -> dict:
        return threads.setdefault(_thread_key(row), {"key": _thread_key(row), "ref": row, "steps": []})

    for index, row in enumerate(snapshot["handoff_savings"]["recent"]):
        thread(row)["steps"].append({"type": "handoff", "row": row, "at": row["age_seconds"], "tie": -index})
    for row in snapshot["poller"].get("verdicts") or ():
        thread(row)["steps"].append({"type": "verdict", "row": row, "at": row.get("age_seconds"), "tie": row["seq"]})

    found = []
    for item in threads.values():
        ordered = sorted(
            (step for step in item["steps"] if step["at"] is not None),
            key=lambda step: (-step["at"], step["type"] == "verdict", step["tie"]),
        )
        verdicts = sorted((step for step in item["steps"] if step["type"] == "verdict"), key=lambda s: s["row"]["seq"])
        for index, step in enumerate(verdicts):
            if step["at"] is None:
                at = next(i for i, s in enumerate(ordered) if s is verdicts[index - 1]) + 1 if index else 0
                ordered.insert(at, step)
        timed = [step["at"] for step in ordered if step["at"] is not None]
        kinds = {(step["type"], step["row"]["kind"]) for step in ordered}
        found.append({
            "key": item["key"], "ref": item["ref"], "steps": ordered,
            "newest": min(timed) if timed else None, "oldest": max(timed) if timed else None,
            "top": verdicts[-1]["row"]["seq"] if verdicts else -1,
            "scripted": ("handoff", "scripted") in kinds,
            "wakes": ("handoff", "bot_to_bot") in kinds,
            "shipped": ("verdict", "shipped") in kinds,
        })
    return sorted(
        found, key=lambda t: (t["newest"] is None, t["newest"] or 0, -t["top"], t["key"])
    )


def _relay_count(threads: list[dict]) -> str:
    oldest = max((t["oldest"] for t in threads if t["oldest"] is not None), default=None)
    count = f"{len(threads)} ticket" + ("" if len(threads) == 1 else "s")
    return count if oldest is None else f"{count} · last {duration(oldest)}"


def _relay_step(step: dict) -> str:
    row = step["row"]
    if step["type"] == "verdict":
        return (
            f'<span class="rl-verdict">{_chip(row["kind"], _VERDICT.get(row["kind"], "muted"))}'
            f'{_span("rl-out", row["outcome"])}</span>'
        )
    bot = row["recipient"] if row["recipient"] in _BOTS else "other"
    b2b = " b2b" if row["kind"] == "bot_to_bot" else ""
    return (
        f'<span class="rl-step rl-{bot}{b2b}">{_span("rl-bot", row["recipient"])}{_span("rl-what", row["action"])}'
        f'{_span("rl-when", duration(row["age_seconds"]) + " ago")}</span>'
        + (_chip("failed", "bad") if row["kind"] == "failed" else "")
    )


def _relay(snapshot: dict) -> str:
    threads = relay_threads(snapshot)
    hrefs = _job_hrefs(snapshot)
    buttons = "".join(
        f'<button class="rl-btn" type="button" aria-pressed="{"true" if key == "all" else "false"}" '
        f'data-relay="{key}">{text}</button>'
        for key, text in _RELAY_FILTERS
    )
    sep = '<span class="rl-sep" aria-hidden="true">→</span>'
    rows = "".join(
        f'<div class="rl-row" data-thread="{_e(t["key"])}">'
        f'<a class="rl-tk" href="{_e(hrefs.get(t["key"], "job.html"))}">{_e(_ticket(t["ref"]))}</a>'
        + sep.join(_relay_step(step) for step in t["steps"])
        + "</div>"
        for t in threads
    )
    body = f'<div class="rl-rows">{rows}</div>' if rows else '<p class="empty">no handoffs or verdicts yet</p>'
    legend = (
        '<div class="rl-legend">'
        '<span class="rl-key"><span class="rl-sw" aria-hidden="true"></span><span>scripted handoff</span></span>'
        '<span class="rl-key"><span class="rl-sw b2b" aria-hidden="true"></span><span>bot-to-bot (grok wake)</span></span>'
        '<span class="rl-key"><span class="chip accent" aria-hidden="true">routed</span><span>verdict</span></span></div>'
    )
    return _panel(
        "relay", "Relay",
        f'<div class="rl-filter" role="group" aria-label="Filter">{buttons}</div>{body}{legend}',
        count=_relay_count(threads),
    )


def _px():
    from lib.dashboard_pages import shell

    return shell


def _attention(snapshot: dict) -> str:
    attention = snapshot["attention"]
    hrefs = _job_hrefs(snapshot)

    def draw(row: dict) -> str:
        component = row.get("component")
        key = f"{row['kind']}:{component or str(row['repo']) + '#' + str(row['ticket_id'])}"
        strip = _px().px_strip(f"attention-{key}", effect="static", tint=_ATTENTION_TINT, cell=4, height=24)
        href = _health_link(row.get("link")) if component else hrefs.get(_thread_key(row), "job.html")
        return (
            f'<div class="at-row"><span class="at-px" aria-hidden="true">{strip}</span>'
            + _chip(row["kind"], _ATTENTION.get(row["kind"], "muted"))
            + _span("at-subj", component or _ticket(row))
            + _span("at-what", row["detail"])
            + f'<a class="at-go" href="{_e(href)}">{"open in System" if component else "open ticket"}</a></div>'
        )

    items = attention["items"]
    count = attention["count"]
    inner = f'<div class="at-rows">{"".join(draw(row) for row in items)}</div>' if items else (
        '<p class="empty">all clear</p>'
    )
    return _panel("attention", "Needs attention", inner, count=f"{count} item" + ("" if count == 1 else "s"))
