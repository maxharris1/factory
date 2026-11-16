from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest

import lib.dashboard_flow as flow
import lib.dashboard_page as page
from lib.dashboard_pages import tickets as tickets_page
from lib.dashboard import collect_snapshot, render_pages
from tests import test_dashboard_layout as layout
from tests import test_dashboard_pixelstrip as pixelstrip
from tests.test_dashboard import NOW, add_job, add_ticket, make_db

CHART = Path(__file__).resolve().parents[1] / "model" / "chart.mmd"
PROJECTS = (("maxharris1/factory", "github"), ("maxharris1/cleffy", "github"), ("aut0-code/aut0", "linear"))
STUCK = ("maxharris1/cleffy", "200")
APPROVER = "maxharris1"
JOBS = {"planning": ("j-plan01", "plan", "claude", None),
        "building": ("j-build1", "build", "claude", '{"round":"1/1"}'),
        "review": ("j-review", "review", "codex", '{"round":"1/3"}'),
        "fixing": ("j-fix001", "fix", "claude", '{"round":"2/3"}')}
QUEUED = ("plan-check", "j-queue1")
VERDICT = ("post-merge", "queued-review")
BOARD = ["ticket-filter", "pipeline", "shipped", "tickets"]
IDS = layout.BAR + ["hud", *BOARD, "jobview", "flow-template"]
GLYPHS = {"Ask": ("twinkle", "#a9b1d6"), "Plan": ("field", "#a9b1d6"), "Build": ("fill", "#a9b1d6"),
          "Review": ("scan", "#a9b1d6"), "Ship": ("off", "#a9b1d6"), "Stop": ("static", "#f7768e")}


def _state_tickets() -> dict[str, tuple[str, str]]:
    chart = flow.load_chart(CHART)
    assert chart is not None
    found = {}
    for index, state in enumerate(item["name"] for item in chart["states"]):
        repo, tracker = PROJECTS[index % len(PROJECTS)]
        found[state] = (repo, f"AUT-{100 + index}" if tracker == "linear" else str(100 + index))
    return found


def board_snapshot(tmp_path: Path) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    tickets = _state_tickets()
    trackers = dict(PROJECTS)
    with sqlite3.connect(db) as connection:
        for index, (state, (repo, ticket_id)) in enumerate(tickets.items()):
            add_ticket(connection, repo, ticket_id, tracker=trackers[repo], state=state,
                       review_round=index % 4)
        add_ticket(connection, *STUCK, state="building", stuck=1,
                   stuck_reason="cancelled: superseded by sanctioned intake (Factory 2026-11-13)")
        repo, ticket_id = tickets["awaiting-ok"]
        connection.execute(
            "INSERT INTO ship_holds (repo, ticket_id, head, approver, reasons, approve_comment_id, held_at) "
            "VALUES (?, ?, 'abc123', ?, 'label', 'c-hold', ?)", (repo, ticket_id, APPROVER, NOW - 300))
        layout._slots_table(connection)
        for state, (job_id, action, recipient, detail) in JOBS.items():
            repo, ticket_id = tickets[state]
            add_job(connection, job_id, action=action, recipient=recipient, state="running", repo=repo,
                    ticket_id=ticket_id, created_at=NOW - 246, last_activity_at=NOW - 6, detail=detail)
        connection.execute("INSERT INTO device_slots VALUES ('j-review', 'pc', 'codex', 'codex-bot', ?)",
                           (NOW - 460,))
        repo, ticket_id = tickets[QUEUED[0]]
        add_job(connection, QUEUED[1], state="queued", repo=repo, ticket_id=ticket_id, created_at=NOW - 120)
        repo, ticket_id = tickets[VERDICT[0]]
        connection.execute("INSERT INTO handled_comments (repo, ticket_id, comment_id, outcome) "
                           "VALUES (?, ?, 'c-verdict', ?)", (repo, ticket_id, VERDICT[1]))
    log = tmp_path / "poller.log"
    log.write_text("cycle\n", encoding="utf-8")
    return collect_snapshot(db, log, now=NOW, chart_path=CHART)


