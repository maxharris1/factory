from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from lib.feeder import IntakeIssue, IssueRecord, _pull_body
from lib.intake_live import GitHubIntakeClient
from tests.test_feeder import FakeGitHub, make_feeder
from tests.test_github_actions import RecordedGh
from tests.test_intake_live import ok, reference, search, timeline

REPO = "owner/app"
PARENT = 10
START = 10_000.0


def auto_part(part: int) -> str:
    return (
        f"Split from #{PARENT} (plan https://github.com/{REPO}/pull/9#issuecomment-77).\n\n"
        "Risk: high\n\n## Acceptance criteria\n\n- ac-1: x.\n\n"
        f"<!-- orch-split plan=77 part={part} -->\n"
    )


FOLLOW_UP = f"Found while building #12.\n\nSplit from #{PARENT}\n\n## Acceptance criteria\n\n- ac-1: y.\n"


def board(tmp_path: Path, parts: dict[int, str]):
    clock = [START]
    github = FakeGitHub()
    github.clock = lambda: clock[0]
    github.filed[(REPO, PARENT)] = {
        "title": "Parent", "body": "The big ticket.\n", "state": "open",
        "labels": ("factory:ready", "orch:state/abandoned"),
    }
    for number, body in parts.items():
        github.filed[(REPO, number)] = {
            "title": f"Part {number}", "body": body, "state": "open", "labels": ("factory:ready",),
        }
    feeder, queue, github, _ = make_feeder(tmp_path, github=github, now=clock)
    feeder.feed_once()
    return feeder, queue, github, clock


def close(github: FakeGitHub, clock: list[float], number: int, reason: str = "completed") -> None:
    clock[0] += 60
    github.filed[(REPO, number)].update(state="closed", reason=reason, closed_at=clock[0])


def poll(feeder, clock: list[float]) -> None:
    clock[0] += 60
    feeder.feed_once()


def parent(github: FakeGitHub) -> dict:
    return github.filed[(REPO, PARENT)]


def parent_comments(github: FakeGitHub) -> list[str]:
    return [comment.body for comment in github.posted.get((REPO, PARENT), [])]


def writes(github: FakeGitHub) -> list[str]:
    return [call for call in github.calls if call.startswith(("close:", "unlabel:"))]


def test_parent_closes_only_on_the_last_part_and_loses_its_labels(tmp_path: Path) -> None:
    feeder, _, github, clock = board(tmp_path, {11: auto_part(1), 12: auto_part(2)})

    close(github, clock, 11)
    poll(feeder, clock)

    assert parent(github)["state"] == "open"
    assert parent(github)["labels"] == ("factory:ready", "orch:state/abandoned")
    assert parent_comments(github) == []

    close(github, clock, 12)
    poll(feeder, clock)

    assert parent(github)["state"] == "closed"
    assert parent(github)["reason"] == "completed"
    assert "factory:ready" not in parent(github)["labels"]
    assert "orch:state/abandoned" not in parent(github)["labels"]
    [comment] = parent_comments(github)
    assert "#11" in comment and "#12" in comment and "merged" in comment


def test_an_open_follow_up_keeps_the_parent_open_until_it_closes(tmp_path: Path) -> None:
    feeder, _, github, clock = board(
        tmp_path, {11: auto_part(1), 12: auto_part(2), 13: FOLLOW_UP}
    )

    close(github, clock, 11)
    close(github, clock, 12)
    poll(feeder, clock)

    assert parent(github)["state"] == "open"
    assert parent_comments(github) == []

    close(github, clock, 13)
    poll(feeder, clock)

    assert parent(github)["state"] == "closed"
    assert parent(github)["reason"] == "completed"
    [comment] = parent_comments(github)
    assert "#13" in comment


def test_a_part_closed_not_planned_keeps_the_parent_open(tmp_path: Path) -> None:
    feeder, _, github, clock = board(tmp_path, {11: auto_part(1), 12: auto_part(2)})

    close(github, clock, 11, reason="not_planned")
    close(github, clock, 12)
    poll(feeder, clock)

    assert parent(github)["state"] == "open"
    assert writes(github) == []


