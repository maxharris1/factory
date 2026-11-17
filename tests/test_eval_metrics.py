"""#209: eval metrics read from a backed-up orch.db, printed beside a baseline."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from datetime import datetime, timezone

from lib import cli, feeder, job_detail, remote, telemetry
from lib.device_pool import DevicePool
from lib.eval_report import (
    NO_RATIO,
    WINDOWED_METRICS,
    EvalError,
    Metrics,
    Unrecorded,
    build_metrics,
    render_metrics,
)
from lib.factory_md import HoldConfig
from lib.queue import JobRequest
from lib.state_backup import backup
from lib.update_check import UpdateClass
from tests.test_dispatcher import setup
from tests.merge_queue_support import Board, sha
from tests.test_lifecycle import HEADS, Flow
from tests.test_remote_start import config_of, machines_file

AUT0 = "aut0-code/aut0"
BUILDER_ROUTER = "factory/factory-builder/SKILL.md"


def _ticket(
    connection: sqlite3.Connection,
    ticket_id: str,
    *,
    state: str,
    review_round: int = 0,
    approved: bool = False,
    repo: str = "owner/repo",
) -> None:
    connection.execute(
        """
        INSERT INTO ticket_state (
            repo, ticket_id, workflow_state, review_round, approve_comment_id
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (repo, ticket_id, state, review_round, f"approve-{ticket_id}" if approved else None),
    )


def _job(queue, ticket_id: str, action: str, key: str, *, state: str = "done") -> str:
    job = queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id=ticket_id,
            action=action,
            recipient="claude",
            source_comment_id=key,
            timeout_seconds=3600,
        ),
        dedupe_key=key,
    )
    with sqlite3.connect(queue.path) as connection:
        connection.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job.id))
    return job.id


def _telemetry(
    connection: sqlite3.Connection,
    job_id: str,
    tools: dict[str, int],
    *,
    harness: str = "claude",
    elapsed: float = 0.0,
    skill_reads: str | None = None,
) -> None:
    connection.execute(
        f"""
        INSERT INTO {telemetry.TABLE} (
            job_id, bot, harness, repo, started_at, updated_at, elapsed_seconds,
            events, turns, tool_calls, files_touched, commands, test_runs,
            tokens_in, tokens_cached, tokens_out, steps, skill_reads
        ) VALUES (?, 'bot', ?, 'owner/repo', 0, 0, ?, 0, 0, ?, 0, 0, 0, 0, 0, 0, '[]', ?)
        """,
        (job_id, harness, elapsed, json.dumps(tools), skill_reads),
    )


def _feeder_go(connection: sqlite3.Connection, issue: str, go_comment_id: str) -> None:
    connection.execute(
        f"""
        INSERT INTO {feeder.FEEDER_TABLE} (
            repo, issue, tracker, title, harness, state, risk, issue_created_at,
            ticket_id, go_comment_id, first_seen_at, seen_at
        ) VALUES ('owner/repo', ?, 'github', 't', 'claude', 'fed', 'low', 0, ?, ?, 0, 0)
        """,
        (issue, issue, go_comment_id),
    )


def _job_event(connection: sqlite3.Connection, job_id: str, event: str, detail: str) -> None:
    connection.execute(
        """
        INSERT INTO job_events (
            job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
        ) VALUES (?, 'owner/repo', '1', 'build', 'claude', ?, ?, 0)
        """,
        (job_id, event, detail),
    )


def _verdict(connection: sqlite3.Connection, ticket_id: str, comment_id: str, verdict: str) -> None:
    repo = AUT0 if ticket_id == "5" else "owner/repo"
    if verdict == "R":
        connection.execute(
            "INSERT INTO review_events VALUES (?, ?, ?, 'queued-review-fix')",
            (repo, ticket_id, comment_id),
        )
    else:
        connection.execute(
            "INSERT INTO ship_intents (repo, ticket_id, comment_id, head, policy, status) "
            "VALUES (?, ?, ?, 'h', 'handoff', 'handed-off')",
            (repo, ticket_id, comment_id),
        )
    connection.execute(
        "INSERT INTO handled_comments VALUES (?, ?, ?, 'handled')", (repo, ticket_id, comment_id)
    )


