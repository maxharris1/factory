from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib.dispatch.constants import STOPPED_POLL_SECONDS
from lib.dispatcher import Dispatcher, TrackedTicket
from tests.test_dispatcher import GO, comment, setup, ticket
from tests.test_lifecycle import outcome
from tests.test_rounds_and_caps import Flow

REPO = "owner/repo"
STOPPED = {
    "1": ("handed-off", False),
    "2": ("capped", False),
    "3": ("blocked", False),
    "4": ("refused", False),
    "5": ("review", True),
}


def _seed_states(queue, states: dict[str, tuple[str | None, bool]]) -> None:
    with queue.connect() as connection:
        for ticket_id, (state, stuck) in states.items():
            connection.execute(
                "INSERT INTO tracked_tickets VALUES (?, ?, 'github', 0)", (REPO, ticket_id)
            )
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, workflow_state, timeout_seconds
                ) VALUES (?, ?, 0, ?, ?, 3600)
                """,
                (REPO, ticket_id, stuck, state),
            )


class RefSource:
    def __init__(self, read=lambda ref: ticket(ticket_id=ref.ticket_id)):
        self.read = read
        self.refs: list[tuple[str, ...]] = []

    def github_candidates(self, label_prefix: str, *, exclude=frozenset()) -> tuple:
        return ()

    def linear_candidates(self, query, *, exclude, reserve) -> tuple:
        return ()

    def tracked_tickets(self, refs) -> tuple[TrackedTicket, ...]:
        self.refs.append(tuple(ref.ticket_id for ref in refs))
        return tuple(self.read(ref) for ref in refs)


def _poll(
    dispatcher: Dispatcher,
    source: RefSource,
    now: list[float],
    cycle_seconds: float,
    cycles: int,
) -> list[tuple[str, ...]]:
    offered = []
    for _ in range(cycles):
        before = len(source.refs)
        dispatcher.poll_once(source)
        offered.append(source.refs[before])
        now[0] += cycle_seconds
    return offered


def _reads(
    tmp_path: Path,
    states: dict[str, tuple[str | None, bool]],
    cycle_seconds: float,
    cycles: int,
) -> dict[str, list[int]]:
    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    _seed_states(queue, states)
    offered = _poll(dispatcher, RefSource(), now, cycle_seconds, cycles)
    return {
        ticket_id: [cycle for cycle, refs in enumerate(offered) if ticket_id in refs]
        for ticket_id in states
    }


def test_stopped_states_are_read_every_300_seconds(tmp_path: Path) -> None:
    reads = _reads(tmp_path, STOPPED, 30, 30)
    for ticket_id, cycles in reads.items():
        assert len(cycles) == 3, ticket_id
        assert [later - earlier for earlier, later in zip(cycles, cycles[1:])] == [10, 10]


@pytest.mark.parametrize("cycle_seconds", [300, 420, 900])
def test_a_stopped_ticket_whose_read_is_300_seconds_old_is_offered_next_cycle(
    tmp_path: Path, cycle_seconds: float
) -> None:
    reads = _reads(tmp_path, STOPPED, cycle_seconds, 6)
    for ticket_id, cycles in reads.items():
        assert cycles[0] <= 1, ticket_id
        assert cycles == list(range(cycles[0], 6)), ticket_id


def test_slow_cycles_read_a_stopped_ticket_every_third_cycle(tmp_path: Path) -> None:
    reads = _reads(tmp_path, STOPPED, 120, 12)
    for ticket_id, cycles in reads.items():
        assert [later - earlier for earlier, later in zip(cycles, cycles[1:])] == [3] * (
            len(cycles) - 1
        ), ticket_id
        assert len(cycles) >= 3, ticket_id


def test_active_states_are_read_every_cycle(tmp_path: Path) -> None:
    active = {
        "1": (None, False),
        "2": ("building", False),
        "3": ("review", False),
        "4": ("fixing", False),
        "5": ("awaiting-ok", False),
        "6": ("waiting-max", False),
        "7": ("waiting-external", False),
    }
    reads = _reads(tmp_path, active, 30, 5)
    assert all(cycles == [0, 1, 2, 3, 4] for cycles in reads.values())


def test_stopped_reads_after_a_start_are_spread_within_300_seconds(tmp_path: Path) -> None:
    stopped = {str(number): ("handed-off", False) for number in range(40)}
    reads = _reads(tmp_path, stopped, 30, 11)
    first = {cycles[0] for cycles in reads.values()}
    assert all(cycles for cycles in reads.values())
    assert len(first) >= 5


@pytest.mark.parametrize("cycle_seconds", [30, 120, 420])
def test_a_restart_does_not_change_when_a_stopped_ticket_is_read(
    tmp_path: Path, cycle_seconds: float
) -> None:
    now = [1000.0]
    running, queue = setup(tmp_path / "running", now)
    _seed_states(queue, {str(number): ("capped", False) for number in range(40)})
    _poll(running, RefSource(), now, cycle_seconds, 4)
    (tmp_path / "restarted").mkdir()
    with sqlite3.connect(tmp_path / "running" / "d.db") as live, sqlite3.connect(
        tmp_path / "restarted" / "d.db"
    ) as copy:
        live.backup(copy)
    restarted, _ = setup(tmp_path / "restarted", now)

    restarted_at = now[0]
    expected = _poll(running, RefSource(), now, cycle_seconds, 12)
    now[0] = restarted_at
    assert _poll(restarted, RefSource(), now, cycle_seconds, 12) == expected


def test_a_go_on_a_capped_ticket_is_handled_within_3_slow_cycles(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    source = RefSource(read=lambda ref: flow.snapshot())

    first_read = next(
        cycle
        for cycle in range(4)
        if "42" in _poll(flow.dispatcher, source, flow.now, 120, 1)[0]
    )
    assert first_read <= 3
    flow.comments.append(
        comment("go-2", GO.format(state="building"), author="grok-login", created_at=flow.now[0])
    )

    handled_at = None
    for cycle in range(1, 4):
        _poll(flow.dispatcher, source, flow.now, 120, 1)
        if outcome(flow.queue, "go-2") is not None:
            handled_at = cycle
            break

    assert handled_at is not None and handled_at <= 3
    assert outcome(flow.queue, "go-2") == "queued-fix"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
