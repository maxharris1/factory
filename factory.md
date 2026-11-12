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
skills: []
---

## Review
One reviewer from a different model family than the builder (Codex Bot) gives the verdict. `review.verify` is `none`; the reviewer reads the diff and the CI result. Low-risk changes get one round. High-risk changes (anything touching ship, handoff, trust or the poller install) get up to three rounds; every extra round needs Max's own words.

## Ship
`ship: handoff`. On APPROVE of the covered head with green CI, the poller marks a draft PR ready, labels it `orch:state/handed-off` and wakes Grok Bot (Factory) with a `done` wake. Factory merges. The poller never merges this repo.

## Context
- PRs target `main`. `orch.yml` lists this repo with `ready_starts_review: true`: PRs here are built outside the queue (a Claude session working from a packet Max approved), so the builder's trusted `ready` starts review with no `go` and no build job.
- To enter the loop, the PR needs one `orch:state/` label (discovery is PR-only and label-based) and a `ready` handoff comment from the builder naming the head (`<!-- orch:v1 kind=ready from=claude-bot role=builder head=<sha> -->`). Review, fix and re-review jobs then go through `orch wait`.
- CI is `.github/workflows/ci.yml`: pytest, the `check-config` steps and the statechart check. Run the suite with `env -u ORCH_DB`.
- Never read credentials, runner tokens or `FACTORY_WEBHOOK_*` values. Use `env -u GH_TOKEN gh`.
