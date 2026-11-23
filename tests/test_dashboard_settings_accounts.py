from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot, render_pages
from tests import test_dashboard_layout as layout
from tests import test_dashboard_settings as test_settings
from tests.test_dashboard_accounts import NOW, world
from tests.test_dashboard_layout import BAR, HARNESS, _no_script, _section, _text, needs_node

CHECKED = "2027-01-15T07:55:00Z"
CLAUDE_B_PC = "env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=/home/claude-bot/.factory-accounts/claude-b claude auth login"
CLAUDE_B_MAC = "env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=/Users/claude-bot/.factory-accounts/claude-b claude auth login"
DEFAULT_MAC = "env -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDE_CONFIG_DIR claude auth login"
CODEX_PC = "env -u CODEX_HOME codex login --device-auth"
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


SIGNOUTS = [
    _change(10, "claude-b", "signout", "ok", "signed-out", device="pc", result="signed-out"),
    _change(11, "claude-b", "signout", "ok", "ok", device="pc", result="failed", error="logout-exit"),
    _change(12, "claude-b", "signout", "ok", None, device="mac", result="refused", error="running-job"),
]


def _history(card: str) -> list[tuple[str, str, str]]:
    return re.findall(r'<li class="st-res"><span class="chip ?([^"]*)">([^<]*)</span>'
                      r'<span class="st-what">([^<]*)</span>', card)


def test_the_card_history_shows_each_signout_result(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, [*AUDIT, *SIGNOUTS])
    assert [row["result"] for row in snapshot["settings"]["results"][-3:]] == ["signed-out", "failed", "refused"]
    card = _card(_panel(snapshot), "claude-b")
    assert _history(card)[:3] == [
        ("warn", "refused", "signout on mac ok → — · maxharris1 · 2026-11-17T10:12:30Z"),
        ("bad", "failed", "signout on pc ok → ok · maxharris1 · 2026-11-17T10:11:30Z"),
        ("muted", "signed-out", "signout on pc ok → signed-out · maxharris1 · 2026-11-17T10:10:30Z"),
    ]
    assert re.findall(r'<span class="st-err">([^<]*)</span>', card)[:2] == ["running-job", "logout-exit"]


@needs_node
def test_script_draws_the_signout_history_like_the_server(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, [*AUDIT, *SIGNOUTS])
    out = _run(tmp_path / "run", snapshot)
    server = _panel(snapshot)
    assert out["html"]["accounts"] == server[server.index(">") + 1:server.rindex("<")]


def test_audit_results_carry_every_signin_and_signout_result() -> None:
    from lib.dashboard import AUDIT_RESULTS

    assert set(AUDIT_RESULTS) >= {"ok", "signed-out", "mismatch", "expired", "cancelled", "failed", "refused",
                                  "applied", "pending"}


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


SIGNED_IN = {"signed_in": True, "login": "maxharris1", "csrf": "csrf-value"}
QUEUED = "queued (applies within ~2 min)"
SET_ACTION = (
    "    } else if (kind === 'set') {\n"
    "      const node = pick(arg.pick);\n"
    "      if (!node) throw new Error('nothing to set: ' + JSON.stringify(arg));\n"
    "      Object.assign(node, arg.props);\n"
    "      fire(node, 'change');\n"
    "      steps.push({set: arg});\n"
)


SCOPED_PICK = ("function pick(spec) {\n  return find(body,", "function pick(spec) {\n  return find(spec.within ? pick(spec.within) || new Node('none') : body,")


def _session_harness(session: dict, reply: dict | None = None) -> str:
    harness = test_settings._harness(session, reply)
    marker = "    } else if (kind === 'key') {\n"
    assert marker in harness and SCOPED_PICK[0] in harness
    return harness.replace(marker, SET_ACTION + marker).replace(*SCOPED_PICK)


def _signed_in(tmp_path: Path, actions: list | None = None, reply: dict | None = None,
               snapshot: dict | None = None) -> dict:
    snapshot = snapshot or _snapshot(tmp_path)
    html = render_pages(snapshot)["settings.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    return layout.run_harness(tmp_path / "run", _session_harness(SIGNED_IN, reply), script, snapshot, "settings",
                              BAR + ["settings", "accounts"], actions or [])


def _posts(out: dict) -> list[dict]:
    return [step["body"] for step in out["steps"] if "post" in step]


def _add_form(markup: str) -> str:
    start = markup.index('data-form="add"')
    return markup[start:markup.index('role="status"', start)]


