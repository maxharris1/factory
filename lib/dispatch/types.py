"""Dispatcher data types: comments, CI results, tracked tickets, Protocols and ship errors."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Literal, Protocol

from lib.factory_md import FactoryConfig, no_ci_base
from lib.hold import LabelEvent, MergeUpdate
from lib.orch_yml import DEFAULT_JOB_TIMEOUT_MINUTES
from lib.queue import Job
from lib.role_router import Candidate, Sample
from lib.update_check import HeadDiff, MovedHead, RebaseScope, RetargetDelta, UpdateClass

if TYPE_CHECKING:
    import sqlite3

    from lib.dispatch.bookkeeping import LockLedger
    from lib.dispatcher import Dispatcher


class Registration(Enum):
    REGISTERED = "registered"
    PENDING = "pending"
    UNREGISTERED = "unregistered"


@dataclass(frozen=True)
class Comment:
    id: str
    author: str
    body: str
    created_at: float
    url: str | None = None
    edited_at: float | None = None


@dataclass(frozen=True)
class RoundGrant:
    key: str
    trigger: Comment
    released_by: Literal["approver", "engine"]
    building: bool = False

    @classmethod
    def approver(cls, go: Comment, *, building: bool = False) -> RoundGrant:
        return cls(go.id, go, "approver", building)

    @classmethod
    def engine(cls, stop: Comment) -> RoundGrant:
        return cls(f"engine:{stop.id}", stop, "engine")


@dataclass(frozen=True)
class PausedStep:
    question_id: str
    action: str
    recipient: str
    generation: int
    head: str | None
    round_number: int | None
    plan_text: str | None = None
    plan_round: int | None = None
    unmapped: str | None = None
    prior: dict[str, object] | None = None


@dataclass(frozen=True)
class PullReview:
    id: str
    author: str
    state: str
    submitted_at: float
    url: str | None = None


@dataclass(frozen=True)
class RouterPicks:
    builder: Candidate
    reviewer: Candidate
    planner: Candidate | None = None
    sample: Sample | None = None


@dataclass(frozen=True)
class HandoffCandidate:
    comment: Comment
    unread: bool


Cause = Literal["infra", "code", "unknown"]


@dataclass(frozen=True)
class FailedCheck:
    name: str
    run_id: str | None = None
    job_id: str | None = None
    run_url: str | None = None
    cause: Cause = "unknown"
    signal: str | None = None


def result_cause(causes: Iterable[Cause]) -> Cause:
    seen = set(causes)
    if "code" in seen:
        return "code"
    return "infra" if "infra" in seen else "unknown"


CHECK_FAILED = (
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
)


@dataclass(frozen=True)
class BaseCheck:
    name: str
    conclusion: str | None
    run_id: str
    job_id: str
    head_sha: str


CIJobStatus = Literal["queued", "in_progress", "completed"]
RerunOutcome = Literal["requested", "running"]
RunnerKind = Literal["self-hosted", "github-hosted", "none"]
RunnerStatus = Literal["online", "offline"]


@dataclass(frozen=True)
class CheckRow:
    name: str
    status: CIJobStatus
    conclusion: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    log_url: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    workflow: str = ""
    run_created_at: float | None = None
    gates: bool = True


@dataclass(frozen=True)
class RunJob:
    job_id: str
    name: str
    status: CIJobStatus
    created_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    runner_name: str | None = None
    runner_kind: RunnerKind | None = None
    steps: int | None = None


@dataclass(frozen=True)
class PoolRunner:
    name: str
    os: str
    status: RunnerStatus
    busy: bool
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class CIResult:
    status: str
    log: str | None = None
    # False when the head has no check run and no commit status at all:
    # GitHub ran nothing there (#255), as opposed to CI still running.
    reported: bool = True
    failed: tuple[FailedCheck, ...] = ()
    pending: tuple[str, ...] = ()
    checks: tuple[CheckRow, ...] = field(default=(), compare=False)
    statuses: tuple[CheckRow, ...] = field(default=(), compare=False)
    stale: tuple[CIChange, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if self.status not in CI_STATUSES:
            raise ValueError(f"unknown CI status {self.status!r}")

    @property
    def gate_checks(self) -> tuple[CheckRow, ...]:
        return tuple(row for row in self.checks if row.gates)

    @property
    def clears(self) -> bool:
        return self.status in ("success", "not-run")

    @property
    def cause(self) -> Cause | None:
        if self.status != "failure":
            return None
        return result_cause(check.cause for check in self.failed)


# #615: not-run is a head on a base CI never runs on (factory.md ci.branches)
# that reported nothing. It clears the CI gates but is never a recorded green.
CI_STATUSES = ("pending", "success", "failure", "not-run")


def ci_on_base(result: CIResult, base_without_ci: str | None) -> CIResult:
    if base_without_ci is None or result.reported or result.status != "pending":
        return result
    return CIResult("not-run", reported=False)


@dataclass(frozen=True)
class _RequiredVerdict:
    failed: tuple[str, ...]
    pending: tuple[str, ...]


def _required_verdict(glob: str, checks: tuple[CheckRow, ...]) -> _RequiredVerdict:
    rows = [row for row in checks if fnmatchcase(row.name, glob)]
    failed = tuple(row.name for row in rows if row.conclusion in CHECK_FAILED)
    running = tuple(row.name for row in rows if row.conclusion is None)
    if running:
        return _RequiredVerdict(failed, running)
    if not rows:
        return _RequiredVerdict(failed, (f"{glob} (not reported)",))
    if not any(row.conclusion == "success" for row in rows):
        return _RequiredVerdict(failed, (f"{glob} (no success)",))
    return _RequiredVerdict(failed, ())


def ci_required(result: CIResult, required: tuple[str, ...]) -> CIResult:
    if not required or not result.reported or (result.status == "pending" and not result.gate_checks):
        return result
    verdicts = [_required_verdict(glob, result.gate_checks) for glob in required]
    failed_names = tuple(dict.fromkeys(name for verdict in verdicts for name in verdict.failed))
    if failed_names:
        gate = tuple(check for check in result.failed if check.name in failed_names)
        failed = gate + tuple(check for check in result.failed if check.name not in failed_names)
        log = "failed: " + ", ".join(check.name for check in failed)
        return CIResult("failure", log, failed=failed, pending=result.pending, checks=result.checks)
    pending = tuple(dict.fromkeys(name for verdict in verdicts for name in verdict.pending))
    if pending:
        return CIResult("pending", pending=pending, checks=result.checks)
    return CIResult("success", checks=result.checks)


def pr_ci(result: CIResult, factory: FactoryConfig, pr_base: str | None) -> CIResult:
    base = no_ci_base(factory, pr_base)
    if base is not None:
        return ci_on_base(result, base)
    return ci_required(result, factory.ci_required_checks)


CIChangeKind = Literal["head-workflow", "required-set", "base-workflow", "retarget"]


@dataclass(frozen=True)
class CIChange:
    kind: CIChangeKind
    at: float
    globs: tuple[str, ...] = ()

    @property
    def trigger(self) -> str:
        return f"{self.kind}:{','.join(self.globs)}@{self.at:.0f}"


def _named_units(rows: tuple[CheckRow, ...]) -> list[tuple[str, list[CheckRow]]]:
    names = dict.fromkeys(row.name for row in rows if row.conclusion == "success")
    return [(name, [row for row in rows if row.name == name]) for name in names]


def _gate_units(
    result: CIResult, change: CIChange, required: tuple[str, ...]
) -> list[tuple[str, list[CheckRow]]]:
    globs = change.globs or required
    if globs:
        return [(glob, [row for row in result.gate_checks if fnmatchcase(row.name, glob)]) for glob in globs]
    units = _named_units(result.gate_checks) + _named_units(result.statuses)
    return units or [("CI", [])]


def _began(row: CheckRow, change: CIChange) -> float | None:
    # A rerun or a delayed job of a workflow run keeps the merge ref of the
    # event that created the run, so after a retarget only the run's creation
    # says which base it tested.
    if change.kind == "retarget" and row.run_id is not None:
        return row.run_created_at
    return row.started_at


def _fresh(row: CheckRow, change: CIChange) -> bool:
    # A run's start time does not say which base it merged: a workflow commit
    # can be pushed long after it was made. Only a head containing the commit
    # runs the base workflow, and such a head raises no base-workflow change.
    if change.kind == "base-workflow":
        return False
    began = _began(row, change)
    return row.conclusion == "success" and began is not None and began > change.at


def stale_units(
    result: CIResult, change: CIChange, required: tuple[str, ...]
) -> list[tuple[str, list[CheckRow]]]:
    return [
        (unit, rows)
        for unit, rows in _gate_units(result, change, required)
        if not any(_fresh(row, change) for row in rows)
    ]


def _unreported(result: CIResult, changes: tuple[CIChange, ...]) -> tuple[CIChange, ...]:
    added = [change for change in changes if change.kind == "required-set" and change.globs]
    missing = tuple(
        change
        for change in added
        if f"{change.globs[0]} (not reported)" in result.pending
        and all(
            row.conclusion is not None and (row.started_at or 0) <= change.at
            for row in result.gate_checks
        )
    )
    names = {f"{change.globs[0]} (not reported)" for change in missing}
    return missing if result.gate_checks and set(result.pending) <= names else ()


def _before_retarget(result: CIResult, changes: tuple[CIChange, ...]) -> CIResult:
    cuts = [change for change in changes if change.kind == "retarget"]
    if result.status != "failure" or not cuts:
        return result
    runs = {row.run_id: row for row in result.gate_checks if row.run_id is not None}

    def stale(check: FailedCheck) -> bool:
        row = runs.get(check.run_id) if check.run_id is not None else None
        began = None if row is None else _began(row, cuts[0])
        return row is not None and (began is None or began <= cuts[0].at)

    kept = tuple(check for check in result.failed if not stale(check))
    if len(kept) == len(result.failed):
        return result
    if kept:
        return replace(result, log="failed: " + ", ".join(check.name for check in kept), failed=kept)
    names = [f"{check.name} (stale after retarget)" for check in result.failed]
    return CIResult(
        "pending",
        pending=tuple(dict.fromkeys((*result.pending, *names))),
        checks=result.checks,
        statuses=result.statuses,
        stale=(cuts[0],),
    )


def ci_fresh(result: CIResult, changes: tuple[CIChange, ...], required: tuple[str, ...]) -> CIResult:
    result = _before_retarget(result, changes)
    if result.status == "pending":
        missing = _unreported(result, changes)
        return replace(result, stale=missing) if missing else result
    if result.status != "success":
        return result
    stale = {
        change: [f"{unit} (stale after {change.kind})" for unit, _rows in units]
        for change in changes
        if (units := stale_units(result, change, required))
    }
    if not stale:
        return result
    return CIResult(
        "pending",
        pending=tuple(dict.fromkeys(name for names in stale.values() for name in names)),
        checks=result.checks,
        statuses=result.statuses,
        stale=tuple(stale),
    )


# Where a Linear issue stands relative to the project's ready state (the
# first linear_handoff_states name), read with the ticket: still before review
# (backlog, triage, unstarted, or started before the ready state), already in
# the ready state, moved on by a human (completed, canceled, or a started
# state after the ready state), on a team with no ready state at all (the PR
# is still marked ready and the done wake reports the unmoved issue), or sent
# back by a reviewer to one of the project's linear_rework_states. The handoff
# moves only before-review and rework issues.
LINEAR_STATE_PHASES = (
    "before-review",
    "ready-for-review",
    "moved-on",
    "no-ready-state",
    "rework",
)


@dataclass(frozen=True)
class StackParent:
    pr: int
    branch: str
    sha: str


@dataclass(frozen=True)
class ParentOpen:
    pass


@dataclass(frozen=True)
class ParentClosed:
    pr: int


@dataclass(frozen=True)
class ParentMerged:
    into: str
    head: str
    heads: tuple[str, ...]


ParentFacts = ParentOpen | ParentClosed | ParentMerged


@dataclass(frozen=True)
class RetargetProof:
    onto_from: str
    head: str
    parent_heads: tuple[str, ...]
    parent_head: str | None = None


@dataclass(frozen=True)
class StackedOn:
    pr: int
    branch: str
    default_branch: str


@dataclass(frozen=True)
class TrackedTicket:
    repo: str
    ticket_id: str
    factory: FactoryConfig
    comments: tuple[Comment, ...]
    current_head: str | None
    ci: Mapping[str, CIResult]
    risk: str
    job_timeout_minutes: int = DEFAULT_JOB_TIMEOUT_MINUTES
    ticket_url: str | None = None
    git_branch_name: str | None = None
    pr_number: str | None = None
    reviews: tuple[PullReview, ...] = ()
    brief: str | None = None
    brief_text: str | None = None
    brief_url: str | None = None
    # #495: the trusted brief the poll picked once (canonical_brief); brief
    # and the reviewer's criteria both come from it. issue_body and pr_body
    # are the source issue's text and the PR description, None when unread.
    brief_comment: Comment | None = None
    issue_body: str | None = None
    read_issue_body: Callable[[], str | None] | None = field(default=None, compare=False, repr=False)
    pr_body: str | None = None
    grok_bot: str = "grok-bot"
    linear_issue_id: str | None = None
    linear_ready_state_id: str | None = None
    linear_reopen_state_id: str | None = None
    linear_ready_for_review: bool = False
    linear_state_phase: str | None = None
    ready_starts_review: bool = False
    # Issue #59: the PR had no orch:state/ label and was a candidate only
    # because it carries a parseable ready. Only a trusted builder ready
    # naming the PR head may then start tracking (Codex PR #64 r1 F1.1).
    admitted_by_ready: bool = False
    # Issue #86: read only when factory.md has hold: on a ship: merge repo.
    # None means not read; the hold then fails closed (no merge, retry).
    # labels are the PR's labels, changed_files the paths its diff against
    # base touches at current_head, label_events its labeled / unlabeled
    # events.
    labels: tuple[str, ...] | None = None
    changed_files: tuple[str, ...] | None = None
    label_events: tuple[LabelEvent, ...] | None = None
    factory_md_trust_changed: bool | None = None
    # Issue #206: factory.md's config when a go routed this ticket to other
    # harnesses; factory then holds the routed pair.
    factory_md: FactoryConfig | None = None
    router_picks: RouterPicks | None = None
    # Issue #115: the source issue (`#115`, or a Linear key) and the open
    # PR's title, so the poller can prefix a title that lacks it.
    source_issue: str | None = None
    pr_title: str | None = None
    # #108 (#42 N3): the PR is merged. A fresh read never records the merge
    # itself: its caller decides.
    merged: bool = False
    # #130: GitHub's mergeability (None while GitHub computes it) and a PR
    # closed without merging.
    mergeable: bool | None = None
    mergeable_state: str | None = None
    closed: bool = False
    closed_at: float | None = None
    draft: bool | None = None
    intake_label: str | None = None
    changed_lines: int | None = None
    pr_base: str | None = None
    pr_commits: int | None = None
    stack: StackParent | None = None
    stacked_on: StackedOn | None = None

    @property
    def no_ci_base(self) -> str | None:
        return no_ci_base(self.factory, self.pr_base)

    def __post_init__(self) -> None:
        if not self.repo or not self.ticket_id:
            raise ValueError("repo and ticket_id must not be empty")
        if self.risk not in ("low", "high"):
            raise ValueError("risk must be low or high")
        if self.job_timeout_minutes < 1:
            raise ValueError("job_timeout_minutes must be positive")


@dataclass(frozen=True)
class TicketRef:
    repo: str
    ticket_id: str
    tracker: str
    allow_merged: bool = False
    pr_number: str | None = None
    closed: bool = False


@dataclass(frozen=True)
class LinearState:
    name: str
    type: str
    handoff: bool
    closed_at: float | None = None


@dataclass(frozen=True)
class LinearStateRead:
    states: Mapping[str, LinearState]
    errors: Mapping[str, str]


class PollSource(Protocol):
    def github_candidates(
        self,
        label_prefix: str,
        *,
        exclude: frozenset[tuple[str, str]],
    ) -> tuple[TrackedTicket, ...]: ...

    def linear_candidates(
        self,
        query: str,
        *,
        exclude: frozenset[str],
        reserve: int,
    ) -> tuple[TrackedTicket, ...]: ...

    def tracked_tickets(
        self,
        tickets: tuple[TicketRef, ...],
    ) -> tuple[TrackedTicket, ...]: ...

    def fresh_ticket(self, ticket: TicketRef) -> TrackedTicket | None: ...


class ReviewActions(Protocol):
    def set_state(self, ticket: TrackedTicket, state: str) -> None: ...

    def stable_patch_id(self, ticket: TrackedTicket, head: str) -> str: ...

    def is_merged(self, ticket: TrackedTicket, head: str) -> bool:
        """True only when the PR was merged with exactly this head commit.

        Reads GitHub's PR state at call time, never a local clone. Squash
        and merge-commit merges also count, so this never proves where the
        base branch points.
        """
        ...

    def merge(
        self,
        ticket: TrackedTicket,
        *,
        squash: bool,
        match_head_commit: str,
    ) -> None: ...

    def fast_forward(
        self,
        ticket: TrackedTicket,
        *,
        branch: str,
        head: str,
    ) -> None:
        """Move branch to exactly head, adding no commit.

        Must be a fast-forward-only update of GitHub's remote ref (for
        example PATCH git/refs/heads/<branch> with force=false), never a
        push from a local clone. Raises when head is not a fast-forward of
        branch's current tip (branch moved, or history diverged). Never
        merges, squashes or force-pushes instead.
        """
        ...

    def branch_tip(self, ticket: TrackedTicket, branch: str) -> str:
        """The full SHA of GitHub's remote ref for branch, read at call time.

        Never a local clone or a cached value: a ship counts only when this
        equals the approved head.
        """
        ...

    def commits_ahead(self, ticket: TrackedTicket, *, base: str, head: str) -> int:
        ...

    def reopen(self, ticket: TrackedTicket) -> None:
        ...

    def set_pull_base(self, ticket: TrackedTicket, base: str) -> None:
        """Retarget the ticket's PR onto base. A repeat is a no-op."""
        ...

    def close_pull(self, ticket: TrackedTicket) -> None:
        """Close the ticket's unmerged PR. A repeat is a no-op."""
        ...

    def handoff(self, ticket: TrackedTicket, head: str) -> str | None:
        """Hand the PR off. A returned note is reported in the done wake."""
        ...

    def reopen_linear(self, ticket: TrackedTicket) -> str | None:
        """Move a reopened ticket's tracker issue out of its handoff state.

        A returned note means the issue was not moved.
        """
        ...

    def await_approver(
        self,
        ticket: TrackedTicket,
        head: str,
        approver: str,
    ) -> None: ...

    def set_title(self, ticket: TrackedTicket, title: str) -> None:
        """Rename the ticket's PR (issue #115)."""
        ...

    def file_issue(
        self,
        ticket: TrackedTicket,
        title: str,
        body: str,
        labels: tuple[str, ...],
        marker: str,
        since: float,
    ) -> int:
        """Open an issue in the ticket's repo once (issue #259).

        Returns the number of an issue created since `since` (epoch seconds)
        whose body has `marker` on a line of its own, else creates one and
        returns its number. The lookup is a consistent read, not search.
        """
        ...

    def comment_once(self, ticket: TrackedTicket, body: str, marker: str) -> None:
        """Post body on the PR unless a comment carrying marker exists."""
        ...

    def open_issue_labels(self, ticket: TrackedTicket, number: int) -> tuple[str, ...] | None:
        """The labels of issue `number` in the ticket's repo, or None once it is closed."""
        ...

    def comment_on_issue_once(
        self, ticket: TrackedTicket, number: int, body: str, marker: str
    ) -> None:
        """Post body on issue `number` unless a comment carrying marker exists."""
        ...


