from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest

import lib.dashboard_flow as flow
import lib.dashboard_page as page
from lib.dashboard_pages import tickets as tickets_page
from lib.dashboard_pages.common import _ticket, ticket_page_href
from lib.dashboard import collect_snapshot, render_pages
from tests import test_dashboard_layout as layout
from tests import test_dashboard_pixelstrip as pixelstrip
from tests.test_dashboard_lanes import Browser, _page_with_status_stub, needs_chrome
from tests.test_dashboard import AUT0, NOW, _hand_off_in_linear, add_job, add_ticket, make_db

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
IDS = layout.BAR + ["hud", *BOARD, "flow-template"]
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


def _wide(node: dict) -> dict:
    return {**node, "kids": [_wide(kid) if isinstance(kid, dict) else kid for kid in node["kids"]
                             if not (isinstance(kid, dict) and _cls("tk-narrow")(kid))]}


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
    "    } else if (kind === 'type') {\n"
    "      document.activeElement.value = arg;\n"
    "      fire(document.activeElement, 'input');\n"
    "    } else if (kind === 'keyon') {\n"
    "      steps.push({keyon: arg, prevented: fire(document.activeElement, 'keydown', {key: arg}).defaultPrevented});\n"
    "    } else if (kind === 'leave') {\n"
    "      const to = find(body, n => (arg[0] === 'class' ? n.className : n.attrs[arg[0]]) === arg[1]);\n"
    "      fire(document.activeElement, 'focusout', {relatedTarget: to});\n"
    "      to.focus();\n"
    "    } else if (kind === 'where') {\n"
    "      steps.push({where: location.href || null, value: ids['find-ticket'].value || ''});\n"
    "    } else if (kind === 'focused') {\n"
    "      const node = document.activeElement;\n"
    "      steps.push({focused: node && node.isConnected ? [node.tagName, node.attrs[arg]] : 'BODY'});\n"
    "    } else if (kind === 'record') {",
).replace(
    "'</' + this.tagName + '>'; }",
    "(this.tagName === 'input' ? '' : '</' + this.tagName + '>'); }",
).replace(
    "global.addEventListener = () => {};",
    "const windowListeners = {hashchange: []};\n"
    "global.addEventListener = (type, fn) => { if (windowListeners[type]) windowListeners[type].push(fn); };",
)


def _script(tmp_path: Path, snapshot: dict, actions: list | None = None, page_from: dict | None = None) -> dict:
    assert "kind === 'press'" in HARNESS and f"nowMs = {int(NOW * 1000)};" in HARNESS
    assert "windowListeners[type].push" in HARNESS
    assert "kind === 'keyon'" in HARNESS and "this.tagName === 'input'" in HARNESS
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
        elif name in ("handed-off", "handed-off-externally", "merged", "done"):
            assert phases[name] == "Shipped"
        else:
            assert phases[name] == legend[state["group"]], name
    assert phases["stuck"] == "Stop"
    assert [s for s, p in phases.items() if p == "Ship"] == [
        "ship", "awaiting-ok", "merge-queue", "waiting-external", "updating", "post-merge"]
    assert [s for s, p in phases.items() if p == "Review"] == ["review"]


def test_a_new_teal_chart_state_joins_ship_with_no_code_change() -> None:
    text = CHART.read_text(encoding="utf-8")
    text = text.replace("  s22: capped\n", "  s22: capped\n  s23: launch-pad\n")
    text = text.replace("  class s22 yellow\n", "  class s22 yellow\n  class s23 teal\n")
    text = text.replace("  s3 --> s3: CI red, fix\n", "  s3 --> s3: CI red, fix\n  s14 --> s23: go\n")
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
    pipeline, shipped = _dom(markup["pipeline"]), _wide(_dom(markup["shipped"]))
    opened = 0
    for column in _all(pipeline, _cls("tk-col")):
        cards = _all(column, _cls("tk-card"))
        assert _number(_text(_one(column, _cls("tk-count")))) == len(cards)
        opened += len(cards)
    tiles = _all(shipped, _cls("tk-tile"))
    assert len(rows) == opened + len(tiles)
    count = _text(_one(pipeline, _cls("count")))
    assert count == f"{len(rows)} tracked · {opened} open · {len(tiles)} shipped"
    assert _text(_one(shipped, _cls("count"))) == str(len(tiles))
    handed = sum(row["state"] in ("handed-off", "handed-off-externally") and not row["stuck"] for row in rows)
    if tiles:
        assert _text(_one(shipped, _cls("tk-sub"))) == f"{len(tiles) - handed} merged · {handed} handed off"
    else:
        assert not _all(shipped, _cls("tk-sub"))
        assert _text(_one(shipped, _cls("empty"))) == "nothing shipped"
    assert sorted(_placed_in(pipeline) + _placed_in(shipped)) == sorted(_key(r) for r in rows)


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
    assert f"waits on {APPROVER}" in _text(waiting)
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
    assert "queued" in _text(queued) and _text(_one(queued, _cls("tk-run"))).startswith("for")
    assert (_canvas(queued)["data-px"], _canvas(queued).get("data-still")) == ("conveyor", "true")
    idle = _card(board, "#".join(tickets[VERDICT[0]]))
    assert not _all(idle, lambda n: n["tag"] == "canvas")
    assert _id_link(idle)["attrs"]["href"] == _page_of(tickets[VERDICT[0]])
    assert not _all(_dom(board), _job_link)
    assert "no job running" not in _text(idle)
    assert _text(_one(idle, _cls("chip"))) == "routed" and VERDICT[1] in _text(idle)
    assert {_text(node) for node in _all(_dom(board), _cls("tk-tag"))} == {"github", "linear"}


def test_shelf_tiles_carry_their_revise_rounds(snapshot: dict) -> None:
    rounds = {_key(row): row["round"] for row in snapshot["tickets"]}
    tiles = _all(_dom(_server(snapshot)["shipped"]), _cls("tk-tile"))
    assert len(tiles) == 4 and len({rounds[t["attrs"]["data-ticket"]] for t in tiles}) > 1
    for tile in tiles:
        assert "#" in _text(_one(tile, _cls("tk-tn")))
        assert _text(_one(tile, _cls("tk-r"))) == f"r{rounds[tile['attrs']['data-ticket']]}"


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
    shelf = _dom(markup["shipped"])
    assert not _all(shelf, _attr("data-phase"))
    assert _text(_one(shelf, _cls("count"))) == "0" and _text(_one(shelf, _cls("empty"))) == "nothing shipped"


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


def _check_tile_links(markup: str, rows: list[dict], hidden: int = 0) -> None:
    by_key = {_key(row): row for row in rows}
    shipped = {key for key, row in by_key.items() if row["state"] in flow.SHIPPED and not row["stuck"]}
    tiles = _all(_wide(_dom(markup)), _cls("tk-tile"))
    assert tiles
    for tile in tiles:
        key = tile["attrs"]["data-ticket"]
        assert tile["tag"] == "a", key
        assert tile["attrs"]["href"] == ticket_page_href(by_key[key]), key
    keys = [tile["attrs"]["data-ticket"] for tile in tiles]
    assert len(keys) == len(set(keys)) and set(keys) <= shipped
    assert len(shipped) - len(keys) == hidden


def test_every_shelf_tile_links_its_ticket_page(snapshot: dict, shelf: dict) -> None:
    _check_tile_links(_server(snapshot)["shipped"], snapshot["tickets"])
    _check_tile_links(_server(shelf)["shipped"], shelf["tickets"], hidden=10)


