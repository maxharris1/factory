from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from lib.dispatch.cycle_clock import CycleClock
from lib.dispatch.types import CheckRow, CIResult, RunJob, TrackedTicket
from lib.dispatcher import Dispatcher
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import (
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    FakeSource,
    factory,
)

REPO = "Owner/Repo"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
DAY = 24 * 60 * 60


def check(
    name: str,
    status: str,
    conclusion: str | None = None,
    *,
    run: str = "900",
    job: str = "1",
    started: float | None = 50.0,
) -> CheckRow:
    return CheckRow(
        name=name,
        status=status,
        conclusion=conclusion,
        started_at=None if status == "queued" else started,
        completed_at=60.0 if status == "completed" else None,
        log_url=f"https://github.com/owner/repo/actions/runs/{run}/job/{job}",
        run_id=run,
        job_id=job,
    )


def pr(ticket_id: str, head: str | None, *checks: CheckRow) -> TrackedTicket:
    return TrackedTicket(
        repo=REPO,
        ticket_id=ticket_id,
        factory=factory(),
        comments=(),
        current_head=head,
        ci={} if head is None else {head: CIResult("pending", checks=checks)},
        risk="high",
        pr_number=ticket_id,
    )


def hosted(job: str, name: str = "pytest") -> RunJob:
    return RunJob(
        job,
        name,
        "in_progress",
        created_at=40.0,
        started_at=50.0,
        runner_name=f"GitHub Actions {job}",
        runner_kind="github-hosted",
    )


class GitHub:
    def __init__(self) -> None:
        self.jobs: dict[str, Sequence[RunJob] | Exception] = {}
        self.logs: dict[str, Mapping[str, str] | Exception] = {}
        self.job_reads: list[str] = []
        self.log_reads: list[str] = []

    def run_jobs(self, repo: str, run_id: str) -> Sequence[RunJob]:
        assert repo.lower() == REPO.lower()
        self.job_reads.append(run_id)
        answer = self.jobs.get(run_id, ())
        if isinstance(answer, Exception):
            raise answer
        return answer

    def ci_logs(self, repo: str, run_id: str) -> Mapping[str, str]:
        assert repo.lower() == REPO.lower()
        self.log_reads.append(run_id)
        answer = self.logs.get(run_id, {})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def spent(self) -> int:
        spent = len(self.job_reads) + len(self.log_reads)
        self.job_reads.clear()
        self.log_reads.clear()
        return spent


class Board:
    def __init__(self, tmp_path: Path) -> None:
        self.now = [1000.0]
        path = tmp_path / "d.db"
        self.queue = SQLiteJobQueue(path, clock=lambda: self.now[0])
        self.github = GitHub()
        self.lines: list[str] = []
        self.dispatcher = Dispatcher(
            path,
            self.queue,
            trusted_logins=TRUSTED_LOGINS,
            review_actions=FakeReviewActions(),
            grok_delivery=FakeGrokDelivery(),
            log=self.lines.append,
            ci_logs=self.github.ci_logs,
            ci_runs=self.github.run_jobs,
        )
        self.dispatcher.cycle_clock = CycleClock()

    def track(self, *ticket_ids: str) -> None:
        with self.queue.connect() as connection:
            connection.executemany(
                "INSERT INTO tracked_tickets VALUES (?, ?, 'github', 0)",
                [(REPO.lower(), ticket_id) for ticket_id in ticket_ids],
            )

    def cycle(self, *tickets: TrackedTicket) -> None:
        self.dispatcher.poll_once(FakeSource(*tickets))

    def rows(self) -> list[dict[str, object]]:
        with self.queue.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM ci_jobs ORDER BY ticket_id, head, check_name"
                )
            ]

    def row(self, ticket_id: str, name: str) -> dict[str, object]:
        (row,) = [
            row
            for row in self.rows()
            if row["ticket_id"] == ticket_id and row["check_name"] == name
        ]
        return row


