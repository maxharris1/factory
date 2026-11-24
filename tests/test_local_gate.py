"""#1463: a ready on a PR into a base outside `ci.branches` counts only after
the base factory.md's `ci.local_gate` passes at the ready's head.

`orch job done --handoff` runs the gate in the job's worktree; the waiter
runs it for a remote run in a throwaway worktree of its shared clone. A pass
records `local-gate-passed` at the head. A failure records
`local-gate-failed`, counts toward the invalid-handoff cap and refuses the
ready. A PR into a `ci.branches` base, or a repo with no `ci`, runs no gate.
"""

from __future__ import annotations

import base64
import copy
import json
import sqlite3
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from lib.cli import main as cli_main
from lib.dispatcher import CIResult, TrackedTicket
from lib.factory_md import CheckCommand
from lib.pre_ready import planned
from lib.queue import (
    LOCAL_GATE_FAILED,
    LOCAL_GATE_PASSED,
    MAX_INVALID_HANDOFFS,
    PRE_READY_FAILED,
    PRE_READY_PASSED,
    SUBMITTED_INVALID,
    Job,
    JobRequest,
    SQLiteJobQueue,
    job_from_row,
)
from lib.job_token import PROOF_ENV
from tests.handoff_store import env_store, serve, serve_handoff
from tests.pool_helpers import CLAIM_HASH, CLAIM_PROOF
from tests.test_factory_md import VALID

REPO = "owner/repo"
COMMENT = "1101"
FEATURE = "feature/x"
GATE_EVENTS = (LOCAL_GATE_PASSED, LOCAL_GATE_FAILED)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _factory_text(
    gate: str | None, *, branches: list[str] | None = ["main"], pre_ready: str | None = None
) -> str:
    data = copy.deepcopy(VALID)
    if branches is not None:
        data["ci"] = {"branches": branches}
        if gate is not None:
            data["ci"]["local_gate"] = {"command": gate, "timeout_seconds": 30}
    if pre_ready is not None:
        data["review"]["pre_ready"] = {"command": pre_ready, "timeout_seconds": 30}
    return f"---\n{yaml.safe_dump(data, sort_keys=False)}---\n"


def _serve_factory_md(store: Path, text: str) -> None:
    serve(
        store,
        f"repos/{REPO}/contents/factory.md?ref=main",
        {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()},
    )


def _orch_yml(tmp_path: Path) -> Path:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\ntrusted_logins:\n  maxharris1:\n    - claude-bot\n"
        f"projects:\n  - repo: {REPO}\n    factory_ref: main\n"
        "grok:\n  webhook_url_env: FACTORY_WEBHOOK_URL\n  webhook_key_env: FACTORY_WEBHOOK_KEY\n",
        encoding="utf-8",
    )
    return path


