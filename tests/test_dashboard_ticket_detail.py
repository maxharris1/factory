"""#146: the ticket detail page at ticket.html#t=<repo>#<ticket>.

The Run log and the Journey render from one visit list and share one
selection; the Journey names nothing ahead of now and offers "to scale" only
when every duration is known; chart colours come from who did each step;
Can come next lists normal ways on, then human waits, then stops.
"""

import copy
import functools
import hashlib
import html
import http.server
import json
import re
import sqlite3
import threading
import time
import urllib.parse
from contextlib import closing
from pathlib import Path

import pytest

from lib.dashboard import RECORDS, render_pages, tickets_json
from lib.dashboard_flow import FLOWMAP_TINT, STOP_RED, JobStep, Transition, job_flow, load_chart
from lib.dashboard_pages import ticket_detail
from lib.exposure import Exposure
from lib.dashboard_pages.common import duration, ticket_page_href
from lib.dashboard_pages.shell import PX_ACTIONS, PX_STATE
from lib.dashboard_pages.journey_canvas import TINT
from tests import test_dashboard_attention_actions as attention
from tests import test_dashboard_tickets_board as board
from tests.test_dashboard import NOW
from tests.test_dashboard_activity import HARNESS as POLLING
from tests.test_dashboard_layout import BAR, HARNESS, _no_script, _run_page, _section, _snapshot, needs_node, run_harness
from tests.test_dashboard_system import CHROME, QuietHandler, _Chrome, publish_assets

WHO = {"script", "claude", "codex", "cursor", "gemini", "grok", "human"}


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
    return _ticket_server(snapshot, LIVE["running"][0])


def _journey(article: str) -> str:
    return re.search(r'<section class="jd-panel"[^>]*><h2><span class="t">Journey</span>.*?</section>', article, re.S).group(0)


