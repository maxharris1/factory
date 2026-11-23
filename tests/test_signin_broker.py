from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from lib import account_doctor, cli, settings_apply, signin_broker, signin_relay
from lib.device_pool import RunningPair
from lib.remote import SshTransport
from tests.test_account_doctor import World
from tests.test_account_pair_requests import Box
from tests.test_settings_apply import NOW, OWNER


class Intake:
    def __init__(self, box: Box, tmp_path: Path):
        self.box = box
        self.relay = tmp_path / "relay"
        self.relay.mkdir()
        self.relay.chmod(0o770)
        self.launched: list[signin_broker.Attempt] = []
        self.homes_at_launch: list[bool] = []
        self.workers: list[threading.Thread] = []
        self.broker = self.make()

    def make(self, **overrides: object) -> signin_broker.Broker:
        settings = dict(
            relay=signin_relay.LocalRelay(self.box.spool, self.relay), machines=self.box.machines,
            db=self.box.world.db, state_dir=self.box.state, approver="maxharris1", owner=OWNER,
            launch=self.launch, claims=settings_apply.claim_lock(self.box.world.db),
            transport=SshTransport(), clock=lambda: NOW, spawn=self.spawn,
        )
        settings.update(overrides)
        return signin_broker.Broker(**settings)

    def spawn(self, work) -> None:
        worker = threading.Thread(target=work, daemon=True)
        self.workers.append(worker)
        worker.start()

    def tick(self) -> list[settings_apply.AuditEntry]:
        entries = self.broker.tick()
        for worker in self.workers:
            worker.join(30)
        return entries

    def launch(self, attempt: signin_broker.Attempt) -> None:
        self.homes_at_launch.append(Path(attempt.home).is_dir())
        self.launched.append(attempt)

    def home(self, account: str) -> Path:
        return self.box.world.device_home / ".factory-accounts" / account

    def made(self, *accounts: str) -> None:
        for account in accounts:
            self.home(account).mkdir(parents=True, exist_ok=True)

    def lines(self) -> list[dict]:
        path = self.box.state / settings_apply.AUDIT_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def view(self, rid: str) -> dict:
        path = self.relay / f"{rid}.relay"
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def intake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Intake:
    return Intake(Box(World(tmp_path, monkeypatch), tmp_path), tmp_path)


def _refusal(line: dict) -> dict:
    return {k: line[k] for k in ("id", "actor", "target", "account", "device", "field", "new", "result",
                                 "error")}


def test_a_signin_for_a_declared_pair_is_launched_as_the_harness_with_its_home(intake: Intake) -> None:
    intake.made("claude-c")
    rid = intake.box.request("signin")

    assert intake.tick() == []
    intake.tick()

    (attempt,) = intake.launched
    assert (attempt.request.id, attempt.request.account, attempt.request.device) == (rid, "claude-c", "pc")
    assert (attempt.harness, attempt.home) == ("claude", str(intake.home("claude-c")))
    assert attempt.device.login_for("claude") == "claude-bot"
    assert intake.lines() == []
    assert intake.box.prepare_calls() == []
    assert intake.view(rid) == {"state": "launching"}


def test_the_broker_picks_up_a_signin_within_five_seconds(intake: Intake) -> None:
    intake.made("claude-c")
    stop = threading.Event()
    serving = threading.Thread(target=intake.broker.serve, args=(stop,), daemon=True)
    serving.start()
    try:
        time.sleep(0.2)
        rid = intake.box.request("signin")
        deadline = time.monotonic() + 5
        while not intake.launched and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        serving.join(10)
    assert [attempt.request.id for attempt in intake.launched] == [rid]
    assert not serving.is_alive()
    assert 0 < signin_broker.POLL_SECONDS <= 5


@pytest.mark.parametrize(("change", "error"), [
    ({"actor": "someone-else"}, "not-approver"),
    ({"actor": "claude-bot[bot]"}, "not-approver"),
    ({"account": "nobody"}, "unknown-account"),
    ({"device": "mars"}, "unknown-device"),
    ({"account": "claude-b", "device": "lap"}, "claude-b is not signed in on lap"),
    ({"account": "claude-default"}, "default-account"),
])
def test_a_request_machines_yml_does_not_allow_is_refused_with_one_line(
    intake: Intake, change: dict, error: str,
) -> None:
    intake.made("claude-b", "claude-c")
    rid = intake.box.request("signin", **change)

    (entry,) = intake.tick()
    intake.tick()

    assert intake.launched == []
    assert intake.box.prepare_calls() == []
    (line,) = intake.lines()
    assert _refusal(line) == {
        "id": rid, "actor": change.get("actor", "maxharris1"), "target": "account",
        "account": change.get("account", "claude-c"), "device": change.get("device", "pc"),
        "field": "signin", "new": None, "result": "refused", "error": error}
    assert entry.result == "refused"
    assert intake.view(rid) == {"result": "refused", "error": error}


