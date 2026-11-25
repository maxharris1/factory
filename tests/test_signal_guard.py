"""A test's signal dispositions and mask never reach the next test (#1598)."""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

LEAKER_THEN_CHECKER = """
import signal

def test_1_leaves_sigterm_ignored_and_sigint_blocked():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})

def test_2_starts_with_the_dispositions_and_mask_the_session_had():
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL
    assert signal.SIGINT not in signal.pthread_sigmask(signal.SIG_BLOCK, [])
"""


def test_a_test_that_ignores_sigterm_does_not_leave_it_ignored_for_the_next(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(test_leak=LEAKER_THEN_CHECKER)
    result = pytester.runpytest_inprocess(
        "-p", "tests.signal_guard", "-W", "ignore::pytest.PytestAssertRewriteWarning",
    )
    result.assert_outcomes(passed=2)
