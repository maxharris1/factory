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
is not refused; it goes into the brief's separate ops step. Optional
`Assumed environment` and `Out of scope` sections (#1033) go into the brief
beside the criteria; a builder criterion with an unbounded quantifier
("every", "all", "never") on a ticket with no `Out of scope` section adds an
intake warning to the brief and is never refused. An optional `Target branch:`
line names the draft PR's base (SPEC section 4.4); a second line, a bad
value or the ticket's own head branch is a defect. A ticket that
fails is `refused-intake` with its reasons, takes no room and no holds,
posts nothing, raises needs-attention (`intake-refused`) and queues one
Grok `refused` wake per distinct defect set. It is re-checked every poll,
so a fixed ticket feeds. A ticket already feeding or fed is not re-checked.
An optional `Features:` line names QA journeys (lib/journeys.py, #1029); each
poll reads the qa.verify skill's feature map at the base once, refuses ids the
map contradicts or a project with no qa.verify, and leaves a ticket eligible while that read fails.

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
lists h (machines.yml, minus held slots), minus the jobs queued for any
harness listed on a device that takes new work for h, minus the tickets this
feeder fed for those harnesses in the last FED_PENDING_SECONDS whose job is
not queued yet. Every ticket fed this poll takes one unit of room, a resumed
one (some steps already done) as much as a new one; resumed tickets only go
first. So the queue never holds more work for a harness than it has free
slots.

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
So does an open blocker in a tracked repo that is neither a candidate nor
fed (`critical-path-blocker`, `gates N candidates, best tier T`), until it
closes; a failed status read keeps the flag.
A holder whose ticket is `capped` in `ticket_state` (read-only) holds only
its `Conflicts with` edges while it waits for a go, and takes its paths back
on the poll after the round resumes (#270).

Idle and blocked (#1262): a GitHub-ticket project with ready tickets waiting
on open blockers, and no ticket fed or waiting on capacity this poll, no
queued or running non-Grok job, no ticket fed in the last
FED_PENDING_SECONDS without a job and no open factory PR, raises
needs-attention (`idle-blocked-ready`, `waiting on #18`) on each such ticket.
An open PR is factory work when the feeder recorded it, its branch starts
with `factory/`, or `ticket_state` tracks it in a non-terminal state.
A `feeder_idle_stalls` row names each project's stall by when it began, and
the project's one Grok `blocked` wake is keyed on it. The row also records the
latest non-Grok job activity (created or finished) the stall has seen, read in
the same statement as the in-flight check. The stall ends when the project
stops stalling, and a new one begins (flags re-raised, a new wake) when a
non-Grok job was created or finished after that mark, so a job that ran
between polls counts and one that finished before the first alert does not. A failed open-PR read keeps that project's
row and flags as they were and wakes nothing.

Stop the line (#1025): for a project whose factory.md sets
`feeder.required_checks`, each poll reads its base branch's checks once
(GitHubIntake.base_checks). While a required check is red there, or the read
fails, a ticket whose feed has not begun (no branch recorded yet, whatever
its state, a failed first attempt included) is `waiting-blocked` with the red
checks and head, unless it is labelled `factory:main-fix`. A begun feed, and
its jobs, go on.

Main-fix filing (#1061, lib/main_fix.py): the first red read of a streak files
one issue labelled with the intake label and `factory:main-fix`, carrying the
red checks and their log tails, and the same poll feeds it as `building`. A
`feeder_main_fix` row per streak and a marker in the issue body keep it to one
issue across crashes and feeders; a green read ends the streak, and an issue
closed while main stays red starts a new one. The row stores the title and body
rendered from the streak's first red read, and a feeder posts or retires it
only while it holds the row's claim, which lapses after CLAIM_SECONDS. Each
later red head of the streak adds one comment with its log to the open issue,
once per head by a marker, under the row's claim. A head whose log read fails
posts nothing until a later poll reads it.

Stacked branches (#205, lib/branch_stack.py): a ticket whose branch already
exists and carries commits of another open PR is `waiting-blocked` until
that PR merges, with one Grok `blocked` wake per set of such PRs. The branch
is read at intake when it already exists, and again before a builder's
ready queues review, so a branch stacked during its build waits there too.
The branch is compared against its open PR's base, or against the recorded
base when it has no PR.

Priority order (#258, #1050, lib/queue.py rank): a ticket's tier first, then
urgent, then bug, then how many of this poll's candidates it unblocks
(critical_path_counts; 0 with `critical_path_order: false`), then the oldest.
A ticket's own tier is the first of the project's `priority_labels` it
carries, or its Linear priority when the project sets `priority_from_linear`,
or 0 when it has neither and is urgent. It ranks on its effective tier: the
best own tier of itself and every candidate, fed or not, it transitively
unblocks through Blocked by / Depends on (effective_tiers). No tier sorts
after every tier.

Project share (#1050): while a project with `intake.capacity_share` holds
fewer tickets in flight than its share of its builder's schedulable slots,
its best eligible ticket is fed before any other project's, after the
tickets whose feed is already under way. In flight is a ticket with a queued
or running non-Grok job, one fed in the last FED_PENDING_SECONDS with no job
yet, or one fed earlier in this poll. With no share set, the order is the
priority order above.

Each poll also refreshes the claim order (`ticket_priority`) of every ticket
the feeder has a ticket id for, so a label or blocker change after the feed
still moves its queued job.

The feeder starts no harness and runs no job. It writes only through the
GitHub and Linear clients it is given; tests pass fakes.
"""

from __future__ import annotations

import heapq
import json
import re
import sqlite3
import sys
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Literal, NamedTuple, Protocol

from markdown_it import MarkdownIt
from markdown_it.token import Token

from lib import attention, live_settings
from lib.branch_stack import OpenPull, stack_wait, stacked_pulls
from lib.brief_comment import FACTORY_MD_HEADING
from lib.device_pool import free_slots, placement_blocked, slots_in_use
from lib.device_sync import Blocked
from lib.dispatch.bookkeeping import LockLedger, bookkeeping_write, must_write
from lib.dispatch.constants import TERMINAL_STATES
from lib.dispatch.types import BaseCheck, FailedCheck
from lib.dispatcher import Comment, TicketRef, TrackedTicket, safe_ship_error
from lib.evidence.ci import CIEvidence, MainRed, ci_evidence, main_red
from lib.evidence.criteria import numbered
from lib.factory_md import FactoryContext
from lib.feeder_gates import (
    ATTENTION_CRITICAL_PATH,
    ATTENTION_IDLE_BLOCKED,
    FILE_PATH,
    MAIN_FIX_LABEL,
    WAIT_REASONS,
    GateLines,
    Holds,
    PathPolicy,
    Progress,
    Ref,
    RefStatus,
    Wait,
    blocker_waits,
    github_ref,
    is_main_fix,
    labels,
    line_stop,
    linear_status,
    parse_gate_lines,
    path_matches,
    paths_collide,
)
from lib.followups import has_marker, split_parent
from lib.github_actions import TIMEOUT_SECONDS
from lib.handoff import Handoff, HandoffError, format_comment, parse_comment
from lib.journeys import (
    Journey,
    NO_VERIFY,
    JourneyCode,
    QaCriterion,
    QaPlan,
    map_ids,
    map_problems,
    parse_features,
    problems,
    qa_plan,
    qa_tag,
)
from lib.main_fix import (
    MAIN_FIX_TABLE,
    Close,
    Comment as MainFixComment,
    File,
    MainFixRow,
    MainRead,
    Retire,
    close_comment,
    close_marker,
    decide,
    head_comment,
    head_marker,
    issue_body,
    issue_title,
    marked_issue_fate,
    marker,
)
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
from lib.split_parent import close_split_parent, split_evidence
from lib.ticket_type import TICKET_TYPES

FEEDER_TABLE = "feeder_items"
POLL_TABLE = "feeder_poll"  # one row: when the feeder last read intake
IDLE_TABLE = "feeder_idle_stalls"  # one row per project in an idle-blocked-ready stall
SPLIT_CURSOR_TABLE = "feeder_split_cursor"
SPLIT_UNSETTLED_TABLE = "feeder_split_unsettled"
SPLIT_CURSOR_OVERLAP_SECONDS = 120  # clock skew between orch and GitHub
SPLIT_RECHECK_SECONDS = 120  # GitHub search allows 30 reads a minute
# eligible: seen this poll, not fed yet; waiting-capacity: no room for its
# harness this poll; feeding: some steps done; error: a step failed (retried
# next poll); fed: brief and go posted; done: fed and its job is queued;
# closed: its recorded PR was closed or merged before the feed finished;
# abandoned: its ticket was abandoned or its PR labelled orch:state/abandoned;
# it holds nothing and is never fed again;
# waiting-blocked: a blocker is not done or an in-flight ticket holds one of
# its paths or a conflicts-with edge (waiting_reason says which);
# refused-intake: its body fails the intake template (waiting_reason says how).
STATES = (
    "eligible",
    "waiting-capacity",
    "waiting-blocked",
    "refused-intake",
    "split-parent",
    "feeding",
    "error",
    "fed",
    "done",
    "closed",
    "abandoned",
)
FEED_NOT_BEGUN = "branch IS NULL"
UNSTARTED = ("eligible", "waiting-capacity", "waiting-blocked", "refused-intake", "split-parent")
IN_FLIGHT = ("feeding", "error")
FED = ("fed", "done")
FINAL = (*FED, "closed", "abandoned")
_FINAL_SQL = ",".join(f"'{state}'" for state in FINAL)
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
    ("own_tier", "INTEGER"),
    ("split_noted", "INTEGER NOT NULL DEFAULT 0"),
    ("target_source", "TEXT"),
    ("stack_parent_pr", "INTEGER"),
    ("stack_parent_branch", "TEXT"),
    ("stack_parent_head_at_feed", "TEXT"),
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
_FED_WITHOUT_JOB = f"""
    SELECT COUNT(*) FROM {FEEDER_TABLE} AS item
    WHERE item.state = 'fed' AND item.fed_at > ?
      AND NOT EXISTS (
        SELECT 1 FROM jobs
        WHERE lower(jobs.repo) = item.repo
          AND jobs.ticket_id = item.ticket_id
          AND jobs.recipient != 'grok'
      )
"""
BODY_LIMIT = 20_000
SKILL_WALK_READS = 64
CLAIM_SECONDS = 15 * TIMEOUT_SECONDS

_FEED_BEGUN = (
    f"FROM {FEEDER_TABLE} WHERE repo = ? AND issue = ? "
    f"AND (branch IS NOT NULL OR state IN ({','.join('?' * len(HOLDING))}))"
)
_CLOSED_ON_GREEN = (
    f"SELECT 1 FROM {MAIN_FIX_TABLE} WHERE repo = ? AND CAST(issue AS TEXT) = ? "
    "AND state IN ('closing', 'closed-unfed')"
)
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")

Log = Callable[[str], None]

# -- intake template (#204) ----------------------------------------------------

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SECTION_HEADING = r"^(?:\*\*|__)?{name}(?:\*\*|__)?:?$"
_SECTIONS = {
    key: (re.compile(_SECTION_HEADING.format(name=name), re.IGNORECASE), paragraphs)
    for key, name, paragraphs in (
        ("criteria", r"acceptance\s+criteria", False),
        ("assumed_environment", r"assumed\s+environment", True),
        ("out_of_scope", r"out\s+of\s+scope", True),
    )
}
_UNBOUNDED = re.compile(
    r"\b(?:every|everything|everywhere|all|any|anything|each|always|never|whatever|whenever)\b",
    re.IGNORECASE,
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.*)$")
_FIELD = r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?\s*{name}\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(?P<value>.*?)\s*$"
_RISK_LINE = re.compile(_FIELD.format(name=r"risk"), re.IGNORECASE)
_TARGET_LINE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?P<open>\*\*|__)?\s*target\s+branch\s*(?P<close>\*\*|__)?\s*:\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)
_TARGET_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,199}")
_TARGET_REFUSED = re.compile(r"\.\.|//|/\.|\.lock(?:/|$)|[./]$")
_EMPHASIZED = re.compile(r"^(\*\*|__|\*|_)(?P<inner>.+)\1$")
_PLAN_LINE = re.compile(_FIELD.format(name=r"plan\s+first(?:\s*\(yes\s*/\s*no\))?"), re.IGNORECASE)
_DEPENDS_LINE = re.compile(_FIELD.format(name=r"depends\s+on"), re.IGNORECASE)
_TYPE_LINE = re.compile(_FIELD.format(name=r"type"), re.IGNORECASE)
_FEATURES_LINE = re.compile(_FIELD.format(name=r"features"), re.IGNORECASE)
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
    code: (
        Literal["no-criteria", "other-prs-criterion", "no-risk", "no-plan-first", "no-depends-on", "bad-type",
                "bad-target-branch"]
        | JourneyCode
    )
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
    ticket_type: str | None = None
    journeys: tuple[Journey, ...] = ()
    qa_criteria: tuple[QaCriterion, ...] = ()
    defects: tuple[Defect, ...] = ()
    assumed_environment: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    target_branch: str | None = None


def _unfenced(body: str) -> list[str]:
    code = {
        index
        for token in _MARKDOWN.parse(body or "")
        if token.type in ("code_block", "fence") and token.map
        for index in range(*token.map)
    }
    return [line for index, line in enumerate((body or "").splitlines()) if index not in code]


def _field(lines: Sequence[str], pattern: re.Pattern[str]) -> str | None:
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group("value").strip("*_ ").strip()
    return None


def _is_target_branch(value: str) -> bool:
    return _TARGET_BRANCH.fullmatch(value) is not None and not _TARGET_REFUSED.search(value)


def _target_value(match: re.Match[str]) -> str:
    value, mark = match["value"], match["open"]
    if mark and not match["close"]:
        if value.startswith(mark):
            value = value[len(mark):].strip()
        elif value.endswith(mark):
            value = value[: -len(mark)].strip()
    if _is_target_branch(value):
        return value
    wrapped = _EMPHASIZED.match(value)
    if wrapped and _is_target_branch(wrapped["inner"]):
        return wrapped["inner"]
    return value


def _target_branch(lines: Sequence[str], head: str | None) -> tuple[str | None, Defect | None]:
    values = [_target_value(match) for match in map(_TARGET_LINE.match, lines) if match]
    if not values:
        return None, None
    if len(values) > 1:
        return None, Defect("bad-target-branch", f"{len(values)} `Target branch:` lines: keep one")
    (value,) = values
    if value.lower() == "default":
        return None, None
    if not _is_target_branch(value):
        return None, Defect("bad-target-branch", f"`Target branch: {value}` is not a plain branch name")
    if value == head:
        return None, Defect("bad-target-branch", f"`Target branch: {value}` is the ticket's own head branch")
    return value, None


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


def _section_items(lines: Sequence[str]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {key: [] for key in _SECTIONS}
    section: str | None = None
    level = 0
    open_item = blank = False
    for line in lines:
        heading = _HEADING.match(line)
        if heading:
            open_item = blank = False
            depth = len(heading.group(1))
            if section is not None and depth <= level:
                section = None
            named = next((key for key, (pattern, _) in _SECTIONS.items() if pattern.match(heading.group(2))), None)
            if named is not None and named != section and 2 <= depth <= 4:
                section, level = named, depth
            continue
        if section is None:
            continue
        if not line.strip():
            blank = True
            continue
        items = sections[section]
        item = _LIST_ITEM.match(line)
        if item:
            current = item.group(1).strip()
            if current:
                items.append(current)
            open_item, blank = bool(current), False
            continue
        if open_item and (not blank or line[:1].isspace()):
            items[-1] = f"{items[-1]} {line.strip()}"
        elif _SECTIONS[section][1]:
            items.append(line.strip())
            open_item = True
        else:
            open_item = False
        blank = False
    return sections


def parse_intake(body: str, labels: Collection[str], *, head: str | None = None) -> IntakeTemplate:
    """The ticket's intake template fields and what is wrong with them."""
    lines = _unfenced(body)
    defects: list[Defect] = []
    criteria: list[str] = []
    ops: list[str] = []
    qa: list[QaCriterion] = []
    sections = _section_items(lines)
    for item in sections["criteria"]:
        tagged = _OPS_TAG.match(item)
        tag = None if tagged else qa_tag(item)
        if tagged:
            ops.append(item[tagged.end():].strip())
        elif tag is None and _OPS_ACTION.search(item):
            ops.append(item)
        else:
            criteria.append(item)
            if tag is not None:
                qa.append(tag)
    if not criteria:
        defects.append(Defect(
            "no-criteria",
            "no builder acceptance criteria: add an `## Acceptance criteria` section "
            "with at least one item the builder can meet",
        ))
    document: dict[str, object] = {}
    _MARKDOWN.parse(body or "", document)
    warnings: list[str] = []
    for item in criteria:
        texts = _prose_and_link_targets(item, document)
        if any(pattern.search(text) for text in texts for pattern in _OTHER_PRS):
            defects.append(Defect(
                "other-prs-criterion",
                f"criterion about other PRs' state ({item!r}): move PR order into "
                "`Blocked by` / `Conflicts with` lines",
            ))
        unbounded = _UNBOUNDED.search(texts[0])
        if unbounded and not sections["out_of_scope"]:
            warnings.append(
                f"criterion {item!r} says {unbounded.group(0)!r} with no `Out of scope` section: "
                "add one to bound it, or narrow the criterion"
            )
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
    ticket_type = _field(lines, _TYPE_LINE)
    if ticket_type is not None and ticket_type not in TICKET_TYPES:
        valid = ", ".join(f"`{name}`" for name in TICKET_TYPES)
        defects.append(Defect("bad-type", f"`Type: {ticket_type}` is not one of {valid} (no line means feature)"))
        ticket_type = None
    target_branch, target_defect = _target_branch(lines, head)
    if target_defect is not None:
        defects.append(target_defect)
    journeys, bad = parse_features(_field(lines, _FEATURES_LINE))
    qa_criteria = tuple(qa)
    defects.extend(Defect(code, reason) for code, reason in problems(journeys, bad, qa_criteria))
    return IntakeTemplate(
        criteria=tuple(criteria),
        ops_criteria=tuple(ops),
        risk=risk,
        risk_source=source,
        plan_first=plan_first,
        depends_on=depends,
        ticket_type=ticket_type,
        journeys=journeys,
        qa_criteria=qa_criteria,
        defects=tuple(defects),
        assumed_environment=tuple(sections["assumed_environment"]),
        out_of_scope=tuple(sections["out_of_scope"]),
        warnings=tuple(warnings),
        target_branch=target_branch,
    )


@dataclass(frozen=True)
class IssueRecord:
    number: int
    state: str
    reason: str | None
    body: str


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

    def branch_pulls(self, repo: str, branch: str) -> Sequence[int]: ...

    def ahead_commits(self, repo: str, base: str, tip: str) -> Sequence[str]: ...

    def open_pull_commits(self, repo: str) -> Mapping[int, OpenPull]: ...

    def pull_commits(self, repo: str, base: str, head: str) -> Collection[str]: ...

    def pull_state(self, repo: str, number: int) -> str: ...

    def pull_base(self, repo: str, number: int) -> str: ...

    def open_draft_pull(
        self, repo: str, *, branch: str, base: str, title: str, body: str
    ) -> int: ...

    def pull_body(self, repo: str, number: int) -> str: ...

    def set_pull_body(self, repo: str, number: int, body: str) -> None: ...

    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> None: ...

    def comments(self, repo: str, number: int) -> Sequence[Comment]: ...

    def post_comment(self, repo: str, number: int, body: str) -> str: ...

    def ref_statuses(self, refs: Collection[Ref]) -> Mapping[Ref, RefStatus]: ...

    def base_checks(self, repo: str, branch: str) -> Mapping[str, BaseCheck]: ...

    def tree_paths(self, repo: str, sha: str, path: str = "") -> tuple[Sequence[str], bool]: ...

    def dir_names(self, repo: str, path: str, sha: str) -> Sequence[str]: ...

    def file_repo_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: tuple[str, ...],
        marker: str,
        since: float,
        *,
        before_post: Callable[[], None],
    ) -> int: ...

    def marked_issues(self, repo: str, marker: str, since: float) -> tuple[int, ...]: ...

    def close_issue(
        self, repo: str, number: int, comment: str | None, *, completed: bool = False
    ) -> None: ...

    def remove_issue_label(self, repo: str, number: int, label: str) -> None: ...

    def closed_issues_since(self, repo: str, since: float) -> Sequence[IssueRecord]: ...

    def split_children(self, repo: str, parent: int) -> Sequence[IssueRecord]: ...

    def issue_record(self, repo: str, number: int) -> IssueRecord: ...

    def post_issue_comment_once(self, repo: str, number: int, body: str, marker: str) -> str: ...

    def issue_state(self, repo: str, number: int) -> str: ...

    def failed_run_logs(self, repo: str, run_id: str) -> Mapping[str, str]: ...


class _ClaimLost(Exception):
    pass


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
class StackParent:
    pr: int
    branch: str
    head: str


@dataclass(frozen=True)
class FeedReport:
    fed: tuple[TicketRef, ...] = ()
    eligible: int = 0
    waiting: int = 0
    blocked: int = 0
    errors: int = 0
    refused: int = 0


@dataclass(frozen=True)
class _MainLine:
    project: Project
    label: str
    required: tuple[str, ...]
    read: MainRead


@dataclass
class _Candidate:
    project: Project
    context: FactoryContext
    issue: IntakeIssue

    lines: GateLines = GateLines()
    template: IntakeTemplate = IntakeTemplate()
    unblocks: int = 0
    effective_tier: int | None = None

    @property
    def harness(self) -> str:
        return self.context.config.builder_harness

    @property
    def base(self) -> str | None:
        if self.template.target_branch is not None:
            return self.template.target_branch
        intake = self.project.intake
        if intake is not None and intake.base_branches and self.issue.linear_project is None:
            return None
        return self.project.base_for(self.issue.linear_project) or self.context.config.base_branch

    @property
    def target_source(self) -> str:
        return "project" if self.template.target_branch is None else "ticket"

    @property
    def line(self) -> tuple[str, str | None]:
        return (self.project.repo.lower(), self.base)

    @property
    def own_tier(self) -> int | None:
        intake = self.project.intake
        assert intake is not None
        labels = {label.lower() for label in self.issue.labels}
        for index, label in enumerate(intake.priority_labels):
            if label.lower() in labels:
                return index
        if intake.priority_from_linear and self.issue.priority in LINEAR_PRIORITIES:
            return LINEAR_PRIORITIES.index(self.issue.priority)
        return 0 if self.issue.urgent else None

    @property
    def ref(self) -> Ref:
        return Ref(self.issue.tracker, self.issue.repo.lower(), self.issue.key)  # type: ignore[arg-type]

    def order(self) -> tuple[bool, int, int, int, float, tuple[int, int, str]]:
        return (
            *rank(tier=self.effective_tier, urgent=self.issue.urgent, resume=False, bug=self.issue.bug,
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
    (`released`), its dispatcher ticket, whether it last held as capped and
    whether its PR's status is still read (`watch`: unreleased, or fed or
    done, so a later abandoned label retires it)."""

    lines: GateLines
    pull: Ref | None
    released: bool
    ticket: tuple[str, str] | None
    capped_release: bool
    watch: bool


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


class _SplitParent(Exception):
    def __init__(self, evidence: str):
        super().__init__(evidence)
        self.evidence = evidence


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
            own_tier INTEGER,
            PRIMARY KEY (repo, issue)
        )
        """
    )
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({FEEDER_TABLE})")}
    for name, kind in _COLUMNS:
        if name not in present:
            connection.execute(f"ALTER TABLE {FEEDER_TABLE} ADD COLUMN {name} {kind}")
    if "split_noted" not in present and _has_merge_states(connection):
        connection.execute(
            f"""
            UPDATE {FEEDER_TABLE} SET split_noted = 1
            WHERE tracker = 'github' AND EXISTS (
                SELECT 1 FROM ticket_state AS ticket
                WHERE lower(ticket.repo) = {FEEDER_TABLE}.repo
                  AND ticket.ticket_id = {FEEDER_TABLE}.ticket_id
                  AND ticket.workflow_state = 'merged'
            )
            """
        )
    attention.ensure_table(connection)
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {POLL_TABLE} "
        "(id INTEGER PRIMARY KEY CHECK (id = 1), at REAL NOT NULL)"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {SPLIT_CURSOR_TABLE} (repo TEXT PRIMARY KEY, at REAL NOT NULL)"
    )
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {SPLIT_UNSETTLED_TABLE} "
        "(repo TEXT NOT NULL, parent INTEGER NOT NULL, checked_at REAL NOT NULL, "
        "PRIMARY KEY (repo, parent))"
    )
    ensure_priority_table(connection)
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {IDLE_TABLE} (repo TEXT PRIMARY KEY, since REAL NOT NULL, seen REAL NOT NULL)"
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MAIN_FIX_TABLE} (
            marker TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            base TEXT NOT NULL,
            state TEXT NOT NULL,
            issue INTEGER,
            last_head TEXT NOT NULL,
            filed_at REAL NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            claimed_by TEXT,
            claimed_at REAL
        )
        """
    )
    connection.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {MAIN_FIX_TABLE}_active ON {MAIN_FIX_TABLE} "
        "(repo, base) WHERE state IN ('filing', 'open')"
    )


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


def _has_merge_states(connection: sqlite3.Connection) -> bool:
    if not _has_table(connection, "ticket_state"):
        return False
    columns = {row[1] for row in connection.execute("PRAGMA table_info(ticket_state)")}
    return {"repo", "ticket_id", "workflow_state"} <= columns


def _merged_issues(
    connection: sqlite3.Connection, repo: str, *, unnoted: bool = False
) -> frozenset[int]:
    if not _has_merge_states(connection):
        return frozenset()
    rows = connection.execute(
        f"""
        SELECT feed.issue FROM {FEEDER_TABLE} AS feed
        JOIN ticket_state AS ticket
          ON lower(ticket.repo) = feed.repo AND ticket.ticket_id = feed.ticket_id
        WHERE feed.repo = ? AND feed.tracker = 'github'
          AND ticket.workflow_state = 'merged'
        """ + (" AND feed.split_noted = 0" if unnoted else ""),
        (repo.lower(),),
    ).fetchall()
    return frozenset(int(issue) for (issue,) in rows if str(issue).isdigit())


def feeder_items(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Tickets eligible at the last poll, priority order, as plain rows."""
    last = last_feeder_poll(connection)
    if last is None or not _has_table(connection, FEEDER_TABLE):
        return []
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({FEEDER_TABLE})")}
    defaults = {"waiting_reason": "NULL", "tier": "NULL", "own_tier": "NULL", "unblocks": "0", "fed_at": "NULL"}
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
        head: Callable[[], str | None],
        log: Log | None = None,
    ):
        self.queue = queue
        self.config = config
        self.machines = machines
        self.head = head
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
        if not self._retire_abandoned():
            self.log("feeder: abandoned rows not retired, orch.db is locked; retrying next poll")
        contexts, context_errors = self._contexts()
        context_errors += self._close_split_parents(contexts, now)
        mains, main_errors = self._main_reads(contexts)
        main_errors += self._file_main_fixes(mains, now)
        candidates, read_repos, read_errors = self._candidates(contexts)
        read_errors += context_errors + main_errors
        if not self._record_seen(candidates, now):
            self.log("feeder: intake not recorded, orch.db is locked; feeding next poll")
            return FeedReport(eligible=len(candidates), errors=read_errors + 1)
        self._mark_done()
        states = {candidate.ref: self._state(candidate) for candidate in candidates}
        begun = {c.ref for c in candidates if self._row(c)["branch"] is not None}
        split = {c.ref for c in candidates if states[c.ref] == "split-parent"}
        unread, map_errors = self._add_map_defects(candidates, states)
        refused = {
            candidate.ref
            for candidate in candidates
            if states[candidate.ref] in UNSTARTED
            and candidate.template.defects
            and candidate.ref not in split
        }
        skip = refused | split
        writes = [self._refuse(candidate) for candidate in candidates if candidate.ref in refused]
        writes += [_state_write(c, "eligible") for c in candidates if c.ref in unread - skip]
        self._sync_intake_attention(
            [c for c in candidates if c.ref in refused], read_repos, now
        )
        waits, critical, holds, capped, gate_read, unblocks, tiers = self._gates(
            candidates, states, skip
        )
        stops, stop_errors = self._line_stops(candidates, begun, skip, mains)
        for candidate in candidates:
            candidate.unblocks = unblocks[candidate.ref]
            candidate.effective_tier = tiers[candidate.ref]
        self._record_priority(candidates, now)
        if gate_read:
            self._sync_attention(waits, critical, read_repos, read_errors == 0, now)
        room = self._room({candidate.harness for candidate in candidates}, now)
        share_room = self._share_room(candidates, now)
        fed: list[TicketRef] = []
        waiting = 0
        blocked = 0
        blocked_on: list[tuple[_Candidate, Wait]] = []
        busy: set[str] = set()
        errors = read_errors + stop_errors + map_errors + (0 if gate_read else 1)
        order = sorted(candidates, key=lambda item: (states[item.ref] not in IN_FLIGHT, *item.order()))
        while order:
            candidate = next(
                (c for c in order if states[c.ref] in IN_FLIGHT or share_room.get(c.ref.repo, 0) > 0),
                order[0],
            )
            order.remove(candidate)
            if candidate.ref in skip or candidate.ref in unread:
                continue
            state = self._state(candidate)
            if state in FINAL:
                continue
            wait = None
            if candidate.ref not in begun:
                wait = line_stop(stops.get(candidate.line), candidate.issue.labels)
            if wait is None and state in UNSTARTED:
                wait = waits.get(candidate.ref) or holds.collision(candidate.ref, candidate.lines)
            if wait is not None:
                writes.append(_state_write(candidate, "waiting-blocked", reason=wait.reason))
                if wait.open_blockers and candidate.ref.tracker == "github":
                    blocked_on.append((candidate, wait))
                blocked += 1
                continue
            if room.get(candidate.harness, 0) <= 0:
                if state not in IN_FLIGHT:
                    writes.append(_state_write(candidate, "waiting-capacity"))
                busy.add(candidate.ref.repo)
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
            except _SplitParent as refusal:
                holds.drop(candidate.ref)
                room[candidate.harness] += 1
                reason = f"already split into parts ({refusal.evidence})"
                writes.append(_state_write(candidate, "split-parent", reason=reason))
                self.log(f"feeder: {candidate.issue.repo}#{candidate.issue.key} refused: {reason}")
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
            busy.add(candidate.ref.repo)
            if candidate.ref.repo in share_room:
                share_room[candidate.ref.repo] -= 1
            self.log(f"feeder: fed {candidate.issue.repo}#{candidate.issue.key} as {ref.ticket_id}")
            if sharing:
                shares = "; ".join(
                    f"{path} with {labels(holders, candidate.ref.repo)}"
                    for path, holders in sharing.items()
                )
                self.log(f"feeder: {candidate.issue.repo}#{candidate.issue.key} shares {shares}")
        self._set_states(writes)
        if gate_read:
            self._sync_idle(blocked_on, busy, read_repos, now)
        return FeedReport(
            fed=tuple(fed),
            eligible=len(candidates),
            waiting=waiting,
            blocked=blocked,
            errors=errors,
            refused=len(refused),
        )

    def _retire_abandoned(self) -> bool:
        """A fed or done row whose ticket the dispatcher abandoned is abandoned, or split-parent if auto-split."""
        with self.queue.connect() as connection:
            if not _has_table(connection, "ticket_state"):
                return True
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ticket_state)")}
            if not {"repo", "ticket_id", "workflow_state"} <= columns:
                return True
            split = "0"
            if _has_table(connection, "ticket_transitions"):
                split = """EXISTS (
                    SELECT 1 FROM ticket_transitions AS transition
                    WHERE lower(transition.repo) = feed.repo AND transition.ticket_id = feed.ticket_id
                      AND transition.to_state = 'abandoned' AND transition.source = 'plan-auto-split'
                )"""
            found = connection.execute(
                f"""
                SELECT feed.repo, feed.issue, feed.pr_number, {split}
                FROM {FEEDER_TABLE} AS feed
                WHERE feed.state IN ({",".join("?" * len(FED))}) AND feed.ticket_id IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM ticket_state AS ticket
                    WHERE lower(ticket.repo) = feed.repo AND ticket.ticket_id = feed.ticket_id
                      AND ticket.workflow_state = 'abandoned'
                  )
                """,
                FED,
            ).fetchall()
        if not found:
            return True
        retired = [
            (repo, issue, "split-parent", f"split into parts on PR #{pull}") if is_split
            else (repo, issue, "abandoned", "ticket abandoned")
            for repo, issue, pull, is_split in found
        ]

        def work(connection: sqlite3.Connection) -> None:
            connection.executemany(
                f"UPDATE {FEEDER_TABLE} SET state = ?, waiting_reason = ?, released = 1, error = NULL "
                f"WHERE repo = ? AND issue = ? AND state IN ({','.join('?' * len(FED))})",
                [(state, reason, repo, issue, *FED) for repo, issue, state, reason in retired],
            )

        if not bookkeeping_write(
            self.queue.connect, work, site="_retire_abandoned", ledger=self.ledger
        ):
            return False
        for repo, issue, _, reason in retired:
            self.log(f"feeder: retired {repo}#{issue}: {reason}")
        return True

    def _contexts(self) -> tuple[list[tuple[Project, FactoryContext]], int]:
        found: list[tuple[Project, FactoryContext]] = []
        errors = 0
        for project in self.projects:
            try:
                found.append((project, self.factory_for(project)))
            except Exception as exc:
                errors += 1
                self.log(f"feeder: cannot read intake for {project.repo}: {safe_ship_error(exc)}")
        return found, errors

    def _close_split_parents(
        self, contexts: Sequence[tuple[Project, FactoryContext]], now: float
    ) -> int:
        errors = 0
        for project, context in contexts:
            intake = project.intake
            assert intake is not None
            if project.linear_project_ids or intake.label is None or context.config.tickets != "github":
                continue
            errors += self._close_repo_split_parents(project.repo, intake.label, now)
        return errors

    def _close_repo_split_parents(self, repo: str, label: str, now: float) -> int:
        key = repo.lower()
        with self.queue.connect() as connection:
            row = connection.execute(
                f"SELECT at FROM {SPLIT_CURSOR_TABLE} WHERE repo = ?", (key,)
            ).fetchone()
            pending = dict(connection.execute(
                f"SELECT parent, checked_at FROM {SPLIT_UNSETTLED_TABLE} WHERE repo = ?", (key,)
            ).fetchall())
            fresh = {
                int(issue) for (issue,) in connection.execute(
                    f"SELECT issue FROM {FEEDER_TABLE} WHERE repo = ? AND tracker = 'github' "
                    "AND state = 'split-parent' AND split_noted = 0",
                    (key,),
                ) if str(issue).isdigit()
            }
            if row is not None:
                cursor = float(row[0])
            else:
                last = last_feeder_poll(connection)
                cursor = now if last is None else last - SPLIT_CURSOR_OVERLAP_SECONDS
        try:
            closed = self.github.closed_issues_since(repo, cursor)
        except Exception as exc:
            self.log(f"feeder: cannot read closed issues for {repo}: {safe_ship_error(exc)}")
            return 1
        errors = 0
        triggers: dict[int, list[IssueRecord]] = {}
        for record in closed:
            parent = split_parent(record.body)
            if record.reason == "completed" and parent is not None:
                triggers.setdefault(parent, []).append(record)
        noted: list[int] = []
        merged_parents: set[int] = set()
        for issue in sorted(self._merged_issues(repo, unnoted=True)):
            try:
                record = self.github.issue_record(repo, issue)
            except Exception as exc:
                errors += 1
                self.log(f"feeder: cannot read merged part {repo}#{issue}: {safe_ship_error(exc)}")
                continue
            noted.append(issue)
            parent = split_parent(record.body)
            if parent is not None:
                merged_parents.add(parent)
        due = {parent for parent, at in pending.items() if now - at >= SPLIT_RECHECK_SECONDS}
        check = merged_parents | due | fresh | (triggers.keys() - pending.keys())
        merged = self._merged_issues(repo) if check else frozenset()
        unsettled = {parent: at for parent, at in pending.items() if parent not in check}
        for parent in sorted(check):
            try:
                outcome = close_split_parent(
                    self.github, repo, parent, triggers.get(parent, ()), merged, label
                )
            except Exception as exc:
                errors += 1
                unsettled[parent] = now
                self.log(f"feeder: cannot close split parent {repo}#{parent}: {safe_ship_error(exc)}")
                continue
            if outcome == "parts-open":
                unsettled[parent] = now
            elif outcome == "closed":
                self.log(f"feeder: closed split parent {repo}#{parent} (all parts merged)")
        if not closed and not check and not noted:
            return errors

        def work(connection: sqlite3.Connection) -> None:
            connection.execute(
                f"INSERT INTO {SPLIT_CURSOR_TABLE} (repo, at) VALUES (?, ?) "
                "ON CONFLICT(repo) DO UPDATE SET at = excluded.at",
                (key, now - SPLIT_CURSOR_OVERLAP_SECONDS),
            )
            connection.execute(f"DELETE FROM {SPLIT_UNSETTLED_TABLE} WHERE repo = ?", (key,))
            connection.executemany(
                f"INSERT INTO {SPLIT_UNSETTLED_TABLE} (repo, parent, checked_at) VALUES (?, ?, ?)",
                [(key, parent, at) for parent, at in sorted(unsettled.items())],
            )
            connection.executemany(
                f"UPDATE {FEEDER_TABLE} SET split_noted = 1 WHERE repo = ? AND issue = ?",
                [(key, str(issue)) for issue in sorted({*noted, *fresh})],
            )

        bookkeeping_write(self.queue.connect, work, site="_close_split_parents", ledger=self.ledger)
        return errors

    def _merged_issues(self, repo: str, *, unnoted: bool = False) -> frozenset[int]:
        with self.queue.connect() as connection:
            return _merged_issues(connection, repo, unnoted=unnoted)

    def _candidates(
        self, contexts: Sequence[tuple[Project, FactoryContext]]
    ) -> tuple[list[_Candidate], set[str], int]:
        found: list[_Candidate] = []
        read: set[str] = set()
        errors = 0
        started = self._started()
        rows = self._main_fix_rows()
        for project, context in contexts:
            intake = project.intake
            assert intake is not None
            try:
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
            local = [active for active in rows if active.repo.lower() == project.repo.lower()]
            for issue in issues:
                if issue.repo.lower() != project.repo.lower() or not _KEY.fullmatch(issue.key):
                    continue
                row = None
                if issue.tracker == "github" and is_main_fix(issue.labels):
                    row = next((active for active in local if has_marker(issue.body, active.marker)), None)
                fate = "feed" if row is None else marked_issue_fate(row, int(issue.key))
                if fate == "close":
                    try:
                        self.github.close_issue(
                            project.repo,
                            int(issue.key),
                            f"Closing as a duplicate of #{row.issue}, the main-fix issue for this red streak.",
                        )
                    except Exception as exc:
                        errors += 1
                        self.log(
                            f"feeder: cannot close main-fix duplicate {project.repo}#{issue.key}: "
                            f"{safe_ship_error(exc)}"
                        )
                if fate != "feed":
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
                        template=parse_intake(issue.body, issue.labels, head=self._branch_for(issue)),
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

    def _add_map_defects(
        self, candidates: Sequence[_Candidate], states: Mapping[Ref, str]
    ) -> tuple[set[Ref], int]:
        maps: dict[tuple[str, str | None, str], frozenset[str] | None] = {}
        unread: set[Ref] = set()
        errors = 0
        for candidate in candidates:
            journeys = candidate.template.journeys
            if not journeys or states[candidate.ref] not in UNSTARTED:
                continue
            qa = candidate.context.config.qa
            verify = qa.verify if qa else None
            if verify is None:
                found = [NO_VERIFY]
            else:
                key = (candidate.project.repo.lower(), candidate.base, verify)
                if key not in maps:
                    maps[key] = self._map_ids(candidate.project.repo, candidate.base, verify)
                    errors += maps[key] is None
                ids = maps[key]
                if ids is None:
                    unread.add(candidate.ref)
                    continue
                found = map_problems(journeys, ids)
            if found:
                defects = tuple(Defect(code, reason) for code, reason in found)
                template = candidate.template
                candidate.template = replace(template, defects=template.defects + defects)
        return unread, errors

    def _map_ids(self, repo: str, base: str | None, verify: str) -> frozenset[str] | None:
        try:
            sha = self.github.branch_sha(repo, base) if base else None
            if sha is not None:
                skill = self._skill_dir(repo, verify, sha)
                return map_ids(self.github.dir_names(repo, f"{skill}/features", sha))
            why = f"no {base or 'base'} branch"
        except Exception as exc:
            why = safe_ship_error(exc)
        self.log(f"feeder: {repo}@{base} feature map unreadable: {why}")
        return None

    def _skill_dir(self, repo: str, verify: str, sha: str) -> str:
        tail = f"/{verify}/SKILL.md"
        folders = [""]
        best: str | None = None
        reads = 0
        while folders and (best is None or folders[0] < best):
            if reads == SKILL_WALK_READS:
                raise RuntimeError(f"verify skill {verify} not found in {SKILL_WALK_READS} tree reads")
            prefix = heapq.heappop(folders)
            folder = prefix.removesuffix("/")
            tree, truncated = self.github.tree_paths(repo, sha, folder)
            reads += 1
            paths = [prefix + path for path in tree]
            if truncated:
                for name in self.github.dir_names(repo, folder, sha):
                    if name.endswith("/"):
                        heapq.heappush(folders, prefix + name)
                    else:
                        paths.append(prefix + name)
            best = min((path for path in (*paths, best) if path and path.endswith(tail)), default=None)
        if best is None:
            raise RuntimeError(f"verify skill {verify} not found at {sha}")
        return best.removesuffix("/SKILL.md")

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

    def _main_reads(
        self, contexts: Sequence[tuple[Project, FactoryContext]]
    ) -> tuple[dict[tuple[str, str], _MainLine], int]:
        lines: dict[tuple[str, str], _MainLine] = {}
        errors = 0
        for project, context in contexts:
            intake = project.intake
            assert intake is not None
            config = context.config
            if (
                project.linear_project_ids
                or intake.label is None
                or intake.base_branches
                or config.tickets != "github"
                or not config.required_checks
            ):
                continue
            base = project.base_for(None) or config.base_branch
            try:
                red = main_red(self.github.base_checks(project.repo, base), config.required_checks)
                read = MainRead(red, False)
            except Exception as exc:
                errors += 1
                self.log(f"feeder: cannot read main CI for {project.repo}@{base}: {safe_ship_error(exc)}")
                read = MainRead(None, True)
            lines[(project.repo.lower(), base)] = _MainLine(
                project, intake.label, tuple(config.required_checks), read
            )
        return lines, errors

    def _file_main_fixes(self, lines: Mapping[tuple[str, str], _MainLine], now: float) -> int:
        errors = 0
        for (repo, base), line in lines.items():
            row = self._main_fix_row(repo, base)
            issue_open: bool | None = None
            feed_begun = False
            if row is not None and row.issue is not None and not line.read.failed:
                feed_begun = self._main_fix_feed_begun(repo, row.issue)
                try:
                    issue_open = self.github.issue_state(line.project.repo, row.issue) == "open"
                except Exception as exc:
                    self.log(f"feeder: cannot read main-fix {repo}#{row.issue}: {safe_ship_error(exc)}")
            try:
                for action in decide(row, line.read, issue_open=issue_open, feed_begun=feed_begun):
                    if isinstance(action, File):
                        claimed = self._claim_main_fix(line, repo, base, action.red, now)
                        if claimed is not None:
                            self._post_main_fix(line, *claimed)
                        continue
                    if isinstance(action, MainFixComment):
                        self._comment_main_fix(line, action.row, action.red)
                        continue
                    if isinstance(action, Close):
                        self._close_main_fix(line, action.row)
                        continue
                    claim = None
                    if action.row.state == "filing":
                        claim = self._take_main_fix(action.row.marker, "filing")
                        if claim is None:
                            continue
                    if isinstance(action, Retire):
                        self._retire_main_fix(action.row)
                    elif claim is not None:
                        self._post_main_fix(line, action.row, claim)
            except Exception as exc:
                errors += 1
                self.log(f"feeder: main-fix for {repo}@{base} not filed yet: {safe_ship_error(exc)}")
        return errors

    def _main_fix_feed_begun(self, repo: str, issue: int) -> bool:
        with self.queue.connect() as connection:
            found = connection.execute(f"SELECT 1 {_FEED_BEGUN}", (repo, str(issue), *HOLDING)).fetchone()
        return found is not None

    def _main_fix_rows(self) -> list[MainFixRow]:
        with self.queue.connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(MainFixRow.__dataclass_fields__)} FROM {MAIN_FIX_TABLE} "
                "WHERE state IN ('filing', 'open', 'closing')"
            ).fetchall()
        return [MainFixRow(*row) for row in rows]

    def _main_fix_row(self, repo: str, base: str) -> MainFixRow | None:
        with self.queue.connect() as connection:
            row = connection.execute(
                f"SELECT {', '.join(MainFixRow.__dataclass_fields__)} FROM {MAIN_FIX_TABLE} "
                "WHERE repo = ? AND base = ? AND state IN ('filing', 'open', 'closing')",
                (repo, base),
            ).fetchone()
        return None if row is None else MainFixRow(*row)

    def _claim_main_fix(
        self, line: _MainLine, repo: str, base: str, red: MainRed, now: float
    ) -> tuple[MainFixRow, str] | None:
        tag = marker(repo, base, now)
        body = self._main_fix_body(line, base, red, tag)
        row = MainFixRow(repo, base, tag, "filing", None, red.head, now, issue_title(red), body)
        claim = uuid.uuid4().hex
        try:
            with self.queue.connect() as connection:
                values = {**asdict(row), "claimed_by": claim, "claimed_at": self.queue.now()}
                connection.execute(
                    f"INSERT INTO {MAIN_FIX_TABLE} ({', '.join(values)}) "
                    f"VALUES ({', '.join(':' + name for name in values)})",
                    values,
                )
        except sqlite3.IntegrityError:
            return None
        return row, claim

    def _take_main_fix(self, marker: str, state: str) -> str | None:
        claim = uuid.uuid4().hex
        now = self.queue.now()
        with self.queue.connect() as connection:
            taken = connection.execute(
                f"UPDATE {MAIN_FIX_TABLE} SET claimed_by = ?, claimed_at = ? "
                "WHERE marker = ? AND state = ? AND (claimed_by IS NULL OR claimed_at <= ?)",
                (claim, now, marker, state, now - CLAIM_SECONDS),
            ).rowcount
        return claim if taken == 1 else None

    def _renew_main_fix(self, marker: str, claim: str, state: str) -> None:
        now = self.queue.now()
        with self.queue.connect() as connection:
            renewed = connection.execute(
                f"UPDATE {MAIN_FIX_TABLE} SET claimed_at = ? "
                "WHERE marker = ? AND state = ? AND claimed_by = ? AND claimed_at > ?",
                (now, marker, state, claim, now - CLAIM_SECONDS),
            ).rowcount
        if renewed != 1:
            raise _ClaimLost(marker)

    def _release_main_fix(self, marker: str, claim: str) -> None:
        with self.queue.connect() as connection:
            connection.execute(
                f"UPDATE {MAIN_FIX_TABLE} SET claimed_by = NULL WHERE marker = ? AND claimed_by = ?",
                (marker, claim),
            )

    def _retire_main_fix(self, row: MainFixRow) -> None:
        with self.queue.connect() as connection:
            connection.execute(
                f"UPDATE {MAIN_FIX_TABLE} SET state = 'closed' WHERE marker = ? AND state = ?",
                (row.marker, row.state),
            )

    def _main_fix_body(self, line: _MainLine, base: str, red: MainRed, tag: str) -> str:
        runs = self._main_fix_runs(line.project.repo, red)
        evidence = self._main_fix_evidence(line.project.repo, red, runs)
        return issue_body(line.project.repo, base, line.required, red, evidence, tag)

    def _main_fix_runs(self, repo: str, red: MainRed) -> dict[str, Mapping[str, str] | None]:
        runs: dict[str, Mapping[str, str] | None] = {}
        for run_id in dict.fromkeys(check.run_id for check in red.checks):
            try:
                runs[run_id] = self.github.failed_run_logs(repo, run_id)
            except Exception as exc:
                self.log(f"feeder: {repo} run {run_id} log unavailable: {safe_ship_error(exc)}")
                runs[run_id] = None
        return runs

    def _main_fix_evidence(
        self, repo: str, red: MainRed, runs: Mapping[str, Mapping[str, str] | None]
    ) -> CIEvidence:
        logs = {
            check.job_id: run_logs.get(check.name)
            for check in red.checks
            if (run_logs := runs[check.run_id]) is not None
        }
        failed = [
            FailedCheck(check.name, check.run_id, check.job_id,
                        f"https://github.com/{repo}/actions/runs/{check.run_id}")
            for check in red.checks
        ]
        return ci_evidence(failed=failed, logs=logs, limit=sys.maxsize)

    def _post_main_fix(self, line: _MainLine, row: MainFixRow, claim: str) -> None:
        repo = line.project.repo
        try:
            number = self.github.file_repo_issue(
                repo, row.title, row.body, (line.label, MAIN_FIX_LABEL), row.marker, row.filed_at,
                before_post=lambda: self._renew_main_fix(row.marker, claim, "filing"),
            )
        except _ClaimLost:
            self._release_main_fix(row.marker, claim)
            self.log(f"feeder: main-fix for {repo}@{row.base} left to the feeder now holding it")
            return
        except Exception:
            self._release_main_fix(row.marker, claim)
            raise
        keep = self._close_main_fix_duplicates(repo, row, number)
        with self.queue.connect() as connection:
            connection.execute(
                f"UPDATE {MAIN_FIX_TABLE} SET state = 'open', issue = ? "
                "WHERE marker = ? AND state IN ('filing', 'open')",
                (keep, row.marker),
            )
        self._release_main_fix(row.marker, claim)
        self.log(f"feeder: filed main-fix {repo}#{keep} for red {row.base} at {row.last_head[:8]}")

    def _comment_main_fix(self, line: _MainLine, row: MainFixRow, red: MainRed) -> None:
        repo = line.project.repo
        claim = self._take_main_fix(row.marker, "open")
        if claim is None:
            return
        try:
            runs = self._main_fix_runs(repo, red)
            if None in runs.values():
                raise RuntimeError(f"a log of red {red.head[:8]} is unreadable")
            evidence = self._main_fix_evidence(repo, red, runs)
            tag = head_marker(row, red.head)
            body = head_comment(row.base, red, evidence, tag)
            self._renew_main_fix(row.marker, claim, "open")
            self.github.post_issue_comment_once(repo, row.issue, body, tag)
        except _ClaimLost:
            self._release_main_fix(row.marker, claim)
            self.log(f"feeder: main-fix {repo}#{row.issue} left to the feeder now holding it")
            return
        except Exception:
            self._release_main_fix(row.marker, claim)
            raise
        with self.queue.connect() as connection:
            connection.execute(
                f"UPDATE {MAIN_FIX_TABLE} SET last_head = ?, claimed_by = NULL "
                "WHERE marker = ? AND claimed_by = ?",
                (red.head, row.marker, claim),
            )
        self.log(f"feeder: added {red.head[:8]} log to main-fix {repo}#{row.issue}")

    def _close_main_fix(self, line: _MainLine, row: MainFixRow) -> None:
        repo = line.project.repo
        claim = self._begin_main_fix_close(row)
        if claim is None:
            return
        try:
            self.github.post_issue_comment_once(repo, row.issue, close_comment(row), close_marker(row))
            self.github.close_issue(repo, row.issue, None)
        except Exception:
            self._release_main_fix(row.marker, claim)
            raise
        with self.queue.connect() as connection:
            connection.execute(
                f"UPDATE {MAIN_FIX_TABLE} SET state = 'closed-unfed', claimed_by = NULL "
                "WHERE marker = ? AND claimed_by = ?",
                (row.marker, claim),
            )
        self.log(f"feeder: closed never-fed main-fix {repo}#{row.issue}: {row.base} is green")

    def _begin_main_fix_close(self, row: MainFixRow) -> str | None:
        claim = uuid.uuid4().hex
        now = self.queue.now()
        with self.queue.connect() as connection:
            taken = connection.execute(
                f"UPDATE {MAIN_FIX_TABLE} SET state = 'closing', claimed_by = ?, claimed_at = ? "
                "WHERE marker = ? AND state IN ('open', 'closing') "
                f"AND (claimed_by IS NULL OR claimed_at <= ?) AND NOT EXISTS (SELECT 1 {_FEED_BEGUN})",
                (claim, now, row.marker, now - CLAIM_SECONDS, row.repo, str(row.issue), *HOLDING),
            ).rowcount
        return claim if taken == 1 else None

    def _close_main_fix_duplicates(self, repo: str, row: MainFixRow, posted: int) -> int:
        found = (posted, *self.github.marked_issues(repo, row.marker, row.filed_at))
        keep = min(found)
        for duplicate in sorted(set(found) - {keep}):
            self.github.close_issue(
                repo, duplicate, f"Closing as a duplicate of #{keep}, the main-fix issue for this red streak."
            )
        return keep

    def _line_stops(
        self,
        candidates: Sequence[_Candidate],
        begun: Collection[Ref],
        refused: Collection[Ref],
        mains: Mapping[tuple[str, str], _MainLine],
    ) -> tuple[dict[tuple[str, str | None], Wait | None], int]:
        """Why each base stops the line, read once per poll, and how many
        reads failed. A failed read stops the line on that base."""
        stops: dict[tuple[str, str | None], Wait | None] = {}
        for key, main in mains.items():
            if main.read.failed:
                stops[key] = Wait(f"main CI unreadable for {key[1]}")
            else:
                stops[key] = None if main.read.red is None else main.read.red.wait
        errors = 0
        for candidate in candidates:
            required = candidate.context.config.required_checks
            base = candidate.base
            if (
                base is None
                or not required
                or candidate.line in stops
                or candidate.ref in begun
                or candidate.ref in refused
            ):
                continue
            repo = candidate.project.repo
            try:
                red = main_red(self.github.base_checks(repo, base), required)
                stops[candidate.line] = None if red is None else red.wait
            except Exception as exc:
                errors += 1
                self.log(f"feeder: cannot read main CI for {repo}@{base}: {safe_ship_error(exc)}")
                stops[candidate.line] = Wait(f"main CI unreadable for {base}")
        return stops, errors

    # -- gates (#156) ----------------------------------------------------------------

    def _gates(
        self,
        candidates: Sequence[_Candidate],
        states: Mapping[Ref, str],
        refused: Collection[Ref] = frozenset(),
    ) -> tuple[
        dict[Ref, Wait],
        dict[tuple[str, str, str], str],
        Holds,
        set[Ref],
        bool,
        dict[Ref, int],
        dict[Ref, int | None],
    ]:
        """Blocker waits, the critical-path blocker flags, the holds of
        tickets in flight, the holders that hold as capped, whether the
        status read (when one was needed) succeeded, how many tickets each
        candidate unblocks, and each candidate's effective tier. A ticket
        refused at intake is not gated, but every candidate, fed or not,
        passes its tier to its blockers."""
        rows = self._feeder_rows()
        unstarted = [
            c for c in candidates if states[c.ref] in UNSTARTED and c.ref not in refused
        ]
        linear_refs = {ref.key: ref for ref in rows if ref.tracker == "linear"}
        linear_refs.update({c.ref.key: c.ref for c in candidates if c.ref.tracker == "linear"})

        def linear_ref(repo: str, key: str) -> Ref:
            return linear_refs.get(key) or Ref("linear", repo.lower(), key)

        lines_of = {
            c.ref: replace(c.lines, blocked_by=frozenset(
                linear_ref(ref.repo, ref.key) if ref.tracker == "linear" else ref
                for ref in c.lines.blocked_by
            ))
            for c in candidates
            if c.lines.blocked_by
        }
        gated = {c.ref: lines_of[c.ref] for c in unstarted if c.ref in lines_of}
        conflicts = {ref for c in unstarted for ref in c.lines.conflicts_with}
        pull_of = {ref: row.pull for ref, row in rows.items() if row.pull is not None}
        known = set(rows) | set(pull_of.values()) | {c.ref for c in candidates}
        holders = {
            ref: row
            for ref, row in rows.items()
            if not row.released and (row.lines or ref in conflicts or row.pull in conflicts)
        }
        watched = {ref: row.pull for ref, row in rows.items() if row.watch and row.pull is not None}
        tracked = {project.repo.lower() for project in self.config.projects}
        blockers = {ref for lines in gated.values() for ref in lines.blocked_by}
        flaggable = {ref: lines for ref, lines in lines_of.items() if ref not in refused}
        outside = {
            ref
            for lines in flaggable.values()
            for ref in lines.blocked_by
            if ref not in known and ref.repo in tracked
        }
        via = {ref: pull_of[ref] for ref in blockers if ref in pull_of}
        linear = {
            linear_ref(c.issue.repo, key): linear_status(state_type)
            for c in candidates
            for key, state_type in c.issue.blocked_by
        }
        external = {ref for ref in conflicts if ref not in known}
        needed = {ref for ref in blockers | outside if ref.tracker == "github"}
        needed |= set(via.values()) | external
        needed |= set(watched.values())
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
        labelled: list[Ref] = []
        for ref, pull in sorted(watched.items()):
            if (status := statuses.get(pull)) is not None and status.abandoned:
                labelled.append(ref)
            elif not rows[ref].released and _progress(statuses, pull) in (Progress.DONE, Progress.UNFINISHED):
                released.append(ref)
        ended = {*released, *labelled}
        capped = self._capped({row.ticket for row in holders.values() if row.ticket})
        flips: list[tuple[Ref, bool]] = []
        capped_refs: set[Ref] = set()
        for ref, row in sorted(holders.items()):
            if ref in ended:
                continue
            pull = row.pull
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
        self._end_holds(released, labelled)
        self._flip_capped(flips)
        shared = {repo: policy.shared for repo, policy in holds.policies.items()}
        blocks = _dependents_by_blocker(candidates, gated, pull_of)
        unblocks = critical_path_counts(candidates, blocks, {c.ref for c in unstarted}, shared)
        inherits = _dependents_by_blocker(candidates, lines_of, pull_of)
        tiers = effective_tiers(candidates, inherits)
        gates: dict[Ref, set[Ref]] = {}
        for dependent, lines in flaggable.items():
            for blocker in lines.blocked_by & outside:
                if _progress(statuses, blocker) is Progress.OPEN:
                    gates.setdefault(blocker, set()).add(dependent)
        critical: dict[tuple[str, str, str], str] = {}
        for blocker, dependents in gates.items():
            found = _with_dependents(inherits, dependents)
            best = min((tier for ref in found if (tier := tiers[ref]) is not None), default=None)
            critical[(blocker.repo, blocker.key, ATTENTION_CRITICAL_PATH)] = (
                f"gates {len(found)} candidates, best tier {'none' if best is None else best}"
            )
        return blocker_waits(gated, statuses), critical, holds, capped_refs, ok, unblocks, tiers

    def _feeder_rows(self) -> dict[Ref, _Holder]:
        """Tickets fed or feeding, and whether each one's PR status is still read."""
        with self.queue.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT repo, issue, tracker, pr_number, ticket_id, touches, conflicts,
                       released, capped_release,
                       NOT released OR state IN ({",".join("?" * len(FED))}) AS watch
                FROM {FEEDER_TABLE} AS feed
                WHERE state IN ({",".join("?" * len(HOLDING))})
                """,
                (*FED, *HOLDING),
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
                watch=bool(row["watch"]),
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

    def _end_holds(self, released: Sequence[Ref], labelled: Sequence[Ref]) -> None:
        """A holder whose PR merged or closed stops holding, for good; a fed or
        done row whose PR carries orch:state/abandoned is abandoned too."""
        if not released and not labelled:
            return
        retired: list[Ref] = []

        def work(connection: sqlite3.Connection) -> None:
            retired.clear()
            connection.executemany(
                f"UPDATE {FEEDER_TABLE} SET released = 1 WHERE repo = ? AND issue = ?",
                [(ref.repo, ref.key) for ref in (*released, *labelled)],
            )
            for ref in labelled:
                cursor = connection.execute(
                    f"UPDATE {FEEDER_TABLE} SET state = 'abandoned', "
                    "waiting_reason = 'PR labelled orch:state/abandoned' "
                    f"WHERE repo = ? AND issue = ? AND state IN ({','.join('?' * len(FED))})",
                    (ref.repo, ref.key, *FED),
                )
                if cursor.rowcount:
                    retired.append(ref)

        if not bookkeeping_write(self.queue.connect, work, site="_end_holds", ledger=self.ledger):
            self.log("feeder: ended holds not recorded, orch.db is locked; retrying next poll")
            return
        for ref in retired:
            self.log(f"feeder: retired {ref.repo}#{ref.key}: PR labelled orch:state/abandoned")

    def _sync_attention(
        self,
        waits: Mapping[Ref, Wait],
        critical: Mapping[tuple[str, str, str], str],
        repos: set[str],
        every_repo_read: bool,
        now: float,
    ) -> None:
        flags = {
            (ref.repo, ref.key, wait.attention): wait.reason
            for ref, wait in waits.items()
            if wait.attention is not None
        }
        tracked = {project.repo.lower() for project in self.config.projects} if every_repo_read else set()
        with self.queue.connect() as connection:
            attention.sync(connection, WAIT_REASONS, repos, flags, now)
            attention.sync(connection, (ATTENTION_CRITICAL_PATH,), tracked, critical, now)

    def _sync_idle(
        self,
        blocked: Sequence[tuple[_Candidate, Wait]],
        busy: set[str],
        repos: set[str],
        now: float,
    ) -> None:
        """Flag each ticket waiting on open blockers in a project with nothing
        in flight, and wake the project's Grok bot once per stall. busy holds
        the repos that fed a ticket or had one wait on capacity this poll."""
        stalls: dict[str, list[tuple[_Candidate, Wait]]] = {}
        for candidate, wait in blocked:
            if candidate.ref.repo not in busy:
                stalls.setdefault(candidate.ref.repo, []).append((candidate, wait))
        scope = set(repos)
        seen: dict[str, float] = {}
        with self.queue.connect() as connection:
            for repo in sorted(stalls):
                running, seen[repo] = connection.execute(
                    "SELECT COALESCE(SUM(state IN ('queued', 'running')), 0),"
                    " COALESCE(MAX(MAX(created_at, COALESCE(finished_at, created_at))), 0)"
                    " FROM jobs WHERE lower(repo) = ? AND recipient != 'grok'",
                    (repo,),
                ).fetchone()
                pending = connection.execute(
                    f"{_FED_WITHOUT_JOB} AND item.repo = ?", (now - FED_PENDING_SECONDS, repo)
                ).fetchone()[0]
                if running or pending:
                    del stalls[repo]
                    continue
                project = stalls[repo][0][0].project
                try:
                    open_pulls = self.github.open_pull_commits(project.repo)
                except Exception as exc:
                    self.log(f"feeder: cannot read open PRs of {project.repo}: {safe_ship_error(exc)}")
                    scope.discard(repo)
                    del stalls[repo]
                    continue
                if _factory_pull(connection, repo, open_pulls):
                    del stalls[repo]
            since: dict[str, float] = {}
            for repo in sorted(scope):
                if repo not in stalls:
                    connection.execute(f"DELETE FROM {IDLE_TABLE} WHERE repo = ?", (repo,))
                    continue
                row = connection.execute(
                    f"SELECT since, seen FROM {IDLE_TABLE} WHERE repo = ?", (repo,)
                ).fetchone()
                if row is None or seen[repo] > row[1]:
                    connection.execute(
                        f"INSERT INTO {IDLE_TABLE} (repo, since, seen) VALUES (?, ?, ?)"
                        " ON CONFLICT(repo) DO UPDATE SET since = excluded.since, seen = excluded.seen",
                        (repo, now, seen[repo]),
                    )
                    connection.execute(
                        "DELETE FROM needs_attention WHERE repo = ? AND reason = ?",
                        (repo, ATTENTION_IDLE_BLOCKED),
                    )
                    since[repo] = now
                else:
                    since[repo] = row[0]
            flags = {
                (repo, candidate.ref.key, ATTENTION_IDLE_BLOCKED): f"waiting on {_open_blockers(wait, repo)}"
                for repo, waiting in stalls.items()
                for candidate, wait in waiting
            }
            attention.sync(connection, (ATTENTION_IDLE_BLOCKED,), scope, flags, now)
            for repo, waiting in sorted(stalls.items()):
                waiting.sort(key=lambda item: _key_order(item[0].issue.key))
                self._wake_idle(connection, waiting, since[repo])

    def _wake_idle(
        self, connection: sqlite3.Connection, waiting: Sequence[tuple[_Candidate, Wait]], stall: float
    ) -> None:
        """Queue the stall's Grok wake, keyed on when the stall began."""
        first = waiting[0][0]
        project = first.project
        repo = project.repo
        tickets = "; ".join(
            f"{candidate.ref.label(repo)} waits on {_open_blockers(wait, repo)}"
            for candidate, wait in waiting
        )
        detail = f"{repo} has no work in flight: {tickets}."
        before = connection.total_changes
        self.queue.enqueue_in_transaction(
            connection,
            JobRequest(
                repo=repo,
                ticket_id=first.issue.key,
                action="blocked",
                recipient="grok",
                source_comment_id=f"feeder-idle:{repo}:{stall!r}",
                timeout_seconds=resolve_job_timeout(self.config, repo) * 60,
                detail=json.dumps(
                    {
                        "project_bot": project.grok_bot,
                        "detail": detail,
                        "ticket_url": first.issue.url,
                        "blocked": [candidate.issue.key for candidate, _ in waiting],
                    },
                    sort_keys=True,
                ),
            ),
            dedupe_key=f"feeder-idle:{repo.lower()}:{stall!r}",
        )
        if connection.total_changes > before:
            self.log(f"feeder: {detail} Woke {project.grok_bot}")

    # -- capacity --------------------------------------------------------------

    def _placement(
        self, connection: sqlite3.Connection, now: float
    ) -> tuple[MachinesConfig | None, Blocked]:
        machines = self.machines
        live = live_settings.peek(connection)
        if live is not None and machines is not None:
            machines = live.placement_config(machines)
        return machines, placement_blocked(connection, now, machines, self.head())

    def _room(self, harnesses: set[str], now: float) -> dict[str, int]:
        room: dict[str, int] = {}
        with self.queue.connect() as connection:
            machines, blocked = self._placement(connection, now)
            used = slots_in_use(connection, machines)
            for harness in harnesses:
                free = free_slots(machines, harness, used, blocked) if machines else 0
                sharing = sorted({harness, *(
                    other
                    for device in (machines.devices if machines else ())
                    if device.takes_new(harness)
                    for other in device.harnesses
                )})
                marks = ",".join("?" * len(sharing))
                queued = connection.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE state = 'queued' AND recipient IN ({marks})",
                    sharing,
                ).fetchone()[0]
                pending = connection.execute(
                    f"{_FED_WITHOUT_JOB} AND item.harness IN ({marks})",
                    (now - FED_PENDING_SECONDS, *sharing),
                ).fetchone()[0]
                room[harness] = free - int(queued) - int(pending)
        return room

    def _share_room(self, candidates: Sequence[_Candidate], now: float) -> dict[str, float]:
        """For each project with a capacity_share, how many more tickets it
        is fed before other projects' tickets: its share of the schedulable
        slots of its builder harness, minus its tickets in flight."""
        shares: dict[str, tuple[float, str]] = {}
        for c in candidates:
            assert c.project.intake is not None
            if c.project.intake.capacity_share is not None:
                shares[c.ref.repo] = (c.project.intake.capacity_share, c.harness)
        room: dict[str, float] = {}
        if not shares:
            return room
        with self.queue.connect() as connection:
            machines, blocked = self._placement(connection, now)
            for repo, (share, harness) in shares.items():
                slots = sum(
                    device.slot_max or 0
                    for device in (machines.devices if machines else ())
                    if device.takes_new(harness) and not blocked.blocks(device.name, harness)
                )
                running = connection.execute(
                    "SELECT COUNT(DISTINCT ticket_id) FROM jobs WHERE lower(repo) = ?"
                    " AND state IN ('queued', 'running') AND recipient != 'grok'",
                    (repo,),
                ).fetchone()[0]
                pending = connection.execute(
                    f"{_FED_WITHOUT_JOB} AND item.repo = ?", (now - FED_PENDING_SECONDS, repo)
                ).fetchone()[0]
                room[repo] = share * slots - int(running) - int(pending)
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
                        repo, issue, tracker, title, harness, state, urgent, bug, tier, own_tier,
                        risk, issue_created_at, first_seen_at, seen_at, touches, conflicts,
                        base_branch, target_source
                    ) VALUES (?, ?, ?, ?, ?, 'eligible', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo, issue) DO UPDATE SET
                        title = excluded.title,
                        touches = excluded.touches,
                        conflicts = excluded.conflicts,
                        urgent = excluded.urgent,
                        bug = excluded.bug,
                        tier = excluded.tier,
                        own_tier = excluded.own_tier,
                        risk = CASE WHEN state IN ({unstarted}) THEN excluded.risk
                                    ELSE risk END,
                        base_branch = CASE WHEN {FEED_NOT_BEGUN} THEN excluded.base_branch
                                           ELSE base_branch END,
                        target_source = CASE WHEN {FEED_NOT_BEGUN} THEN excluded.target_source
                                             ELSE target_source END,
                        harness = CASE WHEN state IN ({_FINAL_SQL}) THEN harness
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
                        candidate.own_tier,
                        candidate.own_tier,
                        candidate.template.risk or "unset",
                        issue.created_at,
                        now,
                        now,
                        json.dumps(sorted(candidate.lines.touches)),
                        json.dumps(
                            [[ref.tracker, ref.repo, ref.key] for ref in sorted(candidate.lines.conflicts_with)]
                        ),
                        candidate.base,
                        candidate.target_source,
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
        """Each ticket's unblocks count and effective tier, and the claim
        order of every ticket with a ticket id, so a label or blocker change
        after the feed still moves its queued job."""

        def work(connection: sqlite3.Connection) -> None:
            for candidate in candidates:
                issue = candidate.issue
                where = (issue.repo.lower(), issue.key)
                connection.execute(
                    f"UPDATE {FEEDER_TABLE} SET unblocks = ?, tier = ? WHERE repo = ? AND issue = ?",
                    (candidate.unblocks, candidate.effective_tier, *where),
                )
                ticket = connection.execute(
                    f"SELECT ticket_id FROM {FEEDER_TABLE} WHERE repo = ? AND issue = ?", where
                ).fetchone()[0]
                if ticket is not None:
                    set_ticket_priority(
                        connection, issue.repo, ticket, urgent=issue.urgent, bug=issue.bug,
                        tier=candidate.effective_tier, own_tier=candidate.own_tier,
                        unblocks=candidate.unblocks, at=now,
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
                WHERE repo = ? AND issue = ? AND state NOT IN ({_FINAL_SQL})
                """,
                writes,
            ),
            site=site,
            ledger=self.ledger,
        )

    def _start_feed(self, candidate: _Candidate) -> None:
        write = _state_write(candidate, "feeding")
        closing: list[bool] = []

        def work(connection: sqlite3.Connection) -> None:
            connection.execute(
                f"UPDATE {FEEDER_TABLE} SET state = ?, error = ?, waiting_reason = ? "
                f"WHERE repo = ? AND issue = ? AND state NOT IN ({_FINAL_SQL}) "
                f"AND NOT EXISTS ({_CLOSED_ON_GREEN})",
                (*write, write.repo, write.issue),
            )
            found = connection.execute(_CLOSED_ON_GREEN, (write.repo, write.issue)).fetchone()
            closing[:] = [found is not None]

        must_write(bookkeeping_write(self.queue.connect, work, site="_start_feed", ledger=self.ledger))
        if closing[0]:
            raise _Held(Wait(f"{_ticket_name(candidate.issue)} was closed because its base is green"))

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
        one a ticket in flight conflicts with, when the branch it would
        reuse carries another open PR's commits (#205), or when a green base
        has begun closing its main-fix issue.
        """
        issue = candidate.issue
        config = candidate.context.config
        row = self._row(candidate)
        evidence = self._split_evidence(candidate, row)
        if evidence is not None:
            raise _SplitParent(evidence)
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
        tips = {base: base_sha}
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
        self._start_feed(candidate)
        parent = self._stack_parent(row, repo, base, tips)
        risk = str(row["risk"])
        if risk not in ("low", "high"):
            raise ValueError(f"ticket risk {risk!r} is not low or high")
        template = candidate.template
        main_fix = is_main_fix(issue.labels)
        state = "building" if main_fix else go_state_for(issue.body, template.plan_first)
        self._record(
            candidate, branch=branch, base_branch=base,
            stack_parent_pr=parent and parent.pr,
            stack_parent_branch=parent and parent.branch,
            stack_parent_head_at_feed=parent and parent.head,
        )
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
                body=_pull_body(issue, parent),
            )
        elif parent is not None:
            body = self.github.pull_body(repo, number)
            line = _stack_line(parent)
            if line not in body:
                self.github.set_pull_body(repo, number, "\n\n".join(filter(None, (body.rstrip(), line))))
        ticket_id = issue.key if issue.tracker == "linear" else str(number)
        self._record(candidate, pr_number=str(number), ticket_id=ticket_id)
        holds.alias(candidate.ref, github_ref(repo, number))
        with self.queue.connect() as connection:
            set_ticket_priority(
                connection, repo, ticket_id, urgent=issue.urgent, bug=issue.bug,
                tier=candidate.effective_tier, own_tier=candidate.own_tier,
                unblocks=candidate.unblocks, at=now,
            )
        linear = issue.tracker == "linear"
        if not linear and config.tickets == "github":
            self.github.add_labels(
                repo, number,
                [FACTORY_LABEL, f"orch:state/{state}", *([MAIN_FIX_LABEL] if main_fix else [])],
            )
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
            qa = None
            verify = config.qa.verify if config.qa else None
            if template.journeys and verify:
                sha = tips.get(base) or self.github.branch_sha(repo, base)
                if sha is None:
                    raise ValueError(f"base branch {base} has no tip for the brief's QA journeys")
                criteria = ((criterion.id, criterion.text) for criterion in numbered(issue.body))
                qa = qa_plan(verify, sha, template.journeys, criteria)
            brief_id = post(
                _brief(issue, candidate.context, number=number, branch=branch, base=base,
                       risk=risk, state=state, visible=linear, template=template, qa=qa, parent=parent)
            )
        self._record(candidate, brief_comment_id=brief_id)
        go_id = row["go_comment_id"] or _existing(existing, "go", authors)
        if go_id is None:
            go_id = post(_go(issue, state=state, risk=risk, number=number, visible=linear))
        self._record(candidate, go_comment_id=go_id, state="fed", fed_at=now, error=None)
        return TicketRef(repo, ticket_id, issue.tracker)

    def _stack_parent(
        self, row: sqlite3.Row, repo: str, base: str, tips: dict[str, str]
    ) -> StackParent | None:
        if row["branch"] is not None:
            if row["stack_parent_pr"] is None:
                return None
            return StackParent(
                int(row["stack_parent_pr"]), row["stack_parent_branch"], row["stack_parent_head_at_feed"]
            )
        if row["target_source"] != "ticket":
            return None
        number = self.github.find_pull(repo, base)
        if number is None:
            return None
        head = tips.get(base) or self.github.branch_sha(repo, base)
        if head is None:
            return None
        return StackParent(number, base, head)

    def _tracked_open_pull(self, repo: str, ticket_id: str | None) -> int | None:
        if ticket_id is None:
            return None
        with self.queue.connect() as connection:
            _, recorded = read_sources(connection).get((repo.casefold(), ticket_id), ("", None))
        if recorded is None or not recorded.isdigit():
            return None
        number = int(recorded)
        return number if self.github.pull_state(repo, number) == "open" else None

    def main_fix_pr(self, repo: str, ticket_id: str) -> bool:
        with self.queue.connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM {FEEDER_TABLE} AS item JOIN {MAIN_FIX_TABLE} AS fix "
                "ON fix.repo = item.repo AND CAST(fix.issue AS TEXT) = item.issue "
                "WHERE item.repo = ? AND item.ticket_id = ?",
                (repo.lower(), ticket_id),
            ).fetchone()
        return row is not None

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

    def _split_evidence(self, candidate: _Candidate, row: sqlite3.Row) -> str | None:
        issue = candidate.issue
        if issue.tracker != "github":
            return None
        repo = candidate.project.repo
        number = int(issue.key)
        children = self.github.split_children(repo, number)
        pulls = set(self.github.branch_pulls(repo, row["branch"] or self._branch_for(issue)))
        if row["pr_number"]:
            pulls.add(int(row["pr_number"]))
        comments = list(self.github.comments(repo, number))
        for pull in sorted(pulls):
            comments += self.github.comments(repo, pull)
        trusted = intake_trusted(self.config, candidate.project) | self._orchestrator_authors("github")
        return split_evidence(issue.body, comments, trusted, children)

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


