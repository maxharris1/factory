import ast
import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib.dispatcher import (
    CIResult,
    Dispatcher,
    FailedCheck,
    RetryableShipError,
    RetryableStateError,
    ReviewShipper,
    ShipError,
    ShipRequest,
    TicketRef,
    TrackedTicket,
    _comment_order,
)
from lib.dispatch.types import BaseCheck, CheckRow, PoolRunner, RunJob
from lib.github_actions import (
    GH,
    GhResult,
    GitHubAdapterError,
    GitHubReviewActions,
    HeadMismatchError,
    LinearReviewActions,
    NotFastForwardError,
    review_actions_for,
)
from lib.evidence.ci import base_red, ci_evidence
from lib.hook_t7 import T7Hook
from lib.hook_t8 import T8Hook
from lib.queue import Job, SQLiteJobQueue
from tests.test_hooks import (
    TRUSTED_LOGINS,
    FakeBot,
    factory,
    in_review,
    outcomes,
    workflow_state,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github"
TOKEN_VARIABLES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
ENV_PREFIX = [
    "env",
    "-u",
    "GH_TOKEN",
    "-u",
    "GITHUB_TOKEN",
    "-u",
    "GH_ENTERPRISE_TOKEN",
    "-u",
    "GITHUB_ENTERPRISE_TOKEN",
    "gh",
]
BASE = "1405df66cbe219b0bf6355bc3d60361a8376b6b4"
HEAD = "1a954628a960aaef81d7b2d4521929579f3541e6"
REPO = "repos/owner/repo"
COMMENTS = ("--paginate", f"{REPO}/issues/42/comments?per_page=100")
CHECKS = ("--paginate", f"{REPO}/commits/{HEAD}/check-runs?per_page=100")
STATUS = (f"{REPO}/commits/{HEAD}/status",)
TIP = (f"{REPO}/git/ref/heads/main",)
PULL = (f"{REPO}/pulls/42",)
FF = (
    "-X",
    "PATCH",
    f"{REPO}/git/refs/heads/main",
    "-f",
    f"sha={HEAD}",
    "-F",
    "force=false",
)
MERGE = (
    "-X",
    "PUT",
    f"{REPO}/pulls/42/merge",
    "-f",
    "merge_method=squash",
    "-f",
    f"sha={HEAD}",
)
LABELS = ("--paginate", f"{REPO}/issues/42/labels?per_page=100")
NODE_ID = "PR_kwDOKfac0s6CiTKq"
MARK_READY_QUERY = (
    "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) "
    "{ pullRequest { id isDraft headRefOid } } }"
)
# GitHub has no REST route for draft -> ready. gh pr ready uses this mutation.
def mark_ready(repo: str = "owner/repo") -> tuple[str, ...]:
    return (
        "-X",
        "POST",
        "graphql",
        "-f",
        f"query={MARK_READY_QUERY}",
        "-f",
        f"id={NODE_ID}",
        "-f",
        f"owner={repo.partition('/')[0]}",
    )


MARK_READY = mark_ready()
PREVIEW = (
    "-X",
    "POST",
    f"{REPO}/issues/42/comments",
    "-f",
    f"body=Preview for tracy-site: head {HEAD}. Waiting for tracy-site's ok "
    "on this exact SHA, or a decline.",
)


def add_label(state: str) -> tuple[str, ...]:
    return ("-X", "POST", f"{REPO}/issues/42/labels", "-f", f"labels[]=orch:state/{state}")


def remove_label(state: str) -> tuple[str, ...]:
    return ("-X", "DELETE", f"{REPO}/issues/42/labels/orch%3Astate%2F{state}")


def served(*names: str) -> GhResult:
    """gh api output: one JSON document per page, back to back."""
    return GhResult(0, "".join((FIXTURES / name).read_text() for name in names), "")


def http_error(name: str, status: int) -> GhResult:
    body = (FIXTURES / name).read_text()
    message = body.split('"message": "', 1)[1].split('"', 1)[0]
    return GhResult(1, body, f"gh: {message} (HTTP {status})\n")


TIMEOUT = subprocess.TimeoutExpired(cmd="gh", timeout=60)


class RecordedGh:
    """The adapter's runner seam, serving tests/fixtures/github responses.

    A route keeps answering with its last response; earlier ones are used
    once each, in order.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, ...], list[GhResult | BaseException]] = {}
        self.calls: list[list[str]] = []

    def route(self, key: tuple[str, ...], *responses: GhResult | BaseException) -> None:
        self.routes[key] = list(responses)

    def __call__(self, argv: Sequence[str], timeout: float) -> GhResult:
        assert timeout > 0
        argv = list(argv)
        self.calls.append(argv)
        key = _key(argv)
        if key not in self.routes:
            raise AssertionError(f"unrecorded gh call {key}")
        queue = self.routes[key]
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, BaseException):
            raise response
        return response

    def count(self, key: tuple[str, ...]) -> int:
        return sum(1 for call in self.calls if _key(call) == key)

    def keys(self) -> list[tuple[str, ...]]:
        return [_key(call) for call in self.calls]


def _key(argv: list[str]) -> tuple[str, ...]:
    assert argv[: len(GH)] == list(GH), argv
    rest = argv[len(GH) :]
    return tuple(rest[1:] if rest[:1] == ["api"] else rest)


def pr(
    ship: str = "after:tracy-site",
    *,
    tickets: str = "github",
    repo: str = "owner/repo",
    ticket_id: str = "42",
) -> TrackedTicket:
    return TrackedTicket(
        repo=repo,
        ticket_id=ticket_id,
        factory=replace(factory(ship), tickets=tickets),
        comments=(),
        current_head=HEAD,
        ci={},
        risk="high",
    )


def adapter() -> tuple[GitHubReviewActions, RecordedGh]:
    gh = RecordedGh()
    return GitHubReviewActions(gh), gh


def test_gh_prefix_unsets_every_token_variable() -> None:
    assert list(GH) == ENV_PREFIX


def test_every_invocation_starts_with_env_u_gh_token_gh() -> None:
    actions, gh = adapter()
    gh.route(TIP, served("ref-main-base.json"))
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(FF, served("ref-main-head.json"))
    gh.route(MERGE, served("merge-squash-200.json"))
    gh.route(PREVIEW, served("issue-comment-201.json"))
    gh.route(LABELS, served("labels-review.json"))
    gh.route(add_label("merged"), served("labels-empty.json"))
    gh.route(remove_label("review"), served("labels-empty.json"))
    gh.route(COMMENTS, served("comments-tied-page-1.json", "comments-tied-page-2.json"))
    gh.route(CHECKS, served("check-runs-success.json"))
    gh.route(STATUS, served("status-empty.json"))

    actions.branch_tip(pr(), "main")
    actions.is_merged(pr(), HEAD)
    actions.fast_forward(pr(), branch="main", head=HEAD)
    actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)
    actions.await_approver(pr(), HEAD, "tracy-site")
    actions.set_state(pr(), "merged")
    actions.list_comments("owner/repo", "42")
    actions.ci_status("owner/repo", HEAD)

    assert len(gh.calls) == 12  # merge reads the pull first (#83 mark-ready)
    for argv in gh.calls:
        assert argv[:11] == [*ENV_PREFIX, "api"]
        assert all(isinstance(part, str) for part in argv)


def test_the_subprocess_seam_is_one_function_with_no_shell() -> None:
    tree = ast.parse((Path(__file__).parents[1] / "lib" / "github_actions.py").read_text())
    callers: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                callers.append(function.name)
                assert all(keyword.arg != "shell" for keyword in node.keywords)
    assert callers == ["run_gh", "git_patch_id"]
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "os" for node in ast.walk(tree)
    )
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    }
    assert used == {"killpg"}


def test_gh_token_in_the_environment_is_not_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    checks = "".join(
        f'if [ "${{{name}+set}}" = set ]; then echo "{name} set"; '
        f'else echo "{name} unset"; fi\n'
        for name in TOKEN_VARIABLES
    )
    stub.write_text(
        "#!/bin/sh\n"
        f'{{\n{checks}}} > "$STUB_TOKEN_SEEN"\n'
        'printf "%s\\n" "$@" > "$STUB_ARGS"\n'
        'cat "$STUB_RESPONSE"\n'
    )
    stub.chmod(0o755)
    seen = tmp_path / "token-seen"
    args = tmp_path / "args"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for name in TOKEN_VARIABLES:
        monkeypatch.setenv(name, "placeholder-not-a-credential")
    monkeypatch.setenv("STUB_TOKEN_SEEN", str(seen))
    monkeypatch.setenv("STUB_ARGS", str(args))
    monkeypatch.setenv("STUB_RESPONSE", str(FIXTURES / "ref-main-base.json"))

    subprocess.run([str(stub)], check=True, capture_output=True)
    assert seen.read_text().splitlines() == [f"{name} set" for name in TOKEN_VARIABLES]

    assert GitHubReviewActions().branch_tip(pr(), "main") == BASE

    assert seen.read_text().splitlines() == [
        f"{name} unset" for name in TOKEN_VARIABLES
    ]
    assert args.read_text().splitlines() == ["api", f"{REPO}/git/ref/heads/main"]


def test_fast_forward_success_patches_the_remote_ref_with_force_false() -> None:
    actions, gh = adapter()
    gh.route(FF, served("ref-main-head.json"))

    actions.fast_forward(pr(), branch="main", head=HEAD)

    assert gh.keys() == [FF]


def test_fast_forward_422_not_a_fast_forward_raises_and_never_falls_back() -> None:
    actions, gh = adapter()
    gh.route(FF, http_error("ref-not-fast-forward-422.json", 422))

    with pytest.raises(NotFastForwardError, match="is not a fast-forward of main") as raised:
        actions.fast_forward(pr(), branch="main", head=HEAD)

    assert isinstance(raised.value, ShipError)
    assert "(HTTP 422)" in str(raised.value)
    assert "Update is not a fast forward" not in str(raised.value)
    assert gh.keys() == [FF]


@pytest.mark.parametrize("outcome", ["landed", "422", "other-error"])
def test_fast_forward_always_sends_force_false_and_never_force_true(outcome: str) -> None:
    actions, gh = adapter()
    response = {
        "landed": served("ref-main-head.json"),
        "422": http_error("ref-not-fast-forward-422.json", 422),
        "other-error": GhResult(1, "", "gh: Server Error (HTTP 502)\n"),
    }[outcome]
    gh.route(FF, response)

    try:
        actions.fast_forward(pr(), branch="main", head=HEAD)
    except GitHubAdapterError:
        assert outcome != "landed"

    assert len(gh.calls) == 1
    argv = gh.calls[0]
    assert argv[argv.index("-F") + 1] == "force=false"
    assert not any("force=true" in part for part in argv)
    assert not any(part in ("--force", "-f=force") for part in argv)


def test_fast_forward_raises_when_github_reports_another_sha() -> None:
    actions, gh = adapter()
    gh.route(FF, served("ref-main-base.json"))

    with pytest.raises(GitHubAdapterError, match=f"main moved to {BASE}, not {HEAD}"):
        actions.fast_forward(pr(), branch="main", head=HEAD)


def test_branch_tip_reads_the_full_sha_of_the_remote_ref_every_call() -> None:
    actions, gh = adapter()
    gh.route(TIP, served("ref-main-base.json"), served("ref-main-head.json"))

    assert actions.branch_tip(pr(), "main") == BASE
    assert actions.branch_tip(pr(), "main") == HEAD
    assert gh.keys() == [TIP, TIP]


@pytest.mark.parametrize(
    "mutate",
    ["short-sha", "uppercase-sha", "wrong-ref", "tag-object", "malformed-json"],
)
def test_branch_tip_raises_on_anything_but_a_full_lowercase_sha(mutate: str) -> None:
    actions, gh = adapter()
    text = (FIXTURES / "ref-main-base.json").read_text()
    text = {
        "short-sha": text.replace(BASE, BASE[:7]),
        "uppercase-sha": text.replace(BASE, BASE.upper()),
        "wrong-ref": text.replace('"refs/heads/main"', '"refs/heads/main-old"'),
        "tag-object": text.replace('"type": "commit"', '"type": "tag"'),
        "malformed-json": text[:-10],
    }[mutate]
    gh.route(TIP, GhResult(0, text, ""))

    with pytest.raises(GitHubAdapterError):
        actions.branch_tip(pr(), "main")


def test_commits_ahead_reads_ahead_by_from_the_compare_of_base_and_head() -> None:
    actions, gh = adapter()
    compare = (f"{REPO}/compare/release/v1...{HEAD}",)
    gh.route(compare, GhResult(0, json.dumps({"ahead_by": 9, "behind_by": 0}), ""))

    assert actions.commits_ahead(pr(), base="release/v1", head=HEAD) == 9
    assert gh.keys() == [compare]


@pytest.mark.parametrize("body", [{}, {"ahead_by": "9"}, {"ahead_by": None}])
def test_commits_ahead_raises_without_an_integer_ahead_by(body: dict[str, object]) -> None:
    actions, gh = adapter()
    gh.route((f"{REPO}/compare/main...{HEAD}",), GhResult(0, json.dumps(body), ""))

    with pytest.raises(GitHubAdapterError):
        actions.commits_ahead(pr(), base="main", head=HEAD)


def test_reopen_patches_the_pull_state_to_open() -> None:
    actions, gh = adapter()
    reopen = ("-X", "PATCH", f"{REPO}/pulls/42", "-f", "state=open")
    gh.route(reopen, GhResult(0, "{}", ""))

    actions.reopen(pr())

    assert gh.keys() == [reopen]


@pytest.mark.parametrize(
    ("fixture", "head", "expected"),
    [
        ("pull-42-merged.json", HEAD, True),
        ("pull-42-open.json", HEAD, False),
        ("pull-42-merged.json", BASE, False),
    ],
    ids=["merged-at-head", "open", "merged-at-another-head"],
)
def test_is_merged_reads_the_pull_at_call_time(
    fixture: str,
    head: str,
    expected: bool,
) -> None:
    actions, gh = adapter()
    gh.route(PULL, served(fixture))

    assert actions.is_merged(pr(), head) is expected
    assert gh.keys() == [PULL]


@pytest.mark.parametrize("fixture", ["pull-42-open.json", "pull-42-merged.json"])
@pytest.mark.parametrize(
    "bad_sha",
    [HEAD[:7], HEAD.upper(), HEAD + "a", "", None],
    ids=["short", "uppercase", "long", "empty", "null"],
)
def test_is_merged_validates_head_sha_even_on_an_open_pr(
    fixture: str,
    bad_sha: str | None,
) -> None:
    actions, gh = adapter()
    text = (FIXTURES / fixture).read_text()
    replacement = "null" if bad_sha is None else f'"{bad_sha}"'
    text = text.replace(f'"sha": "{HEAD}"', f'"sha": {replacement}')
    gh.route(PULL, GhResult(0, text, ""))

    with pytest.raises(GitHubAdapterError, match="pull request head"):
        actions.is_merged(pr(), HEAD)


def test_pull_head_reads_the_head_sha_at_call_time() -> None:
    actions, gh = adapter()
    gh.route(PULL, served("pull-42-open.json"))

    assert actions.pull_head("owner/repo", "42") == HEAD
    assert gh.keys() == [PULL]


@pytest.mark.parametrize(
    "bad_sha",
    [HEAD[:7], HEAD.upper(), "", None],
    ids=["short", "uppercase", "empty", "null"],
)
def test_pull_head_refuses_a_head_that_is_not_a_full_sha(bad_sha: str | None) -> None:
    actions, gh = adapter()
    text = (FIXTURES / "pull-42-open.json").read_text()
    replacement = "null" if bad_sha is None else f'"{bad_sha}"'
    gh.route(PULL, GhResult(0, text.replace(f'"sha": "{HEAD}"', f'"sha": {replacement}'), ""))

    with pytest.raises(GitHubAdapterError, match="pull request head"):
        actions.pull_head("owner/repo", "42")


def test_paginated_comments_with_tied_created_at_order_9_before_10() -> None:
    actions, gh = adapter()
    gh.route(COMMENTS, served("comments-tied-page-1.json", "comments-tied-page-2.json"))

    comments = actions.list_comments("owner/repo", "42")

    assert [comment.id for comment in comments] == ["7", "9", "10"]
    assert comments[1].created_at == comments[2].created_at
    assert [item.id for item in sorted(comments, key=_comment_order)] == ["7", "9", "10"]
    assert comments[1].author == "tracy-login"
    assert comments[1].url == "https://github.com/owner/repo/pull/42#issuecomment-9"
    assert gh.keys() == [COMMENTS]


@pytest.mark.parametrize(
    "bad_id",
    ['"IC_kwDOKfac0s5AAAAJ"', '"9a"', "0", "-9", "true", "9.0", "null", '""'],
)
def test_a_non_numeric_comment_id_is_rejected(bad_id: str) -> None:
    actions, gh = adapter()
    if bad_id == '"IC_kwDOKfac0s5AAAAJ"':
        gh.route(COMMENTS, served("comments-non-numeric-id.json"))
    else:
        text = (FIXTURES / "comments-tied-page-2.json").read_text()
        gh.route(COMMENTS, GhResult(0, text.replace('"id": 9,', f'"id": {bad_id},'), ""))

    with pytest.raises(GitHubAdapterError, match="is not a positive integer"):
        actions.list_comments("owner/repo", "42")


def test_a_numeric_string_comment_id_is_coerced_to_int() -> None:
    actions, gh = adapter()
    text = (FIXTURES / "comments-tied-page-2.json").read_text()
    gh.route(COMMENTS, GhResult(0, text.replace('"id": 9,', '"id": "9",'), ""))

    assert [comment.id for comment in actions.list_comments("owner/repo", "42")] == ["9"]


@pytest.mark.parametrize(
    "created_at",
    ["2026-11-10 20:00:00", "2026-11-10T20:00:00", "yesterday", "2026-13-40T20:00:00Z"],
)
def test_created_at_must_be_iso_8601_with_a_zone(created_at: str) -> None:
    actions, gh = adapter()
    text = (FIXTURES / "comments-tied-page-2.json").read_text()
    text = text.replace('"created_at": "2026-11-10T20:00:00Z"', f'"created_at": "{created_at}"')
    gh.route(COMMENTS, GhResult(0, text, ""))

    with pytest.raises(GitHubAdapterError, match="is not ISO-8601"):
        actions.list_comments("owner/repo", "42")


def test_a_comment_listed_on_two_pages_is_rejected() -> None:
    actions, gh = adapter()
    gh.route(COMMENTS, served("comments-tied-page-2.json", "comments-tied-page-2.json"))

    with pytest.raises(GitHubAdapterError, match="listed twice"):
        actions.list_comments("owner/repo", "42")


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (GhResult(1, "", "gh: Not Found (HTTP 404)\n"), "gh exited 1: gh: Not Found"),
        (GhResult(4, "", "To get started with GitHub CLI, please run: gh auth login\n"), "gh exited 4"),
        (GhResult(0, "{not json", ""), "malformed JSON"),
        (GhResult(0, "", ""), "malformed JSON"),
        (TIMEOUT, "timed out after 60s"),
        (FileNotFoundError("env"), "gh could not run"),
    ],
    ids=["http-404", "not-logged-in", "malformed-json", "empty", "timeout", "no-binary"],
)
def test_a_gh_failure_raises_a_typed_adapter_error(
    response: GhResult | BaseException,
    match: str,
) -> None:
    actions, gh = adapter()
    gh.route(TIP, response)

    with pytest.raises(GitHubAdapterError, match=match) as raised:
        actions.branch_tip(pr(), "main")

    assert isinstance(raised.value, ShipError)


def test_squash_merge_sends_squash_and_the_exact_head_sha() -> None:
    actions, gh = adapter()
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, served("merge-squash-200.json"))

    actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)

    # #83: the pull is read first; it is not a draft, so no mark-ready.
    assert gh.keys() == [PULL, MERGE]


def test_squash_merge_raises_when_the_head_does_not_match() -> None:
    actions, gh = adapter()
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, http_error("merge-head-modified-409.json", 409))

    with pytest.raises(HeadMismatchError, match=f"PR head is not {HEAD}"):
        actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)

    assert gh.keys() == [PULL, MERGE]


@pytest.mark.parametrize("head", [HEAD[:7], HEAD.upper(), HEAD + "a", "-f", ""])
def test_merge_and_fast_forward_refuse_a_head_that_is_not_40_lowercase_hex(
    head: str,
) -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError, match="40-character lowercase SHA"):
        actions.merge(pr("merge"), squash=True, match_head_commit=head)
    with pytest.raises(GitHubAdapterError, match="40-character lowercase SHA"):
        actions.fast_forward(pr(), branch="main", head=head)
    with pytest.raises(GitHubAdapterError, match="40-character lowercase SHA"):
        actions.is_merged(pr(), head)

    assert gh.calls == []


def test_merge_without_squash_is_refused() -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError, match="only squash"):
        actions.merge(pr("merge"), squash=False, match_head_commit=HEAD)

    assert gh.calls == []


@pytest.mark.parametrize(
    "branch",
    [
        "--force",
        "-main",
        "main..x",
        "../main",
        "main/",
        "/main",
        "main//x",
        "main.lock",
        "main?ref=x",
        "main#x",
        "main x",
        "main;rm",
        "ma\nin",
        "",
    ],
)
def test_branch_names_are_validated_before_any_call(branch: str) -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError, match="not a plain branch name"):
        actions.branch_tip(pr(), branch)
    with pytest.raises(GitHubAdapterError, match="not a plain branch name"):
        actions.fast_forward(pr(), branch=branch, head=HEAD)

    assert gh.calls == []


def test_nested_branch_names_are_allowed() -> None:
    actions, gh = adapter()
    key = (f"{REPO}/git/ref/heads/release/v1.2",)
    text = (FIXTURES / "ref-main-base.json").read_text()
    gh.route(key, GhResult(0, text.replace("refs/heads/main", "refs/heads/release/v1.2"), ""))

    assert actions.branch_tip(pr(), "release/v1.2") == BASE


@pytest.mark.parametrize(
    "repo",
    ["owner", "owner/repo/extra", "-X/repo", "owner/..", "owner/.", "own er/repo", "owner/re?po", "/repo"],
)
def test_owner_and_repo_are_validated_before_any_call(repo: str) -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError, match="is not owner/name"):
        actions.branch_tip(pr(repo=repo), "main")
    with pytest.raises(GitHubAdapterError, match="is not owner/name"):
        actions.list_comments(repo, "42")

    assert gh.calls == []


@pytest.mark.parametrize("ticket_id", ["0", "-1", "42/merge", "AUT-12", "4 2", "042"])
def test_pr_numbers_are_validated_before_any_call(ticket_id: str) -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError, match="not a GitHub issue or PR number"):
        actions.is_merged(pr(ticket_id=ticket_id), HEAD)

    assert gh.calls == []


def test_linear_tickets_are_never_merged_through_github_numbers() -> None:
    gh = RecordedGh()
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=lambda ticket: None,
        runner=gh,
    )
    linked = replace(
        pr("merge", tickets="linear", ticket_id="AUT-12"),
        pr_number="42",
    )

    with pytest.raises(GitHubAdapterError, match="can only use handoff"):
        actions.merge(linked, squash=True, match_head_commit=HEAD)
    with pytest.raises(GitHubAdapterError, match="can only use handoff"):
        actions.fast_forward(linked, branch="main", head=HEAD)

    assert gh.calls == []


def test_set_state_adds_the_state_label_then_removes_the_stale_one() -> None:
    actions, gh = adapter()
    gh.route(LABELS, served("labels-review.json"))
    gh.route(add_label("waiting-external"), served("labels-waiting-external.json"))
    gh.route(remove_label("review"), served("labels-waiting-external.json"))

    actions.set_state(pr(), "waiting-external")

    assert gh.keys() == [LABELS, add_label("waiting-external"), remove_label("review")]


@pytest.mark.parametrize(
    "ticket",
    [pr(tickets="linear", ticket_id="AUT-12"), pr(repo="aut0-code/aut0")],
    ids=["linear", "aut0"],
)
def test_set_state_writes_no_labels_on_linear_or_aut0(ticket: TrackedTicket) -> None:
    actions, gh = adapter()

    actions.set_state(ticket, "merged")

    assert gh.calls == []


def test_set_state_refuses_an_unknown_state() -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError, match="unknown state"):
        actions.set_state(pr(), "merged/../x")

    assert gh.calls == []


@pytest.mark.parametrize(
    ("checks", "status", "expected"),
    [
        ("check-runs-success.json", "status-empty.json", "success"),
        ("check-runs-failure.json", "status-empty.json", "failure"),
        ("check-runs-success.json", "status-failure.json", "failure"),
        ("check-runs-in-progress.json", "status-empty.json", "pending"),
        ("check-runs-empty.json", "status-empty.json", "pending"),
        ("check-runs-all-skipped.json", "status-empty.json", "pending"),
        ("check-runs-all-neutral.json", "status-empty.json", "pending"),
        ("check-runs-empty.json", "status-success.json", "success"),
        ("check-runs-all-skipped.json", "status-success.json", "success"),
        ("check-runs-in-progress.json", "status-success.json", "pending"),
        ("check-runs-failure.json", "status-success.json", "failure"),
        ("check-runs-success.json", "status-pending.json", "pending"),
    ],
    ids=[
        "success-plus-skipped",
        "red-check",
        "red-status",
        "running",
        "nothing-reported",
        "all-skipped",
        "all-neutral",
        "status-success-only",
        "skipped-plus-status-success",
        "running-plus-status-success",
        "red-check-plus-status-success",
        "success-plus-status-pending",
    ],
)
def test_ci_status_reads_check_runs_and_statuses(
    checks: str,
    status: str,
    expected: str,
) -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served(checks))
    gh.route(STATUS, served(status))

    assert actions.ci_status("owner/repo", HEAD).status == expected
    assert gh.keys() == [CHECKS, STATUS]


def test_ci_status_names_the_checks_still_running() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-in-progress.json"))
    gh.route(STATUS, served("status-pending.json"))

    result = actions.ci_status("owner/repo", HEAD)
    assert (result.status, result.pending) == ("pending", ("build", "Vercel"))


def concluded_run(status: str, conclusion: str | None) -> GhResult:
    page = json.loads((FIXTURES / "check-runs-in-progress-concluded.json").read_text())
    page["check_runs"][0].update(status=status, conclusion=conclusion)
    return GhResult(0, json.dumps(page), "")


@pytest.mark.parametrize(
    ("status", "conclusion", "expected", "pending"),
    [
        ("in_progress", "success", "success", ()),
        ("queued", "success", "success", ()),
        ("in_progress", "failure", "failure", ()),
        ("in_progress", "cancelled", "failure", ()),
        ("in_progress", "timed_out", "failure", ()),
        ("in_progress", "neutral", "pending", ()),
        ("in_progress", "skipped", "pending", ()),
        ("in_progress", None, "pending", ("Service Account Key Scan",)),
        ("queued", None, "pending", ("Service Account Key Scan",)),
    ],
)
def test_ci_status_judges_a_check_run_by_its_conclusion_whatever_its_status(
    status: str, conclusion: str | None, expected: str, pending: tuple[str, ...]
) -> None:
    actions, gh = adapter()
    gh.route(CHECKS, concluded_run(status, conclusion))
    gh.route(STATUS, served("status-empty.json"))

    result = actions.ci_status("owner/repo", HEAD)
    assert (result.status, result.pending) == (expected, pending)


def test_ci_status_passes_the_in_progress_run_that_concluded_success() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-in-progress-concluded.json"))
    gh.route(STATUS, served("status-empty.json"))

    assert actions.ci_status("owner/repo", HEAD).status == "success"


def test_ci_status_logs_a_contradictory_check_run_once_per_head() -> None:
    actions, gh = adapter()
    lines: list[str] = []
    actions.warning_sink = lines.append
    gh.route(CHECKS, served("check-runs-in-progress-concluded.json"))
    gh.route(STATUS, served("status-empty.json"))

    actions.ci_status("owner/repo", HEAD)
    actions.ci_status("owner/repo", HEAD)

    assert lines == [
        "check run Service Account Key Scan reports status=in_progress"
        " with conclusion=success; using the conclusion"
    ]


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [("completed", "success"), ("in_progress", None)],
)
def test_ci_status_logs_nothing_for_a_consistent_check_run(
    status: str, conclusion: str | None
) -> None:
    actions, gh = adapter()
    lines: list[str] = []
    actions.warning_sink = lines.append
    gh.route(CHECKS, concluded_run(status, conclusion))
    gh.route(STATUS, served("status-empty.json"))

    actions.ci_status("owner/repo", HEAD)

    assert lines == []


@pytest.mark.parametrize(
    "pages",
    [
        ("check-runs-total-3-page-1.json",),
        ("check-runs-total-3-page-1.json", "check-runs-total-3-page-2.json"),
    ],
    ids=["one-of-three", "two-of-three"],
)
def test_ci_status_is_pending_when_pages_hold_fewer_runs_than_total_count(
    pages: tuple[str, ...],
) -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served(*pages))
    gh.route(STATUS, served("status-success.json"))

    assert actions.ci_status("owner/repo", HEAD).status == "pending"


@pytest.mark.parametrize(
    "pages",
    [
        ("check-runs-total-3-page-1.json", "check-runs-total-2-page-2.json"),
        ("check-runs-total-1-two-runs.json",),
    ],
    ids=["page-counts-disagree", "more-runs-than-total"],
)
def test_ci_status_is_pending_when_total_count_does_not_match_the_runs(
    pages: tuple[str, ...],
) -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served(*pages))
    gh.route(STATUS, served("status-success.json"))

    assert actions.ci_status("owner/repo", HEAD).status == "pending"


def test_ci_status_is_success_when_every_counted_run_arrived() -> None:
    actions, gh = adapter()
    third = (FIXTURES / "check-runs-total-3-page-2.json").read_text()
    third = third.replace("31000000015", "31000000016").replace('"test"', '"lint"')
    gh.route(
        CHECKS,
        GhResult(
            0,
            served("check-runs-total-3-page-1.json", "check-runs-total-3-page-2.json").stdout
            + third,
            "",
        ),
    )
    gh.route(STATUS, served("status-empty.json"))

    assert actions.ci_status("owner/repo", HEAD).status == "success"


TRANSCRIPTS = FIXTURES / "gh-transcripts"
RECORDED_HEAD = "1a6d0c7e74daf528a16153db4787bb856de1db30"


def transcript(name: str) -> GhResult:
    recorded = json.loads((TRANSCRIPTS / name).read_text())
    return GhResult(recorded["returncode"], recorded["stdout"], recorded["stderr"])


def test_ci_status_over_recorded_gh_transcripts() -> None:
    commit = f"repos/maxharris1/factory/commits/{RECORDED_HEAD}"
    actions, gh = adapter()
    gh.route(
        ("--paginate", f"{commit}/check-runs?per_page=100"),
        transcript("check-runs-two-pages.json"),
    )
    gh.route((f"{commit}/status",), transcript("status-none.json"))

    assert actions.ci_status("maxharris1/factory", RECORDED_HEAD).status == "success"


@pytest.mark.parametrize("status", [403, 409, 422])
def test_recorded_gh_http_errors_map_to_a_safe_adapter_error(status: int) -> None:
    recorded = transcript(f"http-{status}.json")
    message = json.loads(recorded.stdout)["message"]
    actions, gh = adapter()
    gh.route(CHECKS, recorded)
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, recorded)

    with pytest.raises(GitHubAdapterError) as checks:
        actions.ci_status("owner/repo", HEAD)
    with pytest.raises(GitHubAdapterError) as merge:
        actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)

    assert checks.value.safe_detail.endswith(f"(HTTP {status})")
    assert message not in checks.value.safe_detail
    assert isinstance(merge.value, HeadMismatchError) is (status == 409)
    assert getattr(merge.value, "retryable", False) is False


def test_handoff_marks_draft_ready() -> None:
    actions, gh = adapter()
    draft = served("pull-42-open.json")
    draft_body = draft.stdout.replace('"draft": false', '"draft": true')
    ready = MARK_READY
    gh.route(PULL, GhResult(0, draft_body, ""))
    gh.route(ready, served("graphql-mark-ready.json"))

    actions.handoff(pr("handoff"), HEAD)

    assert gh.keys() == [PULL, ready]


@pytest.mark.parametrize(
    ("enabled", "expected_writes"),
    [(False, []), (True, [("AUT-42", "Ready for Review")])],
)
def test_linear_handoff_only_marks_pr_ready_and_optionally_moves_rfr(
    enabled: bool,
    expected_writes: list[tuple[str, str]],
) -> None:
    gh = RecordedGh()
    draft = served("pull-42-open.json")
    gh.route(PULL, GhResult(0, draft.stdout.replace('"draft": false', '"draft": true'), ""))
    ready = MARK_READY
    gh.route(ready, served("graphql-mark-ready.json"))
    linear_writes: list[tuple[str, str]] = []
    fresh_reads: list[TicketRef] = []
    actions = LinearReviewActions(
        move_ready_for_review=enabled,
        linear_ready=lambda ticket: linear_writes.append(
            (ticket.ticket_id, "Ready for Review")
        ),
        fresh_ticket=lambda ref: fresh_reads.append(ref) or linked,
        runner=gh,
    )
    linked = replace(
        pr("handoff", tickets="linear", ticket_id="AUT-42"),
        pr_number="42",
        linear_ready_for_review=True,
        linear_state_phase="before-review",
    )

    actions.set_state(linked, "building")
    actions.set_state(linked, "review")
    actions.handoff(linked, HEAD)

    assert linear_writes == expected_writes
    assert fresh_reads == [
        TicketRef(repo="owner/repo", ticket_id="AUT-42", tracker="linear")
    ]
    assert gh.keys() == [PULL, ready]
    assert all("--cache" not in call for call in gh.calls)
    assert MERGE not in gh.keys()


def test_linear_handoff_refuses_changed_linked_pr_before_any_write() -> None:
    gh = RecordedGh()
    linear_writes: list[str] = []
    linked = replace(
        pr("handoff", tickets="linear", ticket_id="AUT-42"),
        pr_number="42",
        linear_ready_for_review=True,
        linear_state_phase="before-review",
    )
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=lambda ticket: linear_writes.append(ticket.ticket_id),
        fresh_ticket=lambda ref: replace(linked, pr_number="43"),
        runner=gh,
    )

    with pytest.raises(
        GitHubAdapterError,
        match="linked pull request changed",
    ):
        actions.handoff(linked, HEAD)

    assert gh.calls == []
    assert linear_writes == []


def test_linear_handoff_retries_only_before_external_writes() -> None:
    def fail(*args) -> None:
        del args
        raise RetryableStateError("budget")

    linked = replace(
        pr("handoff", tickets="linear", ticket_id="AUT-42"),
        pr_number="42",
        linear_ready_for_review=True,
        linear_state_phase="before-review",
    )
    before_write = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=lambda ticket: None,
        fresh_ticket=fail,
        runner=RecordedGh(),
    )

    with pytest.raises(RetryableShipError, match="budget"):
        before_write.handoff(linked, HEAD)

    gh = RecordedGh()
    draft = served("pull-42-open.json")
    gh.route(
        PULL,
        GhResult(
            0,
            draft.stdout.replace('"draft": false', '"draft": true'),
            "",
        ),
    )
    ready = MARK_READY
    gh.route(ready, served("graphql-mark-ready.json"))
    after_write = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=fail,
        fresh_ticket=lambda ref: linked,
        runner=gh,
    )

    with pytest.raises(
        GitHubAdapterError,
        match="update was not confirmed",
    ):
        after_write.handoff(linked, HEAD)

    assert gh.keys() == [PULL, ready]


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (('"merged": false', '"merged": true'), "merged"),
        (('"state": "open"', '"state": "closed"'), "not open"),
        ((HEAD, "b" * 40), "PR head"),
    ],
)
def test_handoff_refuses_unsafe_pr_state_before_writes(
    replacement: tuple[str, str],
    message: str,
) -> None:
    actions, gh = adapter()
    pull = served("pull-42-open.json")
    gh.route(PULL, GhResult(0, pull.stdout.replace(*replacement), ""))

    with pytest.raises(GitHubAdapterError, match=message):
        actions.handoff(pr("handoff"), HEAD)

    assert gh.keys() == [PULL]


def test_review_actions_are_picked_from_factory_md_tickets() -> None:
    assert isinstance(review_actions_for("github"), GitHubReviewActions)
    with pytest.raises(ValueError, match="no ReviewActions adapter for tickets: linear"):
        review_actions_for("linear")


class GitHubFixtureSource:
    """A PollSource whose comments and CI come from the real adapter."""

    def __init__(self, actions: GitHubReviewActions, ship: str):
        self.actions = actions
        self.ship = ship

    def _ticket(self) -> TrackedTicket:
        return TrackedTicket(
            repo="owner/repo",
            ticket_id="42",
            factory=factory(self.ship),
            comments=self.actions.list_comments("owner/repo", "42"),
            current_head=HEAD,
            ci={HEAD: self.actions.ci_status("owner/repo", HEAD)},
            risk="high",
        )

    def github_candidates(self, label_prefix: str, *, exclude=frozenset()) -> tuple[TrackedTicket, ...]:
        del label_prefix
        return (self._ticket(),)

    def linear_candidates(
        self,
        query: str,
        *,
        exclude: frozenset[str],
        reserve: int,
    ) -> tuple[TrackedTicket, ...]:
        del query, exclude, reserve
        return ()

    def tracked_tickets(self, refs: tuple[TicketRef, ...]) -> tuple[TrackedTicket, ...]:
        if not any(ref.ticket_id == "42" for ref in refs):
            return ()
        return (self._ticket(),)


class CatchingShipper:
    def __init__(self, inner: ReviewShipper):
        self.inner = inner
        self.requests: list[ShipRequest] = []
        self.errors: list[BaseException] = []

    def ship(self, request: ShipRequest) -> None:
        self.requests.append(request)
        try:
            self.inner.ship(request)
        except BaseException as exc:
            self.errors.append(exc)
            raise


class UnavailableGrokDelivery:
    # Fails on purpose: a failed delivery leaves each Grok wake queued, so
    # FakeBot(queue, "grok").drain() can count the wakes. A delivery that
    # succeeded would mark them done and the drains would see none.
    def deliver(self, job: Job, *, logical_key: str) -> str:
        del job, logical_key
        raise RuntimeError("Grok delivery unavailable in GitHub adapter tests")


def github_dispatcher(
    tmp_path: Path,
    ship: str = "after:tracy-site",
) -> tuple[Dispatcher, SQLiteJobQueue, RecordedGh, GitHubFixtureSource, CatchingShipper]:
    gh = RecordedGh()
    actions = GitHubReviewActions(gh)
    source = GitHubFixtureSource(actions, ship)
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    t8 = T8Hook()
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=actions,
        grok_delivery=UnavailableGrokDelivery(),
        hooks=(T7Hook(), t8),
    )
    shipper = CatchingShipper(ReviewShipper(dispatcher, source))
    t8.shipper = shipper
    in_review(queue, HEAD)
    gh.route(CHECKS, served("check-runs-success.json"))
    gh.route(STATUS, served("status-empty.json"))
    return dispatcher, queue, gh, source, shipper


def approve_then_ok(tmp_path: Path) -> tuple[Dispatcher, SQLiteJobQueue, RecordedGh, CatchingShipper]:
    """Poll 1 reads the APPROVE; poll 2's comment pages add the ok."""
    dispatcher, queue, gh, source, shipper = github_dispatcher(tmp_path)
    gh.route(COMMENTS, served("e2e-comments-approve.json"))
    gh.route(PREVIEW, served("issue-comment-201.json"))
    gh.route(LABELS, served("labels-review.json"))
    gh.route(add_label("waiting-external"), served("labels-waiting-external.json"))
    gh.route(remove_label("review"), served("labels-waiting-external.json"))

    dispatcher.poll_once(source)

    assert outcomes(queue)["10"] == "waiting-external"
    assert gh.count(PREVIEW) == 1
    gh.route(COMMENTS, served("e2e-comments-approve.json", "e2e-comments-ok.json"))
    gh.route(LABELS, served("labels-waiting-external.json"))
    gh.route(add_label("merged"), served("labels-empty.json"))
    gh.route(remove_label("waiting-external"), served("labels-empty.json"))
    gh.route(PULL, served("pull-42-open.json"))
    return dispatcher, queue, gh, shipper


def test_e2e_approve_ok_fast_forward_shipped_with_the_real_adapter(
    tmp_path: Path,
) -> None:
    dispatcher, queue, gh, shipper = approve_then_ok(tmp_path)
    source = shipper.inner.source
    gh.route(TIP, served("ref-main-base.json"), served("ref-main-head.json"))
    gh.route(FF, served("ref-main-head.json"))
    before = len(gh.calls)

    dispatcher.poll_once(source)

    assert outcomes(queue)["11"] == "shipped"
    assert shipper.errors == []
    assert [request.head for request in shipper.requests] == [HEAD]
    assert workflow_state(queue) == "merged"
    ship_calls = [key for key in gh.keys()[before:] if key in (TIP, PULL, FF, MERGE)]
    assert ship_calls == [TIP, PULL, FF, TIP]
    assert gh.count(MERGE) == 0
    assert gh.count(add_label("merged")) == 1
    assert gh.count(remove_label("waiting-external")) == 1
    assert not any("force=true" in part for call in gh.calls for part in call)
    assert all(call[:10] == ENV_PREFIX for call in gh.calls)
    assert FakeBot(queue, "grok").drain() == []

    dispatcher.poll_once(source)
    assert gh.count(FF) == 1


@pytest.mark.parametrize(
    ("failing", "response", "error"),
    [
        (FF, http_error("ref-not-fast-forward-422.json", 422), "NotFastForwardError"),
        (TIP, GhResult(1, "", "gh: Server Error (HTTP 502)\n"), "GitHubAdapterError"),
        (TIP, GhResult(0, "{", ""), "GitHubAdapterError"),
        (TIP, TIMEOUT, "GitHubAdapterError"),
        (PULL, GhResult(1, "", "gh: Bad credentials (HTTP 401)\n"), "GitHubAdapterError"),
    ],
    ids=["ff-422", "tip-gh-exit-1", "tip-malformed-json", "tip-timeout", "pull-gh-exit-1"],
)
def test_e2e_adapter_errors_fail_closed_with_ship_error_and_one_wake(
    tmp_path: Path,
    failing: tuple[str, ...],
    response: GhResult | BaseException,
    error: str,
) -> None:
    dispatcher, queue, gh, shipper = approve_then_ok(tmp_path)
    source = shipper.inner.source
    gh.route(TIP, served("ref-main-base.json"))
    gh.route(FF, served("ref-main-head.json"))
    gh.route(failing, response)

    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    assert outcomes(queue)["11"] == "ship-failed"
    assert len(shipper.requests) == 1
    assert len(shipper.errors) == 1
    assert isinstance(shipper.errors[0], ShipError)
    assert type(shipper.errors[0]).__name__ == error
    assert gh.count(FF) == (1 if failing == FF else 0)
    assert gh.count(MERGE) == 0
    assert gh.count(add_label("merged")) == 0
    assert workflow_state(queue) != "merged"
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert f"raised {error}" in str(grok[0].request.detail)
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT status FROM external_ship_intents"
        ).fetchone()[0] == "failed"
        assert connection.execute(
            "SELECT outcome FROM external_previews"
        ).fetchone()[0] == "ship-failed"


