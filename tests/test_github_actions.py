import ast
import os
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import (
    CIResult,
    Dispatcher,
    ReviewShipper,
    ShipError,
    ShipRequest,
    TicketRef,
    TrackedTicket,
    _comment_order,
)
from lib.github_actions import (
    GH,
    GhResult,
    GitHubAdapterError,
    GitHubReviewActions,
    HeadMismatchError,
    NotFastForwardError,
    review_actions_for,
)
from lib.hook_t7 import T7Hook
from lib.hook_t8 import T8Hook
from lib.queue import SQLiteJobQueue
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
        assert argv[: len(GH) + 1] == [*GH, "api"], argv
        key = tuple(argv[len(GH) + 1 :])
        if key not in self.routes:
            raise AssertionError(f"unrecorded gh call {key}")
        queue = self.routes[key]
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, BaseException):
            raise response
        return response

    def count(self, key: tuple[str, ...]) -> int:
        return sum(1 for call in self.calls if tuple(call[len(GH) + 1 :]) == key)

    def keys(self) -> list[tuple[str, ...]]:
        return [tuple(call[len(GH) + 1 :]) for call in self.calls]


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

    assert len(gh.calls) == 11
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
    assert callers == ["run_gh"]
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "os" not in imported


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
    assert "Update is not a fast forward" in str(raised.value)
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
    gh.route(MERGE, served("merge-squash-200.json"))

    actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)

    assert gh.keys() == [MERGE]


def test_squash_merge_raises_when_the_head_does_not_match() -> None:
    actions, gh = adapter()
    gh.route(MERGE, http_error("merge-head-modified-409.json", 409))

    with pytest.raises(HeadMismatchError, match=f"PR head is not {HEAD}"):
        actions.merge(pr("merge"), squash=True, match_head_commit=HEAD)

    assert gh.keys() == [MERGE]


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
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError, match="is a linear ticket"):
        actions.merge(pr("merge", tickets="linear"), squash=True, match_head_commit=HEAD)

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


def test_stable_patch_id_and_handoff_fail_closed() -> None:
    actions, gh = adapter()

    with pytest.raises(GitHubAdapterError, match="stable_patch_id is not built"):
        actions.stable_patch_id(pr(), HEAD)
    with pytest.raises(GitHubAdapterError, match="handoff is not built"):
        actions.handoff(pr("handoff"), HEAD)

    assert gh.calls == []


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

    def github_candidates(self, label_prefix: str) -> tuple[TrackedTicket, ...]:
        del label_prefix
        return (self._ticket(),)

    def linear_candidates(self, query: str) -> tuple[TrackedTicket, ...]:
        del query
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

    assert result == CIResult("failure", "failed: typecheck")