def test_a_cycle_writes_each_tracked_heads_checks_with_their_status(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1", "2", "3")

    board.cycle(
        pr("1", HEAD, check("pytest", "queued", run="901", job="11")),
        pr("2", HEAD, check("pytest", "in_progress", run="902", job="12")),
        pr("3", HEAD, check("pytest", "completed", "success", run="903", job="13")),
    )

    assert [
        (row["ticket_id"], row["status"], row["conclusion"], row["started_at"], row["completed_at"])
        for row in board.rows()
    ] == [
        ("1", "queued", None, None, None),
        ("2", "in_progress", None, 50.0, None),
        ("3", "completed", "success", 50.0, 60.0),
    ]
    first = board.row("1", "pytest")
    assert first == {
        **first,
        "repo": "owner/repo",
        "head": HEAD,
        "pr": "1",
        "run_id": "901",
        "job_id": "11",
        "log_url": "https://github.com/owner/repo/actions/runs/901/job/11",
        "runner_kind": None,
        "failing_tests": None,
        "seen_at": 1000.0,
    }


def test_a_ticket_without_a_head_or_checks_writes_nothing(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1", "2")

    board.cycle(pr("1", None), pr("2", HEAD))

    assert board.rows() == []
    assert board.github.spent() == 0


def test_runner_reads_take_three_runs_a_cycle_until_each_started_run_has_its_runner(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    ids = [str(n) for n in range(1, 6)]
    board.track(*ids)
    tickets = [pr(n, HEAD, check("pytest", "in_progress", run=f"90{n}", job=f"1{n}")) for n in ids]
    for n in ids:
        board.github.jobs[f"90{n}"] = (hosted(f"1{n}"),)

    board.cycle(*tickets)
    assert len(board.github.job_reads) == 3
    assert board.github.spent() == 3
    board.cycle(*tickets)
    assert board.github.spent() == 2
    board.cycle(*tickets)
    assert board.github.spent() == 0

    for n in ids:
        row = board.row(n, "pytest")
        assert (row["runner_name"], row["runner_kind"], row["created_at"]) == (
            f"GitHub Actions 1{n}",
            "github-hosted",
            40.0,
        )


def test_a_queued_check_spends_no_runner_read(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")

    board.cycle(pr("1", HEAD, check("pytest", "queued")))

    assert board.github.spent() == 0


def test_a_job_that_ran_without_a_runner_is_read_once(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    skipped = pr("1", HEAD, check("deploy", "completed", "skipped"))
    board.github.jobs["900"] = (RunJob("1", "deploy", "completed", runner_kind="none"),)

    board.cycle(skipped)
    board.cycle(skipped)

    assert board.github.job_reads == ["900"]
    assert board.row("1", "deploy")["runner_kind"] == "none"


def test_a_failed_run_costs_one_log_read_once(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    failed = pr(
        "1",
        HEAD,
        check("pytest", "completed", "failure", job="1"),
        check("lint", "completed", "failure", job="2"),
        check("build", "completed", "success", job="3"),
    )
    board.github.logs["900"] = {
        "pytest": "collected 3 items\nFAILED tests/test_a.py::test_b - assert 1 == 2\n1 failed"
    }

    board.cycle(failed)
    assert board.github.log_reads == ["900"]
    board.github.spent()
    board.cycle(failed)
    assert board.github.log_reads == []

    assert json.loads(str(board.row("1", "pytest")["failing_tests"])) == ["tests/test_a.py::test_b"]
    assert json.loads(str(board.row("1", "lint")["failing_tests"])) == []
    assert board.row("1", "build")["failing_tests"] is None


def test_the_log_read_waits_for_the_whole_run(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    board.github.logs["900"] = {"pytest": "FAILED tests/test_a.py::test_b"}

    board.cycle(
        pr(
            "1",
            HEAD,
            check("pytest", "completed", "failure", job="1"),
            check("e2e", "in_progress", job="2"),
        )
    )
    assert board.github.log_reads == []

    board.cycle(
        pr(
            "1",
            HEAD,
            check("pytest", "completed", "failure", job="1"),
            check("e2e", "completed", "success", job="2"),
        )
    )
    assert board.github.log_reads == ["900"]


def fix_job_with_log(board: Board, log_tail: str | None) -> None:
    evidence = {
        "ci": {
            "failed_checks": ["pytest"],
            "runs": [["900", "https://github.com/owner/repo/actions/runs/900"]],
            "jobs": [
                {
                    "check": "pytest",
                    "run_id": "900",
                    "run_url": "https://github.com/owner/repo/actions/runs/900",
                    "log_tail": log_tail,
                    "cut": None,
                }
            ],
        }
    }
    board.queue.enqueue(
        JobRequest(
            repo=REPO,
            ticket_id="1",
            action="fix",
            recipient="claude",
            source_comment_id="verdict-1",
            timeout_seconds=600,
            head=HEAD,
            detail=json.dumps({"evidence": evidence}),
        ),
        dedupe_key="fix-1",
    )


def test_a_ci_fix_that_holds_the_log_saves_the_log_read(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    fix_job_with_log(board, "E   assert 1 == 2\nFAILED tests/test_a.py::test_b - assert 1 == 2")

    board.cycle(pr("1", HEAD, check("pytest", "completed", "failure")))

    assert board.github.log_reads == []
    assert json.loads(str(board.row("1", "pytest")["failing_tests"])) == ["tests/test_a.py::test_b"]


def test_a_ci_fix_without_the_log_still_reads_it(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    fix_job_with_log(board, None)
    board.github.logs["900"] = {"pytest": "FAILED tests/test_a.py::test_c"}

    board.cycle(pr("1", HEAD, check("pytest", "completed", "failure")))

    assert board.github.log_reads == ["900"]
    assert json.loads(str(board.row("1", "pytest")["failing_tests"])) == ["tests/test_a.py::test_c"]


def test_a_busy_board_never_spends_more_than_four_requests_a_cycle(tmp_path: Path) -> None:
    board = Board(tmp_path)
    ids = [str(n) for n in range(10, 22)]
    board.track(*ids)
    tickets = []
    for n in ids:
        failed = int(n) % 3 == 0
        tickets.append(
            pr(
                n,
                HEAD,
                check(
                    "pytest",
                    "completed" if failed else "in_progress",
                    "failure" if failed else None,
                    run=f"9{n}",
                    job=n,
                ),
            )
        )
        board.github.jobs[f"9{n}"] = (hosted(n),)
        board.github.logs[f"9{n}"] = {"pytest": f"FAILED tests/test_{n}.py::test_x"}

    spent = []
    for _ in range(6):
        board.cycle(*tickets)
        spent.append((len(board.github.job_reads), len(board.github.log_reads)))
        board.github.spent()

    assert spent == [(3, 1), (3, 1), (3, 1), (3, 1), (0, 0), (0, 0)]
    assert all(row["runner_kind"] == "github-hosted" for row in board.rows())


def test_a_new_head_drops_the_old_heads_rows_but_keeps_a_failure_for_a_day(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    board.track("1")
    board.cycle(
        pr(
            "1",
            HEAD,
            check("pytest", "completed", "failure", job="1"),
            check("lint", "completed", "success", job="2"),
        )
    )

    board.now[0] += 60
    board.cycle(pr("1", NEW_HEAD, check("pytest", "queued", run="901", job="3")))

    assert [(row["head"], row["check_name"], row["status"]) for row in board.rows()] == [
        (HEAD, "pytest", "completed"),
        (NEW_HEAD, "pytest", "queued"),
    ]

    board.now[0] += DAY
    board.cycle(pr("1", NEW_HEAD, check("pytest", "queued", run="901", job="3")))

    assert [(row["head"], row["check_name"]) for row in board.rows()] == [(NEW_HEAD, "pytest")]


def test_a_rerun_at_the_same_head_replaces_the_row_and_its_lookups(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    board.github.jobs["900"] = (
        RunJob(
            "1",
            "pytest",
            "completed",
            created_at=30.0,
            runner_name="pc-wsl-bots-2",
            runner_kind="self-hosted",
        ),
    )
    board.github.logs["900"] = {"pytest": "FAILED tests/test_a.py::test_b"}
    board.cycle(pr("1", HEAD, check("pytest", "completed", "failure", job="1")))
    assert board.row("1", "pytest")["runner_name"] == "pc-wsl-bots-2"

    board.cycle(pr("1", HEAD, check("pytest", "queued", job="2")))

    row = board.row("1", "pytest")
    assert (row["job_id"], row["status"], row["conclusion"]) == ("2", "queued", None)
    assert (row["runner_name"], row["runner_kind"], row["created_at"], row["failing_tests"]) == (
        None,
        None,
        None,
        None,
    )

    board.github.jobs["900"] = (hosted("2"),)
    board.cycle(pr("1", HEAD, check("pytest", "in_progress", job="2", started=70.0)))

    row = board.row("1", "pytest")
    assert (row["job_id"], row["status"], row["conclusion"], row["started_at"]) == (
        "2",
        "in_progress",
        None,
        70.0,
    )
    assert (row["runner_name"], row["failing_tests"]) == ("GitHub Actions 2", None)


def test_two_checks_sharing_a_name_keep_the_failure(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    shared = pr(
        "1",
        HEAD,
        check("build", "completed", "failure", run="901", job="1", started=10.0),
        check("build", "completed", "success", run="902", job="2", started=20.0),
    )

    board.cycle(shared)
    board.cycle(shared)

    assert [(row["run_id"], row["conclusion"]) for row in board.rows()] == [("901", "failure")]


def test_a_finished_ticket_keeps_only_its_failures(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1", "2")
    board.cycle(
        pr("1", HEAD, check("pytest", "completed", "failure", run="901", job="1")),
        pr("2", HEAD, check("pytest", "completed", "success", run="902", job="2")),
    )
    with board.queue.connect() as connection:
        connection.executemany(
            """
            INSERT INTO ticket_state (repo, ticket_id, workflow_state) VALUES (?, ?, 'merged')
            ON CONFLICT (repo, ticket_id) DO UPDATE SET workflow_state = 'merged'
            """,
            [(REPO.lower(), "1"), (REPO.lower(), "2")],
        )

    board.cycle()

    assert [(row["ticket_id"], row["conclusion"]) for row in board.rows()] == [("1", "failure")]


def test_an_adapter_error_spends_its_read_and_the_cycle_goes_on(tmp_path: Path) -> None:
    board = Board(tmp_path)
    ids = ["1", "2", "3", "4"]
    board.track(*ids)
    tickets = [
        pr(n, HEAD, check("pytest", "in_progress", run=f"90{n}", job=n, started=float(n)))
        for n in ids
    ]
    for n in ids:
        board.github.jobs[f"90{n}"] = (hosted(n),)
    board.github.jobs["904"] = RuntimeError("HTTP 502")

    board.cycle(*tickets)

    assert board.github.job_reads == ["904", "903", "902"]
    assert any("904" in line and "RuntimeError" in line for line in board.lines)
    assert board.row("4", "pytest")["runner_kind"] is None
    assert board.row("3", "pytest")["runner_kind"] == "github-hosted"


def test_a_broken_board_never_fails_the_poll_cycle(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    with board.queue.connect() as connection:
        connection.execute("DROP TABLE ci_jobs")

    board.cycle(pr("1", HEAD, check("pytest", "in_progress")))

    with board.queue.connect() as connection:
        errors = connection.execute("SELECT repo, error FROM poll_errors").fetchall()
    assert [tuple(row) for row in errors] == [("ci-board", "OperationalError")]
