"""The job page's flow diagram, read from the real statechart.

model/chart.mmd is the ticket loop's chart as the statechart tool draws it
from model/Orchestrator.tla; model/check.sh fails CI when the two differ.
The dashboard parses that file at snapshot time (states, transitions,
colour groups and legend), so the diagram on the job page is the chart, not
a copy of it: change the chart and the diagram changes.

A job's path is its ticket's history: the start state, then the state each
of the ticket's jobs worked in (build -> building, review -> review, ...),
then the ticket's current workflow state. Where two of those states have no
direct transition, a bridge of at most MAX_BRIDGE steps through the chart is
filled in, never through a human wait (HUMAN_WAITS); any other gap is kept
as a jump with no edge drawn. Once the ticket has rows in ticket_transitions, those rows are
the path from the first one on, with their times and no bridge.

The job view draws the whole chart on a canvas (flowmap_layout places the
nodes and routes the wires, FLOWMAP_SCRIPT's drawFlowMap paints a frame)
with that path lit in the colour of who did each step. A state is finished
(is_terminal) when the chart has no way back to work from it: no state
reachable from it lies on a cycle (done, merged, handed-off, ...).
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections import deque
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

from lib.dispatch.constants import HANDED_OFF_EXTERNALLY

ROOT = Path(__file__).resolve().parent.parent
CHART_PATH = ROOT / "model" / "chart.mmd"
CHART_MAX_BYTES = 256 * 1024
MAX_BRIDGE = 2
OFF_CHART = "no direct transition on the chart"
HUMAN_WAITS = frozenset({"waiting-max", "questions", "awaiting-ok", "waiting-external"})
GROUPS = ("blue", "purple", "orange", "yellow", "teal", "tan")
# The step a job works in, by job action (lib/dispatcher.py _state_for_action,
# plus the Grok Bot wakes that are themselves a state).
JOB_STATES = {
    "plan": "planning",
    "build": "building",
    "review": "review",
    "fix": "fixing",
    "update-from-main": "updating",
    "clarify": "questions",
    "stuck": "stuck",
    "blocked": "blocked",
    "capped": "capped",
    "refused": "refused",
    "awaiting-ok": "awaiting-ok",
}
QUEUE_JOB_BEFORE_ROW = frozenset(
    {
        "set-state",
        "plan-gap",
        "t3",
        "t5-answers",
        "cap-resume",
        "blocked-resume",
        "unsat-fix",
        "update-queued",
        "held-base-update",
    }
)
# A transition that only an approval won in another state can take: fixing ->
# ship ships the APPROVE a review left on red CI (T2ReadyCovered), so a path
# that skips review never takes it.
EARNED_IN = {("fixing", "ship"): "review"}
# Plain words under a state's name. Only text: states and transitions all
# come from the chart.
GLOSS = {
    "intake": "ticket",
    "fixing": "revise",
    "handed-off": "ready for review",
    "merge-queue": "approved, waiting to merge",
    "updating": "merging base in",
    "done": "shipped",
    "awaiting-ok": "waiting on a human OK",
}

_STATE = re.compile(r"^  (s\d+): (\S.*?)\s*$")
_LEGEND_ITEM = re.compile(r"^    (lg\d+): (\S.*?)\s*$")
_START = re.compile(r"^  \[\*\] --> (s\d+)\s*$")
_EDGE = re.compile(r"^  (s\d+) --> (s\d+)(?:: (.*?))?\s*$")
_CLASS = re.compile(r"^  class (\w+) (\w+)\s*$")


def parse_chart(text: str) -> dict | None:
    """States, transitions, groups and legend of a stateDiagram-v2 chart in
    the statechart tool's format, or None if it is not one."""
    if "stateDiagram-v2" not in text:
        return None
    names: dict[str, str] = {}
    legend: dict[str, str] = {}
    groups: dict[str, str] = {}
    start: str | None = None
    edges: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    raw_edges: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if match := _LEGEND_ITEM.match(line):
            legend[match.group(1)] = match.group(2)
        elif match := _START.match(line):
            start = start or match.group(1)
        elif match := _EDGE.match(line):
            raw_edges.append((match.group(1), match.group(2), match.group(3) or ""))
        elif match := _CLASS.match(line):
            groups[match.group(1)] = match.group(2)
        elif match := _STATE.match(line):
            names[match.group(1)] = match.group(2)
    if not names or start not in names:
        return None
    for a, b, label in raw_edges:
        if a not in names or b not in names:
            continue
        key = (names[a], names[b])
        if key in seen:
            # One arrow per pair; the labels are joined.
            seen[key]["label"] = " / ".join(x for x in (seen[key]["label"], label) if x)
            continue
        seen[key] = {"from": key[0], "to": key[1], "label": label}
        edges.append(seen[key])

    def group(ident: str) -> str:
        value = groups.get(ident, "")
        return value if value in GROUPS else "other"

    return {
        "start": names[start],
        "states": [{"name": name, "group": group(ident)} for ident, name in names.items()],
        "edges": edges,
        "legend": [{"name": name, "group": group(ident)} for ident, name in legend.items()],
    }


