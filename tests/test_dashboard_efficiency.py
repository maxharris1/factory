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


MACHINES = (
    "devices:\n"
    "  - name: box\n    local: true\n    slot_max: 3\n    harnesses:\n"
    "      claude: claude-bot\n      codex: codex-bot\n      cursor: cursor-bot\n"
    "  - name: pc\n    slot_max: 2\n    tailscale_name: pc\n    workdir_root: /w\n    factory_root: /f\n"
    "    ssh_alias: pc-{bot}\n    harnesses:\n      claude: claude-bot\n"
)
CLAUDE_ONLY = "devices:\n  - name: box\n    local: true\n    slot_max: 3\n    harnesses:\n      claude: claude-bot\n"


def _machines(tmp_path: Path, text: str = MACHINES) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _store(connection: sqlite3.Connection, harness: str, bot: str, device: str, *readings: Reading) -> None:
    usage_readings.store(connection, harness=harness, bot=bot, device=device, job_id="j", readings=list(readings))


def _readings(connection: sqlite3.Connection) -> None:
    _store(connection, "claude", "claude-bot", "box",
           Reading("five_hour", 0.42, int(NOW + 7800), NOW - 600),
           Reading("seven_day", 0.3, int(NOW + 3 * 86400), NOW - 600))
    _store(connection, "claude", "claude-bot", "pc",
           Reading("five_hour", 0.41, int(NOW + 7500), NOW - 60),
           Reading("seven_day", 0.26, int(NOW + 3 * 86400), NOW - 60))
    _store(connection, "codex", "codex-bot", "box",
           Reading("five_hour", 0.1, int(NOW + 3600), NOW - 60),
           Reading("seven_day", 0.8, int(NOW - 100), NOW - 60))
    _store(connection, "codex", "codex-bot", "pc",
           Reading("five_hour", 0.5, int(NOW + 3600), NOW - 3 * 3600),
           Reading("seven_day", 0.5, int(NOW + 86400), NOW - 3 * 3600))


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


def _render(tmp_path: Path, snapshot: dict, render: str, ident: str) -> str:
    if render == "server":
        return _inner(_page(snapshot), ident)
    return _run_page(tmp_path, snapshot, "efficiency", [ident])["html"][ident]


