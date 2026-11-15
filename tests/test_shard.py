import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _collect(*args: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "ORCH_DB"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests/test_wait.py", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _node_ids(result: subprocess.CompletedProcess) -> set[str]:
    assert result.returncode == 0, result.stdout + result.stderr
    return {line for line in result.stdout.splitlines() if "::" in line}


def test_three_shards_split_the_collection_without_gaps_or_overlap() -> None:
    everything = _node_ids(_collect())
    shards = [_node_ids(_collect("--shard", f"{k}/3")) for k in (1, 2, 3)]

    assert everything
    assert all(shards), [len(shard) for shard in shards]
    for i, left in enumerate(shards):
        for right in shards[i + 1 :]:
            assert not left & right
    assert set().union(*shards) == everything


@pytest.mark.parametrize("spec", ["4/3", "x"])
def test_a_bad_shard_spec_is_a_usage_error(spec: str) -> None:
    result = _collect("--shard", spec)

    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "::" not in result.stdout
