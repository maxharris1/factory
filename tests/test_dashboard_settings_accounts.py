from __future__ import annotations

import contextlib
import html
import json
import re
import sqlite3
from pathlib import Path

import pytest

from lib import account_doctor, usage_readings
from lib.account_requests import ADD_REFUSED
from lib.dashboard import collect_snapshot, render_pages
from tests import test_dashboard_layout as layout
from tests import test_dashboard_settings as test_settings
from tests.test_dashboard_accounts import (
    CURSOR_REASON,
    MACHINES,
    NOW,
    SIGN_INS,
    cursor_snapshot,
    recorded_cursor_snapshot,
    sign_in,
    stale_world,
    world,
)
from tests.test_remote_start import config_of
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


def _snapshot(tmp_path: Path, audit: list | None = AUDIT, make=world) -> dict:
    db, machines = make(tmp_path)
    if audit is not None:
        (tmp_path / "settings-audit.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in audit), encoding="utf-8")
    return collect_snapshot(db, tmp_path / "poll.log", now=NOW, machines_path=machines)


def _page(snapshot: dict) -> str:
    return _no_script(render_pages(snapshot)["health.html"])


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
    assert _text(_device(default, "box")) == f"box ok claude.ai max checked {CHECKED} read 1m 00s ago"
    assert _text(_device(codex, "box")) == f"box ok chatgpt — checked {CHECKED} no reading"
    assert '<span class="chip ok">ok</span>' in _device(default, "box")
    assert '<span class="chip warn">mismatch</span>' in _device(claude_b, "pc")
    assert '<span class="chip bad">signed-out</span>' in _device(claude_b, "mac")
    assert '<span class="chip warn">unreachable</span>' in _device(default, "mac")
    assert '<span class="chip muted">unchecked</span>' in _device(codex, "pc")
    assert "not signed in on any device" in _text(_card(panel, "ext-grok"))


def test_a_device_row_shows_reading_age_or_no_reading_or_stale(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path, make=stale_world))
    default = _card(panel, "claude-default")
    for device, span in (("box", '<span class="ac-when">read 1m 00s ago</span>'),
                         ("pc", '<span class="warn">stale, read 1d 1h ago</span>'),
                         ("mac", '<span class="ac-when">no reading</span>')):
        row = _device(default, device)
        assert row.index(f"checked {CHECKED}") < row.index(span), row
    claude_b_pc = _device(_card(panel, "claude-b"), "pc")
    assert claude_b_pc.index("no reading") < claude_b_pc.index("same login as claude-default")
    cursor = _card(_panel(cursor_snapshot(tmp_path / "cursor")), "cursor-ultra")
    for device in ("box", "mac", "pc"):
        assert "read " not in _text(_device(cursor, device)) and "no reading" not in _device(cursor, device)