def test_a_part_merged_in_orch_counts_as_done(tmp_path: Path) -> None:
    feeder, queue, github, clock = board(tmp_path, {11: auto_part(1), 12: auto_part(2)})
    with queue.connect() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS ticket_state "
            "(repo TEXT NOT NULL, ticket_id TEXT NOT NULL, workflow_state TEXT, "
            "PRIMARY KEY (repo, ticket_id))"
        )
        connection.execute(
            "INSERT INTO feeder_items (repo, issue, tracker, title, harness, state, risk, "
            "issue_created_at, first_seen_at, seen_at, ticket_id, pr_number) "
            "VALUES (?, '11', 'github', 'Part 11', 'claude', 'done', 'high', 1, 1, 1, '501', '501')",
            (REPO,),
        )
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) VALUES (?, '501', 'merged')",
            (REPO,),
        )

    close(github, clock, 12)
    poll(feeder, clock)

    assert parent(github)["state"] == "closed"


def merge_in_orch(queue, issue: int) -> None:
    with queue.connect() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS ticket_state "
            "(repo TEXT NOT NULL, ticket_id TEXT NOT NULL, workflow_state TEXT, "
            "PRIMARY KEY (repo, ticket_id))"
        )
        connection.execute(
            "INSERT INTO feeder_items (repo, issue, tracker, title, harness, state, risk, "
            "issue_created_at, first_seen_at, seen_at, ticket_id, pr_number) "
            "VALUES (?, ?, 'github', 'Part', 'claude', 'done', 'high', 1, 1, 1, ?, ?)",
            (REPO, str(issue), str(issue + 490), str(issue + 490)),
        )
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) VALUES (?, ?, 'merged')",
            (REPO, str(issue + 490)),
        )


def test_parts_merged_in_orch_whose_issues_stay_open_close_the_parent(tmp_path: Path) -> None:
    feeder, queue, github, clock = board(tmp_path, {11: auto_part(1), 12: auto_part(2)})
    poll(feeder, clock)

    merge_in_orch(queue, 11)
    poll(feeder, clock)
    assert parent(github)["state"] == "open"

    merge_in_orch(queue, 12)
    poll(feeder, clock)

    assert github.filed[(REPO, 11)]["state"] == github.filed[(REPO, 12)]["state"] == "open"
    assert parent(github)["state"] == "closed"
    assert "factory:ready" not in parent(github)["labels"]
    poll(feeder, clock)
    assert [call for call in github.calls if call.startswith("issue-record:")] == [
        f"issue-record:{REPO}#11", f"issue-record:{REPO}#12",
    ]


def test_a_follow_up_merged_after_its_siblings_closed_long_ago_closes_the_parent(
    tmp_path: Path,
) -> None:
    feeder, queue, github, clock = board(tmp_path, {11: auto_part(1), 12: FOLLOW_UP})
    close(github, clock, 11)
    for _ in range(5):
        poll(feeder, clock)
    assert parent(github)["state"] == "open"

    merge_in_orch(queue, 12)
    poll(feeder, clock)

    assert parent(github)["state"] == "closed"


def test_a_stale_open_sibling_is_rechecked_until_the_parent_closes(tmp_path: Path) -> None:
    feeder, _, github, clock = board(tmp_path, {11: auto_part(1), 12: auto_part(2)})
    close(github, clock, 11)
    for _ in range(4):
        poll(feeder, clock)
    fresh = github.split_children
    github.split_children = lambda repo, number: tuple(
        replace(child, state="open", reason=None) if child.number == 11 else child
        for child in fresh(repo, number)
    )
    close(github, clock, 12)
    for _ in range(4):
        poll(feeder, clock)
    assert parent(github)["state"] == "open"

    github.split_children = fresh
    searches = sum(call.startswith("split-children:") for call in github.calls)
    for _ in range(2):
        poll(feeder, clock)

    assert parent(github)["state"] == "closed"
    assert sum(call.startswith("split-children:") for call in github.calls) == searches + 1


def test_a_parent_waiting_on_a_follow_up_is_searched_at_most_every_two_minutes(
    tmp_path: Path,
) -> None:
    feeder, _, github, clock = board(tmp_path, {11: auto_part(1), 12: FOLLOW_UP})
    close(github, clock, 11)
    poll(feeder, clock)
    searches = sum(call.startswith("split-children:") for call in github.calls)

    for _ in range(10):
        clock[0] += 30
        feeder.feed_once()

    assert sum(call.startswith("split-children:") for call in github.calls) == searches + 2
    assert parent(github)["state"] == "open"


def done(number: int) -> dict:
    return {"number": number, "body": auto_part(number - 10), "state": "closed",
            "state_reason": "completed"}


