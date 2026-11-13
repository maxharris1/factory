"""#105: extra rounds, cap enforcement, builder ready headers, the fix-retry guard.

Max's extra round is a recorded fact (ticket_state.cap_extra), raised only by
the trusted Grok go on a capped ticket. Every test drives the real Dispatcher
over a temp database with test_dispatcher's fakes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import CIResult, Comment, TicketRef, TrackedTicket
from lib.handoff import parse_comment
from lib.harness_env import CLAUDE_BG_WAIT_ENV, harness_env
from lib.queue import Job, JobRequest
from tests.test_dispatcher import (
    GO,
    PLAN,
    READY,
    VERDICT,
    FakeSource,
    comment,
    factory,
    handled_outcome,
    job_count,
    setup,
    ticket,
    track,
)
from tests.test_job_state import Poller
from tests.test_remote_start import Ssh, orch

HEADS = [f"{n:x}" * 40 for n in range(1, 10)]
APPROVE = (
    "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
    "round={round} head={head} verdict=APPROVE risk=high -->\nApproved.\n"
)


class Flow:
    """One ticket's comment thread, processed after every post."""

    def __init__(self, tmp_path: Path, *, rounds_high: int = 3):
        self.now = [100.0]
        self.dispatcher, self.queue = setup(tmp_path, self.now)
        track(self.queue)
        self.comments: list[Comment] = []
        self.head = HEADS[0]
        self.ci = "success"
        self.factory = factory(rounds_high=rounds_high)

    def snapshot(self) -> TrackedTicket:
        return ticket(
            *self.comments,
            head=self.head,
            ci={self.head: CIResult(self.ci)},
            factory_config=self.factory,
        )

    def post(self, comment_id: str, body: str, author: str) -> str:
        self.now[0] += 10
        self.comments.append(comment(comment_id, body, author=author, created_at=self.now[0]))
        self.dispatcher.process(self.snapshot())
        return handled_outcome(self.queue, comment_id)

    def go(self, comment_id: str, author: str = "grok-login") -> str:
        return self.post(comment_id, GO.format(state="building"), author)

    def ready(self, head: str) -> str:
        self.head = head
        return self.post(f"ready-{head[:6]}", READY.format(head=head), "claude-login")

    def revise(self, round_text: str) -> str:
        return self.post(
            f"revise-{round_text}",
            VERDICT.format(round=round_text, head=self.head, verdict="REVISE", risk="high"),
            "codex-login",
        )

    def finish(self, recipient: str) -> Job:
        job = self.queue.claim(recipient)
        assert job is not None
        self.queue.finish(job.id, owner=recipient, handoff_comment_id=f"handoff-{job.id}")
        return job

    def state(self) -> sqlite3.Row:
        with self.queue.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ticket_state WHERE repo = 'owner/repo' AND ticket_id = '42'"
            ).fetchone()
        assert row is not None
        return row

    def to_cap(self) -> None:
        """Build, then three REVISE rounds: the ticket ends capped at 3."""
        assert self.go("go-1") == "queued-build"
        self.finish("claude")
        for number in (1, 2, 3):
            assert self.ready(HEADS[number]) == "queued-review"
            assert round_of(self.finish("codex")) == f"{number}/3"
            self.revise(f"{number}/3")
            if number < 3:
                assert round_of(self.finish("claude")) == f"{number + 1}/3"
        assert (self.state()["workflow_state"], self.state()["review_round"]) == ("capped", 3)


def round_of(job: Job) -> str | None:
    return json.loads(job.request.detail or "{}").get("round")


def outcome(queue, comment_id: str) -> str | None:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT outcome FROM handled_comments WHERE comment_id = ?", (comment_id,)
        ).fetchone()
    return None if row is None else str(row["outcome"])


def attention(queue) -> list[str]:
    with queue.connect() as connection:
        return [
            str(row["reason"])
            for row in connection.execute("SELECT reason FROM needs_attention")
        ]


# --- AC 2, AC 5: the extra round is recorded and keyed on the go ---------------------


