from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from lib.dashboard import _ci_runs
from lib.dashboard_pages.ci import _ci_section
from lib.dispatch.cycle_clock import CycleClock
from lib.dispatch.types import CheckRow, CIResult, RunJob, TrackedTicket
from lib.dispatcher import Dispatcher
from lib.github_actions import GitHubReviewActions
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
    completed: float | None = 60.0,
) -> CheckRow:
    return CheckRow(
        name=name,
        status=status,
        conclusion=conclusion,
        started_at=None if status == "queued" else started,
        completed_at=completed if status == "completed" else None,
        log_url=f"https://github.com/owner/repo/actions/runs/{run}/job/{job}",
        run_id=run,
        job_id=job,
        workflow=f"actions-run:{run}",
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
        self.logs: dict[str, str | Exception] = {}
        self.job_reads: list[str] = []
        self.log_reads: list[str] = []

    def run_jobs(self, repo: str, run_id: str) -> Sequence[RunJob]:
        assert repo.lower() == REPO.lower()
        self.job_reads.append(run_id)
        answer = self.jobs.get(run_id, ())
        if isinstance(answer, Exception):
            raise answer
        return answer

    def job_log(self, repo: str, job_id: str) -> str:
        assert repo.lower() == REPO.lower()
        self.log_reads.append(job_id)
        answer = self.logs.get(job_id, "")
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
            ci_runs=self.github.run_jobs,
            ci_job_log=self.github.job_log,
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

    def dashboard(self) -> dict:
        with self.queue.connect() as connection:
            return _ci_runs(connection, self.now[0], {}, {})

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


def test_each_failed_job_costs_one_log_read_once(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    failed = pr(
        "1",
        HEAD,
        check("pytest", "completed", "failure", job="1"),
        check("lint", "completed", "failure", job="2"),
        check("build", "completed", "success", job="3"),
    )
    board.github.logs["1"] = "collected 3 items\nFAILED tests/test_a.py::test_b - assert 1 == 2\n1 failed"

    reads = []
    for _ in range(3):
        board.cycle(failed)
        reads.append(list(board.github.log_reads))
        board.github.spent()

    assert reads == [["1"], ["2"], []]
    assert json.loads(str(board.row("1", "pytest")["failing_tests"])) == ["tests/test_a.py::test_b"]
    assert json.loads(str(board.row("1", "lint")["failing_tests"])) == []
    assert board.row("1", "build")["failing_tests"] is None


def test_a_log_that_cannot_be_read_never_starves_the_older_failures(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    failed = pr(
        "1",
        HEAD,
        check("pytest", "completed", "failure", job="1"),
        check("lint", "completed", "cancelled", job="2", started=None),
    )
    board.github.logs["1"] = "FAILED tests/test_a.py::test_b"
    board.github.logs["2"] = RuntimeError("HTTP 404")

    reads = []
    for _ in range(3):
        board.cycle(failed)
        reads.append(list(board.github.log_reads))
        board.github.spent()

    assert sorted(sum(reads, [])) == ["1", "2"]
    assert any("job 2" in line and "RuntimeError" in line for line in board.lines)
    assert json.loads(str(board.row("1", "pytest")["failing_tests"])) == ["tests/test_a.py::test_b"]
    assert json.loads(str(board.row("1", "lint")["failing_tests"])) == []


def test_a_failed_jobs_log_is_read_while_the_rest_of_its_run_still_runs(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    board.github.logs["1"] = "FAILED tests/test_a.py::test_b"

    board.cycle(
        pr(
            "1",
            HEAD,
            check("pytest", "completed", "failure", job="1"),
            check("e2e", "in_progress", job="2"),
        )
    )

    assert board.github.log_reads == ["1"]
    assert json.loads(str(board.row("1", "pytest")["failing_tests"])) == ["tests/test_a.py::test_b"]


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
        board.github.logs[n] = f"FAILED tests/test_{n}.py::test_x"

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
    board.github.logs["1"] = "FAILED tests/test_a.py::test_b"
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


def test_two_workflows_sharing_a_check_name_keep_both_jobs(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    shared = pr(
        "1",
        HEAD,
        check("build", "completed", "failure", run="901", job="1", started=10.0),
        check("build", "in_progress", run="902", job="2", started=20.0),
    )

    board.cycle(shared)
    board.cycle(shared)

    assert [(row["run_id"], row["status"], row["conclusion"]) for row in board.rows()] == [
        ("901", "completed", "failure"),
        ("902", "in_progress", None),
    ]
    dashboard = board.dashboard()
    (run,) = dashboard["runs"]
    assert run["status"] == "in_progress"
    assert sorted((job["run_id"], job["status"]) for job in run["jobs"]) == [
        ("901", "completed"),
        ("902", "in_progress"),
    ]
    assert [(failure["run_id"], failure["name"]) for failure in dashboard["failures"]] == [
        ("901", "build")
    ]
    section = _ci_section({"ci_runs": dashboard})
    assert "no CI queued or running" not in section
    assert "actions/runs/901/job/1" in section and "actions/runs/902/job/2" in section


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


class RecordingGh:
    """GitHub behind the real adapter: every gh argv, answered by endpoint."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        endpoint = argv[-1]
        runs = re.search(r"/actions/runs/([0-9]+)/jobs", endpoint)
        if runs:
            job = {
                "id": int(runs.group(1)) - 890,
                "name": "pytest",
                "status": "in_progress",
                "conclusion": None,
                "created_at": "2026-11-10T19:49:30Z",
                "started_at": "2026-11-10T19:50:00Z",
                "runner_name": "GitHub Actions 7",
                "runner_group_name": "GitHub Actions",
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps({"total_count": 1, "jobs": [job]}), "")
        if re.search(r"/actions/jobs/[0-9]+/logs$", endpoint):
            return subprocess.CompletedProcess(argv, 0, "FAILED tests/test_x.py::test_y - boom\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected gh call")


def test_the_board_spends_at_most_four_single_gh_api_requests_a_cycle(tmp_path: Path) -> None:
    board = Board(tmp_path)
    gh = RecordingGh()
    actions = GitHubReviewActions(gh)
    board.dispatcher.ci_runs = actions.run_jobs
    board.dispatcher.ci_job_log = actions.job_log
    board.track("1", "2", "3", "4")

    board.cycle(
        pr("1", HEAD, check("pytest", "in_progress", run="901", job="11")),
        pr("2", HEAD, check("pytest", "in_progress", run="902", job="12")),
        pr("3", HEAD, check("pytest", "in_progress", run="903", job="13")),
        pr("4", HEAD, check("pytest", "completed", "failure", run="904", job="14")),
    )

    assert len(gh.calls) <= 4
    for argv in gh.calls:
        rest = argv[argv.index("gh") + 1 :]
        assert rest[0] == "api" and "--paginate" not in rest, argv
        assert "view" not in rest, argv
    assert sorted(argv[-1] for argv in gh.calls) == [
        "repos/owner/repo/actions/jobs/14/logs",
        "repos/owner/repo/actions/runs/901/jobs?per_page=100",
        "repos/owner/repo/actions/runs/902/jobs?per_page=100",
        "repos/owner/repo/actions/runs/903/jobs?per_page=100",
    ]
    assert json.loads(str(board.row("4", "pytest")["failing_tests"])) == ["tests/test_x.py::test_y"]


def test_a_rerun_shows_the_failing_tests_of_its_own_attempt(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    fix_job_with_log(board, "FAILED tests/test_old.py::test_old")
    board.github.logs["1"] = "FAILED tests/test_old.py::test_old"
    board.cycle(pr("1", HEAD, check("pytest", "completed", "failure", job="1")))
    board.cycle(pr("1", HEAD, check("pytest", "queued", job="2")))
    board.github.logs["2"] = "FAILED tests/test_new.py::test_new"
    board.github.spent()

    board.cycle(pr("1", HEAD, check("pytest", "completed", "failure", job="2", completed=70.0)))

    assert board.github.log_reads == ["2"]
    assert [failure["tests"] for failure in board.dashboard()["failures"]] == [
        ["tests/test_new.py::test_new"]
    ]


def test_a_log_read_lands_only_on_the_attempt_it_read(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    read = board.github.job_log

    def rerun_while_reading(repo: str, job_id: str) -> str:
        board.dispatcher.ci_job_log = read
        board.cycle(pr("1", HEAD, check("pytest", "queued", job="2")))
        return "FAILED tests/test_old.py::test_old"

    board.dispatcher.ci_job_log = rerun_while_reading
    board.cycle(pr("1", HEAD, check("pytest", "completed", "failure", job="1")))

    row = board.row("1", "pytest")
    assert (row["job_id"], row["failing_tests"]) == ("2", None)


def test_a_failure_leaves_the_dashboard_a_day_after_it_completed_though_still_polled(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    board.track("1")
    board.github.jobs["900"] = (
        RunJob("1", "pytest", "completed", created_at=40.0, runner_kind="none"),
    )
    board.github.logs["1"] = "FAILED tests/test_a.py::test_b"
    failed = pr("1", HEAD, check("pytest", "completed", "failure", job="1", completed=None))
    board.cycle(failed)
    assert [failure["age_seconds"] for failure in board.dashboard()["failures"]] == [0]
    board.github.spent()

    board.now[0] += 23 * 60 * 60
    board.cycle(failed)
    assert [failure["age_seconds"] for failure in board.dashboard()["failures"]] == [23 * 60 * 60]

    board.now[0] += 2 * DAY + 60 * 60
    board.cycle(failed)
    dashboard = board.dashboard()
    assert dashboard["failures"] == []
    section = _ci_section({"ci_runs": dashboard})
    assert "no CI failures in the last 24 h" in section and "actions/runs/900/job/1" not in section
    assert board.github.spent() == 0


def test_an_old_heads_failure_is_dropped_a_day_after_it_completed(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.track("1")
    failed = pr("1", HEAD, check("pytest", "completed", "failure", job="1", completed=1000.0))
    board.cycle(failed)
    board.now[0] += 20 * 60 * 60
    board.cycle(failed)
    rerun = pr("1", NEW_HEAD, check("pytest", "queued", run="901", job="2"))
    board.now[0] += 3 * 60 * 60
    board.cycle(rerun)
    assert [row["head"] for row in board.rows()] == [HEAD, NEW_HEAD]

    board.now[0] += 2 * 60 * 60
    board.cycle(rerun)

    assert [row["head"] for row in board.rows()] == [NEW_HEAD]