def _rows(headroom: str) -> dict[str, list[str]]:
    body = headroom[headroom.index("<tbody>") : headroom.index("</tbody>")]
    rows = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        cells = [_text(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        rows[cells[0]] = cells[1:]
    return rows


def _blocks(headroom: str) -> list[str]:
    phone = headroom[headroom.index('<div class="ef-accts">') : headroom.index('<p class="ef-hnote-ph">')]
    return re.split(r'(?=<div class="ef-(?:acct|un)">)', phone)[1:]


METER = r'<span class="ef-meter" role="img" aria-label="([^"]*)">((?:<span(?: class="\w+")?></span>)*)</span>'


def _meters(markup: str) -> list[tuple[str, list[str | None]]]:
    return [(label, [cls or None for cls in re.findall(r'<span(?: class="(\w+)")?></span>', cells)])
            for label, cells in re.findall(METER, markup)]


def _usage_snapshot(tmp_path: Path, machines: str = MACHINES, **used: float | None) -> dict:
    db, log = seeded(tmp_path)
    resets = {"five_hour": NOW + 7500, "seven_day": NOW + 86400}
    with sqlite3.connect(db) as connection:
        usage_readings.ensure_table(connection)
        readings = [Reading(w, used[w], int(resets[w]), NOW - 60) for w in resets if used.get(w) is not None]
        if readings:
            _store(connection, "claude", "claude-bot", "box", *readings)
    return collect_snapshot(db, log, now=NOW, machines_path=_machines(tmp_path, machines))


CLAUDE = "claude claude-bot used on box · pc"
CODEX = "codex codex-bot used on box"


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_headroom_has_one_row_per_account(tmp_path: Path, render: str) -> None:
    rows = _rows(_render(tmp_path, _snapshot(tmp_path), render, "headroom"))
    assert list(rows) == [CLAUDE, CODEX, "cursor", "grok"]
    assert rows[CLAUDE] == ["41% used", "26% used", "in 2h 05m", "59%"]
    assert rows[CODEX] == ["10% used", "stale", "in 1h 00m", "90% (5-hour only)"]


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_headroom_meter_lights_5_percent_per_cell(tmp_path: Path, render: str) -> None:
    table = _render(tmp_path, _snapshot(tmp_path), render, "headroom").split("</table>")[0]
    assert _meters(table) == [
        ("claude headroom 59 percent: 12 of 20 cells lit", ["ok"] * 12 + [None] * 8),
        ("codex headroom 90 percent, from the 5-hour window only: 18 of 20 cells lit",
         ["neutral"] * 18 + [None] * 2),
    ]
    assert '<span class="ef-hn ok">59%</span>' in table
    assert '<span class="ef-hn neutral">90%</span><span class="ef-meter"' in table


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
@pytest.mark.parametrize(("five_hour", "tone", "lit"), [
    (0.5, "ok", 10), (0.51, "warn", 10), (0.8, "warn", 4), (0.81, "bad", 4), (0.98, "bad", 0), (1.0, "bad", 0),
])
def test_headroom_colours_follow_the_page_thresholds(
    tmp_path: Path, render: str, five_hour: float, tone: str, lit: int
) -> None:
    table = _render(tmp_path, _usage_snapshot(tmp_path, five_hour=five_hour, seven_day=0.1),
                    render, "headroom").split("</table>")[0]
    pct = round((1 - five_hour) * 100)
    assert f'<span class="ef-hn {tone}">{pct}%</span>' in table
    label, cells = _meters(table)[0]
    assert cells == [tone] * lit + [None] * (20 - lit)
    assert label == f"claude headroom {pct} percent: {lit} of 20 cells lit"


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_headroom_header_has_no_proposed_chip(tmp_path: Path, render: str) -> None:
    headroom = _render(tmp_path, _snapshot(tmp_path), render, "headroom")
    assert headroom.startswith(
        '<h2><span>Subscription headroom</span><span class="count">per account</span></h2>'
    ), headroom[:120]
    assert "proposed" not in headroom


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_headroom_notes_replace_the_lede(tmp_path: Path, render: str) -> None:
    headroom = _render(tmp_path, _snapshot(tmp_path), render, "headroom")
    assert "ef-lede" not in headroom and "read at the start of every job" not in headroom
    assert ('</table></div><p class="ef-hnote">Headroom is what is left in the fullest window. '
            "One account is shared by every device that runs that harness, "
            "so the reading is per account, not per device.</p>") in headroom
    assert headroom.endswith('<p class="ef-hnote-ph">Headroom is what is left in the fullest window. '
                             "One account is shared by every device that runs that harness.</p>")


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
@pytest.mark.parametrize(("used", "cells", "number", "label"), [
    ({"seven_day": 0.26}, ["no reading", "26% used", "in 1d 0h", "74% (7-day only)"], "74%",
     "claude headroom 74 percent, from the 7-day window only: 15 of 20 cells lit"),
    ({"five_hour": 0.41}, ["41% used", "no reading", "in 2h 05m", "59% (5-hour only)"], "59%",
     "claude headroom 59 percent, from the 5-hour window only: 12 of 20 cells lit"),
])
def test_headroom_on_one_window_is_neutral_and_names_it(
    tmp_path: Path, render: str, used: dict, cells: list[str], number: str, label: str
) -> None:
    headroom = _render(tmp_path, _usage_snapshot(tmp_path, **used), render, "headroom")
    table = headroom.split("</table>")[0]
    assert _rows(headroom)["claude claude-bot used on box · pc"] == cells
    only = cells[3].split(" ", 1)[1]
    assert (f'<span class="ef-hn neutral">{number}</span>' in table
            and f'<span class="ef-tag">{only}</span>' in table)
    lit = int(label.split(": ")[1].split()[0])
    assert _meters(table)[0] == (label, ["neutral"] * lit + [None] * (20 - lit))


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_a_claude_window_null_in_three_replies_shows_no_reading(tmp_path: Path, render: str) -> None:
    db, log = seeded(tmp_path)
    resets = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 86400))
    reply = {"type": "control_response", "response": {
        "subtype": "success", "request_id": usage_readings.GET_USAGE_ID, "response": {
            "rate_limits_available": True, "rate_limits": {
                "five_hour": None, "seven_day": {"utilization": 26, "resets_at": resets}}}}}
    with sqlite3.connect(db) as connection:
        for offset in (180, 120, 60):
            parsed = usage_readings.parse_get_usage(json.dumps(reply).encode(), NOW - offset)
            _store(connection, "claude", "claude-bot", "box", *parsed)
    snapshot = collect_snapshot(db, log, now=NOW, machines_path=_machines(tmp_path))
    assert _rows(_render(tmp_path, snapshot, render, "headroom"))[CLAUDE] == [
        "no reading", "26% used", "in 1d 0h", "74% (7-day only)"]


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_a_window_not_on_the_plan_leaves_the_other_as_the_whole_plan(tmp_path: Path, render: str) -> None:
    snapshot = _snapshot(tmp_path)
    [codex] = [e for e in snapshot["usage"]["accounts"] if e["harness"] == "codex"]
    codex.update(
        five_hour={"used": None, "resets_at": None, "observed_at": NOW - 60, "state": "not on plan"},
        seven_day={"used": 0.68, "resets_at": int(NOW + 86400), "observed_at": NOW - 60, "state": "ok"},
        headroom=0.32, state="ok",
    )
    headroom = _render(tmp_path, snapshot, render, "headroom")
    table = headroom.split("</table>")[0]
    assert _rows(headroom)[CODEX] == ["not on this plan", "68% used", "in 1d 0h", "32%"]
    assert '<td class="ef-v muted">not on this plan</td>' in table
    assert '<span class="ef-hn warn">32%</span>' in table
    assert _meters(table)[1] == ("codex headroom 32 percent: 6 of 20 cells lit", ["warn"] * 6 + [None] * 14)
    assert "7-day only" not in headroom


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_headroom_with_no_window_shows_no_number(tmp_path: Path, render: str) -> None:
    headroom = _render(tmp_path, _usage_snapshot(tmp_path), render, "headroom")
    table = headroom.split("</table>")[0]
    assert _rows(headroom)["claude claude-bot used on box · pc"] == ["no reading"] * 3 + [""]
    assert _meters(table)[0] == ("claude headroom: no reading", [None] * 20)
    room = re.findall(r'<td><div class="ef-room">(.*?)</div></td>', table)
    assert room and not any("%" in cell for cell in room)


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_a_lane_limit_replaces_the_number_and_leaves_the_meter_unlit(tmp_path: Path, render: str) -> None:
    limit = "usage limit until 2026-11-17 03:00 UTC"
    snapshot = _snapshot(tmp_path)
    snapshot["usage"]["accounts"][0]["limit_label"] = limit
    headroom = _render(tmp_path, snapshot, render, "headroom")
    table = headroom.split("</table>")[0]
    assert _rows(headroom)[CLAUDE][3] == limit
    assert f'<span class="ef-lim bad">{limit}</span>' in table
    assert _meters(table)[0] == (f"claude headroom: {limit}", [None] * 20)
    assert f'<span class="ef-lim bad">{limit}</span>' in _blocks(headroom)[0]


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_untracked_harness_is_one_muted_line(tmp_path: Path, render: str) -> None:
    headroom = _render(tmp_path, _snapshot(tmp_path), render, "headroom")
    table = headroom.split("</table>")[0]
    assert re.findall(r'<tr class="ef-un">(.*?)</tr>', table) == [
        f'<td><span class="bot {h}">{h}</span></td><td class="ef-v muted" colspan="4">not tracked</td>'
        for h in ("cursor", "grok")
    ]
    assert table.count("bot cursor") == 1 and "cursor-bot" not in headroom


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_harness_without_account_is_not_listed(tmp_path: Path, render: str) -> None:
    headroom = _render(tmp_path, _usage_snapshot(tmp_path, CLAUDE_ONLY, five_hour=0.4, seven_day=0.2),
                       render, "headroom")
    assert list(_rows(headroom)) == ["claude claude-bot used on box", "grok"]
    assert [_text(block).split()[0] for block in _blocks(headroom)] == ["claude", "grok"]
    assert "codex" not in headroom and "cursor" not in headroom


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_headroom_phone_block_per_account(tmp_path: Path, render: str) -> None:
    blocks = _blocks(_render(tmp_path, _snapshot(tmp_path), render, "headroom"))
    assert [_text(block) for block in blocks] == [
        f"{CLAUDE} 59% headroom 5-hour 41% used 7-day 26% used resets in 2h 05m",
        f"{CODEX} 90% headroom (5-hour only) 5-hour 10% used 7-day stale resets in 1h 00m",
        "cursor not tracked",
        "grok not tracked",
    ]
    assert [block[: block.index(">") + 1] for block in blocks] == ['<div class="ef-acct">'] * 2 + [
        '<div class="ef-un">'] * 2
    assert [len(_meters(block)) for block in blocks] == [1, 1, 0, 0]
    assert '<span class="ef-hn ok">59%</span>' in blocks[0]
    assert '<div class="ef-hl">headroom (5-hour only)</div>' in blocks[1]
    assert re.findall(r'<div class="ef-wk">([^<]*)</div>', blocks[0]) == ["5-hour", "7-day", "resets"]


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_headroom_without_the_table_says_no_telemetry(tmp_path: Path, render: str) -> None:
    headroom = _render(tmp_path, _snapshot(tmp_path, readings=False), render, "headroom")
    rows = _rows(headroom)
    assert rows[CLAUDE] == rows[CODEX] == ["no telemetry"] * 3 + [""]
    assert rows["cursor"] == rows["grok"] == ["not tracked"]
    assert _meters(headroom.split("</table>")[0]) == [
        (f"{h} headroom: no telemetry", [None] * 20) for h in ("claude", "codex")
    ]
    assert "%" not in _text(headroom)


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
@pytest.mark.parametrize(("readings", "telemetry"), [(True, ()), (False, ()), (True, "WIDE")])
def test_script_twins_match_the_server_render(tmp_path: Path, readings: bool, telemetry: object) -> None:
    snapshot = _snapshot(tmp_path, readings=readings, telemetry=WIDE if telemetry else ())
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


