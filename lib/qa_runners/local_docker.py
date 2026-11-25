from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from lib import qa_evidence, qa_storage
from lib.qa_runners import (
    AgentInfo,
    AgentStatus,
    Lane,
    Launched,
    LaunchError,
    LaunchFailed,
    RunnerOutage,
    register,
)

RUNNER_NAME = "local-docker"
DEFAULT_IMAGE = "mcr.microsoft.com/playwright:v1.52.0-noble"
DEFAULT_MEMORY = "4g"
DEFAULT_CPUS = "2"
DEFAULT_PIDS_LIMIT = 512
DEFAULT_TIMEOUT_SECONDS = 60
RUN_WITH_PULL_TIMEOUT_SECONDS = 900
LANE_SCRIPT = Path(__file__).with_name("local_lane.sh")
LANE_SCRIPT_MOUNT = "/opt/factory-qa/run-lane.sh"
WORKSPACE = "/workspace"

_FORBIDDEN_HOST_SUBPATHS = (
    Path(".local") / "state" / "factory",
    Path(".config") / "factory",
    Path(".ssh"),
)
_FORBIDDEN_ABSOLUTE_PREFIXES = (
    Path("/etc/factory"),
    Path("/etc/factory-dashboard"),
)
_DAEMON_OUTAGE_MARKERS = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "error during connect",
    "permission denied while trying to connect",
    "docker daemon is not running",
    "failed to connect to the docker api",
)
_NOT_FOUND_MARKERS = (
    "no such object",
    "no such container",
    "is not running",
    "removal of container",
)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_SAFE_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_SAFE_REPO = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_URL = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", re.DOTALL)
_REMOTE_SCHEMES = frozenset({"https", "http", "ssh", "git", "git+ssh", "ssh+git"})


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _default_run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=None if cwd is None else str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def container_name_for(lane_id: str) -> str:
    digest = hashlib.sha256(lane_id.encode("utf-8")).hexdigest()[:16]
    return f"factory-qa-{digest}"


def _is_daemon_outage(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _DAEMON_OUTAGE_MARKERS)


def _is_not_found(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _NOT_FOUND_MARKERS)


def _assert_safe_host_path(path: Path, *, home: Path) -> Path:
    path = path.resolve()
    home = home.resolve()
    protected = (
        *((home / sub).resolve() for sub in _FORBIDDEN_HOST_SUBPATHS),
        *(prefix.resolve() for prefix in _FORBIDDEN_ABSOLUTE_PREFIXES),
    )
    for item in protected:
        if path == item or item in path.parents or path in item.parents:
            raise LaunchFailed(f"refusing host path that overlaps a protected directory: {item}")
    return path


def _assert_safe_source(url: str, *, home: Path) -> str:
    match = _URL.match(url)
    if match and match.group(1).lower() in _REMOTE_SCHEMES:
        return url
    if match and match.group(1) == "file":
        decoded = unquote(match.group(2))
        if "/" not in decoded:
            raise LaunchFailed(f"unsupported repository source: {url!r}")
        _assert_safe_host_path(Path(decoded[decoded.index("/"):]), home=home)
        return url
    first = url.split("/", 1)[0]
    if match or "::" in first or first.lower().startswith("file:"):
        raise LaunchFailed(f"unsupported repository source: {url!r}")
    _assert_safe_host_path(Path(url), home=home)
    return url


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _read_marker(marker: Path) -> dict:
    try:
        saved = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


