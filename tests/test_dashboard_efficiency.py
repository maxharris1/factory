import html
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

import lib.dashboard as dashboard
from lib import usage_readings
from lib.dashboard import collect_snapshot, render_pages
from lib.dashboard_pages import efficiency
from lib.usage_readings import Reading
from tests.test_dashboard import NOW, add_ticket, make_db, seeded
from tests.test_dashboard_lanes import Browser, _page_with_status_stub, needs_chrome
from tests.test_dashboard_layout import _run_page, _section, _text, needs_node
from tests.test_dashboard_telemetry import add_telemetry

PANELS = ["headroom", "tokens", "rounds", "savings", "jobs-lifetime"]


def _machines(tmp_path: Path) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(
        "devices:\n"
        "  - name: box\n    local: true\n    slot_max: 3\n    harnesses:\n"
        "      claude: claude-bot\n      codex: codex-bot\n      cursor: cursor-bot\n"
        "  - name: pc\n    slot_max: 2\n    tailscale_name: pc\n    workdir_root: /w\n    factory_root: /f\n"
        "    ssh_alias: pc-{bot}\n    harnesses:\n      claude: claude-bot\n      codex: codex-bot\n",
        encoding="utf-8",
    )
    return path


def _readings(connection: sqlite3.Connection) -> None:
    def store(harness: str, bot: str, device: str, *readings: Reading) -> None:
        usage_readings.store(connection, harness=harness, bot=bot, device=device, job_id="j",
                             readings=list(readings))

    store("claude", "claude-bot", "box",
          Reading("five_hour", 0.42, int(NOW + 7800), NOW - 60),
          Reading("seven_day", 0.3, int(NOW + 3 * 86400), NOW - 60))
    store("codex", "codex-bot", "box",
          Reading("five_hour", 0.1, int(NOW + 3600), NOW - 60),
          Reading("seven_day", 0.8, int(NOW - 100), NOW - 60))
    store("codex", "codex-bot", "pc",
          Reading("five_hour", 0.5, int(NOW - 10), NOW - 3 * 3600),
          Reading("seven_day", 0.5, int(NOW - 10), NOW - 3 * 3600))


def _snapshot(tmp_path: Path, *, readings: bool = True, telemetry: tuple[dict, ...] = ()) -> dict:
    db, log = seeded(tmp_path)
    for n, row in enumerate(telemetry):
        add_telemetry(db, f"d-job-{n:03d}", **{"finished_at": NOW - 100 - n, "exit_code": 0,
                                               "exit_reason": "success; exit 0", **row})
    with sqlite3.connect(db) as connection:
        for repo, ticket, state, rounds in (
            ("maxharris1/factory", "65", "merged", 0),
            ("maxharris1/cleffy", "96", "merged", 0),
            ("maxharris1/factory", "77", "done", 1),
            ("maxharris1/factory", "98", "handed-off", 2),
            ("maxharris1/factory", "99", "building", 0),
        ):
            add_ticket(connection, repo, ticket, state=state, review_round=rounds)
        if readings:
            _readings(connection)
    return collect_snapshot(db, log, now=NOW, machines_path=_machines(tmp_path))


def _page(snapshot: dict) -> str:
    return render_pages(snapshot)["efficiency.html"]


def _inner(page: str, ident: str) -> str:
    whole = _section(re.sub(r"<script>.*?</script>", "", page, flags=re.S), ident)
    return whole[whole.index(">") + 1 : whole.rindex("<")]


def _with_share(snapshot: dict, scripted: int, grok: int) -> dict:
    total = scripted + grok
    snapshot["handoff_savings"].update(
        scripted=scripted, bot_to_bot=grok, scripted_share=scripted / total if total else None
    )
    return snapshot


SHARES = [(0, 10, 0), (65, 35, 65), (100, 0, 100), (129, 71, 65), (0, 0, None)]


@pytest.mark.parametrize(("scripted", "grok", "lit"), SHARES)
def test_waffle_lights_the_scripted_share_rounded_to_a_whole_percent(
    tmp_path: Path, scripted: int, grok: int, lit: int | None
) -> None:
    savings = _inner(_page(_with_share(_snapshot(tmp_path), scripted, grok)), "savings")
    waffle = re.search(r'<div class="(waffle[^"]*)" role="img" aria-label="([^"]*)">(.*?)</div>', savings)
    assert waffle, savings
    cls, label, cells = waffle.groups()
    assert cells.count("<span") == 100
    assert cells.count('<span class="s">') == (lit or 0)
    if lit is None:
        assert cls == "waffle none" and label == "no handoffs yet"
        assert "—" in _text(savings)
    else:
        assert cls == "waffle"
        assert label == f"{lit} of 100 squares are scripted handoffs, {100 - lit} are grok wakes"
        assert f"{lit}%" in _text(savings)


