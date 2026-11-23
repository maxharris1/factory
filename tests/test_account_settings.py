"""Account `set` requests for `max_jobs` and `paused`, applied by `orch settings apply` (#1358)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lib import account_settings, heal, live_settings, settings_apply
from lib.live_settings import AccountSwitch
from lib.queue import SQLiteJobQueue
from lib.settings_apply import Applier, LocalSpool, run_pass
from tests.pool_helpers import poller_adopts
from tests.test_settings_apply import ALL, NOW, OWNER, Runner, changed_lines, holding

MACHINES = """\
# machines.yml schema v1. Hand-edited; comments stay.
orch_host: box
devices:
  # The orch host.
  - name: box
    local: true
    slot_max: 6  # memory is tight
    home: /home/{bot}
    harnesses:
      claude: claude-bot
      codex: codex-bot
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 3
    harnesses: {claude: claude-bot}
    workdir_root: /home/{bot}/work
    ssh_alias: pc-{bot}
    factory_root: /srv/factory
    home: /home/{bot}
accounts:
  # Max's second Claude seat.
  - id: work
    harness: claude
    signed_in_on: [box]
    max_jobs: 2  # leave headroom
  - id: spare
    harness: claude
    signed_in_on: [pc]
    paused: true
  - id: api
    harness: external
    max_jobs: 4
