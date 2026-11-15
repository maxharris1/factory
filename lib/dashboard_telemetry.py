"""Job telemetry on the dashboard: the snapshot's job summaries and the Efficiency rollup (#93).

`read_telemetry` reads the `job_telemetry` table (written by `orch job run`,
lib/telemetry.py) inside the dashboard's read-only transaction. A database
without the table, or a job without a row, reads as "no telemetry", never an
error. Only allowlisted columns are read and every value is checked: numbers
must be finite, counts are non-negative integers, text is one line of at
most a fixed length, redacted whole before any other change to it (lib.dashboard is
imported late because it imports this module), and step kinds and statuses
are enumerated. The snapshot is then walked by lib/dashboard.py's `_scrub`,
so every string here also goes through `redact` again (and any non-finite
float becomes null) before it is written. Raw event logs are never read: they stay on the
machine that ran the job.

Snapshot shape:

* each running or queued job gets `telemetry`: a summary (counters, tool
  calls by type, files touched, tests, tokens, cost, elapsed, last activity,
  exit reason, the last STEPS_SHOWN steps, each a fixed label) or null when
  it has none;
* each summary also carries the live run (#147 writes it, #148 reads it).
  null means no source: a table from before #147, ORCH_TELEMETRY=counters,
  or nothing captured yet. An empty list means the source is there and empty.
    plan:          null | [{text: str<=TASK_TEXT_LIMIT,
                            status: "pending" | "in_progress" | "done"}]
                   (at most PLAN_ITEMS)
    current_task:  null | str<=TASK_TEXT_LIMIT
    last_finished: null | str<=TASK_TEXT_LIMIT
    subagents:     null | [{kind: str<=LABEL_LIMIT, description: str<=TASK_TEXT_LIMIT,
                            status: "running" | "finished" | "failed",
                            started_age_seconds: int | null,
                            finished_age_seconds: int | null (null while running)}]
                   (at most SUBAGENTS)
  Times are ages in seconds, like every other time in the snapshot. An
  unknown plan status reads as "pending", an unknown sub-agent status as
  "running"; text is one line, capped with "…";
* the top-level `telemetry` holds the rollup over the last WINDOW_DAYS:
  totals, then tokens and cost per harness and per repo, and the summaries
  of the RECENT_JOBS most recently finished jobs (so a job modal opened on
  a job that has just finished can still show how it ended).

The page side mirrors lib/dashboard_page.py: `rollup_panel` renders
server-side, and TELEMETRY_SCRIPT holds the same renderers for the
page script (createElement/textContent only), spliced into its scope.
"""

from __future__ import annotations

import html
import json
import math
import re
import sqlite3
from collections.abc import Iterable

from lib.telemetry import LABEL_LIMIT, LIVE_RUN_COLUMNS, PLAN_ITEMS, SUBAGENTS, TABLE, TASK_TEXT_LIMIT

WINDOW_DAYS = 7
RECENT_JOBS = 12
STEPS_SHOWN = 12
TOOLS_SHOWN = 8
ROW_LIMIT = 2000
TEXT_LIMIT = 160
NO_DATA = "no data"
HARNESSES = ("claude", "codex", "cursor")
STEP_KINDS = ("tool", "command", "error")
STEP_STATUSES = ("running", "ok", "error")
PLAN_STATUSES = ("pending", "in_progress", "done")
SUBAGENT_STATUSES = ("running", "finished", "failed")

