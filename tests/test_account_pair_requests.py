from __future__ import annotations

import json
import shlex
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from lib import account_doctor, account_signin, heal, settings_apply
from lib.machines_yml import load_machines_yml
from lib.remote import SshTransport
from lib.settings_apply import Applier, LocalSpool, run_pass
from tests.test_account_doctor import SIGNED_OUT_CLAUDE, World
from tests.test_settings_apply import NOW, OWNER, Runner

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
            claims=settings_apply.claim_lock(world.db), run=self.runner, clock=lambda: NOW,
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


SECURE = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
LOGOUT_ARGV = {"claude": "auth logout", "codex": "logout"}
LOGOUT_FAKE = """\
#!/bin/sh
home=${{{home_variable}:-default}}
if [ "$*" = "{argv}" ]; then
  printf '%s|%s|%s\\n' "$*" "$home" "$(env | cut -d= -f1 | tr '\\n' ' ')" >> {log}
  echo "logout said sk-logout-output for $home"
  echo "logout stderr sk-logout-stderr" >&2
  code=$(cat {code} 2>/dev/null || echo 0)
  [ "$code" = 0 ] && [ ! -e {stick} ] && touch "$home.logged-out"
  exit "$code"
fi
if [ -e "$home.logged-out" ]; then
  {signed_out}
fi
exec {real} "$@"
"""
SIGNED_OUT_REPLY = {"claude": f"echo {shlex.quote(SIGNED_OUT_CLAUDE.decode())}; exit 1",
                    "codex": "echo 'Not logged in' >&2; exit 1"}
HOME_VARIABLE = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}


class Logout:
    def __init__(self, box: Box, harness: str):
        self.log = box.world.tmp / f"{harness}-logout.log"
        self.code = box.world.tmp / f"{harness}-logout.code"
        self.stick = box.world.tmp / f"{harness}-logout.sticks"
        fake = box.world.bin / harness
        real = fake.with_name(f"{harness}-real")
        fake.rename(real)
        fake.write_text(LOGOUT_FAKE.format(
            home_variable=HOME_VARIABLE[harness], argv=LOGOUT_ARGV[harness], log=self.log,
            code=self.code, stick=self.stick, signed_out=SIGNED_OUT_REPLY[harness], real=real,
        ), encoding="utf-8")
        fake.chmod(0o755)
        (box.world.device_home / ".factory-accounts").mkdir(exist_ok=True)
        box.world.profile(f"export LOGNAME=claude-bot USER=claude-bot {SECURE}=/planted/secure\n")

    def calls(self) -> list[tuple[str, str, set[str]]]:
        if not self.log.exists():
            return []
        return [(argv, home, set(names.split()))
                for argv, home, names in (line.split("|") for line in self.log.read_text().splitlines())]


def _signout_line(box: Box) -> dict:
    (line,) = box.audit()
    text = (box.state / settings_apply.AUDIT_NAME).read_text(encoding="utf-8")
    for canary in ("sk-logout", "sk-planted", "/planted/secure"):
        assert canary not in text, f"{canary} reached the audit"
    return line


def _pair_keys(line: dict) -> dict:
    return {k: line[k] for k in ("id", "actor", "target", "account", "device", "field",
                                 "previous", "new", "result", "error")}


def test_signout_runs_the_logout_as_the_bot_under_the_home_with_tokens_unset_then_the_doctor(
    box: Box,
) -> None:
    logout = Logout(box, "claude")
    assert box.world.doctor() == 0
    rid = box.request("signout", account="claude-b")

    (entry,) = box.run()

    home = str(box.world.device_home / ".factory-accounts" / "claude-b")
    ((argv, logged_home, names),) = logout.calls()
    assert (argv, logged_home) == ("auth logout", home)
    assert not names & {*account_doctor.ACCOUNT_TOKEN_VARIABLES, SECURE}, names
    (call,) = [c for c in box.world.ssh.calls() if "orch-accounts-logout" in c["script"]]
    assert (call["alias"], call["user"]) == ("pc-claude-bot", "claude-bot")
    assert call["script"].startswith("exec bash -lc ")
    assert box.world.statuses()[("claude-b", "pc")] == "signed-out", "the doctor ran after"
    assert _pair_keys(_signout_line(box)) == {
        "id": rid, "actor": "maxharris1", "target": "account", "account": "claude-b", "device": "pc",
        "field": "signout", "previous": "ok", "new": "signed-out", "result": "signed-out", "error": None}
    assert entry.result == "signed-out"
    assert box.run() == [] and len(box.audit()) == 1, "an id is signed out once"
    box.untouched()


