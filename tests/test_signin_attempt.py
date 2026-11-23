from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from lib import account_doctor, dashboard, dashboard_accounts, settings_apply, signin_attempt, signin_broker, signin_relay
from lib.account_signin import KILL_GRACE_SECONDS, Outcome, kill_script, first_process_group, settle
from lib.harness_env import ACCOUNT_ISOLATION
from lib.machines_yml import load_machines_yml
from lib.remote import SshTransport
from tests.test_account_doctor import World, claude_reply
from tests.test_account_pair_requests import Box
from tests.test_settings_apply import NOW, OWNER

CLAUDE_URL = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
              "&response_type=code&scope=user%3Aprofile+user%3Ainference&code_challenge=Zx1-_abc&state=Qm9vX2Zvbw")
CODEX_URL = "https://auth.openai.com/codex/device"
CODEX_CODE = "ABCD-12EFG"
EMAIL = "planted.person@example.org"
TOKEN = "sk-ant-oat01-PlantedTokenAbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
FOREIGN = "https://evil.example.net/cai/oauth/authorize?x=1"
PLANTED = (EMAIL, TOKEN, FOREIGN, "evil.example.net", "PlantedToken")
CODE = "Zq9xPastedCode-3141#st4te"
ORG_C = "4b0c6c5e-0000-4000-8000-00000000c003"
EMAIL_C = "bot-three@example.com"
SECURE = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
TOKENS = tuple(name for isolation in ACCOUNT_ISOLATION.values() for name in isolation.tokens)

FAKE_LOGIN = """\
#!{python}
import json, os, select, shutil, sys, time

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
if args != {{"claude": ["auth", "login", "--claudeai"], "codex": ["login", "--device-auth"]}}[name]:
    os.execv({status!r} + "/" + name, [name, *args])
side = {side!r}
with open(f"{{side}}/{{name}}.login-env", "w") as out:
    out.write("argv=" + " ".join(args) + "\\n" + "".join(f"{{k}}={{v}}\\n" for k, v in os.environ.items()))
with open(f"{{side}}/{{name}}.login-pid", "w") as out:
    out.write(str(os.getpid()))
mode = open(f"{{side}}/mode").read().strip()
noise = {noise!r}


def say(*lines):
    sys.stdout.write("\\n".join(lines) + "\\n")
    sys.stdout.flush()


def wait_for(path):
    while not os.path.exists(path):
        time.sleep(0.05)


def signed_in():
    for reply in os.listdir(f"{{side}}/on-login"):
        shutil.copy(f"{{side}}/on-login/{{reply}}", {replies!r} + "/" + name + "/" + reply)


say(*noise)
if mode == "flood":
    say(*["." * 79] * 1024)
if mode in ("silent", "flood"):
    time.sleep(600)
    sys.exit(0)
if name == "codex":
    say("1. Open this link in your browser", "   \\x1b[94m{codex_url}\\x1b[0m",
        "2. Enter this one-time code", "   \\x1b[94m{codex_code}\\x1b[0m", *noise)
    wait_for(f"{{side}}/approved")
    signed_in()
    sys.exit(0)
say("Browser didn't open? Use the url below to sign in:", {claude_url!r}, *noise)
sys.stdout.write("Paste code here if prompted > ")
sys.stdout.flush()
typed = [os.read(0, 4096).decode()]
if os.path.exists(f"{{side}}/hold"):
    wait_for(f"{{side}}/release")
while select.select([0], [], [], 0.5)[0]:
    more = os.read(0, 4096)
    if not more:
        break
    typed.append(more.decode())
with open(f"{{side}}/claude.stdin", "w") as out:
    json.dump(typed, out)
signed_in()
sys.exit(0)
"""


