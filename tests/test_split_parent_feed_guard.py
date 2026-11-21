from __future__ import annotations

from pathlib import Path

from lib.dispatcher import Comment
from lib.feeder import IssueRecord
from lib.followups import split_comment, split_record
from lib.split_parent import split_evidence
from tests.test_feeder import FakeGitHub, issue, make_feeder, rows

REPO = "owner/app"

OWN_BODY = (
    "Refuse to feed or rebuild a ticket that has already been split into parts.\n"
    "Detect a split parent from an `<!-- orch-split-link -->` comment or from body/comments\n"
    "that record `Split into #N…`. Ordinary unsplit factory:ready tickets must still feed.\n"
)


def part(parent: int) -> dict:
    return {
        "title": f"Part of #{parent}",
        "body": f"Split from #{parent} (plan https://github.com/{REPO}/pull/1#issuecomment-7).\n",
        "state": "open",
        "labels": ("factory:ready",),
    }


def board(tmp_path: Path, number: int, *, children=(), body: str = "The big ticket in lib/x.py."):
    clock = [10_000.0]
    github = FakeGitHub()
    for child in children:
        github.filed[(REPO, child)] = part(number)
    github.issues[REPO] = [issue(number, body=body)]
    feeder, queue, github, _ = make_feeder(tmp_path, github=github, now=clock)
    logs: list[str] = []
    feeder.log = logs.append
    return feeder, github, logs, clock


def comment(author: str, body: str, at: float = 1.0) -> Comment:
    return Comment(id=f"c{at}", author=author, body=body, created_at=at)


def assert_refused(report, github: FakeGitHub, number: int, before: dict, db: Path) -> dict:
    assert report.fed == ()
    assert not [call for call in github.calls if call.startswith(("pull:", "commit:", "branch:"))]
    assert github.posted == before
    row = rows(db)[str(number)]
    assert row["state"] == "split-parent"
    assert row["branch"] is None and row["pr_number"] is None
    return row


def test_factory_1078_auto_split_with_a_leftover_branch_and_closed_pr_is_refused(tmp_path: Path) -> None:
    feeder, github, logs, _ = board(tmp_path, 1078, children=(1183, 1184))
    github.branches[(REPO, "factory/issue-1078")] = "b" * 40
    github.pulls[(REPO, 1181)] = {"head": "factory/issue-1078", "base": "main", "state": "closed"}
    github.posted[(REPO, 1181)] = [comment("grok-bot", split_comment("7042654207", (1183, 1184)))]
    before = {key: list(value) for key, value in github.posted.items()}

    row = assert_refused(feeder.feed_once(), github, 1078, before, tmp_path / "orch.db")

    assert "#1183" in row["waiting_reason"] and "#1184" in row["waiting_reason"]
    assert any("owner/app#1078 refused: already split into parts" in line for line in logs)


def test_coo_89_auto_split_with_only_its_parts_is_refused(tmp_path: Path) -> None:
    feeder, github, _, _ = board(tmp_path, 89, children=(149, 150, 151, 152))

    row = assert_refused(feeder.feed_once(), github, 89, {}, tmp_path / "orch.db")

    assert "#149" in row["waiting_reason"]


def test_coo_21_operator_split_comment_is_refused_before_search_finds_its_parts(tmp_path: Path) -> None:
    feeder, github, _, _ = board(tmp_path, 21)
    github.posted[(REPO, 21)] = [
        comment("maxharris1", "Split into #141 (PR #143) and #142 (PR #144), both merged.")
    ]
    before = {key: list(value) for key, value in github.posted.items()}

    row = assert_refused(feeder.feed_once(), github, 21, before, tmp_path / "orch.db")

    assert "Split into #141" in row["waiting_reason"]


