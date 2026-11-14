"""The go or answers that started a job, for the evidence bundle (#165).

A pure function: the caller passes the PR's comments, the job's trigger
(its source_comment_id), action and detail, the detail of the job it resumes
(the paused step for an answers, the last fix for an extra round, else None),
the ticket's plan_gap_count and the open findings from findings_evidence.

The cause is what the trigger was: `go` for a go that started a plan or a
build, `extra-round` for a go that queued a fix (only Max's extra round on a
capped ticket does), `answers` for an answers that resumed a paused step, and
None for any other trigger. A go or answers counts only with `from=grok-bot`,
`role=orchestrator` and an author trusted_logins lets speak for grok-bot; an
extra-round go also counts from an approvers login, as the dispatcher lets an
approver lift a cap. The prose is the trigger's body without its header, or
None when the comment was edited after it was posted.

prior_instruction is the resumed job's `instruction`, which the resume's own
instruction replaces in its detail. plan_attempt is plan_gap_count + 1 for a
plan job, else None: a question and its answer leave plan_gap_count as it was.
retry_of is the failed job a T6 retry names. Only a fix job carries findings.
Values of the wrong type in either detail are read as None.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lib.dispatcher import Comment, trusted_bots
from lib.evidence.findings import Finding
from lib.handoff import HandoffError, parse_comment

CAUSES = ("go", "extra-round", "answers")
ORCHESTRATOR = "grok-bot"


@dataclass(frozen=True)
class InstructionsEvidence:
    cause: str | None
    prose: str | None
    url: str | None
    prior_instruction: str | None
    plan_attempt: int | None
    retry_of: str | None
    findings: tuple[Finding, ...]


def instructions_evidence(
    *,
    comments: Sequence[Comment],
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]],
    source_comment_id: str | None,
    action: str,
    detail: Mapping[str, object],
    prior_detail: Mapping[str, object] | None,
    plan_gaps: int,
    findings: Sequence[Finding],
    approvers: frozenset[str] = frozenset(),
) -> InstructionsEvidence:
    """The trigger's prose and the typed resume facts. See the module doc."""
    cause, prose, url = _trigger(comments, trusted_logins, approvers, source_comment_id, action)
    return InstructionsEvidence(
        cause=cause,
        prose=prose,
        url=url,
        prior_instruction=_text(prior_detail, "instruction"),
        plan_attempt=plan_gaps + 1 if action == "plan" else None,
        retry_of=_text(detail, "retry_of"),
        findings=tuple(findings) if action == "fix" else (),
    )


def _trigger(
    comments: Sequence[Comment],
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]],
    approvers: frozenset[str],
    source_comment_id: str | None,
    action: str,
) -> tuple[str | None, str | None, str | None]:
    """The trigger's (cause, prose, url), or all None when it is not a trusted go or answers."""
    comment = next((item for item in comments if item.id == source_comment_id), None)
    if comment is None:
        return None, None, None
    try:
        handoff = parse_comment(comment.body)
    except HandoffError:
        return None, None, None
    fields = handoff.as_dict()
    kind = fields["kind"]
    if kind == "go":
        cause = "extra-round" if action == "fix" else "go"
    elif kind == "answers":
        cause = "answers"
    else:
        return None, None, None
    trusted = ORCHESTRATOR in trusted_bots(trusted_logins, comment.author) or (
        cause == "extra-round" and comment.author in approvers
    )
    if not trusted or fields.get("from") != ORCHESTRATOR or fields.get("role") != "orchestrator":
        return None, None, None
    prose = None if comment.edited_at is not None else handoff.body.strip()
    return cause, prose, comment.url


def _text(detail: Mapping[str, object] | None, key: str) -> str | None:
    value = detail.get(key) if detail is not None else None
    return value if isinstance(value, str) and value else None
