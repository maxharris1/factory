"""Backlog feeder: tagged tickets become queued jobs by themselves (issue #85).

Each poll, before discovery, the feeder reads every project whose orch.yml
entry has an `intake` section and finds its eligible tickets:

* a GitHub project (factory.md `tickets: github`): an open issue carrying
  the project's intake label, applied by a trusted login;
* a Linear project: an issue in the project's intake workflow state, moved
  there by a trusted Linear user and, when `linear_assignees` lists its
  Linear project, assigned to one of the users listed for it.

The signal (label or state) and the trusted appliers are per-project
config (lib/orch_yml.py Intake). Nothing here names a project, a label or a
state. Trusted appliers default to the keys of `trusted_logins` (GitHub) or
`trusted_linear_users` (Linear).

For each eligible ticket, in priority order and only while its builder
harness has room, the feeder:

1. opens a branch from the base branch with an empty start commit (a GitHub
   issue gets `factory/issue-<n>`; a Linear issue uses its own
   gitBranchName);
2. opens a draft PR whose body says "Fixes <ticket>";
3. on a GitHub-ticket repo only, labels the PR `factory` and
   `orch:state/<building|planning>` (a Linear repo gets no GitHub labels);
4. posts a `brief` handoff rendered from the ticket body plus the repo's
   factory.md prose, then a `go` handoff (state=planning when the ticket
   says `Plan first: yes` or names more than 3 files, else building; risk
   from the ticket's `Risk:` line, raised to high by a `risk:high` label),
   both as the orchestrator. On a Linear project both go on the Linear
   issue, in the visible header form.

Intake template (#204, parse_intake): a ticket not started yet is fed only
when its body has an `Acceptance criteria` section with at least one
builder criterion, a `Risk: low|high` line, a `Plan first: yes|no` line and
a `Depends on:` line. Risk is never defaulted to low. A criterion about
other PRs' state (SF-5, the N-02 case) is refused: PR order belongs in
`Blocked by` / `Conflicts with`. A criterion that needs an operator action
is not refused; it goes into the brief's separate ops step. A ticket that
fails is `refused-intake` with its reasons, takes no room and no holds,
posts nothing, raises needs-attention (`intake-refused`) and queues one
Grok `refused` wake per distinct defect set. It is re-checked every poll,
so a fixed ticket feeds. A ticket already feeding or fed is not re-checked.

It is idempotent, keyed on (repo, ticket): `feeder_items` records each step
(branch, PR, comment ids) before the next, a PR already open on the branch
is reused, and a brief or go already on the ticket is not posted again. A
ticket that was fed is never fed again, so a re-poll never opens a second PR.
Nor is a ticket that `ticket_state` (read-only) shows past `questions` with
no feed in flight, so one started by a go comment and moved back to the
intake state by hand is not fed as new (#749).
A feed that resumes on a recorded PR first checks that PR is still open; one
closed or merged meanwhile ends the feed (`closed`) with nothing posted.

Room for a harness h is the free slots on every schedulable device that
lists h (machines.yml, minus held slots), minus the jobs queued for h,
minus the tickets this feeder fed for h in the last FED_PENDING_SECONDS
whose job is not queued yet. Every ticket fed this poll takes one unit of
room, a resumed one (some steps already done) as much as a new one; resumed
tickets only go first. So the queue never holds more work for a harness
than it has free slots.

Gates (#156, lib/feeder_gates.py): a ticket not started yet is fed only
once every ticket or PR its `Blocked by` or `Depends on` lines (and, on
Linear, its blocks relations) name is done, fewer than the project's
factory.md `feeder.max_holders_per_path` tickets in flight (default 2, #269)
hold each of its `Touches:` paths, and nothing in flight shares a
`Conflicts with` edge with it. A `Touches:` path in the project's
`shared_paths` never collides; one in its `feeder.exclusive_paths` keeps one
holder. A ticket fed onto a path another ticket holds logs
`feeder: owner/app#3 shares lib/x.py with #1`, and the serial merge plus the
base update resolve the overlap. In flight is a fed ticket whose PR is still
open, or one selected earlier in this poll. A ticket that waits is
`waiting-blocked` with its reason and takes no room; it is re-checked every
poll. Blocker and holder statuses come from one batched, cached read per
poll, and none when no ticket has a gate line. A blocker cycle, a blocker
closed without finishing and one that does not exist raise needs-attention.
A holder whose ticket is `capped` in `ticket_state` (read-only) holds only
its `Conflicts with` edges while it waits for a go, and takes its paths back
on the poll after the round resumes (#270).

Stacked branches (#205, lib/branch_stack.py): a ticket whose branch already
exists and carries commits of another open PR is `waiting-blocked` until
that PR merges, with one Grok `blocked` wake per set of such PRs. The branch
is read at intake when it already exists, and again before a builder's
ready queues review, so a branch stacked during its build waits there too.
The branch is compared against its open PR's base, or against the recorded
base when it has no PR.

Priority order (#258, lib/queue.py rank): a ticket's tier first (the first of
the project's `priority_labels` it carries, or its Linear priority when the
project sets `priority_from_linear`; no tier sorts after every tier), then
urgent, then bug, then how many of this poll's candidates it unblocks
(critical_path_counts; 0 with `critical_path_order: false`), then the oldest.

Each poll also refreshes the claim order (`ticket_priority`) of every ticket
the feeder has a ticket id for, so a label or blocker change after the feed
still moves its queued job.

The feeder starts no harness and runs no job. It writes only through the
GitHub and Linear clients it is given; tests pass fakes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import NamedTuple, Protocol

from markdown_it import MarkdownIt
from markdown_it.token import Token

from lib import attention
from lib.branch_stack import OpenPull, stack_wait, stacked_pulls
from lib.brief_comment import FACTORY_MD_HEADING
from lib.device_pool import free_slots, placement_blocked, slots_in_use
from lib.dispatch.bookkeeping import LockLedger, bookkeeping_write, must_write
from lib.dispatcher import Comment, TicketRef, TrackedTicket, safe_ship_error
from lib.factory_md import FactoryContext
from lib.feeder_gates import (
    ATTENTION_REASONS,
    FILE_PATH,
    GateLines,
    Holds,
    PathPolicy,
    Progress,
    Ref,
    RefStatus,
    Wait,
    blocker_waits,
    github_ref,
    labels,
    linear_status,
    parse_gate_lines,
    path_matches,
    paths_collide,
)
from lib.handoff import Handoff, HandoffError, format_comment, parse_comment
from lib.machines_yml import MachinesConfig
from lib.orch_yml import OrchConfig, Project, resolve_job_timeout
from lib.pr_title import prefixed, read_sources
from lib.queue import (
    JobRequest,
    SQLiteJobQueue,
    _has_table,
    ensure_priority_table,
    rank,
    set_ticket_priority,
)

FEEDER_TABLE = "feeder_items"
POLL_TABLE = "feeder_poll"  # one row: when the feeder last read intake
# eligible: seen this poll, not fed yet; waiting-capacity: no room for its
# harness this poll; feeding: some steps done; error: a step failed (retried
# next poll); fed: brief and go posted; done: fed and its job is queued;
# closed: its recorded PR was closed or merged before the feed finished;
# waiting-blocked: a blocker is not done or an in-flight ticket holds one of
# its paths or a conflicts-with edge (waiting_reason says which);
# refused-intake: its body fails the intake template (waiting_reason says how).
STATES = (
    "eligible",
    "waiting-capacity",
    "waiting-blocked",
    "refused-intake",
    "feeding",
    "error",
    "fed",
    "done",
    "closed",
)
FEED_NOT_BEGUN = "branch IS NULL"
UNSTARTED = ("eligible", "waiting-capacity", "waiting-blocked", "refused-intake")
IN_FLIGHT = ("feeding", "error")
FED = ("fed", "done")
FINAL = (*FED, "closed")
HOLDING = (*IN_FLIGHT, *FED)
_COLUMNS = (
    ("waiting_reason", "TEXT"),
    ("touches", "TEXT"),
    ("conflicts", "TEXT"),
    ("released", "INTEGER NOT NULL DEFAULT 0"),
    ("capped_release", "INTEGER NOT NULL DEFAULT 0"),
    ("tier", "INTEGER"),
    ("unblocks", "INTEGER NOT NULL DEFAULT 0"),
    ("base_branch", "TEXT"),
)
ORCHESTRATOR = "grok-bot"
FACTORY_LABEL = "factory"
URGENT_LABELS = frozenset({"urgent", "priority:urgent"})
BUG_LABELS = frozenset({"bug"})
RISK_HIGH_LABEL = "risk:high"
INTAKE_REFUSED = "intake-refused"  # needs_attention reason
LINEAR_URGENT_PRIORITY = 1
LINEAR_PRIORITIES = (1, 2, 3, 4)  # Urgent, High, Medium, Low; 0 is no priority
PLANNING_FILE_COUNT = 3  # SPEC section 4.1 (D36): over 3 files starts with a plan
# A fed ticket holds room until its job is queued, at most this long: a go
# the poller never acts on (say, an untrusted author) must not stall the
# feeder for its harness forever.
FED_PENDING_SECONDS = 600
BODY_LIMIT = 20_000

_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")

Log = Callable[[str], None]

# -- intake template (#204) ----------------------------------------------------

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_CRITERIA_HEADING = re.compile(r"^(?:\*\*|__)?acceptance\s+criteria(?:\*\*|__)?:?$", re.IGNORECASE)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.*)$")
_FIELD = r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?\s*{name}\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(?P<value>.*?)\s*$"
_RISK_LINE = re.compile(_FIELD.format(name=r"risk"), re.IGNORECASE)
_PLAN_LINE = re.compile(_FIELD.format(name=r"plan\s+first(?:\s*\(yes\s*/\s*no\))?"), re.IGNORECASE)
_DEPENDS_LINE = re.compile(_FIELD.format(name=r"depends\s+on"), re.IGNORECASE)
_OTHER_PRS = (
    re.compile(
        r"\b(?:other|every|all|any)\s+(?:other\s+)?(?:open\s+)?(?:PRs?|pull\s+requests?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bmerges?\s+(?:cleanly|without\s+(?:a\s+)?conflicts?)\s+(?:against|with)\b", re.IGNORECASE),
    re.compile(r"\b(?:PRs?|pull\s+requests?)\s*(?:#|number\s+|no\.\s*)?\d+\b", re.IGNORECASE),
    re.compile(r"/pull/\d+\b"),
)
_MARKDOWN = MarkdownIt("commonmark").enable("strikethrough")
_OPS_TAG = re.compile(r"^(?:ops\s*:|\(ops\)|\[ops\])\s*", re.IGNORECASE)
_OPS_ACTION = re.compile(
    r"^(?:manually|by\s+hand)\b|^(?:restart|install|reboot|rotate)\s+the\b|\bon\s+the\s+box\b|"
    r"\b(?:Max|the\s+operator|an\s+operator)\s+(?:runs|installs|restarts|sets|rotates|deploys)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Defect:
    code: str  # no-criteria, other-prs-criterion, no-risk, no-plan-first, no-depends-on
    reason: str


@dataclass(frozen=True)
class IntakeTemplate:
    """What a ticket body says in the intake template's fields."""

    criteria: tuple[str, ...] = ()
    ops_criteria: tuple[str, ...] = ()
    risk: str | None = None  # "low" or "high"; None when the template does not say
    risk_source: str | None = None  # "Risk line" or "risk:high label"
    plan_first: bool | None = None
    depends_on: str | None = None
    defects: tuple[Defect, ...] = ()


