from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib import settings_apply
from lib.machines_yml import load_machines_yml
from lib.queue import SQLiteJobQueue
from tests.pool_helpers import poller_adopts
from tests.test_account_settings import MACHINES, Box, _audit_result

NEW_BLOCK = "  - id: fresh\n    harness: codex\n    signed_in_on: [box]\n    max_jobs: 2\n"
SPARE_BLOCK = "  - id: spare\n    harness: claude\n    signed_in_on: [pc]\n    paused: true\n"
SPARE_SHOWN = {"harness": "claude", "max_jobs": None, "paused": True, "signed_in_on": ["pc"]}
WORK_SHOWN = {"harness": "claude", "max_jobs": 2, "paused": True, "signed_in_on": ["box"]}
NO_ACCOUNTS = MACHINES[:MACHINES.index("accounts:\n")]


def _base(box: Box, text: str) -> None:
    box.machines.write_text(text, encoding="utf-8")
    box.original = box.machines.read_bytes()


def _add(box: Box, account="fresh", harness="codex", max_jobs=2, signed_in_on=("box",),
         **extra) -> str:
    return box.write({"actor": "maxharris1", "requested_at": "2026-11-22T10:00:00Z",
                      "target": "account", "op": "add", "account": account,
                      "harness": harness, "max_jobs": max_jobs,
                      "signed_in_on": list(signed_in_on) if isinstance(signed_in_on, tuple)
                      else signed_in_on, **extra})


def _remove(box: Box, account="spare", expect=None, **extra) -> str:
    return box.write({"actor": "maxharris1", "requested_at": "2026-11-22T10:00:00Z",
                      "target": "account", "op": "remove", "account": account,
                      "expect": SPARE_SHOWN if expect is None else expect, **extra})


def _devices(box: Box, account="work", expect=("box",), value=("box", "pc")) -> str:
    return box.request(account=account, field="signed_in_on",
                       expect=list(expect) if isinstance(expect, tuple) else expect,
                       value=list(value) if isinstance(value, tuple) else value)


def _applied_once(box: Box, entry, field: str) -> None:
    assert entry.result == "pending", entry
    (line,) = box.audit()
    assert (line["target"], line["field"], line["result"]) == ("account", field, "pending")
    (backup,) = box.backups()
    assert backup.name == line["backup"]
    assert backup.read_bytes() == box.original
    assert line["restarted"] == ["poller", "waiter-claude-bot", "waiter-codex-bot"]


