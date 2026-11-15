from __future__ import annotations

import copy
import json
import re
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path

import pytest

import lib.dashboard_page as page
from lib.dashboard import collect_snapshot, render_pages
from tests import test_dashboard_layout as layout
from tests import test_dashboard_tickets_board as board
from tests.test_dashboard import NOW, add_job, seeded

IDS = layout.BAR + ["hud", "attention", "relay"]
PANELS = ["attention", "relay"]

HARNESS = (
    board.HARNESS.replace("const status = JSON.parse(statusText);", "let status = JSON.parse(statusText);")
    .replace(
        "const intervals = [];",
        "const intervals = [];\nconst realSetTimeout = setTimeout;\nconst polls = [];\n"
        "global.setTimeout = (fn, ms) => { if (ms === 1500) polls.push(fn); return 0; };",
    )
    .replace("function act(actions) {", "async function act(actions) {")
    .replace(
        "    } else if (kind === 'press') {",
        "    } else if (kind === 'poll') {\n"
        "      status = arg;\n"
        "      polls.splice(0).forEach(fn => fn());\n"
        "      await new Promise(resolve => realSetTimeout(resolve, 50));\n"
        "    } else if (kind === 'press') {",
    )
    .replace("setTimeout(() => {\n  act(JSON.parse(actionsText || '[]'));",
             "realSetTimeout(async () => {\n  await act(JSON.parse(actionsText || '[]'));")
)
STILL = HARNESS.replace("global.window = global;",
                        "global.window = global;\nglobal.matchMedia = q => ({matches: /reduce/.test(q)});")


def _handled(connection: sqlite3.Connection, *rows: tuple[str, str, str, str]) -> None:
    connection.executemany(
        "INSERT INTO handled_comments (repo, ticket_id, comment_id, outcome) VALUES (?, ?, ?, ?)", rows
    )


def _finish(connection: sqlite3.Connection, job_id: str, finished_at: float) -> None:
    connection.execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (finished_at, job_id))


def activity_snapshot(tmp_path: Path) -> dict:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _finish(connection, "d-claude-build-1", NOW - 400)
        _finish(connection, "d-codex-review-1", NOW - 100)
        _finish(connection, "f-claude-build", NOW - 250)
        _finish(connection, "g-grok-blocked", NOW - 50)
        add_job(connection, "x-mixed-build", repo="MaxHarris1/Cleffy", ticket_id="20", state="done",
                created_at=NOW - 700, handoff_comment_id="h-20")
        _finish(connection, "x-mixed-build", NOW - 300)
        _handled(
            connection,
            ("maxharris1/cleffy", "20", "comment-x-mixed-build", "queued-build"),
            ("maxharris1/cleffy", "12", "9002", "handed-off"),
            ("maxharris1/cleffy", "21", "c-none", "authorized-unstick-build"),
        )
    return collect_snapshot(db, log, now=NOW)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> dict:
    return activity_snapshot(tmp_path_factory.mktemp("activity"))


def _verdicts(snapshot: dict) -> dict[str, dict]:
    return {row["outcome"]: row for row in snapshot["poller"]["verdicts"]}


def test_a_verdict_carries_the_time_of_the_job_it_queued_or_posted(snapshot: dict) -> None:
    verdicts = _verdicts(snapshot)
    assert verdicts["queued-build"]["age_seconds"] == 700
    assert verdicts["handed-off"]["age_seconds"] == 100
    assert verdicts["authorized-unstick-build"]["age_seconds"] is None
    assert [row["seq"] for row in snapshot["poller"]["verdicts"]] == [3, 2, 1]
    assert "comment-x-mixed-build" not in json.dumps(snapshot["poller"])


