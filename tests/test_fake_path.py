"""link_path_without builds a PATH dir from several bin dirs that may repeat a name."""

from __future__ import annotations

import os
from pathlib import Path

from tests.fake_path import link_path_without


def test_a_dangling_symlink_repeated_across_bin_dirs_links_once(tmp_path: Path) -> None:
    first, second = tmp_path / "usr-bin", tmp_path / "bin"
    for directory in (first, second):
        directory.mkdir()
        (directory / "grub-ntldr-img").symlink_to(tmp_path / "gone")
    (first / "setsid").write_text("")
    (second / "sh").write_text("")

    bare = link_path_without(tmp_path / "no-setsid", [first, second], {"setsid"})

    assert sorted(os.listdir(bare)) == ["grub-ntldr-img", "sh"]
    assert os.readlink(bare / "grub-ntldr-img") == str(first / "grub-ntldr-img")


def test_missing_bin_dirs_are_skipped(tmp_path: Path) -> None:
    present = tmp_path / "bin"
    present.mkdir()
    (present / "sh").write_text("")

    bare = link_path_without(tmp_path / "out", [tmp_path / "absent", present], set())

    assert os.listdir(bare) == ["sh"]