class _Tree(HTMLParser):
    VOID = {"meta", "br", "img", "input", "hr", "link"}

    def __init__(self, markup: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "root", "attrs": {}, "kids": []}
        self._stack = [self.root]
        self.feed(markup)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        node = {"tag": tag, "attrs": {k: "" if v is None else v for k, v in attrs}, "kids": []}
        self._stack[-1]["kids"].append(node)
        if tag not in self.VOID:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        while len(self._stack) > 1 and self._stack.pop()["tag"] != tag:
            pass

    def handle_data(self, data: str) -> None:
        self._stack[-1]["kids"].append(data)


def _dom(markup: str) -> dict:
    return _Tree(markup).root


def _all(node: dict, pred) -> list[dict]:
    found = []
    for kid in node["kids"]:
        if isinstance(kid, dict):
            if pred(kid):
                found.append(kid)
            found += _all(kid, pred)
    return found


def _cls(name: str):
    return lambda node: name in node["attrs"].get("class", "").split()


def _attr(name: str, value: str | None = None):
    return lambda node: name in node["attrs"] and (value is None or node["attrs"][name] == value)


def _one(node: dict, pred) -> dict:
    (found,) = _all(node, pred)
    return found


def _text(node: dict) -> str:
    return "".join(kid if isinstance(kid, str) else _text(kid) for kid in node["kids"])


def _inner(html: str, ident: str) -> str:
    outer = layout._section(layout._no_script(html), ident)
    return outer[outer.index(">") + 1 : outer.rindex("</")]


def _server(snapshot: dict) -> dict[str, str]:
    html = render_pages(snapshot)["job.html"]
    return {ident: _inner(html, ident) for ident in BOARD}


HARNESS = layout.HARNESS.replace("let nowMs = 1800000003000;", f"let nowMs = {int(NOW * 1000)};").replace(
    "    } else if (kind === 'record') {",
    "    } else if (kind === 'press') {\n"
    "      const node = find(body, n => n.attrs[arg[0]] === arg[1]);\n"
    "      if (!node) throw new Error('nothing to press: ' + JSON.stringify(arg));\n"
    "      fire(node, 'click');\n"
    "    } else if (kind === 'snap') {\n"
    "      steps.push({snap: Object.fromEntries(arg.map(id => [id, ids[id].children.map(c => c.html()).join('')])),\n"
    "        hidden: Object.fromEntries(arg.map(id => [id, !!ids[id].hidden]))});\n"
    "    } else if (kind === 'focus') {\n"
    "      find(body, n => n.attrs[arg[0]] === arg[1]).focus();\n"
    "    } else if (kind === 'hashchange') {\n"
    "      windowListeners.hashchange.forEach(fn => fn());\n"
    "    } else if (kind === 'focused') {\n"
    "      const node = document.activeElement;\n"
    "      steps.push({focused: node && node.isConnected ? [node.tagName, node.attrs[arg]] : 'BODY'});\n"
    "    } else if (kind === 'record') {",
).replace(
    "global.addEventListener = () => {};",
    "const windowListeners = {hashchange: []};\n"
    "global.addEventListener = (type, fn) => { if (windowListeners[type]) windowListeners[type].push(fn); };",
)


def _script(tmp_path: Path, snapshot: dict, actions: list | None = None, page_from: dict | None = None) -> dict:
    assert "kind === 'press'" in HARNESS and f"nowMs = {int(NOW * 1000)};" in HARNESS
    assert "windowListeners[type].push" in HARNESS
    html = render_pages(page_from or snapshot)["job.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    (tmp_path / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        [layout.NODE, str(tmp_path / "harness.js"), str(tmp_path / "page.js"), str(tmp_path / "status.json"),
         "job", "", json.dumps(IDS), json.dumps(actions or [])],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _key(row: dict) -> str:
    return f"{row['repo']}#{row['ticket_id']}"


def _placed(markup: str) -> list[str]:
    return [node["attrs"]["data-ticket"] for node in _all(_dom(markup), _attr("data-ticket"))]


def _number(text: str) -> int:
    return int(re.search(r"\d+", text).group(0))


def _card(markup: str, key: str) -> dict:
    return _one(_dom(markup), lambda n: n["attrs"].get("data-ticket") == key)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> dict:
    return board_snapshot(tmp_path_factory.mktemp("board"))


