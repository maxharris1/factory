"""Module functions shared by the dispatcher's mixins: trust, rounds, handoff text, states."""

from __future__ import annotations

import json
import re
import sqlite3
import zlib
from collections.abc import Callable, Mapping
from dataclasses import replace

from lib.dispatch.constants import (
    OWN_CLOSE_SETTLE_SECONDS,
    _FENCE_OPEN,
    _LIST_MARKER,
    _NO_QUESTIONS,
    _QUESTIONS_HEADING,
)
from lib.dispatch.types import (
    CIResult,
    Comment,
    FailedCheck,
    RoundGrant,
    RouterPicks,
    ShipError,
    TrackedTicket,
)
from lib.factory_md import FactoryConfig, family_findings
from lib.feeder_gates import parse_gate_lines
from lib.handoff import matching_head as _matching_head
from lib.queue import RECIPIENTS, Job
from lib.role_router import (
    Candidate,
    Draw,
    GuardFacts,
    KNOWN_HARNESSES,
    RoutingConfig,
    RoutingError,
    Sample,
    TicketFacts,
    build_role_for_ticket_type,
    central_routing,
    draw,
    parse_sample,
    resolve_slot,
)
from lib.schema import HARNESSES
from lib.ticket_type import ticket_type_of


def _spread(repo: str, ticket_id: str) -> int:
    return zlib.crc32(f"{str(repo).lower()}#{ticket_id}".encode())


def _scope(repo: str, ticket_id: str) -> str:
    return f"{repo.lower()}\x1f{ticket_id}"


def grant_open(
    connection: sqlite3.Connection, ticket: TrackedTicket, grant: RoundGrant, open_stop: str | None
) -> bool:
    if grant.released_by == "engine" and grant.trigger.id != open_stop:
        return False
    return (
        connection.execute(
            "SELECT 1 FROM review_events WHERE repo = ? AND ticket_id = ? AND comment_id = ?",
            (ticket.repo.lower(), ticket.ticket_id, grant.key),
        ).fetchone()
        is None
    )


def own_close_at_head(
    connection: sqlite3.Connection, ticket: TrackedTicket
) -> tuple[str, float | None] | None:
    row = connection.execute(
        "SELECT own_close_head, own_close_step, own_close_at FROM ticket_state "
        "WHERE repo = ? AND ticket_id = ?",
        (ticket.repo.lower(), ticket.ticket_id),
    ).fetchone()
    if row is None or row[0] is None or _matching_head(row[0], ticket.current_head) is None:
        return None
    return row[1], row[2]


def own_close_holds(record: tuple[str, float | None] | None, now: float) -> bool:
    if record is None:
        return False
    step, at = record
    return step == "closing" or (at is not None and now - at < OWN_CLOSE_SETTLE_SECONDS)


def record_own_close(
    connection: sqlite3.Connection, ticket: TrackedTicket, head: str, step: str, at: float
) -> None:
    connection.execute(
        "UPDATE ticket_state SET own_close_head = ?, own_close_step = ?, own_close_at = ? "
        "WHERE repo = ? AND ticket_id = ?",
        (head, step, at, ticket.repo.lower(), ticket.ticket_id),
    )
    connection.commit()


def _base_update_key(ticket: TrackedTicket, head: str) -> str:
    return f"base-update:{_scope(ticket.repo, ticket.ticket_id)}:{head}"


def trusted_bots(
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]],
    author: str,
) -> frozenset[str]:
    """The bots a comment author may speak for: GitHub logins from
    trusted_logins, `linear:<id>` authors from trusted_linear_users."""
    tracker = "linear" if author.startswith("linear:") else "github"
    return frozenset(trusted_logins.get(tracker, {}).get(author, frozenset()))


def _normalize_trusted_logins(
    value: Mapping[str, object],
) -> dict[str, dict[str, frozenset[str]]]:
    normalized: dict[str, dict[str, frozenset[str]]] = {}
    if not all(key in ("github", "linear") for key in value):
        raise ValueError("trusted_logins must be keyed by tracker")
    for tracker, entries in value.items():
        if not isinstance(entries, Mapping):
            raise ValueError("trusted_logins tracker entries must be mappings")
        normalized[tracker] = {}
        for login, bots in entries.items():
            if not isinstance(login, str) or not isinstance(
                bots, (set, frozenset, tuple, list)
            ):
                raise ValueError("trusted_logins entries must be bot sets")
            normalized[tracker][login] = frozenset(str(bot) for bot in bots)
    return normalized