@layout.needs_node
def test_script_shelf_tiles_link_their_ticket_page(tmp_path: Path, snapshot: dict, shelf: dict) -> None:
    _check_tile_links(_script(tmp_path, snapshot)["html"]["shipped"], snapshot["tickets"])
    steps = _shelf_script(tmp_path, shelf, [["shelf", "press"], ["snap", ["shipped"]]])
    _check_tile_links(steps[-1]["snap"]["shipped"], shelf["tickets"])


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
def test_script_draws_the_handed_off_in_linear_tile_as_the_server_does(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, AUT0, "AUT-2282", tracker="linear", state="capped", review_round=6)
        _hand_off_in_linear(connection, "AUT-2282", "In Review")
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    snapshot = collect_snapshot(db, log, now=NOW)
    out, server = _script(tmp_path, snapshot), _server(snapshot)
    assert "handed off in Linear 2h 00m ago" in server["shipped"]
    assert out["html"]["shipped"] == server["shipped"]
    assert out["html"]["pipeline"] == server["pipeline"]


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


def _check_in_state(markup: str, rows: list[dict]) -> None:
    by_key = {_key(row): row for row in rows}
    cards = _all(_dom(markup), _cls("tk-card"))
    assert cards
    for card in cards:
        row = by_key[card["attrs"]["data-ticket"]]
        assert "no job running" not in _text(card), row["state"]
        assert _text(_one(card, _cls("tk-in"))).startswith(f"in {row['state']}"), row["state"]


def test_no_card_says_no_job_running_and_each_says_its_time_in_state(snapshot: dict) -> None:
    _check_in_state(_server(_idle(snapshot))["pipeline"], snapshot["tickets"])


@layout.needs_node
def test_script_idle_cards_match_the_server(tmp_path: Path, snapshot: dict) -> None:
    found = _idle(snapshot)
    out = _script(tmp_path, found)
    server = _server(found)
    _check_in_state(out["html"]["pipeline"], snapshot["tickets"])
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
    assert f"waits on {APPROVER}" in _text(card)
    assert not _all(card, _cls("chip")) and "tk-run" not in str(card)
    shown = _card(markup, other)
    assert f"waits on {APPROVER}" in _text(shown)
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
        "    } else if (kind === 'poll') {\n"
        "      status = arg;\n"
        "      polls.splice(0).forEach(fn => fn());\n"
        "      await new Promise(resolve => realSetTimeout(resolve, 50));\n"
        "    } else if (kind === 'press') {",
    ).replace("const status = JSON.parse(statusText);", "let status = JSON.parse(statusText);").replace(
        "const intervals = [];",
        "const intervals = [];\nconst realSetTimeout = setTimeout;\nconst polls = [];\n"
        "global.setTimeout = (fn, ms) => { if (ms === 1500) polls.push(fn); return 0; };",
    ).replace("function act(actions) {", "async function act(actions) {").replace(
        "setTimeout(() => {\n  act(JSON.parse(actionsText));",
        "realSetTimeout(async () => {\n  await act(JSON.parse(actionsText));",
    )
    assert "global.sessionStorage = " in harness and "kind === 'store'" in harness
    assert "await act(" in harness and "polls.push(fn)" in harness
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


def _picked(markup: str) -> tuple[list[str], list[str]]:
    pipeline = _dom(markup)
    return (_pressed(markup, "data-pick"),
            [c["attrs"]["data-phase"] for c in _all(pipeline, _cls("tk-col")) if _cls("tk-picked")(c)])


def _check_phases(markup: str, rows: list[dict], picked: str) -> None:
    phases = flow.ticket_phases(flow.load_chart(CHART))
    counts = Counter(flow.ticket_phase(phases, row["state"], row["stuck"]) for row in rows)
    (bar,) = _all(_dom(markup), _cls("tk-phases"))
    assert bar["attrs"] == {"class": "tk-phases", "role": "group", "aria-label": "Phase"}
    buttons = [kid for kid in bar["kids"] if isinstance(kid, dict)]
    assert [b["attrs"]["data-pick"] for b in buttons] == COLUMNS
    for phase, button in zip(COLUMNS, buttons):
        n, name = counts[phase], "Stopped" if phase == "Stop" else phase
        assert (button["tag"], button["attrs"]["class"], button["attrs"]["type"]) == ("button", "tk-ph", "button")
        assert button["attrs"]["aria-label"] == f"{name}, {n} ticket" + ("" if n == 1 else "s")
        tone = "tk-phc tk-none" if not n else "tk-phc tk-stopped" if phase == "Stop" else "tk-phc"
        assert [(kid["attrs"]["class"], _text(kid)) for kid in button["kids"]] == [("tk-phn", name), (tone, str(n))]
    assert _picked(markup) == ([picked], [picked])


def _ask_live(snapshot: dict) -> dict:
    found = copy.deepcopy(snapshot)
    repo, ticket_id = _state_tickets()["questions"]
    found["running"].append({**found["running"][0], "job": "j-clarify", "repo": repo, "ticket_id": ticket_id,
                             "action": "clarify"})
    return found


def _stale_plan(snapshot: dict) -> dict:
    found = copy.deepcopy(snapshot)
    plan = _state_tickets()["planning"]
    found["running"] = [{**row, "job_state": "stale"} for row in found["running"]
                        if (row["repo"], row["ticket_id"]) == plan]
    found["queued"] = []
    return found


def _nothing(tmp_path: Path) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    (tmp_path / "poller.log").write_text("", encoding="utf-8")
    return collect_snapshot(db, tmp_path / "poller.log", now=NOW, chart_path=CHART)


STARTS = {
    "a live job in Ask": (lambda snap, tmp: _ask_live(snap), "Ask"),
    "live jobs in Plan, Build and Review": (lambda snap, tmp: snap, "Plan"),
    "only a stale job, in Plan": (lambda snap, tmp: _stale_plan(snap), "Plan"),
    "only a queued job, in Plan": (lambda snap, tmp: {**snap, "running": []}, "Ask"),
    "no job, Build first to hold cards": (lambda snap, tmp: ci_wait_snapshot(tmp), "Build"),
    "an empty board": (lambda snap, tmp: _nothing(tmp), "Ask"),
}


@pytest.mark.parametrize("case", list(STARTS))
def test_the_phase_bar_counts_each_column_and_starts_where_work_runs(tmp_path: Path, snapshot: dict,
                                                                    case: str) -> None:
    build, picked = STARTS[case]
    found = build(snapshot, tmp_path)
    _check_phases(_server(found)["pipeline"], found["tickets"], picked)


@layout.needs_node
@pytest.mark.parametrize("case", list(STARTS))
def test_script_phase_bar_matches_the_server(tmp_path: Path, snapshot: dict, case: str) -> None:
    build, picked = STARTS[case]
    found = build(snapshot, tmp_path)
    out, server = _script(tmp_path, found), _server(found)
    _check_phases(out["html"]["pipeline"], found["tickets"], picked)
    for ident in BOARD:
        assert out["html"][ident] == server[ident], ident


def test_phase_buttons_read_like_the_spec(snapshot: dict) -> None:
    bar = _one(_dom(_server(_ask_live(snapshot))["pipeline"]), _cls("tk-phases"))
    labels = [b["attrs"]["aria-label"] for b in _all(bar, _cls("tk-ph"))]
    assert labels == ["Ask, 3 tickets", "Plan, 2 tickets", "Build, 2 tickets", "Review, 1 ticket",
                      "Ship, 6 tickets", "Stopped, 6 tickets"]


def _moved(snapshot: dict) -> dict:
    review = _state_tickets()["review"]
    moved = copy.deepcopy(snapshot)
    moved["generated_at_epoch"] += 30
    moved["running"] = [row for row in moved["running"] if (row["repo"], row["ticket_id"]) == review]
    return moved


