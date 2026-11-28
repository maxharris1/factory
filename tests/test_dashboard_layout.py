"""Dashboard layout: a lanes-first main page, plus job, activity and
efficiency pages.

- Main page (index.html): factory status on top, lanes and slots as the
  centerpiece with each slot's job (ticket or PR, bot, step, round, elapsed),
  the queue in a side rail. The attention strip under the header band (#139)
  links to the Activity page. No poll/Linear budget, handoffs, verdicts, tickets, log or
  savings.
- Job page (job.html): the Tickets board and its table view, and a flow
  diagram drawn from model/chart.mmd (the statechart's checked-in chart),
  with the job's path and current state highlighted, its round and its time
  in that state.
- Efficiency page (efficiency.html): poll and Linear budget, handoff savings
  and a one-line definition of scripted share.
- Evals page (evals.html): the weekly eval table, newest week first.
- #63 FU-1: a lane counts max(held, running) slots.
Every page renders its content server-side, so it reads without script.
"""

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import lib.dashboard as dashboard
import lib.dashboard_flow as flow
from lib.dashboard import collect_snapshot, render_pages, tickets_json, write_snapshot, write_snapshot_pages
from lib.dashboard_pages import cockpit, common, lanes, shell
from lib.dashboard_pages.counts import _counts
from lib.dashboard_pages.common import ticket_address, ticket_page_href
from lib.dashboard_pages.ticket_detail import ticket_view
from tests.test_dashboard import (
    FREE_TEXT_SECRETS,
    GH_CLASSIC,
    NOW,
    WEBHOOK_URL,
    add_job,
    add_ticket,
    seeded,
)

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "model" / "chart.mmd"
MINI = ROOT / "tests" / "fixtures" / "statechart" / "mini.mmd"
NAV = (  # nav order (#78, #92, #777)
    "index.html", "job.html", "activity.html", "efficiency.html", "health.html",
)
PAGES = (*NAV, "evals.html", "settings.html", "ticket.html")


def _slots_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS device_slots (job_id TEXT PRIMARY KEY, "
        "device TEXT, harness TEXT, os_user TEXT, placed_at REAL)"
    )


def _machines(tmp_path: Path) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(
        "devices:\n"
        "  - {name: box, local: true, slot_max: 3, harnesses: [claude, codex, cursor]}\n"
        "  - {name: pc, slot_max: 2, harnesses: [codex]}\n"
        "  - {name: mac, enabled: false, slot_max: 2, harnesses: [claude]}\n",
        encoding="utf-8",
    )
    return path


def _history(connection: sqlite3.Connection) -> None:
    """Ticket 40: built, reviewed, revised once, now in its second review.

    The ticket has one completed review round; the running job's detail
    says it is round 2 of 3, and that is the round a slot shows (#75)."""
    add_ticket(connection, "maxharris1/cleffy", "40", state="review", review_round=1)
    for job_id, action, recipient, state, created, detail in (
        ("h-build", "build", "claude", "done", NOW - 5000, None),
        ("h-review1", "review", "codex", "done", NOW - 4000, '{"round":"1/3"}'),
        ("h-fix", "fix", "claude", "done", NOW - 3000, None),
        ("h-review2", "review", "codex", "running", NOW - 2000, '{"round":"2/3"}'),
    ):
        add_job(
            connection,
            job_id,
            action=action,
            recipient=recipient,
            state=state,
            ticket_id="40",
            created_at=created,
            last_activity_at=NOW - 30,
            detail=detail,
        )


def _snapshot(tmp_path: Path, **kwargs) -> dict:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _history(connection)
    return collect_snapshot(db, log, now=NOW, **kwargs)


def _pages(tmp_path: Path, **kwargs) -> dict[str, str]:
    return render_pages(_snapshot(tmp_path, **kwargs))


def _no_script(html: str) -> str:
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)


def _section(html: str, ident: str) -> str:
    """The server-rendered markup of the element with this id."""
    match = re.search(rf'<(section|article|div|aside|nav|main|a)\b[^>]*\bid="{re.escape(ident)}"', html)
    assert match, ident
    tag = match.group(1)
    depth = 0
    for item in re.finditer(rf"<(/?){tag}\b[^>]*>", html[match.start() :]):
        depth += -1 if item.group(1) else 1
        if depth == 0:
            return html[match.start() : match.start() + item.end()]
    raise AssertionError(f"unclosed {ident}")


def _text(markup: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup)).strip()


def _job_row(snapshot: dict, job: str) -> dict:
    return next(row for row in snapshot["running"] + snapshot["queued"] if row["job"] == job)


def _ticket_page(snapshot: dict, job: str) -> str:
    return ticket_view(snapshot, ticket_address(_job_row(snapshot, job)))


def _tile(html: str, snapshot: dict, job: str) -> re.Match | None:
    href = re.escape(ticket_page_href(_job_row(snapshot, job)))
    return re.search(rf'<a class="tile [^"]*" href="{href}" title="[^"]*\(job {re.escape(job)}\)">.*?</a>', html, re.S)


def _chart_source(path: Path) -> tuple[set[str], set[tuple[str, str]]]:
    """States and transitions read straight from a chart file, independently
    of lib.dashboard_flow."""
    text = path.read_text(encoding="utf-8")
    names = dict(re.findall(r"^  (s\d+): (.+)$", text, flags=re.M))
    edges = {
        (names[a], names[b])
        for a, b in re.findall(r"^  (s\d+) --> (s\d+)", text, flags=re.M)
    }
    return set(names.values()), edges


# --- the pages and how they are written ----------------------------------------------


def test_snapshot_writes_status_json_and_every_page(tmp_path: Path) -> None:
    out = tmp_path / "out"
    paths = write_snapshot_pages(out, _snapshot(tmp_path))
    assert dashboard.PAGES == PAGES
    assert paths == (
        out / "status.json",
        *(out / name for name in dashboard.STATIC_ASSETS),
        *(out / name for name in PAGES),
        out / "tickets.json",
        out / "evals.json",
    )
    assert sorted(path.name for path in out.iterdir()) == sorted(
        ("status.json", *dashboard.STATIC_ASSETS, *PAGES, "tickets.json", "evals.json")
    )


def test_every_page_links_every_other_page(tmp_path: Path) -> None:
    pages = _pages(tmp_path)
    for name, html in pages.items():
        nav = _section(html, "nav")
        for target in NAV:
            assert f'href="{target}"' in nav, (name, target)


def test_static_server_serves_every_page() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "factory_dashboard_http_layout", ROOT / "deploy" / "factory-dashboard-http.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in PAGES:
        assert module.ROUTES[f"/{name}"] == name
        assert module.CONTENT_TYPES[name] == "text/html; charset=utf-8"
    assert module.ROUTES["/"] == "index.html"


