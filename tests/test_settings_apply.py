"""`orch settings apply` (#777): Mission Control device settings, applied on the orch host."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from lib import heal, settings_apply
from lib.settings_apply import Applier, LocalSpool, SshSpool, run_pass
from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
OWNER = pwd.getpwuid(os.getuid()).pw_name
NOW = 1_800_000_000.0
TOKEN = "ghp_FAKEtokenFAKEtokenFAKEtokenFAKE1234"
WEBHOOK = "fake-webhook-secret-value-987"

MACHINES = """\
# machines.yml schema v1. Hand-edited; comments stay.
orch_host: box
devices:
  # The orch host.
  - name: box
    local: true
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

ALL = ("poller", "waiter:claude-bot", "waiter:codex-bot", "waiter:cursor-bot")


class Runner:
    def __init__(self, codes: list[int] | None = None):
        self.codes = list(codes or [])
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv, env, log_path) -> int:
        self.calls.append((list(argv), dict(env)))
        return self.codes.pop(0) if self.codes else 0

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


class Box:
    def __init__(self, tmp_path: Path, *, running: dict[str, int] | None = None,
                 codes: list[int] | None = None, environ: dict[str, str] | None = None):
        self.tmp = tmp_path
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
            approver="maxharris1", environ=environ or {}, restart=ALL[:3],
            pending=("waiter-cursor-bot",), running=lambda config: running or {},
            run=self.runner, clock=lambda: NOW,
        )
        self.count = 0

    def request(self, device="box", field="slot_max", expect=6, value=4,
                actor="maxharris1", **extra) -> str:
        self.count += 1
        rid = f"request-{self.count:04d}-abcdefghij"
        body = {"id": rid, "actor": actor, "requested_at": "2026-11-17T10:00:00Z",
                "device": device, "field": field, "expect": expect, "value": value, **extra}
        path = self.spool / f"{rid}.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        path.chmod(0o644)
        return rid

    def run(self, owner: str = OWNER):
        return run_pass(self.applier, LocalSpool(self.spool), owner)

    def audit(self) -> list[dict]:
        path = self.state / settings_apply.AUDIT_NAME
        return [json.loads(line) for line in path.read_text().splitlines()]

    def backups(self) -> list[Path]:
        return sorted(self.machines.parent.glob("machines.yml.bak-settings-*"))

    def unchanged(self) -> None:
        assert self.machines.read_bytes() == self.original, "machines.yml is byte-identical"
        assert self.backups() == [], "no backup on a refusal"
        assert self.runner.calls == [], "nothing restarted"


def changed_lines(before: str, after: str) -> list[tuple[str | None, str | None]]:
    old, new = before.splitlines(), after.splitlines()
    if len(old) == len(new):
        return [(a, b) for a, b in zip(old, new) if a != b]
    index = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), len(old))
    assert old[index:] == new[index + 1:], "only one line was inserted"
    return [(None, new[index])]


def test_the_approvers_request_is_applied_backed_up_audited_and_restarted(tmp_path: Path) -> None:
    box = Box(tmp_path)
    rid = box.request()

    (entry,) = box.run()

    assert entry.result == "applied", entry
    (line,) = box.audit()
    assert set(line) == {"id", "actor", "requested_at", "applied_at", "device", "field",
                         "previous", "new", "result", "error", "backup", "restarted", "pending"}
    assert {k: line[k] for k in ("id", "actor", "requested_at", "applied_at", "device",
                                 "field", "previous", "new", "result", "error")} == {
        "id": rid, "actor": "maxharris1", "requested_at": "2026-11-17T10:00:00Z",
        "applied_at": heal.stamp(NOW), "device": "box", "field": "slot_max",
        "previous": 6, "new": 4, "result": "applied", "error": None}
    assert line["restarted"] == ["poller", "waiter-claude-bot", "waiter-codex-bot"]
    assert line["pending"] == ["waiter-cursor-bot"]
    (backup,) = box.backups()
    assert backup.name == line["backup"] == "machines.yml.bak-settings-20270115T080000Z"
    assert backup.parent == box.machines.parent
    assert backup.read_bytes() == box.original
    assert changed_lines(MACHINES, box.machines.read_text()) == [
        ("    slot_max: 6  # memory is tight", "    slot_max: 4  # memory is tight")]
    assert box.runner.argvs == [
        [str(box.deploy / "poller-service.sh"), "restart"],
        [str(box.deploy / "waiter-service.sh"), "claude-bot", "restart"],
        [str(box.deploy / "waiter-service.sh"), "codex-bot", "restart"],
    ]
    assert box.runner.calls[0][1]["ORCH_POLL_STOP_WAIT"] == heal.POLLER_STOP_WAIT