def _seeded(directory: Path) -> Path:
    _dispatcher, queue = setup(directory)
    build_1 = _job(queue, "1", "build", "b1")
    build_2 = _job(queue, "2", "build", "b2", state="failed")
    build_3 = _job(queue, "3", "build", "b3", state="cancelled")
    review = _job(queue, "1", "review", "r1")
    _job(queue, "1", "update-from-main", "u1")
    _job(queue, "1", "update-from-main", "u2")
    _job(queue, "4", "update-from-main", "u3")
    with sqlite3.connect(queue.path) as connection:
        telemetry.ensure_table(connection)
        _ticket(connection, "1", state="merged", approved=True)
        _ticket(connection, "2", state="capped", review_round=1)
        _ticket(connection, "3", state="merge-queue", review_round=2, approved=True)
        _ticket(connection, "4", state="blocked")
        _ticket(connection, "5", state="handed-off", approved=True, repo=AUT0)
        _ticket(connection, "6", state="handed-off", approved=True)
        connection.execute(
            "INSERT INTO review_events VALUES ('owner/repo', '7', 'c7', 'capped-review')"
        )
        connection.execute(
            "INSERT INTO handled_comments VALUES ('owner/repo', '1', 'v1', 'duplicate-verdict')"
        )
        connection.execute(
            "INSERT INTO handled_comments VALUES ('owner/repo', '1', 'v2', 'duplicate-verdict')"
        )
        for comment_id, refused in (
            ("a1", "rejected-approve-no-ac"),
            ("a2", "rejected-approve-ac-unmet"),
            ("a3", "rejected-approve-open-blocking"),
        ):
            connection.execute(
                "INSERT INTO handled_comments VALUES ('owner/repo', '2', ?, ?)",
                (comment_id, refused),
            )
        _telemetry(
            connection,
            build_1,
            {"Skill": 2, "Agent": 1, "Bash": 9},
            skill_reads=json.dumps([BUILDER_ROUTER, "pstack/skills/poteto-mode/SKILL.md"]),
        )
        _telemetry(
            connection, build_2, {"Skill": 1, "Task": 4, "Agent": 1}, elapsed=5400, skill_reads="[]"
        )
        _telemetry(connection, build_3, {}, elapsed=1800)
        _telemetry(connection, review, {"exec": 3}, harness="codex")
        for ticket_id, verdicts in (
            ("3", "RRA"),
            ("1", "A"),
            ("2", "R"),
            ("5", "A"),
            ("6", "AR"),
        ):
            for number, verdict in enumerate(verdicts, 1):
                _verdict(connection, ticket_id, f"{verdict}{number}", verdict)
        requeued = "; requeued, no round charged"
        for job_id, reason in (
            (build_1, "missing under bash -lc on pc: gh"),
            (build_1, "preflight on pc exited 2"),
            (build_2, "checkout failed on pc: exited 1"),
        ):
            _job_event(connection, job_id, "requeued-unstarted", reason + requeued)
        feeder.ensure_tables(connection)
        for ticket_id, comment_id, outcome in (
            ("1", "go-fed-1", "queued-build"),
            ("2", "go-fed-2", "queued-plan"),
            ("3", "go-hand", "answered-build"),
        ):
            connection.execute(
                "INSERT INTO handled_comments VALUES ('owner/repo', ?, ?, ?)",
                (ticket_id, comment_id, outcome),
            )
        _feeder_go(connection, "1", "go-fed-1")
        _feeder_go(connection, "2", "go-fed-2")
        for event in ("submitted-invalid", "submitted-invalid", "pre-ready-failed"):
            _job_event(connection, build_1, event, "bad handoff")
        connection.execute(
            "INSERT INTO review_events VALUES ('owner/repo', '8', 'stop-r', 'waiting-max-repeat')"
        )
        connection.execute(
            "INSERT INTO review_events VALUES ('owner/repo', '9', 'stop-u', 'waiting-max-unsat')"
        )
    return queue.path


