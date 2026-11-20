"""`orch job done --handoff <id>` checks the handoff it names (#191, T17a).

Before a job is marked done, its handoff comment is read from GitHub and
checked against the job: the repo, the PR, the author's login (a
trusted_logins key that may speak for the job's bot), and the orch:v1
header's `kind`, `from`, `role`, `head` and `round`. A handoff that fails
is `submitted-invalid` (lib/queue.py record_invalid_handoff): the job
stays running so the harness can post a correct one, until the third
invalid one fails the job for T6.

The expectations come from the same job facts the brief was rendered from
(lib/brief/common.py facts_of), so the check asks for exactly the header
the brief showed. A `questions` handoff is accepted from any job but a qa
job, with its role, and carries no head or round.

A ready names the PR's current head; a verdict names the job's head, when
the job has one. A qa job's handoff is a `kind=qa` from grok-bot at exactly
the job's 40-character head (#763). With no PR number on the job (a Linear
ticket before its PR is known) the PR is the comment's own: every handoff
kind, questions included, must be on a PR, and on the job's branch when the
job names one. A plan needs an `Estimate: <n>` body line (#1021). A ready
for a job whose detail lists `proof` needs a `## Proof` entry for each
criterion it names (lib/proof.py, #1032).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import quote

from lib.approve_rule import approve_refusal
from lib.brief import brief_kind
from lib.brief.common import BriefError, detail_of, facts_of
from lib.github_actions import GH
from lib.handoff import HandoffError, parse_comment, plan_estimate, plan_estimate_text
from lib.proof import Required, from_detail, missing
from lib.queue import Job

GH_TIMEOUT_SECONDS = 30.0
RETRY_DELAYS = (2.0, 8.0)

_COMMENT_ID = re.compile(r"^[1-9][0-9]{0,19}$")
SERVER_ERROR = re.compile(r"HTTP 5\d\d")
_ISSUE_URL = re.compile(r"^https://api\.github\.com/repos/([^/]+/[^/]+)/issues/([1-9][0-9]*)$")


class NotFound(Exception):
    """GitHub answered 404."""


class ReadError(Exception):
    """GitHub could not be read (not a 404): nothing is decided."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


Read = Callable[[str], Mapping[str, object]]


@dataclass(frozen=True)
class Expected:
    repo: str
    pr: int | None
    branch: str | None
    bot: str
    role: str
    kind: str
    head: str | None
    round: str | None
    proof: tuple[Required, ...] | None = None


def expected_for(job: Job, bot: str) -> Expected:
    detail = detail_of(job)
    facts = facts_of(job, detail, None)
    try:
        kind = brief_kind(job)
    except BriefError:
        kind = "implement"
    branch = detail.get("gitBranchName")
    if job.request.action == "qa":
        bot, role, handoff, round_text = "grok-bot", "qa", "qa", None
    elif kind == "review":
        role, handoff, round_text = "reviewer", "verdict", facts["round"]
    elif job.request.action == "plan":
        role, handoff, round_text = "builder", "plan", f"{facts['plan_round']}/2"
    else:
        role, handoff, round_text = "builder", "ready", None
    return Expected(
        repo=job.request.repo,
        pr=facts["pr"],
        branch=branch if isinstance(branch, str) and branch else None,
        bot=bot,
        role=role,
        kind=handoff,
        head=facts["head"] if handoff in ("verdict", "qa") else None,
        round=round_text,
        proof=from_detail(detail.get("proof")) if handoff == "ready" else None,
    )


