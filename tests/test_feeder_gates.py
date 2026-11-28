"""Feeder gates (issue #156): Blocked by, Touches and Conflicts with lines.

Parsing and gate decisions are pure (lib/feeder_gates.py). The feeder tests
use the fakes from tests/test_feeder.py; nothing reaches a real tracker.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.types import BaseCheck
from lib.evidence.ci import main_red
from lib.feeder import FEEDER_TABLE, Feeder, ensure_tables, feeder_counts, feeder_items
from lib.feeder_gates import (
    GateLines,
    Holds,
    Progress,
    Ref,
    RefStatus,
    Wait,
    blocker_waits,
    github_ref,
    line_stop,
    linear_status,
    parse_gate_lines,
    paths_collide,
    stacked_lines,
)
from tests.test_feeder import (
    FACTORY_MD,
    GITHUB_ORCH,
    LINEAR_FACTORY_MD,
    LINEAR_ORCH,
    REQUIRED_MD,
    TEMPLATE,
    FakeGitHub,
    FakeLinear,
    dispatcher_for,
    issue,
    linear_issue,
    make_feeder,
    rows,
)
from tests.test_feeder_template import body, wakes

HOME = "owner/app"
OPEN = RefStatus(Progress.OPEN)
DONE = RefStatus(Progress.DONE)
MERGED = RefStatus(Progress.DONE, is_pull=True)
OPEN_PULL = RefStatus(Progress.OPEN, is_pull=True)
CLOSED_PULL = RefStatus(Progress.UNFINISHED, is_pull=True, detail="closed without merge")
NOT_PLANNED = RefStatus(Progress.UNFINISHED, detail="closed as not planned")


def ref(number: int, repo: str = HOME) -> Ref:
    return github_ref(repo, number)


def blocked(*numbers: int) -> GateLines:
    return GateLines(blocked_by=frozenset(ref(n) for n in numbers))


def states(tmp_path: Path) -> dict[str, tuple[str, str | None]]:
    return {key: (row["state"], row["waiting_reason"]) for key, row in rows(tmp_path / "orch.db").items()}


def flags(tmp_path: Path) -> set[tuple[str, str, str]]:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        return set(connection.execute("SELECT repo, ticket_id, reason FROM needs_attention"))


def fed_heads(github: FakeGitHub, report) -> list[str]:
    return [github.pulls[(HOME, int(item.ticket_id))]["head"] for item in report.fed]


# --- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "numbers"),
    [
        ("Blocked by #4", {4}),
        ("Blocked by: #4", {4}),
        ("**Blocked by:** #4 and #5", {4, 5}),
        ("**Blocked by**: #4", {4}),
        ("Blocked by #4, PR #9, open PR #10", {4, 9, 10}),
        ("- blocked BY: #4", {4}),
        ("1. Blocked by #7 (#8 too)", {7, 8}),
        ("Blocked by https://github.com/owner/app/issues/12 and https://github.com/owner/app/pull/13", {12, 13}),
    ],
)
def test_parse_blocked_by_forms(line: str, numbers: set[int]) -> None:
    assert parse_gate_lines(line, "Owner/App").blocked_by == {ref(n) for n in numbers}


def test_parse_cross_repo_blockers() -> None:
    lines = parse_gate_lines(
        "Blocked by other/repo#3, https://github.com/Third/lib.js/pull/40 and #2", HOME
    )
    assert lines.blocked_by == {ref(3, "other/repo"), ref(40, "third/lib.js"), ref(2)}
    assert ref(3, "other/repo").label(HOME) == "other/repo#3"
    assert ref(2).label(HOME) == "#2"


def test_parse_real_chain_lines() -> None:
    # The gate lines of #138, #141, #145, #147, #150 and #151 as they read on 2026-11-13.
    bodies = {
        138: "**Blocked by:** #137, PR #111, PR #116.\n**Touches:** lib/dashboard_page.py\n\n"
             "Open PRs #111 and #116 should be landed or closed first.",
        141: "**Blocked by:** #140 and #152 (per-project ship mode and round cap data).",
        145: "**Blocked by:** #144, PR #113, PR #131.\n"
             "**Touches:** lib/dashboard.py, lib/dashboard_flow.py",
        147: "**Blocked by:** #137. Does not touch `lib/dashboard_page.py`; may run beside "
             "the Mission Control chain.",
        150: "**Blocked by:** #145 and open PR #113 (ticket lifecycle; same transition code). "
             "Does not touch `lib/dashboard_page.py`.",
        151: "**Blocked by:** #137.\n**Touches:** lib/dashboard.py\n**Conflicts with:** #152",
    }
    parsed = {number: parse_gate_lines(body, "maxharris1/factory") for number, body in bodies.items()}

    def numbers(refs) -> set[int]:
        return {int(item.key) for item in refs}

    assert numbers(parsed[138].blocked_by) == {137, 111, 116}
    assert parsed[138].touches == {"lib/dashboard_page.py"}
    assert numbers(parsed[141].blocked_by) == {140, 152}
    assert numbers(parsed[145].blocked_by) == {144, 113, 131}
    assert parsed[145].touches == {"lib/dashboard.py", "lib/dashboard_flow.py"}
    assert numbers(parsed[147].blocked_by) == {137} and not parsed[147].touches
    assert numbers(parsed[150].blocked_by) == {145, 113} and not parsed[150].touches
    assert parsed[151].touches == {"lib/dashboard.py"}
    assert numbers(parsed[151].conflicts_with) == {152}


def test_parse_ignores_prose_backticks_and_fences() -> None:
    body = "\n".join(
        [
            "This ticket is blocked by nothing yet.",
            "Open PRs #111 and #116 should land first.",
            "`Blocked by #N` / `Blocked by: #N` are examples.",
            "- `Touches: path, path`",
            "```",
            "Blocked by #5",
            "Touches: lib/x.py",
            "```",
            "Touches the dashboard, lib/y.py",
            "Blocked by: none",
        ]
    )
    assert not parse_gate_lines(body, HOME)


def test_parse_touches_and_conflicts() -> None:
    body = (
        "Touches: `lib/a.py`, ./lib/b.py and docs/,  .github/workflows/ci.yml.\n"
        "* **Conflicts with:** #8, other/repo#9\n"
        "Conflicts with https://github.com/owner/app/pull/12"
    )
    lines = parse_gate_lines(body, HOME)
    assert lines.touches == {"lib/a.py", "lib/b.py", "docs/", ".github/workflows/ci.yml"}
    assert lines.conflicts_with == {ref(8), ref(9, "other/repo"), ref(12)}
    assert not lines.blocked_by


@pytest.mark.parametrize(
    ("first", "second", "collide"),
    [
        ("lib/a.py", "lib/a.py", True),
        ("lib/", "lib/a.py", True),
        ("lib/a.py", "lib/", True),
        ("lib/a.py", "lib/b.py", False),
        ("lib", "lib/a.py", False),
        ("lib/a.py", "lib/a.pyc", False),
    ],
)
def test_paths_collide_on_equal_or_folder(first: str, second: str, collide: bool) -> None:
    assert paths_collide(first, second) is collide


# --- gate decisions (pure) ----------------------------------------------------


def test_pr_blocker_done_only_when_merged() -> None:
    gated = {ref(5): blocked(4)}
    assert blocker_waits(gated, {ref(4): MERGED}) == {}
    assert blocker_waits(gated, {ref(4): OPEN_PULL})[ref(5)].reason == "waiting on #4"
    closed = blocker_waits(gated, {ref(4): CLOSED_PULL})[ref(5)]
    assert closed.reason == "waiting on #4 (closed without merge)"
    assert closed.attention == "blocker-closed"


def test_issue_blocker_done_when_closed_completed() -> None:
    gated = {ref(5): blocked(4)}
    assert blocker_waits(gated, {ref(4): DONE}) == {}
    assert blocker_waits(gated, {ref(4): OPEN})[ref(5)].attention is None


def test_a_blocker_closed_not_planned_or_missing_is_flagged() -> None:
    gated = {ref(5): blocked(4, 6, 7)}
    wait = blocker_waits(
        gated, {ref(4): NOT_PLANNED, ref(6): RefStatus(Progress.MISSING), ref(7): OPEN}
    )[ref(5)]
    assert wait.reason == "waiting on #4 (closed as not planned), #6 (not found), #7"
    assert wait.attention == "blocker-closed"
    missing = blocker_waits({ref(5): blocked(6)}, {ref(6): RefStatus(Progress.MISSING)})
    assert missing[ref(5)].attention == "blocker-missing"


def test_an_unread_blocker_waits_without_a_flag() -> None:
    wait = blocker_waits({ref(5): blocked(4)}, {})[ref(5)]
    assert wait.reason == "waiting on #4 (status unreadable)"
    assert wait.attention is None


def test_cycle_detection_two_and_three_long_and_self() -> None:
    gated = {
        ref(1): blocked(2),
        ref(2): blocked(1),
        ref(3): blocked(4),
        ref(4): blocked(5),
        ref(5): blocked(3),
        ref(6): blocked(6),
        ref(7): blocked(1),  # waits on a cycle member, but is not in one
    }
    waits = blocker_waits(gated, {r: OPEN for r in map(ref, range(1, 8))})
    assert {r.key for r, w in waits.items() if w.attention == "blocker-cycle"} == {
        "1", "2", "3", "4", "5", "6"
    }
    assert waits[ref(1)].reason == "waiting on a blocker cycle: #1 -> #2 -> #1"
    assert waits[ref(4)].reason == "waiting on a blocker cycle: #4 -> #5 -> #3 -> #4"
    assert waits[ref(6)].reason == "waiting on a blocker cycle: #6 -> #6"
    assert waits[ref(7)].reason == "waiting on #1" and waits[ref(7)].attention is None


def test_a_done_ticket_breaks_a_cycle() -> None:
    waits = blocker_waits({ref(1): blocked(2), ref(2): blocked(1)}, {ref(1): OPEN, ref(2): DONE})
    assert set(waits) == {ref(2)}
    assert waits[ref(2)].attention is None


def test_linear_status_types() -> None:
    assert linear_status("completed").progress is Progress.DONE
    assert linear_status("canceled").progress is Progress.UNFINISHED
    assert linear_status("started").progress is Progress.OPEN
    assert linear_status(None).progress is Progress.OPEN


def test_holds_collide_on_paths_and_conflicts_with_either_side() -> None:
    holds = Holds()
    holds.take(ref(8), (ref(501),), GateLines(touches=frozenset({"lib/"})))
    holds.take(ref(9), (), GateLines(conflicts_with=frozenset({ref(11)})))
    path = holds.collision(ref(10), GateLines(touches=frozenset({"lib/x.py"})))
    assert path is not None and path.reason == "waiting on file lib/x.py held by #8"
    by_pr = holds.collision(ref(12), GateLines(conflicts_with=frozenset({ref(501)})))
    assert by_pr is not None and by_pr.reason == "waiting on #8 (conflicts with it)"
    named = holds.collision(ref(11), GateLines())
    assert named is not None and named.reason == "waiting on #9 (conflicts with it)"
    assert holds.collision(ref(8), GateLines(touches=frozenset({"lib/x.py"}))) is None
    assert holds.collision(ref(13), GateLines(touches=frozenset({"docs/a.md"}))) is None



def test_holds_scope_paths_to_the_repo_and_keep_cross_repo_conflicts() -> None:
    holds = Holds()
    holds.take(ref(1), (), GateLines(touches=frozenset({"lib/x.py"})))
    holds.take(ref(9, "owner/lib"), (), GateLines(conflicts_with=frozenset({ref(5)})))
    assert holds.collision(ref(2, "owner/other-app"), GateLines(touches=frozenset({"lib/x.py"}))) is None
    same = holds.collision(ref(3), GateLines(touches=frozenset({"lib/"})))
    assert same is not None and same.reason == "waiting on file lib/x.py held by #1"
    across = holds.collision(ref(5), GateLines())
    assert across is not None and across.reason == "waiting on owner/lib#9 (conflicts with it)"
    named = holds.collision(ref(4, "owner/other-app"), GateLines(conflicts_with=frozenset({ref(1)})))
    assert named is not None and named.reason == "waiting on owner/app#1 (conflicts with it)"


def test_equal_paths_in_distinct_repos_feed_together(tmp_path: Path) -> None:
    orch = GITHUB_ORCH + (
        "  - repo: owner/other-app\n    factory_ref: main\n"
        "    intake:\n      label: factory:ready\n"
    )
    feeder, _, github, _ = make_feeder(tmp_path, orch=orch, box=3)
    github.issues[HOME] = [issue(1, body="Touches: lib/x.py", created_at=1)]
    github.issues["owner/other-app"] = [
        replace(issue(2, body="Touches: lib/x.py", created_at=2), repo="owner/other-app")
    ]
    report = feeder.feed_once()
    assert sorted(item.repo for item in report.fed) == ["owner/app", "owner/other-app"]
    assert report.blocked == 0

# --- the blocked-by gate in the feed loop -------------------------------------


def test_a_blocked_ticket_is_not_fed_until_every_blocker_is_done(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [issue(5, body="Blocked by #3, PR #4 and other/lib#9")]
    github.statuses = {ref(3): DONE, ref(4): OPEN_PULL, ref(9, "other/lib"): OPEN}
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["5"] == ("waiting-blocked", "waiting on #4, other/lib#9")
    github.statuses[ref(4)] = MERGED
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["5"] == ("waiting-blocked", "waiting on other/lib#9")
    github.statuses[ref(9, "other/lib")] = DONE
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-5"]
    assert states(tmp_path)["5"] == ("fed", None)


def test_dependent_is_fed_on_the_poll_after_its_blocker_merges(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [issue(1, created_at=1.0), issue(2, created_at=2.0, body="Blocked by #1")]
    github.statuses = {ref(1): OPEN}
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on #1")
    assert feeder.feed_once().fed == ()  # #1's PR is still open
    # #1's PR (501) merges and closes #1. No relabel, no handoff.
    github.statuses[ref(1)] = DONE
    github.statuses[ref(501)] = MERGED
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]


def test_issue_blocker_done_when_its_feeder_pr_merged(tmp_path: Path) -> None:
    # #1's PR merged into a non-default base, so GitHub left #1 open.
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [issue(1, created_at=1.0), issue(2, created_at=2.0, body="Blocked by #1")]
    github.statuses = {ref(1): OPEN, ref(501): OPEN_PULL}
    feeder.feed_once()
    assert feeder.feed_once().fed == ()
    github.statuses[ref(501)] = MERGED
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]
    assert ref(501) in github.status_reads[-1]


def test_a_blocked_ticket_takes_no_room(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=1)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Blocked by #40"),
        issue(2, created_at=2.0),
    ]
    github.statuses = {ref(40): OPEN}
    report = feeder.feed_once()
    assert fed_heads(github, report) == ["factory/issue-2"]
    assert report.blocked == 1 and report.waiting == 0
    assert states(tmp_path)["1"] == ("waiting-blocked", "waiting on #40")


def test_a_cycle_flags_every_participant_and_feeds_none(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [issue(4, body="Blocked by #5"), issue(5, body="Blocked by: #4")]
    github.statuses = {ref(4): OPEN, ref(5): OPEN}
    assert feeder.feed_once().fed == ()
    assert flags(tmp_path) == {(HOME, "4", "blocker-cycle"), (HOME, "5", "blocker-cycle")}
    assert states(tmp_path)["4"] == ("waiting-blocked", "waiting on a blocker cycle: #4 -> #5 -> #4")


def test_a_blocker_closed_unmerged_flags_needs_attention(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body="Blocked by open PR #6")]
    github.statuses = {ref(6): CLOSED_PULL}
    assert feeder.feed_once().fed == ()
    assert flags(tmp_path) == {(HOME, "7", "blocker-closed")}
    assert states(tmp_path)["7"] == ("waiting-blocked", "waiting on #6 (closed without merge)")


def test_a_blocker_closed_not_planned_flags_needs_attention(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body="Blocked by #6")]
    github.statuses = {ref(6): NOT_PLANNED}
    feeder.feed_once()
    assert flags(tmp_path) == {(HOME, "7", "blocker-closed")}


def test_attention_clears_when_the_cause_is_gone(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, now=now)
    github.issues[HOME] = [issue(7, body="Blocked by #6"), issue(8, body="Blocked by #9")]
    github.statuses = {ref(6): NOT_PLANNED, ref(9): RefStatus(Progress.MISSING)}
    feeder.feed_once()
    assert flags(tmp_path) == {(HOME, "7", "blocker-closed"), (HOME, "8", "blocker-missing")}
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        raised = dict(connection.execute("SELECT ticket_id, raised_at FROM needs_attention"))
    now[0] += 60
    feeder.feed_once()  # still flagged: raised_at is kept, not bumped
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        assert dict(connection.execute("SELECT ticket_id, raised_at FROM needs_attention")) == raised
    github.statuses[ref(6)] = DONE  # reopened and completed
    github.issues[HOME] = [issue(7, body="Blocked by #6")]  # #8 lost its label
    now[0] += 60
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-7"]
    assert flags(tmp_path) == set()


def test_flags_of_a_project_whose_intake_read_failed_are_kept(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body="Blocked by #6")]
    github.statuses = {ref(6): NOT_PLANNED}
    feeder.feed_once()

    def broken(repo: str, label: str):
        raise RuntimeError("GitHub is down")

    github.github_intake = broken  # type: ignore[method-assign]
    feeder.feed_once()
    assert flags(tmp_path) == {(HOME, "7", "blocker-closed")}


def test_an_unreadable_status_fails_closed_without_a_flag(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body="Blocked by #6")]
    github.statuses = {ref(6): NOT_PLANNED}
    feeder.feed_once()
    github.fail["statuses"] = 1
    report = feeder.feed_once()
    assert report.fed == () and report.errors == 1
    assert states(tmp_path)["7"] == ("waiting-blocked", "waiting on #6 (status unreadable)")
    assert flags(tmp_path) == {(HOME, "7", "blocker-closed")}  # kept, not flapped


def test_linear_relation_blocker_waits_until_completed(tmp_path: Path) -> None:
    linear = FakeLinear()
    blocked_issue = replace(linear_issue("AUT-8"), blocked_by=(("AUT-7", "started"),))
    linear.issues = [blocked_issue]
    feeder, _, github, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, linear=linear,
                                       factory_text=LINEAR_FACTORY_MD)
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["AUT-8"] == ("waiting-blocked", "waiting on AUT-7")
    assert github.status_reads == []  # Linear's relation came with the intake read
    linear.issues = [replace(blocked_issue, blocked_by=(("AUT-7", "canceled"),))]
    feeder.feed_once()
    assert flags(tmp_path) == {("acme/linear-app", "AUT-8", "blocker-closed")}
    linear.issues = [replace(blocked_issue, blocked_by=(("AUT-7", "completed"),))]
    assert [item.ticket_id for item in feeder.feed_once().fed] == ["AUT-8"]
    assert flags(tmp_path) == set()


def test_linear_description_blocked_by_line_resolves_on_the_project_repo(tmp_path: Path) -> None:
    linear = FakeLinear()
    linear.issues = [replace(linear_issue("AUT-8"), body="Blocked by #12" + TEMPLATE)]
    feeder, _, github, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, linear=linear,
                                       factory_text=LINEAR_FACTORY_MD)
    github.statuses = {ref(12, "acme/linear-app"): OPEN_PULL}
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["AUT-8"] == ("waiting-blocked", "waiting on #12")
    github.statuses[ref(12, "acme/linear-app")] = MERGED
    assert [item.ticket_id for item in feeder.feed_once().fed] == ["AUT-8"]


def test_feeder_reads_statuses_once_per_poll_for_a_long_chain(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [issue(100, created_at=100.0)] + [
        issue(n, created_at=float(n), body=f"**Blocked by:** #{n - 1}") for n in range(101, 115)
    ]
    github.statuses = {ref(n): OPEN for n in range(100, 115)}
    for _ in range(3):
        feeder.feed_once()
    assert len(github.status_reads) == 3
    assert {ref(n) for n in range(100, 114)} <= github.status_reads[0]
    assert sum(1 for state, _ in states(tmp_path).values() if state == "waiting-blocked") == 14


def test_no_gate_lines_reads_only_the_fed_pr(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=1)
    github.issues[HOME] = [issue(1, created_at=1.0), issue(2, created_at=2.0)]
    feeder.feed_once()
    assert github.status_reads == []
    feeder.feed_once()
    assert github.status_reads == [frozenset({ref(501)})]
    assert states(tmp_path)["2"] == ("waiting-capacity", None)


def test_a_waiting_blocked_ticket_that_loses_its_label_is_forgotten(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, now=now)
    github.issues[HOME] = [issue(2, body="Blocked by #1")]
    github.statuses = {ref(1): OPEN}
    feeder.feed_once()
    github.issues[HOME] = []
    now[0] += 60
    feeder.feed_once()
    assert "2" not in rows(tmp_path / "orch.db")


def test_existing_database_gains_the_new_columns(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            f"""CREATE TABLE {FEEDER_TABLE} (
                repo TEXT NOT NULL, issue TEXT NOT NULL, tracker TEXT NOT NULL,
                title TEXT NOT NULL, harness TEXT NOT NULL, state TEXT NOT NULL,
                urgent INTEGER NOT NULL DEFAULT 0, bug INTEGER NOT NULL DEFAULT 0,
                risk TEXT NOT NULL, issue_created_at REAL NOT NULL, branch TEXT,
                pr_number TEXT, ticket_id TEXT, brief_comment_id TEXT, go_comment_id TEXT,
                first_seen_at REAL NOT NULL, seen_at REAL NOT NULL, fed_at REAL, error TEXT,
                PRIMARY KEY (repo, issue))"""
        )
        connection.execute(
            f"INSERT INTO {FEEDER_TABLE} (repo, issue, tracker, title, harness, state, risk, "
            "issue_created_at, first_seen_at, seen_at, pr_number) "
            "VALUES ('owner/app', '3', 'github', 't', 'claude', 'done', 'low', 1, 1, 1, '77')"
        )
        ensure_tables(connection)
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({FEEDER_TABLE})")}
        assert {"waiting_reason", "touches", "conflicts", "released"} <= columns
        assert connection.execute(f"SELECT released FROM {FEEDER_TABLE}").fetchone() == (0,)
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(4)]
    assert len(feeder.feed_once().fed) == 1


# --- the file-conflict gate ---------------------------------------------------


@pytest.mark.parametrize(
    ("tickets", "winner"),
    [
        ([issue(9, created_at=1.0), issue(10, created_at=1.0)], "9"),
        ([issue(2, created_at=1.0), issue(3, created_at=5.0, labels=("factory:ready", "bug"))], "3"),
        ([issue(2, created_at=1.0, labels=("factory:ready", "bug")),
          issue(3, created_at=5.0, labels=("factory:ready", "urgent"))], "3"),
        ([issue(5, created_at=2.0), issue(6, created_at=1.0)], "6"),
    ],
)
def test_collision_keeps_urgent_then_bug_then_oldest_then_lower_number(
    tmp_path: Path, tickets, winner: str
) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [replace(item, body="Touches: lib/x.py" + TEMPLATE) for item in tickets]
    report = feeder.feed_once()
    assert fed_heads(github, report) == [f"factory/issue-{winner}"]
    (loser,) = {item.key for item in tickets} - {winner}
    assert states(tmp_path)[loser] == ("waiting-blocked", f"waiting on file lib/x.py held by #{winner}")


def test_two_tickets_sharing_a_path_are_not_selected_in_one_poll(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: lib/"),
        issue(2, created_at=2.0, body="Touches: lib/feeder.py"),
        issue(3, created_at=3.0, body="Touches: docs/a.md"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1", "factory/issue-3"]
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/feeder.py held by #1")


def test_a_fed_ticket_with_an_open_pr_holds_its_paths(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, box=3, now=now)
    github.issues[HOME] = [issue(1, body="Touches: lib/x.py")]
    feeder.feed_once()  # #1 is fed as PR 501
    github.statuses = {ref(501): OPEN_PULL}
    github.issues[HOME] = [issue(2, body="Touches: lib/x.py")]  # #1's label is gone
    now[0] += 60
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/x.py held by #1")


@pytest.mark.parametrize("ended", [MERGED, CLOSED_PULL])
def test_a_holder_releases_when_its_pr_merges_or_closes(tmp_path: Path, ended) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [issue(1, created_at=1.0, body="Touches: lib/x.py"),
                           issue(2, created_at=2.0, body="Touches: lib/x.py")]
    github.statuses = {ref(501): OPEN_PULL}
    feeder.feed_once()
    assert feeder.feed_once().fed == ()
    github.statuses[ref(501)] = ended
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]
    assert rows(tmp_path / "orch.db")["1"]["released"] == 1
    github.statuses[ref(501)] = OPEN_PULL
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["1"]["released"] == 1
    assert states(tmp_path)["2"][0] == "fed"


def test_conflicts_with_is_symmetric_and_matches_issue_or_pr_number(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Conflicts with #3"),
        issue(2, created_at=2.0),
        issue(3, created_at=3.0),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1", "factory/issue-2"]
    assert states(tmp_path)["3"] == ("waiting-blocked", "waiting on #1 (conflicts with it)")
    github.statuses = {ref(501): OPEN_PULL}
    github.issues[HOME].append(issue(4, created_at=4.0, body="Conflicts with: #502"))
    feeder.feed_once()  # #2 is fed as PR 502, which #4 names
    assert states(tmp_path)["3"] == ("waiting-blocked", "waiting on #1 (conflicts with it)")
    assert states(tmp_path)["4"] == ("waiting-blocked", "waiting on #2 (conflicts with it)")


def test_conflicts_with_an_open_pr_outside_the_feeder_waits(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(1, body="Conflicts with PR #113")]
    github.statuses = {ref(113): OPEN_PULL}
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["1"] == ("waiting-blocked", "waiting on #113 (conflicts with it)")
    github.statuses[ref(113)] = MERGED
    assert len(feeder.feed_once().fed) == 1


def test_a_conflicting_ticket_takes_no_room(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=2)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: lib/x.py"),
        issue(2, created_at=2.0, body="Touches: lib/x.py"),
        issue(3, created_at=3.0),
    ]
    report = feeder.feed_once()
    assert fed_heads(github, report) == ["factory/issue-1", "factory/issue-3"]
    assert report.blocked == 1 and report.waiting == 0


# --- visibility -----------------------------------------------------------------


def waiting_chain(tmp_path: Path):
    feeder, queue, github, _ = make_feeder(tmp_path, box=3)
    dispatcher_for(tmp_path, queue)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: lib/dashboard.py"),
        issue(2, created_at=2.0, body="Blocked by #1"),
        issue(3, created_at=3.0, body="Touches: lib/dashboard.py"),
        issue(4, created_at=4.0, body="Blocked by #5"),
        issue(5, created_at=5.0, body="Blocked by #4"),
    ]
    github.statuses = {ref(n): OPEN for n in range(1, 6)}
    feeder.feed_once()
    return feeder


def test_feeder_counts_and_items_carry_waiting_reasons(tmp_path: Path) -> None:
    waiting_chain(tmp_path)
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        assert feeder_counts(connection) == {
            "eligible": 5, "fed": 1, "waiting_capacity": 0, "waiting_blocked": 4
        }
        reasons = {item["issue"]: item["waiting_reason"] for item in feeder_items(connection)}
    assert reasons["1"] is None
    assert reasons["2"] == "waiting on #1"
    assert reasons["3"] == "waiting on file lib/dashboard.py held by #1"


def test_orch_status_shows_waiting_reasons(tmp_path: Path) -> None:
    from lib.status import SQLiteStatusReader, build_status, render

    waiting_chain(tmp_path)
    reader = SQLiteStatusReader(tmp_path / "orch.db")
    text = render(build_status(reader, reader, clock=lambda: 20_000.0,
                               feeder=reader.feeder(), attention=reader.attention()))
    assert ("Feeder — eligible 5, fed 1, waiting for capacity 0, "
            "waiting on blockers or files 4") in text
    assert "owner/app#2 waiting on #1" in text
    assert "owner/app#3 waiting on file lib/dashboard.py held by #1" in text


def test_status_and_dashboard_list_the_blocker_flags(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from lib.status import SQLiteStatusReader, build_status, render

    waiting_chain(tmp_path)
    reader = SQLiteStatusReader(tmp_path / "orch.db")
    text = render(build_status(reader, reader, clock=lambda: 20_000.0,
                               feeder=reader.feeder(), attention=reader.attention()))
    assert "owner/app#4 blocker-cycle: waiting on a blocker cycle: #4 -> #5 -> #4" in text
    snapshot = collect_snapshot(tmp_path / "orch.db", tmp_path / "none.log", now=20_000.0)
    flagged = {(item["ticket_id"], item["detail"]) for item in snapshot["attention"]["items"]
               if item["kind"] == "needs-attention"}
    assert flagged == {
        ("4", "blocker-cycle: waiting on a blocker cycle: #4 -> #5 -> #4"),
        ("5", "blocker-cycle: waiting on a blocker cycle: #5 -> #4 -> #5"),
    }


def test_dashboard_snapshot_shows_waiting_reasons(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot

    waiting_chain(tmp_path)
    snapshot = collect_snapshot(tmp_path / "orch.db", tmp_path / "none.log", now=20_000.0)
    assert snapshot["feeder"]["waiting_blocked"] == 4
    items = {item["issue"]: item for item in snapshot["feeder"]["items"]}
    assert items["2"]["state"] == "waiting-blocked"
    assert items["2"]["reason"] == "waiting on #1"
    assert items["1"]["reason"] is None


def test_dashboard_page_renders_the_feeder_reason(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot, render_pages
    from lib.dashboard_page import SCRIPT

    waiting_chain(tmp_path)
    page = render_pages(
        collect_snapshot(tmp_path / "orch.db", tmp_path / "none.log", now=20_000.0)
    )["index.html"]
    assert ('<div class="iblock"><div class="ihead"><span class="ticket">app#1</span><span class="what">2 waiting</span>'
            '</div><div class="ichips"><span class="chip muted" title="Issue 3 · waiting on file lib/dashboard.py held by #1">app#3</span>'
            '<span class="chip muted" title="Issue 2 · waiting on #1">app#2</span>'
            '</div></div>') in page
    assert "4 on a blocker or files" in page
    assert "x.reason || FEEDER_STATE[x.state]" in SCRIPT


def test_a_released_blocker_still_counts_through_its_merged_pr(tmp_path: Path) -> None:
    # #1 holds lib/x.py; its PR merges into a non-default base, so #1 stays open.
    feeder, _, github, _ = make_feeder(tmp_path, box=1)
    github.issues[HOME] = [issue(1, created_at=1.0, body="Touches: lib/x.py"),
                           issue(2, created_at=2.0, body="Blocked by #1")]
    github.statuses = {ref(1): OPEN, ref(501): MERGED}
    feeder.feed_once()  # #1 fed as PR 501; #2 waits on #1
    feeder.feed_once()  # 501 merged: #1 released; #2 unblocked but the box is full
    assert rows(tmp_path / "orch.db")["1"]["released"] == 1
    assert states(tmp_path)["2"] == ("waiting-capacity", None)
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute(f"UPDATE {FEEDER_TABLE} SET fed_at = 0 WHERE issue = '1'")
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]


# --- #157 review round 1 --------------------------------------------------------


def test_an_unreadable_external_conflict_keeps_the_ticket_waiting(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(1, body="Conflicts with PR #113")]
    github.statuses = {ref(113): OPEN_PULL}
    assert not feeder.feed_once().fed
    github.fail["statuses"] = 1
    report = feeder.feed_once()
    assert not report.fed
    assert states(tmp_path)["1"] == ("waiting-blocked", "waiting on #113 (status unreadable)")


def test_a_held_conflict_matches_the_candidates_existing_pr(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.pulls[(HOME, 113)] = {"head": "factory/issue-2", "state": "closed"}
    github.issues[HOME] = [issue(1, body="Conflicts with PR #113")]
    github.statuses = {ref(113): CLOSED_PULL}
    assert len(feeder.feed_once().fed) == 1
    github.pulls[(HOME, 113)]["state"] = "open"
    github.statuses = {ref(113): OPEN_PULL, ref(501): OPEN_PULL}
    github.issues[HOME].append(issue(2, created_at=2000))
    report = feeder.feed_once()
    assert not report.fed
    assert report.blocked == 1
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on #1 (conflicts with it)")


def test_linear_cycle_across_projects_flags_both(tmp_path: Path) -> None:
    class CrossProject:
        def linear_intake(self, project_ids: frozenset[str], state: str, assignees) -> list:
            if project_ids == frozenset({"proj-1"}):
                return [replace(linear_issue("AUT-1"), blocked_by=(("AUT-2", "started"),))]
            return [replace(linear_issue("AUT-2"), repo="acme/other-app",
                            blocked_by=(("AUT-1", "started"),))]

    orch = LINEAR_ORCH + (
        "  - repo: acme/other-app\n"
        "    factory_ref: dev\n"
        "    linear_project_id: proj-2\n"
        "    intake:\n"
        "      linear_state: In Progress\n"
    )
    feeder, queue, _, _ = make_feeder(tmp_path, orch=orch, factory_text=LINEAR_FACTORY_MD,
                                      linear=CrossProject())
    report = feeder.feed_once()
    with queue.connect() as connection:
        flags = sorted(tuple(row) for row in connection.execute(
            "SELECT repo, ticket_id, reason FROM needs_attention"))
    assert (len(report.fed), report.blocked) == (0, 2)
    assert flags == [
        ("acme/linear-app", "AUT-1", "blocker-cycle"),
        ("acme/other-app", "AUT-2", "blocker-cycle"),
    ]


def test_linear_blocker_in_flight_in_another_project_counts_through_its_pr(
    tmp_path: Path,
) -> None:
    class CrossProject(FakeLinear):
        def __init__(self) -> None:
            super().__init__()
            self.second: list = []

        def linear_intake(self, project_ids: frozenset[str], state: str, assignees) -> list:
            return self.issues if project_ids == frozenset({"proj-1"}) else self.second

    orch = LINEAR_ORCH + (
        "  - repo: acme/other-app\n"
        "    factory_ref: dev\n"
        "    linear_project_id: proj-2\n"
        "    intake:\n"
        "      linear_state: In Progress\n"
    )
    linear = CrossProject()
    linear.issues = [linear_issue("AUT-1")]
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, orch=orch, factory_text=LINEAR_FACTORY_MD,
                                       linear=linear, now=now)
    assert len(feeder.feed_once().fed) == 1
    pull = int(rows(tmp_path / "orch.db")["AUT-1"]["pr_number"])
    linear.second = [replace(linear_issue("AUT-2"), repo="acme/other-app",
                             blocked_by=(("AUT-1", "started"),))]
    github.statuses = {github_ref("acme/linear-app", pull): MERGED}
    now[0] += 60
    assert [item.ticket_id for item in feeder.feed_once().fed] == ["AUT-2"]


def test_readers_accept_the_feeder_schema_before_migration(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from lib.status import SQLiteStatusReader

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as connection:
        ensure_tables(connection)
        for column in ("waiting_reason", "touches", "conflicts", "released"):
            connection.execute(f"ALTER TABLE {FEEDER_TABLE} DROP COLUMN {column}")
        connection.execute(
            f"INSERT INTO {FEEDER_TABLE} (repo, issue, tracker, title, harness, state, risk, "
            "issue_created_at, first_seen_at, seen_at) "
            "VALUES ('owner/app', '3', 'github', 't', 'claude', 'eligible', 'low', 1, 1, 10000)"
        )
        connection.execute("INSERT INTO feeder_poll(id, at) VALUES (1, 10000)")
    state = SQLiteStatusReader(db).feeder()
    assert state is not None and [item.reason for item in state.items] == [None]
    collect_snapshot(db, tmp_path / "none.log", now=10000)


@pytest.mark.parametrize("reused", [True, False])
def test_a_ticket_selected_this_poll_holds_under_its_pr_number(tmp_path: Path, reused) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    number = 113 if reused else 501
    if reused:
        github.pulls[(HOME, 113)] = {"head": "factory/issue-1", "base": "main", "state": "open"}
        github.statuses = {ref(113): CLOSED_PULL}
    github.issues[HOME] = [
        issue(1, created_at=1000),
        issue(2, created_at=2000, body=f"Conflicts with PR #{number}"),
    ]
    report = feeder.feed_once()
    assert [item.ticket_id for item in report.fed] == [str(number)]
    assert report.blocked == 1
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on #1 (conflicts with it)")


def test_a_resumed_feed_whose_pr_closed_releases_its_hold_this_poll(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [issue(1, body="Touches: lib/a.py")]
    github.fail["labels"] = 1
    assert feeder.feed_once().errors == 1
    github.pulls[(HOME, 501)]["state"] = "closed"
    github.statuses = {ref(501): CLOSED_PULL}
    github.issues[HOME].append(issue(2, created_at=2000, body="Touches: lib/a.py"))
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]


RED_SHA = "c0ffee12" + "0" * 32
MAIN = (HOME, "main")


def check(name: str, conclusion: str | None, run_id: str = "900", head: str = RED_SHA) -> BaseCheck:
    return BaseCheck(name, conclusion, run_id, "1", head)


def test_a_red_required_check_stops_the_line_at_the_newest_red_head() -> None:
    red = main_red(
        {
            "test (2/3)": check("test (2/3)", "failure", "901"),
            "test (1/3)": check("test (1/3)", "cancelled", "900", "f" * 40),
            "test (3/3)": check("test (3/3)", "success"),
            "lint": check("lint", "failure"),
        },
        ("test (*)",),
    )
    assert red is not None
    assert line_stop(red.wait, ("factory:ready",)) == Wait(
        "main is red at c0ffee12: test (1/3), test (2/3)"
    )


@pytest.mark.parametrize(
    ("checks", "required"),
    [
        ({"lint": check("lint", "failure")}, ("test (*)",)),
        ({"test (1/3)": check("test (1/3)", "success")}, ("test (*)",)),
        ({"test (1/3)": check("test (1/3)", None)}, ("test (*)",)),
        ({"test (1/3)": check("test (1/3)", "failure")}, ()),
        ({}, ("test (*)",)),
    ],
)
def test_main_is_not_red_without_a_red_required_check(checks, required) -> None:
    assert main_red(checks, required) is None


def test_a_main_fix_ticket_passes_a_stopped_line() -> None:
    red = main_red({"test (1/3)": check("test (1/3)", "failure")}, ("test (*)",))
    assert red is not None
    assert line_stop(red.wait, ("factory:ready", "Factory:Main-Fix")) is None


def test_a_red_main_starts_only_its_main_fix_but_finishes_one_in_flight(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD, box=4)
    github.issues[HOME] = [issue(1, created_at=1000)]
    github.fail["pull"] = 1
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["1"][0] == "error"
    github.main_checks[MAIN] = {"test (2/3)": check("test (2/3)", "failure")}
    github.issues[HOME] += [issue(2, created_at=2000), issue(3, created_at=3000)]
    github.check_reads.clear()

    report = feeder.feed_once()

    assert fed_heads(github, report) == ["factory/issue-1", "factory/issue-900"]
    assert report.blocked == 2
    stopped = ("waiting-blocked", "main is red at c0ffee12: test (2/3)")
    assert [states(tmp_path)[key] for key in ("2", "3")] == [stopped, stopped]
    assert github.check_reads == [MAIN]
    github.main_checks[MAIN] = {"test (2/3)": check("test (2/3)", "success", "902")}
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2", "factory/issue-3"]


def test_a_main_fix_ticket_is_fed_while_main_is_red(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    github.main_checks[MAIN] = {"test (1/3)": check("test (1/3)", "failure")}
    github.issues[HOME] = [issue(1), issue(2, labels=("factory:ready", "factory:main-fix"))]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2", "factory/issue-900"]


def test_an_unreadable_main_ci_keeps_the_line_stopped(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    github.issues[HOME] = [issue(1)]
    github.fail["base_checks"] = 1
    report = feeder.feed_once()
    assert (report.fed, report.errors) == ((), 1)
    assert states(tmp_path)["1"] == ("waiting-blocked", "main CI unreadable for main")
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]


def test_a_project_without_required_checks_never_reads_main_ci(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.main_checks[MAIN] = {"test (1/3)": check("test (1/3)", "failure")}
    github.issues[HOME] = [issue(1)]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    assert github.check_reads == []


def test_a_red_main_stops_a_ticket_whose_failed_feed_never_began(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    github.issues[HOME] = [issue(1)]
    github.fail["branch_sha"] = 1
    assert feeder.feed_once().errors == 1
    row = rows(tmp_path / "orch.db")["1"]
    assert (row["state"], row["branch"], row["pr_number"]) == ("error", None, None)
    github.main_checks[MAIN] = {"test (1/3)": check("test (1/3)", "failure")}
    github.check_reads.clear()

    report = feeder.feed_once()

    assert (fed_heads(github, report), report.blocked) == (["factory/issue-900"], 1)
    assert github.check_reads == [MAIN]
    assert states(tmp_path)["1"] == ("waiting-blocked", "main is red at c0ffee12: test (1/3)")


def test_a_red_main_lets_a_begun_feed_finish_after_a_stack_hold(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, factory_text=REQUIRED_MD)
    github.issues[HOME] = [issue(1)]
    github.fail["labels"] = 1
    assert feeder.feed_once().errors == 1
    assert rows(tmp_path / "orch.db")["1"]["pr_number"] == "501"
    shared = "a" * 40
    github.branch_commits[(HOME, "factory/issue-1")] = (shared, "c" * 40)
    github.branches[(HOME, "factory/issue-108")] = shared
    github.branch_commits[(HOME, "factory/issue-108")] = (shared,)
    github.pulls[(HOME, 113)] = {"head": "factory/issue-108", "base": "main", "title": "t",
                                 "body": "", "draft": True}
    feeder.feed_once()
    assert states(tmp_path)["1"][0] == "waiting-blocked"
    github.pulls[(HOME, 113)]["state"] = "closed"
    github.main_checks[MAIN] = {"test (1/3)": check("test (1/3)", "failure")}

    report = feeder.feed_once()

    assert [ref.ticket_id for ref in report.fed] == ["501", "502"]
    assert rows(tmp_path / "orch.db")["1"]["state"] == "fed"


# --- stacked targets (#1449) --------------------------------------------------


def test_stacked_lines_drops_only_the_parent() -> None:
    lines = GateLines(blocked_by=frozenset({ref(6), ref(9)}), touches=frozenset({"lib/x.py"}))

    stacked = stacked_lines(lines, frozenset({ref(6), ref(113)}))

    assert stacked == GateLines(blocked_by=frozenset({ref(9)}), touches=frozenset({"lib/x.py"}))


def test_stacked_lines_is_none_when_no_blocker_names_the_parent() -> None:
    assert stacked_lines(blocked(9), frozenset({ref(6), ref(113)})) is None
    assert stacked_lines(GateLines(), frozenset({ref(113)})) is None


PARENT_HEAD = "factory/issue-6"


def child(number: int, depends: str, *, target: bool = True) -> object:
    extra = f"Target branch: {PARENT_HEAD}" if target else ""
    return issue(number, created_at=float(number), body=body(depends=depends, extra=extra), template=False)


def with_open_parent(tmp_path: Path):
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [issue(6, created_at=6.0)]
    assert fed_heads(github, feeder.feed_once()) == [PARENT_HEAD]
    github.statuses = {ref(6): OPEN, ref(501): OPEN_PULL, ref(9): OPEN}
    return feeder, github


def test_a_stacked_github_child_feeds_while_its_parent_pr_is_open(tmp_path: Path) -> None:
    feeder, github = with_open_parent(tmp_path)
    github.issues[HOME] = [child(7, "Depends on: #6"), child(8, "Depends on: #6", target=False)]

    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-7"]

    assert github.pulls[(HOME, 502)]["base"] == PARENT_HEAD
    assert states(tmp_path)["7"] == ("fed", None)
    assert states(tmp_path)["8"] == ("waiting-blocked", "waiting on #6")


def test_a_child_naming_the_parent_pr_feeds_while_it_is_open(tmp_path: Path) -> None:
    feeder, github = with_open_parent(tmp_path)
    github.issues[HOME] = [child(7, "Depends on: PR #501")]

    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-7"]


def test_the_same_child_without_a_target_waits_on_its_open_parent(tmp_path: Path) -> None:
    feeder, github = with_open_parent(tmp_path)
    github.issues[HOME] = [child(7, "Depends on: #6", target=False)]

    assert feeder.feed_once().fed == ()

    assert states(tmp_path)["7"] == ("waiting-blocked", "waiting on #6")


def test_a_second_open_blocker_still_holds_a_stacked_child(tmp_path: Path) -> None:
    feeder, github = with_open_parent(tmp_path)
    github.issues[HOME] = [child(7, "Depends on: #6, #9")]

    assert feeder.feed_once().fed == ()

    assert states(tmp_path)["7"] == ("waiting-blocked", "waiting on #9")
    github.statuses[ref(9)] = DONE
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-7"]


@pytest.mark.parametrize("depends", ["Depends on: none", "Depends on: #9"])
def test_a_stacked_target_without_depends_on_the_parent_is_refused(tmp_path: Path, depends: str) -> None:
    feeder, github = with_open_parent(tmp_path)
    github.issues[HOME] = [child(7, depends)]

    report = feeder.feed_once()
    feeder.feed_once()

    assert report.fed == () and report.refused == 1
    state, reason = states(tmp_path)["7"]
    assert state == "refused-intake"
    assert reason == (
        f"`Target branch: {PARENT_HEAD}` is the head of open PR #501, "
        "but no `Depends on` line names #501 or its ticket"
    )
    assert [json.loads(wake["detail"])["defects"] for wake in wakes(tmp_path)] == [["stack-without-depends"]]


def test_a_target_whose_pr_merged_gates_as_usual(tmp_path: Path) -> None:
    feeder, github = with_open_parent(tmp_path)
    github.pulls[(HOME, 501)]["merged"] = True
    github.pulls[(HOME, 501)]["state"] = "closed"
    github.statuses[ref(501)] = MERGED
    github.issues[HOME] = [child(7, "Depends on: none")]

    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-7"]


def test_an_unread_stack_parent_holds_the_child(tmp_path: Path) -> None:
    feeder, github = with_open_parent(tmp_path)
    github.issues[HOME] = [child(7, "Depends on: none")]
    github.fail["find_pull"] = 1

    report = feeder.feed_once()

    assert report.fed == () and report.errors == 1
    assert states(tmp_path)["7"] == (
        "waiting-blocked", f"open PR on target branch {PARENT_HEAD} was not read"
    )
    feeder.feed_once()
    assert states(tmp_path)["7"][0] == "refused-intake"


def test_a_stacked_linear_child_feeds_while_its_parent_is_agent_reviewed(tmp_path: Path) -> None:
    linear = FakeLinear()
    linear.issues = [linear_issue("AUT-6")]
    feeder, _, github, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, linear=linear,
                                       factory_text=LINEAR_FACTORY_MD)
    assert [item.ticket_id for item in feeder.feed_once().fed] == ["AUT-6"]
    parent_head = linear_issue("AUT-6").branch
    stacked = replace(
        linear_issue("AUT-7"),
        body=body(extra=f"Target branch: {parent_head}"),
        blocked_by=(("AUT-6", "started"),),
    )
    unstacked = replace(linear_issue("AUT-8"), blocked_by=(("AUT-6", "started"),))
    linear.issues = [stacked, unstacked]

    assert [item.ticket_id for item in feeder.feed_once().fed] == ["AUT-7"]

    (pull,) = [pull for pull in github.pulls.values() if pull["head"] == stacked.branch]
    assert pull["base"] == parent_head
    assert states(tmp_path)["AUT-8"] == ("waiting-blocked", "waiting on AUT-6")
