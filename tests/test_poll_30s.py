"""GitHub polls every 30 seconds; Linear keeps its own 60-second cadence.

orch.yml poll_seconds may be 30 or 60. It sets how long `orch poll` sleeps
after each cycle (GitHub reads run every cycle). Linear work (discovery
search, dirty-row reads, tracked reads) runs on its own 60-second window
with the same per-window budget as before, so a 30-second poller makes no
more Linear calls per hour than a 60-second one. All transports and secrets
here are fakes.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib.dispatch.cycle_clock import CycleClock
from lib.dispatcher import POLL_SECONDS, Dispatcher, PollLoop, TicketRef
from lib.orch_yml import (
    LINEAR_POLL_SECONDS,
    POLL_SECONDS_ALLOWED,
    OrchConfig,
    Project,
    load_orch_yml,
)
from lib.poll_source import (
    LINEAR_COMPLEXITY_PER_HOUR,
    LINEAR_ISSUE_COMPLEXITY,
    LINEAR_REQUESTS_PER_HOUR,
    LINEAR_SEARCH_COMPLEXITY,
    LINEAR_SHARE,
    HTTPResult,
    LivePollSource,
    Pacing,
)
from lib.queue import SQLiteJobQueue
from tests.test_live_poll import (
    FACTORY,
    FakeGh,
    config,
    linear_issue,
    linear_search_page,
    linear_states_page,
)
from tests.test_live_poll_no_webhook import (
    PINNED_GH_CALLS,
    PINNED_READS,
    REPO,
    SEARCHES,
    Clock,
    Linear,
    _dispatcher,
    issue,
    source_for,
)
from tests.test_live_poll_webhook import FakeDirty, cycle, refs, webhook_config

ROOT = Path(__file__).resolve().parents[1]


def at(seconds: int, cfg: OrchConfig | None = None) -> OrchConfig:
    return replace(cfg if cfg is not None else config(linear=True), poll_seconds=seconds)


# --- orch.yml ---------------------------------------------------------------


@pytest.mark.parametrize("seconds", [30, 60])
def test_poll_seconds_30_and_60_are_accepted(tmp_path: Path, seconds: int) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        f"poll_seconds: {seconds}\nprojects:\n  - repo: acme/widgets\n",
        encoding="utf-8",
    )
    loaded, findings = load_orch_yml(path)
    assert findings == []
    assert loaded is not None and loaded.poll_seconds == seconds


@pytest.mark.parametrize(
    "value",
    ["0", "-30", "1", "15", "29", "31", "45", "59", "61", "90", "120",
     "30.0", "'30'", "true", "null", "[30]"],
)
def test_poll_seconds_outside_30_or_60_is_refused(tmp_path: Path, value: str) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        f"poll_seconds: {value}\nprojects:\n  - repo: acme/widgets\n",
        encoding="utf-8",
    )
    loaded, findings = load_orch_yml(path)
    assert loaded is None
    assert [f.message for f in findings if f.severity == "error"] == [
        "poll_seconds must be 30 or 60"
    ]


def test_allowed_set_and_linear_cadence_constants() -> None:
    assert POLL_SECONDS_ALLOWED == (30, 60)
    assert LINEAR_POLL_SECONDS == 60
    assert POLL_SECONDS == 60


def test_shipped_orch_yml_and_example_poll_github_every_30_seconds() -> None:
    for name in ("orch.yml", "config/orch.example.yml"):
        loaded, findings = load_orch_yml(ROOT / name, allow_placeholders=True)
        assert loaded is not None, (name, findings)
        assert loaded.poll_seconds == 30, name


# --- poll loop interval -------------------------------------------------------


class _Dispatcher:
    def __init__(self) -> None:
        self.polls = 0

    def poll_once(self, source) -> None:
        del source
        self.polls += 1


class _Source:
    def __init__(self, seconds: int) -> None:
        self.config = at(seconds)


@pytest.mark.parametrize("seconds", [30, 60])
def test_poll_loop_sleeps_the_configured_interval_after_each_cycle(seconds: int) -> None:
    dispatcher = _Dispatcher()
    sleeps: list[float] = []
    PollLoop(dispatcher, _Source(seconds), sleep=sleeps.append).run(max_polls=3)
    assert dispatcher.polls == 3
    assert sleeps == [seconds, seconds]


def test_poll_loop_without_a_config_keeps_60_seconds() -> None:
    sleeps: list[float] = []
    PollLoop(_Dispatcher(), object(), sleep=sleeps.append).run(max_polls=2)
    assert sleeps == [60]


class _PacedSource(_Source):
    def __init__(self) -> None:
        super().__init__(30)
        self.asked: list[tuple[float, int, float]] = []

    def pacing(self, base_interval: float, recent_billed: int, cycle_seconds: float) -> Pacing:
        self.asked.append((base_interval, recent_billed, cycle_seconds))
        return Pacing(90.0, False, "stretch")


class _BetweenClock(CycleClock):
    def __init__(self) -> None:
        super().__init__()
        self.asked: list[float] = []

    def between(self, asked: float) -> str | None:
        self.asked.append(asked)
        return super().between(asked)


def test_poll_loop_sleeps_the_paced_interval_and_tells_between(capsys) -> None:
    source = _PacedSource()
    sleeps: list[float] = []
    loop = PollLoop(_Dispatcher(), source, sleep=sleeps.append)
    loop.clock = _BetweenClock()

    loop.run(max_polls=2)

    assert sleeps == [90.0]
    assert loop.clock.asked == [30, 90.0]
    assert [(base, billed) for base, billed, _ in source.asked] == [(30, 0)]
    assert "orch poll: pacing stretch, next cycle in 90s" in capsys.readouterr().out


# --- GitHub polls every 30-second cycle ---------------------------------------


def _github_only(seconds: int) -> OrchConfig:
    return replace(
        config(), poll_seconds=seconds, projects=(Project(REPO, factory_ref="main"),)
    )


@pytest.mark.parametrize("seconds", [30, 60])
def test_github_discovery_runs_every_cycle(tmp_path: Path, seconds: int) -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    linear_calls: list[bytes] = []
    source = LivePollSource(
        _github_only(seconds),
        environ={},
        gh_runner=gh,
        linear_transport=lambda url, headers, body, timeout: linear_calls.append(body),
        clock=Clock(),
    )
    dispatcher = _dispatcher(tmp_path / "orch.db")
    per_cycle = []
    for _ in range(4):
        dispatcher.poll_once(source)
        per_cycle.append(
            sum(
                any(part.endswith("/issues?state=open&per_page=100") for part in call)
                for call in gh.calls
            )
        )
        gh.calls.clear()
    assert per_cycle == [1, 1, 1, 1]
    assert linear_calls == []


# --- Linear without a webhook: same reads, every other 30-second cycle --------


def _run_no_webhook(tmp_path: Path, seconds: int, cycles: int) -> tuple[list, list]:
    database = tmp_path / "orch.db"
    dispatcher = _dispatcher(database)
    with dispatcher.queue.connect() as connection:
        for identifier in ("AUT-1", "AUT-2"):
            connection.execute(
                "INSERT INTO tracked_tickets VALUES (?, ?, 'linear', 0)",
                (REPO, identifier),
            )
    linear = Linear(
        [issue("AUT-1"), issue("AUT-2"), issue("AUT-3")],
        hits=["AUT-1", "AUT-2", "AUT-3"],
    )
    clock = Clock()
    source, gh = source_for(at(seconds), linear, clock=clock)
    source.error_sink = dispatcher.record_poll_error
    source.deferral_sink = dispatcher.record_poll_deferral
    reads, gh_calls = [], []
    for _ in range(cycles):
        dispatcher.poll_once(source)
        reads.append(linear.take())
        gh_calls.append(len(gh.calls))
        gh.calls.clear()
        clock.now += seconds
    return reads, gh_calls


def test_60_second_poller_keeps_todays_pinned_linear_reads(tmp_path: Path) -> None:
    reads, gh_calls = _run_no_webhook(tmp_path, 60, 3)
    assert reads == PINNED_READS
    assert gh_calls == PINNED_GH_CALLS


def test_30_second_poller_reads_linear_only_every_other_cycle(tmp_path: Path) -> None:
    reads, _gh_calls = _run_no_webhook(tmp_path, 30, 6)
    # Linear cycles carry exactly the 60-second poller's reads; the cycles
    # between them make no Linear call at all.
    assert reads[0::2] == PINNED_READS
    assert reads[1::2] == [[], [], []]


def test_30_second_poller_uses_the_same_per_window_linear_budget() -> None:
    fast = LivePollSource(at(30), environ={"LINEAR_KEY": "key"})
    slow = LivePollSource(at(60), environ={"LINEAR_KEY": "key"})
    for source in (fast, slow):
        source.begin_cycle()
    query = "query FactoryIssue"
    for reserve in (0, 5, 19, 20):
        assert fast._linear_request_available(query, reserve=reserve) == (
            slow._linear_request_available(query, reserve=reserve)
        )
    assert fast._linear_request_available(query, reserve=19)
    assert not fast._linear_request_available(query, reserve=20)


def test_off_cycle_linear_reads_count_against_the_current_window() -> None:
    calls = 0

    def transport(url, headers, body, timeout):
        nonlocal calls
        del url, headers, body, timeout
        calls += 1
        return HTTPResult(200, json.dumps({"data": {"issue": None}}))

    source = LivePollSource(
        at(30), environ={"LINEAR_KEY": "key"}, linear_transport=transport
    )
    limit = int(LINEAR_REQUESTS_PER_HOUR * LINEAR_SHARE) * LINEAR_POLL_SECONDS // 3600
    source.begin_cycle()  # Linear window opens
    for index in range(limit - 1):
        assert source._linear_issue(f"AUT-{index:03d}") is None
    source.end_cycle()
    source.begin_cycle()  # GitHub-only cycle: the window's counters carry on
    assert source._linear_issue("AUT-last") is None
    assert not source._linear_request_available("query FactoryIssue")
    source.end_cycle()
    source.begin_cycle()  # next Linear window: a fresh budget
    assert source._linear_request_available("query FactoryIssue")
    assert calls == limit


def test_off_cycle_fresh_ticket_still_reads_linear_directly() -> None:
    linear = Linear([issue("AUT-1")], hits=[])
    source, _gh = source_for(at(30), linear)
    source.begin_cycle()
    source.end_cycle()
    source.begin_cycle()  # GitHub-only cycle
    ticket = source.fresh_ticket(TicketRef(REPO, "AUT-1", "linear"))
    assert ticket is not None and ticket.ticket_id == "AUT-1"
    assert linear.take() == [("issue", "AUT-1")]


# --- Linear hour: identical calls at 30 and 60 seconds ------------------------


def _hour(tmp_path: Path, seconds: int, matching: int, active: int, done: int):
    issues, by_id = [], {}
    for index in range(matching):
        identifier = f"AUT-{index:04d}"
        value = linear_issue(identifier=identifier)
        value["id"] = f"issue-{index:04d}"
        value["comments"]["nodes"][0]["id"] = f"comment-{index:04d}"
        issues.append(value)
        by_id[value["id"]] = value
        by_id[identifier] = value
    searchable = [v for i, v in enumerate(issues) if i < active or i >= active + done]
    requests: list[tuple[int, dict]] = []
    current = 0

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        requests.append((current, request))
        if request["query"].startswith("query FactoryStates"):
            return linear_states_page(request["variables"])
        if request["query"].startswith("query FactoryPoll"):
            start = int(request["variables"]["after"] or 0)
            end = min(start + 50, len(searchable))
            return linear_search_page(
                [
                    {"id": v["id"], "identifier": v["identifier"], "project": v["project"]}
                    for v in searchable[start:end]
                ],
                has_next=end < len(searchable),
                cursor=str(end) if end < len(searchable) else None,
            )
        return HTTPResult(
            200, json.dumps({"data": {"issue": by_id[request["variables"]["id"]]}})
        )

    database = tmp_path / f"orch-{seconds}.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"github": {}, "linear": {"linear:user-1": {"grok-bot"}}},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    # process() makes no Linear call; skipping its per-ticket work keeps the hour fast.
    dispatcher.process = lambda ticket: None
    with queue.connect() as connection:
        for index in range(active + done):
            identifier = f"AUT-{index:04d}"
            connection.execute(
                "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at)"
                " VALUES ('acme/widgets', ?, 'linear', 0)",
                (identifier,),
            )
            connection.execute(
                "INSERT INTO ticket_state (repo, ticket_id, workflow_state,"
                " gate_count, stuck, generation) VALUES ('acme/widgets', ?, ?, 0, 0, 1)",
                (identifier, "building" if index < active else "done"),
            )
    source = LivePollSource(
        at(seconds),
        environ={"LINEAR_KEY": "key"},
        gh_runner=FakeGh(FACTORY.format(tickets="linear", ship="handoff")),
        linear_transport=transport,
        error_sink=dispatcher.record_poll_error,
        deferral_sink=dispatcher.record_poll_deferral,
    )
    cycles = 3600 // seconds
    for current in range(cycles):
        dispatcher.poll_once(source)
    with queue.connect() as connection:
        errors = connection.execute("SELECT ticket_id, error FROM poll_errors").fetchall()
    assert [tuple(row) for row in errors] == []
    return requests, cycles


@pytest.mark.parametrize(
    ("matching", "active", "done"),
    [(5, 1, 2), (25, 5, 10), (100, 20, 40), (500, 20, 460)],
)
def test_linear_hour_is_identical_at_30_and_60_seconds(
    tmp_path: Path, matching: int, active: int, done: int
) -> None:
    slow, slow_cycles = _hour(tmp_path, 60, matching, active, done)
    fast, fast_cycles = _hour(tmp_path, 30, matching, active, done)
    assert (slow_cycles, fast_cycles) == (60, 120)

    def complexity(requests):
        return sum(
            LINEAR_SEARCH_COMPLEXITY
            if request["query"].startswith("query FactoryPoll")
            else LINEAR_ISSUE_COMPLEXITY
            for _, request in requests
        )

    # Same calls, same order, same hourly total and complexity.
    assert [r for _, r in fast] == [r for _, r in slow]
    assert len(fast) <= int(LINEAR_REQUESTS_PER_HOUR * LINEAR_SHARE)
    assert complexity(fast) == complexity(slow)
    assert complexity(fast) <= int(LINEAR_COMPLEXITY_PER_HOUR * LINEAR_SHARE)
    # Every Linear call lands on an even (Linear) cycle at 30 seconds.
    assert {cycle_index % 2 for cycle_index, _ in fast} <= {0}
    assert [c // 2 for c, _ in fast] == [c for c, _ in slow]


# --- Linear webhook: dirty reads, fallback reconcile, budget unchanged --------


def test_webhook_reconcile_fallback_keeps_its_times_at_30_seconds() -> None:
    linear = Linear([issue("AUT-1"), issue("AUT-5")], hits=["AUT-1", "AUT-5"])
    clock = Clock()
    source, _gh = source_for(at(30, webhook_config()), linear, clock=clock)
    source.linear_dirty = FakeDirty()
    tracked = refs("AUT-1")
    reads_by_cycle = []
    for _ in range(62):  # 31 minutes at 30 seconds
        cycle(source, tracked, exclude=frozenset({"AUT-1"}))
        reads_by_cycle.append(linear.take())
        clock.now += 30

    full = SEARCHES + [("issue", "id-AUT-5"), ("issue", "AUT-1")]
    # Same 900-second fallback as at 60 seconds: minutes 0, 15 and 30.
    assert [i for i, reads in enumerate(reads_by_cycle) if reads] == [0, 30, 60]
    assert reads_by_cycle[0] == reads_by_cycle[30] == reads_by_cycle[60] == full


def test_webhook_dirty_row_is_read_once_on_the_next_linear_cycle() -> None:
    linear = Linear([issue("AUT-9")], hits=[])
    clock = Clock()
    source, _gh = source_for(at(30, webhook_config()), linear, clock=clock)
    dirty = FakeDirty()
    source.linear_dirty = dirty

    cycle(source, ())  # Linear cycle: the startup reconcile search
    assert linear.take() == SEARCHES
    clock.now += 30
    dirty.add("id-AUT-9", "AUT-9", 5.0)
    candidates, _ = cycle(source, ())  # GitHub-only cycle
    assert candidates == ()
    assert linear.take() == []
    assert "id-AUT-9" in dirty.rows  # still queued, not acked unread

    clock.now += 30
    candidates, _ = cycle(source, ())  # Linear cycle
    assert [t.ticket_id for t in candidates] == ["AUT-9"]
    assert linear.take() == [("issue", "id-AUT-9")]
    assert dirty.rows == {}

    for _ in range(4):
        clock.now += 30
        cycle(source, ())
    assert linear.take() == []


def test_webhook_tracked_reads_match_the_60_second_poller() -> None:
    def run(seconds: int) -> list[list[tuple[str, str]]]:
        linear = Linear([issue("AUT-1"), issue("AUT-2")], hits=[])
        clock = Clock()
        source, _gh = source_for(at(seconds, webhook_config()), linear, clock=clock)
        dirty = FakeDirty()
        source.linear_dirty = dirty
        source.active_tickets = lambda: frozenset({(REPO, "AUT-2")})
        tracked = refs("AUT-1", "AUT-2")
        linear_windows = []
        for index in range(600 // seconds):
            if clock.now == 1000.0 + 240:
                dirty.add("id-AUT-1", "AUT-1", clock.now)
            cycle(source, tracked, exclude=frozenset({"AUT-1", "AUT-2"}))
            reads = linear.take()
            if seconds == 60 or index % 2 == 0:
                linear_windows.append(reads)
            else:
                assert reads == []
            clock.now += seconds
        return linear_windows

    assert run(30) == run(60)