def test_a_mismatch_names_the_account_with_the_same_login(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    assert "same login as claude-default" in _text(_device(_card(panel, "claude-b"), "pc"))
    assert "same login as" not in _card(panel, "claude-default")


def test_used_and_headroom_show_with_the_floor_marked(tmp_path: Path) -> None:
    panel = _panel(_snapshot(tmp_path))
    default, claude_b = _facts(_card(panel, "claude-default")), _facts(_card(panel, "claude-b"))
    assert (default["5h used"], default["7d used"], default["headroom"]) == ("25%", "60%", "40% · floor 5%")
    assert (claude_b["5h used"], claude_b["7d used"], claude_b["headroom"]) == ("—", "—", "— · floor 5%")


def test_a_cursor_card_shows_its_plan_each_device_doctor_status_and_why_usage_is_not_tracked(
    tmp_path: Path,
) -> None:
    panel = _panel(cursor_snapshot(tmp_path))
    card = _card(panel, "cursor-ultra")
    facts = _facts(card)
    assert (facts["harness"], facts["plan"], facts["usage"]) == ("cursor", "ultra", f"not tracked: {CURSOR_REASON}")
    assert not {"5h used", "7d used", "headroom"} & set(facts)
    assert re.findall(r'<li class="ac-dev" data-device="([^"]+)">', card) == ["box", "mac", "pc"]
    assert '<span class="chip ok">ok</span>' in _device(card, "box")
    assert '<span class="chip bad">signed-out</span>' in _device(card, "mac")
    assert '<span class="chip warn">unreachable</span>' in _device(card, "pc")
    claude = _facts(_card(panel, "claude-default"))
    assert claude["5h used"] == "—"
    assert "plan" not in claude and "usage" not in claude


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


CURSOR_LOGIN = "env -u CURSOR_API_KEY -u CURSOR_AUTH_TOKEN -u CURSOR_CONFIG_DIR cursor-agent login"
CURSOR_MACHINES = MACHINES.replace(
    "accounts:\n", "accounts:\n  - {id: cursor-ultra, harness: cursor, plan: ultra}\n"
).replace("harnesses: {claude: claude-bot, codex: codex-bot}",
          "harnesses: {claude: claude-bot, codex: codex-bot, cursor: {bot: cursor-bot, account: cursor-ultra}}")


def _cursor_snapshot(tmp_path: Path) -> dict:
    db, machines = world(tmp_path)
    machines.write_text(CURSOR_MACHINES, encoding="utf-8")
    cursor = (sign_in("cursor-ultra", "box", "signed-out", method=None, subscription=None),
              sign_in("cursor-ultra", "pc", "ok", method="login", subscription=None))
    with contextlib.closing(sqlite3.connect(db)) as connection, connection:
        account_doctor.write(connection, SIGN_INS + cursor, config_of(machines), None, NOW - 300)
    return collect_snapshot(db, tmp_path / "poll.log", now=NOW, machines_path=machines)


def test_a_cursor_card_shows_plan_doctor_status_login_command_and_not_tracked_reason(tmp_path: Path) -> None:
    card = _card(_panel(_cursor_snapshot(tmp_path)), "cursor-ultra")
    facts = _facts(card)
    assert (facts["harness"], facts["plan"]) == ("cursor", "ultra")
    assert facts["usage"] == f"not tracked: {usage_readings.NOT_TRACKED['cursor']}"
    assert not {"5h used", "7d used", "headroom"} & set(facts)
    assert '<dd class="muted">not tracked: ' in card
    assert '<span class="chip bad">signed-out</span>' in _device(card, "box")
    assert '<span class="chip ok">ok</span>' in _device(card, "pc")
    assert re.findall(r'<code class="ac-cmd">([^<]*)</code>', card) == [CURSOR_LOGIN]
    assert "plan" not in _facts(_card(_panel(_cursor_snapshot(tmp_path / "again")), "claude-b"))


def _run(tmp_path: Path, snapshot: dict, actions: list | None = None) -> dict:
    from lib.dashboard_page import page_script
    html = render_pages(snapshot)["health.html"]
    assert '<script src="/app.js" defer></script>' in html
    script = page_script(snapshot.get("statechart"))
    return layout.run_harness(tmp_path, HARNESS, script, snapshot, "health", BAR + ["settings", "accounts"],
                              actions or [])


@needs_node
def test_script_draws_the_accounts_like_the_server(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, make=stale_world)
    account = next(a for a in snapshot["accounts"] if a["id"] == "claude-default")
    account["seven_day_used"], account["headroom"] = 0.97, 0.03
    out = _run(tmp_path / "run", snapshot, [["advance", 3600]])
    server = _panel(snapshot)
    for text in ("read 1m 00s ago", "stale, read 1d 1h ago", "no reading"):
        assert text in out["html"]["accounts"]
    assert out["html"]["accounts"] == server[server.index(">") + 1:server.rindex("<")]
    for flag in NOT_DOCUMENTED:
        assert flag not in out["html"]["accounts"]


@needs_node
def test_script_draws_a_cursor_card_like_the_server(tmp_path: Path) -> None:
    snapshot = _cursor_snapshot(tmp_path)
    out = _run(tmp_path / "run", snapshot)
    server = _panel(snapshot)
    assert out["html"]["accounts"] == server[server.index(">") + 1:server.rindex("<")]
    assert "not tracked: " in _card(out["html"]["accounts"], "cursor-ultra")


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
    from lib.dashboard_page import page_script
    snapshot = snapshot or _snapshot(tmp_path)
    html = render_pages(snapshot)["health.html"]
    assert '<script src="/app.js" defer></script>' in html
    script = page_script(snapshot.get("statechart"))
    return layout.run_harness(tmp_path / "run", _session_harness(SIGNED_IN, reply), script, snapshot, "health",
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
    from lib.dashboard_page import page_script
    snapshot = _snapshot(tmp_path)
    html = render_pages(snapshot)["health.html"]
    assert '<script src="/app.js" defer></script>' in html
    script = page_script(snapshot.get("statechart"))
    out = layout.run_harness(tmp_path / "run", _session_harness({"signed_in": False}), script, snapshot,
                             "health", BAR + ["settings", "accounts"], [])
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
    assert re.findall(r"<option[^>]*>([^<]*)</option>", form) == ["claude", "codex", "cursor", "gemini"]
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


DISABLED = ("Object.defineProperty(Node.prototype, 'disabled', {get() { return 'disabled' in this.attrs; },\n"
            "  set(v) { if (v) this.attrs.disabled = ''; else delete this.attrs.disabled; }});\n")
GENERATED = "body.attrs['data-generated']"
ADD_BUTTON = '<button type="button" data-op="add"( disabled="")?>Add account</button>'


def _add_form_after(tmp_path: Path, harness: list[str]) -> str:
    from lib.dashboard_page import page_script
    snapshot = _snapshot(tmp_path)
    assert '<script src="/app.js" defer></script>' in render_pages(snapshot)["health.html"]
    script = page_script(snapshot.get("statechart"))
    page = _session_harness(SIGNED_IN).replace(GENERATED, DISABLED + GENERATED, 1)
    actions = [_set(ADD, {"value": value}, **{"data-field": "harness"}) for value in harness]
    out = layout.run_harness(tmp_path / "run", page, script, snapshot, "health", BAR + ["settings", "accounts"],
                             actions)
    accounts = out["html"]["accounts"]
    start = accounts.index('data-form="add"')
    return accounts[start:accounts.index("</div>", accounts.index('role="status"', start))]


@needs_node
@pytest.mark.parametrize("harness", ["cursor", "gemini"])
def test_choosing_a_device_login_harness_in_the_add_form_shows_the_refusal(tmp_path: Path, harness: str) -> None:
    refusal = ADD_REFUSED[harness]
    assert f"harnesses.{harness}" in refusal and "machines.yml" in refusal
    form = _add_form_after(tmp_path / harness, [harness])
    assert re.search(ADD_BUTTON, form).group(1) == ' disabled=""', form
    assert form.endswith(f'role="status">{html.escape(refusal)}'), form
    form = _add_form_after(tmp_path / "back", [harness, "codex"])
    assert re.search(ADD_BUTTON, form).group(1) is None, form
    assert form.endswith('role="status">'), form


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


SIGNIN_ID = "sgn-0123456789abcdefgh"
LOADED_AT = 1800000003
CLAUDE_URL = "https://claude.com/cai/oauth/authorize?state=s1"
CODEX_URL = "https://auth.openai.com/codex/device"
REMIND = "Paste a code only from a sign-in you just started here. Never paste a code someone sent you."
START = ("POST", "/api/accounts/signin", 202, {"id": SIGNIN_ID})
SIGN_IN_MAC = _click(CLAUDE_B, "signin", **{"data-pair": "mac"})
TICK = ["advance", 1]
ROUTER = (
    "  if (url === '/api/settings' || url === '/api/signout' || url.startsWith('/api/accounts/')) {\n"
    "    const method = opts.method || 'GET';\n"
    "    steps.push({req: method, url, body: opts.body === undefined ? null : JSON.parse(opts.body),\n"
    "      csrf: opts.headers['X-Factory-CSRF'] === undefined ? null : opts.headers['X-Factory-CSRF']});\n"
    "    const at = replies.findIndex(r => r[0] === method && r[1] === url.split('?')[0]);\n"
    "    const [, , code, answer] = at < 0 ? [0, 0, 599, {error: 'unstubbed'}] : replies.splice(at, 1)[0];\n"
    "    return Promise.resolve({ok: code < 300, status: code, json: () => Promise.resolve(answer)});\n"
    "  }\n"
)
SNAP_ACTION = (
    "    } else if (kind === 'snap') {\n"
    "      const at = document.activeElement;\n"
    "      steps.push({snap: arg, html: ids.accounts.children.map(c => c.html()).join(''),\n"
    "        focus: at ? {field: at.getAttribute('data-field'), op: at.getAttribute('data-op'), value: at.value,\n"
    "          selection: [at.selectionStart, at.selectionEnd]} : null});\n"
    "    } else if (kind === 'focus') {\n"
    "      const node = pick(arg.pick);\n"
    "      if (!node) throw new Error('nothing to focus: ' + JSON.stringify(arg));\n"
    "      node.focus();\n"
    "      Object.assign(node, arg.props);\n"
    "      steps.push({focus: arg});\n"
)
DETACH_BLURS = (
    "  replaceChildren(...kids) { this.children.forEach(c => { c.parentNode = null; });\n",
    "  replaceChildren(...kids) { this.children.forEach(c => { c.parentNode = null; });\n"
    "    if (focusedNode && !focusedNode.isConnected) focusedNode = null;\n",
)
FOCUS_MOVES_CARET = (
    "  focus() { global.document.activeElement = this; }\n",
    "  focus() { if (this.tagName === 'input' && global.document.activeElement !== this) {\n"
    "      this.selectionStart = this.selectionEnd = String(this.value || '').length; }\n"
    "    global.document.activeElement = this; }\n"
    "  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }\n",
)
SETTLED = (
    "setTimeout(() => {\n  act(JSON.parse(actionsText));\n",
    "setTimeout(async () => {\n  for (const action of JSON.parse(actionsText)) {\n"
    "    act([action]);\n    await new Promise(resolve => setTimeout(resolve, 0));\n  }\n",
)


def _view(**fields: object) -> tuple:
    return ("GET", "/api/accounts/signin", 200, {"state": "awaiting-code", "url": None, "code": None,
                                                 "expires_at": None, "result": None, "error": None, **fields})


def _relay_harness(account_signin: bool | None, replies: list) -> str:
    session = {**SIGNED_IN, **({} if account_signin is None else {"account_signin": account_signin})}
    stubs = ("  if (url === '/api/session') return Promise.resolve({ok: true, status: 200, json: () =>\n"
             f"    Promise.resolve({json.dumps(session)})}});\n" + ROUTER)
    marker = "  if (url !== 'status.json' || opts.cache !== 'no-store') throw new Error('bad fetch ' + url);"
    key = "    } else if (kind === 'key') {\n"
    for needle in (marker, key, SCOPED_PICK[0], SETTLED[0], DETACH_BLURS[0], FOCUS_MOVES_CARET[0]):
        assert needle in HARNESS
    return (f"const replies = {json.dumps(replies)};\n" + HARNESS.replace(marker, stubs + marker)
            .replace(key, SET_ACTION + SNAP_ACTION + key).replace(*SCOPED_PICK).replace(*SETTLED)
            .replace(*DETACH_BLURS).replace(*FOCUS_MOVES_CARET))


def _relay(tmp_path: Path, actions: list, replies: list | tuple = (), *, account_signin: bool | None = True,
           snapshot: dict | None = None) -> dict:
    from lib.dashboard_page import page_script
    snapshot = snapshot or _snapshot(tmp_path)
    html = render_pages(snapshot)["health.html"]
    assert '<script src="/app.js" defer></script>' in html
    script = page_script(snapshot.get("statechart"))
    return layout.run_harness(tmp_path / "run", _relay_harness(account_signin, list(replies)), script, snapshot,
                              "health", BAR + ["settings", "accounts"], actions)


def _requests(out: dict) -> list[tuple]:
    return [(step["req"], step["url"], step["body"]) for step in out["steps"] if "req" in step]


def _gets(out: dict) -> list[str]:
    return [url for method, url, _ in _requests(out) if method == "GET"]


def _snap(out: dict, name: str) -> str:
    return next(step["html"] for step in out["steps"] if step.get("snap") == name)


def _signin(markup: str) -> str:
    start = markup.index('<div class="ac-signin"')
    return markup[start:markup.index('<div class="st-out', start)]


def _codex_b(tmp_path: Path) -> dict:
    snapshot = _snapshot(tmp_path)
    next(a for a in snapshot["accounts"] if a["id"] == "claude-b")["harness"] = "codex"
    return snapshot


def test_the_server_markup_keeps_the_cli_fallback_and_has_no_sign_in(tmp_path: Path) -> None:
    claude_b = _card(_panel(_snapshot(tmp_path)), "claude-b")
    assert re.findall(r'<code class="ac-cmd">([^<]*)</code>', claude_b) == [CLAUDE_B_PC, CLAUDE_B_MAC]
    for needle in ("<button", "ac-signin", "data-op"):
        assert needle not in claude_b


@needs_node
def test_with_the_relay_each_signed_in_device_has_sign_in_and_sign_out(tmp_path: Path) -> None:
    accounts = _relay(tmp_path, [])["html"]["accounts"]
    claude_b = _card(accounts, "claude-b")
    assert re.findall(r'data-op="signin" data-pair="([^"]+)">Sign in</button>', claude_b) == ["pc", "mac"]
    assert re.findall(r'data-op="signout" data-pair="([^"]+)">Sign out</button>', claude_b) == ["pc", "mac"]
    for read_only in ("claude-default", "codex-default", "ext-grok"):
        assert "data-op" not in _card(accounts, read_only)
    codex = _card(_relay(tmp_path / "codex", [], snapshot=_codex_b(tmp_path / "codex"))["html"]["accounts"],
                  "claude-b")
    assert re.findall(r'data-op="signin" data-pair="([^"]+)"', codex) == ["pc", "mac"]


@needs_node
def test_a_cursor_account_has_no_sign_in_button(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    next(a for a in snapshot["accounts"] if a["id"] == "claude-b")["harness"] = "cursor"
    claude_b = _card(_relay(tmp_path, [], snapshot=snapshot)["html"]["accounts"], "claude-b")
    assert 'data-op="signin"' not in claude_b
    assert re.findall(r'data-op="signout" data-pair="([^"]+)"', claude_b) == ["pc", "mac"]


@needs_node
@pytest.mark.parametrize("account_signin", [False, None], ids=["off", "absent"])
def test_without_the_relay_the_cli_command_is_the_only_route(tmp_path: Path, account_signin: bool | None) -> None:
    accounts = _relay(tmp_path, [], account_signin=account_signin)["html"]["accounts"]
    assert 'data-op="signin"' not in accounts
    claude_b = _card(accounts, "claude-b")
    assert re.findall(r'data-op="signout" data-pair="([^"]+)">Sign out</button>', claude_b) == ["pc", "mac"]
    assert re.findall(r'<code class="ac-cmd">([^<]*)</code>', claude_b) == [CLAUDE_B_PC, CLAUDE_B_MAC]
    assert re.findall(r'<code class="ac-cmd">([^<]*)</code>', _card(accounts, "codex-default")) == [CODEX_PC]


@needs_node
def test_sign_in_posts_the_pair_and_the_claude_panel_shows_link_countdown_cancel_paste_and_reminder(
        tmp_path: Path) -> None:
    expires = LOADED_AT + 1 + 600
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, ["snap", "first"], TICK],
                 [START, _view(url=CLAUDE_URL, expires_at=expires)])
    requests = [step for step in out["steps"] if "req" in step]
    assert (requests[0]["req"], requests[0]["url"], requests[0]["body"], requests[0]["csrf"]) == (
        "POST", "/api/accounts/signin", {"account": "claude-b", "device": "mac"}, "csrf-value")
    assert (requests[1]["req"], requests[1]["url"], requests[1]["body"], requests[1]["csrf"]) == (
        "GET", f"/api/accounts/signin?id={SIGNIN_ID}", None, "csrf-value")
    first = _signin(_card(_snap(out, "first"), "claude-b"))
    assert first.startswith('<div class="ac-signin" data-signin="mac">')
    assert "awaiting-code" in _text(first)
    link = f'<a class="ac-link" href="{CLAUDE_URL}" rel="noopener noreferrer" target="_blank">{CLAUDE_URL}</a>'
    assert link in first
    assert "10:00 left" in _text(first)
    assert 'data-op="signin-cancel">Cancel</button>' in first
    assert re.search(r'<input[^>]*data-field="signin-code"[^>]*>', first)
    assert 'autocomplete="off"' in first
    assert 'data-op="signin-code">Send</button>' in first
    assert f'<p class="ac-remind">{REMIND}</p>' in first
    assert "ac-code" not in first
    assert "9:59 left" in _text(_signin(_card(out["html"]["accounts"], "claude-b")))


@needs_node
def test_a_codex_sign_in_shows_the_link_and_the_code_and_no_paste_field(tmp_path: Path) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK],
                 [START, _view(state="awaiting-approval", url=CODEX_URL, code="ABCD-1234",
                               expires_at=LOADED_AT + 1 + 900)],
                 snapshot=_codex_b(tmp_path))
    panel = _signin(_card(out["html"]["accounts"], "claude-b"))
    assert f'<a class="ac-link" href="{CODEX_URL}" rel="noopener noreferrer" target="_blank">' in panel
    assert '<code class="ac-code">ABCD-1234</code>' in panel
    assert "15:00 left" in _text(panel)
    assert 'data-op="signin-cancel"' in panel
    assert "signin-code" not in panel
    assert f'<p class="ac-remind">{REMIND}</p>' in panel


