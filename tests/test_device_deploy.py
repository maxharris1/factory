"""`orch heal devices` (issue #297): the real deploy script against real git repos.

The transport runs each ssh script locally under `sh -c`, as sshd would on
the device, so the clone, fetch, refusal and checkout paths all run for real.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from lib import device_deploy, device_sync, job_token, skill_preflight
from lib.device_pool import DevicePool
from lib.heal import Webhook
from lib.machines_yml import Device, MachinesConfig, load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, Called
from lib.skill_preflight import Pinned
from lib.waiter import Waiter
from tests.pool_helpers import write_target_machines

ORCH_SCRIPT = (
    "import os, sys\n"
    "with open(os.path.expanduser('~/install.log'), 'a') as log:\n"
    "    log.write(os.getcwd() + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
)
REAL_PREFLIGHT = skill_preflight.preflight


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
    def __init__(self, home: Path):
        self.home = home
        self.offline: set[str] = set()
        self.scripts: list[tuple[str, str]] = []

    def call(self, argv, *, timeout: float, input: bytes | None = None) -> Called:
        alias, script = argv[-2], argv[-1]
        self.scripts.append((alias, script))
        if alias in self.offline:
            return Called(SSH_UNREACHABLE, "", "")
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


def test_every_bot_on_the_shared_box_checkout_is_its_own_target_logged_in_as_box(
    tmp_path: Path,
) -> None:
    config, _ = load_machines_yml(write_target_machines(tmp_path))

    found = [(t.device, t.bot, t.checkout, t.remote.user) for t in device_deploy.targets(config)]

    assert found == [
        ("pc", "claude-bot", "/home/claude-bot/factory", "claude-bot"),
        ("pc", "codex-bot", "/home/codex-bot/factory", "codex-bot"),
        ("pc", "cursor-bot", "/home/cursor-bot/factory", "cursor-bot"),
        ("box", "claude-bot", "/home/box/factory", "box"),
        ("box", "codex-bot", "/home/box/factory", "box"),
        ("box", "cursor-bot", "/home/box/factory", "box"),
    ]


def shared_login_device(tmp_path: Path, name: str, *harnesses: str) -> Device:
    return Device(
        name=name,
        slot_max=2,
        harnesses=MappingProxyType({h: f"{h}-bot" for h in harnesses}),
        tailscale_name=f"{name}.example.ts.net",
        workdir_root=f"{tmp_path}/{name}/work",
        ssh_alias=f"{name}-{{bot}}",
        factory_root=f"{tmp_path}/{name}/factory",
        ssh_user="box",
    )


def test_a_dirty_shared_checkout_refuses_and_blocks_every_harness_on_it(tmp_path: Path) -> None:
    origin = Origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    config = MachinesConfig(devices=(
        device(tmp_path, "box", "claude", local=True),
        shared_login_device(tmp_path, "remote", "claude", "codex"),
    ))
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 100.0)
    pool = DevicePool(queue, config)
    path = tmp_path / "remote" / "factory"
    clone_at(origin, path, origin.old, detach=False)
    (path / "README").write_text("local work\n", encoding="utf-8")

    results = run(origin, LocalTransport(home), config, queue)

    assert {(r.target.harness, r.state) for r in results} == {
        ("claude", "refused"), ("codex", "refused")}
    assert (path / "README").read_text() == "local work\n"
    with queue.connect() as connection:
        gate = device_sync.blocked(connection)
    assert gate.blocks("remote", "claude") and gate.blocks("remote", "codex")
    queue.enqueue(JobRequest(repo="o/r", ticket_id="1", action="build", recipient="codex",
                             source_comment_id="c1", timeout_seconds=60), dedupe_key="1")
    assert pool.claim("codex") is None


INSTALL_ALL_SKILLS = (
    "import os\n"
    "for d in ('.claude/skills', '.agents/skills', '.cursor/skills'):\n"
    "    path = os.path.expanduser(f'~/{d}/how/SKILL.md')\n"
    "    os.makedirs(os.path.dirname(path), exist_ok=True)\n"
    "    with open(path, 'w') as f:\n"
    "        f.write('how\\n')\n"
)


def test_one_harness_missing_its_skills_on_a_shared_checkout_gets_them_back(
    tmp_path: Path,
) -> None:
    origin = Origin(tmp_path)
    sha = origin.commit("bin/orch", INSTALL_ALL_SKILLS)
    home = tmp_path / "home"
    claude_skill = home / ".claude" / "skills" / "how" / "SKILL.md"
    codex_skill = home / ".agents" / "skills" / "how" / "SKILL.md"
    claude_skill.parent.mkdir(parents=True)
    claude_skill.write_text("how\n", encoding="utf-8")
    config = MachinesConfig(devices=(
        device(tmp_path, "box", "claude", local=True),
        shared_login_device(tmp_path, "remote", "claude", "codex"),
    ))
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 100.0)
    DevicePool(queue, config)
    clone_at(origin, tmp_path / "remote" / "factory", sha, detach=False)
    pinned = Pinned("x", {"how/SKILL.md": hashlib.sha256(b"how\n").hexdigest()}, {})

    results = device_deploy.run_pass(queue, config, sha=sha, url=origin.url, pinned=pinned,
                                     transport=LocalTransport(home))

    assert codex_skill.read_text() == "how\n"
    assert {(r.target.harness, r.state) for r in results} == {
        ("claude", "synced"), ("codex", "synced")}


def test_deploys_to_one_checkout_never_overlap_while_other_checkouts_deploy_alongside(
    tmp_path: Path,
) -> None:
    origin = Origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    config = MachinesConfig(devices=(
        shared_login_device(tmp_path, "remote", "claude", "codex"),
        device(tmp_path, "pc", "claude"),
    ))
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 100.0)
    DevicePool(queue, config)
    shared_checkout = str(tmp_path / "remote" / "factory")
    where = {"remote-claude-bot": shared_checkout, "remote-codex-bot": shared_checkout,
             "pc-claude-bot": str(checkout(tmp_path, "pc"))}

    class CountingTransport(LocalTransport):
        def __init__(self, home: Path):
            super().__init__(home)
            self.lock = threading.Lock()
            self.flying: Counter[str] = Counter()
            self.most: Counter[str] = Counter()
            self.together = threading.Event()

        def call(self, argv, **kwargs):
            if "orch-sync" not in argv[-1]:
                return super().call(argv, **kwargs)
            path = where[argv[-2]]
            with self.lock:
                self.flying[path] += 1
                self.most[path] = max(self.most[path], self.flying[path])
                if len(+self.flying) == 2:
                    self.together.set()
            self.together.wait(5)
            try:
                return super().call(argv, **kwargs)
            finally:
                with self.lock:
                    self.flying[path] -= 1

    transport = CountingTransport(home)

    results = run(origin, transport, config, queue)

    assert {r.state for r in results} == {"synced"} and len(results) == 3
    assert transport.together.is_set(), "two checkouts deployed at once"
    assert transport.most == {shared_checkout: 1, where["pc-claude-bot"]: 1}


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


def test_a_busy_device_is_synced_and_its_running_job_left_alone(tmp_path: Path, fleet) -> None:
    origin, transport, config, queue, home = fleet
    pc = checkout(tmp_path, "pc")
    clone_at(origin, pc, origin.old, detach=False)
    queue.enqueue(
        JobRequest(repo="o/r", ticket_id="1", action="build", recipient="claude",
                   source_comment_id="c1", timeout_seconds=60),
        dedupe_key="1",
    )
    with queue.connect() as connection:
        connection.execute("UPDATE jobs SET state = 'running'")
        job_id = connection.execute("SELECT id FROM jobs").fetchone()[0]
        connection.execute(
            "INSERT INTO device_slots (job_id, device, harness, os_user, placed_at) "
            "VALUES (?, 'pc', 'claude', 'claude-bot', 1.0)", (job_id,))
    job_dir = tmp_path / "pc" / "claude-bot" / "work" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "work.txt").write_text("in progress\n", encoding="utf-8")

    results = run(origin, transport, config, queue)

    row = rows(queue)[("pc", "claude-bot")]
    assert (row.state, row.detail) == ("synced", "")
    assert on_main_at(pc, origin.new)
    assert f"{pc} install-skills" in (home / "install.log").read_text().splitlines()
    with queue.connect() as connection:
        assert connection.execute("SELECT state FROM jobs").fetchone()[0] == "running"
        assert connection.execute(
            "SELECT device FROM device_slots WHERE job_id = ?", (job_id,)).fetchone()[0] == "pc"
    assert (job_dir / "work.txt").read_text() == "in progress\n"

    state = tmp_path / "state"
    state.mkdir()
    device_deploy.report(results, state, sha=origin.new, device="box",
                         webhook=Webhook(url_env="HOOK_URL"), environ={}, now=1.0)
    last = json.loads((state / device_deploy.STATE_NAME).read_text())["last"]
    assert last["pc claude-bot"] == "synced"


@pytest.mark.parametrize("state", ["lagging", "synced", "refused"])
def test_only_the_deployer_replaces_a_deploying_row(fleet, state: str) -> None:
    _origin, _transport, _config, queue, _home = fleet

    def write(state: str, now: float, *, replaces_deploying: bool = False) -> None:
        with queue.connect() as connection:
            device_sync.upsert(connection, device="pc", bot="claude-bot", harness="claude",
                               checkout="/c", target=None, sha=None, state=state,
                               detail=state, now=now, replaces_deploying=replaces_deploying)

    write("deploying", 1.0, replaces_deploying=True)
    write(state, 2.0)
    assert rows(queue)[("pc", "claude-bot")].state == "deploying"
    with queue.connect() as connection:
        assert device_sync.blocked(connection).devices == {"pc"}
    write(state, 3.0, replaces_deploying=True)
    assert rows(queue)[("pc", "claude-bot")].state == state


def test_a_lagging_preflight_during_a_busy_deploy_keeps_the_device_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    origin = Origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    config = MachinesConfig(devices=(device(tmp_path, "pc", "claude", "codex"),))
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 100.0)
    pool = DevicePool(queue, config)
    clone_at(origin, checkout(tmp_path, "pc"), origin.old, detach=False)
    clone_at(origin, checkout(tmp_path, "pc", "codex-bot"), origin.new, detach=False)
    for harness in ("claude", "codex"):
        queue.enqueue(JobRequest(repo="o/r", ticket_id=harness, action="build",
                                 recipient=harness, source_comment_id=harness,
                                 timeout_seconds=60), dedupe_key=harness)
    proof, token_hash = job_token.mint()
    job, placement = pool.claim("claude", autostart=(12345, None), token_hash=token_hash)
    deploying = threading.Event()
    resume = threading.Event()

    class PausingTransport(LocalTransport):
        def call(self, argv, **kwargs):
            if "orch-sync" in argv[-1]:
                deploying.set()
                assert resume.wait(10)
            return super().call(argv, **kwargs)

    transport = PausingTransport(home)
    monkeypatch.setattr(skill_preflight, "preflight", REAL_PREFLIGHT)
    monkeypatch.setattr(skill_preflight, "ROOT", origin.work)
    monkeypatch.setattr(skill_preflight, "pinned_files", lambda _root: Pinned("x", {}, {}))
    monkeypatch.setattr(skill_preflight, "SshTransport", lambda: transport)
    waiter = object.__new__(Waiter)
    waiter.queue, waiter.pool = queue, pool
    waiter.bot, waiter.recipient = "claude-bot", "claude"
    waiter._claimer = (12345, None)
    waiter.log = lambda _line: None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(device_deploy.run_pass, queue, config,
                                 sha=origin.new, url=origin.url,
                                 pinned=Pinned("x", {}, {}), transport=transport)
        try:
            assert deploying.wait(10)
            assert waiter._start(job, placement, proof) is None
            assert rows(queue)[("pc", "claude-bot")].state == "deploying"
            assert pool.claim("codex") is None
        finally:
            resume.set()
            future.result(timeout=20)
    assert states(queue)[("pc", "claude-bot")] == "synced"
    assert pool.claim("codex") is not None


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


def seed_box_row(queue: SQLiteJobQueue) -> None:
    with queue.connect() as connection:
        device_sync.upsert(connection, device="box", bot="claude-bot", harness="claude",
                           checkout="/srv/factory", target=None, sha=None,
                           state=device_sync.LAGGING, detail="preflight-lagging", now=1)
        connection.commit()


def test_a_device_removed_from_machines_yml_loses_its_row_and_the_box_keeps_its_own(
    tmp_path: Path, fleet
) -> None:
    origin, transport, config, queue, _home = fleet
    run(origin, transport, config, queue)
    seed_box_row(queue)
    smaller = MachinesConfig(devices=tuple(d for d in config.devices if d.name != "lab"))

    run(origin, transport, smaller, queue)

    assert ("lab", "claude-bot") not in rows(queue)
    assert states(queue)[("box", "claude-bot")] == "lagging"


def test_removing_the_last_remote_device_still_drops_its_row(tmp_path: Path, fleet) -> None:
    _origin, transport, _config, queue, _home = fleet
    with queue.connect() as connection:
        device_sync.upsert(connection, device="removed-pc", bot="claude-bot", harness="claude",
                           checkout="/home/claude-bot/factory", target="a" * 40, sha=None,
                           state=device_sync.MISSING, detail="", now=1)
        connection.commit()
    seed_box_row(queue)
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n  - name: box\n    local: true\n    slot_max: 2\n"
        "    harnesses:\n      claude: claude-bot\n",
        encoding="utf-8",
    )

    code = device_deploy.main(["--state", str(tmp_path / "state"), "--db", str(queue.path),
                               "--machines", str(machines)], transport=transport)

    assert code == 0
    assert transport.scripts == []
    assert list(states(queue)) == [("box", "claude-bot")]


def test_an_ignored_local_file_upstream_starts_tracking_is_refused_untouched(
    tmp_path: Path, fleet
) -> None:
    origin, transport, config, _queue, _home = fleet
    before = origin.commit(".gitignore", "*.cache\n")
    (origin.work / "important.cache").write_text("upstream\n", encoding="utf-8")
    git(origin.work, "add", "-f", "important.cache")
    git(origin.work, "commit", "-qm", "track a cache file")
    git(origin.work, "push", "-q", "origin", "main")
    after = git(origin.work, "rev-parse", "HEAD")
    path = checkout(tmp_path, "mac")
    clone_at(origin, path, before, detach=False)
    (path / "important.cache").write_text("local work\n", encoding="utf-8")
    target = next(t for t in device_deploy.targets(config) if t.device == "mac")

    reason = device_deploy.run_deploy(transport, target, url=origin.url, sha=after)

    assert reason == "git checkout exited 1 (untracked or ignored files in the way?)"
    assert git(path, "rev-parse", "HEAD") == before
    assert (path / "important.cache").read_text(encoding="utf-8") == "local work\n"


def test_install_skills_runs_under_the_python_the_installed_wrapper_names(
    tmp_path: Path, fleet
) -> None:
    origin, transport, config, _queue, home = fleet
    managed = origin.commit(
        "bin/orch",
        "import os, sys\n"
        "sys.exit(0 if os.environ.get('FACTORY_VENV') == '1' else 1)\n",
    )
    target = next(t for t in device_deploy.targets(config) if t.device == "mac")

    assert device_deploy.run_deploy(transport, target, url=origin.url, sha=managed) == (
        "install-skills exited 1")

    venv = home / "venv" / "bin" / "python"
    venv.parent.mkdir(parents=True)
    venv.write_text(f'#!/bin/sh\nFACTORY_VENV=1 exec "{sys.executable}" "$@"\n', encoding="utf-8")
    venv.chmod(0o755)
    wrapper = home / ".local" / "bin" / "orch"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        f'#!/usr/bin/env bash\n# factory-orch-wrapper\nexec "{venv}" "/elsewhere/bin/orch" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    assert device_deploy.run_deploy(transport, target, url=origin.url, sha=managed) is None
    assert on_main_at(Path(target.checkout), managed)


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
