from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import re
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import get_args

import sqlite3

import pytest

from lib import account_doctor, account_requests, account_signin, device_monitor, signin_attempt, signin_broker
from lib.dashboard_accounts import read_accounts
from lib.dashboard_pages.settings import ADD_HARNESSES
from lib.machines_yml import Account, Device, MachinesConfig
from lib.queue import SQLiteJobQueue
from lib.signin_wake import announce
from tests.test_account_doctor import NOW, DoctorTransport, poller_config, sign_out_claude_b

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "ACCOUNTS.md"
DECISION = ROOT / "docs" / "decisions" / "1066-settings-accounts.md"
SIGNIN_NOTE = ROOT / "docs" / "decisions" / "1067-dashboard-signin.md"

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
    account_doctor.PARTIAL_LOGIN,
    account_doctor.CLI_NOT_FOUND,
    account_doctor.NO_IDENTITY,
    account_doctor.TOKEN_AUTH,
    account_doctor.CODEX_API_KEY,
    account_doctor.METHOD_NOT_UNDERSTOOD,
    account_doctor.SHARED_IDENTITY,
    account_doctor.SPLIT_IDENTITY,
    account_doctor.DISTURBED_ACCOUNT,
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


@pytest.mark.parametrize("path, link", [
    ("README.md", "docs/ACCOUNTS.md"),
    ("README.md", "docs/ACCOUNTS.md#sign-in-from-settings"),
    ("docs/SPEC.md", "ACCOUNTS.md"),
    ("docs/SPEC.md", "ACCOUNTS.md#sign-in-from-settings"),
])
def test_readme_and_spec_link_the_procedure(path: str, link: str) -> None:
    assert link in (ROOT / path).read_text(encoding="utf-8")


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
    ("cursor", "env -u CURSOR_API_KEY -u CURSOR_AUTH_TOKEN -u CURSOR_CONFIG_DIR cursor-agent login"),
])
def test_the_device_login_runs_the_documented_command_without_a_folder(harness: str, expected: str) -> None:
    assert account_doctor.login_command(harness, None) == expected
    assert f"`{expected}`" in doc()


def test_the_doc_says_cursor_builds_and_reviews_as_orch_yml_allows() -> None:
    from lib.orch_yml import load_orch_yml

    shipped, _ = load_orch_yml(ROOT / "orch.yml", allow_placeholders=True)
    section = " ".join(doc().partition("### Cursor builds and reviews")[2].split("\n#", 1)[0].split())

    assert shipped is not None
    assert "cursor" not in shipped.refused_harnesses
    assert "gemini" in shipped.refused_harnesses
    assert "`orch.yml` no longer lists it in `refused_harnesses`" in section
    assert "docs/decisions/1411-cursor-accounts.md" in section


def settings_section() -> str:
    return doc().partition("## Manage accounts from Settings")[2].split("\n## ", 1)[0]


ALL_OPS = {**account_requests.OPS, **account_requests.BROKER_OPS}


