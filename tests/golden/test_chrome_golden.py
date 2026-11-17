from __future__ import annotations

import importlib
from pathlib import Path
from unittest import mock

import pytest

from lib.dashboard_pages import shell
from tests.golden.pages import PAGE_FILES, render, sha, snapshots, split

CHROME = ("_topbar", "_band", "_attention_strip", "_statusline")


def moved_files(root: Path, name: str) -> set[str]:
    cases = snapshots(root)
    real = getattr(shell, name)
    moved = set()
    with mock.patch.object(shell, name, lambda *args: real(*args) + "<i>changed</i>"):
        for case, snapshot in cases.items():
            for page, html in render(snapshot).items():
                stem = PAGE_FILES[page]
                golden = importlib.import_module(f"tests.golden.{stem}").GOLDEN
                if sha(split(html)["chrome"]) != golden[f"{case}/chrome"]:
                    moved.add(stem)
    return moved


@pytest.mark.parametrize("name", CHROME)
def test_a_shell_chrome_change_moves_every_page_golden_file(tmp_path: Path, name: str) -> None:
    assert moved_files(tmp_path, name) == set(PAGE_FILES.values())