HEADROOM_VIEWS = """(function () {
  var panel = document.getElementById('headroom').getBoundingClientRect();
  function shown(n) { return getComputedStyle(n).display; }
  return {
    table: shown(document.querySelector('#headroom .ef-scroll')),
    blocks: [].map.call(document.querySelectorAll('#headroom .ef-acct'), function (n) {
      var r = n.getBoundingClientRect();
      return r.width > 0 && r.left >= panel.left && r.right <= panel.right && n.scrollWidth === n.clientWidth;
    }),
    notes: [shown(document.querySelector('.ef-hnote')), shown(document.querySelector('.ef-hnote-ph'))]
  };
})()"""


@needs_chrome
def test_efficiency_has_no_page_scroll_at_375_in_a_browser(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, telemetry=WIDE)
    assert LONG_REPO in _inner(_page(snapshot), "tokens")
    browser = Browser()
    try:
        for script in (False, True):
            _load(browser, snapshot, 375, script)
            assert browser.eval("document.querySelectorAll('#tokens .ef-scroll > table').length") == 2, script
            assert browser.eval(WIDER_THAN_THE_PAGE) == [], script
            assert browser.eval("document.documentElement.scrollWidth") <= browser.eval("innerWidth"), script
            lefts = browser.eval(
                "[].map.call(document.querySelectorAll('main .panel'), function (n) {"
                " return [n.id, n.getBoundingClientRect().left]; })"
            )
            assert [ident for ident, _ in lefts] == PANELS, script
            assert len({left for _, left in lefts}) == 1, (script, lefts)
            assert browser.eval(HEADROOM_VIEWS) == {
                "table": "none", "blocks": [True, True], "notes": ["none", "block"]}, script
    finally:
        browser.close()


