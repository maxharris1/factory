"""#784: the QA worker and lane coordinator, driven tick by tick with the fake runner."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief.qa import Upload
from lib.handoff import parse_comment
from lib.qa_cloud import LaneRow, QaWorker, plan_slices, qa_id, s_id, save_name
from lib.qa_runners import LaunchFailed, RunnerOutage, UsageLimit
from lib.qa_runners.fake import FakeRunner
from lib.queue import JobRequest
from tests.qa_support import HEAD_A, HEAD_B, REPO, Harness, qa_config, ready, scenarios, verdict

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

BASE = "d" * 40
_BRIEF_IDS = re.compile(r'"id": "(s-[0-9]+)"')


def live(count: int) -> list[dict]:
    return [
        {
            "id": f"qa-{n}",
            "criterion": f"ac-{n}",
            "steps": f"Open page {n} with pack demo.",
            "passes": "the page shows its title",
            "pack": "demo",
            "saves": ["after screenshot"],
        }
        for n in range(1, count + 1)
    ]


class Lab:
    def __init__(
        self,
        tmp_path: Path,
        *,
        lanes: int = 3,
        runners: tuple[str, ...] = ("claude-cloud",),
        cap: int = 10,
        shootout: bool = False,
    ):
        qa = replace(qa_config(shootout=shootout), lanes=lanes, timeout_minutes=5)
        qa = replace(qa, runners=tuple(item for item in qa.runners if item.name in runners))
        self.run = Harness(tmp_path, qa)
        self.fakes = {name: FakeRunner(name) for name in ("claude-cloud", "cursor-cloud")}
        self.head_outcome: dict[str, str] = {}
        self.base_outcome: dict[str, str] = {}
        self.broken: dict[tuple[int, int], Callable[[dict], None]] = {}
        self.silent: set[tuple[int, int]] = set()
        self.posted: dict[str, tuple[int, str]] = {}
        self.cap = cap
        self.worker = self.new_worker()

    def new_worker(self) -> QaWorker:
        return QaWorker(
            self.run.queue,
            runners=self.fakes,
            factory=lambda repo: self.run.thread.base.factory,
            base_commit=lambda repo, branch, head: BASE,
            post=self.post,
            collect=self.collect,
            uploads=lambda row, paths: tuple(
                Upload(path, f"https://uploads.invalid/{row.lane_id}/{path}") for path in paths
            ),
            max_cloud_lanes=self.cap,
        )

    def post(self, repo: str, pr: int, body: str, marker: str) -> str:
        for comment_id, (_, posted) in self.posted.items():
            if marker in posted:
                return comment_id
        comment_id = f"qa-comment-{len(self.posted) + 1}"
        self.posted[comment_id] = (pr, body)
        return comment_id

    def collect(self, row: LaneRow) -> dict | None:
        if (row.slice, row.attempt) in self.silent:
            return None
        outcomes = self.base_outcome if row.kind != "head" else self.head_outcome
        result = {
            "run_id": row.run_id,
            "lane": row.lane_no,
            "kind": row.kind,
            "commit": row.commit,
            "base_commit": row.base_commit,
            "method": "fallback-playwright",
            "status": "PASS",
            "scenarios": [
                {"id": s_id(item), "criterion": "ac-1", "result": outcomes.get(item, "verified")}
                for item in row.scenarios
            ],
        }
        if (row.slice, row.attempt) in self.broken:
            self.broken[(row.slice, row.attempt)](result)
        return result

    def enqueue(self, *, ids: int = 5, regression: tuple[str, ...] = (), ticket: str = "784",
                runner: str = "claude-cloud", arm: str = "primary") -> str:
        detail = {
            "action": "qa", "head": HEAD_A, "scope": "full", "runner": runner, "arm": arm,
            "scenarios": [f"qa-{n}" for n in range(1, ids + 1)], "live": live(ids),
            "regression": list(regression), "run_id": f"run-{ticket}-{runner}", "pr_number": "833",
        }
        job = self.run.queue.enqueue(
            JobRequest(
                repo=REPO, ticket_id=ticket, action="qa", recipient="qa", source_comment_id=f"c-{ticket}",
                timeout_seconds=300, head=HEAD_A, detail=json.dumps(detail),
            ),
            dedupe_key=f"qa:{ticket}:{runner}:{arm}",
        )
        return job.id

    def tick(self, count: int = 1) -> None:
        for _ in range(count):
            self.worker.tick()
            self.run.now[0] += 30

    def lanes(self) -> list[tuple]:
        return self.run.rows("SELECT slice, kind, attempt, state, reason, runner FROM qa_lanes ORDER BY slice, attempt")

    def launched(self) -> list:
        return [lane for fake in self.fakes.values() for lane in fake.launched]

    def only_comment(self) -> str:
        assert len(self.posted) == 1, self.posted
        return next(iter(self.posted.values()))[1]

    def header(self) -> dict[str, str]:
        return parse_comment(self.only_comment()).as_dict()


def brief_ids(brief: str) -> set[str]:
    return set(_BRIEF_IDS.findall(brief.split("## Scenarios are fixed", 1)[1].split("## One repeat", 1)[0]))


@pytest.mark.parametrize("lanes", [1, 2, 3])
def test_five_scenarios_open_at_most_qa_lanes(tmp_path: Path, lanes: int) -> None:
    lab = Lab(tmp_path, lanes=lanes)
    lab.enqueue(ids=5)
    lab.tick(4)

    assert len(lab.launched()) == lanes
    assert {row[0] for row in lab.lanes()} == set(range(lanes))
    assert lab.header()["result"] == "PASS"
    assert lab.header()["lanes"] == str(lanes)


def test_each_brief_names_only_its_own_slice(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=3)
    lab.enqueue(ids=5)
    lab.tick(4)

    slices = [brief_ids(lane.brief) for lane in lab.launched()]
    assert sorted(len(item) for item in slices) == [1, 2, 2]
    assert set().union(*slices) == {f"s-{n}" for n in range(1, 6)}
    assert sum(len(item) for item in slices) == 5
    for lane in lab.launched():
        assert f"Test commit `{HEAD_A}`" in lane.brief


def test_regression_lane_brief_names_the_base_commit(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=3)
    lab.enqueue(ids=5, regression=("qa-2",))
    lab.tick(4)

    briefs = {frozenset(brief_ids(lane.brief)): lane.brief for lane in lab.launched()}
    regression = briefs[frozenset({"s-2"})]
    assert f"Test commit `{BASE}`" in regression
    assert "the base commit of pull request #833, not its head" in regression
    heads = [brief for ids, brief in briefs.items() if ids != {"s-2"}]
    assert len(heads) == 2 and all(f"Test commit `{HEAD_A}`" in brief for brief in heads)
    assert [row[1] for row in lab.lanes()] == ["head", "head", "regression"]


def test_qa_and_s_ids_round_trip() -> None:
    assert s_id("qa-12") == "s-12"
    assert qa_id(s_id("qa-12")) == "qa-12"
    assert save_name("after screenshot") == "after-screenshot.png"
    assert save_name("trace.zip") == "trace.zip"
    assert plan_slices([], (), 3) == ()
    assert [item.ids for item in plan_slices(["qa-1", "qa-2"], ("qa-1",), 1)] == [("qa-1", "qa-2")]


def test_result_without_commit_is_dropped_and_relaunched_once(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    lab.enqueue(ids=2)
    lab.broken[(0, 1)] = lambda result: result.pop("commit")
    lab.tick(4)

    assert lab.lanes() == [
        (0, "head", 1, "dropped", "wrong-commit", "claude-cloud"),
        (0, "head", 2, "reported", None, "claude-cloud"),
    ]
    assert len(lab.launched()) == 2
    assert lab.header()["result"] == "PASS"


def test_second_miss_makes_the_run_blocked(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    lab.enqueue(ids=2)
    lab.broken[(0, 1)] = lambda result: result.pop("commit")
    lab.broken[(0, 2)] = lambda result: result.update(method="by-hand")
    lab.tick(4)

    assert [row[3:5] for row in lab.lanes()] == [("dropped", "wrong-commit"), ("gap", "dropped-twice:wrong-method")]
    assert len(lab.launched()) == 2
    assert lab.header()["result"] == "BLOCKED"


def test_lane_ended_without_result_is_blocked(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    lab.enqueue(ids=2)
    lab.silent.add((0, 1))
    lab.tick()
    lab.fakes["claude-cloud"].end("claude-cloud-1")
    lab.tick(2)

    assert [row[3:5] for row in lab.lanes()] == [("gap", "ended-without-result")]
    assert len(lab.launched()) == 1
    assert lab.header()["result"] == "BLOCKED"
    assert "lane 1 (head): gap, ended-without-result" in lab.only_comment()


def test_lane_past_timeout_is_stopped_and_blocked(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    lab.enqueue(ids=2)
    lab.silent.add((0, 1))
    lab.tick(9)
    assert lab.posted == {}
    lab.tick(3)

    assert [row[3:5] for row in lab.lanes()] == [("timed-out", "no-result-in-time")]
    assert lab.fakes["claude-cloud"].stopped == ["claude-cloud-1"]
    assert lab.header()["result"] == "BLOCKED"


def test_failed_scenario_starts_exactly_one_base_check_lane(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=2)
    lab.enqueue(ids=3)
    lab.head_outcome["qa-3"] = "failed"
    lab.tick(8)

    base_checks = [row for row in lab.lanes() if row[1] == "base-check"]
    assert base_checks == [(2, "base-check", 1, "reported", None, "claude-cloud")]
    brief = lab.launched()[-1].brief
    assert brief_ids(brief) == {"s-3"} and f"Test commit `{BASE}`" in brief
    assert len(lab.launched()) == 3


def test_failure_also_on_base_is_follow_up(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    lab.enqueue(ids=2)
    lab.head_outcome["qa-2"] = "failed"
    lab.base_outcome["qa-2"] = "failed"
    lab.tick(5)

    assert lab.header()["result"] == "FAIL"
    assert "- [follow-up] qa-2 (ac-2): failed at the head commit and also on the base commit" in lab.only_comment()
    assert "[blocking]" not in lab.only_comment()


def test_failure_passing_on_base_is_blocking(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    lab.enqueue(ids=2)
    lab.head_outcome["qa-2"] = "failed"
    lab.tick(5)

    comment = lab.only_comment()
    assert lab.header()["result"] == "FAIL"
    assert "- [blocking] qa-2 (ac-2): failed at the head commit and not on the base commit" in comment
    assert "| qa-2 | ac-2 | Failed |" in comment and "| qa-1 | ac-1 | Verified |" in comment


def test_regression_result_spares_the_base_check(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=2)
    lab.enqueue(ids=2, regression=("qa-1",))
    lab.head_outcome["qa-1"] = "failed"
    lab.base_outcome["qa-1"] = "failed"
    lab.tick(5)

    assert [row[1] for row in lab.lanes()] == ["head", "regression"]
    assert "- [follow-up] qa-1 (ac-1)" in lab.only_comment()


def test_cap_two_three_jobs_two_run_one_lane_each_build_claimed(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=3, cap=2)
    jobs = []
    for ticket in ("1", "2", "3"):
        jobs.append(lab.enqueue(ids=3, ticket=ticket))
        lab.run.now[0] += 1
    lab.silent.update({(0, 1), (1, 1), (2, 1)})
    build = lab.run.queue.enqueue(
        JobRequest(repo=REPO, ticket_id="9", action="build", recipient="claude", source_comment_id="go-9",
                   timeout_seconds=3600),
        dedupe_key="build:9",
    )
    lab.tick()
    lab.tick()

    states = dict(lab.run.rows("SELECT id, state FROM jobs WHERE action = 'qa'"))
    assert [states[job] for job in jobs] == ["running", "running", "queued"]
    assert lab.run.rows("SELECT job_id, COUNT(*) FROM qa_lanes WHERE state = 'running' GROUP BY job_id ORDER BY job_id") == sorted(
        [(jobs[0], 1), (jobs[1], 1)]
    )
    assert lab.run.rows("SELECT last_activity_at FROM jobs WHERE id = ?", jobs[2]) == [(1033.0,)]
    claimed = lab.run.queue.claim("claude")
    assert claimed is not None and claimed.id == build.id


def _ready_thread(lab: Lab) -> str:
    lab.run.post("r1", ready(HEAD_A, body=scenarios("qa-1", "qa-2")), head=HEAD_A)
    lab.run.poll()
    return lab.run.rows("SELECT run_id FROM qa_runs")[0][0]


def test_usage_limit_moves_job_to_second_runner(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1, runners=("claude-cloud", "cursor-cloud"))
    _ready_thread(lab)
    lab.fakes["claude-cloud"].launch_errors.append(UsageLimit("at limit"))
    lab.tick(3)

    assert lab.fakes["claude-cloud"].launched == []
    assert len(lab.fakes["cursor-cloud"].launched) == 1
    assert lab.header()["runner"] == "cursor-cloud"
    assert lab.run.rows("SELECT runner FROM qa_runs") == [("cursor-cloud",)]
    lab.run.post("q1", lab.only_comment())
    lab.run.poll()
    assert lab.run.rows("SELECT outcome FROM handled_comments WHERE comment_id = 'q1'") == [("qa-reported",)]
    assert lab.run.rows("SELECT state, result FROM qa_runs") == [("reported", "PASS")]


def test_single_runner_usage_limit_requeues_attempt_unchanged(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    job = lab.enqueue(ids=2)
    lab.fakes["claude-cloud"].launch_errors.append(UsageLimit("at limit"))
    lab.tick()

    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]
    assert lab.run.rows("SELECT event, detail FROM job_events WHERE job_id = ?", job) == [
        ("qa-requeued", "claude-cloud:UsageLimit")
    ]
    assert lab.lanes() == []
    lab.tick(5)
    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", job) == [("queued",)]
    lab.tick(6)
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("done", 1)]


def test_shootout_arm_usage_limit_requeues_and_never_moves(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1, runners=("claude-cloud", "cursor-cloud"), shootout=True)
    job = lab.enqueue(ids=2)
    lab.fakes["claude-cloud"].launch_errors.append(UsageLimit("at limit"))
    lab.tick()

    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]
    assert lab.fakes["cursor-cloud"].launched == []


def test_cancelled_run_leaves_no_open_lane(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=2)
    _ready_thread(lab)
    lab.silent.update({(0, 1), (1, 1)})
    lab.tick(2)
    assert [row[3] for row in lab.lanes()] == ["running", "running"]
    lab.run.finish("codex", "v1")
    lab.run.post("v1", verdict(HEAD_A, "REVISE"))
    lab.run.poll()
    lab.run.finish("claude", "r2")
    lab.run.post("r2", ready(HEAD_B, "2/3"), head=HEAD_B)
    lab.run.poll()
    lab.tick()

    assert lab.run.rows("SELECT COUNT(*) FROM qa_lanes WHERE state IN ('launching', 'running')") == [(0,)]
    assert [row[3] for row in lab.lanes()] == ["cancelled", "cancelled"]
    assert sorted(lab.fakes["claude-cloud"].stopped) == ["claude-cloud-1", "claude-cloud-2"]
    assert lab.run.rows("SELECT state FROM jobs WHERE action = 'qa' AND head = ?", HEAD_A) == [("failed",)]
    assert all(f"head={HEAD_A}" not in body for _, body in lab.posted.values())


@pytest.mark.parametrize(
    ("setup", "result"),
    [
        (lambda lab: None, "PASS"),
        (lambda lab: lab.head_outcome.update({"qa-1": "failed"}), "FAIL"),
        (lambda lab: lab.head_outcome.update({"qa-1": "inconclusive"}), "BLOCKED"),
    ],
)
def test_comment_header_parses_for_every_outcome(tmp_path: Path, setup: Callable, result: str) -> None:
    lab = Lab(tmp_path, lanes=2)
    lab.enqueue(ids=2)
    setup(lab)
    lab.tick(6)

    header = lab.header()
    assert header == {
        "kind": "qa", "from": "grok-bot", "role": "qa", "head": HEAD_A, "result": result, "scope": "full",
        "lanes": "2", "env": "full-stack", "runner": "claude-cloud", "arm": "primary", "attempt": "1",
    }
    assert lab.only_comment().rstrip().endswith("<!-- qa-run run-784-claude-cloud -->")
    assert "(machine fake-4cpu-16gb, agent fake-1.0)" in lab.only_comment()


def test_smoke_run_posts_blocked_with_no_scenarios(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=2)
    job = lab.enqueue(ids=0)
    lab.tick()

    assert lab.launched() == []
    assert "lanes" not in lab.header() and lab.header()["result"] == "BLOCKED"
    assert "- no scenarios" in lab.only_comment()
    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", job) == [("done",)]


def test_restart_mid_run_converges(tmp_path: Path) -> None:
    steady = Lab(tmp_path / "steady", lanes=3)
    steady.enqueue(ids=5)
    steady.head_outcome["qa-4"] = "failed"
    steady.tick(8)

    restarted = Lab(tmp_path / "restarted", lanes=3)
    restarted.enqueue(ids=5)
    restarted.head_outcome["qa-4"] = "failed"
    for _ in range(8):
        restarted.worker = restarted.new_worker()
        restarted.tick()

    assert len(restarted.launched()) == len(steady.launched()) == 4
    assert [row[:4] for row in restarted.lanes()] == [row[:4] for row in steady.lanes()]
    assert restarted.only_comment() == steady.only_comment()


def test_heartbeat_keeps_running_job_alive(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    job = lab.enqueue(ids=1)
    lab.silent.add((0, 1))
    lab.tick(3)

    assert lab.run.rows("SELECT state, last_activity_at FROM jobs WHERE id = ?", job) == [("running", 1060.0)]


def test_old_detail_without_live_is_failed_once(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    job = lab.run.queue.enqueue(
        JobRequest(repo=REPO, ticket_id="1", action="qa", recipient="qa", source_comment_id="c", timeout_seconds=300,
                   detail=json.dumps({"action": "qa", "runner": "claude-cloud"})),
        dedupe_key="qa:old",
    )
    lab.tick(2)

    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", job.id) == [("failed",)]
    assert lab.launched() == []


def test_no_secret_reaches_a_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_SECRET_TOKEN", "hunter2-secret-value")
    lab = Lab(tmp_path, lanes=2)
    lab.enqueue(ids=3)
    lab.tick(4)

    assert lab.launched()
    for lane in lab.launched():
        assert "hunter2-secret-value" not in repr(lane)
        assert lane.environment_name == "full-stack" and lane.base_ref == "main"


def test_no_runner_registered_claims_nothing(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    job = lab.enqueue(ids=1)
    worker = lab.new_worker()
    worker.runners = {}
    worker.tick()

    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", job) == [("queued",)]


def test_failed_first_launch_of_a_multi_lane_run_posts_blocked(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=3)
    job = lab.enqueue(ids=5)
    lab.fakes["claude-cloud"].launch_errors.append(LaunchFailed("bad image"))
    lab.tick(3)

    assert lab.launched() == []
    assert lab.header()["result"] == "BLOCKED" and "lanes" not in lab.header()
    assert "lane 1 (head): gap, launch-failed:LaunchFailed" in lab.only_comment()
    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", job) == [("done",)]


def test_a_run_that_errors_every_tick_stops_its_heartbeat(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    job = lab.enqueue(ids=1)
    lab.tick()
    claimed_at = lab.run.rows("SELECT last_activity_at FROM jobs WHERE id = ?", job)

    def broken(row: LaneRow) -> dict | None:
        raise RuntimeError("collector down")

    lab.worker.collect = broken
    lab.tick(3)

    assert lab.run.rows("SELECT last_activity_at FROM jobs WHERE id = ?", job) == claimed_at


def test_a_failed_fallback_requeues_and_restores_the_run_runner(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1, runners=("claude-cloud", "cursor-cloud"))
    _ready_thread(lab)
    lab.fakes["claude-cloud"].launch_errors.append(UsageLimit("at limit"))
    lab.fakes["cursor-cloud"].launch_errors.append(RunnerOutage("down"))
    lab.tick()

    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE action = 'qa'") == [("queued", 1)]
    assert lab.run.rows("SELECT runner FROM qa_runs") == [("claude-cloud",)]
    assert lab.lanes() == []
    assert lab.posted == {}
