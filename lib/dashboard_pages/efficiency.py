"""efficiency.html: handoff savings (tokens and cost are lib/dashboard_telemetry.py)."""

from __future__ import annotations

from lib.dashboard_pages.common import _b, _e, _panel, _span


SCRIPT = r"""
  function renderSavings(d) {
    var s = d.handoff_savings;
    var root = panel('savings', 'Handoff savings');
    if (!root) return;
    var gauge = add(root, el('div', 'gauge'));
    var ring = add(gauge, el('div', 'ring'));
    var pct = s.scripted_share === null ? null : Math.round(s.scripted_share * 100);
    ring.style.setProperty('--p', pct === null ? 0 : pct);
    add(ring, el('span', null, pct === null ? '—' : pct + '%'));
    var stats = add(gauge, el('div'));
    [[s.scripted, 'scripted handoffs', 'accent'], [s.scripted_in_flight, 'scripted in flight', ''],
     [s.bot_to_bot, 'bot-to-bot (grok wakes)', '']].forEach(function (x) {
      var line = add(stats, el('div', 'row'));
      add(line, el('b', x[2], num(x[0])));
      add(line, el('span', 'muted', x[1]));
    });
    var total = (s.scripted || 0) + (s.bot_to_bot || 0);
    var meter = add(root, el('div', 'meter'));
    if (total) {
      add(meter, el('i', 'scripted')).style.width = (100 * s.scripted / total) + '%';
      add(meter, el('i', 'bots')).style.width = (100 * s.bot_to_bot / total) + '%';
    }
    add(root, el('p', 'definition', s.scripted_share_definition)).setAttribute('id', 'share-definition');
    add(details(root, 'savings-def', 'how each count is made'), el('p', 'muted', s.definition));
  }
"""


def _savings(snapshot: dict) -> str:
    savings = snapshot["handoff_savings"]
    share = savings["scripted_share"]
    pct = None if share is None else round(share * 100)
    lines = "".join(
        f'<div class="row">{_b(v, c)}{_span("muted", text)}</div>'
        for v, text, c in (
            (savings["scripted"], "scripted handoffs", "accent"),
            (savings["scripted_in_flight"], "scripted in flight", ""),
            (savings["bot_to_bot"], "bot-to-bot (grok wakes)", ""),
        )
    )
    total = (savings["scripted"] or 0) + (savings["bot_to_bot"] or 0)
    meter = (
        f'<i class="scripted" style="width:{100 * savings["scripted"] / total:g}%"></i>'
        f'<i class="bots" style="width:{100 * savings["bot_to_bot"] / total:g}%"></i>'
        if total
        else ""
    )
    inner = (
        f'<div class="gauge"><div class="ring" style="--p:{0 if pct is None else pct}">'
        f'<span>{"—" if pct is None else f"{pct}%"}</span></div><div>{lines}</div></div>'
        f'<div class="meter">{meter}</div>'
        f'<p class="definition" id="share-definition">{_e(savings["scripted_share_definition"])}</p>'
        "<details><summary>how each count is made</summary>"
        f'<p class="muted">{_e(savings["definition"])}</p></details>'
    )
    return _panel("savings", "Handoff savings", inner)
