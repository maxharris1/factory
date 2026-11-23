import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs/SPEC.md"
DECISIONS = ROOT / "docs/decisions/1447-stacked-tickets.md"
LINES = [f"line {n}" for n in range(1, 61)]


def _paragraph(lead: str) -> str:
    spec = SPEC.read_text(encoding="utf-8")
    match = re.search(rf"^\*\*{re.escape(lead)}\*\*.*?(?=^\*\*|^#|\Z)", spec, re.MULTILINE | re.DOTALL)
    assert match is not None, lead
    return match.group(0)


def _command(lead: str, prefix: str) -> str:
    spans = [span for span in re.findall(r"`([^`\n]+)`", _paragraph(lead)) if span.startswith(prefix)]
    assert len(spans) == 1, (lead, prefix, spans)
    return spans[0]


def _edit(lines: list[str], number: int, owner: str) -> list[str]:
    return [f"{owner} {line}" if n == number else line for n, line in enumerate(lines, 1)]


class Repo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        self.git("-c", "init.defaultBranch=main", "init", "-q")
        for key, value in (
            ("user.name", "Spec Test"),
            ("user.email", "spec-test@example.invalid"),
            ("commit.gpgsign", "false"),
        ):
            self.git("config", key, value)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.path, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, subject: str, name: str, lines: list[str]) -> str:
        (self.path / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", subject)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def is_ancestor(self, older: str, newer: str) -> bool:
        return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=self.path).returncode == 0

    def squash_into_main(self, branch: str) -> str:
        self.git("switch", "-q", "main")
        self.git("merge", "-q", "--squash", branch)
        self.git("commit", "-q", "-m", f"squash {branch}")
        return self.head()

    def run_documented(self, template: str, shas: dict[str, str]) -> str:
        args = shlex.split(re.sub(r"<([a-z-]+)>", lambda m: shas[m.group(1)], template))
        assert args[0] == "git", template
        return self.git(*args[1:])


def _old_base(repo: Repo, shas: dict[str, str]) -> str | None:
    own = repo.run_documented(_command("Retarget.", "git rev-list <old-head> --not "), shas).split()
    for parent_head in shas["parent-heads"].split():
        candidate = repo.run_documented(_command("Retarget.", "git merge-base <"), {**shas, "parent-head": parent_head})
        listed = repo.run_documented(_command("Retarget.", "git rev-list <old-head> ^"), {**shas, "old-base": candidate})
        if listed.split() == own:
            return candidate
    return None


def _retarget(repo: Repo, shas: dict[str, str], parent_heads: list[str]) -> dict[str, str]:
    shas = {**shas, "parent-heads": " ".join(parent_heads)}
    old_base = _old_base(repo, shas)
    if old_base is None:
        pytest.fail("the documented rule fails closed on a history it should retarget")
    shas = {**shas, "old-base": old_base}
    repo.run_documented(_command("Retarget.", "git rebase --onto "), shas)
    return {**shas, "new-head": repo.head()}


def _assert_range_diff_names_only_the_child(repo: Repo, shas: dict[str, str]) -> None:
    out = repo.run_documented(_command("Review scope and token policy.", "git range-diff "), shas)
    pairs = re.findall(r"^\s*(?:\d+|-):\s+\S+ [<>=!] +(?:\d+|-):.*$", out, re.MULTILINE)
    assert len(pairs) == 1, out
    assert "child-only" in pairs[0], out
    assert "parent-v" not in out


def _parent_and_child(repo: Repo) -> tuple[str, str]:
    repo.commit("base", "main.txt", ["main"])
    repo.git("switch", "-q", "-c", "parent")
    parent = repo.commit("parent-v1", "f.txt", LINES)
    repo.git("switch", "-q", "-c", "child")
    child = repo.commit("child-only", "f.txt", _edit(LINES, 1, "child"))
    return parent, child


def test_scoped_review_skips_a_later_parent_commit(tmp_path):
    repo = Repo(tmp_path / "repo")
    p1, child = _parent_and_child(repo)
    repo.git("switch", "-q", "parent")
    p2 = repo.commit("parent-v2", "f.txt", _edit(LINES, 50, "parent"))
    new_base = repo.squash_into_main("parent")

    shas = _retarget(
        repo,
        {"old-head": child, "new-base": new_base},
        [p1, p2],
    )

    _assert_range_diff_names_only_the_child(repo, shas)