@pytest.mark.usefixtures("every_wake_reaches_grok")
@pytest.mark.parametrize("checks", ["check-runs-all-neutral.json", "check-runs-all-skipped.json"])
def test_e2e_untested_head_never_reaches_t4_ship(tmp_path: Path, checks: str) -> None:
    for ship in ("after:tracy-site", "merge"):
        dispatcher, queue, gh, source, shipper = github_dispatcher(tmp_path / ship, ship)
        gh.route(CHECKS, served(checks))
        gh.route(COMMENTS, served("e2e-comments-approve.json", "e2e-comments-ok.json"))

        dispatcher.poll_once(source)
        dispatcher.poll_once(source)

        assert "10" not in outcomes(queue)
        assert outcomes(queue)["11"] in ("rejected-ok-not-waiting", "rejected-ok-not-after-ship")
        assert shipper.requests == []
        for key in (PREVIEW, PULL, TIP, FF, MERGE):
            assert gh.count(key) == 0
        assert not any(call[11:12] == ["-X"] for call in gh.calls)
        with queue.connect() as connection:
            assert connection.execute("SELECT count(*) FROM ship_intents").fetchone()[0] == 0


def test_e2e_all_neutral_ci_at_ok_time_is_refused_by_review_shipper(
    tmp_path: Path,
) -> None:
    dispatcher, queue, gh, shipper = approve_then_ok(tmp_path)
    source = shipper.inner.source
    gh.route(CHECKS, served("check-runs-all-neutral.json"))
    gh.route(TIP, served("ref-main-base.json"))
    gh.route(FF, served("ref-main-head.json"))

    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    assert outcomes(queue)["11"] == "ship-failed"
    assert len(shipper.errors) == 1
    assert isinstance(shipper.errors[0], ShipError)
    assert "is not green" in str(shipper.errors[0])
    assert gh.count(FF) == 0
    assert gh.count(MERGE) == 0
    assert workflow_state(queue) != "merged"
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["ship-failed"]