def _events(db: Path, job_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'job_events'").fetchone() is None:
            return []
        return connection.execute(
            "SELECT event, detail FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()


def _gate_events(db: Path, job_id: str) -> list[tuple[str, str]]:
    return [item for item in _events(db, job_id) if item[0] in GATE_EVENTS]


def _state(db: Path, job_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


class Box:
    def __init__(self, tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PROOF_ENV, CLAIM_PROOF)
        self.store = store
        self.work = tmp_path / "work"
        self.work.mkdir()
        _git(self.work, "init", "-q")
        (self.work / "factory.md").write_text(_factory_text("touch head-gate-ran"), encoding="utf-8")
        _git(self.work, "add", ".")
        _git(self.work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "head")
        self.head = _git(self.work, "rev-parse", "HEAD")
        self.orch = _orch_yml(tmp_path)
        monkeypatch.setenv("ORCH_JOB_WORKTREE", str(self.work))
        self.db = tmp_path / "orch.db"
        queue = SQLiteJobQueue(self.db, clock=time.time)
        queue.enqueue(
            JobRequest(
                repo=REPO,
                ticket_id="42",
                action="build",
                recipient="claude",
                source_comment_id="src-build",
                timeout_seconds=3600,
                generation=1,
                attempt=1,
                detail=json.dumps(
                    {"pr_number": "7", "risk": "high", "round": "1/3", "gitBranchName": "factory/issue-42"}
                ),
            ),
            dedupe_key="build-claude",
        )
        job = queue.claim("claude", token_hash=CLAIM_HASH)
        assert job is not None
        self.job = job

    def ready(self, factory: str, base: str | None = FEATURE, comment: str = COMMENT) -> None:
        _serve_factory_md(self.store, factory)
        serve_handoff(self.store, self.job, comment, pr_head=self.head, pr_base=base)

    def done(self, comment: str = COMMENT) -> int:
        return cli_main(
            [
                "job", "done", "claude-bot", self.job.id, "--handoff", comment,
                "--db", str(self.db), "--orch", str(self.orch),
            ]
        )

    def state(self) -> tuple[str, str | None]:
        return _state(self.db, self.job.id)

    def gate_events(self) -> list[tuple[str, str]]:
        return _gate_events(self.db, self.job.id)


@pytest.fixture
def box(tmp_path: Path, _fake_gh_store: Path, monkeypatch: pytest.MonkeyPatch) -> Box:
    return Box(tmp_path, _fake_gh_store, monkeypatch)


def test_planned_runs_the_gate_only_on_a_base_outside_ci_branches() -> None:
    from tests.test_dispatcher import factory

    gate = CheckCommand("make gate", 60)
    plain = factory()
    with_ci = replace(plain, ci_branches=("main",), ci_local_gate=gate)

    assert [check.name for check in planned(with_ci, FEATURE)] == ["local_gate"]
    assert planned(with_ci, FEATURE)[0].spec == gate
    assert planned(with_ci, "main") == ()
    assert planned(plain, FEATURE) == ()
    assert planned(replace(plain, ci_branches=("main",)), FEATURE) == ()
    both = replace(with_ci, pre_ready=CheckCommand("make check", 30))
    assert [check.name for check in planned(both, FEATURE)] == ["pre_ready", "local_gate"]
    assert [check.name for check in planned(both, "main")] == ["pre_ready"]


def test_a_failing_gate_refuses_the_ready_with_its_output(box: Box, capsys) -> None:
    box.ready(_factory_text("echo first; echo the gate said no; exit 1"))

    assert box.done() == 1

    err = capsys.readouterr().err
    assert f"submitted-invalid {COMMENT}: local_gate failed: exit 1" in err
    assert "the gate said no" in err
    assert box.state() == ("running", None)
    assert box.gate_events() == [(LOCAL_GATE_FAILED, f"handoff {COMMENT}: local_gate failed: exit 1")]
    assert not (box.work / "head-gate-ran").exists()


def test_a_passing_gate_records_its_head_and_completes(box: Box) -> None:
    box.ready(_factory_text("touch base-gate-ran"))

    assert box.done() == 0

    assert box.state() == ("done", COMMENT)
    assert box.gate_events() == [(LOCAL_GATE_PASSED, f"handoff {COMMENT}: at {box.head}")]
    assert (box.work / "base-gate-ran").exists()
    assert not (box.work / "head-gate-ran").exists()


def test_exit_75_refuses_and_never_passes(box: Box, capsys) -> None:
    box.ready(_factory_text("exit 75"))

    assert box.done() == 1

    assert "local_gate failed: exit 75" in capsys.readouterr().err
    assert box.state() == ("running", None)
    assert [event for event, _ in box.gate_events()] == [LOCAL_GATE_FAILED]


def test_a_gate_timeout_refuses(box: Box, capsys) -> None:
    text = _factory_text("sleep 5").replace("timeout_seconds: 30", "timeout_seconds: 1")
    box.ready(text)

    assert box.done() == 1

    assert "local_gate failed: timeout after 1s" in capsys.readouterr().err
    assert box.state() == ("running", None)


def test_gate_failures_count_toward_the_invalid_handoff_cap(box: Box, capsys) -> None:
    box.ready(_factory_text("exit 1"))
    serve_handoff(box.store, box.job, "1102", pr_head=box.head, pr_base=FEATURE, login="stranger")

    assert box.done("1102") == 1
    for _ in range(MAX_INVALID_HANDOFFS - 1):
        assert box.done() == 1

    assert box.state() == ("failed", None)
    events = [event for event, _ in _events(box.db, box.job.id)]
    assert events.count(SUBMITTED_INVALID) == 1
    assert events.count(LOCAL_GATE_FAILED) == MAX_INVALID_HANDOFFS - 1
    assert f"{MAX_INVALID_HANDOFFS} invalid handoffs" in capsys.readouterr().err


def test_a_failing_pre_ready_runs_no_gate(box: Box) -> None:
    box.ready(_factory_text("touch base-gate-ran", pre_ready="exit 1"))

    assert box.done() == 1

    assert box.gate_events() == []
    assert [event for event, _ in _events(box.db, box.job.id)] == [PRE_READY_FAILED]
    assert not (box.work / "base-gate-ran").exists()


def test_pre_ready_and_gate_both_pass(box: Box) -> None:
    box.ready(_factory_text("touch base-gate-ran", pre_ready="true"))

    assert box.done() == 0

    assert _events(box.db, box.job.id) == [
        (PRE_READY_PASSED, f"handoff {COMMENT}: at {box.head}"),
        (LOCAL_GATE_PASSED, f"handoff {COMMENT}: at {box.head}"),
    ]


def test_a_ready_into_a_ci_branch_runs_no_gate(box: Box) -> None:
    box.ready(_factory_text("touch base-gate-ran; exit 1"), base="main")

    assert box.done() == 0

    assert box.state() == ("done", COMMENT)
    assert box.gate_events() == []
    assert not (box.work / "base-gate-ran").exists()


def test_a_repo_with_no_ci_runs_no_gate(box: Box) -> None:
    box.ready(_factory_text(None, branches=None))

    assert box.done() == 0

    assert box.state() == ("done", COMMENT)
    assert box.gate_events() == []


def test_a_questions_handoff_runs_no_gate(box: Box) -> None:
    box.ready(_factory_text("touch base-gate-ran; exit 1"))
    serve_handoff(box.store, box.job, "1102", kind="questions", head=None)

    assert box.done("1102") == 0
    assert box.gate_events() == []
    assert not (box.work / "base-gate-ran").exists()


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / "gate.sh").write_text("echo gate checked\n", encoding="utf-8")
    _git(source, "add", "gate.sh")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "head")
    bare = tmp_path / "origin" / f"{REPO}.git"
    bare.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
    monkeypatch.setenv("ORCH_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("ORCH_GIT_URL_TEMPLATE", str(tmp_path / "origin" / "{repo}.git"))
    return _git(source, "rev-parse", "HEAD")


def _remote_ready(tmp_path: Path, head: str, factory: str, base: str):
    from tests.test_pre_ready_remote import _orch_yml as remote_orch_yml
    from tests.test_waiter import FakeSpawn, enqueue
    from tests.test_waiter_remote import _finish_run, make_waiter, remote_machines

    machines = remote_machines(tmp_path, box_enabled=False)
    spawn = FakeSpawn()
    log: list[str] = []
    waiter, queue, db = make_waiter(tmp_path, machines, spawn, log=log)
    waiter.orch_yml = remote_orch_yml(tmp_path)
    job_id = enqueue(queue, "a", repo=REPO)
    waiter.step()
    with queue.connect() as connection:
        job = job_from_row(
            connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone(), connection
        )
    serve_handoff(env_store(), job, COMMENT, pr_head=head, pr_base=base)
    _serve_factory_md(env_store(), factory)
    _finish_run(tmp_path, job_id, spawn, f"ORCH-HANDOFF: {COMMENT}\n")
    waiter.step()
    return db, job_id, log


def test_the_waiter_refuses_a_remote_ready_whose_gate_fails(tmp_path: Path, origin: str) -> None:
    db, job_id, log = _remote_ready(
        tmp_path, origin, _factory_text("sh gate.sh && echo broken >&2 && exit 3"), FEATURE
    )

    assert _state(db, job_id) == ("failed", None)
    assert _gate_events(db, job_id) == [
        (LOCAL_GATE_FAILED, f"handoff {COMMENT}: local_gate failed: exit 3")
    ]
    assert any(f"local_gate failed {COMMENT}: exit 3" in line for line in log)
    assert any("gate checked" in line and "broken" in line for line in log)


def test_the_waiter_refuses_a_remote_ready_whose_gate_exits_75(tmp_path: Path, origin: str) -> None:
    db, job_id, _log = _remote_ready(tmp_path, origin, _factory_text("exit 75"), FEATURE)

    assert _state(db, job_id) == ("failed", None)
    assert [event for event, _ in _gate_events(db, job_id)] == [LOCAL_GATE_FAILED]


def test_the_waiter_completes_a_remote_ready_whose_gate_passes_at_its_head(
    tmp_path: Path, origin: str
) -> None:
    gate = f'sh gate.sh && test "$(git rev-parse HEAD)" = {origin}'
    db, job_id, _log = _remote_ready(tmp_path, origin, _factory_text(gate), FEATURE)

    assert _state(db, job_id) == ("done", COMMENT)
    assert _gate_events(db, job_id) == [(LOCAL_GATE_PASSED, f"handoff {COMMENT}: at {origin}")]


def test_the_waiter_runs_no_gate_into_a_ci_branch(tmp_path: Path, origin: str) -> None:
    db, job_id, _log = _remote_ready(tmp_path, origin, _factory_text("exit 1"), "main")

    assert _state(db, job_id) == ("done", COMMENT)
    assert _gate_events(db, job_id) == []


HEAD = "a" * 40


def _failed_build(queue: SQLiteJobQueue, *, passed: str | None = None, failed: str | None = None) -> Job:
    queue.enqueue(
        JobRequest(
            repo=REPO,
            ticket_id="42",
            action="build",
            recipient="claude",
            source_comment_id="build-source",
            timeout_seconds=5400,
            detail="{}",
        ),
        dedupe_key="build-seed",
    )
    build = queue.claim("claude")
    assert build is not None
    if failed is not None:
        queue.record_invalid_handoff(
            build.id, owner="claude", comment_id=failed,
            detail="local_gate failed: exit 1", event=LOCAL_GATE_FAILED,
        )
    if passed is not None:
        queue.record_check_passed(build.id, event=LOCAL_GATE_PASSED, comment_id=passed, head=HEAD)
    queue.finish(build.id, owner="claude", handoff_comment_id=None)
    return build


def _gated_ready(comment_id: str, base: str) -> TrackedTicket:
    from tests.test_dispatcher import READY, comment, ticket

    snapshot = ticket(
        comment(comment_id, READY.format(head=HEAD), author="claude-login", created_at=200),
        head=HEAD,
        ci={HEAD: CIResult("success")},
    )
    factory = replace(
        snapshot.factory, ci_branches=("main",), ci_local_gate=CheckCommand("make gate", 60)
    )
    return replace(snapshot, factory=factory, pr_base=base)


@pytest.mark.job_bound
@pytest.mark.parametrize(
    ("base", "passed", "failed", "settles"),
    [
        (FEATURE, None, None, False),
        (FEATURE, "ready-1", None, True),
        (FEATURE, "ready-10", None, False),
        (FEATURE, "ready-1", "ready-1", False),
        ("main", None, None, True),
    ],
)
def test_t6_settles_a_failed_build_from_its_ready_only_once_the_gate_passed(
    tmp_path: Path, base: str, passed: str | None, failed: str | None, settles: bool
) -> None:
    from tests.test_dispatcher import setup

    dispatcher, queue = setup(tmp_path)
    build = _failed_build(queue, passed=passed, failed=failed)
    snapshot = _gated_ready("ready-1", base)

    dispatcher.process(snapshot)
    dispatcher.reconcile_jobs()
    dispatcher.process(snapshot)

    with queue.connect() as connection:
        handoff = connection.execute(
            "SELECT handoff_comment_id FROM jobs WHERE id = ?", (build.id,)
        ).fetchone()[0]
    assert (handoff == "ready-1") is settles
