"""#222: the suite's autouse SSH control-dir fixture tears down after the
test's own monkeypatches are undone, so a test that patches os or shutil
cannot break that teardown."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATCHING_TEST = '''
import os
import shutil

PATCHED = ("lstat", "stat", "open", "scandir", "rmdir", "unlink", "remove", "rmtree")


def refuse(*_args, **_kwargs):
    raise AssertionError("a test monkeypatch leaked into the conftest teardown")


def test_patches_the_filesystem(monkeypatch):
    for name in PATCHED:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse)
    monkeypatch.setattr(shutil, "rmtree", refuse)
    monkeypatch.setenv("ORCH_SSH_CONTROL_DIR", "/nonexistent/test-owned")


def test_the_control_dir_is_back_and_gone_after(tmp_path):
    import os as real_os
    seen = real_os.environ["ORCH_SSH_CONTROL_DIR"]
    assert seen != "/nonexistent/test-owned"
    assert real_os.path.isdir(seen)
'''


def test_a_test_that_patches_os_and_shutil_does_not_break_conftest_teardown(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    shutil.copy(ROOT / "tests" / "conftest.py", tmp_path / "tests" / "conftest.py")
    (tmp_path / "tests" / "test_patching.py").write_text(PATCHING_TEST, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_patching.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert "error" not in result.stdout.lower()


def test_the_control_dir_is_removed_after_each_test(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    shutil.copy(ROOT / "tests" / "conftest.py", tmp_path / "tests" / "conftest.py")
    record = tmp_path / "dirs.txt"
    (tmp_path / "tests" / "test_record.py").write_text(
        "import os\n"
        f"def test_one():\n    open({str(record)!r}, 'a').write(os.environ['ORCH_SSH_CONTROL_DIR'] + '\\n')\n"
        f"def test_two():\n    open({str(record)!r}, 'a').write(os.environ['ORCH_SSH_CONTROL_DIR'] + '\\n')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_record.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    first, second = record.read_text(encoding="utf-8").split()
    assert first != second
    assert not Path(first).exists()
    assert not Path(second).exists()
