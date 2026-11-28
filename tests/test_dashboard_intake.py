from __future__ import annotations

import re
from pathlib import Path

import pytest

from lib.dashboard import render_pages
from lib.dashboard_pages import lanes
from lib.dashboard_page import intake_groups
from lib.dashboard_pages.lanes import FUNNEL, INTAKE, IntakeGroups, lanes_table
from tests.test_dashboard_lanes import (
    COLUMNS, EMPTY, Browser, FOLD, _assert_columns, _intake_item, _page_with_status_stub, _six_and_nine, _tiles,
    needs_chrome,
)
from tests.test_dashboard_layout import BAR, NODE, _no_script, _run_page, _section
from tests.test_dashboard_shell import AT_SNAPSHOT

LABELS = ["Labelled ready", "Waiting", "Next in line", "Fed today", "Into the factory"]
RENDERS = ["server", "script"]


@pytest.fixture
def browser():
    chrome = Browser()
    yield chrome
    chrome.close()


def _render(render: str, tmp_path: Path, snapshot: dict, actions: list | None = None) -> str:
    if render == "server":
        return _section(_no_script(render_pages(snapshot)["index.html"]), "intake")
    if NODE is None:
        pytest.skip("node is not installed")
    return _run_page(tmp_path, snapshot, "main", BAR + ["hud", "intake"], actions=actions)["html"]["intake"]


def _assert_funnel(intake: str, counts: tuple[int, ...], split: tuple[int, int]) -> None:
    assert intake.count('<ol class="funnel" id="feeder">') == 1, intake
    funnel = re.search(r'<ol class="funnel" id="feeder">(.*?)</ol>', intake, re.S).group(1)
    stages = re.findall(r'<li class="fstage">(.*?)</li>', funnel, re.S)
    assert len(stages) == 5, funnel
    assert [re.search(r'<span class="flabel">([^<]*)</span>', s).group(1) for s in stages] == LABELS
    assert [int(re.search(r'<b class="fnum">(\d+)</b>', s).group(1)) for s in stages] == list(counts)
    blocked, capacity = split
    bar = (f'<span class="fbar" aria-hidden="true"><i class="fb-blocked" style="flex-grow:{blocked}"></i>'
           f'<i class="fb-capacity" style="flex-grow:{capacity}"></i></span>')
    legend = f'<span class="fsplit"><span>{blocked} on a blocker or files</span><span>{capacity} for capacity</span></span>'
    assert bar in stages[1] and legend in stages[1], stages[1]
    assert [s for s in stages if "fbar" in s or "fsplit" in s] == [stages[1]]
    assert stages[4].startswith('<a href="#counts">') and stages[4].endswith("</a>"), stages[4]
    assert funnel.count("<a ") == 1
    assert "imeter" not in intake and "intake-meter" not in intake
    assert '<span class="ilabel">Feeder</span>' not in intake


@pytest.mark.parametrize("render", RENDERS)
def test_the_funnel_reads_the_five_stages_in_order(tmp_path: Path, render: str) -> None:
    _assert_funnel(_render(render, tmp_path, _tiles(tmp_path)), (11, 5, 2, 2, 3), (3, 2))


@pytest.mark.parametrize("render", RENDERS)
@pytest.mark.parametrize(
    ("change", "counts", "split"),
    [
        ({"waiting_blocked": 0, "waiting_capacity": 0}, (11, 0, 0, 2, 3), (0, 0)),
        ({"waiting_blocked": None}, (11, 2, 2, 2, 3), (0, 2)),
    ],
    ids=["empty", "no-blocked-key"],
)
def test_an_empty_waiting_stage_draws_an_empty_track(
    tmp_path: Path, render: str, change: dict, counts: tuple, split: tuple
) -> None:
    snapshot = _tiles(tmp_path)
    for key, value in change.items():
        if value is None:
            del snapshot["feeder"][key]
        else:
            snapshot["feeder"][key] = value
    _assert_funnel(_render(render, tmp_path, snapshot), counts, split)


