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
  verify: none
  followup_from_round: 4
  block_on: user-impact
  auto_extra_rounds: 2
  start_before_ci: true
ship: handoff
finish_line: merge
skills: []
shared_paths: [docs/SPEC.md, docs/decisions/, docs/plans/, README.md]
feeder:
  max_holders_per_path: 4
  exclusive_paths: [model/]
hold:
  approver: maxharris1
  paths:
    - skills/factory/**
    - skills/pstack/**
    - skills/skills.lock
    - grok/**
    - orch.yml
    - lib/hold.py
    - lib/orch_yml.py
    - lib/harness_env.py
    - .github/**
    - factory.md
    - .claude/skills/**
---

## Review
One reviewer from a different model family than the builder (Codex Bot) gives the verdict. `review.verify` is `none`; the reviewer reads the diff. With `review.start_before_ci: true`, the review starts when the builder's `ready` lands, without waiting for CI. The reviewer reads the CI result if it has finished and does not block or REVISE on CI status alone. Low-risk changes get one round. High-risk changes (anything touching ship, handoff, trust or the poller install) get up to three rounds. At the cap, the poller grants the first two extra rounds itself (`review.auto_extra_rounds: 2`, issue #263; Max, 5:30 PM ET 2026-11-14: "Yes, turn on #259 (from round 4), #263 and #262 for the factory repo"). It never grants one for BLOCKED, `repeat=`, `unsat=` or a stuck ticket, and every round past those two needs Max's own words.

The blocking bar is `review.block_on: user-impact` (issue #490). A reviewer REVISEs only for an unmet acceptance criterion, a failed gate (a test, check or build that fails on this head), a bug a user or the factory would hit in normal operation on a path this PR changed, or a shipped doc this PR made wrong. Every other finding is `[follow-up]` from round 1. From round 2, a round checks the fix. The poller parks a trusted verdict's `[follow-up]` findings on one unlabelled issue per PR (issue #492), blocked by the PR. Later rounds add a comment to that issue while it is open and has no intake label. The `done` wake names it, so Grok Bot, the orchestrator, triages it. `review.followup_from_round: 4` still decides when a variant of a rule an earlier round raised stops blocking. From round 4, such a variant is `[follow-up]`. In rounds 2 and 3, the brief still tells the reviewer that a blocker breaking an earlier blocker's rule sets `repeat=`, asks Max and stops.

## Ship
`ship: handoff`, `finish_line: merge`. Nothing ships before CI is green on the approved head, even though the review may start before CI finishes. An APPROVE on red CI queues a CI fix instead, and that fix is a gate retry, not a review round. On APPROVE of the covered head with green CI, the poller marks a draft PR ready and labels it `orch:state/merge-queue`. It then keeps watching the PR until it merges or closes. When the PR conflicts with `main`, the poller queues a Claude `update-from-main` job; an update that only takes one side of each conflict (or both sides, in a `shared_paths` file) keeps the approval, and any other update goes back to Codex for review. The poller squash-merges approved PRs one at a time, oldest approval first, and wakes Grok Bot (Factory) with a `done` wake once a PR is merged. Do not merge these PRs by hand. Two failed updates mark the ticket stuck (`update-failed-2`) and ask Max.

## Context
- PRs target `main`. Tickets enter the queue like any other project's: the feeder picks up an issue labelled `factory:ready`, opens its PR on `factory/issue-<n>`, Grok Bot posts the `brief` and `go`, and the build, review and fix jobs go through `orch wait`.
- Factory PR titles start with the source issue number.
- `orch.yml` also sets `ready_starts_review: true`: a trusted builder `ready` naming the PR head (`<!-- orch:v1 kind=ready from=claude-bot role=builder head=<sha> -->`, posted by a `trusted_logins` login) starts tracking an open PR and, unless CI on that head is red, queues Codex's round-1 review (`review.start_before_ci`), even with no `orch:state/` label (issue #59). A `ready` naming another head, or from anyone else, does not start tracking.
- CI is `.github/workflows/ci.yml`: pytest, the `check-config` steps and the statechart check. CI runs the full suite; a job runs only the tests for the files it touched, with `env -u ORCH_DB`.
- Never read credentials, runner tokens or `FACTORY_WEBHOOK_*` values. Run GitHub as `env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN gh`.
