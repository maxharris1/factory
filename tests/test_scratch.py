from __future__ import annotations

import os
from pathlib import Path

import pytest

from lib.scratch import remove


def test_remove_clears_directories_left_without_permissions(tmp_path: Path) -> None:
    scratch = tmp_path / "tmp"
    (scratch / "a" / "b").mkdir(parents=True)
    (scratch / "a" / "b" / "x").write_text("scratch")
    (scratch / "a" / "b").chmod(0)
    (scratch / "a").chmod(0)
    scratch.chmod(0)

    assert remove(scratch) is None
    assert not scratch.exists()


def test_remove_of_a_missing_scratch_is_quiet(tmp_path: Path) -> None:
    assert remove(tmp_path / "tmp") is None


def test_remove_leaves_a_symlinked_directory_target_alone(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o500)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    (scratch / "link").symlink_to(outside)

    assert remove(scratch) is None
    assert not scratch.exists()
    assert outside.stat().st_mode & 0o777 == 0o500


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes from a read-only directory")
def test_remove_reports_a_scratch_it_cannot_remove(tmp_path: Path) -> None:
    parent = tmp_path / "job"
    scratch = parent / "tmp"
    scratch.mkdir(parents=True)
    parent.chmod(0o500)
    try:
        failure = remove(scratch)
    finally:
        parent.chmod(0o700)

    assert failure is not None and failure.startswith(f"could not remove {scratch}: ")
    assert scratch.exists()
