"""`orch qa smoke`: a synthetic readiness check of a QA runner and evidence root (#1591).

It posts no GitHub comment, signs no storage link and launches no lane."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lib import qa_evidence, qa_runners
from lib.orch_yml import OrchConfig, QaStorage
from lib.qa_storage import RESULT_NAME


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"{'ok' if self.ok else 'fail'} {self.name}: {self.detail}"


@dataclass(frozen=True)
class _Target:
    config: OrchConfig
    repo: str
    runner: str

    def project(self) -> tuple[str, QaStorage]:
        for project in self.config.projects:
            if project.repo.lower() == self.repo.lower():
                if project.qa_storage is None:
                    raise ValueError(f"{project.repo} has no qa_storage entry in orch.yml")
                return project.repo, project.qa_storage
        raise ValueError(f"{self.repo} is not in orch.yml projects")


def _project(target: _Target) -> str:
    return str(target.project()[1].evidence_root)


def _runner(target: _Target) -> str:
    try:
        version = qa_runners.create(target.runner).ping()
    except (KeyError, qa_runners.LaunchError) as exc:
        raise ValueError(str(exc.args[0]) if exc.args else type(exc).__name__) from None
    return f"{target.runner} {version}"


def _evidence_root(target: _Target) -> str:
    root = target.project()[1].evidence_root
    if not root.exists():
        raise ValueError(f"{root} does not exist")
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")
    return str(root)


def _evidence_write(target: _Target) -> str:
    repo, storage = target.project()
    root = storage.evidence_root
    run_id = f"smoke-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{os.getpid()}"
    lane = qa_evidence.lane_dir(root, repo, "smoke", run_id, 1)
    top = _first_missing(root, lane)
    payload = json.dumps({"smoke": True, "run_id": run_id}).encode()
    result = lane / RESULT_NAME
    try:
        lane.mkdir(parents=True)
        result.write_bytes(payload)
        if result.read_bytes() != payload:
            raise ValueError(f"{result} read back different bytes")
    finally:
        if top.exists():
            shutil.rmtree(top)
    return str(result)


def _first_missing(root: Path, lane: Path) -> Path:
    path = root
    for part in lane.relative_to(root).parts:
        path = path / part
        if not path.exists():
            return path
    raise ValueError(f"{lane} already exists")


_CHECKS: tuple[tuple[str, str | None, Callable[[_Target], str]], ...] = (
    ("project", None, _project),
    ("runner", None, _runner),
    ("evidence root", "project", _evidence_root),
    ("evidence write", "evidence root", _evidence_write),
)


def run(config: OrchConfig, repo: str, runner: str) -> tuple[Check, ...]:
    target = _Target(config, repo, runner)
    done: dict[str, Check] = {}
    for name, prerequisite, check in _CHECKS:
        if prerequisite is not None and not done[prerequisite].ok:
            done[name] = Check(name, False, f"skipped ({prerequisite} failed)")
            continue
        try:
            done[name] = Check(name, True, check(target))
        except ValueError as exc:
            done[name] = Check(name, False, str(exc))
        except OSError as exc:
            where = f": {exc.filename}" if exc.filename else ""
            done[name] = Check(name, False, f"{exc.strerror or exc}{where}")
    return tuple(done.values())
