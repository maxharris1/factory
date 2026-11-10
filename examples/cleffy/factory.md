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
---

## Review
One reviewer from a different model family than the builder gives the final verdict. `review.verify` is `none`. The `ui-check` skill stays available for UI changes. Low-risk changes get one round; leftover minors are batched into a follow-up issue. High-risk changes get up to three rounds; every extra round needs Max's own words.

## Ship
`ship: merge` applies to PRs into `dev`. On APPROVE of the covered head, squash-merge with `--match-head-commit`. `dev` → `main` is Max's merge. Never push to `dev` or `main` directly.

## Context
- Risky areas: migrations, RLS, Edge Functions under `supabase/`, and billing/Stripe. Never apply migrations or change config on a hosted Supabase project from an agent session.
- Privacy policy forbids tracking pixels.
- Marketing surfaces use public-domain repertoire only, and the primary CTA is always "Start free".
- PRs target `dev`.
- The existing `ui-check` skill can be used for UI changes.
