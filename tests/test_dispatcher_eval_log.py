import json
import shutil
from dataclasses import replace
from itertools import count
from pathlib import Path
from unittest.mock import patch

import pytest

from lib.cli import main
from lib.dispatcher import Dispatcher
from lib.eval_log import append_lines
from lib.eval_report import build_report
from lib.github_actions import GhResult
from lib.orch_yml import Project
from lib.poll_source import LivePollSource
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import (
    GO,
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    FakeSource,
    comment,
    setup,
    ticket,
)
from tests.test_live_poll import FACTORY, FakeGh, config

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "eval" / "messages"
REPO = "maxharris1/Cleffy"
START = 1_793_610_000.0
CLARIFY = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator -->\nGo.\n"


def cleffy(*comments):
    return replace(ticket(*comments, repo=REPO, ticket_id="7"), pr_number="7")


def bundle_from(state_dir: Path, bundle: Path) -> Path:
    bundle.mkdir()
    shutil.copy(state_dir / "dispatcher.log", bundle / "dispatcher.log")
    shutil.copytree(state_dir / "comments", bundle / "comments")
    shutil.copy(FIXTURE / "orch.yml", bundle / "orch.yml")
    shutil.copytree(FIXTURE / "projects", bundle / "projects")
    return bundle


def test_poll_appends_enter_comment_and_wake_lines_the_report_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [START]
    dispatcher, _ = setup(tmp_path, now)
    go = comment("101", GO.format(state="building"), author="grok-login")
    first = cleffy(go)
    dispatcher.poll_once(FakeSource(first, github=(first,)))
    now[0] += 600
    dispatcher.poll_once(FakeSource(cleffy(go, comment("102", CLARIFY, author="grok-login"))))

    log = tmp_path / "dispatcher.log"
    prefix = f"repo={REPO} ticket=7 pr=7"
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"2026-11-02T09:00:00Z {prefix} enter state=building",
        f"2026-11-02T09:00:00Z {prefix} comment path=comments/{REPO}/7/101.md",
        f"2026-11-02T09:10:00Z {prefix} wake role=orchestrator reason=clarify handled=102",
        f"2026-11-02T09:10:00Z {prefix} comment path=comments/{REPO}/7/102.md",
    ]
    assert (tmp_path / "comments" / REPO / "7" / "102.md").read_text(encoding="utf-8") == CLARIFY

    exported = log.read_text(encoding="utf-8")
    now[0] += 600
    dispatcher.poll_once(FakeSource(cleffy(go, comment("102", CLARIFY, author="grok-login"))))
    assert log.read_text(encoding="utf-8") == exported, "an unchanged poll adds no lines"

    monkeypatch.setenv("ORCH_DB", str(tmp_path / "d.db"))
    assert main(["log-message", "--repo", REPO, "--pr", "7", "--from", "grok-bot", "--to", "claude-bot"]) == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5 and lines[-1].endswith(
        f"repo={REPO} pr=7 message from=grok-bot to=claude-bot kind=handoff"
    )

    report = build_report(bundle_from(tmp_path, tmp_path / "bundle")).report
    assert report.runs == 1
    assert report.open_states == ((REPO, "7", "building"),)
    assert [(item.manual, item.scripted) for item in report.handoffs] == [(0, 2), (1, 0)]


def test_first_export_skips_rows_written_before_the_dispatcher_started(tmp_path: Path) -> None:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: START)
    queue.enqueue(
        JobRequest(
            repo=REPO,
            ticket_id="7",
            action="stuck",
            recipient="grok",
            source_comment_id="old",
            timeout_seconds=60,
        ),
        dedupe_key="old-wake",
    )
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
    )
    dispatcher.poll_once(FakeSource())
    assert not (tmp_path / "dispatcher.log").exists()
    with queue.connect() as connection:
        assert connection.execute("SELECT error FROM poll_errors").fetchall() == []


def test_a_merge_absent_from_the_poll_keeps_the_projects_spelling(tmp_path: Path) -> None:
    now = [START]
    dispatcher, _ = setup(tmp_path, now)
    first = cleffy(comment("101", GO.format(state="building"), author="grok-login"))
    dispatcher.poll_once(FakeSource(first, github=(first,)))
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))

    def runner(argv, timeout):
        endpoint = next(part for part in argv if part.startswith(("repos/", "search/")))
        if endpoint == f"repos/{REPO}/pulls/7":
            merged = {"merged": True, "head": {"sha": gh.head}, "html_url": "u"}
            return GhResult(0, json.dumps(merged), "")
        return gh(argv, timeout)

    source = LivePollSource(
        replace(config(), projects=(Project(REPO, factory_ref="main"),), linear=None),
        gh_runner=runner,
        terminal_sink=dispatcher.record_terminal,
    )
    now[0] += 600
    dispatcher.poll_once(source)

    assert ("maxharris1/cleffy", "7") not in dispatcher._polled
    lines = (tmp_path / "dispatcher.log").read_text(encoding="utf-8").splitlines()
    assert lines[-2:] == [
        f"2026-11-02T09:10:00Z repo={REPO} ticket=7 enter state=merged",
        f"2026-11-02T09:10:00Z repo={REPO} ticket=7 end",
    ]
    report = build_report(bundle_from(tmp_path, tmp_path / "bundle")).report
    assert report.runs == 1
    assert report.open_states == ()


def test_a_crash_after_the_append_leaves_the_log_readable(tmp_path: Path) -> None:
    clock = count(START, 1).__next__
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=clock)

    def dispatcher() -> Dispatcher:
        return Dispatcher(
            queue.path,
            queue,
            trusted_logins=TRUSTED_LOGINS,
            review_actions=FakeReviewActions(),
            grok_delivery=FakeGrokDelivery(),
        )

    first = cleffy(comment("101", GO.format(state="building"), author="grok-login"))

    def append_then_crash(path: Path, lines: list[str]) -> None:
        append_lines(path, lines)
        raise SystemExit("crash after the fsync")

    with patch("lib.dispatch.eval_log.append_lines", append_then_crash):
        with pytest.raises(SystemExit):
            dispatcher().poll_once(FakeSource(first, github=(first,)))
    log = tmp_path / "dispatcher.log"
    exported = log.read_text(encoding="utf-8")
    assert len(exported.splitlines()) == 2

    dispatcher().poll_once(FakeSource(first))
    assert log.read_text(encoding="utf-8") == exported, "recovery appends no line twice"
    assert build_report(bundle_from(tmp_path, tmp_path / "bundle")).report.runs == 1


def test_a_crash_before_the_append_appends_the_batch_once(tmp_path: Path) -> None:
    now = [START]
    dispatcher, _ = setup(tmp_path, now)
    first = cleffy(comment("101", GO.format(state="building"), author="grok-login"))

    with patch("lib.dispatch.eval_log.append_lines", side_effect=SystemExit("crash")):
        with pytest.raises(SystemExit):
            dispatcher.poll_once(FakeSource(first, github=(first,)))
    log = tmp_path / "dispatcher.log"
    assert not log.exists()

    now[0] += 600
    dispatcher.poll_once(FakeSource(first))
    prefix = f"repo={REPO} ticket=7 pr=7"
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"2026-11-02T09:00:00Z {prefix} enter state=building",
        f"2026-11-02T09:00:00Z {prefix} comment path=comments/{REPO}/7/101.md",
    ]
