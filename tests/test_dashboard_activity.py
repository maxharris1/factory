from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import pytest

import lib.dashboard_page as page
from lib.dashboard import RELAY_STEPS, _VERDICT_TIMES, collect_snapshot, render_pages
from lib.dashboard_pages.common import duration
from lib.dashboard_pages.ticket_detail import resolve_ticket
from tests import test_dashboard_layout as layout
from tests import test_dashboard_tickets_board as board
from tests.test_dashboard import NOW, add_job, add_ticket, make_db, seeded
from tests.test_dashboard_lanes import Browser, _page_with_status_stub, needs_chrome
from tests.test_dashboard_pixelstrip import _node as _px_node
from tests.test_dashboard_ticket_records import walk
from tests.test_dispatcher import retry, seed_state, setup, ticket

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
    .replace("setTimeout(() => {\n  act(JSON.parse(actionsText));",
             "realSetTimeout(async () => {\n  await act(JSON.parse(actionsText));")
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
    (thread,) = [t for t in page.relay_threads(snapshot) if t["key"] == "maxharris1/cleffy#12"]
    assert [step["row"]["outcome"] for step in thread["steps"]] == ["queued-review", "merged"]
    assert [t["handoffs"] for t in snapshot["relay"]["threads"]] == [[]] * 4
    relay = layout._section(render_pages(snapshot)["activity.html"], "relay")
    assert relay.index('title="queued-review"') < relay.index('title="merged"')
    assert '<span class="count">4 of 4 tickets · newest activity first</span>' in relay


def test_a_verdict_carries_the_approval_time_recorded_for_its_comment(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, gate_count, stuck, approved_head, "
            "approved_at, approve_comment_id) VALUES ('maxharris1/cleffy', '42', 0, 0, 'h', ?, 'a-42')",
            (NOW - 30,))
        connection.execute(
            "INSERT INTO ship_holds (repo, ticket_id, head, approver, reasons, approve_comment_id, "
            "held_at) VALUES ('MaxHarris1/Cleffy', '44', 'h', 'codex-bot', 'r', 'a-44', ?)", (NOW - 60,))
        _handled(connection, ("maxharris1/cleffy", "42", "a-42", "waiting-external"),
                 ("maxharris1/cleffy", "44", "a-44", "held"),
                 ("maxharris1/cleffy", "43", "a-43", "waiting-external"))
    verdicts = {row["ticket_id"]: row["age_seconds"]
                for row in collect_snapshot(db, log, now=NOW)["poller"]["verdicts"]}
    assert verdicts == {"42": 30, "44": 60, "43": None}

    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE ship_holds")
        connection.execute("ALTER TABLE ticket_state DROP COLUMN approve_comment_id")
    verdicts = {row["ticket_id"]: row["age_seconds"]
                for row in collect_snapshot(db, log, now=NOW)["poller"]["verdicts"]}
    assert verdicts == {"42": None, "44": None, "43": None}


def test_a_retry_with_no_job_carries_the_time_the_dispatcher_recorded(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, now=[NOW - 5])
    seed_state(queue, state="waiting-max", stuck=1, stuck_reason="t7-stuck")
    with queue.connect() as connection:
        add_job(connection, "old-handoff", repo="owner/repo", ticket_id="42", state="done",
                handoff_comment_id="old-ready")
        _finish(connection, "old-handoff", NOW - 100)
    dispatcher.process(ticket(retry("retry-42")))
    with queue.connect() as connection:
        assert not connection.execute(
            "SELECT 1 FROM jobs WHERE source_comment_id = 'retry-42'").fetchall()

    snapshot = collect_snapshot(tmp_path / "d.db", tmp_path / "poller.log", now=NOW)
    (verdict,) = [v for v in snapshot["poller"]["verdicts"] if v["outcome"] == "retried-no-job"]
    assert verdict["age_seconds"] == 5
    (thread,) = [t for t in page.relay_threads(snapshot) if t["ref"]["ticket_id"] == "42"]
    assert [(step["type"], step["at"]) for step in thread["steps"]] == [
        ("handoff", 100), ("verdict", 5)]


