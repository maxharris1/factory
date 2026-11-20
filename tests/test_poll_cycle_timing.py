from __future__ import annotations

import json
from collections import Counter

import pytest

from lib.dispatch.cycle_clock import PHASES, CycleClock
from lib.dispatch.poll import PollLoop
from lib.dispatcher import TicketRef
from lib.github_actions import GhResult
from lib.poll_source import LivePollSource, PollCycleError
from tests.test_live_poll import FakeGh, config
from tests.test_ship_hold import HOLD_FACTORY

PRS = tuple(str(number) for number in range(12, 19))
FILE_READ_SECONDS = 66.0
OTHER_READ_SECONDS = 1.0


class Ticks:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        self.now += 1.5
        return self.now


class Dispatcher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.cycle_clock: CycleClock | None = None

    def poll_once(self, source: object) -> None:
        assert self.cycle_clock is not None
        with self.cycle_clock.phase("feed"):
            pass
        with self.cycle_clock.phase("tracked-read"):
            self.cycle_clock.add_source("acme/widgets", 3.0)
            if self.fail:
                raise RuntimeError("boom")


def test_each_cycle_logs_its_start_end_and_phase_durations(capsys) -> None:
    loop = PollLoop(Dispatcher(), object(), sleep=lambda _: None)
    loop.clock = CycleClock(wall=Ticks())

    loop.run(max_polls=1, after_poll=lambda: None)

    start, end = capsys.readouterr().out.strip().splitlines()
    assert start.startswith("orch poll: cycle start ") and start.endswith(" ET")
    assert end.startswith("orch poll: cycle end ")
    assert "ET (10.5s): feed 1.5s, tracked-read 1.5s [acme/widgets 3.0s], after-poll 1.5s" in end
    assert loop.cycle_state() == (None, 10.5)


def test_a_phase_that_raises_still_writes_the_end_line(capsys) -> None:
    loop = PollLoop(Dispatcher(fail=True), object(), sleep=lambda _: None)
    loop.clock = CycleClock(wall=Ticks())
    errors: list[Exception] = []

    loop.run(max_polls=1, on_error=errors.append)

    end = capsys.readouterr().out.strip().splitlines()[-1]
    assert "tracked-read! 1.5s" in end
    assert [type(error) for error in errors] == [RuntimeError]


def test_the_clock_refuses_a_phase_outside_the_fixed_table() -> None:
    clock = CycleClock()
    clock.start()
    with pytest.raises(ValueError), clock.phase("not-a-phase"):
        pass
    assert "process" in PHASES


def test_a_running_cycle_reports_its_start_until_it_ends() -> None:
    clock = CycleClock(wall=Ticks())
    clock.start()
    started, last = clock.state()
    assert started is not None and last is None
    clock.end()
    assert clock.state() == (None, 1.5)


class CountingGh(FakeGh):
    def __init__(self) -> None:
        super().__init__(HOLD_FACTORY.format(ship="handoff", hold="hold:\n  paths: ['src/**']\n"))
        self.files = [{"filename": "lib/a.py", "status": "modified"}]
        self.heads = {number: "a" * 40 for number in PRS}
        self.bases = {number: "c" * 40 for number in PRS}
        self.fail_files: set[str] = set()
        self.routes: Counter[str] = Counter()

    def __call__(self, argv, timeout):
        endpoint = next(part for part in argv if part.startswith(("repos/", "search/")))
        route = "files" if endpoint.endswith("/files?per_page=100") else "other"
        self.routes[route] += 1
        number = endpoint.split("/")[4] if "/pulls/" in endpoint else None
        if route == "files" and number in self.fail_files:
            return GhResult(1, "", "failed (HTTP 502)")
        if number in self.heads and endpoint.endswith(f"/pulls/{number}"):
            self.calls.append(list(argv))
            pull = {
                "head": {"sha": self.heads[number]},
                "base": {"ref": "main", "sha": self.bases[number]},
            }
            return GhResult(0, json.dumps(pull), "")
        return super().__call__(argv, timeout)

    def simulated_seconds(self) -> float:
        return (
            self.routes["files"] * FILE_READ_SECONDS
            + self.routes["other"] * OTHER_READ_SECONDS
        )


def cycle(source: LivePollSource, gh: CountingGh) -> float:
    gh.routes.clear()
    source.begin_cycle()
    found = source.tracked_tickets(tuple(TicketRef("acme/widgets", n, "github") for n in PRS))
    source.end_cycle()
    assert len(found) == len(PRS)
    return gh.simulated_seconds()


def files_calls(gh: CountingGh) -> int:
    return sum(part.endswith("/files?per_page=100") for call in gh.calls for part in call)


def test_pr_files_are_read_once_per_head_not_once_per_cycle() -> None:
    gh = CountingGh()
    source = LivePollSource(config(), gh_runner=gh)

    for _ in range(3):
        cycle(source, gh)
    assert files_calls(gh) == len(PRS)

    gh.heads["15"] = "b" * 40
    cycle(source, gh)
    assert files_calls(gh) == len(PRS) + 1


