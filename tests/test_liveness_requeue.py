"""#91: stale vs dead, unstarted requeue, early remote exits, the T6 verdict guard.

Every test uses a temp database and fakes: tests/test_job_state.py's
Poller (scripted pid checks, FakeTransport for SSH), tests/fake_ssh.py on
PATH for `orch job run` on a remote device, and test_dispatcher's tickets.
Nothing reaches a real host, a real harness CLI or the live orch.db.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from lib import cli, job_run, job_token, remote
from lib.dashboard import collect_snapshot
from lib.dispatcher import CIResult, Dispatcher
from lib.job_state import (
    DEAD,
    FAILED_UNSTARTED,
    MAX_FREE_REQUEUES,
    RELEASED_UNSTARTED,
    REQUEUED_EARLY_EXIT,
    RUN_DIED,
    RUN_EXITED,
    RUNNING,
    STALE,
    Liveness,
    derive,
    exit_reason,
)
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import (
    ABANDONED,
    READY,
    TRUSTED_LOGINS,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    seed_builder_job,
    setup,
    ticket,
)
from tests.test_job_state import CLAIMED_AT, Poller, placement
from tests.test_remote_start import ORCH, Ssh, orch

HEAD = "e88a661" + "0" * 33


# --- a long silent run under a live runner is not stale ----------------------------


def test_a_live_runner_keeps_a_silent_job_running_even_with_a_late_heartbeat() -> None:
    started = placement(runner_pid=11, worker_pid=22)
    # The heartbeat is 10 minutes old (a busy database), but the runner lives.
    assert derive(
        last_activity_at=400.0, now=1000.0, placement=started, stale_seconds=120,
        liveness=Liveness(runner_alive=True),
    ) == RUNNING
    # Runner gone while its worker may still run: its heartbeat stopped, stale.
    assert derive(
        last_activity_at=400.0, now=1000.0, placement=started, stale_seconds=120,
        liveness=Liveness(runner_alive=False, worker_gone=False),
    ) == STALE
    # Not checked (no pid check supplied): the heartbeat alone decides.
    assert derive(
        last_activity_at=400.0, now=1000.0, placement=started, stale_seconds=120,
    ) == STALE


def test_orch_job_run_heartbeats_while_a_silent_command_runs() -> None:
    # The command prints nothing for its whole run; the beats come anyway.
    beats: list[float] = []
    code = job_run.run(
        [sys.executable, "-c", "import time; time.sleep(1.2)"],
        beat=lambda: beats.append(time.monotonic()),
        interval=0.2,
        log=lambda _m: None,
    )
    assert code == 0
    assert len(beats) >= 4  # the first beat, then one every 0.2 s of silence


def test_a_long_silent_review_under_a_live_runner_is_not_marked_stale(tmp_path: Path) -> None:
    """factory#82: a review showed `stale 2m` minutes after its claim."""
    poller = Poller(tmp_path)
    job_id = poller.review(timeout=3600)
    poller.claim()
    poller.live.update({11, 12})
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    poller.pool.attach_worker(job_id, owner="codex", pid=12, start=None)
    poller.now[0] = CLAIMED_AT + 5
    poller.queue.heartbeat(job_id, owner="codex")

    poller.at(20 * 60)  # 20 minutes without a beat landing, runner alive
    assert poller.row(job_id)["state"] == "running"
    assert poller.events() == []
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    snapshot = collect_snapshot(
        poller.path, log, now=CLAIMED_AT + 20 * 60, alive=lambda pid, _s: pid in poller.live
    )
    (row,) = snapshot["running"]
    assert row["job_state"] == "running"
    assert snapshot["attention"]["count"] == 0
    assert snapshot["stuck"]["overdue_jobs"] == []


# --- a run that really died records why and is released ------------------------


def _box_poller(tmp_path: Path) -> tuple[Poller, str]:
    """A Poller whose clock is real time, so `orch job run` and it agree."""
    poller = Poller(tmp_path, now=time.time(), box_slots=3)  # most free slots: the box
    poller.queue.clock = time.time
    job_id = poller.review(timeout=3600)
    assert poller.claim().device == "box"
    return poller, job_id


def _job_run(poller: Poller, job_id: str, *command: str, env: dict | None = None):
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(poller.path),
        "--machines", str(poller.machines), "--interval", "0.2", "--", *command,
        env=env,
    )
    _, err = proc.communicate(timeout=60)
    return proc.returncode, err


def _details(poller: Poller) -> list[tuple[str, str]]:
    with poller.queue.connect() as connection:
        return [
            (str(row["event"]), str(row["detail"]))
            for row in connection.execute("SELECT * FROM job_events ORDER BY id")
        ]


def test_a_run_that_died_records_its_exit_reason_and_the_poller_fails_it_for_t6(
    tmp_path: Path,
) -> None:
    poller, job_id = _box_poller(tmp_path)
    code, err = _job_run(poller, job_id, sys.executable, "-c", "import sys; sys.exit(3)")
    assert code == 3, err
    assert poller.pool.placement(job_id) is None  # its worker is gone: slot freed
    assert poller.row(job_id)["state"] == "running"  # the bot still may call done
    events = _details(poller)
    assert events == [(RUN_DIED, "orch job run's command on box: exit 3")]

    # Within the stale window the poller waits for the bot's orch job done.
    last = float(poller.row(job_id)["last_activity_at"])
    poller.now[0] = last + 60
    poller.queue.clock = lambda: poller.now[0]
    poller.dispatcher.reconcile_jobs()
    assert poller.row(job_id)["state"] == "running"

    poller.now[0] = last + 121
    poller.dispatcher.reconcile_jobs()
    assert poller.row(job_id)["state"] == "failed"
    dead = [detail for event, detail in _details(poller) if event == "dead"]
    assert len(dead) == 1 and "exit 3" in dead[0] and "failed for T6" in dead[0]
    # T6 took it from there: one retry, attempt 2.
    assert [(state, attempt) for _id, state, attempt in poller.jobs()] == [
        ("failed", 1), ("queued", 2)
    ]


