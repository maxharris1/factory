"""Issue #86: a per-PR human hold before a ship: merge.

A PR that carries factory.md's hold.label, or whose diff against base
touches a hold.paths glob, is not merged on a covered APPROVE with green
CI. It waits in orch:state/awaiting-ok for the approver's
kind=ok head=<sha> (or, for a label-only hold, the approver removing the
label), and Grok Bot is woken once per held head.
"""

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from lib.dashboard import collect_snapshot
from lib.dispatcher import (
    CIResult,
    Comment,
    Dispatcher,
    LabelEvent,
    TrackedTicket,
)
from lib.factory_md import FactoryConfig, HoldConfig, parse_factory_md
from lib.hold import glob_matches
from lib.hook_t8 import T8Hook
from lib.queue import SQLiteJobQueue
from lib.status import SQLiteStatusReader, build_status, render
from tests.test_dispatcher import (
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    seed_review_job,
)

HEAD = "a" * 40
NEW_HEAD = "b" * 40
APPROVER = "max-login"
UI_PATHS = (
    "src/**/*.tsx",
    "src/**/*.css",
    "src/view/**",
    "src/features/**/*.tsx",
    "public/**",
    "index.html",
)
TRUSTED = {
    "github": {
        "grok-login": {"grok-bot"},
        "claude-login": {"claude-bot"},
        "codex-login": {"codex-bot"},
        APPROVER: {"grok-bot"},
        "maxharris1": {"grok-bot"},
    },
    "linear": {},
}
OK = "<!-- orch:v1 kind=ok head={head} -->\nOK after UI QA.\n"


def hold_factory(
    *,
    ship: str = "merge",
    label: str = "factory:hold",
    approver: str = APPROVER,
    paths: tuple[str, ...] = UI_PATHS,
) -> FactoryConfig:
    return FactoryConfig(
        base_branch="main",
        tickets="github",
        builder_harness="claude",
        reviewer_harness="codex",
        rounds_low=1,
        rounds_high=3,
        verify="none",
        ship=ship,
        skills=(),
        hold=HoldConfig(label=label, approver=approver, paths=paths),
    )


def setup(
    tmp_path: Path,
    actions: FakeReviewActions | None = None,
    hooks: tuple = (),
) -> tuple[Dispatcher, SQLiteJobQueue, FakeReviewActions]:
    actions = actions or FakeReviewActions()
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED,
        review_actions=actions,
        grok_delivery=FakeGrokDelivery(),
        hooks=hooks,
    )
    return dispatcher, queue, actions


def approve(head: str = HEAD, comment_id: str = "approve-1") -> Comment:
    return comment(
        comment_id,
        VERDICT.format(round=1, head=head, verdict="APPROVE", risk="high"),
        author="codex-login",
        created_at=10,
    )


def ok(head: str, *, author: str = APPROVER, comment_id: str = "ok-1") -> Comment:
    return comment(comment_id, OK.format(head=head), author=author, created_at=20)


def revise(head: str = HEAD, risk: str = "high") -> Comment:
    return comment(
        "revise-1",
        VERDICT.format(round=1, head=head, verdict="REVISE", risk=risk),
        author="codex-login",
        created_at=15,
    )


def snapshot(
    *comments: Comment,
    head: str = HEAD,
    factory: FactoryConfig | None = None,
    labels: tuple[str, ...] | None = (),
    files: tuple[str, ...] | None = ("lib/server.py",),
    events: tuple[LabelEvent, ...] | None = (),
    ci: str = "success",
    risk: str = "high",
) -> TrackedTicket:
    return TrackedTicket(
        repo="owner/repo",
        ticket_id="42",
        factory=factory or hold_factory(),
        comments=comments,
        current_head=head,
        ci={head: CIResult(ci)},
        risk=risk,
        pr_number="42",
        ticket_url="https://github.com/owner/repo/pull/42",
        labels=labels,
        changed_files=files,
        label_events=events,
    )