def _grok_logical_key(job: Job) -> str:
    request = job.request
    return (
        f"{_scope(request.repo, request.ticket_id)}"
        f"\x1f{request.action}\x1f{request.source_comment_id}"
    )


# Verdict outcomes that took the verdict's action (#80, Codex PR #82 r2
# F2.3): a fix job queued, the review capped, a re-review queued, or a ship
# begun or done. Only these make a later identical verdict a duplicate.
ACTED_VERDICT_OUTCOMES = frozenset(
    {
        "queued-review-fix",
        "capped-review",
        "blocked-review",
        "queued-patch-review",
        "merged",
        "merge-confirmed",
        "handed-off",
        "handed-off-reconciled",
        "merge-queue",
        "waiting-external",
        "ship-unconfirmed",
        "ship-failed",
        "held-base-update",
    }
)


QUESTIONS_OUTCOMES = frozenset(
    {"no-questions", "waiting-max", "already-waiting-max", "rejected-questions-author"}
)


def _answering_outcome(outcome: str) -> bool:
    return not (
        outcome in QUESTIONS_OUTCOMES
        or outcome.startswith("failed-questions-")
        or outcome == "woke-grok-unparsed"
    )


def _acted_verdict(outcome: str | None) -> bool:
    return outcome is not None and (
        outcome in ACTED_VERDICT_OUTCOMES
        or outcome.startswith("ship-already-completed-")
    )


def _round_number(value: object) -> int | None:
    """The N of a round field "N/CAP" (or "N"), or None."""
    if not isinstance(value, str):
        return None
    head = value.split("/", 1)[0].strip()
    return int(head) if head.isdigit() else None


# _posted_handoff: a ready whose head T2 could not yet check against the PR.
_HANDOFF_UNREAD = object()

_NO_READ_SINCE_FAILURE = object()

# The result kinds that settle each failed harness job, so T6 never retries it (#193).
_SETTLING_KINDS = {
    "review": ("verdict", "questions"),
    "plan": ("plan", "questions"),
    "build": ("ready", "questions"),
    "fix": ("ready", "questions"),
    "update-from-main": ("ready", "questions"),
}

_REGISTERING_ACTIONS = {
    "ready": ("build", "fix", "update-from-main"),
    "verdict": ("review",),
}

# The handoff kind and role that answers each harness step (#108).
_ANSWERS = {
    "plan": ("plan", "builder"),
    "build": ("ready", "builder"),
    "fix": ("ready", "builder"),
    "review": ("verdict", "reviewer"),
}

# Handled outcomes that rejected a verdict: such a comment is no job's handoff.
_REJECTED_OUTCOMES = (
    "rejected-",
    "stale-",
    "superseded-",
    "duplicate-",
    "refused-",
    "failed-",
    "ticket-stuck",
    "ticket-abandoned",
)

_UPDATE_NO_HANDOFF_OUTCOMES = (
    "update-failed-",
    "update-failure-counted",
    "update-superseded",
    "update-unattributed",
    "stuck-update-failed-2",
)


def _retry_detail(job: Job) -> str | None:
    """The failed job's detail naming it as `retry_of`, so the retry can
    take over a worktree the first attempt kept (#105)."""
    try:
        context = json.loads(job.request.detail) if job.request.detail else {}
    except ValueError:
        return job.request.detail
    if not isinstance(context, dict):
        return job.request.detail
    context["retry_of"] = job.id
    return json.dumps(context, separators=(",", ":"), sort_keys=True)


def _retry_of(detail: str | None) -> str | None:
    """The failed job a T6 retry repeats (its detail's retry_of), if any."""
    try:
        value = json.loads(detail) if detail else None
    except ValueError:
        return None
    retry_of = value.get("retry_of") if isinstance(value, dict) else None
    return retry_of if isinstance(retry_of, str) else None


