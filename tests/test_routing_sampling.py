import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import account_doctor, lane_limit, usage_readings
from lib.dispatch.helpers import _route_go, _ticket_facts
from lib.dispatcher import CIResult, Dispatcher
from lib.factory_md import FactoryRouting
from lib.machines_yml import Account, Device, MachinesConfig
from lib.orch_yml import Project
from lib.queue import JobRequest, SQLiteJobQueue
from lib.role_router import (
    Draw,
    GuardFacts,
    Hardness,
    HarnessShares,
    HeadroomReading,
    Sample,
    TicketFacts,
    central_routing,
    sample_json,
)
from tests.test_dispatcher import (
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    factory,
    ticket,
    track,
)
from tests.test_dispatcher import setup as unconfigured_setup

HEAD = "a" * 40
ROUTED = replace(factory(), routing=FactoryRouting(budget="medium"))


def _go(extra: str = "") -> str:
    return (
        "<!-- orch:v1 kind=go from=grok-bot role=orchestrator "
        f"state=building {extra}-->\nGo.\n"
    )


def _table(*, build: float, review: float):
    shipped = central_routing()
    roles = dict(shipped.roles)
    roles["review"] = tuple(sorted(roles["review"], key=lambda c: c.harness == "gemini"))
    return replace(shipped, roles=roles, shares={"gemini": HarnessShares(build=build, review=review)})


NOW = 1_800_000_000.0
BOX = Device("box", 4, {"claude": "claude-bot", "codex": "codex-bot", "gemini": "gemini-bot"}, local=True)
ONE_GEMINI_DEVICE = MachinesConfig(devices=(BOX,))


def setup(
    tmp_path: Path,
    config: MachinesConfig = ONE_GEMINI_DEVICE,
    projects: tuple[Project, ...] = (),
) -> tuple[Dispatcher, SQLiteJobQueue]:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: NOW)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        projects=projects,
        devices=SimpleNamespace(config=config, reconcile=lambda *_: None),
    )
    return dispatcher, queue


@pytest.fixture
def guard_reads(monkeypatch: pytest.MonkeyPatch) -> list[GuardFacts]:
    reads: list[GuardFacts] = []
    real = Dispatcher._gemini_guards

    def counted(self: Dispatcher, ticket: object) -> Callable[[], GuardFacts] | None:
        reader = real(self, ticket)
        if reader is None:
            return None

        def read() -> GuardFacts:
            reads.append(reader())
            return reads[-1]

        return read

    monkeypatch.setattr(Dispatcher, "_gemini_guards", counted)
    return reads


def _shares(monkeypatch: pytest.MonkeyPatch, *, build: float, review: float) -> None:
    table = _table(build=build, review=review)
    monkeypatch.setattr("lib.dispatch.helpers.central_routing", lambda: table)


def _routing(queue: SQLiteJobQueue, action: str) -> dict:
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT detail FROM jobs WHERE action = ? ORDER BY created_at, rowid", (action,)
        ).fetchall()
    assert len(rows) == 1, action
    return json.loads(rows[0]["detail"])["routing"]


def _draw(role: str, *, picked: bool) -> Draw:
    return Draw(
        role=role,
        harness="gemini",
        seed=f"owner/repo#42:{role}",
        draw=0.1,
        share=0.25,
        chance=0.5 if picked else 0.05,
        picked=picked,
        skipped_reason=None,
        hardness=Hardness("high", False, 0, None, 1.25) if role == "build" else None,
    )


def _seed_route(queue: SQLiteJobQueue, routing: dict) -> None:
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="build",
            recipient=str(routing["builder"]),
            source_comment_id="go-seed",
            timeout_seconds=5400,
            detail=json.dumps({"routing": routing}),
        ),
        dedupe_key="seed-route:g1",
    )


