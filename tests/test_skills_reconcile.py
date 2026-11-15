"""Installed skills and start gates follow state, not a one-time event (#297, round 3).

A box pass reruns install-skills for its own home when the installed copy
misses the pin, and an install that fails, or a home it cannot read, is a
failed pass retried later, never `up-to-date`. It never installs into
another account's home (#345). Each bot's waiter brings its own home up to
the pin while it has no box run, and again before a box start. A remote start is refused while the box
checkout itself is behind its fetched origin/main.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import device_sync, pstack_pin, self_deploy, skill_preflight, waiter
from lib.device_pool import DevicePool
from lib.grok_delivery import WebhookResult
from lib.heal import Webhook, run_service
from lib.machines_yml import Device as MachineDevice, MachinesConfig
from lib.queue import SQLiteJobQueue
from tests.test_skill_preflight import (
    Device, _skill_preflight, commit_checkout, deploy, detail_of, install, lock_root, state,
    sync_rows,
)
from tests.test_waiter import FakeChild, FakeContext, FakeSpawn

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
    assert state(device.db, job_id) == "failed"
    error = (f"preflight-lagging: box claude-bot {lock_root.resolve()} at {old[:12]}, "
             f"origin/main at {new[:12]}")
    assert detail_of(device.db, job_id)["skill_preflight"]["error"] == error
    assert sync_rows(device.db) == [("box", "claude-bot", "lagging", error)]
    assert any("failed for T6 to requeue after a backoff" in line for line in device.log)


class Installs:
    def __init__(self) -> None:
        self.homes: list[str] = []
        self.codes: list[int] = []

    def __call__(self, argv, env, path):
        code = run_service(argv, env, path)
        self.homes.append(env["HOME"])
        self.codes.append(code)
        return code


class Posts:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url, authorization, body, timeout):
        self.calls.append(body)
        return WebhookResult(200, {})


def box_pass(tmp_path: Path, box: Path, home: Path, installs: Installs, posts: Posts,
             queue: SQLiteJobQueue) -> self_deploy.Outcome:
    temp = tmp_path / "elsewhere"
    temp.mkdir(exist_ok=True)
    environ = {"PATH": os.environ["PATH"], "HOME": str(home), "ORCH_DB": str(queue.path),
               "TMPDIR": str(temp)}
    return self_deploy.deploy(box, tmp_path / "state", device="box", environ=environ,
                              webhook=Webhook(), now=1000, owner=lambda _: True,
                              install=installs, post=posts)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def bot_waiter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box: Path, home: Path,
               queue: SQLiteJobQueue, *, clock: Clock | None = None) -> tuple[waiter.Waiter, list[str]]:
    monkeypatch.setattr(waiter, "FACTORY_CHECKOUT", box)
    config = MachinesConfig(devices=(MachineDevice(
        name="box", local=True, slot_max=2,
        harnesses={"claude": "claude-bot", "codex": "codex-bot", "cursor": "cursor-bot"}),))
    log: list[str] = []
    run = waiter.Waiter(
        bot="codex-bot", queue=queue, pool=DevicePool(queue, config),
        harness=waiter.HarnessCommand(bot="codex-bot", command=("fake-harness",)),
        context=FakeContext(), state_dir=tmp_path / "waiter-state", db=queue.path,
        machines=tmp_path / "machines.yml", spawn=FakeSpawn(), log=log.append,
        environ={"PATH": "/usr/bin:/bin", "HOME": str(home)}, home=home,
        clock=clock if clock is not None else Clock(),
    )
    return run, log


def rows(queue: SQLiteJobQueue) -> list[tuple[str, str, str, str]]:
    with queue.connect() as connection:
        return [(r.device, r.bot, r.state, r.detail) for r in device_sync.read(connection)]


def count_installs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    homes: list[Path] = []
    real = skill_preflight.install_into_home

    def counted(checkout: Path, home: Path) -> list[str]:
        homes.append(home)
        return real(checkout, home)

    monkeypatch.setattr(skill_preflight, "install_into_home", counted)
    return homes


needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")


@needs_permissions
def test_the_box_pass_never_installs_into_another_account_and_that_bots_waiter_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box_behind_new_pin: tuple[Path, Path]
) -> None:
    upstream, box = box_behind_new_pin
    home, other = tmp_path / "box-home", tmp_path / "codex-bot"
    for each in (home, other):
        assert pstack_pin.install_skills(box, each, "local") == []
    other.chmod(0o555)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    installs = Installs()

    try:
        outcome = box_pass(tmp_path, box, home, installs, Posts(), queue)
        again = box_pass(tmp_path, box, home, installs, Posts(), queue)
    finally:
        other.chmod(0o755)

    assert (outcome.kind, again.kind) == (self_deploy.DEPLOYED, self_deploy.UP_TO_DATE)
    assert installs.homes == [str(home)] and installs.codes == [0]
    log = (tmp_path / "state" / self_deploy.INSTALL_LOG).read_text(encoding="utf-8")
    assert "Permission denied" not in log
    assert pstack_pin.installed_errors(box, home) == []
    assert pstack_pin.installed_errors(box, other) != []

    run, _ = bot_waiter(tmp_path, monkeypatch, box, other, queue)
    assert run.step() is None

    assert (other / ".agents/skills/review-retry-skill/SKILL.md").read_text() == (
        upstream / NEW_SKILL).read_text()
    assert pstack_pin.installed_errors(box, other) == []
    assert [(d, b, s) for d, b, s, _ in rows(queue)] == [("box", "codex-bot", "synced")]
    report = skill_preflight.check(bot="codex-bot", placement=None, device=None, transport=None,
                                   home=other, checkout=str(box), root=box)
    assert report.error is None


@needs_permissions
def test_an_unreadable_home_fails_the_box_pass_instead_of_reporting_up_to_date(
    tmp_path: Path, box_behind_new_pin: tuple[Path, Path]
) -> None:
    upstream, box = box_behind_new_pin
    git(box, "pull", "-q", "--ff-only", "origin", "main")
    home = tmp_path / "box-home"
    home.mkdir()
    home.chmod(0)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    installs, posts = Installs(), Posts()

    try:
        first = box_pass(tmp_path, box, home, installs, posts, queue)
        second = box_pass(tmp_path, box, home, installs, posts, queue)
    finally:
        home.chmod(0o755)

    assert (first.kind, second.kind) == (self_deploy.INSTALL_FAILED, self_deploy.INSTALL_FAILED)
    assert first.detail == "orch install-skills exited 1"
    assert installs.codes == [1, 1]
    heal_log = (tmp_path / "state" / self_deploy.LOG_NAME).read_text(encoding="utf-8")
    assert heal_log.count("escalate deploy @ box (orch install-skills exited 1)") == 1
    assert "install-failed: orch install-skills exited 1" in heal_log
    assert "up to date" not in heal_log

    fixed = box_pass(tmp_path, box, home, installs, posts, queue)

    assert fixed.kind == self_deploy.UP_TO_DATE
    assert installs.codes == [1, 1, 0]
    assert pstack_pin.installed_errors(box, home) == []


@needs_permissions
def test_a_waiter_marks_its_unreadable_home_refused_and_retries_after_a_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box_behind_new_pin: tuple[Path, Path]
) -> None:
    upstream, box = box_behind_new_pin
    git(box, "pull", "-q", "--ff-only", "origin", "main")
    home = tmp_path / "codex-bot"
    home.mkdir()
    home.chmod(0)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    clock = Clock()
    run, log = bot_waiter(tmp_path, monkeypatch, box, home, queue, clock=clock)
    installs = count_installs(monkeypatch)

    try:
        run.step()
        [(device, bot, state, detail)] = rows(queue)
        clock.now = waiter.HOME_RETRY_SECONDS - 1
        run.step()
    finally:
        home.chmod(0o755)

    assert (device, bot, state) == ("box", "codex-bot", device_sync.REFUSED)
    assert detail.startswith("install-skills: ")
    assert installs == [home]
    with queue.connect() as connection:
        assert device_sync.blocked(connection).blocks("box", "codex")

    clock.now = waiter.HOME_RETRY_SECONDS
    run.step()

    assert installs == [home, home]
    assert [(d, b, s) for d, b, s, _ in rows(queue)] == [("box", "codex-bot", "synced")]
    assert pstack_pin.installed_errors(box, home) == []
    assert any("retry in 60s" in line for line in log)


def test_a_waiter_leaves_its_home_alone_while_it_runs_a_box_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box_behind_new_pin: tuple[Path, Path]
) -> None:
    upstream, box = box_behind_new_pin
    git(box, "pull", "-q", "--ff-only", "origin", "main")
    home = tmp_path / "codex-bot"
    assert pstack_pin.install_skills(box, home, "local") == []
    (home / ".agents/skills/review-retry-skill").unlink()
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    run, _ = bot_waiter(tmp_path, monkeypatch, box, home, queue)
    installs = count_installs(monkeypatch)
    child = FakeChild(4242)
    run._children["job-1"] = child

    run.step()

    assert installs == []
    assert not (home / ".agents/skills/review-retry-skill/SKILL.md").exists()
    assert rows(queue) == []

    child.returncode = 0
    run.step()

    assert installs == [home]
    assert (home / ".agents/skills/review-retry-skill/SKILL.md").is_file()


def test_a_waiter_does_not_clear_a_lagging_mark_on_its_own_box_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box_behind_new_pin: tuple[Path, Path]
) -> None:
    upstream, box = box_behind_new_pin
    home = tmp_path / "codex-bot"
    assert pstack_pin.install_skills(box, home, "local") == []
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    with queue.connect() as connection:
        device_sync.upsert(connection, device="box", bot="codex-bot", harness="codex",
                           checkout=str(box), target=None, sha=None,
                           state=device_sync.LAGGING, detail="preflight-lagging: box", now=1000)
        connection.commit()
    run, _ = bot_waiter(tmp_path, monkeypatch, box, home, queue)

    run.step()

    assert rows(queue) == [("box", "codex-bot", "lagging", "preflight-lagging: box")]


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


def account_checkout_config(tmp_path: Path) -> MachinesConfig:
    return MachinesConfig(devices=(MachineDevice(
        name="box", local=True, slot_max=3, harnesses={"codex": "codex-bot"},
        factory_root=str(tmp_path / "{bot}" / "factory")),))


def test_an_idle_waiter_brings_its_own_account_checkout_to_the_deployed_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box_behind_new_pin: tuple[Path, Path]
) -> None:
    deployed, old = box_behind_new_pin
    account = tmp_path / "codex-bot" / "factory"
    account.parent.mkdir()
    old.rename(account)
    home = tmp_path / "account-home"
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    run, log = bot_waiter(tmp_path, monkeypatch, deployed, home, queue)
    run.pool.config = account_checkout_config(tmp_path)
    target = git(deployed, "rev-parse", "HEAD")

    run.step()

    assert git(account, "rev-parse", "HEAD") == target, log
    assert git(account, "symbolic-ref", "--short", "HEAD") == "main"
    assert rows(queue) == [("box", "codex-bot", device_sync.SYNCED, "")]
    assert (home / ".agents/skills/review-retry-skill/SKILL.md").read_text() == (
        deployed / NEW_SKILL).read_text()
    report = skill_preflight.check(bot="codex-bot", placement=None,
                                   device=run.pool.config.devices[0], transport=None,
                                   home=home, checkout=str(account), root=deployed)
    assert report.error is None


def test_an_account_checkout_that_cannot_reach_the_deployed_commit_is_refused_not_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box_behind_new_pin: tuple[Path, Path]
) -> None:
    deployed, old = box_behind_new_pin
    account = tmp_path / "codex-bot" / "factory"
    account.parent.mkdir()
    old.rename(account)
    (account / "local-only").write_text("not on origin\n", encoding="utf-8")
    git(account, "add", "local-only")
    git(account, "commit", "-qm", "local commit")
    before = git(account, "rev-parse", "HEAD")
    home = tmp_path / "account-home"
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    run, _ = bot_waiter(tmp_path, monkeypatch, deployed, home, queue)
    run.pool.config = account_checkout_config(tmp_path)
    installs = count_installs(monkeypatch)

    run.step()

    assert git(account, "rev-parse", "HEAD") == before
    assert installs == []
    assert rows(queue) == [("box", "codex-bot", device_sync.REFUSED,
                            "checkout: HEAD has commits not on origin/main")]


@pytest.mark.parametrize("placed", [True, False], ids=["placed", "unplaced"])
def test_a_restarted_waiter_leaves_a_home_alone_while_an_earlier_box_run_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box_behind_new_pin: tuple[Path, Path],
    placed: bool,
) -> None:
    from tests.test_waiter import enqueue

    _, box = box_behind_new_pin
    git(box, "pull", "-q", "--ff-only", "origin", "main")
    home = tmp_path / "codex-home"
    assert pstack_pin.install_skills(box, home, "local") == []
    missing = home / ".agents/skills/review-retry-skill"
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000)
    earlier, _ = bot_waiter(tmp_path, monkeypatch, box, home, queue)
    job_id = enqueue(queue, "local-job", recipient="codex")
    assert earlier.pool.claim("codex") is not None
    if not placed:
        with queue.connect() as connection:
            connection.execute("DELETE FROM device_slots")
            connection.commit()
    missing.unlink()
    restarted, _ = bot_waiter(tmp_path, monkeypatch, box, home, queue)
    installs = count_installs(monkeypatch)

    restarted.step()

    assert restarted.running() == set()
    assert installs == []
    assert not missing.exists()
    with queue.connect() as connection:
        connection.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (job_id,))
        connection.execute("DELETE FROM device_slots")
        connection.commit()

    restarted.step()

    assert installs == [home]
    assert missing.exists()
