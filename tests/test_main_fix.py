from __future__ import annotations

import sys

import pytest

from lib.dispatch.types import BaseCheck, FailedCheck
from lib.evidence.ci import MainRed, ci_evidence
from lib.feeder import BODY_LIMIT, parse_intake
from lib.feeder_gates import MAIN_FIX_LABEL, GateLines, parse_gate_lines
from lib.followups import has_marker
from lib.main_fix import (
    Comment,
    File,
    MainFixRow,
    MainRead,
    Resume,
    Retire,
    decide,
    head_comment,
    head_marker,
    issue_body,
    issue_title,
    marked_issue_fate,
    marker,
)

REPO = "owner/app"
HEAD = "c0ffee12" + "0" * 32
LABELS = ("factory:ready", MAIN_FIX_LABEL)
CHECKS = tuple(
    BaseCheck(f"test ({n}/4)", "failure", "900", str(10 + n), HEAD) for n in range(1, 5)
)
RED = MainRed(HEAD, CHECKS)
FAILED = MainRead(None, True)
RED_READ = MainRead(RED, False)
GREEN = MainRead(None, False)
FILING = MainFixRow(REPO, "main", "m", "filing", None, HEAD, 1.0, "t", "b")
OPEN = MainFixRow(REPO, "main", "m", "open", 7, HEAD, 1.0, "t", "b")
LATER = MainRed("d" * 40, CHECKS)
LATER_READ = MainRead(LATER, False)


@pytest.mark.parametrize(
    ("row", "read", "issue_open", "actions"),
    [
        (FILING, FAILED, None, ()),
        (FILING, RED_READ, None, (Resume(FILING),)),
        (FILING, RED_READ, False, (Resume(FILING),)),
        (FILING, GREEN, None, (Retire(FILING),)),
        (None, FAILED, None, ()),
        (OPEN, FAILED, False, ()),
        (None, RED_READ, None, (File(RED),)),
        (None, GREEN, None, ()),
        (OPEN, RED_READ, True, ()),
        (OPEN, RED_READ, False, (Retire(OPEN), File(RED))),
        (OPEN, RED_READ, None, ()),
        (OPEN, LATER_READ, True, (Comment(OPEN, LATER),)),
        (OPEN, LATER_READ, None, ()),
        (OPEN, LATER_READ, False, (Retire(OPEN), File(LATER))),
        (FILING, LATER_READ, True, (Resume(FILING),)),
        (OPEN, GREEN, True, (Retire(OPEN),)),
        (OPEN, GREEN, None, (Retire(OPEN),)),
    ],
)
def test_decide(row, read, issue_open, actions) -> None:
    assert decide(row, read, issue_open=issue_open) == actions


@pytest.mark.parametrize(
    ("row", "number", "fate"),
    [(FILING, 7, "hold"), (OPEN, 7, "feed"), (OPEN, 8, "close")],
)
def test_marked_issue_fate(row, number, fate) -> None:
    assert marked_issue_fate(row, number) == fate


def body_for(logs: dict[str, str], checks: tuple[BaseCheck, ...] = CHECKS) -> tuple[str, str]:
    failed = [
        FailedCheck(check.name, check.run_id, check.job_id,
                    f"https://github.com/{REPO}/actions/runs/{check.run_id}")
        for check in checks
    ]
    tag = marker(REPO, "main", 1000.0)
    evidence = ci_evidence(failed=failed, logs=logs, limit=sys.maxsize)
    return issue_body(REPO, "main", ("test (*)",), MainRed(HEAD, checks), evidence, tag), tag


def big_log(n: int, tail: tuple[str, ...] = ()) -> str:
    lines = [f"PLANTED-{n} near the start"]
    lines += [f"step {i} " + "x" * 90 for i in range(400)]
    return "\n".join([*lines, *tail, f"FAILED tests/test_x.py::test_{n} - LAST-{n}"])