def load_chart(path: Path | None = None) -> dict | None:
    path = CHART_PATH if path is None else path
    try:
        if not path.is_file() or path.stat().st_size > CHART_MAX_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    chart = parse_chart(text)
    if chart is not None:
        chart["source"] = path.name
    return chart


def _adjacency(chart: dict) -> dict[str, list[str]]:
    adjacency: dict[str, list[str]] = {state["name"]: [] for state in chart["states"]}
    for edge in chart["edges"]:
        adjacency[edge["from"]].append(edge["to"])
    return adjacency


def _bridge(
    adjacency: dict[str, list[str]], a: str, b: str, hops: int, avoid: frozenset[str]
) -> list[str] | None:
    """The shortest chart path a .. b of at most `hops` transitions."""
    queue = deque([[a]])
    while queue:
        path = queue.popleft()
        if len(path) - 1 >= hops:
            continue
        for nxt in adjacency.get(path[-1], ()):
            if nxt in path:
                continue
            if nxt == b:
                return [*path, b]
            if nxt not in avoid:
                queue.append([*path, nxt])
    return None


class JobStep(NamedTuple):
    action: str
    recipient: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    attempt: int = 1


class Visit(NamedTuple):
    state: str
    steps: list[JobStep]


class Transition(NamedTuple):
    from_state: str | None
    to_state: str
    at: float
    source: str


def chart_visits(
    chart: dict | None, observed: Iterable[tuple[str | None, JobStep | None]]
) -> list[Visit]:
    known = None if chart is None else {state["name"] for state in chart["states"]}
    adjacency = {} if chart is None else _adjacency(chart)
    visits: list[Visit] = []
    for state, step in observed:
        if state is None or (known is not None and state not in known):
            continue
        steps = [] if step is None else [step]
        if visits and state == visits[-1].state:
            visits[-1].steps.extend(steps)
            continue
        if visits and chart is not None and state not in adjacency[visits[-1].state]:
            bridge = _bridge(adjacency, visits[-1].state, state, MAX_BRIDGE, HUMAN_WAITS) or []
            visits.extend(Visit(name, []) for name in bridge[1:-1])
        visits.append(Visit(state, steps))
    return visits


def chart_path(chart: dict | None, observed: Iterable[str | None]) -> list[str]:
    """The states a ticket went through, in order, on the chart."""
    return [visit.state for visit in chart_visits(chart, ((state, None) for state in observed))]


def path_edges(chart: dict | None, path: Sequence[str]) -> list[tuple[str, str]]:
    """The chart transitions the path took, in order."""
    if chart is None:
        return []
    edges = {(edge["from"], edge["to"]) for edge in chart["edges"]}
    return [pair for pair in zip(path, path[1:]) if pair in edges]


def successors(chart: dict | None, state: str | None) -> list[str]:
    """The chart's direct successors of `state`, in chart order."""
    if chart is None or state is None:
        return []
    return list(dict.fromkeys(edge["to"] for edge in chart["edges"] if edge["from"] == state))


def _reach(adjacency: dict[str, list[str]], state: str) -> set[str]:
    """Every state reachable from `state` by at least one transition."""
    seen: set[str] = set()
    queue = deque(adjacency.get(state, ()))
    while queue:
        nxt = queue.popleft()
        if nxt not in seen:
            seen.add(nxt)
            queue.extend(adjacency.get(nxt, ()))
    return seen


def is_terminal(chart: dict | None, state: str | None) -> bool:
    """True when the chart has no way back to work from `state`: no state
    reachable from it (itself included) lies on a cycle. done, abandoned and
    the other stop states qualify, and so do merged and post-merge, which
    only lead on to done."""
    if chart is None or state is None:
        return False
    adjacency = _adjacency(chart)
    if state not in adjacency:
        return False
    ahead = {state} | _reach(adjacency, state)
    return not any(name in _reach(adjacency, name) for name in ahead)


PIPELINE = ("Ask", "Plan", "Build", "Review", "Ship")
SHIPPED = ("handed-off", "merged", "done", HANDED_OFF_EXTERNALLY)
PHASES = (*PIPELINE, "Shipped", "Stop")
PHASE_OVERRIDES = {"capped": "Stop", **{name: "Shipped" for name in SHIPPED}}


