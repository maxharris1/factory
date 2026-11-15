"""Installed skills and start gates follow state, not a one-time event (#297, round 3).

A box pass reruns install-skills for every bot home whose installed copy
misses the pin, so each account gets a newly pinned skill without a job
start and a failed install is retried once the cause is gone, but never
while the box runs a job. Each bot's waiter also brings its own home up to
the pin before a box start. A remote start is refused while the box
checkout itself is behind its fetched origin/main.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import pstack_pin, self_deploy, skill_preflight, waiter
from lib.heal import Webhook, run_service
from lib.queue import SQLiteJobQueue
from tests.test_skill_preflight import (
    Device, _skill_preflight, commit_checkout, deploy, detail_of, install, lock_root, state,
    sync_rows,
)

REPO = Path(__file__).resolve().parents[1]
NEW_SKILL = "skills/pstack/skills/review-retry-skill/SKILL.md"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def box_behind_new_pin(tmp_path: Path) -> tuple[Path, Path]:
    bare, upstream, box = tmp_path / "origin.git", tmp_path / "upstream", tmp_path / "box"
    subprocess.run(["git", "clone", "-q", "--bare", "--no-local", str(REPO), str(bare)], check=True)
    git(bare, "update-ref", "refs/heads/main", git(REPO, "rev-parse", "HEAD"))
    for dest in (upstream, box):
        subprocess.run(["git", "clone", "-q", "--branch", "main", str(bare), str(dest)], check=True)
    skill = upstream / NEW_SKILL
    skill.parent.mkdir()
    skill.write_text("---\nname: review-retry-skill\ndescription: test\n---\nNew skill.\n",
                     encoding="utf-8")
    pin = pstack_pin.load_pin(upstream)
    pstack_pin.bump_pin(upstream, pin.commit, pin.version)
    git(upstream, "add", "skills")
    git(upstream, "commit", "-qm", "add pinned skill")
    git(upstream, "push", "-q", "origin", "main")
    return upstream, box


def test_a_failed_install_skills_is_retried_once_the_checkout_is_current(
    tmp_path: Path, box_behind_new_pin: tuple[Path, Path]
) -> None:
    upstream, box = box_behind_new_pin
    home = tmp_path / "home"
    assert pstack_pin.install_skills(box, home, "local") == []
    conflict = home / ".agents/skills/review-retry-skill"
    conflict.mkdir(parents=True)
    (conflict / "SKILL.md").write_text("user-owned skill\n", encoding="utf-8")
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    codes: list[int] = []

    def installer(argv, env, path):
        code = run_service(argv, env, path)
        codes.append(code)
        return code

    temp = tmp_path / "elsewhere"
    temp.mkdir()
    environ = {"PATH": os.environ["PATH"], "HOME": str(home), "ORCH_DB": str(queue.path),
               "TMPDIR": str(temp)}
    kw = dict(device="box", environ=environ, webhook=Webhook(), now=1000,
              owner=lambda _: True, install=installer)

    first = self_deploy.deploy(box, tmp_path / "state", **kw)
    assert first.kind == self_deploy.DEPLOYED
    assert git(box, "rev-parse", "HEAD") == git(upstream, "rev-parse", "HEAD")
    assert codes == [1]
    log = (tmp_path / "state" / self_deploy.INSTALL_LOG).read_text(encoding="utf-8")
    assert "review-retry-skill" in log and "temp dir" not in log

    shutil.rmtree(conflict)
    second = self_deploy.deploy(box, tmp_path / "state", **kw)

    assert second.kind == self_deploy.UP_TO_DATE
    assert codes == [1, 0]
    assert pstack_pin.installed_errors(box, home) == []
    for bot in ("claude-bot", "codex-bot", "cursor-bot"):
        report = skill_preflight.check(bot=bot, placement=None, device=None, transport=None,
                                       home=home, checkout=str(box), root=box)
        assert report.error is None, bot

    self_deploy.deploy(box, tmp_path / "state", **kw)
    assert codes == [1, 0]


def test_each_bot_waiter_installs_a_newly_pinned_skill_into_its_own_home_before_a_box_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box_behind_new_pin: tuple[Path, Path], _skill_preflight
) -> None:
    upstream, box = box_behind_new_pin
    git(box, "pull", "-q", "--ff-only", "origin", "main")
    monkeypatch.setattr(skill_preflight, "ROOT", box)
    monkeypatch.setattr(waiter, "FACTORY_CHECKOUT", box)
    device = Device(tmp_path, monkeypatch, remote=False)
    assert pstack_pin.install_skills(box, device.home, "local") == []
    (device.home / ".claude/skills/review-retry-skill").unlink()

    job_id, started = device.run_one()

    assert started == job_id, device.log
    assert (device.home / ".claude/skills/review-retry-skill/SKILL.md").read_text() == (
        upstream / NEW_SKILL).read_text()
    assert "error" not in detail_of(device.db, job_id)["skill_preflight"]


def test_a_remote_start_is_refused_while_the_box_lags_its_origin_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path, _skill_preflight
) -> None:
    device = Device(tmp_path, monkeypatch, remote=True)
    install(device.home)
    deploy(lock_root)
    old = commit_checkout(lock_root)
    subprocess.run(["git", "clone", "-q", str(lock_root), str(device.checkout)], check=True)
    (lock_root / "later").write_text("merged\n", encoding="utf-8")
    git(lock_root, "add", "later")
    git(lock_root, "commit", "-qm", "merged")
    new = git(lock_root, "rev-parse", "HEAD")
    git(lock_root, "update-ref", "refs/remotes/origin/main", new)
    git(lock_root, "reset", "-q", "--hard", old)
    git(device.checkout, "fetch", "-q", "origin", new)
    git(device.checkout, "update-ref", "refs/remotes/origin/main", new)

    job_id, started = device.run_one()

    assert started is None
    assert device.spawn.calls == []
    assert state(device.db, job_id) == "queued"
    error = (f"preflight-lagging: box claude-bot {lock_root.resolve()} at {old[:12]}, "
             f"origin/main at {new[:12]}")
    assert detail_of(device.db, job_id)["skill_preflight"]["error"] == error
    assert sync_rows(device.db) == [("pc", "claude-bot", "lagging", error)]
    assert any("requeued, no round charged" in line for line in device.log)


def test_one_box_deploy_pass_installs_a_newly_pinned_skill_into_every_bot_home(
    tmp_path: Path, box_behind_new_pin: tuple[Path, Path]
) -> None:
    upstream, box = box_behind_new_pin
    home = tmp_path / "home"
    bot_homes = {bot: tmp_path / bot for bot in ("claude-bot", "codex-bot", "cursor-bot")}
    for each in (home, *bot_homes.values()):
        assert pstack_pin.install_skills(box, each, "local") == []
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    codes: list[int] = []

    def installer(argv, env, path):
        code = run_service(argv, env, path)
        codes.append(code)
        return code

    temp = tmp_path / "elsewhere"
    temp.mkdir()
    environ = {"PATH": os.environ["PATH"], "HOME": str(home), "ORCH_DB": str(queue.path),
               "TMPDIR": str(temp)}
    outcome = self_deploy.deploy(box, tmp_path / "state", device="box", environ=environ,
                                 webhook=Webhook(), now=1000, owner=lambda _: True,
                                 install=installer, homes=(home, *bot_homes.values()))

    assert outcome.kind == self_deploy.DEPLOYED
    assert git(box, "rev-parse", "HEAD") == git(upstream, "rev-parse", "HEAD")
    assert codes == [0, 0, 0, 0]
    for bot, each in bot_homes.items():
        installed = each / skill_preflight.skills_dir(bot) / "review-retry-skill/SKILL.md"
        assert installed.read_text() == (upstream / NEW_SKILL).read_text(), bot
        report = skill_preflight.check(bot=bot, placement=None, device=None, transport=None,
                                       home=each, checkout=str(box), root=box)
        assert report.error is None, bot


def test_the_box_bot_homes_are_the_running_home_and_each_harness_user_that_exists(
    tmp_path: Path,
) -> None:
    import pwd

    from lib.machines_yml import Device as MachineDevice, MachinesConfig

    me = pwd.getpwuid(os.getuid())
    machines = MachinesConfig(devices=(MachineDevice(
        name="box", local=True, slot_max=2,
        harnesses={"claude": me.pw_name, "codex": "no-such-bot-user-297"}),))

    homes = self_deploy.bot_homes(machines, "box", {"HOME": str(tmp_path)})

    assert homes == (tmp_path, Path(me.pw_dir))
    assert self_deploy.bot_homes(None, "box", {"HOME": str(tmp_path)}) == (tmp_path,)


def test_an_install_skills_retry_waits_while_the_box_runs_a_job(
    tmp_path: Path, box_behind_new_pin: tuple[Path, Path]
) -> None:
    from lib.device_pool import slots_in_use
    from tests.test_self_deploy import box_queue, enqueue

    upstream, box = box_behind_new_pin
    home = tmp_path / "home"
    assert pstack_pin.install_skills(box, home, "cloud") == []
    skill = upstream / "skills/pstack/skills/poteto-mode/SKILL.md"
    skill.write_bytes(skill.read_bytes() + b"\nMerged content.\n")
    pin = pstack_pin.load_pin(upstream)
    pstack_pin.bump_pin(upstream, pin.commit, pin.version)
    git(upstream, "add", "skills")
    git(upstream, "commit", "-qm", "change an existing pinned skill")
    git(upstream, "push", "-q", "origin", "main")
    installed = home / ".agents/skills/poteto-mode/SKILL.md"
    conflict = home / ".agents/skills/review-retry-skill"
    conflict.mkdir(parents=True)
    (conflict / "SKILL.md").write_text("user-owned skill\n", encoding="utf-8")
    queue, pool = box_queue(tmp_path)
    calls: list[tuple[dict, int]] = []

    def installer(argv, env, path):
        with queue.connect() as connection:
            busy = slots_in_use(connection, pool.config)
        code = run_service(argv, env, path)
        calls.append((busy, code))
        return code

    temp = tmp_path / "elsewhere"
    temp.mkdir()
    environ = {"PATH": os.environ["PATH"], "HOME": str(home), "ORCH_DB": str(queue.path),
               "TMPDIR": str(temp)}
    kw = dict(device="box", environ=environ, webhook=Webhook(), now=1000,
              owner=lambda _: True, install=installer)
    first = self_deploy.deploy(box, tmp_path / "state", **kw)
    assert first.kind == self_deploy.DEPLOYED
    assert calls == [({}, 1)]
    shutil.rmtree(conflict)
    enqueue(queue, "1")
    assert pool.claim("codex") is not None
    before = installed.read_bytes()

    busy = self_deploy.deploy(box, tmp_path / "state", **kw)

    assert busy.kind == self_deploy.BUSY
    assert installed.read_bytes() == before
    assert not (home / ".agents/skills/review-retry-skill").exists()
    assert calls == [({}, 1)]
    with queue.connect() as connection:
        assert connection.execute("SELECT state FROM jobs").fetchone()[0] == "running"
        connection.execute("UPDATE jobs SET state = 'done'")
        connection.execute("DELETE FROM device_slots")
        connection.commit()

    idle = self_deploy.deploy(box, tmp_path / "state", **kw)

    assert idle.kind == self_deploy.UP_TO_DATE
    assert calls == [({}, 1), ({}, 0)]
    assert b"Merged content." in installed.read_bytes()
    assert pstack_pin.installed_errors(box, home) == []
