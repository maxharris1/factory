"""index.html (Lanes): factory status (with Linear webhook health when a listener has run),
lanes and slots, the queue rail, and the job modal script."""

from __future__ import annotations

from lib.pr_title import label
from lib.dashboard_pages.activity import _health_link
from lib.dashboard_pages.common import (
    anchor, _bot, _chip, duration, _e, _feed, _job_kind, _job_label, _num, _panel, _span,
    _state_chip, _ticket, _when,
)


CSS = """\
.kpis{display:flex;flex-wrap:wrap;gap:12px}
.kpi{flex:1 1 140px;padding:10px 14px 12px;background:var(--bg);box-shadow:var(--elev)}
.kpi b{display:block;font:600 30px/1.15 var(--mono);letter-spacing:-.03em;color:var(--text);font-variant-numeric:tabular-nums}
.kpi span{font:500 12px/1.3 var(--mono);color:var(--muted)}
.kpi.accent b{color:var(--accent)}.kpi.ok b{color:var(--ok)}.kpi.warn b{color:var(--warn)}.kpi.bad b{color:var(--bad)}
.devices{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.device{padding:12px;background:var(--bg);box-shadow:var(--elev)}
.device.disabled{color:var(--muted);box-shadow:none;outline:1px dashed var(--line-hi);outline-offset:-1px;
background:repeating-linear-gradient(135deg,var(--dim) 0 8px,var(--bg) 8px 16px)}
.device.disabled .name{color:var(--muted)}
.device header{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.device .name{font:600 14px/1 var(--mono);letter-spacing:-.01em;color:var(--text)}
.device .cap{margin-left:auto;font:12px/1 var(--mono);color:var(--muted)}
.device .harness{margin:0 0 10px;font-size:12px;color:var(--muted)}
.slots{display:grid;gap:6px}
.slot{min-height:54px;padding:8px 10px;border:1px solid transparent;background:var(--panel-hi)}
.slot.idle{display:flex;align-items:center;justify-content:center;border:1px dashed var(--line-hi);background:transparent;
font:500 11px/1 var(--mono);letter-spacing:.12em;text-transform:lowercase;color:var(--muted)}
.job .top{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px}
.job .ticket{font:600 13px/1.25 var(--mono);color:var(--text);overflow-wrap:anywhere}
.job .action{font-size:12px;color:var(--text-2)}
.job .times{display:flex;justify-content:space-between;gap:8px;margin:5px 0 6px;font:12px/1.2 var(--mono);color:var(--muted)}
.job .times b{font-weight:600;color:var(--text)}
.job.overdue{border-color:var(--bad);box-shadow:inset 0 0 0 1px var(--bad)}
.hb{height:3px;background:var(--line-hi);overflow:hidden}
.hb i{display:block;height:100%;width:0;background-color:var(--ok);
background-image:linear-gradient(90deg,transparent 0,rgba(255,255,255,.35) 50%,transparent 100%);
background-size:200% 100%;animation:shimmer 2.4s linear infinite;transition:width .9s linear}
.hb.warn i{background-color:var(--warn)}.hb.bad i{background-color:var(--bad)}
"""


CSS_LINKS = """\
a.slot,a.queue-item,.jobs a{color:inherit;text-decoration:none}
a.slot{display:block}
a.slot:hover,a.slot:focus-visible{border-color:var(--brand)}
#lanes .devices{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
#lanes .slot{min-height:84px}
.slot.held{display:flex;align-items:center;justify-content:center;border:1px dashed var(--warn);background:transparent;
font:500 11px/1 var(--mono);letter-spacing:.12em;text-transform:lowercase;color:var(--warn)}
.job .step{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:3px;font-size:12px;color:var(--muted)}
.job .step b{font-weight:600;color:var(--text)}
.kind{padding:0 4px;color:var(--muted);font-size:10px}
.queue-item{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;width:100%}
.queue-item:hover .ticket,.jobs a:hover .ticket{color:var(--brand)}
"""


CSS_MODAL = """\
.modal{position:fixed;inset:0;z-index:10;display:flex;align-items:flex-start;justify-content:center;
padding:6vh 16px;overflow-y:auto;background:color-mix(in srgb,var(--ink) 78%,transparent)}
.modal-box{position:relative;width:min(1300px,100%);background:var(--panel);box-shadow:var(--elev-hi)}
.modal-box .jobpage{box-shadow:none}
.modal-box .jobpage h2{padding-right:92px}
.modal-box .more{padding:0 16px 14px}
.modal-close{position:absolute;top:10px;right:12px;z-index:1;padding:2px 10px;border:1px solid var(--line-hi);
background:var(--panel-hi);color:var(--text-2);font:500 12px/1.6 var(--mono);cursor:pointer}
.modal-close:hover{color:var(--text);border-color:var(--brand)}
.modal-close:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
body.modal-open{overflow:hidden}
"""


