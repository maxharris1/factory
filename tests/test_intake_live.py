"""Live intake clients for the feeder (lib/intake_live.py, issue #85).

GitHub runs through the RecordedGh seam (a fake gh); Linear through a fake
of the poller's Linear request path. Nothing reaches a tracker.
"""

from __future__ import annotations

import json

from lib.github_actions import GhResult
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
        return {"data": self.data}


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
    assert source.documents[0]["variables"] == {"project": "proj-1", "state": "In Progress"}
    item = found[0]
    assert (item.repo, item.key, item.applied_by, item.urgent, item.bug, item.branch) == (
        "acme/app", "AUT-7", "max", True, True, "max/aut-7-t"
    )


def test_linear_comment_is_confirmed() -> None:
    source = FakeLinearSource({"commentCreate": {"success": True, "comment": {"id": "c-1"}}})
    client = LinearIntakeClient(source, {})
    assert client.post_linear_comment("uuid-7", "body") == "c-1"
    assert source.documents[0]["query"] == LINEAR_COMMENT_MUTATION
