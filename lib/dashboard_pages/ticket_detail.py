"""job.html#job-<id> and ticket.html#t=<repo>#<ticket>: the ticket detail page (Job.dc.html,
TicketR2.dc.html). Breadcrumb, hero, Run log, Live run slot, Journey, Can come next and the
collapsed Full state chart. The Run log and the Journey render from one visit list and share
one selection."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from lib.dashboard_flow import FLOWMAP_SCRIPT, flowmap_layout, ticket_phases
from lib.dashboard_telemetry import NO_DATA
from lib.dashboard_pages.common import (
    anchor, _bot, _chip, duration, _e, github_links, _job_kind, _job_label, _span, _state_chip,
    _thread_key, ticket_address, _tile_label,
)
from lib.dashboard_pages import journey_canvas
from lib.dashboard_pages.journey_canvas import TINT
from lib.dashboard_pages.lanes import TILE_STATE
from lib.dashboard_pages.tickets import WAITING, _active_jobs, _px


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
JOB_TILES = {"running": ("elapsed_here", "heartbeat", "round", "hops"), "queued": ("waiting", "not_started", "round", "hops")}
LIVE_KINDS = ("running", "queued")
RECORD_FLAGS = ("stopped", "shipped")
ROW_FROM_TICKET = ("tracker", "source", "pr")
_ENDED = ("journey", "chart")
TICKET_KINDS = {
    "running": {"started": True, "still": False, "tail": "device", "strip": {},
                "tiles": ("elapsed", "heartbeat", "round", "hops"), "sections": SECTION_ORDER},
    "queued": {"started": False, "still": True, "tail": "queued", "strip": {},
               "tiles": ("waiting", "not_started", "round", "hops"), "sections": SECTION_ORDER},
    "stopped": {"started": False, "still": True, "tail": "", "strip": {"state": "stuck"},
                "tiles": ("stopped_for", "record_round", "hops"), "sections": _ENDED},
    "shipped": {"started": False, "still": True, "tail": "", "strip": {"effect": "off", "done": True},
                "tiles": ("in_state", "record_round", "hops"), "sections": _ENDED},
    "idle": {"started": False, "still": True, "tail": "", "strip": {},
             "tiles": ("in_state", "record_round", "hops"), "sections": ("journey", "next", "chart")},
}
PANELS = {
    "none": ("No ticket chosen", ""),
    "loading": ("Loading ticket", "Loading ticket {}… "),
    "missing": ("Ticket not found", "{} is not a tracked ticket. "),
}


CSS = """\
#jobview{display:grid;gap:16px}
#ticketview{grid-column:1/-1;min-width:0}
.jobpage{display:flex;flex-direction:column;gap:16px;min-width:0}
.jobpage:target{outline:1px solid var(--brand)}
#jobview:has(.jobpage:target) .jobpage:not(:target){display:none}
.jd-crumb{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;font:12px/1.5 var(--mono);color:var(--muted)}
.jd-crumb a{color:var(--text)}.jd-crumb .here{color:var(--text)}.jd-crumb .job{margin-left:auto}
.jd-hero{min-width:0;background:var(--panel);box-shadow:var(--elev)}
.jd-hero .body{display:flex;flex-wrap:wrap;align-items:flex-start;gap:16px 32px;padding:16px}
.jd-hero .who{flex:2 1 420px;min-width:0;display:flex;flex-direction:column;gap:10px}
.jd-hero .title{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px}
.jd-hero h1{margin:0;font:600 22px/1.3 var(--mono);letter-spacing:-.02em;color:var(--text)}
.jd-hero .kind{padding:0 4px;border:1px solid var(--line-hi);color:var(--muted);font:10px/1.6 var(--mono)}
.jd-hero .device,.jd-hero .tracker{font:12px var(--mono);color:var(--muted)}
.jd-hero .line{margin:0;font-size:15px;line-height:1.5;color:var(--text-2)}
.jd-hero .line b{font-family:var(--mono);font-weight:600;color:var(--accent)}
.jd-gh{display:flex;flex-direction:column;gap:8px;font:12px/1.5 var(--mono);color:var(--text-2)}
.jd-gh .gh-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.jd-gh button,.jd-gh select,.jd-gh textarea{font:12px var(--mono);color:var(--text);background:var(--field);border:1px solid var(--line-hi)}
.jd-gh button{min-height:32px;padding:0 12px;cursor:pointer}
.jd-gh button:focus-visible,.jd-gh select:focus-visible,.jd-gh textarea:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.jd-gh textarea{width:100%;min-height:72px;padding:6px 8px;resize:vertical}
.jd-gh code{font:600 16px var(--mono);letter-spacing:.1em;color:var(--accent)}
.jd-gh a{color:var(--accent)}
.jd-gh .gh-ok{border-color:var(--ok);color:var(--ok)}
.jd-verb{display:flex;align-items:center;gap:8px;font:14px/1.3 var(--mono);color:var(--text)}
.jd-verb .glyph{display:grid;grid-template-columns:5px 5px;grid-template-rows:5px 5px;gap:1px}
.jd-verb .glyph span{background:var(--line-hi)}.jd-verb .glyph span.lit{background:var(--px)}
.jd-verb .phase{padding:0 5px;border:1px solid var(--line-hi);font-size:10px;line-height:1.6;letter-spacing:.08em;color:var(--muted)}
.jd-tiles{flex:1 1 420px;min-width:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(120px,100%),1fr));gap:8px}
.jd-tile{padding:8px 10px;background:var(--bg);box-shadow:var(--elev)}
.jd-tile b{display:flex;align-items:center;gap:8px;font:600 18px/1.25 var(--mono);font-variant-numeric:tabular-nums;color:var(--text)}
.jd-tile b.ok{color:var(--ok)}.jd-tile b.warn{color:var(--warn)}.jd-tile b.bad{color:var(--bad)}
.jd-tile>span{font-size:12px;color:var(--muted)}
.jd-pips{display:flex;gap:2px}.jd-pips span{width:6px;height:6px;background:var(--line-hi)}
.jd-pips span.done{background:var(--text)}.jd-pips span.now{background:var(--accent)}
.jd-cols{display:flex;flex-wrap:wrap;align-items:flex-start;gap:16px}
.jd-panel{min-width:0;padding:14px 16px 16px;background:var(--panel);box-shadow:var(--elev)}
.jd-panel h2{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin:0 0 12px;font:600 13px/1.3 var(--mono);letter-spacing:-.01em;color:var(--text)}
.jd-panel h2 .t{display:flex;align-items:center;gap:9px}
.jd-panel h2 .t::before{content:"";width:7px;height:7px;background:var(--ok)}
.jd-panel h2 .count{font-weight:400;font-size:12px;color:var(--muted)}
.jd-runlog{flex:1 1 300px}
.jd-right{flex:0 1 1032px;min-width:0;display:flex;flex-direction:column;gap:16px}
.runlog{margin:0;padding:0;list-style:none;font-family:var(--mono)}
.runlog li{margin:0 -8px;border-bottom:1px solid var(--line)}
.runlog .visit{display:flex;gap:10px;width:100%;box-sizing:border-box;padding:8px;border:0;text-align:left;font-family:inherit;background:transparent;color:var(--text)}
.runlog button{cursor:pointer}
.runlog [aria-pressed="true"],.runlog [aria-current="true"]{background:var(--sel);box-shadow:inset 0 0 0 1px var(--accent)}
.runlog .num{flex:0 0 20px;font-size:12px;line-height:1.5;color:var(--muted)}
.runlog [aria-pressed="true"] .num,.runlog [aria-current="true"] .num{color:var(--accent)}
.runlog .mark{flex:0 0 8px;height:8px;margin-top:5px;box-sizing:border-box;background:var(--m)}
.runlog .mark.off{background:transparent;border:2px dotted var(--m)}
.runlog .main{display:block;min-width:0}
.runlog .top{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px}
.runlog .name{font-size:13px;line-height:1.5;font-weight:600;color:var(--text)}
.runlog .now .name{color:var(--accent)}.runlog .stop .name{color:var(--bad)}
.runlog .who{font-size:11px;color:var(--m)}
.runlog .dur{font-size:11px;color:var(--text-2)}
.runlog .offchip{padding:0 6px;border:1px solid rgba(255,158,100,0.4);background:rgba(255,158,100,0.09);color:#ff9e64;font-size:11px;line-height:1.6;font-weight:500;white-space:nowrap}
.runlog .note{display:block;font:12px/1.5 var(--sans);color:var(--text-2)}
.jd-foot{margin:12px 0 0;font:12px/1.5 var(--mono);color:var(--muted)}
.jd-box{position:relative;overflow-x:auto;box-shadow:var(--elev)}
.jd-box canvas{display:block;width:1000px;max-width:none}
.journey{position:absolute;inset:0;margin:0;padding:0;list-style:none;width:1000px}
.journey li{display:contents}
.journey button{position:absolute;padding:0;border:0;background:transparent;cursor:pointer}
.journey button:focus-visible{outline:2px solid var(--accent)}
.journey .step{display:none}
.jd-cap{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 12px;padding:10px 12px;border-top:1px solid var(--line);font:12px/1.5 var(--mono)}
.jd-cap .idx{color:var(--muted)}
.jd-cap .st{font-size:13px;font-weight:600;color:var(--text)}
.jd-cap.now .st{color:var(--accent)}
.jd-cap.shipped .st{color:#9ece6a}
.jd-cap.stopped .st{color:#f7768e}
.jd-cap .end{font-size:13px;font-weight:600;white-space:nowrap;color:var(--m)}
.jd-cap .who{padding:0 6px;border:1px solid var(--m);color:var(--m);font-size:11px;line-height:1.6;white-space:nowrap}
.jd-cap .dur{color:var(--text);white-space:nowrap}
.jd-cap .note{flex:1 1 240px;min-width:0;font:13px/1.5 var(--sans);color:var(--text-2)}
.jd-ctl{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;padding:0 12px 12px;font:12px/1.5 var(--mono);color:#8b93b8}
.jd-ctl .axis{display:flex;gap:0}
.jd-ctl button{height:44px;padding:0 12px;font:inherit;font-size:12px;cursor:pointer;background:#1f2230;color:#c0caf5;border:1px solid #414868}
.jd-ctl button[aria-pressed="true"]{background:rgba(158,206,106,0.09);color:#9ece6a;border-color:#9ece6a}
.jd-ctl .gap{flex:1 1 0}
.jd-ctl .key{display:flex;align-items:center;gap:6px}
.jd-ctl .key i{width:12px;height:8px;background:var(--m)}
.jd-ctl .key i.off{width:4px;height:12px;background:repeating-linear-gradient(180deg,var(--m) 0 4px,transparent 4px 8px)}
.jd-legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:10px;font:12px/1.5 var(--mono);color:var(--muted)}
.jd-legend span{display:flex;align-items:center;gap:6px}
.jd-legend i{width:10px;height:10px;background:var(--m)}
.jd-legend i.off{width:20px;height:4px;background:repeating-linear-gradient(90deg,var(--m) 0 8px,transparent 8px 12px)}
.jd-legend i.line{width:20px;height:4px}
.jd-legend i.now{box-sizing:border-box;border:2px solid var(--accent);background:var(--sel)}
.jd-legend i.next{width:20px;height:4px;background:repeating-linear-gradient(90deg,#6b7399 0 8px,transparent 8px 12px)}
.jd-next{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr));gap:8px}
.jd-next .card{display:flex;flex-direction:column;gap:4px;padding:10px 12px;background:var(--bg);border:1px dashed #6b7399}
.jd-next .card.human{border-color:#ff9e64}.jd-next .card.stop{border-color:var(--bad)}
.jd-next .head{display:flex;align-items:baseline;gap:8px;font-family:var(--mono)}
.jd-next .to{font-size:13px;font-weight:600;color:var(--text)}
.jd-next .human .to{color:#ff9e64}.jd-next .stop .to{color:var(--bad)}
.jd-next .tag{font-size:11px;color:var(--muted)}
.jd-next .why{font-size:13px;color:var(--text-2)}
.jd-chart{min-width:0;padding:10px 16px 14px;background:var(--panel);box-shadow:var(--elev)}
.jd-chart summary{cursor:pointer;font:600 13px/1.8 var(--mono);color:var(--text)}
.jd-chart summary .count{font-weight:400;font-size:12px;color:var(--muted)}
.jd-chart .jd-box{margin-top:10px}
.lr{--lr:var(--brand);display:flex;flex-direction:column;gap:14px;padding:14px 14px 12px;background:var(--field);color:var(--text);font:12px/1.5 var(--mono)}
.lr-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.lr-spin{display:block;flex:0 0 16px}
.lr-label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.lr-task{flex:1 1 320px;min-width:0;font:600 18px/1.35 var(--sans);color:var(--text);overflow-wrap:anywhere}
.lr-task.ok{color:var(--ok)}.lr-task.muted{color:var(--muted);font-weight:400}
.lr-last{min-width:0;color:var(--muted);overflow-wrap:anywhere}.lr-last span{color:var(--text-2)}
.lr-cols{display:flex;flex-wrap:wrap;align-items:flex-start;gap:14px 24px}
.lr-col{flex:1 1 400px;min-width:0;display:flex;flex-direction:column;gap:8px}
.lr-colhead{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.lr-count{color:var(--text-2)}
.lr-col .px{height:8px}
.lr-plan{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:4px}
.lr-plan li{display:flex;align-items:baseline;gap:10px;padding:2px 6px}
.lr-mark{flex:0 0 8px;height:8px;box-sizing:border-box;border:1px solid var(--line-hi)}
.lr-text{flex:1 1 auto;min-width:0;font:400 13px/1.5 var(--sans);color:var(--muted);overflow-wrap:anywhere}
.lr-tag{font-size:11px;white-space:nowrap;color:var(--muted)}
.lr-plan .done .lr-mark{background:var(--ok);border-color:var(--ok)}.lr-plan .done .lr-text{color:var(--text-2)}
.lr-plan .now{background:var(--panel)}
.lr-plan .now .lr-mark{background:var(--lr);border-color:var(--lr)}.lr-plan .now .lr-text{font-weight:600;color:var(--text)}
.lr-plan .now .lr-tag{color:var(--accent)}
.lr-alone{padding:10px;border:1px dashed var(--line-hi);color:var(--muted)}
.lr-agent{display:flex;flex-direction:column;background:var(--dim);box-shadow:var(--elev)}
.lr-agent-body{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;padding:6px 10px 8px}
.lr-kind{min-width:0;padding:0 6px;border:1px solid var(--line-hi);color:var(--text-2);font-size:11px;line-height:1.6;overflow-wrap:anywhere}
.lr-desc{flex:1 1 200px;min-width:0;font:13px/1.5 var(--sans);color:var(--text);overflow-wrap:anywhere}
.lr-status{font-size:11px;white-space:nowrap}
.lr-status.running{color:var(--accent)}.lr-status.finished{color:var(--ok)}.lr-status.failed{color:var(--bad)}
.lr-tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}
.lr-tile{padding:8px 10px;background:var(--dim);box-shadow:var(--elev)}
.lr-tile b{display:block;font-size:16px;line-height:1.25;font-weight:600;font-variant-numeric:tabular-nums;color:var(--text)}
.lr-tile span{color:var(--muted)}
@media (max-width:640px){.lr-tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (scripting:none){.jd-box canvas,.journey button,.jd-ctl .axis,.jd-ctl [data-replay],.jd-chart .jd-box{display:none}.journey{position:static;width:auto;padding:8px 12px 8px 36px;list-style:decimal;font:12px/1.6 var(--mono);color:var(--text-2)}.journey li{display:list-item}.journey .step{display:inline}}
"""


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
    return {
        "visits": visits,
        "selected": current[0] if current else len(visits) - 1,
        "state": state,
        "terminal": bool(flow.get("terminal")),
        "next": nexts,
        "hops": max(0, len(visits) - 1),
        "off_chart_count": sum(1 for v in visits if v["off_chart"]),
        "hops_to_shipped": flow.get("hops_to_shipped"),
        "all_durations_known": bool(flow.get("all_durations_known")),
        "state_age_seconds": flow.get("state_age_seconds"),
        "ended": ended,
        "end": flow[ended] if ended else None,
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


def _et_clock(epoch: float) -> str:
    at = datetime.fromtimestamp(epoch, ET)
    return f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'} ET"


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
    return _tile("no reading" if age is None else duration(age), label)


def _stopped_for(row: dict, model: dict) -> str:
    at = row["flow"]["stopped"]["at"]
    return _age_tile(None if at is None else model["now"] - at, "stopped")


def _elapsed_label(row: dict) -> str:
    return f"T+ on {row['device']}" if row.get("device") else "T+ on this device"


TILES = {
    "elapsed_here": lambda row, model: _tile(duration(row.get("elapsed_seconds")), "T+ on this device"),
    "elapsed": lambda row, model: _tile(duration(row.get("elapsed_seconds")), _elapsed_label(row)),
    "heartbeat": _heartbeat_tile,
    "waiting": lambda row, model: _tile(duration(row.get("age_seconds")), "waiting"),
    "not_started": lambda row, model: _tile("not started", "last heartbeat"),
    "round": _round_tile,
    "record_round": lambda row, model: _tile("no round" if row["flow"]["round"] is None else str(row["flow"]["round"]), "revise round"),
    "in_state": lambda row, model: _age_tile(model["state_age_seconds"], f"in {model['state']}"),
    "stopped_for": _stopped_for,
    "hops": lambda row, model: _tile(f"{model['hops']} hops", f"{model['off_chart_count']} off-chart"),
}


def _trouble(row: dict) -> str:
    return (
        _state_chip(row)
        + (_chip("OVERDUE", "bad") if row.get("overdue") else "")
        + (_chip(f"retried {row['retries']}x") if row.get("retries") else "")
    )


def _hero(row: dict, model: dict, motion: str, strip: str, title: str, tiles: tuple[str, ...]) -> str:
    cells = "".join(TILES[name](row, model) for name in tiles)
    return (
        f'<section class="jd-hero">{strip}<div class="body"><div class="who"><div class="title">{title}</div>'
        f'{_hero_line(row, model)}{_verb(row, motion)}</div><div class="jd-tiles">{cells}</div></div></section>'
    )


def _job_hero(row: dict, model: dict, state: str) -> str:
    motion = _motion(row, model)
    strip = _px().px_strip(f"job-hero-{row['job']}", state=motion, bot=row.get("recipient"), cell=4, height=40, seed=2)
    kind = _job_kind(row)
    running = state == "running"
    device = f"on {row['device']}" if running and row.get("device") else "queued" if not running else ""
    title = (
        f'<h1>{_e(_job_label(row))}</h1>{_bot(row.get("recipient"))}'
        + (_span("kind", kind) if kind else "")
        + (_span("device", device) if device else "")
        + _trouble(row)
        + github_links(row)
    )
    return _hero(row, model, motion, strip, title, JOB_TILES[state])


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


def _ticket_hero(found: dict, model: dict, spec: dict) -> str:
    row = found["row"]
    motion = _motion(row, model)
    strip = _px().px_strip(f"ticket-hero-{found['address']}", **{
        "state": motion, "bot": row.get("recipient"), "still": spec["still"], "cell": 4, "height": 40, "seed": 2,
        **spec["strip"]})
    title = (
        f'<h1>{_e(_tile_label(row))}</h1>'
        + (_bot(row["recipient"]) if "recipient" in row else "")
        + _ticket_tracker(row, spec["tail"])
        + _trouble(row)
    )
    return _hero(row, model, motion, strip, title, spec["tiles"])


def _now_dur(model: dict) -> str:
    age = model["state_age_seconds"]
    return "in progress" if age is None else f"{duration(age)} so far"


def _visit_dur(visit: dict, model: dict) -> str:
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


def _runlog_row(visit: dict, model: dict, pressable: bool) -> str:
    tint = _who_tint(visit["who"])
    cls = " ".join(c for c in ("visit", "now" if visit["current"] else "", "stop" if visit["stop"] else "") if c)
    mark_tint = STOP_TINT if visit["stop"] else tint
    name = visit["state"] + (" · now" if visit["current"] else "")
    num = f"{visit['index'] + 1:02d}"
    on = visit["index"] == model["selected"]
    if pressable:
        tag, kind, picked = "button", ' type="button"', f' aria-pressed="{"true" if on else "false"}"'
    else:
        tag, kind, picked = "div", "", ' aria-current="true"' if on else ""
    return (
        f'<li><{tag} class="{cls}"{kind} data-visit="{visit["index"]}" data-state="{_e(visit["state"])}"{picked}'
        f' style="--m:{tint}">'
        f'{_span("num", num)}'
        f'<span class="mark{" off" if visit["off_chart"] else ""}" aria-hidden="true" style="--m:{mark_tint}"></span>'
        f'<span class="main"><span class="top">{_span("name", name)}{_span("who", visit["who"])}'
        f'{_span("dur", _visit_dur(visit, model))}'
        + (_span("offchip", "off-chart") if visit["off_chart"] else "")
        + f'</span>{_span("note", _reason(visit))}</span></{tag}></li>'
    )


def _runlog(model: dict, pressable: bool) -> str:
    rows = "".join(_runlog_row(v, model, pressable) for v in model["visits"])
    count = f"{len(model['visits'])} visits · oldest first"
    body = f'<ol class="runlog">{rows}</ol>' if rows else '<p class="empty">no visits recorded</p>'
    return (
        f'<section class="jd-panel jd-runlog"><h2>{_span("t", "Run log")}{_span("count", count)}</h2>{body}'
        '<p class="jd-foot">Pick a step here or on the chart; both follow.</p></section>'
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


def _lr_tiles(t: dict) -> str:
    known = t.get("tokens_known", True)
    tokens_in, cached = t["tokens_in"], t["tokens_cached"]
    in_label = "tokens in"
    if known and tokens_in:
        in_label += f" · {(200 * cached + tokens_in) // (2 * tokens_in)}% cached"
    tiles = (
        (compact(tokens_in) if known else NO_DATA, in_label),
        (compact(t["tokens_out"]) if known else NO_DATA, "tokens out"),
        (NO_DATA if t["cost_usd"] is None else _dollars(t["cost_usd"]), "cost so far"),
        (f"{t['files_touched']} files · {t['commands']} commands", "touched and run"),
    )
    body = "".join(f'<div class="lr-tile"><b>{_e(v)}</b>{_span(None, label)}</div>' for v, label in tiles)
    return f'<div class="lr-tiles">{body}</div>'


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
        f'<section class="jd-panel" id="liverun-{_e(anchor(row["job"]))}">'
        f'<h2>{_span("t", "Live run")}{_span("count", sub)}</h2>{live_run(row, not started)}</section>'
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
        return "—" if visit["started_at"] is None else f"at {_et_clock(visit['started_at'])}"
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
    legend += f'<span class="key"><i style="--m:{STOP_TINT}"></i>stopped</span>'
    legend += f'<span class="key"><i class="off" style="--m:{HOP_TINT}"></i>off-chart hop</span>'
    return (
        f'<section class="jd-panel"><h2>{_span("t", "Journey")}'
        f'{_span("count", "this ticket through time · pick a step for its reason")}</h2>'
        f'<div class="jd-box"><canvas class="jd-journey" width="1000" height="336" role="img" '
        f'aria-label="{_e(_journey_label(model))}"></canvas><ol class="journey">{hits}</ol></div>{_caption(model)}'
        '<div class="jd-ctl"><div class="axis" role="group" aria-label="Time axis">'
        '<button type="button" data-scale="even" aria-pressed="true">even steps</button>'
        f'{scale}</div>{"" if model["ended"] == "stopped" else REPLAY}<span class="gap"></span>{legend}</div></section>'
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
    return f'<section class="jd-panel"><h2>{_span("t", "Can come next")}{_span("count", sub)}</h2>{body}</section>'


def _chart(key: str, model: dict, chart: dict | None) -> str:
    if chart is None:
        return '<details class="jd-chart"><summary>Full state chart</summary><p class="empty">statechart unavailable</p></details>'
    n = len(chart["states"])
    grid = flowmap_layout(chart)["grid"]
    legend = "".join(f'<span><i class="line" style="background:{TINT[w]}"></i>path taken · {w}</span>' for w in _legend_who(model))
    legend += '<span><i class="line" style="background:#5386a6"></i>visited</span>'
    legend += f'<span><i class="off" style="--m:{TINT["script"]}"></i>off-chart jump</span>'
    for cls, bg, text in CHART_KEYS[model["ended"] or "live"]:
        tint = f' style="background:{bg}"' if bg else ""
        legend += f'<span><i class="{cls}"{tint}></i>{text}</span>'
    return (
        f'<details class="jd-chart" data-key="full-chart:{_e(key)}"><summary>Full state chart '
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
}


def _cols(part: dict, sections: tuple[str, ...], pressable: bool) -> str:
    right = "".join(SECTIONS[name](part) for name in sections)
    return f'<div class="jd-cols">{_runlog(part["model"], pressable)}<div class="jd-right">{right}</div></div>'


def _job_article(row: dict, state: str, chart: dict | None) -> str:
    model = _detail_model(row, chart)
    part = {"row": row, "model": model, "chart": chart, "key": row["job"], "started": state == "running"}
    return (
        f'<article class="jobpage" id="{anchor(row["job"])}">'
        + _crumb(_job_label(row), "job " + str(row["job"]))
        + _job_hero(row, model, state)
        + _cols(part, SECTION_ORDER, pressable=False)
        + "</article>"
    )


def _jobview(snapshot: dict) -> str:
    chart = snapshot.get("statechart")
    jobs = _active_jobs(snapshot)
    inner = (
        "".join(_job_article(row, state, chart) for row, state in jobs)
        if jobs
        else '<p class="empty">no running or queued jobs</p>'
    )
    return f'<div id="jobview">{inner}</div>'


_PICK = {
    "running": lambda rows: min(rows, key=lambda row: row.get("elapsed_seconds") or 0),
    "queued": lambda rows: rows[0],
}


def _ticket_of(snapshot: dict, row: dict) -> dict | None:
    key = _thread_key(row)
    return next((t for t in snapshot["tickets"] if _thread_key(t) == key), None)


def record_row(record: dict, ticket: dict | None) -> dict:
    refs = {k: ticket[k] for k in ROW_FROM_TICKET if k in ticket} if ticket else {}
    return {"repo": record["repo"], "ticket_id": record["ticket_id"], "flow": record, **refs}


def resolve_ticket(snapshot: dict, address: str, records: dict | None = None) -> dict | None:
    repo, _, ticket_id = address.partition("#")

    def exact(row: dict) -> bool:
        return ticket_address(row) == address

    def any_case(row: dict) -> bool:
        return str(row["repo"]).lower() == repo.lower() and str(row["ticket_id"]) == ticket_id

    for rule in (exact, any_case):
        for kind in LIVE_KINDS:
            rows = [row for row in snapshot[kind] if rule(row)]
            if rows:
                row = _PICK[kind](rows)
                return {"kind": kind, "address": ticket_address(row), "row": row, "ticket": _ticket_of(snapshot, row)}
    if not records:
        return None
    key = address if address in records else next((k for k in sorted(records) if any_case(records[k])), None)
    if key is None:
        return None
    record = records[key]
    ticket = _ticket_of(snapshot, record)
    kind = next((flag for flag in RECORD_FLAGS if record.get(flag)), "idle")
    return {"kind": kind, "address": key, "row": record_row(record, ticket), "ticket": ticket}


def _ticket_article(found: dict, snapshot: dict) -> str:
    row, address = found["row"], found["address"]
    spec = TICKET_KINDS[found["kind"]]
    chart = snapshot.get("statechart")
    model = {**_detail_model(row, chart), "now": snapshot.get("generated_at_epoch")}
    start = _first_start(model)
    part = {"row": row, "model": model, "chart": chart, "key": address, "started": spec["started"]}
    return (
        f'<article class="jobpage ticketpage" data-ticket="{_e(address)}">'
        + _crumb(_tile_label(row), None if start is None else "started " + _et_clock(start))
        + _ticket_hero(found, model, spec)
        + _cols(part, spec["sections"], pressable=True)
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


def ticket_view(snapshot: dict, address: str | None) -> str:
    records = _records(snapshot)
    found = None if address is None else resolve_ticket(snapshot, address, records)
    inner = _ticket_article(found, snapshot) if found else _no_ticket(address, records is not None)
    return f'<div id="ticketview">{inner}</div>'


def _ticketview(snapshot: dict) -> str:
    return ticket_view(snapshot, None)


_SCRIPT = r"""
  var DETAIL = {verbs: __VERBS__, hb: __HB__, order: {normal: 0, human: 1, stop: 2},
                first: 'ticket arrives', stop: '#f7768e', hop: '#c0caf5', who: ['script', 'claude', 'codex', 'cursor', 'grok'],
                chartSub: __CHARTSUB__, trackers: __TRACKERS__, sections: __SECTIONS__, jobTiles: __JOBTILES__,
                kinds: __KINDS__, liveKinds: __LIVEKINDS__, recordFlags: __RECORDFLAGS__, rowFromTicket: __ROWFROMTICKET__,
                panels: __PANELS__, chartKeys: __CHARTKEYS__, endTint: __ENDTINT__};
  var SEL = {};
  var journeys = [];
  var reducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  function whoTint(w) { return JOURNEY.tint[w] || JOURNEY.tint.script; }
  function detailModel(r, chart) {
    var flow = r.flow || {};
    var ended = DETAIL.recordFlags.filter(function (f) { return flow[f]; })[0] || null;
    var phases = chart ? phaseTable(chart).states : {};
    var visits = (flow.visits || []).map(function (v, i) {
      var o = {};
      Object.keys(v).forEach(function (k) { o[k] = v[k]; });
      o.index = i;
      o.stop = phases[v.state] === 'Stop';
      return o;
    });
    var current = visits.filter(function (v) { return v.current; });
    var state = flow.state === undefined ? null : flow.state;
    var seen = {};
    visits.forEach(function (v) { seen[v.state] = (seen[v.state] || 0) + 1; });
    function kind(to) { return phases[to] === 'Stop' ? 'stop' : WAITING[to] ? 'human' : 'normal'; }
    var nexts = ((chart && chart.edges) || []).filter(function (e) { return state !== null && e.from === state; })
      .map(function (e, i) { return {to: e.to, label: e.label, kind: kind(e.to), count: seen[e.to] || 0, i: i}; });
    nexts.sort(function (a, b) { return (DETAIL.order[a.kind] - DETAIL.order[b.kind]) || (a.i - b.i); });
    return {
      visits: visits, selected: current.length ? current[0].index : visits.length - 1, state: state,
      terminal: !!flow.terminal, next: nexts, hops: Math.max(0, visits.length - 1),
      off_chart_count: visits.filter(function (v) { return v.off_chart; }).length,
      hops_to_shipped: flow.hops_to_shipped === undefined ? null : flow.hops_to_shipped,
      all_durations_known: !!flow.all_durations_known,
      state_age_seconds: flow.state_age_seconds === undefined ? null : flow.state_age_seconds,
      ended: ended, end: ended ? flow[ended] : null
    };
  }
  function endText(m) {
    var end = m.end, visits = m.visits, n = visits.length;
    if (m.ended === 'shipped') {
      var known = end.intake_to_ship_seconds !== null && end.intake_to_ship_seconds !== undefined;
      return 'shipped · ' + m.hops + (m.hops === 1 ? ' hop' : ' hops') + (known ? ' · ' + dur(end.intake_to_ship_seconds) : '');
    }
    var where = end.stopped_in;
    if (!where) where = n > 1 && visits[n - 1].stop ? visits[n - 2].state : n ? visits[n - 1].state : end.state;
    return 'stopped in ' + where + (m.state_age_seconds === null ? '' : ' · ' + dur(m.state_age_seconds) + ' ago');
  }
  function flowModel(m) {
    var visits = m.visits, state = m.state, next = m.next.map(function (n) { return n.to; });
    if (m.ended === 'stopped') {
      state = m.end.state;
      if (!visits.length || visits[visits.length - 1].state !== state) {
        visits = visits.concat([{index: visits.length, state: state, who: 'script', off_chart: false, current: true, stop: true}]);
      }
      next = (m.end.ways_out || []).map(function (w) { return w.to; });
    } else if (m.ended === 'shipped') next = [];
    return {visits: visits, state: state, next: next};
  }
  function roundOf(r) {
    var m = /^\s*(\d+)\s*\/\s*(\d+)\s*$/.exec(String(r.round === null || r.round === undefined ? '' : r.round));
    return m ? [Number(m[1]), Number(m[2])] : null;
  }
  function span(parent, cls, text) { return add(parent, el('span', cls, text)); }
  function crumb(label, right) {
    var nav = el('nav', 'jd-crumb');
    nav.setAttribute('aria-label', 'Breadcrumb');
    var a = add(nav, link(null, 'job.html'));
    a.textContent = 'Tickets';
    span(nav, null, '/').setAttribute('aria-hidden', 'true');
    span(nav, 'here', label);
    if (right !== null) span(nav, 'job', right);
    return nav;
  }
  function firstStart(m) {
    var starts = m.visits.map(function (v) { return v.started_at; }).filter(function (t) { return t !== null && t !== undefined; });
    return starts.length ? Math.min.apply(null, starts) : null;
  }
  var ET = null;
  function etClock(epoch) {
    ET = ET || new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit', hour12: true});
    var p = {};
    ET.formatToParts(new Date(epoch * 1000)).forEach(function (part) { p[part.type] = part.value; });
    return p.hour + ':' + p.minute + ' ' + p.dayPeriod + ' ET';
  }
  function tile(parent, value, label, cls) {
    var t = add(parent, el('div', 'jd-tile'));
    var b = add(t, el('b', cls || null));
    if (typeof value === 'string') b.textContent = value; else add(b, value);
    span(t, null, label);
    return b;
  }
  function motionOf(r, m) { return WAITING[m.state] ? 'waiting' : PX.state(r); }
  function holdsStill(motion) {
    return !PX.table.effects[(PX.table.states[motion] || PX.table.states.unknown).effect];
  }
  var TILES = {
    elapsed_here: function (box, r) { ticking(tile(box, '', 'T+ on this device'), r.elapsed_seconds); },
    elapsed: function (box, r) { ticking(tile(box, '', r.device ? 'T+ on ' + r.device : 'T+ on this device'), r.elapsed_seconds); },
    heartbeat: function (box, r) {
      var hb = r.heartbeat_age_seconds;
      var tone = DETAIL.hb[PX.state(r)];
      if (hb === null || hb === undefined) tile(box, 'not started', 'last heartbeat', tone);
      else ticking(tile(box, '', 'last heartbeat', tone), hb, '', ' ago');
    },
    waiting: function (box, r) { ticking(tile(box, '', 'waiting'), r.age_seconds); },
    not_started: function (box) { tile(box, 'not started', 'last heartbeat'); },
    round: function (box, r) {
      var rnd = roundOf(r);
      if (!rnd) { tile(box, 'no round', 'revise round'); return; }
      var b = tile(box, rnd[0] + ' / ' + rnd[1], 'revise round');
      var pips = add(b, el('span', 'jd-pips'));
      pips.setAttribute('aria-hidden', 'true');
      for (var i = 0; i < rnd[1]; i++) span(pips, i < rnd[0] - 1 ? 'done' : i === rnd[0] - 1 ? 'now' : null);
    },
    record_round: function (box, r) { tile(box, r.flow.round === null ? 'no round' : String(r.flow.round), 'revise round'); },
    in_state: function (box, r, m) { ageTile(box, m.state_age_seconds, 'in ' + m.state); },
    stopped_for: function (box, r, m) {
      var at = r.flow.stopped.at;
      ageTile(box, at === null || at === undefined ? null : m.now - at, 'stopped');
    },
    hops: function (box, r, m) { tile(box, m.hops + ' hops', m.off_chart_count + ' off-chart'); }
  };
  function ageTile(box, age, label) {
    if (age === null || age === undefined) tile(box, 'no reading', label);
    else ticking(tile(box, '', label), age);
  }
  function troubleChips(title, r) {
    var jobState = stateChip(r);
    if (jobState) add(title, jobState);
    if (r.overdue) add(title, chip('OVERDUE', 'bad'));
    if (r.retries) add(title, chip('retried ' + r.retries + 'x'));
  }
  function trackerText(r) {
    var source = String(r.source), pr = String(r.pr);
    var first = r.tracker === 'linear' ? String(r.ticket_id) : /^#[0-9]+$/.test(source) ? 'issue ' + source : '';
    var refs = [first, /^[0-9]+$/.test(pr) ? 'PR #' + pr : ''].filter(Boolean).join(' · ');
    return [DETAIL.trackers[r.tracker], refs].filter(Boolean).join(' ');
  }
  function trackerSpan(title, r, tail) {
    var text = trackerText(r);
    if (tail === 'queued') {
      ticking(span(title, 'tracker'), r.age_seconds, [text, 'queued for ' + r.recipient].filter(Boolean).join(' · ') + ' · ');
      return;
    }
    text = [text, tail === 'device' && r.device ? 'on ' + r.device : ''].filter(Boolean).join(' · ');
    if (text) span(title, 'tracker', text);
  }
  function hero(c, strip, label, tags, ticket, tiles) {
    var r = c.r, m = c.m, motion = c.motion;
    var sec = el('section', 'jd-hero');
    var action = String(r.action || '');
    add(sec, strip);
    var body = add(sec, el('div', 'body'));
    var who = add(body, el('div', 'who'));
    var title = add(who, el('div', 'title'));
    add(title, el('h1', null, label));
    if ('recipient' in r) add(title, bot(r.recipient));
    tags(title);
    if (GH.on && ghRefs(r)) add(who, ghPanel(r, ticket));
    var line = add(who, el('p', 'line'));
    var parts = 0;
    function sep() { if (parts++) line.appendChild(document.createTextNode(' · ')); }
    if (m.state) { sep(); line.appendChild(document.createTextNode('now ')); add(line, el('b', null, m.state)); }
    var rnd = roundOf(r);
    if (rnd) { sep(); line.appendChild(document.createTextNode('round ' + rnd[0] + ' of ' + rnd[1])); }
    if (m.state && m.state_age_seconds !== null) { sep(); ticking(add(line, el('span')), m.state_age_seconds, '', ' in ' + m.state); }
    if (!parts) line.textContent = 'state not recorded';
    var verb = add(who, el('div', 'jd-verb'));
    var glyph = span(verb, 'glyph');
    glyph.setAttribute('aria-hidden', 'true');
    [0, 1, 2, 3].forEach(function (i) { span(glyph, i === 2 ? 'lit' : null); });
    var verbs = PX.table.actions.hasOwnProperty(motion) ? DETAIL.verbs[motion] : null;
    if (verbs) span(verb, 'verb', verbs[Math.floor(Date.now() / 4000) % verbs.length] + '…');
    if (action) span(verb, 'phase', action.toUpperCase());
    var box = add(body, el('div', 'jd-tiles'));
    tiles.forEach(function (name) { TILES[name](box, r, m); });
    return sec;
  }
  // GitHub actions: present only when the server answers /api/session. The
  // session is an HttpOnly cookie; the page only ever holds the CSRF value.
  var GH = {started: false, on: false, login: null, csrf: null, flow: null, note: '', panels: {}};
  var SHA = /^[0-9a-f]{40}$/;

  function ghPost(path, body, csrf) {
    var headers = {'Content-Type': 'application/json'};
    if (csrf) headers['X-Factory-CSRF'] = GH.csrf || '';
    return fetch(path, {method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: headers,
                        body: JSON.stringify(body)})
      .then(function (res) {
        return res.json().then(function (data) { return data; }, function () { return null; }).then(function (data) {
          data = data && typeof data === 'object' ? data : {};
          return {status: res.status, ok: res.ok, data: data, error: res.ok ? null : String(data.error || 'http-' + res.status)};
        });
      }, function () { return {status: 0, ok: false, data: {}, error: 'network'}; });
  }
  function ghSet(signedIn, data) {
    GH.login = signedIn ? String(data.login || '') : null;
    GH.csrf = signedIn ? String(data.csrf || '') : null;
    Object.keys(GH.panels).forEach(function (k) { ghDraw(GH.panels[k]); });
  }
  function ghStart() {
    var request;
    try { request = fetch('/api/session', {cache: 'no-store', credentials: 'same-origin'}); } catch (err) { return; }
    request.then(function (res) {
      if (res.status !== 200) return null;
      return res.json();
    }).then(function (data) {
      if (!data || typeof data.signed_in !== 'boolean') return;
      GH.on = true;
      ghSet(data.signed_in, data);
      if (latest) render(latest);
    }, function () {});
  }
  function ghSignIn() {
    GH.note = '';
    ghPost('/api/signin', {}, false).then(function (res) {
      if (!res.ok) { GH.note = 'sign-in failed: ' + res.error; ghSet(false, {}); return; }
      var d = res.data;
      GH.flow = {code: String(d.user_code || ''), uri: String(d.verification_uri || ''),
                 interval: Math.max(1, Number(d.interval) || 5)};
      ghSet(false, {});
      setTimeout(ghPoll, GH.flow.interval * 1000);
    });
  }
  function ghPoll() {
    if (!GH.flow) return;
    ghPost('/api/signin/poll', {}, false).then(function (res) {
      var state = res.ok ? res.data.state : null;
      if (state === 'pending') { setTimeout(ghPoll, GH.flow.interval * 1000); return; }
      GH.flow = null;
      GH.note = state === 'signed-in' ? '' : 'sign-in ' + (state || 'failed: ' + res.error);
      ghSet(state === 'signed-in', res.data);
    });
  }
  function ghSignOut() {
    ghPost('/api/signout', {}, true).then(function () { GH.note = ''; ghSet(false, {}); });
  }
  function ghButton(parent, text, onClick, cls) {
    var b = add(parent, el('button', cls || null, text));
    b.setAttribute('type', 'button');
    b.addEventListener('click', onClick);
    return b;
  }
  function ghResult(out, res, text) {
    out.replaceChildren();
    var url = res.ok ? String(res.data.url || '') : '';
    if (/^https:\/\/github\.com\//.test(url)) {
      add(out, document.createTextNode(text + ' '));
      var a = add(out, link(null, url));
      a.setAttribute('rel', 'noopener noreferrer');
      a.setAttribute('target', '_blank');
      a.textContent = url;
    } else out.textContent = res.ok ? text : 'error: ' + res.error;
  }
  function ghBuild() {
    var p = {root: el('div', 'jd-gh'), head: null, busy: false, sent: false};
    p.auth = add(p.root, el('div', 'gh-row'));
    p.form = add(p.root, el('div', 'gh-form'));
    var row = add(p.form, el('div', 'gh-row'));
    p.target = add(row, el('select'));
    p.target.setAttribute('aria-label', 'Comment on');
    p.post = ghButton(row, 'Post', function () { ghComment(p); });
    p.text = add(p.form, el('textarea'));
    p.text.setAttribute('aria-label', 'Comment');
    p.out = add(p.form, el('div', 'gh-out'));
    p.ok = add(p.root, el('details', 'gh-okbox'));
    add(p.ok, el('summary', null, 'Give the hold OK'));
    var okRow = add(p.ok, el('div', 'gh-row'));
    p.okButton = ghButton(okRow, 'OK', function () { ghOk(p); }, 'gh-ok');
    p.okOut = add(okRow, el('span', 'gh-out'));
    p.ok.addEventListener('toggle', function () { if (p.ok.open) ghHead(p); });
    return p;
  }
  function ghDraw(p) {
    var signedIn = !!GH.login;
    p.auth.replaceChildren();
    if (signedIn) {
      add(p.auth, el('span', null, 'signed in as ' + GH.login));
      ghButton(p.auth, 'Sign out', ghSignOut);
    } else if (GH.flow) {
      add(p.auth, el('span', null, 'Code'));
      add(p.auth, el('code', null, GH.flow.code));
      if (/^https:\/\/github\.com\//.test(GH.flow.uri)) {
        var a = add(p.auth, link(null, GH.flow.uri));
        a.setAttribute('rel', 'noopener noreferrer');
        a.setAttribute('target', '_blank');
        a.textContent = GH.flow.uri;
      }
      add(p.auth, el('span', null, 'Enter only this code on github.com.'));
    } else ghButton(p.auth, 'Sign in with GitHub', ghSignIn);
    if (GH.note) add(p.auth, el('span', 'warn', GH.note));
    var refs = ghRefs(p.r);
    var options = refs ? [['issue', refs.issue, 'issue #'], ['pull', refs.pr, 'PR #']].filter(function (o) { return o[1]; }) : [];
    var want = options.map(function (o) { return o[0] + ':' + o[1]; }).join(',');
    if (p.options !== want) {
      var keep = p.target.value;
      p.target.replaceChildren();
      options.forEach(function (o) {
        var opt = add(p.target, el('option', null, o[2] + o[1]));
        opt.value = o[0] + ':' + o[1];
      });
      if (want.split(',').indexOf(keep) >= 0) p.target.value = keep;
      p.options = want;
    }
    p.form.hidden = !signedIn || !options.length;
    var t = p.t;
    var eligible = signedIn && !!t && t.state === 'awaiting-ok' && !!(refs && refs.pr) &&
      String(t.waiting_on || '').toLowerCase() === GH.login.toLowerCase();
    p.ok.hidden = !eligible;
    if (!eligible) { p.ok.open = false; p.head = null; p.okOut.textContent = ''; }
    p.okButton.disabled = !p.head || p.busy;
    p.okButton.textContent = p.sent ? 'OK sent' : p.head ? 'OK ' + p.head.slice(0, 7) : 'OK (reading head…)';
  }
  function ghPanel(r, t) {
    var key = threadKey(r);
    var p = GH.panels[key] || (GH.panels[key] = ghBuild());
    p.r = r;
    p.t = t;
    ghDraw(p);
    return p.root;
  }
  function ghComment(p) {
    var refs = ghRefs(p.r);
    var pick = String(p.target.value).split(':');
    var body = p.text.value;
    if (!refs || pick.length !== 2 || !/^[0-9]+$/.test(pick[1]) || !body.trim()) return;
    p.post.disabled = true;
    ghPost('/api/comment', {repo: refs.repo, kind: pick[0], number: Number(pick[1]), body: body}, true).then(function (res) {
      p.post.disabled = false;
      if (res.ok) p.text.value = '';
      ghResult(p.out, res, 'posted');
    });
  }
  function ghHead(p) {
    var refs = ghRefs(p.r);
    if (!refs || !refs.pr || p.busy) return;
    p.busy = true;
    p.head = null;
    p.sent = false;
    ghDraw(p);
    ghPost('/api/head', {repo: refs.repo, pr: Number(refs.pr)}, true).then(function (res) {
      p.busy = false;
      var head = String(res.data.head || '');
      if (res.ok && SHA.test(head)) p.head = head;
      else p.okOut.textContent = 'error: ' + (res.error || 'bad-head');
      ghDraw(p);
    });
  }
  function ghOk(p) {
    var refs = ghRefs(p.r);
    var head = p.head;
    if (!refs || !refs.pr || !head || p.busy) return;
    p.busy = true;
    ghDraw(p);
    ghPost('/api/ok', {repo: refs.repo, pr: Number(refs.pr), head: head}, true).then(function (res) {
      p.busy = false;
      if (res.status === 409 && res.data.error === 'head-changed') {
        p.okOut.textContent = 'head changed to ' + String(res.data.head || '').slice(0, 7);
        ghHead(p);
        return;
      }
      if (res.ok) { p.head = null; p.sent = true; }
      ghResult(p.okOut, res, 'OK posted');
      ghDraw(p);
    });
  }
  function jdPanel(cls, title, count) {
    var sec = el('section', 'jd-panel' + (cls ? ' ' + cls : ''));
    var h = add(sec, el('h2'));
    span(h, 't', title);
    span(h, 'count', count);
    return sec;
  }
  function runlog(r, m, buttons) {
    var sec = jdPanel('jd-runlog', 'Run log', m.visits.length + ' visits · oldest first');
    if (!m.visits.length) add(sec, el('p', 'empty', 'no visits recorded'));
    else {
      var ol = add(sec, el('ol', 'runlog'));
      m.visits.forEach(function (v) {
        var tint = whoTint(v.who);
        var btn = add(add(ol, el('li')), el('button', ['visit', v.current ? 'now' : '', v.stop ? 'stop' : ''].filter(Boolean).join(' ')));
        buttons.push(btn);
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-visit', String(v.index));
        btn.setAttribute('data-state', v.state);
        btn.setAttribute('aria-pressed', v.index === m.selected ? 'true' : 'false');
        btn.setAttribute('style', '--m:' + tint);
        span(btn, 'num', pad(v.index + 1));
        var mk = span(btn, 'mark' + (v.off_chart ? ' off' : ''));
        mk.setAttribute('aria-hidden', 'true');
        mk.setAttribute('style', '--m:' + (v.stop ? DETAIL.stop : tint));
        var main = span(btn, 'main');
        var top = span(main, 'top');
        span(top, 'name', v.state + (v.current ? ' · now' : ''));
        span(top, 'who', v.who);
        var d = span(top, 'dur');
        if (v.current) nowDur(m, d);
        else d.textContent = v.duration_seconds === null || v.duration_seconds === undefined ? '—' : dur(v.duration_seconds);
        if (v.off_chart) span(top, 'offchip', 'off-chart');
        span(main, 'note', reasonOf(v));
      });
    }
    add(sec, el('p', 'jd-foot', 'Pick a step here or on the chart; both follow.'));
    return sec;
  }
  var LIVE = {fields: ['current_task', 'last_finished', 'plan', 'subagents'],
              planCls: {done: 'done', in_progress: 'now', pending: 'pending'},
              planTag: {done: 'done', in_progress: 'now', pending: ''}};
  function liveMode(t) {
    if (!t) return 'none';
    return LIVE.fields.some(function (k) { return t[k] !== null && t[k] !== undefined; }) ? 'live' : 'counters';
  }
  function compact(n) {
    var tenths;
    if (n < 1000) return String(n);
    if (n < 10000) { tenths = Math.floor((n * 10 + 500) / 1000); return Math.floor(tenths / 10) + '.' + (tenths % 10) + 'k'; }
    if (n < 1000000) return Math.floor((n + 500) / 1000) + 'k';
    tenths = Math.floor((n * 10 + 500000) / 1000000);
    return Math.floor(tenths / 10) + '.' + (tenths % 10) + 'M';
  }
  function dollars(c) {
    var cents = Math.floor(c * 100 + 0.5);
    return '$' + Math.floor(cents / 100) + '.' + pad(cents % 100);
  }
  function lrTiles(t) {
    var known = t.tokens_known !== false;
    var inLabel = 'tokens in';
    if (known && t.tokens_in) inLabel += ' · ' + Math.floor((200 * t.tokens_cached + t.tokens_in) / (2 * t.tokens_in)) + '% cached';
    var box = el('div', 'lr-tiles');
    [[known ? compact(t.tokens_in) : 'no data', inLabel],
     [known ? compact(t.tokens_out) : 'no data', 'tokens out'],
     [t.cost_usd === null || t.cost_usd === undefined ? 'no data' : dollars(t.cost_usd), 'cost so far'],
     [t.files_touched + ' files · ' + t.commands + ' commands', 'touched and run']].forEach(function (s) {
      var tile = add(box, el('div', 'lr-tile'));
      add(tile, el('b', null, s[0]));
      span(tile, null, s[1]);
    });
    return box;
  }
  function lrHead(r, t, moving) {
    var finished = !!t.finished, task = t.current_task, last = t.last_finished;
    var head = el('div', 'lr-head');
    var spin = span(head, 'lr-spin');
    spin.setAttribute('aria-hidden', 'true');
    add(spin, PX.strip('live-head-' + r.job, {effect: moving && task && !finished ? 'spin3' : 'off', tint: 'bot', bot: r.recipient,
                                              seed: 71, still: !moving, cell: 4, done: finished, height: 12}));
    if (finished) {
      span(head, 'lr-label', 'finished');
      span(head, last ? 'lr-task ok' : 'lr-task muted', last || 'nothing recorded');
    } else {
      span(head, 'lr-label', 'working on');
      span(head, task ? 'lr-task' : 'lr-task muted', task || 'no task in progress');
    }
    var lastEl = span(head, 'lr-last', 'last finished: ');
    span(lastEl, null, last || 'nothing yet');
    return head;
  }
  function lrCol(title) {
    var col = el('div', 'lr-col');
    var head = add(col, el('div', 'lr-colhead'));
    span(head, 'lr-label', title);
    return {col: col, head: head};
  }
  function lrPlan(r, plan, moving) {
    var c = lrCol('Its plan');
    if (!plan.length) { add(c.col, el('p', 'empty', 'no plan yet')); return c.col; }
    var done = plan.filter(function (p) { return p.status === 'done'; }).length;
    span(c.head, 'lr-count', done + ' of ' + plan.length + ' done');
    add(c.col, PX.strip('live-plan-' + r.job, {effect: 'fill', tint: 'bot', bot: r.recipient, progress: done / plan.length,
                                               seed: 72, still: !moving, cell: 4, done: done === plan.length, height: 8}));
    var ol = add(c.col, el('ol', 'lr-plan'));
    plan.forEach(function (p) {
      var li = add(ol, el('li', LIVE.planCls[p.status]));
      span(li, 'lr-mark').setAttribute('aria-hidden', 'true');
      span(li, 'lr-text', p.text);
      span(li, 'lr-tag', LIVE.planTag[p.status]);
    });
    return c.col;
  }
  function lrAgents(r, agents, moving) {
    var c = lrCol('Sub-agents');
    if (!agents) { add(c.col, el('p', 'empty', 'no sub-agent data')); return c.col; }
    var running = agents.filter(function (a) { return a.status === 'running'; }).length;
    span(c.head, 'lr-count', agents.length + ' spawned · ' + running + ' running');
    if (!agents.length) { add(c.col, el('div', 'lr-alone', 'working alone so far')); return c.col; }
    agents.forEach(function (a, i) {
      var card = add(c.col, el('div', 'lr-agent'));
      var effect = {running: moving ? 'scan' : 'off', finished: 'off', failed: 'static'}[a.status];
      add(card, PX.strip('live-agent-' + r.job + '-' + i, {effect: effect, tint: a.status === 'failed' ? '#f7768e' : 'bot',
                                                           bot: r.recipient, seed: 80 + i, still: !moving, cell: 4, done: a.status === 'finished', height: 8}));
      var body = add(card, el('div', 'lr-agent-body'));
      span(body, 'lr-kind', a.kind);
      span(body, 'lr-desc', a.description);
      span(body, 'lr-status ' + a.status, a.status);
    });
    return c.col;
  }
  function liveRun(r, queued) {
    if (queued) return el('p', 'empty', 'not started yet');
    var t = r.telemetry;
    var mode = liveMode(t);
    if (mode === 'none') return el('p', 'empty', 'no telemetry');
    var box = el('div', 'lr');
    if (PX.table.bots.hasOwnProperty(r.recipient)) box.setAttribute('style', '--lr:var(--' + r.recipient + ')');
    if (mode === 'live') {
      var moving = PX.table.actions.hasOwnProperty(PX.state(r));
      add(box, lrHead(r, t, moving));
      var cols = add(box, el('div', 'lr-cols'));
      if (t.plan !== null && t.plan !== undefined) add(cols, lrPlan(r, t.plan, moving));
      add(cols, lrAgents(r, t.subagents, moving));
    }
    add(box, lrTiles(t));
    return box;
  }
  function liverun(r, started) {
    var sec = jdPanel(null, 'Live run', r.recipient + ' · job ' + r.job);
    sec.setAttribute('id', 'liverun-' + anchor(r.job));
    add(sec, liveRun(r, !started));
    return sec;
  }
  function legendItem(parent, cls, tint, text, bg) {
    var s = span(parent, null);
    var i = add(s, el('i', cls || null));
    if (tint) i.setAttribute('style', '--m:' + tint);
    if (bg) i.setAttribute('style', 'background:' + bg);
    s.appendChild(document.createTextNode(text));
    return s;
  }
  function nowDur(m, node) {
    if (m.state_age_seconds === null) node.textContent = 'in progress';
    else ticking(node, m.state_age_seconds, '', ' so far');
    return node;
  }
  function stepName(v, m) {
    var took;
    if (v.current) took = m.state_age_seconds === null ? 'in progress' : dur(m.state_age_seconds) + ' so far';
    else took = v.duration_seconds === null || v.duration_seconds === undefined ? 'time unknown' : dur(v.duration_seconds);
    return 'Step ' + (v.index + 1) + ' of ' + m.visits.length + ': ' + v.state + ', ' + v.who + ', ' + took;
  }
  function legendWho(m) {
    var seen = {};
    m.visits.forEach(function (v) { seen[JOURNEY.tint.hasOwnProperty(v.who) ? v.who : 'script'] = true; });
    return DETAIL.who.filter(function (w) { return seen[w]; });
  }
  function reasonOf(v) {
    return v.reason !== null && v.reason !== undefined ? v.reason : (v.index === 0 ? DETAIL.first : '');
  }
  function fillCaption(cap, m, i) {
    cap.replaceChildren();
    var v = m.visits[i];
    var tag = v && v.current ? m.ended || 'now' : '';
    cap.className = 'jd-cap' + (tag ? ' ' + tag : '');
    if (!v) return;
    if (m.ended) span(cap, 'end', endText(m)).setAttribute('style', '--m:' + DETAIL.endTint[m.ended]);
    span(cap, 'idx', pad(v.index + 1) + ' of ' + m.visits.length);
    span(cap, 'st', v.state + (tag ? ' · ' + tag : ''));
    span(cap, 'who', v.who).setAttribute('style', '--m:' + (JOURNEY.tint[v.who] || JOURNEY.tint.script));
    var d = span(cap, 'dur');
    if (v.current && m.ended === 'shipped') d.textContent = v.started_at === null || v.started_at === undefined ? '—' : 'at ' + etClock(v.started_at);
    else if (v.current && m.ended === 'stopped') d.textContent = m.state_age_seconds === null ? '—' : 'for ' + dur(m.state_age_seconds);
    else if (v.current) nowDur(m, d);
    else d.textContent = v.duration_seconds === null || v.duration_seconds === undefined ? '—' : 'took ' + dur(v.duration_seconds);
    span(cap, 'note', reasonOf(v));
  }
  function journeyPanel(r, m, hits, scales, scale) {
    var sec = jdPanel(null, 'Journey', 'this ticket through time · pick a step for its reason');
    var box = add(sec, el('div', 'jd-box'));
    var canvas = add(box, el('canvas', 'jd-journey'));
    canvas.setAttribute('width', '1000');
    canvas.setAttribute('height', '336');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'Journey: ' + (m.visits.map(function (v) { return v.state; }).join(' → ') || 'no visits') +
      (m.ended ? '; ended: ' + endText(m) : '; now ' + (m.state || 'unknown')));
    var ol = add(box, el('ol', 'journey'));
    m.visits.forEach(function (v) {
      var li = add(ol, el('li'));
      var b = add(li, el('button'));
      hits.push(b);
      b.setAttribute('type', 'button');
      b.setAttribute('data-visit', String(v.index));
      b.setAttribute('data-state', v.state);
      b.setAttribute('aria-label', stepName(v, m));
      b.setAttribute('aria-pressed', v.index === m.selected ? 'true' : 'false');
      span(li, 'step', stepName(v, m));
    });
    var cap = add(sec, el('div', 'jd-cap'));
    cap.setAttribute('aria-live', 'polite');
    fillCaption(cap, m, m.selected);
    var ctl = add(sec, el('div', 'jd-ctl'));
    var axis = add(ctl, el('div', 'axis'));
    axis.setAttribute('role', 'group');
    axis.setAttribute('aria-label', 'Time axis');
    function ctlButton(parent, text, attr, val, pressed) {
      var b = add(parent, el('button', null, text));
      b.setAttribute('type', 'button');
      b.setAttribute(attr, val);
      if (pressed !== null) { b.setAttribute('aria-pressed', pressed ? 'true' : 'false'); scales.push(b); }
      return b;
    }
    ctlButton(axis, 'even steps', 'data-scale', 'even', scale === 'even');
    if (m.all_durations_known) ctlButton(axis, 'to scale', 'data-scale', 'scale', scale === 'scale');
    if (m.ended !== 'stopped') ctlButton(ctl, 'replay', 'data-replay', '1', null);
    span(ctl, 'gap');
    legendWho(m).forEach(function (w) { legendItem(ctl, null, JOURNEY.tint[w], w).className = 'key'; });
    legendItem(ctl, null, DETAIL.stop, 'stopped').className = 'key';
    legendItem(ctl, 'off', DETAIL.hop, 'off-chart hop').className = 'key';
    return {sec: sec, canvas: canvas, cap: cap};
  }
  function nextPanel(m) {
    var sub = m.terminal || !m.state ? 'no way out' : m.next.length + ' ways out of ' + m.state;
    var sec = jdPanel(null, 'Can come next', sub);
    if (m.terminal) { add(sec, el('p', 'empty', 'no way out: this ticket is finished')); return sec; }
    if (!m.state) { add(sec, el('p', 'empty', 'state not recorded')); return sec; }
    var grid = add(sec, el('div', 'jd-next'));
    m.next.forEach(function (n, i) {
      var tag = '';
      if (n.kind === 'stop') tag = n.count ? 'stop state · been here ' + (n.count === 1 ? 'once' : n.count + ' times') : 'stop state · not yet';
      else if (n.kind === 'human') tag = 'needs a human';
      else if (i === 0) tag = 'the normal way on';
      var card = add(grid, el('div', 'card ' + n.kind));
      var head = add(card, el('div', 'head'));
      span(head, 'to', n.to);
      if (tag) span(head, 'tag', tag);
      span(card, 'why', n.label);
    });
    return sec;
  }
  function chartPanel(id, m, chart) {
    var box = el('details', 'jd-chart');
    var key = 'full-chart:' + id;
    if (!chart) { add(box, el('summary', null, 'Full state chart')); add(box, el('p', 'empty', 'statechart unavailable')); return {sec: box}; }
    box.setAttribute('data-key', key);
    box.open = !!openDetails[key];
    var sum = add(box, el('summary', null, 'Full state chart '));
    span(sum, 'count', DETAIL.chartSub.replace('{n}', String(chart.states.length)));
    var wrap = add(box, el('div', 'jd-box'));
    var canvas = add(wrap, el('canvas', 'jd-flowmap'));
    canvas.setAttribute('width', '1000');
    canvas.setAttribute('height', String(FLOWMAP ? FLOWMAP.grid.h * FLOWMAP.grid.cell : 416));
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'Full state chart; path: ' + m.visits.map(function (v) { return v.state; }).join(' → '));
    var legend = add(box, el('div', 'jd-legend'));
    legendWho(m).forEach(function (w) { legendItem(legend, 'line', null, 'path taken · ' + w, JOURNEY.tint[w]); });
    legendItem(legend, 'line', null, 'visited', '#5386a6');
    legendItem(legend, 'off', JOURNEY.tint.script, 'off-chart jump');
    DETAIL.chartKeys[m.ended || 'live'].forEach(function (k) { legendItem(legend, k[0], null, k[2], k[1]); });
    box.addEventListener('toggle', function () { openDetails[key] = box.open; });
    return {sec: box, canvas: canvas};
  }

  function selection(key, r, m) {
    var s = SEL[key];
    var beatAt = typeof r.heartbeat_at === 'number' ? r.heartbeat_at : null;
    if (!s) {
      s = SEL[key] = {selected: m.selected, picked: false, tPlay: Date.now(), beatAt: beatAt, beatStart: null,
                        flashAt: null, scale: 'even'};
      return s;
    }
    if (!s.picked || s.selected >= m.visits.length) { s.selected = m.selected; s.picked = false; }
    if (beatAt !== null && beatAt !== s.beatAt) s.beatStart = Date.now();
    s.beatAt = beatAt;
    if (s.scale === 'scale' && !m.all_durations_known) s.scale = 'even';
    return s;
  }
  function pick(j, i, fromChart) {
    var s = SEL[j.key];
    s.selected = i;
    s.picked = true;
    s.flashAt = Date.now();
    [j.logButtons, j.hitButtons].forEach(function (buttons) {
      buttons.forEach(function (b) {
        var on = Number(b.getAttribute('data-visit')) === i;
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (on && fromChart && buttons === j.logButtons && b.scrollIntoView) b.scrollIntoView({block: 'nearest'});
      });
    });
    fillCaption(j.cap, j.m, i);
    drawOne(j, Date.now());
  }
  function placeHits(j) {
    var bars = journeyBars(j.m, SEL[j.key].scale);
    var buttons = j.hitButtons;
    for (var k = 0; k < buttons.length && k < bars.length; k++) {
      var b = bars[k], st = buttons[k].style;
      st.left = b.x + 'px'; st.top = b.y + 'px'; st.width = Math.max(4, b.w) + 'px'; st.height = Math.max(4, b.h) + 'px';
    }
  }
  function drawOne(j, now) {
    var s = SEL[j.key];
    if (j.canvas.getContext) {
      var ctx = j.canvas.getContext('2d');
      if (ctx) {
        var flash = s.flashAt === null || reducedMotion ? 0 : Math.max(0, 1 - (now - s.flashAt) / 260);
        var beat = s.beatStart === null || reducedMotion ? -1 : (now - s.beatStart) / 1000;
        drawJourney(ctx, j.m, {t: reducedMotion ? 1e6 : (now - s.tPlay) / 1000, scale: s.scale, selected: s.selected,
          flash: flash, beat: beat >= 0 && beat <= 1 ? beat : -1, reduced: reducedMotion, still: j.still,
          startClock: j.startClock, nowClock: j.nowClock, endClock: j.endClock});
      }
    }
    if (j.flowCanvas && j.flow.open && j.flowCanvas.getContext && FLOWMAP) {
      var fctx = j.flowCanvas.getContext('2d');
      if (fctx) drawFlowMap(fctx, FLOWMAP, flowModel(j.m), {t: now / 1000, reduced: reducedMotion || j.still || !!j.m.ended,
                                                            tint: j.m.ended === 'shipped' ? DETAIL.endTint.shipped : null});
    }
  }
  function journeyFrame() {
    journeys = journeys.filter(function (j) { return j.canvas.isConnected; });
    if (!journeys.length) return false;
    var now = Date.now();
    journeys.forEach(function (j) { drawOne(j, now); });
    return !reducedMotion;
  }

  var SECTIONS = {
    liverun: function (c) { return liverun(c.r, c.started); },
    journey: function (c) { c.jp = journeyPanel(c.r, c.m, c.hitButtons, c.scales, c.s.scale); return c.jp.sec; },
    next: function (c) { return nextPanel(c.m); },
    chart: function (c) { c.cp = chartPanel(c.key, c.m, c.chart); return c.cp.sec; }
  };
  function detailContext(key, r, d, started) {
    var chart = d.statechart || null;
    var m = detailModel(r, chart);
    var s = selection(key, r, m);
    m.selected = s.selected;
    return {key: key, r: r, m: m, s: s, chart: chart, started: started, motion: motionOf(r, m),
            generated: Number(d.generated_at_epoch) || Date.now() / 1000, logButtons: [], hitButtons: [], scales: []};
  }
  function detailCols(art, c, sections) {
    var s = c.s, scales = c.scales, hitButtons = c.hitButtons;
    var cols = add(art, el('div', 'jd-cols'));
    add(cols, runlog(c.r, c.m, c.logButtons));
    var right = add(cols, el('div', 'jd-right'));
    sections.forEach(function (name) { add(right, SECTIONS[name](c)); });
    var jp = c.jp, cp = c.cp, start = firstStart(c.m);
    var endAt = c.m.end ? c.m.end.at : null;
    var j = {key: c.key, r: c.r, m: c.m, still: !c.m.ended && (!!c.still || holdsStill(c.motion)), logButtons: c.logButtons,
             hitButtons: hitButtons, canvas: jp.canvas, cap: jp.cap, flow: cp.sec, flowCanvas: cp.canvas,
             startClock: start === null ? '' : journeyClock(start), nowClock: journeyClock(c.generated),
             endClock: endAt === null || endAt === undefined ? '' : journeyClock(endAt)};
    art.addEventListener('click', function (ev) {
      var t = ev.target.closest('button');
      if (!t) return;
      if (t.hasAttribute('data-visit')) pick(j, Number(t.getAttribute('data-visit')), hitButtons.indexOf(t) >= 0);
      else if (t.hasAttribute('data-scale')) {
        s.scale = t.getAttribute('data-scale');
        s.tPlay = Date.now();
        scales.forEach(function (b) { b.setAttribute('aria-pressed', b === t ? 'true' : 'false'); });
        placeHits(j);
      } else if (t.hasAttribute('data-replay')) s.tPlay = Date.now();
      else return;
      drawOne(j, Date.now());
    });
    if (cp.canvas) cp.sec.addEventListener('toggle', function () { drawOne(j, Date.now()); });
    placeHits(j);
    journeys.push(j);
    PX.onFrame(journeyFrame);
  }

  function jobArticle(r, state, d) {
    var running = state === 'running';
    var c = detailContext(r.job, r, d, running);
    var art = el('article', 'jobpage');
    art.setAttribute('id', anchor(r.job));
    add(art, crumb(jobLabel(r), 'job ' + r.job));
    var strip = PX.strip('job-hero-' + r.job, {state: c.motion, bot: r.recipient, cell: 4, height: 40, seed: 2});
    add(art, hero(c, strip, jobLabel(r), function (title) {
      var kind = jobKind(r);
      if (kind) span(title, 'kind', kind);
      var device = running ? (r.device ? 'on ' + r.device : '') : 'queued';
      if (device) span(title, 'device', device);
      troubleChips(title, r);
      addGh(title, r);
    }, ticketRow(d, r), DETAIL.jobTiles[state]));
    detailCols(art, c, DETAIL.sections);
    return art;
  }

  function ticketRow(d, r) {
    return (d.tickets || []).filter(function (t) { return threadKey(t) === threadKey(r); })[0] || null;
  }
  var PICK = {
    running: function (rows) { return rows.reduce(function (a, b) { return (b.elapsed_seconds || 0) < (a.elapsed_seconds || 0) ? b : a; }); },
    queued: function (rows) { return rows[0]; }
  };
  function recordRow(rec, t) {
    var r = {repo: rec.repo, ticket_id: rec.ticket_id, flow: rec};
    DETAIL.rowFromTicket.forEach(function (k) { if (t && k in t) r[k] = t[k]; });
    return r;
  }
  function sameTicket(address) {
    var cut = address.indexOf('#');
    var repo = (cut < 0 ? address : address.slice(0, cut)).toLowerCase(), id = cut < 0 ? '' : address.slice(cut + 1);
    return function (r) { return String(r.repo).toLowerCase() === repo && String(r.ticket_id) === id; };
  }
  function resolveTicket(d, address, records) {
    var rules = [function (r) { return ticketAddress(r) === address; }, sameTicket(address)];
    for (var i = 0; i < rules.length; i++) {
      for (var k = 0; k < DETAIL.liveKinds.length; k++) {
        var kind = DETAIL.liveKinds[k];
        var rows = (d[kind] || []).filter(rules[i]);
        if (!rows.length) continue;
        var row = PICK[kind](rows);
        return {kind: kind, address: ticketAddress(row), row: row, ticket: ticketRow(d, row)};
      }
    }
    if (!records) return null;
    var key = Object.prototype.hasOwnProperty.call(records, address) ? address
      : Object.keys(records).sort().filter(function (k) { return rules[1](records[k]); })[0];
    if (key === undefined) return null;
    var rec = records[key], t = ticketRow(d, rec);
    var flag = DETAIL.recordFlags.filter(function (f) { return rec[f]; })[0];
    return {kind: flag || 'idle', address: key, row: recordRow(rec, t), ticket: t};
  }
  function ticketArticle(found, d) {
    var spec = DETAIL.kinds[found.kind];
    var r = found.row;
    var c = detailContext(found.address, r, d, spec.started);
    c.still = spec.still;
    c.m.now = c.generated;
    var art = el('article', 'jobpage ticketpage');
    art.setAttribute('data-ticket', found.address);
    var start = firstStart(c.m);
    add(art, crumb(tileLabel(r), start === null ? null : 'started ' + etClock(start)));
    var strip = PX.strip('ticket-hero-' + found.address, Object.assign({state: c.motion, bot: r.recipient, still: spec.still,
                                                                        cell: 4, height: 40, seed: 2}, spec.strip));
    add(art, hero(c, strip, tileLabel(r), function (title) {
      trackerSpan(title, r, spec.tail);
      troubleChips(title, r);
    }, found.ticket, spec.tiles));
    detailCols(art, c, spec.sections);
    return art;
  }
  function ticketView(d, address, records) {
    var found = address === null ? null : resolveTicket(d, address, records);
    if (found) return ticketArticle(found, d);
    var panel = DETAIL.panels[address === null ? 'none' : records && !recordPending(d, address) ? 'missing' : 'loading'];
    var sec = el('section', 'panel');
    add(add(sec, el('h1')), el('span', null, panel[0]));
    var p = add(sec, el('p', 'empty'));
    if (panel[1]) p.appendChild(document.createTextNode(panel[1].replace('{}', function () { return address; })));
    p.appendChild(document.createTextNode('Pick a ticket on the '));
    add(p, link(null, 'job.html')).textContent = 'Tickets board';
    p.appendChild(document.createTextNode('.'));
    return sec;
  }

  function renderJobView(d) {
    var root = byId('jobview');
    if (!root) return;
    if (!GH.started) { GH.started = true; ghStart(); }
    root.replaceChildren();
    var jobs = activeJobs(d);
    var hash = String(location.hash || '');
    if (hash.indexOf('#job-') === 0) {
      jobs = jobs.filter(function (j) { return '#' + anchor(j.row.job) === hash; });
      if (!jobs.length) {
        var gone = add(root, el('section', 'panel'));
        add(add(gone, el('h2')), el('span', null, hash.slice(1)));
        add(gone, el('p', 'empty', 'That job is no longer running or queued. Pick one from the list above.'));
        return;
      }
    }
    if (!jobs.length) { add(root, el('p', 'empty', 'no running or queued jobs')); return; }
    jobs.forEach(function (j) { add(root, jobArticle(j.row, j.state, d)); });
  }

  var RECORDS = {tickets: null, epoch: null, asked: null, poll: null, busy: false};
  function recordPending(d, address) {
    return RECORDS.tickets !== null && RECORDS.epoch < d.generated_at_epoch
      && (d.tickets || []).some(sameTicket(address)) && !resolveTicket(d, address, RECORDS.tickets);
  }
  function loadRecords(d, address) {
    var epoch = d.generated_at_epoch;
    var retry = address !== null && RECORDS.poll !== accepted && recordPending(d, address);
    if (RECORDS.busy || (!retry && (RECORDS.asked === epoch || (RECORDS.tickets && RECORDS.epoch >= epoch)))) return;
    RECORDS.busy = true;
    RECORDS.asked = epoch;
    RECORDS.poll = accepted;
    fetch('tickets.json', {cache: 'no-store', credentials: 'same-origin'}).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (data) {
      RECORDS.busy = false;
      if (!data || !data.tickets || typeof data.tickets !== 'object') return;
      RECORDS.tickets = data.tickets;
      RECORDS.epoch = data.generated_at_epoch;
      if (latest) render(latest);
    }, function () { RECORDS.busy = false; });
  }
  function renderTicketView(d) {
    if (!GH.started) { GH.started = true; ghStart(); }
    var address = hashAddress();
    loadRecords(d, address);
    tickers.push({run: function () { loadRecords(latest, hashAddress()); }});
    byId('ticketview').replaceChildren(ticketView(d, address, RECORDS.tickets));
  }
"""
_DETAIL = {"VERBS": VERBS, "HB": HEARTBEAT_TONE, "CHARTSUB": CHART_SUB, "TRACKERS": TRACKER_NAMES,
           "SECTIONS": SECTION_ORDER, "JOBTILES": JOB_TILES, "KINDS": TICKET_KINDS, "LIVEKINDS": LIVE_KINDS,
           "RECORDFLAGS": RECORD_FLAGS, "ROWFROMTICKET": ROW_FROM_TICKET, "PANELS": PANELS,
           "CHARTKEYS": CHART_KEYS, "ENDTINT": END_TINT}
_SCRIPT = re.sub(r"__([A-Z]+)__", lambda m: json.dumps(_DETAIL[m.group(1)]), _SCRIPT)
SCRIPT = journey_canvas.SCRIPT + FLOWMAP_SCRIPT + _SCRIPT
