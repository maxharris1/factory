"""Explicit priority tiers and critical-path order (issue #258).

A project's `intake.priority_labels` (or, opted in, Linear priority) puts a
ticket in a tier; the feeder and the claim order rank by tier first, then
urgent, a resumed step, bug, how many tickets it unblocks, and age.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot, render_pages
from lib.feeder import ensure_tables, feeder_items
from lib.orch_yml import load_orch_yml
from lib.queue import JobRequest, SQLiteJobQueue, claim_order, priority_marks, set_ticket_priority
from lib.status import SQLiteStatusReader, build_status, render
from tests.test_feeder import (
    GITHUB_ORCH,
    LINEAR_FACTORY_MD,
    LINEAR_ORCH,
    FakeLinear,
    dispatcher_for,
    issue,
    linear_issue,
    make_feeder,
    orch_config,
    rows,
)
from tests.test_dashboard_layout import _no_script, _run_page, _section, needs_node
from tests.test_feeder_gates import HOME, MERGED, OPEN, OPEN_PULL, fed_heads, ref, states

TIERS = "      priority_labels: [priority:p0, priority:p1, priority:p2, priority:p3]\n"
READY = "factory:ready"


def _with(orch: str, intake_lines: str) -> str:
    anchor = "      label: factory:ready\n" if "label: factory:ready" in orch else "      linear_state: In Progress\n"
    return orch.replace(anchor, anchor + intake_lines)


def _refused(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "orch.yml"
    path.write_text(body, encoding="utf-8")
    config, findings = load_orch_yml(path, allow_placeholders=True)
    assert config is None
    return [item.message for item in findings]


def test_intake_reads_priority_labels_and_the_two_switches(tmp_path: Path) -> None:
    intake = orch_config(tmp_path, _with(GITHUB_ORCH, TIERS + "      critical_path_order: false\n")).projects[0].intake
    assert intake.priority_labels == ("priority:p0", "priority:p1", "priority:p2", "priority:p3")
    assert intake.priority_from_linear is False
    assert intake.critical_path_order is False
    linear = orch_config(tmp_path, _with(LINEAR_ORCH, "      priority_from_linear: true\n")).projects[0].intake
    assert linear.priority_from_linear is True


def test_intake_without_the_new_keys_keeps_todays_order(tmp_path: Path) -> None:
    intake = orch_config(tmp_path, GITHUB_ORCH).projects[0].intake
    assert intake.priority_labels == ()
    assert intake.priority_from_linear is False
    assert intake.critical_path_order is True


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ("      priority_labels: priority:p0\n", "priority_labels must be a list"),
        ("      priority_labels: [priority:p0, '']\n", "priority_labels must be a list"),
        ("      priority_labels: [p0, P0]\n", "priority_labels names p0 twice"),
        ("      critical_path_order: maybe\n", "critical_path_order must be true or false"),
        ("      priority_from_linear: true\n", "priority_from_linear requires linear_project_id"),
    ],
)
def test_bad_priority_config_is_refused(tmp_path: Path, lines: str, message: str) -> None:
    messages = _refused(tmp_path, _with(GITHUB_ORCH, lines))
    assert any(message in item for item in messages), messages


def test_priority_from_linear_must_be_a_bool(tmp_path: Path) -> None:
    messages = _refused(tmp_path, _with(LINEAR_ORCH, "      priority_from_linear: 1\n"))
    assert any("priority_from_linear must be true or false" in item for item in messages), messages


def _claim_tickets(queue: SQLiteJobQueue) -> list[str]:
    with queue.connect() as connection:
        return [candidate.ticket_id for candidate in claim_order(connection, "claude", now=0.0)]


def _enqueue(queue: SQLiteJobQueue, now: list[float], *tickets: str) -> None:
    for ticket in tickets:
        now[0] += 1
        queue.enqueue(JobRequest(repo=HOME, ticket_id=ticket, action="build", recipient="claude",
                                 source_comment_id=ticket, timeout_seconds=600), dedupe_key=ticket)


def test_a_newer_p0_ticket_takes_a_freed_path_before_an_older_urgent_p1_one(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=3)
    holder = issue(1, created_at=1.0, body="Touches: lib/x.py")
    github.issues[HOME] = [holder]
    github.statuses = {ref(501): OPEN_PULL}
    feeder.feed_once()
    github.issues[HOME] = [
        holder,
        issue(2, labels=(READY, "urgent", "priority:p1"), created_at=2.0, body="Touches: lib/x.py"),
        issue(3, labels=(READY, "priority:p0"), created_at=3.0, body="Touches: lib/x.py"),
    ]
    assert feeder.feed_once().fed == ()
    github.statuses[ref(501)] = MERGED
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-3"]
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/x.py held by #3")


def test_relabeling_a_queued_ticket_changes_its_claim_order_on_the_next_poll(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=2, now=now)
    github.issues[HOME] = [issue(1, labels=(READY, "priority:p1"), created_at=1.0),
                           issue(2, labels=(READY, "priority:p0"), created_at=2.0)]
    feeder.feed_once()
    pull = {key: row["ticket_id"] for key, row in rows(tmp_path / "orch.db").items()}
    _enqueue(queue, now, pull["1"], pull["2"])
    assert _claim_tickets(queue) == [pull["2"], pull["1"]]
    github.issues[HOME] = [issue(1, labels=(READY, "Priority:P0"), created_at=1.0),
                           issue(2, labels=(READY, "priority:p1"), created_at=2.0)]
    now[0] += 1
    feeder.feed_once()
    assert _claim_tickets(queue) == [pull["1"], pull["2"]]


def _claim_actions(tmp_path: Path, *jobs: tuple[str, int]) -> list[str]:
    """Queue one job per (action, tier) on its own ticket, oldest first."""
    now = [10_000.0]
    _, queue, _, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), now=now)
    for number, (action, tier) in enumerate(jobs, start=1):
        now[0] += 1
        queue.enqueue(JobRequest(repo=HOME, ticket_id=str(number), action=action, recipient="claude",
                                 source_comment_id=str(number), timeout_seconds=600), dedupe_key=str(number))
        with queue.connect() as connection:
            set_ticket_priority(connection, HOME, str(number), urgent=False, bug=False, at=now[0], tier=tier)
    with queue.connect() as connection:
        return [candidate.action for candidate in claim_order(connection, "claude", now=now[0])]


@pytest.mark.parametrize("finishing", ["review", "fix", "update-from-main", "qa"])
def test_inside_a_tier_finishing_a_ticket_beats_starting_an_older_one(tmp_path: Path, finishing: str) -> None:
    assert _claim_actions(tmp_path, ("plan", 2), ("build", 2), (finishing, 2)) == [finishing, "plan", "build"]


def test_a_better_tier_build_still_beats_a_worse_tier_fix(tmp_path: Path) -> None:
    assert _claim_actions(tmp_path, ("fix", 2), ("build", 0), ("plan", 3)) == ["build", "fix", "plan"]


@pytest.mark.parametrize(("opt_in", "first"), [(False, "AUT-1"), (True, "AUT-2")])
def test_linear_priority_sets_the_tier_only_when_the_project_opts_in(
    tmp_path: Path, opt_in: bool, first: str
) -> None:
    linear = FakeLinear()
    linear.issues = [linear_issue("AUT-1", priority=4, created_at=1.0),
                     linear_issue("AUT-2", priority=2, created_at=2.0)]
    orch = _with(LINEAR_ORCH, "      priority_from_linear: true\n") if opt_in else LINEAR_ORCH
    feeder, _, _, _ = make_feeder(tmp_path, orch=orch, box=1, linear=linear,
                                  factory_text=LINEAR_FACTORY_MD)
    assert [item.ticket_id for item in feeder.feed_once().fed] == [first]


def _tiers(queue: SQLiteJobQueue) -> dict[str, tuple[object, object]]:
    with queue.connect() as connection:
        return {str(item["issue"]): (item["tier"], item["own_tier"]) for item in feeder_items(connection)}


def test_an_urgent_ticket_with_no_tier_ranks_as_tier_zero_in_the_feeder_and_the_claim_order(
    tmp_path: Path,
) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=4, now=now)
    github.issues[HOME] = [
        issue(1, created_at=1.0),
        issue(2, labels=(READY, "priority:p1"), created_at=2.0),
        issue(3, labels=(READY, "priority:p0"), created_at=3.0),
        issue(4, labels=(READY, "urgent"), created_at=4.0),
    ]
    order = ["factory/issue-4", "factory/issue-3", "factory/issue-2", "factory/issue-1"]
    assert fed_heads(github, feeder.feed_once()) == order
    assert _tiers(queue) == {"4": (0, 0), "3": (0, 0), "2": (1, 1), "1": (None, None)}
    pull = {key: row["ticket_id"] for key, row in rows(tmp_path / "orch.db").items()}
    _enqueue(queue, now, pull["1"], pull["2"], pull["3"], pull["4"])
    assert _claim_tickets(queue) == [pull["4"], pull["3"], pull["2"], pull["1"]]


def test_an_explicit_tier_beats_the_urgent_label(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=2)
    github.issues[HOME] = [issue(1, labels=(READY, "urgent", "priority:p2"), created_at=1.0),
                           issue(2, labels=(READY, "priority:p1"), created_at=2.0)]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2", "factory/issue-1"]
    assert _tiers(queue)["1"] == (2, 2)


def test_linear_urgent_priority_is_tier_zero_without_priority_from_linear(tmp_path: Path) -> None:
    linear = FakeLinear()
    linear.issues = [linear_issue("AUT-1", priority=2, created_at=1.0),
                     linear_issue("AUT-2", priority=1, created_at=2.0)]
    feeder, queue, _, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, box=1, linear=linear,
                                      factory_text=LINEAR_FACTORY_MD)
    assert [item.ticket_id for item in feeder.feed_once().fed] == ["AUT-2"]
    assert _tiers(queue) == {"AUT-2": (0, 0), "AUT-1": (None, None)}


def test_a_blocker_takes_the_best_tier_of_the_tickets_it_gates(tmp_path: Path) -> None:
    no_critical_path = TIERS + "      critical_path_order: false\n"
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, no_critical_path), box=1)
    github.statuses = {ref(2): OPEN}
    github.issues[HOME] = [
        issue(1, labels=(READY, "priority:p1"), created_at=1.0),
        issue(2, labels=(READY, "priority:p1"), created_at=2.0),
        issue(3, labels=(READY, "priority:p0"), created_at=3.0, body="Blocked by: #2"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]
    assert _tiers(queue) == {"2": (0, 1), "3": (0, 0), "1": (1, 1)}
    pull = rows(tmp_path / "orch.db")["2"]["ticket_id"]
    with queue.connect() as connection:
        mark = priority_marks(connection)[(HOME, pull)]
    assert (mark.tier, mark.own_tier) == (0, 1)


def test_a_blocker_cycle_ends_and_both_take_the_better_tier(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=2)
    github.statuses = {ref(1): OPEN, ref(2): OPEN}
    github.issues[HOME] = [
        issue(1, labels=(READY, "priority:p2"), created_at=1.0, body="Blocked by #2"),
        issue(2, labels=(READY, "priority:p0"), created_at=2.0, body="Blocked by #1"),
    ]
    assert feeder.feed_once().fed == ()
    assert _tiers(queue) == {"1": (0, 2), "2": (0, 0)}



def _feed_chain(tmp_path: Path, lines: str):
    """A lone ticket #1, then a chain: #3 is blocked by #2, #4 by #3."""
    tmp_path.mkdir()
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, lines), box=1)
    github.statuses = {ref(2): OPEN, ref(3): OPEN}
    github.issues[HOME] = [
        issue(1, created_at=1.0),
        issue(2, created_at=2.0),
        issue(3, created_at=3.0, body="Blocked by #2"),
        issue(4, created_at=4.0, body="Blocked by #3"),
    ]
    fed = fed_heads(github, feeder.feed_once())
    with queue.connect() as connection:
        order = [(item["issue"], item["unblocks"]) for item in feeder_items(connection)]
    return fed, order, github


