from __future__ import annotations

import copy
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from lib.dashboard import render_pages, render_static_assets
from lib.dashboard_page import app_css, page_script
from lib.dashboard_pages import cockpit
from lib.dashboard_pages.common import ticket_address
from tests.test_dashboard import NOW
from tests.test_dashboard_lanes import Browser, browser, needs_chrome  # noqa: F401 (pytest fixture)
from tests.test_dashboard_layout import HARNESS, _run_page, _section, _snapshot, needs_node, run_harness

ASKING = "maxharris1/tracy-harris-coaching#5"
LONG_QUESTION = (
    "Should the sign-in page offer OAuth through GitHub and Google, or an API key that people paste by hand? "
    "OAuth is quicker for most people but needs two app registrations and a callback address for every deploy. "
    "An API key works offline and in CI, but it asks people to find a secret and paste it into a form they may not "
    "trust yet. We could ship both behind a toggle, which doubles the test surface. Which one ships first?"
)
VISUAL = ["ok", "request-changes", "rerun-qa"]


def _shots(count: int) -> list[dict]:
    return [{"lane": 1, "path_parts": [f"qa-1/0{i}-checkout.png"], "scenario": f"checkout step {i}",
             "result": "verified"} for i in range(1, count + 1)]


IMAGE_AND_VIDEO = [
    {"lane": 1, "path_parts": ["qa-1/01-landing.png"], "scenario": "landing", "result": "verified"},
    {"lane": 1, "path_parts": ["qa-1/walkthrough.webm"], "kind": "video", "result": "failed"},
]
RUN = ["0123456789abcdef", "0123456789abcdef"]


def _visual(media: list[dict]) -> dict:
    return {"state": "awaiting-ok", "wait": {"why": "visual-qa-review", "comment_id": "55", "detail": None},
            "visual_qa": {"run": RUN, "media": media}, "actions": VISUAL}


def _question(text: str, options: list[str] | None = None) -> dict:
    entry = {"text": text, **({"options": options} if options else {})}
    return {"state": "waiting-max", "wait": {"why": "question", "comment_id": "77", "detail": None},
            "questions": [entry], "actions": ["answer"]}


MERGE_OK = {"state": "awaiting-ok", "wait": {"why": "awaiting-ok", "comment_id": None, "detail": None},
            "actions": ["ok"]}
OTHER = {"state": "waiting-max", "wait": {"why": "plan-size", "comment_id": None, "detail": None}, "actions": []}

INBOX = {
    "maxharris1/cleffy#41": dict(_visual(_shots(5)), source="#41", pr="42", title="Sign-in flow",
                                 state_age_seconds=4920),
    "maxharris1/tracy-harris-coaching#7": dict(_visual(IMAGE_AND_VIDEO), pr="7", title="Booking page",
                                               state_age_seconds=300),
    "maxharris1/aut0#11": dict(_question(LONG_QUESTION, ["OAuth", "API key", "Both behind a toggle"]), pr="11",
                               title="Pricing copy", state_age_seconds=2472),
    "maxharris1/factory#12": dict(_question("Keep the old route?"), pr="12", title=None, state_age_seconds=60),
    "maxharris1/notes#13": dict(_question("Export as CSV or JSON?", ["CSV", "JSON"]), pr="13",
                                title="Notes export", state_age_seconds=None),
    "maxharris1/cleffy#20": dict(MERGE_OK, pr="20", title="Billing fix", state_age_seconds=9000),
    "maxharris1/site#21": dict(MERGE_OK, pr="21", title="Footer links", state_age_seconds=120000),
    "maxharris1/billing#30": dict(OTHER, pr="30", title="Big refactor", state_age_seconds=700),
}
NULL_TITLE = "maxharris1/factory#12"
NULL_AGE = "maxharris1/notes#13"
TWIN = ("maxharris1/tracy-harris-coaching#7", "maxharris1/aut0#11", NULL_TITLE, NULL_AGE,
        "maxharris1/cleffy#20", "maxharris1/billing#30")


