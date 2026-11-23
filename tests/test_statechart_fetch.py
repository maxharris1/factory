"""model/check.sh survives a rate-limited download of its tools.

GitHub answers 429 to busy CI runners. The check must retry the statechart
fetch and the tla2tools download, then fail if every try fails. git, curl,
sha256sum and sleep are fakes on PATH; HOME and the cache are temporary.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "model" / "check.sh"
FIRST_STEP = re.search(r'echo "(1/\d+ phase 1 model)', SCRIPT.read_text()).group(1)

FAKE_GIT = """#!/bin/sh
echo "$*" >> "$FAKE_LOG/git"
case " $* " in
  *" fetch "*)
    n=$(($(cat "$FAKE_LOG/fetches" 2>/dev/null || echo 0) + 1))
    echo "$n" > "$FAKE_LOG/fetches"
    if [ "$n" -le "$FAKE_GIT_FAILS" ]; then
      echo "fatal: unable to access 'https://github.com/cursor/plugins.git/': The requested URL returned error: 429" >&2
      exit 128
    fi ;;
  *" checkout "*)
    mkdir -p "$2/pstack/skills/statechart/scripts"
    printf '#!/bin/sh\\nexit 0\\n' > "$2/pstack/skills/statechart/scripts/statechart"
    chmod +x "$2/pstack/skills/statechart/scripts/statechart" ;;
esac
exit 0
"""

FAKE_CURL = """#!/bin/sh
n=$(($(cat "$FAKE_LOG/curls" 2>/dev/null || echo 0) + 1))
echo "$n" > "$FAKE_LOG/curls"
if [ "$n" -le "$FAKE_CURL_FAILS" ]; then
  echo "curl: (22) The requested URL returned error: 429" >&2
  exit 22
fi
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then echo jar > "$2"; fi
  shift
done
"""

FAKE_SLEEP = """#!/bin/sh
echo "$1" >> "$FAKE_LOG/sleeps"
"""

FAKE_SHA = """#!/bin/sh
cat >/dev/null
"""


def run_check(tmp_path: Path, curl_fails: int,
              git_fails: int) -> tuple[subprocess.CompletedProcess, Path]:
    bin_dir = tmp_path / "bin"
    log = tmp_path / "log"
    bin_dir.mkdir()
    log.mkdir()
    for name, body in {"git": FAKE_GIT, "curl": FAKE_CURL, "sleep": FAKE_SLEEP,
                       "sha256sum": FAKE_SHA}.items():
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if key not in {"TLA2TOOLS", "STATECHART"}}
    env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(tmp_path / "home"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), FAKE_LOG=str(log),
               FAKE_CURL_FAILS=str(curl_fails), FAKE_GIT_FAILS=str(git_fails))
    proc = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True,
                          text=True, timeout=60)
    return proc, log


def count(log: Path, name: str) -> int:
    path = log / name
    return int(path.read_text().strip()) if path.exists() else 0


@pytest.mark.parametrize("fails", [1, 3])
def test_downloads_retry_through_rate_limits(tmp_path: Path, fails: int) -> None:
    proc, log = run_check(tmp_path, fails, fails)
    assert FIRST_STEP in proc.stdout, proc.stderr
    assert count(log, "fetches") == fails + 1
    assert count(log, "curls") == fails + 1
    sleeps = (log / "sleeps").read_text().split()
    assert len(sleeps) == 2 * fails
    assert all(int(s) > 0 for s in sleeps)


@pytest.mark.parametrize(("curl_fails", "git_fails"), [(100, 0), (0, 100)])
def test_downloads_give_up_after_every_try_fails(tmp_path: Path, curl_fails: int,
                                                 git_fails: int) -> None:
    proc, log = run_check(tmp_path, curl_fails, git_fails)
    assert proc.returncode != 0
    assert FIRST_STEP not in proc.stdout
    tries = count(log, "curls") if curl_fails else count(log, "fetches")
    assert 1 < tries < 100


# --- The same retries, as main tested them (#93/#100) ---
CHECK = ROOT / "model" / "check.sh"

RATE_LIMITED = "remote: This request was rate-limited.\nerror: 429"

LAUNCHER = '#!/bin/sh\ntouch "$LAUNCHED"\nexit 3\n'

RATE_LIMITED_GIT = f"""#!/bin/sh
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

RATE_LIMITED_CURL = f"""#!/bin/sh
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


def run_rate_limited_check(tmp_path: Path, fetcher: str, fails: int) -> tuple[subprocess.CompletedProcess, int]:
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
        tool(bin_dir, "git", RATE_LIMITED_GIT)
        env["TLA2TOOLS"] = str(tmp_path / "tla2tools.jar")
    else:
        tool(bin_dir, "curl", RATE_LIMITED_CURL)
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
    proc, calls = run_rate_limited_check(tmp_path, fetcher, fails=2)

    assert calls == 3, proc.stderr
    assert (tmp_path / "launched").exists(), proc.stderr


@pytest.mark.parametrize("fetcher", ["git", "curl"])
def test_a_download_that_keeps_failing_still_fails_the_check(
    tmp_path: Path, fetcher: str
) -> None:
    proc, calls = run_rate_limited_check(tmp_path, fetcher, fails=100)

    assert proc.returncode != 0
    assert 1 < calls < 10
    assert not (tmp_path / "launched").exists()
