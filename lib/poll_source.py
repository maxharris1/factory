"""Production GitHub and Linear source for the dispatcher poll loop."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from lib.dispatcher import Comment, ReviewActions, TicketRef, TrackedTicket
from lib.factory_md import FactoryConfig, parse_factory_md
from lib.github_actions import (
    GH,
    GitHubReviewActions,
    LinearReviewActions,
    Runner,
    run_gh,
)
from lib.handoff import HandoffError, parse_comment
from lib.orch_yml import OrchConfig, Project, resolve_job_timeout

LINEAR_ENDPOINT = "https://api.linear.app/graphql"
TIMEOUT_SECONDS = 60.0
LINEAR_QUERY = """
query FactoryPoll($term: String!, $first: Int!, $after: String) {
  searchIssues(term: $term, first: $first, after: $after) {
    nodes {
      id
      identifier
      url
      gitBranchName: branchName
      project { id name }
      team {
        states { nodes { id name } }
      }
      comments {
        nodes { id body createdAt user { name } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip()
LINEAR_READY_MUTATION = """
mutation FactoryReadyForReview($issue: String!, $state: String!) {
  issueUpdate(id: $issue, input: {stateId: $state}) { success }
}
""".strip()


class PollSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: str


LinearTransport = Callable[[str, Mapping[str, str], bytes, float], HTTPResult]


def post_json(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> HTTPResult:
    try:
        request = Request(url, data=body, method="POST", headers=dict(headers))
        with urlopen(request, timeout=timeout) as response:
            return HTTPResult(
                response.status,
                response.read().decode("utf-8"),
            )
    except HTTPError as exc:
        return HTTPResult(exc.code, "")
    except (OSError, URLError, ValueError) as exc:
        raise PollSourceError(
            f"Linear request failed: {type(exc).__name__}"
        ) from None


class LivePollSource:
    def __init__(
        self,
        config: OrchConfig,
        *,
        environ: Mapping[str, str] = os.environ,
        gh_runner: Runner = run_gh,
        linear_transport: LinearTransport = post_json,
        timeout_seconds: float = TIMEOUT_SECONDS,
    ):
        self.config = config
        self.environ = environ
        self.gh_runner = gh_runner
        self.linear_transport = linear_transport
        self.timeout_seconds = timeout_seconds
        self.github = GitHubReviewActions(
            runner=gh_runner,
            timeout_seconds=timeout_seconds,
        )

    def review_actions_by_repo(self) -> dict[str, ReviewActions]:
        actions: dict[str, ReviewActions] = {}
        for project in self.config.projects:
            if project.linear_project is None:
                action: ReviewActions = GitHubReviewActions(
                    runner=self.gh_runner,
                    timeout_seconds=self.timeout_seconds,
                )
            else:
                action = LinearReviewActions(
                    move_ready_for_review=project.linear_ready_for_review,
                    linear_ready=self._move_linear_ready,
                    runner=self.gh_runner,
                    timeout_seconds=self.timeout_seconds,
                )
            actions[project.repo.lower()] = action
        return actions

    def github_candidates(self, label_prefix: str) -> tuple[TrackedTicket, ...]:
        tickets: list[TrackedTicket] = []
        for project in self.config.projects:
            factory = self._factory(project)
            if factory.tickets != "github":
                continue
            endpoint = f"repos/{project.repo}/issues?state=all&per_page=100"
            for item in self._gh_pages(endpoint):
                for raw in _array(item, "GitHub issue page"):
                    issue = _object(raw, "GitHub issue")
                    if "pull_request" not in issue:
                        continue
                    if not _has_label(issue.get("labels"), label_prefix):
                        continue
                    tickets.append(
                        self._github_ticket(
                            project,
                            factory,
                            str(_positive_int(issue.get("number"), "issue number")),
                        )
                    )
        return tuple(tickets)

    def linear_candidates(self, query: str) -> tuple[TrackedTicket, ...]:
        projects = {
            project.linear_project: project
            for project in self.config.projects
            if project.linear_project is not None
        }
        if not projects:
            return ()
        tickets: list[TrackedTicket] = []
        for issue in self._linear_issues(query):
            project_data = _object(issue.get("project"), "Linear issue project")
            project = projects.get(project_data.get("id")) or projects.get(
                project_data.get("name")
            )
            if project is None:
                continue
            ticket = self._linear_ticket(project, issue)
            if ticket.factory.tickets == "linear":
                tickets.append(ticket)
        return tuple(tickets)

    def tracked_tickets(
        self,
        tickets: tuple[TicketRef, ...],
    ) -> tuple[TrackedTicket, ...]:
        found: list[TrackedTicket] = []
        projects = {project.repo.lower(): project for project in self.config.projects}
        for ref in tickets:
            project = projects.get(ref.repo.lower())
            if project is None:
                continue
            if ref.tracker == "linear":
                matches = [
                    issue
                    for issue in self._linear_issues(ref.ticket_id)
                    if issue.get("identifier") == ref.ticket_id
                    and _matches_linear_project(issue, project)
                ]
                if len(matches) == 1:
                    found.append(self._linear_ticket(project, matches[0]))
                continue
            if ref.tracker != "github":
                continue
            factory = self._factory(project)
            found.append(
                self._github_ticket(project, factory, ref.ticket_id)
            )
        return tuple(found)

    def _linear_issues(self, query: str) -> tuple[dict[str, Any], ...]:
        issues: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            document = self._linear_graphql(
                LINEAR_QUERY,
                {"term": query, "first": 50, "after": after},
            )
            connection = _object(
                _object(document.get("data"), "Linear data").get("searchIssues"),
                "Linear searchIssues",
            )
            issues.extend(
                _object(item, "Linear issue")
                for item in _array(connection.get("nodes"), "Linear issue nodes")
            )
            page = _object(connection.get("pageInfo"), "Linear pageInfo")
            if page.get("hasNextPage") is not True:
                break
            after_value = page.get("endCursor")
            if not isinstance(after_value, str) or not after_value:
                raise PollSourceError("Linear pagination has no endCursor")
            after = after_value
        return tuple(issues)

    def _linear_graphql(
        self,
        query: str,
        variables: Mapping[str, object],
    ) -> dict[str, Any]:
        payload = json.dumps(
            {"query": query, "variables": variables},
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            result = self.linear_transport(
                LINEAR_ENDPOINT,
                {
                    "Authorization": self._linear_key(),
                    "Content-Type": "application/json",
                },
                payload,
                self.timeout_seconds,
            )
        except PollSourceError:
            raise
        except Exception as exc:
            raise PollSourceError(
                f"Linear request failed: {type(exc).__name__}"
            ) from None
        if result.status != 200:
            raise PollSourceError(f"Linear returned HTTP {result.status}")
        document = _json_object(result.body, "Linear")
        if document.get("errors"):
            raise PollSourceError("Linear returned a GraphQL error")
        return document

    def _move_linear_ready(self, ticket: TrackedTicket) -> None:
        if (
            not ticket.linear_ready_for_review
            or ticket.linear_issue_id is None
            or ticket.linear_ready_state_id is None
        ):
            raise PollSourceError("Linear Ready for Review metadata is unavailable")
        document = self._linear_graphql(
            LINEAR_READY_MUTATION,
            {
                "issue": ticket.linear_issue_id,
                "state": ticket.linear_ready_state_id,
            },
        )
        updated = _object(
            _object(document.get("data"), "Linear data").get("issueUpdate"),
            "Linear issueUpdate",
        )
        if updated.get("success") is not True:
            raise PollSourceError("Linear Ready for Review update was not confirmed")

    def _linear_key(self) -> str:
        if self.config.linear is None:
            raise PollSourceError("linear.api_key_env is required for Linear projects")
        key_name = self.config.linear.api_key_env
        key = self.environ.get(key_name)
        if not key:
            raise PollSourceError(f"set {key_name} for Linear polling")
        return key

    def _factory(self, project: Project) -> FactoryConfig:
        repository = _json_object(
            self._gh_call(f"repos/{project.repo}"),
            "GitHub repository",
        )
        default_branch = repository.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise PollSourceError(f"{project.repo} has no default branch")
        factory = self._factory_at(project.repo, default_branch)
        if factory.base_branch != default_branch:
            factory = self._factory_at(project.repo, factory.base_branch)
        return factory

    def _factory_at(self, repo: str, branch: str) -> FactoryConfig:
        content = _json_object(
            self._gh_call(
                "-X",
                "GET",
                f"repos/{repo}/contents/factory.md",
                "-f",
                f"ref={branch}",
            ),
            "GitHub factory.md",
        )
        encoded = content.get("content")
        if content.get("encoding") != "base64" or not isinstance(encoded, str):
            raise PollSourceError(f"{repo} factory.md is not base64 content")
        try:
            compact = "".join(encoded.split())
            text = base64.b64decode(compact, validate=True).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise PollSourceError(f"{repo} factory.md content is invalid") from exc
        factory, findings = parse_factory_md(text)
        errors = [item.message for item in findings if item.severity == "error"]
        if factory is None:
            detail = errors[0] if errors else "invalid factory.md"
            raise PollSourceError(f"{repo} factory.md: {detail}")
        return factory

    def _github_ticket(
        self,
        project: Project,
        factory: FactoryConfig,
        number: str,
    ) -> TrackedTicket:
        pull = _json_object(
            self._gh_call(f"repos/{project.repo}/pulls/{number}"),
            "GitHub pull request",
        )
        head = _object(pull.get("head"), "GitHub pull request head").get("sha")
        if not isinstance(head, str):
            raise PollSourceError(f"{project.repo}#{number} has no head")
        comments = self.github.list_comments(project.repo, number)
        return TrackedTicket(
            repo=project.repo,
            ticket_id=number,
            factory=factory,
            comments=comments,
            current_head=head,
            ci={head: self.github.ci_status(project.repo, head)},
            risk=_risk(comments),
            job_timeout_minutes=resolve_job_timeout(self.config, project.repo),
            ticket_url=_optional_string(pull.get("html_url")),
            git_branch_name=_optional_string(
                _object(pull.get("head"), "GitHub pull request head").get("ref")
            ),
            pr_number=number,
            brief=_brief(comments),
            grok_bot=project.grok_bot,
        )

    def _linear_ticket(
        self,
        project: Project,
        issue: Mapping[str, Any],
    ) -> TrackedTicket:
        identifier = issue.get("identifier")
        if not isinstance(identifier, str) or not identifier:
            raise PollSourceError("Linear issue has no identifier")
        comments_data = _object(issue.get("comments"), "Linear comments")
        linear_comments = tuple(
            _linear_comment(item)
            for item in _array(comments_data.get("nodes"), "Linear comment nodes")
        )
        branch = _optional_string(issue.get("gitBranchName"))
        pr = self._pull_for_branch(project.repo, branch) if branch is not None else None
        pr_number: str | None = None
        current_head: str | None = _latest_head(linear_comments)
        ci = {}
        github_comments: tuple[Comment, ...] = ()
        if pr is not None:
            pr_number = str(_positive_int(pr.get("number"), "pull request number"))
            head = _object(pr.get("head"), "GitHub pull request head").get("sha")
            if not isinstance(head, str):
                raise PollSourceError(f"{project.repo}#{pr_number} has no head")
            current_head = head
            github_comments = self.github.list_comments(project.repo, pr_number)
            ci = {head: self.github.ci_status(project.repo, head)}
        comments = tuple(
            sorted(
                (*linear_comments, *github_comments),
                key=lambda item: (item.created_at, item.id),
            )
        )
        ready_state_id = _linear_ready_state_id(issue)
        return TrackedTicket(
            repo=project.repo,
            ticket_id=identifier,
            factory=self._factory(project),
            comments=comments,
            current_head=current_head,
            ci=ci,
            risk=_risk(comments),
            job_timeout_minutes=resolve_job_timeout(self.config, project.repo),
            ticket_url=_optional_string(issue.get("url")),
            git_branch_name=branch,
            pr_number=pr_number,
            brief=_brief(linear_comments),
            grok_bot=project.grok_bot,
            linear_issue_id=_optional_string(issue.get("id")),
            linear_ready_state_id=ready_state_id,
            linear_ready_for_review=project.linear_ready_for_review,
        )

    def _pull_for_branch(
        self,
        repo: str,
        branch: str,
    ) -> dict[str, Any] | None:
        owner = repo.split("/", 1)[0]
        endpoint = (
            f"repos/{repo}/pulls?state=all&head={owner}:{branch}&per_page=100"
        )
        pulls = [
            _object(item, "GitHub pull request")
            for page in self._gh_pages(endpoint)
            for item in _array(page, "GitHub pull request page")
        ]
        if len(pulls) > 1:
            raise PollSourceError(f"{repo}:{branch} matches more than one pull request")
        return pulls[0] if pulls else None

    def _gh_pages(self, endpoint: str) -> list[object]:
        text = self._gh_call("--paginate", endpoint)
        decoder = json.JSONDecoder()
        pages: list[object] = []
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index == len(text):
                break
            try:
                page, index = decoder.raw_decode(text, index)
            except json.JSONDecodeError as exc:
                raise PollSourceError("gh returned malformed JSON") from exc
            pages.append(page)
        if not pages:
            raise PollSourceError("gh returned no JSON")
        return pages

    def _gh_call(self, *args: str) -> str:
        argv = [*GH, "api", *args]
        try:
            result = self.gh_runner(argv, self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise PollSourceError("gh api timed out") from exc
        except OSError as exc:
            raise PollSourceError(f"gh could not run: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise PollSourceError(f"gh exited {result.returncode}")
        return result.stdout


def _json_object(text: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PollSourceError(f"{source} returned malformed JSON") from exc
    return _object(value, source)


def _object(value: object, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PollSourceError(f"{source} is not an object")
    return value


def _array(value: object, source: str) -> list[object]:
    if not isinstance(value, list):
        raise PollSourceError(f"{source} is not a list")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PollSourceError(f"{field} is not a positive integer")
    return value


def _has_label(value: object, prefix: str) -> bool:
    return any(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].startswith(prefix)
        for item in _array(value, "GitHub labels")
    )


def _matches_linear_project(issue: Mapping[str, Any], project: Project) -> bool:
    configured = project.linear_project
    if configured is None:
        return False
    value = _object(issue.get("project"), "Linear issue project")
    return configured in (value.get("id"), value.get("name"))


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _linear_ready_state_id(issue: Mapping[str, Any]) -> str | None:
    team = issue.get("team")
    if team is None:
        return None
    states = _object(_object(team, "Linear team").get("states"), "Linear states")
    for raw in _array(states.get("nodes"), "Linear state nodes"):
        state = _object(raw, "Linear state")
        if state.get("name") == "Ready for Review":
            return _optional_string(state.get("id"))
    return None


def _linear_comment(value: object) -> Comment:
    item = _object(value, "Linear comment")
    comment_id = item.get("id")
    body = item.get("body")
    created_at = item.get("createdAt")
    user = _object(item.get("user"), "Linear comment user")
    author = user.get("name")
    required = (comment_id, body, created_at, author)
    if not all(isinstance(field, str) and field for field in required):
        raise PollSourceError("Linear comment is missing a required field")
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise PollSourceError("Linear comment createdAt is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PollSourceError("Linear comment createdAt is not ISO-8601")
    return Comment(
        id=comment_id,
        author=author,
        body=body,
        created_at=parsed.timestamp(),
    )


def _risk(comments: Sequence[Comment]) -> str:
    risk = "high"
    for comment in comments:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if "risk" in fields:
            risk = fields["risk"]
    return risk


def _brief(comments: Sequence[Comment]) -> str | None:
    for comment in comments:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if fields["kind"] == "brief":
            return comment.url or comment.body.strip()
    return None


def _latest_head(comments: Sequence[Comment]) -> str | None:
    head: str | None = None
    for comment in comments:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if "head" in fields:
            head = fields["head"]
    return head