def _inbox(tmp_path: Path, only: tuple[str, ...] | None = None) -> dict:
    snapshot = _snapshot(tmp_path)
    assert snapshot["generated_at_epoch"] == NOW == 1800000000
    template = next(row for row in snapshot["tickets"] if ticket_address(row) == ASKING)
    rows = []
    for address, fields in INBOX.items():
        if only is not None and address not in only:
            continue
        repo, ticket_id = address.split("#")
        row = copy.deepcopy(template)
        row.update({"repo": repo, "ticket_id": ticket_id, "source": None, "record": address, **copy.deepcopy(fields)})
        rows.append(row)
    snapshot["tickets"] = [row for row in snapshot["tickets"] if ticket_address(row) != ASKING] + rows
    return snapshot


def _cards(markup: str) -> dict[str, str]:
    return {match.group(1): match.group(0)
            for match in re.finditer(r'<article\b[^>]*data-cockpit-ticket="([^"]+)".*?</article>', markup, re.S)}


def _in_order(card: str, needles: list[str]) -> None:
    at = [card.find(needle) for needle in needles]
    assert -1 not in at, dict(zip(needles, at))
    assert at == sorted(at), dict(zip(needles, at))


def test_an_inbox_card_draws_its_parts_in_order(tmp_path: Path) -> None:
    cards = _cards(cockpit._needs_max(_inbox(tmp_path)))
    _in_order(cards["maxharris1/cleffy#41"], [
        '<span class="chip accent">visual-qa-review</span>', 'class="ck-tk"', "cleffy#41 (PR #42)</a>",
        "visual QA review", '<span class="when">waited 1h 22m</span>', '<div class="ck-title">Sign-in flow</div>',
        '<div class="ck-gallery">'])
    _in_order(cards["maxharris1/aut0#11"], [
        '<span class="chip warn">waiting-max</span>', 'class="ck-tk"', "aut0#11</a>", "asks you a question",
        '<span class="when">waited 41m 12s</span>', '<div class="ck-title">Pricing copy</div>',
        '<ul class="ck-qlist">', '<ul class="ck-qopts">'])


TODAYS_ORDER = ["maxharris1/cleffy#41", "maxharris1/tracy-harris-coaching#7", "maxharris1/aut0#11", NULL_TITLE,
                NULL_AGE, "maxharris1/cleffy#20", "maxharris1/site#21", "maxharris1/billing#30"]


def test_cards_sit_under_group_headings_in_todays_order(tmp_path: Path) -> None:
    snapshot = _inbox(tmp_path)
    snapshot["tickets"].reverse()
    assert [ticket_address(row) for row in cockpit.inbox_tickets(snapshot)] == TODAYS_ORDER
    markup = cockpit._needs_max(snapshot)
    assert list(_cards(markup)) == TODAYS_ORDER
    assert _headings(markup) == [("Visual QA", "2"), ("Questions", "3"), ("Merge OK", "2"), ("Other", "1")]
    without_merge_ok = dict(snapshot, tickets=[row for row in snapshot["tickets"] if row.get("state") != "awaiting-ok"
                                               or (row.get("wait") or {}).get("why") == "visual-qa-review"])
    assert _headings(cockpit._needs_max(without_merge_ok)) == [("Visual QA", "2"), ("Questions", "3"), ("Other", "1")]
    assert "__NEEDS_MAX__" not in cockpit.SCRIPT and "NEEDS_MAX" in cockpit.SCRIPT


def _headings(markup: str) -> list[tuple[str, str]]:
    return re.findall(r'<h3 class="ck-ghead"><span>([^<]*)</span><span class="count">(\d+)</span></h3>', markup)


def _with_questions(snapshot: dict, total: int) -> dict:
    template = next(row for row in snapshot["tickets"] if ticket_address(row) == NULL_TITLE)
    asked = sum(1 for row in cockpit.inbox_tickets(snapshot) if (row.get("wait") or {}).get("why") == "question")
    for n in range(total - asked):
        snapshot["tickets"].append(dict(copy.deepcopy(template), repo="maxharris1/zeta", ticket_id=str(100 + n),
                                        record=f"maxharris1/zeta#{100 + n}", pr=str(100 + n)))
    return snapshot


