"""ReviewActions on GitHub, run on the factory device under Max's gh login.

Every process call is `env -u GH_TOKEN -u GITHUB_TOKEN -u
GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN gh api ...`, built as an
argv list and passed to run_gh, the one gh seam. gh therefore
uses only the login stored on the device and ignores any of those four
token variables in the environment. The adapter never reads, passes or
logs a token.

Every read goes to GitHub's REST API at call time. There is no local
clone and no cache.

- is_merged: GET pulls/<n>. head.sha must be a full SHA, open or merged.
  A 5xx or timeout on the read is raised with retryable=True, as for merge,
  so a ship's merge preflight gets the same one re-drive.
  True only when merged and head.sha is head.
- branch_tip: GET git/ref/heads/<branch>. The full lowercase SHA, or raise.
- fast_forward: PATCH git/refs/heads/<branch> with sha=<head> and
  force=false. GitHub's 422 "not a fast forward" raises
  NotFastForwardError. It never sends force=true, merges or squashes.
- merge: first the handoff's mark-ready (GET pulls/<n>, and only for a
  draft the GraphQL markPullRequestReadyForReview; issue #83: GitHub
  refuses to merge a draft with HTTP 405), then PUT pulls/<n>/merge with
  merge_method=squash and sha=<head>, the API form of --squash
  --match-head-commit. GitHub's 409 head mismatch raises
  HeadMismatchError. A failure that may pass on a later try (405 not
  mergeable, a 5xx, a timeout, or a mark-ready that did not land) is
  raised with retryable=True, so the dispatcher re-drives it once.
- list_comments and ci_status: GET with --paginate. ci_status counts only
  the latest attempt of each check (issue #84: a rerun at the same head
  replaces the attempt it reran) and is success only with at least one
  successful check run or a success commit status, and nothing failing or
  pending.
- set_state: the orch:state/<state> label. Linear and Aut0 get no labels.
- handoff: GET pulls/<n>, then, only for a draft, the GraphQL mutation
  markPullRequestReadyForReview on the pull's node_id (`gh api -X POST
  graphql`). GitHub has no REST route for draft -> ready (issue #47);
  this is the mutation `gh pr ready` sends. -X POST keeps it out of gh's
  --cache, which also caches POSTs to graphql. A failure before the
  mutation's response confirms isDraft=false sets write_confirmed=False.
  The Linear adapter then moves the issue to Ready for Review only when
  the fresh read shows it still before review; an unknown state fails
  before any write.
- stable_patch_id: GET pulls/<n> for base.ref, then compare/<base>...<head>
  as a diff (the three-dot form diffs head against its merge base, so it
  is the PR's own diff), piped to `git patch-id --stable`. It raises, so
  T4 queues a re-review, on any gh error, an empty diff, a diff naming a
  binary file (GitHub's diff omits binary content, so two heads differing
  only there would match), or a git failure or timeout.
- file_repo_issue and comment_once (issue #259): GET issues?state=all&since=
  (the row's time less an hour, for clock skew) or the PR's comments, and
  return the earlier write when a body carries the marker line; only then
  POST. Only an issue (not a PR) or comment that the gh login (GET user,
  read once) wrote counts, so no one else's text can pose as the write.
  The lookup is a REST list, not the lagging search index, so a retry
  after a crash between the write and its record finds the write.

A GitHub ticket_id is the PR number. Every error is a GitHubAdapterError,
which is a ShipError. Its safe_detail never holds gh's stdout or stderr,
so no response body, URL or token reaches ship_intents.error.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from lib.dispatch.helpers import _base_mismatch
from lib.dispatch.types import (
    CHECK_FAILED,
    BaseCheck,
    Cause,
    CheckRow,
    CIJobStatus,
    PoolRunner,
    RerunOutcome,
    RunJob,
    RunnerKind,
)
from lib.dispatcher import (
    LINEAR_STATE_PHASES,
    AlreadyMergedShip,
    CIResult,
    Comment,
    FailedCheck,
    ReviewActions,
    RetryableShipError,
    RetryableStateError,
    ShipError,
    TicketRef,
    TrackedTicket,
)
from lib.followups import has_marker
from lib.handoff import STATES
from lib.hold import LabelEvent
from lib.pr_title import TITLE_LIMIT
from lib.schema import AUT0_REPO

TOKEN_VARIABLES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
GH = ("env", *(part for name in TOKEN_VARIABLES for part in ("-u", name)), "gh")
TIMEOUT_SECONDS = 60.0
STATE_LABEL_PREFIX = "orch:state/"
PULL_FILES_LIMIT = 3000
GH_DRAIN_SECONDS = 2.0
ANNOTATION_MEMO = 1024
BASE_RUN_PAGES = 3

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
_SHA = re.compile(r"[0-9a-f]{40}")
_BINARY_DIFF = re.compile(r"^(?:Binary files |GIT binary patch)", re.MULTILINE)
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?")
_APPROVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_STATE_LABEL = re.compile(r"orch:state/[a-z][a-z-]*")
_ISO_8601 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_HTTP_STATUS = re.compile(r"\(HTTP ([0-9]{3})\)")
_GRAPHQL_ERROR_TYPE = re.compile(r'"type"\s*:\s*"([A-Z][A-Z_]{0,39})"')
_NODE_ID = re.compile(r"[A-Za-z0-9_=+/-]{4,128}")
_ROUTE_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}")
MARK_READY_QUERY = (
    "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) "
    "{ pullRequest { id isDraft headRefOid } } }"
)

PULL_FINGERPRINT_FIELDS = """
    state merged isDraft updatedAt title
    headRefName headRefOid baseRefName baseRefOid mergeable mergeStateStatus
    labels(first: 100) { totalCount pageInfo { hasNextPage } nodes { name } }
    comments(last: 100) { totalCount nodes { databaseId updatedAt } }
    reviews { totalCount }
    commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) {
      totalCount pageInfo { hasNextPage }
      nodes {
        __typename
        ... on CheckRun { databaseId status conclusion title summary }
        ... on StatusContext { context state }
      }
    } } } } }
"""
PULL_FINGERPRINT_CHUNK = 25

_CHECK_NOT_RUN = ("neutral", "skipped")


LINEAR_NO_READY_STATE_NOTE = "linear-not-moved: team has no Ready for Review state"
LINEAR_NOT_REOPENED_NOTE = (
    "linear-not-reopened: team has no In Progress or other started state "
    "before Ready for Review"
)


class GitHubAdapterError(ShipError):
    """safe_detail defaults to the message, so a message must never hold
    gh's stdout or stderr; a wrapper passes on the inner safe_detail."""

    def __init__(
        self,
        message: str,
        *,
        safe_detail: str | None = None,
        http_status: int | None = None,
        api_message: str | None = None,
        rate_limited: bool = False,
    ):
        super().__init__(message)
        self.safe_detail = message if safe_detail is None else safe_detail
        self.http_status = http_status
        self.api_message = api_message
        self.rate_limited = rate_limited


class NotFastForwardError(GitHubAdapterError):
    pass


class HeadMismatchError(GitHubAdapterError):
    pass


class AlreadyMergedError(GitHubAdapterError, AlreadyMergedShip):
    pass


@dataclass(frozen=True)
class GhResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float], GhResult]


def run_gh(argv: Sequence[str], timeout: float) -> GhResult:
    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        _kill_group(process)
        raise
    return GhResult(process.returncode, _text(stdout), _text(stderr))


def _text(output: bytes) -> str:
    return output.decode("utf-8", errors="replace")


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=GH_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        # A process that left the group with setsid still holds the pipes.
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        process.wait()