@needs_node
def test_a_pasted_code_is_sent_once_and_the_field_reads_code_sent(tmp_path: Path) -> None:
    code_field = _set(CLAUDE_B, {"value": " abc#123 "}, **{"data-field": "signin-code"})
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, code_field, _click(CLAUDE_B, "signin-code")],
                 [START, _view(url=CLAUDE_URL), ("POST", "/api/accounts/signin/code", 202, {})])
    assert _requests(out)[-1] == ("POST", "/api/accounts/signin/code", {"id": SIGNIN_ID, "code": "abc#123"})
    panel = _signin(_card(out["html"]["accounts"], "claude-b"))
    assert "code sent" in _text(panel)
    assert "signin-code" not in panel


@needs_node
def test_a_refused_code_shows_the_error_and_keeps_the_field(tmp_path: Path) -> None:
    code_field = _set(CLAUDE_B, {"value": "abc"}, **{"data-field": "signin-code"})
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, code_field, _click(CLAUDE_B, "signin-code")],
                 [START, _view(url=CLAUDE_URL), ("POST", "/api/accounts/signin/code", 409, {"error": "code-used"})])
    card = _card(out["html"]["accounts"], "claude-b")
    assert '<div class="st-out bad" role="status">error: code-used</div>' in card
    assert 'data-field="signin-code"' in _signin(card)