def test_run_log_and_journey_render_from_one_list(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    path = _review(snapshot)["flow"]["path"]
    article = _article(snapshot)
    log, bars = _rows(article, "runlog"), _rows(article, "journey")
    assert len(log) == len(bars) == len(path)
    assert [state for _, state, _ in log] == [state for _, state, _ in bars] == path
    assert [i for i, _, _ in log] == [i for i, _, _ in bars] == [str(i) for i in range(len(path))]


def test_default_selection_is_the_current_visit(tmp_path: Path) -> None:
    article = _article(_snapshot(tmp_path))
    for cls in ("runlog", "journey"):
        assert [i for i, _, pressed in _rows(article, cls) if pressed == "true"] == ["4"], cls


@needs_node
def test_script_renders_the_same_one_list_and_selection(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    server = _article(snapshot)
    view = _script_view(tmp_path, snapshot)
    for cls in ("runlog", "journey"):
        assert _rows(view, cls) == _rows(server, cls), cls


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
    assert not re.search(r"--g:var\(--(?:codex|cursor|claude|gemini|grok)\)", css)


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
    view = _script_view(tmp_path, snapshot)
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
    view = _script_view(tmp_path, snapshot)
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
    publish_assets(tmp_path / "site", *sites.values())

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


REVIEW = 'article.ticketpage[data-ticket="maxharris1/cleffy#40"]'
CURSOR = 'article.ticketpage[data-ticket="maxharris1/cleffy#14"]'


def _ticket_url(server: http.server.ThreadingHTTPServer, site: str, address: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/{site}/ticket.html#t={_quote(address)}"


def _to_ticket(chrome: _Chrome, address: str) -> None:
    chrome.run(f"location.hash = '#t={_quote(address)}'")


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
    snapshot = _snapshot(tmp_path)
    server = _serve(tmp_path, {"lanes": snapshot}, status_delay)
    chrome = _Chrome(tmp_path / "profile")
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/lanes/"
        tile = f'a[href="{ticket_page_href(_review(snapshot))}"]'
        _open(chrome, base + "index.html", f"document.querySelector('{tile}')")
        chrome.run(f"document.querySelector('{tile}').click()")
        from_lanes = _drawn(chrome, REVIEW)
        _to_ticket(chrome, "maxharris1/cleffy#14")
        after_hashchange = _drawn(chrome, CURSOR)
        _open(chrome, base + "job.html#job-h-review", f"document.querySelector('{REVIEW}')")
        from_old_link = _drawn(chrome, REVIEW)
    finally:
        chrome.close()
        server.shutdown()
    drawn = {"journey": True, "buttons": True, "chart": True}
    assert {"lanes tile": from_lanes, "hash change": after_hashchange, "old job link": from_old_link} == {
        "lanes tile": drawn, "hash change": drawn, "old job link": drawn}


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
            _open(chrome, _ticket_url(server, name, "maxharris1/cleffy#40"), _journey_drawn(REVIEW))
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
    caption = _caption(_script_view(tmp_path, snapshot,
                                    actions=[["click", {"attr": {"data-visit": "2", "data-state": visit["state"]}}]]))
    total = len(_review(snapshot)["flow"]["visits"])
    assert f"|03 of {total}|" in caption, caption
    assert f"|{visit['state']}|" in caption and f"|{visit['who']}|" in caption, caption
    if visit["reason"]:
        assert visit["reason"] in caption, caption
    from lib.dashboard_pages.common import duration
    age = _review(snapshot)["flow"]["state_age_seconds"]
    ticked = _caption(_article(snapshot)).replace(f"|{duration(age)} so far|", f"|{duration(age + 3)} so far|")
    assert _caption(_script_view(tmp_path, snapshot)) == ticked


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
        _open(chrome, _ticket_url(server, "fixture", "maxharris1/cleffy#40"), _journey_drawn(REVIEW))
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
    pressed = "[...document.querySelectorAll('article.ticketpage [data-scale]')].map(b => b.dataset.scale + '=' + b.getAttribute('aria-pressed'))"
    try:
        _open(chrome, _ticket_url(server, "timed", "maxharris1/cleffy#40"), _journey_drawn(REVIEW))
        chrome.run("document.querySelector('article.ticketpage [data-scale=\"scale\"]').click()")
        _to_ticket(chrome, "maxharris1/cleffy#14")
        _wait(chrome, f"document.querySelector('{CURSOR} canvas.jd-journey')")
        _to_ticket(chrome, "maxharris1/cleffy#40")
        _wait(chrome, f"document.querySelector('{REVIEW} [data-scale]')")
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
        f"(() => {{const r = document.querySelector('article.ticketpage .jd-journey').getBoundingClientRect();"
        f"return {{x: r.x + {x}, y: r.y + {y}}}}})()"
    )
    for kind in ("mousePressed", "mouseReleased"):
        chrome.send("Input.dispatchMouseEvent", chrome.session, type=kind, button="left", clickCount=1, **point)


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_under_reduced_motion_a_scale_change_repaints_the_bars_it_hit_tests(tmp_path: Path) -> None:
    server = _serve(tmp_path, {"timed": _fully_timed(_snapshot(tmp_path))})
    chrome = _Chrome(tmp_path / "profile")
    canvas = "document.querySelector('article.ticketpage .jd-journey').toDataURL()"
    try:
        chrome.send("Emulation.setEmulatedMedia", chrome.session,
                    features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
        _open(chrome, _ticket_url(server, "timed", "maxharris1/cleffy#40"), _journey_drawn(REVIEW))
        even = chrome.run(canvas)
        chrome.run("document.querySelector('article.ticketpage [data-scale=scale]').click()")
        time.sleep(0.2)
        clicked = chrome.run(canvas)
        _to_ticket(chrome, "maxharris1/cleffy#14")
        _wait(chrome, f"document.querySelector('{CURSOR} canvas.jd-journey')")
        _to_ticket(chrome, "maxharris1/cleffy#40")
        _wait(chrome, f"document.querySelector('{REVIEW} [data-scale=scale][aria-pressed=true]')")
        time.sleep(0.4)
        rebuilt = chrome.run(canvas)
        _click_canvas(chrome, 336, 140)
        selected = chrome.run("(b => [b.dataset.visit, b.dataset.state])(document.querySelector('article.ticketpage .runlog [aria-pressed=true]'))")
        chrome.run("document.querySelector('article.ticketpage details.jd-chart').open = true")
        time.sleep(0.2)
        chart = chrome.run(f"{PAINTED}('article.ticketpage canvas.jd-flowmap')")
    finally:
        chrome.close()
        server.shutdown()
    assert {"scale drawn": clicked != even, "same as a fresh to-scale paint": clicked == rebuilt,
            "review drawn at x=336 to scale": selected, "chart painted": chart > 0} == {
        "scale drawn": True, "same as a fresh to-scale paint": True,
        "review drawn at x=336 to scale": ["2", "review"], "chart painted": True}


CONTROL_STRIP = r"""
(() => {
  const ctl = document.querySelector('article.ticketpage .jd-ctl');
  const look = b => {const c = getComputedStyle(b); return [b.offsetHeight, c.backgroundColor, c.color, c.borderTopColor];};
  return {
    axis: ctl.querySelector('[role=group]').getAttribute('aria-label'),
    scales: [...ctl.querySelectorAll('[role=group] [data-scale]')].map(b => [b.textContent, b.getAttribute('aria-pressed'), ...look(b)]),
    replay: look(ctl.querySelector('[data-replay]')),
    keys: [...ctl.querySelectorAll('.key')].map(k => k.textContent),
    separateLegend: !!document.querySelector('article.ticketpage .jd-panel .jd-legend'),
  };
})()"""

OFF = ["rgb(31, 34, 48)", "rgb(192, 202, 245)", "rgb(65, 72, 104)"]
ON = ["rgba(158, 206, 106, 0.09)", "rgb(158, 206, 106)", "rgb(158, 206, 106)"]


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_the_journey_control_strip_matches_journey_climb(tmp_path: Path) -> None:
    server = _serve(tmp_path, {"timed": _fully_timed(_snapshot(tmp_path))})
    chrome = _Chrome(tmp_path / "profile")
    try:
        _open(chrome, _ticket_url(server, "timed", "maxharris1/cleffy#40"),
              f"document.querySelector('{REVIEW} .jd-ctl')")
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
    assert re.findall(r'<span class="key[^"]*"><i[^>]*></i>([^<]+)</span>', strip) == PATH_KEYS
    assert "jd-legend" not in journey


def _queued(snapshot: dict) -> str:
    return _ticket_server(snapshot, LIVE["queued"][0])


def _script_view(tmp_path: Path, snapshot: dict, address: str = "maxharris1/cleffy#40",
                 actions: list | None = None) -> str:
    out = _run_page(tmp_path, snapshot, "ticket", TICKET_IDS, hash_="#t=" + _quote(address), actions=actions)
    return out["html"]["ticketview"]


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
    assert _beat_class(_script_view(tmp_path, _snapshot(tmp_path), LIVE["queued"][0])) == ""


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
    ("runner-gone", _set(job_state="stale", stale_cause="runner-gone"), "runner gone, worker alive"),
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


def test_a_queued_job_s_live_run_says_not_started_yet(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    queued = _queued(snapshot)
    assert '<p class="empty">not started yet</p>' in queued
    assert "no telemetry" not in queued
    assert '<p class="empty">no telemetry</p>' in _article(snapshot)


@needs_node
def test_the_script_says_a_queued_job_has_not_started_yet(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    queued = _script_view(tmp_path, snapshot, LIVE["queued"][0])
    assert '<p class="empty">not started yet</p>' in queued and "no telemetry" not in queued
    assert '<p class="empty">no telemetry</p>' in _script_view(tmp_path, snapshot)


def _keys(markup: str) -> list[str]:
    return re.findall(r'<span class="key[^"]*"><i[^>]*></i>([^<]+)</span>', markup)


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
    assert _keys(_script_view(tmp_path, snapshot, LIVE["queued"][0])) == GROK_KEYS


def test_the_page_carries_the_actions_api_and_no_credential(tmp_path: Path) -> None:
    from lib.dashboard_page import page_script
    snapshot = _snapshot(tmp_path)
    _review(snapshot).update(source="#563", pr="601")
    html = render_pages(snapshot)["job.html"]
    script = page_script(snapshot.get("statechart"))
    assert '<script src="/app.js" defer></script>' in html
    for hook in ("/api/session", "/api/signin/poll", "/api/comment", "/api/head", "/api/ok", "X-Factory-CSRF",
                 "Enter only this code on github.com."):
        assert hook in script, hook
    for secret in ("gho_", "ghp_", "github_pat_", "client_secret"):
        assert secret not in html and secret not in script, secret


HELD = "maxharris1/tracy-harris-coaching#5"
HOLD_OK = f'document.querySelector(\'article.ticketpage[data-ticket="{HELD}"] .jd-gh\')'


@attention.needs_chrome
def test_the_hold_ok_shows_the_whole_head_it_pins_and_the_whole_new_head(tmp_path: Path) -> None:
    old, new = "a" * 40, "b" * 40
    snapshot = _snapshot(tmp_path)
    held = next(t for t in snapshot["tickets"] if f"{t['repo']}#{t['ticket_id']}" == HELD)
    held.update(state="awaiting-ok", waiting_on="maxharris1")
    replies = {"/api/head": {"status": 200, "body": {"head": old}},
               "/api/ok": {"status": 409, "body": {"error": "head-changed", "head": new}}}
    stub = attention.STUB.replace("__SESSION__", json.dumps(attention.SIGNED_IN)).replace("__REPLIES__", json.dumps(replies))
    server = _serve(tmp_path, {"site": snapshot})
    chrome = _Chrome(tmp_path / "profile")
    try:
        chrome.send("Page.enable", chrome.session)
        chrome.send("Page.addScriptToEvaluateOnNewDocument", chrome.session, source=stub)
        _open(chrome, _ticket_url(server, "site", HELD), f"{HOLD_OK} && !{HOLD_OK}.querySelector('.gh-okbox').hidden")
        chrome.run(f"{HOLD_OK}.querySelector('.gh-okbox').open = true")
        chrome.wait_for(f"!{HOLD_OK}.querySelector('.gh-ok').disabled", "the head")
        shown = chrome.run(f"{HOLD_OK}.innerText")
        chrome.run(f"{HOLD_OK}.querySelector('.gh-ok').click()")
        chrome.wait_for(f"/head changed/.test({HOLD_OK}.querySelector('.gh-okbox .gh-out').textContent)", "the new head")
        changed = chrome.run(f"{HOLD_OK}.innerText")
        posts = chrome.run("window.posts")
    finally:
        chrome.close()
        server.shutdown()
    assert old in shown
    assert new in changed
    assert {"url": "/api/ok", "csrf": "x", "body": {"repo": "maxharris1/tracy-harris-coaching", "pr": 5, "head": old}} in posts


# --- #531a: ticket.html#t=<repo>#<ticket>, one live ticket keyed by ticket ------------------------

TICKET_IDS = BAR + ["hud", "ticketview"]
AT_SNAPSHOT = [["advance", -3]]
LIVE = {"running": ("maxharris1/cleffy#40", "h-review"), "queued": ("maxharris1/cleffy#12", "g-grok-c")}
SECTIONS = ["Live run", "Journey", "Can come next", "Run log", "Full state chart"]
RUNNING_SECTIONS = ["Live run", "Journey", "Can come next", "Run log", "Exposure", "Full state chart"]
LIVE_SECTIONS = {"running": RUNNING_SECTIONS, "queued": SECTIONS}
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
    from lib.dashboard_page import page_script
    snap = _snapshot(tmp_path)
    assert '<script src="/app.js" defer></script>' in render_pages(snap)["ticket.html"]
    script = page_script(snap.get("statechart"))
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
        assert _sections(markup) == LIVE_SECTIONS[kind], render
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
    for render, markup in _both(tmp_path, snapshot, "maxharris1/cleffy#52").items():
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
  document.querySelector('article.ticketpage details.jd-runlog > summary').click();
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
    if kind == "stopped":
        _stopped(snapshot, reason="superseded-by-sanctioned-intake/" * 30)
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
NO_JOB_SECTIONS = {"idle": ["Journey", "Can come next", "Run log", "Full state chart"],
                   "shipped": ["Run summary", "Journey", "Run log", "Ended", "Full state chart"],
                   "stopped": ["Why it stopped", "Journey", "Run log", "Full state chart"]}
OPENS = {"maxharris1/cleffy#12": SECTIONS, "maxharris1/cleffy#13": RUNNING_SECTIONS,
         "maxharris1/cleffy#40": RUNNING_SECTIONS,
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
    from lib.dashboard_page import page_script
    assert '<script src="/app.js" defer></script>' in render_pages(snapshot)["ticket.html"]
    script = page_script(snapshot.get("statechart"))
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
def test_a_running_ticket_opened_by_its_record_key_stays_live_when_its_repo_is_cut_short(tmp_path: Path) -> None:
    import sqlite3
    from lib.dashboard import collect_snapshot
    from tests.test_dashboard import add_job, add_ticket, seeded
    db, log = seeded(tmp_path / "db")
    repo = "owner/" + "a" * 95
    with sqlite3.connect(db) as connection:
        add_ticket(connection, repo, "7")
        add_job(connection, "r-long", state="running", repo=repo, ticket_id="7")
    snapshot = collect_snapshot(db, log, now=NOW)
    key = f"{repo}#7"
    assert key in snapshot[RECORDS] and _row(snapshot, "r-long")["repo"] != repo
    views = _both(tmp_path, snapshot, key)
    assert views["script"] == views["server"]
    assert ticket_detail.resolve_ticket(snapshot, key, snapshot[RECORDS])["kind"] == "running"
    assert views["server"].startswith(f'<article class="jobpage ticketpage" data-ticket="{key}">')
    assert _sections(views["server"]) == RUNNING_SECTIONS


@needs_node
def test_two_live_jobs_whose_shown_ids_collide_open_their_own_records(tmp_path: Path) -> None:
    import sqlite3
    from lib.dashboard import collect_snapshot
    from tests.test_dashboard import add_job, add_ticket, seeded
    db, log = seeded(tmp_path / "db")
    repos = ["owner/" + "a" * 95 + suffix for suffix in ("A", "B")]
    with sqlite3.connect(db) as connection:
        for index, (repo, action, age) in enumerate(zip(repos, ("build", "review"), (600, 20)), 1):
            add_ticket(connection, repo, "7")
            add_job(connection, f"1234567800004000800000000000000{index}", state="running", repo=repo,
                    ticket_id="7", action=action, created_at=NOW - age)
    snapshot = collect_snapshot(db, log, now=NOW)
    for repo, action, elapsed, other in ((repos[0], "build", 600, "review"), (repos[1], "review", 20, "build")):
        key = f"{repo}#7"
        found = ticket_detail.resolve_ticket(snapshot, key, snapshot[RECORDS])
        assert (found["row"]["action"], found["row"]["elapsed_seconds"]) == (action, elapsed), key
        views = _both(tmp_path, snapshot, key)
        assert views["script"] == views["server"], key
        assert f'<span class="phase">{action.upper()}</span>' in views["server"], key
        assert f'<span class="phase">{other.upper()}</span>' not in views["server"], key
    found = ticket_detail.resolve_ticket(snapshot, f"{repos[0]}#7", None)
    assert (found["row"]["action"], found["row"]["elapsed_seconds"]) == ("build", 600)


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
    from lib.dashboard_page import page_script
    assert '<script src="/app.js" defer></script>' in render_pages(snapshot)["ticket.html"]
    script = page_script(snapshot.get("statechart"))
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
    from lib.dashboard_page import page_script
    assert '<script src="/app.js" defer></script>' in render_pages(snapshot)["ticket.html"]
    script = page_script(snapshot.get("statechart"))

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
    "shipped": (lambda record: None, {"data-px": "off", "data-done": "true", "data-tint": "#9ece6a"},
                [("1h 30m", "total, intake to merge"), ("2", "revise rounds"), ("2 hops", "0 off-chart")],
                ["ticket arrives", "plan approved", "PR merged"]),
    "stopped": (lambda record: None, {"data-px": "static", "data-tint": "#f7768e"},
                [("no reading", "time stopped"), ("0 hops", "before it stopped"), ("none", "revise round")],
                ["ticket arrives", "no direct transition on the chart"]),
    "stopped at a known time": (lambda record: record["stopped"].update(at=NOW - 5400), {},
                                [("1h 30m", "time stopped"), ("0 hops", "before it stopped"), ("none", "revise round")],
                                ["ticket arrives", "no direct transition on the chart"]),
    "stopped with no round": (lambda record: record.update(round=None), {},
                              [("no reading", "time stopped"), ("0 hops", "before it stopped"), ("no round", "revise round")],
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
        assert _sections(markup) == NO_JOB_SECTIONS[case.split()[0]], render
        assert [state for _, state, _ in _rows(markup, "runlog")] == [v["state"] for v in record["visits"]], render
        assert _notes(markup) == notes, render
        assert _ticket_tiles(markup) == tiles, render
        px = _hero_px(markup)
        assert {k: px.get(k) for k in strip} == strip and px["data-still"] == "true", render



def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def _panel(markup: str, title: str) -> str:
    return re.search(rf'<section class="jd-panel[^"]*"(?: data-sec="[a-z]+")?><h2><span class="t">{title}</span>.*?</section>', markup, re.S).group(0)


def _count(panel: str) -> str:
    return _text(re.search(r'<span class="count">(.*?)</span>', panel, re.S).group(1))


HOW = {"merged": ("merged", "merge"), "handed-off": ("handed off", "hand-off")}
RUN = [("intake", "script", 25), ("building", "claude", 1870), ("review", "codex", 425), ("fixing", "claude", 750),
       ("review", "codex", 400), ("fixing", "claude", 555), ("merged", "script", None)]
INTAKE_AT = NOW - 3600 - sum(took or 0 for _, _, took in RUN)


def _seeded_run(snapshot: dict) -> dict:
    record = snapshot[RECORDS][SHIPPED]
    at, visits = INTAKE_AT, []
    for i, (state, who, took) in enumerate(RUN):
        visits.append(_visit(state, who, at, took, None if i == 0 else f"reason {i}", current=took is None))
        at += took or 0
    record.update(visits=visits, hops=len(RUN) - 1)
    record["shipped"].update(jobs=11, tokens_in=6_900_000, tokens_out=96_000, cost_usd=4.18,
                             intake_to_ship_seconds=NOW - 3600 - INTAKE_AT)
    return record


@needs_node
@pytest.mark.parametrize("how", HOW)
def test_a_shipped_ticket_s_hero_is_settled_green_with_its_line_and_tiles(tmp_path: Path, how: str) -> None:
    word, noun = HOW[how]
    snapshot = _with_shipped(_snapshot(tmp_path))
    snapshot[RECORDS][SHIPPED]["shipped"].update(how=how, jobs=11, cost_usd=4.18)
    intake, shipped = ticket_detail._et_clock(NOW - 9000), ticket_detail._et_clock(NOW - 3600)
    views = _both(tmp_path, snapshot, SHIPPED)
    assert views["script"] == views["server"]
    for render, markup in views.items():
        hero = re.search(r'<section class="jd-hero">.*?</section>', markup, re.S).group(0)
        assert f'<h1>cleffy#88</h1><span class="chip ok">{word}</span>' in hero, render
        line = re.search(r'<p class="line">(.*?)</p>', hero, re.S).group(1)
        assert line.startswith(f'<b class="ok">{word}</b> '), render
        assert _text(line) == f"{word} 1h 00m ago · 2 revise rounds · 1h 30m from intake to {noun}", render
        when = re.search(r'<div class="jd-when"><span class="mark" aria-hidden="true"></span>(.*?)</div>', hero, re.S)
        assert _text(when.group(1)) == f"intake {intake} · {word} {shipped}", render
        assert 'class="jd-verb"' not in hero, render
        assert _ticket_tiles(markup) == [("1h 30m", f"total, intake to {noun}"), ("2", "revise rounds"),
                                         ("2 hops", "0 off-chart"), ("$4.18", "cost, 11 jobs")], render
        assert '<b>2<span class="jd-pips" aria-hidden="true"><span class="done"></span><span class="done"></span></span></b>' in hero
        assert f'<span class="job">{word} {shipped}</span></nav>' in markup, render
        px = _hero_px(markup)
        assert (px["data-px"], px["data-tint"], px["data-done"], px["data-still"]) == ("off", "#9ece6a", "true", "true")


@needs_node
def test_a_shipped_ticket_with_no_reported_cost_has_no_cost_tile(tmp_path: Path) -> None:
    snapshot = _with_shipped(_snapshot(tmp_path))
    snapshot[RECORDS][SHIPPED]["shipped"].update(intake_to_ship_seconds=None, revise_rounds=1)
    views = _both(tmp_path, snapshot, SHIPPED)
    assert views["script"] == views["server"]
    for render, markup in views.items():
        assert _ticket_tiles(markup) == [("no reading", "total, intake to merge"), ("1", "revise rounds"),
                                         ("2 hops", "0 off-chart")], render
        assert _text(re.search(r'<p class="line">(.*?)</p>', markup, re.S).group(1)) == "merged 1h 00m ago · 1 revise round"


@needs_node
def test_a_shipped_ticket_has_run_summary_and_ended_in_place_of_live_run_and_next(tmp_path: Path) -> None:
    snapshot = _with_shipped(_snapshot(tmp_path))
    _seeded_run(snapshot)
    intake, shipped = ticket_detail._et_clock(INTAKE_AT), ticket_detail._et_clock(NOW - 3600)
    views = _both(tmp_path, snapshot, SHIPPED)
    assert views["script"] == views["server"]
    for render, markup in views.items():
        assert _sections(markup) == ["Run summary", "Journey", "Run log", "Ended", "Full state chart"], render
        summary = _panel(markup, "Run summary")
        assert _count(summary) == f"the whole run · intake {intake} to merge {shipped}", render
        assert re.findall(r'<div class="lr-tile"><b>([^<]*)</b><span>([^<]*)</span></div>', summary) == [
            ("11", "jobs"), ("6.9M", "tokens in"), ("96k", "tokens out"), ("$4.18", "cost")], render
        assert re.search(r'<span class="lr-label">Time by who</span><span class="lr-count">1h 07m</span>', summary), render
        bar = re.search(r'<div class="sum-bar" aria-hidden="true">(.*?)</div>', summary, re.S).group(1)
        assert re.findall(r'<span style="flex:(\d+) 1 0;background:(#[0-9a-f]{6})"></span>', bar) == [
            ("3175", TINT["claude"]), ("825", TINT["codex"]), ("25", TINT["script"])], render
        key = re.search(r'<div class="sum-key">(.*?)</div>', summary, re.S).group(1)
        assert [_text(k) for k in re.findall(r'<span><i[^>]*></i>(.*?)</span>(?=<span><i|$)', key)] == [
            "claude 52m 55s · building, 2× fixing", "codex 13m 45s · 2× review", "script 25s · intake"], render
        assert '<p class="sum-foot">Totals over every job this ticket ran. Nothing is running now.</p>' in summary
        ended = _panel(markup, "Ended")
        assert _count(ended) == "end state · nothing comes next", render
        assert re.findall(r'<span class="(how|at|ago)">([^<]*)</span>', ended) == [
            ("how", "merged"), ("at", f"merged at {shipped}"), ("ago", "1h 00m ago")], render
        assert "Live run" not in markup and "Can come next" not in markup, render


@needs_node
def test_a_shipped_run_with_nothing_measured_reads_no_reading(tmp_path: Path) -> None:
    snapshot = _with_shipped(_snapshot(tmp_path))
    record = snapshot[RECORDS][SHIPPED]
    for visit in record["visits"]:
        visit["duration_seconds"] = None
    record["shipped"]["at"] = None
    views = _both(tmp_path, snapshot, SHIPPED)
    assert views["script"] == views["server"]
    for render, markup in views.items():
        summary = _panel(markup, "Run summary")
        assert re.findall(r'<div class="lr-tile"><b>([^<]*)</b><span>([^<]*)</span></div>', summary) == [
            ("3", "jobs"), ("no reading", "tokens in"), ("no reading", "tokens out"), ("no reading", "cost")], render
        assert re.search(r'<span class="lr-label">Time by who</span><span class="lr-count">no reading</span>', summary)
        assert 'class="sum-bar"' not in summary and 'class="sum-key"' not in summary, render
        ended = _panel(markup, "Ended")
        assert re.findall(r'<span class="(how|at|ago)">([^<]*)</span>', ended) == [("how", "merged"), ("at", "no reading")]


@needs_node
def test_a_shipped_run_log_lists_every_visit_and_ends_on_shipped(tmp_path: Path) -> None:
    snapshot = _with_shipped(_snapshot(tmp_path))
    _seeded_run(snapshot)
    for render, markup in _both(tmp_path, snapshot, SHIPPED).items():
        assert [state for _, state, _ in _rows(markup, "runlog")] == [state for state, _, _ in RUN], render
        log = re.search(r'<ol class="runlog">(.*?)</ol>', markup, re.S).group(1)
        last = re.findall(r"<li>.*?</li>", log, re.S)[-1]
        assert 'class="visit shipped"' in last and 'style="--m:#9ece6a"' in last, render
        assert '<span class="name">merged · shipped</span>' in last, render
        assert f'<span class="dur">at {ticket_detail._et_clock(NOW - 3600)}</span>' in last, render
        assert " · now" not in log and "so far" not in log, render


@needs_node
def test_a_hostile_actor_and_state_are_escaped_in_the_run_summary(tmp_path: Path) -> None:
    snapshot = _with_shipped(_snapshot(tmp_path))
    visit = snapshot[RECORDS][SHIPPED]["visits"][1]
    visit.update(who="<img src=x onerror=y>", state='"><script>x()</script>')
    for render, markup in _both(tmp_path, snapshot, SHIPPED).items():
        assert "<img src=x" not in markup and "<script>x()" not in markup, render
        summary = _panel(markup, "Run summary")
        assert "&lt;img src=x onerror=y&gt;" in summary and "&lt;script&gt;x()&lt;/script&gt;" in summary, render


def _time_by_who(markup: str) -> tuple[str, set[str]]:
    summary = _panel(markup, "Run summary")
    total = re.search(r'<span class="lr-label">Time by who</span><span class="lr-count">([^<]*)</span>', summary).group(1)
    key = re.search(r'<div class="sum-key">(.*?)</div>', summary, re.S).group(1)
    states = {re.sub(r"^\d+× ", "", state)
              for item in re.findall(r'<span><i[^>]*></i>(.*?)</span>(?=<span><i|$)', key)
              for state in _text(item).split(" · ", 1)[1].split(", ")}
    return total, states


@needs_node
def test_a_shipped_ticket_s_time_by_who_stops_at_shipment_however_long_it_sits_shipped(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from tests.test_dashboard_ticket_records import _shipped
    db, log = _shipped(tmp_path / "db")
    address = "maxharris1/cleffy#80"
    for offset in (0, 3600):
        snapshot = collect_snapshot(db, log, now=NOW + offset)
        took = snapshot[RECORDS][address]["shipped"]["intake_to_ship_seconds"]
        assert took == 1200
        for render, markup in _both(tmp_path, snapshot, address).items():
            total, states = _time_by_who(markup)
            assert total == duration(took), (offset, render)
            assert states == {"intake", "building", "review", "fixing", "ship"}, (offset, render)


@needs_node
def test_a_shipped_ticket_whose_intake_visit_has_no_clock_reads_intake_from_its_tracked_time(tmp_path: Path) -> None:
    import sqlite3
    from lib.dashboard import collect_snapshot
    from tests.test_dashboard import TRACKED_AT, add_job, add_ticket, seeded
    from tests.test_dashboard_ticket_records import transition
    db, log = seeded(tmp_path / "db")
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "maxharris1/cleffy", "99", state="merged")
        add_job(connection, "legacy-build", state="done", ticket_id="99", created_at=NOW - 3000)
        transition(connection, "99", "building", "review", NOW - 1800)
        transition(connection, "99", "review", "ship", NOW - 1200)
        transition(connection, "99", "ship", "merged", NOW - 600)
    snapshot = collect_snapshot(db, log, now=NOW)
    building = next(v for v in snapshot[RECORDS]["maxharris1/cleffy#99"]["visits"] if v["state"] == "building")
    intake, wrong = ticket_detail._et_clock(TRACKED_AT), ticket_detail._et_clock(building["started_at"])
    assert intake != wrong
    for render, markup in _both(tmp_path, snapshot, "maxharris1/cleffy#99").items():
        hero = re.search(r'<section class="jd-hero">.*?</section>', markup, re.S).group(0)
        when = _text(re.search(r'<div class="jd-when">(.*?)</div>', hero, re.S).group(1))
        assert f"intake {intake}" in when, render
        assert _count(_panel(markup, "Run summary")).startswith(f"the whole run · intake {intake} to "), render
        assert f"intake {wrong}" not in _text(markup), render


STILL_TICKET = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  document.querySelector('details.jd-chart').open = true;
  await sleep(4600);
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


def _ended_case(tmp_path: Path, case: str) -> tuple[dict, str]:
    snapshot = _with_shipped(_snapshot(tmp_path))
    address = RECORD[case.split()[0]]
    if case == "stopped a while ago":
        snapshot[RECORDS][address]["state_age_seconds"] = 5400
    return snapshot, address


ENDED_CAPTION = {
    "shipped": ("shipped · 2 hops · 1h 30m", "merged · shipped", "jd-cap shipped",
                "at " + ticket_detail._et_clock(NOW - 3600)),
    "stopped": ("stopped in intake", "stuck · stopped", "jd-cap stopped", "—"),
    "stopped a while ago": ("stopped in intake · 1h 30m ago", "stuck · stopped", "jd-cap stopped", "for 1h 30m"),
}


@needs_node
@pytest.mark.parametrize("case", ENDED_CAPTION)
def test_an_ended_journey_s_caption_leads_with_how_it_ended(tmp_path: Path, case: str) -> None:
    lead, state, cls, took = ENDED_CAPTION[case]
    snapshot, address = _ended_case(tmp_path, case)
    views = _both(tmp_path, snapshot, address)
    captions = {render: re.search(r'<div class="jd-cap[^"]*"[^>]*>.*?</div>', markup, re.S).group(0)
                for render, markup in views.items()}
    assert captions["server"] == captions["script"]
    caption = captions["server"]
    assert caption.startswith(f'<div class="{cls}" aria-live="polite"><span class="end" style="--m:')
    parts = [html.unescape(p) for p in re.findall(r">([^<]+)<", caption)]
    assert parts[0] == lead and parts[2] == state and took in parts, parts
    assert "now" not in " ".join(parts)
    label = re.search(r'<canvas class="jd-journey"[^>]*aria-label="([^"]*)"', views["server"]).group(1)
    assert html.unescape(label).endswith("; ended: " + lead)
    assert re.search(r'<canvas class="jd-journey"[^>]*aria-label="([^"]*)"', views["script"]).group(1) == label


@needs_node
@pytest.mark.parametrize(("kind", "replay", "keys"), [
    ("shipped", True, ["path taken · script", "path taken · codex", "visited", "off-chart jump", "ended here"]),
    ("stopped", False, ["path taken · script", "visited", "off-chart jump", "fall to a stop state", "stopped here"]),
])
def test_an_ended_journey_has_no_now_and_a_stopped_one_no_replay(tmp_path: Path, kind: str, replay: bool, keys) -> None:
    snapshot, address = _ended_case(tmp_path, kind)
    for render, markup in _both(tmp_path, snapshot, address).items():
        assert ("data-replay" in markup) is replay, render
        assert _chart_keys(markup) == keys, render


@needs_node
@pytest.mark.parametrize("kind", ["shipped", "stopped"])
def test_picking_a_step_on_an_ended_ticket_keeps_the_log_and_the_journey_in_step(tmp_path: Path, kind: str) -> None:
    snapshot, address = _ended_case(tmp_path, kind)
    hash_ = "#t=" + _quote(address)
    first = snapshot[RECORDS][address]["visits"][0]["state"]
    journey = re.search(r'<ol class="journey">(.*?)</ol>', _ticket_server(snapshot, address), re.S).group(1)
    label = html.unescape(re.search(r'data-visit="0"[^>]*aria-label="([^"]*)"', journey).group(1))
    for target, attr in (("runlog", {"data-visit": "0"}), ("journey", {"data-visit": "0", "aria-label": label})):
        clicks = [["click", {"attr": attr}]]
        view = _ticket_script(tmp_path, snapshot, hash_, clicks)
        for cls in ("runlog", "journey"):
            assert [i for i, _, p in _rows(view, cls) if p == "true"] == ["0"], (target, cls)
        caption = _caption(view)
        assert "|01 of " in caption and f"|{first}|" in caption, caption


ENDED_STILL = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  document.querySelector('details.jd-chart').open = true;
  await sleep(__WAIT__);
  const shot = () => [...document.querySelectorAll('.jd-hero canvas.px, canvas.jd-journey, canvas.jd-flowmap')]
    .map(c => c.toDataURL());
  const a = shot();
  await sleep(600);
  const strip = document.querySelector('.jd-hero canvas.px');
  return {frames: a, still: a.length === 3 && a.every((x, i) => x === shot()[i]),
          strip: [strip.dataset.px, strip.dataset.tint],
          why: [...document.querySelectorAll('section.jd-why .why-reason')].map(n => n.textContent)};
})()"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_ended_journeys_rest_and_reduced_motion_shows_the_rested_frame(tmp_path: Path) -> None:
    server = _serve(tmp_path, {"ended": _with_shipped(_snapshot(tmp_path))})
    found = {}
    try:
        for reduced in (False, True):
            chrome = _Chrome(tmp_path / f"profile-{reduced}")
            try:
                if reduced:
                    chrome.send("Emulation.setEmulatedMedia", chrome.session,
                                features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
                for kind, wait in (("stopped", 50), ("shipped", 4800)):
                    _open(chrome, f"http://127.0.0.1:{server.server_address[1]}/ended/ticket.html#t={_quote(RECORD[kind])}",
                          _journey_drawn("article.ticketpage"))
                    found[kind, reduced] = chrome.run(ENDED_STILL.replace("__WAIT__", str(wait)))
            finally:
                chrome.close()
    finally:
        server.shutdown()
    for kind in ("stopped", "shipped"):
        assert found[kind, False]["still"] and found[kind, True]["still"], kind
        assert found[kind, False]["frames"] == found[kind, True]["frames"], kind
    for reduced in (False, True):
        assert found["stopped", reduced]["strip"] == ["static", "#f7768e"], reduced
        assert found["stopped", reduced]["why"] == ["job-timeout"], reduced
        assert found["shipped", reduced]["why"] == [], reduced


STOPPED = RECORD["stopped"]
STOPPED_AT = NOW - (3 * 86400 + 2 * 3600)
REASON = "cancelled: superseded by sanctioned intake (Factory 2026-11-13)"
WAYS = [{"to": "planning", "label": "by an authorized unstick"}, {"to": "abandoned", "label": "ends the ticket"},
        {"to": "questions", "label": "Max answers"}, {"to": "building", "label": "answer, same step resumes"}]


def _stopped(snapshot: dict, stuck_flag: bool = False, **stopped) -> dict:
    record = snapshot[RECORDS][STOPPED]
    visits = [_visit("intake", "script", STOPPED_AT - 617, 25, None),
              _visit("questions", "claude", STOPPED_AT - 592, 192, "brief posted"),
              _visit("planning", "claude", STOPPED_AT - 400, None if stuck_flag else 400, REASON, current=stuck_flag)]
    if not stuck_flag:
        visits.append(_visit("stuck", "script", STOPPED_AT, None, "job-timeout", current=True))
    record.update(state="planning" if stuck_flag else "stuck", visits=visits, round=0, hops=len(visits) - 1,
                  off_chart_count=0)
    record["stopped"] = {"state": "stuck", "at": STOPPED_AT, "stopped_in": "planning", "reason": REASON,
                         "ways_out": WAYS[:2], **stopped}
    return record


def _hero_of(markup: str) -> str:
    return re.search(r'<section class="jd-hero">.*?</section>', markup, re.S).group(0)


def _facts(why: str) -> list[tuple[str, str]]:
    return [(th, _text(td)) for th, td in re.findall(r'<tr><th scope="row">([^<]*)</th><td>(.*?)</td></tr>', why, re.S)]


def _ways(why: str) -> list[tuple[str, str, str, bool]]:
    cards = re.findall(r'<div class="card( end)?"><div class="head"><span class="to">([^<]*)</span>'
                       r'(?:<span class="tag">([^<]*)</span>)?</div><span class="why">([^<]*)</span></div>', why)
    return [(to, tag, label, bool(end)) for end, to, tag, label in cards]


def _read_only(why: str) -> str:
    return re.search(r'<p class="why-ro">([^<]*)</p>', why).group(1)


STOP_HERO = {
    "on a stop state": ({}, "3d 2h", "Tue Jan 12, 1:00 AM ET"),
    "by a stuck flag": ({"stuck_flag": True, "at": 1783969500}, "185d 12h", "Mon Jul 13, 3:05 PM ET"),
}


@needs_node
@pytest.mark.parametrize("case", STOP_HERO)
def test_a_stopped_ticket_s_hero_is_static_red_with_its_line_and_tiles(tmp_path: Path, case: str) -> None:
    seed, age, day = STOP_HERO[case]
    snapshot = _snapshot(tmp_path)
    _stopped(snapshot, **seed)
    views = _both(tmp_path, snapshot, STOPPED)
    assert views["script"] == views["server"]
    for render, markup in views.items():
        hero = _hero_of(markup)
        assert _chips(_title(hero)) == ["stuck"] and '</h1><span class="chip bad">stuck</span>' in hero, render
        line = re.search(r'<p class="line">(.*?)</p>', hero, re.S).group(1)
        assert line == f'<b class="bad">stuck</b> in planning for <span>{age}</span>', render
        when = re.search(r'<div class="jd-when stop"><span class="mark" aria-hidden="true"></span>(.*?)</div>', hero, re.S)
        assert _text(when.group(1)) == f"no job running · stopped {day}", render
        assert 'class="jd-verb"' not in hero, render
        assert _ticket_tiles(markup) == [(age, "time stopped"), ("2 hops", "before it stopped"), ("none", "revise round")]
        assert f'<b class="bad">{age}</b><span>time stopped</span>' in hero, render
        assert f'<span class="job">stopped {day}</span></nav>' in markup, render
        px = _hero_px(markup)
        assert (px["data-px"], px["data-tint"], px["data-still"]) == ("static", "#f7768e", "true"), render


@needs_node
@pytest.mark.parametrize("reason", [None, ""])
def test_a_stopped_ticket_with_no_clock_round_place_or_reason_says_so(tmp_path: Path, reason: str | None) -> None:
    snapshot = _snapshot(tmp_path)
    _stopped(snapshot, at=None, stopped_in=None, reason=reason)["round"] = None
    views = _both(tmp_path, snapshot, STOPPED)
    assert views["script"] == views["server"]
    for render, markup in views.items():
        hero = _hero_of(markup)
        assert re.search(r'<p class="line">(.*?)</p>', hero, re.S).group(1) == '<b class="bad">stuck</b>', render
        assert _text(re.search(r'<div class="jd-when stop">(.*?)</div>', hero, re.S).group(1)) == "no job running"
        assert _ticket_tiles(markup) == [("no reading", "time stopped"), ("2 hops", "before it stopped"),
                                         ("no round", "revise round")], render
        assert 'class="job"' not in re.search(r'<nav class="jd-crumb".*?</nav>', markup, re.S).group(0), render
        why = _panel(markup, "Why it stopped")
        assert _count(why) == "", render
        assert _text(re.search(r'<div class="why-reason">(.*?)</div>', why, re.S).group(1)) == "no reason recorded"
        assert _facts(why) == [("when", "no reading"), ("where", "2 hops in (intake → questions → planning)"),
                               ("job", "none running")], render


@needs_node
def test_a_stopped_ticket_says_why_it_stopped_under_the_hero(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _stopped(snapshot)
    views = _both(tmp_path, snapshot, STOPPED)
    assert views["script"] == views["server"]
    for render, markup in views.items():
        after_hero = markup[re.search(r'<section class="jd-hero">.*?</section>', markup, re.S).end():]
        assert after_hero.startswith('<section class="jd-panel jd-why"><h2><span class="t">Why it stopped</span>'), render
        why = _panel(markup, "Why it stopped")
        assert _count(why) == "stopped 3d 2h ago · in planning", render
        assert _text(re.search(r'<div class="why-reason">(.*?)</div>', why, re.S).group(1)) == REASON, render
        assert _facts(why) == [("when", "Tue Jan 12, 1:00 AM ET · 3d 2h ago"),
                               ("where", "in planning, 2 hops in (intake → questions → planning)"),
                               ("job", "none running")], render
        assert '<span class="label">Ways out · 2, from the chart</span>' in why, render
        assert _ways(why) == [("planning", "", "by an authorized unstick", False),
                              ("abandoned", "end state", "ends the ticket", True)], render
        assert _read_only(why) == "This page is read-only; neither is done from here.", render


READ_ONLY = {0: "This page is read-only.", 1: "This page is read-only; it is not done from here.",
             2: "This page is read-only; neither is done from here.",
             3: "This page is read-only; none of these is done from here.",
             4: "This page is read-only; none of these is done from here."}


@needs_node
@pytest.mark.parametrize("n", READ_ONLY)
def test_the_read_only_line_reads_right_for_any_count_of_ways_out(tmp_path: Path, n: int) -> None:
    snapshot = _snapshot(tmp_path)
    _stopped(snapshot, ways_out=WAYS[:n])
    views = _both(tmp_path, snapshot, STOPPED)
    assert views["script"] == views["server"]
    for render, markup in views.items():
        why = _panel(markup, "Why it stopped")
        assert f'<span class="label">Ways out · {n}, from the chart</span>' in why, render
        assert [to for to, _, _, _ in _ways(why)] == [w["to"] for w in WAYS[:n]], render
        assert ('<p class="empty">no way out on the chart</p>' in why) is (n == 0), render
        assert _read_only(why) == READ_ONLY[n], render


@needs_node
def test_a_hostile_stop_reason_place_and_state_are_escaped_in_both_renders(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _stopped(snapshot, state='"><script>x()</script>', stopped_in="<b>planning</b>", reason="<img src=x onerror=y>",
             ways_out=[{"to": "<i>out</i>", "label": "<u>why</u>"}])
    views = _both(tmp_path, snapshot, STOPPED)
    assert views["script"] == views["server"]
    for render, markup in views.items():
        for raw in ("<img src=x", "<script>x()", "<b>planning", "<i>out", "<u>why"):
            assert raw not in markup, (render, raw)
        hero = _hero_of(markup)
        assert '<span class="chip bad">&quot;&gt;&lt;script&gt;x()&lt;/script&gt;</span>' in hero, render
        assert '<b class="bad">&quot;&gt;&lt;script&gt;x()&lt;/script&gt;</b> in &lt;b&gt;planning&lt;/b&gt;' in hero, render
        why = _panel(markup, "Why it stopped")
        for escaped in ("&lt;img src=x onerror=y&gt;", "&lt;b&gt;planning&lt;/b&gt;", "&lt;i&gt;out&lt;/i&gt;",
                        "&lt;u&gt;why&lt;/u&gt;"):
            assert escaped in why, (render, escaped)


PAGE_SECS = {"running": ["hero", "liverun", "journey", "next", "runlog", "exposure", "chart"],
             "queued": ["hero", "liverun", "journey", "next", "runlog", "chart"],
             "idle": ["hero", "journey", "next", "runlog", "chart"],
             "shipped": ["hero", "summary", "journey", "runlog", "ended", "chart"],
             "stopped": ["hero", "journey", "runlog", "chart"]}
ADDRESS = {**{k: a for k, (a, _) in LIVE.items()}, **RECORD}


def _secs(markup: str) -> list[str]:
    return re.findall(r'<(?:section|details) class="jd-[^"]*" (?:id="[^"]*" )?data-sec="([a-z]+)"', markup)


@needs_node
@pytest.mark.parametrize("kind", ticket_detail.TICKET_KINDS)
def test_both_renders_stack_the_sections_in_one_column_with_the_run_log_after_next(tmp_path: Path, kind: str) -> None:
    order = [s for s in PAGE_SECS[kind] if s != "hero"]
    views = _both(tmp_path, _with_shipped(_snapshot(tmp_path)), ADDRESS[kind])
    for render, markup in views.items():
        assert _secs(markup) == order, render
        assert markup.count('<div class="jd-cols">') == 1, render
        assert "jd-right" not in markup, render


PHONE_LAYOUT = r"""
(() => {
  const art = document.querySelector('article.ticketpage');
  const box = n => n && n.getBoundingClientRect();
  const tops = {hero: box(art.querySelector('.jd-hero'))};
  art.querySelectorAll('[data-sec]').forEach(n => { tops[n.dataset.sec] = box(n); });
  const tiles = [...art.querySelectorAll('.jd-hero .jd-tile')].map(box);
  const edges = r => r && [r.left, r.right];
  const chart = art.querySelector('details.jd-chart');
  const canvas = art.querySelector('[data-sec=journey] canvas.jd-journey');
  const css = box(canvas);
  return {order: Object.entries(tops).filter(([, r]) => r).sort((a, b) => a[1].top - b[1].top).map(([k]) => k),
          hero: edges(tops.hero), runlog: edges(tops.runlog), journey: edges(tops.journey),
          cols: new Set(tiles.map(r => Math.round(r.left))).size, rows: new Set(tiles.map(r => Math.round(r.top))).size,
          open: chart.open, logOpen: art.querySelector('details.jd-runlog').open,
          canvas: [canvas.width, canvas.height], canvasCss: [css.left, css.width],
          boxLeft: box(canvas.closest('.jd-box')).left, scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
          innerWidth: innerWidth};
})()"""


def _ticket_at(chrome: _Chrome, base: str, address: str, width: int) -> None:
    chrome.send("Emulation.setDeviceMetricsOverride", chrome.session, width=width, height=800, deviceScaleFactor=1,
                mobile=width < 700)
    _open(chrome, f"{base}ticket.html#t={_quote(address)}",
          f"document.querySelector('article.ticketpage[data-ticket=\"{address}\"] canvas.jd-journey')")


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_at_every_width_the_ticket_page_is_one_full_width_column_with_the_journey_1000px_and_the_run_log_closed(tmp_path: Path) -> None:
    server = _serve(tmp_path, {"fixture": _with_shipped(_snapshot(tmp_path))})
    base = f"http://127.0.0.1:{server.server_address[1]}/fixture/"
    chrome = _Chrome(tmp_path / "profile")
    found = {}
    try:
        for kind in PAGE_SECS:
            for width in (375, 1024, 1280, 1440):
                _ticket_at(chrome, base, ADDRESS[kind], width)
                if width == 1440:
                    _wait(chrome, "document.querySelector('article.ticketpage canvas.jd-journey').width === 1000")
                found[kind, width] = chrome.run(PHONE_LAYOUT)
    finally:
        chrome.close()
        server.shutdown()
    for kind, order in PAGE_SECS.items():
        phone = found[kind, 375]
        assert (phone["cols"], phone["rows"]) == (2, 2), kind
        assert phone["open"] is False, kind
        assert phone["scrollWidth"] == phone["innerWidth"] == 375, kind
        for width in (375, 1024, 1280, 1440):
            page = found[kind, width]
            assert page["order"] == order, (kind, width)
            assert page["runlog"] == page["journey"] == page["hero"], (kind, width)
            assert page["logOpen"] is False, (kind, width)
        desk = found[kind, 1440]
        assert desk["canvas"] == [1000, 336], kind
        assert desk["canvasCss"] == [desk["boxLeft"], 1000], kind
        for width in (1024, 1280):
            page = found[kind, width]
            assert page["innerWidth"] == width, (kind, width)
            assert page["scrollWidth"] == page["clientWidth"] <= width, (kind, width)


def _current_node(snapshot: dict, address: str) -> tuple[float, float]:
    from lib.dashboard_flow import flowmap_layout
    found = ticket_detail.resolve_ticket(snapshot, address, snapshot.get(RECORDS))
    model = ticket_detail._detail_model(found["row"], snapshot["statechart"])
    state = model["end"]["state"] if model["ended"] == "stopped" else model["state"]
    layout = flowmap_layout(snapshot["statechart"])
    node = next(n for n in layout["nodes"] if n["name"] == state)
    cell = layout["grid"]["cell"]
    return node["x"] * cell, (node["x"] + layout["node"]["w"]) * cell


CHART_OPEN = "window.oldTicket = document.querySelector('article.ticketpage'); oldTicket.querySelector('details.jd-chart').open = true"

CHART_VIEW = r"""
(async () => {
  await new Promise(r => setTimeout(r, 300));
  const chart = document.querySelector('article.ticketpage details.jd-chart');
  const box = chart.querySelector('.jd-box');
  return {open: chart.open, left: box.scrollLeft, width: box.clientWidth, scroll: box.scrollWidth,
          page: document.documentElement.scrollWidth, inner: innerWidth};
})()"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
@pytest.mark.parametrize("kind", ["running", "shipped", "stopped"])
def test_on_a_phone_the_state_chart_opens_on_the_current_state_in_its_own_scroll_box(tmp_path: Path, kind: str) -> None:
    snapshot = _with_shipped(_snapshot(tmp_path))
    records = threading.Event()
    server = _serve(tmp_path, {"fixture": snapshot}, records_gate=records)
    base = f"http://127.0.0.1:{server.server_address[1]}/fixture/"
    chrome = _Chrome(tmp_path / "profile")
    live = kind in LIVE
    views = {}
    try:
        if not live:
            records.set()
        _ticket_at(chrome, base, ADDRESS[kind], 375)
        chrome.run(CHART_OPEN)
        views[375, "opened"] = chrome.run(CHART_VIEW)
        if live:
            records.set()
            chrome.wait_for("!window.oldTicket.isConnected", "the records redraw")
            views[375, "redrawn"] = chrome.run(CHART_VIEW)
        _ticket_at(chrome, base, ADDRESS[kind], 320)
        views[320, "resized"] = chrome.run(CHART_VIEW)
    finally:
        records.set()
        chrome.close()
        server.shutdown()
    left, right = _current_node(snapshot, ADDRESS[kind])
    for (width, when), view in views.items():
        assert view["open"] and view["width"] < 1000 and view["scroll"] == 1000, (width, when, view)
        assert view["left"] <= left and right <= view["left"] + view["width"], (width, when, view, left, right)
        assert view["page"] == view["inner"] == width, (width, when)


LIVE_RUN_STACK = r"""
(() => {
  const lr = document.querySelector('article.ticketpage .lr');
  const top = s => lr.querySelector(s).getBoundingClientRect().top;
  const cols = [...lr.querySelectorAll('.lr-col')].map(c => c.getBoundingClientRect().top);
  const tiles = [...lr.querySelectorAll('.lr-tile')].map(t => t.getBoundingClientRect());
  return {head: top('.lr-head'), plan: cols[0], agents: cols[1], tiles: top('.lr-tiles'),
          xs: new Set(tiles.map(r => Math.round(r.left))).size, ys: new Set(tiles.map(r => Math.round(r.top))).size};
})()"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_on_a_phone_live_run_stacks_task_plan_sub_agents_then_tiles_two_by_two(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    _review(snapshot)["telemetry"] = {
        "tokens_in": 1240000, "tokens_cached": 1080000, "tokens_out": 18200, "cost_usd": 0.94, "files_touched": 7,
        "commands": 23, "current_task": "Re-run the dashboard tests", "last_finished": "Update the golden page digests",
        "plan": [{"text": "Read the review notes", "status": "done"}, {"text": "Re-run the dashboard tests", "status": "in_progress"}],
        "subagents": [{"kind": "Explore", "description": "Find the tile tests", "status": "running"}]}
    server = _serve(tmp_path, {"fixture": snapshot})
    base = f"http://127.0.0.1:{server.server_address[1]}/fixture/"
    chrome = _Chrome(tmp_path / "profile")
    found = {}
    try:
        for width in (375, 680):
            _ticket_at(chrome, base, LIVE["running"][0], width)
            found[width] = chrome.run(LIVE_RUN_STACK)
    finally:
        chrome.close()
        server.shutdown()
    for width, lr in found.items():
        assert lr["head"] < lr["plan"] < lr["agents"] < lr["tiles"], (width, lr)
        assert (lr["xs"], lr["ys"]) == (2, 2), (width, lr)


def _fit(box_width: int) -> int:
    return min(max(box_width // 4, 56), 250) * 4


REPLAY = '<button type="button" data-replay="1">replay</button>'


def _ctl(markup: str) -> str:
    return re.search(r'<div class="jd-ctl">.*</div></section>$', _journey(markup), re.S).group(0)


def _end_span(markup: str) -> tuple[str, str] | None:
    match = re.search(r'<div class="jd-cap[^"]*"[^>]*><span class="end" style="--m:([^"]*)">([^<]*)</span>', markup)
    return match and match.groups()


@needs_node
@pytest.mark.parametrize("kind", ["running", "shipped", "stopped"])
def test_both_renders_hold_the_same_journey_controls_with_an_end_note_for_ended_tickets(tmp_path: Path, kind: str) -> None:
    views = _both(tmp_path, _with_shipped(_snapshot(tmp_path)), ADDRESS[kind])
    ctls = {render: _ctl(markup) for render, markup in views.items()}
    assert ctls["server"] == ctls["script"], ctls
    for render, markup in views.items():
        notes = re.findall(r'<span class="endnote" style="--m:([^"]*)">([^<]*)</span>', ctls[render])
        end = _end_span(markup)
        if kind in LIVE:
            assert end is None and notes == [], (render, notes)
        else:
            assert notes == [end], (render, notes, end)
            after = REPLAY if kind == "shipped" else "</div>"
            assert after + '<span class="endnote"' in ctls[render], (render, ctls[render])
        assert "tap a step for its reason" in _journey(markup), render


def _media_harness(phone: bool) -> str:
    """The page harness with the phone query answered, a laid-out box, and scrollIntoView counted per click."""
    swaps = {
        "global.window = global;": (
            "global.window = global;\n"
            f"global.matchMedia = q => ({{matches: {json.dumps(phone)} && q === {json.dumps(ticket_detail.PHONE)}, media: q,"
            " addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}});\n"
            "global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };\n"
            "global.scrolledIntoView = 0;"),
        "  scrollIntoView() {}": (
            "  scrollIntoView() { global.scrolledIntoView += 1; }\n"
            "  get clientWidth() { return 327; }\n"
            "  getBoundingClientRect() { return {x: 0, y: 0, left: 0, top: 0, right: 327, bottom: 240, width: 327, height: 240}; }"),
        "const node = pick(arg);": "const node = pick(arg); const scrolled = global.scrolledIntoView;",
        "prevented: fire(node, 'click').defaultPrevented});":
            "prevented: fire(node, 'click').defaultPrevented, scrolls: global.scrolledIntoView - scrolled});",
    }
    harness = HARNESS
    for old, new in swaps.items():
        assert harness.count(old) == 1, old
        harness = harness.replace(old, new)
    return harness


@needs_node
def test_on_a_phone_picking_a_bar_selects_it_and_leaves_the_run_log_where_it_is(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    address = LIVE["running"][0]
    visit = _review(snapshot)["flow"]["visits"][2]
    label = html.unescape(re.search(r'<ol class="journey">.*?data-visit="2"[^>]*aria-label="([^"]*)"',
                                    _ticket_server(snapshot, address), re.S).group(1))
    from lib.dashboard_page import page_script
    assert '<script src="/app.js" defer></script>' in render_pages(snapshot)["ticket.html"]
    script = page_script(snapshot.get("statechart"))
    found = {}
    for phone in (False, True):
        found[phone] = run_harness(tmp_path / ("phone" if phone else "desktop"), _media_harness(phone), script, snapshot,
                                   "ticket", TICKET_IDS, [["click", {"attr": {"data-visit": "2", "aria-label": label}}]],
                                   "#t=" + _quote(address), tickets_json(snapshot))
    view = found[True]["html"]["ticketview"]
    for cls in ("runlog", "journey"):
        assert [i for i, _, p in _rows(view, cls) if p == "true"] == ["2"], cls
    caption = _caption(view)
    assert f"|{visit['state']}|" in caption and f"|{visit['who']}|" in caption, caption
    assert f"|took {duration(visit['duration_seconds'])}|" in caption, caption
    scrolls = {phone: out["steps"][0]["scrolls"] for phone, out in found.items()}
    assert scrolls[False] > 0, "a desktop bar pick scrolls its log row into view"
    assert scrolls[True] == 0, "a phone bar pick leaves the run log where it is"


JOURNEY_FIT = r"""
(async () => {
  await new Promise(r => setTimeout(r, 300));
  const sec = document.querySelector('article.ticketpage [data-sec=journey]');
  const canvas = sec.querySelector('.jd-box canvas.jd-journey');
  const box = canvas.closest('.jd-box');
  const ctl = sec.querySelector('.jd-ctl');
  const shown = n => n.offsetParent !== null;
  const end = sec.querySelector('.jd-cap .end');
  const note = ctl.querySelector('.endnote');
  return {canvas: [canvas.width, canvas.height], css: canvas.getBoundingClientRect().width,
          box: box.clientWidth, boxScroll: box.scrollWidth,
          axis: [...ctl.querySelectorAll('.axis')].some(shown),
          hits: [...new Set([...sec.querySelectorAll('ol.journey button[data-visit]')].map(b => b.offsetHeight))],
          endnote: note && {shown: shown(note), text: note.textContent, style: note.getAttribute('style')},
          end: end && {shown: true, text: end.textContent, style: end.getAttribute('style')},
          capShown: [...sec.querySelectorAll('.jd-cap .idx, .jd-cap .note, .jd-cap .end')]
            .filter(n => getComputedStyle(n).display !== 'none').map(n => n.className),
          subtitle: sec.querySelector('h2').innerText, page: document.documentElement.scrollWidth};
})()"""


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_on_a_phone_the_journey_fits_its_box_with_no_axis_and_an_end_note(tmp_path: Path) -> None:
    server = _serve(tmp_path, {"fixture": _with_shipped(_snapshot(tmp_path))})
    base = f"http://127.0.0.1:{server.server_address[1]}/fixture/"
    chrome = _Chrome(tmp_path / "profile")
    found = {}
    try:
        for kind in ("running", "shipped", "stopped"):
            for width in (375, 320, 1440):
                _ticket_at(chrome, base, ADDRESS[kind], width)
                _wait(chrome, _journey_drawn("article.ticketpage"))
                found[kind, width] = chrome.run(JOURNEY_FIT)
    finally:
        chrome.close()
        server.shutdown()
    for (kind, width), fit in found.items():
        at = (kind, width, fit)
        if width < 700:
            assert fit["canvas"] == [_fit(fit["box"]), 240], at
            assert fit["css"] <= fit["box"] == fit["boxScroll"], at
            assert not fit["axis"], at
            assert fit["hits"] == [220], at
            assert fit["capShown"] == [], at
            assert "tap a step for its reason" in fit["subtitle"] and "pick a step" not in fit["subtitle"], at
            assert fit["page"] == width, at
        else:
            assert fit["canvas"] == [1000, 336] and fit["axis"], at
            assert "pick a step for its reason" in fit["subtitle"] and "tap a step" not in fit["subtitle"], at
        if kind in LIVE:
            assert fit["endnote"] is None, at
        else:
            assert fit["endnote"] == {**fit["end"], "shown": width < 700}, at


TAP_AT = r"""
(i => {
  const b = document.querySelector(`article.ticketpage ol.journey button[data-visit="${i}"]`);
  b.closest('.jd-box').scrollIntoView({block: 'center'});
  window.oldTicket = document.querySelector('article.ticketpage');
  const r = b.getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2, scrollY: window.scrollY};
})"""

TAPPED = r"""
(async () => {
  await new Promise(r => setTimeout(r, 300));
  const art = document.querySelector('article.ticketpage');
  const sec = art.querySelector('[data-sec=journey]');
  const canvas = sec.querySelector('canvas.jd-journey');
  const text = s => (n => n && n.textContent)(sec.querySelector('.jd-cap ' + s));
  return {caption: {st: text('.st'), who: text('.who'), dur: text('.dur')},
          log: [...art.querySelectorAll('.runlog [aria-pressed=true]')].map(b => b.dataset.visit),
          bars: [...art.querySelectorAll('ol.journey [aria-pressed=true]')].map(b => b.dataset.visit),
          buttons: [...sec.querySelectorAll('.jd-ctl button')].filter(b => b.offsetParent !== null).map(b => b.textContent),
          canvas: [canvas.width, canvas.height], box: canvas.closest('.jd-box').clientWidth, scrollY: window.scrollY};
})()"""

TAPS = {"running": ["1", "2"], "shipped": ["1"], "stopped": ["0"]}


def _tap(chrome: _Chrome, visit: str) -> float:
    at = chrome.run(f"{TAP_AT}({json.dumps(visit)})")
    chrome.send("Input.dispatchTouchEvent", chrome.session, type="touchStart", touchPoints=[{"x": at["x"], "y": at["y"]}])
    chrome.send("Input.dispatchTouchEvent", chrome.session, type="touchEnd", touchPoints=[])
    return at["scrollY"]


def _picked(visit: dict) -> dict:
    took = visit["duration_seconds"]
    return {"st": visit["state"], "who": visit["who"], "dur": "—" if took is None else f"took {duration(took)}"}


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
@pytest.mark.parametrize("kind", TAPS)
def test_on_a_phone_a_tap_picks_a_step_in_place_before_and_after_a_rebuild(tmp_path: Path, kind: str) -> None:
    snapshot = _with_shipped(_snapshot(tmp_path))
    visits = (_review(snapshot)["flow"] if kind in LIVE else snapshot[RECORDS][ADDRESS[kind]])["visits"]
    records = threading.Event()
    server = _serve(tmp_path, {"fixture": snapshot}, records_gate=records)
    base = f"http://127.0.0.1:{server.server_address[1]}/fixture/"
    chrome = _Chrome(tmp_path / "profile")
    taps = []
    try:
        chrome.send("Emulation.setTouchEmulationEnabled", chrome.session, enabled=True, maxTouchPoints=1)
        if kind not in LIVE:
            records.set()
        _ticket_at(chrome, base, ADDRESS[kind], 375)
        _wait(chrome, _journey_drawn("article.ticketpage"))
        for n, visit in enumerate(TAPS[kind]):
            if n:
                records.set()
                chrome.wait_for("!window.oldTicket.isConnected", "the records rebuild")
                _wait(chrome, _journey_drawn("article.ticketpage"))
            before = _tap(chrome, visit)
            taps.append((visit, before, chrome.run(TAPPED)))
    finally:
        records.set()
        chrome.close()
        server.shutdown()
    assert len(taps) == len(TAPS[kind])
    replay = [] if kind == "stopped" else ["replay"]
    for when, (visit, before, tapped) in zip(("first render", "rebuilt"), taps):
        at = (kind, when, visit, tapped)
        assert tapped["caption"] == _picked(visits[int(visit)]), at
        assert tapped["log"] == tapped["bars"] == [visit], at
        assert tapped["scrollY"] == before, ("the tap leaves the page where it was", at)
        assert tapped["buttons"] == replay, at
        assert tapped["canvas"] == [_fit(tapped["box"]), 240], at


REFIT = r"""
(async phone => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const read = () => {
    const c = document.querySelector('article.ticketpage canvas.jd-journey');
    const box = c.closest('.jd-box').clientWidth;
    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
    let drawn = 0;
    for (let i = 0; i < d.length; i += 4) if (d[i + 3] && (d[i] !== 14 || d[i + 1] !== 14 || d[i + 2] !== 20)) drawn++;
    return {canvas: [c.width, c.height], box, drawn};
  };
  const fits = g => g.drawn > 0 && g.canvas[0] === (phone ? Math.min(Math.max(Math.floor(g.box / 4), 56), 250) * 4 : 1000);
  await sleep(100);
  let got = read();
  for (let t = 0; t < 60 && !fits(got); t++) { await sleep(50); got = read(); }
  return got;
})"""

RESIZES = [(320, 800, True), (667, 375, True), (1280, 800, False)]


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_under_reduced_motion_a_resize_refits_and_repaints_the_journey(tmp_path: Path) -> None:
    server = _serve(tmp_path, {"fixture": _with_shipped(_snapshot(tmp_path))})
    base = f"http://127.0.0.1:{server.server_address[1]}/fixture/"
    chrome = _Chrome(tmp_path / "profile")
    found = {}
    try:
        chrome.send("Emulation.setEmulatedMedia", chrome.session,
                    features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
        for kind in ("running", "shipped", "stopped"):
            _ticket_at(chrome, base, ADDRESS[kind], 375)
            _wait(chrome, _journey_drawn("article.ticketpage"))
            found[kind, 375, 800] = chrome.run(f"{REFIT}(true)")
            for width, height, mobile in RESIZES:
                chrome.send("Emulation.setDeviceMetricsOverride", chrome.session, width=width, height=height,
                            deviceScaleFactor=1, mobile=mobile)
                found[kind, width, height] = chrome.run(f"{REFIT}({json.dumps(width < 700)})")
    finally:
        chrome.close()
        server.shutdown()
    for (kind, width, height), got in found.items():
        want = [_fit(got["box"]), 240] if width < 700 else [1000, 336]
        assert got["canvas"] == want, (kind, width, height, got)
        assert got["drawn"] > 0, ("the refit canvas is painted", kind, width, height, got)


EXPOSURE_ADDRESS = "maxharris1/factory#1014"
INDEPENDENT = Exposure("independent-model", "independent-cli 1.2.3", "a" * 64, ("/review/SKILL.md",),
                       "independent-host", "b" * 40, "high", "claude")
REAL = Exposure("gpt-5.5-codex", "codex-cli 0.98.0", hashlib.sha256(b"brief").hexdigest(),
                ("/skills/factory-reviewer/SKILL.md", "/skills/poteto-mode/SKILL.md"), "mac-mini-2",
                "d60fba4d163ee847997dad6036db1f19e26b2e4a", "high", "codex")
UNKNOWN = Exposure("unknown", "unknown", "unknown", None, "unknown", "unknown", "unknown", "unknown")


def _shown(found: Exposure, number: str = "unknown") -> list[tuple[str, str]]:
    paths = "unknown" if found.skill_paths is None else ", ".join(found.skill_paths)
    return [("review round", number), ("model", found.model), ("effort", found.effort), ("harness", found.harness),
            ("CLI version", found.cli_version), ("brief sha256", found.brief_sha256),
            ("skill paths", paths), ("host", found.host), ("factory revision", found.factory_revision)]


def _exposed(tmp_path: Path, found: Exposure | None) -> dict:
    from lib import exposure
    from lib.dashboard import collect_snapshot
    from lib.queue import JobRequest
    from tests.test_dashboard import make_db

    db = tmp_path / "orch.db"
    queue = make_db(db)
    queue.enqueue(JobRequest(repo="maxharris1/factory", ticket_id="1014", action="review", recipient="codex",
                             source_comment_id="c1", timeout_seconds=600), dedupe_key="k1")
    job = queue.claim("codex")
    assert job is not None and job.state == "running"
    if found is not None:
        with closing(sqlite3.connect(db)) as connection, connection:
            exposure.ensure_table(connection)
            exposure.write(connection, job.id, found, NOW)
    return collect_snapshot(db, tmp_path / "missing.log", now=1_800_000_002.0, alive=None)


def _exposure_facts(markup: str) -> list[tuple[str, str]]:
    match = re.search(r'<section class="jd-panel jd-exp" data-sec="exposure">.*?</section>', markup, re.S)
    assert match, "no exposure section"
    return [(name, html.unescape(value)) for name, value in
            re.findall(r'<tr><th scope="row">([^<]*)</th><td>([^<]*)</td></tr>', match.group(0))]


@pytest.mark.parametrize("found", [INDEPENDENT, REAL, UNKNOWN], ids=["independent", "real", "unknown"])
def test_a_running_job_s_ticket_page_shows_its_nine_exposure_fields(tmp_path: Path, found: Exposure) -> None:
    from lib.dashboard import write_snapshot_pages

    snapshot = _exposed(tmp_path, found)
    (row,) = snapshot["running"]
    assert set(row["exposure"]) == NINE
    status = write_snapshot_pages(tmp_path / "out", snapshot)[0]
    assert json.loads(status.read_text(encoding="utf-8"))["running"][0]["exposure"] == row["exposure"]
    assert _exposure_facts(_ticket_server(snapshot, EXPOSURE_ADDRESS)) == _shown(found)


NINE = {"round", "model", "effort", "harness", "cli_version", "brief_sha256", "skill_paths", "host", "factory_revision"}


def _run_log_exposures(markup: str) -> dict[str, list[tuple[str, list[tuple[str, str]]]]]:
    log = re.search(r'<ol class="runlog">(.*?)</ol>', markup, re.S)
    assert log, "no run log"
    found: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {}
    for item in re.findall(r"<li>(.*?)</li>", log.group(1), re.S):
        state = re.search(r'data-state="([^"]*)"', item).group(1)
        found.setdefault(state, []).extend(
            (html.unescape(caption), [(name, html.unescape(value)) for name, value in
                                      re.findall(r'<tr><th scope="row">([^<]*)</th><td>([^<]*)</td></tr>', rows)])
            for caption, rows in re.findall(
                r'<table class="exp"><caption>([^<]*)</caption><tbody>(.*?)</tbody></table>', item, re.S)
        )
    return found


def _run_log_models(markup: str) -> dict[str, list[tuple[str, str, str]]]:
    log = re.search(r'<ol class="runlog">(.*?)</ol>', markup, re.S)
    assert log, "no run log"
    found: dict[str, list[tuple[str, str, str]]] = {}
    for item in re.findall(r"<li>(.*?)</li>", log.group(1), re.S):
        state = re.search(r'data-state="([^"]*)"', item).group(1)
        jobs = re.search(r'<div class="exp-jobs">(.*?)</div>', item, re.S)
        found.setdefault(state, [])
        if not jobs:
            continue
        assert item.index("</button>") < jobs.start() < item.index('<details class="exp-more"'), state
        found[state].extend(
            (html.unescape(job), html.unescape(action), html.unescape(" · ".join(filter(None, (number, model)))))
            for job, action, number, model in re.findall(
                r'<span class="exp-job"><span class="job">([^<]*)</span><span class="act">([^<]*)</span>'
                r'(?:<span class="rnd">([^<]*)</span>)?<span class="model">([^<]*)</span>'
                r'(?:<span class="wait">[^<]*</span>)?</span>', jobs.group(1))
        )
    return found


def _run_log_waits(markup: str) -> dict[str, str | None]:
    log = re.search(r'<ol class="runlog">(.*?)</ol>', markup, re.S)
    assert log, "no run log"
    found = {}
    for item in re.findall(r'<span class="exp-job">(.*?)</span></span>', log.group(1)):
        job = re.search(r'<span class="job">([^<]*)</span>', item).group(1)
        wait = re.search(r'<span class="wait">([^<]*)$', item)
        found[job] = None if wait is None else wait.group(1)
    return found


def _write_suite_waits(db: Path, waits: dict[str, object], *, column: bool = True) -> None:
    from lib import telemetry

    names = ["job_id", "bot", "harness", "repo", "started_at", "updated_at", "elapsed_seconds", "events", "turns",
             "tool_calls", "files_touched", "commands", "test_runs", "tokens_in", "tokens_cached", "tokens_out", "steps"]
    with closing(sqlite3.connect(db)) as connection, connection:
        telemetry.ensure_table(connection)
        if not column:
            connection.execute(f"ALTER TABLE {telemetry.TABLE} DROP COLUMN suite_wait_seconds")
        for job_id, seconds in waits.items():
            values = [job_id, "claude-bot", "claude", "maxharris1/factory", NOW, NOW, 0, 0, 0, "{}", 0, 0, 0, 0, 0, 0, "[]"]
            extra = ["suite_wait_seconds"] if column else []
            connection.execute(
                f"INSERT INTO {telemetry.TABLE} ({', '.join(names + extra)}) "
                f"VALUES ({', '.join('?' for _ in names + extra)})",
                values + ([seconds] if column else []),
            )


def _record_jobs(snapshot: dict) -> dict[str, dict]:
    (record,) = json.loads(tickets_json(snapshot))["tickets"].values()
    return {job["job"]: job for visit in record["visits"] for job in visit["jobs"]}


def _ticket_jobs(
    tmp_path: Path, written: dict[str, Exposure | None], *, table: bool = True,
    waits: dict[str, object] | None = None, wait_column: bool = True,
) -> tuple[dict, dict]:
    from lib import exposure
    from lib.dashboard import collect_snapshot
    from lib.queue import JobRequest
    from tests.test_dashboard import add_ticket, make_db

    db = tmp_path / "orch.db"
    queue = make_db(db)
    with closing(sqlite3.connect(db)) as connection, connection:
        add_ticket(connection, "maxharris1/factory", "1014", state="building")
    ids = {}
    for action in ("plan", "build"):
        queue.enqueue(JobRequest(repo="maxharris1/factory", ticket_id="1014", action=action, recipient="claude",
                                 source_comment_id=f"c-{action}", timeout_seconds=600), dedupe_key=f"k-{action}")
        job = queue.claim("claude")
        assert job is not None and job.request.action == action
        ids[action] = job.id
        if written[action] is not None:
            with closing(sqlite3.connect(db)) as connection, connection:
                exposure.ensure_table(connection)
                exposure.write(connection, job.id, written[action], NOW)
        if action == "plan":
            queue.finish(job.id, owner="claude", handoff_comment_id="h-plan")
    if not table:
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute(f"DROP TABLE IF EXISTS {exposure.TABLE}")
    if waits is not None:
        _write_suite_waits(db, {ids[action]: seconds for action, seconds in waits.items()}, column=wait_column)
    return collect_snapshot(db, tmp_path / "missing.log", now=1_800_000_002.0, alive=None), ids


def test_the_run_log_shows_each_job_s_exposure(tmp_path: Path) -> None:
    snapshot, ids = _ticket_jobs(tmp_path, {"plan": REAL, "build": INDEPENDENT})
    markup = _ticket_server(snapshot, EXPOSURE_ADDRESS)
    shown = _run_log_exposures(markup)
    assert shown["planning"] == [(f"{ids['plan'][:8]} · plan", _shown(REAL))]
    assert shown["building"] == [(f"{ids['build'][:8]} · build", _shown(INDEPENDENT))]
    assert all(not jobs for state, jobs in shown.items() if state not in ("planning", "building"))
    models = _run_log_models(markup)
    assert models["planning"] == [(ids["plan"][:8], "plan", "gpt-5.5-codex · high")]
    assert models["building"] == [(ids["build"][:8], "build", "independent-model · high")]
    assert all(not jobs for state, jobs in models.items() if state not in ("planning", "building"))
    jobs = _record_jobs(snapshot)
    assert set(jobs) == {ids["plan"][:8], ids["build"][:8]}
    assert all(set(job["exposure"]) == NINE for job in jobs.values())
    assert jobs[ids["plan"][:8]]["exposure"]["brief_sha256"] == [REAL.brief_sha256[i:i + 16] for i in range(0, 64, 16)]


@pytest.mark.parametrize("table", [True, False], ids=["no-row", "no-table"])
def test_a_job_with_no_exposure_row_shows_nine_unknowns(tmp_path: Path, table: bool) -> None:
    snapshot, ids = _ticket_jobs(tmp_path, {"plan": None, "build": None}, table=table)
    assert set(snapshot["running"][0]["exposure"]) == NINE
    markup = _ticket_server(snapshot, EXPOSURE_ADDRESS)
    assert _exposure_facts(markup) == _shown(UNKNOWN)
    shown = _run_log_exposures(markup)
    assert shown["planning"] == [(f"{ids['plan'][:8]} · plan", _shown(UNKNOWN))]
    assert shown["building"] == [(f"{ids['build'][:8]} · build", _shown(UNKNOWN))]
    models = _run_log_models(markup)
    assert models["planning"] == [(ids["plan"][:8], "plan", "unknown")]
    assert models["building"] == [(ids["build"][:8], "build", "unknown")]
    assert all(set(job["exposure"]) == NINE and set(job["exposure"].values()) == {"unknown"}
               for job in _record_jobs(snapshot).values())


def test_exposure_stays_on_the_run_log_after_the_job_finishes(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from lib.queue import SQLiteJobQueue

    snapshot, ids = _ticket_jobs(tmp_path, {"plan": REAL, "build": INDEPENDENT})
    build = ids["build"][:8]
    assert [row["job"] for row in snapshot["running"]] == [build]
    before = _run_log_exposures(_ticket_server(snapshot, EXPOSURE_ADDRESS))["building"]
    assert before == [(f"{build} · build", _shown(INDEPENDENT))]
    line = [(build, "build", "independent-model · high")]
    assert _run_log_models(_ticket_server(snapshot, EXPOSURE_ADDRESS))["building"] == line
    recorded = _record_jobs(snapshot)[build]["exposure"]

    SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: NOW + 1).finish(
        ids["build"], owner="claude", handoff_comment_id="h-build")
    after = collect_snapshot(tmp_path / "orch.db", tmp_path / "missing.log", now=1_800_000_003.0, alive=None)

    assert after["running"] == []
    assert _run_log_exposures(_ticket_server(after, EXPOSURE_ADDRESS))["building"] == before
    assert _run_log_models(_ticket_server(after, EXPOSURE_ADDRESS))["building"] == line
    assert _record_jobs(after)[build]["exposure"] == recorded


@needs_node
@pytest.mark.parametrize("found", [INDEPENDENT, REAL, UNKNOWN, None], ids=["independent", "real", "unknown", "none"])
def test_the_script_draws_the_server_s_exposure(tmp_path: Path, found: Exposure | None) -> None:
    views = _both(tmp_path, _exposed(tmp_path / "db", found), EXPOSURE_ADDRESS)
    assert views["script"] == views["server"]
    assert _exposure_facts(views["script"]) == _shown(UNKNOWN if found is None else found)


@needs_node
@pytest.mark.parametrize("written", [{"plan": REAL, "build": INDEPENDENT}, {"plan": None, "build": None}],
                         ids=["recorded", "missing"])
def test_the_script_draws_the_server_s_run_log_exposure(tmp_path: Path, written: dict) -> None:
    (tmp_path / "db").mkdir()
    snapshot, ids = _ticket_jobs(tmp_path / "db", written)
    views = _both(tmp_path, snapshot, EXPOSURE_ADDRESS)
    assert views["script"] == views["server"]
    shown = _run_log_exposures(views["script"])
    assert shown["planning"] == [(f"{ids['plan'][:8]} · plan", _shown(written["plan"] or UNKNOWN))]
    assert shown["building"] == [(f"{ids['build'][:8]} · build", _shown(written["build"] or UNKNOWN))]
    models = _run_log_models(views["script"])
    assert models["planning"] == [(ids["plan"][:8], "plan", "gpt-5.5-codex · high" if written["plan"] else "unknown")]
    assert models["building"] == [
        (ids["build"][:8], "build", "independent-model · high" if written["build"] else "unknown")
    ]


def test_the_run_log_shows_how_long_a_job_waited_for_a_suite_slot(tmp_path: Path) -> None:
    snapshot, ids = _ticket_jobs(tmp_path, {"plan": REAL, "build": INDEPENDENT}, waits={"plan": None, "build": 37})
    plan, build = ids["plan"][:8], ids["build"][:8]
    markup = _ticket_server(snapshot, EXPOSURE_ADDRESS)
    assert _run_log_waits(markup) == {plan: None, build: "suite wait 37s"}
    assert _run_log_models(markup)["building"] == [(build, "build", "independent-model · high")]
    jobs = _record_jobs(snapshot)
    assert (jobs[plan]["suite_wait_seconds"], jobs[build]["suite_wait_seconds"]) == (None, 37)


def test_the_suite_wait_stays_on_the_run_log_after_the_job_finishes(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from lib.queue import SQLiteJobQueue

    snapshot, ids = _ticket_jobs(tmp_path, {"plan": REAL, "build": INDEPENDENT}, waits={"plan": None, "build": 37})
    build = ids["build"][:8]
    assert _run_log_waits(_ticket_server(snapshot, EXPOSURE_ADDRESS))[build] == "suite wait 37s"

    SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: NOW + 1).finish(
        ids["build"], owner="claude", handoff_comment_id="h-build")
    after = collect_snapshot(tmp_path / "orch.db", tmp_path / "missing.log", now=1_800_000_003.0, alive=None)

    assert after["running"] == []
    assert _run_log_waits(_ticket_server(after, EXPOSURE_ADDRESS))[build] == "suite wait 37s"
    assert _record_jobs(after)[build]["suite_wait_seconds"] == 37


@pytest.mark.parametrize(
    "waits, column",
    [({"plan": 5, "build": 37}, False), ({"plan": "soon", "build": None}, True)],
    ids=["column-missing", "not-a-number"],
)
def test_no_suite_wait_is_shown_without_a_usable_value(tmp_path: Path, waits: dict, column: bool) -> None:
    snapshot, ids = _ticket_jobs(tmp_path, {"plan": REAL, "build": INDEPENDENT}, waits=waits, wait_column=column)
    markup = _ticket_server(snapshot, EXPOSURE_ADDRESS)
    assert "suite wait" not in markup
    assert _run_log_models(markup)["building"] == [(ids["build"][:8], "build", "independent-model · high")]
    assert all(job["suite_wait_seconds"] is None for job in _record_jobs(snapshot).values())


@needs_node
def test_the_script_draws_the_server_s_suite_wait(tmp_path: Path) -> None:
    (tmp_path / "db").mkdir()
    snapshot, ids = _ticket_jobs(tmp_path / "db", {"plan": REAL, "build": INDEPENDENT},
                                 waits={"plan": None, "build": 37})
    views = _both(tmp_path, snapshot, EXPOSURE_ADDRESS)
    assert views["script"] == views["server"]
    assert _run_log_waits(views["script"]) == {ids["plan"][:8]: None, ids["build"][:8]: "suite wait 37s"}


def _flow_jobs(result: dict) -> dict[str, str]:
    jobs = [(job["job"], visit["state"]) for visit in result["visits"] for job in visit["jobs"]]
    assert len(jobs) == len(dict(jobs)), "a job sits in more than one visit"
    return dict(jobs)


WALKED = [
    JobStep("plan", "claude", 100.0, 101.0, 140.0, job="plan"),
    JobStep("build", "claude", 200.0, 201.0, 280.0, job="build"),
    JobStep("review", "codex", 300.0, 301.0, 380.0, job="review"),
    JobStep("qa", "qa", 410.0, 411.0, None, job="qa"),
    JobStep("not-an-action", "claude", 420.0, None, None, job="other"),
]
RECORDED = [
    Transition(None, "intake", 50.0, "t"),
    Transition("intake", "building", 150.0, "t"),
    Transition("building", "review", 290.0, "t"),
    Transition("review", "fixing", 400.0, "t"),
]


@pytest.mark.parametrize("chart", ["chart", "mini", "none"])
@pytest.mark.parametrize("transitions", [[], RECORDED], ids=["walked", "recorded"])
def test_every_job_sits_in_exactly_one_visit(chart: str, transitions: list[Transition]) -> None:
    from tests.test_dashboard_layout import MINI

    loaded = {"chart": load_chart(), "mini": load_chart(MINI), "none": None}[chart]
    result = job_flow(loaded, WALKED, "fixing", 500.0, transitions)
    placed = _flow_jobs(result)
    assert set(placed) == {step.job for step in WALKED}
    assert placed["qa"] == placed["other"] == ("fixing" if transitions else "review")


def _qa_ticket(tmp_path: Path, written: Exposure | None) -> tuple[Path, str]:
    from lib import exposure
    from lib.queue import JobRequest
    from tests.test_dashboard import add_ticket, make_db

    db = tmp_path / "orch.db"
    queue = make_db(db)
    with closing(sqlite3.connect(db)) as connection, connection:
        add_ticket(connection, "maxharris1/factory", "1014", state="ship")
    queue.enqueue(JobRequest(repo="maxharris1/factory", ticket_id="1014", action="qa", recipient="qa",
                             source_comment_id="c-qa", timeout_seconds=600), dedupe_key="k-qa")
    job = queue.claim("qa")
    assert job is not None
    if written is not None:
        with closing(sqlite3.connect(db)) as connection, connection:
            exposure.ensure_table(connection)
            exposure.write(connection, job.id, written, NOW)
    return db, job.id


@pytest.mark.parametrize("written", [REAL, None], ids=["recorded", "missing"])
def test_a_qa_job_keeps_its_exposure_on_the_run_log_after_it_finishes(tmp_path: Path, written: Exposure | None) -> None:
    from lib.dashboard import collect_snapshot
    from lib.queue import SQLiteJobQueue

    db, job_id = _qa_ticket(tmp_path, written)
    caption = (f"{job_id[:8]} · qa", _shown(written or UNKNOWN))
    for phase in ("running", "finished"):
        if phase == "finished":
            SQLiteJobQueue(db, clock=lambda: NOW + 1).finish(job_id, owner="qa", handoff_comment_id="h-qa")
        snapshot = collect_snapshot(db, tmp_path / "missing.log", now=1_800_000_003.0, alive=None)
        assert [row["job"] for row in snapshot["running"]] == ([job_id[:8]] if phase == "running" else [])
        assert _run_log_exposures(_ticket_server(snapshot, EXPOSURE_ADDRESS))["ship"] == [caption], phase
        assert set(_record_jobs(snapshot)) == {job_id[:8]}, phase
        assert set(_record_jobs(snapshot)[job_id[:8]]["exposure"]) == NINE


@needs_node
def test_the_script_draws_a_finished_qa_job_s_exposure(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from lib.queue import SQLiteJobQueue

    (tmp_path / "db").mkdir()
    db, job_id = _qa_ticket(tmp_path / "db", REAL)
    SQLiteJobQueue(db, clock=lambda: NOW + 1).finish(job_id, owner="qa", handoff_comment_id="h-qa")
    snapshot = collect_snapshot(db, tmp_path / "missing.log", now=1_800_000_003.0, alive=None)
    views = _both(tmp_path, snapshot, EXPOSURE_ADDRESS)
    assert views["script"] == views["server"]
    assert _run_log_exposures(views["script"])["ship"] == [(f"{job_id[:8]} · qa", _shown(REAL))]


RUN_LOG_HEAD = re.compile(
    r'<details class="jd-panel jd-runlog" data-sec="runlog" data-key="runlog:([^"]*)"><summary><h2>'
    r'<span class="t">Run log</span><span class="count">(\d+) visits · oldest first</span></h2></summary><ol class="runlog">')


def _visit_items(markup: str) -> list[str]:
    return re.findall(r"<li>(.*?)</li>", re.search(r'<ol class="runlog">(.*?)</ol>', markup, re.S).group(1), re.S)


def test_the_run_log_and_each_visit_s_job_metadata_render_collapsed(tmp_path: Path) -> None:
    snapshot, ids = _ticket_jobs(tmp_path, {"plan": REAL, "build": INDEPENDENT})
    markup = _ticket_server(snapshot, EXPOSURE_ADDRESS)
    head = RUN_LOG_HEAD.search(markup)
    assert head, markup
    items = _visit_items(markup)
    assert head.groups() == (EXPOSURE_ADDRESS, str(len(items)))
    assert " open" not in re.search(r"<details[^>]*jd-runlog[^>]*>", markup).group(0)
    with_jobs = {}
    for item in items:
        assert item.startswith("<button"), item
        visit = re.search(r'data-visit="(\d+)"', item).group(1)
        assert '<span class="name">' in item.split("</button>")[0], item
        meta = re.search(r'</button><div class="exp-jobs">.*?</div><details class="exp-more" data-key="runlog-jobs:([^"]*)"><summary>([^<]*)</summary>'
                         r'(<table class="exp">.*</table>)</details>$', item, re.S)
        assert ('<table class="exp">' in item) == bool(meta), item
        if meta:
            assert meta.group(1) == f"{EXPOSURE_ADDRESS}:{visit}"
            with_jobs[re.search(r'data-state="([^"]*)"', item).group(1)] = meta.group(2)
    assert with_jobs == {"planning": "1 job · metadata", "building": "1 job · metadata"}
    shown = _run_log_exposures(markup)
    assert shown["planning"] == [(f"{ids['plan'][:8]} · plan", _shown(REAL))]
    assert shown["building"] == [(f"{ids['build'][:8]} · build", _shown(INDEPENDENT))]


@needs_node
def test_the_script_draws_the_same_collapsed_run_log(tmp_path: Path) -> None:
    (tmp_path / "db").mkdir()
    snapshot, _ = _ticket_jobs(tmp_path / "db", {"plan": REAL, "build": INDEPENDENT})
    views = _both(tmp_path, snapshot, EXPOSURE_ADDRESS)
    assert views["script"] == views["server"]
    assert RUN_LOG_HEAD.search(views["script"]), views["script"]
    assert views["script"].count('<details class="exp-more"') == 2


def test_the_ticket_css_lays_the_sections_out_as_one_column_at_every_width() -> None:
    assert ".jd-cols{display:flex;flex-direction:column;gap:16px}" in ticket_detail.CSS
    for gone in (".jd-right", "@container", "container-type", "[data-sec=", "{order:", "__", ".jd-runlog{flex"):
        assert gone not in ticket_detail.CSS, gone


def test_the_ticket_css_keeps_the_journey_canvas_1000px_wide_and_fits_it_on_a_phone() -> None:
    wide, phone_media, phone = ticket_detail.CSS.partition(f"@media {ticket_detail.PHONE}{{")
    assert phone_media
    assert ".jd-box canvas{display:block;width:1000px;max-width:none}" in wide
    assert ".jd-box canvas{display:block;width:1000px;max-width:none}" not in phone
    assert ".ticketpage canvas.jd-journey{width:auto;max-width:100%}" in phone


RUN_LOG_VIEW = r"""
(() => {
  const art = document.querySelector('article.ticketpage');
  const log = art.querySelector('details.jd-runlog');
  const shown = n => !!n && n.checkVisibility();
  const metas = [...log.querySelectorAll('details.exp-more')];
  return {open: log.open, height: log.getBoundingClientRect().height,
          head: log.querySelector('summary').textContent, rows: [...log.querySelectorAll('.runlog .visit')].map(shown),
          metaOpen: metas.map(d => d.open), tables: [...log.querySelectorAll('table.exp')].map(shown),
          rowsWithMeta: metas.map(d => shown(d.parentNode.querySelector('.visit')))};
})()"""


def _run_log_expanded(chrome: _Chrome) -> list[bool]:
    nodes = chrome.send("Accessibility.getFullAXTree", chrome.session)["nodes"]
    return [{p["name"]: p["value"].get("value") for p in node.get("properties", [])}.get("expanded")
            for node in nodes if node.get("role", {}).get("value") == "DisclosureTriangle"
            and node.get("name", {}).get("value", "").startswith("Run log")]


def _press(chrome: _Chrome, key: str, code: int) -> None:
    for kind in ("keyDown", "keyUp"):
        chrome.send("Input.dispatchKeyEvent", chrome.session, type=kind, key=key, code=key,
                    windowsVirtualKeyCode=code, **({"text": "\r"} if kind == "keyDown" and key == "Enter" else {}))


@pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")
def test_the_run_log_opens_by_key_and_stays_open_through_a_live_redraw(tmp_path: Path) -> None:
    (tmp_path / "db").mkdir()
    snapshot, _ = _ticket_jobs(tmp_path / "db", {"plan": REAL, "build": INDEPENDENT})
    records = threading.Event()
    server = _serve(tmp_path, {"fixture": snapshot}, records_gate=records)
    base = f"http://127.0.0.1:{server.server_address[1]}/fixture/"
    chrome = _Chrome(tmp_path / "profile")
    views = {}
    try:
        chrome.send("Emulation.setDeviceMetricsOverride", chrome.session, width=1440, height=900,
                    deviceScaleFactor=1, mobile=False)
        _open(chrome, f"{base}ticket.html#t={_quote(EXPOSURE_ADDRESS)}",
              "document.querySelector('article.ticketpage details.jd-runlog')")
        views["start"] = chrome.run(RUN_LOG_VIEW)
        views["start", "ax"] = _run_log_expanded(chrome)
        chrome.run("document.querySelector('article.ticketpage details.jd-runlog > summary').focus()")
        _press(chrome, "Enter", 13)
        views["keyed"] = chrome.run(RUN_LOG_VIEW)
        views["keyed", "ax"] = _run_log_expanded(chrome)
        chrome.run("window.oldTicket = document.querySelector('article.ticketpage');"
                   "oldTicket.querySelector('details.exp-more > summary').click()")
        views["meta"] = chrome.run(RUN_LOG_VIEW)
        records.set()
        chrome.wait_for("!window.oldTicket.isConnected", "the records redraw")
        views["redrawn"] = chrome.run(RUN_LOG_VIEW)
        chrome.run("document.querySelector('article.ticketpage details.jd-runlog > summary').click()")
        views["closed"] = chrome.run(RUN_LOG_VIEW)
    finally:
        records.set()
        chrome.close()
        server.shutdown()
    start = views["start"]
    assert start["open"] is False and start["height"] < 80, start
    assert start["head"] == "Run log" + f"{len(start['rows'])} visits · oldest first", start
    assert not any(start["rows"]) and not any(start["tables"]), start
    assert views["start", "ax"] == [False], views["start", "ax"]
    keyed = views["keyed"]
    assert keyed["open"] is True and all(keyed["rows"]), keyed
    assert views["keyed", "ax"] == [True], views["keyed", "ax"]
    assert keyed["metaOpen"] == [False, False] and not any(keyed["tables"]) and all(keyed["rowsWithMeta"]), keyed
    assert views["meta"]["metaOpen"] == [True, False] and views["meta"]["tables"] == [True, False], views["meta"]
    redrawn = views["redrawn"]
    assert redrawn["open"] is True and redrawn["metaOpen"] == [True, False] and all(redrawn["rows"]), redrawn
    assert views["closed"]["open"] is False and not any(views["closed"]["rows"]), views["closed"]


def _with_oks(tmp_path: Path, address: str, outcomes: list[tuple[str, str]]) -> dict:
    from lib.dashboard import collect_snapshot
    from tests.test_dashboard import seeded
    from tests.test_dashboard_layout import _history

    repo, _, ticket_id = address.partition("#")
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _history(connection)
        connection.executemany(
            "INSERT INTO handled_comments (repo, ticket_id, comment_id, outcome) VALUES (?, ?, ?, ?)",
            [(repo, ticket_id, comment_id, outcome) for comment_id, outcome in outcomes],
        )
    return collect_snapshot(db, log, now=NOW)


@needs_node
def test_each_ok_outcome_shows_on_its_ticket_page(tmp_path: Path) -> None:
    address = LIVE["running"][0]
    outcomes = [("ok-1", "hold-ok"), ("approve-1", "merged"), ("ok-2", "ok-no-hold"), ("ok-3", "rejected-hold-ok-author")]
    snapshot = _with_oks(tmp_path, address, outcomes)
    for render, view in _both(tmp_path, snapshot, address).items():
        chips = re.findall(r'<span class="chip muted ok">([^<]*)</span>', view)
        assert chips == ["ok: rejected-hold-ok-author", "ok: ok-no-hold", "ok: hold-ok"], render
    other = _both(tmp_path, snapshot, LIVE["queued"][0])
    assert all('class="chip muted ok"' not in view for view in other.values())


@needs_node
def test_tickets_whose_shown_repos_cut_alike_keep_their_own_ok_outcomes(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from tests.test_dashboard import add_ticket, seeded

    db, log = seeded(tmp_path)
    shared = "o" * 39 + "/" + "r" * 70
    outcomes = {shared + "a": "hold-ok", shared + "b": "ok-no-hold"}
    with sqlite3.connect(db) as connection:
        for repo, outcome in outcomes.items():
            add_ticket(connection, repo, "1", state="waiting-max")
            connection.execute(
                "INSERT INTO handled_comments (repo, ticket_id, comment_id, outcome) VALUES (?, ?, ?, ?)",
                (repo, "1", "ok-1", outcome),
            )
    snapshot = collect_snapshot(db, log, now=NOW)
    for index, (repo, outcome) in enumerate(outcomes.items()):
        out = tmp_path / str(index)
        out.mkdir()
        for render, view in _both(out, snapshot, repo + "#1").items():
            chips = re.findall(r'<span class="chip muted ok">([^<]*)</span>', view)
            assert chips == ["ok: " + outcome], (render, repo)


def _two_qa_rounds(tmp_path: Path) -> tuple[Path, dict[int, str]]:
    from lib import exposure
    from tests.qa_support import HEAD_A, HEAD_B, Harness, qa_config, qa_report, ready, scenarios, verdict

    run = Harness(tmp_path, qa_config())
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("q1", qa_report(HEAD_A, "PASS"))
    run.finish("qa", "q1")
    run.poll()
    run.post("v1", verdict(HEAD_A, "REVISE"))
    run.poll()
    run.finish("claude", "r2")
    run.post("r2", ready(HEAD_B, "2/3", body=scenarios("qa-1")), head=HEAD_B)
    run.poll()
    rounds = dict(run.rows("SELECT round, job_id FROM qa_runs WHERE arm = 'primary' ORDER BY round"))
    assert sorted(rounds) == [1, 2]
    with run.queue.connect() as connection:
        exposure.ensure_table(connection)
        for number, job_id in rounds.items():
            exposure.write(connection, job_id, Exposure(f"qa-model-{number}", "cli", "a" * 64, (), "host", "b" * 40,
                                                        "high", "claude"), NOW)
    claimed = run.queue.claim("qa")
    assert claimed is not None and claimed.id == rounds[2]
    return run.queue.path, rounds


def _qa_round_views(tmp_path: Path, db: Path, *, script: bool) -> list[str]:
    from lib.dashboard import collect_snapshot

    snapshot = collect_snapshot(db, tmp_path / "missing.log", now=1_800_000_003.0, alive=None)
    if not script:
        return [_ticket_server(snapshot, "maxharris1/factory#763")]
    views = _both(tmp_path, snapshot, "maxharris1/factory#763")
    assert views["script"] == views["server"]
    return list(views.values())


def _assert_qa_rounds(markup: str, rounds: dict[int, str], *, running: bool) -> None:
    shown = [job for jobs in _run_log_models(markup).values() for job in jobs]
    qa = sorted((job, model) for job, action, model in shown if action == "qa")
    assert qa == sorted((rounds[n][:8], f"round {n} · qa-model-{n} · high") for n in (1, 2))
    if running:
        facts = dict(_exposure_facts(markup))
        assert (facts["review round"], facts["model"], facts["effort"]) == ("2", "qa-model-2", "high")


@pytest.mark.parametrize("script", [False, pytest.param(True, marks=needs_node)], ids=["server", "script"])
def test_each_qa_job_s_model_names_its_review_round(tmp_path: Path, script: bool) -> None:
    from lib.queue import SQLiteJobQueue

    (tmp_path / "db").mkdir()
    db, rounds = _two_qa_rounds(tmp_path / "db")
    for markup in _qa_round_views(tmp_path / "running", db, script=script):
        _assert_qa_rounds(markup, rounds, running=True)
    SQLiteJobQueue(db, clock=lambda: 1_800_000_002.0).finish(rounds[2], owner="qa", handoff_comment_id="q2")
    for markup in _qa_round_views(tmp_path / "finished", db, script=script):
        _assert_qa_rounds(markup, rounds, running=False)
