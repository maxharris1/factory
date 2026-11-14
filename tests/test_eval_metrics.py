"""#209: eval metrics read from a backed-up orch.db, printed beside a baseline."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import cli, remote, telemetry
from lib.device_pool import DevicePool
from lib.eval_report import EvalError, Metrics, build_metrics, render_metrics
from lib.queue import JobRequest
from lib.state_backup import backup
from tests.test_dispatcher import setup
from tests.test_lifecycle import HEADS, Flow
from tests.test_remote_start import config_of, machines_file

AUT0 = "aut0-code/aut0"


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
) -> None:
    connection.execute(
        f"""
        INSERT INTO {telemetry.TABLE} (
            job_id, bot, harness, repo, started_at, updated_at, elapsed_seconds,
            events, turns, tool_calls, files_touched, commands, test_runs,
            tokens_in, tokens_cached, tokens_out, steps
        ) VALUES (?, 'bot', ?, 'owner/repo', 0, 0, ?, 0, 0, ?, 0, 0, 0, 0, 0, 0, '[]')
        """,
        (job_id, harness, elapsed, json.dumps(tools)),
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
        _telemetry(connection, build_1, {"Skill": 2, "Agent": 1, "Bash": 9})
        _telemetry(connection, build_2, {"Skill": 1, "Task": 4, "Agent": 1}, elapsed=5400)
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
    assert values["skills-read-per-job"] == pytest.approx(1.0)
    assert values["helpers-per-job"] == pytest.approx(2.0)
    assert values["helpers-per-job-max"] == 5
    assert values["preflight-failures"] == 2
    assert values["slot-hours-lost"] == pytest.approx(2.0)
    assert values["hand-written-gos"] is None
    assert values["ac-unmet-approve"] is None
    assert values["submitted-invalid"] is None
    assert values["repeat-block-stops"] is None
    assert values["unsatisfiable-stops"] is None


def test_a_snapshot_without_a_source_table_reports_it_unrecorded(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE ticket_state (repo TEXT, ticket_id TEXT, workflow_state TEXT, "
            "review_round INTEGER)"
        )
        connection.execute("INSERT INTO ticket_state VALUES ('owner/repo', '1', 'fixing', 1)")
    values = _values(build_metrics(db))
    assert values["prs-reviewed"] == 1
    assert values["first-round-revise-rate"] is None
    assert values["first-pass-approve-rate"] is None
    assert values["skills-read-per-job"] is None
    assert values["preflight-failures"] is None
    assert values["prs-dirty-after-approval"] is None


def test_each_metric_prints_beside_the_baseline(tmp_path: Path) -> None:
    current = build_metrics(_seeded(tmp_path / "now"))
    baseline_db = tmp_path / "o4.db"
    sqlite3.connect(baseline_db).close()
    baseline = build_metrics(baseline_db)

    lines = render_metrics(current, baseline).splitlines()

    assert lines[0] == "eval metrics"
    assert lines[1] == "metric current baseline"
    rows = {line.split()[0]: line.split()[1:] for line in lines[2:]}
    assert set(rows) == {name for name, _value in current.values}
    assert all(len(row) == 2 for row in rows.values())
    assert rows["prs-capped"] == ["2", "unrecorded"]
    assert rows["rounds-per-pr"] == ["1.40", "unrecorded"]
    assert rows["submitted-invalid"] == ["unrecorded", "unrecorded"]


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
