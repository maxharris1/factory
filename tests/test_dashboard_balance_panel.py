import re
from pathlib import Path

import pytest

from lib.dashboard import render_pages
from lib.dashboard_pages import efficiency
from tests.test_dashboard_efficiency import _inner, _snapshot
from tests.test_dashboard_layout import _run_page, _section, _text, needs_node

BOTH = ["server", pytest.param("script", marks=needs_node)]
HEAD = ('<h2><span>Account balance</span>'
        '<span class="count">recent placements per harness pool</span></h2>')
NOTE = ("Share is this account's part of its pool's recent placements. Skew flags a share more than "
        "20 points off an even split once the pool has 4 or more placements.")


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return _snapshot(tmp_path_factory.mktemp("site"))


def _window(used: float | None, state: str = "ok") -> dict:
    return {"used": used, "resets_at": None, "observed_at": None, "state": state}


def _entry(base: dict, account: str, harness: str, placements: int, share: float | None, skew: str, *,
           five: dict | None = None, seven: dict | None = None, label: str | None = None) -> dict:
    template = next(e for e in base["usage"]["accounts"] if e["state"] == "ok")
    return {**template, "account": account, "label": label, "harness": harness,
            "five_hour": five or _window(0.42), "seven_day": seven or _window(0.6),
            "recent_placements": placements, "placement_share": share, "skew": skew}


def _pools(base: dict) -> list[dict]:
    return [
        _entry(base, "claude-a", "claude", 6, 0.75, "high"),
        _entry(base, "codex-a", "codex", 1234, 0.5, "balanced", seven=_window(0.05)),
        _entry(base, "claude-b", "claude", 2, 0.25, "low", five=_window(None, "stale")),
        _entry(base, "codex-b", "codex", 1234, 0.5, "balanced", seven=_window(None, "not on plan")),
        _entry(base, "gemini-solo", "gemini", 5, 1.0, "none"),
        _entry(base, "grok-a", "grok", 0, None, "none", five=_window(None, "no reading")),
        _entry(base, "grok-b", "grok", 0, None, "none", label="Grok B"),
    ]


def _with(base: dict, accounts: list[dict]) -> dict:
    return {**base, "usage": {**base["usage"], "accounts": accounts}}


def _panel(tmp_path: Path, snapshot: dict, render: str) -> str:
    if render == "server":
        return _inner(render_pages(snapshot)["efficiency.html"], "balance")
    return _run_page(tmp_path, snapshot, "efficiency", ["balance"])["html"]["balance"]


def _pool_heads(panel: str) -> list[str]:
    return re.findall(r'<tr class="bl-pool"><th colspan="6" scope="rowgroup">(.*?)</th></tr>', panel)


def _row(panel: str, name: str) -> list[str]:
    match = re.search(rf'<tr><th scope="row">(<span class="bot [^"]*">[^<]*</span>)'
                      rf'<span class="bl-name">{re.escape(name)}</span></th>(.*?)</tr>', panel)
    assert match, name
    return re.findall(r"<td[^>]*>.*?</td>", match.group(2))


def _names(panel: str) -> list[str]:
    return re.findall(r'<span class="bl-name">(.*?)</span>', panel)


@needs_node
@pytest.mark.parametrize("accounts", ["pools", "empty"])
def test_server_and_script_draw_the_same_panel(tmp_path: Path, base: dict, accounts: str) -> None:
    snapshot = _with(base, _pools(base) if accounts == "pools" else [])
    assert _panel(tmp_path, snapshot, "script") == _panel(tmp_path, snapshot, "server")


@needs_node
def test_server_and_script_draw_the_same_panel_from_real_usage(tmp_path: Path, base: dict) -> None:
    assert _panel(tmp_path, base, "script") == _panel(tmp_path, base, "server")


@pytest.mark.parametrize("render", BOTH)
def test_panel_heads_with_its_title_and_ends_with_the_note(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _pools(base)), render)
    assert panel.startswith(HEAD + '<div class="bl-scroll"><table class="bl-table"><thead><tr>')
    assert re.findall(r"<thead><tr>(.*?)</tr></thead>", panel) == [
        "<th>account</th><th>5-hour headroom</th><th>7-day headroom</th>"
        "<th>recent placements</th><th>share</th><th>skew</th>"]
    assert panel.endswith(f'</table></div><p class="bl-note">{NOTE.replace(chr(39), "&#x27;")}</p>')