def ticket_phases(chart: dict | None) -> dict[str, str]:
    """Every chart state's board phase, from the legend item that shares its
    colour group, then PHASE_OVERRIDES. In reading order: breadth first from
    the start state, then any state the start cannot reach."""
    if chart is None:
        return {}
    legend = {item["group"]: item["name"] for item in chart["legend"]}
    groups = {state["name"]: state["group"] for state in chart["states"]}
    adjacency = _adjacency(chart)
    order = [chart["start"]]
    queue = deque(order)
    while queue:
        for nxt in adjacency[queue.popleft()]:
            if nxt not in order:
                order.append(nxt)
                queue.append(nxt)
    order += [name for name in groups if name not in order]
    phases = {}
    for name in order:
        phase = PHASE_OVERRIDES.get(name) or legend.get(groups[name])
        phases[name] = phase if phase in PHASES else "Stop"
    return phases


def hops_to_shipped(chart: dict | None, state: str | None) -> int | None:
    if chart is None or state is None:
        return None
    adjacency = _adjacency(chart)
    if state not in adjacency:
        return None
    hops = {state: 0}
    queue = deque([state])
    while queue:
        name = queue.popleft()
        if name in SHIPPED:
            return hops[name]
        for nxt in adjacency[name]:
            # Max's move in Linear is not a step the factory takes (#749).
            if nxt not in hops and nxt != HANDED_OFF_EXTERNALLY:
                hops[nxt] = hops[name] + 1
                queue.append(nxt)
    return None


def ticket_phase(phases: dict[str, str], state: str, stuck: bool) -> str:
    return "Stop" if stuck else phases.get(state, "Stop")


def shipped_facts(chart: dict | None, flow: dict) -> dict | None:
    """{at, how} when the ticket's current state is on the Shipped shelf: when
    it first reached a Shipped state, and whether that was a handoff."""
    phases = ticket_phases(chart)
    if phases.get(flow["state"]) != "Shipped":
        return None
    first = next(visit for visit in flow["visits"] if phases.get(visit["state"]) == "Shipped")
    return {
        "at": first["started_at"],
        "how": "handed-off" if first["state"] == "handed-off" else "merged",
    }


def stop_facts(chart: dict | None, flow: dict, stored_state: str, stuck: bool) -> dict | None:
    """{state, at, stopped_in, ways_out} when the board puts the ticket in its
    Stopped group (ticket_phase). A stuck flag on a stored state that is not
    itself a stop reads as stopped in "stuck"."""
    if chart is None:
        return None
    phases = ticket_phases(chart)
    if ticket_phase(phases, stored_state, stuck) != "Stop":
        return None
    stop = stored_state if ticket_phase(phases, stored_state, False) == "Stop" else "stuck"
    visits = flow["visits"]
    index = next(
        (i for i, visit in enumerate(visits) if visit["current"] and visit["state"] == stop),
        None,
    )
    if stop != stored_state:
        stopped_in: str | None = stored_state
    else:
        stopped_in = visits[index - 1]["state"] if index else None
    return {
        "state": stop,
        "at": None if index is None else visits[index]["started_at"],
        "stopped_in": stopped_in,
        "ways_out": [
            {"to": edge["to"], "label": edge["label"]}
            for edge in chart["edges"]
            if edge["from"] == stop
        ],
    }


def _started_at(visit: Visit) -> float | None:
    if not visit.steps:
        return None
    step = visit.steps[0]
    return step.created_at if step.started_at is None else step.started_at


def _owning_row(
    transitions: Sequence[Transition],
    floors: Sequence[float],
    ends: Sequence[float],
    state: str,
    created_at: float,
) -> int | None:
    for i, row in enumerate(transitions):
        if row.to_state == state and floors[i] <= created_at < ends[i]:
            return i
    for i, row in enumerate(transitions):
        if row.at <= created_at < ends[i]:
            return i
    return None


def _every_path_visits(chart: dict | None, origin: str | None, target: str, via: str) -> bool:
    if via in (origin, target):
        return True
    if chart is None or origin is None or origin == target:
        return False
    adjacency = _adjacency(chart)
    avoiding = {
        name: [nxt for nxt in nexts if via not in (nxt, EARNED_IN.get((name, nxt)))]
        for name, nexts in adjacency.items()
    }
    return target in _reach(adjacency, origin) and target not in _reach(avoiding, origin)


def _first_row_floor(
    chart: dict | None, steps: Sequence[tuple[str, JobStep]], first: Transition
) -> float:
    if first.from_state is None:
        return -math.inf
    start = None if chart is None else chart["start"]
    last_other_job, last_state = max(
        ((step.created_at, state) for state, step in steps
         if step.created_at < first.at and state != first.to_state),
        default=(-math.inf, start),
    )
    kept_by_old_visit, *later = sorted(
        (step.created_at, step.attempt) for state, step in steps
        if state == first.to_state and last_other_job < step.created_at < first.at
    ) or [None]
    reopened = [at for at, attempt in later if attempt == 1]
    if not reopened and not _every_path_visits(chart, last_state, first.from_state, first.to_state):
        return last_other_job
    if first.source not in QUEUE_JOB_BEFORE_ROW:
        return first.at
    return max(reopened, default=first.at)


