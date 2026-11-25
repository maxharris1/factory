"""What a job ran under, recorded at its start (F-01a)."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from lib import exposure, job_run, telemetry
from lib.brief import render_brief
from lib.brief.skills import Router, pinned_paths, resolve, section
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import Exchanged, RemoteTarget, ssh_argv
from tests.regenerate_brief_snapshots import HEAD as SNAPSHOT_HEAD
from tests.regenerate_brief_snapshots import _job as _snapshot_job
from tests.regenerate_brief_snapshots import ci_bundle
from tests.test_brief import job

FACTORY_ROOT = "/home/claude-bot/factory"
SHA = "0123456789abcdef0123456789abcdef01234567"
EXPECTED_COLUMNS = [
    "job_id", "model", "cli_version", "brief_sha256", "skill_paths", "host",
    "factory_revision", "recorded_at", "effort", "harness",
]


def _router(role: str, bot: str, action: str = "build") -> Router:
    router = resolve(role, bot, FACTORY_ROOT, action=action)
    assert isinstance(router, Router), router
    return router


def _shell(replies: dict[str, str | None]) -> exposure.Shell:
    def shell(script: str, args: Sequence[str]) -> str | None:
        return next((reply for needle, reply in replies.items() if needle in script), None)

    return shell


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _exposure(**changes: object) -> exposure.Exposure:
    fields: dict[str, object] = {
        "model": "gpt-5.5",
        "cli_version": "codex-cli 1.2.3",
        "brief_sha256": hashlib.sha256(b"brief").hexdigest(),
        "skill_paths": ("/f/a/SKILL.md", "/f/b/SKILL.md"),
        "host": "box",
        "factory_revision": SHA,
        "effort": "high",
        "harness": "codex",
    }
    fields.update(changes)
    return exposure.Exposure(**fields)  # type: ignore[arg-type]


# --- pinned_paths is section's inverse -----------------------------------------


@pytest.mark.parametrize(
    ("role", "bot"),
    [("builder", "claude-bot"), ("builder", "codex-bot"), ("reviewer", "claude-bot"), ("reviewer", "codex-bot")],
)
def test_pinned_paths_reads_back_the_paths_section_wrote(role: str, bot: str) -> None:
    router = _router(role, bot)
    assert pinned_paths(section(router)) == router.paths


@pytest.mark.parametrize(
    ("action", "recipient", "role", "extra"),
    [
        ("plan", "claude", "builder", {}),
        ("build", "codex", "builder", {}),
        ("fix", "claude", "builder", {"instruction": "https://github.com/owner/repo/pull/42#issuecomment-7"}),
        ("review", "codex", "reviewer", {"round": "2/3"}),
    ],
)
def test_pinned_paths_reads_the_router_paths_from_a_full_rendered_brief(
    action: str, recipient: str, role: str, extra: dict
) -> None:
    brief = render_brief(
        job(action, recipient, **extra), bot=f"{recipient}-bot", context=None, factory_root=FACTORY_ROOT
    )
    assert pinned_paths(brief) == _router(role, f"{recipient}-bot", action).paths


def test_exposure_records_the_router_paths_when_a_quoted_ci_log_holds_a_skills_section() -> None:
    log = (
        "2026-11-19T12:00:00Z ## Skills\n"
        "2026-11-19T12:00:01Z - `/tmp/log-only/SKILL.md`\n"
        "2026-11-19T12:00:02Z ## Result\n"
        "2026-11-19T12:00:03Z FAILED tests/test_docs.py::test_docs"
    )
    ci_job = _snapshot_job("fix", "claude", 1, SNAPSHOT_HEAD, {"evidence": ci_bundle({"31000000004": log})})
    brief = render_brief(ci_job, bot="claude-bot", context=None, factory_root=FACTORY_ROOT)
    assert "\n## Skills\n- `/tmp/log-only/SKILL.md`\n" in brief
    found = exposure.gather(
        argv=["claude", "-p"], binary="claude", harness="claude", brief=brief.encode(), host="box",
        factory_root=FACTORY_ROOT, shell=_shell({}), launch_dir=None,
    )
    assert found.skill_paths == _router("builder", "claude-bot", "fix").paths


def test_pinned_paths_skips_skills_headings_and_section_ends_inside_fences() -> None:
    brief = (
        "## Method\n````\n## Skills\n- `/quoted/SKILL.md`\n````\n\n"
        "## Skills\n- `/a/SKILL.md`\n```\n## Not a section\n```\n- `/b/SKILL.md`\n\n"
        "## Job\n- `/c/SKILL.md`\n"
    )
    assert pinned_paths(brief) == ("/a/SKILL.md", "/b/SKILL.md")
    assert pinned_paths("## Method\n`````ticket\n## Skills\n- `/quoted/SKILL.md`\n`````\n") is None


def test_pinned_paths_is_none_without_a_skills_section_or_any_path() -> None:
    assert pinned_paths("# Brief\n\n## Task\n- `/not/a/skill.md`\n") is None
    assert pinned_paths("## Skills\nno paths here\n## Task\n") is None


def test_pinned_paths_stops_at_the_next_section() -> None:
    brief = "## Skills\n- `/a/SKILL.md`\n\n## Task\n- `/b/SKILL.md`\n"
    assert pinned_paths(brief) == ("/a/SKILL.md",)


# --- the model -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "harness", "expected"),
    [
        (["claude", "-p", "--model", "claude-opus-5-5", "--verbose"], "claude", "claude-opus-5-5"),
        (["claude", "-p", "--model=opus"], "claude", "opus"),
        (["codex", "exec", "-m", "gpt-5.5", "--json"], "codex", "gpt-5.5"),
        (["codex", "exec", "-c", 'model="gpt-5.5"'], "codex", "gpt-5.5"),
        (["codex", "exec", "--config", "model=gpt-5.4"], "codex", "gpt-5.4"),
        (["codex", "exec", "--config=model='o3'"], "codex", "o3"),
        (["codex", "exec", "-c", 'model="a"', "-m", "b"], "codex", "b"),
    ],
)
def test_the_model_comes_from_the_command_line(argv: list[str], harness: str, expected: str) -> None:
    shell = _shell({"config.toml": 'model = "from-config"\n'})
    assert exposure.chosen(argv, harness, shell).model == expected


@pytest.mark.parametrize(
    ("argv", "harness", "expected"),
    [
        (["claude", "-p", "--effort", "high", "--verbose"], "claude", "high"),
        (["claude", "-p", "--effort=low"], "claude", "low"),
        (["claude", "-p", "--", "--effort", "high"], "claude", exposure.UNKNOWN),
        (["claude", "-p"], "claude", exposure.UNKNOWN),
        (["codex", "exec", "-c", "model_reasoning_effort=xhigh"], "codex", "xhigh"),
        (["codex", "exec", "--config", 'model_reasoning_effort="medium"'], "codex", "medium"),
        (["codex", "exec", "--effort", "high"], "codex", exposure.UNKNOWN),
        (["claude", "-p", "--effort", "$(rm -rf /)"], "claude", exposure.UNKNOWN),
    ],
)
def test_the_effort_comes_from_the_command_line(argv: list[str], harness: str, expected: str) -> None:
    assert exposure.chosen(argv, harness, _shell({})).effort == expected


def test_options_after_a_double_dash_are_not_the_harnesses() -> None:
    shell = _shell({})
    assert exposure.chosen(["claude", "-p", "--", "--model", "x"], "claude", shell).model == exposure.UNKNOWN


def test_a_codex_job_without_a_model_flag_reads_its_config_toml(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5.5-codex"\n[profiles.x]\nmodel = "other"\n', encoding="utf-8")
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home)})
    assert exposure.chosen(["codex", "exec", "--json"], "codex", shell, str(tmp_path)).model == "gpt-5.5-codex"


def test_no_model_anywhere_is_unknown(tmp_path: Path) -> None:
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(tmp_path / "none")})
    assert exposure.chosen(["codex", "exec"], "codex", shell).model == exposure.UNKNOWN
    assert exposure.chosen(["claude", "-p"], "claude", _shell({"config.toml": 'model = "x"'})).model == exposure.UNKNOWN
    assert exposure.chosen(["codex", "exec"], "codex", _shell({"config.toml": "model = 3"})).model == exposure.UNKNOWN
    assert exposure.chosen(["codex", "exec"], "codex", _shell({"config.toml": "not toml ["})).model == exposure.UNKNOWN


BASE = 'model = "default-model"\n'
REVIEW = 'model = "review-model"\n'


def _codex_home(tmp_path: Path, files: dict[str, str]) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    for name, text in files.items():
        (home / name).write_text(text, encoding="utf-8")
    return home


@pytest.mark.parametrize(
    ("argv", "files", "expected"),
    [
        (["codex", "--profile", "review", "exec"], {"config.toml": BASE, "review.config.toml": REVIEW}, "review-model"),
        (["codex", "exec", "--profile=review"], {"config.toml": BASE, "review.config.toml": REVIEW}, "review-model"),
        (["codex", "-p", "review", "exec"], {"config.toml": BASE, "review.config.toml": REVIEW}, "review-model"),
        (["codex", "-preview", "exec"], {"config.toml": BASE, "review.config.toml": REVIEW}, "review-model"),
        (["codex", "-p", "review", "exec"], {"review.config.toml": REVIEW}, "review-model"),
        (["codex", "-p", "review", "exec", "-m", "flag"], {"config.toml": BASE, "review.config.toml": REVIEW}, "flag"),
        (["codex", "-p", "review", "exec"], {"config.toml": BASE, "review.config.toml": "approval_policy = 'never'\n"},
         "default-model"),
        (["codex", "-p", "review", "exec"], {"config.toml": BASE}, exposure.UNKNOWN),
        (["codex", "-p", "review", "exec"], {"config.toml": BASE, "review.config.toml": "model = ["}, exposure.UNKNOWN),
        (["codex", "-p", "review", "exec"], {"config.toml": BASE, "review.config.toml": "model = 3"}, exposure.UNKNOWN),
        (["codex", "-p", "../review", "exec"], {"config.toml": BASE, "review.config.toml": REVIEW}, exposure.UNKNOWN),
        (["codex", "exec"], {"config.toml": 'profile = "review"\n' + BASE, "review.config.toml": REVIEW},
         exposure.UNKNOWN),
        (["codex", "exec", "-c", "profile=review"], {"config.toml": BASE, "review.config.toml": REVIEW},
         exposure.UNKNOWN),
        (["codex", "-p", "review", "exec"],
         {"config.toml": BASE + '[profiles.review]\nmodel = "old-review"\n', "review.config.toml": REVIEW},
         exposure.UNKNOWN),
        (["claude", "-p", "review"], {"config.toml": BASE, "review.config.toml": REVIEW}, exposure.UNKNOWN),
    ],
)
def test_a_codex_profile_selects_its_own_model_or_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], files: dict[str, str], expected: str
) -> None:
    home = _codex_home(tmp_path, files)
    monkeypatch.chdir(tmp_path)
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home)})
    assert exposure.chosen(argv, argv[0], shell).model == expected


def test_a_profile_file_that_cannot_be_read_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _codex_home(tmp_path, {"config.toml": BASE})
    (home / "review.config.toml").mkdir()
    monkeypatch.chdir(tmp_path)
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home)})
    assert exposure.chosen(["codex", "-p", "review", "exec"], "codex", shell).model == exposure.UNKNOWN


def test_a_project_codex_config_above_the_launch_directory_that_sets_the_model_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _codex_home(tmp_path, {"config.toml": BASE, "review.config.toml": REVIEW})
    project = tmp_path / "project"
    (project / "sub" / ".codex").mkdir(parents=True)
    (project / ".codex").mkdir()
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home)})
    monkeypatch.chdir(project / "sub")

    (project / ".codex" / "config.toml").write_text("approval_policy = 'never'\n", encoding="utf-8")
    assert exposure.chosen(["codex", "exec"], "codex", shell).model == "default-model"
    (project / ".codex" / "config.toml").write_text('model = "project-model"\n', encoding="utf-8")
    assert exposure.chosen(["codex", "exec"], "codex", shell).model == exposure.UNKNOWN
    assert exposure.chosen(["codex", "-p", "review", "exec"], "codex", shell).model == exposure.UNKNOWN
    assert exposure.chosen(["codex", "-m", "flag", "exec"], "codex", shell).model == "flag"

    (project / ".codex" / "config.toml").write_text("model = [", encoding="utf-8")
    assert exposure.chosen(["codex", "exec"], "codex", shell).model == exposure.UNKNOWN
    (project / ".codex" / "config.toml").unlink()
    (project / "sub" / ".codex" / "config.toml").write_text('model = "nested"\n', encoding="utf-8")
    monkeypatch.chdir(project)
    assert exposure.chosen(["codex", "exec"], "codex", shell).model == "default-model"
    assert exposure.chosen(["codex", "-C", "sub", "exec"], "codex", shell).model == exposure.UNKNOWN


def test_the_user_config_is_not_read_as_a_project_config_from_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(BASE, encoding="utf-8")
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert exposure.chosen(["codex", "exec"], "codex", shell).model == "default-model"


EFFORT = 'model_reasoning_effort = "high"\n'


@pytest.mark.parametrize(
    ("argv", "files", "expected"),
    [
        (["codex", "--profile", "review", "exec"], {"review.config.toml": 'model = "profile-model"\n' + EFFORT},
         ("profile-model", "high")),
        (["codex", "exec"], {"config.toml": BASE + EFFORT}, ("default-model", "high")),
        (["codex", "-p", "review", "exec"],
         {"config.toml": BASE + 'model_reasoning_effort = "low"\n', "review.config.toml": REVIEW},
         ("review-model", "low")),
        (["codex", "-p", "review", "exec"],
         {"config.toml": BASE + 'model_reasoning_effort = "low"\n', "review.config.toml": REVIEW + EFFORT},
         ("review-model", "high")),
        (["codex", "exec", "-m", "flag"], {"config.toml": BASE + EFFORT}, ("flag", "high")),
        (["codex", "exec", "-c", "model_reasoning_effort=xhigh"], {"config.toml": BASE + EFFORT},
         ("default-model", "xhigh")),
        (["codex", "exec"], {"config.toml": 'profile = "review"\n' + BASE + EFFORT, "review.config.toml": REVIEW},
         (exposure.UNKNOWN, exposure.UNKNOWN)),
        (["codex", "exec", "-c", "profile=review"], {"config.toml": BASE + EFFORT}, (exposure.UNKNOWN, exposure.UNKNOWN)),
        (["codex", "-p", "review", "exec"],
         {"config.toml": BASE + '[profiles.review]\nmodel_reasoning_effort = "low"\n',
          "review.config.toml": REVIEW + EFFORT},
         ("review-model", exposure.UNKNOWN)),
        (["codex", "exec"], {"config.toml": BASE + 'model_reasoning_effort = "$(rm -rf /)"\n'},
         ("default-model", exposure.UNKNOWN)),
        (["codex", "exec"], {"config.toml": BASE + "model_reasoning_effort = 3\n"}, ("default-model", exposure.UNKNOWN)),
    ],
)
def test_a_codex_profile_or_default_config_supplies_the_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], files: dict[str, str], expected: tuple
) -> None:
    home = _codex_home(tmp_path, files)
    monkeypatch.chdir(tmp_path)
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home)})
    found = exposure.chosen(argv, "codex", shell)
    assert (found.model, found.effort) == expected


def test_a_project_codex_config_that_sets_the_effort_makes_it_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _codex_home(tmp_path, {"config.toml": BASE + EFFORT})
    (tmp_path / "project" / ".codex").mkdir(parents=True)
    (tmp_path / "project" / ".codex" / "config.toml").write_text('model_reasoning_effort = "low"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path / "project")
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home)})
    found = exposure.chosen(["codex", "exec"], "codex", shell)
    assert (found.model, found.effort) == ("default-model", exposure.UNKNOWN)


def test_a_recorded_codex_profile_job_stores_the_profiles_model_and_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _codex_home(tmp_path, {"review.config.toml": 'model = "profile-model"\n' + EFFORT})
    monkeypatch.chdir(tmp_path)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    argv = ["codex", "--profile", "review", "exec"]
    exposure.record(queue.connect, "job-1", lambda: exposure.gather(
        argv=argv, binary="codex", harness="codex", brief=None, host="pc", factory_root=None,
        shell=exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home)}), launch_dir=None,
    ), lambda line: None)
    with closing(queue.connect()) as connection:
        row = connection.execute("SELECT model, effort FROM job_exposure WHERE job_id = 'job-1'").fetchone()
    assert tuple(row) == ("profile-model", "high")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["claude", "-p", "--settings", '{"model":"opus","effortLevel":"high"}'], ("opus", "high")),
        (["claude", "-p", '--settings={"effortLevel": "max"}'], (exposure.UNKNOWN, "max")),
        (["claude", "-p", "--settings", '{"model":"opus","effortLevel":"high"}', "--effort", "low", "--model", "x"],
         ("x", "low")),
        (["claude", "-p", "--effort", "low", "--settings", '{"effortLevel":"high"}'], (exposure.UNKNOWN, "low")),
        (["claude", "-p", "--settings", "/etc/claude/settings.json"], (exposure.UNKNOWN, exposure.UNKNOWN)),
        (["claude", "-p", "--settings", '{"effortLevel":'], (exposure.UNKNOWN, exposure.UNKNOWN)),
        (["claude", "-p", "--settings", '["high"]'], (exposure.UNKNOWN, exposure.UNKNOWN)),
        (["claude", "-p", "--settings", '{"effortLevel":"$(rm -rf /)","model":3}'], (exposure.UNKNOWN, exposure.UNKNOWN)),
        (["claude", "-p", "--", "--settings", '{"effortLevel":"high"}'], (exposure.UNKNOWN, exposure.UNKNOWN)),
    ],
)
def test_claudes_inline_settings_supply_the_model_and_effort(argv: list[str], expected: tuple) -> None:
    found = exposure.chosen(argv, "claude", _shell({}))
    assert (found.model, found.effort) == expected


@pytest.mark.parametrize(
    ("argv", "harness", "expected"),
    [
        (["/usr/bin/env", "--", "codex", "exec", "-m", "flag-model", "-c", "model_reasoning_effort=xhigh"], "codex",
         ("flag-model", "xhigh")),
        (["env", "-u", "OPENAI_API_KEY", "--unset=X", "-i", "FOO=1", "codex", "exec", "-m", "m", "-c",
          "model_reasoning_effort=low"], "codex", ("m", "low")),
        (["env", "--", "FOO=1", "claude", "-p", "--model", "opus", "--effort", "high"], "claude", ("opus", "high")),
        (["env", "-0", "--ignore-environment", "claude", "-p", "--effort=max", "--", "--model", "x"], "claude",
         (exposure.UNKNOWN, "max")),
        (["env", "-S", "codex exec -m m", "codex"], "codex", (exposure.UNKNOWN, exposure.UNKNOWN)),
        (["env", "FOO=1"], "codex", (exposure.UNKNOWN, exposure.UNKNOWN)),
    ],
)
def test_an_env_wrapper_passes_the_harness_its_own_arguments(
    tmp_path: Path, argv: list[str], harness: str, expected: tuple
) -> None:
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(tmp_path / "none")})
    found = exposure.chosen(argv, harness, shell)
    assert (found.model, found.effort) == expected


@pytest.mark.parametrize(
    "wrapper",
    [
        ["env", "CODEX_HOME=/elsewhere"],
        ["env", "-u", "CODEX_HOME"],
        ["env", "--unset=CODEX_HOME"],
        ["env", "HOME=/elsewhere"],
        ["env", "-i"],
        ["env", "-C", "/elsewhere"],
        ["env", "--chdir=/elsewhere"],
    ],
)
def test_an_env_wrapper_that_moves_the_codex_config_makes_the_config_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrapper: list[str]
) -> None:
    home = _codex_home(tmp_path, {"config.toml": BASE + EFFORT})
    monkeypatch.chdir(tmp_path)
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home), "HOME": str(tmp_path)})
    plain = exposure.chosen(["codex", "exec"], "codex", shell)
    wrapped = exposure.chosen([*wrapper, "codex", "exec"], "codex", shell)
    assert ((plain.model, plain.effort), (wrapped.model, wrapped.effort)) == (
        ("default-model", "high"), (exposure.UNKNOWN, exposure.UNKNOWN)
    )


# --- the CLI version -------------------------------------------------------------


def test_the_cli_version_is_the_first_line_of_version(tmp_path: Path) -> None:
    binary = _script(tmp_path, "fake", '[ "$1" = --version ] && printf "\\n  fake-cli 9.9.9  \\nmore\\n"')
    assert exposure.cli_version(str(binary), exposure.local_shell({"PATH": "/usr/bin:/bin"})) == "fake-cli 9.9.9"


def test_a_long_version_line_is_cut(tmp_path: Path) -> None:
    binary = _script(tmp_path, "fake", "printf 'v1.%0300d\\n' 0")
    assert exposure.cli_version(str(binary), exposure.local_shell({})) == "v1." + "0" * 197


def test_a_binary_that_ignores_version_is_unknown_not_its_output(tmp_path: Path) -> None:
    binary = _script(tmp_path, "fake", """echo '{"command": "deploy --pin s3cret 1.0"}'""")
    assert exposure.cli_version(str(binary), exposure.local_shell({})) == exposure.UNKNOWN


def test_a_version_that_hangs_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure, "TIMEOUT_SECONDS", 0.5)
    binary = _script(tmp_path, "fake", "echo started; exec sleep 30")
    started = time.monotonic()
    assert exposure.cli_version(str(binary), exposure.local_shell({"PATH": "/usr/bin:/bin"})) == exposure.UNKNOWN
    assert time.monotonic() - started < 5


def test_a_version_that_exits_non_zero_or_is_missing_is_unknown(tmp_path: Path) -> None:
    binary = _script(tmp_path, "fake", "echo fake-cli 1.0; exit 3")
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin"})
    assert exposure.cli_version(str(binary), shell) == exposure.UNKNOWN
    assert exposure.cli_version(str(tmp_path / "missing"), shell) == exposure.UNKNOWN


def _two_harnesses(tmp_path: Path) -> tuple[Path, Path]:
    waiter, job = tmp_path / "waiter", tmp_path / "job"
    for directory, version in ((waiter, "1.0.0"), (job, "2.0.0")):
        directory.mkdir()
        _script(directory, "harness", f"echo harness-cli {version}")
    return waiter, job


def test_a_relative_binary_is_versioned_in_the_launch_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiter, job = _two_harnesses(tmp_path)
    monkeypatch.chdir(waiter)
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin"})
    assert exposure.cli_version("./harness", shell, str(job)) == "harness-cli 2.0.0"
    assert exposure.cli_version("./harness", shell, None) == "harness-cli 1.0.0"
    assert exposure.cli_version("./harness", shell, str(tmp_path / "gone")) == exposure.UNKNOWN
    assert exposure.cli_version(str(waiter / "harness"), shell, str(tmp_path / "gone")) == "harness-cli 1.0.0"


@pytest.mark.parametrize("entry", [".", "", "bin"])
def test_a_bare_binary_on_a_relative_path_entry_is_the_one_the_job_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    waiter, job = tmp_path / "waiter", tmp_path / "job"
    for directory, version in ((waiter, "1.0.0"), (job, "2.0.0")):
        (directory / entry).mkdir(parents=True, exist_ok=True)
        _script(directory / entry, "harness", f"echo harness-cli {version}")
    env = {**os.environ, "PATH": f"{entry}:/usr/bin:/bin"}
    monkeypatch.chdir(waiter)
    recorded: list[str] = []
    launched: list[str] = []

    def setup() -> Path:
        recorded.append(exposure.cli_version("harness", exposure.local_shell(env), str(job)))
        return job

    code = job_run.run(["harness"], beat=lambda: None, interval=30, log=lambda _: None, env=env, setup=setup,
                       output=lambda stream: launched.append(stream.read().decode().strip()))

    assert (code, launched) == (0, ["harness-cli 2.0.0"])
    assert recorded == launched


def test_a_bare_binary_for_a_launch_directory_not_made_yet_skips_relative_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiter, _job = _two_harnesses(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    _script(tools, "harness", "echo harness-cli 3.0.0")
    monkeypatch.chdir(waiter)
    gone = str(tmp_path / "gone")
    assert exposure.cli_version("harness", exposure.local_shell({"PATH": f".::bin:{tools}"}), gone) == "harness-cli 3.0.0"
    assert exposure.cli_version("harness", exposure.local_shell({"PATH": ".:bin"}), gone) == exposure.UNKNOWN


def test_gather_reads_the_version_and_the_project_config_in_the_launch_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiter, job = _two_harnesses(tmp_path)
    (job / ".codex").mkdir()
    (job / ".codex" / "config.toml").write_text('model = "project-model"\n', encoding="utf-8")
    home = _codex_home(tmp_path, {"config.toml": BASE})
    monkeypatch.chdir(waiter)
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home)})

    def gather(launch_dir: str | None) -> exposure.Exposure:
        return exposure.gather(argv=["./harness", "exec"], binary="./harness", harness="codex", brief=None,
                               host="box", factory_root=None, shell=shell, launch_dir=launch_dir)

    assert gather(str(job)) == exposure.Exposure(
        exposure.UNKNOWN, "harness-cli 2.0.0", exposure.UNKNOWN, None, "box", exposure.UNKNOWN, exposure.UNKNOWN,
        "codex",
    )
    assert gather(None) == exposure.Exposure(
        "default-model", "harness-cli 1.0.0", exposure.UNKNOWN, None, "box", exposure.UNKNOWN, exposure.UNKNOWN,
        "codex",
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_remote_shell_reads_a_relative_binary_in_the_job_directory_it_may_not_have_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiter, job = _two_harnesses(tmp_path)
    monkeypatch.chdir(waiter)
    target = RemoteTarget("pc", "pc-codex-bot", "codex-bot", str(tmp_path / "work"), "job1")
    shell = exposure.remote_shell(_Transport(), target)
    assert exposure.cli_version("./harness", shell, target.job_dir) == exposure.UNKNOWN
    Path(target.job_dir).parent.mkdir(parents=True)
    job.rename(target.job_dir)
    assert exposure.cli_version("./harness", shell, target.job_dir) == "harness-cli 2.0.0"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_remote_shell_reads_a_bare_binary_on_the_login_path_in_the_job_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiter, job = _two_harnesses(tmp_path)
    monkeypatch.chdir(waiter)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_profile").write_text("PATH=.:/usr/bin:/bin\n", encoding="utf-8")
    target = RemoteTarget("pc", "pc-codex-bot", "codex-bot", str(tmp_path / "work"), "job1")
    Path(target.job_dir).parent.mkdir(parents=True)
    job.rename(target.job_dir)
    shell = exposure.remote_shell(_Transport(env={"HOME": str(home), "PATH": "/usr/bin:/bin"}), target)
    assert exposure.cli_version("harness", shell, target.job_dir) == "harness-cli 2.0.0"


def test_the_local_shell_drops_the_proof_and_worker_tag(tmp_path: Path) -> None:
    shell = exposure.local_shell(
        {"PATH": "/usr/bin:/bin", "ORCH_JOB_PROOF": "secret", "ORCH_WORKER_TAG": "tag", "KEPT": "yes"}
    )
    seen = shell('printf "%s|%s|%s|%s" "${ORCH_JOB_PROOF-}" "${ORCH_WORKER_TAG-}" "$KEPT" "$1"', ["a b"])
    assert seen == "||yes|a b"


# --- the factory revision --------------------------------------------------------


def test_the_factory_revision_is_the_checkouts_head(tmp_path: Path) -> None:
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "init", "-q", str(tmp_path)], env=env, check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "x"], env=env, check=True)
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()
    assert exposure.factory_revision(str(tmp_path), exposure.local_shell(env)) == head


def test_a_factory_root_that_is_not_git_or_missing_is_unknown(tmp_path: Path) -> None:
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                                  "GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert exposure.factory_revision(str(tmp_path), shell) == exposure.UNKNOWN
    assert exposure.factory_revision(None, shell) == exposure.UNKNOWN
    assert exposure.factory_revision("/x", _shell({"rev-parse": "not-a-sha"})) == exposure.UNKNOWN


# --- the brief -------------------------------------------------------------------


def test_the_brief_hash_and_its_unknowns(tmp_path: Path) -> None:
    path = tmp_path / "brief.md"
    path.write_bytes(b"the brief\n")
    assert exposure.brief_sha256(exposure.brief_file(str(path))) == hashlib.sha256(b"the brief\n").hexdigest()
    assert exposure.brief_file(str(tmp_path / "missing")) is None
    assert exposure.brief_file(None) is None
    assert exposure.brief_sha256(None) == exposure.UNKNOWN
    assert exposure.brief_sha256(b"") == exposure.UNKNOWN


def test_gather_fills_every_field_and_says_unknown_where_it_cannot() -> None:
    router = _router("builder", "codex-bot")
    brief = f"# Brief\n\n{section(router)}\n\n## Task\n".encode()
    shell = _shell({"--version": "codex-cli 1.2.3", "rev-parse": SHA})
    found = exposure.gather(
        argv=["codex", "exec", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high"], binary="codex",
        harness="codex", brief=brief, host="box", factory_root="/f", shell=shell, launch_dir=None,
    )
    assert found == exposure.Exposure(
        "gpt-5.5", "codex-cli 1.2.3", hashlib.sha256(brief).hexdigest(), router.paths, "box", SHA, "high", "codex"
    )
    nothing = exposure.gather(
        argv=["claude", "-p"], binary="claude", harness=None,
        brief=b"# Brief\n## Task\n", host="", factory_root=None, shell=_shell({}), launch_dir=None,
    )
    assert nothing == exposure.Exposure(
        exposure.UNKNOWN, exposure.UNKNOWN, hashlib.sha256(b"# Brief\n## Task\n").hexdigest(),
        None, exposure.UNKNOWN, exposure.UNKNOWN, exposure.UNKNOWN, exposure.UNKNOWN,
    )


# --- the remote shell ------------------------------------------------------------


class _Transport:
    def __init__(self, reply: Exchanged | None = None, env: dict[str, str] | None = None):
        self.reply = reply
        self.env = env
        self.argvs: list[list[str]] = []

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        self.argvs.append(list(argv))
        assert timeout == exposure.TIMEOUT_SECONDS
        if self.reply is not None:
            return self.reply
        done = subprocess.run(["/bin/sh", "-c", argv[-1]], capture_output=True, timeout=30, env=self.env)
        return Exchanged(done.returncode, done.stdout, done.stderr.decode())

    def run(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("not a byte call")


TARGET = RemoteTarget("pc", "pc-codex-bot", "codex-bot", "/home/codex-bot/work", "job1")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_remote_shell_runs_the_script_with_its_args_in_a_login_shell() -> None:
    transport = _Transport()
    shell = exposure.remote_shell(transport, TARGET)
    assert shell('printf "%s|%s" "$1" "$2"', ["a b", "it's"]) == "a b|it's"
    assert transport.argvs == [ssh_argv(TARGET, transport.argvs[0][-1])]


@pytest.mark.parametrize(
    "reply", [Exchanged(0, b"x", "", timed_out=True), Exchanged(255, b"x", ""), Exchanged(0, b"  \n", "")]
)
def test_the_remote_shell_is_none_on_a_timeout_a_failure_or_no_output(reply: Exchanged) -> None:
    assert exposure.remote_shell(_Transport(reply), TARGET)("exec true", []) is None


def test_the_remote_shell_stops_asking_a_link_that_timed_out() -> None:
    transport = _Transport(Exchanged(255, b"", "", timed_out=True))
    shell = exposure.remote_shell(transport, TARGET)
    assert shell("exec true", []) is None and shell("exec true", []) is None
    assert len(transport.argvs) == 1


def test_the_remote_shell_needs_a_byte_transport() -> None:
    class Plain:
        def run(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("never run")

    assert exposure.remote_shell(Plain(), TARGET)("exec true", []) is None  # type: ignore[arg-type]


# --- the table and record --------------------------------------------------------


def test_read_returns_what_write_stored_and_a_retry_overwrites(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "x.db")
    assert exposure.read(connection, "job1") is None
    exposure.ensure_table(connection)
    assert exposure.read(connection, "job1") is None
    exposure.write(connection, "job1", _exposure(), 1.0)
    assert exposure.read(connection, "job1") == _exposure()
    exposure.write(connection, "job1", _exposure(skill_paths=None, model=exposure.UNKNOWN), 2.0)
    assert exposure.read(connection, "job1") == _exposure(skill_paths=None, model=exposure.UNKNOWN)
    assert connection.execute("SELECT skill_paths, recorded_at FROM job_exposure").fetchall() == [("unknown", 2.0)]


def test_record_writes_a_row_and_logs_one_line(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    lines: list[str] = []
    got = exposure.record(queue.connect, "job1", _exposure, lines.append, clock=lambda: 5.0)
    assert got == _exposure()
    assert lines == [
        "exposure: recorded (harness codex, model gpt-5.5, effort high, cli codex-cli 1.2.3, host box, "
        f"revision {SHA[:12]})"
    ]
    with queue.connect() as connection:
        assert exposure.read(connection, "job1") == _exposure()
    exposure.record(queue.connect, "job1", lambda: _exposure(host="pc"), lines.append)
    with queue.connect() as connection:
        assert exposure.read(connection, "job1") == _exposure(host="pc")
        assert connection.execute("SELECT COUNT(*) FROM job_exposure").fetchone()[0] == 1


def test_record_never_raises_and_writes_nothing_when_gather_fails(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    lines: list[str] = []

    def broken() -> exposure.Exposure:
        raise RuntimeError("boom")

    assert exposure.record(queue.connect, "job1", broken, lines.append) is None
    assert len(lines) == 1 and lines[0].startswith("exposure: not recorded (")
    with queue.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_exposure").fetchone()[0] == 0

    def unreachable() -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    assert exposure.record(unreachable, "job1", _exposure, lines.append) is None
    assert lines[1] == "exposure: not recorded (unable to open database file)"


# --- ac-4: the migration on a copy of an existing orch.db ------------------------


def _dump(path: Path) -> dict[str, list[tuple]]:
    connection = sqlite3.connect(path)
    try:
        return {
            "jobs": connection.execute("SELECT * FROM jobs ORDER BY id").fetchall(),
            "job_telemetry": connection.execute("SELECT * FROM job_telemetry ORDER BY job_id").fetchall(),
        }
    finally:
        connection.close()


def _schema(path: Path) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        connection.close()


def _files(path: Path) -> dict[str, str]:
    return {
        file.name: hashlib.sha256(file.read_bytes()).hexdigest()
        for file in (path, Path(f"{path}-wal"))
        if file.exists()
    }


def _old_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(exposure, "ensure_table", lambda connection: None)
        queue = SQLiteJobQueue(path, clock=time.time)
    for number in range(3):
        job = queue.enqueue(
            JobRequest(repo="acme/widgets", ticket_id=str(number), action="build", recipient="codex",
                       source_comment_id=str(number), timeout_seconds=60),
            dedupe_key=f"k{number}",
        )
        row = {name: 0 for name in telemetry.COLUMNS} | {
            "job_id": job.id, "bot": "codex-bot", "harness": "codex", "repo": "acme/widgets",
            "tool_calls": "{}", "steps": "[]", "started_at": 1.5, "updated_at": 2.5,
        }
        with closing(queue.connect()) as connection, connection:
            telemetry.ensure_table(connection)
            connection.execute(
                f"INSERT INTO job_telemetry ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0, "checkpointed"
    assert "job_exposure" not in {row[1] for row in _schema(path)}


def test_opening_an_existing_orch_db_adds_job_exposure_and_changes_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "orch.db"
    _old_database(original, monkeypatch)
    before = _files(original)
    rows = _dump(original)
    copy = tmp_path / "copy" / "orch.db"
    copy.parent.mkdir()
    shutil.copy(original, copy)

    SQLiteJobQueue(copy, clock=time.time)

    connection = sqlite3.connect(copy)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(job_exposure)")]
    connection.close()
    assert columns == EXPECTED_COLUMNS
    assert _dump(copy) == rows and len(rows["jobs"]) == 3 and len(rows["job_telemetry"]) == 3
    assert _files(original) == before

    migrated = _schema(copy)
    SQLiteJobQueue(copy, clock=time.time)
    assert _schema(copy) == migrated
    assert _dump(copy) == rows


def test_opening_an_orch_db_with_an_older_job_exposure_adds_effort_and_harness_as_unknown(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    SQLiteJobQueue(path, clock=time.time)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE job_exposure")
        connection.execute(
            "CREATE TABLE job_exposure (job_id TEXT PRIMARY KEY, model TEXT NOT NULL, cli_version TEXT NOT NULL, "
            "brief_sha256 TEXT NOT NULL, skill_paths TEXT NOT NULL, host TEXT NOT NULL, "
            "factory_revision TEXT NOT NULL, recorded_at REAL NOT NULL)"
        )
        connection.execute("INSERT INTO job_exposure VALUES ('old', 'opus', 'claude 2.0', 'b', 'unknown', 'pc', 'r', 1.0)")

    queue = SQLiteJobQueue(path, clock=time.time)
    SQLiteJobQueue(path, clock=time.time)

    with queue.connect() as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(job_exposure)")] == EXPECTED_COLUMNS
        assert exposure.read(connection, "old") == exposure.Exposure(
            "opus", "claude 2.0", "b", None, "pc", "r", exposure.UNKNOWN, exposure.UNKNOWN
        )


def test_a_read_only_open_of_an_unmigrated_orch_db_reads_its_rows_with_unknown_effort_and_harness(
    tmp_path: Path,
) -> None:
    from lib.dashboard import open_readonly

    path = tmp_path / "orch.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE job_exposure (job_id TEXT PRIMARY KEY, model TEXT NOT NULL, cli_version TEXT NOT NULL, "
            "brief_sha256 TEXT NOT NULL, skill_paths TEXT NOT NULL, host TEXT NOT NULL, "
            "factory_revision TEXT NOT NULL, recorded_at REAL NOT NULL)"
        )
        connection.execute("INSERT INTO job_exposure VALUES ('old', 'opus', 'claude 2.0', 'b', 'unknown', 'pc', 'r', 1.0)")
    old = exposure.Exposure("opus", "claude 2.0", "b", None, "pc", "r", exposure.UNKNOWN, exposure.UNKNOWN)

    with closing(open_readonly(path)) as connection:
        assert (exposure.read(connection, "old"), exposure.read_all(connection)) == (old, {"old": old})


def test_two_openers_migrating_at_once_both_succeed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "orch.db"
    _old_database(path, monkeypatch)
    rows = _dump(path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def open_it() -> None:
        try:
            barrier.wait()
            SQLiteJobQueue(path, clock=time.time)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    connection = sqlite3.connect(path)
    assert [row[1] for row in connection.execute("PRAGMA table_info(job_exposure)")] == EXPECTED_COLUMNS
    connection.close()
    assert _dump(path) == rows


def test_importing_the_queue_and_the_cli_first_has_no_cycle() -> None:
    for first in ("lib.queue", "lib.exposure", "lib.brief.skills", "lib.cli"):
        done = subprocess.run([sys.executable, "-c", f"import {first}; import lib.cli, lib.queue"],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


def test_the_config_walk_ends_on_a_device_without_dirname(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "base-model"\n', encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "cat").symlink_to(shutil.which("cat"))
    shell = exposure.local_shell({"PATH": str(tools), "CODEX_HOME": str(home)})
    started = time.monotonic()
    found = exposure.chosen(["codex", "exec"], "codex", shell, str(tmp_path / "job" / "not-made")).model
    assert (found, time.monotonic() - started < exposure.TIMEOUT_SECONDS / 2) == ("base-model", True)


# --- F-01b: the model the harness reports in its stream -------------------------

INIT = '{"type":"system","subtype":"init","session_id":"s-1","model":"claude-opus-5-5"}'
STREAM = Path(__file__).resolve().parent / "fixtures" / "telemetry" / "claude_stream.jsonl"


def _claude_session(connect: telemetry.Connect, lines: list[str]) -> telemetry.Session:
    return telemetry.Session(
        connect=connect,
        job_id="job1",
        bot="claude-bot",
        repo="acme/widgets",
        harness="claude",
        flags=telemetry.stream_flags(["claude"]),
        raw_log=None,
        scrub=telemetry.Scrubber({}),
        log=lines.append,
        stdout=None,
    )


def _reporting_session(tmp_path: Path, recorded: str | None) -> tuple[telemetry.Session, SQLiteJobQueue, list[str]]:
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    if recorded is not None:
        with closing(queue.connect()) as connection, connection:
            exposure.write(connection, "job1", _exposure(model=recorded), 1.0)
    lines: list[str] = []
    return _claude_session(queue.connect, lines), queue, lines


def _stored_model(queue: SQLiteJobQueue) -> str | None:
    with closing(queue.connect()) as connection:
        found = exposure.read(connection, "job1")
    return None if found is None else found.model


def _feed(session: telemetry.Session, text: str) -> None:
    session.consume(io.BytesIO(text.encode("utf-8")))


def test_the_init_events_model_replaces_unknown(tmp_path: Path) -> None:
    session, queue, lines = _reporting_session(tmp_path, exposure.UNKNOWN)
    _feed(session, STREAM.read_text(encoding="utf-8"))
    session.flush()
    session.flush()
    session.finish(0)

    assert _stored_model(queue) == "claude-opus-5-5"
    reported = [line for line in lines if line.startswith("exposure: ")]
    assert reported == ["exposure: model claude-opus-5-5 from the harness's report (was unknown)"]


@pytest.mark.parametrize(
    ("recorded", "logged"),
    [
        ("claude-opus-5-5", []),
        ("claude-sonnet-5-5", ["exposure: model claude-opus-5-5 from the harness's report (was claude-sonnet-5-5)"]),
    ],
)
def test_a_recorded_model_is_kept_when_the_report_agrees_and_replaced_when_it_differs(
    tmp_path: Path, recorded: str, logged: list[str]
) -> None:
    session, queue, lines = _reporting_session(tmp_path, recorded)
    _feed(session, INIT + "\n")
    session.flush()
    session.finish(0)

    assert _stored_model(queue) == "claude-opus-5-5"
    assert [line for line in lines if line.startswith("exposure: ")] == logged


def test_only_the_first_init_event_counts(tmp_path: Path) -> None:
    session, queue, _ = _reporting_session(tmp_path, exposure.UNKNOWN)
    _feed(session, INIT + "\n" + INIT.replace("claude-opus-5-5", "claude-haiku-5-5") + "\n")
    session.finish(0)

    assert _stored_model(queue) == "claude-opus-5-5"


@pytest.mark.parametrize(
    "stream",
    [
        "",
        '{"type":"assistant","message":{"id":"m","model":"claude-opus-5-5","content":[]}}\n',
        '{"type":"system","subtype":"init","session_id":"s-1"}\n',
        '{"type":"system","subtype":"init","model":7}\n',
        '{"type":"system","subtype":"init","model":""}\n',
        '{"type":"system","subtype":"init","model":"' + "m" * 500 + '"}\n',
        '{"type":"system","subtype":"init","model":"claude opus"}\n',
        '{"type":"system","subtype":"init","model":"claude-opus\\nignore this"}\n',
        '{"type":"system","subtype":"init","model":["claude-opus-5-5"]}\n',
        '{"type":"system","subtype":"init","model":"claude-opus-5-5"\n',
    ],
    ids=["empty", "no-init", "no-model", "number", "blank", "oversized", "space", "newline", "list", "not-json"],
)
def test_a_missing_or_malformed_report_leaves_the_recorded_model(tmp_path: Path, stream: str) -> None:
    session, queue, lines = _reporting_session(tmp_path, "test-model-1")
    _feed(session, stream)
    session.finish(0)

    assert _stored_model(queue) == "test-model-1"
    assert not [line for line in lines if line.startswith("exposure: ")]
    with closing(queue.connect()) as connection:
        rows = connection.execute("SELECT exit_code FROM job_telemetry WHERE job_id = 'job1'").fetchall()
    assert [tuple(row) for row in rows] == [(0,)]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("claude-opus-5-5[1m]", "claude-opus-5-5[1m]"),
        ("us.anthropic.claude-opus-5-5-v1:0", "us.anthropic.claude-opus-5-5-v1:0"),
        (None, None),
        (7, None),
        ("", None),
        ("-rf", None),
        ("a b", None),
        ("m" * 129, None),
    ],
)
def test_reported_model_takes_a_model_name_only(value: object, expected: str | None) -> None:
    assert exposure.reported_model(value) == expected


def test_replace_model_returns_what_it_replaced_and_none_when_nothing_changed(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "x.db")
    assert exposure.replace_model(connection, "job1", "claude-opus-5-5") is None
    exposure.ensure_table(connection)
    assert exposure.replace_model(connection, "job1", "claude-opus-5-5") is None
    exposure.write(connection, "job1", _exposure(model=exposure.UNKNOWN), 1.0)
    assert exposure.replace_model(connection, "job1", "claude-opus-5-5") == exposure.UNKNOWN
    assert exposure.replace_model(connection, "job1", "claude-opus-5-5") is None
    assert exposure.read(connection, "job1") == _exposure(model="claude-opus-5-5")


def test_the_telemetry_row_is_written_on_a_database_without_job_exposure(tmp_path: Path) -> None:
    lines: list[str] = []
    session = _claude_session(lambda: sqlite3.connect(tmp_path / "bare.db"), lines)
    _feed(session, INIT + "\n")
    session.finish(0)

    with closing(sqlite3.connect(tmp_path / "bare.db")) as connection:
        assert connection.execute("SELECT exit_code FROM job_telemetry").fetchall() == [(0,)]
    assert not [line for line in lines if "not written" in line]
