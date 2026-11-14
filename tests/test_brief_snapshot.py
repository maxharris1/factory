"""Every job kind renders byte-identically to its snapshot (#166)."""

from __future__ import annotations

import pytest

from tests.regenerate_brief_snapshots import CASES, SNAPSHOTS, render_snapshot


def test_one_snapshot_per_kind_and_no_strays() -> None:
    assert {path.stem for path in SNAPSHOTS.glob("*.txt")} == set(CASES)


@pytest.mark.parametrize("kind", sorted(CASES))
def test_brief_matches_snapshot(kind: str) -> None:
    expected = (SNAPSHOTS / f"{kind}.txt").read_bytes()
    assert render_snapshot(kind).encode("utf-8") == expected