def _read_only_backup(tmp_path: Path) -> Path:
    db = _seeded(tmp_path / "state")
    copy = tmp_path / "backup" / "orch.db"
    backup(db, copy)
    copy.chmod(stat.S_IRUSR)
    copy.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    return copy


def _values(metrics: Metrics) -> dict[str, object]:
    return dict(metrics.values)


def test_metrics_read_a_read_only_backup_and_leave_it_untouched(tmp_path: Path) -> None:
    copy = _read_only_backup(tmp_path)
    before = hashlib.sha256(copy.read_bytes()).hexdigest()
    try:
        values = _values(build_metrics(copy))
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == before
        assert sorted(path.name for path in copy.parent.iterdir()) == ["orch.db"]
    finally:
        copy.parent.chmod(stat.S_IRWXU)

    assert values["prs-reviewed"] == 5
    assert values["rounds-per-pr"] == pytest.approx(7 / 5)
    assert values["first-round-revise-rate"] == pytest.approx(2 / 5)
    assert values["first-pass-approve-rate"] == pytest.approx(3 / 5)
    assert values["prs-capped"] == 2
    assert values["prs-dirty-after-approval"] == 2
    assert values["duplicate-handoffs"] == 2
    assert values["blocked-stops"] == 1
    assert values["tickets-succeeded"] == 2
    assert values["router-read-share-build"] == pytest.approx(0.5)
    assert values["skill-files-per-job-build"] == pytest.approx(1.0)
    assert values["skill-reads-unrecorded-build"] == 0
    assert values["helpers-per-job"] == pytest.approx(2.0)
    assert values["helpers-per-job-max"] == 5
    assert values["preflight-failures"] == 2
    assert values["slot-hours-lost"] == pytest.approx(2.0)
    assert values["hand-written-gos"] == 1
    assert values["refused-approvals"] == 3
    assert values["submitted-invalid"] == 2
    assert values["repeat-block-stops"] == 1
    assert values["unsatisfiable-stops"] == 1


def test_hand_written_gos_without_a_feeder_table_print_unrecorded_with_the_reason(
    tmp_path: Path,
) -> None:
    db = _seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(f"DROP TABLE {feeder.FEEDER_TABLE}")
    metrics = build_metrics(db)
    assert _values(metrics)["hand-written-gos"] == Unrecorded("no-feeder_items-table")
    assert "hand-written-gos unrecorded(no-feeder_items-table) " in render_metrics(
        metrics, metrics
    )