# --- main page --------------------------------------------------------------------------


def test_main_page_has_needs_max_and_intake_then_lanes_then_just_happened(tmp_path: Path) -> None:
    html = _pages(tmp_path)["index.html"]
    for panel in ("needs-max", "lanes", "queue", "just-happened", "intake"):
        assert f'id="{panel}"' in html, panel
    # Moved to the Activity and Efficiency pages.
    for panel in ("health", "handoffs", "verdicts", "tickets", "log", "savings"):
        assert f'id="{panel}"' not in html, panel
    for words in ("Poll and Linear budget", "Recent handoffs", "Recent verdicts",
                  "Handoff savings", "poller.log", "scripted share"):
        assert words.lower() not in _text(_no_script(html)).lower(), words
    hud = _section(html, "hud")
    heading_end = hud.index("</h1>") + len("</h1>")
    assert hud[hud.index(">") + 1 :].startswith('<h1 class="vh"'), hud[:200]
    assert hud[heading_end:].startswith('<section class="panel" id="counts"'), hud[heading_end : heading_end + 200]
    order = ['id="counts"', 'id="needs-max"', 'id="intake"', 'id="lanes"', 'id="queue"', 'data-device="box"',
             'id="just-happened"']
    positions = [html.index(needle) for needle in order]
    assert positions == sorted(positions), dict(zip(order, positions))
    assert "lanes-first" not in html
    rest, children = hud[heading_end : -len("</main>")], []
    while rest:
        child = re.match(r'<section class="panel" id="([^"]+)"', rest)
        assert child, rest[:200]
        children.append(child.group(1))
        rest = rest[len(_section(rest, child.group(1))) :]
    assert children == ["counts", "needs-max", "intake", "lanes", "just-happened"]
    lane_panel = _section(html, "lanes")
    assert 'id="queue" data-device="queue"' in lane_panel
    assert re.findall(r'class="lane[^"]*" (?:id="[^"]*" )?data-device="([^"]+)"', lane_panel)[-1] == "ci"


def test_main_page_panels_are_five_full_width_rows() -> None:
    assert "lanes-first" not in lanes.CSS
    assert "#hud #lanes,#needs-max,#intake,#just-happened{grid-column:1/-1}" in lanes.CSS
    assert ".pair" not in lanes.CSS
    assert shell._PANELS["index.html"] == (_counts, cockpit._needs_max, lanes._intake, lanes._lanes,
                                           lanes._just_happened)


def test_script_renderer_order_matches_the_server_page(tmp_path: Path) -> None:
    mounts = {"renderCounts": "counts", "renderNeedsMax": "needs-max", "renderIntake": "intake",
              "renderLanes": "lanes", "renderJustHappened": "just-happened"}
    listed = re.search(r"\bmain: \[([^\]]*)\]", common.SCRIPT_TAIL)
    assert listed, "PAGES.main"
    script_order = [mounts[name.strip()] for name in listed.group(1).split(",")]
    html = _pages(tmp_path)["index.html"]
    assert script_order == sorted(mounts.values(), key=lambda ident: html.index(f'id="{ident}"'))