class IssueComments(Protocol):
    def issue_has_marker(self, repo: str, number: int, marker: str) -> bool:
        """Whether this login has a comment carrying marker on issue `number` in repo."""
        ...

    def post_issue_comment(self, repo: str, number: int, body: str) -> None:
        ...


class GrokDelivery(Protocol):
    """Push a Grok job and return a non-empty delivery receipt.

    Any failure after the push but before queue completion can repeat the wake.
    Implementations must deduplicate on logical_key, which is stable across
    T6 retries, rather than on job.id.
    """

    def deliver(self, job: Job, *, logical_key: str) -> str: ...


class RetryableStateError(RuntimeError):
    pass


class AlreadyDeferred(RuntimeError):
    pass


class RetryableShipError(RetryableStateError):
    pass


class RouteError(RuntimeError):
    """A ticket's restored routing fails factory.md's cross-family rule, or
    factory.md's routing cannot pick a pair."""

    def __init__(self, message: str, source_comment_id: str):
        super().__init__(message)
        self.source_comment_id = source_comment_id


class BaseMismatch(RuntimeError):
    def __init__(self, message: str, ticket: TrackedTicket):
        super().__init__(message)
        self.ticket = ticket


class PollCycleFailed(RuntimeError):
    pass


class ShipError(RuntimeError):
    """A ship that did not complete.

    write_confirmed is True unless the raiser knows that no external write
    it sent was confirmed before it failed. A handoff failure with
    write_confirmed False, or a merge failure the raiser marked retryable
    (a draft or not-yet-mergeable PR, a GitHub 5xx, a timeout: issue #83),
    is eligible for the one ship re-drive. safe_detail, when set, is the
    only text recorded in ship_intents.error.
    """

    write_confirmed: bool = True
    retryable: bool = False
    safe_detail: str | None = None


