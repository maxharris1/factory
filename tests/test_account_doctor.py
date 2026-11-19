from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib import account_doctor
from lib.account_doctor import Probe, SignIn

NOW = 1_800_000_000.0
ORG = "4b0c6c5e-0000-4000-8000-00000000a001"
EMAIL = "bot-one@example.com"


def fingerprint(org: str, email: str) -> str:
    return hashlib.sha256(f"{org}\0{email}".encode()).hexdigest()[:12]


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
    account_doctor.write(connection, first)
    assert [row[:3] for row in stored(connection)] == [("gone", "box", "ok"), ("kept", "mac", "ok")]

    second = account_doctor.judge([claude_probe(account="kept", device="mac", code=1, reply=SIGNED_OUT_CLAUDE)],
                                  NOW + 60)
    account_doctor.write(connection, second)
    assert [row[:3] + (row[6],) for row in stored(connection)] == [("kept", "mac", "signed-out", NOW + 60)]
    assert not connection.in_transaction


def test_write_rolls_back_whole_on_a_bad_row(tmp_path: Path) -> None:
    connection = connect(tmp_path / "orch.db")
    good = account_doctor.judge([claude_probe()], NOW)
    account_doctor.write(connection, good)
    bad = replace(good[0], status="fine")
    with pytest.raises(sqlite3.IntegrityError):
        account_doctor.write(connection, [good[0], bad])
    assert [row[:3] for row in stored(connection)] == [("acct", "box", "ok")]
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


def test_canaries_reach_neither_rows_details_nor_database(tmp_path: Path) -> None:
    probes = leaky_probes()
    rows = account_doctor.judge(probes, NOW)
    database = tmp_path / "orch.db"
    connection = connect(database)
    account_doctor.write(connection, rows)
    stored_rows = stored(connection)

    expected = fingerprint(CANARY_ORG, CANARY_EMAIL)
    assert (expected,) in connection.execute("SELECT fingerprint FROM account_signins WHERE account = 'seen'")
    assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    connection.close()

    surfaces = {
        "probes": repr(probes).encode(),
        "rows": repr(rows).encode(),
        "details": "\n".join(row.detail for row in rows).encode(),
        "stored": repr(stored_rows).encode(),
        "orch.db": database.read_bytes(),
        "orch.db-wal": (tmp_path / "orch.db-wal").read_bytes() if (tmp_path / "orch.db-wal").exists() else b"",
    }
    for name, data in surfaces.items():
        for canary in CANARIES:
            assert canary.encode() not in data, f"{canary} reached {name}"
    assert expected.encode() in surfaces["orch.db"]


ORG_B = "4b0c6c5e-0000-4000-8000-00000000b002"
EMAIL_B = "bot-two@example.com"
TOKENS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY")

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
    harnesses: {{claude: claude-bot, codex: codex-bot}}
    workdir_root: /home/{{bot}}/work
    ssh_alias: pc-{{bot}}
    factory_root: /srv/factory
    home: {home}
  - name: lap
    tailscale_name: lap.example.ts.net
    slot_max: 1
    enabled: false
    harnesses: {{claude: claude-bot, codex: codex-bot}}
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
if [ -n "{token_test}" ]; then cat {replies}/token; exit 0; fi
folder=${{{home_variable}:-default}}
cat "{replies}/${{folder##*/}}"
"""
TOKEN_TESTS = {
    "claude": "$CLAUDE_CODE_OAUTH_TOKEN$ANTHROPIC_API_KEY$ANTHROPIC_AUTH_TOKEN",
    "codex": "$OPENAI_API_KEY$CODEX_API_KEY",
}
HOME_VARIABLES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
REPLIES = {
    "claude": {
        "default": claude_reply(),
        "claude-b": claude_reply(org=ORG_B, email=EMAIL_B, subscription="pro"),
        "claude-c": SIGNED_OUT_CLAUDE,
        "token": claude_reply(method="oauth_token"),
    },
    "codex": {
        "default": b"Logged in using ChatGPT\n",
        "codex-b": b"Not logged in\n",
        "token": b"Logged in using an API key - sk-proj-***\n",
    },
}
PROFILE = """\
export PATH={bin}:$PATH
export CLAUDE_CONFIG_DIR={home}/.factory-accounts/claude-c CODEX_HOME={home}/.factory-accounts/codex-b
{tokens}
echo profile says hello
printf 'no newline at the end'
"""


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from tests.test_remote_start import Ssh

        self.tmp = tmp_path
        self.ssh = Ssh(tmp_path)
        for alias in ("pc-claude-bot", "lap-claude-bot"):
            (self.ssh.hosts / alias).write_text("claude-bot", encoding="utf-8")
        for alias in ("pc-codex-bot", "lap-codex-bot"):
            (self.ssh.hosts / alias).write_text("codex-bot", encoding="utf-8")
        for alias in ("lap-claude-bot", "lap-codex-bot"):
            self.ssh.offline(alias).touch()
        self.device_home = tmp_path / "device-home"
        self.device_home.mkdir()
        self.bin = tmp_path / "device-bin"
        self.bin.mkdir()
        self.env_log = tmp_path / "harness-env.log"
        for harness, replies in REPLIES.items():
            folder = tmp_path / "replies" / harness
            folder.mkdir(parents=True)
            for name, reply in replies.items():
                (folder / name).write_bytes(reply)
            fake = self.bin / harness
            fake.write_text(FAKE_HARNESS.format(
                name=harness, log=self.env_log, token_test=TOKEN_TESTS[harness],
                replies=folder, home_variable=HOME_VARIABLES[harness],
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
}


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


def test_token_variables_from_the_login_profile_never_reach_the_harness(world: World) -> None:
    assert world.doctor() == 0
    envs = world.harness_envs()
    assert sorted(name for name, _ in envs) == ["claude"] * 3 + ["codex"] * 2
    for _, names in envs:
        assert not names & set(TOKENS)
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
    for account in ("claude-default", "codex-default"):
        assert (rows[(account, "lap")].status, rows[(account, "lap")].detail) == ("unreachable", "device unreachable over ssh")
    calls = world.ssh.calls()
    assert sorted(call["alias"] for call in calls) == ["lap-claude-bot", "lap-codex-bot", "pc-claude-bot", "pc-codex-bot"]
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


def framed(*frames: tuple[int, int, bytes], before: bytes = b"") -> bytes:
    body = b"".join(reply + b"\nORCH-ACCOUNT %d %d\n" % (index, code) for index, code, reply in frames)
    return before + b"\nORCH-ACCOUNTS-BEGIN\n" + body


def test_replies_drop_profile_output_and_split_frames() -> None:
    stdout = framed((0, 0, b'{"a": 1}'), (1, 3, b"Not logged in"), before=b"ORCH-ACCOUNT 0 9\nnoise")
    assert account_doctor._replies(stdout, 2) == {0: (0, b'{"a": 1}'), 1: (3, b"Not logged in")}


def test_replies_ignore_a_forged_marker_inside_a_reply() -> None:
    stdout = framed((0, 0, b"first\nORCH-ACCOUNT 1 0\nstill first"), (1, 1, b"second"))
    assert account_doctor._replies(stdout, 2) == {0: (0, b"first\nORCH-ACCOUNT 1 0\nstill first"), 1: (1, b"second")}


def test_replies_without_a_frame_leave_it_out() -> None:
    assert account_doctor._replies(framed((0, 0, b"only")), 2) == {0: (0, b"only")}
    assert account_doctor._replies(b"no begin line\nORCH-ACCOUNT 0 0\n", 1) == {}
