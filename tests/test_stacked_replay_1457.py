from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from lib.brief import render_brief
from lib.dispatch.types import (
    CheckRow,
    CIChange,
    CIResult,
    ParentMerged,
    ParentOpen,
    StackedOn,
    StackParent,
    ci_fresh,
    pr_ci,
)
from lib.dispatcher import Comment, TicketRef, TrackedTicket
from lib.factory_md import CheckCommand
from lib.poll_source import FedPulls
from lib.pre_ready import planned
from lib.queue import LOCAL_GATE_PASSED, LOCAL_GATE_UNAVAILABLE, Job
from lib.update_check import RetargetDelta
from tests.merge_queue_support import sha
from tests.regenerate_brief_snapshots import BOX_FACTORY_ROOT
from tests.test_dispatcher import READY, VERDICT, factory, jobs_of
from tests.test_feeder import dispatcher_for, make_feeder, rows
from tests.test_feeder import issue as feeder_issue
from tests.test_feeder_template import body
from tests.test_lifecycle import outcome
from tests.test_retarget_review import Delta
from tests.test_stack_retarget_transition import ONTO, Actions, Bounds, rebases
from tests.test_ticket_base_branch import stack_attention, stack_poll_errors

REPO = "owner/app"
PARENT, CHILD = "factory/issue-6", "factory/issue-7"
PARENT_PR, CHILD_PR = 501, 502
C1, P2, C2 = sha(0xC1), sha(0xB2), sha(0xC2)
FACTORY = replace(
    factory(ship="handoff", finish_line=None),
    ci_branches=("main",),
    ci_local_gate=CheckCommand("make gate", 60),
)
UNCHANGED = RetargetDelta("same", "same", frozenset({"lib/child.py"}), frozenset({"lib/parent.py"}))


def green(run_created_at: float) -> CIResult:
    row = CheckRow("test", "completed", "success", run_id="1", run_created_at=run_created_at)
    return CIResult("success", checks=(row,))


class StackBoard:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "orch.db"
        self.clock = [10_000.0]
        self.feeder, self.queue, self.github, _ = make_feeder(tmp_path, now=self.clock)
        self.github.landings[(REPO, PARENT_PR)] = ParentOpen()
        self.dispatcher = dispatcher_for(tmp_path, self.queue, feeder=self.feeder)
        self.dispatcher.review_actions = self.actions = Actions(self.github)
        self.dispatcher.stack_bounds = self.bounds = Bounds()
        self.dispatcher.retarget_delta = self.delta = Delta(UNCHANGED)
        self.fed = FedPulls(self.db)
        self.ci: dict[str, CIResult] = {}

    def github_candidates(self, label_prefix: str, *, exclude=frozenset()) -> tuple:
        return ()

    def linear_candidates(self, query: str, *, exclude, reserve) -> tuple:
        return ()

    def fresh_ticket(self, ref: TicketRef) -> TrackedTicket:
        return self.snapshot(int(ref.ticket_id))

    def tracked_tickets(self, refs: tuple[TicketRef, ...]) -> tuple[TrackedTicket, ...]:
        found = []
        for ref in refs:
            if self.github.pulls[(REPO, int(ref.ticket_id))].get("merged"):
                self.dispatcher.record_terminal(REPO, ref.ticket_id, "merged")
            else:
                found.append(self.snapshot(int(ref.ticket_id)))
        return tuple(found)

    def snapshot(self, number: int) -> TrackedTicket:
        pull, ticket_id = self.github.pulls[(REPO, number)], str(number)
        head = self.head(pull["head"])
        config = replace(FACTORY, base_branch=self.fed.ticket_base(REPO, ticket_id) or "main")
        recorded = self.fed.stack_parent(REPO, ticket_id)
        stack = None
        if recorded is not None and pull["base"] == recorded[1]:
            stack = StackParent(*recorded, self.head(recorded[1]))
            config = replace(config, base_branch=stack.branch)
        cut = self.fed.retarget_at(REPO, ticket_id)
        changes = () if cut is None else (CIChange("retarget", cut),)
        raw = self.ci.get(head, CIResult("pending", reported=False))
        return TrackedTicket(
            repo=REPO,
            ticket_id=ticket_id,
            factory=config,
            comments=tuple(self.github.comments(REPO, number)),
            current_head=head,
            ci={head: ci_fresh(pr_ci(raw, config, pull["base"]), changes, config.ci_required_checks)},
            risk="low",
            pr_number=ticket_id,
            pr_base=pull["base"],
            mergeable_state=pull.get("mergeable_state"),
            stack=stack,
            stacked_on=None if recorded is None else StackedOn(*recorded, "main"),
        )

    def head(self, branch: str) -> str:
        return self.github.branches[(REPO, branch)]

    def feed(self, number: int, text: str) -> None:
        self.github.issues.setdefault(REPO, []).append(feeder_issue(number, body=text, template=False))

    def poll(self, times: int = 1) -> None:
        for _ in range(times):
            self.clock[0] += 10
            self.dispatcher.poll_once(self)

    def post(self, comment_id: str, text: str) -> None:
        self.clock[0] += 1
        comment = Comment(id=comment_id, author="maxharris1", body=text, created_at=self.clock[0])
        self.github.posted.setdefault((REPO, CHILD_PR), []).append(comment)

    def push(self, branch: str, head: str, **pull: object) -> None:
        self.github.branches[(REPO, branch)] = head
        number = PARENT_PR if branch == PARENT else CHILD_PR
        self.github.pulls[(REPO, number)].update(pull)

    def finish(self, harness: str, handoff: str, gate: tuple[str, str] | None = None) -> Job:
        others = [job.id for job in self.queue.outstanding() if job.request.ticket_id != str(CHILD_PR)]
        job = self.queue.claim(harness, excluded_job_ids=others)
        assert job is not None, f"no {harness} job for the child"
        if gate is not None:
            self.queue.record_head_check(job.id, event=gate[0], comment_id=handoff, head=gate[1])
        self.queue.finish(job.id, owner=harness, handoff_comment_id=handoff)
        return job

    def gate_passed(self, job: Job, comment_id: str, head: str) -> None:
        self.queue.record_head_check(job.id, event=LOCAL_GATE_PASSED, comment_id=comment_id, head=head)

    def land(self) -> None:
        parent = self.github.pulls[(REPO, PARENT_PR)]
        head = self.head(PARENT)
        self.github.landings[(REPO, PARENT_PR)] = ParentMerged(into="main", head=head, heads=(head,))
        parent.update(merged=True, state="closed")

    def jobs(self, action: str) -> list[dict]:
        return [
            {**dict(row), "detail": json.loads(row["detail"] or "{}")}
            for row in jobs_of(self.queue, action)
            if row["ticket_id"] == str(CHILD_PR)
        ]