def _unfenced(body: str) -> list[str]:
    lines: list[str] = []
    fenced = False
    for line in (body or "").splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def _field(lines: Sequence[str], pattern: re.Pattern[str]) -> str | None:
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group("value").strip("*_ ").strip()
    return None


def _prose_and_link_targets(item: str, document: dict[str, object]) -> tuple[str, ...]:
    words: list[str] = []
    targets: list[str] = []

    def walk(tokens: Sequence[Token]) -> None:
        for token in tokens:
            if token.type in ("text", "code_inline"):
                words.append(token.content)
            elif token.type in ("softbreak", "hardbreak"):
                words.append(" ")
            elif token.type == "link_open":
                targets.append(str(token.attrs["href"]))
            elif token.type == "image":
                targets.append(str(token.attrs["src"]))
            elif token.type == "html_inline":
                targets.append(token.content)
            walk(token.children or ())

    walk(_MARKDOWN.parseInline(item, document))
    return ("".join(words), *targets)


def _criteria_items(lines: Sequence[str]) -> list[str]:
    items: list[str] = []
    level: int | None = None
    open_item = blank = False
    for line in lines:
        heading = _HEADING.match(line)
        if heading:
            open_item = blank = False
            depth = len(heading.group(1))
            if level is not None and depth <= level:
                level = None
            if level is None and 2 <= depth <= 4 and _CRITERIA_HEADING.match(heading.group(2)):
                level = depth
            continue
        if level is None:
            continue
        if not line.strip():
            blank = True
            continue
        item = _LIST_ITEM.match(line)
        if item:
            current = item.group(1).strip()
            if current:
                items.append(current)
            open_item, blank = bool(current), False
            continue
        if open_item and (not blank or line[:1].isspace()):
            items[-1] = f"{items[-1]} {line.strip()}"
        else:
            open_item = False
        blank = False
    return items


def parse_intake(body: str, labels: Collection[str]) -> IntakeTemplate:
    """The ticket's intake template fields and what is wrong with them."""
    lines = _unfenced(body)
    defects: list[Defect] = []
    criteria: list[str] = []
    ops: list[str] = []
    for item in _criteria_items(lines):
        tagged = _OPS_TAG.match(item)
        if tagged:
            ops.append(item[tagged.end():].strip())
        elif _OPS_ACTION.search(item):
            ops.append(item)
        else:
            criteria.append(item)
    if not criteria:
        defects.append(Defect(
            "no-criteria",
            "no builder acceptance criteria: add an `## Acceptance criteria` section "
            "with at least one item the builder can meet",
        ))
    document: dict[str, object] = {}
    _MARKDOWN.parse(body or "", document)
    for item in criteria:
        if any(pattern.search(text) for text in _prose_and_link_targets(item, document)
               for pattern in _OTHER_PRS):
            defects.append(Defect(
                "other-prs-criterion",
                f"criterion about other PRs' state ({item!r}): move PR order into "
                "`Blocked by` / `Conflicts with` lines",
            ))
    risk_text = _field(lines, _RISK_LINE)
    risk = risk_text.lower() if risk_text and risk_text.lower() in ("low", "high") else None
    source = "Risk line" if risk else None
    if any(label.lower() == RISK_HIGH_LABEL for label in labels) and risk != "high":
        risk, source = "high", "risk:high label"
    if risk_text is None or risk_text.lower() not in ("low", "high"):
        defects.append(Defect("no-risk", "no `Risk: low` or `Risk: high` line"))
    plan_text = (_field(lines, _PLAN_LINE) or "").lower()
    plan_first = {"yes": True, "no": False}.get(plan_text)
    if plan_first is None:
        defects.append(Defect("no-plan-first", "no `Plan first: yes` or `Plan first: no` line"))
    depends = _field(lines, _DEPENDS_LINE) or None
    if depends is None:
        defects.append(Defect("no-depends-on", "no `Depends on:` line (`Depends on: none` is fine)"))
    return IntakeTemplate(
        criteria=tuple(criteria),
        ops_criteria=tuple(ops),
        risk=risk,
        risk_source=source,
        plan_first=plan_first,
        depends_on=depends,
        defects=tuple(defects),
    )


