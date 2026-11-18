from __future__ import annotations

import pytest

from lib.dispatch.types import CIResult, FailedCheck, result_cause
from lib.evidence.ci import (
    LOG_LINES,
    CIEvidence,
    CIJobLog,
    ci_evidence,
    classify_failure,
    classify_logged,
    failing_tests,
)

RUN_URL = "https://github.com/o/r/actions/runs/42"
PYTEST = FailedCheck("pytest", run_id="42", job_id="7", run_url=RUN_URL, cause="code")


def _log(lines: int) -> str:
    return "\n".join(f"line {n}" for n in range(1, lines + 1)) + "\n"


def _tail(log: str, limit: int = LOG_LINES) -> CIJobLog:
    return ci_evidence(failed=(PYTEST,), logs={"7": log}, limit=limit).jobs[0]


def test_a_fetched_log_reaches_the_bundle_with_its_run():
    bundle = ci_evidence(
        failed=(PYTEST, FailedCheck("commit status")),
        logs={"7": "setup\nFAILED tests/test_x.py::test_y - AssertionError\n1 failed\n"},
    )

    assert bundle == CIEvidence(
        failed_checks=("pytest", "commit status"),
        runs=(("42", RUN_URL),),
        jobs=(
            CIJobLog(
                "pytest",
                "42",
                RUN_URL,
                "setup\nFAILED tests/test_x.py::test_y - AssertionError\n1 failed",
                None,
                "code",
                None,
            ),
            CIJobLog("commit status", None, None, None, None, "unknown", None),
        ),
        cause="code",
    )


def test_two_jobs_of_one_run_list_the_run_once():
    other = FailedCheck("lint", run_id="42", job_id="8", run_url=RUN_URL)

    bundle = ci_evidence(failed=(PYTEST, other), logs={"7": "a", "8": None})

    assert bundle.runs == (("42", RUN_URL),)
    assert [job.log_tail for job in bundle.jobs] == ["a", None]


def test_a_job_whose_log_was_not_fetched_is_unavailable():
    bundle = ci_evidence(failed=(PYTEST,), logs={})

    assert bundle.jobs == (CIJobLog("pytest", "42", RUN_URL, None, None, "code", None),)


def test_cap_keeps_last_lines_and_says_what_it_cut():
    job = _tail(_log(400))

    assert LOG_LINES == 150
    assert job.cut == "[cut 250 earlier lines]"
    lines = (job.log_tail or "").splitlines()
    assert lines[0] == job.cut
    assert lines[1] == "line 251"
    assert lines[-1] == "line 400"
    assert len(lines) == LOG_LINES + 1


def test_log_at_the_cap_is_not_cut():
    job = _tail(_log(150))

    assert job.cut is None
    assert (job.log_tail or "").splitlines() == [f"line {n}" for n in range(1, 151)]


def test_limit_can_be_lowered():
    assert _tail(_log(5), limit=2).log_tail == "[cut 3 earlier lines]\nline 4\nline 5"


def test_trailing_blank_lines_do_not_use_the_cap():
    job = _tail("a\nb\n\n\n", limit=2)

    assert (job.log_tail, job.cut) == ("a\nb", None)


TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
BEARER = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlc2lnbmF0dXJl"
KEY_BODY = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW"


def test_secrets_in_the_log_tail_are_redacted():
    log = "\n".join(
        [
            "collecting",
            f"export GH_TOKEN={TOKEN}",
            f"Authorization: Bearer {BEARER}",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            KEY_BODY,
            "AAAA",
            "-----END OPENSSH PRIVATE KEY-----",
            "FAILED tests/test_x.py::test_y",
        ]
    )

    tail = _tail(log).log_tail or ""

    for secret in (TOKEN, BEARER, KEY_BODY, "BEGIN OPENSSH"):
        assert secret not in tail
    assert tail.splitlines()[0] == "collecting"
    assert tail.splitlines()[-1] == "FAILED tests/test_x.py::test_y"


def test_a_credential_value_on_the_line_after_its_name_is_redacted():
    tail = _tail("collecting\npassword:\n  synthetic-swordfish\nFAILED x\n").log_tail or ""

    assert "synthetic-swordfish" not in tail
    assert tail.splitlines()[-1] == "FAILED x"


def test_a_timestamped_credential_value_on_the_line_after_its_name_is_redacted():
    log = (
        "2026-11-10T19:50:00.1234567Z password:\n"
        "\ufeff2026-11-10T19:50:01Z   synthetic-swordfish\n"
        "2026-11-10T19:50:02Z FAILED x\n"
    )

    tail = _tail(log).log_tail or ""

    assert "synthetic-swordfish" not in tail
    assert tail.splitlines()[-1] == "FAILED x"


