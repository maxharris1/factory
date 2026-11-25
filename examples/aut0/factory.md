---
base_branch: dev
tickets: linear
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: verify-aut0
ship: handoff
skills:
  - nsa-process
  - aut0-review
  - verify-aut0
human_reviewers: [olivermillard, KonstantineBiganashvili]
qa:
  enabled: false
  gate: report
  visual_hold: true
  runners: [local-docker]
  local_docker:
    environment: qa-local
  profile: supabase
  lane:
    boot: npm run dev
    base_url: http://127.0.0.1:3000
---

## Review
One reviewer from a different model family than the builder gives the final factory verdict and runs `verify-aut0` (UI and database checks) once that skill is on `dev`. Until then, list it in the PR as not run. Low-risk changes get one round, with leftover minors batched into a follow-up Linear ticket. High-risk changes get up to three rounds; every extra round needs Max's own words. `/aut0-review` is a repo skill the reviewer may use. After the factory verdict, Oliver's flow (including Aut0 Judge, if it still runs) takes over. The factory does not run a second judge.

## Ship
On factory APPROVE of the covered head: move the Linear ticket to Ready for Review and mark the PR ready. Do not merge. Max picks every merge. Never request `maxharris1` as a reviewer.

## QA
The QA lane runs on the `local-docker` runner. `qa-local` names no Docker image, so the lane runs in the runner's default image, runs `lane.boot` in the PR's worktree and checks `lane.base_url`. `profile: supabase` only labels the result comment's `env=` field: the factory starts no service for any profile. `lane.boot` and `lane.base_url` are placeholders. Before turning QA on, set `lane.boot` to a command that starts the app and the local Supabase database it needs, and set `local_docker.environment` to a prepared image (a name with `:` or `/`) if the default image lacks them. Set `lane.base_url` to the URL where the app answers. `gate: report` posts the result and never gates a ship. `visual_hold: true` holds a passing UI-changing PR with screenshots or video in `awaiting-ok` until an approver posts `/ok <sha>`. To turn QA on, add the repo's `qa_storage` entry under its project in `orch.local.yml`, then set `enabled: true`.

## Context
- Ticket first: if work has no AUT ticket, file one in the aut0-linear-board shape, then copy `gitBranchName` verbatim. Never invent a branch name from a pattern. Older `aut/AUT-n` branches stay as they are.
- The orchestrator creates no GitHub issues or labels in this repo. State lives on the Linear ticket and in PR comments.
- PRs into `dev` or `main` get CI (9 jobs in `.github/workflows/ci.yml`, including pgTAP as `test:db`). There is no pre-merge E2E gate; `deploy.yml` runs Playwright against staging after a push to `dev` or `main`. Feature-branch PRs (for example Lawyer Platform stacked on Oliver's #1262 chain or a parent PR head) get no CI: run typecheck, Jest and pgTAP locally, paste the results, and say that CI did not run. Never push to branches we do not own.
- Human reviewers: olivermillard, KonstantineBiganashvili. Ask Max before any GitHub APPROVE.
- Domain: read `.claude/skills/nsa-process/SKILL.md` first. Aut0 rules win over pstack when they conflict.