def _append_detail(detail: str, note: str) -> str:
    try:
        context = json.loads(detail)
    except ValueError:
        return f"{detail}; {note}"
    if not isinstance(context, dict):
        return f"{detail}; {note}"
    prior = context.get("detail")
    context["detail"] = f"{prior}; {note}" if isinstance(prior, str) else note
    return json.dumps(context, separators=(",", ":"), sort_keys=True)


def _detail_round_number(detail: str | None) -> int | None:
    """The round number a job's detail JSON carries ("round": "N/CAP")."""
    if not detail:
        return None
    try:
        context = json.loads(detail)
    except ValueError:
        return None
    return _round_number(context.get("round")) if isinstance(context, dict) else None


def _detail_round(detail: str | None) -> str | None:
    """The round field a job's detail JSON carries ("N/CAP")."""
    return _detail_field(detail, "round")


def _detail_field(detail: str | None, name: str) -> str | None:
    if not detail:
        return None
    try:
        context = json.loads(detail)
    except ValueError:
        return None
    value = context.get(name) if isinstance(context, dict) else None
    return value if isinstance(value, str) else None


_UNQUEUEABLE = tuple(harness for harness in KNOWN_HARNESSES if harness not in RECIPIENTS)
_PICK_KEYS = (
    "builder_model",
    "builder_effort",
    "reviewer_model",
    "reviewer_effort",
    "planner",
    "plan_model",
    "plan_effort",
)


def _detail_routing(
    detail: str | None,
) -> tuple[str, str, RouterPicks | None] | None:
    """The (builder, reviewer) a job's detail JSON was routed to (#206)."""
    if not detail:
        return None
    try:
        context = json.loads(detail)
    except ValueError:
        return None
    routing = context.get("routing") if isinstance(context, dict) else None
    if not isinstance(routing, dict):
        return None
    builder, reviewer = routing.get("builder"), routing.get("reviewer")
    if builder not in HARNESSES or reviewer not in HARNESSES:
        return None
    sample = parse_sample(routing.get("sample"))
    if sample is None and not any(isinstance(routing.get(key), str) for key in _PICK_KEYS):
        return builder, reviewer, None

    def text(key: str) -> str | None:
        value = routing.get(key)
        return value if isinstance(value, str) else None

    planner = routing.get("planner")
    picks = RouterPicks(
        builder=Candidate(builder, text("builder_model"), text("builder_effort")),
        reviewer=Candidate(reviewer, text("reviewer_model"), text("reviewer_effort")),
        planner=(
            Candidate(planner, text("plan_model"), text("plan_effort"))
            if planner in HARNESSES
            else None
        ),
        sample=sample,
    )
    return builder, reviewer, picks


def _route(ticket: TrackedTicket, builder: str, reviewer: str) -> TrackedTicket:
    """ticket with factory.md's harnesses swapped for builder and reviewer.
    A model hint is kept only for a side whose harness stays factory.md's."""
    base = ticket.factory_md or ticket.factory
    return replace(
        ticket,
        factory_md=base,
        router_picks=None,
        factory=replace(
            base,
            builder_harness=builder,
            builder_model=base.builder_model if builder == base.builder_harness else None,
            reviewer_harness=reviewer,
            reviewer_model=(
                base.reviewer_model if reviewer == base.reviewer_harness else None
            ),
        ),
    )


def _route_picks(ticket: TrackedTicket, picks: RouterPicks) -> TrackedTicket:
    """ticket routed to routing.yml's picks: harness, model and effort per side."""
    builder, reviewer = picks.builder, picks.reviewer
    routed = _route(ticket, builder.harness, reviewer.harness)
    return replace(
        routed,
        router_picks=picks,
        factory=replace(
            routed.factory, builder_model=builder.model, reviewer_model=reviewer.model
        ),
    )


def _ticket_block(body: str) -> str | None:
    lines = body.splitlines()
    for index, line in enumerate(lines[:-1]):
        fence = lines[index + 1]
        if line == "### Ticket" and re.fullmatch(r"`{3,}", fence):
            rest = lines[index + 2 :]
            return "\n".join(rest[: rest.index(fence)]) if fence in rest else None
    return None


