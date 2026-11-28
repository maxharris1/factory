from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple, get_args

import pytest

from lib import account_doctor, usage_refresh
from lib.account_doctor import Probe, SignIn
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.dispatch.types import GrokDelivery
from lib.dispatcher import Dispatcher
from lib.machines_yml import MachinesConfig
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, Exchanged
from tests.test_dispatcher import TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions
from tests.test_remote_start import config_of, enqueue, poll_at, poll_errors, stamp, state

NOW = 1_800_000_000.0
ORG = "4b0c6c5e-0000-4000-8000-00000000a001"
EMAIL = "bot-one@example.com"


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:12]


def claude_reply(
    *,
    logged_in: object = True,
    method: str = "claude.ai",
    subscription: str | None = "max",
    org: str | None = ORG,
    email: str | None = EMAIL,
    **extra: object,
) -> bytes:
    data: dict[str, object] = {
        "loggedIn": logged_in,
        "authMethod": method,
        "apiProvider": "firstParty",
        "configDirectory": "/home/claude-bot/.factory-accounts/acct",
    }
    if subscription is not None:
        data["subscriptionType"] = subscription
    if org is not None:
        data["orgId"] = org
    if email is not None:
        data["email"] = email
    data.update(extra)
    return json.dumps(data, indent=2).encode()


