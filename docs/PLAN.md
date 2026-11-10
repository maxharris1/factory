# Simple orchestrator plan (draft for Max, 2026-11-10)

Status: **draft, nothing built.** Old factory work is paused (Max, 9:19 AM ET). This plan starts from a fresh repo and reuses only the *behavior* of the approved v13.8 state charts, not their implementation or item list.

## 1. Goal

Max, in his words: "I want to use the P-Stack skills to integrate into my Grok bot to be able to use it as an orchestrator to work on projects. I want to be able to develop across Cursor, Claude, and Codex using the Grok bot as my central orchestrator and handoff."

Done means Max can hand Grok Bot a ticket on any project, answer only product questions, and get back a merged (or handed-off) PR that one harness built and a different harness reviewed, with pstack driving how each session works.

## 2. Architecture (four parts, nothing else)

1. **pstack in every harness.** One pinned pstack source, installed natively where Claude Code, Codex and Cursor load skills. pstack (poteto-mode plus the principle skills) decides *how* work gets done. The orchestrator adds no work rules of its own.
2. **One handoff format.** Each piece of work is one GitHub issue plus its PR. The brief, questions, answers, plan, verdicts and "done" are comments on them, with one small machine-readable header (section 5). Labels show the current state. There is no other state store. The dispatcher's local job list can be rebuilt from GitHub at any time.
3. **One Grok Bot skill, `orchestrate-project`.** Grok Bot plans with Max, asks him product questions, decides technical ones, picks the builder and reviewer, and handles everything the dispatcher can't decide.
4. **One dispatcher script, `orch`, a few hundred lines.** It polls GitHub, applies the table in section 4, and wakes an agent only when judgment is needed. Bots wait on it with a blocking `orch wait <role>` shell call. That costs no tokens, and the shell exiting wakes the bot. No webhooks, no new credentials. It uses the existing `gh` login.

Ownership stays as Max decided: **Claude Bot starts every Claude session, Codex Bot every Codex session, Cursor Bot every Cursor CLI job.** Grok Bot and `orch` only queue requests. They never run `claude`, `codex` or `cursor-agent` themselves.

## 3. State chart (simplified from v13.8 charts 1a, 1b, 1c, 1d, 1e, 1g, 1h)

```mermaid
stateDiagram-v2
  [*] --> Intake: Max gives Grok Bot a ticket
  Intake --> Refused: project config invalid
  Intake --> Questions: brief + AC posted on the issue
  Questions --> WaitingOnMax: a real product call (D31, D33-F)
  WaitingOnMax --> Questions: Max answers
  Questions --> Planning: no open product calls, ticket over 3 files (D36)
  Questions --> Building: no open product calls, 3 files or fewer
  Planning --> PlanCheck: plan.md maps each AC to code and a test
  PlanCheck --> Planning: an AC is unmapped (once)
  PlanCheck --> Building: every AC mapped
  PlanCheck --> Stuck: gaps a 2nd time
  Building --> Review: builder posts ready, CI green on that head
  Building --> Building: CI red, fix (gate retries don't count as rounds)
  Building --> Stuck: CI red 3 times in a row, or the job dies twice
  Review --> Fixing: REVISE, under the round cap
  Fixing --> Review: fixed head ready, CI green
  Review --> Capped: REVISE at the cap, or a 2nd AC-unmet verdict
  Review --> Blocked: BLOCKED
  Review --> Ship: APPROVE on the current head
  Ship --> Review: head changed and the patch differs (D33-D)
  Ship --> Merged: ship = merge
  Ship --> HandedOff: ship = handoff (Max merges, or the Aut0 RfR handoff)
  Ship --> WaitingExternal: ship = after an approver (Tracy Site)
  WaitingExternal --> Merged: OK names the previewed SHA
  WaitingExternal --> Fixing: declined (one fix pass, 3rd decline = Stuck)
  Merged --> PostMerge: project has a post-merge check
  Merged --> Done
  PostMerge --> Done: passes
  PostMerge --> Done: fails, blocking follow-up issue filed
  Building --> WaitingOnMax: question mid-work
  Fixing --> WaitingOnMax: question mid-work
  WaitingOnMax --> Building: answer, same step resumes
  WaitingOnMax --> Fixing: answer, same step resumes
  WaitingOnMax --> Abandoned: Max drops it
  Capped --> Fixing: Max's own words OK one more round
  Done --> [*]
```

