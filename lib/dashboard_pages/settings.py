"""settings.html (Settings, #777): one card per machines.yml device with its
enabled, slot_max, harnesses, held slots, drain state and its recent change
results from the settings audit log. Signed in through the #563 flow
(`ghAuth` in common.py), each card gets a toggle for enabled and drain and a
slot_max input that POST /api/settings; the orch host applies the queued
request (lib/settings_apply.py)."""

from __future__ import annotations

import json
import math
import time

from lib.dashboard_pages.common import _chip, _e, _panel, _span

RESULT_TONE = {"applied": "ok", "pending": "warn", "ok": "ok", "signed-out": "muted", "mismatch": "warn",
               "expired": "warn", "cancelled": "muted", "refused": "warn", "failed": "bad", "superseded": "muted",
               "other": "muted"}
RESULTS_PER_CARD = 5
QUEUED = "queued (applies within ~2 min)"
READ_ONLY = "read-only: sign-in is not available here"
WAITING = "waiting for the poller's next cycle"
NO_CHANGES = "no changes yet"
SIGN_IN_TONE = {"ok": "ok", "mismatch": "warn", "signed-out": "bad", "unreachable": "warn"}
UNCHECKED = "unchecked"
ACCOUNT_READ_ONLY = {"default": "read-only: default account", "external": "read-only: external account"}
NOT_SIGNED_IN = "not signed in on any device"
NO_ACCOUNTS = "no accounts: machines.yml lists none"
ADD_HARNESSES = ("claude", "codex", "cursor", "gemini")
SIGNIN_HARNESSES = ("claude", "codex")
SIGNIN_REMIND = "Paste a code only from a sign-in you just started here. Never paste a code someone sent you."


def drain_state(row: dict) -> tuple[str, str]:
    if not row.get("drain"):
        return "off", ""
    held = row.get("held") or 0
    return (f"draining, {held} running", "warn") if held else ("drained", "ok")


def device_facts(row: dict) -> list[tuple[str, str, str]]:
    slot_max = "—" if row.get("slot_max") is None else str(row["slot_max"])
    return [
        ("enabled", "yes" if row.get("enabled") else "no", "" if row.get("enabled") else "warn"),
        ("slot_max", slot_max, ""),
        ("harnesses", ", ".join(row.get("harnesses") or []) or "none", ""),
        ("slots", f"{row.get('held') or 0} of {slot_max} held", ""),
        ("drain", *drain_state(row)),
    ]


def _recent(snapshot: dict, keep) -> list[dict]:
    rows = (snapshot.get("settings") or {}).get("results") or []
    return [r for r in reversed(rows) if keep(r)][:RESULTS_PER_CARD]


def device_results(snapshot: dict, name: str) -> list[dict]:
    return _recent(snapshot, lambda r: r.get("device") == name and r.get("target") != "account")


def account_results(snapshot: dict, account_id: str) -> list[dict]:
    return _recent(snapshot, lambda r: r.get("target") == "account" and r.get("account") == account_id)


def percent(value: float | None) -> str:
    return "—" if value is None else f"{math.floor(value * 100 + 0.5)}%"


def headroom_fact(account: dict) -> tuple[str, str]:
    headroom, floor = account.get("headroom"), account["floor"]
    if headroom is not None and headroom < floor:
        return f"{percent(headroom)} · below floor {percent(floor)}", "bad"
    return f"{percent(headroom)} · floor {percent(floor)}", ""


def running_line(account: dict) -> str:
    jobs = account.get("running") or []
    return ", ".join(
        (f"{job['ticket']} " if job.get("ticket") else "") + f"{job['kind']} on {job['device']}" for job in jobs
    ) or "none"


def account_facts(account: dict) -> list[tuple[str, str, str]]:
    facts = [
        ("harness", account["harness"], ""),
        ("max_jobs", "—" if account.get("max_jobs") is None else str(account["max_jobs"]), ""),
        ("paused", "yes" if account.get("paused") else "no", "warn" if account.get("paused") else ""),
        ("5h used", percent(account.get("five_hour_used")), ""),
        ("7d used", percent(account.get("seven_day_used")), ""),
        ("headroom", *headroom_fact(account)),
        ("running", running_line(account), ""),
    ]
    if account.get("plan"):
        facts.insert(1, ("plan", str(account["plan"]), ""))
    if account.get("usage_note"):
        facts.append(("usage", f"not tracked — {account['usage_note']}", ""))
    return facts


