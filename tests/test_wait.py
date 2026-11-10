import ast
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.dispatcher_queue import (
    Job,
    JobQueue,
    JobQueueError,
    StubJobQueue,
    default_queue_path,
    open_queue,
)
from lib.handoff import HandoffError, parse_comment
from lib.wait import POLL_SECONDS, format_wait_output, wait_for_job

ROOT = Path(__file__).resolve().parents[1]
GO = (ROOT / "tests" / "fixtures" / "comments" / "go.txt").read_text(encoding="utf-8")
HARNESS_BINARIES = ("claude", "codex", "cursor-agent")


def run(args: list[str], queue: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), *args, "--queue", str(queue)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def enqueue(queue: Path, role: str, step: str, comment: str = GO, note: str = "") -> str:
    job = open_queue(queue).enqueue(role, step, comment, note)
    return job.id


def test_wait_for_job_blocks_until_a_job_is_claimed() -> None:
    pauses: list[float] = []
    calls = {"n": 0}

    class Queue:
        def claim(self, role: str):
            assert role == "claude-bot"
            calls["n"] += 1
            if calls["n"] < 3:
                return None
            return Job(
                id="abc123",
                role=role,
                step="build",
                comment=GO,
                note="",
            )

    job = wait_for_job(Queue(), "claude-bot", pause=lambda seconds: pauses.append(seconds))
    assert pauses == [POLL_SECONDS, POLL_SECONDS]
    assert job is not None
    assert job.step == "build"
    assert job.comment == GO


def test_wait_blocks_on_the_shell_until_a_job_arrives(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "bin" / "orch"),
            "wait",
            "claude-bot",
            "--queue",
            str(tmp_path),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(f"wait exited early ({proc.returncode}): {out!r} {err!r}")
            state = _state(proc.pid)
            if state.startswith("S"):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("wait never blocked")
        time.sleep(0.2)
        assert proc.poll() is None
        assert _pending(proc.stdout) is False
        assert _child_pids(proc.pid) == []
        assert _socket_fds(proc.pid) == []
        job_id = enqueue(tmp_path, "claude-bot", "build", GO, note="failing log")
        stdout, stderr = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, stderr
    assert stderr == ""
    meta, comment = _split_output(stdout)
    assert meta == {
        "id": job_id,
        "note": "failing log",
        "role": "claude-bot",
        "step": "build",
    }
    assert parse_comment(comment).as_dict()["kind"] == "go"
    assert comment == GO


def test_wait_does_not_start_a_harness(tmp_path: Path) -> None:
    enqueue(tmp_path, "codex-bot", "review", GO)
    result = run(["wait", "codex-bot"], tmp_path)
    assert result.returncode == 0, result.stderr
    modules = _imports(ROOT / "lib" / "wait.py") | _imports(ROOT / "lib" / "dispatcher_queue.py")
    assert "subprocess" not in modules
    assert "socket" not in modules
    cli = (ROOT / "lib" / "cli.py").read_text(encoding="utf-8")
    for name in HARNESS_BINARIES:
        assert name not in cli


def test_wait_delivers_only_the_oldest_job_for_that_role(tmp_path: Path) -> None:
    queue = open_queue(tmp_path)
    first = queue.enqueue("claude-bot", "build", GO)
    second = queue.enqueue("claude-bot", "fix", GO, "the log")
    queue.enqueue("codex-bot", "review", GO)
    claimed = queue.claim("claude-bot")
    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.note == ""
    again = queue.claim("claude-bot")
    assert again is not None
    assert again.id == second.id
    assert again.note == "the log"
    assert queue.claim("claude-bot") is None
    other = queue.claim("codex-bot")
    assert other is not None
    assert other.step == "review"


def test_job_done_completes_only_a_claimed_job(tmp_path: Path) -> None:
    job_id = enqueue(tmp_path, "cursor-bot", "fix", GO)
    queued = run(["job", "done", job_id], tmp_path)
    assert queued.returncode == 1
    assert "not claimed" in queued.stderr

    wait = run(["wait", "cursor-bot"], tmp_path)
    assert wait.returncode == 0, wait.stderr
    done = run(["job", "done", job_id], tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"done {job_id}\n"
    assert done.stderr == ""
    again = run(["job", "done", job_id], tmp_path)
    assert again.returncode == 1
    assert "not claimed" in again.stderr
    with pytest.raises(JobQueueError):
        open_queue(tmp_path).complete(job_id)


def test_wait_rejects_a_role_the_handoff_parser_rejects(tmp_path: Path) -> None:
    result = run(["wait", "Claude-Bot"], tmp_path)
    assert result.returncode == 2
    assert result.stdout == ""


def test_job_done_rejects_a_bad_id(tmp_path: Path) -> None:
    result = run(["job", "done", "../secrets"], tmp_path)
    assert result.returncode == 2
    assert result.stdout == ""


def test_enqueue_requires_an_orch_v1_comment_and_a_phase_1_step(tmp_path: Path) -> None:
    queue = open_queue(tmp_path)
    with pytest.raises(HandoffError):
        queue.enqueue("claude-bot", "build", "not a handoff\n")
    with pytest.raises(JobQueueError):
        queue.enqueue("claude-bot", "post-merge", GO)
    with pytest.raises(HandoffError):
        queue.enqueue("Bad Role", "build", GO)


def test_open_queue_returns_the_stub_seam(tmp_path: Path) -> None:
    queue = open_queue(tmp_path)
    assert isinstance(queue, StubJobQueue)
    assert isinstance(queue, JobQueue)


def test_default_queue_path_is_the_local_job_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCH_JOB_QUEUE", raising=False)
    assert default_queue_path() == Path.home() / ".factory" / "job-queue"
    monkeypatch.setenv("ORCH_JOB_QUEUE", "/tmp/factory-jobs")
    assert default_queue_path() == Path("/tmp/factory-jobs")


def test_three_handoff_skills_document_wait_and_job_done() -> None:
    expected = {
        "claude-bot": "claude",
        "codex-bot": "codex",
        "cursor-bot": "cursor-agent",
    }
    for role, harness in expected.items():
        text = (ROOT / "bots" / role / "SKILL.md").read_text(encoding="utf-8")
        heading, _, body = text.partition("## orch jobs")
        assert heading.strip()
        paragraph = body.strip()
        assert paragraph
        assert "\n\n" not in paragraph
        assert "\n#" not in paragraph
        assert f"orch wait {role}" in paragraph
        assert "orch job done" in paragraph
        assert harness in paragraph
        assert "no model tokens" in paragraph
        assert "starts" in paragraph


def test_format_wait_output_keeps_the_handoff_verbatim() -> None:
    job = Job(id="abc", role="claude-bot", step="plan", comment=GO, note="")
    meta, comment = _split_output(format_wait_output(job))
    assert meta["step"] == "plan"
    assert comment == GO


def _split_output(stdout: str) -> tuple[dict[str, str], str]:
    line, _, comment = stdout.partition("\n")
    return json.loads(line), comment


def _pending(stream: object) -> bool:
    ready, _, _ = select.select([stream], [], [], 0)
    return bool(ready)


def _state(pid: int) -> str:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("State:"):
            return line.split(":", 1)[1].strip()
    return ""


def _child_pids(pid: int) -> list[str]:
    text = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
    return text.split()


def _socket_fds(pid: int) -> list[str]:
    found: list[str] = []
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:"):
            found.append(target)
    return found


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module.split(".", 1)[0])
    return modules