def test_a_yaml_block_scalar_credential_is_scrubbed_as_the_dispatcher_scrubs_it():
    log = (
        "2026-11-10T19:50:00Z env:\n"
        "2026-11-10T19:50:00Z   password: |\n"
        "2026-11-10T19:50:01Z     synthetic-swordfish\n"
        "2026-11-10T19:50:01Z\n"
        "2026-11-10T19:50:01Z     second-line-swordfish\n"
        "2026-11-10T19:50:01Z   api_key: >-\n"
        "2026-11-10T19:50:01Z     folded-swordfish\n"
        "2026-11-10T19:50:02Z FAILED x\n"
    )

    tail = _tail(log).log_tail or ""

    assert "swordfish" not in tail
    assert tail.splitlines() == [
        "env:",
        "  <credential omitted>",
        "  <credential omitted>",
        "FAILED x",
    ]


def test_a_name_above_the_cut_still_hides_the_value_below_it():
    tail = _tail("password:\n  synthetic-swordfish\nFAILED x\n", limit=2).log_tail or ""

    assert "synthetic-swordfish" not in tail


def test_an_unfinished_key_block_whose_begin_was_cut_stays_hidden():
    log = "\n".join(["setup", "-----BEGIN OPENSSH PRIVATE KEY-----", *[KEY_BODY] * 4, "AAAA"])

    tail = _tail(log, limit=3).log_tail or ""

    assert KEY_BODY not in tail
    assert "AAAA" not in tail


PYTEST_SUMMARY = """\
2026-11-10T19:54:58.1234567Z ============================= short test summary info ============================
2026-11-10T19:54:58.1234568Z FAILED tests/test_ci_board.py::test_budget[3 runs] - AssertionError: assert 4 == 3
2026-11-10T19:54:58.1234569Z FAILED tests/test_poll.py::test_cycle - KeyError: 'ci-board'
2026-11-10T19:54:58.1234570Z ERROR tests/test_queue.py::test_claim - sqlite3.OperationalError: database is locked
2026-11-10T19:54:58.1234571Z ERROR tests/test_broken.py - ImportError: cannot import name 'x'
2026-11-10T19:54:58.1234572Z ======= 2 failed, 310 passed, 2 errors in 41.20s =======
"""


def test_failing_tests_reads_the_pytest_summary_lines():
    assert failing_tests(PYTEST_SUMMARY) == [
        "tests/test_ci_board.py::test_budget[3 runs]",
        "tests/test_poll.py::test_cycle",
        "tests/test_queue.py::test_claim",
        "tests/test_broken.py",
    ]


def test_failing_tests_reads_raw_log_failed_lines_with_job_and_step():
    log = (
        "pytest\tRun tests\t2026-11-10T19:54:58.1Z FAILED tests/test_a.py::test_b - assert 1 == 2\n"
        "pytest\tRun tests\t2026-11-10T19:54:58.2Z \x1b[31mFAILED\x1b[0m tests/test_a.py::test_c\n"
    )

    assert failing_tests(log) == ["tests/test_a.py::test_b", "tests/test_a.py::test_c"]


def test_failing_tests_skips_lines_that_only_mention_failure():
    log = (
        "FAILED to fetch the cache\n"
        "ERROR: Process completed with exit code 1.\n"
        "tests/test_a.py::test_b FAILED                                   [ 50%]\n"
        "E   AssertionError: FAILED tests/test_a.py::test_b\n"
    )

    assert failing_tests(log) == []


def test_failing_tests_keeps_the_first_ten_distinct_tests():
    lines = [f"FAILED tests/test_a.py::test_{n % 12} - boom" for n in range(30)]

    assert failing_tests("\n".join(lines)) == [f"tests/test_a.py::test_{n}" for n in range(10)]


NOT_ACQUIRED = "The job was not acquired by Runner of type hosted even after multiple attempts"
BILLING = (
    "The job was not started because recent account payments have failed or your spending limit "
    "needs to be increased. Please check the 'Billing & plans' section in your settings"
)


@pytest.mark.parametrize(
    ("conclusion", "texts", "expected"),
    [
        ("cancelled", [NOT_ACQUIRED], ("infra", "runner not acquired")),
        ("failure", [BILLING], ("infra", "billing or spending limit")),
        ("failure", ["The hosted runner lost communication with the server."], ("infra", "runner lost")),
        ("failure", ["The runner has received a shutdown signal."], ("infra", "runner lost")),
        (
            "failure",
            ["GitHub Actions has encountered an internal error when running your job."],
            ("infra", "hosted runner unavailable"),
        ),
        ("failure", ["FAILED tests/test_x.py::test_y - AssertionError"], ("code", None)),
        ("failure", [], ("code", None)),
        ("cancelled", ["The operation was canceled."], ("unknown", None)),
        ("timed_out", [], ("unknown", None)),
        (None, [], ("unknown", None)),
    ],
)
def test_classify_failure_reads_infra_signals_before_the_conclusion(conclusion, texts, expected):
    assert classify_failure(conclusion, texts) == expected


