from __future__ import annotations

from pathlib import Path

from lib.dashboard import collect_snapshot
from lib.dispatch.constants import (
    CI_INFRA_BLOCKED_REASON,
    CI_INFRA_WAIT_SECONDS,
    CI_STALE_REASON,
    CI_STALE_SECONDS,
)
from lib.dispatch.types import BaseCheck
from tests.merge_queue_support import PR, sha
from tests.test_ci_rerun import RerunBoard, base_red_ready, built, stale_rows
from tests.test_ready_ci_gate import after_a_ci_fix, needs_attention


def stale(board: RerunBoard) -> list[str]:
    return [detail.split(" for ")[0] for _reason, detail in stale_rows(board)]


def on(state: str, head: str) -> str:
    return f"CI {state} on {head[:12]}"


def wakes(board: RerunBoard) -> int:
    return len(board.jobs(CI_STALE_REASON))


def dashboard(board: RerunBoard, tmp_path: Path) -> dict[str, int]:
    snapshot = collect_snapshot(tmp_path / "d.db", tmp_path / "no-log", now=board.now[0])
    return snapshot["ci_attention"]


def two_hours_later(board: RerunBoard) -> list[str]:
    board.now[0] += CI_STALE_SECONDS + 1
    board.poll()
    return stale(board)


def past_the_edge(board: RerunBoard, waited: float = 0.0) -> None:
    before = wakes(board)
    board.now[0] += CI_STALE_SECONDS - waited - 100
    board.poll()
    assert stale(board) == []
    assert wakes(board) == before
    board.now[0] += 200
    board.poll()


def stays_at_one(board: RerunBoard, item: str) -> None:
    for _ in range(3):
        assert two_hours_later(board) == [item]
        assert wakes(board) == 1


def red_with_its_fix_queued(tmp_path: Path) -> tuple[RerunBoard, PR]:
    board, pr = built(tmp_path)
    board.red(pr, pr.head, "11")
    board.ready(pr, pr.head)
    board.poll()
    board.red(pr, pr.head, "12")
    board.poll()
    assert [job["state"] for job in board.jobs("fix")] == ["queued"]
    return board, pr


def red_after_its_fix(tmp_path: Path) -> tuple[RerunBoard, PR]:
    board, pr = red_with_its_fix_queued(tmp_path)
    board.finish("claude")
    board.poll()
    return board, pr


def raised_on_a_red_head(tmp_path: Path) -> tuple[RerunBoard, PR]:
    board, pr = red_after_its_fix(tmp_path)
    assert two_hours_later(board) == [on("ci-failed", pr.head)]
    return board, pr


def test_a_red_head_nobody_is_fixing_raises_one_stale_item_after_two_hours(tmp_path: Path) -> None:
    board, pr = red_after_its_fix(tmp_path)
    past_the_edge(board)

    assert stale(board) == [on("ci-failed", pr.head)]
    assert wakes(board) == 1
    stays_at_one(board, on("ci-failed", pr.head))


def test_a_fix_queued_or_running_on_the_red_head_raises_no_stale_item(tmp_path: Path) -> None:
    board, pr = red_with_its_fix_queued(tmp_path)
    board.now[0] += 3000
    board.poll()
    fix = board.claim("claude")
    for _ in range(4):
        board.now[0] += 3000
        board.queue.heartbeat(fix.id, owner="claude")
        board.poll()

    assert stale(board) == []
    assert wakes(board) == 0

    board.queue.finish(fix.id, owner="claude", handoff_comment_id="handoff-fix")
    board.poll()

    assert two_hours_later(board) == [on("ci-failed", pr.head)]


def test_a_stuck_red_ticket_keeps_one_stale_item_across_later_polls(tmp_path: Path) -> None:
    board, pr = built(tmp_path)
    for number in (1, 2, 3):
        pr.push(sha(number))
        board.red(pr, sha(number), f"{number}1")
        board.ready(pr, sha(number))
        board.poll()
        board.red(pr, sha(number), f"{number}2")
        board.poll()
        if number < 3:
            board.finish("claude")

    assert two_hours_later(board) == [on("ci-red-3", sha(3))]
    assert wakes(board) == 1
    stays_at_one(board, on("ci-red-3", sha(3)))