def test_a_split_link_on_the_open_pr_the_feed_would_reuse_is_refused(tmp_path: Path) -> None:
    feeder, github, _, _ = board(tmp_path, 30)
    github.branches[(REPO, "factory/issue-30")] = "c" * 40
    github.pulls[(REPO, 31)] = {"head": "factory/issue-30", "base": "main"}
    github.posted[(REPO, 31)] = [comment("maxharris1", split_comment("55", (32, 33)))]
    before = {key: list(value) for key, value in github.posted.items()}

    row = assert_refused(feeder.feed_once(), github, 30, before, tmp_path / "orch.db")

    assert "orch-split-link" in row["waiting_reason"]


def test_refusal_is_logged_leaves_no_branch_and_sticks_without_new_reads(tmp_path: Path) -> None:
    feeder, github, logs, clock = board(tmp_path, 89, children=(149, 150))
    reads: list[int] = []
    seen = github.comments
    github.comments = lambda repo, number: reads.append(number) or seen(repo, number)

    assert_refused(feeder.feed_once(), github, 89, {}, tmp_path / "orch.db")
    assert (REPO, "factory/issue-89") not in github.branches
    assert logs.count("feeder: owner/app#89 refused: already split into parts (split into #149, #150)") == 1
    searched = github.calls.count(f"split-children:{REPO}#89")
    read = len(reads)

    clock[0] += 60
    assert feeder.feed_once().fed == ()
    assert github.calls.count(f"split-children:{REPO}#89") == searched
    assert len(reads) == read
    assert rows(tmp_path / "orch.db")["89"]["state"] == "split-parent"

    github.issues[REPO] = []
    clock[0] += 60
    feeder.feed_once()
    assert "89" not in rows(tmp_path / "orch.db")
    github.issues[REPO] = [issue(89)]
    clock[0] += 60
    feeder.feed_once()
    assert github.calls.count(f"split-children:{REPO}#89") == searched + 1


def test_a_failed_split_read_never_feeds(tmp_path: Path) -> None:
    feeder, github, _, _ = board(tmp_path, 40)
    github.fail["split_children"] = 1

    report = feeder.feed_once()

    assert report.fed == () and report.errors == 1
    assert rows(tmp_path / "orch.db")["40"]["state"] == "error"
    assert (REPO, "factory/issue-40") not in github.branches


def test_an_unsplit_ticket_that_only_mentions_the_markers_still_feeds(tmp_path: Path) -> None:
    feeder, github, _, _ = board(tmp_path, 1255, body=OWN_BODY)
    github.posted[(REPO, 1255)] = [comment("drive-by", "Split into #7 and #8.")]

    report = feeder.feed_once()

    assert len(report.fed) == 1
    assert (REPO, "factory/issue-1255") in github.branches
    assert rows(tmp_path / "orch.db")["1255"]["state"] in ("fed", "done")


def test_split_record_reads_markers_and_split_into_lines_only() -> None:
    assert split_record(split_comment("7042654207", (1183, 1184))) == (
        "<!-- orch-split-link plan=7042654207 -->"
    )
    assert split_record("Split into #141 (PR #143) and #142.") == "Split into #141 (PR #143) and #142."
    assert split_record("**Split into #5** and #6") == "**Split into #5** and #6"
    assert split_record(OWN_BODY) is None
    assert split_record("```\nSplit into #141\n<!-- orch-split-link plan=1 -->\n```\n") is None
    assert split_record("Split from #12 (plan x).") is None
    assert split_record("Split into #N parts") is None
    assert split_record("<!-- orch-split-link -->") is None


def test_split_evidence_trusts_children_body_and_trusted_comments_only() -> None:
    child = IssueRecord(number=9, state="open", reason=None, body="Split from #3")
    assert split_evidence("x", (), frozenset(), (child,)) == "split into #9"
    assert split_evidence("Split into #9.", (), frozenset(), ()) == "Split into #9."
    said = (comment("Stranger", "Split into #9."),)
    assert split_evidence("x", said, frozenset({"maxharris1"}), ()) is None
    assert split_evidence("x", said, frozenset({"stranger"}), ()) == "Split into #9."