def test_every_chart_state_has_exactly_one_phase_from_its_legend_group() -> None:
    chart = flow.load_chart(CHART)
    phases = flow.ticket_phases(chart)
    assert sorted(phases) == sorted(state["name"] for state in chart["states"])
    legend = {item["group"]: item["name"] for item in chart["legend"]}
    for state in chart["states"]:
        name = state["name"]
        if name == "capped":
            assert phases[name] == "Stop"
        elif name in ("handed-off", "merged", "done"):
            assert phases[name] == "Shipped"
        else:
            assert phases[name] == legend[state["group"]], name
    assert phases["stuck"] == "Stop"
    assert [s for s, p in phases.items() if p == "Ship"] == [
        "ship", "awaiting-ok", "merge-queue", "waiting-external", "updating", "post-merge"]
    assert [s for s, p in phases.items() if p == "Review"] == ["review"]


def test_a_new_teal_chart_state_joins_ship_with_no_code_change() -> None:
    text = CHART.read_text(encoding="utf-8")
    text = text.replace("  s21: capped\n", "  s21: capped\n  s22: launch-pad\n")
    text = text.replace("  class s21 yellow\n", "  class s21 yellow\n  class s22 teal\n")
    text = text.replace("  s3 --> s3: CI red, fix\n", "  s3 --> s3: CI red, fix\n  s14 --> s22: go\n")
    phases = flow.ticket_phases(flow.parse_chart(text))
    assert phases["launch-pad"] == "Ship"


def test_stuck_and_unknown_tickets_go_to_stop() -> None:
    phases = flow.ticket_phases(flow.load_chart(CHART))
    assert flow.ticket_phase(phases, "building", False) == "Build"
    assert flow.ticket_phase(phases, "building", True) == "Stop"
    assert flow.ticket_phase(phases, "other", False) == "Stop"
    assert flow.ticket_phases(None) == {}


def test_the_fixture_covers_every_chart_state(snapshot: dict) -> None:
    chart = flow.load_chart(CHART)
    states = Counter(row["state"] for row in snapshot["tickets"])
    assert set(states) == {state["name"] for state in chart["states"]}
    assert len({row["repo"] for row in snapshot["tickets"]}) == 3
    assert {row["action"] for row in snapshot["running"]} == {"plan", "build", "review", "fix"}
    assert len(snapshot["queued"]) == 1


def test_server_board_places_every_ticket_once(snapshot: dict) -> None:
    server = _server(snapshot)
    placed = _placed(server["pipeline"] + server["shipped"])
    assert sorted(placed) == sorted(_key(row) for row in snapshot["tickets"])
    assert len(placed) == len(set(placed))
    stuck = "#".join(STUCK)
    stopped = _one(_dom(server["shipped"]), _attr("data-phase", "Stop"))
    build = _one(_dom(server["pipeline"]), _attr("data-phase", "Build"))
    assert stuck in _placed_in(stopped) and stuck not in _placed_in(build)


def _placed_in(node: dict) -> list[str]:
    return [found["attrs"]["data-ticket"] for found in _all(node, _attr("data-ticket"))]


@layout.needs_node
def test_script_board_places_every_ticket_once_and_matches_the_server(tmp_path: Path, snapshot: dict) -> None:
    out = _script(tmp_path, snapshot)
    placed = _placed(out["html"]["pipeline"] + out["html"]["shipped"])
    assert sorted(placed) == sorted(_key(row) for row in snapshot["tickets"])
    server = _server(snapshot)
    for ident in BOARD:
        assert out["html"][ident] == server[ident], ident
    assert out["hidden"]["tickets"] and not out["hidden"]["pipeline"]


def _check_counts(markup: dict[str, str], rows: list[dict]) -> None:
    pipeline, shipped = _dom(markup["pipeline"]), _dom(markup["shipped"])
    in_flight = 0
    for column in _all(pipeline, _cls("tk-col")):
        cards = _all(column, _cls("tk-card"))
        assert _number(_text(_one(column, _cls("tk-count")))) == len(cards)
        in_flight += len(cards)
    count = _text(_one(pipeline, _cls("count")))
    assert count == f"{len(rows)} tracked · {in_flight} in flight"
    tiles = _all(shipped, _cls("tk-tile"))
    assert _text(_one(shipped, _cls("count"))) == f"{len(tiles)} tickets"
    for group in _all(shipped, _cls("tk-sub")):
        shown = len(_all(group, _attr("data-ticket")))
        assert _text(_one(group, _cls("tk-label"))).endswith(f"({shown})")
    assert sorted(_placed(markup["pipeline"] + markup["shipped"])) == sorted(_key(r) for r in rows)


