import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "model" / "check.sh"

FLAKY = """#!/bin/sh
echo "$*" >> "{log}"
{before}case " $* " in
  *" {verb} "*)
    n=$(cat "{count}" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "{count}"
    if [ "$n" -le 2 ]; then
      echo "The requested URL returned error: 429" >&2
      exit {code}
    fi
    {on_success}
    ;;
esac
exit 0
"""

LAUNCHER = "#!/bin/sh\nexit 3\n"


def write_tool(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_tool(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    return bin_dir, env


def run_check(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(CHECK)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_statechart_fetch_survives_github_rate_limit(tmp_path: Path) -> None:
    bin_dir, env = sandbox(tmp_path)
    log = tmp_path / "git.log"
    count = tmp_path / "fetches"
    on_checkout = (
        'if [ "$3" = checkout ]; then\n'
        '  mkdir -p "$2/pstack/skills/statechart/scripts"\n'
        f"  printf '{LAUNCHER}' > \"$2/pstack/skills/statechart/scripts/statechart\"\n"
        '  chmod +x "$2/pstack/skills/statechart/scripts/statechart"\n'
        "fi\n"
    )
    write_tool(
        bin_dir / "git",
        FLAKY.format(
            log=log,
            count=count,
            verb="fetch",
            code=128,
            on_success="",
            before=on_checkout,
        ),
    )
    env["TLA2TOOLS"] = str(tmp_path / "tla2tools.jar")

    result = run_check(env)

    assert count.read_text().strip() == "3"
    assert "1/8 phase 1 model" in result.stdout, result.stderr
    assert "exited 3, expected 0" in result.stderr


def test_tlc_jar_download_survives_github_rate_limit(tmp_path: Path) -> None:
    bin_dir, env = sandbox(tmp_path)
    log = tmp_path / "curl.log"
    count = tmp_path / "downloads"
    write_tool(
        bin_dir / "curl",
        FLAKY.format(
            log=log,
            count=count,
            verb="-fsSL",
            code=22,
            on_success='shift 2; echo jar > "$1"',
            before="",
        ),
    )
    env["STATECHART"] = str(tmp_path / "statechart")

    result = run_check(env)

    assert count.read_text().strip() == "3"
    assert "429" in result.stderr
    assert "FAILED" in result.stdout + result.stderr


FAKE_STATECHART = """#!/bin/sh
while [ $# -gt 0 ]; do
  case $1 in
    --config) cfg=$(basename "$2"); shift 2 ;;
    --out) out=$2; shift 2 ;;
    *) shift ;;
  esac
done
chart={chart}
breaks() {{
  printf '## %s breaks\\nThe shortest path has 2 steps:\\n0. intake\\n' "$1" > "$out/report.md"
  printf '%s\\n' "$2" >> "$out/report.md"
  exit 1
}}
case $cfg in
  Orchestrator.cfg)
    printf '## Rules that hold\\nT1KindGo, T1PDecided, Phase1T9Off, HandoffCountsApprovedRound, ReopenHoldOnlyAtWork, ShipNeedsCoveredQa, QaResultFromRun\\n' > "$out/report.md" ;;
  OrchestratorAll.cfg)
    printf '## Rules that hold\\nT9Decided\\n' > "$out/report.md" ;;
  chart.cfg) grep -v ': post-merge$' "$chart" > "$out/chart.mmd" ;;
  chart-all.cfg) cp "$chart" "$out/chart.mmd" ;;
  omit-t1.cfg) breaks T1KindGo '{t1_steps}' ;;
  omit-t1p.cfg)
    breaks T1PDecided '1. brief and acceptance criteria posted: questions
2. kind=go, queue plan: planning' ;;
  omit-ship-guard.cfg) breaks ShipPinnedToCoveredHead '1. ship = merge: merged' ;;
  omit-qa-guard.cfg) {qa_guard} ;;
esac
exit 0
"""

QA_GUARD_BREAKS = "breaks ShipNeedsCoveredQa '1. ship = merge, QA covers the head: merged'"


@pytest.mark.parametrize(
    ("t1_steps", "passes"),
    [
        ("1. brief and acceptance criteria posted: questions", True),
        (
            "1. brief and acceptance criteria posted: questions\n"
            "2. kind=go, queue plan: planning",
            False,
        ),
    ],
    ids=["ends-in-questions", "ends-elsewhere"],
)
def test_check_sh_fails_when_the_break_trace_ends_elsewhere(
    tmp_path: Path,
    t1_steps: str,
    passes: bool,
) -> None:
    _bin_dir, env = sandbox(tmp_path)
    fake = tmp_path / "statechart"
    write_tool(
        fake,
        FAKE_STATECHART.format(
            chart=ROOT / "model" / "chart.mmd",
            t1_steps=t1_steps,
            qa_guard=QA_GUARD_BREAKS,
        ),
    )
    env["STATECHART"] = str(fake)
    env["TLA2TOOLS"] = str(tmp_path / "tla2tools.jar")

    result = run_check(env)

    if passes:
        assert result.returncode == 0, result.stderr
        assert "statechart ok" in result.stdout
    else:
        assert result.returncode != 0
        assert "omit-t1 counterexample did not end in questions" in result.stderr


@pytest.mark.parametrize(
    ("qa_guard", "message"),
    [
        (
            "printf '## Rules that hold\\nShipNeedsCoveredQa\\n' > \"$out/report.md\"",
            "omit-qa-guard exited 0, expected 1",
        ),
        (
            "breaks ShipNeedsCoveredQa '1. ship = merge, QA covers the head: handed-off'",
            "omit-qa-guard counterexample did not end in merged",
        ),
    ],
    ids=["holds", "ends-elsewhere"],
)
def test_check_sh_fails_when_the_qa_guard_fixture_does_not_break(
    tmp_path: Path,
    qa_guard: str,
    message: str,
) -> None:
    _bin_dir, env = sandbox(tmp_path)
    fake = tmp_path / "statechart"
    write_tool(
        fake,
        FAKE_STATECHART.format(
            chart=ROOT / "model" / "chart.mmd",
            t1_steps="1. brief and acceptance criteria posted: questions",
            qa_guard=qa_guard,
        ),
    )
    env["STATECHART"] = str(fake)
    env["TLA2TOOLS"] = str(tmp_path / "tla2tools.jar")

    result = run_check(env)

    assert result.returncode != 0
    assert message in result.stderr
