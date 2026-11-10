import ast
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from lib.eval_report import CHART_TRANSITIONS, EvalError, build_report, render
from lib.factory_md import load_factory_md
from lib.handoff import STATES, parse_comment
from lib.orch_yml import load_orch_yml

ROOT = Path(__file__).resolve().parents[1]
WEEK = ROOT / "tests" / "fixtures" / "eval" / "week"

# Section 4.1 stateDiagram names, mapped to lib.handoff.STATES.
SPEC_STATE = {
    "Intake": "intake",
    "Refused": "refused",
    "Questions": "questions",
    "WaitingOnMax": "waiting-max",
    "Planning": "planning",
    "PlanCheck": "plan-check",
    "Building": "building",
    "Review": "review",
    "Fixing": "fixing",
    "Capped": "capped",
    "Blocked": "blocked",
    "Ship": "ship",
    "Merged": "merged",
    "HandedOff": "handed-off",
    "WaitingExternal": "waiting-external",
    "PostMerge": "post-merge",
    "Done": "done",
    "Stuck": "stuck",
    "Abandoned": "abandoned",
}

EXPECTED = """\
weekly eval report
runs: 5

time in each state
intake 70
questions 70
building 340
review 130
ship 30
merged 35
post-merge 0
done 0
stuck 0
qa 0

wasted agent wakes
count: 2
2026-11-03T15:04:30Z maxharris1/Cleffy 12 claude-bot fix
2026-11-03T15:08:00Z maxharris1/Cleffy 12 grok-bot nobody

review rounds per PR
maxharris1/Cleffy 9 0
maxharris1/Cleffy 12 1
maxharris1/Cleffy 15 0
maxharris1/tracy-harris-coaching 3 0
maxharris1/tracy-harris-coaching 4 2

stuck jobs
maxharris1/Cleffy 9

rule breaks the model finds
2026-11-03T15:08:00Z maxharris1/Cleffy 12 ship-unpinned approval head does not match covered head

chart mismatches
maxharris1/Cleffy 15 post-merge-in-phase-1
maxharris1/tracy-harris-coaching 3 illegal-transition review qa
maxharris1/tracy-harris-coaching 3 unknown-state qa
maxharris1/tracy-harris-coaching 4 round-over-cap low 2 1
"""

WINDOW = """\
weekly eval report
runs: 1

time in each state
intake 10
questions 10
building 100
stuck 0

wasted agent wakes
count: 0

review rounds per PR
maxharris1/Cleffy 9 0

stuck jobs
maxharris1/Cleffy 9

rule breaks the model finds
none

chart mismatches
none
"""

MIN_FACTORY = """\
---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: none
ship: merge
skills: []
---
"""