def test_add_appends_one_block_after_the_last_account(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _add(box)

    (entry,) = box.run()

    _applied_once(box, entry, "add")
    assert entry.account == "fresh"
    assert box.machines.read_text() == MACHINES + NEW_BLOCK


def test_add_creates_the_accounts_block_at_eof_when_absent(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _base(box, NO_ACCOUNTS)
    _add(box)

    (entry,) = box.run()

    _applied_once(box, entry, "add")
    assert box.machines.read_text() == NO_ACCOUNTS + "accounts:\n" + NEW_BLOCK


def test_add_to_a_file_without_a_final_newline_keeps_every_line(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _base(box, NO_ACCOUNTS.rstrip("\n"))
    _add(box)

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert box.machines.read_text() == NO_ACCOUNTS + "accounts:\n" + NEW_BLOCK


def test_add_replaces_an_empty_accounts_list_keeping_its_comment(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _base(box, NO_ACCOUNTS + "accounts: []  # none yet\n")
    _add(box)

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert box.machines.read_text() == (
        NO_ACCOUNTS + "accounts:  # none yet\n" + NEW_BLOCK)


def test_add_without_a_cap_writes_no_max_jobs_line(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _add(box, account="solo", harness="claude", max_jobs=None, signed_in_on=("pc",))

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert box.machines.read_text() == (
        MACHINES + "  - id: solo\n    harness: claude\n    signed_in_on: [pc]\n")


def test_add_signed_in_nowhere_is_refused_by_the_schema(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _add(box, account="solo", harness="claude", max_jobs=None, signed_in_on=())

    (entry,) = box.run()

    assert entry.result == "refused", entry
    assert entry.error.startswith("invalid after the edit: "), entry
    box.unchanged()


def test_add_quotes_an_id_yaml_would_read_as_another_type(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _add(box, account="1e3", harness="claude", max_jobs=None, signed_in_on=("pc",))

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert box.machines.read_text() == (
        MACHINES + '  - id: "1e3"\n    harness: claude\n    signed_in_on: [pc]\n')


ON_DEVICE = MACHINES.replace("accounts:\n", (
    "  - name: \"on\"\n    tailscale_name: on.example.ts.net\n    slot_max: 1\n"
    "    harnesses: {claude: claude-bot}\n    workdir_root: /home/{bot}/work\n"
    "    ssh_alias: on-{bot}\n    factory_root: /srv/factory\n    home: /home/{bot}\n"
    "accounts:\n"))


@pytest.mark.parametrize(
    ("request_it", "account", "line"),
    [(lambda box: _add(box, account="solo", harness="claude", max_jobs=None,
                       signed_in_on=("on",)), "solo", '    signed_in_on: ["on"]\n'),
     (lambda box: _devices(box, value=("box", "on")), "work",
      '    signed_in_on: [box, "on"]\n')],
    ids=["add", "set"],
)
def test_device_names_yaml_would_read_as_another_type_stay_names(
        tmp_path: Path, request_it, account: str, line: str) -> None:
    box = Box(tmp_path)
    _base(box, ON_DEVICE)
    request_it(box)

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert line in box.machines.read_text()
    config, _ = load_machines_yml(box.machines)
    assert "on" in config.account(account).signed_in_on


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"account": "bad id"}, "bad-id"),
        ({"account": ".."}, "bad-id"),
        ({"account": "x" * 65}, "bad-id"),
        ({"account": ""}, "bad-id"),
        ({"account": "work"}, "exists"),
        ({"account": "api"}, "exists"),
        ({"account": "codex-default"}, "read-only"),
        ({"harness": "gemini"}, "unknown-harness"),
        ({"harness": "external", "signed_in_on": ()}, "unknown-harness"),
        ({"harness": None}, "unknown-harness"),
        ({"signed_in_on": ("laptop",)}, "unknown-device"),
        ({"signed_in_on": ("box", "pc")}, "harness-not-on-device"),
        ({"max_jobs": 0}, "bad-value"),
        ({"max_jobs": 65}, "bad-value"),
        ({"max_jobs": "2"}, "bad-value"),
        ({"signed_in_on": "box"}, "bad-value"),
        ({"signed_in_on": ("box", "box")}, "bad-value"),
        ({"signed_in_on": [1]}, "bad-value"),
    ],
)
def test_a_bad_add_is_refused_and_writes_nothing(tmp_path: Path, args: dict, error: str) -> None:
    box = Box(tmp_path)
    _add(box, **args)

    (entry,) = box.run()

    assert (entry.result, entry.error, entry.field) == ("refused", error, "add"), entry
    box.unchanged()


def test_setting_signed_in_on_rewrites_one_line(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _devices(box)

    (entry,) = box.run()

    _applied_once(box, entry, "signed_in_on")
    (line,) = box.audit()
    assert (line["previous"], line["new"]) == (["box"], ["box", "pc"])
    assert box.machines.read_text() == MACHINES.replace(
        "    signed_in_on: [box]\n", "    signed_in_on: [box, pc]\n")


def test_expect_matches_signed_in_on_in_any_order(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _base(box, MACHINES.replace("signed_in_on: [box]", "signed_in_on: [pc, box]"))
    _devices(box, expect=("box", "pc"), value=("pc",))

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert "    signed_in_on: [pc]\n" in box.machines.read_text()


def test_emptying_signed_in_on_is_refused_by_the_schema(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _devices(box, value=())

    (entry,) = box.run()

    assert entry.result == "refused", entry
    assert entry.error.startswith("invalid after the edit: "), entry
    box.unchanged()


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"value": ("box", "laptop")}, "unknown-device"),
        ({"expect": ("pc",)}, "changed"),
        ({"value": ("box",)}, "no-change"),
        ({"value": "box"}, "bad-value"),
        ({"value": ("box", "box")}, "bad-value"),
        ({"expect": None}, "bad-value"),
    ],
)
def test_a_bad_signed_in_on_is_refused(tmp_path: Path, args: dict, error: str) -> None:
    box = Box(tmp_path)
    _devices(box, **args)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", error), entry
    box.unchanged()


def test_signed_in_on_a_device_without_the_harness_is_refused(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _base(box, MACHINES + "  - id: cx\n    harness: codex\n    signed_in_on: [box]\n")
    _devices(box, account="cx")

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "harness-not-on-device"), entry
    box.unchanged()


def test_removing_a_paused_idle_account_deletes_its_block(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _remove(box)

    (entry,) = box.run()

    _applied_once(box, entry, "remove")
    assert box.machines.read_text() == MACHINES.replace(SPARE_BLOCK, "")


def test_removing_keeps_the_blocks_comments_as_comment_lines(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _base(box, MACHINES.replace("    max_jobs: 2  # leave headroom\n",
                                "    max_jobs: 2  # leave headroom\n    paused: true\n"))
    _remove(box, account="work", expect=WORK_SHOWN)

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert box.machines.read_text() == MACHINES.replace(
        "  - id: work\n    harness: claude\n    signed_in_on: [box]\n"
        "    max_jobs: 2  # leave headroom\n",
        "    # leave headroom\n")


def test_removing_the_last_account_writes_an_empty_list(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _base(box, NO_ACCOUNTS + "accounts:  # seats\n" + SPARE_BLOCK)
    _remove(box)

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert box.machines.read_text() == NO_ACCOUNTS + "accounts: []  # seats\n"


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"account": "work", "expect": {**WORK_SHOWN, "paused": False}}, "not-paused"),
        ({"expect": {**SPARE_SHOWN, "max_jobs": 3}}, "changed"),
        ({"expect": {**SPARE_SHOWN, "signed_in_on": ["box"]}}, "changed"),
        ({"expect": {"harness": "claude"}}, "bad-value"),
        ({"expect": ["spare"]}, "bad-value"),
        ({"account": "nobody"}, "unknown-account"),
        ({"account": "api"}, "read-only"),
        ({"account": "claude-default"}, "read-only"),
    ],
)
def test_a_bad_remove_is_refused(tmp_path: Path, args: dict, error: str) -> None:
    box = Box(tmp_path)
    _remove(box, **args)

    (entry,) = box.run()

    assert (entry.result, entry.error, entry.field) == ("refused", error, "remove"), entry
    box.unchanged()


@pytest.mark.parametrize(("body", "error"), [
    ({"op": "add", "harness": "codex", "max_jobs": 2}, "bad-keys"),
    ({"op": "add", "harness": "codex", "max_jobs": 2, "signed_in_on": [], "expect": None},
     "bad-keys"),
    ({"op": "remove"}, "bad-keys"),
    ({"op": "remove", "expect": SPARE_SHOWN, "value": None}, "bad-keys"),
])
def test_an_add_or_remove_with_the_wrong_keys_is_refused(
    tmp_path: Path, body: dict, error: str
) -> None:
    box = Box(tmp_path)
    box.write({"actor": "maxharris1", "requested_at": "2026-11-22T10:00:00Z",
               "target": "account", "account": "spare", **body})

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", error), entry
    box.unchanged()


def test_a_failed_restart_after_an_add_restores_the_file(tmp_path: Path) -> None:
    box = Box(tmp_path, codes=[0, 1])
    _add(box)

    (entry,) = box.run()

    assert entry.result == "failed", entry
    (backup,) = box.backups()
    assert entry.error == f"restart waiter-claude-bot exited 1; restored {backup.name}"
    assert box.machines.read_bytes() == box.original


class ClaimDb:
    def __init__(self, tmp_path: Path, box: Box):
        self.db = tmp_path / "orch.db"
        poller_adopts(SQLiteJobQueue(self.db, clock=lambda: 100.0), box.machines)
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS device_slots (job_id TEXT PRIMARY KEY, "
                "device TEXT NOT NULL, harness TEXT NOT NULL, os_user TEXT NOT NULL, "
                "placed_at REAL NOT NULL)")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(device_slots)")}
            if "account" not in columns:
                connection.execute("ALTER TABLE device_slots ADD COLUMN account TEXT")
            connection.commit()
        finally:
            connection.close()
        box.applier = replace(box.applier, claims=settings_apply.claim_lock(self.db))

    def place(self, job: str, device: str, account: str | None, harness: str = "claude") -> None:
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "INSERT INTO device_slots (job_id, device, harness, os_user, placed_at, account) "
                "VALUES (?, ?, ?, 'claude-bot', 50.0, ?)", (job, device, harness, account))
            connection.commit()
        finally:
            connection.close()