@pytest.mark.parametrize("op", sorted(ALL_OPS))
def test_the_settings_op_table_gives_each_op_its_keys(op: str) -> None:
    row = next((line for line in settings_section().splitlines() if line.startswith(f"| `{op}` |")), "")
    assert row
    keys = set(ALL_OPS[op])
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
    calls = {node.func.id for node in ast.walk(apply_pair)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    ops_read = [node for node in ast.walk(apply_pair) if isinstance(node, ast.Attribute) and node.attr == "op"]
    assert "prepare_pair" in calls and ops_read == []

    row = next(line for line in settings_section().splitlines() if line.startswith("| `doctor` |"))
    assert "as Prepare does" in row
    prose = {path: " ".join((ROOT / path).read_text(encoding="utf-8").split())
             for path in ("docs/ACCOUNTS.md", "README.md", "docs/SPEC.md")}
    assert "Run doctor does the same: it prepares the pair first" in prose["docs/ACCOUNTS.md"]
    assert "Doctor does the same: it prepares the pair before the pass" in prose["README.md"]
    assert "`prepare` and `doctor` each run `orch accounts prepare` for the pair" in prose["docs/SPEC.md"]
    assert [path for path, text in prose.items() if "Doctor runs only the" in text or "doctor runs only" in text] == []


@cache
def server() -> ModuleType:
    spec = importlib.util.spec_from_file_location("signin_doc_server", ROOT / "deploy" / "factory-dashboard-http.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def signin_note() -> str:
    return SIGNIN_NOTE.read_text(encoding="utf-8")


def decision_line(name: str) -> str:
    lines = [line for line in signin_note().splitlines()
             if line.startswith("- **Decision:**") and f"`{name}`" in line]
    assert len(lines) == 1, name
    return lines[0]


def signin_section() -> str:
    return doc().partition("## Sign in from Settings")[2].split("\n## ", 1)[0]


SIGNIN_LIMITS = {
    "expiry minutes": lambda: signin_attempt.EXPIRY_SECONDS / 60,
    "cap": lambda: signin_broker.MAX_CONCURRENT,
    "code seconds": lambda: server().CODE_SECONDS,
    "cancel seconds": lambda: server().CANCEL_SECONDS,
}


@pytest.mark.parametrize("limit, path, pattern", [
    ("expiry minutes", "docs/decisions/1067-dashboard-signin.md", r"ends `expired` (\d+) minutes after"),
    ("expiry minutes", "docs/ACCOUNTS.md", r"An attempt that is not done (\d+) minutes"),
    ("expiry minutes", "docs/ACCOUNTS.md", r"\| (\d+) minutes passed\. \|"),
    ("expiry minutes", "README.md", r"the (\d+)-minute window"),
    ("cap", "docs/decisions/1067-dashboard-signin.md", r"at most (\d+) at once"),
    ("cap", "docs/ACCOUNTS.md", r"\| `too-many` \| (\d+) attempts are already live"),
    ("cap", "README.md", r"the (\d+)-at-once cap"),
    ("code seconds", "docs/decisions/1067-dashboard-signin.md", r"has not taken after (\d+) seconds"),
    ("code seconds", "docs/ACCOUNTS.md", r"taken after (\d+) seconds, and a `<id>\.cancel`"),
    ("code seconds", "README.md", r"has not taken after (\d+) seconds"),
    ("code seconds", "docs/SPEC.md", r"removes an untaken paste code after (\d+) seconds"),
    ("cancel seconds", "docs/ACCOUNTS.md", r"a `<id>\.cancel` after (\d+) seconds"),
])
def test_each_documented_signin_limit_is_the_codes_whole_number(limit: str, path: str, pattern: str) -> None:
    text = " ".join((ROOT / path).read_text(encoding="utf-8").split())
    found = [int(number) for number in re.findall(rf"(?<![\d.]){pattern}", text)]

    assert len(found) == 1, (path, pattern, found)
    assert found[0] == SIGNIN_LIMITS[limit](), (path, pattern, found)


def test_the_decision_note_allowlist_is_exactly_the_codes_patterns() -> None:
    decision_line("account_signin._ALLOW")
    block = re.search(r"^```text\n(.*?)^```$", signin_note(), re.MULTILINE | re.DOTALL)
    assert block
    listed = set(block[1].splitlines())

    broker = {pattern.pattern for allow in account_signin._ALLOW.values()
              for pattern in (allow.url, allow.code) if pattern is not None}
    page = {*(pattern.pattern for pattern in server().SIGNIN_URLS.values()), server().SIGNIN_CODE.pattern}

    assert listed == broker
    assert listed == page


def test_the_decision_note_has_one_block_per_limit() -> None:
    note = signin_note()

    for field in ("- **Decision:**", "- **When:**", "- **Source:**"):
        assert note.count(field) == 4, field
    assert "decisions/1067-dashboard-signin.md" in signin_section()


def test_the_signin_section_names_every_broker_refusal_and_result() -> None:
    section = signin_section()
    refused = {code for (code,) in refusals(ROOT / "lib" / "signin_broker.py", "Refused", None)}
    results = {*get_args(account_signin.Outcome.__annotations__["result"]), "refused"}

    assert refused
    assert sorted(code for code in refused | results if f"`{code}`" not in section) == []


SIGNIN_SERVER_SCOPES = frozenset({"_account_signin", "_signin_code", "_signin_cancel", "find", "use_code"})


def test_the_signin_section_names_every_server_refusal_of_a_signin() -> None:
    lines = signin_section().splitlines()
    found = refusals(ROOT / "deploy" / "factory-dashboard-http.py", "ApiError", SIGNIN_SERVER_SCOPES)

    assert found
    assert sorted((status, code) for status, code in found
                  if not any(line.startswith(f"| {status} |") and f"`{code}`" in line for line in lines)) == []


def test_the_signin_section_names_every_failed_attempt_error() -> None:
    section = signin_section()
    errors = {"shared-identity", "disturbed-account", "token-auth", "signed-out", "unreachable",
              "doctor-db", "no-doctor-row", *get_args(account_signin.End)} - {"exit", "expired", "cancelled"}

    assert sorted(error for error in errors if f"`{error}`" not in section) == []


@pytest.mark.parametrize("phrase", [
    "`dashboard.settings.relay`", "`DASHBOARD_SIGNIN_RELAY`", "`BindPaths=`", "install-autostart",
    "`CLAUDE_SECURESTORAGE_CONFIG_DIR`", COMMANDS[1], COMMANDS[2], "decisions/1067-dashboard-signin.md",
])
def test_the_signin_section_names_the_setup_and_the_fallback(phrase: str) -> None:
    assert phrase in signin_section()


def never_stored() -> str:
    return signin_section().partition("### What is never stored")[2].split("\n### ", 1)[0]


@pytest.mark.parametrize("store", [
    "log", "`settings-audit.jsonl`", "`status.json`", "`orch.db`", "`orch.health.db`", "job files",
])
def test_never_stored_names_each_factory_store(store: str) -> None:
    assert store in never_stored()


def test_never_stored_leaves_the_clis_own_credentials_out_of_scope() -> None:
    text = " ".join(never_stored().split())

    assert "Out of scope:" in text
    assert "`~/.factory-accounts/<id>`" in text
    assert "Keychain" in text


def test_the_cli_fallback_gives_the_command_every_card_shows() -> None:
    machines = MachinesConfig(
        devices=(Device(name="pc", slot_max=2,
                        harnesses={harness: f"{harness}-bot" for harness in ADD_HARNESSES},
                        accounts={harness: f"{harness}-own" for harness in ADD_HARNESSES},
                        home="/home/{bot}"),),
        accounts=(*(Account(id=f"{harness}-extra", harness=harness, signed_in_on=("pc",))
                    for harness in ADD_HARNESSES if harness != "cursor"),
                  Account(id="cursor-own", harness="cursor")),
    )
    with sqlite3.connect(":memory:") as connection:
        rows = [(account["harness"], row["command"])
                for account in read_accounts(connection, 0, machines)
                for row in account["signed_in_on"]]
    section = doc().partition("### The CLI fallback")[2].split("\n## ", 1)[0]

    assert {harness for harness, _ in rows} == set(ADD_HARNESSES)
    assert [harness for harness, command in rows if command is None] == []
    assert [command for harness, command in rows
            if harness == "cursor" and command not in section] == []


DROP_CLAIMS = (
    "A drop starts when a doctor pass reads a pair `signed-out` for a known reason.",
    "The drop ends when a pass reads the pair `ok`, or when the pair leaves `machines.yml`.",
    "The poller wakes Grok once per drop.",
    "Grok asks Max once to sign in from the dashboard Settings page.",
    "Neither the doctor nor the wake runs a login or copies one between devices.",
    "A sign-out from Settings does not wake.",
    "`orch accounts doctor`, `orch usage refresh`, the sign-in broker, Settings Prepare and Settings Run doctor "
    "record drops but queue no wake. The poller's next doctor pass announces them.",
    "A wake that is not delivered within 24 hours fails.",
)


def slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def test_the_doc_says_what_happens_when_a_sign_in_drops(tmp_path: Path) -> None:
    job_queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: NOW)
    config = poller_config(tmp_path)
    transport = DoctorTransport()
    sign_out_claude_b(transport)
    with contextlib.closing(job_queue.connect()) as connection:
        account_doctor.run(connection, config, transport, NOW, "Owner/Factory")
    [job] = announce(job_queue, config, "Owner/Factory")
    anchor = json.loads(job.request.detail or "")["link"].partition("#")[2]
    headings = [line for line in doc().splitlines()
                if line.startswith("#") and slug(line.lstrip("#").strip()) == anchor]
    section = " ".join(doc().partition("\n## When a sign-in drops\n")[2].split("\n## ", 1)[0].split())

    assert headings == ["## When a sign-in drops"]
    assert [claim for claim in DROP_CLAIMS if claim not in section] == []
    assert "lib/signin_wake.py" in (device_monitor.__doc__ or "")
