"""Split the suite into N stable slices so CI can run them as parallel jobs (#311).

Loaded from pytest.ini with `-p tests.shard`. `--shard K/N` keeps the tests
whose node id hashes (crc32, so every process agrees) to slice K of N and
deselects the rest. Every xdist worker deselects the same set, so their
collections still match. Without `--shard`, nothing changes.
"""

from __future__ import annotations

import argparse
import zlib

import pytest


def parse_shard(spec: str) -> tuple[int, int]:
    index, sep, total = spec.partition("/")
    try:
        k, n = int(index), int(total)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected K/N, got {spec!r}") from None
    if not sep or not 1 <= k <= n:
        raise argparse.ArgumentTypeError(f"expected K/N with 1 <= K <= N, got {spec!r}")
    return k, n


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        type=parse_shard,
        default=None,
        metavar="K/N",
        help="run only slice K of N of the collected tests",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.getoption("shard")
    if shard is None:
        return
    k, n = shard
    kept, dropped = [], []
    for item in items:
        (kept if zlib.crc32(item.nodeid.encode()) % n == k - 1 else dropped).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