def _recorded_walk(
    chart: dict | None,
    steps: Sequence[tuple[str, JobStep]],
    current: str | None,
    transitions: Sequence[Transition],
    tracked_at: float | None,
) -> tuple[list[Visit], list[float | None]]:
    known = None if chart is None else {state["name"] for state in chart["states"]}
    start = None if chart is None else chart["start"]
    first = transitions[0]
    ends = [row.at for row in transitions[1:]] + [math.inf]
    floors = [_first_row_floor(chart, steps, first), *(row.at for row in transitions[:-1])]
    owned: list[list[JobStep]] = [[] for _ in transitions]
    before = []
    for state, step in steps:
        index = _owning_row(transitions, floors, ends, state, step.created_at)
        if index is None:
            before.append((state, step))
        else:
            owned[index].append(step)
    if first.from_state is None:
        walk = [] if start is None else [Visit(start, [step for _, step in before])]
        opened = tracked_at if tracked_at is not None and tracked_at <= first.at else None
        starts = [opened] * len(walk)
    else:
        walk = chart_visits(chart, [(start, None), *before, (first.from_state, None)])
        starts = [_started_at(visit) for visit in walk]
    recorded = [(row.to_state, row.at, jobs) for row, jobs in zip(transitions, owned)]
    if current is not None and current != transitions[-1].to_state:
        recorded.append((current, None, []))
    for state, began, jobs in recorded:
        if known is not None and state not in known:
            continue
        if walk and walk[-1].state == state:
            walk[-1].steps.extend(jobs)
            continue
        walk.append(Visit(state, jobs))
        starts.append(began)
    return walk, starts


def job_flow(
    chart: dict | None,
    history: Sequence[JobStep],
    current: str | None,
    now: float,
    transitions: Sequence[Transition] = (),
    tracked_at: float | None = None,
) -> dict:
    """{state, path, state_age_seconds, next, terminal, visits,
    hops_to_shipped, all_durations_known} for a ticket whose jobs, oldest
    first, are `history` and whose recorded state changes, oldest first,
    are `transitions` (_recorded_walk). `next` is the chart's direct
    successors of the current state; `terminal` says the current state is
    finished (is_terminal). `visits` holds one entry per `path` state:
    {state, who, started_at, duration_seconds, time_known, off_chart,
    reason, current}. A visit ends when the next one starts, else when its
    last job finished; the current visit counts up to `now`. Unknown times
    are None."""
    steps = [(JOB_STATES[step.action], step) for step in history if step.action in JOB_STATES]
    if chart is not None and current is not None and current not in {
        state["name"] for state in chart["states"]
    }:
        current = None
    if transitions:
        walk, starts = _recorded_walk(chart, steps, current, transitions, tracked_at)
    else:
        start = chart["start"] if chart is not None else None
        walk = chart_visits(chart, [(start, None), *steps, (current, None)])
        starts = [_started_at(visit) for visit in walk]
    path = [visit.state for visit in walk]
    age = None
    if current is not None and transitions and transitions[-1].to_state == current:
        age = int(max(0.0, now - transitions[-1].at))
    elif current is not None and steps and steps[-1][0] == current:
        # Time in state counts from the first job of the run that is still
        # in it, when no recorded transition says when the state began.
        index = len(steps) - 1
        while index > 0 and steps[index - 1][0] == current:
            index -= 1
        age = int(max(0.0, now - float(steps[index][1].created_at)))
    at = max((i for i, state in enumerate(path) if state == current), default=-1)
    phases = ticket_phases(chart)
    labels = {} if chart is None else {
        (edge["from"], edge["to"]): edge["label"] for edge in chart["edges"]
    }
    visits = []
    for i, visit in enumerate(walk):
        began = starts[i]
        end = starts[i + 1] if i + 1 < len(walk) else None
        if i == at and began is not None:
            end = now
        elif end is None and visit.steps:
            end = visit.steps[-1].finished_at
        duration = None if began is None or end is None else int(max(0.0, end - began))
        hop = (path[i - 1], visit.state) if i else None
        visits.append({
            "state": visit.state,
            "who": (
                "script"
                if not visit.steps or phases.get(visit.state) == "Stop"
                else visit.steps[0].recipient
            ),
            "started_at": began,
            "duration_seconds": duration,
            "time_known": duration is not None,
            "off_chart": hop is not None and hop not in labels,
            "reason": None if hop is None else labels.get(hop, OFF_CHART),
            "current": i == at,
        })
    return {
        "state": current,
        "path": path,
        "state_age_seconds": age,
        "next": successors(chart, current),
        "terminal": is_terminal(chart, current),
        "visits": visits,
        "hops_to_shipped": hops_to_shipped(chart, current),
        "all_durations_known": bool(visits) and all(
            visit["duration_seconds"] is not None for visit in visits
        ),
    }