@dataclass(frozen=True)
class IntakeIssue:
    """One ticket an intake source reports as carrying the project's signal."""

    repo: str
    tracker: str  # "github" or "linear"
    key: str  # the GitHub issue number or the Linear identifier
    title: str
    body: str
    labels: tuple[str, ...]
    created_at: float
    applied_by: str | None  # who applied the label, or moved the issue to the state
    url: str | None = None
    linear_id: str | None = None
    branch: str | None = None  # Linear's gitBranchName
    priority: int | None = None  # Linear priority (1 is Urgent)
    # Linear blocks relations naming this issue: (blocker identifier, state type).
    blocked_by: tuple[tuple[str, str | None], ...] = ()
    linear_project: str | None = None
    assignee: str | None = None

    @property
    def urgent(self) -> bool:
        return (
            any(label.lower() in URGENT_LABELS for label in self.labels)
            or self.priority == LINEAR_URGENT_PRIORITY
        )

    @property
    def bug(self) -> bool:
        return any(label.lower() in BUG_LABELS for label in self.labels)


class GitHubIntake(Protocol):
    """GitHub reads and writes the feeder needs. Tests pass a fake."""

    def github_intake(self, repo: str, label: str) -> Sequence[IntakeIssue]: ...

    def branch_sha(self, repo: str, branch: str) -> str | None: ...

    def create_start_commit(self, repo: str, base_sha: str, message: str) -> str: ...

    def create_branch(self, repo: str, branch: str, sha: str) -> None: ...

    def find_pull(self, repo: str, branch: str) -> int | None: ...

    def ahead_commits(self, repo: str, base: str, tip: str) -> Sequence[str]: ...

    def open_pull_commits(self, repo: str) -> Mapping[int, OpenPull]: ...

    def pull_commits(self, repo: str, base: str, head: str) -> Collection[str]: ...

    def pull_state(self, repo: str, number: int) -> str: ...

    def pull_base(self, repo: str, number: int) -> str: ...

    def open_draft_pull(
        self, repo: str, *, branch: str, base: str, title: str, body: str
    ) -> int: ...

    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> None: ...

    def comments(self, repo: str, number: int) -> Sequence[Comment]: ...

    def post_comment(self, repo: str, number: int, body: str) -> str: ...

    def ref_statuses(self, refs: Collection[Ref]) -> Mapping[Ref, RefStatus]: ...


class LinearIntake(Protocol):
    def linear_intake(
        self,
        project_ids: frozenset[str],
        state: str,
        assignees: Mapping[str, frozenset[str]],
    ) -> Sequence[IntakeIssue]: ...

    def linear_comments(self, issue_id: str) -> Sequence[Comment]: ...

    def post_linear_comment(self, issue_id: str, body: str) -> str: ...


@dataclass(frozen=True)
class FeedReport:
    fed: tuple[TicketRef, ...] = ()
    eligible: int = 0
    waiting: int = 0
    blocked: int = 0
    errors: int = 0
    refused: int = 0


@dataclass
class _Candidate:
    project: Project
    context: FactoryContext
    issue: IntakeIssue

    lines: GateLines = GateLines()
    template: IntakeTemplate = IntakeTemplate()
    unblocks: int = 0

    @property
    def harness(self) -> str:
        return self.context.config.builder_harness

    @property
    def base(self) -> str | None:
        intake = self.project.intake
        if intake is not None and intake.base_branches and self.issue.linear_project is None:
            return None
        return self.project.base_for(self.issue.linear_project) or self.context.config.base_branch

    @property
    def tier(self) -> int | None:
        intake = self.project.intake
        assert intake is not None
        labels = {label.lower() for label in self.issue.labels}
        for index, label in enumerate(intake.priority_labels):
            if label.lower() in labels:
                return index
        if intake.priority_from_linear and self.issue.priority in LINEAR_PRIORITIES:
            return LINEAR_PRIORITIES.index(self.issue.priority)
        return None

    @property
    def ref(self) -> Ref:
        return Ref(self.issue.tracker, self.issue.repo.lower(), self.issue.key)  # type: ignore[arg-type]

    def order(self) -> tuple[bool, int, int, int, float, tuple[int, int, str]]:
        return (
            *rank(tier=self.tier, urgent=self.issue.urgent, resume=False, bug=self.issue.bug,
                  unblocks=self.unblocks),
            self.issue.created_at,
            _key_order(self.issue.key),
        )


def _holding(lines: GateLines, capped: bool) -> GateLines:
    """What a holder holds: a capped one keeps only its Conflicts with edges."""
    return GateLines(conflicts_with=lines.conflicts_with) if capped else lines


@dataclass(frozen=True)
class _Holder:
    """A fed ticket's feeder row: its gate lines, PR, whether its PR ended
    (`released`), its dispatcher ticket and whether it last held as capped."""

    lines: GateLines
    pull: Ref | None
    released: bool
    ticket: tuple[str, str] | None
    capped_release: bool


class _StateWrite(NamedTuple):
    # Field order is the parameter order of the UPDATE in Feeder._set_states.
    state: str
    error: str | None
    reason: str | None
    repo: str
    issue: str


def _state_write(
    candidate: _Candidate, state: str, *, error: str | None = None, reason: str | None = None
) -> _StateWrite:
    return _StateWrite(state, error, reason, candidate.issue.repo.lower(), candidate.issue.key)


class _Held(Exception):
    def __init__(self, wait: Wait):
        super().__init__(wait.reason)
        self.wait = wait


def _key_order(key: str) -> tuple[int, int, str]:
    """A ticket number sorts as a number (9 before 10), an identifier as text."""
    return (0, int(key), "") if key.isdigit() else (1, 0, key)


def gate_lines(issue: IntakeIssue) -> GateLines:
    """The ticket's gate lines, plus its Linear blocks relations."""
    lines = parse_gate_lines(issue.body, issue.repo)
    if not issue.blocked_by:
        return lines
    linear = {Ref("linear", issue.repo.lower(), key) for key, _ in issue.blocked_by}
    return GateLines(lines.blocked_by | linear, lines.touches, lines.conflicts_with)


def go_state_for(body: str, plan_first: bool | None = None) -> str:
    """planning when the ticket says Plan first: yes or names more than 3
    distinct files, else building."""
    if plan_first:
        return "planning"
    files = {match.group(1) for match in FILE_PATH.finditer(body or "")}
    return "planning" if len(files) > PLANNING_FILE_COUNT else "building"


def intake_trusted(config: OrchConfig, project: Project) -> frozenset[str]:
    """Who may apply a project's intake signal."""
    intake = project.intake
    if intake is not None and intake.trusted is not None:
        return intake.trusted
    if project.linear_project_ids:
        # orch.yml keys Linear users as "linear:<user id>".
        return frozenset(key.removeprefix("linear:") for key in config.trusted_linear_users)
    return frozenset(config.trusted_logins)


