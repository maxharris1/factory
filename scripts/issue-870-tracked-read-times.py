import argparse
import math
import os
import re
import statistics
import sys
from pathlib import Path

CYCLE_END = re.compile(r"orch poll: cycle end (.+?) \(([0-9.]+)s\): .*?\btracked-read!? ([0-9.]+)s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", nargs="?", type=Path)
    parser.add_argument("--last", type=int, default=0, help="only the newest N cycle-end lines")
    args = parser.parse_args()
    default = os.environ.get("ORCH_POLL_LOG") or "~/.local/state/factory/poller.log"
    log = (args.log or Path(default)).expanduser()
    rows = [match.groups() for line in log.read_text().splitlines() if (match := CYCLE_END.search(line))]
    rows = rows[-args.last :] if args.last else rows
    if not rows:
        print(f"no cycle-end lines with a tracked-read phase in {log}", file=sys.stderr)
        return 1
    tracked = sorted(float(row[2]) for row in rows)
    cycles = sorted(float(row[1]) for row in rows)

    def p90(values: list[float]) -> float:
        return values[math.ceil(0.9 * len(values)) - 1]

    print(f"{len(rows)} cycles, {rows[0][0]} to {rows[-1][0]}")
    print(f"tracked-read: median {statistics.median(tracked):.1f}s, p90 {p90(tracked):.1f}s")
    print(f"whole cycle:  median {statistics.median(cycles):.1f}s, p90 {p90(cycles):.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