Note: Refused, Stuck, Blocked, Capped and Abandoned are terminal unless Max acts. Each one wakes Grok Bot once, and Grok Bot tells Max once.

**What each approved invariant becomes:**

| v13.8 rule | Where it lives now |
|---|---|
| QuestionsBeforeBuilding, QuestionResumesWhereItPaused | Grok Bot skill (questions step) + dispatcher T5 (resume the same step) |
| MappedBeforeBuilding, MappingApprovedForCurrentArtifact | PlanCheck is a review of `plan.md` at a SHA. A scope-changing answer sends the ticket back to Planning (skill judgment) |
| GateLoopsBounded, DeclinesBounded, StopsAfterTwoACUnmet, round cap | Counters in labels, enforced mechanically by the dispatcher |
| ShipPinnedToCoveredHead, InheritedVerdictOnlyOnExactRebase | T4: merge only with `--match-head-commit` on the approved SHA. A moved head carries the approval only if `git patch-id --stable` is equal. Otherwise it gets a re-review |
| OnlyTheHarnessBotStarts | Only owning bots run harnesses. `orch` refuses to exec a harness binary |
| RerouteKeepsCrossFamily, OnlyUrgentUsesReserve, 20% reserve | Routing table in project config plus the owning bot's usage check (section 6) |
| ContradictoryConfigNeverRuns | `orch check-config` refuses an inconsistent project config (e.g. a same-family judge where cross-family is required) |
| ExitCodesMeanOneThing (15/16/17/18) | Labels: `waiting:max`, `abandoned`, `waiting:external`, `waiting:deploy` |
| Slots, PC offline 60 min, LivePatch, lane cutover, MissionControl | Phase 2 (slots), or dropped (section 9) |

## 4. Dispatcher transition table

`orch` polls each configured repo every 60 s (`env -u GH_TOKEN gh`). It reads only labelled issues/PRs and comments that carry an `orch:` header. Every row is a plain `if`. It's idempotent: each handled comment ID is recorded, so a restart never acts twice.

| # | Event (mechanical) | Known answer | Wakes |
|---|---|---|---|
| T1 | Issue gets `orch:go` (Grok Bot, after Questions) | Queue a `build` job (or `plan` if over 3 files) for the builder from config | Builder's owning bot |
| T2 | `ready head=<sha>` comment, then CI finishes on that SHA | Green: queue `review` for the reviewer. Red: queue `fix` with the failing log (gate count +1; 3rd in a row → `stuck`) | Reviewer's or builder's bot |
| T3 | `verdict=REVISE` | Round under the cap: queue `fix` with the verdict link. At the cap: label `capped` | Builder's bot, or Grok Bot at the cap |
| T4 | `verdict=APPROVE head=<sha>` | Head equals sha, or patch-id equal → ship per config (`merge` = `gh pr merge --squash --match-head-commit`, `handoff`, `after:<bot>`). Else queue a re-`review` | Nobody on merge. The approver bot for `after:` |
| T5 | `questions` comment from any session, then `answers` | Label `waiting:max`. On answers, re-queue the same step with the answer linked (not a round) | Grok Bot (it decides technical ones itself) |
| T6 | Job exits with no handoff comment, or runs past its timeout | Retry once. 2nd time → `stuck` | Grok Bot on the 2nd |
| T7 | `verdict=BLOCKED`, `stuck`, `capped`, config refused, anything unparsed | No known answer | Grok Bot, once |
| T8 | `ok sha=<sha>` / `decline` from the external approver | OK on the previewed SHA → ship. Decline → `fix` (3rd → `stuck`) | Builder's bot on decline |
| T9 | PR merged and the project has `post_merge` | Queue a `post-merge` job (e.g. deployed QA once the SHA is served, bounded wait). A fail files a blocking follow-up issue | QA owner bot |

