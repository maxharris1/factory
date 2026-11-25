from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from lib import account_doctor, usage_readings
from lib.account_doctor import SignIn
from lib.dashboard import collect_snapshot
from lib.dashboard_accounts import read_accounts
from lib.device_pool import FLOOR
from lib.usage_readings import Reading
from tests.test_dashboard import add_job, make_db
from tests.test_remote_start import config_of

NOW = 1_800_000_000.0
FIVE_HOUR_RESET = int(NOW) + 3600
SEVEN_DAY_RESET = int(NOW) + 86_400

EMAIL = "bot-one@example.com"
ORG = "4b0c6c5e-0000-4000-8000-00000000a001"
FINGERPRINT = "9f3e2a1b7c6d"
TOKEN = "sk-ant-oat01-Zq7vW2xK9mP4rT8sY1uN6bH3cJ5dF0gL"
PLANTED = (EMAIL, ORG, FINGERPRINT, TOKEN)

MACHINES = """\
accounts:
  - {id: claude-b, harness: claude, signed_in_on: [pc, mac], max_jobs: 2, paused: true}
  - {id: ext-grok, harness: external}
devices:
  - name: box
    local: true
    slot_max: 2
    harnesses: {claude: claude-bot, codex: codex-bot}
    home: /home/{bot}
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {claude: claude-bot, codex: codex-bot}
    workdir_root: /home/{bot}/work
    ssh_alias: pc-{bot}
    factory_root: /srv/factory
    home: /home/{bot}
  - name: mac
    tailscale_name: mac.example.ts.net
    slot_max: 1
    harnesses: {claude: claude-bot}
    workdir_root: /Users/{bot}/work
    ssh_alias: mac-{bot}
    factory_root: /srv/factory
    home: /Users/{bot}
"""
CLAUDE_B_PC = "/home/claude-bot/.factory-accounts/claude-b"
CLAUDE_B_MAC = "/Users/claude-bot/.factory-accounts/claude-b"


def sign_in(account: str, device: str, status: str, detail: str = "", *, method: str | None = "claude.ai",
            subscription: str | None = "max", fingerprint: str | None = None) -> SignIn:
    return SignIn(account, device, status, method, subscription, fingerprint, NOW - 300, detail)


SIGN_INS = (
    sign_in("claude-default", "box", "ok", fingerprint="aaaaaaaaaaaa"),
    sign_in("claude-default", "pc", "ok", fingerprint="aaaaaaaaaaaa"),
    sign_in("claude-default", "mac", "unreachable", account_doctor.SSH_DOWN, method=None, subscription=None),
    sign_in("claude-b", "pc", "mismatch", "same sign-in as account claude-default", fingerprint="aaaaaaaaaaaa"),
    sign_in("claude-b", "mac", "signed-out", account_doctor.NOT_LOGGED_IN, method="none", subscription=None),
    sign_in("codex-default", "box", "ok", method="chatgpt", subscription=None),
)


def world(tmp_path: Path, sign_ins: tuple[SignIn, ...] = SIGN_INS) -> tuple[Path, Path]:
    db = tmp_path / "orch.db"
    queue = make_db(db)
    machines = tmp_path / "machines.yml"
    machines.write_text(MACHINES, encoding="utf-8")
    config = config_of(machines)
    with contextlib.closing(sqlite3.connect(db)) as connection, connection:
        account_doctor.write(connection, sign_ins, config, None, NOW - 300)
        connection.execute(
            "CREATE TABLE device_slots (job_id TEXT PRIMARY KEY, device TEXT NOT NULL, harness TEXT NOT NULL, "
            "os_user TEXT NOT NULL, placed_at REAL NOT NULL, account TEXT)"
        )
        for job_id, ticket, action, recipient, device, account in (
            ("job-b-pc", "41", "build", "claude", "pc", "claude-b"),
            ("job-default-box", "42", "fix", "claude", "box", None),
            ("job-codex-pc", "43", "review", "codex", "pc", "codex-default"),
        ):
            add_job(connection, job_id, action=action, recipient=recipient, state="running", ticket_id=ticket)
            connection.execute("INSERT INTO device_slots VALUES (?, ?, ?, ?, ?, ?)",
                               (job_id, device, recipient, f"{recipient}-bot", NOW - 120, account))
        usage_readings.store(
            connection, harness="claude", bot="claude-bot", device="box", job_id="job-default-box",
            readings=[Reading("five_hour", 0.25, FIVE_HOUR_RESET, NOW - 60),
                      Reading("seven_day", 0.6, SEVEN_DAY_RESET, NOW - 60)],
        )
    del queue
    return db, machines


def snapshot_accounts(tmp_path: Path, **kwargs: object) -> list[dict]:
    db, machines = world(tmp_path, **kwargs)
    return collect_snapshot(db, tmp_path / "poll.log", now=NOW, machines_path=machines)["accounts"]


def by_id(accounts: list[dict]) -> dict[str, dict]:
    return {account["id"]: account for account in accounts}


