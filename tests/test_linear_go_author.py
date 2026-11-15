from __future__ import annotations

from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import Dispatcher
from lib.orch_yml import approvals, load_approvals
from tests.test_approver_credential import (
    APP_BOT,
    MAX,
    grok_wakes,
    load,
    orch_text,
)
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions, job_count
from tests.test_rounds_and_caps import Flow

MAX_LINEAR = "linear:max-id"
OTHER_LINEAR = "linear:other-id"
TRUSTED_LINEAR = "trusted_linear_users:\n  max-id: [grok-bot]\n  other-id: [grok-bot]\n"
APPROVER_LINEAR = "approver_linear_users:\n  - max-id\n"
SWITCH = [None, False, True]


def linear_text(*, enabled: bool | None, approver: bool = True) -> str:
    return orch_text(
        enabled=enabled,
        extra=TRUSTED_LINEAR + (APPROVER_LINEAR if approver else ""),
    )


class LinearFlow(Flow):
    def __init__(self, tmp_path: Path, *, enabled: bool | None, approver: bool = True):
        super().__init__(tmp_path)
        config, findings = load(tmp_path, linear_text(enabled=enabled, approver=approver))
        assert config is not None, findings
        self.dispatcher = Dispatcher(
            self.queue.path,
            self.queue,
            trusted_logins={
                "github": dict(config.trusted_logins),
                "linear": dict(config.trusted_linear_users),
            },
            review_actions=FakeReviewActions(),
            grok_delivery=FakeGrokDelivery(),
            approvals=approvals(config),
        )


@pytest.mark.parametrize("enabled", SWITCH)
def test_capped_linear_go_from_the_approver_raises_cap_extra(
    tmp_path: Path, enabled: bool | None
) -> None:
    flow = LinearFlow(tmp_path, enabled=enabled)
    flow.to_cap()
    fixes = job_count(flow.queue, "fix")

    assert flow.go("go-2", author=MAX_LINEAR) == "queued-fix"

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
    assert job_count(flow.queue, "fix") == fixes + 1
    assert grok_wakes(flow, "go-not-from-approver") == []


@pytest.mark.parametrize("enabled", SWITCH)
def test_uncapped_linear_go_from_another_user_keeps_todays_trust(
    tmp_path: Path, enabled: bool | None
) -> None:
    flow = LinearFlow(tmp_path, enabled=enabled)
    assert flow.go("go-1", author=OTHER_LINEAR) == "queued-build"


@pytest.mark.parametrize("enabled", SWITCH)
@pytest.mark.parametrize("approver", [True, False])
def test_capped_linear_go_from_another_user_does_not_lift_the_cap(
    tmp_path: Path, enabled: bool | None, approver: bool
) -> None:
    flow = LinearFlow(tmp_path, enabled=enabled, approver=approver)
    flow.to_cap()
    fixes = job_count(flow.queue, "fix")

    assert flow.go("go-2", author=OTHER_LINEAR) == "go-not-from-approver"
    flow.dispatcher.process(flow.snapshot())

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)
    assert job_count(flow.queue, "fix") == fixes
    assert [row["source_comment_id"] for row in grok_wakes(flow, "go-not-from-approver")] == ["go-2"]


@pytest.mark.parametrize("enabled", SWITCH)
def test_unset_key_lifts_no_capped_linear_go(tmp_path: Path, enabled: bool | None) -> None:
    flow = LinearFlow(tmp_path, enabled=enabled, approver=False)
    flow.to_cap()
    assert flow.go("go-2", author=MAX_LINEAR) == "go-not-from-approver"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)


@pytest.mark.parametrize(
    ("enabled", "author", "outcome"),
    [
        (None, MAX, "queued-fix"),
        (None, APP_BOT, "queued-fix"),
        (False, MAX, "queued-fix"),
        (False, APP_BOT, "queued-fix"),
        (True, MAX, "queued-fix"),
        (True, APP_BOT, "go-not-from-approver"),
    ],
)
def test_github_capped_go_is_unchanged_with_a_linear_approver_set(
    tmp_path: Path, enabled: bool | None, author: str, outcome: str
) -> None:
    flow = LinearFlow(tmp_path, enabled=enabled)
    flow.to_cap()
    assert flow.go("go-2", author=author) == outcome


def test_linear_approver_config_parses(tmp_path: Path) -> None:
    config, findings = load(tmp_path, linear_text(enabled=None))
    assert config is not None, findings
    assert config.approver_linear_users == frozenset({MAX_LINEAR})
    policy = approvals(config)
    assert policy.may_approve_linear(MAX_LINEAR)
    assert not policy.may_approve_linear(OTHER_LINEAR)

    loaded, findings = load_approvals(tmp_path / "orch.yml")
    assert loaded is not None, findings
    assert loaded.linear_users == frozenset({MAX_LINEAR})


@pytest.mark.parametrize(
    "value",
    ["approver_linear_users: max-id\n", "approver_linear_users: []\n", "approver_linear_users:\n  - '  '\n"],
)
def test_malformed_linear_approver_is_an_error(tmp_path: Path, value: str) -> None:
    config, findings = load(tmp_path, orch_text(enabled=None, extra=TRUSTED_LINEAR + value))
    assert config is None
    assert any("approver_linear_users" in item.message for item in findings), findings


def test_linear_approver_outside_trusted_linear_users_is_an_error(tmp_path: Path) -> None:
    text = orch_text(enabled=None, extra=TRUSTED_LINEAR + "approver_linear_users:\n  - stranger-id\n")
    config, findings = load(tmp_path, text)
    assert config is None
    assert any(
        item.severity == "error" and "stranger-id" in item.message and "trusted_linear_users" in item.message
        for item in findings
    ), findings
    assert cli._check_config(["--orch", str(tmp_path / "orch.yml")]) != 0


LINEAR_PROJECT = """\
poll_seconds: 30
linear:
  api_key_env: LINEAR_API_KEY
projects:
  - repo: owner/repo
    linear_project_id: fake-project
trusted_logins:
  maxharris1: [grok-bot]
"""


@pytest.mark.parametrize(
    ("text", "warned"),
    [
        (LINEAR_PROJECT + TRUSTED_LINEAR, True),
        (LINEAR_PROJECT + TRUSTED_LINEAR + APPROVER_LINEAR, False),
        (orch_text(enabled=None), False),
    ],
)
def test_check_config_warns_when_a_linear_project_has_no_linear_approver(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, warned: bool
) -> None:
    (tmp_path / "orch.yml").write_text(text, encoding="utf-8")
    assert cli._check_config(["--orch", str(tmp_path / "orch.yml")]) == 0
    assert ("approver_linear_users is unset" in capsys.readouterr().err) is warned
