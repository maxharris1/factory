"""A factory PR's title starts with its source issue (issue #115).

GitHub numbers issues and PRs from one sequence, so issue #107 can become
PR #111. Every factory-built PR's title therefore starts with its source:
`#<issue>: <title>` for a GitHub issue, `<KEY>: <title>` for a Linear
ticket (for example `AUT-2284: <title>`). The source is written as that
prefix (`#115` or `AUT-2284`) everywhere it is stored or shown.

A GitHub PR's source issue is a `#115:` prefix its title already has, else
the first closing reference in its body to an issue in the PR's own repo
(`Fixes #115`, `Fixes owner/repo#115` or the issue's URL), else the
feeder's branch name (`factory/issue-115`). A Linear ticket's source is its
identifier.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping

_CLOSING = re.compile(
    r"(?<![\w/])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
    r"(?:https://github\.com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)?#)"
    r"([1-9][0-9]{0,9})\b",
    re.IGNORECASE,
)
_FEEDER_BRANCH = re.compile(r"^factory/issue-([1-9][0-9]{0,9})$")
_TITLE_ISSUE = re.compile(r"^#([1-9][0-9]{0,9}):")
# The dispatcher's (repo, ticket) -> source issue and PR number, read by
# `orch status` and the dashboard.
SOURCES_TABLE = "ticket_sources"
TITLE_LIMIT = 256  # GitHub's longest PR title
SOURCE = re.compile(r"^(?:#[1-9][0-9]{0,9}|[A-Z][A-Z0-9]{0,9}-[1-9][0-9]{0,9})$")


def github_source(pull: Mapping[str, object], repo: str, number: str) -> str | None:
    """`#<issue>` for a GitHub PR in repo, or None when it names no source issue."""
    body = pull.get("body")
    head = pull.get("head")
    branch = head.get("ref") if isinstance(head, Mapping) else None
    title = pull.get("title")
    found = [
        *(_TITLE_ISSUE.findall(title) if isinstance(title, str) else ()),
        *(
            issue
            for url_repo, named_repo, issue in (
                _CLOSING.findall(body) if isinstance(body, str) else ()
            )
            if (url_repo or named_repo or repo).casefold() == repo.casefold()
        ),
        *(_FEEDER_BRANCH.findall(branch) if isinstance(branch, str) else ()),
    ]
    for issue in found:
        if issue != number:
            return f"#{issue}"
    return None


def prefixed(source: str, title: str) -> str:
    """title with `<source>: ` in front. Idempotent.

    A title that already starts with the source, bare or bracketed and with
    or without the colon (`AUT-2284 Fix`, `[AUT-2284] Fix`), is rewritten to
    the one form rather than prefixed twice.
    """
    if not SOURCE.fullmatch(source):
        raise ValueError(f"{source!r} is not an issue number or ticket key")
    rest = re.sub(
        rf"^\[?{re.escape(source)}\]?(?![0-9A-Za-z-])\s*:?\s*", "", title.strip(), count=1
    )
    return f"{source}: {rest}" if rest else source


def label(repo: str, ticket_id: str, source: str | None, pr_number: str | None) -> str:
    """`owner/repo#115 (PR #116)`: the source issue first, the PR second."""
    first = source.removeprefix("#") if source else ticket_id
    tail = f" (PR #{pr_number})" if pr_number and pr_number != first else ""
    return f"{repo}#{first}{tail}"


def read_sources(connection: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, str | None]]:
    """(repo casefolded, ticket id) -> (source, PR number); empty before any is recorded."""
    present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (SOURCES_TABLE,)
    ).fetchone()
    if present is None:
        return {}
    return {
        (str(repo).casefold(), str(ticket_id)): (
            str(source),
            str(pr_number) if pr_number is not None else None,
        )
        for repo, ticket_id, source, pr_number in connection.execute(
            f"SELECT repo, ticket_id, source, pr_number FROM {SOURCES_TABLE}"
        )
    }