def test_e2e_squash_repo_merges_with_match_head_on_approve(tmp_path: Path) -> None:
    dispatcher, queue, gh, source, _shipper = github_dispatcher(tmp_path, "merge")
    gh.route(COMMENTS, served("e2e-comments-approve.json"))
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, served("merge-squash-200.json"))
    gh.route(LABELS, served("labels-review.json"))
    gh.route(add_label("merged"), served("labels-empty.json"))
    gh.route(remove_label("review"), served("labels-empty.json"))

    dispatcher.poll_once(source)

    assert outcomes(queue)["10"] == "merged"
    assert gh.count(MERGE) == 1
    assert gh.count(FF) == 0
    assert workflow_state(queue) == "merged"


def test_e2e_squash_head_mismatch_fails_closed_with_one_wake(tmp_path: Path) -> None:
    dispatcher, queue, gh, source, _shipper = github_dispatcher(tmp_path, "merge")
    gh.route(COMMENTS, served("e2e-comments-approve.json"))
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, http_error("merge-head-modified-409.json", 409))

    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    assert outcomes(queue)["10"] == "failed-verdict-HeadMismatchError"
    assert gh.count(MERGE) == 1
    assert gh.count(FF) == 0
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert "raised HeadMismatchError" in str(grok[0].request.detail)
    with queue.connect() as connection:
        assert connection.execute("SELECT status FROM ship_intents").fetchone()[0] == "failed"


