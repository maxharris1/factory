"""orch.yml schema (docs/SPEC.md section 7).

Required: poll_seconds (the decided interval, 60) and projects[].repo.
job_timeout is optional and measured in minutes. When the file and the
project both omit it, the timeout is 90 minutes. Unknown fields are
refused so v1 stays closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from lib.load import Finding, is_strict_int, load_yaml

POLL_SECONDS = 60
DEFAULT_JOB_TIMEOUT_MINUTES = 90

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TOP_KEYS = {"poll_seconds", "job_timeout", "projects"}
_PROJECT_KEYS = {"repo", "job_timeout"}


@dataclass(frozen=True)
class Project:
    repo: str
    job_timeout: int | None = None


@dataclass(frozen=True)
class OrchConfig:
    poll_seconds: int
    projects: tuple[Project, ...]
    job_timeout: int | None = None


def resolve_job_timeout(config: OrchConfig, repo: str) -> int:
    """Project override, then the file override, then 90 minutes."""
    project = next((item for item in config.projects if item.repo == repo), None)
    if project is None:
        raise KeyError(repo)
    if project.job_timeout is not None:
        return project.job_timeout
    if config.job_timeout is not None:
        return config.job_timeout
    return DEFAULT_JOB_TIMEOUT_MINUTES


def load_orch_yml(path: Path) -> tuple[OrchConfig | None, list[Finding]]:
    if not path.is_file():
        return None, [Finding("error", "file not found")]
    text = path.read_text(encoding="utf-8")
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        text = " ".join(line.strip() for line in str(exc).splitlines() if line.strip())
        return None, [Finding("error", f"invalid YAML: {text or 'invalid YAML'}")]
    if not isinstance(data, dict):
        return None, [Finding("error", "orch.yml must be a mapping")]
    return _validate(data)


def _validate(data: dict) -> tuple[OrchConfig | None, list[Finding]]:
    findings: list[Finding] = []
    for key in sorted(data, key=lambda item: str(item)):
        if key not in _TOP_KEYS:
            findings.append(Finding("error", f"unknown field {str(key)!r}"))

    poll_seconds = _poll_seconds(data, findings)
    job_timeout = _timeout(data.get("job_timeout", None), "job_timeout", "job_timeout" in data, findings)
    projects = _projects(data, findings)

    if any(item.severity == "error" for item in findings):
        return None, findings
    assert poll_seconds is not None
    assert projects is not None
    return (
        OrchConfig(
            poll_seconds=poll_seconds,
            projects=projects,
            job_timeout=job_timeout,
        ),
        findings,
    )


def _poll_seconds(data: dict, findings: list[Finding]) -> int | None:
    if "poll_seconds" not in data:
        findings.append(Finding("error", "missing required field 'poll_seconds'"))
        return None
    value = data["poll_seconds"]
    if value != POLL_SECONDS:
        findings.append(Finding("error", "poll_seconds must be 60"))
        return None
    return POLL_SECONDS


def _timeout(
    value: object,
    field: str,
    present: bool,
    findings: list[Finding],
) -> int | None:
    if not present:
        return None
    if not is_strict_int(value) or value < 1:
        findings.append(
            Finding("error", f"{field} must be a positive integer (minutes)")
        )
        return None
    return int(value)


def _projects(data: dict, findings: list[Finding]) -> tuple[Project, ...] | None:
    if "projects" not in data:
        findings.append(Finding("error", "missing required field 'projects'"))
        return None
    value = data["projects"]
    if not isinstance(value, list) or not value:
        findings.append(Finding("error", "projects must list at least one repo"))
        return None
    projects: list[Project] = []
    seen: set[str] = set()
    ok = True
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            findings.append(Finding("error", f"projects[{index}] must be a mapping"))
            ok = False
            continue
        for key in sorted(item, key=lambda entry: str(entry)):
            if key not in _PROJECT_KEYS:
                findings.append(
                    Finding("error", f"unknown field 'projects[{index}].{key}'")
                )
                ok = False
        repo = item.get("repo")
        if not isinstance(repo, str) or _REPO.fullmatch(repo) is None:
            findings.append(Finding("error", f"projects[{index}].repo must be owner/name"))
            ok = False
            continue
        if repo in seen:
            findings.append(Finding("error", f"duplicate project {repo!r}"))
            ok = False
        seen.add(repo)
        timeout = _timeout(
            item.get("job_timeout"),
            f"projects[{index}].job_timeout",
            "job_timeout" in item,
            findings,
        )
        if "job_timeout" in item and timeout is None:
            ok = False
            continue
        projects.append(Project(repo=repo, job_timeout=timeout))
    if not ok:
        return None
    return tuple(projects)
