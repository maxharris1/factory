"""Idle-blocked-ready (issue #1262): a GitHub-ticket project with nothing in
flight whose ready tickets all wait on open blockers raises a flag and wakes
its Grok bot once per stall."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib.device_pool import DevicePool
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_feeder import issue, make_feeder, rows
from tests.test_feeder_gates import CLOSED_PULL, DONE, HOME, OPEN, OPEN_PULL, ref

IDLE = "idle-blocked-ready"


def _attention(tmp_path: Path, reason: str) -> dict[tuple[str, str], str]:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        return {
            (repo, ticket): detail
            for repo, ticket, detail in connection.execute(
                "SELECT repo, ticket_id, detail FROM needs_attention WHERE reason = ?", (reason,)
            )
        }


def _wakes(queue: SQLiteJobQueue) -> list[sqlite3.Row]:
    with queue.connect() as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM jobs WHERE recipient = 'grok' AND source_comment_id LIKE 'feeder-idle:%'"
        ).fetchall()


def _job(queue: SQLiteJobQueue, key: str, *, repo: str = HOME, state: str = "queued") -> str:
    job = queue.enqueue(
        JobRequest(repo=repo, ticket_id=key, action="build", recipient="claude",
                   source_comment_id=key, timeout_seconds=600),
        dedupe_key=key,
    )
    with queue.connect() as connection:
        connection.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job.id))
    return job.id


def _coo(tmp_path: Path, *, pull_open: bool = False, clock: list[float] | None = None):
    """#18 fed and done, its PR closed, the issue still open; #19 ready and
    depending on it."""
    feeder, queue, github, linear = make_feeder(tmp_path, box=2, now=clock)
    github.issues[HOME] = [issue(18)]
    (fed,) = feeder.feed_once().fed
    pull = int(fed.ticket_id)
    build = _job(queue, fed.ticket_id)
    feeder.feed_once()
    with queue.connect() as connection:
        connection.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (build,))
    if not pull_open:
        github.pulls[(HOME, pull)]["state"] = "closed"
    github.statuses = {ref(18): OPEN, ref(pull): OPEN_PULL if pull_open else CLOSED_PULL}
    github.issues[HOME] = [
        issue(18),
        issue(19, body="Depends on: #18\nFix the thing in lib/y.py."),
    ]
    return feeder, queue, github, linear


def test_coo_shape_raises_the_flag_and_wakes_grok_once(tmp_path: Path) -> None:
    feeder, queue, _, _ = _coo(tmp_path)
    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db")["18"]["state"] == "done"
    assert rows(tmp_path / "orch.db")["19"]["state"] == "waiting-blocked"
    flags = _attention(tmp_path, IDLE)
    assert list(flags) == [(HOME, "19")]
    assert "#18" in flags[(HOME, "19")]
    assert _attention(tmp_path, "critical-path-blocker") == {}
    (wake,) = _wakes(queue)
    assert (wake["repo"], wake["action"], wake["state"]) == (HOME, "blocked", "queued")
    assert '"project_bot": "grok-bot"' in wake["detail"]
    assert "#19" in wake["detail"] and "#18" in wake["detail"]

    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "19")]
    assert len(_wakes(queue)) == 1


def test_one_row_per_blocked_ticket_naming_only_its_open_blockers(tmp_path: Path) -> None:
    feeder, queue, github, _ = _coo(tmp_path)
    github.statuses[ref(30)] = DONE
    github.issues[HOME].append(issue(20, body="Depends on: #18, #30\nBlocked by #31\nlib/z.py"))
    github.statuses[ref(31)] = OPEN
    feeder.feed_once()
    flags = _attention(tmp_path, IDLE)
    assert sorted(flags) == [(HOME, "19"), (HOME, "20")]
    assert "#18" in flags[(HOME, "20")] and "#31" in flags[(HOME, "20")]
    assert "#30" not in flags[(HOME, "20")]
    assert len(_wakes(queue)) == 1


def test_the_flag_clears_when_the_blocker_closes(tmp_path: Path) -> None:
    feeder, queue, github, _ = _coo(tmp_path)
    feeder.feed_once()
    github.statuses[ref(18)] = DONE
    report = feeder.feed_once()
    assert report.fed != ()
    assert _attention(tmp_path, IDLE) == {}
    assert len(_wakes(queue)) == 1


def test_a_queued_job_clears_the_flag_and_the_next_stall_wakes_again(tmp_path: Path) -> None:
    clock = [10_000.0]
    feeder, queue, _, _ = _coo(tmp_path, clock=clock)
    feeder.feed_once()
    job = _job(queue, "other-work")
    feeder.feed_once()
    assert _attention(tmp_path, IDLE) == {}
    feeder.feed_once()
    assert len(_wakes(queue)) == 1
    with queue.connect() as connection:
        connection.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (job,))
    clock[0] += 60
    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "19")]
    assert len(_wakes(queue)) == 2


@pytest.mark.parametrize("state", ["queued", "running"])
def test_a_non_grok_job_in_flight_keeps_the_flag_down(tmp_path: Path, state: str) -> None:
    feeder, queue, _, _ = _coo(tmp_path)
    _job(queue, "busy", state=state)
    feeder.feed_once()
    assert _attention(tmp_path, IDLE) == {}
    assert _wakes(queue) == []


def test_a_job_in_another_project_does_not_count(tmp_path: Path) -> None:
    feeder, queue, _, _ = _coo(tmp_path)
    _job(queue, "elsewhere", repo="other/repo")
    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "19")]


def test_an_open_factory_pr_keeps_the_flag_down(tmp_path: Path) -> None:
    feeder, queue, _, _ = _coo(tmp_path, pull_open=True)
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["19"]["state"] == "waiting-blocked"
    assert _attention(tmp_path, IDLE) == {}
    assert _wakes(queue) == []


def test_tickets_waiting_on_capacity_keep_the_flag_down(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    _job(queue, "elsewhere", repo="other/repo")
    assert DevicePool(queue, feeder.machines).claim("claude") is not None
    github.issues[HOME] = [issue(1), issue(2, body="Depends on: #900\nlib/y.py")]
    github.statuses = {ref(900): OPEN}
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["1"]["state"] == "waiting-capacity"
    assert rows(tmp_path / "orch.db")["2"]["state"] == "waiting-blocked"
    assert _attention(tmp_path, IDLE) == {}
    assert _wakes(queue) == []


def test_a_failed_open_pr_read_keeps_the_flag_as_it_was(tmp_path: Path) -> None:
    feeder, queue, github, _ = _coo(tmp_path)

    def broken(repo: str):
        raise RuntimeError("fake GitHub failure in open pulls")

    read = github.open_pull_commits
    github.open_pull_commits = broken
    feeder.feed_once()
    assert _attention(tmp_path, IDLE) == {}
    assert _wakes(queue) == []

    github.open_pull_commits = read
    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "19")]

    github.open_pull_commits = broken
    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "19")]
    assert len(_wakes(queue)) == 1


def _blocked_on_90(tmp_path: Path, clock: list[float]):
    feeder, queue, github, _ = make_feeder(tmp_path, now=clock)
    github.statuses[ref(90)] = OPEN
    github.issues[HOME] = [issue(1, body="Blocked by: #90")]
    return feeder, queue, github


def test_a_stall_keeps_one_wake_while_its_blocked_tickets_change(tmp_path: Path) -> None:
    clock = [10_000.0]
    feeder, queue, github = _blocked_on_90(tmp_path, clock)
    feeder.feed_once()
    clock[0] += 60
    github.issues[HOME].append(issue(2, body="Blocked by: #90"))
    feeder.feed_once()
    clock[0] += 60
    github.issues[HOME] = [issue(2, body="Blocked by: #90")]
    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "2")]
    assert len(_wakes(queue)) == 1


def test_a_job_that_starts_and_ends_between_polls_ends_the_stall(tmp_path: Path) -> None:
    clock = [10_000.0]
    feeder, queue, _ = _blocked_on_90(tmp_path, clock)
    feeder.feed_once()
    wake = queue.claim("grok")
    queue.finish(wake.id, owner="grok", handoff_comment_id="delivered")
    clock[0] += 10
    build = _job(queue, "other-work")
    assert queue.claim("claude").id == build
    clock[0] += 10
    queue.finish(build, owner="claude", handoff_comment_id="finished")
    clock[0] += 40
    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "1")]
    assert len(_wakes(queue)) == 2
    clock[0] += 60
    feeder.feed_once()
    assert len(_wakes(queue)) == 2


def test_an_open_factory_branch_pr_the_feeder_never_opened_keeps_the_flag_down(
    tmp_path: Path,
) -> None:
    feeder, queue, github = _blocked_on_90(tmp_path, [10_000.0])
    github.pulls[(HOME, 700)] = {"head": "factory/issue-18", "base": "main"}
    feeder.feed_once()
    assert _attention(tmp_path, IDLE) == {}
    assert _wakes(queue) == []


def _track(tmp_path: Path, number: int, state: str) -> None:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS ticket_state"
            " (repo TEXT NOT NULL, ticket_id TEXT NOT NULL, workflow_state TEXT)"
        )
        connection.execute("INSERT INTO ticket_state VALUES (?, ?, ?)", (HOME, str(number), state))


def test_an_open_pr_the_dispatcher_tracks_keeps_the_flag_down(tmp_path: Path) -> None:
    feeder, queue, github = _blocked_on_90(tmp_path, [10_000.0])
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO feeder_items (repo, issue, tracker, title, harness, state, risk,"
            " issue_created_at, pr_number, ticket_id, first_seen_at, seen_at, fed_at)"
            " VALUES (?, '5', 'github', 't', 'claude', 'done', 'low', 1, '501', '501', 1, 1, 1)",
            (HOME,),
        )
    github.pulls[(HOME, 501)] = {"head": "factory/issue-5", "base": "main", "state": "closed"}
    github.pulls[(HOME, 700)] = {"head": "human-branch", "base": "main"}
    _track(tmp_path, 700, "reviewing")
    feeder.feed_once()
    assert _attention(tmp_path, IDLE) == {}
    assert _wakes(queue) == []


def test_an_open_pr_neither_factory_nor_tracked_does_not_count(tmp_path: Path) -> None:
    feeder, queue, github = _blocked_on_90(tmp_path, [10_000.0])
    github.pulls[(HOME, 700)] = {"head": "human-branch", "base": "main"}
    github.pulls[(HOME, 701)] = {"head": "other-branch", "base": "main"}
    _track(tmp_path, 701, "merged")
    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "1")]
    assert len(_wakes(queue)) == 1


def test_a_failed_open_pr_read_without_feeder_history_keeps_the_flag(tmp_path: Path) -> None:
    feeder, queue, github = _blocked_on_90(tmp_path, [10_000.0])

    def broken(repo: str):
        raise RuntimeError("fake GitHub failure in open pulls")

    read = github.open_pull_commits
    github.open_pull_commits = broken
    feeder.feed_once()
    assert _attention(tmp_path, IDLE) == {}
    assert _wakes(queue) == []

    github.open_pull_commits = read
    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "1")]

    github.open_pull_commits = broken
    feeder.feed_once()
    assert list(_attention(tmp_path, IDLE)) == [(HOME, "1")]
    assert len(_wakes(queue)) == 1
