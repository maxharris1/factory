# Aut0 cleanup scope (read-only draft, 2026-11-10)

Status: **draft only.** Do not file the Linear ticket, open a PR, or push to `aut0-code/aut0` until Max says go. This is the detail file for plan items 11–12 in [docs/PLAN.md](PLAN.md).

Sourced from `dev` of `aut0-code/aut0` on 2026-11-10, plus merged PRs [#1303](https://github.com/aut0-code/aut0/pull/1303), [#1304](https://github.com/aut0-code/aut0/pull/1304) and [#1314](https://github.com/aut0-code/aut0/pull/1314), closed PRs [#1306](https://github.com/aut0-code/aut0/pull/1306), [#1317](https://github.com/aut0-code/aut0/pull/1317) and [#1321](https://github.com/aut0-code/aut0/pull/1321), and open PRs [#1305](https://github.com/aut0-code/aut0/pull/1305) and [#1318](https://github.com/aut0-code/aut0/pull/1318).

## Goal

Put Aut0 on the slim `factory.md` the new orchestrator reads, and drop the old factory plan's config and statechart tooling, without touching pstack or the in-flight verify-aut0 work.

Max's rules for Aut0 (9:38–9:42 AM ET, 2026-11-10):

- Linear is the ticket source of truth. Every repo change goes through a PR. The orchestrator creates no GitHub issues or labels here.
- Keep a slim per-repo `factory.md` (models, review process, special skills, extra context).
- One reviewer from a different model family gives the final verdict, and that reviewer runs the UI and database checks through `verify-aut0` once it lands.

## Counts

| Bucket | Files on `dev` now | What happens |
|---|---|---|
| Keep (vendored pstack + vendor scripts) | **248** | Leave in place (6 of these get a small edit; see below) |
| Remove | **421** | Delete in the cleanup PR |
| Edit (repo docs / vendor support) | **10** | Pointer and exclusion updates; not deleted |
| Add | **1** | New root `factory.md` (draft below) |

Remove breaks down as:

- 410 files under `.claude/skills/statechart/` plus the `.agents/skills/statechart` symlink (brought back in #1314 only so the old factory could formally verify its state charts)
- 4 files in `create-formal-verification-skill` (`.claude/skills/create-formal-verification-skill/{.pstack-vendored,SKILL.md,agents/openai.yaml}` and `.agents/skills/create-formal-verification-skill`). #1304 had already excluded this skill because it needs `statechart`; #1314 brought it back with `statechart`.
- 7 factory statechart scripts and docs (listed below)

`maintain-formal-verification-skill` stays. It was vendored in #1303, was not part of the #1314 statechart bring-back, and does not need the statechart tools.

## Keep

The 51 vendored pstack skills already on `dev`, their `.agents/skills/` symlinks, and the vendor support files from #1303 / #1304. This matches the post-#1304 vendor set (`statechart` and `create-formal-verification-skill` were not in it).

**Skills (51):** `architect`, `arena`, `benchmark-checklist`, `blast-radius`, `bro`, `correct`, `create-verification-skill`, `figure-it-out`, `how`, `in-your-own-words`, `interrogate`, `ketchup`, `maintain-formal-verification-skill`, `maintain-verification-skill`, `no-comments`, `poteto-help`, `principle-attack-the-premise`, `principle-boundary-discipline`, `principle-build-the-lever`, `principle-encode-lessons-in-structure`, `principle-exhaust-the-design-space`, `principle-experience-first`, `principle-explain-the-number`, `principle-fix-root-causes`, `principle-foundational-thinking`, `principle-guard-the-context-window`, `principle-laziness-protocol`, `principle-make-operations-idempotent`, `principle-migrate-callers-then-delete-legacy-apis`, `principle-minimize-reader-load`, `principle-model-the-domain`, `principle-never-block-on-the-human`, `principle-outcome-oriented-execution`, `principle-prove-it-works`, `principle-redesign-from-first-principles`, `principle-separate-before-serializing-shared-state`, `principle-sequence-verifiable-units`, `principle-subtract-before-you-add`, `principle-test-behavior-not-implementation`, `principle-type-system-discipline`, `recall`, `reflect`, `sharpen`, `show-me-your-work`, `swarm`, `tdd`, `teach`, `technical-writing`, `typescript-best-practices`, `unslop`, `why`.

`poteto-mode` is already not vendored (`EXCLUDE` in `scripts/vendor-pstack.sh`). Leave that exclusion as it is.

**Vendor support (keep, some edited):**

- `scripts/vendor-pstack.sh` — add `statechart` and `create-formal-verification-skill` back to `EXCLUDE`, so a later vendor rerun does not restore them. Current pin stays `519d36aa98955bf680db01bcde5fdfba686dc901` (pstack 0.15.34) until a later bump.
- `scripts/vendor-pstack.test.ts`, `scripts/test-vendor-pstack.sh`
- `scripts/codex-adapter.md` and `.claude/docs/harness/codex.md` — drop the statechart-preflight / Factory-section wording; repo rules still win over pstack, now pointing at `factory.md`
- `.claude/docs/harness/claude-code.md` — only if a Factory-section pointer is present
- `.claude/vendor/pstack/{LICENSE,README.md,SOURCE_SHA,MANIFEST}` — MANIFEST and README update when the two skills are un-vendored
- `.claude/agents/comment-sicko.md`
- `.gitignore` entry `/.claude/vendor/pstack/.staging.*/` — keep

Do **not** touch Aut0-owned skills or agents (`aut0-review`, `ci-and-pr-workflow`, `cloud-dev-stack`, `cpt-acuity-blurbs`, `drive-app-ui`, `nsa-process`, `post-build-polish`, `.claude/agents/aut0-implementer.md`, `.claude/agents/aut0-reviewer.md`). The factory PRs did not add them.

## Remove

Delete these paths, then drop the `test:statechart-preflight` script from `package.json`. Jest already matches `scripts/**/*.test.ts`, so deleting `scripts/statechart-preflight.test.ts` is enough to keep Unit Tests from loading it. `.github/workflows/ci.yml` has no statechart or factory-runner hooks (closed #1306 never merged).

**Tooling and docs (7 files):**

- `docs/aut-2166-factory-follow-up.md`
- `docs/factory/statechart-hosts.md` (and the then-empty `docs/factory/` directory)
- `scripts/factory/install-statechart-tools.sh` (and the then-empty `scripts/factory/` directory)
- `scripts/statechart-mise.sh`
- `scripts/statechart-preflight.sh`
- `scripts/statechart-preflight.test.ts`
- `scripts/test-statechart-preflight.sh`

**Skills (414 files):** the entire `.claude/skills/statechart/` tree (409 blobs plus the folder markers as listed on `dev`; 410 files when the `.agents/skills/statechart` symlink is included) and the 4 `create-formal-verification-skill` files named above.

Also strip from `AGENTS.md`: the `## Factory` fenced block and notes (today lines 84–120, including the statechart host-routing table), the pstack-router row that sends people to `statechart` / `create-formal-verification-skill` and "Factory notes", and the Platforms parenthetical around line 1294. Leave the rest of the pstack router. Replace the Factory section with one pointer line: agent factory config is in `factory.md` (Grok Bot / orchestrator only; human developers can ignore it).

`.claude/CLAUDE.md` line 5 today says "Factory settings and agent notes: see the Factory section of AGENTS.md." Retarget that line to `factory.md`.

## Open verify-aut0 PRs

| PR | Title | State | vs its base | Overlap with this cleanup? |
|---|---|---|---|---|
| [#1305](https://github.com/aut0-code/aut0/pull/1305) | chore: verify-aut0 skill + eval suite | Open, draft, Linear AUT-2129 | `MERGEABLE` against `dev`, but `BEHIND` | No. All 44 files are under `.claude/skills/verify-aut0/` and `.agents/skills/verify-aut0`. |
| [#1318](https://github.com/aut0-code/aut0/pull/1318) | chore(verify-aut0): PLAN.v3 increment 0 — checkpoints, timeouts, dispatch, reservation, harness pin | Open, draft | Base is #1305's branch `max/aut-2129-chore-verify-aut0-skill-eval-suite-pstack-create`, but at older SHA `80401bc8…`. Current #1305 head is `cc8a35a4…`. GitHub reports `CONFLICTING` / `DIRTY` against that base. All 97 files are also under `verify-aut0`. | No overlap with the cleanup paths. It already conflicts with #1305, independent of this work. Rebase #1318 onto current #1305 before merging it; do not fold it into the cleanup PR. |

`verify-aut0` is not on `dev` yet. Keep both PRs open. The slim `factory.md` may name `verify-aut0` now; until #1305 merges, the reviewer lists that check as not run.

Closed PRs #1306, #1317 and #1321 (closed 9:42 AM ET, 2026-11-10) stay closed. Useful Aut0-specific facts from #1317's `factory.md` are reused in the draft below; the 26-field block, fixer lanes, Judge/Council stages and `factory-docs.test.ts` are not.

## Draft `factory.md` for Aut0

```markdown
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
---

## Review
One reviewer from a different model family than the builder gives the final factory verdict and runs `verify-aut0` (UI and database checks) once that skill is on `dev`. Until then, list it in the PR as not run. Low-risk changes get one round, with leftover minors batched into a follow-up Linear ticket. High-risk changes get up to three rounds; every extra round needs Max's own words. `/aut0-review` is a repo skill the reviewer may use. After the factory verdict, Oliver's flow (including Aut0 Judge, if it still runs) takes over. The factory does not run a second judge.

## Ship
On factory APPROVE of the covered head: move the Linear ticket to Ready for Review and mark the PR ready. Do not merge. Max picks every merge. Never request `maxharris1` as a reviewer.

## Context
- Ticket first: if work has no AUT ticket, file one in the aut0-linear-board shape, then copy `gitBranchName` verbatim. Never invent a branch name from a pattern. Older `aut/AUT-n` branches stay as they are.
- The orchestrator creates no GitHub issues or labels in this repo. State lives on the Linear ticket and in PR comments.
- PRs into `dev` or `main` get CI (9 jobs in `.github/workflows/ci.yml`, including pgTAP as `test:db`). There is no pre-merge E2E gate; `deploy.yml` runs Playwright against staging after a push to `dev` or `main`. Feature-branch PRs (for example Lawyer Platform stacked on Oliver's #1262 chain or a parent PR head) get no CI: run typecheck, Jest and pgTAP locally, paste the results, and say that CI did not run. Never push to branches we do not own.
- Human reviewers: olivermillard, KonstantineBiganashvili. Ask Max before any GitHub APPROVE.
- Domain: read `.claude/skills/nsa-process/SKILL.md` first. Aut0 rules win over pstack when they conflict.
```

YAML that #1317 had and this draft drops on purpose: `owner_bot`, `stages` (judge / phi / council), `reviewers: a+b`, `judge` / `judge_session`, fixer lanes and cloud overflow, `tests_gate: factory-gate`, `qa: none`, `small_ticket` path lists, and cloud env IDs. Those were the bloated factory-loop fields. Cloud env IDs can come back later if a real run needs them.

## Linear ticket draft

Do not file this until Max says go. Do not invent a priority; ask Max if it is unset. Shape follows the aut0-linear-board skill (chore: Description, Acceptance Criteria, Notes). Status after filing: unassigned, not In Progress, then Ready for Review once the PR is open.

**Title:** chore: slim factory.md and remove retired statechart tooling

**Description:**
The old factory plan left a `## Factory` block in `AGENTS.md` and vendored the pstack `statechart` skill plus install/preflight scripts so that plan could formally verify its state charts (#1314). That factory repo has been wiped and restarted as a thin pstack orchestrator. Aut0 stays on the existing handoff: Linear is the ticket source of truth, every repo change goes through a PR, and the orchestrator will not create GitHub issues or labels here.

This ticket adds a one-screen root `factory.md` (builder/reviewer preferences, one cross-family reviewer who also runs `verify-aut0`, handoff to Ready for Review, Linear ticket-first rules, special skills, short repo context). It removes the old Factory section, the `statechart` skill tree, `create-formal-verification-skill` (it needs `statechart`), and the seven factory statechart scripts/docs. Vendored pstack otherwise stays. Open verify-aut0 PRs #1305 and #1318 are out of scope.

File-level keep/remove lists and the drafted `factory.md` are in maxharris1/factory `docs/AUT0-CLEANUP.md`.

**Acceptance Criteria:**

1. Root `factory.md` matches the slim format in maxharris1/factory `docs/PLAN.md` section 7 and the Aut0 draft in `docs/AUT0-CLEANUP.md` (Linear tickets, `ship: handoff` to Ready for Review, Max merges, one cross-family reviewer, `verify: verify-aut0`, skills `nsa-process` / `aut0-review` / `verify-aut0`).
2. `AGENTS.md` has no `## Factory` section and no statechart host-routing table. It has one pointer to `factory.md`. The pstack skill router remains, without the `statechart` / `create-formal-verification-skill` row. `.claude/CLAUDE.md` (and any harness adapter that pointed at the Factory section) points at `factory.md`.
3. These paths are gone from `dev`: `.claude/skills/statechart/`, `.agents/skills/statechart`, `.claude/skills/create-formal-verification-skill/`, `.agents/skills/create-formal-verification-skill`, `docs/aut-2166-factory-follow-up.md`, `docs/factory/`, `scripts/factory/`, `scripts/statechart-mise.sh`, `scripts/statechart-preflight.sh`, `scripts/statechart-preflight.test.ts`, `scripts/test-statechart-preflight.sh`. `package.json` no longer has `test:statechart-preflight`.
4. `scripts/vendor-pstack.sh` lists `statechart` and `create-formal-verification-skill` in `EXCLUDE`. The other 51 vendored pstack skills, `scripts/vendor-pstack.sh` itself, and `npm run test:vendor-pstack` still pass. Aut0-owned skills and the open verify-aut0 PRs (#1305, #1318) are untouched.
5. No GitHub issues or labels are created for this work. The PR targets `dev`. Max picks the merge.

**Notes:**

- Closed factory PRs #1306, #1317 and #1321 stay closed.
- #1318 already conflicts with its #1305 base; rebase that separately. This cleanup does not overlap `verify-aut0` paths.
- Until #1305 merges, `factory.md` may name `verify-aut0`, but reviewers must list that check as not run.
- Priority is unset in this draft; set it when filing, after Max names it.