def test_a_stacked_parent_and_child_replay_from_feed_to_handoff(tmp_path: Path) -> None:
    board = StackBoard(tmp_path)

    board.feed(6, body())
    board.poll()
    parent = rows(board.db)["6"]
    assert (parent["pr_number"], parent["base_branch"]) == (str(PARENT_PR), "main")
    assert board.github.pulls[(REPO, PARENT_PR)]["base"] == "main"
    p1 = board.head(PARENT)

    board.feed(7, body(depends="Depends on: #6", extra=f"Target branch: {PARENT}"))
    board.poll()
    child = rows(board.db)["7"]
    assert child["state"] == "fed", "a child whose Depends on parent is still open is fed, not held"
    assert (child["stack_parent_pr"], child["stack_parent_branch"], child["stack_parent_head_at_feed"]) == (
        PARENT_PR, PARENT, p1,
    )
    assert board.github.pulls[(REPO, CHILD_PR)]["base"] == PARENT
    (build,) = board.jobs("build")
    assert build["detail"]["base_branch"] == PARENT, "the child builds on its parent's branch"
    assert build["detail"]["stack"] == {"pr": PARENT_PR, "branch": PARENT, "sha": p1}

    board.push(CHILD, C1)
    board.post("ready-1", READY.format(head=C1))
    built = board.finish("claude", "ready-1", gate=(LOCAL_GATE_UNAVAILABLE, C1))
    board.poll()
    assert board.jobs("review") == [], "a ready into the parent branch waits for the local gate"
    board.gate_passed(built, "ready-1", C1)
    board.poll()
    (review,) = board.jobs("review")
    assert (review["head"], review["detail"]["local_gate_head"]) == (C1, C1)

    (job,) = [job for job in board.queue.outstanding() if job.request.action == "review"]
    text = render_brief(
        job, bot="codex-bot", context=None, context_note="test", factory_root=BOX_FACTORY_ROOT
    )
    assert f"This diff is `{PARENT}...{C1}`, this PR's own work." in text
    assert f"Parent code is reviewed on #{PARENT_PR}" in text

    approve = VERDICT.format(round=review["detail"]["round"], head=C1, verdict="APPROVE", risk="low")
    board.post("approve-1", approve)
    board.finish("codex", "approve-1")
    board.poll()
    assert board.actions.handoffs == [], "a child stacked on its parent's branch is not handed off"
    board.push(PARENT, P2)
    board.push(CHILD, C1, mergeable_state="behind")
    board.poll()
    assert len(board.jobs("review")) == 1, "a parent push spends no review on the child"
    assert "base-mismatch" not in stack_attention(tmp_path)

    board.land()
    board.poll()
    assert board.actions.bases == ["main"]
    assert rows(board.db)["7"]["base_branch"] == "main"
    assert board.bounds.calls == [(REPO, C1, (p1, P2))]
    (rebase,) = rebases(board.queue)
    assert (rebase["rebase"]["onto_from"], rebase["rebase"]["parent_head"]) == (ONTO, P2)

    retarget_at = rows(board.db)["7"]["retarget_at"]
    board.push(CHILD, C2, mergeable_state="clean")
    board.post("ready-9", READY.format(head=C2))
    board.finish("claude", "ready-9")
    board.ci[C2] = green(run_created_at=retarget_at - 1)
    board.poll()
    moved = board.snapshot(CHILD_PR)
    assert planned(moved.factory, moved.pr_base) == (), "the retargeted child plans no local gate"
    assert outcome(board.queue, "ready-9") is None, "a green from before the retarget does not count"
    assert board.actions.handoffs == []

    board.ci[C2] = green(run_created_at=retarget_at + 1)
    board.poll(times=2)
    assert len(board.jobs("review")) == 1, "an unchanged patch-id after the rebase spends no review"
    assert board.delta.calls == [(REPO, "main", ONTO, C1, P2, C2)]
    assert outcome(board.queue, "ready-9") == "retarget-carry"
    with board.queue.connect() as connection:
        carries = connection.execute("SELECT from_head, to_head FROM approval_carries").fetchall()
    assert [tuple(row) for row in carries] == [(C1, C2)]
    assert outcome(board.queue, "approve-1") == "handed-off"
    assert board.actions.handoffs == [C2]
    assert stack_poll_errors(tmp_path) == []
