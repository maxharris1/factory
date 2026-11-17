from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest

import lib.dashboard_flow as flow
import lib.dashboard_page as page
from lib.dashboard_pages import tickets as tickets_page
from lib.dashboard_pages.common import ticket_page_href
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
COLUMNS = ["Ask", "Plan", "Build", "Review", "Ship", "Stop"]
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
    return layout.run_harness(tmp_path, HARNESS, script, snapshot, "job", IDS, actions or [])


def _key(row: dict) -> str:
    return f"{row['repo']}#{row['ticket_id']}"


def _placed(markup: str) -> list[str]:
    return [node["attrs"]["data-ticket"] for node in _all(_dom(markup), _attr("data-ticket"))]


def _number(text: str) -> int:
    return int(re.search(r"\d+", text).group(0))


def _job_link(node: dict) -> bool:
    return node["tag"] == "a" and node["attrs"]["href"].startswith("job.html")


def _id_link(card: dict) -> dict:
    first = _one(card, _cls("tk-id"))["kids"][0]
    assert first["tag"] == "a", first
    return first


def _page_of(repo_ticket: tuple[str, str]) -> str:
    return "ticket.html#t=" + "%23".join(repo_ticket).replace("/", "%2F")


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
    stopped = _one(_dom(server["pipeline"]), _attr("data-phase", "Stop"))
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
    opened = 0
    for column in _all(pipeline, _cls("tk-col")):
        cards = _all(column, _cls("tk-card"))
        assert _number(_text(_one(column, _cls("tk-count")))) == len(cards)
        opened += len(cards)
    tiles = _all(shipped, _cls("tk-tile"))
    assert len(rows) == opened + len(tiles)
    count = _text(_one(pipeline, _cls("count")))
    assert count == f"{len(rows)} tracked · {opened} open · {len(tiles)} shipped"
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
    assert [c["attrs"]["data-phase"] for c in columns] == COLUMNS
    for node in columns:
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
    stopped = _one(_dom(server["pipeline"]), _attr("data-phase", "Stop"))
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


def test_cards_show_round_bot_device_time_and_link_their_ticket_page(snapshot: dict) -> None:
    server = _server(snapshot)
    tickets = _state_tickets()
    board = server["pipeline"]
    fixing = _card(board, "#".join(tickets["fixing"]))
    pips = _one(fixing, _cls("tk-pips"))
    assert [kid["attrs"].get("class", "") for kid in pips["kids"]] == ["done", "now", ""]
    text = _text(fixing)
    for needle in ("fixing", "round 2/3", "claude", "@ box", "T+ 4m 06s", "github"):
        assert needle in text, needle
    assert _id_link(fixing)["attrs"]["href"] == _page_of(tickets["fixing"])
    assert "@ pc" in _text(_card(board, "#".join(tickets["review"])))
    planning = _card(board, "#".join(tickets["planning"]))
    assert "round —" in _text(planning) and not _all(planning, _cls("tk-pips"))
    queued = _card(board, "#".join(tickets[QUEUED[0]]))
    assert _id_link(queued)["attrs"]["href"] == _page_of(tickets[QUEUED[0]])
    assert "queued" in _text(queued) and not _all(queued, lambda n: n["tag"] == "canvas")
    idle = _card(board, "#".join(tickets[VERDICT[0]]))
    assert not _all(idle, lambda n: n["tag"] == "canvas")
    assert _id_link(idle)["attrs"]["href"] == _page_of(tickets[VERDICT[0]])
    assert not _all(_dom(board), _job_link)
    assert "no job running" not in _text(idle)
    assert _text(_one(idle, _cls("chip"))) == "routed" and VERDICT[1] in _text(idle)
    assert {_text(node) for node in _all(_dom(board), _cls("tk-tag"))} == {"github", "linear"}


def test_shelf_tiles_carry_their_revise_rounds(snapshot: dict) -> None:
    shipped = _dom(_server(snapshot)["shipped"])
    labels = [_text(_one(g, _cls("tk-label"))) for g in _all(shipped, _cls("tk-sub"))]
    assert labels == ["handed-off · ready for review (1)", "merged (1)", "done · shipped (1)"]
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
    _check_empty(_server(snapshot))