def test_a_codex_signout_runs_codex_logout_under_its_home(box: Box) -> None:
    box.world.declare("claude-b", "codex-b")
    box.machines.write_text(box.machines.read_text(encoding="utf-8").replace("/srv/factory", str(ROOT)),
                            encoding="utf-8")
    box.original = box.machines.read_bytes()
    logout = Logout(box, "codex")
    box.request("signout", account="codex-b")

    (entry,) = box.run()

    home = str(box.world.device_home / ".factory-accounts" / "codex-b")
    ((argv, logged_home, names),) = logout.calls()
    assert (argv, logged_home) == ("logout", home)
    assert not names & {*account_doctor.ACCOUNT_TOKEN_VARIABLES, SECURE}, names
    (call,) = [c for c in box.world.ssh.calls() if "orch-accounts-logout" in c["script"]]
    assert (call["alias"], call["user"]) == ("pc-codex-bot", "codex-bot")
    line = _signout_line(box)
    assert (line["field"], line["previous"], line["new"], line["result"], line["error"]) == (
        "signout", None, "signed-out", "signed-out", None)
    assert entry.result == "signed-out"
    box.untouched()


def test_signout_is_refused_while_the_pair_holds_a_running_job(box: Box) -> None:
    from contextlib import contextmanager
    from dataclasses import replace

    from lib.device_pool import RunningPair

    logout = Logout(box, "claude")
    assert box.world.doctor() == 0
    before = len(box.world.ssh.calls())

    @contextmanager
    def busy():
        yield settings_apply.Held(running=lambda config: {"pc": 1},
                                  commit_version=lambda machines: None,
                                  pairs=lambda config: [RunningPair("job-1", "claude-b", "pc", NOW)],
                                  hold=lambda account, device, owner, now: True)

    box.applier = replace(box.applier, claims=busy)
    rid = box.request("signout", account="claude-b")

    (entry,) = box.run()

    assert logout.calls() == []
    assert len(box.world.ssh.calls()) == before, "no logout and no doctor"
    assert box.world.statuses()[("claude-b", "pc")] == "ok"
    assert _pair_keys(_signout_line(box)) == {
        "id": rid, "actor": "maxharris1", "target": "account", "account": "claude-b", "device": "pc",
        "field": "signout", "previous": "ok", "new": None, "result": "refused", "error": "running-job"}
    assert entry.result == "refused"
    box.untouched()


def test_a_running_job_on_another_device_or_account_does_not_refuse_a_signout(box: Box) -> None:
    from contextlib import contextmanager
    from dataclasses import replace

    from lib.device_pool import RunningPair

    Logout(box, "claude")

    @contextmanager
    def busy():
        yield settings_apply.Held(running=lambda config: {"pc": 1},
                                  commit_version=lambda machines: None,
                                  pairs=lambda config: [RunningPair("job-1", "claude-c", "pc", NOW),
                                                        RunningPair("job-2", "claude-b", "lap", NOW)],
                                  hold=lambda account, device, owner, now: True)

    box.applier = replace(box.applier, claims=busy)
    box.request("signout", account="claude-b")

    (entry,) = box.run()

    assert entry.result == "signed-out"


def test_no_job_is_placed_on_the_pair_while_it_signs_out(box: Box) -> None:
    from dataclasses import replace

    from lib.device_pool import DevicePool, running_pairs
    from lib.queue import SQLiteJobQueue
    from tests.test_device_pool import enqueue

    logout = Logout(box, "claude")
    assert box.world.doctor() == 0
    config, _ = load_machines_yml(box.machines)
    queue = SQLiteJobQueue(box.world.db, clock=lambda: NOW)
    pool = DevicePool(queue, config)
    placed = []
    transport = box.applier.transport

    class ClaimDuringLogout:
        def exchange(self, argv, **kwargs):
            if "orch-accounts-logout" in argv[-1]:
                for key in ("first", "second", "third"):
                    enqueue(queue, "claude", f"signout-race-{key}")
                    claimed = pool.claim("claude")
                    if claimed is not None:
                        placed.append((claimed[1].account, claimed[1].device))
            return transport.exchange(argv, **kwargs)

    box.applier = replace(box.applier, transport=ClaimDuringLogout())
    box.request("signout", account="claude-b")

    (entry,) = box.run()

    assert len(logout.calls()) == 1 and entry.result == "signed-out"
    assert placed, "the claims during the logout placed work elsewhere"
    assert ("claude-b", "pc") not in placed
    with sqlite3.connect(box.world.db) as connection:
        assert ("claude-b", "pc") not in {(p.account, p.device) for p in running_pairs(connection, config)}


