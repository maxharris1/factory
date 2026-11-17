from __future__ import annotations

import re

import pytest

from lib.ci_lane import SEGMENTS, ci_lane, progress, progress_text, segment
from lib.dashboard import CI_CONCLUSIONS
from lib.dashboard_pages.ci import ci_lane_html, ci_popup
from lib.dispatch.types import CHECK_FAILED

REPO = "maxharris1/cleffy"
LOCAL = ("pc-wsl-bots-1", "pc-wsl-bots-2", "pc-wsl-bots-3", "box-1")


def runner(name: str, labels: list[str], *, status: str = "online", busy: bool = False,
           repo: str = "maxharris1/factory") -> dict:
    return {"repo": repo, "name": name, "os": "Linux", "status": status, "busy": busy, "labels": labels}


def pool(*runners: dict) -> dict:
    return {"state": "live", "age_seconds": 12, "runners": list(runners)}


def full_pool(**overrides: dict) -> dict:
    found = [runner(name, ["self-hosted", "factory-linux"], **overrides.get(name, {})) for name in LOCAL]
    found.append(runner("maxs-macbook-pro", ["self-hosted", "factory-mac"]))
    return pool(*reversed(found))


def job(name: str, status: str = "in_progress", conclusion: str | None = None, *,
        on: str | None = None, hosted: bool = False) -> dict:
    kind = "github-hosted" if hosted else "self-hosted" if on else None
    return {"name": name, "status": status, "conclusion": conclusion, "run_id": "9100", "job_id": "9101",
            "runner_kind": kind, "runner_name": on, "created_at": 0.0, "started_at": 1.0 if status != "queued" else None,
            "completed_at": None, "elapsed_seconds": 40, "queue_seconds": 5}


def run(ticket: str, *jobs: dict, status: str = "in_progress", head: str = "b2c3d4e") -> dict:
    return {"repo": REPO, "ticket_id": ticket, "source": None, "pr": f"1{ticket}", "tracker": "github",
            "head_short": head, "status": status, "age_seconds": 300, "jobs": list(jobs)}


def snapshot(ci_pool: dict, runs: list[dict] = (), failures: list[dict] = (), tickets: list[dict] = ()) -> dict:
    return {"ci_pool": ci_pool, "ci_runs": {"runs": list(runs), "failures": list(failures)}, "tickets": list(tickets)}


def slots(html: str) -> list[str]:
    return re.findall(r'<div class="ci-slot" data-runner="([^"]+)">', html)


def group(html: str, name: str) -> str:
    return html.split(f'data-group="{name}"')[1]


def test_the_lane_has_one_slot_per_pool_runner_local_pool_first() -> None:
    twice = full_pool()
    twice["runners"].append(runner("pc-wsl-bots-1", ["self-hosted", "factory-linux"], repo=REPO))
    html = ci_lane_html(snapshot(twice))
    assert slots(html) == ["box-1", "pc-wsl-bots-1", "pc-wsl-bots-2", "pc-wsl-bots-3", "maxs-macbook-pro"]
    assert re.search(r'data-px-key="meter-ci"[^>]*>', html) and 'data-slots="5"' in html
    assert "0 running · 0 queued" in html and "pool live" in html


def test_a_free_runner_keeps_a_full_size_idle_box_and_an_offline_one_says_off() -> None:
    html = ci_lane_html(snapshot(full_pool(**{"box-1": {"status": "offline"}, "pc-wsl-bots-3": {"busy": True}})))
    tiles = dict(re.findall(r'data-runner="([^"]+)"><span class="ci-rn">[^<]*</span>(<div class="tile[^"]*")', html))
    assert tiles["pc-wsl-bots-1"] == '<div class="tile empty free"'
    assert tiles["box-1"] == '<div class="tile empty free"'
    assert tiles["pc-wsl-bots-3"] == '<div class="tile empty px-held"'
    lbls = dict(re.findall(r'data-runner="([^"]+)">.*?<div class="lbl">(\w+)</div>', html))
    assert (lbls["pc-wsl-bots-1"], lbls["box-1"], lbls["pc-wsl-bots-3"]) == ("idle", "off", "busy")


