"""ticket.html#t=<repo>#<ticket>: the ticket detail page (TicketR2.dc.html). Breadcrumb,
hero, Run log, Live run slot, Journey, Can come next and the collapsed Full state chart. The Run log and the Journey render from one visit list and share
one selection."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from lib.dashboard_flow import FLOWMAP_SCRIPT, flowmap_layout, ticket_phases
from lib.dashboard_telemetry import NO_DATA
from lib.dashboard_pages.cockpit import ticket_cockpit_html
from lib.dashboard_pages.common import (
    anchor, _bot, _chip, duration, _e, model_effort, _span, _state_chip,
    _thread_key, ticket_address, _tile_label,
)
from lib.dashboard_pages import journey_canvas
from lib.dashboard_pages.journey_canvas import TINT
from lib.dashboard_pages.lanes import TILE_STATE
from lib.dashboard_pages.tickets import WAITING, _px
from lib.dashboard_static import read_asset
from lib.exposure import UNKNOWN


VERBS = {name: spec["verbs"] for name, spec in TILE_STATE.items() if "verbs" in spec}
HB_TONE = {"live": "ok", "warn": "warn", "bad": "bad", "none": ""}
HEARTBEAT_TONE = {name: HB_TONE[spec["hb"]] for name, spec in TILE_STATE.items()}
NEXT_ORDER = {"normal": 0, "human": 1, "stop": 2}
FIRST_REASON = "ticket arrives"
STOP_TINT = journey_canvas.STOP
HOP_TINT = "#c0caf5"
CHART_SUB = "· {n} states, with this ticket's path drawn on the whole machine"
ET = ZoneInfo("America/New_York")
TRACKER_NAMES = {"github": "GitHub", "linear": "Linear"}
SECTION_ORDER = ("liverun", "journey", "next", "chart")
PHONE = "(max-width:699.98px)"
EXPOSURE_FIELDS = (("round", "review round", ""), ("model", "model", ""), ("effort", "effort", ""),
                   ("harness", "harness", ""), ("cli_version", "CLI version", ""),
                   ("brief_sha256", "brief sha256", ""), ("skill_paths", "skill paths", ", "), ("host", "host", ""),
                   ("factory_revision", "factory revision", ""))
EXPOSURE_SUB = "what this job runs under"
LIVE_KINDS = ("running", "queued")
RECORD_FLAGS = ("stopped", "shipped")
ROW_FROM_TICKET = ("tracker", "source", "pr")
_ENDED = ("journey", "chart")
TICKET_KINDS = {
    "running": {"started": True, "still": False, "tail": "device", "strip": {},
                "tiles": ("elapsed", "heartbeat", "round", "hops"),
                "sections": ("liverun", "journey", "next", "exposure", "chart")},
    "queued": {"started": False, "still": True, "tail": "queued", "strip": {},
               "tiles": ("waiting", "not_started", "round", "hops"), "sections": SECTION_ORDER},
    "stopped": {"started": False, "still": True, "tail": "", "line": "stopped",
                "strip": {"effect": "static", "tint": "#f7768e", "seed": 5},
                "tiles": ("stopped_for", "hops_before", "stop_round"), "lead": ("why",), "sections": _ENDED},
    "shipped": {"started": False, "still": True, "tail": "", "line": "shipped",
                "strip": {"effect": "off", "done": True, "tint": "#9ece6a"},
                "tiles": ("total", "rounds_done", "hops", "cost"), "sections": ("summary", "journey", "ended", "chart")},
    "idle": {"started": False, "still": True, "tail": "", "strip": {},
             "tiles": ("in_state", "record_round", "hops"), "sections": ("journey", "next", "chart")},
}
HOW = {"merged": ("merged", "merge"), "handed-off": ("handed off", "hand-off")}
END_PHASES = ("Stop", "Shipped")
READ_ONLY = ("This page is read-only.", "This page is read-only; it is not done from here.",
             "This page is read-only; neither is done from here.",
             "This page is read-only; none of these is done from here.")
PANELS = {
    "none": ("No ticket chosen", ""),
    "loading": ("Loading ticket", "Loading ticket {}… "),
    "missing": ("Ticket not found", "{} is not a tracked ticket. "),
}


PHONE_CSS = read_asset("css/ticket_detail_phone.css")

CSS = read_asset("css/ticket_detail.css") + f"@media {PHONE}{{\n{PHONE_CSS}}}\n"


def _who_tint(who: object) -> str:
    return TINT.get(str(who), TINT["script"])


def _detail_model(row: dict, chart: dict | None) -> dict:
    flow = row.get("flow") or {}
    ended = next((flag for flag in RECORD_FLAGS if flow.get(flag)), None)
    phases = ticket_phases(chart)
    visits = [
        {**visit, "index": i, "stop": phases.get(visit["state"]) == "Stop"}
        for i, visit in enumerate(flow.get("visits") or [])
    ]
    current = [v["index"] for v in visits if v["current"]]
    state = flow.get("state")
    seen: dict[str, int] = {}
    for visit in visits:
        seen[visit["state"]] = seen.get(visit["state"], 0) + 1

    def kind(to: str) -> str:
        if phases.get(to) == "Stop":
            return "stop"
        return "human" if to in WAITING else "normal"

    edges = [e for e in (chart or {}).get("edges", []) if state is not None and e["from"] == state]
    nexts = sorted(
        ({"to": e["to"], "label": e["label"], "kind": kind(e["to"]), "count": seen.get(e["to"], 0)} for e in edges),
        key=lambda n: NEXT_ORDER[n["kind"]],
    )
    end = flow[ended] if ended else None
    ways_out = [
        {"to": w["to"], "label": w["label"], "end": phases.get(w["to"]) in END_PHASES}
        for w in (end or {}).get("ways_out", [])
    ]
    return {
        "visits": visits,
        "selected": current[0] if current else len(visits) - 1,
        "state": state,
        "terminal": bool(flow.get("terminal")),
        "next": nexts,
        "ways_out": ways_out,
        "hops": max(0, len(visits) - 1),
        "off_chart_count": sum(1 for v in visits if v["off_chart"]),
        "hops_to_shipped": flow.get("hops_to_shipped"),
        "all_durations_known": bool(flow.get("all_durations_known")),
        "state_age_seconds": flow.get("state_age_seconds"),
        "ended": ended,
        "end": end,
    }


def _round(row: dict) -> tuple[int, int] | None:
    parts = str(row.get("round") or "").split("/")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def _crumb(label: str, right: str | None) -> str:
    return (
        '<nav class="jd-crumb" aria-label="Breadcrumb"><a href="job.html">Tickets</a>'
        '<span aria-hidden="true">/</span>'
        f'{_span("here", label)}{"" if right is None else _span("job", right)}</nav>'
    )


def _first_start(model: dict) -> float | None:
    starts = [v["started_at"] for v in model["visits"] if v["started_at"] is not None]
    return min(starts) if starts else None


def _intake_at(model: dict) -> float | None:
    end = model["end"]
    took = end["intake_to_ship_seconds"]
    return None if end["at"] is None or took is None else end["at"] - took


def _et_clock(epoch: float) -> str:
    at = datetime.fromtimestamp(epoch, ET)
    return f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'} ET"


def _et_day(epoch: float) -> str:
    at = datetime.fromtimestamp(epoch, ET)
    return f"{at:%a %b} {at.day}, {_et_clock(epoch)}"


def _stop_age(model: dict) -> float | None:
    at = model["end"]["at"]
    return None if at is None else model["now"] - at


def _stop_facts(model: dict) -> dict:
    states = [v["state"] for v in model["visits"]]
    path = states[:-1] if states and states[-1] == model["end"]["state"] else states
    return {"hops_before": max(0, len(path) - 1), "path": path}


def _hops(n: int) -> str:
    return f"{n} hop{'' if n == 1 else 's'}"


def _how(model: dict) -> tuple[str, str]:
    return HOW[model["end"]["how"]]


def _reading(value: object, fmt) -> str:
    return "no reading" if value is None else fmt(value)


def _hero_line(row: dict, model: dict) -> str:
    parts = []
    if model["state"]:
        parts.append(f"now <b>{_e(model['state'])}</b>")
    rnd = _round(row)
    if rnd:
        parts.append(f"round {rnd[0]} of {rnd[1]}")
    if model["state"] and model["state_age_seconds"] is not None:
        parts.append(f"<span>{duration(model['state_age_seconds'])} in {_e(model['state'])}</span>")
    return f'<p class="line">{" · ".join(parts) or "state not recorded"}</p>'


def _motion(row: dict, model: dict) -> str:
    return "waiting" if model["state"] in WAITING else _px().px_state(row)


def _verb(row: dict, motion: str) -> str:
    action = str(row.get("action") or "")
    glyph = '<span class="glyph" aria-hidden="true"><span></span><span></span><span class="lit"></span><span></span></span>'
    word = _span("verb", VERBS[motion][0] + "…") if motion in _px().PX_ACTIONS else ""
    return f'<div class="jd-verb">{glyph}{word}{_span("phase", action.upper()) if action else ""}</div>'


def _started(model: dict) -> str | None:
    start = _first_start(model)
    return None if start is None else "started " + _et_clock(start)


def _ended_clock(model: dict) -> str | None:
    at = model["end"]["at"]
    return None if at is None else f"{_how(model)[0]} {_et_clock(at)}"


def _shipped_line(model: dict) -> str:
    end, (word, noun) = model["end"], _how(model)
    age = "" if end["at"] is None else f' <span>{duration(model["now"] - end["at"])} ago</span>'
    rounds, took = end["revise_rounds"], end["intake_to_ship_seconds"]
    rest = (
        "" if rounds is None else f"{rounds} revise round{'' if rounds == 1 else 's'}",
        "" if took is None else f"{duration(took)} from intake to {noun}",
    )
    return f'<p class="line"><b class="ok">{_e(word)}</b>{age}{"".join(" · " + _e(p) for p in rest if p)}</p>'


def _when(model: dict) -> str:
    start = _intake_at(model)
    parts = ("" if start is None else "intake " + _et_clock(start), _ended_clock(model) or "")
    text = " · ".join(p for p in parts if p)
    return f'<div class="jd-when"><span class="mark" aria-hidden="true"></span>{_span(None, text)}</div>'


def _stopped_clock(model: dict) -> str | None:
    at = model["end"]["at"]
    return None if at is None else f"stopped {_et_day(at)}"


def _stopped_line(model: dict) -> str:
    end, age = model["end"], _stop_age(model)
    where = f" in {_e(end['stopped_in'])}" if end.get("stopped_in") else ""
    took = "" if age is None else f" for <span>{duration(age)}</span>"
    text = " · ".join(p for p in ("no job running", _stopped_clock(model)) if p)
    return (
        f'<p class="line"><b class="bad">{_e(end["state"])}</b>{where}{took}</p>'
        f'<div class="jd-when stop"><span class="mark" aria-hidden="true"></span>{_span(None, text)}</div>'
    )


HERO_LINES = {
    "state": {"crumb": _started, "chip": lambda model: "",
              "body": lambda row, model, motion: _hero_line(row, model) + _verb(row, motion)},
    "shipped": {"crumb": _ended_clock, "chip": lambda model: _chip(_how(model)[0], "ok"),
                "body": lambda row, model, motion: _shipped_line(model) + _when(model)},
    "stopped": {"crumb": _stopped_clock, "chip": lambda model: _chip(model["end"]["state"], "bad"),
                "body": lambda row, model, motion: _stopped_line(model)},
}


def _tile(value: str, label: str, cls: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    return f'<div class="jd-tile"><b{attr}>{value}</b>{_span(None, label)}</div>'


def _heartbeat_tile(row: dict, model: dict) -> str:
    hb = row.get("heartbeat_age_seconds")
    return _tile("not started" if hb is None else f"{duration(hb)} ago", "last heartbeat", HEARTBEAT_TONE[_px().px_state(row)])


def _round_tile(row: dict, model: dict) -> str:
    rnd = _round(row)
    if not rnd:
        return _tile("no round", "revise round")
    pips = "".join(
        f'<span class="{"done" if i < rnd[0] - 1 else "now" if i == rnd[0] - 1 else ""}"></span>'
        for i in range(rnd[1])
    ).replace(' class=""', "")
    return _tile(f'{rnd[0]} / {rnd[1]}<span class="jd-pips" aria-hidden="true">{pips}</span>', "revise round")


def _age_tile(age: float | None, label: str) -> str:
    return _tile(_reading(age, duration), label)


def _rounds_done(row: dict, model: dict) -> str:
    rounds = model["end"]["revise_rounds"]
    if rounds is None:
        return _tile("no round", "revise rounds")
    pips = '<span class="done"></span>' * rounds
    return _tile(f'{rounds}<span class="jd-pips" aria-hidden="true">{pips}</span>', "revise rounds")


def _cost_tile(row: dict, model: dict) -> str:
    end = model["end"]
    if end["cost_usd"] is None:
        return ""
    return _tile(_dollars(end["cost_usd"]), f"cost, {end['jobs']} job{'' if end['jobs'] == 1 else 's'}")


def _stopped_for(row: dict, model: dict) -> str:
    age = _stop_age(model)
    return _tile("no reading", "time stopped") if age is None else _tile(duration(age), "time stopped", "bad")


def _stop_round(row: dict, model: dict) -> str:
    rnd = row["flow"]["round"]
    if rnd is None:
        return _tile("no round", "revise round")
    return _tile(str(rnd), "revise round") if rnd else _tile("none", "revise round", "soft")


def _elapsed_label(row: dict) -> str:
    return f"T+ on {row['device']}" if row.get("device") else "T+ on this device"


TILES = {
    "elapsed": lambda row, model: _tile(duration(row.get("elapsed_seconds")), _elapsed_label(row)),
    "heartbeat": _heartbeat_tile,
    "waiting": lambda row, model: _tile(duration(row.get("age_seconds")), "waiting"),
    "not_started": lambda row, model: _tile("not started", "last heartbeat"),
    "round": _round_tile,
    "record_round": lambda row, model: _tile("no round" if row["flow"]["round"] is None else str(row["flow"]["round"]), "revise round"),
    "in_state": lambda row, model: _age_tile(model["state_age_seconds"], f"in {model['state']}"),
    "stopped_for": _stopped_for,
    "hops_before": lambda row, model: _tile(_hops(_stop_facts(model)["hops_before"]), "before it stopped"),
    "stop_round": _stop_round,
    "hops": lambda row, model: _tile(f"{model['hops']} hops", f"{model['off_chart_count']} off-chart"),
    "total": lambda row, model: _age_tile(model["end"]["intake_to_ship_seconds"], f"total, intake to {_how(model)[1]}"),
    "rounds_done": _rounds_done,
    "cost": _cost_tile,
}


def _trouble(row: dict) -> str:
    return (
        _state_chip(row)
        + (_chip("OVERDUE", "bad") if row.get("overdue") else "")
        + (_chip(f"retried {row['retries']}x") if row.get("retries") else "")
    )


def _hero(row: dict, model: dict, motion: str, strip: str, title: str, tiles: tuple[str, ...], line: str = "state") -> str:
    cells = "".join(TILES[name](row, model) for name in tiles)
    return (
        f'<section class="jd-hero">{strip}<div class="body"><div class="who"><div class="title">{title}</div>'
        f'{HERO_LINES[line]["body"](row, model, motion)}</div><div class="jd-tiles">{cells}</div></div></section>'
    )


_NUMBER = re.compile(r"[0-9]+")


def _tracker_text(row: dict) -> str:
    source, pr = str(row.get("source")), str(row.get("pr"))
    tracker = row.get("tracker")
    if tracker == "linear":
        first = str(row["ticket_id"])
    else:
        first = f"issue {source}" if source.startswith("#") and _NUMBER.fullmatch(source[1:]) else ""
    refs = " · ".join(p for p in (first, f"PR #{pr}" if _NUMBER.fullmatch(pr) else "") if p)
    return " ".join(p for p in (TRACKER_NAMES.get(tracker), refs) if p)


TRACKER_TAILS = {
    "device": lambda row: f"on {row['device']}" if row.get("device") else "",
    "queued": lambda row: f"queued for {row.get('recipient')} · {duration(row.get('age_seconds'))}",
    "": lambda row: "",
}


def _ticket_tracker(row: dict, tail: str) -> str:
    text = " · ".join(p for p in (_tracker_text(row), TRACKER_TAILS[tail](row)) if p)
    return _span("tracker", text) if text else ""


def _ok_chips(ticket: dict | None) -> str:
    return "".join(_chip(f"ok: {outcome}", "muted ok") for outcome in (ticket or {}).get("ok_outcomes", ()))


def _ticket_hero(found: dict, model: dict, spec: dict) -> str:
    row = found["row"]
    motion = _motion(row, model)
    strip = _px().px_strip(f"ticket-hero-{found['address']}", **{
        "state": motion, "bot": row.get("recipient"), "still": spec["still"], "cell": 4, "height": 40, "seed": 2,
        **spec["strip"]})
    line = spec.get("line", "state")
    title = (
        f'<h1>{_e(_tile_label(row))}</h1>'
        + (_bot(row["recipient"]) if "recipient" in row else "")
        + HERO_LINES[line]["chip"](model)
        + _ticket_tracker(row, spec["tail"])
        + _trouble(row)
        + _ok_chips(found["ticket"])
    )
    return _hero(row, model, motion, strip, title, spec["tiles"], line)


def _now_dur(model: dict) -> str:
    age = model["state_age_seconds"]
    return "in progress" if age is None else f"{duration(age)} so far"


def _shipped_dur(visit: dict) -> str:
    return "—" if visit["started_at"] is None else f"at {_et_clock(visit['started_at'])}"


def _visit_tag(visit: dict, model: dict) -> str:
    if not visit["current"]:
        return ""
    return "shipped" if model["ended"] == "shipped" else "now"


def _visit_dur(visit: dict, model: dict) -> str:
    if visit["current"] and model["ended"] == "shipped":
        return _shipped_dur(visit)
    if visit["current"]:
        return _now_dur(model)
    return "—" if visit["duration_seconds"] is None else duration(visit["duration_seconds"])


def _step_name(visit: dict, model: dict) -> str:
    if visit["current"]:
        took = _now_dur(model)
    else:
        took = "time unknown" if visit["duration_seconds"] is None else duration(visit["duration_seconds"])
    return f"Step {visit['index'] + 1} of {len(model['visits'])}: {visit['state']}, {visit['who']}, {took}"


def _reason(visit: dict) -> str:
    if visit["reason"] is not None:
        return visit["reason"]
    return FIRST_REASON if visit["index"] == 0 else ""


def _runlog_row(key: str, visit: dict, model: dict) -> str:
    tint = _who_tint(visit["who"])
    tag = _visit_tag(visit, model)
    cls = " ".join(c for c in ("visit", tag, "stop" if visit["stop"] else "") if c)
    mark_tint = STOP_TINT if visit["stop"] else END_TINT.get(tag, tint)
    name = visit["state"] + (f" · {tag}" if tag else "")
    num = f"{visit['index'] + 1:02d}"
    on = visit["index"] == model["selected"]
    return (
        f'<li><button class="{cls}" type="button" data-visit="{visit["index"]}" data-state="{_e(visit["state"])}" aria-pressed="{"true" if on else "false"}"'
        f' style="--m:{tint}">'
        f'{_span("num", num)}'
        f'<span class="mark{" off" if visit["off_chart"] else ""}" aria-hidden="true" style="--m:{mark_tint}"></span>'
        f'<span class="main"><span class="top">{_span("name", name)}{_span("who", visit["who"])}'
        f'{_span("dur", _visit_dur(visit, model))}'
        + (_span("offchip", "off-chart") if visit["off_chart"] else "")
        + f'</span>{_span("note", _reason(visit))}</span></button>'
        + _runlog_jobs(key, visit)
        + "</li>"
    )


def _runlog_job(job: dict) -> str:
    found = job["exposure"]
    number = "" if found["round"] == UNKNOWN else _span("rnd", f'round {found["round"]}')
    waited = job.get("suite_wait_seconds")
    wait = "" if waited is None else _span("wait", f"suite wait {waited}s")
    return (f'<span class="exp-job">{_span("job", job["job"])}{_span("act", job["action"])}{number}'
            f'{_span("model", model_effort(found) or "unknown")}{wait}</span>')


def _runlog_jobs(key: str, visit: dict) -> str:
    jobs = visit.get("jobs") or []
    if not jobs:
        return ""
    tables = "".join(
        f'<table class="exp"><caption>{_e(job["job"])} · {_e(job["action"])}</caption>'
        f'<tbody>{_exposure_rows(job["exposure"])}</tbody></table>'
        for job in jobs
    )
    line = "".join(_runlog_job(job) for job in jobs)
    label = f'{len(jobs)} job{"" if len(jobs) == 1 else "s"} · metadata'
    return f'<div class="exp-jobs">{line}</div><details class="exp-more" data-key="runlog-jobs:{_e(key)}:{visit["index"]}"><summary>{label}</summary>{tables}</details>'


def _runlog(key: str, model: dict) -> str:
    rows = "".join(_runlog_row(key, v, model) for v in model["visits"])
    count = f"{len(model['visits'])} visits · oldest first"
    body = f'<ol class="runlog">{rows}</ol>' if rows else '<p class="empty">no visits recorded</p>'
    return (
        f'<details class="jd-panel jd-runlog" data-sec="runlog" data-key="runlog:{_e(key)}">'
        f'<summary><h2>{_span("t", "Run log")}{_span("count", count)}</h2></summary>{body}'
        '<p class="jd-foot">Pick a step here or on the chart; both follow.</p></details>'
    )


LIVE_FIELDS = ("current_task", "last_finished", "plan", "subagents")


def live_mode(telemetry: dict | None) -> str:
    """none: no telemetry; counters: #100's counters only; live: #148's fields too."""
    if telemetry is None:
        return "none"
    return "live" if any(telemetry.get(name) is not None for name in LIVE_FIELDS) else "counters"


