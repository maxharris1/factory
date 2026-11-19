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


FEATURES = ".claude/skills/verify-app/features"


def test_dir_names_lists_one_folder_at_a_sha() -> None:
    gh = RecordedGh()
    sha = "a" * 40
    gh.route((f"{REPO}/contents/{FEATURES}?ref={sha}",), ok([
        {"name": "README.md", "type": "file"},
        {"name": "search.md", "type": "file"},
        {"name": "drafts", "type": "dir"},
    ]))
    assert GitHubIntakeClient(gh).dir_names("owner/app", FEATURES, sha) == ("README.md", "search.md")


def test_dir_names_is_empty_for_a_missing_folder() -> None:
    gh = RecordedGh()
    sha = "a" * 40
    gh.route((f"{REPO}/contents/{FEATURES}?ref={sha}",), GhResult(1, "{}", "gh: Not Found (HTTP 404)\n"))
    assert GitHubIntakeClient(gh).dir_names("owner/app", FEATURES, sha) == ()


@pytest.mark.parametrize(
    ("result", "sha"),
    [
        (GhResult(1, "{}", "gh: Server Error (HTTP 502)\n"), "a" * 40),
        (ok({"name": "features", "type": "file"}), "a" * 40),
        (ok([]), "main"),
    ],
    ids=["server-error", "not-a-folder", "not-a-sha"],
)
def test_dir_names_fails_rather_than_guess(result: GhResult, sha: str) -> None:
    gh = RecordedGh()
    gh.route((f"{REPO}/contents/{FEATURES}?ref={sha}",), result)
    with pytest.raises(GitHubAdapterError):
        GitHubIntakeClient(gh).dir_names("owner/app", FEATURES, sha)


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
    "createdAt", "labels", "history", "project", "assignee", "state", "team", "comments",
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

    found = client.linear_intake(frozenset({"proj-1"}), "In Progress", {})

    assert source.documents[0]["query"] == LINEAR_INTAKE_QUERY
    assert source.documents[0]["variables"] == {
        "projects": ["proj-1"], "state": "In Progress", "after": None,
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
    found = LinearIntakeClient(source, {"proj-1": "acme/app"}).linear_intake(frozenset({"proj-1"}), "In Progress", {})
    assert [(item.key, item.branch, item.applied_by) for item in found] == [
        ("AUT-7", "max/aut-7", "max")
    ]


def test_the_linear_intake_reads_every_page() -> None:
    first = [linear_node(number) for number in range(1, 51)]
    second = [linear_node(51, priority=1)]  # urgent, past the first page
    source = PagedLinearSource([first, second])
    found = LinearIntakeClient(source, {"proj-1": "acme/app"}).linear_intake(frozenset({"proj-1"}), "In Progress", {})
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
        LinearIntakeClient(NoCursor([]), {"proj-1": "acme/app"}).linear_intake(frozenset({"proj-1"}), "In Progress", {})


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


@pytest.mark.parametrize(
    ("served", "state"),
    [
        (pull(90, "open"), "open"),
        (pull(90, "closed"), "closed"),
        (pull(90, "closed", merged=True), "merged"),
    ],
)
def test_pull_state_reads_open_closed_or_merged(served: dict, state: str) -> None:
    gh = RecordedGh()
    gh.route((f"{REPO}/pulls/90",), ok(served))
    assert GitHubIntakeClient(gh).pull_state("owner/app", 90) == state


def test_pull_state_refuses_an_unreadable_pull() -> None:
    gh = RecordedGh()
    gh.route((f"{REPO}/pulls/90",), ok({"number": 90}))
    with pytest.raises(GitHubAdapterError):
        GitHubIntakeClient(gh).pull_state("owner/app", 90)


# --- #156: blocker statuses and Linear blocks relations -----------------------


class GraphQLGh:
    """gh api graphql for ref_statuses: answers by repo from served nodes."""

    def __init__(self, served: dict[str, dict | None], *, errors: dict[str, list] | None = None,
                 returncode: int = 0):
        self.served = served
        self.errors = errors or {}
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout: float) -> GhResult:
        argv = list(argv)
        self.calls.append(argv)
        fields = dict(arg.split("=", 1) for arg in argv if "=" in arg and not arg.startswith("--"))
        repo = f"{fields['owner']}/{fields['name']}"
        numbers = re.findall(r"(r\d+): issueOrPullRequest\(number: (\d+)\)", fields["query"])
        nodes = self.served[repo]
        data = {"repository": None if nodes is None else {
            alias: nodes.get(int(number)) for alias, number in numbers
        }}
        document: dict = {"data": data}
        if repo in self.errors:
            document["errors"] = self.errors[repo]
        return GhResult(self.returncode if "errors" in document else 0, json.dumps(document), "")


