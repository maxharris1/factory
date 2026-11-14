"""The target repo's factory.md prose, appended as additional context."""

from __future__ import annotations

import re

from lib.factory_md import FactoryContext


def context_section(
    facts: dict,
    context: FactoryContext | None,
    ref: str | None,
    note: str | None,
) -> str:
    heading = "## Additional context from factory.md"
    repo = facts["repo"] or "the job's repo (see Job data)"
    if context is not None:
        where = f"{repo} factory.md at {ref}" if ref else f"{repo} factory.md"
        return (
            f"{heading}\n"
            f"From {where}. It adds repo-specific facts to this brief; where it is more specific, follow it.\n\n"
            f"{_nested(context.body)}"
        )
    base = facts["base"] or "its base branch"
    reason = f" ({note})" if note else ""
    return (
        f"{heading}\n"
        f"factory.md was not loaded{reason}. Before you start, read `factory.md` from the base branch "
        f"{base} of {repo} yourself (not from the PR head) and follow its Review, Ship "
        "and Context sections."
    )


def _nested(body: str) -> str:
    """factory.md headings one level down, inside this section."""
    return re.sub(r"^(#{1,5}) ", r"#\1 ", body, flags=re.MULTILINE)