def _dependents_by_blocker(
    candidates: Sequence[_Candidate], eligible_gates: Mapping[Ref, GateLines], pull_of: Mapping[Ref, Ref]
) -> dict[Ref, set[Ref]]:
    owner = {c.ref: c.ref for c in candidates}
    owner.update({pull_of[c.ref]: c.ref for c in candidates if c.ref in pull_of})
    blocks: dict[Ref, set[Ref]] = {c.ref: set() for c in candidates}
    for dependent, lines in eligible_gates.items():
        for blocker in lines.blocked_by:
            if owner.get(blocker, dependent) != dependent:
                blocks[owner[blocker]].add(dependent)
    return blocks


def _with_dependents(blocks: Mapping[Ref, set[Ref]], seed: set[Ref]) -> set[Ref]:
    found = set(seed)
    pending = list(found)
    while pending:
        for dependent in blocks[pending.pop()] - found:
            found.add(dependent)
            pending.append(dependent)
    return found


def critical_path_counts(
    candidates: Sequence[_Candidate],
    blocks: Mapping[Ref, set[Ref]],
    unstarted: Collection[Ref],
    shared: Mapping[str, Collection[str]],
) -> dict[Ref, int]:
    counts: dict[Ref, int] = {}
    for c in candidates:
        assert c.project.intake is not None
        if not c.project.intake.critical_path_order:
            counts[c.ref] = 0
            continue
        own = shared.get(c.ref.repo, ())
        found = _with_dependents(blocks, blocks[c.ref] | {
            other.ref
            for other in candidates
            if other.ref in unstarted
            and other.ref.repo == c.ref.repo
            and _paths_meet(c.lines.touches, other.lines.touches, own)
        })
        found.discard(c.ref)
        counts[c.ref] = len(found)
    return counts


