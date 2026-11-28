import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.harness_env import CPU_CAP, TEST_WORKERS, account_environ, cpu_cap_env, cpu_window, harness_env

ROOT = Path(__file__).resolve().parents[1]
CAPPED = {
    "ORCH_CPU_CAP": "4",
    "PYTEST_XDIST_AUTO_NUM_WORKERS": str(TEST_WORKERS),
    "VITEST_MAX_THREADS": str(TEST_WORKERS),
    "VITEST_MAX_FORKS": str(TEST_WORKERS),
    "VITEST_MIN_THREADS": "1",
    "VITEST_MIN_FORKS": "1",
}
VITEST = os.environ.get("TEST_VITEST_MJS")
NODE = shutil.which("node")


@pytest.mark.parametrize("harness", ["claude", "codex", "cursor"])
def test_every_harness_env_carries_the_cpu_cap(harness: str) -> None:
    assert harness_env(harness).items() >= CAPPED.items()


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_an_account_home_keeps_the_cpu_cap(harness: str) -> None:
    assert harness_env(harness, "/srv/accounts/a").items() >= CAPPED.items()


@pytest.mark.parametrize(
    ("harness", "home", "tokens"),
    [
        ("claude", "CLAUDE_CONFIG_DIR", ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")),
        ("codex", "CODEX_HOME", ("OPENAI_API_KEY", "CODEX_API_KEY")),
    ],
)
def test_an_account_environ_drops_its_tokens_and_names_its_home(
    harness: str, home: str, tokens: tuple[str, ...]
) -> None:
    base = {"PATH": "/usr/bin", home: "/login/home", **dict.fromkeys(tokens, "sk-test")}
    assert account_environ(base, harness, "/srv/accounts/a") == {"PATH": "/usr/bin", home: "/srv/accounts/a"}
    login = account_environ(base, harness, None)
    assert login == base and login is not base


def test_the_cap_is_four_cpus_in_code_and_runners_get_the_worker_count() -> None:
    assert CPU_CAP == 4
    assert cpu_cap_env() == CAPPED
    assert cpu_cap_env(2) == {
        "ORCH_CPU_CAP": "2",
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "1",
        "VITEST_MAX_THREADS": "1",
        "VITEST_MAX_FORKS": "1",
        "VITEST_MIN_THREADS": "1",
        "VITEST_MIN_FORKS": "1",
    }


def test_the_three_runner_maxima_are_the_one_worker_count() -> None:
    assert TEST_WORKERS == 2
    assert 1 <= TEST_WORKERS < CPU_CAP
    env = cpu_cap_env()
    for name in ("PYTEST_XDIST_AUTO_NUM_WORKERS", "VITEST_MAX_THREADS", "VITEST_MAX_FORKS"):
        assert env[name] == str(TEST_WORKERS), name


@pytest.mark.parametrize(("cap", "workers"), [(1, 1), (2, 1), (3, 2), (4, TEST_WORKERS), (64, TEST_WORKERS)])
def test_the_worker_count_never_passes_the_cap_less_one(cap: int, workers: int) -> None:
    env = cpu_cap_env(cap)
    for name in ("PYTEST_XDIST_AUTO_NUM_WORKERS", "VITEST_MAX_THREADS", "VITEST_MAX_FORKS"):
        assert env[name] == str(workers), name


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


@pytest.mark.parametrize("cap", [1, 2, 3, CPU_CAP, 64])
def test_every_vitest_pool_maximum_has_a_minimum_of_one_at_or_below_it(cap: int) -> None:
    env = cpu_cap_env(cap)
    for pool in ("THREADS", "FORKS"):
        assert 1 == int(env[f"VITEST_MIN_{pool}"]) <= int(env[f"VITEST_MAX_{pool}"])
    assert int(env["PYTEST_XDIST_AUTO_NUM_WORKERS"]) >= 1


@pytest.mark.skipif(
    not (VITEST and NODE), reason="set TEST_VITEST_MJS to an installed vitest 1 or later vitest.mjs to run it under the env cap"
)
@pytest.mark.parametrize("pool", ["threads", "forks", "vmThreads", "vmForks"])
@pytest.mark.parametrize("cap", [1, 2, CPU_CAP])
def test_vitest_runs_under_the_env_cap_without_affinity(tmp_path: Path, pool: str, cap: int) -> None:
    (tmp_path / "cap.test.js").write_text("test('ok', () => expect(2 + 2).toBe(4));\n")
    inherited = {k: v for k, v in os.environ.items() if not k.startswith(("VITEST", "ORCH_CPU_CAP"))}
    proc = subprocess.run(
        [NODE, VITEST, "run", "cap.test.js", "--root", str(tmp_path), "--globals", "--pool", pool],
        cwd=tmp_path,
        env={**inherited, **cpu_cap_env(cap)},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout
