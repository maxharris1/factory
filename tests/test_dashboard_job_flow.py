"""Issue #78: nav order and a job modal on the lanes page. #146 moved the
job view to the ticket detail page; tests/test_dashboard_ticket_detail.py
covers its panels.

- Every page's nav reads Lanes, Tickets, Activity, Efficiency, System.
- On the lanes page (index.html), clicking a job in a lane slot or the queue
  rail opens a modal with that job's ticket page; Esc, the close button and a
  backdrop click close it. Without script the link goes to
  job.html#job-<id>.
- The Run log lists the path the ticket took, oldest first (a revisited
  state appears again, in sequence), and Can come next lists the chart's
  direct successors of the current state. Everything comes from
  model/chart.mmd.
"""

import re
import sqlite3
from pathlib import Path

import lib.dashboard_flow as flow
import lib.dashboard_page as dashboard_page
from lib.dashboard import collect_snapshot, render_pages
from tests.test_dashboard import NOW, add_job, add_ticket
from tests.test_dashboard_layout import (
    BAR,
    JOB,
    CHART,
    MINI,
    _chart_source,
    _history,
    _no_script,
    _run_page,
    _section,
    _snapshot,
    _text,
    needs_node,
    seeded,
)

NAV = [
    ("index.html", "Lanes"),
    ("job.html", "Tickets"),  # #139
    ("activity.html", "Activity"),
    ("efficiency.html", "Efficiency"),
    ("health.html", "System"),  # #92, #139
]
# Ticket 40 (tests/test_dashboard_layout.py _history): built, reviewed,
# revised once, now in its second review.
TICKET_40_PATH = ["intake", "questions", "building", "review", "fixing", "review"]


def _successors(path: Path, state: str) -> set[str]:
    """The chart's direct successors of `state`, read straight from the file."""
    _, edges = _chart_source(path)
    return {b for a, b in edges if a == state}


def _visits(markup: str, cls: str) -> list[str]:
    """The data-state of each visit button in the `<ol class="{cls}">` list."""
    match = re.search(rf'<ol class="{cls}">(.*?)</ol>', markup, flags=re.S)
    assert match, f"no {cls} list"
    return re.findall(r'<button[^>]*data-state="([^"]+)"', match.group(1))


def _next_cards(markup: str) -> list[str]:
    """The target state of each Can come next card, in order."""
    return re.findall(r'<div class="card [a-z]+"><div class="head"><span class="to">([^<]+)</span>', markup)


def _with_finished_tickets(tmp_path: Path) -> dict:
    """Ticket 40's history plus a done ticket and a merged ticket, each with a
    queued job."""
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _history(connection)
        add_ticket(connection, "maxharris1/cleffy", "50", state="done")
        add_job(connection, "t-done-1", state="queued", ticket_id="50")
        add_ticket(connection, "maxharris1/cleffy", "51", state="merged")
        add_job(connection, "t-merg-1", state="queued", ticket_id="51")
    return collect_snapshot(db, log, now=NOW)


# --- 1. nav order -------------------------------------------------------------------


def test_nav_order_is_lanes_jobs_activity_efficiency_on_every_page(tmp_path: Path) -> None:
    pages = render_pages(_snapshot(tmp_path))
    assert set(pages) == {name for name, _ in NAV}
    assert list(dashboard_page.PAGES) == [name for name, _ in NAV]
    for name, html in pages.items():
        nav = _section(html, "nav")
        links = re.findall(r'<a href="([^"]+)"( aria-current="page")?>([^<]+)</a>', nav)
        assert [(href, label) for href, _, label in links] == NAV, name
        assert [href for href, current, _ in links if current] == [name], name


# --- 3. the path and the next steps on the job view ------------------------------------


def test_run_log_is_the_chronological_path_with_the_revisit_repeated(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "40")
    assert job["flow"]["path"] == TICKET_40_PATH
    article = _section(_no_script(render_pages(snapshot)["job.html"]), "job-h-review")
    assert _visits(article, "runlog") == TICKET_40_PATH
    assert re.findall(r'<button[^>]*data-visit="(\d+)"[^>]*class="now', article) == ["5"]


