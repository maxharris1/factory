"""#495: the poll hands the criteria every source it holds, and one brief."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.dispatcher import TrackedTicket
from lib.evidence import criteria_evidence
from lib.github_actions import GhResult
from lib.evidence.criteria import EMPTY
from lib.poll_source import LINEAR_ISSUE_QUERY, LivePollSource
from tests.test_dispatcher import BRIEF, comment
from tests.test_evidence_wiring import CIFlow, claim, evidence
from tests.test_live_poll import FACTORY, FakeGh, config, linear_issue, linear_transport_for
from tests.test_rounds_and_caps import HEADS

SECTION = "## Acceptance criteria\n- {name} first.\n- {name} second.\n"
ISSUE = re.compile(r"repos/acme/widgets/issues/(\d+)")


class IssueGh(FakeGh):
    def __init__(self) -> None:
        super().__init__(FACTORY.format(tickets="github", ship="merge"))
        self.issue_bodies: dict[str, str | None] = {}

    def __call__(self, argv, timeout):
        found = next((ISSUE.fullmatch(part) for part in argv if ISSUE.fullmatch(part)), None)
        if found is None:
            return super().__call__(argv, timeout)
        self.calls.append(list(argv))
        return GhResult(0, json.dumps({"body": self.issue_bodies[found.group(1)]}), "")

    def issue_reads(self) -> int:
        return sum(any(ISSUE.fullmatch(part) for part in call) for call in self.calls)


def brief_comment_json(number: int, text: str) -> dict:
    return {
        "id": number,
        "user": {"login": "grok-login"},
        "body": f"<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=high -->\n{text}",
        "created_at": f"2026-11-11T12:00:{number:02d}Z",
        "html_url": f"https://github.test/comment/{number}",
    }


def github_ticket(gh: IssueGh, source: LivePollSource | None = None) -> TrackedTicket:
    source = source or LivePollSource(config(), gh_runner=gh)
    project = config().projects[0]
    return source._github_ticket(project, source._factory(project), "12", fresh=True)


def bundle(ticket: TrackedTicket):
    return criteria_evidence(
        issue_key=ticket.source_issue or ticket.ticket_id,
        issue_url=ticket.ticket_url,
        brief=ticket.brief_comment,
        issue_body=ticket.issue_body,
        pr_body=ticket.pr_body,
        ready_starts_review=True,
        pr_url=ticket.ticket_url,
    )


def test_github_poll_keeps_the_pr_description_and_reads_the_source_issue_once_per_head() -> None:
    gh = IssueGh()
    gh.pull = {"body": "Fixes #495\n\n" + SECTION.format(name="PR"), "title": "#495: x"}
    gh.issue_bodies["495"] = SECTION.format(name="Issue")

    source = LivePollSource(config(), gh_runner=gh)
    ticket = github_ticket(gh, source)
    again = github_ticket(gh, source)

    assert ticket.pr_body == gh.pull["body"]
    assert ticket.issue_body == again.issue_body == SECTION.format(name="Issue")
    assert gh.issue_reads() == 1
    assert bundle(ticket).source == "issue"
    assert [item.text for item in bundle(ticket).criteria] == ["Issue first.", "Issue second."]


def test_github_poll_reads_no_issue_when_a_brief_exists() -> None:
    gh = IssueGh()
    gh.pull = {"body": "Fixes #495", "title": "#495: x"}
    gh.comments = [brief_comment_json(1, SECTION.format(name="Brief"))]

    ticket = github_ticket(gh)

    assert gh.issue_reads() == 0
    assert ticket.issue_body is None
    assert bundle(ticket).source == "brief"


def test_github_poll_with_no_source_issue_falls_back_to_the_pr_description() -> None:
    gh = IssueGh()
    gh.pull = {"body": SECTION.format(name="PR")}

    ticket = github_ticket(gh)

    assert gh.issue_reads() == 0
    assert bundle(ticket).source == "pr"
    assert [item.id for item in bundle(ticket).criteria] == ["ac-1", "ac-2"]


def test_two_briefs_give_the_builder_and_the_reviewer_the_same_one() -> None:
    gh = IssueGh()
    gh.comments = [
        brief_comment_json(1, SECTION.format(name="A")),
        brief_comment_json(2, SECTION.format(name="B")),
    ]

    ticket = github_ticket(gh)
    criteria = bundle(ticket)

    assert ticket.brief == "https://github.test/comment/2"
    assert criteria.source_url == ticket.brief
    assert [item.text for item in criteria.criteria] == ["B first.", "B second."]


def test_linear_poll_reads_the_issue_description_with_the_query_it_already_sends() -> None:
    assert re.search(r"^\s+description$", LINEAR_ISSUE_QUERY, re.MULTILINE)
    gh = IssueGh()
    gh.pull = {"body": "PR text"}
    issue = {**linear_issue(), "description": SECTION.format(name="Linear")}
    source = LivePollSource(config(linear=True), gh_runner=gh, linear_transport=linear_transport_for(issue))
    project = config(linear=True).projects[0]

    ticket = source._linear_ticket(project, issue)

    assert ticket.issue_body == issue["description"]
    assert ticket.pr_body == "PR text"
    assert gh.issue_reads() == 0
    assert [item.text for item in bundle(ticket).criteria] == ["Linear first.", "Linear second."]


class SourcesFlow(CIFlow):
    def __init__(self, tmp_path: Path, **bodies: str | None):
        super().__init__(tmp_path)
        self.bodies = bodies

    def snapshot(self) -> TrackedTicket:
        return replace(super().snapshot(), ready_starts_review=True, **self.bodies)


def reviewed(flow: SourcesFlow) -> dict:
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    assert flow.ready(HEADS[1]) == "queued-review"
    return evidence(claim(flow, "codex"))["criteria"]


@pytest.mark.parametrize(
    ("bodies", "source", "name"),
    [
        ({"issue_body": SECTION.format(name="Issue"), "pr_body": SECTION.format(name="PR")}, "issue", "Issue"),
        ({"issue_body": "  ", "pr_body": SECTION.format(name="PR")}, "pr", "PR"),
    ],
)
def test_review_bundle_takes_criteria_from_the_issue_or_the_pr(
    tmp_path: Path, bodies: dict, source: str, name: str
) -> None:
    criteria = reviewed(SourcesFlow(tmp_path, **bodies))
    assert criteria["source"] == source
    assert [(item["id"], item["text"]) for item in criteria["criteria"]] == [
        ("ac-1", f"{name} first."),
        ("ac-2", f"{name} second."),
    ]


def test_review_bundle_with_no_criteria_says_so_and_renders(tmp_path: Path) -> None:
    flow = SourcesFlow(tmp_path, issue_body="", pr_body=None)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    assert flow.ready(HEADS[1]) == "queued-review"
    job = claim(flow, "codex")
    criteria = evidence(job)["criteria"]

    assert (criteria["source"], criteria["criteria"], criteria["text"]) == ("none", [], EMPTY)
    text = render_brief(job, bot="codex-bot", context=None, factory_root="/srv/orch/factory")
    assert EMPTY in text


def test_brief_still_wins_in_the_review_bundle(tmp_path: Path) -> None:
    flow = SourcesFlow(tmp_path, issue_body=SECTION.format(name="Issue"))
    flow.comments.append(comment("brief-1", BRIEF, author="grok-login", url="u/brief-1"))
    assert reviewed(flow)["source"] == "brief"
