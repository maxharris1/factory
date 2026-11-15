"""The standard brief a waiter hands its harness for one job (#80).

`orch wait <bot> --autostart` claims a job and starts the bot's harness under
`orch job run` itself, so the brief is the whole hand-off: no model turn and
no bot-to-bot message sits between the claim and the start. There is one
brief per job kind:

* implement: a `build` or `plan` job (the builder);
* fix: a `fix` job (CI red, or a REVISE verdict);
* review: a `review` job (the cross-family reviewer);
* merge-only: an `update-from-main` job (#130), or a `fix` job whose detail
  says `"kind": "merge-only"` (bring the base branch in by a merge commit,
  nothing else).

Each brief is rendered from the job row alone: repo, PR or ticket, head sha,
round and cap, risk, the review rules (docs/SPEC.md section 8) and the
orch:v1 handoff header the job must end with (section 5). The target repo's factory.md prose is appended as an
additional-context section (lib/factory_md.py parse_factory_context), so
project-specific behavior stays in each repo's factory.md.

Job text is data. Free text from the row (the brief, a CI log, a verdict
link) appears only inside one fenced "Job data" block, whose fence is longer
than any backtick run in it. A value shown outside that block (repo, ticket,
head, round, risk, base branch, PR number) is shown only when it matches its
strict form; otherwise the brief says it is missing and points at the data.
The brief is passed to the harness on stdin and as a file, never in an argv.

A job placed on a remote device (issue #85) gets the same brief with two
differences: its working directory is the job directory on that device
(`$ORCH_JOB_WORKDIR`), and since the device has no orch.db, the harness does
not run `orch job done`. It ends its output with `ORCH-HANDOFF: <comment-id>`
(or `ORCH-HANDOFF: failed`) and the waiter runs `orch job done` on the box.

The package splits the renderer by part: `common` (job facts and the shared
sections), `context` (factory.md), `rules`, `skills`, and one module per job
(`plan`, `build`, `fix`, `review`, `merge_only`). tests/snapshots/brief holds
one rendered brief per job kind; regenerate it with
`python tests/regenerate_brief_snapshots.py`, never by hand.
"""

from __future__ import annotations

from pathlib import Path

from lib.brief import build, fix, merge_only, plan, review, skills
from lib.brief.common import (
    BOTS,
    TEXT_LIMIT,
    BriefError,
    data_section,
    detail_of,
    facts_of,
    handoff_section,
    job_section,
    opening,
    questions_handoff_section,
)
from lib.brief.context import context_section
from lib.brief.plan import PLAN_HANDOFF_LIMIT
from lib.brief.rules import finish_section, review_rules, rules
from lib.factory_md import FactoryContext
from lib.pstack_pin import ROOT
from lib.queue import Job

__all__ = [
    "KINDS",
    "PLAN_HANDOFF_LIMIT",
    "TEXT_LIMIT",
    "BriefError",
    "brief_kind",
    "render_brief",
]

KINDS = ("implement", "fix", "review", "merge-only")

_ACTION_KINDS = {
    "build": "implement",
    "plan": "implement",
    "fix": "fix",
    "review": "review",
    "update-from-main": "merge-only",
}


def brief_kind(job: Job) -> str:
    action = job.request.action
    kind = _ACTION_KINDS.get(action)
    if kind is None:
        raise BriefError(f"no standard brief for a {action!r} job")
    if action == "fix" and detail_of(job).get("kind") == "merge-only":
        return "merge-only"
    return kind


def render_brief(
    job: Job,
    *,
    bot: str,
    context: FactoryContext | None,
    context_ref: str | None = None,
    context_note: str | None = None,
    remote_device: str | None = None,
    factory_root: str | None = None,
    pin_root: Path = ROOT,
) -> str:
    """The brief for job, as `bot` runs it. See the module doc.

    remote_device names the remote device the job is placed on, or None on
    the box. factory_root is the placed device's deployed factory checkout;
    the Skills section names files under it, checked against pin_root's
    skills.lock. A plan, build, fix or review job whose files cannot be named
    gets a questions-only brief (fail closed)."""
    if bot not in BOTS:
        raise BriefError(f"{bot!r} is not a harness bot")
    if f"{job.request.recipient}-bot" != bot:
        raise BriefError(f"job {job.id} belongs to {job.request.recipient}, not {bot}")
    kind = brief_kind(job)
    detail = detail_of(job)
    facts = facts_of(job, detail, context)
    role = "reviewer" if kind == "review" else "builder"
    router = (
        None
        if kind == "merge-only"
        else skills.resolve(role, bot, factory_root, pin_root)
    )
    if isinstance(router, str):
        middle = [
            "## What to do\n" + "\n".join(skills.blocked_task_lines(router)),
            questions_handoff_section(bot, role, facts),
        ]
    else:
        middle = [
            *([skills.section(router)] if router is not None else []),
            "## What to do\n" + "\n".join(_task_lines(job, kind, facts)),
            _handoff(job, bot, kind, facts, skills_read=router is not None),
        ]
    parts = [
        opening(bot, kind),
        job_section(job, bot, kind, facts, remote_device),
        *middle,
        finish_section(job, bot, remote_device),
        review_rules(facts),
        rules(),
        data_section(job, detail),
        context_section(facts, context, context_ref, context_note, kind),
    ]
    return "\n\n".join(part.rstrip() for part in parts) + "\n"


def _is_plan(job: Job, kind: str) -> bool:
    return kind == "implement" and job.request.action == "plan"


def _task_lines(job: Job, kind: str, facts: dict) -> list[str]:
    if _is_plan(job, kind):
        return plan.task_lines(facts)
    if kind == "implement":
        return build.task_lines(job, facts)
    if kind == "fix":
        return fix.task_lines(job, facts)
    if kind == "review":
        return review.task_lines(facts)
    return merge_only.task_lines(job, facts)


def _handoff(job: Job, bot: str, kind: str, facts: dict, *, skills_read: bool) -> str:
    if kind == "review":
        header, example = review.handoff(bot, facts)
    elif _is_plan(job, kind):
        header, example = plan.handoff(bot, facts)
    else:
        header, example = build.ready_handoff(bot)
    role = "reviewer" if kind == "review" else "builder"
    return handoff_section(bot, role, facts, header, example, skills_read=skills_read)