COST_ROWS = (
    {"harness": "claude", "repo": "maxharris1/cleffy", "tokens_in": 2_000_000_000,
     "tokens_cached": 1_900_000_000, "tokens_out": 30_000_000, "cost_usd": 1057.64},
    {"harness": "codex", "repo": "maxharris1/factory", "tokens_in": 635_600_591,
     "tokens_cached": 510_000_000, "tokens_out": 1_800_000},
)


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_cost_panel_has_four_tiles_in_order(tmp_path: Path, render: str) -> None:
    panel = _render(tmp_path, _snapshot(tmp_path, telemetry=COST_ROWS), render, "tokens")
    stats = re.search(r'<div class="stats">(.*?)</div><h3', panel)
    assert stats, panel
    assert stats.group(1) == (
        '<div class="stat"><b>$1,057.64</b><span>cost</span></div>'
        '<div class="stat"><b>2</b><span>jobs</span></div>'
        '<div class="stat" title="2,635,600,591"><b>2.636B</b><span>tokens in</span>'
        '<div class="cached" title="2.41B of 2.636B tokens in were cached">91% cached</div></div>'
        '<div class="stat" title="31,800,000"><b>31.8M</b><span>tokens out</span></div>'
    )


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
@pytest.mark.parametrize(("tokens_in", "cached", "line"), [
    (200, 101, "51% cached"), (200, 99, "50% cached"), (3, 2, "67% cached"), (5, 0, "0% cached"), (0, 0, None),
])
def test_cached_share_rounds_half_up_and_is_absent_without_tokens_in(
    tmp_path: Path, render: str, tokens_in: int, cached: int, line: str | None
) -> None:
    snapshot = _snapshot(tmp_path, telemetry=({"harness": "claude", "tokens_in": tokens_in,
                                               "tokens_cached": cached, "tokens_out": 7},))
    panel = _render(tmp_path, snapshot, render, "tokens")
    found = re.findall(r'<div class="cached" title="[^"]*">(.*?)</div>', panel)
    assert found == ([line] if line else []), panel


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_a_harness_without_cost_says_no_cost_reported(tmp_path: Path, render: str) -> None:
    panel = _render(tmp_path, _snapshot(tmp_path, telemetry=COST_ROWS), render, "tokens")
    harness = panel[panel.index("per harness</h3>"):panel.index("</table>")]
    assert ('<tr><td><span class="bot codex">codex</span></td><td class="num">1</td><td class="num">635,600,591</td>'
            '<td class="num">510,000,000</td><td class="num">1,800,000</td>'
            '<td class="num muted">no cost reported</td></tr>') in harness, harness
    assert '<td class="num">$1,057.64</td>' in harness
    repo = _rows(panel[panel.index("<details>"):])
    assert repo["maxharris1/factory"][-1] == "no cost reported" and repo["maxharris1/cleffy"][-1] == "$1,057.64"
    assert '<td class="num muted">no cost reported</td>' in panel[panel.index("<details>"):]

    (tmp_path / "unpriced").mkdir()
    unpriced = _snapshot(tmp_path / "unpriced", telemetry=({"harness": "codex"}, {"harness": "cursor"}))
    panel = _render(tmp_path, unpriced, render, "tokens")
    assert '<div class="stat"><b class="none">no cost reported</b><span>cost</span></div>' in panel, panel
    assert "$0.00" not in panel


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_per_repo_sits_in_a_closed_details(tmp_path: Path, render: str) -> None:
    panel = _render(tmp_path, _snapshot(tmp_path, telemetry=COST_ROWS), render, "tokens")
    assert re.findall(r"<details[^>]*>", panel) == ["<details>"], panel
    assert panel.index("per harness</h3>") < panel.index("<table>") < panel.index("<details>")
    assert panel.count("<table>") == 2
    repo = re.search(r"<details><summary>per repo</summary>(.*?)</details>", panel)
    assert repo, panel
    assert "<table>" in repo.group(1) and "<th>repo</th>" in repo.group(1)
    assert set(_rows(repo.group(1))) == {"maxharris1/cleffy", "maxharris1/factory"}
    assert "per repo</h3>" not in panel


