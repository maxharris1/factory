"""Issue #261: factory.md shared_paths.

A Touches: path in shared_paths never causes a feeder collision wait, and in
a shared file a conflict hunk kept as both sides is still a trivial update.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lib.factory_md import parse_factory_md
from lib.feeder_gates import shared_path_matches
from lib.update_check import classify
from tests.merge_queue_support import Board, sha
from tests.test_feeder import FACTORY_MD, make_feeder
from tests.test_feeder_gates import HOME, OPEN_PULL, fed_heads, issue, ref, states
from tests.test_update_check import Repo, run

SHARED_MD = FACTORY_MD.replace(
    "skills: []\n", "skills: []\nshared_paths: [docs/SPEC.md, docs/decisions/]\n"
)


# --- factory.md ---------------------------------------------------------------


def test_shared_paths_parse_and_default_to_empty() -> None:
    config, findings = parse_factory_md(SHARED_MD)
    assert config is not None, findings
    assert config.shared_paths == ("docs/SPEC.md", "docs/decisions/")
    plain, _ = parse_factory_md(FACTORY_MD)
    assert plain is not None and plain.shared_paths == ()
    empty, _ = parse_factory_md(FACTORY_MD.replace("skills: []\n", "skills: []\nshared_paths:\n"))
    assert empty is not None and empty.shared_paths == ()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("docs/SPEC.md", "shared_paths must be a list"),
        ("[../x]", "shared_paths[0] must be a repo-relative path or glob"),
        ("[docs/ok.md, /abs]", "shared_paths[1] must be a repo-relative path or glob"),
        ("[docs//x]", "shared_paths[0] must be a repo-relative path or glob"),
        ("[3]", "shared_paths[0] must be a repo-relative path or glob"),
    ],
)
def test_bad_shared_paths_are_refused(value: str, message: str) -> None:
    config, findings = parse_factory_md(
        FACTORY_MD.replace("skills: []\n", f"skills: []\nshared_paths: {value}\n")
    )
    assert config is None
    assert message in [item.message for item in findings]


# --- matching -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("patterns", "path", "matches"),
    [
        (("docs/SPEC.md",), "docs/SPEC.md", True),
        (("docs/SPEC.md",), "docs/SPEC.md.bak", False),
        (("docs/SPEC.md",), "docs/", False),
        (("docs/decisions/",), "docs/decisions", True),
        (("docs/decisions/",), "docs/decisions/", True),
        (("docs/decisions/",), "docs/decisions/0042-x.md", True),
        (("docs/decisions/",), "docs/decisions/sub/y.md", True),
        (("docs/decisions/",), "docs/decisions-old/y.md", False),
        (("docs/decisions/",), "docs/", False),
        (("docs/*.md",), "docs/a.md", True),
        (("docs/*.md",), "docs/sub/a.md", False),
        (("docs/**/*.md",), "docs/sub/a.md", True),
        ((), "docs/SPEC.md", False),
    ],
)
def test_shared_path_matches(patterns, path: str, matches: bool) -> None:
    assert shared_path_matches(patterns, path) is matches


# --- the feeder ---------------------------------------------------------------


def test_two_tickets_sharing_only_shared_paths_are_both_fed_in_one_poll(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3, factory_text=SHARED_MD)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: docs/SPEC.md, docs/decisions/, lib/a.py"),
        issue(2, created_at=2.0, body="Touches: docs/SPEC.md, docs/decisions, lib/b.py"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1", "factory/issue-2"]


def test_an_in_flight_holder_of_a_shared_path_does_not_hold(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, _, github, _ = make_feeder(tmp_path, box=3, factory_text=SHARED_MD, now=now)
    github.issues[HOME] = [issue(1, body="Touches: docs/SPEC.md")]
    feeder.feed_once()
    github.statuses = {ref(501): OPEN_PULL}
    github.issues[HOME] = [issue(2, body="Touches: docs/SPEC.md")]
    now[0] += 60
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-2"]


def test_a_shared_code_path_still_waits(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3, factory_text=SHARED_MD)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: docs/SPEC.md, lib/x.py"),
        issue(2, created_at=2.0, body="Touches: docs/SPEC.md, lib/x.py"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/x.py held by #1")


def test_an_explicit_conflicts_with_edge_still_holds(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3, factory_text=SHARED_MD)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: docs/SPEC.md"),
        issue(2, created_at=2.0, body="Touches: docs/SPEC.md\nConflicts with #1"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on #1 (conflicts with it)")


def test_without_shared_paths_a_docs_path_still_waits(tmp_path: Path) -> None:
    feeder, _, github, _ = make_feeder(tmp_path, box=3)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: docs/SPEC.md"),
        issue(2, created_at=2.0, body="Touches: docs/SPEC.md"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file docs/SPEC.md held by #1")


# --- the update check ---------------------------------------------------------

SPEC = "".join(f"para {n}\n" for n in range(1, 21))
PR_PARA = "para 10\nPR paragraph\n"
MAIN_PARA = "para 10\nmain paragraph\n"


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    repo = Repo(tmp_path / "r")
    (repo.root / "docs").mkdir()
    repo.write("docs/SPEC.md", SPEC)
    repo.write("lib.py", SPEC)
    repo.base = repo.commit("docs")
    return repo


def conflicted(repo: Repo, path: str) -> str:
    approved, _ = repo.branches([(path, "para 10\n", PR_PARA)], [(path, "para 10\n", MAIN_PARA)])
    return approved


def keep(repo: Repo, path: str, middle: str, extra=None):
    def resolve(r: Repo) -> None:
        text = SPEC.replace("para 10\n", "para 10\n" + middle)
        if extra is not None:
            text = text.replace(*extra)
        r.write(path, text)

    return resolve


SHARED = ("docs/SPEC.md",)


@pytest.mark.parametrize(
    "middle", ["PR paragraph\nmain paragraph\n", "main paragraph\nPR paragraph\n"]
)
def test_both_sides_in_a_shared_file_keep_the_approval(repo: Repo, middle: str) -> None:
    approved = conflicted(repo, "docs/SPEC.md")
    head = repo.merge_main(keep(repo, "docs/SPEC.md", middle))
    result = run_shared(repo, approved, head, SHARED)
    assert result.trivial, result.reason
    assert result.reason == (
        "1 conflicted file(s), each hunk resolved to one side or, in a shared path, both"
    )


def test_one_side_in_a_shared_file_keeps_todays_reason(repo: Repo) -> None:
    approved = conflicted(repo, "docs/SPEC.md")
    head = repo.merge_main(lambda r: r.git("checkout", "--ours", "docs/SPEC.md"))
    result = run_shared(repo, approved, head, SHARED)
    assert result.trivial, result.reason
    assert result.reason == "1 conflicted file(s), each hunk resolved to one side"


def test_both_sides_in_a_code_file_do_not(repo: Repo) -> None:
    approved = conflicted(repo, "lib.py")
    head = repo.merge_main(keep(repo, "lib.py", "PR paragraph\nmain paragraph\n"))
    result = run_shared(repo, approved, head, SHARED)
    assert not result.trivial
    assert result.reason == "lib.py: a conflict hunk was not resolved to exactly one side"


@pytest.mark.parametrize(
    ("middle", "extra"),
    [
        ("PR paragraph\nmain paragraph\nextra line\n", None),
        ("PR paragraph\nedited\nmain paragraph\n", None),
        ("PR paragraph\nmain paragraph\n", ("para 18\n", "para 18 edited\n")),
        ("PR paragraph main paragraph\n", None),
        ("PR paragraph\n", ("para 18\n", "para 18 edited\n")),
    ],
)
def test_both_sides_with_any_other_edit_do_not(repo: Repo, middle: str, extra) -> None:
    approved = conflicted(repo, "docs/SPEC.md")
    head = repo.merge_main(keep(repo, "docs/SPEC.md", middle, extra))
    result = run_shared(repo, approved, head, SHARED)
    assert not result.trivial
    assert result.reason == "docs/SPEC.md: a conflict hunk was not resolved to exactly one side"


def test_without_shared_paths_both_sides_in_docs_do_not(repo: Repo) -> None:
    approved = conflicted(repo, "docs/SPEC.md")
    head = repo.merge_main(keep(repo, "docs/SPEC.md", "PR paragraph\nmain paragraph\n"))
    result = run(repo, approved, head)
    assert not result.trivial
    assert result.reason == "docs/SPEC.md: a conflict hunk was not resolved to exactly one side"


def test_a_directory_pattern_covers_new_lines_in_a_file_under_it(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "r")
    (repo.root / "docs" / "decisions").mkdir(parents=True)
    repo.write("docs/decisions/index.md", SPEC)
    repo.base = repo.commit("decisions")
    approved = conflicted(repo, "docs/decisions/index.md")
    head = repo.merge_main(keep(repo, "docs/decisions/index.md", "PR paragraph\nmain paragraph\n"))
    assert run_shared(repo, approved, head, ("docs/decisions/",)).trivial


EOF_CASES = [
    ("ours", "theirs", "ourstheirs", True),
    ("ours", "theirs", "theirsours", True),
    ("ours", "theirs", "ours", True),
    ("ours", "theirs", "theirs", True),
    ("ours", "theirs", "ours\ntheirs\n", False),
    ("ours", "theirs", "ours\ntheirs", False),
    ("ours", "theirs", "theirs\nours\n", False),
    ("ours", "theirs", "ours\n", False),
    ("ours", "theirs", "theirs\n", False),
    ("ours\n", "theirs", "ours\ntheirs", True),
    ("ours\n", "theirs", "theirsours\n", True),
    ("ours\n", "theirs", "ours\n", True),
    ("ours\n", "theirs", "theirs", True),
    ("ours\n", "theirs", "ours\ntheirs\n", False),
    ("ours\n", "theirs", "theirs\nours\n", False),
    ("ours\n", "theirs", "theirs\n", False),
    ("ours", "theirs\n", "ourstheirs\n", True),
    ("ours", "theirs\n", "theirs\nours", True),
    ("ours", "theirs\n", "ours\ntheirs\n", False),
    ("ours", "theirs\n", "ours\n", False),
]


@pytest.mark.parametrize("before", ["", "line\n"])
@pytest.mark.parametrize(("ours", "theirs", "resolved", "trivial"), EOF_CASES)
def test_a_side_without_a_final_newline_is_matched_byte_for_byte(
    tmp_path: Path, before: str, ours: str, theirs: str, resolved: str, trivial: bool
) -> None:
    repo = Repo(tmp_path / "r")
    (repo.root / "docs").mkdir()
    repo.write("docs/SPEC.md", before + "base")
    repo.base = repo.commit("docs without a final newline")
    approved, _ = repo.branches(
        [("docs/SPEC.md", "base", ours)], [("docs/SPEC.md", "base", theirs)]
    )
    head = repo.merge_main(lambda r: r.write("docs/SPEC.md", before + resolved))
    result = run_shared(repo, approved, head, SHARED)
    assert result.trivial is trivial, result.reason


@pytest.mark.parametrize(("resolved", "trivial"), [("ours", True), ("ours\n", False)])
def test_a_one_side_resolution_without_a_final_newline_outside_shared_paths(
    tmp_path: Path, resolved: str, trivial: bool
) -> None:
    repo = Repo(tmp_path / "r")
    repo.write("app.py", "base")
    repo.base = repo.commit("no final newline")
    approved, _ = repo.branches([("app.py", "base", "ours")], [("app.py", "base", "theirs")])
    head = repo.merge_main(lambda r: r.write("app.py", resolved))
    assert run(repo, approved, head).trivial is trivial


def run_shared(repo: Repo, approved: str, head: str, shared: tuple[str, ...]):
    return classify(repo.gitdir, approved, head, "refs/heads/main", shared)


# --- the dispatcher passes the project's patterns -----------------------------


def test_the_dispatcher_passes_the_projects_shared_paths(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.factory = replace(board.factory, shared_paths=SHARED)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    assert board.classified == [("owner/repo", "main", sha(1), sha(2))]
    assert board.classified_shared == [SHARED]
