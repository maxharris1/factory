from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.dispatcher import Dispatcher
from lib.rule_engine import Check, Escalate, Grant, Ruling, finding_id
from tests.test_hooks import setup, ticket

F1 = finding_id("f-1")
AC2 = finding_id("ac-2")
GO = "<!-- orch:v1 kind=go from=orch round=4/3 state=fixing -->"
GRANT = Ruling(
    "first-cap-grant",
    Grant(GO),
    (Check("first-stop", True, 1), Check("stuck", False, None), Check("ci", None, "unknown")),
)
ESCALATE = Ruling("no-rule", Escalate("no-rule"), ())


def rows(dispatcher: Dispatcher) -> list[dict]:
    with dispatcher._connect() as connection:
        cursor = connection.execute(
            "SELECT repo, ticket_id, site, subject, rule, outcome, reason, checks, finding_ids,"
            " header, decided_at FROM engine_decisions"
        )
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def log(dispatcher: Dispatcher, ruling: Ruling, subject: str = "verdict-7") -> bool:
    with dispatcher._connect() as connection:
        return dispatcher.review.log_ruling(connection, ticket(), "cap", subject, ruling, (F1, AC2))


def engine_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if " engine " in line]


def test_a_ruling_writes_one_row_and_one_poller_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dispatcher, _queue = setup(tmp_path)

    assert log(dispatcher, GRANT) is True

    assert rows(dispatcher) == [
        {
            "repo": "owner/repo",
            "ticket_id": "42",
            "site": "cap",
            "subject": "verdict-7",
            "rule": "first-cap-grant",
            "outcome": "granted",
            "reason": None,
            "checks": json.dumps(
                [
                    {"name": "first-stop", "passed": True, "value": 1},
                    {"name": "stuck", "passed": False, "value": None},
                    {"name": "ci", "passed": None, "value": "unknown"},
                ]
            ),
            "finding_ids": json.dumps(["f-1", "ac-2"]),
            "header": GO,
            "decided_at": 100.0,
        }
    ]
    assert engine_lines(capsys) == [
        "orch poll: engine owner/repo 42 site=cap subject=verdict-7"
        " rule=first-cap-grant outcome=granted"
    ]


def test_the_same_ruling_again_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dispatcher, _queue = setup(tmp_path)
    log(dispatcher, GRANT)
    first = rows(dispatcher)
    capsys.readouterr()

    assert log(dispatcher, GRANT) is False

    assert rows(dispatcher) == first
    assert engine_lines(capsys) == []


def test_a_changed_outcome_for_the_same_subject_updates_the_row_with_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dispatcher, _queue = setup(tmp_path)
    log(dispatcher, GRANT)
    capsys.readouterr()

    assert log(dispatcher, ESCALATE) is True

    [row] = rows(dispatcher)
    assert (row["rule"], row["outcome"], row["reason"], row["checks"], row["header"]) == (
        "no-rule",
        "escalated",
        "no-rule",
        "[]",
        None,
    )
    assert engine_lines(capsys) == [
        "orch poll: engine owner/repo 42 site=cap subject=verdict-7"
        " rule=no-rule outcome=escalated reason=no-rule"
    ]


def test_another_subject_is_its_own_row(tmp_path: Path) -> None:
    dispatcher, _queue = setup(tmp_path)
    log(dispatcher, GRANT, subject="verdict-7")
    log(dispatcher, GRANT, subject="verdict-9")

    assert [row["subject"] for row in rows(dispatcher)] == ["verdict-7", "verdict-9"]