def test_removing_a_paused_account_with_a_running_job_is_refused(tmp_path: Path) -> None:
    box = Box(tmp_path)
    ClaimDb(tmp_path, box).place("j-1", "pc", "spare")
    _remove(box)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "spare holds 1 running job(s)"), entry
    box.unchanged()


LOGIN_SPARE = MACHINES.replace(
    "    harnesses: {claude: claude-bot}\n",
    "    harnesses:\n      claude: {bot: claude-bot, account: spare}\n",
).replace("    signed_in_on: [pc]\n    paused: true\n", "    paused: true\n")


def test_a_null_account_slot_counts_as_the_devices_login_account(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _base(box, LOGIN_SPARE)
    ClaimDb(tmp_path, box).place("j-1", "pc", None)
    _remove(box, expect={**SPARE_SHOWN, "signed_in_on": []})

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "spare holds 1 running job(s)"), entry
    box.unchanged()


def test_jobs_on_other_accounts_never_block_a_remove(tmp_path: Path) -> None:
    box = Box(tmp_path)
    slots = ClaimDb(tmp_path, box)
    slots.place("j-1", "pc", None)
    slots.place("j-2", "box", "work")
    _remove(box)

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert box.machines.read_text() == MACHINES.replace(SPARE_BLOCK, "")