def _ticket_facts(ticket: TrackedTicket) -> TicketFacts:
    from lib.feeder import parse_intake

    brief = ticket.brief_comment
    block = None if brief is None else _ticket_block(brief.body)
    return TicketFacts(
        repo=ticket.repo,
        ticket=ticket.ticket_id,
        risk=ticket.risk,
        plan_first=block is not None and parse_intake(block, ()).plan_first is True,
        touches=() if block is None else tuple(sorted(parse_gate_lines(block, ticket.repo).touches)),
        ticket_type=None if brief is None else ticket_type_of(brief.body),
    )


def _router_picks(
    ticket: TrackedTicket,
    refused: frozenset[str],
    builder_only: str | None = None,
    reviewer_only: str | None = None,
    *,
    sample: Sample | None = None,
    guards: Callable[[], GuardFacts] | None = None,
) -> RouterPicks | None:
    base = ticket.factory_md or ticket.factory
    if base.routing is None:
        return None
    central = central_routing()
    brief = ticket.brief_comment
    build_role = build_role_for_ticket_type(None if brief is None else ticket_type_of(brief.body))
    roles = [role for role, only in (("build", builder_only), ("review", reviewer_only)) if only is None]
    draws: dict[str, Draw | None] = {}
    if sample is not None:
        draws = {role: getattr(sample, role) for role in roles}
    elif guards is not None and central is not None:
        drawing = [role for role in roles if central.share("gemini", role) > 0]
        if drawing:
            facts, guard_facts = _ticket_facts(ticket), guards()
            for role in drawing:
                draws[role] = draw(
                    role, facts, central.shares["gemini"], guard_facts, build=draws.get("build")
                )

    def pick(
        role: str,
        only: str | None,
        base_harness: str,
        base_model: str | None,
        built_by: tuple[str, str | None] | None = None,
        drawn: Draw | None = None,
    ) -> tuple[Candidate, Draw | None]:
        blocked = set(_UNQUEUEABLE)
        if only is not None:
            blocked |= set(KNOWN_HARNESSES) - {only}
        try:
            slot = resolve_slot(
                role,
                routing=central,
                factory=base,
                built_by=built_by,
                refused_harnesses=refused,
                unavailable_harnesses=blocked,
                drawn=drawn,
            )
        except RoutingError:
            if only is None:
                raise
            return Candidate(only, base_model if only == base_harness else None), None
        return Candidate(slot.harness, slot.model, slot.effort), slot.draw

    builder, build_draw = pick(
        build_role, builder_only, base.builder_harness, base.builder_model, drawn=draws.get("build")
    )
    reviewer, review_draw = pick(
        "review",
        reviewer_only,
        base.reviewer_harness,
        base.reviewer_model,
        built_by=(builder.harness, builder.model),
        drawn=draws.get("review"),
    )
    return RouterPicks(
        builder,
        reviewer,
        _plan_pick(central, base, refused, on=builder.harness),
        sample=None if build_draw is None and review_draw is None else Sample(build_draw, review_draw),
    )


def _review_pick(
    ticket: TrackedTicket, refused: frozenset[str], built_by: Candidate, drawn: Draw | None = None
) -> Candidate | None:
    base = ticket.factory_md or ticket.factory
    if base.routing is None:
        return None
    try:
        slot = resolve_slot(
            "review",
            routing=central_routing(),
            factory=base,
            built_by=(built_by.harness, built_by.model),
            refused_harnesses=refused,
            unavailable_harnesses=_UNQUEUEABLE,
            drawn=drawn,
        )
    except RoutingError:
        return None
    return Candidate(slot.harness, slot.model, slot.effort)


def _plan_pick(
    central: RoutingConfig | None,
    base: FactoryConfig,
    refused: frozenset[str],
    *,
    on: str,
) -> Candidate | None:
    try:
        slot = resolve_slot(
            "plan",
            routing=central,
            factory=base,
            refused_harnesses=refused,
            unavailable_harnesses=set(KNOWN_HARNESSES) - {on},
        )
    except RoutingError:
        return None
    return Candidate(slot.harness, slot.model, slot.effort)


