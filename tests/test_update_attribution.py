"""#241: an update-from-main ready is charged to the job that produced it,
by handoff id, else to the newest update job that started. No path compares
a job's fractional created_at to GitHub's whole-second comment time."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib.device_pool import DevicePool, link_drop_detail
from lib.github_actions import _comment
from lib.job_state import (
    CLAIMED,
    FAILED_UNSTARTED,
    LINK_DROP,
    MAX_FREE_REQUEUES,
    REQUEUED_EARLY_EXIT,
    RUN_DIED,
    derive,
    fail_lagging,
    never_started,
    record_job_event,
    requeue_link_drop,
    requeue_unstarted,
)
from lib.machines_yml import Device, MachinesConfig
from lib.update_check import classify
from tests.merge_queue_support import Board, sha
from tests.test_dispatcher import READY
from tests.test_update_check import Repo


def _pool(board: Board) -> DevicePool:
    return DevicePool(
        board.queue,
        MachinesConfig(
            devices=(
                Device(
                    name="local-test",
                    slot_max=4,
                    harnesses={"claude": "claude-bot"},
                    local=True,
                ),
            )
        ),
    )


def _github_ready(pr, comment_id: str, at: float, head: str) -> None:
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pr.comments.append(
        _comment(
            {
                "id": comment_id,
                "user": {"login": "claude-login"},
                "body": READY.format(head=head),
                "created_at": stamp,
            }
        )
    )


@pytest.mark.parametrize('first_handoff_recorded', [False, True])
def test_late_ready_cannot_fail_retry_whose_worker_never_started(tmp_path: Path, first_handoff_recorded):
    board = Board(tmp_path)
    pr = board.open('42', sha(1))
    board.build_and_approve(pr, mergeable=False)
    pool = DevicePool(board.queue, MachinesConfig(devices=(Device(
        name='local-test', slot_max=4, harnesses={'claude':'claude-bot'}, local=True,
    ),)))
    first, _ = pool.claim('claude')
    pool.attach_worker(first.id, owner='claude', pid=os.getpid())
    assert not never_started(pool.placement(first.id))
    board.ready(pr, sha(1), 'first-old-ready')
    board.poll()
    retry, placement = pool.claim('claude')
    assert never_started(placement)
    assert derive(last_activity_at=retry.last_activity_at, now=board.now[0],
                  placement=placement, stale_seconds=120) == CLAIMED
    print('retry worker never started:', never_started(placement),
          'derived state:', derive(last_activity_at=retry.last_activity_at,
          now=board.now[0], placement=placement, stale_seconds=120))
    late = board.ready(pr, sha(1), 'first-late-old-ready')
    if first_handoff_recorded:
        board.queue.finish(first.id, owner='claude', handoff_comment_id=late)
    print('first', first.id, 'retry', retry.id, 'late attributed',
          board.dispatcher._update_job_before(board.snapshot(pr), pr.comments[-1]))
    board.poll()
    board.poll()
    print('outcome', board.outcome(late), 'failures', board.state('42')['update_failures'],
          'stuck', board.state('42')['stuck'])
    assert never_started(pool.placement(retry.id))
    assert board.state('42')['update_failures'] == 1
    assert not board.state('42')['stuck']


@pytest.mark.parametrize('delay', [.1, 1.1])
def test_new_ready_pending_ci_still_settles_update(tmp_path: Path, delay):
    repo = Repo(tmp_path/'git')
    approved, _ = repo.branches([('app.py', 'line 5\n', 'ours\n')],
                               [('app.py', 'line 5\n', 'theirs\n')])
    new = repo.merge_main(lambda r: r.git('checkout', '--ours', 'app.py'))
    assert classify(repo.gitdir, approved, new, 'refs/heads/main').trivial
    board = Board(tmp_path, classifier=lambda _repo, _base, old, head, _shared:
                  classify(repo.gitdir, old, head, 'refs/heads/main'))
    board.now[0] = 100.5
    pr = board.open('42', approved)
    board.build_and_approve(pr, mergeable=False)
    first = board.claim('claude')
    pr.push(new, ci='pending')
    board.now[0] = first.created_at + delay
    stamp = datetime.fromtimestamp(board.now[0], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    pr.comments.append(_comment({'id':'500','user':{'login':'claude-login'},
        'body':READY.format(head=new), 'created_at':stamp}))
    print('queued', first.created_at, 'ready', pr.comments[-1].created_at)
    board.queue.finish(first.id, owner='claude', handoff_comment_id=None)
    board.poll()
    print('pending ci: failures', board.state('42')['update_failures'], 'state', board.state('42')['workflow_state'])
    pr.ci[new] = 'success'
    board.poll()
    print('green ci: outcome', board.outcome('500'), 'state', board.state('42')['workflow_state'],
          'approved', board.state('42')['approved_head'], 'review count', len(board.jobs('review')))
    assert board.state('42')['approved_head'] == new
    assert len(board.jobs('review')) == 1
    assert board.actions.merges == [(True, new)]


@pytest.mark.parametrize("delay", [0.1, 1.1])
def test_one_failed_attempt_has_two_signals_same_second(tmp_path: Path, delay: float) -> None:
    board = Board(tmp_path)
    board.now[0] = 132.5
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    first = board.claim("claude")
    _github_ready(pr, "500", first.created_at + delay, sha(1))
    board.queue.finish(first.id, owner="claude", handoff_comment_id=None)
    board.poll()
    board.poll()

    row = board.state("42")
    assert (row["update_failures"], row["stuck"]) == (1, 0)
    assert [job["state"] for job in board.jobs("update-from-main")] == ["failed", "queued"]
    assert board.queue.claim("claude").request.action == "update-from-main"


def test_late_ready_from_first_running_job_is_not_a_second_attempt(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.claim("claude")
    board.ready(pr, sha(1), "first-old-ready")
    board.poll()
    assert [job["state"] for job in board.jobs("update-from-main")] == ["running", "queued"]
    board.ready(pr, sha(1), "first-late-old-ready")
    board.poll()

    row = board.state("42")
    assert (row["update_failures"], row["stuck"]) == (1, 0)
    assert [job["state"] for job in board.jobs("update-from-main")] == ["running", "queued"]


def test_a_started_pool_retrys_own_old_head_ready_is_the_second_failure(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    pool = _pool(board)
    first, _ = pool.claim("claude")
    pool.attach_worker(first.id, owner="claude", pid=os.getpid())
    board.ready(pr, sha(1), "first-old-ready")
    board.poll()
    retry, _ = pool.claim("claude")
    pool.attach_worker(retry.id, owner="claude", pid=os.getpid())
    board.ready(pr, sha(1), "retry-old-ready")
    board.poll()

    row = board.state("42")
    assert (row["update_failures"], row["stuck"], row["stuck_reason"]) == (
        2,
        1,
        "update-failed-2",
    )


def _attributed(board: Board, pr) -> str | None:
    return board.dispatcher._update_job_before(board.snapshot(pr), pr.comments[-1])


def test_the_recorded_handoff_id_wins_over_a_newer_started_job(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    pool = _pool(board)
    first, _ = pool.claim("claude")
    pool.attach_worker(first.id, owner="claude", pid=os.getpid())
    board.ready(pr, sha(1), "first-old-ready")
    board.poll()
    retry, _ = pool.claim("claude")
    pool.attach_worker(retry.id, owner="claude", pid=os.getpid())
    late = board.ready(pr, sha(1), "first-late-old-ready")
    assert _attributed(board, pr) == retry.id
    board.queue.finish(first.id, owner="claude", handoff_comment_id=late)
    assert _attributed(board, pr) == first.id


def test_the_fallback_skips_jobs_that_never_started(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    [queued] = board.jobs("update-from-main")
    board.ready(pr, sha(1), "orphan-ready")
    assert _attributed(board, pr) is None

    pool = _pool(board)
    first, placement = pool.claim("claude")
    assert first.id == queued["id"] and never_started(placement)
    assert _attributed(board, pr) is None

    pool.attach_worker(first.id, owner="claude", pid=os.getpid())
    assert _attributed(board, pr) == first.id


def test_a_job_failed_unstarted_is_never_charged(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    first = board.claim("claude")
    board.ready(pr, sha(1), "first-old-ready")
    board.poll()
    retry = board.claim("claude")
    with board.queue.connect() as connection:
        record_job_event(connection, retry.id, FAILED_UNSTARTED, "test", board.now[0])
        connection.commit()
    board.ready(pr, sha(1), "late-old-ready")
    assert _attributed(board, pr) == first.id


@pytest.mark.parametrize("delay", [0.1, 1.1])
def test_a_ready_from_before_the_update_was_queued_is_not_its_handoff(
    tmp_path: Path, delay: float
) -> None:
    board = Board(tmp_path)
    board.now[0] = 132.5
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.claim("claude")
    pr.push(sha(2), state="dirty")
    board.ready(pr, sha(2), "first-dirty-ready")
    board.poll()
    assert board.state("42")["update_failures"] == 1
    retry = board.claim("claude")
    board.now[0] = retry.created_at + delay
    board.queue.finish(retry.id, owner="claude", handoff_comment_id=None)
    board.poll()
    board.poll()

    row = board.state("42")
    assert (row["update_failures"], row["stuck_reason"]) == (2, "update-failed-2")
    assert board.jobs("update-from-main")[-1]["state"] == "failed"


def test_a_ready_handled_as_an_update_failure_is_no_later_jobs_handoff(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.claim("claude")
    pr.push(sha(2), state="dirty")
    board.ready(pr, sha(2), "first-dirty-ready")
    board.poll()
    assert board.outcome("first-dirty-ready") == "update-failed-1"
    retry = board.claim("claude")
    pr.push(sha(3), state="dirty")
    with board.queue.connect() as connection:
        connection.execute("UPDATE jobs SET head = ? WHERE id = ?", (sha(3), retry.id))
        connection.commit()
    board.queue.finish(retry.id, owner="claude", handoff_comment_id=None)
    board.poll()
    board.poll()

    row = board.state("42")
    assert (row["update_failures"], row["stuck_reason"]) == (2, "update-failed-2")
    assert board.jobs("update-from-main")[-1]["state"] == "failed"


def _link_drop_then_fail_unstarted(board: Board, pool: DevicePool, job_id: str) -> None:
    board.queue.finish(job_id, owner="claude", handoff_comment_id=None)
    with board.queue.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert requeue_link_drop(connection, job_id, now=board.now[0], reason="SSH link dropped")
        connection.commit()
    for _ in range(MAX_FREE_REQUEUES + 1):
        claimed, placement = pool.claim("claude")
        assert claimed.id == job_id and never_started(placement)
        with board.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = requeue_unstarted(
                connection,
                job_id,
                now=board.now[0],
                event=REQUEUED_EARLY_EXIT,
                reason="runner exited before harness start",
                slot_guard="harness = ?",
                slot_params=("claude",),
            )
            connection.commit()
    assert result[0] == FAILED_UNSTARTED


def test_a_job_that_ran_before_a_link_drop_keeps_its_late_ready(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    pool = _pool(board)
    first, _ = pool.claim("claude")
    pool.attach_worker(first.id, owner="claude", pid=os.getpid())
    board.ready(pr, sha(1), "first-old-ready")
    board.poll()
    _link_drop_then_fail_unstarted(board, pool, first.id)
    board.poll()
    board.poll()
    retry, placement = pool.claim("claude")
    assert retry.id != first.id and never_started(placement)
    late = board.ready(pr, sha(1), "first-late-old-ready")
    assert _attributed(board, pr) == first.id
    board.poll()
    assert board.outcome(late) == "update-failure-counted"
    row = board.state("42")
    assert (row["update_failures"], row["stuck"]) == (1, 0)


def test_a_ready_no_started_job_produced_charges_no_update_failure(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.ready(pr, sha(1), "orphan-old-ready")
    assert _attributed(board, pr) is None
    board.poll()
    assert board.outcome("orphan-old-ready") == "update-unattributed"
    row = board.state("42")
    assert (row["update_failures"], row["stuck"]) == (0, 0)

    pr.push(sha(2), state="dirty")
    board.ready(pr, sha(2), "orphan-dirty-ready")
    board.poll()
    assert board.outcome("orphan-dirty-ready") == "update-unattributed"
    row = board.state("42")
    assert (row["update_failures"], row["stuck"]) == (0, 0)


@pytest.mark.parametrize("keep_orphan", [True, False])
def test_an_unattributed_ready_is_no_later_workers_handoff(tmp_path: Path, keep_orphan: bool) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    pr.push(sha(2), state="dirty")
    board.ready(pr, sha(2), "orphan")
    board.poll()
    assert board.outcome("orphan") == "update-unattributed"
    if not keep_orphan:
        pr.comments = [c for c in pr.comments if c.id != "orphan"]
    pool = DevicePool(
        board.queue,
        MachinesConfig(
            devices=(
                Device(
                    name="remote-test",
                    tailscale_name="test-node",
                    slot_max=4,
                    harnesses={"claude": "claude-bot"},
                    workdir_root="/tmp/factory/{bot}",
                ),
            )
        ),
    )
    job, _ = pool.claim("claude")
    tag = pool.attach_worker(job.id, owner="claude", remote=True)
    with board.queue.connect() as connection:
        record_job_event(connection, job.id, RUN_DIED, "remote follow exited", board.now[0])
        record_job_event(
            connection, job.id, LINK_DROP, link_drop_detail("remote-test", "10 minutes", tag), board.now[0]
        )
        connection.commit()
    board.queue.finish(job.id, owner="claude", handoff_comment_id=None)
    board.poll()
    board.now[0] += 61
    board.poll()
    board.poll()

    jobs = [(r["id"] == job.id, r["state"], r["handoff_comment_id"]) for r in board.jobs("update-from-main")]
    assert board.state("42")["update_failures"] == 0
    assert jobs == [(True, "queued", None)]


def test_a_ready_after_a_lagging_refusal_charges_no_update_failure(tmp_path: Path) -> None:
    """#357: a start the preflight refused for a lagging checkout never ran."""
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    pool = _pool(board)
    update, placement = pool.claim("claude")
    assert never_started(placement)
    with board.queue.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert fail_lagging(
            connection,
            update.id,
            now=board.now[0],
            reason="preflight-lagging: box behind origin/main",
            slot_guard="harness = ?",
            slot_params=("claude",),
        )
        connection.commit()
    board.ready(pr, sha(1), "old-ready")
    assert _attributed(board, pr) is None
    board.poll()
    assert board.outcome("old-ready") == "update-unattributed"
    row = board.state("42")
    assert (row["update_failures"], row["stuck"]) == (0, 0)