def test_inside_a_tier_the_ticket_that_unblocks_most_goes_first(tmp_path: Path) -> None:
    fed, order, github = _feed_chain(tmp_path / "on", "")
    assert fed == ["factory/issue-2"]
    assert order == [("2", 2), ("3", 1), ("1", 0), ("4", 0)]
    fed, order, without = _feed_chain(tmp_path / "off", "      critical_path_order: false\n")
    assert fed == ["factory/issue-1"]
    assert order == [("1", 0), ("2", 0), ("3", 0), ("4", 0)]
    assert github.status_reads == without.status_reads
    assert github.calls == [
        call.replace("issue-1", "issue-2").replace(f"{HOME}#1", f"{HOME}#2") for call in without.calls
    ]


def test_tickets_waiting_on_a_path_count_toward_its_holder(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, box=3, now=now)
    github.issues[HOME] = [issue(1, created_at=1.0), issue(2, created_at=2.0, body="Touches: lib/x.py")]
    feeder.feed_once()
    pull = {key: row["ticket_id"] for key, row in rows(tmp_path / "orch.db").items()}
    github.statuses = {ref(int(pull["2"])): OPEN_PULL}
    github.issues[HOME] += [issue(3, created_at=3.0, body="Touches: lib/x.py"),
                            issue(4, created_at=4.0, body="Touches: lib/x.py\nBlocked by #5"),
                            issue(5, created_at=5.0, body="Touches: docs/y.md")]
    now[0] += 60
    feeder.feed_once()
    with queue.connect() as connection:
        unblocks = {item["issue"]: item["unblocks"] for item in feeder_items(connection)}
    assert unblocks == {"1": 0, "2": 2, "3": 1, "4": 1, "5": 1}
    _enqueue(queue, now, pull["1"], pull["2"])
    assert _claim_tickets(queue) == [pull["2"], pull["1"]]