def pull_node(state: str, merged: bool = False) -> dict:
    return {"__typename": "PullRequest", "state": state, "merged": merged}


def issue_node(state: str, reason: str | None = None, linked: tuple[bool, ...] = ()) -> dict:
    return {"__typename": "Issue", "state": state, "stateReason": reason,
            "closedByPullRequestsReferences": {"nodes": [{"merged": m} for m in linked]}}


def test_ref_statuses_reads_issue_and_pr_kinds() -> None:
    from lib.feeder_gates import Progress, github_ref

    gh = GraphQLGh({"owner/app": {
        1: pull_node("MERGED", merged=True),
        2: pull_node("CLOSED"),
        3: pull_node("OPEN"),
        4: issue_node("CLOSED", "COMPLETED"),
        5: issue_node("CLOSED", "NOT_PLANNED"),
        6: issue_node("CLOSED", "DUPLICATE"),
        7: issue_node("CLOSED", None),
        8: issue_node("OPEN", linked=(False, True)),
        9: issue_node("CLOSED", "NOT_PLANNED", linked=(True,)),
        10: issue_node("OPEN", "REOPENED", linked=(False,)),
    }}, errors={"owner/app": [{"type": "NOT_FOUND", "path": ["repository", "r10"]}]},
        returncode=1)
    refs = [github_ref("owner/app", n) for n in range(1, 12)]
    found = GitHubIntakeClient(gh).ref_statuses(refs)
    assert {int(ref.key): (status.progress, status.is_pull) for ref, status in found.items()} == {
        1: (Progress.DONE, True),
        2: (Progress.UNFINISHED, True),
        3: (Progress.OPEN, True),
        4: (Progress.DONE, False),
        5: (Progress.UNFINISHED, False),
        6: (Progress.UNFINISHED, False),
        7: (Progress.DONE, False),
        8: (Progress.DONE, False),
        9: (Progress.DONE, False),
        10: (Progress.OPEN, False),
        11: (Progress.MISSING, False),
    }
    assert found[github_ref("owner/app", 2)].detail == "closed without merge"
    assert found[github_ref("owner/app", 5)].detail == "closed as not planned"


def test_ref_statuses_is_one_cached_graphql_read_per_repo() -> None:
    from lib.feeder_gates import github_ref

    gh = GraphQLGh({
        "owner/app": {n: issue_node("OPEN") for n in range(1, 61)},
        "other/lib": {3: pull_node("OPEN")},
    })
    client = GitHubIntakeClient(gh)
    refs = [github_ref("owner/app", n) for n in range(1, 61)] + [github_ref("other/lib", 3)]
    assert len(client.ref_statuses(refs)) == 61
    assert len(gh.calls) == 3  # owner/app in chunks of 50, then other/lib
    for argv in gh.calls:
        api = argv.index("api")
        assert argv[api + 1:api + 4] == ["--cache", "60s", "graphql"]
    # The client's other reads stay uncached: find_pull never sees a stale "no PR".
    recorded = RecordedGh()
    recorded.route(FIND, ok([]))
    GitHubIntakeClient(recorded).find_pull("owner/app", "factory/issue-85")
    assert "--cache" not in recorded.calls[0]


