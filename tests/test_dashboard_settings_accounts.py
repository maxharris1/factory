from __future__ import annotations

import json
import re
from pathlib import Path

from lib.dashboard import collect_snapshot, render_pages
from tests import test_dashboard_layout as layout
from tests.test_dashboard_accounts import NOW, world
from tests.test_dashboard_layout import BAR, HARNESS, _no_script, _section, _text, needs_node

CHECKED = "2027-01-15T07:55:00Z"
CLAUDE_B_PC = "CLAUDE_CONFIG_DIR=/home/claude-bot/.factory-accounts/claude-b claude auth login"
CLAUDE_B_MAC = "CLAUDE_CONFIG_DIR=/Users/claude-bot/.factory-accounts/claude-b claude auth login"
DEFAULT_MAC = "env -u CLAUDE_CONFIG_DIR claude auth login"
CODEX_PC = "env -u CODEX_HOME codex login"
NOT_DOCUMENTED = ("--with-api-key", "--with-access-token", "setup-token")


def _change(n: int, account: str, field: str, previous: object, new: object, **extra: object) -> dict:
    return {"id": f"{n:016x}", "actor": "maxharris1", "requested_at": f"2026-11-17T10:{n:02d}:00Z",
            "applied_at": f"2026-11-17T10:{n:02d}:30Z", "device": None, "field": field, "previous": previous,
            "new": new, "result": "refused", "error": None, "backup": None, "restarted": [], "pending": [],
            "target": "account", "account": account, **extra}


AUDIT = [
    *[_change(n, "claude-b", "max_jobs", n, n + 1) for n in range(1, 8)],
    _change(8, "codex-default", "paused", False, True),
    {**_change(9, "claude-b", "slot_max", 1, 2), "target": None, "account": None, "device": "pc"},
]