_COLUMNS = (
    "job_id", "harness", "repo", "started_at", "updated_at", "last_event_at",
    "finished_at", "elapsed_seconds", "events", "turns", "tool_calls",
    "files_touched", "commands", "test_runs", "tests_passed",
    "tests_failed", "tokens_in", "tokens_cached", "tokens_out", "cost_usd",
    "exit_code", "exit_reason", "steps",
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# What a label or exit reason can be (lib/telemetry.py writes only these
# shapes): tool names, fixed words and counts, "[redacted-env]". Anything
# else in the table is not shown.
_LABEL = re.compile(r"[\w .:\[\]-]{1,40}")
_REASON = re.compile(r"[\w .;()-]{1,100}")


# --- reading -------------------------------------------------------------------


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _count(value: object) -> int:
    return _maybe_count(value) or 0


def _maybe_count(value: object) -> int | None:
    number = _number(value)
    return None if number is None else max(0, int(number))


def _money(value: object) -> float | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return round(float(number), 4)


def _text(value: object, limit: int = TEXT_LIMIT) -> str:
    from lib.dashboard import redact

    text = " ".join(_CONTROL.sub(" ", redact(str(value))).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _age(now: float, then: object) -> int | None:
    number = _number(then)
    return None if number is None else int(max(0.0, now - float(number)))


def _json(value: object, kind: type) -> object:
    try:
        data = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return kind()
    return data if isinstance(data, kind) else kind()


def _shaped(value: object, shape: re.Pattern[str], fallback: str) -> str:
    return value if isinstance(value, str) and shape.fullmatch(value) else fallback


def _tokens_known(row: sqlite3.Row) -> bool:
    """False when the CLI reported no usage (#264): the dashboard shows "no
    data", not 0. A row from before `tokens_unknown` is known if it has any."""
    flag = row["tokens_unknown"]
    if isinstance(flag, int) and not isinstance(flag, bool):
        return flag == 0
    return any(_count(row[name]) for name in ("tokens_in", "tokens_cached", "tokens_out"))


def _harness(value: object) -> str:
    return str(value) if str(value) in HARNESSES else "other"


def _json_list(value: object) -> list | None:
    try:
        data = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _line(value: object) -> str | None:
    return (_text(value, TASK_TEXT_LIMIT) or None) if isinstance(value, str) else None


def _plan(value: object) -> list[dict] | None:
    items = _json_list(value)
    if items is None:
        return None
    plan = []
    for item in items:
        text = _line(item.get("text")) if isinstance(item, dict) else None
        if text is None:
            continue
        status = item.get("status")
        plan.append({"text": text, "status": status if status in PLAN_STATUSES else "pending"})
    return plan[:PLAN_ITEMS]


def _subagents(value: object, now: float) -> list[dict] | None:
    items = _json_list(value)
    if items is None:
        return None
    agents = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = item.get("status") if item.get("status") in SUBAGENT_STATUSES else "running"
        kind = item.get("kind")
        agents.append(
            {
                "kind": (_text(kind, LABEL_LIMIT) if isinstance(kind, str) else "") or "agent",
                "description": _line(item.get("description")) or "",
                "status": status,
                "started_age_seconds": _age(now, item.get("started_at")),
                "finished_age_seconds": None if status == "running" else _age(now, item.get("finished_at")),
            }
        )
    return agents[:SUBAGENTS]


def _live(row: sqlite3.Row | dict, now: float) -> dict:
    return {
        "plan": _plan(row["plan"]),
        "current_task": _line(row["current_task"]),
        "last_finished": _line(row["last_finished"]),
        "subagents": _subagents(row["subagents"], now),
    }


def summary(row: sqlite3.Row | dict, now: float) -> dict:
    """One job's telemetry as the dashboard shows it."""
    tools = _json(row["tool_calls"], dict)
    counted = sorted(
        ((_shaped(name, _LABEL, "other"), _count(n)) for name, n in tools.items()),  # type: ignore[union-attr]
        key=lambda item: (-item[1], item[0]),
    )
    steps = []
    for step in list(_json(row["steps"], list))[-STEPS_SHOWN:]:  # type: ignore[arg-type]
        if not isinstance(step, dict):
            continue
        kind = step.get("kind") if step.get("kind") in STEP_KINDS else "tool"
        status = step.get("status")
        steps.append(
            {
                "kind": kind,
                "status": status if status in STEP_STATUSES else "running",
                "label": _shaped(step.get("label"), _LABEL, kind),
                "age_seconds": _age(now, step.get("at")),
            }
        )
    finished = row["finished_at"] is not None
    return {
        "job": _text(str(row["job_id"])[:8], 8),
        "harness": _harness(row["harness"]),
        "repo": _text(row["repo"], 100),
        "events": _count(row["events"]),
        "turns": _count(row["turns"]),
        "tool_calls": [{"name": name, "count": n} for name, n in counted[:TOOLS_SHOWN]],
        "tool_calls_total": sum(n for _, n in counted),
        "files_touched": _count(row["files_touched"]),
        "commands": _count(row["commands"]),
        "test_runs": _count(row["test_runs"]),
        "tests_passed": _maybe_count(row["tests_passed"]),
        "tests_failed": _maybe_count(row["tests_failed"]),
        "tokens_in": _count(row["tokens_in"]),
        "tokens_cached": _count(row["tokens_cached"]),
        "tokens_out": _count(row["tokens_out"]),
        "tokens_known": _tokens_known(row),
        "cost_usd": _money(row["cost_usd"]),
        "elapsed_seconds": _count(row["elapsed_seconds"]),
        "last_activity_age_seconds": _age(now, row["last_event_at"]),
        "updated_age_seconds": _age(now, row["updated_at"]),
        "finished": finished,
        "exit_code": row["exit_code"] if finished and isinstance(row["exit_code"], int) else None,
        "exit_reason": _shaped(row["exit_reason"], _REASON, "finished") if finished and row["exit_reason"] else None,
        "steps": steps,
        **_live(row, now),
    }


def _empty_rollup(state: str) -> dict:
    return {
        "state": state,
        "window_days": WINDOW_DAYS,
        "totals": _bucket(None),
        "by_harness": [],
        "by_repo": [],
        "recent": [],
    }


def _bucket(key: str | None) -> dict:
    item = {
        "jobs": 0,
        "tokens_in": 0,
        "tokens_cached": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "priced_jobs": 0,
        "unmetered_jobs": 0,
    }
    return item if key is None else {"name": key, **item}


def _add(bucket: dict, row: sqlite3.Row) -> None:
    bucket["jobs"] += 1
    if not _tokens_known(row):
        bucket["unmetered_jobs"] += 1
    for name in ("tokens_in", "tokens_cached", "tokens_out"):
        bucket[name] += _count(row[name])
    cost = _money(row["cost_usd"])
    if cost is not None:
        bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 4)
        bucket["priced_jobs"] += 1


def read_telemetry(connection: sqlite3.Connection, now: float) -> dict:
    """{"rows": {job id prefix: row}, "rollup": {...}} from the last WINDOW_DAYS."""
    present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    if present is None:
        return {"rows": {}, "rollup": _empty_rollup("none")}
    try:
        present = {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
        live = [
            name if name in present else f"NULL AS {name}"
            for name in (*LIVE_RUN_COLUMNS, "tokens_unknown")
        ]
        rows = connection.execute(
            f"SELECT {', '.join([*_COLUMNS, *live])} FROM {TABLE} WHERE updated_at >= ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (now - WINDOW_DAYS * 86400, ROW_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"rows": {}, "rollup": _empty_rollup("unreadable")}
    totals = _bucket(None)
    harness: dict[str, dict] = {}
    repo: dict[str, dict] = {}
    by_prefix: dict[str, sqlite3.Row] = {}
    for row in rows:
        by_prefix.setdefault(str(row["job_id"])[:8], row)
        _add(totals, row)
        name = _harness(row["harness"])
        _add(harness.setdefault(name, _bucket(name)), row)
        place = _text(row["repo"], 100)
        _add(repo.setdefault(place, _bucket(place)), row)
    finished = sorted(
        (row for row in rows if _number(row["finished_at"]) is not None),
        key=lambda row: _number(row["finished_at"]),
        reverse=True,
    )
    order = lambda item: (-item["tokens_in"] - item["tokens_out"], item["name"])  # noqa: E731
    return {
        "rows": by_prefix,
        "rollup": {
            "state": "ok" if rows else "empty",
            "window_days": WINDOW_DAYS,
            "totals": totals,
            "by_harness": sorted(harness.values(), key=order),
            "by_repo": sorted(repo.values(), key=order),
            "recent": [summary(row, now) for row in finished[:RECENT_JOBS]],
        },
    }


def attach(items: Iterable[dict], read: dict, now: float) -> None:
    """Give each running or queued job its summary (None: no telemetry)."""
    rows = read["rows"]
    for item in items:
        row = rows.get(item.get("job"))
        item["telemetry"] = summary(row, now) if row is not None else None


# --- server-side rendering (the same markup the script builds) -----------------------


def _e(value: object) -> str:
    return "—" if value is None else html.escape(str(value), quote=True)


def _num(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return _e(value)


def _usd(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"${value:,.2f}"


def _span(cls: str | None, text: object) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<span{attr}>{_e(text)}</span>"


def _table(rows: list[dict], first: str) -> str:
    if not rows:
        return '<p class="empty">no jobs with telemetry</p>'
    head = "".join(
        f"<th>{name}</th>"
        for name in (first, "jobs", "tokens in", "cached", "tokens out", "cost")
    )
    body = "".join(
        "<tr>"
        f"<td>{_e(row['name'])}</td>"
        f'<td class="num">{_num(row["jobs"])}</td>'
        f'<td class="num">{_num(row["tokens_in"])}</td>'
        f'<td class="num">{_num(row["tokens_cached"])}</td>'
        f'<td class="num">{_num(row["tokens_out"])}</td>'
        f'<td class="num">{_usd(row["cost_usd"]) if row["priced_jobs"] else "—"}</td>'
        "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _recent_table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">no finished jobs with telemetry</p>'
    head = "".join(
        f"<th>{name}</th>"
        for name in ("job", "repo", "harness", "tokens in", "tokens out", "cost", "exit")
    )
    body = "".join(
        "<tr>"
        f"<td>{_e(row['job'])}</td>"
        f"<td>{_e(row['repo'])}</td>"
        f"<td>{_e(row['harness'])}</td>"
        + (
            f'<td class="num">{_num(row["tokens_in"])}</td><td class="num">{_num(row["tokens_out"])}</td>'
            if row.get("tokens_known", True)
            else f'<td class="num muted">{NO_DATA}</td><td class="num muted">{NO_DATA}</td>'
        )
        + f'<td class="num">{_usd(row["cost_usd"])}</td>'
        + ('<td class="bad">' if row["exit_code"] not in (0, None) else "<td>")
        + f"{_e(row['exit_reason'])}</td>"
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def rollup_panel(snapshot: dict) -> str:
    """Efficiency: tokens and cost per job, per harness and per repo."""
    rollup = snapshot.get("telemetry") or _empty_rollup("none")
    totals = rollup["totals"]
    count = f"last {rollup['window_days']} days"
    if not totals["jobs"]:
        inner = '<p class="empty">no telemetry</p>'
    else:
        unpriced = totals["jobs"] - totals["priced_jobs"]
        stats = "".join(
            f'<div class="stat"><b>{value}</b>{_span(None, label)}</div>'
            for value, label in (
                (_num(totals["jobs"]), "jobs"),
                (_num(totals["tokens_in"]), "tokens in"),
                (_num(totals["tokens_cached"]), "of them cached"),
                (_num(totals["tokens_out"]), "tokens out"),
                (_usd(totals["cost_usd"]), "cost reported"),
            )
        )
        note = (
            f'<p class="muted">{unpriced} jobs report no cost (Codex does not)</p>'
            if unpriced
            else ""
        )
        if totals["unmetered_jobs"]:
            note += f'<p class="muted">{totals["unmetered_jobs"]} jobs report no token usage ({NO_DATA})</p>'

        inner = (
            f'<div class="stats">{stats}</div>{note}'
            '<h3 class="tele-head">per harness</h3>' + _table(rollup["by_harness"], "harness")
            + '<h3 class="tele-head">per repo</h3>' + _table(rollup["by_repo"], "repo")
            + '<h3 class="tele-head">recent jobs</h3>' + _recent_table(rollup["recent"])
        )
    return (
        '<section class="panel" id="tokens" aria-label="Tokens and cost">'
        f"<h2>{_span(None, 'Tokens and cost')}{_span('count', count)}</h2>{inner}</section>"
    )


TELEMETRY_CSS = (
    ".tele-head{margin:14px 0 6px;font:500 11px/1.4 var(--mono);color:var(--muted);"
    "text-transform:uppercase;letter-spacing:.08em}"
    "#tokens .stats{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 12px}"
    "#tokens .stat{flex:1 1 140px;padding:10px 14px 12px}"
    "#tokens .stat b{font:600 30px/1.15 var(--mono);letter-spacing:-.03em}"
    "#tokens .stat span{font:500 12px/1.3 var(--mono)}"
)


# The page script's renderers for the same markup. Spliced into the script's
# scope (lib/dashboard_pages/common.py, __TELEMETRY__), so el, add and num
# are the script's own helpers. textContent only.
TELEMETRY_SCRIPT = r"""
  // --- job telemetry (#93): the Efficiency rollup -------

  function usd(v) { return (v === null || v === undefined || !isFinite(v)) ? '—' : '$' + Number(v).toFixed(2); }
  function teleTable(root, rows, first, recent) {
    if (!rows.length) { add(root, el('p', 'empty', recent ? 'no finished jobs with telemetry' : 'no jobs with telemetry')); return; }
    var table = add(root, el('table'));
    var head = add(add(table, el('thead')), el('tr'));
    (recent ? ['job', 'repo', 'harness', 'tokens in', 'tokens out', 'cost', 'exit']
            : [first, 'jobs', 'tokens in', 'cached', 'tokens out', 'cost']).forEach(function (h) { add(head, el('th', null, h)); });
    var tbody = add(table, el('tbody'));
    rows.forEach(function (r) {
      var tr = add(tbody, el('tr'));
      if (recent) {
        add(tr, el('td', null, r.job));
        add(tr, el('td', null, r.repo));
        add(tr, el('td', null, r.harness));
        var known = r.tokens_known !== false;
        add(tr, el('td', known ? 'num' : 'num muted', known ? num(r.tokens_in) : 'no data'));
        add(tr, el('td', known ? 'num' : 'num muted', known ? num(r.tokens_out) : 'no data'));
        add(tr, el('td', 'num', usd(r.cost_usd)));
        add(tr, el('td', (r.exit_code === 0 || r.exit_code === null || r.exit_code === undefined) ? null : 'bad', r.exit_reason || '—'));
      } else {
        add(tr, el('td', null, r.name));
        add(tr, el('td', 'num', num(r.jobs)));
        add(tr, el('td', 'num', num(r.tokens_in)));
        add(tr, el('td', 'num', num(r.tokens_cached)));
        add(tr, el('td', 'num', num(r.tokens_out)));
        add(tr, el('td', 'num', r.priced_jobs ? usd(r.cost_usd) : '—'));
      }
    });
  }
  function renderTokens(d) {
    var r = d.telemetry || {window_days: 7, totals: {jobs: 0}, by_harness: [], by_repo: [], recent: []};
    var root = panel('tokens', 'Tokens and cost', 'last ' + r.window_days + ' days');
    if (!root) return;
    var t = r.totals || {jobs: 0};
    if (!t.jobs) { add(root, el('p', 'empty', 'no telemetry')); return; }
    var stats = add(root, el('div', 'stats'));
    [[num(t.jobs), 'jobs'], [num(t.tokens_in), 'tokens in'], [num(t.tokens_cached), 'of them cached'],
     [num(t.tokens_out), 'tokens out'], [usd(t.cost_usd), 'cost reported']].forEach(function (s) {
      var box = add(stats, el('div', 'stat'));
      add(box, el('b', null, s[0]));
      add(box, el('span', null, s[1]));
    });
    var unpriced = t.jobs - t.priced_jobs;
    if (unpriced) add(root, el('p', 'muted', unpriced + ' jobs report no cost (Codex does not)'));
    if (t.unmetered_jobs) add(root, el('p', 'muted', t.unmetered_jobs + ' jobs report no token usage (no data)'));
    add(root, el('h3', 'tele-head', 'per harness'));
    teleTable(root, r.by_harness || [], 'harness', false);
    add(root, el('h3', 'tele-head', 'per repo'));
    teleTable(root, r.by_repo || [], 'repo', false);
    add(root, el('h3', 'tele-head', 'recent jobs'));
    teleTable(root, r.recent || [], null, true);
  }
"""