def test_the_script_keeps_no_copy_of_the_stage_table() -> None:
    assert lanes_table()["funnel"] == FUNNEL
    assert [stage["label"] for stage in FUNNEL] == LABELS
    for words in ("Labelled ready", "Next in line", "Into the factory", "on a blocker or files", "#counts"):
        assert words not in lanes.SCRIPT, words
    assert "imeter" not in lanes.CSS and "imeter" not in lanes.SCRIPT


@pytest.mark.parametrize("render", RENDERS)
def test_the_feeder_id_follows_the_feeder(tmp_path: Path, render: str) -> None:
    snapshot = _tiles(tmp_path)
    assert render_pages(snapshot)["index.html"].count('id="feeder"') == 1
    assert _render(render, tmp_path, snapshot).count('id="feeder"') == 1
    snapshot["feeder"] = None
    page = render_pages(snapshot)["index.html"]
    assert 'id="feeder"' not in page
    intake = _render(render, tmp_path, snapshot)
    assert 'id="feeder"' not in intake and "funnel" not in intake
    assert 'id="linear-webhook"' in intake


def _open_server(browser: Browser, snapshot: dict, width: int) -> None:
    from lib.dashboard_page import app_css

    page = Path(browser.tmp.name) / "page.html"
    page.write_text(
        _no_script(render_pages(snapshot)["index.html"]).replace(
            '<link rel="stylesheet" href="/app.css">', f"<style>{app_css()}</style>", 1
        ),
        encoding="utf-8",
    )
    browser.call("Emulation.setDeviceMetricsOverride", width=width, height=FOLD, deviceScaleFactor=1, mobile=False)
    browser.call("Page.navigate", url=page.as_uri())
    browser.eval("""new Promise(function (done) { (function wait() {
      if (document.readyState === 'complete' && document.getElementById('feeder')) done(true);
      else setTimeout(wait, 50); })(); })""")


def _open(browser: Browser, snapshot: dict, width: int, render: str = "script") -> None:
    if render == "script":
        browser.open(_page_with_status_stub(snapshot, "index.html"), width)
    else:
        _open_server(browser, snapshot, width)


LAYOUT = """(function () {
  var f = document.getElementById('feeder').getBoundingClientRect();
  var box = document.querySelector('#intake .intake').getBoundingClientRect();
  var stages = [].map.call(document.querySelectorAll('#feeder > .fstage'), function (s) {
    var b = s.getBoundingClientRect(); return {left: Math.round(b.left), top: Math.round(b.top)}; });
  return {funnel: [f.left, f.right], intake: [box.left, box.right], stages: stages,
          scroll: document.documentElement.scrollWidth};
})()"""


@needs_chrome
@pytest.mark.parametrize(("width", "lefts", "tops"), [(760, 5, 1), (759, 1, 5)])
def test_the_funnel_is_one_row_from_760_px_and_one_column_below(
    tmp_path: Path, browser: Browser, width: int, lefts: int, tops: int
) -> None:
    _open(browser, _tiles(tmp_path), width)
    box = browser.eval(LAYOUT)
    assert len(box["stages"]) == 5, box
    assert len({s["left"] for s in box["stages"]}) == lefts, box
    assert len({s["top"] for s in box["stages"]}) == tops, box
    assert abs(box["funnel"][0] - box["intake"][0]) <= 0.5, box
    assert abs(box["funnel"][1] - box["intake"][1]) <= 0.5, box
    assert box["scroll"] <= width, box


@needs_chrome
@pytest.mark.parametrize("render", RENDERS)
def test_the_funnel_does_not_scroll_a_phone_sideways(tmp_path: Path, browser: Browser, render: str) -> None:
    _open(browser, _tiles(tmp_path), 375, render)
    box = browser.eval(LAYOUT)
    assert len({s["left"] for s in box["stages"]}) == 1, box
    assert box["scroll"] <= 375, f"phone page scrolls sideways: {box}"


BAR_WIDTHS = """(function () {
  function w(sel) { return document.querySelector('#feeder ' + sel).getBoundingClientRect().width; }
  return {bar: w('.fbar'), blocked: w('.fb-blocked'), capacity: w('.fb-capacity')};
})()"""


