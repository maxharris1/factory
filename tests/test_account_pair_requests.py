from __future__ import annotations

import json
import shlex
import sqlite3
import sys
from pathlib import Path

import pytest

from lib import account_doctor, heal, settings_apply
from lib.machines_yml import load_machines_yml
from lib.remote import SshTransport
from lib.settings_apply import Applier, LocalSpool, run_pass
from tests.test_account_doctor import World
from tests.test_settings_apply import NOW, OWNER, Runner, holding

ROOT = Path(__file__).resolve().parent.parent
PREPARE = "accounts prepare --device pc --account claude-c --machines /dev/stdin"


class Box:
    def __init__(self, world: World, tmp_path: Path):
        self.world = world
        world.declare("claude-b", "claude-c")
        text = world.machines.read_text(encoding="utf-8").replace("/srv/factory", str(ROOT))
        world.machines.write_text(text, encoding="utf-8")
        python = world.bin / "python3"
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        world.profile("export LOGNAME=claude-bot USER=claude-bot\n")
        sqlite3.connect(world.db).close()
        self.machines = world.machines
        self.original = self.machines.read_bytes()
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.spool = tmp_path / "spool"
        self.spool.mkdir()
        self.spool.chmod(0o755)
        self.runner = Runner(None)
        self.applier = Applier(
            machines=self.machines, state_dir=self.state, deploy=tmp_path / "deploy",
            approver="maxharris1", environ={}, restart=("poller",), pending=(),
            claims=holding({}), run=self.runner, clock=lambda: NOW,
            db=world.db, transport=SshTransport(), repo=None,
        )
        self.count = 0

    def request(self, op: str, account: str = "claude-c", device: str = "pc",
                actor: str = "maxharris1", **extra: object) -> str:
        self.count += 1
        rid = f"pair-{self.count:04d}-abcdefghijk"
        body = {"id": rid, "actor": actor, "requested_at": "2026-11-22T10:00:00Z",
                "target": "account", "op": op, "account": account, "device": device, **extra}
        path = self.spool / f"{rid}.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        path.chmod(0o644)
        return rid

    def run(self) -> list[settings_apply.AuditEntry]:
        return run_pass(self.applier, LocalSpool(self.spool), OWNER)

    def audit(self) -> list[dict]:
        path = self.state / settings_apply.AUDIT_NAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def untouched(self) -> None:
        assert self.machines.read_bytes() == self.original, "machines.yml is byte-identical"
        assert sorted(self.machines.parent.glob("machines.yml.bak-settings-*")) == [], "no backup"
        assert self.runner.calls == [], "nothing restarted"

    def prepare_calls(self) -> list[dict]:
        return [call for call in self.world.ssh.calls() if "accounts prepare" in call["script"]]


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Box:
    return Box(World(tmp_path, monkeypatch), tmp_path)


def test_prepare_runs_orch_over_ssh_as_the_bot_with_machines_on_stdin_then_the_doctor(
    box: Box,
) -> None:
    rid = box.request("prepare")

    (entry,) = box.run()

    (call,) = box.prepare_calls()
    assert (call["alias"], call["user"]) == ("pc-claude-bot", "claude-bot")
    assert call["script"].startswith("exec bash -lc ")
    assert f"{ROOT}/bin/orch {PREPARE}" in call["script"]
    folder = box.world.device_home / ".factory-accounts" / "claude-c"
    assert folder.is_dir(), "the device's orch read the host's machines.yml from stdin"
    assert (folder.stat().st_mode & 0o777) == 0o700
    assert box.world.statuses()[("claude-c", "pc")] == "signed-out", "the doctor ran after"
    (line,) = box.audit()
    assert {k: line[k] for k in ("id", "actor", "applied_at", "target", "account", "device",
                                 "field", "previous", "new", "result", "error", "backup",
                                 "restarted", "pending", "version")} == {
        "id": rid, "actor": "maxharris1", "applied_at": heal.stamp(NOW), "target": "account",
        "account": "claude-c", "device": "pc", "field": "prepare", "previous": None,
        "new": "signed-out", "result": "applied", "error": None, "backup": None,
        "restarted": [], "pending": [], "version": None}
    assert entry.new == "signed-out"
    box.untouched()


def test_doctor_prepares_the_pair_before_the_doctor_and_records_its_status(box: Box) -> None:
    rid = box.request("doctor")

    (entry,) = box.run()

    calls = box.world.ssh.calls()
    (call,) = box.prepare_calls()
    assert calls[0] == call and len(calls) > 1, "one prepare over ssh, then the doctor's probes"
    assert (call["alias"], call["user"]) == ("pc-claude-bot", "claude-bot")
    assert f"{ROOT}/bin/orch {PREPARE}" in call["script"]
    assert (box.world.device_home / ".factory-accounts" / "claude-c").is_dir()
    assert (entry.id, entry.field, entry.previous, entry.new, entry.result, entry.error) == (
        rid, "doctor", None, "signed-out", "applied", None)
    assert len(box.audit()) == 1
    box.untouched()