def test_a_command_that_exited_0_is_left_to_its_bots_orch_job_done(tmp_path: Path) -> None:
    poller, job_id = _box_poller(tmp_path)
    code, err = _job_run(poller, job_id, sys.executable, "-c", "pass")
    assert code == 0, err
    assert [event for event, _d in _details(poller)] == [RUN_EXITED]
    last = float(poller.row(job_id)["last_activity_at"])
    poller.now[0] = last + 10 * 60
    poller.queue.clock = lambda: poller.now[0]
    poller.dispatcher.reconcile_jobs()
    assert poller.row(job_id)["state"] == "running"  # only the timeout ends it


def test_a_finished_job_records_no_run_end(tmp_path: Path) -> None:
    poller, job_id = _box_poller(tmp_path)
    script = (
        "import subprocess, sys; "
        f"subprocess.run([sys.executable, {str(Path(__file__).parents[1] / 'bin' / 'orch')!r}, "
        f"'job', 'done', 'codex-bot', {job_id!r}, '--handoff', '77', '--db', "
        f"{str(poller.path)!r}], check=True); sys.exit(1)"
    )
    code, err = _job_run(poller, job_id, sys.executable, "-c", script)
    assert code == 1, err
    assert poller.row(job_id)["state"] == "done"
    assert _details(poller) == []


def test_a_killed_runner_records_the_exit_reason_on_the_dead_event(tmp_path: Path) -> None:
    poller = Poller(tmp_path)
    job_id = poller.review()
    poller.claim()
    poller.live.update({11, 22})
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    poller.pool.attach_worker(job_id, owner="codex", pid=22, start=None)
    poller.now[0] = CLAIMED_AT + 30
    poller.queue.heartbeat(job_id, owner="codex")
    with poller.queue.connect() as connection:
        connection.execute(
            "INSERT INTO job_events (job_id, repo, ticket_id, action, recipient, event, "
            "detail, recorded_at) VALUES (?, 'maxharris1/factory', '72', 'review', 'codex', "
            "?, ?, ?)",
            (job_id, RUN_DIED, "orch job run's command on box: killed by SIGKILL (exit 137)",
             CLAIMED_AT + 31),
        )
    poller.live.clear()
    poller.at(60)
    assert poller.row(job_id)["state"] == "failed"
    (dead,) = [d for e, d in _details(poller) if e == "dead"]
    assert "pid 11" in dead and "killed by SIGKILL (exit 137)" in dead


def test_exit_reasons_name_the_signal() -> None:
    assert exit_reason(0) == "exit 0"
    assert exit_reason(127) == "exit 127"
    assert exit_reason(137) == "killed by SIGKILL (exit 137)"
    assert exit_reason(None).startswith("no exit status")


# --- running with a placement but never started -------------------------------------


def test_044db50d_a_remote_job_whose_worker_never_started_is_released_like_a_claim(
    tmp_path: Path,
) -> None:
    """Placed on pc, state running, remote_started=0, no worker pid, silent 11 min."""
    poller = Poller(tmp_path, box=False)
    job_id = poller.review()
    placed = poller.claim()
    assert placed.device == "pc"
    # `orch job run`'s first beat landed, then the run was refused or died
    # before it recorded itself: not "claimed", and nothing ever started.
    poller.now[0] = CLAIMED_AT + 20
    poller.queue.heartbeat(job_id, owner="codex")

    poller.at(20 + 4 * 60)
    assert poller.row(job_id)["state"] == "running"
    poller.at(20 + 5 * 60 + 1)
    row = poller.row(job_id)
    assert row["state"] == "queued"
    assert poller.pool.placement(job_id) is None
    assert poller.jobs() == [(job_id, "queued", 1)]  # same row: no round, no T6 attempt
    assert json.loads(row["detail"])["round"] == "1/1"
    assert poller.events() == [(job_id, RELEASED_UNSTARTED)]
    assert any("no round charged" in line for line in poller.lines)


def test_a_dead_runner_that_never_started_its_worker_is_released_not_failed(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box=False)
    job_id = poller.review()
    poller.claim()
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    poller.now[0] = CLAIMED_AT + 20
    poller.queue.heartbeat(job_id, owner="codex")
    poller.live.add(11)
    poller.at(60)  # runner alive (setting up): left alone
    assert poller.row(job_id)["state"] == "running"
    poller.live.discard(11)
    poller.at(90)  # runner gone, no worker pid, no remote start
    assert poller.row(job_id)["state"] == "queued"
    assert poller.events() == [(job_id, RELEASED_UNSTARTED)]


def test_an_unstarted_job_on_an_abandoned_ticket_is_closed_not_requeued(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box=False)
    job_id = poller.review()
    poller.claim()
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    poller.now[0] = CLAIMED_AT + 20
    poller.queue.heartbeat(job_id, owner="codex")
    poller.dispatcher.process(
        ticket(
            comment("drop", ABANDONED, author="grok-login"),
            repo="maxharris1/factory",
            ticket_id="72",
        )
    )

    poller.at(90)

    assert poller.jobs() == [(job_id, "failed", 1)]
    assert poller.row(job_id)["failure_handled"] == 1
    assert poller.events() == [(job_id, RELEASED_UNSTARTED), (job_id, "abandoned")]
    assert poller.pool.claim("codex") is None