@needs_chrome
def test_the_waiting_bar_splits_by_its_two_numbers(tmp_path: Path, browser: Browser) -> None:
    snapshot = _tiles(tmp_path)
    _open(browser, snapshot, 1440)
    widths = browser.eval(BAR_WIDTHS)
    assert widths["bar"] > 0, widths
    assert abs(widths["blocked"] - widths["bar"] * 3 / 5) <= 1, widths
    assert abs(widths["capacity"] - widths["bar"] * 2 / 5) <= 1, widths
    snapshot["feeder"].update(waiting_blocked=0, waiting_capacity=0)
    _open(browser, snapshot, 1440)
    widths = browser.eval(BAR_WIDTHS)
    assert widths["bar"] > 0 and widths["blocked"] == 0 and widths["capacity"] == 0, widths


@needs_chrome
def test_into_the_factory_links_to_the_count_strip(tmp_path: Path, browser: Browser) -> None:
    snapshot = _tiles(tmp_path)
    assert render_pages(snapshot)["index.html"].count('id="counts"') == 1
    _open(browser, snapshot, 1440)
    assert browser.eval("document.querySelectorAll('[id=counts]').length") == 1
    hash_ = browser.eval("document.querySelector('#feeder a').click(), location.hash")
    assert hash_ == "#counts"


HEADING = ('<h2><span>Intake</span><span class="count" id="linear-webhook">'
           "Linear webhook · last received 30s ago (Issue) · dirty queue 0</span></h2>")
BARE_HEADING = "<h2><span>Intake</span></h2>"
OPEN = '<section class="panel" id="intake" aria-label="Intake">'
TROUBLE = ('<ul class="itrouble">'
           '<li><span class="ticket">factory#3</span><span class="what">feed error, retrying</span></li>'
           '<li><span class="ticket">factory#6</span><span class="what">no acceptance criteria</span></li></ul>')
WARNING = "no webhook for 2h"
QUIET = "quiet: no Linear webhook for 2h 10m (nothing missed by the last reconcile)"


def _inner(render: str, intake: str) -> str:
    if render == "server":
        assert intake.startswith(OPEN) and intake.endswith("</section>"), intake
        return intake[len(OPEN) : -len("</section>")]
    return intake


def _assert_header_trouble_and_titles(intake: str) -> None:
    assert intake.count(HEADING) == 1, intake
    assert intake.index(HEADING) < intake.index('<div class="intake">')
    for gone in ("irow", "ilabel", "itext", "what bad", "waiting · "):
        assert gone not in intake, gone
    assert intake.count('class="itrouble"') == 1
    assert intake.index("</ol>") < intake.index(TROUBLE) < intake.index('<div class="igroup">'), intake
    assert intake.index(TROUBLE) < intake.index("</div>", intake.index('<div class="intake">'))
    assert intake.count('class="ititle"') == 2, intake
    for issue, title in ((2, "Header line"), (1, "Funnel row")):
        assert (f'<span class="ticket">factory#{issue}</span>'
                f'<span class="ititle" title="{title}">{title}</span>') in intake
    assert '<span class="ticket">factory#4</span><span class="what">waiting for capacity</span>' in intake


@pytest.mark.parametrize("render", RENDERS)
def test_the_header_holds_the_webhook_line_and_trouble_is_one_list(tmp_path: Path, render: str) -> None:
    _assert_header_trouble_and_titles(_render(render, tmp_path, _tiles(tmp_path)))


@pytest.mark.parametrize("render", RENDERS)
def test_a_webhook_with_no_feeder_says_no_feeder_has_run(tmp_path: Path, render: str) -> None:
    snapshot = _tiles(tmp_path)
    snapshot["feeder"] = None
    snapshot["poller"]["linear_webhook"].update(warning=WARNING, note=QUIET)
    assert _inner(render, _render(render, tmp_path, snapshot)) == (
        HEADING + f'<div class="intake"><p class="bad">warning: {WARNING}</p><p class="muted">{QUIET}</p>'
        '<p class="empty">no feeder has run</p></div>'
    )


