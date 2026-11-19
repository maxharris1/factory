"""Today's default-account job scripts, pinned byte for byte."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from lib import remote, skill_probe, usage_probe
from lib.harness_env import harness_env

GOLDEN = Path(__file__).parent / "fixtures" / "account_env"
TARGET = remote.RemoteTarget(
    device="pc",
    alias="pc-wsl-claude-bot",
    user="claude-bot",
    workdir_root="/home/claude-bot/factory-work",
    job_id="job-1",
    tag="0123456789abcdef0123456789abcdef",
)
ENV = {"ORCH_JOB_ID": "job-1", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}
SOURCE_MARK = "<codex_rollout source>"


def _codex_probe() -> str:
    script = usage_probe.codex_remote_script("thread-1")
    source = Path(usage_probe.codex_rollout.__file__).read_text(encoding="utf-8")
    return script.replace(shlex.quote(source), SOURCE_MARK)


CASES = {
    "start_plain": lambda: remote.start_script(TARGET, ["claude", "-p"], ENV),
    "start_guarded_report": lambda: remote.start_script(
        TARGET,
        ["claude", "-p", "--verbose"],
        ENV,
        guarded_dirs=("/home/claude-bot/factory/skills", "docs"),
        insert_at=2,
        cd="repo",
        report_env=True,
        env_file="/home/claude-bot/factory-work/jobs/job-1.run/env",
    ),
    "usage_probe_claude": usage_probe.claude_remote_script,
    "usage_probe_codex": _codex_probe,
    "fetch_claude": lambda: skill_probe.fetch_script(
        "claude", "/home/claude-bot/factory-work/jobs/job-1/repo"
    ),
    "fetch_codex": lambda: skill_probe.fetch_script(
        "codex", "/home/codex-bot/factory-work/jobs/job-1/repo"
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_default_account_script_is_unchanged(name: str) -> None:
    path = GOLDEN / f"{name}.txt"
    assert CASES[name]() == path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("harness", "expected"),
    [
        ("claude", {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}),
        ("codex", {}),
        ("cursor", {}),
    ],
)
def test_default_account_harness_env_is_unchanged(harness: str, expected: dict) -> None:
    assert harness_env(harness) == expected