def test_three_big_logs_fit_the_body_limit_keeping_each_last_line() -> None:
    logs = {check.job_id: big_log(n) for n, check in enumerate(CHECKS, start=1)}
    assert sum(map(len, logs.values())) > 3 * 40_000

    body, tag = body_for(logs)

    assert len(body) <= BODY_LIMIT
    assert has_marker(body, tag)
    assert f"https://github.com/{REPO}/actions/runs/900" in body
    assert HEAD in body
    for n in (1, 2, 3):
        assert f"FAILED tests/test_x.py::test_{n} - LAST-{n}" in body
        assert f"PLANTED-{n}" not in body
        assert f"`tests/test_x.py::test_{n}`" in body
    assert "test (4/4)" in body
    assert "LAST-4" not in body
    assert body.count("earlier lines]") == 3
    template = parse_intake(body, LABELS)
    assert template.defects == ()
    assert (template.risk, template.plan_first, template.ticket_type) == ("high", False, "bug-fix")
    assert template.criteria == ("The required checks (`test (*)`) pass on `main`.",)
    assert parse_gate_lines(body, REPO) == GateLines()


def test_long_check_names_and_test_ids_leave_every_log_tail_its_last_line() -> None:
    checks = tuple(
        BaseCheck(f"test ({n}) " + "c" * 900, "failure", "900", str(n), HEAD) for n in range(400)
    )
    logs = {
        check.job_id: "\n".join(
            [*(f"FAILED tests/test_x.py::test_{check.job_id}_{i}[{'p' * 800}]" for i in range(40)),
             f"LAST-{check.job_id}"]
        )
        for check in checks[:3]
    }

    body, _ = body_for(logs, checks)

    assert len(body) <= BODY_LIMIT
    assert all(f"LAST-{n}" in body for n in range(3))
    assert "Logs are shown for the first 3 red jobs only." in body
    assert parse_intake(body, LABELS).defects == ()


def test_a_job_with_no_log_says_so() -> None:
    body, _ = body_for({"11": "FAILED tests/test_x.py::test_1"})
    assert "FAILED tests/test_x.py::test_1" in body
    assert body.count("Log unavailable.") == 2


def test_log_text_cannot_add_template_fields_or_gate_lines() -> None:
    hostile = (
        "```",
        "~~~",
        "Risk: low",
        "Plan first: yes",
        "Touches: x.py",
        "Blocked by #5",
        "Conflicts with #6",
        "## Acceptance criteria",
        "- ac-9 merges after PR #3",
        "```",
    )
    clean, _ = body_for({check.job_id: big_log(1) for check in CHECKS})
    dirty, _ = body_for({check.job_id: big_log(1, hostile) for check in CHECKS})
    assert "- ac-9 merges after PR #3" in dirty
    assert parse_intake(dirty, LABELS) == parse_intake(clean, LABELS)
    assert parse_gate_lines(dirty, REPO) == GateLines()


def test_the_title_names_the_head_and_the_red_checks() -> None:
    assert issue_title(MainRed(HEAD, CHECKS[:2])) == "Main is red at c0ffee12: test (1/4), test (2/4)"


def test_a_head_comment_carries_its_head_and_each_last_log_line_within_the_limit() -> None:
    failed = [
        FailedCheck(check.name, check.run_id, check.job_id,
                    f"https://github.com/{REPO}/actions/runs/{check.run_id}")
        for check in CHECKS
    ]
    logs = {check.job_id: big_log(n) for n, check in enumerate(CHECKS, start=1)}
    evidence = ci_evidence(failed=failed, logs=logs, limit=sys.maxsize)
    row = MainFixRow(REPO, "main", marker(REPO, "main", 1000.0), "open", 7, HEAD, 1000.0, "t", "b")
    tag = head_marker(row, LATER.head)

    comment = head_comment("main", LATER, evidence, tag)

    assert len(comment) <= BODY_LIMIT
    assert has_marker(comment, tag)
    assert not has_marker(comment, row.marker)
    assert LATER.head in comment
    for n in (1, 2, 3):
        assert f"FAILED tests/test_x.py::test_{n} - LAST-{n}" in comment
        assert f"`tests/test_x.py::test_{n}`" in comment
    assert parse_gate_lines(comment, REPO) == GateLines()


def test_each_head_of_a_streak_has_its_own_marker() -> None:
    other = MainFixRow(REPO, "main", "m2", "open", 8, HEAD, 2.0, "t", "b")
    tags = {head_marker(OPEN, HEAD), head_marker(OPEN, LATER.head), head_marker(other, HEAD)}
    assert len(tags) == 3