def sign_in_label(entry: dict) -> str:
    login = entry.get("login")
    return f"sign in as {login} on {entry['device']}: " if login else f"sign in on {entry['device']}: "


def _value(value: object) -> str:
    if value is None:
        return "—"
    return json.dumps(value) if isinstance(value, bool) else str(value)


def result_line(row: dict) -> str:
    where = f" on {row['device']}" if row.get("target") == "account" and row.get("device") else ""
    return (f"{row.get('field') or '—'}{where} {_value(row.get('previous'))} → {_value(row.get('new'))}"
            f" · {row.get('actor') or '—'} · {row.get('applied_at') or row.get('requested_at') or '—'}")


def status_line(row: dict) -> tuple[str, str] | None:
    if row.get("result") == "superseded":
        return "st-sup", f"superseded by {row.get('superseded_by')}"
    if row.get("result") != "pending":
        return None
    if row.get("waiting"):
        return "st-err", WAITING
    if not row.get("pending"):
        return None
    return "st-err", "restart pending: " + ", ".join(row["pending"])


def _class(cls: str) -> str:
    return f' class="{cls}"' if cls else ""


def _facts(facts: list[tuple[str, str, str]]) -> str:
    rows = "".join(f"<div><dt>{_e(label)}</dt><dd{_class(cls)}>{_e(text)}</dd></div>" for label, text, cls in facts)
    return f'<dl class="st-facts">{rows}</dl>'


def _results(results: list[dict]) -> str:
    if not results:
        return f'<p class="empty">{_e(NO_CHANGES)}</p>'
    return '<ul class="st-results">' + "".join(
        '<li class="st-res">' + _chip(r["result"], RESULT_TONE.get(r["result"], "muted"))
        + _span("st-what", result_line(r))
        + (_span("st-err", r["error"]) if r.get("error") else "")
        + (_span(*status) if (status := status_line(r)) else "") + "</li>"
        for r in results
    ) + "</ul>"


def _card(snapshot: dict, row: dict) -> str:
    where = _chip("local · orch host", "ok") if row.get("local") else _chip("remote")
    return (
        f'<article class="st-card" data-device="{_e(row["name"])}">'
        f'<h3>{_span("st-name", row["name"])}{where}</h3>'
        f'{_facts(device_facts(row))}{_results(device_results(snapshot, row["name"]))}</article>'
    )


def _settings(snapshot: dict) -> str:
    slots = snapshot.get("slots") or {}
    devices = slots.get("devices") or []
    inner = f'<p class="st-note">{_e(READ_ONLY)}</p>'
    if devices:
        inner += '<div class="st-grid">' + "".join(_card(snapshot, row) for row in devices) + "</div>"
    else:
        inner += f'<p class="empty">no devices: machines.yml {_e(slots.get("machines_yml") or "absent")}</p>'
    return _panel("settings", "Devices", inner, count=f"{len(devices)} devices")


def _sign_in(entry: dict) -> str:
    status = entry.get("status")
    checked = entry.get("checked_at")
    same = entry.get("same_login_as") or []
    command = entry.get("command")
    return (
        f'<li class="ac-dev" data-device="{_e(entry["device"])}">{_span("ac-name", entry["device"])}'
        + _chip(status or UNCHECKED, SIGN_IN_TONE.get(status, "muted"))
        + _span(None, entry.get("auth_method")) + _span(None, entry.get("subscription_type"))
        + (_span("ac-when", "checked " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(checked)))
           if checked is not None else "")
        + (_span(None, "same login as " + ", ".join(same)) if same else "")
        + (f'<span class="ac-sign">{_e(sign_in_label(entry))}<code class="ac-cmd">{_e(command)}</code></span>'
           if command is not None else "")
        + "</li>"
    )


def _account_card(snapshot: dict, account: dict) -> str:
    reason = ACCOUNT_READ_ONLY.get(account.get("read_only"))
    entries = account.get("signed_in_on") or []
    devices = (
        '<ul class="ac-devs">' + "".join(_sign_in(entry) for entry in entries) + "</ul>"
        if entries else f'<p class="empty">{_e(NOT_SIGNED_IN)}</p>'
    )
    return (
        f'<article class="ac-card" data-account="{_e(account["id"])}">'
        f'<h3>{_span("st-name", account["id"])}{_chip(reason) if reason else ""}</h3>'
        f'{_facts(account_facts(account))}{devices}{_results(account_results(snapshot, account["id"]))}</article>'
    )


