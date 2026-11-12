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
filled in (intake -> questions -> building, since questions is Grok Bot's
step and has no job of its own); longer gaps are kept as a jump with no
edge drawn.
"""

from __future__ import annotations

import html
import re
from collections import deque
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHART_PATH = ROOT / "model" / "chart.mmd"
CHART_MAX_BYTES = 256 * 1024
MAX_BRIDGE = 2
GROUPS = ("blue", "purple", "orange", "yellow", "teal", "tan")
# The step a job works in, by job action (lib/dispatcher.py _state_for_action,
# plus the Grok Bot wakes that are themselves a state).
JOB_STATES = {
    "plan": "planning",
    "build": "building",
    "review": "review",
    "fix": "fixing",
    "clarify": "questions",
    "stuck": "stuck",
    "blocked": "blocked",
    "capped": "capped",
    "refused": "refused",
}
# Plain words under a state's name. Only text: states and transitions all
# come from the chart.
GLOSS = {
    "intake": "ticket",
    "fixing": "revise",
    "handed-off": "ready for review",
    "done": "shipped",
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


def _bridge(adjacency: dict[str, list[str]], a: str, b: str, hops: int) -> list[str] | None:
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
            queue.append([*path, nxt])
    return None


def chart_path(chart: dict | None, observed: Iterable[str | None]) -> list[str]:
    """The states a ticket went through, in order, on the chart."""
    known = None if chart is None else {state["name"] for state in chart["states"]}
    adjacency = {} if chart is None else _adjacency(chart)
    path: list[str] = []
    for state in observed:
        if state is None or (known is not None and state not in known):
            continue
        if not path:
            path.append(state)
            continue
        last = path[-1]
        if state == last:
            continue
        if chart is None or state in adjacency[last]:
            path.append(state)
            continue
        bridge = _bridge(adjacency, last, state, MAX_BRIDGE)
        path.extend(bridge[1:] if bridge else [state])
    return path


def path_edges(chart: dict | None, path: Sequence[str]) -> list[tuple[str, str]]:
    """The chart transitions the path took, in order."""
    if chart is None:
        return []
    edges = {(edge["from"], edge["to"]) for edge in chart["edges"]}
    return [pair for pair in zip(path, path[1:]) if pair in edges]


def job_flow(
    chart: dict | None,
    history: Sequence[tuple[str, float]],
    current: str | None,
    now: float,
) -> dict:
    """{state, path, state_age_seconds} for a ticket whose jobs, oldest
    first, are `history` as (action, created_at)."""
    steps = [(JOB_STATES[action], at) for action, at in history if action in JOB_STATES]
    start = chart["start"] if chart is not None else None
    path = chart_path(chart, [start, *(state for state, _ in steps), current])
    if chart is not None and current is not None and current not in {
        state["name"] for state in chart["states"]
    }:
        current = None
    age = None
    if current is not None and steps and steps[-1][0] == current:
        # Time in state counts from the first job of the run that is still
        # in it (the database records no transition times).
        index = len(steps) - 1
        while index > 0 and steps[index - 1][0] == current:
            index -= 1
        age = int(max(0.0, now - float(steps[index][1])))
    return {"state": current, "path": path, "state_age_seconds": age}


# --- drawing ------------------------------------------------------------------------

NODE_W = 128
NODE_H = 36
COL_W = 172
ROW_H = 60
LEFT = 64
TOP = 56
BOTTOM = 24
ARROW = 7.0


def layout(chart: dict) -> dict[str, tuple[float, float, int]]:
    """{state: (x, y, column)}: columns by distance from the start, stop
    states (tan) at the bottom of their column."""
    adjacency = _adjacency(chart)
    depth = {chart["start"]: 0}
    order = [chart["start"]]
    queue = deque([chart["start"]])
    while queue:
        state = queue.popleft()
        for nxt in adjacency[state]:
            if nxt not in depth:
                depth[nxt] = depth[state] + 1
                order.append(nxt)
                queue.append(nxt)
    deepest = max(depth.values())
    for state in chart["states"]:
        if state["name"] not in depth:
            depth[state["name"]] = deepest + 1
            order.append(state["name"])
    group = {state["name"]: state["group"] for state in chart["states"]}
    columns: dict[int, list[str]] = {}
    for state in sorted(order, key=lambda name: (group[name] == "tan", order.index(name))):
        columns.setdefault(depth[state], []).append(state)
    return {
        state: (LEFT + column * COL_W, TOP + row * ROW_H, column)
        for column, states in columns.items()
        for row, state in enumerate(states)
    }


def _n(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _arrow(x: float, y: float, cx: float, cy: float) -> str:
    """A filled triangle at (x, y) pointing away from (cx, cy)."""
    dx, dy = x - cx, y - cy
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / length, dy / length
    bx, by = x - ux * ARROW * 1.6, y - uy * ARROW * 1.6
    points = [(x, y), (bx - uy * ARROW * 0.7, by + ux * ARROW * 0.7),
              (bx + uy * ARROW * 0.7, by - ux * ARROW * 0.7)]
    return " ".join(f"{_n(px)},{_n(py)}" for px, py in points)


def _edge_geometry(a: tuple[float, float, int], b: tuple[float, float, int], same: bool):
    ax, ay, acol = a
    bx, by, bcol = b
    if same:
        x, y = ax + NODE_W - 22, ay
        c1, c2, end = (x, y - 30), (x + 34, y - 30), (ax + NODE_W, ay + 10)
        return (x, y), c1, c2, end
    if bcol > acol:
        start = (ax + NODE_W, ay + NODE_H / 2)
        end = (bx, by + NODE_H / 2)
        bend = (end[0] - start[0]) / 2
        return start, (start[0] + bend, start[1]), (end[0] - bend, end[1]), end
    if bcol == acol:
        start = (ax + NODE_W, ay + NODE_H / 2)
        end = (bx + NODE_W, by + NODE_H / 2)
        reach = 26 + 6 * abs(by - ay) / ROW_H
        return start, (start[0] + reach, start[1]), (end[0] + reach, end[1]), end
    # Backward: arch over the top of both boxes.
    start = (ax + NODE_W / 2 - 10, ay)
    end = (bx + NODE_W / 2 + 10, by)
    lift = 26 + 7 * (acol - bcol)
    top = min(ay, by) - lift
    return start, (start[0], top), (end[0], top), end


def render_svg(
    chart: dict,
    path: Sequence[str] = (),
    current: str | None = None,
    *,
    label: str = "Ticket statechart",
) -> str:
    """The chart as inline SVG (no external assets, no url() references),
    with `path` and `current` highlighted. Every state is a <g data-state>,
    every transition a <g data-from data-to>, so the page script can move
    the highlight without redrawing."""
    places = layout(chart)
    visited = set(path)
    taken = set(path_edges(chart, path))
    outgoing = {edge["from"] for edge in chart["edges"]}
    columns = max(column for _, _, column in places.values()) + 1
    rows = max(int((y - TOP) / ROW_H) for _, y, _ in places.values()) + 1
    width = LEFT + columns * COL_W - (COL_W - NODE_W) + 24
    height = TOP + rows * ROW_H + BOTTOM
    e = lambda value: html.escape(str(value), quote=True)  # noqa: E731
    parts = [
        f'<svg class="flow" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="{e(label)}">'
    ]
    sx, sy, _ = places[chart["start"]]
    parts.append(
        f'<g class="entry{" taken" if path else ""}"><title>ticket → {e(chart["start"])}</title>'
        f'<circle cx="{_n(sx - 34)}" cy="{_n(sy + NODE_H / 2)}" r="7"/>'
        f'<path d="M{_n(sx - 27)} {_n(sy + NODE_H / 2)} L{_n(sx - ARROW * 1.6)} {_n(sy + NODE_H / 2)}"/>'
        f'<polygon points="{_arrow(sx, sy + NODE_H / 2, sx - 27, sy + NODE_H / 2)}"/></g>'
    )
    for edge in chart["edges"]:
        a, b = edge["from"], edge["to"]
        start, c1, c2, end = _edge_geometry(places[a], places[b], a == b)
        classes = "edge taken" if (a, b) in taken else "edge"
        title = f"{a} → {b}" + (f": {edge['label']}" if edge["label"] else "")
        parts.append(
            f'<g class="{classes}" data-from="{e(a)}" data-to="{e(b)}"><title>{e(title)}</title>'
            f'<path d="M{_n(start[0])} {_n(start[1])} C{_n(c1[0])} {_n(c1[1])} '
            f'{_n(c2[0])} {_n(c2[1])} {_n(end[0])} {_n(end[1])}"/>'
            f'<polygon points="{_arrow(end[0], end[1], c2[0], c2[1])}"/></g>'
        )
    for state in chart["states"]:
        name = state["name"]
        x, y, _ = places[name]
        classes = ["node", f"g-{state['group']}"]
        if name not in outgoing:
            classes.append("end")
        if name in visited:
            classes.append("visited")
        if name == current:
            classes.append("current")
        gloss = GLOSS.get(name)
        text_y = y + (NODE_H / 2 - 2 if gloss else NODE_H / 2 + 4)
        parts.append(
            f'<g class="{" ".join(classes)}" data-state="{e(name)}"><title>{e(name)}</title>'
            f'<rect x="{_n(x)}" y="{_n(y)}" width="{NODE_W}" height="{NODE_H}" rx="6"/>'
            f'<text x="{_n(x + NODE_W / 2)}" y="{_n(text_y)}">{e(name)}</text>'
            + (
                f'<text class="gloss" x="{_n(x + NODE_W / 2)}" y="{_n(y + NODE_H - 6)}">{e(gloss)}</text>'
                if gloss
                else ""
            )
            + "</g>"
        )
    parts.append("</svg>")
    return "".join(parts)
