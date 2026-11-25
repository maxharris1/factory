from __future__ import annotations

import copy
import json
import re
import sqlite3
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot, render_pages
from lib.orch_yml import OrchConfig, QaWorkerConfig
from tests import test_dashboard_layout as layout
from tests.test_dashboard import NOW, add_job, add_ticket, make_db
from tests.test_dashboard_layout import BAR, _no_script, _run_page, _section
from tests.test_dashboard_tickets_board import CHART, _card, _script, _server, _text

REPO = "maxharris1/factory"
RUNNING = ("500", "run-500")
REPORTED = ("501", "0123456789abcdef" * 2)
SHOOTOUT = ("501", "fedcba9876543210" * 2)
NO_QA = "502"
CAP = 2


def _run(connection, run_id: str, ticket_id: str, *, repo: str = REPO, head: str = "h2", scope: str = "full",
         runner: str = "claude-cloud", arm: str = "primary", state: str = "reported", result: str | None = None,
         started_at: float = NOW - 900, finished_at: float | None = None) -> None:
    connection.execute(
        "INSERT INTO qa_runs (run_id, repo, ticket_id, head, round, scope, runner, arm, job_id, state, result, "
        "started_at, finished_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, repo, ticket_id, head, scope, runner, arm, None if scope == "none" else f"j-{run_id[:8]}",
         state, result, started_at, finished_at),
    )


def _lane(connection, run_id: str, slice_: int, asked: list[str], *, kind: str = "head", attempt: int = 1,
          state: str = "reported", runner: str = "claude-cloud", results: dict[str, str] | None = None) -> None:
    result = None if results is None else json.dumps(
        {"scenarios": [{"id": "s-" + item.removeprefix("qa-"), "result": word} for item, word in results.items()]}
    )
    connection.execute(
        "INSERT INTO qa_lanes (lane_id, run_id, job_id, slice, kind, lane_no, scenarios, commit_sha, base_commit, "
        "runner, agent_url, state, attempt, result, started_at) "
        "VALUES (?, ?, 'j', ?, ?, 1, ?, 'c', 'b', ?, 'https://agent.example.invalid/a', ?, ?, ?, ?)",
        (f"{run_id}-{slice_}-{attempt}", run_id, slice_, kind, json.dumps(asked), runner, state, attempt, result,
         NOW - 600),
    )


def qa_snapshot(tmp_path: Path) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        for ticket_id, state in ((RUNNING[0], "ship"), (REPORTED[0], "ship"), (NO_QA, "review")):
            add_ticket(connection, REPO, ticket_id, state=state)
        _run(connection, RUNNING[1], RUNNING[0], repo=REPO.upper(), state="running", started_at=NOW - 125)
        _lane(connection, RUNNING[1], 0, ["qa-1"], state="running")
        _lane(connection, RUNNING[1], 1, ["qa-2"], state="launching")

        _run(connection, "a" * 32, REPORTED[0], head="h1", result="PASS", finished_at=NOW - 2000)
        _run(connection, SHOOTOUT[1], SHOOTOUT[0], runner="cursor-cloud", arm="shootout", result="FAIL",
             finished_at=NOW - 400)
        _lane(connection, SHOOTOUT[1], 0, ["qa-1"], runner="cursor-cloud", results={"qa-1": "failed"})
        _run(connection, REPORTED[1], REPORTED[0], result="PASS", finished_at=NOW - 300)
        _lane(connection, REPORTED[1], 0, ["qa-1", "qa-2"], results={"qa-1": "verified", "qa-2": "failed"})
        _lane(connection, REPORTED[1], 1, ["qa-3", "qa-4"], state="gap")
        _lane(connection, REPORTED[1], 1, ["qa-3", "qa-4"], attempt=2,
              results={"qa-3": "verified", "qa-4": "verified"})
        _lane(connection, REPORTED[1], 2, ["qa-2"], kind="base-check", results={"qa-2": "verified"})

        _run(connection, "b" * 32, NO_QA, scope="none", state="reported", result="PASS", finished_at=NOW - 10)
        _run(connection, "c" * 32, NO_QA, state="cancelled")
        _run(connection, "d" * 32, "999", state="running")

        for job_id, repo, ticket_id in (("j-qa-1", "maxharris1/cleffy", "12"), ("j-qa-2", "maxharris1/cleffy", "13"),
                                        ("j-qa-3", REPO, "503")):
            add_job(connection, job_id, action="qa", recipient="qa", repo=repo, ticket_id=ticket_id)
    log = tmp_path / "poller.log"
    log.write_text("cycle\n", encoding="utf-8")
    orch = OrchConfig(poll_seconds=30, projects=(), qa=QaWorkerConfig(max_cloud_lanes=CAP))
    return collect_snapshot(db, log, now=NOW, chart_path=CHART, orch=orch)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> dict:
    return qa_snapshot(tmp_path_factory.mktemp("qa"))