SIGNED_OUT_CLAUDE = json.dumps(
    {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty", "loginExpiresAt": None}
).encode()

CURSOR_USER_ID = 271828
CURSOR_SIGNED_IN = json.dumps({
    "status": "authenticated", "isAuthenticated": True,
    "userInfo": {"email": EMAIL, "userId": CURSOR_USER_ID, "name": "Bot One"},
}).encode()
CURSOR_NO_USER_DETAILS = json.dumps({
    "status": "authenticated", "isAuthenticated": True, "message": "Logged in (unable to fetch user details)",
}).encode()
CURSOR_PARTIAL = json.dumps({
    "status": "partially-authenticated", "isAuthenticated": False, "userInfo": {"email": EMAIL},
}).encode()
CURSOR_SIGNED_OUT = json.dumps({"status": "unauthenticated", "isAuthenticated": False}).encode()
CURSOR_ERROR = json.dumps({"status": "error", "message": "could not reach api2.cursor.sh"}).encode()


def cursor_signed_in_as(user_id: object) -> bytes:
    reply = json.loads(CURSOR_SIGNED_IN)
    reply["userInfo"]["userId"] = user_id
    return json.dumps(reply).encode()


def claude_probe(account: str = "acct", device: str = "box", code: int = 0, reply: bytes | None = None) -> Probe:
    return account_doctor.parse_claude(account, device, code, claude_reply() if reply is None else reply)


def codex_probe(reply: bytes, code: int = 0, account: str = "acct", device: str = "box") -> Probe:
    return account_doctor.parse_codex(account, device, code, reply)


def judged(*probes: Probe) -> dict[tuple[str, str], SignIn]:
    return {(row.account, row.device): row for row in account_doctor.judge(probes, NOW)}


def test_claude_folder_sign_in_reads_ok_with_its_fields() -> None:
    row = judged(claude_probe())[("acct", "box")]
    assert (row.status, row.auth_method, row.subscription_type) == ("ok", "claude.ai", "max")
    assert row.fingerprint == fingerprint(ORG, EMAIL)
    assert row.checked_at == NOW


@pytest.mark.parametrize("code, reply", [
    (1, SIGNED_OUT_CLAUDE),
    (0, SIGNED_OUT_CLAUDE),
    (1, claude_reply()),
    (0, b"not json at all"),
    (0, b"[1, 2]"),
    (0, b""),
    (0, claude_reply(org=None)),
    (0, claude_reply(email=None)),
    (0, claude_reply(org="")),
    (0, claude_reply(logged_in="yes")),
], ids=["logged-out", "logged-out-exit-0", "nonzero-exit", "not-json", "not-an-object",
        "empty", "no-org", "no-email", "empty-org", "loggedIn-not-bool"])
def test_claude_reply_that_is_not_a_folder_sign_in_reads_signed_out(code: int, reply: bytes) -> None:
    row = judged(claude_probe(code=code, reply=reply))[("acct", "box")]
    assert row.status == "signed-out"
    assert row.fingerprint is None
    assert row.detail


@pytest.mark.parametrize("method", ["oauth_token", "api_key", "auth_token", "apiKeyHelper"])
def test_claude_token_auth_method_reads_signed_out_never_ok(method: str) -> None:
    row = judged(claude_probe(reply=claude_reply(method=method)))[("acct", "box")]
    assert row.status == "signed-out"
    assert row.detail == "auth from a token variable, not the account folder"
    assert row.fingerprint is None


def test_claude_unknown_auth_method_reads_signed_out_and_is_not_stored_verbatim() -> None:
    row = judged(claude_probe(reply=claude_reply(method="some-new-method")))[("acct", "box")]
    assert row.status == "signed-out"
    assert row.auth_method == "unknown"


def test_codex_chatgpt_sign_in_reads_ok() -> None:
    row = judged(codex_probe(b"Logged in using ChatGPT\n"))[("acct", "box")]
    assert (row.status, row.auth_method, row.subscription_type, row.fingerprint) == ("ok", "chatgpt", None, None)


@pytest.mark.parametrize("code, reply", [
    (1, b"Not logged in\n"),
    (0, b"Not logged in\n"),
    (1, b"Logged in using ChatGPT\n"),
    (0, b""),
    (0, b"something else entirely\n"),
], ids=["not-logged-in", "not-logged-in-exit-0", "nonzero-exit", "empty", "not-understood"])
def test_codex_reply_that_is_not_a_chatgpt_sign_in_reads_signed_out(code: int, reply: bytes) -> None:
    row = judged(codex_probe(reply, code=code))[("acct", "box")]
    assert row.status == "signed-out"
    assert row.detail


def test_codex_api_key_login_reads_signed_out_never_ok() -> None:
    row = judged(codex_probe(b"Logged in using an API key - sk-proj-***ABCD\n"))[("acct", "box")]
    assert (row.status, row.auth_method) == ("signed-out", "api_key")
    assert row.detail == "api key, not a ChatGPT sign-in"


def test_not_reached_probe_reads_unreachable() -> None:
    probe = Probe(account="acct", device="mac", harness="claude", reached=False, logged_in=False,
                  auth_method=None, subscription_type=None, fingerprint=None,
                  detail="no ssh_alias for claude on mac")
    row = judged(probe)[("acct", "mac")]
    assert (row.status, row.detail) == ("unreachable", "no ssh_alias for claude on mac")


def test_two_account_ids_with_one_fingerprint_both_read_mismatch() -> None:
    rows = judged(claude_probe(account="one"), claude_probe(account="two"))
    assert rows[("one", "box")].status == rows[("two", "box")].status == "mismatch"
    assert "two" in rows[("one", "box")].detail
    assert "one" in rows[("two", "box")].detail
    assert rows[("one", "box")].fingerprint == fingerprint(ORG, EMAIL)


def test_one_account_with_two_fingerprints_reads_mismatch_on_both_devices() -> None:
    other = claude_reply(email="bot-two@example.com")
    rows = judged(claude_probe(device="box"), claude_probe(device="mac", reply=other))
    assert rows[("acct", "box")].status == rows[("acct", "mac")].status == "mismatch"
    assert "mac" in rows[("acct", "box")].detail
    assert "box" in rows[("acct", "mac")].detail


def test_same_account_same_fingerprint_on_two_devices_reads_ok() -> None:
    rows = judged(claude_probe(device="box"), claude_probe(device="mac"))
    assert {row.status for row in rows.values()} == {"ok"}


def test_signed_out_row_never_joins_a_mismatch() -> None:
    token = claude_reply(method="oauth_token")
    rows = judged(claude_probe(account="one"), claude_probe(account="two", reply=token))
    assert rows[("one", "box")].status == "ok"
    assert rows[("two", "box")].status == "signed-out"


def test_unreachable_row_never_joins_a_mismatch() -> None:
    away = Probe(account="acct", device="mac", harness="claude", reached=False, logged_in=False,
                 auth_method=None, subscription_type=None, fingerprint=fingerprint("x", "y"), detail="unreachable")
    rows = judged(claude_probe(device="box"), away)
    assert (rows[("acct", "box")].status, rows[("acct", "mac")].status) == ("ok", "unreachable")


def test_codex_never_mismatches() -> None:
    rows = judged(codex_probe(b"Logged in using ChatGPT\n", account="one"),
                  codex_probe(b"Logged in using ChatGPT\n", account="two"))
    assert {row.status for row in rows.values()} == {"ok"}


def test_codex_and_claude_rows_of_one_account_do_not_cross_mismatch() -> None:
    rows = account_doctor.judge(
        [claude_probe(device="box"), codex_probe(b"Logged in using ChatGPT\n", device="mac")], NOW
    )
    assert {row.status for row in rows} == {"ok"}


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def attention_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    if connection.execute("SELECT name FROM sqlite_master WHERE name = 'needs_attention'").fetchone() is None:
        return []
    return [tuple(row) for row in connection.execute(
        "SELECT repo, ticket_id, reason, detail, raised_at FROM needs_attention "
        "ORDER BY repo, ticket_id, reason"
    )]


def stored(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return connection.execute(
        "SELECT account, device, status, auth_method, subscription_type, fingerprint, checked_at, detail "
        "FROM account_signins ORDER BY account, device"
    ).fetchall()


def test_ensure_table_has_the_listed_columns_and_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    account_doctor.ensure_table(connection)
    account_doctor.ensure_table(connection)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(account_signins)")]
    assert columns == ["account", "device", "status", "auth_method", "subscription_type",
                       "fingerprint", "checked_at", "detail"]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO account_signins (account, device, status, checked_at) VALUES ('a', 'b', 'fine', 0)"
        )


def test_write_replaces_every_row(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    first = account_doctor.judge([claude_probe(account="gone"), claude_probe(account="kept", device="mac",
                                  reply=claude_reply(email="kept@example.com"))], NOW)
    config = leaky_config(tmp_path, [claude_probe(account="gone"), claude_probe(account="kept")])
    account_doctor.write(connection, first, config, None, NOW)
    assert [row[:3] for row in stored(connection)] == [("gone", "box", "ok"), ("kept", "mac", "ok")]

    second = account_doctor.judge([claude_probe(account="kept", device="mac", code=1, reply=SIGNED_OUT_CLAUDE)],
                                  NOW + 60)
    account_doctor.write(connection, second, config, None, NOW + 60)
    assert [row[:3] + (row[6],) for row in stored(connection)] == [("kept", "mac", "signed-out", NOW + 60)]
    assert not connection.in_transaction


def test_write_rolls_back_whole_on_a_bad_row(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    config = leaky_config(tmp_path, [claude_probe()])
    out = account_doctor.judge([claude_probe(code=1, reply=SIGNED_OUT_CLAUDE)], NOW)
    account_doctor.write(connection, out, config, "owner/factory", NOW)
    raised = attention_rows(connection)
    assert [row[:3] for row in raised] == [("owner/factory", "acct@box", "account-signin")]
    good = account_doctor.judge([claude_probe()], NOW + 60)
    bad = replace(good[0], status="fine")
    with pytest.raises(sqlite3.IntegrityError):
        account_doctor.write(connection, [good[0], bad], config, "owner/factory", NOW + 60)
    assert [row[:3] for row in stored(connection)] == [("acct", "box", "signed-out")]
    assert attention_rows(connection) == raised
    assert holds(connection) == [("acct", "box", NOW, "doctor", account_doctor.NOT_LOGGED_IN)]
    assert not connection.in_transaction


CANARY_EMAIL = "leak.canary@example.com"
CANARY_ORG = "org-canary-7f3a"
CANARY_TOKEN = "sk-ant-oat01-CANARYCANARYCANARY"
CANARY_EXTRA = "extra-canary-91c2"
CANARIES = (CANARY_EMAIL, CANARY_ORG, CANARY_TOKEN, CANARY_EXTRA)


def leaky_probes() -> list[Probe]:
    planted = {"email": CANARY_EMAIL, "org": CANARY_ORG, "oauthAccessToken": CANARY_TOKEN, "unknownKey": CANARY_EXTRA}
    every = f"{CANARY_EMAIL} {CANARY_ORG} {CANARY_TOKEN} {CANARY_EXTRA}"
    return [
        account_doctor.parse_claude("seen", "box", 0, claude_reply(**planted)),
        account_doctor.parse_claude("token", "box", 0, claude_reply(method=CANARY_TOKEN, **planted)),
        account_doctor.parse_claude("sub", "box", 0, claude_reply(subscription=CANARY_TOKEN, **planted)),
        account_doctor.parse_claude("default", "box", 0, claude_reply(method="oauth_token", **planted)),
        account_doctor.parse_claude("out", "box", 1, claude_reply(logged_in=False, method="none", **planted)),
        account_doctor.parse_claude("broken", "box", 0, ('{"email": "%s", "orgId": "%s", "token": "%s", "x": "%s"'
                                                        % (CANARY_EMAIL, CANARY_ORG, CANARY_TOKEN, CANARY_EXTRA)).encode()),
        account_doctor.parse_claude("binary", "box", 0, b"\xff\xfe" + every.encode()),
        account_doctor.parse_codex("chat", "box", 0, f"Logged in using ChatGPT {every}\n".encode()),
        account_doctor.parse_codex("key", "box", 0, f"Logged in using an API key - {CANARY_TOKEN}\n{every}\n".encode()),
        account_doctor.parse_codex("none", "box", 1, f"Not logged in {every}\n".encode()),
        account_doctor.parse_codex("junk", "box", 0, every.encode()),
    ]


LEAKY_MACHINES = """\
accounts:
{accounts}
devices:
  - name: box
    local: true
    slot_max: 1
    harnesses: {{claude: claude-bot, codex: codex-bot}}
    home: /home/{{bot}}
"""


def leaky_config(tmp_path: Path, probes: list[Probe]) -> MachinesConfig:
    path = tmp_path / "leaky-machines.yml"
    path.write_text(LEAKY_MACHINES.format(accounts="\n".join(
        f"  - {{id: {probe.account}, harness: {probe.harness}, signed_in_on: [box]}}" for probe in probes
    )), encoding="utf-8")
    return config_of(path)


def assert_no_canary_reaches_a_surface(
    tmp_path: Path, probes: list[Probe], config: MachinesConfig
) -> dict[tuple[str, str], tuple[object, ...]]:
    rows = account_doctor.judge(probes, NOW)
    flagged = account_doctor.flags(rows, {}, config, "owner/factory")
    database = tmp_path / "orch.db"
    connection = connect(database)
    account_doctor.write(connection, rows, config, "owner/factory", NOW)
    stored_rows = stored(connection)
    raised = attention_rows(connection)
    assert len(raised) == len([row for row in rows if row.status != "ok"])
    assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    connection.close()

    surfaces = {
        "probes": repr(probes).encode(),
        "rows": repr(rows).encode(),
        "details": "\n".join(row.detail for row in rows).encode(),
        "stored": repr(stored_rows).encode(),
        "flags": repr(flagged).encode(),
        "needs_attention": repr(raised).encode(),
        "orch.db": database.read_bytes(),
        "orch.db-wal": (tmp_path / "orch.db-wal").read_bytes() if (tmp_path / "orch.db-wal").exists() else b"",
    }
    for name, data in surfaces.items():
        for canary in CANARIES:
            assert canary.encode() not in data, f"{canary} reached {name}"
    return {(str(row[0]), str(row[1])): tuple(row) for row in stored_rows}


def test_canaries_reach_neither_rows_details_nor_database(tmp_path: Path) -> None:
    probes = leaky_probes()
    stored_rows = assert_no_canary_reaches_a_surface(tmp_path, probes, leaky_config(tmp_path, probes))
    assert stored_rows[("seen", "box")][5] == fingerprint(CANARY_ORG, CANARY_EMAIL)
    assert sum(row[2] != "ok" for row in stored_rows.values()) > 0


def cursor_canary(reply: bytes) -> bytes:
    data = json.loads(reply)
    planted = {"email": CANARY_EMAIL, "accessToken": CANARY_TOKEN, "unknownKey": CANARY_EXTRA, "org": CANARY_ORG}
    data["userInfo"] = {**data.get("userInfo", {}), **planted}
    if "message" in data:
        data["message"] = f"{data['message']} {CANARY_EMAIL} {CANARY_TOKEN}"
    return json.dumps(data).encode()


@pytest.mark.parametrize("code, reply, status", [
    (0, cursor_canary(CURSOR_SIGNED_IN), "ok"),
    (0, cursor_canary(CURSOR_NO_USER_DETAILS), "ok"),
    (0, cursor_canary(CURSOR_PARTIAL), "signed-out"),
    (0, cursor_canary(CURSOR_SIGNED_OUT), "signed-out"),
    (1, cursor_canary(CURSOR_ERROR), "signed-out"),
    (0, cursor_canary(CURSOR_SIGNED_IN)[:-2], "signed-out"),
    (0, b"\xff\xfe" + " ".join(CANARIES).encode(), "signed-out"),
], ids=["signed-in", "no-user-details", "partial", "signed-out", "error", "broken-json", "binary"])
def test_cursor_row_never_holds_the_email(tmp_path: Path, code: int, reply: bytes, status: str) -> None:
    probe = account_doctor.parse_cursor("cursor-default", "pc", code, reply)
    stored_rows = assert_no_canary_reaches_a_surface(tmp_path, [probe], cursor_config(tmp_path))
    assert stored_rows[("cursor-default", "pc")][2] == status


ORG_B = "4b0c6c5e-0000-4000-8000-00000000b002"
EMAIL_B = "bot-two@example.com"
TOKENS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CURSOR_API_KEY",
    "CURSOR_AUTH_TOKEN",
)

WORLD_MACHINES = """\
orch_host: box
accounts:
{accounts}
devices:
  - name: box
    local: true
    slot_max: 1
    harnesses: {{claude: claude-bot}}
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {{claude: claude-bot, codex: codex-bot, cursor: cursor-bot}}
    workdir_root: /home/{{bot}}/work
    ssh_alias: pc-{{bot}}
    factory_root: /srv/factory
    home: {home}
  - name: lap
    tailscale_name: lap.example.ts.net
    slot_max: 1
    enabled: false
    harnesses: {{claude: claude-bot, codex: codex-bot, cursor: cursor-bot}}
    workdir_root: /home/{{bot}}/work
    ssh_alias: lap-{{bot}}
    factory_root: /srv/factory
    home: {home}
"""
WORLD_ACCOUNTS = {
    "claude-b": "  - {id: claude-b, harness: claude, signed_in_on: [pc]}",
    "claude-c": "  - {id: claude-c, harness: claude, signed_in_on: [pc]}",
    "codex-b": "  - {id: codex-b, harness: codex, signed_in_on: [pc]}",
}

FAKE_HARNESS = """\
#!/bin/sh
printf '%s %s\\n' {name} "$(env | cut -d= -f1 | tr '\\n' ' ')" >> {log}
folder=${{{home_variable}:-default}}
reply=${{folder##*/}}
if [ -n "{token_test}" ]; then reply=token; fi
cat "{replies}/$reply" {stream}
exit "$(cat "{replies}/$reply.code")"
"""
PROGRAMS = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}
STREAMS = {"claude": "", "codex": ">&2", "cursor": ""}
TOKEN_TESTS = {
    "claude": "$CLAUDE_CODE_OAUTH_TOKEN$ANTHROPIC_API_KEY$ANTHROPIC_AUTH_TOKEN",
    "codex": "$OPENAI_API_KEY$CODEX_API_KEY",
    "cursor": "$CURSOR_API_KEY$CURSOR_AUTH_TOKEN",
}
HOME_VARIABLES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "cursor": "CURSOR_CONFIG_DIR"}
REPLIES = {
    "claude": {
        "default": (claude_reply(), 0),
        "claude-b": (claude_reply(org=ORG_B, email=EMAIL_B, subscription="pro"), 0),
        "claude-c": (SIGNED_OUT_CLAUDE, 1),
        "token": (claude_reply(method="oauth_token"), 0),
    },
    "codex": {
        "default": (b"Logged in using ChatGPT\n", 0),
        "codex-b": (b"Not logged in\n", 1),
        "token": (b"Logged in using an API key - sk-proj-***\n", 0),
    },
    "cursor": {
        "default": (CURSOR_SIGNED_IN, 0),
        "token": (cursor_signed_in_as("user_from_a_token"), 0),
    },
}
PROFILE = """\
export PATH={bin}:$PATH
export CLAUDE_CONFIG_DIR={home}/.factory-accounts/claude-c CODEX_HOME={home}/.factory-accounts/codex-b
export CURSOR_CONFIG_DIR={home}/.cursor-elsewhere
{tokens}
echo profile says hello
printf 'no newline at the end'
"""


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from tests.test_remote_start import Ssh

        self.tmp = tmp_path
        self.ssh = Ssh(tmp_path)
        for bot in ("claude-bot", "codex-bot", "cursor-bot"):
            for device in ("pc", "lap"):
                (self.ssh.hosts / f"{device}-{bot}").write_text(bot, encoding="utf-8")
            self.ssh.offline(f"lap-{bot}").touch()
        self.device_home = tmp_path / "device-home"
        self.device_home.mkdir()
        self.bin = tmp_path / "device-bin"
        self.bin.mkdir()
        self.env_log = tmp_path / "harness-env.log"
        for harness, replies in REPLIES.items():
            folder = tmp_path / "replies" / harness
            folder.mkdir(parents=True)
            for name, (reply, code) in replies.items():
                (folder / name).write_bytes(reply)
                (folder / f"{name}.code").write_text(str(code), encoding="utf-8")
            fake = self.bin / PROGRAMS[harness]
            fake.write_text(FAKE_HARNESS.format(
                name=harness, log=self.env_log, token_test=TOKEN_TESTS[harness],
                replies=folder, home_variable=HOME_VARIABLES[harness], stream=STREAMS[harness],
            ), encoding="utf-8")
            fake.chmod(0o755)
        self.profile()
        self.machines = tmp_path / "machines.yml"
        self.declare(*WORLD_ACCOUNTS)
        self.db = tmp_path / "orch.db"
        for name, value in self.ssh.env().items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("FAKE_SSH_HOME", str(self.device_home))
        monkeypatch.setenv("ORCH_SSH_MUX", "off")

    def profile(self, extra: str = "") -> None:
        tokens = "\n".join(f"export {name}=sk-planted-{name.lower()}" for name in TOKENS)
        (self.device_home / ".bash_profile").write_text(
            PROFILE.format(bin=self.bin, home=self.device_home, tokens=tokens) + extra, encoding="utf-8"
        )

    def declare(self, *accounts: str) -> None:
        self.machines.write_text(WORLD_MACHINES.format(
            accounts="\n".join(WORLD_ACCOUNTS[name] for name in accounts),
            home=self.device_home,
        ), encoding="utf-8")

    def doctor(self, *extra: str) -> int:
        from lib import cli

        return cli.main(["accounts", "doctor", "--db", str(self.db), "--machines", str(self.machines), *extra])

    def rows(self) -> dict[tuple[str, str], SignIn]:
        with sqlite3.connect(self.db) as connection:
            found = connection.execute(f"SELECT * FROM {account_doctor.TABLE}").fetchall()
        return {(row[0], row[1]): SignIn(*row) for row in found}

    def statuses(self) -> dict[tuple[str, str], str]:
        return {key: row.status for key, row in self.rows().items()}

    def harness_envs(self) -> list[tuple[str, set[str]]]:
        lines = self.env_log.read_text(encoding="utf-8").splitlines()
        return [(line.split(" ", 1)[0], set(line.split()[1:])) for line in lines]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


