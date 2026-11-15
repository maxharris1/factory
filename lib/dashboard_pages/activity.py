"""activity.html: needs attention, recent handoffs and verdicts."""

from __future__ import annotations

import re

from lib.pr_title import label
from lib.dashboard_pages.common import _bot, _chip, _e, _feed, _panel, _span, _ticket, _when


SCRIPT = r"""
  // --- activity page ---------------------------------------------------------

  var ATTENTION = {stuck: 'bad', overdue: 'bad', dead: 'bad', 'needs-attention': 'bad', stale: 'warn', paused: 'warn',
                   down: 'bad', 'no-waiter': 'bad', health: 'bad'};
  function renderAttention(d) {
    var items = attentionItems(d);
    var root = panel('attention', 'Needs attention', items.length + (items.length === 1 ? ' item' : ' items'));
    if (!root) return;
    list(root, items, 'all clear', function (x) {
      var li = el('li');
      add(li, chip(x.kind, ATTENTION[x.kind] || 'muted'));
      if (x.component) add(li, link('ticket', healthLink(x.link))).textContent = x.component;
      else add(li, el('span', 'ticket', ticketOf(x)));
      add(li, el('span', 'what', x.detail));
      return mark('attention', x.kind + ':' + (x.component || x.repo + '#' + x.ticket_id), li);
    });
  }

  var HANDOFF = {scripted: ['scripted', 'ok'], bot_to_bot: ['bot-to-bot', 'accent'],
                 failed: ['failed', 'bad'], done: ['done', 'muted']};
  function renderHandoffs(d) {
    var rows = d.handoff_savings.recent || [];
    var root = panel('handoffs', 'Recent handoffs');
    if (!root) return;
    list(root, rows, 'no finished jobs yet', function (r) {
      var li = el('li');
      var kind = HANDOFF[r.kind] || [r.kind, 'muted'];
      add(li, chip(kind[0], kind[1]));
      add(li, bot(r.recipient));
      add(li, el('span', 'ticket', ticketOf(r)));
      add(li, el('span', 'what', r.action));
      ticking(add(li, el('span', 'when')), r.age_seconds, '', ' ago');
      return mark('handoffs', r.job + ':' + r.state, li);
    });
  }

  var VERDICT = {routed: 'accent', shipped: 'ok', stale: 'muted', rejected: 'warn', stuck: 'bad',
                 blocked: 'bad', waiting: 'warn', other: 'muted'};
  function renderVerdicts(d) {
    var rows = d.poller.verdicts;
    var root = panel('verdicts', 'Recent verdicts');
    if (!root) return;
    if (rows === null || rows === undefined) { add(root, el('p', 'empty', 'no handled comments recorded yet')); return; }
    list(root, rows, 'no verdicts yet', function (r) {
      var li = el('li');
      add(li, chip(r.kind, VERDICT[r.kind] || 'muted'));
      add(li, el('span', 'ticket', ticketOf(r)));
      add(li, el('span', 'what', r.outcome));
      return mark('verdicts', r.repo + '#' + r.ticket_id + ':' + r.outcome, li);
    });
  }
"""


# --- activity page -----------------------------------------------------------------


_ATTENTION = {
    "stuck": "bad", "overdue": "bad", "dead": "bad", "needs-attention": "bad",
    "stale": "warn", "paused": "warn", "down": "bad", "no-waiter": "bad", "health": "bad",
}


_HEALTH_LINK = re.compile(r"health\.html#[A-Za-z0-9_-]+")


def _health_link(href: object) -> str:
    """Only a link into the Health tab (#92); anything else is the tab itself."""
    return str(href) if isinstance(href, str) and _HEALTH_LINK.fullmatch(href) else "health.html"


def _attention(snapshot: dict) -> str:
    attention = snapshot["attention"]

    def draw(row: dict) -> str:
        who = (
            f'<a class="ticket" href="{_e(_health_link(row.get("link")))}">'
            f'{_e(row["component"])}</a>'
            if row.get("component")
            else _span("ticket", _ticket(row))
        )
        return (
            _chip(row["kind"], _ATTENTION.get(row["kind"], "muted"))
            + who
            + _span("what", row["detail"])
        )

    count = attention["count"]
    return _panel(
        "attention", "Needs attention", _feed(attention["items"], "all clear", draw),
        count=f"{count} item" + ("" if count == 1 else "s"),
    )


_HANDOFF = {"scripted": ("scripted", "ok"), "bot_to_bot": ("bot-to-bot", "accent"),
            "failed": ("failed", "bad"), "done": ("done", "muted")}


def _handoffs(snapshot: dict) -> str:
    def draw(row: dict) -> str:
        label, cls = _HANDOFF.get(row["kind"], (row["kind"], "muted"))
        return (
            _chip(label, cls) + _bot(row["recipient"]) + _span("ticket", _ticket(row))
            + _span("what", row["action"]) + _when(row["age_seconds"], suffix=" ago")
        )

    rows = snapshot["handoff_savings"]["recent"]
    return _panel("handoffs", "Recent handoffs", _feed(rows, "no finished jobs yet", draw))


_VERDICT = {"routed": "accent", "shipped": "ok", "stale": "muted", "rejected": "warn",
            "stuck": "bad", "blocked": "bad", "waiting": "warn", "other": "muted"}


def _verdicts(snapshot: dict) -> str:
    rows = snapshot["poller"]["verdicts"]
    if rows is None:
        inner = '<p class="empty">no handled comments recorded yet</p>'
    else:
        inner = _feed(
            rows,
            "no verdicts yet",
            lambda row: _chip(row["kind"], _VERDICT.get(row["kind"], "muted"))
            + _span("ticket", _ticket(row)) + _span("what", row["outcome"]),
        )
    return _panel("verdicts", "Recent verdicts", inner)
