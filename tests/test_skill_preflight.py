"""The waiter's skill preflight on the placed device (issue #179, SF-7).

Before a harness starts, every file skills/skills.lock pins must be on the
placed device with the hash the lock records: the installed skills under the
bot's skills directory, and the whole pinned tree in the device's deployed
factory checkout, which the routers read pstack's docs/ and agents/ and
skills/factory/harness/ from. The box is read in place; a remote device
answers one ssh call (tests/fake_ssh.py here, which runs the real script
against a fake home).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sqlite3
import time
from pathlib import Path

import pytest

from lib import device_sync, skill_preflight
from lib.device_pool import DevicePool, Placement
from lib.machines_yml import Device as MachinesDevice, load_machines_yml
from lib.pstack_pin import install_skills
from lib.remote import SshTransport
from lib.queue import SQLiteJobQueue
from lib import waiter
from lib.waiter import HarnessCommand, Waiter
from tests.test_remote_start import Ssh
from tests.test_waiter import FakeContext, FakeSpawn, enqueue, state
from tests.test_waiter_remote import remote_machines

REPO = Path(__file__).resolve().parents[1]

PSTACK = {
    "skills/interrogate/SKILL.md": "interrogate: run arena\n",
    "skills/arena/SKILL.md": "arena\n",
    "skills/arena/references/rounds.md": "rounds\n",
    "README.md": "not installed\n",
    "LICENSE": "pstack license\n",
    "docs/harness/claude-code.md": "claude adapter\n",
    "agents/poteto-agent.md": "agent\n",
}
FACTORY = {
    "factory-builder/SKILL.md": "builder\n",
    "harness/codex.md": "not a router\n",
}
KIT = {
    "LICENSE": "kit license\n",
    "deslop/SKILL.md": "deslop\n",
}
INSTALLED = {
    "interrogate/SKILL.md": PSTACK["skills/interrogate/SKILL.md"],
    "arena/SKILL.md": PSTACK["skills/arena/SKILL.md"],
    "arena/references/rounds.md": PSTACK["skills/arena/references/rounds.md"],
    "factory-builder/SKILL.md": FACTORY["factory-builder/SKILL.md"],
    "deslop/SKILL.md": KIT["deslop/SKILL.md"],
}
SECTIONS = (("files", "skills/pstack", PSTACK), ("factory", "skills/factory", FACTORY),
            ("kit", "skills/cursor-team-kit", KIT))


@pytest.fixture
def _skill_preflight():
    """The real preflight, over the suite-wide no-op in tests/conftest.py."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def lock_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "factory"
    lines = ['version: "1.0.0"', f"commit: {'a' * 40}", 'token: "t"']
    for key, _dir, tree in SECTIONS:
        lines.append(f"{key}:")
        lines += [f"  {rel}: {_sha(text)}" for rel, text in sorted(tree.items())]
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "skills.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(skill_preflight, "ROOT", root)
    monkeypatch.setattr(waiter, "FACTORY_CHECKOUT", root)
    return root


def deploy(checkout: Path, *, skip: str | None = None, change: str | None = None) -> None:
    """The deployed factory checkout: every file each lock section pins."""
    for rel, text in checkout_tree().items():
        if rel == skip:
            continue
        path = checkout / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("edited\n" if rel == change else ""), encoding="utf-8")


def checkout_tree() -> dict[str, str]:
    return {f"{base}/{rel}": text for _key, base, tree in SECTIONS for rel, text in tree.items()}


def checkout_files(checkout: Path) -> dict[str, str]:
    return {f"{checkout}/{rel}": _sha(text) for rel, text in checkout_tree().items()}


def install(home: Path, *, skip: str | None = None, change: str | None = None) -> None:
    for rel, text in INSTALLED.items():
        if rel == skip:
            continue
        path = home / ".claude" / "skills" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("edited\n" if rel == change else ""), encoding="utf-8")


