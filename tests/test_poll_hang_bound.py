from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.dispatch.cycle_clock import CycleClock
from lib.dispatcher import Dispatcher
from lib.intake_live import GitHubIntakeClient
from lib.orch_yml import LinearConfig, OrchConfig, Project
from lib.poll_source import HANG_STRIKES, LivePollSource, PollCycleError
from lib.queue import SQLiteJobQueue
from tests.test_live_poll import FACTORY, FakeGh

DEADLINE = 0.3
ATTEMPT = 0.2
SLACK = 1.5
BOUND = HANG_STRIKES * DEADLINE + SLACK
GITHUB = tuple(str(number) for number in range(12, 18))
LINEAR = ("AUT-1", "AUT-2", "AUT-3", "AUT-4")


def mixed_config() -> OrchConfig:
    return OrchConfig(
        poll_seconds=60,
        projects=(
            Project("acme/widgets", factory_ref="main"),
            Project(
                "acme/linear",
                factory_ref="main",
                linear_project_ids=frozenset({"project-1"}),
            ),
        ),
        linear=LinearConfig("LINEAR_KEY"),
        trusted_logins={"grok-login": frozenset({"grok-bot"})},
    )


def github_config() -> OrchConfig:
    return OrchConfig(poll_seconds=60, projects=(Project("acme/widgets", factory_ref="main"),))


def tracked_dispatcher(tmp_path: Path, refs) -> Dispatcher:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"github": {"grok-login": {"grok-bot"}}, "linear": {}},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    with queue.connect() as connection:
        connection.executemany(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) VALUES (?, ?, ?, 0)",
            refs,
        )
    dispatcher.process = Mock()
    dispatcher.reconcile_jobs = Mock()
    return dispatcher


def hanging_gh(calls: list[float]):
    def runner(argv, timeout):
        calls.append(timeout)
        time.sleep(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)

    return runner


def processed(dispatcher: Dispatcher) -> list[str]:
    return [call.args[0].ticket_id for call in dispatcher.process.call_args_list]


def test_a_hanging_gh_read_ends_the_cycle_within_the_bound_and_is_read_next_cycle(
    tmp_path: Path,
) -> None:
    dispatcher = tracked_dispatcher(
        tmp_path, [("acme/widgets", number, "github") for number in GITHUB]
    )
    calls: list[float] = []
    healthy = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    hanging = [True]
    warnings: list[str] = []

    def runner(argv, timeout):
        if hanging[0]:
            return hanging_gh(calls)(argv, timeout)
        return healthy(argv, timeout)

    source = LivePollSource(
        github_config(),
        gh_runner=runner,
        error_sink=dispatcher.record_poll_error,
        gh_call_timeout_seconds=ATTEMPT,
        read_deadline_seconds=DEADLINE,
    )
    source.warning_sink = warnings.append

    began = time.monotonic()
    with pytest.raises(PollCycleError):
        dispatcher.poll_once(source)
    elapsed = time.monotonic() - began

    assert elapsed < BOUND, elapsed
    assert sum(calls) <= HANG_STRIKES * DEADLINE + 1e-6
    assert processed(dispatcher) == []
    assert warnings == [
        f"github skipped for the rest of this cycle after {HANG_STRIKES} "
        "timed-out reads; read again next cycle"
    ]
    with dispatcher._connect() as connection:
        errored = {row[0] for row in connection.execute("SELECT ticket_id FROM poll_errors")}
    assert set(GITHUB) <= errored

    hanging[0] = False
    dispatcher.poll_once(source)

    assert processed(dispatcher) == list(GITHUB)


def test_a_hanging_linear_read_ends_the_cycle_within_the_bound_and_github_goes_on(
    tmp_path: Path,
) -> None:
    dispatcher = tracked_dispatcher(
        tmp_path,
        [("acme/widgets", "12", "github")]
        + [("acme/linear", key, "linear") for key in LINEAR],
    )
    never = threading.Event()
    linear_calls: list[float] = []

    def transport(url, headers, body, timeout):
        linear_calls.append(timeout)
        never.wait()

    source = LivePollSource(
        mixed_config(),
        environ={"LINEAR_KEY": "key"},
        gh_runner=FakeGh(FACTORY.format(tickets="github", ship="merge")),
        linear_transport=transport,
        error_sink=dispatcher.record_poll_error,
        read_deadline_seconds=DEADLINE,
    )

    began = time.monotonic()
    try:
        with pytest.raises(PollCycleError, match="acme/linear x4"):
            dispatcher.poll_once(source)
    finally:
        never.set()
    elapsed = time.monotonic() - began

    assert elapsed < BOUND, elapsed
    assert linear_calls == [DEADLINE] * HANG_STRIKES
    assert processed(dispatcher) == ["12"]


def test_a_hanging_intake_read_skips_github_after_the_strikes() -> None:
    calls: list[float] = []
    source = LivePollSource(
        github_config(),
        gh_runner=hanging_gh(calls),
        gh_call_timeout_seconds=ATTEMPT,
        read_deadline_seconds=DEADLINE,
    )
    source.begin_cycle()
    intake = GitHubIntakeClient(runner=source._adapter_runner)

    began = time.monotonic()
    for repo in ("acme/widgets", "acme/gadgets", "acme/tools", "acme/more"):
        with pytest.raises(Exception, match="timed out|skipped"):
            intake.github_intake(repo, "factory:ready")
    elapsed = time.monotonic() - began

    assert elapsed < BOUND, elapsed
    assert sum(calls) <= HANG_STRIKES * DEADLINE + 1e-6


def test_the_feeder_reads_github_through_the_bounded_poll_runner(tmp_path: Path) -> None:
    from lib.cli import _feeder

    source = LivePollSource(mixed_config())
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 10)

    feeder = _feeder(mixed_config(), queue, None, source, [])

    assert feeder.github.runner == source._adapter_runner


def test_the_cycle_line_names_the_reads_inside_feed_and_tracked_read(
    tmp_path: Path,
) -> None:
    dispatcher = tracked_dispatcher(
        tmp_path, [("acme/widgets", number, "github") for number in GITHUB[:3]]
    )
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    now = [0.0]

    def slow(argv, timeout):
        now[0] += 1.0
        return gh(argv, timeout)

    source = LivePollSource(github_config(), gh_runner=slow)
    project = source.config.projects[0]
    dispatcher.feeder = SimpleNamespace(
        feed_once=lambda: (source.factory_context(project), SimpleNamespace(fed=()))[1]
    )
    clock = CycleClock(wall=lambda: now[0])
    dispatcher.cycle_clock = clock
    clock.start()
    dispatcher.poll_once(source)
    line = clock.end()

    for phase in ("feed", "tracked-read"):
        match = re.search(rf"{phase} ([\d.]+)s \[([^\]]*)\]", line)
        assert match, line
        named = sum(float(seconds) for seconds in re.findall(r" ([\d.]+)s", match.group(2)))
        assert "acme/widgets" in match.group(2)
        assert float(match.group(1)) > 0
        assert named >= 0.9 * float(match.group(1)), line