def _group(markup: str, key: str) -> str:
    start = markup.index(f'<div class="ck-group" data-ck-group="{key}">')
    end = markup.find('<div class="ck-group"', start + 1)
    return markup[start:end if end >= 0 else len(markup)]


def test_a_group_past_nine_shows_nine_and_a_toggle(tmp_path: Path) -> None:
    base = _inbox(tmp_path)
    for total, more in ((11, 2), (10, 1), (9, 0)):
        group = _group(cockpit._needs_max(_with_questions(copy.deepcopy(base), total)), "question")
        shown, _, folded = group.partition("<details")
        assert len(_cards(shown)) == 9, total
        assert len(_cards(folded)) == more, total
        assert ('<details class="ck-more"><summary>show %d more</summary>' % more in group) == bool(more), total


def _script_needs_max(tmp_path: Path, snapshot: dict, actions: list | None = None) -> str:
    return _run_page(tmp_path, snapshot, "main", ["link", "needs-max"], actions=actions)["html"]["needs-max"]


@needs_node
def test_a_null_title_and_a_null_age_draw_nothing(tmp_path: Path) -> None:
    snapshot = _inbox(tmp_path)
    for render in (cockpit._needs_max(snapshot), _script_needs_max(tmp_path, snapshot)):
        cards = _cards(render)
        assert len(cards) == len(INBOX)
        assert "ck-title" not in cards[NULL_TITLE] and 'class="when"' in cards[NULL_TITLE]
        assert 'class="when"' not in cards[NULL_AGE] and "ck-title" in cards[NULL_AGE]


@needs_node
def test_the_waited_time_ticks_in_the_script(tmp_path: Path) -> None:
    snapshot = _inbox(tmp_path)
    server = _cards(cockpit._needs_max(snapshot))["maxharris1/cleffy#41"]
    assert '<span class="when">waited 1h 22m</span>' in server
    ticked = _cards(_script_needs_max(tmp_path, snapshot, [["advance", 60]]))["maxharris1/cleffy#41"]
    assert '<span class="when">waited 1h 23m</span>' in ticked


VOID = {"img", "br", "hr", "input", "meta", "link", "source", "wbr"}


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.events.append(("start", tag, tuple(sorted((k, v or "") for k, v in attrs))))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID:
            self.events.append(("end", tag))

    def handle_data(self, data: str) -> None:
        if self.events and self.events[-1][0] == "text":
            self.events[-1] = ("text", self.events[-1][1] + data)
        else:
            self.events.append(("text", data))


def _tree(markup: str) -> list[tuple]:
    parser = _Tree()
    parser.feed(markup)
    parser.close()
    return parser.events


SWAPPED = {
    "limit": 2,
    "groups": (
        {"key": "asked", "label": "Asked of you", "why": "question", "state": None},
        {"key": "screens", "label": "Screens", "why": "visual-qa-review", "state": None},
        {"key": "ship", "label": "Ship it?", "why": None, "state": "awaiting-ok"},
        {"key": "rest", "label": "Everything else", "why": None, "state": None},
    ),
}


@needs_node
@pytest.mark.parametrize("table", ["shipped", "swapped"])
def test_the_script_draws_the_same_needs_max_as_the_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           table: str) -> None:
    snapshot = _with_questions(_inbox(tmp_path, TWIN + ("maxharris1/cleffy#41", "maxharris1/site#21")), 11)
    script_js = render_static_assets(snapshot)["app.js"]
    if table == "swapped":
        shipped = json.dumps(cockpit.NEEDS_MAX, separators=(",", ":"))
        assert shipped in script_js
        script_js = script_js.replace(shipped, json.dumps(SWAPPED, separators=(",", ":")))
        monkeypatch.setattr(cockpit, "NEEDS_MAX", SWAPPED)
    section = _section(cockpit._needs_max(snapshot), "needs-max")
    server = section[section.index(">") + 1:section.rindex("</section>")]
    harness = HARNESS.replace("let nowMs = 1800000003000;", "let nowMs = 1800000000000;")
    assert harness != HARNESS
    out = run_harness(tmp_path, harness, script_js, snapshot, "main", ["link", "needs-max"], [])
    script = out["html"]["needs-max"]
    assert len(_cards(server)) == len(TWIN) + 2 + 8
    assert server.count('<div class="ck-group"') == 4
    if table == "shipped":
        assert "show 2 more" in server
    else:
        assert _headings(server)[0] == ("Asked of you", "11") and "show 9 more" in server
    assert _tree(script) == _tree(server)


