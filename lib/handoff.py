"""orch:v1 handoff comments (docs/SPEC.md section 5).

The header is one HTML comment. Canonical key order is KEYS. v1 keys are
the example keys plus state and builder, which GitHub-issue repos also
show as labels and which Aut0 carries in the header instead, and reviewer.
A go's builder and reviewer route that ticket (issue #206). A verdict may
name repeat=<ac-n|f-n> and unsat=<ac-n>. A plan may name lines=<n>, its
expected changed lines, and unsat=<ac-n>, the criterion it left unmapped.

labels_for returns the labels a later dispatcher would apply. It does
not write anything. tickets linear, and the Aut0 repo, return no labels.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from lib.schema import AUT0_REPO, HARNESSES, TICKETS

KINDS = (
    "brief",
    "questions",
    "answers",
    "go",
    "plan",
    "ready",
    "verdict",
    "ok",
    "decline",
    "done",
    "stuck",
    "retry",
)

# state and builder are how Aut0 keeps orch:state and orch:builder in the header.
KEYS = (
    "kind",
    "from",
    "role",
    "round",
    "head",
    "ac",
    "verdict",
    "risk",
    "state",
    "builder",
    "reviewer",
    "repeat",
    "unsat",
    "lines",
)

# Kebab-case forms of the section 4.1 states.
STATES = (
    "intake",
    "refused",
    "questions",
    "waiting-max",
    "planning",
    "plan-check",
    "building",
    "review",
    "fixing",
    "ship",
    "merged",
    "handed-off",
    "merge-queue",
    "updating",
    "waiting-external",
    "awaiting-ok",
    "post-merge",
    "done",
    "stuck",
    "blocked",
    "capped",
    "abandoned",
    "handed-off-externally",
)

VERDICTS = ("REVISE", "APPROVE", "BLOCKED")
AC_VALUES = ("met", "unmet")
RISKS = ("low", "high")

REQUIRED_BY_KIND = {
    "brief": frozenset(),
    "questions": frozenset(),
    "answers": frozenset(),
    "go": frozenset(),
    "plan": frozenset({"ac", "round"}),
    "ready": frozenset({"head"}),
    "verdict": frozenset({"verdict"}),
    "ok": frozenset({"head"}),
    "decline": frozenset(),
    "done": frozenset(),
    "stuck": frozenset(),
    "retry": frozenset(),
}

_HTML_LINE = re.compile(r"^<!--\s*orch:v1\b\s*(.*?)\s*-->$")
_VISIBLE_LINE = re.compile(r"^`orch:v1\b\s*(.*?)\s*`$")
_ATTR = re.compile(r"^[a-z][a-z0-9_]*=[A-Za-z0-9_.:/-]+$")
_TOKEN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ROUND = re.compile(r"^([1-9][0-9]{0,8})(?:/([1-9][0-9]{0,8}))?$")
_HEAD = re.compile(r"^[0-9a-f]{7,40}$")
_REPEAT = re.compile(r"^(?:ac|f)-[1-9][0-9]*$")
_UNSAT = re.compile(r"^ac-[1-9][0-9]*$")
_LINES = re.compile(r"^(?:0|[1-9][0-9]{0,8})$")


class HandoffError(ValueError):
    """The comment is not a valid orch:v1 handoff."""


class UnmetApproveError(HandoffError):
    """A verdict=APPROVE that says ac=unmet."""


@dataclass(frozen=True)
class Handoff:
    fields: tuple[tuple[str, str], ...]
    body: str

    def as_dict(self) -> dict[str, str]:
        return dict(self.fields)


def parse_header_line(line: str) -> dict[str, str]:
    stripped = line.strip()
    match = _HTML_LINE.fullmatch(stripped) or _VISIBLE_LINE.fullmatch(stripped)
    if match is None:
        raise HandoffError("not an orch:v1 header")
    raw = match.group(1) or ""
    found: dict[str, str] = {}
    if raw != "":
        for token in raw.split():
            if _ATTR.fullmatch(token) is None:
                raise HandoffError(f"bad attribute {token!r}")
            key, value = token.split("=", 1)
            if key not in KEYS:
                raise HandoffError(f"unknown key {key!r}")
            if key in found:
                raise HandoffError(f"duplicate key {key!r}")
            found[key] = value
    if "kind" not in found:
        raise HandoffError("missing required key 'kind'")
    kind = found["kind"]
    if kind not in KINDS:
        raise HandoffError(f"unknown kind {kind!r}")
    for key, value in found.items():
        _check_value(key, value)
    if kind != "verdict" and "verdict" in found:
        raise HandoffError("verdict is only valid on kind=verdict")
    if kind != "verdict" and "repeat" in found:
        raise HandoffError("repeat is only valid on kind=verdict")
    if kind not in ("plan", "verdict") and "unsat" in found:
        raise HandoffError("unsat is only valid on kind=plan or kind=verdict")
    if kind != "plan" and "lines" in found:
        raise HandoffError("lines is only valid on kind=plan")
    if kind not in ("plan", "verdict") and "ac" in found:
        raise HandoffError("ac is only valid on kind=plan or kind=verdict")
    for key in sorted(REQUIRED_BY_KIND[kind]):
        if key not in found:
            raise HandoffError(f"missing required key {key!r}")
    if kind == "verdict" and found.get("verdict") == "APPROVE" and "head" not in found:
        raise HandoffError("missing required key 'head'")
    if kind == "verdict" and found.get("verdict") == "APPROVE" and found.get("ac") == "unmet":
        raise UnmetApproveError(
            "APPROVE needs ac=met; got ac=unmet. Post verdict=REVISE with ac=unmet,"
            " or ac=met if every criterion is met"
        )
    return {key: found[key] for key in KEYS if key in found}


def format_header(fields: Mapping[str, str], *, visible: bool = False) -> str:
    extra = [key for key in fields if key not in KEYS]
    if extra:
        raise HandoffError(f"unknown key {extra[0]!r}")
    for key in KEYS:
        if key not in fields:
            continue
        if _unsafe_value(fields[key]):
            raise HandoffError(f"bad {key} {fields[key]!r}")
    parts = ["orch:v1"]
    for key in KEYS:
        if key in fields:
            parts.append(f"{key}={fields[key]}")
    marker = " ".join(parts)
    line = f"`{marker}`" if visible else f"<!-- {marker} -->"
    parsed = parse_header_line(line)
    expected = {key: fields[key] for key in KEYS if key in fields}
    if parsed != expected:
        raise HandoffError("header does not round-trip")
    return line


def parse_comment(text: str) -> Handoff:
    if text.strip() == "":
        raise HandoffError("not an orch:v1 header")
    normalized = text if text.endswith("\n") else text + "\n"
    _, _, rest = normalized.partition("\n")
    fields = parse_header_line(first_line(text))
    return Handoff(fields=tuple(fields.items()), body=rest)


def first_line(text: str) -> str:
    return text.partition("\n")[0]


def matching_head(reported: str, current: str | None) -> str | None:
    """current when a handoff's head names it: either is a prefix of the
    other, both at least 7 characters long."""
    if current is None or min(len(reported), len(current)) < 7:
        return None
    if current.startswith(reported) or reported.startswith(current):
        return current
    return None


def repeat_id(handoff: Handoff) -> str | None:
    """The verdict's repeat id: the header's repeat=, else a first body line
    that is exactly repeat=<ac-n|f-n>."""
    fields = handoff.as_dict()
    if "repeat" in fields:
        return fields["repeat"]
    first = next((line.strip() for line in handoff.body.splitlines() if line.strip()), "")
    key, _, value = first.partition("=")
    return value if key == "repeat" and _REPEAT.fullmatch(value) else None


def format_comment(handoff: Handoff, *, visible: bool = False) -> str:
    header = format_header(handoff.as_dict(), visible=visible)
    if handoff.body == "":
        return header + "\n"
    if handoff.body.endswith("\n"):
        return header + "\n" + handoff.body
    return header + "\n" + handoff.body + "\n"


def labels_for(
    handoff: Handoff,
    *,
    tickets: str,
    repo: str | None = None,
) -> tuple[str, ...]:
    """Labels for a GitHub-issue repo. Linear and Aut0 get none."""
    if tickets not in TICKETS:
        raise HandoffError("tickets must be github or linear")
    if tickets == "linear" or (
        repo is not None and repo.lower() == AUT0_REPO.lower()
    ):
        return ()
    fields = handoff.as_dict()
    labels: list[str] = []
    if "state" in fields:
        labels.append(f"orch:state/{fields['state']}")
    if "round" in fields:
        labels.append("orch:round/" + fields["round"].split("/", 1)[0])
    if "risk" in fields:
        labels.append(f"orch:risk/{fields['risk']}")
    if "builder" in fields:
        labels.append(f"orch:builder/{fields['builder']}")
    return tuple(labels)


def _unsafe_value(value: str) -> bool:
    return any(character.isspace() for character in value) or "=" in value or "-->" in value


def _check_value(key: str, value: str) -> None:
    if key == "kind":
        if value not in KINDS:
            raise HandoffError(f"unknown kind {value!r}")
        return
    if key in ("from", "role"):
        if _TOKEN.fullmatch(value) is None:
            raise HandoffError(f"bad {key} {value!r}")
        return
    if key == "round":
        # Syntax only: whether N is within the cap is the dispatcher's call,
        # since only it knows the extra rounds Max approved (#105).
        if _ROUND.fullmatch(value) is None:
            raise HandoffError(f"bad round {value!r}")
        return
    if key == "head":
        if _HEAD.fullmatch(value) is None:
            raise HandoffError(f"bad head {value!r}")
        return
    if key == "ac":
        if value not in AC_VALUES:
            raise HandoffError(f"bad ac {value!r}")
        return
    if key == "verdict":
        if value not in VERDICTS:
            raise HandoffError(f"bad verdict {value!r}")
        return
    if key == "risk":
        if value not in RISKS:
            raise HandoffError(f"bad risk {value!r}")
        return
    if key == "state":
        if value not in STATES:
            raise HandoffError(f"bad state {value!r}")
        return
    if key in ("builder", "reviewer"):
        if value not in HARNESSES:
            raise HandoffError(f"bad {key} {value!r}")
        return
    if key == "repeat":
        if _REPEAT.fullmatch(value) is None:
            raise HandoffError(f"bad repeat {value!r}")
        return
    if key == "lines":
        if _LINES.fullmatch(value) is None:
            raise HandoffError(f"bad lines {value!r}")
        return
    if key == "unsat":
        if _UNSAT.fullmatch(value) is None:
            raise HandoffError(f"bad unsat {value!r}")
        return
    raise HandoffError(f"unknown key {key!r}")