def _check_buttons(markup: str, rows: list[dict], pressed: str) -> None:
    buttons = _all(_dom(markup), _attr("data-project"))
    counts = Counter(row["repo"] for row in rows)
    assert _text(buttons[0]) == f"all {len(rows)}" and buttons[0]["attrs"]["data-project"] == ""
    assert {b["attrs"]["data-project"]: _text(b) for b in buttons[1:]} == {
        repo: f"{repo.rsplit('/', 1)[-1]} {n}" for repo, n in counts.items()}
    assert [b["attrs"]["data-project"] for b in buttons if b["attrs"]["aria-pressed"] == "true"] == [pressed]


def test_server_counts_equal_the_rendered_cards(snapshot: dict) -> None:
    server = _server(snapshot)
    _check_counts(server, snapshot["tickets"])
    _check_buttons(server["ticket-filter"], snapshot["tickets"], "")
    buttons = _all(_dom(server["ticket-filter"]), _attr("data-project"))
    counts = [_number(_text(b)) for b in buttons[1:]]
    assert counts == sorted(counts, reverse=True)


@layout.needs_node
def test_script_project_filter_recomputes_every_count(tmp_path: Path, snapshot: dict) -> None:
    rows = snapshot["tickets"]
    repos = sorted({row["repo"] for row in rows})
    actions: list = [["snap", BOARD]]
    for repo in repos:
        actions += [["press", ["data-project", repo]], ["snap", BOARD]]
    actions += [["press", ["data-view", "table"]], ["snap", BOARD], ["press", ["data-project", ""]],
                ["snap", BOARD]]
    snaps = [step for step in _script(tmp_path, snapshot, actions)["steps"] if "snap" in step]
    _check_counts(snaps[0]["snap"], rows)
    for repo, step in zip(repos, snaps[1:]):
        _check_counts(step["snap"], [row for row in rows if row["repo"] == repo])
        _check_buttons(step["snap"]["ticket-filter"], rows, repo)
    table, cleared = snaps[-2], snaps[-1]
    assert table["hidden"] == {"ticket-filter": False, "pipeline": True, "shipped": True, "tickets": False}
    last = [row for row in rows if row["repo"] == repos[-1]]
    assert _text(_one(_dom(table["snap"]["tickets"]), _cls("count"))) == f"{len(last)} tracked"
    assert len(_all(_dom(table["snap"]["tickets"]), lambda n: n["tag"] == "tr")) == len(last) + 1
    assert cleared["hidden"]["tickets"] is False
    assert len(_all(_dom(cleared["snap"]["tickets"]), lambda n: n["tag"] == "tr")) == len(rows) + 1


def _canvas(node: dict) -> dict:
    return _one(node, lambda n: n["tag"] == "canvas")["attrs"]


def test_phase_glyphs_follow_design_tokens_section_5(snapshot: dict) -> None:
    server = _server(snapshot)
    columns = _all(_dom(server["pipeline"]), _cls("tk-col"))
    assert [c["attrs"]["data-phase"] for c in columns] == ["Ask", "Plan", "Build", "Review", "Ship"]
    stopped = _one(_dom(server["shipped"]), _attr("data-phase", "Stop"))
    for node in columns + [stopped]:
        effect, tint = GLYPHS[node["attrs"]["data-phase"]]
        glyph = _canvas(_one(node, _cls("tk-glyph")))
        assert (glyph["data-px"], glyph["data-tint"], glyph["data-cell"]) == (effect, tint, "2")
        assert glyph["style"].endswith("height:16px")
        assert ("data-progress" in glyph) == (effect == "fill")
        assert ("data-done" in glyph) == (node["attrs"]["data-phase"] == "Ship")
    assert _canvas(_one(columns[2], _cls("tk-glyph")))["data-progress"] == "0.600"
    assert _canvas(_one(columns[4], _cls("tk-glyph")))["data-done"] == "true"