# --- drawing ------------------------------------------------------------------------

GRID_W, CELL = 250, 4
FM_NODE_W, FM_NODE_H = 34, 9
STOP_ROW = 5
STOP_RED = "#f7768e"
FLOWMAP_TINT = {
    "script": "#a9b1d6",
    "claude": "#e0af68",
    "codex": "#7aa2f7",
    "cursor": "#bb9af7",
    "grok": "#73daca",
    "human": "#ff9e64",
}
FLOWMAP_PLACES = {
    "intake": (0, 0), "questions": (0, 1), "waiting-max": (0, 2),
    "planning": (1, 0), "plan-check": (1, 1),
    "building": (2, 0), "fixing": (2, 1),
    "review": (3, 0), "capped": (3, 1),
    "ship": (4, 0), "merge-queue": (4, 1), "updating": (4, 2), "awaiting-ok": (4, 3), "waiting-external": (4, 4),
    "merged": (5, 0), "post-merge": (5, 1), "done": (5, 2), "handed-off": (5, 3),
}
FLOWMAP_RAIL = ("refused", "abandoned", "stuck", "blocked")
FLOWMAP_COLUMNS = {"Ask": 0, "Plan": 1, "Build": 2, "Review": 3, "Ship": 4, "Shipped": 5}
FLOWMAP_HEADS = (("Ask", 0, 1), ("Plan", 1, 1), ("Build", 2, 1), ("Review", 3, 1), ("Ship", 4, 2))


def _col_x(column: int) -> int:
    return 3 + column * 42


def _row_y(row: int, rail: int) -> int:
    return 9 + row * 16 + (3 if row >= rail else 0)


def _channel_y(row: int, off: int, rail: int) -> int:
    y = _row_y(row, rail) + off
    return min(y, 9 + rail * 16 - 3) if row == rail - 1 else y


def _flowmap_places(chart: dict) -> tuple[dict[str, tuple[int, int]], int]:
    phases = ticket_phases(chart)
    adjacency = _adjacency(chart)
    places = {name: FLOWMAP_PLACES[name] for name in phases if name in FLOWMAP_PLACES}
    taken = set(places.values())
    stops = []
    for name, phase in phases.items():
        if name in places or name in FLOWMAP_RAIL:
            continue
        if phase == "Stop" and all(nxt == name for nxt in adjacency[name]):
            stops.append(name)
            continue
        column = FLOWMAP_COLUMNS.get(phase, FLOWMAP_COLUMNS["Review"])
        place = next((column, row) for row in itertools.count() if (column, row) not in taken)
        places[name] = place
        taken.add(place)
    rail = max([STOP_ROW, *(row + 1 for _, row in places.values())])
    places.update({name: (FLOWMAP_RAIL.index(name), rail) for name in phases if name in FLOWMAP_RAIL})
    taken = set(places.values())
    spots = ((column, row) for row in itertools.count(rail) for column in range(len(FLOWMAP_COLUMNS)))
    for name in stops:
        places[name] = next(spot for spot in spots if spot not in taken)
    return places, rail


def _route(a: dict, b: dict, nodes: Sequence[dict], rail: int) -> list[list[int]]:
    nw, nh = FM_NODE_W, FM_NODE_H
    ax, ay0, bx, by0 = a["x"], a["y"], b["x"], b["y"]
    if a is b:
        g1 = min(GRID_W - 2, ax + 36)
        return [[ax + nw, ay0 + 2], [g1, ay0 + 2], [g1, ay0 + 6], [ax + nw, ay0 + 6]]
    if a["col"] == b["col"]:
        lo, hi = sorted((a["row"], b["row"]))
        blocked = any(n["col"] == a["col"] and lo < n["row"] < hi for n in nodes)
        if not blocked:
            if b["row"] > a["row"]:
                return [[ax + 14, ay0 + nh], [ax + 14, by0 - 1]]
            return [[ax + 20, ay0 - 1], [ax + 20, by0 + nh]]
        g1 = min(GRID_W - 2, ax + 36)
        return [[ax + nw, ay0 + 2], [g1, ay0 + 2], [g1, by0 + 6], [bx + nw, by0 + 6]]
    below = b["row"] >= a["row"] and a["row"] < rail
    channel = a["row"] if below else a["row"] - 1
    if b["col"] > a["col"]:
        ay, by, g1 = ay0 + 2, by0 + 2, ax + 36
        if b["col"] == a["col"] + 1:
            return [[ax + nw, ay], [g1, ay], [g1, by], [bx - 1, by]]
        ch = _channel_y(channel, 11, rail)
        g2 = _col_x(b["col"] - 1) + 36
        return [[ax + nw, ay], [g1, ay], [g1, ch], [g2, ch], [g2, by], [bx - 1, by]]
    ay, by, g1 = ay0 + 6, by0 + 6, _col_x(a["col"] - 1) + 39
    if b["col"] == a["col"] - 1:
        return [[ax - 1, ay], [g1, ay], [g1, by], [bx + nw, by]]
    ch = _channel_y(channel, 14, rail)
    g2 = _col_x(b["col"]) + 39
    return [[ax - 1, ay], [g1, ay], [g1, ch], [g2, ch], [g2, by], [bx + nw, by]]