@needs_node
def test_cancel_posts_the_attempt_and_the_cancelled_result_ends_the_panel(tmp_path: Path) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, _click(CLAUDE_B, "signin-cancel"), TICK],
                 [START, _view(url=CLAUDE_URL), ("POST", "/api/accounts/signin/cancel", 202, {}),
                  _view(state="verifying", result="cancelled")])
    assert ("POST", "/api/accounts/signin/cancel", {"id": SIGNIN_ID}) in _requests(out)
    panel = _signin(_card(out["html"]["accounts"], "claude-b"))
    assert "sign-in cancelled" in _text(panel)
    assert "signin-cancel" not in panel and "signin-code" not in panel


@needs_node
@pytest.mark.parametrize("result,error,line", [("ok", None, "sign-in ok"),
                                               ("failed", "launch-exit", "sign-in failed: launch-exit")])
def test_a_result_stops_polling_and_drops_cancel_and_the_paste_field(
        tmp_path: Path, result: str, error: str | None, line: str) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, TICK, TICK],
                 [START, _view(url=CLAUDE_URL, result=result, error=error)])
    assert _gets(out) == [f"/api/accounts/signin?id={SIGNIN_ID}"]
    panel = _signin(_card(out["html"]["accounts"], "claude-b"))
    assert line in _text(panel)
    for gone in ("signin-cancel", "signin-code", "ac-link"):
        assert gone not in panel