@pytest.mark.parametrize("render", RENDERS)
def test_a_feeder_with_no_webhook_has_no_count_and_no_webhook_id(tmp_path: Path, render: str) -> None:
    snapshot = _tiles(tmp_path)
    del snapshot["poller"]["linear_webhook"]
    inner = _inner(render, _render(render, tmp_path, snapshot))
    assert inner.startswith(BARE_HEADING + '<div class="intake"><ol class="funnel" id="feeder">'), inner
    assert 'class="count"' not in inner and "linear-webhook" not in inner and "Linear webhook" not in inner
    marks = ["</ol>" + TROUBLE + "</div>", '<div class="icols">', '<div class="igroup">Next in line</div>',
             '<div class="igroup">Waiting on a blocker or files</div>', '<div class="igroup">Fed today</div>']
    at = [inner.index(mark) for mark in marks]
    assert at == sorted(at), inner


@pytest.mark.parametrize("render", RENDERS)
def test_neither_half_keeps_the_empty_panel_byte_for_byte(tmp_path: Path, render: str) -> None:
    snapshot = _tiles(tmp_path)
    snapshot["feeder"] = None
    del snapshot["poller"]["linear_webhook"]
    empty = BARE_HEADING + '<div class="intake"><p class="empty">no listener or feeder has run</p></div>'
    if render == "server":
        assert _render(render, tmp_path, snapshot) == OPEN + empty + "</section>"
    else:
        assert _render(render, tmp_path, snapshot) == empty


@pytest.mark.parametrize("render", RENDERS)
def test_no_trouble_draws_no_trouble_line(tmp_path: Path, render: str) -> None:
    snapshot = _tiles(tmp_path)
    items = snapshot["feeder"]["items"]
    snapshot["feeder"]["items"] = [x for x in items if x["state"] not in ("error", "refused-intake")]
    intake = _render(render, tmp_path, snapshot)
    assert 'id="feeder"' in intake and "itrouble" not in intake


LONG_TITLE = ("Move the Linear webhook line into the Intake heading and draw trouble as one red line "
              "under the funnel, with titles")[:120]
LONG_REASON = ("refused at intake: the ticket names lib/dashboard_pages/lanes.py, lib/dashboard_static/js/lanes.js "
               "and lib/dashboard_static/css/lanes.css but has no acceptance criteria, no risk line and no estimate, "
               "so the feeder will not feed it until a person edits it")


def _long(tmp_path: Path) -> dict:
    snapshot = _tiles(tmp_path)
    for item in snapshot["feeder"]["items"]:
        item["title"] = LONG_TITLE
        if item["state"] == "refused-intake":
            item["reason"] = LONG_REASON
    return snapshot


TROUBLE_LAYOUT = """(function () {
  var ul = document.querySelector('#intake .itrouble');
  var probe = document.createElement('span'); probe.style.color = 'var(--bad)'; ul.parentNode.appendChild(probe);
  var bad = getComputedStyle(probe).color; probe.remove();
  var css = getComputedStyle(ul);
  return {display: css.display, wrap: css.flexWrap, before: ul.previousElementSibling.id,
          inBox: ul.parentNode.className, bad: bad,
          colors: [].map.call(ul.querySelectorAll('li, li span'), function (n) { return getComputedStyle(n).color; }),
          tops: [].map.call(ul.children, function (li) { return Math.round(li.getBoundingClientRect().top); })};
})()"""


@needs_chrome
@pytest.mark.parametrize("render", RENDERS)
def test_trouble_is_one_wrapping_red_line_under_the_funnel(tmp_path: Path, browser: Browser, render: str) -> None:
    _open(browser, _tiles(tmp_path), 1440, render)
    box = browser.eval(TROUBLE_LAYOUT)
    assert box["display"] == "flex" and box["wrap"] == "wrap", box
    assert box["before"] == "feeder" and box["inBox"] == "intake", box
    assert len(box["tops"]) == 2 and len(set(box["tops"])) == 1, box
    assert box["colors"] and set(box["colors"]) == {box["bad"]}, box


TITLES = """(function () {
  var titles = [].map.call(document.querySelectorAll('#intake .ititle'), function (t) {
    var css = getComputedStyle(t);
    return {lines: Math.round(t.getBoundingClientRect().height / parseFloat(css.lineHeight)),
            cut: t.scrollWidth > t.clientWidth, space: css.whiteSpace, overflow: css.textOverflow,
            title: t.getAttribute('title'), text: t.textContent};
  });
  var trouble = [].map.call(document.querySelectorAll('#intake .itrouble li'), function (li) {
    return li.getClientRects().length && Math.round(li.getBoundingClientRect().height); });
  return {titles: titles, trouble: trouble, scroll: document.documentElement.scrollWidth};
})()"""


