"""Follow-up findings (issue #259). Pure text and policy, no I/O.

From factory.md review.followup_from_round on, a reviewer tags a variant of
an already-raised rule `[follow-up]` instead of `[blocking]`. The brief and
the poller both ask followups_active, so they cannot disagree on the round.
The poller parks a trusted verdict's `[follow-up]` items on one unlabelled
issue per PR: the first verdict files it (followup_issue), later verdicts add
a comment to it while it stays open and out of the queue (followup_addition),
and each verdict links it from the PR (link_comment). Each write carries
a hidden marker keyed on the verdict comment id, so a retry after a crash
finds the earlier write instead of repeating it.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

from lib.feeder_gates import FILE_PATH

ISSUE_BODY_LIMIT = 65536
_MARKDOWN = MarkdownIt("commonmark")
_TAG = "[follow-up]"
_CUT_NOTE = "Some findings were cut to fit GitHub's issue size limit; the verdict has them all."
_CITED_FILE = re.compile(
    r"(?<![\w/.:@-])(?=[\w./@-]*[A-Za-z])(?:[\w.@-]+/)+[\w.@-]*[\w-](?![\w/@-]|#\d)|"
    r"(?<![\w/.:@-])\.[A-Za-z](?:[\w.-]*[\w-])?(?![\w/@-]|#\d)|" + FILE_PATH.pattern
)


def followups_active(followup_from: int | None, round_number: int | None) -> bool:
    if followup_from is None or round_number is None:
        return False
    return round_number >= followup_from


def followup_findings(body: str) -> tuple[str, ...]:
    text = re.sub(r"\r\n?", "\n", body or "")
    lines = text.split("\n")
    found: list[str] = []
    for token in _MARKDOWN.parse(text):
        if token.type != "list_item_open" or token.level != 1 or token.markup not in (".", ")"):
            continue
        start, end = token.map
        item = lines[start:end]
        if _TAG not in item[0].lower():
            continue
        while not item[-1].strip():
            item.pop()
        found.append("\n".join(item))
    return tuple(found)


def followup_marker(comment_id: str) -> str:
    return f"<!-- orch-followup verdict={comment_id} -->"


def link_marker(comment_id: str) -> str:
    return f"<!-- orch-followup-link verdict={comment_id} -->"


def followup_issue(
    repo: str,
    pr: int,
    round_number: int,
    verdict_url: str,
    comment_id: str,
    findings: tuple[str, ...],
    risk: str,
) -> tuple[str, str]:
    title = f"Follow-ups from #{pr} r{round_number}"
    files = dict.fromkeys(match.group(0) for finding in findings for match in _CITED_FILE.finditer(finding))
    touches = f"Touches: {', '.join(files)}\n" if files else ""
    criteria = "".join(f"- Finding {n} below is fixed.\n" for n in range(1, len(findings) + 1))
    head = (
        f"Follow-up findings from the round {round_number} review verdict on "
        f"{repo}#{pr}: {verdict_url}\nBlocked by: #{pr}\n\n"
        f"Plan first: no\nDepends on: #{pr}\nRisk: {risk}\nConflicts with: none\n{touches}\n"
        f"## Acceptance criteria\n\n{criteria}\n## Findings\n\n"
    )
    return title, _with_findings(head, findings, comment_id)


def followup_addition(
    repo: str,
    pr: int,
    round_number: int,
    verdict_url: str,
    comment_id: str,
    findings: tuple[str, ...],
) -> str:
    head = (
        f"Follow-up findings from the round {round_number} review verdict on "
        f"{repo}#{pr}: {verdict_url}\n\n## Findings\n\n"
    )
    return _with_findings(head, findings, comment_id)


def _with_findings(head: str, findings: tuple[str, ...], comment_id: str) -> str:
    marker = followup_marker(comment_id)
    quoted = "\n\n".join(_defuse_gate_lines(finding) for finding in findings)
    room = ISSUE_BODY_LIMIT - len(head) - len(marker) - 2
    if len(quoted) > room:
        room -= len(_CUT_NOTE) + 2
        cut = quoted.rfind("\n", 0, room)
        quoted = quoted[: cut if cut > 0 else room].rstrip() + "\n\n" + _CUT_NOTE
    return f"{head}{quoted}\n\n{marker}"


def link_comment(comment_id: str, issue_number: int, round_number: int) -> str:
    return (
        f"Follow-ups from review round {round_number} are parked on "
        f"#{issue_number}; they do not block this PR.\n\n{link_marker(comment_id)}"
    )


def has_marker(body: str | None, marker: str) -> bool:
    return any(line.strip() == marker for line in (body or "").splitlines())


def _defuse_gate_lines(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())
