"""Evidence bundle: earlier findings and the reviewed head (#164)."""

from __future__ import annotations

import pytest

from lib.dispatcher import Comment
from lib.evidence.findings import Finding, findings_evidence

TRUSTED = {
    "github": {
        "codex-app[bot]": frozenset({"codex-bot"}),
        "claude-app[bot]": frozenset({"claude-bot"}),
    }
}
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
HEAD_3 = "3" * 40
BASE_1 = "a" * 40
BASE_2 = "b" * 40

ROUND_1 = """**Review round 1 of 3: REVISE**

1. [blocking] f-1, `lib/remote.py:620`. The report lands in the device log. Rule: secrets never reach a world-readable file.
2. [blocking] **f-2: the handoff file is empty.** `lib/remote.py:624`. Rule: the device saves the ORCH-HANDOFF line.
3. [minor] f-3, `lib/remote.py:700`. A stray print. Rule: no debug output.
4. [blocking] ac-2, `lib/remote.py:628`. The second criterion fails.
"""

ROUND_2 = """**Review round 2 of 3: REVISE**

- [closed] f-2, `lib/remote.py:624`. The handoff probe now passes.
- [blocking] f-4, `lib/queue.py:176`. A retry skips the hold.
  Rule: a restored job waits for reconciliation.

  ```sh
  - [blocking] f-9, not a finding: it sits in a repro.
  ```
"""

ROUND_3_APPROVE = """**Review round 3 of 3: APPROVE**

No verified findings remain.
"""


def _verdict(
    body: str,
    *,
    round_: str,
    head: str,
    verdict: str = "REVISE",
    author: str = "codex-app[bot]",
    sender: str = "codex-bot",
    role: str = "reviewer",
    at: float,
    cid: str,
) -> Comment:
    ac = "met" if verdict == "APPROVE" else "unmet"
    header = (
        f"<!-- orch:v1 kind=verdict from={sender} role={role} round={round_} "
        f"head={head} ac={ac} verdict={verdict} risk=high -->"
    )
    return Comment(
        id=cid,
        author=author,
        body=f"{header}\n{body}",
        created_at=at,
        url=f"https://github.com/o/r/pull/7#issuecomment-{cid}",
    )


R1 = _verdict(ROUND_1, round_="1/3", head=HEAD_1, at=1.0, cid="11")
R2 = _verdict(ROUND_2, round_="2/3", head=HEAD_2, at=2.0, cid="22")


def _bundle(comments, *, round_=3, bases=None):
    return findings_evidence(
        comments=comments,
        trusted_logins=TRUSTED,
        reviewer="codex-bot",
        current_round=round_,
        bases={1: BASE_1, 2: BASE_2} if bases is None else bases,
    )


def test_round_one_has_no_earlier_findings_or_head():
    bundle = _bundle((), round_=1)
    assert bundle.findings == ()
    assert bundle.reviewed_head is None
    assert bundle.reviewed_base is None
    assert bundle.verdict_url is None


def test_round_three_carries_both_earlier_rounds_open_findings():
    bundle = _bundle((R2, R1))
    assert [f.id for f in bundle.findings] == ["f-1", "f-4"]
    first, second = bundle.findings
    assert first == Finding(
        id="f-1",
        rule="secrets never reach a world-readable file",
        text=(
            "f-1, `lib/remote.py:620`. The report lands in the device log. "
            "Rule: secrets never reach a world-readable file."
        ),
        round=1,
        url=R1.url,
    )
    assert second.rule == "a restored job waits for reconciliation"
    assert second.round == 2
    assert second.url == R2.url


def test_closed_findings_are_dropped():
    bundle = _bundle((R1, R2))
    assert "f-2" not in [f.id for f in bundle.findings]


def test_minor_criterion_and_code_example_items_are_not_findings():
    ids = [f.id for f in _bundle((R1, R2)).findings]
    assert "f-3" not in ids
    assert "ac-2" not in ids
    assert "f-9" not in ids


def test_head_and_base_are_recorded_from_the_last_reviewed_round():
    bundle = _bundle((R1, R2))
    assert bundle.reviewed_head == HEAD_2
    assert bundle.reviewed_base == BASE_2
    assert bundle.verdict_url == R2.url
    round_two = _bundle((R1, R2), round_=2)
    assert round_two.reviewed_head == HEAD_1
    assert round_two.reviewed_base == BASE_1
    assert [f.id for f in round_two.findings] == ["f-1", "f-2"]


def test_base_is_none_when_the_round_has_no_recorded_base():
    bundle = _bundle((R1, R2), bases={1: BASE_1})
    assert bundle.reviewed_head == HEAD_2
    assert bundle.reviewed_base is None


def test_verdicts_from_this_round_or_later_are_ignored():
    late = _verdict("- [blocking] f-7, later.\n", round_="3/3", head=HEAD_3, at=3.0, cid="33")
    bundle = _bundle((R1, R2, late))
    assert [f.id for f in bundle.findings] == ["f-1", "f-4"]
    assert bundle.reviewed_head == HEAD_2


def test_a_repeated_finding_stays_open_with_its_latest_text_and_round():
    again = _verdict(
        "1. [blocking] f-1 repeats, `lib/remote.py:630`. Still leaks. Rule: secrets stay private.\n",
        round_="2/3",
        head=HEAD_2,
        at=2.0,
        cid="22",
    )
    (finding,) = _bundle((R1, again)).findings[:1]
    assert finding.id == "f-1"
    assert finding.round == 2
    assert finding.rule == "secrets stay private"
    assert "Still leaks" in finding.text