def effective_tiers(
    candidates: Sequence[_Candidate], blocks: Mapping[Ref, set[Ref]]
) -> dict[Ref, int | None]:
    own = {c.ref: c.own_tier for c in candidates}
    tiers: dict[Ref, int | None] = {}
    for c in candidates:
        found = [own[ref] for ref in _with_dependents(blocks, {c.ref})]
        tiers[c.ref] = min((tier for tier in found if tier is not None), default=None)
    return tiers


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


def _open_blockers(wait: Wait, home: str) -> str:
    return ", ".join(blocker.label(home) for blocker in wait.open_blockers)


def _factory_pull(connection: sqlite3.Connection, repo: str, pulls: Mapping[int, OpenPull]) -> bool:
    if any(pull.branch.startswith("factory/") for pull in pulls.values()):
        return True
    tracked = {
        str(number)
        for (number,) in connection.execute(
            f"SELECT pr_number FROM {FEEDER_TABLE} WHERE repo = ? AND pr_number IS NOT NULL", (repo,)
        )
    }
    if _has_table(connection, "ticket_state"):
        tracked |= {
            str(ticket)
            for (ticket,) in connection.execute(
                "SELECT ticket_id FROM ticket_state WHERE lower(repo) = ?"
                f" AND COALESCE(workflow_state, '') NOT IN ({','.join('?' * len(TERMINAL_STATES))})",
                (repo, *TERMINAL_STATES),
            )
        }
    return not tracked.isdisjoint(str(number) for number in pulls)