def test_overflow_to_github_hosted_runners_shows_a_labeled_group_after_the_slots() -> None:
    runs = [run(str(12 + i), job("pytest", on=name)) for i, name in enumerate(LOCAL)]
    runs.append(run("20", job("pytest (1/2)", hosted=True), job("pytest (2/2)", "queued")))
    html = ci_lane_html(snapshot(full_pool(), runs))
    lane = ci_lane(snapshot(full_pool(), runs))
    assert [s["state"] for s in lane["slots"]] == ["lead"] * 4 + ["idle"]
    assert lane["groups"] == [{"name": "hosted", "cards": [f"{REPO}#20"]}]
    assert html.index('class="tiles"') < html.index('data-group="hosted"')
    hosted = group(html, "hosted")
    assert '<span class="igroup">GitHub-hosted</span>' in hosted and 'data-ci="maxharris1/cleffy#20"' in hosted
    assert "5 running · 0 queued" in html


def test_a_ticket_on_three_runners_is_one_card_and_two_continuations() -> None:
    spread = run("13", job("pytest (1/3)", on="pc-wsl-bots-1"), job("pytest (2/3)", on="pc-wsl-bots-2"),
                 job("pytest (3/3)", on="pc-wsl-bots-3"))
    html = ci_lane_html(snapshot(full_pool(), [spread]))
    assert html.count('<div class="tile ci ') == 1
    assert re.findall(r'<span class="lbl">([^<]+)</span>', html) == [
        "↳ cleffy#13 · pytest (2/3)", "↳ cleffy#13 · pytest (3/3)"]


def test_a_runner_seen_on_a_job_but_not_in_the_pool_list_still_gets_a_slot() -> None:
    html = ci_lane_html(snapshot(pool(), [run("13", job("pytest", on="new-runner"))]))
    assert slots(html) == ["new-runner"] and "pool live" in html
    unread = ci_lane_html(snapshot({"state": "unread", "age_seconds": None, "runners": []}))
    assert slots(unread) == [] and "pool unread" in unread


@pytest.mark.parametrize(("status", "conclusion", "seg"), [
    ("in_progress", None, "running"),
    ("queued", None, "queued"),
    ("completed", "success", "passed"),
    ("completed", "failure", "failed"),
    ("completed", "cancelled", "failed"),
    ("completed", "timed_out", "failed"),
    ("completed", "neutral", "skipped"),
    ("completed", "skipped", "skipped"),
    ("completed", None, "skipped"),
])
def test_each_check_lands_in_one_segment(status: str, conclusion: str | None, seg: str) -> None:
    assert segment(job("x", status, conclusion)) == seg


def test_every_conclusion_has_a_segment_and_every_failure_is_a_conclusion() -> None:
    assert set(CHECK_FAILED) <= set(CI_CONCLUSIONS)
    assert {segment(job("x", "completed", c)) for c in CI_CONCLUSIONS} <= set(SEGMENTS)


def test_progress_counts_build_the_bar_and_its_text() -> None:
    jobs = ([job(f"ok {i}", "completed", "success") for i in range(4)]
            + [job(f"run {i}", on="pc-wsl-bots-1") for i in range(3)]
            + [job(f"wait {i}", "queued") for i in range(2)])
    p = progress(jobs)
    assert p == {"passed": 4, "failed": 0, "running": 3, "queued": 2, "skipped": 0, "total": 9}
    assert progress_text(p) == "4/9 passed, 3 running, 0 failed, 2 queued"
    assert progress_text({**p, "queued": 0, "total": 7}) == "4/7 passed, 3 running, 0 failed"
    html = ci_lane_html(snapshot(full_pool(), [run("13", *jobs)]))
    assert ('<div class="cibar" aria-hidden="true"><i class="passed" style="flex:4"></i>'
            '<i class="running" style="flex:3"></i><i class="queued" style="flex:2"></i></div>'
            '<span class="cibt">4/9 passed, 3 running, 0 failed, 2 queued</span>') in html