@needs_chrome
@pytest.mark.parametrize(("width", "space", "cut"), [(760, "nowrap", True), (759, "normal", False)])
def test_a_long_title_is_one_cut_line_from_760_px_and_wraps_below(
    tmp_path: Path, browser: Browser, width: int, space: str, cut: bool
) -> None:
    _open(browser, _long(tmp_path), width)
    box = browser.eval(TITLES)
    assert len(box["titles"]) == 7, box
    for title in box["titles"]:
        assert title["space"] == space and title["cut"] is cut, title
        assert title["overflow"] == "ellipsis" and title["title"] == title["text"] == LONG_TITLE, title
        assert title["lines"] == 1 or not cut, title
    assert box["scroll"] <= width, box


@needs_chrome
@pytest.mark.parametrize("render", RENDERS)
def test_a_long_title_and_a_long_reason_do_not_scroll_a_phone_sideways(
    tmp_path: Path, browser: Browser, render: str
) -> None:
    _open(browser, _long(tmp_path), 375, render)
    box = browser.eval(TITLES)
    assert len(box["titles"]) == 7, box
    assert all(t["lines"] > 1 and not t["cut"] for t in box["titles"]), box
    assert len(box["trouble"]) == 2 and box["trouble"][1] > box["trouble"][0], box
    assert box["scroll"] <= 375, f"phone page scrolls sideways: {box}"


GROUP_ITEMS = [
    _intake_item(1, "done", fed_age_seconds=900),
    _intake_item(2, "done", fed_age_seconds=300),
    _intake_item(3, "fed", fed_age_seconds=900),
    _intake_item(4, "done", fed_age_seconds=86400),
    _intake_item(5, "done"),
    _intake_item(6, "error", fed_age_seconds=60),
    _intake_item(7, "waiting-capacity"),
    _intake_item(8, "waiting-blocked", reason="waiting on #7"),
    _intake_item(9, "waiting-capacity"),
    _intake_item(10, "refused-intake", reason="no acceptance criteria"),
    _intake_item(11, "fed", fed_age_seconds=86399),
    _intake_item(12, "eligible"),
]
GROUPS = [["6", "10"], ["7", "9"], ["8"], ["2", "1", "3", "11"]]


def test_intake_groups_returns_four_lists() -> None:
    groups = intake_groups(GROUP_ITEMS)
    assert [[x["issue"] for x in group] for group in groups] == GROUPS
    trouble, next_, blocked, fed = groups
    assert [len(trouble), len(next_), len(blocked), len(fed)] == [2, 2, 1, 4]
    assert groups.fed is fed and groups.next is next_


@pytest.mark.parametrize("render", RENDERS)
def test_the_columns_hold_the_same_tickets(tmp_path: Path, render: str) -> None:
    snapshot = _tiles(tmp_path)
    snapshot["feeder"]["items"] = [dict(x) for x in GROUP_ITEMS]
    intake = _render(render, tmp_path, snapshot)
    trouble = re.search(r'<ul class="itrouble">(.*?)</ul>', intake, re.S).group(1)
    assert re.findall(r"factory#(\d+)<", trouble) == GROUPS[0]
    _assert_columns(intake, GROUPS[1:], [None, None, None])


