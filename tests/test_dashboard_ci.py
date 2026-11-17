from __future__ import annotations

import copy
import json
import re
import sqlite3
from pathlib import Path

import pytest

from lib.ci_lane import ci_lane
from lib.dashboard import collect_snapshot, render_pages, tickets_json, write_snapshot
from lib.dashboard_pages.ci import ci_popup
from lib.dashboard_pages.ticket_detail import ticket_view
from tests import test_dashboard_activity as activity
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_lanes import Browser, _page_with_status_stub, needs_chrome
from tests.test_dashboard_layout import BAR, _no_script, _section, needs_node, run_harness

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ("issue-717-ci-1440.png", "issue-717-ci-375.png")
REPO = "maxharris1/cleffy"
HEAD_A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
HEAD_B = "b2c3d4e5f60718293a4b5c6d7e8f901234567890"
HEAD_C = "c3d4e5f60718293a4b5c6d7e8f90123456789012"
HEAD_D = "d4e5f60718293a4b5c6d7e8f9012345678901234"


def add_ci(
    connection: sqlite3.Connection, ticket: str, head: str, name: str, status: str,
    conclusion: str | None = None, **columns: object,
) -> None:
    row = {"repo": REPO, "ticket_id": ticket, "head": head, "workflow": "actions-run:1",
           "check_name": name, "status": status, "conclusion": conclusion, "created_at": NOW - 600, "seen_at": NOW - 5, **columns}
    connection.execute(
        f"INSERT INTO ci_jobs ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values())
    )


def snapshot_with(tmp_path: Path, seed) -> dict:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        seed(connection)
    return collect_snapshot(db, log, now=NOW)


def ci_fixture(connection: sqlite3.Connection) -> None:
    for shard, runner, kind in ((1, None, "github-hosted"), (2, "pc-wsl-bots-1", "self-hosted"),
                                (3, "pc-wsl-bots-2", "self-hosted")):
        add_ci(connection, "13", HEAD_B, f"pytest ({shard}/4)", "in_progress", pr="113", run_id="9100",
               job_id=f"910{shard}", created_at=NOW - 300, started_at=NOW - 300 + 20 * shard,
               runner_name=runner, runner_kind=kind)
    add_ci(connection, "13", HEAD_B, "pytest (4/4)", "queued", pr="113", run_id="9100", job_id="9104",
           created_at=NOW - 300)
    add_ci(connection, "13", HEAD_B, "lint", "completed", "success", pr="113", run_id="9100", job_id="9105",
           created_at=NOW - 300, started_at=NOW - 290, completed_at=NOW - 250, runner_kind="github-hosted")
    add_ci(connection, "12", HEAD_A, "pytest", "queued", pr="112", run_id="9200", job_id="9201",
           created_at=NOW - 60)
    add_ci(connection, "14", HEAD_C, "pytest", "completed", "success", pr="114", created_at=NOW - 900,
           started_at=NOW - 880, completed_at=NOW - 700, runner_kind="github-hosted")
    add_ci(connection, "13", HEAD_D, "pytest (2/4)", "completed", "failure", pr="113", run_id="8800",
           job_id="8802", created_at=NOW - 4000, started_at=NOW - 3990, completed_at=NOW - 3600,
           runner_name="pc-wsl-bots-1", runner_kind="self-hosted", seen_at=NOW - 3500,
           failing_tests=json.dumps(["tests/test_dashboard_lanes.py::test_queue_belt_moves",
                                     "tests/test_feeder.py::test_waiting_on_capacity"]))
    add_ci(connection, "12", HEAD_C, "lint", "completed", "timed_out", pr="112", run_id="8700", job_id="8701",
           created_at=NOW - 8000, started_at=NOW - 7990, completed_at=NOW - 7200, runner_kind="github-hosted",
           seen_at=NOW - 7000)


def ci_snapshot(tmp_path: Path) -> dict:
    return snapshot_with(tmp_path, ci_fixture)


def _popup(snapshot: dict, ticket: str = "12") -> str:
    row = {"repo": REPO, "ticket_id": ticket, "source": None, "tracker": "github"}
    return ci_popup(snapshot, f"{REPO}#{ticket}", row)