def test_a_started_remote_worker_is_never_released_as_unstarted(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=False, check=255)
    job_id = poller.review()
    poller.claim()
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    poller.pool.attach_worker(job_id, owner="codex", remote=True, tag="c" * 32)
    poller.now[0] = CLAIMED_AT + 20
    poller.queue.heartbeat(job_id, owner="codex")
    poller.at(20 * 60)  # runner gone, device unreachable: may still run
    assert poller.row(job_id)["state"] == "running"
    assert poller.events() == []


def test_unstarted_requeues_are_capped_then_the_job_fails_for_t6(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=False)
    job_id = poller.review()
    offset = 0.0
    for _ in range(MAX_FREE_REQUEUES):
        poller.now[0] = CLAIMED_AT + offset
        poller.claim()
        poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
        poller.queue.heartbeat(job_id, owner="codex")
        poller.at(offset + 30)
        assert poller.row(job_id)["state"] == "queued"
        offset += 60
    poller.now[0] = CLAIMED_AT + offset
    poller.claim()
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    poller.queue.heartbeat(job_id, owner="codex")
    poller.at(offset + 30)
    assert poller.pool.placement(job_id) is None
    events = [event for _id, event in poller.events()]
    assert events == [RELEASED_UNSTARTED] * MAX_FREE_REQUEUES + [FAILED_UNSTARTED]
    # Failed, then T6's single retry (a new row with its own budget).
    assert [(state, attempt) for _id, state, attempt in poller.jobs()] == [
        ("failed", 1), ("queued", 2)
    ]


# --- a failed remote start frees the slot and requeues the job --------------------


def _remote(tmp_path: Path) -> tuple[Poller, str, Ssh]:
    poller = Poller(tmp_path, box=False, now=time.time())
    poller.queue.clock = time.time
    job_id = poller.review(timeout=3600)
    assert poller.claim().device == "pc"
    return poller, job_id, Ssh(tmp_path)


def test_044db50d_a_remote_harness_not_on_path_is_requeued_as_its_slot_is_freed(
    tmp_path: Path,
) -> None:
    """exit 127: gh was not on PATH. The slot was freed, the job left running."""
    poller, job_id, ssh = _remote(tmp_path)
    code, err = _job_run(poller, job_id, "orch-test-no-such-harness", env=ssh.env())
    assert code == 127, err
    row = poller.row(job_id)
    assert row["state"] == "queued"  # requeued in the step that freed the slot
    assert poller.pool.placement(job_id) is None
    assert (row["attempt"], row["generation"]) == (1, 1)
    events = _details(poller)
    assert [event for event, _d in events] == [RUN_DIED, REQUEUED_EARLY_EXIT]
    assert "exit 127" in events[1][1] and "no round charged" in events[1][1]
    assert f"released pc slot of job {job_id}" in err

    # The poller never touches it again: it waits in the queue as before.
    poller.now[0] = time.time() + 600
    poller.queue.clock = lambda: poller.now[0]
    poller.dispatcher.reconcile_jobs()
    assert poller.jobs() == [(job_id, "queued", 1)]


def test_a_remote_harness_that_ran_and_failed_is_not_requeued(tmp_path: Path) -> None:
    poller, job_id, ssh = _remote(tmp_path)
    code, err = _job_run(poller, job_id, sys.executable, "-c", "import sys; sys.exit(1)", env=ssh.env())
    assert code == 1, err
    assert poller.row(job_id)["state"] == "running"
    assert poller.pool.placement(job_id) is None
    assert [event for event, _d in _details(poller)] == [RUN_DIED]


def test_remote_harness_runs_under_a_login_shell_so_profile_path_applies(
    tmp_path: Path,
) -> None:
    poller, job_id, ssh = _remote(tmp_path)
    home = tmp_path / "home"
    tools = home / ".local" / "bin"
    tools.mkdir(parents=True)
    report = tmp_path / "ran"
    tool = tools / "orch-test-gh"
    tool.write_text(f'#!/bin/sh\necho "$ORCH_JOB_ID" > {report}\nexit 0\n', encoding="utf-8")
    tool.chmod(0o755)
    (home / ".profile").write_text('PATH="$HOME/.local/bin:$PATH"\nexport PATH\n', encoding="utf-8")
    (home / ".bash_profile").write_text(". \"$HOME/.profile\"\n", encoding="utf-8")
    env = {**ssh.env(), "FAKE_SSH_HOME": str(home)}
    code, err = _job_run(poller, job_id, "orch-test-gh", env=env)
    assert code == 0, err
    assert report.read_text(encoding="utf-8").strip() == job_id
    start = next(
        call for call in ssh.calls()
        if (call["script"] or "").startswith(f"exec env {remote.TAG_ENV}=")
    )
    assert "-lc" in start["script"]


def test_an_early_exit_keeps_its_slot_while_the_worker_is_not_confirmed_gone(
    tmp_path: Path,
) -> None:
    poller, job_id, ssh = _remote(tmp_path)
    marker = tmp_path / "go-offline"
    # The device answers the start (127), then goes offline for the check.
    script = (
        f"import sys, time, pathlib; pathlib.Path({str(marker)!r}).touch(); "
        "time.sleep(0.5); sys.exit(127)"
    )
    env = ssh.env()
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(poller.path),
        "--machines", str(poller.machines), "--", sys.executable, "-c", script,
        env=env,
    )
    deadline = time.time() + 30
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.02)
    ssh.offline("fakepc-codex-bot").touch()
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == 127, err
    # Not confirmed gone: the slot stays, so nothing is requeued under it.
    assert poller.pool.placement(job_id) is not None
    assert poller.row(job_id)["state"] == "running"