def _redrawn(before: str, after: str) -> bool:
    planning = "#".join(_state_tickets()["planning"])
    strips = [_all(_card(markup, planning), lambda n: n["tag"] == "canvas") for markup in (before, after)]
    return bool(strips[0]) and not strips[1]


@layout.needs_node
def test_a_pressed_phase_survives_a_redraw_and_is_stored(tmp_path: Path, snapshot: dict) -> None:
    out = _stored(tmp_path, snapshot, {}, [["press", ["data-pick", "Build"]], ["snap", ["pipeline"]],
                                           ["poll", _moved(snapshot)], ["snap", ["pipeline"]], ["store", None]])
    before, after = (step["snap"]["pipeline"] for step in out["steps"] if "snap" in step)
    assert _redrawn(before, after)
    assert _picked(before) == _picked(after) == (["Build"], ["Build"])
    assert out["steps"][-1]["store"] == {"tk-phase": "Build"}


@layout.needs_node
def test_a_redraw_keeps_the_first_pick_when_the_work_moves(tmp_path: Path, snapshot: dict) -> None:
    moved = _moved(snapshot)
    assert _picked(_server(moved)["pipeline"]) == (["Review"], ["Review"])
    out = _stored(tmp_path, snapshot, {}, [["snap", ["pipeline"]], ["poll", moved], ["snap", ["pipeline"]],
                                           ["store", None]])
    first, after = (step["snap"]["pipeline"] for step in out["steps"] if "snap" in step)
    assert _redrawn(first, after)
    assert _picked(first) == _picked(after) == (["Plan"], ["Plan"])
    assert out["steps"][-1]["store"] == {}


@layout.needs_node
def test_a_stored_phase_comes_back_after_a_reload(tmp_path: Path, snapshot: dict) -> None:
    out = _stored(tmp_path, snapshot, {"tk-phase": "Review"}, [["snap", ["pipeline"]]])
    assert _picked(out["steps"][0]["snap"]["pipeline"]) == (["Review"], ["Review"])


@layout.needs_node
@pytest.mark.parametrize("stored", ["Shipped", "review", ""])
def test_a_stored_phase_that_is_no_column_falls_back_to_where_work_runs(tmp_path: Path, snapshot: dict,
                                                                       stored: str) -> None:
    out = _stored(tmp_path, snapshot, {"tk-phase": stored}, [["snap", ["pipeline"]]])
    assert out["steps"][0]["snap"]["pipeline"] == _server(snapshot)["pipeline"]
    assert _picked(out["steps"][0]["snap"]["pipeline"]) == (["Plan"], ["Plan"])


@layout.needs_node
def test_phase_presses_work_when_storage_throws(tmp_path: Path, snapshot: dict) -> None:
    out = _stored(tmp_path, snapshot, None, [["snap", ["pipeline"]], ["press", ["data-pick", "Stop"]],
                                             ["poll", _moved(snapshot)], ["snap", ["pipeline"]]])
    first, last = (step["snap"]["pipeline"] for step in out["steps"] if "snap" in step)
    assert first == _server(snapshot)["pipeline"]
    assert _redrawn(first, last)
    assert _picked(last) == (["Stop"], ["Stop"])


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
    assert _text(_one(idle, _cls("tk-in"))).startswith("in building") and "waiting on CI" not in _text(idle)
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


STALE = ("maxharris1/cleffy", "131")
LIVE = {("maxharris1/factory", "474"): ("fixing", 665), ("maxharris1/factory", "478"): ("building", 372)}
QUEUED_FOR = ("maxharris1/factory", "472")
QUEUED_LATER = ("maxharris1/factory", "476")
IDLE_AGES = {("maxharris1/factory", "471"): 840, ("maxharris1/factory", "470"): 300}
HELD = ("maxharris1/factory", "490")
STOPPED = ("maxharris1/cleffy", "103")
STOPPED_AGE = 3 * 86400 + 2 * 3600
ASK_AGES = dict(zip((f"{n}" for n in range(479, 487)), (180, 840, 540, None, 1320, 60, 1800, 300)))
BUILD_ORDER = ["maxharris1/cleffy#131", "maxharris1/factory#474", "maxharris1/factory#478",
               "maxharris1/factory#472", "maxharris1/factory#476", "maxharris1/factory#471",
               "maxharris1/factory#470"]
ASK_ORDER = [f"maxharris1/factory#{n}" for n in (485, 483, 480, 481, 486, 479, 484, 482)]


def order_snapshot(tmp_path: Path) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        def ticket(repo: str, ticket_id: str, state: str, age: int | None, **kw) -> None:
            add_ticket(connection, repo, ticket_id, state=state, **kw)
            if age is not None:
                connection.execute(
                    "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
                    "VALUES (?, ?, NULL, ?, ?, 'poller')", (repo, ticket_id, state, NOW - age))

        for (repo, ticket_id), age in IDLE_AGES.items():
            ticket(repo, ticket_id, "building", age)
        ticket(*QUEUED_FOR, "building", 60)
        add_job(connection, "j-queued", recipient="codex", repo=QUEUED_FOR[0], ticket_id=QUEUED_FOR[1],
                created_at=NOW - 90)
        ticket(*QUEUED_LATER, "building", 30)
        add_job(connection, "j-queued-later", recipient="codex", repo=QUEUED_LATER[0], ticket_id=QUEUED_LATER[1],
                created_at=NOW - 15)
        for (repo, ticket_id), (state, elapsed) in LIVE.items():
            ticket(repo, ticket_id, state, None, review_round=1)
            add_job(connection, f"j-{ticket_id}", state="running", repo=repo, ticket_id=ticket_id,
                    created_at=NOW - elapsed, last_activity_at=NOW - 6, detail='{"round":"1/1"}')
        ticket(*STALE, "building", None)
        add_job(connection, "j-stale", state="running", repo=STALE[0], ticket_id=STALE[1],
                created_at=NOW - 1180, last_activity_at=NOW - 250, detail='{"round":"1/1"}')
        for ticket_id, age in ASK_AGES.items():
            ticket("maxharris1/factory", ticket_id, "intake", age)
        ticket(*HELD, "awaiting-ok", 420)
        connection.execute(
            "INSERT INTO ship_holds (repo, ticket_id, head, approver, reasons, approve_comment_id, held_at) "
            "VALUES (?, ?, 'abc123', ?, 'label', 'c-hold', ?)", (*HELD, APPROVER, NOW - 420))
        ticket(*STOPPED, "planning", STOPPED_AGE, stuck=1,
               stuck_reason="cancelled: superseded by sanctioned intake (Factory 2026-11-13)")
    log = tmp_path / "poller.log"
    log.write_text("cycle\n", encoding="utf-8")
    return collect_snapshot(db, log, now=NOW, chart_path=CHART)


@pytest.fixture(scope="module")
def ordered(tmp_path_factory) -> dict:
    return order_snapshot(tmp_path_factory.mktemp("order"))


def _column(markup: str, phase: str) -> dict:
    return _one(_dom(markup), _attr("data-phase", phase))


def _check_order(markup: str) -> None:
    assert _placed_in(_column(markup, "Build")) == BUILD_ORDER
    assert _placed_in(_column(markup, "Ask")) == ASK_ORDER