def test_a_new_key_is_one_inserted_line_and_every_comment_stays(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request(field="drain", expect=False, value=True)

    (entry,) = box.run()

    assert entry.result == "applied", entry
    after = box.machines.read_text()
    assert changed_lines(MACHINES, after) == [(None, "    drain: true")]
    for line in MACHINES.splitlines():
        if line.lstrip().startswith("#"):
            assert line in after.splitlines()


@pytest.mark.parametrize("actor", ["claude-bot", "grok", "x[bot]", "someone-else", "maxharris1[bot]"])
def test_anyone_but_the_approver_is_refused(tmp_path: Path, actor: str) -> None:
    box = Box(tmp_path)
    box.request(actor=actor)

    (entry,) = box.run()

    assert (entry.result, entry.error, entry.actor) == ("refused", "not-approver", actor)
    box.unchanged()


def test_the_approver_login_matches_case_insensitively(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request(actor="MaxHarris1")
    assert box.run()[0].result == "applied"


@pytest.mark.parametrize("value", [0, -1, 65, True, "6", None, 4.0])
def test_a_bad_slot_max_is_refused_without_a_backup(tmp_path: Path, value: object) -> None:
    box = Box(tmp_path)
    box.request(value=value)

    (entry,) = box.run()

    assert (entry.result, entry.error, entry.previous) == ("refused", "bad-value", 6)
    box.unchanged()


@pytest.mark.parametrize(
    ("request_args", "error"),
    [
        ({"expect": 5}, "changed"),
        ({"device": "mac"}, "unknown-device"),
        ({"field": "workdir_root", "expect": "/a", "value": "/b"}, "bad-field"),
        ({"field": "enabled", "expect": True, "value": "false"}, "bad-value"),
        ({"value": 6}, "no-change"),
    ],
)
def test_a_stale_or_unknown_request_is_refused(tmp_path: Path, request_args: dict, error: str) -> None:
    box = Box(tmp_path)
    box.request(**request_args)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", error)
    box.unchanged()


def test_a_field_machines_local_yml_sets_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY", raising=False)
    box = Box(tmp_path)
    (box.machines.parent / "machines.local.yml").write_text(
        "devices:\n  - name: box\n    slot_max: 5\n", encoding="utf-8")
    box.request(expect=5)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "slot_max is set in machines.local.yml")
    box.unchanged()


def test_a_field_the_overlay_leaves_alone_still_applies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY", raising=False)
    box = Box(tmp_path)
    (box.machines.parent / "machines.local.yml").write_text(
        "devices:\n  - name: box\n    slot_max: 5\n", encoding="utf-8")
    box.request(field="drain", expect=False, value=True)
    assert box.run()[0].result == "applied"


def test_disabling_a_device_that_holds_a_running_job_is_refused(tmp_path: Path) -> None:
    box = Box(tmp_path, running={"box": 3})
    box.request(field="enabled", expect=True, value=False)

    (entry,) = box.run()

    assert entry.result == "refused"
    assert entry.error == "box holds 3 running job(s); drain it first"
    box.unchanged()


def test_disabling_an_idle_device_applies(tmp_path: Path) -> None:
    box = Box(tmp_path, running={"box": 3})
    box.request(device="pc", field="enabled", expect=True, value=False)

    (entry,) = box.run()

    assert entry.result == "applied", entry
    assert changed_lines(MACHINES, box.machines.read_text()) == [
        ("    enabled: true", "    enabled: false")]


@pytest.mark.parametrize(
    ("owner", "file_mode", "dir_mode", "error"),
    [
        ("dash", 0o644, 0o755, "untrusted-owner"),
        (OWNER, 0o664, 0o755, "untrusted-mode"),
        (OWNER, 0o646, 0o755, "untrusted-mode"),
        (OWNER, 0o644, 0o775, "spool-writable-by-others"),
        (OWNER, 0o644, 0o777, "spool-writable-by-others"),
    ],
)
def test_an_untrusted_request_file_is_refused(
    tmp_path: Path, owner: str, file_mode: int, dir_mode: int, error: str
) -> None:
    box = Box(tmp_path)
    rid = box.request()
    (box.spool / f"{rid}.json").chmod(file_mode)
    box.spool.chmod(dir_mode)

    (entry,) = box.run(owner)

    assert (entry.id, entry.result, entry.error) == (rid, "refused", error)
    assert (entry.actor, entry.device, entry.new) == (None, None, None), "nothing echoed"
    box.unchanged()


def test_a_symlinked_request_file_is_refused(tmp_path: Path) -> None:
    box = Box(tmp_path)
    rid = box.request()
    target = tmp_path / "elsewhere.json"
    (box.spool / f"{rid}.json").rename(target)
    (box.spool / f"{rid}.json").symlink_to(target)

    (entry,) = box.run()

    assert (entry.result, entry.error) == ("refused", "not-a-regular-file")
    box.unchanged()


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b"not json", "not-json"),
        (b'{"id": "x"}', "bad-keys"),
    ],
)
def test_a_malformed_request_is_refused_by_its_file_name(tmp_path: Path, body: bytes, error: str) -> None:
    box = Box(tmp_path)
    path = box.spool / "malformed-request-0001.json"
    path.write_bytes(body)
    path.chmod(0o644)

    (entry,) = box.run()

    assert (entry.id, entry.result, entry.error) == ("malformed-request-0001", "refused", error)
    box.unchanged()