def _ci_lane(html: str) -> str:
    start = html.index('<div class="lane" data-device="ci"')
    depth = 0
    for tag in re.finditer(r"<(/?)div\b[^>]*>", html[start:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return html[start : start + tag.end()]
    raise AssertionError("unclosed CI lane")


def _lane(snapshot: dict) -> str:
    return _ci_lane(_no_script(render_pages(snapshot)["index.html"]))


def _text(markup: str) -> str:
    return re.sub(r"<[^>]+>", " ", markup)


def test_only_queued_and_running_runs_are_listed_newest_first(tmp_path: Path) -> None:
    runs = ci_snapshot(tmp_path)["ci_runs"]["runs"]
    assert [(run["ticket_id"], run["head_short"], run["status"], run["pr"]) for run in runs] == [
        ("12", HEAD_A[:7], "queued", "112"),
        ("13", HEAD_B[:7], "in_progress", "113"),
    ]
    jobs = runs[1]["jobs"]
    assert [job["name"] for job in jobs] == ["lint", "pytest (1/4)", "pytest (2/4)", "pytest (3/4)", "pytest (4/4)"]
    lint, first = jobs[:2]
    assert (first["elapsed_seconds"], first["queue_seconds"]) == (280, 20)
    assert (lint["status"], lint["conclusion"], lint["elapsed_seconds"], lint["queue_seconds"]) == (
        "completed", "success", 40, 10)
    waiting = jobs[4]
    assert (waiting["started_at"], waiting["elapsed_seconds"], waiting["queue_seconds"]) == (None, None, 300)


def test_the_lane_cards_link_pr_and_head_and_the_popup_links_job_logs(tmp_path: Path) -> None:
    snapshot = ci_snapshot(tmp_path)
    lane = _lane(snapshot)
    assert lane.count('<div class="tile ci ') == 2
    for href in (f"https://github.com/{REPO}/pull/113", f"https://github.com/{REPO}/commit/{HEAD_B[:7]}",
                 f"https://github.com/{REPO}/pull/112", f"https://github.com/{REPO}/commit/{HEAD_A[:7]}"):
        assert f'href="{href}"' in lane, href
    assert f'href="https://github.com/{REPO}/actions/runs/9100/job/9102"' in _popup(snapshot, "13")
    assert HEAD_C[:7] not in lane and HEAD_B not in lane
    assert "ci-job" not in lane


@pytest.mark.parametrize(("jobs", "listed"), [
    ([("queued", None), ("queued", None)], "queued"),
    ([("in_progress", None), ("queued", None)], "in_progress"),
    ([("completed", "success"), ("queued", None)], "in_progress"),
    ([("in_progress", None), ("completed", "failure")], "in_progress"),
    ([("completed", "success"), ("completed", "skipped")], None),
    ([("completed", "success"), ("completed", "failure")], None),
])
def test_a_runs_status_is_derived_from_its_jobs(tmp_path: Path, jobs: list, listed: str | None) -> None:
    def seed(connection: sqlite3.Connection) -> None:
        for i, (status, conclusion) in enumerate(jobs):
            add_ci(connection, "12", HEAD_A, f"job {i}", status, conclusion)

    runs = snapshot_with(tmp_path, seed)["ci_runs"]["runs"]
    assert [run["status"] for run in runs] == ([listed] if listed else [])


def test_runner_cells_name_the_runner_or_say_what_it_waits_for(tmp_path: Path) -> None:
    def seed(connection: sqlite3.Connection) -> None:
        add_ci(connection, "12", HEAD_A, "self", "in_progress", started_at=NOW - 500,
               runner_name="pc-wsl-bots-1", runner_kind="self-hosted")
        add_ci(connection, "12", HEAD_A, "hosted", "in_progress", started_at=NOW - 590, runner_kind="github-hosted")
        add_ci(connection, "12", HEAD_A, "waiting", "queued", created_at=NOW - 75)
        add_ci(connection, "12", HEAD_A, "skipped", "completed", "cancelled", runner_kind="none")
        add_ci(connection, "12", HEAD_A, "unlooked", "in_progress", started_at=NOW - 600)

    runners = re.findall(r'<span class="ci-runner">([^<]*)</span>', _popup(snapshot_with(tmp_path, seed)))
    assert runners == [
        "GitHub-hosted · queued 10s",
        "self-hosted · pc-wsl-bots-1 · queued 1m 40s",
        "no runner",
        "runner unknown · queued 0s",
        "waiting for runner · queued 1m 15s",
    ]


def test_recent_failures_name_the_failing_tests_with_a_flake_search(tmp_path: Path) -> None:
    snapshot = ci_snapshot(tmp_path)
    failures = snapshot["ci_runs"]["failures"]
    assert [(f["ticket_id"], f["name"], f["conclusion"], f["head_short"]) for f in failures] == [
        ("13", "pytest (2/4)", "failure", HEAD_D[:7]),
        ("12", "lint", "timed_out", HEAD_C[:7]),
    ]
    assert failures[0]["tests"] == ["tests/test_dashboard_lanes.py::test_queue_belt_moves",
                                    "tests/test_feeder.py::test_waiting_on_capacity"]
    section = _popup(snapshot, "13")
    failed = section[section.index("Recent failures (24 h)"):]
    assert f'href="https://github.com/{REPO}/actions/runs/8800/job/8802"' in failed
    assert (f'href="https://github.com/{REPO}/issues?q=is%3Aissue%20%22tests%2Ftest_feeder.py%3A%3A'
            'test_waiting_on_capacity%22"') in failed
    assert ">tests/test_feeder.py::test_waiting_on_capacity</a>" in failed
    assert "timed out" not in _text(failed) and "timed out" in _text(_popup(snapshot, "12"))


def test_failures_keep_a_day_newest_first_and_at_most_ten(tmp_path: Path) -> None:
    def seed(connection: sqlite3.Connection) -> None:
        for i in range(12):
            add_ci(connection, "12", HEAD_A, f"shard {i}", "completed", "failure", completed_at=NOW - 100 * i)
        add_ci(connection, "12", HEAD_B, "old", "completed", "failure", completed_at=NOW - 90000,
               seen_at=NOW - 86401)
        add_ci(connection, "12", HEAD_B, "skipped", "completed", "skipped", completed_at=NOW)

    failures = snapshot_with(tmp_path, seed)["ci_runs"]["failures"]
    assert [f["name"] for f in failures] == [f"shard {i}" for i in range(10)]


def test_a_failure_is_listed_for_a_day_from_when_it_completed_not_when_last_seen(tmp_path: Path) -> None:
    def seed(connection: sqlite3.Connection) -> None:
        add_ci(connection, "12", HEAD_A, "fresh", "completed", "failure", completed_at=NOW - 23 * 3600)
        add_ci(connection, "12", HEAD_B, "stale", "completed", "failure", completed_at=NOW - 3 * 86400)

    snapshot = snapshot_with(tmp_path, seed)
    failures = snapshot["ci_runs"]["failures"]
    assert [(f["name"], f["age_seconds"]) for f in failures] == [("fresh", 23 * 3600)]
    failed = _popup(snapshot).split("Recent failures (24 h)")[1]
    assert "fresh" in failed and "stale" not in failed and HEAD_B[:7] not in failed


def test_a_log_link_comes_only_from_github_job_ids(tmp_path: Path) -> None:
    def seed(connection: sqlite3.Connection) -> None:
        add_ci(connection, "12", HEAD_A, "from-url", "queued",
               log_url=f"https://github.com/{REPO}/actions/runs/71/job/72")
        add_ci(connection, "12", HEAD_A, "elsewhere", "queued", log_url="https://ci.example.invalid/runs/1/job/2")
        add_ci(connection, "12", HEAD_A, "bad-ids", "queued", run_id="1;drop", job_id="2")

    section = _popup(snapshot_with(tmp_path, seed))
    assert re.findall(r'class="ci-a log" href="([^"]*)"', section) == [
        f"https://github.com/{REPO}/actions/runs/71/job/72"]
    assert "example.invalid" not in section


def test_ci_strings_are_redacted(tmp_path: Path) -> None:
    token = "ghp_" + "Q7x" * 8

    def seed(connection: sqlite3.Connection) -> None:
        add_ci(connection, "12", HEAD_A, f"deploy {token}", "in_progress", started_at=NOW - 5,
               runner_name=f"runner-{token}", runner_kind="self-hosted")
        add_ci(connection, "12", HEAD_B, "unit", "completed", "failure", failing_tests=json.dumps([f"t {token}"]))

    out = tmp_path / "out"
    write_snapshot(out, snapshot_with(tmp_path, seed))
    text = (out / "status.json").read_text(encoding="utf-8") + (out / "index.html").read_text(encoding="utf-8")
    assert token not in text
    assert HEAD_A not in text and HEAD_A[:7] in text


def test_a_database_without_ci_jobs_shows_an_empty_lane_and_popup(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE ci_jobs")
    snapshot = collect_snapshot(db, log, now=NOW)
    assert snapshot["ci_runs"] == {"runs": [], "failures": []}
    popup = _popup(snapshot)
    assert "no CI queued or running" in popup and "no CI failures in the last 24 h" in popup
    lane = _lane(snapshot)
    assert "0 running · 0 queued" in lane and '<div class="tile ci ' not in lane


def test_the_ci_lane_replaces_the_ci_section_and_the_waiting_lane(tmp_path: Path) -> None:
    html = render_pages(ci_snapshot(tmp_path))["index.html"]
    assert 'id="ci-runs"' not in html and 'data-device="ci-wait"' not in html
    lanes = _section(_no_script(html), "lanes")
    assert re.findall(r'data-device="([^"]+)"', lanes)[-1] == "ci"
    assert "1 running · 1 queued" in _text(_lane(ci_snapshot(tmp_path / "again")))


def test_a_rerun_with_its_times_cleared_shows_no_made_up_ages(tmp_path: Path) -> None:
    def seed(connection: sqlite3.Connection) -> None:
        add_ci(connection, "12", HEAD_A, "pytest", "queued", created_at=None)

    snapshot = snapshot_with(tmp_path, seed)
    (run,) = snapshot["ci_runs"]["runs"]
    assert (run["status"], run["age_seconds"], run["jobs"][0]["queue_seconds"]) == ("queued", None, None)
    section = _popup(snapshot)
    assert "started" not in section and '<span class="ci-runner">waiting for runner</span>' in section


@needs_node
def test_the_script_draws_a_rerun_as_the_server_does(tmp_path: Path) -> None:
    def seed(connection: sqlite3.Connection) -> None:
        add_ci(connection, "12", HEAD_A, "pytest", "queued", created_at=None)

    snapshot = snapshot_with(tmp_path, seed)
    html = render_pages(snapshot)["index.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    out = run_harness(tmp_path, activity.HARNESS, script, snapshot, "main", IDS, [["snap", ["lanes"]]])
    assert _ci_lane(out["steps"][0]["snap"]["lanes"]) == _lane(snapshot)


IDS = BAR + ["hud", "lanes", "just-happened", "intake"]


def _moved(snapshot: dict, seconds: int, status: str, conclusion: str | None = None) -> dict:
    later = copy.deepcopy(snapshot)
    later["generated_at_epoch"] += seconds
    run = later["ci_runs"]["runs"][0]
    job = run["jobs"][0]
    job["status"], job["conclusion"] = status, conclusion
    job["started_at"] = NOW - 10
    job["runner_kind"], job["runner_name"] = "self-hosted", "pc-wsl-bots-1"
    job["queue_seconds"], job["elapsed_seconds"] = 50, 10 + seconds
    if status == "completed":
        job["completed_at"] = NOW + seconds
        later["ci_runs"]["runs"].pop(0)
    else:
        run["status"] = "in_progress"
    later["ci_lane"] = ci_lane(later)
    return later


@needs_node
def test_the_script_redraws_the_lane_live_as_the_server_draws_it(tmp_path: Path) -> None:
    first = ci_snapshot(tmp_path)
    running = _moved(first, 3, "in_progress")
    passed = _moved(first, 5, "completed", "success")
    html = render_pages(first)["index.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    snap = ["snap", ["lanes"]]
    actions = [snap, ["advance", 3], snap, ["poll", running], snap, ["advance", 2], ["poll", passed], snap]
    out = run_harness(tmp_path, activity.HARNESS, script, first, "main", IDS, actions)
    seen = [_ci_lane(step["snap"]["lanes"]) for step in out["steps"] if "snap" in step]
    assert out["fetches"] == 3
    assert seen[0] == _lane(first)
    assert seen[2] == _lane(running)
    assert seen[3] == _lane(passed)
    assert "started 5m 03s ago" in _text(seen[1])
    assert "1 running · 1 queued" in _text(seen[0])
    assert "2 running · 0 queued" in _text(seen[2]) and "1 running · 0 queued" in _text(seen[3])
    assert seen[3].count('<div class="tile ci ') == 2 and 'data-group="failed"' in seen[3]
    assert "1 failed in 24 h" in _text(seen[3]) and 'aria-label="CI failed"' in seen[3]
    assert "ci-job" not in "".join(seen)


@needs_node
def test_the_ticket_page_script_draws_the_ci_popup_as_the_server_does(tmp_path: Path) -> None:
    snapshot = ci_snapshot(tmp_path)
    address = f"{REPO}#13"
    card = re.search(r'<a class="cover" href="([^"]+)"', _lane(snapshot).split('data-ci="maxharris1/cleffy#13"')[1])
    assert card.group(1).replace("&amp;", "&") == "ticket.html#t=maxharris1%2Fcleffy%2313&ci=1"
    server = ticket_view(snapshot, address, ci=True)
    dialog = server[server.index("<dialog"):server.index("</dialog>") + 9]
    assert 'id="ci-pop"' in dialog and dialog.count('<li class="ci-job') == 5
    assert "actions/runs/8800/job/8802" in dialog and "test_waiting_on_capacity" in dialog
    html = render_pages(snapshot)["ticket.html"]
    (script,) = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    out = run_harness(tmp_path, activity.HARNESS, script, snapshot, "ticket", BAR + ["hud", "ticketview"],
                      [["snap", ["ticketview"]]], "#t=maxharris1%2Fcleffy%2313&ci=1", tickets_json(snapshot))
    drawn = out["steps"][0]["snap"]["ticketview"]
    assert "cleffy#13" in drawn
    assert drawn[drawn.index("<dialog"):drawn.index("</dialog>") + 9] == dialog
    closed = run_harness(tmp_path / "closed", activity.HARNESS, script, snapshot, "ticket",
                         BAR + ["hud", "ticketview"], [["snap", ["ticketview"]]], "#t=maxharris1%2Fcleffy%2313",
                         tickets_json(snapshot))["steps"][0]["snap"]["ticketview"]
    assert "<dialog" not in closed and 'class="ci-open" href="ticket.html#t=maxharris1%2Fcleffy%2313&amp;ci=1"' in closed


CI_LAYOUT = """(function () {
  var cards = [].map.call(document.querySelectorAll('#lanes .lane[data-device="ci"] .tile'), function (t) {
    var b = t.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.right)]; });
  return {cards: cards, scrollWidth: document.documentElement.scrollWidth};
})()"""


@needs_chrome
def test_the_phone_page_has_no_sideways_scroll(tmp_path: Path) -> None:
    browser = Browser()
    try:
        browser.open(_page_with_status_stub(ci_snapshot(tmp_path), "index.html"), 375)
        found = browser.eval(CI_LAYOUT)
    finally:
        browser.close()
    assert len(found["cards"]) == 3, found
    assert all(0 <= left and right <= 375 for left, right in found["cards"]), found
    assert found["scrollWidth"] <= 375, found


def test_review_screenshots_are_committed() -> None:
    for name in SHOTS:
        assert (ROOT / "docs" / "screenshots" / name).stat().st_size > 0, name