def test_e2e_settle_that_keeps_failing_on_gh_wakes_grok_once(tmp_path: Path) -> None:
    dispatcher, queue, gh, shipper = approve_then_ok(tmp_path)
    source = shipper.inner.source
    gh.route(TIP, served("ref-main-base.json"), served("ref-main-head.json"))
    gh.route(FF, served("ref-main-head.json"))
    gh.route(LABELS, GhResult(1, "", "gh: Server Error (HTTP 502)\n"))

    for _poll in range(3):
        dispatcher.poll_once(source)

    assert outcomes(queue)["11"] == "shipped"
    assert gh.count(FF) == 1
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert "write failed 3 times: RetryableStateError: merged" in str(
        grok[0].request.detail
    )
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT status FROM external_ship_intents"
        ).fetchone()[0] == "completed"


def test_ci_result_feeds_the_dispatcher_as_a_ci_result() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-failure.json"))
    gh.route(STATUS, served("status-empty.json"))

    result = actions.ci_status("owner/repo", HEAD)

    assert result == CIResult(
        "failure", "failed: typecheck", failed=(FailedCheck("typecheck", cause="code"),)
    )
    assert result.cause == "code"


def test_ci_status_names_the_actions_run_and_job_of_each_failed_check() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-failure-actions.json"))
    gh.route(STATUS, served("status-failure.json"))

    result = actions.ci_status("owner/repo", HEAD)

    assert result.log == "failed: pytest, lint, commit status"
    assert result.failed == (
        FailedCheck(
            "pytest",
            run_id="123456",
            job_id="31000000004",
            run_url="https://github.com/owner/repo/actions/runs/123456",
            cause="code",
        ),
        FailedCheck("lint", cause="code"),
        FailedCheck("commit status", cause="code"),
    )


