"""Mission-control dashboard: the status.json fields the live page needs, and
the page itself (polls status.json every 1 to 2 seconds, no external assets,
reduced motion respected, WCAG AA contrast, read-only and redacted).
"""

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import lib.dashboard as dashboard
import lib.dashboard_page as page
import lib.poll_source as poll_source
from lib.dashboard import collect_snapshot, render_html, write_snapshot
from tests.test_dashboard import (
    FREE_TEXT_SECRETS,
    GH_CLASSIC,
    NOW,
    WEBHOOK_URL,
    add_job,
    add_poll_error,
    seeded,
)

ROOT = Path(__file__).resolve().parents[1]


def _slots_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS device_slots (job_id TEXT PRIMARY KEY, "
        "device TEXT, harness TEXT, os_user TEXT, placed_at REAL)"
    )


def _handled(connection: sqlite3.Connection, *rows: tuple[str, str, str, str]) -> None:
    connection.executemany(
        "INSERT INTO handled_comments (repo, ticket_id, comment_id, outcome) VALUES (?, ?, ?, ?)",
        rows,
    )


def _finish(connection: sqlite3.Connection, job_id: str, finished_at: float) -> None:
    connection.execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (finished_at, job_id))


# --- running jobs: device lane, elapsed time, heartbeat ----------------------------