def test_a_base_branch_advance_reads_the_files_again() -> None:
    gh = CountingGh()
    source = LivePollSource(config(), gh_runner=gh)
    cycle(source, gh)

    gh.bases["15"] = "d" * 40
    gh.files = [
        {"filename": "lib/a.py", "status": "modified"},
        {"filename": "src/held.py", "status": "modified"},
    ]
    cycle(source, gh)
    source.begin_cycle()
    ticket = source.fresh_ticket(TicketRef("acme/widgets", "15", "github"))

    assert files_calls(gh) == len(PRS) + 1
    assert ticket.changed_files == ("lib/a.py", "src/held.py")


def test_cycles_that_skip_a_stopped_ticket_keep_its_files(tmp_path) -> None:
    from tests.test_dispatcher import setup
    from tests.test_live_poll import _handed_off

    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    _handed_off(queue, "github", list(PRS))
    with queue.connect() as connection:
        connection.executemany(
            "INSERT INTO ticket_reads VALUES ('acme/widgets', ?, 0)", [(n,) for n in PRS]
        )
    gh = CountingGh()
    source = LivePollSource(config(), gh_runner=gh)

    for timestamp in (1000.0, 1060.0, 1300.0):
        now[0] = timestamp
        dispatcher.poll_once(source)

    assert files_calls(gh) == len(PRS)


def test_a_failed_files_read_is_read_again_next_cycle() -> None:
    gh = CountingGh()
    gh.fail_files = {"13"}
    source = LivePollSource(config(), gh_runner=gh, error_sink=lambda *_: None)

    with pytest.raises(PollCycleError):
        cycle(source, gh)
    first = gh.routes["files"]
    gh.fail_files = set()
    cycle(source, gh)

    assert gh.routes["files"] == 1
    assert first == len(PRS)


def test_a_fresh_read_in_the_same_cycle_reads_no_files() -> None:
    gh = CountingGh()
    source = LivePollSource(config(), gh_runner=gh)
    cycle(source, gh)
    before = files_calls(gh)

    source.begin_cycle()
    ticket = source.fresh_ticket(TicketRef("acme/widgets", "14", "github"))

    assert ticket.changed_files == ("lib/a.py",)
    assert files_calls(gh) == before


def test_a_no_change_cycle_over_seven_prs_fits_the_90s_budget() -> None:
    gh = CountingGh()
    source = LivePollSource(config(), gh_runner=gh)

    first = cycle(source, gh)
    later = [cycle(source, gh) for _ in range(2)]

    assert first > 90
    assert all(seconds < 90 for seconds in later), later


class Wall:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def test_the_end_line_counts_github_calls_by_route() -> None:
    wall = Wall()
    clock = CycleClock(wall=wall)
    clock.start()
    with clock.phase("feed"):
        for route, count in (
            ("POST issues/<value>/comments", 1),
            ("GET pulls/<value>", 3),
            ("GET issues/<value>/comments", 3),
        ):
            for _ in range(count):
                clock.add_call(route, "billed")
        clock.add_call("GET contents/factory.md", "304")
        clock.add_call("GET contents/factory.md", "cached")
        clock.add_call("GET graphql", "graphql")
        clock.add_call("GET graphql", "graphql")
        clock.note_core(4990, 5000, wall.now + 601)

    assert clock.end().endswith(
        " ET (0.0s): feed 0.0s; github core 7 [GET issues/<value>/comments 3, "
        "GET pulls/<value> 3, POST issues/<value>/comments 1], "
        "free 2 (304 1, cached 1), graphql 2; core 4990/5000 resets 11m"
    )


def test_the_end_line_names_the_top_eight_routes_and_sums_the_rest() -> None:
    clock = CycleClock(wall=Wall())
    clock.start()
    for index in range(10):
        for _ in range(index + 1):
            clock.add_call(f"GET route-{index}", "billed")

    routes = ", ".join(f"GET route-{index} {index + 1}" for index in range(9, 1, -1))
    assert clock.end().endswith(
        f"; github core 55 [{routes}, other 3], free 0 (304 0, cached 0), graphql 0"
    )


def test_a_cycle_with_no_github_calls_keeps_the_end_line_unchanged() -> None:
    clock = CycleClock(wall=Ticks())
    clock.start()
    with clock.phase("feed"):
        pass

    assert clock.end().endswith(" ET (4.5s): feed 1.5s")


def test_free_calls_alone_print_no_route_brackets() -> None:
    clock = CycleClock(wall=Wall())
    clock.start()
    clock.add_call("GET contents/factory.md", "304")

    assert clock.end().endswith("; github core 0, free 1 (304 1, cached 0), graphql 0")


def test_each_cycle_counts_only_its_own_calls() -> None:
    wall = Wall()
    clock = CycleClock(wall=wall)
    clock.start()
    clock.add_call("GET pulls/<value>", "billed")
    clock.note_core(4990, 5000, wall.now + 60)
    clock.end()

    clock.start()
    with clock.phase("feed"):
        pass

    assert "github" not in clock.end()