MIN_ORCH = """\
poll_seconds: 60
projects:
  - repo: maxharris1/Cleffy
"""


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), *args],
        cwd=cwd or ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_bundle(
    root: Path,
    log: str,
    comments: dict[str, str] | None = None,
    *,
    factory: str = MIN_FACTORY,
    orch: str = MIN_ORCH,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "orch.yml").write_text(orch, encoding="utf-8")
    factory_path = root / "projects" / "maxharris1" / "Cleffy" / "factory.md"
    factory_path.parent.mkdir(parents=True)
    factory_path.write_text(factory, encoding="utf-8")
    (root / "dispatcher.log").write_text(log, encoding="utf-8")
    for relative, text in (comments or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_fixture_report_covers_every_f1_14_measure() -> None:
    orch, orch_findings = load_orch_yml(WEEK / "orch.yml")
    assert orch is not None
    assert not any(item.severity == "error" for item in orch_findings)
    tracy, tracy_findings = load_factory_md(
        WEEK / "projects" / "maxharris1" / "tracy-harris-coaching" / "factory.md"
    )
    assert tracy is not None and tracy.rounds_low == 1
    assert not any(item.severity == "error" for item in tracy_findings)
    ready = sorted((WEEK / "comments" / "cleffy-12").glob("ready*.txt"))
    assert len(ready) == 2
    verdict = parse_comment(
        (WEEK / "comments" / "cleffy-12" / "verdict.txt").read_text(encoding="utf-8")
    ).as_dict()
    assert verdict["kind"] == "verdict"
    assert verdict["round"] == "1/1"
    stuck = parse_comment(
        (WEEK / "comments" / "cleffy-9" / "stuck.txt").read_text(encoding="utf-8")
    ).as_dict()
    assert stuck["kind"] == "stuck"
    text = render(build_report(WEEK).report)
    assert text == EXPECTED
    for heading in (
        "time in each state",
        "wasted agent wakes",
        "review rounds per PR",
        "stuck jobs",
        "rule breaks the model finds",
    ):
        assert heading in text


def test_chart_edges_match_section_4_1() -> None:
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    start = spec.index("stateDiagram-v2")
    block = spec[start : spec.index("```", start)]
    found: set[tuple[str, str]] = set()
    for line in block.splitlines():
        match = re.search(r"(\[\*\]|[A-Za-z]+) --> (\[\*\]|[A-Za-z]+)", line)
        if match is None:
            continue
        src, dst = match.group(1), match.group(2)
        if src == "[*]" or dst == "[*]":
            continue
        found.add((SPEC_STATE[src], SPEC_STATE[dst]))
    assert found == CHART_TRANSITIONS
    assert set(SPEC_STATE.values()) == set(STATES)
    for src, dst in CHART_TRANSITIONS:
        assert src in STATES
        assert dst in STATES


def test_week_window_uses_the_same_directory_shape() -> None:
    text = render(
        build_report(
            WEEK,
            since="2026-11-03T16:00:00Z",
            until="2026-11-03T16:02:00Z",
        ).report
    )
    assert text == WINDOW


def test_any_directory_can_be_the_weekly_input(tmp_path: Path) -> None:
    copied = tmp_path / "collected"
    shutil.copytree(WEEK, copied)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert render(build_report(copied).report) == EXPECTED
    result = run(
        ["eval-report", "--input", str(copied)],
        cwd=elsewhere,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == EXPECTED
    assert result.stderr == ""


def test_cli_week_window_flags() -> None:
    result = run(
        [
            "eval-report",
            "--input",
            str(WEEK),
            "--since",
            "2026-11-03T16:00:00Z",
            "--until",
            "2026-11-03T16:02:00Z",
        ]
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == WINDOW


def test_open_state_without_end_adds_no_seconds(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "\n".join(
            (
                "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 enter state=intake",
                "2026-11-03T15:01:00Z repo=maxharris1/Cleffy pr=1 enter state=questions",
                "",
            )
        ),
    )
    text = render(build_report(tmp_path).report)
    assert "intake 60\nquestions 0\n" in text


def test_verdict_without_risk_skips_the_round_cap(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "\n".join(
            (
                "2026-11-03T18:00:00Z repo=maxharris1/Cleffy pr=4 enter state=review",
                "2026-11-03T18:00:10Z repo=maxharris1/Cleffy pr=4 comment path=comments/verdict.txt",
                "2026-11-03T18:00:10Z repo=maxharris1/Cleffy pr=4 end",
                "",
            )
        ),
        {
            "comments/verdict.txt": (
                "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
                "round=5/5 verdict=REVISE -->\nRevise.\n"
            )
        },
    )
    text = render(build_report(tmp_path).report)
    assert "maxharris1/Cleffy 4 5\n" in text
    assert "round-over-cap" not in text


def test_stuck_comment_without_the_state_is_still_a_stuck_job(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "\n".join(
            (
                "2026-11-03T16:00:00Z repo=maxharris1/Cleffy pr=8 comment path=comments/stuck.txt",
                "2026-11-03T16:00:00Z repo=maxharris1/Cleffy pr=8 end",
                "",
            )
        ),
        {
            "comments/stuck.txt": (
                "<!-- orch:v1 kind=stuck from=grok-bot role=orchestrator state=stuck -->\n"
                "Stuck.\n"
            )
        },
    )
    text = render(build_report(tmp_path).report)
    assert "stuck jobs\nmaxharris1/Cleffy 8\n" in text
    assert "maxharris1/Cleffy 8 stuck-comment-without-state\n" in text


def test_invalid_comment_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 comment path=comments/bad.txt\n",
        {"comments/bad.txt": "not a handoff\n"},
    )
    with pytest.raises(EvalError, match="not an orch:v1 header"):
        build_report(tmp_path)


def test_comment_path_cannot_leave_the_input_directory(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("credential-marker", encoding="utf-8")
    root = tmp_path / "week"
    write_bundle(
        root,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 comment path=comments/../../secret.txt\n",
    )
    with pytest.raises(EvalError, match="leaves the input directory") as raised:
        build_report(root)
    assert "credential-marker" not in str(raised.value)


def test_repo_missing_from_orch_yml_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/other pr=1 enter state=intake\n",
    )
    with pytest.raises(EvalError, match="is not a project in orch.yml"):
        build_report(tmp_path)


def test_missing_factory_md_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 enter state=intake\n",
        orch="poll_seconds: 60\nprojects:\n  - repo: maxharris1/Cleffy\n  - repo: maxharris1/missing\n",
    )
    log = tmp_path / "dispatcher.log"
    log.write_text(
        "2026-11-03T15:00:00Z repo=maxharris1/missing pr=1 enter state=intake\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalError, match="file not found"):
        build_report(tmp_path)


def test_since_after_until_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 enter state=intake\n",
    )
    with pytest.raises(EvalError, match="since is after until"):
        build_report(
            tmp_path,
            since="2026-11-03T16:00:00Z",
            until="2026-11-03T15:00:00Z",
        )


def test_invalid_orch_yml_is_refused(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 enter state=intake\n",
        orch="poll_seconds: 60\nprojects: []\nextra: true\n",
    )
    with pytest.raises(EvalError, match="unknown field"):
        build_report(tmp_path)


def test_cli_refuses_a_bad_comment(tmp_path: Path) -> None:
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 comment path=comments/bad.txt\n",
        {"comments/bad.txt": "nope\n"},
    )
    result = run(["eval-report", "--input", str(tmp_path)])
    assert result.returncode == 1
    assert "not an orch:v1 header" in result.stderr
    assert result.stdout == ""


