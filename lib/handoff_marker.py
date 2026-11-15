"""The remote harness's last word: `ORCH-HANDOFF: <id|failed>` (#87 F1.5, #192).

One rule, read in three places: the waiter on the box parses run.log with
it, the telemetry relay (lib/telemetry.py) uses it to pick the one line of a
stream-mode final message that reaches run.log, and the device's HANDOFF_AWK
(lib/remote.py) repeats it in awk. tests/test_marker_parser.py holds all
three to the same table.

The rule: skip blank lines, code fences and the box runner's own
`orch job run: ...` lines; of the last MARKER_SEARCH_LINES lines left, the
last candidate decides. A candidate is a line that starts with the marker
prefix after any leading backticks. It completes only as a bare marker or a
marker in one pair of backticks; any other candidate means no completion,
even when an earlier line holds a good marker.
"""

from __future__ import annotations

import re

MARKER_PREFIX = "ORCH-HANDOFF:"
MARKER_SEARCH_LINES = 20
# A candidate is relayed cut to this length; a good marker is at most 52.
MARKER_LINE_LIMIT = 120
# Lines `orch job run` itself writes to run.log (lib/cli.py _job_run log()).
RUNNER_LOG_PREFIX = "orch job run: "
_FENCE = re.compile(r"^```[A-Za-z0-9_-]*$")
_MARKER = re.compile(
    r"^(`?)ORCH-HANDOFF: ([0-9]{1,20}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|failed)\1$"
)


def marker_line(text: str | None) -> str | None:
    """The candidate line that decides `text`, stripped and cut, or None."""
    if not text:
        return None
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(RUNNER_LOG_PREFIX) or _FENCE.fullmatch(stripped):
            continue
        kept.append(stripped)
    for line in reversed(kept[-MARKER_SEARCH_LINES:]):
        if line.lstrip("`").startswith(MARKER_PREFIX):
            return line[:MARKER_LINE_LIMIT]
    return None


def handoff_marker(text: str) -> str | None:
    """The comment id or `failed` that `text` ends with by the rule, or None."""
    line = marker_line(text)
    match = None if line is None else _MARKER.fullmatch(line)
    return None if match is None else match.group(2)