def test_card_strips_show_the_step_and_bot_and_never_move_when_stopped_or_waiting(snapshot: dict) -> None:
    server = _server(snapshot)
    tickets = _state_tickets()
    board = server["pipeline"] + server["shipped"]
    steps = {"planning": ("field", "claude"), "building": ("fill", "claude"), "review": ("scan", "codex"),
             "fixing": ("patch", "claude")}
    for state, (effect, bot) in steps.items():
        strip = _canvas(_card(board, "#".join(tickets[state])))
        assert (strip["data-px"], strip["data-tint"], strip["style"].endswith("height:8px")) == (effect, bot, True)
    stopped = _one(_dom(server["shipped"]), _attr("data-phase", "Stop"))
    cards = _all(stopped, _cls("tk-card"))
    assert len(cards) == len([s for s, p in flow.ticket_phases(flow.load_chart(CHART)).items() if p == "Stop"]) + 1
    for card in cards:
        assert "tk-stop" in card["attrs"]["class"].split()
        assert (_canvas(card)["data-px"], _canvas(card)["data-tint"]) == ("static", "#f7768e")
    waiting = _card(board, "#".join(tickets["awaiting-ok"]))
    assert (_canvas(waiting)["data-px"], _canvas(waiting)["data-tint"]) == ("static", "#ff9e64")
    assert not page.PX_EFFECTS["static"]
    assert f"waiting on {APPROVER}" in _text(waiting)
    stuck = _card(board, "#".join(STUCK))
    assert "stuck" in _text(_one(stuck, _cls("chip")))
    assert "cancelled: superseded by sanctioned intake" in _text(stuck)


def test_cards_show_round_bot_device_time_and_link_their_job(snapshot: dict) -> None:
    server = _server(snapshot)
    tickets = _state_tickets()
    board = server["pipeline"]
    fixing = _card(board, "#".join(tickets["fixing"]))
    pips = _one(fixing, _cls("tk-pips"))
    assert [kid["attrs"].get("class", "") for kid in pips["kids"]] == ["done", "now", ""]
    text = _text(fixing)
    for needle in ("fixing", "round 2/3", "claude", "@ box", "T+ 4m 06s", "github"):
        assert needle in text, needle
    assert _one(fixing, lambda n: n["tag"] == "a")["attrs"]["href"] == "job.html#job-j-fix001"
    assert "@ pc" in _text(_card(board, "#".join(tickets["review"])))
    planning = _card(board, "#".join(tickets["planning"]))
    assert "round —" in _text(planning) and not _all(planning, _cls("tk-pips"))
    queued = _card(board, "#".join(tickets[QUEUED[0]]))
    assert _one(queued, lambda n: n["tag"] == "a")["attrs"]["href"] == f"job.html#job-{QUEUED[1]}"
    assert "queued" in _text(queued) and not _all(queued, lambda n: n["tag"] == "canvas")
    idle = _card(board, "#".join(tickets[VERDICT[0]]))
    assert not _all(idle, lambda n: n["tag"] in ("a", "canvas"))
    assert "no job running" in _text(idle)
    assert _text(_one(idle, _cls("chip"))) == "routed" and VERDICT[1] in _text(idle)
    assert {_text(node) for node in _all(_dom(board), _cls("tk-tag"))} == {"github", "linear"}


def test_shelf_tiles_carry_their_revise_rounds(snapshot: dict) -> None:
    shipped = _dom(_server(snapshot)["shipped"])
    labels = [_text(_one(g, _cls("tk-label"))) for g in _all(shipped, _cls("tk-sub"))]
    assert labels == ["handed-off · ready for review (1)", "merged (1)", "done · shipped (1)", "stopped (6)"]
    rounds = {row["state"]: row["round"] for row in snapshot["tickets"]}
    for tile in _all(shipped, _cls("tk-tile")):
        assert "#" in _text(_one(tile, _cls("tk-tn")))
    states = [g["attrs"]["data-state"] for g in _all(shipped, _attr("data-state"))]
    for state, group in zip(states, _all(shipped, _attr("data-state"))):
        assert _text(_one(group, _cls("tk-r"))) == f"r{rounds[state]}"


