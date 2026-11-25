"""health.html (System, #92, #141, #537): Broken now, the components matrix (a device list on a
phone) and process details, version drift when a component is behind, the poll and Linear budget
beside poller.log, rules and limits, and harnesses with no live waiter."""

from __future__ import annotations

import re

from lib.health import CODE_COMPONENTS
from lib.pr_title import label
from lib.dashboard_pages.common import (
    _b, _chip, duration, _e, _feed, _num, _panel, _span, _td, _ticket, _when,
)
from lib.dashboard_static import read_asset


CSS = read_asset("css/system.css")


SCRIPT_LOG = read_asset("js/system_log.js")


SCRIPT_HEALTH = read_asset("js/system_health.js")


SCRIPT_COMPONENTS = read_asset("js/system_components.js")


_LOG_KINDS = (
    (re.compile(r"exit rc=(?!0\b)\d+"), "bad"),
    (re.compile(r"Traceback"), "bad"),
    (re.compile(r"\b\w*(?:Error|Exception):"), "bad"),
    (re.compile(r"\b(?:ERROR|CRITICAL)\b"), "bad"),
    (re.compile(r"\b(?:WARNING|WARN)\b"), "warn"),
    (re.compile(r"another orch poll owns"), "warn"),
    (re.compile(r"poller start"), "start"),
)


def _log_kinds(lines: list[str]) -> list[str | None]:
    kinds: list[str | None] = []
    traceback = False
    for line in lines:
        if traceback and line[:1].isspace():
            kinds.append("bad")
            continue
        traceback = "Traceback" in line
        kinds.append(next((kind for pattern, kind in _LOG_KINDS if pattern.search(line)), None))
    return kinds


def _log(snapshot: dict) -> str:
    log = snapshot["poller"]["log"]
    count = (
        f"written {duration(log['age_seconds'])} before the snapshot"
        if log["present"]
        else "absent"
    )
    tail = log["tail"]
    lines = "".join(
        f'<div class="{kind}">{_e(line)}</div>' if kind else f"<div>{_e(line)}</div>"
        for line, kind in reversed(list(zip(tail, _log_kinds(tail))))
    )
    inner = (
        f'<p class="sys-note">last {len(log["tail"])} lines, newest first, redacted</p>'
        f'<div class="sys-log">{lines}</div>'
    )
    return _panel("log", "poller.log", inner, count=count)


def _project_line(project: dict) -> str:
    rounds = project.get("rounds")
    if project.get("status") != "ok" or not rounds:
        return f"ship mode and round cap: unknown ({project.get('reason') or 'not read'})"
    approver = f" (approver {project['approver']})" if project.get("approver") else ""
    return (
        f"ship mode {project['ship']}{approver} · "
        f"round cap {rounds['low']} low, {rounds['high']} high"
    )


def _rules(snapshot: dict) -> str:
    rules = snapshot.get("rules") or []
    rows = (
        '<ul class="sys-rules">'
        + "".join(
            f"<li>{_span('key', row['key'])}{_span('text', row['text'])}</li>" for row in rules
        )
        + "</ul>"
        if rules
        else '<p class="empty">rules unavailable</p>'
    )
    projects = snapshot.get("projects") or []
    listing = (
        '<ul class="feed">'
        + "".join(
            "<li>"
            + _span("ticket", p["repo"])
            + _span("what", p["tracker"])
            + _span("sys-proj", _project_line(p))
            + "</li>"
            for p in projects
        )
        + "</ul>"
        if projects
        else '<p class="empty">no projects loaded</p>'
    )
    inner = rows + '<div class="sys-sub">projects</div>' + listing
    return _panel("rules", "Rules and limits", inner, count="read-only")


# --- system page: poll and Linear budget ----------------------------------------------


_POLL_STATE = {"ok": ("OK", "ok"), "late": ("LATE", "warn"), "stale": ("STALE", "bad"),
               "unknown": ("UNKNOWN", "muted")}


_BUDGET_STATE = {"ok": ("OK", "ok"), "deferring": ("DEFERRING", "warn"),
                 "exhausted": ("EXHAUSTED", "bad"), "rate_limited": ("RATE LIMITED", "bad")}


