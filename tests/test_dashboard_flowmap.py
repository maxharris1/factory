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
        assert 0 <= node["y"] * 4 and (node["y"] + NH) * 4 <= 416, node


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
            assert 0 <= x < 250 and 0 <= y < 104, (key, x, y)
            for name, node in NODES.items():
                assert not _inside(node, x, y), f"{key} crosses {name} at {(x, y)}"


def test_stop_states_sit_on_the_bottom_rail():
    stops = {"refused", "abandoned", "stuck", "blocked"}
    rail = LAYOUT["rail"]["y"]
    for name, node in NODES.items():
        assert node["stop"] == (name in stops), name
        if name in stops:
            assert node["y"] > rail
        else:
            assert node["y"] + NH <= rail


SHIP_COLUMN = ["ship", "merge-queue", "updating", "awaiting-ok", "waiting-external"]
RAIL = ["refused", "abandoned", "stuck", "blocked"]


def _place(node):
    return node["col"], node["row"]


def test_ship_column_holds_five_states_above_the_stop_rail():
    assert flow.STOP_ROW == 5
    assert LAYOUT["grid"] == {"w": 250, "h": 104, "cell": 4}
    assert [_place(NODES[name]) for name in SHIP_COLUMN] == [(4, row) for row in range(5)]
    assert [_place(NODES[name]) for name in RAIL] == [(col, 5) for col in range(4)]
    assert not any(NODES[name]["stop"] for name in SHIP_COLUMN)


def _chart_with(state, group, edges):
    text = flow.CHART_PATH.read_text(encoding="utf-8")
    text = text.replace("  s22: capped\n", f"  s22: capped\n  s23: {state}\n")
    text = text.replace("  class s22 yellow\n", f"  class s22 yellow\n  class s23 {group}\n")
    text = text.replace("  s3 --> s3: CI red, fix\n", "  s3 --> s3: CI red, fix\n" + "".join(f"  {e}: go\n" for e in edges))
    chart = flow.parse_chart(text)
    assert state in {s["name"] for s in chart["states"]}
    return flow.flowmap_layout(chart)


def test_a_sixth_live_ship_state_grows_the_grid_and_the_rail_moves_down():
    layout = _chart_with("launch-pad", "teal", ["s14 --> s23", "s23 --> s8"])
    nodes = {node["name"]: node for node in layout["nodes"]}
    assert _place(nodes["launch-pad"]) == (4, 5)
    assert not nodes["launch-pad"]["stop"]
    assert [_place(nodes[name]) for name in RAIL] == [(col, 6) for col in range(4)]
    rail = layout["rail"]["y"]
    for node in nodes.values():
        if node["stop"]:
            assert node["y"] > rail, node["name"]
        else:
            assert node["y"] + NH <= rail, node["name"]
    assert layout["grid"]["h"] == 120


def test_an_unknown_stop_state_with_no_exit_goes_on_the_rail():
    layout = _chart_with("parked", "tan", ["s10 --> s23"])
    parked = next(node for node in layout["nodes"] if node["name"] == "parked")
    assert _place(parked) == (4, 5)
    assert parked["stop"] and parked["end"]
    assert parked["y"] > layout["rail"]["y"]
    assert layout["grid"] == LAYOUT["grid"]


def _box(node):
    return node["x"] * 4, node["y"] * 4, (node["x"] + NW) * 4, (node["y"] + NH) * 4


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_merge_queue_and_updating_wires_cross_no_other_node():
    ends = {"merge-queue", "updating"}
    edges = [(e["from"], e["to"]) for e in CHART["edges"] if {e["from"], e["to"]} & ends]
    assert len(edges) == 11
    for a, b in edges:
        for x, y in _cells(LAYOUT["routes"][f"{a}>{b}"]):
            for name, node in NODES.items():
                if name not in (a, b):
                    assert not _inside(node, x, y), f"{a}>{b} crosses {name} at {(x, y)}"


def test_no_node_covers_a_column_head_or_the_stop_label():
    label = LAYOUT["stop_label"]
    boxes = [(h["x"], 0, h["x"] + h["w"], 24) for h in LAYOUT["heads"]]
    boxes.append((label["x"], label["y"], label["x"] + 11 * 8, label["y"] + 14))
    for name, node in NODES.items():
        for box in boxes:
            assert not _overlap(_box(node), box), (name, box)


def test_no_live_wire_touches_the_stop_rail():
    rail = LAYOUT["rail"]["y"]
    live = [name for name, node in NODES.items() if not node["stop"]]
    for a in live:
        for b in live:
            key = f"{a}>{b}"
            if key in LAYOUT["routes"]:
                assert all(y != rail for _, y in _cells(LAYOUT["routes"][key])), key


STUB = r"""
var calls = {strokes: [], fills: [], strokeRects: 0, texts: [], fonts: {}};
var ctx = {
  set fillStyle(v) { this._f = v; calls.fills.push(v); }, get fillStyle() { return this._f; },
  set strokeStyle(v) { this._s = v; calls.strokes.push(v); }, get strokeStyle() { return this._s; },
  fillRect: function () {}, strokeRect: function () { calls.strokeRects++; },
  fillText: function (s) { calls.texts.push(String(s)); calls.fonts[String(s)] = this.font; },
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


def test_long_glosses_draw_a_size_smaller():
    fonts = _draw(_model(), {"t": 0, "reduced": True})["fonts"]
    assert fonts["approved, waiting to merge"].startswith("400 8px ")
    assert fonts["merging base in"].startswith("400 9px ")


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


def _halted():
    model = _model()
    model["visits"] = model["visits"][:9]
    model["visits"][-1]["current"] = True
    return {**model, "state": "stuck", "next": flow.successors(CHART, "stuck")}


def test_a_path_ending_in_a_stop_state_lies_still_on_a_red_checker():
    frames = [_draw(_halted(), {"t": t, "reduced": False}) for t in (0, 1.3, 7.3)]
    assert frames[0] == frames[1] == frames[2]
    assert "#542d39" in frames[0]["fills"] and flow.STOP_RED in frames[0]["fills"]
    assert "#b8e5ff" not in frames[0]["fills"], "no marching crest on a halted node"
    assert "#542d39" not in _draw(_model(), {"t": 0, "reduced": False})["fills"]


def test_a_tint_overrides_the_current_node_band():
    plain = _draw(_model(), {"t": 0, "reduced": True})["fills"]
    green = _draw(_model(), {"t": 0, "reduced": True, "tint": "#9ece6a"})["fills"]
    assert "rgb(158,206,106)" in green and "rgb(158,206,106)" not in plain