PICKUP_RUN = "133415718662"
ANNOTATIONS = ("--paginate", f"{REPO}/check-runs/{PICKUP_RUN}/annotations?per_page=100")


def test_ci_status_reads_a_run_not_acquired_by_a_runner_as_infra() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-cancelled-pickup.json"))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(ANNOTATIONS, served("annotations-not-acquired.json"))

    result = actions.ci_status("owner/repo", HEAD)

    assert (result.status, result.cause) == ("failure", "infra")
    assert [(check.name, check.cause, check.signal) for check in result.failed] == [
        ("macos", "infra", "runner not acquired")
    ]


def test_ci_status_reads_a_completed_runs_annotations_once() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-cancelled-pickup.json"))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(ANNOTATIONS, served("annotations-not-acquired.json"))

    first = actions.ci_status("owner/repo", HEAD)
    second = actions.ci_status("owner/repo", HEAD)

    assert first == second
    assert second.cause == "infra"
    assert gh.count(ANNOTATIONS) == 1


def test_a_failure_annotation_past_the_first_page_classifies_the_run() -> None:
    notice = {"annotation_level": "notice", "title": "", "message": "Queue times may be longer."}
    lost = {
        "annotation_level": "failure",
        "title": "",
        "message": "The hosted runner: GitHub Actions 7 lost communication with the server.",
    }
    pages = json.dumps([notice] * 100) + json.dumps([lost])
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-cancelled-pickup.json"))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(ANNOTATIONS, GhResult(0, pages, ""))

    first = actions.ci_status("owner/repo", HEAD)
    second = actions.ci_status("owner/repo", HEAD)

    assert [(check.cause, check.signal) for check in first.failed] == [("infra", "runner lost")]
    assert second == first
    assert gh.count(ANNOTATIONS) == 1


def test_an_unreadable_annotations_page_leaves_the_cause_unknown_and_is_read_again() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-cancelled-pickup.json"))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(
        ANNOTATIONS,
        GhResult(1, "", "gh: Server Error (HTTP 502)\n"),
        served("annotations-not-acquired.json"),
    )

    failed = actions.ci_status("owner/repo", HEAD)
    retried = actions.ci_status("owner/repo", HEAD)

    assert (failed.status, failed.cause) == ("failure", "unknown")
    assert [(check.cause, check.signal) for check in failed.failed] == [("unknown", None)]
    assert retried.cause == "infra"
    assert gh.count(ANNOTATIONS) == 2


def test_annotations_of_a_run_not_yet_completed_are_read_again() -> None:
    page = json.loads((FIXTURES / "check-runs-cancelled-pickup.json").read_text())
    page["check_runs"][0]["status"] = "in_progress"
    actions, gh = adapter()
    gh.route(CHECKS, GhResult(0, json.dumps(page), ""))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(ANNOTATIONS, served("annotations-not-acquired.json"))

    actions.ci_status("owner/repo", HEAD)
    result = actions.ci_status("owner/repo", HEAD)

    assert result.cause == "infra"
    assert gh.count(ANNOTATIONS) == 2


@pytest.mark.parametrize("count", [0, None])
def test_a_failed_run_without_annotations_reads_no_annotations(count: int | None) -> None:
    page = json.loads((FIXTURES / "check-runs-cancelled-pickup.json").read_text())
    output = page["check_runs"][0]["output"]
    if count is None:
        del output["annotations_count"]
    else:
        output["annotations_count"] = count
    actions, gh = adapter()
    gh.route(CHECKS, GhResult(0, json.dumps(page), ""))
    gh.route(STATUS, served("status-empty.json"))

    result = actions.ci_status("owner/repo", HEAD)

    assert (result.status, result.cause) == ("failure", "unknown")
    assert gh.count(ANNOTATIONS) == 0


@pytest.mark.parametrize(("state", "cause"), [("failure", "code"), ("error", "unknown")])
def test_a_red_commit_status_is_code_only_when_it_failed(state: str, cause: str) -> None:
    combined = json.loads((FIXTURES / "status-failure.json").read_text())
    combined["state"] = state
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-empty.json"))
    gh.route(STATUS, GhResult(0, json.dumps(combined), ""))

    result = actions.ci_status("owner/repo", HEAD)

    assert result.failed == (FailedCheck("commit status", cause=cause),)
    assert result.cause == cause


def test_ci_status_reads_an_infra_signal_in_the_check_output() -> None:
    page = json.loads((FIXTURES / "check-runs-failure.json").read_text())
    page["check_runs"][0]["output"] = {
        "title": "Billing",
        "summary": "The job was not started because recent account payments have failed.",
        "annotations_count": 0,
    }
    actions, gh = adapter()
    gh.route(CHECKS, GhResult(0, json.dumps(page), ""))
    gh.route(STATUS, served("status-empty.json"))

    result = actions.ci_status("owner/repo", HEAD)

    assert result.failed == (
        FailedCheck("typecheck", cause="infra", signal="billing or spending limit"),
    )


def epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()


def test_ci_status_lists_every_latest_attempt_check_without_another_read() -> None:
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-failure-actions.json"))
    gh.route(STATUS, served("status-failure.json"))

    result = actions.ci_status("owner/repo", HEAD)

    assert result.checks == (
        CheckRow(
            "pytest",
            "completed",
            "failure",
            started_at=epoch("2026-11-10T19:50:00"),
            completed_at=epoch("2026-11-10T19:55:00"),
            log_url="https://github.com/owner/repo/actions/runs/123456/job/31000000004",
            run_id="123456",
            job_id="31000000004",
            workflow="actions-run:123456",
        ),
        CheckRow(
            "lint",
            "completed",
            "failure",
            started_at=epoch("2026-11-10T19:50:00"),
            completed_at=epoch("2026-11-10T19:56:00"),
            workflow="suite:77",
        ),
    )
    assert gh.keys() == [CHECKS, STATUS]


@pytest.mark.parametrize(
    ("status", "conclusion", "row_status", "started"),
    [
        ("in_progress", None, "in_progress", True),
        ("queued", None, "queued", False),
        ("waiting", None, "queued", False),
        ("requested", None, "queued", False),
        ("pending", None, "queued", False),
        ("in_progress", "success", "completed", True),
    ],
)
def test_ci_status_maps_each_check_run_status_onto_the_board_statuses(
    status: str, conclusion: str | None, row_status: str, started: bool
) -> None:
    actions, gh = adapter()
    gh.route(CHECKS, concluded_run(status, conclusion))
    gh.route(STATUS, served("status-empty.json"))

    (row,) = actions.ci_status("owner/repo", HEAD).checks

    assert (row.status, row.conclusion) == (row_status, conclusion)
    assert (row.started_at is not None) is started


def test_ci_status_keeps_the_latest_attempt_check_only() -> None:
    actions, gh = adapter()
    gh.route(STATUS, served("status-empty.json"))
    page = json.loads((FIXTURES / "check-runs-failure-actions.json").read_text())
    first = dict(page["check_runs"][0])
    rerun = dict(
        first,
        id=31000000009,
        status="in_progress",
        conclusion=None,
        completed_at=None,
        started_at="2026-11-10T20:10:00Z",
        details_url="https://github.com/owner/repo/actions/runs/123456/job/31000000009",
    )
    gh.route(CHECKS, GhResult(0, json.dumps({"total_count": 2, "check_runs": [first, rerun]}), ""))

    result = actions.ci_status("owner/repo", HEAD)

    assert result.status == "pending"
    assert [(row.name, row.status, row.job_id) for row in result.checks] == [
        ("pytest", "in_progress", "31000000009")
    ]


def test_ci_status_is_the_same_gate_whatever_the_check_times_hold() -> None:
    actions, gh = adapter()
    page = json.loads((FIXTURES / "check-runs-failure-actions.json").read_text())
    page["check_runs"][0].update(started_at="soon", completed_at=17)
    gh.route(CHECKS, GhResult(0, json.dumps(page), ""))
    gh.route(STATUS, served("status-empty.json"))

    result = actions.ci_status("owner/repo", HEAD)

    assert result.status == "failure"
    assert (result.checks[0].started_at, result.checks[0].completed_at) == (None, None)


RUN_JOBS = (f"{REPO}/actions/runs/123456/jobs?per_page=100",)


def test_run_jobs_names_each_jobs_runner_in_one_read() -> None:
    actions, gh = adapter()
    gh.route(RUN_JOBS, served("run-jobs.json"))

    jobs = actions.run_jobs("owner/repo", "123456")

    queued = epoch("2026-11-10T19:49:30")
    assert jobs == (
        RunJob(
            "31000000004",
            "pytest",
            "completed",
            created_at=queued,
            started_at=epoch("2026-11-10T19:50:00"),
            completed_at=epoch("2026-11-10T19:55:00"),
            runner_name="GitHub Actions 1000000123",
            runner_kind="github-hosted",
        ),
        RunJob(
            "31000000006",
            "e2e",
            "in_progress",
            created_at=queued,
            started_at=epoch("2026-11-10T19:52:10"),
            runner_name="pc-wsl-bots-2",
            runner_kind="self-hosted",
        ),
        RunJob("31000000007", "lint", "queued", created_at=queued),
        RunJob(
            "31000000008",
            "deploy",
            "completed",
            created_at=queued,
            started_at=queued,
            completed_at=queued,
            runner_kind="none",
        ),
    )
    assert gh.keys() == [RUN_JOBS]


def test_run_jobs_counts_each_jobs_steps_and_a_job_that_never_started_has_none() -> None:
    actions, gh = adapter()
    page = json.loads((FIXTURES / "run-jobs.json").read_text())
    page["jobs"][0]["steps"] = [{"name": "Set up job"}, {"name": "Run tests"}]
    page["jobs"][3]["steps"] = []
    gh.route(RUN_JOBS, GhResult(0, json.dumps(page), ""))

    jobs = actions.run_jobs("owner/repo", "123456")

    assert [(job.name, job.steps) for job in jobs] == [
        ("pytest", 2),
        ("e2e", None),
        ("lint", None),
        ("deploy", 0),
    ]


def test_run_jobs_refuses_a_run_id_that_is_not_a_number() -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError):
        actions.run_jobs("owner/repo", "../secrets")
    assert gh.calls == []


