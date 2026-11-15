import json
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, TicketRef
from lib.queue import JobRequest
from lib.poll_source import IntakeRisks, LivePollSource
from tests.test_dispatcher import comment, factory, setup, ticket
from tests.test_live_poll import config
from tests.test_risk_once import BRIEF_LOW, github_gh

HEAD = "a" * 40
READY_HIGH = (
    "<!-- orch:v1 kind=ready from=claude-bot role=builder "
    f"round=1/3 head={HEAD} risk=high -->\nReady.\n"
)


def review_round(tmp_path: Path, risk: str, repo: str = "owner/repo", ticket_id: str = "42") -> str:
    dispatcher, queue = setup(tmp_path)
    queue.enqueue(
        JobRequest(
            repo=repo,
            ticket_id=ticket_id,
            action="build",
            recipient="claude",
            source_comment_id="go",
            timeout_seconds=5400,
        ),
        dedupe_key="build",
    )
    build = queue.claim("claude")
    assert build is not None
    queue.finish(build.id, owner="claude", handoff_comment_id="ready")
    dispatcher.process(
        ticket(
            comment("ready", READY_HIGH, author="claude-login"),
            head=HEAD,
            ci={HEAD: CIResult("success")},
            factory_config=factory(rounds_low=1, rounds_high=3),
            risk=risk,
            repo=repo,
            ticket_id=ticket_id,
        )
    )
    review = queue.claim("codex")
    assert review is not None and review.request.action == "review"
    return json.loads(review.request.detail)["round"]


def test_a_builder_ready_with_risk_high_on_a_low_ticket_leaves_the_cap_at_1(
    tmp_path: Path,
) -> None:
    source = LivePollSource(
        config(),
        gh_runner=github_gh(BRIEF_LOW, READY_HIGH),
        risks=IntakeRisks(tmp_path / "d.db"),
    )
    (polled,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))
    assert polled.risk == "low"
    assert review_round(tmp_path, polled.risk, polled.repo, polled.ticket_id) == "1/1"


@pytest.mark.parametrize(
    ("stored", "risk", "expected"),
    [
        (None, "low", "1/3"),
        ("high", "low", "1/3"),
        ("low", "high", "1/3"),
        ("low", "low", "1/1"),
    ],
)
def test_only_a_stored_low_gives_the_low_cap(
    tmp_path: Path, stored: str | None, risk: str, expected: str
) -> None:
    if stored is not None:
        IntakeRisks(tmp_path / "d.db").keep("owner/repo", "42", stored)
    assert review_round(tmp_path, risk) == expected