class Attempts:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.tmp = tmp_path
        self.world = World(tmp_path, monkeypatch)
        self.box = Box(self.world, tmp_path)
        self.world.declare("claude-b", "claude-c", "codex-b")
        self.world.profile(f"export LOGNAME=claude-bot USER=claude-bot {SECURE}=/planted/secure\n")
        self.side = tmp_path / "device-side"
        (self.side / "on-login").mkdir(parents=True)
        status = tmp_path / "status-bin"
        status.mkdir()
        for harness in ("claude", "codex"):
            shutil.move(self.world.bin / harness, status / harness)
            fake = self.world.bin / harness
            fake.write_text(FAKE_LOGIN.format(
                python=sys.executable, status=str(status), side=str(self.side),
                replies=str(tmp_path / "replies"), noise=[f"Signed in before as {EMAIL}", f"token {TOKEN}",
                                                          f"or open {FOREIGN}"],
                codex_url=CODEX_URL, codex_code=CODEX_CODE, claude_url=CLAUDE_URL,
            ), encoding="utf-8")
            fake.chmod(0o755)
        for account in ("claude-b", "claude-c", "codex-b"):
            (self.world.device_home / ".factory-accounts" / account).mkdir(parents=True)
        self.relay = tmp_path / "relay"
        self.relay.mkdir()
        self.relay.chmod(0o770)
        self.workers: list[threading.Thread] = []
        self.runner = signin_attempt.Runner(
            relay=signin_relay.LocalRelay(self.box.spool, self.relay), machines=self.world.machines,
            db=self.world.db, transport=SshTransport(), tick_seconds=0.05)
        self.broker = signin_broker.Broker(
            relay=signin_relay.LocalRelay(self.box.spool, self.relay), machines=self.world.machines,
            db=self.world.db, state_dir=tmp_path, approver="maxharris1", owner=OWNER,
            launch=lambda attempt, finish: self.runner.run(attempt, finish),
            claims=settings_apply.claim_lock(self.world.db), transport=SshTransport(),
            clock=lambda: NOW, spawn=self.spawn)
        assert self.world.doctor() == 0

    def spawn(self, work: Callable[[], None]) -> None:
        worker = threading.Thread(target=work, daemon=True)
        self.workers.append(worker)
        worker.start()

    def mode(self, mode: str, **on_login: bytes) -> None:
        (self.side / "mode").write_text(mode, encoding="utf-8")
        for account, reply in on_login.items():
            account = account.replace("_", "-")
            (self.side / "on-login" / account).write_bytes(reply)
            (self.side / "on-login" / f"{account}.code").write_text("0", encoding="utf-8")

    def start(self, account: str = "claude-c") -> str:
        rid = self.box.request("signin", account=account)
        assert self.broker.tick() == []
        return rid

    def view(self, rid: str) -> dict:
        path = self.relay / f"{rid}.relay"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def wait(self, rid: str, done: Callable[[dict], bool], seconds: float = 20) -> dict:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            view = self.view(rid)
            if done(view):
                return view
            time.sleep(0.05)
        raise AssertionError(f"relay view never got there: {self.view(rid)}")

    def drop(self, rid: str, kind: str, body: str = "") -> Path:
        path = self.relay / f"{rid}.{kind}"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o640)
        return path

    def finish(self) -> None:
        for worker in self.workers:
            worker.join(60)
            assert not worker.is_alive(), "the attempt ended"

    def lines(self, rid: str | None = None) -> list[dict]:
        path = self.tmp / settings_apply.AUDIT_NAME
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        return [line for line in lines if rid is None or line["id"] == rid]

    def login_pid(self, harness: str) -> int:
        return int((self.side / f"{harness}.login-pid").read_text(encoding="utf-8"))

    def kill_calls(self) -> list[dict]:
        return [call for call in self.world.ssh.calls() if "orch-signin-kill" in call["script"]]

    def status_json(self) -> str:
        log = self.tmp / "poller.log"
        log.touch()
        snapshot = dashboard.collect_snapshot(self.world.db, log, now=NOW, machines_path=self.world.machines)
        return json.dumps(snapshot)

    def box_side(self) -> dict[str, bytes]:
        device = {self.side, self.world.device_home, self.world.bin, self.tmp / "replies", self.tmp / "status-bin"}
        return {
            str(path.relative_to(self.tmp)): path.read_bytes()
            for path in self.tmp.rglob("*")
            if path.is_file() and not any(path.is_relative_to(folder) for folder in device)
        }


@pytest.fixture
def attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Attempts:
    return Attempts(tmp_path, monkeypatch)


def _group_gone(pgid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        if _zombies_only(pgid):
            return True
        time.sleep(0.05)
    return False


def _zombies_only(pgid: int) -> bool:
    states = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) == pgid:
            states.append(fields[0])
    return all(state == "Z" for state in states)


def _sign_in_claude(attempts: Attempts, rid: str) -> dict:
    view = attempts.wait(rid, lambda view: view.get("state") == "awaiting-code")
    attempts.drop(rid, "code", CODE + "\n")
    attempts.finish()
    return view


