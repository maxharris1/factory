from __future__ import annotations

import re

TICKET_TYPES = ("feature", "bug-fix", "refactoring", "perf-issue")
_TYPE_FACT = re.compile(r"^- Type: (?P<value>\S+)$")


def ticket_type_of(brief: str) -> str | None:
    for line in brief.splitlines():
        if line.startswith("#"):
            break
        match = _TYPE_FACT.fullmatch(line)
        if match and match.group("value") in TICKET_TYPES:
            return match.group("value")
    return None