def test_a_linear_blocker_in_another_project_counts_its_dependent(tmp_path: Path) -> None:
    orch = LINEAR_ORCH + (
        "  - repo: acme/other-app\n    factory_ref: dev\n"
        "    linear_project_id: proj-2\n    intake:\n      linear_state: In Progress\n"
    )
    linear = FakeLinear()
    linear.issues = [
        linear_issue("AUT-1", created_at=1),
        linear_issue("AUT-2", created_at=2),
        replace(linear_issue("EXT-3", created_at=3), repo="acme/other-app",
                blocked_by=(("AUT-2", "started"),)),
    ]
    feeder, queue, _, _ = make_feeder(tmp_path, orch=orch, linear=linear, box=1, factory_text=LINEAR_FACTORY_MD)
    fed = [item.ticket_id for item in feeder.feed_once().fed]
    with queue.connect() as connection:
        unblocks = {item["issue"]: item["unblocks"] for item in feeder_items(connection)}
    assert rows(tmp_path / "orch.db")["EXT-3"]["waiting_reason"] == "waiting on AUT-2"
    assert unblocks == {"AUT-1": 0, "AUT-2": 1, "EXT-3": 0}
    assert fed == ["AUT-2"]


def test_a_ticket_refused_at_intake_unblocks_nothing(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    github.statuses = {ref(2): OPEN}
    github.issues[HOME] = [
        issue(1, created_at=1.0),
        issue(2, created_at=2.0),
        issue(3, created_at=3.0, body="Blocked by #2", template=False),
    ]
    fed = fed_heads(github, feeder.feed_once())
    with queue.connect() as connection:
        unblocks = {item["issue"]: item["unblocks"] for item in feeder_items(connection)}
    assert rows(tmp_path / "orch.db")["3"]["state"] == "refused-intake"
    assert unblocks == {"1": 0, "2": 0, "3": 0}
    assert fed == ["factory/issue-1"]


def _fed_tier_two(tmp_path: Path) -> Path:
    """#1 (priority:p2) fed and queued as PR 501; #2 (no tier) waits for room."""
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=1, now=now)
    dispatcher_for(tmp_path, queue)
    github.issues[HOME] = [issue(1, labels=(READY, "priority:p2"), created_at=1.0), issue(2, created_at=2.0)]
    feeder.feed_once()
    _enqueue(queue, now, "501")
    return tmp_path / "orch.db"