def _approve_codex(attempts: Attempts, rid: str) -> dict:
    view = attempts.wait(rid, lambda view: view.get("state") == "awaiting-approval")
    (attempts.side / "approved").touch()
    attempts.finish()
    return view


CLAUDE_OK = claude_reply(org=ORG_C, email=EMAIL_C, subscription="pro")


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_login_runs_over_ssh_tt_as_the_bot_with_the_home_set_and_no_profile_token(
    attempts: Attempts, harness: str,
) -> None:
    profile = (attempts.world.device_home / ".bash_profile").read_text(encoding="utf-8")
    assert all(f"export {name}=sk-planted" in profile for name in TOKENS) and f"{SECURE}=/planted/secure" in profile
    account = f"{harness}-b" if harness == "codex" else "claude-c"
    if harness == "claude":
        attempts.mode("ok", claude_c=CLAUDE_OK)
    else:
        attempts.mode("ok", codex_b=b"Logged in using ChatGPT\n")

    rid = attempts.start(account)
    (_sign_in_claude if harness == "claude" else _approve_codex)(attempts, rid)

    (login,) = [call for call in attempts.world.ssh.calls() if "orch-accounts-login" in call["script"]]
    assert login["tty"] is True
    assert (login["user"], login["alias"]) == (f"{harness}-bot", f"pc-{harness}-bot")
    seen = (attempts.side / f"{harness}.login-env").read_text(encoding="utf-8")
    variables = dict(line.split("=", 1) for line in seen.splitlines() if "=" in line)
    assert variables["argv"] == {"claude": "auth login --claudeai", "codex": "login --device-auth"}[harness]
    config, _ = load_machines_yml(attempts.world.machines)
    assert variables[ACCOUNT_ISOLATION[harness].home] == config.account_home("pc", harness, account)
    assert not set(variables) & {*TOKENS, SECURE}, sorted(set(variables) & {*TOKENS, SECURE})
    assert "sk-planted" not in seen and "/planted/secure" not in seen
    (line,) = attempts.lines(rid)
    assert (line["field"], line["account"], line["result"], line["error"]) == ("signin", account, "ok", None)
    assert (line["previous"], line["new"]) == ("signed-out", "ok")
    assert attempts.view(rid) == {"result": "ok", "error": None}


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_only_the_parsed_url_and_code_leave_the_cli_output(
    attempts: Attempts, harness: str, capfd: pytest.CaptureFixture[str],
) -> None:
    account = "codex-b" if harness == "codex" else "claude-c"
    attempts.mode("ok", **({"claude_c": CLAUDE_OK} if harness == "claude" else
                           {"codex_b": b"Logged in using ChatGPT\n"}))

    rid = attempts.start(account)
    view = (_sign_in_claude if harness == "claude" else _approve_codex)(attempts, rid)

    if harness == "claude":
        assert set(view) == {"state", "url", "expires_at"} and view["url"] == CLAUDE_URL
    else:
        assert set(view) == {"state", "url", "code", "expires_at"}
        assert (view["url"], view["code"]) == (CODEX_URL, CODEX_CODE)
    (line,) = attempts.lines(rid)
    assert line["result"] == "ok"
    out, err = capfd.readouterr()
    surfaces = {**attempts.box_side(), "status.json": attempts.status_json().encode(),
                "stdout": out.encode(), "stderr": err.encode()}
    assert f"relay/{rid}.relay" in surfaces and settings_apply.AUDIT_NAME in surfaces and "ssh.log" in surfaces
    for name, data in surfaces.items():
        for planted in (*PLANTED, CODE):
            assert planted.encode() not in data, f"{planted} reached {name}"
    assert CLAUDE_URL.encode() not in surfaces[settings_apply.AUDIT_NAME]


def test_output_with_no_recognized_prompt_fails_as_unrecognized_output(
    attempts: Attempts, capfd: pytest.CaptureFixture[str],
) -> None:
    assert signin_attempt.UNRECOGNIZED_SECONDS == 30
    attempts.runner.unrecognized_seconds = 0.5
    attempts.mode("silent")

    rid = attempts.start()
    attempts.finish()

    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"], line["new"]) == ("failed", "unrecognized-output", "signed-out")
    assert _group_gone(attempts.login_pid("claude"))
    out, err = capfd.readouterr()
    for name, data in {**attempts.box_side(), "status.json": attempts.status_json().encode(),
                       "stdout": out.encode(), "stderr": err.encode()}.items():
        for planted in PLANTED:
            assert planted.encode() not in data, f"{planted} reached {name}"


