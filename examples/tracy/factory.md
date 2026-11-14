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
ship: after:tracy-site
skills: []
---

## Review
One reviewer from a different model family than the builder gives the final verdict. `review.verify` is `none`. Low-risk changes get one round; leftover minors are batched into a follow-up issue. High-risk changes get up to three rounds; every extra round needs Max's own words.

## Ship
On factory APPROVE, wait for Tracy's OK on the preview (`ok head=<sha>` naming that SHA). A decline is a fix pass. A push to `main` deploys production on Vercel; other branches get previews. After Tracy approves the preview, `main` is fast-forwarded to that exact SHA (no force-push, no merge commit).

## Context
- Small static marketing site. Work runs on the box.
- Before ship, run lint, typecheck and build, plus the "Never ship" grep in `CLAUDE.md`.
- Copy, pricing, photo and motion rules in `CLAUDE.md` stay.