# --- T6: never retry a review whose verdict is already posted (PR #88) --------------


def _review_failed_after_its_verdict(tmp_path: Path, *, verdict_round: str, author: str):
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    ready = comment(f"ready-{HEAD}", READY.format(head=HEAD), author="claude-login")
    dispatcher.process(ticket(ready, head=HEAD, ci={HEAD: CIResult("success")}))
    now[0] = 110
    review = queue.claim("codex")
    assert review is not None and review.request.action == "review"
    verdict = comment(
        "verdict-r1",
        VERDICT.format(round=verdict_round, head=HEAD[:7], verdict="REVISE", risk="high"),
        author=author,
        created_at=120,
    )
    now[0] = 146  # 26 s after the verdict, the review job fails
    queue.finish(review.id, owner="codex", handoff_comment_id=None)
    return dispatcher, queue, review, ready, verdict, now


def _rows(queue: SQLiteJobQueue) -> list[tuple[str, str, int, str | None]]:
    with queue.connect() as connection:
        return [
            (str(row["action"]), str(row["state"]), int(row["attempt"]), row["handoff_comment_id"])
            for row in connection.execute("SELECT * FROM jobs ORDER BY created_at, rowid")
        ]


def _events(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [
            (str(row["event"]), str(row["detail"]))
            for row in connection.execute("SELECT * FROM job_events ORDER BY id")
        ]


def test_pr88_a_failed_review_whose_verdict_is_posted_is_done_and_not_retried(
    tmp_path: Path,
) -> None:
    dispatcher, queue, review, ready, verdict, now = _review_failed_after_its_verdict(
        tmp_path, verdict_round="1/3", author="codex-login"
    )
    now[0] = 160
    dispatcher.process(ticket(ready, verdict, head=HEAD, ci={HEAD: CIResult("success")}))
    dispatcher.reconcile_jobs()
    rows = _rows(queue)
    reviews = [row for row in rows if row[0] == "review"]
    assert reviews == [("review", "done", 1, "verdict-r1")]  # no t6 retry review
    assert [row[:2] for row in rows if row[0] == "fix"] == [("fix", "queued")]
    (event,) = _events(queue)
    assert event[0] == "done-from-verdict" and "verdict-r1" in event[1]
    with queue.connect() as connection:
        handled = connection.execute(
            "SELECT failure_handled FROM jobs WHERE id = ?", (review.id,)
        ).fetchone()[0]
    assert handled == 1
    assert queue.unhandled_failures() == ()


def test_pr88_the_same_through_poll_once(tmp_path: Path) -> None:
    from tests.test_dispatcher import FakeSource

    dispatcher, queue, _review, ready, verdict, now = _review_failed_after_its_verdict(
        tmp_path, verdict_round="1/3", author="codex-login"
    )
    now[0] = 160
    snapshot = ticket(ready, verdict, head=HEAD, ci={HEAD: CIResult("success")})
    with dispatcher._connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) "
            "VALUES (?, ?, ?, ?)",
            ("owner/repo", "42", "github", 0.0),
        )
    dispatcher.poll_once(FakeSource(snapshot))
    assert [row for row in _rows(queue) if row[0] == "review"] == [
        ("review", "done", 1, "verdict-r1")
    ]


def test_a_verdict_for_another_round_or_from_an_untrusted_login_does_not_count(
    tmp_path: Path,
) -> None:
    for name, round_field, author in (
        ("round", "2/3", "codex-login"),
        ("author", "1/3", "grok-login"),
    ):
        dispatcher, queue, _review, ready, verdict, now = _review_failed_after_its_verdict(
            tmp_path / name, verdict_round=round_field, author=author
        )
        now[0] = 160
        dispatcher.process(ticket(ready, verdict, head=HEAD, ci={HEAD: CIResult("success")}))
        dispatcher.reconcile_jobs()
        reviews = [row[:3] for row in _rows(queue) if row[0] == "review"]
        assert reviews == [("review", "failed", 1), ("review", "queued", 2)], name


def test_no_retry_review_is_queued_while_a_fix_for_that_head_is_open(tmp_path: Path) -> None:
    dispatcher, queue, _review, _ready, _verdict, now = _review_failed_after_its_verdict(
        tmp_path, verdict_round="1/3", author="codex-login"
    )
    # The REVISE was handled in an earlier poll (its fix job is open); this
    # dispatcher has not read the ticket, so only the open fix job tells.
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="fix",
            recipient="claude",
            source_comment_id="verdict-r1",
            timeout_seconds=3600,
            head=HEAD,
        ),
        dedupe_key="t3:owner/repo#42:verdict-r1",
    )
    fresh = Dispatcher(
        dispatcher.state_path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
    )
    now[0] = 160
    fresh.reconcile_jobs()
    reviews = [row[:2] for row in _rows(queue) if row[0] == "review"]
    assert reviews == [("review", "failed")]
    assert [event for event, _d in _events(queue)] == ["retry-deferred"]
    assert [job.id for job in queue.unhandled_failures()] == [_review.id]  # deferred, not dropped
    fresh.reconcile_jobs()
    assert [event for event, _d in _events(queue)] == ["retry-deferred"]  # recorded once


