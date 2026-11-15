from __future__ import annotations

from pathlib import Path

import pytest

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


@pytest.mark.parametrize("stop", ["raise KeyboardInterrupt('collection interrupted')", "raise SystemExit(0)"])
def test_a_module_that_leaks_and_then_interrupts_collection_names_the_leak(tmp_path: Path, stop: str) -> None:
    result, pid = _import_loop(tmp_path, stop)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "test_leak.py left processes running" in result.stdout + result.stderr
    assert not alive(pid)


HOOK_LOOP = '''
import os
import subprocess

pytest_plugins = [__name__]


def {hook}(session):
    child = subprocess.Popen(["sleep", "60"], env={{"PATH": os.environ["PATH"]}}, start_new_session=True)
    open({record!r}, "w").write(str(child.pid))


def test_passes():
    pass
'''


@pytest.mark.parametrize("hook", ["pytest_collection_modifyitems", "pytest_collection_finish"])
@pytest.mark.parametrize("extra", [[], ["--collect-only"]])
def test_a_module_whose_collection_hook_leaves_a_loop_running_fails_the_run(
    tmp_path: Path, hook: str, extra: list[str]
) -> None:
    record = tmp_path / "pid"
    result = _run_guarded(tmp_path, HOOK_LOOP.format(hook=hook, record=str(record)), *extra)
    pid = int(record.read_text(encoding="utf-8"))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "test_leak.py" in result.stdout and "left processes running" in result.stdout
    assert f"{pid} HOME= sleep 60" in result.stdout
    assert "1 passed" not in result.stdout
    assert not alive(pid)


EXIT_HOOK = HOOK_LOOP.replace(
    "str(child.pid))\n", "str(child.pid))\n    import pytest\n    pytest.exit('collection ended', returncode=0)\n"
)


@pytest.mark.parametrize("extra", [[], ["--collect-only"]])
def test_a_collection_hook_that_leaks_and_then_exits_zero_fails_the_run(tmp_path: Path, extra: list[str]) -> None:
    record = tmp_path / "pid"
    result = _run_guarded(tmp_path, EXIT_HOOK.format(hook="pytest_collection_finish", record=str(record)), *extra)
    pid = int(record.read_text(encoding="utf-8"))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "test_leak.py" in result.stdout and "left processes running" in result.stdout
    assert not alive(pid)


OBJECT_HOOK = '''
import os
import subprocess


class Hooks:
    def pytest_collection_finish(self, session):
        child = subprocess.Popen(["sleep", "60"], env={{"PATH": os.environ["PATH"]}}, start_new_session=True)
        open({record!r}, "w").write(str(child.pid))


def pytest_generate_tests(metafunc):
    metafunc.config.pluginmanager.register(Hooks(), name="class-collection-hook")


def test_passes():
    pass
'''


def test_a_hook_object_a_module_registers_is_named_by_that_module(tmp_path: Path) -> None:
    record = tmp_path / "pid"
    result = _run_guarded(tmp_path, OBJECT_HOOK.format(record=str(record)))
    pid = int(record.read_text(encoding="utf-8"))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "collection hooks in test_leak.py left processes running" in result.stdout
    assert not alive(pid)


def test_a_module_that_leaks_at_import_and_exits_zero_at_session_finish_fails_the_run(tmp_path: Path) -> None:
    exit_zero = "import pytest\npytest_plugins = [__name__]\n\n\ndef pytest_sessionfinish(session):\n    pytest.exit('done', returncode=0)"
    result, pid = _import_loop(tmp_path, exit_zero)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "test_leak.py left processes running" in result.stdout
    assert not alive(pid)


UNREGISTERING_HOOK = OBJECT_HOOK.replace(
    "str(child.pid))\n", "str(child.pid))\n        session.config.pluginmanager.unregister(self)\n"
)


def test_a_hook_object_that_unregisters_itself_is_named_by_its_module(tmp_path: Path) -> None:
    record = tmp_path / "pid"
    result = _run_guarded(tmp_path, UNREGISTERING_HOOK.format(record=str(record)))
    pid = int(record.read_text(encoding="utf-8"))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "collection hooks in test_leak.py left processes running" in result.stdout
    assert not alive(pid)