"""


class Box:
    def __init__(self, tmp_path: Path, *, codes: list[int] | None = None, claims=None):
        self.machines = tmp_path / "checkout" / "machines.yml"
        self.machines.parent.mkdir()
        self.machines.write_text(MACHINES, encoding="utf-8")
        self.original = self.machines.read_bytes()
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.deploy = tmp_path / "deploy"
        self.spool = tmp_path / "spool"
        self.spool.mkdir()
        self.spool.chmod(0o755)
        self.runner = Runner(codes)
        self.applier = Applier(
            machines=self.machines, state_dir=self.state, deploy=self.deploy,
            approver="maxharris1", environ={}, restart=ALL[:3],
            pending=("waiter-cursor-bot",), claims=claims or holding({}),
            run=self.runner, clock=lambda: NOW,
        )
        self.count = 0

    def write(self, body: dict) -> str:
        self.count += 1
        rid = f"account-{self.count:04d}-abcdefghij"
        path = self.spool / f"{rid}.json"
        path.write_text(json.dumps({"id": rid, **body}), encoding="utf-8")
        path.chmod(0o644)
        return rid

    def request(self, account="work", field="max_jobs", expect=2, value=5,
                actor="maxharris1", **extra) -> str:
        return self.write({"actor": actor, "requested_at": "2026-11-22T10:00:00Z",
                           "target": "account", "op": "set", "account": account,
                           "field": field, "expect": expect, "value": value, **extra})

    def run(self):
        return run_pass(self.applier, LocalSpool(self.spool), OWNER)

    def audit(self) -> list[dict]:
        path = self.state / settings_apply.AUDIT_NAME
        return [json.loads(line) for line in path.read_text().splitlines()]

    def backups(self) -> list[Path]:
        return sorted(self.machines.parent.glob("machines.yml.bak-settings-*"))

    def unchanged(self) -> None:
        assert self.machines.read_bytes() == self.original, "machines.yml is byte-identical"
        assert self.backups() == [], "no backup on a refusal"
        assert self.runner.calls == [], "nothing restarted"


def test_setting_max_jobs_edits_one_line_backs_up_restarts_and_audits(tmp_path: Path) -> None:
    box = Box(tmp_path)
    rid = box.request()

    (entry,) = box.run()

    assert entry.result == "pending", entry
    (line,) = box.audit()
    assert {k: line[k] for k in ("id", "actor", "requested_at", "applied_at", "target",
                                 "account", "device", "field", "previous", "new", "result",
                                 "error")} == {
        "id": rid, "actor": "maxharris1", "requested_at": "2026-11-22T10:00:00Z",
        "applied_at": heal.stamp(NOW), "target": "account", "account": "work",
        "device": None, "field": "max_jobs", "previous": 2, "new": 5, "result": "pending",
        "error": None}
    assert line["restarted"] == ["poller", "waiter-claude-bot", "waiter-codex-bot"]
    assert line["pending"] == ["waiter-cursor-bot"]
    (backup,) = box.backups()
    assert backup.name == line["backup"]
    assert backup.read_bytes() == box.original
    assert changed_lines(MACHINES, box.machines.read_text()) == [
        ("    max_jobs: 2  # leave headroom", "    max_jobs: 5  # leave headroom")]
    assert box.runner.argvs == [
        [str(box.deploy / "poller-service.sh"), "restart"],
        [str(box.deploy / "waiter-service.sh"), "claude-bot", "restart"],
        [str(box.deploy / "waiter-service.sh"), "codex-bot", "restart"],
    ]


def test_setting_max_jobs_where_none_is_set_inserts_one_line(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request(account="spare", expect=None, value=1)

    (entry,) = box.run()

    assert (entry.result, entry.previous, entry.new) == ("pending", None, 1), entry
    after = box.machines.read_text()
    assert changed_lines(MACHINES, after) == [(None, "    max_jobs: 1")]
    assert after.splitlines()[after.splitlines().index("  - id: spare") + 1] == "    max_jobs: 1"


def test_pausing_writes_paused_true_and_nothing_else(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request(field="paused", expect=False, value=True)

    (entry,) = box.run()

    assert (entry.result, entry.previous, entry.new) == ("pending", False, True), entry
    after = box.machines.read_text()
    assert changed_lines(MACHINES, after) == [(None, "    paused: true")]
    for line in MACHINES.splitlines():
        if line.lstrip().startswith("#"):
            assert line in after.splitlines()


def test_resuming_deletes_the_paused_line(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request(account="spare", field="paused", expect=True, value=False)

    (entry,) = box.run()

    assert (entry.result, entry.previous, entry.new) == ("pending", True, False), entry
    after = box.machines.read_text()
    assert after == MACHINES.replace("    signed_in_on: [pc]\n    paused: true\n",
                                     "    signed_in_on: [pc]\n")
    (backup,) = box.backups()
    assert backup.read_bytes() == box.original


def test_a_pause_reaches_the_live_row_under_the_claim_lock(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=lambda: 100.0)
    box = Box(tmp_path, claims=settings_apply.claim_lock(db))
    poller_adopts(queue, box.machines)
    box.request(field="paused", expect=False, value=True)

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert isinstance(entry.version, int)
    connection = sqlite3.connect(db)
    try:
        live = live_settings.peek(connection)
    finally:
        connection.close()
    assert live.written == entry.version
    assert live.accounts["work"] == AccountSwitch(paused=True, max_jobs=2)
    assert live.accounts["spare"] == AccountSwitch(paused=True, max_jobs=None)


@pytest.mark.parametrize("actor", ["claude-bot", "grok", "x[bot]", "maxharris1[bot]", "someone-else"])
def test_a_bot_or_anyone_but_the_approver_is_refused(tmp_path: Path, actor: str) -> None:
    box = Box(tmp_path)
    box.request(actor=actor)

    (entry,) = box.run()

    assert (entry.result, entry.error, entry.account) == ("refused", "not-approver", "work")
    box.unchanged()


@pytest.mark.parametrize("value", [0, 65, -1, True, "5", None, 5.0])
def test_max_jobs_outside_1_to_64_is_refused(tmp_path: Path, value: object) -> None:
    box = Box(tmp_path)
    box.request(value=value)

    (entry,) = box.run()

    assert (entry.result, entry.error, entry.previous) == ("refused", "bad-value", 2)
    box.unchanged()


def test_max_jobs_64_is_the_highest_accepted(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request(value=64)
    assert box.run()[0].result == "pending"


@pytest.mark.parametrize(
    ("request_args", "error"),
    [
        ({"account": "nobody"}, "unknown-account"),
        ({"account": "claude-default", "expect": None}, "read-only"),
        ({"account": "codex-default", "field": "paused", "expect": False, "value": True},
         "read-only"),
        ({"account": "api", "expect": 4}, "read-only"),
        ({"expect": 3}, "changed"),
        ({"field": "paused", "expect": True, "value": False}, "changed"),
        ({"value": 2}, "no-change"),
        ({"field": "paused", "expect": False, "value": "true"}, "bad-value"),
        ({"field": "signed_in_on", "expect": ["box"], "value": ["pc"]}, "bad-field"),
        ({"field": "harness", "expect": "claude", "value": "codex"}, "bad-field"),
    ],
)
def test_an_unknown_read_only_stale_or_bad_request_is_refused(
    tmp_path: Path, request_args: dict, error: str
) -> None:
    box = Box(tmp_path)
    box.request(**request_args)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", error), entry
    box.unchanged()


def test_a_declared_default_account_is_still_read_only(tmp_path: Path) -> None:
    box = Box(tmp_path)
    text = MACHINES + "  - id: claude-default\n    harness: claude\n    max_jobs: 3\n"
    box.machines.write_text(text, encoding="utf-8")
    box.original = box.machines.read_bytes()
    box.request(account="claude-default", expect=3, value=4)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "read-only"), entry
    box.unchanged()


def test_a_key_machines_local_yml_sets_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY", raising=False)
    box = Box(tmp_path)
    (box.machines.parent / "machines.local.yml").write_text(
        "accounts:\n  - id: work\n    max_jobs: 3\n", encoding="utf-8")
    box.request(expect=3)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "max_jobs is set in machines.local.yml")
    box.unchanged()


def test_a_key_the_overlay_leaves_alone_still_applies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY", raising=False)
    box = Box(tmp_path)
    (box.machines.parent / "machines.local.yml").write_text(
        "accounts:\n  - id: work\n    max_jobs: 3\n", encoding="utf-8")
    box.request(field="paused", expect=False, value=True)
    assert box.run()[0].result == "pending"


def test_an_edit_whose_parse_changes_more_than_the_field_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    box = Box(tmp_path)
    real = account_settings.edit_account_field

    def sloppy(text: str, account: str, key: str, value: object) -> str | None:
        edited = real(text, account, key, value)
        return None if edited is None else edited.replace("max_jobs: 4", "max_jobs: 7")

    monkeypatch.setattr(account_settings, "edit_account_field", sloppy)
    box.request()

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "the edit changed more than that field")
    box.unchanged()


def test_an_edit_that_touches_a_device_is_refused(tmp_path: Path, monkeypatch) -> None:
    box = Box(tmp_path)
    real = account_settings.edit_account_field

    def sloppy(text: str, account: str, key: str, value: object) -> str | None:
        edited = real(text, account, key, value)
        return None if edited is None else edited.replace("slot_max: 3", "slot_max: 2")

    monkeypatch.setattr(account_settings, "edit_account_field", sloppy)
    box.request(field="paused", expect=False, value=True)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "the edit changed more than that field")
    box.unchanged()


def test_an_account_written_as_a_flow_mapping_is_refused_not_mangled(tmp_path: Path) -> None:
    box = Box(tmp_path)
    text = MACHINES.replace("  - id: api\n    harness: external\n    max_jobs: 4\n",
                            "  - {id: api2, harness: claude, signed_in_on: [pc], max_jobs: 4}\n")
    box.machines.write_text(text, encoding="utf-8")
    box.original = box.machines.read_bytes()
    box.request(account="api2", expect=4, value=5)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "cannot edit the account's block"), entry
    box.unchanged()


def test_a_failed_restart_restores_the_backup_and_records_failed(tmp_path: Path) -> None:
    box = Box(tmp_path, codes=[0, 1])
    box.request(field="paused", expect=False, value=True)

    (entry,) = box.run()

    assert entry.result == "failed"
    (backup,) = box.backups()
    assert entry.error == f"restart waiter-claude-bot exited 1; restored {backup.name}"
    assert box.machines.read_bytes() == box.original, "the old file is back"
    assert backup.read_bytes() == box.original
    assert [line["result"] for line in box.audit()] == ["failed"]


def test_a_second_pass_applies_nothing_again(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request()
    assert [e.result for e in box.run()] == ["pending"]

    assert box.run() == []
    assert len(box.audit()) == 1
    assert len(box.backups()) == 1


@pytest.mark.parametrize(
    ("extra", "drop", "error"),
    [
        ({"device": "box"}, (), "bad-keys"),
        ({}, ("expect",), "bad-keys"),
        ({"op": "rename"}, (), "bad-op"),
        ({"op": None}, (), "bad-op"),
        ({"target": "device"}, (), "bad-target"),
        ({"account": 7}, (), "bad-account"),
        ({"field": ["max_jobs"]}, (), "bad-field"),
    ],
)
def test_a_malformed_account_request_is_refused_by_its_file_name(
    tmp_path: Path, extra: dict, drop: tuple[str, ...], error: str
) -> None:
    box = Box(tmp_path)
    body = {"actor": "maxharris1", "requested_at": "2026-11-22T10:00:00Z", "target": "account",
            "op": "set", "account": "work", "field": "max_jobs", "expect": 2, "value": 5, **extra}
    for key in drop:
        del body[key]
    rid = box.write(body)

    (entry,) = box.run()

    assert (entry.id, entry.result, entry.error) == (rid, "refused", error)
    assert (entry.actor, entry.account, entry.new) == (None, None, None), "nothing echoed"
    box.unchanged()


def test_a_device_request_in_a_file_with_accounts_edits_only_the_device(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.write({"actor": "maxharris1", "requested_at": "2026-11-22T10:00:00Z",
               "device": "pc", "field": "slot_max", "expect": 3, "value": 2})

    (entry,) = box.run()

    assert (entry.result, entry.target, entry.account, entry.device) == (
        "pending", None, None, "pc"), entry
    assert changed_lines(MACHINES, box.machines.read_text()) == [
        ("    slot_max: 3", "    slot_max: 2")]


def test_the_cli_line_names_the_account(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request()

    (entry,) = box.run()

    assert settings_apply.describe(entry).startswith(
        f"settings: {entry.id} pending account work.max_jobs 2 -> 5")
