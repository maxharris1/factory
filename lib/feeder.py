"""Backlog feeder: tagged tickets become queued jobs by themselves (issue #85).

Each poll, before discovery, the feeder reads every project whose orch.yml
entry has an `intake` section and finds its eligible tickets:

* a GitHub project (factory.md `tickets: github`): an open issue carrying
  the project's intake label, applied by a trusted login;
* a Linear project: an issue in the project's intake workflow state, moved
  there by a trusted Linear user.

The signal (label or state) and the trusted appliers are per-project
config (lib/orch_yml.py Intake). Nothing here names a project, a label or a
state. Trusted appliers default to the keys of `trusted_logins` (GitHub) or
`trusted_linear_users` (Linear).

For each eligible ticket, in priority order (urgent, then bug, then oldest)
and only while its builder harness has room, the feeder:

1. opens a branch from the base branch with an empty start commit (a GitHub
   issue gets `factory/issue-<n>`; a Linear issue uses its own
   gitBranchName);
2. opens a draft PR whose body says "Fixes <ticket>";
3. on a GitHub-ticket repo only, labels the PR `factory` and
   `orch:state/<building|planning>` (a Linear repo gets no GitHub labels);
4. posts a `brief` handoff rendered from the ticket body plus the repo's
   factory.md prose, then a `go` handoff (state=planning when the ticket
   names more than 3 files, else building; risk=high on a `risk:high`
   label, else low), both as the orchestrator. On a Linear project both go
   on the Linear issue, in the visible header form.

It is idempotent, keyed on (repo, ticket): `feeder_items` records each step
(branch, PR, comment ids) before the next, a PR already open on the branch
is reused, and a brief or go already on the ticket is not posted again. A
ticket that was fed is never fed again, so a re-poll never opens a second PR.

Room for a harness h is the free slots on every schedulable device that
lists h (machines.yml, minus held slots), minus the jobs queued for h,
minus the tickets this feeder fed for h whose job is not queued yet. So the
queue never holds more work for a harness than it has free slots.

The feeder starts no harness and runs no job. It writes only through the
GitHub and Linear clients it is given; tests pass fakes.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from lib.device_pool import free_slots
from lib.dispatcher import Comment, TicketRef, safe_ship_error
from lib.factory_md import FactoryContext
from lib.handoff import Handoff, HandoffError, format_comment, parse_comment
from lib.machines_yml import MachinesConfig
from lib.orch_yml import OrchConfig, Project
from lib.queue import (
    PRIORITY_TABLE,
    SLOTS_TABLE,
    SQLiteJobQueue,
    _has_table,
    priority_rank,
    set_ticket_priority,
)

FEEDER_TABLE = "feeder_items"
# eligible: seen this poll, not fed yet; waiting-capacity: no room for its
# harness this poll; feeding: some steps done; error: a step failed (retried
# next poll); fed: brief and go posted; done: fed and its job is queued.
STATES = ("eligible", "waiting-capacity", "feeding", "error", "fed", "done")
IN_FLIGHT = ("feeding", "error")
FED = ("fed", "done")
ORCHESTRATOR = "grok-bot"
FACTORY_LABEL = "factory"
URGENT_LABELS = frozenset({"urgent", "priority:urgent"})
BUG_LABELS = frozenset({"bug"})
RISK_HIGH_LABEL = "risk:high"
LINEAR_URGENT_PRIORITY = 1
PLANNING_FILE_COUNT = 3  # SPEC section 4.1 (D36): over 3 files starts with a plan
BODY_LIMIT = 20_000

_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
# A file path an issue names: a dotted file name, optionally under folders.
_FILE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:py|md|mmd|yml|yaml|json|toml|ini|cfg|txt|"
    r"sh|sql|ts|tsx|js|jsx|mjs|css|scss|html|go|rs|rb|java|kt|swift|tla|svg))(?![\w/-])"
)

Log = Callable[[str], None]


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

    @property
    def urgent(self) -> bool:
        return (
            any(label.lower() in URGENT_LABELS for label in self.labels)
            or self.priority == LINEAR_URGENT_PRIORITY
        )

    @property
    def bug(self) -> bool:
        return any(label.lower() in BUG_LABELS for label in self.labels)

    @property
    def risk(self) -> str:
        return "high" if any(label.lower() == RISK_HIGH_LABEL for label in self.labels) else "low"


class GitHubIntake(Protocol):
    """GitHub reads and writes the feeder needs. Tests pass a fake."""

    def github_intake(self, repo: str, label: str) -> Sequence[IntakeIssue]: ...

    def branch_sha(self, repo: str, branch: str) -> str | None: ...

    def create_start_commit(self, repo: str, base_sha: str, message: str) -> str: ...

    def create_branch(self, repo: str, branch: str, sha: str) -> None: ...

    def find_pull(self, repo: str, branch: str) -> int | None: ...

    def open_draft_pull(
        self, repo: str, *, branch: str, base: str, title: str, body: str
    ) -> int: ...

    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> None: ...

    def comments(self, repo: str, number: int) -> Sequence[Comment]: ...

    def post_comment(self, repo: str, number: int, body: str) -> str: ...


class LinearIntake(Protocol):
    def linear_intake(self, project_id: str, state: str) -> Sequence[IntakeIssue]: ...

    def linear_comments(self, issue_id: str) -> Sequence[Comment]: ...

    def post_linear_comment(self, issue_id: str, body: str) -> str: ...


@dataclass(frozen=True)
class FeedReport:
    fed: tuple[TicketRef, ...] = ()
    eligible: int = 0
    waiting: int = 0
    errors: int = 0


@dataclass
class _Candidate:
    project: Project
    context: FactoryContext
    issue: IntakeIssue

    @property
    def harness(self) -> str:
        return self.context.config.builder_harness

    def order(self) -> tuple[int, float, str]:
        rank = priority_rank(urgent=self.issue.urgent, resume=False, bug=self.issue.bug)
        return rank, self.issue.created_at, self.issue.key


def go_state_for(body: str) -> str:
    """planning when the ticket names more than 3 distinct files, else building."""
    files = {match.group(1) for match in _FILE.finditer(body or "")}
    return "planning" if len(files) > PLANNING_FILE_COUNT else "building"


def intake_trusted(config: OrchConfig, project: Project) -> frozenset[str]:
    """Who may apply a project's intake signal."""
    intake = project.intake
    if intake is not None and intake.trusted is not None:
        return intake.trusted
    if project.linear_project_id is not None:
        # orch.yml keys Linear users as "linear:<user id>".
        return frozenset(key.removeprefix("linear:") for key in config.trusted_linear_users)
    return frozenset(config.trusted_logins)


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
            PRIMARY KEY (repo, issue)
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PRIORITY_TABLE} (
            repo TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            urgent INTEGER NOT NULL DEFAULT 0,
            bug INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (repo, ticket_id)
        )
        """
    )


def feeder_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """eligible, fed and waiting_capacity among tickets eligible at the last poll."""
    counts = {"eligible": 0, "fed": 0, "waiting_capacity": 0}
    for row in feeder_items(connection):
        counts["eligible"] += 1
        if row["state"] in FED:
            counts["fed"] += 1
        elif row["state"] == "waiting-capacity":
            counts["waiting_capacity"] += 1
    return counts


def feeder_items(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Tickets eligible at the last poll, priority order, as plain rows."""
    if not _has_table(connection, FEEDER_TABLE):
        return []
    last = connection.execute(f"SELECT MAX(seen_at) FROM {FEEDER_TABLE}").fetchone()[0]
    if last is None:
        return []
    cursor = connection.execute(
        f"""
        SELECT repo, issue, tracker, title, harness, state, urgent, bug,
               pr_number, issue_created_at, error
        FROM {FEEDER_TABLE} WHERE seen_at = ?
        """,
        (last,),
    )
    names = [column[0] for column in cursor.description]
    items = [dict(zip(names, tuple(row))) for row in cursor.fetchall()]
    items.sort(
        key=lambda row: (
            priority_rank(urgent=bool(row["urgent"]), resume=False, bug=bool(row["bug"])),
            float(row["issue_created_at"]),
            str(row["repo"]),
            str(row["issue"]),
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
        with self.queue.connect() as connection:
            ensure_tables(connection)

    @property
    def projects(self) -> tuple[Project, ...]:
        return tuple(project for project in self.config.projects if project.intake is not None)

    # -- one poll ------------------------------------------------------------

    def feed_once(self) -> FeedReport:
        now = self.queue.now()
        candidates, read_errors = self._candidates()
        self._record_seen(candidates, now)
        self._mark_done()
        room = self._room({candidate.harness for candidate in candidates})
        fed: list[TicketRef] = []
        waiting = 0
        errors = read_errors
        resumed = {(c.issue.repo.lower(), c.issue.key) for c in candidates if self._in_flight(c)}
        for candidate in sorted(candidates, key=lambda item: (
            (item.issue.repo.lower(), item.issue.key) not in resumed, *item.order()
        )):
            identity = (candidate.issue.repo.lower(), candidate.issue.key)
            state = self._state(candidate)
            if state in FED:
                continue
            if identity not in resumed:
                if room.get(candidate.harness, 0) <= 0:
                    self._set_state(candidate, "waiting-capacity")
                    waiting += 1
                    continue
                room[candidate.harness] = room.get(candidate.harness, 0) - 1
            try:
                ref = self._feed(candidate, now)
            except Exception as exc:  # one ticket's failure never stops the poll
                errors += 1
                self._set_state(candidate, "error", error=safe_ship_error(exc))
                self.log(
                    f"feeder: {candidate.issue.repo}#{candidate.issue.key} "
                    f"not fed yet: {safe_ship_error(exc)}"
                )
                continue
            fed.append(ref)
            self.log(f"feeder: fed {candidate.issue.repo}#{candidate.issue.key} as {ref.ticket_id}")
        return FeedReport(fed=tuple(fed), eligible=len(candidates), waiting=waiting, errors=errors)

    def _candidates(self) -> tuple[list[_Candidate], int]:
        found: list[_Candidate] = []
        errors = 0
        for project in self.projects:
            intake = project.intake
            assert intake is not None
            try:
                context = self.factory_for(project)
                if project.linear_project_id is not None:
                    if self.linear is None or intake.linear_state is None:
                        continue
                    issues = self.linear.linear_intake(project.linear_project_id, intake.linear_state)
                else:
                    if context.config.tickets != "github" or intake.label is None:
                        continue
                    issues = self.github.github_intake(project.repo, intake.label)
            except Exception as exc:
                errors += 1
                self.log(f"feeder: cannot read intake for {project.repo}: {safe_ship_error(exc)}")
                continue
            trusted = intake_trusted(self.config, project)
            for issue in issues:
                if issue.repo.lower() != project.repo.lower() or not _KEY.fullmatch(issue.key):
                    continue
                if issue.applied_by is None or issue.applied_by not in trusted:
                    continue
                found.append(_Candidate(project=project, context=context, issue=issue))
        return found, errors

    # -- capacity --------------------------------------------------------------

    def _room(self, harnesses: set[str]) -> dict[str, int]:
        room: dict[str, int] = {}
        with self.queue.connect() as connection:
            used: dict[str, int] = {}
            if _has_table(connection, SLOTS_TABLE):
                used = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        f"SELECT device, COUNT(*) FROM {SLOTS_TABLE} GROUP BY device"
                    )
                }
            for harness in harnesses:
                free = free_slots(self.machines, harness, used) if self.machines else 0
                queued = connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state = 'queued' AND recipient = ?",
                    (harness,),
                ).fetchone()[0]
                pending = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {FEEDER_TABLE} AS item
                    WHERE item.harness = ? AND item.state IN ('feeding', 'error', 'fed')
                      AND NOT EXISTS (
                        SELECT 1 FROM jobs
                        WHERE lower(jobs.repo) = item.repo
                          AND jobs.ticket_id = item.ticket_id
                      )
                    """,
                    (harness,),
                ).fetchone()[0]
                room[harness] = free - int(queued) - int(pending)
        return room

    # -- bookkeeping -------------------------------------------------------------

    def _record_seen(self, candidates: Sequence[_Candidate], now: float) -> None:
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for candidate in candidates:
                issue = candidate.issue
                connection.execute(
                    f"""
                    INSERT INTO {FEEDER_TABLE} (
                        repo, issue, tracker, title, harness, state, urgent, bug,
                        risk, issue_created_at, first_seen_at, seen_at
                    ) VALUES (?, ?, ?, ?, ?, 'eligible', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo, issue) DO UPDATE SET
                        title = excluded.title,
                        urgent = excluded.urgent,
                        bug = excluded.bug,
                        risk = CASE WHEN state IN ('fed', 'done') THEN risk ELSE excluded.risk END,
                        harness = CASE WHEN state IN ('fed', 'done') THEN harness
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
                        issue.risk,
                        issue.created_at,
                        now,
                        now,
                    ),
                )
            # A ticket that lost its signal before it was fed is forgotten; a
            # fed one is kept forever, so it is never fed (or opened) twice.
            connection.execute(
                f"""
                DELETE FROM {FEEDER_TABLE}
                WHERE seen_at < ? AND state IN ('eligible', 'waiting-capacity')
                """,
                (now,),
            )
            connection.commit()

    def _mark_done(self) -> None:
        with self.queue.connect() as connection:
            connection.execute(
                f"""
                UPDATE {FEEDER_TABLE} SET state = 'done'
                WHERE state = 'fed' AND EXISTS (
                    SELECT 1 FROM jobs
                    WHERE lower(jobs.repo) = {FEEDER_TABLE}.repo
                      AND jobs.ticket_id = {FEEDER_TABLE}.ticket_id
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

    def _in_flight(self, candidate: _Candidate) -> bool:
        return self._state(candidate) in IN_FLIGHT

    def _set_state(self, candidate: _Candidate, state: str, *, error: str | None = None) -> None:
        with self.queue.connect() as connection:
            connection.execute(
                f"""
                UPDATE {FEEDER_TABLE} SET state = ?, error = ?
                WHERE repo = ? AND issue = ? AND state NOT IN ('fed', 'done')
                """,
                (state, error, candidate.issue.repo.lower(), candidate.issue.key),
            )

    def _record(self, candidate: _Candidate, **values: object) -> None:
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self.queue.connect() as connection:
            connection.execute(
                f"UPDATE {FEEDER_TABLE} SET {assignments} WHERE repo = ? AND issue = ?",
                (*values.values(), candidate.issue.repo.lower(), candidate.issue.key),
            )

    # -- one ticket ----------------------------------------------------------------

    def _feed(self, candidate: _Candidate, now: float) -> TicketRef:
        issue = candidate.issue
        config = candidate.context.config
        row = self._row(candidate)
        self._set_state(candidate, "feeding")
        risk = str(row["risk"])
        state = go_state_for(issue.body)
        branch = row["branch"] or self._branch_for(issue)
        if not _BRANCH.fullmatch(branch) or ".." in branch:
            raise ValueError(f"branch name {branch!r} is not safe")
        self._record(candidate, branch=branch)
        repo = candidate.project.repo
        number = int(row["pr_number"]) if row["pr_number"] else self.github.find_pull(repo, branch)
        if number is None:
            if self.github.branch_sha(repo, branch) is None:
                base = self.github.branch_sha(repo, config.base_branch)
                if base is None:
                    raise ValueError(f"base branch {config.base_branch} not found")
                start = self.github.create_start_commit(
                    repo, base, f"Start {_ticket_name(issue)}: {issue.title}"[:200]
                )
                self.github.create_branch(repo, branch, start)
            number = self.github.open_draft_pull(
                repo,
                branch=branch,
                base=config.base_branch,
                title=_pull_title(issue),
                body=_pull_body(issue),
            )
        ticket_id = issue.key if issue.tracker == "linear" else str(number)
        self._record(candidate, pr_number=str(number), ticket_id=ticket_id)
        with self.queue.connect() as connection:
            set_ticket_priority(
                connection, repo, ticket_id, urgent=issue.urgent, bug=issue.bug, at=now
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
        brief_id = row["brief_comment_id"] or _existing(existing, "brief")
        if brief_id is None:
            brief_id = post(
                _brief(issue, candidate.context, number=number, branch=branch, risk=risk,
                       state=state, visible=linear)
            )
        self._record(candidate, brief_comment_id=brief_id)
        go_id = row["go_comment_id"] or _existing(existing, "go")
        if go_id is None:
            go_id = post(_go(issue, state=state, risk=risk, number=number, visible=linear))
        self._record(candidate, go_comment_id=go_id, state="fed", fed_at=now, error=None)
        return TicketRef(repo, ticket_id, issue.tracker)

    @staticmethod
    def _branch_for(issue: IntakeIssue) -> str:
        if issue.tracker == "linear":
            if issue.branch and _BRANCH.fullmatch(issue.branch):
                return issue.branch
            return f"factory/{issue.key.lower()}"
        return f"factory/issue-{issue.key}"


def _ticket_name(issue: IntakeIssue) -> str:
    return issue.key if issue.tracker == "linear" else f"#{issue.key}"


def _pull_title(issue: IntakeIssue) -> str:
    title = " ".join(issue.title.split())[:180]
    return f"{issue.key}: {title}" if issue.tracker == "linear" else title


def _pull_body(issue: IntakeIssue) -> str:
    lines = [f"Fixes {_ticket_name(issue)}", ""]
    if issue.url:
        lines.append(f"Ticket: {issue.url}")
    lines.append(
        "Opened by the factory feeder from the ticket's intake signal. The builder "
        "pushes the work here; the brief and go handoffs are on the ticket."
    )
    return "\n".join(lines) + "\n"


def _existing(comments: Sequence[Comment], kind: str) -> str | None:
    """The id of a brief or go the orchestrator already posted, if any."""
    for item in comments:
        try:
            fields = parse_comment(item.body).as_dict()
        except HandoffError:
            continue
        if fields.get("kind") == kind and fields.get("from") == ORCHESTRATOR:
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
    risk: str,
    state: str,
    visible: bool,
) -> str:
    config = context.config
    body = issue.body.strip() or "(the ticket has no body)"
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + f"\n[cut at {BODY_LIMIT} characters]"
    fence = _fence(body)
    why = (
        "the ticket names more than 3 files, so it starts with a plan"
        if state == "planning"
        else "the ticket names 3 files or fewer, so it starts building"
    )
    prose = re.sub(r"^(#{1,5}) ", r"#\1 ", context.body.strip(), flags=re.MULTILINE)
    text = "\n".join(
        [
            f"**Brief for {_ticket_name(issue)}: {' '.join(issue.title.split())[:180]}** (factory feeder)",
            "",
            f"- Goal: {' '.join(issue.title.split())[:180]}",
            f"- Ticket: {issue.url or _ticket_name(issue)}",
            f"- PR: #{number} on branch `{branch}` off `{config.base_branch}`",
            f"- Risk: {risk} ({'a risk:high label' if risk == 'high' else 'no risk:high label'})",
            f"- Start: {state} ({why})",
            f"- Builder: {config.builder_harness}; reviewer: {config.reviewer_harness}",
            "- Questions: the chat that filed this ticket was its questions step. "
            "Post a questions handoff if a product call is still open.",
            "",
            "### Ticket",
            fence,
            body,
            fence,
            "",
            f"### factory.md (from `{config.base_branch}`)",
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
