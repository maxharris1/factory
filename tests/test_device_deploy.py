"""`orch heal devices` (issue #297): the real deploy script against real git repos.

The transport runs each ssh script locally under `sh -c`, as sshd would on
the device, so the clone, fetch, refusal and checkout paths all run for real.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from lib import device_deploy, device_sync
from lib.device_pool import DevicePool
from lib.heal import Webhook
from lib.machines_yml import Device, MachinesConfig
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import Called
from lib.skill_preflight import Pinned

ORCH_SCRIPT = '#!/bin/sh\necho "$PWD" >> "$HOME/install.log"\n'


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


class Origin:
    def __init__(self, tmp_path: Path):
        self.bare = tmp_path / "origin.git"
        self.work = tmp_path / "origin-work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        git(self.work, "remote", "add", "origin", str(self.bare))
        (self.work / "bin").mkdir()
        (self.work / "bin" / "orch").write_text(ORCH_SCRIPT, encoding="utf-8")
        (self.work / "bin" / "orch").chmod(0o755)
        self.old = self.commit("README", "one\n")
        self.new = self.commit("README", "two\n")

    def commit(self, rel: str, text: str) -> str:
        (self.work / rel).write_text(text, encoding="utf-8")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", rel)
        git(self.work, "push", "-q", "origin", "main")
        return git(self.work, "rev-parse", "HEAD")

    @property
    def url(self) -> str:
        return str(self.bare)


class LocalTransport:
    """Runs the ssh argv's script on this machine; aliases in `offline` exit 255."""

    def __init__(self, home: Path):
        self.home = home
        self.offline: set[str] = set()
        self.scripts: list[tuple[str, str]] = []

    def call(self, argv, *, timeout: float, input: bytes | None = None) -> Called:
        alias, script = argv[-2], argv[-1]
        self.scripts.append((alias, script))
        if alias in self.offline:
            return Called(255, "", "")
        done = subprocess.run(
            ["sh", "-c", script], input=input or b"", capture_output=True, timeout=timeout,
            env={"HOME": str(self.home), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
        return Called(done.returncode, done.stdout.decode(), done.stderr.decode())

    def deploys(self) -> list[str]:
        return [alias for alias, script in self.scripts if "orch-sync" in script]


def device(tmp_path: Path, name: str, *harnesses: str, local: bool = False,
           enabled: bool = True) -> Device:
    return Device(
        name=name,
        slot_max=2,
        harnesses=MappingProxyType({h: f"{h}-bot" for h in harnesses}),
        tailscale_name=None if local else f"{name}.example.ts.net",
        local=local,
        enabled=enabled,
        workdir_root=None if local else f"{tmp_path}/{name}/{{bot}}/work",
        ssh_alias=None if local else f"{name}-{{bot}}",
        factory_root=f"{tmp_path}/{name}/{{bot}}/factory",
    )


def checkout(tmp_path: Path, name: str, bot: str = "claude-bot") -> Path:
    return tmp_path / name / bot / "factory"


@pytest.fixture
def fleet(tmp_path: Path):
    origin = Origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    config = MachinesConfig(devices=(
        device(tmp_path, "box", "claude", "codex", local=True),
        device(tmp_path, "pc", "claude", "codex"),
        device(tmp_path, "mac", "claude"),
        device(tmp_path, "lab", "claude"),
        device(tmp_path, "off", "claude", enabled=False),
    ))
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 100.0)
    DevicePool(queue, config)
    return origin, LocalTransport(home), config, queue, home


def run(origin: Origin, transport: LocalTransport, config: MachinesConfig, queue: SQLiteJobQueue,
        *, pinned: Pinned | None = None, dry_run: bool = False):
    return device_deploy.run_pass(
        queue, config, sha=origin.new, url=origin.url,
        pinned=pinned or Pinned("x", {}, {}), transport=transport, dry_run=dry_run,
    )


def rows(queue: SQLiteJobQueue) -> dict[tuple[str, str], device_sync.Row]:
    with queue.connect() as connection:
        return {(r.device, r.bot): r for r in device_sync.read(connection)}


def states(queue: SQLiteJobQueue) -> dict[tuple[str, str], str]:
    return {key: row.state for key, row in rows(queue).items()}


def on_main_at(path: Path, sha: str) -> bool:
    return (
        git(path, "rev-parse", "HEAD") == sha
        and git(path, "symbolic-ref", "--short", "HEAD") == "main"
        and git(path, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    )


def clone_at(origin: Origin, path: Path, sha: str, *, detach: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", origin.url, str(path)], check=True)
    if detach:
        git(path, "checkout", "-q", "--detach", sha)
    else:
        git(path, "reset", "-q", "--hard", sha)


def test_targets_are_every_enabled_remote_bot_checkout_never_the_box(fleet) -> None:
    _origin, _transport, config, _queue, _home = fleet
    assert [(t.device, t.bot) for t in device_deploy.targets(config)] == [
        ("pc", "claude-bot"), ("pc", "codex-bot"), ("mac", "claude-bot"), ("lab", "claude-bot"),
    ]


def test_a_box_only_fleet_makes_no_ssh_call_and_writes_no_row(tmp_path: Path, fleet) -> None:
    origin, transport, _config, queue, _home = fleet
    box_only = MachinesConfig(devices=(device(tmp_path, "box", "claude", local=True),))

    assert run(origin, transport, box_only, queue) == []
    assert transport.scripts == []
    assert rows(queue) == {}


def test_every_remote_checkout_including_missing_ones_lands_on_main_at_the_target(
    tmp_path: Path, fleet
) -> None:
    origin, transport, config, queue, home = fleet
    pinned = Pinned("x", {}, {"bin/orch": hashlib.sha256(ORCH_SCRIPT.encode()).hexdigest()})

    results = run(origin, transport, config, queue, pinned=pinned)

    assert {r.state for r in results} == {"synced"}
    assert set(states(queue).values()) == {"synced"}
    for name, bot in (("pc", "claude-bot"), ("pc", "codex-bot"), ("mac", "claude-bot"),
                      ("lab", "claude-bot")):
        assert on_main_at(checkout(tmp_path, name, bot), origin.new)
    assert not checkout(tmp_path, "box").exists()
    assert not checkout(tmp_path, "off").exists()
    assert len((home / "install.log").read_text().splitlines()) == 4

    transport.scripts.clear()
    run(origin, transport, config, queue, pinned=pinned)
    assert transport.deploys() == []


@pytest.mark.parametrize("detach", [True, False])
def test_a_detached_or_behind_checkout_moves_to_main_at_the_target(
    tmp_path: Path, fleet, detach: bool
) -> None:
    origin, transport, config, queue, _home = fleet
    mac = checkout(tmp_path, "mac")
    clone_at(origin, mac, origin.old, detach=detach)

    run(origin, transport, config, queue)

    assert on_main_at(mac, origin.new)
    assert states(queue)[("mac", "claude-bot")] == "synced"


def test_a_checkout_with_local_edits_is_refused_untouched_and_escalated_once(
    tmp_path: Path, fleet
) -> None:
    origin, transport, config, queue, _home = fleet
    mac = checkout(tmp_path, "mac")
    clone_at(origin, mac, origin.old, detach=False)
    (mac / "README").write_text("local work\n", encoding="utf-8")

    results = run(origin, transport, config, queue)

    row = rows(queue)[("mac", "claude-bot")]
    assert (row.state, row.detail) == ("refused", "tracked files have local changes")
    assert git(mac, "rev-parse", "HEAD") == origin.old
    assert (mac / "README").read_text() == "local work\n"
    assert states(queue)[("pc", "claude-bot")] == "synced"

    posts: list[object] = []
    webhook = Webhook(url_env="HOOK_URL")
    state = tmp_path / "state"
    state.mkdir()
    for _ in range(2):
        device_deploy.report(
            results, state, sha=origin.new, device="box", webhook=webhook,
            environ={"HOOK_URL": "https://hook.invalid", "FACTORY_WEBHOOK_AUTH": "a",
                     "FACTORY_WEBHOOK_KEY": "k"},
            now=1.0,
            post=lambda *args: posts.append(args) or SimpleNamespace(status=200),
        )
    assert len(posts) == 1
    assert "mac claude-bot refused: tracked files have local changes" in (
        state / "self-heal.log").read_text()


def test_a_checkout_with_local_commits_is_refused(tmp_path: Path, fleet) -> None:
    origin, transport, config, queue, _home = fleet
    mac = checkout(tmp_path, "mac")
    clone_at(origin, mac, origin.old, detach=False)
    (mac / "LOCAL").write_text("x\n", encoding="utf-8")
    git(mac, "add", "LOCAL")
    git(mac, "commit", "-qm", "local")
    local = git(mac, "rev-parse", "HEAD")

    run(origin, transport, config, queue)

    row = rows(queue)[("mac", "claude-bot")]
    assert (row.state, row.detail) == ("refused", "HEAD has commits not on origin/main")
    assert git(mac, "rev-parse", "HEAD") == local


def test_a_busy_device_is_left_alone_drains_and_deploys_once_idle(tmp_path: Path, fleet) -> None:
    origin, transport, config, queue, _home = fleet
    queue.enqueue(
        JobRequest(repo="o/r", ticket_id="1", action="build", recipient="codex",
                   source_comment_id="c1", timeout_seconds=60),
        dedupe_key="1",
    )
    with queue.connect() as connection:
        connection.execute("UPDATE jobs SET state = 'running'")
        job_id = connection.execute("SELECT id FROM jobs").fetchone()[0]
        connection.execute(
            "INSERT INTO device_slots (job_id, device, harness, os_user, placed_at) "
            "VALUES (?, 'pc', 'codex', 'codex-bot', 1.0)", (job_id,))

    run(origin, transport, config, queue)

    pc = rows(queue)[("pc", "claude-bot")]
    assert (pc.state, pc.detail) == ("missing", "busy (1 jobs), retry next pass")
    assert not any(alias.startswith("pc-") for alias in transport.deploys())
    assert states(queue)[("mac", "claude-bot")] == "synced"
    queue.enqueue(
        JobRequest(repo="o/r", ticket_id="2", action="build", recipient="claude",
                   source_comment_id="c2", timeout_seconds=60),
        dedupe_key="2",
    )
    claim = DevicePool(queue, MachinesConfig(devices=(config.device("pc"),))).claim("claude")
    assert claim is None

    with queue.connect() as connection:
        connection.execute("DELETE FROM device_slots")
    run(origin, transport, config, queue)

    assert states(queue)[("pc", "claude-bot")] == "synced"
    assert on_main_at(checkout(tmp_path, "pc"), origin.new)


def test_a_target_left_deploying_by_a_dead_pass_is_reobserved(tmp_path: Path, fleet) -> None:
    origin, transport, config, queue, _home = fleet
    run(origin, transport, config, queue)
    with queue.connect() as connection:
        device_sync.upsert(connection, device="mac", bot="claude-bot", harness="claude",
                           checkout=str(checkout(tmp_path, "mac")), target=origin.new, sha=None,
                           state="deploying", detail="", now=1.0)

    run(origin, transport, config, queue)

    assert states(queue)[("mac", "claude-bot")] == "synced"


def test_an_unreachable_device_is_recorded_and_the_rest_proceed(tmp_path: Path, fleet) -> None:
    origin, transport, config, queue, _home = fleet
    transport.offline.add("lab-claude-bot")

    run(origin, transport, config, queue)

    assert states(queue)[("lab", "claude-bot")] == "unreachable"
    assert states(queue)[("mac", "claude-bot")] == "synced"


def test_a_dry_run_observes_and_writes_nothing(tmp_path: Path, fleet) -> None:
    origin, transport, config, queue, _home = fleet

    results = run(origin, transport, config, queue, dry_run=True)

    assert {r.state for r in results} == {"missing"}
    assert transport.deploys() == []
    assert rows(queue) == {}
    assert not checkout(tmp_path, "mac").exists()


def test_a_device_removed_from_machines_yml_loses_its_row(tmp_path: Path, fleet) -> None:
    origin, transport, config, queue, _home = fleet
    run(origin, transport, config, queue)
    smaller = MachinesConfig(devices=tuple(d for d in config.devices if d.name != "lab"))

    run(origin, transport, smaller, queue)

    assert ("lab", "claude-bot") not in rows(queue)


def test_cli_runs_one_pass_and_skips_when_the_lock_is_held(tmp_path: Path, fleet, monkeypatch) -> None:
    import fcntl

    origin, transport, _config, _queue, _home = fleet
    state = tmp_path / "cli-state"
    state.mkdir()
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n"
        "  - name: box\n    local: true\n    slot_max: 1\n    harnesses: {claude: claude-bot}\n"
        "  - name: mac\n    tailscale_name: mac.example.ts.net\n    slot_max: 1\n"
        "    harnesses: {claude: claude-bot}\n"
        f"    workdir_root: {tmp_path}/cli/{{bot}}/work\n    ssh_alias: mac-{{bot}}\n"
        f"    factory_root: {tmp_path}/cli/{{bot}}/factory\n",
        encoding="utf-8",
    )
    box = tmp_path / "box-checkout"
    clone_at(origin, box, origin.new, detach=False)
    git(box, "remote", "set-url", "origin", "https://github.com/acme/factory.git")
    monkeypatch.setattr(device_deploy, "url_template", lambda _explicit: origin.url)
    monkeypatch.setattr(device_deploy, "pinned_files", lambda _root: Pinned("x", {}, {}))
    argv = ["--state", str(state), "--db", str(tmp_path / "cli.db"), "--checkout", str(box),
            "--machines", str(machines), "--orch", str(tmp_path / "absent-orch.yml")]

    with open(state / device_deploy.LOCK_NAME, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert device_deploy.main(argv, transport=transport) == 0
        assert transport.scripts == []

    assert device_deploy.main(argv, transport=transport) == 0
    assert on_main_at(tmp_path / "cli" / "claude-bot" / "factory", origin.new)
