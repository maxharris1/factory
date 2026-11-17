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
- file_issue and comment_once (issue #259): GET issues?state=all&since=
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
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from lib.dispatch.helpers import _base_mismatch
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

_CHECK_NOT_RUN = ("neutral", "skipped")
_CHECK_FAILED = (
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
)


LINEAR_NO_READY_STATE_NOTE = "linear-not-moved: team has no Ready for Review state"


class GitHubAdapterError(ShipError):
    """safe_detail defaults to the message, so a message must never hold
    gh's stdout or stderr; a wrapper passes on the inner safe_detail."""

    def __init__(self, message: str, *, safe_detail: str | None = None):
        super().__init__(message)
        self.safe_detail = message if safe_detail is None else safe_detail


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
    """The adapter's only process call. argv is a list; no shell."""
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return GhResult(completed.returncode, completed.stdout, completed.stderr)


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

    def handoff(self, ticket: TrackedTicket, head: str) -> str | None:
        self._mark_ready(ticket, head)
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
        issues = _repo_path(ticket.repo) + "/issues"
        after = datetime.fromtimestamp(since - 3600, tz=timezone.utc)
        listing = f"{issues}?state=all&since={after:%Y-%m-%dT%H:%M:%SZ}&per_page=100"
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
                    return _issue_number(raw.get("number"))
        args = ["-X", "POST", issues, "-f", f"title={title}", "-f", f"body={body}"]
        for label in labels:
            args.extend(("-f", f"labels[]={label}"))
        return _issue_number(_object(self._json(*args)).get("number"))

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
        if any(
            has_marker(comment.body, marker) and comment.author == self._login()
            for comment in self.list_comments(repo, number)
        ):
            return
        self._call("-X", "POST", f"{_repo_path(repo)}/issues/{number}/comments", "-f", f"body={body}")

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

    def pull_labels(self, repo: str, number: str) -> tuple[str, ...]:
        """The PR's labels, all pages (issue #86 hold label)."""
        endpoint = f"{_repo_path(repo)}/issues/{_number(number)}/labels?per_page=100"
        return tuple(sorted(_labels(self._pages(endpoint))))

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
        """
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
        failed: list[FailedCheck] = []
        pending: list[str] = []
        passed = False
        for run in runs:
            name = str(run.get("name"))
            status = run.get("status")
            conclusion = run.get("conclusion")
            if status != "completed":
                pending.append(name)
            elif conclusion in _CHECK_FAILED:
                failed.append(_failed_check(name, run.get("details_url")))
            elif conclusion == "success":
                passed = True
            elif conclusion not in _CHECK_NOT_RUN:
                raise GitHubAdapterError(f"check run {name} concluded {conclusion!r}")
        if total:
            state = combined.get("state")
            if state in ("failure", "error"):
                failed.append(FailedCheck("commit status"))
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
            )
        return CIResult("success" if passed and not pending else "pending", pending=tuple(pending))

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
        if result.returncode != 0:
            detail = " ".join(f"{result.stderr} {result.stdout}".split())[:500]
            safe = f"{route} exited {result.returncode}"
            status = _HTTP_STATUS.search(result.stderr)
            if status is not None:
                safe += f" (HTTP {status.group(1)})"
            graphql = _graphql_detail(result.stdout)
            if graphql:
                safe += f" {graphql}"
            raise GitHubAdapterError(
                f"gh exited {result.returncode}: {detail or 'no output'}",
                safe_detail=safe,
            )
        return result.stdout

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
        """Mark the PR ready, then move a before-review issue to Ready for
        Review. Returns a note for the done wake when the move was skipped
        because the team has no Ready for Review state, else None."""
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
        # Move only an issue still before review (Backlog, Triage, Todo, or
        # started before Ready for Review). One already in Ready for Review
        # (a no-op re-drive) or moved on by a human (Done, Canceled, or past
        # Ready for Review) is left where it is. A team with no Ready for
        # Review state has nowhere to move it: the handoff still completes,
        # and the note tells the project bot the issue was not moved.
        if (
            self.move_ready_for_review
            and ticket.linear_state_phase == "no-ready-state"
        ):
            return LINEAR_NO_READY_STATE_NOTE
        if self.move_ready_for_review and ticket.linear_state_phase == "before-review":
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


def _failed_check(name: str, details: object) -> FailedCheck:
    match = _ACTIONS_JOB.match(details) if isinstance(details, str) else None
    if match is None:
        return FailedCheck(name)
    return FailedCheck(name, run_id=match.group(2), job_id=match.group(3), run_url=match.group(1))


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
            and (index == 0 or args[index - 1] not in ("-X", "-f", "-F", "--cache"))
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