def test_extra_round_raises_the_cap_once_per_go(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    fixes: list[Job] = []
    for extra, go_id in ((1, "go-2"), (2, "go-3")):
        number = 3 + extra
        assert flow.go(go_id) == "queued-fix"
        assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", extra)
        fix = flow.finish("claude")
        fixes.append(fix)
        assert fix.request.action == "fix"
        assert round_of(fix) == f"{number}/{number}"
        assert json.loads(fix.request.detail or "{}")["resume"] is True
        assert flow.ready(HEADS[number]) == "queued-review"
        assert round_of(flow.finish("codex")) == f"{number}/{number}"
        assert flow.revise(f"{number}/{number}") == "capped-review"
        state = flow.state()
        assert (state["workflow_state"], state["review_round"]) == ("capped", number)
    assert len({fix.id for fix in fixes}) == 2
    assert job_count(flow.queue, "fix") == 4  # rounds 2 and 3, then one per go


def test_replayed_resume_go_queues_no_second_fix(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    assert flow.go("go-2") == "queued-fix"
    with flow.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'go-2'")
    flow.dispatcher.process(flow.snapshot())
    assert handled_outcome(flow.queue, "go-2") == "queued-fix"
    assert job_count(flow.queue, "fix") == 3
    assert (flow.state()["cap_extra"], flow.state()["workflow_state"]) == (1, "fixing")


def test_untrusted_go_on_a_capped_ticket_records_no_extra_round(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    assert flow.go("go-forged", author="claude-login") == "rejected-go-author"
    assert (flow.state()["cap_extra"], flow.state()["workflow_state"]) == (0, "capped")
    assert job_count(flow.queue, "fix") == 2


# --- AC 2: any round parses; the dispatcher judges it against the effective cap -------


@pytest.mark.parametrize("round_text", ["4/4", "4/3", "4"])
def test_round_over_its_denominator_parses(round_text: str) -> None:
    fields = parse_comment(
        VERDICT.format(round=round_text, head=HEADS[0], verdict="REVISE", risk="high")
    ).as_dict()
    assert fields["round"] == round_text


@pytest.mark.parametrize("verdict", ["REVISE", "APPROVE"])
def test_verdict_over_the_effective_cap_is_rejected_over_cap(
    tmp_path: Path, verdict: str
) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    wakes_before = job_count(flow.queue, "rejected")
    body = (VERDICT if verdict == "REVISE" else APPROVE).format(
        round="4/3", head=flow.head, verdict=verdict, risk="high"
    )
    assert flow.post("over-cap", body, "codex-login") == "rejected-over-cap"
    assert job_count(flow.queue, "fix") == 2
    assert job_count(flow.queue, "rejected") == wakes_before + 1
    assert (flow.state()["workflow_state"], flow.state()["review_round"]) == ("capped", 3)


def test_plan_round_over_two_is_still_stale(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    assert flow.post("go-plan", GO.format(state="planning"), "grok-login") == "queued-plan"
    flow.finish("claude")
    assert flow.post("plan-3", PLAN.format(round=3, ac="met"), "claude-login") == "stale-plan"
    assert job_count(flow.queue, "build") == 0


# --- AC 3: a ready after the cap queues nothing ---------------------------------------


def test_ready_after_the_cap_queues_nothing_and_flags_attention(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    reviews = job_count(flow.queue, "review")
    assert flow.ready(HEADS[4]) == "capped-ready"
    assert job_count(flow.queue, "review") == reviews
    assert attention(flow.queue) == ["ready-after-cap"]
    assert flow.state()["workflow_state"] == "capped"


def test_ready_on_a_capped_ticket_with_rounds_left_queues_nothing(tmp_path: Path) -> None:
    # A second AC-unmet verdict caps a ticket with rounds still left.
    flow = Flow(tmp_path)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    with flow.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = 'capped', review_round = 1"
        )
    assert flow.ready(HEADS[2]) == "capped-ready"
    assert job_count(flow.queue, "review") == 0
    assert attention(flow.queue) == ["ready-after-cap"]


def test_a_review_past_the_cap_is_never_queued(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    with flow.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = 'fixing', review_round = 3"
        )
    assert flow.ready(HEADS[4]) == "capped-ready"
    assert job_count(flow.queue, "review") == 0
    assert attention(flow.queue) == ["ready-after-cap"]
    assert flow.state()["workflow_state"] == "fixing"


def test_ready_after_an_extra_round_queues_the_next_review(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    assert flow.ready(HEADS[4]) == "capped-ready"
    assert flow.go("go-2") == "queued-fix"
    assert attention(flow.queue) == []
    flow.finish("claude")
    assert flow.ready(HEADS[5]) == "queued-review"
    review = flow.finish("codex")
    assert (review.request.head, round_of(review)) == (HEADS[5], "4/4")


# --- AC 1: builder jobs carry the next round over the effective cap -------------------


def test_build_and_fix_jobs_carry_next_round_and_cap(tmp_path: Path) -> None:
    build = Flow(tmp_path / "build")
    build.go("go-1")
    assert round_of(build.finish("claude")) == "1/3"

    planned = Flow(tmp_path / "plan")
    planned.post("go-plan", GO.format(state="planning"), "grok-login")
    assert round_of(planned.finish("claude")) is None
    assert planned.post("plan-1", PLAN.format(round=1, ac="met"), "claude-login") == (
        "queued-plan-build"
    )
    assert round_of(planned.finish("claude")) == "1/3"

    red = Flow(tmp_path / "ci")
    red.go("go-1")
    red.finish("claude")
    red.ci = "failure"
    assert red.ready(HEADS[1]) == "queued-ci-fix"
    assert round_of(red.finish("claude")) == "1/3"


# --- AC 1: a ready naming a newer head re-reads the PR head uncached -------------------


class FreshSource(FakeSource):
    def __init__(self, cached: TrackedTicket, fresh: TrackedTicket | Exception | None):
        super().__init__(cached)
        self.fresh = fresh
        self.fresh_reads: list[TicketRef] = []

    def fresh_ticket(self, ref: TicketRef) -> TrackedTicket | None:
        self.fresh_reads.append(ref)
        if isinstance(self.fresh, Exception):
            raise self.fresh
        return self.fresh


def _newer_ready(tmp_path: Path) -> tuple[Flow, TrackedTicket, TrackedTicket]:
    flow = Flow(tmp_path)
    flow.go("go-1")
    flow.finish("claude")
    flow.now[0] += 10
    flow.comments.append(
        comment("ready-new", READY.format(head=HEADS[2]), author="claude-login", created_at=flow.now[0])
    )
    cached = ticket(*flow.comments, head=HEADS[1], ci={HEADS[1]: CIResult("success")})
    fresh = ticket(*flow.comments, head=HEADS[2], ci={HEADS[2]: CIResult("success")})
    return flow, cached, fresh


def test_ready_naming_a_newer_head_rereads_the_pr_head(tmp_path: Path) -> None:
    flow, cached, fresh = _newer_ready(tmp_path)
    source = FreshSource(cached, fresh)
    flow.dispatcher.poll_once(source)
    assert source.fresh_reads == [TicketRef("owner/repo", "42", "github")]
    assert handled_outcome(flow.queue, "ready-new") == "queued-review"
    assert flow.finish("codex").request.head == HEADS[2]


def test_ready_naming_a_head_the_fresh_read_lacks_is_stale(tmp_path: Path) -> None:
    flow, cached, _fresh = _newer_ready(tmp_path)
    flow.dispatcher.poll_once(FreshSource(cached, cached))
    assert handled_outcome(flow.queue, "ready-new") == "stale-ready-head"
    assert job_count(flow.queue, "review") == 0


def test_a_failed_fresh_read_leaves_the_ready_for_the_next_poll(tmp_path: Path) -> None:
    flow, cached, fresh = _newer_ready(tmp_path)
    flow.dispatcher.poll_once(FreshSource(cached, RuntimeError("gh down")))
    assert outcome(flow.queue, "ready-new") is None
    flow.dispatcher.poll_once(FreshSource(cached, fresh))
    assert handled_outcome(flow.queue, "ready-new") == "queued-review"


# --- AC 6: a failed builder job whose ready is posted is done, not retried ------------


def _failed_fix(flow: Flow) -> Job:
    """Round-1 REVISE, then its fix job claimed (head HEADS[1])."""
    flow.go("go-1")
    flow.finish("claude")
    flow.ready(HEADS[1])
    flow.finish("codex")
    assert flow.revise("1/3") == "queued-review-fix"
    fix = flow.queue.claim("claude")
    assert fix is not None and fix.request.head == HEADS[1]
    return fix


def _events(queue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [
            (str(row["event"]), str(row["detail"]))
            for row in connection.execute(
                "SELECT event, detail FROM job_events "
                "WHERE event NOT LIKE 'handoff-%' ORDER BY id"
            )
        ]


def _jobs(queue, action: str) -> list[tuple[str, int, str | None]]:
    with queue.connect() as connection:
        return [
            (str(row["state"]), int(row["attempt"]), row["handoff_comment_id"])
            for row in connection.execute(
                "SELECT * FROM jobs WHERE action = ? ORDER BY created_at, rowid", (action,)
            )
        ]


def test_failed_fix_with_a_posted_ready_is_done_from_ready(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    fix = _failed_fix(flow)
    assert flow.ready(HEADS[2]) == "queued-review"
    flow.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    flow.dispatcher.reconcile_jobs()
    assert _jobs(flow.queue, "fix") == [("done", 1, f"ready-{HEADS[2][:6]}")]
    ((event, detail),) = _events(flow.queue)
    assert event == "done-from-ready" and f"ready-{HEADS[2][:6]}" in detail
    assert flow.queue.unhandled_failures() == ()


def test_failed_build_with_a_posted_ready_is_done_from_ready(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    flow.go("go-1")
    build = flow.queue.claim("claude")
    assert build is not None and build.request.head is None
    flow.ci = "pending"
    flow.head = HEADS[1]
    flow.now[0] += 10
    ready_id = f"ready-{HEADS[1][:6]}"
    flow.comments.append(
        comment(ready_id, READY.format(head=HEADS[1]), author="claude-login", created_at=flow.now[0])
    )
    flow.dispatcher.process(flow.snapshot())
    assert outcome(flow.queue, ready_id) is None  # CI pending: routed later
    flow.queue.finish(build.id, owner="claude", handoff_comment_id=None)
    flow.dispatcher.reconcile_jobs()
    assert _jobs(flow.queue, "build") == [("done", 1, ready_id)]
    assert [event for event, _detail in _events(flow.queue)] == ["done-from-ready"]
    flow.ci = "success"
    flow.dispatcher.process(flow.snapshot())
    assert outcome(flow.queue, ready_id) == "queued-review"


def _unvalidated_ready(tmp_path: Path) -> tuple[Flow, Job, TrackedTicket]:
    """A failed fix whose ready names a head the polled PR head is not."""
    flow = Flow(tmp_path)
    fix = _failed_fix(flow)
    flow.now[0] += 10
    flow.comments.append(
        comment("ready-new", READY.format(head=HEADS[2]), author="claude-login", created_at=flow.now[0])
    )
    flow.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    return flow, fix, flow.snapshot()


def test_t6_waits_while_the_ready_head_is_unread(tmp_path: Path) -> None:
    flow, fix, cached = _unvalidated_ready(tmp_path)
    flow.dispatcher.poll_once(FreshSource(cached, RuntimeError("PR read failed")))
    assert outcome(flow.queue, "ready-new") is None
    assert _jobs(flow.queue, "fix") == [("failed", 1, None)]
    assert [job.id for job in flow.queue.unhandled_failures()] == [fix.id]
    assert [event for event, _detail in _events(flow.queue)] == ["done-deferred"]


def test_a_ready_found_stale_after_a_failed_read_does_not_stop_the_retry(
    tmp_path: Path,
) -> None:
    flow, _fix, cached = _unvalidated_ready(tmp_path)
    flow.dispatcher.poll_once(FreshSource(cached, RuntimeError("PR read failed")))
    flow.dispatcher.poll_once(FreshSource(cached, cached))
    assert outcome(flow.queue, "ready-new") == "stale-ready-head"
    assert _jobs(flow.queue, "fix") == [("failed", 1, None), ("queued", 2, None)]


def test_a_ready_found_current_after_a_failed_read_is_done_from_ready(
    tmp_path: Path,
) -> None:
    flow, _fix, cached = _unvalidated_ready(tmp_path)
    flow.dispatcher.poll_once(FreshSource(cached, RuntimeError("PR read failed")))
    flow.head = HEADS[2]
    flow.dispatcher.poll_once(FreshSource(cached, flow.snapshot()))
    assert outcome(flow.queue, "ready-new") == "queued-review"
    assert _jobs(flow.queue, "fix") == [("done", 1, "ready-new")]


def test_a_ready_at_the_pr_head_with_ci_pending_is_done_from_ready(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    fix = _failed_fix(flow)
    flow.ci = "pending"
    flow.head = HEADS[2]
    flow.now[0] += 10
    flow.comments.append(
        comment("ready-new", READY.format(head=HEADS[2]), author="claude-login", created_at=flow.now[0])
    )
    flow.dispatcher.process(flow.snapshot())
    assert outcome(flow.queue, "ready-new") is None
    flow.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    flow.dispatcher.reconcile_jobs()
    assert _jobs(flow.queue, "fix") == [("done", 1, "ready-new")]


def test_ready_at_the_job_head_does_not_stop_the_retry(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    fix = _failed_fix(flow)
    flow.post("ready-same", READY.format(head=HEADS[1]), "claude-login")
    flow.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    flow.dispatcher.reconcile_jobs()
    assert _jobs(flow.queue, "fix") == [("failed", 1, None), ("queued", 2, None)]


@pytest.mark.parametrize("author", ["grok-login", "stranger"])
def test_untrusted_ready_does_not_stop_the_retry(tmp_path: Path, author: str) -> None:
    flow = Flow(tmp_path)
    fix = _failed_fix(flow)
    flow.head = HEADS[2]
    flow.post("ready-forged", READY.format(head=HEADS[2]), author)
    flow.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    flow.dispatcher.reconcile_jobs()
    assert _jobs(flow.queue, "fix") == [("failed", 1, None), ("queued", 2, None)]


def test_a_ready_from_before_the_job_does_not_stop_the_retry(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    fix = _failed_fix(flow)
    flow.comments.append(
        comment("ready-early", READY.format(head=HEADS[2]), author="claude-login", created_at=1)
    )
    flow.dispatcher.process(flow.snapshot())
    flow.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    flow.dispatcher.reconcile_jobs()
    assert _jobs(flow.queue, "fix") == [("failed", 1, None), ("queued", 2, None)]


def test_t6_retry_detail_names_the_failed_job(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    fix = _failed_fix(flow)
    flow.queue.finish(fix.id, owner="claude", handoff_comment_id=None)
    flow.dispatcher.reconcile_jobs()
    retry = flow.queue.claim("claude")
    assert retry is not None and retry.request.attempt == 2
    detail = json.loads(retry.request.detail or "{}")
    assert detail["retry_of"] == fix.id
    assert detail["round"] == "2/3"


# --- AC 7: Claude print mode waits for its background tasks ---------------------------


@pytest.mark.parametrize(
    ("harness", "expected"),
    [("claude", {CLAUDE_BG_WAIT_ENV: "0"}), ("codex", {}), ("cursor", {})],
)
def test_harness_env_names_the_bg_wait_ceiling_for_claude_only(
    harness: str, expected: dict
) -> None:
    assert harness_env(harness) == expected


def _report_env(tmp_path: Path) -> tuple[str, Path]:
    report = tmp_path / "seen.json"
    script = (
        "import json, os\n"
        f"json.dump(os.environ.get({CLAUDE_BG_WAIT_ENV!r}), open({str(report)!r}, 'w'))\n"
    )
    return script, report


def _claimed(poller, harness: str) -> str:
    job = poller.queue.enqueue(
        JobRequest(
            repo="maxharris1/factory",
            ticket_id="72",
            action="fix",
            recipient=harness,
            source_comment_id="revise-1",
            timeout_seconds=3600,
            head="a" * 40,
            detail=json.dumps({"round": "2/3", "action": "fix"}),
        ),
        dedupe_key=f"fix:72:{harness}",
    )
    claimed = poller.pool.claim(harness)
    assert claimed is not None and claimed[0].id == job.id
    return job.id


def _run(poller, job_id: str, harness: str, script: str, env: dict | None = None):
    proc = orch(
        "job", "run", f"{harness}-bot", job_id, "--db", str(poller.path),
        "--machines", str(poller.machines), "--interval", "0.2", "--",
        sys.executable, "-c", script,
        env=env,
    )
    _out, err = proc.communicate(timeout=60)
    return proc.returncode, err


@pytest.mark.parametrize(("harness", "expected"), [("claude", "0"), ("codex", None)])
def test_claude_print_mode_gets_the_bg_wait_ceiling_on_the_box(
    tmp_path: Path, harness: str, expected: str | None
) -> None:
    poller = Poller(tmp_path, now=time.time(), box_slots=3)
    poller.queue.clock = time.time
    job_id = _claimed(poller, harness)
    script, report = _report_env(tmp_path)
    code, err = _run(poller, job_id, harness, script)
    assert code == 0, err
    assert json.loads(report.read_text(encoding="utf-8")) == expected


@pytest.mark.parametrize(("harness", "expected"), [("claude", "0"), ("codex", None)])
def test_claude_print_mode_gets_the_bg_wait_ceiling_on_a_remote_device(
    tmp_path: Path, harness: str, expected: str | None
) -> None:
    poller = Poller(tmp_path, box=False, now=time.time())
    poller.queue.clock = time.time
    job_id = _claimed(poller, harness)
    script, report = _report_env(tmp_path)
    ssh = Ssh(tmp_path)
    code, err = _run(poller, job_id, harness, script, env=ssh.env())
    assert code == 0, err
    assert json.loads(report.read_text(encoding="utf-8")) == expected
    start = ssh.calls()[0]["script"]
    assert (f"{CLAUDE_BG_WAIT_ENV}=0" in start) is (expected is not None)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ('{"retry_of":"abc123","round":"2/3"}', "abc123"),
        ('{"round":"2/3"}', None),
        ('{"retry_of":7}', None),
        ("not json", None),
        (None, None),
    ],
)
def test_orch_job_run_reads_the_job_a_retry_repeats(detail: str | None, expected: str | None) -> None:
    assert cli._retry_of(detail) == expected
