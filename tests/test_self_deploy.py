"""Self-deploy (#219): real git against a temporary bare origin, no network.

The box checkout is a clone of origin; upstream commits come from a second
clone. Install runs and webhook posts are fakes that record their calls.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lib import device_sync, heal, pstack_pin, self_deploy
from lib.device_pool import DevicePool, slots_in_use
from lib.machines_yml import Device, MachinesConfig
from lib.queue import JobRequest, SQLiteJobQueue
from lib.grok_delivery import WebhookResult
from lib.heal import Webhook, run_service
from tests.test_skills_reconcile import box_behind_new_pin

NOW = 1_800_000_000.0
SECRETS = {
    "FACTORY_WEBHOOK_URL": "https://hooks.example.test/secret-path",
    "FACTORY_WEBHOOK_AUTH": "Bearer secret-auth-value",
    "GH_TOKEN": "ghp_secrettoken",
}
WEBHOOK = Webhook()
MACHINES = "".join(f"line {n}\n" for n in range(1, 11))
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd.parent), **GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, text=True,
                          capture_output=True).stdout.strip()


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Repo:
    def __init__(self, tmp_path: Path):
        self.origin = tmp_path / "origin.git"
        self.upstream = tmp_path / "upstream"
        self.box = tmp_path / "box"
        self.state = tmp_path / "state"
        subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main",
                        str(self.origin)], check=True)
        subprocess.run(["git", "clone", "--quiet", str(self.origin), str(self.upstream)],
                       check=True, capture_output=True)
        git(self.upstream, "checkout", "--quiet", "-B", "main")
        write(self.upstream, "machines.yml", MACHINES)
        write(self.upstream, "orch.yml", "linear: REPLACE\ntrusted: []\n")
        write(self.upstream, "deploy/install.sh", "#!/bin/sh\necho install\n")
        write(self.upstream, "lib/code.py", "VERSION = 1\n")
        self.commit("seed")
        subprocess.run(["git", "clone", "--quiet", str(self.origin), str(self.box)],
                       check=True, capture_output=True)

    def commit(self, message: str) -> str:
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "--quiet", "-m", message)
        git(self.upstream, "push", "--quiet", "origin", "main")
        return git(self.upstream, "rev-parse", "HEAD")

    def head(self) -> str:
        return git(self.box, "rev-parse", "HEAD")

    def snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.box)): p.read_bytes()
                for p in sorted(self.box.rglob("*"))
                if p.is_file() and ".git" not in p.relative_to(self.box).parts}


class Runs:
    def __init__(self, code: int = 0):
        self.code = code
        self.calls: list[tuple[list[str], dict, Path]] = []

    def __call__(self, argv, env, log_path):
        self.calls.append((list(argv), dict(env), log_path))
        return self.code


class Posts:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, authorization, body, timeout):
        self.calls.append(json.loads(body))
        return WebhookResult(200, {})


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def run(repo: Repo, *, now: float = NOW, runs: Runs | None = None, posts: Posts | None = None,
        owner=lambda _path: True, db: Path | None = None):
    runs = runs if runs is not None else Runs()
    posts = posts if posts is not None else Posts()
    environ = {"PATH": os.environ["PATH"], "HOME": str(repo.box.parent), **GIT_ENV, **SECRETS}
    if db is not None:
        environ["ORCH_DB"] = str(db)
    outcome = self_deploy.deploy(repo.box, repo.state, device="box", environ=environ,
                                 webhook=WEBHOOK, now=now, install=runs, post=posts,
                                 owner=owner, clock=lambda: 0.0)
    return outcome, runs, posts


def log(repo: Repo) -> list[str]:
    path = repo.state / "self-heal.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# --- up to date ----------------------------------------------------------------------------


def test_up_to_date_is_a_no_op_logged_once(repo: Repo) -> None:
    before = repo.head()
    first, runs, posts = run(repo)
    second, _, _ = run(repo, runs=runs, posts=posts)
    assert (first.kind, second.kind) == ("up-to-date", "up-to-date")
    assert repo.head() == before
    assert log(repo) == [f"2027-01-15T08:00:00Z self-deploy: up to date at {before[:12]}"]
    assert runs.calls == [] and posts.calls == []


# --- fast-forward --------------------------------------------------------------------------


def test_a_clean_checkout_fast_forwards_to_origin_main(repo: Repo) -> None:
    old = repo.head()
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    new = repo.commit("code")
    outcome, runs, posts = run(repo)
    assert outcome.kind == "deployed"
    assert repo.head() == new
    assert (repo.box / "lib/code.py").read_text() == "VERSION = 2\n"
    assert log(repo) == [f"2027-01-15T08:00:00Z self-deploy: deployed "
                         f"{old[:12]}..{new[:12]} kept=- merged=- 0.0s"]
    assert runs.calls == [] and posts.calls == []


def test_a_local_edit_upstream_did_not_touch_is_kept(repo: Repo) -> None:
    write(repo.box, "orch.yml", "linear: box-project-id\ntrusted: [max]\n")
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    new = repo.commit("code")
    outcome, _, _ = run(repo)
    assert outcome.kind == "deployed"
    assert repo.head() == new
    assert (repo.box / "orch.yml").read_text() == "linear: box-project-id\ntrusted: [max]\n"
    assert "kept=orch.yml merged=-" in log(repo)[-1]


def test_a_local_edit_is_reapplied_over_an_upstream_change(repo: Repo) -> None:
    write(repo.box, "machines.yml", MACHINES.replace("line 1\n", "line 1 slots: 4\n"))
    write(repo.upstream, "machines.yml", MACHINES.replace("line 9\n", "line 9 upstream\n"))
    new = repo.commit("machines")
    outcome, _, _ = run(repo)
    assert outcome.kind == "deployed"
    assert repo.head() == new
    assert (repo.box / "machines.yml").read_text() == (
        MACHINES.replace("line 1\n", "line 1 slots: 4\n").replace("line 9\n", "line 9 upstream\n"))
    diff = git(repo.box, "diff", "HEAD", "--", "machines.yml")
    assert "+line 1 slots: 4" in diff and "upstream" not in diff
    assert "kept=machines.yml merged=machines.yml" in log(repo)[-1]
    backups = list((repo.state / "self-deploy").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "files/machines.yml").read_text().startswith("line 1 slots: 4\n")


def test_overlay_files_are_untouched(repo: Repo) -> None:
    write(repo.box, "orch.local.yml", "linear:\n  project_id: box\n")
    write(repo.box, "machines.local.yml", "devices: []\n")
    write(repo.upstream, ".gitignore", "orch.local.yml\nmachines.local.yml\n")
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    new = repo.commit("code")
    assert run(repo)[0].kind == "deployed"
    assert repo.head() == new
    assert (repo.box / "orch.local.yml").read_text() == "linear:\n  project_id: box\n"
    assert (repo.box / "machines.local.yml").read_text() == "devices: []\n"


# --- install steps -------------------------------------------------------------------------


def test_a_deploy_change_reruns_install_update_without_secrets(repo: Repo) -> None:
    write(repo.upstream, "deploy/install.sh", "#!/bin/sh\necho install v2\n")
    repo.commit("deploy")
    outcome, runs, posts = run(repo)
    assert outcome.kind == "deployed"
    ((argv, env, log_path),) = runs.calls
    assert argv == [str(repo.box / "deploy/install.sh"), "--update"]
    assert "FACTORY_WEBHOOK_URL" not in env and "FACTORY_WEBHOOK_AUTH" not in env
    assert log_path == repo.state / "self-deploy-install.log"
    assert log(repo)[-1].endswith("self-deploy: install.sh --update exit=0 0.0s")
    assert posts.calls == []


def test_a_requirements_change_reruns_install(repo: Repo) -> None:
    write(repo.upstream, "requirements.txt", "pyyaml\n")
    repo.commit("deps")
    assert len(run(repo)[1].calls) == 1


def test_a_failing_install_is_logged_and_escalated_and_the_checkout_stays_new(
    repo: Repo,
) -> None:
    write(repo.upstream, "deploy/self-heal.sh", "#!/bin/sh\n")
    new = repo.commit("deploy")
    outcome, _, posts = run(repo, runs=Runs(code=3))
    assert outcome.kind == "deployed"
    assert repo.head() == new
    assert [p["summary"] for p in posts.calls] == [
        "deploy @ box: deploy/install.sh --update exited 3"]


def test_a_skills_change_runs_install_skills_from_the_new_checkout(repo: Repo) -> None:
    write(repo.upstream, "skills/pstack/skills/new-skill/SKILL.md", "new\n")
    repo.commit("skill")
    outcome, runs, posts = run(repo)
    assert outcome.kind == "deployed"
    ((argv, env, log_path),) = runs.calls
    assert argv == [sys.executable, str(repo.box / "bin/orch"), "install-skills"]
    assert "FACTORY_WEBHOOK_URL" not in env and "FACTORY_WEBHOOK_AUTH" not in env
    assert log(repo)[-1].endswith("self-deploy: install-skills exit=0 0.0s")
    assert posts.calls == []


def test_a_failing_install_skills_is_escalated_once_and_the_checkout_stays_new(
    repo: Repo,
) -> None:
    write(repo.upstream, "skills/skills.lock", "commit: b\n")
    new = repo.commit("pin")
    outcome, _, posts = run(repo, runs=Runs(code=3))
    assert outcome.kind == "deployed"
    assert repo.head() == new
    assert [p["summary"] for p in posts.calls] == ["deploy @ box: orch install-skills exited 3"]


# --- the box's own jobs (#297) -------------------------------------------------------------


def box_queue(tmp_path: Path) -> tuple[SQLiteJobQueue, DevicePool]:
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: NOW)
    config = MachinesConfig(devices=(Device(name="box", local=True, slot_max=2,
                                            harnesses={"codex": "codex-bot"}),))
    return queue, DevicePool(queue, config)


def enqueue(queue: SQLiteJobQueue, ticket: str) -> None:
    queue.enqueue(JobRequest(repo="o/r", ticket_id=ticket, action="review", recipient="codex",
                             source_comment_id=f"c{ticket}", timeout_seconds=60),
                  dedupe_key=ticket)


def box_rows(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [(row.device, row.state) for row in device_sync.read(connection)]


def test_a_box_with_a_job_stays_on_its_old_head_until_the_job_is_gone(
    repo: Repo, tmp_path: Path
) -> None:
    queue, pool = box_queue(tmp_path)
    enqueue(queue, "1")
    assert pool.claim("codex") is not None
    old = repo.head()
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    new = repo.commit("code")

    outcome, _, posts = run(repo, db=queue.path)

    assert outcome.kind == "busy"
    assert outcome.detail == "1 jobs on box, retry next pass"
    assert repo.head() == old
    assert posts.calls == []
    assert box_rows(queue) == []
    with queue.connect() as connection:
        connection.execute("DELETE FROM device_slots")
        connection.execute("UPDATE jobs SET state = 'done'")
        connection.commit()

    assert run(repo, db=queue.path)[0].kind == "deployed"
    assert repo.head() == new


def test_no_job_is_placed_on_the_box_while_its_checkout_moves(repo: Repo, tmp_path: Path) -> None:
    queue, pool = box_queue(tmp_path)
    with queue.connect() as connection:
        device_sync.upsert(connection, device="box", bot="codex-bot", harness="codex",
                           checkout=str(repo.box), target=None, sha=None,
                           state=device_sync.LAGGING, detail="preflight-lagging", now=NOW)
        connection.commit()
    enqueue(queue, "1")
    write(repo.upstream, "deploy/install.sh", "#!/bin/sh\necho install v2\n")
    repo.commit("deploy")
    during: list[tuple[list[tuple[str, str]], object]] = []

    def install(argv, env, log_path):
        during.append((box_rows(queue), pool.claim("codex")))
        return 0

    outcome, _, _ = run(repo, runs=install, db=queue.path)

    assert outcome.kind == "deployed"
    assert during == [([("box", "deploying"), ("box", "lagging")], None)]
    assert box_rows(queue) == []
    assert pool.claim("codex") is not None


# --- conflicts and refusals ----------------------------------------------------------------


def test_a_conflicting_pull_leaves_the_checkout_unchanged_and_escalates_once(
    repo: Repo,
) -> None:
    write(repo.box, "orch.yml", "linear: box-project-id\ntrusted: []\n")
    old = repo.head()
    before = repo.snapshot()
    write(repo.upstream, "orch.yml", "linear: upstream-change\ntrusted: []\n")
    repo.commit("orch")
    posts = Posts()
    first, _, _ = run(repo, posts=posts)
    second, _, _ = run(repo, posts=posts, now=NOW + 120)
    assert (first.kind, second.kind) == ("conflict", "conflict")
    assert repo.head() == old
    assert repo.snapshot() == before
    assert git(repo.box, "status", "--porcelain") == "M orch.yml"
    assert len(posts.calls) == 1
    assert posts.calls[0]["summary"] == (
        "deploy @ box: local edits conflict with upstream: orch.yml")
    assert log(repo) == [
        "2027-01-15T08:00:00Z self-deploy: conflict: local edits conflict with upstream: orch.yml",
        "2027-01-15T08:00:00Z self-deploy: escalate deploy @ box "
        "(local edits conflict with upstream: orch.yml) escalated",
    ]
    assert not (repo.state / "self-deploy").exists()


def test_after_a_conflict_is_resolved_a_new_one_escalates_again(repo: Repo) -> None:
    write(repo.box, "orch.yml", "linear: box\ntrusted: []\n")
    write(repo.upstream, "orch.yml", "linear: up\ntrusted: []\n")
    repo.commit("orch")
    posts = Posts()
    run(repo, posts=posts)
    git(repo.box, "checkout", "--", "orch.yml")
    assert run(repo, posts=posts)[0].kind == "deployed"
    write(repo.box, "orch.yml", "linear: box2\ntrusted: []\n")
    write(repo.upstream, "orch.yml", "linear: up2\ntrusted: []\n")
    repo.commit("orch 2")
    assert run(repo, posts=posts)[0].kind == "conflict"
    assert len(posts.calls) == 2


@pytest.mark.parametrize("setup, reason", [
    (lambda r: write(r.box, "lib/code.py", "VERSION = 99\n"),
     "local edits outside the keep list: lib/code.py"),
    (lambda r: git(r.box, "checkout", "--quiet", "-b", "hotfix"), "not on main (on hotfix)"),
    (lambda r: git(r.box, "checkout", "--quiet", "--detach"), "not on main (detached HEAD)"),
    (lambda r: (write(r.box, "orch.yml", "x\n"), git(r.box, "add", "orch.yml")),
     "staged changes"),
])
def test_a_checkout_in_an_unexpected_shape_is_left_alone(repo: Repo, setup, reason) -> None:
    setup(repo)
    write(repo.upstream, "lib/other.py", "X = 1\n")
    repo.commit("other")
    old = repo.head()
    before = repo.snapshot()
    outcome, _, posts = run(repo)
    assert (outcome.kind, outcome.detail) == ("refused", reason)
    assert repo.head() == old and repo.snapshot() == before
    assert [p["summary"] for p in posts.calls] == [f"deploy @ box: {reason}"]


def test_local_commits_are_left_alone(repo: Repo) -> None:
    write(repo.box, "lib/local.py", "X = 1\n")
    git(repo.box, "add", "-A")
    git(repo.box, "commit", "--quiet", "-m", "local")
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    repo.commit("code")
    old = repo.head()
    outcome, _, posts = run(repo)
    assert outcome.kind == "refused"
    assert outcome.detail.startswith("local commits:")
    assert repo.head() == old
    assert len(posts.calls) == 1


def test_an_account_that_does_not_own_the_checkout_does_nothing(repo: Repo) -> None:
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    repo.commit("code")
    old = repo.head()
    outcome, _, _ = run(repo, owner=lambda _path: False)
    assert outcome.kind == "skipped"
    assert repo.head() == old
    assert not repo.state.exists()


def test_owns_checks_the_uid_and_write_access(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert self_deploy.owns(tmp_path) is True
    assert self_deploy.owns(tmp_path / "missing") is False


# --- fetch failure ---------------------------------------------------------------------------


def test_a_failed_fetch_logs_only_its_exit_code_and_escalates_after_30m(repo: Repo) -> None:
    git(repo.box, "remote", "set-url", "origin",
        "https://user:secret-in-url@127.0.0.1:1/nowhere.git")
    posts = Posts()
    first, _, _ = run(repo, posts=posts)
    run(repo, posts=posts, now=NOW + 600)
    assert first.kind == "fetch-failed"
    assert posts.calls == []
    run(repo, posts=posts, now=NOW + 31 * 60)
    run(repo, posts=posts, now=NOW + 33 * 60)
    assert len(posts.calls) == 1
    text = "\n".join(log(repo))
    assert "secret" not in text and "127.0.0.1" not in text and "https" not in text
    assert log(repo)[0].startswith("2027-01-15T08:00:00Z self-deploy: fetch-failed: "
                                   "fetch failed exit=")


def test_git_never_gets_the_webhook_secrets_or_a_github_token() -> None:
    env = self_deploy.git_env({"PATH": "/bin", **SECRETS, "GITHUB_TOKEN": "x"}, WEBHOOK)
    assert env == {"PATH": "/bin", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


# --- rollback ---------------------------------------------------------------------------------


def test_a_failed_apply_rolls_back_to_the_old_head_and_local_edits(
        repo: Repo, monkeypatch) -> None:
    local = MACHINES.replace("line 1\n", "line 1 local\n")
    write(repo.box, "machines.yml", local)
    write(repo.box, "lib/untracked.py", "untracked local file\n")
    write(repo.upstream, "machines.yml", MACHINES.replace("line 9\n", "line 9 upstream\n"))
    write(repo.upstream, "lib/new.py", "upstream adds this\n")
    repo.commit("adds new.py")
    real = self_deploy.Git.out

    def merge_then_fail(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        if args[0] == "merge":
            raise self_deploy.GitError(args, 1)
        return result

    monkeypatch.setattr(self_deploy.Git, "out", merge_then_fail)
    old = repo.head()
    before = repo.snapshot()
    outcome, _, posts = run(repo)
    assert outcome.kind == "rolled-back"
    assert "restored the old HEAD and local edits" in outcome.detail
    assert repo.head() == old
    assert repo.snapshot() == before
    assert len(posts.calls) == 1


# --- filesystem shapes a rollback could not restore are refused before any change ----------


def tree(root: Path) -> dict[str, tuple[str, bytes | str]]:
    """Every path under root but .git, with its type and contents or link target."""
    out: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] == ".git":
            continue
        if path.is_symlink():
            out[str(rel)] = ("link", os.readlink(path))
        elif path.is_dir():
            out[str(rel)] = ("dir", b"")
        else:
            out[str(rel)] = ("file", path.read_bytes())
    return out


def kept_file_is_a_directory(r: Repo) -> None:
    (r.box / "orch.yml").unlink()
    write(r.box, "orch.yml/local-settings.yml", "precious local data\n")


def kept_file_is_a_symlink(r: Repo) -> None:
    write(r.box.parent, "elsewhere/machines.yml", "live machines\n")
    (r.box / "machines.yml").unlink()
    (r.box / "machines.yml").symlink_to(r.box.parent / "elsewhere/machines.yml")


def kept_parent_is_a_symlink(r: Repo) -> None:
    write(r.box.parent, "deploy-live/install.sh", "#!/bin/sh\necho live\n")
    (r.box / "deploy/install.sh").unlink()
    (r.box / "deploy").rmdir()
    (r.box / "deploy").symlink_to(r.box.parent / "deploy-live")


@pytest.mark.parametrize("setup, reason", [
    (kept_file_is_a_directory, "unsupported file type at orch.yml"),
    (kept_file_is_a_symlink, "unsupported file type at machines.yml"),
    (kept_parent_is_a_symlink, "unsupported file type at deploy"),
])
@pytest.mark.parametrize("collides", [False, True])
def test_a_kept_path_that_is_not_a_plain_file_is_refused_untouched(
        repo: Repo, setup, reason, collides) -> None:
    setup(repo)
    if collides:
        write(repo.box, "lib/new.py", "untracked collision\n")
        write(repo.upstream, "lib/new.py", "upstream file\n")
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    repo.commit("upstream")
    old = repo.head()
    before = tree(repo.box)
    outside = tree(repo.box.parent / "elsewhere") if (repo.box.parent / "elsewhere").exists() else {}
    outcome, _, posts = run(repo)
    assert (outcome.kind, outcome.detail) == ("refused", reason)
    assert repo.head() == old
    assert tree(repo.box) == before
    if outside:
        assert tree(repo.box.parent / "elsewhere") == outside
    assert len(posts.calls) == 1


def untracked_file(r: Repo) -> str:
    write(r.box, "lib/new.py", "untracked local file\n")
    return "lib/new.py"


def ignored_file(r: Repo) -> str:
    write(r.box, ".git/info/exclude", "*.local\n")
    write(r.box, "notes.local", "ignored but precious\n")
    return "notes.local"


def untracked_directory(r: Repo) -> str:
    write(r.box, "tools/scratch.txt", "local scratch\n")
    return "tools"


def untracked_symlink(r: Repo) -> str:
    (r.box / "lib/new.py").symlink_to(r.box.parent / "nowhere")
    return "lib/new.py"


@pytest.mark.parametrize("setup, adds", [
    (untracked_file, "lib/new.py"),
    (ignored_file, "notes.local"),
    (untracked_directory, "tools"),
    (untracked_symlink, "lib/new.py"),
])
def test_a_local_path_in_the_way_of_an_upstream_addition_is_refused_untouched(
        repo: Repo, setup, adds) -> None:
    in_the_way = setup(repo)
    write(repo.upstream, adds, "upstream adds this\n")
    repo.commit("adds")
    old = repo.head()
    before = tree(repo.box)
    outcome, _, posts = run(repo)
    assert (outcome.kind, outcome.detail) == (
        "refused", f"local path in the way of upstream: {in_the_way}")
    assert repo.head() == old
    assert tree(repo.box) == before
    assert len(posts.calls) == 1


def ignored_in_directory(r: Repo) -> str:
    write(r.box, ".git/info/exclude", "*.local\n")
    write(r.box, "lib/data.local", "precious local data\n")
    return "lib/data.local"


def untracked_in_directory(r: Repo) -> str:
    write(r.box, "lib/scratch.py", "local scratch\n")
    return "lib/scratch.py"


def directory_becomes_file(r: Repo) -> None:
    (r.upstream / "lib/code.py").unlink()
    (r.upstream / "lib").rmdir()
    write(r.upstream, "lib", "upstream replaces directory with file\n")


def directory_becomes_symlink(r: Repo) -> None:
    (r.upstream / "lib/code.py").unlink()
    (r.upstream / "lib").rmdir()
    (r.upstream / "lib").symlink_to("elsewhere")


@pytest.mark.parametrize("local", [ignored_in_directory, untracked_in_directory])
@pytest.mark.parametrize("change", [directory_becomes_file, directory_becomes_symlink])
def test_a_directory_upstream_replaces_is_refused_while_it_holds_local_files(
        repo: Repo, local, change) -> None:
    in_the_way = local(repo)
    change(repo)
    repo.commit("replace a directory")
    old = repo.head()
    before = tree(repo.box)
    outcome, _, posts = run(repo)
    assert (outcome.kind, outcome.detail) == (
        "refused", f"local path in the way of upstream: {in_the_way}")
    assert repo.head() == old
    assert tree(repo.box) == before
    assert len(posts.calls) == 1


def test_a_directory_upstream_replaces_holding_only_tracked_files_deploys(repo: Repo) -> None:
    directory_becomes_file(repo)
    new = repo.commit("replace a directory")
    outcome, _, _ = run(repo)
    assert outcome.kind == "deployed"
    assert repo.head() == new


def kept_file_becomes_symlink(r: Repo) -> None:
    (r.upstream / "orch.yml").unlink()
    (r.upstream / "orch.yml").symlink_to("../shared-orch.yml")


def kept_parent_becomes_symlink(r: Repo) -> None:
    (r.upstream / "deploy/install.sh").unlink()
    (r.upstream / "deploy").rmdir()
    (r.upstream / "deploy").symlink_to("../shared-deploy")


@pytest.mark.parametrize("change, kept, at", [
    (kept_file_becomes_symlink, "orch.yml", "orch.yml"),
    (kept_parent_becomes_symlink, "deploy/install.sh", "deploy"),
])
@pytest.mark.parametrize("edit", ["mode", "content"])
def test_an_edited_kept_file_upstream_turns_into_a_non_file_is_refused_untouched(
        repo: Repo, change, kept, at, edit) -> None:
    shared = repo.box.parent / "shared-orch.yml"
    shared.write_text("precious shared local data\n")
    write(repo.box.parent, "shared-deploy/install.sh", "#!/bin/sh\necho shared\n")
    outside = {**tree(repo.box.parent / "shared-deploy"), "orch": shared.read_bytes()}
    if edit == "mode":
        (repo.box / kept).chmod(0o755)
    else:
        with open(repo.box / kept, "a", encoding="utf-8") as out:
            out.write("# local\n")
    change(repo)
    repo.commit("kept path becomes a symlink")
    old = repo.head()
    before = tree(repo.box)
    outcome, _, posts = run(repo)
    assert (outcome.kind, outcome.detail) == (
        "refused", f"upstream changes the file type at {at}, which has local edits")
    assert repo.head() == old
    assert tree(repo.box) == before
    assert {**tree(repo.box.parent / "shared-deploy"), "orch": shared.read_bytes()} == outside
    assert len(posts.calls) == 1


# --- local edits git diff does not report are refused before any change -------------------


def assume_unchanged(r: Repo, name: str) -> None:
    git(r.box, "update-index", "--assume-unchanged", name)


def skip_worktree(r: Repo, name: str) -> None:
    git(r.box, "update-index", "--skip-worktree", name)


def stale_fsmonitor(r: Repo, name: str) -> None:
    hook = r.box.parent / "fsmonitor-hook"
    hook.write_text("#!/bin/sh\nprintf 'token\\0'\n")
    hook.chmod(0o755)
    git(r.box, "config", "core.fsmonitor", str(hook))
    git(r.box, "update-index", "--fsmonitor")
    git(r.box, "status", "--porcelain")


@pytest.mark.parametrize("hide", [assume_unchanged, skip_worktree, stale_fsmonitor])
@pytest.mark.parametrize("name", ["machines.yml", "lib/code.py"])
def test_a_local_edit_git_diff_does_not_report_is_refused_untouched(
        repo: Repo, hide, name) -> None:
    original = (repo.box / name).read_text(encoding="utf-8")
    hide(repo, name)
    write(repo.box, name, original + "LOCAL\n")
    os.utime(repo.box / name, (NOW - 86400, NOW - 86400))
    assert git(repo.box, "diff", "--name-only", "HEAD") == ""
    write(repo.upstream, name, "UPSTREAM\n" + original)
    repo.commit("upstream change to the hidden file")
    old = repo.head()
    before = tree(repo.box)
    outcome, _, posts = run(repo)
    assert (outcome.kind, outcome.detail) == (
        "refused", f"local edits git diff does not report: {name}")
    assert repo.head() == old
    assert tree(repo.box) == before
    assert len(posts.calls) == 1


def test_an_index_flag_on_an_unedited_file_is_refused(repo: Repo) -> None:
    assume_unchanged(repo, "lib/code.py")
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    repo.commit("upstream")
    old = repo.head()
    outcome, _, _ = run(repo)
    assert (outcome.kind, outcome.detail) == (
        "refused", "local edits git diff does not report: lib/code.py")
    assert repo.head() == old


# --- orch heal deploy -------------------------------------------------------------------------


def test_orch_heal_deploy_runs_a_pass(repo: Repo, monkeypatch) -> None:
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    new = repo.commit("code")
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)
    code = heal.main(["deploy", "--checkout", str(repo.box), "--state", str(repo.state),
                      "--device", "box"], clock=lambda: NOW)
    assert code == 0
    assert repo.head() == new


def test_orch_heal_deploy_dry_run_moves_nothing(repo: Repo) -> None:
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    repo.commit("code")
    old = repo.head()
    code = heal.main(["deploy", "--checkout", str(repo.box), "--state", str(repo.state),
                      "--device", "box", "--dry-run"], clock=lambda: NOW)
    assert code == 0
    assert repo.head() == old
    assert "would deploy" in log(repo)[-1]


def test_an_up_to_date_pass_keeps_each_waiters_own_box_row(repo: Repo, tmp_path: Path) -> None:
    queue, _ = box_queue(tmp_path)
    with queue.connect() as connection:
        for bot, state in (("", "deploying"), ("claude-bot", "refused"),
                           ("codex-bot", "lagging"), ("cursor-bot", "synced")):
            device_sync.upsert(connection, device="box", bot=bot, harness=bot[:-4], checkout="c",
                               target=None, sha=None, state=state, detail="", now=NOW)
        connection.commit()

    assert run(repo, db=queue.path)[0].kind == "up-to-date"

    with queue.connect() as connection:
        assert [(r.bot, r.state) for r in device_sync.read(connection)] == [
            ("claude-bot", "refused"), ("cursor-bot", "synced")]


@pytest.mark.parametrize("second_pass", ["same state", "other state", "other checkout"])
def test_an_overlapping_deploy_pass_leaves_the_active_pass_and_its_placement_guard_alone(
    tmp_path: Path, box_behind_new_pin: tuple[Path, Path], second_pass: str
) -> None:
    upstream, box = box_behind_new_pin
    home = tmp_path / "home"
    assert pstack_pin.install_skills(box, home, "cloud") == []
    git(box, "pull", "-q", "--ff-only", "origin", "main")
    queue, pool = box_queue(tmp_path)
    temp = tmp_path / "elsewhere"
    temp.mkdir()
    environ = {"PATH": os.environ["PATH"], "HOME": str(home), "ORCH_DB": str(queue.path),
               "TMPDIR": str(temp)}
    kw = dict(device="box", environ=environ, webhook=Webhook(), now=NOW,
              owner=lambda _: True, machines=pool.config, homes=(home,))
    ready, resume = threading.Event(), threading.Event()
    installs: list[tuple[dict, int]] = []
    first: list[str] = []

    def slow_installer(argv, env, log_path):
        ready.set()
        assert resume.wait(30)
        with queue.connect() as connection:
            busy = slots_in_use(connection, pool.config)
        installs.append((busy, run_service(argv, env, log_path)))
        return installs[-1][1]

    thread = threading.Thread(target=lambda: first.append(
        self_deploy.deploy(box, tmp_path / "state", install=slow_installer, **kw).kind))
    thread.start()
    try:
        assert ready.wait(30)
        assert box_rows(queue) == [("box", "deploying")]

        if second_pass == "other checkout":
            other = tmp_path / "other-box"
            git(tmp_path, "clone", "-q", "--branch", "main", str(tmp_path / "origin.git"),
                str(other))
            second = self_deploy.deploy(other, tmp_path / "other-state", **kw)
        else:
            state = "state" if second_pass == "same state" else "other-state"
            second = self_deploy.deploy(box, tmp_path / state, **kw)
        enqueue(queue, "1")

        assert pool.claim("codex") is None
        assert box_rows(queue) == [("box", "deploying")]
        assert (second.kind, second.detail) == ("skipped", "another deploy pass is running")
    finally:
        resume.set()
        thread.join(30)
    assert not thread.is_alive()
    assert first == ["up-to-date"]
    assert [(busy.get("box", 0), code) for busy, code in installs] == [(0, 0)]
    assert box_rows(queue) == []
    assert pool.claim("codex") is not None


def test_a_deploy_pass_finding_the_lock_held_skips_and_writes_nothing(repo: Repo) -> None:
    run(repo)
    state_file = repo.state / "self-deploy.state.json"
    before = state_file.read_bytes(), log(repo)
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    repo.commit("code")
    old = repo.head()

    with open(repo.state / "self-deploy.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        outcome, runs, posts = run(repo)

    assert (outcome.kind, outcome.detail) == ("skipped", "another deploy pass is running")
    assert (state_file.read_bytes(), log(repo)) == before
    assert repo.head() == old
    assert runs.calls == [] and posts.calls == []