def test_a_failed_signout_gives_the_pair_back_to_placement(box: Box) -> None:
    from dataclasses import replace

    logout = Logout(box, "claude")
    logout.code.write_text("3", encoding="utf-8")
    assert box.world.doctor() == 0
    ticks = iter(range(10))
    box.applier = replace(box.applier, clock=lambda: NOW + next(ticks))
    box.request("signout", account="claude-b")

    (entry,) = box.run()

    assert entry.result == "failed"
    with sqlite3.connect(box.world.db) as connection:
        assert ("claude-b", "pc") not in account_doctor.held(connection)
        assert ("claude-b", "pc") not in account_doctor.signing_out(connection, NOW)


def test_a_signout_on_a_device_with_a_placeholder_home_records_one_failed_line(box: Box) -> None:
    logout = Logout(box, "claude")
    box.machines.write_text(box.machines.read_text(encoding="utf-8").replace(
        str(box.world.device_home), "REPLACE_WITH_BOT_HOME"), encoding="utf-8")
    box.original = box.machines.read_bytes()
    config, _ = load_machines_yml(box.machines)
    assert config is not None and config.device("pc").placeholder
    rid = box.request("signout", account="claude-b")

    (entry,) = box.run()

    assert logout.calls() == []
    line = _signout_line(box)
    assert (line["id"], line["field"], line["result"], line["error"]) == (
        rid, "signout", "failed", account_doctor.PLACEHOLDER_DEVICE)
    assert entry.result == "failed"
    assert box.run() == [] and len(box.audit()) == 1, "the request is not retried"
    box.untouched()


def test_a_logout_that_exits_nonzero_records_failed_and_still_runs_the_doctor(box: Box) -> None:
    logout = Logout(box, "claude")
    logout.code.write_text("3", encoding="utf-8")
    assert box.world.doctor() == 0
    box.request("signout", account="claude-b")

    (entry,) = box.run()

    assert len(logout.calls()) == 1
    line = _signout_line(box)
    assert (line["field"], line["previous"], line["new"], line["result"], line["error"]) == (
        "signout", "ok", "ok", "failed", "logout-exit")
    assert entry.result == "failed"
    box.untouched()


def test_a_logout_that_leaves_the_pair_signed_in_records_failed(box: Box) -> None:
    logout = Logout(box, "claude")
    logout.stick.touch()
    box.request("signout", account="claude-b")

    (entry,) = box.run()

    line = _signout_line(box)
    assert (line["new"], line["result"], line["error"]) == ("ok", "failed", "still-signed-in")
    assert entry.result == "failed"


def test_a_signout_that_cannot_reach_the_device_records_failed_unreachable(box: Box) -> None:
    Logout(box, "claude")
    box.world.ssh.offline("pc-claude-bot").touch()
    box.request("signout", account="claude-b")

    (entry,) = box.run()

    line = _signout_line(box)
    assert (line["new"], line["result"], line["error"]) == ("unreachable", "failed", "unreachable")


def test_the_devices_own_login_account_is_never_signed_out(box: Box) -> None:
    logout = Logout(box, "claude")
    box.request("signout", account="claude-default")

    (entry,) = box.run()

    assert logout.calls() == []
    assert box.world.ssh.calls() == []
    line = _signout_line(box)
    assert (line["field"], line["result"], line["error"]) == ("signout", "refused", "default-account")


@pytest.mark.parametrize("extra, error", [
    ({"actor": "claude-bot"}, "not-approver"),
    ({"account": "nobody"}, "unknown-account"),
    ({"device": "lap"}, "claude-b is not signed in on lap"),
], ids=["bot", "unknown-account", "not-signed-in"])
def test_a_refused_signout_runs_nothing_and_writes_one_line(box: Box, extra: dict, error: str) -> None:
    logout = Logout(box, "claude")
    box.request("signout", **{"account": "claude-b", **extra})

    (entry,) = box.run()

    assert logout.calls() == [] and box.world.ssh.calls() == []
    line = _signout_line(box)
    assert (line["field"], line["result"], line["error"]) == ("signout", "refused", error)