def flowmap_layout(chart: dict) -> dict:
    """The full state chart on FlowMapR2's 250 x 104 cell grid (4 px cells,
    1000 x 416 px): phase columns (Ship two columns wide), stop states on
    the rail below them, and a wire route for every ordered pair of states
    so an off-chart hop has one too. `edges` lists the chart's transitions."""
    places, rail = _flowmap_places(chart)
    phases = ticket_phases(chart)
    adjacency = _adjacency(chart)
    nodes = []
    for state in chart["states"]:
        name = state["name"]
        column, row = places[name]
        exits = [nxt for nxt in adjacency[name] if nxt != name]
        nodes.append({
            "name": name,
            "gloss": GLOSS.get(name, ""),
            "col": column,
            "row": row,
            "x": _col_x(column),
            "y": _row_y(row, rail),
            "stop": row >= rail and phases[name] == "Stop",
            "end": not exits,
        })
    loops = {edge["from"] for edge in chart["edges"] if edge["from"] == edge["to"]}
    routes = {
        f'{a["name"]}>{b["name"]}': _route(a, b, nodes, rail)
        for a in nodes
        for b in nodes
        if a is not b or a["name"] in loops
    }
    edges = [f'{edge["from"]}>{edge["to"]}' for edge in chart["edges"]]
    last = max([rail, *(node["row"] for node in nodes)])
    bottom = {node["col"] for node in nodes if node["row"] == last}
    free = [column for column in range(len(FLOWMAP_COLUMNS)) if column not in bottom]
    return {
        "grid": {"w": GRID_W, "h": _row_y(last, rail) + 12, "cell": CELL},
        "node": {"w": FM_NODE_W, "h": FM_NODE_H},
        "nodes": nodes,
        "edges": edges,
        "routes": routes,
        "heads": [
            {"label": label, "x": _col_x(column) * CELL, "w": (span * 42 - 8) * CELL}
            for label, column, span in FLOWMAP_HEADS
        ],
        "rail": {"y": 9 + rail * 16 - 1, "x0": 3, "x1": 247},
        "stop_label": {"x": _col_x(free[0]) * CELL if free else 700, "y": (_row_y(last, rail) + 2) * CELL + 2},
    }


