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

from lib.telemetry import (
    LABEL_LIMIT,
    LIVE_RUN_COLUMNS,
    PLAN_ITEMS,
    SUBAGENTS,
    TABLE,
    TASK_TEXT_LIMIT,
    effective_cost_usd,
)
from lib.dashboard_static import read_asset

WINDOW_DAYS = 7
RECENT_JOBS = 12
STEPS_SHOWN = 12
TOOLS_SHOWN = 8
ROW_LIMIT = 2000
TEXT_LIMIT = 160
NO_DATA = "no data"
NO_COST = "no cost reported"
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
        "spend": {"productive_usd": 0.0, "wasted_usd": 0.0, "estimated_total_usd": 0.0},
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
    has_jobs = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone()
        is not None
    )
    cutoff = now - WINDOW_DAYS * 86400
    try:
        cols = {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
        live = [
            f"t.{name}" if name in cols else f"NULL AS {name}"
            for name in (*LIVE_RUN_COLUMNS, "tokens_unknown")
        ]
        # Full 7-day rollup without silent ROW_LIMIT truncation: stream lightweight
        # rollup columns over the entire window, and load detailed summaries only
        # for recent/active jobs.
        job_state_col = "j.state AS job_state" if has_jobs else "NULL AS job_state"
        job_join = f"LEFT JOIN jobs j ON j.id = t.job_id" if has_jobs else ""
        rollup_cursor = connection.execute(
            f"SELECT t.job_id, t.harness, t.repo, t.tokens_in, t.tokens_cached, t.tokens_out, "
            f"{'t.tokens_unknown' if 'tokens_unknown' in cols else 'NULL AS tokens_unknown'}, "
            f"t.cost_usd, t.exit_code, {job_state_col} "
            f"FROM {TABLE} t {job_join} WHERE t.updated_at >= ? ORDER BY t.updated_at DESC",
            (cutoff,),
        )
        totals = _bucket(None)
        harness: dict[str, dict] = {}
        repo: dict[str, dict] = {}
        productive_usd = 0.0
        wasted_usd = 0.0
        estimated_total_usd = 0.0
        total_rows = 0
        for row in rollup_cursor:
            total_rows += 1
            _add(totals, row)
            name = _harness(row["harness"])
            _add(harness.setdefault(name, _bucket(name)), row)
            place = _text(row["repo"], 100)
            _add(repo.setdefault(place, _bucket(place)), row)
            eff = effective_cost_usd(
                str(row["harness"] or ""),
                _money(row["cost_usd"]),
                tokens_in=_count(row["tokens_in"]),
                tokens_cached=_count(row["tokens_cached"]),
                tokens_out=_count(row["tokens_out"]),
            )
            if eff is not None:
                estimated_total_usd += eff
                jstate = row["job_state"]
                if jstate in ("failed", "cancelled") or (
                    jstate is None and isinstance(row["exit_code"], int) and row["exit_code"] != 0
                ):
                    wasted_usd += eff
                else:
                    productive_usd += eff
        col_list = ", ".join([*(f"t.{c}" for c in _COLUMNS), *live])
        detail_rows = connection.execute(
            f"SELECT {col_list} FROM {TABLE} t WHERE t.updated_at >= ? "
            "ORDER BY t.updated_at DESC LIMIT ?",
            (cutoff, ROW_LIMIT),
        ).fetchall()
        finished = connection.execute(
            f"SELECT {col_list} FROM {TABLE} t WHERE t.updated_at >= ? AND t.finished_at IS NOT NULL "
            "ORDER BY t.finished_at DESC LIMIT ?",
            (cutoff, RECENT_JOBS),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"rows": {}, "rollup": _empty_rollup("unreadable")}
    by_prefix: dict[str, sqlite3.Row] = {}
    for row in detail_rows:
        by_prefix.setdefault(str(row["job_id"])[:8], row)
    order = lambda item: (-item["tokens_in"] - item["tokens_out"], item["name"])  # noqa: E731
    return {
        "rows": by_prefix,
        "rollup": {
            "state": "ok" if total_rows else "empty",
            "window_days": WINDOW_DAYS,
            "totals": totals,
            "spend": {
                "productive_usd": round(productive_usd, 4),
                "wasted_usd": round(wasted_usd, 4),
                "estimated_total_usd": round(estimated_total_usd, 4),
            },
            "by_harness": sorted(harness.values(), key=order),
            "by_repo": sorted(repo.values(), key=order),
            "recent": [summary(row, now) for row in finished],
        },
    }


def ticket_totals(connection: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """{(repo casefolded, ticket id): {jobs, tokens_in, tokens_out, cost_usd}}
    over every job a ticket ever had, with no time window. Tokens add up the
    metered jobs (`_tokens_known`) and cost the priced ones; each is None when
    no job of the ticket has any, so a missing reading never shows as 0."""
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "jobs" not in tables:
        return {}
    present = (
        {str(row[1]) for row in connection.execute(f"PRAGMA table_info({TABLE})")}
        if TABLE in tables
        else set()
    )
    joined = "job_id" in present
    columns = ", ".join(
        f"t.{name} AS {name}" if joined and name in present else f"NULL AS {name}"
        for name in ("tokens_in", "tokens_cached", "tokens_out", "tokens_unknown", "cost_usd")
    )
    totals: dict[tuple[str, str], dict] = {}
    for row in connection.execute(
        f"SELECT jobs.repo, jobs.ticket_id, {columns} FROM jobs"
        + (f" LEFT JOIN {TABLE} t ON t.job_id = jobs.id" if joined else "")
    ):
        key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
        item = totals.setdefault(
            key, {"jobs": 0, "tokens_in": None, "tokens_out": None, "cost_usd": None}
        )
        item["jobs"] += 1
        if _tokens_known(row):
            item["tokens_in"] = (item["tokens_in"] or 0) + _count(row["tokens_in"])
            item["tokens_out"] = (item["tokens_out"] or 0) + _count(row["tokens_out"])
        cost = _money(row["cost_usd"])
        if cost is not None:
            item["cost_usd"] = round((item["cost_usd"] or 0.0) + cost, 4)
    return totals


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
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    cents = math.floor(value * 100 + 0.5)
    return f"${cents // 100:,}.{cents % 100:02d}"


TOKEN_UNITS = ((10**9, "B"), (10**6, "M"), (10**3, "K"))


def _compact(value: int) -> str:
    if value < 10_000:
        return _num(value)
    divisor = 10 ** (len(str(value)) - 4)
    q = (value + divisor // 2) // divisor
    if q == 10**4:
        q, divisor = 1000, divisor * 10
    value = q * divisor
    unit, letter = next(pair for pair in TOKEN_UNITS if pair[0] <= value)
    places = max(0, 4 - len(str(value // unit)))
    digits = str(value * 10**places // unit)
    cut = len(digits) - places
    return f"{digits[:cut]}.{digits[cut:]}".rstrip("0").rstrip(".") + letter


def _span(cls: str | None, text: object) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<span{attr}>{_e(text)}</span>"


def _table(rows: list[dict], first: str, chips: bool = False) -> str:
    if not rows:
        return '<p class="empty">no jobs with telemetry</p>'
    head = "".join(
        f"<th>{name}</th>"
        for name in (first, "jobs", "tokens in", "cached", "tokens out", "cost")
    )
    body = "".join(
        "<tr>"
        f"<td>{_span('bot ' + row['name'], row['name']) if chips else _e(row['name'])}</td>"
        f'<td class="num">{_num(row["jobs"])}</td>'
        f'<td class="num">{_num(row["tokens_in"])}</td>'
        f'<td class="num">{_num(row["tokens_cached"])}</td>'
        f'<td class="num">{_num(row["tokens_out"])}</td>'
        + (f'<td class="num">{_usd(row["cost_usd"])}</td>' if row["priced_jobs"]
           else f'<td class="num muted">{NO_COST}</td>')
        + "</tr>"
        for row in rows
    )
    return f'<div class="ef-scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _cached(totals: dict) -> str:
    tokens_in, cached = totals["tokens_in"], totals["tokens_cached"]
    if not tokens_in:
        return ""
    share = (200 * cached + tokens_in) // (2 * tokens_in)
    return (
        f'<div class="cached" title="{_compact(cached)} of {_compact(tokens_in)} tokens in were cached">'
        f"{share}% cached</div>"
    )


def rollup_panel(snapshot: dict) -> str:
    """Efficiency: cost and token tiles, then per harness, then per repo folded away."""
    rollup = snapshot.get("telemetry") or _empty_rollup("none")
    totals = rollup["totals"]
    heading = f"Cost, last {rollup['window_days']} days"
    if not totals["jobs"]:
        inner = '<p class="empty">no telemetry</p>'
    else:
        tiles = (
            (_usd(totals["cost_usd"]) if totals["priced_jobs"] else None, "cost", "", ""),
            (_num(totals["jobs"]), "jobs", "", ""),
            (_compact(totals["tokens_in"]), "tokens in", f' title="{_num(totals["tokens_in"])}"', _cached(totals)),
            (_compact(totals["tokens_out"]), "tokens out", f' title="{_num(totals["tokens_out"])}"', ""),
        )
        stats = "".join(
            f'<div class="stat"{title}>'
            + (f'<b class="none">{NO_COST}</b>' if value is None else f"<b>{value}</b>")
            + f"{_span(None, label)}{sub}</div>"
            for value, label, title, sub in tiles
        )
        unpriced = sorted(
            ((row["jobs"] - row["priced_jobs"], row["name"]) for row in rollup["by_harness"]
             if row["jobs"] > row["priced_jobs"]),
            key=lambda pair: (-pair[0], pair[1]),
        )
        note = (
            f'<p class="muted">{sum(count for count, _ in unpriced)} jobs report no cost: '
            f'{_e(", ".join(f"{name} {count}" for count, name in unpriced))}</p>'
            if unpriced
            else ""
        )
        if totals["unmetered_jobs"]:
            note += f'<p class="muted">{totals["unmetered_jobs"]} jobs report no token usage ({NO_DATA})</p>'

        inner = (
            f'<div class="stats">{stats}</div>'
            '<h3 class="tele-head">per harness</h3>' + _table(rollup["by_harness"], "harness", chips=True)
            + note
            + "<details><summary>per repo</summary>" + _table(rollup["by_repo"], "repo") + "</details>"
        )
    return (
        f'<section class="panel" id="tokens" aria-label="{_e(heading)}">'
        f"<h2>{_span(None, heading)}{_span('count', 'as reported by each harness')}</h2>{inner}</section>"
    )


TELEMETRY_CSS = read_asset("css/telemetry.css")


# The page script's renderers for the same markup. Spliced into the script's
# scope (lib/dashboard_pages/common.py, __TELEMETRY__), so el, add and num
# are the script's own helpers. textContent only.
TELEMETRY_SCRIPT = read_asset("js/telemetry.js")
