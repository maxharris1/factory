"""Live GitHub and Linear clients for the backlog feeder (lib/feeder.py, #85).

GitHub goes through the same `env -u GH_TOKEN ... gh api` seam as every other
GitHub call (lib/github_actions.py run_gh), with the token variables unset;
the client never reads, passes or logs a token. Linear goes through the
poller's own Linear request path (LivePollSource._linear_request), so the
feeder's Linear reads and comments share the poll's request and complexity
budget and its rate-limit backoff, and the API key is only read from the
configured environment variable at request time.

Reads:
* github_intake: open issues carrying the label (`issues?labels=`), and for
  each, who applied the label: the actor of the latest `labeled` event for
  that label (`issues/<n>/events`). That lookup is cached on the issue's
  updated_at, which GitHub bumps on every label change;
* linear_intake: issues in the project and workflow state, and for each, who
  moved it there: the actor of the latest history entry whose toState is
  that state.

Writes (each one exactly once per call; the feeder makes them idempotent):
the empty start commit (git data API: the base commit's tree, one parent),
the branch ref, the draft PR, the PR labels, PR comments, Linear comments.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

from lib.dispatcher import Comment
from lib.feeder import IntakeIssue
from lib.github_actions import (
    GitHubAdapterError,
    GitHubReviewActions,
    _repo_path,
    _status,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
LINEAR_INTAKE_QUERY = (
    "query($project: ID!, $state: String!) { issues(first: 50, filter: "
    "{ project: { id: { eq: $project } }, state: { name: { eq: $state } } }) "
    "{ nodes { id identifier title description url gitBranchName priority createdAt "
    "labels(first: 20) { nodes { name } } "
    "history(first: 25) { nodes { createdAt actor { id } toState { name } } } } } }"
)
LINEAR_COMMENTS_QUERY = (
    "query($issue: String!) { issue(id: $issue) { comments(first: 100) "
    "{ nodes { id body createdAt user { id } } } } }"
)
LINEAR_COMMENT_MUTATION = (
    "mutation($issue: String!, $body: String!) { commentCreate(input: "
    "{ issueId: $issue, body: $body }) { success comment { id } } }"
)


def _epoch(value: object) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


class GitHubIntakeClient(GitHubReviewActions):
    """The feeder's GitHub reads and writes, on the adapter's gh seam."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._applied: dict[tuple[str, int, str, str], str | None] = {}

    def github_intake(self, repo: str, label: str) -> list[IntakeIssue]:
        path = _repo_path(repo)
        found: list[IntakeIssue] = []
        endpoint = f"{path}/issues?state=open&labels={quote(label, safe='')}&per_page=100"
        for page in self._pages(endpoint):
            if not isinstance(page, list):
                raise GitHubAdapterError("issue page is not a list")
            for item in page:
                if not isinstance(item, dict) or "pull_request" in item:
                    continue
                number = item.get("number")
                if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                    continue
                labels = tuple(
                    str(entry.get("name"))
                    for entry in item.get("labels") or []
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                )
                if label not in labels:
                    continue
                found.append(
                    IntakeIssue(
                        repo=repo,
                        tracker="github",
                        key=str(number),
                        title=_text(item.get("title")),
                        body=_text(item.get("body")),
                        labels=labels,
                        created_at=_epoch(item.get("created_at")),
                        applied_by=self._applied_by(
                            repo, number, label, _text(item.get("updated_at"))
                        ),
                        url=_text(item.get("html_url")) or None,
                    )
                )
        return found

    def _applied_by(self, repo: str, number: int, label: str, updated_at: str) -> str | None:
        key = (repo.lower(), number, label, updated_at)
        if key in self._applied:
            return self._applied[key]
        actor: str | None = None
        for page in self._pages(f"{_repo_path(repo)}/issues/{number}/events?per_page=100"):
            if not isinstance(page, list):
                raise GitHubAdapterError("issue events page is not a list")
            for event in page:
                if (
                    isinstance(event, dict)
                    and event.get("event") == "labeled"
                    and isinstance(event.get("label"), dict)
                    and event["label"].get("name") == label
                ):
                    login = (event.get("actor") or {}).get("login")
                    actor = login if isinstance(login, str) else None
        self._applied[key] = actor
        return actor

    def branch_sha(self, repo: str, branch: str) -> str | None:
        if not _BRANCH.fullmatch(branch):
            raise GitHubAdapterError("branch name is not safe")
        try:
            ref = self._json(f"{_repo_path(repo)}/git/ref/heads/{branch}")
        except GitHubAdapterError as exc:
            if _status(exc) == 404:
                return None
            raise
        sha = ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise GitHubAdapterError(f"ref heads/{branch} has no commit sha")
        return sha

    def create_start_commit(self, repo: str, base_sha: str, message: str) -> str:
        if not _SHA.fullmatch(base_sha):
            raise GitHubAdapterError("base sha is not a full sha")
        base = self._json(f"{_repo_path(repo)}/git/commits/{base_sha}")
        tree = ((base or {}).get("tree") or {}).get("sha") if isinstance(base, dict) else None
        if not isinstance(tree, str) or not _SHA.fullmatch(tree):
            raise GitHubAdapterError("base commit has no tree sha")
        made = self._json(
            "-X", "POST", f"{_repo_path(repo)}/git/commits",
            "-f", f"message={message}",
            "-f", f"tree={tree}",
            "-f", f"parents[]={base_sha}",
        )
        sha = made.get("sha") if isinstance(made, dict) else None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise GitHubAdapterError("start commit was not confirmed")
        return sha

    def create_branch(self, repo: str, branch: str, sha: str) -> None:
        if not _BRANCH.fullmatch(branch) or not _SHA.fullmatch(sha):
            raise GitHubAdapterError("branch or sha is not safe")
        self._json(
            "-X", "POST", f"{_repo_path(repo)}/git/refs",
            "-f", f"ref=refs/heads/{branch}",
            "-f", f"sha={sha}",
        )

    def find_pull(self, repo: str, branch: str) -> int | None:
        owner = repo.split("/", 1)[0]
        endpoint = (
            f"{_repo_path(repo)}/pulls?state=all&per_page=100"
            f"&head={quote(owner, safe='')}:{quote(branch, safe='/')}"
        )
        pulls = self._json(endpoint)
        if not isinstance(pulls, list):
            raise GitHubAdapterError("pulls list is not a list")
        numbers = sorted(
            item["number"]
            for item in pulls
            if isinstance(item, dict)
            and isinstance(item.get("number"), int)
            and ((item.get("head") or {}).get("ref") == branch)
        )
        return numbers[0] if numbers else None

    def open_draft_pull(self, repo: str, *, branch: str, base: str, title: str, body: str) -> int:
        made = self._json(
            "-X", "POST", f"{_repo_path(repo)}/pulls",
            "-f", f"title={title}",
            "-f", f"head={branch}",
            "-f", f"base={base}",
            "-f", f"body={body}",
            "-F", "draft=true",
        )
        number = made.get("number") if isinstance(made, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise GitHubAdapterError("draft pull request was not confirmed")
        return number

    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> None:
        args: list[str] = ["-X", "POST", f"{_repo_path(repo)}/issues/{int(number)}/labels"]
        for label in labels:
            args += ["-f", f"labels[]={label}"]
        self._call(*args)

    def comments(self, repo: str, number: int) -> list[Comment]:
        return list(self.list_comments(repo, str(int(number))))

    def post_comment(self, repo: str, number: int, body: str) -> str:
        made = self._json(
            "-X", "POST", f"{_repo_path(repo)}/issues/{int(number)}/comments",
            "-f", f"body={body}",
        )
        comment_id = made.get("id") if isinstance(made, dict) else None
        if not isinstance(comment_id, int) or isinstance(comment_id, bool):
            raise GitHubAdapterError("comment was not confirmed")
        return str(comment_id)


class LinearIntakeClient:
    """The feeder's Linear reads and comments, on the poller's Linear path."""

    def __init__(self, source: Any, repo_for_project: dict[str, str]):
        self.source = source
        self.repo_for_project = repo_for_project

    def _request(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        document = self.source._linear_request({"query": query, "variables": variables})
        data = document.get("data")
        if not isinstance(data, dict):
            raise GitHubAdapterError("Linear returned no data")
        return data

    def linear_intake(self, project_id: str, state: str) -> list[IntakeIssue]:
        repo = self.repo_for_project[project_id]
        data = self._request(LINEAR_INTAKE_QUERY, {"project": project_id, "state": state})
        nodes = ((data.get("issues") or {}).get("nodes")) or []
        found: list[IntakeIssue] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            identifier = node.get("identifier")
            issue_id = node.get("id")
            if not isinstance(identifier, str) or not isinstance(issue_id, str):
                continue
            moves = [
                entry
                for entry in ((node.get("history") or {}).get("nodes") or [])
                if isinstance(entry, dict)
                and ((entry.get("toState") or {}).get("name")) == state
            ]
            moves.sort(key=lambda entry: _text(entry.get("createdAt")))
            actor = ((moves[-1].get("actor") or {}).get("id")) if moves else None
            priority = node.get("priority")
            found.append(
                IntakeIssue(
                    repo=repo,
                    tracker="linear",
                    key=identifier,
                    title=_text(node.get("title")),
                    body=_text(node.get("description")),
                    labels=tuple(
                        str(entry.get("name"))
                        for entry in ((node.get("labels") or {}).get("nodes") or [])
                        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                    ),
                    created_at=_epoch(node.get("createdAt")),
                    applied_by=actor if isinstance(actor, str) else None,
                    url=_text(node.get("url")) or None,
                    linear_id=issue_id,
                    branch=_text(node.get("gitBranchName")) or None,
                    priority=priority if isinstance(priority, int) else None,
                )
            )
        return found

    def linear_comments(self, issue_id: str) -> list[Comment]:
        data = self._request(LINEAR_COMMENTS_QUERY, {"issue": issue_id})
        nodes = (((data.get("issue") or {}).get("comments") or {}).get("nodes")) or []
        return [
            Comment(
                id=str(node.get("id")),
                author=_text((node.get("user") or {}).get("id")),
                body=_text(node.get("body")),
                created_at=_epoch(node.get("createdAt")),
            )
            for node in nodes
            if isinstance(node, dict) and isinstance(node.get("id"), str)
        ]

    def post_linear_comment(self, issue_id: str, body: str) -> str:
        data = self._request(LINEAR_COMMENT_MUTATION, {"issue": issue_id, "body": body})
        made = data.get("commentCreate") or {}
        comment = made.get("comment") or {}
        if made.get("success") is not True or not isinstance(comment.get("id"), str):
            raise GitHubAdapterError("Linear comment was not confirmed")
        return str(comment["id"])