EXPECTED = {
    ("claude-default", "box"): "unreachable",
    ("claude-default", "pc"): "ok",
    ("claude-b", "pc"): "ok",
    ("claude-c", "pc"): "signed-out",
    ("codex-default", "pc"): "ok",
    ("codex-b", "pc"): "signed-out",
    ("claude-default", "lap"): "unreachable",
    ("codex-default", "lap"): "unreachable",
    ("cursor-default", "pc"): "ok",
    ("cursor-default", "lap"): "unreachable",
}
PROBED = ["claude"] * 3 + ["codex"] * 2 + ["cursor"]


def test_doctor_reads_ok_and_signed_out_end_to_end_for_both_harnesses(world: World) -> None:
    assert world.doctor() == 0
    assert world.statuses() == EXPECTED
    rows = world.rows()
    claude = rows[("claude-default", "pc")]
    assert (claude.auth_method, claude.subscription_type, claude.fingerprint) == ("claude.ai", "max", fingerprint(ORG, EMAIL))
    second = rows[("claude-b", "pc")]
    assert (second.subscription_type, second.fingerprint) == ("pro", fingerprint(ORG_B, EMAIL_B))
    assert rows[("claude-c", "pc")].detail == account_doctor.NOT_LOGGED_IN
    assert rows[("codex-default", "pc")].auth_method == "chatgpt"
    assert rows[("codex-b", "pc")].detail == account_doctor.NOT_LOGGED_IN
    cursor = rows[("cursor-default", "pc")]
    assert (cursor.auth_method, cursor.fingerprint) == ("login", fingerprint("cursor", str(CURSOR_USER_ID)))


def test_token_variables_from_the_login_profile_never_reach_the_harness(world: World) -> None:
    assert world.doctor() == 0
    envs = world.harness_envs()
    assert sorted(name for name, _ in envs) == PROBED
    for _, names in envs:
        assert not names & set(TOKENS)
    assert world.statuses() == EXPECTED


def test_cursor_probe_unsets_token_variables(world: World) -> None:
    world.profile("export CURSOR_API_KEY=sk-planted-cursor CURSOR_AUTH_TOKEN=sk-planted-cursor-auth\n")
    assert world.doctor() == 0
    (names,) = [names for name, names in world.harness_envs() if name == "cursor"]
    assert not names & {"CURSOR_API_KEY", "CURSOR_AUTH_TOKEN", "CURSOR_CONFIG_DIR"}
    row = world.rows()[("cursor-default", "pc")]
    assert (row.status, row.fingerprint) == ("ok", fingerprint("cursor", str(CURSOR_USER_ID)))
    (call,) = [call for call in world.ssh.calls() if call["alias"] == "pc-cursor-bot"]
    assert "unset " in call["script"]
    unset = call["script"].split("unset ", 1)[1].split(";", 1)[0].split()
    assert {"CURSOR_API_KEY", "CURSOR_AUTH_TOKEN", "CURSOR_CONFIG_DIR"} <= set(unset)


@pytest.mark.parametrize("errexit", [
    "set -e\n",
    'claude() { set -e; command claude "$@"; }\ncodex() { set -e; command codex "$@"; }\n',
    'claude() { command claude "$@" || exit; }\ncodex() { command codex "$@" || exit; }\n',
    'claude() { exec claude "$@"; }\ncodex() { exec codex "$@"; }\n',
], ids=["profile", "function", "exit", "exec"])
def test_errexit_from_the_login_profile_neither_skips_nor_loses_a_pair(world: World, errexit: str) -> None:
    world.profile(errexit)
    assert world.doctor() == 0
    assert sorted(name for name, _ in world.harness_envs()) == PROBED
    assert world.statuses() == EXPECTED


def test_profile_function_that_exports_a_token_at_call_time_reads_signed_out(world: World) -> None:
    world.profile(
        'claude() { export CLAUDE_CODE_OAUTH_TOKEN=x; command claude "$@"; }\n'
        'codex() { export OPENAI_API_KEY=x; command codex "$@"; }\n'
    )
    assert world.doctor() == 0
    rows = world.rows()
    for account in ("claude-default", "claude-b", "claude-c"):
        assert (rows[(account, "pc")].status, rows[(account, "pc")].detail) == ("signed-out", account_doctor.TOKEN_AUTH)
    for account in ("codex-default", "codex-b"):
        assert (rows[(account, "pc")].status, rows[(account, "pc")].detail) == ("signed-out", account_doctor.CODEX_API_KEY)


def test_offline_device_and_device_without_ssh_alias_read_unreachable(world: World) -> None:
    assert world.doctor() == 0
    rows = world.rows()
    assert rows[("claude-default", "box")].detail == "no ssh_alias for claude on box"
    for account in ("claude-default", "codex-default", "cursor-default"):
        assert (rows[(account, "lap")].status, rows[(account, "lap")].detail) == ("unreachable", "device unreachable over ssh")
    calls = world.ssh.calls()
    assert sorted(call["alias"] for call in calls) == [
        "lap-claude-bot", "lap-codex-bot", "lap-cursor-bot", "pc-claude-bot", "pc-codex-bot", "pc-cursor-bot"]
    assert all(call["script"].startswith("exec bash -lc ") for call in calls)


def test_removed_account_loses_its_row_on_the_next_pass(world: World) -> None:
    assert world.doctor() == 0
    world.declare("claude-c", "codex-b")
    assert world.doctor() == 0
    expected = {key: status for key, status in EXPECTED.items() if key[0] != "claude-b"}
    assert world.statuses() == expected