def test_dropping_a_device_whose_pair_holds_a_job_is_refused(tmp_path: Path) -> None:
    box = Box(tmp_path)
    ClaimDb(tmp_path, box).place("j-1", "pc", "spare")
    _devices(box, account="spare", expect=("pc",), value=())

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "spare holds a running job on pc"), entry
    box.unchanged()


def test_dropping_an_idle_device_while_another_pair_runs_applies(tmp_path: Path) -> None:
    box = Box(tmp_path)
    _base(box, MACHINES.replace("signed_in_on: [box]", "signed_in_on: [box, pc]"))
    ClaimDb(tmp_path, box).place("j-1", "box", "work")
    _devices(box, expect=("box", "pc"), value=("box",))

    (entry,) = box.run()

    assert entry.result == "pending", entry
    assert "    signed_in_on: [box]\n" in box.machines.read_text()


def test_a_failed_restart_never_restores_a_file_that_drops_a_running_pair(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path, codes=[0, 1])
    ClaimDb(tmp_path, box).place("j-1", "pc", "work")
    _devices(box)

    (entry,) = box.run()

    assert entry.result == "failed", entry
    assert entry.error == ("restart waiter-claude-bot exited 1; "
                           "not restored: work holds a running job on pc")
    assert "    signed_in_on: [box, pc]\n" in box.machines.read_text()


def test_a_failed_restart_never_restores_away_an_added_account_with_a_job(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path, codes=[0, 1])
    ClaimDb(tmp_path, box).place("j-1", "box", "fresh", harness="codex")
    _add(box)

    (entry,) = box.run()

    assert entry.result == "failed", entry
    assert entry.error == ("restart waiter-claude-bot exited 1; "
                           "not restored: fresh holds 1 running job(s)")
    assert box.machines.read_text() == MACHINES + NEW_BLOCK


@pytest.mark.parametrize("send", [_add, _remove, _devices], ids=["add", "remove", "signed_in_on"])
def test_an_adopted_add_remove_or_signed_in_on_reads_applied(tmp_path: Path, send) -> None:
    box = Box(tmp_path)
    slots = ClaimDb(tmp_path, box)
    send(box)
    (entry,) = box.run()
    assert entry.result == "pending", entry

    assert _audit_result(box, slots.db) == "applied"
