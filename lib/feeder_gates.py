"""Feeder gates: `Blocked by`, `Depends on`, `Touches:` and `Conflicts with` lines (issue #156).

A ticket that carries its project's intake signal is fed only once every
blocker it names is done, fewer than its project's limit of tickets in
flight hold each of its paths, and nothing in flight shares a
conflicts-with edge with it. This module parses the lines and makes every
gate decision. It does no I/O: lib/feeder.py reads the statuses and writes
the rows.

Gate lines are explicit. A gate line is one whole line, outside fenced code,
that starts (after an optional list marker and `**`) with `Blocked by`,
`Depends on`, `Conflicts with` or `Touches:`. A `Depends on` line is a
`Blocked by` line under the intake template's name for it (#205). Refs on a
`Blocked by`, `Depends on` or `Conflicts with` line are `#N` (the ticket's
own repo; `PR #N` counts), `owner/repo#N` and GitHub issue or pull URLs.
Paths on a `Touches:` line split on commas and spaces. Prose is never read: a ref on the next line does not count.

A `Touches:` path that matches the project's factory.md `shared_paths`
(issue #261) never causes a collision wait; a `Conflicts with` edge still
holds.

Up to the project's factory.md `feeder.max_holders_per_path` tickets may
hold colliding paths at once (issue #269). The limit is counted on both
sides of a collision, so a held directory never gets more holders than the
limit either. A path matching `feeder.exclusive_paths` keeps one holder,
and a `Conflicts with` edge always does.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from lib.hold import glob_matches, glob_can_match_under

Tracker = Literal["github", "linear"]

ATTENTION_CYCLE = "blocker-cycle"
ATTENTION_CLOSED = "blocker-closed"
ATTENTION_MISSING = "blocker-missing"
ATTENTION_CRITICAL_PATH = "critical-path-blocker"
WAIT_REASONS = (ATTENTION_CYCLE, ATTENTION_CLOSED, ATTENTION_MISSING)
ATTENTION_REASONS = (*WAIT_REASONS, ATTENTION_CRITICAL_PATH)

FILE_PATH = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:py|md|mmd|yml|yaml|json|toml|ini|cfg|txt|"
    r"sh|sql|ts|tsx|js|jsx|mjs|css|scss|html|go|rs|rb|java|kt|swift|tla|svg))(?![\w/-])"
)

_FENCE = re.compile(r"^\s*(```|~~~)")
_GATE_LINE = re.compile(
    r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?\s*"
    r"(?P<kind>blocked\s+by|depends\s+on|conflicts\s+with|touches)\b(?P<rest>.*)$",
    re.IGNORECASE,
)
_LEAD = re.compile(r"^[\s:*_]*")
_REF = re.compile(
    r"https?://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/(?:issues|pull)/(?P<url_number>\d+)"
    r"|(?<![\w/.-])(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)"
    r"|(?<![\w/&#])#(?P<local>\d+)",
    re.IGNORECASE,
)
_PATH_LEAD = "`'\"([<*"
_PATH_TRAIL = "`'\")]>,;:.!?*"
_PATH_WORDS = frozenset({"and", "or", "&"})
_LINEAR_DONE = "completed"
_LINEAR_UNFINISHED = "canceled"


@dataclass(frozen=True, order=True)
class Ref:
    """A ticket or PR a gate line names, or a candidate itself.

    GitHub: repo is owner/repo lowercased, key the number as text.
    Linear: repo is the project's repo lowercased, key the identifier.
    """

    tracker: Tracker
    repo: str
    key: str

    def label(self, home: str) -> str:
        if self.tracker == "linear":
            return self.key
        if self.repo == home.lower():
            return f"#{self.key}"
        return f"{self.repo}#{self.key}"

    def sort_key(self) -> tuple[str, str, int, str]:
        number = int(self.key) if self.key.isdigit() else -1
        return (self.tracker, self.repo, number, self.key)


def github_ref(repo: str, number: int | str) -> Ref:
    return Ref("github", repo.lower(), str(int(number)))


@dataclass(frozen=True)
class GateLines:
    blocked_by: frozenset[Ref] = frozenset()
    touches: frozenset[str] = frozenset()
    conflicts_with: frozenset[Ref] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.blocked_by or self.touches or self.conflicts_with)


def parse_gate_lines(body: str, repo: str) -> GateLines:
    blocked: set[Ref] = set()
    touches: set[str] = set()
    conflicts: set[Ref] = set()
    fenced = False
    for line in (body or "").splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _GATE_LINE.match(line)
        if match is None:
            continue
        kind = " ".join(match.group("kind").lower().split())
        rest = match.group("rest")
        lead = _LEAD.match(rest)
        separator = lead.group(0) if lead else ""
        rest = rest[len(separator):]
        if kind == "touches":
            if ":" in separator:
                touches.update(_paths(rest))
        else:
            (conflicts if kind == "conflicts with" else blocked).update(_refs(rest, repo))
    return GateLines(frozenset(blocked), frozenset(touches), frozenset(conflicts))


def _refs(text: str, repo: str) -> set[Ref]:
    found: set[Ref] = set()
    for match in _REF.finditer(text):
        if match.group("url_repo"):
            found.add(github_ref(match.group("url_repo"), match.group("url_number")))
        elif match.group("repo"):
            found.add(github_ref(match.group("repo"), match.group("number")))
        else:
            found.add(github_ref(repo, match.group("local")))
    return found


def _paths(text: str) -> set[str]:
    found: set[str] = set()
    for token in re.split(r"[,\s]+", text):
        path = token.lstrip(_PATH_LEAD).rstrip(_PATH_TRAIL)
        while path.startswith("./"):
            path = path[2:]
        if path and path.lower() not in _PATH_WORDS:
            found.add(path)
    return found


def paths_collide(first: str, second: str) -> bool:
    if first == second:
        return True
    if first.endswith("/") and second.startswith(first):
        return True
    return second.endswith("/") and first.startswith(second)


def path_matches(patterns: Iterable[str], path: str) -> bool:
    """Whether path matches a shared_paths or exclusive_paths pattern: a glob
    with hold.paths rules, where a trailing / names a directory and
    everything under it."""
    path = path.removesuffix("/")
    for pattern in patterns:
        if pattern.endswith("/"):
            directory = pattern[:-1]
            if glob_matches(directory, path) or glob_matches(directory + "/**", path):
                return True
        elif glob_matches(pattern, path):
            return True
    return False


def matches_at_or_under(patterns: Iterable[str], path: str) -> bool:
    if path_matches(patterns, path):
        return True
    if not path.endswith("/"):
        return False
    return any(
        glob_can_match_under(form, path)
        for pattern in patterns
        for form in (
            (pattern[:-1], pattern[:-1] + "/**") if pattern.endswith("/") else (pattern,)
        )
    )


class Progress(Enum):
    DONE = "done"
    OPEN = "open"
    UNFINISHED = "unfinished"
    MISSING = "missing"


@dataclass(frozen=True)
class RefStatus:
    progress: Progress
    is_pull: bool = False
    detail: str = ""


def linear_status(state_type: str | None) -> RefStatus:
    if state_type == _LINEAR_DONE:
        return RefStatus(Progress.DONE)
    if state_type == _LINEAR_UNFINISHED:
        return RefStatus(Progress.UNFINISHED, detail="canceled")
    return RefStatus(Progress.OPEN)


@dataclass(frozen=True)
class Wait:
    reason: str
    attention: str | None = None


def blocker_waits(
    gated: Mapping[Ref, GateLines],
    statuses: Mapping[Ref, RefStatus],
) -> dict[Ref, Wait]:
    """Why each gated ticket waits on its blockers; absent means it need not.

    A blocker with no status (its read failed this poll) keeps the ticket
    waiting with no flag. A cycle among gated tickets flags every member and
    feeds none; a blocker closed without finishing, or one that does not
    exist, flags the ticket.
    """
    edges = {
        ref: sorted(
            (
                blocker
                for blocker in lines.blocked_by
                if blocker in gated and _progress(statuses, blocker) is not Progress.DONE
            ),
            key=Ref.sort_key,
        )
        for ref, lines in gated.items()
    }
    waits: dict[Ref, Wait] = {}
    for ref in _cyclic(edges):
        loop = " -> ".join(item.label(ref.repo) for item in _shortest_loop(ref, edges))
        waits[ref] = Wait(f"waiting on a blocker cycle: {loop}", ATTENTION_CYCLE)
    for ref, lines in gated.items():
        if ref in waits:
            continue
        parts: list[str] = []
        attention: str | None = None
        for blocker in sorted(
            lines.blocked_by, key=lambda item: (item.repo != ref.repo, item.sort_key())
        ):
            status = statuses.get(blocker)
            label = blocker.label(ref.repo)
            if status is None:
                parts.append(f"{label} (status unreadable)")
            elif status.progress is Progress.OPEN:
                parts.append(label)
            elif status.progress is Progress.UNFINISHED:
                parts.append(f"{label} ({status.detail or 'closed without finishing'})")
                attention = ATTENTION_CLOSED
            elif status.progress is Progress.MISSING:
                parts.append(f"{label} (not found)")
                attention = attention or ATTENTION_MISSING
        if parts:
            waits[ref] = Wait("waiting on " + ", ".join(parts), attention)
    return waits


def _progress(statuses: Mapping[Ref, RefStatus], ref: Ref) -> Progress | None:
    status = statuses.get(ref)
    return None if status is None else status.progress


def _cyclic(edges: Mapping[Ref, list[Ref]]) -> set[Ref]:
    """Every node on a cycle: Tarjan's strongly connected components."""
    index: dict[Ref, int] = {}
    low: dict[Ref, int] = {}
    stack: list[Ref] = []
    on_stack: set[Ref] = set()
    found: set[Ref] = set()
    counter = 0

    def visit(node: Ref) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for target in edges.get(node, ()):
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            component: list[Ref] = []
            while True:
                item = stack.pop()
                on_stack.discard(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1 or node in edges.get(node, ()):
                found.update(component)

    for node in sorted(edges, key=Ref.sort_key):
        if node not in index:
            visit(node)
    return found


def _shortest_loop(start: Ref, edges: Mapping[Ref, list[Ref]]) -> list[Ref]:
    """start -> ... -> start along blocker edges, fewest steps."""
    previous: dict[Ref, Ref] = {}
    frontier = [start]
    while frontier:
        following: list[Ref] = []
        for node in frontier:
            for target in edges.get(node, ()):
                if target == start:
                    path = [node]
                    while path[-1] != start:
                        path.append(previous[path[-1]])
                    return [*reversed(path), start]
                if target not in previous:
                    previous[target] = node
                    following.append(target)
        frontier = following
    return [start, start]


@dataclass(frozen=True)
class PathPolicy:
    shared: tuple[str, ...] = ()
    exclusive: tuple[str, ...] = ()
    max_holders: int = 1


@dataclass
class Holds:
    """Paths and conflicts-with edges held by tickets in flight.

    Starts from fed tickets whose PR is still open, then grows as this poll
    selects tickets to feed, in priority order, so the earlier ticket keeps
    a contested path.
    """

    held: list[tuple[Ref, frozenset[Ref], GateLines, str]] = field(default_factory=list)
    policies: Mapping[str, PathPolicy] = field(default_factory=dict)

    def take(
        self, holder: Ref, aliases: Iterable[Ref], lines: GateLines, note: str = "conflicts with it"
    ) -> None:
        self.held.append((holder, frozenset({holder, *aliases}), lines, note))

    def alias(self, holder: Ref, name: Ref) -> None:
        self.held = [
            (ref, names | {name} if ref == holder else names, lines, note)
            for ref, names, lines, note in self.held
        ]

    def drop(self, holder: Ref) -> None:
        self.held = [item for item in self.held if item[0] != holder]

    def collision(self, ref: Ref, lines: GateLines, aliases: Iterable[Ref] = ()) -> Wait | None:
        """Why ref waits on a holder; aliases are other refs it goes by (its PR)."""
        own = frozenset({ref, *aliases})
        policy = self.policies.get(ref.repo, PathPolicy())
        for holder, names, held, note in self.held:
            if names & own:
                continue
            for path in sorted(lines.touches) if holder.repo == ref.repo else ():
                if path_matches(policy.shared, path):
                    continue
                for other in sorted(held.touches):
                    if path_matches(policy.shared, other) or not paths_collide(path, other):
                        continue
                    longer = path if len(path) >= len(other) else other
                    exclusive = matches_at_or_under(policy.exclusive, longer)
                    limit = 1 if exclusive else policy.max_holders
                    groups = ({holder},) if limit == 1 else (
                        self._holders_on(ref.repo, own, path, policy),
                        self._holders_on(ref.repo, own, other, policy),
                    )
                    for group in groups:
                        if len(group) >= limit:
                            return Wait(
                                f"waiting on file {longer} held by {labels(group, ref.repo)}"
                            )
            if lines.conflicts_with & names or own & held.conflicts_with:
                return Wait(f"waiting on {holder.label(ref.repo)} ({note})")
        return None

    def co_holders(self, ref: Ref, lines: GateLines) -> dict[str, tuple[Ref, ...]]:
        """The other holders of each of ref's paths that are not shared."""
        own = frozenset({ref})
        policy = self.policies.get(ref.repo, PathPolicy())
        found: dict[str, tuple[Ref, ...]] = {}
        for path in sorted(lines.touches):
            if path_matches(policy.shared, path):
                continue
            group = self._holders_on(ref.repo, own, path, policy)
            if group:
                found[path] = tuple(sorted(group, key=Ref.sort_key))
        return found

    def _holders_on(
        self, repo: str, own: frozenset[Ref], path: str, policy: PathPolicy
    ) -> set[Ref]:
        return {
            holder
            for holder, names, held, _note in self.held
            if holder.repo == repo
            and not names & own
            and any(
                paths_collide(path, other) and not path_matches(policy.shared, other)
                for other in held.touches
            )
        }


def labels(refs: Iterable[Ref], home: str) -> str:
    return ", ".join(item.label(home) for item in sorted(refs, key=Ref.sort_key))