def test_the_sample_rides_the_build_review_and_fix_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts]
) -> None:
    _shares(monkeypatch, build=0.001, review=0.25)
    dispatcher, queue = setup(tmp_path)
    go = comment("go-1", _go(), author="grok-login")
    dispatcher.process(ticket(go, factory_config=ROUTED))

    build = queue.claim("claude")
    assert build is not None and build.request.action == "build"
    routing = json.loads(build.request.detail)["routing"]
    sample = routing["sample"]
    assert sorted(sample) == ["build", "review"]
    assert sample["build"]["seed"] == "owner/repo#42:build"
    assert sample["review"]["seed"] == "owner/repo#42:review"
    assert (routing["builder"], routing["reviewer"]) == ("claude", "codex")
    queue.finish(build.id, owner="claude", handoff_comment_id="ready-1")

    ready = comment(
        "ready-1",
        f"<!-- orch:v1 kind=ready from=claude-bot role=builder head={HEAD} -->\nReady.\n",
        author="claude-login",
        created_at=2,
    )
    dispatcher.process(ticket(go, ready, head=HEAD, ci={HEAD: CIResult("success")}, factory_config=ROUTED))
    review = queue.claim("codex")
    assert review is not None and review.request.action == "review"
    assert json.loads(review.request.detail)["routing"]["sample"] == sample
    queue.finish(review.id, owner="codex", handoff_comment_id="verdict-1")

    verdict = comment(
        "verdict-1",
        "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
        f"round=1/3 head={HEAD} verdict=REVISE risk=high -->\nRevise.\n",
        author="codex-login",
        created_at=3,
    )
    dispatcher.process(
        ticket(go, ready, verdict, head=HEAD, ci={HEAD: CIResult("success")}, factory_config=ROUTED)
    )
    fix = queue.claim("claude")
    assert fix is not None and fix.request.action == "fix"
    assert json.loads(fix.request.detail)["routing"]["sample"] == sample


def test_an_unnamed_go_replays_the_stored_sample_and_reads_no_guard_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts]
) -> None:
    _shares(monkeypatch, build=0.25, review=0.25)
    dispatcher, queue = setup(tmp_path)
    go = comment("go-1", _go(), author="grok-login")
    dispatcher.process(ticket(go, factory_config=ROUTED))
    stored = _routing(queue, "build")
    assert stored["builder"] == "gemini" and stored["sample"]["build"]["picked"] is True
    reads = len(guard_reads)

    routed = dispatcher._routed(ticket(go, factory_config=ROUTED))
    again = _route_go(routed, {"kind": "go", "state": "building"})

    assert again is not None and again.router_picks is not None
    assert again.factory.builder_harness == "gemini"
    assert sample_json(again.router_picks.sample) == stored["sample"]
    second = comment("go-2", _go(), author="grok-login", created_at=5)
    dispatcher.process(ticket(go, second, factory_config=ROUTED))
    assert len(guard_reads) == reads


def test_a_stored_route_without_a_sample_stays_undrawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts]
) -> None:
    _shares(monkeypatch, build=1.0, review=1.0)
    dispatcher, queue = setup(tmp_path)
    _seed_route(queue, {"builder": "claude", "reviewer": "codex", "builder_model": "claude-opus-5-5"})
    go = comment("go-1", _go(), author="grok-login")

    routed = dispatcher._routed(ticket(go, factory_config=ROUTED))
    again = _route_go(routed, {"kind": "go", "state": "building"})

    assert routed.router_picks is not None and routed.router_picks.sample is None
    assert again is not None and again.router_picks is not None
    assert again.router_picks.sample is None
    assert again.factory.builder_harness == "claude"
    dispatcher.process(ticket(go, factory_config=ROUTED))
    assert guard_reads == []


def test_the_refused_reviewer_re_pick_replays_the_stored_review_draw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts]
) -> None:
    _shares(monkeypatch, build=0.25, review=0.25)
    dispatcher, queue = setup(tmp_path)
    dispatcher.refused_harnesses = frozenset({"codex"})
    sample = Sample(build=_draw("build", picked=False), review=_draw("review", picked=True))
    _seed_route(
        queue,
        {
            "builder": "claude",
            "builder_model": "claude-opus-5-5",
            "reviewer": "codex",
            "reviewer_model": "gpt-6.2-sol",
            "sample": sample_json(sample),
        },
    )

    routed = dispatcher._routed(ticket(factory_config=ROUTED))

    assert routed.factory.reviewer_harness == "gemini"
    assert routed.router_picks is not None and routed.router_picks.sample == sample
    assert guard_reads == []


