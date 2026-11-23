from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from lib import account_doctor, account_requests

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "ACCOUNTS.md"
DECISION = ROOT / "docs" / "decisions" / "1066-settings-accounts.md"

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


def settings_section() -> str:
    return doc().partition("## Manage accounts from Settings")[2].split("\n## ", 1)[0]


@pytest.mark.parametrize("op", sorted(account_requests.OPS))
def test_the_settings_op_table_gives_each_op_its_keys(op: str) -> None:
    row = next((line for line in settings_section().splitlines() if line.startswith(f"| `{op}` |")), "")
    keys = set(account_requests.OPS[op])
    keys |= account_requests.SET_FIELDS if op == "set" else set()
    keys |= account_requests.REMOVE_EXPECT_KEYS if op == "remove" else set()

    assert [key for key in sorted(keys) if f"`{key}`" not in row] == []


def refusals(path: Path, raiser: str, scopes: frozenset[str] | None) -> set[tuple[object, ...]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef) or (scopes is not None and scope.name not in scopes):
            continue
        for call in ast.walk(scope):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == raiser:
                args = []
                for arg in call.args:
                    if not isinstance(arg, ast.Constant):
                        break
                    args.append(arg.value)
                if args:
                    found.add(tuple(args))
    return found


SERVER_SCOPES = frozenset({
    "_read_json", "_session", "require_approver", "_account_settings", "device_row", "submit", "_write",
})


@pytest.mark.parametrize("path, raiser, scopes", [
    ("deploy/factory-dashboard-http.py", "ApiError", SERVER_SCOPES),
    ("lib/account_settings.py", "Refused", None),
    ("lib/settings_apply.py", "Refused", frozenset({"_approve", "_plan", "_pair"})),
])
def test_every_refusal_an_account_request_can_meet_is_named(
    path: str, raiser: str, scopes: frozenset[str] | None
) -> None:
    lines = settings_section().splitlines()

    def named(refusal: tuple[object, ...]) -> bool:
        *status, code = refusal
        return any(f"`{code}`" in line and all(line.startswith(f"| {s} |") for s in status) for line in lines)

    found = refusals(ROOT / path, raiser, scopes)

    assert found
    assert sorted(refusal for refusal in found if not named(refusal)) == []


def test_the_1066_decision_note_records_the_three_defaults() -> None:
    assert DECISION.is_file()
    note = DECISION.read_text(encoding="utf-8")

    for field in ("- **Decision:**", "- **When:**", "- **Source:**"):
        assert note.count(field) == 3, field
    for phrase in ("`paused`", "running job", "`~/.factory-accounts/<id>`", "`<harness>-default`", "`external`",
                   "read-only"):
        assert phrase in note, phrase
    assert "decisions/1066-settings-accounts.md" in settings_section()


def test_run_doctor_is_documented_as_preparing_the_pair_first() -> None:
    apply_pair = next(
        node for node in ast.walk(ast.parse((ROOT / "lib" / "settings_apply.py").read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_pair"
    )
    calls = {node.func.attr for node in ast.walk(apply_pair)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    ops_read = [node for node in ast.walk(apply_pair) if isinstance(node, ast.Attribute) and node.attr == "op"]
    assert "_prepare" in calls and ops_read == []

    row = next(line for line in settings_section().splitlines() if line.startswith("| `doctor` |"))
    assert "as Prepare does" in row
    prose = {path: " ".join((ROOT / path).read_text(encoding="utf-8").split())
             for path in ("docs/ACCOUNTS.md", "README.md", "docs/SPEC.md")}
    assert "Run doctor does the same: it prepares the pair first" in prose["docs/ACCOUNTS.md"]
    assert "Doctor does the same: it prepares the pair before the pass" in prose["README.md"]
    assert "`prepare` and `doctor` each run `orch accounts prepare` for the pair" in prose["docs/SPEC.md"]
    assert [path for path, text in prose.items() if "Doctor runs only the" in text or "doctor runs only" in text] == []
