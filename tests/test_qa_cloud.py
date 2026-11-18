"""#784: the QA worker and lane coordinator, driven tick by tick with the fake runner."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from lib import lane_limit
from lib.dispatch.schema import ensure_qa_lanes
from lib.handoff import parse_comment
from lib.qa_cloud import LaneRow, QaWorker, plan_slices, qa_id, s_id, save_name
from lib.qa_runners import LaunchFailed, RunnerOutage, UsageLimit
from lib.qa_runners.fake import FakeRunner
from lib.queue import JobRequest
from tests.qa_support import (
    HEAD_A,
    HEAD_B,
    REPO,
    SIGNATURE,
    STORE,
    Harness,
    SigningStorage,
    qa_config,
    ready,
    scenarios,
    verdict,
)

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

BASE = "d" * 40
_BRIEF_IDS = re.compile(r'"id": "(s-[0-9]+)"')


def live(count: int, packs: dict[str, str | None] | None = None) -> list[dict]:
    packs = packs or {}
    return [
        {
            "id": f"qa-{n}",
            "criterion": f"ac-{n}",
            "steps": f"Open page {n} with pack demo.",
            "passes": "the page shows its title",
            "pack": packs.get(f"qa-{n}", "demo"),
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
        shares_usage_with: dict[str, str] | None = None,
    ):
        qa = replace(qa_config(shootout=shootout), lanes=lanes, timeout_minutes=5)
        qa = replace(qa, runners=tuple(item for name in runners for item in qa.runners if item.name == name))
        self.run = Harness(tmp_path, qa)
        self.fakes = {name: FakeRunner(name) for name in ("claude-cloud", "cursor-cloud")}
        self.head_outcome: dict[str, str] = {}
        self.base_outcome: dict[str, str] = {}
        self.broken: dict[tuple[int, int], Callable[[dict], None]] = {}
        self.silent: set[tuple[int, int]] = set()
        self.posted: dict[str, tuple[int, str]] = {}
        self.storage = SigningStorage()
        self.logs: list[str] = []
        self.cap = cap
        self.shares_usage_with = shares_usage_with or {}
        self.worker = self.new_worker()

    def new_worker(self) -> QaWorker:
        return QaWorker(
            self.run.queue,
            runners=self.fakes,
            factory=lambda repo: self.run.thread.base.factory,
            base_commit=lambda repo, branch, head: BASE,
            post=self.post,
            collect=self.collect,
            storage=lambda repo: self.storage,
            max_cloud_lanes=self.cap,
            shares_usage_with=self.shares_usage_with,
            log=self.logs.append,
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
                runner: str = "claude-cloud", arm: str = "primary",
                packs: dict[str, str | None] | None = None) -> str:
        detail = {
            "action": "qa", "head": HEAD_A, "scope": "full", "runner": runner, "arm": arm,
            "scenarios": [f"qa-{n}" for n in range(1, ids + 1)], "live": live(ids, packs),
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


def test_every_lane_row_has_its_own_lane_number(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=2)
    lab.enqueue(ids=3)
    lab.broken[(0, 1)] = lambda result: result.pop("commit")
    lab.head_outcome["qa-3"] = "failed"
    lab.tick(10)

    assert lab.run.rows("SELECT slice, attempt, kind, lane_no, state FROM qa_lanes ORDER BY lane_no") == [
        (0, 1, "head", 1, "dropped"),
        (1, 1, "head", 2, "reported"),
        (0, 2, "head", 3, "reported"),
        (2, 1, "base-check", 4, "reported"),
    ]
    assert [lane.brief.split("\n", 1)[0] for lane in lab.launched()] == [
        f"# QA lane {n} brief" for n in (1, 2, 3, 4)
    ]
    assert lab.header()["result"] == "FAIL"
    assert "lane 4 (base-check)" in lab.only_comment()


def test_lane_brief_carries_signed_links(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=2)
    lab.enqueue(ids=4, packs={"qa-1": "demo", "qa-2": "demo", "qa-3": "other", "qa-4": None})
    lab.tick(4)

    lane_nos = dict(lab.run.rows("SELECT lane_id, lane_no FROM qa_lanes"))
    packs = {1: {"demo"}, 2: {"other"}}
    assert len(lab.launched()) == 2
    for lane in lab.launched():
        n = lane_nos[lane.lane_id]
        prefix = f"{STORE}/qa/factory/run-784-claude-cloud/lane-{n}/"
        assert f"{prefix}result.json?up={SIGNATURE}" in lane.brief
        assert f"{prefix}bundle.tar.gz?up={SIGNATURE}" in lane.brief
        uploads = set(re.findall(rf"{re.escape(STORE)}/qa/[^?]+\?up=", lane.brief))
        assert uploads == {f"{prefix}result.json?up=", f"{prefix}bundle.tar.gz?up="}
        downloads = re.findall(rf"{re.escape(STORE)}/packs/([^?]+)\?down={SIGNATURE}", lane.brief)
        assert sorted(downloads) == sorted(packs[n])
        assert SIGNATURE not in repr(lane)
    assert {expires for _, _, expires in lab.storage.signed} == {5 * 60}
    assert lab.header()["result"] == "PASS"
    assert all(SIGNATURE not in line for line in lab.logs)
    assert all(SIGNATURE not in body for _, body in lab.posted.values())


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
    assert lab.run.rows("SELECT detail FROM job_events WHERE event = 'qa-requeued'") == [("cursor-cloud:RunnerOutage",)]
    assert lab.lanes() == []
    assert lab.posted == {}


def test_usage_limit_requeue_outlasts_the_backoff_and_retries(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    job = lab.enqueue(ids=2)
    lab.fakes["claude-cloud"].launch_errors.append(UsageLimit("at limit"))
    lab.tick()
    while lab.run.now[0] < 1300:
        lab.tick()
    lab.run.dispatcher.reconcile_jobs()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]

    lab.tick()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("done", 1)]
    assert len(lab.launched()) == 1
    assert lab.run.rows("SELECT event FROM job_events WHERE job_id = ? AND event = 'qa-no-retry'", job) == []


def test_a_restarted_worker_keeps_the_runner_backoff(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    job = lab.enqueue(ids=2)
    fake = lab.fakes["claude-cloud"]
    launch = fake.launch
    called_at: list[float] = []

    def timed_launch(lane):
        called_at.append(lab.run.now[0])
        return launch(lane)

    fake.launch = timed_launch
    fake.launch_errors.append(UsageLimit("at limit"))
    while lab.run.now[0] < 1300:
        lab.worker = lab.new_worker()
        lab.tick()
    assert called_at == [1000.0]

    lab.run.dispatcher.reconcile_jobs()
    lab.worker = lab.new_worker()
    lab.tick()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("done", 1)]
    assert len(fake.launched) == 1


def _cancel_by_new_head(lab: Lab) -> None:
    lab.run.finish("codex", "v1")
    lab.run.post("v1", verdict(HEAD_A, "REVISE"))
    lab.run.poll()
    lab.run.finish("claude", "r2")
    lab.run.post("r2", ready(HEAD_B, "2/3"), head=HEAD_B)
    lab.run.poll()


def test_cancel_keeps_a_lane_open_until_its_stop_succeeds(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    _ready_thread(lab)
    lab.silent.add((0, 1))
    lab.tick(2)
    _cancel_by_new_head(lab)
    lab.fakes["claude-cloud"].stop_errors.append(ConnectionError("reset"))
    lab.tick()

    assert [row[3] for row in lab.lanes()] == ["running"]
    assert lab.run.rows("SELECT state FROM jobs WHERE action = 'qa' AND head = ?", HEAD_A) == [("failed",)]

    lab.tick()
    assert [row[3] for row in lab.lanes()] == ["cancelled"]
    assert lab.fakes["claude-cloud"].stopped == ["claude-cloud-1"]
    assert lab.fakes["claude-cloud"].agents == {"claude-cloud-1": "ended"}


def test_cancel_stops_an_agent_launched_before_a_crash_saved_its_id(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1)
    _ready_thread(lab)
    fake = lab.fakes["claude-cloud"]
    launch = fake.launch

    def launch_then_crash(lane):
        launch(lane)
        raise SystemExit("worker killed")

    fake.launch = launch_then_crash
    with pytest.raises(SystemExit):
        lab.tick()
    fake.launch = launch
    assert [(row[3], row[1]) for row in lab.lanes()] == [("launching", "head")]

    _cancel_by_new_head(lab)
    lab.worker = lab.new_worker()
    lab.tick()

    assert [row[3] for row in lab.lanes()] == ["cancelled"]
    assert fake.stopped == ["claude-cloud-1"]
    assert len(fake.launched) == 1


SHARED = {"claude-cloud": "claude"}


def _build(lab: Lab, recipient: str = "claude") -> str:
    return lab.run.queue.enqueue(
        JobRequest(repo=REPO, ticket_id="9", action="build", recipient=recipient, source_comment_id="go-9",
                   timeout_seconds=3600),
        dedupe_key=f"build:9:{recipient}",
    ).id


def _pause(lab: Lab, recipient: str, seconds: int) -> lane_limit.LaneLimit:
    now = lab.run.now[0]
    with lab.run.queue.connect() as connection:
        limit = lane_limit.record(connection, job_id=_build(lab, recipient), harness=recipient,
                                  bot=f"{recipient}-bot", limit=lane_limit.UsageLimit(int(now) + seconds), now=now)
    assert limit is not None
    return limit


def _paused_pair(lab: Lab) -> tuple[str, str, lane_limit.LaneLimit]:
    held = lab.enqueue(ids=2, ticket="1", runner="claude-cloud")
    free = lab.enqueue(ids=2, ticket="2", runner="cursor-cloud")
    limit = _pause(lab, "claude", 3600)
    lab.silent.add((0, 1))
    lab.tick(3)
    return held, free, limit


def test_paused_recipient_holds_its_sharing_runner_and_not_another(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1, runners=("claude-cloud", "cursor-cloud"), shares_usage_with=SHARED)
    held, free, limit = _paused_pair(lab)

    last_tick = lab.run.now[0] - 30
    assert lab.run.rows("SELECT state, last_activity_at FROM jobs WHERE id = ?", held) == [("queued", last_tick)]
    assert lab.run.rows("SELECT event, detail FROM job_events WHERE job_id = ?", held) == [
        ("qa-yielded", f"claude: {lane_limit.label(limit)}")
    ]
    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", free) == [("running",)]
    assert lab.fakes["claude-cloud"].launched == []
    assert len(lab.fakes["cursor-cloud"].launched) == 1


def test_waiting_job_is_claimed_when_the_pause_ends(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1, runners=("claude-cloud", "cursor-cloud"), shares_usage_with=SHARED)
    held, _, limit = _paused_pair(lab)
    lab.run.now[0] = limit.until + 1
    lab.tick()

    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", held) == [("running",)]
    assert len(lab.fakes["claude-cloud"].launched) == 1


def test_paused_recipient_grants_no_extra_lane(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=3, shares_usage_with=SHARED)
    lab.enqueue(ids=5)
    lab.silent.add((0, 1))
    fake = lab.fakes["claude-cloud"]
    launch = fake.launch
    paused: list[lane_limit.LaneLimit] = []

    def launch_then_pause(lane):
        launched = launch(lane)
        paused.append(_pause(lab, "claude", 90))
        return launched

    fake.launch = launch_then_pause
    lab.tick()
    fake.launch = launch
    assert len(lab.fakes["claude-cloud"].launched) == 1

    limit = paused[0]
    lab.tick(2)
    assert len(lab.fakes["claude-cloud"].launched) == 1
    assert [row[3] for row in lab.lanes()] == ["running"]

    lab.run.now[0] = limit.until
    lab.tick(2)
    assert len(lab.fakes["claude-cloud"].launched) == 3


def test_sharing_runner_usage_limit_requeues_behind_recipient_builds(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1, shares_usage_with=SHARED)
    job = lab.enqueue(ids=2)
    lab.fakes["claude-cloud"].launch_errors.append(UsageLimit("at limit"))
    lab.tick()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]

    build = _build(lab)
    while lab.run.now[0] < 1400:
        lab.tick()
    lab.run.dispatcher.reconcile_jobs()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]
    assert lab.run.rows("SELECT detail FROM job_events WHERE job_id = ? AND event = 'qa-yielded'", job) == [
        ("behind queued claude jobs",)
    ]
    assert lab.fakes["claude-cloud"].launched == []

    claimed = lab.run.queue.claim("claude")
    assert claimed is not None and claimed.id == build
    lab.tick()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("done", 1)]
    assert len(lab.fakes["claude-cloud"].launched) == 1


@pytest.mark.parametrize(
    ("shares", "moved"),
    [
        ({"claude-cloud": "claude", "cursor-cloud": "claude"}, False),
        ({"claude-cloud": "claude", "cursor-cloud": "cursor"}, True),
    ],
)
def test_usage_limit_falls_back_only_to_another_account(
    tmp_path: Path, shares: dict[str, str], moved: bool
) -> None:
    lab = Lab(tmp_path, lanes=1, runners=("claude-cloud", "cursor-cloud"), shares_usage_with=shares)
    job = lab.enqueue(ids=2)
    lab.fakes["claude-cloud"].launch_errors.append(UsageLimit("at limit"))
    lab.tick()

    assert len(lab.fakes["cursor-cloud"].launched) == int(moved)
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("done" if moved else "queued", 1)]


BOTH = {"claude-cloud": "claude", "cursor-cloud": "cursor"}


@pytest.mark.parametrize("restart", [False, True], ids=["queue", "queue-restart"])
def test_usage_limit_before_a_failed_fallback_still_waits_behind_recipient_builds(
    tmp_path: Path, restart: bool
) -> None:
    lab = Lab(tmp_path, lanes=1, runners=("claude-cloud", "cursor-cloud"), shares_usage_with=BOTH)
    job = lab.enqueue(ids=2)
    lab.fakes["claude-cloud"].launch_errors.append(UsageLimit("at limit"))
    lab.fakes["cursor-cloud"].launch_errors.append(RunnerOutage("down"))
    lab.tick()
    assert lab.run.rows("SELECT event, detail FROM job_events WHERE job_id = ?", job) == [
        ("qa-requeued", "claude-cloud:UsageLimit,cursor-cloud:RunnerOutage")
    ]

    build = _build(lab)
    lab.run.now[0] = 1301
    if restart:
        lab.worker = lab.new_worker()
    lab.tick()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]
    assert lab.run.rows("SELECT detail FROM job_events WHERE job_id = ? AND event = 'qa-yielded'", job) == [
        ("behind queued claude jobs",)
    ]
    assert lab.launched() == []

    claimed = lab.run.queue.claim("claude")
    assert claimed is not None and claimed.id == build
    lab.tick()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("done", 1)]
    assert len(lab.fakes["claude-cloud"].launched) == 1


@pytest.mark.parametrize("error", [RunnerOutage("down"), UsageLimit("at limit")], ids=["outage", "usage-limit"])
def test_a_runner_whose_account_is_paused_is_not_a_fallback(tmp_path: Path, error: Exception) -> None:
    lab = Lab(tmp_path, lanes=1, runners=("claude-cloud", "cursor-cloud"), shares_usage_with=BOTH)
    job = lab.enqueue(ids=2)
    _pause(lab, "cursor", 3600)
    lab.fakes["claude-cloud"].launch_errors.append(error)
    lab.tick()

    assert lab.launched() == []
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]
    assert lab.run.rows("SELECT detail FROM job_events WHERE job_id = ? AND event = 'qa-requeued'", job) == [
        (f"claude-cloud:{type(error).__name__}",)
    ]


@pytest.mark.parametrize(
    ("setup", "result"),
    [
        (lambda lab: lab.head_outcome.update({"qa-2": "failed"}), "FAIL"),
        (lambda lab: lab.broken.update({(0, 1): lambda result: result.pop("commit")}), "PASS"),
    ],
    ids=["base-check", "retry"],
)
def test_paused_account_launches_no_new_lane_until_the_pause_ends(
    tmp_path: Path, setup: Callable, result: str
) -> None:
    lab = Lab(tmp_path, lanes=1, shares_usage_with=SHARED)
    job = lab.enqueue(ids=2)
    setup(lab)
    lab.silent.add((0, 1))
    lab.tick()
    assert len(lab.fakes["claude-cloud"].launched) == 1

    limit = _pause(lab, "claude", 3600)
    lab.silent.clear()
    lab.tick(3)
    assert len(lab.fakes["claude-cloud"].launched) == 1
    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", job) == [("running",)]
    assert lab.posted == {}

    lab.run.now[0] = limit.until
    lab.tick()
    assert len(lab.fakes["claude-cloud"].launched) == 2
    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", job) == [("done",)]
    assert lab.header()["result"] == result


class _Crash(BaseException):
    pass


OWED_ORDERS = [
    pytest.param(("claude-cloud", "cursor-cloud"), RunnerOutage("down"), "cursor", id="fallback-limited"),
    pytest.param(("cursor-cloud", "claude-cloud"), RunnerOutage("down"), "claude", id="reversed-fallback-limited"),
    pytest.param(("claude-cloud", "cursor-cloud"), UsageLimit("at limit"), "cursor", id="both-limited"),
]


@pytest.mark.parametrize("restart", [False, True], ids=["queue", "queue-restart"])
@pytest.mark.parametrize(("order", "first_error", "owed"), OWED_ORDERS)
def test_a_limited_fallback_is_not_launched_until_its_recipient_jobs_are_claimed(
    tmp_path: Path, order: tuple[str, str], first_error: Exception, owed: str, restart: bool
) -> None:
    original, fallback = order
    lab = Lab(tmp_path, lanes=1, runners=order, shares_usage_with=BOTH)
    job = lab.enqueue(ids=2, runner=original)
    lab.fakes[original].launch_errors.append(first_error)
    lab.fakes[fallback].launch_errors.append(UsageLimit("at limit"))
    lab.tick()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]

    build = _build(lab, owed)
    lab.run.now[0] = 1301
    if restart:
        lab.worker = lab.new_worker()
    lab.fakes[original].launch_errors.append(RunnerOutage("down"))
    lab.tick()
    assert lab.launched() == []
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]
    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", build) == [("queued",)]

    lab.run.now[0] = 1700
    if restart:
        lab.worker = lab.new_worker()
    lab.fakes[original].launch_errors.append(RunnerOutage("down"))
    lab.tick()
    assert lab.launched() == []
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("queued", 1)]

    claimed = lab.run.queue.claim(owed)
    assert claimed is not None and claimed.id == build
    lab.run.now[0] = 2100
    lab.fakes[original].launch_errors.append(RunnerOutage("down"))
    lab.tick()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("done", 1)]
    assert len(lab.fakes[fallback].launched) == 1
    assert lab.fakes[original].launched == []


def test_a_lane_moved_to_a_limited_fallback_before_a_crash_waits_after_restart(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1, runners=("claude-cloud", "cursor-cloud"), shares_usage_with=BOTH)
    job = lab.enqueue(ids=2)
    lab.fakes["claude-cloud"].launch_errors.append(RunnerOutage("down"))
    lab.fakes["cursor-cloud"].launch_errors.append(UsageLimit("at limit"))
    lab.tick()

    lab.run.now[0] = 1301
    lab.fakes["claude-cloud"].launch_errors.append(RunnerOutage("down"))
    lab.fakes["cursor-cloud"].launch_errors.append(_Crash())
    with pytest.raises(_Crash):
        lab.worker.tick()
    assert [row[5] for row in lab.lanes()] == ["cursor-cloud"]

    build = _build(lab, "cursor")
    lab.worker = lab.new_worker()
    lab.tick(2)
    assert lab.launched() == []
    assert lab.run.rows("SELECT state FROM jobs WHERE id = ?", job) == [("running",)]

    claimed = lab.run.queue.claim("cursor")
    assert claimed is not None and claimed.id == build
    lab.tick()
    assert lab.run.rows("SELECT state, attempt FROM jobs WHERE id = ?", job) == [("done", 1)]
    assert len(lab.fakes["cursor-cloud"].launched) == 1


def test_a_lanes_table_capped_at_lane_three_is_widened_and_keeps_its_rows(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "old.db")
    ensure_qa_lanes(connection)
    old = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'qa_lanes'").fetchone()[0]
    connection.execute("DROP TABLE qa_lanes")
    connection.execute(old.replace("BETWEEN 1 AND 8", "BETWEEN 1 AND 3"))
    insert = (
        "INSERT INTO qa_lanes (lane_id, run_id, job_id, slice, kind, lane_no, scenarios, commit_sha, base_commit,"
        " runner, state, attempt, started_at) VALUES (?, 'run', 'job', ?, 'head', ?, '[]', ?, ?, 'r', 'reported', 1, 0)"
    )
    connection.execute(insert, ("lane-3", 2, 3, HEAD_A, BASE))
    connection.commit()

    ensure_qa_lanes(connection)
    connection.execute(insert, ("lane-4", 3, 4, HEAD_A, BASE))

    assert connection.execute("SELECT lane_id, lane_no FROM qa_lanes ORDER BY lane_no").fetchall() == [
        ("lane-3", 3), ("lane-4", 4),
    ]
