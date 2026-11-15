"""Per-PR human hold before a ship: merge (issue #86, docs/SPEC.md 4.2 T4H).

factory.md's optional hold: names a label (default factory:hold), an
approver (default max, a GitHub login that must also be a trusted_logins
key) and paths (globs). On a ship: merge repo, a covered APPROVE with
green CI does not merge while the PR is held:

- label: the PR carries the hold label, or the label's latest labeled /
  unlabeled event is a removal by someone other than the approver (a
  removal the approver did not make never clears the hold);
- paths: the PR's diff against base touches a hold.paths glob, read at
  the current head on every poll, so a PR that grows into those files
  gets held.

The hold clears on a kind=ok head=<40-char sha> comment from the approver
whose head covers the current head by the ship rule (the same commit, or
an equal git patch-id --stable), or, for a label-only hold, by the
approver removing the label. An ok does not clear a label hold whose
label was labeled at or after the latest covering ok: a label put back
after the ok holds the PR again, and still holds it once someone other
than the approver removes it. Neither clears unless the approver
may approve: a trusted_logins key, or, once the GitHub App switch is on, a
login in orch.yml's approver_logins (#122). Anything unread fails closed.

Globs are matched against repo-relative paths: ``*`` and ``?`` never
cross ``/``; ``**`` matches any number of whole directories (``a/**``
is everything under ``a/``, ``a/**/b`` includes ``a/b``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from lib.factory_md import HoldConfig
from lib.handoff import HandoffError, parse_comment

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out), re.DOTALL)


def glob_matches(pattern: str, path: str) -> bool:
    return _glob_regex(pattern).fullmatch(path) is not None


def held_paths(patterns: Sequence[str], files: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                path
                for path in files
                if any(glob_matches(pattern, path) for pattern in patterns)
            }
        )
    )


@dataclass(frozen=True)
class LabelEvent:
    """One labeled / unlabeled event on the PR (GitHub issue events)."""

    id: str
    action: str
    label: str
    actor: str
    created_at: float


@dataclass(frozen=True)
class HoldDecision:
    """reasons is empty when nothing holds the PR. unread names the input
    that could not be read (the caller retries next poll). cleared names
    how a hold was released: ok or label-removed."""

    reasons: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    unread: str | None = None
    cleared: str | None = None

    @property
    def blocks(self) -> bool:
        return self.unread is not None or (bool(self.reasons) and self.cleared is None)


def decide(
    hold: HoldConfig,
    *,
    labels: Sequence[str] | None,
    changed_files: Sequence[str] | None,
    label_events: Sequence[LabelEvent] | None,
    comments: Iterable[object],
    approver_allowed: bool,
    covers: Callable[[str], bool],
) -> HoldDecision:
    """Whether the hold blocks a merge of the current head.

    comments are dispatcher Comments (id, author, body). covers(head) says
    whether an ok naming head covers the current head by the ship rule.
    """
    if labels is None:
        return HoldDecision(unread="labels")
    if changed_files is None:
        return HoldDecision(unread="changed files")
    if label_events is None:
        return HoldDecision(unread="label events")
    approver = hold.approver.casefold()
    wanted = hold.label.casefold()
    on_pr = any(label.casefold() == wanted for label in labels)
    latest = _latest_label_event(label_events, wanted)
    removed_by_approver = (
        approver_allowed
        and latest is not None
        and latest.action == "unlabeled"
        and latest.actor.casefold() == approver
    )
    label_hold = on_pr or (latest is not None and latest.action == "unlabeled" and not removed_by_approver)
    paths = held_paths(hold.paths, changed_files)
    reasons = tuple(
        name for name, active in (("label", label_hold), ("paths", bool(paths))) if active
    )
    if not reasons:
        cleared = "label-removed" if removed_by_approver and not on_pr else None
        return HoldDecision(cleared=cleared)
    ok_at = _approver_ok(comments, approver, covers) if approver_allowed else None
    relabeled = ok_at is not None and label_hold and any(
        event.action == "labeled"
        and event.label.casefold() == wanted
        and event.created_at >= ok_at
        for event in label_events
    )
    if ok_at is not None and not relabeled:
        return HoldDecision(reasons=reasons, paths=paths, cleared="ok")
    return HoldDecision(reasons=reasons, paths=paths)


def _approver_ok(
    comments: Iterable[object],
    approver: str,
    covers: Callable[[str], bool],
) -> float | None:
    newest_first = sorted(
        comments, key=lambda item: float(getattr(item, "created_at", 0)), reverse=True
    )
    for item in newest_first:
        if str(getattr(item, "author", "")).casefold() != approver:
            continue
        try:
            fields = parse_comment(str(getattr(item, "body", ""))).as_dict()
        except HandoffError:
            continue
        if fields["kind"] != "ok":
            continue
        head = fields.get("head", "")
        if _FULL_SHA.fullmatch(head) is None:
            continue
        if covers(head):
            return float(getattr(item, "created_at", 0))
    return None


def _latest_label_event(
    events: Sequence[LabelEvent],
    label: str,
) -> LabelEvent | None:
    relevant = [
        event
        for event in events
        if event.action in ("labeled", "unlabeled") and event.label.casefold() == label
    ]
    if not relevant:
        return None
    return max(relevant, key=_event_order)


def _event_order(event: LabelEvent) -> tuple[float, int, int | str]:
    if event.id.isdigit():
        return event.created_at, 0, int(event.id)
    return event.created_at, 1, event.id