def compact(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 10000:
        tenths = (n * 10 + 500) // 1000
        return f"{tenths // 10}.{tenths % 10}k"
    if n < 1000000:
        return f"{(n + 500) // 1000}k"
    tenths = (n * 10 + 500000) // 1000000
    return f"{tenths // 10}.{tenths % 10}M"


def _dollars(cost: float) -> str:
    cents = math.floor(cost * 100 + 0.5)
    return f"${cents // 100}.{cents % 100:02d}"


def _lr_tile_row(tiles: tuple[tuple[str, str], ...]) -> str:
    body = "".join(f'<div class="lr-tile"><b>{_e(v)}</b>{_span(None, label)}</div>' for v, label in tiles)
    return f'<div class="lr-tiles">{body}</div>'


def _lr_tiles(t: dict) -> str:
    known = t.get("tokens_known", True)
    tokens_in, cached = t["tokens_in"], t["tokens_cached"]
    in_label = "tokens in"
    if known and tokens_in:
        in_label += f" · {(200 * cached + tokens_in) // (2 * tokens_in)}% cached"
    return _lr_tile_row((
        (compact(tokens_in) if known else NO_DATA, in_label),
        (compact(t["tokens_out"]) if known else NO_DATA, "tokens out"),
        (NO_DATA if t["cost_usd"] is None else _dollars(t["cost_usd"]), "cost so far"),
        (f"{t['files_touched']} files · {t['commands']} commands", "touched and run"),
    ))


def _lr_head(row: dict, t: dict, moving: bool) -> str:
    finished = bool(t.get("finished"))
    task, last = t.get("current_task"), t.get("last_finished")
    effect = "spin3" if moving and task and not finished else "off"
    strip = _px().px_strip(f"live-head-{row['job']}", effect=effect, tint="bot", bot=row.get("recipient"),
                           seed=71, still=not moving, cell=4, done=finished, height=12)
    if finished:
        label, text, cls = "finished", last or "nothing recorded", "lr-task ok" if last else "lr-task muted"
    else:
        label, text, cls = "working on", task or "no task in progress", "lr-task" if task else "lr-task muted"
    return (
        f'<div class="lr-head"><span class="lr-spin" aria-hidden="true">{strip}</span>'
        f'{_span("lr-label", label)}{_span(cls, text)}'
        f'<span class="lr-last">last finished: {_span(None, last or "nothing yet")}</span></div>'
    )


def _lr_plan(row: dict, plan: list[dict], moving: bool) -> str:
    head = '<div class="lr-colhead"><span class="lr-label">Its plan</span>'
    if not plan:
        return f'<div class="lr-col">{head}</div><p class="empty">no plan yet</p></div>'
    done = sum(1 for item in plan if item["status"] == "done")
    strip = _px().px_strip(f"live-plan-{row['job']}", effect="fill", tint="bot", bot=row.get("recipient"),
                           progress=done / len(plan), seed=72, still=not moving, cell=4, done=done == len(plan), height=8)
    items = "".join(
        f'<li class="{_PLAN_CLS[item["status"]]}"><span class="lr-mark" aria-hidden="true"></span>'
        f'{_span("lr-text", item["text"])}{_span("lr-tag", _PLAN_TAG[item["status"]])}</li>'
        for item in plan
    )
    return (
        f'<div class="lr-col">{head}{_span("lr-count", f"{done} of {len(plan)} done")}</div>'
        f'{strip}<ol class="lr-plan">{items}</ol></div>'
    )


_PLAN_CLS = {"done": "done", "in_progress": "now", "pending": "pending"}
_PLAN_TAG = {"done": "done", "in_progress": "now", "pending": ""}


def _lr_agents(row: dict, agents: list[dict] | None, moving: bool) -> str:
    head = '<div class="lr-colhead"><span class="lr-label">Sub-agents</span>'
    if agents is None:
        return f'<div class="lr-col">{head}</div><p class="empty">no sub-agent data</p></div>'
    running = sum(1 for a in agents if a["status"] == "running")
    count = _span("lr-count", f"{len(agents)} spawned · {running} running")
    if not agents:
        return f'<div class="lr-col">{head}{count}</div><div class="lr-alone">working alone so far</div></div>'
    cards = []
    for i, agent in enumerate(agents):
        status = agent["status"]
        effect = {"running": "scan" if moving else "off", "finished": "off", "failed": "static"}[status]
        strip = _px().px_strip(f"live-agent-{row['job']}-{i}", effect=effect,
                               tint="#f7768e" if status == "failed" else "bot", bot=row.get("recipient"),
                               seed=80 + i, still=not moving, cell=4, done=status == "finished", height=8)
        cards.append(
            f'<div class="lr-agent">{strip}<div class="lr-agent-body">{_span("lr-kind", agent["kind"])}'
            f'{_span("lr-desc", agent["description"])}'
            f'{_span("lr-status " + status, status)}</div></div>'
        )
    return f'<div class="lr-col">{head}{count}</div>{"".join(cards)}</div>'


def _lr_moving(row: dict) -> bool:
    return _px().px_state(row) in _px().PX_ACTIONS


def live_run(row: dict, queued: bool = False) -> str:
    """The Live run panel's body (RunTape.dc.html)."""
    if queued:
        return '<p class="empty">not started yet</p>'
    t = row.get("telemetry")
    mode = live_mode(t)
    if mode == "none":
        return '<p class="empty">no telemetry</p>'
    tint = row.get("recipient") if row.get("recipient") in _px().PX_BOT_TINTS else None
    style = f' style="--lr:var(--{tint})"' if tint else ""
    if mode == "counters":
        return f'<div class="lr"{style}>{_lr_tiles(t)}</div>'
    moving = _lr_moving(row)
    plan = "" if t.get("plan") is None else _lr_plan(row, t["plan"], moving)
    return (
        f'<div class="lr"{style}>{_lr_head(row, t, moving)}'
        f'<div class="lr-cols">{plan}{_lr_agents(row, t.get("subagents"), moving)}</div>'
        f"{_lr_tiles(t)}</div>"
    )


def _liverun(row: dict, started: bool) -> str:
    sub = f"{row.get('recipient')} · job {row['job']}"
    return (
        f'<section class="jd-panel" id="liverun-{_e(anchor(row["job"]))}" data-sec="liverun">'
        f'<h2>{_span("t", "Live run")}{_span("count", sub)}</h2>{live_run(row, not started)}</section>'
    )


def time_by_who(model: dict) -> list[tuple[str, int, str]]:
    end = model["end"]["at"]
    seconds: dict[str, int] = {}
    states: dict[str, dict[str, int]] = {}
    for visit in model["visits"]:
        took, start = visit["duration_seconds"], visit["started_at"]
        if took is not None and end is not None and start is not None:
            took = int(min(took, end - start)) if start < end else None
        if took is None:
            continue
        who = str(visit["who"])
        seconds[who] = seconds.get(who, 0) + took
        seen = states.setdefault(who, {})
        seen[visit["state"]] = seen.get(visit["state"], 0) + 1
    return [
        (who, seconds[who], ", ".join(state if n == 1 else f"{n}× {state}" for state, n in states[who].items()))
        for who in sorted(seconds, key=lambda who: -seconds[who])
    ]


def _who_key(who: str, seconds: int, states: str) -> str:
    tint = _who_tint(who)
    return (
        f'<span><i style="background:{tint}"></i><b style="color:{tint}">{_e(who)}</b>'
        f'{_e(f" {duration(seconds)} · {states}")}</span>'
    )


def _time_by_who(model: dict) -> str:
    rows = time_by_who(model)
    total = _reading(sum(s for _, s, _ in rows) if rows else None, duration)
    body = f'<div class="lr-colhead">{_span("lr-label", "Time by who")}{_span("lr-count", total)}</div>'
    if rows:
        bar = "".join(f'<span style="flex:{s} 1 0;background:{_who_tint(who)}"></span>' for who, s, _ in rows)
        key = "".join(_who_key(*row) for row in rows)
        body += f'<div class="sum-bar" aria-hidden="true">{bar}</div><div class="sum-key">{key}</div>'
    return f'<div class="sum-who">{body}</div>'


def _summary(model: dict) -> str:
    end, noun = model["end"], _how(model)[1]
    start, at = _intake_at(model), end["at"]
    run = " to ".join(p for p in ("" if start is None else f"intake {_et_clock(start)}",
                                  "" if at is None else f"{noun} {_et_clock(at)}") if p)
    tiles = _lr_tile_row((
        (_reading(end["jobs"], str), "jobs"),
        (_reading(end["tokens_in"], compact), "tokens in"),
        (_reading(end["tokens_out"], compact), "tokens out"),
        (_reading(end["cost_usd"], _dollars), "cost"),
    ))
    return (
        f'<section class="jd-panel jd-sum" data-sec="summary"><h2>{_span("t", "Run summary")}'
        f'{_span("count", " · ".join(p for p in ("the whole run", run) if p))}</h2>'
        f'<div class="lr">{tiles}{_time_by_who(model)}'
        '<p class="sum-foot">Totals over every job this ticket ran. Nothing is running now.</p></div></section>'
    )


def _ended(model: dict) -> str:
    at, word = model["end"]["at"], _how(model)[0]
    when = _span("at", "no reading") if at is None else (
        _span("at", f"{word} at {_et_clock(at)}") + _span("ago", f"{duration(model['now'] - at)} ago"))
    return (
        f'<section class="jd-panel jd-end" data-sec="ended"><h2>{_span("t", "Ended")}{_span("count", "end state · nothing comes next")}</h2>'
        f'<div class="jd-ended"><span class="mark" aria-hidden="true"></span>{_span("how", word)}{when}</div></section>'
    )


def _why(model: dict) -> str:
    end, facts, age = model["end"], _stop_facts(model), _stop_age(model)
    where = f"in {end['stopped_in']}" if end.get("stopped_in") else ""
    sub = " · ".join(p for p in ("" if age is None else f"stopped {duration(age)} ago", where) if p)
    path = f"{_hops(facts['hops_before'])} in ({' → '.join(facts['path'])})" if facts["path"] else ""
    rows = (
        ("when", "no reading" if age is None else f"{_et_day(end['at'])} · {duration(age)} ago"),
        ("where", ", ".join(p for p in (where, path) if p) or "no reading"),
        ("job", "none running"),
    )
    table = "".join(f'<tr><th scope="row">{name}</th><td>{_e(text)}</td></tr>' for name, text in rows)
    ways = model["ways_out"]
    cards = "".join(
        f'<div class="card{" end" if w["end"] else ""}"><div class="head">{_span("to", w["to"])}'
        + (_span("tag", "end state") if w["end"] else "")
        + f'</div>{_span("why", w["label"])}</div>'
        for w in ways
    )
    out = f'<div class="jd-next">{cards}</div>' if ways else '<p class="empty">no way out on the chart</p>'
    return (
        f'<section class="jd-panel jd-why"><h2>{_span("t", "Why it stopped")}{_span("count", sub)}</h2>'
        f'<div class="why-body"><div class="why-col">{_span("label", "Reason")}'
        f'<div class="why-reason">{_e(end.get("reason") or "no reason recorded")}</div>'
        f'<table class="why-facts"><tbody>{table}</tbody></table></div>'
        f'<div class="why-col">{_span("label", f"Ways out · {len(ways)}, from the chart")}{out}'
        f'<p class="why-ro">{READ_ONLY[min(len(ways), len(READ_ONLY) - 1)]}</p></div></div></section>'
    )


def _exposed(value: str | list[str], joiner: str) -> str:
    return value if isinstance(value, str) else joiner.join(value) or "none"


def _exposure_rows(found: dict) -> str:
    return "".join(f'<tr><th scope="row">{label}</th><td>{_e(_exposed(found[key], joiner))}</td></tr>'
                   for key, label, joiner in EXPOSURE_FIELDS)


def _exposure(row: dict) -> str:
    return (
        f'<section class="jd-panel jd-exp" data-sec="exposure"><h2>{_span("t", "Exposure")}{_span("count", EXPOSURE_SUB)}</h2>'
        f'<table class="why-facts"><tbody>{_exposure_rows(row["exposure"])}</tbody></table></section>'
    )


def _journey_label(model: dict) -> str:
    path = " → ".join(v["state"] for v in model["visits"]) or "no visits"
    if model["ended"]:
        return f"Journey: {path}; ended: {_end_text(model)}"
    return f"Journey: {path}; now {model['state'] or 'unknown'}"


END_TINT = {"shipped": "#9ece6a", "stopped": STOP_TINT}


def _end_text(model: dict) -> str:
    end, visits = model["end"], model["visits"]
    if model["ended"] == "shipped":
        parts = [f"shipped · {model['hops']} hop{'' if model['hops'] == 1 else 's'}"]
        if end.get("intake_to_ship_seconds") is not None:
            parts.append(duration(end["intake_to_ship_seconds"]))
        return " · ".join(parts)
    where = end.get("stopped_in")
    if not where:
        where = visits[-2]["state"] if len(visits) > 1 and visits[-1]["stop"] else visits[-1]["state"] if visits else end["state"]
    age = model["state_age_seconds"]
    return f"stopped in {where}" + ("" if age is None else f" · {duration(age)} ago")


_LEGEND_WHO = ("script", "claude", "codex", "cursor", "grok")


def _legend_who(model: dict) -> list[str]:
    seen = {who if who in TINT else "script" for who in (str(v["who"]) for v in model["visits"])}
    return [who for who in _LEGEND_WHO if who in seen]


def _caption_dur(visit: dict, model: dict) -> str:
    if visit["current"] and model["ended"] == "shipped":
        return _shipped_dur(visit)
    if visit["current"] and model["ended"] == "stopped":
        return "—" if model["state_age_seconds"] is None else f"for {duration(model['state_age_seconds'])}"
    if visit["current"]:
        return _now_dur(model)
    return "—" if visit["duration_seconds"] is None else f"took {duration(visit['duration_seconds'])}"


def _caption(model: dict) -> str:
    visits = model["visits"]
    if not visits:
        return '<div class="jd-cap" aria-live="polite"></div>'
    visit = visits[model["selected"]]
    index = f"{visit['index'] + 1:02d} of {len(visits)}"
    tag = (model["ended"] or "now") if visit["current"] else ""
    lead = (
        f'<span class="end" style="--m:{END_TINT[model["ended"]]}">{_e(_end_text(model))}</span>' if model["ended"] else ""
    )
    return (
        f'<div class="jd-cap{" " + tag if tag else ""}" aria-live="polite">{lead}'
        f'{_span("idx", index)}'
        f'{_span("st", visit["state"] + (" · " + tag if tag else ""))}'
        f'<span class="who" style="--m:{_who_tint(visit["who"])}">{_e(visit["who"])}</span>'
        f'{_span("dur", _caption_dur(visit, model))}{_span("note", _reason(visit))}</div>'
    )


REPLAY = '<button type="button" data-replay="1">replay</button>'
JOURNEY_SUB = "this ticket through time · pick a step for its reason"
JOURNEY_TAP = "tap a step for its reason"


def _journey(row: dict, model: dict) -> str:
    hits = "".join(
        f'<li><button type="button" data-visit="{v["index"]}" data-state="{_e(v["state"])}" '
        f'aria-label="{_e(_step_name(v, model))}" aria-pressed="{"true" if v["index"] == model["selected"] else "false"}">'
        f'</button>{_span("step", _step_name(v, model))}</li>'
        for v in model["visits"]
    )
    scale = (
        '<button type="button" data-scale="scale" aria-pressed="false">to scale</button>'
        if model["all_durations_known"]
        else ""
    )
    legend = "".join(f'<span class="key"><i style="--m:{TINT[w]}"></i>{w}</span>' for w in _legend_who(model))
    legend += f'<span class="key stop"><i style="--m:{STOP_TINT}"></i>stopped</span>'
    legend += f'<span class="key hop"><i class="off" style="--m:{HOP_TINT}"></i>off-chart hop</span>'
    endnote = (
        f'<span class="endnote" style="--m:{END_TINT[model["ended"]]}">{_e(_end_text(model))}</span>' if model["ended"] else ""
    )
    return (
        f'<section class="jd-panel" data-sec="journey"><h2>{_span("t", "Journey")}'
        f'<span class="count">{_span("desk", JOURNEY_SUB)}{_span("tap", JOURNEY_TAP)}</span></h2>'
        f'<div class="jd-box"><canvas class="jd-journey" width="1000" height="336" role="img" '
        f'aria-label="{_e(_journey_label(model))}"></canvas><ol class="journey">{hits}</ol></div>{_caption(model)}'
        '<div class="jd-ctl"><div class="axis" role="group" aria-label="Time axis">'
        '<button type="button" data-scale="even" aria-pressed="true">even steps</button>'
        f'{scale}</div>{"" if model["ended"] == "stopped" else REPLAY}{endnote}<span class="gap"></span>{legend}</div></section>'
    )


def _next_tag(item: dict, first: bool) -> str:
    if item["kind"] == "stop":
        times = {0: "not yet", 1: "once"}.get(item["count"], f"{item['count']} times")
        return f"stop state · been here {times}" if item["count"] else "stop state · not yet"
    if item["kind"] == "human":
        return "needs a human"
    return "the normal way on" if first else ""


def _next(model: dict) -> str:
    if model["terminal"] or not model["state"]:
        sub = "no way out"
        body = '<p class="empty">no way out: this ticket is finished</p>' if model["terminal"] else '<p class="empty">state not recorded</p>'
    else:
        sub = f"{len(model['next'])} ways out of {model['state']}"
        cards = []
        for i, item in enumerate(model["next"]):
            tag = _next_tag(item, i == 0)
            cards.append(
                f'<div class="card {item["kind"]}"><div class="head">{_span("to", item["to"])}'
                + (_span("tag", tag) if tag else "")
                + f'</div>{_span("why", item["label"])}</div>'
            )
        body = f'<div class="jd-next">{"".join(cards)}</div>'
    return f'<section class="jd-panel" data-sec="next"><h2>{_span("t", "Can come next")}{_span("count", sub)}</h2>{body}</section>'


def _chart(key: str, model: dict, chart: dict | None) -> str:
    if chart is None:
        return '<details class="jd-chart" data-sec="chart"><summary>Full state chart</summary><p class="empty">statechart unavailable</p></details>'
    n = len(chart["states"])
    grid = flowmap_layout(chart)["grid"]
    legend = "".join(f'<span><i class="line" style="background:{TINT[w]}"></i>path taken · {w}</span>' for w in _legend_who(model))
    legend += '<span><i class="line" style="background:#5386a6"></i>visited</span>'
    legend += f'<span><i class="off" style="--m:{TINT["script"]}"></i>off-chart jump</span>'
    for cls, bg, text in CHART_KEYS[model["ended"] or "live"]:
        tint = f' style="background:{bg}"' if bg else ""
        legend += f'<span><i class="{cls}"{tint}></i>{text}</span>'
    return (
        f'<details class="jd-chart" data-sec="chart" data-key="full-chart:{_e(key)}"><summary>Full state chart '
        f'{_span("count", CHART_SUB.format(n=n))}</summary>'
        f'<div class="jd-box"><canvas class="jd-flowmap" width="1000" height="{grid["h"] * grid["cell"]}" role="img" '
        f'aria-label="{_e("Full state chart; path: " + " → ".join(v["state"] for v in model["visits"]))}"></canvas></div>'
        f'<div class="jd-legend">{legend}</div></details>'
    )


CHART_KEYS = {
    "live": (("now", None, "now"), ("next", None, "can come next")),
    "shipped": (("now", None, "ended here"),),
    "stopped": (("line", STOP_TINT, "fall to a stop state"), ("now", None, "stopped here")),
}


SECTIONS = {
    "liverun": lambda part: _liverun(part["row"], part["started"]),
    "journey": lambda part: _journey(part["row"], part["model"]),
    "next": lambda part: _next(part["model"]),
    "chart": lambda part: _chart(part["key"], part["model"], part["chart"]),
    "summary": lambda part: _summary(part["model"]),
    "ended": lambda part: _ended(part["model"]),
    "why": lambda part: _why(part["model"]),
    "exposure": lambda part: _exposure(part["row"]),
}


def _cols(part: dict, sections: tuple[str, ...]) -> str:
    after = "next" if "next" in sections else "journey"
    log = _runlog(part["key"], part["model"])
    stack = "".join(SECTIONS[name](part) + (log if name == after else "") for name in sections)
    return f'<div class="jd-cols">{stack}</div>'


_PICK = {
    "running": lambda rows: min(rows, key=lambda row: row.get("elapsed_seconds") or 0),
    "queued": lambda rows: rows[0],
}


def _ticket_of(snapshot: dict, row: dict, record: str | None = None) -> dict | None:
    record = record or row.get("record")
    if record is not None:
        return next((t for t in snapshot["tickets"] if t.get("record") == record), None)
    key = _thread_key(row)
    return next((t for t in snapshot["tickets"] if _thread_key(t) == key), None)


def record_row(record: dict, ticket: dict | None) -> dict:
    refs = {k: ticket[k] for k in ROW_FROM_TICKET if k in ticket} if ticket else {}
    return {"repo": record["repo"], "ticket_id": record["ticket_id"], "flow": record, **refs}


def resolve_ticket(snapshot: dict, address: str, records: dict | None = None) -> dict | None:
    repo, _, ticket_id = address.partition("#")

    def record_key(row: dict) -> bool:
        return row.get("record") == address

    def exact(row: dict) -> bool:
        return ticket_address(row) == address

    def any_case(row: dict) -> bool:
        return str(row["repo"]).lower() == repo.lower() and str(row["ticket_id"]) == ticket_id

    for rule in (record_key, exact, any_case):
        for kind in LIVE_KINDS:
            rows = [row for row in snapshot[kind] if rule(row)]
            if rows:
                row = _PICK[kind](rows)
                shown = ticket_address(row) if rule is any_case else address
                return {"kind": kind, "address": shown, "row": row, "ticket": _ticket_of(snapshot, row)}
    if not records:
        return None
    key = address if address in records else next((k for k in sorted(records) if any_case(records[k])), None)
    if key is None:
        return None
    record = records[key]
    ticket = _ticket_of(snapshot, record, key)
    kind = next((flag for flag in RECORD_FLAGS if record.get(flag)), "idle")
    return {"kind": kind, "address": key, "row": record_row(record, ticket), "ticket": ticket}


def _ticket_article(found: dict, snapshot: dict) -> str:
    row, address = found["row"], found["address"]
    spec = TICKET_KINDS[found["kind"]]
    chart = snapshot.get("statechart")
    model = {**_detail_model(row, chart), "now": snapshot.get("generated_at_epoch")}
    part = {"row": row, "model": model, "chart": chart, "key": address, "started": spec["started"]}
    return (
        f'<article class="jobpage ticketpage" data-ticket="{_e(address)}">'
        + _crumb(_tile_label(row), HERO_LINES[spec.get("line", "state")]["crumb"](model))
        + _ticket_hero(found, model, spec)
        + "".join(SECTIONS[name](part) for name in spec.get("lead", ()))
        + _cols(part, spec["sections"])
        + "</article>"
    )


def _no_ticket(address: str | None, loaded: bool) -> str:
    title, lead = PANELS["none" if address is None else "missing" if loaded else "loading"]
    return (
        f'<section class="panel"><h1><span>{title}</span></h1>'
        f'<p class="empty">{lead.format(_e(address))}Pick a ticket on the <a href="job.html">Tickets board</a>.</p></section>'
    )


def _records(snapshot: dict) -> dict | None:
    from lib.dashboard import RECORDS

    return snapshot.get(RECORDS)


def ticket_view(snapshot: dict, address: str | None, open_ci: bool = False) -> str:
    from lib.dashboard_pages.ci import ci_extras

    records = _records(snapshot)
    found = None if address is None else resolve_ticket(snapshot, address, records)
    if not found:
        return f'<div id="ticketview">{_no_ticket(address, records is not None)}</div>'
    cockpit = ticket_cockpit_html(found.get("ticket"))
    extras = ci_extras(snapshot, found["address"], found["row"], open_ci)
    return f'<div id="ticketview">{_ticket_article(found, snapshot)}{cockpit}{extras}</div>'


def _ticketview(snapshot: dict) -> str:
    return ticket_view(snapshot, None)


_SCRIPT = read_asset("js/ticket_detail.js")
_DETAIL = {"VERBS": VERBS, "HB": HEARTBEAT_TONE, "CHARTSUB": CHART_SUB, "TRACKERS": TRACKER_NAMES,
           "KINDS": TICKET_KINDS, "LIVEKINDS": LIVE_KINDS,
           "RECORDFLAGS": RECORD_FLAGS, "ROWFROMTICKET": ROW_FROM_TICKET, "PANELS": PANELS,
           "CHARTKEYS": CHART_KEYS, "ENDTINT": END_TINT, "HOW": HOW, "ENDPHASES": END_PHASES, "READONLY": READ_ONLY,
           "PHONE": PHONE, "JOURNEYSUB": JOURNEY_SUB, "JOURNEYTAP": JOURNEY_TAP,
           "EXPOSURE": EXPOSURE_FIELDS, "EXPOSURESUB": EXPOSURE_SUB}
_SCRIPT = re.sub(r"__([A-Z]+)__", lambda m: json.dumps(_DETAIL[m.group(1)]), _SCRIPT)
SCRIPT = journey_canvas.SCRIPT + FLOWMAP_SCRIPT + _SCRIPT
