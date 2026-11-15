"""Follow-up findings (issue #259). Pure text and policy, no I/O.

From factory.md review.followup_from_round on, a reviewer tags a variant of
an already-raised rule `[follow-up]` instead of `[blocking]`. The brief and
the poller both ask followups_active, so they cannot disagree on the round.
The poller files a trusted verdict's `[follow-up]` items as one new ticket
(followup_issue) and links it from the PR (link_comment). Each write carries
a hidden marker keyed on the verdict comment id, so a retry after a crash
finds the earlier write instead of repeating it.
"""

from __future__ import annotations

import re

ISSUE_BODY_LIMIT = 65536
_ITEM = re.compile(r"^( *)\d+[.)]\s")
_HEADING = re.compile(r"^#{1,6}\s")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TAG = "[follow-up]"
_CUT_NOTE = "Some findings were cut to fit GitHub's issue size limit; the verdict has them all."


def followups_active(followup_from: int | None, round_number: int | None) -> bool:
    if followup_from is None or round_number is None:
        return False
    return round_number >= followup_from


def followup_findings(body: str) -> tuple[str, ...]:
    items: list[list[str]] = []
    current: list[str] | None = None
    indent = 0
    fenced = False
    for line in (body or "").splitlines():
        fence = _FENCE.match(line)
        item = _ITEM.match(line)
        if fenced or fence:
            fenced = fenced != bool(fence)
            if current is not None:
                current.append(line)
        elif item and (current is None or len(item.group(1)) <= indent):
            current = [line]
            indent = len(item.group(1))
            items.append(current)
        elif _HEADING.match(line):
            current = None
        elif current is not None:
            current.append(line)
    found: list[str] = []
    for lines in items:
        if _TAG not in lines[0].lower():
            continue
        while lines and not lines[-1].strip():
            lines.pop()
        found.append("\n".join(lines))
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
) -> tuple[str, str]:
    title = f"Follow-ups from #{pr} r{round_number}"
    head = (
        f"Follow-up findings from the round {round_number} review verdict on "
        f"{repo}#{pr}: {verdict_url}\nBlocked by: #{pr}\n\n"
    )
    marker = followup_marker(comment_id)
    quoted = "\n\n".join(_defuse_gate_lines(finding) for finding in findings)
    room = ISSUE_BODY_LIMIT - len(head) - len(marker) - 2
    if len(quoted) > room:
        room -= len(_CUT_NOTE) + 2
        cut = quoted.rfind("\n", 0, room)
        quoted = quoted[: cut if cut > 0 else room].rstrip() + "\n\n" + _CUT_NOTE
    return title, f"{head}{quoted}\n\n{marker}"


def link_comment(comment_id: str, issue_number: int, round_number: int) -> str:
    return (
        f"Follow-ups from review round {round_number} are filed as "
        f"#{issue_number}; they do not block this PR.\n\n{link_marker(comment_id)}"
    )


def has_marker(body: str | None, marker: str) -> bool:
    return any(line.strip() == marker for line in (body or "").splitlines())


def _defuse_gate_lines(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())