def test_a_failed_review_with_no_verdict_and_no_fix_is_still_retried_once(
    tmp_path: Path,
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    ready = comment(f"ready-{HEAD}", READY.format(head=HEAD), author="claude-login")
    dispatcher.process(ticket(ready, head=HEAD, ci={HEAD: CIResult("success")}))
    review = queue.claim("codex")
    assert review is not None
    queue.finish(review.id, owner="codex", handoff_comment_id=None)
    dispatcher.process(ticket(ready, head=HEAD, ci={HEAD: CIResult("success")}))
    dispatcher.reconcile_jobs()
    reviews = [row[:3] for row in _rows(queue) if row[0] == "review"]
    assert reviews == [("review", "failed", 1), ("review", "queued", 2)]


def test_dead_and_stale_states_are_unchanged_for_jobs_without_a_runner_check() -> None:
    # The dispatcher only asks about a recorded runner; nothing else moved.
    started = placement(runner_pid=11, worker_pid=22)
    gone = Liveness(runner_alive=False, worker_gone=True)
    assert derive(
        last_activity_at=990.0, now=1000.0, placement=started, stale_seconds=120, liveness=gone
    ) == DEAD


# --- PR #99 round 1 (Codex REVISE) ---------------------------------------------------


def _autostart_box(tmp_path: Path) -> tuple[Poller, str]:
    """A box job claimed by an autostart waiter: this test process, the run's parent."""
    from lib.worktrees import process_start

    poller = Poller(tmp_path, now=time.time(), box_slots=3)  # most free slots: the box
    poller.queue.clock = time.time
    job_id = poller.review(timeout=3600)
    me = os.getpid()
    claimed = poller.pool.claim("codex", autostart=(me, process_start(me)))
    assert claimed is not None and claimed[1].device == "box"
    return poller, job_id


def test_r1_a1_an_autostart_run_whose_harness_never_started_gets_the_free_requeue(
    tmp_path: Path,
) -> None:
    poller, job_id = _autostart_box(tmp_path)
    code, err = _job_run(poller, job_id, "orch-test-no-such-harness")
    assert code == 127, err
    row = poller.row(job_id)
    assert (row["state"], row["attempt"], row["generation"]) == ("queued", 1, 1), err
    assert poller.pool.placement(job_id) is None
    assert poller.events()[-1] == (job_id, REQUEUED_EARLY_EXIT)


def test_r1_a1_an_autostart_run_whose_setup_failed_gets_the_free_requeue(
    tmp_path: Path,
) -> None:
    poller, job_id = _autostart_box(tmp_path)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("", encoding="utf-8")
    code, err = _job_run(
        poller, job_id, "--worktree", "--work-root", str(blocked / "work"), "true"
    )
    assert code != 0, err
    assert poller.row(job_id)["state"] == "queued", err
    assert poller.events()[-1] == (job_id, REQUEUED_EARLY_EXIT)


def test_r1_a1_an_autostart_run_whose_harness_ran_and_failed_still_goes_to_t6(
    tmp_path: Path,
) -> None:
    poller, job_id = _autostart_box(tmp_path)
    code, err = _job_run(poller, job_id, sys.executable, "-c", "import sys; sys.exit(1)")
    assert code == 1, err
    assert poller.row(job_id)["state"] == "failed"
    assert REQUEUED_EARLY_EXIT not in [event for _j, event in poller.events()]


def _side_effect_then(code: int, marker: Path) -> list[str]:
    return [sys.executable, "-c", f"import pathlib, sys; pathlib.Path({str(marker)!r}).touch(); sys.exit({code})"]


def test_r1_a2_a_box_harness_that_ran_is_never_unstarted_whatever_its_status(
    tmp_path: Path,
) -> None:
    for code in (126, 127):
        poller, job_id = _box_poller(tmp_path / str(code))
        marker = tmp_path / f"ran-{code}"
        got, err = _job_run(poller, job_id, *_side_effect_then(code, marker))
        assert got == code and marker.exists(), err
        assert poller.row(job_id)["state"] == "running", code  # normal failure handling
        assert [event for event, _d in _details(poller)] == [RUN_DIED], code


def test_r1_a2_a_remote_harness_that_ran_is_never_unstarted_whatever_its_status(
    tmp_path: Path,
) -> None:
    for code in (remote.START_REFUSED, 126, 127, 255):
        poller, job_id, ssh = _remote(tmp_path / str(code))
        marker = tmp_path / f"ran-{code}"
        got, err = _job_run(poller, job_id, *_side_effect_then(code, marker), env=ssh.env())
        assert got == code and marker.exists(), err
        assert poller.row(job_id)["state"] == "running", code
        assert poller.pool.placement(job_id) is None
        assert [event for event, _d in _details(poller)] == [RUN_DIED], code


def test_r1_a2_a_remote_start_refused_before_the_harness_is_requeued(tmp_path: Path) -> None:
    poller, job_id, ssh = _remote(tmp_path)
    work = tmp_path / "remote" / "codex-bot" / "work"
    work.mkdir(parents=True)
    (work / "jobs").write_text("", encoding="utf-8")  # the job directory cannot be made
    marker = tmp_path / "ran"
    code, err = _job_run(poller, job_id, *_side_effect_then(0, marker), env=ssh.env())
    assert code == remote.START_REFUSED, err
    assert not marker.exists()
    assert poller.row(job_id)["state"] == "queued"
    assert poller.events()[-1] == (job_id, REQUEUED_EARLY_EXIT)


# --- PR #99 round 2 (Codex REVISE): a started autostart run is settled before its slot goes


def _tokenless_done(poller: Poller, job_id: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if k not in ("ORCH_DB", job_token.PROOF_ENV)}
    return subprocess.run(
        [sys.executable, str(ORCH), "job", "done", "codex-bot", job_id,
         "--db", str(poller.path), "--handoff", "unrelated-turn"],
        env=clean, capture_output=True, text=True,
    )


def _run_in_process(
    poller: Poller, job_id: str, monkeypatch, env: dict | None = None, before: str = ""
) -> list:
    """`orch job run` of a started harness that fails, in this process (its waiter is our parent).

    Every slot release and the T6 failure first run a real tokenless
    `orch job done` for the job, as an unrelated same-bot turn would.
    """
    from lib.device_pool import DevicePool
    from lib.worktrees import process_start

    parent = os.getppid()
    assert poller.pool.claim("codex", autostart=(parent, process_start(parent))) is not None
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    competing: list = []
    release, fail = DevicePool.release, cli._fail_unfinished

    def released(pool, job, *, owner):
        competing.append(("release", _tokenless_done(poller, job)))
        return release(pool, job, owner=owner)

    def failed(*args):
        competing.append(("fail", _tokenless_done(poller, job_id)))
        return fail(*args)

    monkeypatch.setattr(DevicePool, "release", released)
    monkeypatch.setattr(cli, "_fail_unfinished", failed)
    code = cli._job_run([
        "codex-bot", job_id, "--db", str(poller.path), "--machines", str(poller.machines),
        "--interval", "0.2", "--", sys.executable, "-c", f"{before}raise SystemExit(1)",
    ])
    assert code == 1
    return competing


def _assert_never_finished_by_another_turn(
    poller: Poller, job_id: str, competing: list, state: str = "failed"
) -> None:
    assert competing and "release" in [when for when, _r in competing]
    for when, result in competing:
        assert result.returncode in (1, 2), (when, result.returncode, result.stdout, result.stderr)
    row = poller.row(job_id)
    assert (row["state"], row["handoff_comment_id"]) == (state, None)
    assert poller.pool.placement(job_id) is None


def test_r2_a_started_box_autostart_run_keeps_its_ownership_until_the_job_is_failed(
    tmp_path: Path, monkeypatch
) -> None:
    poller = Poller(tmp_path, now=time.time(), box_slots=3)
    poller.queue.clock = time.time
    job_id = poller.review(timeout=3600)
    competing = _run_in_process(poller, job_id, monkeypatch)
    _assert_never_finished_by_another_turn(poller, job_id, competing)


def test_r2_a_started_remote_autostart_run_keeps_its_ownership_and_is_left_to_its_waiter(
    tmp_path: Path, monkeypatch
) -> None:
    # A remote autostart job is finished by its waiter from the run's exit
    # status and final ORCH-HANDOFF line (lib/waiter.py), not by orch job run.
    poller = Poller(tmp_path, box=False, now=time.time())
    poller.queue.clock = time.time
    job_id = poller.review(timeout=3600)
    competing = _run_in_process(poller, job_id, monkeypatch, env=Ssh(tmp_path).env())
    assert "fail" not in [when for when, _r in competing]
    _assert_never_finished_by_another_turn(poller, job_id, competing, state="running")


def test_r2_a_started_remote_autostart_run_whose_slot_stays_held_keeps_it_running(
    tmp_path: Path, monkeypatch
) -> None:
    poller = Poller(tmp_path, box=False, now=time.time())
    poller.queue.clock = time.time
    job_id = poller.review(timeout=3600)
    ssh = Ssh(tmp_path)
    offline = ssh.offline("fakepc-codex-bot")
    competing = _run_in_process(
        poller, job_id, monkeypatch, env=ssh.env(),
        before=f"import pathlib; pathlib.Path({str(offline)!r}).touch(); ",
    )
    assert competing == []
    row = poller.row(job_id)
    assert (row["state"], row["handoff_comment_id"]) == ("running", None)
    assert poller.pool.placement(job_id) is not None  # the DeviceMonitor confirms it later
    tokenless = _tokenless_done(poller, job_id)
    assert tokenless.returncode == 2, tokenless.stderr


def test_r1_a3_a_review_job_with_no_round_never_takes_a_posted_verdict(
    tmp_path: Path,
) -> None:
    dispatcher, queue, review, ready, verdict, now = _review_failed_after_its_verdict(
        tmp_path, verdict_round="1/3", author="codex-login"
    )
    with queue.connect() as connection:
        connection.execute(
            "UPDATE jobs SET detail = ? WHERE id = ?",
            (json.dumps({"action": "review", "round": None}), review.id),
        )
    now[0] = 160
    dispatcher._polled[("owner/repo", "42")] = ticket(
        ready, verdict, head=HEAD, ci={HEAD: CIResult("success")}
    )
    dispatcher.reconcile_jobs()
    reviews = [row[:3] for row in _rows(queue) if row[0] == "review"]
    assert reviews == [("review", "failed", 1), ("review", "queued", 2)]
    assert "done-from-verdict" not in [event for event, _d in _events(queue)]


def test_r1_a3_a_review_resumed_by_t5_keeps_its_round(tmp_path: Path) -> None:
    from tests.test_dispatcher import ANSWERS

    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    ci = {HEAD: CIResult("success")}
    ready = comment(f"ready-{HEAD}", READY.format(head=HEAD), author="claude-login")
    dispatcher.process(ticket(ready, head=HEAD, ci=ci))
    review = queue.claim("codex")
    assert review is not None and review.request.action == "review"
    queue.finish(review.id, owner="codex", handoff_comment_id="questions-1")
    asked = comment(
        "questions-1",
        "<!-- orch:v1 kind=questions from=codex-bot role=reviewer -->\nQuestions.\n",
        author="codex-login",
        created_at=120,
    )
    dispatcher.process(ticket(ready, asked, head=HEAD, ci=ci))
    wake = queue.claim("grok")
    assert wake is not None
    queue.finish(wake.id, owner="grok", handoff_comment_id="answers-1")
    answered = comment("answers-1", ANSWERS, author="grok-login", created_at=130)
    dispatcher.process(ticket(ready, asked, answered, head=HEAD, ci=ci))
    resumed = queue.claim("codex")
    assert resumed is not None and resumed.request.action == "review"
    assert json.loads(resumed.request.detail or "{}")["round"] == "1/3"


def test_r1_a4_a_verdict_already_rejected_as_stale_never_finishes_a_later_review(
    tmp_path: Path,
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    early = comment(
        "verdict-early",
        VERDICT.format(round="1/3", head=HEAD[:7], verdict="REVISE", risk="high"),
        author="codex-login",
        created_at=90,
    )
    ci = {HEAD: CIResult("success")}
    dispatcher.process(ticket(early, head=HEAD, ci=ci))  # no review yet: stale
    now[0] = 110
    ready = comment(f"ready-{HEAD}", READY.format(head=HEAD), author="claude-login", created_at=105)
    dispatcher.process(ticket(early, ready, head=HEAD, ci=ci))
    review = queue.claim("codex")
    assert review is not None and review.request.action == "review"
    now[0] = 150
    queue.finish(review.id, owner="codex", handoff_comment_id=None)
    dispatcher.process(ticket(early, ready, head=HEAD, ci=ci))
    dispatcher.reconcile_jobs()
    reviews = [row[:3] for row in _rows(queue) if row[0] == "review"]
    assert reviews == [("review", "failed", 1), ("review", "queued", 2)]
    assert "done-from-verdict" not in [event for event, _d in _events(queue)]


def test_r1_a4_a_verdict_handled_as_stale_never_finishes_a_review_whatever_its_time(
    tmp_path: Path,
) -> None:
    dispatcher, queue, _review, ready, verdict, now = _review_failed_after_its_verdict(
        tmp_path, verdict_round="1/3", author="codex-login"
    )
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO handled_comments (repo, ticket_id, comment_id, outcome) "
            "VALUES ('owner/repo', '42', ?, 'stale-revise')",
            (verdict.id,),
        )
    now[0] = 160
    dispatcher._polled[("owner/repo", "42")] = ticket(
        ready, verdict, head=HEAD, ci={HEAD: CIResult("success")}
    )
    dispatcher.reconcile_jobs()
    reviews = [row[:3] for row in _rows(queue) if row[0] == "review"]
    assert reviews == [("review", "failed", 1), ("review", "queued", 2)]


def _review_failed_beside_an_open_fix(tmp_path: Path):
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    ci = {HEAD: CIResult("success")}
    ready = comment(f"ready-{HEAD}", READY.format(head=HEAD), author="claude-login")
    dispatcher.process(ticket(ready, head=HEAD, ci=ci))
    review = queue.claim("codex")
    assert review is not None and review.request.action == "review"
    fix = queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="fix",
            recipient="claude",
            source_comment_id="ci-red",
            timeout_seconds=3600,
            head=HEAD,
        ),
        dedupe_key="ci-fix:owner/repo#42:1",
    )
    now[0] = 150
    queue.finish(review.id, owner="codex", handoff_comment_id=None)
    dispatcher.process(ticket(ready, head=HEAD, ci=ci))
    dispatcher.reconcile_jobs()
    assert [row[:2] for row in _rows(queue) if row[0] == "review"] == [("review", "failed")]
    return dispatcher, queue, fix, ready, now