def test_the_pasted_code_is_typed_once_and_its_file_removed(
    attempts: Attempts, capfd: pytest.CaptureFixture[str],
) -> None:
    attempts.mode("ok", claude_c=CLAUDE_OK)
    (attempts.side / "hold").touch()
    rid = attempts.start()
    attempts.wait(rid, lambda view: view.get("state") == "awaiting-code")

    first = attempts.drop(rid, "code", CODE + "\n")
    deadline = time.monotonic() + 10
    while first.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not first.exists(), "the broker took the code file"
    second = attempts.drop(rid, "code", "SecondPaste-999")
    while second.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not second.exists(), "a later code file is taken too"
    (attempts.side / "release").touch()
    attempts.finish()

    typed = json.loads((attempts.side / "claude.stdin").read_text(encoding="utf-8"))
    assert [chunk.strip() for chunk in typed] == [CODE]
    assert sorted(path.suffix for path in attempts.relay.iterdir()) == [".relay"]
    (line,) = attempts.lines(rid)
    assert line["result"] == "ok"
    out, err = capfd.readouterr()
    history = json.dumps(dashboard._settings_results(attempts.tmp / settings_apply.AUDIT_NAME, None))
    for name, data in {**attempts.box_side(), "status.json": attempts.status_json().encode(),
                       "history": history.encode(), "stdout": out.encode(), "stderr": err.encode()}.items():
        assert CODE.encode() not in data, f"the code reached {name}"
        assert b"SecondPaste" not in data, f"the second code reached {name}"


def test_the_attempt_expires_after_the_window_and_kills_the_remote_group(attempts: Attempts) -> None:
    assert signin_attempt.EXPIRY_SECONDS == 15 * 60
    attempts.runner.expiry_seconds = 2
    attempts.mode("ok", claude_c=CLAUDE_OK)

    rid = attempts.start()
    view = attempts.wait(rid, lambda view: view.get("state") == "awaiting-code")
    pgid = attempts.login_pid("claude")
    attempts.finish()

    assert view["expires_at"]
    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"], line["new"]) == ("expired", None, "signed-out")
    (kill,) = attempts.kill_calls()
    assert f"kill -TERM -- -{pgid}" in kill["script"]
    assert (kill["user"], kill["alias"]) == ("claude-bot", "pc-claude-bot")
    assert _group_gone(pgid)


def test_a_cancel_file_ends_the_attempt_and_kills_the_remote_group(attempts: Attempts) -> None:
    attempts.mode("ok", codex_b=b"Logged in using ChatGPT\n")
    rid = attempts.start("codex-b")
    attempts.wait(rid, lambda view: view.get("state") == "awaiting-approval")
    pgid = attempts.login_pid("codex")

    attempts.drop(rid, "cancel")
    attempts.finish()

    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"], line["new"]) == ("cancelled", None, "signed-out")
    (kill,) = attempts.kill_calls()
    assert (kill["user"], kill["alias"]) == ("codex-bot", "pc-codex-bot")
    assert f"kill -TERM -- -{pgid}" in kill["script"]
    assert _group_gone(pgid)
    assert not (attempts.relay / f"{rid}.cancel").exists()


def test_a_cancel_waiting_before_the_session_reads_the_marker_still_kills_the_remote_group(
    attempts: Attempts,
) -> None:
    attempts.mode("silent")
    rid = attempts.box.request("signin", account="claude-c")
    attempts.drop(rid, "cancel")
    assert attempts.broker.tick() == []
    attempts.finish()

    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"]) == ("cancelled", None)
    (kill,) = attempts.kill_calls()
    pgid = int(re.search(r"kill -TERM -- -([0-9]+)", kill["script"])[1])
    assert _group_gone(pgid)


def test_output_flooding_past_the_tail_after_the_marker_still_lets_expiry_kill_the_group(attempts: Attempts) -> None:
    attempts.runner.expiry_seconds = 2
    attempts.runner.tick_seconds = 1.5
    attempts.mode("flood")

    rid = attempts.start()
    attempts.finish()

    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"]) == ("expired", None)
    pgid = attempts.login_pid("claude")
    (kill,) = attempts.kill_calls()
    assert f"kill -TERM -- -{pgid}" in kill["script"]
    assert _group_gone(pgid)


