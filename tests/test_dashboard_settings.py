"""settings.html (#777): one card per machines.yml device, its controls when
signed in, and the settings audit results the snapshot carries."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from lib import live_settings
from lib.dashboard import collect_snapshot, render_pages, write_snapshot_pages
from tests import test_dashboard_layout as layout
from tests.test_dashboard import GH_CLASSIC, GH_OAUTH, GH_PAT, NOW, make_db
from tests.test_dashboard_layout import BAR, HARNESS, _no_script, _section, _text, needs_node

PLANTED = "planted-" + "Kq7Vt2Lm9Wx4" * 2
MACHINES = (
    "devices:\n"
    "  - {name: box, local: true, drain: true, slot_max: 3, harnesses: [claude, codex]}\n"
    f"  - {{name: pc, slot_max: 2, harnesses: [codex], api_key: {PLANTED}}}\n"
    "  - {name: mac, enabled: false, slot_max: 2, harnesses: [claude]}\n"
)
AUDIT = [
    {"id": "a" * 16, "actor": "maxharris1", "requested_at": "2026-11-17T10:00:00Z",
     "applied_at": "2026-11-17T10:01:00Z", "device": "pc", "field": "slot_max", "previous": 1, "new": 2,
     "result": "applied", "error": None, "backup": "machines.yml.bak-settings-20261117T100100Z",
     "restarted": ["poller", "waiter-codex"], "pending": [], "token": PLANTED},
    {"id": "b" * 16, "actor": "maxharris1", "requested_at": "2026-11-17T11:00:00Z",
     "applied_at": "2026-11-17T11:01:00Z", "device": "mac", "field": "enabled", "previous": False, "new": True,
     "result": "refused", "error": "expect does not match", "backup": None, "restarted": [], "pending": [],
     "cookie": PLANTED},
    {"id": "c" * 16, "actor": "maxharris1", "requested_at": "2026-11-17T12:00:00Z",
     "applied_at": "2026-11-17T12:01:00Z", "device": "box", "field": "drain", "previous": False, "new": True,
     "result": "failed", "error": f"restart failed {GH_OAUTH}", "backup": None, "restarted": [], "pending": []},
]
IDS = BAR + ["hud", "settings"]


def _snapshot(tmp_path: Path, audit: list | None = AUDIT, live: str | None = None,
              seen: dict[str, int] | None = None) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS device_slots (job_id TEXT PRIMARY KEY, device TEXT)")
        connection.executemany("INSERT INTO device_slots VALUES (?, 'box')", [("j1",), ("j2",), ("j3",)])
    machines = tmp_path / "machines.yml"
    machines.write_text(MACHINES, encoding="utf-8")
    if live is not None:
        _settings_row(db, tmp_path / "live" / "machines.yml", adopt=live == "adopted", seen=seen or {})
    if audit is not None:
        lines = [json.dumps(entry) for entry in audit]
        (tmp_path / "settings-audit.jsonl").write_text("\n".join([*lines, "not json"]) + "\n", encoding="utf-8")
    return collect_snapshot(db, tmp_path / "poller.log", now=NOW, machines_path=machines)


def _card(markup: str, device: str) -> str:
    start = markup.index(f'<article class="st-card" data-device="{device}">')
    return markup[start:markup.index("</article>", start)]


def _facts(card: str) -> dict[str, str]:
    return dict(re.findall(r"<dt>([^<]*)</dt><dd[^>]*>([^<]*)</dd>", card))


def test_device_rows_carry_local_and_drain(tmp_path: Path) -> None:
    rows = {row["name"]: row for row in _snapshot(tmp_path)["slots"]["devices"]}
    assert {name: (row["local"], row["drain"], row["held"]) for name, row in rows.items()} == {
        "box": (True, True, 3), "pc": (False, False, 0), "mac": (False, False, 0)}


def test_settings_results_keep_only_audit_keys_oldest_first(tmp_path: Path) -> None:
    results = _snapshot(tmp_path)["settings"]["results"]
    assert [r["id"] for r in results] == ["a" * 16, "b" * 16, "c" * 16]
    assert all(set(r) == set(AUDIT[0]) - {"token"} | {"version"} for r in results)
    assert results[0]["restarted"] == ["poller", "waiter-codex"]
    assert results[1]["previous"] is False and results[1]["new"] is True


def test_no_audit_log_is_no_results(tmp_path: Path) -> None:
    assert _snapshot(tmp_path, audit=None)["settings"] == {"results": []}


def test_each_card_shows_its_device(tmp_path: Path) -> None:
    page = _section(_no_script(render_pages(_snapshot(tmp_path))["settings.html"]), "settings")
    assert "3 devices" in page
    expect = {
        "box": {"enabled": "yes", "slot_max": "3", "harnesses": "claude, codex", "slots": "3 of 3 held",
                "drain": "draining, 3 running"},
        "pc": {"enabled": "yes", "slot_max": "2", "harnesses": "codex", "slots": "0 of 2 held", "drain": "off"},
        "mac": {"enabled": "no", "slot_max": "2", "harnesses": "claude", "slots": "0 of 2 held", "drain": "off"},
    }
    for device, facts in expect.items():
        assert _facts(_card(page, device)) == facts, device
    assert "local · orch host" in _card(page, "box")
    assert "remote" in _text(_card(page, "pc")) and "orch host" not in _card(page, "pc")

def _settings_row(db: Path, machines: Path, *, adopt: bool, seen: dict[str, int]) -> None:
    machines.parent.mkdir()
    machines.write_text("devices:\n  - {name: box, local: true, slot_max: 3, harnesses: {claude: claude-bot}}\n",
                        encoding="utf-8")
    with sqlite3.connect(db, isolation_level=None) as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert live_settings.narrowed(connection, machines).written == 1
        if adopt:
            live_settings.adopted(connection, machines)
        for harness, version in seen.items():
            live_settings.record_seen(connection, harness, version)
        connection.execute("COMMIT")


def test_a_drained_device_reads_drained(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot["slots"]["devices"][0]["held"] = 0
    page = render_pages(snapshot)["settings.html"]
    assert _facts(_card(page, "box"))["drain"] == "drained"


def test_each_result_sits_on_its_own_card(tmp_path: Path) -> None:
    page = _section(_no_script(render_pages(_snapshot(tmp_path))["settings.html"]), "settings")
    mac = _card(page, "mac")
    assert '<span class="chip warn">refused</span>' in mac
    assert '<span class="st-err">expect does not match</span>' in mac
    assert "enabled false → true" in mac
    assert '<span class="chip ok">applied</span>' in _card(page, "pc")
    assert '<span class="chip bad">failed</span>' in _card(page, "box")
    assert "refused" not in _card(page, "pc")


PENDING = {**AUDIT[0], "id": "d" * 16, "result": "pending", "pending": ["waiter-codex-bot"]}


def test_a_pending_result_names_the_waiter_left_to_restart(tmp_path: Path) -> None:
    page = _section(_no_script(render_pages(_snapshot(tmp_path, [PENDING]))["settings.html"]), "settings")
    pc = _card(page, "pc")
    assert '<span class="chip warn">pending</span>' in pc
    assert '<span class="st-err">restart pending: waiter-codex-bot</span>' in pc


VERSIONED = {**AUDIT[0], "id": "e" * 16, "result": "applied", "version": 1}


@pytest.mark.parametrize("live", [None, "written"])
def test_a_versioned_change_is_pending_until_the_poller_adopts_it(tmp_path: Path, live: str | None) -> None:
    snapshot = _snapshot(tmp_path, [AUDIT[0], VERSIONED], live)
    old, versioned = snapshot["settings"]["results"]
    assert (old["result"], "waiting" in old) == ("applied", False), "a line with no version keeps its result"
    assert (versioned["result"], versioned["waiting"]) == ("pending", True)
    pc = _card(_section(_no_script(render_pages(snapshot)["settings.html"]), "settings"), "pc")
    assert '<span class="chip warn">pending</span>' in pc
    assert "<span class=\"st-err\">waiting for the poller&#x27;s next cycle</span>" in pc


def test_a_versioned_change_is_applied_once_the_poller_adopts_it(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, [VERSIONED], "adopted")
    (versioned,) = snapshot["settings"]["results"]
    assert (versioned["result"], versioned["waiting"]) == ("applied", False)
    pc = _card(_section(_no_script(render_pages(snapshot)["settings.html"]), "settings"), "pc")
    assert '<span class="chip ok">applied</span>' in pc
    assert "waiting" not in pc and "pending" not in pc


def test_a_pending_change_stays_pending_once_the_poller_adopts_it(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, [{**PENDING, "version": 1}], "adopted")
    (row,) = snapshot["settings"]["results"]
    assert (row["result"], row.get("waiting")) == ("pending", None)
    pc = _card(_section(_no_script(render_pages(snapshot)["settings.html"]), "settings"), "pc")
    assert '<span class="chip warn">pending</span>' in pc
    assert '<span class="st-err">restart pending: waiter-codex-bot</span>' in pc


@pytest.mark.parametrize("seen", [{"codex": 1}, {"codex": 2}])
def test_a_pending_change_is_applied_once_the_waiter_places_by_it(tmp_path: Path, seen: dict) -> None:
    snapshot = _snapshot(tmp_path, [{**PENDING, "version": 1}], "adopted", seen)
    (row,) = snapshot["settings"]["results"]
    assert row["result"] == "applied"
    pc = _card(_section(_no_script(render_pages(snapshot)["settings.html"]), "settings"), "pc")
    assert '<span class="chip ok">applied</span>' in pc
    assert "pending" not in pc


@pytest.mark.parametrize("live,seen", [("adopted", {"codex": 0}), ("adopted", {"claude": 1}),
                                       ("written", {"codex": 1})],
                         ids=["older", "other-harness", "not-adopted"])
def test_a_pending_change_stays_pending_until_the_waiter_places_by_it(
    tmp_path: Path, live: str, seen: dict
) -> None:
    (row,) = _snapshot(tmp_path, [{**PENDING, "version": 1}], live, seen)["settings"]["results"]
    assert (row["result"], row.get("waiting")) == ("pending", None)


@pytest.mark.parametrize("live,result", [("written", "pending"), ("adopted", "applied")])
def test_a_change_pending_only_on_the_poller_is_applied_once_it_adopts(
    tmp_path: Path, live: str, result: str
) -> None:
    entry = {**PENDING, "pending": ["poller"], "version": 1}
    (row,) = _snapshot(tmp_path, [entry], live)["settings"]["results"]
    assert row["result"] == result


def test_a_pending_change_naming_an_unknown_service_stays_pending(tmp_path: Path) -> None:
    entry = {**PENDING, "pending": ["poller", "waiter-grok-bot"], "version": 1}
    (row,) = _snapshot(tmp_path, [entry], "adopted", {"grok": 1})["settings"]["results"]
    assert row["result"] == "pending"


def test_every_page_links_settings(tmp_path: Path) -> None:
    pages = render_pages(_snapshot(tmp_path))
    for name, html in pages.items():
        assert '<a href="settings.html"' in _section(html, "nav"), name
    assert '<a href="settings.html" aria-current="page">Settings</a>' in pages["settings.html"]


def test_no_credential_reaches_the_page_or_status_json(tmp_path: Path) -> None:
    out = tmp_path / "out"
    write_snapshot_pages(out, _snapshot(tmp_path))
    for name in ("settings.html", "status.json"):
        text = (out / name).read_text(encoding="utf-8")
        for secret in (PLANTED, GH_OAUTH, GH_CLASSIC, GH_PAT, "gho_", "ghp_", "github_pat_"):
            assert secret not in text, (name, secret)
    assert "restart failed" in (out / "status.json").read_text(encoding="utf-8")



def _harness(session: dict | None, reply: dict | None = None) -> str:
    stubs = ""
    if session is not None:
        stubs += ("  if (url === '/api/session') return Promise.resolve({ok: true, status: 200, json: () =>\n"
                  f"    Promise.resolve({json.dumps(session)})}});\n")
    if reply is not None:
        stubs += ("  if (url === '/api/settings') { steps.push({post: url, method: opts.method, body: JSON.parse(opts.body),\n"
                  "    csrf: opts.headers['X-Factory-CSRF']});\n"
                  f"    return Promise.resolve({{ok: {json.dumps(reply['status'] < 300)}, status: {reply['status']},\n"
                  f"      json: () => Promise.resolve({json.dumps(reply['body'])})}}); }}\n")
    marker = "  if (url !== 'status.json' || opts.cache !== 'no-store') throw new Error('bad fetch ' + url);"
    # The clicks' POST settles between two timers, before the page is read.
    settle = "  act(JSON.parse(actionsText));\n"
    harness = HARNESS.replace(marker, stubs + marker).replace(settle, settle + "}, 200);\nsetTimeout(() => {\n")
    assert marker in harness and harness.count("setTimeout(() => {\n") == 2
    return harness


def _run(tmp_path: Path, snapshot: dict, harness: str, actions: list | None = None) -> dict:
    html = render_pages(snapshot)["settings.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    return layout.run_harness(tmp_path, harness, script, snapshot, "settings", IDS, actions or [])


SIGNED_IN = {"signed_in": True, "login": "maxharris1", "csrf": "csrf-value"}


@needs_node
@pytest.mark.parametrize("audit,live", [(AUDIT, None), ([PENDING], None), ([VERSIONED], "written"),
                                        ([VERSIONED], "adopted")],
                         ids=["results", "pending", "waiting", "adopted"])
def test_script_draws_the_cards_like_the_server(tmp_path: Path, audit: list, live: str | None) -> None:
    snapshot = _snapshot(tmp_path, audit, live)
    out = _run(tmp_path, snapshot, _harness(None))
    server = _section(_no_script(render_pages(snapshot)["settings.html"]), "settings")
    assert out["html"]["settings"] == server[server.index(">") + 1:server.rindex("<")]


@needs_node
def test_signed_out_shows_the_sign_in_and_no_controls(tmp_path: Path) -> None:
    out = _run(tmp_path, _snapshot(tmp_path), _harness({"signed_in": False}))
    assert "Sign in with GitHub" in out["text"]["settings"]
    assert "st-ctl" not in out["html"]["settings"]


@needs_node
def test_signed_in_each_card_has_its_controls(tmp_path: Path) -> None:
    out = _run(tmp_path, _snapshot(tmp_path), _harness(SIGNED_IN))
    html = out["html"]["settings"]
    assert "signed in as maxharris1" in out["text"]["settings"]
    assert html.count('<div class="st-ctl">') == 3
    box, mac = _card(html, "box"), _card(html, "mac")
    assert 'data-field="enabled" aria-pressed="true">Disable</button>' in box
    assert 'data-field="drain" aria-pressed="true">Stop draining</button>' in box
    assert 'data-field="enabled" aria-pressed="false">Enable</button>' in mac
    assert 'data-field="drain" aria-pressed="false">Drain</button>' in mac
    assert 'type="number" min="1" max="64" data-field="slot_max" aria-label="slot_max for box"' in box
    assert "Apply slot_max" in box


@needs_node
def test_a_toggle_posts_the_shown_value_and_shows_queued(tmp_path: Path) -> None:
    harness = _harness(SIGNED_IN, {"status": 202, "body": {"id": "x" * 22}})
    out = _run(tmp_path, _snapshot(tmp_path), harness, [["click", {"attr": {"data-field": "drain"}}]])
    post = next(step for step in out["steps"] if "post" in step)
    assert post == {"post": "/api/settings", "method": "POST", "csrf": "csrf-value",
                    "body": {"device": "box", "field": "drain", "expect": True, "value": False}}
    assert "queued (applies within ~2 min)" in _card(out["html"]["settings"], "box")


@needs_node
def test_apply_posts_slot_max_with_its_shown_value(tmp_path: Path) -> None:
    harness = _harness(SIGNED_IN, {"status": 202, "body": {"id": "x" * 22}})
    out = _run(tmp_path, _snapshot(tmp_path), harness, [["click", {"attr": {"data-field": "apply"}}]])
    post = next(step for step in out["steps"] if "post" in step)
    assert post["body"] == {"device": "box", "field": "slot_max", "expect": 3, "value": 3}


@pytest.mark.parametrize("status,error", [(409, "changed"), (403, "not-approver"), (429, "busy"), (400, "bad-value")])
@needs_node
def test_an_error_shows_inline_on_its_card(tmp_path: Path, status: int, error: str) -> None:
    harness = _harness(SIGNED_IN, {"status": status, "body": {"error": error}})
    out = _run(tmp_path, _snapshot(tmp_path), harness, [["click", {"attr": {"data-field": "enabled"}}]])
    html = out["html"]["settings"]
    assert f'<div class="st-out bad" role="status">error: {error}</div>' in _card(html, "box")
    assert f"error: {error}" not in _card(html, "pc")