def _route_go(
    ticket: TrackedTicket, fields: Mapping[str, str], refused: frozenset[str] = frozenset()
) -> TrackedTicket | None:
    """The ticket routed as a go names it (#206), or None when the pair
    fails factory.md's cross-family rule. A go naming neither harness
    routes back to factory.md's."""
    base = ticket.factory_md or ticket.factory
    named = "builder" in fields or "reviewer" in fields
    picks = _router_picks(
        ticket,
        refused,
        builder_only=fields.get("builder", base.builder_harness) if named else None,
        reviewer_only=fields.get("reviewer", base.reviewer_harness) if named else None,
        sample=ticket.router_picks.sample if ticket.router_picks is not None else None,
    )
    if picks is not None:
        routed = _route_picks(ticket, picks)
    elif not named:
        return replace(ticket, factory=base, factory_md=None, router_picks=None)
    else:
        routed = _route(
            ticket,
            fields.get("builder", base.builder_harness),
            fields.get("reviewer", base.reviewer_harness),
        )
    return None if _family_errors(routed.factory) else routed


def _family_errors(config: FactoryConfig) -> list[str]:
    return [
        finding.message
        for finding in family_findings(
            config.builder_harness,
            config.builder_model,
            config.reviewer_harness,
            config.reviewer_model,
        )
        if finding.severity == "error"
    ]


def _base_mismatch(ticket: TrackedTicket) -> str | None:
    base = ticket.factory.base_branch
    if ticket.pr_base is None or ticket.pr_base == base:
        return None
    return f"PR #{ticket.pr_number} targets {ticket.pr_base}; the ticket base is {base}"


def _stack_wait(ticket: TrackedTicket, head: str | None) -> str | None:
    stacked = ticket.stacked_on
    if stacked is None:
        return None
    base = ticket.pr_base or ticket.factory.base_branch
    if base == stacked.branch:
        return f"stacked on {stacked.branch}: waits for its retarget"
    if ticket.factory.ship == "handoff" and base != stacked.default_branch:
        return f"stacked child: on {base}, waits for a retarget to {stacked.default_branch}"
    if ticket.no_ci_base is not None:
        return f"stacked child: GitHub CI does not run on {base}, waits for a retarget to a CI base"
    ci = ticket.ci.get(head) if head is not None else None
    if ci is not None and ci.status == "not-run":
        return f"stacked child: CI not-run on {base}, waits for GitHub CI"
    return None


def _redrive_eligible(exc: BaseException, policy: str) -> bool:
    """Whether a failed ship gets its one re-drive (issue #47, issue #83)."""
    if not isinstance(exc, ShipError):
        return False
    if policy == "handoff":
        return exc.write_confirmed is False
    if policy == "merge":
        return exc.retryable is True
    return False


def _holds(ticket: TrackedTicket) -> bool:
    return ticket.factory.hold_applies


def ship_mode(policy: str) -> str:
    """after:<approver> repos fast-forward; every other repo squash-merges."""
    return "fast-forward" if policy.startswith("after:") else "squash"


def _comment_order(comment: Comment) -> tuple[float, int, int | str]:
    """(created_at, id) with numeric ids compared as integers, so 9 < 10."""
    if re.fullmatch(r"[0-9]+", comment.id):
        return comment.created_at, 0, int(comment.id)
    return comment.created_at, 1, comment.id


def _asks_questions(fields: Mapping[str, str], body: str) -> bool:
    return fields.get("state") == "waiting-max" or (
        fields.get("role") != "orchestrator" and _has_questions(body)
    )


