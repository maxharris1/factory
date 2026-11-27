"""Mission Control device settings (#777) end to end: the real snapshot and
health.html, the real dashboard server's POST /api/settings into a spool
directory, and the real `settings_apply.run_pass` reading that spool."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from lib import live_settings, settings_apply
from lib.dashboard import collect_snapshot, write_snapshot_pages
from lib.settings_apply import Applier, LocalSpool, claim_lock, run_pass
from tests.test_dashboard import NOW, make_db
from tests.test_dashboard_http import MAX_TOKEN, Client, fake_github, serve_actions
from tests.test_dashboard_layout import _no_script, _section
from tests.test_dashboard_settings import _card, _facts
from tests.test_settings_apply import NOW as APPLY_NOW
from tests.test_settings_apply import OWNER, Runner

MACHINES = """\
# machines.yml schema v1. Hand-edited; comments stay.
orch_host: box
devices:
  # The orch host.
  - name: box
    local: true
    drain: false  # flip from Mission Control
    slot_max: 6  # memory is tight
    harnesses:
      claude: claude-bot
      codex: codex-bot
  # The PC.
  - name: pc
    tailscale_name: pc.example.ts.net
    enabled: true
    slot_max: 3
    harnesses: {claude: claude-bot}
    workdir_root: /home/{bot}/work
    ssh_alias: pc-{bot}
    factory_root: /srv/factory