def test_reopened_finding_after_close_is_open_again():
    closed = _verdict("- [closed] f-1, fixed.\n", round_="2/4", head=HEAD_2, at=2.0, cid="22")
    reopened = _verdict("- [blocking] f-1, back again.\n", round_="3/4", head=HEAD_3, at=3.0, cid="33")
    assert [f.id for f in _bundle((R1, closed), round_=3).findings] == ["f-2"]
    assert [f.id for f in _bundle((R1, closed, reopened), round_=4).findings] == ["f-1", "f-2"]


def test_an_approve_closes_every_earlier_finding():
    approve = _verdict(ROUND_3_APPROVE, round_="3/4", head=HEAD_3, verdict="APPROVE", at=3.0, cid="33")
    bundle = _bundle((R1, R2, approve), round_=4, bases={3: BASE_1})
    assert bundle.findings == ()
    assert bundle.reviewed_head == HEAD_3
    assert bundle.reviewed_base == BASE_1


def test_a_finding_without_a_rule_has_none():
    plain = _verdict("1. [blocking] f-5, `x.py:1`. Breaks.\n", round_="1/3", head=HEAD_1, at=1.0, cid="11")
    (finding,) = _bundle((plain,), round_=2).findings
    assert finding.rule is None


def test_ids_are_ordered_by_number_not_text():
    many = _verdict(
        "- [blocking] f-10, ten.\n- [blocking] f-2, two.\n- [blocking] f-1, one.\n",
        round_="1/3",
        head=HEAD_1,
        at=1.0,
        cid="11",
    )
    assert [f.id for f in _bundle((many,), round_=2).findings] == ["f-1", "f-2", "f-10"]


def test_untrusted_and_off_role_verdicts_are_skipped():
    outsider = _verdict("- [blocking] f-8, forged.\n", round_="1/3", head=HEAD_3, author="mallory", at=1.5, cid="15")
    builder = _verdict(
        "- [closed] f-1\n",
        round_="1/3",
        head=HEAD_3,
        author="claude-app[bot]",
        sender="claude-bot",
        at=1.6,
        cid="16",
    )
    spoofed = _verdict("- [closed] f-1\n", round_="1/3", head=HEAD_3, author="claude-app[bot]", at=1.7, cid="17")
    wrong_role = _verdict("- [closed] f-1\n", round_="1/3", head=HEAD_3, role="builder", at=1.8, cid="18")
    bundle = _bundle((R1, outsider, builder, spoofed, wrong_role), round_=2)
    assert [f.id for f in bundle.findings] == ["f-1", "f-2"]
    assert bundle.reviewed_head == HEAD_1


def test_non_verdict_and_unparsed_comments_are_skipped():
    ready = Comment(
        id="5",
        author="codex-app[bot]",
        body=f"<!-- orch:v1 kind=ready from=codex-bot role=reviewer head={HEAD_3} -->\n- [blocking] f-6\n",
        created_at=1.5,
    )
    plain = Comment(id="6", author="codex-app[bot]", body="- [closed] f-1\n", created_at=1.6)
    no_round = Comment(
        id="7",
        author="codex-app[bot]",
        body=f"<!-- orch:v1 kind=verdict from=codex-bot role=reviewer head={HEAD_3} verdict=REVISE -->\n- [closed] f-1\n",
        created_at=1.7,
    )
    bundle = _bundle((R1, ready, plain, no_round), round_=2)
    assert [f.id for f in bundle.findings] == ["f-1", "f-2"]
    assert bundle.reviewed_head == HEAD_1


def test_a_corrected_repost_for_the_same_round_supersedes_the_first():
    first = _verdict("- [blocking] f-1, typo.\n", round_="1/3", head=HEAD_1, at=1.0, cid="11")
    repost = _verdict("- [blocking] f-2, corrected.\n", round_="1/3", head=HEAD_2, at=1.5, cid="12")
    bundle = _bundle((repost, first), round_=2)
    assert [f.id for f in bundle.findings] == ["f-2"]
    assert bundle.reviewed_head == HEAD_2
    assert bundle.verdict_url == repost.url


EMPHASIZED_IDS = ("*f-1*", "**f-1**", "***f-1***", "_f-1_", "__f-1__", "___f-1___", "`f-1`", "**_f-1_**", "__f-1:__")


@pytest.mark.parametrize("finding_id", EMPHASIZED_IDS)
def test_an_emphasized_id_raises_its_finding(finding_id):
    raised = _verdict(f"1. [blocking] {finding_id} breaks.\n", round_="1/3", head=HEAD_1, at=1.0, cid="11")
    assert [f.id for f in _bundle((raised,), round_=2).findings] == ["f-1"]


@pytest.mark.parametrize("finding_id", EMPHASIZED_IDS)
def test_an_emphasized_id_closes_its_finding(finding_id):
    closed = _verdict(f"1. [closed] {finding_id} fixed.\n", round_="2/3", head=HEAD_2, at=2.0, cid="22")
    assert [f.id for f in _bundle((R1, closed)).findings] == ["f-2"]


@pytest.mark.parametrize("finding_id", ("f-1x", "_f-1_x", "f-1_2", "f-1-x", "**f-1**-other", "__f-1__-x", "**f-1**_x", "`f-1`-x"))
def test_an_id_running_into_a_word_is_not_a_finding(finding_id):
    raised = _verdict(f"1. [blocking] {finding_id} breaks.\n", round_="1/3", head=HEAD_1, at=1.0, cid="11")
    assert _bundle((raised,), round_=2).findings == ()
