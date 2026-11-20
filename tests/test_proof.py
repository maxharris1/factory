"""The proof a builder's ready gives for each acceptance criterion (#1032)."""

from __future__ import annotations

import pytest

from lib.proof import Required, from_detail, missing, required, to_detail

TICKET = """Goal.

Features: upload-file, search
Risk: low
Plan first: no
Depends on: none

## Acceptance criteria
- [ ] The parser accepts the new field.
- qa: Uploading a file shows it in the list.
- ops: restart the daemon on the box.
- qa(search): Search finds the uploaded file.
"""

ITEMS = (
    Required("ac-1", "review", ()),
    Required("ac-2", "qa", ("upload-file", "search")),
    Required("ac-3", "qa", ("search",)),
)

READY = """What changed.

## Proof
- ac-1 (review): `pytest -q tests/test_parser.py`
  ```
  3 passed
  ```
- **ac-2** (qa): `verify run upload-file search`
  ```
  upload-file PASS
  search PASS
  ```
- ac-3 (qa): unverified. The verify skill's doctor failed: `verify doctor`
  ```
  chrome not found
  ```

## Open
Nothing.
"""


def _ready(*entries: str) -> str:
    return "Done.\n\n## Proof\n" + "\n".join(entries) + "\n"


def test_ids_follow_builder_criteria_and_qa_tags_bind_journeys() -> None:
    assert required(TICKET) == ITEMS


def test_an_explicit_id_prefix_wins_over_position_and_may_precede_a_qa_tag() -> None:
    body = TICKET.replace("- qa(search): Search", "- AC-7: qa(upload-file): Search")
    assert required(body) == (
        Required("ac-1", "review", ()),
        Required("ac-2", "qa", ("upload-file", "search")),
        Required("ac-7", "qa", ("upload-file",)),
    )


def test_a_ticket_without_criteria_requires_nothing() -> None:
    assert required("Just prose.") == ()


def test_detail_round_trips() -> None:
    assert from_detail(to_detail(ITEMS)) == ITEMS


@pytest.mark.parametrize(
    "value",
    [
        None,
        "ac-1",
        [{"id": "ac-1", "kind": "manual", "journeys": []}],
        [{"id": "ac-1", "kind": "qa", "journeys": "search"}],
        [{"id": 1, "kind": "review", "journeys": []}],
        [{"kind": "review", "journeys": []}],
    ],
)
def test_a_missing_or_malformed_detail_reads_as_none(value: object) -> None:
    assert from_detail(value) is None


def test_a_full_proof_section_passes() -> None:
    assert missing(ITEMS, READY) == []


def test_missing_entries_are_named_together_in_criterion_order() -> None:
    ready = _ready("- ac-1 (review): `pytest`\n  ```\n  ok\n  ```")
    assert missing(ITEMS, ready) == ["ready has no Proof entry for ac-2, ac-3"]


def test_no_proof_section_misses_every_id() -> None:
    assert missing(ITEMS, "What changed, and the tests passed.\n") == [
        "ready has no Proof entry for ac-1, ac-2, ac-3"
    ]


def test_entries_under_another_heading_do_not_count() -> None:
    ready = READY.replace("## Proof", "## Tests")
    assert missing(ITEMS, ready) == ["ready has no Proof entry for ac-1, ac-2, ac-3"]


@pytest.mark.parametrize(
    "entry",
    [
        "- ac-1 (review): `pytest -q`, 3 passed",
        "- ac-1 (review): `pytest -q`\n  ```\n\n  ```",
        "- ac-1 (review): ran the tests\n  ```\n  3 passed\n  ```",
    ],
)
def test_a_review_entry_needs_a_command_and_its_output(entry: str) -> None:
    (problem,) = missing(ITEMS[:1], _ready(entry))
    assert "ac-1 (review)" in problem


def test_a_qa_entry_must_name_every_bound_journey() -> None:
    ready = _ready("- ac-2 (qa): `verify run upload-file`\n  ```\n  upload-file PASS\n  ```")
    (problem,) = missing(ITEMS[1:2], ready)
    assert "ac-2 (qa)" in problem and "search" in problem and "upload-file," not in problem


def test_a_journey_inside_a_longer_id_does_not_count() -> None:
    ready = _ready("- ac-3 (qa): `verify run search-results`\n  ```\n  search-results PASS\n  ```")
    (problem,) = missing(ITEMS[2:], ready)
    assert "search" in problem


def test_a_qa_entry_needs_a_command_and_its_output() -> None:
    ready = _ready("- ac-3 (qa): ran search, it passed.")
    (problem,) = missing(ITEMS[2:], ready)
    assert "ac-3 (qa)" in problem


@pytest.mark.parametrize(
    "entry",
    [
        "- ac-3 (qa): unverified. `verify doctor`\n  ```\n  \n  ```",
        "- ac-3 (qa): Unverified, the doctor failed.\n  ```\n  chrome not found\n  ```",
    ],
)
def test_an_unverified_entry_needs_the_doctor_command_and_output(entry: str) -> None:
    (problem,) = missing(ITEMS[2:], _ready(entry))
    assert "ac-3 (qa)" in problem and "unverified" in problem


def test_the_first_entry_for_an_id_wins() -> None:
    ready = _ready(
        "- ac-1 (review): no output yet",
        "- ac-1 (review): `pytest`\n  ```\n  ok\n  ```",
    )
    (problem,) = missing(ITEMS[:1], ready)
    assert "ac-1 (review)" in problem


def test_a_nested_bullet_belongs_to_its_entry() -> None:
    ready = _ready("- ac-1 (review): ran it\n  - `pytest -q`\n\n    ```\n    ok\n    ```")
    assert missing(ITEMS[:1], ready) == []


@pytest.mark.parametrize("span", ["`ac-1`", "`ac-1 (review)`", "`AC-1`:", "**`ac-1`**"])
def test_the_ids_own_code_span_is_not_the_command(span: str) -> None:
    (problem,) = missing(ITEMS[:1], _ready(f"- {span} (review): Tests passed.\n  ```\n  3 passed\n  ```"))
    assert "ac-1 (review)" in problem and "command" in problem


def test_a_command_after_a_code_formatted_id_counts() -> None:
    assert missing(ITEMS[:1], _ready("- `ac-1` (review): `pytest -q`\n  ```\n  3 passed\n  ```")) == []


@pytest.mark.parametrize(
    "entry",
    [
        "- ac-2 (qa): `verify run upload-file`\n  ```\n  upload-file PASS\n  unverified checks: 0\n  ```",
        "- ac-2 (qa): `verify run upload-file --unverified=0`\n  ```\n  upload-file PASS\n  ```",
    ],
)
def test_unverified_in_captured_output_or_a_command_does_not_waive_journeys(entry: str) -> None:
    (problem,) = missing(ITEMS[1:2], _ready(entry))
    assert "does not name journey search" in problem


def test_a_feeder_brief_requires_criteria_its_cut_ticket_quote_lost() -> None:
    ticket = (
        "Features: search\nRisk: low\nPlan first: no\nDepends on: none\n\n"
        + "Background. " * 1800
        + "\n\n## Acceptance criteria\n- Parser accepts the field.\n- qa(search): Search finds the export.\n"
    )
    brief = (
        "**Brief**\n\n### Acceptance criteria\n- Parser accepts the field.\n"
        "- qa(search): Search finds the export.\n\n### Ticket\n```\n"
        + ticket[:20_000] + "\n[cut at 20000 characters]\n```\n"
    )
    assert required(brief) == (Required("ac-1", "review"), Required("ac-2", "qa", ("search",)))
