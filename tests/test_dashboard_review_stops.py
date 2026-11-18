"""#822: a ticket a review capped keeps its actions on the dashboard, driven
through real dispatcher flows."""

from pathlib import Path

from lib.dashboard import collect_snapshot
from tests.test_dispatcher import VERDICT
from tests.test_rounds_and_caps import HEADS, Flow


def capped_flow(tmp_path: Path) -> Flow:
    flow = Flow(tmp_path)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    for number in (1, 2, 3):
        assert flow.ready(HEADS[number]) == "queued-review"
        flow.finish("codex")
        flow.post(
            str(9100 + number),
            VERDICT.format(round=f"{number}/3", head=flow.head, verdict="REVISE", risk="high"),
            "codex-login",
        )
        if number < 3:
            flow.finish("claude")
    assert flow.state()["workflow_state"] == "capped"
    return flow


def snapshot(tmp_path: Path, flow) -> dict:
    log = tmp_path / "poll.log"
    log.write_text("")
    return collect_snapshot(flow.queue.path, log, now=flow.now[0])


def ticket(snap: dict) -> dict:
    return next(
        row for row in snap["tickets"]
        if row["repo"] == "owner/repo" and row["ticket_id"] == "42"
    )


def test_a_ready_after_the_cap_keeps_the_capped_actions_in_attention(tmp_path: Path) -> None:
    flow = capped_flow(tmp_path)
    assert flow.ready(HEADS[4]) == "capped-ready"
    snap = snapshot(tmp_path, flow)
    row = ticket(snap)
    items = [
        item for item in snap["attention"]["items"]
        if item.get("repo") == "owner/repo" and item.get("ticket_id") == "42"
    ]
    assert row["actions"] == ["another-round", "close"]
    assert [(item["kind"], item.get("wait"), item.get("actions")) for item in items] == [
        ("needs-attention", row["wait"], row["actions"]),
    ]
