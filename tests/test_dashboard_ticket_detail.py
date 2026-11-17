"""#146: the ticket detail page at job.html#job-<id>.

The Run log and the Journey render from one visit list and share one
selection; the Journey names nothing ahead of now and offers "to scale" only
when every duration is known; chart colours come from who did each step;
Can come next lists normal ways on, then human waits, then stops.
"""

import copy
import functools
import html
import http.server
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from lib.dashboard import RECORDS, render_pages, tickets_json
from lib.dashboard_flow import FLOWMAP_TINT, STOP_RED
from lib.dashboard_pages import ticket_detail
from lib.dashboard_pages.common import duration
from lib.dashboard_pages.shell import PX_ACTIONS, PX_STATE
from lib.dashboard_pages.journey_canvas import TINT
from tests import test_dashboard_tickets_board as board
from tests.test_dashboard import NOW
from tests.test_dashboard_activity import HARNESS as POLLING
from tests.test_dashboard_layout import BAR, JOB, _no_script, _run_page, _section, _snapshot, needs_node, run_harness
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


def _serve(tmp_path: Path, sites: dict[str, dict], status_delay: float = 0.0,
           records_gate: threading.Event | None = None) -> http.server.ThreadingHTTPServer:
    for name, snapshot in sites.items():
        site = tmp_path / "site" / name
        site.mkdir(parents=True)
        for page, html in render_pages(snapshot).items():
            (site / page).write_text(html, encoding="utf-8")
        (site / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
        (site / "tickets.json").write_text(tickets_json(snapshot), encoding="utf-8")

    class Handler(QuietHandler):
        def do_GET(self) -> None:
            if self.path.endswith("/status.json"):
                time.sleep(status_delay)
            if records_gate is not None and self.path.endswith("/tickets.json"):
                records_gate.wait(30)
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
console.log(JSON.stringify(JSON.parse(rowsText).map(r => { nowMs += 20000; return selection(r.job, r, m).beatStart; })));
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


# --- #531a: ticket.html#t=<repo>#<ticket>, one live ticket keyed by ticket ------------------------

TICKET_IDS = BAR + ["hud", "ticketview"]
AT_SNAPSHOT = [["advance", -3]]
LIVE = {"running": ("maxharris1/cleffy#40", "h-review"), "queued": ("maxharris1/cleffy#12", "g-grok-c")}
SECTIONS = ["Run log", "Live run", "Journey", "Can come next", "Full state chart"]
AWKWARD = ["a/b", "a#b", "50%", "a b", "!*'()", "Ünï-çødé", "~2"]


def _quote(address: str) -> str:
    return urllib.parse.quote(address, safe="-_.!~*'()")


def _ticket_server(snapshot: dict, address: str | None) -> str:
    view = ticket_detail.ticket_view(snapshot, address)
    assert view.startswith('<div id="ticketview">') and view.endswith("</div>")
    return view[len('<div id="ticketview">'):-len("</div>")]


def _ticket_script(tmp_path: Path, snapshot: dict, hash_: str, actions: list | None = None) -> str:
    out = _run_page(tmp_path, snapshot, "ticket", TICKET_IDS, hash_=hash_, actions=AT_SNAPSHOT + (actions or []))
    return out["html"]["ticketview"]


def _both(tmp_path: Path, snapshot: dict, address: str | None) -> dict[str, str]:
    hash_ = "" if address is None else "#t=" + _quote(address)
    return {"server": _ticket_server(snapshot, address), "script": _ticket_script(tmp_path, snapshot, hash_)}


def _row(snapshot: dict, job: str) -> dict:
    return next(row for row in snapshot["running"] + snapshot["queued"] if row["job"] == job)


def _h1s(markup: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h1\b[^>]*>(.*?)</h1>", markup, re.S)]


def _sections(markup: str) -> list[str]:
    return [t or s for t, s in re.findall(r'<span class="t">([^<]+)</span>|<summary>(Full state chart)', markup)]


def _ticket_tiles(markup: str) -> list[tuple[str, str]]:
    return [(re.sub(r"<span[^>]*>.*$", "", value), label) for value, label in
            re.findall(r'<div class="jd-tile"><b[^>]*>(.*?)</b><span>([^<]*)</span></div>', markup)]


def _tracker(markup: str) -> str:
    return re.search(r'<span class="tracker">([^<]*)</span>', markup).group(1)


@needs_node
def test_the_ticket_page_href_is_the_script_s_and_names_any_ticket(tmp_path: Path) -> None:
    import subprocess
    from lib.dashboard_pages.common import ticket_address, ticket_page_href
    from tests.test_dashboard_layout import NODE
    rows = [{"repo": "maxharris1/cleffy", "ticket_id": tid} for tid in AWKWARD]
    (script,) = re.findall(r"<script>(.*?)</script>", render_pages(_snapshot(tmp_path))["ticket.html"], re.S)
    defs = re.findall(r"^  (function (?:ticketAddress|ticketPageHref)\(.*\}$)", script, re.M)
    assert len(defs) == 2
    (tmp_path / "href.js").write_text("\n".join(defs) + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[2])"
                                      ".map(r => [ticketPageHref(r), ticketAddress(r)])));", encoding="utf-8")
    out = subprocess.run([NODE, str(tmp_path / "href.js"), json.dumps(rows)], capture_output=True, text=True,
                         timeout=30, check=True)
    hrefs = [ticket_page_href(row) for row in rows]
    assert json.loads(out.stdout) == [[href, ticket_address(row)] for href, row in zip(hrefs, rows)]
    assert [urllib.parse.unquote(h.removeprefix("ticket.html#t=")) for h in hrefs] == [
        f"maxharris1/cleffy#{tid}" for tid in AWKWARD]
    assert all(re.fullmatch(r"ticket\.html#t=[A-Za-z0-9%_.!~*'()-]+", h) for h in hrefs)


@needs_node
@pytest.mark.parametrize("tid", AWKWARD)
def test_the_script_opens_the_ticket_its_href_names(tmp_path: Path, tid: str) -> None:
    from lib.dashboard_pages.common import ticket_page_href
    snapshot = _snapshot(tmp_path)
    _review(snapshot)["ticket_id"] = tid
    href = ticket_page_href(_review(snapshot))
    view = _ticket_script(tmp_path, snapshot, href.removeprefix("ticket.html"))
    assert f'data-ticket="{html.escape("maxharris1/cleffy#" + tid)}"' in view
    assert 'id="liverun-job-h-review"' in view


@needs_node
@pytest.mark.parametrize("kind", LIVE)
def test_the_script_draws_the_server_s_ticket(tmp_path: Path, kind: str) -> None:
    views = _both(tmp_path, _snapshot(tmp_path), LIVE[kind][0])
    assert views["script"] == views["server"]


@needs_node
@pytest.mark.parametrize("kind", LIVE)
def test_a_live_ticket_has_the_job_page_s_sections_and_one_h1(tmp_path: Path, kind: str) -> None:
    snapshot = _snapshot(tmp_path)
    address, job = LIVE[kind]
    for render, markup in _both(tmp_path, snapshot, address).items():
        assert re.search(r'^<article class="jobpage ticketpage" data-ticket="maxharris1/cleffy#\d+">', markup), render
        assert _sections(markup) == SECTIONS, render
        assert _h1s(markup) == [f"cleffy#{address.rsplit('#')[1]}"], render
        assert f'id="liverun-job-{job}"' in markup, render
        assert '<nav class="jd-crumb" aria-label="Breadcrumb"><a href="job.html">Tickets</a>' in markup, render


@needs_node
def test_a_running_ticket_says_where_it_runs_and_how_it_beats(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    row = _review(snapshot)
    for render, markup in _both(tmp_path, snapshot, LIVE["running"][0]).items():
        assert _ticket_tiles(markup) == [
            (duration(row["elapsed_seconds"]), "T+ on box"),
            (f"{duration(row['heartbeat_age_seconds'])} ago", "last heartbeat"),
            ("2 / 3", "revise round"),
            ("4 hops", "1 off-chart"),
        ], render
        assert _tracker(markup) == "GitHub PR #40 · on box", render
        assert '<span class="job">started 1:36 AM ET</span></nav>' in markup, render
        assert "data-still" not in _hero_px(markup), render
        assert '<p class="empty">no telemetry</p>' in markup, render
        assert not re.search(r'<span class="(?:kind|device)">', markup), render
        assert "github.com" not in markup, render


@needs_node
def test_a_queued_ticket_says_who_it_waits_for_and_holds_still(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    row = _row(snapshot, "g-grok-c")
    for render, markup in _both(tmp_path, snapshot, LIVE["queued"][0]).items():
        assert _tracker(markup) == f"GitHub PR #12 · queued for grok · {duration(row['age_seconds'])}", render
        assert _hero_px(markup)["data-still"] == "true", render
        assert '<p class="empty">not started yet</p>' in markup, render
        labels = [label for _, label in _ticket_tiles(markup)]
        assert labels[:3] == ["waiting", "last heartbeat", "revise round"] and labels[3].endswith(" off-chart"), render
        assert _ticket_tiles(markup)[:2] == [(duration(row["age_seconds"]), "waiting"),
                                             ("not started", "last heartbeat")], render


def _requeued_after_a_run(tmp_path: Path) -> dict:
    import sqlite3
    from lib.dashboard import collect_snapshot
    from lib.device_pool import DevicePool
    from lib.job_state import requeue_link_drop
    from lib.machines_yml import MachinesConfig
    from lib.queue import SQLiteJobQueue
    from tests.test_dashboard import NOW, add_job, seeded
    from tests.test_dashboard_telemetry import add_telemetry

    db, log = seeded(tmp_path)
    DevicePool(SQLiteJobQueue(db, clock=lambda: NOW), MachinesConfig(devices=()))
    with sqlite3.connect(db) as connection:
        add_job(connection, "r-retry", action="fix", recipient="codex", state="failed", ticket_id="52")
    add_telemetry(db, "r-retry", finished_at=NOW - 30, exit_code=0, exit_reason="done")
    with sqlite3.connect(db) as connection:
        assert requeue_link_drop(connection, "r-retry", now=NOW - 5, reason="SSH link dropped")
    snapshot = collect_snapshot(db, log, now=NOW)
    assert _row(snapshot, "r-retry")["telemetry"]["tokens_in"] == 24763
    return snapshot


@needs_node
def test_a_requeued_ticket_s_live_run_says_not_started_yet_over_the_last_run(tmp_path: Path) -> None:
    snapshot = _requeued_after_a_run(tmp_path)
    views = {**_both(tmp_path, snapshot, "maxharris1/cleffy#52"),
             "job server": _section(_no_script(render_pages(snapshot)["job.html"]), "job-r-retry"),
             "job script": _script_view(tmp_path, snapshot, "r-retry")}
    for render, markup in views.items():
        if render in ("server", "script"):
            assert "queued for codex" in _tracker(markup), render
        live = _section(markup, "liverun-job-r-retry")
        assert live.endswith('<p class="empty">not started yet</p></section>'), render
        assert "24,763" not in live and 'class="lr"' not in live, render


@needs_node
def test_the_queued_tracker_counts_up(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    age = _row(snapshot, "g-grok-c")["age_seconds"]
    later = _ticket_script(tmp_path, snapshot, "#t=" + _quote(LIVE["queued"][0]), [["advance", 7]])
    assert _tracker(later).endswith(f"queued for grok · {duration(age + 7)}")


def test_a_linear_ticket_names_its_tracker_and_pr(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _review(snapshot).update(tracker="linear", ticket_id="AUT-9", source="AUT-9", pr="77")
    assert _tracker(_ticket_server(snapshot, "maxharris1/cleffy#AUT-9")) == "Linear AUT-9 · PR #77 · on box"
    _review(snapshot).update(tracker="github", ticket_id="531", source="#531", pr=None, device=None)
    markup = _ticket_server(snapshot, "maxharris1/cleffy#531")
    assert _tracker(markup) == "GitHub issue #531"
    assert _ticket_tiles(markup)[0][1] == "T+ on this device"


NOT_FOUND = [
    ("unknown", "maxharris1/cleffy#999", "Ticket not found"),
    ("hostile", '<img src=x onerror=y>#"1"', "Ticket not found"),
    ("none", None, "No ticket chosen"),
]


@needs_node
@pytest.mark.parametrize(("case", "address", "title"), NOT_FOUND, ids=[c[0] for c in NOT_FOUND])
def test_without_a_live_ticket_the_page_says_so_and_links_the_board(tmp_path: Path, case, address, title) -> None:
    views = _both(tmp_path, _snapshot(tmp_path), address)
    assert views["script"] == views["server"], case
    markup = views["server"]
    assert markup.startswith('<section class="panel">'), case
    assert _h1s(markup) == [title], case
    assert '<a href="job.html">Tickets board</a>' in markup, case
    assert "jobpage" not in markup and "<img" not in markup, case
    if address:
        assert f"{html.escape(address)} is not a tracked ticket. " in markup, case


@needs_node
@pytest.mark.parametrize("hash_", ["#t=", "#t=%E0%A4%A", "#job-h-review", "#t=maxharris1%2Fcleffy%23999"])
def test_a_hash_that_names_no_live_ticket_draws_a_panel(tmp_path: Path, hash_: str) -> None:
    snapshot = _snapshot(tmp_path)
    view = _ticket_script(tmp_path, snapshot, hash_)
    expected = "Ticket not found" if hash_.endswith("999") else "No ticket chosen"
    assert _h1s(view) == [expected]


def test_the_resolver_prefers_running_then_the_freshest_run_and_an_exact_repo(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    older = {**_review(snapshot), "job": "h-old", "elapsed_seconds": 9000}
    snapshot["running"].insert(0, older)
    snapshot["queued"].append({**_row(snapshot, "q-claude"), "job": "q-40", "ticket_id": "40"})
    found = ticket_detail.resolve_ticket(snapshot, "maxharris1/cleffy#40")
    assert (found["kind"], found["row"]["job"], found["address"]) == ("running", "h-review", "maxharris1/cleffy#40")
    assert found["ticket"]["state"] == "review"
    queued = ticket_detail.resolve_ticket(snapshot, "maxharris1/cleffy#12")
    assert (queued["kind"], queued["row"]["job"]) == ("queued", "g-grok-c")
    loose = ticket_detail.resolve_ticket(snapshot, "MaxHarris1/Cleffy#40")
    assert (loose["kind"], loose["row"]["job"], loose["address"]) == ("running", "h-review", "maxharris1/cleffy#40")
    snapshot["queued"].append({**_row(snapshot, "q-claude"), "job": "q-caps", "repo": "MaxHarris1/Cleffy", "ticket_id": "40"})
    exact = ticket_detail.resolve_ticket(snapshot, "MaxHarris1/Cleffy#40")
    assert (exact["kind"], exact["row"]["job"]) == ("queued", "q-caps")
    assert ticket_detail.resolve_ticket(snapshot, "maxharris1/cleffy#40x") is None
    assert ticket_detail.resolve_ticket(snapshot, "maxharris1/cleffy#4") is None


@needs_node
def test_the_script_resolves_like_the_server(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["running"].insert(0, {**_review(snapshot), "job": "h-old", "elapsed_seconds": 9000})
    loose = _ticket_script(tmp_path, snapshot, "#t=" + _quote("MaxHarris1/Cleffy#40"))
    assert loose == _ticket_server(snapshot, "MaxHarris1/Cleffy#40")
    assert 'data-ticket="maxharris1/cleffy#40"' in loose and 'id="liverun-job-h-review"' in loose
    snapshot["queued"].append({**_row(snapshot, "q-claude"), "job": "q-caps", "repo": "MaxHarris1/Cleffy", "ticket_id": "40"})
    exact = _ticket_script(tmp_path, snapshot, "#t=" + _quote("MaxHarris1/Cleffy#40"))
    assert 'id="liverun-job-q-caps"' in exact


def _emptied(snapshot: dict) -> dict:
    return {**copy.deepcopy(snapshot), "running": [], "queued": [], "tickets": []}


def test_the_served_ticket_page_is_the_no_ticket_panel_for_any_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    pages = {name: render_pages(s)["ticket.html"] for name, s in (("fixture", snapshot), ("empty", _emptied(snapshot)))}
    views = {name: _section(_no_script(page), "ticketview") for name, page in pages.items()}
    assert views["fixture"] == views["empty"]
    assert "No ticket chosen" in views["fixture"] and 'class="jobpage' not in views["fixture"]
    page = pages["fixture"]
    assert 'data-page="ticket"' in page
    assert _h1s(_no_script(page)) == ["No ticket chosen"]
    nav = _section(page, "nav")
    assert "ticket.html" not in nav
    assert re.findall(r'<a href="([^"]+)" aria-current="page">', nav) == ["job.html"]


@needs_node
def test_picking_a_log_row_on_the_ticket_page_selects_its_bar_and_the_reverse(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    hash_ = "#t=" + _quote(LIVE["running"][0])
    row = _ticket_script(tmp_path, snapshot, hash_, [["click", {"attr": {"data-visit": "1"}}]])
    for cls in ("runlog", "journey"):
        assert [i for i, _, p in _rows(row, cls) if p == "true"] == ["1"], cls
    bar = _ticket_script(tmp_path, snapshot, hash_,
                         [["click", {"attr": {"data-visit": "2", "aria-label": "Step 3 of 5: review, codex, 16m 40s"}}]])
    for cls in ("runlog", "journey"):
        assert [i for i, _, p in _rows(bar, cls) if p == "true"] == ["2"], cls


KEEP_PLACE = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  document.querySelector('article.ticketpage .runlog button[data-visit="2"]').focus();
  window.scrollTo(0, 240);
  await sleep(50);
  window.oldTicket = document.querySelector('article.ticketpage');
  return {focused: document.activeElement === document.querySelector('article.ticketpage .runlog button[data-visit="2"]'),
          y: window.scrollY};
})()"""

AFTER_REDRAW = r"""
(() => {
  const now = document.activeElement;
  const out = {redrawn: !window.oldTicket.isConnected, visit: now && now.getAttribute('data-visit'),
               inLog: !!(now && now.closest('.runlog')), y: window.scrollY};
  window.oldTicket = document.querySelector('article.ticketpage');
  return out;
})()"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_a_redraw_keeps_focus_and_scroll_on_the_ticket_page(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    records = threading.Event()
    server = _serve(tmp_path, {"fixture": snapshot}, records_gate=records)
    chrome = _Chrome(tmp_path / "profile")
    try:
        _open(chrome, f"http://127.0.0.1:{server.server_address[1]}/fixture/ticket.html#t={_quote(LIVE['running'][0])}",
              "document.querySelector('article.ticketpage .runlog button')")
        before = chrome.run(KEEP_PLACE)
        records.set()
        chrome.wait_for("!window.oldTicket.isConnected", "the records redraw")
        on_records = chrome.run(AFTER_REDRAW)
        later = {**snapshot, "generated_at_epoch": snapshot["generated_at_epoch"] + 5}
        (tmp_path / "site" / "fixture" / "status.json").write_text(json.dumps(later), encoding="utf-8")
        chrome.wait_for("!window.oldTicket.isConnected", "the status redraw")
        on_status = chrome.run(AFTER_REDRAW)
    finally:
        records.set()
        chrome.close()
        server.shutdown()
    assert before == {"focused": True, "y": 240}
    assert on_records == on_status == {"redrawn": True, "visit": "2", "inLog": True, "y": 240}


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
@pytest.mark.parametrize("kind", ["running", "queued", "idle", "shipped", "stopped"])
def test_the_ticket_page_never_scrolls_sideways_on_a_phone(tmp_path: Path, kind: str) -> None:
    snapshot = _with_shipped(_snapshot(tmp_path))
    _row(snapshot, "g-grok-c").update(source="#5310", pr="5770", round="2/3")
    address = {**{k: a for k, (a, _) in LIVE.items()}, **RECORD}[kind]
    server = _serve(tmp_path, {"fixture": snapshot})
    chrome = _Chrome(tmp_path / "profile")
    try:
        chrome.send("Emulation.setDeviceMetricsOverride", chrome.session, width=375, height=800, deviceScaleFactor=1,
                    mobile=True)
        _open(chrome, f"http://127.0.0.1:{server.server_address[1]}/fixture/ticket.html#t={_quote(address)}",
              f"document.querySelector('article.ticketpage[data-ticket=\"{address}\"] canvas.jd-journey')")
        width = chrome.run("document.documentElement.scrollWidth")
    finally:
        chrome.close()
        server.shutdown()
    assert width == 375


SHIPPED = "maxharris1/cleffy#88"
RECORD = {"idle": "maxharris1/tracy-harris-coaching#5", "stopped": "aut0-code/aut0#AUT-7", "shipped": SHIPPED}
NO_JOB_SECTIONS = {"idle": ["Run log", "Journey", "Can come next", "Full state chart"],
                   "shipped": ["Run log", "Journey", "Full state chart"],
                   "stopped": ["Run log", "Journey", "Full state chart"]}
OPENS = {"maxharris1/cleffy#12": SECTIONS, "maxharris1/cleffy#13": SECTIONS, "maxharris1/cleffy#40": SECTIONS,
         **{address: NO_JOB_SECTIONS[kind] for kind, address in RECORD.items()}}
POLLED = POLLING.replace(
    "    } else if (kind === 'poll') {",
    "    } else if (kind === 'release') {\n"
    "      heldTickets.splice(0).forEach(release => release());\n"
    "      await new Promise(resolve => realSetTimeout(resolve, 50));\n"
    "    } else if (kind === 'count') {\n"
    "      steps.push({tickets: ticketFetches});\n"
    "    } else if (kind === 'poll') {",
)


def _visit(state: str, who: str, at: float, took: float | None, reason: str | None, current: bool = False) -> dict:
    return {"state": state, "who": who, "started_at": at, "duration_seconds": took, "time_known": True,
            "off_chart": False, "reason": reason, "current": current}


def _with_shipped(snapshot: dict) -> dict:
    visits = [_visit("intake", "script", NOW - 9000, 600, None),
              _visit("building", "codex", NOW - 8400, 4800, "plan approved"),
              _visit("merged", "script", NOW - 3600, None, "PR merged", current=True)]
    snapshot[RECORDS][SHIPPED] = {
        "repo": "maxharris1/cleffy", "ticket_id": "88", "state": "merged", "state_age_seconds": 3600, "round": 2,
        "hops": 2, "off_chart_count": 0, "terminal": True, "hops_to_shipped": 0, "all_durations_known": False,
        "visits": visits, "stopped": None, "jobs": [],
        "shipped": {"at": NOW - 3600, "how": "merged", "intake_to_ship_seconds": 5400, "revise_rounds": 2, "jobs": 3,
                    "tokens_in": None, "tokens_out": None, "cost_usd": None},
    }
    snapshot["tickets"].append({**snapshot["tickets"][-1], "repo": "maxharris1/cleffy", "ticket_id": "88", "source": "#88",
                                "pr": "91", "state": "merged", "round": 2})
    return snapshot


def _unloaded(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key != RECORDS}


def _polled(tmp_path: Path, snapshot: dict, address: str, actions: list) -> dict:
    (script,) = re.findall(r"<script>(.*?)</script>", render_pages(snapshot)["ticket.html"], re.S)
    return run_harness(tmp_path, POLLED, script, snapshot, "ticket", TICKET_IDS, actions, "#t=" + _quote(address),
                       tickets_json(snapshot))


def test_the_fixture_tracks_the_tickets_these_tests_open(tmp_path: Path) -> None:
    assert set(_with_shipped(_snapshot(tmp_path))[RECORDS]) == set(OPENS)


@needs_node
@pytest.mark.parametrize("address", OPENS)
def test_every_tracked_ticket_opens_in_both_renders(tmp_path: Path, address: str) -> None:
    views = _both(tmp_path, _with_shipped(_snapshot(tmp_path)), address)
    assert views["script"] == views["server"]
    assert views["server"].startswith(f'<article class="jobpage ticketpage" data-ticket="{html.escape(address)}">')
    assert _sections(views["server"]) == OPENS[address]


def _board_links(markup: dict[str, str]) -> dict[str, str]:
    cards = {card["attrs"]["data-ticket"]: board._id_link(card)["attrs"].get("href")
             for card in board._all(board._dom(markup["pipeline"]), board._cls("tk-card"))}
    tiles = {tile["attrs"]["data-ticket"]: tile["attrs"].get("href")
             for tile in board._all(board._dom(markup["shipped"]), board._cls("tk-tile"))}
    assert cards and tiles
    return {**cards, **tiles}


def _check_board_links_open(links: dict[str, str], rows: list[dict], open_page) -> None:
    keys = {board._key(row): row for row in rows}
    assert sorted(links) == sorted(keys)
    assert {row["state"] for row in rows} >= {"capped", "post-merge", "merged", "intake"}, \
        "a capped card, a post-merge card, a shipped tile and an idle card with no job"
    for key, row in keys.items():
        href = links[key]
        assert href and href.startswith("ticket.html#t="), key
        hash_ = href[len("ticket.html"):]
        markup = open_page(urllib.parse.unquote(hash_[len("#t="):]), hash_)
        assert markup.startswith(f'<article class="jobpage ticketpage" data-ticket="{html.escape(key)}">'), href
        (title,) = _h1s(markup)
        assert title != "Ticket not found" and str(row["ticket_id"]) in title, href


def test_every_board_card_and_shelf_tile_opens_its_ticket(tmp_path: Path) -> None:
    snapshot = board.board_snapshot(tmp_path)
    _check_board_links_open(_board_links(board._server(snapshot)), snapshot["tickets"],
                            lambda address, hash_: _ticket_server(snapshot, address))


@needs_node
def test_script_board_cards_and_shelf_tiles_open_their_ticket(tmp_path: Path) -> None:
    snapshot = board.board_snapshot(tmp_path)
    _check_board_links_open(_board_links(board._script(tmp_path, snapshot)["html"]), snapshot["tickets"],
                            lambda address, hash_: _ticket_script(tmp_path, snapshot, hash_))


@needs_node
def test_a_record_opens_by_its_own_key_and_a_shared_address_by_the_first_key(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    idle = RECORD["idle"]
    twin = idle + "~2"
    snapshot[RECORDS][twin] = {**copy.deepcopy(snapshot[RECORDS][RECORD["stopped"]]),
                               "repo": "maxharris1/tracy-harris-coaching", "ticket_id": "5"}
    for address, key, kind in ((twin, twin, "stopped"), (idle, idle, "idle"),
                               ("MaxHarris1/Tracy-Harris-Coaching#5", idle, "idle")):
        views = _both(tmp_path, snapshot, address)
        assert views["script"] == views["server"], address
        assert f'data-ticket="{key}"' in views["server"], address
        assert _sections(views["server"]) == NO_JOB_SECTIONS[kind], address


@needs_node
def test_a_no_job_ticket_shows_a_loading_panel_until_its_record_arrives(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    address = RECORD["idle"]
    out = _polled(tmp_path, snapshot, address, [["hold"], ["snap", ["ticketview"]], ["release"]])
    loading = out["steps"][0]["snap"]["ticketview"]
    assert loading == _ticket_server(_unloaded(snapshot), address)
    assert loading.startswith('<section class="panel">')
    assert _h1s(loading) == ["Loading ticket"]
    assert f"Loading ticket {address}… " in loading
    assert out["html"]["ticketview"] == _ticket_server(snapshot, address)
    assert out["html"]["ticketview"].startswith(f'<article class="jobpage ticketpage" data-ticket="{address}">')


@needs_node
def test_a_live_ticket_draws_at_once_while_the_records_load(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    address = LIVE["running"][0]
    out = _polled(tmp_path, snapshot, address, [["hold"], ["count"]])
    assert out["steps"] == [{"tickets": 1}]
    assert out["html"]["ticketview"] == _ticket_server(_unloaded(snapshot), address) == _ticket_server(snapshot, address)


@needs_node
def test_the_ticket_page_fetches_tickets_json_once_per_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    later = {**snapshot, "generated_at_epoch": snapshot["generated_at_epoch"] + 5}
    polls = [["count"], ["poll", snapshot], ["count"], ["poll", later], ["count"]]
    held = [["hold"], ["poll", later], ["count"], ["release"], ["count"]]
    counts = {name: [step["tickets"] for step in _polled(tmp_path, snapshot, RECORD["idle"], actions)["steps"]]
              for name, actions in (("polls", polls), ("held", held))}
    assert counts == {"polls": [1, 1, 2], "held": [1, 2]}


@needs_node
@pytest.mark.parametrize("address", [RECORD["idle"], "maxharris1/cleffy#404"])
def test_records_behind_a_status_only_push_are_fetched_once_per_snapshot(tmp_path: Path, address: str) -> None:
    snapshot = _snapshot(tmp_path)
    ahead = {**_unloaded(snapshot), "generated_at_epoch": snapshot["generated_at_epoch"] + 5}
    (script,) = re.findall(r"<script>(.*?)</script>", render_pages(snapshot)["ticket.html"], re.S)
    actions = [["poll", ahead], ["poll", ahead], ["poll", ahead], ["count"]]
    out = run_harness(tmp_path, POLLED, script, ahead, "ticket", TICKET_IDS, actions, "#t=" + _quote(address),
                      tickets_json(snapshot))
    assert out["steps"] == [{"tickets": 1}]
    assert out["html"]["ticketview"] == _ticket_server(snapshot, address)
SKEWED = (POLLED.replace("JSON.parse(ticketsText)", "JSON.parse(publishedTickets)")
          .replace("let ticketFetches = 0;", "let ticketFetches = 0;\nlet publishedTickets = ticketsText;")
          .replace("    } else if (kind === 'poll') {",
                   "    } else if (kind === 'publish') {\n      publishedTickets = arg;\n    } else if (kind === 'poll') {"))


@needs_node
def test_records_older_than_the_status_load_again_on_each_poll_until_the_ticket_arrives(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    address = RECORD["idle"]
    status = _unloaded(snapshot)
    older = {**snapshot, "generated_at_epoch": snapshot["generated_at_epoch"] - 5,
             RECORDS: {key: record for key, record in snapshot[RECORDS].items() if key != address}}
    (script,) = re.findall(r"<script>(.*?)</script>", render_pages(snapshot)["ticket.html"], re.S)

    def run(target: str, actions: list) -> dict:
        return run_harness(tmp_path / str(len(actions)), SKEWED, script, status, "ticket", TICKET_IDS, actions,
                           "#t=" + _quote(target), tickets_json(older))

    out = run(address, [["snap", ["ticketview"]], ["count"], ["poll", status], ["snap", ["ticketview"]], ["count"],
                        ["publish", tickets_json(snapshot)], ["poll", status], ["count"], ["poll", status], ["count"]])
    loading = _ticket_server(status, address)
    assert _h1s(loading) == ["Loading ticket"]
    assert [step["snap"]["ticketview"] for step in out["steps"] if "snap" in step] == [loading, loading]
    assert [step["tickets"] for step in out["steps"] if "tickets" in step] == [1, 2, 3, 3]
    assert out["html"]["ticketview"] == _ticket_server(snapshot, address)

    untracked = "owner/nowhere#1"
    out = run(untracked, [["poll", status], ["poll", status], ["count"]])
    assert out["steps"] == [{"tickets": 1}]
    assert _h1s(out["html"]["ticketview"]) == ["Ticket not found"]
    assert out["html"]["ticketview"] == _ticket_server(snapshot, untracked)


NO_JOB = {
    "idle": (lambda record: None, ("static", "#ff9e64"),
             [("no reading", "in waiting-max"), ("0", "revise round"), ("1 hops", "1 off-chart")]),
    "idle elsewhere": (lambda record: (record.update(state="building", state_age_seconds=4000),
                                       record["visits"][-1].update(state="building")),
                       (PX_STATE["held"]["effect"], PX_STATE["held"]["tint"]),
                       [("1h 06m", "in building"), ("0", "revise round"), ("1 hops", "1 off-chart")]),
}


@needs_node
@pytest.mark.parametrize("case", NO_JOB)
def test_a_ticket_with_no_job_holds_still_and_leads_with_its_time_in_state(tmp_path: Path, case: str) -> None:
    change, strip, tiles = NO_JOB[case]
    snapshot = _snapshot(tmp_path)
    change(snapshot[RECORDS][RECORD["idle"]])
    for render, markup in _both(tmp_path, snapshot, RECORD["idle"]).items():
        assert _sections(markup) == NO_JOB_SECTIONS["idle"], render
        assert 'id="liverun-' not in markup, render
        px = _hero_px(markup)
        assert (px["data-px"], px["data-tint"], px["data-still"]) == (*strip, "true"), render
        assert _ticket_tiles(markup) == tiles, render
        assert _h1s(markup) == ["tracy-harris-coaching#5"], render
        assert _tracker(markup) == "GitHub PR #5", render
        assert 'class="bot ' not in markup, render


def _notes(markup: str) -> list[str]:
    log = re.search(r'<ol class="runlog">(.*?)</ol>', markup, re.S).group(1)
    return re.findall(r'<span class="note">([^<]*)</span>', log)


ENDED = {
    "shipped": (lambda record: None, {"data-px": "off", "data-done": "true"},
                [("1h 00m", "in merged"), ("2", "revise round"), ("2 hops", "0 off-chart")],
                ["ticket arrives", "plan approved", "PR merged"]),
    "stopped": (lambda record: None, {"data-px": PX_STATE["stuck"]["effect"], "data-tint": PX_STATE["stuck"]["tint"]},
                [("no reading", "stopped"), ("0", "revise round"), ("1 hops", "1 off-chart")],
                ["ticket arrives", "no direct transition on the chart"]),
    "stopped at a known time": (lambda record: record["stopped"].update(at=NOW - 5400), {},
                                [("1h 30m", "stopped"), ("0", "revise round"), ("1 hops", "1 off-chart")],
                                ["ticket arrives", "no direct transition on the chart"]),
    "stopped with no round": (lambda record: record.update(round=None), {},
                              [("no reading", "stopped"), ("no round", "revise round"), ("1 hops", "1 off-chart")],
                              ["ticket arrives", "no direct transition on the chart"]),
}


@needs_node
@pytest.mark.parametrize("case", ENDED)
def test_a_shipped_or_stopped_ticket_draws_its_record_without_live_run_or_next(tmp_path: Path, case: str) -> None:
    change, strip, tiles, notes = ENDED[case]
    address = RECORD[case.split()[0]]
    snapshot = _with_shipped(_snapshot(tmp_path))
    record = snapshot[RECORDS][address]
    change(record)
    for render, markup in _both(tmp_path, snapshot, address).items():
        assert _sections(markup) == ["Run log", "Journey", "Full state chart"], render
        assert [state for _, state, _ in _rows(markup, "runlog")] == [v["state"] for v in record["visits"]], render
        assert _notes(markup) == notes, render
        assert _ticket_tiles(markup) == tiles, render
        px = _hero_px(markup)
        assert {k: px.get(k) for k in strip} == strip and px["data-still"] == "true", render


STILL_TICKET = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  document.querySelector('details.jd-chart').open = true;
  await sleep(3300);
  const shot = () => [...document.querySelectorAll('.jd-hero canvas.px, canvas.jd-journey, canvas.jd-flowmap')]
    .map(c => c.toDataURL());
  const a = shot();
  await sleep(500);
  const b = shot();
  return a.length === 3 && a.every((x, i) => x === b[i]);
})()"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_a_ticket_without_a_job_holds_its_hero_strip_and_journey_still(tmp_path: Path) -> None:
    cases = {"running": LIVE["running"][0], **RECORD}
    for name in cases:
        (tmp_path / name).mkdir()
    server = _serve(tmp_path, {name: _with_shipped(_snapshot(tmp_path / name)) for name in cases})
    chrome = _Chrome(tmp_path / "profile")
    still = {}
    try:
        for name, address in cases.items():
            _open(chrome, f"http://127.0.0.1:{server.server_address[1]}/{name}/ticket.html#t={_quote(address)}",
                  _journey_drawn("article.ticketpage"))
            still[name] = chrome.run(STILL_TICKET)
    finally:
        chrome.close()
        server.shutdown()
    assert still == {"running": False, "idle": True, "stopped": True, "shipped": True}
