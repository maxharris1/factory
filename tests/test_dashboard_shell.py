"""The global shell on every page (#139, Mission Control T2): the TopBar with
its pixel caret, the header band, the attention strip and the status line.

Artboards: docs/design/mission-control/reference/artboards/TopBar.dc.html,
HeaderBand.dc.html and StatusLine.dc.html. Every piece has a server render and
a page-script twin; the node tests run the real page script (the harness in
tests/test_dashboard_layout.py) and compare its markup with the server's.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import lib.dashboard_page as page
from lib.attention import ensure_table
from lib.dashboard import collect_snapshot, render_pages
from lib.dashboard_pages.common import ticket_page_href
from lib.dashboard_pages.ticket_detail import ticket_view
from lib.health import CODE_COMPONENTS
from tests.test_dashboard import NOW, add_ticket, make_db, seeded
from tests import test_dashboard_activity as activity
from tests import test_dashboard_layout as layout
from tests import test_dashboard_system as system
from tests.test_dashboard_layout import BAR, JOB, _no_script, _run_page, _section, _snapshot, _text, needs_node
from tests.test_dashboard_pixelstrip import _node
from tests.test_dashboard_lanes import FOLD, Browser, _page_with_status_stub, needs_chrome

PAGES = ("index.html", "job.html", "activity.html", "efficiency.html", "health.html", "ticket.html")
SCRIPT_PAGE = {"index.html": "main", "job.html": "job", "activity.html": "activity",
               "efficiency.html": "efficiency", "health.html": "health", "ticket.html": "ticket"}
NAV = [("index.html", "Lanes"), ("job.html", "Tickets"), ("activity.html", "Activity"),
       ("efficiency.html", "Efficiency"), ("health.html", "System")]
SHELL = ["caret", "band", "attention-strip", "statusline"]
# The harness clock starts 3 s after the snapshot; this puts it back, so
# every ticking age reads as the server wrote it.
AT_SNAPSHOT = [["advance", -3]]
REVIEW_PAGE = ticket_page_href({"repo": "maxharris1/cleffy", "ticket_id": "40"})
REVIEW_HASH = REVIEW_PAGE.removeprefix("ticket.html")


@pytest.fixture
def base(tmp_path: Path) -> dict:
    return collect_snapshot(*seeded(tmp_path), now=NOW)


def _inner(html: str, ident: str) -> str:
    whole = _footer(html) if ident == "statusline" else _section(_no_script(html), ident)
    return whole[whole.index(">") + 1 : whole.rindex("<")]


def _footer(html: str) -> str:
    match = re.search(r'<footer class="statusline" id="statusline" aria-label="Status line">.*?</footer>', html, flags=re.S)
    assert match
    return match.group(0)


def _script(tmp_path: Path, snapshot: dict, name: str = "index.html", actions: list | None = None,
            ids: list[str] | None = None) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _run_page(tmp_path, snapshot, SCRIPT_PAGE[name], ids or BAR,
                     actions=AT_SNAPSHOT if actions is None else actions)


def _segments(snapshot: dict) -> dict[str, dict]:
    return {seg["key"]: seg for seg in page.status_segments(snapshot)}


# --- 1. the attention strip ------------------------------------------------------------


SYNTHETIC = ["stale", "down", "stale", "no-waiter", "down", "paused", "dead", "stale", "down", "stale", "health",
             "needs-attention", "stale"]
COMPONENT_KINDS = {"down", "no-waiter", "health"}


def _synthetic(kind: str, i: int) -> dict:
    if kind in COMPONENT_KINDS:
        return {"kind": kind, "repo": "", "ticket_id": "", "component": f"poller @ dev{i}",
                "link": "health.html#health-poller-box", "detail": f"{kind} for {i}m"}
    return {"kind": kind, "repo": "maxharris1/cleffy", "ticket_id": str(100 + i), "detail": f"{kind} {i}"}


def _items(base: dict, count: int) -> list[dict]:
    seeded_items = copy.deepcopy(base["attention"]["items"])
    extra = [_synthetic(SYNTHETIC[i % len(SYNTHETIC)], i) for i in range(max(0, count - len(seeded_items)))]
    return (seeded_items + extra)[:count]


def _with(base: dict, items: list[dict]) -> dict:
    out = copy.deepcopy(base)
    out["attention"] = {"count": len(items), "items": items}
    return out


WORST = [("stuck", "aut0#AUT-7", ticket_page_href({"repo": "aut0-code/aut0", "ticket_id": "AUT-7"})),
         ("overdue", "cleffy#14", ticket_page_href({"repo": "maxharris1/cleffy", "ticket_id": "14"}))]
STRIPS = {
    1: ([("1 stuck", "bad")], None, "1 stuck"),
    2: ([("1 stuck", "bad"), ("1 overdue", "bad")], None, "1 stuck · 1 overdue"),
    3: ([("1 stuck", "bad"), ("1 overdue", "bad"), ("1 paused", "warn")], "+1 more →", "1 stuck · 1 overdue · +1"),
    16: ([("3 down", "bad"), ("1 stuck", "bad"), ("1 overdue", "bad"), ("1 no-waiter", "bad"), ("1 dead", "bad"),
          ("1 health", "bad"), ("1 needs-attention", "bad"), ("5 stale", "warn"), ("2 paused", "warn")],
         "+14 more →", "5 stale · 3 down · +8"),
}
ITEM = re.compile(
    r'<div class="attn-item" data-attn="[^"]+"><span class="chip ([a-z-]+)">([^<]+)</span>'
    r'<a class="attn-who" href="([^"]+)">([^<]+)</a><span class="attn-what">[^<]*</span></div>')


@pytest.mark.parametrize("name", PAGES)
@pytest.mark.parametrize("count", sorted(STRIPS))
def test_the_strip_is_one_row_with_a_tally_and_the_worst_two(base: dict, count: int, name: str) -> None:
    snapshot = _with(base, _items(base, count))
    html = render_pages(snapshot)[name]
    strip = _section(_no_script(html), "attention-strip")
    head, inner = strip[: strip.index(">") + 1], _inner(html, "attention-strip")
    assert head == '<div class="attn" id="attention-strip" role="region" aria-label="Needs attention">'
    need = "needs" if count == 1 else "need"
    tally, more, short = STRIPS[count]
    if name == "activity.html":
        assert inner == (
            f'<div class="attn-here"><span class="attn-blk" aria-hidden="true">'
            f'{page.px_strip("attention", state="attention", height=16)}</span>'
            f'<a class="attn-count" href="#attention"><b id="attention-count">{count}</b><span> {need} attention</span></a>'
            '<span class="attn-below">listed below</span></div>')
    else:
        row, phone = re.fullmatch(r'(<div class="attn-row">.*</div>)(<a class="attn-phone".*</a>)', inner).groups()
        assert row.startswith(
            '<div class="attn-row"><div class="attn-head"><span class="attn-blk" aria-hidden="true">'
            f'{page.px_strip("attention", state="attention", height=16)}</span>'
            f'<a class="attn-count" href="activity.html#attention"><b id="attention-count">{count}</b>'
            f'<span> {need} attention</span></a></div><div class="attn-tally">')
        spans = '<span aria-hidden="true">·</span>'.join(
            f'<span class="attn-n {tone}">{text}</span>' for text, tone in tally)
        assert f'<div class="attn-tally">{spans}</div><span class="attn-rule" aria-hidden="true"></span>' in row
        assert [(kind, who, href) for _, kind, href, who in ITEM.findall(row)] == WORST[:count]
        assert [tone for tone, *_ in ITEM.findall(row)] == ["bad"] * min(2, count)
        if more is None:
            assert "attn-more" not in row
            assert row.endswith("</div></div>")
        else:
            assert row.endswith(f'<a class="attn-more" href="activity.html#attention">{more}</a></div>')
        assert f'<b>{count} {need} attention</b><span class="attn-short">{short}</span>' in phone


@needs_node
@pytest.mark.parametrize("name", PAGES)
@pytest.mark.parametrize("count", sorted(STRIPS))
def test_the_script_strip_is_the_server_strip_at_every_count(tmp_path: Path, base: dict, count: int, name: str) -> None:
    snapshot = _with(base, _items(base, count))
    out = _script(tmp_path, snapshot, name)
    assert out["html"]["attention-strip"] == _inner(render_pages(snapshot)[name], "attention-strip")
    assert out["hidden"]["attention-strip"] is False


def test_the_seeded_lanes_strip_shows_the_two_bad_items_before_the_warn(base: dict) -> None:
    strip = _inner(render_pages(base)["index.html"], "attention-strip")
    assert [(kind, who, href) for _, kind, href, who in ITEM.findall(strip)] == WORST
    assert "tracy-harris-coaching#5" not in strip
    assert '<a class="attn-more" href="activity.html#attention">+1 more →</a>' in strip


def _kinds(*kinds: str, ages: list | None = None) -> list[dict]:
    rows = [{"kind": kind, "repo": "o/r", "ticket_id": str(i), "detail": ""} for i, kind in enumerate(kinds)]
    for row, age in zip(rows, ages or []):
        if age is not None:
            row["age_seconds"] = age
    return rows


def _ids_of(rows: list[dict]) -> list[str]:
    return [row["ticket_id"] for row in rows]


def test_the_tally_puts_bad_before_warn_then_the_larger_count_first() -> None:
    model = page.strip_model(_kinds("stale", "stale", "stale", "stuck", "down", "down"))
    assert model["tally"] == [("down", 2, "bad"), ("stuck", 1, "bad"), ("stale", 3, "warn")]
    assert (model["count"], model["need"], model["more"]) == (6, " need attention", 4)


def test_a_warn_item_listed_first_loses_to_a_later_bad_item() -> None:
    assert _ids_of(page.strip_model(_kinds("paused", "stale", "stuck"))["worst"]) == ["2", "0"]


def test_aged_items_order_oldest_first_within_a_tone() -> None:
    rows = _kinds("stuck", "stuck", "stuck", "paused", ages=[60, None, 600, 9000])
    assert _ids_of(page.strip_model(rows)["worst"]) == ["2", "0"]
    rows = _kinds("stuck", "stuck", "stuck", ages=[True, float("nan"), 5])
    assert _ids_of(page.strip_model(rows)["worst"]) == ["2", "0"]


def test_without_ages_the_worst_keep_snapshot_order() -> None:
    assert _ids_of(page.strip_model(_kinds("stale", "down", "dead", "stuck"))["worst"]) == ["1", "2"]
    assert page.strip_model(_kinds("stale"))["need"] == " needs attention"


def test_short_and_label_read_the_artboard_mix() -> None:
    model = page.strip_model(_kinds("down", "stuck", "no-waiter", "stale", "down"))
    assert model["short"] == "2 down · 1 stuck · +2"
    assert model["label"] == "5 need attention: 2 down, 1 stuck, 1 no-waiter, 1 stale. Open Activity."
    one = page.strip_model(_kinds("stale", "stale"))
    assert (one["short"], one["label"]) == ("2 stale", "2 need attention: 2 stale. Open Activity.")


@needs_node
def test_the_phone_tally_names_the_two_largest_kinds_in_both_renders(tmp_path: Path, base: dict) -> None:
    snapshot = _with(base, _kinds("stuck", "overdue", "paused", "paused", "paused"))
    strip = _inner(render_pages(snapshot)["index.html"], "attention-strip")
    assert '<span class="attn-short">3 paused · 1 stuck · +1</span>' in strip
    assert 'aria-label="5 need attention: 1 stuck, 1 overdue, 3 paused. Open Activity."' in strip
    assert _script(tmp_path, snapshot)["html"]["attention-strip"] == strip


def test_activity_shows_the_count_only_and_holds_the_anchor(base: dict) -> None:
    html = _no_script(render_pages(_with(base, _items(base, 16)))["activity.html"])
    strip = _section(html, "attention-strip")
    assert '<a class="attn-count" href="#attention">' in strip
    assert "listed below" in strip
    assert 'data-px-key="attention"' in strip
    for gone in ("attn-n", "attn-item", "attn-more", "attn-phone", "attn-row"):
        assert gone not in strip, gone
    assert 'id="attention"' in html


def test_the_phone_line_is_one_link_to_activity(base: dict) -> None:
    html = render_pages(_with(base, _items(base, 3)))["index.html"]
    phone = re.search(r'<a class="attn-phone".*?</a>', _inner(html, "attention-strip")).group(0)
    assert phone == (
        '<a class="attn-phone" href="activity.html#attention" '
        'aria-label="3 need attention: 1 stuck, 1 overdue, 1 paused. Open Activity.">'
        f'<span class="attn-blk" aria-hidden="true">{page.px_strip("attention-phone", state="attention", height=16)}'
        '</span><b>3 need attention</b><span class="attn-short">1 stuck · 1 overdue · +1</span>'
        '<span class="attn-go" aria-hidden="true">→</span></a>')


STRIP = """(function () {
  var strip = document.getElementById('attention-strip');
  function shown(sel) { var n = strip.querySelector(sel); return n && getComputedStyle(n).display !== 'none' ? n : null; }
  function box(n) { return n ? n.getBoundingClientRect() : null; }
  var row = shown('.attn-row'), what = row && row.querySelector('.attn-what'), more = row && row.querySelector('.attn-more');
  return {strip: box(strip).height,
          row: row && {fits: row.scrollHeight === row.clientHeight,
                       centers: new Set([].concat.apply([], [].map.call(row.children, function (n) {
                         return getComputedStyle(n).display === 'contents' ? [].slice.call(n.children) : [n];
                       })).map(function (n) {
                         var b = n.getBoundingClientRect(); return Math.round(b.top + b.bottom); })).size},
          more: more && box(more).right,
          tally: row && [].map.call(row.querySelectorAll('.attn-tally'), function (n) { return n.scrollWidth <= n.clientWidth; })[0],
          ellipsis: what && [what.scrollWidth > what.clientWidth, getComputedStyle(what).textOverflow],
          details: row && [].map.call(row.querySelectorAll('.attn-what'), function (n) { return box(n).width; }),
          items: row && [].map.call(row.querySelectorAll('.attn-item'), function (item) {
            var r = box(row), chip = box(item.querySelector('.chip')), who = item.querySelector('.attn-who'),
                at = box(who), detail = box(item.querySelector('.attn-what'));
            var hit = document.elementFromPoint((at.left + at.right) / 2, (at.top + at.bottom) / 2);
            var text = who.firstChild, last = document.createRange();
            last.setStart(text, text.length - 1);
            last.setEnd(text, text.length);
            var end = last.getBoundingClientRect();
            function inside(b) { return b.width > 0 && b.left >= r.left && b.right <= r.right; }
            return {chip: inside(chip), who: inside(at) && end.left >= at.left && end.right <= at.right + 0.5,
                    clickable: !!hit && who.contains(hit), detail: inside(detail) && detail.width >= 20,
                    whole: who.scrollWidth <= who.clientWidth};
          }),
          phone: shown('.attn-phone') && box(shown('.attn-phone')).height,
          here: shown('.attn-here') && box(shown('.attn-here')).height,
          sideways: document.documentElement.scrollWidth > innerWidth,
          moving: document.getAnimations().filter(function (a) {
            return a.effect && a.effect.target && strip.contains(a.effect.target); }).length};
})()"""


@needs_chrome
def test_the_strip_is_one_36px_row_at_every_count_and_width_in_a_browser(base: dict) -> None:
    browser = Browser()
    try:
        for count in (1, 2, 3, 16, 50):
            snapshot = _with(base, _items(base, count))
            snapshot["attention"]["items"][0]["detail"] = "a detail far too long for one row " * 12
            html = _page_with_status_stub(_now(snapshot), "index.html")
            for width in (700, 1100, 1440):
                browser.open(html, width)
                got = browser.eval(STRIP)
                where = (count, width)
                assert got["strip"] == 36, where
                assert got["row"] == {"fits": True, "centers": 1}, where
                assert got["phone"] is None and got["here"] is None, where
                assert got["ellipsis"] == [True, "ellipsis"], where
                drawn = {"chip": True, "who": True, "clickable": True, "detail": True, "whole": True}
                assert got["items"] == [drawn] * min(2, count), f"each worst item shows its chip, subject and detail: {where}"
                if count > 2:
                    assert got["more"] <= width, where
                if not got["tally"]:
                    assert max(got["details"]) <= 40, f"the tally is cut only once each detail is at its floor: {where}"
                if width >= 1440 and count <= 16:
                    assert got["tally"] is True, f"a long detail must not squeeze the tally: {where}"
                assert (got["sideways"], got["moving"]) == (False, 0), where
            for width in (375, 699):
                browser.open(html, width)
                got = browser.eval(STRIP)
                assert (got["row"], got["phone"], got["strip"]) == (None, 36, 36), (count, width)
                assert (got["sideways"], got["moving"]) == (False, 0), (count, width)
        html = _page_with_status_stub(_now(_with(base, _items(base, 16))), "activity.html")
        for width in (375, 1440):
            browser.open(html, width)
            got = browser.eval(STRIP)
            assert (got["here"], got["strip"], got["row"], got["phone"]) == (36, 36, None, None), width
            assert got["moving"] == 0, width
    finally:
        browser.close()


COLLECTED = {
    "two waiting-max tickets": [("maxharris1/tracy-harris-coaching", t, "paused") for t in ("5", "6")],
    "three waiting-max tickets": [("maxharris1/tracy-harris-coaching", t, "paused") for t in ("5", "6", "7")],
    "fifty with two stuck": [("maxharris1/a-repository-name-longer-than-most", t, "stuck") for t in ("1201", "1202")]
                            + [("maxharris1/tracy-harris-coaching", str(t), "paused") for t in range(100, 148)],
    "three ship-failed tickets": [("maxharris1/tracy-harris-coaching", t, "ship-failed") for t in ("5", "6", "7")],
    "fifty ship-failed tickets": [("maxharris1/a-repository-name-longer-than-most", str(t), "ship-failed")
                                  for t in range(1200, 1250)],
}


def _collected(tmp_path: Path, tickets: list[tuple[str, str, str]]) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        ensure_table(connection)
        for repo, ticket, how in tickets:
            if how == "ship-failed":
                add_ticket(connection, repo, ticket)
                connection.execute("INSERT INTO needs_attention VALUES (?, ?, ?, ?, ?)",
                                   (repo, ticket, "ship-failed", "merge failed", NOW))
            else:
                add_ticket(connection, repo, ticket, state="waiting-max", paused_action="clarify",
                           stuck=int(how == "stuck"), stuck_reason="review loop" if how == "stuck" else None)
    return _now(collect_snapshot(db, tmp_path / "poller.log", now=NOW))


@needs_chrome
@pytest.mark.parametrize("case", sorted(COLLECTED))
def test_both_worst_items_stay_inside_the_row_with_long_collected_subjects(tmp_path: Path, case: str) -> None:
    snapshot = _collected(tmp_path, COLLECTED[case])
    assert len(snapshot["attention"]["items"]) == len(COLLECTED[case])
    html = _page_with_status_stub(snapshot, "index.html")
    browser = Browser()
    try:
        for width in (700, 1100, 1440):
            browser.open(html, width)
            got = browser.eval(STRIP)
            where = (case, width)
            assert got["strip"] == 36, where
            assert got["row"] == {"fits": True, "centers": 1}, where
            shown = [{k: v for k, v in item.items() if k != "whole"} for item in got["items"]]
            drawn = {"chip": True, "who": True, "clickable": True, "detail": True}
            assert shown == [drawn, drawn], f"both items keep chip, subject end and detail inside the row: {where}"
            if len(COLLECTED[case]) > 2:
                assert got["more"] is not None and got["more"] <= width, f"the +N more link stays inside the row: {where}"
            if not all(item["whole"] for item in got["items"]):
                assert (got["tally"], max(got["details"]) <= 40) == (False, True), f"subjects are cut last: {where}"
            if width >= 1440:
                assert [item["whole"] for item in got["items"]] == [True, True], where
            assert (got["sideways"], got["moving"]) == (False, 0), where
    finally:
        browser.close()


def _clear(snapshot: dict) -> dict:
    clear = copy.deepcopy(snapshot)
    clear["attention"] = {"count": 0, "items": []}
    return clear


def test_the_strip_is_absent_when_the_count_is_zero(base: dict) -> None:
    for name, html in render_pages(_clear(base)).items():
        assert '<div class="attn" id="attention-strip" role="region" aria-label="Needs attention" hidden></div>' in html, name
        assert "attention-count" not in _no_script(html)
        assert 'data-px-key="attention"' not in html


@needs_node
@pytest.mark.parametrize("name", PAGES)
def test_the_script_strip_is_the_server_strip_on_every_page(tmp_path: Path, base: dict, name: str) -> None:
    html = render_pages(base)[name]
    out = _script(tmp_path / "busy", base, name)
    assert out["html"]["attention-strip"] == _inner(html, "attention-strip")
    assert out["hidden"]["attention-strip"] is False
    clear = _clear(base)
    out = _script(tmp_path / "clear", clear, name)
    assert out["html"]["attention-strip"] == ""
    assert out["hidden"]["attention-strip"] is True


def _health_rows(*rows: dict, publish: float | None = 15) -> dict:
    return {
        "known": True, "error": None, "code_version": "abc1234def", "no_waiter": [], "down": 0,
        "stale_code": 0, "stale": 0, "publish_seconds": publish,
        "thresholds": {"beat_seconds": 15.0, "probe_seconds": 60.0, "stale_beats": 3, "down_beats": 8},
        "components": [dict(_row(), **row) for row in rows],
    }


def _row() -> dict:
    return {"component": "poller", "device": "box", "bot": "", "pid": 1, "state": "up", "reason": "",
            "last_seen_et": "x", "last_seen_age_seconds": 0, "interval_seconds": 15.0, "configured": True,
            "started_at_et": None, "started_at": None, "version": "abc1234def", "stale_code": False,
            "label": "poller @ box", "anchor": "health-poller-box"}


@needs_node
def test_the_script_strip_adds_what_an_overdue_snapshot_ages(tmp_path: Path, base: dict) -> None:
    writer = copy.deepcopy(base)
    writer["health"] = _health_rows(
        {}, {"component": "dashboard", "label": "dashboard @ box", "anchor": "health-dashboard-box"})
    out = _script(tmp_path / "writer", writer, "activity.html", actions=[["advance", 8 * 15 + 1]],
                  ids=BAR + ["attention"])
    assert out["text"]["attention-count"] == "4"
    assert "dashboard @ box" in out["text"]["attention"]
    out = _script(tmp_path / "writer-lanes", writer, "index.html", actions=[["advance", 8 * 15 + 1]])
    strip = out["html"]["attention-strip"]
    assert out["text"]["attention-count"] == "4"
    assert '<span class="attn-n bad">1 down</span>' in strip
    assert strip.count('class="attn-item"') == 2
    assert '<a class="attn-more" href="activity.html#attention">+2 more →</a>' in strip

    lone = copy.deepcopy(base)
    lone["health"] = _health_rows({}, publish=None)
    out = _script(tmp_path / "lone", lone, "index.html", actions=[["advance", 3 * 15 + 1]])
    assert out["text"]["attention-count"] == "4"
    assert '<span class="attn-n bad">1 health</span>' in out["html"]["attention-strip"]


@needs_node
def test_a_hostile_component_link_falls_back_to_the_system_page(tmp_path: Path, base: dict) -> None:
    hostile = copy.deepcopy(base)
    hostile["attention"] = {"count": 1, "items": [
        {"kind": "down", "repo": "", "ticket_id": "", "component": "<b>x</b>",
         "link": "javascript:alert(1)", "detail": "down \"now\""}]}
    html = render_pages(hostile)["index.html"]
    strip = _inner(html, "attention-strip")
    assert '<a class="attn-who" href="health.html">&lt;b&gt;x&lt;/b&gt;</a>' in strip
    assert "javascript" not in strip
    assert _script(tmp_path, hostile)["html"]["attention-strip"] == strip


# --- 2. the status line ----------------------------------------------------------------

def _devices(snapshot: dict) -> dict:
    out = copy.deepcopy(snapshot)
    out["slots"]["devices"] = [
        {"name": "box", "slot_max": 3, "held": 0, "enabled": True, "placeholder": False, "harnesses": []},
        {"name": "pc", "slot_max": 6, "held": 5, "enabled": True, "placeholder": False, "harnesses": []},
        {"name": "mac", "slot_max": 4, "held": 0, "enabled": False, "placeholder": False, "harnesses": []},
        {"name": "spare", "slot_max": 2, "held": 0, "enabled": True, "placeholder": True, "harnesses": []},
        {"name": "lab", "slot_max": None, "held": 1, "enabled": True, "placeholder": False, "harnesses": []},
    ]
    out["running"][0]["device"] = "pc"
    out["running"][1]["device"] = "attic"
    return out


def _capacity(snapshot: dict) -> dict:
    out = _devices(snapshot)
    out["slots"]["devices"] = [d for d in out["slots"]["devices"] if d["name"] != "lab"]
    out["running"][1]["device"] = "box"
    return out


def _set(path: str, value: object):
    def apply(snapshot: dict) -> dict:
        out = copy.deepcopy(snapshot)
        *head, last = path.split(".")
        node = out
        for key in head:
            node = node[key]
        node[last] = value
        return out
    return apply


def _webhook(age: int | None, warning: str | None = None):
    return _set("poller.linear_webhook", {
        "last_received_age_seconds": age, "last_event": "Issue", "listener_age_seconds": 30,
        "dirty": 0, "warning": warning, "note": None})


def _feeder(*states: str):
    return _set("feeder", {"eligible": 2, "fed": 11, "waiting_capacity": 0, "waiting_blocked": 0,
                           "items": [{"repo": "o/r", "issue": n, "state": s, "reason": None, "pr": None}
                                     for n, s in enumerate(states)]})


def _health(*rows: dict, code: str = "abc1234def", known: bool = True, error: str | None = None):
    def apply(snapshot: dict) -> dict:
        out = copy.deepcopy(snapshot)
        out["health"] = _health_rows(*rows)
        out["health"].update(code_version=code, known=known, error=error)
        return out
    return apply


def _waiter(n: int, **extra: object) -> dict:
    return {"component": "waiter", "bot": f"bot{n}", "label": f"waiter bot{n} @ box",
            "anchor": f"health-waiter-bot{n}-box", **extra}


# (case id, how to change the base snapshot, segment key, text, state, link)
SEGMENTS = [
    ("devices", _devices, "device:box", "box 0/3", None, None),
    ("device-held", _devices, "device:pc", "pc 5/6", None, None),
    ("device-disabled", _devices, "device:mac", "mac off", None, None),
    ("device-placeholder", _devices, "device:spare", "spare off", None, None),
    ("device-no-slot-max", _devices, "device:lab", "lab 1/—", None, None),
    ("device-unlisted", _devices, "device:attic", "attic 1/—", None, None),
    ("queue", _set("queue.totals.queued", 1234), "queue", "queue 1,234", None, None),
    ("slots", _capacity, "slots", "slots 6/9", None, None),
    ("slots-unknown-capacity", _devices, "slots", "slots 7/—", None, None),
    ("slots-no-slot-max", lambda s: s, "slots", "slots 2/—", None, None),
    ("poll-ok", _set("poller.health", {"state": "ok", "last_cycle_age_seconds": 50}),
     "poll", "poll ok · 50s", "ok", "health.html#health"),
    ("poll-late", _set("poller.health", {"state": "late", "last_cycle_age_seconds": 200}),
     "poll", "poll late · 3m 20s", "warn", "health.html#health"),
    ("poll-stale", _set("poller.health", {"state": "stale", "last_cycle_age_seconds": 4000}),
     "poll", "poll stale · 1h 06m", "bad", "health.html#health"),
    ("poll-unknown", _set("poller.health", None), "poll", "poll unknown", "muted", "health.html#health"),
    ("poll-errors-0", _set("poller.errors.last_hour", 0), "poll-errors", "0 poll errors / 1 h", "ok",
     "health.html#health"),
    ("poll-errors-1", _set("poller.errors.last_hour", 1), "poll-errors", "1 poll error / 1 h", "bad",
     "health.html#health"),
    ("poll-errors-bad", _set("poller.errors.last_hour", 45), "poll-errors", "45 poll errors / 1 h", "bad",
     "health.html#health"),
    ("linear-ok", _set("poller.linear_budget", None), "linear", "linear ok", "ok", "health.html#health"),
    ("linear-deferring", _set("poller.linear_budget.state", "deferring"), "linear", "linear deferring",
     "warn", "health.html#health"),
    ("linear-exhausted", _set("poller.linear_budget.state", "exhausted"), "linear", "linear exhausted",
     "bad", "health.html#health"),
    ("linear-rate-limited", _set("poller.linear_budget.state", "rate_limited"), "linear",
     "linear rate limited", "bad", "health.html#health"),
    ("webhook-ok", _webhook(371), "webhook", "webhook 6m 11s", "ok", "index.html#linear-webhook"),
    ("webhook-warning", _webhook(371, "no event for 6m"), "webhook", "webhook 6m 11s", "bad",
     "index.html#linear-webhook"),
    ("webhook-never", _webhook(None), "webhook", "webhook never", "warn", "index.html#linear-webhook"),
    ("webhook-never-warning", _webhook(None, "listener down"), "webhook", "webhook never", "bad",
     "index.html#linear-webhook"),
    ("webhook-absent", _set("poller.linear_webhook", None), "webhook", "no webhook", "muted",
     "index.html#intake"),
    ("feeder-ok", _feeder("fed", "eligible"), "feeder", "feeder 11 fed", "ok", "index.html#feeder"),
    ("feeder-error", _feeder("fed", "error"), "feeder", "feeder 11 fed", "bad", "index.html#feeder"),
    ("feeder-absent", _set("feeder", None), "feeder", "no feeder", "muted", "index.html#intake"),
    ("components-up", _health({}, _waiter(1)), "components", "2 / 2 up", "ok", "health.html#components"),
    ("components-stale", _health({}, _waiter(1, state="stale")), "components", "1 / 2 up", "warn",
     "health.html#components"),
    ("components-down", _health({}, _waiter(1, state="down"), _waiter(2, state="stale")), "components",
     "1 / 3 up", "bad", "health.html#components"),
    ("components-not-configured", _health({}, _waiter(1, state="down", configured=False)), "components",
     "1 / 1 up", "ok", "health.html#components"),
    ("components-unknown", _health(known=False), "components", "components unknown", "muted",
     "health.html#components"),
    ("components-unreadable", _health(known=False, error="cannot read orch.health.db"), "components",
     "components unknown", "bad", "health.html#components"),
    ("version-current", _health({}, _waiter(1)), "version", "abc1234 · 0 behind", "ok",
     "health.html#drift"),
    ("version-behind", _health({"stale_code": True, "version": "0ld0ld0ld"},
                               _waiter(1, stale_code=True, version="0ld0ld0ld"), _waiter(2)),
     "version", "abc1234 · 2 behind", "warn", "health.html#drift"),
    ("version-unknown-checkout", _health({}, code="unknown"), "version", "version unknown", "warn",
     "health.html#drift"),
    ("version-unknown-component", _health({"version": "unknown"}, _waiter(1)), "version",
     "abc1234 · 0 behind", "ok", "health.html#drift"),
]


@pytest.mark.parametrize(("case", "change", "key", "text", "state", "href"), SEGMENTS,
                         ids=[row[0] for row in SEGMENTS])
def test_each_segment_is_computed_from_the_snapshot(
    base: dict, case: str, change, key: str, text: str, state: str | None, href: str | None,
) -> None:
    snapshot = change(base)
    seg = _segments(snapshot)[key]
    shown = text if seg["label"] is None else f'{seg["label"]} {seg["text"]}'
    assert (shown, seg["state"], seg["href"]) == (text, state, href), case
    footer = _footer(render_pages(snapshot)["index.html"])
    if state is None:
        short = ' data-short="q"' if key == "queue" else ""
        assert f'data-seg="{key}"><span class="sl-k"{short}>{seg["label"]}</span><span class="sl-v">{seg["text"]}' in footer
    else:
        assert (f'<a class="sl-vital {state}" href="{href}" data-seg="{key}"><span class="blk" '
                f'aria-hidden="true"></span><span>{text}</span><span class="vh">, {state}</span></a>') in footer


@needs_node
@pytest.mark.parametrize(("case", "change"), [(row[0], row[1]) for row in SEGMENTS],
                         ids=[row[0] for row in SEGMENTS])
def test_the_script_status_line_is_the_server_status_line(tmp_path: Path, base: dict, case: str, change) -> None:
    snapshot = change(base)
    out = _script(tmp_path, snapshot)
    assert out["html"]["statusline"] == _inner(render_pages(snapshot)["index.html"], "statusline"), case


@needs_node
def test_the_script_ticks_the_ages_and_ages_the_components(tmp_path: Path, base: dict) -> None:
    snapshot = _webhook(371)(_set("poller.health", {"state": "ok", "last_cycle_age_seconds": 50})(
        _health({}, _waiter(1))(base)))
    out = _script(tmp_path, snapshot, actions=[["advance", 10 * 15]])
    line = out["html"]["statusline"]
    assert "<span>poll ok · 3m 23s</span>" in line  # 50 s + 3 s + 150 s
    assert "<span>webhook 8m 44s</span>" in line
    # The snapshot is overdue, so the page can no longer see either component.
    assert ('<a class="sl-vital warn" href="health.html#components" data-seg="components">'
            '<span class="blk" aria-hidden="true"></span><span>0 / 2 up</span><span class="vh">, warn</span></a>') in line


def _all_ok(snapshot: dict) -> dict:
    return _webhook(30)(_feeder("fed")(_health({}, _waiter(1))(_set("poller.errors.last_hour", 0)(
        _set("poller.health", {"state": "ok", "last_cycle_age_seconds": 12})(snapshot)))))


def _none_ok(snapshot: dict) -> dict:
    return _set("poller.linear_budget.state", "exhausted")(_health(known=False, code="unknown")(snapshot))


@pytest.mark.parametrize(("change", "count"), [
    (lambda s: s, "2 ok"),
    (_all_ok, "7 ok"),
    (_health({}, _waiter(1, state="down")), "2 ok"),
])
def test_one_ok_count_stands_for_the_ok_vitals_and_links_to_system(base: dict, change, count: str) -> None:
    segments = page.status_segments(change(base))
    *vitals, last = [seg for seg in segments if seg["state"] is not None]
    assert (last["key"], last["text"], last["state"], last["href"]) == ("ok-count", count, "ok", "health.html#health")
    assert last["text"] == f'{sum(seg["state"] == "ok" for seg in vitals)} ok'


def test_no_ok_count_when_no_vital_is_ok(base: dict) -> None:
    segments = page.status_segments(_none_ok(base))
    assert "ok" not in [seg["state"] for seg in segments]
    assert "ok-count" not in [seg["key"] for seg in segments]


def test_the_server_line_holds_every_width_form_with_the_age_segment_hidden(base: dict) -> None:
    footer = _footer(render_pages(_capacity(base))["index.html"])
    keys = re.findall(r'data-seg="([^"]+)"', footer)
    assert keys == ["device:box", "device:pc", "device:mac", "device:spare", "slots", "queue", "age",
                    "poll", "poll-errors", "linear", "webhook", "feeder", "components", "version", "ok-count"]
    assert ('<span class="sl-fill"></span><span class="sl-age" data-seg="age" hidden><span class="blk" aria-hidden="true">'
            '</span><span class="sl-t" data-short=""></span></span><a class="sl-vital') in footer


@needs_node
@pytest.mark.parametrize("change", [_all_ok, _none_ok, _capacity], ids=["all-ok", "none-ok", "capacity"])
def test_the_script_line_is_the_server_line_with_and_without_ok_vitals(tmp_path: Path, base: dict, change) -> None:
    snapshot = change(base)
    assert _script(tmp_path, snapshot)["html"]["statusline"] == _inner(render_pages(snapshot)["index.html"], "statusline")


def test_the_line_never_wraps_and_folds_by_width_in_css() -> None:
    css = page.CSS
    rule = re.search(r"\.statusline\{[^}]*\}", css).group(0)
    assert "flex-wrap:nowrap" in rule and "flex-wrap:wrap" not in rule


LINE = """(function () {
  var line = document.getElementById('statusline'), box = line.getBoundingClientRect();
  var shown = [].filter.call(line.children, function (n) { return n.getClientRects().length > 0; });
  var segs = shown.filter(function (n) { return n.hasAttribute('data-seg'); });
  return {segs: segs.map(function (n) { return n.getAttribute('data-seg'); }),
          rows: new Set(shown.map(function (n) { return Math.round(n.getBoundingClientRect().top); })).size,
          height: box.height,
          sideways: document.documentElement.scrollWidth > innerWidth,
          padding: getComputedStyle(segs[0]).padding,
          floor: Math.round(box.bottom) === innerHeight};
})()"""
WIDE = ["device:box", "device:pc", "device:mac", "queue", "poll", "poll-errors", "linear", "webhook",
        "feeder", "components", "version"]
NARROW = ["slots", "queue", "components", "ok-count"]


def _now(snapshot: dict, age: float = 0) -> dict:
    out = copy.deepcopy(snapshot)
    out["generated_at_epoch"] = time.time() - age
    return out


def _phone_fit(snapshot: dict) -> dict:
    out = _health({}, _waiter(1, state="down"), _waiter(2))(_all_ok(snapshot))
    out["slots"]["devices"] = [
        {"name": name, "slot_max": cap, "held": held, "enabled": True, "placeholder": False, "harnesses": []}
        for name, cap, held in (("box", 6, 2), ("pc", 10, 4), ("mac", 4, 1))]
    out["running"] = []
    out["queue"]["totals"]["queued"] = 1
    return out


@needs_chrome
def test_the_status_line_is_one_row_at_every_width_in_a_browser(base: dict) -> None:
    html = _page_with_status_stub(_now(_phone_fit(base)), "index.html")
    browser = Browser()
    try:
        for width, segs, padding in (
                (1440, WIDE, "6px 9px"), (1280, WIDE, "6px 9px"), (1100, WIDE, "6px 9px"),
                (1099, NARROW, "6px 9px"), (800, NARROW, "6px 9px"),
                (700, NARROW, "6px 9px"), (699, NARROW, "6px 7px"), (375, NARROW, "6px 7px")):
            browser.open(html, width)
            browser.eval("scrollTo(0, document.documentElement.scrollHeight)")
            line = browser.eval(LINE)
            assert line["segs"] == segs, width
            assert (line["rows"], line["height"], line["sideways"]) == (1, 32, False), width
            assert (line["padding"], line["floor"]) == (padding, True), width
            ok = browser.eval("document.querySelector('[data-seg=ok-count]').textContent")
            assert ok == "6 ok, ok", width
    finally:
        browser.close()


def test_the_status_line_is_on_every_page_after_main(base: dict) -> None:
    for name, html in render_pages(base).items():
        assert html.index("</main>") < html.index('<footer class="statusline" id="statusline" aria-label="Status line">'), name
        footer = _footer(html)
        assert footer.count('<span class="sl-brand">factory</span>') == 1
        for href in re.findall(r'href="([^"]+)"', footer):
            assert re.fullmatch(r"(health|index|efficiency)\.html#[a-z-]+", href), href


def test_bad_and_warn_segments_are_red_or_orange_text_and_block() -> None:
    css = page.CSS
    assert ".sl-vital .blk{width:8px;height:8px;background:currentColor}" in css
    assert ".bad{color:var(--bad)}" in css and ".warn{color:var(--warn)}" in css
    # Only an ok segment keeps the quiet text colour with a green block.
    assert ".sl-vital.ok{color:var(--text-2)}.sl-vital.ok .blk{background:var(--ok)}" in css
    assert ".statusline{position:sticky;bottom:0;" in css
    assert "min-height:32px;\nbackground:var(--dim);border-top:1px solid var(--line)" in css


def test_behind_is_the_stale_code_rule() -> None:
    """version_drift counts exactly lib/health.py's stale_code components;
    an unknown version is listed apart and never counted as behind."""
    health = _health_rows(
        {"stale_code": True, "version": "0ld"},
        _waiter(1, stale_code=True, version="0ld"),
        _waiter(2, version="unknown"),
        _waiter(3, stale_code=True, version="0ld", configured=False),
        {"component": "tunnel", "label": "tunnel @ box", "version": "unknown"},
    )
    drift = page.version_drift(health)
    assert drift == {"newest": "abc1234def", "behind": ["poller @ box", "waiter bot1 @ box"],
                     "unknown": ["waiter bot2 @ box"]}
    flagged = [c["label"] for c in health["components"] if c["stale_code"] and c["configured"]]
    assert drift["behind"] == flagged
    assert set(page.shell_table()["code"]) == set(CODE_COMPONENTS)
    assert page.version_drift(None) == {"newest": "unknown", "behind": [], "unknown": []}


# --- the header band ----------------------------------------------------------------


@pytest.mark.parametrize(("change", "load"), [
    (lambda s: s, "0.000"),  # no device has a slot_max
    (_devices, "0.556"),  # (0 + 5) / (3 + 6): disabled, placeholder and no-slot devices left out
])
def test_band_brightness_is_running_over_total_slots(base: dict, change, load: str) -> None:
    snapshot = change(base)
    band = _section(render_pages(snapshot)["index.html"], "band")
    assert ('<canvas class="px" data-px="field" data-tint="#9ece6a" data-load="' + load
            + '" data-seed="21" data-px-key="band" aria-hidden="true" style="--px:#9ece6a;--l:' + load
            + ';height:24px">') in band


@needs_node
def test_the_script_band_is_the_server_band(tmp_path: Path, base: dict) -> None:
    snapshot = _devices(base)
    assert _script(tmp_path, snapshot)["html"]["band"] == _inner(render_pages(snapshot)["index.html"], "band")


# --- 3. the caret per link state ------------------------------------------------------

LINK = {"link-live": ("#9ece6a", 1), "link-stale": ("#ff9e64", 0.4), "link-lost": ("#f7768e", 0),
        "link-boot": ("#8b93b8", 0), "link-paused": ("#8b93b8", 0)}


def test_caret_effect_and_rate_per_link_state_are_in_the_table() -> None:
    states = page.px_table()["states"]
    for name, (tint, rate) in LINK.items():
        assert states[name] == {"effect": "caret", "tint": tint, "rate": rate}, name
    script = page.px_script()
    (blob,) = re.findall(r"var TABLE = (\{.*?\});\n", script)
    assert {k: v for k, v in json.loads(blob)["states"].items() if k.startswith("link-")} == {
        name: {"effect": "caret", "tint": tint, "rate": rate} for name, (tint, rate) in LINK.items()}


def test_the_page_starts_on_the_still_boot_caret(base: dict) -> None:
    for name, html in render_pages(base).items():
        caret = re.search(r'<span class="caret" id="caret" aria-hidden="true">(.*?)</span>', html).group(1)
        assert caret == ('<canvas class="px" data-px="caret" data-tint="#8b93b8" data-rate="0" '
                         'data-px-key="caret" aria-hidden="true" style="--px:#8b93b8;height:12px"></canvas>'), name


def _caret(tint: str, rate: float) -> str:
    return (f'<canvas class="px" data-px="caret" data-tint="{tint}" data-rate="{rate:g}" '
            f'data-px-key="caret" aria-hidden="true" style="--px:{tint};height:12px"></canvas>')


@needs_node
@pytest.mark.parametrize(("link", "generated", "advance", "label"), [
    ("link-live", NOW, -3, "LIVE"),
    ("link-stale", NOW - 200, -3, "STALE"),
    ("link-lost", NOW, 7, "LINK LOST"),
])
def test_update_link_swaps_the_caret_by_link_state(
    tmp_path: Path, base: dict, link: str, generated: float, advance: int, label: str,
) -> None:
    snapshot = copy.deepcopy(base)
    snapshot["generated_at_epoch"] = generated
    out = _script(tmp_path, snapshot, actions=[["advance", advance]])
    assert out["text"]["link-text"] == label
    assert out["html"]["caret"] == _caret(*LINK[link])
    assert out["html"]["caret"] == page.px_strip("caret", state=link, height=12)


ON_OFF = r"""
function sample(rate, still, reduced) {
  const s = Object.create(PX.Strip.prototype);
  Object.assign(s, {props: {effect: 'caret', tint: '#9ece6a', cell: 4, rate: String(rate), still: still},
    _el: canvas({}, 8, 12), _t0: 0, _beatSeen: 0, _burstSeen: 0, _beatAt: -1000, _burstAt: -1000, _doneAt: null,
    _sysStill: !!reduced});
  const seen = [];
  for (let ms = 0; ms <= 10000; ms += 50) { const start = s._el.ops.length; s._draw(ms);
    const ops = s._el.ops.slice(start); if (ops.length) seen.push([ms, ops.filter(o => o[0] === 'r').length > 1]); }
  return seen;
}
out.live = sample(1, false); out.stale = sample(0.4, false); out.lost = sample(0, false);
out.stillStale = sample(0.4, true); out.reduced = sample(0.4, false, true);
"""


def _runs(seen: list) -> list[int]:
    """Lengths in ms of each on or off run, dropping the cut first and last."""
    edges = [ms for (ms, on), (_, before) in zip(seen[1:], seen) if on != before]
    return [b - a for a, b in zip(edges, edges[1:])]


@needs_node
def test_live_blinks_at_1_hz_stale_at_0_4_hz_and_lost_is_static(tmp_path: Path) -> None:
    out = _node(tmp_path, ON_OFF)
    assert set(_runs(out["live"])) == {500}  # on 0.5 s, off 0.5 s: 1 Hz
    assert set(_runs(out["stale"])) == {1250}  # on 1.25 s, off 1.25 s: 0.4 Hz
    # Lost: one lit frame, then nothing changes, so nothing is redrawn.
    assert out["lost"] == [[0, True]]
    # A still caret (reduced motion) is one lit frame at any rate.
    assert out["stillStale"] == [[0, True]]
    assert out["reduced"] == [[0, True]]


@needs_node
@pytest.mark.parametrize("reduced", [False, True])
def test_static_carets_draw_once_and_the_loop_stops(tmp_path: Path, reduced: bool) -> None:
    out = _node(tmp_path, r"""
      const states = ['link-live', 'link-stale', 'link-lost', 'link-boot'];
      found = states.map(s => PX.strip('caret-' + s, {state: s, height: 12}));
      found.forEach(el => { el.clientWidth = 8; el.clientHeight = 12; });
      PX.bind();
      for (let t = 1050; t <= 5050; t += 50) frame(t);
      out.draws = found.map(draws);
      out.lit = found.map(el => rects(el) > draws(el));
      out.pending = rafQueue.length;
    """, reduced=reduced)
    if reduced:
        assert out["draws"] == [1, 1, 1, 1]
        assert out["pending"] == 0
    else:
        assert out["draws"][2:] == [1, 1]
        assert out["draws"][0] > 50 and out["draws"][1] > 50
        assert out["pending"] == 1
    assert out["lit"][2:] == [True, True]
    if reduced:
        assert out["lit"] == [True] * 4


@needs_node
def test_script_strips_with_rate_load_and_seed_are_the_server_strips(tmp_path: Path) -> None:
    calls = [
        ("caret", {"state": "link-live", "height": 12}),
        ("caret", {"state": "link-stale", "height": 12}),
        ("caret", {"state": "link-lost", "height": 12}),
        ("caret", {"state": "link-boot", "height": 12}),
        ("band", {"state": "band", "load": 0.69, "height": 24}),
        ("band", {"state": "band", "load": 5 / 9, "height": 24}),
        ("band", {"state": "band", "load": 7, "height": 24}),
        ("attention", {"state": "attention", "height": 16}),
        ("x", {"effect": "caret", "tint": "#7dcfff", "rate": 2.5, "seed": 3}),
    ]
    out = _node(tmp_path, "out.calls = " + json.dumps(calls) + ".map(c => serialize(PX.strip(c[0], c[1])));")
    assert out["calls"] == [page.px_strip(key, **kwargs) for key, kwargs in calls]


# --- 4. nav labels, the current item and the file names ----------------------------------


def test_nav_labels_current_item_and_file_names_on_every_page(base: dict) -> None:
    pages = render_pages(base)
    assert tuple(pages) == PAGES == page.PAGES
    assert page.NAV_PAGES == tuple(name for name, _ in NAV)
    for name, html in pages.items():
        nav = _section(html, "nav")
        links = re.findall(r'<a href="([^"]+)"( aria-current="page")?>([^<]+)</a>', nav)
        assert [(href, label) for href, _, label in links] == NAV, name
        assert [href for href, current, _ in links if current] == [{"ticket.html": "job.html"}.get(name, name)], name
        label = {**dict(NAV), "ticket.html": "Ticket"}[name].lower()
        assert f"<title>factory mission control · {label}</title>" in html


def test_current_item_style_is_the_artboards() -> None:
    css = page.CSS
    rule = re.search(r"\.nav a\[aria-current=page\]\{([^}]*)\}", css).group(1)
    assert "background:var(--panel-hi)" in rule and "box-shadow:inset 0 -2px 0 var(--brand)" in rule
    assert "color:var(--hdr-text)" in rule
    assert "--panel-hi:#24283b" in css and "--brand:#9ece6a" in css and "--hdr-text:#ced8ff" in css
    assert ".nav a{padding:6px 12px;color:var(--hdr-text-2);text-decoration:none;font:500 14px/1.2 var(--sans)}" in css


def test_slots_open_the_ticket_page_and_shell_links_only_name_the_six_pages(base: dict) -> None:
    pages = render_pages(base)
    codex = next(row for row in base["running"] if row["job"] == "r-codex-")
    assert f'href="{ticket_page_href(codex)}"' in pages["index.html"]
    for name, html in pages.items():
        shell = html[html.index('<header class="topbar"') : html.index("</header>")] + _footer(html)
        for href in re.findall(r'href="([^"#]+)', shell):
            assert href in PAGES, (name, href)


def test_main_keeps_the_ticket_box_model() -> None:
    rule = re.search(r"\nmain\{([^}]*)\}", page.CSS).group(1)
    # align-content:start: main grows to push the status line down, rows do not stretch.
    for part in ("max-width:clamp(1600px,90vw,2400px)", "padding:20px 24px 32px", "gap:16px", "flex:1 0 auto",
                 "align-content:start"):
        assert part in rule


@needs_chrome
def test_main_grows_past_1600px_only_on_wide_screens_in_a_browser(base: dict) -> None:
    html = _page_with_status_stub(_now(_phone_fit(base)), "index.html")
    probe = """(() => {
        const main = document.querySelector('main'), box = main.getBoundingClientRect();
        const width = sel => document.querySelector(sel).getBoundingClientRect().width;
        return {left: box.left, width: box.width, topbar: width('.topbar'), statusline: width('#statusline'),
                padding: getComputedStyle(main).padding, lane: width('.lane')};
    })()"""
    browser = Browser()
    lanes = {}
    try:
        for width in (375, 699, 1440, 1600, 1700, 1920, 2560, 3840):
            browser.open(html, width)
            got = browser.eval(probe)
            expected = width if width <= 1600 else min(2400, max(1600, round(0.9 * width)))
            assert round(got["width"]) == expected, (width, got)
            assert round(got["left"]) == round((width - got["width"]) / 2), (width, got)
            assert (got["topbar"], got["statusline"]) == (width, width), (width, got)
            assert got["padding"] == ("14px 12px 24px" if width < 641 else "20px 24px 32px"), (width, got)
            lanes[width] = got["lane"]
    finally:
        browser.close()
    assert lanes[2560] > lanes[1600], lanes


def test_topbar_markup_follows_the_artboard(base: dict) -> None:
    html = render_pages(base)["index.html"]
    top = html[html.index('<header class="topbar"') : html.index("</header>")]
    assert '<div class="brand"><span>Factory <b>Mission Control</b></span><span class="caret"' in top
    assert '<div class="link boot" id="link" role="status" aria-live="polite">' in top
    assert top.index('id="nav"') < top.index('id="link"') < top.index('class="meta"') < top.index('id="band"')
    assert top.index('id="band"') < top.index('id="attention-strip"')
    assert "read-only, redacted" in _text(top)
    css = page.CSS
    assert ".top-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px;min-height:56px;padding:8px 24px}" in css
    assert "min-height:36px;padding:6px 24px;" in css  # 56 + 24 + 36 = the artboard's 116 px


# --- every shell link lands ------------------------------------------------------------


def _without_webhook_or_feeder(snapshot: dict) -> dict:
    out = copy.deepcopy(snapshot)
    out["poller"]["linear_webhook"] = None
    out["feeder"] = None
    return out


@pytest.mark.parametrize("change", [lambda s: s, _without_webhook_or_feeder], ids=["full", "bare"])
def test_every_shell_link_lands_on_an_anchor_its_page_renders(base: dict, change) -> None:
    snapshot = change(base)
    pages = render_pages(snapshot)
    ids = {name: set(re.findall(r'\sid="([^"]+)"', _no_script(html))) for name, html in pages.items()}
    targets = {seg["key"]: seg["href"] for seg in page.status_segments(snapshot) if seg["href"]}
    assert set(targets) == set(page.STATUS_LINKS)
    for name, html in pages.items():
        shell = (html[html.index('<header class="topbar"') : html.index("</header>")]
                 + _footer(html))
        for href in re.findall(r'href="([^"]+#[^"]+)"', shell) + list(targets.values()):
            target, anchor = href.split("#")
            if target == "ticket.html":
                address = urllib.parse.unquote(anchor.removeprefix("t="))
                assert 'class="jobpage ticketpage"' in ticket_view(snapshot, address), (name, href)
            else:
                assert anchor in ids[target], (name, href)


@needs_node
def test_the_script_status_line_links_where_the_server_links_without_webhook_or_feeder(
        tmp_path: Path, base: dict) -> None:
    bare = _without_webhook_or_feeder(base)
    assert _script(tmp_path, bare)["html"]["statusline"] == _inner(render_pages(bare)["index.html"], "statusline")


def test_the_status_line_stays_stuck_to_the_bottom_at_every_width() -> None:
    positions = re.findall(r"\.statusline\{[^}]*?position:([a-z]+)", page.CSS)
    assert positions == ["sticky"]


def test_the_no_script_band_reflects_load(base: dict) -> None:
    def band(held: int) -> str:
        snapshot = copy.deepcopy(base)
        snapshot["running"] = []
        snapshot["slots"]["devices"] = [{"name": "box", "slot_max": 3, "held": held, "enabled": True,
                                         "placeholder": False, "harnesses": []}]
        return _section(render_pages(snapshot)["index.html"], "band")
    idle, full = band(0), band(3)
    assert 'data-load="0.000"' in idle and 'data-load="1.000"' in full
    assert "--l:0.000;" in idle and "--l:1.000;" in full
    rule = re.search(r"\n\.px\[data-load\]\{([^}]*)\}", page.CSS)
    assert rule and "var(--l" in rule.group(1)


def _place_harness() -> str:
    return activity.HARNESS.replace(
        "    } else if (kind === 'poll') {",
        "    } else if (kind === 'put') {\n"
        "      const scope = arg.in ? find(body, n => n.attrs.id === arg.in || n.className.split(' ')[0] === arg.in) : body;\n"
        "      const node = find(scope, n => Object.entries(arg.attr || {}).every(([k, v]) => n.attrs[k] === v) &&\n"
        "        (arg.cls === undefined || n.className.split(' ')[0] === arg.cls) && (arg.tag === undefined || n.tagName === arg.tag) &&\n"
        "        (arg.has === undefined || n.children.some(c => c.className.split(' ')[0] === arg.has)));\n"
        "      if (!node) throw new Error('nothing to put: ' + JSON.stringify(arg));\n"
        "      if (arg.focus) { node.focus(); kept = node; }\n"
        "      if (arg.left !== undefined) node.scrollLeft = arg.left;\n"
        "      if (arg.top !== undefined) node.scrollTop = arg.top;\n"
        "    } else if (kind === 'where') {\n"
        "      const node = document.activeElement;\n"
        "      const path = [];\n"
        "      for (let n = node; n && n !== body; n = n.parentNode) path.unshift(n.attrs.id || n.className.split(' ')[0] || n.tagName);\n"
        "      const scroll = [];\n"
        "      (function walk(n) { if (n.scrollLeft || n.scrollTop) scroll.push([n.className.split(' ')[0], n.scrollLeft || 0, n.scrollTop || 0, n.isConnected]);\n"
        "        n.children.forEach(walk); })(body);\n"
        "      steps.push({where: {tag: node ? node.tagName : 'BODY', attrs: node ? node.attrs : {}, cls: node ? node.className : '', path,\n"
        "        same: node === kept, connected: !!kept && kept.isConnected}, scroll});\n"
        "    } else if (kind === 'poll') {",
    ).replace("const steps = [];", "const steps = [];\nlet kept = null;")


def _place(tmp_path: Path, snapshot: dict, page_name: str, ids: list[str], actions: list, hash_: str = "") -> list[dict]:
    (script,) = re.findall(r"<script>(.*?)</script>", render_pages(snapshot)[page_name], flags=re.S)
    out = layout.run_harness(tmp_path, _place_harness(), script, snapshot, SCRIPT_PAGE[page_name], ids, actions, hash_)
    return [step for step in out["steps"] if "where" in step]


def _next(snapshot: dict, change=None) -> dict:
    later = copy.deepcopy(snapshot)
    later["generated_at_epoch"] = snapshot["generated_at_epoch"] + 5
    if change:
        change(later)
    return later


def _activity(tmp_path: Path) -> dict:
    return activity._rich(activity.activity_snapshot(tmp_path / "db"))


def _job(tmp_path: Path) -> dict:
    return _snapshot(tmp_path / "db")


def _ids(page_name: str) -> list[str]:
    return {"activity.html": activity.IDS, "job.html": JOB, "ticket.html": BAR + ["hud", "ticketview"]}.get(page_name, BAR)


@needs_node
def test_a_relay_filter_button_keeps_focus_across_two_snapshots(tmp_path: Path) -> None:
    snapshot = _activity(tmp_path)
    at = {"in": "relay", "attr": {"data-relay": "shipped"}, "focus": True}
    steps = _place(tmp_path, snapshot, "activity.html", _ids("activity.html"),
                   [["put", at], ["poll", _next(snapshot)], ["where", None], ["poll", _next(_next(snapshot))], ["where", None]])
    for step in steps:
        where = step["where"]
        assert (where["tag"], where["attrs"].get("data-relay"), where["path"][0]) == ("button", "shipped", "relay"), where
        assert not where["connected"], "the focused button was rebuilt, not kept"


SAME_CONTROL = [
    ("activity.html", "", {"in": "relay", "tag": "button", "attr": {"data-relay": "needs"}}, "data-relay"),
    ("job.html", "", {"in": "ticket-filter", "attr": {"data-view": "table"}}, "data-view"),
    ("job.html", "", {"in": "ticket-filter", "attr": {"data-project": ""}}, "data-project"),
    ("ticket.html", REVIEW_HASH, {"in": "runlog", "attr": {"data-visit": "2"}}, "data-visit"),
    ("ticket.html", REVIEW_HASH, {"in": "journey", "attr": {"data-visit": "3"}}, "data-visit"),
    ("ticket.html", REVIEW_HASH, {"in": "ticketview", "attr": {"data-scale": "even"}}, "data-scale"),
    ("ticket.html", REVIEW_HASH, {"in": "ticketview", "attr": {"data-replay": "1"}}, "data-replay"),
    ("index.html", "", {"in": "statusline", "cls": "sl-vital", "tag": "a"}, "href"),
    ("index.html", "", {"in": "attention-strip", "cls": "attn-count"}, "href"),
]


@needs_node
@pytest.mark.parametrize(("page_name", "hash_", "at", "key"), SAME_CONTROL,
                         ids=[f"{p}:{a['in']}:{k}" for p, _, a, k in SAME_CONTROL])
def test_focus_lands_on_the_rebuilt_twin_of_the_same_control(
    tmp_path: Path, page_name: str, hash_: str, at: dict, key: str
) -> None:
    snapshot = _activity(tmp_path) if page_name == "activity.html" else _job(tmp_path)
    steps = _place(tmp_path, snapshot, page_name, _ids(page_name),
                   [["put", {**at, "focus": True}], ["where", None], ["poll", _next(snapshot)], ["where", None]],
                   hash_=hash_)
    before, after = (step["where"] for step in steps)
    assert after["tag"] == before["tag"] and after["attrs"].get(key) == before["attrs"].get(key), (before, after)
    assert after["path"] == before["path"], (before, after)
    assert not after["connected"], "the focused control was rebuilt, not kept"


@needs_node
def test_focus_falls_back_to_the_panel_root_when_the_control_is_gone(tmp_path: Path) -> None:
    snapshot = _job(tmp_path)
    repo = snapshot["tickets"][0]["repo"]
    gone = _next(snapshot, lambda s: s.update(tickets=[t for t in s["tickets"] if t["repo"] != repo]))
    at = {"in": "ticket-filter", "attr": {"data-project": repo}, "focus": True}
    (step,) = _place(tmp_path, snapshot, "job.html", _ids("job.html"), [["put", at], ["poll", gone], ["where", None]])
    assert step["where"]["path"] == ["ticket-filter"], step
    assert step["where"]["attrs"]["tabindex"] == "-1", step


@needs_node
def test_a_redraw_never_takes_focus(tmp_path: Path) -> None:
    snapshot = _job(tmp_path)
    steps = _place(tmp_path, snapshot, "job.html", _ids("job.html"),
                   [["where", None], ["poll", _next(snapshot)], ["where", None],
                    ["put", {"in": "link", "tag": "div", "focus": True}], ["poll", _next(_next(snapshot))], ["where", None]])
    nothing, still_nothing, outside = (step["where"] for step in steps)
    assert nothing["tag"] == still_nothing["tag"] == "BODY", (nothing, still_nothing)
    assert outside["same"] and outside["connected"], outside


@needs_node
@pytest.mark.parametrize(("page_name", "hash_", "box", "axis"), [
    ("ticket.html", REVIEW_HASH, {"cls": "jd-box", "has": "jd-journey"}, "left"),
    ("ticket.html", REVIEW_HASH, {"cls": "jd-box", "has": "jd-flowmap"}, "left"),
    ("health.html", "", {"in": "log", "cls": "sys-log"}, "top"),
    ("health.html", "", {"in": "components", "cls": "sys-scroll"}, "left"),
    ("efficiency.html", "", {"cls": "ef-scroll"}, "left"),
], ids=["journey", "state-chart", "poller-log", "components", "efficiency"])
def test_a_scroll_box_keeps_its_offset_across_two_snapshots(
    tmp_path: Path, page_name: str, hash_: str, box: dict, axis: str
) -> None:
    snapshot = system._snapshot(tmp_path / "db") if page_name == "health.html" else _job(tmp_path)
    ids = _ids(page_name) + {"health.html": ["components", "log"], "efficiency.html": ["headroom"]}.get(page_name, [])
    steps = _place(tmp_path, snapshot, page_name, ids,
                   [["put", {**box, axis: 137}], ["poll", _next(snapshot)], ["where", None],
                    ["poll", _next(_next(snapshot))], ["where", None]], hash_=hash_)
    for step in steps:
        assert [s for s in step["scroll"] if s[0] == box["cls"]] == [
            [box["cls"], 137 if axis == "left" else 0, 137 if axis == "top" else 0, True]
        ], step["scroll"]


def _ship(snapshot: dict) -> None:
    flow = next(job for job in snapshot["running"] if job["job"] == "h-review")["flow"]
    flow["visits"][-1]["current"] = False
    flow["visits"].append({"state": "ship", "who": "script", "started_at": 1800000000.0, "duration_seconds": None,
                           "time_known": False, "off_chart": False, "reason": "APPROVE", "current": True})
    flow["state"] = "ship"
    flow["path"].append("ship")


@needs_node
def test_a_run_log_row_keeps_focus_when_it_stops_being_current(tmp_path: Path) -> None:
    snapshot = _job(tmp_path)
    at = {"in": "runlog", "attr": {"data-visit": "4"}, "focus": True}
    steps = _place(tmp_path, snapshot, "ticket.html", _ids("ticket.html"),
                   [["put", at], ["where", None], ["poll", _next(snapshot, _ship)], ["where", None]], hash_=REVIEW_HASH)
    before, after = (step["where"] for step in steps)
    assert "now" in before["cls"].split() and "now" not in after["cls"].split(), (before, after)
    assert (after["tag"], after["attrs"].get("data-visit")) == ("button", "4"), after
    assert after["path"][:-1] == before["path"][:-1], (before, after)
    assert not after["connected"], "the focused row was rebuilt, not kept"


@needs_node
def test_a_ticket_link_keeps_focus_when_its_card_moves_to_another_column(tmp_path: Path) -> None:
    snapshot = _job(tmp_path)
    href = REVIEW_PAGE

    def advance(later: dict) -> None:
        next(t for t in later["tickets"] if t["ticket_id"] == "40")["state"] = "ship"
        _ship(later)

    at = {"in": "pipeline", "attr": {"href": href}, "focus": True}
    (step,) = _place(tmp_path, snapshot, "job.html", _ids("job.html"),
                     [["put", at], ["poll", _next(snapshot, advance)], ["where", None]])
    after = step["where"]
    assert (after["tag"], after["attrs"].get("href")) == ("a", href), after
    assert not after["connected"], "the focused link was rebuilt, not kept"


GONE_ATTENTION = [
    ("job.html", "attention-strip", "attn-who"),
    ("activity.html", "attention", "at-go"),
]


@needs_node
@pytest.mark.parametrize(("page_name", "root", "cls"), GONE_ATTENTION, ids=[g[1] for g in GONE_ATTENTION])
def test_focus_on_a_resolved_attention_item_falls_back_to_the_panel_root(
    tmp_path: Path, page_name: str, root: str, cls: str
) -> None:
    snapshot = _activity(tmp_path) if page_name == "activity.html" else _job(tmp_path)
    first = snapshot["attention"]["items"][0]

    def resolve(s: dict) -> None:
        s["attention"]["items"] = [x for x in s["attention"]["items"] if x != first]
        s["attention"]["count"] = len(s["attention"]["items"])

    later = _next(snapshot, resolve)
    assert later["attention"]["items"], "another attention item must remain to be wrongly focused"
    at = {"in": root, "cls": cls, "tag": "a", "focus": True}
    (step,) = _place(tmp_path, snapshot, page_name, _ids(page_name), [["put", at], ["poll", later], ["where", None]])
    assert step["where"]["path"] == [root], step
    assert step["where"]["attrs"]["tabindex"] == "-1", step


# --- #480: link state, strip ticket links, skip link and landmarks ---------------------


def _link_harness() -> str:
    return (
        activity.HARNESS.replace("const steps = [];", "const steps = [];\nlet failing = false;")
        .replace(
            "  return Promise.resolve({ok: true, status: 200,",
            "  if (failing) return Promise.reject(new Error('offline'));\n  return Promise.resolve({ok: true, status: 200,",
        )
        .replace(
            "    } else if (kind === 'poll') {",
            "    } else if (kind === 'hide' || kind === 'show') {\n"
            "      document.hidden = kind === 'hide';\n"
            "      (docListeners.visibilitychange || []).forEach(fn => fn({type: 'visibilitychange'}));\n"
            "      if (document.hidden) polls.splice(0).forEach(fn => fn());\n"
            "      await new Promise(resolve => realSetTimeout(resolve, 50));\n"
            "    } else if (kind === 'fail' || kind === 'ok') {\n"
            "      failing = kind === 'fail';\n"
            "    } else if (kind === 'focused') {\n"
            "      steps.push({focused: document.activeElement ? document.activeElement.attrs.id : null});\n"
            "    } else if (kind === 'label') {\n"
            "      ids['attention-strip'].setAttribute('aria-label', 'Needs attention');\n"
            "      ids.statusline.setAttribute('aria-label', 'Status line');\n"
            "    } else if (kind === 'labels') {\n"
            "      steps.push({labels: [ids['attention-strip'].getAttribute('aria-label'), ids.statusline.getAttribute('aria-label')]});\n"
            "    } else if (kind === 'link') {\n"
            "      steps.push({link: [ids['link-text'].textContent, ids.link.className, ids.caret.children.map(c => c.html()).join('')]});\n"
            "    } else if (kind === 'poll') {",
        )
    )


def _link_run(tmp_path: Path, snapshot: dict, actions: list, name: str = "index.html",
              ids: list[str] | None = None, harness: str | None = None) -> dict:
    harness = harness or _link_harness()
    assert "kind === 'hide'" in harness and "if (failing)" in harness
    (script,) = re.findall(r"<script>(.*?)</script>", render_pages(snapshot)[name], flags=re.S)
    return layout.run_harness(tmp_path, harness, script, snapshot, SCRIPT_PAGE[name], ids or BAR, actions)


def _links(out: dict) -> list[list[str]]:
    return [step["link"] for step in out["steps"] if "link" in step]


@needs_node
def test_a_hidden_tab_never_reads_link_lost(tmp_path: Path, base: dict) -> None:
    out = _link_run(tmp_path, base, [
        ["link"], ["hide"], ["link"], ["advance", 10], ["link"], ["show"], ["link"], ["advance", 1], ["link"],
    ])
    seen = _links(out)
    assert [text for text, *_ in seen] == ["LIVE"] * 5
    assert len({tuple(state) for state in seen}) == 1, seen


@needs_node
def test_hidden_time_does_not_count_after_the_tab_shows(tmp_path: Path, base: dict) -> None:
    out = _link_run(tmp_path, base, [
        ["hide"], ["advance", 30], ["fail"], ["show"], ["link"], ["advance", 5], ["link"],
    ])
    assert [text for text, *_ in _links(out)] == ["LIVE", "LIVE"]


@needs_node
def test_failing_polls_on_a_visible_tab_still_reach_link_lost(tmp_path: Path, base: dict) -> None:
    out = _link_run(tmp_path, base, [["fail"], ["hide"], ["show"], ["link"], ["advance", 7], ["link"]])
    (before, _, _), (after, cls, caret) = _links(out)
    assert (before, after, cls) == ("LIVE", "LINK LOST", "link lost")
    assert caret == page.px_strip("caret", state="link-lost", height=12)
    out = _link_run(tmp_path / "visible", base, [["fail"], ["advance", 7], ["link"]])
    assert _links(out)[0][0] == "LINK LOST"


def _ticket_items(snapshot: dict) -> dict:
    out = copy.deepcopy(snapshot)
    out["queued"].append(dict(out["queued"][0], ticket_id="13", job="q-later"))
    out["attention"] = {"count": 3, "items": [
        {"kind": "stuck", "repo": "maxharris1/cleffy", "ticket_id": "13", "detail": "running"},
        {"kind": "stale", "repo": "maxharris1/Cleffy", "ticket_id": "12", "detail": "queued"},
        {"kind": "paused", "repo": "aut0-code/aut0", "ticket_id": "AUT-7", "detail": "neither"},
    ]}
    return out


TICKET_HREFS = [ticket_page_href({"repo": repo, "ticket_id": tid}) for repo, tid in
                (("maxharris1/cleffy", "13"), ("maxharris1/Cleffy", "12"), ("aut0-code/aut0", "AUT-7"))]
STRIP_HREFS = TICKET_HREFS[:2]


def _strip_hrefs(markup: str) -> list[str]:
    return re.findall(r'<a class="attn-who" href="([^"]+)"', markup)


def _panel_hrefs(markup: str) -> list[str]:
    return re.findall(r'<a class="at-go" href="([^"]+)"', markup)


def test_strip_ticket_links_follow_the_activity_panel_rule(base: dict) -> None:
    snapshot = _ticket_items(base)
    pages = render_pages(snapshot)
    assert _panel_hrefs(_section(pages["activity.html"], "attention")) == TICKET_HREFS
    for name, html in pages.items():
        want = [] if name == "activity.html" else STRIP_HREFS
        assert _strip_hrefs(_section(_no_script(html), "attention-strip")) == want, name


@needs_node
def test_script_strip_ticket_links_follow_the_activity_panel_rule(tmp_path: Path, base: dict) -> None:
    out = _script(tmp_path / "activity", _ticket_items(base), "activity.html", ids=BAR + ["attention"])
    assert _strip_hrefs(out["html"]["attention-strip"]) == []
    assert _panel_hrefs(out["html"]["attention"]) == TICKET_HREFS
    out = _script(tmp_path / "lanes", _ticket_items(base), "index.html")
    assert _strip_hrefs(out["html"]["attention-strip"]) == STRIP_HREFS


FOCUSABLE = re.compile(r'<(a\s[^>]*href=|button\b|input\b|select\b|textarea\b|summary\b|[a-z]+\s[^>]*tabindex="(?!-1)")')


def test_skip_to_content_is_the_first_focusable_element_on_every_page(base: dict) -> None:
    for name, html in render_pages(base).items():
        body = _no_script(html)[html.index("<body"):]
        body = body[body.index(">") + 1:]
        first = FOCUSABLE.search(body)
        assert first and body[first.start():].startswith(
            '<a class="skip" id="skip" href="#hud">Skip to content</a>'), name
        assert '<main id="hud" tabindex="-1">' in html, name
    css = page.CSS
    assert ".vh,.skip:not(:focus){position:absolute;width:1px;height:1px;" in css
    assert "clip:rect(0 0 0 0)" in css
    assert re.search(r"\.skip:focus\{position:fixed;[^}]*background:var\(--panel\);[^}]*color:var\(--text\)", css)
    assert "a:focus-visible{outline:2px solid var(--brand)" in css


@needs_node
@pytest.mark.parametrize("name", PAGES)
def test_the_skip_link_moves_focus_to_main_without_a_hash(tmp_path: Path, base: dict, name: str) -> None:
    out = _link_run(tmp_path, base, [["focused"], ["click", {"id": "skip"}], ["focused"]], name=name,
                    ids=JOB + ["skip"] if name == "job.html" else BAR + ["skip", "hud"])
    before, click, after = out["steps"]
    assert (before["focused"], click["prevented"], after["focused"]) == (None, True, "hud")


@pytest.mark.parametrize(("name", "label"), [
    ("index.html", "Lanes"), ("activity.html", "Activity"), ("efficiency.html", "Efficiency"), ("health.html", "System"),
    ("job.html", "Tickets"),
])
def test_five_pages_have_one_hidden_h1_with_the_nav_label(base: dict, name: str, label: str) -> None:
    html = render_pages(base)[name]
    assert re.findall(r"<h1\b[^>]*>.*?</h1>", html) == [f'<h1 class="vh">{label}</h1>']
    assert f'<a href="{name}" aria-current="page">{label}</a>' in html
    assert '<main id="hud" tabindex="-1"><h1 class="vh">' in html


def test_the_strip_and_status_line_are_labelled(base: dict) -> None:
    for snapshot in (base, _clear(base)):
        for name, html in render_pages(snapshot).items():
            assert '<div class="attn" id="attention-strip" role="region" aria-label="Needs attention"' in html, name
            assert '<footer class="statusline" id="statusline" aria-label="Status line">' in html, name


@needs_node
def test_a_redraw_keeps_the_strip_and_status_line_labels(tmp_path: Path, base: dict) -> None:
    later = _ticket_items(base)
    later["generated_at_epoch"] += 5
    out = _link_run(tmp_path, base, [["label"], ["poll", later], ["labels"]])
    assert out["steps"][-1]["labels"] == ["Needs attention", "Status line"]
    assert _strip_hrefs(out["html"]["attention-strip"]) == STRIP_HREFS


def _visible(markup: str) -> str:
    return re.sub(r'<span class="vh">[^<]*</span>', "", markup)


@pytest.mark.parametrize(("case", "change"), [(row[0], row[1]) for row in SEGMENTS],
                         ids=[row[0] for row in SEGMENTS])
def test_every_stated_segment_says_its_state_in_hidden_text(base: dict, case: str, change) -> None:
    snapshot = change(base)
    footer = _footer(render_pages(snapshot)["index.html"])
    vitals = re.findall(r'<a class="sl-vital ([a-z]+)"[^>]*>(.*?)</a>', footer)
    assert vitals, case
    for state, inner in vitals:
        assert state in ("ok", "warn", "bad", "muted"), case
        assert inner.endswith(f'<span class="vh">, {state}</span>'), (case, inner)
        assert _visible(inner).count("<span") == 2, (case, inner)
    assert '<span class="vh">' not in "".join(re.findall(r'<span class="sl-seg[^"]*".*?</span></span>', footer))


FRESH_IDS = BAR + ["fresh-bar", "fresh-head", "fresh-text", "fresh-say"]
FRESH_BAR = ('<div class="fresh-bar" id="fresh-bar" hidden><span class="fresh-blk" aria-hidden="true"></span>'
             '<b id="fresh-head" aria-hidden="true"></b><span id="fresh-text" aria-hidden="true"></span>'
             '<span class="vh" id="fresh-say" role="alert"></span></div>')
AGE_SEG = ('<span class="sl-age" data-seg="age" hidden><span class="blk" aria-hidden="true"></span>'
           '<span class="sl-t" data-short=""></span></span>')
STALE_AGE = 554
GREY = "#8b93b8"
GREEN = "#9ece6a"


def _fresh_harness() -> str:
    return (
        _link_harness()
        .replace("let failing = false;", "let failing = false;\nlet holding = false;\nconst held = [];")
        .replace(
            "  if (failing) return Promise.reject(new Error('offline'));",
            "  if (holding) return new Promise((res, rej) => held.push({res, rej}));\n"
            "  if (failing) return Promise.reject(new Error('offline'));",
        )
        .replace(
            "if (ids['flow-template'] && status.statechart) {",
            "if (ids['fresh-bar']) {\n"
            "  const bar = ids['fresh-bar']; bar.className = 'fresh-bar'; bar.hidden = true;\n"
            "  ['fresh-head', 'fresh-text', 'fresh-say'].forEach(id => { body.removeChild(ids[id]); bar.appendChild(ids[id]); });\n"
            "}\n"
            "if (ids['flow-template'] && status.statechart) {",
        )
        .replace(
            "    } else if (kind === 'poll') {",
            "    } else if (kind === 'hold') {\n"
            "      holding = true;\n"
            "    } else if (kind === 'answer') {\n"
            "      holding = false;\n"
            "      if (arg && typeof arg === 'object') status = arg;\n"
            "      held.splice(0).forEach(h => arg === 'fail' ? h.rej(new Error('offline'))\n"
            "        : h.res({ok: true, status: 200, json: () => Promise.resolve(status)}));\n"
            "      await new Promise(resolve => realSetTimeout(resolve, 50));\n"
            "    } else if (kind === 'fresh') {\n"
            "      const has = (n, c) => n.className.split(' ').includes(c);\n"
            "      const bar = ids['fresh-bar'];\n"
            "      const seg = find(ids.statusline, n => has(n, 'sl-age'));\n"
            "      const t = seg && find(seg, n => has(n, 'sl-t'));\n"
            "      steps.push({fresh: {state: body.attrs['data-fresh'] === undefined ? null : body.attrs['data-fresh'],\n"
            "        link: ids.link.className, text: ids['link-text'].textContent,\n"
            "        bar: {hidden: !!bar.hidden, cls: bar.className, head: ids['fresh-head'].textContent,\n"
            "          text: ids['fresh-text'].textContent, say: ids['fresh-say'].textContent},\n"
            "        band: ids.band.children.map(c => c.html()).join(''),\n"
            "        caret: ids.caret.children.map(c => c.html()).join(''),\n"
            "        line: ids.statusline.children.map(c => c.html()).join(''),\n"
            "        age: seg ? {hidden: !!seg.hidden, cls: seg.className, text: t ? t.textContent : null,\n"
            "          short: t ? t.getAttribute('data-short') : null} : null}});\n"
            "    } else if (kind === 'poll') {",
            1,
        )
    )


def _plain(value):
    """Intl writes "2:50 AM"; a reader sees a plain space."""
    if isinstance(value, str):
        return value.replace(" ", " ").replace("\xa0", " ")
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _fresh_run(tmp_path: Path, snapshot: dict, actions: list) -> list[dict]:
    harness = _fresh_harness()
    assert "kind === 'answer'" in harness and "kind === 'fresh'" in harness and "holding = false" in harness
    out = _link_run(tmp_path, snapshot, actions, ids=FRESH_IDS, harness=harness)
    return [_plain(step["fresh"]) for step in out["steps"] if "fresh" in step]


def _aged(base: dict, age: float = 0, publish: float | None = 15, beats: int | None = 3) -> dict:
    out = copy.deepcopy(base)
    out["health"] = _health_rows({}, publish=publish)
    if beats is None:
        del out["health"]["thresholds"]["stale_beats"]
    else:
        out["health"]["thresholds"]["stale_beats"] = beats
    out["generated_at_epoch"] = NOW - age
    return out


def _at(snapshot: dict, epoch: float) -> dict:
    out = copy.deepcopy(snapshot)
    out["generated_at_epoch"] = epoch
    return out


def _et(epoch: float) -> str:
    at = datetime.fromtimestamp(epoch, ZoneInfo("America/New_York"))
    return f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'} ET"


def _tint(markup: str) -> str:
    return re.search(r'data-tint="([^"]+)"', markup).group(1)


def _still(markup: str) -> str | None:
    found = re.search(r'data-still="([^"]+)"', markup)
    return found.group(1) if found else None


def _published(epoch: float) -> str:
    return f"The dashboard writer last published at {_et(epoch)}. Everything below is as of then."


def _showing(epoch: float) -> str:
    return f"Showing the last snapshot, from {_et(epoch)}."


def test_the_server_render_has_the_hidden_bar_and_age_segment_and_no_freshness(base: dict) -> None:
    for snapshot in (base, _aged(base, STALE_AGE)):
        for name, html in render_pages(snapshot).items():
            static = _no_script(html)
            band = _section(static, "band")
            assert static[static.index(band) + len(band):].startswith(FRESH_BAR), name
            assert static.count('id="fresh-bar"') == 1, name
            body = static[static.index("<body"): static.index(">", static.index("<body")) + 1]
            assert "data-fresh" not in body, (name, body)
            assert _footer(html).count(AGE_SEG) == 1, name
            assert _tint(band) == GREEN and _still(band) is None, name


def test_the_paused_caret_is_still_and_grey_in_the_table() -> None:
    assert page.px_table()["states"]["link-paused"]["tint"] == GREY
    assert page.px_strip("caret", state="link-paused", height=12) == _caret(GREY, 0)


@needs_node
def test_a_46_second_old_snapshot_reads_stale_at_a_15_second_publish(tmp_path: Path) -> None:
    root = tmp_path
    s = collect_snapshot(*seeded(root), now=NOW)
    s["health"] = _health_rows({})
    s["generated_at_epoch"] = NOW - 46
    out = _script(root / "run", s, actions=[["advance", -3]])
    assert out["text"]["link-text"] == "STALE", out["text"]["link-text"]


@needs_node
@pytest.mark.parametrize(("publish", "beats", "age", "label"), [
    (15, 3, 44, "LIVE"), (15, 3, 46, "STALE"),
    (60, 3, 179, "LIVE"), (60, 3, 181, "STALE"),
    (15, 5, 74, "LIVE"), (15, 5, 76, "STALE"),
    (20, None, 59, "LIVE"), (20, None, 61, "STALE"),
    (None, 3, 179, "LIVE"), (None, 3, 181, "STALE"),
    (0, 3, 179, "LIVE"), (0, 3, 181, "STALE"),
])
def test_the_stale_threshold_is_stale_beats_publish_intervals_else_180_seconds(
    tmp_path: Path, base: dict, publish: float | None, beats: int | None, age: int, label: str,
) -> None:
    (seen,) = _fresh_run(tmp_path, _aged(base, age, publish, beats), [["fresh"]])
    assert (seen["text"], seen["state"], seen["link"]) == (label, label.lower(), "link " + label.lower())


@needs_node
def test_a_stale_snapshot_shows_the_amber_bar_a_grey_still_band_and_ticking_ages(tmp_path: Path, base: dict) -> None:
    snapshot = _aged(base, STALE_AGE)
    first, second, third = _fresh_run(tmp_path, snapshot, [
        ["fresh"], ["advance", 1], ["fresh"], *[["advance", 5], ["poll", snapshot]] * 12, ["fresh"]])
    text = _published(NOW - STALE_AGE)
    assert (first["state"], first["link"], first["text"]) == ("stale", "link stale", "STALE")
    assert first["bar"] == {"hidden": False, "cls": "fresh-bar stale", "head": "Snapshot is 9m 14s old.",
                            "text": text, "say": "Snapshot is 9m 14s old. " + text}
    assert first["age"] == {"hidden": False, "cls": "sl-age stale", "text": "as of 9m 14s ago", "short": "9m old"}
    assert (_tint(first["band"]), _still(first["band"])) == (GREY, "true")
    assert first["caret"] == page.px_strip("caret", state="link-stale", height=12)
    assert (second["bar"]["head"], second["age"]["text"]) == ("Snapshot is 9m 15s old.", "as of 9m 15s ago")
    assert (third["state"], third["bar"]["head"]) == ("stale", "Snapshot is 10m 15s old.")
    assert (third["age"]["text"], third["age"]["short"]) == ("as of 10m 15s ago", "10m old")
    assert second["bar"]["say"] == third["bar"]["say"] == first["bar"]["say"], "announced once, not per tick"


@needs_node
def test_failing_polls_on_a_visible_tab_show_the_red_bar(tmp_path: Path, base: dict) -> None:
    snapshot = _aged(base)
    before, lost, later = _fresh_run(tmp_path, snapshot, [
        ["fail"], ["poll", snapshot], ["fresh"], ["advance", 7], ["fresh"], ["advance", 1], ["fresh"]])
    assert (before["state"], before["text"], before["bar"]["hidden"]) == ("live", "LIVE", True)
    assert (lost["state"], lost["link"], lost["text"]) == ("lost", "link lost", "LINK LOST")
    text = _showing(NOW)
    assert lost["bar"] == {"hidden": False, "cls": "fresh-bar lost", "head": "Cannot reach the dashboard.",
                           "text": text, "say": "Cannot reach the dashboard. " + text}
    assert lost["age"] == {"hidden": False, "cls": "sl-age lost", "text": "as of 7s ago", "short": "7s old"}
    assert (_tint(lost["band"]), _still(lost["band"])) == (GREY, "true")
    assert lost["caret"] == page.px_strip("caret", state="link-lost", height=12)
    assert (later["age"]["text"], later["age"]["short"]) == ("as of 8s ago", "8s old")
    assert later["bar"] == lost["bar"], "announced once, not per tick"


@needs_node
def test_lost_outranks_stale_and_is_announced_on_entry(tmp_path: Path, base: dict) -> None:
    snapshot = _aged(base, STALE_AGE)
    stale, lost = _fresh_run(tmp_path, snapshot, [["fresh"], ["fail"], ["poll", snapshot], ["advance", 7], ["fresh"]])
    assert (stale["state"], stale["bar"]["cls"]) == ("stale", "fresh-bar stale")
    assert (lost["state"], lost["text"], lost["bar"]["cls"], lost["age"]["cls"]) == (
        "lost", "LINK LOST", "fresh-bar lost", "sl-age lost")
    assert lost["bar"]["say"] == "Cannot reach the dashboard. " + _showing(NOW - STALE_AGE)


@needs_node
def test_polls_that_fail_from_the_first_fetch_show_the_red_bar(tmp_path: Path, base: dict) -> None:
    snapshot = _aged(base, STALE_AGE)
    harness = (
        _fresh_harness()
        .replace("let failing = false;", "let failing = true;", 1)
        .replace("body.attrs['data-generated'] = '1800000000.000';",
                 f"body.attrs['data-generated'] = '{snapshot['generated_at_epoch']:.3f}';", 1)
    )
    assert "let failing = true;" in harness and f"'{NOW - STALE_AGE:.3f}'" in harness
    out = _link_run(tmp_path, snapshot, [["fresh"], ["advance", 7], ["fresh"], ["advance", 1], ["fresh"]],
                    ids=FRESH_IDS, harness=harness)
    early, lost, later = [_plain(step["fresh"]) for step in out["steps"] if "fresh" in step]
    assert (early["state"], early["text"], early["bar"]["hidden"]) == ("boot", "NO LINK · static view", True)
    assert (lost["state"], lost["link"], lost["text"]) == ("lost", "link lost", "LINK LOST")
    text = _showing(NOW - STALE_AGE)
    assert lost["bar"] == {"hidden": False, "cls": "fresh-bar lost", "head": "Cannot reach the dashboard.",
                           "text": text, "say": "Cannot reach the dashboard. " + text}
    assert (_tint(lost["band"]), _still(lost["band"])) == (GREY, "true")
    assert lost["caret"] == page.px_strip("caret", state="link-lost", height=12)
    assert later["bar"] == lost["bar"], "announced once, not per tick"


LOST = """(function () {
  function shown(n) { return !!n && !n.hidden && n.getClientRects().length > 0; }
  var bar = document.getElementById('fresh-bar'), band = document.querySelector('#band canvas');
  var age = document.querySelector('#statusline [data-seg=age]');
  return {state: document.body.getAttribute('data-fresh'), hidden: document.hidden,
    link: document.getElementById('link-text').textContent,
    bar: {shown: shown(bar), cls: bar.className, head: document.getElementById('fresh-head').textContent,
          text: document.getElementById('fresh-text').textContent},
    age: {shown: shown(age), cls: age.className, text: age.querySelector('.sl-t').textContent},
    band: {tint: band.getAttribute('data-tint'), still: band.getAttribute('data-still')}};
})()"""


@needs_chrome
def test_a_page_whose_polls_never_answer_reads_link_lost_in_a_browser(base: dict) -> None:
    snapshot = _now(base, STALE_AGE)
    html = _fresh_page(snapshot).replace("window.__fail = false;", "window.__fail = true;", 1)
    browser = Browser()
    try:
        _open(browser, html, 1440, None)
        browser.eval("window.__skew = 7000;")
        deadline = time.monotonic() + 20
        while browser.eval("document.body.getAttribute('data-fresh')") != "lost":
            assert time.monotonic() < deadline, browser.eval(LOST)
            time.sleep(0.05)
        seen = _plain(browser.eval(LOST))
    finally:
        browser.close()
    assert (seen["state"], seen["hidden"], seen["link"]) == ("lost", False, "LINK LOST")
    assert seen["bar"] == {"shown": True, "cls": "fresh-bar lost", "head": "Cannot reach the dashboard.",
                           "text": _showing(snapshot["generated_at_epoch"])}
    assert (seen["age"]["shown"], seen["age"]["cls"]) == (True, "sl-age lost")
    assert re.fullmatch(r"as of \d+m \d\ds ago", seen["age"]["text"]), seen["age"]
    assert seen["band"] == {"tint": GREY, "still": "true"}


def _assert_paused(seen: dict) -> None:
    assert (seen["state"], seen["link"], seen["text"]) == ("paused", "link paused", "PAUSED · resuming"), seen
    assert (seen["bar"]["hidden"], seen["bar"]["say"]) == (True, ""), seen["bar"]
    assert seen["age"]["hidden"] is True, seen["age"]
    assert (_tint(seen["band"]), _still(seen["band"])) == (GREY, "true")
    assert seen["caret"] == page.px_strip("caret", state="link-paused", height=12)
    assert _tint(seen["caret"]) == GREY


@needs_node
def test_a_tab_coming_back_reads_paused_until_the_first_poll_answers(tmp_path: Path, base: dict) -> None:
    snapshot = _aged(base)
    paused, waited, answered = _fresh_run(tmp_path, snapshot, [
        ["hide"], ["advance", 600], ["hold"], ["show"], ["fresh"], ["advance", 7], ["fresh"],
        ["answer", _at(snapshot, NOW + 607)], ["fresh"]])
    _assert_paused(paused)
    _assert_paused(waited)
    assert (answered["state"], answered["text"], answered["bar"]["hidden"]) == ("live", "LIVE", True)


@needs_node
def test_a_tab_coming_back_before_any_poll_answered_reads_paused(tmp_path: Path, base: dict) -> None:
    snapshot = _aged(base)
    harness = _fresh_harness().replace("let holding = false;", "let holding = true;", 1)
    out = _link_run(tmp_path, snapshot, [
        ["hide"], ["advance", 600], ["show"], ["fresh"], ["advance", 7], ["fresh"],
        ["answer", _at(snapshot, NOW + 607)], ["fresh"]], ids=FRESH_IDS, harness=harness)
    paused, waited, answered = [_plain(step["fresh"]) for step in out["steps"] if "fresh" in step]
    assert (paused["age"], waited["age"]) == (None, None)
    _assert_paused({**paused, "age": {"hidden": True}})
    _assert_paused({**waited, "age": {"hidden": True}})
    assert (answered["state"], answered["text"], answered["bar"]["hidden"]) == ("live", "LIVE", True)


@needs_node
def test_a_failed_first_poll_ends_paused_and_lost_waits_for_the_visible_interval(tmp_path: Path, base: dict) -> None:
    snapshot = _aged(base)
    answered, five, seven = _fresh_run(tmp_path, snapshot, [
        ["hide"], ["advance", 600], ["hold"], ["show"], ["answer", "fail"], ["fresh"],
        ["advance", 5], ["fresh"], ["advance", 2], ["fresh"]])
    assert [answered["state"], five["state"], seven["state"]] == ["stale", "stale", "lost"]
    assert seven["text"] == "LINK LOST"


@needs_node
def test_a_tab_that_was_only_hidden_never_reads_link_lost(tmp_path: Path, base: dict) -> None:
    snapshot = _aged(base)
    seen = _fresh_run(tmp_path, snapshot, [
        ["fresh"], ["hide"], ["fresh"], ["advance", 600], ["fresh"], ["show"], ["fresh"],
        ["advance", 5], ["poll", snapshot], ["fresh"], ["advance", 5], ["poll", snapshot], ["fresh"]])
    assert [s["text"] for s in seen if s["text"] == "LINK LOST" or s["state"] == "lost"] == [], seen
    assert all(s["bar"]["cls"] != "fresh-bar lost" for s in seen)
    assert seen[-1]["state"] == "stale"


@needs_node
@pytest.mark.parametrize("start", ["stale", "lost", "paused"])
def test_a_fresh_snapshot_clears_every_state_on_that_redraw(tmp_path: Path, base: dict, start: str) -> None:
    snapshot = _aged(base, STALE_AGE if start == "stale" else 0)
    lead, now = {
        "stale": ([], NOW),
        "lost": ([["fail"], ["poll", snapshot], ["advance", 7], ["ok"]], NOW + 7),
        "paused": ([["hide"], ["advance", 600], ["hold"], ["show"], ["advance", 0]], NOW + 600),
    }[start]
    fresh = _at(snapshot, now)
    arrive = ["answer", fresh] if start == "paused" else ["poll", fresh]
    before, after = _fresh_run(tmp_path, snapshot, lead + [["fresh"], arrive, ["fresh"]])
    assert before["state"] == start
    assert (after["state"], after["link"], after["text"]) == ("live", "link live", "LIVE")
    assert (after["bar"]["hidden"], after["bar"]["say"]) == (True, "")
    assert after["age"] == {"hidden": True, "cls": "sl-age", "text": "", "short": ""}
    html = render_pages(fresh)["index.html"]
    assert after["band"] == _inner(html, "band")
    assert (_tint(after["band"]), _still(after["band"])) == (GREEN, None)
    assert after["line"] == _inner(html, "statusline")
    assert after["caret"] == page.px_strip("caret", state="link-live", height=12)


@needs_node
def test_the_bar_is_announced_again_only_on_a_new_entry(tmp_path: Path, base: dict) -> None:
    snapshot = _aged(base, STALE_AGE)
    fresh = _at(snapshot, NOW)
    polled = [step for _ in range(9) for step in (["advance", 5], ["poll", fresh])]
    entered, ticked, cleared, again, again_ticked = _fresh_run(tmp_path, snapshot, [
        ["fresh"], ["advance", 1], ["fresh"], ["poll", fresh], ["fresh"],
        *polled, ["fresh"], ["advance", 1], ["poll", fresh], ["fresh"]])
    assert entered["bar"]["say"] == ticked["bar"]["say"] == "Snapshot is 9m 14s old. " + _published(NOW - STALE_AGE)
    assert ticked["bar"]["head"] == "Snapshot is 9m 15s old."
    assert (cleared["state"], cleared["bar"]["say"]) == ("live", "")
    assert again["state"] == "stale"
    assert again["bar"]["say"] == again_ticked["bar"]["say"] == "Snapshot is 46s old. " + _published(NOW)
    assert again_ticked["bar"]["head"] == "Snapshot is 47s old."


def _fresh_page(snapshot: dict, name: str = "index.html") -> str:
    shim = (
        "<script>(function () { var real = Date.now.bind(Date); window.__skew = 0; window.__fail = false;"
        " Date.now = function () { return real() + window.__skew; };"
        " window.fetch = function () { if (window.__fail) return Promise.reject(new Error('offline'));"
        " return Promise.resolve({ok: true, status: 200, json: function () { return Promise.resolve("
        + json.dumps(snapshot) + "); }}); }; })();</script>"
    )
    return render_pages(snapshot)[name].replace("<script>", shim + "<script>", 1)


_OPENED = [0]


def _open(browser: Browser, html: str, width: int, fresh: str | None) -> None:
    _OPENED[0] += 1
    mark = f"?n={_OPENED[0]}"
    path = Path(browser.tmp.name) / "page.html"
    path.write_text(html, encoding="utf-8")
    browser.call("Emulation.setDeviceMetricsOverride", width=width, height=FOLD, deviceScaleFactor=1, mobile=False)
    browser.call("Page.navigate", url=path.as_uri() + mark)

    def wait(state: str | None) -> None:
        probe = (f"location.search === {json.dumps(mark)} && document.readyState === 'complete' && "
                 + ("true" if state is None else f"document.body.getAttribute('data-fresh') === {json.dumps(state)}"))
        deadline = time.monotonic() + 20
        while not browser.eval(probe):
            assert time.monotonic() < deadline, (width, state, browser.eval("document.body && document.body.getAttribute('data-fresh')"))
            time.sleep(0.05)

    if fresh == "lost":
        wait("live")
        browser.eval("window.__fail = true; window.__skew = 7000;")
    wait(fresh)
    browser.eval("document.fonts.ready.then(function () { return true; })")
    browser.eval("scrollTo(0, document.documentElement.scrollHeight)")


FORM = """(function () {
  var line = document.getElementById('statusline');
  function colour(v) { var p = document.createElement('i'); p.style.color = v; document.body.appendChild(p);
    var c = getComputedStyle(p).color; p.remove(); return c; }
  function q(sel) { return line.querySelector(sel); }
  var shown = [].filter.call(line.children, function (n) { return n.getClientRects().length > 0 && n.hasAttribute('data-seg'); });
  var vitals = [].filter.call(line.querySelectorAll('.sl-vital'), function (n) { return n.getAttribute('data-seg') !== 'ok-count'; });
  var k = q('[data-seg=queue] .sl-k'), age = q('[data-seg=age]'), t = q('[data-seg=age] .sl-t');
  return {
    vitals: vitals.map(function (n) { return n.getAttribute('data-seg'); }),
    notOk: vitals.filter(function (n) { return !n.classList.contains('ok'); }).map(function (n) { return n.getAttribute('data-seg'); }),
    vitalColours: shown.filter(function (n) { return n.classList.contains('sl-vital'); }).map(function (n) { return getComputedStyle(n).color; }),
    valueColours: [].map.call(line.querySelectorAll('.sl-v'), function (n) { return getComputedStyle(n).color; }),
    brand: getComputedStyle(q('.sl-brand')).backgroundColor,
    age: getComputedStyle(age).color,
    muted: colour('var(--muted)'), warn: colour('var(--warn)'), bad: colour('var(--bad)'),
    queue: {text: k.textContent, short: k.getAttribute('data-short'), after: getComputedStyle(k, '::after').content,
            size: getComputedStyle(k).fontSize},
    ageText: {text: t.textContent, short: t.getAttribute('data-short'), after: getComputedStyle(t, '::after').content,
              size: getComputedStyle(t).fontSize}
  };
})()"""
VITALS = ["poll", "poll-errors", "linear", "webhook", "feeder", "components", "version"]


def _stale_fit(base: dict, age: float) -> dict:
    out = _now(_phone_fit(base), age)
    out["health"]["publish_seconds"] = 15
    return out


@needs_chrome
@pytest.mark.parametrize("fresh", ["live", "stale", "lost"])
def test_each_width_form_per_freshness_state_in_a_browser(base: dict, fresh: str) -> None:
    html = _fresh_page(_stale_fit(base, STALE_AGE if fresh == "stale" else 0))
    browser = Browser()
    try:
        for width, padding in ((1440, "6px 9px"), (1100, "6px 9px"), (1099, "6px 9px"), (699, "6px 7px"), (375, "6px 7px")):
            _open(browser, html, width, fresh)
            line, form = browser.eval(LINE), browser.eval(FORM)
            assert (line["rows"], line["height"], line["sideways"], line["floor"]) == (1, 32, False, True), (fresh, width)
            assert line["padding"] == padding, (fresh, width)
            assert form["vitals"] == VITALS
            ok_count = ["ok-count"] if len(form["notOk"]) < len(VITALS) else []
            if fresh == "live":
                expected = WIDE if width >= 1100 else ["slots", "queue"] + form["notOk"] + ok_count
                assert line["segs"] == expected, (fresh, width)
                assert form["brand"] != form["muted"]
                assert form["queue"]["after"] in ("none", "normal"), width
                continue
            expected = (["slots", "queue", "age"] + VITALS if width >= 1100
                        else ["slots", "queue", "age"] + form["notOk"] + ok_count)
            assert line["segs"] == expected, (fresh, width)
            assert form["notOk"], "the fixture keeps a vital that is not ok"
            assert form["brand"] == form["muted"], (fresh, width)
            assert set(form["vitalColours"]) == {form["muted"]}, (fresh, width)
            assert set(form["valueColours"]) == {form["muted"]}, (fresh, width)
            assert form["age"] == (form["warn"] if fresh == "stale" else form["bad"]), (fresh, width)
            assert (form["queue"]["text"], form["queue"]["short"]) == ("queue", "q")
            seconds = STALE_AGE if fresh == "stale" else 7
            assert re.fullmatch(r"as of (9m \d\ds|\d+s) ago", form["ageText"]["text"]), form["ageText"]
            assert form["ageText"]["short"] == ("9m old" if fresh == "stale" else form["ageText"]["text"][6:-4] + " old")
            if fresh == "stale":
                assert form["ageText"]["text"].startswith("as of 9m "), seconds
            if width < 700:
                assert (form["queue"]["after"], form["queue"]["size"]) == ('"q"', "0px"), width
                assert form["ageText"]["after"] == json.dumps(form["ageText"]["short"]), width
                assert form["ageText"]["size"] == "0px", width
            else:
                assert form["queue"]["after"] in ("none", "normal"), width
                assert form["ageText"]["size"] == "12px" and form["ageText"]["after"] in ("none", "normal"), width
    finally:
        browser.close()


REACH = """(function () {
  var l = document.getElementById('statusline');
  var segs = [].filter.call(l.children, function (n) { return n.hasAttribute('data-seg') && n.getClientRects().length > 0; });
  var out = segs.map(function (n) {
    l.scrollLeft = 0;
    var lb = l.getBoundingClientRect(), nb = n.getBoundingClientRect();
    var left = nb.left - lb.left - l.clientLeft;
    l.scrollLeft = Math.max(0, left + nb.width - l.clientWidth);
    lb = l.getBoundingClientRect(); nb = n.getBoundingClientRect();
    var box = {left: Math.max(lb.left + l.clientLeft, 0), right: Math.min(lb.left + l.clientLeft + l.clientWidth, innerWidth)};
    var cut = [n].concat([].slice.call(n.querySelectorAll('*'))).filter(function (e) {
      return !e.classList.contains('vh') && e.clientWidth > 0 && e.scrollWidth > e.clientWidth; });
    return {seg: n.getAttribute('data-seg'), inside: nb.left >= box.left - 0.5 && nb.right <= box.right + 0.5,
            cut: cut.map(function (e) { return e.className || e.tagName; })};
  });
  l.scrollLeft = 0;
  return {segs: out, overflow: getComputedStyle(l).overflowX};
})()"""


def _f1(base: dict) -> dict:
    return _set("poller.errors.last_hour", 1)(_phone_fit(base))


@needs_chrome
@pytest.mark.parametrize(("case", "width", "fresh", "required"), [
    ("phone-fit-poll-error", 375, "live", ["slots", "queue", "poll-errors", "components", "ok-count"]),
    ("phone-fit-poll-error", 375, "stale", None),
    ("seeded", 375, "live", None),
    ("seeded", 1100, "live", None),
    ("seeded", 375, None, None),
], ids=["phone-fit-375", "phone-fit-375-stale", "seeded-375", "seeded-1100", "seeded-375-no-script"])
def test_every_shown_segment_is_reachable_and_whole_in_a_browser(
    base: dict, case: str, width: int, fresh: str | None, required: list[str] | None,
) -> None:
    snapshot = _f1(base) if case == "phone-fit-poll-error" else copy.deepcopy(base)
    snapshot["health"]["publish_seconds"] = 15
    snapshot = _now(snapshot, STALE_AGE if fresh == "stale" else 0)
    html = _fresh_page(snapshot) if fresh else _no_script(render_pages(snapshot)["index.html"])
    browser = Browser()
    try:
        _open(browser, html, width, fresh)
        line, reach = browser.eval(LINE), browser.eval(REACH)
        assert (line["rows"], line["height"], line["sideways"]) == (1, 32, False), (case, width)
        if required:
            assert [s["seg"] for s in reach["segs"]] == required
        assert reach["segs"], case
        for seg in reach["segs"]:
            assert seg["inside"], (case, width, seg)
            assert seg["cut"] == [], (case, width, seg)
        assert reach["overflow"] in ("auto", "scroll"), reach["overflow"]
    finally:
        browser.close()
