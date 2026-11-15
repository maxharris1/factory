"""job.html (Tickets): the running and queued jobs; also the tickets table (on activity.html)."""

from __future__ import annotations

from lib.dashboard_pages.common import (
    anchor, _bot, _chip, _feed, _job_label, _panel, _span, _td, _ticket,
)


SCRIPT_TICKETS = r"""
  function renderTickets(d) {
    var rows = d.tickets || [];
    var root = panel('tickets', 'Tickets', rows.length + ' tracked');
    if (!root) return;
    if (!rows.length) { add(root, el('p', 'empty', 'no tracked tickets')); return; }
    var table = add(root, el('table'));
    var head = add(add(table, el('thead')), el('tr'));
    ['ticket', 'tracker', 'state', 'round', 'gates', 'stuck'].forEach(function (h) { add(head, el('th', null, h)); });
    var tbody = add(table, el('tbody'));
    rows.forEach(function (r) {
      var tr = add(tbody, el('tr'));
      add(tr, el('td', null, ticketIn(r.repo, r)));
      add(tr, el('td', null, r.tracker));
      add(tr, el('td', r.stuck ? 'bad' : null, r.state));
      add(tr, el('td', 'num', r.round));
      add(tr, el('td', 'num', r.gate_count));
      add(tr, el('td', r.stuck ? 'bad' : null, r.stuck ? 'yes' : 'no'));
    });
  }
"""


SCRIPT_JOBS = r"""
  // --- job page -----------------------------------------------------------------

  function activeJobs(d) {
    return (d.running || []).map(function (r) { return {row: r, state: 'running'}; })
      .concat((d.queued || []).map(function (r) { return {row: r, state: 'queued'}; }));
  }

  function renderJobs(d) {
    var jobs = activeJobs(d);
    var root = panel('jobs', 'Jobs', (d.running || []).length + ' running · ' + (d.queued || []).length + ' queued');
    if (!root) return;
    list(root, jobs, 'no running or queued jobs', function (j) {
      var li = el('li');
      var a = add(li, link('queue-item', '#' + anchor(j.row.job)));
      add(a, bot(j.row.recipient));
      add(a, el('span', 'ticket', jobLabel(j.row)));
      add(a, el('span', 'what', j.row.action));
      add(a, chip(j.state, j.state === 'running' ? 'accent' : 'warn'));
      return li;
    });
  }
"""


def _tickets(snapshot: dict) -> str:
    rows = snapshot["tickets"]
    if not rows:
        inner = '<p class="empty">no tracked tickets</p>'
    else:
        head = "".join(
            f"<th>{name}</th>"
            for name in ("ticket", "tracker", "state", "round", "gates", "stuck")
        )
        body = "".join(
            "<tr>"
            + _td(_ticket(row, full=True))
            + _td(row["tracker"])
            + _td(row["state"], "bad" if row["stuck"] else None)
            + _td(row["round"], "num")
            + _td(row["gate_count"], "num")
            + _td("yes" if row["stuck"] else "no", "bad" if row["stuck"] else None)
            + "</tr>"
            for row in rows
        )
        inner = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    return _panel("tickets", "Tickets", inner, count=f"{len(rows)} tracked")


# --- job page ---------------------------------------------------------------------


def _active_jobs(snapshot: dict) -> list[tuple[dict, str]]:
    return [(row, "running") for row in snapshot["running"]] + [
        (row, "queued") for row in snapshot["queued"]
    ]


def _jobs(snapshot: dict) -> str:
    def draw(item: tuple[dict, str]) -> str:
        row, state = item
        return (
            f'<a class="queue-item" href="#{anchor(row["job"])}">'
            f'{_bot(row["recipient"])}{_span("ticket", _job_label(row))}'
            f'{_span("what", row["action"])}{_chip(state, "accent" if state == "running" else "warn")}</a>'
        )

    count = f"{len(snapshot['running'])} running · {len(snapshot['queued'])} queued"
    return _panel(
        "jobs", "Jobs", _feed(_active_jobs(snapshot), "no running or queued jobs", draw),
        count=count,
    )