def test_a_snapshot_without_a_source_table_reports_it_unrecorded(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE ticket_state (repo TEXT, ticket_id TEXT, workflow_state TEXT, "
            "review_round INTEGER)"
        )
        connection.execute("INSERT INTO ticket_state VALUES ('owner/repo', '1', 'fixing', 1)")
        connection.execute(
            "CREATE TABLE handled_comments (repo TEXT, ticket_id TEXT, comment_id TEXT, outcome TEXT)"
        )
    values = _values(build_metrics(db))
    assert values["prs-reviewed"] == 1
    assert values["first-round-revise-rate"] == Unrecorded("no-review_events-table")
    assert values["first-pass-approve-rate"] == Unrecorded("no-review_events-table")
    assert values["router-read-share-build"] == Unrecorded("no-job_telemetry-table")
    assert values["preflight-failures"] == Unrecorded("no-job_events-table")
    assert values["prs-dirty-after-approval"] == Unrecorded("no-jobs-table")
    assert values["refused-approvals"] == Unrecorded("no-approve_rule_floor-table")
    assert values["approved-by-round-2-share"] == Unrecorded("no-verdict_heads-table")


def test_each_metric_prints_beside_the_baseline(tmp_path: Path) -> None:
    current = build_metrics(_seeded(tmp_path / "now"))
    baseline_db = tmp_path / "o4.db"
    sqlite3.connect(baseline_db).close()
    baseline = build_metrics(baseline_db)

    lines = render_metrics(current, baseline).splitlines()

    assert lines[:4] == [
        "eval metrics",
        "window - to -",
        f"windowed {' '.join(WINDOWED_METRICS)}",
        "metric current baseline",
    ]
    end = lines.index("merged PRs with follow-ups")
    rows = {line.split()[0]: line.split()[1:] for line in lines[4:end]}
    assert set(rows) == {name for name, _value in current.values}
    assert all(len(row) == 2 for row in rows.values())
    assert rows["prs-capped"] == ["2", "unrecorded(no-ticket_state-table)"]
    assert rows["rounds-per-pr"] == ["1.40", "unrecorded(no-ticket_state-table)"]
    assert rows["submitted-invalid"] == ["2", "unrecorded(no-job_events-table)"]
    assert lines[end + 1 :] == ["none"]


def test_skill_reads_per_job_kind_come_from_files_read_not_the_skill_tool(
    tmp_path: Path,
) -> None:
    _dispatcher, queue = setup(tmp_path / "now")
    pstack = "pstack/skills/poteto-mode/SKILL.md"
    seeds = [
        (_job(queue, key, action, key), tools, reads)
        for key, action, tools, reads in (
            ("b1", "build", {}, [BUILDER_ROUTER, pstack, "cursor-team-kit/deslop/SKILL.md"]),
            ("b2", "build", {}, [pstack]),
            ("f1", "fix", {"Skill": 9}, []),
            ("f2", "fix", {}, "not json"),
            ("r1", "review", {}, ["factory/factory-reviewer/SKILL.md"]),
            ("r2", "review", {}, None),
            ("r3", "review", None, None),
            ("p1", "plan", None, None),
            ("u1", "update-from-main", {}, [pstack]),
        )
    ]
    _job(queue, "b3", "build", "b3", state="queued")
    merge_only = _job(queue, "f3", "fix", "f3")
    with sqlite3.connect(queue.path) as connection:
        connection.execute(
            "UPDATE jobs SET detail = ? WHERE id = ?", (json.dumps({"kind": "merge-only"}), merge_only)
        )
        telemetry.ensure_table(connection)
        _telemetry(connection, merge_only, {}, skill_reads=json.dumps([BUILDER_ROUTER]))
        for job_id, tools, reads in seeds:
            if tools is not None:
                raw = json.dumps(reads) if isinstance(reads, list) else reads
                _telemetry(connection, job_id, tools, skill_reads=raw)
    _dispatcher, old = setup(tmp_path / "o4")
    with sqlite3.connect(old.path) as connection:
        telemetry.ensure_table(connection)
        connection.execute(f"ALTER TABLE {telemetry.TABLE} DROP COLUMN skill_reads")

    lines = render_metrics(build_metrics(queue.path), build_metrics(old.path)).splitlines()

    rows = {
        line.split()[0]: line.split()[1:]
        for line in lines
        if line.startswith(("router-", "skill"))
    }
    none, old_none = "unrecorded(no-skill-reads)", "unrecorded(no-skill_reads-column)"
    assert rows == {
        f"{metric}-{kind}": [value, old_none]
        for kind, values in (
            ("plan", (none, none, "1")),
            ("build", ("0.50", "2.00", "0")),
            ("fix", ("0.00", "0.00", "1")),
            ("review", ("1.00", "1.00", "2")),
        )
        for metric, value in zip(
            ("router-read-share", "skill-files-per-job", "skill-reads-unrecorded"), values
        )
    }


def test_a_missing_or_corrupt_snapshot_is_an_eval_error(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="not found"):
        build_metrics(tmp_path / "absent.db")
    junk = tmp_path / "junk.db"
    junk.write_text("not sqlite\n" * 20, encoding="utf-8")
    with pytest.raises(EvalError, match="SQLite"):
        build_metrics(junk)


def test_cli_prints_metrics_beside_the_baseline(tmp_path: Path, capsys) -> None:
    copy = _read_only_backup(tmp_path)
    try:
        code = cli.main(["eval-report", "--db", str(copy), "--baseline", str(copy)])
    finally:
        copy.parent.chmod(stat.S_IRWXU)
    out = capsys.readouterr().out
    assert code == 0
    assert "prs-capped 2 2\n" in out


def test_cli_db_needs_a_baseline_and_excludes_input(tmp_path: Path, capsys) -> None:
    db = _seeded(tmp_path)
    assert cli.main(["eval-report", "--db", str(db)]) == 2
    assert cli.main(["eval-report", "--db", str(db), "--baseline", str(db), "--input", "x"]) == 2
    assert cli.main(["eval-report", "--db", str(db), "--baseline", str(db), "--json"]) == 2
    capsys.readouterr()


def _rates(db: Path) -> tuple[object, object]:
    values = _values(build_metrics(db))
    return values["first-round-revise-rate"], values["first-pass-approve-rate"]


def _backed_up(flow: Flow, tmp_path: Path) -> Path:
    copy = tmp_path / "backup" / "orch.db"
    backup(flow.queue.path, copy)
    return copy


def test_first_round_rates_survive_a_reentry_approve(tmp_path: Path) -> None:
    flow = Flow(tmp_path / "flow")
    flow.hand_off_at(1)
    assert _rates(flow.queue.path) == (0.0, 1.0)
    assert flow.ready(HEADS[5]) == "queued-review"
    flow.finish("codex")
    assert flow.approve("2/3") == "handed-off"
    assert _rates(_backed_up(flow, tmp_path)) == (0.0, 1.0)


def test_first_round_rates_survive_a_reentry_revise(tmp_path: Path) -> None:
    flow = Flow(tmp_path / "flow")
    flow.hand_off_at(1)
    assert flow.ready(HEADS[5]) == "queued-review"
    flow.finish("codex")
    assert flow.revise("2/3") == "queued-review-fix"
    assert _rates(_backed_up(flow, tmp_path)) == (0.0, 1.0)


def test_a_first_round_revise_stays_one_after_a_later_approve(tmp_path: Path) -> None:
    flow = Flow(tmp_path / "flow")
    flow.hand_off_at(2)
    assert _rates(_backed_up(flow, tmp_path)) == (1.0, 0.0)


def test_first_verdict_survives_update_rereview(tmp_path: Path) -> None:
    board = Board(tmp_path / "state")
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    assert _rates(board.queue.path) == (0.0, 1.0)
    board.verdict = UpdateClass(False, "manual conflict resolution")
    board.finish("claude")
    pr.push(sha(2))
    ready_id = board.ready(pr, sha(2))
    board.poll()
    assert board.outcome(ready_id) == "queued-review"
    assert board.state("42")["workflow_state"] == "review"
    copy = tmp_path / "backup" / "orch.db"
    backup(board.queue.path, copy)
    assert _rates(copy) == (0.0, 1.0)


def test_held_approval_is_not_dropped_from_denominator(tmp_path: Path) -> None:
    board = Board(tmp_path / "state", ship="merge", hold=HoldConfig())
    first = board.open("42", sha(1))
    first.labels = ("factory:hold",)
    board.build_and_approve(first)
    assert board.state("42")["workflow_state"] == "awaiting-ok"
    second = board.open("43", sha(2))
    board.go(second, "go-43")
    board.poll()
    board.finish("claude")
    board.ready(second, second.head)
    board.poll()
    board.finish("codex")
    board.revise(second, "1/3")
    board.poll()
    copy = tmp_path / "backup" / "orch.db"
    backup(board.queue.path, copy)
    assert _rates(copy) == (0.5, 0.5)


def test_a_held_approve_after_a_first_round_revise_stays_a_revise(tmp_path: Path) -> None:
    _dispatcher, queue = setup(tmp_path / "state")
    with sqlite3.connect(queue.path) as connection:
        _verdict(connection, "8", "R1", "R")
        connection.execute(
            "INSERT INTO ship_holds VALUES ('owner/repo', '8', 'h', 'codex-bot', '[]', 'A2', 0)"
        )
        connection.execute(
            "INSERT INTO ship_holds VALUES ('owner/repo', '9', 'h', 'codex-bot', '[]', 'A1', 0)"
        )
        connection.execute("UPDATE handled_comments SET repo = 'Owner/Repo'")
    assert _rates(queue.path) == (0.5, 0.5)


def _requeued(tmp_path: Path, fail) -> Path:
    _dispatcher, queue = setup(tmp_path)
    pool = DevicePool(queue, config_of(machines_file(tmp_path, box_enabled=False)))
    job = queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="7",
            action="build",
            recipient="claude",
            source_comment_id="go-7",
            timeout_seconds=3600,
        ),
        dedupe_key="7",
    )
    assert pool.claim("claude") is not None
    pool.attach_runner(job.id, owner="claude", pid=12345, start=None, alive=lambda *_: False)
    target = remote.RemoteTarget("pc", "fakepc-claude-bot", "claude-bot", "/tmp/work", job.id)
    with pytest.raises(remote.CheckoutError) as caught:
        fail(target)
    reason = " ".join(str(caught.value).split())
    assert pool.requeue_unstarted(job.id, owner="claude", runner=12345, reason=reason)
    return queue.path