def _snapshot(tmp_path: Path, audit: list | None = AUDIT) -> dict:
    db, machines = world(tmp_path)
    if audit is not None:
        (tmp_path / "settings-audit.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in audit), encoding="utf-8")
    return collect_snapshot(db, tmp_path / "poll.log", now=NOW, machines_path=machines)


def _page(snapshot: dict) -> str:
    return _no_script(render_pages(snapshot)["settings.html"])


def _panel(snapshot: dict) -> str:
    return _section(_page(snapshot), "accounts")


def _card(markup: str, account: str) -> str:
    start = markup.index(f'<article class="ac-card" data-account="{account}">')
    return markup[start:markup.index("</article>", start)]


def _device(card: str, device: str) -> str:
    start = card.index(f'<li class="ac-dev" data-device="{device}">')
    return card[start:card.index("</li>", start)]


def _facts(card: str) -> dict[str, str]:
    return {label: _text(value) for label, value in
            re.findall(r"<dt>([^<]*)</dt><dd[^>]*>(.*?)</dd>", card, flags=re.S)}


def test_the_accounts_panel_sits_under_devices(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    page = _page(snapshot)
    assert page.index('id="settings"') < page.index('id="accounts"')
    head = _panel(snapshot)
    assert re.search(r"<h2><span>Accounts</span><span class=\"count\">4 accounts</span></h2>", head)


def test_each_account_gets_a_card_with_its_settings(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    ids = re.findall(r'<article class="ac-card" data-account="([^"]+)">', panel)
    assert ids == ["claude-default", "codex-default", "claude-b", "ext-grok"]
    facts = {account: _facts(_card(panel, account)) for account in ids}
    assert {a: (f["harness"], f["max_jobs"], f["paused"]) for a, f in facts.items()} == {
        "claude-default": ("claude", "—", "no"),
        "codex-default": ("codex", "—", "no"),
        "claude-b": ("claude", "2", "yes"),
        "ext-grok": ("external", "—", "no"),
    }
    assert 'class="st-name">claude-b</span>' in _card(panel, "claude-b")


def test_default_and_external_accounts_are_drawn_read_only(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    assert "read-only: default account" in _text(_card(panel, "claude-default"))
    assert "read-only: default account" in _text(_card(panel, "codex-default"))
    assert "read-only: external account" in _text(_card(panel, "ext-grok"))
    assert "read-only" not in _card(panel, "claude-b")
    assert "<button" not in panel and "<input" not in panel


def test_one_row_per_signed_in_device_with_the_doctor_status(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    default, claude_b, codex = (_card(panel, a) for a in ("claude-default", "claude-b", "codex-default"))
    assert re.findall(r'<li class="ac-dev" data-device="([^"]+)">', default) == ["box", "pc", "mac"]
    assert re.findall(r'<li class="ac-dev" data-device="([^"]+)">', claude_b) == ["pc", "mac"]
    assert re.findall(r'<li class="ac-dev" data-device="([^"]+)">', codex) == ["box", "pc"]
    assert _text(_device(default, "box")) == f"box ok claude.ai max checked {CHECKED}"
    assert _text(_device(codex, "box")) == f"box ok chatgpt — checked {CHECKED}"
    assert '<span class="chip ok">ok</span>' in _device(default, "box")
    assert '<span class="chip warn">mismatch</span>' in _device(claude_b, "pc")
    assert '<span class="chip bad">signed-out</span>' in _device(claude_b, "mac")
    assert '<span class="chip warn">unreachable</span>' in _device(default, "mac")
    assert '<span class="chip muted">unchecked</span>' in _device(codex, "pc")
    assert "not signed in on any device" in _text(_card(panel, "ext-grok"))


def test_a_mismatch_names_the_account_with_the_same_login(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    assert "same login as claude-default" in _text(_device(_card(panel, "claude-b"), "pc"))
    assert "same login as" not in _card(panel, "claude-default")


def test_used_and_headroom_show_with_the_floor_marked(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    default, claude_b = _facts(_card(panel, "claude-default")), _facts(_card(panel, "claude-b"))
    assert (default["5h used"], default["7d used"], default["headroom"]) == ("25%", "60%", "40% · floor 5%")
    assert (claude_b["5h used"], claude_b["7d used"], claude_b["headroom"]) == ("—", "—", "— · floor 5%")


def test_headroom_under_the_floor_reads_below_floor(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    account = next(a for a in snapshot["accounts"] if a["id"] == "claude-default")
    account["seven_day_used"], account["headroom"] = 0.97, 0.03
    card = _card(_panel(snapshot), "claude-default")
    assert _facts(card)["headroom"] == "3% · below floor 5%"
    assert '<dd class="bad">' in card


def test_each_card_lists_its_running_jobs(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    assert _facts(_card(panel, "claude-default"))["running"] == "42 fix on box"
    assert _facts(_card(panel, "claude-b"))["running"] == "41 build on pc"
    assert _facts(_card(panel, "codex-default"))["running"] == "43 review on pc"
    assert _facts(_card(panel, "ext-grok"))["running"] == "none"


def test_each_card_shows_its_last_five_account_results_newest_first(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    claude_b = _card(panel, "claude-b")
    lines = re.findall(r'<span class="st-what">([^<]*)</span>', claude_b)
    assert lines == [f"max_jobs {n} → {n + 1} · maxharris1 · 2026-11-17T10:{n:02d}:30Z" for n in (7, 6, 5, 4, 3)]
    assert "slot_max" not in claude_b
    assert re.findall(r'<span class="st-what">([^<]*)</span>', _card(panel, "codex-default")) == [
        "paused false → true · maxharris1 · 2026-11-17T10:08:30Z"]
    assert '<p class="empty">no changes yet</p>' in _card(panel, "claude-default")


def test_each_pair_that_is_not_ok_shows_its_documented_sign_in_command(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    commands = {
        (account, device): re.findall(r'<code class="ac-cmd">([^<]*)</code>', _device(_card(panel, account), device))
        for account, device in (("claude-default", "box"), ("claude-default", "pc"), ("claude-default", "mac"),
                                ("claude-b", "pc"), ("claude-b", "mac"), ("codex-default", "box"),
                                ("codex-default", "pc"))
    }
    assert commands == {
        ("claude-default", "box"): [],
        ("claude-default", "pc"): [],
        ("claude-default", "mac"): [DEFAULT_MAC],
        ("claude-b", "pc"): [CLAUDE_B_PC],
        ("claude-b", "mac"): [CLAUDE_B_MAC],
        ("codex-default", "box"): [],
        ("codex-default", "pc"): [CODEX_PC],
    }
    assert "sign in as claude-bot on mac: " + CLAUDE_B_MAC in _text(_device(_card(panel, "claude-b"), "mac"))
    assert "sign in as codex-bot on pc: " + CODEX_PC in _text(_device(_card(panel, "codex-default"), "pc"))
    for flag in NOT_DOCUMENTED:
        assert flag not in panel


def test_no_accounts_reads_empty(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["accounts"] = []
    panel = _panel(snapshot)
    assert "4 accounts" not in panel and "0 accounts" in panel
    assert '<p class="empty">no accounts: machines.yml lists none</p>' in panel


def _run(tmp_path: Path, snapshot: dict) -> dict:
    html = render_pages(snapshot)["settings.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    return layout.run_harness(tmp_path, HARNESS, script, snapshot, "settings", BAR + ["settings", "accounts"], [])


@needs_node
def test_script_draws_the_accounts_like_the_server(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    account = next(a for a in snapshot["accounts"] if a["id"] == "claude-default")
    account["seven_day_used"], account["headroom"] = 0.97, 0.03
    out = _run(tmp_path / "run", snapshot)
    server = _panel(snapshot)
    assert out["html"]["accounts"] == server[server.index(">") + 1:server.rindex("<")]
    for flag in NOT_DOCUMENTED:
        assert flag not in out["html"]["accounts"]


@needs_node
def test_script_draws_no_accounts_like_the_server(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, None)
    snapshot["accounts"] = []
    out = _run(tmp_path / "run", snapshot)
    server = _panel(snapshot)
    assert out["html"]["accounts"] == server[server.index(">") + 1:server.rindex("<")]