def assignee_allowed(project: Project, issue: IntakeIssue) -> bool:
    allowlists = project.intake.linear_assignees if project.intake is not None else {}
    if not allowlists:
        return True
    if issue.linear_project not in project.linear_project_ids:
        return False
    allowed = allowlists.get(issue.linear_project)
    return allowed is None or issue.assignee in allowed


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FEEDER_TABLE} (
            repo TEXT NOT NULL,
            issue TEXT NOT NULL,
            tracker TEXT NOT NULL,
            title TEXT NOT NULL,
            harness TEXT NOT NULL,
            state TEXT NOT NULL,
            urgent INTEGER NOT NULL DEFAULT 0,
            bug INTEGER NOT NULL DEFAULT 0,
            risk TEXT NOT NULL,
            issue_created_at REAL NOT NULL,
            branch TEXT,
            pr_number TEXT,
            ticket_id TEXT,
            brief_comment_id TEXT,
            go_comment_id TEXT,
            first_seen_at REAL NOT NULL,
            seen_at REAL NOT NULL,
            fed_at REAL,
            error TEXT,
            waiting_reason TEXT,
            touches TEXT,
            conflicts TEXT,
            released INTEGER NOT NULL DEFAULT 0,
            capped_release INTEGER NOT NULL DEFAULT 0,
            tier INTEGER,
            unblocks INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (repo, issue)
        )
        """
    )
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({FEEDER_TABLE})")}
    for name, kind in _COLUMNS:
        if name not in present:
            connection.execute(f"ALTER TABLE {FEEDER_TABLE} ADD COLUMN {name} {kind}")
    attention.ensure_table(connection)
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {POLL_TABLE} "
        "(id INTEGER PRIMARY KEY CHECK (id = 1), at REAL NOT NULL)"
    )
    ensure_priority_table(connection)


def feeder_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """eligible, fed, waiting_capacity and waiting_blocked among tickets
    eligible at the last poll."""
    counts = {"eligible": 0, "fed": 0, "waiting_capacity": 0, "waiting_blocked": 0}
    for row in feeder_items(connection):
        counts["eligible"] += 1
        if row["state"] in FED:
            counts["fed"] += 1
        elif row["state"] == "waiting-capacity":
            counts["waiting_capacity"] += 1
        elif row["state"] == "waiting-blocked":
            counts["waiting_blocked"] += 1
    return counts


def last_feeder_poll(connection: sqlite3.Connection) -> float | None:
    """When the feeder last read intake, or None when no feeder has run here."""
    if not _has_table(connection, POLL_TABLE):
        return None
    row = connection.execute(f"SELECT at FROM {POLL_TABLE}").fetchone()
    return None if row is None else float(row[0])


def feeder_items(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Tickets eligible at the last poll, priority order, as plain rows."""
    last = last_feeder_poll(connection)
    if last is None or not _has_table(connection, FEEDER_TABLE):
        return []
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({FEEDER_TABLE})")}
    defaults = {"waiting_reason": "NULL", "tier": "NULL", "unblocks": "0", "fed_at": "NULL"}
    optional = ", ".join(name if name in present else f"{value} AS {name}" for name, value in defaults.items())
    cursor = connection.execute(
        f"""
        SELECT repo, issue, tracker, title, harness, state, urgent, bug,
               pr_number, issue_created_at, error, {optional}
        FROM {FEEDER_TABLE} WHERE seen_at = ?
        """,
        (last,),
    )
    names = [column[0] for column in cursor.description]
    items = [dict(zip(names, tuple(row))) for row in cursor.fetchall()]
    items.sort(
        key=lambda row: (
            *rank(tier=row["tier"], urgent=bool(row["urgent"]), resume=False,
                  bug=bool(row["bug"]), unblocks=int(row["unblocks"])),
            float(row["issue_created_at"]),
            str(row["repo"]),
            _key_order(str(row["issue"])),
        )
    )
    return items