def test_ref_statuses_marks_a_missing_repo_and_fails_on_other_errors() -> None:
    from lib.feeder_gates import Progress, github_ref

    gone = GraphQLGh({"gone/repo": None},
                     errors={"gone/repo": [{"type": "NOT_FOUND", "path": ["repository"]}]},
                     returncode=1)
    found = GitHubIntakeClient(gone).ref_statuses([github_ref("gone/repo", 1)])
    assert found[github_ref("gone/repo", 1)].progress is Progress.MISSING

    limited = GraphQLGh({"owner/app": {}},
                        errors={"owner/app": [{"type": "RATE_LIMITED", "path": ["repository"]}]},
                        returncode=1)
    with pytest.raises(GitHubAdapterError):
        GitHubIntakeClient(limited).ref_statuses([github_ref("owner/app", 1)])

    def down(argv, timeout: float) -> GhResult:
        return GhResult(1, "", "gh: HTTP 502")

    with pytest.raises(GitHubAdapterError):
        GitHubIntakeClient(down).ref_statuses([github_ref("owner/app", 1)])



class LinkedPullsGh:
    """One open issue, #1, with linked PRs served the way GitHub pages a
    connection: honoring first and after, with pageInfo."""

    def __init__(self, merged: list[bool]):
        self.merged = merged
        self.queries: list[dict[str, str]] = []

    def page(self, first: int, after: str | None) -> dict:
        start = int(after) if after else 0
        end = min(start + first, len(self.merged))
        return {"nodes": [{"merged": m} for m in self.merged[start:end]],
                "pageInfo": {"hasNextPage": end < len(self.merged), "endCursor": str(end)}}

    def __call__(self, argv, timeout: float) -> GhResult:
        fields = dict(arg.split("=", 1) for arg in argv if "=" in arg and not arg.startswith("--"))
        self.queries.append(fields)
        query = fields["query"]
        first = int(re.search(r"closedByPullRequestsReferences\(first: (\d+)", query)[1])
        issue = {"__typename": "Issue", "state": "OPEN", "stateReason": None,
                 "closedByPullRequestsReferences": self.page(first, fields.get("after"))}
        aliases = re.findall(r"(r\d+): issueOrPullRequest\(number: 1\)", query)
        repository = {alias: issue for alias in aliases} if aliases else {"issue": issue}
        return GhResult(0, json.dumps({"data": {"repository": repository}}), "")


@pytest.mark.parametrize("count", [11, 101, 250, 1000])
def test_ref_statuses_finds_a_merged_linked_pr_on_any_page(count: int) -> None:
    from lib.feeder_gates import Progress, github_ref

    gh = LinkedPullsGh([False] * count)
    blocker = github_ref("owner/app", 1)
    assert GitHubIntakeClient(gh).ref_statuses([blocker])[blocker].progress is Progress.OPEN
    gh.merged[-1] = True
    gh.queries.clear()
    assert GitHubIntakeClient(gh).ref_statuses([blocker])[blocker].progress is Progress.DONE
    assert all(query["owner"] == "owner" and query["name"] == "app" for query in gh.queries)


def test_ref_statuses_stops_paging_at_the_first_merged_linked_pr() -> None:
    from lib.feeder_gates import Progress, github_ref

    gh = LinkedPullsGh([True] + [False] * 500)
    blocker = github_ref("owner/app", 1)
    assert GitHubIntakeClient(gh).ref_statuses([blocker])[blocker].progress is Progress.DONE
    assert len(gh.queries) == 1


def test_ref_statuses_fails_past_the_linked_pr_page_cap() -> None:
    from lib.feeder_gates import github_ref

    gh = LinkedPullsGh([False] * 5000)
    with pytest.raises(GitHubAdapterError):
        GitHubIntakeClient(gh).ref_statuses([github_ref("owner/app", 1)])


