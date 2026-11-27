from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

import lib.dashboard_page as page
from lib.ci_lane import ci_lane
from lib.dashboard import render_pages
from lib.dashboard_pages import cockpit, common, counts, tickets
from tests import test_dashboard_layout as layout
from tests.test_dashboard_activity import HARNESS, _inner
from tests.test_dashboard_ci import ci_snapshot
from tests.test_dashboard_layout import _no_script, _snapshot

STAGES = ("Ask", "Plan", "Build", "Review", "Ship", "Stop")
CELL = re.compile(r'<a class="jc-cell" href="([^"]*)" data-cell="([^"]*)"><b(?: class="([^"]*)")?>([^<]*)</b> ([^<]*)</a>')


def cells(snapshot: dict) -> list[tuple[str, str, str, str, str]]:
    return CELL.findall(counts._counts(snapshot))


def numbers(snapshot: dict) -> dict[str, str]:
    return {key: number for _, key, _, number, _ in cells(snapshot)}


def classes(snapshot: dict) -> dict[str, str]:
    return {key: cls for _, key, cls, _, _ in cells(snapshot)}


def feeder(*states: str) -> dict:
    items = [{"repo": "maxharris1/cleffy", "issue": n, "state": state} for n, state in enumerate(states, 1)]
    return {"eligible": 0, "fed": 0, "waiting_capacity": 0, "waiting_blocked": 0, "items": items}


def test_the_cells_are_one_table_in_the_board_order_with_ci_after_build() -> None:
    assert [(cell["key"], cell["label"], cell["href"]) for cell in counts.CELLS] == [
        ("blocked", "blocked by a dependency", "index.html#intake"),
        ("Ask", "ask", "job.html"),
        ("Plan", "plan", "job.html"),
        ("Build", "build", "job.html"),
        ("ci", "ci", "index.html#lanes"),
        ("Review", "review", "job.html"),
        ("Ship", "ship", "job.html"),
        ("Stop", "stopped", "job.html"),
    ]
    assert tuple(cell["key"] for cell in counts.CELLS if cell["href"] == "job.html") == tickets.COLUMNS