class Feeder:
    def __init__(
        self,
        queue: SQLiteJobQueue,
        config: OrchConfig,
        machines: MachinesConfig | None,
        *,
        github: GitHubIntake,
        linear: LinearIntake | None,
        factory_for: Callable[[Project], FactoryContext],
        log: Log | None = None,
    ):
        self.queue = queue
        self.config = config
        self.machines = machines
        self.github = github
        self.linear = linear
        self.factory_for = factory_for
        self.log = log or (lambda line: None)
        self._base_notes: set[tuple[str, int, str, str, str]] = set()
        self.ledger: LockLedger | None = None
        with self.queue.connect() as connection:
            ensure_tables(connection)

    @property
    def projects(self) -> tuple[Project, ...]:
        return tuple(project for project in self.config.projects if project.intake is not None)

    # -- one poll ------------------------------------------------------------

    def feed_once(self) -> FeedReport:
        now = self.queue.now()
        candidates, read_repos, read_errors = self._candidates()
        if not self._record_seen(candidates, now):
            self.log("feeder: intake not recorded, orch.db is locked; feeding next poll")
            return FeedReport(eligible=len(candidates), errors=read_errors + 1)
        self._mark_done()
        states = {candidate.ref: self._state(candidate) for candidate in candidates}
        refused = {
            candidate.ref
            for candidate in candidates
            if states[candidate.ref] in UNSTARTED and candidate.template.defects
        }
        writes = [self._refuse(candidate) for candidate in candidates if candidate.ref in refused]
        self._sync_intake_attention(
            [c for c in candidates if c.ref in refused], read_repos, now
        )
        waits, holds, capped, gate_read, unblocks = self._gates(candidates, states, refused)
        for candidate in candidates:
            candidate.unblocks = unblocks[candidate.ref]
        self._record_priority(candidates, now)
        if gate_read:
            self._sync_attention(waits, read_repos, now)
        room = self._room({candidate.harness for candidate in candidates}, now)
        fed: list[TicketRef] = []
        waiting = 0
        blocked = 0
        errors = read_errors + (0 if gate_read else 1)
        for candidate in sorted(candidates, key=lambda item: (
            states[item.ref] not in IN_FLIGHT, *item.order()
        )):
            if candidate.ref in refused:
                continue
            state = self._state(candidate)
            if state in FINAL:
                continue
            if state in UNSTARTED:
                wait = waits.get(candidate.ref) or holds.collision(candidate.ref, candidate.lines)
                if wait is not None:
                    writes.append(_state_write(candidate, "waiting-blocked", reason=wait.reason))
                    blocked += 1
                    continue
            if room.get(candidate.harness, 0) <= 0:
                if state not in IN_FLIGHT:
                    writes.append(_state_write(candidate, "waiting-capacity"))
                waiting += 1
                continue
            room[candidate.harness] = room.get(candidate.harness, 0) - 1
            sharing = holds.co_holders(candidate.ref, candidate.lines)
            holds.take(candidate.ref, (), _holding(candidate.lines, candidate.ref in capped))
            try:
                ref = self._feed(candidate, now, holds)
            except _Held as held:
                holds.drop(candidate.ref)
                room[candidate.harness] += 1
                writes.append(_state_write(candidate, "waiting-blocked", reason=held.wait.reason))
                blocked += 1
                continue
            except Exception as exc:  # one ticket's failure never stops the poll
                errors += 1
                writes.append(_state_write(candidate, "error", error=safe_ship_error(exc)))
                self.log(
                    f"feeder: {candidate.issue.repo}#{candidate.issue.key} "
                    f"not fed yet: {safe_ship_error(exc)}"
                )
                continue
            if ref is None:
                holds.drop(candidate.ref)
                room[candidate.harness] += 1
                self.log(
                    f"feeder: {candidate.issue.repo}#{candidate.issue.key} not fed: "
                    "its PR was closed or merged"
                )
                continue
            fed.append(ref)
            self.log(f"feeder: fed {candidate.issue.repo}#{candidate.issue.key} as {ref.ticket_id}")
            if sharing:
                shares = "; ".join(
                    f"{path} with {labels(holders, candidate.ref.repo)}"
                    for path, holders in sharing.items()
                )
                self.log(f"feeder: {candidate.issue.repo}#{candidate.issue.key} shares {shares}")
        self._set_states(writes)
        return FeedReport(
            fed=tuple(fed),
            eligible=len(candidates),
            waiting=waiting,
            blocked=blocked,
            errors=errors,
            refused=len(refused),
        )

    def _candidates(self) -> tuple[list[_Candidate], set[str], int]:
        found: list[_Candidate] = []
        read: set[str] = set()
        errors = 0
        started = self._started()
        for project in self.projects:
            intake = project.intake
            assert intake is not None
            try:
                context = self.factory_for(project)
                if project.linear_project_ids:
                    if self.linear is None or intake.linear_state is None:
                        continue
                    issues = self.linear.linear_intake(
                        project.linear_project_ids, intake.linear_state, intake.linear_assignees
                    )
                else:
                    if context.config.tickets != "github" or intake.label is None:
                        continue
                    issues = self.github.github_intake(project.repo, intake.label)
            except Exception as exc:
                errors += 1
                self.log(f"feeder: cannot read intake for {project.repo}: {safe_ship_error(exc)}")
                continue
            read.add(project.repo.lower())
            trusted = intake_trusted(self.config, project)
            for issue in issues:
                if issue.repo.lower() != project.repo.lower() or not _KEY.fullmatch(issue.key):
                    continue
                if (issue.repo.lower(), issue.key) in started:
                    continue
                if not assignee_allowed(project, issue):
                    continue
                if issue.applied_by is None or issue.applied_by not in trusted:
                    continue
                found.append(
                    _Candidate(
                        project=project,
                        context=context,
                        issue=issue,
                        lines=gate_lines(issue),
                        template=parse_intake(issue.body, issue.labels),
                    )
                )
        return found, read, errors

    # -- intake template (#204) ------------------------------------------------------

    def _refuse(self, candidate: _Candidate) -> _StateWrite:
        """Wake Grok once per defect set; the refused-intake state is the
        caller's to write."""
        issue = candidate.issue
        defects = candidate.template.defects
        reason = "; ".join(defect.reason for defect in defects)
        repo = candidate.project.repo
        codes = sorted({defect.code for defect in defects})
        detail = (
            f"{_ticket_name(issue)} was not fed: its body fails the intake template "
            f"(#204): {reason}"
        )
        self.queue.enqueue(
            JobRequest(
                repo=repo,
                ticket_id=issue.key,
                action="refused",
                recipient="grok",
                source_comment_id=f"feeder-intake:{issue.key}",
                timeout_seconds=resolve_job_timeout(self.config, repo) * 60,
                detail=json.dumps(
                    {
                        "project_bot": candidate.project.grok_bot,
                        "detail": detail,
                        "ticket_url": issue.url,
                        "link": issue.url,
                        "defects": codes,
                    },
                    sort_keys=True,
                ),
            ),
            dedupe_key=f"feeder-intake:{repo.lower()}:{issue.key}:{','.join(codes)}",
        )
        self.log(f"feeder: {issue.repo}#{issue.key} refused at intake: {reason}")
        return _state_write(candidate, "refused-intake", reason=reason)

    def _sync_intake_attention(
        self, refused: Sequence[_Candidate], repos: set[str], now: float
    ) -> None:
        flags = {
            (c.issue.repo.lower(), c.issue.key, INTAKE_REFUSED): "; ".join(
                defect.reason for defect in c.template.defects
            )
            for c in refused
        }
        with self.queue.connect() as connection:
            attention.sync(connection, (INTAKE_REFUSED,), repos, flags, now)

    # -- gates (#156) ----------------------------------------------------------------

    def _gates(
        self,
        candidates: Sequence[_Candidate],
        states: Mapping[Ref, str],
        refused: Collection[Ref] = frozenset(),
    ) -> tuple[dict[Ref, Wait], Holds, set[Ref], bool, dict[Ref, int]]:
        """Blocker waits, the holds of tickets in flight, the holders that
        hold as capped, whether the status read (when one was needed)
        succeeded, and how many tickets each candidate unblocks. A ticket
        refused at intake is not gated."""
        rows = self._feeder_rows()
        unstarted = [
            c for c in candidates if states[c.ref] in UNSTARTED and c.ref not in refused
        ]
        linear_refs = {ref.key: ref for ref in rows if ref.tracker == "linear"}
        linear_refs.update({c.ref.key: c.ref for c in candidates if c.ref.tracker == "linear"})

        def linear_ref(repo: str, key: str) -> Ref:
            return linear_refs.get(key) or Ref("linear", repo.lower(), key)

        gated = {
            c.ref: replace(c.lines, blocked_by=frozenset(
                linear_ref(ref.repo, ref.key) if ref.tracker == "linear" else ref
                for ref in c.lines.blocked_by
            ))
            for c in unstarted
            if c.lines.blocked_by
        }
        conflicts = {ref for c in unstarted for ref in c.lines.conflicts_with}
        pull_of = {ref: row.pull for ref, row in rows.items() if row.pull is not None}
        known = set(rows) | set(pull_of.values()) | {c.ref for c in candidates}
        holders = {
            ref: row
            for ref, row in rows.items()
            if not row.released and (row.lines or ref in conflicts or row.pull in conflicts)
        }
        blockers = {ref for lines in gated.values() for ref in lines.blocked_by}
        via = {ref: pull_of[ref] for ref in blockers if ref in pull_of}
        linear = {
            linear_ref(c.issue.repo, key): linear_status(state_type)
            for c in unstarted
            for key, state_type in c.issue.blocked_by
        }
        external = {ref for ref in conflicts if ref not in known}
        needed = {ref for ref in blockers if ref.tracker == "github"}
        needed |= set(via.values()) | external
        needed |= {row.pull for row in holders.values() if row.pull is not None}
        statuses: dict[Ref, RefStatus] = {}
        ok = True
        if needed:
            try:
                statuses = dict(self.github.ref_statuses(needed))
            except Exception as exc:
                ok = False
                self.log(f"feeder: cannot read blocker status: {safe_ship_error(exc)}")
        statuses.update(linear)
        for blocker, pull in via.items():
            if _progress(statuses, pull) is Progress.DONE:
                statuses[blocker] = statuses[pull]
        holds = Holds(policies={
            c.ref.repo: PathPolicy(
                shared=c.context.config.shared_paths,
                exclusive=c.context.config.exclusive_paths,
                max_holders=c.context.config.max_holders_per_path,
            )
            for c in candidates
        })
        released: list[Ref] = []
        capped = self._capped({row.ticket for row in holders.values() if row.ticket})
        flips: list[tuple[Ref, bool]] = []
        capped_refs: set[Ref] = set()
        for ref, row in sorted(holders.items()):
            pull = row.pull
            if pull is not None and _progress(statuses, pull) in (Progress.DONE, Progress.UNFINISHED):
                released.append(ref)
                continue
            is_capped = row.ticket in capped
            if is_capped:
                capped_refs.add(ref)
            if is_capped != row.capped_release:
                flips.append((ref, is_capped))
            holds.take(ref, () if pull is None else (pull,), _holding(row.lines, is_capped))
        for ref in sorted(external):
            status = statuses.get(ref)
            if status is None:
                holds.take(ref, (), GateLines(), "status unreadable")
            elif status.is_pull and status.progress is Progress.OPEN:
                holds.take(ref, (), GateLines())
        self._release(released)
        self._flip_capped(flips)
        shared = {repo: policy.shared for repo, policy in holds.policies.items()}
        unblocks = critical_path_counts(candidates, gated, {c.ref for c in unstarted}, pull_of, shared)
        return blocker_waits(gated, statuses), holds, capped_refs, ok, unblocks

    def _feeder_rows(self) -> dict[Ref, _Holder]:
        """Tickets fed or feeding."""
        with self.queue.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT repo, issue, tracker, pr_number, ticket_id, touches, conflicts,
                       released, capped_release
                FROM {FEEDER_TABLE}
                WHERE state IN ({",".join("?" * len(HOLDING))})
                """,
                HOLDING,
            ).fetchall()
        found: dict[Ref, _Holder] = {}
        for row in rows:
            ref = Ref(row["tracker"], row["repo"], row["issue"])
            pull = github_ref(row["repo"], row["pr_number"]) if row["pr_number"] else None
            touches = frozenset(json.loads(row["touches"] or "[]"))
            conflicts = frozenset(Ref(*item) for item in json.loads(row["conflicts"] or "[]"))
            lines = GateLines(touches=touches, conflicts_with=conflicts)
            found[ref] = _Holder(
                lines=lines,
                pull=pull,
                released=bool(row["released"]),
                ticket=(row["repo"], row["ticket_id"]) if row["ticket_id"] else None,
                capped_release=bool(row["capped_release"]),
            )
        return found

    def _started(self) -> set[tuple[str, str]]:
        """The (repo, ticket_id) of tickets the dispatcher took past
        questions whose feed is not in flight; read-only."""
        with self.queue.connect() as connection:
            if not _has_table(connection, "ticket_state"):
                return set()
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ticket_state)")}
            if not {"repo", "ticket_id", "workflow_state"} <= columns:
                return set()
            return {
                (str(repo).lower(), str(ticket_id))
                for repo, ticket_id in connection.execute(
                    f"""
                    SELECT ticket.repo, ticket.ticket_id FROM ticket_state AS ticket
                    WHERE COALESCE(ticket.workflow_state, 'intake') NOT IN ('intake', 'refused', 'questions')
                      AND NOT EXISTS (
                        SELECT 1 FROM {FEEDER_TABLE} AS feed
                        WHERE feed.repo = lower(ticket.repo) AND feed.issue = ticket.ticket_id
                          AND feed.state IN ({",".join("?" * len(IN_FLIGHT))})
                      )
                    """,
                    IN_FLIGHT,
                )
            }

    def _capped(self, tickets: set[tuple[str, str]]) -> set[tuple[str, str]]:
        """The (repo, ticket_id) of tickets whose round is capped and not
        stuck; read-only, and empty on a database without that state."""
        if not tickets:
            return set()
        with self.queue.connect() as connection:
            if not _has_table(connection, "ticket_state"):
                return set()
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ticket_state)")}
            if not {"repo", "ticket_id", "workflow_state"} <= columns:
                return set()
            stuck = " AND stuck = 0" if "stuck" in columns else ""
            found = {
                (str(repo), str(ticket_id))
                for repo, ticket_id in connection.execute(
                    f"SELECT repo, ticket_id FROM ticket_state WHERE workflow_state = 'capped'{stuck}"
                )
            }
        return found & tickets

    def _flip_capped(self, flips: Sequence[tuple[Ref, bool]]) -> None:
        """Record and log a holder releasing its paths (capped) or taking them back."""
        if not flips:
            return
        with self.queue.connect() as connection:
            connection.executemany(
                f"UPDATE {FEEDER_TABLE} SET capped_release = ? WHERE repo = ? AND issue = ?",
                [(int(capped), ref.repo, ref.key) for ref, capped in flips],
            )
        for ref, capped in flips:
            name = f"{ref.repo}#{ref.key}"
            self.log(
                f"feeder: released locks of {name} (capped)" if capped
                else f"feeder: re-held locks of {name}"
            )

    def _release(self, refs: Sequence[Ref]) -> None:
        """A holder whose PR merged or closed stops holding, for good."""
        if not refs:
            return
        with self.queue.connect() as connection:
            connection.executemany(
                f"UPDATE {FEEDER_TABLE} SET released = 1 WHERE repo = ? AND issue = ?",
                [(ref.repo, ref.key) for ref in refs],
            )

    def _sync_attention(self, waits: Mapping[Ref, Wait], repos: set[str], now: float) -> None:
        flags = {
            (ref.repo, ref.key, wait.attention): wait.reason
            for ref, wait in waits.items()
            if wait.attention is not None
        }
        with self.queue.connect() as connection:
            attention.sync(connection, ATTENTION_REASONS, repos, flags, now)

    # -- capacity --------------------------------------------------------------

    def _room(self, harnesses: set[str], now: float) -> dict[str, int]:
        room: dict[str, int] = {}
        with self.queue.connect() as connection:
            used = slots_in_use(connection, self.machines)
            blocked = placement_blocked(connection, now, self.machines)
            for harness in harnesses:
                free = free_slots(self.machines, harness, used, blocked) if self.machines else 0
                queued = connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state = 'queued' AND recipient = ?",
                    (harness,),
                ).fetchone()[0]
                pending = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {FEEDER_TABLE} AS item
                    WHERE item.harness = ? AND item.state = 'fed' AND item.fed_at > ?
                      AND NOT EXISTS (
                        SELECT 1 FROM jobs
                        WHERE lower(jobs.repo) = item.repo
                          AND jobs.ticket_id = item.ticket_id
                          AND jobs.recipient != 'grok'
                      )
                    """,
                    (harness, now - FED_PENDING_SECONDS),
                ).fetchone()[0]
                room[harness] = free - int(queued) - int(pending)
        return room

    # -- bookkeeping -------------------------------------------------------------

    def _record_seen(self, candidates: Sequence[_Candidate], now: float) -> bool:
        unstarted = ", ".join(f"'{state}'" for state in UNSTARTED)

        def work(connection: sqlite3.Connection) -> None:
            for candidate in candidates:
                issue = candidate.issue
                connection.execute(
                    f"""
                    INSERT INTO {FEEDER_TABLE} (
                        repo, issue, tracker, title, harness, state, urgent, bug, tier,
                        risk, issue_created_at, first_seen_at, seen_at, touches, conflicts,
                        base_branch
                    ) VALUES (?, ?, ?, ?, ?, 'eligible', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo, issue) DO UPDATE SET
                        title = excluded.title,
                        touches = excluded.touches,
                        conflicts = excluded.conflicts,
                        urgent = excluded.urgent,
                        bug = excluded.bug,
                        tier = excluded.tier,
                        risk = CASE WHEN state IN ({unstarted}) THEN excluded.risk
                                    ELSE risk END,
                        base_branch = CASE WHEN {FEED_NOT_BEGUN} THEN excluded.base_branch
                                           ELSE base_branch END,
                        harness = CASE WHEN state IN ('fed', 'done', 'closed') THEN harness
                                       ELSE excluded.harness END,
                        seen_at = excluded.seen_at
                    """,
                    (
                        issue.repo.lower(),
                        issue.key,
                        issue.tracker,
                        issue.title[:200],
                        candidate.harness,
                        int(issue.urgent),
                        int(issue.bug),
                        candidate.tier,
                        candidate.template.risk or "unset",
                        issue.created_at,
                        now,
                        now,
                        json.dumps(sorted(candidate.lines.touches)),
                        json.dumps(
                            [[ref.tracker, ref.repo, ref.key] for ref in sorted(candidate.lines.conflicts_with)]
                        ),
                        candidate.base,
                    ),
                )
            connection.execute(
                f"INSERT INTO {POLL_TABLE} (id, at) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET at = excluded.at",
                (now,),
            )
            # A ticket that lost its signal before its feed began is forgotten;
            # a begun one is kept forever, so it is never opened twice.
            connection.execute(
                f"DELETE FROM {FEEDER_TABLE} WHERE seen_at < ? AND state IN ({unstarted}) "
                f"AND {FEED_NOT_BEGUN}",
                (now,),
            )

        return bookkeeping_write(
            self.queue.connect, work, site="_record_seen", ledger=self.ledger
        )

    def _record_priority(self, candidates: Sequence[_Candidate], now: float) -> None:
        """Each ticket's unblocks count, and the claim order of every ticket
        with a ticket id, so a label or blocker change after the feed still
        moves its queued job."""

        def work(connection: sqlite3.Connection) -> None:
            for candidate in candidates:
                issue = candidate.issue
                where = (issue.repo.lower(), issue.key)
                connection.execute(
                    f"UPDATE {FEEDER_TABLE} SET unblocks = ? WHERE repo = ? AND issue = ?",
                    (candidate.unblocks, *where),
                )
                ticket = connection.execute(
                    f"SELECT ticket_id FROM {FEEDER_TABLE} WHERE repo = ? AND issue = ?", where
                ).fetchone()[0]
                if ticket is not None:
                    set_ticket_priority(
                        connection, issue.repo, ticket, urgent=issue.urgent, bug=issue.bug,
                        tier=candidate.tier, unblocks=candidate.unblocks, at=now,
                    )

        bookkeeping_write(self.queue.connect, work, site="_record_priority", ledger=self.ledger)

    def _mark_done(self) -> None:
        with self.queue.connect() as connection:
            connection.execute(
                f"""
                UPDATE {FEEDER_TABLE} SET state = 'done'
                WHERE state = 'fed' AND EXISTS (
                    SELECT 1 FROM jobs
                    WHERE lower(jobs.repo) = {FEEDER_TABLE}.repo
                      AND jobs.ticket_id = {FEEDER_TABLE}.ticket_id
                      AND jobs.recipient != 'grok'
                )
                """
            )

    def _row(self, candidate: _Candidate) -> sqlite3.Row:
        with self.queue.connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                f"SELECT * FROM {FEEDER_TABLE} WHERE repo = ? AND issue = ?",
                (candidate.issue.repo.lower(), candidate.issue.key),
            ).fetchone()
        assert row is not None
        return row

    def _state(self, candidate: _Candidate) -> str:
        return str(self._row(candidate)["state"])

    def _set_state(
        self,
        candidate: _Candidate,
        state: str,
        *,
        error: str | None = None,
        reason: str | None = None,
    ) -> bool:
        return self._set_states(
            [_state_write(candidate, state, error=error, reason=reason)], site="_set_state"
        )

    def _set_states(self, writes: Sequence[_StateWrite], *, site: str = "_set_states") -> bool:
        if not writes:
            return True
        return bookkeeping_write(
            self.queue.connect,
            lambda connection: connection.executemany(
                f"""
                UPDATE {FEEDER_TABLE} SET state = ?, error = ?, waiting_reason = ?
                WHERE repo = ? AND issue = ? AND state NOT IN ('fed', 'done', 'closed')
                """,
                writes,
            ),
            site=site,
            ledger=self.ledger,
        )

    def _record(self, candidate: _Candidate, **values: object) -> None:
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self.queue.connect() as connection:
            connection.execute(
                f"UPDATE {FEEDER_TABLE} SET {assignments} WHERE repo = ? AND issue = ?",
                (*values.values(), candidate.issue.repo.lower(), candidate.issue.key),
            )

    # -- one ticket ----------------------------------------------------------------

    def _feed(self, candidate: _Candidate, now: float, holds: Holds) -> TicketRef | None:
        """Feed one ticket; None when its recorded PR is no longer open.

        Raises _Held, before any write, when the open PR it would reuse is
        one a ticket in flight conflicts with, or when the branch it would
        reuse carries another open PR's commits (#205).
        """
        issue = candidate.issue
        config = candidate.context.config
        row = self._row(candidate)
        branch = row["branch"] or self._branch_for(issue)
        if not _BRANCH.fullmatch(branch) or ".." in branch:
            raise ValueError(f"branch name {branch!r} is not safe")
        repo = candidate.project.repo
        base = row["base_branch"] or candidate.base
        if base is None:
            raise _Held(Wait(f"{_ticket_name(issue)} has no Linear project to pick its base branch"))
        base_sha = self.github.branch_sha(repo, base)
        if base_sha is None:
            raise _Held(Wait(f"base branch {base} does not exist on {repo}"))
        number: int | None = None
        if row["pr_number"]:
            number = int(row["pr_number"])
            pull = self.github.pull_state(repo, number)
            if pull != "open":
                self._set_state(candidate, "closed", error=f"PR #{number} is {pull}")
                return None
            branch_found = True
        else:
            tracked = row["ticket_id"] or (issue.key if issue.tracker == "linear" else None)
            number = self._tracked_open_pull(repo, tracked)
            if number is None:
                number = self.github.find_pull(repo, branch)
            if number is not None:
                wait = holds.collision(candidate.ref, candidate.lines, (github_ref(repo, number),))
                if wait is not None:
                    raise _Held(wait)
            branch_found = number is not None or self.github.branch_sha(repo, branch) is not None
        if number is not None:
            tip = self.github.branch_sha(repo, branch) or branch
            pull_base = self._pull_base(repo, issue.key, base, number, tip)
            if pull_base != base:
                base = pull_base
                self._record(candidate, base_branch=base)
        if branch_found:
            wait = self._stack_hold(
                repo, issue.key, base, branch, number,
                where=f"branch {branch}", project_bot=candidate.project.grok_bot,
                ticket_url=issue.url,
                held=f"{_ticket_name(issue)} was not fed", until="it feeds",
            )
            if wait is not None:
                raise _Held(wait)
        must_write(self._set_state(candidate, "feeding"))
        risk = str(row["risk"])
        if risk not in ("low", "high"):
            raise ValueError(f"ticket risk {risk!r} is not low or high")
        template = candidate.template
        state = go_state_for(issue.body, template.plan_first)
        self._record(candidate, branch=branch, base_branch=base)
        if number is None:
            if not branch_found:
                start = self.github.create_start_commit(
                    repo, base_sha, f"Start {_ticket_name(issue)}: {issue.title}"[:200]
                )
                self.github.create_branch(repo, branch, start)
            number = self.github.open_draft_pull(
                repo,
                branch=branch,
                base=base,
                title=_pull_title(issue),
                body=_pull_body(issue),
            )
        ticket_id = issue.key if issue.tracker == "linear" else str(number)
        self._record(candidate, pr_number=str(number), ticket_id=ticket_id)
        holds.alias(candidate.ref, github_ref(repo, number))
        with self.queue.connect() as connection:
            set_ticket_priority(
                connection, repo, ticket_id, urgent=issue.urgent, bug=issue.bug,
                tier=candidate.tier, unblocks=candidate.unblocks, at=now,
            )
        linear = issue.tracker == "linear"
        if not linear and config.tickets == "github":
            self.github.add_labels(repo, number, [FACTORY_LABEL, f"orch:state/{state}"])
        if linear:
            if self.linear is None or issue.linear_id is None:
                raise ValueError("Linear issue has no id to comment on")
            existing = list(self.linear.linear_comments(issue.linear_id))
            linear_id = issue.linear_id
            post: Callable[[str], str] = lambda body: self.linear.post_linear_comment(  # type: ignore[union-attr]
                linear_id, body
            )
        else:
            existing = list(self.github.comments(repo, number))
            post = lambda body: self.github.post_comment(repo, number, body)
        authors = self._orchestrator_authors(issue.tracker)
        brief_id = row["brief_comment_id"] or _existing(existing, "brief", authors)
        if brief_id is None:
            brief_id = post(
                _brief(issue, candidate.context, number=number, branch=branch, base=base,
                       risk=risk, state=state, visible=linear, template=template)
            )
        self._record(candidate, brief_comment_id=brief_id)
        go_id = row["go_comment_id"] or _existing(existing, "go", authors)
        if go_id is None:
            go_id = post(_go(issue, state=state, risk=risk, number=number, visible=linear))
        self._record(candidate, go_comment_id=go_id, state="fed", fed_at=now, error=None)
        return TicketRef(repo, ticket_id, issue.tracker)

    def _tracked_open_pull(self, repo: str, ticket_id: str | None) -> int | None:
        if ticket_id is None:
            return None
        with self.queue.connect() as connection:
            _, recorded = read_sources(connection).get((repo.casefold(), ticket_id), ("", None))
        if recorded is None or not recorded.isdigit():
            return None
        number = int(recorded)
        return number if self.github.pull_state(repo, number) == "open" else None

    def review_base_unless_stacked(self, ticket: TrackedTicket, head: str) -> str | None:
        number = int(ticket.pr_number) if ticket.pr_number else None
        base = ticket.factory.base_branch
        if number is not None:
            key = (ticket.repo.lower(), ticket.ticket_id)
            with self.queue.connect() as connection:
                row = connection.execute(
                    f"SELECT base_branch FROM {FEEDER_TABLE} WHERE repo = ? AND ticket_id = ?", key
                ).fetchone()
            recorded = row[0] if row is not None and row[0] else base
            base = self._pull_base(ticket.repo, ticket.ticket_id, recorded, number, head)
            if base != recorded:
                with self.queue.connect() as connection:
                    connection.execute(
                        f"UPDATE {FEEDER_TABLE} SET base_branch = ? WHERE repo = ? AND ticket_id = ?",
                        (base, *key),
                    )
        wait = self._stack_hold(
            ticket.repo, ticket.ticket_id, base, head, number,
            where=f"head {head[:7]}",
            project_bot=ticket.grok_bot, ticket_url=ticket.ticket_url,
            held=f"{ticket.source_issue or '#' + ticket.ticket_id} was not sent to review",
            until="review starts",
        )
        return None if wait is not None else base

    def _pull_base(self, repo: str, ticket_id: str, recorded: str, number: int, head: str) -> str:
        base = self.github.pull_base(repo, number)
        note = (repo.lower(), number, head, recorded, base)
        if base != recorded and note not in self._base_notes:
            self._base_notes.add(note)
            self.log(
                f"feeder: {repo}#{ticket_id} recorded base {recorded} differs from PR base {base};"
                " using the PR base"
            )
        return base

    def _stack_hold(
        self,
        repo: str,
        ticket_id: str,
        base: str,
        tip: str,
        own: int | None,
        *,
        where: str,
        project_bot: str,
        ticket_url: str | None,
        held: str,
        until: str,
    ) -> Wait | None:
        pulls = stacked_pulls(self.github, repo, base, tip, own)
        if not pulls:
            return None
        wait = stack_wait(repo, where, pulls)
        stack = ",".join(map(str, pulls))
        self.queue.enqueue(
            JobRequest(
                repo=repo,
                ticket_id=ticket_id,
                action="blocked",
                recipient="grok",
                source_comment_id=f"feeder-stack:{ticket_id}:{stack}",
                timeout_seconds=resolve_job_timeout(self.config, repo) * 60,
                detail=json.dumps(
                    {
                        "project_bot": project_bot,
                        "detail": f"{held}: {wait.reason}; {until} once that merges.",
                        "ticket_url": ticket_url,
                        "pulls": list(pulls),
                    },
                    sort_keys=True,
                ),
            ),
            dedupe_key=f"feeder-stack:{repo.lower()}:{ticket_id}:{stack}",
        )
        self.log(f"feeder: {repo}#{ticket_id} held: {wait.reason}")
        return wait

    def _orchestrator_authors(self, tracker: str) -> frozenset[str]:
        """Who may have posted the orchestrator's own brief or go (#87 F1.3).

        The logins (GitHub) or Linear user ids that orch.yml trusts as
        grok-bot: the same rule the dispatcher applies to a go (T1), so a
        reused go is one the dispatcher will act on.
        """
        table = (
            self.config.trusted_linear_users
            if tracker == "linear"
            else self.config.trusted_logins
        )
        return frozenset(
            _bare_author(key) for key, bots in table.items() if ORCHESTRATOR in bots
        )

    @staticmethod
    def _branch_for(issue: IntakeIssue) -> str:
        if issue.tracker == "linear":
            if issue.branch and _BRANCH.fullmatch(issue.branch):
                return issue.branch
            return f"factory/{issue.key.lower()}"
        return f"factory/issue-{issue.key}"


