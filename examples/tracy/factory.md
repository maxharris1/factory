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
qa:
  enabled: false
  gate: report
  visual_hold: true
  runners: [local-docker]
  local_docker:
    environment: qa-local
  profile: none
  lane:
    boot: npm run dev
    base_url: http://127.0.0.1:3000
---

## Review
One reviewer from a different model family than the builder gives the final verdict. `review.verify` is `none`. Low-risk changes get one round; leftover minors are batched into a follow-up issue. High-risk changes get up to three rounds; every extra round needs Max's own words.

## Ship
On factory APPROVE, wait for Tracy's OK on the preview (`ok head=<sha>` naming that SHA). A decline is a fix pass. A push to `main` deploys production on Vercel; other branches get previews. After Tracy approves the preview, `main` is fast-forwarded to that exact SHA (no force-push, no merge commit).

## QA
The QA lane runs on the `local-docker` runner. `qa-local` names no Docker image, so the lane runs in the runner's default image, runs `lane.boot` in the PR's worktree and checks `lane.base_url`. `profile: none` only labels the result comment's `env=` field: the factory starts no service for any profile. `lane.boot` and `lane.base_url` are placeholders. Before turning QA on, set `lane.boot` to a command that starts the app. A static marketing site needs no backend. Set `lane.base_url` to the URL where the app answers. `gate: report` posts the result and never gates a ship. `visual_hold` applies only to `ship: merge` or `ship: handoff`, so under `ship: after:tracy-site` Tracy's preview OK stays the visual gate. To turn QA on, add the repo's `qa_storage` entry under its project in `orch.local.yml`, then set `enabled: true`.

## Context
- Small static marketing site. Work runs on the box.
- Before ship, run lint, typecheck and build, plus the "Never ship" grep in `CLAUDE.md`.
- Copy, pricing, photo and motion rules in `CLAUDE.md` stay.
