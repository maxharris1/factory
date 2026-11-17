from __future__ import annotations

from lib.dispatch.types import FailedCheck
from lib.evidence.ci import LOG_LINES, CIEvidence, CIJobLog, ci_evidence, failing_tests

RUN_URL = "https://github.com/o/r/actions/runs/42"
PYTEST = FailedCheck("pytest", run_id="42", job_id="7", run_url=RUN_URL)


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
            ),
            CIJobLog("commit status", None, None, None, None),
        ),
    )


def test_two_jobs_of_one_run_list_the_run_once():
    other = FailedCheck("lint", run_id="42", job_id="8", run_url=RUN_URL)

    bundle = ci_evidence(failed=(PYTEST, other), logs={"7": "a", "8": None})

    assert bundle.runs == (("42", RUN_URL),)
    assert [job.log_tail for job in bundle.jobs] == ["a", None]


def test_a_job_whose_log_was_not_fetched_is_unavailable():
    bundle = ci_evidence(failed=(PYTEST,), logs={})

    assert bundle.jobs == (CIJobLog("pytest", "42", RUN_URL, None, None),)


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
