import re
from pathlib import Path

import pytest

import lib.dashboard_page as page
from lib.dashboard import render_pages
from lib.dashboard_pages import efficiency, usage_chip
from lib.dashboard_static import read_asset
from tests.test_dashboard_efficiency import CLAUDE_ONLY, _accounts_snapshot, _usage_snapshot

OPEN = '<a class="uchip" id="usage-chip" href="efficiency.html#headroom"'
HIDDEN = f"{OPEN} hidden></a>"
CURSOR_ONLY = "devices:\n  - name: box\n    local: true\n    slot_max: 3\n    harnesses:\n      cursor: cursor-bot\n"
CODEX_A_SEGMENT = (
    '<span class="uc-seg bad"><span class="uc-name codex">codex</span>'
    '<span class="uc-bar" aria-hidden="true"><i style="width:10%"></i></span>'
    '<b>10%</b><span class="uc-reset">30m 00s</span></span>'
)
STALE_CLAUDE = '<span class="uc-seg stale"><span class="uc-name claude">claude</span><span class="uc-stale">stale</span></span>'
TITLE = (
    "Subscription headroom\n"
    "claude claude-default: 5-hour no reading, 7-day no reading\n"
    "codex codex-a: 5-hour 90% used, 7-day 30% used\n"
    "cursor cursor-default: not tracked\n"
    "codex codex-b: 5-hour 20% used, 7-day 15% used\n"
    "external Cursor Grok Ultra: 5-hour no reading, 7-day no reading"
)


def _entry(snapshot: dict, account: str) -> dict:
    [entry] = [e for e in snapshot["usage"]["accounts"] if e["account"] == account]
    return entry


def _segments(snapshot: dict) -> list[dict]:
    return usage_chip.chip_model(snapshot)["segments"]


def _attr(chip: str, name: str) -> str:
    return re.search(f' {name}="([^"]*)"', chip).group(1)


def _panel_tone(snapshot: dict) -> str:
    return re.search(r'<span class="ef-hn (\w+)">', efficiency._headroom(snapshot)).group(1)


def _go_stale(entry: dict) -> None:
    for window in efficiency.WINDOWS:
        entry[window]["state"] = "stale"
    entry["headroom"] = None


def test_the_chip_is_one_link_to_the_headroom_panel(tmp_path: Path) -> None:
    chip = usage_chip._usage_chip(_accounts_snapshot(tmp_path))
    assert chip.startswith(OPEN) and chip.endswith("</a>")
    assert chip.count("<a") == 1
    assert not [word for word in ("<button", "<input", "<select", "<form", "<details", "tabindex") if word in chip]


def test_no_tracked_account_draws_a_hidden_empty_link(tmp_path: Path) -> None:
    snapshot = _usage_snapshot(tmp_path / "untracked", CURSOR_ONLY)
    assert [e["state"] for e in snapshot["usage"]["accounts"]] == ["not tracked"]
    assert usage_chip._usage_chip(snapshot) == HIDDEN
    snapshot["usage"]["accounts"] = []
    assert usage_chip._usage_chip(snapshot) == HIDDEN


def test_no_page_draws_the_chip_yet(tmp_path: Path) -> None:
    pages = render_pages(_accounts_snapshot(tmp_path))
    assert not [name for name, html in pages.items() if "usage-chip" in html]


def test_the_css_is_served() -> None:
    assert usage_chip.CSS == read_asset("css/usage_chip.css")
    assert usage_chip.CSS in page.CSS


def test_one_segment_per_harness_with_a_tracked_account_in_accounts_order(tmp_path: Path) -> None:
    assert [s["harness"] for s in _segments(_accounts_snapshot(tmp_path))] == ["claude", "codex", "external"]


def test_a_segment_shows_the_name_bar_percent_and_reset(tmp_path: Path) -> None:
    chip = usage_chip._usage_chip(_accounts_snapshot(tmp_path))
    assert CODEX_A_SEGMENT in chip


@pytest.mark.parametrize(("headroom", "pct"), [(0.125, 13), (0.124, 12)])
def test_the_percent_rounds_half_up(tmp_path: Path, headroom: float, pct: int) -> None:
    snapshot = _usage_snapshot(tmp_path, five_hour=0.3, seven_day=0.1)
    snapshot["usage"]["accounts"][0]["headroom"] = headroom
    chip = usage_chip._usage_chip(snapshot)
    assert f'<i style="width:{pct}%"></i></span><b>{pct}%</b>' in chip


def test_the_reset_is_the_fullest_fresh_windows(tmp_path: Path) -> None:
    chip = usage_chip._usage_chip(_usage_snapshot(tmp_path, five_hour=0.3, seven_day=0.6))
    assert '<b>40%</b><span class="uc-reset">1d 0h</span></span>' in chip


def test_the_reset_is_left_out_when_that_window_has_none(tmp_path: Path) -> None:
    snapshot = _usage_snapshot(tmp_path, five_hour=0.3, seven_day=0.6)
    snapshot["usage"]["accounts"][0]["seven_day"]["resets_at"] = None
    chip = usage_chip._usage_chip(snapshot)
    assert "<b>40%</b></span>" in chip and "uc-reset" not in chip


