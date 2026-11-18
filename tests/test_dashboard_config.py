from pathlib import Path

import pytest

from lib.dashboard_config import parse_dashboard
from lib.load import Finding
from tests.test_dashboard_push import BASE_ORCH, TARGET, run_orch


def _parse(push: dict) -> tuple[object, list[str]]:
    findings: list[Finding] = []
    config = parse_dashboard({"push": push}, findings)
    return config, [item.message for item in findings if item.severity == "error"]


@pytest.mark.parametrize("budget", [1, 5])
def test_qa_budget_gib_is_a_positive_integer_on_an_ssh_push(budget: int) -> None:
    config, errors = _parse({"transport": "ssh", "host": TARGET, "qa_budget_gib": budget})
    assert errors == []
    assert config.push.qa_budget_gib == budget


def test_qa_budget_gib_is_unset_by_default() -> None:
    config, _ = _parse({"transport": "ssh", "host": TARGET})
    assert config.push.qa_budget_gib is None


@pytest.mark.parametrize("budget", [0, -1, 1.5, "5", True, False])
def test_qa_budget_gib_refuses_anything_but_a_positive_integer(budget: object) -> None:
    config, errors = _parse({"transport": "ssh", "host": TARGET, "qa_budget_gib": budget})
    assert config is None
    assert errors == ["dashboard.push.qa_budget_gib must be a positive integer"]


def test_qa_budget_gib_is_only_for_ssh() -> None:
    config, errors = _parse({"transport": "none", "qa_budget_gib": 5})
    assert config is None
    assert errors == ["dashboard.push.qa_budget_gib is only for transport ssh"]


def test_check_config_accepts_a_qa_budget(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    push = f"dashboard:\n  push:\n    transport: ssh\n    host: {TARGET}\n    qa_budget_gib: 5\n"
    path.write_text(BASE_ORCH + push, encoding="utf-8")
    result = run_orch("check-config", "--orch", str(path))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "push, message",
    [
        *[
            (f"transport: ssh\n    host: {TARGET}\n    qa_budget_gib: {budget}\n", "qa_budget_gib must be a positive integer")
            for budget in ("0", "-1", "1.5", '"5"', "true")
        ],
        ("transport: none\n    qa_budget_gib: 5\n", "qa_budget_gib is only for transport ssh"),
    ],
)
def test_check_config_refuses_a_bad_qa_budget(tmp_path: Path, push: str, message: str) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(BASE_ORCH + f"dashboard:\n  push:\n    {push}", encoding="utf-8")
    result = run_orch("check-config", "--orch", str(path))
    assert result.returncode != 0
    assert f"dashboard.push.{message}" in result.stdout + result.stderr
