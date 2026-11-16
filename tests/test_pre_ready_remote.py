"""#503: a remote run's ready runs the base factory.md's review.pre_ready on the box.

The waiter checks the remote marker's handoff (#192), then runs the check in
a throwaway worktree of its shared bare clone at the ready's head. The
clone's origin is a local repo, factory.md and the handoff come from
tests/fake_gh.py, and orch.yml is the factory's own with one test project.
"""

from __future__ import annotations

import base64
import copy
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from lib.queue import PRE_READY_FAILED, job_from_row
from tests.handoff_store import env_store, serve, serve_handoff
from tests.test_factory_md import VALID
from tests.test_waiter import FakeSpawn, enqueue
from tests.test_waiter_remote import ROOT, _finish_run, make_waiter, remote_machines

REPO = "owner/repo"
COMMENT = "4242"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A bare origin for REPO whose head commit holds ok.sh; its sha."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / "ok.sh").write_text("echo checked\n", encoding="utf-8")
    _git(source, "add", "ok.sh")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "head")
    bare = tmp_path / "origin" / f"{REPO}.git"
    bare.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    monkeypatch.setenv("ORCH_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("ORCH_GIT_URL_TEMPLATE", str(tmp_path / "origin" / "{repo}.git"))
    return _git(source, "rev-parse", "HEAD")


def _orch_yml(tmp_path: Path) -> Path:
    data = yaml.safe_load((ROOT / "orch.yml").read_text(encoding="utf-8"))
    data["projects"].append({"repo": REPO, "factory_ref": "main"})
    path = tmp_path / "orch.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _serve_factory_md(command: str) -> None:
    data = copy.deepcopy(VALID)
    data["review"]["pre_ready"] = {"command": command, "timeout_seconds": 60}
    text = f"---\n{yaml.safe_dump(data, sort_keys=False)}---\n"
    serve(
        env_store(),
        f"repos/{REPO}/contents/factory.md?ref=main",
        {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()},
    )


def _remote_ready(tmp_path: Path, head: str, command: str | None):
    machines = remote_machines(tmp_path, box_enabled=False)
    spawn = FakeSpawn()
    log: list[str] = []
    waiter, queue, db = make_waiter(tmp_path, machines, spawn, log=log)
    waiter.orch_yml = _orch_yml(tmp_path)
    job_id = enqueue(queue, "a", repo=REPO)
    waiter.step()
    with queue.connect() as connection:
        job = job_from_row(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    serve_handoff(env_store(), job, COMMENT, pr_head=head)
    if command is not None:
        _serve_factory_md(command)
    _finish_run(tmp_path, job_id, spawn, f"ORCH-HANDOFF: {COMMENT}\n")
    waiter.step()
    return db, job_id, log


def _row(db: Path, job_id: str) -> tuple:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


def _events(db: Path, job_id: str) -> list[tuple[str, str]]:
    """The job's events; a waiter-only db has no job_events until one is recorded."""
    with sqlite3.connect(db) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'job_events'").fetchone() is None:
            return []
        return connection.execute(
            "SELECT event, detail FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()


def _worktrees(tmp_path: Path) -> list[str]:
    clone = tmp_path / "work" / "clones" / f"{REPO}.git"
    return _git(clone, "worktree", "list", "--porcelain").splitlines()


def test_a_ready_whose_pre_ready_fails_fails_the_remote_job(tmp_path: Path, origin: str) -> None:
    db, job_id, log = _remote_ready(tmp_path, origin, "sh ok.sh && echo broken >&2 && exit 3")
    assert _row(db, job_id) == ("failed", None)
    assert _events(db, job_id) == [
        (PRE_READY_FAILED, f"handoff {COMMENT}: pre_ready failed: exit 3")
    ]
    assert any(f"pre_ready failed {COMMENT}: exit 3" in line for line in log)
    assert any("checked" in line and "broken" in line for line in log)
    assert [line for line in _worktrees(tmp_path) if line.startswith("worktree ")] == [
        f"worktree {tmp_path / 'work' / 'clones' / f'{REPO}.git'}"
    ]


def test_a_ready_whose_pre_ready_passes_completes_the_remote_job(
    tmp_path: Path, origin: str
) -> None:
    db, job_id, _log = _remote_ready(tmp_path, origin, "sh ok.sh")
    assert _row(db, job_id) == ("done", COMMENT)
    assert _events(db, job_id) == []


def test_the_check_runs_at_the_readys_head(tmp_path: Path, origin: str) -> None:
    db, job_id, _log = _remote_ready(tmp_path, origin, f'test "$(git rev-parse HEAD)" = {origin}')
    assert _row(db, job_id) == ("done", COMMENT)


def test_an_unreadable_factory_md_fails_the_job_and_records_nothing(
    tmp_path: Path, origin: str
) -> None:
    db, job_id, log = _remote_ready(tmp_path, origin, None)
    assert _row(db, job_id) == ("failed", None)
    assert any(f"could not check handoff {COMMENT}" in line for line in log)
    assert _events(db, job_id) == []
