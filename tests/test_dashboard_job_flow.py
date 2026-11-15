"""Issue #78: nav order, a job modal on the lanes page, and a simplified
per-job flow.

- Every page's nav reads Lanes, Jobs, Activity, Efficiency.
- On the lanes page (index.html), clicking a job in a lane slot or the queue
  rail opens a modal with that job's flow; Esc, the close button and a
  backdrop click close it. Without script the link goes to
  job.html#job-<id>.
- The job view draws only the path the ticket took, as a chronological
  left-to-right chain (a revisited state appears again, in sequence), the
  current step (breathing, unless the job is finished), and the chart's
  direct successors of the current state, grayed with dashed arrows. The
  full chart stays behind a collapsed "full chart" toggle. Everything comes
  from model/chart.mmd.
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


def _trail(markup: str) -> dict:
    """The simplified flow svg in `markup`: its visits (in document order),
    next steps and arrows."""
    match = re.search(r'<svg class="flow steps".*?</svg>', markup, flags=re.S)
    assert match, "no simplified flow"
    svg = match.group(0)
    return {
        "svg": svg,
        "visits": [
            (int(step), state, set(classes.split()))
            for classes, state, step in re.findall(
                r'<g class="(visit[^"]*)" data-state="([^"]+)" data-step="(\d+)"', svg
            )
        ],
        "next": {
            state: set(classes.split())
            for classes, state in re.findall(r'<g class="(next[^"]*)" data-state="([^"]+)"', svg)
        },
        "hops": [
            (a, b, step, set(classes.split()))
            for classes, a, b, step in re.findall(
                r'<g class="(hop[^"]*)" data-from="([^"]+)" data-to="([^"]+)"'
                r'(?: data-step="(\d+)")?',
                svg,
            )
        ],
    }


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


# --- 3. the simplified flow on the job view ----------------------------------------------


def test_path_is_a_chronological_chain_with_the_revisit_repeated(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "40")
    assert job["flow"]["path"] == TICKET_40_PATH
    article = _section(_no_script(render_pages(snapshot)["job.html"]), "job-h-review")
    trail = _trail(article)
    # One node per visit, left to right in the order the ticket went: review
    # appears twice, not as a loop back.
    assert [(step, state) for step, state, _ in trail["visits"]] == list(enumerate(TICKET_40_PATH))
    xs = [float(x) for x in re.findall(r'<g class="visit[^"]*"[^>]*><title>[^<]*</title><rect x="([\d.]+)"',
                                       trail["svg"])]
    assert xs == sorted(xs) and len(set(xs)) == len(xs)
    # Arrows run in chronological order, each from one visit to the next.
    path_hops = [(a, b, int(step)) for a, b, step, classes in trail["hops"] if "next" not in classes]
    assert path_hops == [
        (TICKET_40_PATH[i - 1], TICKET_40_PATH[i], i) for i in range(1, len(TICKET_40_PATH))
    ]
    # Only the last visit is the current step, even though review was visited before.
    assert [step for step, _, classes in trail["visits"] if "current" in classes] == [5]
    assert "breathe" in trail["visits"][-1][2]
    assert not any("breathe" in classes for _, _, classes in trail["visits"][:-1])


def test_render_path_svg_repeats_revisits_in_sequence() -> None:
    chart = flow.load_chart(MINI)
    path = ["intake", "building", "review", "fixing", "review", "fixing", "review"]
    trail = _trail(flow.render_path_svg(chart, path, "review"))
    assert [state for _, state, _ in trail["visits"]] == path
    assert [int(step) for _, _, step, classes in trail["hops"] if "next" not in classes] == list(
        range(1, len(path))
    )


def test_next_steps_are_the_charts_direct_successors_of_the_current_state(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "40")
    expected = _successors(CHART, "review")
    assert expected == {"blocked", "ship", "stuck", "fixing", "capped"}
    assert set(job["flow"]["next"]) == expected
    trail = _trail(_section(_no_script(render_pages(snapshot)["job.html"]), "job-h-review"))
    assert set(trail["next"]) == expected
    next_hops = {(a, b) for a, b, _, classes in trail["hops"] if "next" in classes}
    assert next_hops == {("review", state) for state in expected}
    # Nothing else from the chart: only the path and the next steps.
    drawn = {state for _, state, _ in trail["visits"]} | set(trail["next"])
    assert drawn == set(TICKET_40_PATH) | expected
    assert len(re.findall(r"<g class=\"(?:visit|next)", trail["svg"])) == len(TICKET_40_PATH) + len(expected)


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
    trail = _trail(_section(_no_script(render_pages(snapshot)["job.html"]), "job-h-review"))
    assert set(trail["next"]) == {"fixing", "waiting-max"}


def test_finished_jobs_do_not_breathe(tmp_path: Path) -> None:
    snapshot = _with_finished_tickets(tmp_path)
    jobs = {row["ticket_id"]: row for row in snapshot["running"] + snapshot["queued"]}
    assert (jobs["50"]["flow"]["state"], jobs["50"]["flow"]["terminal"]) == ("done", True)
    assert (jobs["51"]["flow"]["state"], jobs["51"]["flow"]["terminal"]) == ("merged", True)
    assert (jobs["40"]["flow"]["state"], jobs["40"]["flow"]["terminal"]) == ("review", False)
    html = _no_script(render_pages(snapshot)["job.html"])
    for ident, state in (("job-t-done-1", "done"), ("job-t-merg-1", "merged")):
        article = _section(html, ident)
        trail = _trail(article)
        current = [classes for _, s, classes in trail["visits"] if "current" in classes]
        assert len(current) == 1 and trail["visits"][-1][1] == state, ident
        assert "breathe" not in current[0], ident
        assert "breathe" not in article, ident
    # merged still shows where the chart goes next; done goes nowhere.
    assert set(_trail(_section(html, "job-t-merg-1"))["next"]) == _successors(CHART, "merged")
    assert _trail(_section(html, "job-t-done-1"))["next"] == {}
    assert "breathe" in _trail(_section(html, "job-h-review"))["visits"][-1][2]


def test_breathing_is_slow_in_the_accent_and_off_under_reduced_motion(tmp_path: Path) -> None:
    html = render_pages(_snapshot(tmp_path))["job.html"]
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    rule = re.search(r"\.breathe[^{]*\{[^}]*animation:breathe ([\d.]+)s[^}]*\}", css)
    assert rule, "no breathing animation"
    assert 2 <= float(rule.group(1)) <= 3
    keyframes = re.search(r"@keyframes breathe\{.*?\}\}", css, re.S)
    assert keyframes and "var(--accent)" in keyframes.group(0)
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)") :]
    assert re.search(r"\.breathe[^{]*\{[^}]*animation:none", reduced)


def test_current_step_keeps_its_round_and_time_in_state(tmp_path: Path) -> None:
    article = _section(_no_script(render_pages(_snapshot(tmp_path))["job.html"]), "job-h-review")
    trail = _trail(article)
    current = re.search(r'<g class="visit[^"]*current[^"]*".*?</g>', trail["svg"], re.S).group(0)
    assert "r2/3" in _text(current) and "33m 20s" in _text(current)
    # The other visits carry no made-up data.
    assert "r1/3" not in trail["svg"]
    # The plain-text summary stays.
    text = _text(article)
    assert "round 2/3" in text and "33m 20s in review" in text


def test_full_chart_is_behind_a_collapsed_toggle(tmp_path: Path) -> None:
    article = _section(_no_script(render_pages(_snapshot(tmp_path))["job.html"]), "job-h-review")
    toggle = re.search(r"<details class=\"fullchart\">(.*?)</details>", article, re.S)
    assert toggle, "no full chart toggle"
    assert "<details class=\"fullchart\" open" not in article
    assert re.search(r"<summary>full chart</summary>", toggle.group(1))
    assert '<svg class="flow"' in toggle.group(1)
    assert '<svg class="flow steps"' not in toggle.group(1)
    assert article.index('<svg class="flow steps"') < article.index("<details")


def test_lanes_page_links_jobs_to_the_job_page_without_script(tmp_path: Path) -> None:
    html = _no_script(render_pages(_snapshot(tmp_path))["index.html"])
    assert 'href="job.html#job-h-review"' in _section(html, "lanes")
    assert 'href="job.html#job-q-claude"' in _section(html, "queue")
    # #140: the Lanes job modal is dropped (STATUS.md decision 1), and with it the chart template.
    for needle in ('id="job-modal"', "modal-box", 'role="dialog"', 'id="flow-template"'):
        assert needle not in html, needle


# --- 3, live: the job page script draws the same simplified flow -----------------------------


@needs_node
def test_script_draws_the_simplified_flow_on_the_job_page(tmp_path: Path) -> None:
    out = _run_page(tmp_path, _snapshot(tmp_path), "job", JOB, hash_="#job-h-review")
    view = out["dump"]["jobview"]
    visits = re.findall(r"g\.visit[.a-z]*\[data-state=([^\]]+)\]\[data-step=(\d+)\]", view)
    assert visits == [(state, str(i)) for i, state in enumerate(TICKET_40_PATH)]
    assert re.findall(r"g\.visit[.a-z]*current[.a-z]*\[data-state=([^\]]+)\]\[data-step=(\d+)\]", view) == [
        ("review", "5")
    ]
    assert "g.visit.current.breathe[data-state=review][data-step=5]" in view
    hops = re.findall(r"g\.hop\[data-from=([^\]]+)\]\[data-to=([^\]]+)\]\[data-step=(\d+)\]", view)
    assert hops == [(TICKET_40_PATH[i - 1], TICKET_40_PATH[i], str(i)) for i in range(1, 6)]
    assert set(re.findall(r"g\.next\[data-state=([^\]]+)\]", view)) == _successors(CHART, "review")
    assert len(re.findall(r"g\.hop\.next\[data-from=review\]", view)) == 5
    assert "details.fullchart" in view and '"full chart"' in view
    assert "r2/3" in out["text"]["jobview"]


@needs_node
def test_script_does_not_breathe_a_finished_job(tmp_path: Path) -> None:
    out = _run_page(tmp_path, _with_finished_tickets(tmp_path), "job", JOB, hash_="#job-t-done-1")
    view = out["dump"]["jobview"]
    assert re.search(r"g\.visit\.current\[data-state=done\]", view)
    assert "breathe" not in view
    assert "g.next" not in view