def test_feeder_releases_a_dependent_once_a_late_linked_pr_merges(tmp_path) -> None:
    from tests.test_feeder import issue, make_feeder, rows

    feeder, _, github, _ = make_feeder(tmp_path)
    gh = LinkedPullsGh([False] * 150)
    github.ref_statuses = GitHubIntakeClient(gh).ref_statuses
    github.issues["owner/app"] = [issue(2, body="Blocked by #1")]
    first = feeder.feed_once()
    assert first.fed == () and first.blocked == 1
    assert rows(tmp_path / "orch.db")["2"]["waiting_reason"] == "waiting on #1"
    gh.merged[-1] = True
    second = feeder.feed_once()
    assert len(second.fed) == 1 and second.blocked == 0

def test_linear_intake_reads_blocks_relations() -> None:
    node = linear_node(8)
    node["inverseRelations"] = {"nodes": [
        {"type": "blocks", "issue": {"identifier": "AUT-7", "state": {"type": "started"}}},
        {"type": "related", "issue": {"identifier": "AUT-6", "state": {"type": "started"}}},
        {"type": "blocks", "issue": {"identifier": "AUT-5", "state": {"type": "completed"}}},
    ]}
    found = LinearIntakeClient(PagedLinearSource([[node]]), {"proj-1": "acme/app"}).linear_intake(
        frozenset({"proj-1"}), "In Progress", {}
    )
    assert found[0].blocked_by == (("AUT-7", "started"), ("AUT-5", "completed"))
    assert "inverseRelations(first: 10) { nodes { type issue { identifier state { type } } } " in (
        LINEAR_INTAKE_QUERY
    )


def test_linear_relations_add_no_linear_request() -> None:
    node = linear_node(8)
    node["inverseRelations"] = {"nodes": [
        {"type": "blocks", "issue": {"identifier": "AUT-7", "state": {"type": "started"}}},
    ]}
    source = PagedLinearSource([[node]])
    LinearIntakeClient(source, {"proj-1": "acme/app"}).linear_intake(frozenset({"proj-1"}), "In Progress", {})
    assert len(source.documents) == 1


class RelationPagesSource(PagedLinearSource):
    """One issue with 120 blocks relations, served the way Linear pages them:
    the intake query's `first`, then the relations query's `first` and `after`."""

    def __init__(self) -> None:
        super().__init__([])
        self.relations = [
            {"type": "blocks", "issue": {"identifier": f"AUT-{n}",
             "state": {"type": "completed" if n < 120 else "started"}}}
            for n in range(1, 121)
        ]

    def _page(self, query: str, after: str | None) -> dict:
        count = int(re.search(r"inverseRelations\(first: (\d+)", query).group(1))
        start = 0 if after is None else int(after.removeprefix("rel-"))
        end = start + count
        more = end < len(self.relations)
        return {"nodes": self.relations[start:end],
                "pageInfo": {"hasNextPage": more, "endCursor": f"rel-{end}" if more else None}}

    def _linear_request(self, document: dict) -> dict:
        self.documents.append(document)
        query = document["query"]
        if "issues(" in query:
            node = linear_node(200)
            node["inverseRelations"] = self._page(query, None)
            return {"data": {"issues": {"nodes": [node], "pageInfo": {"hasNextPage": False}}}}
        assert document["variables"]["issue"] == "uuid-200"
        return {"data": {"issue": {
            "inverseRelations": self._page(query, document["variables"]["after"])}}}


def test_linear_intake_reads_every_page_of_blocks_relations() -> None:
    source = RelationPagesSource()
    found = LinearIntakeClient(source, {"proj-1": "acme/app"}).linear_intake(frozenset({"proj-1"}), "In Progress", {})
    assert len(found[0].blocked_by) == 120
    assert found[0].blocked_by[-1] == ("AUT-120", "started")
    assert len(source.documents) == 4


def test_linear_relation_pages_without_a_cursor_fail_the_read() -> None:
    class NoCursor(RelationPagesSource):
        def _page(self, query: str, after: str | None) -> dict:
            return {"nodes": self.relations[:1], "pageInfo": {"hasNextPage": True, "endCursor": None}}

    with pytest.raises(GitHubAdapterError, match="endCursor"):
        LinearIntakeClient(NoCursor(), {"proj-1": "acme/app"}).linear_intake(frozenset({"proj-1"}), "In Progress", {})