@pytest.mark.parametrize(("extra", "error"), [
    ({"value": 1}, "bad-keys"),
    ({"requested_at": "yesterday"}, "bad-requested_at"),
])
def test_a_malformed_signin_is_refused_by_the_broker_with_one_line(
    intake: Intake, extra: dict, error: str,
) -> None:
    rid = intake.box.request("signin", **extra)

    intake.tick()
    intake.tick()

    (line,) = intake.lines()
    assert (line["id"], line["result"], line["error"]) == (rid, "refused", error)
    assert intake.launched == []


def test_a_signin_file_others_can_write_is_refused_by_the_broker(intake: Intake) -> None:
    intake.made("claude-c")
    rid = intake.box.request("signin")
    (intake.box.spool / f"{rid}.json").chmod(0o666)

    intake.tick()

    (line,) = intake.lines()
    assert (line["id"], line["result"], line["error"]) == (rid, "refused", "untrusted-mode")
    assert intake.launched == []


def test_a_pair_holding_a_running_job_is_refused(intake: Intake) -> None:
    intake.made("claude-c")

    @contextmanager
    def busy():
        yield settings_apply.Held(running=lambda config: {"pc": 1}, commit_version=lambda machines: None,
                                  pairs=lambda config: [RunningPair("job-1", "claude-c", "pc", NOW)],
                                  hold=lambda account, device, owner, now: True)

    intake.broker = intake.make(claims=busy)
    rid = intake.box.request("signin")

    intake.tick()

    (line,) = intake.lines()
    assert (line["id"], line["result"], line["error"]) == (rid, "refused", "running-job")
    assert intake.launched == []


def test_a_running_job_on_another_pair_does_not_refuse(intake: Intake) -> None:
    intake.made("claude-c")

    @contextmanager
    def busy():
        yield settings_apply.Held(running=lambda config: {"pc": 1}, commit_version=lambda machines: None,
                                  pairs=lambda config: [RunningPair("job-1", "claude-b", "pc", NOW)],
                                  hold=lambda account, device, owner, now: True)

    intake.broker = intake.make(claims=busy)
    intake.box.request("signin")

    intake.tick()

    assert len(intake.launched) == 1
    assert intake.lines() == []


def test_a_pair_signing_out_is_refused(intake: Intake) -> None:
    intake.made("claude-c")
    with sqlite3.connect(intake.box.world.db) as connection:
        account_doctor.reserve_pair(connection, "claude-c", "pc", "signout-1", NOW)
    rid = intake.box.request("signin")

    intake.tick()

    (line,) = intake.lines()
    assert (line["id"], line["result"], line["error"]) == (rid, "refused", "signing-out")


def test_the_refusal_records_the_pairs_doctor_status_before(intake: Intake) -> None:
    assert intake.box.world.doctor() == 0
    rid = intake.box.request("signin", actor="someone-else")

    intake.tick()

    (line,) = intake.lines()
    assert (line["id"], line["previous"], line["error"]) == (rid, "signed-out", "not-approver")


def test_one_signin_per_pair(intake: Intake) -> None:
    intake.made("claude-c")
    first = intake.box.request("signin")
    second = intake.box.request("signin")

    intake.tick()
    intake.tick()

    assert [attempt.request.id for attempt in intake.launched] == [first]
    (line,) = intake.lines()
    assert (line["id"], line["result"], line["error"]) == (second, "refused", "attempt-live")


def test_at_most_two_signins_at_once(intake: Intake) -> None:
    intake.box.world.declare("claude-b", "claude-c", "codex-b")
    intake.made("claude-b", "claude-c", "codex-b")
    first = intake.box.request("signin", account="claude-b")
    second = intake.box.request("signin", account="claude-c")
    third = intake.box.request("signin", account="codex-b")

    intake.tick()

    assert [attempt.request.id for attempt in intake.launched] == [first, second]
    assert signin_broker.MAX_CONCURRENT == 2
    (line,) = intake.lines()
    assert (line["id"], line["result"], line["error"]) == (third, "refused", "too-many")