@pytest.mark.parametrize(
    ("causes", "expected"),
    [
        (["infra", "code"], "code"),
        (["infra", "infra"], "infra"),
        (["infra", "unknown"], "infra"),
        (["unknown", "unknown"], "unknown"),
        ([], "unknown"),
    ],
)
def test_result_cause_is_code_over_infra_over_unknown(causes, expected):
    assert result_cause(causes) == expected


def test_only_a_failed_ci_result_has_a_cause():
    infra = FailedCheck("macos", cause="infra", signal="runner lost")

    assert CIResult("success").cause is None
    assert CIResult("pending").cause is None
    assert CIResult("failure", "failed: x").cause == "unknown"
    assert CIResult("failure", failed=(infra, infra)).cause == "infra"
    assert CIResult("failure", failed=(infra, PYTEST)).cause == "code"


RUNNER_LOST = (
    "2026-11-10T19:50:00Z collecting\n"
    "2026-11-10T19:50:01Z ##[error]The runner has received a shutdown signal.\n"
)


def test_a_fetched_runner_lost_log_makes_the_dispatchers_result_and_the_bundle_infra():
    result = classify_logged(CIResult("failure", failed=(PYTEST,)), {"7": RUNNER_LOST})
    bundle = ci_evidence(failed=result.failed, logs={"7": RUNNER_LOST})

    assert [(check.cause, check.signal) for check in result.failed] == [("infra", "runner lost")]
    assert (result.cause, bundle.cause) == ("infra", "infra")
    assert [(job.cause, job.signal) for job in bundle.jobs] == [("infra", "runner lost")]


def test_a_log_with_a_failing_test_stays_code_even_when_it_quotes_an_infra_signal():
    failed = "2026-11-10T19:50:02Z FAILED tests/test_x.py::test_y - boom\n"
    logs = {"7": RUNNER_LOST + failed}

    bundle = ci_evidence(failed=classify_logged(CIResult("failure", failed=(PYTEST,)), logs).failed, logs=logs)

    assert [(job.cause, job.signal) for job in bundle.jobs] == [("code", None)]
    assert bundle.cause == "code"


def test_an_infra_check_keeps_its_signal_in_the_bundle():
    lost = FailedCheck("macos", run_id="42", job_id="9", cause="infra", signal="runner not acquired")

    bundle = ci_evidence(failed=(lost,), logs={})

    assert [(job.cause, job.signal) for job in bundle.jobs] == [("infra", "runner not acquired")]
    assert bundle.cause == "infra"


SPENDING_ASSERTION = (
    "FAILED tests/test_billing.py::test_spending_limit - AssertionError: spending limit was not enforced"
)
UNITTEST_SPENDING = (
    "2026-11-10T19:50:00Z FAIL: test_spending_limit (tests.test_billing.BillingTest)\n"
    "2026-11-10T19:50:00Z Traceback (most recent call last):\n"
    "2026-11-10T19:50:00Z AssertionError: the spending limit needs to be increased was not shown\n"
    "2026-11-10T19:50:00Z ##[error]Process completed with exit code 1.\n"
)


def test_an_application_failure_that_mentions_billing_stays_code():
    assert classify_failure("failure", [SPENDING_ASSERTION]) == ("code", None)
    assert classify_failure("failure", [UNITTEST_SPENDING]) == ("code", None)
    for log in (SPENDING_ASSERTION, UNITTEST_SPENDING):
        result = classify_logged(CIResult("failure", failed=(PYTEST,)), {"7": log})
        assert result.cause == "code"
        assert ci_evidence(failed=result.failed, logs={"7": log}).cause == "code"


def test_infra_sentences_only_count_at_the_start_of_a_line():
    quoted = f"AssertionError: expected {NOT_ACQUIRED!r}"

    assert classify_failure("failure", [quoted]) == ("code", None)
    assert classify_failure("failure", [f"##[error]{NOT_ACQUIRED}"])[0] == "infra"


def test_classify_logged_leaves_a_green_result_alone():
    assert classify_logged(CIResult("success"), {"7": RUNNER_LOST}) == CIResult("success")


def test_the_spec_t2_row_states_the_rerun_and_classification_protocol():
    from pathlib import Path

    spec = (Path(__file__).parent.parent / "docs" / "SPEC.md").read_text(encoding="utf-8")
    row = next(line for line in spec.splitlines() if line.startswith("| T2 |"))

    assert "reruns the failed jobs once per head before the gate count moves or a fix is queued" in row
    assert "classified `infra` or `code`" in row
    assert "no `ready` or verdict naming that head" in row
    assert "Red: queue `fix` with the failing log" not in row