"""

RESTART = ("poller", "waiter:claude-bot", "waiter:codex-bot")


class Mission:
    def __init__(self, tmp_path: Path):
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.db = self.state / "orch.db"
        make_db(self.db)
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS device_slots (job_id TEXT PRIMARY KEY, device TEXT)")
            connection.executemany("INSERT INTO device_slots VALUES (?, 'box')", [("j1",), ("j2",), ("j3",)])
        self.machines = tmp_path / "checkout" / "machines.yml"
        self.machines.parent.mkdir()
        self.machines.write_text(MACHINES, encoding="utf-8")
        self.original = self.machines.read_bytes()
        self.site = tmp_path / "site"
        self.spool = tmp_path / "spool"
        self.spool.mkdir()
        self.spool.chmod(0o755)
        self.runner = Runner()
        self.applier = Applier(
            machines=self.machines, state_dir=self.state, deploy=tmp_path / "deploy",
            approver="maxharris1", environ={}, restart=RESTART, pending=(),
            claims=claim_lock(self.db), run=self.runner, clock=lambda: APPLY_NOW,
        )
        self.seen_spool: dict[str, str] = {}

    def publish(self) -> dict:
        snapshot = collect_snapshot(self.db, self.state / "poller.log", now=NOW,
                                    machines_path=self.machines)
        write_snapshot_pages(self.site, snapshot)
        return json.loads((self.site / "status.json").read_text(encoding="utf-8"))

    def page(self) -> str:
        return _section(_no_script((self.site / "health.html").read_text(encoding="utf-8")), "settings")

    def apply(self) -> list:
        self.remember_spool()
        return run_pass(self.applier, LocalSpool(self.spool), OWNER)

    def remember_spool(self) -> None:
        for path in self.spool.iterdir():
            self.seen_spool[path.name] = path.read_text(encoding="utf-8")

    def audit(self) -> list[dict]:
        path = self.state / settings_apply.AUDIT_NAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def poller_adopts(self) -> None:
        with sqlite3.connect(self.db, isolation_level=None) as connection:
            connection.execute("BEGIN IMMEDIATE")
            live_settings.adopted(connection, self.machines)
            connection.execute("COMMIT")

    def backups(self) -> list[Path]:
        return sorted(self.machines.parent.glob("machines.yml.bak-settings-*"))

    def device(self, name: str) -> dict:
        return next(d for d in yaml.safe_load(self.machines.read_text())["devices"] if d["name"] == name)


@pytest.fixture
def mission(tmp_path: Path) -> Mission:
    return Mission(tmp_path)


@pytest.fixture
def actions_site(mission: Mission) -> Path:
    mission.publish()
    return mission.site


@pytest.fixture
def max_on_settings(serve_actions, fake_github, mission: Mission) -> Client:
    client = serve_actions(spool=mission.spool)
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    return client


def change(client: Client, **fields) -> tuple[int, dict]:
    return client.post("/api/settings", {"device": "box", **fields})


def assert_no_secret(mission: Mission, client: Client) -> None:
    session = client.server.actions.session
    secrets = (MAX_TOKEN, session.session_id, session.csrf)
    mission.remember_spool()
    texts = {f"spool/{name}": text for name, text in mission.seen_spool.items()}
    texts["audit"] = (mission.state / settings_apply.AUDIT_NAME).read_text(encoding="utf-8")
    for name in ("status.json", "health.html"):
        texts[name] = (mission.site / name).read_text(encoding="utf-8")
    assert len(texts) > 3
    for name, text in texts.items():
        for secret in secrets:
            assert secret not in text, name


def test_drain_from_the_page_is_applied_audited_and_shown(mission: Mission, max_on_settings: Client) -> None:
    before = mission.publish()
    box_row = next(row for row in before["slots"]["devices"] if row["name"] == "box")
    assert (box_row["drain"], box_row["slot_max"], box_row["enabled"]) == (False, 6, True)
    assert _facts(_card(mission.page(), "box"))["drain"] == "off"

    status, answer = change(max_on_settings, field="drain", expect=False, value=True)
    assert status == 202, answer
    first = answer["id"]
    assert (mission.spool / f"{first}.json").is_file()

    (entry,) = mission.apply()

    assert entry.result == "applied", entry
    assert mission.device("box")["drain"] is True
    assert "drain" not in mission.device("pc")
    after = mission.machines.read_text(encoding="utf-8")
    assert [line for line in after.splitlines() if line not in MACHINES.splitlines()] == [
        "    drain: true  # flip from Mission Control"]
    assert len(after.splitlines()) == len(MACHINES.splitlines())
    (backup,) = mission.backups()
    assert backup.read_bytes() == mission.original
    (line,) = mission.audit()
    assert {k: line[k] for k in ("id", "actor", "device", "field", "previous", "new", "result", "error",
                                 "backup", "restarted")} == {
        "id": first, "actor": "maxharris1", "device": "box", "field": "drain", "previous": False,
        "new": True, "result": "applied", "error": None, "backup": backup.name,
        "restarted": ["poller", "waiter-claude-bot", "waiter-codex-bot"]}
    assert [argv[1:] for argv in mission.runner.argvs] == [
        ["restart"], ["claude-bot", "restart"], ["codex-bot", "restart"]]
    assert [Path(argv[0]).name for argv in mission.runner.argvs] == [
        "poller-service.sh", "waiter-service.sh", "waiter-service.sh"]

    assert line["version"] == 1

    published = mission.publish()
    (result,) = published["settings"]["results"]
    assert {k: result[k] for k in ("id", "actor", "device", "field", "previous", "new", "result")} == {
        "id": first, "actor": "maxharris1", "device": "box", "field": "drain", "previous": False,
        "new": True, "result": "pending"}
    card = _card(mission.page(), "box")
    assert '<span class="chip warn">pending</span>' in card
    assert "waiting for the poller&#x27;s next cycle" in card, "pending until the poller adopts it"

    mission.poller_adopts()
    published = mission.publish()
    (result,) = published["settings"]["results"]
    assert (result["id"], result["result"]) == (first, "applied")
    rows = {row["name"]: row for row in published["slots"]["devices"]}
    assert (rows["box"]["drain"], rows["pc"]["drain"]) == (True, False)
    card = _card(mission.page(), "box")
    assert _facts(card)["drain"] == "draining, 3 running"
    assert '<span class="chip ok">applied</span>' in card
    assert "drain false → true" in card
    assert "applied" not in _card(mission.page(), "pc")

    status, answer = change(max_on_settings, field="slot_max", expect=6, value=4)
    assert status == 202, answer
    assert not (mission.spool / f"{first}.json").exists(), "the applied request is cleared"
    assert (mission.spool / f"{answer['id']}.json").is_file()

    assert_no_secret(mission, max_on_settings)


def test_a_stale_request_after_a_hand_edit_is_refused_and_shown(
    mission: Mission, max_on_settings: Client
) -> None:
    mission.publish()
    status, answer = change(max_on_settings, field="slot_max", expect=6, value=4)
    assert status == 202, answer
    edited = MACHINES.replace("slot_max: 6  # memory", "slot_max: 5  # memory")
    mission.machines.write_text(edited, encoding="utf-8")
    hand_edited = mission.machines.read_bytes()

    (entry,) = mission.apply()

    assert (entry.result, entry.error) == ("refused", "changed"), entry
    assert mission.machines.read_bytes() == hand_edited
    assert mission.backups() == []
    assert mission.runner.calls == []
    (line,) = mission.audit()
    assert {k: line[k] for k in ("id", "actor", "device", "field", "previous", "new", "result", "error")} == {
        "id": answer["id"], "actor": "maxharris1", "device": "box", "field": "slot_max", "previous": 5,
        "new": 4, "result": "refused", "error": "changed"}

    mission.publish()
    card = _card(mission.page(), "box")
    assert '<span class="chip warn">refused</span>' in card
    assert '<span class="st-err">changed</span>' in card
    assert _facts(card)["slot_max"] == "5"
    assert "refused" not in _card(mission.page(), "pc")

    assert_no_secret(mission, max_on_settings)