def test_a_finished_attempt_frees_its_pair_and_writes_one_line(intake: Intake) -> None:
    intake.made("claude-c")
    first = intake.box.request("signin")
    intake.tick()
    (attempt,) = intake.launched

    entry = intake.broker.finish(attempt, "failed", "cli-exit", None)
    second = intake.box.request("signin")
    intake.tick()

    assert [a.request.id for a in intake.launched] == [first, second]
    (line,) = intake.lines()
    assert (line["id"], line["field"], line["result"], line["error"]) == (first, "signin", "failed", "cli-exit")
    assert entry.id == first
    assert intake.view(first) == {"result": "failed", "error": "cli-exit"}
    with pytest.raises(ValueError):
        intake.broker.finish(attempt, "failed", "cli-exit", None)
    assert len(intake.lines()) == 1


def test_prepare_runs_first_when_the_home_is_missing(intake: Intake) -> None:
    assert not intake.home("claude-c").exists()
    intake.box.request("signin")

    intake.tick()

    (call,) = intake.box.prepare_calls()
    assert call["user"] == "claude-bot"
    assert intake.homes_at_launch == [True]
    assert intake.lines() == []


def test_a_prepare_that_fails_ends_the_attempt_with_one_failed_line(intake: Intake) -> None:
    intake.box.world.profile("export LOGNAME=someone-else USER=someone-else\n")
    rid = intake.box.request("signin")

    intake.tick()
    intake.tick()

    assert intake.launched == []
    (line,) = intake.lines()
    assert (line["id"], line["result"]) == (rid, "failed")
    assert line["error"].startswith("prepare exited 2"), line["error"]
    assert intake.view(rid)["result"] == "failed"


def test_an_unreachable_device_is_refused_before_any_prepare(intake: Intake) -> None:
    intake.box.world.ssh.offline("pc-claude-bot").touch()
    rid = intake.box.request("signin")

    intake.tick()

    (line,) = intake.lines()
    assert (line["id"], line["result"], line["error"]) == (rid, "refused", "unreachable")
    assert intake.box.prepare_calls() == []


class _SlowFirstExchange:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.real = SshTransport()

    def exchange(self, argv, **kwargs):
        if not self.entered.is_set():
            self.entered.set()
            self.release.wait(15)
        return self.real.exchange(argv, **kwargs)


def test_a_slow_home_probe_does_not_hold_up_the_next_signin(intake: Intake) -> None:
    intake.box.world.declare("claude-b", "claude-c", "codex-b")
    intake.made("claude-b", "claude-c")
    slow = _SlowFirstExchange()
    intake.broker = intake.make(transport=slow)
    first = intake.box.request("signin", account="claude-b")
    stop = threading.Event()
    serving = threading.Thread(target=intake.broker.serve, args=(stop,), daemon=True)
    serving.start()
    try:
        assert slow.entered.wait(5)
        second = intake.box.request("signin", account="claude-c")
        deadline = time.monotonic() + 5
        while not intake.launched and time.monotonic() < deadline:
            time.sleep(0.05)
        launched_while_blocked = [attempt.request.id for attempt in intake.launched]
    finally:
        slow.release.set()
        stop.set()
        serving.join(10)
        for worker in intake.workers:
            worker.join(30)
    assert launched_while_blocked == [second]
    assert [attempt.request.id for attempt in intake.launched] == [second, first]


def _job_queue(intake: Intake):
    from lib import device_pool
    from lib.machines_yml import load_machines_yml
    from lib.queue import SQLiteJobQueue

    intake.box.world.db.unlink()
    queue = SQLiteJobQueue(intake.box.world.db, clock=lambda: NOW)
    with queue.connect() as connection:
        account_doctor.ensure_table(connection)
        connection.execute("INSERT INTO account_signins (account, device, status, checked_at) VALUES (?, ?, ?, ?)",
                           ("claude-c", "pc", "ok", NOW))
    config, _ = load_machines_yml(intake.box.machines)
    pool = device_pool.DevicePool(queue, config, pins=lambda: {"owner/repo": frozenset({"pc"})})
    return queue, pool


def _claimed_pair(pool) -> tuple[str, str] | None:
    claim = pool.claim("claude", token_hash="synthetic-hash")
    return None if claim is None else (claim[1].account, claim[1].device)