def gh_reader(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Read:
    def read(path: str) -> Mapping[str, object]:
        try:
            result = run(
                [*GH, "api", "-X", "GET", path],
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReadError(
                f"gh api {path}: {type(exc).__name__}",
                retryable=isinstance(exc, subprocess.TimeoutExpired),
            ) from exc
        if result.returncode != 0:
            text = " ".join(f"{result.stderr} {result.stdout}".split())
            if "HTTP 404" in text or "Not Found" in text:
                raise NotFound(path)
            raise ReadError(
                f"gh api {path}: {text[:300] or f'exit {result.returncode}'}",
                retryable=SERVER_ERROR.search(text) is not None,
            )
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise ReadError(f"gh api {path}: not JSON") from exc
        if not isinstance(value, dict):
            raise ReadError(f"gh api {path}: not an object")
        return value

    return read


def retrying(
    read: Read,
    log: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    *,
    label: str = "handoff check",
) -> Read:
    def read_again(path: str) -> Mapping[str, object]:
        delays = RETRY_DELAYS
        total = len(delays) + 1
        for attempt, delay in enumerate(delays, 1):
            try:
                return read(path)
            except ReadError as exc:
                if not exc.retryable:
                    raise
                log(f"{label}: {exc}; attempt {attempt} of {total}, retrying in {delay:g}s")
            sleep(delay)
        try:
            return read(path)
        except ReadError as exc:
            if not exc.retryable:
                raise
            raise ReadError(f"{exc} (attempt {total} of {total})") from exc

    return read_again


def problems(
    expected: Expected,
    comment_id: str,
    read: Read,
    trusted_logins: Mapping[str, frozenset[str]],
) -> list[str]:
    """Why the handoff comment does not fit the job; empty when it does.

    Raises ReadError when GitHub could not be read, so a network failure
    is never taken for an invalid handoff."""
    if not _COMMENT_ID.fullmatch(comment_id):
        return [f"{comment_id!r} is not a GitHub comment id"]
    try:
        comment = read(f"repos/{expected.repo}/issues/comments/{comment_id}")
    except NotFound:
        return [f"comment {comment_id} does not exist in {expected.repo}"]
    found: list[str] = []
    match = _ISSUE_URL.fullmatch(str(comment.get("issue_url") or ""))
    if match is None:
        return [f"comment {comment_id} is not on an issue or PR"]
    repo, number = match.group(1), int(match.group(2))
    if repo.lower() != expected.repo.lower():
        found.append(f"comment {comment_id} is in {repo}, not {expected.repo}")
    if expected.pr is not None and number != expected.pr:
        found.append(f"comment {comment_id} is on #{number}, not PR #{expected.pr}")
    pull = None
    if expected.pr is None:
        pull = _pull(repo, number, read)
        if pull is None:
            found.append(f"#{number} is not a PR")
        elif expected.branch is not None and pull.ref != expected.branch:
            found.append(f"#{number} is on branch {pull.ref}, not {expected.branch}")
    user = comment.get("user")
    login = str(user.get("login") or "") if isinstance(user, Mapping) else ""
    if expected.bot not in _bots_for(trusted_logins, login):
        found.append(f"author {login or '(none)'} is not a trusted_logins login for {expected.bot}")
    try:
        handoff = parse_comment(str(comment.get("body") or ""))
    except HandoffError as exc:
        return [*found, f"comment {comment_id} has no valid orch:v1 header ({exc})"]
    fields = handoff.as_dict()
    kind = fields.get("kind")
    role = fields.get("role", "qa" if kind == "qa" else None)
    if fields.get("from") != expected.bot:
        found.append(f"from={fields.get('from')} is not {expected.bot}")
    if role != expected.role:
        found.append(f"role={role} is not {expected.role}")
    if kind == "questions" and expected.kind != "qa":
        return found
    if kind != expected.kind:
        allowed = "" if expected.kind == "qa" else " (or questions)"
        return [*found, f"kind={kind} is not {expected.kind}{allowed}"]
    if expected.round is not None and fields.get("round") != expected.round:
        found.append(f"round={fields.get('round')} is not {expected.round}")
    if kind == "verdict" and expected.head is not None:
        reported = fields.get("head")
        if reported is None:
            found.append(f"no head; the reviewed head is {expected.head}")
        elif not _same_head(reported, expected.head):
            found.append(f"head={reported} is not the reviewed head {expected.head}")
    if kind == "qa" and fields["head"] != expected.head:
        found.append(f"head={fields['head']} is not the job's head {expected.head}")
    if kind == "plan" and plan_estimate(handoff.body) is None:
        text = plan_estimate_text(handoff.body)
        found.append(
            'plan has no "Estimate: <n>" line' if text is None
            else f"plan Estimate {text!r} is not a whole number of lines, 0 to 999999999"
        )
    if kind == "ready" and expected.proof is not None:
        found.extend(missing(expected.proof, handoff.body))
    refusal = approve_refusal(fields, handoff.body)
    if refusal is not None:
        found.append(refusal.message)
    if kind == "ready" and expected.pr is not None:
        pull = _pull(repo, number, read)
        if pull is None:
            found.append(f"#{number} is not a PR")
    if kind == "ready" and pull is not None and not _same_head(fields["head"], pull.sha):
        found.append(
            f"head={fields['head']} is not the head of #{number} ({pull.sha or 'unknown'})"
        )
    return found


@dataclass(frozen=True)
class _Pull:
    sha: str
    ref: str


def _pull(repo: str, number: int, read: Read) -> _Pull | None:
    try:
        pull = read(f"repos/{repo}/pulls/{number}")
    except NotFound:
        return None
    head = pull.get("head")
    if not isinstance(head, Mapping):
        return _Pull(sha="", ref="")
    sha, ref = str(head.get("sha") or ""), str(head.get("ref") or "")
    if ref and pull.get("state") == "closed" and not pull.get("merged"):
        sha = _branch_tip(repo, ref, read) or sha
    return _Pull(sha=sha, ref=ref)


def _branch_tip(repo: str, ref: str, read: Read) -> str | None:
    """GitHub freezes a closed PR's head.sha, so a branch rebuilt after
    the PR auto-closed (#705) is read from the branch itself."""
    try:
        found = read(f"repos/{repo}/git/ref/heads/{quote(ref, safe='/')}")
    except NotFound:
        return None
    target = found.get("object")
    sha = target.get("sha") if isinstance(target, Mapping) else None
    return sha if isinstance(sha, str) and sha else None


def _same_head(reported: str, current: str | None) -> bool:
    if not current or min(len(reported), len(current)) < 7:
        return False
    return current.startswith(reported) or reported.startswith(current)


def _bots_for(trusted_logins: Mapping[str, frozenset[str]], login: str) -> frozenset[str]:
    wanted = login.casefold()
    for key, bots in trusted_logins.items():
        if key.casefold() == wanted:
            return frozenset(bots)
    return frozenset()


def ready_head(expected: Expected, comment_id: str, read: Read) -> str | None:
    """The head a ready names (#503), as the PR's full sha when they still
    agree; None when the handoff is not a ready."""
    if expected.kind != "ready":
        return None
    comment = read(f"repos/{expected.repo}/issues/comments/{comment_id}")
    try:
        fields = parse_comment(str(comment.get("body") or "")).as_dict()
    except HandoffError:
        return None
    if fields.get("kind") != "ready":
        return None
    match = _ISSUE_URL.fullmatch(str(comment.get("issue_url") or ""))
    if match is None:
        return None
    pull = _pull(match.group(1), int(match.group(2)), read)
    named = str(fields.get("head") or "")
    return pull.sha if pull is not None and _same_head(named, pull.sha) else named