def _ssh(code: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        call=lambda *args, **kwargs: SimpleNamespace(code=code, stdout="", stderr=stderr)
    )


@pytest.mark.parametrize(
    ("code", "stderr"),
    [
        (3, "orch-preflight: missing gh\n"),
        (remote.SSH_UNREACHABLE, ""),
        (127, ""),
        (2, ""),
    ],
)
def test_every_preflight_failure_is_counted(tmp_path: Path, code: int, stderr: str) -> None:
    db = _requeued(tmp_path, lambda target: remote.preflight(_ssh(code, stderr), target, "claude"))
    assert _values(build_metrics(db))["preflight-failures"] == 1


def test_a_checkout_failure_is_not_a_preflight_failure(tmp_path: Path) -> None:
    def fail(target) -> None:
        remote.checkout(
            _ssh(1, "orch-checkout: fetch failed\n"),
            target,
            url="https://github.com/owner/repo.git",
            head=None,
            base_branch=None,
            branch=None,
            brief=None,
        )

    assert _values(build_metrics(_requeued(tmp_path, fail)))["preflight-failures"] == 0


def _day(day: int) -> float:
    return datetime(2026, 11, day, tzinfo=timezone.utc).timestamp()


SINCE, UNTIL = "2026-11-02T00:00:00Z", "2026-11-10T00:00:00Z"
EMPTY = ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")