def test_a_ticket_row_carries_its_records_time_in_state(ordered: dict) -> None:
    from lib.dashboard import RECORDS

    ages = {_key(row): row["state_age_seconds"] for row in ordered["tickets"]}
    assert ages == {key: record["state_age_seconds"] for key, record in ordered[RECORDS].items()}
    assert ages["#".join(STOPPED)] == STOPPED_AGE and ages["maxharris1/factory#482"] is None
    assert all(ages["#".join(key)] == age for key, age in IDLE_AGES.items())


def test_live_jobs_come_first_then_queued_then_idle_longest_first(ordered: dict) -> None:
    _check_order(_server(ordered)["pipeline"])


@layout.needs_node
def test_script_orders_cards_like_the_server(tmp_path: Path, ordered: dict) -> None:
    out = _script(tmp_path, ordered)
    _check_order(out["html"]["pipeline"])
    for ident in BOARD:
        assert out["html"][ident] == _server(ordered)[ident], ident


def _check_idle(markup: str) -> None:
    assert "no job running" not in _text(_dom(markup))
    assert _text(_one(_card(markup, "maxharris1/factory#471"), _cls("tk-in"))) == "in building 14m 00s"
    assert _text(_one(_card(markup, "maxharris1/factory#470"), _cls("tk-in"))) == "in building 5m 00s"
    assert _text(_one(_card(markup, "maxharris1/factory#482"), _cls("tk-in"))) == "in intake"
    held = _card(markup, "#".join(HELD))
    assert _text(_one(held, _cls("tk-in"))) == "in awaiting-ok 7m 00s"
    assert _text(_one(held, _cls("tk-wait"))) == f"waits on {APPROVER}"
    assert "waiting on" not in _text(held)
    stopped = _card(markup, "#".join(STOPPED))
    state = _one(stopped, _cls("tk-state"))
    assert [_text(kid) for kid in state["kids"]] == ["stuck", "in planning 3d 2h"]
    assert "superseded by sanctioned intake" in _text(_one(stopped, _cls("tk-reason")))


def test_idle_cards_say_how_long_they_sat_in_their_state(ordered: dict) -> None:
    _check_idle(_server(ordered)["pipeline"])


@layout.needs_node
def test_script_idle_cards_say_how_long_they_sat_in_their_state(tmp_path: Path, ordered: dict) -> None:
    _check_idle(_script(tmp_path, ordered)["html"]["pipeline"])


def _check_queued_and_stale(markup: str) -> None:
    queued = _card(markup, "#".join(QUEUED_FOR))
    assert [_text(kid) for kid in _one(queued, _cls("tk-state"))["kids"]][:2] == ["building", "queued"]
    run = _one(queued, _cls("tk-run"))
    assert [_text(kid) for kid in run["kids"]] == ["for", "codex", "waiting 1m 30s"]
    assert "bot" in _one(run, _cls("codex"))["attrs"]["class"].split()
    strip = _canvas(queued)
    assert (strip["data-px"], strip["data-tint"], strip.get("data-still")) == ("conveyor", "codex", "true")
    stale = _card(markup, "#".join(STALE))
    assert "tk-stale" in stale["attrs"]["class"].split()
    assert _state_chips(stale) == [("stale", "warn")]
    assert _text(_one(stale, _cls("tk-hb"))) == "no heartbeat for 4m 10s"
    assert "T+ 19m 40s" in _text(_one(stale, _cls("tk-run")))
    assert _canvas(stale)["data-px"] == "decay" and not page.PX_EFFECTS["decay"]
    for key in LIVE:
        card = _card(markup, "#".join(key))
        assert "tk-stale" not in card["attrs"]["class"] and not _all(card, _cls("tk-hb"))


def test_queued_and_stale_cards_say_what_they_wait_on(ordered: dict) -> None:
    _check_queued_and_stale(_server(ordered)["pipeline"])
    assert ".tk-card.tk-stale{border:1px solid color-mix(in srgb,var(--warn) 45%,transparent);box-shadow:none}" in page.CSS


@layout.needs_node
def test_script_queued_and_stale_cards_match_the_server(tmp_path: Path, ordered: dict) -> None:
    _check_queued_and_stale(_script(tmp_path, ordered)["html"]["pipeline"])


def _check_cap(markup: str) -> None:
    ask = _column(markup, "Ask")
    cards = [kid for kid in ask["kids"] if isinstance(kid, dict) and _cls("tk-card")(kid)]
    assert [c["attrs"]["data-ticket"] for c in cards] == ASK_ORDER[:6]
    (more,) = [kid for kid in ask["kids"] if isinstance(kid, dict) and kid["tag"] == "details"]
    assert "open" not in more["attrs"] and more["attrs"]["class"] == "tk-more"
    assert _text(_one(more, lambda n: n["tag"] == "summary")) == "+2 more"
    assert _placed_in(more) == ASK_ORDER[6:]
    assert _text(_one(ask, _cls("tk-count"))) == "8"
    build = _column(markup, "Build")
    (fold,) = [kid for kid in build["kids"] if isinstance(kid, dict) and kid["tag"] == "details"]
    assert _placed_in(fold) == BUILD_ORDER[6:]
    assert not _all(_column(markup, "Stop"), lambda n: n["tag"] == "details")


def test_a_column_shows_six_cards_and_folds_the_rest(ordered: dict) -> None:
    _check_cap(_server(ordered)["pipeline"])


@layout.needs_node
def test_script_column_cap_matches_the_server(tmp_path: Path, ordered: dict) -> None:
    _check_cap(_script(tmp_path, ordered)["html"]["pipeline"])


MORE_HARNESS = HARNESS.replace(
    "    } else if (kind === 'press') {",
    "    } else if (kind === 'more') {\n"
    "      const node = find(body, n => n.className === 'tk-more');\n"
    "      if (arg === 'toggle') { node.open = !node.open; fire(node, 'toggle'); }\n"
    "      steps.push({open: !!node.open, kept: node === global.lastMore});\n"
    "      global.lastMore = node;\n"
    "    } else if (kind === 'press') {",
)


@layout.needs_node
def test_script_more_box_stays_open_across_redraws(tmp_path: Path, ordered: dict) -> None:
    assert "kind === 'more'" in MORE_HARNESS
    (script,) = re.findall(r"<script>(.*?)</script>", render_pages(ordered)["job.html"], flags=re.S)
    actions = [["more", "look"], ["hashchange", None], ["more", "look"], ["more", "toggle"],
               ["hashchange", None], ["more", "look"]]
    out = layout.run_harness(tmp_path, MORE_HARNESS, script, ordered, "job", IDS, actions)
    assert [step for step in out["steps"] if "open" in step] == [
        {"open": False, "kept": False}, {"open": False, "kept": False}, {"open": True, "kept": True},
        {"open": True, "kept": False}]


SHIPPED_COUNT = 70
HANDED = {4, 18, 35, 47, 63}
UNDATED = SHIPPED_COUNT - 1
SHELF_OPEN = (("maxharris1/factory", "2001", "building"), ("maxharris1/factory", "2002", "intake"),
              ("maxharris1/cleffy", "2003", "review"))


def _shipped(i: int) -> tuple[str, str]:
    return ("maxharris1/cleffy" if i % 3 == 0 else "maxharris1/factory"), str(1000 + i)


def _shipped_age(i: int) -> int:
    return 60 * (i + 1)


