"""Issue #195: a ticket's risk is read once at intake and never silently low."""

import json

import pytest

from lib.dispatcher import Comment, TicketRef
from lib.github_actions import GhResult
from lib.poll_source import LINEAR_ISSUE_QUERY, HTTPResult, LivePollSource, _risk
from tests.test_live_poll import FACTORY, FakeGh, config, linear_issue

TRUSTED = {
    "github": {"grok-login": frozenset({"grok-bot"}), "claude-login": frozenset({"claude-bot"})},
    "linear": {},
}
BRIEF_LOW = "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=low -->\n"
GO_LOW = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building risk=low -->\n"
GO_HIGH = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building risk=high -->\n"


def said(author: str, body: str, number: int, *, edited: bool = False) -> Comment:
    return Comment(
        id=str(number),
        author=author,
        body=body,
        created_at=number,
        edited_at=number + 0.5 if edited else None,
    )


def test_a_later_trusted_risk_high_does_not_move_a_stored_low() -> None:
    comments = (said("grok-login", BRIEF_LOW, 1), said("grok-login", GO_HIGH, 2))
    assert _risk(comments, TRUSTED) == "low"


def test_the_first_trusted_go_sets_the_risk_when_no_brief_carries_one() -> None:
    comments = (
        said("grok-login", "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->\n", 1),
        said("grok-login", GO_LOW, 2),
        said("grok-login", GO_HIGH, 3),
    )
    assert _risk(comments, TRUSTED) == "low"


@pytest.mark.parametrize(
    "body",
    [
        "<!-- orch:v1 kind=ready from=claude-bot role=builder head=abc1234 risk=low -->\n",
        "<!-- orch:v1 kind=go from=claude-bot role=orchestrator state=building risk=low -->\n",
        "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator risk=low -->\n",
    ],
)
def test_only_an_orchestrator_brief_or_go_sets_the_risk(body: str) -> None:
    login = "claude-login" if "claude-bot" in body else "grok-login"
    assert _risk((said(login, body, 1),), TRUSTED) == "high"


def test_an_untrusted_or_edited_risk_comment_is_not_the_stored_one() -> None:
    assert _risk((said("stranger", BRIEF_LOW, 1),), TRUSTED) == "high"
    assert _risk((said("grok-login", BRIEF_LOW, 1, edited=True),), TRUSTED) == "high"
    assert _risk(
        (said("stranger", GO_HIGH, 1), said("grok-login", GO_LOW, 2), said("grok-login", GO_HIGH, 3)),
        TRUSTED,
    ) == "low"


def test_unknown_risk_is_high() -> None:
    assert _risk((), TRUSTED) == "high"
    assert _risk((said("grok-login", "plain words risk=low", 1),), TRUSTED) == "high"


def test_github_ticket_keeps_its_intake_risk() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.comments = [
        {
            "id": number,
            "user": {"login": "grok-login"},
            "body": body,
            "created_at": f"2026-11-11T12:00:{number:02d}Z",
            "html_url": f"https://github.test/comment/{number}",
        }
        for number, body in enumerate((BRIEF_LOW, GO_HIGH), start=1)
    ]
    source = LivePollSource(config(), gh_runner=gh)

    (ticket,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))

    assert ticket.risk == "low"


class FilesGh(FakeGh):
    def __init__(self, files: list[str] | None) -> None:
        super().__init__(FACTORY.format(tickets="linear", ship="handoff"))
        self.files = files

    def __call__(self, argv, timeout):
        endpoint = next(part for part in argv if part.startswith(("repos/", "search/")))
        if "/files?" in endpoint:
            self.calls.append(list(argv))
            if self.files is None:
                return GhResult(1, "", "failed (HTTP 502)")
            return GhResult(0, json.dumps([{"filename": path} for path in self.files]), "")
        return super().__call__(argv, timeout)


def linear_ticket(
    *,
    labels: list[str] | None = None,
    files: list[str] | None = (),
    linked: bool = True,
    go: str = GO_LOW,
    errors: list | None = None,
):
    issue = linear_issue()
    issue["comments"]["nodes"][0]["body"] = go
    if labels is not None:
        issue["labels"] = {
            "nodes": [{"name": name} for name in labels],
            "pageInfo": {"hasNextPage": False},
        }
    if not linked:
        issue["attachments"]["nodes"] = []
    gh = FilesGh(None if files is None else list(files))

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        return HTTPResult(200, json.dumps({"data": {"issue": issue}}))

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=transport,
        error_sink=None if errors is None else lambda *item: errors.append(item),
    )
    (ticket,) = source.tracked_tickets((TicketRef("acme/widgets", "AUT-42", "linear"),))
    return ticket, gh


def test_linear_issue_query_reads_the_issue_labels() -> None:
    assert "labels(first: 50) { nodes { name } pageInfo { hasNextPage } }" in LINEAR_ISSUE_QUERY


def test_a_linear_low_ticket_with_no_risk_signal_stays_low() -> None:
    ticket, _ = linear_ticket(labels=["bug"], files=["src/app.ts"])
    assert ticket.risk == "low"


def test_a_linear_migrations_ticket_is_high_without_a_risk_high_header() -> None:
    ticket, _ = linear_ticket(labels=["Migrations"], files=["src/app.ts"])
    assert ticket.risk == "high"


@pytest.mark.parametrize(
    "path",
    [
        "supabase/migrations/20261114_add_table.sql",
        "db/migration_0042.py",
        "supabase/realtime/tables.sql",
    ],
)
def test_a_linear_migration_or_realtime_change_is_high(path: str) -> None:
    ticket, _ = linear_ticket(labels=[], files=["src/app.ts", path])
    assert ticket.risk == "high"


def test_a_linear_ticket_whose_labels_are_unread_is_high() -> None:
    ticket, _ = linear_ticket(labels=None, files=[])
    assert ticket.risk == "high"


def test_a_linear_ticket_whose_changed_files_are_unread_is_high() -> None:
    errors: list = []
    ticket, _ = linear_ticket(labels=[], files=None, errors=errors)
    assert ticket.risk == "high"
    assert [(repo, ticket_id) for repo, ticket_id, _ in errors] == [("acme/widgets", "AUT-42")]


def test_a_linear_ticket_with_no_pr_yet_is_judged_on_its_labels() -> None:
    assert linear_ticket(labels=[], linked=False)[0].risk == "low"
    assert linear_ticket(labels=["migrations"], linked=False)[0].risk == "high"


def test_a_linear_high_ticket_reads_no_changed_files() -> None:
    ticket, gh = linear_ticket(labels=[], files=["src/app.ts"], go=GO_HIGH)
    assert ticket.risk == "high"
    assert not any("/files?" in part for call in gh.calls for part in call)