def _raw_job(
    connection: sqlite3.Connection,
    ticket_id: str,
    action: str,
    detail: dict[str, object],
    *,
    head: str = "",
    created: int = 3,
    handoff: str | None = None,
) -> None:
    job_id = f"{action}-{ticket_id}-{connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]}"
    connection.execute(
        """
        INSERT INTO jobs (
            id, dedupe_key, repo, ticket_id, action, recipient, source_comment_id,
            timeout_seconds, generation, attempt, head, detail, state, created_at,
            last_activity_at, handoff_comment_id
        ) VALUES (?, ?, 'owner/repo', ?, ?, 'claude', 's', 60, 1, 1, ?, ?, 'done', ?, 0, ?)
        """,
        (job_id, job_id, ticket_id, action, head, json.dumps(detail), _day(created), handoff),
    )


def _head_verdict(
    connection: sqlite3.Connection,
    ticket_id: str,
    comment_id: str,
    outcome: str,
    *,
    day: int,
    review_round: int | None,
    lines: int | None,
    head: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO verdict_heads VALUES ('owner/repo', ?, ?, ?, ?, ?, ?)",
        (ticket_id, comment_id, head or comment_id, review_round, _day(day), lines),
    )
    if outcome in ("A", "R"):
        connection.execute(
            "INSERT INTO handled_times VALUES ('owner/repo', ?, ?, ?)",
            (ticket_id, comment_id, _day(day)),
        )
    if outcome == "A":
        connection.execute(
            "INSERT INTO ship_intents (repo, ticket_id, comment_id, head, policy, status) "
            "VALUES ('owner/repo', ?, ?, 'h', 'handoff', 'handed-off')",
            (ticket_id, comment_id),
        )
    elif outcome == "R":
        connection.execute(
            "INSERT INTO review_events VALUES ('owner/repo', ?, ?, 'queued-review-fix')",
            (ticket_id, comment_id),
        )


