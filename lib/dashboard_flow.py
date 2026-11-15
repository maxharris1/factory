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

The job view draws that path as a left-to-right chain, one node per visit
(a ticket revised twice shows review, fixing, review, fixing, review), the
current step highlighted, and the chart's direct successors of the current
state grayed (render_path_svg). The full chart (render_svg) stays behind a
toggle. A state is finished (is_terminal) when the chart has no way back to
work from it: no state reachable from it lies on a cycle (done, merged,
handed-off, ...); a finished job's current step does not breathe.
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
    "update-from-main": "updating",
    "clarify": "questions",
    "stuck": "stuck",
    "blocked": "blocked",
    "capped": "capped",
    "refused": "refused",
    "awaiting-ok": "awaiting-ok",
}
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


# The Tickets board's phases (#142): the five pipeline columns, the Shipped
# shelf and the Stopped group.
PIPELINE = ("Ask", "Plan", "Build", "Review", "Ship")
SHIPPED = ("handed-off", "merged", "done")
PHASES = (*PIPELINE, "Shipped", "Stop")
# Max's calls on top of the chart's colour groups: capped is Stopped
# (STATUS.md decision 4) and the settled ship states sit on the shelf.
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


def ticket_phase(phases: dict[str, str], state: str, stuck: bool) -> str:
    """A ticket's phase: Stop when flagged stuck, whatever its state (STATUS.md
    decision 5), and Stop for a state the chart does not have, so the board
    never drops a ticket."""
    return "Stop" if stuck else phases.get(state, "Stop")


def job_flow(
    chart: dict | None,
    history: Sequence[tuple[str, float]],
    current: str | None,
    now: float,
) -> dict:
    """{state, path, state_age_seconds, next, terminal} for a ticket whose
    jobs, oldest first, are `history` as (action, created_at). `next` is the
    chart's direct successors of the current state; `terminal` says the
    current state is finished (is_terminal)."""
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
    return {
        "state": current,
        "path": path,
        "state_age_seconds": age,
        "next": successors(chart, current),
        "terminal": is_terminal(chart, current),
    }


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



# --- the job view: the path taken, the current step, what can come next -------------

VISIT_H = 50
HOP = 44
PATH_LEFT = 16
PATH_TOP = 14
NEXT_ROW = 60


def path_geometry() -> dict:
    """The simplified diagram's sizes, shared with the page script so the
    live redraw matches the server's."""
    return {
        "w": NODE_W, "h": VISIT_H, "hop": HOP, "left": PATH_LEFT, "top": PATH_TOP,
        "row": NEXT_ROW, "arrow": ARROW, "gloss": GLOSS,
    }


def _lines(y: float, texts: Sequence[tuple[str, str]]) -> list[tuple[float, str, str]]:
    """Baselines for one to three lines of text in a node at `y`."""
    baselines = {1: (VISIT_H / 2 + 4,), 2: (21, 36), 3: (17, 30, 43)}[len(texts)]
    return [(y + base, cls, text) for base, (cls, text) in zip(baselines, texts)]