def _health(snapshot: dict) -> str:
    poller = snapshot["poller"]
    health = poller["health"] or {"state": "unknown"}
    budget = poller["linear_budget"] or {"state": "ok"}
    label, cls = _POLL_STATE.get(health["state"], _POLL_STATE["unknown"])
    cycle = health.get("last_cycle_age_seconds")
    cycle_text = (
        _span("muted", "cycle time unknown (live push off)")
        if cycle is None
        else _span(None, f"last cycle {duration(cycle)} ago")
    )
    blabel, bcls = _BUDGET_STATE.get(budget["state"], ("OTHER", "muted"))
    errors = poller["errors"]
    limit_hits = (budget.get("exhausted_last_hour") or 0) + (budget.get("rate_limited_last_hour") or 0)
    stats = [
        (errors["last_hour"], "poll errors, 1 h", "bad" if errors["last_hour"] else ""),
        (errors["last_day"], "poll errors, 24 h", ""),
        (budget.get("deferrals_last_hour"), "Linear deferrals, 1 h",
         "warn" if budget.get("deferrals_last_hour") else ""),
        (limit_hits, "Linear limit hits, 1 h", "bad" if limit_hits else ""),
    ]
    stat_html = "".join(
        f'<div class="stat">{_b(v, c)}{_span(None, text)}</div>'
        for v, text, c in stats
    )
    recent = errors["recent"][:3]
    recent_html = (
        _feed(
            recent,
            "",
            lambda row: _span(
                "ticket",
                row["repo"] if row["repo"] == "poll" or row["ticket_id"] == "-" else _ticket(row),
            )
            + _span("what", row["error"])
            + _when(row["age_seconds"], suffix=" ago"),
        )
        if recent
        else ""
    )
    inner = (
        f'<div class="row">{_span("label", "poll")}{_chip(label, cls)}{cycle_text}</div>'
        f'<div class="row">{_span("label", "linear")}{_chip(blabel, bcls)}'
        + _span(
            "muted",
            f"{_num(budget.get('requests_per_hour'))} req/h · "
            f"{_num(budget.get('complexity_per_hour'))} complexity/h",
        )
        + "</div>"
        f'<div class="stats">{stat_html}</div>'
        f'<p class="muted">last poller activity {duration(poller["last_activity_age_seconds"])} ago</p>'
        + recent_html
    )
    return _panel("health", "Poll and Linear budget", inner)


# --- health page (#92, #141) ---------------------------------------------------------


_HEALTH_STATE = {"up": ("UP", "ok"), "stale": ("STALE", "warn"), "down": ("DOWN", "bad"),
                 "unknown": ("UNKNOWN", "muted")}


_ANCHOR = re.compile(r"[A-Za-z0-9_-]+")


def _anchor_attr(value: object) -> str:
    return f' id="{value}"' if isinstance(value, str) and _ANCHOR.fullmatch(value) else ""


def _matrix_key(row: dict) -> str:
    return f"{row['component']} · {row['bot']}" if row.get("bot") else row["component"]


def _valid_anchor(value: object) -> str | None:
    return value if isinstance(value, str) and _ANCHOR.fullmatch(value) else None


_BROKEN_STATES = (("down", "bad"), ("stale", "warn"))
_BROKEN_TONE = dict(_BROKEN_STATES)


def broken_items(health: dict) -> list[dict]:
    rows = [row for row in health.get("components") or [] if row.get("configured") is not False]
    items = []
    for state, tone in _BROKEN_STATES:
        for row in rows:
            if row["state"] != state:
                continue
            age = row.get("last_seen_age_seconds")
            anchor = _valid_anchor(row.get("anchor"))
            items.append({
                "kind": state, "what": _matrix_key(row), "device": row["device"], "tone": tone,
                "for_seconds": age, "last_seen_et": row.get("last_seen_et"),
                "reason": row.get("reason") or ("never seen" if age is None else ""),
                "anchor": f"broken-{anchor}" if anchor else None,
            })
    for row in health.get("no_waiter") or []:
        anchor = _valid_anchor(row.get("anchor"))
        items.append({
            "kind": "no-waiter", "what": f"queued job · {row['harness']}", "device": None,
            "tone": "bad", "for_seconds": None, "last_seen_et": None, "reason": _no_waiter_text(row),
            "anchor": f"broken-{anchor}" if anchor else None,
        })
    return items


def _broken_count(health: dict, items: list[dict]) -> str:
    down = sum(item["kind"] == "down" for item in items)
    stale = sum(item["kind"] == "stale" for item in items)
    queued = sum(row["count"] for row in health.get("no_waiter") or [])
    parts = [
        (down, f"{down} component{'' if down == 1 else 's'} down"),
        (stale, f"{stale} component{'' if stale == 1 else 's'} stale"),
        (queued, f"{queued} queued job{'' if queued == 1 else 's'} with no waiter"),
    ]
    return " · ".join(text for n, text in parts if n)