def _accounts(snapshot: dict) -> str:
    accounts = snapshot.get("accounts") or []
    inner = (
        '<div class="st-grid">' + "".join(_account_card(snapshot, account) for account in accounts) + "</div>"
        if accounts else f'<p class="empty">{_e(NO_ACCOUNTS)}</p>'
    )
    return _panel("accounts", "Accounts", inner, count=f"{len(accounts)} accounts")


CSS = """\
#settings{grid-column:1/-1}
#settings .st-note{margin:0 0 12px;color:var(--muted);font:12px/1.5 var(--mono)}
.st-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
:is(.st-card,.ac-card){display:flex;flex-direction:column;gap:10px;padding:12px;background:var(--panel-hi);border:1px solid var(--line)}
:is(.st-card,.ac-card) h3{display:flex;align-items:center;gap:8px;margin:0;font:600 15px/1.2 var(--mono);color:var(--text)}
.st-facts{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0;font:12px/1.5 var(--mono)}
.st-facts div{display:contents}
.st-facts dt{color:var(--muted)}.st-facts dd{margin:0;color:var(--text)}
.st-facts dd.warn{color:var(--warn)}.st-facts dd.ok{color:var(--ok)}.st-facts dd.bad{color:var(--bad)}
.st-results{display:flex;flex-direction:column;gap:4px;margin:0;padding:0;list-style:none;font:12px/1.5 var(--mono);color:var(--text-2)}
.st-res{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px}
.st-err,.st-out.bad{color:var(--bad)}.st-out.ok{color:var(--ok)}
.st-sup{color:var(--muted)}
.st-auth,.st-ctl{display:flex;flex-direction:column;gap:8px;font:12px/1.5 var(--mono);color:var(--text-2)}
.st-auth{margin:0 0 12px}
.st-auth .gh-row,.st-ctl .gh-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.st-auth,.st-auth .gh-row{flex-direction:row;flex-wrap:wrap;align-items:center}
:is(.st-auth,.st-ctl) :is(button,input,select){min-height:32px;padding:0 12px;font:12px var(--mono);color:var(--text);background:var(--field);border:1px solid var(--line-hi)}
:is(.st-auth,.st-ctl) button{cursor:pointer}
:is(.st-auth,.st-ctl) button[aria-pressed=true]{border-color:var(--warn);color:var(--warn)}
.st-ctl input{width:7ch}
.st-ctl input[data-field=id]{width:16ch}
.st-ctl input[type=checkbox]{width:auto;min-height:0;padding:0;accent-color:var(--brand)}
.st-ctl label{display:inline-flex;align-items:center;gap:4px}
:is(.st-auth,.st-ctl) :is(button,input,select):focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.st-auth code{font:600 16px var(--mono);letter-spacing:.1em;color:var(--accent)}
.st-auth a{color:var(--accent)}
#accounts{grid-column:1/-1}
#accounts>.st-ctl{margin:0 0 12px}
.ac-list{display:contents}
.ac-devs{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none;font:12px/1.5 var(--mono);color:var(--text-2)}
.ac-dev{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px}
.ac-name{color:var(--text)}.ac-when{color:var(--muted)}
.ac-sign{flex-basis:100%}
.ac-cmd{font:12px var(--mono);color:var(--accent);overflow-wrap:anywhere}
.ac-signin{display:flex;flex-direction:column;gap:8px;padding:8px;border:1px solid var(--line-hi)}
.st-ctl .ac-signin input{width:24ch}
.ac-link{color:var(--accent);overflow-wrap:anywhere}
.ac-code{font:600 16px var(--mono);letter-spacing:.1em;color:var(--accent)}
.ac-remind{margin:0;color:var(--warn)}
"""


