# Plan: #168 Prompt refactor T01c: verdict text

Ticket: https://github.com/maxharris1/factory/issues/168 · PR #231 · branch `factory/issue-168` · risk high · depends on T01b (#167, merged as 636bcb4).

## Architect scaffold

Every brief is built by `render_brief` (`lib/brief/__init__.py`), which joins the same sections for every job kind. Two of them carry the text this ticket changes:

| Where | What it says today | Seen by |
|---|---|---|
| `lib/brief/rules.py` `review_rules()` | heading "(the factory-review-rules contract, docs/SPEC.md section 8)"; "Blocks only on a demonstrated failure."; "A second AC-unmet verdict caps the ticket, even if rounds remain."; "If the `factory-review-rules` skill is installed for you, read it as well." | every brief (build, plan, fix, review, merge-only, remote) |
| `lib/brief/review.py` `task_lines()` step 3 | "Block only on a demonstrated failure, with a repro." | review brief |
| `lib/brief/review.py` `handoff()` | header with `ac=<met\|unmet> verdict=<APPROVE\|REVISE\|BLOCKED>`, but no meaning given for either field | review brief |

The verdict and `ac` values the parser accepts are `lib/handoff.py` `VERDICTS = ("REVISE", "APPROVE", "BLOCKED")` and `AC_VALUES = ("met", "unmet")`. Their effects (docs/SPEC.md section 9): REVISE queues a fix, APPROVE ships the covered head, BLOCKED wakes Grok Bot. The definitions below follow those effects. No parser, dispatcher or SPEC change is in scope: T16 owns the true AC-unmet rule.

Tests that pin the old text: `tests/test_brief.py:133-134` (asserts "factory-review-rules" and "Blocks only on a demonstrated failure" in every kind) and `tests/test_brief.py:246` (uses `text.index("factory-review-rules")` as the marker that the standard parts precede factory.md). All eight `tests/snapshots/brief/*.txt` contain the review-rules section, so they are regenerated with `python tests/regenerate_brief_snapshots.py` (never by hand).

## Work

### W1. `lib/brief/rules.py` `review_rules()`
- Heading becomes `## Review rules (docs/SPEC.md section 8)`.
- "Blocks only on a demonstrated failure." becomes "That reviewer marks a finding `[blocking]` only when they have run it and seen it fail." (third person, since this section is shared by every role).
- Delete "- A second AC-unmet verdict caps the ticket, even if rounds remain."
- Delete "- If the `factory-review-rules` skill is installed for you, read it as well."
- Add the definitions, so builders reading a REVISE and reviewers writing one share them:
  - "- `ac=met`: every acceptance criterion in the ticket is met at the reviewed head. `ac=unmet`: at least one is not; the verdict names which."
  - "- `verdict=APPROVE`: ship this head as it is. Only with `ac=met` and no `[blocking]` finding."
  - "- `verdict=REVISE`: the builder must change the PR: at least one `[blocking]` finding, or `ac=unmet`."
  - "- `verdict=BLOCKED`: you could not review at all; say what you need."

### W2. `lib/brief/review.py`
- Step 3 becomes: "3. You may run code in a throwaway copy. Mark a finding `[blocking]` only when you have run it and seen it fail, and give the repro."
- The handoff example keeps its format; it points at the definitions in the Review rules section ("Pick `ac` and the verdict by the definitions in the Review rules below.").

### W3. `lib/brief/__init__.py` module docstring
- Drop "the `factory-review-rules` contract" from the description of the review-rules part (one phrase; keeps the package's own description true). README line 699 has the same phrase; it is not a brief and stays for a docs pass.

### W4. `tests/test_brief.py`
- Line 133-134: assert `"factory-review-rules" not in text`, `"second AC-unmet" not in text`, and that the Review rules heading `## Review rules (docs/SPEC.md section 8)` is present.
- Line 246: use `text.index("## Review rules")` as the marker.

### W5. `tests/test_brief_verdicts.py` (new)
Renders every job kind (build, plan, fix, review, merge-only, plus a remote-device render and every snapshot case from `tests/regenerate_brief_snapshots.py`) and asserts:
- no brief contains "factory-review-rules", "second AC-unmet" or "demonstrated failure";
- every brief defines `ac=met`, `ac=unmet`, `verdict=APPROVE`, `verdict=REVISE` and `verdict=BLOCKED`, with BLOCKED's text "you could not review at all; say what you need";
- the defined verdicts are exactly `lib.handoff.VERDICTS` and the defined `ac` values exactly `lib.handoff.AC_VALUES` (so text and parser cannot drift);
- the review brief contains "Mark a finding `[blocking]` only when you have run it and seen it fail", and every brief contains the shared third-person form.

### W6. Snapshots
`python tests/regenerate_brief_snapshots.py`, then review the diff: only the Review rules section and review step 3 change.

### W7. Prove it
`env -u ORCH_DB python -m pytest -q` (full suite, as CI runs it), plus the `check-config` and statechart steps from `.github/workflows/ci.yml`. `grep -rn "factory-review-rules\|second AC-unmet" lib/brief tests/snapshots` returns nothing.

## Acceptance criteria map

| Criterion | Work that meets it | How it is checked |
|---|---|---|
| No brief contains "factory-review-rules" or "second AC-unmet". | W1 (heading, skill sentence, cap sentence removed), W3, W6 | W5 absence test over every kind and snapshot; W4 updated assertion; W7 grep |
| The three verdicts and `ac` are defined. | W1 (definitions in the shared Review rules), W2 (handoff points at them) | W5 definition test, tied to `lib.handoff.VERDICTS` / `AC_VALUES` |
| The new blocking wording is present. | W1 (shared form), W2 (exact sentence in review step 3) | W5 wording test; review snapshot |
| `tests/test_brief.py` passes with the updated assertion. | W4 | W7 full pytest run |

## Out of scope
- docs/SPEC.md section 8 and `model/Orchestrator.tla` still state the AC-unmet cap; the dispatcher behavior is unchanged. T16 brings back the true rule.
- README line 699 wording.