def test_doctor_json_prints_every_stored_row(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert world.doctor("--json") == 0
    printed = json.loads(capsys.readouterr().out)
    fields = list(SignIn.__dataclass_fields__)
    assert all(list(item) == fields for item in printed)
    assert {(item["account"], item["device"]): SignIn(**item) for item in printed} == world.rows()


def test_doctor_prints_one_line_per_row(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert world.doctor() == 0
    lines = capsys.readouterr().out.splitlines()
    assert "claude-default@pc ok claude.ai" in lines
    assert "claude-default@box unreachable - no ssh_alias for claude on box" in lines
    assert len(lines) == len(EXPECTED)


def test_canaries_from_every_device_reply_reach_no_sink_end_to_end(
    world: World, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    every = " ".join(CANARIES)
    planted = {"orgId": CANARY_ORG, "email": CANARY_EMAIL, "oauthAccessToken": CANARY_TOKEN, "unknownKey": CANARY_EXTRA}
    replies = tmp_path / "replies"
    (replies / "claude" / "default").write_bytes(
        claude_reply(org=CANARY_ORG, email=CANARY_EMAIL, oauthAccessToken=CANARY_TOKEN, unknownKey=CANARY_EXTRA))
    (replies / "claude" / "claude-b").write_bytes(claude_reply(**planted)[:-1])
    for name in ("claude-c", "token"):
        reply = json.loads(REPLIES["claude"][name][0])
        (replies / "claude" / name).write_bytes(json.dumps({**reply, **planted}).encode())
    for name, (reply, _) in REPLIES["codex"].items():
        (replies / "codex" / name).write_bytes(reply + every.encode() + b"\n")
    for name, (reply, _) in REPLIES["cursor"].items():
        (replies / "cursor" / name).write_bytes(cursor_canary(reply))
    world.profile(
        f"echo '{every}'\necho '{every}' >&2\n"
        f"claude() {{ echo '{every}' >&2; command claude \"$@\"; }}\n"
        f"codex() {{ command codex \"$@\"; code=$?; echo '{every}' >&2; return $code; }}\n"
    )
    SQLiteJobQueue(world.db, clock=lambda: NOW).enqueue(JobRequest(
        repo="maxharris1/factory", ticket_id="1123", action="review", recipient="codex",
        source_comment_id="c1", timeout_seconds=600, detail='{"note": "seeded"}',
    ), dedupe_key="k1")
    wal_holder = sqlite3.connect(world.db)
    wal_holder.execute("SELECT count(*) FROM jobs").fetchone()
    from lib import cli

    caplog.set_level(logging.DEBUG)
    assert world.doctor() == 0
    assert world.doctor("--json") == 0
    wal = tmp_path / "orch.db-wal"
    wal_before = wal.read_bytes()
    out = tmp_path / "site"
    assert cli.main(["dashboard", "snapshot", "--db", str(world.db), "--log", str(tmp_path / "poller.log"),
                     "--out", str(out), "--machines", str(world.machines)]) == 0
    printed = capsys.readouterr()

    rows = world.rows()
    expected = fingerprint(CANARY_ORG, CANARY_EMAIL)
    assert (rows[("claude-default", "pc")].status, rows[("claude-default", "pc")].fingerprint) == ("ok", expected)
    assert rows[("claude-b", "pc")].status == "signed-out"
    assert (rows[("cursor-default", "pc")].status, rows[("cursor-default", "pc")].fingerprint) == (
        "ok", fingerprint("cursor", str(CURSOR_USER_ID)))

    assert wal_holder.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    details = [row[0] for row in wal_holder.execute("SELECT detail FROM jobs")]
    wal_holder.close()
    assert details, "a seeded job has a detail to check"
    sinks = {
        "orch.db": world.db.read_bytes(),
        "orch.db-wal before checkpoint": wal_before,
        "orch.db-wal after checkpoint": wal.read_bytes() if wal.exists() else b"",
        "stdout": printed.out.encode(),
        "stderr": printed.err.encode(),
        "status.json": (out / "status.json").read_bytes(),
        **{f"log record {i}": f"{r.getMessage()} {r.args!r}".encode() for i, r in enumerate(caplog.records)},
        **{f"jobs.detail {i}": (detail or "").encode() for i, detail in enumerate(details)},
    }
    for name, data in sinks.items():
        for canary in CANARIES:
            assert canary.encode() not in data, f"{canary} reached {name}"
    assert expected.encode() in sinks["orch.db"]


def framed(*frames: tuple[int, int, bytes], before: bytes = b"") -> bytes:
    body = b"".join(reply + b"\nORCH-ACCOUNT %d %d\n" % (index, code) for index, code, reply in frames)
    return before + b"\nORCH-ACCOUNTS-BEGIN\n" + body


def test_replies_drop_profile_output_and_split_frames() -> None:
    stdout = framed((0, 0, b'{"a": 1}'), (1, 3, b"Not logged in"), before=b"ORCH-ACCOUNT 0 9\nnoise")
    assert account_doctor._replies(stdout, 2) == {0: (0, b'{"a": 1}', b""), 1: (3, b"Not logged in", b"")}


def test_replies_ignore_a_forged_marker_inside_a_reply() -> None:
    stdout = framed((0, 0, b"first\nORCH-ACCOUNT 1 0\nstill first"), (1, 1, b"second"))
    assert account_doctor._replies(stdout, 2) == {0: (0, b"first\nORCH-ACCOUNT 1 0\nstill first", b""), 1: (1, b"second", b"")}


def test_replies_split_stderr_only_for_a_split_status() -> None:
    stdout = framed((0, 0, b"gemini-3.1-pro\nORCH-ACCOUNT-STDERR\nWarning"))
    assert account_doctor._replies(stdout, 1, split=True) == {0: (0, b"gemini-3.1-pro", b"Warning")}
    assert account_doctor._replies(stdout, 1) == {0: (0, b"gemini-3.1-pro\nORCH-ACCOUNT-STDERR\nWarning", b"")}


def test_replies_without_a_frame_leave_it_out() -> None:
    assert account_doctor._replies(framed((0, 0, b"only")), 2) == {0: (0, b"only", b"")}
    assert account_doctor._replies(b"no begin line\nORCH-ACCOUNT 0 0\n", 1) == {}


DOCTOR_MACHINES = """\
accounts:
  - {id: claude-b, harness: claude, signed_in_on: [pc, mac]}
devices:
  - name: box
    local: true
    enabled: false
    slot_max: 1
    harnesses: {}
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {claude: claude-bot, codex: codex-bot}
    workdir_root: /home/{bot}/work
    ssh_alias: pc-{bot}
    factory_root: /srv/factory
    home: /home/{bot}
"""
PLACEHOLDER_MAC = """\
  - name: mac
    tailscale_name: mac.example.ts.net
    slot_max: 1
    harnesses: {claude: claude-bot}
    workdir_root: /Users/{bot}/work
    ssh_alias: REPLACE_WITH_SSH_ALIAS
    factory_root: /srv/factory
    home: REPLACE_WITH_HOME
"""
POLLER_MACHINES = DOCTOR_MACHINES.replace("[pc, mac]", "[pc]")
CLAUDE_B_HOME = "/home/claude-bot/.factory-accounts/claude-b"
KEY = ("owner/factory", "claude-b@pc", "account-signin")


class DoctorTransport:
    def __init__(self) -> None:
        self.replies: dict[str, bytes | Exchanged] = {
            "pc-claude-bot": framed((0, 0, claude_reply()), (1, 0, claude_reply(org=ORG_B, email=EMAIL_B))),
            "pc-codex-bot": framed((0, 0, b"Logged in using ChatGPT\n")),
        }
        self.exchanges: list[str] = []
        self.reads: list[str] = []
        self.error: Exception | None = None

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        return 0

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        if "ORCH-ACCOUNTS-BEGIN" not in argv[-1]:
            self.reads.append(argv[-2])
            return Exchanged(0, b"", "")
        self.exchanges.append(argv[-2])
        if self.error is not None:
            raise self.error
        reply = self.replies[argv[-2]]
        return reply if isinstance(reply, Exchanged) else Exchanged(0, reply, "")


class Poller(NamedTuple):
    now: list[float]
    queue: SQLiteJobQueue
    pool: DevicePool
    transport: DoctorTransport
    dispatcher: Dispatcher
    machines: Path

    def poll(self, at: float) -> None:
        poll_at(self.now, self.dispatcher, at)

    def flags(self) -> list[tuple[object, ...]]:
        with contextlib.closing(self.queue.connect()) as connection:
            return attention_rows(connection)


def poller(tmp_path: Path, *, sign_ins: bool = True, grok_delivery: GrokDelivery | None = None) -> Poller:
    from lib import cli

    now = [0.0]
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: now[0])
    machines = tmp_path / "machines.yml"
    machines.write_text(POLLER_MACHINES, encoding="utf-8")
    config = config_of(machines)
    pool = DevicePool(queue, config)
    transport = DoctorTransport()
    check = functools.partial(usage_refresh.poll_pass, queue, transport, "Owner/Factory") if sign_ins else None
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=grok_delivery or FakeGrokDelivery(),
        devices=DeviceMonitor(pool, config, transport, alive=lambda *_: False,
                              machines_path=machines, sign_ins=check),
    )
    return Poller(now, queue, pool, transport, dispatcher, machines)


def sign_out_claude_b(transport: DoctorTransport) -> None:
    transport.replies["pc-claude-bot"] = framed((0, 0, claude_reply()), (1, 1, SIGNED_OUT_CLAUDE))


def test_poller_runs_the_doctor_on_the_first_poll_then_every_30_minutes(tmp_path: Path) -> None:
    world = poller(tmp_path)
    world.poll(0.0)
    assert sorted(world.transport.exchanges) == ["pc-claude-bot", "pc-codex-bot"]
    world.poll(600.0)
    assert len(world.transport.exchanges) == 2
    world.poll(1800.0)
    assert len(world.transport.exchanges) == 4


def test_poller_runs_the_doctor_once_after_machines_yml_changes(tmp_path: Path) -> None:
    world = poller(tmp_path)
    world.poll(0.0)
    world.machines.write_text(world.machines.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    stamp(world.machines, 10**18)
    world.poll(300.0)
    assert len(world.transport.exchanges) == 4
    world.poll(360.0)
    assert len(world.transport.exchanges) == 4


def test_doctor_error_is_recorded_and_the_poll_goes_on(tmp_path: Path) -> None:
    world = poller(tmp_path)
    world.transport.error = OSError("ssh blew up")
    job_id = enqueue(world.queue, "claude", "1", timeout=86_400)
    world.pool.claim("claude")
    world.poll(400.0)
    errors = poll_errors(world.queue)
    assert [repo for repo, _ in errors] == ["devices"]
    assert errors[0][1].startswith("account doctor:")
    assert state(world.queue, job_id) == "queued"
    probed = len(world.transport.exchanges)
    world.poll(500.0)
    assert len(world.transport.exchanges) == probed


def test_the_monitor_runs_the_doctor_alone_between_usage_passes(tmp_path: Path) -> None:
    world = poller(tmp_path)

    def polled(at: float) -> tuple[int, int]:
        world.poll(at)
        return len(world.transport.exchanges), len(world.transport.reads)

    assert polled(0.0) == (2, 3), "the first poll runs the whole pass"
    assert polled(60.0) == (2, 3), "a quiet poll runs nothing"
    preflight_hold(world.queue, 70.0)
    assert polled(80.0) == (4, 3), "a newer hold runs the doctor alone"
    preflight_hold(world.queue, 70.0)
    assert polled(80.0 + account_doctor.HELD_INTERVAL_SECONDS) == (6, 3), "so does 120 seconds under a hold"
    world.machines.write_text(world.machines.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    stamp(world.machines, 10**18)
    assert polled(300.0) == (8, 3), "and a machines config change"
    assert polled(float(usage_refresh.SLOT_SECONDS)) == (10, 6), "the next slot runs the whole pass"


def test_a_pass_error_is_recorded_and_the_poll_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(_stdout: bytes, _now: float) -> list[object]:
        raise RuntimeError("usage reply blew up")

    world = poller(tmp_path)
    sign_out_claude_b(world.transport)
    monkeypatch.setitem(usage_refresh.READERS, "claude", replace(usage_refresh.READERS["claude"], parse=unreadable))
    job_id = enqueue(world.queue, "claude", "1", timeout=86_400)
    world.pool.claim("claude")

    world.poll(400.0)
    errors = poll_errors(world.queue)
    assert [repo for repo, _ in errors] == ["devices"]
    assert "RuntimeError" in errors[0][1]
    assert state(world.queue, job_id) == "queued"
    assert (len(world.transport.exchanges), len(world.transport.reads)) == (2, 2)
    with contextlib.closing(world.queue.connect()) as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT ticket_id FROM jobs WHERE action = 'account-signin'")] == [("claude-b@pc",)]

    world.poll(460.0)
    assert (len(world.transport.exchanges), len(world.transport.reads)) == (2, 2)
    world.poll(float(usage_refresh.SLOT_SECONDS))
    assert (len(world.transport.exchanges), len(world.transport.reads)) == (4, 4)
    assert len(poll_errors(world.queue)) == 2


def test_a_signed_out_pair_raises_one_flag_naming_the_operator_step(tmp_path: Path) -> None:
    world = poller(tmp_path)
    sign_out_claude_b(world.transport)
    world.poll(0.0)
    (flag,) = world.flags()
    assert flag[:3] == KEY
    assert "claude auth login" in str(flag[3])
    assert f"CLAUDE_CONFIG_DIR={CLAUDE_B_HOME}" in str(flag[3])


def test_a_flag_keeps_its_raised_at_while_failing_and_clears_once_ok(tmp_path: Path) -> None:
    world = poller(tmp_path)
    sign_out_claude_b(world.transport)
    world.poll(0.0)
    world.poll(1800.0)
    assert len(world.transport.exchanges) == 4
    assert [(flag[:3], flag[4]) for flag in world.flags()] == [(KEY, 0.0)]
    world.transport.replies = DoctorTransport().replies
    world.poll(3600.0)
    assert world.flags() == []


def test_two_failing_pairs_raise_two_flags(tmp_path: Path) -> None:
    world = poller(tmp_path)
    sign_out_claude_b(world.transport)
    world.transport.replies["pc-codex-bot"] = framed((0, 1, b"Not logged in\n"))
    world.poll(0.0)
    assert [flag[1] for flag in world.flags()] == ["claude-b@pc", "codex-default@pc"]


def test_a_monitor_without_sign_ins_never_runs_the_doctor(tmp_path: Path) -> None:
    world = poller(tmp_path, sign_ins=False)
    world.poll(0.0)
    world.poll(1800.0)
    assert world.transport.exchanges == []
    with contextlib.closing(world.queue.connect()) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = ?", (account_doctor.TABLE,)
        ).fetchone() is None


CURSOR_MACHINES = """\
devices:
  - name: box
    local: true
    slot_max: 1
    harnesses: {cursor: cursor-bot}
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {cursor: cursor-bot}
    workdir_root: /home/{bot}/work
    ssh_alias: pc-{bot}
    factory_root: /srv/factory
  - name: lap
    tailscale_name: lap.example.ts.net
    slot_max: 1
    harnesses: {cursor: cursor-bot}
    workdir_root: /home/{bot}/work
    ssh_alias: lap-{bot}
    factory_root: /srv/factory
"""
CURSOR_LOGIN = "env -u CURSOR_API_KEY -u CURSOR_AUTH_TOKEN -u CURSOR_CONFIG_DIR cursor-agent login"
CURSOR_STEP = f"sign in as cursor-bot on pc: {CURSOR_LOGIN}, then orch accounts doctor"


def cursor_config(tmp_path: Path) -> MachinesConfig:
    path = tmp_path / "cursor-machines.yml"
    path.write_text(CURSOR_MACHINES, encoding="utf-8")
    return config_of(path)


def cursor_transport(pc: bytes | Exchanged, lap: bytes | Exchanged | None = None) -> DoctorTransport:
    transport = DoctorTransport()
    transport.replies = {"pc-cursor-bot": pc, "lap-cursor-bot": framed((0, 0, CURSOR_SIGNED_IN)) if lap is None else lap}
    return transport


def run_cursor(
    connection: sqlite3.Connection, tmp_path: Path, transport: DoctorTransport, now: float = NOW
) -> dict[tuple[str, str], SignIn]:
    rows = account_doctor.run(connection, cursor_config(tmp_path), transport, now, "Owner/Factory")
    return {(row.account, row.device): row for row in rows}


def cursor_flags(connection: sqlite3.Connection) -> dict[str, str]:
    return {str(row[1]): str(row[3]) for row in attention_rows(connection)}


@pytest.mark.parametrize("reply, expected", [
    (CURSOR_SIGNED_IN, fingerprint("cursor", str(CURSOR_USER_ID))),
    (cursor_signed_in_as("user_01J9ZK"), fingerprint("cursor", "user_01J9ZK")),
    (CURSOR_NO_USER_DETAILS, None),
    (cursor_signed_in_as(True), None),
], ids=["numeric-id", "text-id", "no-user-details", "bool-id"])
def test_cursor_sign_in_reads_ok_with_a_user_id_fingerprint(
    tmp_path: Path, reply: bytes, expected: str | None
) -> None:
    connection = connect(tmp_path / "orch.db")
    rows = run_cursor(connection, tmp_path, cursor_transport(framed((0, 0, reply)), framed((0, 0, reply))))
    for device in ("pc", "lap"):
        row = rows[("cursor-default", device)]
        assert (row.status, row.auth_method, row.subscription_type, row.fingerprint, row.detail) == (
            "ok", "login", None, expected, "")
    assert list(cursor_flags(connection)) == ["cursor-default@box"]
    assert holds(connection) == []


@pytest.mark.parametrize("reply, detail, held", [
    (CURSOR_PARTIAL, "partly signed in", False),
    (CURSOR_SIGNED_OUT, account_doctor.NOT_LOGGED_IN, True),
], ids=["partial", "signed-out"])
def test_cursor_partial_or_signed_out_reads_signed_out(tmp_path: Path, reply: bytes, detail: str, held: bool) -> None:
    connection = connect(tmp_path / "orch.db")
    run_cursor(connection, tmp_path, cursor_transport(framed((0, 0, reply))))
    assert [row for row in stored(connection) if row[1] == "pc"] == [
        ("cursor-default", "pc", "signed-out", None, None, None, NOW, detail)]
    assert cursor_flags(connection)["cursor-default@pc"] == f"signed-out; run: {CURSOR_STEP}; {detail}"
    assert holds(connection) == ([("cursor-default", "pc", NOW, "doctor", detail)] if held else [])


@pytest.mark.parametrize("code, reply, detail", [
    (1, CURSOR_ERROR, account_doctor.EXIT_NONZERO),
    (0, CURSOR_ERROR, account_doctor.EXIT_NONZERO),
    (1, CURSOR_SIGNED_IN, account_doctor.EXIT_NONZERO),
    (0, b"cursor-agent: command not found", account_doctor.NOT_UNDERSTOOD),
    (0, b'{"status": "rate-limited"}', account_doctor.NOT_UNDERSTOOD),
    (0, b'["authenticated"]', account_doctor.NOT_UNDERSTOOD),
    (0, b'{"status": ["authenticated"]}', account_doctor.NOT_UNDERSTOOD),
], ids=["error", "error-exit-0", "signed-in-exit-1", "not-json", "unknown-status", "not-an-object",
        "status-not-text"])
def test_cursor_status_error_is_inconclusive(tmp_path: Path, code: int, reply: bytes, detail: str) -> None:
    connection = connect(tmp_path / "orch.db")
    run_cursor(connection, tmp_path, cursor_transport(framed((0, 1, CURSOR_SIGNED_OUT))))
    rows = run_cursor(connection, tmp_path, cursor_transport(framed((0, code, reply))), NOW + 120)
    row = rows[("cursor-default", "pc")]
    assert (row.status, row.auth_method, row.fingerprint, row.detail) == ("signed-out", None, None, detail)
    assert holds(connection) == [("cursor-default", "pc", NOW, "doctor", account_doctor.NOT_LOGGED_IN)]
    assert cursor_flags(connection)["cursor-default@pc"].endswith("; not logged in")


@pytest.mark.parametrize("pc", [
    Exchanged(SSH_UNREACHABLE, b"", "ssh: connect to host pc port 22: Connection refused"),
    Exchanged(-9, b"", "", timed_out=True),
], ids=["ssh-unreachable", "timeout"])
def test_cursor_unreachable(tmp_path: Path, pc: Exchanged) -> None:
    connection = connect(tmp_path / "orch.db")
    rows = run_cursor(connection, tmp_path, cursor_transport(pc))
    assert (rows[("cursor-default", "pc")].status, rows[("cursor-default", "pc")].detail) == (
        "unreachable", account_doctor.SSH_DOWN)
    assert (rows[("cursor-default", "box")].status, rows[("cursor-default", "box")].detail) == (
        "unreachable", "no ssh_alias for cursor on box")
    assert rows[("cursor-default", "lap")].status == "ok"
    assert cursor_flags(connection)["cursor-default@pc"] == (
        "unreachable; run: fix ssh to pc for cursor, then orch accounts doctor; device unreachable over ssh")
    assert holds(connection) == []


def test_one_cursor_account_as_two_users_reads_mismatch(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    rows = run_cursor(connection, tmp_path, cursor_transport(
        framed((0, 0, CURSOR_SIGNED_IN)), framed((0, 0, cursor_signed_in_as("user_other")))))
    pc, lap = rows[("cursor-default", "pc")], rows[("cursor-default", "lap")]
    assert (pc.status, pc.detail) == ("mismatch", "another sign-in on device lap")
    assert (lap.status, lap.detail) == ("mismatch", "another sign-in on device pc")
    assert cursor_flags(connection)["cursor-default@pc"] == (
        f"mismatch; run: sign cursor-default in to its own account as cursor-bot on pc: {CURSOR_LOGIN}, "
        "then orch accounts doctor; another sign-in on device lap")


def test_doctor_tables_name_the_same_harnesses() -> None:
    harnesses = set(get_args(account_doctor.Harness))
    assert harnesses == {"claude", "codex", "cursor", "gemini"}
    for table in (account_doctor._STATUS, account_doctor._RULES, account_doctor._LOGIN, account_doctor.AUTH_METHODS):
        assert set(table) == harnesses


def flag_config(tmp_path: Path) -> MachinesConfig:
    path = tmp_path / "flag-machines.yml"
    path.write_text(DOCTOR_MACHINES + PLACEHOLDER_MAC, encoding="utf-8")
    return config_of(path)


def sign_in(account: str, status: str, detail: str, device: str = "pc") -> SignIn:
    return SignIn(account, device, status, None, None, None, NOW, detail)


@pytest.mark.parametrize("row, expected", [
    (sign_in("claude-b", "signed-out", account_doctor.NOT_LOGGED_IN),
     f"signed-out; run: sign in as claude-bot on pc: env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={CLAUDE_B_HOME} "
     "claude auth login, then orch accounts doctor; not logged in"),
    (sign_in("codex-default", "signed-out", account_doctor.NOT_LOGGED_IN),
     "signed-out; run: sign in as codex-bot on pc: env -u CODEX_HOME codex login --device-auth, "
     "then orch accounts doctor; not logged in"),
    (sign_in("claude-default", "signed-out", account_doctor.TOKEN_AUTH),
     "signed-out; run: remove the token export from claude-bot's login profile on pc, "
     f"then orch accounts doctor; {account_doctor.TOKEN_AUTH}"),
    (sign_in("claude-b", "mismatch", "same sign-in as account claude-default"),
     "mismatch; run: sign claude-b in to its own account as claude-bot on pc: "
     f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={CLAUDE_B_HOME} claude auth login, then orch accounts doctor; "
     "same sign-in as account claude-default"),
    (sign_in("codex-default", "unreachable", account_doctor.SSH_DOWN),
     "unreachable; run: fix ssh to pc for codex, then orch accounts doctor; device unreachable over ssh"),
    (sign_in("claude-b", "unreachable", account_doctor.PLACEHOLDER_DEVICE, device="mac"),
     "unreachable; run: fix ssh to mac for claude, then orch accounts doctor; device has install placeholders"),
    (sign_in("claude-default", "ok", ""), None),
], ids=["signed-out", "signed-out-default", "token-auth", "mismatch", "unreachable", "placeholder", "ok"])
def test_flags_name_the_operator_step_for_each_failing_status(
    tmp_path: Path, row: SignIn, expected: str | None
) -> None:
    flagged = account_doctor.flags([row], {}, flag_config(tmp_path), "Owner/Factory")
    key = ("owner/factory", f"{row.account}@{row.device}", "account-signin")
    assert flagged == ({} if expected is None else {key: expected})


def test_a_default_account_flag_names_no_account_folder(tmp_path: Path) -> None:
    rows = [sign_in("claude-default", "signed-out", account_doctor.NOT_LOGGED_IN),
            sign_in("claude-default", "mismatch", "another sign-in on device mac")]
    for row in rows:
        (detail,) = account_doctor.flags([row], {}, flag_config(tmp_path), "owner/factory").values()
        assert "CLAUDE_CONFIG_DIR=" not in detail


SPACED_MACHINES = DOCTOR_MACHINES.replace(
    "  - {id: claude-b, harness: claude, signed_in_on: [pc, mac]}",
    "  - {id: claude-b, harness: claude, signed_in_on: [pc]}\n"
    "  - {id: codex-b, harness: codex, signed_in_on: [pc]}",
).replace("home: /home/{bot}", "home: /home/{bot}/Bot Home")
FAKE_LOGIN = "#!/bin/sh\nprintf 'login %s %s\\n' \"$0\" \"${CLAUDE_CONFIG_DIR-${CODEX_HOME-unset}}\"\n"


def run_step(tmp_path: Path, detail: str, environ: dict[str, str]) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("claude", "codex"):
        (bin_dir / name).write_text(FAKE_LOGIN, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    command = detail.split(" on pc: ", 1)[1].split(", then orch accounts doctor", 1)[0]
    done = subprocess.run(
        ["bash", "-c", command], env={"PATH": f"{bin_dir}:/usr/bin:/bin", **environ},
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize("account, variable, expected", [
    ("claude-default", "CLAUDE_CONFIG_DIR", "claude unset"),
    ("codex-default", "CODEX_HOME", "codex unset"),
    ("claude-b", "CLAUDE_CONFIG_DIR", "claude /home/claude-bot/Bot Home/.factory-accounts/claude-b"),
    ("codex-b", "CODEX_HOME", "codex /home/codex-bot/Bot Home/.factory-accounts/codex-b"),
], ids=["claude-default", "codex-default", "claude-named", "codex-named"])
@pytest.mark.parametrize("status", ["signed-out", "mismatch"])
def test_the_flagged_login_command_signs_in_the_home_the_doctor_probes(
    tmp_path: Path, account: str, variable: str, expected: str, status: str
) -> None:
    path = tmp_path / "spaced.yml"
    path.write_text(SPACED_MACHINES, encoding="utf-8")
    (detail,) = account_doctor.flags([sign_in(account, status, "x")], {}, config_of(path), "owner/factory").values()
    ran = run_step(tmp_path, detail, {variable: "/synthetic/other-account"})
    program, home = expected.split(" ", 1)
    assert ran == f"login {tmp_path / 'bin' / program} {home}"


def test_a_stored_flag_keeps_its_operator_step_however_long_the_explanation(tmp_path: Path) -> None:
    accounts = [f"claude-{index}-" + "x" * 45 for index in range(10)]
    path = tmp_path / "long.yml"
    path.write_text(POLLER_MACHINES.replace(
        "  - {id: claude-b, harness: claude, signed_in_on: [pc]}",
        "\n".join(f"  - {{id: {account}, harness: claude, signed_in_on: [pc]}}" for account in accounts),
    ), encoding="utf-8")
    config = config_of(path)
    probes = [account_doctor.parse_claude(account, "pc", 0, claude_reply())
              for account in config.accounts_on("pc", "claude")]
    rows = account_doctor.judge(probes, NOW)
    connection = connect(tmp_path / "orch.db")
    account_doctor.write(connection, rows, config, "owner/factory", NOW)
    details = {str(row[1]): str(row[3]) for row in attention_rows(connection)}
    assert len(details) == 11
    for detail in details.values():
        assert "claude auth login, then orch accounts doctor" in detail
    assert "run: sign claude-default in to its own account" in details["claude-default@pc"]


def test_run_without_a_repo_writes_the_table_and_raises_no_flag(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    transport = DoctorTransport()
    sign_out_claude_b(transport)
    path = tmp_path / "machines.yml"
    path.write_text(POLLER_MACHINES, encoding="utf-8")
    rows = account_doctor.run(connection, config_of(path), transport, NOW, None)
    assert {(row.account, row.status) for row in rows} == {
        ("claude-default", "ok"), ("claude-b", "signed-out"), ("codex-default", "ok")}
    assert [row[:3] for row in stored(connection)] == [
        ("claude-b", "pc", "signed-out"), ("claude-default", "pc", "ok"), ("codex-default", "pc", "ok")]
    assert attention_rows(connection) == []


def test_doctor_cli_raises_and_clears_the_flag_for_the_factory_repo(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lib.self_deploy.own_repo", lambda *_: "owner/factory")
    assert world.doctor() == 0
    with contextlib.closing(sqlite3.connect(world.db)) as connection:
        raised = [row[:3] for row in attention_rows(connection)]
    assert ("owner/factory", "claude-c@pc", "account-signin") in raised
    replies = world.tmp / "replies" / "claude"
    (replies / "claude-c").write_bytes(claude_reply(org=ORG_B, email="bot-three@example.com"))
    (replies / "claude-c.code").write_text("0", encoding="utf-8")
    assert world.doctor() == 0
    with contextlib.closing(sqlite3.connect(world.db)) as connection:
        assert "claude-c@pc" not in [row[1] for row in attention_rows(connection)]


def test_the_dashboard_lists_an_account_signin_flag_by_its_reason(tmp_path: Path) -> None:
    from lib.dashboard import collect_snapshot
    from tests.test_dashboard import NOW as DASHBOARD_NOW, seeded

    db, log = seeded(tmp_path)
    rows = [sign_in("claude-b", "signed-out", account_doctor.NOT_LOGGED_IN)]
    with contextlib.closing(sqlite3.connect(db)) as connection:
        account_doctor.write(connection, rows, flag_config(tmp_path), "owner/factory", DASHBOARD_NOW)
    items = collect_snapshot(db, log, now=DASHBOARD_NOW)["attention"]["items"]
    assert [item["detail"] for item in items if item.get("ticket_id") == "claude-b@pc"] == [
        f"account-signin: signed-out; run: sign in as claude-bot on pc: "
        f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={CLAUDE_B_HOME} "
        "claude auth login, then orch accounts doctor; not logged in"]


def holds(queue_or_connection: SQLiteJobQueue | sqlite3.Connection) -> list[tuple[object, ...]]:
    if isinstance(queue_or_connection, SQLiteJobQueue):
        with contextlib.closing(queue_or_connection.connect()) as connection:
            return holds(connection)
    return [tuple(row) for row in queue_or_connection.execute(
        f"SELECT account, device, since, source, reason FROM {account_doctor.HOLDS} ORDER BY account, device"
    )]


def preflight_hold(queue: SQLiteJobQueue, at: float) -> None:
    with contextlib.closing(queue.connect()) as connection:
        account_doctor.hold(connection, "claude-b", "pc", "preflight", account_doctor.NOT_LOGGED_IN, at)
        connection.commit()


def test_repeated_holds_keep_one_row_and_the_first_since(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    account_doctor.hold(connection, "claude-b", "pc", "preflight", "first", NOW)
    account_doctor.hold(connection, "claude-b", "pc", "runtime", "second", NOW + 60)
    account_doctor.hold(connection, "codex-default", "pc", "runtime", "other", NOW + 90)
    assert holds(connection) == [
        ("claude-b", "pc", NOW, "preflight", "first"), ("codex-default", "pc", NOW + 90, "runtime", "other")]
    assert account_doctor.held(connection) == frozenset({("claude-b", "pc"), ("codex-default", "pc")})


def test_held_reads_an_empty_set_before_any_hold(tmp_path: Path) -> None:
    assert account_doctor.held(connect(tmp_path / "orch.db")) == frozenset()


def test_two_failed_launches_on_one_bad_login_raise_one_flag_with_one_raised_at(tmp_path: Path) -> None:
    world = poller(tmp_path)
    world.poll(0.0)
    assert world.flags() == []
    sign_out_claude_b(world.transport)
    preflight_hold(world.queue, 10.0)
    world.poll(20.0)
    assert len(world.transport.exchanges) == 4
    assert [(flag[:3], flag[4]) for flag in world.flags()] == [(KEY, 20.0)]
    preflight_hold(world.queue, 30.0)
    world.poll(40.0)
    assert len(world.transport.exchanges) == 4
    world.poll(140.0)
    assert len(world.transport.exchanges) == 6
    assert [(flag[:3], flag[4]) for flag in world.flags()] == [(KEY, 20.0)]
    assert holds(world.queue) == [("claude-b", "pc", 10.0, "preflight", account_doctor.NOT_LOGGED_IN)]


def test_a_later_ok_status_clears_the_hold_and_the_flag_with_no_hand_write(tmp_path: Path) -> None:
    world = poller(tmp_path)
    sign_out_claude_b(world.transport)
    preflight_hold(world.queue, 0.0)
    world.poll(10.0)
    assert [flag[:3] for flag in world.flags()] == [KEY]
    world.transport.replies = DoctorTransport().replies
    world.poll(130.0)
    with contextlib.closing(world.queue.connect()) as connection:
        assert account_doctor.held(connection) == frozenset()
    assert world.flags() == []
    world.poll(250.0)
    assert len(world.transport.exchanges) == 4


def test_a_held_pair_is_rechecked_every_2_minutes_then_every_30_once_clear(tmp_path: Path) -> None:
    world = poller(tmp_path)
    sign_out_claude_b(world.transport)
    world.poll(0.0)
    assert holds(world.queue) == [("claude-b", "pc", 0.0, "doctor", account_doctor.NOT_LOGGED_IN)]
    world.poll(60.0)
    assert len(world.transport.exchanges) == 2
    world.poll(120.0)
    assert len(world.transport.exchanges) == 4
    world.transport.replies = DoctorTransport().replies
    world.poll(240.0)
    assert holds(world.queue) == []
    world.poll(360.0)
    assert len(world.transport.exchanges) == 6
    world.poll(240.0 + account_doctor.INTERVAL_SECONDS)
    assert len(world.transport.exchanges) == 8


class HoldDuringProbe(DoctorTransport):
    def __init__(self, db: Path, at: float) -> None:
        super().__init__()
        self.db = db
        self.at = at

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        with contextlib.closing(connect(self.db)) as connection:
            account_doctor.hold(connection, "claude-b", "pc", "runtime", "authentication_failed", self.at)
        return super().exchange(argv, timeout=timeout, input=input)


def poller_config(tmp_path: Path) -> MachinesConfig:
    path = tmp_path / "machines.yml"
    path.write_text(POLLER_MACHINES, encoding="utf-8")
    return config_of(path)


@pytest.mark.parametrize("at", [NOW, NOW + 5])
def test_a_hold_written_during_the_probe_survives_an_ok_reading(tmp_path: Path, at: float) -> None:
    db = tmp_path / "orch.db"
    connection = connect(db)
    config = poller_config(tmp_path)
    rows = account_doctor.run(connection, config, HoldDuringProbe(db, at), NOW, "Owner/Factory")
    assert {row.status for row in rows} == {"ok"}
    assert holds(connection) == [("claude-b", "pc", at, "runtime", "authentication_failed")]
    assert [row[:3] for row in attention_rows(connection)] == [KEY]
    account_doctor.run(connection, config, DoctorTransport(), NOW + 120, "Owner/Factory")
    assert holds(connection) == []
    assert attention_rows(connection) == []


@pytest.mark.parametrize("at", [NOW + 120, NOW + 125])
def test_a_hold_written_during_the_probe_on_a_held_pair_survives_an_ok_reading(tmp_path: Path, at: float) -> None:
    db = tmp_path / "orch.db"
    connection = connect(db)
    config = poller_config(tmp_path)
    transport = DoctorTransport()
    sign_out_claude_b(transport)
    account_doctor.run(connection, config, transport, NOW, "Owner/Factory")
    rows = account_doctor.run(connection, config, HoldDuringProbe(db, at), NOW + 120, "Owner/Factory")
    assert {row.status for row in rows} == {"ok"}
    assert holds(connection) == [("claude-b", "pc", NOW, "doctor", account_doctor.NOT_LOGGED_IN)]
    assert [(row[:3], row[4]) for row in attention_rows(connection)] == [(KEY, NOW)]
    account_doctor.run(connection, config, DoctorTransport(), NOW + 240, "Owner/Factory")
    assert holds(connection) == []
    assert attention_rows(connection) == []


@pytest.mark.parametrize("reply, step", [
    (claude_reply(method="oauth_token"), "remove the token export from claude-bot's login profile on pc"),
    (claude_reply(), "sign claude-b in to its own account as claude-bot on pc"),
], ids=["token-auth", "mismatch"])
def test_a_newer_failed_diagnosis_on_a_held_pair_names_its_own_step(tmp_path: Path, reply: bytes, step: str) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    transport = DoctorTransport()
    sign_out_claude_b(transport)
    account_doctor.run(connection, config, transport, NOW, "Owner/Factory")
    transport.replies["pc-claude-bot"] = framed((0, 0, claude_reply()), (1, 0, reply))
    rows = account_doctor.run(connection, config, transport, NOW + 120, "Owner/Factory")
    (current,) = [row for row in rows if row.account == "claude-b"]
    ((detail, raised_at),) = [(row[3], row[4]) for row in attention_rows(connection) if row[:3] == KEY]
    assert raised_at == NOW
    assert step in str(detail)
    assert str(detail).endswith(current.detail)


def test_an_ok_reading_clears_a_hold_written_before_the_probe(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    account_doctor.hold(connection, "claude-b", "pc", "runtime", "authentication_failed", NOW - 1)
    account_doctor.run(connection, poller_config(tmp_path), DoctorTransport(), NOW, "Owner/Factory")
    assert holds(connection) == []
    assert attention_rows(connection) == []


def test_a_hold_on_a_pair_machines_yml_no_longer_lists_is_dropped(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    account_doctor.hold(connection, "claude-gone", "pc", "runtime", "authentication_failed", NOW + 5)
    account_doctor.run(connection, poller_config(tmp_path), DoctorTransport(), NOW, "Owner/Factory")
    assert holds(connection) == []
    assert attention_rows(connection) == []


def test_an_unreached_reading_keeps_the_hold_and_its_signed_out_flag(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    transport = DoctorTransport()
    sign_out_claude_b(transport)
    account_doctor.run(connection, config, transport, NOW, "Owner/Factory")
    (before,) = attention_rows(connection)
    transport.replies = {alias: b"" for alias in transport.replies}
    rows = account_doctor.run(connection, config, transport, NOW + 120, "Owner/Factory")
    assert {row.status for row in rows} == {"unreachable"}
    assert holds(connection) == [("claude-b", "pc", NOW, "doctor", account_doctor.NOT_LOGGED_IN)]
    flags = {row[1]: row for row in attention_rows(connection)}
    assert flags["claude-b@pc"][3:] == before[3:]


@pytest.mark.parametrize("reply, code", [
    (claude_reply(method="oauth_token"), 0),
    (claude_reply(), 0),
    (b"not json", 0),
    (claude_reply(org=ORG_B, email=EMAIL_B), 1),
], ids=["token-auth", "mismatch", "not-understood", "exit-nonzero"])
def test_only_a_not_logged_in_reading_holds_the_pair(tmp_path: Path, reply: bytes, code: int) -> None:
    connection = connect(tmp_path / "orch.db")
    transport = DoctorTransport()
    transport.replies["pc-claude-bot"] = framed((0, 0, claude_reply()), (1, code, reply))
    account_doctor.run(connection, poller_config(tmp_path), transport, NOW, "Owner/Factory")
    assert holds(connection) == []
    assert "claude-b@pc" in [row[1] for row in attention_rows(connection)]


@pytest.mark.parametrize("last, newest, now, expected", [
    (None, None, NOW, True),
    (NOW, None, NOW + account_doctor.INTERVAL_SECONDS - 1, False),
    (NOW, None, NOW + account_doctor.INTERVAL_SECONDS, True),
    (NOW, NOW + 1, NOW + 2, True),
    (NOW, NOW, NOW + 2, False),
    (NOW, NOW - 50, NOW + account_doctor.HELD_INTERVAL_SECONDS - 1, False),
    (NOW, NOW - 50, NOW + account_doctor.HELD_INTERVAL_SECONDS, True),
], ids=["first", "clear-waits", "clear-due", "new-hold", "own-hold", "held-waits", "held-due"])
def test_due(last: float | None, newest: float | None, now: float, expected: bool) -> None:
    assert account_doctor.due(last, newest, now) is expected


def test_a_signin_fence_is_refused_while_a_pair_is_held_and_unfence_clears_it(tmp_path: Path) -> None:
    pairs = [("claude-b", "pc"), ("claude-c", "pc")]
    with contextlib.closing(sqlite3.connect(tmp_path / "orch.db", isolation_level=None)) as connection:
        assert account_doctor.reserve_pair(connection, "claude-c", "pc", "signin-1", NOW)
        assert account_doctor.fence_signins(connection, pairs, NOW + 1) is False
        assert account_doctor.signing_out(connection, NOW + 1) == {("claude-c", "pc")}
        account_doctor.release_pair(connection, "claude-c", "pc", "signin-1")
        assert account_doctor.fence_signins(connection, pairs, NOW + 1) is True
        assert account_doctor.signing_out(connection, NOW + 1) == set(pairs)
        assert not account_doctor.reserve_pair(connection, "claude-b", "pc", "signin-2", NOW + 2)
        account_doctor.unfence_signins(connection)
        assert account_doctor.signing_out(connection, NOW + 1) == frozenset()


def test_a_signin_fence_expires_after_the_lease(tmp_path: Path) -> None:
    lease = account_doctor.SIGNOUT_LEASE_SECONDS
    with contextlib.closing(sqlite3.connect(tmp_path / "orch.db", isolation_level=None)) as connection:
        assert account_doctor.reserve_pair(connection, "claude-c", "pc", "signin-1", NOW)
        assert account_doctor.fence_signins(connection, [("claude-c", "pc")], NOW + lease) is True
        assert account_doctor.signing_out(connection, NOW + 2 * lease - 1) == {("claude-c", "pc")}
        assert account_doctor.reserve_pair(connection, "claude-c", "pc", "signin-2", NOW + 2 * lease)


def test_extend_signin_fence_covers_pairs_added_during_active_fence(tmp_path: Path) -> None:
    with contextlib.closing(sqlite3.connect(tmp_path / "orch.db", isolation_level=None)) as connection:
        assert account_doctor.fence_signins(connection, (), NOW) is True
        assert account_doctor.fence_signins(connection, (), NOW + 1) is False
        assert not account_doctor.reserve_pair(connection, "claude-new", "pc", "signin-1", NOW + 1)
        account_doctor.extend_signin_fence(connection, [("claude-new", "pc")])
        assert account_doctor.signing_out(connection, NOW + 1) == {("claude-new", "pc")}
        account_doctor.unfence_signins(connection)
        assert account_doctor.signing_out(connection, NOW + 1) == frozenset()
        assert account_doctor.reserve_pair(connection, "claude-new", "pc", "signin-1", NOW + 2)


def drops(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [tuple(row) for row in connection.execute(
        f"SELECT account, device, since, announced FROM {account_doctor.DROPS} ORDER BY account, device")]


@pytest.mark.parametrize("opened", [connect, lambda path: SQLiteJobQueue(path, clock=lambda: NOW).connect()],
                         ids=["cli", "queue"])
def test_a_definite_signed_out_row_records_an_unannounced_drop(
    tmp_path: Path, opened: Callable[[Path], sqlite3.Connection]
) -> None:
    transport = DoctorTransport()
    transport.replies["pc-claude-bot"] = framed((0, 0, claude_reply(method="oauth_token")), (1, 1, SIGNED_OUT_CLAUDE))
    with contextlib.closing(opened(tmp_path / "orch.db")) as connection:
        account_doctor.run(connection, poller_config(tmp_path), transport, NOW, "Owner/Factory")
    with contextlib.closing(connect(tmp_path / "orch.db")) as connection:
        assert drops(connection) == [("claude-b", "pc", NOW, 0), ("claude-default", "pc", NOW, 0)]


def test_a_held_pair_that_reads_unreachable_is_a_drop(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    account_doctor.hold(connection, "claude-b", "pc", "runtime", account_doctor.AUTHENTICATION_FAILED, NOW - 1)
    transport = DoctorTransport()
    transport.replies = {alias: b"" for alias in transport.replies}
    rows = account_doctor.run(connection, poller_config(tmp_path), transport, NOW, "Owner/Factory")
    assert {row.status for row in rows} == {"unreachable"}
    assert drops(connection) == [("claude-b", "pc", NOW, 0)]


def test_a_drop_read_again_keeps_its_since(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    transport = DoctorTransport()
    sign_out_claude_b(transport)
    account_doctor.run(connection, config, transport, NOW, "Owner/Factory")
    account_doctor.run(connection, config, transport, NOW + 120, "Owner/Factory")
    assert drops(connection) == [("claude-b", "pc", NOW, 0)]
    transport.replies = {alias: b"" for alias in transport.replies}
    account_doctor.run(connection, config, transport, NOW + 240, "Owner/Factory")
    assert drops(connection) == [("claude-b", "pc", NOW, 0)]


def test_an_ok_row_deletes_the_drop_and_the_next_drop_takes_a_new_since(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    transport = DoctorTransport()
    sign_out_claude_b(transport)
    account_doctor.run(connection, config, transport, NOW, "Owner/Factory")
    assert drops(connection) == [("claude-b", "pc", NOW, 0)]
    account_doctor.run(connection, config, DoctorTransport(), NOW + 120, "Owner/Factory")
    assert drops(connection) == []
    account_doctor.run(connection, config, transport, NOW + 240, "Owner/Factory")
    assert drops(connection) == [("claude-b", "pc", NOW + 240, 0)]


def test_a_pair_that_leaves_machines_config_loses_its_drop(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    transport = DoctorTransport()
    sign_out_claude_b(transport)
    rows = account_doctor.run(connection, config, transport, NOW, "Owner/Factory")
    assert drops(connection) == [("claude-b", "pc", NOW, 0)]
    account_doctor.write(connection, [row for row in rows if row.account != "claude-b"], config,
                         "Owner/Factory", NOW + 120)
    assert drops(connection) == []


@pytest.mark.parametrize("status, detail", [
    ("unreachable", account_doctor.SSH_DOWN),
    ("mismatch", "same sign-in as account claude-default"),
    ("signed-out", account_doctor.NOT_UNDERSTOOD),
    ("signed-out", account_doctor.EXIT_NONZERO),
], ids=["unreachable", "mismatch", "not-understood", "exit-nonzero"])
def test_an_unreachable_mismatch_or_inconclusive_row_with_no_hold_leaves_the_drops_as_they_are(
    tmp_path: Path, status: str, detail: str
) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    pairs = ("claude-b", "claude-default")
    account_doctor.write(connection, [sign_in(account, status, detail) for account in pairs], config, None, NOW)
    assert drops(connection) == []
    account_doctor.write(connection, [sign_in(account, "signed-out", account_doctor.TOKEN_AUTH) for account in pairs],
                         config, None, NOW + 1, deliberate=("claude-b", "pc"))
    account_doctor.write(connection, [sign_in(account, status, detail) for account in pairs], config, None, NOW + 2)
    assert holds(connection) == []
    assert drops(connection) == [("claude-b", "pc", NOW + 1, 1), ("claude-default", "pc", NOW + 1, 0)]


def test_a_deliberate_pair_is_recorded_announced(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    rows = [sign_in(account, "signed-out", account_doctor.NOT_LOGGED_IN) for account in ("claude-b", "claude-default")]
    account_doctor.write(connection, rows, config, None, NOW, deliberate=("claude-b", "pc"))
    assert drops(connection) == [("claude-b", "pc", NOW, 1), ("claude-default", "pc", NOW, 0)]
    account_doctor.write(connection, rows, config, None, NOW + 1, deliberate=("claude-default", "pc"))
    assert drops(connection) == [("claude-b", "pc", NOW, 1), ("claude-default", "pc", NOW, 1)]
    account_doctor.write(connection, rows, config, None, NOW + 2)
    assert drops(connection) == [("claude-b", "pc", NOW, 1), ("claude-default", "pc", NOW, 1)]
    other = connect(tmp_path / "other.db")
    for row in (sign_in("codex-default", "ok", ""), sign_in("codex-default", "unreachable", account_doctor.SSH_DOWN)):
        account_doctor.write(other, [row], config, None, NOW, deliberate=("codex-default", "pc"))
        assert drops(other) == []


TOKEN_READING = ("signed-out; run: remove the token export from claude-bot's login profile on pc, "
                 f"then orch accounts doctor; {account_doctor.TOKEN_AUTH}")
HELD_READING = (f"signed-out; run: sign in as claude-bot on pc: env -u CLAUDE_CODE_OAUTH_TOKEN "
                f"CLAUDE_CONFIG_DIR={CLAUDE_B_HOME} claude auth login, then orch accounts doctor; "
                f"{account_doctor.AUTHENTICATION_FAILED}")


def test_take_unannounced_marks_in_the_callers_transaction(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    account_doctor.hold(connection, "claude-b", "pc", "runtime", account_doctor.AUTHENTICATION_FAILED, NOW - 1)
    transport = DoctorTransport()
    transport.replies["pc-claude-bot"] = framed((0, 0, claude_reply(method="oauth_token")), (1, 0, b"not json"))
    account_doctor.run(connection, config, transport, NOW, "Owner/Factory")
    expected = [account_doctor.Drop("claude-b", "pc", "claude", NOW, HELD_READING),
                account_doctor.Drop("claude-default", "pc", "claude", NOW, TOKEN_READING)]
    assert {row[1]: row[3] for row in attention_rows(connection)} == {
        "claude-b@pc": HELD_READING, "claude-default@pc": TOKEN_READING}
    connection.execute("BEGIN IMMEDIATE")
    assert account_doctor.take_unannounced(connection, config) == expected
    connection.execute("ROLLBACK")
    assert drops(connection) == [("claude-b", "pc", NOW, 0), ("claude-default", "pc", NOW, 0)]
    connection.execute("BEGIN IMMEDIATE")
    assert account_doctor.take_unannounced(connection, config) == expected
    connection.execute("COMMIT")
    assert drops(connection) == [("claude-b", "pc", NOW, 1), ("claude-default", "pc", NOW, 1)]
    assert account_doctor.take_unannounced(connection, config) == []


def test_take_unannounced_skips_a_pair_the_config_does_not_declare(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    account_doctor.write(connection, [sign_in("claude-b", "signed-out", account_doctor.TOKEN_AUTH)], config, None, NOW)
    connection.execute(f"INSERT INTO {account_doctor.TABLE} (account, device, status, checked_at, detail) "
                       "VALUES ('claude-gone', 'pc', 'signed-out', ?, 'not logged in')", (NOW,))
    connection.execute(f"INSERT INTO {account_doctor.DROPS} VALUES ('claude-gone', 'pc', ?, 0)", (NOW - 5,))
    assert account_doctor.take_unannounced(connection, config) == [
        account_doctor.Drop("claude-b", "pc", "claude", NOW, TOKEN_READING)]
    assert drops(connection) == [("claude-b", "pc", NOW, 1), ("claude-gone", "pc", NOW - 5, 0)]


MISSING_CLI_MACHINES = """\
devices:
  - name: box
    local: true
    slot_max: 1
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot, gemini: gemini-bot}
    home: HOME
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 1
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot, gemini: gemini-bot}
    workdir_root: HOME/work
    ssh_alias: pc-{bot}
    factory_root: /srv/factory
    home: HOME
"""
NO_HARNESS_PATH = "/usr/bin:/bin"
INSTALL_READING = ("signed-out; run: install the claude CLI for claude-bot on pc, then orch accounts doctor; "
                   "harness CLI not found")


class ShellTransport:
    def __init__(self, home: Path) -> None:
        self.env = {"HOME": str(home), "PATH": NO_HARNESS_PATH}

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        return 0

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        done = subprocess.run(["/bin/sh", "-c", argv[-1]], env=self.env, capture_output=True, timeout=timeout,
                              check=False)
        return Exchanged(done.returncode, done.stdout, done.stderr.decode())


def missing_cli_world(tmp_path: Path) -> tuple[MachinesConfig, ShellTransport, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "missing-cli.yml"
    path.write_text(MISSING_CLI_MACHINES.replace("HOME", str(home)), encoding="utf-8")
    return config_of(path), ShellTransport(home), {"HOME": str(home), "PATH": NO_HARNESS_PATH}


@pytest.mark.parametrize("where", ["box", "pc"])
@pytest.mark.parametrize("harness", sorted(get_args(account_doctor.Harness)))
def test_exit_127_reads_harness_cli_not_found_for_every_harness(tmp_path: Path, harness: str, where: str) -> None:
    config, transport, environ = missing_cli_world(tmp_path)
    device = config.device(where)
    account = device.account_for(harness)
    if device.local:
        reading = account_doctor._probe_local(
            device, harness, account, config.account_home(where, harness, account), environ)
    else:
        [reading] = account_doctor.probe(transport, config, device, harness)
    [row] = account_doctor.judge([reading], NOW)
    assert (row.account, row.status, row.detail) == (account, "signed-out", "harness CLI not found")
    assert account_doctor.CLI_NOT_FOUND not in account_doctor._INCONCLUSIVE


@pytest.mark.parametrize("where", ["box", "pc"])
@pytest.mark.parametrize("harness", sorted(get_args(account_doctor.Harness)))
def test_the_job_preflight_does_not_raise_for_a_missing_cli(tmp_path: Path, harness: str, where: str) -> None:
    config, transport, environ = missing_cli_world(tmp_path)
    device = config.device(where)
    account_doctor.check_login(transport, config, device, harness, device.account_for(harness), environ)


def test_a_missing_cli_is_a_drop(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    config = poller_config(tmp_path)
    transport = DoctorTransport()
    transport.replies["pc-claude-bot"] = framed((0, 0, claude_reply()), (1, 127, b""))
    rows = account_doctor.run(connection, config, transport, NOW, "Owner/Factory")
    assert [(row.account, row.status, row.detail) for row in rows if row.status != "ok"] == [
        ("claude-b", "signed-out", "harness CLI not found")]
    assert drops(connection) == [("claude-b", "pc", NOW, 0)]
    assert account_doctor.take_unannounced(connection, config) == [
        account_doctor.Drop("claude-b", "pc", "claude", NOW, INSTALL_READING)]


def test_the_flag_for_a_missing_cli_says_install(tmp_path: Path) -> None:
    row = sign_in("claude-b", "signed-out", "harness CLI not found")
    assert account_doctor.flags([row], {}, flag_config(tmp_path), "Owner/Factory") == {KEY: INSTALL_READING}
