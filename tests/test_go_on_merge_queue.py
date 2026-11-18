"""#130: a trusted go on an approved, watched ticket is honored; on a
ready-for-review ticket it still changes nothing (#108)."""

from __future__ import annotations

from pathlib import Path

from tests.merge_queue_support import REPO, Board, sha


def queued(tmp_path: Path, **options):
    board = Board(tmp_path, **options)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    return board, pr


def test_go_on_a_merge_queue_ticket_queues_the_build_and_leaves_the_queue(
    tmp_path: Path,
) -> None:
    board, pr = queued(tmp_path)
    assert board.state("42")["workflow_state"] == "merge-queue"
    builds = len(board.jobs("build"))

    board.go(pr, "go-again")
    board.poll()

    assert board.outcome("go-again") == "queued-build"
    assert len(board.jobs("build")) == builds + 1
    row = board.state("42")
    assert row["workflow_state"] == "building"
    assert row["approved_head"] is None
    assert row["approve_comment_id"] is None

    pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()
    assert board.actions.merges == []


def test_go_while_updating_cancels_the_queued_update(tmp_path: Path) -> None:
    board, pr = queued(tmp_path)
    pr.conflict()
    board.poll()
    [update] = board.jobs("update-from-main")
    assert update["state"] == "queued"

    board.go(pr, "go-plan", state="planning")
    board.poll()

    assert board.outcome("go-plan") == "queued-plan"
    assert board.jobs("update-from-main")[0]["state"] == "cancelled"
    assert board.state("42")["workflow_state"] == "planning"
    board.poll()
    assert len(board.jobs("update-from-main")) == 1


def test_a_replayed_go_queues_no_second_job(tmp_path: Path) -> None:
    board, pr = queued(tmp_path)
    board.go(pr, "go-again")
    board.poll()
    builds = len(board.jobs("build"))
    with board.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'go-again'")
    board.poll()
    assert len(board.jobs("build")) == builds
    assert board.state("42")["workflow_state"] == "building"


def test_a_replayed_go_retries_the_failed_label_write(tmp_path: Path) -> None:
    board, pr = queued(tmp_path)
    builds = len(board.jobs("build"))
    board.actions.state_failures.add((REPO, "building"))
    board.go(pr, "go-again")
    board.poll()
    assert board.outcome("go-again") is None
    board.actions.state_failures.clear()

    board.poll()

    assert len(board.jobs("build")) == builds + 1
    assert board.state("42")["workflow_state"] == "building"
    assert board.actions.states[-1] == "building"


def test_an_untrusted_go_is_rejected_and_the_ticket_stays_queued(tmp_path: Path) -> None:
    board, pr = queued(tmp_path)
    board.go(pr, "go-stranger", author="stranger")
    board.poll()
    assert board.outcome("go-stranger") == "rejected-go-author"
    row = board.state("42")
    assert row["workflow_state"] == "merge-queue"
    assert row["approved_head"] == sha(1)


def test_go_on_a_ready_for_review_handoff_still_changes_nothing(tmp_path: Path) -> None:
    board, pr = queued(tmp_path, finish_line="ready-for-review")
    assert board.state("42")["workflow_state"] == "handed-off"
    builds = len(board.jobs("build"))
    board.go(pr, "go-again")
    board.dispatcher.process(board.snapshot(pr))
    assert board.outcome("go-again") == "go-after-handoff"
    assert len(board.jobs("build")) == builds
    assert board.state("42")["workflow_state"] == "handed-off"


def test_a_builder_ready_at_a_new_head_re_enters_review(tmp_path: Path) -> None:
    board, pr = queued(tmp_path)
    pr.push(sha(2))
    ready = board.ready(pr, sha(2))
    board.poll()
    assert board.outcome(ready) == "queued-review"
    row = board.state("42")
    assert row["workflow_state"] == "review"
    assert row["approved_head"] is None


def test_a_ready_at_the_approved_head_is_already_shipped(tmp_path: Path) -> None:
    board, pr = queued(tmp_path)
    ready = board.ready(pr, sha(1), comment_id="ready-again")
    board.poll()
    assert board.outcome(ready) == "ready-already-shipped"
    assert board.state("42")["workflow_state"] == "merge-queue"


def test_go_on_an_approved_after_ticket_in_the_queue_is_honored(tmp_path: Path) -> None:
    board, pr = queued(tmp_path, ship="after:tracy-site")
    assert board.state("42")["workflow_state"] == "waiting-external"
    board.go(pr, "go-again")
    board.poll()
    assert board.outcome("go-again") == "queued-build"
    assert board.state("42")["approved_head"] is None


def test_go_on_a_ready_for_review_after_ticket_keeps_its_generation_key(tmp_path: Path) -> None:
    board, pr = queued(tmp_path, ship="after:tracy-site", finish_line="ready-for-review")
    assert board.state("42")["workflow_state"] == "waiting-external"
    builds = len(board.jobs("build"))
    board.go(pr, "go-again")
    board.poll()
    assert board.outcome("go-again") == "deduped-build"
    assert len(board.jobs("build")) == builds