def critical_path_counts(
    candidates: Sequence[_Candidate],
    eligible_gates: Mapping[Ref, GateLines],
    unstarted: Collection[Ref],
    pull_of: Mapping[Ref, Ref],
    shared: Mapping[str, Collection[str]],
) -> dict[Ref, int]:
    owner = {c.ref: c.ref for c in candidates}
    owner.update({pull_of[c.ref]: c.ref for c in candidates if c.ref in pull_of})
    blocks: dict[Ref, set[Ref]] = {c.ref: set() for c in candidates}
    for dependent, lines in eligible_gates.items():
        for blocker in lines.blocked_by:
            if owner.get(blocker, dependent) != dependent:
                blocks[owner[blocker]].add(dependent)
    counts: dict[Ref, int] = {}
    for c in candidates:
        assert c.project.intake is not None
        if not c.project.intake.critical_path_order:
            counts[c.ref] = 0
            continue
        own = shared.get(c.ref.repo, ())
        found = blocks[c.ref] | {
            other.ref
            for other in candidates
            if other.ref in unstarted
            and other.ref.repo == c.ref.repo
            and _paths_meet(c.lines.touches, other.lines.touches, own)
        }
        pending = list(found)
        while pending:
            for dependent in blocks[pending.pop()] - found:
                found.add(dependent)
                pending.append(dependent)
        found.discard(c.ref)
        counts[c.ref] = len(found)
    return counts