def test_a_verdict_carries_the_time_its_follow_up_filing_recorded(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO followup_filings (repo, ticket_id, comment_id, round, title, body, labels, "
            "status, created_at) VALUES ('maxharris1/cleffy', '42', 'v-42', 1, 't', 'b', '[]', "
            "'pending', ?)", (NOW - 40,))
        _handled(connection, ("maxharris1/cleffy", "42", "v-42", "queued-ship"))
    verdicts = {row["ticket_id"]: row["age_seconds"]
                for row in collect_snapshot(db, log, now=NOW)["poller"]["verdicts"]}
    assert verdicts == {"42": 40}

    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE followup_filings")
    verdicts = {row["ticket_id"]: row["age_seconds"]
                for row in collect_snapshot(db, log, now=NOW)["poller"]["verdicts"]}
    assert verdicts == {"42": None}


def test_a_verdict_carries_the_time_its_auto_extra_round_recorded(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO auto_extra_comments (repo, ticket_id, comment_id, body, status, "
            "created_at) VALUES ('maxharris1/cleffy', '42', 'v-42', 'b', 'pending', ?)",
            (NOW - 30,))
        _handled(connection, ("maxharris1/cleffy", "42", "v-42", "queued-fix"))
    verdicts = {row["ticket_id"]: row["age_seconds"]
                for row in collect_snapshot(db, log, now=NOW)["poller"]["verdicts"]}
    assert verdicts == {"42": 30}


def test_every_table_that_times_a_comment_feeds_the_verdict_time(tmp_path: Path) -> None:
    db, _ = seeded(tmp_path)
    timed = set()
    with sqlite3.connect(db) as connection:
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            if any(c.endswith("comment_id") for c in columns) and any(
                    c.endswith("_at") for c in columns):
                timed.add(table)
    assert timed == {table for table, *_ in _VERDICT_TIMES}


def _moved(tmp_path: Path, count: int, stopped_seconds_ago: dict[int, int] | None = None) -> dict:
    stopped = stopped_seconds_ago or {}
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        for n in range(1, count + 1):
            add_ticket(connection, "owner/repo", str(n), state="stuck" if n in stopped else "building",
                       stuck=int(n in stopped))
            add_job(connection, f"j-{n}", repo="owner/repo", ticket_id=str(n), state="done",
                    created_at=NOW - 60 * n - 30)
            _finish(connection, f"j-{n}", NOW - 60 * n)
            if n in stopped:
                connection.execute(
                    "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
                    "VALUES ('owner/repo', ?, 'building', 'stuck', ?, 'test')", (str(n), NOW - stopped[n]))
    log = tmp_path / "poller.log"
    log.write_text("")
    return collect_snapshot(db, log, now=NOW)


def _relay_ids(snapshot: dict) -> list[int]:
    return [int(thread["ticket_id"]) for thread in snapshot["relay"]["threads"]]


def test_the_relay_holds_the_twelve_tickets_that_moved_last_newest_first(tmp_path: Path) -> None:
    snapshot = _moved(tmp_path, 15)
    assert _relay_ids(snapshot) == list(range(1, 13))
    assert snapshot["relay"]["tracked"] == 15
    relay = _server(snapshot)["relay"]
    assert '<span class="count">12 of 15 tickets · newest activity first</span>' in relay
    assert relay.endswith('<p class="rl-note">The relay shows the 12 tickets that moved most recently. '
                          "A stopped ticket stays listed until it moves again.</p>")
    assert _relay_links(relay)["owner/repo#1"] == "job.html"


def test_stopped_tickets_keep_their_places_and_the_oldest_movers_drop_out(tmp_path: Path) -> None:
    snapshot = _moved(tmp_path, 15, {13: 5, 14: 5, 15: 5})
    assert _relay_ids(snapshot) == [13, 14, 15, *range(1, 10)]


def test_past_twelve_stopped_tickets_the_most_recently_stopped_stay(tmp_path: Path) -> None:
    snapshot = _moved(tmp_path, 15, {n: 15 - n for n in range(1, 15)})
    assert _relay_ids(snapshot) == list(range(14, 2, -1))


def test_past_twelve_stuck_wakes_the_most_recently_stuck_stay(tmp_path: Path) -> None:
    clock = [NOW - 100]
    dispatcher, queue = setup(tmp_path, now=clock)
    with queue.connect() as connection:
        for n in range(1, 16):
            add_ticket(connection, "owner/repo", str(n))
    for n in range(1, 16):
        clock[0] = NOW - 100 + n
        dispatcher._mark_stuck_and_wake(repo="owner/repo", ticket_id=str(n), source_comment_id=f"s-{n}",
                                         timeout_seconds=600, reason="job-timeout", detail="job-timeout")
    snapshot = collect_snapshot(tmp_path / "d.db", tmp_path / "poller.log", now=NOW)
    assert _relay_ids(snapshot) == list(range(15, 3, -1))


def test_a_retried_stuck_wake_keeps_its_ticket_stop_time(tmp_path: Path) -> None:
    clock = [NOW - 100]
    dispatcher, queue = setup(tmp_path, now=clock)
    with queue.connect() as connection:
        for n in range(1, 16):
            add_ticket(connection, "owner/repo", str(n))
    for n in range(1, 16):
        clock[0] = NOW - 100 + n
        dispatcher._mark_stuck_and_wake(repo="owner/repo", ticket_id=str(n), source_comment_id=f"s-{n}",
                                         timeout_seconds=600, reason="job-timeout", detail="job-timeout")
    with queue.connect() as connection:
        wake = connection.execute(
            "SELECT stuck_wake_job_id FROM ticket_state WHERE ticket_id = '1'").fetchone()[0]
        connection.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (wake,))
    clock[0] = NOW - 1
    dispatcher._ensure_all_stuck_wakes()
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT stuck_wake_job_id FROM ticket_state WHERE ticket_id = '1'").fetchone()[0] != wake
    snapshot = collect_snapshot(tmp_path / "d.db", tmp_path / "poller.log", now=NOW)
    assert _relay_ids(snapshot) == list(range(15, 3, -1))


def test_a_merge_with_no_job_moves_its_ticket_to_the_top(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, now=[NOW - 1])
    with queue.connect() as connection:
        for n in range(1, 16):
            add_ticket(connection, "owner/repo", str(n))
            add_job(connection, f"j-{n}", repo="owner/repo", ticket_id=str(n), state="done",
                    created_at=NOW - 60 * n - 30)
            _finish(connection, f"j-{n}", NOW - 60 * n)
    dispatcher.record_terminal("owner/repo", "15", "merged")
    snapshot = collect_snapshot(tmp_path / "d.db", tmp_path / "poller.log", now=NOW)
    assert _relay_ids(snapshot) == [15, *range(1, 12)]


def test_each_thread_says_where_its_ticket_stopped_shipped_or_waits(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "maxharris1/cleffy", "90", state="stuck", stuck=1, stuck_reason="gaps-twice")
        walk(connection, "90", ["intake", "planning", "stuck"], NOW - 266600)
        add_ticket(connection, "maxharris1/cleffy", "70", state="merged")
        walk(connection, "70", ["intake", "building", "review", "ship", "merged"], NOW - 3000)
        add_ticket(connection, "maxharris1/cleffy", "71", state="handed-off")
        walk(connection, "71", ["intake", "building", "ship", "handed-off"], NOW - 1000)
        add_ticket(connection, "maxharris1/cleffy", "72", state="awaiting-ok")
        walk(connection, "72", ["intake", "building", "awaiting-ok"], NOW - 3800)
        connection.execute(
            "INSERT INTO ship_holds (repo, ticket_id, head, approver, reasons, approve_comment_id, held_at) "
            "VALUES ('maxharris1/cleffy', '72', 'h', 'max', 'r', 'a-72', ?)", (NOW - 3600,))
    threads = {t["ticket_id"]: t for t in collect_snapshot(db, log, now=NOW)["relay"]["threads"]}
    assert threads["90"]["now"] == {"kind": "stopped", "state": "stuck", "stopped_in": "planning",
                                    "age_seconds": 266400}
    assert threads["70"]["now"] == {"kind": "shipped", "how": "merged", "age_seconds": 2600}
    assert threads["71"]["now"] == {"kind": "shipped", "how": "handed-off", "age_seconds": 700}
    assert threads["72"]["now"] == {"kind": "idle", "state": "awaiting-ok", "age_seconds": 3600,
                                    "waiting_on": "max"}
    assert threads["13"]["now"]["waiting_on"] is None
    assert threads["14"]["now"] is None


def _relay_links(relay: str) -> dict[str, str]:
    return dict(re.findall(r'data-thread="([^"]*)"><span class="rl-id"><a class="rl-tk" href="([^"]*)"', relay))


def _assert_relay_links_open(snapshot: dict, relay: str) -> None:
    links = _relay_links(relay)
    assert len(links) == len(snapshot["relay"]["threads"])
    assert links["aut0-code/aut0#AUT-7"] == "job.html"
    assert links["maxharris1/cleffy#20"] == "job.html"
    assert links["maxharris1/cleffy#13"] == "ticket.html#t=maxharris1%2Fcleffy%2313"
    for key, href in links.items():
        if href != "job.html":
            address = urllib.parse.unquote(href.removeprefix("ticket.html#t="))
            assert href.startswith("ticket.html#t=") and resolve_ticket(snapshot, address) is not None, key


def test_a_relay_ticket_opens_its_ticket_page_only_while_a_job_is_live(snapshot: dict) -> None:
    _assert_relay_links_open(snapshot, _server(snapshot)["relay"])


@layout.needs_node
def test_the_script_links_relay_tickets_like_the_server(tmp_path: Path, snapshot: dict) -> None:
    _assert_relay_links_open(snapshot, _script(tmp_path, snapshot)["html"]["relay"])


def test_a_thread_carries_every_step_up_to_the_cap(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        for ticket_id, count in (("12", RELAY_STEPS + 2), ("13", 15)):
            for n in range(count):
                add_job(connection, f"h-{ticket_id}-{n:02}", ticket_id=ticket_id, state="done", created_at=NOW - 5000 + n)
                _finish(connection, f"h-{ticket_id}-{n:02}", NOW - 5000 + n)
        _handled(connection, *[("maxharris1/cleffy", "13", f"v-{n}", "queued-review") for n in range(15)])
    threads = {t["ticket_id"]: t for t in collect_snapshot(db, log, now=NOW)["relay"]["threads"]}
    assert [row["job"] for row in threads["12"]["handoffs"]] == [f"h-12-{n:02}" for n in range(RELAY_STEPS + 1, 1, -1)]
    assert len(threads["13"]["handoffs"]) == 15 and len(threads["13"]["verdicts"]) == 15
    (thread,) = page.relay_threads({"relay": {"threads": [threads["13"]]}, "running": [], "queued": []})
    assert len(thread["steps"]) == 30


WORDS = {
    "queued-review": ("routed", "accent", "sent to review"),
    "queued-plan": ("routed", "accent", "sent to plan"),
    "queued-plan-build": ("routed", "accent", "plan accepted, sent to build"),
    "merge-queue": ("routed", "accent", "approved, queued to merge"),
    "resumed-fix": ("routed", "accent", "resumed the fix"),
    "handed-off": ("shipped", "ok", "handed off"),
    "merged": ("shipped", "ok", "merged"),
    "authorized-unstick-build": ("unstuck", "accent", "Max unstuck it"),
    "authorized-unstick-fix": ("unstuck", "accent", "Max unstuck it"),
    "waiting-max-repeat": ("waiting", "warn", "still waiting on Max"),
    "approve-ci-red": ("blocked", "bad", "approved, CI red"),
}
UNMAPPED = "ship-weird-thing"


@pytest.fixture(scope="module")
def worded(tmp_path_factory) -> dict:
    tmp_path = tmp_path_factory.mktemp("worded")
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "owner/repo", "7")
        _handled(connection, *[("owner/repo", "7", f"c-{outcome}", outcome) for outcome in [*WORDS, UNMAPPED]])
    log = tmp_path / "poller.log"
    log.write_text("")
    return collect_snapshot(db, log, now=NOW)


def _said(relay: str, outcome: str) -> str:
    return re.search(rf'<span class="rl-verdict" title="{outcome}">((?:<span[^>]*>[^<]*</span>)*)</span>', relay).group(1)


@pytest.mark.parametrize("outcome", list(WORDS))
def test_a_verdict_reads_as_one_chip_and_plain_words(worded: dict, outcome: str) -> None:
    chip, tone, words = WORDS[outcome]
    assert _said(_server(worded)["relay"], outcome) == f'<span class="chip {tone}">{chip}</span><span class="rl-out">{words}</span>'


def test_an_unmapped_verdict_shows_its_raw_text_once_with_a_muted_chip(worded: dict) -> None:
    relay = _server(worded)["relay"]
    assert _said(relay, UNMAPPED) == f'<span class="chip muted">blocked</span><span class="rl-out">{UNMAPPED}</span>'
    assert re.sub(r' title="[^"]*"', "", relay).count(UNMAPPED) == 1


@layout.needs_node
def test_the_script_draws_the_window_and_the_words_like_the_server(tmp_path: Path, worded: dict) -> None:
    window = _moved(tmp_path / "db", 15, {13: 5})
    for case, snap in (("window", window), ("worded", worded)):
        assert _script(tmp_path / case, snap)["html"]["relay"] == _server(snap)["relay"], case
    relay = _server(window)["relay"]
    assert '<span class="count">12 of 15 tickets · newest activity first</span>' in relay
    assert 'class="rl-note"' in relay


def test_a_new_approval_leads_the_relay_ahead_of_an_older_handoff(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_job(connection, "o-claude-build-43", ticket_id="43", state="done", handoff_comment_id="r-43")
        _finish(connection, "o-claude-build-43", NOW - 20)
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, gate_count, stuck, approved_head, "
            "approved_at, approve_comment_id) VALUES ('maxharris1/cleffy', '42', 0, 0, 'h', ?, 'a-42')",
            (NOW - 5,))
        _handled(connection, ("maxharris1/cleffy", "42", "a-42", "waiting-external"))
    order = [t["ref"]["ticket_id"] for t in page.relay_threads(collect_snapshot(db, log, now=NOW))]
    assert order.index("42") < order.index("43")


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
        assert {f"{str(s['row']['repo']).lower()}#{s['row']['ticket_id']}" for s in thread["steps"]} <= {thread["key"]}


def test_mixed_case_repos_share_one_thread(snapshot: dict) -> None:
    threads = {thread["key"]: thread for thread in page.relay_threads(snapshot)}
    steps = threads["maxharris1/cleffy#20"]["steps"]
    assert [(s["type"], s["at"]) for s in steps] == [("verdict", 700), ("handoff", 300)]


def _handoff(job: str, ticket: str, age: int, kind: str = "scripted", recipient: str = "claude",
             action: str = "build") -> dict:
    return {"job": job, "repo": "maxharris1/factory", "ticket_id": ticket, "action": action, "recipient": recipient,
            "state": "failed" if kind == "failed" else "done", "kind": kind, "age_seconds": age}


def _verdict(seq: int, ticket: str, outcome: str, age: int | None, kind: str = "routed") -> dict:
    chip, _, words = WORDS.get(outcome, (None, None, None))
    return {"repo": "maxharris1/factory", "ticket_id": ticket, "outcome": outcome, "kind": kind, "seq": seq,
            "age_seconds": age, "chip": chip, "words": words}


def _threaded(handoffs: list[dict], verdicts: list[dict]) -> list[dict]:
    threads: dict[str, dict] = {}
    for name, rows in (("handoffs", handoffs), ("verdicts", verdicts)):
        for row in rows:
            threads.setdefault(row["ticket_id"], {"repo": row["repo"], "ticket_id": row["ticket_id"],
                                                  "handoffs": [], "verdicts": []})[name].append(row)
    return list(threads.values())


def _snap(handoffs: list[dict], verdicts: list[dict], tracked: int | None = None) -> dict:
    threads = _threaded(handoffs, verdicts)
    return {"relay": {"tracked": len(threads) if tracked is None else tracked, "threads": threads},
            "running": [], "queued": []}


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


def test_threads_keep_the_snapshot_order_and_the_count_reads_shown_of_tracked() -> None:
    snap = _snap(
        [_handoff("old", "1", 900), _handoff("new", "2", 5)],
        [_verdict(1, "4", "first-untimed", None, "other"), _verdict(3, "1", "queued-review", 30)],
        tracked=222,
    )
    assert [t["key"] for t in page.relay_threads(snap)] == [
        "maxharris1/factory#1", "maxharris1/factory#2", "maxharris1/factory#4"]
    assert '<span class="count">3 of 222 tickets · newest activity first</span>' in page._relay(snap)


def _inner(html: str, ident: str) -> str:
    outer = layout._section(layout._no_script(html), ident)
    return outer[outer.index(">") + 1 : outer.rindex("</")]


def _server(snapshot: dict) -> dict[str, str]:
    html = render_pages(snapshot)["activity.html"]
    return {ident: _inner(html, ident) for ident in PANELS}


def _script(tmp_path: Path, snapshot: dict, actions: list | None = None, harness: str = HARNESS) -> dict:
    html = render_pages(snapshot)["activity.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    return layout.run_harness(tmp_path, harness, script, snapshot, "activity", IDS, actions or [])


def _rich(snapshot: dict, handoffs: tuple | list = (), verdicts: tuple | list = ()) -> dict:
    rich = copy.deepcopy(snapshot)
    rich["relay"] = {"tracked": 9, "threads": _threaded([
        _handoff("j-rev114", "114", 4, recipient="codex", action="review"),
        _handoff("j-wake24", "124", 167, "bot_to_bot", "grok", "done"),
        _handoff("j-rev124", "124", 202, recipient="codex", action="review"),
        _handoff("j-bld124", "124", 382),
        _handoff("j-fix100", "100", 482, "failed", action="fix"),
        _handoff("j-bld114", "114", 904),
        *handoffs,
    ], [
        *verdicts,
        _verdict(10, "100", "ship-weird-thing", 300, "blocked"),
        _verdict(9, "124", "handed-off", 160, "shipped"),
        _verdict(8, "100", "queued-review", None),
        _verdict(7, "124", "queued-review", 382),
        _verdict(6, "107", "authorized-unstick-fix", None, "other"),
        _verdict(5, "124", "authorized-unstick-build", 600, "other"),
    ])}
    return rich


@layout.needs_node
def test_script_relay_and_attention_match_the_server(tmp_path: Path, snapshot: dict) -> None:
    assert {page._ATTENTION[row["kind"]] for row in snapshot["attention"]["items"]} == {"warn", "bad"}
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
    for key, row in rows:
        assert re.fullmatch(r'<span class="rl-id"><a class="rl-tk" href="job\.html">[^<]*</a>'
                            r'(<button class="rl-more rl-more-phone" [^>]*>\+\d+ earlier</button>)?</span>'
                            r'<span class="rl-steps">.*</span>', row), key
    row124 = dict(rows)["maxharris1/factory#124"]
    assert re.findall(r'class="(rl-step [^"]*|rl-verdict)"', row124) == [
        "rl-verdict", "rl-step rl-claude", "rl-verdict", "rl-step rl-codex", "rl-step rl-grok b2b", "rl-verdict"]
    assert row124.count('<span class="rl-sep" aria-hidden="true">→</span>') == 5
    assert ('<span class="rl-verdict" title="handed-off"><span class="chip ok">shipped</span>'
            '<span class="rl-out">handed off</span>') in row124
    assert '<span class="chip bad">failed</span>' in dict(rows)["maxharris1/factory#100"]
    assert '<span class="count">4 of 9 tickets · newest activity first</span>' in relay
    assert ".rl-step.b2b{border-style:dashed}" in page.CSS
    assert re.search(r"\.rl-step\{[^}]*border:1px solid", page.CSS)
    buttons = re.findall(r'<button class="rl-btn" type="button" aria-pressed="(true|false)" data-relay="(\w+)">'
                         r"([^<]+)</button>", relay)
    assert buttons == [("true", "all", "all"), ("false", "needs", "needs me"),
                       ("false", "shipped", "shipped"), ("false", "wakes", "grok wakes")]
    assert '<div class="rl-filter" role="group" aria-label="Filter the relay"><button class="rl-btn"' in relay
    (legend,) = re.findall(r'<div class="rl-legend">(.*?)</div>', relay)
    keys = re.findall(r'<span class="rl-key"><span class="([^"]+)" aria-hidden="true">([^<]*)</span>'
                      r"<span>([^<]+)</span></span>", legend)
    assert keys == [("rl-sw", "", "handoff"), ("rl-sw b2b", "", "bot-to-bot (grok wake)"),
                    ("chip accent", "routed", "verdict"), ("chip bad", "failed", "failed handoff"),
                    ("rl-sw now", "", "where the ticket is now")]
    assert legend.count('class="rl-key"') == 5
    assert legend.endswith('</span></span><a class="rl-find" href="job.html">find any of the 9 tickets</a>')
    for drawn in ('class="rl-step rl-claude"', 'class="rl-step rl-grok b2b"',
                  'class="rl-verdict" title="queued-review"><span class="chip accent">routed</span>',
                  '<span class="chip bad">failed</span>'):
        assert drawn in relay


def test_a_verdict_shows_its_age_like_a_handoff_and_no_time_when_it_has_none() -> None:
    relay = page._relay(_snap([_handoff("build", "1", 400)],
                              [_verdict(2, "1", "queued-review", 380), _verdict(1, "1", "authorized-unstick-build", None)]))
    assert ('<span class="rl-verdict" title="queued-review"><span class="chip accent">routed</span>'
            '<span class="rl-out">sent to review</span><span class="rl-when">6m 20s ago</span></span>') in relay
    assert '<span class="rl-what">build</span><span class="rl-when">6m 40s ago</span>' in relay
    assert ('<span class="rl-verdict" title="authorized-unstick-build"><span class="chip accent">unstuck</span>'
            '<span class="rl-out">Max unstuck it</span></span>') in relay
    assert relay.count('class="rl-when"') == 2


def _threads(markup: str) -> list[str]:
    return re.findall(r'data-thread="([^"]+)"', markup)


def _tickets(markup: str) -> list[str]:
    return [key.removeprefix("maxharris1/factory#") for key in _threads(markup)]


@layout.needs_node
def test_filter_buttons_show_only_the_matching_threads(tmp_path: Path, snapshot: dict) -> None:
    snap = _endings(snapshot)
    snap["relay"]["threads"][8]["handoffs"].append(_handoff("j-wake209", "209", 500, "bot_to_bot", "grok", "done"))
    actions: list = []
    for name in ("needs", "shipped", "wakes", "all"):
        actions += [["press", ["data-relay", name]], ["snap", ["relay"]]]
    snaps = [step["snap"]["relay"] for step in _script(tmp_path, snap, actions)["steps"] if "snap" in step]
    assert _tickets(snaps[0]) == ["203", "204", "206", "210"]
    assert _tickets(snaps[1]) == ["207", "208"]
    assert _tickets(snaps[2]) == ["209"]
    assert _tickets(snaps[3]) == [str(n) for n in range(201, 212)]
    assert 'aria-pressed="true" data-relay="needs"' in snaps[0]
    assert '<span class="count">4 of 11 tickets · newest activity first</span>' in snaps[0]
    assert snaps[3] == _server(snap)["relay"]
    flags = {t["key"].split("#")[1]: (t["needs"], t["shipped"], t["wakes"]) for t in page.relay_threads(snap)}
    assert [ticket for ticket, flag in flags.items() if flag[0]] == ["203", "204", "206", "210"]
    assert [ticket for ticket, flag in flags.items() if flag[1]] == ["207", "208"]
    assert [ticket for ticket, flag in flags.items() if flag[2]] == ["209"]


def _shipped(snapshot: dict, seq: int) -> dict:
    later = copy.deepcopy(snapshot)
    later["generated_at_epoch"] = snapshot["generated_at_epoch"] + 2
    thread = next(t for t in later["relay"]["threads"] if t["ticket_id"] == "114")
    thread["verdicts"].insert(0, _verdict(seq, "114", "merged", None, "shipped"))
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
    assert 'data-done="true" data-settle="true"' in row
    still = [step["snap"]["relay"] for step in _script(tmp_path, rich, actions, STILL)["steps"] if "snap" in step]
    assert [markup.count('data-done="true"') for markup in still] == [0, 0, 0]


@layout.needs_node
def test_each_new_shipped_verdict_on_one_ticket_gets_its_own_settle(tmp_path: Path, snapshot: dict) -> None:
    rich = _rich(snapshot)
    first = _shipped(rich, 10)
    second = _shipped(first, 11)
    actions = [["poll", first], ["snap", ["relay"]], ["poll", second], ["snap", ["relay"]]]
    snaps = [step["snap"]["relay"] for step in _script(tmp_path, rich, actions)["steps"] if "snap" in step]
    keys = [re.findall(r'data-px-key="(relay-[^"]+)"', markup) for markup in snaps]
    assert keys == [["relay-maxharris1/factory#114:10"], ["relay-maxharris1/factory#114:11"]]


def test_attention_rows_are_full_rows_with_a_still_block_in_the_chip_tone_and_a_link(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    snapshot = collect_snapshot(db, log, now=NOW)
    snapshot["attention"]["items"].append({"kind": "down", "repo": "", "ticket_id": "", "component": "waiter x",
                                           "link": "health.html#waiter-x", "detail": "down (never seen)"})
    snapshot["attention"]["count"] += 1
    attention = _server(snapshot)["attention"]
    rows = re.findall(r'<div class="at-row" data-attn="[^"]+">(.*?)</div>', attention)
    assert len(rows) == snapshot["attention"]["count"]
    palette = {tone: re.search(rf"--{tone}:(#[0-9a-f]{{6}})", page.CSS).group(1) for tone in ("warn", "bad")}
    tones = []
    for row in rows:
        (tone,) = re.findall(r'<span class="chip (\w+)">', row)
        tones.append(tone)
        assert row.startswith('<span class="at-px" aria-hidden="true"><canvas class="px" data-px="static" '
                              f'data-tint="{palette[tone]}"')
        assert 'data-cell="4"' in row and "height:24px" in row
    assert sorted(set(tones)) == ["bad", "warn"]
    down = next(row for row in rows if "waiter x" in row)
    assert '<span class="at-subj">waiter x</span><span class="at-what">down (never seen)</span>' in down
    assert down.endswith('<a class="at-go" href="health.html#waiter-x">open in System</a>')
    running = next(row for row in rows if "cleffy#14" in row)
    assert running.endswith('<a class="at-go" href="job.html#job-r-cursor">open ticket</a>')
    stuck = next(row for row in rows if "AUT-7" in row)
    assert stuck.endswith('<a class="at-go" href="job.html">open ticket</a>')


@layout.needs_node
def test_a_relay_filter_keeps_focus_when_pressed(tmp_path: Path, snapshot: dict) -> None:
    actions = [["focus", ["data-relay", "shipped"]], ["press", ["data-relay", "shipped"]], ["focused", "data-relay"]]
    steps = _script(tmp_path, _rich(snapshot), actions)["steps"]
    assert [step["focused"] for step in steps if "focused" in step] == [["button", "shipped"]]


@layout.needs_node
def test_attention_ticket_rows_link_github_in_both_renderers(tmp_path: Path, snapshot: dict) -> None:
    snap = copy.deepcopy(snapshot)
    item = next(row for row in snap["attention"]["items"] if not row.get("component"))
    item.update(repo="maxharris1/cleffy", source="#14", pr="15")
    server = _server(snap)["attention"]
    links = re.findall(r'href="(https://github\.com/[^"]*)" rel="noopener noreferrer" target="_blank"', server)
    issue = links.index("https://github.com/maxharris1/cleffy/issues/14")
    assert links[issue + 1] == "https://github.com/maxharris1/cleffy/pull/15"
    assert _script(tmp_path, snap)["html"]["attention"] == server


def _only(snapshot: dict, handoffs: list[dict], verdicts: list[dict] | None = None) -> dict:
    snap = copy.deepcopy(snapshot)
    snap["relay"] = _snap(handoffs, verdicts or [])["relay"]
    return snap


def _steps(count: int, ticket: str = "1") -> list[dict]:
    return [_handoff(f"j-{ticket}-{n:02}", ticket, 1000 - 10 * n) for n in range(count)]


def _ages(count: int) -> list[str]:
    return [duration(1000 - 10 * n) + " ago" for n in range(count)]


def _row(relay: str, ticket: str = "1") -> str:
    return re.search(rf'data-thread="maxharris1/factory#{ticket}">(.*?)</div>', relay).group(1)


def _early(row: str) -> list[str]:
    return re.findall(r'<span class="rl-early">(.*?<span class="rl-sep" aria-hidden="true">→</span>)</span>', row)


def _shown(row: str) -> list[str]:
    return re.findall(r'class="rl-when">([^<]*)<', re.sub(r'<span class="rl-early">.*?→</span></span>', "", row))


@layout.needs_node
@pytest.mark.parametrize(("count", "hidden"), [(6, 0), (7, 1), (9, 3)])
def test_a_thread_shows_its_six_newest_steps_and_folds_the_rest_behind_a_button(
    tmp_path: Path, snapshot: dict, count: int, hidden: int,
) -> None:
    snap = _only(snapshot, _steps(count))
    relay = _server(snap)["relay"]
    assert _script(tmp_path, snap)["html"]["relay"] == relay
    row = _row(relay)
    assert len(_early(row)) == hidden == row.count('class="rl-early"')
    assert _shown(row) == _ages(count)[hidden:] and len(_shown(row)) == 6
    if not hidden:
        assert 'class="rl-more"' not in row
        return
    noun = "step" if hidden == 1 else "steps"
    assert row.split('<span class="rl-steps">')[1].startswith(
        '<button class="rl-more" type="button" aria-expanded="false" data-more="maxharris1/factory#1" '
        f'title="show {hidden} earlier {noun}">+{hidden} earlier</button><span class="rl-early">')


def _open(relay: str, ticket: str) -> bool | None:
    row = _row(relay, ticket)
    found = re.findall(r'class="rl-more" type="button" aria-expanded="(true|false)"', row)
    assert len(found) == 1
    assert (found[0] == "true") == ("rl-early" not in row)
    return found[0] == "true"


@layout.needs_node
def test_the_earlier_button_opens_one_thread_in_place_and_it_stays_open_across_polls(
    tmp_path: Path, snapshot: dict,
) -> None:
    snap = _only(snapshot, [*_steps(9, "1"), *_steps(8, "2")])
    later = copy.deepcopy(snap)
    later["generated_at_epoch"] += 2
    later["relay"]["threads"][0]["handoffs"].insert(0, _handoff("j-1-99", "1", 5))
    press = ["press", ["data-more", "maxharris1/factory#1"]]
    actions = [press, ["snap", ["relay"]], ["poll", later], ["snap", ["relay"]], press, ["snap", ["relay"]]]
    snaps = [step["snap"]["relay"] for step in _script(tmp_path, snap, actions)["steps"] if "snap" in step]
    assert [(_open(relay, "1"), _open(relay, "2")) for relay in snaps] == [
        (True, False), (True, False), (False, False)]
    assert "+4 earlier" in snaps[1] and "+4 earlier" in snaps[2]
    assert snaps[2] == _server(later)["relay"]


@layout.needs_node
def test_the_earlier_button_keeps_focus_when_pressed(tmp_path: Path, snapshot: dict) -> None:
    key = "maxharris1/factory#1"
    actions = [["focus", ["data-more", key]], ["press", ["data-more", key]], ["focused", "data-more"],
               ["press", ["data-more", key]], ["focused", "data-more"]]
    steps = _script(tmp_path, _only(snapshot, _steps(9)), actions)["steps"]
    assert [step["focused"] for step in steps if "focused" in step] == [["button", key]] * 2


RELAY_WINDOWED = """[].map.call(document.querySelectorAll('#relay .rl-row'), function (row) {
  function seen(n) { var b = n.getBoundingClientRect(); return b.width > 0 && b.height > 0; }
  return {
    shown: [].filter.call(row.querySelectorAll('.rl-step'), seen).map(function (n) {
      return n.querySelector('.rl-when').textContent; }),
    early: [].map.call(row.querySelectorAll('.rl-early, .rl-early *'), seen),
    more: [].filter.call(row.querySelectorAll('.rl-more'), seen).map(function (n) { return n.textContent; })
  };
})"""


@needs_chrome
def test_the_earlier_steps_fold_only_with_script_in_a_browser(snapshot: dict) -> None:
    snap = _only(snapshot, _steps(9))
    ages = _ages(9)
    browser = Browser()
    try:
        _load(browser, snap, 1440, True)
        (row,) = browser.eval(RELAY_WINDOWED)
        assert row["shown"] == ages[3:] and row["more"] == ["+3 earlier"]
        assert row["early"] and not any(row["early"])
        _load(browser, snap, 1440, False)
        (row,) = browser.eval(RELAY_WINDOWED)
        assert row["shown"] == ages and row["more"] == []
    finally:
        browser.close()


def _job(job: str, ticket: str, action: str = "build", recipient: str = "claude", **row: object) -> dict:
    return {"job": job, "repo": "maxharris1/factory", "ticket_id": ticket, "action": action, "recipient": recipient,
            "source": None, "pr": None, "round": None, "timeout_seconds": 5400, "overdue": False, "stuck": False,
            "tracker": "github", "device": "box", "elapsed_from": "queued", "heartbeat_at": None, "tier": None,
            "telemetry": None, **row}


def _endings(snapshot: dict) -> dict:
    stopped = {"kind": "stopped", "state": "stuck", "stopped_in": "planning", "age_seconds": 266400}
    shipped = {"kind": "shipped", "how": "merged", "age_seconds": 7200}
    rests = {"206": stopped, "207": shipped, "208": shipped,
             "209": {"kind": "idle", "state": "review", "age_seconds": 3600, "waiting_on": None},
             "210": {"kind": "idle", "state": "awaiting-ok", "age_seconds": 3600, "waiting_on": "max"},
             "201": stopped, "205": {"kind": "idle", "state": "review", "age_seconds": 50, "waiting_on": None}}
    snap = _only(snapshot, [_handoff(f"j-{n}", str(n), 400) for n in range(201, 212)],
                 [_verdict(1, "207", "merged", 300, "shipped")])
    for thread in snap["relay"]["threads"]:
        thread["now"] = rests.get(thread["ticket_id"])
    snap["running"] = [
        _job("old-201", "201", job_state="stale", elapsed_seconds=900, heartbeat_age_seconds=600),
        _job("run-201", "201", job_state="running", elapsed_seconds=372, heartbeat_age_seconds=5),
        _job("run-202", "202", "review", "codex", job_state="running", elapsed_seconds=220, heartbeat_age_seconds=5),
        _job("run-203", "203", "update-from-main", job_state="stale", elapsed_seconds=900, heartbeat_age_seconds=250),
        _job("run-204", "204", "update-from-main", "cursor", job_state="dead", elapsed_seconds=900, heartbeat_age_seconds=900),
    ]
    snap["queued"] = [_job("q-205", "205", "update-from-main", "codex", age_seconds=90),
                      _job("q-205b", "205", age_seconds=30)]
    return snap


def _now(relay: str, ticket: str) -> str:
    row = _row(relay, ticket)
    return row[row.index('<span class="rl-now'):] if "rl-now" in row else ""


NOW_TITLE = 'title="where the ticket is now"'
ENDINGS = {
    "201": ['class="rl-now rl-claude live" ' + NOW_TITLE, 'data-px="fill"', 'data-px-key="relay-now-run-201"',
            '<span class="rl-px" aria-hidden="true"><canvas', '<span class="rl-run">running 6m 12s</span>'],
    "202": ['class="rl-now rl-codex live" ' + NOW_TITLE, 'data-px="scan"', '<span class="rl-run">running 3m 40s</span>'],
    "203": ['class="rl-now stale" ' + NOW_TITLE, '<span class="rl-blk rl-claude">', 'data-px="decay"',
            'data-still="true"', '<span class="chip warn">stale</span>',
            '<span class="rl-late">no heartbeat for 4m 10s</span>'],
    "204": ['<span class="rl-blk rl-cursor">', 'data-px="decay"', 'data-still="true"', '<span class="chip bad">dead</span>',
            '<span class="rl-late">no heartbeat for 15m 00s</span>'],
    "205": ['class="rl-now queued rl-codex" ' + NOW_TITLE, '<span class="rl-bot">codex</span><span class="rl-what">update-from-main</span>',
            '<span class="rl-wait">queued 1m 30s</span>'],
    "206": [NOW_TITLE + '><span class="chip bad">stuck</span><span class="rl-say">in planning for 3d 2h</span></span>'],
    "208": [NOW_TITLE + '><span class="chip ok">shipped</span><span class="rl-out">merged</span>'
            '<span class="rl-when">2h 00m ago</span></span>'],
    "209": [NOW_TITLE + '><span class="rl-when">in review for 1h 00m</span></span>'],
    "210": [NOW_TITLE + '><span class="chip warn">waiting</span><span class="rl-say">on max for 1h 00m</span></span>'],
}


@layout.needs_node
def test_each_thread_ends_on_where_its_ticket_is_now_in_both_renders(tmp_path: Path, snapshot: dict) -> None:
    snap = _endings(snapshot)
    relay = _server(snap)["relay"]
    assert _script(tmp_path, snap)["html"]["relay"] == relay
    for ticket, parts in ENDINGS.items():
        ending = _now(relay, ticket)
        assert _row(relay, ticket).endswith('<span class="rl-sep" aria-hidden="true">→</span>' + ending), ticket
        for part in parts:
            assert part in ending, (ticket, part)
        assert ending.count("rl-now") == 1 and ending.count("<canvas") == (1 if ticket < "205" else 0), ticket
        if ticket in ("201", "202"):
            assert "data-still" not in ending
        else:
            assert re.findall(r"<canvas [^>]*>", ending) == re.findall(r'<canvas [^>]*data-still="true"[^>]*>', ending)
    assert "rl-now" not in _row(relay, "207")
    assert _row(relay, "207").endswith('<span class="rl-out">merged</span><span class="rl-when">5m 00s ago</span></span></span>')
    assert "rl-now" not in _row(relay, "211")


@layout.needs_node
def test_the_ending_counts_toward_the_six(tmp_path: Path, snapshot: dict) -> None:
    for steps, button in ((5, None), (6, "+1 earlier")):
        snap = _only(snapshot, _steps(steps))
        snap["queued"] = [_job("q-1", "1", age_seconds=90)]
        relay = _server(snap)["relay"]
        assert _script(tmp_path / str(steps), snap)["html"]["relay"] == relay
        row = _row(relay)
        assert row.endswith('<span class="rl-wait">queued 1m 30s</span></span></span>')
        assert re.findall(r'class="rl-more" [^>]*>(\+\d+ earlier)</button>', row) == ([button] if button else [])


@layout.needs_node
def test_the_running_ending_is_a_pixelstrip_that_holds_still_under_reduced_motion(
    tmp_path: Path, snapshot: dict,
) -> None:
    snap = _endings(snapshot)
    relay = _server(snap)["relay"]
    assert _script(tmp_path / "still", snap, harness=STILL)["html"]["relay"] == relay
    (canvas,) = re.findall(r"<canvas ([^>]*)>", _now(relay, "201"))
    attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', canvas))
    scenario = (f"const el = canvas({json.dumps(attrs)}, 40, 10); found = [el]; PX.bind();"
                "for (let t = 50; t <= 3000; t += 50) frame(t); out.draws = draws(el);")
    assert _px_node(tmp_path, scenario, reduced=False)["draws"] > 1
    assert _px_node(tmp_path, scenario, reduced=True)["draws"] == 1


def _long(snapshot: dict) -> dict:
    return _rich(snapshot, [
        _handoff("j-l1", "131", 3000), _handoff("j-l2", "131", 2800, recipient="codex", action="review"),
        _handoff("j-l3", "131", 2600, "failed", action="fix"), _handoff("j-l4", "131", 2400, "bot_to_bot", "grok", "done"),
        _handoff("j-l5", "131", 2200, action="fix"), _handoff("j-l6", "131", 2000, recipient="codex", action="review"),
    ], [
        _verdict(19, "131", "handed-off", 1900, "shipped"), _verdict(18, "131", "authorized-unstick-build", None, "other"),
        _verdict(17, "131", "queued-review", 2500), _verdict(16, "131", "queued-fix", 2700)])


def _load(browser: Browser, snapshot: dict, width: int, script: bool) -> None:
    browser.call("Emulation.setScriptExecutionDisabled", value=not script)
    if script:
        browser.open(_page_with_status_stub(snapshot, "activity.html"), width)
        return
    bare = Path(browser.tmp.name) / f"bare-{width}.html"
    bare.write_text(render_pages(snapshot)["activity.html"], encoding="utf-8")
    browser.call("Emulation.setDeviceMetricsOverride", width=width, height=900, deviceScaleFactor=1, mobile=False)
    browser.call("Page.navigate", url=bare.as_uri())
    deadline = time.monotonic() + 30
    while not browser.eval(f"location.href === {json.dumps(bare.as_uri())} && document.readyState === 'complete' "
                           "&& document.fonts.status === 'loaded'"):
        assert time.monotonic() < deadline, f"{bare.name} never loaded"
        time.sleep(0.05)


RELAY_ROWS = """[].map.call(document.querySelectorAll('#relay .rl-row'), function (row) {
  var box = row.getBoundingClientRect(), tk = row.querySelector('.rl-tk').getBoundingClientRect();
  var block = innerWidth < 700;
  var parts = [].filter.call(row.querySelectorAll('*'), function (n) {
    return !n.closest('.rl-id') && n.getClientRects().length > 0;
  });
  return {
    thread: row.getAttribute('data-thread'),
    outside: parts.filter(function (n) {
      var b = n.getBoundingClientRect();
      var start = block ? b.top < tk.bottom || b.left < box.left - 0.5 : b.left < tk.right;
      return start || b.right > box.right + 0.5;
    }).map(function (n) { return n.className + ': ' + n.textContent; }),
    lines: new Set(parts.filter(function (n) { return n.matches('.rl-step, .rl-verdict'); }).map(function (n) {
      return Math.round(n.getBoundingClientRect().top);
    })).size
  };
})"""


@needs_chrome
def test_wrapped_relay_steps_start_in_the_second_column_or_below_the_id_on_a_phone(snapshot: dict) -> None:
    long = _long(snapshot)
    browser = Browser()
    try:
        for script in (True, False):
            for width in (1440, 375):
                _load(browser, long, width, script)
                rows = {row["thread"]: row for row in browser.eval(RELAY_ROWS)}
                assert len(rows) == 5, (script, width)
                assert rows["maxharris1/factory#131"]["lines"] > 1, (script, width)
                for row in rows.values():
                    assert not row["outside"], (script, width, row)
    finally:
        browser.close()


RELAY_FILTER = """[].filter.call(document.querySelectorAll('#relay .rl-filter, #relay .rl-btn'), function (n) {
  return n.getClientRects().length > 0;
}).map(function (n) { return n.textContent; })"""


@needs_chrome
def test_the_relay_filter_shows_only_with_script_in_a_browser(snapshot: dict) -> None:
    rich = _rich(snapshot)
    browser = Browser()
    try:
        _load(browser, rich, 1440, False)
        assert browser.eval("document.querySelectorAll('#relay .rl-filter .rl-btn').length") == 4
        assert browser.eval(RELAY_FILTER) == []
        _load(browser, rich, 1440, True)
        assert browser.eval(RELAY_FILTER)[1:] == ["all", "needs me", "shipped", "grok wakes"]
        threads = browser.eval("""(document.querySelector('[data-relay="wakes"]').click(),
          [].map.call(document.querySelectorAll('#relay .rl-row'), function (r) { return r.getAttribute('data-thread'); }))""")
        assert threads == ["maxharris1/factory#124"]
    finally:
        browser.close()


@needs_chrome
def test_every_ending_stays_inside_its_row_in_a_browser(snapshot: dict) -> None:
    snap = _endings(snapshot)
    browser = Browser()
    try:
        for script in (True, False):
            for width in (1440, 375):
                _load(browser, snap, width, script)
                rows = browser.eval(RELAY_ROWS)
                assert len(rows) == 11, (script, width)
                for row in rows:
                    assert not row["outside"], (script, width, row)
    finally:
        browser.close()


FILTER_BOX = """[].map.call(document.querySelectorAll('#relay .rl-btn'), function (n) {
  var b = n.getBoundingClientRect();
  return [n.textContent, Math.round(b.top), b.height];
})"""


@needs_chrome
def test_the_relay_filter_is_one_row_of_28px_buttons_at_every_width_in_a_browser(snapshot: dict) -> None:
    browser = Browser()
    try:
        for width in (1440, 375):
            _load(browser, _rich(snapshot), width, True)
            buttons = browser.eval(FILTER_BOX)
            assert [text for text, _, _ in buttons] == ["all", "needs me", "shipped", "grok wakes"], width
            assert len({top for _, top, _ in buttons}) == 1, (width, buttons)
            assert {height for _, _, height in buttons} == {28}, (width, buttons)
            assert browser.eval("document.documentElement.scrollWidth") <= width
    finally:
        browser.close()


ATTENTION = [("paused", "1"), ("stuck", "2"), ("stale", "3"), ("down", "4")]


def _attending(snapshot: dict) -> dict:
    snap = copy.deepcopy(snapshot)
    snap["attention"] = {"count": len(ATTENTION), "items": [
        {"kind": kind, "repo": "maxharris1/factory", "ticket_id": ticket, "detail": f"{kind} detail"}
        for kind, ticket in ATTENTION]}
    return snap


@layout.needs_node
def test_needs_attention_lists_bad_items_first_in_both_renders(tmp_path: Path, snapshot: dict) -> None:
    snap = _attending(snapshot)
    server = _server(snap)["attention"]
    assert _script(tmp_path, snap)["html"]["attention"] == server
    assert re.findall(r'data-attn="(\w+):', server) == ["stuck", "down", "paused", "stale"]
    assert '<span class="count">4 items · worst first</span>' in server
    assert '<section class="panel" id="attention"' in render_pages(snap)["activity.html"]


@needs_chrome
def test_the_attention_panel_keeps_its_id_at_every_width_in_a_browser(snapshot: dict) -> None:
    browser = Browser()
    try:
        for width in (1440, 375):
            _load(browser, _attending(snapshot), width, True)
            box = browser.eval("(function (b) { return [b.width, b.height]; })"
                               "(document.getElementById('attention').getBoundingClientRect())")
            assert box[0] > 0 and box[1] > 0, width
    finally:
        browser.close()


def _phone_more(row: str) -> list[str]:
    return re.findall(r'<button class="rl-more rl-more-phone" type="button" aria-expanded="(?:true|false)" '
                      r'data-more="[^"]+" title="show \d+ earlier steps?">(\+\d+ earlier)</button></span>', row)


@layout.needs_node
@pytest.mark.parametrize(("count", "desktop", "phone"), [(9, "+3 earlier", "+5 earlier"), (5, None, "+1 earlier"),
                                                         (4, None, None)])
def test_a_phone_thread_folds_all_but_its_four_newest_steps(
    tmp_path: Path, snapshot: dict, count: int, desktop: str | None, phone: str | None,
) -> None:
    snap = _only(snapshot, _steps(count))
    relay = _server(snap)["relay"]
    assert _script(tmp_path, snap)["html"]["relay"] == relay
    row = _row(relay)
    assert re.findall(r'class="rl-more" [^>]*>(\+\d+ earlier)</button>', row) == ([desktop] if desktop else [])
    assert _phone_more(row) == ([phone] if phone else [])
    folded = re.findall(r'<span class="rl-early-phone">.*?class="rl-when">([^<]*)<', row)
    assert folded == _ages(count)[max(count - 6, 0):max(count - 4, 0)]
    tail = re.sub(r'<span class="rl-early(?:-phone)?">.*?→</span></span>', "", row)
    assert re.findall(r'class="rl-when">([^<]*)<', tail) == _ages(count)[-4:]


PHONE_ROWS = """[].map.call(document.querySelectorAll('#relay .rl-row'), function (row) {
  function seen(n) { return n.getClientRects().length > 0; }
  var box = row.getBoundingClientRect();
  var tk = row.querySelector('.rl-tk').getBoundingClientRect();
  var more = [].filter.call(row.querySelectorAll('.rl-more'), seen);
  var items = [].filter.call(row.querySelectorAll('.rl-step, .rl-verdict, .rl-now'), seen);
  return {
    more: more.map(function (n) {
      var b = n.getBoundingClientRect();
      return {text: n.textContent, line: Math.abs(b.top + b.bottom - tk.top - tk.bottom) < 2,
              right: b.right - box.right,
              height: b.height, expanded: n.getAttribute('aria-expanded')};
    }),
    tops: items.map(function (n) { return n.getBoundingClientRect().top; }),
    whens: items.map(function (n) { var w = n.querySelector('.rl-when, .rl-wait'); return w && w.textContent; }),
    last: items.length ? items[items.length - 1].className : null,
    expanded: [].map.call(row.querySelectorAll('.rl-more'), function (n) { return n.getAttribute('aria-expanded'); })
  };
})"""


@needs_chrome
def test_a_phone_thread_is_a_block_of_four_steps_newest_last_in_a_browser(snapshot: dict) -> None:
    snap = _only(snapshot, _steps(8))
    snap["queued"] = [_job("q-1", "1", age_seconds=90)]
    ages = _ages(8)
    browser = Browser()
    try:
        _load(browser, snap, 375, True)
        (row,) = browser.eval(PHONE_ROWS)
        ((more,),) = [row["more"]]
        assert more["text"] == "+5 earlier" and more["line"] and abs(more["right"]) < 0.5 and more["height"] == 28
        assert len(row["tops"]) == 4 and row["tops"] == sorted(set(row["tops"]))
        assert row["whens"] == [*ages[5:], "queued 1m 30s"]
        assert row["last"].startswith("rl-now queued")
        assert browser.eval("document.documentElement.scrollWidth") <= 375
        browser.eval("document.querySelector('#relay .rl-more-phone').click()")
        (row,) = browser.eval(PHONE_ROWS)
        assert row["whens"] == [*ages, "queued 1m 30s"]
        assert row["tops"] == sorted(set(row["tops"]))
        assert row["expanded"] == ["true", "true"]
        _load(browser, snap, 375, False)
        (row,) = browser.eval(PHONE_ROWS)
        assert row["more"] == [] and row["whens"] == [*ages, "queued 1m 30s"]
    finally:
        browser.close()