@needs_node
def test_a_foreign_url_draws_no_link(tmp_path: Path) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK], [START, _view(url="https://evil.example/claude.com/")])
    panel = _signin(_card(out["html"]["accounts"], "claude-b"))
    assert "<a" not in panel and "evil.example" not in panel
    assert 'data-op="signin-cancel"' in panel


@needs_node
def test_a_missing_attempt_ends_the_panel_with_no_attempt(tmp_path: Path) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, TICK], [START, ("GET", "/api/accounts/signin", 404, {"error": "x"})])
    assert len(_gets(out)) == 1
    panel = _signin(_card(out["html"]["accounts"], "claude-b"))
    assert "error: no-attempt" in _text(panel)
    assert "signin-cancel" not in panel


@needs_node
def test_a_refused_start_shows_the_error_on_the_card_and_no_panel(tmp_path: Path) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK], [("POST", "/api/accounts/signin", 409, {"error": "running-job"})])
    card = _card(out["html"]["accounts"], "claude-b")
    assert '<div class="st-out bad" role="status">error: running-job</div>' in card
    assert "ac-signin" not in card
    assert _gets(out) == []


@needs_node
def test_the_cli_fallback_stays_while_a_sign_in_panel_is_open(tmp_path: Path) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK], [START, _view(url=CLAUDE_URL)])
    claude_b = _card(out["html"]["accounts"], "claude-b")
    assert 'data-signin="mac"' in claude_b
    assert re.findall(r'<code class="ac-cmd">([^<]*)</code>', claude_b) == [CLAUDE_B_PC, CLAUDE_B_MAC]


