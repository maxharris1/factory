from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from lib.dispatch.ci_board import record
from lib.dispatch.types import ci_required
from lib.github_actions import GhResult
from lib.poll_source import LivePollSource
from tests.test_github_actions import (
    CHECKS,
    HEAD,
    REPO,
    STATUS,
    WORKFLOW_RUNS,
    adapter,
    served,
)
from tests.test_ci_board import Board
from tests.test_live_poll import FACTORY, FakeGh, config


def check_run(
    check_id: int,
    name: str,
    conclusion: str | None,
    started_at: str | None,
    workflow_run_id: int,
    *,
    status: str | None = None,
    annotations_count: int = 0,
) -> dict[str, Any]:
    if status is None:
        status = "in_progress" if conclusion is None else "completed"
    return {
        "id": check_id,
        "head_sha": HEAD,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at,
        "completed_at": started_at if status == "completed" else None,
        "app": {"slug": "github-actions", "id": 15368},
        "check_suite": {"id": 900 + workflow_run_id},
        "details_url": f"https://github.com/owner/repo/actions/runs/{workflow_run_id}/job/{check_id}",
        "output": {"title": None, "summary": None, "annotations_count": annotations_count},
    }


def checks_page(*runs: dict[str, Any]) -> GhResult:
    return GhResult(0, json.dumps({"total_count": len(runs), "check_runs": list(runs)}), "")


def wf_run(
    run_id: int,
    event: str,
    path: str | None,
    created_at: str | None = None,
    *,
    workflow_id: int | None = None,
    run_number: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": run_id,
        "event": event,
        "check_suite_id": 900 + run_id,
        "run_number": run_id if run_number is None else run_number,
    }
    if path is not None:
        item["path"] = path
    if workflow_id is not None:
        item["workflow_id"] = workflow_id
    if created_at is not None:
        item["created_at"] = created_at
    return item


def wf_runs_page(*runs: dict[str, Any], total_count: int | None = None) -> GhResult:
    total = len(runs) if total_count is None else total_count
    return GhResult(0, json.dumps({"total_count": total, "workflow_runs": list(runs)}), "")


