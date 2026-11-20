"""QA journeys a ticket names at intake (#1029, F-21a).

A ticket's `Features:` line names journeys from the project's feature map,
the `features/` folder of its qa.verify skill: `<id>` for one the map has,
`new:<id>` for one the ticket adds. A `qa:` tag on an acceptance criterion
marks it for QA; `qa(<id>, ...):` binds it to some of those journeys, and a
bare tag binds it to all of them. Everything here is pure; the feeder reads
the map.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

JourneyCode = Literal[
    "features-unknown", "features-unbound", "features-exists", "features-no-verify", "qa-no-features"
]
Problem = tuple[JourneyCode, str]

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_GRAMMAR = "`[a-z0-9][a-z0-9-]{0,63}`"
_NEW = re.compile(r"new:", re.IGNORECASE)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_VERIFY_LINE = re.compile(r"^- Verify skill: `(?P<verify>[^`\s]+)`$")
_BASE_LINE = re.compile(r"^- Base: `(?P<sha>[0-9a-f]{40,64})`$")
_JOURNEY_LINE = re.compile(rf"^- `(?P<new>new:)?(?P<id>{ID.pattern})`(?: \(.*\))?$")
_CHECK_LINE = re.compile(r"^- (?P<ac>ac-[1-9][0-9]*) \[(?P<ids>[^\]]+)\]:(?: (?P<text>.*))?$")
_QA_TAG = re.compile(r"^(?:qa\s*(?:\((?P<ids>[^)]*)\))?\s*:|\(qa\)|\[qa\])\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Journey:
    id: str
    new: bool = False


@dataclass(frozen=True)
class QaCriterion:
    text: str
    journeys: tuple[str, ...] = ()


@dataclass(frozen=True)
class QaCheck:
    ac: str
    journeys: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class QaPlan:
    verify: str
    base_sha: str
    journeys: tuple[Journey, ...]
    checks: tuple[QaCheck, ...]

    def render(self) -> list[str]:
        lines = ["### QA journeys", f"- Verify skill: `{self.verify}`", f"- Base: `{self.base_sha}`"]
        lines += [
            f"- `new:{j.id}` (this PR adds `features/{j.id}.md` and its README row)" if j.new else f"- `{j.id}`"
            for j in self.journeys
        ]
        if self.checks:
            lines += ["", "### QA criteria"]
            lines += [f"- {c.ac} [{', '.join(c.journeys)}]: {c.text}".rstrip() for c in self.checks]
        return lines

    @classmethod
    def parse(cls, text: str | None) -> QaPlan | None:
        """The plan in a brief's `### QA journeys` and `### QA criteria` sections, else None.

        Fenced lines are skipped so a quoted ticket cannot forge a plan, and any line off
        the grammar makes the whole block None.
        """
        sections = _sections(_unfenced(text or ""))
        titles = [title for title, _ in sections]
        if "QA journeys" not in titles:
            return None
        at = titles.index("QA journeys")
        found = sections[at][1]
        following = sections[at + 1] if at + 1 < len(sections) else (None, [])
        if len(found) < 3:
            return None
        verify, base = _VERIFY_LINE.match(found[0]), _BASE_LINE.match(found[1])
        listed = [_JOURNEY_LINE.match(line) for line in found[2:]]
        checks = [_CHECK_LINE.match(line) for line in (following[1] if following[0] == "QA criteria" else [])]
        if verify is None or base is None or None in listed or None in checks:
            return None
        return cls(
            verify["verify"],
            base["sha"],
            tuple(Journey(m["id"], new=bool(m["new"])) for m in listed if m),
            tuple(
                QaCheck(m["ac"], tuple(i.strip() for i in m["ids"].split(",")), m["text"] or "")
                for m in checks
                if m
            ),
        )

    def edited(self, criteria: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
        """The first (ac, reason) where the criteria's qa tags no longer match this plan's checks, else None."""
        every = tuple(journey.id for journey in self.journeys)
        checks = {check.ac: check for check in self.checks}
        for ac, text in criteria:
            tag = qa_tag(text)
            check = checks.pop(ac, None)
            if (tag and (tag.journeys or every)) != (check and check.journeys):
                return ac, f"{ac}'s qa tag changed after the brief"
        for ac in checks:
            return ac, f"{ac} is not in the ticket any more"
        return None


def qa_plan(
    verify: str, base_sha: str, journeys: tuple[Journey, ...], criteria: Iterable[tuple[str, str]]
) -> QaPlan:
    every = tuple(journey.id for journey in journeys)
    checks = tuple(
        QaCheck(ac, tag.journeys or every, tag.text)
        for ac, item in criteria
        if (tag := qa_tag(item)) is not None
    )
    return QaPlan(verify, base_sha, journeys, checks)


def _unfenced(text: str) -> list[str]:
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        mark = _FENCE.match(line)
        if fence is None and mark:
            fence = mark.group(1)
        elif fence is None:
            lines.append(line.rstrip())
        elif mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence) and line.strip() == mark.group(1):
            fence = None
    return lines


def _sections(lines: list[str]) -> list[tuple[str | None, list[str]]]:
    sections: list[tuple[str | None, list[str]]] = []
    for line in lines:
        if line.startswith("#"):
            sections.append((line[4:].strip() if line.startswith("### ") else None, []))
        elif sections and line.strip():
            sections[-1][1].append(line)
    return sections


def parse_features(value: str | None) -> tuple[tuple[Journey, ...], tuple[str, ...]]:
    """The journeys a `Features:` value names, and the tokens that are not ids."""
    journeys: list[Journey] = []
    bad: list[str] = []
    for token in re.split(r"[\s,]+", (value or "").replace("`", "")):
        if not token or token.lower() == "none":
            continue
        new = _NEW.match(token)
        journey = Journey(token[new.end():], new=True) if new else Journey(token)
        if not ID.fullmatch(journey.id):
            if token not in bad:
                bad.append(token)
        elif journey not in journeys:
            journeys.append(journey)
    return tuple(journeys), tuple(bad)


def qa_tag(item: str) -> QaCriterion | None:
    tag = _QA_TAG.match(item)
    if tag is None:
        return None
    ids = (tag.group("ids") or "").replace("`", "").split(",")
    return QaCriterion(item[tag.end():].strip(), tuple(i.strip() for i in ids if i.strip()))


def _names(ids: Iterable[str]) -> str:
    return ", ".join(f"`{i}`" for i in ids)


def problems(
    journeys: tuple[Journey, ...], bad: tuple[str, ...], qa_criteria: tuple[QaCriterion, ...]
) -> list[Problem]:
    """What is wrong with the journeys and qa criteria as the body states them."""
    found: list[Problem] = []
    if bad:
        found.append(("features-unknown", f"`Features:` names {_names(bad)}, not ids of the form {_GRAMMAR}"))
    listed = {journey.id for journey in journeys}
    bound = dict.fromkeys(i for criterion in qa_criteria for i in criterion.journeys)
    if qa_criteria and not journeys:
        found.append(("qa-no-features", "qa criteria but no `Features:` line naming the journeys QA drives"))
    elif unbound := [i for i in bound if i not in listed]:
        found.append((
            "features-unbound",
            f"a qa criterion names {_names(unbound)}, which the `Features:` line does not list",
        ))
    return found


def skill_dir(paths: Iterable[str], verify: str) -> str | None:
    tail = f"/{verify}/SKILL.md"
    found = min((path for path in paths if path.endswith(tail)), default=None)
    return None if found is None else found.removesuffix("/SKILL.md")


def map_ids(names: Iterable[str]) -> frozenset[str]:
    return frozenset(
        name[:-3] for name in names if name.endswith(".md") and ID.fullmatch(name[:-3])
    )


NO_VERIFY: Problem = (
    "features-no-verify",
    "`Features:` names journeys but factory.md sets no `qa.verify` skill to hold the feature map",
)


def map_problems(journeys: tuple[Journey, ...], ids: frozenset[str]) -> list[Problem]:
    found: list[Problem] = []
    unknown = [journey.id for journey in journeys if not journey.new and journey.id not in ids]
    if unknown:
        found.append((
            "features-unknown",
            f"the feature map has no {_names(unknown)} (write `new:<id>` for a journey this ticket adds)",
        ))
    exists = [journey.id for journey in journeys if journey.new and journey.id in ids]
    if exists:
        found.append(("features-exists", f"`new:` {_names(exists)} is already in the feature map"))
    return found