def _check_empty(markup: dict[str, str]) -> None:
    pipeline = _dom(markup["pipeline"])
    empties = [_text(n) for n in _all(pipeline, _cls("tk-empty"))]
    assert empties == ["nothing waiting on questions", "nothing in plan", "nothing in build",
                       "nothing in review", "nothing in ship", "nothing stopped"]
    stop = _one(pipeline, _attr("data-phase", "Stop"))
    assert _one(stop, _cls("tk-count"))["attrs"]["class"] == "tk-count"
    assert _text(_one(pipeline, _cls("count"))) == "0 tracked · 0 open · 0 shipped"
    assert not _all(_dom(markup["shipped"]), _attr("data-phase"))


@layout.needs_node
def test_script_empty_columns_match_the_server(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    (tmp_path / "poller.log").write_text("", encoding="utf-8")
    snapshot = collect_snapshot(db, tmp_path / "poller.log", now=NOW, chart_path=CHART)
    out = _script(tmp_path, snapshot)
    _check_empty(out["html"])
    for ident in BOARD:
        assert out["html"][ident] == _server(snapshot)[ident], ident


def _check_stopped_column(markup: dict[str, str], rows: list[dict]) -> None:
    pipeline = _dom(markup["pipeline"])
    columns = _all(pipeline, _cls("tk-col"))
    assert [c["attrs"]["data-phase"] for c in columns] == COLUMNS
    assert [_text(_one(c, _cls("tk-name"))) for c in columns] == [*COLUMNS[:-1], "Stopped"]
    states = {c["attrs"]["data-phase"]: _text(_one(c, _cls("tk-states"))).split(" · ") for c in columns}
    assert states["Review"] == ["review"]
    assert "capped" in states["Stop"]
    stop = _one(pipeline, _attr("data-phase", "Stop"))
    stopped = {_key(r) for r in rows if r["stuck"] or r["state"] in states["Stop"]}
    assert {_key(r) for r in rows if r["state"] == "capped"} <= stopped
    assert sorted(_placed_in(stop)) == sorted(stopped)
    count = _one(stop, _cls("tk-count"))
    assert count["attrs"]["class"].split() == (["tk-count", "tk-stopped"] if stopped else ["tk-count"])
    for column in columns[:-1]:
        assert _one(column, _cls("tk-count"))["attrs"]["class"] == "tk-count"
    assert not _all(_dom(markup["shipped"]), _attr("data-phase"))


def _check_id_links(markup: str, rows: list[dict]) -> None:
    by_key = {_key(row): row for row in rows}
    cards = _all(_dom(markup), _cls("tk-card"))
    assert cards
    for card in cards:
        row = by_key[card["attrs"]["data-ticket"]]
        link = _id_link(card)
        assert link["attrs"]["href"] == ticket_page_href(row), card["attrs"]["data-ticket"]
        assert _text(link) == f"{row['repo'].rsplit('/', 1)[-1]}#{row['ticket_id']}"


def test_stopped_is_the_sixth_column_with_every_stuck_and_capped_ticket(snapshot: dict) -> None:
    assert any(r["state"] == "capped" for r in snapshot["tickets"])
    _check_stopped_column(_server(snapshot), snapshot["tickets"])
    assert ".tk-count.tk-stopped{font-weight:600;color:var(--bad)}" in page.CSS


@layout.needs_node
def test_script_stopped_column_matches_the_server(tmp_path: Path, snapshot: dict) -> None:
    out = _script(tmp_path, snapshot)
    _check_stopped_column(out["html"], snapshot["tickets"])
    assert out["html"]["pipeline"] == _server(snapshot)["pipeline"]


def test_every_card_id_links_its_ticket_page(snapshot: dict) -> None:
    _check_id_links(_server(snapshot)["pipeline"], snapshot["tickets"])


@layout.needs_node
def test_script_card_ids_link_their_ticket_page(tmp_path: Path, snapshot: dict) -> None:
    _check_id_links(_script(tmp_path, snapshot)["html"]["pipeline"], snapshot["tickets"])


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
    assert "@media (scripting:none){#tickets[hidden]{display:block !important}#ticket-filter{display:none}}" in page.CSS


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


SIGNED_IN = HARNESS.replace(
    "  if (url !== 'status.json' || opts.cache !== 'no-store') throw new Error('bad fetch ' + url);",
    "  if (url === '/api/session') return Promise.resolve({ok: true, status: 200, json: () =>\n"
    "    Promise.resolve({signed_in: true, login: 'maxharris1', csrf: 'csrf-value'})});\n"
    "  if (url !== 'status.json' || opts.cache !== 'no-store') throw new Error('bad fetch ' + url);",
)


@layout.needs_node
def test_a_held_card_with_no_job_keeps_the_comment_and_ok_controls(tmp_path: Path, snapshot: dict) -> None:
    assert SIGNED_IN != HARNESS
    snap = json.loads(json.dumps(snapshot))
    idle = [c["ticket"] for col in tickets_page.board(snap)["columns"] for c in col["cards"]
            if not c.get("live") and not c.get("queued")]
    held = next(row for row in snap["tickets"] if _key(row) == _key(idle[0]))
    held.update(state="awaiting-ok", waiting_on="maxharris1", source="#563", pr="601")
    html = render_pages(snap)["job.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    out = layout.run_harness(tmp_path, SIGNED_IN, script, snap, "job", IDS, [])
    card = _card(out["html"]["pipeline"], _key(held))
    (panel,) = _all(card, lambda n: "jd-gh" in n["attrs"].get("class", "").split())
    assert "signed in as maxharris1" in json.dumps(panel)
    (form,) = _all(panel, lambda n: n["attrs"].get("class") == "gh-form")
    assert "hidden" not in form["attrs"]
    assert [o["kids"] for o in _all(form, lambda n: n["tag"] == "option")] == [["issue #563"], ["PR #601"]]
    assert _all(form, lambda n: n["tag"] == "textarea")
    (okbox,) = _all(panel, lambda n: "gh-okbox" in n["attrs"].get("class", "").split())
    assert "hidden" not in okbox["attrs"]


def _troubled(snapshot: dict) -> tuple[dict, dict[str, list[tuple[str, str]]]]:
    tickets = _state_tickets()
    found = copy.deepcopy(snapshot)
    troubles = {"planning": ("claimed", False), "building": ("stale", False), "review": ("dead", False),
                "fixing": ("running", True)}
    for row in found["running"]:
        state = next(s for s, (repo, ticket) in tickets.items() if (row["repo"], row["ticket_id"]) == (repo, ticket))
        row["job_state"], row["overdue"] = troubles[state]
    expect = {"planning": [("claimed", "warn")], "building": [("stale", "warn")], "review": [("dead", "bad")],
              "fixing": [("overdue", "bad")]}
    return found, {"#".join(tickets[state]): chips for state, chips in expect.items()}


def _state_chips(card: dict) -> list[tuple[str, str]]:
    return [(_text(node), next(c for c in node["attrs"]["class"].split() if c != "chip"))
            for node in _all(_one(card, _cls("tk-state")), _cls("chip"))]


def test_troubled_jobs_show_their_lanes_chip_beside_the_state(snapshot: dict) -> None:
    healthy = _server(snapshot)["pipeline"]
    for key in _troubled(snapshot)[1]:
        assert _state_chips(_card(healthy, key)) == []
    found, expect = _troubled(snapshot)
    board = _server(found)["pipeline"]
    for key, chips in expect.items():
        assert _state_chips(_card(board, key)) == chips, key


@layout.needs_node
def test_script_troubled_chips_match_the_server(tmp_path: Path, snapshot: dict) -> None:
    found, expect = _troubled(snapshot)
    out = _script(tmp_path, found)
    assert out["html"]["pipeline"] == _server(found)["pipeline"]
    for key, chips in expect.items():
        assert _state_chips(_card(out["html"]["pipeline"], key)) == chips, key


def _idle(snapshot: dict) -> dict:
    return {**snapshot, "running": [], "queued": []}


def _says_no_job(markup: str) -> dict[str, bool]:
    return {key: "no job running" in _text(_card(markup, key)) for key in _placed(markup)}


def test_no_job_running_only_in_plan_build_and_review(snapshot: dict) -> None:
    server = _server(_idle(snapshot))
    phases = flow.ticket_phases(flow.load_chart(CHART))
    says = _says_no_job(server["pipeline"] + server["shipped"])
    for row in snapshot["tickets"]:
        phase = "Stop" if row["stuck"] else phases[row["state"]]
        if phase != "Shipped":
            assert says[_key(row)] == (phase in ("Plan", "Build", "Review")), (row["state"], phase)


@layout.needs_node
def test_script_no_job_running_matches_the_server(tmp_path: Path, snapshot: dict) -> None:
    found = _idle(snapshot)
    out = _script(tmp_path, found)
    server = _server(found)
    for ident in ("pipeline", "shipped"):
        assert out["html"][ident] == server[ident], ident


def _waiting_verdicts(snapshot: dict) -> tuple[dict, str, str]:
    tickets = _state_tickets()
    found = copy.deepcopy(_idle(snapshot))
    waiting, other = tickets["waiting-max"], tickets["awaiting-ok"]
    for row in found["tickets"]:
        if (row["repo"], row["ticket_id"]) in (waiting, other):
            row["waiting_on"] = APPROVER
    found["poller"]["verdicts"] = [
        {"repo": waiting[0], "ticket_id": waiting[1], "outcome": "waiting-max", "kind": "waiting", "seq": 2,
         "age_seconds": None},
        {"repo": other[0], "ticket_id": other[1], "outcome": "queued-review", "kind": "routed", "seq": 3,
         "age_seconds": None},
    ]
    return found, "#".join(waiting), "#".join(other)


def _check_waiting(markup: str, waiting: str, other: str) -> None:
    card = _card(markup, waiting)
    assert f"waiting on {APPROVER}" in _text(card)
    assert not _all(card, _cls("chip")) and "tk-run" not in str(card)
    shown = _card(markup, other)
    assert f"waiting on {APPROVER}" in _text(shown)
    assert _text(_one(shown, _cls("chip"))) == "routed" and "queued-review" in _text(shown)


def test_a_waiting_card_drops_its_waiting_verdict_row(snapshot: dict) -> None:
    found, waiting, other = _waiting_verdicts(snapshot)
    _check_waiting(_server(found)["pipeline"], waiting, other)


@layout.needs_node
def test_script_waiting_card_drops_its_waiting_verdict_row(tmp_path: Path, snapshot: dict) -> None:
    found, waiting, other = _waiting_verdicts(snapshot)
    out = _script(tmp_path, found)
    assert out["html"]["pipeline"] == _server(found)["pipeline"]
    _check_waiting(out["html"]["pipeline"], waiting, other)


def _scroll_table(markup: str) -> None:
    (wrap,) = _all(_dom(markup), _cls("tk-scroll"))
    assert [kid["tag"] for kid in wrap["kids"]] == ["table"]
    assert not [n for n in _all(_dom(markup), lambda n: n["tag"] == "table") if n not in wrap["kids"]]


def test_the_table_scrolls_sideways_inside_its_own_wrapper(snapshot: dict) -> None:
    _scroll_table(_server(snapshot)["tickets"])
    assert ".tk-scroll{overflow-x:auto}" in page.CSS


@layout.needs_node
def test_script_table_has_the_same_wrapper(tmp_path: Path, snapshot: dict) -> None:
    out = _script(tmp_path, snapshot)
    _scroll_table(out["html"]["tickets"])
    assert out["html"]["tickets"] == _server(snapshot)["tickets"]


def _stored(tmp_path: Path, snapshot: dict, store: dict | None, actions: list) -> dict:
    storage = (
        "{getItem(k) { throw new Error('denied'); }, setItem(k, v) { throw new Error('denied'); }}" if store is None
        else "{data: " + json.dumps(store) + ", getItem(k) { return k in this.data ? this.data[k] : null; }, "
        "setItem(k, v) { this.data[k] = String(v); }}"
    )
    harness = HARNESS.replace("global.window = global;\n",
                              "global.window = global;\nglobal.sessionStorage = " + storage + ";\n").replace(
        "    } else if (kind === 'press') {",
        "    } else if (kind === 'store') {\n"
        "      steps.push({store: global.sessionStorage.data || null});\n"
        "    } else if (kind === 'press') {",
    )
    assert "global.sessionStorage = " in harness and "kind === 'store'" in harness
    html = render_pages(snapshot)["job.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    return layout.run_harness(tmp_path, harness, script, snapshot, "job", IDS, actions)


def _rows(markup: str) -> int:
    return len(_all(_one(_dom(markup), lambda n: n["tag"] == "tbody"), lambda n: n["tag"] == "tr"))


def _pressed(markup: str, attr: str) -> list[str]:
    return [n["attrs"][attr] for n in _all(_dom(markup), _attr(attr)) if n["attrs"]["aria-pressed"] == "true"]


@layout.needs_node
def test_a_stored_project_and_view_come_back_after_a_reload(tmp_path: Path, snapshot: dict) -> None:
    repo = snapshot["tickets"][0]["repo"]
    out = _stored(tmp_path, snapshot, {"tk-project": repo, "tk-view": "table"},
                  [["snap", ["ticket-filter", "pipeline", "tickets"]], ["press", ["data-project", ""]],
                   ["press", ["data-view", "board"]], ["store", None]])
    first = out["steps"][0]
    assert _pressed(first["snap"]["ticket-filter"], "data-project") == [repo]
    assert _pressed(first["snap"]["ticket-filter"], "data-view") == ["table"]
    assert first["hidden"] == {"ticket-filter": False, "pipeline": True, "tickets": False}
    assert _rows(first["snap"]["tickets"]) == sum(r["repo"] == repo for r in snapshot["tickets"])
    assert out["steps"][-1]["store"] == {"tk-project": "", "tk-view": "board"}


@layout.needs_node
def test_presses_are_stored_for_the_session(tmp_path: Path, snapshot: dict) -> None:
    repo = snapshot["tickets"][0]["repo"]
    out = _stored(tmp_path, snapshot, {}, [["press", ["data-project", repo]], ["press", ["data-view", "table"]],
                                           ["store", None]])
    assert out["steps"][-1]["store"] == {"tk-project": repo, "tk-view": "table"}


@layout.needs_node
def test_a_stored_project_gone_from_the_snapshot_falls_back_to_all(tmp_path: Path, snapshot: dict) -> None:
    out = _stored(tmp_path, snapshot, {"tk-project": "gone/repo", "tk-view": "board"},
                  [["snap", ["ticket-filter", "tickets"]], ["store", None]])
    snap = out["steps"][0]["snap"]
    assert _pressed(snap["ticket-filter"], "data-project") == [""]
    assert _rows(snap["tickets"]) == len(snapshot["tickets"])
    assert out["steps"][-1]["store"]["tk-project"] == ""


@layout.needs_node
def test_the_board_works_when_storage_throws(tmp_path: Path, snapshot: dict) -> None:
    repo = snapshot["tickets"][0]["repo"]
    out = _stored(tmp_path, snapshot, None, [["snap", ["ticket-filter", "pipeline"]],
                                             ["press", ["data-project", repo]], ["press", ["data-view", "table"]],
                                             ["snap", ["ticket-filter", "pipeline", "tickets"]]])
    server = _server(snapshot)
    first, last = out["steps"][0], out["steps"][-1]
    assert first["snap"]["pipeline"] == server["pipeline"] and first["hidden"]["pipeline"] is False
    assert _pressed(last["snap"]["ticket-filter"], "data-project") == [repo]
    assert last["hidden"] == {"ticket-filter": False, "pipeline": True, "tickets": False}


CI_WAIT = ("maxharris1/factory", "591")
CI_WAIT_LINEAR = ("aut0-code/aut0", "AUT-591")
NO_JOB = ("maxharris1/factory", "592")


def ci_wait_snapshot(tmp_path: Path) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, *CI_WAIT, state="building")
        add_ticket(connection, *CI_WAIT_LINEAR, tracker="linear", state="building")
        add_ticket(connection, *NO_JOB, state="building")
        connection.executemany(
            "INSERT INTO ci_waits (repo, ticket_id, comment_id, head, reason, checks, since) "
            "VALUES (?, ?, 'c-ready', ?, ?, ?, ?)",
            [(*CI_WAIT, "a" * 40, "ci-pending", '["build", "lint"]', NOW - 125),
             (*CI_WAIT_LINEAR, "b" * 40, "approved-ci-pending", "[]", NOW - 5)],
        )
    log = tmp_path / "poller.log"
    log.write_text("cycle\n", encoding="utf-8")
    return collect_snapshot(db, log, now=NOW, chart_path=CHART)


def test_a_ready_held_for_ci_shows_waiting_on_ci_instead_of_no_job(tmp_path: Path) -> None:
    board = _server(ci_wait_snapshot(tmp_path))["pipeline"]
    card = _card(board, "#".join(CI_WAIT))
    assert card["attrs"]["data-wait"] == "ci"
    assert "no job running" not in _text(card)
    assert (_canvas(card)["data-px"], _canvas(card)["data-tint"]) == ("static", "#ff9e64")
    line = _one(card, _cls("tk-ci"))
    assert _text(_one(line, _cls("chip"))) == "waiting on CI"
    for needle in ("build, lint", "checks", "waiting 2m 05s"):
        assert needle in _text(line), needle
    link = _one(line, lambda n: n["tag"] == "a")
    assert link["attrs"]["href"] == "https://github.com/maxharris1/factory/pull/591/checks"

    linear = _one(_card(board, "#".join(CI_WAIT_LINEAR)), _cls("tk-ci"))
    assert "checks pending" in _text(linear) and not _all(linear, lambda n: n["tag"] == "a")

    idle = _card(board, "#".join(NO_JOB))
    assert "no job running" in _text(idle) and "waiting on CI" not in _text(idle)
    assert "data-wait" not in idle["attrs"] and not _all(idle, lambda n: n["tag"] == "canvas")


@layout.needs_node
def test_script_draws_a_ready_held_for_ci_like_the_server(tmp_path: Path) -> None:
    snapshot = ci_wait_snapshot(tmp_path)
    out, server = _script(tmp_path, snapshot), _server(snapshot)
    assert 'data-wait="ci"' in server["pipeline"]
    for ident in BOARD:
        assert out["html"][ident] == server[ident], ident


NO_CI = ("maxharris1/Factory", "615")
ON_CI = ("maxharris1/factory", "616")


def no_ci_snapshot(tmp_path: Path) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, *NO_CI, state="building")
        add_ticket(connection, *ON_CI, state="building")
        connection.execute(
            "INSERT INTO no_ci_bases (repo, ticket_id, base) VALUES (?, ?, 'release/1.x')",
            (NO_CI[0].lower(), NO_CI[1]),
        )
    log = tmp_path / "poller.log"
    log.write_text("cycle\n", encoding="utf-8")
    return collect_snapshot(db, log, now=NOW, chart_path=CHART)


def _check_no_ci(board: str) -> None:
    card = _card(board, "#".join(NO_CI))
    line = _one(card, _cls("tk-ci"))
    chip = _one(line, _cls("chip"))
    assert (_text(chip), chip["attrs"]["class"]) == ("no CI on this base", "chip muted")
    assert "release/1.x" in _text(line)
    assert "waiting on CI" not in _text(card) and "data-wait" not in card["attrs"]
    plain = _card(board, "#".join(ON_CI))
    assert "no CI on this base" not in _text(plain) and not _all(plain, _cls("tk-ci"))


def test_a_ticket_on_a_base_without_ci_says_so_instead_of_waiting_on_ci(tmp_path: Path) -> None:
    snapshot = no_ci_snapshot(tmp_path)
    assert {row["ticket_id"]: row.get("no_ci") for row in snapshot["tickets"]} == {
        NO_CI[1]: {"base": "release/1.x"}, ON_CI[1]: None,
    }
    _check_no_ci(_server(snapshot)["pipeline"])


@layout.needs_node
def test_script_draws_a_base_without_ci_like_the_server(tmp_path: Path) -> None:
    snapshot = no_ci_snapshot(tmp_path)
    out, server = _script(tmp_path, snapshot), _server(snapshot)
    _check_no_ci(out["html"]["pipeline"])
    for ident in BOARD:
        assert out["html"][ident] == server[ident], ident
