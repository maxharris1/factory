from __future__ import annotations

from pathlib import Path

from tests.procs import alive
from tests.test_conftest_isolation import _run_guarded

IMPORT_LOOP = '''
import os
import subprocess

child = subprocess.Popen(["sleep", "60"], env={{"PATH": os.environ["PATH"]}}, start_new_session=True)
open({record!r}, "w").write(str(child.pid))
{stop}


def test_passes():
    pass
'''


def _import_loop(tmp_path: Path, stop: str):
    record = tmp_path / "pid"
    result = _run_guarded(tmp_path, IMPORT_LOOP.format(record=str(record), stop=stop))
    return result, int(record.read_text(encoding="utf-8"))


def test_a_module_that_leaves_a_loop_running_from_import_fails_the_run(tmp_path: Path) -> None:
    result, pid = _import_loop(tmp_path, "")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "test_leak.py left processes running" in result.stdout
    assert f"{pid} HOME= sleep 60" in result.stdout
    assert "1 passed" not in result.stdout
    assert not alive(pid)


def test_a_module_that_stops_its_import_time_child_passes(tmp_path: Path) -> None:
    result, pid = _import_loop(tmp_path, "child.kill()\nchild.wait()")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert not alive(pid)


def test_a_module_that_leaks_and_then_fails_to_import_keeps_both_errors(tmp_path: Path) -> None:
    result, pid = _import_loop(tmp_path, "import no_such_module_393")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "ModuleNotFoundError" in result.stdout
    assert "test_leak.py left processes running" in result.stdout
    assert not alive(pid)