def test_the_snapshot_carries_each_tracked_tickets_runs_at_its_newest_head(snapshot: dict) -> None:
    assert snapshot["qa"]["runs"] == [
        {"repo": REPO.upper(), "ticket_id": RUNNING[0], "runner": "claude-cloud", "arm": "primary",
         "state": "running", "scope": "full", "result": None, "lanes": 2, "passed": None, "total": None,
         "elapsed_seconds": 125, "run": None},
        {"repo": REPO, "ticket_id": REPORTED[0], "runner": "claude-cloud", "arm": "primary",
         "state": "reported", "scope": "full", "result": "PASS", "lanes": 2, "passed": 3, "total": 4,
         "elapsed_seconds": 600, "run": [REPORTED[1][:16], REPORTED[1][16:]]},
        {"repo": REPO, "ticket_id": SHOOTOUT[0], "runner": "cursor-cloud", "arm": "shootout",
         "state": "reported", "scope": "full", "result": "FAIL", "lanes": 1, "passed": 0, "total": 1,
         "elapsed_seconds": 500, "run": [SHOOTOUT[1][:16], SHOOTOUT[1][16:]]},
    ]
    assert snapshot["qa"]["cap"] == CAP
    assert snapshot["qa"]["runners"] == [
        {"runner": "claude-cloud", "open": 2},
        {"runner": "cursor-cloud", "open": 0},
        {"runner": "local-docker", "open": 0},
    ]
    assert snapshot["qa"]["queued"] == [{"repo": "maxharris1/cleffy", "count": 2}, {"repo": REPO, "count": 1}]
    assert "agent.example" not in json.dumps(snapshot)


def test_a_database_without_qa_tables_reads_as_no_qa(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    sqlite3.connect(db).close()
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    assert collect_snapshot(db, log, now=NOW)["qa"] == {
        "runs": [],
        "cap": None,
        "runners": [
            {"runner": "claude-cloud", "open": 0},
            {"runner": "cursor-cloud", "open": 0},
            {"runner": "local-docker", "open": 0},
        ],
        "queued": [],
    }


def _qa_lines(card: dict) -> list[str]:
    return [_text(line) for line in card["kids"][-1]["kids"] if "tk-run" in line["attrs"].get("class", "")
            and _text(line).startswith("QA")]


def _links(card: dict) -> list[str]:
    found = []

    def walk(node: dict) -> None:
        for kid in node["kids"]:
            if isinstance(kid, dict):
                if kid["tag"] == "a" and kid["attrs"]["href"].startswith("/qa/"):
                    found.append(kid["attrs"]["href"])
                walk(kid)

    walk(card)
    return found


def test_ac1_a_running_run_shows_runner_state_scope_lanes_and_elapsed(snapshot: dict) -> None:
    card = _card(_server(snapshot)["pipeline"], f"{REPO}#{RUNNING[0]}")
    assert _qa_lines(card) == ["QAclaude-cloudrunning · full · 2 lanes2m 05s"]
    assert _links(card) == []


def test_ac1_ac2_a_reported_head_shows_scenarios_evidence_and_primary_before_shootout(snapshot: dict) -> None:
    card = _card(_server(snapshot)["pipeline"], f"{REPO}#{REPORTED[0]}")
    assert _qa_lines(card) == [
        "QA PASSclaude-cloudreported · full · 2 lanes · 3/4 scenariosevidence10m 00s",
        "QA FAILcursor-cloudshootoutreported · full · 1 lane · 0/1 scenariosevidence8m 20s",
    ]
    assert _links(card) == [f"/qa/runs/{REPORTED[1]}", f"/qa/runs/{SHOOTOUT[1]}"]


def test_ac1_the_chip_tone_follows_state_and_result(snapshot: dict) -> None:
    pipeline = _server(snapshot)["pipeline"]
    tones = re.findall(r'<span class="chip (\w+)">(QA[^<]*)</span>', pipeline)
    assert tones == [("accent", "QA"), ("ok", "QA PASS"), ("bad", "QA FAIL")]


def test_ac4_a_card_with_no_run_is_byte_identical_to_one_without_qa(snapshot: dict) -> None:
    bare = copy.deepcopy(snapshot)
    del bare["qa"]
    with_qa, without = _server(snapshot)["pipeline"], _server(bare)["pipeline"]
    key = f'data-ticket="{REPO}#{NO_QA}"'

    def card(markup: str) -> str:
        start = markup.index(key)
        return markup[start:markup.index("</div></div>", start)]

    assert card(with_qa) == card(without)
    assert "QA" not in card(with_qa)
    assert render_pages({**bare, "qa": {"runs": [], "cap": None, "runners": [], "queued": []}})["job.html"] == (
        render_pages(bare)["job.html"]
    )


@layout.needs_node
def test_the_script_draws_the_qa_lines_like_the_server(tmp_path: Path, snapshot: dict) -> None:
    out = _script(tmp_path, snapshot)
    server = _server(snapshot)
    assert out["html"]["pipeline"] == server["pipeline"]
    assert f'href="/qa/runs/{REPORTED[1]}"' in out["html"]["pipeline"]


def _qa_lane(lanes: str) -> str:
    start = lanes.index('<div class="lane" data-device="qa">')
    return lanes[start:re.compile(r'<div class="lane[" ]').search(lanes, start + 1).start()]


def test_ac3_the_lanes_page_shows_open_lanes_against_the_cap_and_queued_qa_jobs(snapshot: dict) -> None:
    lane = _qa_lane(_section(_no_script(render_pages(snapshot)["index.html"]), "lanes"))
    items = re.findall(r"<li>(.*?)</li>", lane)
    text = [re.sub(r"<[^>]+>", " ", item).split() for item in items]
    assert text == [
        ["claude-cloud", "2", "/", "2", "lanes", "at", "cap"],
        ["cursor-cloud", "0", "/", "2", "lanes"],
        ["local-docker", "0", "/", "2", "lanes"],
        ["maxharris1/cleffy", "2", "waiting"],
        [REPO, "1", "waiting"],
    ]
    assert '<div class="sub">3 waiting</div>' in lane


def test_ac3_without_a_cap_the_lane_shows_a_question_mark(snapshot: dict) -> None:
    capless = {**snapshot, "qa": {**snapshot["qa"], "cap": None}}
    lane = _qa_lane(_section(_no_script(render_pages(capless)["index.html"]), "lanes"))
    assert "2 / ? lanes" in lane and "at cap" not in lane


def test_ac3_the_qa_lane_is_left_out_when_nothing_is_open_or_queued(snapshot: dict) -> None:
    quiet = {**snapshot, "qa": {**snapshot["qa"], "queued": [],
                                "runners": [{"runner": "claude-cloud", "open": 0}, {"runner": "cursor-cloud", "open": 0}]}}
    assert 'data-device="qa"' not in render_pages(quiet)["index.html"]


@layout.needs_node
def test_ac3_the_script_draws_the_qa_lane_like_the_server(tmp_path: Path, snapshot: dict) -> None:
    server = _qa_lane(_section(_no_script(render_pages(snapshot)["index.html"]), "lanes"))
    script = _run_page(tmp_path / "busy", snapshot, "main", BAR + ["hud", "lanes"])["html"]["lanes"]
    assert _qa_lane(script) == server
    quiet = {**snapshot, "qa": {**snapshot["qa"], "queued": [],
                                "runners": [{"runner": "claude-cloud", "open": 0}, {"runner": "cursor-cloud", "open": 0}]}}
    bare = _run_page(tmp_path / "quiet", quiet, "main", BAR + ["hud", "lanes"])["html"]["lanes"]
    assert 'data-device="qa"' not in bare


def _one_ticket(tmp_path: Path, build) -> dict:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, REPO, RUNNING[0], state="ship")
        build(connection)
    log = tmp_path / "poller.log"
    log.write_text("cycle\n", encoding="utf-8")
    return collect_snapshot(db, log, now=NOW, chart_path=CHART)