def test_a_failed_check_gets_a_red_segment_and_counts_as_failed() -> None:
    jobs = [job("lint", "completed", "success"), job("pytest", "completed", "failure"),
            job("e2e", on="pc-wsl-bots-1"), job("docs", "completed", "skipped")]
    p = progress(jobs)
    assert progress_text(p) == "1/4 passed, 1 running, 1 failed, 1 skipped"
    html = ci_lane_html(snapshot(full_pool(), [run("13", *jobs)]))
    assert ('<i class="passed" style="flex:1"></i><i class="failed" style="flex:1"></i>'
            '<i class="running" style="flex:1"></i><i class="skipped" style="flex:1"></i>') in html


def test_a_card_keeps_the_tile_lines_and_waits_then_review_round() -> None:
    ticket = {"repo": REPO, "ticket_id": "13", "source": None, "tracker": "github", "pr": "113",
              "ci_wait": {"reason": "ci-pending", "checks": ["build"], "waiting_seconds": 125, "round": 3}}
    html = ci_lane_html(snapshot(full_pool(), [run("13", job("pytest", on="pc-wsl-bots-1"))], tickets=[ticket]))
    card = html.split('data-ci="maxharris1/cleffy#13"')[1]
    for needle in ('<span class="ticket">cleffy#13</span>', f'href="https://github.com/{REPO}/pull/113"',
                   f'href="https://github.com/{REPO}/commit/b2c3d4e"', 'aria-label="Testing…"',
                   '<span class="action">then review</span>', '<span class="round">round 3</span>',
                   '<span class="t">waiting 2m 05s</span>', f'href="https://github.com/{REPO}/pull/113/checks"'):
        assert needle in card, needle
    assert re.search(r'data-px="scan" data-tint="#ff9e64"[^>]*data-px-key="ci-maxharris1/cleffy#13"', card)


def test_a_ticket_waiting_with_no_jobs_is_queued_and_a_recent_failure_with_nothing_running_is_failed() -> None:
    waiting = {"repo": REPO, "ticket_id": "14", "source": None, "tracker": "github", "pr": None,
               "ci_wait": {"reason": "approved-ci-pending", "checks": [], "waiting_seconds": 3700}}
    failure = {"repo": REPO, "ticket_id": "15", "source": None, "pr": "115", "tracker": "github",
               "head_short": "c3d4e5f", "name": "pytest", "conclusion": "failure", "run_id": "8800",
               "job_id": "8802", "tests": [], "more_tests": 0, "age_seconds": 600}
    html = ci_lane_html(snapshot(full_pool(), failures=[failure], tickets=[waiting]))
    queued = group(html, "queued")
    assert "then merge" in queued and "checks pending" in queued and "waiting 1h 01m" in queued
    failed = group(html, "failed")
    assert '<span class="igroup">Failed · 24 h</span>' in failed and 'aria-label="CI failed"' in failed
    assert "0/1 passed, 0 running, 1 failed" in failed and "failed 10m 00s ago" in failed
    assert '<span class="chip bad">1 failed in 24 h</span>' in html and "0 running · 1 queued" in html


def test_a_card_click_opens_the_ticket_with_the_ci_popup_and_its_job_rows() -> None:
    jobs = [job("lint", "completed", "success"), job("pytest", on="pc-wsl-bots-1")]
    shot = snapshot(full_pool(), [run("13", *jobs)])
    html = ci_lane_html(shot)
    assert ('<a class="cover" href="ticket.html#t=maxharris1%2Fcleffy%2313&amp;ci=1" '
            'aria-label="CI for cleffy#13"></a>') in html
    row = {"repo": REPO, "ticket_id": "13", "source": None, "tracker": "github"}
    popup = ci_popup(shot, f"{REPO.upper()}#13", row)
    assert popup.startswith('<dialog class="ci-pop" id="ci-pop" open="open">')
    assert '<a class="ci-x" href="ticket.html#t=maxharris1%2Fcleffy%2313" aria-label="close">✕</a>' in popup
    names = re.findall(r'<span class="ci-name">([^<]+)</span>', popup)
    assert names == ["lint", "pytest"]
    assert re.findall(r'<span class="ci-runner">([^<]+)</span>', popup) == [
        "runner unknown · queued 5s", "self-hosted · pc-wsl-bots-1 · queued 5s"]
    assert popup.count('class="ci-a log"') == 2 and "no CI failures in the last 24 h" in popup