@pytest.mark.parametrize("render", RENDERS)
def test_each_column_shows_four_rows_and_then_a_more_line(tmp_path: Path, render: str) -> None:
    snapshot = _six_and_nine(tmp_path)
    intake = _render(render, tmp_path, snapshot, AT_SNAPSHOT)
    _assert_columns(intake, [["21", "22", "23", "24"], ["2", "5", "8"], ["35", "32", "34", "31"]],
                    ["+2 more", None, "+5 more today"])
    assert ('<li><span class="ticket">factory#21</span><span class="ititle" title="Next row">Next row</span>'
            '<span class="chip accent">tier 2</span><span class="what">waiting for capacity</span></li>') in intake
    assert ('<li><span class="ticket">factory#2</span><span class="ititle" title="Header line">Header line</span>'
            '<span class="what">waiting on #1</span></li>') in intake
    assert ('<li><span class="ticket">factory#35</span><span class="ititle" title="Fed row">Fed row</span>'
            '<span class="chip accent">tier 1</span><span class="what">fed, job queued</span>'
            '<span>→ PR #135</span><span class="when">fed 1m 00s ago</span></li>') in intake
    assert '<li><span class="ticket">factory#32</span><span class="what">fed</span><span class="when">fed 5m 00s ago</span></li>' in intake
    snapshot["feeder"]["items"] += [_intake_item(issue, "waiting-blocked", reason="waiting on #21") for issue in (41, 42, 43)]
    _assert_columns(_render(render, tmp_path, snapshot), [["21", "22", "23", "24"], ["2", "5", "8", "41"],
                    ["35", "32", "34", "31"]], ["+2 more", "+2 more", "+5 more today"])


@pytest.mark.parametrize("render", RENDERS)
def test_a_column_with_no_rows_says_so(tmp_path: Path, render: str) -> None:
    snapshot = _tiles(tmp_path)
    snapshot["feeder"]["items"] = []
    _assert_columns(_render(render, tmp_path, snapshot), [[], [], []], [None, None, None])
    snapshot["feeder"]["items"] = [_intake_item(issue, "waiting-capacity") for issue in (1, 2, 3, 4)]
    intake = _render(render, tmp_path, snapshot)
    _assert_columns(intake, [["1", "2", "3", "4"], [], []], [None, None, None])
    assert 'class="more"' not in intake


def test_the_limit_comes_from_the_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(INTAKE, "rows", 2)
    snapshot = _six_and_nine(tmp_path)
    for render in RENDERS:
        _assert_columns(_render(render, tmp_path, snapshot), [["21", "22"], ["2", "5"], ["35", "32"]],
                        ["+4 more", "+1 more", "+7 more today"])


def test_the_script_keeps_no_copy_of_the_column_table() -> None:
    assert lanes_table()["intake"] == INTAKE
    assert [col["label"] for col in INTAKE["columns"]] == COLUMNS
    assert [col["empty"] for col in INTAKE["columns"]] == EMPTY
    for words in (*COLUMNS[1:], *EMPTY, "more today", "' more", " fed today", "intake-fed", "intake-waiting"):
        assert words not in lanes.SCRIPT, words
    for gone in ("fed-more", "#intake details"):
        assert gone not in lanes.CSS and gone not in lanes.SCRIPT, gone
    keys = [col["key"] for col in INTAKE["columns"]]
    assert keys == list(lanes._INTAKE_BODY) == list(IntakeGroups._fields[1:])


def test_the_fed_age_ticks_in_the_script(tmp_path: Path) -> None:
    snapshot = _six_and_nine(tmp_path)
    fed_row = r'factory#35<.*?<span class="when">([^<]*)</span>'
    assert re.search(fed_row, _render("server", tmp_path, snapshot)).group(1) == "fed 1m 00s ago"
    ticked = _render("script", tmp_path, snapshot, AT_SNAPSHOT + [["advance", 60]])
    assert re.search(fed_row, ticked).group(1) == "fed 2m 00s ago"


COLUMN_LAYOUT = """(function () {
  var cols = [].map.call(document.querySelectorAll('#intake .icols > .icol'), function (c) {
    var b = c.getBoundingClientRect(); return {left: Math.round(b.left), top: Math.round(b.top)}; });
  return {cols: cols, scroll: document.documentElement.scrollWidth};
})()"""


@needs_chrome
@pytest.mark.parametrize("render", RENDERS)
@pytest.mark.parametrize(("width", "lefts", "tops"), [(760, 3, 1), (759, 1, 3)])
def test_the_columns_are_a_row_from_760_px_and_a_stack_below(
    tmp_path: Path, browser: Browser, render: str, width: int, lefts: int, tops: int
) -> None:
    _open(browser, _six_and_nine(tmp_path), width, render)
    box = browser.eval(COLUMN_LAYOUT)
    assert len(box["cols"]) == 3, box
    assert len({c["left"] for c in box["cols"]}) == lefts, box
    assert len({c["top"] for c in box["cols"]}) == tops, box
    assert box["scroll"] <= width, box