def placed(skills: Path) -> dict[str, str]:
    """{path under the skills dir: sha256} for every file there, through symlinks, bar install markers."""
    found: dict[str, str] = {}
    for dirpath, _dirs, names in os.walk(skills, followlinks=True):
        for name in names:
            path = Path(dirpath) / name
            rel = path.relative_to(skills).as_posix()
            if name != ".orch-owned":
                found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def detail_of(db: Path, job_id: str) -> dict:
    with sqlite3.connect(db) as connection:
        raw = connection.execute("SELECT detail FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    return json.loads(raw)


class Device:
    """A waiter for claude-bot on the box or the fake-ssh pc, with a spawn that
    notes how many ssh calls had happened when the run started."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, remote: bool,
                 box_root: Path | None = None):
        self.ssh = Ssh(tmp_path)
        for name, value in self.ssh.env().items():
            monkeypatch.setenv(name, value)
        self.home = tmp_path / ("device-home" if remote else "box-home")
        self.home.mkdir()
        monkeypatch.setenv("FAKE_SSH_HOME", str(self.home))
        if not remote:
            monkeypatch.setenv("HOME", str(self.home))
        # Only the device under test is enabled, so it takes the job (issue
        # #223 sends an empty-pool tie off-box).
        machines = remote_machines(tmp_path, box_enabled=not remote, box_slots=5, pc=remote)
        self.checkout = tmp_path / "pc-factory"
        text = machines.read_text(encoding="utf-8")
        line = "    factory_root: /srv/factory\n"
        text = text.replace(line, f"    factory_root: {self.checkout}\n")
        if box_root is not None:
            text = text.replace("    local: true\n", f"    local: true\n    factory_root: {box_root}\n", 1)
        machines.write_text(text, encoding="utf-8")
        self.db = tmp_path / "orch.db"
        self.queue = SQLiteJobQueue(self.db, clock=time.time)
        config, findings = load_machines_yml(machines, allow_placeholders=True)
        assert config is not None, findings
        self.spawn = FakeSpawn()
        self.ssh_calls_at_spawn: list[int] = []
        self.log: list[str] = []

        def spawn(argv, **kwargs):
            self.ssh_calls_at_spawn.append(len(self.ssh.calls()))
            return self.spawn(argv, **kwargs)

        self.waiter = Waiter(
            bot="claude-bot",
            queue=self.queue,
            pool=DevicePool(self.queue, config),
            harness=HarnessCommand(bot="claude-bot", command=("fake-harness",)),
            context=FakeContext(),
            state_dir=tmp_path / "state",
            db=self.db,
            machines=machines,
            orch=("python3", "/factory/bin/orch"),
            spawn=spawn,
            log=self.log.append,
            environ={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )

    def run_one(self) -> tuple[str, str | None]:
        job_id = enqueue(self.queue, "a")
        return job_id, self.waiter.step()

    def brief(self) -> str:
        [stdin] = self.spawn.stdin
        return stdin.decode("utf-8")


def test_the_pinned_set_is_every_installed_skill_file_and_the_whole_checkout(lock_root: Path) -> None:
    pinned = skill_preflight.pinned_files(lock_root)
    assert pinned.commit == "a" * 40
    assert pinned.installed == {rel: _sha(text) for rel, text in INSTALLED.items()}
    assert pinned.checkout == {rel: _sha(text) for rel, text in checkout_tree().items()}


def test_the_pinned_set_is_exactly_what_a_real_install_places(tmp_path: Path) -> None:
    root = tmp_path / "factory"
    shutil.copytree(REPO / "skills", root / "skills")
    home = tmp_path / "home"
    assert install_skills(root, home, "local") == []

    pinned = skill_preflight.pinned_files(root)

    for skills in (".claude/skills", ".agents/skills", ".cursor/skills"):
        assert placed(home / skills) == pinned.installed
    assert "deslop/SKILL.md" in pinned.installed
    assert "skills/cursor-team-kit/deslop/SKILL.md" in pinned.checkout


def test_the_pinned_set_covers_the_real_lock() -> None:
    pinned = skill_preflight.pinned_files(REPO)
    for rel in ("interrogate/SKILL.md", "arena/SKILL.md", "figure-it-out/SKILL.md",
                "show-me-your-work/SKILL.md", "architect/SKILL.md", "factory-builder/SKILL.md"):
        assert rel in pinned.installed
    assert not any(rel.startswith("harness/") for rel in pinned.installed)
    for rel in ("skills/pstack/docs/harness/claude-code.md", "skills/pstack/agents/poteto-agent.md",
                "skills/factory/harness/codex.md", "skills/pstack/skills/poteto-mode/SKILL.md"):
        assert rel in pinned.checkout


@pytest.mark.parametrize("defect", ["missing", "hash"])
@pytest.mark.parametrize(
    "rel", ["skills/pstack/docs/harness/claude-code.md", "skills/pstack/agents/poteto-agent.md",
            "skills/factory/harness/codex.md"],
)
def test_a_real_install_with_a_router_dependency_missing_or_changed_fails(
    tmp_path: Path, defect: str, rel: str
) -> None:
    root = tmp_path / "factory"
    shutil.copytree(REPO / "skills", root / "skills")
    home = tmp_path / "home"
    assert install_skills(root, home, "local") == []
    report = skill_preflight.check(bot="claude-bot", placement=None, device=None,
                                   transport=SshTransport(), home=home,
                                   checkout=str(root.resolve()), root=root)
    assert report.error is None
    target = root / rel
    if defect == "missing":
        target.unlink()
    else:
        target.write_text("changed adapter\n", encoding="utf-8")

    report = skill_preflight.check(bot="claude-bot", placement=None, device=None,
                                   transport=SshTransport(), home=home,
                                   checkout=str(root.resolve()), root=root)

    assert report.error == f"preflight-{defect}: {root.resolve()}/{rel}"


@pytest.mark.parametrize("rel", ["arena/SKILL.md", "deslop/SKILL.md"])
def test_a_remote_missing_a_referenced_skill_starts_no_harness_and_fails_with_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path, rel: str
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home, skip=rel)
    deploy(device.checkout)

    job_id, started = device.run_one()

    assert started is None
    assert device.spawn.calls == []
    assert state(device.db, job_id) == "failed"
    assert any(f"preflight-missing: ~/.claude/skills/{rel}" in line for line in device.log)
    entry = detail_of(device.db, job_id)["skill_preflight"]
    assert entry["error"] == f"preflight-missing: ~/.claude/skills/{rel}"
    assert entry["device"] == "pc"
    assert len(device.ssh.calls()) == 1


def test_a_remote_hash_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home, change="arena/references/rounds.md")
    deploy(device.checkout)

    job_id, started = device.run_one()

    assert started is None
    assert device.spawn.calls == []
    assert state(device.db, job_id) == "failed"
    error = "preflight-hash: ~/.claude/skills/arena/references/rounds.md"
    assert any(error in line for line in device.log)
    assert detail_of(device.db, job_id)["skill_preflight"]["error"] == error


def test_with_everything_present_the_job_detail_lists_paths_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)
    deploy(device.checkout)

    job_id, started = device.run_one()

    assert started == job_id
    assert state(device.db, job_id) == "running"
    detail = detail_of(device.db, job_id)
    assert detail["risk"]  # the dispatcher's detail is kept
    entry = detail["skill_preflight"]
    assert entry == {
        "device": "pc",
        "root": "~/.claude/skills",
        "checkout": str(device.checkout),
        "commit": "a" * 40,
        "files": {f"~/.claude/skills/{rel}": _sha(text) for rel, text in sorted(INSTALLED.items())}
        | checkout_files(device.checkout),
    }


def test_remote_preflight_is_one_ssh_round_trip_before_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)
    deploy(device.checkout)

    device.run_one()

    # The waiter's own ssh: exactly the preflight, before it spawns
    # `orch job run` (which does the detached launch, #128/#129).
    assert device.ssh_calls_at_spawn == [1]
    [call] = device.ssh.calls()
    assert call["alias"] == "fakepc-claude-bot" and call["user"] == "claude-bot"
    assert "orch-skills" in call["script"]


@pytest.mark.parametrize(
    "defect, rel",
    [("missing", "skills/pstack/docs/harness/claude-code.md"),
     ("hash", "skills/pstack/docs/harness/claude-code.md"),
     ("missing", "skills/factory/harness/codex.md"),
     ("hash", "skills/pstack/agents/poteto-agent.md"),
     ("missing", "skills/cursor-team-kit/deslop/SKILL.md"),
     ("hash", "skills/cursor-team-kit/LICENSE")],
)
def test_a_remote_checkout_missing_or_changing_a_router_dependency_fails_in_one_ssh_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path, defect: str, rel: str
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)
    deploy(device.checkout, **({"skip": rel} if defect == "missing" else {"change": rel}))

    job_id, started = device.run_one()

    assert started is None
    assert device.spawn.calls == []
    assert state(device.db, job_id) == "failed"
    error = f"preflight-{defect}: {device.checkout}/{rel}"
    assert any(error in line for line in device.log)
    assert detail_of(device.db, job_id)["skill_preflight"]["error"] == error
    assert len(device.ssh.calls()) == 1


def test_a_remote_without_a_factory_root_fails_without_ssh(tmp_path: Path, lock_root: Path) -> None:
    pc = MachinesDevice(name="pc", slot_max=1, harnesses={"claude": "claude-bot"},
                        workdir_root="/w/{bot}", ssh_alias="fakepc-{bot}")
    placement = Placement(job_id="j", device="pc", harness="claude", os_user="claude-bot", placed_at=0.0)

    class NoSsh:
        def call(self, *args, **kwargs):
            raise AssertionError("no ssh without a factory_root")

    with pytest.raises(skill_preflight.PreflightError, match="^preflight: pc has no factory_root for claude$"):
        skill_preflight.check(bot="claude-bot", placement=placement, device=pc, transport=NoSsh(),
                              home=tmp_path, checkout=skill_preflight.checkout_root(pc, "claude", lock_root),
                              root=lock_root)


def test_an_unreachable_remote_fails_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)
    deploy(device.checkout)
    device.ssh.offline("fakepc-claude-bot").touch()

    job_id, started = device.run_one()

    assert started is None
    assert device.spawn.calls == []
    assert state(device.db, job_id) == "failed"
    assert detail_of(device.db, job_id)["skill_preflight"]["error"].startswith(
        "preflight-unreachable: pc"
    )


def test_the_box_is_checked_in_place_without_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=False)
    install(device.home, skip="interrogate/SKILL.md")
    deploy(lock_root)

    job_id, started = device.run_one()

    assert started is None
    assert device.ssh.calls() == []
    assert detail_of(device.db, job_id)["skill_preflight"]["error"] == (
        "preflight-missing: ~/.claude/skills/interrogate/SKILL.md"
    )


def test_a_complete_box_starts_and_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=False)
    install(device.home)
    deploy(lock_root)

    job_id, started = device.run_one()

    assert started == job_id
    assert device.ssh.calls() == []
    entry = detail_of(device.db, job_id)["skill_preflight"]
    assert entry["device"] == "box"
    assert entry["checkout"] == str(lock_root.resolve())
    assert set(entry["files"]) == {f"~/.claude/skills/{rel}" for rel in INSTALLED} | set(
        checkout_files(lock_root.resolve())
    )


def test_the_box_checks_its_checkout_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=False)
    install(device.home)
    deploy(lock_root, skip="skills/pstack/docs/harness/claude-code.md")

    job_id, started = device.run_one()

    assert started is None
    assert device.ssh.calls() == []
    assert detail_of(device.db, job_id)["skill_preflight"]["error"] == (
        f"preflight-missing: {lock_root.resolve()}/skills/pstack/docs/harness/claude-code.md"
    )


def test_a_symlinked_skill_dir_counts(tmp_path: Path, lock_root: Path) -> None:
    source = tmp_path / "source"
    install(source)
    skills = tmp_path / "home" / ".claude" / "skills"
    skills.mkdir(parents=True)
    for name in ("interrogate", "arena", "factory-builder", "deslop"):
        (skills / name).symlink_to(source / ".claude" / "skills" / name)
    deploy(lock_root)
    expected = skill_preflight.wanted(".claude/skills", str(lock_root), skill_preflight.pinned_files(lock_root))
    found = skill_preflight.local_hashes(tmp_path / "home", sorted(expected))
    assert skill_preflight.compare(expected, found) is None


def test_the_box_checkout_is_its_factory_root_when_machines_yml_names_one(lock_root: Path) -> None:
    config, findings = load_machines_yml(REPO / "machines.yml", allow_placeholders=True)
    assert config is not None, findings
    box = next(device for device in config.devices if device.local)
    named = box.factory_root_for("claude")
    assert named not in (None, str(lock_root.resolve()))

    assert skill_preflight.checkout_root(box, "claude", lock_root) == named
    assert skill_preflight.checkout_root(None, "claude", lock_root) == str(lock_root.resolve())


@pytest.mark.parametrize(
    "defect, rel",
    [("missing", "skills/pstack/docs/harness/claude-code.md"),
     ("hash", "skills/pstack/docs/harness/claude-code.md"),
     ("missing", "skills/factory/factory-builder/SKILL.md"),
     ("hash", "skills/cursor-team-kit/LICENSE")],
)
def test_the_box_checks_the_factory_root_it_names_in_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path, defect: str, rel: str
) -> None:
    configured = tmp_path / "configured-factory"
    device = Device(tmp_path, monkeypatch, remote=False, box_root=configured)
    install(device.home)
    deploy(lock_root)
    deploy(configured, **({"skip": rel} if defect == "missing" else {"change": rel}))

    job_id, started = device.run_one()

    assert started is None
    assert device.spawn.calls == []
    assert state(device.db, job_id) == "failed"
    error = f"preflight-{defect}: {configured}/{rel}"
    assert any(error in line for line in device.log)
    entry = detail_of(device.db, job_id)["skill_preflight"]
    assert entry["checkout"] == str(configured)
    assert entry["error"] == error


@pytest.mark.parametrize("where", ["box", "box-configured", "pc"])
def test_the_checkout_preflight_checks_is_the_one_the_brief_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path, where: str
) -> None:
    configured = tmp_path / "configured-factory"
    device = Device(tmp_path, monkeypatch, remote=where == "pc",
                    box_root=configured if where == "box-configured" else None)
    install(device.home)
    for checkout in (lock_root, configured, device.checkout):
        deploy(checkout)

    job_id, started = device.run_one()

    assert started == job_id
    checkout = detail_of(device.db, job_id)["skill_preflight"]["checkout"]
    assert checkout == {"box": str(lock_root.resolve()), "box-configured": str(configured),
                        "pc": str(device.checkout)}[where]
    assert f"{checkout}/skills/factory/factory-builder/SKILL.md" in device.brief()


def commit_checkout(checkout: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "deploy")
    return git("rev-parse", "HEAD")


def box_head(root: Path, sha: str) -> None:
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "HEAD").write_text(sha + "\n", encoding="utf-8")


def sync_rows(db: Path) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(db) as connection:
        return [(r.device, r.bot, r.state, r.detail) for r in device_sync.read(connection)]


def slot_count(db: Path) -> int:
    with sqlite3.connect(db) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM device_slots").fetchone()[0])


def test_a_remote_checkout_behind_the_box_requeues_without_a_round_and_marks_it_lagging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)
    deploy(device.checkout)
    old = commit_checkout(device.checkout)
    box_head(lock_root, "c" * 40)

    job_id, started = device.run_one()

    assert started is None
    assert device.spawn.calls == []
    assert state(device.db, job_id) == "queued"
    assert slot_count(device.db) == 0
    error = f"preflight-lagging: pc claude-bot {device.checkout} at {old[:12]}, box at {'c' * 12}"
    assert detail_of(device.db, job_id)["skill_preflight"]["error"] == error
    assert sync_rows(device.db) == [("pc", "claude-bot", "lagging", error)]
    assert any("requeued, no round charged" in line for line in device.log)
    assert len(device.ssh.calls()) == 1

    assert device.waiter.step() is None
    assert state(device.db, job_id) == "queued"
    assert len(device.ssh.calls()) == 1


def test_a_missing_remote_checkout_is_refused_by_name_before_any_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)
    box_head(lock_root, "c" * 40)

    job_id, started = device.run_one()

    assert started is None
    assert state(device.db, job_id) == "queued"
    assert detail_of(device.db, job_id)["skill_preflight"]["error"] == (
        f"preflight-no-checkout: pc claude-bot {device.checkout}"
    )
    assert [row[2] for row in sync_rows(device.db)] == ["lagging"]


def test_a_remote_checkout_on_the_box_commit_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)
    deploy(device.checkout)
    box_head(lock_root, commit_checkout(device.checkout))

    job_id, started = device.run_one()

    assert started == job_id
    assert sync_rows(device.db) == []
    assert len(device.ssh.calls()) == 1


def test_a_box_checkout_behind_its_origin_main_requeues_without_a_round_until_it_deploys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=False)
    install(device.home)
    deploy(lock_root)
    head = commit_checkout(lock_root)
    (lock_root / "later").write_text("merged\n", encoding="utf-8")
    git = ["git", "-C", str(lock_root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "later"], check=True)
    subprocess.run([*git, "commit", "-qm", "merged"], check=True)
    later = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True,
                           text=True).stdout.strip()
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", later], check=True)
    subprocess.run([*git, "reset", "-q", "--hard", head], check=True)

    job_id, started = device.run_one()

    assert started is None
    assert device.spawn.calls == []
    assert state(device.db, job_id) == "queued"
    error = (f"preflight-lagging: box claude-bot {lock_root.resolve()} at {head[:12]}, "
             f"origin/main at {later[:12]}")
    assert detail_of(device.db, job_id)["skill_preflight"]["error"] == error
    assert sync_rows(device.db) == [("box", "claude-bot", "lagging", error)]
    assert any("requeued, no round charged" in line for line in device.log)

    subprocess.run([*git, "reset", "-q", "--hard", later], check=True)
    with sqlite3.connect(device.db) as connection:
        device_sync.clear(connection, "box")

    assert device.waiter.step() == job_id