def test_a_second_pass_applies_nothing_again(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request()
    box.request(actor="someone-else")
    assert [e.result for e in box.run()] == ["applied", "refused"]
    after = box.machines.read_bytes()
    calls = len(box.runner.calls)

    assert box.run() == []

    assert box.machines.read_bytes() == after
    assert len(box.runner.calls) == calls
    assert len(box.audit()) == 2
    assert len(box.backups()) == 1


def test_a_failed_restart_restores_the_backup_restarts_again_and_records_failed(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path, codes=[0, 1])
    box.request()

    (entry,) = box.run()

    assert entry.result == "failed"
    assert entry.error == (
        "restart waiter-claude-bot exited 1; restored machines.yml.bak-settings-20270115T080000Z")
    assert box.machines.read_bytes() == box.original, "the old file is back"
    (backup,) = box.backups()
    assert backup.read_bytes() == box.original
    poller = [str(box.deploy / "poller-service.sh"), "restart"]
    claude = [str(box.deploy / "waiter-service.sh"), "claude-bot", "restart"]
    codex = [str(box.deploy / "waiter-service.sh"), "codex-bot", "restart"]
    assert box.runner.argvs == [poller, claude, poller, claude, codex]
    assert box.audit()[0]["result"] == "failed"


def test_two_applies_in_one_second_keep_both_backups(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request()
    box.request(field="drain", expect=False, value=True)

    assert [e.result for e in box.run()] == ["applied", "applied"]

    first, second = box.backups()
    assert first.read_bytes() == box.original
    assert "drain" not in second.read_text() and "slot_max: 4" in second.read_text()


def test_the_ssh_spool_lists_and_reads_the_spool_in_one_call(tmp_path: Path) -> None:
    box = Box(tmp_path)
    rid = box.request()
    (box.spool / ".pending.tmp").write_text("{}")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        assert argv[:6] == ["ssh", "-F", "/home/box/.ssh/config", "-o", "BatchMode=yes", "--"]
        assert argv[6] == "pc-wsl-cursor-bot"
        return subprocess.run(["sh", "-c", argv[7]], capture_output=True, text=True,
                              timeout=kwargs["timeout"], check=False)

    spool = SshSpool("pc-wsl-cursor-bot", str(box.spool), ssh_config="/home/box/.ssh/config",
                     runner=runner)
    (entry,) = run_pass(box.applier, spool, OWNER)

    assert len(calls) == 1
    assert (entry.id, entry.result) == (rid, "applied")


def test_the_ssh_spool_refuses_a_group_writable_spool(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.request()
    box.spool.chmod(0o775)

    def runner(argv, **kwargs):
        return subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True, check=False)

    (entry,) = run_pass(box.applier, SshSpool("pc", str(box.spool), runner=runner), OWNER)

    assert entry.error == "spool-writable-by-others"
    box.unchanged()


def test_an_ssh_failure_applies_nothing_and_names_no_secret(tmp_path: Path) -> None:
    box = Box(tmp_path)

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 255, "", f"Permission denied {TOKEN}\n")

    with pytest.raises(settings_apply.SpoolError) as caught:
        run_pass(box.applier, SshSpool("pc", str(box.spool), runner=runner), OWNER)
    assert TOKEN not in str(caught.value)
    assert "exit 255" in str(caught.value)


FAKE_SERVICE = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["SERVICE_LOG"], "a") as out:
    out.write(json.dumps({{"argv": sys.argv, "env": dict(os.environ)}}) + "\\n")