def test_a_job_cannot_claim_the_pair_while_its_signin_runs(intake: Intake) -> None:
    from lib.remote import Exchanged
    from tests.test_pair_placement import enqueue

    queue, pool = _job_queue(intake)
    enqueue(queue, "job-during-probe")
    claimed: list[tuple[str, str] | None] = []

    class ClaimDuringProbe:
        def exchange(self, argv, **kwargs):
            claimed.append(_claimed_pair(pool))
            return Exchanged(0, b"", "")

    intake.broker = intake.make(transport=ClaimDuringProbe())
    intake.box.request("signin")
    intake.tick()
    (attempt,) = intake.launched
    assert claimed and ("claude-c", "pc") not in claimed
    assert _claimed_pair(pool) != ("claude-c", "pc")

    intake.broker.finish(attempt, "signed-in", None, "ok")
    enqueue(queue, "job-after-signin")

    assert _claimed_pair(pool) == ("claude-c", "pc")


def test_the_pair_stays_held_for_a_signin_longer_than_the_signout_lease(intake: Intake) -> None:
    intake.made("claude-c")
    now = [NOW]
    intake.broker = intake.make(clock=lambda: now[0])
    intake.box.request("signin")
    intake.tick()
    (attempt,) = intake.launched

    for _ in range(3):
        now[0] += account_doctor.SIGNOUT_LEASE_SECONDS / 2
        intake.tick()
        with sqlite3.connect(intake.box.world.db) as connection:
            assert ("claude-c", "pc") in account_doctor.signing_out(connection, now[0])

    intake.broker.finish(attempt, "signed-in", None, "ok")
    with sqlite3.connect(intake.box.world.db) as connection:
        assert account_doctor.signing_out(connection, now[0]) == frozenset()


@pytest.mark.parametrize("state", ["preparing", "launching"])
def test_a_relay_error_before_launch_frees_the_pair_and_the_request_is_retried(
    intake: Intake, state: str,
) -> None:
    if state == "launching":
        intake.made("claude-c")
    rid = intake.box.request("signin")
    publish = intake.broker.relay.publish

    def fail_once(request_id: str, view: dict) -> None:
        if view.get("state") == state:
            intake.broker.relay.publish = publish
            raise settings_apply.SpoolError("temporary SSH publish failure")
        publish(request_id, view)

    intake.broker.relay.publish = fail_once
    intake.tick()
    assert intake.launched == []
    with sqlite3.connect(intake.box.world.db) as connection:
        assert account_doctor.signing_out(connection, NOW) == frozenset()

    intake.tick()

    assert [attempt.request.id for attempt in intake.launched] == [rid]
    assert intake.lines() == []
    assert intake.view(rid) == {"state": "launching"}


def test_a_launch_that_raises_ends_the_attempt_with_one_failed_line(intake: Intake) -> None:
    intake.made("claude-c")

    def broken(attempt: signin_broker.Attempt) -> None:
        raise RuntimeError("no terminal")

    intake.broker = intake.make(launch=broken)
    first = intake.box.request("signin")
    intake.tick()
    intake.box.request("signin")
    intake.broker.launch = intake.launch
    intake.tick()

    (line,) = intake.lines()
    assert (line["id"], line["result"], line["error"]) == (first, "failed", "launch: RuntimeError")
    assert len(intake.launched) == 1


def test_without_a_relay_directory_the_broker_has_nothing_to_serve(tmp_path: Path) -> None:
    from lib.dashboard_config import SettingsConfig

    settings = SettingsConfig(spool=str(tmp_path / "spool"), spool_owner=OWNER, approver="maxharris1")
    assert signin_relay.relay_for(settings, None) is None


def test_the_applier_leaves_signin_requests_to_the_broker(intake: Intake) -> None:
    box = intake.box
    box.request("signin")
    box.request("signin", value=1)
    untrusted = box.request("signin")
    (box.spool / f"{untrusted}.json").chmod(0o666)
    prepare = box.request("prepare")

    entries = box.run()

    assert [entry.id for entry in entries] == [prepare]
    assert [line["id"] for line in intake.lines()] == [prepare]
    assert len(list(box.spool.glob("*.json"))) == 4


