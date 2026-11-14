"""The repo skill paths a job names (`repo_skill_paths` in its detail)."""

from __future__ import annotations

import re

_SKILL_PATH = re.compile(r"^\.claude/skills/[a-z0-9-]+/SKILL\.md$")


def skill_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and _SKILL_PATH.fullmatch(item)]
