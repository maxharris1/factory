from __future__ import annotations

import pytest

from lib import leftovers, remote
from lib.leftovers import Keep, Reap, plan_leftovers
from lib.remote import Called, ListedRun

TAG = "a" * 32
BASE = remote.RemoteTarget("pc", "fakepc", "claude-bot", "/w", "containers")


@pytest.mark.parametrize(
    ("state", "held", "ended", "verdict"),
    [
        pytest.param(None, frozenset(), True, Keep("its job is not in orch.db"), id="unknown job"),
        pytest.param("running", frozenset(), True, Keep("its job is running"), id="running"),
        pytest.param("queued", frozenset(), True, Keep("its job is queued"), id="a requeued job's old tag"),
        pytest.param("unreadable", frozenset(), True, Keep("its job is unreadable"), id="unreadable state"),
        pytest.param("done", frozenset({TAG}), True, Keep("a held slot carries its tag"), id="done and held"),
        pytest.param("done", None, True, Keep("the held slots are unreadable"), id="slots unreadable"),
        pytest.param("cancelled", frozenset(), False, Keep("its run has not ended"), id="not ended"),
        pytest.param("done", frozenset(), True, Reap(), id="done"),
        pytest.param("failed", frozenset({"b" * 32}), True, Reap(), id="failed"),
        pytest.param("cancelled", frozenset(), True, Reap(), id="cancelled"),
    ],
)
def test_plan_leftovers(state, held, ended, verdict) -> None:
    run = ListedRun("job1", TAG, ended)
    assert plan_leftovers([run], {"job1": state}.get, lambda: held) == [(run, verdict)]


def tag(n: int) -> str:
    return f"{n:032x}"


class Device:
    def __init__(self, listing: Called, reaps: dict[str, Called] | None = None):
        self.listing = listing
        self.reaps = reaps or {}
        self.reaped: list[str] = []

    def __call__(self, script: str) -> Called:
        if remote.REAP_MARK not in script:
            return self.listing
        (found,) = [t for t in self.reaps if f"t={t}" in script]
        self.reaped.append(found)
        return self.reaps[found]


def listed(*rows: tuple[str, int, int]) -> str:
    return "".join(f"{remote.LEFTOVER_MARK} {job} {tag(n)} {ended}\n" for job, n, ended in rows)


STATES = {"done1": "done", "done2": "done", "failed1": "failed", "gone1": "cancelled", "run1": "running"}


def test_sweep_lines() -> None:
    device = Device(
        Called(0, "Welcome to the profile\n" + listed(
            ("done1", 1, 1), ("done2", 2, 1), ("failed1", 3, 1), ("gone1", 4, 1), ("run1", 5, 1),
            ("failed1", 6, 1),
        ) + f"{remote.LEFTOVER_MARK} done1 not-a-tag 1\n{remote.LEFTOVER_MARK} two words\n", ""),
        {
            tag(1): Called(0, f"{remote.REAP_MARK} 0 -\n", ""),
            tag(2): Called(0, f"motd\n{remote.REAP_MARK} 1 0\n", ""),
            tag(3): Called(0, f"{remote.REAP_MARK} 1 1\n", ""),
            tag(4): Called(1, "", "boom"),
            tag(6): Called(0, f"{remote.REAP_MARK} 3 3\n", ""),
        },
    )
    lines = leftovers.sweep(device, BASE, STATES.get, lambda: frozenset())
    assert lines == [
        "kept 2 unreadable run line(s) on pc",
        f"killed what was left of run {tag(2)} of job done2 on pc",
        f"kept run {tag(3)} of job failed1 on pc: still running after a kill",
        f"could not sweep run {tag(4)} of job gone1 on pc (exit 1: boom)",
        f"kept run {tag(5)} of job run1 on pc: its job is running",
        f"kept run {tag(6)} of job failed1 on pc: not known to be gone after a kill",
    ]
    assert device.reaped == [tag(1), tag(2), tag(3), tag(4), tag(6)]


def test_sweep_reaps_at_most_a_cap_of_runs_per_device() -> None:
    count = leftovers.REAPS_PER_SWEEP + 3
    device = Device(
        Called(0, listed(*(("done1", n, 1) for n in range(count))), ""),
        {tag(n): Called(0, f"{remote.REAP_MARK} 0 -\n", "") for n in range(count)},
    )
    lines = leftovers.sweep(device, BASE, STATES.get, lambda: frozenset())
    assert lines == ["deferred 3 run(s) on pc to the next sweep"]
    assert device.reaped == [tag(n) for n in range(leftovers.REAPS_PER_SWEEP)]


def test_an_unreachable_device_ends_its_pass() -> None:
    device = Device(
        Called(0, listed(("done1", 1, 1), ("done2", 2, 1)), ""),
        {tag(1): Called(255, "", "no route"), tag(2): Called(0, f"{remote.REAP_MARK} 0 -\n", "")},
    )
    lines = leftovers.sweep(device, BASE, STATES.get, lambda: frozenset())
    assert lines == [f"could not sweep run {tag(1)} of job done1 on pc (unreachable over ssh: no route)"]
    assert device.reaped == [tag(1)]


def test_a_failed_listing_is_one_line() -> None:
    lines = leftovers.sweep(Device(Called(255, "", "")), BASE, STATES.get, lambda: frozenset())
    assert lines == ["could not list runs on pc (unreachable over ssh)"]