def test_main_page_slot_shows_ticket_bot_step_round_and_elapsed(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    lanes = _section(_no_script(render_pages(snapshot)["index.html"]), "lanes")
    card = _tile(lanes, snapshot, "h-review")
    assert card, lanes
    text = _text(card.group(0))
    assert "cleffy#40" in text and "PR" not in text.split()
    assert "codex" in text  # bot
    assert "review" in text  # step
    assert "2/3" in text  # the job's detail.round, not the ticket's count
    assert "T+ 33m 20s" in text  # elapsed since it was queued


def test_a_linear_slot_shows_its_ticket_not_a_pr(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_job(connection, "lin-build", state="running", repo="aut0-code/aut0",
                ticket_id="AUT-7", created_at=NOW - 30)
    snapshot = collect_snapshot(db, log, now=NOW)
    card = _tile(_no_script(render_pages(snapshot)["index.html"]), snapshot, "lin-buil")
    assert card
    text = _text(card.group(0))
    assert "AUT-7" in text and "ticket" not in text.split() and "PR" not in text.split()


def test_queue_items_open_their_ticket_page(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    queue = _section(_no_script(render_pages(snapshot)["index.html"]), "queue")
    assert sorted(re.findall(r'<li><a href="([^"]*)"', queue)) == sorted(
        ticket_page_href(_job_row(snapshot, job)) for job in ("q-claude", "g-grok-c"))
    assert "cleffy#12" in _text(queue)


def test_attention_items_are_listed_once_each(tmp_path: Path) -> None:
    attention = collect_snapshot(*seeded(tmp_path), now=NOW)["attention"]
    # AUT-7 is stuck (and so also paused); it is one item, not two.
    assert [(row["kind"], row["ticket_id"]) for row in attention["items"]] == [
        ("stuck", "AUT-7"),
        ("overdue", "14"),
        ("paused", "5"),
    ]
    assert attention["count"] == 3


def test_attention_strip_counts_and_links_to_activity(tmp_path: Path) -> None:
    """#139: the strip under the band replaces the badge."""
    pages = _pages(tmp_path)
    for name, html in pages.items():
        strip = _section(_no_script(html), "attention-strip")
        href = "#attention" if name == "activity.html" else "activity.html#attention"
        assert f'<a class="attn-count" href="{href}">' in strip, name
        assert re.search(r'<b id="attention-count">3</b>', strip), (name, strip)
        assert "3 need attention" in _text(strip)
        assert "attention-badge" not in html


def test_attention_strip_is_absent_when_all_clear(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM ticket_state")
        connection.execute("UPDATE jobs SET last_activity_at = ?", (NOW - 5,))
    snapshot = collect_snapshot(db, log, now=NOW)
    assert snapshot["attention"] == {"count": 0, "items": []}
    for name, html in render_pages(snapshot).items():
        assert '<div class="attn" id="attention-strip" role="region" aria-label="Needs attention" hidden></div>' in html, name
        assert "attention-count" not in _no_script(html), name


# --- #63 FU-1: a lane counts max(held, running) ----------------------------------------


def _held_more_than_running(tmp_path: Path) -> dict:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _slots_table(connection)
        # r-codex-review runs on pc; a second slot is still held there by a
        # job that already finished (its slot not yet released).
        connection.execute(
            "INSERT INTO device_slots VALUES ('r-codex-review', 'pc', 'codex', 'codex-bot', ?)",
            (NOW - 300,),
        )
        connection.execute(
            "INSERT INTO device_slots VALUES ('d-codex-review-1', 'pc', 'codex', 'codex-bot', ?)",
            (NOW - 900,),
        )
    return collect_snapshot(db, log, now=NOW, machines_path=_machines(tmp_path))


def test_lane_uses_held_slots_when_more_than_running(tmp_path: Path) -> None:
    snapshot = _held_more_than_running(tmp_path)
    pc = next(row for row in snapshot["slots"]["devices"] if row["name"] == "pc")
    assert pc["held"] == 2
    lanes = _section(_no_script(render_pages(snapshot)["index.html"]), "lanes")
    card = re.search(r'<div class="lane[^"]*" data-device="pc">.*?(?=<div class="lane[ "]|$)', lanes, re.S)
    assert card, lanes
    text = _text(card.group(0))
    assert "2 / 2 slots" in text
    assert "idle" not in text  # the held slot is not shown as free
    assert "held" in text


def test_lane_uses_running_when_more_than_held(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    snapshot = collect_snapshot(db, log, now=NOW, machines_path=_machines(tmp_path))
    lanes = _section(_no_script(render_pages(snapshot)["index.html"]), "lanes")
    card = re.search(r'<div class="lane[^"]*" data-device="box">.*?(?=<div class="lane[ "]|$)', lanes, re.S)
    text = _text(card.group(0))
    # Two running box jobs, no device_slots rows: 2 used, 1 idle.
    assert "2 / 3 slots" in text
    assert text.count("idle") == 1


# --- activity page -------------------------------------------------------------------------


def test_activity_page_has_attention_and_relay_only(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO handled_comments (repo, ticket_id, comment_id, outcome) "
            "VALUES ('maxharris1/cleffy', '13', 'c2', 'queued-review-fix')"
        )
        connection.execute("UPDATE jobs SET finished_at = ? WHERE id = 'd-codex-review-1'", (NOW - 100,))
    html = _no_script(render_pages(collect_snapshot(db, log, now=NOW))["activity.html"])
    for panel in ("attention", "relay"):
        assert f'id="{panel}"' in html, panel
    relay = _text(_section(html, "relay"))
    assert "queued-review-fix" in relay and "codex" in relay
    attention = _text(_section(html, "attention"))
    assert "AUT-7" in attention and "job-timeout" in attention
    assert "cleffy#14" in attention and "tracy-harris-coaching#5" in attention
    for panel in ("lanes", "queue", "health", "savings", "tickets", "handoffs", "verdicts", "log"):
        assert f'id="{panel}"' not in html, panel
    assert "poller.log" not in _text(html)


def test_job_page_has_the_board_and_the_tickets_table(tmp_path: Path) -> None:
    html = _no_script(render_pages(collect_snapshot(*seeded(tmp_path), now=NOW))["job.html"])
    for panel in ("ticket-filter", "pipeline", "shipped", "tickets"):
        assert f'id="{panel}"' in html, panel
    assert 'id="jobs"' not in html
    tickets = _text(_section(html, "tickets"))
    assert "AUT-7" in tickets and "waiting-max" in tickets
    assert "AUT-7" not in _text(_section(html, "shipped"))
    assert "AUT-7" in _text(_section(html, "pipeline"))
    assert "tracy-harris-coaching#5" in _text(_section(html, "pipeline"))


# --- efficiency page --------------------------------------------------------------------------


def test_efficiency_page_has_savings_and_a_one_line_definition(tmp_path: Path) -> None:
    pages = _pages(tmp_path, last_cycle_at=NOW - 4)
    html = _no_script(pages["efficiency.html"])
    assert 'id="savings"' in html
    health = _text(_section(_no_script(pages["health.html"]), "health"))
    assert "OK" in health and "1,250 req/h" in health
    savings = _section(html, "savings")
    line = re.search(r'<p class="definition" id="share-definition">(.*?)</p>', savings, re.S)
    assert line, savings
    definition = _text(line.group(1))
    assert definition == dashboard.SCRIPTED_SHARE_DEFINITION
    assert "\n" not in dashboard.SCRIPTED_SHARE_DEFINITION
    assert definition.lower().startswith("scripted share")
    assert len(definition) <= 200
    assert "50%" in _text(savings)
    for panel in ("lanes", "queue", "handoffs", "verdicts", "tickets", "log", "health"):
        assert f'id="{panel}"' not in html, panel


def _main_children(html: str) -> list:
    children: list = []
    depth = 0
    for tag in re.finditer(r"<(/?)(section|div|article|aside)\b([^>]*)>", _section(_no_script(html), "hud")):
        if tag.group(1):
            depth -= 1
            continue
        depth += 1
        ident = re.search(r'\bid="([^"]+)"', tag.group(3))
        row = re.search(r'\bclass="((?:ef|sys)-row)"', tag.group(3))
        if depth == 1:
            children.append((row.group(1), []) if row else ident.group(1))
        elif depth == 2 and ident and isinstance(children[-1], tuple):
            children[-1][1].append(ident.group(1))
    return children


def test_system_page_draws_devices_accounts_and_routing_after_the_log(tmp_path: Path) -> None:
    html = _pages(tmp_path)["health.html"]
    assert _main_children(html) == [
        "broken", "components", "versions", ("sys-row", ["health", "log"]), "settings",
        "accounts", "routing", "rules", "waiters",
    ]
    ids = re.findall(r'\bid="([^"]+)"', _no_script(html))
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_efficiency_page_puts_headroom_first_then_balance_then_evals_beside_cost(tmp_path: Path) -> None:
    pages = _pages(tmp_path)
    page = _no_script(pages["efficiency.html"])
    hud = _section(page, "hud")
    evals = _section(page, "evals")
    assert evals == _section(pages["evals.html"], "evals")
    wrapper = '<div style="display:grid;flex:3 1 560px;min-width:0">'
    top = f'<div class="ef-row">{wrapper}{evals}</div>{_section(page, "tokens")}</div>'
    assert top in hud
    assert re.findall(r"<[^>]*\bstyle=[^>]*>", top) == [wrapper]
    assert _main_children(page) == [
        ("ef-row", ["headroom"]), ("ef-row", ["balance"]), ("ef-row", ["tokens"]),
        ("ef-row", ["rounds", "savings"]), "jobs-lifetime",
    ]


# --- the statechart and the job path ------------------------------------------------------


def test_parse_chart_reads_states_edges_and_legend() -> None:
    chart = flow.load_chart(MINI)
    assert chart is not None
    assert chart["start"] == "intake"
    assert chart["states"] == [
        {"name": "intake", "group": "blue"},
        {"name": "building", "group": "orange"},
        {"name": "review", "group": "yellow"},
        {"name": "fixing", "group": "orange"},
        {"name": "handed-off", "group": "teal"},
    ]
    assert [(e["from"], e["to"]) for e in chart["edges"]] == [
        ("intake", "building"),
        ("building", "review"),
        ("review", "fixing"),
        ("fixing", "review"),
        ("review", "handed-off"),
    ]
    assert chart["edges"][0]["label"] == "kind=go, queue build"
    assert chart["legend"] == [
        {"name": "Ask", "group": "blue"},
        {"name": "Build", "group": "orange"},
        {"name": "Review", "group": "yellow"},
        {"name": "Ship", "group": "teal"},
    ]


def test_missing_or_broken_chart_degrades(tmp_path: Path) -> None:
    assert flow.load_chart(tmp_path / "missing.mmd") is None
    bad = tmp_path / "bad.mmd"
    bad.write_text("not a chart\n", encoding="utf-8")
    assert flow.load_chart(bad) is None
    snapshot = _snapshot(tmp_path, chart_path=bad)
    assert snapshot["statechart"] is None
    job = _no_script(render_pages(snapshot)["job.html"])
    assert "statechart unavailable" in job


def _layout_of(chart_path: Path) -> tuple[set[str], set[tuple[str, str]]]:
    layout = flow.flowmap_layout(flow.load_chart(chart_path))
    return {n["name"] for n in layout["nodes"]}, {tuple(key.split(">")) for key in layout["edges"]}


def test_job_page_diagram_is_the_real_statechart(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    assert snapshot["statechart"]["source"] == "chart.mmd"
    states, transitions = _chart_source(CHART)
    assert _layout_of(CHART) == (states, transitions)
    assert len(states) == 23  # section 4.1, with awaiting-ok (#86), merge-queue and updating (#130), handed-off-externally (#749)
    assert "Full state chart <span class=\"count\">· 23 states," in _ticket_page(snapshot, "h-review")


def test_changing_the_fixture_statechart_changes_the_diagram(tmp_path: Path) -> None:
    nodes, edges = _layout_of(MINI)
    assert nodes == {"intake", "building", "review", "fixing", "handed-off"}
    assert ("review", "handed-off") in edges
    assert ("fixing", "waiting-max") not in edges

    changed = tmp_path / "changed.mmd"
    text = MINI.read_text(encoding="utf-8")
    text = text.replace("  s4: handed-off\n", "  s4: handed-off\n  s5: waiting-max\n")
    text = text.replace("  s2 --> s4: ship = handoff\n", "  s3 --> s5: a product question\n")
    changed.write_text(text, encoding="utf-8")
    nodes, edges = _layout_of(changed)
    assert nodes == {"intake", "building", "review", "fixing", "handed-off", "waiting-max"}
    assert ("review", "handed-off") not in edges
    assert "· 6 states," in _ticket_page(_snapshot(tmp_path / "b", chart_path=changed), "h-review")


def _flowmap_names(script: str) -> set[str]:
    layout = json.loads(re.search(r"var FLOWMAP = (.*);", script).group(1))
    return {node["name"] for node in layout["nodes"]}


def test_the_flowmap_draws_the_snapshots_chart(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "mini", chart_path=MINI)
    expected = {state["name"] for state in snapshot["statechart"]["states"]}
    assert len(expected) == 5
    assert _flowmap_names(dashboard.render_static_assets(snapshot)["app.js"]) == expected
    default = _snapshot(tmp_path / "default")
    assert len(_flowmap_names(dashboard.render_static_assets(default)["app.js"])) == 23
    broken = dict(snapshot, statechart=None)
    assert "var FLOWMAP = null;" in dashboard.render_static_assets(broken)["app.js"]


def _wide_chart(path: Path, working: int, stops: int) -> Path:
    names = [f"step-{i}" for i in range(working)] + [f"halt-{i}" for i in range(stops)]
    lines = ["stateDiagram-v2", '  state "Legend" as legend {', "    lg0: Build", "    lg1: Stop", "  }",
             "  class lg0 orange", "  class lg1 red"]
    lines += [f"  s{i}: {name}" for i, name in enumerate(names)]
    lines.append("  [*] --> s0")
    lines += [f"  s{i} --> s{i + 1}: next" for i in range(working - 1)]
    lines += [f"  s{working - 1} --> s{working + i}: halt" for i in range(stops)]
    lines += [f"  class s{i} {'orange' if i < working else 'red'}" for i in range(len(names))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _assert_drawable(layout: dict, names: set[str]) -> None:
    nodes = layout["nodes"]
    assert {node["name"] for node in nodes} == names
    w, h, nw, nh = layout["grid"]["w"], layout["grid"]["h"], layout["node"]["w"], layout["node"]["h"]
    for node in nodes:
        assert 0 <= node["x"] and node["x"] + nw <= w and 0 <= node["y"] and node["y"] + nh <= h, node
        if node["stop"]:
            assert node["y"] > layout["rail"]["y"], node
    spots = [(node["x"], node["y"]) for node in nodes]
    assert len(set(spots)) == len(spots)
    for key in layout["edges"]:
        for x, y in layout["routes"][key]:
            assert 0 <= x < w and 0 <= y < h, (key, x, y)


@pytest.mark.parametrize("working,stops", [(1, 7), (2, 13), (30, 9)])
def test_the_flowmap_places_every_state_of_a_chart_that_overflows_its_rail(
    tmp_path: Path, working: int, stops: int,
) -> None:
    chart_path = _wide_chart(tmp_path / "wide.mmd", working, stops)
    snapshot = _snapshot(tmp_path / "db", chart_path=chart_path)
    names = {state["name"] for state in snapshot["statechart"]["states"]}
    assert len(names) == working + stops
    pages = render_pages(snapshot)
    assert sorted(pages) == sorted(PAGES)
    script = dashboard.render_static_assets(snapshot)["app.js"]
    layout = json.loads(re.search(r"var FLOWMAP = (.*);", script).group(1))
    _assert_drawable(layout, names)
    assert layout["grid"]["h"] > 104


def test_the_default_chart_keeps_its_one_row_rail() -> None:
    layout = flow.flowmap_layout(flow.load_chart(CHART))
    assert layout["grid"] == {"w": 250, "h": 104, "cell": 4}
    assert layout["stop_label"]["y"] == 378
    _assert_drawable(layout, {state["name"] for state in flow.load_chart(CHART)["states"]})


def test_job_path_follows_its_history_through_the_chart() -> None:
    chart = flow.load_chart(CHART)
    assert flow.chart_path(
        chart, ["intake", "building", "review", "fixing", "review"]
    ) == ["intake", "building", "review", "fixing", "review"]
    # Repeats collapse; unknown states are skipped.
    assert flow.chart_path(chart, ["intake", "questions", "questions", "other", "planning"]) == [
        "intake", "questions", "planning",
    ]
    # No short bridge: the jump is kept but has no edge.
    assert flow.chart_path(chart, ["intake", "merged"]) == ["intake", "merged"]
    assert flow.path_edges(chart, ["intake", "merged"]) == []
    assert flow.path_edges(chart, ["intake", "questions", "building"]) == [
        ("intake", "questions"),
        ("questions", "building"),
    ]


def test_running_job_carries_its_flow(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "40")
    assert job["round"] == "2/3"
    assert job["tracker"] == "github"
    keys = ("state", "who", "started_at", "duration_seconds", "time_known", "off_chart",
            "reason", "current", "jobs")
    unknown = dict.fromkeys(
        ("round", "model", "effort", "harness", "cli_version", "brief_sha256", "skill_paths", "host",
         "factory_revision"),
        "unknown")

    def ran(job: str, action: str, recipient: str, number: str = "unknown") -> list[dict]:
        return [{"job": job, "action": action, "recipient": recipient, "exposure": {**unknown, "round": number},
                 "suite_wait_seconds": None}]
    assert job["flow"] == {
        "state": "review",
        "path": ["intake", "building", "review", "fixing", "review"],
        "state_age_seconds": 2000,
        # #78: the chart's direct successors of review, and review is not finished.
        "next": ["blocked", "ship", "fixing", "waiting-max", "stuck", "capped"],
        "terminal": False,
        "visits": [dict(zip(keys, row)) for row in (
            ("intake", "script", None, None, False, False, None, False, []),
            ("building", "claude", NOW - 5000, 1000, True, True, flow.OFF_CHART, False,
             ran("h-build", "build", "claude")),
            ("review", "codex", NOW - 4000, 1000, True, False, "ready, CI green", False,
             ran("h-review", "review", "codex", "1")),
            ("fixing", "claude", NOW - 3000, 1000, True, False,
             "APPROVE on red CI, CI fix or REVISE, under the round cap"
             " or trusted kind=rebase, rebase-only fix", False,
             ran("h-fix", "fix", "claude")),
            ("review", "codex", NOW - 2000, 2000, True, False, "ready, CI green", True,
             ran("h-review", "review", "codex", "2")),
        )],
        "hops_to_shipped": 2,
        "all_durations_known": False,
    }


def test_ticket_page_shows_the_path_and_current_state(tmp_path: Path) -> None:
    article = _ticket_page(_snapshot(tmp_path), "h-review")
    runlog = re.search(r'<ol class="runlog">(.*?)</ol>', article, re.S).group(1)
    assert re.findall(r'data-state="([^"]+)"', runlog) == [
        "intake", "building", "review", "fixing", "review"]
    assert re.findall(r'class="[^"]*\bnow\b[^"]*"[^>]*data-state="([^"]+)"', runlog) == ["review"]
    text = _text(article)
    assert "round 2 of 3" in text
    assert "33m 20s in review" in text
    assert "intake → building → review → fixing → review" in article
    assert "codex" in text and "cleffy#40" in text


def test_every_running_and_queued_job_opens_on_its_ticket_page(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    html = _no_script(render_pages(snapshot)["job.html"])
    for row in snapshot["running"] + snapshot["queued"]:
        article = _ticket_page(snapshot, row["job"])
        assert f'<article class="jobpage ticketpage" data-ticket="{ticket_address(row)}">' in article, row["job"]
        assert 'id="liverun-job-' in article, row["job"]
    board = _section(html, "pipeline") + _section(html, "shipped")
    for row in snapshot["tickets"]:
        assert f'href="{ticket_page_href(row)}"' in board, ticket_address(row)
    for job in ("h-review", "r-codex-"):
        assert f'href="{ticket_page_href(_job_row(snapshot, job))}"' in board, job
    assert 'href="job.html#job-' not in board


def test_a_job_with_no_ticket_state_still_gets_a_run_log(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_job(connection, "orphan-build", state="queued", ticket_id="99")
    snapshot = collect_snapshot(db, log, now=NOW)
    job = next(row for row in snapshot["queued"] if row["ticket_id"] == "99")
    assert job["round"] is None
    assert job["flow"]["state"] is None
    assert job["flow"]["path"] == ["intake", "building"]
    article = _ticket_page(snapshot, "orphan-b")
    (runlog,) = re.findall(r'<ol class="runlog">.*?</ol>', article, re.S)
    assert not re.search(r'class="[^"]*\bnow\b', runlog)


# --- no-JS rendering ---------------------------------------------------------------------


def test_every_page_reads_without_script(tmp_path: Path) -> None:
    pages = _pages(tmp_path)
    for name, html in pages.items():
        assert '<noscript><meta http-equiv="refresh" content="20"></noscript>' in html, name
        bare = _no_script(html)
        main = _section(bare, "hud")
        assert not re.match(r"<main[^>]*\bhidden\b", main), name
        # Every panel is filled server-side, not left for the script.
        for panel in re.findall(r'<section class="panel[^"]*" id="([a-z-]+)"', main):
            section = _section(bare, panel)
            if re.fullmatch(r"<section [^>]*\bhidden></section>", section):
                continue
            assert re.search(r"<h2[ >]", section), (name, panel)
    lanes = _text(_section(_no_script(pages["index.html"]), "lanes"))
    assert "cleffy#13" in lanes and "cleffy#14" in lanes


def test_pages_have_no_external_assets_or_markup_sinks(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path)
    script = dashboard.render_static_assets(snap)["app.js"]
    for name, html in render_pages(snap).items():
        # #563: the only https:// text is a link to github.com, never an asset.
        assert set(re.findall(r"https://[^/'\"]*/", html)) <= {"https://github.com/"}, name
        assert '<link rel="stylesheet" href="/app.css">' in html, name
        assert '<script src="/app.js" defer></script>' in html, name
        assert "<script>" not in html and "<style>" not in html, name
        for needle in ("http://", "@import", "url(", "//cdn"):
            assert needle not in html, (name, needle)
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert sink not in script, (name, sink)
        assert re.findall(r"fetch\(([^,)]+)", script) == [
            "'evals.json'", "path", "'/api/session'", "'tickets.json'", "STATUS_URL"], name
        assert all(path.startswith("/api/") for path in re.findall(r"ghPost\('([^']*)'", script)), name


def test_no_secret_reaches_any_page(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_job(connection, "secret-run", state="running", repo=f"repo {WEBHOOK_URL}",
                ticket_id=GH_CLASSIC)
        add_ticket(connection, f"repo {WEBHOOK_URL}", GH_CLASSIC, state="review",
                   stuck=1, stuck_reason=f"token={GH_CLASSIC} {WEBHOOK_URL}")
    out = tmp_path / "out"
    write_snapshot(out, collect_snapshot(db, log, now=NOW))
    text = "".join(path.read_text(encoding="utf-8") for path in out.iterdir())
    for name, secret in FREE_TEXT_SECRETS.items():
        assert secret not in text, name
    assert str(ROOT) not in text  # no local paths either



def test_no_device_checkout_path_reaches_any_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, log = seeded(tmp_path)
    box = dashboard.component_health.DeviceSync("box", "", "/srv/factory", "a" * 40, "a" * 40,
                                                "synced", "", None, False)
    monkeypatch.setattr(dashboard.component_health, "box_sync", lambda device, root: box)
    snapshot = collect_snapshot(db, log, now=NOW)
    assert [device["device"] for device in snapshot["health"]["devices"]] == ["box"]
    out = tmp_path / "out"
    write_snapshot(out, snapshot)
    text = "".join(path.read_text(encoding="utf-8") for path in out.iterdir())
    assert "/srv/factory" not in text

# --- the page script, against a tiny DOM -------------------------------------------------

HARNESS = r"""
const fs = require('fs');
const [script, statusText, page, hash, idsText, actionsText, ticketsText, evalsText] = [2, 3, 4, 5, 6, 7, 8, 9].map(i => {
  const v = process.argv[i]; return i <= 3 || i >= 7 ? fs.readFileSync(v, 'utf8') : v; });
const status = JSON.parse(statusText);
const ids = {};
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this._text = ''; this.attrs = {};
    this.className = ''; this.hidden = false; this.title = ''; this.style = {setProperty: (k, v) => { this.style[k] = v; }};
    this.listeners = {}; this.parentNode = null;
    const self = this;
    this.classList = {add: (...c) => { self.className = (self.className + ' ' + c.join(' ')).trim(); },
      remove: (...c) => { self.className = self.className.split(' ').filter(x => !c.includes(x)).join(' '); },
      toggle: (c, on) => { if (on === undefined) on = !self.className.split(' ').includes(c);
        if (on) self.classList.add(c); else self.classList.remove(c); return on; },
      contains: (c) => self.className.split(' ').includes(c)}; }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); child.parentNode = null; return child; }
  replaceChildren(...kids) { this.children.forEach(c => { c.parentNode = null; });
    this.children = kids; kids.forEach(c => { c.parentNode = this; }); this._text = ''; }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === body; }
  focus() { global.document.activeElement = this; }
  scrollIntoView() {}
  setAttribute(k, v) { if (k === 'class') { this.className = String(v); return; }
    this.attrs[k] = String(v); if (k === 'id') ids[v] = this; }
  getAttribute(k) { if (k === 'class') return this.className; return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  get attributes() { return Object.keys(this.attrs).map(name => ({name, value: this.attrs[name]})); }
  closest(tag) { let n = this; while (n && n.tagName !== tag) n = n.parentNode; return n || null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  cloneNode(deep) { const n = new Node(this.tagName); n.className = this.className;
    n.attrs = Object.assign({}, this.attrs); n._text = this._text;
    if (deep) this.children.forEach(c => n.appendChild(c.cloneNode(true))); return n; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  html() { const attrs = (this.className ? ' class="' + esc(this.className) + '"' : '') +
      Object.keys(this.attrs).map(k => ' ' + k + '="' + esc(this.attrs[k]) + '"').join('') +
      (this.title ? ' title="' + esc(this.title) + '"' : '') + (this.hidden ? ' hidden' : '');
    return '<' + this.tagName + attrs + '>' + esc(this._text) + this.children.map(c => c.html()).join('') +
      '</' + this.tagName + '>'; }
  dump(depth) { const pad = ' '.repeat(depth || 0);
    const attrs = ['href', 'data-device', 'data-state', 'data-from', 'data-to', 'data-step', 'role']
      .filter(k => k in this.attrs).map(k => '[' + k + '=' + this.attrs[k] + ']').join('');
    return pad + this.tagName + (this.className ? '.' + this.className.split(' ').join('.') : '') + attrs +
      (this._text ? ' "' + this._text + '"' : '') + '\n' + this.children.map(c => c.dump((depth || 0) + 1)).join(''); }
}
class Text extends Node {
  constructor(text) { super('#text'); this._text = String(text); }
  html() { return esc(this._text); }
  dump(depth) { return ' '.repeat(depth || 0) + '"' + this._text + '"\n'; }
}
function esc(v) { return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#x27;'); }
const body = new Node('body');
body.attrs['data-generated'] = '1800000000.000';
body.attrs['data-refresh'] = '20';
body.attrs['data-page'] = page;
for (const id of JSON.parse(idsText)) {
  const n = new Node('div'); n.attrs.id = id; ids[id] = n; body.appendChild(n);
}
if (ids['flow-template'] && status.statechart) {
  // What job.html's server-rendered template holds: one g per state and edge.
  const svg = new Node('svg'); svg.className = 'flow';
  status.statechart.edges.forEach(e => { const g = new Node('g'); g.className = 'edge';
    g.attrs['data-from'] = e.from; g.attrs['data-to'] = e.to; g.appendChild(new Node('path')); svg.appendChild(g); });
  status.statechart.states.forEach(s => { const g = new Node('g'); g.className = 'node';
    g.attrs['data-state'] = s.name; g.appendChild(new Node('rect')); svg.appendChild(g); });
  ids['flow-template'].appendChild(svg);
}
global.window = global;
global.addEventListener = () => {};
const docListeners = {};
let focusedNode = null;
global.document = {body, hidden: false,
  get activeElement() { return focusedNode && focusedNode.isConnected ? focusedNode : null; },
  set activeElement(node) { focusedNode = node; },
  getElementById: (id) => ids[id] || null,
  createElement: (tag) => new Node(tag),
  createElementNS: (ns, tag) => { const n = new Node(tag); n.ns = ns; return n; },
  createTextNode: (text) => new Text(text),
  addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); }};
// Scripted user actions, run after the first render: click a node (by href,
// id or class), press a key, advance the clock, or record what the body holds.
function find(node, pred) {
  if (pred(node)) return node;
  for (const c of node.children) { const f = find(c, pred); if (f) return f; }
  return null;
}
function fire(target, type, extra) {
  const ev = Object.assign({type, target, button: 0, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}}, extra || {});
  for (let n = target; n; n = n.parentNode) (n.listeners[type] || []).forEach(fn => fn.call(n, ev));
  (docListeners[type] || []).forEach(fn => fn(ev));
  return ev;
}
function pick(spec) {
  return find(body, n => (spec.href === undefined || n.attrs.href === spec.href) &&
    (spec.id === undefined || n.attrs.id === spec.id) &&
    (spec.cls === undefined || n.className.split(' ').includes(spec.cls)) &&
    (spec.attr === undefined || Object.entries(spec.attr).every(([k, v]) => n.attrs[k] === v)));
}
const steps = [];
function act(actions) {
  for (const [kind, arg] of actions) {
    if (kind === 'click') {
      const node = pick(arg);
      if (!node) throw new Error('nothing to click: ' + JSON.stringify(arg));
      steps.push({click: arg, prevented: fire(node, 'click').defaultPrevented});
    } else if (kind === 'key') {
      steps.push({key: arg, prevented: fire(body, 'keydown', {key: arg}).defaultPrevented});
    } else if (kind === 'advance') {
      // Move the clock on by `arg` seconds and run the page's interval timers.
      nowMs += arg * 1000;
      intervals.forEach(fn => fn());
      steps.push({advance: arg});
    } else if (kind === 'record') {
      const modal = find(body, n => n.attrs.id === 'job-modal');
      steps.push({record: arg, kids: body.children.map(c => c.attrs.id || c.tagName),
        modal: modal ? modal.dump(0) : null, text: modal ? modal.textContent : null,
        focus: document.activeElement ? (document.activeElement.attrs.href || document.activeElement.className) : null});
    }
  }
}
const replaced = [];
global.location = {hash, reload: () => { console.log('RELOAD'); }, replace: (url) => { replaced.push(url); }};
let fetches = 0;
let ticketFetches = 0;
let evalsFetches = 0;
const evalsAnswer = JSON.parse(evalsText);
const heldTickets = [];
const holdTickets = JSON.parse(actionsText).some(([kind]) => kind === 'hold');
global.fetch = (url, opts) => {
  if (url === 'tickets.json') {
    if (opts.cache !== 'no-store' || opts.credentials !== 'same-origin') throw new Error('bad fetch ' + url);
    ticketFetches += 1;
    const res = {ok: true, status: 200, json: () => Promise.resolve(JSON.parse(ticketsText))};
    return holdTickets ? new Promise(resolve => heldTickets.push(() => resolve(res))) : Promise.resolve(res);
  }
  if (url === 'evals.json') {
    if (opts.cache !== 'no-store' || opts.credentials !== 'same-origin') throw new Error('bad fetch ' + url);
    evalsFetches += 1;
    const answer = evalsAnswer || {status: 404, body: null};
    return Promise.resolve({ok: answer.status === 200, status: answer.status,
      json: () => Promise.resolve(answer.body)});
  }
  fetches += 1;
  if (url !== 'status.json' || opts.cache !== 'no-store') throw new Error('bad fetch ' + url);
  return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(status)}); };
let nowMs = 1800000003000;
Date.now = () => nowMs;
const intervals = [];
global.setInterval = (fn) => { intervals.push(fn); return intervals.length; };
eval(script);
setTimeout(() => {
  act(JSON.parse(actionsText));
  process.stdout.write(JSON.stringify({fetches, steps, tickets: ticketFetches, evals: evalsFetches, replaced,
    text: Object.fromEntries(Object.entries(ids).map(([k, v]) => [k, v.textContent])),
    html: Object.fromEntries(Object.entries(ids).map(([k, v]) => [k, v.children.map(c => c.html()).join('')])),
    hidden: Object.fromEntries(Object.entries(ids).map(([k, v]) => [k, !!v.hidden])),
    dump: Object.fromEntries(Object.entries(ids).map(([k, v]) => [k, v.dump(0)]))}), () => process.exit(0));
}, 200);
"""

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run_page(
    tmp_path: Path, snapshot: dict, page: str, ids: list[str], hash_: str = "",
    actions: list | None = None, evals: dict | None = None,
) -> dict:
    name = {"main": "index.html"}.get(page, f"{page}.html")
    html = render_pages(snapshot)[name]
    assert '<script src="/app.js" defer></script>' in html
    script = dashboard.render_static_assets(snapshot)["app.js"]
    assert f'data-page="{page}"' in html
    out = run_harness(tmp_path, HARNESS, script, snapshot, page, ids, actions or [], hash_, tickets_json(snapshot),
                      evals)
    assert out["fetches"] >= 1
    return out


def run_harness(
    tmp_path: Path, harness: str, script: str, snapshot: dict, page: str, ids: list[str], actions: list | dict,
    hash_: str = "", tickets: str = "null", evals: dict | None = None,
) -> dict:
    """Linux caps one argument at 128 KiB, so actions, tickets.json and evals.json go by file like the script and snapshot."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    files = {"harness.js": harness, "page.js": script, "status.json": json.dumps(snapshot),
             "actions.json": json.dumps(actions), "tickets.json": tickets, "evals.json": json.dumps(evals)}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(tmp_path / "page.js"), str(tmp_path / "status.json"),
         page, hash_, json.dumps(ids), str(tmp_path / "actions.json"), str(tmp_path / "tickets.json"),
         str(tmp_path / "evals.json")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_the_harness_takes_an_action_list_past_the_linux_argument_cap(tmp_path: Path) -> None:
    padding = "x" * 200_000
    out = _run_page(tmp_path, _snapshot(tmp_path), "main", ["link"], actions=[["pad", padding]])
    assert len(json.dumps([["pad", padding]]).encode()) > 131072
    assert out["steps"] == []


# The shell on every page (#139): the TopBar's link state and caret, the band,
# the attention strip and the status line.
BAR = ["link", "link-text", "age", "clock", "caret", "band", "attention-strip", "statusline"]
JOB = BAR + ["hud", "ticket-filter", "pipeline", "shipped", "tickets", "flow-template"]


def attention_count(out: dict) -> str:
    """The attention strip's count as the script drew it; "0" only when the
    strip is hidden and empty, as it is when nothing needs attention."""
    if out["hidden"]["attention-strip"]:
        assert out["html"]["attention-strip"] == "", out["html"]["attention-strip"]
        return "0"
    return out["text"]["attention-count"]


@needs_node
def test_script_renders_the_main_page_live(tmp_path: Path) -> None:
    snapshot = _held_more_than_running(tmp_path)
    panels = ["needs-max", "intake", "lanes", "just-happened"]
    out = _run_page(tmp_path, snapshot, "main", BAR + ["hud"] + panels, actions=[["record", "after"]])
    assert "LIVE" in out["text"]["link-text"]
    for panel in panels:
        assert out["dump"][panel].count("\n h2") == 1, (panel, out["dump"][panel])
    kids = out["steps"][0]["kids"]
    assert [kids.index(panel) for panel in panels] == sorted(kids.index(panel) for panel in panels), kids
    assert attention_count(out) == "3"
    lanes = out["dump"]["lanes"]
    assert f"a.tile.codex[href={ticket_page_href(_job_row(snapshot, 'r-codex-'))}]" in lanes
    for needle in ("cleffy#13", "codex", "review", "3/3", "T+ ", "overdue", "DISABLED",
                   "stale"):
        assert needle in out["text"]["lanes"], (needle, lanes)
    # #63 FU-1: pc holds 2 slots with 1 job running; the lane is full.
    pc = lanes[lanes.index("[data-device=pc]") :]
    pc = pc[: pc.index("[data-device=", 1)] if "[data-device=" in pc[1:] else pc
    assert '"2 / 2 slots"' in pc and '"held"' in pc and '"idle"' not in pc
    queue = lanes[lanes.index("[data-device=queue]") : lanes.index("[data-device=", lanes.index("[data-device=queue]") + 1)]
    assert f"a[href={ticket_page_href(_job_row(snapshot, 'q-claude'))}]" in queue
    assert "cleffy#12" in queue


@needs_node
def test_script_renders_the_activity_and_efficiency_pages(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, last_cycle_at=NOW - 4)
    activity = _run_page(tmp_path, snapshot, "activity", BAR + ["hud", "attention", "relay"])
    text = activity["text"]
    assert "AUT-7" in text["attention"] and "tracy-harris-coaching#5" in text["attention"]
    assert text["attention"].count("AUT-7") == 1
    system = _run_page(tmp_path, snapshot, "health", BAR + ["hud", "health", "log"])
    assert "cycle line 79" in system["text"]["log"]
    assert "OK" in system["text"]["health"]
    efficiency = _run_page(tmp_path, snapshot, "efficiency", BAR + ["hud", "savings"])
    text = efficiency["text"]
    assert dashboard.SCRIPTED_SHARE_DEFINITION in text["savings"]
    assert "50%" in text["savings"]


@needs_node
def test_script_shows_the_selected_jobs_path(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    out = _run_page(tmp_path / "job", snapshot, "job", JOB, hash_="#job-h-review")
    assert out["replaced"] == [ticket_page_href(_job_row(snapshot, "h-review"))]
    assert f"a[href={ticket_page_href(_job_row(snapshot, 'r-codex-'))}]" in out["dump"]["pipeline"]
    assert "AUT-7" in out["text"]["tickets"]
    page = _run_page(tmp_path / "ticket", snapshot, "ticket", BAR + ["hud", "ticketview"],
                     hash_=out["replaced"][0].removeprefix("ticket.html"))
    view = page["dump"]["ticketview"]
    assert "cleffy#40" in page["text"]["ticketview"]
    assert "round 2 of 3" in page["text"]["ticketview"]
    assert "33m 2" in page["text"]["ticketview"]  # time in state, ticking from 33m 20s
    runlog = view[view.index("ol.runlog"):view.index("section.jd-panel.jd-exp")]
    assert re.findall(r"button[.a-z]*\[data-state=([^\]]+)\]", runlog) == [
        "intake", "building", "review", "fixing", "review"]
    assert re.findall(r"button\.visit\.now\[data-state=([^\]]+)\]", runlog) == ["review"]
    assert "cleffy#13" not in page["text"]["ticketview"]


@needs_node
def test_script_stays_on_the_board_when_the_selected_job_is_gone(tmp_path: Path) -> None:
    out = _run_page(tmp_path, _snapshot(tmp_path), "job", JOB, hash_="#job-finished")
    assert out["replaced"] == []
    assert "AUT-7" in out["text"]["tickets"]


# --- the full push carries every page ------------------------------------------------------


def test_cli_full_push_publishes_every_page_in_one_rename(tmp_path: Path) -> None:
    import os
    import sys

    from tests.test_dashboard_live import _fake_bin

    db, log = seeded(tmp_path)
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 60\nprojects:\n  - repo: maxharris1/factory\n"
        "dashboard:\n  push:\n    transport: ssh\n    host: pc-wsl-cursor-bot\n"
        "    remote_dir: factory-dashboard\n    method: scp\n    live: true\n",
        encoding="utf-8",
    )
    remote_home = tmp_path / "remote-home"
    site = remote_home / "factory-dashboard"
    site.mkdir(parents=True)
    remote_log = tmp_path / "remote.log"
    env = {key: value for key, value in os.environ.items() if not key.startswith("ORCH_")}
    env.update(
        PATH=f"{_fake_bin(tmp_path)}{os.pathsep}{env.get('PATH', '')}",
        FAKE_REMOTE_HOME=str(remote_home),
        FAKE_REMOTE_LOG=str(remote_log),
    )
    out = tmp_path / "local-site"
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "dashboard", "push", "--once",
         "--db", str(db), "--orch", str(orch), "--out", str(out), "--log", str(log)],
        capture_output=True, text=True, env=env, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    calls = [eval(line) for line in remote_log.read_text().splitlines()]  # noqa: S307
    names = ("status.json", *dashboard.STATIC_ASSETS, *PAGES, "tickets.json", "evals.json")
    assert [call[0] for call in calls] == ["scp"] * len(names) + ["ssh"]
    assert calls[-1][-1] == "cd -- factory-dashboard && " + " && ".join(
        f"mv -f -- .{name}.push-tmp {name}" for name in names
    )
    assert sorted(path.name for path in site.iterdir()) == sorted(names)
    for name in PAGES:
        assert (site / name).read_text(encoding="utf-8") == (out / name).read_text(encoding="utf-8")


def test_jobs_on_a_database_without_ticket_tables_still_render(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE tracked_tickets")
        connection.execute("DROP TABLE ticket_state")
    snapshot = collect_snapshot(db, log, now=NOW)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "13")
    # The round is the job's own (detail.round), so it survives (#75).
    assert (job["tracker"], job["round"], job["flow"]["state"]) == (None, "3/3", None)
    # Only a review job: intake -> review is three chart steps, more than a
    # bridge, so it stays a jump.
    assert job["flow"]["path"] == ["intake", "review"]
    pages = render_pages(snapshot)
    assert 'id="liverun-job-r-codex-"' in _ticket_page(snapshot, "r-codex-")
    assert "3/3" in _text(_section(_no_script(pages["index.html"]), "lanes"))