def _px():
    from lib.dashboard_pages import shell

    return shell


def _broken_row(item: dict) -> str:
    key = f"broken-{item['kind']}-{item['what']}-{item['device'] or ''}"
    strip = _px().px_strip(key, effect="static", tint="#f7768e", cell=4, height=16)
    seconds, seen = item["for_seconds"], item["last_seen_et"]
    return (
        f'<tr{_anchor_attr(item["anchor"])}>'
        f'<td><span class="sys-what"><span class="sys-px" aria-hidden="true">{strip}</span>'
        f'{_span("sys-name", item["what"])}</span></td>'
        + _td(item["device"])
        + f"<td>{_chip(item['kind'], item['tone'])}</td>"
        + (_td(None, "muted") if seconds is None else _td(duration(seconds), "sys-for"))
        + (_td(None, "muted") if seen is None else _td(seen, "sys-seen"))
        + _td(item["reason"], "sys-why")
        + "</tr>"
    )


def _broken(snapshot: dict) -> str:
    health = snapshot.get("health") or {}
    items = broken_items(health)
    if not items:
        return '<section class="panel" id="broken" aria-label="Broken now" hidden></section>'
    heading_id = "" if health.get("no_waiter") else ' id="no-waiter"'
    heads = ("what", "device", "state", "for", "last seen", "why")
    return (
        '<section class="panel" id="broken" aria-label="Broken now">'
        f'<h2{heading_id}>{_span(None, "Broken now")}{_span("count bad", _broken_count(health, items))}</h2>'
        '<div class="sys-scroll"><table class="sys-table sys-broken"><thead><tr>'
        + "".join(f"<th>{text}</th>" for text in heads)
        + "</tr></thead><tbody>"
        + "".join(_broken_row(item) for item in items)
        + "</tbody></table></div></section>"
    )


def _matrix(snapshot: dict, rows: list[dict]) -> tuple[list[str], list[str], dict]:
    """Rows by component kind (a waiter per bot), columns by device in lane
    order, then any device only the heartbeats name (matrixOf in the script)."""
    keys: list[str] = []
    seen: list[str] = []
    cells: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = _matrix_key(row)
        if key not in keys:
            keys.append(key)
        if row["device"] not in seen:
            seen.append(row["device"])
        cells[(key, row["device"])] = row
    listed = [device["name"] for device in (snapshot.get("slots") or {}).get("devices") or []]
    devices = [name for name in listed if name in seen] + [name for name in seen if name not in listed]
    return keys, devices, cells


def _matrix_cell(row: dict) -> str:
    label, tone = _HEALTH_STATE.get(row["state"], ("OTHER", "muted"))
    word = label.lower()
    configured = row.get("configured") is not False
    age = row.get("last_seen_age_seconds")
    if _is_up(row):
        return (f'<td><span class="sys-sq ok" role="img" aria-label="up" '
                f'title="up · beat {duration(age)} ago"></span></td>')
    if not configured:
        tone = "muted"
    text = f"{word} · never seen" if age is None else f"{word} {duration(age)}"
    inner = f'<span class="sys-blk {tone}" aria-hidden="true"></span>{_span(None, text)}'
    anchor = _valid_anchor(row.get("anchor"))
    if configured and anchor and row["state"] in _BROKEN_TONE:
        return f'<td><a class="sys-cell {tone}" href="#broken-{anchor}">{inner}</a></td>'
    return f'<td><span class="sys-cell">{inner}</span></td>'


def _is_up(row: dict) -> bool:
    return row.get("configured") is not False and row["state"] == "up"


def _drift_shown(health: dict) -> bool:
    return bool(health.get("known")) and bool(version_drift(health)["behind"])


def _components_count(health: dict) -> tuple[str, str]:
    if not health.get("known"):
        return "unknown", "count"
    rows = [row for row in health.get("components") or [] if row.get("configured") is not False]
    drift = version_drift(health)
    behind = len(drift["behind"])
    last = (
        f"{behind} behind" if behind
        else "version unknown" if drift["newest"] == "unknown"
        else f"all on {drift['newest'][:7]}"
    )
    up = sum(_is_up(row) for row in rows)
    return f"{len(rows)} configured · {up} up · {last}", "count warn" if behind else "count"


