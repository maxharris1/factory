"""The suite's temp files go to RAM (/dev/shm) when it qualifies (#225)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path

import pytest

from tests import shm_temp

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parent.parent
Usage = namedtuple("Usage", "total used free")
GIB = 1 << 30
PROBE_LINE = re.compile(r"(?:^|\s)(?P<key>TMP_PATH|GETTEMPDIR|TMPDIR|TEMP|TMP)=(?P<value>\S*)$")


def usage(free: int):
    return lambda _path: Usage(free * 2, free, free)


PROBE = """
import os, tempfile

def test_probe(tmp_path):
    print("TMP_PATH=" + str(tmp_path))
    print("GETTEMPDIR=" + tempfile.gettempdir())
    for name in ("TMPDIR", "TEMP", "TMP"):
        print(name + "=" + os.environ.get(name, ""))
"""


def probe_values(lines) -> dict[str, str]:
    found = (PROBE_LINE.search(line) for line in lines)
    return {match["key"]: match["value"] for match in found if match}


@pytest.fixture
def fake_shm(tmp_path, monkeypatch):
    root = tmp_path / "shm"
    root.mkdir()
    monkeypatch.setattr(shm_temp, "SHM_ROOT", root)
    monkeypatch.setattr(shm_temp, "disk_usage", usage(4 * GIB))
    return root


def run_probe(pytester, *args):
    pytester.makepyfile(test_probe=PROBE)
    debug_root = os.environ.pop("PYTEST_DEBUG_TEMPROOT", None)
    try:
        result = pytester.runpytest_inprocess(
            "-p", "tests.shm_temp", "-s", "-W", "ignore::pytest.PytestAssertRewriteWarning", *args
        )
    finally:
        if debug_root is not None:
            os.environ["PYTEST_DEBUG_TEMPROOT"] = debug_root
    result.assert_outcomes(passed=1)
    return probe_values(result.outlines)


def test_a_large_enough_root_gets_a_unique_dir_per_session(tmp_path):
    first = shm_temp.choose_session_root(tmp_path, GIB, usage(2 * GIB))
    second = shm_temp.choose_session_root(tmp_path, GIB, usage(2 * GIB))
    assert first is not None and second is not None
    assert first != second
    assert first.parent == tmp_path and second.parent == tmp_path
    assert first.name.startswith(f"pytest-factory-{os.getpid()}-")


def test_exactly_the_threshold_is_enough(tmp_path):
    assert shm_temp.choose_session_root(tmp_path, GIB, usage(GIB)) is not None


def test_a_missing_root_falls_back(tmp_path):
    assert shm_temp.choose_session_root(tmp_path / "absent", GIB, usage(4 * GIB)) is None


def test_a_root_below_the_threshold_falls_back(tmp_path):
    assert shm_temp.choose_session_root(tmp_path, GIB, usage(GIB - 1)) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a 0o500 directory")
def test_an_unwritable_root_falls_back(tmp_path):
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(0o500)
    try:
        assert shm_temp.choose_session_root(root, GIB, usage(4 * GIB)) is None
    finally:
        root.chmod(0o700)


def test_a_mkdtemp_error_falls_back(tmp_path, monkeypatch):
    def refuse(**_kwargs):
        raise OSError("no space")

    monkeypatch.setattr(shm_temp.tempfile, "mkdtemp", refuse)
    assert shm_temp.choose_session_root(tmp_path, GIB, usage(4 * GIB)) is None


def test_a_session_puts_basetemp_and_process_temp_in_the_root(pytester, fake_shm):
    values = run_probe(pytester)
    sessions = [values["TMP_PATH"], values["GETTEMPDIR"], values["TMPDIR"]]
    assert all(value.startswith(str(fake_shm) + os.sep) for value in sessions)
    session_root = Path(values["TMPDIR"]).parent
    assert Path(values["TMP_PATH"]).is_relative_to(session_root / "basetemp")
    assert values["TEMP"] == values["TMP"] == values["TMPDIR"]
    assert list(fake_shm.iterdir()) == []


def test_a_session_restores_the_environment(pytester, fake_shm, monkeypatch):
    monkeypatch.setenv("TMPDIR", "/original")
    monkeypatch.delenv("TEMP", raising=False)
    run_probe(pytester)
    assert os.environ["TMPDIR"] == "/original"
    assert "TEMP" not in os.environ
    assert tempfile.tempdir is None


def test_a_session_leaves_another_live_sessions_dir_alone(pytester, fake_shm):
    other = shm_temp.choose_session_root(fake_shm, GIB, usage(4 * GIB))
    (other / "basetemp").mkdir()
    (other / "basetemp" / "keep.txt").write_text("in use", encoding="utf-8")
    values = run_probe(pytester)
    assert Path(values["TMPDIR"]).parent != other
    assert (other / "basetemp" / "keep.txt").read_text(encoding="utf-8") == "in use"


@pytest.mark.parametrize("case", ["missing", "small"])
def test_a_session_without_a_usable_root_keeps_the_normal_temp_dir(
    pytester, fake_shm, monkeypatch, case
):
    if case == "missing":
        monkeypatch.setattr(shm_temp, "SHM_ROOT", fake_shm / "absent")
    else:
        monkeypatch.setattr(shm_temp, "disk_usage", usage(GIB - 1))
    before = os.environ.get("TMPDIR", "")
    values = run_probe(pytester)
    assert values["TMPDIR"] == before
    assert not values["TMP_PATH"].startswith(str(fake_shm))


def test_an_explicit_basetemp_wins(pytester, fake_shm, tmp_path):
    chosen = tmp_path / "chosen"
    values = run_probe(pytester, f"--basetemp={chosen}")
    assert Path(values["TMP_PATH"]).is_relative_to(chosen)
    assert values["TMPDIR"].startswith(str(fake_shm) + os.sep)


def test_the_sweep_removes_only_dead_and_old_session_dirs(tmp_path):
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True).stdout.strip()
    old = time.time() - 2 * shm_temp.STALE_SECONDS
    stale = tmp_path / f"pytest-factory-{dead}-stale"
    fresh_dead = tmp_path / f"pytest-factory-{dead}-fresh"
    live = tmp_path / f"pytest-factory-{os.getpid()}-live"
    foreign = tmp_path / "something-else"
    for path in (stale, fresh_dead, live, foreign):
        (path / "tmp").mkdir(parents=True)
    for path in (stale, live, foreign):
        os.utime(path, (old, old))

    shm_temp.sweep_stale(tmp_path)

    assert not stale.exists()
    assert fresh_dead.exists() and live.exists() and foreign.exists()


def run_real_pytest(tmp_path, *args):
    test_file = tmp_path / "test_probe.py"
    test_file.write_text(PROBE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in {"PYTEST_ADDOPTS", "PYTEST_DEBUG_TEMPROOT"}}
    env["PYTHONPATH"] = str(REPO)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.shm_temp", "-s", "-q",
         "-p", "no:cacheprovider", str(test_file), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return probe_values(result.stdout.splitlines())


def real_shm_usable() -> bool:
    return shm_temp.qualifies(shm_temp.SHM_ROOT, shm_temp.MIN_FREE, shm_temp.disk_usage)


@pytest.mark.skipif(not real_shm_usable(), reason="no usable /dev/shm on this machine")
def test_a_real_run_uses_dev_shm(tmp_path):
    values = run_real_pytest(tmp_path)
    for key in ("TMP_PATH", "GETTEMPDIR", "TMPDIR"):
        assert values[key].startswith("/dev/shm/pytest-factory-"), values
    assert not Path(values["TMPDIR"]).parent.exists()


def test_a_real_run_honours_basetemp(tmp_path):
    chosen = tmp_path / "bt"
    values = run_real_pytest(tmp_path, f"--basetemp={chosen}")
    assert Path(values["TMP_PATH"]).is_relative_to(chosen)


def test_pytest_ini_loads_the_plugin():
    addopts = [line for line in (REPO / "pytest.ini").read_text(encoding="utf-8").splitlines()
               if line.startswith("addopts")]
    assert addopts and "-p tests.shm_temp" in addopts[0]