def _paths_meet(first: Collection[str], second: Collection[str], shared: Collection[str]) -> bool:
    return any(
        paths_collide(path, other)
        for path in first
        if not path_matches(shared, path)
        for other in second
        if not path_matches(shared, other)
    )


def _progress(statuses: Mapping[Ref, RefStatus], ref: Ref) -> Progress | None:
    status = statuses.get(ref)
    return None if status is None else status.progress


def _ticket_name(issue: IntakeIssue) -> str:
    return issue.key if issue.tracker == "linear" else f"#{issue.key}"


def _pull_title(issue: IntakeIssue) -> str:
    return prefixed(_ticket_name(issue), " ".join(issue.title.split())[:180])


def _pull_body(issue: IntakeIssue) -> str:
    lines = [f"Fixes {_ticket_name(issue)}", ""]
    if issue.url:
        lines.append(f"Ticket: {issue.url}")
    lines.append(
        "Opened by the factory feeder from the ticket's intake signal. The builder "
        "pushes the work here; the brief and go handoffs are on the ticket."
    )
    return "\n".join(lines) + "\n"


def _bare_author(author: str) -> str:
    """A comment author or trust key without the poller's `linear:` prefix."""
    return author.removeprefix("linear:")


def _existing(comments: Sequence[Comment], kind: str, authors: frozenset[str]) -> str | None:
    """The id of a brief or go the orchestrator already posted, if any.

    Only a comment by a trusted orchestrator author counts: a forged handoff
    from anyone else is ignored, so it can never stand in for the real one.
    """
    for item in comments:
        if _bare_author(item.author) not in authors:
            continue
        try:
            fields = parse_comment(item.body).as_dict()
        except HandoffError:
            continue
        if (
            fields.get("kind") == kind
            and fields.get("from") == ORCHESTRATOR
            and fields.get("role") == "orchestrator"
        ):
            return item.id
    return None


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _brief(
    issue: IntakeIssue,
    context: FactoryContext,
    *,
    number: int,
    branch: str,
    base: str,
    risk: str,
    state: str,
    visible: bool,
    template: IntakeTemplate = IntakeTemplate(),
) -> str:
    config = context.config
    body = issue.body.strip() or "(the ticket has no body)"
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + f"\n[cut at {BODY_LIMIT} characters]"
    fence = _fence(body)
    if state == "planning" and template.plan_first:
        why = "the ticket says Plan first: yes"
    elif state == "planning":
        why = "the ticket names more than 3 files, so it starts with a plan"
    else:
        why = "the ticket names 3 files or fewer, so it starts building"
    source = template.risk_source if template.risk == risk else None
    sections: list[str] = []
    if template.criteria:
        sections += ["", "### Acceptance criteria", *(f"- {item}" for item in template.criteria)]
    if template.ops_criteria:
        sections += [
            "",
            "### Ops step (operator action, not the builder's)",
            *(f"- {item}" for item in template.ops_criteria),
        ]
    prose = re.sub(r"^(#{1,5}) ", r"#\1 ", context.body.strip(), flags=re.MULTILINE)
    text = "\n".join(
        [
            f"**Brief for {_ticket_name(issue)}: {' '.join(issue.title.split())[:180]}** (factory feeder)",
            "",
            f"- Goal: {' '.join(issue.title.split())[:180]}",
            f"- Ticket: {issue.url or _ticket_name(issue)}",
            f"- PR: #{number} on branch `{branch}` off `{base}`",
            f"- Risk: {risk} (from the ticket's {source or 'intake record'})",
            f"- Start: {state} ({why})",
            f"- Builder: {config.builder_harness}; reviewer: {config.reviewer_harness}",
            "- Questions: the chat that filed this ticket was its questions step. "
            "Post a questions handoff if a product call is still open.",
            *sections,
            "",
            "### Ticket",
            fence,
            body,
            fence,
            "",
            f"{FACTORY_MD_HEADING}`{config.base_branch}`)",
            prose or "(no prose sections)",
        ]
    )
    return format_comment(
        Handoff(
            fields=(("kind", "brief"), ("from", ORCHESTRATOR), ("role", "orchestrator"), ("risk", risk)),
            body=text,
        ),
        visible=visible,
    )


def _go(issue: IntakeIssue, *, state: str, risk: str, number: int, visible: bool) -> str:
    return format_comment(
        Handoff(
            fields=(
                ("kind", "go"),
                ("from", ORCHESTRATOR),
                ("role", "orchestrator"),
                ("risk", risk),
                ("state", state),
            ),
            body=f"Go: {state} {_ticket_name(issue)} on PR #{number}. Opened by the factory feeder.",
        ),
        visible=visible,
    )
