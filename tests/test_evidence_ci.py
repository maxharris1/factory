"""Evidence bundle: the failed CI checks and the failed log's tail (#163)."""

from __future__ import annotations

from lib.evidence.ci import LOG_LINES, CIEvidence, ci_evidence

RUN_URL = "https://github.com/o/r/actions/runs/42"


def _log(lines: int) -> str:
    return "\n".join(f"line {n}" for n in range(1, lines + 1)) + "\n"


def test_ci_fix_fixture_carries_run_url_and_log_tail():
    fetched = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return "setup\nFAILED tests/test_x.py::test_y - AssertionError\n1 failed\n"

    bundle = ci_evidence(failed_checks=("pytest", "check-config"), run_url=RUN_URL, fetch_log=fetch)

    assert fetched == [RUN_URL]
    assert bundle == CIEvidence(
        failed_checks=("pytest", "check-config"),
        run_url=RUN_URL,
        log_tail="setup\nFAILED tests/test_x.py::test_y - AssertionError\n1 failed",
        cut=None,
    )


def test_cap_keeps_last_lines_and_says_what_it_cut():
    bundle = ci_evidence(failed_checks=("pytest",), run_url=RUN_URL, fetch_log=lambda _: _log(400))

    assert LOG_LINES == 150
    assert bundle.cut == "[cut 250 earlier lines]"
    lines = bundle.log_tail.splitlines()
    assert lines[0] == bundle.cut
    assert lines[1] == "line 251"
    assert lines[-1] == "line 400"
    assert len(lines) == LOG_LINES + 1


def test_log_at_the_cap_is_not_cut():
    bundle = ci_evidence(failed_checks=("pytest",), run_url=RUN_URL, fetch_log=lambda _: _log(150))

    assert bundle.cut is None
    assert bundle.log_tail.splitlines() == [f"line {n}" for n in range(1, 151)]


def test_limit_can_be_lowered():
    bundle = ci_evidence(failed_checks=("pytest",), run_url=RUN_URL, fetch_log=lambda _: _log(5), limit=2)

    assert bundle.log_tail == "[cut 3 earlier lines]\nline 4\nline 5"


def test_trailing_blank_lines_do_not_use_the_cap():
    bundle = ci_evidence(
        failed_checks=("pytest",), run_url=RUN_URL, fetch_log=lambda _: "a\nb\n\n\n", limit=2
    )

    assert bundle.log_tail == "a\nb"
    assert bundle.cut is None


def test_without_a_run_url_nothing_is_fetched():
    def fetch(url: str) -> str:
        raise AssertionError("fetched")

    bundle = ci_evidence(failed_checks=("commit status",), run_url=None, fetch_log=fetch)

    assert bundle == CIEvidence(failed_checks=("commit status",), run_url=None, log_tail="", cut=None)