@pytest.mark.parametrize("named", ["builder=claude ", "reviewer=codex "])
def test_a_go_naming_a_side_makes_no_draw_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts], named: str
) -> None:
    _shares(monkeypatch, build=1.0, review=1.0)
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(ticket(comment("go-1", _go(named), author="grok-login"), factory_config=ROUTED))

    routing = _routing(queue, "build")
    assert (routing["builder"], routing["reviewer"]) == ("claude", "codex")
    assert "sample" not in routing


_FEEDER_BRIEF = """<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=high -->
**Brief for #7: Example** (factory feeder)

- Goal: Example
- Ticket: https://github.com/owner/repo/issues/7
- PR: #42 on branch `factory/issue-7` off `main`
- Risk: high (from the ticket's Risk line)
{type_line}- Start: building (the ticket names 3 files or fewer, so it starts building)
- Builder: claude; reviewer: codex

### Acceptance criteria
- ac-1: it works.

### Ticket
````
Risk: high
{ticket_lines}Depends on: none

```
Touches: fenced/inside.py
```

## Acceptance criteria

- ac-1: it works.
````

### Notes about this repo (from factory.md at `main`)
Plan first: yes
Touches: prose/a.py, prose/b.py, prose/c.py, prose/d.py
"""


def _brief_ticket(*, type_line: str, ticket_lines: str, issue_body: str):
    body = _FEEDER_BRIEF.format(type_line=type_line, ticket_lines=ticket_lines)
    brief = comment("brief-1", body, author="grok-login", created_at=0)
    return replace(ticket(brief, risk="low"), issue_body=issue_body)


def test_ticket_facts_come_from_the_trusted_brief_and_its_ticket_block() -> None:
    editable = "Plan first: yes\nTouches: issue/a.py, issue/b.py, issue/c.py, issue/d.py\n- Type: bug-fix\n"
    full = _brief_ticket(
        type_line="- Type: perf-issue\n",
        ticket_lines="Plan first: yes\nTouches: a.py, b.py, ./a.py, c.py, d.py\n",
        issue_body=editable,
    )
    bare = _brief_ticket(type_line="", ticket_lines="", issue_body=editable)

    assert _ticket_facts(full) == TicketFacts(
        repo="owner/repo",
        ticket="42",
        risk="low",
        plan_first=True,
        touches=("a.py", "b.py", "c.py", "d.py"),
        ticket_type="perf-issue",
    )
    assert _ticket_facts(bare) == TicketFacts(
        repo="owner/repo", ticket="42", risk="low", plan_first=False, touches=(), ticket_type=None
    )
    assert _ticket_facts(replace(full, brief_comment=None)) == TicketFacts(
        repo="owner/repo", ticket="42", risk="low", plan_first=False, touches=(), ticket_type=None
    )


def _queued_build_detail(path: Path, go_body: str) -> dict:
    path.mkdir()
    dispatcher, queue = setup(path)
    dispatcher.process(ticket(comment("go-1", go_body, author="grok-login")))
    build = queue.claim("claude")
    assert build is not None and build.request.action == "build"
    return json.loads(build.request.detail)


def test_a_legacy_pair_repo_queues_the_same_detail_with_shares_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts]
) -> None:
    today = _queued_build_detail(tmp_path / "today", _go())
    _shares(monkeypatch, build=1.0, review=1.0)

    shared = _queued_build_detail(tmp_path / "shared", _go())

    assert shared == today
    assert "routing" not in shared
    assert guard_reads == []


def _outcome(queue: SQLiteJobQueue, comment_id: str) -> str:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT outcome FROM handled_comments WHERE comment_id = ?", (comment_id,)
        ).fetchone()
    return str(row["outcome"])


def _ready_comment() -> object:
    return comment(
        "ready-1",
        f"<!-- orch:v1 kind=ready from=claude-bot role=builder head={HEAD} -->\nReady.\n",
        author="claude-login",
        created_at=2,
    )


def _outside(*comments: object) -> object:
    return replace(
        ticket(*comments, head=HEAD, ci={HEAD: CIResult("success")}, factory_config=ROUTED),
        ready_starts_review=True,
    )