def test_next_steps_are_the_charts_direct_successors_of_the_current_state(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "40")
    expected = _successors(CHART, "review")
    assert expected == {"blocked", "ship", "stuck", "fixing", "capped"}
    assert set(job["flow"]["next"]) == expected
    article = _section(_no_script(render_pages(snapshot)["job.html"]), "job-h-review")
    assert set(_next_cards(article)) == expected


def test_successors_and_finished_states_come_from_the_chart() -> None:
    for path in (CHART, MINI):
        chart = flow.load_chart(path)
        states, _ = _chart_source(path)
        for state in states:
            assert set(flow.successors(chart, state)) == _successors(path, state), (path.name, state)
    chart = flow.load_chart(CHART)
    # Finished: the chart has no way back to work from there.
    for state in ("done", "merged", "post-merge", "abandoned", "refused"):
        assert flow.is_terminal(chart, state), state
    # #108: a trusted ready after handoff re-enters review.
    for state in (
        "intake", "building", "review", "fixing", "ship", "waiting-external", "capped",
        "handed-off",
    ):
        assert not flow.is_terminal(chart, state), state
    assert not flow.is_terminal(chart, None)
    mini = flow.load_chart(MINI)
    assert flow.is_terminal(mini, "handed-off") and not flow.is_terminal(mini, "review")
    assert flow.successors(mini, "review") == ["fixing", "handed-off"]


def test_a_chart_change_changes_the_next_steps(tmp_path: Path) -> None:
    changed = tmp_path / "changed.mmd"
    text = MINI.read_text(encoding="utf-8")
    text = text.replace("  s4: handed-off\n", "  s4: handed-off\n  s5: waiting-max\n")
    text = text.replace("  s2 --> s4: ship = handoff\n", "  s2 --> s5: a product question\n")
    changed.write_text(text, encoding="utf-8")
    snapshot = _snapshot(tmp_path, chart_path=changed)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "40")
    assert set(job["flow"]["next"]) == _successors(changed, "review") == {"fixing", "waiting-max"}
    article = _section(_no_script(render_pages(snapshot)["job.html"]), "job-h-review")
    assert set(_next_cards(article)) == {"fixing", "waiting-max"}


def test_a_finished_ticket_has_no_way_out(tmp_path: Path) -> None:
    snapshot = _with_finished_tickets(tmp_path)
    jobs = {row["ticket_id"]: row for row in snapshot["running"] + snapshot["queued"]}
    assert (jobs["50"]["flow"]["state"], jobs["50"]["flow"]["terminal"]) == ("done", True)
    assert (jobs["40"]["flow"]["state"], jobs["40"]["flow"]["terminal"]) == ("review", False)
    html = _no_script(render_pages(snapshot)["job.html"])
    done = _section(html, "job-t-done-1")
    assert _next_cards(done) == [] and "no way out: this ticket is finished" in done
    assert _next_cards(_section(html, "job-h-review"))


def test_the_job_page_draws_no_ring_or_breathing(tmp_path: Path) -> None:
    html = render_pages(_snapshot(tmp_path))["job.html"]
    assert "breathe" not in html
    assert not re.search(r"\bring\b", _section(_no_script(html), "jobview"))


def test_hero_keeps_the_round_and_time_in_state(tmp_path: Path) -> None:
    article = _section(_no_script(render_pages(_snapshot(tmp_path))["job.html"]), "job-h-review")
    line = re.search(r'<p class="line">(.*?)</p>', article).group(1)
    assert _text(line) == "now review · round 2 of 3 · 33m 20s in review"


def test_full_chart_is_behind_a_collapsed_toggle(tmp_path: Path) -> None:
    article = _section(_no_script(render_pages(_snapshot(tmp_path))["job.html"]), "job-h-review")
    toggle = re.search(r'<details class="jd-chart"[^>]*>(.*?)</details>', article, re.S)
    assert toggle, "no full chart toggle"
    assert not re.search(r'<details class="jd-chart"[^>]*\bopen\b', article)
    assert re.search(r"<summary>Full state chart ", toggle.group(1))
    assert '<canvas class="jd-flowmap" width="1000" height="352"' in toggle.group(1)