def test_an_infra_wait_is_flagged_blocked_then_stale_two_hours_after_it_began(
    tmp_path: Path,
) -> None:
    board, pr, _ready = after_a_ci_fix(tmp_path, steps=0)
    board.now[0] += CI_INFRA_WAIT_SECONDS
    board.poll()
    assert [reason for reason, _ in needs_attention(board)] == [CI_INFRA_BLOCKED_REASON]

    past_the_edge(board, CI_INFRA_WAIT_SECONDS)

    assert sorted(reason for reason, _ in needs_attention(board)) == [
        CI_INFRA_BLOCKED_REASON,
        CI_STALE_REASON,
    ]
    assert stale(board) == [on("ci-infra-wait", pr.head)]
    assert wakes(board) == 1


def test_a_base_red_hold_raises_one_stale_item_at_the_edge_and_keeps_it(tmp_path: Path) -> None:
    board, pr, _ready = base_red_ready(tmp_path)
    past_the_edge(board)

    assert stale(board) == [on("ci-base-red", pr.head)]
    assert wakes(board) == 1
    stays_at_one(board, on("ci-base-red", pr.head))


def test_a_new_red_head_restarts_the_stale_clock_and_wakes_again(tmp_path: Path) -> None:
    board, pr, _ready = base_red_ready(tmp_path)
    assert two_hours_later(board) == [on("ci-base-red", sha(1))]

    pr.push(sha(2))
    board.red(pr, sha(2), "11", cause="code")
    board.ready(pr, sha(2))
    board.poll()
    assert stale(board) == []
    past_the_edge(board)

    assert stale(board) == [on("ci-base-red", sha(2))]
    assert wakes(board) == 2


def test_a_base_red_hold_that_ends_in_a_plain_red_raises_its_own_item(tmp_path: Path) -> None:
    board, pr, _ready = base_red_ready(tmp_path)
    assert two_hours_later(board) == [on("ci-base-red", pr.head)]
    assert dashboard(board, tmp_path) == {"base_red": 1, "stale": 1}

    board.base = {"pytest-11": BaseCheck("pytest-11", "success", "902", "903")}
    board.poll()
    board.red(pr, pr.head, "21", cause="code")
    board.base = {"pytest-21": BaseCheck("pytest-21", "success", "904", "905")}
    board.poll()
    board.finish("claude")
    board.poll()
    assert stale(board) == []
    assert dashboard(board, tmp_path) == {"base_red": 0, "stale": 0}
    past_the_edge(board)

    assert stale(board) == [on("ci-failed", pr.head)]
    assert dashboard(board, tmp_path) == {"base_red": 0, "stale": 1}
    assert wakes(board) == 2


def test_a_green_head_drops_the_stale_item_for_good(tmp_path: Path) -> None:
    board, pr = raised_on_a_red_head(tmp_path)
    pr.ci[pr.head] = "success"
    board.poll()
    assert stale(board) == []
    assert two_hours_later(board) == []
    assert wakes(board) == 1


def test_an_abandoned_ticket_drops_the_stale_item_for_good(tmp_path: Path) -> None:
    board, pr = raised_on_a_red_head(tmp_path)
    pr.closed = True
    board.poll()
    assert board.state("42")["workflow_state"] == "abandoned"
    assert stale(board) == []
    assert two_hours_later(board) == []
    assert wakes(board) == 1


def test_a_head_back_in_its_first_stale_state_is_flagged_again_with_no_second_wake(
    tmp_path: Path,
) -> None:
    board, pr, _ready = base_red_ready(tmp_path)
    assert two_hours_later(board) == [on("ci-base-red", pr.head)]
    assert wakes(board) == 1
    board.base = {}
    board.poll()
    assert stale(board) == []

    board.base_red("pytest-21")
    board.red(pr, pr.head, "21", cause="code")
    board.poll()

    assert two_hours_later(board) == [on("ci-base-red", pr.head)]
    assert wakes(board) == 1