def test_unknown_model_warns_and_still_reports(tmp_path: Path) -> None:
    factory = MIN_FACTORY.replace(
        "reviewer:\n  harness: codex\n",
        "reviewer:\n  harness: codex\n  model: not-a-real-model\n",
    )
    write_bundle(
        tmp_path,
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 enter state=intake\n"
        "2026-11-03T15:00:00Z repo=maxharris1/Cleffy pr=1 end\n",
        factory=factory,
    )
    result = run(["eval-report", "--input", str(tmp_path)])
    assert result.returncode == 0, result.stderr
    assert "warning: unknown model name 'not-a-real-model'" in result.stderr
    assert "runs: 1\n" in result.stdout


def test_report_does_not_write_or_read_a_dotenv(tmp_path: Path) -> None:
    copied = tmp_path / "week"
    shutil.copytree(WEEK, copied)
    secret = copied / ".env"
    secret.write_text("TOKEN=should-not-appear\n", encoding="utf-8")
    before = {
        path: path.read_bytes()
        for path in copied.rglob("*")
        if path.is_file() and path.name != ".env"
    }
    text = render(build_report(copied).report)
    after = {
        path: path.read_bytes()
        for path in copied.rglob("*")
        if path.is_file() and path.name != ".env"
    }
    assert before == after
    assert secret.read_text(encoding="utf-8") == "TOKEN=should-not-appear\n"
    assert "should-not-appear" not in text
    assert "TOKEN" not in text


@pytest.mark.parametrize("command", ["correct", "reflect", "maintain", "blinded-eval"])
def test_retired_self_improvement_commands_are_rejected(command: str) -> None:
    result = run([command])
    assert result.returncode == 2
    assert f"unknown command: {command}" in result.stderr
    text = render(build_report(WEEK).report)
    assert "weekly correct" not in text
    assert "blinded" not in text
    assert "daily maintain" not in text


def test_eval_report_imports_and_does_not_touch_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (ROOT / "lib" / "eval_report.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    banned_defs = {"correct", "reflect", "maintain", "blinded_eval"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                modules.add(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name not in banned_defs
    assert modules <= {
        "__future__",
        "re",
        "collections",
        "dataclasses",
        "datetime",
        "pathlib",
        "typing",
        "lib.factory_md",
        "lib.handoff",
        "lib.load",
        "lib.orch_yml",
    }

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("process or network call")

    monkeypatch.setattr(os, "system", boom)
    monkeypatch.setattr(os, "popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "call", boom)
    monkeypatch.setattr(subprocess, "check_call", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    assert render(build_report(WEEK).report) == EXPECTED