def test_orch_status_prints_the_tier_before_the_state(tmp_path: Path) -> None:
    reader = SQLiteStatusReader(_fed_tier_two(tmp_path))
    text = render(build_status(reader, reader, clock=lambda: 20_000.0,
                               feeder=reader.feeder(), attention=reader.attention()))
    assert "  owner/app#1 tier 2 fed -> PR #501\n" in text
    assert "  owner/app#2 waiting for capacity\n" in text


CHIP = '<span class="chip accent">tier 2</span>'


def test_the_dashboard_shows_the_tier_in_the_feeder_and_the_queue(tmp_path: Path) -> None:
    snapshot = collect_snapshot(_fed_tier_two(tmp_path), tmp_path / "none.log", now=20_000.0)
    assert [(item["issue"], item["tier"]) for item in snapshot["feeder"]["items"]] == [("1", 2), ("2", None)]
    assert [(row["ticket_id"], row["tier"]) for row in snapshot["queued"]] == [("501", 2)]
    index = _no_script(render_pages(snapshot)["index.html"])
    intake = _section(index, "intake")
    assert intake.count("tier") == 1
    assert ('<li><span class="ticket">app#1</span><span class="ititle" title="Issue 1">Issue 1</span>'
            f'{CHIP}<span class="what">fed</span>') in intake
    assert _section(index, "queue").count(CHIP) == 1


