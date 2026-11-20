from pathlib import Path

import pytest

from lib.harness_env import CPU_CAP, cpu_cap_env, cpu_window, harness_env

ROOT = Path(__file__).resolve().parents[1]
CAPPED = {
    "ORCH_CPU_CAP": "4",
    "PYTEST_XDIST_AUTO_NUM_WORKERS": "3",
    "VITEST_MAX_THREADS": "3",
    "VITEST_MAX_FORKS": "3",
}


@pytest.mark.parametrize("harness", ["claude", "codex", "cursor"])
def test_every_harness_env_carries_the_cpu_cap(harness: str) -> None:
    assert harness_env(harness).items() >= CAPPED.items()


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_an_account_home_keeps_the_cpu_cap(harness: str) -> None:
    assert harness_env(harness, "/srv/accounts/a").items() >= CAPPED.items()


def test_the_cap_is_four_cpus_in_code_and_runners_get_one_less() -> None:
    assert CPU_CAP == 4
    assert cpu_cap_env() == CAPPED
    assert cpu_cap_env(2) == {
        "ORCH_CPU_CAP": "2",
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "1",
        "VITEST_MAX_THREADS": "1",
        "VITEST_MAX_FORKS": "1",
    }


def test_the_cap_is_not_a_machines_yml_key() -> None:
    for path in ("machines.yml", "lib/machines_yml.py"):
        assert "cpu" not in (ROOT / path).read_text(encoding="utf-8").lower(), path


def test_no_window_when_the_host_has_at_most_the_cap() -> None:
    assert cpu_window(range(4), 4, "00000000") is None
    assert cpu_window({0, 2}, 4, "ffffffff") is None


def test_the_window_is_cap_consecutive_allowed_cpus_from_the_tag() -> None:
    allowed = [9, 1, 3, 5, 7, 11]
    assert cpu_window(allowed, 4, "00000000") == frozenset({1, 3, 5, 7})
    assert cpu_window(allowed, 4, "00000002") == frozenset({5, 7, 9, 11})
    assert cpu_window(allowed, 4, "00000004") == frozenset({9, 11, 1, 3})
    assert cpu_window(allowed, 1, "0000000b") == frozenset({11})


def test_tags_spread_windows_across_the_host() -> None:
    windows = [cpu_window(range(24), 4, f"{n:08x}") for n in range(0, 24, 4)]
    assert set().union(*windows) == set(range(24))