@pytest.mark.parametrize("build_share", [0.0, 0.25, 1.0])
def test_a_ticket_built_outside_the_queue_starts_review_on_its_builders_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts], build_share: float
) -> None:
    _shares(monkeypatch, build=build_share, review=0.0)
    dispatcher, queue = setup(tmp_path)

    dispatcher.process(_outside(_ready_comment()))

    assert _outcome(queue, "ready-1") == "queued-review"
    routing = _routing(queue, "review")
    assert (routing["builder"], routing["reviewer"]) == ("claude", "codex")
    assert "build" not in routing.get("sample", {})


def test_a_route_stored_on_a_review_is_replayed_by_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts]
) -> None:
    _shares(monkeypatch, build=0.0, review=1.0)
    dispatcher, queue = setup(tmp_path)
    ready = _ready_comment()
    dispatcher.refused_harnesses = frozenset({"gemini"})
    dispatcher.process(_outside(ready))
    review = _routing(queue, "review")
    assert review["reviewer"] == "codex"
    assert review["sample"]["review"]["skipped_reason"] == "refused"
    job = queue.claim("codex")
    assert job is not None
    queue.finish(job.id, owner="codex", handoff_comment_id="verdict-1")
    reads = len(guard_reads)

    dispatcher.refused_harnesses = frozenset()
    verdict = comment(
        "verdict-1",
        "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
        f"round=1/3 head={HEAD} verdict=REVISE risk=high -->\nRevise.\n",
        author="codex-login",
        created_at=3,
    )
    dispatcher.process(_outside(ready, verdict))

    fix = _routing(queue, "fix")
    assert (fix["builder"], fix["reviewer"]) == ("claude", "codex")
    assert fix["sample"] == review["sample"]
    assert len(guard_reads) == reads


def test_a_factory_md_slots_pool_takes_no_draw_and_leaves_the_review_chance_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts]
) -> None:
    _shares(monkeypatch, build=0.5, review=0.25)
    pinned = replace(
        factory(),
        routing=FactoryRouting(budget="medium", slots={"build": (("claude", "claude-opus-5-5", "medium"),)}),
    )
    dispatcher, queue = setup(tmp_path)

    dispatcher.process(ticket(comment("go-1", _go(), author="grok-login"), factory_config=pinned))

    routing = _routing(queue, "build")
    assert routing["builder"] == "claude"
    assert "build" not in routing["sample"]
    assert routing["sample"]["review"]["chance"] == 0.25


@pytest.mark.parametrize(
    ("ci", "job", "action"), [("success", "codex", "review"), ("failure", "claude", "fix")]
)
def test_a_ready_on_a_newer_head_keeps_the_outside_queue_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ci: str, job: str, action: str
) -> None:
    _shares(monkeypatch, build=1.0, review=0.0)
    dispatcher, queue = setup(tmp_path)
    ready = _ready_comment()
    newer = replace(
        ticket(ready, head=HEAD, ci={HEAD: CIResult(ci)}, factory_config=ROUTED),
        ready_starts_review=True,
    )
    track(queue)
    dispatcher._fresh_reader = lambda ref: newer
    stale = replace(newer, current_head="b" * 40, ci={})

    dispatcher.process(stale)

    assert _outcome(queue, "ready-1") == f"queued-{'review' if ci == 'success' else 'ci-fix'}"
    routing = _routing(queue, action)
    assert (routing["builder"], routing["reviewer"]) == ("claude", "codex")
    assert "build" not in routing.get("sample", {})
    assert queue.claim(job) is not None


GEMINI_A = Account("gemini-a", "gemini")
GEMINI_B = Account("gemini-b", "gemini", signed_in_on=frozenset({"box"}))
TWO_GEMINI_ACCOUNTS = MachinesConfig(
    devices=(replace(BOX, accounts={"gemini": "gemini-a"}),), accounts=(GEMINI_A, GEMINI_B)
)
GEMINI_FAR_FROM_THE_PIN = MachinesConfig(
    devices=(
        Device("box", 4, {"claude": "claude-bot", "codex": "codex-bot"}, local=True),
        Device("far", 4, {"gemini": "gemini-bot"}),
    )
)


