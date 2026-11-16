import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib.eval_report import EvalError, build_report

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = ROOT / "tests" / "fixtures" / "eval" / "messages"
LINE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z repo=maxharris1/Cleffy pr=443 "
    r"message from=grok-bot to=codex-bot kind=handoff\n"
)


def orch(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def section(text: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index("bot-to-bot messages vs scripted handoffs") + 1
    end = lines.index("", start) if "" in lines[start:] else len(lines)
    return lines[start:end]


def test_log_message_appends_one_line(tmp_path: Path) -> None:
    log = tmp_path / "dispatcher.log"
    log.write_text("2026-11-02T09:00:00Z repo=maxharris1/Cleffy ticket=7 end\n", encoding="utf-8")
    args = ["log-message", "--log", str(log), "--repo", "maxharris1/Cleffy", "--pr", "443"]
    result = orch([*args, "--from", "grok-bot", "--to", "codex-bot"])
    assert result.returncode == 0, result.stderr
    lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) == 2
    assert LINE.fullmatch(lines[1])


def test_log_message_defaults_to_the_log_beside_the_database(tmp_path: Path) -> None:
    env = {"ORCH_DB": str(tmp_path / "orch.db"), "PATH": "/usr/bin:/bin"}
    result = orch(
        ["log-message", "--repo", "maxharris1/Cleffy", "--from", "a", "--to", "b"], env=env
    )
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "dispatcher.log").read_text(encoding="utf-8")
    assert text.endswith(" repo=maxharris1/Cleffy message from=a to=b kind=handoff\n")


@pytest.mark.parametrize(
    "bad",
    [
        ["--repo", "Cleffy"],
        ["--repo", "maxharris1/Cleffy/extra"],
        ["--pr", "abc"],
        ["--pr", "0"],
        ["--from", ""],
        ["--to", ""],
    ],
)
def test_log_message_refuses_bad_input_and_writes_nothing(tmp_path: Path, bad: list[str]) -> None:
    log = tmp_path / "dispatcher.log"
    values = {"--repo": "maxharris1/Cleffy", "--pr": "443", "--from": "grok-bot", "--to": "codex-bot"}
    values[bad[0]] = bad[1]
    args = [item for pair in values.items() for item in pair]
    result = orch(["log-message", "--log", str(log), *args])
    assert result.returncode != 0
    assert not log.exists()


@pytest.mark.parametrize("ending", [b"\n", b""])
def test_logged_messages_are_counted_by_the_report(tmp_path: Path, ending: bytes) -> None:
    root = tmp_path / "bundle"
    shutil.copytree(MESSAGES, root)
    log = root / "dispatcher.log"
    log.write_bytes(log.read_bytes().rstrip(b"\n") + ending)
    result = orch(
        [
            "log-message",
            "--log",
            str(log),
            "--repo",
            "maxharris1/Cleffy",
            "--from",
            "grok-bot",
            "--to",
            "codex-bot",
        ]
    )
    assert result.returncode == 0, result.stderr
    counts = build_report(root).report.handoffs
    assert sum(item.manual for item in counts) == 4


def test_fixture_counts_manual_and_scripted_per_repo_and_week() -> None:
    text = orch(["eval-report", "--input", str(MESSAGES)])
    assert text.returncode == 0, text.stderr
    assert section(text.stdout) == [
        "maxharris1/Cleffy 2026-W45 manual=3 scripted=5",
        "token savings: not measured (no per-message token count in the input)",
    ]
    data = orch(["eval-report", "--input", str(MESSAGES), "--json"])
    assert data.returncode == 0, data.stderr
    report = json.loads(data.stdout)
    assert report["handoffs"] == [
        {"repo": "maxharris1/Cleffy", "week": "2026-W45", "manual": 3, "scripted": 5}
    ]
    assert "token" not in data.stdout


def test_no_manual_messages_still_counts_scripted(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    shutil.copytree(MESSAGES, root)
    log = root / "dispatcher.log"
    kept = [line for line in log.read_text(encoding="utf-8").splitlines() if " message " not in line]
    log.write_text("\n".join(kept) + "\n", encoding="utf-8")
    text = orch(["eval-report", "--input", str(root)]).stdout
    rows = section(text)
    assert rows[0] == "maxharris1/Cleffy 2026-W45 manual=0 scripted=5"
    assert not re.search(r"token savings: \d", text)


def test_counts_split_by_week_and_window(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    shutil.copytree(MESSAGES, root)
    with (root / "dispatcher.log").open("a", encoding="utf-8") as handle:
        handle.write("2026-11-09T08:00:00Z repo=maxharris1/Cleffy message from=a to=b kind=handoff\n")
    rows = section(orch(["eval-report", "--input", str(root)]).stdout)
    assert rows[:2] == [
        "maxharris1/Cleffy 2026-W45 manual=3 scripted=5",
        "maxharris1/Cleffy 2026-W46 manual=1 scripted=0",
    ]
    window = build_report(root, since="2026-11-04T00:00:00Z").report.handoffs
    assert [(item.week, item.manual, item.scripted) for item in window] == [
        ("2026-W45", 1, 1),
        ("2026-W46", 1, 0),
    ]


@pytest.mark.parametrize(
    "line",
    [
        "2026-11-02T09:00:00Z repo=maxharris1/Cleffy message from=a",
        "2026-11-02T09:00:00Z repo=maxharris1/Cleffy message from=a to=b ticket=7",
    ],
)
def test_malformed_message_line_is_skipped_and_counted(tmp_path: Path, line: str) -> None:
    root = tmp_path / "bundle"
    shutil.copytree(MESSAGES, root)
    before = build_report(root).report.handoffs
    with (root / "dispatcher.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    built = build_report(root)
    assert built.report.skipped_lines == 1
    assert built.report.handoffs == before


def test_message_line_for_unknown_repo_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    shutil.copytree(MESSAGES, root)
    with (root / "dispatcher.log").open("a", encoding="utf-8") as handle:
        handle.write("2026-11-02T09:00:00Z repo=maxharris1/Other message from=a to=b\n")
    with pytest.raises(EvalError, match="is not a project in orch.yml"):
        build_report(root)


def test_json_needs_input() -> None:
    result = orch(["eval-report", "--db", "x", "--baseline", "y", "--json"])
    assert result.returncode == 2
