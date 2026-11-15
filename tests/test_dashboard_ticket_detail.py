"""#146: the ticket detail page at job.html#job-<id>.

The Run log and the Journey render from one visit list and share one
selection; the Journey names nothing ahead of now and offers "to scale" only
when every duration is known; chart colours come from who did each step;
Can come next lists normal ways on, then human waits, then stops.
"""

import copy
import functools
import http.server
import json
import re
import threading
import time
from pathlib import Path

import pytest

from lib.dashboard import render_pages
from lib.dashboard_flow import FLOWMAP_TINT, STOP_RED
from lib.dashboard_pages import ticket_detail
from lib.dashboard_pages.shell import PX_ACTIONS, PX_STATE
from lib.dashboard_pages.journey_canvas import TINT
from tests.test_dashboard_layout import JOB, _no_script, _run_page, _section, _snapshot, needs_node
from tests.test_dashboard_system import CHROME, QuietHandler, _Chrome

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


def _hero_px(markup: str) -> dict[str, str]:
    tag = re.search(r'<section class="jd-hero"><canvas[^>]*>', markup).group(0)
    return dict(re.findall(r'([a-z-]+)="([^"]*)"', tag))


def _human_wait(row: dict) -> None:
    row["flow"]["state"] = "waiting-max"
    row["flow"]["visits"][-1]["state"] = "waiting-max"
    row["flow"]["path"][-1] = "waiting-max"


def _set(**fields):
    return lambda row: row.update(fields)


HERO = [
    *[(action, _set(action=action), PX_STATE[action]["effect"], "codex", True) for action in PX_ACTIONS],
    ("stale", _set(job_state="stale"), "decay", "codex", False),
    ("dead", _set(job_state="dead"), "static", "#f7768e", False),
    ("overdue", _set(overdue=True), "static", "#f7768e", False),
    ("human wait", _human_wait, "static", "#ff9e64", False),
]


def _hero_case(snapshot: dict, change) -> None:
    change(_review(snapshot))


@pytest.mark.parametrize(("case", "change", "effect", "tint", "verb"), HERO, ids=[c[0] for c in HERO])
def test_the_hero_strip_follows_the_state_table(tmp_path: Path, case, change, effect, tint, verb) -> None:
    snapshot = _snapshot(tmp_path)
    _hero_case(snapshot, change)
    article = _article(snapshot)
    px = _hero_px(article)
    assert (px["data-px"], px["data-tint"]) == (effect, tint), case
    assert ('<span class="verb">' in article) is verb, case


@needs_node
@pytest.mark.parametrize(("case", "change", "effect", "tint", "verb"), HERO, ids=[c[0] for c in HERO])
def test_the_script_hero_strip_follows_the_state_table(tmp_path: Path, case, change, effect, tint, verb) -> None:
    snapshot = _snapshot(tmp_path)
    _hero_case(snapshot, change)
    view = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review")["html"]["jobview"]
    px = _hero_px(view)
    assert (px["data-px"], px["data-tint"]) == (effect, tint), case
    assert ('<span class="verb">' in view) is verb, case


def _serve(tmp_path: Path, sites: dict[str, dict]) -> http.server.ThreadingHTTPServer:
    for name, snapshot in sites.items():
        site = tmp_path / "site" / name
        site.mkdir(parents=True)
        for page, html in render_pages(snapshot).items():
            (site / page).write_text(html, encoding="utf-8")
        (site / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    handler = functools.partial(QuietHandler, directory=str(tmp_path / "site"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _wait(chrome: _Chrome, ready: str) -> None:
    deadline = time.monotonic() + 30
    while not chrome.run(f"document.readyState === 'complete' && !!({ready})"):
        assert time.monotonic() < deadline, f"never showed {ready}"
        time.sleep(0.05)


def _open(chrome: _Chrome, url: str, ready: str) -> None:
    chrome.send("Page.navigate", chrome.session, url=url)
    _wait(chrome, ready)


PAINTED = r"""
(sel => {
  const c = document.querySelector(sel);
  if (!c) return -1;
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  let n = 0;
  for (let i = 3; i < d.length; i += 4) if (d[i]) n++;
  return n;
})"""

SIZES = "[...document.querySelectorAll('ol.journey button')].map(b => [b.offsetWidth, b.offsetHeight])"


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_the_lanes_modal_draws_the_journey_and_chart(tmp_path: Path) -> None:
    server = _serve(tmp_path, {"lanes": _snapshot(tmp_path)})
    chrome = _Chrome(tmp_path / "profile")
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/lanes/"
        _open(chrome, base + "index.html", "document.querySelector('a[href=\"job.html#job-h-review\"]')")
        chrome.run("document.querySelector('a[href=\"job.html#job-h-review\"]').click()")
        _wait(chrome, "document.querySelector('#job-modal canvas.jd-journey')")
        time.sleep(0.4)
        journey = chrome.run(f"{PAINTED}('#job-modal canvas.jd-journey')")
        sizes = chrome.run(SIZES.replace("document.", "document.getElementById('job-modal')."))
        chrome.run("document.querySelector('#job-modal details.jd-chart').open = true")
        time.sleep(0.4)
        chart = chrome.run(f"{PAINTED}('#job-modal canvas.jd-flowmap')")
    finally:
        chrome.close()
        server.shutdown()
    assert journey > 0, "the modal Journey is blank"
    assert sizes and all(w > 0 and h > 0 for w, h in sizes), sizes
    assert chart > 0, "the modal Full state chart is blank"


STILL_PIXELS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  document.querySelector('details.jd-chart').open = true;
  await sleep(3300);
  const shot = () => [...document.querySelectorAll('canvas.jd-journey, canvas.jd-flowmap')].map(c => c.toDataURL());
  const a = shot();
  await sleep(400);
  const b = shot();
  return a.length === 2 && a.every((x, i) => x === b[i]);
})()"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_broken_jobs_and_human_waits_hold_the_journey_still(tmp_path: Path) -> None:
    cases = {"working": _set(), "stale": _set(job_state="stale"), "dead": _set(job_state="dead"), "human": _human_wait}
    sites = {}
    for name, change in cases.items():
        (tmp_path / name).mkdir()
        snapshot = _snapshot(tmp_path / name)
        _hero_case(snapshot, change)
        sites[name] = snapshot
    server = _serve(tmp_path, sites)
    chrome = _Chrome(tmp_path / "profile")
    still = {}
    try:
        for name in cases:
            _open(chrome, f"http://127.0.0.1:{server.server_address[1]}/{name}/job.html#job-h-review",
                  "document.querySelector('#job-h-review canvas.jd-journey')")
            still[name] = chrome.run(STILL_PIXELS)
    finally:
        chrome.close()
        server.shutdown()
    assert still == {"working": False, "stale": True, "dead": True, "human": True}
