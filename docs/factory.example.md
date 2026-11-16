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
  # pre_ready:  # optional; a check the factory runs before a builder's ready counts
  #   command: pytest -q tests/fast
  #   timeout_seconds: 300
  # block_on: any-failure  # optional; user-impact uses pstack's blocking bar and files follow-ups from round 1
  # verify_unavailable: true  # optional; the verify skill may not be on the base yet, so the reviewer lists it as not run
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
