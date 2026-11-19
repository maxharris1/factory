from pathlib import Path

from lib.dashboard import collect_snapshot
from lib.dispatch.constants import CI_STALE_REASON
from tests.merge_queue_support import PR, sha
from tests.test_ci_rerun import CI_STALE_SECONDS, RerunBoard, handed_off, stale_rows, stopped_poll


def held_against_red_base(tmp_path: Path) -> tuple[RerunBoard, PR]:
    board, pr = handed_off(tmp_path)
    board.base_red("pytest-31")
    pr.push(sha(2))
    board.red(pr, sha(2), "31")
    stopped_poll(board)
    assert board.ci_wait() == ["ci-base-red"]
    return board, pr


def assert_no_ci_attention_after_the_stale_timer(board: RerunBoard, tmp_path: Path) -> None:
    board.now[0] += CI_STALE_SECONDS + 1
    stopped_poll(board)
    snapshot = collect_snapshot(tmp_path / "d.db", tmp_path / "no-log", now=board.now[0])
    assert board.ci_wait() == []
    assert stale_rows(board) == []
    assert board.jobs(CI_STALE_REASON) == []
    assert snapshot["ci_attention"] == {"base_red": 0, "stale": 0}


def test_recovered_unannounced_base_hold_clears_wait(tmp_path: Path) -> None:
    board, pr = held_against_red_base(tmp_path)

    pr.ci[sha(2)] = "success"
    stopped_poll(board)

    assert board.ci_wait() == []
    assert_no_ci_attention_after_the_stale_timer(board, tmp_path)


def test_a_base_red_hold_clears_when_the_head_moves(tmp_path: Path) -> None:
    board, pr = held_against_red_base(tmp_path)

    pr.push(sha(3))
    stopped_poll(board)

    assert board.ci_wait() == []
    assert_no_ci_attention_after_the_stale_timer(board, tmp_path)