def test_logout_script_unsets_every_token_variable_after_the_profile() -> None:
    from lib.harnesses.gemini import AUTH_OVERRIDES

    tokens = set(account_doctor.ACCOUNT_TOKEN_VARIABLES)
    assert len(tokens) == 17
    assert set(AUTH_OVERRIDES) <= tokens
    for harness, command in (("claude", "claude auth logout"), ("codex", "codex logout")):
        inner = shlex.split(account_signin.logout_script(harness, "/h/acct"))[3]
        unset = next(part for part in inner.split(";") if part.strip().startswith("unset "))
        assert set(unset.split()[1:]) >= tokens | {SECURE}
        assert inner.index("unset ") < inner.index(command)
        assert f"{HOME_VARIABLE[harness]}=/h/acct {command}" in inner


def test_a_doctor_pass_during_the_logout_does_not_give_the_pair_back_to_placement(box: Box) -> None:
    from dataclasses import replace

    from lib.device_pool import DevicePool
    from lib.queue import SQLiteJobQueue
    from tests.test_device_pool import enqueue

    logout = Logout(box, "claude")
    assert box.world.doctor() == 0
    config, _ = load_machines_yml(box.machines)
    queue = SQLiteJobQueue(box.world.db, clock=lambda: NOW + 60)
    pool = DevicePool(queue, config)
    placed = []
    transport = box.applier.transport

    class DoctorThenClaimDuringLogout:
        def exchange(self, argv, **kwargs):
            if "orch-accounts-logout" in argv[-1]:
                with sqlite3.connect(box.world.db, isolation_level=None) as connection:
                    account_doctor.run(connection, config, transport, NOW + 30, None)
                for key in ("first", "second", "third"):
                    enqueue(queue, "claude", f"signout-doctor-race-{key}")
                    claimed = pool.claim("claude")
                    if claimed is not None:
                        placed.append((claimed[1].account, claimed[1].device))
            return transport.exchange(argv, **kwargs)

    box.applier = replace(box.applier, transport=DoctorThenClaimDuringLogout())
    box.request("signout", account="claude-b")

    (entry,) = box.run()

    assert len(logout.calls()) == 1 and entry.result == "signed-out"
    assert placed, "the claims during the logout placed work elsewhere"
    assert ("claude-b", "pc") not in placed, placed


def test_a_signout_lock_table_from_before_owners_still_reserves_by_owner() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("CREATE TABLE signout_locks (account TEXT NOT NULL, device TEXT NOT NULL, "
                       "since REAL NOT NULL, PRIMARY KEY (account, device))")
    connection.execute("INSERT INTO signout_locks VALUES ('claude-b', 'pc', ?)", (NOW,))

    assert not account_doctor.reserve_pair(connection, "claude-b", "pc", "signin-1", NOW + 60)
    assert account_doctor.reserve_pair(connection, "claude-c", "pc", "signin-1", NOW + 60)
    assert account_doctor.signing_out(connection, NOW + 60) == {("claude-b", "pc"), ("claude-c", "pc")}


def test_a_signout_lock_left_by_a_crashed_applier_lapses() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    account_doctor.reserve_pair(connection, "claude-b", "pc", "signout-1", NOW)

    assert ("claude-b", "pc") in account_doctor.signing_out(connection, NOW + 60)
    assert ("claude-b", "pc") not in account_doctor.signing_out(
        connection, NOW + account_doctor.SIGNOUT_LEASE_SECONDS + 1)


def _drops(box: Box) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(box.world.db)) as connection:
        return connection.execute(
            f"SELECT account, device, since, announced FROM {account_doctor.DROPS} ORDER BY account, device").fetchall()


def test_a_settings_sign_out_records_its_drop_as_announced(box: Box) -> None:
    Logout(box, "claude")
    box.applier = replace(box.applier, repo="owner/factory")
    box.request("signout", account="claude-b")

    (entry,) = box.run()

    assert entry.result == "signed-out"
    assert ("claude-b", "pc", NOW, 1) in _drops(box)
    with closing(sqlite3.connect(box.world.db)) as connection:
        held = connection.execute(f"SELECT account, device FROM {account_doctor.HOLDS}").fetchall()
        assert ("claude-b", "pc") in held
        assert connection.execute(
            "SELECT 1 FROM needs_attention WHERE repo = 'owner/factory' AND ticket_id = 'claude-b@pc' "
            "AND reason = ?", (account_doctor.REASON,)).fetchall() == [(1,)]


def test_run_doctor_from_settings_records_an_unannounced_drop(box: Box) -> None:
    box.request("doctor", account="claude-c")

    (entry,) = box.run()

    assert entry.new == "signed-out"
    assert ("claude-c", "pc", NOW, 0) in _drops(box)
