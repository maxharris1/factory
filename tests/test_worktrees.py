import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from lib import worktrees
from lib.worktrees import WorktreeError

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def origin(tmp_path: Path) -> dict:
    """A file:// remote for acme/widgets with main and a PR-only commit."""
    remotes = tmp_path / "remotes"
    bare = remotes / "acme" / "widgets.git"
    bare.parent.mkdir(parents=True)
    git("init", "--bare", "-q", "-b", "main", str(bare))
    git("-C", str(bare), "config", "uploadpack.allowReachableSHA1InWant", "true")
    seed = tmp_path / "seed"
    git("clone", "-q", str(bare), str(seed))
    (seed / "README").write_text("one\n", encoding="utf-8")
    (seed / ".gitignore").write_text("build/\n", encoding="utf-8")
    git("add", ".", cwd=seed)
    git("commit", "-q", "-m", "one", cwd=seed)
    git("push", "-q", "origin", "HEAD:main", cwd=seed)
    main = git("rev-parse", "HEAD", cwd=seed)
    (seed / "README").write_text("pr\n", encoding="utf-8")
    git("commit", "-q", "-am", "pr", cwd=seed)
    pr = git("rev-parse", "HEAD", cwd=seed)
    git("push", "-q", "origin", "HEAD:refs/pull/7/head", cwd=seed)
    return {
        "template": f"file://{remotes}/{{repo}}.git",
        "main": main,
        "pr": pr,
        "bare": bare,
    }


def job_id(n: int) -> str:
    return f"{n:032x}"


def prepare(root: Path, origin: dict, n: int, head: str | None = None, **kw):
    return worktrees.prepare(
        root,
        job_id(n),
        "acme/widgets",
        head,
        url_template=origin["template"],
        **kw,
    )


def listed(checkout) -> list[str]:
    text = git("-C", str(checkout.clone), "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1]
        for line in text.splitlines()
        if line.startswith("worktree ")
    ]


def test_jobs_share_one_clone_per_repo_and_check_out_the_head(
    tmp_path: Path, origin: dict
) -> None:
    root = tmp_path / "work"
    first = prepare(root, origin, 1, origin["main"])
    second = prepare(root, origin, 2, origin["pr"])

    assert first.clone == second.clone == root / "clones" / "acme" / "widgets.git"
    assert [p.name for p in (root / "clones" / "acme").iterdir() if p.is_dir()] == [
        "widgets.git"
    ]
    assert first.path == root / "jobs" / job_id(1)
    assert git("rev-parse", "HEAD", cwd=first.path) == origin["main"]
    # The PR head is not on any branch: it is fetched by SHA.
    assert git("rev-parse", "HEAD", cwd=second.path) == origin["pr"]
    assert (second.path / ".git").is_file()  # a worktree, not a full clone
    assert str(first.path) in listed(first)
    assert str(second.path) in listed(first)
    record = json.loads((root / "jobs" / f"{job_id(1)}.json").read_text())
    assert record["repo"] == "acme/widgets"
    assert record["job_id"] == job_id(1)
    assert record["pid"] == os.getpid()
    assert record["start"] == origin["main"]


def test_no_head_checks_out_the_remote_default_branch(
    tmp_path: Path, origin: dict
) -> None:
    checkout = prepare(tmp_path / "work", origin, 1)
    assert git("rev-parse", "HEAD", cwd=checkout.path) == origin["main"]
    # A branch the harness creates can track origin.
    git("checkout", "-q", "-b", "fix", "origin/main", cwd=checkout.path)


def test_unknown_head_is_refused_and_leaves_nothing_behind(
    tmp_path: Path, origin: dict
) -> None:
    root = tmp_path / "work"
    with pytest.raises(WorktreeError, match="cannot fetch"):
        prepare(root, origin, 1, "f" * 40)
    assert not (root / "jobs" / job_id(1)).exists()
    assert not (root / "jobs" / f"{job_id(1)}.json").exists()


def test_clean_worktree_is_removed_and_pruned(tmp_path: Path, origin: dict) -> None:
    root = tmp_path / "work"
    checkout = prepare(root, origin, 1, origin["main"])
    (checkout.path / "build").mkdir()
    (checkout.path / "build" / "out.o").write_text("ignored\n", encoding="utf-8")
    messages: list[str] = []

    assert worktrees.finish(checkout, messages.append) is True

    assert not checkout.path.exists()
    assert not (root / "jobs" / f"{job_id(1)}.json").exists()
    assert str(checkout.path) not in listed(checkout)
    assert not (checkout.clone / "worktrees" / job_id(1)).exists()
    assert messages == [f"removed job worktree {checkout.path}"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("untracked", "uncommitted changes"),
        ("modified", "uncommitted changes"),
        ("commit", "unpushed commits"),
    ],
)
def test_worktree_with_work_in_it_is_kept_and_logged(
    tmp_path: Path, origin: dict, change: str, reason: str
) -> None:
    root = tmp_path / "work"
    checkout = prepare(root, origin, 1, origin["main"])
    if change == "untracked":
        (checkout.path / "new.txt").write_text("x\n", encoding="utf-8")
    else:
        (checkout.path / "README").write_text("changed\n", encoding="utf-8")
        if change == "commit":
            git("commit", "-q", "-am", "local", cwd=checkout.path)
    messages: list[str] = []

    assert worktrees.finish(checkout, messages.append) is False

    assert checkout.path.is_dir()
    assert (root / "jobs" / f"{job_id(1)}.json").is_file()
    assert messages == [f"kept job worktree {checkout.path}: {reason}"]