def _read_usage(queue: SQLiteJobQueue, account: str, used: float) -> None:
    with queue.connect() as connection:
        connection.execute(usage_readings.ACCOUNT_READINGS_DDL)
        for window in usage_readings.WINDOWS:
            connection.execute(
                usage_readings.ACCOUNT_READINGS_UPSERT,
                (account, "box", window, "gemini", "gemini-bot", used, int(NOW) + 3600, NOW - 60, "probe", 0, NOW - 60),
            )


def _limit(queue: SQLiteJobQueue, account: str) -> None:
    with queue.connect() as connection:
        connection.execute(lane_limit.ACCOUNT_DDL)
        connection.execute(
            f"INSERT INTO {lane_limit.ACCOUNT_TABLE}"
            " (account, harness, bot, recipient, limited_at, until, source, job_id)"
            " VALUES (?, 'gemini', 'gemini-bot', 'gemini', ?, ?, 'event', 'job-limited')",
            (account, NOW - 60, int(NOW) + 3600),
        )


def _lane_limit(queue: SQLiteJobQueue) -> None:
    with queue.connect() as connection:
        connection.execute(lane_limit.DDL)
        connection.execute(
            f"INSERT INTO {lane_limit.TABLE} (recipient, harness, bot, limited_at, until, source, job_id)"
            " VALUES ('gemini', 'gemini', 'gemini-bot', ?, ?, 'event', 'job-limited')",
            (NOW - 60, int(NOW) + 3600),
        )


def _sign_in_hold(queue: SQLiteJobQueue) -> None:
    with queue.connect() as connection:
        account_doctor.hold(connection, "gemini-default", "box", "runtime", "not logged in", NOW - 60)


def _sign_out_lock(queue: SQLiteJobQueue) -> None:
    with queue.connect() as connection:
        assert account_doctor.reserve_pair(connection, "gemini-default", "box", "signout-1", NOW - 60)


def _gemini_job(
    queue: SQLiteJobQueue, ticket_id: str, action: str, *, repo: str = "owner/repo", state: str = "queued"
) -> None:
    job = queue.enqueue(
        JobRequest(repo, ticket_id, action, "gemini", f"go-{ticket_id}-{action}", 5400),
        dedupe_key=f"gemini:{repo}#{ticket_id}:{action}",
    )
    with queue.connect() as connection:
        connection.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job.id))


def _builds_in_flight(queue: SQLiteJobQueue) -> None:
    _gemini_job(queue, "7", "plan")
    _gemini_job(queue, "7", "build", state="running")
    _gemini_job(queue, "8", "fix", state="running")
    _gemini_job(queue, "9", "review")
    _gemini_job(queue, "10", "build", state="done")


def _five_and_seven_day(headroom: float) -> tuple[HeadroomReading, ...]:
    return tuple(HeadroomReading(window, headroom, NOW - 60) for window in usage_readings.WINDOWS)


def _no_seed(queue: SQLiteJobQueue) -> None:
    return None


@dataclass(frozen=True)
class Seed:
    name: str
    facts: GuardFacts
    skipped_reason: str | None
    write: Callable[[SQLiteJobQueue], None] = _no_seed
    config: MachinesConfig = ONE_GEMINI_DEVICE
    projects: tuple[Project, ...] = ()
    refused: frozenset[str] = frozenset()


