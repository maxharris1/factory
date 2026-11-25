"""Tests for E1-1: fleet-local isolated Docker/worktree QA runner (local-docker)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lib import qa_evidence, qa_runners
from lib.factory_md import QA_RUNNERS, QaRunner, parse_factory_md
from lib.handoff import parse_comment
from lib.qa_runners import Lane, LaunchFailed, RunnerOutage
from lib.qa_runners.local_docker import (
    DEFAULT_IMAGE,
    RUNNER_NAME,
    LocalDockerConfig,
    LocalDockerRunner,
    container_name_for,
    write_lane_evidence,
)

HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40


class FakeDockerGit:
    """In-memory stub for `docker` and `git` subprocess invocations."""

    def __init__(self, *, daemon_down: bool = False, run_error: str | None = None) -> None:
        self.daemon_down = daemon_down
        self.run_error = run_error
        self.calls: list[tuple[list[str], Path | None]] = []
        self.containers: dict[str, str] = {}  # name -> "running" | "exited"
        self.removed: list[str] = []
        self.worktrees_added: list[Path] = []
        self.worktrees_removed: list[Path] = []

    def __call__(self, argv: list[str], *, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), cwd))
        prog = argv[0]
        if prog == "docker":
            return self._docker(argv[1:])
        if prog == "git":
            return self._git(argv[1:], cwd)
        return subprocess.CompletedProcess(argv, 1, "", f"unexpected binary: {prog}")

    def _docker(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if self.daemon_down:
            return subprocess.CompletedProcess(
                ["docker", *args],
                1,
                "",
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
            )
        sub = args[0]
        if sub == "version":
            return subprocess.CompletedProcess(["docker", *args], 0, "29.9.2\n", "")
        if sub == "inspect":
            name = args[-1]
            state = self.containers.get(name)
            if state is None:
                return subprocess.CompletedProcess(["docker", *args], 1, "", f"Error: No such object: {name}\n")
            return subprocess.CompletedProcess(["docker", *args], 0, "true\n" if state == "running" else "false\n", "")
        if sub == "run":
            if self.run_error:
                return subprocess.CompletedProcess(["docker", *args], 125, "", self.run_error)
            name = args[args.index("--name") + 1]
            self.containers[name] = "running"
            return subprocess.CompletedProcess(["docker", *args], 0, "deadbeef1234\n", "")
        if sub == "rm":
            name = args[-1]
            if name not in self.containers:
                return subprocess.CompletedProcess(
                    ["docker", *args], 1, "", f"Error response from daemon: No such container: {name}\n"
                )
            self.containers.pop(name)
            self.removed.append(name)
            return subprocess.CompletedProcess(["docker", *args], 0, f"{name}\n", "")
        return subprocess.CompletedProcess(["docker", *args], 1, "", f"unknown docker subcommand: {sub}")

    def _git(self, args: list[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        sub = args[0]
        if sub == "clone":
            dest = Path(args[-1])
            dest.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        if sub in ("fetch", "rev-parse"):
            return subprocess.CompletedProcess(["git", *args], 0, BASE_SHA + "\n", "")
        if sub == "worktree":
            if args[1] == "prune":
                return subprocess.CompletedProcess(["git", *args], 0, "", "")
            if args[1] == "add":
                wt = Path(args[3])
                wt.mkdir(parents=True, exist_ok=True)
                self.worktrees_added.append(wt)
                return subprocess.CompletedProcess(["git", *args], 0, "", "")
            if args[1] == "remove":
                wt = Path(args[-1])
                self.worktrees_removed.append(wt)
                return subprocess.CompletedProcess(["git", *args], 0, "", "")
        return subprocess.CompletedProcess(["git", *args], 1, "", f"unexpected git args: {args}")


def _runner(tmp_path: Path, stub: FakeDockerGit, *, home: Path | None = None) -> LocalDockerRunner:
    cfg = LocalDockerConfig(
        work_root=tmp_path / "lanes",
        repo_cache_root=tmp_path / "repos",
    )
    return LocalDockerRunner(cfg, run=stub, home=home or (tmp_path / "home"))


def _lane(lane_id: str = "lane-abc-1", environment: str = "qa-local", model: str | None = None) -> Lane:
    return Lane(
        lane_id=lane_id,
        brief="# QA lane brief\nRun checks.\n",
        repo="maxharris1/cleffy",
        base_ref="main",
        environment_name=environment,
        model=model,
    )


def test_local_docker_is_registered_and_accepted_by_factory_md_and_handoff() -> None:
    assert RUNNER_NAME in QA_RUNNERS
    assert RUNNER_NAME in qa_runners.registered()
    instance = qa_runners.create(RUNNER_NAME)
    assert isinstance(instance, LocalDockerRunner)

    text = (
        "---\n"
        "base_branch: main\n"
        "tickets: github\n"
        "builder: {harness: claude}\n"
        "reviewer: {harness: codex}\n"
        "review: {rounds: {low: 1, high: 3}, verify: none}\n"
        "ship: merge\n"
        "skills: [poteto-mode]\n"
        "qa:\n"
        "  enabled: true\n"
        "  runners: [local-docker]\n"
        "  local_docker:\n"
        "    environment: qa-local\n"
        "  profile: full-stack\n"
        "  lane: {boot: 'npm run dev', base_url: 'http://127.0.0.1:3000'}\n"
        "---\n"
    )
    cfg, findings = parse_factory_md(text, strict=True)
    assert findings == []
    assert cfg is not None and cfg.qa is not None
    assert cfg.qa.runners == (QaRunner("local-docker", "qa-local", None),)

    parsed = parse_comment(
        f"<!-- orch:v1 kind=qa from=grok-bot role=qa head={HEAD_SHA} result=PASS scope=full "
        "runner=local-docker arm=primary attempt=1 -->\n## QA result: PASS\n"
    )
    assert parsed is not None
    assert dict(parsed.fields)["runner"] == "local-docker"


def test_launch_is_idempotent_across_calls_and_restarts_and_uses_hardened_flags(tmp_path: Path) -> None:
    stub = FakeDockerGit()
    runner = _runner(tmp_path, stub)
    lane = _lane()

    first = runner.launch(lane)
    second = runner.launch(lane)
    assert first == second
    assert first.agent_id == container_name_for(lane.lane_id)
    assert first.url == f"docker://local/{first.agent_id}"

    # Restart simulation: a brand-new LocalDockerRunner sees the marker and does not spawn a second container.
    restarted = _runner(tmp_path, stub)
    third = restarted.launch(lane)
    assert third == first

    docker_runs = [argv for argv, _ in stub.calls if argv[:2] == ["docker", "run"]]
    assert len(docker_runs) == 1
    run_argv = docker_runs[0]
    assert "--rm" in run_argv
    assert "--detach" in run_argv
    assert "--read-only" in run_argv
    assert "--cap-drop" in run_argv and "ALL" in run_argv
    assert "--security-opt" in run_argv and "no-new-privileges:true" in run_argv
    assert "--memory" in run_argv and "4g" in run_argv
    assert "--cpus" in run_argv and "2" in run_argv
    assert "--pids-limit" in run_argv
    assert DEFAULT_IMAGE in run_argv
    assert any(part.endswith(":/qa-brief.md:ro") for part in run_argv)
    assert any(part.endswith(":/qa-evidence:rw") for part in run_argv)
    assert any(part.endswith(":/workspace:rw") for part in run_argv)

    # Status and describe reflect live running container.
    assert runner.status(first.agent_id) == "running"
    info = runner.describe(first.agent_id)
    assert "local-docker" in info.machine
    assert info.version == "docker-29.9.2"


def test_status_transitions_to_ended_and_stop_cleans_up_worktree_and_exited_container(tmp_path: Path) -> None:
    stub = FakeDockerGit()
    runner = _runner(tmp_path, stub)
    launched = runner.launch(_lane("lane-clean-1"))
    lane_dir = tmp_path / "lanes" / launched.agent_id
    assert lane_dir.is_dir()
    assert (lane_dir / "worktree").is_dir()
    assert runner.status(launched.agent_id) == "running"

    # Container exits and is auto-removed by --rm.
    stub.containers.pop(launched.agent_id)
    assert runner.status(launched.agent_id) == "ended"

    # stop() succeeds cleanly on an already-exited container and removes worktree + lane_dir.
    runner.stop(launched.agent_id)
    assert not lane_dir.exists()
    assert len(stub.worktrees_removed) == 1

    # Stopping a second time is also a clean no-op.
    runner.stop(launched.agent_id)


def test_stop_kills_running_container_and_cleans_up_after_restart(tmp_path: Path) -> None:
    stub = FakeDockerGit()
    runner = _runner(tmp_path, stub)
    launched = runner.launch(_lane("lane-running-1"))

    # A fresh runner instance (e.g. after orch qa-worker restart) can still stop & clean up.
    fresh = _runner(tmp_path, stub)
    fresh.stop(launched.agent_id)
    assert launched.agent_id in stub.removed
    assert fresh.status(launched.agent_id) == "ended"
    assert not (tmp_path / "lanes" / launched.agent_id).exists()


def test_daemon_unavailable_raises_runner_outage(tmp_path: Path) -> None:
    stub = FakeDockerGit(daemon_down=True)
    runner = _runner(tmp_path, stub)
    with pytest.raises(RunnerOutage):
        runner.launch(_lane("lane-down-1"))
    with pytest.raises(RunnerOutage):
        runner.status("factory-qa-0123456789abcdef")
    with pytest.raises(RunnerOutage):
        runner.stop("factory-qa-0123456789abcdef")


def test_refuses_host_factory_state_and_config_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    stub = FakeDockerGit()
    for forbidden in (home / ".local" / "state" / "factory" / "qa", home / ".config" / "factory" / "qa"):
        cfg = LocalDockerConfig(work_root=forbidden, repo_cache_root=tmp_path / "repos")
        runner = LocalDockerRunner(cfg, run=stub, home=home)
        with pytest.raises(LaunchFailed, match="protected directory"):
            runner.launch(_lane("lane-bad-path"))


def test_docker_run_failure_raises_launch_failed_and_cleans_worktree(tmp_path: Path) -> None:
    stub = FakeDockerGit(run_error="Unable to find image 'missing:latest' locally")
    runner = _runner(tmp_path, stub)
    lane = _lane("lane-img-err", environment="missing:latest")
    with pytest.raises(LaunchFailed, match="docker run failed"):
        runner.launch(lane)
    assert not (tmp_path / "lanes" / container_name_for(lane.lane_id)).exists()


def test_evidence_layout_matches_qa_evidence_stored_and_collect(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    result_path, bundle_path = write_lane_evidence(
        evidence_dir,
        run_id="run-local-1",
        lane_no=1,
        result={
            "run_id": "run-local-1",
            "lane": 1,
            "kind": "head",
            "commit": HEAD_SHA,
            "base_commit": BASE_SHA,
            "method": "fallback-playwright",
            "status": "PASS",
            "scenarios": [{"id": "s-1", "criterion": "ac-1", "result": "verified", "cause": None}],
        },
        files={
            "s-1/after-screenshot.png": b"\x89PNG\r\n\x1a\nfake-png",
            "s-1/walkthrough.webm": b"\x1a\x45\xdf\xa3fake-webm",
            "s-1/pass-test-output.txt": b"verified title OK\n",
            "s-1/trace-summary.json": b'{"ok":true}\n',
        },
    )
    assert result_path.is_file()
    assert bundle_path.is_file()
    loaded = qa_evidence.stored(evidence_dir)
    assert loaded["run_id"] == "run-local-1"
    assert loaded["status"] == "PASS"
    assert loaded["mismatches"] == []
    assert len(loaded["artifacts"]) == 4

    # Feed result.json + bundle.tar.gz through qa_evidence.collect via an in-memory storage adapter.
    class _MemStorage:
        def __init__(self) -> None:
            self.data = {
                "qa/cleffy/run-local-1/lane-1/result.json": result_path.read_bytes(),
                "qa/cleffy/run-local-1/lane-1/bundle.tar.gz": bundle_path.read_bytes(),
            }

        def fetch(self, key: str, dest: Path, max_bytes: int) -> bool:
            if key not in self.data:
                return False
            dest.write_bytes(self.data[key])
            return True

    collected_root = (tmp_path / "collected").resolve()
    collected = qa_evidence.collect(
        "run-local-1",
        1,
        storage=_MemStorage(),
        repo="maxharris1/cleffy",
        ticket="42",
        root=collected_root,
    )
    assert collected is not None
    assert collected["mismatches"] == []
    dest = qa_evidence.lane_dir(collected_root, "maxharris1/cleffy", "42", "run-local-1", 1)
    assert (dest / "s-1" / "after-screenshot.png").is_file()
    assert (dest / "s-1" / "walkthrough.webm").is_file()
    assert (dest / qa_evidence.MANIFEST_NAME).is_file()
    manifest = json.loads((dest / qa_evidence.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["mismatches"] == []