def shelf_snapshot(tmp_path: Path) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        def moved(repo: str, ticket_id: str, from_state: str | None, to_state: str, at: float) -> None:
            connection.execute(
                "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
                "VALUES (?, ?, ?, ?, ?, 'poller')", (repo, ticket_id, from_state, to_state, at))

        for i in range(SHIPPED_COUNT):
            repo, ticket_id = _shipped(i)
            state = "handed-off" if i in HANDED else "done" if i % 10 == 7 else "merged"
            add_ticket(connection, repo, ticket_id, state=state, review_round=i % 4)
            if i == UNDATED:
                continue
            at = NOW - _shipped_age(i)
            if state == "done":
                moved(repo, ticket_id, "post-merge", "merged", at)
                moved(repo, ticket_id, "merged", "done", at + 30)
            else:
                moved(repo, ticket_id, "ship", state, at)
        for repo, ticket_id, state in SHELF_OPEN:
            add_ticket(connection, repo, ticket_id, state=state)
            moved(repo, ticket_id, None, state, NOW - 100)
    log = tmp_path / "poller.log"
    log.write_text("cycle\n", encoding="utf-8")
    return collect_snapshot(db, log, now=NOW, chart_path=CHART)


@pytest.fixture(scope="module")
def shelf(tmp_path_factory) -> dict:
    return shelf_snapshot(tmp_path_factory.mktemp("shelf"))


SHELF_HARNESS = HARNESS.replace(
    "    } else if (kind === 'press') {",
    "    } else if (kind === 'shelf') {\n"
    "      const box = find(body, n => n.className === 'tk-earlier');\n"
    "      if (arg === 'toggle') { box.open = !box.open; fire(box, 'toggle'); }\n"
    "      if (arg === 'press') fire(find(box, n => n.tagName === 'button'), 'click');\n"
    "      steps.push({open: !!find(body, n => n.className === 'tk-earlier').open});\n"
    "    } else if (kind === 'press') {",
)


def _shelf_script(tmp_path: Path, snapshot: dict, actions: list) -> list[dict]:
    assert "kind === 'shelf'" in SHELF_HARNESS
    (script,) = re.findall(r"<script>(.*?)</script>", render_pages(snapshot)["job.html"], flags=re.S)
    return layout.run_harness(tmp_path, SHELF_HARNESS, script, snapshot, "job", IDS, actions)["steps"]


def _ago(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds % 60:02d}s"


def _want_tile(i: int) -> dict:
    repo, ticket_id = _shipped(i)
    name = f"{repo.rsplit('/', 1)[-1]}#{ticket_id}"
    rounds = "1 revise round" if i % 4 == 1 else f"{i % 4} revise rounds"
    how = "handed off" if i in HANDED else "merged"
    if i == UNDATED:
        return {"key": f"{repo}#{ticket_id}", "title": f"{name} · {rounds}", "id": name, "r": f"r{i % 4}",
                "ago": None}
    ago = _ago(_shipped_age(i))
    return {"key": f"{repo}#{ticket_id}", "title": f"{name} · {how} {ago} ago · {rounds}", "id": name,
            "r": f"r{i % 4}", "ago": ("handed off " if i in HANDED else "") + f"{ago} ago"}


def _got_tile(tile: dict) -> dict:
    (square,) = _all(tile, _cls("tk-blk"))
    assert square["attrs"].get("aria-hidden") == "true" and not _all(tile, lambda n: n["tag"] == "canvas")
    ago = _all(tile, _cls("tk-ago"))
    return {"key": tile["attrs"]["data-ticket"], "title": tile["attrs"].get("title"),
            "id": _text(_one(tile, _cls("tk-tn"))), "r": _text(_one(tile, _cls("tk-r"))),
            "ago": _text(ago[0]) if ago else None}


def _check_shelf(markup: str, order: list[int], shown: int = 50) -> None:
    shelf = _wide(_dom(markup))
    handed = sum(i in HANDED for i in order)
    assert _text(_one(shelf, _cls("count"))) == str(len(order))
    assert _text(_one(shelf, _cls("tk-sub"))) == f"{len(order) - handed} merged · {handed} handed off"
    newest = _all(shelf, _cls("tk-tiles"))[0]
    assert [_got_tile(t) for t in _all(newest, _cls("tk-tile"))] == [_want_tile(i) for i in order[:10]]
    rest = order[10:]
    drawers = _all(shelf, lambda n: n["tag"] == "details")
    if not rest:
        assert not drawers
        return
    (drawer,) = drawers
    assert "open" not in drawer["attrs"] and drawer["attrs"]["class"] == "tk-earlier"
    assert _text(_one(drawer, lambda n: n["tag"] == "summary")) == f"{len(rest)} earlier"
    assert [_got_tile(t) for t in _all(drawer, _cls("tk-tile"))] == [_want_tile(i) for i in rest[:shown]]
    left = max(0, len(rest) - shown)
    buttons = [_text(b) for b in _all(drawer, lambda n: n["tag"] == "button")]
    notes = [_text(n) for n in _all(drawer, _cls("tk-note"))]
    assert (buttons, notes) == ((["show 50 more"], [f"{left} older not shown · newest first"]) if left else ([], []))


SHIP_ORDER = [i for i in range(SHIPPED_COUNT) if i != UNDATED] + [UNDATED]


def test_a_ticket_row_carries_when_and_how_it_shipped(shelf: dict) -> None:
    from lib.dashboard import RECORDS

    rows = {_key(row): row for row in shelf["tickets"]}
    for key, record in shelf[RECORDS].items():
        found = record["shipped"]
        assert (rows[key]["shipped_at"], rows[key]["shipped_how"]) == (
            (found["at"], found["how"]) if found else (None, None)), key
    for i in range(SHIPPED_COUNT):
        row = rows["#".join(_shipped(i))]
        assert row["shipped_at"] == (None if i == UNDATED else NOW - _shipped_age(i)), i
        assert row["shipped_how"] == ("handed-off" if i in HANDED else "merged"), i
    for repo, ticket_id, _ in SHELF_OPEN:
        assert (rows[f"{repo}#{ticket_id}"]["shipped_at"], rows[f"{repo}#{ticket_id}"]["shipped_how"]) == (None, None)


def test_the_shelf_shows_the_ten_newest_then_fifty_earlier_in_a_closed_drawer(shelf: dict) -> None:
    _check_shelf(_server(shelf)["shipped"], SHIP_ORDER)
    assert re.search(r"@media \(scripting:none\)\{[^@]*\.tk-earlier \.tk-btn\{display:none\}", page.CSS)


@layout.needs_node
def test_script_shelf_matches_the_server(tmp_path: Path, shelf: dict) -> None:
    out = _script(tmp_path, shelf)
    _check_shelf(out["html"]["shipped"], SHIP_ORDER)
    server = _server(shelf)
    for ident in BOARD:
        assert out["html"][ident] == server[ident], ident


@layout.needs_node
def test_show_more_adds_fifty_and_survives_a_redraw_with_the_drawer_open(tmp_path: Path, shelf: dict) -> None:
    actions = [["shelf", "look"], ["shelf", "toggle"], ["shelf", "press"], ["snap", ["shipped"]],
               ["hashchange", None], ["shelf", "look"], ["snap", ["shipped"]]]
    steps = _shelf_script(tmp_path, shelf, actions)
    assert [step["open"] for step in steps if "open" in step] == [False, True, True, True]
    for snap in (step["snap"]["shipped"] for step in steps if "snap" in step):
        _check_shelf(snap, SHIP_ORDER, shown=100)


@layout.needs_node
def test_a_project_press_recomputes_the_shelf(tmp_path: Path, shelf: dict) -> None:
    repos = ["maxharris1/cleffy", "maxharris1/factory", ""]
    actions = [step for repo in repos for step in (["press", ["data-project", repo]], ["snap", BOARD])]
    snaps = [step["snap"] for step in _script(tmp_path, shelf, actions)["steps"] if "snap" in step]
    for repo, snap in zip(repos, snaps):
        _check_shelf(snap["shipped"], [i for i in SHIP_ORDER if repo in ("", _shipped(i)[0])])
        if repo:
            _check_counts(snap, [row for row in shelf["tickets"] if row["repo"] == repo])