def test_ac1_scenarios_count_only_the_head_slices_and_lanes_count_every_launched_slice(tmp_path: Path) -> None:
    run_id = "e" * 32

    def build(connection) -> None:
        _run(connection, run_id, RUNNING[0], result="PASS", finished_at=NOW - 300)
        _lane(connection, run_id, 0, ["qa-1"], results={"qa-1": "verified"})
        _lane(connection, run_id, 1, ["qa-2"], results={"qa-2": "verified"})
        _lane(connection, run_id, 2, ["qa-2"], kind="regression", results={"qa-2": "failed"})

    snapshot = _one_ticket(tmp_path, build)
    [run] = snapshot["qa"]["runs"]
    assert (run["lanes"], run["passed"], run["total"]) == (3, 2, 2)
    card = _card(_server(snapshot)["pipeline"], f"{REPO}#{RUNNING[0]}")
    assert _qa_lines(card) == ["QA PASSclaude-cloudreported · full · 3 lanes · 2/2 scenariosevidence10m 00s"]


@pytest.mark.parametrize("newer", [
    {"scope": "targeted", "state": "cancelled"},
    {"scope": "targeted", "state": "stale"},
    {"scope": "none", "state": "reported", "result": "PASS", "finished_at": NOW - 10},
])
def test_a_hidden_run_at_the_newest_head_never_brings_back_an_older_heads_result(tmp_path: Path, newer: dict) -> None:
    def build(connection) -> None:
        _run(connection, "a" * 32, RUNNING[0], head="ha", result="PASS", finished_at=NOW - 2000)
        _lane(connection, "a" * 32, 0, ["qa-1"], results={"qa-1": "verified"})
        _run(connection, "b" * 32, RUNNING[0], head="hb", **newer)

    snapshot = _one_ticket(tmp_path, build)
    assert snapshot["qa"]["runs"] == []
    card = _card(_server(snapshot)["pipeline"], f"{REPO}#{RUNNING[0]}")
    assert _qa_lines(card) == []
    assert _links(card) == []
