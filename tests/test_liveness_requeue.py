"""#91: stale vs dead, unstarted requeue, early remote exits, the T6 verdict guard.

Every test uses a temp database and fakes: tests/test_job_state.py's
Poller (scripted pid checks, FakeTransport for SSH), tests/fake_ssh.py on
PATH for `orch job run` on a remote device, and test_dispatcher's tickets.
Nothing reaches a real host, a real harness CLI or the live orch.db.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from lib import job_run, remote
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
from tests.test_remote_start import Ssh, orch

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
    poller = Poller(tmp_path, now=time.time())
    poller.queue.clock = time.time
    job_id = poller.review(timeout=3600)
    poller.claim()
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


def test_a_remote_start_the_device_refuses_is_requeued(tmp_path: Path) -> None:
    poller, job_id, ssh = _remote(tmp_path)
    code, err = _job_run(
        poller, job_id, sys.executable, "-c", f"import sys; sys.exit({remote.START_REFUSED})",
        env=ssh.env(),
    )
    assert code == remote.START_REFUSED, err
    assert poller.row(job_id)["state"] == "queued"
    assert poller.events()[-1] == (job_id, REQUEUED_EARLY_EXIT)


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
    assert [event for event, _d in _events(queue)] == ["retry-skipped"]
    assert queue.unhandled_failures() == ()


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