def _has_questions(body: str) -> bool:
    """Whether a trusted questions comment asks anything.

    Fail closed: every questions comment counts unless it explicitly says
    there are none. Fenced code blocks (``` or ~~~, closed) are example text:
    a Questions heading inside one is ignored. Every Questions heading outside
    fences (any style: ``## Questions``, ``**Questions:**``, ``Questions: ...``)
    opens a section, and the comment has questions if any section does. In a
    section only the first content after the heading decides, whether that
    is the inline remainder or the next non-blank line: a none-marker
    ("None.", "N/A", "No questions") or no content at all means none; anything
    else, headings, bold labels and fenced blocks included, means questions.
    Headings never close a section, because a heading right under Questions
    can itself be a question or a label for one.
    Without a Questions heading, any line that is not a none-marker counts.
    """
    lines = body.splitlines()
    unfenced = _collapse_fences(lines)
    sections = [
        (heading.group("rest") or "", unfenced[index + 1 :])
        for index, line in enumerate(unfenced)
        if line is not None
        and (heading := _QUESTIONS_HEADING.fullmatch(line.strip())) is not None
    ]
    if sections:
        return any(_section_asks(rest, after) for rest, after in sections)
    return any(
        item and item not in _NO_QUESTIONS
        for item in map(_question_item, lines)
    )


def _section_asks(rest: str, after: list[str | None]) -> bool:
    """Whether a Questions section's first content is not a none-marker.

    A fenced block (None) is never a none-marker, so it counts as asking.
    """
    for content in [rest, *after]:
        if content is None:
            return True
        item = _question_item(content)
        if item:
            return item not in _NO_QUESTIONS
    return False


def _collapse_fences(lines: list[str]) -> list[str | None]:
    """The lines with each closed fenced code block replaced by one None.

    An opening fence with no closing fence is left as ordinary text, so an
    unclosed fence never hides anything (fail closed).
    """
    collapsed: list[str | None] = []
    index = 0
    while index < len(lines):
        opening = _FENCE_OPEN.match(lines[index])
        if opening is not None:
            fence = opening.group("fence")
            closing = re.compile(
                rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*"
            )
            end = next(
                (
                    later
                    for later in range(index + 1, len(lines))
                    if closing.fullmatch(lines[later])
                ),
                None,
            )
            if end is not None:
                collapsed.append(None)
                index = end + 1
                continue
        collapsed.append(lines[index])
        index += 1
    return collapsed


def _question_item(line: str) -> str:
    """A line reduced to its words: no heading, bold or list markup.

    Markup-only lines ("##", "**", "---") reduce to "" and count as blank.
    """
    text = re.sub(r"^#{1,6}(?=\s|$)|\s#+$", "", line.strip())
    text = _LIST_MARKER.sub("", text.strip("*: ")).strip("*: ")
    text = text.rstrip(".").strip().lower()
    return "" if re.fullmatch(r"[-*_#=\s]*", text) else text


def _comment_link(comment: Comment) -> str:
    return comment.url or f"comment:{comment.id}"


def _detail_project_bot(detail: str | None) -> str | None:
    if detail is None:
        return None
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    project_bot = parsed.get("project_bot")
    return project_bot if isinstance(project_bot, str) else None


def _merge_only(detail: str | None) -> bool:
    try:
        parsed = json.loads(detail) if detail else None
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed.get("kind") == "merge-only"


def _is_patch_id(value: str) -> bool:
    return re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


def _is_full_sha(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _ship_state(policy: str) -> str:
    if policy == "merge":
        return "merged"
    if policy == "handoff":
        return "handed-off"
    return "waiting-external"


def _queue_state(policy: str) -> str:
    """Where an approved finish_line: merge ticket waits (#130)."""
    return "waiting-external" if policy.startswith("after:") else "merge-queue"


def _state_for_action(action: str) -> str:
    return {
        "plan": "planning",
        "build": "building",
        "review": "review",
        "fix": "fixing",
        "update-from-main": "updating",
        "clarify": "questions",
        "intake-clarify": "intake",
        "qa": "review",
    }[action]


def failed_checks(result: CIResult) -> tuple[FailedCheck, ...]:
    if result.failed:
        return result.failed
    return tuple(
        FailedCheck(name)
        for name in (result.log or "").removeprefix("failed: ").split(", ")
        if name
    )


def red_checks(ticket: TrackedTicket, head: str | None) -> tuple[str, ...]:
    result = ticket.ci.get(head) if head is not None else None
    if result is None or result.status != "failure":
        return ()
    return tuple(check.name for check in failed_checks(result))


def _post_to(pr_number: str | None) -> str:
    if pr_number is None:
        return "the linked PR issue comments (not reviews or inline comments)"
    return f"PR #{pr_number} issue comments (not reviews or inline comments)"