def test_r1_a5_a_fix_that_readies_the_same_head_gets_the_review_it_waited_for(
    tmp_path: Path,
) -> None:
    dispatcher, queue, fix, ready, now = _review_failed_beside_an_open_fix(tmp_path)
    claimed = queue.claim("claude")
    assert claimed is not None and claimed.id == fix.id
    now[0] = 200
    again = comment("ready-again", READY.format(head=HEAD), author="claude-login", created_at=190)
    queue.finish(fix.id, owner="claude", handoff_comment_id=again.id)
    dispatcher.process(ticket(ready, again, head=HEAD, ci={HEAD: CIResult("success")}))
    dispatcher.reconcile_jobs()
    reviews = [row[:3] for row in _rows(queue) if row[0] == "review"]
    assert reviews[-1] == ("review", "queued", 2)  # a runnable review of that head


def test_r1_a5_a_fix_that_pushed_a_new_head_ends_the_old_heads_retry(tmp_path: Path) -> None:
    dispatcher, queue, fix, ready, now = _review_failed_beside_an_open_fix(tmp_path)
    claimed = queue.claim("claude")
    assert claimed is not None and claimed.id == fix.id
    now[0] = 200
    new_head = "f00dfee" + "1" * 33
    again = comment(f"ready-{new_head}", READY.format(head=new_head), author="claude-login", created_at=190)
    queue.finish(fix.id, owner="claude", handoff_comment_id=again.id)
    dispatcher.process(ticket(ready, again, head=new_head, ci={new_head: CIResult("success")}))
    dispatcher.reconcile_jobs()
    with queue.connect() as connection:
        reviews = [
            (str(row["head"]), str(row["state"]), int(row["attempt"]))
            for row in connection.execute(
                "SELECT * FROM jobs WHERE action = 'review' ORDER BY created_at, rowid"
            )
        ]
    assert reviews == [(HEAD, "failed", 1), (new_head, "queued", 1)]
    assert queue.unhandled_failures() == ()