def test_running_jobs_carry_their_lane_and_elapsed_time(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _slots_table(connection)
        connection.execute(
            "INSERT INTO device_slots VALUES ('r-codex-review', 'pc', 'codex', 'codex-bot', ?)",
            (NOW - 300,),
        )
        add_job(
            connection,
            "r-grok-wake",
            action="blocked",
            recipient="grok",
            state="running",
            created_at=NOW - 90,
            last_activity_at=NOW - 20,
            ticket_id="15",
        )
    running = {row["ticket_id"]: row for row in collect_snapshot(db, log, now=NOW)["running"]}
    # Placed on a device: elapsed counts from the placement.
    assert running["13"]["device"] == "pc"
    assert running["13"]["elapsed_seconds"] == 300
    assert running["13"]["elapsed_from"] == "placed"
    assert running["13"]["heartbeat_age_seconds"] == 45
    # Not placed: a box slot, and the job table has no claim time, so elapsed
    # counts from when the job was queued (and says so).
    assert running["14"]["device"] == "box"
    assert running["14"]["elapsed_seconds"] == 600
    assert running["14"]["elapsed_from"] == "queued"
    # Grok wakes hold no slot.
    assert running["15"]["device"] is None
    assert running["15"]["recipient"] == "grok"


def test_running_jobs_without_a_device_slots_table_are_box_jobs(tmp_path: Path) -> None:
    snapshot = collect_snapshot(*seeded(tmp_path), now=NOW)
    assert {row["device"] for row in snapshot["running"]} == {"box"}


def test_an_unknown_device_name_is_redacted_text_not_markup(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _slots_table(connection)
        connection.execute(
            "INSERT INTO device_slots VALUES ('r-codex-review', ?, 'codex', 'codex-bot', ?)",
            (f"pc {WEBHOOK_URL}", NOW - 5),
        )
    text = json.dumps(collect_snapshot(db, log, now=NOW))
    assert WEBHOOK_URL not in text


# --- recent handoffs and verdicts -----------------------------------------------------


def test_recent_handoffs_newest_first_with_their_kind(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _finish(connection, "d-claude-build-1", NOW - 400)
        _finish(connection, "d-codex-review-1", NOW - 100)
        _finish(connection, "f-claude-build", NOW - 50)
        _finish(connection, "g-grok-blocked", NOW - 200)
    recent = collect_snapshot(db, log, now=NOW)["handoff_savings"]["recent"]
    assert [(row["job"], row["kind"], row["age_seconds"]) for row in recent] == [
        ("f-claude", "failed", 50),
        ("d-codex-", "scripted", 100),
        ("g-grok-b", "bot_to_bot", 200),
        ("d-claude", "scripted", 400),
    ]
    assert set(recent[0]) == {
        "job", "repo", "ticket_id", "action", "recipient", "state", "kind", "age_seconds",
    }
    # The handoff comment id itself is never copied out.
    assert "9001" not in json.dumps(recent) and "9002" not in json.dumps(recent)


def test_recent_handoffs_are_capped(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        for index in range(dashboard.RECENT_HANDOFFS + 5):
            add_job(connection, f"many-{index:03d}", state="done", handoff_comment_id=str(index))
            _finish(connection, f"many-{index:03d}", NOW - index)
    recent = collect_snapshot(db, log, now=NOW)["handoff_savings"]["recent"]
    assert len(recent) == dashboard.RECENT_HANDOFFS


def test_recent_verdicts_newest_first_and_classified(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _handled(
            connection,
            ("maxharris1/cleffy", "12", "c1", "queued-review"),
            ("maxharris1/cleffy", "13", "c2", "queued-review-fix"),
            ("aut0-code/aut0", "AUT-7", "c3", "stale-approve"),
            ("maxharris1/cleffy", "13", "c4", "merged"),
            ("maxharris1/cleffy", "12", "c5", "rejected-ready-author"),
            ("maxharris1/cleffy", "12", "c6", "stuck-ci-red-3"),
            ("maxharris1/cleffy", "12", "c7", "Not An <Outcome>"),
        )
    verdicts = collect_snapshot(db, log, now=NOW)["poller"]["verdicts"]
    assert [(row["outcome"], row["kind"]) for row in verdicts] == [
        ("other", "other"),
        ("stuck-ci-red-3", "stuck"),
        ("rejected-ready-author", "rejected"),
        ("merged", "shipped"),
        ("stale-approve", "stale"),
        ("queued-review-fix", "routed"),
        ("queued-review", "routed"),
    ]
    assert verdicts[-1] == {
        "repo": "maxharris1/cleffy",
        "ticket_id": "12",
        "outcome": "queued-review",
        "kind": "routed",
    }


def test_verdicts_absent_on_a_db_without_the_table(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE handled_comments")
    assert collect_snapshot(db, log, now=NOW)["poller"]["verdicts"] is None


# --- poll health and Linear budget ------------------------------------------------------


@pytest.mark.parametrize(
    ("ago", "state"),
    [(None, "unknown"), (5, "ok"), (150, "ok"), (151, "late"), (300, "late"), (301, "stale")],
)
def test_poll_health_from_the_last_cycle(tmp_path: Path, ago, state) -> None:
    db, log = seeded(tmp_path)
    last = None if ago is None else NOW - ago
    health = collect_snapshot(db, log, now=NOW, last_cycle_at=last)["poller"]["health"]
    assert health == {
        "state": state,
        "cycle_seconds": 60,
        "last_cycle_age_seconds": ago,
        "last_activity_age_seconds": 10,
    }


def test_linear_budget_ok_when_nothing_was_deferred(tmp_path: Path) -> None:
    budget = collect_snapshot(*seeded(tmp_path), now=NOW)["poller"]["linear_budget"]
    assert budget == {
        "state": "ok",
        "deferrals_last_hour": 0,
        "exhausted_last_hour": 0,
        "rate_limited_last_hour": 0,
        "requests_per_hour": 1250,
        "complexity_per_hour": 1_500_000,
    }


def test_linear_budget_constants_match_the_poller() -> None:
    assert dashboard.LINEAR_BUDGET_REQUESTS_PER_HOUR == int(
        poll_source.LINEAR_REQUESTS_PER_HOUR * poll_source.LINEAR_SHARE
    )
    assert dashboard.LINEAR_BUDGET_COMPLEXITY_PER_HOUR == int(
        poll_source.LINEAR_COMPLEXITY_PER_HOUR * poll_source.LINEAR_SHARE
    )


def test_linear_budget_states(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO poll_deferrals (detail, recorded_at) VALUES (?, ?)",
            ("Linear poll budget deferred discovery x2", NOW - 60),
        )
        connection.execute(
            "INSERT INTO poll_deferrals (detail, recorded_at) VALUES (?, ?)",
            ("Linear poll budget deferred tracked ticket x1", NOW - 7200),
        )
    assert collect_snapshot(db, log, now=NOW)["poller"]["linear_budget"]["state"] == "deferring"
    with sqlite3.connect(db) as connection:
        add_poll_error(connection, "PollSourceError:Linear poll budget is exhausted",
                       recorded_at=NOW - 30)
    budget = collect_snapshot(db, log, now=NOW)["poller"]["linear_budget"]
    assert budget["state"] == "exhausted"
    assert budget["deferrals_last_hour"] == 1
    assert budget["exhausted_last_hour"] == 1
    with sqlite3.connect(db) as connection:
        add_poll_error(connection, "RetryablePollSourceError:Linear returned HTTP 429",
                       recorded_at=NOW - 20)
        add_poll_error(connection, "PollSourceError:Linear returned RATELIMITED",
                       recorded_at=NOW - 10)
    budget = collect_snapshot(db, log, now=NOW)["poller"]["linear_budget"]
    assert budget["state"] == "rate_limited"
    assert budget["rate_limited_last_hour"] == 2


# --- redaction of the new fields --------------------------------------------------------


def test_new_fields_never_carry_secrets(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _handled(
            connection,
            (f"maxharris1/{GH_CLASSIC}", WEBHOOK_URL, "c1", f"queued-{GH_CLASSIC}"),
        )
        add_job(
            connection,
            "secret-job",
            state="done",
            repo=f"repo {WEBHOOK_URL}",
            ticket_id=GH_CLASSIC,
            handoff_comment_id=GH_CLASSIC,
        )
        _finish(connection, "secret-job", NOW - 5)
    out = tmp_path / "out"
    write_snapshot(out, collect_snapshot(db, log, now=NOW))
    text = (out / "status.json").read_text(encoding="utf-8") + (
        out / "index.html"
    ).read_text(encoding="utf-8")
    for name, secret in FREE_TEXT_SECRETS.items():
        assert secret not in text, name


# --- the page -------------------------------------------------------------------------


def _html(tmp_path: Path) -> str:
    return render_html(collect_snapshot(*seeded(tmp_path), now=NOW))


def _script(html: str) -> str:
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    return script


def _css(html: str) -> str:
    (css,) = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
    return css


def test_page_polls_status_json_every_one_to_two_seconds(tmp_path: Path) -> None:
    html = _html(tmp_path)
    script = _script(html)
    assert 1000 <= page.POLL_MS <= 2000
    assert f"var POLL_MS = {page.POLL_MS};" in script
    assert "var STATUS_URL = 'status.json';" in script
    # The only request the page makes is for the relative status.json.
    assert re.findall(r"fetch\(([^,)]+)", script) == ["STATUS_URL"]
    assert "cache: 'no-store'" in script
    assert "location.reload" in script  # falls back to reloading if polling is blocked


def test_page_keeps_a_no_script_fallback(tmp_path: Path) -> None:
    html = _html(tmp_path)
    assert '<noscript><meta http-equiv="refresh" content="20"></noscript>' in html
    # The server-rendered tables are still there, under the live view.
    assert 'id="static"' in html and "Running jobs" in html


def test_page_has_every_mission_control_panel(tmp_path: Path) -> None:
    html = _html(tmp_path)
    for panel in ("lanes", "queue", "handoffs", "verdicts", "health", "savings", "attention"):
        assert f'id="{panel}"' in html, panel


def test_page_has_no_external_assets_or_markup_sinks(tmp_path: Path) -> None:
    html = _html(tmp_path)
    for needle in ("<link", "src=", "http://", "https://", "@import", "url(", "//cdn"):
        assert needle not in html, needle
    script = _script(html)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script, sink


def test_page_respects_reduced_motion(tmp_path: Path) -> None:
    css = _css(_html(tmp_path))
    assert "@keyframes" in css  # animated by default
    block = css[css.index("@media (prefers-reduced-motion: reduce)") :]
    assert "animation: none !important" in block
    assert "transition: none !important" in block


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_palette_meets_wcag_aa_contrast(tmp_path: Path) -> None:
    css = _css(_html(tmp_path))
    palette = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", css))
    text = ["text", "muted", "accent", "ok", "warn", "bad", "claude", "codex", "cursor", "grok"]
    surfaces = ["bg", "panel", "panel-hi", "dim"]
    for name in text + surfaces:
        assert name in palette, name
    for fg in text:
        for bg in surfaces:
            ratio = _contrast(palette[fg], palette[bg])
            assert ratio >= 4.5, (fg, bg, round(ratio, 2))


def test_server_csp_lets_the_page_poll_its_own_status_json() -> None:
    source = (ROOT / "deploy" / "factory-dashboard-http.py").read_text(encoding="utf-8")
    assert "connect-src 'self'" in source
    assert "default-src 'none'" in source


# --- the page script, run against a tiny DOM -------------------------------------------

NODE_HARNESS = r"""
const fs = require('fs');
const [script, statusText] = [fs.readFileSync(process.argv[2], 'utf8'), fs.readFileSync(process.argv[3], 'utf8')];
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this._text = ''; this.attrs = {};
    this.className = ''; this.hidden = false; this.title = ''; this.style = {setProperty: (k, v) => { this.style[k] = v; }};
    const self = this;
    this.classList = {add: (...c) => { self.className = (self.className + ' ' + c.join(' ')).trim(); },
      remove: (...c) => { self.className = self.className.split(' ').filter(x => !c.includes(x)).join(' '); },
      toggle: (c, on) => { if (on === undefined) on = !self.className.split(' ').includes(c);
        if (on) self.classList.add(c); else self.classList.remove(c); return on; },
      contains: (c) => self.className.split(' ').includes(c)}; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...kids) { this.children = kids; this._text = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') ids[v] = this; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  dump(depth) { const pad = ' '.repeat(depth || 0);
    return pad + this.tagName + (this.className ? '.' + this.className.split(' ').join('.') : '') +
      (this._text ? ' "' + this._text + '"' : '') + '\n' + this.children.map(c => c.dump((depth || 0) + 1)).join(''); }
}
const ids = {};
const body = new Node('body');
body.attrs['data-generated'] = '1800000000.000';
body.attrs['data-refresh'] = '20';
for (const id of ['hud', 'static', 'link', 'link-text', 'age', 'clock', 'kpis', 'lanes', 'queue',
                  'handoffs', 'verdicts', 'health', 'savings', 'attention', 'tickets', 'log']) {
  const n = new Node('div'); n.attrs.id = id; ids[id] = n; body.appendChild(n);
}
ids.hud.hidden = true;
global.window = global;
global.document = {body, hidden: false, getElementById: (id) => ids[id] || null,
  createElement: (tag) => new Node(tag), addEventListener: () => {}};
global.matchMedia = () => ({matches: false});
global.location = {reload: () => { console.log('RELOAD'); }};
let fetches = 0;
global.fetch = (url, opts) => { fetches += 1;
  if (url !== 'status.json' || opts.cache !== 'no-store') throw new Error('bad fetch ' + url);
  return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve(JSON.parse(statusText))}); };
const realNow = Date.now; Date.now = () => 1800000003000;
eval(script);
setTimeout(() => {
  process.stdout.write(JSON.stringify({fetches, hudHidden: ids.hud.hidden, staticHidden: ids.static.hidden,
    text: Object.fromEntries(Object.entries(ids).map(([k, v]) => [k, v.textContent])),
    dump: ids.lanes.dump(0) + ids.queue.dump(0)}));
  process.exit(0);
}, 200);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_page_script_renders_a_live_snapshot(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _slots_table(connection)
        connection.execute(
            "INSERT INTO device_slots VALUES ('r-codex-review', 'pc', 'codex', 'codex-bot', ?)",
            (NOW - 300,),
        )
        _handled(connection, ("maxharris1/cleffy", "13", "c2", "queued-review-fix"))
        _finish(connection, "d-codex-review-1", NOW - 100)
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n"
        "  - {name: box, local: true, slot_max: 3, harnesses: [claude, codex, cursor]}\n"
        "  - {name: pc, slot_max: 2, harnesses: [codex]}\n"
        "  - {name: mac, enabled: false, slot_max: 2, harnesses: [claude]}\n",
        encoding="utf-8",
    )
    snapshot = collect_snapshot(db, log, now=NOW, machines_path=machines, last_cycle_at=NOW - 4)
    html = render_html(snapshot)
    (tmp_path / "page.js").write_text(_script(html), encoding="utf-8")
    (tmp_path / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (tmp_path / "harness.js").write_text(NODE_HARNESS, encoding="utf-8")
    result = subprocess.run(
        ["node", str(tmp_path / "harness.js"), str(tmp_path / "page.js"),
         str(tmp_path / "status.json")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["fetches"] >= 1
    assert out["hudHidden"] is False and out["staticHidden"] is True
    text = out["text"]
    assert "LIVE" in text["link-text"]
    lanes = text["lanes"]
    for needle in ("box", "pc", "mac", "DISABLED", "codex", "cleffy#13", "cursor", "cleffy#14",
                   "OVERDUE", "idle"):
        assert needle in lanes, (needle, out["dump"])
    assert ".device.disabled" in out["dump"]
    assert "grok" in text["queue"] and "cleffy#12" in text["queue"]
    assert "queued-review-fix" in text["verdicts"]
    assert "scripted" in text["handoffs"]
    assert "OK" in text["health"] and "LINEAR" in text["health"].upper()
    assert "50%" in text["savings"]
    assert "AUT-7" in text["attention"]
