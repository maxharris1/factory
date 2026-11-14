import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "model" / "check.sh"

RATE_LIMITED = "remote: This request was rate-limited.\nerror: 429"

LAUNCHER = '#!/bin/sh\ntouch "$LAUNCHED"\nexit 3\n'

FAKE_GIT = f"""#!/bin/sh
dir=$2
shift 2
case $1 in
fetch)
  echo x >>"$CALLS"
  if [ "$(wc -l <"$CALLS")" -le "$FAILS" ]; then
    echo "{RATE_LIMITED}" >&2
    exit 128
  fi
  ;;
checkout)
  mkdir -p "$dir/pstack/skills/statechart/scripts"
  printf '%s' '{LAUNCHER}' >"$dir/pstack/skills/statechart/scripts/statechart"
  chmod +x "$dir/pstack/skills/statechart/scripts/statechart"
  ;;
esac
"""

FAKE_CURL = f"""#!/bin/sh
echo x >>"$CALLS"
if [ "$(wc -l <"$CALLS")" -le "$FAILS" ]; then
  echo "curl: (22) The requested URL returned error: 429" >&2
  exit 22
fi
while [ "$1" != -o ]; do shift; done
echo jar >"$2"
"""


def tool(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def run_check(tmp_path: Path, fetcher: str, fails: int) -> tuple[subprocess.CompletedProcess, int]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    calls.touch()
    tool(bin_dir, "sleep", "#!/bin/sh\n")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "CALLS": str(calls),
        "FAILS": str(fails),
        "LAUNCHED": str(tmp_path / "launched"),
    }
    if fetcher == "git":
        tool(bin_dir, "git", FAKE_GIT)
        env["TLA2TOOLS"] = str(tmp_path / "tla2tools.jar")
    else:
        tool(bin_dir, "curl", FAKE_CURL)
        tool(bin_dir, "sha256sum", "#!/bin/sh\ncat >/dev/null\n")
        env["STATECHART"] = str(tool(tmp_path, "statechart", LAUNCHER))
    proc = subprocess.run(
        ["sh", str(CHECK)], env=env, capture_output=True, text=True, timeout=60
    )
    return proc, len(calls.read_text().splitlines())


@pytest.mark.parametrize("fetcher", ["git", "curl"])
def test_a_rate_limited_download_is_retried_before_the_check_runs(
    tmp_path: Path, fetcher: str
) -> None:
    proc, calls = run_check(tmp_path, fetcher, fails=2)

    assert calls == 3, proc.stderr
    assert (tmp_path / "launched").exists(), proc.stderr


@pytest.mark.parametrize("fetcher", ["git", "curl"])
def test_a_download_that_keeps_failing_still_fails_the_check(
    tmp_path: Path, fetcher: str
) -> None:
    proc, calls = run_check(tmp_path, fetcher, fails=100)

    assert proc.returncode != 0
    assert 1 < calls < 10
    assert not (tmp_path / "launched").exists()
