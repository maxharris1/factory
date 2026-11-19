from __future__ import annotations

import ast
import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import lib.ci_route as ci_route
from lib.ci_route import SLOTS, STALL_AFTER, Job, decide, parse_job, reserved_slots

NOW = datetime(2026, 11, 17, 22, 45, 0, tzinfo=timezone.utc)
OWN = 500
EARLIER = 400
LATER = 600
POOL = ("self-hosted", "Linux", "X64", "factory-linux")


def job(name: str = "test (1/3)", status: str = "in_progress", *, run_id: int = EARLIER,
        labels: tuple[str, ...] = POOL, age: timedelta = timedelta(seconds=30)) -> Job:
    return Job(run_id=run_id, name=name, status=status, labels=labels, created_at=NOW - age)


def busy(count: int) -> list[Job]:
    return [job(f"test ({i + 1}/3)") for i in range(count)]


def hosted(online: int, others: list[Job] | None = None, reserved: int = 0) -> tuple[str, ...]:
    return decide(SLOTS, online, others or [], reserved, NOW)


def test_slots_hold_the_rank_order() -> None:
    assert SLOTS == ("test-1", "test-2", "test-3", "statechart", "checks", "py311-compile", "macos-pickup")


def test_job_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        job().status = "completed"


def test_parse_job_reads_the_api_shape() -> None:
    raw = {"id": 55501, "run_id": 19432001, "run_attempt": 1, "node_id": "CR_kwDO", "head_sha": "b2c3d4e",
           "url": "https://api.github.com/repos/maxharris1/factory/actions/jobs/55501",
           "name": "test (2/3)", "status": "queued", "conclusion": None, "workflow_name": "ci",
           "labels": ["self-hosted", "Linux", "X64", "factory-linux"], "created_at": "2026-11-17T22:41:07Z",
           "started_at": "2026-11-17T22:41:07Z", "completed_at": None, "steps": [],
           "runner_id": None, "runner_name": None, "runner_group_id": None, "runner_group_name": None}
    parsed = parse_job(raw)
    assert parsed == Job(run_id=19432001, name="test (2/3)", status="queued", labels=POOL,
                         created_at=datetime(2026, 11, 17, 22, 41, 7, tzinfo=timezone.utc))
    assert parsed.created_at.utcoffset() == timedelta(0)


def test_imports_are_stdlib_only() -> None:
    tree = ast.parse(Path(ci_route.__file__).read_text())
    modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names}
    modules |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert modules and modules <= set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.parametrize(("online", "others", "local"), [
    (3, [], 3),
    (0, [], 0),
    (7, [], 7),
    (9, [], 7),
    (5, busy(2), 3),
])
def test_decide_fills_free_slots_in_rank_order(online: int, others: list[Job], local: int) -> None:
    assert hosted(online, others) == SLOTS[local:]


def test_three_idle_runners_send_the_last_four_slots_hosted() -> None:
    assert hosted(3) == ("statechart", "checks", "py311-compile", "macos-pickup")


def test_pool_jobs_queued_or_running_elsewhere_claim_runners() -> None:
    assert hosted(3, busy(3)) == SLOTS
    assert hosted(3, [job(status="queued")]) == SLOTS[2:]


def test_hosted_and_completed_jobs_take_no_pool_slot() -> None:
    assert hosted(3, [job(labels=("ubuntu-latest",))]) == SLOTS[3:]
    assert hosted(3, [job(status="completed") for _ in range(3)]) == SLOTS[3:]


def test_an_earlier_run_that_routed_but_lists_no_slot_jobs_reserves_every_slot() -> None:
    earlier = [job("changes", "completed")]
    reserved = reserved_slots(OWN, earlier)
    assert reserved == len(SLOTS)
    assert hosted(3, earlier, reserved) == SLOTS


def test_a_later_run_never_reserves_for_an_earlier_one() -> None:
    assert reserved_slots(OWN, [job("changes", "completed", run_id=LATER)]) == 0


def test_an_earlier_run_with_listed_slot_jobs_counts_through_its_jobs_not_a_reservation() -> None:
    earlier = [job("changes", "completed"), job("test (1/3)", "queued")]
    assert reserved_slots(OWN, earlier) == 0
    assert hosted(3, earlier) == SLOTS[2:]


def test_reservations_add_up_across_earlier_runs() -> None:
    runs = [job("changes", "completed", run_id=run_id) for run_id in (100, 200, LATER)]
    assert reserved_slots(OWN, runs) == 2 * len(SLOTS)


def test_a_pool_job_queued_past_the_stall_window_with_nothing_running_sends_everything_hosted() -> None:
    stale = job(status="queued", age=STALL_AFTER + timedelta(seconds=1))
    assert hosted(3, [stale]) == SLOTS


def test_a_pool_job_queued_exactly_the_stall_window_is_not_a_stall() -> None:
    assert hosted(3, [job(status="queued", age=STALL_AFTER)]) == SLOTS[2:]


def test_a_stale_queued_job_beside_a_running_pool_job_is_not_a_stall() -> None:
    stale = job(status="queued", age=STALL_AFTER + timedelta(minutes=5))
    assert hosted(3, [stale, job()]) == SLOTS[1:]


def test_decide_does_not_flip_flop() -> None:
    before = dict(vars(ci_route))
    locals_seen = [len(SLOTS) - len(hosted(3, busy(n))) for n in (3, 2, 3)]
    assert locals_seen == [0, 1, 0]
    assert dict(vars(ci_route)) == before