def test_the_panel_draws_one_link_per_cell_in_table_order(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    html = counts._counts(snapshot)
    assert html.startswith('<section class="panel" id="counts" aria-label="Where jobs are"><h2><span>Where jobs are</span>')
    assert html.count("<a ") == len(counts.CELLS)
    assert [(href, key, label) for href, key, _, _, label in cells(snapshot)] == [
        (cell["href"], cell["key"], cell["label"]) for cell in counts.CELLS
    ]


def test_the_css_uses_existing_tokens_and_does_not_move() -> None:
    tokens = set(re.findall(r"var\((--[\w-]+)\)", counts.CSS))
    assert tokens and tokens <= {"--mono", "--text", "--muted", "--bad"}
    assert all(f"{token}:" in common.CSS_BASE for token in tokens)
    assert not re.search(r"animation|transition|@keyframes|@media", counts.CSS)


def test_the_panel_holds_no_control(tmp_path: Path) -> None:
    assert not re.search(r"<(button|form|input|select|textarea)\b", counts._counts(_snapshot(tmp_path)))


def test_the_css_is_served_after_the_cockpit_css_and_the_script_after_the_tickets_script() -> None:
    assert cockpit.CSS + counts.CSS in page.CSS
    assert tickets.SCRIPT_TICKETS + counts.SCRIPT in page.SCRIPT
    assert "__COUNTS__" not in counts.SCRIPT


def test_the_strip_is_the_first_panel_on_the_lanes_page_and_on_no_other(tmp_path: Path) -> None:
    pages = render_pages(_snapshot(tmp_path))
    assert [name for name, html in pages.items() if 'id="counts"' in html] == ["index.html"]
    hud = layout._section(_no_script(pages["index.html"]), "hud")
    after_heading = hud[hud.index("</h1>") + len("</h1>") :]
    assert after_heading.startswith('<section class="panel" id="counts"'), after_heading[:200]


def test_stage_counts_are_the_numbers_the_board_draws(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    board = _no_script(render_pages(snapshot)["job.html"])
    drawn = dict(re.findall(r'<div class="tk-col[^"]*" data-phase="(\w+)">.*?<span class="tk-count[^"]*">(\d+)</span>', board))
    assert set(drawn) == set(STAGES)
    assert {phase: numbers(snapshot)[phase] for phase in STAGES} == drawn
    assert sum(map(int, drawn.values())) > 0


def test_blocked_counts_the_feeder_items_waiting_on_a_blocker(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    assert snapshot.get("feeder") is None
    assert counts.tally(snapshot)["blocked"] == 0
    snapshot["feeder"] = feeder("waiting-blocked", "waiting-capacity", "waiting-blocked", "fed")
    assert counts.tally(snapshot)["blocked"] == 2
    assert numbers(snapshot)["blocked"] == "2"


def test_ci_counts_running_plus_queued_of_the_ci_lane(tmp_path: Path) -> None:
    snapshot = ci_snapshot(tmp_path)
    lane = ci_lane(snapshot)
    assert lane["running"] and lane["queued"]
    assert counts.tally(snapshot)["ci"] == lane["running"] + lane["queued"]
    assert numbers(snapshot)["ci"] == str(lane["running"] + lane["queued"])


def test_zero_is_drawn_muted_and_never_hidden(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    found = numbers(snapshot)
    assert list(found) == [cell["key"] for cell in counts.CELLS]
    zero = {key for key, number in found.items() if number == "0"}
    assert zero
    assert {key for key, cls in classes(snapshot).items() if cls == "jc-zero"} == zero


def test_without_a_statechart_the_stages_show_a_muted_dash(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["statechart"] = None
    found, drawn = numbers(snapshot), classes(snapshot)
    assert {phase: (found[phase], drawn[phase]) for phase in STAGES} == dict.fromkeys(STAGES, ("—", "jc-zero"))
    assert found["blocked"].isdigit() and found["ci"].isdigit()


def test_only_a_nonzero_stopped_count_is_bad(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    assert {key for key, cls in classes(snapshot).items() if cls == "jc-bad"} == {"Stop"}
    snapshot["tickets"] = [row for row in snapshot["tickets"] if not row["stuck"]]
    assert classes(snapshot)["Stop"] == "jc-zero"
    assert ".jc-cell b.jc-bad{color:var(--bad)}" in counts.CSS


def test_each_link_lands_on_a_panel_the_served_pages_draw(tmp_path: Path) -> None:
    pages = render_pages(_snapshot(tmp_path))
    assert {cell["href"].split("#")[0] for cell in counts.CELLS} <= set(pages)
    index = _no_script(pages["index.html"])
    for cell in counts.CELLS:
        if "#" in cell["href"]:
            assert f'id="{cell["href"].split("#")[1]}"' in index


def busy(tmp_path: Path) -> dict:
    snapshot = ci_snapshot(tmp_path)
    snapshot["feeder"] = feeder("waiting-blocked", "fed", "waiting-blocked")
    return snapshot


def no_statechart(tmp_path: Path) -> dict:
    snapshot = _snapshot(tmp_path)
    snapshot["statechart"] = None
    return snapshot


MOUNTS = layout.BAR + ["hud", "counts", "needs-max", "intake", "lanes", "just-happened"]


def served(snapshot: dict) -> str:
    html = render_pages(snapshot)["index.html"]
    assert layout._section(_no_script(html), "counts") == counts._counts(snapshot)
    return _inner(html, "counts")


def script(tmp_path: Path, snapshot: dict, actions: list) -> dict:
    return layout.run_harness(tmp_path, HARNESS, page.page_script(snapshot.get("statechart")), snapshot, "main",
                              MOUNTS, actions)


@layout.needs_node
@pytest.mark.parametrize("make", [_snapshot, busy, no_statechart])
def test_the_script_draws_the_strip_the_server_draws(tmp_path: Path, make) -> None:
    snapshot = make(tmp_path)
    if make is busy:
        assert counts.tally(snapshot)["blocked"] == 2 and counts.tally(snapshot)["ci"] > 0
    assert script(tmp_path / "run", snapshot, [])["html"]["counts"] == served(snapshot)


@layout.needs_node
def test_a_poll_with_new_counts_redraws_the_strip_in_place(tmp_path: Path) -> None:
    (tmp_path / "first").mkdir()
    (tmp_path / "later").mkdir()
    first = _snapshot(tmp_path / "first")
    later = busy(tmp_path / "later")
    later["generated_at_epoch"] = first["generated_at_epoch"] + 2
    assert counts.tally(first) != counts.tally(later)
    actions = [["record", "before"], ["snap", ["counts"]], ["poll", later], ["record", "after"], ["snap", ["counts"]]]
    out = script(tmp_path / "run", copy.deepcopy(first), actions)
    records = [step["kids"] for step in out["steps"] if "record" in step]
    assert [kids.count("counts") for kids in records] == [1, 1]
    assert [step["snap"]["counts"] for step in out["steps"] if "snap" in step] == [served(first), served(later)]
