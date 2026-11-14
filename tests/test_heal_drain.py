"""#219: a stale-code waiter with jobs in flight is drained, not restarted."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lib import heal
from lib.heal import DRAIN, ESCALATE, RESTART
from tests.test_heal import CHECKOUT, NOW, Posts, Runs, component, facts, log_lines, report

STALE = component(version="0123456789ab")
LABEL = STALE["label"]


def busy(jobs: int | None, **changes):
    return facts(waiter_jobs={"codex-bot": jobs}, **changes)


def run(tmp_path: Path, host, *, now: float = NOW, data: dict | None = None,
        runs: Runs | None = None, posts: Posts | None = None):
    runs = runs if runs is not None else Runs()
    posts = posts if posts is not None else Posts()
    actions = heal.heal(data or report(STALE), facts=host, state_dir=tmp_path,
                        deploy=Path("/srv/factory/deploy"), webhook=heal.Webhook(),
                        environ={"PATH": "/usr/bin",
                                 "FACTORY_WEBHOOK_URL": "https://hooks.example.test/x",
                                 "FACTORY_WEBHOOK_AUTH": "Bearer x"},
                        now=now, run=runs, post=posts, clock=lambda: 0.0)
    return actions, runs, posts


def marker(tmp_path: Path) -> Path:
    return tmp_path / "waiter-codex-bot.drain"


def test_a_busy_stale_waiter_is_drained_not_restarted() -> None:
    (action,) = heal.decide(report(STALE), {}, busy(2), NOW)
    assert (action.kind, action.reason) == (DRAIN, "stale code, 2 running jobs")


def test_an_idle_stale_waiter_restarts_once_it_acknowledges_the_drain() -> None:
    (action,) = heal.decide(report(STALE), {}, busy(0, drain_acked={"codex-bot": CHECKOUT}),
                            NOW)
    assert action.kind == RESTART


def test_an_idle_stale_waiter_without_an_ack_for_this_commit_is_drained() -> None:
    for acked in ({}, {"codex-bot": "0123456789ab"}):
        (action,) = heal.decide(report(STALE), {}, busy(0, drain_acked=acked), NOW)
        assert (action.kind, action.reason) == (
            DRAIN, "stale code, waiting for the waiter to stop claiming")


def test_an_unknown_job_count_counts_as_busy() -> None:
    (action,) = heal.decide(report(STALE), {}, busy(None), NOW)
    assert (action.kind, action.reason) == (DRAIN, "stale code, unknown running jobs")


def test_a_down_waiter_restarts_even_with_jobs() -> None:
    down = component(state="down", reason="no heartbeat")
    (action,) = heal.decide(report(down), {}, busy(3), NOW)
    assert action.kind == RESTART


def test_draining_writes_the_marker_and_logs_once(tmp_path: Path) -> None:
    _, runs, _ = run(tmp_path, busy(1))
    first_mtime = marker(tmp_path).stat().st_mtime_ns
    run(tmp_path, busy(1), now=NOW + 120)
    assert runs.calls == []
    assert marker(tmp_path).read_text() == f"{CHECKOUT}\n"
    assert marker(tmp_path).stat().st_mtime_ns == first_mtime
    assert log_lines(tmp_path) == [
        f"2027-01-15T08:00:00Z self-heal: drain {LABEL} (stale code, 1 running jobs)"]


def test_a_drained_waiter_restarts_once_its_jobs_finish(tmp_path: Path) -> None:
    run(tmp_path, busy(1))
    _, runs, _ = run(tmp_path, busy(0, drain_acked={"codex-bot": CHECKOUT}), now=NOW + 120)
    assert [call[0] for call in runs.calls] == [
        ["/srv/factory/deploy/waiter-service.sh", "codex-bot", "restart"]]
    assert not marker(tmp_path).exists()
    assert not heal.drain_ack_path(tmp_path, "codex-bot").exists()


def test_a_marker_for_a_waiter_no_longer_stale_is_removed(tmp_path: Path) -> None:
    run(tmp_path, busy(1))
    run(tmp_path, busy(1), data=report(component()), now=NOW + 120)
    assert not marker(tmp_path).exists()


def test_a_drain_over_two_hours_escalates_once_and_kills_nothing(tmp_path: Path) -> None:
    since = NOW - 2 * 3600 - 60
    actions = heal.decide(report(STALE), {}, busy(1, drain_since={"codex-bot": since}), NOW)
    assert [a.kind for a in actions] == [DRAIN, ESCALATE]
    assert actions[1].reason == "draining for 121m, 1 running jobs"
    posts = Posts()
    host = busy(1, drain_since={"codex-bot": since})
    _, runs, _ = run(tmp_path, host, posts=posts)
    run(tmp_path, host, posts=posts, runs=runs, now=NOW + 120)
    assert runs.calls == []
    assert len(posts.calls) == 1


def test_facts_count_running_jobs_per_owned_waiter_and_date_the_marker(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE jobs (id TEXT, recipient TEXT, state TEXT)")
        connection.executemany("INSERT INTO jobs VALUES (?, ?, ?)", [
            ("1", "codex", "running"), ("2", "codex", "running"), ("3", "codex", "queued"),
            ("4", "claude", "running"), ("5", "codex", "done")])
    (tmp_path / "waiter-autostart-codex-bot").touch()
    marker(tmp_path).write_text("x\n")
    host = heal.facts_for("box", tmp_path, tmp_path, db, NOW)
    assert host.waiter_jobs == {"codex-bot": 2}
    assert host.drain_since == {"codex-bot": marker(tmp_path).stat().st_mtime}
    assert heal.facts_for("box", tmp_path, tmp_path, None, NOW).waiter_jobs == {"codex-bot": None}
    assert heal.running_jobs(tmp_path / "missing.db", "codex") is None


# --- the drain handshake: no claim lands between the idle check and the restart ----------------


def real_waiter(tmp_path: Path):
    from tests.test_waiter import FakeSpawn, make_queue, make_waiter

    db, queue = make_queue(tmp_path)
    waiter = make_waiter(tmp_path, queue, db, FakeSpawn())
    waiter.drain = heal.drain_path(tmp_path, "claude-bot")
    waiter.version = "aaaaaaaaaaaa"
    for flag in ("waiter-autostart-claude-bot", "poller-autostart"):
        (tmp_path / flag).touch()
    return db, queue, waiter


def stale_pair():
    return report(component("poller", bot="", version="aaaaaaaaaaaa"),
                  component(bot="claude-bot", version="aaaaaaaaaaaa"))


def heal_with_waiter(tmp_path: Path, db: Path, *, on_service=None, on_sleep=None,
                     refresh: bool = True):
    calls: list[tuple[str, int | None]] = []

    def service(argv, env, log_path):
        if on_service is not None:
            on_service(argv)
        calls.append((Path(argv[0]).name, heal.running_jobs(db, "claude")))
        return 0

    host = heal.facts_for("box", tmp_path, tmp_path, db, NOW)
    heal.heal(stale_pair(), facts=host, state_dir=tmp_path, deploy=Path("deploy"),
              webhook=heal.Webhook(), environ={}, now=NOW, run=service,
              refresh=(lambda: heal.facts_for("box", tmp_path, tmp_path, db, NOW))
              if refresh else None,
              sleep=on_sleep or (lambda _seconds: None))
    return calls


def busy_restarts(calls) -> list:
    return [call for call in calls if call[0] == "waiter-service.sh" and call[1] != 0]


def test_a_claim_during_the_poller_restart_never_gets_its_waiter_restarted(
        tmp_path: Path) -> None:
    from tests.test_waiter import enqueue

    db, queue, waiter = real_waiter(tmp_path)
    job = enqueue(queue, "race")

    def claim_during_poller_restart(argv):
        if argv[0].endswith("poller-service.sh"):
            assert waiter.step() in (job, None)

    calls = heal_with_waiter(tmp_path, db, on_service=claim_during_poller_restart,
                             refresh=False)
    assert busy_restarts(calls) == []
    assert heal.drain_path(tmp_path, "claude-bot").exists()


def test_a_claim_before_the_waiter_acknowledges_keeps_it_draining(tmp_path: Path) -> None:
    from tests.test_waiter import enqueue

    db, queue, waiter = real_waiter(tmp_path)
    job = enqueue(queue, "race")
    claimed: list[str | None] = []

    def waiter_runs(_seconds):
        if not claimed:
            marker, waiter.drain = waiter.drain, None
            claimed.append(waiter.step())
            waiter.drain = marker
        waiter.step()

    calls = heal_with_waiter(tmp_path, db, on_sleep=waiter_runs)
    assert claimed == [job]
    assert busy_restarts(calls) == []
    assert [c for c in calls if c[0] == "waiter-service.sh"] == []
    assert heal.drain_path(tmp_path, "claude-bot").exists()


def test_an_idle_waiter_that_acknowledges_the_drain_restarts_in_the_same_pass(
        tmp_path: Path) -> None:
    from tests.test_waiter import enqueue

    db, queue, waiter = real_waiter(tmp_path)
    job = enqueue(queue, "after")
    steps: list[str | None] = []
    calls = heal_with_waiter(tmp_path, db, on_sleep=lambda _s: steps.append(waiter.step()))
    assert set(steps) == {None}
    assert ("waiter-service.sh", 0) in calls
    assert busy_restarts(calls) == []
    assert heal.running_jobs(db, "claude") == 0
    assert not heal.drain_path(tmp_path, "claude-bot").exists()
    assert not heal.drain_ack_path(tmp_path, "claude-bot").exists()
    assert waiter.step() == job


def test_an_idle_waiter_that_never_acknowledges_is_drained_not_restarted(
        tmp_path: Path) -> None:
    db, _, _ = real_waiter(tmp_path)
    calls = heal_with_waiter(tmp_path, db)
    assert [c for c in calls if c[0] == "waiter-service.sh"] == []
    assert heal.drain_path(tmp_path, "claude-bot").read_text() == f"{CHECKOUT}\n"


def test_a_stale_ack_from_an_earlier_drain_does_not_count(tmp_path: Path) -> None:
    db, _, _ = real_waiter(tmp_path)
    heal.drain_ack_path(tmp_path, "claude-bot").write_text(f"{CHECKOUT}\n")
    calls = heal_with_waiter(tmp_path, db)
    assert [c for c in calls if c[0] == "waiter-service.sh"] == []