def test_run_jobs_refuses_a_page_without_a_jobs_list() -> None:
    actions, gh = adapter()
    gh.route(RUN_JOBS, GhResult(0, json.dumps({"total_count": 0}), ""))

    with pytest.raises(GitHubAdapterError):
        actions.run_jobs("owner/repo", "123456")


RUNNERS = ("--cache", "60s", f"{REPO}/actions/runners?per_page=100")


def test_list_runners_reads_the_repos_self_hosted_pool_in_one_cached_read() -> None:
    actions, gh = adapter()
    gh.route(RUNNERS, served("runners.json"))

    runners = actions.list_runners("owner/repo")

    linux = ("self-hosted", "Linux", "X64")
    assert runners == (
        PoolRunner("pc-wsl-bots-1", "Linux", "online", False, linux),
        PoolRunner("pc-wsl-bots-2", "Linux", "online", True, linux),
        PoolRunner("box-1", "Linux", "offline", False, linux),
        PoolRunner(
            "maxs-macbook-pro", "macOS", "online", False,
            ("self-hosted", "macOS", "ARM64", "factory-mac"),
        ),
    )
    assert gh.keys() == [RUNNERS]


def test_list_runners_refuses_a_page_without_a_runners_list() -> None:
    actions, gh = adapter()
    gh.route(RUNNERS, GhResult(0, json.dumps({"total_count": 0}), ""))

    with pytest.raises(GitHubAdapterError):
        actions.list_runners("owner/repo")


JOB_LOG = (f"{REPO}/actions/jobs/31000000004/logs", "--allow-escape-sequences")


def test_job_log_reads_one_coloured_jobs_log_in_one_api_request() -> None:
    actions, gh = adapter()
    text = "2026-11-10T19:50:02Z \x1b[31mFAILED\x1b[0m tests/test_a.py::test_b\n"
    gh.route(JOB_LOG, GhResult(0, text, ""))

    assert actions.job_log("owner/repo", "31000000004") == text
    assert gh.calls == [[*GH, "api", *JOB_LOG]]


def test_job_log_refuses_a_job_id_that_is_not_a_number() -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError):
        actions.job_log("owner/repo", "../secrets")
    assert gh.calls == []


RUN_LOG = ("run", "view", "123456", "--repo", "owner/repo", "--log-failed")


def test_failed_run_logs_reads_the_run_once_and_splits_it_by_job() -> None:
    actions, gh = adapter()
    gh.route(
        RUN_LOG,
        GhResult(
            0,
            "pytest\tRun tests\t2026-11-10T19:50:00Z a\n"
            "lint\tRun lint\t2026-11-10T19:50:01Z b\tc\n"
            "pytest\tRun tests\t2026-11-10T19:50:02Z FAILED x\n",
            "",
        ),
    )

    assert actions.failed_run_logs("owner/repo", "123456") == {
        "pytest": "2026-11-10T19:50:00Z a\n2026-11-10T19:50:02Z FAILED x",
        "lint": "2026-11-10T19:50:01Z b\tc",
    }
    assert gh.count(RUN_LOG) == 1
    with pytest.raises(GitHubAdapterError):
        actions.failed_run_logs("owner/repo", "../secrets")


def test_failed_run_logs_keeps_a_job_whose_name_starts_like_a_status_line() -> None:
    actions, gh = adapter()
    gh.route(RUN_LOG, GhResult(0, "HTTP/2 compatibility\tRun tests\tFAILED x\n", ""))

    assert actions.failed_run_logs("owner/repo", "123456") == {"HTTP/2 compatibility": "FAILED x"}


def test_job_log_keeps_a_log_that_starts_like_a_status_line() -> None:
    actions, gh = adapter()
    text = "HTTP/1.1 200 OK\n\nFAILED tests/test_a.py::test_b\n"
    gh.route(JOB_LOG, GhResult(0, text, ""))

    assert actions.job_log("owner/repo", "31000000004") == text


def draft_pull() -> GhResult:
    pull = served("pull-42-open.json")
    return GhResult(0, pull.stdout.replace('"draft": false', '"draft": true'), "")


def test_handoff_marks_a_draft_ready_with_the_exact_graphql_gh_argv() -> None:
    """Issue #47: POST pulls/<n>/ready_for_review is not a GitHub REST route."""
    actions, gh = adapter()
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))

    actions.handoff(pr("handoff"), HEAD)

    assert gh.calls == [
        [*ENV_PREFIX, "api", *PULL],
        [
            *ENV_PREFIX,
            "api",
            "-X",
            "POST",
            "graphql",
            "-f",
            "query=mutation($id: ID!) { markPullRequestReadyForReview("
            "input: {pullRequestId: $id}) { pullRequest { id isDraft headRefOid } } }",
            "-f",
            "id=PR_kwDOKfac0s6CiTKq",
            "-f",
            "owner=owner",
        ],
    ]
    assert not any("ready_for_review" in part for call in gh.calls for part in call)


def test_handoff_of_a_ready_pr_sends_no_mutation() -> None:
    actions, gh = adapter()
    gh.route(PULL, served("pull-42-open.json"))

    actions.handoff(pr("handoff"), HEAD)

    assert gh.keys() == [PULL]


def test_the_mark_ready_mutation_is_never_cached() -> None:
    gh = RecordedGh()
    actions = GitHubReviewActions(gh, cache_ttl="60s")
    gh.route(("--cache", "60s", *PULL), draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))

    actions.handoff(pr("handoff"), HEAD)

    assert gh.keys() == [("--cache", "60s", *PULL), MARK_READY]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            GhResult(0, '{"data": null, "errors": [{"type": "NOT_FOUND", "message": "x"}]}', ""),
            "returned errors",
        ),
        (
            GhResult(
                0,
                served("graphql-mark-ready.json").stdout.replace(
                    '"isDraft": false', '"isDraft": true'
                ),
                "",
            ),
            "did not confirm",
        ),
        (GhResult(1, '{"errors": [{"type": "FORBIDDEN"}]}', "gh: Forbidden (HTTP 403)\n"), "gh exited 1"),
        (TIMEOUT, "timed out"),
    ],
    ids=["graphql-errors", "still-draft", "gh-exit-1", "timeout"],
)
def test_an_unconfirmed_mark_ready_has_no_confirmed_write(
    response: GhResult | BaseException,
    message: str,
) -> None:
    actions, gh = adapter()
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, response)

    with pytest.raises(GitHubAdapterError, match=message) as raised:
        actions.handoff(pr("handoff"), HEAD)

    assert raised.value.write_confirmed is False
    assert "http" not in raised.value.safe_detail
    assert "{" not in raised.value.safe_detail


def test_a_draft_without_a_node_id_fails_before_the_mutation() -> None:
    actions, gh = adapter()
    pull = draft_pull()
    gh.route(PULL, GhResult(0, pull.stdout.replace(f'"node_id": "{NODE_ID}"', '"node_id": null'), ""))

    with pytest.raises(GitHubAdapterError, match="node_id") as raised:
        actions.handoff(pr("handoff"), HEAD)

    assert raised.value.write_confirmed is False
    assert gh.keys() == [PULL]


def test_a_pr_marked_ready_at_another_head_is_a_confirmed_write() -> None:
    actions, gh = adapter()
    gh.route(PULL, draft_pull())
    moved = served("graphql-mark-ready.json").stdout.replace(HEAD, "c" * 40)
    gh.route(MARK_READY, GhResult(0, moved, ""))

    with pytest.raises(HeadMismatchError, match="marked ready") as raised:
        actions.handoff(pr("handoff"), HEAD)

    assert raised.value.write_confirmed is True


@pytest.mark.parametrize(("draft", "confirmed"), [(True, True), (False, False)])
def test_an_unconfirmed_linear_move_reports_whether_the_pr_write_landed(
    draft: bool,
    confirmed: bool,
) -> None:
    def fail(ticket: TrackedTicket) -> None:
        del ticket
        raise RetryableStateError("budget")

    gh = RecordedGh()
    gh.route(PULL, draft_pull() if draft else served("pull-42-open.json"))
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    linked = replace(
        pr("handoff", tickets="linear", ticket_id="AUT-42"),
        pr_number="42",
        linear_ready_for_review=True,
        linear_state_phase="before-review",
    )
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=fail,
        runner=gh,
    )

    with pytest.raises(GitHubAdapterError, match="not confirmed") as raised:
        actions.handoff(linked, HEAD)

    assert raised.value.write_confirmed is confirmed


def test_gh_failure_safe_detail_keeps_status_but_no_output() -> None:
    actions, gh = adapter()
    gh.route(
        TIP,
        GhResult(
            1,
            '{"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"}',
            "gh: Bad credentials (HTTP 401)\n",
        ),
    )

    with pytest.raises(GitHubAdapterError) as raised:
        actions.branch_tip(pr(), "main")

    assert raised.value.safe_detail == "gh api GET git/ref/heads/main exited 1 (HTTP 401)"


# Codex r1 F1.2: the Linear move is made only from the issue's pre-review
# state, read fresh at handoff time.


@pytest.mark.parametrize(
    ("phase", "draft", "writes"),
    [
        ("before-review", True, ["AUT-42"]),
        ("before-review", False, ["AUT-42"]),
        ("ready-for-review", True, []),
        ("ready-for-review", False, []),
        ("moved-on", True, []),
        ("moved-on", False, []),
        ("rework", True, ["AUT-42"]),
        ("rework", False, ["AUT-42"]),
    ],
    ids=[
        "before-review-draft",
        "before-review-ready-pr",
        "already-rfr-draft",
        "already-rfr-no-op",
        "moved-on-draft",
        "moved-on-ready-pr",
        "rework-draft",
        "rework-ready-pr",
    ],
)
def test_linear_handoff_moves_only_an_issue_still_before_review(
    phase: str,
    draft: bool,
    writes: list[str],
) -> None:
    gh = RecordedGh()
    gh.route(PULL, draft_pull() if draft else served("pull-42-open.json"))
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    linear_writes: list[str] = []
    snapshot = replace(
        pr("handoff", tickets="linear", ticket_id="AUT-42"),
        pr_number="42",
        linear_ready_for_review=True,
        linear_state_phase="before-review",
    )
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=lambda ticket: linear_writes.append(ticket.ticket_id),
        fresh_ticket=lambda ref: replace(snapshot, linear_state_phase=phase),
        runner=gh,
    )

    actions.handoff(snapshot, HEAD)

    assert linear_writes == writes
    assert gh.keys() == ([PULL, MARK_READY] if draft else [PULL])


@pytest.mark.parametrize("draft", [True, False])
def test_linear_handoff_with_an_unknown_issue_state_fails_closed_before_any_write(
    draft: bool,
) -> None:
    gh = RecordedGh()
    gh.route(PULL, draft_pull() if draft else served("pull-42-open.json"))
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    linear_writes: list[str] = []
    linked = replace(
        pr("handoff", tickets="linear", ticket_id="AUT-42"),
        pr_number="42",
        linear_ready_for_review=True,
    )
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=lambda ticket: linear_writes.append(ticket.ticket_id),
        fresh_ticket=lambda ref: linked,
        runner=gh,
    )

    with pytest.raises(GitHubAdapterError, match="Linear issue state is unavailable") as raised:
        actions.handoff(linked, HEAD)

    assert raised.value.write_confirmed is False
    assert gh.calls == []
    assert linear_writes == []


# Codex r1 F1.3: the merge 409 and fast-forward 422 wrappers carry only the
# inner safe detail, never gh's stderr or stdout.

LEAKED_TOKEN = "ghp_" + "Q" * 36


def leaky_http_error(name: str, status: int) -> GhResult:
    body = (FIXTURES / name).read_text()
    message = body.split('"message": "', 1)[1].split('"', 1)[0]
    stdout = body.rstrip().removesuffix("}") + f', "token": "{LEAKED_TOKEN}"}}'
    stderr = (
        f"gh: {message} (HTTP {status})\n"
        f"https://api.github.com/repos/owner/repo?access_token={LEAKED_TOKEN}\n"
    )
    return GhResult(1, stdout, stderr)