def _transition(connection: sqlite3.Connection, ticket_id: str, day: int) -> None:
    connection.execute(
        "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
        "VALUES ('owner/repo', ?, 'merge-queue', 'merged', ?, 'poll')",
        (ticket_id, _day(day)),
    )


def _followup(connection: sqlite3.Connection, ticket_id: str, status: str, issue: int) -> None:
    connection.execute(
        """
        INSERT INTO followup_filings (
            repo, ticket_id, comment_id, round, title, body, labels, status,
            issue_number, created_at
        ) VALUES ('owner/repo', ?, ?, 4, 't', 'b', '[]', ?, ?, 0)
        """,
        (ticket_id, f"f{ticket_id}", status, issue),
    )


def _loop_db(directory: Path) -> Path:
    _dispatcher, queue = setup(directory)
    with sqlite3.connect(queue.path) as connection:
        for ticket_id, lines, verdicts in (
            ("10", 100, (("A", 1),)),
            ("11", 150, (("R", 1), ("A", 2))),
            ("12", 400, (("R", 1), ("R", 2), ("A", 3))),
            ("13", 800, (("R", 1),)),
            ("14", None, (("R", None), ("R", None), ("A", None))),
        ):
            for day, (outcome, review_round) in enumerate(verdicts, 2):
                _head_verdict(
                    connection,
                    ticket_id,
                    f"v{ticket_id}-{day}",
                    outcome,
                    day=day,
                    review_round=review_round,
                    lines=lines,
                )
        _head_verdict(connection, "15", "v15", "A", day=1, review_round=1, lines=10)
        _raw_job(connection, "10", "review", {"risk": "high"}, created=2, handoff="v10-2")
        _raw_job(connection, "10", "fix", {"risk": "low"}, created=4)
        _raw_job(connection, "11", "build", {"risk": "low"})
        _raw_job(connection, "12", "build", {"risk": "high"})
        for ticket_id, day in (("14", 5), ("10", 6), ("15", 1)):
            _transition(connection, ticket_id, day)
        _followup(connection, "14", "filed", 901)
        _followup(connection, "10", "pending", 902)
        _followup(connection, "16", "filed", 903)
    return queue.path


def test_review_loop_measures_cover_the_prs_first_reviewed_in_the_window(
    tmp_path: Path,
) -> None:
    metrics = build_metrics(_loop_db(tmp_path), since=SINCE, until=UNTIL)
    values = _values(metrics)

    assert values["approved-by-round-2-share"] == pytest.approx(2 / 5)
    assert values["verdicts-per-merged-pr"] == pytest.approx(4 / 2)
    assert values["first-round-approve-rate-high-risk"] == pytest.approx(1 / 2)
    assert values["first-round-approve-rate-lines-under-150"] == pytest.approx(1.0)
    assert values["first-round-approve-rate-lines-150-399"] == pytest.approx(0.0)
    assert values["first-round-approve-rate-lines-400-799"] == pytest.approx(0.0)
    assert values["first-round-approve-rate-lines-800-plus"] == pytest.approx(0.0)
    assert values["first-round-approve-rate-lines-unknown"] == pytest.approx(0.0)
    assert metrics.followups == (
        ("owner/repo", "14", "https://github.com/owner/repo/issues/901"),
    )
    text = render_metrics(metrics, metrics)
    assert "window 2026-11-02T00:00:00Z to 2026-11-10T00:00:00Z\n" in text
    assert text.endswith(
        "merged PRs with follow-ups\nowner/repo 14 https://github.com/owner/repo/issues/901\n"
    )


def test_without_a_window_the_earlier_pr_joins_the_cohort(tmp_path: Path) -> None:
    values = _values(build_metrics(_loop_db(tmp_path)))
    assert values["approved-by-round-2-share"] == pytest.approx(3 / 6)
    assert values["verdicts-per-merged-pr"] == pytest.approx(5 / 3)
    assert values["first-round-approve-rate-lines-under-150"] == pytest.approx(1.0)


