"""job.html#job-<id>: the ticket detail page (Job.dc.html). Breadcrumb, hero, Run
log, Live run slot, Journey, Can come next and the collapsed Full state chart.
The Run log and the Journey render from one visit list and share one selection."""

from __future__ import annotations

import json

from lib.dashboard_flow import FLOWMAP_SCRIPT, ticket_phases
from lib.dashboard_telemetry import job_block
from lib.dashboard_pages.common import anchor, _bot, _chip, duration, _e, _job_kind, _job_label, _span, _state_chip
from lib.dashboard_pages import journey_canvas
from lib.dashboard_pages.journey_canvas import TINT
from lib.dashboard_pages.tickets import WAITING, _active_jobs, _px


VERBS = {
    "plan": ("Pondering", "Sketching", "Scheming"),
    "build": ("Assembling", "Stacking", "Forging"),
    "review": ("Scrutinizing", "Combing", "Squinting"),
    "fix": ("Patching", "Mending", "Tinkering"),
}
NEXT_ORDER = {"normal": 0, "human": 1, "stop": 2}
FIRST_REASON = "ticket arrives"
STOP_TINT = journey_canvas.STOP
CHART_SUB = "· {n} states, with this ticket's path drawn on the whole machine"


CSS = """\
#jobview{display:grid;gap:16px}
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
.jd-hero .device{font:12px var(--mono);color:var(--muted)}
.jd-hero .line{margin:0;font-size:15px;line-height:1.5;color:var(--text-2)}
.jd-hero .line b{font-family:var(--mono);font-weight:600;color:var(--accent)}
.jd-verb{display:flex;align-items:center;gap:8px;font:14px/1.3 var(--mono);color:var(--text)}
.jd-verb .glyph{display:grid;grid-template-columns:5px 5px;grid-template-rows:5px 5px;gap:1px}
.jd-verb .glyph span{background:var(--line-hi)}.jd-verb .glyph span.lit{background:var(--px)}
.jd-verb .phase{padding:0 5px;border:1px solid var(--line-hi);font-size:10px;line-height:1.6;letter-spacing:.08em;color:var(--muted)}
.jd-tiles{flex:1 1 420px;min-width:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(120px,100%),1fr));gap:8px}
.jd-tile{padding:8px 10px;background:var(--bg);box-shadow:var(--elev)}
.jd-tile b{display:flex;align-items:center;gap:8px;font:600 18px/1.25 var(--mono);font-variant-numeric:tabular-nums;color:var(--text)}
.jd-tile b.ok{color:var(--ok)}
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
.runlog button{display:flex;gap:10px;width:100%;box-sizing:border-box;padding:8px;border:0;text-align:left;font-family:inherit;cursor:pointer;background:transparent;color:var(--text)}
.runlog button[aria-pressed="true"]{background:var(--sel);box-shadow:inset 0 0 0 1px var(--accent)}
.runlog .num{flex:0 0 20px;font-size:12px;line-height:1.5;color:var(--muted)}
.runlog button[aria-pressed="true"] .num{color:var(--accent)}
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
.jd-ctl{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:10px;font:12px/1.5 var(--mono);color:var(--muted)}
.jd-ctl button{padding:2px 8px;border:1px solid var(--line-hi);background:transparent;color:var(--text-2);font:inherit;cursor:pointer}
.jd-ctl button[aria-pressed="true"]{border-color:var(--accent);color:var(--accent)}
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
"""


def _who_tint(who: object) -> str:
    return TINT.get(str(who), TINT["script"])


def _detail_model(row: dict, chart: dict | None) -> dict:
    flow = row.get("flow") or {}
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
    }


def _round(row: dict) -> tuple[int, int] | None:
    parts = str(row.get("round") or "").split("/")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def _crumb(row: dict) -> str:
    return (
        '<nav class="jd-crumb" aria-label="Breadcrumb"><a href="job.html">Tickets</a>'
        '<span aria-hidden="true">/</span>'
        f'{_span("here", _job_label(row))}{_span("job", "job " + str(row["job"]))}</nav>'
    )


def _hero_line(row: dict, model: dict) -> str:
    parts = []
    if model["state"]:
        parts.append(f"now <b>{_e(model['state'])}</b>")
    rnd = _round(row)
    if rnd:
        parts.append(f"round {rnd[0]} of {rnd[1]}")
    if model["state"] and model["state_age_seconds"] is not None:
        parts.append(f"{duration(model['state_age_seconds'])} in {_e(model['state'])}")
    return f'<p class="line">{" · ".join(parts) or "state not recorded"}</p>'