@needs_node
def test_a_card_runs_one_sign_in_at_a_time(tmp_path: Path) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, _click(CLAUDE_B, "signin", **{"data-pair": "pc"}), TICK],
                 [START, ("POST", "/api/accounts/signin", 202, {"id": "other"}), _view()])
    assert [body for method, url, body in _requests(out) if method == "POST"] == [
        {"account": "claude-b", "device": "mac"}]
    accounts = out["html"]["accounts"]
    assert accounts.count('class="ac-signin"') == 1 and 'data-signin="mac"' in accounts


@needs_node
def test_a_finished_sign_in_can_start_again_on_another_device(tmp_path: Path) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, _click(CLAUDE_B, "signin", **{"data-pair": "pc"}), TICK, TICK],
                 [START, _view(result="ok"), ("POST", "/api/accounts/signin", 202, {"id": "second"}),
                  _view(), _view()])
    assert _gets(out) == [f"/api/accounts/signin?id={SIGNIN_ID}", "/api/accounts/signin?id=second",
                          "/api/accounts/signin?id=second"]
    accounts = out["html"]["accounts"]
    assert accounts.count('class="ac-signin"') == 1 and 'data-signin="pc"' in accounts


@needs_node
def test_signing_out_of_github_stops_the_polling(tmp_path: Path) -> None:
    github_sign_out = ["click", {"within": {"cls": "st-auth"}, "attr": {"type": "button"}}]
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, github_sign_out, TICK, TICK],
                 [START, _view(), ("POST", "/api/signout", 200, {})])
    assert ("POST", "/api/signout", {}) in _requests(out)
    assert len(_gets(out)) == 1
    assert "ac-signin" not in out["html"]["accounts"]


