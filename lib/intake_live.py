"""Live GitHub and Linear clients for the backlog feeder (lib/feeder.py, #85).

GitHub goes through the same `env -u GH_TOKEN ... gh api` seam as every other
GitHub call (lib/github_actions.py run_gh), with the token variables unset;
the client never reads, passes or logs a token. Linear goes through the
poller's own Linear request path (LivePollSource._linear_request), so the
feeder's Linear reads and comments share the poll's request and complexity
budget and its rate-limit backoff, and the API key is only read from the
configured environment variable at request time.

Reads:
* ahead_commits and commit_pulls: a branch's commits past the base, and the
  open PRs that carry a commit, for the stacked-branch hold (#205);
* github_intake: open issues carrying the label (`issues?labels=`), and for
  each, who applied the label: the actor of the latest `labeled` event for
  that label (`issues/<n>/events`). That lookup is cached on the issue's
  updated_at, which GitHub bumps on every label change;
* linear_intake: issues in any of a repo's projects and in the workflow
  state, every page of them, and for each, who moved it there: the actor of the latest history
  entry whose toState is that state.

Writes (each one exactly once per call; the feeder makes them idempotent):
the empty start commit (git data API: the base commit's tree, one parent),
the branch ref, the draft PR, the PR labels, PR comments, Linear comments.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

from lib.dispatcher import Comment
from lib.feeder import IntakeIssue
from lib.feeder_gates import Progress, Ref, RefStatus
from lib.github_actions import (
    GitHubAdapterError,
    GitHubReviewActions,
    _repo_path,
    _status,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
# Every page of an issue's blocks relations is read (#157 review): a blocker
# past the first page still holds the ticket. Past the cap the read fails.
# The intake query reads the first 10 (it pages 50 issues at once); a further
# page is one request for that issue alone.
LINEAR_INTAKE_RELATIONS = 10
LINEAR_RELATIONS_PAGE = 50
LINEAR_RELATIONS_MAX_PAGES = 20
_RELATION_FIELDS = (
    "{ nodes { type issue { identifier state { type } } } pageInfo { hasNextPage endCursor } }"
)
LINEAR_RELATIONS_QUERY = (
    "query($issue: String!, $after: String) { issue(id: $issue) { "
    f"inverseRelations(first: {LINEAR_RELATIONS_PAGE}, after: $after) {_RELATION_FIELDS} }} }}"
)
# Linear's Issue field is branchName; it is read under the alias the poller
# uses (poll_source.py LINEAR_ISSUE_QUERY), so the node key is gitBranchName.
LINEAR_INTAKE_QUERY = (
    "query($projects: [ID!]!, $state: String!, $after: String) { issues(first: 50, after: $after, "
    "filter: { project: { id: { in: $projects } }, state: { name: { eq: $state } } }) "
    "{ nodes { id identifier title description url gitBranchName: branchName priority createdAt "
    "labels(first: 20) { nodes { name } } "
    "history(first: 25) { nodes { createdAt actor { id } toState { name } } } "
    f"inverseRelations(first: {LINEAR_INTAKE_RELATIONS}) {_RELATION_FIELDS} }} "
    "pageInfo { hasNextPage endCursor } } }"
)
# One aliased read per blocker repo per poll (#156), cached like the poller's
# reads, so a long chain costs one GraphQL call per repo, not one per ticket.
REF_STATUS_CACHE = "60s"
REF_STATUS_CHUNK = 50
# An issue's linked PRs are paged until one is merged; past the cap the read fails.
LINKED_PULLS_PAGE = 100
LINKED_PULLS_MAX_PAGES = 20
_LINKED_PULLS = (
    f"closedByPullRequestsReferences(first: {LINKED_PULLS_PAGE}, includeClosedPrs: true"
    "{after}) { nodes { merged } pageInfo { hasNextPage endCursor } }"
)
REF_STATUS_FIELDS = (
    "__typename "
    "... on PullRequest { state merged } "
    "... on Issue { state stateReason " + _LINKED_PULLS.replace("{after}", "") + " }"
)
LINKED_PULLS_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $after: String!) "
    "{ repository(owner: $owner, name: $name) { issue(number: $number) { "
    + _LINKED_PULLS.replace("{after}", ", after: $after") + " } } }"
)
_ISSUE_UNFINISHED = {"NOT_PLANNED": "closed as not planned", "DUPLICATE": "closed as duplicate"}
# 40 pages of 50: past that the read fails, rather than feed from a partial
# list that could hide an urgent ticket.
LINEAR_INTAKE_MAX_PAGES = 40
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

    def ref_statuses(self, refs: Collection[Ref]) -> dict[Ref, RefStatus]:
        """Whether each GitHub issue or PR is done: one cached GraphQL read
        per repo (chunks of 50). A number that does not exist is MISSING;
        any other error fails the read."""
        by_repo: dict[str, list[Ref]] = {}
        for ref in refs:
            if ref.tracker == "github" and ref.key.isdigit():
                by_repo.setdefault(ref.repo, []).append(ref)
        found: dict[Ref, RefStatus] = {}
        for repo, items in sorted(by_repo.items()):
            items.sort(key=Ref.sort_key)
            for start in range(0, len(items), REF_STATUS_CHUNK):
                found.update(self._ref_chunk(repo, items[start:start + REF_STATUS_CHUNK]))
        return found

    def _ref_chunk(self, repo: str, refs: list[Ref]) -> dict[Ref, RefStatus]:
        owner, _, name = _repo_path(repo).removeprefix("repos/").partition("/")
        fields = " ".join(
            f"r{index}: issueOrPullRequest(number: {int(ref.key)}) {{ {REF_STATUS_FIELDS} }}"
            for index, ref in enumerate(refs)
        )
        query = (
            "query($owner: String!, $name: String!) "
            f"{{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
        )
        document = self._json(
            "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}",
            cache_ttl=REF_STATUS_CACHE, partial_graphql=True,
        )
        if not isinstance(document, dict) or not isinstance(document.get("data"), dict):
            raise GitHubAdapterError("ref status read returned no data")
        missing_repo = False
        for error in document.get("errors") or []:
            path = error.get("path") if isinstance(error, dict) else None
            if not isinstance(error, dict) or error.get("type") != "NOT_FOUND" or not path:
                raise GitHubAdapterError("ref status read failed")
            if path == ["repository"]:
                missing_repo = True
        nodes = document["data"].get("repository")
        if nodes is None and missing_repo:
            return {ref: RefStatus(Progress.MISSING) for ref in refs}
        if not isinstance(nodes, dict):
            raise GitHubAdapterError("ref status read has no repository")
        found: dict[Ref, RefStatus] = {}
        for index, ref in enumerate(refs):
            node = nodes.get(f"r{index}")
            merged, cursor = _linked_page(node)
            if cursor is not None:
                merged = self._merged_after(owner, name, int(ref.key), cursor)
            found[ref] = _ref_status(node, merged)
        return found

    def _merged_after(self, owner: str, name: str, number: int, cursor: str) -> bool:
        """Whether a linked PR past cursor is merged, reading the remaining pages."""
        for _ in range(LINKED_PULLS_MAX_PAGES):
            document = self._json(
                "graphql", "-f", f"query={LINKED_PULLS_QUERY}", "-f", f"owner={owner}",
                "-f", f"name={name}", "-F", f"number={number}", "-f", f"after={cursor}",
                cache_ttl=REF_STATUS_CACHE,
            )
            data = document.get("data") if isinstance(document, dict) else None
            repository = data.get("repository") if isinstance(data, dict) else None
            issue = repository.get("issue") if isinstance(repository, dict) else None
            if not isinstance(issue, dict) or not isinstance(
                issue.get("closedByPullRequestsReferences"), dict
            ):
                raise GitHubAdapterError("linked pull request read returned no issue")
            merged, following = _linked_page(issue)
            if merged or following is None:
                return merged
            cursor = following
        raise GitHubAdapterError("linked pull requests exceed the page cap")

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
        """The open PR on branch (the oldest, if several), or None.

        A closed or merged PR on the same branch is never reused (#87 F1.6):
        the poller does not discover it, so its brief and go would go
        nowhere.
        """
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
            and not isinstance(item.get("number"), bool)
            and item.get("state") == "open"
            and item.get("merged_at") is None
            and ((item.get("head") or {}).get("ref") == branch)
        )
        return numbers[0] if numbers else None

    def ahead_commits(self, repo: str, base: str, tip: str) -> tuple[str, ...]:
        """The commits on tip that base lacks (#205). The compare read
        lists up to 250; a longer list fails rather than hide a commit."""
        if not _BRANCH.fullmatch(base) or not _BRANCH.fullmatch(tip):
            raise GitHubAdapterError("branch name is not safe")
        compared = self._json(
            f"{_repo_path(repo)}/compare/{quote(base, safe='/')}...{quote(tip, safe='/')}"
        )
        commits = compared.get("commits") if isinstance(compared, dict) else None
        if not isinstance(commits, list):
            raise GitHubAdapterError("compare read has no commits")
        shas = tuple(
            item["sha"] for item in commits
            if isinstance(item, dict) and isinstance(item.get("sha"), str) and _SHA.fullmatch(item["sha"])
        )
        total = compared.get("total_commits")
        if not isinstance(total, int) or isinstance(total, bool) or total != len(shas):
            raise GitHubAdapterError(f"{tip} has {total} commits past {base}; read {len(shas)}")
        return shas

    def commit_pulls(self, repo: str, sha: str) -> frozenset[int]:
        """The open PRs that carry sha (#205), cached like the gate reads."""
        if not _SHA.fullmatch(sha):
            raise GitHubAdapterError("commit sha is not a full sha")
        pulls = self._json(
            f"{_repo_path(repo)}/commits/{sha}/pulls?per_page=100", cache_ttl=REF_STATUS_CACHE
        )
        if not isinstance(pulls, list):
            raise GitHubAdapterError("commit pulls list is not a list")
        return frozenset(
            item["number"]
            for item in pulls
            if isinstance(item, dict)
            and isinstance(item.get("number"), int)
            and not isinstance(item.get("number"), bool)
            and item.get("state") == "open"
        )

    def pull_state(self, repo: str, number: int) -> str:
        """open, closed or merged: a recorded PR is rechecked before a feed resumes."""
        pull = self._json(f"{_repo_path(repo)}/pulls/{int(number)}")
        state = pull.get("state") if isinstance(pull, dict) else None
        if state not in ("open", "closed"):
            raise GitHubAdapterError("pull request state is not readable")
        if pull.get("merged") is True or pull.get("merged_at") is not None:
            return "merged"
        return str(state)

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


def _linear_blockers(relations: Sequence[object]) -> tuple[tuple[str, str | None], ...]:
    """(identifier, state type) of each issue whose `blocks` relation names this one."""
    found: list[tuple[str, str | None]] = []
    for relation in relations:
        if not isinstance(relation, dict) or relation.get("type") != "blocks":
            continue
        blocker = relation.get("issue") or {}
        identifier = blocker.get("identifier") if isinstance(blocker, dict) else None
        if not isinstance(identifier, str):
            continue
        state = (blocker.get("state") or {}).get("type")
        found.append((identifier, state if isinstance(state, str) else None))
    return tuple(found)


def _linked_page(node: object) -> tuple[bool, str | None]:
    """Whether this page of an issue's linked PRs has a merged one, and the
    cursor of the next page when none does and more remain."""
    linked = node.get("closedByPullRequestsReferences") if isinstance(node, dict) else None
    if not isinstance(linked, dict):
        return False, None
    pulls = linked.get("nodes") or []
    if any(isinstance(pull, dict) and pull.get("merged") is True for pull in pulls):
        return True, None
    page = linked.get("pageInfo") or {}
    if not isinstance(page, dict) or page.get("hasNextPage") is not True:
        return False, None
    cursor = page.get("endCursor")
    if not isinstance(cursor, str) or not cursor:
        raise GitHubAdapterError("linked pull request page has no cursor")
    return False, cursor


def _ref_status(node: object, linked_merged: bool) -> RefStatus:
    if node is None:
        return RefStatus(Progress.MISSING)
    if not isinstance(node, dict):
        raise GitHubAdapterError("ref status node is not an object")
    kind = node.get("__typename")
    state = node.get("state")
    if kind == "PullRequest":
        if node.get("merged") is True or state == "MERGED":
            return RefStatus(Progress.DONE, is_pull=True)
        if state == "CLOSED":
            return RefStatus(Progress.UNFINISHED, is_pull=True, detail="closed without merge")
        if state == "OPEN":
            return RefStatus(Progress.OPEN, is_pull=True)
    elif kind == "Issue":
        if linked_merged:
            return RefStatus(Progress.DONE)
        reason = node.get("stateReason")
        if state == "CLOSED" and reason in _ISSUE_UNFINISHED:
            return RefStatus(Progress.UNFINISHED, detail=_ISSUE_UNFINISHED[str(reason)])
        if state == "CLOSED":
            return RefStatus(Progress.DONE)
        if state == "OPEN":
            return RefStatus(Progress.OPEN)
    raise GitHubAdapterError("ref status is not readable")


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

    def linear_intake(self, project_ids: frozenset[str], state: str) -> list[IntakeIssue]:
        repos = {self.repo_for_project[project_id] for project_id in project_ids}
        if len(repos) != 1:
            raise GitHubAdapterError("Linear intake projects must belong to one repo")
        (repo,) = repos
        projects = sorted(project_ids)
        nodes: list[object] = []
        after: str | None = None
        for _ in range(LINEAR_INTAKE_MAX_PAGES):
            data = self._request(
                LINEAR_INTAKE_QUERY, {"projects": projects, "state": state, "after": after}
            )
            connection = data.get("issues") or {}
            if not isinstance(connection, dict):
                raise GitHubAdapterError("Linear issues is not an object")
            nodes.extend(connection.get("nodes") or [])
            page = connection.get("pageInfo") or {}
            if not isinstance(page, dict) or page.get("hasNextPage") is not True:
                break
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubAdapterError("Linear intake has a next page but no endCursor")
            after = cursor
        else:
            raise GitHubAdapterError(
                f"Linear intake has over {LINEAR_INTAKE_MAX_PAGES} pages; not fed"
            )
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
                    blocked_by=_linear_blockers(self._relations(issue_id, node)),
                )
            )
        return found

    def _relations(self, issue_id: str, node: dict[str, Any]) -> list[object]:
        """Every inverse relation of the issue: the intake page, then the rest."""
        connection = node.get("inverseRelations") or {}
        found: list[object] = []
        for _ in range(LINEAR_RELATIONS_MAX_PAGES):
            if not isinstance(connection, dict):
                raise GitHubAdapterError("Linear relations is not an object")
            found.extend(connection.get("nodes") or [])
            page = connection.get("pageInfo") or {}
            if not isinstance(page, dict) or page.get("hasNextPage") is not True:
                return found
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubAdapterError("Linear relations have a next page but no endCursor")
            data = self._request(LINEAR_RELATIONS_QUERY, {"issue": issue_id, "after": cursor})
            connection = (data.get("issue") or {}).get("inverseRelations")
        raise GitHubAdapterError(
            f"Linear relations have over {LINEAR_RELATIONS_MAX_PAGES} pages; not fed"
        )

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
