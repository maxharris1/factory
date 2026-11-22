"""settings.html (Settings, #777): one card per machines.yml device with its
enabled, slot_max, harnesses, held slots, drain state and its recent change
results from the settings audit log. Signed in through the #563 flow
(`ghAuth` in common.py), each card gets a toggle for enabled and drain and a
slot_max input that POST /api/settings; the orch host applies the queued
request (lib/settings_apply.py)."""

from __future__ import annotations

import json

from lib.dashboard_pages.common import _chip, _e, _panel, _span

RESULT_TONE = {"applied": "ok", "pending": "warn", "refused": "warn", "failed": "bad", "superseded": "muted",
               "other": "muted"}
RESULTS_PER_DEVICE = 5
QUEUED = "queued (applies within ~2 min)"
READ_ONLY = "read-only: sign-in is not available here"
WAITING = "waiting for the poller's next cycle"


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


def device_results(snapshot: dict, name: str) -> list[dict]:
    rows = (snapshot.get("settings") or {}).get("results") or []
    return [r for r in reversed(rows)
            if r.get("device") == name and r.get("target") != "account"][:RESULTS_PER_DEVICE]


def _value(value: object) -> str:
    if value is None:
        return "—"
    return json.dumps(value) if isinstance(value, bool) else str(value)


def result_line(row: dict) -> str:
    return (f"{row.get('field') or '—'} {_value(row.get('previous'))} → {_value(row.get('new'))}"
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


def _card(snapshot: dict, row: dict) -> str:
    where = _chip("local · orch host", "ok") if row.get("local") else _chip("remote")
    facts = "".join(
        f"<div><dt>{_e(label)}</dt><dd{_class(cls)}>{_e(text)}</dd></div>" for label, text, cls in device_facts(row)
    )
    results = device_results(snapshot, row["name"])
    listing = (
        '<ul class="st-results">' + "".join(
            '<li class="st-res">' + _chip(r["result"], RESULT_TONE.get(r["result"], "muted"))
            + _span("st-what", result_line(r))
            + (_span("st-err", r["error"]) if r.get("error") else "")
            + (_span(*status) if (status := status_line(r)) else "") + "</li>"
            for r in results
        ) + "</ul>"
        if results else '<p class="empty">no changes yet</p>'
    )
    return (
        f'<article class="st-card" data-device="{_e(row["name"])}">'
        f'<h3>{_span("st-name", row["name"])}{where}</h3>'
        f'<dl class="st-facts">{facts}</dl>{listing}</article>'
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


CSS = """\
#settings{grid-column:1/-1}
#settings .st-note{margin:0 0 12px;color:var(--muted);font:12px/1.5 var(--mono)}
.st-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.st-card{display:flex;flex-direction:column;gap:10px;padding:12px;background:var(--panel-hi);border:1px solid var(--line)}
.st-card h3{display:flex;align-items:center;gap:8px;margin:0;font:600 15px/1.2 var(--mono);color:var(--text)}
.st-facts{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0;font:12px/1.5 var(--mono)}
.st-facts div{display:contents}
.st-facts dt{color:var(--muted)}.st-facts dd{margin:0;color:var(--text)}
.st-facts dd.warn{color:var(--warn)}.st-facts dd.ok{color:var(--ok)}
.st-results{display:flex;flex-direction:column;gap:4px;margin:0;padding:0;list-style:none;font:12px/1.5 var(--mono);color:var(--text-2)}
.st-res{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px}
.st-err,.st-out.bad{color:var(--bad)}.st-out.ok{color:var(--ok)}
.st-sup{color:var(--muted)}
.st-auth,.st-ctl{display:flex;flex-direction:column;gap:8px;font:12px/1.5 var(--mono);color:var(--text-2)}
.st-auth{margin:0 0 12px}
.st-auth .gh-row,.st-ctl .gh-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.st-auth,.st-auth .gh-row{flex-direction:row;flex-wrap:wrap;align-items:center}
:is(.st-auth,.st-ctl) :is(button,input){min-height:32px;padding:0 12px;font:12px var(--mono);color:var(--text);background:var(--field);border:1px solid var(--line-hi)}
:is(.st-auth,.st-ctl) button{cursor:pointer}
:is(.st-auth,.st-ctl) button[aria-pressed=true]{border-color:var(--warn);color:var(--warn)}
.st-ctl input{width:7ch}
:is(.st-auth,.st-ctl) :is(button,input):focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.st-auth code{font:600 16px var(--mono);letter-spacing:.1em;color:var(--accent)}
.st-auth a{color:var(--accent)}
"""


_SCRIPT = r"""
  // settings.html (#777). A card's controls are kept nodes (ST.cards), so a
  // typed slot_max and the last answer survive each redraw.
  var ST = __ST__;
  ST.cards = {};
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
    return (r.field || '—') + ' ' + stValue(r.previous) + ' → ' + stValue(r.new) + ' · ' + (r.actor || '—') +
      ' · ' + (r.applied_at || r.requested_at || '—');
  }
  function stResults(d, name) {
    var rows = ((d.settings || {}).results || []).filter(function (r) { return r.device === name && r.target !== 'account'; });
    return rows.reverse().slice(0, ST.perDevice);
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
  function stApply(c) {
    var text = String(c.slot.value || '').trim();
    var n = Number(text);
    if (!/^[0-9]+$/.test(text) || n < 1 || n > 64) {
      c.out.className = 'st-out bad';
      c.out.textContent = 'error: bad-value';
      return;
    }
    stSend(c, 'slot_max', c.x.slot_max, n);
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
    var facts = add(card, el('dl', 'st-facts'));
    stFacts(x).forEach(function (f) {
      var row = add(facts, el('div'));
      add(row, el('dt', null, f[0]));
      add(row, el('dd', f[2] || null, f[1]));
    });
    if (GH.on && GH.login) add(card, stControls(x));
    var rows = stResults(d, x.name);
    if (!rows.length) { add(card, el('p', 'empty', 'no changes yet')); return card; }
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
"""
SCRIPT = _SCRIPT.replace("__ST__", json.dumps(
    {"tone": RESULT_TONE, "perDevice": RESULTS_PER_DEVICE, "queued": QUEUED, "readOnly": READ_ONLY,
     "waiting": WAITING}
))
