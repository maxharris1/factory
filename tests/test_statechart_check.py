import os
import subprocess
from pathlib import Path

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
    assert "1/6 phase 1 model" in result.stdout, result.stderr
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
