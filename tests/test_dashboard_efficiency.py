"""The Efficiency page (#144, SPEC 3.5, Efficiency.dc.html): handoff savings
waffle, jobs lifetime, revise rounds before ship, who is working now,
subscription headroom, then #100's tokens and cost. Each panel's server render
and its page-script twin produce the same markup."""

import html
import re
import sqlite3
from pathlib import Path

import pytest

import lib.dashboard as dashboard
from lib import usage_readings
from lib.dashboard import collect_snapshot, render_pages
from lib.dashboard_pages import efficiency
from lib.usage_readings import Reading
from tests.test_dashboard import NOW, add_ticket, make_db, seeded
from tests.test_dashboard_layout import _run_page, _section, _text, needs_node

PANELS = ["savings", "jobs-lifetime", "rounds", "working", "headroom", "tokens", "health"]


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


def _snapshot(tmp_path: Path, *, readings: bool = True) -> dict:
    db, log = seeded(tmp_path)
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
    assert rows["codex-bot box"] == ["10% used", "stale", "in 1h 00m", "90%"]
    assert rows["claude-bot pc"] == ["no reading", "no reading", "—", "no reading yet"]
    assert rows["codex-bot pc"] == ["stale", "stale", "—", "reading 3h 00m old"]
    assert rows["cursor-bot"] == ["not tracked"] * 4
    assert rows["grok"] == ["not tracked"] * 4
    for name in ("claude-bot pc", "codex-bot pc", "cursor-bot", "grok"):
        assert not any(re.search(r"\d+%", cell) for cell in rows[name]), name


def test_headroom_colours_follow_the_page_thresholds(tmp_path: Path) -> None:
    headroom = _inner(_page(_snapshot(tmp_path)), "headroom")
    assert '<td class="ef-v ok">58%</td>' in headroom
    assert '<td class="ef-v ok">90%</td>' in headroom


def test_headroom_without_the_table_says_no_telemetry(tmp_path: Path) -> None:
    rows = _rows(_inner(_page(_snapshot(tmp_path, readings=False)), "headroom"))
    for name in ("claude-bot box", "codex-bot box", "claude-bot pc", "codex-bot pc"):
        assert rows[name] == ["no telemetry"] * 4, name
    assert rows["cursor-bot"] == ["not tracked"] * 4


def test_jobs_lifetime_counts_done_and_failed(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["queue"]["totals"].update(done=174, failed=74)
    panel = _inner(_page(snapshot), "jobs-lifetime")
    assert '<span class="count">248 finished</span>' in panel
    assert '<div class="ef-tile"><b class="ok">174</b><span>done</span></div>' in panel
    assert '<div class="ef-tile"><b class="bad">74</b><span>failed</span></div>' in panel
    assert (
        '<div class="ef-bar" role="img" aria-label="174 jobs done, 74 jobs failed">'
        '<span class="ok" style="flex:0 0 70.2%"></span><span class="bad" style="flex:0 0 29.8%"></span></div>'
    ) in panel
    assert "70% of 248 finished jobs succeeded" in _text(panel)


def test_jobs_lifetime_with_nothing_finished(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["queue"]["totals"].update(done=0, failed=0)
    panel = _inner(_page(snapshot), "jobs-lifetime")
    assert '<div class="ef-bar" role="img" aria-label="0 jobs done, 0 jobs failed"></div>' in panel
    assert "no finished jobs yet" in _text(panel)


def test_revise_rounds_group_shipped_tickets(tmp_path: Path) -> None:
    panel = _inner(_page(_snapshot(tmp_path)), "rounds")
    assert '<span class="count">4 shipped tickets</span>' in panel
    groups = re.findall(r'<div class="ef-group">(.*?)</div>', panel)
    assert [_text(g) for g in groups] == [
        "0 rounds 2 cleffy#96 · factory#65",
        "1 round 1 factory#77",
        "2 rounds 1 factory#98",
    ]
    assert groups[0].count("<span></span>") == 2 and 'class="ef-sq zero"' in groups[0]
    assert "factory#99" not in panel
    assert "2 of 4 shipped with no revise round" in _text(panel)


def test_revise_rounds_with_no_shipped_ticket(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["tickets"] = [t for t in snapshot["tickets"] if t["state"] == "building"]
    assert "no shipped tickets yet" in _text(_inner(_page(snapshot), "rounds"))


def test_who_is_working_has_a_cell_per_slot_and_a_meter_per_device(tmp_path: Path) -> None:
    panel = _inner(_page(_snapshot(tmp_path)), "working")
    cells = re.search(r'<div class="ef-cells" role="img" aria-label="([^"]*)">(.*?)</div>', panel)
    assert cells, panel
    total = cells.group(2).count("<span")
    assert total == 5
    assert cells.group(1).startswith("5 slots: ")
    assert "grok" in _text(panel)
    meters = re.findall(r'<canvas class="px" data-px="meter"[^>]*data-used="(\d+)" data-slots="(\d+)"', panel)
    assert meters == [("2", "3"), ("0", "2")]
    assert "2 / 3 slots" in _text(panel) and "0 / 2 slots" in _text(panel)
    assert "2 of 5 slots used" in _text(panel)


@needs_node
@pytest.mark.parametrize("readings", [True, False])
def test_script_twins_match_the_server_render(tmp_path: Path, readings: bool) -> None:
    snapshot = _snapshot(tmp_path, readings=readings)
    page = _page(snapshot)
    twins = PANELS[:-1]
    out = _run_page(tmp_path, snapshot, "efficiency", twins)
    for ident in twins:
        assert out["html"][ident] == _inner(page, ident), ident


def test_efficiency_parts_carry_no_motion_of_their_own() -> None:
    assert "@keyframes" not in efficiency.CSS and "animation" not in efficiency.CSS
    assert "transition" not in efficiency.CSS


def test_panels_are_in_the_artboard_order(tmp_path: Path) -> None:
    page = _page(_snapshot(tmp_path))
    found = [page.index(f'id="{ident}"') for ident in PANELS]
    assert found == sorted(found)


def test_an_empty_factory_renders_every_panel(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    snapshot = collect_snapshot(db, tmp_path / "poll.log", now=NOW)
    page = _page(snapshot)
    for ident in PANELS:
        assert f'id="{ident}"' in page, ident
    assert "no shipped tickets yet" in page and "no finished jobs yet" in page