def render_path_svg(
    chart: dict,
    path: Sequence[str],
    current: str | None,
    *,
    next_steps: Sequence[str] | None = None,
    terminal: bool | None = None,
    meta: str = "",
    label: str = "Path taken",
) -> str:
    """The path a ticket took as inline SVG: one node per visit, left to
    right in the order the ticket went (a revisited state appears again),
    arrows in that order, the current step (the last visit of `current`)
    highlighted and, unless it is finished, breathing; then the chart's
    direct successors of the current state, grayed with dashed arrows.
    Nothing else from the chart. `meta` (round, time in state) goes on the
    current step only."""
    if next_steps is None:
        next_steps = successors(chart, current)
    if terminal is None:
        terminal = is_terminal(chart, current)
    at = max((i for i, state in enumerate(path) if state == current), default=-1)
    nexts = list(next_steps) if at >= 0 else []
    group = {state["name"]: state["group"] for state in chart["states"]}
    labels = {(edge["from"], edge["to"]): edge["label"] for edge in chart["edges"]}
    step = NODE_W + HOP
    rows = max(1, len(nexts))
    y0 = PATH_TOP + (rows - 1) * NEXT_ROW / 2
    mid = y0 + VISIT_H / 2
    width = 2 * PATH_LEFT + max(1, len(path) + (1 if nexts else 0)) * step - HOP
    height = 2 * PATH_TOP + (rows - 1) * NEXT_ROW + VISIT_H
    e = lambda value: html.escape(str(value), quote=True)  # noqa: E731

    def title(a: str, b: str) -> str:
        if (a, b) not in labels:
            return f"{a} → {b} (no direct transition on the chart)"
        return f"{a} → {b}" + (f": {labels[(a, b)]}" if labels[(a, b)] else "")

    def texts(name: str, extra: str = "") -> list[tuple[str, str]]:
        lines = [("", name)]
        if GLOSS.get(name):
            lines.append(("gloss", GLOSS[name]))
        if extra:
            lines.append(("meta", extra))
        return lines

    def node(x: float, y: float, lines: list[tuple[str, str]]) -> str:
        out = []
        for base, cls, text in _lines(y, lines):
            attr = f' class="{cls}"' if cls else ""
            out.append(f'<text{attr} x="{_n(x + NODE_W / 2)}" y="{_n(base)}">{e(text)}</text>')
        return "".join(out)

    parts = [
        f'<svg class="flow steps" viewBox="0 0 {_n(width)} {_n(height)}" width="{_n(width)}" '
        f'height="{_n(height)}" role="img" aria-label="{e(label)}">'
    ]
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        x1, x2 = PATH_LEFT + (i - 1) * step + NODE_W, PATH_LEFT + i * step
        classes = "hop" if (a, b) in labels else "hop jump"
        parts.append(
            f'<g class="{classes}" data-from="{e(a)}" data-to="{e(b)}" data-step="{i}">'
            f"<title>{e(title(a, b))}</title>"
            f'<path d="M{_n(x1)} {_n(mid)} L{_n(x2 - ARROW * 1.6)} {_n(mid)}"/>'
            f'<polygon points="{_arrow(x2, mid, x1, mid)}"/></g>'
        )
    if nexts:
        sx = PATH_LEFT + at * step + NODE_W
        nx = PATH_LEFT + len(path) * step
        for j, name in enumerate(nexts):
            ny = PATH_TOP + j * NEXT_ROW + VISIT_H / 2
            bend = (nx - sx) / 2
            parts.append(
                f'<g class="hop next" data-from="{e(current)}" data-to="{e(name)}">'
                f"<title>{e(title(str(current), name))}</title>"
                f'<path d="M{_n(sx)} {_n(mid)} C{_n(sx + bend)} {_n(mid)} {_n(nx - bend)} {_n(ny)} '
                f'{_n(nx - ARROW * 1.6)} {_n(ny)}"/>'
                f'<polygon points="{_arrow(nx, ny, nx - bend, ny)}"/></g>'
            )
    for i, name in enumerate(path):
        x = PATH_LEFT + i * step
        classes = ["visit"]
        if i == at:
            classes.append("current")
            if not terminal:
                classes.append("breathe")
        halo = (
            f'<rect class="halo" x="{_n(x - 4)}" y="{_n(y0 - 4)}" width="{NODE_W + 8}" '
            f'height="{VISIT_H + 8}" rx="9"/>'
            if i == at
            else ""
        )
        parts.append(
            f'<g class="{" ".join(classes)}" data-state="{e(name)}" data-step="{i}">'
            f"<title>{e(name)}{' (now)' if i == at else ''}</title>"
            f'<rect x="{_n(x)}" y="{_n(y0)}" width="{NODE_W}" height="{VISIT_H}" rx="6" '
            f'class="box g-{e(group.get(name, "other"))}"/>{halo}'
            + node(x, y0, texts(name, meta if i == at else ""))
            + "</g>"
        )
    for j, name in enumerate(nexts):
        x, y = PATH_LEFT + len(path) * step, PATH_TOP + j * NEXT_ROW
        parts.append(
            f'<g class="next" data-state="{e(name)}"><title>{e(name)} (can come next)</title>'
            f'<rect x="{_n(x)}" y="{_n(y)}" width="{NODE_W}" height="{VISIT_H}" rx="6" class="box"/>'
            + node(x, y, texts(name))
            + "</g>"
        )
    parts.append("</svg>")
    return "".join(parts)
