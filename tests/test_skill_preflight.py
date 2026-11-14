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
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from lib import skill_preflight
from lib.device_pool import DevicePool, Placement
from lib.machines_yml import Device as MachinesDevice, load_machines_yml
from lib.pstack_pin import install_skills
from lib.remote import SshTransport
from lib.queue import SQLiteJobQueue
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
    "docs/harness/claude-code.md": "claude adapter\n",
    "agents/poteto-agent.md": "agent\n",
}
FACTORY = {
    "factory-builder/SKILL.md": "builder\n",
    "harness/codex.md": "not a router\n",
}
INSTALLED = {
    "interrogate/SKILL.md",
    "arena/SKILL.md",
    "arena/references/rounds.md",
    "factory-builder/SKILL.md",
}


@pytest.fixture
def _skill_preflight():
    """The real preflight, over the suite-wide no-op in tests/conftest.py."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content(rel: str) -> str:
    if rel.startswith("factory-builder/"):
        return FACTORY[rel]
    return PSTACK[f"skills/{rel}"]


@pytest.fixture
def lock_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "factory"
    lines = ['version: "1.0.0"', f"commit: {'a' * 40}", 'token: "t"', "files:"]
    lines += [f"  {rel}: {_sha(text)}" for rel, text in sorted(PSTACK.items())]
    lines.append("factory:")
    lines += [f"  {rel}: {_sha(text)}" for rel, text in sorted(FACTORY.items())]
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "skills.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(skill_preflight, "ROOT", root)
    return root


def deploy(checkout: Path, *, skip: str | None = None, change: str | None = None) -> None:
    """The deployed factory checkout: every pinned file under skills/pstack and skills/factory."""
    tree = {f"skills/pstack/{rel}": text for rel, text in PSTACK.items()}
    tree |= {f"skills/factory/{rel}": text for rel, text in FACTORY.items()}
    for rel, text in tree.items():
        if rel == skip:
            continue
        path = checkout / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("edited\n" if rel == change else ""), encoding="utf-8")


def checkout_files(checkout: Path) -> dict[str, str]:
    files = {f"{checkout}/skills/pstack/{rel}": _sha(text) for rel, text in PSTACK.items()}
    files |= {f"{checkout}/skills/factory/{rel}": _sha(text) for rel, text in FACTORY.items()}
    return files


def install(home: Path, *, skip: str | None = None, change: str | None = None) -> None:
    for rel in INSTALLED:
        if rel == skip:
            continue
        path = home / ".claude" / "skills" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_content(rel) + ("edited\n" if rel == change else ""), encoding="utf-8")


def detail_of(db: Path, job_id: str) -> dict:
    with sqlite3.connect(db) as connection:
        raw = connection.execute("SELECT detail FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    return json.loads(raw)


class Device:
    """A waiter for claude-bot on the box or the fake-ssh pc, with a spawn that
    notes how many ssh calls had happened when the run started."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, remote: bool):
        self.ssh = Ssh(tmp_path)
        for name, value in self.ssh.env().items():
            monkeypatch.setenv(name, value)
        self.home = tmp_path / ("device-home" if remote else "box-home")
        self.home.mkdir()
        monkeypatch.setenv("FAKE_SSH_HOME", str(self.home))
        if not remote:
            monkeypatch.setenv("HOME", str(self.home))
        # The least loaded device takes the job: the pc (3 free) or the box (5).
        machines = remote_machines(tmp_path, box_enabled=not remote, box_slots=5)
        self.checkout = tmp_path / "pc-factory"
        text = machines.read_text(encoding="utf-8")
        line = "    factory_root: /srv/factory\n"
        machines.write_text(text.replace(line, f"    factory_root: {self.checkout}\n"), encoding="utf-8")
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


def test_the_pinned_set_is_every_installed_skill_file_and_the_whole_checkout(lock_root: Path) -> None:
    pinned = skill_preflight.pinned_files(lock_root)
    assert pinned.commit == "a" * 40
    assert set(pinned.installed) == INSTALLED
    assert pinned.installed["arena/SKILL.md"] == _sha("arena\n")
    assert set(pinned.checkout) == {f"skills/pstack/{rel}" for rel in PSTACK} | {
        f"skills/factory/{rel}" for rel in FACTORY
    }


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
                                   transport=SshTransport(), home=home, root=root)
    assert report.error is None
    target = root / rel
    if defect == "missing":
        target.unlink()
    else:
        target.write_text("changed adapter\n", encoding="utf-8")

    report = skill_preflight.check(bot="claude-bot", placement=None, device=None,
                                   transport=SshTransport(), home=home, root=root)

    assert report.error == f"preflight-{defect}: {root.resolve()}/{rel}"


def test_a_remote_missing_a_referenced_skill_starts_no_harness_and_fails_with_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home, skip="arena/SKILL.md")
    deploy(device.checkout)

    job_id, started = device.run_one()

    assert started is None
    assert device.spawn.calls == []
    assert state(device.db, job_id) == "failed"
    assert any("preflight-missing: ~/.claude/skills/arena/SKILL.md" in line for line in device.log)
    entry = detail_of(device.db, job_id)["skill_preflight"]
    assert entry["error"] == "preflight-missing: ~/.claude/skills/arena/SKILL.md"
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
        "files": {f"~/.claude/skills/{rel}": _sha(_content(rel)) for rel in sorted(INSTALLED)}
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
     ("hash", "skills/pstack/agents/poteto-agent.md")],
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
        skill_preflight.check(bot="claude-bot", placement=placement, device=pc,
                              transport=NoSsh(), home=tmp_path, root=lock_root)


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
    for name in ("interrogate", "arena", "factory-builder"):
        (skills / name).symlink_to(source / ".claude" / "skills" / name)
    deploy(lock_root)
    expected = skill_preflight.wanted(".claude/skills", str(lock_root), skill_preflight.pinned_files(lock_root))
    found = skill_preflight.local_hashes(tmp_path / "home", sorted(expected))
    assert skill_preflight.compare(expected, found) is None