SEEDS = (
    Seed("open", GuardFacts(), None),
    Seed("refused", GuardFacts(refused=True), "refused", refused=frozenset({"gemini"})),
    Seed("sign-in-hold", GuardFacts(signed_out=True), "signed-out", _sign_in_hold),
    Seed("sign-out-lock", GuardFacts(signed_out=True), "signed-out", _sign_out_lock),
    Seed(
        "outside-the-pins",
        GuardFacts(signed_out=True),
        "signed-out",
        config=GEMINI_FAR_FROM_THE_PIN,
        projects=(Project("owner/repo", devices=frozenset({"box"})),),
    ),
    Seed("usage-limit", GuardFacts(usage_held=True), "usage-held", lambda q: _limit(q, "gemini-default")),
    Seed("lane-wide-limit", GuardFacts(usage_held=True), "usage-held", _lane_limit),
    Seed("headroom-floor", GuardFacts(usage_held=True), "usage-held", lambda q: _read_usage(q, "gemini-default", 0.97)),
    Seed(
        "paused",
        GuardFacts(usage_held=True),
        "usage-held",
        config=replace(ONE_GEMINI_DEVICE, accounts=(Account("gemini-default", "gemini", paused=True),)),
    ),
    Seed(
        "low-headroom",
        GuardFacts(readings=_five_and_seven_day(0.1)),
        "low-headroom",
        lambda q: _read_usage(q, "gemini-default", 0.9),
    ),
    Seed("builds-in-flight", GuardFacts(builds_in_flight=2), "builds-in-flight", _builds_in_flight),
    Seed(
        "one-of-two-accounts-held",
        GuardFacts(readings=_five_and_seven_day(0.7)),
        None,
        lambda q: (_limit(q, "gemini-a"), _read_usage(q, "gemini-b", 0.3)),
        config=TWO_GEMINI_ACCOUNTS,
    ),
    Seed(
        "most-headroom-of-two-accounts",
        GuardFacts(readings=_five_and_seven_day(0.2)),
        None,
        lambda q: (_read_usage(q, "gemini-a", 0.8), _read_usage(q, "gemini-b", 0.86)),
        config=TWO_GEMINI_ACCOUNTS,
    ),
)


def _seeded(tmp_path: Path, seed: Seed) -> tuple[Dispatcher, SQLiteJobQueue]:
    dispatcher, queue = setup(tmp_path, seed.config, seed.projects)
    dispatcher.refused_harnesses = seed.refused
    seed.write(queue)
    return dispatcher, queue


@pytest.mark.parametrize("seed", SEEDS, ids=[seed.name for seed in SEEDS])
def test_the_reader_reads_each_gemini_guard_fact_from_the_database_and_config(
    tmp_path: Path, seed: Seed
) -> None:
    dispatcher, _ = _seeded(tmp_path, seed)

    reader = dispatcher._gemini_guards(ticket(factory_config=ROUTED))

    assert reader is not None
    assert reader() == seed.facts


@pytest.mark.parametrize("seed", SEEDS, ids=[seed.name for seed in SEEDS])
def test_a_go_whose_build_draw_would_pick_gemini_records_the_seeded_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts], seed: Seed
) -> None:
    _shares(monkeypatch, build=1.0, review=0.0)
    dispatcher, queue = _seeded(tmp_path, seed)

    dispatcher.process(ticket(comment("go-1", _go(), author="grok-login"), factory_config=ROUTED))

    assert guard_reads == [seed.facts]
    with queue.connect() as connection:
        (detail,) = connection.execute(
            "SELECT detail FROM jobs WHERE ticket_id = '42' AND action = 'build'"
        ).fetchone()
    routing = json.loads(detail)["routing"]
    build = routing["sample"]["build"]
    assert build["skipped_reason"] == seed.skipped_reason
    assert build["picked"] is (seed.skipped_reason is None)
    expected = ("gemini", "codex") if seed.skipped_reason is None else ("claude", "codex")
    assert (routing["builder"], routing["reviewer"]) == expected


def test_builds_in_flight_leave_out_this_tickets_own_gemini_jobs(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    _gemini_job(queue, "42", "build")
    _gemini_job(queue, "42", "plan", repo="other/repo")

    reader = dispatcher._gemini_guards(ticket(factory_config=ROUTED))

    assert reader is not None and reader() == GuardFacts(builds_in_flight=1)


def test_a_dispatcher_with_no_machines_config_makes_no_draw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[GuardFacts]
) -> None:
    _shares(monkeypatch, build=1.0, review=1.0)
    dispatcher, queue = unconfigured_setup(tmp_path)

    assert dispatcher._gemini_guards(ticket(factory_config=ROUTED)) is None
    dispatcher.process(ticket(comment("go-1", _go(), author="grok-login"), factory_config=ROUTED))

    routing = _routing(queue, "build")
    assert (routing["builder"], routing["reviewer"]) == ("claude", "codex")
    assert "sample" not in routing
    assert guard_reads == []
