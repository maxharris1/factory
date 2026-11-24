"""Live GitHub and Linear clients for the backlog feeder (lib/feeder.py, #85).

GitHub goes through the same `env -u GH_TOKEN ... gh api` seam as every other
GitHub call (lib/github_actions.py run_gh), with the token variables unset;
the client never reads, passes or logs a token. Linear goes through the
poller's own Linear request path (LivePollSource._linear_request), so the
feeder's Linear reads and comments share the poll's request and complexity
budget and its rate-limit backoff, and the API key is only read from the
configured environment variable at request time.

Reads:
* ahead_commits, open_pull_commits and pull_commits: for the
  stacked-branch hold (#205), a branch's commits past its base, every open
  PR's first 100 commits, and all of one PR's commits;
* github_intake: open issues carrying the label (`issues?labels=`), and for
  each, who applied the label: the actor of the latest `labeled` event for
  that label (`issues/<n>/events`). That lookup is cached on the issue's
  updated_at, which GitHub bumps on every label change;
* linear_intake: issues in any of a repo's projects and in the workflow
  state (assigned to a listed user, for a project with linear_assignees),
  every page of them, and for each, who moved it there: the actor of the
  latest history entry whose toState is that state, or the creator when the
  issue was created in that state;
* tree_paths: every file path at a commit, or under one folder of it
  (one recursive `git/trees` read of `<sha>` or `<sha>:<folder>`), and
  whether GitHub truncated it, to find the verify skill (#1029);
* dir_names: the file names, and the subfolder names with a trailing `/`,
  in one repo folder at a commit (`contents/`), for the feature map check
  and the truncated tree walk (#1029);
* closed_issues_since: issues closed at or after a time (`issues?state=closed&since=`);
* split_children: every issue whose body says `Split from #<parent>`, found
  by search (`search/issues`) or by the parent's timeline cross-references
  (`issues/<parent>/timeline`), which a lagging search index cannot omit;
  the timeline's copy wins. A search that returns fewer issues than its
  total_count, as past the 1,000-result ceiling, fails the read rather than
  returning a partial set;
* issue_record: one issue's state, close reason and body (`issues/<n>`);
* pull_landing: a stacked child's parent PR as open, closed or merged, and
  for a merged one its merge target and every head its branch held (the
  head at merge plus each force push's before commit, every timeline page);
  a timeline it cannot read in full raises StackHeadsUnknown.

Writes (each one exactly once per call; the feeder makes them idempotent):
the empty start commit (git data API: the base commit's tree, one parent),
the branch ref, the draft PR, the PR labels, PR comments, Linear comments.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, TypeVar
from urllib.parse import quote

from lib.branch_stack import OpenPull
from lib.dispatch.constants import GITHUB_STATE_LABEL_PREFIX
from lib.dispatch.types import ParentClosed, ParentFacts, ParentMerged, ParentOpen
from lib.dispatcher import Comment
from lib.feeder import IntakeIssue, IssueRecord
from lib.feeder_gates import Progress, Ref, RefStatus
from lib.followups import split_parent
from lib.github_actions import (
    GitHubAdapterError,
    GitHubReviewActions,
    _github_branch,
    _repo_path,
    _status,
)
from lib.update_check import StackUnbounded

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
_LINEAR_INTAKE_SELECTION = (
    "{ nodes { id identifier title description url gitBranchName: branchName priority createdAt "
    "project { id } assignee { id } creator { id } "
    "labels(first: 20) { nodes { name } } "
    "history(first: 25) { nodes { createdAt actor { id } toState { name } } pageInfo { hasNextPage } } "
    f"inverseRelations(first: {LINEAR_INTAKE_RELATIONS}) {_RELATION_FIELDS} }} "
    "pageInfo { hasNextPage endCursor } }"
)
LINEAR_INTAKE_QUERY = (
    "query($projects: [ID!]!, $state: String!, $after: String) { issues(first: 50, after: $after, "
    "filter: { project: { id: { in: $projects } }, state: { name: { eq: $state } } }) "
    f"{_LINEAR_INTAKE_SELECTION} }}"
)
LINEAR_ASSIGNED_INTAKE_QUERY = (
    "query($projects: [ID!]!, $state: String!, $assignees: [ID!]!, $after: String) "
    "{ issues(first: 50, after: $after, "
    "filter: { project: { id: { in: $projects } }, state: { name: { eq: $state } }, "
    "assignee: { id: { in: $assignees } } }) "
    f"{_LINEAR_INTAKE_SELECTION} }}"
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
    "... on PullRequest { state merged labels(first: 30) { nodes { name } } } "
    "... on Issue { state stateReason " + _LINKED_PULLS.replace("{after}", "") + " }"
)
LINKED_PULLS_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $after: String!) "
    "{ repository(owner: $owner, name: $name) { issue(number: $number) { "
    + _LINKED_PULLS.replace("{after}", ", after: $after") + " } } }"
)
OPEN_PULLS_MAX_PAGES = 20
OPEN_PULL_COMMITS_QUERY = (
    "query($owner: String!, $name: String!, $after: String) "
    "{ repository(owner: $owner, name: $name) { pullRequests(states: OPEN, first: 50, after: $after) "
    "{ nodes { number baseRefName headRefName headRefOid commits(first: 100) { totalCount nodes { commit { oid } } } } "
    "pageInfo { hasNextPage endCursor } } } }"
)
OPEN_PULLS_TTL = 60.0
PULL_LANDING_MAX_PAGES = 20
PULL_LANDING_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $after: String) "
    "{ repository(owner: $owner, name: $name) { pullRequest(number: $number) { "
    "state baseRefName headRefOid "
    "timelineItems(itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT], first: 100, after: $after) "
    "{ filteredCount pageInfo { hasNextPage endCursor } "
    "nodes { ... on HeadRefForcePushedEvent { beforeCommit { oid } } } } } } }"
)
PULL_COMMITS_PAGE = 250
PULL_COMMITS_MAX_PAGES = 40
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


def _issue_items(pages: Sequence[object]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, list):
            raise GitHubAdapterError("issue page is not a list")
        items.extend(
            item
            for item in page
            if isinstance(item, dict)
            and "pull_request" not in item
            and isinstance(item.get("number"), int)
            and not isinstance(item.get("number"), bool)
        )
    return items


def _issue_record(item: dict[str, Any]) -> IssueRecord:
    reason = item.get("state_reason")
    return IssueRecord(
        number=item["number"],
        state=_text(item.get("state")),
        reason=reason if isinstance(reason, str) else None,
        body=_text(item.get("body")),
    )


class StackHeadsUnknown(GitHubAdapterError, StackUnbounded):
    pass


def _branch_path(branch: str) -> str:
    if not _github_branch(branch):
        raise GitHubAdapterError("branch name is not safe")
    return quote(branch, safe="/")


class GitHubIntakeClient(GitHubReviewActions):
    """The feeder's GitHub reads and writes, on the adapter's gh seam."""

    def __init__(
        self,
        *args: Any,
        list_pages: Callable[[str], list[object]] | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.list_pages = list_pages
        self._applied: dict[tuple[str, int, str, str], str | None] = {}
        self.clock: Callable[[], float] = time.monotonic
        self._open_pull_pages: dict[
            tuple[str, str | None], tuple[float, dict[int, OpenPull], str | None]
        ] = {}
        self._compare_pages: dict[tuple[str, str, str, int], tuple[float, int, tuple[str, ...]]] = {}

    def github_intake(self, repo: str, label: str) -> list[IntakeIssue]:
        path = _repo_path(repo)
        found: list[IntakeIssue] = []
        endpoint = f"{path}/issues?state=open&labels={quote(label, safe='')}&per_page=100"
        for page in (self.list_pages or self._pages)(endpoint):
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

    def closed_issues_since(self, repo: str, since: float) -> tuple[IssueRecord, ...]:
        stamp = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        endpoint = f"{_repo_path(repo)}/issues?state=closed&since={stamp}&per_page=100"
        items = _issue_items(self._pages(endpoint))
        return tuple(
            _issue_record(item) for item in items if _epoch(item.get("closed_at")) >= since
        )

    def split_children(self, repo: str, parent: int) -> tuple[IssueRecord, ...]:
        name = _repo_path(repo).removeprefix("repos/")
        query = quote(f'repo:{name} is:issue in:body "Split from #{parent}"', safe="")
        pages = self._pages(f"search/issues?q={query}&per_page=100")
        searches = [page for page in pages if isinstance(page, dict)]
        if len(searches) != len(pages):
            raise GitHubAdapterError("issue search page is not an object")
        if any(search.get("incomplete_results") for search in searches):
            raise GitHubAdapterError("issue search results are incomplete")
        totals = [search.get("total_count") for search in searches]
        if not all(type(total) is int for total in totals):
            raise GitHubAdapterError("issue search page has no total_count")
        found = [search.get("items") for search in searches]
        returned = sum(len(page) for page in found if isinstance(page, list))
        if returned < max(totals):
            raise GitHubAdapterError(
                f"issue search returned {returned} of {max(totals)} issues"
            )
        children = {
            child.number: child
            for child in map(_issue_record, _issue_items(found))
            if split_parent(child.body) == parent
        }
        children.update((child.number, child) for child in self._referencing_children(repo, parent))
        return tuple(sorted(children.values(), key=lambda child: child.number))

    def _referencing_children(self, repo: str, parent: int) -> list[IssueRecord]:
        path = _repo_path(repo)
        pages = self._pages(f"{path}/issues/{parent}/timeline?per_page=100")
        if not all(isinstance(page, list) for page in pages):
            raise GitHubAdapterError("issue timeline page is not a list")
        here = f"https://api.github.com/{path}".lower()
        sources = [
            event["source"]["issue"]
            for page in pages
            for event in page
            if isinstance(event, dict)
            and event.get("event") == "cross-referenced"
            and isinstance(event.get("source"), dict)
            and isinstance(event["source"].get("issue"), dict)
            and _text(event["source"]["issue"].get("repository_url")).lower() == here
        ]
        return [
            child
            for child in map(_issue_record, _issue_items([sources]))
            if split_parent(child.body) == parent
        ]

    def issue_record(self, repo: str, number: int) -> IssueRecord:
        items = _issue_items([[self._json(f"{_repo_path(repo)}/issues/{number}")]])
        if not items:
            raise GitHubAdapterError(f"{repo}#{number} is not an issue")
        return _issue_record(items[0])

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
        path = _branch_path(branch)
        try:
            ref = self._json(f"{_repo_path(repo)}/git/ref/heads/{path}")
        except GitHubAdapterError as exc:
            if _status(exc) == 404:
                return None
            raise
        sha = ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise GitHubAdapterError(f"ref heads/{branch} has no commit sha")
        return sha

    def tree_paths(self, repo: str, sha: str, path: str = "") -> tuple[tuple[str, ...], bool]:
        if not _SHA.fullmatch(sha):
            raise GitHubAdapterError("tree read sha is not a full sha")
        tree_ish = f"{sha}:{quote(path)}" if path else sha
        tree = self._json(f"{_repo_path(repo)}/git/trees/{tree_ish}?recursive=1")
        entries = tree.get("tree") if isinstance(tree, dict) else None
        if not isinstance(entries, list):
            raise GitHubAdapterError(f"tree {sha} has no entries")
        paths = tuple(
            entry["path"] for entry in entries
            if isinstance(entry, dict) and entry.get("type") == "blob" and isinstance(entry.get("path"), str)
        )
        return paths, tree.get("truncated") is True

    def dir_names(self, repo: str, path: str, sha: str) -> tuple[str, ...]:
        if not _SHA.fullmatch(sha):
            raise GitHubAdapterError("folder read sha is not a full sha")
        try:
            entries = self._json(f"{_repo_path(repo)}/contents/{quote(path)}?ref={sha}")
        except GitHubAdapterError as exc:
            if _status(exc) == 404:
                return ()
            raise
        if not isinstance(entries, list):
            raise GitHubAdapterError(f"{path} is not a folder")
        return tuple(
            entry["name"] + ("/" if entry.get("type") == "dir" else "") for entry in entries
            if isinstance(entry, dict) and entry.get("type") in ("file", "dir") and isinstance(entry.get("name"), str)
        )

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
        numbers = sorted(
            item["number"]
            for item in self._branch_pull_items(repo, branch)
            if item.get("state") == "open" and item.get("merged_at") is None
        )
        return numbers[0] if numbers else None

    def branch_pulls(self, repo: str, branch: str) -> tuple[int, ...]:
        return tuple(sorted(item["number"] for item in self._branch_pull_items(repo, branch)))

    def _branch_pull_items(self, repo: str, branch: str) -> list[dict]:
        owner = repo.split("/", 1)[0]
        endpoint = (
            f"{_repo_path(repo)}/pulls?state=all&per_page=100"
            f"&head={quote(owner, safe='')}:{quote(branch, safe='/')}"
        )
        pulls = self._json(endpoint)
        if not isinstance(pulls, list):
            raise GitHubAdapterError("pulls list is not a list")
        return [
            item
            for item in pulls
            if isinstance(item, dict)
            and isinstance(item.get("number"), int)
            and not isinstance(item.get("number"), bool)
            and ((item.get("head") or {}).get("ref") == branch)
        ]

    def ahead_commits(self, repo: str, base: str, tip: str) -> tuple[str, ...]:
        """The commits on tip that base lacks (#205). The compare read
        lists up to 250; a longer list fails rather than hide a commit."""
        compared = self._json(
            f"{_repo_path(repo)}/compare/{_branch_path(base)}...{_branch_path(tip)}"
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

    def open_pull_commits(self, repo: str) -> dict[int, OpenPull]:
        found: dict[int, OpenPull] = {}
        after: str | None = None
        for _ in range(OPEN_PULLS_MAX_PAGES):
            pulls, after = self._open_pull_page(repo, after)
            found.update(pulls)
            if after is None:
                return found
        raise GitHubAdapterError("open pull requests exceed the page cap")

    def _open_pull_page(
        self, repo: str, after: str | None
    ) -> tuple[dict[int, OpenPull], str | None]:
        key, now = (repo.lower(), after), self.clock()
        held = self._open_pull_pages.get(key)
        if held is not None and now - held[0] < OPEN_PULLS_TTL:
            return held[1], held[2]
        owner, _, name = _repo_path(repo).removeprefix("repos/").partition("/")
        document = self._json(
            "graphql", "-f", f"query={OPEN_PULL_COMMITS_QUERY}",
            "-f", f"owner={owner}", "-f", f"name={name}",
            *(("-f", f"after={after}") if after is not None else ()),
        )
        listed = _graphql_repository(document).get("pullRequests")
        if not isinstance(listed, dict) or not isinstance(listed.get("nodes"), list):
            raise GitHubAdapterError("open pull request read returned no pull requests")
        pulls = dict(_open_pull(node) for node in listed["nodes"])
        page = listed.get("pageInfo")
        following = None
        if isinstance(page, dict) and page.get("hasNextPage") is True:
            following = page.get("endCursor")
            if not isinstance(following, str) or not following:
                raise GitHubAdapterError("open pull request page has no cursor")
        self._open_pull_pages = _unexpired(self._open_pull_pages, now)
        self._open_pull_pages[key] = (now, pulls, following)
        return pulls, following

    def pull_commits(self, repo: str, base: str, head: str) -> frozenset[str]:
        total, shas = self._compare_page(repo, base, head, 1)
        pages = max(1, -(-total // PULL_COMMITS_PAGE))
        if pages > PULL_COMMITS_MAX_PAGES:
            raise GitHubAdapterError(f"{head} has {total} commits past {base}; past the page cap")
        found = set(shas)
        for number in range(2, pages + 1):
            page_total, shas = self._compare_page(repo, base, head, number)
            if page_total != total:
                raise GitHubAdapterError(f"{head} compare total changed from {total} to {page_total}")
            found.update(shas)
        if len(found) != total:
            raise GitHubAdapterError(f"{head} has {total} commits past {base}; read {len(found)}")
        return frozenset(found)

    def _compare_page(self, repo: str, base: str, head: str, page: int) -> tuple[int, tuple[str, ...]]:
        key, now = (repo.lower(), base, head, page), self.clock()
        held = self._compare_pages.get(key)
        if held is not None and now - held[0] < OPEN_PULLS_TTL:
            return held[1], held[2]
        compared = self._json(
            f"{_repo_path(repo)}/compare/{_branch_path(base)}...{_branch_path(head)}"
            f"?per_page={PULL_COMMITS_PAGE}&page={page}"
        )
        commits = compared.get("commits") if isinstance(compared, dict) else None
        total = compared.get("total_commits") if isinstance(compared, dict) else None
        if not isinstance(commits, list) or not isinstance(total, int) or isinstance(total, bool):
            raise GitHubAdapterError("compare read has no commits")
        shas = tuple(item.get("sha") if isinstance(item, dict) else None for item in commits)
        if any(not isinstance(sha, str) or not _SHA.fullmatch(sha) for sha in shas):
            raise GitHubAdapterError("compare read has a commit without a sha")
        expected = max(0, min(PULL_COMMITS_PAGE, total - PULL_COMMITS_PAGE * (page - 1)))
        if len(shas) != expected:
            raise GitHubAdapterError(
                f"compare page {page} of {head} lists {len(shas)} commits; expected {expected}"
            )
        self._compare_pages = _unexpired(self._compare_pages, now)
        self._compare_pages[key] = (now, total, shas)
        return total, shas

    def pull_state(self, repo: str, number: int) -> str:
        """open, closed or merged: a recorded PR is rechecked before a feed resumes."""
        pull = self._json(f"{_repo_path(repo)}/pulls/{int(number)}")
        state = pull.get("state") if isinstance(pull, dict) else None
        if state not in ("open", "closed"):
            raise GitHubAdapterError("pull request state is not readable")
        if pull.get("merged") is True or pull.get("merged_at") is not None:
            return "merged"
        return str(state)

    def pull_base(self, repo: str, number: int) -> str:
        pull = self._json(f"{_repo_path(repo)}/pulls/{int(number)}")
        base = pull.get("base") if isinstance(pull, dict) else None
        ref = base.get("ref") if isinstance(base, dict) else None
        if not _github_branch(ref):
            raise GitHubAdapterError("pull request base branch is not readable")
        return ref

    def pull_landing(self, repo: str, number: int) -> ParentFacts:
        owner, _, name = _repo_path(repo).removeprefix("repos/").partition("/")
        befores: list[str] = []
        after: str | None = None
        total: int | None = None
        for _ in range(PULL_LANDING_MAX_PAGES):
            document = self._json(
                "graphql", "-f", f"query={PULL_LANDING_QUERY}",
                "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={int(number)}",
                *(("-f", f"after={after}") if after is not None else ()),
            )
            pull = _graphql_repository(document).get("pullRequest")
            if not isinstance(pull, dict):
                raise GitHubAdapterError(f"{repo}#{number} is not a pull request")
            state = pull.get("state")
            if state == "OPEN":
                return ParentOpen()
            if state == "CLOSED":
                return ParentClosed(int(number))
            into, head = pull.get("baseRefName"), pull.get("headRefOid")
            if state != "MERGED" or not _github_branch(into) or not _is_sha(head):
                raise GitHubAdapterError(f"PR #{number} state, base or head is not readable")
            remaining, page, after = _force_push_page(number, pull.get("timelineItems"))
            total = len(befores) + remaining if total is None else total
            if len(befores) + remaining != total:
                raise StackHeadsUnknown(f"PR #{number} force pushes changed while paging")
            befores.extend(page)
            if after is None:
                if len(befores) != total:
                    raise StackHeadsUnknown(
                        f"PR #{number} has {total} force pushes; read {len(befores)}"
                    )
                return ParentMerged(
                    into=into, head=head, heads=tuple(dict.fromkeys((head, *befores)))
                )
        raise StackHeadsUnknown(f"PR #{number} force pushes exceed the page cap")

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

    def pull_body(self, repo: str, number: int) -> str:
        pull = self._json(f"{_repo_path(repo)}/pulls/{int(number)}")
        body = (pull.get("body") or "") if isinstance(pull, dict) else None
        if not isinstance(body, str):
            raise GitHubAdapterError("pull request body is not readable")
        return body

    def set_pull_body(self, repo: str, number: int, body: str) -> None:
        made = self._json(
            "-X", "PATCH", f"{_repo_path(repo)}/pulls/{int(number)}",
            "-f", f"body={body}",
        )
        if not isinstance(made, dict) or made.get("number") != int(number):
            raise GitHubAdapterError("pull request body update was not confirmed")

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


_Held = TypeVar("_Held", bound=tuple)


def _unexpired(cache: dict[Any, _Held], now: float) -> dict[Any, _Held]:
    return {key: held for key, held in cache.items() if now - held[0] < OPEN_PULLS_TTL}


def _graphql_repository(document: object) -> dict[str, Any]:
    data = document.get("data") if isinstance(document, dict) else None
    repository = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repository, dict) or document.get("errors"):
        raise GitHubAdapterError("GraphQL read returned no repository")
    return repository


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _force_push_page(number: int, items: object) -> tuple[int, tuple[str, ...], str | None]:
    nodes = items.get("nodes") if isinstance(items, dict) else None
    remaining = items.get("filteredCount") if isinstance(items, dict) else None
    page = items.get("pageInfo") if isinstance(items, dict) else None
    if not isinstance(nodes, list) or type(remaining) is not int or not isinstance(page, dict):
        raise StackHeadsUnknown(f"PR #{number} force pushes are not readable")
    commits = [node.get("beforeCommit") if isinstance(node, dict) else None for node in nodes]
    befores = tuple(commit.get("oid") if isinstance(commit, dict) else None for commit in commits)
    if not all(_is_sha(before) for before in befores):
        raise StackHeadsUnknown(f"PR #{number} has a force push with no before commit")
    if page.get("hasNextPage") is not True:
        return remaining, befores, None
    cursor = page.get("endCursor")
    if not isinstance(cursor, str) or not cursor:
        raise StackHeadsUnknown(f"PR #{number} force push page has no cursor")
    return remaining, befores, cursor


def _open_pull(node: object) -> tuple[int, OpenPull]:
    number = node.get("number") if isinstance(node, dict) else None
    if not isinstance(number, int) or isinstance(number, bool):
        raise GitHubAdapterError("open pull request has no number")
    base, head, branch = node.get("baseRefName"), node.get("headRefOid"), node.get("headRefName")
    if (
        not _github_branch(base) or not isinstance(branch, str)
        or not isinstance(head, str) or not _SHA.fullmatch(head)
    ):
        raise GitHubAdapterError(f"open PR #{number} base or head is not readable")
    commits = node.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    total = commits.get("totalCount") if isinstance(commits, dict) else None
    if not isinstance(nodes, list) or not isinstance(total, int) or isinstance(total, bool):
        raise GitHubAdapterError(f"open PR #{number} commits are not readable")
    shas = [
        item["commit"].get("oid")
        if isinstance(item, dict) and isinstance(item.get("commit"), dict) else None
        for item in nodes
    ]
    if any(not isinstance(sha, str) or not _SHA.fullmatch(sha) for sha in shas) or total < len(shas):
        raise GitHubAdapterError(f"open PR #{number} commits are not readable")
    return number, OpenPull(
        commits=frozenset(shas), complete=total == len(shas), base=base, head=head, branch=branch
    )


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
        labels = node.get("labels")
        nodes = labels.get("nodes") if isinstance(labels, dict) else None
        abandoned = isinstance(nodes, list) and any(
            isinstance(label, dict) and label.get("name") == GITHUB_STATE_LABEL_PREFIX + "abandoned"
            for label in nodes
        )
        if node.get("merged") is True or state == "MERGED":
            return RefStatus(Progress.DONE, is_pull=True, abandoned=abandoned)
        if state == "CLOSED":
            return RefStatus(Progress.UNFINISHED, is_pull=True, detail="closed without merge", abandoned=abandoned)
        if state == "OPEN":
            return RefStatus(Progress.OPEN, is_pull=True, abandoned=abandoned)
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

    def linear_intake(
        self,
        project_ids: frozenset[str],
        state: str,
        assignees: Mapping[str, frozenset[str]],
    ) -> list[IntakeIssue]:
        repos = {self.repo_for_project[project_id] for project_id in project_ids}
        if len(repos) != 1:
            raise GitHubAdapterError("Linear intake projects must belong to one repo")
        (repo,) = repos
        nodes: list[object] = []
        open_projects = sorted(project_id for project_id in project_ids if project_id not in assignees)
        if open_projects:
            nodes.extend(
                self._intake_nodes(LINEAR_INTAKE_QUERY, {"projects": open_projects, "state": state})
            )
        allowlisted_projects = sorted(project_id for project_id in project_ids if project_id in assignees)
        for project_id in allowlisted_projects:
            nodes.extend(
                self._intake_nodes(
                    LINEAR_ASSIGNED_INTAKE_QUERY,
                    {
                        "projects": [project_id],
                        "state": state,
                        "assignees": sorted(assignees[project_id]),
                    },
                )
            )
        found: list[IntakeIssue] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            identifier = node.get("identifier")
            issue_id = node.get("id")
            if not isinstance(identifier, str) or not isinstance(issue_id, str):
                continue
            history = node.get("history") or {}
            moves = [
                entry
                for entry in (history.get("nodes") or [])
                if isinstance(entry, dict)
                and ((entry.get("toState") or {}).get("name")) == state
            ]
            moves.sort(key=lambda entry: _text(entry.get("createdAt")))
            if moves:
                actor = (moves[-1].get("actor") or {}).get("id")
            elif (history.get("pageInfo") or {}).get("hasNextPage") is not True:
                actor = (node.get("creator") or {}).get("id")
            else:
                actor = None
            priority = node.get("priority")
            project = (node.get("project") or {}).get("id")
            assignee = (node.get("assignee") or {}).get("id")
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
                    linear_project=project if isinstance(project, str) else None,
                    assignee=assignee if isinstance(assignee, str) else None,
                )
            )
        return found

    def _intake_nodes(self, query: str, variables: dict[str, object]) -> list[object]:
        nodes: list[object] = []
        after: str | None = None
        for _ in range(LINEAR_INTAKE_MAX_PAGES):
            data = self._request(query, {**variables, "after": after})
            connection = data.get("issues") or {}
            if not isinstance(connection, dict):
                raise GitHubAdapterError("Linear issues is not an object")
            nodes.extend(connection.get("nodes") or [])
            page = connection.get("pageInfo") or {}
            if not isinstance(page, dict) or page.get("hasNextPage") is not True:
                return nodes
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubAdapterError("Linear intake has a next page but no endCursor")
            after = cursor
        raise GitHubAdapterError(
            f"Linear intake has over {LINEAR_INTAKE_MAX_PAGES} pages; not fed"
        )

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