def _split_claude_b_across_pc_and_lap(attempts: Attempts) -> None:
    world, lap_home, status = attempts.world, attempts.tmp / "lap-home", attempts.tmp / "status-bin"
    text = world.machines.read_text(encoding="utf-8")
    text = text.replace("{id: claude-b, harness: claude, signed_in_on: [pc]}",
                        "{id: claude-b, harness: claude, signed_in_on: [pc, lap]}")
    before, _, after = text.rpartition(f"home: {world.device_home}")
    world.machines.write_text(f"{before}home: {lap_home}{after}", encoding="utf-8")
    world.ssh.offline("lap-claude-bot").unlink()
    (status / "claude").rename(status / "claude-real")
    (status / "claude").write_text(
        f'#!/bin/sh\ncase "$CLAUDE_CONFIG_DIR" in {lap_home}/*) CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR-lap";; esac\n'
        f'exec {status}/claude-real "$@"\n', encoding="utf-8")
    (status / "claude").chmod(0o755)
    replies = attempts.tmp / "replies" / "claude"
    (replies / "claude-b-lap").write_bytes(claude_reply(org="a-lap-only-org", email="lap@example.invalid"))
    (replies / "claude-b-lap.code").write_text("0", encoding="utf-8")


def test_an_unchanged_split_identity_on_another_account_does_not_blame_the_signin(attempts: Attempts) -> None:
    _split_claude_b_across_pc_and_lap(attempts)
    assert attempts.world.doctor() == 0
    assert attempts.world.statuses()[("claude-b", "pc")] == "mismatch"
    attempts.mode("ok", claude_c=CLAUDE_OK)

    rid = attempts.start()
    _sign_in_claude(attempts, rid)

    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"], line["new"]) == ("ok", None, "ok")


def test_a_login_the_profile_token_answers_is_not_ok(attempts: Attempts) -> None:
    attempts.mode("ok", claude_c=claude_reply(method="oauth_token"))

    rid = attempts.start()
    _sign_in_claude(attempts, rid)

    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"], line["new"]) == ("failed", "token-auth", "signed-out")


def test_a_claude_signin_that_disturbs_another_account_is_mismatch_and_the_card_shows_the_cli(
    attempts: Attempts,
) -> None:
    attempts.mode("swap", claude_c=CLAUDE_OK, claude_b=CLAUDE_OK)

    rid = attempts.start()
    _sign_in_claude(attempts, rid)

    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"], line["new"]) == ("mismatch", "shared-identity", "mismatch")
    _assert_the_pc_card_shows_the_cli(attempts, "claude-c")


def test_a_claude_signin_that_changes_another_accounts_identity_is_mismatch_and_the_card_shows_the_cli(
    attempts: Attempts,
) -> None:
    attempts.mode("swap", claude_c=CLAUDE_OK,
                  claude_b=claude_reply(org="a-distinct-changed-org", email="changed@example.invalid"))

    rid = attempts.start()
    _sign_in_claude(attempts, rid)

    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"], line["new"]) == ("mismatch", "disturbed-account", "mismatch")
    _assert_the_pc_card_shows_the_cli(attempts, "claude-c")


def _assert_the_pc_card_shows_the_cli(attempts: Attempts, account: str) -> None:
    config, _ = load_machines_yml(attempts.world.machines)
    with sqlite3.connect(attempts.world.db) as connection:
        cards = {card["id"]: card for card in dashboard_accounts.read_accounts(connection, NOW, config)}
    (pc,) = [entry for entry in cards[account]["signed_in_on"] if entry["device"] == "pc"]
    assert pc["status"] == "mismatch"
    assert pc["command"] == account_doctor.login_command("claude", config.account_home("pc", "claude", account))


@pytest.mark.parametrize(("mode", "expected"), [
    ("ok", ("ok", None)),
    ("swap", ("mismatch", "shared-identity")),
    ("expired", ("expired", None)),
    ("cancelled", ("cancelled", None)),
    ("silent", ("failed", "unrecognized-output")),
])
def test_each_attempt_ends_in_exactly_one_audit_line(attempts: Attempts, mode: str, expected: tuple) -> None:
    attempts.runner.expiry_seconds = 2 if mode == "expired" else 60
    attempts.runner.unrecognized_seconds = 0.5
    replies = {"claude_c": CLAUDE_OK, **({"claude_b": CLAUDE_OK} if mode == "swap" else {})}
    attempts.mode("silent" if mode == "silent" else "ok", **replies)
    rid = attempts.start()
    if mode in ("ok", "swap"):
        _sign_in_claude(attempts, rid)
    elif mode == "cancelled":
        attempts.wait(rid, lambda view: view.get("state") == "awaiting-code")
        attempts.drop(rid, "cancel")
    attempts.finish()

    assert attempts.broker.tick() == []
    (line,) = attempts.lines()
    assert line["id"] == rid
    assert (line["result"], line["error"]) == expected
    assert set(line) <= {*settings_apply.AuditEntry.__dataclass_fields__}
    text = json.dumps(line)
    for kept in (CLAUDE_URL, "claude.com", CODE, *PLANTED, "Paste code"):
        assert kept not in text
    assert attempts.view(rid) == {"result": expected[0], "error": expected[1]}


