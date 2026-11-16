import json
import shutil
import subprocess

import pytest

import lib.dashboard_flow as flow

CHART = flow.load_chart()
LAYOUT = flow.flowmap_layout(CHART)
NODES = {node["name"]: node for node in LAYOUT["nodes"]}
NW, NH = LAYOUT["node"]["w"], LAYOUT["node"]["h"]


def _inside(node, x, y):
    return node["x"] <= x < node["x"] + NW and node["y"] <= y < node["y"] + NH


def _cells(points):
    x, y = points[0]
    out = [(x, y)]
    for tx, ty in points[1:]:
        assert x == tx or y == ty, f"diagonal segment {points}"
        while (x, y) != (tx, ty):
            x += (tx > x) - (tx < x)
            y += (ty > y) - (ty < y)
            out.append((x, y))
    return out


def test_layout_is_json():
    assert json.loads(json.dumps(LAYOUT)) == LAYOUT


def test_every_state_has_a_node_inside_the_canvas():
    assert set(NODES) == {state["name"] for state in CHART["states"]}
    for node in NODES.values():
        assert 0 <= node["x"] * 4 and (node["x"] + NW) * 4 <= 1000, node
        assert 0 <= node["y"] * 4 and (node["y"] + NH) * 4 <= 352, node


def test_no_two_nodes_overlap():
    names = sorted(NODES)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            na, nb = NODES[a], NODES[b]
            apart = (na["x"] + NW <= nb["x"] or nb["x"] + NW <= na["x"]
                     or na["y"] + NH <= nb["y"] or nb["y"] + NH <= na["y"])
            assert apart, (a, b)


def test_every_chart_edge_has_a_route_that_crosses_no_node():
    keys = {f'{edge["from"]}>{edge["to"]}' for edge in CHART["edges"]}
    assert keys <= set(LAYOUT["routes"])
    for key, points in LAYOUT["routes"].items():
        for x, y in _cells(points):
            assert 0 <= x < 250 and 0 <= y < 88, (key, x, y)
            for name, node in NODES.items():
                assert not _inside(node, x, y), f"{key} crosses {name} at {(x, y)}"


def test_stop_states_sit_on_the_bottom_rail():
    stops = {"refused", "abandoned", "stuck", "blocked"}
    rail = LAYOUT["rail"]["y"]
    for name, node in NODES.items():
        assert node["stop"] == (name in stops), name
        if name in stops:
            assert node["y"] > rail
        elif node["row"] < 4:
            assert node["y"] + NH <= rail


def test_artboard_places_kept_and_new_states_take_free_rows():
    assert (NODES["review"]["col"], NODES["review"]["row"]) == (3, 0)
    assert (NODES["handed-off"]["col"], NODES["handed-off"]["row"]) == (5, 3)
    assert NODES["merge-queue"]["col"] == NODES["updating"]["col"] == 4
    assert NODES["merge-queue"]["row"] == 3


PLACES_BEFORE_BLOCKED_MOVED = {
    "intake": (0, 0), "questions": (0, 1), "waiting-max": (0, 2),
    "planning": (1, 0), "plan-check": (1, 1),
    "building": (2, 0), "fixing": (2, 1),
    "review": (3, 0), "capped": (3, 1),
    "ship": (4, 0), "awaiting-ok": (4, 1), "waiting-external": (4, 2),
    "merge-queue": (4, 3), "updating": (4, 4),
    "merged": (5, 0), "post-merge": (5, 1), "done": (5, 2), "handed-off": (5, 3),
    "refused": (0, 4), "abandoned": (1, 4), "stuck": (2, 4),
}


def test_blocked_moves_to_the_stop_rail_and_nothing_else_moves():
    blocked = NODES["blocked"]
    assert (blocked["col"], blocked["row"]) == (3, 4)
    assert blocked["stop"] and not blocked["end"]
    assert blocked["y"] > LAYOUT["rail"]["y"]
    others = {name: (node["col"], node["row"]) for name, node in NODES.items() if name != "blocked"}
    assert others == PLACES_BEFORE_BLOCKED_MOVED