def _fewer(shelf: dict, count: int) -> dict:
    keep = {"#".join(_shipped(i)) for i in range(count)} | {f"{repo}#{ticket}" for repo, ticket, _ in SHELF_OPEN}
    return {**shelf, "tickets": [row for row in shelf["tickets"] if _key(row) in keep]}


def _check_phone_shelf(markup: str, order: list[int]) -> None:
    shelf = _dom(markup)
    assert [_got_tile(t) for t in _all(_all(shelf, _cls("tk-tiles"))[0], _cls("tk-tile"))] == [
        _want_tile(i) for i in order[:10]]
    drawers = _all(shelf, lambda n: n["tag"] == "details")
    if len(order) <= 5:
        assert not drawers
        return
    (drawer,) = drawers
    earlier = max(0, len(order) - 10)
    assert drawer["attrs"]["class"] == ("tk-earlier" if earlier else "tk-earlier tk-narrow")
    summary = _one(drawer, lambda n: n["tag"] == "summary")
    assert [(kid["attrs"]["class"], _text(kid)) for kid in summary["kids"]] == [
        ("tk-wide", f"{earlier} earlier"), ("tk-narrow", f"{len(order) - 5} earlier")]
    box = _one(drawer, _cls("tk-drawer"))
    again = box["kids"][0]
    assert again["attrs"]["class"] == "tk-narrow"
    assert [_got_tile(t) for t in _all(again, _cls("tk-tile"))] == [_want_tile(i) for i in order[5:10]]
    assert len(_all(box, _cls("tk-tiles"))) == (2 if earlier else 1)


SHELF_SIZES = (SHIPPED_COUNT, 8, 5)


def test_a_phone_drawer_opens_on_tiles_six_to_ten_then_the_earlier_ones(shelf: dict) -> None:
    for count in SHELF_SIZES:
        _check_phone_shelf(_server(_fewer(shelf, count))["shipped"], [i for i in SHIP_ORDER if i < count])


@layout.needs_node
def test_script_phone_drawer_matches_the_server(tmp_path: Path, shelf: dict) -> None:
    for count in SHELF_SIZES:
        found = _fewer(shelf, count)
        out = _script(tmp_path / str(count), found)
        _check_phone_shelf(out["html"]["shipped"], [i for i in SHIP_ORDER if i < count])
        assert out["html"]["shipped"] == _server(found)["shipped"], count


SHOWN = "e => e.getClientRects().length > 0"
PHONE_BOARD = """(() => {
  const shown = """ + SHOWN + """;
  const buttons = [...document.querySelectorAll('#pipeline .tk-ph')];
  return {buttons: buttons.length, heights: buttons.filter(shown).map(b => b.getBoundingClientRect().height),
          pressed: buttons.filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.dataset.pick),
          columns: [...document.querySelectorAll('#pipeline .tk-col')].filter(shown).map(c => c.dataset.phase),
          tops: [...document.querySelectorAll('#pipeline .tk-col')].filter(shown).map(c => c.offsetTop),
          scroll: document.documentElement.scrollWidth, inner: innerWidth};
})()"""
SHELF_ROWS = """(() => {
  const shown = """ + SHOWN + """;
  const row = document.querySelector('#shipped .tk-shelf > .tk-tiles');
  const drawer = document.querySelector('#shipped details');
  drawer.open = true;
  const opened = [...drawer.querySelectorAll('.tk-tile')].filter(shown).map(t => t.dataset.ticket);
  drawer.open = false;
  return {row: row.getBoundingClientRect().width,
          tiles: [...row.children].filter(shown).map(t => [t.getBoundingClientRect().width,
                                                           t.getBoundingClientRect().height]),
          summary: [...drawer.querySelectorAll('summary span')].filter(shown).map(s => s.textContent),
          summaryHeight: drawer.querySelector('summary').getBoundingClientRect().height, opened};
})()"""


@needs_chrome
def test_a_phone_shows_one_phase_and_five_shelf_rows(snapshot: dict, shelf: dict) -> None:
    browser = Browser()
    try:
        browser.open(_page_with_status_stub(snapshot, "job.html"), 375)
        start = browser.eval(PHONE_BOARD)
        pressed = []
        for phase in COLUMNS:
            browser.eval(f"document.querySelector('[data-pick={phase}]').click()")
            pressed.append(browser.eval(PHONE_BOARD))
        browser.open(_page_with_status_stub(shelf, "job.html"), 375)
        rows = browser.eval(SHELF_ROWS)
        wide = browser.eval("document.documentElement.scrollWidth")
    finally:
        browser.close()
    assert len(start["heights"]) == 6 and min(start["heights"]) >= 44
    assert (start["pressed"], start["columns"]) == (["Plan"], ["Plan"])
    for phase, seen in zip(COLUMNS, pressed):
        assert (seen["pressed"], seen["columns"]) == ([phase], [phase])
        assert seen["scroll"] <= seen["inner"] == 375, phase
    assert len(rows["tiles"]) == 5
    assert all(width == rows["row"] and height >= 44 for width, height in rows["tiles"]), rows
    assert rows["summary"] == ["65 earlier"] and rows["summaryHeight"] >= 44
    order = [_want_tile(i)["key"] for i in SHIP_ORDER]
    assert rows["opened"] == order[5:60]
    assert wide <= 375


@needs_chrome
def test_a_desktop_keeps_six_columns_in_a_row_and_ten_shelf_tiles(shelf: dict) -> None:
    browser = Browser()
    try:
        browser.open(_page_with_status_stub(shelf, "job.html"), 1440)
        board = browser.eval(PHONE_BOARD)
        bar = browser.eval("document.querySelector('#pipeline .tk-phases').getClientRects().length")
        rows = browser.eval(SHELF_ROWS)
    finally:
        browser.close()
    assert bar == 0 and board["heights"] == []
    assert board["columns"] == COLUMNS and len(set(board["tops"])) == 1
    assert len(rows["tiles"]) == 10 and rows["summary"] == ["60 earlier"]
    order = [_want_tile(i)["key"] for i in SHIP_ORDER]
    assert rows["opened"] == order[10:60]


@needs_chrome
def test_a_phone_without_scripts_lists_every_phase(snapshot: dict) -> None:
    browser = Browser()
    try:
        browser.call("Emulation.setScriptExecutionDisabled", value=True)
        page = Path(browser.tmp.name) / "page.html"
        page.write_text(render_pages(snapshot)["job.html"], encoding="utf-8")
        browser.call("Emulation.setDeviceMetricsOverride", width=375, height=900, deviceScaleFactor=1, mobile=False)
        browser.call("Page.navigate", url=page.as_uri())
        deadline = time.monotonic() + 30
        while browser.eval("document.readyState") != "complete" and time.monotonic() < deadline:
            time.sleep(0.05)
        scripting = browser.eval("matchMedia('(scripting:none)').matches")
        seen = browser.eval(PHONE_BOARD)
    finally:
        browser.close()
    assert scripting
    assert seen["buttons"] == 6 and seen["heights"] == [] and seen["columns"] == COLUMNS
    assert seen["scroll"] <= seen["inner"] == 375