def test_doctor_records_the_pairs_status_before_and_after(box: Box) -> None:
    box.request("doctor", account="claude-b")
    box.request("doctor", account="claude-b")

    first, second = box.run()

    assert len(box.prepare_calls()) == 2
    assert (first.field, first.previous, first.new, first.result) == ("doctor", None, "ok", "applied")
    assert (second.previous, second.new) == ("ok", "ok")
    assert len(box.audit()) == 2
    assert box.world.statuses()[("claude-b", "pc")] == "ok"
    box.untouched()


def test_the_devices_python_over_ssh_runs_in_this_interpreters_environment(box: Box) -> None:
    config, _ = load_machines_yml(box.machines)
    script = "exec bash -lc " + shlex.quote("""python3 -c 'print("prefix=" + __import__("sys").prefix)'""")

    exchanged = SshTransport().exchange(
        account_doctor.ssh_argv(config.device("pc"), "claude", script), timeout=30)

    assert exchanged.code == 0, exchanged.stderr
    assert exchanged.stdout.decode().rpartition("prefix=")[2].strip() == sys.prefix


def test_a_prepare_the_device_refuses_records_failed_and_still_runs_the_doctor(box: Box) -> None:
    box.world.profile("export LOGNAME=someone-else USER=someone-else\n")
    box.request("prepare")

    (entry,) = box.run()

    assert entry.result == "failed"
    assert entry.error.startswith("prepare exited 2: orch accounts prepare: refused:"), entry.error
    assert entry.new == "signed-out"
    assert box.world.statuses()[("claude-c", "pc")] == "signed-out"
    box.untouched()


def test_a_prepare_that_cannot_reach_the_device_records_its_unreachable_status(box: Box) -> None:
    box.world.ssh.offline("pc-claude-bot").touch()
    box.request("prepare")

    (entry,) = box.run()

    assert (entry.result, entry.new) == ("failed", "unreachable")
    assert entry.error.startswith("prepare exited 255"), entry.error
    box.untouched()


def test_a_pair_with_no_ssh_path_is_never_prepared_and_reads_the_doctors_status(box: Box) -> None:
    box.request("prepare", account="claude-default", device="box")

    (entry,) = box.run()

    assert box.prepare_calls() == []
    assert (entry.result, entry.new) == ("failed", "unreachable")
    assert entry.error == "no ssh_alias for claude on box"
    box.untouched()


def test_profile_output_on_stderr_never_reaches_the_audit(box: Box) -> None:
    box.world.profile("export LOGNAME=nobody USER=nobody\necho sk-planted-stderr >&2\n")
    box.request("prepare")

    box.run()

    assert "sk-planted" not in (box.state / settings_apply.AUDIT_NAME).read_text(encoding="utf-8")


@pytest.mark.parametrize("op, extra, error", [
    ("prepare", {"actor": "claude-bot"}, "not-approver"),
    ("doctor", {"actor": "someone"}, "not-approver"),
    ("prepare", {"account": "nobody"}, "unknown-account"),
    ("prepare", {"device": "nowhere"}, "unknown-device"),
    ("prepare", {"device": "lap"}, "claude-c is not signed in on lap"),
], ids=["bot", "not-approver", "unknown-account", "unknown-device", "not-signed-in"])
def test_a_refused_pair_request_runs_nothing(box: Box, op: str, extra: dict, error: str) -> None:
    box.request(op, **extra)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", error)
    assert box.world.ssh.calls() == [], "no ssh call"
    with sqlite3.connect(box.world.db) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master").fetchall()
    assert tables == [], "no doctor pass"
    box.untouched()


@pytest.mark.parametrize("extra", [{"field": "max_jobs"}, {"expect": None}, {"value": 1}])
def test_a_pair_request_with_extra_keys_is_bad_keys(box: Box, extra: dict) -> None:
    box.request("doctor", **extra)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "bad-keys")
    assert box.world.ssh.calls() == []


def test_a_set_request_naming_a_device_is_bad_keys(box: Box) -> None:
    box.request("set", account="claude-b", field="max_jobs", expect=None, value=2)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "bad-keys")


def test_without_a_database_a_pair_request_is_refused(box: Box, tmp_path: Path) -> None:
    from dataclasses import replace

    box.applier = replace(box.applier, db=tmp_path / "missing.db")
    box.request("doctor", account="claude-b")

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "no orch.db to record the doctor in")
    assert box.world.ssh.calls() == []
    assert not (tmp_path / "missing.db").exists()


def test_the_cli_line_names_the_pair(box: Box) -> None:
    box.request("doctor", account="claude-b")

    (entry,) = box.run()

    assert settings_apply.describe(entry) == (
        f"settings: {entry.id} applied account claude-b@pc.doctor null -> \"ok\"")


def test_the_doctor_row_is_the_one_the_audit_names(box: Box) -> None:
    box.request("doctor", account="claude-c")

    (entry,) = box.run()

    with sqlite3.connect(box.world.db) as connection:
        (status,) = connection.execute(
            f"SELECT status FROM {account_doctor.TABLE} WHERE account = ? AND device = ?",
            ("claude-c", "pc")).fetchone()
    assert entry.new == status == "signed-out"
