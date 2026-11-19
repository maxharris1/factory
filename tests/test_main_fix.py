"""Main-fix filing (issue #1061): the pure decision table and the issue body."""

from __future__ import annotations

import pytest

from lib.dispatch.types import BaseCheck, FailedCheck
from lib.evidence.ci import MainRed, ci_evidence
from lib.feeder import BODY_LIMIT, parse_intake
from lib.feeder_gates import MAIN_FIX_LABEL, GateLines, parse_gate_lines
from lib.followups import has_marker
from lib.main_fix import (
    File,
    MainFixRow,
    MainRead,
    Resume,
    Retire,
    decide,
    issue_body,
    issue_title,
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
FILING = MainFixRow(REPO, "main", "m", "filing", None, HEAD, 1.0)
OPEN = MainFixRow(REPO, "main", "m", "open", 7, HEAD, 1.0)


@pytest.mark.parametrize(
    ("row", "read", "issue_open", "actions"),
    [
        (FILING, FAILED, None, ()),
        (FILING, RED_READ, None, (Resume(FILING, RED),)),
        (FILING, RED_READ, False, (Resume(FILING, RED),)),
        (FILING, GREEN, None, (Retire(FILING),)),
        (None, FAILED, None, ()),
        (OPEN, FAILED, False, ()),
        (None, RED_READ, None, (File(RED),)),
        (None, GREEN, None, ()),
        (OPEN, RED_READ, True, ()),
        (OPEN, RED_READ, False, (Retire(OPEN), File(RED))),
        (OPEN, RED_READ, None, ()),
        (OPEN, GREEN, True, (Retire(OPEN),)),
        (OPEN, GREEN, None, (Retire(OPEN),)),
    ],
)
def test_decide(row, read, issue_open, actions) -> None:
    assert decide(row, read, issue_open=issue_open) == actions


def body_for(logs: dict[str, str]) -> tuple[str, str]:
    failed = [
        FailedCheck(check.name, check.run_id, check.job_id,
                    f"https://github.com/{REPO}/actions/runs/{check.run_id}")
        for check in CHECKS
    ]
    tag = marker(REPO, "main", 1000.0)
    evidence = ci_evidence(failed=failed, logs=logs)
    return issue_body(REPO, "main", ("test (*)",), RED, evidence, tag), tag


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
