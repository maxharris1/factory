from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parent.parent
ISSUE = "https://github.com/acme/widgets/issues/7"
OTHER_ISSUE = "https://github.com/acme/widgets/issues/8"

FLAKES = """
import time
import pytest

def test_fails():
    assert False

def test_passes():
    pass

def test_unlisted_fails():
    assert False

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown flake")

def test_teardown_fails(broken_teardown):
    pass

def test_times_out():
    time.sleep(5)

@pytest.mark.parametrize("n", [1, 2])
def test_param(n):
    assert n == 1
"""


def quarantine(pytester, *lines: str) -> Path:
    path = pytester.path / "quarantine.txt"
    path.write_text("# flaky\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(pytester, path: Path, *args: str):
    pytester.makepyfile(test_flakes=FLAKES)
    return pytester.runpytest_inprocess("-p", "tests.quarantine", "--quarantine-file", str(path), *args)


def test_a_quarantined_failure_and_pass_report_without_failing_the_run(pytester):
    path = quarantine(pytester, f"test_flakes.py::test_fails {ISSUE}", f"test_flakes.py::test_passes {OTHER_ISSUE}")
    result = run(pytester, path, "-k", "test_fails or test_passes")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(xfailed=1, xpassed=1)
    result.stdout.fnmatch_lines([
        "*= quarantined =*",
        f"FAILED test_flakes.py::test_fails {ISSUE}",
        f"PASSED test_flakes.py::test_passes {OTHER_ISSUE}",
    ])


def test_an_unlisted_failure_still_fails_the_run(pytester):
    path = quarantine(pytester, f"test_flakes.py::test_fails {ISSUE}")
    result = run(pytester, path, "-k", "test_fails or test_unlisted_fails")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes(failed=1, xfailed=1)


def test_a_quarantined_teardown_error_or_timeout_counts_as_a_quarantined_failure(pytester):
    path = quarantine(
        pytester,
        f"test_flakes.py::test_teardown_fails {ISSUE}",
        f"test_flakes.py::test_times_out {ISSUE}",
    )
    result = run(pytester, path, "-k", "teardown_fails or times_out", "--timeout", "1",
                 "--timeout-method", "signal")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines([
        f"FAILED test_flakes.py::test_teardown_fails {ISSUE}",
        f"FAILED test_flakes.py::test_times_out {ISSUE}",
    ])


def test_a_base_node_id_quarantines_every_parametrization(pytester):
    path = quarantine(pytester, f"test_flakes.py::test_param {ISSUE}")
    result = run(pytester, path, "-k", "test_param")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(xpassed=1, xfailed=1)
    result.stdout.fnmatch_lines([
        f"PASSED test_flakes.py::test_param[1] {ISSUE}",
        f"FAILED test_flakes.py::test_param[2] {ISSUE}",
    ])


def test_the_github_step_summary_gets_a_quarantine_table(pytester, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    path = quarantine(pytester, f"test_flakes.py::test_fails {ISSUE}", f"test_flakes.py::test_passes {OTHER_ISSUE}")
    run(pytester, path, "-k", "test_fails or test_passes")
    assert summary.read_text(encoding="utf-8") == (
        "earlier step\n"
        "### Quarantined tests\n\n"
        "| Test | Result | Issue |\n"
        "| --- | --- | --- |\n"
        f"| `test_flakes.py::test_fails` | failed | {ISSUE} |\n"
        f"| `test_flakes.py::test_passes` | passed | {OTHER_ISSUE} |\n\n"
    )


def test_a_run_with_no_quarantined_results_reports_nothing(pytester, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    path = quarantine(pytester, f"test_flakes.py::test_fails {ISSUE}")
    result = run(pytester, path, "-k", "test_passes")
    assert result.ret == pytest.ExitCode.OK
    assert "= quarantined =" not in result.stdout.str()
    assert not summary.exists()


@pytest.mark.parametrize("entry", [
    "test_flakes.py::test_fails",
    f"test_flakes.py::test_fails {ISSUE} extra",
    "test_flakes.py::test_fails https://github.com/acme/widgets/pull/7",
    "test_flakes.py::test_fails https://example.com/acme/widgets/issues/7",
    f"test_flakes.py::test_passes {ISSUE}",
], ids=["no-link", "extra-field", "pull-request", "not-github", "duplicate"])
def test_a_malformed_entry_fails_the_run_naming_its_line(pytester, entry):
    path = quarantine(pytester, f"test_flakes.py::test_passes {ISSUE}", entry)
    result = run(pytester, path)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"ERROR: {path}:4: *"])


def run_cli(*args: str):
    return subprocess.run([sys.executable, "-m", "tests.quarantine", *args],
                          cwd=REPO, capture_output=True, text=True, timeout=60)


def test_the_cli_prints_each_linked_issue_once(tmp_path):
    path = tmp_path / "quarantine.txt"
    path.write_text(f"a.py::test_a {ISSUE}\nb.py::test_b {OTHER_ISSUE}\nc.py::test_c {ISSUE}\n",
                    encoding="utf-8")
    result = run_cli(str(path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [ISSUE, OTHER_ISSUE]


def test_the_cli_fails_on_a_malformed_file(tmp_path):
    path = tmp_path / "quarantine.txt"
    path.write_text("a.py::test_a\n", encoding="utf-8")
    result = run_cli(str(path))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith(f"{path}:1: ")


def test_the_shipped_quarantine_file_parses():
    result = run_cli()
    assert result.returncode == 0, result.stderr


def test_the_suite_runs_with_the_plugin(request):
    assert request.config.pluginmanager.has_plugin("tests.quarantine")


def test_under_xdist_the_controller_lists_the_quarantined_failure(pytester, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(REPO))
    path = quarantine(pytester, f"test_flakes.py::test_fails {ISSUE}")
    pytester.makepyfile(test_flakes=FLAKES)
    result = pytester.runpytest_subprocess(
        "-p", "tests.quarantine", "--quarantine-file", str(path), "-n", "2", "-k", "test_fails or test_passes",
    )
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1, xfailed=1)
    result.stdout.fnmatch_lines(["*= quarantined =*", f"FAILED test_flakes.py::test_fails {ISSUE}"])
