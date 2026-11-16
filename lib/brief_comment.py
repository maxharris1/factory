from __future__ import annotations

import re

FACTORY_MD_HEADING = "### factory.md (from "

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def before_factory_md(body: str) -> str:
    fence: str | None = None
    lines = body.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if fence is not None:
            closing = line.strip()
            if set(closing) == {fence[0]} and len(closing) >= len(fence):
                fence = None
        elif (match := _FENCE.match(line)) is not None:
            fence = match.group(1)
        elif line.startswith(FACTORY_MD_HEADING):
            return "".join(lines[:index]).rstrip() + "\n"
    return body