def _device_row(row: dict) -> str:
    label, tone = _HEALTH_STATE.get(row["state"], ("OTHER", "muted"))
    configured = row.get("configured") is not False
    age = row.get("last_seen_age_seconds")
    word = label.lower() + ("" if configured else " · not configured")
    return (
        f'<div class="sys-dev-row{"" if _is_up(row) else " two"}">'
        + _span("sys-kind", _matrix_key(row))
        + f'<span class="sys-cell"><span class="sys-blk {tone if configured else "muted"}" '
        f'aria-hidden="true"></span>{_span(None, word)}</span>'
        + _span("sys-seen", "never" if age is None else f"{duration(age)} ago")
        + "</div>"
    )


def _phone_devices(keys: list[str], devices: list[str], cells: dict) -> str:
    blocks = []
    for device in devices:
        rows = [cells[(key, device)] for key in keys if (key, device) in cells]
        ordered = [row for row in rows if not _is_up(row)] + [row for row in rows if _is_up(row)]
        blocks.append(
            '<div class="sys-dev"><div class="sys-dev-head">'
            + _span("sys-name", device)
            + _span("muted", f"{sum(map(_is_up, rows))} of {len(rows)} up")
            + "</div>"
            + "".join(_device_row(row) for row in ordered)
            + "</div>"
        )
    return f'<div class="sys-devices">{"".join(blocks)}</div>'


_PROCESS_RANK = {"down": 0, "stale": 1}


def _et_cell(value: str | None) -> str | None:
    return value.removesuffix(" ET") if value else value


def _components(snapshot: dict) -> str:
    health = snapshot.get("health") or {"known": False, "components": []}
    rows = health.get("components") or []
    count, count_cls = _components_count(health)
    head = {"count": count, "count_cls": count_cls,
            "count_id": None if _drift_shown(health) else "drift",
            "heading_id": None if broken_items(health) else "no-waiter"}
    if not health.get("known"):
        text = f"unknown: {health['error']}" if health.get("error") else "no heartbeats recorded yet"
        return _panel("components", "Components", f'<p class="empty">{_e(text)}</p>', **head)
    keys, devices, cells = _matrix(snapshot, rows)
    matrix = (
        '<div class="sys-scroll sys-matrix"><table class="sys-table"><thead><tr>'
        + "".join(f"<th>{_e(text)}</th>" for text in ["component", *devices])
        + "</tr></thead><tbody>"
        + "".join(
            f'<tr><td class="sys-kind">{_e(key)}</td>'
            + "".join(
                _matrix_cell(cells[(key, device)]) if (key, device) in cells
                else '<td class="muted">—</td>'
                for device in devices
            )
            + "</tr>"
            for key in keys
        )
        + "</tbody></table></div>"
        + _phone_devices(keys, devices, cells)
    )
    heads = ("component", "bot", "device", "state", "last seen (ET)", "ago", "pid", "version",
             "started (ET)")
    body = []
    for row in sorted(rows, key=lambda row: _PROCESS_RANK.get(row["state"], 2)):
        label, cls = _HEALTH_STATE.get(row["state"], ("OTHER", "muted"))
        reason = _span("muted", " " + row["reason"]) if row.get("reason") else ""
        if row.get("last_cycle_seconds") is not None:
            reason += _span("muted", f" last cycle {int(row['last_cycle_seconds'])}s")
        if row.get("configured") is False:
            reason += _span("muted", " (not configured)")
        body.append(
            f'<tr{_anchor_attr(row.get("anchor"))}>'
            + _td(row["component"])
            + _td(row["bot"] or None)
            + _td(row["device"])
            + f"<td>{_chip(label, cls)}{reason}</td>"
            + _td(_et_cell(row["last_seen_et"]) or "never")
            + _td(duration(row["last_seen_age_seconds"]), "num")
            + _td(row["pid"], "num")
            + (f'<td>{_e(row["version"])}{_span("warn", " stale code")}</td>'
               if row.get("stale_code") else _td(row["version"]))
            + _td(_et_cell(row["started_at_et"]))
            + "</tr>"
        )
    thresholds = health.get("thresholds") or {}
    note = (
        f"up: a beat within {thresholds.get('stale_beats', 3)} intervals "
        f"({thresholds.get('beat_seconds', 15):g} s each; device probes every "
        f"{thresholds.get('probe_seconds', 60):g} s) · stale: within "
        f"{thresholds.get('down_beats', 8)} · down: older, stopped or never seen"
        " · unknown: this snapshot is too old to tell"
    )
    details = (
        '<details class="sys-details"><summary>process details</summary>'
        '<div class="sys-scroll"><table class="sys-table wide"><thead><tr>'
        + "".join(f"<th>{_e(text)}</th>" for text in heads)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div></details>"
    )
    inner = matrix + f'<p class="sys-note sys-legend">{_e(note)}</p>' + details
    return _panel("components", "Components", inner, **head)


