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


def _rows(markup: str, cls: str) -> list[tuple[str, str, str]]:
    """(data-visit, data-state, selected) of each visit in `<ol class="{cls}">`, a button or a plain row."""
    match = re.search(rf'<ol class="{cls}">(.*?)</ol>', markup, flags=re.S)
    assert match, f"no {cls} list"
    found = []
    for tag in re.findall(r'<[a-z]+[^>]*\bdata-visit="[^>]*>', match.group(1)):
        attrs = dict(re.findall(r'([a-z-]+)="([^"]*)"', tag))
        selected = "true" in (attrs.get("aria-pressed"), attrs.get("aria-current"))
        found.append((attrs["data-visit"], attrs["data-state"], "true" if selected else "false"))
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
    log, bars = _rows(article, "runlog"), _rows(article, "journey")
    assert len(log) == len(bars) == len(path)
    assert [state for _, state, _ in log] == [state for _, state, _ in bars] == path
    assert [i for i, _, _ in log] == [i for i, _, _ in bars] == [str(i) for i in range(len(path))]


def test_without_script_the_run_log_rows_are_plain_rows_not_buttons(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    path = _review(snapshot)["flow"]["path"]
    log = re.search(r'<ol class="runlog">(.*?)</ol>', _article(snapshot), re.S).group(1)
    assert "<button" not in log and "aria-pressed" not in log
    rows = re.findall(r'<div class="visit[ "][^>]*>', log)
    assert len(rows) == len(path)
    assert [i for i, row in enumerate(rows) if 'aria-current="true"' in row] == [4]


def test_default_selection_is_the_current_visit(tmp_path: Path) -> None:
    article = _article(_snapshot(tmp_path))
    for cls in ("runlog", "journey"):
        assert [i for i, _, pressed in _rows(article, cls) if pressed == "true"] == ["4"], cls


@needs_node
def test_script_renders_the_same_one_list_and_selection(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    server = _article(snapshot)
    out = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review")
    view = out["html"]["jobview"]
    for cls in ("runlog", "journey"):
        assert _rows(view, cls) == _rows(server, cls), cls


@needs_node
def test_picking_a_log_row_selects_its_bar_and_the_reverse(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    row = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review",
                    actions=[["click", {"attr": {"data-visit": "1"}}]])
    for cls in ("runlog", "journey"):
        assert [i for i, _, p in _rows(row["html"]["jobview"], cls) if p == "true"] == ["1"], cls
    bar = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review",
                    actions=[["click", {"attr": {"data-visit": "2", "aria-label": "Step 3 of 5: review, codex, 16m 40s"}}]])
    for cls in ("runlog", "journey"):
        assert [i for i, _, p in _rows(bar["html"]["jobview"], cls) if p == "true"] == ["2"], cls


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


NEW_ACTIONS = [("clarify", "field", "Clarifying"), ("update-from-main", "patch", "Merging")]


@pytest.mark.parametrize(("action", "effect", "verb"), NEW_ACTIONS)
def test_clarify_and_update_from_main_heroes_draw_like_plan_and_fix(tmp_path: Path, action, effect, verb) -> None:
    snapshot = _snapshot(tmp_path)
    _hero_case(snapshot, _set(action=action))
    article = _article(snapshot)
    assert (_hero_px(article)["data-px"], _hero_px(article)["data-tint"]) == (effect, "codex")
    assert f'<span class="verb">{verb}…</span>' in article


@needs_node
@pytest.mark.parametrize(("action", "effect", "verb"), NEW_ACTIONS)
def test_the_script_draws_clarify_and_update_from_main_heroes(tmp_path: Path, action, effect, verb) -> None:
    snapshot = _snapshot(tmp_path)
    _hero_case(snapshot, _set(action=action))
    view = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review")["html"]["jobview"]
    assert (_hero_px(view)["data-px"], _hero_px(view)["data-tint"]) == (effect, "codex")
    assert f'<span class="verb">{verb}…</span>' in view


def _serve(tmp_path: Path, sites: dict[str, dict], status_delay: float = 0.0) -> http.server.ThreadingHTTPServer:
    for name, snapshot in sites.items():
        site = tmp_path / "site" / name
        site.mkdir(parents=True)
        for page, html in render_pages(snapshot).items():
            (site / page).write_text(html, encoding="utf-8")
        (site / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")

    class Handler(QuietHandler):
        def do_GET(self) -> None:
            if self.path.endswith("/status.json"):
                time.sleep(status_delay)
            super().do_GET()

    handler = functools.partial(Handler, directory=str(tmp_path / "site"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _wait(chrome: _Chrome, ready: str) -> None:
    chrome.wait_for(f"document.readyState === 'complete' && !!({ready})", ready)


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

SIZES = r"""
(sel => [...document.querySelectorAll(sel + ' ol.journey button')].map(b => [b.offsetWidth, b.offsetHeight]))"""


def _journey_drawn(article: str) -> str:
    return f"{PAINTED}('{article} canvas.jd-journey') > 0"


def _eventually(chrome: _Chrome, expression: str) -> bool:
    deadline = time.monotonic() + 30
    while not (shown := bool(chrome.run(expression))) and time.monotonic() < deadline:
        time.sleep(0.05)
    return shown


def _drawn(chrome: _Chrome, article: str) -> dict:
    _wait(chrome, f"document.querySelector('{article} canvas.jd-journey')")
    journey = _eventually(chrome, _journey_drawn(article))
    buttons = _eventually(chrome, f"(s => s.length > 0 && s.every(([w, h]) => w > 0 && h > 0))({SIZES}('{article}'))")
    chrome.run(f"document.querySelector('{article} details.jd-chart').open = true")
    chart = _eventually(chrome, f"{PAINTED}('{article} canvas.jd-flowmap') > 0")
    return {"journey": journey, "buttons": buttons, "chart": chart}


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
@pytest.mark.parametrize("status_delay", [0.0, 1.5], ids=["status now", "status late"])
def test_every_way_onto_a_job_draws_the_journey_and_chart(tmp_path: Path, status_delay: float) -> None:
    server = _serve(tmp_path, {"lanes": _snapshot(tmp_path)}, status_delay)
    chrome = _Chrome(tmp_path / "profile")
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/lanes/"
        _open(chrome, base + "index.html", "document.querySelector('a[href=\"job.html#job-h-review\"]')")
        chrome.run("document.querySelector('a[href=\"job.html#job-h-review\"]').click()")
        from_lanes = _drawn(chrome, "#job-h-review")
        chrome.run("location.hash = '#job-r-cursor'")
        after_hashchange = _drawn(chrome, "#job-r-cursor")
    finally:
        chrome.close()
        server.shutdown()
    drawn = {"journey": True, "buttons": True, "chart": True}
    assert {"lanes tile": from_lanes, "hash change": after_hashchange} == {"lanes tile": drawn, "hash change": drawn}


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
                  _journey_drawn("#job-h-review"))
            still[name] = chrome.run(STILL_PIXELS)
    finally:
        chrome.close()
        server.shutdown()
    assert still == {"working": False, "stale": True, "dead": True, "human": True}


BEATS = r"""
const [script, rowsText] = process.argv.slice(2);
let nowMs = 1000;
Date.now = () => nowMs;
const page = new Function('window', require('fs').readFileSync(script, 'utf8') + '\nreturn {selection: selection};');
const {selection} = page({matchMedia: () => ({matches: false})});
const m = {selected: 0, visits: [{}], all_durations_known: false};
console.log(JSON.stringify(JSON.parse(rowsText).map(r => { nowMs += 20000; return selection(r, m).beatStart; })));
"""


def _beats(tmp_path: Path, rows: list[dict]) -> list:
    """beatStart after each poll's row goes through the page's own selection()."""
    import subprocess
    from tests.test_dashboard_layout import NODE
    (tmp_path / "page.js").write_text(ticket_detail.SCRIPT, encoding="utf-8")
    (tmp_path / "beats.js").write_text(BEATS, encoding="utf-8")
    result = subprocess.run([NODE, str(tmp_path / "beats.js"), str(tmp_path / "page.js"), json.dumps(rows)],
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


@needs_node
def test_the_journey_beats_once_per_real_heartbeat(tmp_path: Path) -> None:
    import sqlite3
    from lib.dashboard import collect_snapshot
    from tests.test_dashboard import add_job, seeded
    from tests.test_dashboard_layout import NOW, _history

    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _history(connection)
        add_job(connection, "h-next", action="fix", recipient="claude", state="queued",
                ticket_id="40", created_at=NOW - 10, last_activity_at=NOW - 10)

    def rows(now: float) -> dict:
        snap = collect_snapshot(db, log, now=now)
        return {r["job"]: r for r in snap["running"] + snap["queued"]}

    polls = [rows(NOW + 0.1), rows(NOW + 20.9), rows(NOW + 40.1)]
    assert [p["h-review"]["heartbeat_age_seconds"] for p in polls] == [30, 50, 70]
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET last_activity_at = ? WHERE id = 'h-review2'", (NOW + 55,))
        connection.execute("UPDATE jobs SET state = 'running', last_activity_at = ? WHERE id = 'h-next'", (NOW + 55,))
    polls.append(rows(NOW + 60.4))
    assert _beats(tmp_path, [p["h-review"] for p in polls]) == [None, None, None, 81000]
    assert _beats(tmp_path, [p["h-next"] for p in polls]) == [None, None, None, 81000]


def _caption(markup: str) -> str:
    return re.sub(r"<[^>]+>", "|", re.search(r'<div class="jd-cap[^"]*"[^>]*>.*?</div>', markup, re.S).group(0))


def test_the_caption_names_the_selected_visit(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    row = _review(snapshot)
    visits = row["flow"]["visits"]
    caption = _caption(_journey(_article(snapshot)))
    now = visits[-1]
    assert f"|{len(visits):02d} of {len(visits)}|" in caption
    assert f"|{now['state']} · now|" in caption
    assert f"|{now['who']}|" in caption
    assert " so far|" in caption or "|in progress|" in caption


@needs_node
def test_picking_a_visit_rewrites_the_caption_in_both_renderers(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    visit = _review(snapshot)["flow"]["visits"][2]
    out = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review",
                    actions=[["click", {"attr": {"data-visit": "2", "data-state": visit["state"]}}]])
    caption = _caption(out["html"]["jobview"])
    total = len(_review(snapshot)["flow"]["visits"])
    assert f"|03 of {total}|" in caption, caption
    assert f"|{visit['state']}|" in caption and f"|{visit['who']}|" in caption, caption
    if visit["reason"]:
        assert visit["reason"] in caption, caption
    from lib.dashboard_pages.common import duration
    age = _review(snapshot)["flow"]["state_age_seconds"]
    loaded = _run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review")
    ticked = _caption(_article(snapshot)).replace(f"|{duration(age)} so far|", f"|{duration(age + 3)} so far|")
    assert _caption(loaded["html"]["jobview"]) == ticked


DRAWN_NAMES = r"""
window.journeyTexts = [];
const original = CanvasRenderingContext2D.prototype.fillText;
CanvasRenderingContext2D.prototype.fillText = function (text, ...args) {
  if (this.canvas.classList.contains('jd-journey')) window.journeyTexts.push(String(text));
  return original.call(this, text, ...args);
};
"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_the_journey_names_each_visit_up_to_now_on_its_bar(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    flow = _review(snapshot)["flow"]
    server = _serve(tmp_path, {"fixture": snapshot})
    chrome = _Chrome(tmp_path / "profile")
    try:
        chrome.send("Page.enable", chrome.session)
        chrome.send("Page.addScriptToEvaluateOnNewDocument", chrome.session, source=DRAWN_NAMES)
        _open(chrome, f"http://127.0.0.1:{server.server_address[1]}/fixture/job.html#job-h-review",
              _journey_drawn("#job-h-review"))
        time.sleep(3.4)
        drawn = set(chrome.run("[...new Set(window.journeyTexts)]"))
    finally:
        chrome.close()
        server.shutdown()
    assert set(flow["path"]) <= drawn
    assert not (set(flow["next"]) - set(flow["path"])) & drawn


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_a_rebuild_keeps_the_picked_scale_pressed(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _review(snapshot)["flow"]["all_durations_known"] = True
    server = _serve(tmp_path, {"timed": snapshot})
    chrome = _Chrome(tmp_path / "profile")
    pressed = "[...document.querySelectorAll('#job-h-review [data-scale]')].map(b => b.dataset.scale + '=' + b.getAttribute('aria-pressed'))"
    try:
        _open(chrome, f"http://127.0.0.1:{server.server_address[1]}/timed/job.html#job-h-review",
              _journey_drawn("#job-h-review"))
        chrome.run("document.querySelector('#job-h-review [data-scale=\"scale\"]').click()")
        chrome.run("location.hash = '#job-r-cursor'")
        _wait(chrome, "document.querySelector('#job-r-cursor canvas.jd-journey')")
        chrome.run("location.hash = '#job-h-review'")
        _wait(chrome, "document.querySelector('#job-h-review [data-scale]')")
        after = chrome.run(pressed)
    finally:
        chrome.close()
        server.shutdown()
    assert after == ["even=false", "scale=true"]


def _fully_timed(snapshot: dict) -> dict:
    flow = _review(snapshot)["flow"]
    flow["all_durations_known"] = True
    for i, visit in enumerate(flow["visits"]):
        visit.update(duration_seconds=(i + 1) * 600, time_known=True)
    return snapshot


def _click_canvas(chrome: _Chrome, x: int, y: int) -> None:
    point = chrome.run(
        f"(() => {{const r = document.querySelector('#job-h-review .jd-journey').getBoundingClientRect();"
        f"return {{x: r.x + {x}, y: r.y + {y}}}}})()"
    )
    for kind in ("mousePressed", "mouseReleased"):
        chrome.send("Input.dispatchMouseEvent", chrome.session, type=kind, button="left", clickCount=1, **point)


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_under_reduced_motion_a_scale_change_repaints_the_bars_it_hit_tests(tmp_path: Path) -> None:
    server = _serve(tmp_path, {"timed": _fully_timed(_snapshot(tmp_path))})
    chrome = _Chrome(tmp_path / "profile")
    canvas = "document.querySelector('#job-h-review .jd-journey').toDataURL()"
    try:
        chrome.send("Emulation.setEmulatedMedia", chrome.session,
                    features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
        _open(chrome, f"http://127.0.0.1:{server.server_address[1]}/timed/job.html#job-h-review",
              _journey_drawn("#job-h-review"))
        even = chrome.run(canvas)
        chrome.run("document.querySelector('#job-h-review [data-scale=scale]').click()")
        time.sleep(0.2)
        clicked = chrome.run(canvas)
        chrome.run("location.hash = '#job-r-cursor'")
        _wait(chrome, "document.querySelector('#job-r-cursor canvas.jd-journey')")
        chrome.run("location.hash = '#job-h-review'")
        _wait(chrome, "document.querySelector('#job-h-review [data-scale=scale][aria-pressed=true]')")
        time.sleep(0.4)
        rebuilt = chrome.run(canvas)
        _click_canvas(chrome, 336, 140)
        selected = chrome.run("(b => [b.dataset.visit, b.dataset.state])(document.querySelector('#job-h-review .runlog [aria-pressed=true]'))")
        chrome.run("document.querySelector('#job-h-review details.jd-chart').open = true")
        time.sleep(0.2)
        chart = chrome.run(f"{PAINTED}('#job-h-review canvas.jd-flowmap')")
    finally:
        chrome.close()
        server.shutdown()
    assert {"scale drawn": clicked != even, "same as a fresh to-scale paint": clicked == rebuilt,
            "review drawn at x=336 to scale": selected, "chart painted": chart > 0} == {
        "scale drawn": True, "same as a fresh to-scale paint": True,
        "review drawn at x=336 to scale": ["2", "review"], "chart painted": True}


CONTROL_STRIP = r"""
(() => {
  const ctl = document.querySelector('#job-h-review .jd-ctl');
  const look = b => {const c = getComputedStyle(b); return [b.offsetHeight, c.backgroundColor, c.color, c.borderTopColor];};
  return {
    axis: ctl.querySelector('[role=group]').getAttribute('aria-label'),
    scales: [...ctl.querySelectorAll('[role=group] [data-scale]')].map(b => [b.textContent, b.getAttribute('aria-pressed'), ...look(b)]),
    replay: look(ctl.querySelector('[data-replay]')),
    keys: [...ctl.querySelectorAll('.key')].map(k => k.textContent),
    separateLegend: !!document.querySelector('#job-h-review .jd-panel .jd-legend'),
  };
})()"""

OFF = ["rgb(31, 34, 48)", "rgb(192, 202, 245)", "rgb(65, 72, 104)"]
ON = ["rgba(158, 206, 106, 0.09)", "rgb(158, 206, 106)", "rgb(158, 206, 106)"]


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_the_journey_control_strip_matches_journey_climb(tmp_path: Path) -> None:
    server = _serve(tmp_path, {"timed": _fully_timed(_snapshot(tmp_path))})
    chrome = _Chrome(tmp_path / "profile")
    try:
        _open(chrome, f"http://127.0.0.1:{server.server_address[1]}/timed/job.html#job-h-review",
              "document.querySelector('#job-h-review .jd-ctl')")
        strip = chrome.run(CONTROL_STRIP)
    finally:
        chrome.close()
        server.shutdown()
    assert strip == {
        "axis": "Time axis",
        "scales": [["even steps", "true", 44, *ON], ["to scale", "false", 44, *OFF]],
        "replay": [44, *OFF],
        "keys": PATH_KEYS,
        "separateLegend": False,
    }


def test_the_no_script_control_strip_holds_the_axis_replay_and_legend_in_one_row(tmp_path: Path) -> None:
    journey = _journey(_article(_fully_timed(_snapshot(tmp_path))))
    strip = re.search(r'<div class="jd-ctl">(.*)</div></section>$', journey, re.S).group(1)
    assert re.search(r'<div class="axis" role="group" aria-label="Time axis">.*data-scale="even".*data-scale="scale".*</div>'
                     r'<button type="button" data-replay="1">replay</button><span class="gap"></span>', strip, re.S)
    assert re.findall(r'<span class="key"><i[^>]*></i>([^<]+)</span>', strip) == PATH_KEYS
    assert "jd-legend" not in journey


def _queued(snapshot: dict) -> str:
    return _section(_no_script(render_pages(snapshot)["job.html"]), "job-q-claude")


def _script_view(tmp_path: Path, snapshot: dict, job: str = "h-review", actions: list | None = None) -> str:
    out = _run_page(tmp_path, snapshot, "job", JOB, hash_=f"#job-{job}", actions=actions)
    return out["html"]["jobview"]


def _beat_class(markup: str) -> str:
    match = re.search(r'<div class="jd-tile"><b(?: class="([a-z]*)")?>[^<]*(?:<span>[^<]*</span>)?</b>'
                      r'<span>last heartbeat</span>', markup)
    assert match, markup
    return match.group(1) or ""


BEAT = [
    ("running fresh", _set(), "ok"),
    ("claimed", _set(job_state="claimed"), ""),
    ("stale", _set(job_state="stale"), "warn"),
    ("dead", _set(job_state="dead"), "bad"),
    ("overdue", _set(overdue=True), "bad"),
]


@pytest.mark.parametrize(("case", "change", "tone"), BEAT, ids=[c[0] for c in BEAT])
def test_the_heartbeat_tile_takes_its_tone_from_the_job(tmp_path: Path, case, change, tone) -> None:
    snapshot = _snapshot(tmp_path)
    _hero_case(snapshot, change)
    assert _beat_class(_article(snapshot)) == tone, case


@needs_node
@pytest.mark.parametrize(("case", "change", "tone"), BEAT, ids=[c[0] for c in BEAT])
def test_the_script_heartbeat_tile_takes_its_tone_from_the_job(tmp_path: Path, case, change, tone) -> None:
    snapshot = _snapshot(tmp_path)
    _hero_case(snapshot, change)
    assert _beat_class(_script_view(tmp_path, snapshot)) == tone, case


def test_a_queued_job_s_heartbeat_tile_has_no_tone(tmp_path: Path) -> None:
    assert _beat_class(_queued(_snapshot(tmp_path))) == ""


@needs_node
def test_the_script_gives_a_queued_job_s_heartbeat_tile_no_tone(tmp_path: Path) -> None:
    assert _beat_class(_script_view(tmp_path, _snapshot(tmp_path), "q-claude")) == ""


def test_the_heartbeat_tone_is_the_lanes_heartbeat_rule() -> None:
    from lib.dashboard_pages.lanes import TILE_STATE
    lanes = {"live": "ok", "warn": "warn", "bad": "bad", "none": ""}
    assert ticket_detail.HEARTBEAT_TONE == {name: lanes[spec["hb"]] for name, spec in TILE_STATE.items()}
    assert ".jd-tile b.warn{color:var(--warn)}" in ticket_detail.CSS
    assert ".jd-tile b.bad{color:var(--bad)}" in ticket_detail.CSS


def _title(markup: str) -> str:
    return re.search(r'<div class="title">(.*?)</div>', markup, re.S).group(1)


CHIPS = [
    ("running", _set(), None),
    ("claimed", _set(job_state="claimed"), "claimed, not started"),
    ("stale", _set(job_state="stale"), "stale heartbeat"),
    ("dead", _set(job_state="dead"), "runner dead"),
    ("overdue", _set(overdue=True), "OVERDUE"),
    ("retries", _set(retries=2), "retried 2x"),
]


def _chips(title: str) -> list[str]:
    return re.findall(r'<span class="chip [a-z]+">([^<]*)</span>', title)


@pytest.mark.parametrize(("case", "change", "chip"), CHIPS, ids=[c[0] for c in CHIPS])
def test_the_hero_chips_only_trouble(tmp_path: Path, case, change, chip) -> None:
    snapshot = _snapshot(tmp_path)
    _hero_case(snapshot, change)
    assert _chips(_title(_article(snapshot))) == ([chip] if chip else []), case


@needs_node
@pytest.mark.parametrize(("case", "change", "chip"), CHIPS, ids=[c[0] for c in CHIPS])
def test_the_script_hero_chips_only_trouble(tmp_path: Path, case, change, chip) -> None:
    snapshot = _snapshot(tmp_path)
    _hero_case(snapshot, change)
    assert _chips(_title(_script_view(tmp_path, snapshot))) == ([chip] if chip else []), case


def _now_durs(markup: str) -> dict[str, str]:
    hero = re.search(r'<p class="line">.*?</p>', markup, re.S).group(0)
    log = re.search(r'<(?:button|div)[^>]*class="visit now"[^>]*>.*?<span class="dur">([^<]*)</span>', markup, re.S)
    cap = re.search(r'<div class="jd-cap now"[^>]*>.*?<span class="dur">([^<]*)</span>', markup, re.S)
    return {"hero": re.search(r"(\d+m \d\ds) in review", hero).group(1), "log": log.group(1), "cap": cap.group(1)}


def test_the_current_step_says_how_long_so_far(tmp_path: Path) -> None:
    from lib.dashboard_pages.common import duration
    snapshot = _snapshot(tmp_path)
    age = _review(snapshot)["flow"]["state_age_seconds"]
    durs = _now_durs(_article(snapshot))
    assert durs == {"hero": duration(age), "log": f"{duration(age)} so far", "cap": f"{duration(age)} so far"}


@needs_node
def test_the_script_counts_the_current_step_up_with_the_hero_line(tmp_path: Path) -> None:
    from lib.dashboard_pages.common import duration
    snapshot = _snapshot(tmp_path)
    age = _review(snapshot)["flow"]["state_age_seconds"]
    first = _now_durs(_script_view(tmp_path, snapshot))
    later = _now_durs(_script_view(tmp_path, snapshot, actions=[["advance", 5]]))
    assert first["log"] == first["cap"] == first["hero"] + " so far"
    assert later["log"] == later["cap"] == later["hero"] + " so far"
    assert first["hero"] == duration(age + 3) and later["hero"] == duration(age + 8)


def test_in_progress_only_when_the_time_in_state_is_unknown(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _review(snapshot)["flow"]["state_age_seconds"] = None
    article = _article(snapshot)
    assert re.search(r'<(?:button|div)[^>]*class="visit now"[^>]*>.*?<span class="dur">in progress</span>', article, re.S)
    assert '<span class="dur">in progress</span><span class="note">' in _caption_html(article)


@needs_node
def test_the_script_says_in_progress_only_when_the_time_in_state_is_unknown(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _review(snapshot)["flow"]["state_age_seconds"] = None
    view = _script_view(tmp_path, snapshot, actions=[["advance", 5]])
    assert re.search(r'<(?:button|div)[^>]*class="visit now"[^>]*>.*?<span class="dur">in progress</span>', view, re.S)
    assert '<span class="dur">in progress</span><span class="note">' in _caption_html(view)


def _caption_html(markup: str) -> str:
    return re.search(r'<div class="jd-cap[^"]*"[^>]*>.*?</div>', markup, re.S).group(0)


def _step_labels(markup: str) -> list[str]:
    ol = re.search(r'<ol class="journey">(.*?)</ol>', markup, re.S).group(1)
    return re.findall(r'<button[^>]*aria-label="([^"]*)"', ol)


def _steps(markup: str) -> list[str]:
    ol = re.search(r'<ol class="journey">(.*?)</ol>', markup, re.S).group(1)
    return re.findall(r'<span class="step">([^<]*)</span>', ol)


STEP_NAMES = [
    "Step 1 of 5: intake, script, time unknown",
    "Step 2 of 5: building, claude, 16m 40s",
    "Step 3 of 5: review, codex, 16m 40s",
    "Step 4 of 5: fixing, claude, 16m 40s",
    "Step 5 of 5: review, codex, 33m 20s so far",
]


def test_each_journey_step_is_named_by_place_state_actor_and_time(tmp_path: Path) -> None:
    article = _article(_snapshot(tmp_path))
    assert _step_labels(article) == STEP_NAMES
    assert _steps(article) == STEP_NAMES


@needs_node
def test_the_script_names_each_journey_step_the_same_way(tmp_path: Path) -> None:
    view = _script_view(tmp_path, _snapshot(tmp_path))
    assert _step_labels(view) == STEP_NAMES
    assert _steps(view) == STEP_NAMES
    assert len(set(STEP_NAMES)) == len(STEP_NAMES)


def test_no_script_css_shows_the_step_list_and_hides_canvases_and_dead_controls() -> None:
    css = ticket_detail.CSS
    assert ".journey .step{display:none}" in css
    block = re.search(r"@media \(scripting:none\)\{(.*)\}\n", css).group(1)
    hidden = re.search(r"^([^{]*)\{display:none\}", block).group(1).split(",")
    assert set(hidden) == {".jd-box canvas", ".journey button", ".jd-ctl .axis", ".jd-ctl [data-replay]", ".jd-chart .jd-box"}
    assert ".journey .step{display:inline}" in block


NO_SCRIPT_VIEW = r"""
(() => {
  const art = document.querySelector('#job-h-review');
  const shown = n => n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
  return {
    canvases: [...art.querySelectorAll('canvas.jd-journey, canvas.jd-flowmap')].filter(shown).length,
    buttons: [...art.querySelectorAll('.jd-box button, .jd-ctl button, .jd-chart button')].filter(shown).length,
    steps: [...art.querySelectorAll('ol.journey .step')].filter(shown).map(s => s.textContent),
    allButtons: [...document.body.querySelectorAll('button')].filter(shown).length,
    filterButtons: [...document.querySelectorAll('#ticket-filter button')].filter(shown).length,
    logNames: [...art.querySelectorAll('ol.runlog .visit .name')].filter(shown).map(s => s.textContent),
  };
})()"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_with_scripting_off_the_journey_is_a_readable_list(tmp_path: Path) -> None:
    snapshot = _fully_timed(_snapshot(tmp_path))
    path = _review(snapshot)["flow"]["path"]
    server = _serve(tmp_path, {"fixture": snapshot})
    url = f"http://127.0.0.1:{server.server_address[1]}/fixture/job.html#job-h-review"
    views = {}
    try:
        for name, off in (("off", True), ("on", False)):
            chrome = _Chrome(tmp_path / f"profile-{name}")
            try:
                chrome.send("Emulation.setScriptExecutionDisabled", chrome.session, value=off)
                _open(chrome, url, "document.querySelector('#job-h-review ol.journey')")
                chrome.run("document.querySelector('#job-h-review details.jd-chart').open = true")
                time.sleep(0.3)
                views[name] = chrome.run(NO_SCRIPT_VIEW)
            finally:
                chrome.close()
    finally:
        server.shutdown()
    assert views["off"]["canvases"] == 0 and views["off"]["buttons"] == 0 and views["off"]["allButtons"] == 0
    assert [step.split(": ")[1].split(",")[0] for step in views["off"]["steps"]] == path
    assert [name.removesuffix(" · now") for name in views["off"]["logNames"]] == path
    assert views["on"]["canvases"] == 2 and views["on"]["steps"] == []
    assert views["on"]["buttons"] == len(path) + 3
    assert views["on"]["filterButtons"] > 0
    assert views["on"]["allButtons"] == 2 * len(path) + 3 + views["on"]["filterButtons"]


def test_a_queued_job_s_live_run_says_not_started_yet(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    queued = _queued(snapshot)
    assert '<p class="empty">not started yet</p>' in queued
    assert "no telemetry" not in queued
    assert '<p class="empty">no telemetry</p>' in _article(snapshot)


@needs_node
def test_the_script_says_a_queued_job_has_not_started_yet(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    queued = _script_view(tmp_path, snapshot, "q-claude")
    assert '<p class="empty">not started yet</p>' in queued and "no telemetry" not in queued
    assert '<p class="empty">no telemetry</p>' in _script_view(tmp_path, snapshot)


def _keys(markup: str) -> list[str]:
    return re.findall(r'<span class="key"><i[^>]*></i>([^<]+)</span>', markup)


def _chart_keys(markup: str) -> list[str]:
    legend = re.search(r'<div class="jd-legend">(.*?)</div>', markup, re.S).group(1)
    return re.findall(r"<span><i[^>]*></i>([^<]+)</span>", legend)


PATH_KEYS = ["script", "claude", "codex", "stopped", "off-chart hop"]
CHART_KEYS = ["path taken · script", "path taken · claude", "path taken · codex", "visited", "off-chart jump", "now",
              "can come next"]
GROK_KEYS = ["script", "claude", "codex", "grok", "stopped", "off-chart hop"]


def test_the_legends_list_only_this_ticket_s_actors(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    article = _article(snapshot)
    assert _keys(article) == PATH_KEYS
    assert _chart_keys(article) == CHART_KEYS
    assert _keys(_queued(snapshot)) == GROK_KEYS


@needs_node
def test_the_script_legends_list_only_this_ticket_s_actors(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    view = _script_view(tmp_path, snapshot)
    assert _keys(view) == PATH_KEYS
    assert _chart_keys(view) == CHART_KEYS
    assert _keys(_script_view(tmp_path, snapshot, "q-claude")) == GROK_KEYS


GH_CASES = [
    ("issue and PR", {"source": "#563", "pr": "601"},
     ["https://github.com/maxharris1/cleffy/issues/563", "https://github.com/maxharris1/cleffy/pull/601"]),
    ("no PR", {"source": "#563", "pr": None}, ["https://github.com/maxharris1/cleffy/issues/563"]),
    ("linear source", {"source": "ABC-12", "pr": "601"}, ["https://github.com/maxharris1/cleffy/pull/601"]),
    ("malformed repo", {"repo": "maxharris1/cleffy/../x", "source": "#563", "pr": "601"}, []),
]


def _gh_links(markup: str) -> list[str]:
    hero = re.search(r'<section class="jd-hero">.*?</section>', markup, re.S).group(0)
    links = re.findall(r'<a href="(https://github\.com/[^"]*)" rel="noopener noreferrer" target="_blank">', hero)
    assert len(links) == hero.count("https://github.com/")
    return links


@pytest.mark.parametrize(("case", "fields", "hrefs"), GH_CASES, ids=[c[0] for c in GH_CASES])
def test_the_hero_links_the_github_issue_and_pr(tmp_path: Path, case, fields, hrefs) -> None:
    snapshot = _snapshot(tmp_path)
    _review(snapshot).update(fields)
    assert _gh_links(_article(snapshot)) == hrefs, case


@needs_node
@pytest.mark.parametrize(("case", "fields", "hrefs"), GH_CASES, ids=[c[0] for c in GH_CASES])
def test_the_script_hero_links_github_like_the_server(tmp_path: Path, case, fields, hrefs) -> None:
    snapshot = _snapshot(tmp_path)
    _review(snapshot).update(fields)
    assert _gh_links(_run_page(tmp_path, snapshot, "job", JOB, hash_="#job-h-review")["html"]["jobview"]) == hrefs


def test_the_page_carries_the_actions_api_and_no_credential(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _review(snapshot).update(source="#563", pr="601")
    html = render_pages(snapshot)["job.html"]
    for hook in ("/api/session", "/api/signin/poll", "/api/comment", "/api/head", "/api/ok", "X-Factory-CSRF",
                 "Enter only this code on github.com."):
        assert hook in html, hook
    for secret in ("gho_", "ghp_", "github_pat_", "client_secret"):
        assert secret not in html, secret