def _motion(row: dict, model: dict) -> str:
    return "waiting" if model["state"] in WAITING else _px().px_state(row)


def _verb(row: dict, motion: str) -> str:
    action = str(row.get("action") or "")
    glyph = '<span class="glyph" aria-hidden="true"><span></span><span></span><span class="lit"></span><span></span></span>'
    word = _span("verb", VERBS[motion][0] + "…") if motion in _px().PX_ACTIONS else ""
    return f'<div class="jd-verb">{glyph}{word}{_span("phase", action.upper())}</div>'


def _tile(value: str, label: str, cls: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    return f'<div class="jd-tile"><b{attr}>{value}</b>{_span(None, label)}</div>'


def _tiles(row: dict, model: dict, running: bool) -> str:
    if running:
        first = _tile(duration(row.get("elapsed_seconds")), "T+ on this device")
        hb = row.get("heartbeat_age_seconds")
        beat = _tile("not started" if hb is None else f"{duration(hb)} ago", "last heartbeat", "ok")
    else:
        first = _tile(duration(row.get("age_seconds")), "waiting")
        beat = _tile("not started", "last heartbeat")
    rnd = _round(row)
    if rnd:
        pips = "".join(
            f'<span class="{"done" if i < rnd[0] - 1 else "now" if i == rnd[0] - 1 else ""}"></span>'
            for i in range(rnd[1])
        ).replace(' class=""', "")
        third = _tile(f'{rnd[0]} / {rnd[1]}<span class="jd-pips" aria-hidden="true">{pips}</span>', "revise round")
    else:
        third = _tile("no round", "revise round")
    hops = _tile(f"{model['hops']} hops", f"{model['off_chart_count']} off-chart")
    return f'<div class="jd-tiles">{first}{beat}{third}{hops}</div>'


def _hero(row: dict, model: dict, running: bool) -> str:
    motion = _motion(row, model)
    key = f"job-hero-{row['job']}"
    strip = _px().px_strip(key, state=motion, bot=row.get("recipient"), cell=4, height=40, seed=2)
    kind = _job_kind(row)
    device = f"on {row['device']}" if running and row.get("device") else "queued" if not running else ""
    title = (
        f'<div class="title"><h1>{_e(_job_label(row))}</h1>{_bot(row.get("recipient"))}'
        + (_span("kind", kind) if kind else "")
        + (_span("device", device) if device else "")
        + _state_chip(row)
        + (_chip("OVERDUE", "bad") if row.get("overdue") else "")
        + (_chip(f"retried {row['retries']}x") if row.get("retries") else "")
        + "</div>"
    )
    return (
        f'<section class="jd-hero">{strip}<div class="body"><div class="who">{title}'
        f"{_hero_line(row, model)}{_verb(row, motion)}</div>{_tiles(row, model, running)}</div></section>"
    )


def _visit_dur(visit: dict) -> str:
    if visit["current"]:
        return "in progress"
    return "—" if visit["duration_seconds"] is None else duration(visit["duration_seconds"])


def _runlog_row(visit: dict, selected: int) -> str:
    tint = _who_tint(visit["who"])
    cls = " ".join(c for c in ("now" if visit["current"] else "", "stop" if visit["stop"] else "") if c)
    mark_tint = STOP_TINT if visit["stop"] else tint
    reason = visit["reason"] if visit["reason"] is not None else (FIRST_REASON if visit["index"] == 0 else "")
    name = visit["state"] + (" · now" if visit["current"] else "")
    num = f"{visit['index'] + 1:02d}"
    return (
        f'<li><button type="button" data-visit="{visit["index"]}" data-state="{_e(visit["state"])}" '
        f'aria-pressed="{"true" if visit["index"] == selected else "false"}"'
        + (f' class="{cls}"' if cls else "")
        + f' style="--m:{tint}">'
        f'{_span("num", num)}'
        f'<span class="mark{" off" if visit["off_chart"] else ""}" aria-hidden="true" style="--m:{mark_tint}"></span>'
        f'<span class="main"><span class="top">{_span("name", name)}{_span("who", visit["who"])}'
        f'{_span("dur", _visit_dur(visit))}'
        + (_span("offchip", "off-chart") if visit["off_chart"] else "")
        + f'</span>{_span("note", reason)}</span></button></li>'
    )


def _runlog(model: dict) -> str:
    rows = "".join(_runlog_row(v, model["selected"]) for v in model["visits"])
    count = f"{len(model['visits'])} visits · oldest first"
    body = f'<ol class="runlog">{rows}</ol>' if rows else '<p class="empty">no visits recorded</p>'
    return (
        f'<section class="jd-panel jd-runlog"><h2>{_span("t", "Run log")}{_span("count", count)}</h2>{body}'
        '<p class="jd-foot">Pick a step here or on the chart; both follow.</p></section>'
    )


def _liverun(row: dict) -> str:
    sub = f"{row.get('recipient')} · job {row['job']}"
    return (
        f'<section class="jd-panel" id="liverun-{_e(anchor(row["job"]))}">'
        f'<h2>{_span("t", "Live run")}{_span("count", sub)}</h2>{job_block(row.get("telemetry"))}</section>'
    )


def _journey_label(model: dict) -> str:
    path = " → ".join(v["state"] for v in model["visits"]) or "no visits"
    return f"Journey: {path}; now {model['state'] or 'unknown'}"


_LEGEND_WHO = ("script", "claude", "codex", "cursor", "grok")


def _journey(row: dict, model: dict) -> str:
    hits = "".join(
        f'<li><button type="button" data-visit="{v["index"]}" data-state="{_e(v["state"])}" '
        f'aria-label="{_e(v["state"])}" aria-pressed="{"true" if v["index"] == model["selected"] else "false"}">'
        "</button></li>"
        for v in model["visits"]
    )
    scale = (
        '<button type="button" data-scale="scale" aria-pressed="false">to scale</button>'
        if model["all_durations_known"]
        else ""
    )
    legend = "".join(f'<span><i style="--m:{TINT[w]}"></i>{w}</span>' for w in _LEGEND_WHO)
    legend += f'<span><i style="--m:{STOP_TINT}"></i>stopped</span>'
    legend += f'<span><i class="off" style="--m:{TINT["script"]}"></i>off-chart hop</span>'
    return (
        f'<section class="jd-panel"><h2>{_span("t", "Journey")}'
        f'{_span("count", "this ticket through time · pick a step for its reason")}</h2>'
        f'<div class="jd-box"><canvas class="jd-journey" width="1000" height="336" role="img" '
        f'aria-label="{_e(_journey_label(model))}"></canvas><ol class="journey">{hits}</ol></div>'
        '<div class="jd-ctl"><button type="button" data-scale="even" aria-pressed="true">even steps</button>'
        f'{scale}<button type="button" data-replay="1">replay</button></div>'
        f'<div class="jd-legend">{legend}</div></section>'
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


def _chart(row: dict, model: dict, chart: dict | None) -> str:
    if chart is None:
        return '<details class="jd-chart"><summary>Full state chart</summary><p class="empty">statechart unavailable</p></details>'
    n = len(chart["states"])
    legend = "".join(f'<span><i class="line" style="background:{TINT[w]}"></i>path taken · {w}</span>' for w in _LEGEND_WHO)
    legend += '<span><i class="line" style="background:#5386a6"></i>visited</span>'
    legend += f'<span><i class="off" style="--m:{TINT["script"]}"></i>off-chart jump</span>'
    legend += '<span><i class="now"></i>now</span><span><i class="next"></i>can come next</span>'
    return (
        f'<details class="jd-chart" data-key="full-chart:{_e(row["job"])}"><summary>Full state chart '
        f'{_span("count", CHART_SUB.format(n=n))}</summary>'
        f'<div class="jd-box"><canvas class="jd-flowmap" width="1000" height="352" role="img" '
        f'aria-label="{_e("Full state chart; path: " + " → ".join(v["state"] for v in model["visits"]))}"></canvas></div>'
        f'<div class="jd-legend">{legend}</div></details>'
    )


def _job_article(row: dict, state: str, chart: dict | None) -> str:
    model = _detail_model(row, chart)
    running = state == "running"
    return (
        f'<article class="jobpage" id="{anchor(row["job"])}">'
        + _crumb(row)
        + _hero(row, model, running)
        + '<div class="jd-cols">'
        + _runlog(model)
        + '<div class="jd-right">'
        + _liverun(row)
        + _journey(row, model)
        + _next(model)
        + _chart(row, model, chart)
        + "</div></div></article>"
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


_SCRIPT = r"""
  var DETAIL = {verbs: __VERBS__, order: {normal: 0, human: 1, stop: 2},
                first: 'ticket arrives', stop: '#f7768e', who: ['script', 'claude', 'codex', 'cursor', 'grok'],
                chartSub: __CHARTSUB__};
  var SEL = {};
  var journeys = [];
  var reducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  function whoTint(w) { return JOURNEY.tint[w] || JOURNEY.tint.script; }
  function detailModel(r, chart) {
    var flow = r.flow || {};
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
      state_age_seconds: flow.state_age_seconds === undefined ? null : flow.state_age_seconds
    };
  }
  function roundOf(r) {
    var m = /^\s*(\d+)\s*\/\s*(\d+)\s*$/.exec(String(r.round === null || r.round === undefined ? '' : r.round));
    return m ? [Number(m[1]), Number(m[2])] : null;
  }
  function span(parent, cls, text) { return add(parent, el('span', cls, text)); }
  function crumb(r) {
    var nav = el('nav', 'jd-crumb');
    nav.setAttribute('aria-label', 'Breadcrumb');
    var a = add(nav, link(null, 'job.html'));
    a.textContent = 'Tickets';
    span(nav, null, '/').setAttribute('aria-hidden', 'true');
    span(nav, 'here', jobLabel(r));
    span(nav, 'job', 'job ' + r.job);
    return nav;
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
  function hero(r, m, running, motion) {
    var sec = el('section', 'jd-hero');
    var action = String(r.action || '');
    add(sec, PX.strip('job-hero-' + r.job, {state: motion, bot: r.recipient, cell: 4, height: 40, seed: 2}));
    var body = add(sec, el('div', 'body'));
    var who = add(body, el('div', 'who'));
    var title = add(who, el('div', 'title'));
    add(title, el('h1', null, jobLabel(r)));
    add(title, bot(r.recipient));
    var kind = jobKind(r);
    if (kind) span(title, 'kind', kind);
    var device = running ? (r.device ? 'on ' + r.device : '') : 'queued';
    if (device) span(title, 'device', device);
    var jobState = stateChip(r);
    if (jobState) add(title, jobState);
    if (r.overdue) add(title, chip('OVERDUE', 'bad'));
    if (r.retries) add(title, chip('retried ' + r.retries + 'x'));
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
    span(verb, 'phase', action.toUpperCase());
    var tiles = add(body, el('div', 'jd-tiles'));
    if (running) {
      ticking(tile(tiles, '', 'T+ on this device'), r.elapsed_seconds);
      var hb = r.heartbeat_age_seconds;
      if (hb === null || hb === undefined) tile(tiles, 'not started', 'last heartbeat', 'ok');
      else ticking(tile(tiles, '', 'last heartbeat', 'ok'), hb, '', ' ago');
    } else {
      ticking(tile(tiles, '', 'waiting'), r.age_seconds);
      tile(tiles, 'not started', 'last heartbeat');
    }
    if (rnd) {
      var b = tile(tiles, rnd[0] + ' / ' + rnd[1], 'revise round');
      var pips = add(b, el('span', 'jd-pips'));
      pips.setAttribute('aria-hidden', 'true');
      for (var i = 0; i < rnd[1]; i++) span(pips, i < rnd[0] - 1 ? 'done' : i === rnd[0] - 1 ? 'now' : null);
    } else tile(tiles, 'no round', 'revise round');
    tile(tiles, m.hops + ' hops', m.off_chart_count + ' off-chart');
    return sec;
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
        var btn = add(add(ol, el('li')), el('button', [v.current ? 'now' : '', v.stop ? 'stop' : ''].filter(Boolean).join(' ') || null));
        buttons.push(btn);
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-visit', String(v.index));
        btn.setAttribute('data-state', v.state);
        btn.setAttribute('aria-pressed', v.index === m.selected ? 'true' : 'false');
        btn.style.setProperty('--m', tint);
        span(btn, 'num', pad(v.index + 1));
        var mk = span(btn, 'mark' + (v.off_chart ? ' off' : ''));
        mk.setAttribute('aria-hidden', 'true');
        mk.style.setProperty('--m', v.stop ? DETAIL.stop : tint);
        var main = span(btn, 'main');
        var top = span(main, 'top');
        span(top, 'name', v.state + (v.current ? ' · now' : ''));
        span(top, 'who', v.who);
        var d = span(top, 'dur');
        if (v.current) d.textContent = 'in progress';
        else d.textContent = v.duration_seconds === null || v.duration_seconds === undefined ? '—' : dur(v.duration_seconds);
        if (v.off_chart) span(top, 'offchip', 'off-chart');
        span(main, 'note', v.reason !== null && v.reason !== undefined ? v.reason : (v.index === 0 ? DETAIL.first : ''));
      });
    }
    add(sec, el('p', 'jd-foot', 'Pick a step here or on the chart; both follow.'));
    return sec;
  }
  function liverun(r) {
    var sec = jdPanel(null, 'Live run', r.recipient + ' · job ' + r.job);
    sec.setAttribute('id', 'liverun-' + anchor(r.job));
    add(sec, telemetryBlock(r.telemetry));
    return sec;
  }
  function legendItem(parent, cls, tint, text, bg) {
    var s = span(parent, null);
    var i = add(s, el('i', cls || null));
    if (tint) i.style.setProperty('--m', tint);
    if (bg) i.style.background = bg;
    s.appendChild(document.createTextNode(text));
  }
  function journeyPanel(r, m, hits, scales) {
    var sec = jdPanel(null, 'Journey', 'this ticket through time · pick a step for its reason');
    var box = add(sec, el('div', 'jd-box'));
    var canvas = add(box, el('canvas', 'jd-journey'));
    canvas.setAttribute('width', '1000');
    canvas.setAttribute('height', '336');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'Journey: ' + (m.visits.map(function (v) { return v.state; }).join(' → ') || 'no visits') +
      '; now ' + (m.state || 'unknown'));
    var ol = add(box, el('ol', 'journey'));
    m.visits.forEach(function (v) {
      var b = add(add(ol, el('li')), el('button'));
      hits.push(b);
      b.setAttribute('type', 'button');
      b.setAttribute('data-visit', String(v.index));
      b.setAttribute('data-state', v.state);
      b.setAttribute('aria-label', v.state);
      b.setAttribute('aria-pressed', v.index === m.selected ? 'true' : 'false');
    });
    var ctl = add(sec, el('div', 'jd-ctl'));
    function ctlButton(text, attr, val, pressed) {
      var b = add(ctl, el('button', null, text));
      b.setAttribute('type', 'button');
      b.setAttribute(attr, val);
      if (pressed !== null) { b.setAttribute('aria-pressed', pressed ? 'true' : 'false'); scales.push(b); }
      return b;
    }
    ctlButton('even steps', 'data-scale', 'even', true);
    if (m.all_durations_known) ctlButton('to scale', 'data-scale', 'scale', false);
    ctlButton('replay', 'data-replay', '1', null);
    var legend = add(sec, el('div', 'jd-legend'));
    DETAIL.who.forEach(function (w) { legendItem(legend, null, JOURNEY.tint[w], w); });
    legendItem(legend, null, DETAIL.stop, 'stopped');
    legendItem(legend, 'off', JOURNEY.tint.script, 'off-chart hop');
    return {sec: sec, canvas: canvas};
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
  function chartPanel(r, m, chart) {
    var box = el('details', 'jd-chart');
    var key = 'full-chart:' + r.job;
    if (!chart) { add(box, el('summary', null, 'Full state chart')); add(box, el('p', 'empty', 'statechart unavailable')); return {sec: box}; }
    box.setAttribute('data-key', key);
    box.open = !!openDetails[key];
    var sum = add(box, el('summary', null, 'Full state chart '));
    span(sum, 'count', DETAIL.chartSub.replace('{n}', String(chart.states.length)));
    var wrap = add(box, el('div', 'jd-box'));
    var canvas = add(wrap, el('canvas', 'jd-flowmap'));
    canvas.setAttribute('width', '1000');
    canvas.setAttribute('height', '352');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'Full state chart; path: ' + m.visits.map(function (v) { return v.state; }).join(' → '));
    var legend = add(box, el('div', 'jd-legend'));
    DETAIL.who.forEach(function (w) { legendItem(legend, 'line', null, 'path taken · ' + w, JOURNEY.tint[w]); });
    legendItem(legend, 'line', null, 'visited', '#5386a6');
    legendItem(legend, 'off', JOURNEY.tint.script, 'off-chart jump');
    legendItem(legend, 'now', null, 'now');
    legendItem(legend, 'next', null, 'can come next');
    box.addEventListener('toggle', function () { openDetails[key] = box.open; });
    return {sec: box, canvas: canvas};
  }

  function selection(r, m, generated) {
    var s = SEL[r.job];
    var beatAt = r.heartbeat_age_seconds === null || r.heartbeat_age_seconds === undefined
      ? null : generated - r.heartbeat_age_seconds;
    if (!s) {
      s = SEL[r.job] = {selected: m.selected, picked: false, tPlay: Date.now(), beatAt: beatAt, beatStart: null,
                        flashAt: null, scale: 'even'};
      return s;
    }
    if (!s.picked || s.selected >= m.visits.length) { s.selected = m.selected; s.picked = false; }
    if (beatAt !== null && s.beatAt !== null && beatAt > s.beatAt + 0.5) s.beatStart = Date.now();
    if (beatAt !== null) s.beatAt = beatAt;
    if (s.scale === 'scale' && !m.all_durations_known) s.scale = 'even';
    return s;
  }
  function pick(j, i, fromChart) {
    var s = SEL[j.r.job];
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
    drawOne(j, Date.now());
  }
  function placeHits(j) {
    var bars = journeyBars(j.m, SEL[j.r.job].scale);
    var buttons = j.hitButtons;
    for (var k = 0; k < buttons.length && k < bars.length; k++) {
      var b = bars[k], st = buttons[k].style;
      st.left = b.x + 'px'; st.top = b.y + 'px'; st.width = Math.max(4, b.w) + 'px'; st.height = Math.max(4, b.h) + 'px';
    }
  }
  function clock(epoch) {
    if (epoch === null || epoch === undefined || !isFinite(epoch)) return '';
    var d = new Date(epoch * 1000);
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function drawOne(j, now) {
    var s = SEL[j.r.job];
    if (j.canvas.getContext) {
      var ctx = j.canvas.getContext('2d');
      if (ctx) {
        var flash = s.flashAt === null || reducedMotion ? 0 : Math.max(0, 1 - (now - s.flashAt) / 260);
        var beat = s.beatStart === null || reducedMotion ? -1 : (now - s.beatStart) / 1000;
        drawJourney(ctx, j.m, {t: reducedMotion ? 1e6 : (now - s.tPlay) / 1000, scale: s.scale, selected: s.selected,
          flash: flash, beat: beat >= 0 && beat <= 1 ? beat : -1, reduced: reducedMotion, still: j.still,
          startClock: j.startClock, nowClock: j.nowClock});
      }
    }
    if (j.flowCanvas && j.flow.open && j.flowCanvas.getContext && FLOWMAP) {
      var fctx = j.flowCanvas.getContext('2d');
      if (fctx) drawFlowMap(fctx, FLOWMAP, {visits: j.m.visits, state: j.m.state, next: j.m.next.map(function (n) { return n.to; })},
                            {t: now / 1000, reduced: reducedMotion || j.still});
    }
  }
  function journeyFrame() {
    journeys = journeys.filter(function (j) { return j.canvas.isConnected; });
    if (!journeys.length) return false;
    var now = Date.now();
    journeys.forEach(function (j) { drawOne(j, now); });
    return !reducedMotion;
  }

  function jobArticle(r, state, d) {
    var chart = d.statechart || null;
    var m = detailModel(r, chart);
    var running = state === 'running';
    var motion = motionOf(r, m);
    var generated = Number(d.generated_at_epoch) || Date.now() / 1000;
    var s = selection(r, m, generated);
    m.selected = s.selected;
    var art = el('article', 'jobpage');
    art.setAttribute('id', anchor(r.job));
    add(art, crumb(r));
    add(art, hero(r, m, running, motion));
    var cols = add(art, el('div', 'jd-cols'));
    var logButtons = [], hitButtons = [], scales = [];
    add(cols, runlog(r, m, logButtons));
    var right = add(cols, el('div', 'jd-right'));
    add(right, liverun(r));
    var jp = journeyPanel(r, m, hitButtons, scales);
    add(right, jp.sec);
    add(right, nextPanel(m));
    var cp = chartPanel(r, m, chart);
    add(right, cp.sec);
    var starts = m.visits.map(function (v) { return v.started_at; }).filter(function (t) { return t !== null && t !== undefined; });
    var j = {r: r, m: m, still: holdsStill(motion), logButtons: logButtons, hitButtons: hitButtons, canvas: jp.canvas, flow: cp.sec, flowCanvas: cp.canvas,
             startClock: starts.length ? clock(Math.min.apply(null, starts)) : '', nowClock: clock(generated)};
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
    });
    placeHits(j);
    journeys.push(j);
    PX.onFrame(journeyFrame);
    return art;
  }

  function renderJobView(d) {
    var root = byId('jobview');
    if (!root) return;
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
""".replace("__VERBS__", json.dumps(VERBS)).replace(
    "__CHARTSUB__", json.dumps(CHART_SUB)
)
SCRIPT = journey_canvas.SCRIPT + FLOWMAP_SCRIPT + _SCRIPT