def assert_no_gh_output(text: str, message: str) -> None:
    for forbidden in (
        message,
        LEAKED_TOKEN,
        "ghp_",
        "http",
        "docs.github.com",
        "api.github.com",
        "documentation_url",
        "{",
    ):
        assert forbidden not in text, (forbidden, text)


@pytest.mark.parametrize(
    ("ship", "response", "error", "safe", "message"),
    [
        (
            "merge",
            leaky_http_error("merge-head-modified-409.json", 409),
            HeadMismatchError,
            f"PR head is not {HEAD}: gh api PUT pulls/42/merge exited 1 (HTTP 409)",
            "Head branch was modified",
        ),
        (
            "fast-forward",
            leaky_http_error("ref-not-fast-forward-422.json", 422),
            NotFastForwardError,
            f"{HEAD} is not a fast-forward of main: "
            "gh api PATCH git/refs/heads/main exited 1 (HTTP 422)",
            "Update is not a fast forward",
        ),
    ],
    ids=["merge-409", "fast-forward-422"],
)
def test_merge_and_fast_forward_wrappers_keep_gh_output_out_of_the_error(
    ship: str,
    response: GhResult,
    error: type[GitHubAdapterError],
    safe: str,
    message: str,
) -> None:
    actions, gh = adapter()
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE if ship == "merge" else FF, response)

    with pytest.raises(error) as raised:
        if ship == "merge":
            actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)
        else:
            actions.fast_forward(pr("merge"), branch="main", head=HEAD)

    assert raised.value.safe_detail == safe
    assert_no_gh_output(raised.value.safe_detail, message)
    assert_no_gh_output(str(raised.value), message)


def test_e2e_merge_409_records_no_gh_output_in_ship_intents(tmp_path: Path) -> None:
    dispatcher, queue, gh, source, _shipper = github_dispatcher(tmp_path, "merge")
    gh.route(COMMENTS, served("e2e-comments-approve.json"))
    gh.route(PULL, served("pull-42-open.json"))
    gh.route(MERGE, leaky_http_error("merge-head-modified-409.json", 409))

    dispatcher.poll_once(source)

    with queue.connect() as connection:
        error = connection.execute("SELECT error FROM ship_intents").fetchone()[0]
    assert error == (
        f"HeadMismatchError: PR head is not {HEAD}: "
        "gh api PUT pulls/42/merge exited 1 (HTTP 409)"
    )
    assert_no_gh_output(error, "Head branch was modified")
    grok = FakeBot(queue, "grok").drain()
    detail = json.loads(str(grok[0].request.detail))["detail"]
    assert detail == f"ship raised {error}"
    assert_no_gh_output(detail, "Head branch was modified")


def test_e2e_fast_forward_422_records_no_gh_output_in_external_ship_intents(
    tmp_path: Path,
) -> None:
    dispatcher, queue, gh, shipper = approve_then_ok(tmp_path)
    source = shipper.inner.source
    gh.route(TIP, served("ref-main-base.json"))
    gh.route(FF, leaky_http_error("ref-not-fast-forward-422.json", 422))

    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    with queue.connect() as connection:
        error = connection.execute(
            "SELECT error FROM external_ship_intents"
        ).fetchone()[0]
    assert error == (
        f"NotFastForwardError: {HEAD} is not a fast-forward of main: "
        "gh api PATCH git/refs/heads/main exited 1 (HTTP 422)"
    )
    assert_no_gh_output(error, "Update is not a fast forward")


# #48 G2 F2.3: a failed confirming tip read after a fast-forward records only
# the inner safe detail in external_ship_intents.error, never gh's output.


def test_e2e_unreadable_tip_after_fast_forward_records_no_gh_output(
    tmp_path: Path,
) -> None:
    dispatcher, queue, gh, shipper = approve_then_ok(tmp_path)
    source = shipper.inner.source
    marker = "STDERR-MARKER-tip-read-7f3a"
    gh.route(
        TIP,
        served("ref-main-base.json"),
        GhResult(1, f"stdout {marker}", f"gh: {marker} (HTTP 502)\n"),
    )
    gh.route(FF, served("ref-main-head.json"))

    dispatcher.poll_once(source)

    with queue.connect() as connection:
        error = connection.execute(
            "SELECT error FROM external_ship_intents"
        ).fetchone()[0]
    assert marker not in error
    assert error == (
        "ShipError: main is unreadable (GitHubAdapterError: "
        "gh api GET git/ref/heads/main exited 1 (HTTP 502)), "
        f"not {HEAD}, after fast-forward"
    )
    assert gh.count(FF) == 1
    grok = FakeBot(queue, "grok").drain()
    # Codex r1 F1.2: all() over an empty drain is vacuous, so pin the wake.
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert all(marker not in str(job.request.detail) for job in grok)


def test_handoff_of_a_merged_pr_raises_the_typed_already_merged_error() -> None:
    """#108 (#42 N3): the dispatcher ends the ship as merged on this error."""
    from lib.dispatcher import AlreadyMergedShip
    from lib.github_actions import AlreadyMergedError

    actions, gh = adapter()
    pull = served("pull-42-open.json")
    gh.route(PULL, GhResult(0, pull.stdout.replace('"merged": false', '"merged": true'), ""))

    with pytest.raises(AlreadyMergedError) as raised:
        actions.handoff(pr("handoff"), HEAD)

    assert isinstance(raised.value, AlreadyMergedShip)
    assert raised.value.write_confirmed is False
    assert gh.keys() == [PULL]


@pytest.mark.parametrize(
    ("checks", "status", "reported"),
    [
        ("check-runs-empty.json", "status-empty.json", False),
        ("check-runs-in-progress.json", "status-empty.json", True),
        ("check-runs-empty.json", "status-pending.json", True),
    ],
    ids=["nothing-reported", "running", "status-pending-only"],
)
def test_ci_status_says_whether_anything_reported(
    checks: str, status: str, reported: bool
) -> None:
    """#255: a head with no check run and no status (GitHub skipped CI on a
    conflicting PR) is told apart from a head whose CI is still running."""
    actions, gh = adapter()
    gh.route(CHECKS, served(checks))
    gh.route(STATUS, served(status))

    result = actions.ci_status("owner/repo", HEAD)
    assert result.status == "pending"
    assert result.reported is reported


FOLLOWUP_MARKER = "<!-- orch-followup verdict=99 -->"
FOLLOWUP_SINCE = datetime(2026, 11, 14, 12, tzinfo=timezone.utc).timestamp()
ISSUES = ("--paginate", f"{REPO}/issues?state=all&since=2026-11-14T11:00:00Z&per_page=100")
CREATE_ISSUE = (
    "-X",
    "POST",
    f"{REPO}/issues",
    "-f",
    "title=Follow-ups from #42 r4",
    "-f",
    f"body=findings\n\n{FOLLOWUP_MARKER}",
    "-f",
    "labels[]=factory:ready",
    "-f",
    "labels[]=urgent",
)


USER = ("user",)
OWN_LOGIN = "maxharris1"


def issues_page(*bodies: str | None, login: str = OWN_LOGIN, **extra: object) -> GhResult:
    items = [
        {"number": 100 + index, "body": body, "user": {"login": login}, **extra}
        for index, body in enumerate(bodies)
    ]
    return GhResult(0, json.dumps(items), "")


def adapter_as_own_login() -> tuple[GitHubReviewActions, RecordedGh]:
    actions, gh = adapter()
    gh.route(USER, GhResult(0, json.dumps({"login": OWN_LOGIN}), ""))
    return actions, gh


def file_followup(actions: GitHubReviewActions) -> int:
    return actions.file_issue(
        pr(),
        "Follow-ups from #42 r4",
        f"findings\n\n{FOLLOWUP_MARKER}",
        ("factory:ready", "urgent"),
        FOLLOWUP_MARKER,
        FOLLOWUP_SINCE,
    )


def test_file_issue_creates_the_issue_when_no_issue_carries_the_marker() -> None:
    actions, gh = adapter_as_own_login()
    gh.route(ISSUES, issues_page("unrelated", f"> {FOLLOWUP_MARKER}", None))
    gh.route(CREATE_ISSUE, GhResult(0, json.dumps({"number": 307}), ""))

    assert file_followup(actions) == 307
    assert gh.keys() == [ISSUES, CREATE_ISSUE]


def test_file_issue_returns_the_issue_an_earlier_try_created() -> None:
    actions, gh = adapter_as_own_login()
    gh.route(ISSUES, issues_page("unrelated", f"findings\n\n{FOLLOWUP_MARKER}"))

    assert file_followup(actions) == 101
    assert gh.keys() == [ISSUES, USER]


@pytest.mark.parametrize(
    "page",
    [
        issues_page(FOLLOWUP_MARKER, login="untrusted-contributor"),
        issues_page(
            FOLLOWUP_MARKER,
            pull_request={"url": f"https://api.github.com/{REPO}/pulls/42"},
        ),
    ],
    ids=["other-author", "pull-request"],
)
def test_file_issue_creates_the_issue_when_only_someone_else_carries_the_marker(
    page: GhResult,
) -> None:
    actions, gh = adapter_as_own_login()
    gh.route(ISSUES, page)
    gh.route(CREATE_ISSUE, GhResult(0, json.dumps({"number": 307}), ""))

    assert file_followup(actions) == 307
    assert gh.count(CREATE_ISSUE) == 1


def pr_comment(comment_id: int, body: str, login: str = OWN_LOGIN) -> dict[str, object]:
    return {
        "id": comment_id,
        "user": {"login": login},
        "body": body,
        "created_at": "2026-11-14T12:00:00Z",
    }


LINK_MARKER = "<!-- orch-followup-link verdict=99 -->"
LINK_BODY = f"Follow-ups are filed as #307.\n\n{LINK_MARKER}"
POST_LINK = ("-X", "POST", f"{REPO}/issues/42/comments", "-f", f"body={LINK_BODY}")


def test_comment_once_posts_only_when_no_comment_carries_the_marker() -> None:
    actions, gh = adapter_as_own_login()
    gh.route(COMMENTS, GhResult(0, json.dumps([pr_comment(1, f"quoted\n> {LINK_MARKER}")]), ""))
    gh.route(POST_LINK, GhResult(0, "{}", ""))
    actions.comment_once(pr(), LINK_BODY, LINK_MARKER)
    assert gh.keys() == [COMMENTS, POST_LINK]

    actions, gh = adapter_as_own_login()
    gh.route(COMMENTS, GhResult(0, json.dumps([pr_comment(1, LINK_BODY)]), ""))
    actions.comment_once(pr(), LINK_BODY, LINK_MARKER)
    assert gh.keys() == [COMMENTS, USER]


def test_comment_once_posts_when_only_someone_else_carries_the_marker() -> None:
    actions, gh = adapter_as_own_login()
    forged = pr_comment(1, LINK_BODY, login="untrusted-contributor")
    gh.route(COMMENTS, GhResult(0, json.dumps([forged]), ""))
    gh.route(POST_LINK, GhResult(0, "{}", ""))
    actions.comment_once(pr(), LINK_BODY, LINK_MARKER)
    assert gh.count(POST_LINK) == 1


ISSUE = (f"{REPO}/issues/307",)
ISSUE_COMMENTS = ("--paginate", f"{REPO}/issues/307/comments?per_page=100")
ADDITION_MARKER = "<!-- orch-followup verdict=99 -->"
ADDITION = f"Follow-up findings from round 5.\n\n{ADDITION_MARKER}"
POST_ADDITION = ("-X", "POST", f"{REPO}/issues/307/comments", "-f", f"body={ADDITION}")


def issue(state: str, *labels: str) -> GhResult:
    return GhResult(
        0, json.dumps({"number": 307, "state": state, "labels": [{"name": n} for n in labels]}), ""
    )