STUB = r"""
var calls = {strokes: [], fills: [], strokeRects: 0, texts: []};
var ctx = {
  set fillStyle(v) { this._f = v; calls.fills.push(v); }, get fillStyle() { return this._f; },
  set strokeStyle(v) { this._s = v; calls.strokes.push(v); }, get strokeStyle() { return this._s; },
  fillRect: function () {}, strokeRect: function () { calls.strokeRects++; },
  fillText: function (s) { calls.texts.push(String(s)); },
  measureText: function (s) { return {width: s.length * 6}; },
  lineWidth: 1, font: '', textAlign: '', textBaseline: ''
};
var input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
drawFlowMap(ctx, input.layout, input.model, input.opts);
process.stdout.write(JSON.stringify(calls));
"""


def _draw(model, opts):
    node = shutil.which("node") or "/usr/bin/node"
    if not shutil.which(node):
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "-e", flow.FLOWMAP_SCRIPT + STUB],
        input=json.dumps({"layout": LAYOUT, "model": model, "opts": opts}),
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)


PATH = [
    ("intake", "script"), ("questions", "grok"), ("planning", "claude"), ("plan-check", "script"),
    ("building", "codex"), ("planning", "claude"), ("review", "codex"), ("fixing", "cursor"),
    ("stuck", "script"), ("fixing", "human"),
]


def _model():
    visits = [
        {"index": i, "state": state, "who": who, "off_chart": False,
         "current": i == len(PATH) - 1, "stop": NODES[state]["stop"]}
        for i, (state, who) in enumerate(PATH)
    ]
    return {"visits": visits, "state": "fixing", "next": flow.successors(CHART, "fixing")}


@pytest.mark.parametrize("opts", [
    {"t": 0, "reduced": False}, {"t": 0, "reduced": True}, {"t": 7.3, "reduced": False},
])
def test_draw_flowmap_paints_path_in_who_colours(opts):
    calls = _draw(_model(), opts)
    allowed = set(flow.FLOWMAP_TINT.values()) | {flow.STOP_RED}
    hops = sum(1 for a, b in zip(PATH, PATH[1:]) if a[0] != b[0])
    assert calls["strokeRects"] == hops
    assert set(calls["strokes"]) <= allowed
    assert flow.STOP_RED in calls["strokes"]
    assert flow.FLOWMAP_TINT["cursor"] in calls["strokes"]
    assert "#6b7399" in calls["fills"]
    assert "#5386a6" in calls["fills"]
    assert calls["texts"].count("\u00d72") == 2
    assert [str(n) for n in range(1, hops + 1)] == [x for x in calls["texts"] if x.isdigit()]


def test_script_is_safe_to_splice():
    for needle in ("url(", "src=", "data:", "=>", "let ", "const "):
        assert needle not in flow.FLOWMAP_SCRIPT, needle


def test_tint_is_keyed_by_who():
    assert set(flow.FLOWMAP_TINT) == {"script", "claude", "codex", "cursor", "grok", "human"}


HUMAN_WAITS = {"waiting-max", "questions", "awaiting-ok", "waiting-external"}


def test_bridges_before_the_first_row_add_no_human_wait_visit():
    jobs = [
        flow.JobStep("plan", "claude", 1000.0, None, None),
        flow.JobStep("build", "claude", 2000.0, None, None),
        flow.JobStep("fix", "claude", 3000.0, None, None),
        flow.JobStep("review", "codex", 4000.0, None, None),
    ]
    rows = [flow.Transition("building", "review", 4000.0, "set-state")]
    result = flow.job_flow(CHART, jobs, "review", 5000.0, rows, None)
    assert HUMAN_WAITS.isdisjoint(result["path"]), result["path"]


def test_a_lone_build_job_draws_the_start_hop_off_chart():
    jobs = [flow.JobStep("build", "claude", 1000.0, None, None)]
    result = flow.job_flow(CHART, jobs, None, 2000.0)
    assert result["path"] == ["intake", "building"]
    assert result["visits"][1]["off_chart"] is True


def test_a_bridge_never_passes_through_a_human_wait_state():
    names = [state["name"] for state in CHART["states"]]
    for a in names:
        for b in names:
            path = flow.chart_path(CHART, [a, b])
            assert HUMAN_WAITS.isdisjoint(path[1:-1]), (a, b, path)