SESSION_IN = "{ok: true, status: 200, json: function () { return Promise.resolve(%s); }}" % json.dumps(
    {"signed_in": True, "login": "maxharris1", "csrf": "x"})
SESSION_OUT = "{ok: false, status: 404, json: function () { return Promise.resolve(null); }}"


def _stubbed(snapshot: dict, signed_in: bool) -> str:
    shim = (
        "<script>window.fetch = function (url) {"
        f" if (url === '/api/session') return Promise.resolve({SESSION_IN if signed_in else SESSION_OUT});"
        f" window.snapshot = window.snapshot || {json.dumps(snapshot)}; return Promise.resolve({{ok: true, status: 200,"
        " json: function () { return Promise.resolve(JSON.parse(JSON.stringify(window.snapshot))); }}); };</script>"
    )
    return (
        render_pages(snapshot)["index.html"]
        .replace('<link rel="stylesheet" href="/app.css">', f"<style>{app_css()}</style>", 1)
        .replace('<script src="/app.js" defer></script>',
                 shim + f"<script>{page_script(snapshot.get('statechart'))}</script>", 1)
    )


def _wait_for_buttons(browser: Browser) -> None:
    drawn = browser.eval("""new Promise(function (done) { var tries = 0; (function wait() {
      if (document.querySelector('#needs-max [data-ck-act]') || ++tries > 100) done(tries <= 100);
      else setTimeout(wait, 20); })(); })""")
    assert drawn, "the signed-in session never drew the action buttons"


@needs_chrome
@pytest.mark.parametrize(("width", "columns"), [(1200, 3), (1199, 2), (760, 2), (759, 1)])
def test_the_grid_is_three_two_then_one_column(tmp_path: Path, browser: Browser, width: int,
                                               columns: int) -> None:
    browser.open(_stubbed(_inbox(tmp_path), signed_in=False), width)
    box = browser.eval("""({lefts: [].map.call(document.querySelectorAll('#needs-max .ck-grid > .ck-card'),
        function (c) { return Math.round(c.getBoundingClientRect().left); }),
      scroll: document.documentElement.scrollWidth, viewport: innerWidth})""")
    assert len(box["lefts"]) == len(INBOX)
    assert len(set(box["lefts"])) == columns, box
    assert box["scroll"] <= box["viewport"], box


@needs_chrome
def test_a_long_question_shows_three_lines_and_its_options(tmp_path: Path, browser: Browser) -> None:
    browser.open(_stubbed(_inbox(tmp_path), signed_in=False), 1440)
    box = browser.eval("""(function () {
      var card = document.querySelector('#needs-max [data-cockpit-ticket="maxharris1/aut0#11"]');
      var text = card.querySelector('.ck-qtext'), opts = card.querySelector('.ck-qopts');
      return {scroll: text.scrollHeight, client: text.clientHeight,
              line: parseFloat(getComputedStyle(text).lineHeight),
              opts: opts.getBoundingClientRect().height, items: opts.querySelectorAll('li').length};
    })()""")
    assert box["scroll"] > box["client"], box
    assert abs(box["client"] - 3 * box["line"]) <= 1, box
    assert box["opts"] > 0 and box["items"] == 3, box