SCRIPT = r"""
  // --- main page: status, lanes and slots, the queue rail ------------------

  function renderKpis(d) {
    var root = panel('kpis', 'Factory status');
    if (!root) return;
    var t = d.queue.totals;
    var stuck = d.stuck.tickets.length + d.stuck.overdue_jobs.length;
    var row = add(root, el('div', 'kpis'));
    [
      ['running', t.running, 'accent'],
      ['queued', t.queued, t.queued ? 'warn' : ''],
      ['done', t.done, 'ok'],
      ['failed', t.failed, t.failed ? 'bad' : ''],
      ['stuck / overdue', stuck, stuck ? 'bad' : 'ok']
    ].forEach(function (k) {
      var tile = add(row, el('div', 'kpi ' + k[2]));
      add(tile, el('b', null, num(k[1])));
      add(tile, el('span', null, k[0]));
    });
  }

  // Webhook health (#56): a small block appended to the Factory status
  // panel that renderKpis has just rebuilt, so it is redrawn from
  // status.json on every poll; absent when no listener has run. The heading
  // is split so a page without a listener never contains the phrase
  // (tests/test_linear_webhook_health.py).
  function renderLinearWebhook(d) {
    var root = byId('kpis');
    var w = d.poller && d.poller.linear_webhook;
    if (!root || !w) return;
    var received;
    if (w.last_received_age_seconds !== null && w.last_received_age_seconds !== undefined) {
      received = dur(w.last_received_age_seconds) + ' ago' + (w.last_event ? ' (' + w.last_event + ')' : '');
    } else if (w.listener_age_seconds === null || w.listener_age_seconds === undefined) {
      received = 'never (listener has not started)';
    } else {
      received = 'never';
    }
    var box = add(root, el('div'));
    box.setAttribute('id', 'linear-webhook');
    add(box, el('h2', null, 'Linear' + ' webhook'));
    add(box, el('p', null, 'last received ' + received + ' · dirty queue ' + w.dirty));
    if (w.warning !== null && w.warning !== undefined) add(box, el('p', 'bad', 'warning: ' + w.warning));
    if (w.note !== null && w.note !== undefined) add(box, el('p', 'muted', 'note: ' + w.note));
  }

  // Backlog feeder (#85): appended to Factory status like the webhook block.
  var FEEDER_STATE = {'eligible': 'eligible', 'waiting-capacity': 'waiting for capacity',
    'waiting-blocked': 'waiting on a blocker', 'feeding': 'feeding', 'error': 'feed error, retrying',
    'fed': 'fed', 'done': 'fed, job queued', 'closed': 'PR closed, not fed'};
  function renderFeeder(d) {
    var root = byId('kpis');
    var f = d.feeder;
    if (!root || !f) return;
    var box = add(root, el('div'));
    box.setAttribute('id', 'feeder');
    add(box, el('h2', null, 'Feeder'));
    add(box, el('p', null, 'eligible ' + f.eligible + ' · fed ' + f.fed +
      ' · waiting for capacity ' + f.waiting_capacity +
      ' · waiting on blockers or files ' + f.waiting_blocked));
    var list = add(box, el('ul'));
    (f.items || []).forEach(function (item) {
      var tiered = item.tier !== null && item.tier !== undefined;
      var li = add(list, el('li', null, item.repo + '#' + item.issue + ' ' +
        (item.reason || FEEDER_STATE[item.state] || item.state) +
        (item.pr ? ' → PR #' + item.pr : '') + (tiered ? ' ' : '')));
      tierChip(li, item.tier);
    });
  }

  function tierChip(parent, tier) {
    if (tier !== null && tier !== undefined) add(parent, chip('tier ' + tier, 'accent'));
  }

  function slotCard(r) {
    var card = opensModal(link('slot job' + (r.overdue ? ' overdue' : ''), 'job.html#' + anchor(r.job)), r.job);
    card.title = ticketIn(r.repo, r) + ' (job ' + r.job + ')';
    var top = add(card, el('div', 'top'));
    add(top, bot(r.recipient));
    var kind = jobKind(r);
    if (kind) add(top, el('span', 'kind', kind));
    add(top, el('span', 'ticket', jobLabel(r)));
    var state = stateChip(r);
    if (state) add(top, state);
    if (r.overdue) add(top, chip('OVERDUE', 'bad'));
    var step = add(card, el('div', 'step'));
    add(step, el('span', 'action', r.action));
    add(step, el('span', 'round', 'round ' + (r.round === null || r.round === undefined ? '—' : r.round)));
    var times = add(card, el('div', 'times'));
    var elapsed = add(times, el('span'));
    if (r.job_state === 'claimed') {
      // Claimed and never started: no runner, so no T+ or heartbeat bar.
      elapsed.title = 'no orch job run has started this job';
      add(elapsed, el('span', null, 'claimed '));
      ticking(add(elapsed, el('b')), r.elapsed_seconds, '', ' ago');
      add(times, el('span', 'warn', 'not started'));
      return mark('lanes', r.job, card);
    }
    elapsed.title = r.elapsed_from === 'placed' ? 'since it was placed on this device' : 'since it was queued';
    add(elapsed, el('span', null, 'T+ '));
    ticking(add(elapsed, el('b')), r.elapsed_seconds);
    var beat = add(times, el('span'));
    beat.title = 'last heartbeat';
    add(beat, el('span', null, 'hb '));
    ticking(add(beat, el('b')), r.heartbeat_age_seconds, '', ' ago');
    var bar = add(card, el('div', 'hb'));
    var fill = add(bar, el('i'));
    var base = r.heartbeat_age_seconds;
    var timeout = r.timeout_seconds || 1;
    var item = {run: function (dt) {
      var frac = Math.min(1, (base + dt) / timeout);
      fill.style.width = Math.max(2, Math.round(frac * 100)) + '%';
      bar.className = 'hb' + (frac >= 1 ? ' bad' : frac >= 0.6 ? ' warn' : '');
    }};
    tickers.push(item);
    item.run(since());
    return mark('lanes', r.job, card);
  }

  function renderLanes(d) {
    var running = d.running || [];
    var root = panel('lanes', 'Lanes and slots', running.length + ' running');
    if (!root) return;
    var lanes = laneDevices(d);
    var grid = add(root, el('div', 'devices'));
    lanes.devices.forEach(function (dev) {
      var jobs = dev.jobs;
      var used = dev.used;
      var card = add(grid, el('section', 'device' + (dev.enabled ? '' : ' disabled')));
      card.setAttribute('data-device', dev.name);
      var head = add(card, el('header'));
      add(head, el('span', 'name', dev.name));
      if (!dev.enabled) add(head, chip('DISABLED', 'muted'));
      else if (dev.placeholder) add(head, chip('PLACEHOLDER', 'warn'));
      else if (!dev.listed) add(head, chip('UNLISTED', 'warn'));
      else add(head, chip('ONLINE', 'ok'));
      add(head, el('span', 'cap', used + ' / ' + (dev.slot_max === null || dev.slot_max === undefined ? '?' : dev.slot_max) + ' slots'));
      if (dev.harnesses.length) add(card, el('p', 'harness', dev.harnesses.join(' · ')));
      var slots = add(card, el('div', 'slots'));
      jobs.forEach(function (r) { add(slots, slotCard(r)); });
      var held = used - jobs.length;
      for (var h = 0; h < held; h++) add(slots, el('div', 'slot held', 'held'));
      var free = Math.max(0, (dev.slot_max || 0) - used);
      if (!used && !free) free = 1;
      for (var i = 0; i < free; i++) add(slots, el('div', 'slot idle', dev.enabled ? 'idle' : 'off'));
    });
    var wakes = lanes.wakes;
    if (wakes.length) {
      var card = add(grid, el('section', 'device'));
      card.setAttribute('data-device', 'grok-wakes');
      var head = add(card, el('header'));
      add(head, el('span', 'name', 'grok wakes'));
      add(head, chip('BOT-TO-BOT', 'accent'));
      var slots = add(card, el('div', 'slots'));
      wakes.forEach(function (r) { add(slots, slotCard(r)); });
    }
  }

  function renderQueue(d) {
    var rows = d.queued || [];
    var root = panel('queue', 'Queue', rows.length + ' waiting');
    if (!root) return;
    list(root, rows, 'queue empty', function (r) {
      var li = el('li');
      var a = add(li, opensModal(link('queue-item', 'job.html#' + anchor(r.job)), r.job));
      add(a, bot(r.recipient));
      add(a, el('span', 'ticket', jobLabel(r)));
      tierChip(a, r.tier);
      add(a, el('span', 'what', r.action));
      ticking(add(a, el('span', 'when')), r.age_seconds, 'waiting ');
      if (r.no_waiter) add(li, link('chip bad', healthLink(r.no_waiter_link))).textContent = 'no waiter for ' + r.no_waiter;
      return mark('queue', r.job, li);
    });
  }
"""


