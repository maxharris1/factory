import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "issue-1768-status-push-times.py"


def test_prints_size_timing_and_statement_gaps():
    env = {key: value for key, value in os.environ.items() if not key.startswith("ORCH_")}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--jobs", "20", "--detail-kb", "4", "--runs", "1", "--statements", "3"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120, check=True,
    )
    lines = result.stdout.splitlines()
    assert re.fullmatch(r"database: \d+\.\d MiB, 20 jobs, 4 KB detail, 2 tracked tickets", lines[0])
    assert re.fullmatch(r"collect_snapshot: best \d+\.\d\d s of 1 runs \(\d+\.\d\d s\)", lines[1])
    assert re.fullmatch(r"longest 3 of \d+ statement gaps .*:", lines[2])
    assert len(lines) == 6
    assert all(re.fullmatch(r"  \d+\.\d{3} s  \S.*", line) for line in lines[3:])
