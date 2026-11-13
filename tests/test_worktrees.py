import json
import os
import subprocess
import time
from contextlib import contextmanager
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


def _forge_old_orphan_record(root: Path, n: int, *, clone: Path, start: str) -> Path:
    record = root / "jobs" / f"{job_id(n)}.json"
    record.write_text(
        json.dumps(
            {
                "job_id": job_id(n),
                "repo": "acme/widgets",
                "clone": str(clone),
                "created_at": time.time() - 48 * 3600,
                "pid": 2**22 + 12345,
                "start": start,
            }
        ),
        encoding="utf-8",
    )
    return record


def test_gc_never_removes_a_worktree_of_a_clone_the_record_names(
    tmp_path: Path, origin: dict
) -> None:
    # Regression (#52 F1.2): gc trusted the record's `clone` field, so a
    # forged record could make it force-remove a registered worktree of any
    # repo that sat at <root>/jobs/<id>.
    root = tmp_path / "work"
    (root / "jobs").mkdir(parents=True)
    foreign = tmp_path / "foreign"
    git("clone", "-q", str(origin["bare"]), str(foreign))
    path = root / "jobs" / job_id(1)
    git("-C", str(foreign), "worktree", "add", "-q", "--detach", str(path))
    _forge_old_orphan_record(root, 1, clone=foreign / ".git", start=origin["main"])
    messages: list[str] = []

    assert worktrees.gc(root, older_than_hours=24, log=messages.append) == 1

    assert path.is_dir()
    assert str(path) in git("-C", str(foreign), "worktree", "list")
    assert any("not a worktree of" in line for line in messages), messages


def test_gc_refuses_a_symlinked_jobs_directory(tmp_path: Path, origin: dict) -> None:
    real = tmp_path / "real"
    checkout = prepare(real, origin, 1, origin["main"])
    _age(real, 1, 30)
    _orphan_pid(real, 1)
    other = tmp_path / "other"
    other.mkdir()
    (other / "jobs").symlink_to(real / "jobs")
    messages: list[str] = []

    assert worktrees.gc(other, older_than_hours=24, log=messages.append) == 1

    assert checkout.path.is_dir()
    assert any("symlink" in line for line in messages), messages


def test_gc_rechecks_owner_and_work_under_the_clone_lock(
    tmp_path: Path, origin: dict, monkeypatch
) -> None:
    # Regression (#52 F1.3): gc checked the owner and keep_reason, then took
    # the clone lock and force-removed. A job re-run that reused and dirtied
    # the worktree in between lost its checkout.
    root = tmp_path / "work"
    checkout = prepare(root, origin, 1, origin["main"])
    _age(root, 1, 30)
    _orphan_pid(root, 1)
    real_lock = worktrees._clone_lock
    fired: list[bool] = []

    @contextmanager
    def reuse_first(clone: Path):
        if not fired:
            fired.append(True)
            # A new `orch job run` (this live process) takes the job over.
            prepare(root, origin, 1, origin["main"], pid=os.getpid())
            (checkout.path / "wip.txt").write_text("new work\n", encoding="utf-8")
        with real_lock(clone):
            yield

    monkeypatch.setattr(worktrees, "_clone_lock", reuse_first)
    messages: list[str] = []
    assert worktrees.gc(root, older_than_hours=24, log=messages.append) == 0

    assert fired
    assert (checkout.path / "wip.txt").read_text() == "new work\n"
    assert any(line.startswith(f"kept job worktree {checkout.path}") for line in messages)


