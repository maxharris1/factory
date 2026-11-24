import json
from dataclasses import replace

import pytest

from lib.dispatch.types import StackedOn
from lib.github_actions import GhResult
from tests.test_github_actions import MARK_READY, PULL, draft_pull, served
from tests.test_ship_redrive import approve_snapshot, github_setup, grok_wakes, intent


@pytest.mark.usefixtures("every_wake_reaches_grok")
def test_retried_handoff_names_the_parent(tmp_path):
    dispatcher, queue, gh = github_setup(tmp_path)
    gh.route(PULL, GhResult(1, "", "gh: Server Error (HTTP 502)\n"), draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    ticket = replace(
        approve_snapshot(),
        pr_number="42",
        pr_base="main",
        stacked_on=StackedOn(10, "factory/issue-10", "main"),
    )
    dispatcher.process(ticket)
    dispatcher.process(ticket)
    done = [json.loads(d) for action, d in grok_wakes(queue) if action == "done"]
    assert intent(queue)["status"] == "completed"
    assert len(done) == 1
    assert done[0].get("note") == "Stacked on #10. Merge #10 first."