T1–T6 are the core loop. T7–T9 are added only when a project needs them. Any new row needs a real run that showed a wasted wake.

## 5. Handoff comment format

Each handoff is one comment. Its first line is a header the dispatcher parses, and the rest is plain prose for people and agents:

```
<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round=2/3 head=3f9c2ab ac=met verdict=REVISE risk=high -->
**Review round 2 of 3: REVISE** (Codex Bot, gpt-6-astra high)
1. [blocking] tools/x.py:40, empty result treated as resolved. Repro: `…`
2. [minor] … (batched into follow-up if low risk)
Full report: <link>
```

The comment kinds are `brief`, `questions`, `answers`, `plan`, `ready`, `verdict`, `ok`, `decline`, `done`, `stuck`. Labels hold the current state, round and counters: `orch:state/<name>`, `orch:round/<n>`, `orch:risk/<low|high>`, `orch:builder/<harness>`. Max can read everything on GitHub, and nothing else is needed to resume.

## 6. Grok Bot orchestration skill (`orchestrate-project`)

1. **Intake.** It reads the project's `orch.yml`, then writes the issue `brief`: goal, acceptance criteria, files likely touched, a risk class, and done-evidence (pstack orchestrate kickoff rules).
2. **Questions (D31, always).** It posts the restatement (in-your-own-words) and the open questions. Each question gets classified:
   - Observable: settled with a prototype.
   - Default-able: decided by the bot and recorded.
   - Product call: asked of Max, one at a time, in chat. The answers get posted as an `answers` comment.
   Then the skill sets `orch:go`.
