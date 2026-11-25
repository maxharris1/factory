---
base_branch: dev
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
ship: merge
skills: []
qa:
  enabled: false
  gate: report
  visual_hold: true
  runners: [local-docker]
  local_docker:
    environment: qa-local
  profile: full-stack
  lane:
    boot: npm run dev
    base_url: http://127.0.0.1:3000
---

## Review
One reviewer from a different model family than the builder gives the final verdict. `review.verify` is `none`. The `ui-check` skill stays available for UI changes. Low-risk changes get one round; leftover minors are batched into a follow-up issue. High-risk changes get up to three rounds; every extra round needs Max's own words.

## Ship
`ship: merge` applies to PRs into `dev`. On APPROVE of the covered head, squash-merge with `--match-head-commit`. `dev` → `main` is Max's merge. Never push to `dev` or `main` directly.

## QA
The QA lane runs on the `local-docker` runner. `qa-local` names no Docker image, so the lane runs in the runner's default image, runs `lane.boot` in the PR's worktree and checks `lane.base_url`. `profile: full-stack` only labels the result comment's `env=` field: the factory starts no service for any profile. `lane.boot` and `lane.base_url` are placeholders. Before turning QA on, set `lane.boot` to a command that starts the app and every service it needs, including the local Supabase database and the Edge Functions under `supabase/`, and set `local_docker.environment` to a prepared image (a name with `:` or `/`) if the default image lacks them. Set `lane.base_url` to the URL where the app answers. `gate: report` posts the result and never gates a ship. `visual_hold: true` holds a passing UI-changing PR with screenshots or video in `awaiting-ok` until an approver posts `/ok <sha>`. To turn QA on, add the repo's `qa_storage` entry under its project in `orch.local.yml`, then set `enabled: true`.

## Context
- Risky areas: migrations, RLS, Edge Functions under `supabase/`, and billing/Stripe. Never apply migrations or change config on a hosted Supabase project from an agent session.
- Privacy policy forbids tracking pixels.
- Marketing surfaces use public-domain repertoire only, and the primary CTA is always "Start free".
- PRs target `dev`.
- The existing `ui-check` skill can be used for UI changes.
