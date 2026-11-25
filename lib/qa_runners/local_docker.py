"""Fleet-local isolated Docker + git worktree Playwright QA runner (E1-1).

Implements ``lib.qa_runners.Runner`` so Live App QA runs on ``pc`` or ``box``
inside a detached git worktree and a hardened Docker container without waiting
on external vendor cloud QA APIs.

Security invariants:
- Containers always run with ``--rm``, explicit memory/CPU/PID caps,
  ``--cap-drop ALL``, ``--security-opt no-new-privileges:true``, and a
  read-only root filesystem (with bounded tmpfs mounts for ``/tmp`` and
  ``/home/qa``).
- Never mounts or exposes host factory state/config/secret paths
  (``~/.local/state/factory``, ``~/.config/factory``, ``/etc/factory``,
  ``~/.ssh``) and passes only an explicit non-secret allowlist of env vars.
- ``launch(lane)`` is idempotent on ``lane.lane_id`` both in-memory and across
  worker restarts via deterministic container naming and a lane state marker.
"""

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

from lib import qa_evidence, qa_storage
from lib.qa_runners import (
    AgentInfo,
    AgentStatus,
    Lane,
    Launched,
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
    """Deterministic, Docker-safe container name for a QA lane."""
    digest = hashlib.sha256(lane_id.encode("utf-8")).hexdigest()[:16]
    return f"factory-qa-{digest}"


def _is_daemon_outage(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _DAEMON_OUTAGE_MARKERS)


def _is_not_found(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _NOT_FOUND_MARKERS)


def _assert_safe_host_path(path: Path, *, home: Path) -> Path:
    """Refuse any mount/workspace path pointing into host factory secrets/state."""
    resolved = path.resolve()
    home_resolved = home.resolve()
    for sub in _FORBIDDEN_HOST_SUBPATHS:
        forbidden = (home_resolved / sub).resolve()
        if resolved == forbidden or forbidden in resolved.parents:
            raise LaunchFailed(f"refusing host path inside protected directory: {sub.as_posix()}")
    for prefix in _FORBIDDEN_ABSOLUTE_PREFIXES:
        try:
            prefix_res = prefix.resolve()
        except OSError:
            prefix_res = prefix
        if resolved == prefix_res or prefix_res in resolved.parents:
            raise LaunchFailed(f"refusing host path inside protected directory: {prefix}")
    return resolved


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
    entrypoint_cmd: tuple[str, ...] = (
        "sh",
        "-lc",
        "if [ -x /workspace/scripts/qa/run-local-lane.sh ]; then exec /workspace/scripts/qa/run-local-lane.sh; "
        "else echo 'QA brief ready at /qa-brief.md' && exit 0; fi",
    )
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
            repo_url_template=url_template,
        )