class AlreadyMergedShip(ShipError):
    """The PR was merged before the ship could hand it off (#108, #42 N3):
    the ship has nothing left to do, so it ends as merged, not failed."""


@dataclass(frozen=True)
class ShipRequest:
    repo: str
    ticket_id: str
    head: str
    approver: str
    source_comment_id: str
    approve_comment_id: str


class Hook(Protocol):
    """A project hook (T7, T8) sees each unhandled comment before T1/T1P/T2.

    fields is None when the comment has no parseable orch:v1 header.
    handle returns None when the comment is not the hook's event, or
    (handled, outcome); handled comments go into the ledger. Hooks run in
    order and the first answer wins, so (False, outcome) leaves the
    comment for the next poll without offering it to later hooks or the
    core rows. kind=go, kind=plan and kind=ready are never offered to hooks.
    """

    def initialize(self, dispatcher: Dispatcher) -> None: ...

    def handle(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str] | None,
    ) -> tuple[bool, str] | None: ...


class DeviceChecks(Protocol):
    """F2-02's DeviceMonitor: release gone workers' slots, find lost jobs.

    A DeviceMonitor also has `liveness(placement)` (#75), which the
    dispatcher uses, when present, to find dead jobs.
    """

    def reconcile(
        self,
        now: float,
        lost_after: float,
        on_lost: Callable[[str, str, str], None],
    ) -> object: ...