def _focus(within: dict, props: dict | None = None, **attr: str) -> list:
    return ["focus", {"pick": {"within": within, "attr": attr}, "props": props or {}}]


def _focused(out: dict, name: str) -> dict | None:
    return next(step["focus"] for step in out["steps"] if step.get("snap") == name)


@needs_node
def test_polling_keeps_focus_and_selection_in_the_code_field(tmp_path: Path) -> None:
    typing = _focus(CLAUDE_B, {"value": "partial-code", "selectionStart": 3, "selectionEnd": 7},
                    **{"data-field": "signin-code"})
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, typing, ["snap", "before"], TICK, ["snap", "tick"],
                            TICK, ["snap", "changed"]],
                 [START, _view(url=CLAUDE_URL, expires_at=LOADED_AT + 600), _view(url=CLAUDE_URL,
                                                                                  expires_at=LOADED_AT + 600),
                  _view(state="verifying", url=CLAUDE_URL, expires_at=LOADED_AT + 600)])
    field = {"field": "signin-code", "op": None, "value": "partial-code", "selection": [3, 7]}
    assert _focused(out, "before") == field
    assert _focused(out, "tick") == field
    assert _focused(out, "changed") == field
    assert "verifying" in _text(_signin(_card(_snap(out, "changed"), "claude-b")))