@needs_chrome
def test_screenshots_are_one_strip_that_scrolls_sideways(tmp_path: Path, browser: Browser) -> None:
    browser.open(_stubbed(_inbox(tmp_path), signed_in=False), 1440)
    box = browser.eval("""(function () {
      var gallery = document.querySelector('#needs-max [data-cockpit-ticket="maxharris1/cleffy#41"] .ck-gallery');
      return {tops: [].map.call(gallery.querySelectorAll('.ck-media'),
                function (m) { return Math.round(m.getBoundingClientRect().top); }),
              scroll: gallery.scrollWidth, client: gallery.clientWidth,
              page: document.documentElement.scrollWidth, viewport: innerWidth};
    })()""")
    assert len(box["tops"]) == 5 and len(set(box["tops"])) == 1, box
    assert box["scroll"] > box["client"], box
    assert box["page"] <= box["viewport"], box


BUTTONS = {
    "maxharris1/cleffy#41": VISUAL, "maxharris1/tracy-harris-coaching#7": VISUAL,
    "maxharris1/aut0#11": ["answer"], NULL_TITLE: ["answer"], NULL_AGE: ["answer"],
    "maxharris1/cleffy#20": ["ok"], "maxharris1/site#21": ["ok"], "maxharris1/billing#30": [],
}


@needs_chrome
@pytest.mark.parametrize("width", [1440, 900])
def test_each_card_keeps_its_buttons_and_a_rows_button_bars_line_up(tmp_path: Path, browser: Browser,
                                                                    width: int) -> None:
    snapshot = _inbox(tmp_path)
    browser.open(_stubbed(snapshot, signed_in=True), width)
    _wait_for_buttons(browser)
    cards = browser.eval("""[].map.call(document.querySelectorAll('#needs-max .ck-grid > .ck-card'), function (c) {
      var bar = c.querySelector(':scope > .ck-actions');
      return {ticket: c.getAttribute('data-cockpit-ticket'), top: Math.round(c.getBoundingClientRect().top),
              acts: [].map.call(c.querySelectorAll('[data-ck-act]'), function (b) { return b.getAttribute('data-ck-act'); }),
              bottom: bar ? bar.getBoundingClientRect().bottom : null};
    })""")
    assert {card["ticket"]: card["acts"] for card in cards} == BUTTONS
    rows: dict[int, list[float]] = {}
    for card in cards:
        if card["bottom"] is not None:
            rows.setdefault(card["top"], []).append(card["bottom"])
    assert len(rows) >= 2, cards
    for top, bottoms in rows.items():
        assert max(bottoms) - min(bottoms) <= 0.5, (top, cards)
    browser.open(_stubbed(snapshot, signed_in=False), width)
    signed_out = browser.eval("""new Promise(function (done) { setTimeout(function () { done({
      cards: document.querySelectorAll('#needs-max .ck-grid > .ck-card').length,
      buttons: document.querySelectorAll('#needs-max [data-ck-act], #needs-max .ck-actions').length}); }, 300); })""")
    assert signed_out == {"cards": len(INBOX), "buttons": 0}


def _poll(browser: Browser, change: str = "") -> None:
    replaced = browser.eval("""new Promise(function (done) {
      var before = document.querySelector('#needs-max .ck-card'), tries = 0;
      %s
      window.snapshot.generated_at_epoch += 1;
      (function wait() { if (!before.isConnected || ++tries > 200) done(tries <= 200); else setTimeout(wait, 20); })();
    })""" % change)
    assert replaced, "no poll redrew the panel"


@needs_chrome
def test_show_more_reveals_the_rest_and_stays_open_across_polls(tmp_path: Path, browser: Browser) -> None:
    browser.open(_stubbed(_with_questions(_inbox(tmp_path), 11), signed_in=False), 1440)
    shown = """[].filter.call(document.querySelectorAll('#needs-max [data-ck-group="question"] .ck-card'),
      function (c) { return c.checkVisibility(); }).length"""
    assert browser.eval(shown) == 9
    browser.eval("""document.querySelector('#needs-max [data-ck-group="question"] summary').focus(),
      document.querySelector('#needs-max [data-ck-group="question"] summary').click()""")
    assert browser.eval(shown) == 11
    _poll(browser)
    assert browser.eval(shown) == 11
    assert browser.eval("""document.activeElement.tagName + ' ' + document.activeElement.textContent""") == \
        "SUMMARY show 2 more"