class Feeder(Protocol):
    """The backlog feeder (lib/feeder.py, issue #85).

    feed_once opens a draft PR plus brief and go handoffs for each eligible
    ticket that fits the free capacity and returns a report whose `fed`
    lists the tickets it fed this poll as TicketRefs. review_base_unless_stacked
    holds a ready's review, with the same Grok wake, while the ready's head carries
    another open PR's commits, and returns None when it did; otherwise it
    returns the base the review runs against, the open PR's own base.
    """

    ledger: LockLedger | None

    def feed_once(self) -> object: ...

    def review_base_unless_stacked(self, ticket: TrackedTicket, head: str) -> str | None: ...

    def stacked_parent_facts(self, ticket: TrackedTicket) -> ParentFacts | None: ...

    def move_stack_base(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        base: str,
        proof: RetargetProof,
    ) -> bool: ...

    def pending_retarget(
        self, connection: sqlite3.Connection, ticket: TrackedTicket
    ) -> RetargetProof | None: ...

    def set_pending_retarget(
        self, connection: sqlite3.Connection, ticket: TrackedTicket, proof: RetargetProof | None
    ) -> None: ...


# (repo, base branch, approved head, updated head, shared_paths) -> whether the update is
# trivial, and why (lib/update_check.py).
UpdateClassifier = Callable[[str, str, str, str, tuple[str, ...]], UpdateClass]

# (repo, base branch, last reviewed head, new head) -> None when the new head
# descends from the last reviewed one, else both fork points (lib/update_check.py).
RebaseClassifier = Callable[[str, str, str, str], RebaseScope | None]


class HoldCarry(Protocol):
    def __call__(self, repo: str, base: str, approved: str, head: str) -> MergeUpdate: ...


class MovedHeadProbe(Protocol):
    def __call__(self, repo: str, base: str, ok: str, head: str) -> MovedHead: ...


class StackBoundsProbe(Protocol):
    def __call__(self, repo: str, old_head: str, parent_heads: tuple[str, ...]) -> str: ...


class RetargetDeltaProbe(Protocol):
    def __call__(
        self, repo: str, base: str, onto_from: str, from_head: str, parent_head: str, head: str
    ) -> RetargetDelta: ...


class QaDiff(Protocol):
    def __call__(self, repo: str, base: str, since: str | None, head: str) -> HeadDiff: ...