def test_r1_a6_the_bots_login_profile_cannot_change_orchs_env_or_directory(
    tmp_path: Path,
) -> None:
    poller, job_id, ssh = _remote(tmp_path)
    home = tmp_path / "device-home"
    home.mkdir()
    (home / ".bash_profile").write_text(
        "export ORCH_JOB_ID=profile ORCH_JOB_BOT=profile ORCH_JOB_DEVICE=profile\n"
        "export ORCH_JOB_WORKDIR=/profile ORCH_WORKER_TAG=profile\n"
        "cd /\n",
        encoding="utf-8",
    )
    report = tmp_path / "seen.json"
    keys = ("ORCH_JOB_ID", "ORCH_JOB_BOT", "ORCH_JOB_DEVICE", "ORCH_JOB_WORKDIR", remote.TAG_ENV)
    script = (
        "import json, os\n"
        f"json.dump({{'cwd': os.getcwd(), **{{k: os.environ.get(k) for k in {keys!r}}}}}, "
        f"open({str(report)!r}, 'w'))\n"
    )
    code, err = _job_run(
        poller, job_id, sys.executable, "-c", script,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    assert code == 0, err
    start = next(
        call for call in ssh.calls()
        if (call["script"] or "").startswith(f"exec env {remote.TAG_ENV}=")
    )
    tag = start["script"].split()[2].removeprefix(f"{remote.TAG_ENV}=")
    workdir = f"{tmp_path}/remote/codex-bot/work/jobs/{job_id}"
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "cwd": workdir,
        "ORCH_JOB_ID": job_id,
        "ORCH_JOB_BOT": "codex-bot",
        "ORCH_JOB_DEVICE": "pc",
        "ORCH_JOB_WORKDIR": workdir,
        remote.TAG_ENV: tag,
    }