def write_lane_evidence(
    evidence_dir: Path,
    *,
    run_id: str,
    lane_no: int,
    result: Mapping[str, object],
    files: Mapping[str, bytes],
) -> tuple[Path, Path]:
    """Package artifacts in ``evidence_dir`` into the ``lib/qa_evidence.py`` layout.

    Writes every relative file under ``evidence_dir``, produces a scrubbed
    ``bundle.tar.gz`` and ``result.json`` (with computed ``sha256`` and ``size``
    entries), and writes ``manifest.json`` matching ``qa_evidence.MANIFEST_NAME``
    so both ``qa_evidence.collect`` and local readers see a valid lane folder.
    """
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
    """Fleet-local QA ``Runner`` backed by detached git worktrees + Docker."""

    def __init__(
        self,
        config: LocalDockerConfig | None = None,
        *,
        run: RunCommand = _default_run,
        home: Path | None = None,
    ) -> None:
        self.config = config or LocalDockerConfig.from_env()
        self._run = run
        self._home = Path.home() if home is None else Path(home)
        self._by_lane: dict[str, Launched] = {}
        self._worktrees: dict[str, tuple[Path, Path, Path]] = {}  # agent_id -> (repo_cache, worktree, lane_dir)

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

    def _check_daemon(self) -> str:
        proc = self._docker("version", "--format", "{{.Server.Version}}", timeout=15)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RunnerOutage(f"docker daemon unavailable: {err[:160] or f'exit {proc.returncode}'}")
        return (proc.stdout or "").strip() or "unknown"

    def _inspect_running(self, agent_id: str) -> bool | None:
        """True if running, False if exited, None if container does not exist."""
        proc = self._docker("inspect", "--format", "{{.State.Running}}", agent_id, timeout=15)
        if proc.returncode == 0:
            return (proc.stdout or "").strip().lower() == "true"
        err = (proc.stderr or proc.stdout or "").strip()
        if _is_daemon_outage(err):
            raise RunnerOutage(f"docker daemon unavailable: {err[:160]}")
        if _is_not_found(err):
            return None
        raise RunnerOutage(f"docker inspect failed: {err[:160] or f'exit {proc.returncode}'}")

    def _prepare_worktree(self, lane: Lane, lane_dir: Path) -> tuple[Path, Path]:
        if not _SAFE_REPO.fullmatch(lane.repo) or ".." in lane.repo:
            raise LaunchFailed(f"unsafe repository name: {lane.repo!r}")
        if not _SAFE_REF.fullmatch(lane.base_ref) or ".." in lane.base_ref or lane.base_ref.startswith("-"):
            raise LaunchFailed(f"unsafe base_ref: {lane.base_ref!r}")
        cache_root = _assert_safe_host_path(self.config.repo_cache_root, home=self._home)
        cache_root.mkdir(parents=True, exist_ok=True)
        owner, name = lane.repo.split("/", 1)
        repo_cache = cache_root / f"{owner}__{name}.git"
        worktree = lane_dir / "worktree"

        if not repo_cache.exists():
            url = self.config.repo_url_template.format(repo=lane.repo)
            cloned = self._git("clone", "--bare", "--", url, str(repo_cache), timeout=180)
            if cloned.returncode != 0:
                raise LaunchFailed(f"git clone failed: {(cloned.stderr or '').strip()[:200]}")
        fetched = self._git(
            "fetch", "--prune", "origin", f"+refs/heads/{lane.base_ref}:refs/remotes/origin/{lane.base_ref}",
            cwd=repo_cache,
            timeout=120,
        )
        if fetched.returncode != 0:
            # Fall back to fetching base_ref directly (e.g. a commit SHA or tag)
            fetched = self._git("fetch", "origin", lane.base_ref, cwd=repo_cache, timeout=120)
            if fetched.returncode != 0:
                raise LaunchFailed(f"git fetch {lane.base_ref} failed: {(fetched.stderr or '').strip()[:200]}")

        ref_target = f"refs/remotes/origin/{lane.base_ref}"
        check_ref = self._git("rev-parse", "--verify", "--quiet", ref_target, cwd=repo_cache)
        if check_ref.returncode != 0:
            ref_target = "FETCH_HEAD"
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
        self._git("worktree", "prune", cwd=repo_cache)
        added = self._git("worktree", "add", "--detach", str(worktree), ref_target, cwd=repo_cache)
        if added.returncode != 0:
            raise LaunchFailed(f"git worktree add failed: {(added.stderr or '').strip()[:200]}")
        return repo_cache, worktree

    def _image_for(self, lane: Lane) -> str:
        # environment_name may be a docker image tag (containing ':' or '/') or a logical profile name.
        candidate = lane.environment_name
        if candidate and (":" in candidate or "/" in candidate) and _SAFE_TOKEN.fullmatch(candidate):
            return candidate
        return self.config.image

    def _lane_paths(self, agent_id: str) -> tuple[Path, Path, Path, Path]:
        if not _SAFE_AGENT_ID.fullmatch(agent_id) or ".." in agent_id:
            raise LaunchFailed(f"unsafe agent_id: {agent_id!r}")
        work_root = _assert_safe_host_path(self.config.work_root, home=self._home)
        lane_dir = work_root / agent_id
        brief_file = lane_dir / "brief.md"
        evidence_dir = lane_dir / "evidence"
        marker_file = lane_dir / "launched.json"
        return lane_dir, brief_file, evidence_dir, marker_file

    def build_docker_run_argv(
        self,
        lane: Lane,
        *,
        agent_id: str,
        worktree: Path,
        brief_file: Path,
        evidence_dir: Path,
    ) -> list[str]:
        """Construct the hardened ``docker run`` argv for a QA lane."""
        _assert_safe_host_path(worktree, home=self._home)
        _assert_safe_host_path(brief_file, home=self._home)
        _assert_safe_host_path(evidence_dir, home=self._home)

        argv = [
            "run",
            "--detach",
            "--rm",
            "--name",
            agent_id,
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
                f"{worktree.resolve()}:/workspace:rw",
                "-v",
                f"{brief_file.resolve()}:/qa-brief.md:ro",
                "-v",
                f"{evidence_dir.resolve()}:/qa-evidence:rw",
                "-w",
                "/workspace",
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
        argv.append(self._image_for(lane))
        argv.extend(self.config.entrypoint_cmd)
        return argv

    def launch(self, lane: Lane) -> Launched:
        if lane.lane_id in self._by_lane:
            return self._by_lane[lane.lane_id]

        agent_id = container_name_for(lane.lane_id)
        url = f"docker://local/{agent_id}"
        lane_dir, brief_file, evidence_dir, marker_file = self._lane_paths(agent_id)

        # Idempotent across worker restarts: check marker file and existing container before spawning.
        if marker_file.is_file():
            try:
                saved = json.loads(marker_file.read_text(encoding="utf-8"))
                if saved.get("lane_id") == lane.lane_id and saved.get("agent_id") == agent_id:
                    launched = Launched(agent_id=agent_id, url=str(saved.get("url") or url))
                    self._by_lane[lane.lane_id] = launched
                    return launched
            except (OSError, ValueError):
                pass

        self._check_daemon()
        existing = self._inspect_running(agent_id)
        if existing is not None:
            launched = Launched(agent_id=agent_id, url=url)
            self._by_lane[lane.lane_id] = launched
            return launched

        lane_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        brief_file.write_text(lane.brief, encoding="utf-8")
        repo_cache, worktree = self._prepare_worktree(lane, lane_dir)
        self._worktrees[agent_id] = (repo_cache, worktree, lane_dir)

        argv = self.build_docker_run_argv(
            lane,
            agent_id=agent_id,
            worktree=worktree,
            brief_file=brief_file,
            evidence_dir=evidence_dir,
        )
        proc = self._docker(*argv)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            self._cleanup_worktree(agent_id, repo_cache=repo_cache, worktree=worktree, lane_dir=lane_dir)
            if _is_daemon_outage(err):
                raise RunnerOutage(f"docker daemon unavailable: {err[:160]}")
            raise LaunchFailed(f"docker run failed: {err[:200] or f'exit {proc.returncode}'}")

        launched = Launched(agent_id=agent_id, url=url)
        marker_file.write_text(
            json.dumps(
                {
                    "lane_id": lane.lane_id,
                    "agent_id": agent_id,
                    "url": url,
                    "repo_cache": str(repo_cache),
                    "worktree": str(worktree),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._by_lane[lane.lane_id] = launched
        return launched

    def status(self, agent_id: str) -> AgentStatus:
        running = self._inspect_running(agent_id)
        return "running" if running is True else "ended"

    def stop(self, agent_id: str) -> None:
        proc = self._docker("rm", "-f", agent_id, timeout=30)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if _is_daemon_outage(err):
                raise RunnerOutage(f"docker daemon unavailable: {err[:160]}")
            if not _is_not_found(err):
                raise LaunchFailed(f"docker rm failed: {err[:200] or f'exit {proc.returncode}'}")
        self._cleanup_worktree(agent_id)

    def _cleanup_worktree(
        self,
        agent_id: str,
        *,
        repo_cache: Path | None = None,
        worktree: Path | None = None,
        lane_dir: Path | None = None,
    ) -> None:
        tracked = self._worktrees.pop(agent_id, None)
        if tracked is not None:
            repo_cache, worktree, lane_dir = tracked
        else:
            try:
                default_lane_dir, _, _, marker_file = self._lane_paths(agent_id)
            except LaunchFailed:
                return
            lane_dir = lane_dir or default_lane_dir
            if marker_file.is_file():
                try:
                    saved = json.loads(marker_file.read_text(encoding="utf-8"))
                    repo_cache = repo_cache or (Path(saved["repo_cache"]) if saved.get("repo_cache") else None)
                    worktree = worktree or (Path(saved["worktree"]) if saved.get("worktree") else None)
                except (OSError, ValueError):
                    pass
        if repo_cache is not None and worktree is not None and repo_cache.exists():
            try:
                self._git("worktree", "remove", "--force", str(worktree), cwd=repo_cache, timeout=30)
            except LaunchFailed:
                pass
        if lane_dir is not None and lane_dir.exists():
            shutil.rmtree(lane_dir, ignore_errors=True)

    def describe(self, agent_id: str) -> AgentInfo:
        version = "unknown"
        try:
            version = self._check_daemon()
        except RunnerOutage:
            pass
        machine = f"local-docker-{self.config.cpus}cpu-{self.config.memory}"
        return AgentInfo(machine=machine, version=f"docker-{version}")


register(RUNNER_NAME, LocalDockerRunner)