def _cleffy_166_runs() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Exact maxharris1/Cleffy#166 shape on head 02d9d39:
    all ci.yml PR checks green, two forced-failure workflow_dispatch probe
    runs (45527270121, 45527318860), and one later passing workflow_dispatch
    probe run (45529418050) in .github/workflows/uptime.yml.
    """
    ci_run = 45527237070
    checks = [
        check_run(138913673836, "lint", "success", "2026-11-25T01:39:04Z", ci_run),
        check_run(138913674073, "typecheck", "success", "2026-11-25T01:39:04Z", ci_run),
        check_run(138913673873, "test (1/2)", "success", "2026-11-25T01:39:03Z", ci_run),
        check_run(138913673962, "test (2/2)", "success", "2026-11-25T01:39:04Z", ci_run),
        check_run(138913673939, "build", "success", "2026-11-25T01:39:03Z", ci_run),
        check_run(138914549794, "ci-ok", "success", "2026-11-25T01:41:15Z", ci_run),
        check_run(138914589607, "checks", "success", "2026-11-25T01:41:22Z", ci_run),
        check_run(138913700001, "probe", "failure", "2026-11-25T01:39:10Z", 45527270121),
        check_run(138913800002, "probe", "failure", "2026-11-25T01:40:05Z", 45527318860),
        check_run(138920727902, "probe", "success", "2026-11-25T01:56:59Z", 45529418050),
    ]
    workflows = [
        wf_run(ci_run, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:38:34Z", workflow_id=325132108),
        wf_run(45527270121, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:05Z", workflow_id=429759741),
        wf_run(45527318860, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:40:00Z", workflow_id=429759741),
        wf_run(45529418050, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:56:55Z", workflow_id=429759741),
    ]
    return checks, workflows


@pytest.mark.parametrize(
    "required",
    [(), ("ci-ok",), ("probe",), ("ci-ok", "probe")],
    ids=["no-required", "ci-ok-required", "probe-required", "both-required"],
)
def test_cleffy_166_replay_passes_with_or_without_required_checks(
    required: tuple[str, ...],
) -> None:
    checks, workflows = _cleffy_166_runs()
    actions, gh = adapter()
    gh.route(CHECKS, checks_page(*checks))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(WORKFLOW_RUNS, wf_runs_page(*workflows))

    result = actions.ci_status("owner/repo", HEAD, required_checks=required)

    assert result.status == "success"
    assert result.failed == ()
    assert ci_required(result, required).status == "success"
    probe_rows = [row for row in result.checks if row.name == "probe"]
    assert [(row.run_id, row.conclusion) for row in probe_rows] == [
        ("45527270121", "failure"),
        ("45527318860", "failure"),
        ("45529418050", "success"),
    ]


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule"])
def test_non_pr_failure_does_not_fail_ci_unless_in_required_checks(event: str) -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "test", "success", "2026-11-25T01:39:00Z", 100),
            check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200, annotations_count=3),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:38:00Z"),
            wf_run(200, event, ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
        ),
    )

    annotations_key = ("--paginate", f"{REPO}/check-runs/20/annotations?per_page=100")
    gh.route(annotations_key, GhResult(0, "[]", ""))

    unrequired = actions.ci_status("owner/repo", HEAD)
    assert unrequired.status == "success"
    assert unrequired.failed == ()
    assert gh.count(annotations_key) == 0

    other_required = actions.ci_status("owner/repo", HEAD, required_checks=("test",))
    assert other_required.status == "success"
    assert gh.count(annotations_key) == 0

    exact_required = actions.ci_status("owner/repo", HEAD, required_checks=("probe",))
    assert exact_required.status == "failure"
    assert [check.name for check in exact_required.failed] == ["probe"]
    assert gh.count(annotations_key) == 1

    glob_required = actions.ci_status("owner/repo", HEAD, required_checks=("pro*",))
    assert glob_required.status == "failure"
    assert [check.name for check in glob_required.failed] == ["probe"]


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule"])
def test_non_pr_checks_alone_never_falsely_pass_when_not_required(event: str) -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "probe", "failure", "2026-11-25T01:39:00Z", 100),
            check_run(20, "probe", "success", "2026-11-25T01:40:00Z", 200),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, event, ".github/workflows/uptime.yml", "2026-11-25T01:38:55Z"),
            wf_run(200, event, ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD)
    assert result.status == "pending"
    assert result.failed == ()


def test_green_actions_runs_make_no_workflow_runs_read() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(check_run(10, "test", "success", "2026-11-25T01:39:00Z", 100)),
    )
    gh.route(STATUS, served("status-empty.json"))

    assert actions.ci_status("owner/repo", HEAD).status == "success"
    assert gh.keys() == [CHECKS, STATUS]


def test_required_workflow_dispatch_failure_waits_when_later_dispatch_is_in_progress() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "test", "success", "2026-11-25T01:39:00Z", 100),
            check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200),
            check_run(30, "probe", None, None, 300, status="queued"),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:38:00Z"),
            wf_run(200, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
            wf_run(300, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:41:00Z"),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD, required_checks=("probe",))
    assert result.status == "pending"
    assert result.pending == ("probe",)
    assert result.failed == ()


def test_later_pull_request_run_in_same_workflow_file_supersedes_earlier_failed_dispatch() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "test", "failure", "2026-11-25T01:38:00Z", 100),
            check_run(20, "test", "success", "2026-11-25T01:40:00Z", 200),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "workflow_dispatch", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z"),
            wf_run(200, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:39:55Z"),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD, required_checks=("test",))
    assert result.status == "success"
    assert result.failed == ()


def test_earlier_passing_dispatch_does_not_hide_later_failed_required_dispatch() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "probe", "success", "2026-11-25T01:38:00Z", 100),
            check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:37:55Z"),
            wf_run(200, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD, required_checks=("probe",))
    assert result.status == "failure"
    assert [check.run_id for check in result.failed] == ["200"]
    assert ci_required(result, ("probe",)).status == "failure"


def test_different_workflow_file_never_supersedes_required_dispatch_failure() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "probe", "failure", "2026-11-25T01:38:00Z", 100),
            check_run(20, "probe", "success", "2026-11-25T01:40:00Z", 200),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "workflow_dispatch", ".github/workflows/uptime-a.yml", "2026-11-25T01:37:55Z"),
            wf_run(200, "workflow_dispatch", ".github/workflows/uptime-b.yml", "2026-11-25T01:39:55Z"),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD, required_checks=("probe",))
    assert result.status == "failure"
    assert [check.run_id for check in result.failed] == ["100"]


def test_skipped_later_dispatch_does_not_clear_required_dispatch_failure() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "test", "success", "2026-11-25T01:37:00Z", 100),
            check_run(20, "probe", "failure", "2026-11-25T01:38:00Z", 200),
            check_run(30, "probe", "skipped", "2026-11-25T01:40:00Z", 300),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:36:55Z"),
            wf_run(200, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:37:55Z"),
            wf_run(300, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD, required_checks=("probe",))
    assert result.status == "failure"
    assert [check.run_id for check in result.failed] == ["200"]


def test_workflow_id_fallback_supersedes_when_path_is_absent() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "probe", "failure", "2026-11-25T01:38:00Z", 100),
            check_run(20, "probe", "success", "2026-11-25T01:40:00Z", 200),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "workflow_dispatch", None, "2026-11-25T01:37:55Z", workflow_id=777),
            wf_run(200, "workflow_dispatch", None, "2026-11-25T01:39:55Z", workflow_id=777),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD, required_checks=("probe",))
    assert result.status == "success"


def test_pr_triggered_failure_is_never_superseded_by_later_dispatch_or_other_pr_run() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "test", "failure", "2026-11-25T01:38:00Z", 100),
            check_run(20, "test", "success", "2026-11-25T01:40:00Z", 200),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z"),
            wf_run(200, "workflow_dispatch", ".github/workflows/ci.yml", "2026-11-25T01:39:55Z"),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD)
    assert result.status == "failure"
    assert [check.run_id for check in result.failed] == ["100"]

    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z"),
            wf_run(200, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:39:55Z"),
        ),
    )
    second = actions.ci_status("owner/repo", HEAD)
    assert second.status == "failure"
    assert [check.run_id for check in second.failed] == ["100"]


def test_workflow_runs_read_failure_or_missing_run_fails_closed() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "test", "success", "2026-11-25T01:38:00Z", 100),
            check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(WORKFLOW_RUNS, GhResult(1, "", "gh: Server Error (HTTP 502)\n"))

    errored = actions.ci_status("owner/repo", HEAD)
    assert errored.status == "failure"
    assert [check.name for check in errored.failed] == ["probe"]

    # Run 200 omitted from workflow_runs -> fail closed.
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z"),
        ),
    )
    missing = actions.ci_status("owner/repo", HEAD)
    assert missing.status == "failure"
    assert [check.name for check in missing.failed] == ["probe"]


def test_workflow_runs_short_read_is_pending_and_multi_page_is_read_completely() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "test", "success", "2026-11-25T01:38:00Z", 100),
            check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z"),
            total_count=2,
        ),
    )

    assert actions.ci_status("owner/repo", HEAD).status == "pending"

    page1 = json.dumps(
        {
            "total_count": 2,
            "workflow_runs": [
                wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z")
            ],
        }
    )
    page2 = json.dumps(
        {
            "total_count": 2,
            "workflow_runs": [
                wf_run(200, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z")
            ],
        }
    )
    gh.route(WORKFLOW_RUNS, GhResult(0, page1 + page2, ""))
    assert actions.ci_status("owner/repo", HEAD).status == "success"


def test_pr_failure_beside_non_required_dispatch_failure_reports_only_pr_failure() -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "test", "failure", "2026-11-25T01:38:00Z", 100),
            check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z"),
            wf_run(200, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD)
    assert result.status == "failure"
    assert [check.name for check in result.failed] == ["test"]


class _DispatchPollGh(FakeGh):
    def __init__(self, factory: str, checks: list[dict[str, Any]], workflows: list[dict[str, Any]]):
        super().__init__(factory)
        self._checks = checks
        self._workflows = workflows
        self.issues = [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}]

    def __call__(self, argv, timeout):
        if any(part.endswith("/check-runs?per_page=100") for part in argv):
            self.calls.append(list(argv))
            return GhResult(
                0,
                json.dumps({"total_count": len(self._checks), "check_runs": self._checks}),
                "",
            )
        if any("/actions/runs?head_sha=" in part for part in argv):
            self.calls.append(list(argv))
            return GhResult(
                0,
                json.dumps({"total_count": len(self._workflows), "workflow_runs": self._workflows}),
                "",
            )
        return super().__call__(argv, timeout)


def test_poller_passes_factory_required_checks_for_non_pr_runs() -> None:
    checks = [
        check_run(10, "ci-ok", "success", "2026-11-25T01:38:00Z", 100),
        check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200),
    ]
    workflows = [
        wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z"),
        wf_run(200, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
    ]
    plain = FACTORY.format(tickets="github", ship="merge")
    probe_required = plain.replace("skills: []\n---", "skills: []\nci:\n  required_checks: [probe]\n---")

    gh_plain = _DispatchPollGh(plain, checks, workflows)
    [ticket_plain] = LivePollSource(config(), gh_runner=gh_plain).github_candidates("orch:state/")
    assert ticket_plain.ci[gh_plain.head].status == "success"

    gh_req = _DispatchPollGh(probe_required, checks, workflows)
    [ticket_req] = LivePollSource(config(), gh_runner=gh_req).github_candidates("orch:state/")
    assert ticket_req.ci[gh_req.head].status == "failure"
    assert [c.name for c in ticket_req.ci[gh_req.head].failed] == ["probe"]


def test_last_read_cache_invalidates_when_required_checks_change() -> None:
    checks = [
        check_run(10, "ci-ok", "success", "2026-11-25T01:38:00Z", 100),
        check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200),
    ]
    workflows = [
        wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z"),
        wf_run(200, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
    ]
    plain = FACTORY.format(tickets="github", ship="merge")

    gh = _DispatchPollGh(plain, checks, workflows)
    source = LivePollSource(config(), gh_runner=gh)
    project = config().projects[0]
    first_factory = source._factory(project)
    first = source._github_ticket(project, first_factory, "12", probed="fp-1")
    assert first.ci[gh.head].status == "success"

    # Change factory.md required_checks while probe fingerprint stays identical.
    source._ticket_cache.clear()
    second_factory = replace(first_factory, ci_required_checks=("probe",))
    second = source._github_ticket(project, second_factory, "12", probed="fp-1")
    assert second.ci[gh.head].status == "failure"
    assert [c.name for c in second.ci[gh.head].failed] == ["probe"]


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [("failure", "success", "success"), ("success", "failure", "failure")],
    ids=["later-run-passes", "later-run-fails"],
)
def test_later_workflow_run_supersedes_by_run_order_not_job_start_time(
    first: str, second: str, expected: str
) -> None:
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(
            check_run(10, "probe", first, "2026-11-25T01:40:20Z", 41),
            check_run(20, "probe", second, "2026-11-25T01:40:05Z", 42),
        ),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(41, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:40:00Z"),
            wf_run(42, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:40:00Z"),
        ),
    )

    result = ci_required(actions.ci_status("owner/repo", HEAD, required_checks=("probe",)), ("probe",))

    assert result.status == expected


def test_check_from_another_app_is_not_discarded_as_the_dispatch_run_it_links_to() -> None:
    probe = check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200)
    report = {
        **check_run(30, "quality-report", "failure", "2026-11-25T01:41:00Z", 200),
        "app": {"slug": "quality-bot", "id": 777},
        "check_suite": {"id": 5555},
    }
    actions, gh = adapter()
    gh.route(
        CHECKS,
        checks_page(check_run(10, "test", "success", "2026-11-25T01:39:00Z", 100), probe, report),
    )
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        WORKFLOW_RUNS,
        wf_runs_page(
            wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:38:00Z"),
            wf_run(200, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
        ),
    )

    result = actions.ci_status("owner/repo", HEAD)

    assert result.status == "failure"
    assert [check.name for check in result.failed] == ["quality-report"]


def test_board_stops_showing_a_non_pr_probe_running_once_it_completes(tmp_path: Path) -> None:
    queue = Board(tmp_path).queue
    with queue.connect() as connection:
        connection.execute("INSERT INTO tracked_tickets VALUES ('acme/widgets', '12', 'github', 0)")
    workflows = [
        wf_run(100, "pull_request", ".github/workflows/ci.yml", "2026-11-25T01:37:55Z"),
        wf_run(200, "workflow_dispatch", ".github/workflows/uptime.yml", "2026-11-25T01:39:55Z"),
    ]
    green = check_run(10, "ci-ok", "success", "2026-11-25T01:38:00Z", 100)
    plain = FACTORY.format(tickets="github", ship="merge")

    def poll(probe: dict[str, Any], now: float) -> str:
        gh = _DispatchPollGh(plain, [green, probe], workflows)
        [ticket] = LivePollSource(config(), gh_runner=gh).github_candidates("orch:state/")
        record(queue.connect, [ticket], now, run_jobs=None, job_log=None, log=None)
        return ticket.ci[gh.head].status

    poll(check_run(20, "probe", None, "2026-11-25T01:40:00Z", 200), 1000.0)
    gate = poll(check_run(20, "probe", "failure", "2026-11-25T01:40:00Z", 200), 1060.0)

    with queue.connect() as connection:
        probe_row = connection.execute(
            "SELECT status, conclusion FROM ci_jobs WHERE check_name = 'probe'"
        ).fetchone()
    assert gate == "success"
    assert tuple(probe_row) == ("completed", "failure")