def version_groups(health: dict) -> list[dict]:
    """Version drift (SPEC 9: group components by version): the newest
    version first (the checkout's, else the latest started), then the other
    known versions by latest start, then unknown (driftOf in the script)."""
    groups: dict[str, dict] = {}
    for row in health.get("components") or []:
        if row.get("configured") is False:
            continue
        version = str(row.get("version") or "unknown")
        group = groups.setdefault(version, {"version": version, "rows": [], "started": None,
                                            "started_et": None})
        group["rows"].append(row)
        started = row.get("started_at")
        if started is not None and (group["started"] is None or started > group["started"]):
            group["started"], group["started_et"] = started, row.get("started_at_et")
    by_start = sorted(
        (v for v in groups if v != "unknown"),
        key=lambda v: -(groups[v]["started"] or 0),
    )
    code = health.get("code_version")
    newest = str(code) if code and code != "unknown" else (by_start[0] if by_start else None)
    ordered = [groups[newest]] if newest in groups else []
    ordered += [groups[v] for v in by_start if v != newest]
    if "unknown" in groups:
        ordered.append(groups["unknown"])
    for group in ordered:
        version = group["version"]
        group["tone"] = "ok" if version == newest else "bad" if version == "unknown" else "warn"
    return ordered


def version_drift(health: dict | None) -> dict:
    """newest: the checkout's version; behind: configured code components on
    another known version (lib/health.py stale_code); unknown: configured code
    components whose version is unknown, not counted as behind."""
    health = health or {}
    rows = [
        row for row in health.get("components") or []
        if row.get("configured") is not False and row.get("component") in CODE_COMPONENTS
    ]
    return {
        "newest": str(health.get("code_version") or "unknown"),
        "behind": [row["label"] for row in rows if row.get("stale_code")],
        "unknown": [row["label"] for row in rows if row.get("version") == "unknown"],
    }


def _drift(health: dict) -> str:
    items = []
    for group in version_groups(health):
        rows = group["rows"]
        tone = group["tone"]
        if tone == "ok":
            meta = f"{len(rows)} component" + ("" if len(rows) == 1 else "s")
        else:
            meta = ", ".join(str(row["label"]) for row in rows)
            if group["started_et"]:
                meta += f" · started {group['started_et']}"
        blocks = "".join(f'<span class="sys-blk {tone}"></span>' for _ in rows)
        items.append(
            '<div class="sys-ver">'
            + _span(f"name {tone}", group["version"])
            + f'<span class="blocks" aria-hidden="true">{blocks}</span>'
            + _span("meta", meta)
            + "</div>"
        )
    count = _span("count warn", f"{len(version_drift(health)['behind'])} behind")
    return (
        '<section class="panel" id="drift" aria-label="Version drift">'
        f'<h2>{_span(None, "Version drift")}{count}</h2>'
        f'<div class="sys-drift">{"".join(items)}</div></section>'
    )


def _versions(snapshot: dict) -> str:
    health = snapshot.get("health") or {}
    if not _drift_shown(health):
        return '<div id="versions" hidden></div>'
    return f'<div id="versions">{_drift(health)}</div>'


def _log_row(snapshot: dict) -> str:
    return f'<div class="sys-row">{_health(snapshot)}{_log(snapshot)}</div>'


def _no_waiter_text(row: dict) -> str:
    return (
        f"no waiter for {row['harness']}: {row['count']} queued job"
        + ("" if row["count"] == 1 else "s")
        + f" ({', '.join(row['jobs'])})"
    )


def _no_waiter(snapshot: dict) -> str:
    rows = (snapshot.get("health") or {}).get("no_waiter") or []
    if not rows:
        return '<div id="waiters" hidden></div>'
    items = "".join(
        f'<li{_anchor_attr(row.get("anchor"))}>'
        + _chip("no waiter", "bad")
        + _span("ticket", f"waiter {row['bot']}")
        + _span("what", _no_waiter_text(row))
        + "</li>"
        for row in rows
    )
    panel = _panel("no-waiter", "Queued jobs with no waiter", f'<ul class="feed">{items}</ul>',
                   count="1 harness" if len(rows) == 1 else f"{len(rows)} harnesses")
    return f'<div id="waiters">{panel}</div>'