def test_append_audit_waits_for_the_other_writer(tmp_path: Path) -> None:
    path = tmp_path / settings_apply.AUDIT_NAME
    path.touch()
    entry = settings_apply.AuditEntry("x" * 16, "maxharris1", None, "now", "pc", "signin", None, None,
                                      "refused", "too-many")
    with open(path, "a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        writer = threading.Thread(target=settings_apply.append_audit, args=(path, entry))
        writer.start()
        writer.join(0.3)
        assert writer.is_alive(), "append_audit waits while the other writer holds the lock"
        assert path.read_text(encoding="utf-8") == ""
    writer.join(5)
    assert path.read_text(encoding="utf-8") == entry.line() + "\n"


def test_two_processes_appending_at_once_leave_only_whole_lines(tmp_path: Path) -> None:
    import multiprocessing

    path = tmp_path / settings_apply.AUDIT_NAME
    context = multiprocessing.get_context("fork")
    writers = [context.Process(target=_append_many, args=(path, name)) for name in "ab"]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(60)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 400
    assert sorted({json.loads(line)["id"][:1] for line in lines}) == ["a", "b"]


def _append_many(path: Path, name: str) -> None:
    for index in range(200):
        settings_apply.append_audit(path, settings_apply.AuditEntry(
            f"{name}{index:015d}", "maxharris1", None, "now", "pc", "signin", "x" * 3000, None, "refused"))


def _local_relay(tmp_path: Path) -> tuple[Path, Path]:
    spool = tmp_path / "spool"
    spool.mkdir()
    spool.chmod(0o755)
    relay = tmp_path / "relay"
    relay.mkdir()
    relay.chmod(0o770)
    return spool, relay


def _spool_request(spool: Path, rid: str) -> None:
    path = spool / f"{rid}.json"
    path.write_text(json.dumps({"id": rid}), encoding="utf-8")
    path.chmod(0o644)


RID = "abcdefghijklmnop1234"


def _exercise(relay: signin_relay.Relay, spool: Path, directory: Path) -> None:
    _spool_request(spool, RID)
    listing = relay.poll()
    assert listing.directory and [item.name for item in listing.files] == [f"{RID}.json"]
    assert listing.files[0].owner == OWNER

    relay.publish(RID, {"state": "launching"})
    relay.publish(RID, {"result": "refused", "error": "too-many"})
    view = directory / f"{RID}.relay"
    assert json.loads(view.read_text(encoding="utf-8")) == {"result": "refused", "error": "too-many"}
    assert stat.S_IMODE(view.stat().st_mode) == 0o640

    (directory / f"{RID}.code").write_text("CODE-1234", encoding="utf-8")
    assert relay.take(RID, "code") == b"CODE-1234"
    assert relay.take(RID, "code") is None
    assert relay.take(RID, "cancel") is None
    assert sorted(path.name for path in directory.iterdir()) == [f"{RID}.relay"]

    with pytest.raises(ValueError):
        relay.publish("../escape-attempt-x", {})
    with pytest.raises(ValueError):
        relay.take(RID, "json")


def test_the_local_relay_reads_and_writes_the_relay_directory(tmp_path: Path) -> None:
    spool, directory = _local_relay(tmp_path)
    _exercise(signin_relay.LocalRelay(spool, directory), spool, directory)


def test_the_ssh_relay_goes_over_one_persistent_push_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_remote_start import Ssh

    ssh = Ssh(tmp_path)
    (ssh.hosts / "dash-push").write_text(OWNER, encoding="utf-8")
    for name, value in ssh.env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ORCH_SSH_MUX", raising=False)
    spool, directory = _local_relay(tmp_path)

    _exercise(signin_relay.SshRelay("dash-push", OWNER, str(spool), str(directory)), spool, directory)

    data = ssh.calls()
    assert len(data) >= 5
    assert {(call["alias"], call["user"], call["via"]) for call in data} == {("dash-push", OWNER, "master")}
    for call in data:
        assert not {"-i", "-F"} & set(call["options"]), call["options"]
    controls = [json.loads(line) for line in Path(f"{ssh.log}.control").read_text().splitlines()]
    assert [entry["op"] for entry in controls].count("master") == 1


def test_relay_for_reads_the_push_login_from_ssh_config_without_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lib.dashboard_config import PushConfig, SettingsConfig

    calls: list[list[str]] = []

    def resolve(argv, **kwargs):
        import subprocess

        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "user dash\nhostname pc\nport 22\n", "")

    settings = SettingsConfig(spool="/srv/spool", spool_owner="dash", approver="maxharris1",
                              relay="/srv/relay")
    push = PushConfig(transport="ssh", host="pc-dash", remote_dir="factory-dashboard",
                      ssh_config="/home/box/.ssh/config")

    relay = signin_relay.relay_for(settings, push, runner=resolve)

    assert isinstance(relay, signin_relay.SshRelay)
    assert (relay.host, relay.user, relay.spool, relay.relay) == ("pc-dash", "dash", "/srv/spool", "/srv/relay")
    assert calls == [["ssh", "-F", "/home/box/.ssh/config", "-o", "BatchMode=yes", "-G", "--", "pc-dash"]]
    local = signin_relay.relay_for(settings, PushConfig())
    assert isinstance(local, signin_relay.LocalRelay)