def _boxes(markup: str) -> list[str]:
    return re.findall(r'type="checkbox" data-device="([^"]+)"', markup)


ADD = {"attr": {"data-form": "add"}}
CLAUDE_B = {"attr": {"data-account": "claude-b"}}


def _click(within: dict, op: str, **attr: str) -> list:
    return ["click", {"within": within, "attr": {"data-op": op, **attr}}]


def _set(within: dict, props: dict, **attr: str) -> list:
    return ["set", {"pick": {"within": within, "attr": attr}, "props": props}]


@needs_node
def test_signed_out_the_accounts_panel_has_no_controls(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    html = render_pages(snapshot)["settings.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    out = layout.run_harness(tmp_path / "run", _session_harness({"signed_in": False}), script, snapshot,
                             "settings", BAR + ["settings", "accounts"], [])
    accounts = out["html"]["accounts"]
    assert "Sign in with GitHub" in out["text"]["settings"]
    for needle in ("st-ctl", "<button", "<input", "<select", "data-op"):
        assert needle not in accounts
    server = _panel(snapshot)
    assert accounts == server[server.index(">") + 1:server.rindex("<")]


@needs_node
def test_signed_in_the_panel_has_the_add_form_and_each_editable_card_its_controls(tmp_path: Path) -> None:
    accounts = _signed_in(tmp_path)["html"]["accounts"]
    form = _add_form(accounts)
    assert 'data-field="id"' in form
    assert re.findall(r"<option[^>]*>([^<]*)</option>", form) == ["claude", "codex", "cursor"]
    assert 'type="number" min="1" max="64" data-field="max_jobs"' in form
    assert _boxes(form) == ["box", "pc", "mac"]
    assert ">Add account</button>" in form
    claude_b = _card(accounts, "claude-b")
    assert 'type="number" min="1" max="64" data-field="max_jobs"' in claude_b
    assert 'data-op="max_jobs"' in claude_b and 'data-op="signed_in_on"' in claude_b
    assert _boxes(claude_b) == ["box", "pc", "mac"]
    assert 'data-op="paused" aria-pressed="true">Resume</button>' in claude_b
    assert 'data-op="remove"' in claude_b
    assert re.findall(r'data-op="prepare" data-pair="([^"]+)"', claude_b) == ["pc", "mac"]
    assert re.findall(r'data-op="doctor" data-pair="([^"]+)"', claude_b) == ["pc", "mac"]
    assert re.findall(r'data-op="signout" data-pair="([^"]+)">Sign out</button>', claude_b) == ["pc", "mac"]
    for read_only in ("claude-default", "codex-default", "ext-grok"):
        assert "st-ctl" not in _card(accounts, read_only)


@needs_node
def test_the_add_form_lists_only_devices_that_run_the_chosen_harness(tmp_path: Path) -> None:
    out = _signed_in(tmp_path, [_set(ADD, {"value": "codex"}, **{"data-field": "harness"})])
    assert _boxes(_add_form(out["html"]["accounts"])) == ["box", "pc"]
    out = _signed_in(tmp_path / "cursor", [_set(ADD, {"value": "cursor"}, **{"data-field": "harness"})])
    assert _boxes(_add_form(out["html"]["accounts"])) == []


ACCEPTED = {"status": 202, "body": {"id": "x" * 22}}
CONTROLS = {
    "add": (
        [_set(ADD, {"value": "claude-c"}, **{"data-field": "id"}),
         _set(ADD, {"value": "codex"}, **{"data-field": "harness"}),
         _set(ADD, {"value": "3"}, **{"data-field": "max_jobs"}),
         _set(ADD, {"checked": True}, **{"data-device": "pc"}),
         _click(ADD, "add")],
        {"target": "account", "op": "add", "account": "claude-c", "harness": "codex", "max_jobs": 3,
         "signed_in_on": ["pc"]},
        None,
    ),
    "max_jobs": (
        [_set(CLAUDE_B, {"value": "4"}, **{"data-field": "max_jobs"}), _click(CLAUDE_B, "max_jobs")],
        {"target": "account", "op": "set", "account": "claude-b", "field": "max_jobs", "expect": 2, "value": 4},
        "claude-b",
    ),
    "signed_in_on": (
        [_set(CLAUDE_B, {"checked": True}, **{"data-device": "box"}),
         _set(CLAUDE_B, {"checked": False}, **{"data-device": "mac"}),
         _click(CLAUDE_B, "signed_in_on")],
        {"target": "account", "op": "set", "account": "claude-b", "field": "signed_in_on",
         "expect": ["pc", "mac"], "value": ["box", "pc"]},
        "claude-b",
    ),
    "paused": (
        [_click(CLAUDE_B, "paused")],
        {"target": "account", "op": "set", "account": "claude-b", "field": "paused", "expect": True, "value": False},
        "claude-b",
    ),
    "remove": (
        [_click(CLAUDE_B, "remove")],
        {"target": "account", "op": "remove", "account": "claude-b",
         "expect": {"harness": "claude", "max_jobs": 2, "paused": True, "signed_in_on": ["pc", "mac"]}},
        "claude-b",
    ),
    "prepare": (
        [_click(CLAUDE_B, "prepare", **{"data-pair": "mac"})],
        {"target": "account", "op": "prepare", "account": "claude-b", "device": "mac"},
        "claude-b",
    ),
    "doctor": (
        [_click(CLAUDE_B, "doctor", **{"data-pair": "pc"})],
        {"target": "account", "op": "doctor", "account": "claude-b", "device": "pc"},
        "claude-b",
    ),
    "signout": (
        [_click(CLAUDE_B, "signout", **{"data-pair": "mac"})],
        {"target": "account", "op": "signout", "account": "claude-b", "device": "mac"},
        "claude-b",
    ),
}


def _where(accounts: str, card: str | None) -> str:
    return _card(accounts, card) if card else accounts[accounts.index('data-form="add"'):accounts.index("st-grid")]


@needs_node
@pytest.mark.parametrize("control", list(CONTROLS))
def test_each_control_posts_its_body_and_shows_queued_on_its_card(tmp_path: Path, control: str) -> None:
    actions, body, card = CONTROLS[control]
    out = _signed_in(tmp_path, actions, ACCEPTED)
    assert _posts(out) == [body]
    accounts = out["html"]["accounts"]
    assert f'<div class="st-out ok" role="status">{QUEUED}</div>' in _where(accounts, card)
    assert accounts.count(QUEUED) == 1


@needs_node
@pytest.mark.parametrize("control", list(CONTROLS))
@pytest.mark.parametrize("status,error", [(409, "changed"), (403, "read-only")])
def test_each_control_shows_the_error_on_its_card(tmp_path: Path, control: str, status: int, error: str) -> None:
    actions, body, card = CONTROLS[control]
    out = _signed_in(tmp_path, actions, {"status": status, "body": {"error": error}})
    assert _posts(out) == [body]
    accounts = out["html"]["accounts"]
    assert f'<div class="st-out bad" role="status">error: {error}</div>' in _where(accounts, card)
    assert accounts.count(f"error: {error}") == 1


@needs_node
@pytest.mark.parametrize("value", ["0", "65", "two", ""])
def test_a_bad_max_jobs_shows_bad_value_without_posting(tmp_path: Path, value: str) -> None:
    out = _signed_in(tmp_path, [_set(CLAUDE_B, {"value": value}, **{"data-field": "max_jobs"}),
                                _click(CLAUDE_B, "max_jobs")], ACCEPTED)
    assert _posts(out) == []
    assert '<div class="st-out bad" role="status">error: bad-value</div>' in _card(out["html"]["accounts"], "claude-b")


INHERITED = ["constructor", "__proto__", "toString", "hasOwnProperty"]


@needs_node
@pytest.mark.parametrize("name", INHERITED)
def test_an_account_named_like_an_object_property_gets_its_controls(tmp_path: Path, name: str) -> None:
    snapshot = _snapshot(tmp_path)
    next(a for a in snapshot["accounts"] if a["id"] == "claude-b")["id"] = name
    card = {"attr": {"data-account": name}}
    out = _signed_in(tmp_path, [_click(card, "paused")], ACCEPTED, snapshot)
    accounts = out["html"]["accounts"]
    ids = re.findall(r'<article class="ac-card" data-account="([^"]+)">', accounts)
    assert ids == ["claude-default", "codex-default", name, "ext-grok"]
    assert 'data-op="remove"' in _card(accounts, name)
    assert _posts(out) == [{"target": "account", "op": "set", "account": name, "field": "paused",
                            "expect": True, "value": False}]
    assert f'<div class="st-out ok" role="status">{QUEUED}</div>' in _card(accounts, name)