def ready(dispatcher: Dispatcher, queue: SQLiteJobQueue, head: str = HEAD) -> None:
    """Seed a build and a review job for head, so an APPROVE is current."""
    seed_review_job(dispatcher, queue, head)


def grok_jobs(queue: SQLiteJobQueue) -> list[sqlite3.Row]:
    with queue.connect() as connection:
        return connection.execute(
            "SELECT action, head, detail FROM jobs WHERE recipient = 'grok'"
        ).fetchall()


def workflow_state(queue: SQLiteJobQueue) -> str:
    with queue.connect() as connection:
        return str(
            connection.execute(
                "SELECT workflow_state FROM ticket_state WHERE ticket_id = '42'"
            ).fetchone()[0]
        )


def assert_held(
    queue: SQLiteJobQueue,
    actions: FakeReviewActions,
    head: str = HEAD,
) -> None:
    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"
    assert actions.states[-1] == "awaiting-ok"
    wakes = [row for row in grok_jobs(queue) if row["action"] == "awaiting-ok"]
    assert wakes and wakes[-1]["head"] == head
    detail = json.loads(wakes[-1]["detail"])
    assert detail["head"] == head
    assert detail["pr"] == "https://github.com/owner/repo/pull/42"
    assert detail["approver"] == APPROVER


# --- the hold blocks the merge -------------------------------------------


