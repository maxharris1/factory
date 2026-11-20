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
from dataclasses import dataclass

from markdown_it import MarkdownIt

from lib.feeder_gates import FILE_PATH
from lib.handoff import plan_estimate

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


def followup_start(followup_from: int | None, block_on: str) -> int | None:
    return 1 if block_on == "user-impact" else followup_from


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


SPLIT_LIMIT = 4_000
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*(?:(?<=\s)#+\s*)?$")
_SPLIT_CRITERION = re.compile(r"^\s*[-*+]\s+(ac-\w+:.*\S)\s*$", re.IGNORECASE)
_SPLIT_DEPENDS = re.compile(r"\bdepends\s+on:\s*(.*?)\s*\.?\s*$", re.IGNORECASE)
_SPLIT_REFS = re.compile(r"(?:parts?\s*)?\d+(?:\s*(?:,|and|&)\s*(?:parts?\s*)?\d+)*", re.IGNORECASE)


@dataclass(frozen=True)
class SplitPart:
    title: str
    criteria: tuple[str, ...]
    estimate: int | None
    depends: tuple[int, ...] | None


def _split_section(plan_text: str) -> list[str]:
    section: list[str] = []
    level = 0
    for line in plan_text.splitlines():
        heading = _HEADING.match(line.strip())
        if level and heading and len(heading.group(1)) <= level:
            break
        if level:
            section.append(line)
        elif heading and heading.group(2).lower() == "proposed split":
            level = len(heading.group(1))
            section.append(line)
    return section


def proposed_split(plan_text: str) -> str:
    section = _split_section(plan_text)
    if not section:
        return "no split proposed"
    text = "\n".join(section).strip()
    if len(text) > SPLIT_LIMIT:
        return f"{text[:SPLIT_LIMIT]}\n[cut at {SPLIT_LIMIT} characters; read the rest in the plan]"
    return text


def split_parts(plan_text: str) -> tuple[SplitPart, ...]:
    section = _split_section(plan_text)
    if not section:
        return ()
    child = len(_HEADING.match(section[0].strip()).group(1)) + 1
    blocks: list[tuple[str, list[str]]] = []
    for line in section[1:]:
        heading = _HEADING.match(line.strip())
        if heading and len(heading.group(1)) == child:
            blocks.append((heading.group(2), []))
        elif blocks:
            blocks[-1][1].append(line)
    kept = {}
    for number, (title, lines) in enumerate(blocks, 1):
        criteria = tuple(match.group(1) for line in lines if (match := _SPLIT_CRITERION.match(line)))
        if criteria:
            kept[number] = (title, criteria, [line for line in lines if not _SPLIT_CRITERION.match(line)])
    renumber = {old: new for new, old in enumerate(kept, 1)}
    parts = []
    for old, (title, criteria, lines) in kept.items():
        depends = _split_depends(lines, old)
        if depends is not None and not set(depends) <= renumber.keys():
            depends = None
        parts.append(SplitPart(
            title=title,
            criteria=criteria,
            estimate=plan_estimate("\n".join(lines)),
            depends=None if depends is None else tuple(renumber[number] for number in depends),
        ))
    return tuple(parts) if len(parts) >= 2 else ()


def _split_depends(lines: list[str], number: int) -> tuple[int, ...] | None:
    for line in lines:
        match = _SPLIT_DEPENDS.search(line)
        if match is None:
            continue
        value = match.group(1)
        if value.lower() == "none":
            return ()
        if not _SPLIT_REFS.fullmatch(value):
            return None
        refs = tuple(sorted({int(ref) for ref in re.findall(r"\d+", value)}))
        return refs if all(0 < ref < number for ref in refs) else None
    return None


def split_marker(comment_id: str, part: int) -> str:
    return f"<!-- orch-split plan={comment_id} part={part} -->"


def split_link_marker(comment_id: str) -> str:
    return f"<!-- orch-split-link plan={comment_id} -->"


def split_issue(
    issue: str,
    plan_url: str,
    comment_id: str,
    number: int,
    part: SplitPart,
    risk: str,
    depends_on: tuple[int, ...],
) -> tuple[str, str]:
    depends = ", ".join(f"#{ref}" for ref in depends_on) or "none"
    criteria = "".join(f"- {criterion}\n" for criterion in part.criteria)
    estimate = "" if part.estimate is None else f"Estimate: {part.estimate}\n\n"
    body = (
        f"Split from #{issue} (plan {plan_url}).\n\n"
        f"Risk: {risk}\nPlan first: no\nDepends on: {depends}\nConflicts with: none\n\n"
        f"## Acceptance criteria\n\n{criteria}\n{estimate}{split_marker(comment_id, number)}"
    )
    return (part.title or f"Part {number} of #{issue}")[:256], body


def split_dependencies(parts: tuple[SplitPart, ...]) -> tuple[tuple[int, ...], ...]:
    return tuple(
        (number - 1,) if part.depends is None and number > 1 else part.depends or ()
        for number, part in enumerate(parts, 1)
    )


def split_comment(comment_id: str, issues: tuple[int, ...]) -> str:
    filed = ", ".join(f"#{issue}" for issue in issues)
    return f"The plan's proposed split is filed as {filed}.\n\n{split_link_marker(comment_id)}"
