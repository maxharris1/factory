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
_QA_TAG = re.compile(r"^(?:qa\s*(?:\((?P<ids>[^)]*)\))?\s*:|\(qa\)|\[qa\])\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Journey:
    id: str
    new: bool = False


@dataclass(frozen=True)
class QaCriterion:
    text: str
    journeys: tuple[str, ...] = ()


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


SKILL_ROOTS = (".claude/skills", ".cursor/skills", ".agents/skills", ".codex/skills")


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