3. **Routing (technical, the bot's call).** It picks the builder and the model by fit for the job, from the project's routing table. The reviewer has to come from a different model family where the config says `cross_family: required`. Aut0 is `off`, so it stays Codex-judged (D46). Codex reviews by default, and merges happen on Codex approval.
   - **Risk class:** `low` (docs, cleanup, monitoring) gets one review round, with minors batched into a follow-up issue. `high` (ship, merge, sign-off, credential, isolation code) gets up to 3 rounds. Every extra round needs Max's own words.
4. **On a T5/T7 wake.** It answers technical questions itself, relays product ones to Max, and reports a stop once, with evidence and a recommended next step.
5. **Never:** launch a harness, create logins or credentials, or treat another agent's approval as Max's.

The builder and reviewer briefs say "run as poteto-mode". The builder follows the playbook that fits the ticket (feature, bug-fix: repro first, refactor). For tickets over 3 files, it starts with an architect scaffold and `plan.md`. Before `ready`, it does a self-check: prove-it-works, deslop, no-comments. The reviewer runs interrogate and blast-radius, can run code in a throwaway copy, and blocks only on a demonstrated failure (D33-A/B).

**Usage (D35/D44).** Before starting a job, each owning bot checks its harness's usage. At the last 20% of the cycle, non-urgent jobs go to the next-best fit, and urgent jobs (the resumed paused step, past-ship work, or anything Max marks urgent) may use the reserve. If a harness has no verified usage source, routing is best fit only.

## 7. Installing pstack in each harness

One source: `skills/` in the new repo, holding a pinned pstack (0.15.36 today) plus a `skills.lock` with the commit and sha256 of each file. `orch install-skills` symlinks the same files into:

| Harness | Local path (per bot user, per machine) | Cloud runs |
|---|---|---|
| Claude Code | `~/.claude/skills/<name>/` | Vendored into the project at `.claude/skills/` |
| Codex | `~/.agents/skills/<name>/` (`~/.codex/skills` is the deprecated alias) | Vendored at `.agents/skills/` |
| Cursor | `~/.cursor/skills/` or a Cursor plugin install of pstack | `.agents/skills/` or `.cursor/skills/` |

`orch doctor` runs a canary prompt in each harness, started by that harness's own bot. The canary asks the harness to name the poteto-mode skill and quote a token from it. It fails if any harness didn't load the pinned version. Bumps go through one PR that updates the pin and the lock together.

## 8. Build list (each item is one PR)

**Phase 1: one machine (the box), end-to-end loop**

1. Repo skeleton: README, `orch.yml` schema plus a validator (`orch check-config`), and an example config with Aut0, Cleffy and Tracy shown only as examples.
2. pstack source and pin: `skills/`, `skills.lock`, `orch install-skills`, and `orch doctor` with a load canary in all three harnesses.
3. Handoff format: a parser and writer for `orch:v1` headers, the label conventions, and tests on fixture comments.
4. Dispatcher core: the poll loop, the handled-comment ledger, and T1 + T2 + T6, with fake bots in tests.
5. Dispatcher review loop: T3 + T4 (round cap by risk class, `--match-head-commit` merge, patch-id carry-over) + T5.
6. `orch wait <role>`, the blocking wait for each owning bot, and an `orch job done` helper. Add a one-paragraph "orch jobs" section to the Claude Bot, Codex Bot and Cursor Bot handoff skills.
7. Grok Bot skill `orchestrate-project`: intake, questions, routing, and stop handling.
8. Usage reserve and routing: the fit table, the 20% reserve check in each owning bot, and best-fit-only while a source is unverified.
9. Project hooks: T7 + T8 (external OK, for Tracy) + T9 (post-merge check, for Cleffy deployed QA and the 1-in-10 post-merge audit task).
10. Live acceptance: three real tickets, one each on three projects that Max picks, run end to end. Fix only what those runs expose.

**Phase 2: more compute and self-hosted CI (after Phase 1 works)**

11. Device pool: `machines.yml` with the box, PC and Mac (Tailscale names), a declared safe maximum of slots per device, and each harness counted as eligible only where Max has provisioned its login under that bot's user.
12. Remote start: each owning bot starts its harness on a device over Tailscale SSH as its own user (claude-bot, codex-bot, cursor-bot). A slot stays held until the worker is confirmed gone. 60 minutes offline means `stuck`.
13. Self-hosted CI: runner labels per project in `orch.yml`, plus a registration checklist for Max. Max registers the runners himself, and the plan never touches runner tokens. Credential-holding deploy workflows stay hosted (D21).
14. `orch status`: a read-only text summary of queue, slots and paused items, generated from GitHub labels and the local job list.

## 9. Deliberately left out

These are out until a real run shows they're needed:
- The bash driver, exit-code protocol and run directories. Labels and comments replace them.
- Two reviewers plus a separate Judge. One cross-family reviewer gives the verdict.
- A separate pre-merge QA stage. The reviewer runs `verify-<repo>` when the project has one.
- Byte-identical rebase proofs. These are replaced by patch-id plus re-review.
- The live reader patch, lane coexistence and old/new cutover, since there's no old lane in a fresh repo.
- The mission-control dashboard. `orch status` (item 14) covers it.
- Per-run `decisions.tsv` and receipts. The PR comments are the trail.
- Self-improvement routines (weekly correct, reflect, blinded evals, daily maintain jobs).
- Webhooks, and any new credential or login.
- The genericity purge and protected-field base reads. The repo starts generic, and `orch.yml` is read from the base branch.
- Arena and swarm by default. They stay available to the builder as pstack tools.

## 10. Phase 1 estimate

Items 1–9 are small, and most can run in parallel after item 3: items 2, 7 and 8 are independent of 4–6. Expect about **2–3 working days**, with a first end-to-end ticket on day 2 and item 10 on day 3. If Max says go on Tue 11-10, Phase 1 would finish **Thu 11-12 to Fri 11-13 ET**. Phase 2 is about 2 more days, plus Max's runner and login setup.

## 11. Proposed repo

`maxharris1/orch` (private)

```
orch/
  README.md
  bin/orch               # CLI: poll, wait, job, check-config, install-skills, doctor, status
  lib/                   # handoff.py (headers/labels), transitions.py (table), gh.py, usage.py
  skills/                # pinned pstack + skills.lock
  grok/orchestrate-project/SKILL.md
  bots/                  # short "orch jobs" sections for Claude/Codex/Cursor Bot skills
  config/orch.example.yml
  tests/                 # fake GitHub + fake bots, one test per transition row
  docs/PLAN.md           # this file
```