@needs_node
def test_the_script_draws_the_same_tier_chips(tmp_path: Path) -> None:
    snapshot = collect_snapshot(_fed_tier_two(tmp_path), tmp_path / "none.log", now=20_000.0)
    out = _run_page(tmp_path, snapshot, "main", ["intake", "lanes"])
    server = _no_script(render_pages(snapshot)["index.html"])
    assert out["html"]["intake"].count(CHIP) == _section(server, "intake").count(CHIP) == 1
    assert out["html"]["lanes"].count(CHIP) == _section(server, "queue").count(CHIP) == 1


def _fed_inherited(tmp_path: Path) -> Path:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=1, now=now)
    dispatcher_for(tmp_path, queue)
    github.statuses = {ref(1): OPEN}
    github.issues[HOME] = [issue(1, labels=(READY, "priority:p1"), created_at=1.0),
                           issue(2, labels=(READY, "priority:p0"), created_at=2.0, body="Blocked by #1")]
    feeder.feed_once()
    _enqueue(queue, now, "501")
    return tmp_path / "orch.db"


def test_orch_status_names_the_own_tier_of_a_ticket_that_inherited_one(tmp_path: Path) -> None:
    reader = SQLiteStatusReader(_fed_inherited(tmp_path))
    text = render(build_status(reader, reader, clock=lambda: 20_000.0,
                               feeder=reader.feeder(), attention=reader.attention()))
    assert "  owner/app#1 tier 0 (own 1) fed -> PR #501\n" in text
    assert "  owner/app#2 tier 0 waiting on #1\n" in text


INHERITED = '<span class="chip accent">tier 0 (own 1)</span>'


def test_the_dashboard_names_the_own_tier_of_a_ticket_that_inherited_one(tmp_path: Path) -> None:
    snapshot = collect_snapshot(_fed_inherited(tmp_path), tmp_path / "none.log", now=20_000.0)
    assert [(item["issue"], item["tier"], item["own_tier"]) for item in snapshot["feeder"]["items"]] == [
        ("1", 0, 1), ("2", 0, 0)
    ]
    assert [(row["ticket_id"], row["tier"], row["own_tier"]) for row in snapshot["queued"]] == [("501", 0, 1)]
    index = _no_script(render_pages(snapshot)["index.html"])
    assert _section(index, "intake").count(INHERITED) == 1
    assert _section(index, "queue").count(INHERITED) == 1


@needs_node
def test_the_script_draws_the_same_inherited_tier_chips(tmp_path: Path) -> None:
    snapshot = collect_snapshot(_fed_inherited(tmp_path), tmp_path / "none.log", now=20_000.0)
    out = _run_page(tmp_path, snapshot, "main", ["intake", "lanes"])
    assert out["html"]["intake"].count(INHERITED) == 1
    assert out["html"]["lanes"].count(INHERITED) == 1


def _fed_untiered_blocker(tmp_path: Path) -> Path:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=1, now=now)
    dispatcher_for(tmp_path, queue)
    github.statuses = {ref(1): OPEN}
    github.issues[HOME] = [issue(1, created_at=1.0),
                           issue(2, labels=(READY, "priority:p0"), created_at=2.0, body="Blocked by #1")]
    feeder.feed_once()
    _enqueue(queue, now, "501")
    return tmp_path / "orch.db"


def test_orch_status_says_an_inherited_tier_has_no_own_tier(tmp_path: Path) -> None:
    reader = SQLiteStatusReader(_fed_untiered_blocker(tmp_path))
    text = render(build_status(reader, reader, clock=lambda: 20_000.0,
                               feeder=reader.feeder(), attention=reader.attention()))
    assert "  owner/app#1 tier 0 (own none) fed -> PR #501\n" in text