def test_empty_columns_are_dashed_boxes_with_their_copy(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    (tmp_path / "poller.log").write_text("", encoding="utf-8")
    snapshot = collect_snapshot(db, tmp_path / "poller.log", now=NOW, chart_path=CHART)
    server = _server(snapshot)
    empties = [_text(n) for n in _all(_dom(server["pipeline"]), _cls("tk-empty"))]
    assert empties == ["nothing waiting on questions", "nothing in plan", "nothing in build",
                       "nothing in review", "nothing in ship"]
    stopped = _one(_dom(server["shipped"]), _attr("data-phase", "Stop"))
    assert _text(_one(stopped, _cls("tk-none"))) == "nothing refused, stuck, abandoned, blocked or capped"
    assert _text(_one(_dom(server["pipeline"]), _cls("count"))) == "0 tracked · 0 in flight"


def test_without_a_chart_the_board_says_so_and_the_table_lists_every_ticket(tmp_path: Path) -> None:
    snapshot = board_snapshot(tmp_path)
    snapshot["statechart"] = None
    server = _server(snapshot)
    assert "statechart unavailable" in _text(_dom(server["pipeline"]))
    assert not _placed(server["pipeline"] + server["shipped"])
    rows = _all(_dom(server["tickets"]), lambda n: n["tag"] == "tr")
    assert len(rows) == len(snapshot["tickets"]) + 1


@layout.needs_node
def test_script_without_a_chart_matches_the_server(tmp_path: Path) -> None:
    snapshot = board_snapshot(tmp_path)
    snapshot["statechart"] = None
    out = _script(tmp_path, snapshot)
    server = _server(snapshot)
    for ident in BOARD:
        assert out["html"][ident] == server[ident], ident


@layout.needs_node
def test_script_board_recovers_when_the_chart_returns(tmp_path: Path, snapshot: dict) -> None:
    chartless = {**snapshot, "statechart": None}
    out = _script(tmp_path, snapshot, page_from=chartless)
    server = _server(snapshot)
    for ident in BOARD:
        assert out["html"][ident] == server[ident], ident


def _recased(snapshot: dict) -> dict:
    def upper(rows: list) -> list:
        return [{**row, "repo": row["repo"].upper()} for row in rows]

    return {
        **snapshot,
        "running": upper(snapshot["running"]),
        "queued": upper(snapshot["queued"]),
        "poller": {**snapshot["poller"], "verdicts": upper(snapshot["poller"]["verdicts"])},
        "stuck": {**snapshot["stuck"], "tickets": upper(snapshot["stuck"]["tickets"])},
    }


def test_server_cards_join_jobs_whatever_the_repo_case(snapshot: dict) -> None:
    recased, server = _server(_recased(snapshot)), _server(snapshot)
    assert "no job running" not in _text(_card(server["pipeline"], _key(snapshot["running"][0])))
    for ident in ("pipeline", "shipped"):
        assert recased[ident] == server[ident], ident


@layout.needs_node
def test_script_cards_join_jobs_whatever_the_repo_case(tmp_path: Path, snapshot: dict) -> None:
    out, server = _script(tmp_path, _recased(snapshot)), _server(snapshot)
    for ident in ("pipeline", "shipped"):
        assert out["html"][ident] == server[ident], ident


@layout.needs_node
def test_filter_buttons_keep_focus_across_renders(tmp_path: Path, snapshot: dict) -> None:
    repo = snapshot["tickets"][0]["repo"]
    actions = [["focus", ["data-project", repo]], ["press", ["data-project", repo]], ["focused", "data-project"],
               ["focus", ["data-view", "table"]], ["hashchange", None], ["focused", "data-view"],
               ["snap", ["pipeline"]]]
    steps = _script(tmp_path, snapshot, actions)["steps"]
    assert [step["focused"] for step in steps if "focused" in step] == [["button", repo], ["button", "table"]]
    assert steps[-1]["hidden"] == {"pipeline": False}


def _strip_attrs(markup: str) -> list[dict]:
    return [node["attrs"] for node in _all(_dom(markup), lambda n: n["tag"] == "canvas")]


@layout.needs_node
@pytest.mark.parametrize("reduced", [True, False])
def test_reduced_motion_draws_each_board_strip_once(tmp_path: Path, snapshot: dict, reduced: bool) -> None:
    server = _server(snapshot)
    strips = _strip_attrs(server["pipeline"] + server["shipped"])
    assert any(s.get("data-done") == "true" for s in strips)
    out = pixelstrip._node(tmp_path, r"""
      found = """ + json.dumps(strips) + r""".map(a => canvas(a, 160, 16));
      PX.bind();
      for (let t = 1050; t <= 4000; t += 50) frame(t);
      out.draws = found.map(draws);
      out.done = found.map(el => el.getAttribute('data-done'));
      out.pending = rafQueue.length;
    """, reduced=reduced)
    ship = out["draws"][out["done"].index("true")]
    assert ship == 1
    if reduced:
        assert out["draws"] == [1] * len(strips)
        assert out["pending"] == 0
    else:
        assert max(out["draws"]) > 1 and out["pending"] == 1


@layout.needs_node
def test_script_strip_with_cell_and_done_is_the_server_strip(tmp_path: Path) -> None:
    calls = [("phase-Ship", {"effect": "off", "tint": "#a9b1d6", "seed": 75, "cell": 2, "done": True, "height": 16}),
             ("phase-Build", {"effect": "fill", "tint": "#a9b1d6", "progress": 0.6, "cell": 2, "height": 16})]
    out = pixelstrip._node(tmp_path, "out.calls = " + json.dumps(calls) +
                           ".map(c => serialize(PX.strip(c[0], c[1])));")
    assert out["calls"] == [page.px_strip(key, **kwargs) for key, kwargs in calls]


def test_the_table_is_on_job_html_only(snapshot: dict) -> None:
    pages = render_pages(snapshot)
    assert 'id="tickets"' in pages["job.html"] and 'id="tickets"' not in pages["activity.html"]
    assert 'id="jobs"' not in pages["job.html"]
    assert re.search(r'<section class="panel" id="tickets" aria-label="Tickets" hidden>', pages["job.html"])
    assert "@media (scripting:none){#tickets[hidden]{display:block !important}}" in page.CSS


def _gh_hrefs(node: dict) -> list[str]:
    links = _all(node, lambda n: n["tag"] == "a" and n["attrs"].get("href", "").startswith("https://github.com/"))
    assert all(a["attrs"]["rel"] == "noopener noreferrer" and a["attrs"]["target"] == "_blank" for a in links)
    return [a["attrs"]["href"] for a in links]


def _gh_snapshot(snapshot: dict) -> tuple[dict, list[str]]:
    snap = json.loads(json.dumps(snapshot))
    found = tickets_page.board(snap)
    keys = [_key(c["ticket"]) for col in found["columns"] for c in col["cards"]][:4]
    rows = {_key(row): row for row in snap["tickets"]}
    issue, no_pr, linear, bad = (rows[key] for key in keys)
    issue.update(source="#563", pr="601")
    no_pr.update(source="#564", pr=None)
    linear.update(source="ABC-12", pr="602")
    bad.update(repo="evil/repo/x", source="#7", pr="603")
    return snap, [_key(row) for row in (issue, no_pr, linear, bad)]


def _check_gh(markup: str, keys: list[str], snap: dict) -> None:
    rows = {_key(row): row for row in snap["tickets"]}
    issue, no_pr, linear, bad = keys
    assert _gh_hrefs(_card(markup, issue)) == [
        f"https://github.com/{rows[issue]['repo']}/issues/563", f"https://github.com/{rows[issue]['repo']}/pull/601"]
    assert _gh_hrefs(_card(markup, no_pr)) == [f"https://github.com/{rows[no_pr]['repo']}/issues/564"]
    assert _gh_hrefs(_card(markup, linear)) == [f"https://github.com/{rows[linear]['repo']}/pull/602"]
    assert _gh_hrefs(_card(markup, bad)) == []


def test_cards_link_their_github_issue_and_pr(snapshot: dict) -> None:
    snap, keys = _gh_snapshot(snapshot)
    _check_gh(_server(snap)["pipeline"], keys, snap)


@layout.needs_node
def test_script_cards_link_github_like_the_server(tmp_path: Path, snapshot: dict) -> None:
    snap, keys = _gh_snapshot(snapshot)
    out = _script(tmp_path, snap, page_from=snap)
    _check_gh(out["html"]["pipeline"], keys, snap)
    assert out["html"]["pipeline"] == _server(snap)["pipeline"]