@needs_chrome
def test_a_phone_whose_status_fetch_fails_lists_every_phase(snapshot: dict) -> None:
    shim = "<script>window.fetch = function () { return Promise.reject(new Error('offline')); };</script>"
    browser = Browser()
    try:
        page = Path(browser.tmp.name) / "page.html"
        page.write_text(render_pages(snapshot)["job.html"].replace("<script>", shim + "<script>", 1), encoding="utf-8")
        browser.call("Emulation.setDeviceMetricsOverride", width=375, height=900, deviceScaleFactor=1, mobile=False)
        browser.call("Page.navigate", url=page.as_uri())
        deadline = time.monotonic() + 30
        while "static view" not in browser.eval("document.body ? document.body.textContent : ''") and time.monotonic() < deadline:
            time.sleep(0.05)
        link = browser.eval("document.body.textContent.includes('NO LINK · static view')")
        seen = browser.eval(PHONE_BOARD)
    finally:
        browser.close()
    assert link
    assert seen["heights"] == [] and seen["columns"] == COLUMNS
    assert seen["scroll"] <= seen["inner"] == 375


def _find(markup: str) -> dict:
    return _one(_dom(markup), _cls("tk-find"))


def _options(markup: str) -> list[tuple[str, str, str]]:
    return [(_text(li), li["kids"][0]["attrs"]["href"], li["attrs"]["aria-selected"])
            for li in _all(_find(markup), _attr("role", "option"))]


def _expanded(markup: str) -> tuple[str, bool]:
    find = _find(markup)
    listbox = _one(find, _attr("role", "listbox"))
    return _one(find, _attr("id", "find-ticket"))["attrs"]["aria-expanded"], "hidden" in listbox["attrs"]


def _page(snapshot: dict, label: str) -> str:
    (row,) = [row for row in snapshot["tickets"] if _ticket(row) == label]
    return ticket_page_href(row)


def _searched(tmp_path: Path, snapshot: dict, query: str, keys: list[str], *, project: str | None = None) -> dict:
    actions: list = [["press", ["data-project", project]]] if project else []
    actions += [["focus", ["id", "find-ticket"]], ["type", query], ["snap", ["ticket-filter"]]]
    actions += [step for key in keys for step in (["keyon", key], ["snap", ["ticket-filter"]])]
    actions += [["where", None], ["focused", "id"]]
    steps = _script(tmp_path, snapshot, actions)["steps"]
    snaps = [step["snap"]["ticket-filter"] for step in steps if "snap" in step]
    (where,) = [step for step in steps if "where" in step]
    (focused,) = [step["focused"] for step in steps if "focused" in step]
    return {"snaps": snaps, "where": where["where"], "value": where["value"], "focused": focused,
            "prevented": [step["prevented"] for step in steps if "keyon" in step]}


def test_the_toolbar_has_a_labelled_find_a_ticket_box_after_the_view_toggle(snapshot: dict) -> None:
    toolbar = _dom(_server(snapshot)["ticket-filter"])
    (tools, find) = [kid for kid in toolbar["kids"] if isinstance(kid, dict)]
    assert [g["attrs"]["aria-label"] for g in _all(tools, _attr("role", "group"))] == ["Project", "View"]
    assert find["attrs"] == {"class": "tk-find", "role": "search"}
    label = _one(find, lambda n: n["tag"] == "label")
    field = _one(find, lambda n: n["tag"] == "input")
    assert _text(label) == "Find a ticket" and label["attrs"]["for"] == field["attrs"]["id"] == "find-ticket"
    assert {k: field["attrs"][k] for k in ("type", "placeholder", "autocomplete", "spellcheck", "role",
                                           "aria-controls", "aria-expanded")} == {
        "type": "text", "placeholder": "factory#470", "autocomplete": "off", "spellcheck": "false",
        "role": "combobox", "aria-controls": "find-ticket-list", "aria-expanded": "false"}
    listbox = _one(find, _attr("id", "find-ticket-list"))
    assert listbox["attrs"]["role"] == "listbox" and "hidden" in listbox["attrs"] and not listbox["kids"]
    opener = _one(find, _cls("tk-open"))
    assert opener["attrs"]["aria-disabled"] == "true" and "href" not in opener["attrs"]
    assert re.search(r"@media \(scripting:none\)\{[^@]*#ticket-filter\{display:none\}", page.CSS)


@layout.needs_node
def test_find_lists_eight_matches_from_every_project_whatever_the_filter(tmp_path: Path, snapshot: dict) -> None:
    found = _searched(tmp_path, snapshot, "10", [], project="maxharris1/cleffy")
    options = _options(found["snaps"][0])
    assert len(options) == 8 and len(snapshot["tickets"]) > 8
    assert {label.split("#")[0] for label, _, _ in options} == {"factory", "cleffy", "aut0"}
    assert _expanded(found["snaps"][0]) == ("true", False)
    assert all(selected == "false" for _, _, selected in options)
    assert found["focused"] == ["input", "find-ticket"] and found["value"] == "10"


@layout.needs_node
def test_find_ranks_an_exact_id_first_then_prefixes_then_the_rest(tmp_path: Path, snapshot: dict) -> None:
    labels = [label for label, _, _ in _options(_searched(tmp_path, snapshot, "AUT-1", [])["snaps"][0])]
    assert labels and all(label.startswith("aut0#AUT-1") for label in labels)
    labels = [label for label, _, _ in _options(_searched(tmp_path / "sub", snapshot, "11", [])["snaps"][0])]
    ids = [str(row["ticket_id"]) for row in snapshot["tickets"]]
    starts = [_ticket(row) for row, i in zip(snapshot["tickets"], ids) if i.startswith("11")]
    inside = [_ticket(row) for row, i in zip(snapshot["tickets"], ids) if "11" in i and not i.startswith("11")]
    assert len(starts) < 8 < len(starts) + len(inside) and inside[0].startswith("aut0#")
    assert labels == (starts + inside)[:8]
    exact = "cleffy#200"
    options = _options(_searched(tmp_path / "exact", snapshot, "200", [])["snaps"][0])
    assert options[0][:2] == (exact, _page(snapshot, exact))
    assert [label for label, _, _ in _options(_searched(tmp_path / "none", snapshot, "zzz", [])["snaps"][0])] == []


@layout.needs_node
def test_arrow_keys_pick_a_match_and_enter_opens_its_ticket_page(tmp_path: Path, snapshot: dict) -> None:
    found = _searched(tmp_path, snapshot, "1", ["ArrowDown", "ArrowDown", "ArrowUp", "ArrowUp", "ArrowDown",
                                                "ArrowDown", "Enter"])
    picked = [[i for i, (_, _, sel) in enumerate(_options(snap)) if sel == "true"] for snap in found["snaps"][1:7]]
    assert picked == [[0], [1], [0], [7], [0], [1]]
    second = _options(found["snaps"][6])[1]
    field = _one(_find(found["snaps"][6]), _attr("id", "find-ticket"))
    assert field["attrs"]["aria-activedescendant"] == "find-ticket-1"
    assert _one(_find(found["snaps"][6]), _cls("tk-open"))["attrs"]["href"] == second[1]
    assert found["where"] == second[1] and found["prevented"] == [True] * 7


@layout.needs_node
def test_enter_with_nothing_picked_opens_the_first_match(tmp_path: Path, snapshot: dict) -> None:
    found = _searched(tmp_path, snapshot, "cleffy#200", ["Enter"])
    assert found["where"] == _page(snapshot, "cleffy#200")
    assert _one(_find(found["snaps"][0]), _cls("tk-open"))["attrs"]["href"] == found["where"]


@layout.needs_node
def test_escape_closes_the_list_and_keeps_the_text(tmp_path: Path, snapshot: dict) -> None:
    found = _searched(tmp_path, snapshot, "1", ["ArrowDown", "Escape", "Enter"])
    assert _expanded(found["snaps"][2]) == ("false", True)
    assert found["value"] == "1" and found["where"] == _options(found["snaps"][0])[0][1]


