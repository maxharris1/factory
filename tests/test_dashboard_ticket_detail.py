"""#146: the ticket detail page at job.html#job-<id>.

The Run log and the Journey render from one visit list and share one
selection; the Journey names nothing ahead of now and offers "to scale" only
when every duration is known; chart colours come from who did each step;
Can come next lists normal ways on, then human waits, then stops.
"""

import copy
import re
from pathlib import Path

from lib.dashboard import render_pages
from lib.dashboard_flow import FLOWMAP_TINT, STOP_RED
from lib.dashboard_pages import ticket_detail
from lib.dashboard_pages.journey_canvas import TINT
from tests.test_dashboard_layout import JOB, _no_script, _run_page, _section, _snapshot, needs_node

WHO = {"script", "claude", "codex", "cursor", "grok", "human"}


def _buttons(markup: str, cls: str) -> list[tuple[str, str, str]]:
    """(data-visit, data-state, aria-pressed) of each button in `<ol class="{cls}">`."""
    match = re.search(rf'<ol class="{cls}">(.*?)</ol>', markup, flags=re.S)
    assert match, f"no {cls} list"
    found = []
    for tag in re.findall(r"<button[^>]*>", match.group(1)):
        attrs = dict(re.findall(r'([a-z-]+)="([^"]*)"', tag))
        found.append((attrs["data-visit"], attrs["data-state"], attrs["aria-pressed"]))
    return found


def _review(snapshot: dict) -> dict:
    return next(row for row in snapshot["running"] if row["job"] == "h-review")


def _article(snapshot: dict) -> str:
    return _section(_no_script(render_pages(snapshot)["job.html"]), "job-h-review")


def _journey(article: str) -> str:
    return re.search(r'<section class="jd-panel"><h2><span class="t">Journey</span>.*?</section>', article, re.S).group(0)


def test_run_log_and_journey_render_from_one_list(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    path = _review(snapshot)["flow"]["path"]
    article = _article(snapshot)
    log, bars = _buttons(article, "runlog"), _buttons(article, "journey")
    assert len(log) == len(bars) == len(path)
    assert [state for _, state, _ in log] == [state for _, state, _ in bars] == path
    assert [i for i, _, _ in log] == [i for i, _, _ in bars] == [str(i) for i in range(len(path))]


def test_default_selection_is_the_current_visit(tmp_path: Path) -> None:
    article = _article(_snapshot(tmp_path))
    for cls in ("runlog", "journey"):
        assert [i for i, _, pressed in _buttons(article, cls) if pressed == "true"] == ["5"], cls


@needs_node
def test_script_renders_the_same_one_list_and_selection(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    server = _article(snapshot)
    out = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review")
    view = out["html"]["jobview"]
    for cls in ("runlog", "journey"):
        assert _buttons(view, cls) == _buttons(server, cls), cls


@needs_node
def test_picking_a_log_row_selects_its_bar_and_the_reverse(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    row = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review",
                    actions=[["click", {"attr": {"data-visit": "2"}}]])
    for cls in ("runlog", "journey"):
        assert [i for i, _, p in _buttons(row["html"]["jobview"], cls) if p == "true"] == ["2"], cls
    bar = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review",
                    actions=[["click", {"attr": {"data-visit": "3", "aria-label": "review"}}]])
    for cls in ("runlog", "journey"):
        assert [i for i, _, p in _buttons(bar["html"]["jobview"], cls) if p == "true"] == ["3"], cls


def test_to_scale_is_offered_only_when_every_duration_is_known(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _review(snapshot)["flow"]["all_durations_known"] = False
    partly = _journey(_article(snapshot))
    assert 'data-scale="even" aria-pressed="true"' in partly
    assert 'data-scale="scale"' not in partly
    timed = copy.deepcopy(snapshot)
    _review(timed)["flow"]["all_durations_known"] = True
    assert '<button type="button" data-scale="scale" aria-pressed="false">to scale</button>' in _journey(_article(timed))


def test_the_journey_names_nothing_ahead_of_now(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    flow = _review(snapshot)["flow"]
    ahead = set(flow["next"]) - set(flow["path"])
    assert ahead
    journey = _journey(_article(snapshot))
    for state in ahead:
        assert state not in journey, state


def test_chart_colours_come_from_who_never_from_the_phase() -> None:
    assert set(TINT) == set(FLOWMAP_TINT) == WHO
    assert TINT == FLOWMAP_TINT
    assert STOP_RED == ticket_detail.STOP_TINT
    css = ticket_detail.CSS
    assert not re.search(r"\.g-[a-z]+", css)
    assert not re.search(r"--g:var\(--(?:codex|cursor|claude|grok)\)", css)


def test_can_come_next_lists_normal_then_human_then_stop(tmp_path: Path) -> None:
    article = _article(_snapshot(tmp_path))
    kinds = re.findall(r'<div class="card ([a-z]+)">', article)
    order = {"normal": 0, "human": 1, "stop": 2}
    assert kinds and kinds == sorted(kinds, key=order.__getitem__)
    assert kinds[-1] == "stop"
    assert '<span class="tag">the normal way on</span>' in article


def test_a_hostile_reason_and_state_are_escaped(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    visit = _review(snapshot)["flow"]["visits"][1]
    visit["reason"] = "<img src=x onerror=y>"
    visit["state"] = '"><script>x()</script>'
    article = _article(snapshot)
    assert "<img src=x" not in article and "<script>x()" not in article
    assert "&lt;img src=x onerror=y&gt;" in article
