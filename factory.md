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
---

## Review
One reviewer from a different model family than the builder (Codex Bot) gives the verdict. `review.verify` is `none`; the reviewer reads the diff and the CI result. Low-risk changes get one round. High-risk changes (anything touching ship, handoff, trust or the poller install) get up to three rounds; every extra round needs Max's own words.

## Ship
`ship: handoff`, `finish_line: merge`. On APPROVE of the covered head with green CI, the poller marks a draft PR ready and labels it `orch:state/merge-queue`. It then keeps watching the PR until it merges or closes. When the PR conflicts with `main`, the poller queues a Claude `update-from-main` job; an update that only takes one side of each conflict keeps the approval, and any other update goes back to Codex for review. The poller squash-merges approved PRs one at a time, oldest approval first, and wakes Grok Bot (Factory) with a `done` wake once a PR is merged. Do not merge these PRs by hand. Two failed updates mark the ticket stuck (`update-failed-2`) and ask Max.

## Context
- PRs target `main`. `orch.yml` lists this repo with `ready_starts_review: true`: PRs here are built outside the queue (a Claude session working from a packet Max approved), so the builder's trusted `ready` starts review with no `go` and no build job.
- To enter the loop, the PR needs a `ready` handoff comment from the builder naming the PR head (`<!-- orch:v1 kind=ready from=claude-bot role=builder head=<sha> -->`, posted by a `trusted_logins` login). No `orch:state/` label is needed (`ready_starts_review` projects are discovered from an open PR's `ready`, issue #59); the poller labels the PR once it tracks it. That `ready` is enough: the next poll starts tracking the PR and, with CI green on that head, queues Codex's round-1 review in the same cycle. No Grok `brief` or `go` is needed. A `ready` naming another head, or from anyone else, does not start tracking. Review, fix and re-review jobs then go through `orch wait`.
- CI is `.github/workflows/ci.yml`: pytest, the `check-config` steps and the statechart check. Run the suite with `env -u ORCH_DB`.
- Never read credentials, runner tokens or `FACTORY_WEBHOOK_*` values. Use `env -u GH_TOKEN gh`.
