"""Live intake clients for the feeder (lib/intake_live.py, issue #85).

GitHub runs through the RecordedGh seam (a fake gh); Linear through a fake
of the poller's Linear request path. Nothing reaches a tracker.
"""

from __future__ import annotations

import json
import re

import pytest

from lib.github_actions import GhResult, GitHubAdapterError
from lib.intake_live import (
    LINEAR_COMMENT_MUTATION,
    LINEAR_INTAKE_QUERY,
    GitHubIntakeClient,
    LinearIntakeClient,
)
from tests.test_github_actions import RecordedGh

REPO = "repos/owner/app"
ISSUES = ("--paginate", f"{REPO}/issues?state=open&labels=factory%3Aready&per_page=100")


def ok(value: object) -> GhResult:
    return GhResult(0, json.dumps(value), "")


def events(number: int) -> tuple[str, ...]:
    return ("--paginate", f"{REPO}/issues/{number}/events?per_page=100")


def test_github_intake_reads_labelled_issues_and_who_applied_the_label() -> None:
    gh = RecordedGh()
    gh.route(ISSUES, ok([
        {"number": 85, "title": "Feeder", "body": "Do it", "created_at": "2026-11-12T20:00:00Z",
         "updated_at": "2026-11-12T20:05:00Z", "html_url": "https://github.com/owner/app/issues/85",
         "labels": [{"name": "factory:ready"}, {"name": "bug"}]},
        {"number": 86, "pull_request": {}, "labels": [{"name": "factory:ready"}]},
    ]))
    gh.route(events(85), ok([
        {"event": "labeled", "label": {"name": "factory:ready"}, "actor": {"login": "drive-by"}},
        {"event": "unlabeled", "label": {"name": "factory:ready"}, "actor": {"login": "maxharris1"}},
        {"event": "labeled", "label": {"name": "factory:ready"}, "actor": {"login": "maxharris1"}},
        {"event": "labeled", "label": {"name": "bug"}, "actor": {"login": "someone"}},
    ]))
    client = GitHubIntakeClient(gh)

    found = client.github_intake("owner/app", "factory:ready")

    assert [(item.key, item.applied_by, item.bug) for item in found] == [("85", "maxharris1", True)]
    assert found[0].created_at > 0
    client.github_intake("owner/app", "factory:ready")
    assert gh.count(events(85)) == 1  # cached until the issue's updated_at changes


def test_branch_sha_is_none_for_a_missing_branch() -> None:
    gh = RecordedGh()
    gh.route((f"{REPO}/git/ref/heads/factory/issue-1",), GhResult(1, "{}", "gh: Not Found (HTTP 404)\n"))
    assert GitHubIntakeClient(gh).branch_sha("owner/app", "factory/issue-1") is None


def test_start_commit_reuses_the_base_tree_with_one_parent() -> None:
    gh = RecordedGh()
    base, tree, made = "a" * 40, "b" * 40, "c" * 40
    gh.route((f"{REPO}/git/commits/{base}",), ok({"sha": base, "tree": {"sha": tree}}))
    gh.route(("-X", "POST", f"{REPO}/git/commits", "-f", "message=Start #1: x",
              "-f", f"tree={tree}", "-f", f"parents[]={base}"), ok({"sha": made}))
    assert GitHubIntakeClient(gh).create_start_commit("owner/app", base, "Start #1: x") == made


def test_open_draft_pull_sends_draft_true() -> None:
    gh = RecordedGh()
    key = ("-X", "POST", f"{REPO}/pulls", "-f", "title=T", "-f", "head=factory/issue-1",
           "-f", "base=main", "-f", "body=Fixes #1", "-F", "draft=true")
    gh.route(key, ok({"number": 501}))
    assert GitHubIntakeClient(gh).open_draft_pull(
        "owner/app", branch="factory/issue-1", base="main", title="T", body="Fixes #1"
    ) == 501


class FakeLinearSource:
    def __init__(self, data: dict):
        self.data = data
        self.documents: list[dict] = []

    def _linear_request(self, document: dict) -> dict:
        self.documents.append(document)
        _check_issue_fields(document["query"])
        return {"data": self.data}


# Linear's Issue type has `branchName`; `gitBranchName` is only ever an
# alias (poll_source.py LINEAR_ISSUE_QUERY). A query that selects a field
# Issue does not have gets a GraphQL error, which LivePollSource raises.
LINEAR_ISSUE_FIELDS = frozenset({
    "id", "identifier", "title", "description", "url", "branchName", "priority",
    "createdAt", "labels", "history", "project", "state", "team", "comments",
    "attachments",
})


def _check_issue_fields(query: str) -> None:
    match = re.search(r"\bnodes \{ (.*?) labels\(", query)
    if match is None:
        return
    for selection in re.findall(r"(?:(\w+): )?(\w+)", match.group(1)):
        field = selection[1]
        if field not in LINEAR_ISSUE_FIELDS:
            raise GitHubAdapterError(
                f'Linear returned a GraphQL error: Cannot query field "{field}" on type "Issue"'
            )


class PagedLinearSource:
    """Linear's issues connection, served one page per cursor."""

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.documents: list[dict] = []

    def _linear_request(self, document: dict) -> dict:
        self.documents.append(document)
        _check_issue_fields(document["query"])
        after = document["variables"].get("after")
        index = 0 if after is None else int(after.removeprefix("cursor-"))
        last = index + 1 >= len(self.pages)
        return {"data": {"issues": {
            "nodes": self.pages[index],
            "pageInfo": {"hasNextPage": not last,
                         "endCursor": None if last else f"cursor-{index + 1}"},
        }}}