"""


def _orch_yml(tmp_path: Path, spool: Path) -> Path:
    data = yaml.safe_load((ROOT / "orch.yml").read_text(encoding="utf-8"))
    data["dashboard"] = {"settings": {"spool": str(spool), "spool_owner": OWNER,
                                      "approver": "maxharris1"}}
    path = tmp_path / "orch.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_the_cli_applies_through_the_service_scripts_and_leaks_no_secret(
    tmp_path: Path, capsys
) -> None:
    box = Box(tmp_path)
    rid = box.request()
    box.deploy.mkdir()
    for name in ("poller-service.sh", "waiter-service.sh"):
        (box.deploy / name).write_text(FAKE_SERVICE, encoding="utf-8")
        (box.deploy / name).chmod(0o755)
    config = tmp_path / "config"
    config.mkdir()
    for flag in ("poller-autostart", "waiter-autostart-codex-bot"):
        (config / flag).touch()
    service_log = tmp_path / "services.jsonl"
    environ = {
        "HOME": str(tmp_path), "PATH": os.environ["PATH"], "SERVICE_LOG": str(service_log),
        "GH_TOKEN": TOKEN, "FACTORY_WEBHOOK_SECRET": WEBHOOK,
        "FACTORY_WEBHOOK_AUTH": WEBHOOK, "FACTORY_WEBHOOK_URL": "https://example.invalid/x",
    }

    code = settings_apply.main(
        ["apply", "--state", str(box.state), "--orch", str(_orch_yml(tmp_path, box.spool)),
         "--machines", str(box.machines), "--deploy", str(box.deploy), "--config", str(config)],
        environ=environ, clock=lambda: NOW)

    out = capsys.readouterr()
    assert code == 0, out.err
    assert f"settings: {rid} applied box.slot_max 6 -> 4" in out.out
    calls = [json.loads(line) for line in service_log.read_text().splitlines()]
    assert [c["argv"][1:] for c in calls] == [["restart"], ["codex-bot", "restart"]]
    for call in calls:
        assert "FACTORY_WEBHOOK_AUTH" not in call["env"] and "FACTORY_WEBHOOK_URL" not in call["env"]
    (line,) = box.audit()
    assert line["restarted"] == ["poller", "waiter-codex-bot"]
    assert line["pending"] == ["waiter-claude-bot", "waiter-cursor-bot"]
    audit = (box.state / settings_apply.AUDIT_NAME).read_text()
    for text in (audit, out.out, out.err):
        assert TOKEN not in text and WEBHOOK not in text


def test_without_a_settings_block_the_cli_does_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    code = settings_apply.main(["apply", "--state", str(state), "--orch", str(ROOT / "orch.yml")],
                               environ={"HOME": str(tmp_path)})
    assert code == 0
    assert not (state / settings_apply.AUDIT_NAME).exists()


def test_orch_settings_is_an_orch_command(tmp_path: Path) -> None:
    result = subprocess.run([str(ROOT / "bin" / "orch"), "settings", "apply", "--state",
                             str(tmp_path / "state"), "--orch", str(ROOT / "orch.yml")],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("block", "error"),
    [
        ({"spool": "relative/dir", "spool_owner": "dash", "approver": "maxharris1"},
         "dashboard.settings.spool must be an absolute path"),
        ({"spool": "/srv/spool", "spool_owner": "dash", "approver": "claude-bot"},
         "dashboard.settings.approver must be a person, not a bot login"),
        ({"spool": "/srv/spool", "spool_owner": "dash"}, "dashboard.settings.approver is required"),
        ({"spool": "/srv/spool", "spool_owner": "dash", "approver": "m", "extra": 1},
         "unknown field 'dashboard.settings.extra'"),
    ],
)
def test_check_config_validates_the_settings_block(block: dict, error: str) -> None:
    from lib.dashboard_config import parse_dashboard

    findings: list = []
    assert parse_dashboard({"settings": block}, findings) is None
    assert any(f.message.startswith(error) for f in findings), findings


def test_a_real_waiter_restart_leaves_a_running_job_alive(tmp_path: Path) -> None:
    from tests.test_waiter_service import _cleanup, _env, _make_home, _starts, _wait_for

    home = _make_home(tmp_path / "home")
    box = Box(tmp_path)
    applier = Applier(
        machines=box.machines, state_dir=home / ".local/state/factory",
        deploy=ROOT / "deploy", approver="maxharris1", environ=_env(home),
        restart=("waiter:claude-bot",), pending=(), running=lambda config: {},
        run=heal.run_service, clock=lambda: NOW,
    )
    job = None
    try:
        started = subprocess.run(["bash", str(ROOT / "deploy" / "waiter-service.sh"),
                                  "claude-bot", "start"], env=_env(home), text=True,
                                 capture_output=True, timeout=60, check=False)
        assert started.returncode == 0, started.stderr
        first = _wait_for(lambda: _starts(home))[0]
        job = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                               start_new_session=True)
        box.request(field="drain", expect=False, value=True)

        (entry,) = run_pass(applier, LocalSpool(box.spool), OWNER)

        assert entry.result == "applied", entry
        assert entry.restarted == ("waiter-claude-bot",)
        second = _wait_for(lambda: len(_starts(home)) >= 2 and _starts(home)[1])
        assert second != first and not alive(first), "the waiter was restarted"
        time.sleep(0.3)
        assert job.poll() is None and alive(job.pid), "the running job survived the restart"
    finally:
        if job is not None:
            job.kill()
            job.wait()
        _cleanup(home)