@pytest.mark.parametrize("render", ["server", pytest.param("script", marks=needs_node)])
def test_recent_jobs_table_is_gone(tmp_path: Path, render: str) -> None:
    snapshot = _snapshot(tmp_path, telemetry=COST_ROWS)
    assert [row["job"] for row in snapshot["telemetry"]["recent"]] == ["d-job-00", "d-job-00"]
    panel = _render(tmp_path, snapshot, render, "tokens")
    assert "recent jobs" not in _text(panel)
    assert "success; exit 0" not in panel and "d-job-00" not in panel
    assert "<th>job</th>" not in panel and "<th>exit</th>" not in panel


COST_TILES_AND_TABLES = """(function () {
  var tiles = [].map.call(document.querySelectorAll('#tokens .stat'), function (n) {
    var r = n.getBoundingClientRect(); return [n.querySelector('span').textContent, r.left, r.top];
  });
  document.querySelector('#tokens details').open = true;
  var panel = document.getElementById('tokens').getBoundingClientRect();
  var tables = [].map.call(document.querySelectorAll('#tokens table'), function (t) {
    var box = t.parentElement, r = box.getBoundingClientRect();
    return [box.className, getComputedStyle(box).overflowX, t.getBoundingClientRect().height > 0,
            r.left >= panel.left - 0.5 && r.right <= panel.right + 0.5];
  });
  return {tiles: tiles, tables: tables, scroll: document.documentElement.scrollWidth, width: innerWidth};
})()"""