def git_patch_id(diff: str, timeout: float) -> str:
    """`git patch-id --stable` of diff. It needs no repository."""
    try:
        completed = subprocess.run(
            ["git", "patch-id", "--stable"],
            input=diff,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubAdapterError(f"git patch-id timed out after {exc.timeout:g}s") from exc
    except OSError as exc:
        raise GitHubAdapterError(f"git could not run: {type(exc).__name__}") from exc
    fields = completed.stdout.split()
    if completed.returncode != 0 or not fields or _SHA.fullmatch(fields[0]) is None:
        raise GitHubAdapterError(f"git patch-id exited {completed.returncode} with no id")
    return fields[0]


def review_actions_for(tickets: str) -> ReviewActions:
    """factory.md's tickets: picks the adapter, as ship: picks ship_mode."""
    if tickets == "github":
        return GitHubReviewActions()
    raise ValueError(f"no ReviewActions adapter for tickets: {tickets}")


class GitHubReviewActions:
    def __init__(
        self,
        runner: Runner = run_gh,
        *,
        timeout_seconds: float = TIMEOUT_SECONDS,
        cleared_variables: Sequence[str] = (),
        cache_ttl: str | None = None,
    ):
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        names = (*TOKEN_VARIABLES, *cleared_variables)
        unique = tuple(dict.fromkeys(names))
        self.gh = (
            "env",
            *(part for name in unique for part in ("-u", name)),
            "gh",
        )
        self.cache_ttl = cache_ttl
        self.login: str | None = None
        self.warning_sink: Callable[[str], None] | None = None
        self._contradictions_logged: set[tuple[str, str, int | str]] = set()
        self._completed_run_annotations: dict[tuple[str, int], tuple[str, ...]] = {}

    def set_state(self, ticket: TrackedTicket, state: str) -> None:
        if state not in STATES:
            raise GitHubAdapterError(f"unknown state {state!r}")
        if ticket.factory.tickets == "linear" or ticket.repo.lower() == AUT0_REPO:
            return
        issue = f"{_repo_path(ticket.repo)}/issues/{_pr_number(ticket)}"
        wanted = f"{STATE_LABEL_PREFIX}{state}"
        current = _labels(self._pages(f"{issue}/labels?per_page=100"))
        if wanted not in current:
            self._call("-X", "POST", f"{issue}/labels", "-f", f"labels[]={wanted}")
        for name in current:
            if name != wanted and _STATE_LABEL.fullmatch(name):
                self._call("-X", "DELETE", f"{issue}/labels/{quote(name, safe='')}")

    def stable_patch_id(self, ticket: TrackedTicket, head: str) -> str:
        head = _sha(head, "head")
        repo = _repo_path(ticket.repo)
        pull = _object(self._json(f"{repo}/pulls/{_pr_number(ticket)}"))
        base = _branch(_object(pull.get("base")).get("ref"))
        diff = self._call(
            "-H",
            "Accept: application/vnd.github.diff",
            f"{repo}/compare/{quote(base, safe='/')}...{head}",
        )
        if not diff.strip():
            raise GitHubAdapterError("the PR diff is empty")
        if _BINARY_DIFF.search(diff):
            raise GitHubAdapterError("the PR diff names a binary file")
        return git_patch_id(diff, self.timeout_seconds)

    def is_merged(self, ticket: TrackedTicket, head: str) -> bool:
        head = _sha(head, "head")
        try:
            read = self._json(f"{_repo_path(ticket.repo)}/pulls/{_pr_number(ticket)}")
        except GitHubAdapterError as exc:
            exc.retryable = _transient(exc)
            raise
        pull = _object(read)
        merged = pull.get("merged")
        if not isinstance(merged, bool):
            raise GitHubAdapterError("pull request has no boolean merged field")
        pr_head = _sha(_object(pull.get("head")).get("sha"), "pull request head")
        return merged and pr_head == head

    def merge(
        self,
        ticket: TrackedTicket,
        *,
        squash: bool,
        match_head_commit: str,
    ) -> None:
        if not squash:
            raise GitHubAdapterError("only squash merges are built")
        head = _sha(match_head_commit, "match_head_commit")
        endpoint = f"{_repo_path(ticket.repo)}/pulls/{_pr_number(ticket)}/merge"
        try:
            self._mark_ready(ticket, head)
        except HeadMismatchError:
            raise
        except GitHubAdapterError as exc:
            # Nothing merged; the mark-ready read or write did not land.
            exc.retryable = not _refusal(exc)
            raise
        try:
            result = _object(
                self._json(
                    "-X",
                    "PUT",
                    endpoint,
                    "-f",
                    "merge_method=squash",
                    "-f",
                    f"sha={head}",
                )
            )
        except GitHubAdapterError as exc:
            status = _status(exc)
            if status == 409:
                raise HeadMismatchError(
                    f"PR head is not {head}: {exc.safe_detail}"
                ) from exc
            exc.retryable = status == 405 or _transient(exc)
            raise
        if result.get("merged") is not True:
            raise GitHubAdapterError(f"squash merge of {head} was not confirmed")

    def fast_forward(
        self,
        ticket: TrackedTicket,
        *,
        branch: str,
        head: str,
    ) -> None:
        head = _sha(head, "head")
        endpoint = f"{_repo_path(ticket.repo)}/git/refs/heads/{_branch(branch)}"
        try:
            ref = _object(
                self._json(
                    "-X",
                    "PATCH",
                    endpoint,
                    "-f",
                    f"sha={head}",
                    "-F",
                    "force=false",
                )
            )
        except GitHubAdapterError as exc:
            if _status(exc) == 422 and "fast forward" in str(exc).lower():
                raise NotFastForwardError(
                    f"{head} is not a fast-forward of {branch}: {exc.safe_detail}"
                ) from exc
            raise
        moved = _ref_sha(ref, branch)
        if moved != head:
            raise GitHubAdapterError(f"{branch} moved to {moved}, not {head}")

    def branch_tip(self, ticket: TrackedTicket, branch: str) -> str:
        endpoint = f"{_repo_path(ticket.repo)}/git/ref/heads/{_branch(branch)}"
        return _ref_sha(_object(self._json(endpoint)), branch)

    def commits_ahead(self, ticket: TrackedTicket, *, base: str, head: str) -> int:
        base = quote(_branch(base), safe="/")
        compare = f"{_repo_path(ticket.repo)}/compare/{base}...{_sha(head, 'head')}"
        ahead = _object(self._json(compare)).get("ahead_by")
        if type(ahead) is not int:
            raise GitHubAdapterError("the compare response has no ahead_by count")
        return ahead

    def reopen(self, ticket: TrackedTicket) -> None:
        self._call(
            "-X",
            "PATCH",
            f"{_repo_path(ticket.repo)}/pulls/{_pr_number(ticket)}",
            "-f",
            "state=open",
        )

    def handoff(self, ticket: TrackedTicket, head: str) -> str | None:
        self._mark_ready(ticket, head)
        return None

    def reopen_linear(self, ticket: TrackedTicket) -> str | None:
        del ticket
        return None

    def _mark_ready(self, ticket: TrackedTicket, head: str) -> bool:
        """Mark the PR ready if it is a draft. True when this call confirmed it.

        Every error raised before GitHub's response confirms isDraft=false
        has write_confirmed=False: nothing this call sent is known to have
        landed, and a retry re-reads the pull first.
        """
        try:
            head = _sha(head, "head")
            pull = _object(
                self._json(f"{_repo_path(ticket.repo)}/pulls/{_pr_number(ticket)}")
            )
            current = _sha(_object(pull.get("head")).get("sha"), "pull request head")
            if current != head:
                raise HeadMismatchError(f"PR head is {current}, not {head}")
            if pull.get("merged") is True:
                raise AlreadyMergedError("cannot hand off a merged pull request")
            if pull.get("state") != "open":
                raise GitHubAdapterError(
                    "cannot hand off a pull request that is not open"
                )
            if pull.get("draft") is not True:
                return False
            node_id = pull.get("node_id")
            if not isinstance(node_id, str) or _NODE_ID.fullmatch(node_id) is None:
                raise GitHubAdapterError("pull request has no valid node_id")
            response = _object(
                self._json(
                    "-X",
                    "POST",
                    "graphql",
                    "-f",
                    f"query={MARK_READY_QUERY}",
                    "-f",
                    f"id={node_id}",
                    "-f",
                    f"owner={ticket.repo.partition('/')[0]}",
                )
            )
            if response.get("errors"):
                raise GitHubAdapterError(
                    "markPullRequestReadyForReview returned errors",
                    safe_detail=(
                        "markPullRequestReadyForReview returned errors "
                        + _graphql_detail(json.dumps(response))
                    ).rstrip(),
                )
            ready = _object(
                _object(
                    _object(response.get("data")).get("markPullRequestReadyForReview")
                ).get("pullRequest")
            )
            if ready.get("isDraft") is not False:
                raise GitHubAdapterError("GitHub did not confirm the PR is ready")
        except GitHubAdapterError as exc:
            exc.write_confirmed = False
            raise
        if ready.get("headRefOid") != head:
            raise HeadMismatchError(
                f"PR was marked ready at {ready.get('headRefOid')!r}, not {head}"
            )
        return True

    def await_approver(
        self,
        ticket: TrackedTicket,
        head: str,
        approver: str,
    ) -> None:
        head = _sha(head, "head")
        if _APPROVER.fullmatch(approver) is None:
            raise GitHubAdapterError(f"approver {approver!r} is not a name")
        issue = f"{_repo_path(ticket.repo)}/issues/{_pr_number(ticket)}"
        body = (
            f"Preview for {approver}: head {head}. Waiting for {approver}'s "
            "ok on this exact SHA, or a decline."
        )
        self._call("-X", "POST", f"{issue}/comments", "-f", f"body={body}")

    def set_title(self, ticket: TrackedTicket, title: str) -> None:
        """The REST write `gh pr edit --title` makes (issue #115)."""
        if not title or len(title) > TITLE_LIMIT:
            raise GitHubAdapterError(f"a PR title is 1 to {TITLE_LIMIT} characters")
        self._call(
            "-X",
            "PATCH",
            f"{_repo_path(ticket.repo)}/pulls/{_pr_number(ticket)}",
            "-f",
            f"title={title}",
        )

    def file_issue(
        self,
        ticket: TrackedTicket,
        title: str,
        body: str,
        labels: tuple[str, ...],
        marker: str,
        since: float,
    ) -> int:
        return self.file_repo_issue(ticket.repo, title, body, labels, marker, since)

    def file_repo_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: tuple[str, ...],
        marker: str,
        since: float,
        *,
        before_post: Callable[[], None] | None = None,
    ) -> int:
        """The marked issue, posting it first when there is none. before_post
        runs between the marker search and the POST and may raise to stop it."""
        found = self.marked_issues(repo, marker, since)
        if found:
            return found[0]
        if before_post is not None:
            before_post()
        args = ["-X", "POST", _repo_path(repo) + "/issues", "-f", f"title={title}", "-f", f"body={body}"]
        for label in labels:
            args.extend(("-f", f"labels[]={label}"))
        return _issue_number(_object(self._json(*args)).get("number"))

    def marked_issues(self, repo: str, marker: str, since: float) -> tuple[int, ...]:
        """This login's issues (not PRs) carrying marker, lowest number first."""
        after = datetime.fromtimestamp(since - 3600, tz=timezone.utc)
        listing = f"{_repo_path(repo)}/issues?state=all&since={after:%Y-%m-%dT%H:%M:%SZ}&per_page=100"
        found: set[int] = set()
        for page in self._pages(listing):
            if not isinstance(page, list):
                raise GitHubAdapterError("issue page is not a list")
            for item in page:
                raw = _object(item)
                body_text = raw.get("body")
                if (
                    isinstance(body_text, str)
                    and has_marker(body_text, marker)
                    and "pull_request" not in raw
                    and _object(raw.get("user")).get("login") == self._login()
                ):
                    found.add(_issue_number(raw.get("number")))
        return tuple(sorted(found))

    def close_issue(
        self, repo: str, number: int, comment: str | None, *, completed: bool = False
    ) -> None:
        if comment is not None:
            self.post_issue_comment(repo, number, comment)
        reason = ("-f", "state_reason=completed") if completed else ()
        path = f"{_repo_path(repo)}/issues/{_issue_number(number)}"
        self._call("-X", "PATCH", path, "-f", "state=closed", *reason)

    def remove_issue_label(self, repo: str, number: int, label: str) -> None:
        try:
            self._call(
                "-X",
                "DELETE",
                f"{_repo_path(repo)}/issues/{_issue_number(number)}/labels/{quote(label, safe='')}",
            )
        except GitHubAdapterError as exc:
            if _status(exc) != 404:
                raise

    def issue_state(self, repo: str, number: int) -> str:
        state = _object(self._json(f"{_repo_path(repo)}/issues/{_issue_number(number)}")).get("state")
        if not isinstance(state, str):
            raise GitHubAdapterError("issue state is not readable")
        return state

    def comment_once(self, ticket: TrackedTicket, body: str, marker: str) -> None:
        self._comment_once(ticket.repo, _pr_number(ticket), body, marker)

    def comment_on_issue_once(
        self, ticket: TrackedTicket, number: int, body: str, marker: str
    ) -> None:
        self._comment_once(ticket.repo, str(_issue_number(number)), body, marker)

    def open_issue_labels(self, ticket: TrackedTicket, number: int) -> tuple[str, ...] | None:
        raw = _object(self._json(f"{_repo_path(ticket.repo)}/issues/{_issue_number(number)}"))
        if raw.get("state") != "open":
            return None
        return tuple(sorted(_labels([raw.get("labels")])))

    def _comment_once(self, repo: str, number: str, body: str, marker: str) -> None:
        issue = int(_number(number))
        if not self.issue_has_marker(repo, issue, marker):
            self.post_issue_comment(repo, issue, body)

    def issue_has_marker(self, repo: str, number: int, marker: str) -> bool:
        return any(
            has_marker(comment.body, marker) and comment.author == self._login()
            for comment in self.list_comments(repo, str(_issue_number(number)))
        )

    def post_issue_comment(self, repo: str, number: int, body: str) -> None:
        self._call(
            "-X", "POST", f"{_repo_path(repo)}/issues/{_issue_number(number)}/comments",
            "-f", f"body={body}",
        )

    def post_issue_comment_once(self, repo: str, number: int, body: str, marker: str) -> str:
        """The id of this login's comment carrying marker, posting body first when there is none."""
        for comment in self.list_comments(repo, str(_issue_number(number))):
            if has_marker(comment.body, marker) and comment.author == self._login():
                return comment.id
        posted = _object(
            self._json(
                "-X", "POST", f"{_repo_path(repo)}/issues/{_issue_number(number)}/comments",
                "-f", f"body={body}",
            )
        )
        return str(posted["id"])

    def _login(self) -> str:
        if self.login is None:
            login = _object(self._json("user")).get("login")
            if not isinstance(login, str) or _LOGIN.fullmatch(login) is None:
                raise GitHubAdapterError("gh user has no valid login")
            self.login = login
        return self.login

    def list_comments(self, repo: str, number: str) -> tuple[Comment, ...]:
        """Every issue comment, all pages, ordered by (created_at, int id)."""
        endpoint = (
            f"{_repo_path(repo)}/issues/{_number(number)}/comments?per_page=100"
        )
        comments: list[Comment] = []
        seen: set[int] = set()
        for page in self._pages(endpoint):
            if not isinstance(page, list):
                raise GitHubAdapterError("comment page is not a list")
            for item in page:
                comment = _comment(item)
                if int(comment.id) in seen:
                    raise GitHubAdapterError(f"comment {comment.id} is listed twice")
                seen.add(int(comment.id))
                comments.append(comment)
        return tuple(sorted(comments, key=lambda item: (item.created_at, int(item.id))))

    def pull_fingerprints(self, repo: str, numbers: Sequence[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for chunk, pulls, broken in self._pull_batches(repo, numbers, PULL_FINGERPRINT_FIELDS):
            for number in chunk:
                pull = pulls.get(f"p{number}")
                whole = isinstance(pull, dict) and _whole_fingerprint(pull)
                if whole and f"p{number}" not in broken:
                    found[number] = json.dumps(pull, sort_keys=True, separators=(",", ":"))
        return found

    def pull_states(self, repo: str, numbers: Sequence[str]) -> dict[str, str]:
        """Each observed number's PR state: OPEN, CLOSED or MERGED. A number
        missing from the result was not observed, never assumed open."""
        states: dict[str, str] = {}
        for chunk, pulls, broken in self._pull_batches(repo, numbers, "state"):
            for number in chunk:
                pull = pulls.get(f"p{number}")
                if f"p{number}" not in broken and isinstance(pull, dict):
                    state = pull.get("state")
                    if isinstance(state, str):
                        states[number] = state
        return states

    def _pull_batches(
        self, repo: str, numbers: Sequence[str], fields: str
    ) -> Iterator[tuple[list[str], dict[str, Any], set[str | None]]]:
        owner, _, name = _repo_path(repo).removeprefix("repos/").partition("/")
        wanted = list(dict.fromkeys(_number(number) for number in numbers))
        for start in range(0, len(wanted), PULL_FINGERPRINT_CHUNK):
            chunk = wanted[start : start + PULL_FINGERPRINT_CHUNK]
            aliases = " ".join(
                f"p{number}: pullRequest(number: {number}) {{ ...pull }}" for number in chunk
            )
            query = (
                "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) "
                f"{{ {aliases} }} }} fragment pull on PullRequest {{ {fields} }}"
            )
            document = _object(
                self._json(
                    "-X", "POST", "graphql",
                    "-f", f"query={query}",
                    "-f", f"owner={owner}",
                    "-f", f"name={name}",
                    partial_graphql=True,
                )
            )
            pulls = _object(document.get("data")).get("repository")
            broken = _error_aliases(document)
            if isinstance(pulls, dict) and None not in broken:
                yield chunk, pulls, broken

    def pull_labels(self, repo: str, number: str) -> tuple[str, ...]:
        """The PR's labels, all pages (issue #86 hold label)."""
        endpoint = f"{_repo_path(repo)}/issues/{_number(number)}/labels?per_page=100"
        return tuple(sorted(_labels(self._pages(endpoint))))

    def pull_head(self, repo: str, number: str) -> str:
        pull = _object(self._json(f"{_repo_path(repo)}/pulls/{_number(number)}"))
        return _sha(_object(pull.get("head")).get("sha"), "pull request head")

    def pull_files(self, repo: str, number: str) -> tuple[str, ...]:
        """Every path the PR's diff against base touches, all pages, with a
        rename's old path too (issue #86 hold paths). GitHub lists at most
        3000 files; a PR at that limit is refused rather than read short."""
        endpoint = f"{_repo_path(repo)}/pulls/{_number(number)}/files?per_page=100"
        paths: set[str] = set()
        count = 0
        for page in self._pages(endpoint):
            if not isinstance(page, list):
                raise GitHubAdapterError("pull request files page is not a list")
            for item in page:
                raw = _object(item)
                count += 1
                for key in ("filename", "previous_filename"):
                    value = raw.get(key)
                    if value is None and key == "previous_filename":
                        continue
                    if not isinstance(value, str) or not value:
                        raise GitHubAdapterError(f"pull request file has no {key}")
                    paths.add(value)
        if count >= PULL_FILES_LIMIT:
            raise GitHubAdapterError("pull request lists 3000 or more files")
        return tuple(sorted(paths))

    def label_events(self, repo: str, number: str) -> tuple[LabelEvent, ...]:
        """The PR's labeled and unlabeled events, all pages (issue #86)."""
        endpoint = f"{_repo_path(repo)}/issues/{_number(number)}/events?per_page=100"
        events: list[LabelEvent] = []
        for page in self._pages(endpoint):
            if not isinstance(page, list):
                raise GitHubAdapterError("issue events page is not a list")
            for item in page:
                raw = _object(item)
                action = raw.get("event")
                if action not in ("labeled", "unlabeled"):
                    continue
                name = _object(raw.get("label")).get("name")
                actor = _object(raw.get("actor")).get("login")
                if not isinstance(name, str) or not name:
                    raise GitHubAdapterError("label event has no label name")
                if not isinstance(actor, str) or _LOGIN.fullmatch(actor) is None:
                    raise GitHubAdapterError("label event has no valid actor login")
                events.append(
                    LabelEvent(
                        id=str(_comment_id(raw.get("id"))),
                        action=str(action),
                        label=name,
                        actor=actor,
                        created_at=_created_at(raw.get("created_at")),
                    )
                )
        return tuple(events)

    def committed_at(self, repo: str, sha: str) -> float:
        commit = _object(self._json(f"{_repo_path(repo)}/commits/{_sha(sha, 'commit')}"))
        committer = _object(_object(commit.get("commit")).get("committer"))
        return _created_at(committer.get("date"))

    def ci_status(self, repo: str, head: str) -> CIResult:
        """Check runs and commit statuses on head.

        success needs at least one check run that concluded success, or a
        success combined status, and nothing failing or pending. All
        neutral or skipped, nothing reported, or runs received that do not
        match total_count on every page is pending. Nothing reported at
        all is pending with reported=False (#255).

        A short read is pending even when a run in view failed: a rerun on
        an unread page may supersede that failure, so pending is the safe
        answer, at the cost of one poll.

        A run is pending only with no conclusion; a conclusion decides it
        even when status is not completed (#720).
        """
        from lib.evidence.ci import classify_failure

        head = _sha(head, "head")
        commit = f"{_repo_path(repo)}/commits/{head}"
        runs: list[dict[str, Any]] = []
        counted: set[int] = set()
        for page in self._pages(f"{commit}/check-runs?per_page=100"):
            listed = _object(page).get("check_runs")
            if not isinstance(listed, list):
                raise GitHubAdapterError("check-runs page has no check_runs list")
            counted.add(_count(_object(page).get("total_count"), "check-runs"))
            runs.extend(_object(item) for item in listed)
        combined = _object(self._json(f"{commit}/status"))
        total = _count(combined.get("total_count"), "combined status")
        if counted != {len(runs)}:
            return CIResult("pending")
        if not runs and not total:
            return CIResult("pending", reported=False)
        runs = _latest_attempts(runs)
        checks = tuple(map(_check_row, runs))
        statuses = _status_rows(combined)
        failed: list[FailedCheck] = []
        pending: list[str] = []
        passed = False
        for run in runs:
            name = str(run.get("name"))
            status = run.get("status")
            conclusion = run.get("conclusion")
            if conclusion is None and status != "completed":
                pending.append(name)
                continue
            if status != "completed":
                self._warn_contradiction(repo, head, run, name, status, conclusion)
            if conclusion in CHECK_FAILED:
                cause, signal = classify_failure(conclusion, self._failure_texts(repo, run))
                failed.append(_failed_check(name, run.get("details_url"), cause, signal))
            elif conclusion == "success":
                passed = True
            elif conclusion not in _CHECK_NOT_RUN:
                raise GitHubAdapterError(f"check run {name} concluded {conclusion!r}")
        if total:
            state = combined.get("state")
            if state in ("failure", "error"):
                cause, signal = classify_failure(state, ())
                failed.append(FailedCheck("commit status", cause=cause, signal=signal))
            elif state == "pending":
                pending.extend(_pending_contexts(combined) or ["commit status"])
            elif state == "success":
                passed = True
            else:
                raise GitHubAdapterError(f"combined status is {state!r}")
        if failed:
            return CIResult(
                "failure",
                "failed: " + ", ".join(check.name for check in failed),
                failed=tuple(failed),
                pending=tuple(pending),
                checks=checks,
                statuses=statuses,
            )
        return CIResult(
            "success" if passed and not pending else "pending",
            pending=tuple(pending),
            checks=checks,
            statuses=statuses,
        )

    def _failure_texts(self, repo: str, run: dict[str, Any]) -> list[str]:
        output = run.get("output")
        output = output if isinstance(output, dict) else {}
        texts = [text for text in (output.get("title"), output.get("summary")) if isinstance(text, str)]
        run_id = run.get("id")
        count = output.get("annotations_count")
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            return texts
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return texts
        key = (repo.lower(), run_id)
        annotations = self._completed_run_annotations.get(key)
        if annotations is None:
            annotations = self._read_failure_annotations(repo, run_id)
            if annotations is None:
                return texts
            if run.get("status") == "completed":
                if len(self._completed_run_annotations) >= ANNOTATION_MEMO:
                    del self._completed_run_annotations[next(iter(self._completed_run_annotations))]
                self._completed_run_annotations[key] = annotations
        return [*texts, *annotations]

    def _read_failure_annotations(self, repo: str, run_id: int) -> tuple[str, ...] | None:
        try:
            pages = self._pages(f"{_repo_path(repo)}/check-runs/{run_id}/annotations?per_page=100")
        except GitHubAdapterError:
            return None
        if not all(isinstance(page, list) for page in pages):
            return None
        return tuple(
            text
            for page in pages
            for item in page
            if isinstance(item, dict) and item.get("annotation_level") == "failure"
            for text in (item.get("title"), item.get("message"))
            if isinstance(text, str) and text
        )

    def _warn_contradiction(
        self, repo: str, head: str, run: dict[str, Any], name: str, status: Any, conclusion: Any
    ) -> None:
        run_id = run.get("id")
        key = (repo, head, run_id if isinstance(run_id, int) else name)
        if self.warning_sink is None or key in self._contradictions_logged:
            return
        self._contradictions_logged.add(key)
        self.warning_sink(
            f"check run {name} reports status={status} with conclusion={conclusion}; using the conclusion"
        )

    def failed_run_logs(self, repo: str, run_id: str) -> dict[str, str]:
        if not run_id.isdigit():
            raise GitHubAdapterError("Actions run id is not a number")
        _repo_path(repo)
        text = self._gh(
            ("run", "view", run_id, "--repo", repo, "--log-failed"), route="gh run view --log-failed"
        )
        logs: dict[str, list[str]] = {}
        for line in text.splitlines():
            job, _, rest = line.partition("\t")
            _, _, entry = rest.partition("\t")
            logs.setdefault(job, []).append(entry)
        return {job: "\n".join(lines) for job, lines in logs.items()}

    def run_jobs(self, repo: str, run_id: str) -> tuple[RunJob, ...]:
        """The jobs of the run's latest attempt, one page of 100."""
        if not run_id.isdigit():
            raise GitHubAdapterError("Actions run id is not a number")
        page = _object(self._json(f"{_repo_path(repo)}/actions/runs/{run_id}/jobs?per_page=100"))
        jobs = page.get("jobs")
        if not isinstance(jobs, list):
            raise GitHubAdapterError("run jobs page has no jobs list")
        return tuple(_run_job(_object(job)) for job in jobs)

    def list_runners(self, repo: str) -> tuple[PoolRunner, ...]:
        page = _object(
            self._json(f"{_repo_path(repo)}/actions/runners?per_page=100", cache_ttl="60s")
        )
        runners = page.get("runners")
        if not isinstance(runners, list):
            raise GitHubAdapterError("runners page has no runners list")
        return tuple(_pool_runner(_object(runner)) for runner in runners)

    def job_log(self, repo: str, job_id: str) -> str:
        """gh 2.97+ exits 1 on a body with escape sequences unless allowed,
        and pytest's output is coloured."""
        if not job_id.isdigit():
            raise GitHubAdapterError("Actions job id is not a number")
        return self._call(
            f"{_repo_path(repo)}/actions/jobs/{job_id}/logs", "--allow-escape-sequences"
        )

    def base_checks(self, repo: str, branch: str) -> dict[str, BaseCheck]:
        path = _repo_path(repo)
        latest: dict[object, tuple[int, str]] = {}
        for number in range(1, BASE_RUN_PAGES + 1):
            page = _object(
                self._json(
                    f"{path}/actions/runs?branch={quote(_branch(branch), safe='')}"
                    f"&event=push&status=completed&per_page=100&page={number}"
                )
            )
            runs = page.get("workflow_runs")
            if not isinstance(runs, list):
                raise GitHubAdapterError("workflow runs page has no workflow_runs list")
            for run in map(_object, runs):
                run_id = run.get("id")
                if isinstance(run_id, bool) or not isinstance(run_id, int):
                    raise GitHubAdapterError("workflow run has no numeric id")
                head = run.get("head_sha")
                if not isinstance(head, str):
                    raise GitHubAdapterError("workflow run has no head sha")
                workflow = run.get("workflow_id")
                latest[workflow] = max(latest.get(workflow, (run_id, head)), (run_id, head))
            if len(runs) < 100:
                break
        checks: dict[str, BaseCheck] = {}
        for run_id, head in sorted(latest.values(), reverse=True):
            for job in map(_object, self._run_jobs(f"{path}/actions/runs/{run_id}/jobs")):
                job_id, name, conclusion = job.get("id"), str(job.get("name")), job.get("conclusion")
                if isinstance(job_id, bool) or not isinstance(job_id, int):
                    raise GitHubAdapterError("run job has no numeric id")
                checks.setdefault(
                    name,
                    BaseCheck(
                        name,
                        conclusion if isinstance(conclusion, str) else None,
                        str(run_id),
                        str(job_id),
                        head,
                    ),
                )
        return checks

    def _run_jobs(self, endpoint: str) -> list[object]:
        jobs: list[object] = []
        number = 1
        while True:
            page = _object(self._json(f"{endpoint}?filter=latest&per_page=100&page={number}"))
            listed = page.get("jobs")
            if not isinstance(listed, list):
                raise GitHubAdapterError("run jobs page has no jobs list")
            jobs.extend(listed)
            if len(listed) < 100:
                return jobs
            number += 1

    def rerun_failed_jobs(self, repo: str, run_ids: Sequence[str]) -> RerunOutcome:
        if not all(run_id.isdigit() for run_id in run_ids):
            raise GitHubAdapterError("Actions run id is not a number")
        if not all(self._run_completed(repo, run_id) for run_id in run_ids):
            return "running"
        requested = False
        for run_id in run_ids:
            try:
                self._call(
                    "-X", "POST", f"{_repo_path(repo)}/actions/runs/{run_id}/rerun-failed-jobs"
                )
            except GitHubAdapterError as exc:
                already = exc.http_status == 403 and "already running" in (
                    exc.api_message or ""
                ).lower()
                if not already and self._run_completed(repo, run_id):
                    raise
            else:
                requested = True
        return "requested" if requested else "running"

    def _run_completed(self, repo: str, run_id: str) -> bool:
        endpoint = f"{_repo_path(repo)}/actions/runs/{run_id}"
        try:
            text = self._gh(("api", "--include", endpoint), route=f"gh api {_route((endpoint,))}")
            run = _object(json.loads(text))
        except json.JSONDecodeError:
            return False
        except GitHubAdapterError as exc:
            if _transient(exc) or exc.rate_limited:
                return False
            raise
        return run.get("status") == "completed"

    def _call(
        self, *args: str, cache_ttl: str | None = None, partial_graphql: bool = False
    ) -> str:
        """One gh api call. cache_ttl caches this call even when it is a
        GraphQL query (gh caches POSTs to graphql too), so pass it only for
        a read. partial_graphql returns GraphQL data that came back with
        errors (gh exits 1 then); the caller checks the errors."""
        argv = ["api"]
        if cache_ttl is not None:
            argv.extend(("--cache", cache_ttl))
        elif self.cache_ttl is not None and _is_read(args):
            argv.extend(("--cache", self.cache_ttl))
        argv.extend(args)
        return self._gh(argv, route=f"gh api {_route(args)}", partial_graphql=partial_graphql)

    def _gh(self, args: Sequence[str], *, route: str, partial_graphql: bool = False) -> str:
        try:
            result = self.runner([*self.gh, *args], self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            # The runner may use its own timeout (the poller runs writes at
            # 60 s), so report the one the timed-out call actually had.
            raise GitHubAdapterError(
                f"gh api timed out after {exc.timeout:g}s",
                safe_detail=f"{route} timed out after {exc.timeout:g}s",
            ) from exc
        except OSError as exc:
            raise GitHubAdapterError(
                f"gh could not run: {type(exc).__name__}",
                safe_detail=f"{route} could not run: {type(exc).__name__}",
            ) from exc
        if result.returncode != 0 and partial_graphql and _has_graphql_data(result.stdout):
            return result.stdout
        body = _without_headers(result.stdout) if "--include" in args else result.stdout
        if result.returncode != 0:
            detail = " ".join(f"{result.stderr} {body}".split())[:500]
            safe = f"{route} exited {result.returncode}"
            found = _HTTP_STATUS.search(result.stderr)
            status = int(found.group(1)) if found is not None else None
            if status is not None:
                safe += f" (HTTP {status})"
            graphql = _graphql_detail(body)
            if graphql:
                safe += f" {graphql}"
            raise GitHubAdapterError(
                f"gh exited {result.returncode}: {detail or 'no output'}",
                safe_detail=safe,
                http_status=status,
                api_message=_api_message(body),
                rate_limited=rate_limited(status, result.stdout, result.stderr),
            )
        return body

    def _json(
        self, *args: str, cache_ttl: str | None = None, partial_graphql: bool = False
    ) -> object:
        text = self._call(*args, cache_ttl=cache_ttl, partial_graphql=partial_graphql)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitHubAdapterError(
                f"gh returned malformed JSON: {exc}",
                safe_detail=f"gh api {_route(args)} returned malformed JSON",
            ) from exc

    def _pages(self, endpoint: str) -> list[object]:
        """gh api --paginate prints one JSON document per page, back to back."""
        text = self._call("--paginate", endpoint)
        decoder = json.JSONDecoder()
        pages: list[object] = []
        index = 0
        while True:
            while index < len(text) and text[index].isspace():
                index += 1
            if index == len(text):
                break
            try:
                page, index = decoder.raw_decode(text, index)
            except json.JSONDecodeError as exc:
                raise GitHubAdapterError(f"gh returned malformed JSON: {exc}") from exc
            pages.append(page)
        if not pages:
            raise GitHubAdapterError("gh returned no JSON")
        return pages


class LinearReviewActions(GitHubReviewActions):
    """GitHub PR actions plus the one explicitly enabled Linear ship write."""

    def __init__(
        self,
        *,
        move_ready_for_review: bool,
        linear_ready: Callable[[TrackedTicket], None],
        linear_reopen: Callable[[TrackedTicket], None],
        fresh_ticket: Callable[[TicketRef], TrackedTicket | None] | None = None,
        runner: Runner = run_gh,
        timeout_seconds: float = TIMEOUT_SECONDS,
        cleared_variables: Sequence[str] = (),
        cache_ttl: str | None = None,
    ):
        super().__init__(
            runner=runner,
            timeout_seconds=timeout_seconds,
            cleared_variables=cleared_variables,
            cache_ttl=cache_ttl,
        )
        self.move_ready_for_review = move_ready_for_review
        self.linear_ready = linear_ready
        self.linear_reopen = linear_reopen
        self.fresh_ticket = fresh_ticket

    def set_state(self, ticket: TrackedTicket, state: str) -> None:
        del ticket, state

    def merge(
        self,
        ticket: TrackedTicket,
        *,
        squash: bool,
        match_head_commit: str,
    ) -> None:
        del ticket, squash, match_head_commit
        raise GitHubAdapterError("Linear tickets can only use handoff")

    def fast_forward(
        self,
        ticket: TrackedTicket,
        *,
        branch: str,
        head: str,
    ) -> None:
        del ticket, branch, head
        raise GitHubAdapterError("Linear tickets can only use handoff")

    def handoff(self, ticket: TrackedTicket, head: str) -> str | None:
        if self.fresh_ticket is not None:
            try:
                fresh = self.fresh_ticket(
                    TicketRef(
                        repo=ticket.repo,
                        ticket_id=ticket.ticket_id,
                        tracker="linear",
                    )
                )
            except RetryableStateError as exc:
                raise RetryableShipError(str(exc)) from None
            if fresh is None:
                error = GitHubAdapterError("linked Linear ticket is unavailable")
                error.write_confirmed = False
                raise error
            if fresh.pr_number != ticket.pr_number:
                error = GitHubAdapterError("linked pull request changed before handoff")
                error.write_confirmed = False
                raise error
            mismatch = _base_mismatch(fresh)
            if mismatch is not None:
                raise RetryableShipError(mismatch)
            ticket = fresh
        if (
            self.move_ready_for_review
            and ticket.linear_state_phase not in LINEAR_STATE_PHASES
        ):
            error = GitHubAdapterError("Linear issue state is unavailable")
            error.write_confirmed = False
            raise error
        marked = self._mark_ready(ticket, head)
        if (
            self.move_ready_for_review
            and ticket.linear_state_phase == "no-ready-state"
        ):
            return LINEAR_NO_READY_STATE_NOTE
        if self.move_ready_for_review and ticket.linear_state_phase in (
            "before-review",
            "rework",
        ):
            try:
                self.linear_ready(ticket)
            except RetryableStateError as exc:
                error = GitHubAdapterError(
                    "Linear handoff update was not confirmed: "
                    f"{type(exc).__name__}"
                )
                error.write_confirmed = marked
                raise error from None
        return None

    def reopen_linear(self, ticket: TrackedTicket) -> str | None:
        if not self.move_ready_for_review:
            return None
        if ticket.linear_reopen_state_id is None:
            return LINEAR_NOT_REOPENED_NOTE
        self.linear_reopen(ticket)
        return None


def _latest_attempts(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the latest attempt of each check (issue #84).

    A rerun at the same head adds a new check run with the same name, from
    the same app, in the same workflow run. Only such a rerun supersedes an
    earlier attempt; the latest is the one that started last (then the
    higher id). Two workflows that share a job name are distinct checks
    (#87 F1.1), and a run with no workflow identity supersedes nothing, so a
    failure there always keeps the merge gate.
    """
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for run in runs:
        app = _object(run.get("app")) if isinstance(run.get("app"), dict) else {}
        key = (
            str(app.get("id", app.get("slug", ""))),
            _workflow_identity(run),
            str(run.get("name")),
        )
        current = latest.get(key)
        if current is None or _attempt_order(run) > _attempt_order(current):
            latest[key] = run
    return list(latest.values())


_ACTIONS_RUN = re.compile(r"/actions/runs/([0-9]+)(?:/|$)")
_ACTIONS_JOB = re.compile(
    r"^(https://github\.com/[^/\s]+/[^/\s]+/actions/runs/([0-9]+))/job/([0-9]+)(?:[/?#]|$)"
)


def _pending_contexts(combined: dict[str, Any]) -> list[str]:
    statuses = combined.get("statuses")
    if not isinstance(statuses, list):
        return []
    return [
        str(status.get("context"))
        for status in map(_object, statuses)
        if status.get("state") == "pending"
    ]


def _status_rows(combined: dict[str, Any]) -> tuple[CheckRow, ...]:
    statuses = combined.get("statuses")
    if not isinstance(statuses, list):
        return ()
    return tuple(
        CheckRow(
            name=str(status.get("context")),
            status="completed",
            conclusion=str(status.get("state")),
            started_at=_display_time(status.get("created_at")),
        )
        for status in map(_object, statuses)
    )


def _failed_check(name: str, details: object, cause: Cause, signal: str | None) -> FailedCheck:
    match = _ACTIONS_JOB.match(details) if isinstance(details, str) else None
    if match is None:
        return FailedCheck(name, cause=cause, signal=signal)
    return FailedCheck(
        name,
        run_id=match.group(2),
        job_id=match.group(3),
        run_url=match.group(1),
        cause=cause,
        signal=signal,
    )


def _check_row(run: dict[str, Any]) -> CheckRow:
    conclusion = run.get("conclusion")
    status = _board_status(run.get("status"), conclusion)
    details = run.get("details_url")
    job = _ACTIONS_JOB.match(details) if isinstance(details, str) else None
    return CheckRow(
        name=str(run.get("name")),
        status=status,
        conclusion=conclusion if isinstance(conclusion, str) else None,
        started_at=None if status == "queued" else _display_time(run.get("started_at")),
        completed_at=_display_time(run.get("completed_at")) if status == "completed" else None,
        log_url=None if job is None else details,
        run_id=None if job is None else job.group(2),
        job_id=None if job is None else job.group(3),
        workflow=_workflow_identity(run),
    )


def _run_job(job: dict[str, Any]) -> RunJob:
    job_id = job.get("id")
    if isinstance(job_id, bool) or not isinstance(job_id, int):
        raise GitHubAdapterError("run job has no numeric id")
    status = _board_status(job.get("status"), job.get("conclusion"))
    runner = job.get("runner_name")
    runner = runner if isinstance(runner, str) and runner else None
    kind: RunnerKind | None
    if runner is not None:
        hosted = job.get("runner_group_name") == "GitHub Actions"
        kind = "github-hosted" if hosted else "self-hosted"
    else:
        kind = "none" if status == "completed" else None
    return RunJob(
        job_id=str(job_id),
        name=str(job.get("name")),
        status=status,
        created_at=_display_time(job.get("created_at")),
        started_at=None if status == "queued" else _display_time(job.get("started_at")),
        completed_at=_display_time(job.get("completed_at")) if status == "completed" else None,
        runner_name=runner,
        runner_kind=kind,
        steps=len(steps) if isinstance(steps := job.get("steps"), list) else None,
    )


def _pool_runner(runner: dict[str, Any]) -> PoolRunner:
    name = runner.get("name")
    if not isinstance(name, str) or not name:
        raise GitHubAdapterError("runner has no name")
    os_name = runner.get("os")
    labels = runner.get("labels")
    return PoolRunner(
        name=name,
        os=os_name if isinstance(os_name, str) else "",
        status="online" if runner.get("status") == "online" else "offline",
        busy=runner.get("busy") is True,
        labels=tuple(
            label["name"]
            for label in (labels if isinstance(labels, list) else ())
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        ),
    )


def _board_status(status: object, conclusion: object) -> CIJobStatus:
    if conclusion is not None:
        return "completed"
    return "in_progress" if status == "in_progress" else "queued"


def _display_time(value: object) -> float | None:
    try:
        return _created_at(value)
    except GitHubAdapterError:
        return None


def _workflow_identity(run: dict[str, Any]) -> str:
    """The workflow run a check run belongs to, stable across its reruns.

    GitHub Actions names the workflow run in details_url
    (`.../actions/runs/<run id>/job/<job id>`), and a rerun keeps the run id.
    Any other app's checks are grouped by check suite. With neither, the
    check run's own id: it is its own check and supersedes nothing.
    """
    details = run.get("details_url")
    if isinstance(details, str):
        match = _ACTIONS_RUN.search(details)
        if match is not None:
            return f"actions-run:{match.group(1)}"
    suite = run.get("check_suite")
    if isinstance(suite, dict):
        suite_id = suite.get("id")
        if isinstance(suite_id, int) and not isinstance(suite_id, bool):
            return f"suite:{suite_id}"
    run_id = run.get("id")
    if isinstance(run_id, int) and not isinstance(run_id, bool):
        return f"check-run:{run_id}"
    return f"unidentified:{id(run)}"


def _attempt_order(run: dict[str, Any]) -> tuple[str, int]:
    started = run.get("started_at")
    run_id = run.get("id")
    return (
        started if isinstance(started, str) else "",
        run_id if isinstance(run_id, int) and not isinstance(run_id, bool) else -1,
    )


def _refusal(exc: GitHubAdapterError) -> bool:
    """A mark-ready failure that a retry cannot fix: merged, or not open."""
    text = str(exc)
    return "merged pull request" in text or "not open" in text


def _route(args: Sequence[str]) -> str:
    """METHOD and API route for an error detail: no host, query or values."""
    method = "GET"
    if "-X" in args:
        index = args.index("-X")
        if index + 1 < len(args):
            method = args[index + 1]
    endpoint = next(
        (
            arg
            for index, arg in enumerate(args)
            if not arg.startswith("-")
            and (index == 0 or args[index - 1] not in ("-X", "-f", "-F", "-H", "--cache"))
        ),
        "",
    )
    parts = endpoint.split("?", 1)[0].split("/")
    if parts[:1] == ["repos"] and len(parts) >= 3:
        parts = parts[3:]
    route = "/".join(
        part if _ROUTE_PART.fullmatch(part) else "<value>" for part in parts
    )
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        method = "<method>"
    return f"{method} {route or '<route>'}"


def _whole_fingerprint(pull: dict[str, Any]) -> bool:
    commits = pull.get("commits") or {}
    rollups = [
        ((node or {}).get("commit") or {}).get("statusCheckRollup")
        for node in commits.get("nodes") or ()
    ]
    connections = [pull.get("labels"), *(rollup.get("contexts") for rollup in rollups if rollup)]
    return all(
        isinstance(connection, dict)
        and (connection.get("pageInfo") or {}).get("hasNextPage") is False
        for connection in connections
    )


def _error_aliases(document: dict[str, Any]) -> set[str | None]:
    """The repository field each GraphQL error sits under; None for an
    error that names no single field, which spoils the whole response."""
    errors = document.get("errors")
    aliases: set[str | None] = set()
    for error in errors if isinstance(errors, list) else ():
        path = error.get("path") if isinstance(error, dict) else None
        named = isinstance(path, list) and len(path) > 1 and path[0] == "repository"
        aliases.add(path[1] if named and isinstance(path[1], str) else None)
    return aliases


def _has_graphql_data(text: str) -> bool:
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(document, dict) and isinstance(document.get("data"), dict)


def _graphql_detail(text: str) -> str:
    """GraphQL error types only, such as NOT_FOUND. Never messages."""
    types = sorted(set(_GRAPHQL_ERROR_TYPE.findall(text)))[:3]
    return f"graphql errors: {','.join(types)}" if types else ""


_BODY_FLAGS = ("-f", "-F", "--field", "--raw-field", "--input")


def _is_read(args: Sequence[str]) -> bool:
    """Whether a ``gh api`` call is provably a GET, so safe to cache or retry.

    gh sends POST when any field or --input is given without a method, so
    any body flag, any method other than GET, and any graphql call (a POST)
    make the call a non-read. Fields under an explicit ``-X GET`` would be
    query parameters, but no read here needs them, so they are not trusted.
    """
    methods: list[str] = []
    for index, arg in enumerate(args):
        if arg == "graphql":
            return False
        if arg in ("-X", "--method"):
            methods.append(args[index + 1] if index + 1 < len(args) else "")
        elif arg.startswith("--method="):
            methods.append(arg.partition("=")[2])
        elif arg.startswith("-X"):
            methods.append(arg[2:])
        elif any(
            arg == flag
            or arg.startswith(f"{flag}=")
            or (len(flag) == 2 and arg.startswith(flag))
            for flag in _BODY_FLAGS
        ):
            return False
    return all(method.upper() == "GET" for method in methods)


def _repo_path(repo: str) -> str:
    owner, slash, name = repo.partition("/")
    if (
        not slash
        or _OWNER.fullmatch(owner) is None
        or _REPO_NAME.fullmatch(name) is None
        or name in (".", "..")
    ):
        raise GitHubAdapterError(f"repo {repo!r} is not owner/name")
    return f"repos/{owner}/{name}"


def _branch(branch: str) -> str:
    if (
        not isinstance(branch, str)
        or len(branch) > 255
        or _BRANCH.fullmatch(branch) is None
        or ".." in branch
        or branch.endswith((".", ".lock"))
    ):
        raise GitHubAdapterError(f"branch {branch!r} is not a plain branch name")
    return branch


def _number(value: str) -> str:
    if not isinstance(value, str) or _NUMBER.fullmatch(value) is None:
        raise GitHubAdapterError(f"{value!r} is not a GitHub issue or PR number")
    return value


def _pr_number(ticket: TrackedTicket) -> str:
    number = ticket.pr_number
    if number is None and ticket.factory.tickets == "github":
        number = ticket.ticket_id
    if number is None:
        raise GitHubAdapterError(f"{ticket.repo}#{ticket.ticket_id} has no linked PR")
    return _number(number)


def _sha(value: object, what: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise GitHubAdapterError(f"{what} {value!r} is not a 40-character lowercase SHA")
    return value


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubAdapterError("gh returned JSON that is not an object")
    return value


def _ref_sha(ref: dict[str, Any], branch: str) -> str:
    if ref.get("ref") != f"refs/heads/{branch}":
        raise GitHubAdapterError(f"GitHub returned ref {ref.get('ref')!r} for {branch}")
    target = _object(ref.get("object"))
    if target.get("type") != "commit":
        raise GitHubAdapterError(f"refs/heads/{branch} does not point at a commit")
    return _sha(target.get("sha"), f"refs/heads/{branch}")


def _count(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GitHubAdapterError(f"{what} has no total_count")
    return value


def _labels(pages: list[object]) -> list[str]:
    names: list[str] = []
    for page in pages:
        if not isinstance(page, list):
            raise GitHubAdapterError("label page is not a list")
        for item in page:
            name = _object(item).get("name")
            if not isinstance(name, str):
                raise GitHubAdapterError("label has no name")
            names.append(name)
    return names


def _transient(exc: GitHubAdapterError) -> bool:
    """A failure a later try may pass: a 5xx, a timeout, or no HTTP status at all."""
    status = _status(exc)
    return status is None or status >= 500


def rate_limited(status: int | None, stdout: str, stderr: str) -> bool:
    if status == 429:
        return True
    if status != 403:
        return False
    text = f"{stdout}\n{stderr}".lower()
    return (
        "rate limit" in text
        or re.search(r"(?im)^x-ratelimit-remaining:\s*0\s*$", stdout) is not None
        or re.search(r"(?im)^retry-after:", stdout) is not None
    )


def _without_headers(stdout: str) -> str:
    if not stdout.startswith("HTTP/"):
        return stdout
    found = re.search(r"\r?\n\r?\n", stdout)
    return stdout[found.end() :] if found is not None else ""


def _api_message(stdout: str) -> str | None:
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    message = body.get("message") if isinstance(body, dict) else None
    return message[:200] if isinstance(message, str) else None


def _status(exc: GitHubAdapterError) -> int | None:
    found = _HTTP_STATUS.search(str(exc))
    return int(found.group(1)) if found is not None else None


def _comment_id(value: object) -> int:
    """Positive integer ids only, so 9 sorts before 10. Anything else raises."""
    if isinstance(value, bool):
        raise GitHubAdapterError(f"comment id {value!r} is not a positive integer")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and _NUMBER.fullmatch(value):
        return int(value)
    raise GitHubAdapterError(f"comment id {value!r} is not a positive integer")


def _issue_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GitHubAdapterError(f"issue number {value!r} is not a positive integer")
    return value


def _created_at(value: object) -> float:
    if not isinstance(value, str) or _ISO_8601.fullmatch(value) is None:
        raise GitHubAdapterError(f"created_at {value!r} is not ISO-8601")
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError as exc:
        raise GitHubAdapterError(f"created_at {value!r} is not ISO-8601") from exc


def _comment(item: object) -> Comment:
    raw = _object(item)
    comment_id = _comment_id(raw.get("id"))
    login = _object(raw.get("user")).get("login")
    if not isinstance(login, str) or _LOGIN.fullmatch(login) is None:
        raise GitHubAdapterError(f"comment {comment_id} has no valid author login")
    body = raw.get("body")
    if not isinstance(body, str):
        raise GitHubAdapterError(f"comment {comment_id} has no body")
    url = raw.get("html_url")
    if url is not None and not isinstance(url, str):
        raise GitHubAdapterError(f"comment {comment_id} has a bad html_url")
    created_at = _created_at(raw.get("created_at"))
    updated = raw.get("updated_at")
    updated_at = created_at if updated is None else _created_at(updated)
    return Comment(
        id=str(comment_id),
        author=login,
        body=body,
        created_at=created_at,
        url=url,
        edited_at=updated_at if updated_at > created_at else None,
    )
