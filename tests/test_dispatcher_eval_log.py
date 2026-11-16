import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from lib.cli import main
from lib.dispatcher import Dispatcher
from lib.eval_report import build_report
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
