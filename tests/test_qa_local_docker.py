from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
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
from tests.test_qa_cloud import Lab

HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
LANE_SCRIPT = Path(__file__).resolve().parents[1] / "lib" / "qa_runners" / "local_lane.sh"
CREDENTIAL = {"ANTHROPIC_API_KEY": "sk-test-not-a-real-key"}
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "QA",
    "GIT_AUTHOR_EMAIL": "qa@example.invalid",
    "GIT_COMMITTER_NAME": "QA",
    "GIT_COMMITTER_EMAIL": "qa@example.invalid",
}


def git(*args: object, cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *map(str, args)], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def source_repo(root: Path) -> tuple[Path, str]:
    source = root / "source"
    if not source.exists():
        git("init", "-b", "main", source)
        (source / "app.txt").write_text("base\n")
        git("add", ".", cwd=source)
        git("commit", "-m", "base", cwd=source)
    return source, git("rev-parse", "main", cwd=source)


class FakeDocker:
    def __init__(
        self,
        *,
        daemon_down: bool = False,
        run_error: str | None = None,
        run_times_out: bool = False,
        down_after_run: bool = False,
    ) -> None:
        self.daemon_down = daemon_down
        self.run_error = run_error
        self.run_times_out = run_times_out
        self.down_after_run = down_after_run
        self.calls: list[list[str]] = []
        self.containers: dict[str, str] = {}
        self.removed: list[str] = []
        self.run_timeouts: list[int] = []

    def __call__(self, argv: list[str], *, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git":
            return subprocess.run(
                list(argv), cwd=cwd, env=GIT_ENV, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=timeout,
            )
        self.calls.append(list(argv))

        def done(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, code, out, err)

        if self.daemon_down:
            return done(1, err="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")
        sub, name = argv[1], argv[-1]
        if sub == "version":
            return done(0, "29.9.2\n")
        if sub == "inspect":
            if name not in self.containers:
                return done(1, err=f"Error: No such object: {name}\n")
            return done(0, "true\n" if self.containers[name] == "running" else "false\n")
        if sub == "run":
            self.run_timeouts.append(timeout)
            if self.run_error:
                return done(125, err=self.run_error)
            self.containers[argv[argv.index("--name") + 1]] = "running"
            self.daemon_down = self.down_after_run
            if self.run_times_out:
                self.run_times_out = False
                raise subprocess.TimeoutExpired(argv, timeout)
            return done(0, "deadbeef1234\n")
        if sub == "rm":
            if self.containers.pop(name, None) is None:
                return done(1, err=f"Error response from daemon: No such container: {name}\n")
            self.removed.append(name)
            return done(0, f"{name}\n")
        raise AssertionError(argv)

    def runs(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv[1] == "run"]


def _runner(
    tmp_path: Path,
    stub: FakeDocker,
    *,
    home: Path | None = None,
    environ: dict[str, str] | None = None,
    work_root: Path | None = None,
) -> LocalDockerRunner:
    source, _ = source_repo(tmp_path)
    cfg = LocalDockerConfig(
        work_root=work_root or tmp_path / "lanes",
        repo_cache_root=tmp_path / "repos",
        repo_url_template=str(source),
    )
    return LocalDockerRunner(cfg, run=stub, home=home or tmp_path / "home", environ=CREDENTIAL if environ is None else environ)


def _lane(
    lane_id: str = "lane-abc-1",
    environment: str = "qa-local",
    model: str | None = None,
    base_ref: str = "main",
    commits: tuple[str, ...] = (),
) -> Lane:
    return Lane(
        lane_id=lane_id,
        brief="# QA lane brief\nRun checks.\n",
        repo="maxharris1/cleffy",
        base_ref=base_ref,
        environment_name=environment,
        model=model,
        commits=commits,
    )


def _mounts(argv: list[str]) -> dict[str, str]:
    pairs = [argv[i + 1] for i, part in enumerate(argv) if part == "-v"]
    return {spec.split(":")[1]: spec.split(":")[0] for spec in pairs}


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
    stub = FakeDocker()
    runner = _runner(tmp_path, stub)
    lane = _lane()

    first = runner.launch(lane)
    assert runner.launch(lane) == first
    assert first.agent_id == container_name_for(lane.lane_id)
    assert first.url == f"docker://local/{first.agent_id}"
    assert _runner(tmp_path, stub).launch(lane) == first, "a restarted runner adopts the launched lane"

    assert len(stub.runs()) == 1
    run_argv = stub.runs()[0]
    for flag in ("--rm", "--detach", "--read-only", "--pids-limit"):
        assert flag in run_argv
    assert run_argv[run_argv.index("--cap-drop") + 1] == "ALL"
    assert run_argv[run_argv.index("--security-opt") + 1] == "no-new-privileges:true"
    assert run_argv[run_argv.index("--memory") + 1] == "4g"
    assert run_argv[run_argv.index("--cpus") + 1] == "2"
    assert DEFAULT_IMAGE in run_argv
    assert any(part.endswith(":/qa-brief.md:ro") for part in run_argv)
    assert any(part.endswith(":/qa-evidence:rw") for part in run_argv)
    assert any(part.endswith(":/workspace:rw") for part in run_argv)

    assert runner.status(first.agent_id) == "running"
    info = runner.describe(first.agent_id)
    assert "local-docker" in info.machine
    assert info.version == "docker-29.9.2"


def test_status_transitions_to_ended_and_stop_removes_the_lane_dir(tmp_path: Path) -> None:
    stub = FakeDocker()
    runner = _runner(tmp_path, stub)
    launched = runner.launch(_lane("lane-clean-1"))
    lane_dir = tmp_path / "lanes" / launched.agent_id
    assert (lane_dir / "worktree" / "app.txt").is_file()
    assert runner.status(launched.agent_id) == "running"

    stub.containers.pop(launched.agent_id)
    assert runner.status(launched.agent_id) == "ended"

    runner.stop(launched.agent_id)
    assert not lane_dir.exists()
    runner.stop(launched.agent_id)


def test_stop_kills_running_container_and_cleans_up_after_restart(tmp_path: Path) -> None:
    stub = FakeDocker()
    launched = _runner(tmp_path, stub).launch(_lane("lane-running-1"))

    fresh = _runner(tmp_path, stub)
    fresh.stop(launched.agent_id)
    assert launched.agent_id in stub.removed
    assert fresh.status(launched.agent_id) == "ended"
    assert not (tmp_path / "lanes" / launched.agent_id).exists()


def test_daemon_unavailable_raises_runner_outage(tmp_path: Path) -> None:
    runner = _runner(tmp_path, FakeDocker(daemon_down=True))
    with pytest.raises(RunnerOutage):
        runner.launch(_lane("lane-down-1"))
    with pytest.raises(RunnerOutage):
        runner.status("factory-qa-0123456789abcdef")
    with pytest.raises(RunnerOutage):
        runner.stop("factory-qa-0123456789abcdef")


@pytest.mark.parametrize("relative", [".local/state/factory/qa", ".config/factory/qa", ".", ".."])
def test_refuses_work_roots_inside_or_around_protected_paths(tmp_path: Path, relative: str) -> None:
    home = tmp_path / "home"
    (home / ".config" / "factory").mkdir(parents=True)
    runner = _runner(tmp_path, FakeDocker(), home=home, work_root=home / relative)
    with pytest.raises(LaunchFailed, match="protected directory"):
        runner.launch(_lane("lane-bad-path"))


@pytest.mark.parametrize("relative", [".", "..", ".ssh/keys"])
def test_mount_source_containing_or_inside_protected_state_is_refused(tmp_path: Path, relative: str) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    safe = tmp_path / "lane"
    safe.mkdir()
    runner = _runner(tmp_path, FakeDocker(), home=home)
    with pytest.raises(LaunchFailed, match="protected directory"):
        runner.build_docker_run_argv(
            _lane(), agent_id="factory-qa-x", worktree=safe, brief_file=safe / "brief.md",
            evidence_dir=(home / relative),
        )


def test_launch_never_writes_through_a_planted_brief_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    protected = home / ".config" / "factory"
    protected.mkdir(parents=True)
    target = protected / "dummy.txt"
    target.write_text("ORIGINAL DUMMY CONTENT\n")
    lane = _lane("planted-brief")
    lane_dir = tmp_path / "lanes" / container_name_for(lane.lane_id)
    lane_dir.mkdir(parents=True)
    (lane_dir / "brief.md").symlink_to(target)

    try:
        _runner(tmp_path, FakeDocker(), home=home).launch(lane)
    except LaunchFailed:
        pass
    assert target.read_text() == "ORIGINAL DUMMY CONTENT\n"


def test_launch_refuses_a_repo_cache_entry_symlinked_into_protected_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    protected = home / ".config" / "factory" / "protected.git"
    source, _ = source_repo(tmp_path)
    git("clone", "--bare", "--", source, protected)
    before = sorted(str(p.relative_to(protected)) for p in protected.rglob("*"))
    (tmp_path / "repos").mkdir()
    (tmp_path / "repos" / "maxharris1__cleffy.git").symlink_to(protected, target_is_directory=True)
    stub = FakeDocker()

    with pytest.raises(LaunchFailed, match="protected directory"):
        _runner(tmp_path, stub, home=home).launch(_lane("planted-cache"))
    assert stub.runs() == []
    assert sorted(str(p.relative_to(protected)) for p in protected.rglob("*")) == before


@pytest.mark.parametrize("scheme", ["", "file://"])
def test_launch_refuses_a_local_repo_source_inside_protected_state(tmp_path: Path, scheme: str) -> None:
    home = tmp_path / "home"
    protected = home / ".config" / "factory" / "source.git"
    source, _ = source_repo(tmp_path)
    git("clone", "--bare", "--", source, protected)
    runner = LocalDockerRunner(
        LocalDockerConfig(
            work_root=tmp_path / "lanes",
            repo_cache_root=tmp_path / "repos",
            repo_url_template=f"{scheme}{protected}",
        ),
        run=FakeDocker(),
        home=home,
        environ=CREDENTIAL,
    )

    with pytest.raises(LaunchFailed, match="protected directory"):
        runner.launch(_lane("protected-source"))
    assert not (tmp_path / "repos").exists()


def test_launch_and_stop_refuse_a_lane_dir_symlinked_into_protected_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    protected = home / ".config" / "factory" / "lane"
    protected.mkdir(parents=True)
    lane = _lane("planted-lane-dir")
    agent_id = container_name_for(lane.lane_id)
    marker = {"lane_id": lane.lane_id, "agent_id": agent_id, "url": f"docker://local/{agent_id}", "state": "running"}
    (protected / "launched.json").write_text(json.dumps(marker))
    (tmp_path / "lanes").mkdir()
    (tmp_path / "lanes" / agent_id).symlink_to(protected, target_is_directory=True)
    runner = _runner(tmp_path, FakeDocker(), home=home)

    with pytest.raises(LaunchFailed, match="protected directory"):
        runner.launch(lane)
    with pytest.raises(LaunchFailed, match="protected directory"):
        runner.stop(agent_id)
    assert (protected / "launched.json").exists()


def test_launch_and_stop_unlink_a_lane_dir_symlink_without_deleting_its_target(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("keep\n")
    lane = _lane("symlinked-lane-dir")
    agent_id = container_name_for(lane.lane_id)
    (tmp_path / "lanes").mkdir()
    (tmp_path / "lanes" / agent_id).symlink_to(elsewhere, target_is_directory=True)
    runner = _runner(tmp_path, FakeDocker())

    runner.launch(lane)
    assert not (tmp_path / "lanes" / agent_id).is_symlink()
    runner.stop(agent_id)
    assert (elsewhere / "keep.txt").read_text() == "keep\n"


def test_launch_never_mounts_a_directory_holding_protected_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "factory").mkdir(parents=True)
    lane = _lane("planted-evidence")
    lane_dir = tmp_path / "lanes" / container_name_for(lane.lane_id)
    lane_dir.mkdir(parents=True)
    (lane_dir / "evidence").symlink_to(home, target_is_directory=True)
    stub = FakeDocker()

    try:
        _runner(tmp_path, stub, home=home).launch(lane)
    except LaunchFailed:
        assert stub.runs() == []
        return
    evidence = Path(_mounts(stub.runs()[0])["/qa-evidence"])
    assert not evidence.is_symlink() and evidence.is_dir()
    assert evidence.resolve() not in (home.resolve(), *home.resolve().parents)


def test_docker_run_failure_raises_launch_failed_and_cleans_the_lane_dir(tmp_path: Path) -> None:
    stub = FakeDocker(run_error="Unable to find image 'missing:latest' locally")
    lane = _lane("lane-img-err", environment="missing:latest")
    with pytest.raises(LaunchFailed, match="docker run failed"):
        _runner(tmp_path, stub).launch(lane)
    assert not (tmp_path / "lanes" / container_name_for(lane.lane_id)).exists()


def test_launch_checks_out_the_requested_ref_not_a_stale_cached_branch(tmp_path: Path) -> None:
    stub = FakeDocker()
    runner = _runner(tmp_path, stub)
    source, _ = source_repo(tmp_path)
    git("branch", "release", cwd=source)
    runner.launch(_lane("first", base_ref="release"))

    git("branch", "-D", "release", cwd=source)
    (source / "app.txt").write_text("tag\n")
    git("commit", "-am", "tag", cwd=source)
    git("tag", "release", cwd=source)
    second = runner.launch(_lane("second", base_ref="release"))

    worktree = tmp_path / "lanes" / second.agent_id / "worktree"
    assert git("rev-parse", "HEAD", cwd=worktree) == git("rev-parse", "release", cwd=source)
    assert (worktree / "app.txt").read_text() == "tag\n"
    assert git("for-each-ref", "refs/factory-qa", cwd=tmp_path / "repos" / "maxharris1__cleffy.git") == ""


def test_lane_clone_serves_the_brief_commit_commands_without_the_host_cache(tmp_path: Path) -> None:
    source, base = source_repo(tmp_path)
    git("checkout", "-b", "pr", cwd=source)
    (source / "app.txt").write_text("pr\n")
    git("commit", "-am", "pr", cwd=source)
    head = git("rev-parse", "HEAD", cwd=source)
    git("checkout", "main", cwd=source)
    launched = _runner(tmp_path, FakeDocker()).launch(_lane("brief-commands", commits=(head, base)))

    worktree = tmp_path / "lanes" / launched.agent_id / "worktree"
    assert git("rev-parse", "HEAD", cwd=worktree) == base
    assert git("remote", "get-url", "origin", cwd=worktree) == "/workspace"
    objects = [path for path in (worktree / ".git" / "objects").rglob("*") if path.is_file()]
    assert objects and all(path.stat().st_nlink == 1 for path in objects), "lane objects share no inode with the cache"

    workspace = tmp_path / "container" / "workspace"
    shutil.copytree(worktree, workspace, symlinks=True)
    (tmp_path / "repos").rename(tmp_path / "unmounted-repos")
    git("status", "--short", cwd=workspace)
    git("remote", "set-url", "origin", workspace, cwd=workspace)
    git("fetch", "origin", head, cwd=workspace)
    git("worktree", "add", "--detach", tmp_path / "lane-worktree", head, cwd=workspace)
    assert git("rev-parse", "HEAD", cwd=tmp_path / "lane-worktree") == head


def test_a_starting_marker_is_not_a_launched_lane(tmp_path: Path) -> None:
    stub = FakeDocker()
    lane = _lane("crashed-mid-launch")
    agent_id = container_name_for(lane.lane_id)
    lane_dir = tmp_path / "lanes" / agent_id
    lane_dir.mkdir(parents=True)
    (lane_dir / "launched.json").write_text(json.dumps(
        {"lane_id": lane.lane_id, "agent_id": agent_id, "url": f"docker://local/{agent_id}", "state": "starting"}
    ))

    _runner(tmp_path, stub).launch(lane)
    assert len(stub.runs()) == 1
    assert stub.containers == {agent_id: "running"}


def test_an_unreconciled_launch_is_swept_by_the_next_launch(tmp_path: Path) -> None:
    stub = FakeDocker(run_times_out=True, down_after_run=True)
    lane = _lane("timed-out")
    with pytest.raises(RunnerOutage):
        _runner(tmp_path, stub).launch(lane)
    orphan = container_name_for(lane.lane_id)
    assert orphan in stub.containers
    assert json.loads((tmp_path / "lanes" / orphan / "launched.json").read_text())["state"] == "uncertain"

    stub.daemon_down = stub.down_after_run = False
    later = _runner(tmp_path, stub).launch(_lane("later"))
    assert set(stub.containers) == {later.agent_id}
    assert sorted(path.name for path in (tmp_path / "lanes").iterdir()) == [later.agent_id]


def test_a_timed_out_docker_run_leaves_no_orphan_across_a_worker_retry(tmp_path: Path) -> None:
    stub = FakeDocker(run_times_out=True)
    runner = _runner(tmp_path, stub)
    _, real = source_repo(tmp_path)
    launch = runner.launch
    runner.launch = lambda lane: launch(replace(lane, commits=(real,)))
    lab = Lab(tmp_path / "worker", lanes=1)
    base = lab.run.thread.base
    qa = replace(base.factory.qa, runners=(QaRunner("local-docker", "qa"),))
    lab.run.thread.base = replace(base, factory=replace(base.factory, qa=qa))
    lab.fakes = {"local-docker": runner}
    lab.worker = lab.new_worker()
    lab.enqueue(ids=1, runner="local-docker")

    lab.tick()
    assert stub.containers == {}
    assert list((tmp_path / "lanes").iterdir()) == []
    lab.run.now[0] += 1000
    lab.tick()

    agents = {agent for (agent,) in lab.run.rows("SELECT agent_id FROM qa_lanes")}
    assert len(stub.runs()) == 2
    assert set(stub.containers) <= agents
    assert len(list((tmp_path / "lanes").iterdir())) <= 1


def test_docker_run_waits_long_enough_for_a_first_image_pull(tmp_path: Path) -> None:
    stub = FakeDocker()
    _runner(tmp_path, stub).launch(_lane())
    assert stub.run_timeouts and min(stub.run_timeouts) >= 600


def test_lane_script_feeds_the_brief_to_headless_claude(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "claude-call.txt"
    fake = bin_dir / "claude"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\necho --- >> {record}\ncat >> {record}\n')
    fake.chmod(0o755)
    brief = tmp_path / "brief.md"
    brief.write_text("# QA lane brief\n")

    proc = subprocess.run(
        [str(LANE_SCRIPT)], capture_output=True, text=True, timeout=30,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "QA_BRIEF_PATH": str(brief), "QA_MODEL": "opus"},
    )
    assert proc.returncode == 0, proc.stderr
    argv, stdin = record.read_text().split("---\n")
    assert argv.split() == ["-p", "--dangerously-skip-permissions", "--model", "opus"]
    assert stdin == "# QA lane brief\n"


def test_lane_script_fails_when_claude_cannot_be_installed(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("# QA lane brief\n")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    proc = subprocess.run(
        ["/bin/sh", str(LANE_SCRIPT)], capture_output=True, text=True, timeout=30,
        env={"PATH": str(empty), "HOME": str(tmp_path), "QA_BRIEF_PATH": str(brief)},
    )
    assert proc.returncode != 0
    assert "npm" in proc.stderr


def test_default_command_runs_the_mounted_lane_script_as_the_host_user(tmp_path: Path) -> None:
    stub = FakeDocker()
    environ = {"ANTHROPIC_API_KEY": "sk-secret-value", "CLAUDE_CODE_OAUTH_TOKEN": ""}
    _runner(tmp_path, stub, environ=environ).launch(_lane())
    argv = stub.runs()[0]

    assert f"{LANE_SCRIPT}:/opt/factory-qa/run-lane.sh:ro" in argv
    assert argv[-1] == "/opt/factory-qa/run-lane.sh"
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "ANTHROPIC_API_KEY" in argv
    assert not any("sk-secret-value" in part or "CLAUDE_CODE_OAUTH_TOKEN" in part for part in argv)


def test_launch_without_an_agent_credential_fails_before_writing(tmp_path: Path) -> None:
    stub = FakeDocker()
    runner = _runner(tmp_path, stub, environ={"PATH": "/usr/bin"})
    with pytest.raises(LaunchFailed, match="ANTHROPIC_API_KEY"):
        runner.launch(_lane())
    assert not (tmp_path / "lanes").exists()
    assert not (tmp_path / "repos").exists()
    assert stub.runs() == []


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