NO_OWN = '<span class="chip accent">tier 0 (own none)</span>'


def test_the_dashboard_says_an_inherited_tier_has_no_own_tier(tmp_path: Path) -> None:
    snapshot = collect_snapshot(_fed_untiered_blocker(tmp_path), tmp_path / "none.log", now=20_000.0)
    index = _no_script(render_pages(snapshot)["index.html"])
    assert _section(index, "intake").count(NO_OWN) == 1
    assert _section(index, "queue").count(NO_OWN) == 1


@needs_node
def test_the_script_says_an_inherited_tier_has_no_own_tier(tmp_path: Path) -> None:
    snapshot = collect_snapshot(_fed_untiered_blocker(tmp_path), tmp_path / "none.log", now=20_000.0)
    out = _run_page(tmp_path, snapshot, "main", ["intake", "lanes"])
    assert out["html"]["intake"].count(NO_OWN) == 1
    assert out["html"]["lanes"].count(NO_OWN) == 1



# The two tables exactly as they were before issue #258.
OLD_PRIORITY = """
CREATE TABLE ticket_priority (
    repo TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    urgent INTEGER NOT NULL DEFAULT 0,
    bug INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (repo, ticket_id)
)
"""
OLD_FEEDER = """
CREATE TABLE feeder_items (
    repo TEXT NOT NULL,
    issue TEXT NOT NULL,
    tracker TEXT NOT NULL,
    title TEXT NOT NULL,
    harness TEXT NOT NULL,
    state TEXT NOT NULL,
    urgent INTEGER NOT NULL DEFAULT 0,
    bug INTEGER NOT NULL DEFAULT 0,
    risk TEXT NOT NULL,
    issue_created_at REAL NOT NULL,
    branch TEXT,
    pr_number TEXT,
    ticket_id TEXT,
    brief_comment_id TEXT,
    go_comment_id TEXT,
    first_seen_at REAL NOT NULL,
    seen_at REAL NOT NULL,
    fed_at REAL,
    error TEXT,
    waiting_reason TEXT,
    touches TEXT,
    conflicts TEXT,
    released INTEGER NOT NULL DEFAULT 0,
    capped_release INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, issue)
)
"""


