"""#784: the runner registry and the fake runner."""

from __future__ import annotations

import pytest

from lib import qa_runners
from lib.factory_md import QA_RUNNERS, parse_factory_md
from lib.qa_runners import Lane, LaunchFailed, LaunchError, RunnerOutage, UsageLimit
from lib.qa_runners.fake import FakeRunner

LANE = Lane("lane-1", "brief", "acme/widgets", "main", "full-stack", None)


def test_fake_registers_by_name_and_starts_stops_and_describes() -> None:
    assert "fake" in qa_runners.registered()
    runner = qa_runners.create("fake")
    launched = runner.launch(LANE)
    assert runner.status(launched.agent_id) == "running"
    runner.stop(launched.agent_id)
    assert runner.status(launched.agent_id) == "ended"
    runner.stop(launched.agent_id)
    assert runner.describe(launched.agent_id).machine


def test_stopping_an_ended_agent_is_not_an_error() -> None:
    runner = FakeRunner()
    agent = runner.launch(LANE).agent_id
    runner.end(agent)
    runner.stop(agent)
    assert runner.stopped == []


def test_fake_replays_a_lane_id_it_already_launched() -> None:
    runner = FakeRunner()
    first = runner.launch(LANE)

    assert runner.launch(LANE) == first
    assert runner.launched == [LANE]
    assert list(runner.agents) == [first.agent_id]


def test_the_three_errors_are_runner_errors() -> None:
    assert all(issubclass(error, LaunchError) for error in (RunnerOutage, UsageLimit, LaunchFailed))
    runner = FakeRunner()
    runner.launch_errors.append(UsageLimit("limit"))
    with pytest.raises(UsageLimit):
        runner.launch(LANE)
    assert runner.launched == []


def test_an_unknown_runner_name_is_refused() -> None:
    with pytest.raises(KeyError):
        qa_runners.create("nope")


def test_factory_md_cannot_select_fake() -> None:
    assert "fake" not in QA_RUNNERS
    text = (
        "---\nbase_branch: main\ntickets: github\nbuilder: claude\nreviewer: codex\nrounds: {low: 1, high: 3}\n"
        "verify: none\nship: handoff\nqa:\n  enabled: true\n  runners: [fake]\n  profile: full-stack\n"
        "  lane: {boot: make boot, base_url: 'http://localhost:3000'}\n---\n"
    )
    _, findings = parse_factory_md(text, strict=True)
    assert any("qa.runners[0] must be" in finding.message for finding in findings), findings