def test_the_css_names_only_existing_colour_tokens() -> None:
    base = read_asset("css/common_base.css")
    used = set(re.findall(r"var\((--[\w-]+)\)", usage_chip.CSS))
    assert used and not [token for token in used if f"{token}:" not in base]
    for harness in ("claude", "codex", "cursor"):
        assert f".uc-name.{harness}{{color:var(--{harness})}}" in usage_chip.CSS
    assert not re.search(r"\.uc-name\.(?!claude|codex|cursor)\w", usage_chip.CSS)


def test_the_segment_shows_the_account_with_the_lowest_headroom(tmp_path: Path) -> None:
    snapshot = _accounts_snapshot(tmp_path)
    [codex] = [s for s in _segments(snapshot) if s["harness"] == "codex"]
    assert (codex["pct"], codex["resets"]) == (10, "30m 00s")
    _entry(snapshot, "codex-b")["headroom"] = 0.05
    [codex] = [s for s in _segments(snapshot) if s["harness"] == "codex"]
    assert (codex["pct"], codex["resets"]) == (5, "2h 30m")


def test_an_account_with_no_fresh_reading_is_skipped(tmp_path: Path) -> None:
    snapshot = _accounts_snapshot(tmp_path)
    _go_stale(_entry(snapshot, "codex-a"))
    [codex] = [s for s in _segments(snapshot) if s["harness"] == "codex"]
    assert (codex["pct"], codex["tone"], codex["resets"]) == (80, "ok", "2h 30m")


def test_a_harness_with_no_fresh_reading_reads_stale(tmp_path: Path) -> None:
    snapshot = _accounts_snapshot(tmp_path)
    for account in ("codex-a", "codex-b"):
        _go_stale(_entry(snapshot, account))
    chip = usage_chip._usage_chip(snapshot)
    assert STALE_CLAUDE in chip
    assert '<span class="uc-seg stale"><span class="uc-name codex">codex</span><span class="uc-stale">stale</span></span>' in chip
    body = chip[chip.index(">") + 1:]
    assert "uc-bar" not in body and "%" not in body and "uc-reset" not in body
    assert re.search(r"\.uc-seg\.stale \.uc-name[^{]*\{color:var\(--muted\)\}", usage_chip.CSS)
    assert re.search(r"\.uc-stale\{[^}]*color:var\(--muted\)", usage_chip.CSS)


@pytest.mark.parametrize(("five_hour", "tone"), [(0.5, "ok"), (0.51, "warn"), (0.8, "warn"), (0.81, "bad")])
def test_tones_are_the_headroom_panels(tmp_path: Path, five_hour: float, tone: str) -> None:
    snapshot = _usage_snapshot(tmp_path, CLAUDE_ONLY, five_hour=five_hour, seven_day=0.1)
    [segment] = _segments(snapshot)
    assert segment["tone"] == _panel_tone(snapshot) == tone


def test_one_fresh_window_of_two_is_neutral(tmp_path: Path) -> None:
    snapshot = _usage_snapshot(tmp_path, CLAUDE_ONLY, five_hour=0.41)
    [segment] = _segments(snapshot)
    assert segment["tone"] == _panel_tone(snapshot) == "neutral"


def test_a_lane_limit_is_bad(tmp_path: Path) -> None:
    snapshot = _usage_snapshot(tmp_path, CLAUDE_ONLY, five_hour=0.2, seven_day=0.1)
    snapshot["usage"]["accounts"][0]["limit_label"] = "usage limit until 2026-11-17 03:00 UTC"
    [segment] = _segments(snapshot)
    assert (segment["pct"], segment["tone"]) == (80, "bad")


def test_the_chip_follows_the_panels_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _usage_snapshot(tmp_path, CLAUDE_ONLY, five_hour=0.2, seven_day=0.1)
    monkeypatch.setattr(efficiency, "_tone", lambda room: "warn")
    [segment] = _segments(snapshot)
    assert segment["tone"] == _panel_tone(snapshot) == "warn"


def test_the_title_lists_every_account_with_both_windows(tmp_path: Path) -> None:
    snapshot = _accounts_snapshot(tmp_path)
    assert usage_chip.chip_model(snapshot)["title"] == TITLE
    assert _attr(usage_chip._usage_chip(snapshot), "title") == TITLE


def test_the_title_ends_a_limited_accounts_line_with_its_limit_label(tmp_path: Path) -> None:
    snapshot = _accounts_snapshot(tmp_path, limit="codex-a")
    [limited] = [e for e in snapshot["usage"]["accounts"] if e["limit_label"]]
    lines = usage_chip.chip_model(snapshot)["title"].split("\n")
    name = limited["label"] or limited["account"]
    [line] = [line for line in lines if line.startswith(f"{limited['harness']} {name}: ")]
    assert line.endswith(f" · {limited['limit_label']}")


def test_screen_readers_get_the_title_text(tmp_path: Path) -> None:
    chip = usage_chip._usage_chip(_accounts_snapshot(tmp_path))
    assert _attr(chip, "aria-label") == _attr(chip, "title")


def test_account_names_are_escaped(tmp_path: Path) -> None:
    snapshot = _accounts_snapshot(tmp_path)
    _entry(snapshot, "codex-a")["label"] = 'a<b"&c'
    chip = usage_chip._usage_chip(snapshot)
    for name in ("title", "aria-label"):
        assert "codex a&lt;b&quot;&amp;c: 5-hour" in _attr(chip, name)