def test_a_database_from_before_tiers_still_reads_and_then_migrates(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    now = [100.0]
    _enqueue(SQLiteJobQueue(db, clock=lambda: now[0]), now, "7", "8")
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE ticket_priority")
        connection.execute(OLD_PRIORITY)
        connection.execute("INSERT INTO ticket_priority VALUES (?, '8', 1, 0, 100.0)", (HOME,))
        connection.execute(OLD_FEEDER)
        connection.execute("CREATE TABLE feeder_poll (id INTEGER PRIMARY KEY CHECK (id = 1), at REAL NOT NULL)")
        connection.execute("INSERT INTO feeder_poll VALUES (1, 100.0)")
        connection.execute(
            "INSERT INTO feeder_items (repo, issue, tracker, title, harness, state, urgent, bug, risk,"
            " issue_created_at, first_seen_at, seen_at) VALUES (?, '8', 'github', 'Eight', 'claude',"
            " 'eligible', 1, 0, 'low', 5.0, 100.0, 100.0)",
            (HOME,),
        )
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        assert [candidate.ticket_id for candidate in claim_order(connection, "claude", now=0.0)] == ["8", "7"]
        assert [(item["issue"], item["tier"], item["own_tier"], item["unblocks"])
                for item in feeder_items(connection)] == [("8", None, None, 0)]
        assert priority_marks(connection)[(HOME, "8")].own_tier is None
    SQLiteJobQueue(db, clock=lambda: now[0])
    with sqlite3.connect(db) as connection:
        ensure_tables(connection)
    with sqlite3.connect(db) as connection:
        assert list(connection.execute(
            "SELECT ticket_id, urgent, tier, own_tier, unblocks FROM ticket_priority"
        )) == [("8", 1, None, None, 0)]
        assert list(connection.execute("SELECT issue, urgent, tier, own_tier, unblocks FROM feeder_items")) == [
            ("8", 1, None, None, 0)
        ]


def test_a_fed_ticket_that_gains_a_blocker_passes_it_its_tier(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=3, now=now)
    first = issue(1, labels=(READY, "priority:p1"), created_at=1.0)
    second = issue(2, labels=(READY, "priority:p0"), created_at=2.0)
    third = issue(3, labels=(READY, "urgent", "priority:p1"), created_at=3.0)
    github.issues[HOME] = [first, second, third]
    feeder.feed_once()
    pull = {key: row["ticket_id"] for key, row in rows(tmp_path / "orch.db").items()}
    _enqueue(queue, now, pull["1"], pull["2"], pull["3"])
    github.issues[HOME] = [first, replace(second, body="Blocked by #1\n" + second.body), third]
    github.statuses = {ref(1): OPEN, **{ref(int(number)): OPEN_PULL for number in pull.values()}}
    now[0] += 1
    feeder.feed_once()
    assert _tiers(queue)["1"] == (0, 1)
    assert _claim_tickets(queue) == [pull["1"], pull["2"], pull["3"]]


def test_a_ticket_whose_feed_failed_passes_its_tier_to_its_blocker(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=2)
    dependent = issue(2, labels=(READY, "priority:p0"), created_at=2.0)
    github.issues[HOME] = [dependent]
    github.fail["pull"] = 1
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"][0] == "error"
    github.statuses = {ref(1): OPEN}
    github.issues[HOME] = [
        issue(1, labels=(READY, "priority:p2"), created_at=1.0),
        replace(dependent, body="Blocked by #1\n" + dependent.body),
        issue(3, labels=(READY, "priority:p1"), created_at=3.0),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2", "factory/issue-1"]
    assert _tiers(queue)["1"] == (0, 2)


def _critical_flags(queue: SQLiteJobQueue) -> list[tuple[str, str, str]]:
    with queue.connect() as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT repo, ticket_id, detail FROM needs_attention WHERE reason = 'critical-path-blocker'"
        )]


def test_an_open_blocker_that_is_not_a_candidate_raises_one_flag_until_it_closes(tmp_path: Path) -> None:
    from tests.test_feeder_gates import DONE

    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(GITHUB_ORCH, TIERS), box=2)
    github.issues[HOME] = [
        issue(1, labels=(READY, "priority:p2"), body="Blocked by: #780"),
        issue(2, labels=(READY, "priority:p1"), body="Blocked by: #780"),
        issue(3, body="Blocked by: #781"),
    ]
    github.statuses = {ref(780): OPEN, ref(781): OPEN}
    feeder.feed_once()
    assert sorted(_critical_flags(queue)) == [
        (HOME, "780", "gates 2 candidates, best tier 1"),
        (HOME, "781", "gates 1 candidates, best tier none"),
    ]
    github.fail["statuses"] = 1
    github.statuses = {ref(780): DONE, ref(781): DONE}
    feeder.feed_once()
    assert len(_critical_flags(queue)) == 2
    feeder.feed_once()
    assert _critical_flags(queue) == []


TWO_REPOS = _with(GITHUB_ORCH, TIERS) + """  - repo: owner/aut
    factory_ref: main
    intake:
      label: factory:ready
"""
OUTSIDE = ("owner/aut", "9", "gates 1 candidates, best tier 0")


def _flag_outside_blocker(tmp_path: Path):
    feeder, queue, github, _ = make_feeder(tmp_path, orch=TWO_REPOS, box=2)
    github.issues[HOME] = [issue(2, labels=(READY, "priority:p0"), body="Depends on: owner/aut#9")]
    github.statuses = {ref(9, "owner/aut"): OPEN}
    feeder.feed_once()
    assert _critical_flags(queue) == [OUTSIDE]
    return feeder, queue, github


def test_a_failed_dependent_read_keeps_the_open_blockers_flag(tmp_path: Path) -> None:
    feeder, queue, github = _flag_outside_blocker(tmp_path)
    github.fail[f"intake:{HOME}"] = 1
    assert feeder.feed_once().errors == 1
    assert _critical_flags(queue) == [OUTSIDE]
    feeder.feed_once()
    assert _critical_flags(queue) == [OUTSIDE]


