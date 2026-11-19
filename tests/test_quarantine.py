from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.quarantine import DEFAULT_FILE, parse

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parent.parent
ISSUE = "https://github.com/acme/widgets/issues/7"
OTHER_ISSUE = "https://github.com/acme/widgets/issues/8"
CLOSED_ISSUE = "https://github.com/acme/widgets/issues/9"
OPEN_PR = "https://github.com/acme/widgets/issues/10"
UNKNOWN_ISSUE = "https://github.com/acme/widgets/issues/11"

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

@pytest.mark.xfail(strict=True)
def test_strict_xfail_passes():
    pass

@pytest.mark.parametrize("s", ["go ", "go"])
def test_spaced(s):
    assert s == "go"
"""


@pytest.fixture(autouse=True)
def no_step_summary(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


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


def test_a_quarantined_pass_overrides_the_tests_own_strict_xfail(pytester):
    path = quarantine(pytester, f"test_flakes.py::test_strict_xfail_passes {ISSUE}")
    result = run(pytester, path, "-k", "test_strict_xfail_passes")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(xpassed=1)


def test_a_base_node_id_quarantines_every_parametrization(pytester):
    path = quarantine(pytester, f"test_flakes.py::test_param {ISSUE}")
    result = run(pytester, path, "-k", "test_param")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(xpassed=1, xfailed=1)
    result.stdout.fnmatch_lines([
        f"PASSED test_flakes.py::test_param[1] {ISSUE}",
        f"FAILED test_flakes.py::test_param[2] {ISSUE}",
    ])


def test_a_node_id_with_a_space_in_its_param_id_is_quarantined(pytester):
    path = quarantine(pytester, f"test_flakes.py::test_spaced[go ] {ISSUE}")
    result = run(pytester, path, "-k", "test_spaced")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1, xfailed=1)
    result.stdout.fnmatch_lines(["*= quarantined =*", f"FAILED test_flakes.py::test_spaced[go ] {ISSUE}"])


def test_the_suite_leaves_the_real_step_summary_alone(tmp_path):
    summary = tmp_path / "summary.md"
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}
    env.pop("ORCH_DB", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:xdist", "tests/test_quarantine.py",
         "-k", "not test_the_suite_leaves_the_real_step_summary_alone"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not summary.exists() or summary.read_text(encoding="utf-8") == ""


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


FAKE_GH = """\
import json, sys
with open({log!r}, "a", encoding="utf-8") as log:
    log.write(sys.argv[2] + "\\n")
issues = {{
    "repos/acme/widgets/issues/7": {{"state": "open"}},
    "repos/acme/widgets/issues/8": {{"state": "open"}},
    "repos/acme/widgets/issues/9": {{"state": "closed"}},
    "repos/acme/widgets/issues/10": {{"state": "open", "pull_request": {{}}}},
}}
if sys.argv[1] != "api" or sys.argv[2] not in issues:
    sys.exit("gh: Not Found (HTTP 404)")
print(json.dumps(issues[sys.argv[2]]))
"""


@pytest.fixture
def gh(tmp_path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    log.touch()
    script = bin_dir / "gh"
    script.write_text(f"#!{sys.executable}\n" + FAKE_GH.format(log=str(log)), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def run_cli(*args: str):
    return subprocess.run([sys.executable, "-m", "tests.quarantine", *args],
                          cwd=REPO, capture_output=True, text=True, timeout=60)


def write_quarantine(tmp_path, *urls: str) -> Path:
    path = tmp_path / "quarantine.txt"
    path.write_text("".join(f"t.py::test_{i} {url}\n" for i, url in enumerate(urls)), encoding="utf-8")
    return path


def error_line(url: str, problem: str) -> str:
    return f"::error title=quarantine::{url} is {problem}; fix the flake or drop its tests/quarantine.txt entry"


def test_the_cli_passes_when_every_link_is_an_open_issue(tmp_path, gh):
    result = run_cli(str(write_quarantine(tmp_path, ISSUE, OTHER_ISSUE)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert gh.read_text().splitlines() == ["repos/acme/widgets/issues/7", "repos/acme/widgets/issues/8"]


@pytest.mark.parametrize("url, problem", [
    (OPEN_PR, "a pull request"),
    (CLOSED_ISSUE, "closed"),
    (UNKNOWN_ISSUE, "unreadable"),
], ids=["pull-request", "closed", "gh-fails"])
def test_the_cli_fails_on_a_link_that_is_not_an_open_issue(tmp_path, gh, url, problem):
    result = run_cli(str(write_quarantine(tmp_path, ISSUE, url)))
    assert result.returncode == 1
    assert result.stdout.splitlines() == [error_line(url, problem)]


def test_the_cli_queries_each_linked_issue_once(tmp_path, gh):
    result = run_cli(str(write_quarantine(tmp_path, ISSUE, CLOSED_ISSUE, ISSUE, CLOSED_ISSUE)))
    assert result.returncode == 1
    assert result.stdout.splitlines() == [error_line(CLOSED_ISSUE, "closed")]
    assert gh.read_text().splitlines() == ["repos/acme/widgets/issues/7", "repos/acme/widgets/issues/9"]


def test_the_cli_fails_on_a_malformed_file(tmp_path):
    path = tmp_path / "quarantine.txt"
    path.write_text("a.py::test_a\n", encoding="utf-8")
    result = run_cli(str(path))
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith(f"{path}:1: ")


def test_the_shipped_quarantine_file_parses():
    parse(DEFAULT_FILE)


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