def test_accounts_list_every_account_with_its_settings_and_read_only_reason(tmp_path: Path) -> None:
    accounts = snapshot_accounts(tmp_path)
    assert [(a["id"], a["harness"], a["max_jobs"], a["paused"], a["read_only"]) for a in accounts] == [
        ("claude-default", "claude", None, False, "default"),
        ("codex-default", "codex", None, False, "default"),
        ("claude-b", "claude", 2, True, None),
        ("ext-grok", "external", None, False, "external"),
    ]
    assert by_id(accounts)["ext-grok"]["signed_in_on"] == []


def test_each_signed_in_device_carries_the_doctor_status_and_a_sign_in_command_when_not_ok(
    tmp_path: Path,
) -> None:
    accounts = by_id(snapshot_accounts(tmp_path))
    assert accounts["claude-b"]["signed_in_on"] == [
        {
            "device": "pc",
            "status": "mismatch",
            "auth_method": "claude.ai",
            "subscription_type": "max",
            "checked_at": NOW - 300,
            "same_login_as": ["claude-default"],
            "command": f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={CLAUDE_B_PC} claude auth login",
            "login": "claude-bot",
        },
        {
            "device": "mac",
            "status": "signed-out",
            "auth_method": "none",
            "subscription_type": None,
            "checked_at": NOW - 300,
            "same_login_as": [],
            "command": f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={CLAUDE_B_MAC} claude auth login",
            "login": "claude-bot",
        },
    ]
    default = accounts["claude-default"]["signed_in_on"]
    assert [(d["device"], d["status"], d["command"]) for d in default] == [
        ("box", "ok", None),
        ("pc", "ok", None),
        ("mac", "unreachable", "env -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDE_CONFIG_DIR claude auth login"),
    ]
    codex = accounts["codex-default"]["signed_in_on"]
    assert [(d["device"], d["status"], d["auth_method"], d["command"]) for d in codex] == [
        ("box", "ok", "chatgpt", None),
        ("pc", None, None, "env -u CODEX_HOME codex login --device-auth"),
    ]


def test_each_account_carries_its_usage_floor_and_running_jobs(tmp_path: Path) -> None:
    accounts = by_id(snapshot_accounts(tmp_path))
    default = accounts["claude-default"]
    assert (default["five_hour_used"], default["seven_day_used"], default["headroom"], default["floor"]) == (
        0.25, 0.6, pytest.approx(0.4), FLOOR,
    )
    assert accounts["claude-b"]["five_hour_used"] is None
    assert accounts["claude-b"]["headroom"] is None
    assert default["running"] == [{"ticket": "42", "kind": "fix", "device": "box"}]
    assert accounts["claude-b"]["running"] == [{"ticket": "41", "kind": "build", "device": "pc"}]
    assert accounts["codex-default"]["running"] == [{"ticket": "43", "kind": "review", "device": "pc"}]
    assert accounts["ext-grok"]["running"] == []