def test_open_issue_labels_reads_an_open_issue_and_hides_a_closed_one() -> None:
    actions, gh = adapter()
    gh.route(ISSUE, issue("open", "urgent", "factory:ready"))
    assert actions.open_issue_labels(pr(), 307) == ("factory:ready", "urgent")

    actions, gh = adapter()
    gh.route(ISSUE, issue("closed"))
    assert actions.open_issue_labels(pr(), 307) is None


def test_comment_on_issue_once_posts_on_the_issue_only_once() -> None:
    actions, gh = adapter_as_own_login()
    gh.route(ISSUE_COMMENTS, GhResult(0, "[]", ""))
    gh.route(POST_ADDITION, GhResult(0, "{}", ""))
    actions.comment_on_issue_once(pr(), 307, ADDITION, ADDITION_MARKER)
    assert gh.keys() == [ISSUE_COMMENTS, POST_ADDITION]

    actions, gh = adapter_as_own_login()
    gh.route(ISSUE_COMMENTS, GhResult(0, json.dumps([pr_comment(1, ADDITION)]), ""))
    actions.comment_on_issue_once(pr(), 307, ADDITION, ADDITION_MARKER)
    assert gh.keys() == [ISSUE_COMMENTS, USER]


QA_MARKER = "<!-- qa-run run-1 -->"
QA_BODY = f"<!-- orch:v1 kind=qa -->\n\n{QA_MARKER}"
POST_QA = ("-X", "POST", f"{REPO}/issues/42/comments", "-f", f"body={QA_BODY}")


def test_post_issue_comment_once_returns_the_new_or_existing_comment_id() -> None:
    actions, gh = adapter_as_own_login()
    forged = pr_comment(1, QA_BODY, login="untrusted-contributor")
    gh.route(COMMENTS, GhResult(0, json.dumps([forged]), ""))
    gh.route(POST_QA, GhResult(0, json.dumps({"id": 555}), ""))
    assert actions.post_issue_comment_once("owner/repo", 42, QA_BODY, QA_MARKER) == "555"
    assert gh.count(POST_QA) == 1

    actions, gh = adapter_as_own_login()
    gh.route(COMMENTS, GhResult(0, json.dumps([pr_comment(9, QA_BODY)]), ""))
    assert actions.post_issue_comment_once("owner/repo", 42, QA_BODY, QA_MARKER) == "9"
    assert gh.count(POST_QA) == 0


WATCHDOG_RUN = 133415718700
WATCHDOG_ANNOTATIONS = (
    "--paginate",
    f"{REPO}/check-runs/{WATCHDOG_RUN}/annotations?per_page=100",
)


def _watchdog_pickup_message() -> str:
    workflow = (Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml").read_text()
    line = next(line for line in workflow.splitlines() if "title=macos runner pickup::" in line)
    message = line.split("title=macos runner pickup::", 1)[1].rstrip().removesuffix('"')
    return message.replace("$((PICKUP_SECONDS / 60))", "10").replace(
        "$created_at", "2026-11-16T15:49:22Z"
    )


@pytest.mark.parametrize("watchdog_conclusion", ["failure", "cancelled"])
def test_the_macos_pickup_watchdog_and_the_job_it_cancelled_read_as_infra(
    watchdog_conclusion: str,
) -> None:
    page = json.loads((FIXTURES / "check-runs-cancelled-pickup.json").read_text())
    macos = page["check_runs"][0]
    macos["output"]["annotations_count"] = 0
    watchdog = json.loads(json.dumps(macos))
    watchdog.update(
        id=WATCHDOG_RUN,
        name="macos-pickup",
        conclusion=watchdog_conclusion,
        details_url=f"https://github.com/owner/repo/actions/runs/43900570415/job/{WATCHDOG_RUN}",
    )
    watchdog["output"]["annotations_count"] = 1
    page["check_runs"].append(watchdog)
    page["total_count"] = 2
    annotation = {
        "path": ".github",
        "annotation_level": "failure",
        "title": "macos runner pickup",
        "message": _watchdog_pickup_message(),
    }
    actions, gh = adapter()
    gh.route(CHECKS, GhResult(0, json.dumps(page), ""))
    gh.route(STATUS, served("status-empty.json"))
    gh.route(WATCHDOG_ANNOTATIONS, GhResult(0, json.dumps([annotation]), ""))

    result = actions.ci_status("owner/repo", HEAD)

    assert [(check.name, check.cause, check.signal) for check in result.failed] == [
        ("macos", "unknown", None),
        ("macos-pickup", "infra", "runner pickup timed out"),
    ]
    assert result.cause == "infra"
    assert ci_evidence(failed=result.failed, logs={}).cause == "infra"


def test_a_failed_test_that_names_the_spending_limit_stays_code() -> None:
    page = json.loads((FIXTURES / "check-runs-failure.json").read_text())
    page["check_runs"][0]["output"] = {
        "title": "pytest",
        "summary": "FAILED tests/test_billing.py::test_spending_limit - AssertionError: spending limit was not enforced",
        "annotations_count": 0,
    }
    actions, gh = adapter()
    gh.route(CHECKS, GhResult(0, json.dumps(page), ""))
    gh.route(STATUS, served("status-empty.json"))

    result = actions.ci_status("owner/repo", HEAD)

    assert [(check.cause, check.signal) for check in result.failed] == [("code", None)]


def base_runs(page: int = 1) -> tuple[str, ...]:
    return (
        f"{REPO}/actions/runs?branch=release%2F1&event=push&status=completed"
        f"&per_page=100&page={page}",
    )


BASE_RUNS = base_runs()


def base_jobs(run_id: int, page: int = 1) -> tuple[str, ...]:
    return (f"{REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}",)


def base_run(run_id: int, workflow: int = 1) -> dict[str, object]:
    return {"id": run_id, "workflow_id": workflow, "head_sha": f"sha{run_id}"}


def json_result(document: object) -> GhResult:
    return GhResult(0, json.dumps(document), "")


def test_base_checks_reads_each_workflows_latest_completed_run_by_job_name() -> None:
    actions, gh = adapter()
    gh.route(
        BASE_RUNS,
        json_result(
            {
                "workflow_runs": [
                    base_run(503),
                    base_run(502, 2),
                    base_run(501),
                ]
            }
        ),
    )
    gh.route(
        base_jobs(503),
        json_result(
            {
                "jobs": [
                    {"id": 9001, "name": "test (3/3)", "conclusion": "failure"},
                    {"id": 9002, "name": "lint", "conclusion": "success"},
                ]
            }
        ),
    )
    gh.route(
        base_jobs(502),
        json_result({"jobs": [{"id": 9003, "name": "statechart", "conclusion": "timed_out"}]}),
    )

    checks = actions.base_checks("owner/repo", "release/1")

    assert checks == {
        "test (3/3)": BaseCheck("test (3/3)", "failure", "503", "9001", "sha503"),
        "lint": BaseCheck("lint", "success", "503", "9002", "sha503"),
        "statechart": BaseCheck("statechart", "timed_out", "502", "9003", "sha502"),
    }
    assert gh.count(base_jobs(501)) == 0


def test_base_checks_never_reads_a_run_still_going_on_the_base_tip() -> None:
    actions, gh = adapter()
    gh.route(BASE_RUNS, json_result({"workflow_runs": [base_run(501)]}))
    gh.route(
        base_jobs(501),
        json_result({"jobs": [{"id": 9001, "name": "test (3/3)", "conclusion": "failure"}]}),
    )

    checks = actions.base_checks("owner/repo", "release/1")

    assert checks == {"test (3/3)": BaseCheck("test (3/3)", "failure", "501", "9001", "sha501")}
    assert gh.keys() == [BASE_RUNS, base_jobs(501)]


def test_base_checks_refuses_a_branch_that_is_not_a_plain_name() -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError):
        actions.base_checks("owner/repo", "main..evil")
    assert gh.calls == []


def test_base_checks_reads_a_check_on_the_second_page_of_jobs() -> None:
    actions, gh = adapter()
    gh.route(BASE_RUNS, json_result({"workflow_runs": [base_run(501)]}))
    others = [{"id": 1000 + n, "name": f"other-{n}", "conclusion": "success"} for n in range(100)]
    gh.route(base_jobs(501), json_result({"total_count": 101, "jobs": others}))
    gh.route(
        base_jobs(501, 2),
        json_result(
            {"total_count": 101, "jobs": [{"id": 1100, "name": "pytest", "conclusion": "failure"}]}
        ),
    )

    checks = actions.base_checks("owner/repo", "release/1")

    assert len(checks) == 101
    assert checks["pytest"] == BaseCheck("pytest", "failure", "501", "1100", "sha501")


def test_base_checks_reads_a_workflow_whose_latest_run_is_past_twenty_newer_runs() -> None:
    actions, gh = adapter()
    runs = [base_run(rid) for rid in range(522, 502, -1)]
    gh.route(BASE_RUNS, json_result({"workflow_runs": [*runs, base_run(502, 2)]}))
    gh.route(
        base_jobs(522),
        json_result({"jobs": [{"id": 5220, "name": "lint", "conclusion": "success"}]}),
    )
    gh.route(
        base_jobs(502),
        json_result({"jobs": [{"id": 5020, "name": "test", "conclusion": "failure"}]}),
    )

    checks = actions.base_checks("owner/repo", "release/1")

    test = checks["test"]
    assert (test.conclusion, test.run_id, test.job_id) == ("failure", "502", "5020")
    assert base_red([FailedCheck("test", job_id="pr1")], {}, checks, {})


def test_base_checks_reads_a_workflows_latest_run_on_the_second_page_of_runs() -> None:
    actions, gh = adapter()
    runs = [base_run(rid) for rid in range(700, 600, -1)]
    gh.route(base_runs(1), json_result({"workflow_runs": runs}))
    gh.route(base_runs(2), json_result({"workflow_runs": [base_run(600, 2)]}))
    gh.route(
        base_jobs(700),
        json_result({"jobs": [{"id": 7000, "name": "lint", "conclusion": "success"}]}),
    )
    gh.route(
        base_jobs(600),
        json_result({"jobs": [{"id": 6000, "name": "test", "conclusion": "failure"}]}),
    )

    checks = actions.base_checks("owner/repo", "release/1")

    assert checks == {
        "lint": BaseCheck("lint", "success", "700", "7000", "sha700"),
        "test": BaseCheck("test", "failure", "600", "6000", "sha600"),
    }
    assert gh.count(base_runs(3)) == 0


def test_base_checks_stops_paging_runs_after_three_full_pages() -> None:
    actions, gh = adapter()
    for page in (1, 2, 3, 4):
        first = 1000 - 100 * page
        runs = [base_run(first + n) for n in range(100, 0, -1)]
        gh.route(base_runs(page), json_result({"workflow_runs": runs}))
    gh.route(
        base_jobs(1000),
        json_result({"jobs": [{"id": 9000, "name": "lint", "conclusion": "success"}]}),
    )

    checks = actions.base_checks("owner/repo", "release/1")

    assert checks == {"lint": BaseCheck("lint", "success", "1000", "9000", "sha1000")}
    assert [gh.count(base_runs(page)) for page in (1, 2, 3, 4)] == [1, 1, 1, 0]


def test_base_checks_has_no_evidence_for_a_workflow_with_no_completed_run() -> None:
    actions, gh = adapter()
    runs = [base_run(rid) for rid in range(700, 600, -1)]
    gh.route(base_runs(1), json_result({"workflow_runs": runs}))
    gh.route(base_runs(2), json_result({"workflow_runs": []}))
    gh.route(
        base_jobs(700),
        json_result({"jobs": [{"id": 7000, "name": "lint", "conclusion": "success"}]}),
    )

    checks = actions.base_checks("owner/repo", "release/1")

    assert checks == {"lint": BaseCheck("lint", "success", "700", "7000", "sha700")}
    assert not base_red([FailedCheck("test", job_id="pr1")], {}, checks, {})
