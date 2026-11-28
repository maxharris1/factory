import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.helpers import _route_go, _ticket_facts
from lib.dispatcher import CIResult, Dispatcher
from lib.factory_md import FactoryRouting
from lib.queue import JobRequest, SQLiteJobQueue
from lib.role_router import (
    Draw,
    GuardFacts,
    Hardness,
    HarnessShares,
    Sample,
    TicketFacts,
    central_routing,
    sample_json,
)
from tests.test_dispatcher import comment, factory, setup, ticket

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


@pytest.fixture
def guard_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    reads: list[int] = []

    def read(self: Dispatcher) -> GuardFacts:
        reads.append(1)
        return GuardFacts()

    monkeypatch.setattr(Dispatcher, "_guard_facts", read)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[int]
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[int]
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[int]
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[int]
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reads: list[int], named: str
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = _queued_build_detail(tmp_path / "today", _go())
    reads: list[int] = []
    monkeypatch.setattr(Dispatcher, "_guard_facts", lambda self: reads.append(1) or GuardFacts())
    _shares(monkeypatch, build=1.0, review=1.0)

    shared = _queued_build_detail(tmp_path / "shared", _go())

    assert shared == today
    assert "routing" not in shared
    assert reads == []
