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
from lib.dashboard_pages.ticket_detail import resolve_ticket
from tests import test_dashboard_layout as layout
from tests import test_dashboard_tickets_board as board
from tests.test_dashboard import NOW, add_job, add_ticket, make_db, seeded
from tests.test_dashboard_lanes import Browser, _page_with_status_stub, needs_chrome
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


def _relay_links(relay: str) -> dict[str, str]:
    return dict(re.findall(r'data-thread="([^"]*)"><a class="rl-tk" href="([^"]*)"', relay))


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
    (thread,) = page.relay_threads({"relay": {"threads": [threads["13"]]}})
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
        assert re.fullmatch(r'<a class="rl-tk" href="job\.html">[^<]*</a><span class="rl-steps">.*</span>', row), key
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
    assert buttons == [("true", "all", "all"), ("false", "scripted", "scripted"),
                       ("false", "wakes", "grok wakes"), ("false", "shipped", "shipped")]
    (legend,) = re.findall(r'<div class="rl-legend">(.*?)</div>', relay)
    keys = re.findall(r'<span class="rl-key"><span class="([^"]+)" aria-hidden="true">([^<]*)</span>'
                      r"<span>([^<]+)</span></span>", legend)
    assert keys == [("rl-sw", "", "handoff"), ("rl-sw b2b", "", "bot-to-bot (grok wake)"),
                    ("chip accent", "routed", "verdict"), ("chip bad", "failed", "failed handoff")]
    assert legend.count('class="rl-key"') == 4
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
    assert '<span class="count">1 of 9 tickets · newest activity first</span>' in snaps[2]


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
  var parts = [].filter.call(row.querySelectorAll('*'), function (n) { return !n.closest('.rl-tk'); });
  return {
    thread: row.getAttribute('data-thread'),
    outside: parts.filter(function (n) {
      var b = n.getBoundingClientRect();
      return b.left < tk.right || b.right > box.right + 0.5;
    }).map(function (n) { return n.className + ': ' + n.textContent; }),
    lines: new Set([].map.call(row.querySelectorAll('.rl-step, .rl-verdict'), function (n) {
      return Math.round(n.getBoundingClientRect().top);
    })).size
  };
})"""


@needs_chrome
def test_wrapped_relay_steps_start_in_the_second_column_in_a_browser(snapshot: dict) -> None:
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
        assert browser.eval(RELAY_FILTER)[1:] == ["all", "scripted", "grok wakes", "shipped"]
        threads = browser.eval("""(document.querySelector('[data-relay="wakes"]').click(),
          [].map.call(document.querySelectorAll('#relay .rl-row'), function (r) { return r.getAttribute('data-thread'); }))""")
        assert threads == ["maxharris1/factory#124"]
    finally:
        browser.close()