def test_a_fed_dependent_keeps_its_open_blockers_flag_until_the_blocker_closes(tmp_path: Path) -> None:
    from tests.test_feeder_gates import DONE

    feeder, queue, github = _flag_outside_blocker(tmp_path)
    with queue.connect() as connection:
        connection.execute("UPDATE feeder_items SET state = 'fed' WHERE issue = '2'")
    feeder.feed_once()
    assert states(tmp_path)["2"][0] == "fed"
    assert _critical_flags(queue) == [OUTSIDE]
    github.statuses = {ref(9, "owner/aut"): DONE}
    feeder.feed_once()
    assert _critical_flags(queue) == []


def test_blockers_off_the_critical_path_raise_no_flag(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    github.issues[HOME] = [issue(1)]
    feeder.feed_once()
    github.issues[HOME] = [issue(2, body="Blocked by: #1\nBlocked by: other/lib#5")]
    github.statuses = {ref(1): OPEN, ref(5, "other/lib"): OPEN, ref(501): OPEN_PULL}
    feeder.feed_once()
    assert states(tmp_path)["2"][0] == "waiting-blocked"
    assert _critical_flags(queue) == []


SHARE_ORCH = GITHUB_ORCH + """  - repo: owner/aut
    factory_ref: main
    intake:
      label: factory:ready
"""


def _share_feeder(tmp_path: Path, share: str):
    feeder, queue, github, _ = make_feeder(tmp_path, orch=_with(SHARE_ORCH, TIERS) + share, box=4)
    github.issues[HOME] = [issue(n, labels=(READY, "priority:p0"), created_at=float(n)) for n in (1, 2, 3, 4)]
    github.issues["owner/aut"] = [issue(n, repo="owner/aut", created_at=0.5) for n in (11, 12, 13)]
    return feeder, queue, github


def _fed(github, report) -> list[str]:
    return [f"{item.repo}#{github.pulls[(item.repo, int(item.ticket_id))]['head'].split('-')[-1]}"
            for item in report.fed]


@pytest.mark.parametrize(("share", "fed"), [
    ("", ["owner/app#1", "owner/app#2", "owner/app#3", "owner/app#4"]),
    ("      capacity_share: 0.5\n", ["owner/aut#11", "owner/aut#12", "owner/app#1", "owner/app#2"]),
])
def test_a_project_under_its_capacity_share_is_fed_first(tmp_path: Path, share: str, fed: list[str]) -> None:
    feeder, _, github = _share_feeder(tmp_path, share)
    assert _fed(github, feeder.feed_once()) == fed


def test_queued_jobs_count_toward_a_projects_capacity_share(tmp_path: Path) -> None:
    feeder, queue, github = _share_feeder(tmp_path, "      capacity_share: 0.5\n")
    for ticket in ("901", "902"):
        queue.enqueue(JobRequest(repo="owner/aut", ticket_id=ticket, action="build", recipient="claude",
                                 source_comment_id=ticket, timeout_seconds=600), dedupe_key=ticket)
    assert _fed(github, feeder.feed_once()) == ["owner/app#1", "owner/app#2"]


@pytest.mark.parametrize(("value", "message"), [
    ("1.5", "capacity_share must be a number from 0 to 1"),
    ("-0.1", "capacity_share must be a number from 0 to 1"),
    ("true", "capacity_share must be a number from 0 to 1"),
])
def test_a_bad_capacity_share_is_refused(tmp_path: Path, value: str, message: str) -> None:
    messages = _refused(tmp_path, _with(GITHUB_ORCH, f"      capacity_share: {value}\n"))
    assert any(message in item for item in messages), messages


def test_capacity_shares_summing_over_one_are_refused(tmp_path: Path) -> None:
    anchor = "      label: factory:ready\n"
    orch = SHARE_ORCH.replace(anchor, anchor + "      capacity_share: 0.6\n", 1) + "      capacity_share: 0.5\n"
    messages = _refused(tmp_path, orch)
    assert any("capacity_share values sum to 1.1, more than 1" in item for item in messages), messages


def test_a_capacity_share_of_one_half_is_accepted(tmp_path: Path) -> None:
    intake = orch_config(tmp_path, _with(GITHUB_ORCH, "      capacity_share: 0.5\n")).projects[0].intake
    assert intake.capacity_share == 0.5
    assert orch_config(tmp_path, GITHUB_ORCH).projects[0].intake.capacity_share is None