def _ticket_name(issue: IntakeIssue) -> str:
    return issue.key if issue.tracker == "linear" else f"#{issue.key}"


def _pull_title(issue: IntakeIssue) -> str:
    return prefixed(_ticket_name(issue), " ".join(issue.title.split())[:180])


def _stack_line(parent: StackParent) -> str:
    return f"Stacked on #{parent.pr}. Merge #{parent.pr} first."


def _pull_body(issue: IntakeIssue, parent: StackParent | None = None) -> str:
    lines = [f"Fixes {_ticket_name(issue)}", ""]
    if issue.url:
        lines.append(f"Ticket: {issue.url}")
    if parent is not None:
        lines.append(_stack_line(parent))
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
    qa: QaPlan | None = None,
    parent: StackParent | None = None,
) -> str:
    config = context.config
    stacked = f" (stacked on #{parent.pr} at `{parent.head}`)" if parent is not None else ""
    body = issue.body.strip() or "(the ticket has no body)"
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + f"\n[cut at {BODY_LIMIT} characters]"
    fence = _fence(body)
    if is_main_fix(issue.labels):
        why = "it is a main-fix ticket, so it starts building"
    elif state == "planning" and template.plan_first:
        why = "the ticket says Plan first: yes"
    elif state == "planning":
        why = "the ticket names more than 3 files, so it starts with a plan"
    else:
        why = "the ticket names 3 files or fewer, so it starts building"
    source = template.risk_source if template.risk == risk else None
    sections: list[str] = []
    for title, items in (
        ("Acceptance criteria", template.criteria),
        ("Assumed environment", template.assumed_environment),
        ("Out of scope", template.out_of_scope),
        ("Intake warnings", template.warnings),
        ("Ops step (operator action, not the builder's)", template.ops_criteria),
    ):
        if items:
            sections += ["", f"### {title}", *(f"- {item}" for item in items)]
    if qa is not None:
        sections += ["", *qa.render()]
    prose = re.sub(r"^(#{1,5}) ", r"#\1 ", context.body.strip(), flags=re.MULTILINE)
    text = "\n".join(
        [
            f"**Brief for {_ticket_name(issue)}: {' '.join(issue.title.split())[:180]}** (factory feeder)",
            "",
            f"- Goal: {' '.join(issue.title.split())[:180]}",
            f"- Ticket: {issue.url or _ticket_name(issue)}",
            f"- PR: #{number} on branch `{branch}` off `{base}`{stacked}",
            f"- Risk: {risk} (from the ticket's {source or 'intake record'})",
            *([f"- Type: {template.ticket_type}"] if template.ticket_type else []),
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