FLOWMAP_SCRIPT = r"""
var FM_TINT = __TINT__;
var FM_RED = '__RED__';
var FM_FONT = "'JetBrains Mono', ui-monospace, Menlo, monospace";

function fmHex(s) {
  var m = /^#?([0-9a-f]{6})/i.exec(String(s || ''));
  if (!m) return [224, 175, 104];
  return [parseInt(m[1].substr(0, 2), 16), parseInt(m[1].substr(2, 2), 16), parseInt(m[1].substr(4, 2), 16)];
}

function fmMix(rgb, k) {
  return fmRgb(14 + (rgb[0] - 14) * k, 14 + (rgb[1] - 14) * k, 20 + (rgb[2] - 20) * k);
}

function fmRgb(r, g, b) {
  return 'rgb(' + Math.round(r) + ',' + Math.round(g) + ',' + Math.round(b) + ')';
}

function fmCells(pts) {
  var out = [], i, x = pts[0][0], y = pts[0][1], tx, ty;
  out.push([x, y]);
  for (i = 1; i < pts.length; i++) {
    tx = pts[i][0];
    ty = pts[i][1];
    while (x !== tx || y !== ty) {
      if (x !== tx) x += tx > x ? 1 : -1;
      else y += ty > y ? 1 : -1;
      out.push([x, y]);
    }
  }
  return out;
}

function fmHead(cells) {
  var n = cells.length;
  if (n < 2) return [];
  var lx = cells[n - 1][0], ly = cells[n - 1][1];
  var dx = lx - cells[n - 2][0], dy = ly - cells[n - 2][1];
  return [[lx - dx + dy, ly - dy + dx], [lx - dx - dy, ly - dy - dx]];
}

function fmPerim(n, NW, NH) {
  var out = [], j;
  for (j = 0; j < NW; j++) out.push([n.x + j, n.y]);
  for (j = 1; j < NH; j++) out.push([n.x + NW - 1, n.y + j]);
  for (j = NW - 2; j >= 0; j--) out.push([n.x + j, n.y + NH - 1]);
  for (j = NH - 2; j >= 1; j--) out.push([n.x, n.y + j]);
  return out;
}

function drawFlowMap(ctx, layout, model, opts) {
  var NW = layout.node.w, NH = layout.node.h, px = layout.grid.cell;
  var W = layout.grid.w * px, H = layout.grid.h * px;
  var put = function (x, y) { ctx.fillRect(x * px, y * px, px, px); };
  var byName = {}, i, j, k, n, c, d, w, name, v;
  var nodes = layout.nodes;
  for (i = 0; i < nodes.length; i++) {
    byName[nodes[i].name] = { def: nodes[i], visits: 0, current: false, next: false };
  }
  var edgeSet = {};
  for (i = 0; i < layout.edges.length; i++) edgeSet[layout.edges[i]] = true;

  var visits = (model && model.visits) || [], path = [], curWho = null;
  for (i = 0; i < visits.length; i++) {
    v = visits[i];
    if (!byName[v.state]) continue;
    path.push(v);
    byName[v.state].visits += 1;
    if (v.current) curWho = v.who;
  }
  var curName = model && model.state && byName[model.state] ? model.state : null;
  var cur = curName ? byName[curName] : null;
  if (cur) cur.current = true;
  var halted = !!(cur && cur.def.stop);
  var reduced = halted || !!(opts && opts.reduced);
  var t = reduced ? 0 : ((opts && opts.t) || 0);

  var wire = [], heads = [], hops = [], cols = [], taken = {}, a, b, id, cells, jump, hex, rgb, mid;
  for (i = 1; i < path.length; i++) {
    a = path[i - 1].state;
    b = path[i].state;
    if (a === b) continue;
    id = a + '>' + b;
    if (!layout.routes[id]) continue;
    jump = !edgeSet[id] || !!path[i].off_chart;
    taken[id] = true;
    cells = fmCells(layout.routes[id]);
    hex = byName[b].def.stop ? FM_RED : (FM_TINT[path[i].who] || FM_TINT.script);
    rgb = fmHex(hex);
    cols.push({
      lit: hex,
      mid: fmMix(rgb, 0.66),
      crest: fmRgb((rgb[0] + 255) / 2, (rgb[1] + 255) / 2, (rgb[2] + 255) / 2)
    });
    for (j = 0; j < cells.length; j++) {
      wire.push([cells[j][0], cells[j][1], jump ? 1 : 0, j, cols.length - 1]);
    }
    heads.push(fmHead(cells));
    mid = cells[Math.floor(cells.length / 2)];
    hops.push({ label: String(hops.length + 1), x: mid[0] * px - 5, y: mid[1] * px - 5, color: hex });
  }

  var nextWires = [], nexts = (model && model.next) || [];
  for (i = 0; i < nexts.length; i++) {
    if (!cur || !byName[nexts[i]]) continue;
    byName[nexts[i]].next = true;
    id = curName + '>' + nexts[i];
    if (nexts[i] === curName || taken[id] || !layout.routes[id]) continue;
    cells = fmCells(layout.routes[id]);
    nextWires.push({ cells: cells, head: fmHead(cells) });
  }

  ctx.fillStyle = '#0e0e14';
  ctx.fillRect(0, 0, W, H);

  ctx.fillStyle = '#24283b';
  for (i = layout.rail.x0; i < layout.rail.x1; i += 2) put(i, layout.rail.y);

  ctx.fillStyle = '#6b7399';
  for (i = 0; i < nextWires.length; i++) {
    w = nextWires[i].cells;
    for (j = 0; j < w.length; j++) {
      if (j % 3 !== 2) put(w[j][0], w[j][1]);
    }
    w = nextWires[i].head;
    for (j = 0; j < w.length; j++) put(w[j][0], w[j][1]);
  }

  var total = wire.length;
  var pulse = reduced ? -99 : (t * 30) % (total + 45);
  for (i = 0; i < total; i++) {
    c = wire[i];
    if (c[2] === 1 && c[3] % 3 === 2) continue;
    ctx.fillStyle = cols[c[4]].mid;
    put(c[0], c[1]);
  }
  if (!reduced) {
    for (i = 0; i < total; i++) {
      c = wire[i];
      d = pulse - i;
      if (d < 0 || d >= 14) continue;
      if (c[2] === 1 && c[3] % 3 === 2) continue;
      ctx.fillStyle = d < 4 ? cols[c[4]].crest : cols[c[4]].lit;
      put(c[0], c[1]);
    }
  }
  for (i = 0; i < heads.length; i++) {
    ctx.fillStyle = cols[i].lit;
    for (j = 0; j < heads[i].length; j++) put(heads[i][j][0], heads[i][j][1]);
  }

  var tintRgb = fmHex((opts && opts.tint) || FM_TINT[curWho] || FM_TINT.script);
  var tint = fmRgb(tintRgb[0], tintRgb[1], tintRgb[2]);
  var tintDim = fmMix(tintRgb, 0.3);
  var march = Math.floor(t * 14), band = Math.floor(t * 10), perim, edge, dashed;
  for (i = 0; i < nodes.length; i++) {
    n = byName[nodes[i].name];
    d = n.def;
    ctx.fillStyle = n.current ? '#292e42' : n.visits > 0 ? '#16222d' : '#13141c';
    ctx.fillRect((d.x + 1) * px, (d.y + 1) * px, (NW - 2) * px, (NH - 2) * px);
    perim = fmPerim(d, NW, NH);
    edge = n.current ? '#7dcfff' : n.visits > 0 ? '#5386a6' : n.next ? '#8b93b8' : '#343a55';
    dashed = !n.current && n.visits === 0 && n.next;
    ctx.fillStyle = edge;
    for (j = 0; j < perim.length; j++) {
      if (dashed && j % 2 === 1) continue;
      put(perim[j][0], perim[j][1]);
    }
    if (d.end) {
      put(d.x + 1, d.y + 1);
      put(d.x + NW - 2, d.y + 1);
      put(d.x + 1, d.y + NH - 2);
      put(d.x + NW - 2, d.y + NH - 2);
    }
    if (n.current) {
      if (!reduced) {
        ctx.fillStyle = '#b8e5ff';
        for (j = 0; j < 8; j++) {
          k = (((march - j) % perim.length) + perim.length) % perim.length;
          put(perim[k][0], perim[k][1]);
        }
      }
      for (j = 1; j < NW - 1; j++) {
        if (halted) {
          ctx.fillStyle = j % 2 === 0 ? FM_RED : '#542d39';
          put(d.x + j, d.y + NH - 3);
          ctx.fillStyle = j % 2 === 0 ? '#542d39' : FM_RED;
          put(d.x + j, d.y + NH - 2);
          continue;
        }
        k = (((j - band) % 12) + 12) % 12;
        ctx.fillStyle = k < 3 ? tint : tintDim;
        put(d.x + j, d.y + NH - 3);
        put(d.x + j, d.y + NH - 2);
      }
    }
  }

  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = '400 11px ' + FM_FONT;
  ctx.letterSpacing = '0.88px';
  ctx.fillStyle = '#8b93b8';
  for (i = 0; i < layout.heads.length; i++) {
    w = layout.heads[i];
    ctx.fillText(w.label.toUpperCase(), w.x + w.w / 2, 17);
  }
  ctx.letterSpacing = '0px';
  var left, top, height, cx, cy;
  for (i = 0; i < nodes.length; i++) {
    n = byName[nodes[i].name];
    d = n.def;
    left = d.x * px;
    top = d.y * px;
    height = n.current ? 26 : 36;
    cx = left + 68;
    cy = top + height / 2;
    ctx.font = '500 11px ' + FM_FONT;
    ctx.fillStyle = n.current ? '#7dcfff' : n.visits > 0 ? '#c0caf5' : n.next ? '#a9b1d6' : '#8b93b8';
    ctx.fillText(d.name, cx, d.gloss ? cy - 5.5 : cy);
    if (d.gloss) {
      ctx.font = (d.gloss.length > 23 ? '400 8px ' : '400 9px ') + FM_FONT;
      ctx.fillStyle = '#8b93b8';
      ctx.fillText(d.gloss, cx, cy + 6.5);
    }
  }
  ctx.font = '700 10px ' + FM_FONT;
  for (i = 0; i < nodes.length; i++) {
    n = byName[nodes[i].name];
    if (n.visits < 2) continue;
    d = n.def;
    name = '×' + n.visits;
    w = ctx.measureText(name).width + 8;
    left = (d.x + NW) * px - 26;
    top = d.y * px - 7;
    ctx.fillStyle = '#7dcfff';
    ctx.fillRect(left, top, w, 14);
    ctx.fillStyle = '#0c0e10';
    ctx.fillText(name, left + w / 2, top + 7);
  }
  ctx.font = '700 9px ' + FM_FONT;
  ctx.lineWidth = 1;
  for (i = 0; i < hops.length; i++) {
    ctx.fillStyle = '#0e0e14';
    ctx.fillRect(hops[i].x, hops[i].y, 14, 14);
    ctx.strokeStyle = hops[i].color;
    ctx.strokeRect(hops[i].x + 0.5, hops[i].y + 0.5, 13, 13);
    ctx.fillStyle = hops[i].color;
    ctx.fillText(hops[i].label, hops[i].x + 7, hops[i].y + 7);
  }
  ctx.textAlign = 'left';
  ctx.font = '400 11px ' + FM_FONT;
  ctx.letterSpacing = '0.88px';
  ctx.fillStyle = '#8b93b8';
  ctx.fillText('STOP STATES', layout.stop_label.x, layout.stop_label.y + 7);
  ctx.letterSpacing = '0px';
}
""".replace("__TINT__", json.dumps(FLOWMAP_TINT)).replace("__RED__", STOP_RED)