SCRIPT_MODAL = r"""
  // --- the job modal on the lanes page (#78) ----------------------------------

  // A slot or queue link opens its job's flow in a modal; without script (or
  // with a modifier key) the link still goes to job.html#job-<id>.
  function opensModal(a, job) {
    if (PAGE !== 'main') return a;
    a.addEventListener('click', function (ev) {
      if (ev.defaultPrevented || ev.button || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      openModal(job, a);
    });
    if (modal && modal.job === job) modal.opener = a;
    return a;
  }

  function fillModal() {
    if (!modal || !latest) return;
    var box = modal.box;
    box.replaceChildren();
    var close = add(box, el('button', 'modal-close', '✕ close'));
    close.setAttribute('type', 'button');
    close.setAttribute('aria-label', 'Close');
    close.addEventListener('click', function () { closeModal(); });
    modal.close = close;
    var found = activeJobs(latest).filter(function (j) { return j.row.job === modal.job; })[0];
    if (found) {
      add(box, jobArticle(found.row, found.state, latest));
      modal.node.setAttribute('aria-label', jobLabel(found.row) + ' flow');
    } else {
      var gone = add(box, el('section', 'panel jobpage'));
      add(add(gone, el('h2')), el('span', null, anchor(modal.job)));
      add(gone, el('p', 'empty', 'That job is no longer running or queued.'));
      var ended = finishedTelemetry(latest, modal.job);
      if (ended) add(gone, telemetryBlock(ended));
    }
    var more = add(box, el('p', 'more'));
    add(more, link(null, 'job.html#' + anchor(modal.job))).textContent = 'open on the job page';
  }

  function openModal(job, opener) {
    if (modal && modal.node.parentNode) modal.node.parentNode.removeChild(modal.node);
    var node = el('div', 'modal');
    node.setAttribute('id', 'job-modal');
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-modal', 'true');
    // A click on the backdrop (outside the box) closes it.
    node.addEventListener('click', function (ev) { if (ev.target === node) closeModal(); });
    modal = {job: job, node: node, box: add(node, el('div', 'modal-box')), opener: opener, close: null};
    fillModal();
    add(body, node);
    body.classList.add('modal-open');
    scrollTrails();
    if (modal.close && modal.close.focus) modal.close.focus();
  }

  function closeModal() {
    if (!modal) return;
    var m = modal;
    modal = null;
    if (m.node.parentNode) m.node.parentNode.removeChild(m.node);
    body.classList.remove('modal-open');
    if (m.opener && m.opener.isConnected && m.opener.focus) m.opener.focus();
  }

  document.addEventListener('keydown', function (ev) {
    if (modal && (ev.key === 'Escape' || ev.key === 'Esc')) { ev.preventDefault(); closeModal(); }
  });

  // Long paths scroll; keep the current step (at the right end) in view.
  function scrollTrails() {
    trails.forEach(function (box) { if (box.scrollWidth) box.scrollLeft = box.scrollWidth; });
    trails = [];
  }
"""