def test_ci_fixes_without_a_log_and_verdicts_on_an_accepted_head_are_counted(
    tmp_path: Path,
) -> None:
    _dispatcher, queue = setup(tmp_path)
    with sqlite3.connect(queue.path) as connection:
        _raw_job(connection, "20", "fix", {"instruction": "CI failed on h1"}, head="h1")
        _raw_job(connection, "20", "fix", {"instruction": "E   assert 1 == 2"}, head="h2")
        _raw_job(connection, "20", "fix", {"instruction": "address the review"}, head="h2")
        _raw_job(connection, "20", "fix", {"instruction": "CI failed on h3"}, head="h3", created=1)
        for day, outcome, head in ((2, "R", "a" * 7), (3, "-", "a" * 40), (4, "-", "a" * 40)):
            _head_verdict(
                connection, "21", f"x{day}", outcome, day=day, review_round=1, lines=1, head=head
            )
        for day in (2, 3):
            _head_verdict(
                connection, "22", f"y{day}", "-", day=day, review_round=1, lines=1, head="b" * 40
            )
    values = _values(build_metrics(queue.path, since=SINCE, until=UNTIL))
    assert values["ci-fix-without-log"] == 1
    assert values["verdicts-on-accepted-head"] == 2


def test_ci_fixes_and_hand_written_gos_read_detail_moved_out_of_the_database(
    tmp_path: Path,
) -> None:
    _dispatcher, queue = setup(tmp_path)
    resumed = {"evidence": {"instructions": {"cause": "go"}}}
    with sqlite3.connect(queue.path) as connection:
        _raw_job(connection, "20", "fix", {"instruction": "CI failed on h1"}, head="h1")
        _raw_job(connection, "20", "fix", {"instruction": "failed: pytest"}, head="h2")
        _raw_job(connection, "20", "fix", {"instruction": "address the review"}, head="h2")
        _raw_job(connection, "21", "build", resumed)
        feeder.ensure_tables(connection)
        _feeder_go(connection, "99", "other-go")
        connection.execute(
            "INSERT INTO handled_comments (repo, ticket_id, comment_id, outcome) "
            "VALUES ('owner/repo', '21', 's', 'resumed-build')"
        )
    with queue.connect() as connection:
        assert job_detail.spill_existing(connection) == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE json_type(detail, '$.instruction') IS NOT NULL"
            " OR json_type(detail, '$.evidence') IS NOT NULL"
        ).fetchone()[0] == 0
    values = _values(build_metrics(queue.path, since=SINCE, until=UNTIL))
    assert values["ci-fix-without-log"] == 2
    assert _values(build_metrics(queue.path))["hand-written-gos"] == 1


@pytest.mark.parametrize("seed", [_seeded, _loop_db, lambda path: setup(path)[1].path])
def test_an_empty_window_prints_zero_counts_and_no_ratio(seed, tmp_path: Path) -> None:
    metrics = build_metrics(seed(tmp_path), since=EMPTY[0], until=EMPTY[1])
    values = _values(metrics)
    windowed = {name: values[name] for name in WINDOWED_METRICS}
    counts = {"submitted-invalid", "repeat-block-stops", "unsatisfiable-stops"}
    counts |= {"ci-fix-without-log", "verdicts-on-accepted-head", "hand-written-gos"}
    for name, value in windowed.items():
        if name in counts:
            assert value in (0, Unrecorded("no-feeder_items-table")), name
        else:
            assert value is NO_RATIO, name
    assert metrics.followups == ()
    text = render_metrics(metrics, metrics)
    assert "approved-by-round-2-share - -\n" in text
    assert text.endswith("merged PRs with follow-ups\nnone\n")


def test_cli_windows_both_snapshots(tmp_path: Path, capsys) -> None:
    db = _loop_db(tmp_path)
    args = ["eval-report", "--db", str(db), "--baseline", str(db)]
    assert cli.main([*args, "--since", SINCE, "--until", UNTIL]) == 0
    assert "approved-by-round-2-share 0.40 0.40\n" in capsys.readouterr().out
    assert cli.main([*args, "--since", EMPTY[0], "--until", EMPTY[1]]) == 0
    out = capsys.readouterr().out
    assert "verdicts-per-merged-pr - -\n" in out
    assert "ci-fix-without-log 0 0\n" in out
