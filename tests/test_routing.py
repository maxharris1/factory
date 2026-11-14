"""Routing for throughput (issue #85 B): least-loaded placement and claim order."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lib.device_pool import DevicePool, choose_device
from lib.machines_yml import load_machines_yml
from lib.queue import PRIORITY_TABLE, JobRequest, SQLiteJobQueue, set_ticket_priority


def machines(tmp_path: Path, devices: list[tuple[str, int, bool]]):
    lines = ["devices:"]
    for name, slots, local in devices:
        lines += [f"  - name: {name}"]
        if local:
            lines += ["    local: true"]
        else:
            lines += [
                f"    tailscale_name: {name}.example.ts.net",
                "    workdir_root: /home/{bot}/factory-work",
                f"    ssh_alias: {name}-{{bot}}",
                "    factory_root: /home/{bot}/factory",
            ]
        lines += [f"    slot_max: {slots}", "    harnesses: {claude: claude-bot, codex: codex-bot}"]
    path = tmp_path / "machines.yml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config, findings = load_machines_yml(path, allow_placeholders=True)
    assert config is not None, findings
    return config


# --- least-loaded placement --------------------------------------------------


def test_choose_device_picks_the_device_with_the_most_free_slots(tmp_path: Path) -> None:
    config = machines(tmp_path, [("box", 3, True), ("pc", 6, False), ("mac", 4, False)])
    assert choose_device(config, "claude", {}).name == "pc"
    assert choose_device(config, "claude", {"pc": 3}).name == "mac"  # 3 free on pc, 4 on mac
    assert choose_device(config, "claude", {"pc": 4, "mac": 2}).name == "box"  # 3 > 2 = 2


def test_ties_go_to_the_earlier_device_in_machines_yml(tmp_path: Path) -> None:
    config = machines(tmp_path, [("box", 3, True), ("pc", 6, False), ("mac", 4, False)])
    assert choose_device(config, "claude", {"pc": 3}).name == "mac"  # mac 4 > box 3 = pc 3
    assert choose_device(config, "claude", {"pc": 3, "mac": 1}).name == "box"  # all tie at 3
    assert choose_device(config, "claude", {"pc": 4, "mac": 2}).name == "box"  # box 3, pc 2, mac 2


def test_a_full_device_is_never_chosen(tmp_path: Path) -> None:
    config = machines(tmp_path, [("box", 1, True), ("pc", 1, False)])
    assert choose_device(config, "claude", {"box": 1, "pc": 1}) is None
    assert choose_device(config, "claude", {"box": 1}).name == "pc"


def test_pool_claims_spread_jobs_across_devices(tmp_path: Path) -> None:
    config = machines(tmp_path, [("box", 3, True), ("pc", 6, False), ("mac", 4, False)])
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    for n in range(6):
        queue.enqueue(
            JobRequest(repo="o/r", ticket_id=str(n), action="build", recipient="claude",
                       source_comment_id=f"c{n}", timeout_seconds=600),
            dedupe_key=f"k{n}",
        )
    pool = DevicePool(queue, config)
    devices = [pool.claim("claude")[1].device for _ in range(6)]
    # free (box, pc, mac): (3,6,4) pc, (3,5,4) pc, (3,4,4) pc on the tie,
    # (3,3,4) mac, (3,3,3) box on the tie, (2,3,3) pc.
    assert devices == ["pc", "pc", "pc", "mac", "box", "pc"]
    assert pool.used() == {"pc": 4, "mac": 1, "box": 1}


# --- claim order: urgent, then a resumed paused step, then bug, then oldest ---


def _enqueue(queue: SQLiteJobQueue, key: str, *, ticket: str, detail: dict | None = None,
             action: str = "build") -> str:
    return queue.enqueue(
        JobRequest(repo="o/r", ticket_id=ticket, action=action, recipient="claude",
                   source_comment_id=f"c-{key}", timeout_seconds=600,
                   detail=json.dumps(detail) if detail is not None else None),
        dedupe_key=key,
    ).id


def _ordered_queue(tmp_path: Path, now: list[float]):
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: now[0])
    ids = {}
    now[0] = 1.0
    ids["oldest"] = _enqueue(queue, "a", ticket="1")
    now[0] = 2.0
    ids["bug"] = _enqueue(queue, "b", ticket="2")
    now[0] = 3.0
    ids["resume"] = _enqueue(queue, "c", ticket="3", detail={"resume": True})
    now[0] = 4.0
    ids["urgent"] = _enqueue(queue, "d", ticket="4")
    now[0] = 5.0
    ids["newest"] = _enqueue(queue, "e", ticket="5")
    with queue.connect() as connection:
        set_ticket_priority(connection, "o/r", "2", urgent=False, bug=True, at=now[0])
        set_ticket_priority(connection, "o/r", "4", urgent=True, bug=False, at=now[0])
    return queue, ids


def test_queue_claim_order_is_urgent_resume_bug_then_oldest(tmp_path: Path) -> None:
    now = [0.0]
    queue, ids = _ordered_queue(tmp_path, now)
    claimed = [queue.claim("claude").id for _ in range(5)]
    assert claimed == [ids["urgent"], ids["resume"], ids["bug"], ids["oldest"], ids["newest"]]


def test_pool_claim_order_is_urgent_resume_bug_then_oldest(tmp_path: Path) -> None:
    now = [0.0]
    queue, ids = _ordered_queue(tmp_path, now)
    config = machines(tmp_path, [("box", 5, True)])
    pool = DevicePool(queue, config)
    claimed = [pool.claim("claude")[0].id for _ in range(5)]
    assert claimed == [ids["urgent"], ids["resume"], ids["bug"], ids["oldest"], ids["newest"]]


def test_claim_order_works_on_a_database_without_the_priority_table(tmp_path: Path) -> None:
    now = [0.0]
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: now[0])
    with queue.connect() as connection:
        connection.execute(f"DROP TABLE IF EXISTS {PRIORITY_TABLE}")
    now[0] = 1.0
    first = _enqueue(queue, "a", ticket="1")
    now[0] = 2.0
    resumed = _enqueue(queue, "b", ticket="2", detail={"resume": True})
    assert [queue.claim("claude").id for _ in range(2)] == [resumed, first]


def test_t5_answers_requeue_is_marked_as_a_resumed_paused_step(tmp_path: Path) -> None:
    from lib.dispatcher import CIResult
    from tests.test_dispatcher import ANSWERS, GO, QUESTIONS, comment, setup, ticket

    dispatcher, queue = setup(tmp_path)
    go = comment("go", GO.format(state="building"), author="grok-login", created_at=1)
    dispatcher.process(ticket(go))
    build = queue.claim("claude")
    assert build is not None
    questions = comment("q", QUESTIONS, author="claude-login", created_at=2)
    dispatcher.process(ticket(go, questions))
    answers = comment("a", ANSWERS, author="grok-login", created_at=3)
    dispatcher.process(ticket(go, questions, answers))
    resumed = [job for job in queue.outstanding() if job.state == "queued"
               and job.request.recipient == "claude"]
    assert resumed, queue.outstanding()
    assert json.loads(resumed[-1].request.detail)["resume"] is True
