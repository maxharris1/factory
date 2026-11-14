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
ship: handoff
finish_line: merge
skills: []
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
One reviewer from a different model family than the builder (Codex Bot) gives the verdict. `review.verify` is `none`; the reviewer reads the diff and the CI result. Low-risk changes get one round. High-risk changes (anything touching ship, handoff, trust or the poller install) get up to three rounds; every extra round needs Max's own words.

## Ship
`ship: handoff`, `finish_line: merge`. On APPROVE of the covered head with green CI, the poller marks a draft PR ready and labels it `orch:state/merge-queue`. It then keeps watching the PR until it merges or closes. When the PR conflicts with `main`, the poller queues a Claude `update-from-main` job; an update that only takes one side of each conflict keeps the approval, and any other update goes back to Codex for review. The poller squash-merges approved PRs one at a time, oldest approval first, and wakes Grok Bot (Factory) with a `done` wake once a PR is merged. Do not merge these PRs by hand. Two failed updates mark the ticket stuck (`update-failed-2`) and ask Max.

## Context
- PRs target `main`. Tickets enter the queue like any other project's: the feeder picks up an issue labelled `factory:ready`, opens its PR on `factory/issue-<n>`, Grok Bot posts the `brief` and `go`, and the build, review and fix jobs go through `orch wait`.
- Factory PR titles start with the source issue number.
- `orch.yml` also sets `ready_starts_review: true`: a trusted builder `ready` naming the PR head (`<!-- orch:v1 kind=ready from=claude-bot role=builder head=<sha> -->`, posted by a `trusted_logins` login) starts tracking an open PR and, with CI green on that head, queues Codex's round-1 review, even with no `orch:state/` label (issue #59). A `ready` naming another head, or from anyone else, does not start tracking.
- CI is `.github/workflows/ci.yml`: pytest, the `check-config` steps and the statechart check. Run the suite with `env -u ORCH_DB`.
- Never read credentials, runner tokens or `FACTORY_WEBHOOK_*` values. Use `env -u GH_TOKEN gh`.