@dataclass(frozen=True)
class LocalDockerConfig:
    work_root: Path
    repo_cache_root: Path
    image: str = DEFAULT_IMAGE
    memory: str = DEFAULT_MEMORY
    cpus: str = DEFAULT_CPUS
    pids_limit: int = DEFAULT_PIDS_LIMIT
    read_only_root: bool = True
    network: str | None = None
    entrypoint_cmd: tuple[str, ...] = (LANE_SCRIPT_MOUNT,)
    agent_env: tuple[str, ...] = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    docker_binary: str = "docker"
    git_binary: str = "git"
    repo_url_template: str = "https://github.com/{repo}.git"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> LocalDockerConfig:
        env = os.environ if environ is None else environ
        base_work = Path(
            env.get("ORCH_QA_DOCKER_WORK_ROOT")
            or Path("/tmp") / "factory-qa-lanes"
        )
        base_cache = Path(
            env.get("ORCH_QA_DOCKER_REPO_CACHE")
            or Path("/tmp") / "factory-qa-repos"
        )
        image = env.get("ORCH_QA_DOCKER_IMAGE") or DEFAULT_IMAGE
        memory = env.get("ORCH_QA_DOCKER_MEMORY") or DEFAULT_MEMORY
        cpus = env.get("ORCH_QA_DOCKER_CPUS") or DEFAULT_CPUS
        pids_raw = env.get("ORCH_QA_DOCKER_PIDS_LIMIT")
        pids_limit = int(pids_raw) if pids_raw and pids_raw.isdigit() else DEFAULT_PIDS_LIMIT
        read_only = env.get("ORCH_QA_DOCKER_READ_ONLY", "1").strip().lower() not in {"0", "false", "no"}
        network = env.get("ORCH_QA_DOCKER_NETWORK") or None
        cmd_override = env.get("ORCH_QA_DOCKER_CMD")
        entrypoint = ("sh", "-lc", cmd_override) if cmd_override else cls.entrypoint_cmd
        agent_env_raw = env.get("ORCH_QA_DOCKER_AGENT_ENV")
        agent_env = (
            tuple(name.strip() for name in agent_env_raw.split(",") if name.strip())
            if agent_env_raw
            else cls.agent_env
        )
        url_template = env.get("ORCH_QA_REPO_URL_TEMPLATE") or cls.repo_url_template
        return cls(
            work_root=base_work,
            repo_cache_root=base_cache,
            image=image,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            read_only_root=read_only,
            network=network,
            entrypoint_cmd=entrypoint,
            agent_env=agent_env,
            repo_url_template=url_template,
        )


@dataclass(frozen=True)
class _LanePaths:
    lane_dir: Path
    brief: Path
    evidence: Path
    marker: Path
    worktree: Path


