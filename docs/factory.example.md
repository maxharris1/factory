---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: verify-cleffy
  # followup_from_round: 4  # optional; from this round, variants of an earlier finding become a follow-up ticket
  # auto_extra_rounds: 0  # optional; the poller grants this many extra rounds at the cap itself
  # start_before_ci: true  # optional; review starts when the ready lands, ship still needs green CI
ship: merge
# finish_line: merge  # optional; omitted, it follows ship
skills:
  - verify-cleffy
---

## Review
One reviewer from a different model family than the builder gives the final verdict and runs `verify-cleffy` (UI and database checks). Low-risk changes get one round; leftover minors are batched into a follow-up issue. High-risk changes get up to three rounds; every extra round needs Max's own words.

## Ship
On APPROVE of the covered head, squash-merge with `--match-head-commit`.

## Context
PRs target `main`. Mention any repo-specific gotchas here, not in the YAML.