# --- main page -------------------------------------------------------------------------


def _kpis(snapshot: dict) -> str:
    totals = snapshot["queue"]["totals"]
    stuck = len(snapshot["stuck"]["tickets"]) + len(snapshot["stuck"]["overdue_jobs"])
    tiles = [
        ("running", totals["running"], "accent"),
        ("queued", totals["queued"], "warn" if totals["queued"] else ""),
        ("done", totals["done"], "ok"),
        ("failed", totals["failed"], "bad" if totals["failed"] else ""),
        ("stuck / overdue", stuck, "bad" if stuck else "ok"),
    ]
    inner = "".join(
        f'<div class="kpi {cls}"><b>{_num(value)}</b>{_span(None, label)}</div>'
        for label, value, cls in tiles
    )
    return _panel(
        "kpis",
        "Factory status",
        f'<div class="kpis">{inner}</div>' + _linear_webhook(snapshot) + _feeder(snapshot),
    )


_FEEDER_STATE = {
    "eligible": "eligible",
    "waiting-capacity": "waiting for capacity",
    "waiting-blocked": "waiting on a blocker",
    "feeding": "feeding",
    "error": "feed error, retrying",
    "fed": "fed",
    "done": "fed, job queued",
    "closed": "PR closed, not fed",
}


def _feeder(snapshot: dict) -> str:
    """The backlog feeder (#85), a small block inside Factory status; only
    once a feeder has run. The script's renderFeeder builds the same markup."""
    feeder = snapshot.get("feeder")
    if not feeder:
        return ""
    rows = "".join(
        f"<li>{_e(item['repo'])}#{_e(item['issue'])} "
        f"{_e(item.get('reason') or _FEEDER_STATE.get(item['state'], item['state']))}"
        + (f" → PR #{_e(item['pr'])}" if item.get("pr") else "")
        + ("" if item.get("tier") is None else " " + _tier_chip(item["tier"]))
        + "</li>"
        for item in feeder["items"]
    )
    return (
        '<div id="feeder"><h2>Feeder</h2>'
        f"<p>eligible {_e(feeder['eligible'])} · fed {_e(feeder['fed'])} · "
        f"waiting for capacity {_e(feeder['waiting_capacity'])} · "
        f"waiting on blockers or files {_e(feeder.get('waiting_blocked', 0))}</p>"
        f"<ul>{rows}</ul></div>"
    )