@pytest.mark.parametrize("render", BOTH)
def test_accounts_group_by_pool_in_order_of_first_appearance(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _pools(base)), render)
    assert _pool_heads(panel) == [
        "claude · 2 accounts", "codex · 2 accounts", "gemini · 1 account", "grok · 2 accounts"]
    assert _names(panel) == ["claude-a", "claude-b", "codex-a", "codex-b", "gemini-solo", "grok-a", "Grok B"]
    assert panel.count("<tbody>") == 4
    assert '<span class="bot codex">codex</span>' in re.search(r'<tr><th scope="row">(.*?)</th>',
                                                               panel[panel.index("codex · 2"):]).group(1)


@pytest.mark.parametrize("render", BOTH)
def test_high_and_low_skew_carry_a_warning_chip(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _pools(base)), render)
    assert _row(panel, "claude-a")[2:] == [
        '<td class="bl-v">6</td>', '<td class="bl-v">75%</td>',
        '<td class="bl-v"><span class="chip warn">skew high</span></td>']
    assert _row(panel, "claude-b")[2:] == [
        '<td class="bl-v">2</td>', '<td class="bl-v">25%</td>',
        '<td class="bl-v"><span class="chip warn">skew low</span></td>']


@pytest.mark.parametrize("render", BOTH)
def test_balanced_and_no_skew_carry_no_chip(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _pools(base)), render)
    assert _row(panel, "codex-a")[2:] == [
        '<td class="bl-v">1,234</td>', '<td class="bl-v">50%</td>',
        '<td class="bl-v"><span class="muted">balanced</span></td>']
    assert _row(panel, "grok-a")[2:] == [
        '<td class="bl-v">0</td>', '<td class="bl-v">—</td>', '<td class="bl-v"><span class="muted">—</span></td>']
    assert panel.count("chip") == 2


@pytest.mark.parametrize("render", BOTH)
def test_a_single_account_pool_holds_the_whole_share(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, [_entry(base, "solo", "claude", 7, 1.0, "none")]), render)
    assert _pool_heads(panel) == ["claude · 1 account"]
    assert _row(panel, "solo") == [
        '<td class="bl-v">58%</td>', '<td class="bl-v">40%</td>', '<td class="bl-v">7</td>',
        '<td class="bl-v">100%</td>', '<td class="bl-v"><span class="muted">—</span></td>']


@pytest.mark.parametrize("render", BOTH)
def test_headroom_is_what_each_window_has_left(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _pools(base)), render)
    assert _row(panel, "claude-a")[:2] == ['<td class="bl-v">58%</td>', '<td class="bl-v">40%</td>']
    assert _row(panel, "codex-a")[:2] == ['<td class="bl-v">58%</td>', '<td class="bl-v">95%</td>']


@pytest.mark.parametrize("render", BOTH)
def test_a_window_with_no_fresh_reading_shows_its_state_muted(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _pools(base)), render)
    assert _row(panel, "claude-b")[0] == '<td class="bl-v muted">stale</td>'
    assert _row(panel, "codex-b")[1] == '<td class="bl-v muted">not on plan</td>'
    assert _row(panel, "grok-a")[0] == '<td class="bl-v muted">no reading</td>'


@pytest.mark.parametrize("render", BOTH)
def test_no_accounts_says_so(tmp_path: Path, base: dict, render: str) -> None:
    assert _panel(tmp_path, _with(base, []), render) == HEAD + '<p class="empty">no accounts</p>'


@pytest.mark.parametrize("render", BOTH)
def test_an_account_label_is_escaped(tmp_path: Path, base: dict, render: str) -> None:
    hostile = _entry(base, "evil", "claude", 1, 1.0, "none", label="<script>x()</script>")
    panel = _panel(tmp_path, _with(base, [hostile]), render)
    assert '<span class="bl-name">&lt;script&gt;x()&lt;/script&gt;</span>' in panel
    assert "<script>" not in panel


def test_real_usage_lists_every_account_with_its_headroom(base: dict) -> None:
    panel = _inner(render_pages(base)["efficiency.html"], "balance")
    assert _pool_heads(panel) == ["claude · 1 account", "codex · 1 account", "cursor · 1 account"]
    assert _row(panel, "claude-default")[:2] == ['<td class="bl-v">59%</td>', '<td class="bl-v">74%</td>']
    assert _text(_row(panel, "cursor-default")[0]) == "no reading"


def test_balance_sits_alone_in_its_row(base: dict) -> None:
    page = render_pages(base)["efficiency.html"]
    panel = _section(page, "balance")
    assert f'<div class="ef-row">{panel}</div>' in page


def test_balance_css_keeps_square_corners() -> None:
    rules = re.findall(r"[^{}]*\.bl-[^{}]*\{[^}]*\}", efficiency.CSS)
    assert rules
    assert not [rule for rule in rules if "radius" in rule]