@needs_node
@pytest.mark.parametrize("op", ["signin-code", "signin-cancel"])
def test_polling_keeps_focus_on_the_panel_buttons(tmp_path: Path, op: str) -> None:
    out = _relay(tmp_path, [SIGN_IN_MAC, TICK, _focus(CLAUDE_B, **{"data-op": op}), TICK, ["snap", "tick"],
                            TICK, ["snap", "changed"]],
                 [START, _view(url=CLAUDE_URL), _view(url=CLAUDE_URL), _view(state="verifying", url=CLAUDE_URL)])
    for name in ("tick", "changed"):
        focus = _focused(out, name)
        assert focus is not None and focus["op"] == op, name


def test_a_cursor_card_with_a_recorded_reading_shows_the_window_numbers(tmp_path: Path) -> None:
    card = _card(_panel(recorded_cursor_snapshot(tmp_path)), "cursor-ultra")
    facts = _facts(card)
    assert (facts["5h used"], facts["7d used"], facts["headroom"]) == ("—", "1%", "99% · floor 5%")
    assert "usage" not in facts
    assert "not tracked" not in card


@needs_node
def test_script_draws_a_recorded_cursor_card_like_the_server(tmp_path: Path) -> None:
    snapshot = recorded_cursor_snapshot(tmp_path)
    out = _run(tmp_path / "run", snapshot)
    server = _panel(snapshot)
    assert out["html"]["accounts"] == server[server.index(">") + 1:server.rindex("<")]
    assert "not tracked" not in _card(out["html"]["accounts"], "cursor-ultra")