def test_a_clean_review_of_a_pr_head_on_no_branch_is_removed(
    tmp_path: Path, origin: dict
) -> None:
    # The PR head is fetched by SHA and is on no remote-tracking ref; only
    # commits made after the checkout count as unpushed work.
    checkout = prepare(tmp_path / "work", origin, 1, origin["pr"])
    messages: list[str] = []
    assert worktrees.finish(checkout, messages.append) is True
    assert messages == [f"removed job worktree {checkout.path}"]


def test_a_commit_on_top_of_a_pr_head_is_kept(tmp_path: Path, origin: dict) -> None:
    checkout = prepare(tmp_path / "work", origin, 1, origin["pr"])
    (checkout.path / "README").write_text("fix\n", encoding="utf-8")
    git("commit", "-q", "-am", "fix", cwd=checkout.path)
    messages: list[str] = []
    assert worktrees.finish(checkout, messages.append) is False
    assert messages == [f"kept job worktree {checkout.path}: unpushed commits"]


def test_pushed_commits_do_not_keep_a_worktree(tmp_path: Path, origin: dict) -> None:
    checkout = prepare(tmp_path / "work", origin, 1, origin["main"])
    git("checkout", "-q", "-b", "fix", cwd=checkout.path)
    (checkout.path / "README").write_text("fixed\n", encoding="utf-8")
    git("commit", "-q", "-am", "fix", cwd=checkout.path)
    git("push", "-q", "origin", "fix", cwd=checkout.path)

    assert worktrees.finish(checkout, lambda _m: None) is True
    assert not checkout.path.exists()


@pytest.mark.parametrize(
    "repo,job",
    [
        ("acme", job_id(1)),
        ("acme/../etc", job_id(1)),
        ("../x/y", job_id(1)),
        ("acme/widgets", "../escape"),
        ("acme/widgets", ""),
    ],
)
def test_unsafe_repo_or_job_id_is_refused(
    tmp_path: Path, origin: dict, repo: str, job: str
) -> None:
    with pytest.raises(WorktreeError):
        worktrees.prepare(
            tmp_path / "work", job, repo, None, url_template=origin["template"]
        )
    assert not (tmp_path / "work" / "jobs").exists() or not any(
        (tmp_path / "work" / "jobs").iterdir()
    )


def _age(root: Path, n: int, hours: float) -> None:
    path = root / "jobs" / f"{job_id(n)}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["created_at"] = time.time() - hours * 3600
    path.write_text(json.dumps(record), encoding="utf-8")


def _orphan_pid(root: Path, n: int) -> None:
    path = root / "jobs" / f"{job_id(n)}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["pid"] = 2**22 + 12345  # above pid_max on default kernels
    record.pop("pid_start", None)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_gc_removes_only_old_unchanged_worktrees_not_in_use(
    tmp_path: Path, origin: dict
) -> None:
    root = tmp_path / "work"
    old_clean = prepare(root, origin, 1, origin["main"])
    old_dirty = prepare(root, origin, 2, origin["main"])
    young_clean = prepare(root, origin, 3, origin["main"])
    old_in_use = prepare(root, origin, 4, origin["main"])
    (old_dirty.path / "notes.txt").write_text("keep me\n", encoding="utf-8")
    for n in (1, 2, 4):
        _age(root, n, 30)
    for n in (1, 2, 3):
        _orphan_pid(root, n)
    # Job 4 still records this live process as its owner.

    dry: list[str] = []
    assert worktrees.gc(root, older_than_hours=24, log=dry.append, dry_run=True) == 0
    assert old_clean.path.exists()
    assert f"would remove job worktree {old_clean.path} (acme/widgets, 30h old)" in dry

    messages: list[str] = []
    assert worktrees.gc(root, older_than_hours=24, log=messages.append) == 0

    assert not old_clean.path.exists()
    assert str(old_clean.path) not in listed(old_clean)
    assert old_dirty.path.exists()
    assert young_clean.path.exists()
    assert old_in_use.path.exists()
    assert f"removed job worktree {old_clean.path} (acme/widgets, 30h old)" in messages
    assert f"kept job worktree {old_dirty.path}: uncommitted changes" in messages
    assert any(
        line.startswith(f"kept job worktree {old_in_use.path}: in use by pid ")
        for line in messages
    )
    assert not any(str(young_clean.path) in line for line in messages)


def test_gc_forgets_a_record_whose_worktree_was_deleted_by_hand(
    tmp_path: Path, origin: dict
) -> None:
    import shutil

    root = tmp_path / "work"
    checkout = prepare(root, origin, 1, origin["main"])
    _age(root, 1, 30)
    _orphan_pid(root, 1)
    shutil.rmtree(checkout.path)
    messages: list[str] = []

    assert worktrees.gc(root, older_than_hours=24, log=messages.append) == 0

    assert not (root / "jobs" / f"{job_id(1)}.json").exists()
    assert str(checkout.path) not in listed(checkout)
    assert messages == [f"removed job worktree {checkout.path} (acme/widgets, 30h old)"]


def test_gc_on_a_missing_root_is_a_no_op(tmp_path: Path) -> None:
    messages: list[str] = []
    assert worktrees.gc(tmp_path / "none", older_than_hours=1, log=messages.append) == 0
    assert messages == []


def test_work_root_prefers_explicit_then_env_then_home(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORCH_WORK_ROOT", raising=False)
    assert worktrees.work_root(None) == tmp_path / "factory-work"
    monkeypatch.setenv("ORCH_WORK_ROOT", str(tmp_path / "env"))
    assert worktrees.work_root(None) == tmp_path / "env"
    assert worktrees.work_root(tmp_path / "x") == tmp_path / "x"
