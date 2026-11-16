from pathlib import Path

import pytest

from lib import cli
from tests.test_approver_credential import held_factory_md

TRUSTED = """\
poll_seconds: 30
projects:
  - repo: owner/repo
trusted_logins:
  maxharris1: [grok-bot]
  claude-login: [claude-bot]
  codex-login: [codex-bot]
"""

APP = """\
github_app:
  enabled: false
  apps:
    cursor-bot:
      bot_login: factory-cursor[bot]
      app_id: 111111
      installations:
        owner: 222222
      private_key_file: /nonexistent/fake-app.pem
"""


def check(tmp_path: Path, orch_text: str, *factory: Path) -> int:
    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text, encoding="utf-8")
    args = ["--orch", str(orch)]
    for path in factory:
        args += ["--factory", str(path)]
    return cli._check_config(args)


def test_a_harness_bot_login_in_approver_logins_warns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = check(tmp_path, TRUSTED + "approver_logins: [maxharris1, Claude-Login]\n")

    err = capsys.readouterr().err
    assert status == 0, err
    assert (
        "warning: login Claude-Login carries harness bot claude-bot and is in "
        "approver_logins: that bot could lift its own cap"
    ) in err
    assert "login maxharris1 carries" not in err


def test_a_harness_bot_login_as_hold_approver_warns(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = check(
        tmp_path,
        TRUSTED + "approver_logins: [maxharris1, claude-login]\n",
        held_factory_md(tmp_path, "claude-login"),
    )

    err = capsys.readouterr().err
    assert status == 0, err
    assert (
        "warning: hold.approver claude-login carries harness bot claude-bot: "
        "that bot could clear its own hold"
    ) in err


def test_no_overlap_prints_no_bot_approver_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = check(
        tmp_path,
        TRUSTED + "approver_logins: [maxharris1]\n" + APP,
        held_factory_md(tmp_path, "maxharris1"),
    )

    err = capsys.readouterr().err
    assert status == 0, err
    assert "carries harness bot" not in err