@needs_node
@pytest.mark.parametrize(("scripted", "grok", "lit"), SHARES)
def test_script_draws_the_same_waffle(tmp_path: Path, scripted: int, grok: int, lit: int | None) -> None:
    snapshot = _with_share(_snapshot(tmp_path), scripted, grok)
    out = _run_page(tmp_path, snapshot, "efficiency", ["savings"])
    assert out["html"]["savings"] == _inner(_page(snapshot), "savings")


def test_definition_texts_are_unchanged(tmp_path: Path) -> None:
    savings = _inner(_page(_snapshot(tmp_path)), "savings")
    assert (
        f'<p class="definition" id="share-definition">{html.escape(dashboard.SCRIPTED_SHARE_DEFINITION)}</p>'
        in savings
    )
    assert (
        "<details><summary>how each count is made</summary>"
        f'<p class="muted">{html.escape(dashboard.HANDOFF_SAVINGS_DEFINITION)}</p></details>'
    ) in savings


def _rows(table: str) -> dict[str, list[str]]:
    rows = {}
    for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)[1:]:
        cells = [_text(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        rows[cells[0]] = cells[1:]
    return rows


def test_headroom_shows_the_fixture_readings_and_no_number_without_one(tmp_path: Path) -> None:
    rows = _rows(_inner(_page(_snapshot(tmp_path)), "headroom"))
    assert rows["claude-bot box"] == ["42% used", "30% used", "in 2h 10m", "58%"]
    assert rows["codex-bot box"] == ["10% used", "stale", "in 1h 00m", "90% (5-hour only)"]
    assert rows["claude-bot pc"] == ["no reading", "no reading", "—", "no reading yet"]
    assert rows["codex-bot pc"] == ["stale", "stale", "—", "reading 3h 00m old"]
    assert rows["cursor-bot"] == ["not tracked"] * 4
    assert rows["grok"] == ["not tracked"] * 4
    for name in ("claude-bot pc", "codex-bot pc", "cursor-bot", "grok"):
        assert not any(re.search(r"\d+%", cell) for cell in rows[name]), name


def test_headroom_colours_follow_the_page_thresholds(tmp_path: Path) -> None:
    headroom = _inner(_page(_snapshot(tmp_path)), "headroom")
    assert '<td class="ef-v ok">58%</td>' in headroom
    assert '<td class="ef-v">90% (5-hour only)</td>' in headroom


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_headroom_header_has_no_proposed_chip(tmp_path: Path, render: str) -> None:
    snapshot = _snapshot(tmp_path)
    headroom = (_inner(_page(snapshot), "headroom") if render == "server"
                else _run_page(tmp_path, snapshot, "efficiency", ["headroom"])["html"]["headroom"])
    assert headroom.startswith("<h2><span>Subscription headroom</span></h2>"), headroom[:120]
    assert "proposed" not in headroom


def _seven_day_only(tmp_path: Path) -> dict:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        usage_readings.store(connection, harness="claude", bot="claude-bot", device="box", job_id="j",
                             readings=[Reading("seven_day", 0.26, int(NOW + 86400), NOW - 60)])
    return collect_snapshot(db, log, now=NOW, machines_path=_machines(tmp_path))


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_headroom_on_one_window_is_neutral_and_names_it(tmp_path: Path, render: str) -> None:
    snapshot = _seven_day_only(tmp_path)
    headroom = (_inner(_page(snapshot), "headroom") if render == "server"
                else _run_page(tmp_path, snapshot, "efficiency", ["headroom"])["html"]["headroom"])
    assert _rows(headroom)["claude-bot box"] == ["no reading", "26% used", "in 1d 0h", "74% (7-day only)"]
    assert '<td class="ef-v">74% (7-day only)</td>' in headroom


def test_headroom_without_the_table_says_no_telemetry(tmp_path: Path) -> None:
    rows = _rows(_inner(_page(_snapshot(tmp_path, readings=False)), "headroom"))
    for name in ("claude-bot box", "codex-bot box", "claude-bot pc", "codex-bot pc"):
        assert rows[name] == ["no telemetry"] * 4, name
    assert rows["cursor-bot"] == ["not tracked"] * 4


def _render(tmp_path: Path, snapshot: dict, render: str, ident: str) -> str:
    if render == "server":
        return _inner(_page(snapshot), ident)
    return _run_page(tmp_path, snapshot, "efficiency", [ident])["html"][ident]


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
@pytest.mark.parametrize(("done", "failed", "lines"), [
    (174, 74, ["174 jobs done", "74 failed", "70% of 248 finished jobs succeeded"]),
    (0, 0, ["0 jobs done", "0 failed", "no finished jobs yet"]),
])
def test_lifetime_is_one_line_of_done_failed_and_success_rate(
    tmp_path: Path, render: str, done: int, failed: int, lines: list[str]
) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["queue"]["totals"].update(done=done, failed=failed)
    panel = _render(tmp_path, snapshot, render, "jobs-lifetime")
    assert panel.startswith("<h2><span>Lifetime</span></h2>"), panel[:80]
    assert 'class="count"' not in panel
    parts = [_text(m) for m in re.findall(r'<span class="ef-(?:life|pct)">(.*?)</span>(?=<span|$)', panel)]
    assert parts == lines, panel
    assert f'<b class="ok">{done}</b>' in panel and f'<b class="bad">{failed}</b>' in panel


def _shipped(snapshot: dict, rounds: list[int]) -> dict:
    base = next(t for t in snapshot["tickets"] if t["state"] == "merged")
    snapshot["tickets"] = [
        {**base, "ticket_id": str(500 + n), "source": f"#{500 + n}", "state": "merged", "round": r}
        for n, r in enumerate(rounds)
    ]
    return snapshot


def _bars(panel: str) -> list[tuple[str, str, str]]:
    return [
        (label, width, _text(n))
        for label, width, n in re.findall(
            r'<div class="ef-rb"><span class="ef-k">(.*?)</span><span class="ef-track" aria-hidden="true">'
            r'<span class="ef-fill(?: zero)?" style="width:([\d.]+%)"></span></span>'
            r'<span class="ef-n">(.*?)</span></div>',
            panel,
        )
    ]


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_revise_rounds_draw_five_bars_on_the_fixture(tmp_path: Path, render: str) -> None:
    panel = _render(tmp_path, _snapshot(tmp_path), render, "rounds")
    assert '<span class="count">4 shipped tickets</span>' in panel
    assert _bars(panel) == [
        ("0 rounds", "100.0%", "2 · 50%"),
        ("1 round", "50.0%", "1 · 25%"),
        ("2 rounds", "50.0%", "1 · 25%"),
        ("3 rounds", "0.0%", "0 · 0%"),
        ("4 or more", "0.0%", "0 · 0%"),
    ]
    assert "2 of 4 shipped with no revise round" in _text(panel)


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_revise_rounds_bars_are_proportional_to_the_largest_bucket(tmp_path: Path, render: str) -> None:
    snapshot = _shipped(_snapshot(tmp_path), [0] * 88 + [1] * 71 + [2] * 31 + [3] * 12 + [4, 5, 7, 7])
    panel = _render(tmp_path, snapshot, render, "rounds")
    assert '<span class="count">206 shipped tickets</span>' in panel
    assert _bars(panel) == [
        ("0 rounds", "100.0%", "88 · 43%"),
        ("1 round", "80.7%", "71 · 34%"),
        ("2 rounds", "35.2%", "31 · 15%"),
        ("3 rounds", "13.6%", "12 · 6%"),
        ("4 or more", "4.5%", "4 · 2%"),
    ]
    assert panel.count('class="ef-fill zero"') == 1
    assert '<span class="ef-fill zero" style="width:100.0%"></span>' in panel
    assert re.findall(r"<b[^>]*>", panel) == ['<b class="ok">'] + ["<b>"] * 4
    assert re.search(r"[\w-]+#\d+", panel) is None
    assert ('<div class="ef-foot"><p class="ef-note">88 of 206 shipped with no revise round</p>'
            '<a href="job.html">see shipped tickets</a></div>') in panel


def test_revise_rounds_with_no_shipped_ticket(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["tickets"] = [t for t in snapshot["tickets"] if t["state"] == "building"]
    assert "no shipped tickets yet" in _text(_inner(_page(snapshot), "rounds"))


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_who_is_working_is_gone(tmp_path: Path, render: str) -> None:
    snapshot = _snapshot(tmp_path)
    page = _page(snapshot)
    if render == "server":
        assert 'id="working"' not in page
        assert "Who is working now" not in page
    else:
        out = _run_page(tmp_path, snapshot, "efficiency", ["working"])
        assert out["html"]["working"] == "" and out["text"]["working"] == ""


@needs_node
@pytest.mark.parametrize("readings", [True, False])
def test_script_twins_match_the_server_render(tmp_path: Path, readings: bool) -> None:
    snapshot = _snapshot(tmp_path, readings=readings)
    page = _page(snapshot)
    out = _run_page(tmp_path, snapshot, "efficiency", PANELS)
    for ident in PANELS:
        assert out["html"][ident] == _inner(page, ident), ident


LONG_REPO = "maxharris1/the-factory-dashboard-efficiency-page-on-a-phone"
WIDE = tuple(
    {"harness": harness, "repo": LONG_REPO, "tokens_in": 2_635_600_591 + n,
     "tokens_cached": 812_400_000, "tokens_out": 45_200_000, "cost_usd": 12345.675 if n % 2 else None}
    for n, harness in enumerate(["claude", "codex", "claude", "codex", "claude"])
)


def _load(browser: Browser, snapshot: dict, width: int, script: bool) -> None:
    browser.call("Emulation.setScriptExecutionDisabled", value=not script)
    if script:
        browser.open(_page_with_status_stub(snapshot, "efficiency.html"), width)
        return
    bare = Path(browser.tmp.name) / f"bare-{width}.html"
    bare.write_text(_page(snapshot), encoding="utf-8")
    browser.call("Emulation.setDeviceMetricsOverride", width=width, height=900, deviceScaleFactor=1, mobile=False)
    browser.call("Page.navigate", url=bare.as_uri())
    deadline = time.monotonic() + 30
    while not browser.eval(f"location.href === {json.dumps(bare.as_uri())} && document.readyState === 'complete' "
                           "&& document.fonts.status === 'loaded'"):
        assert time.monotonic() < deadline, f"{bare.name} never loaded"
        time.sleep(0.05)


WIDER_THAN_THE_PAGE = """[].filter.call(document.querySelectorAll('main .panel'), function (n) {
  return n.getBoundingClientRect().right > innerWidth + 0.5;
}).map(function (n) { return n.id; })"""


@needs_chrome
def test_efficiency_has_no_page_scroll_at_375_in_a_browser(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, telemetry=WIDE)
    assert LONG_REPO in _inner(_page(snapshot), "tokens")
    browser = Browser()
    try:
        for script in (False, True):
            _load(browser, snapshot, 375, script)
            assert browser.eval("document.querySelectorAll('#tokens .ef-scroll > table').length") == 3, script
            assert browser.eval(WIDER_THAN_THE_PAGE) == [], script
            assert browser.eval("document.documentElement.scrollWidth") <= browser.eval("innerWidth"), script
            lefts = browser.eval(
                "[].map.call(document.querySelectorAll('main .panel'), function (n) {"
                " return [n.id, n.getBoundingClientRect().left]; })"
            )
            assert [ident for ident, _ in lefts] == PANELS, script
            assert len({left for _, left in lefts}) == 1, (script, lefts)
    finally:
        browser.close()


TOPS = """(function () {
  var out = {};
  [].forEach.call(document.querySelectorAll('main .panel'), function (n) { out[n.id] = n.getBoundingClientRect().top; });
  return out;
})()"""


@needs_chrome
def test_efficiency_rows_at_1440_in_a_browser(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    browser = Browser()
    try:
        for script in (False, True):
            _load(browser, snapshot, 1440, script)
            top = browser.eval(TOPS)
            assert top["headroom"] == top["tokens"], (script, top)
            assert top["rounds"] == top["savings"] > top["headroom"], (script, top)
            assert top["jobs-lifetime"] > top["rounds"], (script, top)
    finally:
        browser.close()


def test_efficiency_parts_carry_no_motion_of_their_own() -> None:
    assert "@keyframes" not in efficiency.CSS and "animation" not in efficiency.CSS
    assert "transition" not in efficiency.CSS


def test_panels_are_in_the_artboard_order(tmp_path: Path) -> None:
    page = _page(_snapshot(tmp_path))
    found = [page.index(f'id="{ident}"') for ident in PANELS]
    assert found == sorted(found)
    rows = re.findall(r'<div class="ef-row">(.*?</section>)</div>', _section(page, "hud"))
    assert [re.findall(r'<section class="[^"]*" id="([^"]+)"', row) for row in rows] == [
        ["headroom", "tokens"], ["rounds", "savings"],
    ]


def test_an_empty_factory_renders_every_panel(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    snapshot = collect_snapshot(db, tmp_path / "poll.log", now=NOW)
    page = _page(snapshot)
    for ident in PANELS:
        assert f'id="{ident}"' in page, ident
    assert "no shipped tickets yet" in page and "no finished jobs yet" in page
