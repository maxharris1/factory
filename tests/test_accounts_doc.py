from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from lib import account_doctor

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "ACCOUNTS.md"

COMMANDS = (
    "orch accounts prepare",
    "env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=~/.factory-accounts/<id> claude auth login",
    "CODEX_HOME=~/.factory-accounts/<id> codex login --device-auth",
    "orch accounts doctor",
)

DETAILS = (
    account_doctor.NOT_LOGGED_IN,
    account_doctor.NOT_UNDERSTOOD,
    account_doctor.EXIT_NONZERO,
    account_doctor.NO_IDENTITY,
    account_doctor.TOKEN_AUTH,
    account_doctor.CODEX_API_KEY,
    account_doctor.METHOD_NOT_UNDERSTOOD,
    account_doctor.SHARED_IDENTITY,
    account_doctor.SPLIT_IDENTITY,
    account_doctor.PLACEHOLDER_DEVICE,
    account_doctor.NO_SSH_ALIAS,
    account_doctor.SSH_DOWN,
    account_doctor.NO_REPLY,
)


def doc() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("command", COMMANDS)
def test_the_procedure_gives_each_command_verbatim(command: str) -> None:
    assert command in doc()


def test_every_doctor_status_and_the_flag_reason_are_named() -> None:
    text = doc()
    names = [*get_args(account_doctor.Status), account_doctor.REASON]

    assert [name for name in names if f"`{name}`" not in text] == []


def test_every_detail_the_doctor_writes_is_in_the_status_table() -> None:
    text = doc()
    missing = [
        detail for detail in DETAILS
        if any(part.strip() not in text for part in detail.split("{}") if part.strip())
    ]

    assert missing == []


@pytest.mark.parametrize("path", ["README.md", "docs/SPEC.md"])
def test_readme_and_spec_link_the_procedure(path: str) -> None:
    assert "docs/ACCOUNTS.md" in (ROOT / path).read_text(encoding="utf-8")


def mac_token_steps() -> str:
    section = doc().split("## Move the Mac's profile token off the default account", 1)[1]
    return section.split("\n## ", 1)[0]


def test_the_mac_token_move_unsets_the_inherited_export_before_the_login() -> None:
    steps = mac_token_steps()

    assert "unset CLAUDE_CODE_OAUTH_TOKEN" in steps
    assert steps.index("unset CLAUDE_CODE_OAUTH_TOKEN") < steps.index("claude auth login")
    assert "new login shell" not in steps


def test_the_token_status_fix_points_to_the_mac_token_steps() -> None:
    row = next(line for line in doc().splitlines() if f"`{account_doctor.TOKEN_AUTH}`" in line)

    assert "Move the Mac's profile token off the default account" in row


DOC_HOME = "~/.factory-accounts/<id>"


@pytest.mark.parametrize("harness, documented", [("claude", COMMANDS[1]), ("codex", COMMANDS[2])])
def test_the_printed_sign_in_command_is_the_documented_one(harness: str, documented: str) -> None:
    assert documented in doc()
    home = "/home/claude-bot/.factory-accounts/claude-b"
    assert account_doctor.login_command(harness, home) == documented.replace(DOC_HOME, home)


@pytest.mark.parametrize("harness, expected", [
    ("claude", "env -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDE_CONFIG_DIR claude auth login"),
    ("codex", "env -u CODEX_HOME codex login --device-auth"),
])
def test_the_device_login_runs_the_documented_command_without_a_folder(harness: str, expected: str) -> None:
    assert account_doctor.login_command(harness, None) == expected
