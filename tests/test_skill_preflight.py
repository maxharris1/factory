"""The waiter's skill preflight on the placed device (issue #179, SF-7).

Before a harness starts, every file the pinned skills install must be on the
placed device with the hash skills/skills.lock records. The box is read in
place; a remote device answers one ssh call (tests/fake_ssh.py here, which
runs the real script against a fake home).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from lib import skill_preflight
from lib.device_pool import DevicePool
from lib.machines_yml import load_machines_yml
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


def test_the_pinned_set_is_every_installed_skill_file(lock_root: Path) -> None:
    commit, files = skill_preflight.pinned_files(lock_root)
    assert commit == "a" * 40
    assert set(files) == INSTALLED
    assert files["arena/SKILL.md"] == _sha("arena\n")


def test_the_pinned_set_covers_the_real_lock() -> None:
    _commit, files = skill_preflight.pinned_files(REPO)
    for rel in ("interrogate/SKILL.md", "arena/SKILL.md", "figure-it-out/SKILL.md",
                "show-me-your-work/SKILL.md", "architect/SKILL.md", "factory-builder/SKILL.md"):
        assert rel in files
    assert not any(rel.startswith("harness/") for rel in files)


def test_a_remote_missing_a_referenced_skill_starts_no_harness_and_fails_with_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home, skip="arena/SKILL.md")

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

    job_id, started = device.run_one()

    assert started == job_id
    assert state(device.db, job_id) == "running"
    detail = detail_of(device.db, job_id)
    assert detail["risk"]  # the dispatcher's detail is kept
    entry = detail["skill_preflight"]
    assert entry == {
        "device": "pc",
        "root": "~/.claude/skills",
        "commit": "a" * 40,
        "files": {rel: _sha(_content(rel)) for rel in sorted(INSTALLED)},
    }


def test_remote_preflight_is_one_ssh_round_trip_before_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)

    device.run_one()

    # The waiter's own ssh: exactly the preflight, before it spawns
    # `orch job run` (which does the detached launch, #128/#129).
    assert device.ssh_calls_at_spawn == [1]
    [call] = device.ssh.calls()
    assert call["alias"] == "fakepc-claude-bot" and call["user"] == "claude-bot"
    assert "orch-skills" in call["script"]


def test_an_unreachable_remote_fails_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)
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

    job_id, started = device.run_one()

    assert started == job_id
    assert device.ssh.calls() == []
    entry = detail_of(device.db, job_id)["skill_preflight"]
    assert entry["device"] == "box"
    assert set(entry["files"]) == INSTALLED


def test_a_symlinked_skill_dir_counts(tmp_path: Path, lock_root: Path) -> None:
    source = tmp_path / "source"
    install(source)
    skills = tmp_path / "home" / ".claude" / "skills"
    skills.mkdir(parents=True)
    for name in ("interrogate", "arena", "factory-builder"):
        (skills / name).symlink_to(source / ".claude" / "skills" / name)
    _commit, pinned = skill_preflight.pinned_files(lock_root)
    found = skill_preflight.local_hashes(tmp_path / "home", ".claude/skills", sorted(pinned))
    assert skill_preflight.compare(".claude/skills", pinned, found) is None