def test_verdicts_on_a_database_without_jobs_have_no_time(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _handled(connection, ("maxharris1/cleffy", "12", "c1", "queued-review"),
                 ("maxharris1/cleffy", "12", "c2", "merged"))
        connection.execute("DROP TABLE jobs")
    snapshot = collect_snapshot(db, log, now=NOW)
    assert [(row["outcome"], row["age_seconds"]) for row in snapshot["poller"]["verdicts"]] == [
        ("merged", None), ("queued-review", None)]
    (thread,) = page.relay_threads(snapshot)
    assert [step["row"]["outcome"] for step in thread["steps"]] == ["queued-review", "merged"]
    relay = layout._section(render_pages(snapshot)["activity.html"], "relay")
    assert relay.index("queued-review") < relay.index("merged")
    assert '<span class="count">1 ticket</span>' in relay


def _keys(threads: list[dict]) -> Counter:
    return Counter(
        (step["type"], step["row"]["job"] if step["type"] == "handoff" else step["row"]["seq"])
        for thread in threads for step in thread["steps"]
    )


def test_every_handoff_and_verdict_is_in_exactly_one_thread(snapshot: dict) -> None:
    threads = page.relay_threads(snapshot)
    rows = Counter([("handoff", row["job"]) for row in snapshot["handoff_savings"]["recent"]]
                   + [("verdict", row["seq"]) for row in snapshot["poller"]["verdicts"]])
    assert _keys(threads) == rows and len(rows) == 8
    assert len({thread["key"] for thread in threads}) == len(threads)
    for thread in threads:
        assert {f"{str(s['row']['repo']).lower()}#{s['row']['ticket_id']}" for s in thread["steps"]} == {thread["key"]}


def test_mixed_case_repos_share_one_thread(snapshot: dict) -> None:
    threads = {thread["key"]: thread for thread in page.relay_threads(snapshot)}
    steps = threads["maxharris1/cleffy#20"]["steps"]
    assert [(s["type"], s["at"]) for s in steps] == [("verdict", 700), ("handoff", 300)]


def _handoff(job: str, ticket: str, age: int, kind: str = "scripted", recipient: str = "claude",
             action: str = "build") -> dict:
    return {"job": job, "repo": "maxharris1/factory", "ticket_id": ticket, "action": action, "recipient": recipient,
            "state": "failed" if kind == "failed" else "done", "kind": kind, "age_seconds": age}


def _verdict(seq: int, ticket: str, outcome: str, age: int | None, kind: str = "routed") -> dict:
    return {"repo": "maxharris1/factory", "ticket_id": ticket, "outcome": outcome, "kind": kind, "seq": seq,
            "age_seconds": age}


def _snap(handoffs: list[dict], verdicts: list[dict] | None) -> dict:
    return {"handoff_savings": {"recent": handoffs}, "poller": {"verdicts": verdicts}, "running": [], "queued": []}


def _order(thread: dict) -> list[str]:
    return [s["row"]["job"] if s["type"] == "handoff" else s["row"]["outcome"] for s in thread["steps"]]


def test_timed_steps_go_oldest_first_and_a_handoff_wins_a_tie() -> None:
    (thread,) = page.relay_threads(_snap(
        [_handoff("review", "1", 30, recipient="codex", action="review"), _handoff("build", "1", 90)],
        [_verdict(2, "1", "handed-off", 30, "shipped"), _verdict(1, "1", "queued-review", 90)],
    ))
    assert _order(thread) == ["build", "queued-review", "review", "handed-off"]


def test_two_verdicts_on_one_time_keep_handled_order() -> None:
    (thread,) = page.relay_threads(_snap([], [_verdict(7, "1", "second", 40), _verdict(3, "1", "first", 40)]))
    assert _order(thread) == ["first", "second"]


def test_an_untimed_verdict_follows_the_verdict_handled_before_it() -> None:
    (thread,) = page.relay_threads(_snap(
        [_handoff("build", "1", 200), _handoff("fix", "1", 20, action="fix")],
        [_verdict(1, "1", "authorized-unstick-build", None, "other"), _verdict(2, "1", "queued-review", 100),
         _verdict(3, "1", "approve-untimed", None, "other"), _verdict(4, "1", "also-untimed", None, "other")],
    ))
    assert _order(thread) == [
        "authorized-unstick-build", "build", "queued-review", "approve-untimed", "also-untimed", "fix"]


def test_threads_go_newest_first_and_untimed_threads_last() -> None:
    snap = _snap(
        [_handoff("old", "1", 900), _handoff("new", "2", 5), _handoff("mid", "3", 60)],
        [_verdict(1, "4", "first-untimed", None, "other"), _verdict(2, "5", "later-untimed", None, "other"),
         _verdict(3, "1", "queued-review", 30)],
    )
    threads = page.relay_threads(snap)
    assert [t["key"] for t in threads] == [
        "maxharris1/factory#2", "maxharris1/factory#1", "maxharris1/factory#3",
        "maxharris1/factory#5", "maxharris1/factory#4"]
    assert '<span class="count">5 tickets · last 15m 00s</span>' in page._relay(snap)
    assert '<span class="count">2 tickets</span>' in page._relay(_snap([], snap["poller"]["verdicts"][:2]))


def _inner(html: str, ident: str) -> str:
    outer = layout._section(layout._no_script(html), ident)
    return outer[outer.index(">") + 1 : outer.rindex("</")]


def _server(snapshot: dict) -> dict[str, str]:
    html = render_pages(snapshot)["activity.html"]
    return {ident: _inner(html, ident) for ident in PANELS}


def _script(tmp_path: Path, snapshot: dict, actions: list | None = None, harness: str = HARNESS) -> dict:
    html = render_pages(snapshot)["activity.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    (tmp_path / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (tmp_path / "harness.js").write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [layout.NODE, str(tmp_path / "harness.js"), str(tmp_path / "page.js"), str(tmp_path / "status.json"),
         "activity", "", json.dumps(IDS), json.dumps(actions or [])],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _rich(snapshot: dict) -> dict:
    rich = copy.deepcopy(snapshot)
    rich["handoff_savings"]["recent"] = [
        _handoff("j-rev114", "114", 4, recipient="codex", action="review"),
        _handoff("j-wake24", "124", 167, "bot_to_bot", "grok", "done"),
        _handoff("j-rev124", "124", 202, recipient="codex", action="review"),
        _handoff("j-bld124", "124", 382),
        _handoff("j-fix100", "100", 482, "failed", action="fix"),
        _handoff("j-bld114", "114", 904),
    ]
    rich["poller"]["verdicts"] = [
        _verdict(9, "124", "handed-off", 160, "shipped"),
        _verdict(8, "100", "queued-review", None),
        _verdict(7, "124", "queued-review", 382),
        _verdict(6, "107", "authorized-unstick-fix", None, "other"),
        _verdict(5, "124", "authorized-unstick-build", 600, "other"),
    ]
    return rich


@layout.needs_node
def test_script_relay_and_attention_match_the_server(tmp_path: Path, snapshot: dict) -> None:
    for case, snap in (("seeded", snapshot), ("rich", _rich(snapshot))):
        (tmp_path / case).mkdir()
        out = _script(tmp_path / case, snap)
        server = _server(snap)
        for ident in PANELS:
            assert out["html"][ident] == server[ident], (case, ident)


def test_relay_rows_follow_the_rule_and_dash_bot_to_bot_steps(snapshot: dict) -> None:
    relay = _server(_rich(snapshot))["relay"]
    rows = re.findall(r'<div class="rl-row" data-thread="([^"]+)">(.*?)</div>', relay)
    assert [key for key, _ in rows] == [
        "maxharris1/factory#114", "maxharris1/factory#124", "maxharris1/factory#100", "maxharris1/factory#107"]
    row124 = dict(rows)["maxharris1/factory#124"]
    assert re.findall(r'class="(rl-step[^"]*|rl-verdict)"', row124) == [
        "rl-verdict", "rl-step rl-claude", "rl-verdict", "rl-step rl-codex", "rl-step rl-grok b2b", "rl-verdict"]
    assert row124.count('<span class="rl-sep" aria-hidden="true">→</span>') == 5
    assert '<span class="chip ok">shipped</span><span class="rl-out">handed-off</span>' in row124
    assert '<span class="chip bad">failed</span>' in dict(rows)["maxharris1/factory#100"]
    assert '<span class="count">4 tickets · last 15m 04s</span>' in relay
    assert ".rl-step.b2b{border-style:dashed}" in page.CSS
    assert re.search(r"\.rl-step\{[^}]*border:1px solid", page.CSS)
    buttons = re.findall(r'<button class="rl-btn" type="button" aria-pressed="(true|false)" data-relay="(\w+)">'
                         r"([^<]+)</button>", relay)
    assert buttons == [("true", "all", "all"), ("false", "scripted", "scripted"),
                       ("false", "wakes", "grok wakes"), ("false", "shipped", "shipped")]
    for words in ("scripted handoff", "bot-to-bot (grok wake)", "verdict"):
        assert f"<span>{words}</span>" in relay


def _threads(markup: str) -> list[str]:
    return re.findall(r'data-thread="([^"]+)"', markup)


@layout.needs_node
def test_filter_buttons_show_only_the_matching_threads(tmp_path: Path, snapshot: dict) -> None:
    rich = _rich(snapshot)
    actions: list = []
    for name in ("scripted", "wakes", "shipped", "all"):
        actions += [["press", ["data-relay", name]], ["snap", ["relay"]]]
    snaps = [step["snap"]["relay"] for step in _script(tmp_path, rich, actions)["steps"] if "snap" in step]
    every = ["maxharris1/factory#114", "maxharris1/factory#124", "maxharris1/factory#100", "maxharris1/factory#107"]
    assert _threads(snaps[0]) == every[:2]
    assert _threads(snaps[1]) == ["maxharris1/factory#124"]
    assert _threads(snaps[2]) == ["maxharris1/factory#124"]
    assert _threads(snaps[3]) == every
    assert 'aria-pressed="true" data-relay="shipped"' in snaps[2]
    assert '<span class="count">1 ticket · last 10m 00s</span>' in snaps[2]


def _shipped(snapshot: dict, seq: int) -> dict:
    later = copy.deepcopy(snapshot)
    later["generated_at_epoch"] = snapshot["generated_at_epoch"] + 2
    later["poller"]["verdicts"].insert(0, _verdict(seq, "114", "merged", None, "shipped"))
    return later


@layout.needs_node
def test_a_new_shipped_verdict_plays_done_once_and_not_on_first_paint(tmp_path: Path, snapshot: dict) -> None:
    rich = _rich(snapshot)
    later = _shipped(rich, 10)
    again = copy.deepcopy(later)
    again["generated_at_epoch"] += 2
    actions = [["snap", ["relay"]], ["poll", later], ["snap", ["relay"]], ["poll", again], ["snap", ["relay"]]]
    snaps = [step["snap"]["relay"] for step in _script(tmp_path, rich, actions)["steps"] if "snap" in step]
    assert [markup.count('data-done="true"') for markup in snaps] == [0, 1, 0]
    row = re.search(r'data-thread="maxharris1/factory#114">.*?</div>', snaps[1]).group(0)
    assert 'class="rl-done" aria-hidden="true"' in row and 'data-tint="#9ece6a"' in row
    still = [step["snap"]["relay"] for step in _script(tmp_path, rich, actions, STILL)["steps"] if "snap" in step]
    assert [markup.count('data-done="true"') for markup in still] == [0, 0, 0]


def test_attention_rows_are_full_rows_with_a_static_red_block_and_a_link(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    snapshot = collect_snapshot(db, log, now=NOW)
    snapshot["attention"]["items"].append({"kind": "down", "repo": "", "ticket_id": "", "component": "waiter x",
                                           "link": "health.html#waiter-x", "detail": "down (never seen)"})
    snapshot["attention"]["count"] += 1
    attention = _server(snapshot)["attention"]
    rows = re.findall(r'<div class="at-row">(.*?)</a></div>', attention)
    assert len(rows) == snapshot["attention"]["count"]
    for row in rows:
        assert row.startswith('<span class="at-px" aria-hidden="true"><canvas class="px" data-px="static" '
                              'data-tint="#f7768e"')
        assert 'data-cell="4"' in row and "height:24px" in row
    down = next(row for row in rows if "waiter x" in row)
    assert '<span class="at-subj">waiter x</span><span class="at-what">down (never seen)</span>' in down
    assert down.endswith('<a class="at-go" href="health.html#waiter-x">open in System')
    running = next(row for row in rows if "cleffy#14" in row)
    assert running.endswith('<a class="at-go" href="job.html#job-r-cursor">open ticket')
    stuck = next(row for row in rows if "AUT-7" in row)
    assert stuck.endswith('<a class="at-go" href="job.html">open ticket')