# --- r3: a fix awaiting its own T6 retry keeps the review retry deferred ------------


def _review_deferred_then_fix_failed(tmp_path: Path):
    """PR #99 r3: the review fails first (deferred), then its fix fails too.

    Real poll_once, handoff parsing, queue writes and claims; the builder
    posts ready on the same head before its run settles, so the round-2
    review runs beside the fix, fails and is deferred; the fix then fails
    without a handoff. Both failures are unhandled when the next poll runs
    T6, the review's first (it finished first).
    """
    from dataclasses import replace

    from tests.test_dispatcher import FakeSource

    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    ci = {HEAD: CIResult("success")}

    def poll(*comments) -> None:
        snapshot = replace(ticket(*comments, head=HEAD, ci=ci), ready_starts_review=True)
        dispatcher.poll_once(FakeSource(snapshot, github=(snapshot,)))

    ready = comment("ready-1", READY.format(head=HEAD), author="claude-login", created_at=100)
    poll(ready)
    review1 = queue.claim("codex")
    assert review1 is not None and review1.request.action == "review"
    now[0] = 120
    revise = comment(
        "revise-1",
        VERDICT.format(round="1/3", head=HEAD, verdict="REVISE", risk="high"),
        author="codex-login",
        created_at=120,
    )
    queue.finish(review1.id, owner="codex", handoff_comment_id=revise.id)
    poll(ready, revise)
    fix = queue.claim("claude")
    assert fix is not None and fix.request.action == "fix"

    now[0] = 140
    again = comment("ready-2", READY.format(head=HEAD), author="claude-login", created_at=140)
    poll(ready, revise, again)
    review2 = queue.claim("codex")
    assert review2 is not None and review2.request.action == "review"
    now[0] = 150
    queue.finish(review2.id, owner="codex", handoff_comment_id=None)
    poll(ready, revise, again)
    assert "retry-deferred" in [event for event, _d in _events(queue)]

    now[0] = 200
    queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    assert [job.request.action for job in queue.unhandled_failures()] == ["review", "fix"]
    return queue, poll, (ready, revise, again), now


def test_r3_a_failed_fix_awaiting_its_retry_keeps_the_review_retry_deferred(
    tmp_path: Path,
) -> None:
    queue, poll, comments, _now = _review_deferred_then_fix_failed(tmp_path)
    poll(*comments)
    assert queue.claim("codex") is None  # the review retry still waits
    fix_retry = queue.claim("claude")
    assert fix_retry is not None and fix_retry.request.action == "fix"
    assert fix_retry.request.attempt == 2
    assert [job.request.action for job in queue.unhandled_failures()] == ["review"]

    poll(*comments)  # the fix retry runs: still deferred
    assert queue.claim("codex") is None


def test_r3_the_deferred_review_runs_once_the_fix_retry_readies_the_same_head(
    tmp_path: Path,
) -> None:
    queue, poll, comments, now = _review_deferred_then_fix_failed(tmp_path)
    poll(*comments)
    fix_retry = queue.claim("claude")
    assert fix_retry is not None
    now[0] = 260
    ready3 = comment("ready-3", READY.format(head=HEAD), author="claude-login", created_at=250)
    queue.finish(fix_retry.id, owner="claude", handoff_comment_id=ready3.id)
    poll(*comments, ready3)
    review = queue.claim("codex")
    assert review is not None and review.request.action == "review"
    assert review.request.head == HEAD
    assert queue.claim("codex") is None  # exactly one review of that head


def test_r3_a_fix_that_fails_twice_leaves_no_review_retry_beside_the_stuck_ticket(
    tmp_path: Path,
) -> None:
    queue, poll, comments, now = _review_deferred_then_fix_failed(tmp_path)
    poll(*comments)
    fix_retry = queue.claim("claude")
    assert fix_retry is not None
    now[0] = 260
    queue.finish(fix_retry.id, owner="claude", handoff_comment_id=None)
    poll(*comments)
    poll(*comments)
    assert queue.claim("codex") is None
    assert queue.claim("claude") is None
    assert queue.unhandled_failures() == ()