_SCRIPT = r"""
  // settings.html (#777). A card's controls are kept nodes (ST.cards,
  // ST.accountCards, ST.addForm), so typed values and the last answer survive
  // each redraw.
  var ST = __ST__;
  ST.cards = Object.create(null);
  ST.accountCards = Object.create(null);
  ST.addForm = null;
  function stDrain(x) {
    if (!x.drain) return ['off', ''];
    var held = x.held || 0;
    return held ? ['draining, ' + held + ' running', 'warn'] : ['drained', 'ok'];
  }
  function stFacts(x) {
    var max = x.slot_max === null || x.slot_max === undefined ? '—' : String(x.slot_max);
    return [['enabled', x.enabled ? 'yes' : 'no', x.enabled ? '' : 'warn'], ['slot_max', max, ''],
            ['harnesses', (x.harnesses || []).join(', ') || 'none', ''],
            ['slots', (x.held || 0) + ' of ' + max + ' held', ''], ['drain'].concat(stDrain(x))];
  }
  function stValue(v) { return v === null || v === undefined ? '—' : String(v); }
  function stLine(r) {
    var where = r.target === 'account' && r.device ? ' on ' + r.device : '';
    return (r.field || '—') + where + ' ' + stValue(r.previous) + ' → ' + stValue(r.new) + ' · ' + (r.actor || '—') +
      ' · ' + (r.applied_at || r.requested_at || '—');
  }
  function stRecent(d, keep) { return ((d.settings || {}).results || []).filter(keep).reverse().slice(0, ST.perCard); }
  function stResults(d, name) {
    return stRecent(d, function (r) { return r.device === name && r.target !== 'account'; });
  }
  function stFactsList(card, facts) {
    var dl = add(card, el('dl', 'st-facts'));
    facts.forEach(function (f) {
      var row = add(dl, el('div'));
      add(row, el('dt', null, f[0]));
      add(row, el('dd', f[2] || null, f[1]));
    });
  }
  function stList(card, rows) {
    if (!rows.length) { add(card, el('p', 'empty', ST.noChanges)); return; }
    var ul = add(card, el('ul', 'st-results'));
    rows.forEach(function (r) {
      var li = add(ul, el('li', 'st-res'));
      add(li, chip(r.result, ST.tone[r.result] || 'muted'));
      add(li, el('span', 'st-what', stLine(r)));
      if (r.error) add(li, el('span', 'st-err', r.error));
      if (r.result === 'superseded') {
        add(li, el('span', 'st-sup', 'superseded by ' + r.superseded_by));
      } else if (r.result === 'pending' && r.waiting) {
        add(li, el('span', 'st-err', ST.waiting));
      } else if (r.result === 'pending' && (r.pending || []).length) {
        add(li, el('span', 'st-err', 'restart pending: ' + r.pending.join(', ')));
      }
    });
  }
  function stSend(c, field, expect, value) {
    if (c.busy) return;
    c.busy = true;
    c.out.className = 'st-out';
    c.out.textContent = 'sending…';
    stSync(c);
    ghPost('/api/settings', {device: c.x.name, field: field, expect: expect, value: value}, true).then(function (res) {
      c.busy = false;
      c.out.className = 'st-out ' + (res.status === 202 ? 'ok' : 'bad');
      c.out.textContent = res.status === 202 ? ST.queued : 'error: ' + res.error;
      stSync(c);
    });
  }
  function stCount(value) {
    var text = String(value || '').trim();
    var n = Number(text);
    return /^[0-9]+$/.test(text) && n >= 1 && n <= 64 ? n : null;
  }
  function stBad(c) {
    c.out.className = 'st-out bad';
    c.out.textContent = 'error: bad-value';
  }
  function stApply(c) {
    var n = stCount(c.slot.value);
    if (n === null) stBad(c);
    else stSend(c, 'slot_max', c.x.slot_max, n);
  }
  function stBuild() {
    var c = {root: el('div', 'st-ctl'), busy: false, shown: undefined, x: null};
    var row = add(c.root, el('div', 'gh-row'));
    c.enabled = ghButton(row, '', function () { stSend(c, 'enabled', c.x.enabled, !c.x.enabled); });
    c.enabled.setAttribute('data-field', 'enabled');
    c.drain = ghButton(row, '', function () { stSend(c, 'drain', c.x.drain, !c.x.drain); });
    c.drain.setAttribute('data-field', 'drain');
    var slots = add(c.root, el('div', 'gh-row'));
    c.slot = add(slots, el('input'));
    c.slot.setAttribute('type', 'number');
    c.slot.setAttribute('min', '1');
    c.slot.setAttribute('max', '64');
    c.slot.setAttribute('data-field', 'slot_max');
    c.apply = ghButton(slots, 'Apply slot_max', function () { stApply(c); });
    c.apply.setAttribute('data-field', 'apply');
    c.out = add(c.root, el('div', 'st-out'));
    c.out.setAttribute('role', 'status');
    return c;
  }
  function stSync(c) {
    var x = c.x;
    c.enabled.textContent = x.enabled ? 'Disable' : 'Enable';
    c.enabled.setAttribute('aria-pressed', String(!!x.enabled));
    c.drain.textContent = x.drain ? 'Stop draining' : 'Drain';
    c.drain.setAttribute('aria-pressed', String(!!x.drain));
    c.slot.setAttribute('aria-label', 'slot_max for ' + x.name);
    if (c.shown !== x.slot_max) {
      c.shown = x.slot_max;
      c.slot.value = x.slot_max === null || x.slot_max === undefined ? '' : String(x.slot_max);
    }
    c.enabled.disabled = c.drain.disabled = c.apply.disabled = c.busy;
  }
  function stControls(x) {
    var c = ST.cards[x.name] || (ST.cards[x.name] = stBuild());
    c.x = x;
    stSync(c);
    return c.root;
  }
  function stCard(d, x) {
    var card = el('article', 'st-card');
    card.setAttribute('data-device', x.name);
    var h = add(card, el('h3'));
    add(h, el('span', 'st-name', x.name));
    add(h, x.local ? chip('local · orch host', 'ok') : chip('remote'));
    stFactsList(card, stFacts(x));
    if (GH.on && GH.login) add(card, stControls(x));
    stList(card, stResults(d, x.name));
    return card;
  }
  function renderSettings(d) {
    if (!GH.started) { GH.started = true; ghStart(); }
    var slots = d.slots || {};
    var devices = slots.devices || [];
    var root = panel('settings', 'Devices', devices.length + ' devices');
    if (!root) return;
    if (GH.on) ghAuth(add(root, el('div', 'st-auth')));
    else add(root, el('p', 'st-note', ST.readOnly));
    if (!devices.length) { add(root, el('p', 'empty', 'no devices: machines.yml ' + (slots.machines_yml || 'absent'))); return; }
    var grid = add(root, el('div', 'st-grid'));
    devices.forEach(function (x) { add(grid, stCard(d, x)); });
  }
  function acPct(v) { return v === null || v === undefined ? '—' : Math.floor(v * 100 + 0.5) + '%'; }
  function acHeadroom(a) {
    var h = a.headroom;
    if (h !== null && h !== undefined && h < a.floor) return [acPct(h) + ' · below floor ' + acPct(a.floor), 'bad'];
    return [acPct(h) + ' · floor ' + acPct(a.floor), ''];
  }
  function acRunning(a) {
    return (a.running || []).map(function (j) {
      return (j.ticket ? j.ticket + ' ' : '') + j.kind + ' on ' + j.device;
    }).join(', ') || 'none';
  }
  function acFacts(a) {
    var out = [['harness', a.harness, '']];
    if (a.plan) out.push(['plan', a.plan, '']);
    out.push(['max_jobs', stValue(a.max_jobs), ''],
             ['paused', a.paused ? 'yes' : 'no', a.paused ? 'warn' : ''],
             ['5h used', acPct(a.five_hour_used), ''], ['7d used', acPct(a.seven_day_used), ''],
             ['headroom'].concat(acHeadroom(a)), ['running', acRunning(a), '']);
    if (a.usage_note) out.push(['usage', 'not tracked — ' + a.usage_note, '']);
    return out;
  }
  function acSignIn(ul, x) {
    var li = add(ul, el('li', 'ac-dev'));
    li.setAttribute('data-device', x.device);
    add(li, el('span', 'ac-name', x.device));
    add(li, chip(x.status || ST.unchecked, ST.signIn[x.status] || 'muted'));
    add(li, el('span', null, stValue(x.auth_method)));
    add(li, el('span', null, stValue(x.subscription_type)));
    if (x.checked_at !== null && x.checked_at !== undefined) {
      add(li, el('span', 'ac-when', 'checked ' + new Date(x.checked_at * 1000).toISOString().slice(0, 19) + 'Z'));
    }
    if ((x.same_login_as || []).length) add(li, el('span', null, 'same login as ' + x.same_login_as.join(', ')));
    if (x.command !== null && x.command !== undefined) {
      var label = x.login ? 'sign in as ' + x.login + ' on ' + x.device + ': ' : 'sign in on ' + x.device + ': ';
      add(add(li, el('span', 'ac-sign', label)), el('code', 'ac-cmd', x.command));
    }
  }
  function acDevices(a) { return (a.signed_in_on || []).map(function (x) { return x.device; }); }
  function acCandidates(d, harness) {
    return ((d.slots || {}).devices || []).filter(function (x) { return (x.harnesses || []).indexOf(harness) >= 0; })
      .map(function (x) { return x.name; });
  }
  function acChecked(c) {
    return c.boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.getAttribute('data-device'); });
  }
  function acSet(c, field, expect, value) { return {op: 'set', account: c.a.id, field: field, expect: expect, value: value}; }
  var AC_OPS = {
    add: function (c) {
      var n = stCount(c.max.value);
      return n === null ? null : {op: 'add', account: String(c.id.value || '').trim(), harness: c.harness.value,
                                  max_jobs: n, signed_in_on: acChecked(c)};
    },
    max_jobs: function (c) {
      var n = stCount(c.max.value);
      return n === null ? null : acSet(c, 'max_jobs', c.a.max_jobs, n);
    },
    signed_in_on: function (c) { return acSet(c, 'signed_in_on', acDevices(c.a), acChecked(c)); },
    paused: function (c) { return acSet(c, 'paused', c.a.paused, !c.a.paused); },
    remove: function (c) {
      var a = c.a;
      return {op: 'remove', account: a.id,
              expect: {harness: a.harness, max_jobs: a.max_jobs, paused: a.paused, signed_in_on: acDevices(a)}};
    },
    prepare: function (c, device) { return {op: 'prepare', account: c.a.id, device: device}; },
    doctor: function (c, device) { return {op: 'doctor', account: c.a.id, device: device}; },
    signout: function (c, device) { return {op: 'signout', account: c.a.id, device: device}; }
  };
  function acLive(c) { return !!c.signin && !c.signin.done; }
  function acDisable(c) {
    c.buttons.concat(c.pairButtons, c.panelButtons).forEach(function (b) {
      b.disabled = c.busy || (b.getAttribute('data-op') === 'signin' && acLive(c));
    });
  }
  function acPost(c, send, ok) {
    if (c.busy) return;
    c.busy = true;
    c.out.className = 'st-out';
    c.out.textContent = 'sending…';
    acDisable(c);
    send().then(function (res) {
      c.busy = false;
      c.out.className = 'st-out ' + (res.status === 202 ? 'ok' : 'bad');
      c.out.textContent = res.status === 202 ? ok(res.data) : 'error: ' + res.error;
      acDisable(c);
    });
  }
  function acSend(c, body) {
    acPost(c, function () { return ghPost('/api/settings', Object.assign({target: 'account'}, body), true); },
           function () { return ST.queued; });
  }
  function acLeft(at) {
    var n = Math.max(0, Math.floor(Number(at) - Date.now() / 1000));
    return Math.floor(n / 60) + ':' + ('0' + n % 60).slice(-2) + ' left';
  }
  function acPanel(c) {
    var s = c.signin;
    var v = s && s.view || {};
    var key = s && JSON.stringify([s.id, s.done, s.sent, v.state, v.url, v.code, v.expires_at]);
    if (key === c.panelKey) {
      if (c.left) c.left.textContent = acLeft(v.expires_at);
      return;
    }
    c.panelKey = key;
    acKeepFocus(c, function () { acDraw(c, s, v); });
  }
  function acKeepFocus(c, redraw) {
    var active = document.activeElement;
    var n = active;
    while (n && n !== c.signinSlot) n = n.parentNode;
    if (!n) { redraw(); return; }
    var start = active.selectionStart, end = active.selectionEnd, op = active.getAttribute('data-op');
    redraw();
    var target = active.isConnected ? active : c.panelButtons.filter(function (b) {
      return b.getAttribute('data-op') === op;
    })[0];
    if (!target || document.activeElement === target) return;
    target.focus({preventScroll: true});
    if (target === active && typeof start === 'number') active.setSelectionRange(start, end);
  }
  function acDraw(c, s, v) {
    c.panelButtons = [];
    c.left = null;
    c.signinSlot.replaceChildren();
    if (!s) return;
    var p = acAttrs(add(c.signinSlot, el('div', 'ac-signin')), {'data-signin': s.device});
    var row = add(p, el('div', 'gh-row'));
    add(row, el('span', 'ac-name', s.device));
    add(row, el('span', null, s.done || v.state || 'accepted'));
    if (!s.done) {
      var url = String(v.url || '');
      if (/^https:\/\/(claude\.com|auth\.openai\.com)\//.test(url)) {
        var a = add(row, acAttrs(link('ac-link', url), {rel: 'noopener noreferrer', target: '_blank'}));
        a.textContent = url;
      }
      if (s.harness === 'codex' && v.code) add(row, el('code', 'ac-code', String(v.code)));
      if (v.expires_at) c.left = add(row, el('span', 'ac-when', acLeft(v.expires_at)));
      acButton(c, c.panelButtons, row, 'Cancel', 'signin-cancel', null, function () { acCancel(c, s); });
      if (s.harness === 'claude' && s.sent) add(p, el('span', null, 'code sent'));
      else if (s.harness === 'claude') {
        var paste = add(p, el('div', 'gh-row'));
        s.input = s.input || acAttrs(el('input'), {'data-field': 'signin-code', autocomplete: 'off',
                                                   placeholder: 'code', 'aria-label': 'code for ' + s.device});
        add(paste, s.input);
        acButton(c, c.panelButtons, paste, 'Send', 'signin-code', null, function () { acCode(c, s); });
      }
    }
    add(p, el('p', 'ac-remind', ST.remind));
  }
  function acSignin(c, device) {
    if (acLive(c)) return;
    var a = c.a;
    acPost(c, function () { return ghPost('/api/accounts/signin', {account: a.id, device: device}, true); },
           function (data) {
             c.signin = {id: String(data.id || ''), device: device, harness: a.harness, view: null, done: null,
                         polling: false, sent: false, input: null};
             if (!ST.ticking) { ST.ticking = true; setInterval(acTick, 1000); }
             acPanel(c);
             return '';
           });
  }
  function acCode(c, s) {
    var code = String(s.input.value || '').trim();
    if (!code) { stBad(c); return; }
    acPost(c, function () { return ghPost('/api/accounts/signin/code', {id: s.id, code: code}, true); },
           function () { s.sent = true; acPanel(c); return ''; });
  }
  function acCancel(c, s) {
    acPost(c, function () { return ghPost('/api/accounts/signin/cancel', {id: s.id}, true); },
           function () { return ''; });
  }
  function acView(s, res) {
    if (res.status === 404) s.done = 'error: no-attempt';
    if (!res.ok) return;
    s.view = res.data;
    if (res.data.result) s.done = 'sign-in ' + res.data.result + (res.data.error ? ': ' + res.data.error : '');
  }
  function acTick() {
    if (!GH.login) return;
    Object.keys(ST.accountCards).forEach(function (id) {
      var c = ST.accountCards[id];
      var s = c.signin;
      if (!s || s.done) return;
      acPanel(c);
      if (s.polling) return;
      s.polling = true;
      ghGet('/api/accounts/signin?id=' + encodeURIComponent(s.id)).then(function (res) {
        s.polling = false;
        acView(s, res);
        if (c.signin !== s) return;
        acPanel(c);
        acDisable(c);
      });
    });
  }
  function acRun(c, op, device) {
    var body = AC_OPS[op](c, device);
    if (body) acSend(c, body);
    else stBad(c);
  }
  function acAttrs(node, attrs) {
    Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }
  function acButton(c, list, row, text, op, device, onClick) {
    var b = acAttrs(ghButton(row, text, onClick || function () { acRun(c, op, device); }), {'data-op': op});
    if (device) b.setAttribute('data-pair', device);
    list.push(b);
    return b;
  }
  function acBase(label, op) {
    var c = {root: el('div', 'st-ctl'), busy: false, boxes: [], boxKey: undefined, buttons: [], pairButtons: [],
             panelButtons: [], signin: null, panelKey: null, left: null};
    c.fields = add(c.root, el('div', 'gh-row'));
    c.max = acAttrs(el('input'), {type: 'number', min: '1', max: '64', 'data-field': 'max_jobs'});
    var row = add(c.root, el('div', 'gh-row'));
    c.boxRow = add(row, el('span', 'ac-list'));
    acButton(c, c.buttons, row, label, op);
    return c;
  }
  function acOut(c) {
    c.out = add(c.root, el('div', 'st-out'));
    c.out.setAttribute('role', 'status');
  }
  function acBoxes(c, key, names, checked) {
    if (c.boxKey === key) return;
    c.boxKey = key;
    c.boxRow.replaceChildren();
    c.boxes = names.map(function (name) {
      var label = add(c.boxRow, el('label'));
      var box = add(label, acAttrs(el('input'), {type: 'checkbox', 'data-device': name}));
      box.checked = checked.indexOf(name) >= 0;
      add(label, document.createTextNode(name));
      return box;
    });
  }
  function acAddSync(c) {
    var names = acCandidates(c.d, c.harness.value);
    acBoxes(c, JSON.stringify(names), names, acChecked(c));
    acDisable(c);
  }
  function acAddBuild() {
    var c = acBase('Add account', 'add');
    c.root.setAttribute('data-form', 'add');
    c.id = add(c.fields, acAttrs(el('input'), {'data-field': 'id', placeholder: 'account id'}));
    c.harness = add(c.fields, acAttrs(el('select'), {'data-field': 'harness', 'aria-label': 'harness'}));
    ST.harnesses.forEach(function (h) { add(c.harness, el('option', null, h)); });
    c.harness.value = ST.harnesses[0];
    c.harness.addEventListener('change', function () { acAddSync(c); });
    add(c.fields, c.max).setAttribute('placeholder', 'max_jobs');
    acOut(c);
    return c;
  }
  function acAddForm(d) {
    var c = ST.addForm || (ST.addForm = acAddBuild());
    c.d = d;
    acAddSync(c);
    return c.root;
  }
  function acCardBuild(a) {
    var c = acBase('Apply devices', 'signed_in_on');
    add(c.fields, c.max).setAttribute('aria-label', 'max_jobs for ' + a.id);
    acButton(c, c.buttons, c.fields, 'Apply max_jobs', 'max_jobs');
    var row = add(c.root, el('div', 'gh-row'));
    c.paused = acButton(c, c.buttons, row, '', 'paused');
    acButton(c, c.buttons, row, 'Remove', 'remove');
    c.pairs = add(c.root, el('div', 'ac-list'));
    c.signinSlot = add(c.root, el('div', 'ac-list'));
    acOut(c);
    return c;
  }
  function acControls(d, a) {
    var c = ST.accountCards[a.id] || (ST.accountCards[a.id] = acCardBuild(a));
    c.a = a;
    c.paused.textContent = a.paused ? 'Resume' : 'Pause';
    c.paused.setAttribute('aria-pressed', String(!!a.paused));
    if (c.shown !== a.max_jobs) {
      c.shown = a.max_jobs;
      c.max.value = a.max_jobs === null || a.max_jobs === undefined ? '' : String(a.max_jobs);
    }
    var on = acDevices(a);
    var names = acCandidates(d, a.harness);
    acBoxes(c, JSON.stringify([names, on]), names, on);
    var signin = GH.accountSignin && ST.signinHarnesses.indexOf(a.harness) >= 0;
    if (c.pairKey !== JSON.stringify([on, signin])) {
      c.pairKey = JSON.stringify([on, signin]);
      c.pairs.replaceChildren();
      c.pairButtons = [];
      on.forEach(function (device) {
        var row = add(c.pairs, el('div', 'gh-row'));
        add(row, el('span', 'ac-name', device));
        acButton(c, c.pairButtons, row, 'Prepare', 'prepare', device);
        acButton(c, c.pairButtons, row, 'Run doctor', 'doctor', device);
        if (signin) acButton(c, c.pairButtons, row, 'Sign in', 'signin', device, function () { acSignin(c, device); });
        acButton(c, c.pairButtons, row, 'Sign out', 'signout', device);
      });
    }
    acDisable(c);
    return c.root;
  }
  function acCard(d, a) {
    var card = el('article', 'ac-card');
    card.setAttribute('data-account', a.id);
    var h = add(card, el('h3'));
    add(h, el('span', 'st-name', a.id));
    if (ST.accountReadOnly[a.read_only]) add(h, chip(ST.accountReadOnly[a.read_only]));
    stFactsList(card, acFacts(a));
    if (GH.on && GH.login && !a.read_only) add(card, acControls(d, a));
    var on = a.signed_in_on || [];
    if (on.length) {
      var ul = add(card, el('ul', 'ac-devs'));
      on.forEach(function (x) { acSignIn(ul, x); });
    } else {
      add(card, el('p', 'empty', ST.notSignedIn));
    }
    stList(card, stRecent(d, function (r) { return r.target === 'account' && r.account === a.id; }));
    return card;
  }
  function renderAccounts(d) {
    var accounts = d.accounts || [];
    var root = panel('accounts', 'Accounts', accounts.length + ' accounts');
    if (!root) return;
    if (GH.on && GH.login) add(root, acAddForm(d));
    if (!accounts.length) { add(root, el('p', 'empty', ST.noAccounts)); return; }
    var grid = add(root, el('div', 'st-grid'));
    accounts.forEach(function (a) { add(grid, acCard(d, a)); });
  }
"""
SCRIPT = _SCRIPT.replace("__ST__", json.dumps(
    {"tone": RESULT_TONE, "perCard": RESULTS_PER_CARD, "queued": QUEUED, "readOnly": READ_ONLY,
     "waiting": WAITING, "noChanges": NO_CHANGES, "signIn": SIGN_IN_TONE, "unchecked": UNCHECKED,
     "accountReadOnly": ACCOUNT_READ_ONLY, "notSignedIn": NOT_SIGNED_IN, "noAccounts": NO_ACCOUNTS,
     "harnesses": ADD_HARNESSES, "signinHarnesses": SIGNIN_HARNESSES, "remind": SIGNIN_REMIND}
))
