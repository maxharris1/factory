# Factory specification, plan and to-do

Status: **draft specification** for [maxharris1/factory](https://github.com/maxharris1/factory). Nothing is built yet. This document is the full spec, plan and to-do list for the simplified factory. It is based on [docs/PLAN.md](PLAN.md) at `aa941d27`, [docs/AUT0-CLEANUP.md](AUT0-CLEANUP.md), the approved state chart, and Max's decisions on 2026-11-10 (America/New_York). It does not invent decisions. Anything still open is in [Open questions](#16-open-questions). Technical decisions by Factory are recorded as "Factory decided (technical)" in the Decisions table and in the section they affect.

Shorter companion: [docs/PLAN.md](PLAN.md). Aut0 cleanup detail: [docs/AUT0-CLEANUP.md](AUT0-CLEANUP.md).

## Decisions on 2026-11-10 (ET)

| When | Decision | Source |
|---|---|---|
| 9:38 AM | One reviewer from a different model family than the builder gives the final verdict. | Max, Q1 |
| 9:38 AM | That reviewer also runs the UI and database checks, through the project's verify skill. | Max, Q2 |
| 9:38 AM | The repo stays named `factory`. The old `maxharris1/factory` was deleted and recreated empty for this plan. A backup of the retired repo is on the box at `/workspace/factory-backup-20261110`. | Max, Q3 |
| 9:38–9:42 AM | Aut0 (`aut0-code/aut0`) uses the existing handoff. Linear is the ticket source of truth. Every repo change goes through a PR. The orchestrator creates no GitHub issues or labels there. | Max, Q4 |
| 9:42 AM | Every project repo keeps a slim `factory.md` (models, review process, special skills, extra context). Simplify it if the old config has become bloated. | Max |
| 9:42 AM | Aut0 PRs #1306, #1317 and #1321 were closed without merging. They stay closed. | Factory, with Max's earlier agreement |
| 9:49 AM | The pstack `statechart` skill is a core part of pstack and is kept, including `create-formal-verification-skill` and the Aut0 install/preflight scripts, so we can build evaluation systems that measure the factory. | Max |
| 9:49 AM | Cleffy's deployed QA and its 1-in-10 post-merge audit are deferred. Phase 1 stays lean. | Max skipped that question; recorded as deferred |
| 10:18 AM | Add F1-15: migrate Cleffy (`maxharris1/Cleffy`) and Tracy (`maxharris1/tracy-harris-coaching`) to slim `factory.md`. | Max |
| 10:18 AM | Priority for the Aut0 cleanup Linear ticket (F1-11) is High. | Max |
| 10:19 AM | Default job timeout of no heartbeat or progress, first set at 90; superseded on Nov 11 by 60 minutes (below). A project may override it with `job_timeout` in `orch.yml`. After that, T6 retries once and then marks the job stuck. | Factory decided (technical) |
| 10:19 AM | Phase 1 routes on best fit only, plus each owning bot's own reserve check. A verified usage source is added per harness when its bot can report one. This is not a Phase 1 blocker. | Factory decided (technical) |
| 10:19 AM | Self-hosted runner labels go in each repo's `factory.md` (`ci_runner:`), because they are repo policy. The device inventory goes in `orch.yml`. | Factory decided (technical) |
| 10:19 AM | Optional model hints have no fixed allow-list. `check-config` warns on an unknown model name, and the owning bot can refuse. | Factory decided (technical) |
| 10:19 AM | Extra `orch:v1` header keys are added only when a real run needs them. This is a standing rule, not an open question. | Factory decided (technical) |
| 10:19 AM | Factory merges factory-repo PRs itself once the cross-family reviewer approves the current head and CI is green. Product calls still go to Max. Max's words: "Yes, keep merging approved PRs in the new factory repo". | Max |
| earlier today, still in force | Claude Bot starts every Claude session, Codex Bot every Codex session, Cursor Bot every Cursor CLI job. Grok Bot and the dispatcher never launch those binaries. | Standing rule |
| earlier today, still in force | Max provisions logins and registers runners himself. The factory does not create logins or credentials without his OK, and never reads credentials, runner tokens, or `FACTORY_WEBHOOK_*` values. | Standing rule |
| earlier today, still in force | Product questions go to Max, one at a time, in his own words for approvals that need them. Technical questions are decided by the orchestrator. | Standing rule |
| earlier today, still in force | Every ticket gets a clarifying-questions step before building (D31). | Approved invariant, kept |
| Nov 11, 2:31 PM | The dispatcher may send outbound deliveries to the existing Grok routine webhook. It does not accept inbound webhooks or create credentials. | Factory decided (technical) |
| Nov 11, 9:09 PM | F2-01 inventory: the PC is the WSL Ubuntu node `pc-wsl-bots.tail68624b.ts.net` (not the Windows host), 3 slots, Claude, Codex and Cursor under `claude-bot`, `codex-bot` and `cursor-bot`, shipped `enabled: false` until Max OKs SSH; jobs stay off `/mnt/c`. The Mac is `maxs-macbook-pro.tail68624b.ts.net`, 2 slots, `enabled: false`; its bot users do not exist and SSH is off. No device has SSH yet. A device that drops mid-job keeps its slot and goes stuck through the heartbeat timeout, never done. | Max |
| Nov 11, 9:09 PM | The device inventory lives in `machines.yml`, not `orch.yml`. The box is the one `local: true` device and needs no Tailscale name; it keeps 3 slots because the box runs at most 3 heavy local agent sessions at once (slots are generic, not one per bot). Two devices may not share a `tailscale_name`, so one host has one `slot_max`. Remote devices name a `workdir_root` that is not under `/mnt/`. The scheduler picks the first eligible device in file order. Apart from a requeue (a claim undone before work starts), a slot is freed only by an explicit release after the worker is gone. | Factory decided (technical) |
| Nov 11 | A job goes stuck after 60 minutes with no heartbeat or progress, not 90: the default job timeout is 60 minutes. A project may still override it with `job_timeout` in `orch.yml`; T6 still retries once, then marks the job stuck. | Factory decided (technical) |
| Nov 11 | The PC's `workdir_root` is `/home/<bot>/factory-work` on `pc-wsl-bots`, written `/home/{bot}/factory-work` and resolved per harness to that harness's bot user, so jobs stay off `/mnt/c`. F2-02 reaches the PC through the box's SSH aliases `pc-wsl-claude-bot`, `pc-wsl-codex-bot` and `pc-wsl-cursor-bot`. The PC and the Mac stay `enabled: false` until F2-02 remote start lands. | Factory decided (technical) |
| Nov 12, 10:02 AM | Linear discovery moves to a Linear webhook. Max's words: "Go straight to a Linear webhook through Tailscale Funnel". This supersedes the Nov 11, 2:31 PM "accepts no inbound webhooks" for this one listener only. `orch linear-webhook serve` verifies Linear's HMAC signature and timestamp and only marks issues dirty in `orch.db`; the poller reads dirty issues each cycle and runs the full search as a reconcile every `linear_webhook.reconcile_seconds` (default 900). With no `linear_webhook` in `orch.yml` nothing changes. Tailscale Network owns the Funnel; Max creates the Linear webhook and saves its signing secret on the box (issue #55, section 7). | Max; details Factory decided (technical) |
| Nov 12, 5:29 AM | Factory lessons batch 1, ops: the job heartbeat is orch-owned. A bot starts its harness as `orch job run <bot> <id> [--worktree] -- <command>`, which heartbeats every third of `timeout_seconds` and stops when the command exits, the job is no longer running, or `orch job run` dies (issue #49). With `--worktree`, a job checkout is a git worktree of one shared clone per repo, removed when the command exits unless it has uncommitted changes or unpushed commits; `orch gc` removes stale ones. `orch poll` writes and removes its own pidfile under its single-poller lock, and `deploy/poller-service.sh` is the no-systemd service with opt-in autostart. | Max approved the batch ("Proceed with fixes"); details Factory decided (technical) |
| Nov 12, 10:12 AM | Continue with Phase 2: F2-02 remote start is built next. Max's words: "continue with phase 2". | Max |
| Nov 12 | F2-02 design: each remote device in `machines.yml` names an `ssh_alias` containing `{bot}` (the PC's is `pc-wsl-{bot}`, so `pc-wsl-claude-bot`, `pc-wsl-codex-bot` and `pc-wsl-cursor-bot`), the box's own SSH config holds the host and key, and `orch` always passes `-l <bot user>` and never a key path. `orch wait` claims through the device pool; `orch job run` starts a remote job over that alias in `<workdir_root>/jobs/<job-id>` (`--worktree` stays box-only). A slot is released only once the worker is confirmed gone: by `orch job run` when its command ends (a remote worker still running is sent SIGTERM and checked again), or by the poller for a finished job whose runner died. The poller checks each remote device holding a slot over SSH once per poll; a device that fails every check for 60 minutes marks each job still running on it `stuck` at once (no T6 retry), wakes Grok Bot once through the ticket's stuck flag, and keeps the slot held. The PC and Mac stay `enabled: false` until Max turns them on. | Factory decided (technical) |
| Nov 12, 4:03 PM | Job states (issue #75). A running job is `claimed` (it holds a device slot, no `orch job run` recorded itself, and nothing has heartbeated since the claim), `running` (fresh heartbeat), `stale` (no heartbeat for more than `job_states.stale_minutes`, default 2) or `dead` (its recorded runner is gone and its worker is confirmed gone, by pid on the box or by `check_worker` over SSH), derived in one function, `lib/job_state.py`, used by the poller, the dashboard and `orch status`. `orch job run` heartbeats every 30 seconds by default (or a third of the timeout if shorter). The poller requeues a job claimed for more than `job_states.release_claimed_minutes` (default 5) as the same job row, so no review round or T6 attempt is charged, frees its slot and logs a `job_events` row; a dead job takes the existing failure path (failed, then T6). The dashboard shows the state on each slot, the job's own round (`detail.round`), and counts stale and dead jobs in stuck / overdue and needs attention. | Max approved the scope ("yes lets address these fixes"); details Factory decided (technical) |
| Nov 12 | Job states follow-up (PR #77). A job the poller requeues after an unstarted claim restarts its queue-wait clock at the release (`last_activity_at`; `created_at` is kept for its place in line), so the job timeout and T6 never act on time it spent queued before the claim; a failed Grok delivery requeue keeps its original clock. `orch job run` heartbeats every 15 seconds by default (or a third of the timeout if shorter); an `--interval` override is capped at 30 seconds (half the smallest `job_states.stale_minutes`) and at a third of the timeout. | Factory decided (technical) |
| Nov 12, 6:59 PM | The waiter starts claimed jobs itself (issue #80). Max's words, 6:13 PM: "We shouldn't require any bot-to-bot handoffs. To your point, we should have a standard brief that is augmented by the factory.md additional context in each repo." And 6:59 PM: "Yes, let the waiter start the harness itself and update the spec rule." `orch wait <bot> --autostart`, running as the owning bot's own user and login, starts each claimed job under `orch job run` from the standard brief for its kind plus the repo's `factory.md`, then waits again. Waiters run under a restart supervisor. The dispatcher and `orch poll` still never exec a harness. Section 4.3 and section 10 updated. | Max |
| Nov 12, 9:23 PM | Job telemetry (issue #93). Max's words: "Yes, file it and build after #82". `orch job run` runs Claude Code with `--output-format stream-json --verbose` and Codex with `codex exec --json`, keeps each job's raw event stream in a capped local log (`events.jsonl`, about 20 MB, rotated once) on the machine that runs `orch job run`, and writes a scrubbed summary to `job_telemetry` in `orch.db` with each heartbeat (~15 s) and at exit. The job modal shows a live feed of recent steps, counters and the exit reason; the Efficiency tab rolls up tokens and cost per job, harness and repo. Only summaries and scrubbed step titles reach the dashboard. Section 4.3, section 10, section 11 and F2-05. | Max (go); details Factory decided (technical) |

Low-risk changes (docs, cleanup, monitoring) get one Codex review round, with leftover minors batched into a follow-up ticket. High-risk changes (ship, merge, sign-off, credential and isolation code) keep up to three rounds. Every extra round needs Max's own words. (Max, earlier today.)

Max will pick the three first live-test tickets when the build is ready. That choice is still open.

## 1. Goals

Max, in his words: "I want to use the P-Stack skills to integrate into my Grok bot to be able to use it as an orchestrator to work on projects. I want to be able to develop across Cursor, Claude, and Codex using the Grok bot as my central orchestrator and handoff."

Done means Max can hand Grok Bot a ticket on any configured project, answer only product questions, and get back a merged or handed-off PR that one harness built and a different-family harness reviewed, with pstack driving how each session works.

The factory is four parts and nothing else:

1. **pstack in every harness.** One pinned pstack source, installed where Claude Code, Codex and Cursor load skills. pstack (poteto-mode plus the principle skills) decides *how* work gets done. The orchestrator adds no work rules of its own.
2. **One handoff format.** Each piece of work is one ticket plus its PR. The brief, questions, answers, plan, verdicts and "done" are comments, with one small machine-readable header. There is no other state store. The dispatcher's local job list can be rebuilt from the tracker at any time.
3. **One Grok Bot skill, `orchestrate-project`.** Grok Bot plans with Max, asks him product questions, decides technical ones, picks the builder and reviewer, and handles everything the dispatcher cannot decide. It reads each project's `factory.md`.
4. **One dispatcher script, `orch`.** A few hundred lines. It polls the configured trackers, applies the transition table, and wakes an agent only when judgment is needed. Bots wait on a blocking `orch wait <role>` shell call, which costs no tokens. It creates no credentials. Its only inbound endpoint is the optional Linear webhook listener (section 7, Nov 12 decision), which only marks Linear issues dirty for the next poll. Outbound Grok delivery uses the existing routine webhook. GitHub uses the existing `gh` login, and Aut0 uses existing Linear access.

The factory repo's `orch.yml` lists which projects to poll and the dispatcher settings. Per-repo policy lives in that repo's `factory.md`.

## 2. Non-goals

These stay out until a real run shows they are needed. They are not Phase 1 or Phase 2 work:

- A custom bash driver, exit-code protocol, or run directories. Comments (and labels on GitHub-issue repos, or Linear on Aut0) replace them.
- Two reviewers plus a separate Judge inside the factory loop. One cross-family reviewer gives the verdict. If Aut0 Judge still runs, it is part of Oliver's flow after handoff, not of the factory loop.
- A separate pre-merge QA stage. The reviewer runs `verify-<repo>` when the project has one.
- Cleffy's deployed QA and its 1-in-10 post-merge audit (dispatcher T9). Deferred past Phase 1.
- Byte-identical rebase proofs. Replaced by `git patch-id --stable` plus re-review when the patch differs.
- A live reader patch, lane coexistence, or old/new cutover. There is no old lane in this repo.
- A mission-control dashboard (anything that controls the factory). Phase 2 `orch status` is a read-only text summary. The requested `orch dashboard snapshot` is a read-only, redacted static view of the same data that is pushed out from the box; it controls nothing (README "Dashboard").
- Per-run `decisions.tsv` and receipts. PR comments are the trail.
- Self-improvement routines (weekly correct, reflect, blinded evals, daily maintain jobs). The weekly factory eval report in Phase 1 is a measurement of runs against the chart, not that list.
- Inbound webhooks other than the Linear webhook listener (section 7), any
  new credential or login, or creating another Grok routine webhook. Outbound
  delivery to the existing Grok routine is part of the dispatcher. The Linear
  signing secret is created by Max in Linear, not by the factory.
- A genericity purge or protected-field base reads. The repo starts generic. `factory.md` is read from the project's base branch.
- Arena and swarm by default. They stay available to the builder as pstack tools.

The 125-item old factory plan, its state-chart formalization of that plan, and its driver are retired. A backup of that repo is at `/workspace/factory-backup-20261110`. This specification does not carry those items forward.

## 3. Roles

| Role | Who | Does | Does not |
|---|---|---|---|
| Consumer | Max | Hands Grok Bot a ticket; answers product questions one at a time; provisions logins and runners; picks the first live-test tickets; OKs extra review rounds in his own words; merges Aut0 PRs (and any other `ship: handoff` repo); gives or withholds an external OK when a repo uses `ship: after:<approver>`. | Does not have to poll CI, drive review rounds under the cap, or answer technical questions the orchestrator can decide. |
| Orchestrator | Grok Bot, skill `orchestrate-project` (this Factory bot) | Intake, clarifying questions, routing, technical decisions, stop reports, posting `orch:go`, relaying product questions. | Does not start `claude`, `codex`, or `cursor-agent`. Does not create logins or credentials. Does not treat another agent's approval as Max's. Does not create GitHub issues or labels on Aut0. |
| Builder session | Claude Bot, Codex Bot, or Cursor Bot, as routed | Its own waiter (`orch wait <bot> --autostart`, running as that bot) starts its harness under `orch job run` from the standard brief plus `factory.md`; the session builds or plans as poteto-mode, self-checks, posts `ready`. | Does not start a harness it does not own. |
| Reviewer session | A bot whose model family differs from the builder | Reviews, runs the named verify skill (UI and database checks), posts `verdict`. Blocks only on a demonstrated failure. | Does not merge. Does not act as a second Judge. |
| Dispatcher | `orch`, a script | Polls trackers every 60 seconds, applies the transition table, queues jobs, merges when `ship: merge` and the head is the approved SHA, marks terminal states. | Does not make judgment calls. Does not exec a harness binary. Wakes Grok Bot or a harness bot when the next action is not fully determined. |
| Aut0 after handoff | Oliver's existing flow | Whatever Aut0 already does after Ready for Review, which may include Aut0 Judge and human review. | Not part of the factory loop. The factory does not run a second judge. |

Harness ownership is fixed: **Claude Bot starts every Claude session, Codex Bot every Codex session, Cursor Bot every Cursor CLI job.** Grok Bot, the dispatcher and `orch poll` only queue requests. A bot's harness is started by that bot's own waiter, which runs as the bot's own user and login (section 4.3).

Phase 1 runs on one machine: the box. Phase 2 adds Max's other Tailscale devices, with each harness eligible only where Max has provisioned its login under that bot's user.

## 4. Ticket lifecycle and transitions

Max starts a ticket by handing it to Grok Bot (a GitHub pull request in Phase 1, a Linear issue, or a description that becomes a ticket on that project's tracker). GitHub issue-only discovery is deferred; an `orch:state/` label on an issue is not polled. Grok Bot reads `factory.md`, writes the brief, and always runs the questions step. After product questions are answered, or when there are none, Grok Bot posts `orch:go`, a handoff comment with `kind=go`. That is the mechanical start of build or plan. Max does not have to type "go" a second time unless he is restarting a capped or stuck ticket.

### 4.1 States

```mermaid
stateDiagram-v2
  [*] --> Intake: Max gives Grok Bot a ticket
  Intake --> Refused: project config invalid
  Intake --> Questions: brief + AC posted on the issue
  Questions --> WaitingOnMax: a real product call (D31, D33-F)
  WaitingOnMax --> Questions: Max answers
  Questions --> Planning: no open product calls, ticket over 3 files (D36)
  Questions --> Building: no open product calls, 3 files or fewer
  Planning --> PlanCheck: trusted builder posts kind=plan
  Planning --> Stuck: job dies twice (T6), or its device is offline 60 minutes
  PlanCheck --> Planning: an AC is unmapped (once)
  PlanCheck --> Building: every AC mapped
  PlanCheck --> Stuck: gaps a 2nd time
  Building --> Review: builder posts ready, CI green on that head
  Building --> Building: CI red, fix (gate retries don't count as rounds)
  Building --> Stuck: CI red 3 times in a row, the job dies twice, or its device is offline 60 minutes
  Review --> Fixing: REVISE, under the round cap
  Fixing --> Review: fixed head ready, CI green
  Fixing --> Stuck: job dies twice (T6), or its device is offline 60 minutes
  Review --> Capped: REVISE at the cap, or a 2nd AC-unmet verdict
  Review --> Blocked: BLOCKED
  Review --> Ship: APPROVE on the current head
  Review --> Stuck: job dies twice (T6), or its device is offline 60 minutes
  Ship --> Review: head changed and the patch differs (D33-D)
  Ship --> Merged: ship = merge
  Ship --> HandedOff: ship = handoff (Max merges, or the Aut0 RfR handoff)
  Ship --> WaitingExternal: ship = after an approver (Tracy Site)
  WaitingExternal --> Merged: OK names the previewed SHA
  WaitingExternal --> Fixing: declined (one fix pass, 3rd decline = Stuck)
  WaitingExternal --> Stuck: 3rd decline
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

Refused, Stuck, Blocked, Capped and Abandoned are terminal unless Max acts. Each one wakes Grok Bot once, and Grok Bot tells Max once. On Aut0, a blocking follow-up is a Linear ticket, never a GitHub issue.

Planning is required when the ticket is expected to touch more than 3 files (D36). Three files or fewer go straight to Building after questions. A scope-changing answer mid-work sends the ticket back to Planning; that is a skill judgment, not a dispatcher `if`.

`PostMerge` (T9) is defined in the chart and the transition table, but Cleffy's deployed QA and post-merge audit are deferred past Phase 1. No Phase 1 project is configured with `post_merge` until that work is scheduled.

### 4.2 Dispatcher transition table

`orch` polls each configured repo every 60 s (`env -u GH_TOKEN gh`, plus Linear for Aut0). It reads only tickets and PRs it already tracks, and comments that carry an `orch:` header. Every row is a plain `if`. It is idempotent: each handled comment ID is recorded, so a restart never acts twice.

| # | Event (mechanical) | Known answer | Wakes |
|---|---|---|---|
| T1 | GitHub PR or Linear ticket gets a `kind=go` handoff comment (`orch:go`, posted by Grok Bot after Questions) | Queue a `build` job (or `plan` if over 3 files) for the builder from `factory.md` | Builder's owning bot |
| T1P | While Planning, the trusted builder for the current plan job posts `kind=plan ac=met\|unmet round=<n>/2` | `ac=met`: queue `build` for the same builder, with the plan link or comment ID in `detail`. First `ac=unmet`: return to Planning and queue one more `plan` with `round=2/2` in its detail. Second: mark `stuck`. Stale rounds and duplicate comments are no-ops. | Builder's owning bot, or Grok Bot on the second gap |
| T2 | `ready head=<sha>` comment, then CI finishes on that SHA | Green: queue `review` for the reviewer. Red: queue `fix` with the failing log (gate count +1; 3rd in a row → `stuck`) | Reviewer's or builder's bot |
| T3 | `verdict=REVISE` | Round under the cap: queue `fix` with the verdict link. At the cap: mark `capped` | Builder's bot, or Grok Bot at the cap |
| T4 | `verdict=APPROVE head=<sha>` | Head equals sha, or patch-id equal → ship per `factory.md` (`merge` = `gh pr merge --squash --match-head-commit`, `handoff`, `after:<bot>`). Else queue a re-`review` | Nobody on merge. The approver bot for `after:` |
| T5 | `questions` comment from any session, then `answers` | Set state `orch:state/waiting-max`. On answers, re-queue the same step with the answer linked (not a round) | Grok Bot (it decides technical ones itself) |
| T6 | Job exits with no handoff comment, or 60 minutes pass with no heartbeat or progress (or the project's `job_timeout` in `orch.yml`), or the job is `dead` (its `orch job run` and worker are confirmed gone, issue #75). Phase 2 (F2-02): the device the job runs on has failed every SSH check for 60 minutes | Retry once. 2nd time → `stuck`. Device offline 60 minutes → `stuck` at once, no retry; the slot stays held | Grok Bot on the 2nd, or once for the offline device |
| T7 | `verdict=BLOCKED`, `stuck`, `capped`, config refused, anything unparsed | No known answer | Grok Bot, once |
| T8 | `ok head=<sha>` / `decline` from the external approver | OK on the previewed SHA → ship. Decline → `fix` (3rd → `stuck`) | Builder's bot on decline |
| T9 | PR merged and the project has `post_merge` | Queue a `post-merge` job. A fail files a blocking follow-up on the project's ticket tracker. Deferred from Phase 1. | QA owner bot |

T1, T1P and T2–T6 are the core loop. T7–T9 are added only when a project needs them. T9 is not built in Phase 1. Any new row needs a real run that showed a wasted wake. The default job timeout is 60 minutes of no heartbeat or progress; a project may override it with `job_timeout` in `orch.yml` (Factory decided (technical), 10:19 AM ET; 60 minutes since Nov 11).

Grok jobs are pushed by the dispatcher through `GrokDelivery` during reconciliation, never through `orch wait`.

Harness jobs start without a bot-to-bot message (issue #80). Each harness bot's waiter runs `orch wait <bot> --autostart` under a restart supervisor (`deploy/waiter-service.sh`, or the `deploy/orch-waiter@.service` user unit), so a killed waiter comes back by itself. On a claim (box slots only; a remote device needs a bot running plain `orch wait`), the waiter renders the standard brief for the job's kind (implement for `build` and `plan`, fix, review, and merge-only for a `fix` whose detail says `"kind": "merge-only"`) from the job row: repo, PR or ticket, head, round and cap, risk, the review rules of section 8 and the `orch:v1` header the job must end with. It appends the prose of the repo's `factory.md`, read from the repo's `factory_ref` (never a PR head) as an additional-context section; project-specific behavior stays there. It then runs `orch job run <bot> <job-id> --worktree -- <the bot's configured command>` with the brief on stdin, in its own session, and goes back to waiting. `orch job run` heartbeats before the harness starts, so the job reads `running` within seconds of the claim. The harness posts its handoff and runs `orch job done`. A job with no standard brief, or whose start fails, is failed at once for T6, and so is a job whose harness exits without `orch job done`. Each job gets exactly one handoff (Max, via Factory on PR #82): the claim records the autostart waiter on the job's slot, only that waiter's own `orch job run` may run the job and only that run may `orch job done` it, so a bot turn never runs or posts for it; a plain `orch wait <bot>` is refused while the bot's autostart waiter runs; a second `orch job run` of a running job is refused; `orch job done` with the outcome already recorded is a no-op and a different outcome is refused without a state change; and a verdict that repeats an already handled verdict (same author, head, round and verdict) is recorded as `duplicate-verdict` and starts no job, ship or wake. One waiter runs per bot: it holds a per-bot lock for its whole life.

Each builder/reviewer job's `detail` is compact JSON containing the Linear
URL, `gitBranchName`, base branch, linked PR number when known, brief text or
link, risk, `round=n/cap`, reviewer harness, verify skill, repo skill paths,
and PR issue-comment destination. Rejected author/marker comments and
verdicts stale for the current state or round wake the configured project bot
once. Terminal success is delivered once to that bot.

`orch poll` is the production entry point. It takes `--db` or `$ORCH_DB`;
`--once` runs exactly one cycle. The live `PollSource` reads GitHub through
`env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN -u
GITHUB_ENTERPRISE_TOKEN gh api` and reads configured Linear projects through
the Linear GraphQL API. Grok jobs are delivered to the owning bot through its
existing routine webhook, with the queue's stable logical key as the delivery
deduplication key.

### 4.3 Invariants, and where they live now

| Rule | Where it lives |
|---|---|
| Questions before building; a mid-work question resumes where it paused | Grok Bot skill + T5 |
| Every acceptance criterion is mapped before building; the mapping is for the current plan | PlanCheck; a scope-changing answer returns to Planning |
| Gate loops, declines, two AC-unmet verdicts, and the round cap are bounded | Counters on the ticket, enforced by the dispatcher |
| Ship is pinned to the covered head; an inherited verdict is only valid on an exact patch-id rebase | T4: `--match-head-commit`; otherwise re-review |
| Only the owning harness bot starts that harness | The dispatcher and `orch poll` exec no harness, and plain `orch wait` starts nothing. The waiter, running as the owning bot's own user and login, may start that bot's harness under `orch job run` from the standard brief plus `factory.md` (`orch wait <bot> --autostart`, issue #80). The harness command comes only from that bot's own config file (`~/.config/factory/waiter-<bot>.yml`, owned by the bot and writable by no one else); the brief goes on stdin and into a file, and no job, PR or comment text reaches a command line. `orch job run` only runs the command it is passed after `--`, for a running job that bot owns, and never names a harness itself. The one change it makes to that command is the fixed telemetry flags (issue #93): `--output-format stream-json --verbose` for a `claude` run with `-p`/`--print` and no `--output-format`, and `--json` after `exec` for a `codex exec` run without it; `ORCH_TELEMETRY=off` turns that off |
| Reviewer stays cross-family; last 20% of a usage cycle is reserved; only urgent work may use the reserve | `factory.md` plus each owning bot's usage check |
| Contradictory config never runs | `orch check-config` |
| Waiting, abandoned, external and deploy-wait states mean one thing | Labels on GitHub-issue repos; Linear + `orch:` comments on Aut0, with no GitHub labels |

## 5. Handoff comment format

Each handoff is one comment. The first line is a header the dispatcher parses. The rest is plain prose for people and agents.

```
<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round=2/3 head=3f9c2ab ac=met verdict=REVISE risk=high -->
**Review round 2 of 3: REVISE** (Codex Bot, gpt-6-astra high)
1. [blocking] tools/x.py:40, empty result treated as resolved. Repro: `…`
2. [minor] … (batched into follow-up if low risk)
Full report: <link>
```

Comment kinds: `brief`, `questions`, `answers`, `go`, `plan`, `ready`, `verdict`, `ok`, `decline`, `done`, `stuck`.

On GitHub-issue repos, labels hold current state, round and counters: `orch:state/<name>`, `orch:round/<n>`, `orch:risk/<low|high>`, `orch:builder/<harness>`.

On Aut0, those same facts stay in the `orch:` header and on the Linear ticket. The orchestrator does not create GitHub issues or labels in `aut0-code/aut0`.

Linear comments may use the visible first line
`` `orch:v1 key=value …` `` when Linear strips or visibly renders HTML
comments. `lib.orchestrate` and `format_comment(..., visible=True)` write that
form without hand-written headers, and it has the same validation as
`<!-- orch:v1 … -->`. For
Linear-backed tickets, brief/questions/answers/go stay on Linear;
ready/verdict/stuck are PR issue comments, not reviews or inline comments.
The poll source joins both streams using the linked PR.

Max can read everything on the ticket and the PR. Nothing else is needed to resume.

The `orch:v1` header keys in the example are `kind`, `from`, `role`, `round`, `head`, `ac`, `verdict` and `risk`.
A `plan` uses `ac=met|unmet` and `round=<n>/2`: `n=1` for the first plan and `n=2` after one gap. The round is checked against the persisted plan-gap count. Extra keys are added only when a real run needs them (Factory decided (technical), 10:19 AM ET).

## 6. `factory.md` schema

Every project repo keeps a root `factory.md` that the orchestrator reads from the base branch. It is about one screen: a short YAML front block plus a few short prose sections. Human developers can ignore it.

### Required YAML fields

| Field | Type | Meaning |
|---|---|---|
| `builder.harness` | `claude` \| `codex` \| `cursor` | Preferred builder harness. The orchestrator may pick a different builder by fit. |
| `reviewer.harness` | `claude` \| `codex` \| `cursor` | Preferred reviewer harness. The reviewer must be a different model family than the builder actually used. |
| `review.rounds.low` | integer | Round cap for low-risk work. Default 1 if the field is present as decided; examples use 1. |
| `review.rounds.high` | integer | Round cap for high-risk work. Examples use 3. |
| `review.verify` | skill name or `none` | The verify skill the reviewer runs for UI and database checks. |
| `ship` | `merge` \| `handoff` \| `after:<approver>` | What happens on APPROVE of the covered head. |
| `tickets` | `github` \| `linear` | Ticket source. `linear` means the orchestrator creates no GitHub issues or labels in that repo. |
| `base_branch` | branch name | Integration branch (`main`, `dev`, …). |
| `skills` | list of names | Special repo skills to load, besides pstack. |

Optional:

- `builder.model` and `reviewer.model` as hints. There is no fixed allow-list. `orch check-config` warns on an unknown model name, and the owning bot can refuse (Factory decided (technical), 10:19 AM ET).
- `ci_runner` — self-hosted runner label for this repo, when Phase 2 self-hosted CI is in use. Runner labels are repo policy, so they live here, not in `orch.yml` (Factory decided (technical), 10:19 AM ET).

Free-text sections after the front block:

- **Review** — who gives the verdict, what the reviewer runs.
- **Ship** — what happens on APPROVE, including any external OK or Aut0 Ready-for-Review handoff.
- **Context** — repo-specific facts that do not belong in the YAML (branch naming, CI caveats, domain skills, human reviewers).

`orch check-config` refuses a file that is missing a required field, names a same-family reviewer as required, or sets `tickets: linear` while also asking the orchestrator to create GitHub issues or labels. It warns on an unknown optional model name and does not fail the check for that alone.

Generic example (GitHub-issue repo, merge on approval):

```markdown
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
  verify: verify-cleffy
ship: merge
skills:
  - verify-cleffy
---

## Review
One reviewer from a different model family than the builder gives the final verdict and runs `verify-cleffy` (UI and database checks). Low-risk changes get one round; leftover minors are batched into a follow-up issue. High-risk changes get up to three rounds; every extra round needs Max's own words.

## Ship
On APPROVE of the covered head, squash-merge with `--match-head-commit`.

## Context
PRs target `main`. Mention any repo-specific gotchas here, not in the YAML.
```

A Tracy-style file would set `ship: after:tracy-site` and describe the Site OK in **Ship**. The Aut0 draft (`tickets: linear`, `ship: handoff`, `verify: verify-aut0`, Ready for Review, Max merges) is in [docs/AUT0-CLEANUP.md](AUT0-CLEANUP.md). Until `verify-aut0` merges to Aut0 `dev` (#1305), reviewers list that check as not run.

**F1-15 migrates Cleffy and Tracy to this schema.** Today neither repo has a `factory.md`. Each still has an old-plan `## Factory` block in `AGENTS.md` (Cleffy on `dev`, lines 5–50, from PRs #77 and #81; Tracy on `main`, from line 11, from PRs #2 and #3). Those blocks contradict today's decisions (`reviewers: a+b` or `review: none`, a judge, `qa: browser:click-through` or `qa: none`, `fixer: codex-local`). F1-15 is one cleanup PR per repo (Cleffy targets `dev`, Tracy targets `main`): add a slim root `factory.md`, move the still-valid Context notes, replace the `## Factory` block with a pointer, and keep the pstack router. Do not open those PRs until that to-do is started. Still-valid Context to carry over:

- **Cleffy:** risky areas (migrations, RLS, Edge Functions under `supabase/`, and billing/Stripe); never apply migrations or change config on a hosted Supabase project from an agent session; privacy policy forbids tracking pixels; marketing surfaces use public-domain repertoire only and the primary CTA is always "Start free"; PRs target `dev`; never push to `dev` or `main` directly; `dev` → `main` is Max's merge. Name the repo's verify skill if one exists, otherwise `review.verify: none` (the old block had `verify: off`). The existing `ui-check` skill can stay in Context for UI changes. `ship: merge` applies to PRs into `dev`.
- **Tracy:** small static marketing site; a push to `main` deploys production on Vercel and other branches get previews; Tracy approves the preview, then `main` is fast-forwarded to that exact SHA (no force-push, no merge commit); work runs on the box; before ship, run lint, typecheck and build plus the "Never ship" grep in `CLAUDE.md`; copy, pricing, photo and motion rules in `CLAUDE.md` stay. `ship: after:tracy-site`. Drop `review: none`; the new review rules apply.

Fields from the old Aut0 Factory block that this schema drops on purpose: `owner_bot`, `stages`, `reviewers: a+b`, `judge` / `judge_session`, fixer lanes and cloud overflow, `tests_gate`, `qa`, `small_ticket` path lists, and cloud env IDs. Those are not part of this factory. Cloud env IDs may come back later if a real run needs them; that is open.

## 7. `orch.yml` schema

`orch.yml` lives in the factory repo, not in each project. It lists which projects to poll and the dispatcher settings. It does not repeat per-repo policy.

Decided content:

```yaml
poll_seconds: 60
# Default job timeout. A project may set job_timeout (minutes) to override.
# job_timeout: 60
linear:
  api_key_env: LINEAR_API_KEY
grok:
  webhook_url_env: FACTORY_WEBHOOK_URL
  webhook_auth_env: FACTORY_WEBHOOK_AUTH
  webhook_key_env: FACTORY_WEBHOOK_KEY
trusted_logins:
  maxharris1: [grok-bot, claude-bot, codex-bot, cursor-bot]
# Before install, Max adds the confirmed stable Linear user ID:
# trusted_linear_users:
#   stable-user-id: [grok-bot]
projects:
  - repo: owner/name
    factory_ref: main
    # Required when this repo's tickets are in Linear:
    # linear_project_id: REPLACE_WITH_LINEAR_PROJECT_ID
    # grok_bot: project-bot
    # linear_ready_for_review: false
    # ready_starts_review: false
    # optional job_timeout: 60
# Optional Linear webhook listener (issue #55). Absent means none, and the
# poller behaves as it did before the listener existed.
# linear_webhook:
#   bind: 127.0.0.1:8787            # IP:port; --bind overrides
#   path: /linear-webhook           # the one path served; default shown
#   secret_file: ~/.config/factory/linear-webhook.secret  # mode 600
#   reconcile_seconds: 900          # 60 to 3600; default shown
# Optional job states (issue #75), whole minutes, defaults shown.
# job_states:
#   stale_minutes: 2                # no heartbeat this long: stale
#   release_claimed_minutes: 5      # claimed, never started: requeued
```

Phase 1 polls from the box, using the existing `gh` login and the Linear key
named by `linear.api_key_env`. The key name is configuration; the key value is
only read from that environment variable at request time. A missing key fails
with the variable name, never its value. `trusted_logins` maps GitHub authors
to sets of accepted `from=` bot labels. `trusted_linear_users` separately
maps immutable Linear user IDs to those sets. The expected `from=` and
`role=` are mandatory. With one shared GitHub login, the marker is a routing
label, not an identity or signature; no signing secret is added. The shipped
file records the known shared GitHub login and leaves the stable Linear ID for
Max to confirm. Max approved that GitHub map at 3:09 PM ET on November 11,
2026: "Yes, the factory may trust maxharris1 for comments on factory PRs".
That approval does not make the marker an identity. While a configured Linear
project has no trusted Linear-user map, `orch poll` refuses to start at all,
including for GitHub projects.

`factory_ref` is the trusted base branch from which the poller reads
`factory.md`; it never discovers policy through the repository's default
branch. `linear_project_id` is an immutable Linear project ID, not its
renameable display name.

The Grok webhook fields also contain environment-variable names, not values.
Delivery sends one JSON object containing the job fields and `logical_key`,
with the existing routine's bearer/key headers. The dispatcher never prints
or includes either webhook value in an exception.

Before install, Max must confirm that `LINEAR_API_KEY` names an existing,
Max-provisioned key on the box. This work does not create one. A read-only
Linear introspection and known-comment search must also confirm
`includeComments`, preservation of the `<!-- orch:v1 ... -->` body, and that
both the ID-only search query and direct issue query are accepted without a
complexity error. Brief and go use separate searches; the poller does not
depend on full-text `OR` semantics.

Linear's API-key limits are shared per user: 2,500 requests/hour and
3,000,000 complexity points/hour. Factory owns the named
`LINEAR_SHARE = 0.5`; the other half remains available to Max's other API
keys. For poll interval `P`, one cycle is capped at
`floor(2500 × LINEAR_SHARE × P / 3600)` requests and
`floor(3,000,000 × LINEAR_SHARE × P / 3600)` complexity. At `P = 60`, that
is 20 requests and 25,000 complexity per cycle, or at most 1,200 requests and
1,500,000 complexity per hour.

Brief and go each run a filtered search over configured project IDs and
workflow states other than `completed` and `canceled`. A page contains 50
hits, costs about 116 complexity, and each term is limited to two pages. The
normal one-page-per-term case uses two requests, leaves two requests for a
same-cycle handoff, and permits up to 16 direct issue reads: at most 1,200
requests and about 726,720 complexity per hour if all process headroom is
used. Every additional search page is counted against both budgets and
reduces direct reads. A page-limit or budget cutoff is recorded as a
non-error deferral, at most one aggregated row per cycle.
Tracked reads and discovery fair-share the remaining budget, and tracked
reads rotate across cycles so a deferred ticket is served later. Search hits
already tracked are not fetched directly again, and a direct issue read is
reused throughout its cycle. HTTP 429 or GraphQL `RATELIMITED` pauses Linear
requests for `Retry-After`; T4 and T8 pre-write deferrals retry next cycle.

**Linear webhook (optional, issue #55).** `linear_webhook` turns on
`orch linear-webhook serve`, a small stdlib HTTP server on `bind` (an IP
literal and port). It accepts POST on `path` only, refuses a Content-Length
over 1 MiB before reading the body, verifies `Linear-Signature` (hex
HMAC-SHA256 of the raw body under the signing secret) with a constant-time
compare, and refuses a `webhookTimestamp` more than 60 seconds from now. When
it can see the client address (the TCP peer, or the last `X-Forwarded-For`
hop when the peer is a local proxy) it must be one of Linear's published
source IPs. A signed, fresh Issue event for a configured
`linear_project_id` (or one just moved out of it), or a Comment on a tracked
issue, or a Comment carrying an `orch:v1` marker on an issue a signed Issue
event last placed in a configured project (Comment payloads do not carry the
issue's project, and a marker is text anyone can type), upserts one row in
`linear_dirty(issue_id, identifier, event, received_at)` in `orch.db`; every
other signed event gets 200 and is ignored (a handoff on an issue the
listener has not seen is found by the reconcile search). At most 8 requests
are worked on at once (a further connection is closed unread) and each must
arrive in full within 10 seconds in total. It never runs handoff logic and never calls Linear. Log lines carry
the outcome, event type and issue identifier only. `secret_file` names the
signing-secret file Max saves (mode 600; a group- or world-readable file is
refused); only the listener reads it, once at startup, and the value is never
logged, printed or put in an error. `check-config` validates the keys and
does not read the file. `linear_webhook` needs at least one Linear project.

With the webhook configured, each poll cycle reads the dirty rows and the
tickets with queued or running jobs. Dirty untracked issues are read
directly (oldest first, under the same budget) instead of searching; an
issue outside the configured projects, completed, canceled or deleted is
dropped quietly. A tracked ticket is reread in full only when it is dirty,
has an active job, or its last read is `reconcile_seconds` old; otherwise its
last read is reused and the ticket is still rebuilt each cycle, so its
GitHub PR head, CI and PR comments stay current. The full brief/go search
runs at startup and then every `reconcile_seconds` (default 900; it stays due
until it completes without a budget or error cutoff), so a missed webhook
costs at most one reconcile interval. A dirty row is deleted only after its
issue was read in that cycle and only if no newer event replaced it; a
tracked ticket the dispatcher no longer polls (merged, handed off, done or
abandoned) is not read with or without a webhook, so its row is deleted
unread. A read that finds the issue gone drops the reused read too. If the
dirty queue cannot be read, the cycle records the error and runs as a full
cycle. With no `linear_webhook` the poller is unchanged, and the dispatcher
creates no webhook tables (the listener creates them; a webhook-mode poller
records `linear_webhook_polling`).
`orch status` and the dashboard show the last delivery, the dirty-queue
depth and a warning after more than an hour with no delivery while Linear
tickets are active, once a listener or a webhook-mode poller has run on the
database (a listener that never started shows as `never`).

The default job timeout is 60 minutes of no heartbeat or progress. A project may override it with `job_timeout` in this file (Factory decided (technical), 10:19 AM ET; 60 minutes since Nov 11). Phase 2 device inventory (Tailscale names, slot maximums, harness eligibility) lives in `machines.yml`, not here (section 13). Runner labels do not: they go in each repo's `factory.md` as `ci_runner:` (Factory decided (technical), 10:19 AM ET). Log path and Linear access details may be added as placeholders in the F1-01 skeleton; do not invent new required fields.

`grok_bot` routes stops and successful handoffs to the owning project bot. If
the webhook's `target_bot` is set, only that project bot handles the wake. If
it is `null`, Grok Bot handles the wake for the repo's project.
`linear_ready_for_review` defaults to `false` and controls only the Linear
Ready for Review move after a covered cross-family APPROVE. Aut0 enables it;
no other Linear state transition is written. The move is made only from a
state before Ready for Review (Backlog, Triage, unstarted, or started before
it), never from Ready for Review itself or a state a human moved the issue on
to (completed, canceled, or started after it). On a team with no Ready for
Review state the handoff still completes and the `done` wake reports that the
issue was not moved.
`ready_starts_review` defaults to `false`. When `true`, a trusted `ready` from
`factory.md`'s builder (`from=<builder>-bot role=builder`, by a
`trusted_logins` author) whose head names the PR head starts tracking an
open PR with no Grok `brief` or `go`, and starts the first review even with
no build job, for a project whose PRs are built outside the queue (the
factory repo itself). Such a PR needs no `orch:state/` label to be
discovered: an open PR with a parseable `kind=ready` comment is a candidate,
the trust and head rules above decide, and the poller writes the label once
it tracks the PR (issue #59). Only that trusted `ready` admits an unlabelled
PR: a Grok `brief` or `go` on it starts nothing until the PR is labelled. With the flag off, only a trusted Grok `brief`
or `go` on a labelled PR starts tracking.

## 8. Review and merge rules

- **One reviewer**, from a **different model family** than the builder, gives the **final factory verdict**.
- That reviewer runs the project's **verify skill** (UI and database checks) when `factory.md` names one, plus interrogate and blast-radius. They may run code in a throwaway copy. They block only on a demonstrated failure.
- There is no second factory reviewer and no factory Judge. Aut0 Judge, if it still runs, is Oliver's flow after handoff.
- **Risk class**, assigned at intake:
  - `low` — docs, cleanup, monitoring. One review round. Leftover minors are batched into a follow-up ticket (a GitHub issue, or a Linear ticket on Aut0).
  - `high` — ship, merge, sign-off, credential, or isolation code. Up to three rounds. Every extra round needs Max's own words.
  - If the class is unclear, ask Max. Do not invent a third class.
- Gate retries (CI red, then fix) do **not** count as review rounds. Three CI-red cycles in a row mark the ticket `stuck`.
- A second AC-unmet review verdict marks the ticket `capped`, even if rounds remain.
- **Ship is pinned to the covered head.** `ship: merge` uses `gh pr merge --squash --match-head-commit <sha>`. If the PR head has moved, the approval carries only when `git patch-id --stable` is equal; otherwise the dispatcher queues a re-review.
- `ship: handoff` (Aut0): on APPROVE, mark the linked PR ready. The
  per-project `linear_ready_for_review` flag alone controls the Linear Ready
  for Review move; it defaults off and Aut0 enables it. Never merge, request
  `maxharris1` as a reviewer, or make any other Linear state change.
- `ship: after:<approver>`: wait for `ok head=<sha>` on the previewed SHA, or `decline` (fix; third decline is `stuck`).
- Another agent's approval is never treated as Max's.

The builder brief says "run as poteto-mode". The builder follows the playbook that fits the ticket (feature; bug-fix, repro first; refactor). Tickets over 3 files start with an architect scaffold and `plan.md`. Before `ready`, the builder self-checks: prove-it-works, deslop, no-comments.

**Usage (D35/D44).** Before starting a job, each owning bot checks its harness's usage. At the last 20% of the cycle, non-urgent jobs go to the next-best fit. Urgent jobs (a resumed paused step, past-ship work, or anything Max marks urgent) may use the reserve. Phase 1 routes on best fit only, plus each owning bot's own reserve check. A verified usage source is added per harness when its bot can report one. That is not a Phase 1 blocker (Factory decided (technical), 10:19 AM ET).

## 9. Failure handling

| Failure | What happens |
|---|---|
| Invalid or contradictory `factory.md` at intake | `Refused`. Grok Bot tells Max once. |
| Product question mid-intake or mid-work | `WaitingOnMax`. On answers, the same step resumes. Not a review round. Max may abandon the ticket. |
| PlanCheck finds an unmapped acceptance criterion | Return to Planning once. A second gap is `Stuck`. |
| CI red on a `ready` head | Queue `fix` with the log. Gate count +1. Third in a row: `Stuck`. |
| Job exits with no handoff comment, or 60 minutes pass with no heartbeat or progress (or the project's `job_timeout`) | Retry once. Second time: `Stuck`. |
| `verdict=REVISE` under the cap | Queue `fix`. |
| `verdict=REVISE` at the cap, or a second AC-unmet verdict | `Capped`. Grok Bot tells Max once. Max's own words can OK one more round. |
| `verdict=BLOCKED`, anything unparsed, or config refused later | Wake Grok Bot once. Grok Bot tells Max once, with evidence and a recommended next step. |
| External `decline` | One fix pass per decline. Third decline: `Stuck`. |
| Device offline 60 minutes (Phase 2 only) | `Stuck` (F2-02). Each poll runs one SSH check on every remote device that holds a slot. When a device has failed every check for 60 minutes, each job still running on it is failed with its failure already handled (no T6 retry), its ticket is marked `stuck`, and Grok Bot is woken once. The slot stays held until the device confirms the worker gone, and the job is never `done`. A device that drops mid-job and is back before then is still caught by T6 if the job stops heartbeating. |
| Post-merge check fails (deferred) | File a blocking follow-up on the project's tracker, then `Done`. |

Grok Bot reports each stop once. It does not poll Max about the same stop.

## 10. Security

- The factory never creates logins or credentials without Max's OK. Max provisions harness logins and registers self-hosted runners himself.
- Never read credentials, runner tokens, or `FACTORY_WEBHOOK_*` values. Never `cat` `.credentials*`, `.runner`, `auth.json`, or `.env`.
- The Linear webhook signing secret lives only in the file `linear_webhook.secret_file` names, which Max creates mode 600. Only `orch linear-webhook serve` reads it, and it never logs, prints, stores or echoes it in an error. The listener's public exposure is a Tailscale Funnel on its one path, owned by Tailscale Network.
- For `maxharris1/*` and `aut0-code/aut0`, run GitHub as `env -u GH_TOKEN gh`.
- The dispatcher uses the existing `gh` login, configured Linear access, and
  the existing Grok routine webhook. `orch.yml`, documentation, unit files,
  errors, and logs contain only environment-variable names, never credential
  values.
- Credential-holding deploy workflows stay on hosted runners (D21). Phase 2 self-hosted CI is for non-credential checks. The plan never touches runner tokens.
- The dispatcher and `orch poll` never exec `claude`, `codex`, or `cursor-agent`, and plain `orch wait` starts nothing. A harness runs only under `orch job run`, for a running job its bot owns, with the command passed after `--`: by the bot itself, or by that bot's waiter (`orch wait <bot> --autostart`), which runs as the bot's own user and login and passes the command from that bot's own harness config, with the standard brief plus `factory.md` on stdin. `orch` never names a harness itself, and no job, PR or comment text reaches a command line (section 4.3). The only change `orch job run` makes to the command is the fixed telemetry output flags (issue #93, section 11).
- Job telemetry (issue #93): the raw event stream of a harness run stays in `events.jsonl` (mode 600, in a mode-700 directory) on the machine that runs `orch job run`, and is never copied into `orch.db`, the dashboard or a comment. Everything that leaves that machine's process (the `job_telemetry` row and so the dashboard) is scrubbed first: the values of the runner's environment variables whose names look secret are replaced, whatever their length (boolean flag values such as `1` or `true`, and the XDG_SESSION_* and DESKTOP_SESSION descriptors, are kept), then the dashboard's `redact` removes token-shaped strings, auth headers, key assignments, URLs and email addresses, and each text is cut to one short line. Assistant text and tool output are never stored. The dashboard redacts every string once more.
- `factory.md` and `orch.yml` are read from the relevant base branch, not from an untrusted PR head, when deciding whether a project may run and how it ships.
- The factory does not create GitHub issues or labels on Aut0.

## 11. Measuring and evals

`statechart` is a core pstack skill (pstack plugin README and `docs/guide/06-verify-and-ship.md`). Max wants it kept so we can measure the factory.

- Keep the orchestrator's loop as a TLA+ model plus a Mermaid statechart in this repo, under `model/`.
- Check it with pstack `/statechart`.
- Re-run that check in CI whenever the dispatcher's transition table changes, so the picture, the model and the code cannot drift apart.
- The evaluation compares real runs against the chart's states, using `orch:` comment headers and the dispatcher log.
- It measures: time in each state, wasted agent wakes, review rounds per PR, stuck jobs, and the rule breaks the model finds.
- A small weekly report is built from that data (to-do F1-13 and F1-14). The report may run against fixtures until live tickets exist.
- This is not the retired self-improvement routine list.

Job telemetry (issue #93, `lib/telemetry.py`, `lib/dashboard_telemetry.py`). `orch job run` hands the harness's stdout to a telemetry session. Every line goes to `events.jsonl` in the job's state directory: the waiter's job directory (`ORCH_JOB_STATE_DIR`), else `telemetry-<bot>/<job-id>` beside `orch.db`. A write that would take the file past 20 MB first renames it to `events.jsonl.1`, so at most two files are kept per job. Lines that are not JSON go on to the waiter's `run.log` as before; JSON events do not. Each event updates the job's summary: turns, tool calls by type, files touched, commands run, the last test run's pass and fail counts (pytest, jest/vitest and cargo forms), tokens in (all input, cached included) and of them cached, tokens out, cost when the CLI reports it (Claude Code does, Codex does not), elapsed time, last activity, the CLI's own exit reason and the last 20 steps (tool calls and commands with their status, and errors). `orch job run` writes the summary to the `job_telemetry` table (one row per job) right after each heartbeat and once more when the harness exits, with the exit status. So `job_telemetry.updated_at` moves with `jobs.last_activity_at` while the harness runs, and `telemetry.last_seen(connection, job_id)` reads it for the stale and health checks (#91, #92). A remote job's stream reaches the box over its ssh session, so its raw log is kept on the box, where its run log already was. A telemetry failure (an unwritable log, a locked database, a malformed event) is one log line and never stops the job or its heartbeat.

The dashboard reads `job_telemetry` read-only. Each job's article (the job page and the lanes page's job modal) ends with its telemetry: the CLI and the time since its last event, counters, tool calls by type, and the most recent steps, newest first, each with its status; a finished job shows its exit reason; a job with no row shows "no telemetry" (and one with a row but no events yet, "no telemetry yet"). A modal left open on a job that has just finished shows its final summary from the rollup. The Efficiency page's "Tokens and cost" panel rolls up the last 7 days: totals, then tokens and cost per harness and per repo, and the 12 most recently finished jobs with their exit reasons.

On Aut0, the `statechart` skill, `create-formal-verification-skill`, and the install/preflight scripts stay vendored so those skills can run there. They are not removed in the cleanup PR.

## 12. Phase 1

One machine (the box). End-to-end loop. The 15 to-do items are listed in [section 17](#17-to-do-checklist). F1-15 (Cleffy and Tracy `factory.md` migrations) can start after F1-01 and must land before live tickets on those repos. Aut0 cleanup and those two PRs can run in parallel with items 2–9, but live acceptance now waits on those merges (Aut0's merge is Max's). Expect about **3 working days**, with a first end-to-end path on day 2 and live acceptance on day 3 or 4. If work starts 2026-11-10, Phase 1 would finish **Friday 11-13 to Saturday 11-14 ET**.

Phase 1 includes Aut0 cleanup (High-priority Linear ticket, then a PR into `dev`), Cleffy and Tracy `factory.md` migrations, and the model, chart and weekly eval report. It does not include T9, Tailscale device scheduling, or self-hosted CI.

Live acceptance is three real tickets, one each on three projects Max picks, run end to end. Fix only what those runs expose. Max will pick the tickets when the build is ready.

## 13. Phase 2 (later add-ons)

Start only after Phase 1 works.

- **Device pool.** `machines.yml` with the box, PC and Mac (Tailscale names), a declared safe maximum of slots per device, and each harness eligible only where Max has provisioned its login under that bot's user (`claude-bot`, `codex-bot`, `cursor-bot`).
  Each device has `name`, `tailscale_name`, `slot_max`, `harnesses` (each eligible harness mapped to its own bot user), `workdir_root` and `enabled` (default `true`). The one `local: true` device is the box that runs `orch`; it omits `tailscale_name` and `workdir_root`. Every remote device names an absolute `workdir_root` that is not under `/mnt/`, so a WSL node never works on a Windows drive; a whole path component may be `{bot}`, resolved to the harness's bot user (the PC uses `/home/{bot}/factory-work`). Every remote device also names an `ssh_alias`, the Host alias in the box's own SSH config, containing `{bot}` so each bot logs in through its own alias (the PC's `pc-wsl-{bot}`); it is never a key path, `user@` or option. The PC and Mac stay `enabled: false` until Max turns them on. A device schedules only when it is enabled, has no `REPLACE_WITH_` value, and lists the harness. `orch check-config` refuses a placeholder unless `--allow-placeholders` is passed, then names it, and always names a disabled device. Scheduling holds one slot per job on the first eligible device in file order with a free slot, in the same transaction that claims the job, so two claims cannot take the same last slot; any free slot takes any harness. A queued job never holds a slot: requeueing a claimed job frees its slot in the same transaction. A job's slot is not freed when it finishes, fails or times out, only by an explicit release once the worker is gone. No two devices may share a `tailscale_name`. Nothing assumes a device is reachable. `orch status` lists each device's harnesses, slots held over `slot_max`, its running jobs, and whether it is disabled or a placeholder; a pool-placed job is not also counted under `Slots — box`.
- **Remote start.** Each owning bot starts its harness on a device over Tailscale SSH as its own user. A slot stays held until the worker is confirmed gone. 60 minutes offline means `stuck`.
  `orch wait` claims through the device pool and prints the job's `device` and `remote` (and, on a remote device, its `workdir`). `orch job run` refuses a job that holds no slot, whose slot is another bot's, that another live `orch job run` already runs, or whose earlier worker (left by a runner that died) is not confirmed gone. On the box the command's pid is recorded on the slot before the command may run. On a remote device it runs the command as `ssh -o BatchMode=yes ... -l <bot user> -- <ssh_alias> <script>` under a fresh worker tag recorded on the slot first; the script makes `<workdir_root>/jobs/<job-id>`, records the worker's process group and boot id there, and runs the command with `ORCH_JOB_ID`, `ORCH_JOB_BOT`, `ORCH_JOB_DEVICE`, `ORCH_JOB_WORKDIR` and `ORCH_WORKER_TAG`, exiting with its status. `--worktree` is box-only. When the command ends (done, failed or killed) the slot is released once the worker is confirmed gone: on the box, the command has exited; on a remote device, an SSH check finds no process carrying the tag and no process left in the recorded group, after a SIGTERM to the tagged processes if any were left running. An unreachable device keeps the slot; each poll then releases slots whose job is no longer running and whose worker is confirmed gone (stopping an orphaned remote worker first), and runs the 60-minute offline check (section 9).
- **Self-hosted CI.** Runner labels live in each repo's `factory.md` as `ci_runner:`. The device inventory lives in `orch.yml` (or `machines.yml`). Plus a registration checklist for Max. Max registers the runners himself. Credential-holding deploy workflows stay hosted.
- **`orch status`.** A read-only text summary of queue, slots and paused items, generated from the tracker and the local job list.

About two more days, plus Max's runner and login setup. Exact device names, slot counts and which logins are provisioned where are open until Max provides them (Q8).

## 14. Aut0 cleanup (summary)

Do not file the Linear ticket or open the Aut0 PR until Max says go. Priority is **High** (Max, 10:18 AM ET).

On Aut0 `dev`, keep 668 factory-added pstack files (53 vendored skills, including `statechart` and `create-formal-verification-skill`, plus vendor scripts and install/preflight tooling). Remove 1 file: `docs/aut-2166-factory-follow-up.md`. Edit `AGENTS.md` (replace the `## Factory` block with a pointer to slim `factory.md`; keep the pstack router row for `statechart`), retarget Factory-section pointers, and add `factory.md`. Open PRs #1305 and #1318 (verify-aut0) stay open and do not overlap the cleanup paths; #1318 already conflicts with its #1305 base and should be rebased separately.

Draft ticket title: **chore: slim factory.md and remove old factory plan config**. Full lists, the drafted `factory.md`, and acceptance criteria are in [docs/AUT0-CLEANUP.md](AUT0-CLEANUP.md).

## 15. Repo layout

This is [maxharris1/factory](https://github.com/maxharris1/factory) (private).

```
factory/
  README.md
  bin/orch               # CLI: poll, wait, job, check-config, install-skills, doctor, status
  lib/                   # handoff.py, transitions.py, gh.py, usage.py
  deploy/                # reviewed systemd user unit; never auto-installed
  skills/                # pinned pstack + skills.lock
  grok/orchestrate-project/SKILL.md
  bots/                  # short "orch jobs" sections for Claude/Codex/Cursor Bot skills
  config/orch.example.yml
  config/machines.example.yml
  machines.yml           # Phase 2 device pool (F2-01) and SSH aliases (F2-02)
  model/                 # TLA+ model + Mermaid statechart of the orchestrator loop
  docs/SPEC.md           # this file
  docs/PLAN.md           # shorter plan
  docs/AUT0-CLEANUP.md
  docs/factory.example.md
  tests/                 # fake GitHub + fake bots, one test per transition row
```

The current pstack pin named in the plan is 0.15.36. Aut0's existing vendor pin is pstack 0.15.34 at `cursor/plugins` commit `519d36aa98955bf680db01bcde5fdfba686dc901`. A bump of Aut0's pin is not part of the cleanup PR.

## 16. Open questions

Do not invent answers. Ask Max when the work needs one. The only items still open are:

1. **Which three tickets** run as live acceptance (F1-10)? Max said he will pick them when the build is ready.
2. **When to schedule T9** (Cleffy's deployed QA and post-merge audit) after Phase 1.
3. **Phase 2 inventory:** the Mac `workdir_root` path, and which harness logins exist on the Mac. Max gave the Tailscale names, slot maximums and PC logins on November 11; Factory set the PC `workdir_root` the same day (Decisions table).

## 17. To-do checklist

Each item is one PR, except Aut0 work, which follows Aut0's own Linear ticket plus a PR into `dev`, and F1-15, which is one PR on Cleffy and one on Tracy. Sizes: **S** (hours), **M** (about a day or less), **L** (spans the live runs). Phase 1 items are small except F1-10.

Do not start F1-11, F1-12, or F1-10's Aut0 ticket until Max says go on the Linear ticket (priority is already High) and has picked live tickets. Do not open the F1-15 PRs until that item is started; this document only plans them.

### Phase 1

#### F1-01 — Repo skeleton

- **Description:** README, `orch.yml` (repos to poll and dispatcher settings), a `factory.md` schema plus `orch check-config`, and example `factory.md` files (Aut0, Cleffy and Tracy as examples only).
- **Acceptance criteria:** `orch check-config` accepts the examples and refuses a missing required field, a same-family reviewer, and `tickets: linear` combined with GitHub issue or label creation. `orch.yml` lists projects and `poll_seconds: 60`. The default job timeout is 60 minutes; a project may set `job_timeout` to override. `check-config` warns on an unknown optional model name. No other invented `orch.yml` fields are required to pass.
- **Depends on:** none.
- **Size:** S.

#### F1-02 — pstack source and pin

- **Description:** `skills/` in this repo, a `skills.lock` with the commit and sha256 of each file, `orch install-skills`, and `orch doctor` with a load canary in Claude Code, Codex and Cursor, each started by that harness's own bot.
- **Acceptance criteria:** Install symlinks or vendors the pinned files to the paths in [docs/PLAN.md](PLAN.md) section 9. The canary asks the harness to name poteto-mode and quote a token from the pinned skill, and fails if any harness did not load that version. A pin bump is one PR that updates the pin and the lock together.
- **Depends on:** F1-01 (can start in parallel once the repo layout exists).
- **Size:** M.

#### F1-03 — Handoff format

- **Description:** Parser and writer for `orch:v1` headers, label conventions, the Aut0 no-label path, and tests on fixture comments.
- **Acceptance criteria:** Round-trip tests for every comment kind in section 5. GitHub-issue repos get the labels listed there. Aut0 fixtures carry the same facts in the header only, with no label writes.
- **Depends on:** F1-01.
- **Size:** S.

#### F1-04 — Dispatcher core

- **Description:** Poll loop, handled-comment ledger, and transitions T1, T2 and T6, with fake bots in tests.
- **Acceptance criteria:** One test per of T1, T2 and T6. A restarted dispatcher does not handle the same comment twice. CI red increments the gate count and marks `stuck` on the third in a row. A job with no handoff, or with 60 minutes of no heartbeat or progress (or the project's `job_timeout`), is retried once and marked `stuck` the second time. `orch` never chooses or starts a harness on its own: only `orch job run`, called by the owning bot, runs the command that bot passes.
- **Depends on:** F1-03.
- **Size:** M.

#### F1-05 — Dispatcher review loop

- **Description:** T3, T4 and T5: round cap by risk class, `--match-head-commit` merge, patch-id carry-over, and question resume.
- **Acceptance criteria:** One test per of T3, T4 and T5. Low-risk cap is 1, high-risk cap is 3, unless `factory.md` says otherwise. A moved head with an equal `git patch-id --stable` ships; a different patch-id is re-reviewed. `ship: handoff` and `ship: after:<approver>` do not merge. Answers resume the same step and do not increment the round.
- **Depends on:** F1-04.
- **Size:** M.

#### F1-06 — Blocking wait

- **Description:** `orch wait <role>`, and an `orch job done` helper. Add a one-paragraph "orch jobs" section to the Claude Bot, Codex Bot and Cursor Bot handoff skills.
- **Acceptance criteria:** A waiting bot is blocked on the shell call and spends no model tokens until the shell exits. The owning bot is the process that then starts the harness: since issue #80 its waiter (`orch wait <bot> --autostart`) starts it under `orch job run` from the standard brief plus `factory.md`. The three handoff skills document how to wait and how to mark a job done.
- **Depends on:** F1-04.
- **Size:** S.

#### F1-07 — `orchestrate-project` skill

- **Description:** Grok Bot skill for intake, questions, routing and stop handling.
- **Acceptance criteria:** The skill reads `factory.md`, posts a brief (goal, acceptance criteria, likely files, risk class, done-evidence), always runs the questions step, classifies questions as observable, default-able, or product, asks Max product questions one at a time, records technical defaults, posts `orch:go` only after product questions are cleared, picks a cross-family reviewer, and reports each stop once with evidence and a recommended next step.
- **Depends on:** F1-01, F1-03.
- **Size:** M.

#### F1-08 — Usage reserve and routing

- **Description:** Fit table, and each owning bot's own reserve check. Phase 1 routes on best fit only. A verified usage source is added per harness when its bot can report one; that is not a Phase 1 blocker.
- **Acceptance criteria:** Phase 1 routing is best fit and stays cross-family for the reviewer. Each owning bot still applies its own reserve check when it can. Missing a verified usage source does not block F1-08 or F1-10.
- **Depends on:** F1-07.
- **Size:** S.

#### F1-09 — Project hooks T7 and T8

- **Description:** T7 (unparsed or terminal states wake Grok Bot once) and T8 (external OK / decline, for Tracy-style `after:<approver>`). Do not build T9.
- **Acceptance criteria:** One test each for T7 and T8. An `ok` is accepted only for the previewed SHA. A third decline is `stuck`. No `post_merge` job is queued in Phase 1.
- **Depends on:** F1-05.
- **Size:** S.

#### F1-10 — Live acceptance

- **Description:** Three real tickets, one each on three projects Max picks, run end to end. Fix only what those runs expose.
- **Acceptance criteria:** Each of the three tickets reaches `Merged`, `HandedOff`, or the `ship` state that project's `factory.md` defines, without a rule break the model flags as unexpected. Issues found are fixed or filed. No scope is added beyond what the runs expose.
- **Depends on:** F1-05, F1-06, F1-07, F1-09; F1-12 before the Aut0 ticket; F1-15 before the Cleffy or Tracy ticket; Max picking the three tickets.
- **Size:** L.

#### F1-11 — Aut0 Linear ticket

- **Description:** File the cleanup ticket on Aut0's board in the aut0-linear-board skill shape (Description, Acceptance Criteria, Notes), using the draft in [docs/AUT0-CLEANUP.md](AUT0-CLEANUP.md). Title: `chore: slim factory.md and remove old factory plan config`. Priority is **High** (Max, 10:18 AM ET).
- **Acceptance criteria:** One ticket, unassigned, not In Progress at filing, priority High. No GitHub issue is created. Not filed until Max says go.
- **Depends on:** Max saying go.
- **Size:** S.

#### F1-12 — Aut0 cleanup PR

- **Description:** PR on `aut0-code/aut0` targeting `dev`: add the slim `factory.md`, replace the `## Factory` block with a pointer, delete `docs/aut-2166-factory-follow-up.md`, retarget Factory-section pointers, keep pstack including `statechart`, `create-formal-verification-skill`, and the install/preflight scripts. Leave #1305 and #1318 alone.
- **Acceptance criteria:** The acceptance criteria in [docs/AUT0-CLEANUP.md](AUT0-CLEANUP.md) are met. `npm run test:vendor-pstack` and `npm run test:statechart-preflight` still pass. Max picks the merge.
- **Depends on:** F1-11.
- **Size:** M.

#### F1-13 — Orchestrator model and chart

- **Description:** A small TLA+ model of the loop in `model/`, plus the Mermaid statechart `/statechart` draws from it. CI re-checks the model whenever the dispatcher transition table changes.
- **Acceptance criteria:** `/statechart` runs clean on the model. A deliberate transition-table change in CI (or a fixture that omits a decided transition) fails the check. The chart's states match section 4.1, including deferred T9 as present in the model but not enabled in Phase 1 config.
- **Depends on:** F1-05.
- **Size:** M.

#### F1-14 — Weekly eval report

- **Description:** A small script that reads `orch:` comment headers and the dispatcher log, compares each run to the chart, and reports time in each state, wasted agent wakes, review rounds per PR, stuck jobs, and rule breaks the model finds.
- **Acceptance criteria:** The script produces that report from fixtures without live runs. Once F1-10 has data, a weekly run can use it. It does not implement the retired self-improvement routines.
- **Depends on:** F1-13. Live data depends on F1-10, fixtures do not.
- **Size:** S.

#### F1-15 — Migrate Cleffy and Tracy to `factory.md`

- **Description:** One cleanup PR per repo. Cleffy's targets `dev` on `maxharris1/Cleffy`. Tracy's targets `main` on `maxharris1/tracy-harris-coaching`. Each PR adds a slim root `factory.md` in the section 6 schema, with the new review rules (one cross-family reviewer runs the verify skill; low-risk gets 1 round, high-risk gets 3). It moves the still-valid Context notes (for Cleffy: risky areas, the privacy/no-pixels rule, the marketing rules, and that `dev` → `main` is Max's merge; for Tracy: preview-then-fast-forward to `main` after Tracy's OK, pre-push checks, and the CLAUDE.md copy rules). It replaces the `## Factory` block in `AGENTS.md` with a pointer, and keeps the pstack router. Old-plan fields that contradict today's decisions (`reviewers: a+b`, `review: none`, a judge, `qa: browser:click-through`, `fixer: codex-local`) are not carried over. These PRs are planned only until this item is started.
- **Acceptance criteria:** Each repo has a root `factory.md` that passes `orch check-config`. `AGENTS.md` has no `## Factory` section and has one pointer to `factory.md`. The pstack router remains. Cleffy's `factory.md` uses `base_branch: dev`, `tickets: github`, `ship: merge`, and the Context notes listed in section 6. Tracy's uses `base_branch: main`, `tickets: github`, `ship: after:tracy-site`, and the Context notes listed in section 6. Review policy in both is one cross-family reviewer, `review.rounds.low: 1`, `review.rounds.high: 3`, and `review.verify` set to the repo's verify skill or `none`.
- **Depends on:** F1-01 (schema and `check-config`).
- **Size:** M.

### Phase 2

#### F2-01 — Device pool

- **Description:** `machines.yml` with the box, PC and Mac (Tailscale names), slot maximums, and harness eligibility only where Max has provisioned the login under that bot's user.
- **Acceptance criteria:** A harness is never scheduled on a device that is not listed as eligible for it. Slot counts are not exceeded. Missing inventory is not invented; it is asked of Max. Concurrent claims cannot take the same last slot. A disabled or placeholder device never schedules and `check-config` names it. A device that drops mid-job keeps its slot and goes `stuck` through T6, never `done`. `orch status` shows device rows.
- **Depends on:** Phase 1 complete (F1-10), and Max's inventory.
- **Built:** `machines.yml`, `lib/machines_yml.py`, `lib/device_pool.py`. No SSH, remote start, or harness execution; at F2-01 `orch wait` did not yet claim through the pool (F2-02 added that).
- **Size:** M.

#### F2-02 — Remote start

- **Description:** Each owning bot starts its harness on a device over Tailscale SSH as its own user. A slot stays held until the worker is confirmed gone. 60 minutes offline means `stuck`.
- **Acceptance criteria:** Only the owning bot starts that harness, over SSH as `claude-bot`, `codex-bot`, or `cursor-bot`. A slot is released only after the worker is gone. An offline host for 60 minutes marks the job `stuck` and wakes Grok Bot once.
- **Depends on:** F2-01.
- **Built:** `ssh_alias` in `machines.yml`, `lib/remote.py` (SSH argv and the remote start, check and stop scripts), `lib/placed_run.py` (`orch job run` for a placed job), `lib/device_monitor.py` (slot release and the offline check, run by the poller), and `orch wait` claiming through `lib/device_pool.py`. The model has `T6DeviceOffline`. The PC and Mac still ship `enabled: false`; nothing has been run against a real device.
- **Size:** M.

#### F2-03 — Self-hosted CI

- **Description:** Runner labels per project, plus a registration checklist for Max. Max registers the runners. Credential-holding deploy workflows stay hosted. The plan never touches runner tokens.
- **Acceptance criteria:** The checklist tells Max how to register runners without the factory handling tokens. Credential-holding deploy workflows remain on hosted runners. Labels are read from each repo's `factory.md` (`ci_runner:`). The device inventory is in `orch.yml` or `machines.yml`.
- **Depends on:** F2-01, and Max registering runners.
- **Size:** M.

#### F2-04 — `orch status`

- **Description:** A read-only text summary of queue, slots and paused items, generated from the tracker and the local job list.
- **Acceptance criteria:** The command prints that summary and changes no state. It covers the box and, once F2-01 exists, the device pool.
- **Depends on:** F1-04; device rows depend on F2-01.
- **Size:** S.

#### F2-05 — Job telemetry

- **Description:** Stream each job's CLI events into a capped local log and a scrubbed per-job summary in `orch.db`; show a live feed in the job modal and tokens and cost on the Efficiency page (issue #93, section 11).
- **Acceptance criteria:** Claude Code runs with `--output-format stream-json --verbose` and Codex with `codex exec --json`. The summary is written with each heartbeat and at exit. Secrets are scrubbed before anything leaves the machine; the raw log stays local, capped at about 20 MB and rotated. A job with no events shows "no telemetry", not an error. Tests parse fixtures for both CLIs' streams and cover scrubbing and the size cap.
- **Depends on:** F2-02 and issue #80 (the waiter starts the harness).
- **Built:** `lib/telemetry.py` (flags, parsing, scrubbing, the capped log, the session and the `job_telemetry` table), `lib/dashboard_telemetry.py` (read, the job feed and the Efficiency panel), an `output` hook in `lib/job_run.py` and the session wiring in `orch job run`.
- **Size:** M.