@needs_chrome
def test_cost_tiles_are_two_by_two_at_375_in_a_browser(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, telemetry=WIDE)
    browser = Browser()
    try:
        for script in (False, True):
            _load(browser, snapshot, 375, script)
            out = browser.eval(COST_TILES_AND_TABLES)
            assert [label for label, _, _ in out["tiles"]] == ["cost", "jobs", "tokens in", "tokens out"], script
            lefts = [round(left, 1) for _, left, _ in out["tiles"]]
            tops = [round(top, 1) for _, _, top in out["tiles"]]
            assert lefts[0] == lefts[2] < lefts[1] == lefts[3], (script, lefts)
            assert tops[0] == tops[1] < tops[2] == tops[3], (script, tops)
            assert out["tables"] == [["ef-scroll", "auto", True, True]] * 2, (script, out["tables"])
            assert out["scroll"] <= out["width"], (script, out)
    finally:
        browser.close()


TILE_VALUES = """(function () {
  return [].map.call(document.querySelectorAll('#tokens .stat'), function (tile) {
    tile.scrollIntoView({block: 'center'});
    var value = tile.querySelector('b'), range = document.createRange();
    range.selectNodeContents(value);
    var box = tile.getBoundingClientRect(), style = getComputedStyle(tile);
    var left = box.left + parseFloat(style.paddingLeft), right = box.right - parseFloat(style.paddingRight);
    var rects = [].slice.call(range.getClientRects());
    var lines = rects.map(function (r) { return Math.round(r.top); })
      .filter(function (top, i, all) { return all.indexOf(top) === i; }).length;
    var spill = rects.filter(function (r) { return r.left < left - 0.5 || r.right > right + 0.5; }).length;
    var end = rects[rects.length - 1], probe = document.elementFromPoint(end.right - 1, (end.top + end.bottom) / 2);
    return [tile.querySelector('span').textContent, value.textContent, spill, lines, value.contains(probe),
            parseFloat(getComputedStyle(value).fontSize)];
  });
})()"""
BIG_COST = tuple({**row, "cost_usd": 617_283.9 if row["cost_usd"] else None} for row in WIDE)


@needs_chrome
@pytest.mark.parametrize("width", [320, 375, 699, 700, 1440])
def test_cost_tile_values_stay_whole_inside_their_tiles_in_a_browser(tmp_path: Path, width: int) -> None:
    (tmp_path / "big").mkdir()
    cases = {"$24,691.35": _snapshot(tmp_path, telemetry=WIDE),
             "$1,234,567.80": _snapshot(tmp_path / "big", telemetry=BIG_COST)}
    browser = Browser()
    try:
        for cost, snapshot in cases.items():
            for script in (False, True):
                _load(browser, snapshot, width, script)
                tiles = browser.eval(TILE_VALUES)
                where = (cost, script, tiles)
                assert [(label, value) for label, value, *_ in tiles][:2] == [("cost", cost), ("jobs", "5")], where
                for label, value, spill, lines, visible, size in tiles:
                    assert spill == 0 and visible, (label, where)
                    assert size >= 14, (label, where)
                    if cost == "$24,691.35":
                        assert lines == 1, (label, where)
    finally:
        browser.close()


TOPS ="""(function () {
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
            assert browser.eval(HEADROOM_VIEWS) == {
                "table": "block", "blocks": [False, False], "notes": ["block", "none"]}, script
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