def test_an_open_follow_up_the_search_index_misses_keeps_the_parent_open(
    tmp_path: Path,
) -> None:
    feeder, _, github, clock = board(
        tmp_path, {11: auto_part(1), 12: auto_part(2), 13: FOLLOW_UP}
    )
    gh = RecordedGh()
    gh.route(search(PARENT), ok(
        {"total_count": 2, "incomplete_results": False, "items": [done(11), done(12)]}
    ))
    gh.route(timeline(PARENT), ok([
        reference(11, auto_part(1), "closed", "completed"),
        reference(12, auto_part(2), "closed", "completed"),
        reference(13, FOLLOW_UP),
    ]))
    github.split_children = GitHubIntakeClient(gh).split_children
    close(github, clock, 11)
    close(github, clock, 12)
    poll(feeder, clock)

    assert parent(github)["state"] == "open"
    assert writes(github) == []


def test_the_closer_never_needs_fixes_parent_in_a_part(tmp_path: Path) -> None:
    part = IntakeIssue(
        repo=REPO, tracker="github", key="11", title="Part 11", body=auto_part(1),
        labels=("factory:ready",), created_at=1.0, applied_by="maxharris1",
        url=f"https://github.com/{REPO}/issues/11",
    )
    assert _pull_body(part).splitlines()[0] == "Fixes #11"
    assert f"#{PARENT}" not in _pull_body(part)

    feeder, _, github, clock = board(tmp_path, {11: auto_part(1), 12: auto_part(2)})
    for number in (11, 12):
        assert "Fixes" not in github.filed[(REPO, number)]["body"]
        close(github, clock, number)
    poll(feeder, clock)

    assert parent(github)["state"] == "closed"


def test_rerunning_the_closer_after_the_parent_closed_writes_nothing(tmp_path: Path) -> None:
    feeder, _, github, clock = board(tmp_path, {11: auto_part(1), 12: auto_part(2)})
    close(github, clock, 11)
    close(github, clock, 12)
    poll(feeder, clock)
    done = writes(github)
    assert parent(github)["state"] == "closed"

    poll(feeder, clock)
    poll(feeder, clock)

    assert writes(github) == done
    assert len(parent_comments(github)) == 1


def test_a_parent_already_closed_by_hand_is_left_alone(tmp_path: Path) -> None:
    feeder, _, github, clock = board(tmp_path, {11: auto_part(1), 12: auto_part(2)})
    parent(github).update(state="closed", reason="completed", closed_at=START)

    close(github, clock, 11)
    close(github, clock, 12)
    poll(feeder, clock)

    assert writes(github) == []
    assert parent_comments(github) == []
    assert parent(github)["labels"] == ("factory:ready", "orch:state/abandoned")


def test_a_failed_write_retries_next_poll_and_comments_once(tmp_path: Path) -> None:
    feeder, _, github, clock = board(tmp_path, {11: auto_part(1), 12: auto_part(2)})
    close(github, clock, 11)
    close(github, clock, 12)
    github.fail["unlabel"] = 1

    poll(feeder, clock)
    assert parent(github)["state"] == "open"

    clock[0] += 3600
    feeder.feed_once()

    assert parent(github)["state"] == "closed"
    assert len(parent_comments(github)) == 1
    assert "factory:ready" not in parent(github)["labels"]


def test_parts_closed_before_the_first_poll_never_trigger(tmp_path: Path) -> None:
    clock = [START]
    github = FakeGitHub()
    github.clock = lambda: clock[0]
    github.filed[(REPO, PARENT)] = {
        "title": "Parent", "body": "x\n", "state": "open", "labels": ("factory:ready",),
    }
    github.filed[(REPO, 11)] = {
        "title": "Part", "body": auto_part(1), "state": "closed", "reason": "completed",
        "closed_at": START - 86_400, "labels": (),
    }
    feeder, _, github, _ = make_feeder(tmp_path, github=github, now=clock)

    feeder.feed_once()
    poll(feeder, clock)

    assert parent(github)["state"] == "open"
    assert not any(call.startswith("split-children:") for call in github.calls)


def test_a_linear_project_reads_no_closed_issues(tmp_path: Path) -> None:
    from tests.test_feeder import LINEAR_FACTORY_MD, LINEAR_ORCH

    feeder, _, github, _ = make_feeder(tmp_path, orch=LINEAR_ORCH, factory_text=LINEAR_FACTORY_MD)
    feeder.feed_once()
    feeder.feed_once()

    assert not any(call.startswith("closed-since:") for call in github.calls)



def test_the_spec_records_the_rule_with_follow_ups() -> None:
    spec = (Path(__file__).resolve().parents[1] / "docs" / "SPEC.md").read_text(encoding="utf-8")
    [row] = [line for line in spec.splitlines() if line.startswith("| Nov 20 | A split parent closes")]
    assert "follow-up" in row and "`Fixes #<part>`" in row and "idempotent" in row