def write_lane_evidence(
    evidence_dir: Path,
    *,
    run_id: str,
    lane_no: int,
    result: Mapping[str, object],
    files: Mapping[str, bytes],
) -> tuple[Path, Path]:
    import tarfile

    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, object]] = []
    for rel, payload in sorted(files.items()):
        parts = qa_evidence._safe_parts(rel)
        if parts is None:
            raise ValueError(f"unsafe evidence relative path: {rel!r}")
        dest = evidence_dir.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        artifacts.append({"path": "/".join(parts), "sha256": digest, "size": len(payload)})

    result_payload = dict(result)
    result_payload.setdefault("run_id", run_id)
    result_payload.setdefault("lane", lane_no)
    result_payload["artifacts"] = artifacts
    result_path = evidence_dir / qa_storage.RESULT_NAME
    result_path.write_text(json.dumps(result_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    bundle_path = evidence_dir / qa_storage.BUNDLE_NAME
    with tarfile.open(bundle_path, "w:gz") as tar:
        for item in artifacts:
            rel_path = str(item["path"])
            tar.add(evidence_dir.joinpath(*rel_path.split("/")), arcname=rel_path)

    manifest = {
        "run_id": run_id,
        "lane": lane_no,
        "commit": result_payload.get("commit"),
        "files": artifacts,
        "mismatches": [],
        "note": "Generated by local-docker QA runner.",
    }
    (evidence_dir / qa_evidence.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result_path, bundle_path




class LocalDockerRunner:
    def __init__(
        self,
        config: LocalDockerConfig | None = None,
        *,
        run: RunCommand = _default_run,
        home: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config or LocalDockerConfig.from_env(environ)
        self._run = run
        self._home = Path.home() if home is None else Path(home)
        self._environ = os.environ if environ is None else environ
        self._by_lane: dict[str, Launched] = {}

    def _docker(self, *args: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
        try:
            proc = self._run([self.config.docker_binary, *args], timeout=timeout)
        except FileNotFoundError:
            raise RunnerOutage("docker binary is not installed") from None
        except subprocess.TimeoutExpired:
            raise RunnerOutage("docker command timed out") from None
        except OSError as exc:
            raise RunnerOutage(f"docker invocation failed: {exc}") from None
        return proc

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = self._run([self.config.git_binary, *args], cwd=cwd, timeout=timeout)
        except FileNotFoundError:
            raise LaunchFailed("git binary is not installed") from None
        except subprocess.TimeoutExpired:
            raise LaunchFailed("git command timed out") from None
        except OSError as exc:
            raise LaunchFailed(f"git invocation failed: {exc}") from None
        return proc

    def _git_ok(self, *args: str, cwd: Path | None = None, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
        proc = self._git(*args, cwd=cwd, timeout=timeout)
        if proc.returncode != 0:
            raise LaunchFailed(f"git {args[0]} failed: {(proc.stderr or '').strip()[:200]}")
        return (proc.stdout or "").strip()

    def _check_daemon(self) -> str:
        proc = self._docker("version", "--format", "{{.Server.Version}}", timeout=15)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RunnerOutage(f"docker daemon unavailable: {err[:160] or f'exit {proc.returncode}'}")
        return (proc.stdout or "").strip() or "unknown"

    def _inspect_running(self, agent_id: str) -> bool | None:
        proc = self._docker("inspect", "--format", "{{.State.Running}}", agent_id, timeout=15)
        if proc.returncode == 0:
            return (proc.stdout or "").strip().lower() == "true"
        err = (proc.stderr or proc.stdout or "").strip()
        if _is_daemon_outage(err):
            raise RunnerOutage(f"docker daemon unavailable: {err[:160]}")
        if _is_not_found(err):
            return None
        raise RunnerOutage(f"docker inspect failed: {err[:160] or f'exit {proc.returncode}'}")

    def _rm_container(self, agent_id: str) -> None:
        proc = self._docker("rm", "-f", agent_id, timeout=30)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if _is_daemon_outage(err):
                raise RunnerOutage(f"docker daemon unavailable: {err[:160]}")
            if not _is_not_found(err):
                raise LaunchFailed(f"docker rm failed: {err[:200] or f'exit {proc.returncode}'}")

    def _agent_env(self) -> tuple[str, ...]:
        names = tuple(name for name in self.config.agent_env if self._environ.get(name))
        if not names:
            raise LaunchFailed(f"no agent credential for the QA lane: set one of {', '.join(self.config.agent_env)}")
        return names

    def _repo_cache(self, lane: Lane) -> Path:
        if not _SAFE_REPO.fullmatch(lane.repo) or ".." in lane.repo:
            raise LaunchFailed(f"unsafe repository name: {lane.repo!r}")
        if not _SAFE_REF.fullmatch(lane.base_ref) or ".." in lane.base_ref or lane.base_ref.startswith("-"):
            raise LaunchFailed(f"unsafe base_ref: {lane.base_ref!r}")
        for sha in lane.commits:
            if not _SHA.fullmatch(sha):
                raise LaunchFailed(f"unsafe commit: {sha!r}")
        owner, name = lane.repo.split("/", 1)
        _assert_safe_source(self.config.repo_url_template.format(repo=lane.repo), home=self._home)
        cache_root = _assert_safe_host_path(self.config.repo_cache_root, home=self._home)
        return _assert_safe_host_path(cache_root / f"{owner}__{name}.git", home=self._home)

    def _lane_paths(self, agent_id: str) -> _LanePaths:
        if not _SAFE_AGENT_ID.fullmatch(agent_id) or ".." in agent_id:
            raise LaunchFailed(f"unsafe agent_id: {agent_id!r}")
        work_root = _assert_safe_host_path(self.config.work_root, home=self._home)
        lane_dir = work_root / agent_id
        _assert_safe_host_path(lane_dir, home=self._home)
        return _LanePaths(
            lane_dir=lane_dir,
            brief=lane_dir / "brief.md",
            evidence=lane_dir / "evidence",
            marker=lane_dir / "launched.json",
            worktree=lane_dir / "worktree",
        )

    def _prepare_worktree(self, lane: Lane, lane_dir: Path) -> tuple[Path, Path]:
        repo_cache = self._repo_cache(lane)
        worktree = lane_dir / "worktree"
        if not repo_cache.exists():
            repo_cache.parent.mkdir(parents=True, exist_ok=True)
            url = _assert_safe_source(self.config.repo_url_template.format(repo=lane.repo), home=self._home)
            self._git_ok("clone", "--bare", "--", url, str(repo_cache), timeout=180)
        lane_refs = f"refs/factory-qa/{container_name_for(lane.lane_id)}"
        refs = [f"{lane_refs}/base", *(f"{lane_refs}/c{index}" for index in range(len(lane.commits)))]
        try:
            branch = self._git("fetch", "origin", f"+refs/heads/{lane.base_ref}:{refs[0]}", cwd=repo_cache, timeout=120)
            if branch.returncode != 0:
                self._git_ok("fetch", "origin", f"+{lane.base_ref}:{refs[0]}", cwd=repo_cache, timeout=120)
            for ref, sha in zip(refs[1:], lane.commits):
                self._git_ok("fetch", "origin", f"+{sha}:{ref}", cwd=repo_cache, timeout=120)
            base = self._git_ok("rev-parse", "--verify", f"{refs[0]}^{{commit}}", cwd=repo_cache)
            self._git_ok("clone", "--no-hardlinks", "--no-checkout", "--", str(repo_cache), str(worktree), timeout=180)
        finally:
            for ref in refs:
                self._git("update-ref", "-d", ref, cwd=repo_cache)
        for sha in dict.fromkeys((base, *lane.commits)):
            self._git_ok("update-ref", f"refs/qa/{sha}", sha, cwd=worktree)
        self._git_ok("checkout", "--quiet", "--detach", base, cwd=worktree)
        self._git_ok("remote", "set-url", "origin", WORKSPACE, cwd=worktree)
        return repo_cache, worktree

    def _image_for(self, lane: Lane) -> str:
        candidate = lane.environment_name
        if candidate and (":" in candidate or "/" in candidate) and _SAFE_TOKEN.fullmatch(candidate):
            return candidate
        return self.config.image

    def build_docker_run_argv(
        self,
        lane: Lane,
        *,
        agent_id: str,
        worktree: Path,
        brief_file: Path,
        evidence_dir: Path,
    ) -> list[str]:
        worktree, brief_file, evidence_dir, script = (
            _assert_safe_host_path(path, home=self._home) for path in (worktree, brief_file, evidence_dir, LANE_SCRIPT)
        )
        argv = [
            "run",
            "--detach",
            "--rm",
            "--name",
            agent_id,
            # --cap-drop ALL leaves root unable to write the host-owned bind mounts.
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--label",
            f"factory.qa.lane_id={lane.lane_id}",
            "--label",
            f"factory.qa.repo={lane.repo}",
            "--memory",
            self.config.memory,
            "--cpus",
            self.config.cpus,
            "--pids-limit",
            str(self.config.pids_limit),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
        ]
        if self.config.read_only_root:
            argv.extend(
                [
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,size=1g",
                    "--tmpfs",
                    "/var/tmp:rw,size=256m",
                    "--tmpfs",
                    "/home/qa:rw,size=512m",
                ]
            )
        if self.config.network:
            argv.extend(["--network", self.config.network])
        argv.extend(
            [
                "-v",
                f"{worktree}:{WORKSPACE}:rw",
                "-v",
                f"{brief_file}:/qa-brief.md:ro",
                "-v",
                f"{evidence_dir}:/qa-evidence:rw",
                "-v",
                f"{script}:{LANE_SCRIPT_MOUNT}:ro",
                "-w",
                WORKSPACE,
                "-e",
                "HOME=/home/qa",
                "-e",
                f"QA_LANE_ID={lane.lane_id}",
                "-e",
                f"QA_REPO={lane.repo}",
                "-e",
                f"QA_BASE_REF={lane.base_ref}",
                "-e",
                f"QA_ENVIRONMENT={lane.environment_name}",
                "-e",
                "QA_BRIEF_PATH=/qa-brief.md",
                "-e",
                "QA_EVIDENCE_DIR=/qa-evidence",
            ]
        )
        if lane.model:
            argv.extend(["-e", f"QA_MODEL={lane.model}"])
        for name in self._agent_env():
            argv.extend(["-e", name])
        argv.append(self._image_for(lane))
        argv.extend(self.config.entrypoint_cmd)
        return argv

    def launch(self, lane: Lane) -> Launched:
        if lane.lane_id in self._by_lane:
            return self._by_lane[lane.lane_id]
        agent_id = container_name_for(lane.lane_id)
        launched = Launched(agent_id=agent_id, url=f"docker://local/{agent_id}")
        paths = self._lane_paths(agent_id)
        self._repo_cache(lane)
        _assert_safe_host_path(LANE_SCRIPT, home=self._home)
        saved = _read_marker(paths.marker)
        if not (saved.get("state") == "running" and saved.get("lane_id") == lane.lane_id):
            self._agent_env()
            self._check_daemon()
            self._sweep_uncertain(paths.lane_dir.parent)
            if self._inspect_running(agent_id) is None:
                self._start(lane, paths, launched)
        self._by_lane[lane.lane_id] = launched
        return launched

    def _start(self, lane: Lane, paths: _LanePaths, launched: Launched) -> None:
        _remove(paths.lane_dir)
        paths.lane_dir.parent.mkdir(parents=True, exist_ok=True)
        paths.lane_dir.mkdir()
        brief = os.open(paths.brief, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
        with os.fdopen(brief, "w", encoding="utf-8") as handle:
            handle.write(lane.brief)
        paths.evidence.mkdir()
        self._mark(paths, lane, launched, "starting")
        try:
            self._prepare_worktree(lane, paths.lane_dir)
            argv = self.build_docker_run_argv(
                lane,
                agent_id=launched.agent_id,
                worktree=paths.worktree,
                brief_file=paths.brief,
                evidence_dir=paths.evidence,
            )
        except Exception:
            _remove(paths.lane_dir)
            raise
        try:
            proc = self._docker(*argv, timeout=RUN_WITH_PULL_TIMEOUT_SECONDS)
        except RunnerOutage:
            self._undo_failed_run(lane, paths, launched)
            raise
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            self._undo_failed_run(lane, paths, launched)
            if _is_daemon_outage(err):
                raise RunnerOutage(f"docker daemon unavailable: {err[:160]}")
            raise LaunchFailed(f"docker run failed: {err[:200] or f'exit {proc.returncode}'}")
        self._mark(paths, lane, launched, "running")

    def _undo_failed_run(self, lane: Lane, paths: _LanePaths, launched: Launched) -> None:
        try:
            self._rm_container(launched.agent_id)
        except LaunchError:
            self._mark(paths, lane, launched, "uncertain")
            raise RunnerOutage(f"docker run outcome unknown; {launched.agent_id} kept for a later sweep") from None
        _remove(paths.lane_dir)

    def _sweep_uncertain(self, work_root: Path) -> None:
        for marker in work_root.glob("*/launched.json"):
            lane_dir = marker.parent
            if (
                lane_dir.is_symlink()
                or not _SAFE_AGENT_ID.fullmatch(lane_dir.name)
                or _read_marker(marker).get("state") != "uncertain"
            ):
                continue
            try:
                self._rm_container(lane_dir.name)
            except LaunchError:
                continue
            _remove(lane_dir)

    def _mark(self, paths: _LanePaths, lane: Lane, launched: Launched, state: str) -> None:
        marker = {"lane_id": lane.lane_id, "agent_id": launched.agent_id, "url": launched.url, "state": state}
        paths.marker.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    def status(self, agent_id: str) -> AgentStatus:
        running = self._inspect_running(agent_id)
        return "running" if running is True else "ended"

    def stop(self, agent_id: str) -> None:
        lane_dir = self._lane_paths(agent_id).lane_dir
        self._rm_container(agent_id)
        _remove(lane_dir)

    def describe(self, agent_id: str) -> AgentInfo:
        version = "unknown"
        try:
            version = self._check_daemon()
        except RunnerOutage:
            pass
        machine = f"local-docker-{self.config.cpus}cpu-{self.config.memory}"
        return AgentInfo(machine=machine, version=f"docker-{version}")


register(RUNNER_NAME, LocalDockerRunner)
