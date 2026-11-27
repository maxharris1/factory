from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.dashboard import collect_snapshot  # noqa: E402
from lib.dispatcher import Dispatcher  # noqa: E402
from lib.queue import SQLiteJobQueue  # noqa: E402

REPO = "maxharris1/factory"
JOBS_PER_TICKET = 10


def build(db: Path, *, jobs: int, detail_kb: int, now: float) -> int:
    queue = SQLiteJobQueue(db, clock=lambda: now)
    Dispatcher(db, queue, trusted_logins={"github": {"maxharris1": {"grok-bot"}}},
               review_actions=object(), grok_delivery=object())
    tickets = math.ceil(jobs / JOBS_PER_TICKET)
    detail = json.dumps({"round": "2/3", "pad": "x" * (detail_kb * 1024)})
    with queue.connect() as connection:
        connection.executemany(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) VALUES (?, ?, 'github', ?)",
            [(REPO, str(t + 1), now - 86400) for t in range(tickets)],
        )
        connection.executemany(
            """
            INSERT INTO jobs (id, dedupe_key, repo, ticket_id, action, recipient, source_comment_id,
                timeout_seconds, generation, attempt, detail, state, created_at, last_activity_at,
                finished_at, handoff_comment_id)
            VALUES (?, ?, ?, ?, 'review', 'claude', ?, 5400, 1, 1, ?, 'done', ?, ?, ?, ?)
            """,
            [
                (f"job-{i}", f"dedupe-{i}", REPO, str(i // JOBS_PER_TICKET + 1), f"comment-{i}", detail,
                 now - 7200 - i, now - 3600 - i, now - 3600 - i, f"handoff-{i}")
                for i in range(jobs)
            ],
        )
    return tickets


def traced_snapshot(db: Path, log: Path, now: float) -> tuple[list[tuple[float, str]], float]:
    trace: list[tuple[float, str]] = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(lambda sql: trace.append((time.perf_counter(), sql)))
        return connection

    sqlite3.connect = connect
    try:
        collect_snapshot(db, log, now=now)
        end = time.perf_counter()
    finally:
        sqlite3.connect = real_connect
    return sorted(trace, key=lambda entry: entry[0]), end


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=2000)
    parser.add_argument("--detail-kb", type=int, default=124)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--statements", type=int, default=0, help="print the N longest statement gaps from one traced run")
    args = parser.parse_args()
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        db, log = Path(tmp) / "orch.db", Path(tmp) / "poller.log"
        tickets = build(db, jobs=args.jobs, detail_kb=args.detail_kb, now=now)
        size = sum(path.stat().st_size for path in (db, db.with_name(db.name + "-wal")) if path.exists())
        print(f"database: {size / 2**20:.1f} MiB, {args.jobs} jobs, {args.detail_kb} KB detail, {tickets} tracked tickets")
        times = []
        for _ in range(args.runs):
            start = time.perf_counter()
            collect_snapshot(db, log, now=now)
            times.append(time.perf_counter() - start)
        each = ", ".join(f"{t:.2f} s" for t in times)
        print(f"collect_snapshot: best {min(times):.2f} s of {args.runs} runs ({each})")
        if args.statements:
            trace, end = traced_snapshot(db, log, now)
            starts = [start for start, _ in trace][1:] + [end]
            gaps = [
                (following - start, sql, index == len(trace) - 1)
                for index, ((start, sql), following) in enumerate(zip(trace, starts))
            ]
            print(f"longest {args.statements} of {len(trace)} statement gaps (start to next statement start):")
            for gap, sql, last in sorted(gaps, key=lambda g: g[0], reverse=True)[: args.statements]:
                text = " ".join(sql.split())[:160]
                print(f"  {gap:.3f} s  {text}{'  (to end of snapshot)' if last else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
