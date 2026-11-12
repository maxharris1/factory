"""ReviewActions on GitHub, run on the factory device under Max's gh login.

Every process call is `env -u GH_TOKEN -u GITHUB_TOKEN -u
GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN gh api ...`, built as an
argv list and passed to run_gh, the one subprocess seam. gh therefore
uses only the login stored on the device and ignores any of those four
token variables in the environment. The adapter never reads, passes or
logs a token.

Every read goes to GitHub's REST API at call time. There is no local
clone and no cache.

- is_merged: GET pulls/<n>. head.sha must be a full SHA, open or merged.
  True only when merged and head.sha is head.
- branch_tip: GET git/ref/heads/<branch>. The full lowercase SHA, or raise.
- fast_forward: PATCH git/refs/heads/<branch> with sha=<head> and
  force=false. GitHub's 422 "not a fast forward" raises
  NotFastForwardError. It never sends force=true, merges or squashes.
- merge: PUT pulls/<n>/merge with merge_method=squash and sha=<head>,
  the API form of --squash --match-head-commit. GitHub's 409 head
  mismatch raises HeadMismatchError.
- list_comments and ci_status: GET with --paginate. ci_status is success
  only with at least one successful check run or a success commit
  status, and nothing failing or pending.
- set_state: the orch:state/<state> label. Linear and Aut0 get no labels.
- stable_patch_id and handoff are not built here and raise, so T4
  queues a re-review and a handoff ship fails closed.

A GitHub ticket_id is the PR number. Every error is a GitHubAdapterError,
which is a ShipError.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from lib.dispatcher import (
    CIResult,
    Comment,
    ReviewActions,
    RetryableShipError,
    RetryableStateError,
    ShipError,
    TicketRef,
    TrackedTicket,
)
from lib.handoff import STATES
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

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
_SHA = re.compile(r"[0-9a-f]{40}")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\[bot\])?")
_APPROVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_STATE_LABEL = re.compile(r"orch:state/[a-z][a-z-]*")
_ISO_8601 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_HTTP_STATUS = re.compile(r"\(HTTP ([0-9]{3})\)")

_CHECK_NOT_RUN = ("neutral", "skipped")
_CHECK_FAILED = (
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
)


class GitHubAdapterError(ShipError):
    pass


class NotFastForwardError(GitHubAdapterError):
    pass


class HeadMismatchError(GitHubAdapterError):
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
        del ticket, head
        raise GitHubAdapterError("stable_patch_id is not built in the GitHub adapter")

    def is_merged(self, ticket: TrackedTicket, head: str) -> bool:
        head = _sha(head, "head")
        pull = _object(
            self._json(f"{_repo_path(ticket.repo)}/pulls/{_pr_number(ticket)}")
        )
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
            if _status(exc) == 409:
                raise HeadMismatchError(f"PR head is not {head}: {exc}") from exc
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
                    f"{head} is not a fast-forward of {branch}: {exc}"
                ) from exc
            raise
        moved = _ref_sha(ref, branch)
        if moved != head:
            raise GitHubAdapterError(f"{branch} moved to {moved}, not {head}")

    def branch_tip(self, ticket: TrackedTicket, branch: str) -> str:
        endpoint = f"{_repo_path(ticket.repo)}/git/ref/heads/{_branch(branch)}"
        return _ref_sha(_object(self._json(endpoint)), branch)

    def handoff(self, ticket: TrackedTicket, head: str) -> None:
        head = _sha(head, "head")
        pull = _object(
            self._json(f"{_repo_path(ticket.repo)}/pulls/{_pr_number(ticket)}")
        )
        current = _sha(_object(pull.get("head")).get("sha"), "pull request head")
        if current != head:
            raise HeadMismatchError(f"PR head is {current}, not {head}")
        if pull.get("merged") is True:
            raise GitHubAdapterError("cannot hand off a merged pull request")
        if pull.get("state") != "open":
            raise GitHubAdapterError("cannot hand off a pull request that is not open")
        if pull.get("draft") is True:
            self._call(
                "-X",
                "POST",
                f"{_repo_path(ticket.repo)}/pulls/{_pr_number(ticket)}/ready_for_review",
            )

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

    def ci_status(self, repo: str, head: str) -> CIResult:
        """Check runs and commit statuses on head.

        success needs at least one check run that concluded success, or a
        success combined status, and nothing failing or pending. All
        neutral or skipped, nothing reported, or fewer runs received than
        total_count is pending.
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
        failed: list[str] = []
        pending = False
        passed = False
        for run in runs:
            name = str(run.get("name"))
            status = run.get("status")
            conclusion = run.get("conclusion")
            if status != "completed":
                pending = True
            elif conclusion in _CHECK_FAILED:
                failed.append(name)
            elif conclusion == "success":
                passed = True
            elif conclusion not in _CHECK_NOT_RUN:
                raise GitHubAdapterError(f"check run {name} concluded {conclusion!r}")
        if total:
            state = combined.get("state")
            if state in ("failure", "error"):
                failed.append("commit status")
            elif state == "pending":
                pending = True
            elif state == "success":
                passed = True
            else:
                raise GitHubAdapterError(f"combined status is {state!r}")
        if failed:
            return CIResult("failure", "failed: " + ", ".join(failed))
        return CIResult("success" if passed and not pending else "pending")

    def _call(self, *args: str) -> str:
        argv = [*self.gh, "api"]
        if self.cache_ttl is not None and _is_read(args):
            argv.extend(("--cache", self.cache_ttl))
        argv.extend(args)
        try:
            result = self.runner(argv, self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            # The runner may use its own timeout (the poller runs writes at
            # 60 s), so report the one the timed-out call actually had.
            raise GitHubAdapterError(
                f"gh api timed out after {exc.timeout:g}s"
            ) from exc
        except OSError as exc:
            raise GitHubAdapterError(f"gh could not run: {type(exc).__name__}") from exc
        if result.returncode != 0:
            detail = " ".join(f"{result.stderr} {result.stdout}".split())[:500]
            raise GitHubAdapterError(
                f"gh exited {result.returncode}: {detail or 'no output'}"
            )
        return result.stdout

    def _json(self, *args: str) -> object:
        text = self._call(*args)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitHubAdapterError(f"gh returned malformed JSON: {exc}") from exc

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

    def handoff(self, ticket: TrackedTicket, head: str) -> None:
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
                raise GitHubAdapterError("linked Linear ticket is unavailable")
            if fresh.pr_number != ticket.pr_number:
                raise GitHubAdapterError("linked pull request changed before handoff")
            ticket = fresh
        super().handoff(ticket, head)
        if self.move_ready_for_review:
            try:
                self.linear_ready(ticket)
            except RetryableStateError as exc:
                raise GitHubAdapterError(
                    "Linear handoff update was not confirmed: "
                    f"{type(exc).__name__}"
                ) from None


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
    return Comment(
        id=str(comment_id),
        author=login,
        body=body,
        created_at=_created_at(raw.get("created_at")),
        url=url,
    )