def _tier_chip(tier: object) -> str:
    return "" if tier is None else _chip(f"tier {tier}", "accent")


def _linear_webhook(snapshot: dict) -> str:
    """Linear webhook health (#56), a small block inside Factory status; only
    when a listener or webhook-mode poller has run on this database. The
    script's renderLinearWebhook builds the same markup from status.json."""
    webhook = snapshot["poller"].get("linear_webhook")
    if webhook is None:
        return ""
    if webhook["last_received_age_seconds"] is not None:
        received = f'{duration(webhook["last_received_age_seconds"])} ago' + (
            f' ({webhook["last_event"]})' if webhook["last_event"] else ""
        )
    elif webhook["listener_age_seconds"] is None:
        received = "never (listener has not started)"
    else:
        received = "never"
    warning = (
        f'<p class="bad">warning: {_e(webhook["warning"])}</p>'
        if webhook["warning"] is not None
        else ""
    )
    if webhook.get("note") is not None:
        warning += f'<p class="muted">note: {_e(webhook["note"])}</p>'
    return (
        '<div id="linear-webhook"><h2>Linear webhook</h2>'
        f"<p>last received {_e(received)} · dirty queue {_e(webhook['dirty'])}</p>"
        f"{warning}</div>"
    )


def _slot(row: dict) -> str:
    kind = _job_kind(row)
    rnd = "—" if row.get("round") is None else row["round"]
    age = row["heartbeat_age_seconds"]
    frac = min(1.0, age / (row["timeout_seconds"] or 1))
    level = " bad" if frac >= 1 else " warn" if frac >= 0.6 else ""
    since = (
        "since it was placed on this device"
        if row.get("elapsed_from") == "placed"
        else "since it was queued"
    )
    if row.get("job_state") == "claimed":
        # Claimed and never started: no runner, so no T+ or heartbeat bar.
        times = (
            '<div class="times"><span title="no orch job run has started this job">'
            f'<span>claimed </span><b>{duration(row["elapsed_seconds"])} ago</b></span>'
            + _span("warn", "not started")
            + "</div>"
        )
    else:
        times = (
            f'<div class="times"><span title="{since}"><span>T+ </span>'
            f'<b>{duration(row["elapsed_seconds"])}</b></span>'
            f'<span title="last heartbeat"><span>hb </span><b>{duration(age)} ago</b></span></div>'
            f'<div class="hb{level}"><i style="width:{max(2, round(frac * 100))}%"></i></div>'
        )
    return (
        f'<a class="slot job{" overdue" if row["overdue"] else ""}" '
        f'href="job.html#{anchor(row["job"])}" '
        f'title="{_e(_ticket(row, full=True))} (job {_e(row["job"])})">'
        f'<div class="top">{_bot(row["recipient"])}'
        + (_span("kind", kind) if kind else "")
        + _span("ticket", _job_label(row))
        + _state_chip(row)
        + (_chip("OVERDUE", "bad") if row["overdue"] else "")
        + "</div>"
        f'<div class="step">{_span("action", row["action"])}{_span("round", f"round {rnd}")}</div>'
        + times
        + "</a>"
    )