def test_finish_keeps_a_worktree_another_live_run_has_taken_over(
    tmp_path: Path, origin: dict
) -> None:
    root = tmp_path / "work"
    checkout = prepare(root, origin, 1, origin["main"])
    other = subprocess.Popen(["sleep", "30"])
    try:
        prepare(root, origin, 1, origin["main"], pid=other.pid)
        messages: list[str] = []
        assert worktrees.finish(checkout, messages.append) is False
        assert checkout.path.is_dir()
        assert messages == [f"kept job worktree {checkout.path}: in use by pid {other.pid}"]
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize(
    "template",
    [
        "https://x:SENTINEL@example.invalid/{repo}.git",
        "https://SENTINEL@example.invalid/{repo}.git",
        "ssh://git:SENTINEL@example.invalid/{repo}.git",
    ],
)
def test_a_url_template_with_credentials_is_refused_before_git_sees_it(
    tmp_path: Path, monkeypatch, template: str
) -> None:
    # Regression (#52 F1.6): the formatted URL went into git argv and
    # remote.origin.url, so a token in the template leaked to ps and config.
    seen: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "0" * 40 + "\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(WorktreeError) as caught:
        worktrees.prepare(
            tmp_path / "work", job_id(1), "acme/widgets", None, url_template=template
        )
    assert "SENTINEL" not in str(caught.value)
    assert "credential" in str(caught.value)
    assert not any("SENTINEL" in arg for argv in seen for arg in argv)


@pytest.mark.parametrize(
    "template",
    [
        "https://github.com/{repo}.git",
        "ssh://git@github.com/{repo}.git",
        "git@github.com:{repo}.git",
        "file:///srv/git/{repo}.git",
    ],
)
def test_url_templates_without_secrets_are_accepted(template: str) -> None:
    worktrees.check_remote_url(template.format(repo="acme/widgets"))


# --- #105: a T6 retry takes over the kept worktree of the attempt it repeats ---------


def _kept_with_a_commit(root: Path, origin: dict, n: int):
    checkout = prepare(root, origin, n, origin["pr"])
    (checkout.path / "README").write_text("half done\n", encoding="utf-8")
    git("commit", "-q", "-am", "attempt 1", cwd=checkout.path)
    commit = git("rev-parse", "HEAD", cwd=checkout.path)
    assert worktrees.finish(checkout, lambda _m: None) is False
    _orphan_pid(root, n)
    return checkout, commit


def test_retry_adopts_the_kept_worktree_with_unpushed_commits(
    tmp_path: Path, origin: dict
) -> None:
    root = tmp_path / "work"
    first, commit = _kept_with_a_commit(root, origin, 1)
    messages: list[str] = []

    retry = prepare(root, origin, 2, origin["pr"], adopt=job_id(1), log=messages.append)

    assert retry.path == root / "jobs" / job_id(2)
    assert git("rev-parse", "HEAD", cwd=retry.path) == commit
    assert retry.start == origin["pr"]
    assert not first.path.exists()
    assert not first.record.exists()
    assert json.loads(retry.record.read_text())["start"] == origin["pr"]
    assert str(retry.path) in listed(retry)
    assert messages == [f"reusing kept worktree of job {job_id(1)} (unpushed commits)"]
    # The commit is still work, so the retry's own finish keeps it too.
    assert worktrees.finish(retry, lambda _m: None) is False


@pytest.mark.parametrize("case", ["clean", "live", "missing", "self"])
def test_retry_does_not_adopt_a_clean_or_live_worktree(
    tmp_path: Path, origin: dict, case: str
) -> None:
    root = tmp_path / "work"
    if case == "clean":
        first = prepare(root, origin, 1, origin["pr"])
        _orphan_pid(root, 1)
    elif case == "live":
        first, _commit = _kept_with_a_commit(root, origin, 1)
        record = json.loads(first.record.read_text())
        record["pid"] = os.getpid()
        record.pop("pid_start", None)
        first.record.write_text(json.dumps(record), encoding="utf-8")
    else:
        first = None
    adopt = job_id(2) if case == "self" else job_id(1)
    messages: list[str] = []

    retry = prepare(root, origin, 2, origin["pr"], adopt=adopt, log=messages.append)

    assert git("rev-parse", "HEAD", cwd=retry.path) == origin["pr"]
    assert messages == []
    if first is not None:
        assert first.path.is_dir() and first.record.is_file()