def test_scoped_review_skips_a_rewritten_parent(tmp_path):
    repo = Repo(tmp_path / "repo")
    p1, child = _parent_and_child(repo)
    repo.git("switch", "-q", "main")
    repo.commit("main-v2", "main.txt", ["main", "main 2"])
    repo.git("switch", "-q", "parent")
    repo.git("rebase", "-q", "main")
    rewritten = repo.head()
    new_base = repo.squash_into_main("parent")

    shas = _retarget(
        repo,
        {"old-head": child, "new-base": new_base},
        [p1, rewritten, p1],
    )

    assert shas["old-base"] == p1
    _assert_range_diff_names_only_the_child(repo, shas)


@pytest.mark.parametrize(
    ("landing", "head_moves"), [("merge-commit", True), ("squash", True), ("fast-forward", False)]
)
def test_rebase_moves_the_head_unless_the_parent_fast_forwarded(tmp_path, landing, head_moves):
    repo = Repo(tmp_path / "repo")
    parent, child = _parent_and_child(repo)
    if landing == "squash":
        new_base = repo.squash_into_main("parent")
    else:
        repo.git("switch", "-q", "main")
        repo.git("merge", "-q", "--no-edit", {"merge-commit": "--no-ff", "fast-forward": "--ff-only"}[landing], "parent")
        new_base = repo.head()

    repo.run_documented(
        _command("Retarget.", "git rebase --onto "),
        {"old-base": parent, "old-head": child, "new-base": new_base},
    )

    assert (repo.head() != child) == head_moves


def test_ci_fallback_is_keyed_on_an_unchanged_head():
    ci = _paragraph("CI after the retarget.")
    assert re.search(r"head (?:after|before) the rebase", ci), ci
    assert "merge commit" not in ci.lower()

    entries = [
        entry
        for entry in DECISIONS.read_text(encoding="utf-8").split("\n\n")
        if entry.startswith("- **Decision:**") and "pushes no new head" in entry
    ]
    assert len(entries) == 1, entries
    assert "merge commit" not in entries[0].lower()


def test_scoped_review_skips_a_parent_update_the_child_merged_in(tmp_path):
    repo = Repo(tmp_path / "repo")
    p1, _ = _parent_and_child(repo)
    repo.git("switch", "-q", "parent")
    p2 = repo.commit("parent-v2", "f.txt", _edit(LINES, 50, "parent"))
    repo.git("switch", "-q", "child")
    repo.git("merge", "-q", "--no-ff", "--no-edit", "parent")
    old_head = repo.head()
    repo.git("switch", "-q", "parent")
    repo.git("reset", "-q", "--soft", "main")
    repo.git("commit", "-q", "-m", "parent-rewritten")
    rewritten = repo.head()
    new_base = repo.squash_into_main("parent")

    shas = _retarget(
        repo,
        {"old-head": old_head, "new-base": new_base},
        [p1, rewritten, p2],
    )

    assert shas["old-base"] == p2
    _assert_range_diff_names_only_the_child(repo, shas)


def test_retarget_fails_closed_when_the_child_merged_two_parent_rewrites(tmp_path):
    repo = Repo(tmp_path / "repo")
    p1, _ = _parent_and_child(repo)
    repo.git("switch", "-q", "parent")
    p2 = repo.commit("parent-v2", "f.txt", _edit(LINES, 50, "parent"))
    repo.git("switch", "-q", "child")
    repo.git("merge", "-q", "--no-ff", "--no-edit", "parent")
    repo.git("switch", "-q", "parent")
    repo.git("reset", "-q", "--hard", p1)
    rewritten = repo.commit("parent-v2-rewritten", "f.txt", _edit(LINES, 40, "parent"))
    repo.git("switch", "-q", "child")
    repo.git("merge", "-q", "--no-ff", "--no-edit", "parent")

    shas = {"old-head": repo.head(), "parent-heads": " ".join([p1, rewritten, p2])}

    assert _old_base(repo, shas) is None