def lane_devices(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """The lanes the script draws: (devices with their running jobs, grok
    wakes). Mirrors renderLanes in the page script."""
    slots = snapshot["slots"]
    devices = [
        {
            "name": row["name"],
            "slot_max": row["slot_max"],
            "held": row.get("held") or 0,
            "enabled": row["enabled"],
            "placeholder": row["placeholder"],
            "harnesses": row["harnesses"],
            "listed": True,
            "jobs": [],
        }
        for row in slots["devices"]
    ]
    by_name = {device["name"]: device for device in devices}
    wakes: list[dict] = []
    for row in snapshot["running"]:
        if row["device"] is None:
            wakes.append(row)
            continue
        if row["device"] not in by_name:
            device = {
                "name": row["device"],
                "slot_max": slots["box"]["slot_max"] if row["device"] == "box" else None,
                "held": 0,
                "enabled": True,
                "placeholder": False,
                "harnesses": [],
                "listed": row["device"] == "box" and not devices,
                "jobs": [],
            }
            devices.append(device)
            by_name[row["device"]] = device
        by_name[row["device"]]["jobs"].append(row)
    if not devices:
        devices.append(
            {"name": "box", "slot_max": slots["box"]["slot_max"], "held": 0, "enabled": True,
             "placeholder": False, "harnesses": [], "listed": True, "jobs": []}
        )
    for device in devices:
        # #63 FU-1: a slot held in device_slots is in use even when its job
        # is not (or no longer) running, so a lane counts max(held, running).
        device["used"] = max(device["held"], len(device["jobs"]))
    return devices, wakes


def _lanes(snapshot: dict) -> str:
    devices, wakes = lane_devices(snapshot)
    cards = []
    for device in devices:
        if not device["enabled"]:
            state = _chip("DISABLED", "muted")
        elif device["placeholder"]:
            state = _chip("PLACEHOLDER", "warn")
        elif not device["listed"]:
            state = _chip("UNLISTED", "warn")
        else:
            state = _chip("ONLINE", "ok")
        cap = "?" if device["slot_max"] is None else device["slot_max"]
        used = device["used"]
        held = used - len(device["jobs"])
        free = max(0, (device["slot_max"] or 0) - used)
        if not used and not free:
            free = 1
        slots = "".join(_slot(row) for row in device["jobs"])
        slots += '<div class="slot held">held</div>' * held
        slots += f'<div class="slot idle">{"idle" if device["enabled"] else "off"}</div>' * free
        harness = (
            f'<p class="harness">{_e(" · ".join(device["harnesses"]))}</p>'
            if device["harnesses"]
            else ""
        )
        cards.append(
            f'<section class="device{"" if device["enabled"] else " disabled"}" '
            f'data-device="{_e(device["name"])}">'
            f'<header>{_span("name", device["name"])}{state}'
            f'{_span("cap", f"{used} / {cap} slots")}</header>'
            f'{harness}<div class="slots">{slots}</div></section>'
        )
    if wakes:
        cards.append(
            '<section class="device" data-device="grok-wakes"><header>'
            f'{_span("name", "grok wakes")}{_chip("BOT-TO-BOT", "accent")}</header>'
            f'<div class="slots">{"".join(_slot(row) for row in wakes)}</div></section>'
        )
    return _panel(
        "lanes",
        "Lanes and slots",
        f'<div class="devices">{"".join(cards)}</div>',
        count=f"{len(snapshot['running'])} running",
    )


def _queue(snapshot: dict) -> str:
    rows = snapshot["queued"]

    def draw(row: dict) -> str:
        return (
            f'<a class="queue-item" href="job.html#{anchor(row["job"])}">'
            f'{_bot(row["recipient"])}{_span("ticket", _job_label(row))}{_tier_chip(row.get("tier"))}'
            f'{_span("what", row["action"])}{_when(row["age_seconds"], "waiting ")}</a>'
            + (
                f'<a class="chip bad" href="{_e(_health_link(row.get("no_waiter_link")))}">'
                f'no waiter for {_e(row["no_waiter"])}</a>'
                if row.get("no_waiter")
                else ""
            )
        )

    return _panel(
        "queue", "Queue", _feed(rows, "queue empty", draw),
        count=f"{len(rows)} waiting", extra="rail",
    )