@layout.needs_node
def test_no_match_shows_no_list_and_a_disabled_open(tmp_path: Path, snapshot: dict) -> None:
    found = _searched(tmp_path, snapshot, "no-such-ticket", ["ArrowDown", "Enter"])
    for snap in found["snaps"]:
        assert _options(snap) == [] and _expanded(snap) == ("false", True)
        opener = _one(_find(snap), _cls("tk-open"))
        assert "href" not in opener["attrs"] and opener["attrs"]["aria-disabled"] == "true"
    assert found["where"] is None and found["prevented"] == [True, False]


@layout.needs_node
def test_a_redraw_while_typing_keeps_the_text_focus_and_picked_match(tmp_path: Path, snapshot: dict) -> None:
    actions = [["focus", ["id", "find-ticket"]], ["type", "1"], ["keyon", "ArrowDown"], ["keyon", "ArrowDown"],
               ["snap", ["ticket-filter"]], ["hashchange", None], ["press", ["data-view", "table"]],
               ["focus", ["id", "find-ticket"]], ["hashchange", None], ["snap", ["ticket-filter"]],
               ["focused", "id"], ["where", None], ["keyon", "Enter"], ["where", None]]
    steps = _script(tmp_path, snapshot, actions)["steps"]
    before, after = (step["snap"]["ticket-filter"] for step in steps if "snap" in step)
    assert _options(after) == _options(before) and _options(after)[1][2] == "true"
    assert _expanded(after) == ("true", False)
    assert [step["focused"] for step in steps if "focused" in step] == [["input", "find-ticket"]]
    wheres = [step for step in steps if "where" in step]
    assert wheres[0] == {"where": None, "value": "1"}
    assert wheres[1]["where"] == _options(before)[1][1]
    assert _all(_dom(after), _attr("data-view", "table"))[0]["attrs"]["aria-pressed"] == "true"


@layout.needs_node
def test_typing_a_repo_and_id_opens_that_ticket_even_when_its_pr_differs(tmp_path: Path, snapshot: dict) -> None:
    changed = copy.deepcopy(snapshot)
    row = next(row for row in changed["tickets"] if row["repo"] == "maxharris1/cleffy" and row["ticket_id"] == "119")
    row.update(ticket_id="10", pr=55)
    found = _searched(tmp_path, changed, "cleffy#10", ["Enter"])
    assert _options(found["snaps"][0])[0][0] == "cleffy#10 (PR #55)"
    assert found["where"] == ticket_page_href(row)


@layout.needs_node
def test_moving_focus_out_of_the_box_closes_the_list(tmp_path: Path, snapshot: dict) -> None:
    actions = [["focus", ["id", "find-ticket"]], ["type", "1"], ["keyon", "ArrowDown"],
               ["leave", ["class", "tk-open"]], ["snap", ["ticket-filter"]],
               ["leave", ["data-view", "table"]], ["hashchange", None], ["snap", ["ticket-filter"]], ["where", None]]
    steps = _script(tmp_path, snapshot, actions)["steps"]
    inside, outside = (step["snap"]["ticket-filter"] for step in steps if "snap" in step)
    assert _expanded(inside) == ("true", False)
    assert _expanded(outside) == ("false", True)
    assert [step for step in steps if "where" in step] == [{"where": None, "value": "1"}]


def _collected(tmp_path: Path, ids: list[str], *, owner: str = "maxharris1", source: str | None = None) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    repo = f"{owner}/factory"
    with sqlite3.connect(db) as connection:
        for ident in ids:
            add_ticket(connection, repo, ident, state="intake")
        if source:
            connection.execute("INSERT INTO ticket_sources (repo, ticket_id, source, pr_number) VALUES (?, ?, ?, ?)",
                               (repo, ids[-1], source, ids[-1]))
    return collect_snapshot(db, tmp_path / "poller.log", now=NOW)


@layout.needs_node
def test_an_id_fragment_is_not_crowded_out_by_a_digit_in_the_owner(tmp_path: Path) -> None:
    ids = ["200", "202", "203", "204", "205", "206", "207", "208", "209", "291"]
    snap = _collected(tmp_path, ids)
    found = _searched(tmp_path / "run", snap, "1", ["Enter"])
    labels = [label for label, _, _ in _options(found["snaps"][0])]
    assert labels[0] == "factory#291" and len(labels) == 8
    assert found["where"] == _page(snap, "factory#291")


@layout.needs_node
@pytest.mark.parametrize("query", ["12", "#12"])
def test_an_exact_source_issue_id_ranks_before_id_prefixes(tmp_path: Path, query: str) -> None:
    snap = _collected(tmp_path, [str(i) for i in range(120, 128)] + ["999"], source="#12")
    found = _searched(tmp_path / "run", snap, query, ["Enter"])
    labels = [label for label, _, _ in _options(found["snaps"][0])]
    assert labels[0] == "factory#12 (PR #999)"
    (row,) = [row for row in snap["tickets"] if row["ticket_id"] == "999"]
    assert found["where"] == ticket_page_href(row)


SHIFT = 8


def _press_key(chrome, key: str, code: int, modifiers: int = 0) -> None:
    for kind in ("rawKeyDown", "keyUp"):
        chrome.send("Input.dispatchKeyEvent", chrome.session, type=kind, key=key, code=key,
                    windowsVirtualKeyCode=code, modifiers=modifiers)


FIELD_STATE = """(() => {
  const f = document.getElementById('find-ticket');
  return {value: f.value, selection: [f.selectionStart, f.selectionEnd], focused: document.activeElement === f,
          options: [...document.querySelectorAll('#find-ticket-list [role=option]')].map(li => li.textContent)};
})()"""


def test_the_first_render_keeps_a_keyboard_selection_in_the_find_box(tmp_path: Path, snapshot: dict) -> None:
    import functools
    import http.server
    import threading

    from tests.test_dashboard_system import CHROME, QuietHandler, _Chrome

    if CHROME is None:
        pytest.skip("Chrome is not installed")
    site = tmp_path / "site"
    site.mkdir()
    (site / "job.html").write_text(render_pages(snapshot)["job.html"], encoding="utf-8")
    (site / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    gate = threading.Event()

    class Held(QuietHandler):
        def do_GET(self) -> None:
            if self.path.endswith("/status.json"):
                gate.wait(30)
            super().do_GET()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Held, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chrome = _Chrome(tmp_path / "profile")
    try:
        chrome.send("Page.navigate", chrome.session, url=f"http://127.0.0.1:{server.server_address[1]}/job.html")
        chrome.wait_for("document.readyState !== 'loading' && !!document.getElementById('find-ticket')", "find box")
        chrome.run("window.serverList = document.getElementById('find-ticket-list');"
                   "document.getElementById('find-ticket').focus()")
        chrome.send("Input.insertText", chrome.session, text="107")
        _press_key(chrome, "ArrowLeft", 37, SHIFT)
        held = chrome.run(FIELD_STATE)
        gate.set()
        chrome.wait_for("document.getElementById('find-ticket-list') !== window.serverList", "first render")
        drawn = chrome.run(FIELD_STATE)
        chrome.send("Input.insertText", chrome.session, text="4")
        typed = chrome.run(FIELD_STATE)
    finally:
        gate.set()
        chrome.close()
        server.shutdown()
    assert held == {"value": "107", "selection": [2, 3], "focused": True, "options": []}
    assert (drawn["value"], drawn["selection"], drawn["focused"]) == ("107", [2, 3], True)
    assert typed["value"] == "104" and typed["options"][0] == "cleffy#104"
