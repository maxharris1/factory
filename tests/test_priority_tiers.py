"""Explicit priority tiers and critical-path order (issue #258).

A project's `intake.priority_labels` (or, opted in, Linear priority) puts a
ticket in a tier; the feeder and the claim order rank by tier first, then
urgent, a resumed step, bug, how many tickets it unblocks, and age.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.orch_yml import load_orch_yml
from tests.test_feeder import GITHUB_ORCH, LINEAR_ORCH, orch_config

TIERS = "      priority_labels: [priority:p0, priority:p1, priority:p2, priority:p3]\n"


def _with(orch: str, intake_lines: str) -> str:
    anchor = "      label: factory:ready\n" if "label: factory:ready" in orch else "      linear_state: In Progress\n"
    return orch.replace(anchor, anchor + intake_lines)


def _refused(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "orch.yml"
    path.write_text(body, encoding="utf-8")
    config, findings = load_orch_yml(path, allow_placeholders=True)
    assert config is None
    return [item.message for item in findings]


def test_intake_reads_priority_labels_and_the_two_switches(tmp_path: Path) -> None:
    intake = orch_config(tmp_path, _with(GITHUB_ORCH, TIERS + "      critical_path_order: false\n")).projects[0].intake
    assert intake.priority_labels == ("priority:p0", "priority:p1", "priority:p2", "priority:p3")
    assert intake.priority_from_linear is False
    assert intake.critical_path_order is False
    linear = orch_config(tmp_path, _with(LINEAR_ORCH, "      priority_from_linear: true\n")).projects[0].intake
    assert linear.priority_from_linear is True


def test_intake_without_the_new_keys_keeps_todays_order(tmp_path: Path) -> None:
    intake = orch_config(tmp_path, GITHUB_ORCH).projects[0].intake
    assert intake.priority_labels == ()
    assert intake.priority_from_linear is False
    assert intake.critical_path_order is True


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ("      priority_labels: priority:p0\n", "priority_labels must be a list"),
        ("      priority_labels: [priority:p0, '']\n", "priority_labels must be a list"),
        ("      priority_labels: [p0, P0]\n", "priority_labels names p0 twice"),
        ("      critical_path_order: maybe\n", "critical_path_order must be true or false"),
        ("      priority_from_linear: true\n", "priority_from_linear requires linear_project_id"),
    ],
)
def test_bad_priority_config_is_refused(tmp_path: Path, lines: str, message: str) -> None:
    messages = _refused(tmp_path, _with(GITHUB_ORCH, lines))
    assert any(message in item for item in messages), messages


def test_priority_from_linear_must_be_a_bool(tmp_path: Path) -> None:
    messages = _refused(tmp_path, _with(LINEAR_ORCH, "      priority_from_linear: 1\n"))
    assert any("priority_from_linear must be true or false" in item for item in messages), messages