TRUSTED = """\
poll_seconds: 30
projects:
  - repo: owner/repo
trusted_logins:
  maxharris1: [grok-bot]
dashboard:
  settings:
    spool: /home/dash/settings-spool
    spool_owner: dash
    approver: maxharris1
"""


def _check(tmp_path: Path, text: str) -> int:
    orch = tmp_path / "orch.yml"
    orch.write_text(text, encoding="utf-8")
    return cli._check_config(["--orch", str(orch)])


def test_check_config_takes_an_optional_relay_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from lib.orch_yml import load_orch_yml

    assert _check(tmp_path, TRUSTED) == 0, capsys.readouterr().err
    assert _check(tmp_path, TRUSTED + "    relay: /home/dash/signin-relay\n") == 0, capsys.readouterr().err
    config, _ = load_orch_yml(tmp_path / "orch.yml", allow_placeholders=True)
    assert config.dashboard.settings.relay == "/home/dash/signin-relay"


@pytest.mark.parametrize("relay", ["signin-relay", "/home/dash/../etc", "/home/dash/relay;rm", "''", "7"])
def test_check_config_refuses_a_relay_that_is_not_a_plain_absolute_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], relay: str,
) -> None:
    assert _check(tmp_path, TRUSTED + f"    relay: {relay}\n") != 0
    assert "dashboard.settings.relay must be an absolute path" in capsys.readouterr().err


def test_a_signout_cannot_take_the_pair_from_a_live_signin(intake: Intake, monkeypatch: pytest.MonkeyPatch) -> None:
    from lib.device_pool import DevicePool
    from lib.machines_yml import load_machines_yml
    from lib.queue import SQLiteJobQueue
    from tests.test_pair_placement import enqueue, limit

    box = intake.box
    box.world.db.unlink()
    queue = SQLiteJobQueue(box.world.db, clock=lambda: NOW)
    config, _ = load_machines_yml(box.machines)
    pool = DevicePool(queue, config, pins=lambda: {"owner/repo": frozenset({"pc"})})
    limit(queue, "claude-default")
    limit(queue, "claude-c")
    assert box.world.doctor() == 0
    entered, release = threading.Event(), threading.Event()

    def probe(self, *args):
        entered.set()
        assert release.wait(15)
        return False

    monkeypatch.setattr(signin_broker.Broker, "_home_missing", probe)
    signin = box.request("signin", account="claude-b")
    intake.broker.tick()
    try:
        assert entered.wait(2)
        signout = box.request("signout", account="claude-b")
        (entry,) = box.run()
        intake.broker.tick()
        with sqlite3.connect(box.world.db) as connection:
            holds = account_doctor.signing_out(connection, NOW)
        enqueue(queue, "after-signout-during-signin")
        claimed = pool.claim("claude")
    finally:
        release.set()
        for worker in intake.workers:
            worker.join(5)

    assert (entry.id, entry.result, entry.error) == (signout, "refused", "signing-in")
    assert ("claude-b", "pc") in holds
    assert claimed is None or (claimed[1].account, claimed[1].device) != ("claude-b", "pc")
    assert [attempt.request.id for attempt in intake.launched] == [signin]


def test_a_signin_cannot_take_the_pair_from_a_live_signout(intake: Intake) -> None:
    intake.made("claude-c")
    with sqlite3.connect(intake.box.world.db, isolation_level=None) as connection:
        assert account_doctor.reserve_pair(connection, "claude-c", "pc", "signout-1", NOW)
    rid = intake.box.request("signin")

    intake.tick()

    (line,) = intake.lines()
    assert (line["id"], line["result"], line["error"]) == (rid, "refused", "signing-out")
    with sqlite3.connect(intake.box.world.db, isolation_level=None) as connection:
        assert ("claude-c", "pc") in account_doctor.signing_out(connection, NOW)
        assert not account_doctor.reserve_pair(connection, "claude-c", "pc", "signin-2", NOW)
        account_doctor.release_pair(connection, "claude-c", "pc", "signin-2")
        assert ("claude-c", "pc") in account_doctor.signing_out(connection, NOW)
        account_doctor.release_pair(connection, "claude-c", "pc", "signout-1")
        assert account_doctor.signing_out(connection, NOW) == frozenset()