def test_the_operator_step_and_the_dashboard_share_one_login_command(tmp_path: Path) -> None:
    config = config_of_machines(tmp_path)
    (flag,) = account_doctor.flags(
        [sign_in("claude-b", "pc", "signed-out", account_doctor.NOT_LOGGED_IN)], {}, config, "o/r"
    ).values()
    assert account_doctor.login_command("claude", CLAUDE_B_PC) in flag
    assert account_doctor.login_command("codex", None) == "env -u CODEX_HOME codex login --device-auth"


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
"""


def test_a_cursor_account_shows_its_doctor_status_and_the_cursor_login_command(tmp_path: Path) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(CURSOR_MACHINES, encoding="utf-8")
    config = config_of(machines)
    with contextlib.closing(sqlite3.connect(tmp_path / "orch.db")) as connection:
        with connection:
            account_doctor.write(connection, (
                sign_in("cursor-default", "box", "signed-out", "partly signed in", method=None, subscription=None),
                sign_in("cursor-default", "pc", "ok", method="login", subscription=None, fingerprint="bbbbbbbbbbbb"),
            ), config, None, NOW - 300)
        accounts = by_id(read_accounts(connection, NOW, config))
    assert accounts["cursor-default"]["signed_in_on"] == [
        {"device": "box", "status": "signed-out", "auth_method": None, "subscription_type": None,
         "checked_at": NOW - 300, "same_login_as": [], "login": "cursor-bot",
         "command": "env -u CURSOR_API_KEY -u CURSOR_AUTH_TOKEN -u CURSOR_CONFIG_DIR cursor-agent login"},
        {"device": "pc", "status": "ok", "auth_method": "login", "subscription_type": None,
         "checked_at": NOW - 300, "same_login_as": [], "login": "cursor-bot", "command": None},
    ]


def config_of_machines(tmp_path: Path):
    machines = tmp_path / "machines.yml"
    machines.write_text(MACHINES, encoding="utf-8")
    return config_of(machines)


PLANTED_SIGN_INS = (
    sign_in("claude-b", "pc", "mismatch", f"same sign-in as account claude-default, {EMAIL}; {ORG}; {TOKEN}",
            method=TOKEN, subscription=EMAIL, fingerprint=FINGERPRINT),
    sign_in("claude-default", "pc", "ok", f"{EMAIL} {ORG} {FINGERPRINT}", method=ORG, subscription=TOKEN,
            fingerprint=FINGERPRINT),
    sign_in("codex-default", "box", "signed-out", TOKEN, method=EMAIL, subscription=FINGERPRINT,
            fingerprint=TOKEN),
)


def plant(db: Path) -> None:
    with contextlib.closing(sqlite3.connect(db)) as connection, connection:
        add_job(connection, "job-planted", state="running", repo=f"{EMAIL}/{TOKEN}",
                detail=json.dumps({"email": EMAIL, "org": ORG, "fingerprint": FINGERPRINT, "token": TOKEN}))
        connection.execute("INSERT INTO device_slots VALUES (?, ?, ?, ?, ?, ?)",
                           ("job-planted", "pc", "claude", "claude-bot", NOW - 60, "claude-b"))
        usage_readings.store(
            connection, harness="claude", bot=EMAIL, device=ORG, job_id=TOKEN,
            readings=[Reading("five_hour", 0.5, FIVE_HOUR_RESET, NOW - 30)],
        )


def test_no_planted_secret_reaches_accounts_or_status_json(tmp_path: Path) -> None:
    db, machines = world(tmp_path, sign_ins=PLANTED_SIGN_INS)
    plant(db)
    with contextlib.closing(sqlite3.connect(db)) as connection:
        stored = " ".join(str(value) for row in connection.execute(
            f"SELECT * FROM {account_doctor.TABLE}") for value in row)
        unscrubbed = json.dumps(read_accounts(connection, NOW, config_of(machines)))
    assert all(secret in stored for secret in PLANTED)
    snapshot = collect_snapshot(db, tmp_path / "poll.log", now=NOW, machines_path=machines)
    published = json.dumps(snapshot)
    for secret in PLANTED:
        assert secret not in unscrubbed
        assert secret not in published
    claude_b_pc = by_id(snapshot["accounts"])["claude-b"]["signed_in_on"][0]
    assert (claude_b_pc["auth_method"], claude_b_pc["subscription_type"]) == (None, None)
    assert claude_b_pc["same_login_as"] == ["claude-default"]
    assert {"ticket": "12", "kind": "build", "device": "pc"} in by_id(snapshot["accounts"])["claude-b"]["running"]


def test_a_database_without_jobs_slots_doctor_or_readings_lists_accounts_with_nothing_read(
    tmp_path: Path,
) -> None:
    with contextlib.closing(sqlite3.connect(tmp_path / "empty.db")) as connection:
        accounts = by_id(read_accounts(connection, NOW, config_of_machines(tmp_path)))
    assert accounts["claude-b"]["running"] == []
    assert accounts["claude-b"]["headroom"] is None
    assert [d["status"] for d in accounts["claude-b"]["signed_in_on"]] == [None, None]


@pytest.mark.parametrize(("slots", "state"), [
    ("1", "enabled: true"),
    ("1", "enabled: false"),
    ("1", "drain: true"),
    ("REPLACE_WITH_SLOTS", "enabled: true"),
])
def test_a_device_that_stops_taking_work_keeps_its_account_sign_in_and_running_job(
    tmp_path: Path, slots: str, state: str
) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    machines = tmp_path / "machines.yml"
    machines.write_text(
        f"devices:\n  - name: box\n    local: true\n    slot_max: {slots}\n    harnesses: {{claude: claude-bot}}\n"
        f"    {state}\n",
        encoding="utf-8",
    )
    config = config_of(machines)
    with contextlib.closing(sqlite3.connect(db)) as connection, connection:
        account_doctor.write(
            connection,
            (sign_in("claude-default", "box", "signed-out", account_doctor.NOT_LOGGED_IN, method="none",
                     subscription=None),),
            config, None, NOW - 300,
        )
        connection.execute(
            "CREATE TABLE device_slots (job_id TEXT PRIMARY KEY, device TEXT NOT NULL, harness TEXT NOT NULL, "
            "os_user TEXT NOT NULL, placed_at REAL NOT NULL, account TEXT)"
        )
        add_job(connection, "job-17", action="build", recipient="claude", state="running", ticket_id="17")
        connection.execute("INSERT INTO device_slots VALUES (?, ?, ?, ?, ?, ?)",
                           ("job-17", "box", "claude", "claude-bot", NOW - 120, "claude-default"))

    accounts = collect_snapshot(db, tmp_path / "poll.log", now=NOW, machines_path=machines)["accounts"]

    assert [(a["id"], a["signed_in_on"], a["running"]) for a in accounts] == [(
        "claude-default",
        [{"device": "box", "status": "signed-out", "auth_method": "none", "subscription_type": None,
          "checked_at": NOW - 300, "same_login_as": [], "command": "env -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDE_CONFIG_DIR claude auth login",
          "login": "claude-bot"}],
        [{"ticket": "17", "kind": "build", "device": "box"}],
    )]