@pytest.mark.parametrize(("end", "outcome", "expected"), [
    ("exit", Outcome("ok", None, "ok"), Outcome("ok", None, "ok")),
    ("exit", Outcome("failed", "signed-out", "signed-out"), Outcome("failed", "signed-out", "signed-out")),
    ("expired", Outcome("failed", "signed-out", "signed-out"), Outcome("expired", None, "signed-out")),
    ("cancelled", Outcome("ok", None, "ok"), Outcome("cancelled", None, "ok")),
    ("unrecognized-output", Outcome("failed", "signed-out", "signed-out"),
     Outcome("failed", "unrecognized-output", "signed-out")),
    ("cancelled", Outcome("mismatch", "disturbed-account", "ok"), Outcome("mismatch", "disturbed-account", "ok")),
])
def test_settle_table(end: str, outcome: Outcome, expected: Outcome) -> None:
    assert settle(end, outcome) == expected


def test_only_the_first_group_marker_counts() -> None:
    assert first_process_group(b"profile says hello\r\nORCH-SIGNIN-PGID 4242\r\nORCH-SIGNIN-PGID 1\n") == 4242
    assert first_process_group(b"ORCH-SIGNIN-PGID \r\nhostile ORCH-SIGNIN-PGID 77\n") is None
    assert first_process_group(b"ORCH-SIGNIN-PGID 1\n") is None
    assert first_process_group(b"no marker") is None
    with pytest.raises(ValueError):
        kill_script(1)
    assert f"-lt {KILL_GRACE_SECONDS}" in kill_script(4242)


@pytest.mark.parametrize("end", ["expired", "cancelled"])
def test_a_login_profile_flooding_past_the_tail_before_the_marker_still_lets_the_end_kill_the_group(
    attempts: Attempts, end: str,
) -> None:
    attempts.world.profile(f"export LOGNAME=claude-bot USER=claude-bot\nhead -c 90000 /dev/zero | tr '\\\\0' '.'\n")
    attempts.runner.expiry_seconds = 2
    attempts.mode("silent")

    rid = attempts.start()
    deadline = time.monotonic() + 20
    while not (attempts.side / "claude.login-pid").exists():
        assert time.monotonic() < deadline, "the fake CLI started"
        time.sleep(0.05)
    if end == "cancelled":
        attempts.drop(rid, "cancel")
    attempts.finish()

    (line,) = attempts.lines(rid)
    assert (line["result"], line["error"]) == (end, None)
    pgid = attempts.login_pid("claude")
    (kill,) = attempts.kill_calls()
    assert f"kill -TERM -- -{pgid}" in kill["script"]
    assert _group_gone(pgid)


class _Reads(io.RawIOBase):
    def __init__(self, reads: list[bytes]):
        self._reads = reads

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if not self._reads:
            return 0
        data = self._reads.pop(0)
        buffer[:len(data)] = data
        return len(data)


@pytest.mark.parametrize("reads, group", [
    ([b"." * 4096] * 22 + [b"\r\nORCH-SIG", b"NIN-PGID 42", b"424\r", b"\nlater ORCH-SIGNIN-PGID 77\n"], 42424),
    ([b"." * 4096] * 22 + [b"ORCH-SIGNIN-PGID \r\nhostile ORCH-SIGNIN-PGID 77\n"], None),
    ([b"." * 4096] * 22, None),
])
def test_the_group_marker_is_found_after_any_volume_and_only_the_first_counts(
    reads: list[bytes], group: int | None,
) -> None:
    output = signin_attempt._Output(io.BufferedReader(_Reads(reads), buffer_size=4096))
    assert output.grouped.wait(5)
    assert output.group == group
