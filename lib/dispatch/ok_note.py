"""#1419: the note on an approver's kind=ok that cannot move the ticket."""

from __future__ import annotations

from dataclasses import dataclass

from lib.dispatch.helpers import _comment_link
from lib.dispatch.types import Comment
from lib.dispatch.verdict.repeat import STOP_OPTIONS, Stop

_STOP_LABELS: dict[Stop, str] = {
    "repeat": "repeat stop on {ids}",
    "capped": "review cap reached with {ids} still blocking",
    "stalled": "review not converging, {ids} still blocking",
    "unsat": "unsatisfiable {ids}",
}
_REVIEW = (
    "the reviewer's `verdict=APPROVE` with `ac=met` at the PR head, "
    "which ships with no ok while no hold applies"
)
_BUILD = "the builder's ready at a new head, then the reviewer's `verdict=APPROVE` with `ac=met`"
_MOVES_BY_STATE = {
    "review": _REVIEW,
    "fixing": _BUILD,
    "building": _BUILD,
    "waiting-max": "an answer to the open question",
    "blocked": "an approver's `/go` that resumes the review",
}


@dataclass(frozen=True)
class LatestVerdict:
    link: str
    verdict: str
    round: str


@dataclass(frozen=True)
class HeldStop:
    stop: Stop
    finding_ids: frozenset[str]


def ok_marker(comment_id: str) -> str:
    return f"<!-- orch:ok-note {comment_id} -->"


def _ids(finding_ids: frozenset[str]) -> str:
    return ", ".join(f"`{item}`" for item in sorted(finding_ids)) or "no named finding"


def no_hold_body(
    ok: Comment,
    *,
    pr_head: str,
    state: str | None,
    latest: LatestVerdict | None,
    stop: HeldStop | None,
    stuck: str | None,
) -> str:
    verdict = (
        "none yet"
        if latest is None
        else f"{latest.link} (`{latest.verdict}`, round `{latest.round}`)"
    )
    stop_line = (
        "none" if stop is None else _STOP_LABELS[stop.stop].format(ids=_ids(stop.finding_ids))
    )
    stuck_lines: tuple[str, ...] = ()
    if stuck is not None:
        stuck_lines = (f"- Stuck: `{stuck}`",)
        moves = (
            "a grok-bot `kind=retry` posted by a trusted login (e.g. via `orch retry`), "
            f"which restarts the step stuck on `{stuck}`"
        )
    elif stop is None:
        moves = _MOVES_BY_STATE.get(state or "", _REVIEW)
    else:
        moves = "one of these options\n" + "\n".join(
            f"  - {option.name}: {option.consequence}" for option in STOP_OPTIONS[stop.stop]
        )
    return "\n".join(
        (
            ok_marker(ok.id),
            f"The ok in {_comment_link(ok)} moves nothing, because no hold applies to this PR "
            f"and no APPROVE covers head `{pr_head}`.",
            "",
            f"- Latest verdict: {verdict}",
            f"- Stop: {stop_line}",
            *stuck_lines,
            f"- Moves it forward: {moves}",
        )
    )


def other_head_body(ok: Comment, *, ok_head: str | None, pr_head: str) -> str:
    named = "no head" if ok_head is None else f"head `{ok_head}`"
    return "\n".join(
        (
            ok_marker(ok.id),
            f"The ok in {_comment_link(ok)} names {named}, which does not cover the PR head `{pr_head}`.",
            f"This ok does not clear the hold. An approver's ok naming a head that covers `{pr_head}` clears it.",
        )
    )