def test_lanes_page_links_jobs_to_the_job_page_without_script(tmp_path: Path) -> None:
    html = _no_script(render_pages(_snapshot(tmp_path))["index.html"])
    assert 'href="job.html#job-h-review"' in _section(html, "lanes")
    assert 'href="job.html#job-q-claude"' in _section(html, "queue")
    assert 'id="job-modal"' not in html
    assert "flow-template" not in html


# --- 2. the job modal, against the page script ------------------------------------------

MAIN_IDS = BAR + ["hud", "kpis", "lanes", "queue"]


@needs_node
def test_clicking_a_lane_slot_opens_the_job_modal_and_it_closes_cleanly(tmp_path: Path) -> None:
    slot = {"href": "job.html#job-h-review"}
    out = _run_page(tmp_path, _snapshot(tmp_path), "main", MAIN_IDS, actions=[
        ["click", slot], ["record", "open"],
        ["click", {"cls": "modal-box"}], ["record", "inside"],
        ["key", "Escape"], ["record", "esc"],
        ["click", slot], ["click", {"cls": "modal-close"}], ["record", "button"],
        ["click", slot], ["click", {"id": "job-modal"}], ["record", "backdrop"],
    ])
    steps = out["steps"]
    assert steps[0] == {"click": slot, "prevented": True}
    opened = steps[1]
    assert "job-modal" in opened["kids"]
    modal = opened["modal"]
    assert "[role=dialog]" in modal
    assert "cleffy#40" in opened["text"] and "round 2 of 3" in opened["text"]
    # The same ticket page as job.html.
    runlog = modal[modal.index("ol.runlog"):modal.index("ol.journey")]
    assert re.findall(r"button[.a-z]*\[data-state=([^\]]+)\]", runlog) == TICKET_40_PATH
    assert "Full state chart" in opened["text"]
    assert "button.modal-close" in modal
    # A click inside the box leaves it open; Esc, the button and the backdrop close it.
    assert "job-modal" in steps[3]["kids"]
    assert steps[4]["key"] == "Escape"
    for label in ("esc", "button", "backdrop"):
        record = next(step for step in steps if step.get("record") == label)
        assert "job-modal" not in record["kids"], label
        assert record["modal"] is None, label
    # Focus goes back to the job that was clicked.
    assert next(step for step in steps if step.get("record") == "esc")["focus"] == slot["href"]


@needs_node
def test_clicking_a_queued_job_opens_its_modal(tmp_path: Path) -> None:
    item = {"href": "job.html#job-q-claude"}
    out = _run_page(tmp_path, _snapshot(tmp_path), "main", MAIN_IDS,
                    actions=[["click", item], ["record", "open"], ["key", "Escape"], ["record", "closed"]])
    assert out["steps"][0]["prevented"] is True
    opened = out["steps"][1]
    assert "job-modal" in opened["kids"] and "cleffy#12" in opened["text"]
    assert "ol.runlog" in opened["modal"]
    assert "job-modal" not in out["steps"][3]["kids"]


# --- 3, live: the job page script draws the same path and next steps -------------------------


@needs_node
def test_script_draws_the_same_path_and_next_steps(tmp_path: Path) -> None:
    out = _run_page(tmp_path, _snapshot(tmp_path), "job", JOB, hash_="#job-h-review")
    view = out["html"]["jobview"]
    assert _visits(view, "runlog") == TICKET_40_PATH
    assert set(_next_cards(view)) == _successors(CHART, "review")
    assert "round 2 of 3" in out["text"]["jobview"]


@needs_node
def test_script_shows_no_way_out_for_a_finished_ticket(tmp_path: Path) -> None:
    out = _run_page(tmp_path, _with_finished_tickets(tmp_path), "job", JOB, hash_="#job-t-done-1")
    view = out["html"]["jobview"]
    assert _next_cards(view) == [] and "no way out: this ticket is finished" in view
    assert "breathe" not in view