def test_a_label_hold_blocks_the_merge(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()

    dispatcher.process(snapshot(approve(), labels=("factory:hold", "orch:state/review")))

    assert_held(queue, actions)
    detail = json.loads(grok_jobs(queue)[-1]["detail"])
    assert detail["reasons"] == ["label"]


def test_a_paths_auto_hold_blocks_the_merge(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()

    dispatcher.process(
        snapshot(approve(), files=("lib/server.py", "src/view/Board.tsx"))
    )

    assert_held(queue, actions)
    detail = json.loads(grok_jobs(queue)[-1]["detail"])
    assert detail["reasons"] == ["paths"]
    assert detail["held_paths"] == ["src/view/Board.tsx"]


def test_a_non_ui_pr_still_auto_merges(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()

    dispatcher.process(
        snapshot(approve(), files=("lib/server.py", "docs/notes.md", "srcx/a.tsx"))
    )

    assert actions.merges == [(True, HEAD)]
    assert actions.states == ["merged"]
    assert [row["action"] for row in grok_jobs(queue)] == ["done"]


def test_a_repo_with_no_hold_configured_merges_without_reading_holds(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    plain = FactoryConfig(
        base_branch="main",
        tickets="github",
        builder_harness="claude",
        reviewer_harness="codex",
        rounds_low=1,
        rounds_high=3,
        verify="none",
        ship="merge",
        skills=(),
    )

    dispatcher.process(
        snapshot(approve(), factory=plain, labels=None, files=None, events=None)
    )

    assert actions.merges == [(True, HEAD)]


@pytest.mark.parametrize("ship", ["handoff", "after:tracy-site"])
def test_a_hold_is_never_applied_on_a_handoff_or_after_repo(
    tmp_path: Path,
    ship: str,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()

    dispatcher.process(
        snapshot(
            approve(),
            factory=hold_factory(ship=ship),
            labels=("factory:hold",),
            files=("src/view/Board.tsx",),
        )
    )

    assert "awaiting-ok" not in actions.states
    assert not any(row["action"] == "awaiting-ok" for row in grok_jobs(queue))
    if ship == "handoff":
        assert actions.handoffs == [HEAD]
        assert actions.states == ["handed-off"]
    else:
        assert actions.approvals == [(HEAD, "tracy-site")]
        assert actions.states == ["waiting-external"]
    assert actions.merges == []


@pytest.mark.parametrize(
    ("labels", "files", "events"),
    [
        (None, ("lib/a.py",), ()),
        ((), None, ()),
        ((), ("lib/a.py",), None),
    ],
)
def test_an_unread_label_diff_or_event_list_fails_closed_and_retries(
    tmp_path: Path,
    labels: tuple[str, ...] | None,
    files: tuple[str, ...] | None,
    events: tuple[LabelEvent, ...] | None,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(snapshot(approve(), labels=labels, files=files, events=events))

    assert actions.merges == []
    dispatcher.process(snapshot(approve()))
    assert actions.merges == [(True, HEAD)]


def test_the_hold_wakes_grok_once_and_writes_the_state_once(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()
    held = snapshot(approve(), labels=("factory:hold",), files=("index.html",))

    for _ in range(3):
        dispatcher.process(held)
        dispatcher.reconcile_jobs()

    assert actions.merges == []
    assert actions.states == ["awaiting-ok"]
    wakes = [row for row in grok_jobs(queue) if row["action"] == "awaiting-ok"]
    assert len(wakes) == 1
    assert json.loads(wakes[0]["detail"])["reasons"] == ["label", "paths"]


def test_a_path_with_a_newline_is_still_auto_held(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    actions.states.clear()

    dispatcher.process(snapshot(approve(), files=("lib/a.py", "public/a\nb.svg")))

    assert_held(queue, actions)
    detail = json.loads(grok_jobs(queue)[-1]["detail"])
    assert detail["held_paths"] == ["public/a\nb.svg"]


@pytest.mark.parametrize("ci", ["failure", "pending"])
def test_a_ci_failure_while_held_does_not_consume_the_approve(
    tmp_path: Path,
    ci: str,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    files = ("src/view/Board.tsx",)
    dispatcher.process(snapshot(approve(), files=files))
    assert workflow_state(queue) == "awaiting-ok"

    dispatcher.process(snapshot(approve(), ok(HEAD), files=files, ci=ci))
    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"

    dispatcher.process(snapshot(approve(), ok(HEAD), files=files))
    dispatcher.process(snapshot(approve(), ok(HEAD), files=files))
    assert actions.merges == [(True, HEAD)]


@pytest.mark.parametrize(
    ("risk", "state", "job_action"),
    [("high", "fixing", "fix"), ("low", "capped", "capped")],
)
@pytest.mark.parametrize("same_poll", [False, True])
def test_a_revise_after_a_held_approve_supersedes_it(
    tmp_path: Path,
    risk: str,
    state: str,
    job_action: str,
    same_poll: bool,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    files = ("src/view/Board.tsx",)
    dispatcher.process(snapshot(approve(), files=files, risk=risk))
    assert workflow_state(queue) == "awaiting-ok"

    if not same_poll:
        dispatcher.process(snapshot(approve(), revise(risk=risk), files=files, risk=risk))
    for _ in range(2):
        dispatcher.process(
            snapshot(approve(), revise(risk=risk), ok(HEAD), files=files, risk=risk)
        )

    assert actions.merges == []
    assert workflow_state(queue) == state
    with queue.connect() as connection:
        actions_queued = [
            row[0] for row in connection.execute("SELECT action FROM jobs").fetchall()
        ]
        outcomes = dict(
            connection.execute(
                "SELECT comment_id, outcome FROM handled_comments"
            ).fetchall()
        )
    assert job_action in actions_queued
    assert outcomes["approve-1"] == "superseded-approve"
    assert "stale" not in [row["action"] for row in grok_jobs(queue)]


def test_a_revise_from_an_untrusted_login_does_not_supersede_a_held_approve(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    files = ("src/view/Board.tsx",)
    forged = comment(
        "revise-1",
        VERDICT.format(round=1, head=HEAD, verdict="REVISE", risk="high"),
        author="claude-login",
        created_at=15,
    )
    dispatcher.process(snapshot(approve(), files=files))

    dispatcher.process(snapshot(approve(), forged, ok(HEAD), files=files))

    assert actions.merges == [(True, HEAD)]


def test_on_a_hold_repo_a_same_poll_revise_supersedes_an_unheld_approve(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(snapshot(approve(), revise()))

    assert actions.merges == []
    assert workflow_state(queue) == "fixing"


# --- clearing the hold ---------------------------------------------------


def test_an_ok_at_the_head_merges_with_match_head_commit(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    dispatcher.process(snapshot(approve(), files=("src/view/Board.tsx",)))
    assert actions.merges == []

    dispatcher.process(snapshot(approve(), ok(HEAD), files=("src/view/Board.tsx",)))

    assert actions.merges == [(True, HEAD)]
    assert workflow_state(queue) == "merged"
    assert actions.states[-1] == "merged"
    with queue.connect() as connection:
        outcomes = dict(
            connection.execute(
                "SELECT comment_id, outcome FROM handled_comments"
            ).fetchall()
        )
    assert outcomes["approve-1"] == "merged"
    assert outcomes["ok-1"] == "hold-ok"


def test_an_ok_posted_before_the_approve_at_the_same_head_also_clears(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    early_ok = comment("ok-0", OK.format(head=HEAD), author=APPROVER, created_at=5)

    dispatcher.process(snapshot(early_ok, approve(), labels=("factory:hold",)))

    assert actions.merges == [(True, HEAD)]


def test_an_ok_at_a_stale_head_does_not_merge(tmp_path: Path) -> None:
    actions = FakeReviewActions({HEAD: "1" * 40, NEW_HEAD: "2" * 40})
    dispatcher, queue, actions = setup(tmp_path, actions)
    ready(dispatcher, queue, NEW_HEAD)
    actions.states.clear()

    dispatcher.process(
        snapshot(
            approve(NEW_HEAD),
            ok(HEAD),
            head=NEW_HEAD,
            files=("src/view/Board.tsx",),
        )
    )

    assert_held(queue, actions, NEW_HEAD)


def test_an_ok_carries_to_a_new_head_only_on_an_equal_patch_id(tmp_path: Path) -> None:
    actions = FakeReviewActions({HEAD: "1" * 40, NEW_HEAD: "1" * 40})
    dispatcher, queue, actions = setup(tmp_path, actions)
    ready(dispatcher, queue)

    dispatcher.process(
        snapshot(approve(), ok(HEAD), head=NEW_HEAD, files=("src/view/Board.tsx",))
    )

    assert actions.merges == [(True, NEW_HEAD)]


def test_a_new_head_after_the_ok_needs_a_new_review_and_a_new_ok(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions({HEAD: "1" * 40, NEW_HEAD: "2" * 40})
    dispatcher, queue, actions = setup(tmp_path, actions)
    ready(dispatcher, queue)
    dispatcher.process(snapshot(approve(), files=("lib/a.py", "src/view/A.tsx")))
    assert workflow_state(queue) == "awaiting-ok"

    # The builder pushes a different patch after the hold: re-review, no merge.
    dispatcher.process(
        snapshot(approve(), ok(HEAD), head=NEW_HEAD, files=("src/view/A.tsx",))
    )
    assert actions.merges == []
    assert workflow_state(queue) == "review"
    review = queue.claim("codex")
    assert review is not None and review.request.head == NEW_HEAD
    queue.finish(review.id, owner="codex", handoff_comment_id="approve-2")

    # A fresh APPROVE on the new head is held again: the old OK does not count.
    second = comment(
        "approve-2",
        VERDICT.format(round=1, head=NEW_HEAD, verdict="APPROVE", risk="high"),
        author="codex-login",
        created_at=30,
    )
    dispatcher.process(
        snapshot(
            approve(),
            ok(HEAD),
            second,
            head=NEW_HEAD,
            files=("src/view/A.tsx",),
        )
    )
    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"
    wakes = [row["head"] for row in grok_jobs(queue) if row["action"] == "awaiting-ok"]
    assert wakes == [HEAD, NEW_HEAD]

    new_ok = comment("ok-2", OK.format(head=NEW_HEAD), author=APPROVER, created_at=40)
    dispatcher.process(
        snapshot(
            approve(),
            ok(HEAD),
            second,
            new_ok,
            head=NEW_HEAD,
            files=("src/view/A.tsx",),
        )
    )
    assert actions.merges == [(True, NEW_HEAD)]


@pytest.mark.parametrize(
    "author",
    ["grok-login", "claude-login", "someone-else", "MAX-LOGIN-2"],
)
def test_an_ok_from_anyone_but_the_approver_does_not_clear(
    tmp_path: Path,
    author: str,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(
        snapshot(approve(), ok(HEAD, author=author), labels=("factory:hold",))
    )

    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"


def test_an_approver_missing_from_trusted_logins_never_clears(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    factory = hold_factory(approver="not-trusted")

    dispatcher.process(
        snapshot(
            approve(),
            ok(HEAD, author="not-trusted"),
            factory=factory,
            labels=("factory:hold",),
        )
    )

    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"


def test_an_approver_missing_from_trusted_logins_cannot_clear_by_removing_the_label(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    factory = hold_factory(approver="not-trusted")
    dispatcher.process(
        snapshot(
            approve(),
            factory=factory,
            labels=("factory:hold",),
            events=(labeled("grok-login"),),
        )
    )

    dispatcher.process(
        snapshot(
            approve(),
            factory=factory,
            labels=(),
            events=(labeled("grok-login"), unlabeled("not-trusted")),
        )
    )

    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"


def test_the_approver_login_matches_case_insensitively(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    factory = hold_factory(approver="MaxHarris1")

    dispatcher.process(
        snapshot(
            approve(),
            ok(HEAD, author="maxharris1"),
            factory=factory,
            labels=("factory:hold",),
        )
    )

    assert actions.merges == [(True, HEAD)]


def test_a_short_ok_head_does_not_clear(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(snapshot(approve(), ok(HEAD[:7]), labels=("factory:hold",)))

    assert actions.merges == []


def unlabeled(actor: str, event_id: str = "e2", label: str = "factory:hold") -> LabelEvent:
    return LabelEvent(
        id=event_id, action="unlabeled", label=label, actor=actor, created_at=30
    )


def labeled(actor: str, event_id: str = "e1", label: str = "factory:hold") -> LabelEvent:
    return LabelEvent(
        id=event_id, action="labeled", label=label, actor=actor, created_at=5
    )


def test_the_approver_removing_the_label_clears_a_label_only_hold(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    dispatcher.process(
        snapshot(approve(), labels=("factory:hold",), events=(labeled("grok-login"),))
    )
    assert workflow_state(queue) == "awaiting-ok"

    dispatcher.process(
        snapshot(
            approve(),
            labels=(),
            events=(labeled("grok-login"), unlabeled(APPROVER)),
        )
    )

    assert actions.merges == [(True, HEAD)]


def test_someone_else_removing_the_label_keeps_the_hold(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    dispatcher.process(
        snapshot(approve(), labels=("factory:hold",), events=(labeled("grok-login"),))
    )

    dispatcher.process(
        snapshot(
            approve(),
            labels=(),
            events=(labeled("grok-login"), unlabeled("claude-login")),
        )
    )

    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"


def test_a_relabel_after_the_approver_removed_it_holds_again(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(
        snapshot(
            approve(),
            labels=("factory:hold",),
            events=(unlabeled(APPROVER, "e1"), labeled("grok-login", "e3")),
        )
    )

    assert actions.merges == []


def test_removing_the_label_does_not_clear_a_paths_hold(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(
        snapshot(
            approve(),
            labels=(),
            files=("public/logo.svg",),
            events=(labeled("grok-login"), unlabeled(APPROVER)),
        )
    )

    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"
    dispatcher.process(
        snapshot(
            approve(),
            ok(HEAD),
            labels=(),
            files=("public/logo.svg",),
            events=(labeled("grok-login"), unlabeled(APPROVER)),
        )
    )
    assert actions.merges == [(True, HEAD)]


def test_label_names_match_case_insensitively(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    dispatcher.process(snapshot(approve(), labels=("Factory:Hold",)))

    assert actions.merges == []


def test_with_the_t8_hook_installed_the_ok_is_not_rejected(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path, hooks=(T8Hook(),))
    ready(dispatcher, queue)

    dispatcher.process(snapshot(approve(), ok(HEAD), labels=("factory:hold",)))

    assert actions.merges == [(True, HEAD)]
    assert not any(row["action"] == "rejected" for row in grok_jobs(queue))


def test_an_untrusted_ok_is_rejected_once_and_wakes_grok(tmp_path: Path) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)

    held = snapshot(approve(), ok(HEAD, author="claude-login"), labels=("factory:hold",))
    dispatcher.process(held)
    dispatcher.process(held)

    rejected = [row for row in grok_jobs(queue) if row["action"] == "rejected"]
    assert len(rejected) == 1
    assert actions.merges == []


def test_a_crash_after_the_cleared_merge_does_not_merge_twice(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue, actions = setup(tmp_path, actions)
    ready(dispatcher, queue)
    held = snapshot(approve(), labels=("factory:hold",))
    dispatcher.process(held)
    actions.crash_after_merge = True
    cleared = snapshot(approve(), ok(HEAD), labels=("factory:hold",))
    with pytest.raises(SystemExit):
        dispatcher.process(cleared)
    actions.crash_after_merge = False

    dispatcher.process(cleared)

    assert actions.merges == [(True, HEAD)]
    assert workflow_state(queue) == "merged"


# --- orch status and the dashboard show who the PR waits on -------------


def test_orch_status_and_the_dashboard_show_waiting_on_the_approver(
    tmp_path: Path,
) -> None:
    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue)
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) "
            "VALUES ('owner/repo', '42', 'github', 1)"
        )
    dispatcher.process(snapshot(approve(), labels=("factory:hold",)))

    reader = SQLiteStatusReader(queue.path)
    output = render(build_status(reader, reader, clock=lambda: 100))
    assert f"owner/repo#42 awaiting-ok waiting on {APPROVER} [github]" in output

    data = collect_snapshot(queue.path, tmp_path / "missing.log", now=100.0)
    paused = {row["ticket_id"]: row for row in data["paused"]}
    assert paused["42"]["state"] == "awaiting-ok"
    assert paused["42"]["waiting_on"] == APPROVER
    tickets = {row["ticket_id"]: row for row in data["tickets"]}
    assert tickets["42"]["waiting_on"] == APPROVER
    items = [item for item in data["attention"]["items"] if item["ticket_id"] == "42"]
    assert items == [
        {
            "kind": "paused",
            "repo": "owner/repo",
            "ticket_id": "42",
            "detail": f"awaiting-ok (waiting on {APPROVER})",
        }
    ]

    dispatcher.process(snapshot(approve(), ok(HEAD), labels=("factory:hold",)))
    output = render(build_status(SQLiteStatusReader(queue.path), SQLiteStatusReader(queue.path), clock=lambda: 100))
    assert "waiting on" not in output


# --- factory.md hold: ----------------------------------------------------

VALID = {
    "base_branch": "main",
    "tickets": "github",
    "builder": {"harness": "claude"},
    "reviewer": {"harness": "codex"},
    "review": {"rounds": {"low": 1, "high": 3}, "verify": "none"},
    "ship": "merge",
    "skills": [],
}


def parse(**extra: object):
    data = {**VALID, **extra}
    return parse_factory_md(f"---\n{yaml.safe_dump(data, sort_keys=False)}---\n")


def test_factory_md_without_hold_has_no_hold() -> None:
    config, findings = parse()
    assert findings == [] and config is not None
    assert config.hold is None


def test_factory_md_hold_defaults() -> None:
    config, findings = parse(hold={})
    assert findings == [] and config is not None
    assert config.hold == HoldConfig(label="factory:hold", approver="max", paths=())


def test_factory_md_hold_reads_the_cleffy_paths() -> None:
    config, findings = parse(
        hold={"paths": list(UI_PATHS), "approver": "maxharris1", "label": "qa:hold"}
    )
    assert findings == [] and config is not None
    assert config.hold == HoldConfig(
        label="qa:hold", approver="maxharris1", paths=UI_PATHS
    )


@pytest.mark.parametrize(
    ("hold", "message"),
    [
        ("yes", "hold must be a mapping"),
        ({"labels": "x"}, "unknown field 'hold.labels'"),
        ({"label": ""}, "hold.label must be a GitHub label name"),
        ({"label": 3}, "hold.label must be a GitHub label name"),
        ({"label": "a" * 51}, "hold.label must be a GitHub label name"),
        ({"label": "a,b"}, "hold.label must be a GitHub label name"),
        ({"approver": "max harris"}, "hold.approver must be a GitHub login"),
        ({"approver": "-max"}, "hold.approver must be a GitHub login"),
        ({"paths": "src/**"}, "hold.paths must be a list"),
        ({"paths": [""]}, "hold.paths[0] must be a repo-relative glob"),
        ({"paths": ["/etc/x"]}, "hold.paths[0] must be a repo-relative glob"),
        ({"paths": ["src/../x"]}, "hold.paths[0] must be a repo-relative glob"),
        ({"paths": ["ok/*", 7]}, "hold.paths[1] must be a repo-relative glob"),
        ({"paths": ["a b/*"]}, "hold.paths[0] must be a repo-relative glob"),
    ],
)
def test_factory_md_refuses_a_bad_hold(hold: object, message: str) -> None:
    config, findings = parse(hold=hold)
    assert config is None
    assert message in [finding.message for finding in findings]


@pytest.mark.parametrize("ship", ["handoff", "after:tracy-site"])
def test_factory_md_warns_that_hold_has_no_effect_without_ship_merge(
    ship: str,
) -> None:
    config, findings = parse(ship=ship, hold={"paths": ["src/**"]})
    assert config is not None
    assert [(item.severity, item.message) for item in findings] == [
        ("warning", "hold has no effect unless ship is merge")
    ]


# --- globs ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/**/*.tsx", "src/App.tsx", True),
        ("src/**/*.tsx", "src/view/deep/Board.tsx", True),
        ("src/**/*.tsx", "src/App.ts", False),
        ("src/**/*.tsx", "other/src/App.tsx", False),
        ("src/**/*.css", "src/a/b.css", True),
        ("src/view/**", "src/view/x.ts", True),
        ("src/view/**", "src/view/a/b/c.json", True),
        ("src/view/**", "src/viewer/x.ts", False),
        ("src/features/**/*.tsx", "src/features/a/B.tsx", True),
        ("public/**", "public/logo.svg", True),
        ("public/**", "publicity/x", False),
        ("index.html", "index.html", True),
        ("index.html", "public/index.html", False),
        ("src/*.ts", "src/a.ts", True),
        ("src/*.ts", "src/a/b.ts", False),
        ("src/?.ts", "src/a.ts", True),
        ("src/?.ts", "src/ab.ts", False),
        ("**/*.css", "a.css", True),
        ("**/*.css", "x/y/a.css", True),
        ("a.b", "aXb", False),
        ("public/**", "public/a\nb.svg", True),
        ("src/**/*.tsx", "src/a\nb/C.tsx", True),
        ("src/view/**", "src/view/\n", True),
        ("**/*.css", "x\ny/a.css", True),
        ("src/*.ts", "src/a\nb.ts", True),
        ("src/?.ts", "src/\n.ts", True),
        ("index.html", "index.html\n", False),
        ("src/*.ts", "src/a.ts\n", False),
    ],
)
def test_glob_matches(pattern: str, path: str, expected: bool) -> None:
    assert glob_matches(pattern, path) is expected


# --- the live poll source reads the hold inputs, uncached ----------------

HOLD_FACTORY = """---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: none
ship: {ship}
skills: []
{hold}---
"""


class HoldGh:
    """FakeGh plus the three hold reads."""

    def __init__(self, factory_text: str, *, fail: str | None = None):
        from tests.test_live_poll import FakeGh

        self.inner = FakeGh(factory_text)
        self.fail = fail
        self.calls = self.inner.calls

    def __call__(self, argv, timeout):
        from lib.github_actions import GhResult

        call = list(argv)
        endpoint = next((part for part in call if part.startswith("repos/")), "")
        if self.fail and self.fail in endpoint:
            self.calls.append(call)
            return GhResult(1, "", "failed (HTTP 502)")
        if endpoint.endswith("/issues/12/labels?per_page=100"):
            value: object = [{"name": "factory:hold"}, {"name": "orch:state/review"}]
        elif endpoint.endswith("/pulls/12/files?per_page=100"):
            value = [
                {"filename": "src/view/Board.tsx", "status": "modified"},
                {
                    "filename": "lib/new.py",
                    "previous_filename": "public/old.py",
                    "status": "renamed",
                },
            ]
        elif endpoint.endswith("/issues/12/events?per_page=100"):
            value = [
                {
                    "id": 7,
                    "event": "labeled",
                    "label": {"name": "factory:hold"},
                    "actor": {"login": "grok-login"},
                    "created_at": "2026-11-12T20:00:00Z",
                },
                {
                    "id": 8,
                    "event": "head_ref_force_pushed",
                    "actor": {"login": "claude-login"},
                    "created_at": "2026-11-12T20:01:00Z",
                },
            ]
        else:
            return self.inner(argv, timeout)
        self.calls.append(call)
        return GhResult(0, json.dumps(value), "")


def live_ticket(gh: HoldGh) -> tuple[TrackedTicket, list[tuple[str, str, str]]]:
    from lib.dispatcher import TicketRef
    from lib.poll_source import LivePollSource
    from tests.test_live_poll import config

    errors: list[tuple[str, str, str]] = []
    source = LivePollSource(
        config(),
        gh_runner=gh,
        error_sink=lambda repo, ticket_id, error: errors.append((repo, ticket_id, error)),
    )
    source.begin_cycle()
    (found,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))
    return found, errors


def test_the_poll_source_reads_labels_files_and_events_uncached() -> None:
    gh = HoldGh(HOLD_FACTORY.format(ship="merge", hold="hold:\n  paths: ['src/**']\n"))

    found, errors = live_ticket(gh)

    assert errors == []
    assert found.factory.hold == HoldConfig(paths=("src/**",))
    assert found.labels == ("factory:hold", "orch:state/review")
    assert found.changed_files == ("lib/new.py", "public/old.py", "src/view/Board.tsx")
    assert found.label_events == (
        LabelEvent(
            id="7",
            action="labeled",
            label="factory:hold",
            actor="grok-login",
            created_at=found.label_events[0].created_at,
        ),
    )
    hold_calls = [
        call
        for call in gh.calls
        if any(part.endswith(("/labels?per_page=100", "/files?per_page=100", "/events?per_page=100")) for part in call)
    ]
    assert len(hold_calls) == 3
    assert all("--cache" not in call for call in hold_calls)


@pytest.mark.parametrize(
    "hold",
    ["", "hold:\n  paths: ['src/**']\n"],
)
def test_the_poll_source_skips_hold_reads_without_hold_on_ship_merge(hold: str) -> None:
    ship = "merge" if hold == "" else "handoff"
    gh = HoldGh(HOLD_FACTORY.format(ship=ship, hold=hold))

    found, errors = live_ticket(gh)

    assert errors == []
    assert found.labels is None
    assert found.changed_files is None
    assert found.label_events is None
    assert not any(
        part.endswith(("/files?per_page=100", "/events?per_page=100"))
        for call in gh.calls
        for part in call
    )


def test_a_failed_hold_read_is_recorded_and_left_unread() -> None:
    gh = HoldGh(
        HOLD_FACTORY.format(ship="merge", hold="hold: {}\n"),
        fail="/files?",
    )

    found, errors = live_ticket(gh)

    assert found.labels == ("factory:hold", "orch:state/review")
    assert found.changed_files is None
    assert found.label_events is not None
    assert [(repo, ticket_id) for repo, ticket_id, _error in errors] == [
        ("acme/widgets", "12")
    ]