def linear_node(number: int, *, priority: int = 3) -> dict:
    return {
        "id": f"uuid-{number}", "identifier": f"AUT-{number}", "title": f"T{number}",
        "description": "", "url": f"https://linear.app/x/issue/aut-{number}",
        "gitBranchName": f"max/aut-{number}", "priority": priority,
        "createdAt": "2026-11-12T20:00:00Z", "labels": {"nodes": []},
        "history": {"nodes": [
            {"createdAt": "2026-11-12T21:00:00Z", "actor": {"id": "max"},
             "toState": {"name": "In Progress"}},
        ]},
    }


def test_linear_intake_reads_who_moved_the_issue_to_the_state() -> None:
    source = FakeLinearSource({"issues": {"nodes": [{
        "id": "uuid-7", "identifier": "AUT-7", "title": "T", "description": "D",
        "url": "https://linear.app/x/issue/aut-7", "gitBranchName": "max/aut-7-t",
        "priority": 1, "createdAt": "2026-11-12T20:00:00Z",
        "labels": {"nodes": [{"name": "Bug"}]},
        "history": {"nodes": [
            {"createdAt": "2026-11-12T21:00:00Z", "actor": {"id": "max"}, "toState": {"name": "In Progress"}},
            {"createdAt": "2026-11-12T20:30:00Z", "actor": {"id": "other"}, "toState": {"name": "In Progress"}},
            {"createdAt": "2026-11-12T22:00:00Z", "actor": {"id": "bot"}, "toState": {"name": "Todo"}},
        ]},
    }]}})
    client = LinearIntakeClient(source, {"proj-1": "acme/app"})

    found = client.linear_intake("proj-1", "In Progress")

    assert source.documents[0]["query"] == LINEAR_INTAKE_QUERY
    assert source.documents[0]["variables"] == {
        "project": "proj-1", "state": "In Progress", "after": None,
    }
    item = found[0]
    assert (item.repo, item.key, item.applied_by, item.urgent, item.bug, item.branch) == (
        "acme/app", "AUT-7", "max", True, True, "max/aut-7-t"
    )


def test_linear_comment_is_confirmed() -> None:
    source = FakeLinearSource({"commentCreate": {"success": True, "comment": {"id": "c-1"}}})
    client = LinearIntakeClient(source, {})
    assert client.post_linear_comment("uuid-7", "body") == "c-1"
    assert source.documents[0]["query"] == LINEAR_COMMENT_MUTATION


# --- #87 review F1.2, F1.6, F1.7 ----------------------------------------------


def test_the_linear_intake_query_aliases_branch_name_as_the_poller_does() -> None:
    assert "gitBranchName: branchName" in LINEAR_INTAKE_QUERY
    assert re.search(r"\bgitBranchName\b(?!: branchName)", LINEAR_INTAKE_QUERY) is None


def test_the_linear_intake_reads_the_branch_from_the_aliased_field() -> None:
    source = PagedLinearSource([[linear_node(7)]])
    found = LinearIntakeClient(source, {"proj-1": "acme/app"}).linear_intake("proj-1", "In Progress")
    assert [(item.key, item.branch, item.applied_by) for item in found] == [
        ("AUT-7", "max/aut-7", "max")
    ]


def test_the_linear_intake_reads_every_page() -> None:
    first = [linear_node(number) for number in range(1, 51)]
    second = [linear_node(51, priority=1)]  # urgent, past the first page
    source = PagedLinearSource([first, second])
    found = LinearIntakeClient(source, {"proj-1": "acme/app"}).linear_intake("proj-1", "In Progress")
    assert len(found) == 51
    assert [doc["variables"]["after"] for doc in source.documents] == [None, "cursor-1"]
    assert "pageInfo { hasNextPage endCursor }" in source.documents[0]["query"]
    assert [item.key for item in found if item.urgent] == ["AUT-51"]


def test_a_next_page_without_a_cursor_fails_the_read() -> None:
    class NoCursor(PagedLinearSource):
        def _linear_request(self, document: dict) -> dict:
            self.documents.append(document)
            return {"data": {"issues": {"nodes": [linear_node(1)],
                                        "pageInfo": {"hasNextPage": True, "endCursor": None}}}}

    with pytest.raises(GitHubAdapterError, match="endCursor"):
        LinearIntakeClient(NoCursor([]), {"proj-1": "acme/app"}).linear_intake("proj-1", "In Progress")


def pull(number: int, state: str, branch: str = "factory/issue-85", merged: bool = False) -> dict:
    return {"number": number, "state": state, "merged_at": "2026-11-01T00:00:00Z" if merged else None,
            "head": {"ref": branch}}


FIND = (f"{REPO}/pulls?state=all&per_page=100&head=owner:factory/issue-85",)


def test_find_pull_prefers_the_open_pr_over_an_older_merged_one() -> None:
    gh = RecordedGh()
    gh.route(FIND, ok([pull(90, "open"), pull(12, "closed", merged=True)]))
    assert GitHubIntakeClient(gh).find_pull("owner/app", "factory/issue-85") == 90


def test_find_pull_ignores_a_branch_whose_only_pr_is_closed_or_merged() -> None:
    gh = RecordedGh()
    gh.route(FIND, ok([pull(12, "closed", merged=True), pull(14, "closed")]))
    assert GitHubIntakeClient(gh).find_pull("owner/app", "factory/issue-85") is None


def test_find_pull_takes_the_oldest_of_two_open_prs() -> None:
    gh = RecordedGh()
    gh.route(FIND, ok([pull(95, "open"), pull(90, "open"), pull(3, "closed")]))
    assert GitHubIntakeClient(gh).find_pull("owner/app", "factory/issue-85") == 90
