# Factory Phase 1–2 Self-Build Roadmap (Feeder-Compliant GitHub Tickets)

> **Prerequisite:** Merge the stacked Phase 0 stabilization branches (`p0/correctness-fixes` → `p0/runtime-ssh-sqlite` → `p0/security-and-wakes` → `p0/unified-evals-and-cost` → `p0/subtract-dead-code` → `p0/runbook-and-roadmap`) and complete the host/credential steps in [docs/RUNBOOK-HARDENING.md](RUNBOOK-HARDENING.md) before labeling these issues `factory:ready`.
>
> Every ticket below strictly conforms to the factory `#204` feeder intake template (`Acceptance criteria`, `Out of scope`, `Risk:`, `Plan first:`, `Depends on:`, `Touches:`), avoids cross-PR wording in criteria, and targets 150–400 changed lines. When filing on GitHub, replace placeholder IDs like `E1-1` in `Depends on:` with the real GitHub issue numbers (`#1540`, etc.).

---

## Recommended Filing Wave Order

1. **Wave 1 (Parallel immediately after Phase 0):** `E1-1`, `E2-1`, `E2-5`, `E3-3`, `E5-1`
2. **Wave 2:** `E1-2`, `E1-3`, `E2-2`, `E2-3`, `E4-3`
3. **Wave 3:** `E1-4`, `E2-4`, `E3-4`, `E5-2`
4. **Wave 4:** `E3-1`, `E4-2`, `E5-3`
5. **Wave 5:** `E3-2`, `E4-1`

---

## Epic 1: Live App QA & Visual QA Gate

### E1-1 — Fleet-local isolated Docker/worktree + Playwright QA runner (`local-docker`)

**Suggested labels:** `factory:ready`, `priority:p0`

```markdown
## Goal
Implement a real, production-ready fleet-local QA runner (`local-docker`) behind the existing `lib/qa_runners.Runner` protocol so Live App QA runs end-to-end on `pc` or `box` in isolated git worktrees and Docker/Playwright containers without waiting on vendor cloud APIs.

## Acceptance criteria
- `lib/qa_runners/local_docker.py` implements `Runner` (`launch`, `status`, `stop`, `describe`) and registers as `"local-docker"` in `lib/qa_runners`.
- `launch(lane)` is idempotent on `lane.lane_id` (a repeated launch returns the existing `Launched` handle without spawning a duplicate container).
- Each lane executes inside a detached git worktree at `lane.base_ref` and a container with `--rm`, memory/CPU caps, read-only root where feasible, and no access to host `~/.local/state/factory` or `~/.config/factory`.
- Lane artifacts (screenshots `.png`/`.webp`, videos `.webm`/`.mp4`, JSON trace summaries, and the structured QA verdict comment payload) are written to the lane's evidence directory in the layout expected by `lib/qa_evidence.py`.
- `stop(agent_id)` stops the running container, removes the temporary worktree, and succeeds cleanly when the container has already exited.
- `lib/factory_md.py` accepts `local-docker` as a valid `qa.runner` value and `orch check-config` validates it without errors.
- Unit and integration tests in `tests/test_qa_local_docker.py` cover idempotent launch, status transitions (`running` -> `ended`), evidence output layout, timeout/stop cleanup, and `RunnerOutage` when the Docker daemon is unavailable.

## Out of scope
- Cloud vendor drivers (`claude-cloud`, `cursor-cloud`) — handled in E1-3.
- Visual QA auto-hold policy on PR shipping — handled in E1-2.
- Project-specific `verify-*` Playwright scripts for Aut0/Cleffy/Tracy/coo — handled in E1-4.

Risk: high
Plan first: yes
Depends on: none
Touches: lib/qa_runners/__init__.py, lib/qa_runners/local_docker.py, lib/factory_md.py, lib/qa_cloud.py, tests/test_qa_local_docker.py
```

---

### E1-2 — Visual QA Auto-Hold Gate on UI evidence (`visual-qa-review`)

**Suggested labels:** `factory:ready`, `priority:p0`

```markdown
## Goal
Automatically hold any PR whose primary Live App QA run produces UI screenshot or video evidence in `awaiting-ok` (`reason: visual-qa-review`) so Max can inspect the captured visuals and approve with `/ok <sha>` or one click in Mission Control before shipping.

## Acceptance criteria
- `lib/factory_md.py` supports an optional `qa.visual_hold: true|false` setting (default `true` whenever `qa.enabled` is true) and validates it in `orch check-config`.
- When a primary QA run passes on an approved head and its collected evidence contains at least one visual artifact (`.png`, `.jpg`, `.jpeg`, `.webp`, `.webm`, `.mp4`), `lib/dispatch/holds.py` and `lib/dispatch/ship.py` place the PR in `awaiting-ok` with hold reason `visual-qa-review` and include the QA evidence link in the `awaiting-ok` wake detail.
- When a primary QA run passes with zero visual artifacts (for example backend-only or API-only verification), no visual hold is added and normal ship rules apply.
- An approver `/ok <sha>` or `kind=ok` clears the `visual-qa-review` hold for that head and carries across a subsequent rebase whose `stable_patch_id` matches, matching existing `hold.paths` semantics.
- Aut0 (`ship: handoff`) still hands off at Ready for Review without merging; `visual-qa-review` never enables auto-merge on a handoff repo.
- Tests in `tests/test_visual_qa_hold.py` cover visual-evidence auto-hold, non-visual pass-through, `/ok <sha>` release, `stable_patch_id` carry, and Aut0 handoff preservation.

## Out of scope
- Mission Control inline screenshot/video gallery UI — handled in E4-1.
- Changes to `model/Orchestrator.tla` — handled in E5-3.

Risk: high
Plan first: yes
Depends on: E1-1
Touches: lib/factory_md.py, lib/dispatch/holds.py, lib/dispatch/qa.py, lib/dispatch/ship.py, lib/qa_evidence.py, tests/test_visual_qa_hold.py
```

---

### E1-3 — Vendor Cloud QA Runners (`claude-cloud` and `cursor-cloud`)

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Implement real `claude-cloud` and `cursor-cloud` drivers behind `lib/qa_runners.Runner` so repositories can run Live App QA in cloud sandboxes or run primary-vs-shootout comparisons against `local-docker`.

## Acceptance criteria
- `lib/qa_runners/claude_cloud.py` and `lib/qa_runners/cursor_cloud.py` implement `Runner` (`launch`, `status`, `stop`, `describe`) and register as `"claude-cloud"` and `"cursor-cloud"`.
- Both drivers read API credentials exclusively from configured environment variable names (never logged, never serialized to `orch.db` or `Lane.environment_name`) and map HTTP 429 to `UsageLimit`, HTTP 5xx/network failures to `RunnerOutage`, and 4xx config errors to `LaunchFailed`.
- Both drivers enforce idempotent `launch(lane)` keyed by `lane.lane_id` and map vendor lifecycle states to `AgentStatus` (`"running"` or `"ended"`).
- `lib/qa_cloud.py` selects the configured runner via `lib/qa_runners.create(name)` with zero vendor-specific branches.
- Tests in `tests/test_qa_cloud_drivers.py` verify request payloads, idempotency, error classification (`UsageLimit`, `RunnerOutage`, `LaunchFailed`), and secret scrubbing against mocked HTTP transports.
- ops: Provision vendor cloud QA API env vars in host `webhook.env` (or dedicated `qa-cloud.env`) before switching any repo's `qa.runner` from `local-docker`.

## Out of scope
- Changes to `local-docker` container isolation — handled in E1-1.
- Billing alerts or vendor quota dashboards.

Risk: high
Plan first: yes
Depends on: E1-1
Touches: lib/qa_runners/__init__.py, lib/qa_runners/claude_cloud.py, lib/qa_runners/cursor_cloud.py, lib/qa_cloud.py, tests/test_qa_cloud_drivers.py
```

---

### E1-4 — Live App QA Onboarding Templates & Config Validation for Aut0, Cleffy, Tracy, and coo

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Provide validated `qa:` policy templates, starter feature-map journey definitions, and a CLI smoke-check (`orch qa smoke`) so Aut0, Cleffy, Tracy's site, and coo can enable `local-docker` Live App QA cleanly.

## Acceptance criteria
- `examples/aut0/factory.md`, `examples/cleffy/factory.md`, and `examples/tracy/factory.md` include valid, documented `qa:` blocks configured for `local-docker` with `visual_hold: true`, and pass `orch check-config`.
- Add `docs/qa-onboarding.md` with copy-paste starter `verify-<project>/SKILL.md` + `features/<journey>.md` templates for Next.js/web projects (Cleffy, Aut0, Tracy) and CLI/service projects (coo).
- Add `orch qa smoke --repo <owner/name> --runner local-docker` in `lib/cli.py` to run a single synthetic health check of the QA runner, evidence directory write permissions, and `qa_storage` / dashboard evidence path without creating a GitHub comment.
- Tests in `tests/test_qa_onboarding.py` verify all example `factory.md` configs pass `check-config` and `orch qa smoke` succeeds with a fake runner and fails cleanly on missing evidence root.
- ops: Copy the validated `qa:` stanza and starter `verify-*` skill into each target repository (`aut0-code/aut0`, `maxharris1/Cleffy`, `maxharris1/tracy-harris-coaching`, `maxharris1/coo`) and run `orch qa smoke` on `pc`/`box`.

## Out of scope
- Editing the external target repositories directly from this PR (done via per-repo PRs after templates land).

Risk: low
Plan first: no
Depends on: E1-1, E1-2
Touches: examples/aut0/factory.md, examples/cleffy/factory.md, examples/tracy/factory.md, docs/qa-onboarding.md, lib/cli.py, tests/test_qa_onboarding.py
```

---

## Epic 2: Pluggable Harness Layer, pstack Best-in-Slot Role Routing & Even Multi-Account Balancing

### E2-1 — Extract Pluggable `HarnessDriver` Protocol for Claude and Codex

**Suggested labels:** `factory:ready`, `priority:p0`

```markdown
## Goal
Replace hardcoded `if harness == "claude" / "codex"` branching scattered across `lib/harness_env.py`, `lib/telemetry.py`, `lib/usage_probe.py`, and `lib/pstack_pin.py` with a single pluggable `HarnessDriver` protocol (`lib/harnesses/`), migrating Claude and Codex onto it first with zero behavior regression.

## Acceptance criteria
- Create `lib/harnesses/__init__.py` defining the `HarnessDriver` protocol (`name`, `family_for_model`, `bot_user`, `skills_dir`, `account_isolation`, `stream_flags`, `parse_event`, `probe_usage`, `handoff_marker`) and a driver registry (`register`, `get`, `all_drivers`).
- Implement `lib/harnesses/claude.py` and `lib/harnesses/codex.py` encapsulating existing Claude and Codex CLI flags, stream-JSON event parsing, account-home isolation env vars, and pstack skill paths.
- Update `lib/harness_env.py` and `lib/telemetry.py` to delegate harness-specific lookups to the registry instead of duplicating per-harness dictionaries.
- Existing tests (`tests/test_telemetry.py`, `tests/test_telemetry_argv.py`, `tests/test_harness_env_allowlist.py`, `tests/test_usage_readings.py`) pass unchanged.
- Add `tests/test_harness_drivers.py` verifying registry lookup, Claude/Codex driver contracts, and clean rejection of unregistered harness names.

## Out of scope
- Adding Cursor or Gemini drivers — handled in E2-2 and E2-3.
- Changing model routing logic — handled in E2-4.

Risk: high
Plan first: yes
Depends on: none
Touches: lib/harnesses/__init__.py, lib/harnesses/claude.py, lib/harnesses/codex.py, lib/harness_env.py, lib/telemetry.py, tests/test_harness_drivers.py
```

---

### E2-2 — Onboard Cursor CLI (`cursor-agent`) Harness Driver & Auth Verification

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Promote Cursor (`cursor-agent`) from a refused placeholder harness to a first-class `HarnessDriver` with account-home isolation, telemetry parsing, `orch doctor` verification, and an operator sign-in checklist so Cursor can participate in role routing and builds/reviews.

## Acceptance criteria
- Implement `lib/harnesses/cursor.py` registered as `"cursor"`, defining `cursor-agent` print/headless stream flags, token/tool telemetry extraction, `ORCH-HANDOFF:` marker extraction, skills directory (`~/.cursor/skills`), and account-home isolation env vars (`CURSOR_CONFIG_DIR` / stripped API tokens).
- `lib/account_doctor.py` supports checking Cursor authentication state without reading raw credentials, reporting clear signed-out vs signed-in status.
- `lib/pstack_pin.py` (`orch doctor`) includes Cursor in canary verification when Cursor is not listed in `refused_harnesses`.
- `tests/test_cursor_driver.py` verifies `cursor-agent` argv wrapping, JSON stream telemetry parsing, fallback cost estimation via `lib/pricing.py`, account isolation, and `doctor` canary handling.
- ops: Authenticate `cursor-agent` under the `cursor-bot` user (and any extra Cursor account homes) on `pc` and `box`, remove `cursor` from `refused_harnesses` in `orch.local.yml`, and verify `orch doctor` + `orch doctor-runtime` pass.

## Out of scope
- Removing `cursor` from the tracked default `orch.yml` `refused_harnesses` before Max authenticates it on live hosts (use `orch.local.yml` to opt in).
- Vendor Cursor Cloud QA runner — handled in E1-3.

Risk: high
Plan first: yes
Depends on: E2-1
Touches: lib/harnesses/cursor.py, lib/harnesses/__init__.py, lib/account_doctor.py, lib/pstack_pin.py, tests/test_cursor_driver.py
```

---

### E2-3 — Onboard Gemini CLI (`gemini`) Harness Driver & Auth Verification

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Add Google Gemini CLI (`gemini`) as a fourth first-class `HarnessDriver` (`gemini-bot`, `google` model family) with account isolation, stream telemetry, token cost estimation, pstack skill installation, and `orch doctor` verification.

## Acceptance criteria
- Add `"gemini"` to `lib/schema.py` (`HARNESSES`), `"gemini-bot"` to trusted worker bot conventions, and `"google"` model-family resolution (`gemini-*`) in `lib/factory_md.py`.
- Implement `lib/harnesses/gemini.py` registered as `"gemini"`, defining headless CLI flags, stream-JSON telemetry parsing, `GEMINI_CONFIG_DIR` / stripped `GEMINI_API_KEY` & `GOOGLE_API_KEY` account isolation, and `~/.gemini/skills` installation in `lib/pstack_pin.py`.
- Update `lib/dashboard_telemetry.py` and `lib/machines_yml.py` to recognize `gemini` / `gemini-bot` as a native harness alongside Claude, Codex, and Cursor.
- Add `tests/test_gemini_driver.py` covering cross-family resolution (`google` vs `anthropic`/`openai`), stream parsing, pricing via `lib/pricing.py`, account isolation, `install-skills`, and `check-config`.
- ops: Create the `gemini-bot` OS user / SSH alias on `pc` and `box`, authenticate `gemini` CLI under its account home(s), add `gemini: gemini-bot` to `machines.local.yml`, and confirm `orch doctor` passes.

## Out of scope
- Changing default builder/reviewer assignments in existing project `factory.md` files (done via `config/routing.yml` in E2-4).

Risk: high
Plan first: yes
Depends on: E2-1
Touches: lib/schema.py, lib/factory_md.py, lib/harnesses/gemini.py, lib/harnesses/__init__.py, lib/pstack_pin.py, lib/machines_yml.py, lib/dashboard_telemetry.py, tests/test_gemini_driver.py
```

---

### E2-4 — Central pstack Best-in-Slot Role Router (`config/routing.yml`)

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Replace static per-repo builder/reviewer pairs with a central pstack-style benchmark-driven role routing table (`config/routing.yml` + `lib/role_router.py`) that assigns the best `(harness, model, effort)` — or cross-family preference pool — to every factory and pstack role across Claude, Codex, Gemini, and Cursor, with budget tiers and optional per-repo `factory.md` overrides.

## Acceptance criteria
- Define `config/routing.yml` schema and loader in `lib/role_router.py` supporting role slots (`orchestrate`, `plan`, `build:feature`, `build:bug`, `build:refactor`, `build:perf`, `review`, `qa`, and pstack helper tiers), ordered preference pools `[{harness, model, effort}]`, and a `budget` ladder (`unlimited`, `large`, `medium`, `small`).
- Enforce cross-family review mechanically: when resolving `review` for a ticket built by `(builder_harness, builder_model)`, filter out candidates sharing `builder_family` and pick the highest-ranked eligible cross-family candidate (failing closed with a clear error if none is enabled/unpaused).
- Support optional per-repo overrides in `factory.md` while keeping legacy `builder:`/`reviewer:` front matter working cleanly as a fallback when `config/routing.yml` is absent or specifies `inherit`.
- Pass the resolved `model` and `effort` cleanly into queued job payloads and `orch job run` argv without breaking existing `factory.md` configs.
- Validate `config/routing.yml` inside `orch check-config`.
- Add `tests/test_role_router.py` covering role slot selection, cross-family filtering across all 4 harnesses (`anthropic`, `openai`, `google`, `cursor`), budget ladder downgrades, refused-harness filtering, and `factory.md` override precedence.

## Out of scope
- Deleting legacy `factory.md` `builder:`/`reviewer:` schema keys (kept backward-compatible).
- Mission Control routing matrix UI — handled in E4-2.

Risk: high
Plan first: yes
Depends on: E2-1, E2-2, E2-3
Touches: config/routing.yml, lib/role_router.py, lib/factory_md.py, lib/orchestrate.py, lib/dispatch/jobs.py, lib/cli.py, tests/test_role_router.py
```

---

### E2-5 — Even Same-Harness Multi-Account Usage Balancing in `DevicePool`

**Suggested labels:** `factory:ready`, `priority:p0`

```markdown
## Goal
Ensure multiple accounts of the same harness (for example two Claude accounts or two Codex accounts) drain evenly over time rather than saturating one account while peers sit underutilized, replacing the deleted cross-harness 20% reserve concept with true intra-harness load and headroom balancing.

## Acceptance criteria
- Upgrade account scoring in `lib/device_pool.py` so eligible accounts of the same harness rank by balanced effective headroom: combining fresh `five_hour` and `seven_day` remaining headroom, in-flight job pending charge, and rolling recent placement share (so accounts with identical/unmeasured headroom alternate round-robin rather than always hitting the alphabetically first account).
- Never cross-route to a different harness or model family during account placement — balancing operates strictly across accounts of the job's assigned harness.
- Expose per-account placement counts, active jobs, effective headroom score, and skew indicator in `lib/dashboard_usage.py` snapshot output.
- Preserve existing safety guards: paused accounts, `usage_limit` accounts, accounts below `FLOOR` headroom, and accounts at `max_jobs` are never selected.
- Add `tests/test_account_balancing.py` proving even distribution across 2 and 3 accounts under: (a) skewed fresh readings, (b) identical/unmeasured readings (round-robin tie-break), (c) concurrent claims, and (d) one paused or usage-limited peer.

## Out of scope
- Cross-harness fallback routing — governed by `config/routing.yml` in E2-4.
- UI charts for account balance — handled in E4-2.

Risk: high
Plan first: yes
Depends on: E2-1
Touches: lib/device_pool.py, lib/dashboard_usage.py, tests/test_account_balancing.py
```

---

## Epic 3: Universal Pull-Based AI-Agnostic Orchestrator, Natural D31 Intake & Thin pstack Briefs

### E3-1 — Pull-Based Universal Orchestrator (`orch wait orchestrator` + `driver_inbox` + `orchestrator/SKILL.md`)

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Decouple orchestration from Grok webhooks and the WSL Claude session hack by introducing a pull-based `orch wait orchestrator` loop backed by `driver_inbox` and a single AI-agnostic `orchestrator/SKILL.md` so Claude, Codex, Gemini, Grok, or Cursor can run as the central orchestrator identically with full telemetry and zero 30/hr cloud-routine limits.

## Acceptance criteria
- Add `orch wait orchestrator` (and `orch orchestrator ack/finish`) in `lib/cli.py` and `lib/wait.py`, blocking with zero tokens until a wake/inbox item is available in `driver_inbox` (claiming atomically with lease heartbeat and retry-on-crash).
- Add a vendor-neutral `skills/factory/orchestrator/SKILL.md` (hashed in `skills/skills.lock`) replacing Grok-specific wording while preserving the exact wake table, stop policy, and `orch:v1` handoff contract.
- Support configuring `driver: pull` (or selecting any `HarnessDriver` via `config/routing.yml` `orchestrate` slot) in `orch.yml`, keeping existing webhook push (`grok` / `claude` fire URL) as an optional fallback when configured.
- Record orchestrator session token usage and USD spend in `job_telemetry` (bot `orchestrator`) so orchestrator cost appears in Monday weekly evals.
- Ship `deploy/orch-orchestrator.service` template and update `tests/test_orchestrator_pull.py` to test claim, lease expiry requeue, deduplication by `logical_key`, telemetry capture, and full wake-table parity.

## Out of scope
- Deleting legacy `grok/orchestrate-project/SKILL.md` until Max confirms cutover from Grok Bot.
- Mission Control inbox UI for product questions — handled in E4-1.

Risk: high
Plan first: yes
Depends on: E2-1, E2-4
Touches: lib/cli.py, lib/wait.py, lib/dispatch/inbox.py, lib/orch_yml.py, skills/factory/orchestrator/SKILL.md, skills/skills.lock, deploy/orch-orchestrator.service, tests/test_orchestrator_pull.py
```

---

### E3-2 — Restore Natural D31 Clarifying Intake Flow

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Allow Max to file rough, natural-language tickets (without filling out rigid boilerplate) that enter `intake`, wake the universal orchestrator to run D31 clarifying questions one product call at a time, and automatically transition into a validated brief + `orch:go` while keeping the strict `factory:ready` feeder fast-path for pre-structured tickets.

## Acceptance criteria
- Add an optional `intake.natural_label` (default `factory:idea` on GitHub, or `Backlog`/`Triage` state on Linear) in `orch.yml` so rough issues enter `orch:state/intake` and enqueue a single `intake-clarify` item in `driver_inbox` instead of failing with `refused-intake`.
- Keep `intake.label` (`factory:ready`) enforcing the strict `#204` template so well-formed tickets continue to feed directly with zero orchestrator latency.
- Enforce D31 question discipline: `observable` and `default-able` technical questions are recorded with defaults without waiting on Max; only genuine `product` questions transition the ticket to `waiting-max` and surface in the Mission Control Product-Question Inbox (and chat).
- Once Max answers all open product questions, the orchestrator posts the structured `brief` (including `Acceptance criteria`, `Risk`, `Out of scope`, `Done-evidence`) and `kind=go` to launch `planning` or `building`.
- Add `tests/test_natural_intake.py` covering rough ticket pickup, deduplication of `intake-clarify` wakes, product-question pause/resume, and unchanged strict validation on `factory:ready`.

## Out of scope
- Replacing the `#204` feeder template for `factory:ready` issues.
- Mission Control UI rendering of the question inbox — handled in E4-1.

Risk: high
Plan first: yes
Depends on: E3-1
Touches: lib/orch_yml.py, lib/feeder.py, lib/dispatch/questions.py, lib/dispatch/inbox.py, skills/factory/orchestrator/SKILL.md, tests/test_natural_intake.py
```

---

### E3-3 — Slim `lib/brief/` Back to Thin pstack Routers

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Eliminate rule duplication between Python prompt generators in `lib/brief/` and the vendored pstack skills so pstack owns how work gets done and `lib/brief/` emits only dynamic ticket context, paths, evidence, and thin router pointers.

## Acceptance criteria
- Remove duplicated work-rule prose (generic coding, review, and commit-hygiene instructions already defined in `skills/pstack/` and `skills/factory/*`) from `lib/brief/build.py`, `lib/brief/fix.py`, `lib/brief/plan.py`, `lib/brief/review.py`, and `lib/brief/rules.py`.
- Remove double-injected model/harness preamble lines so each brief states its dynamic facts once.
- Add a brief budget regression test in `tests/test_brief_thinness.py` asserting static boilerplate size stays under a tight ceiling and every generated builder/reviewer/QA brief still names its required dynamic evidence, head SHA, criteria, and `factory-*` router skill.
- Regenerate `skills/skills.lock` if any `skills/factory/*/SKILL.md` wording is tightened so `orch doctor` stays green.

## Out of scope
- Upgrading the vendored pstack version (`0.15.36`).
- Changing `orch:v1` header fields.

Risk: low
Plan first: yes
Depends on: none
Touches: lib/brief/build.py, lib/brief/fix.py, lib/brief/plan.py, lib/brief/review.py, lib/brief/rules.py, skills/factory/factory-builder/SKILL.md, skills/factory/factory-reviewer/SKILL.md, skills/skills.lock, tests/test_brief_thinness.py
```

---

### E3-4 — Real End-to-End `orch doctor` Live Skill-Load Verification Across All Harnesses

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Make `orch doctor` genuinely prove that every enabled harness (Claude, Codex, Gemini, Cursor) actually loads the exact pinned pstack version at runtime and that Claude's `~/.claude/{agents,docs,rules}` support files match the lock.

## Acceptance criteria
- Unify `orch doctor` (`lib/pstack_pin.py`) and `lib/skill_probe.py` so `orch doctor --live` executes the pinned canary prompt through each enabled, non-refused `HarnessDriver` (respecting `refused_harnesses` and device SSH aliases) and verifies the exact quoted passage from `skills/pstack`.
- Extend `installed_errors()` in `lib/pstack_pin.py` to check `~/.claude/{agents,docs,rules}` symlinks/files when installed, flagging missing or stale files with actionable remediation commands.
- Keep offline `orch doctor --responses DIR` working identically for CI and unit tests.
- Add `tests/test_doctor_live.py` covering multi-harness live probe pass/fail, skipped refused harnesses, stale symlink detection in `~/.claude/rules`, and timeout handling.

## Out of scope
- Automatic re-installation of skills during `orch doctor` (remains read-only; `orch install-skills` performs writes).

Risk: low
Plan first: yes
Depends on: E2-2, E2-3
Touches: lib/pstack_pin.py, lib/skill_probe.py, lib/cli.py, tests/test_doctor_live.py
```

---

## Epic 4: Mission Control Daily Operator Cockpit

### E4-1 — Product-Question Inbox & Live QA Screenshot/Video Review Gate in Mission Control

**Suggested labels:** `factory:ready`, `priority:p0`

```markdown
## Goal
Turn Mission Control into Max's single daily cockpit for both human touchpoints: answering D31 / mid-work product questions inline and inspecting Live App QA screenshots/videos with one-click Approve (`/ok <sha>`) or Request Changes (`/go`).

## Acceptance criteria
- Add a **Needs Max** / **Product Inbox** panel on Mission Control (`index.html` and `ticket.html`) listing tickets in `waiting-max` (with open product questions) and `awaiting-ok` (including `visual-qa-review` holds).
- For `visual-qa-review` holds, render an inline gallery of the run's `.png`/`.webp` screenshots and `.webm`/`.mp4` video players served via existing `/qa/runs/<run_id>/...` routes, plus one-click **Approve & Ship** (`ok` on the verified head SHA), **Request Changes** (posts approver instruction), and **Re-run QA** buttons backed by `POST /api/act`.
- For open product questions, render each question clearly with an inline answer box that submits via `POST /api/act` using genuine `from=approver role=approver` attribution and logs to `DASHBOARD_ACTIONS_AUDIT`.
- Reject stale head SHAs or already-answered questions cleanly with HTTP 409 and a visible refresh prompt.
- Add tests in `tests/test_dashboard_cockpit.py` covering API validation, stale-head rejection, genuine approver attribution, and HTML/JSON rendering of visual QA galleries and product questions.

## Out of scope
- Extracting inline JS/CSS into static bundles — handled in E4-3.
- Account balance & eval charts — handled in E4-2.

Risk: high
Plan first: yes
Depends on: E1-2, E3-2
Touches: deploy/factory-dashboard-http.py, lib/dashboard.py, lib/dashboard_pages/qa_evidence.py, lib/dashboard_pages/ticket.py, lib/dashboard_pages/index.py, tests/test_dashboard_cockpit.py
```

---

### E4-2 — Role-Routing Matrix, Multi-Account Balance Panel & Monday Weekly Eval Trends Tab

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Give Max full visibility in Mission Control into which `(harness, model, effort)` owns each factory role, how evenly multi-account pools are draining, and week-over-week trends from the Monday weekly eval report (including true cost-per-PR and wasted spend).

## Acceptance criteria
- Add a **Routing & Accounts** section to Mission Control's `efficiency.html` / `settings.html` showing the active `config/routing.yml` role matrix (with budget tier) alongside per-account 5-hour/7-day headroom, rolling placement balance, and skew warnings from E2-5.
- Add a **Weekly Evals** tab (`evals.html`, served by `factory-dashboard-http.py`) consuming the Monday JSON output (`eval-report --db --json`) pushed alongside `status.json`, displaying week-over-week deltas for: throughput, first-pass approve rate, cap rate, poll errors, stuck/failed jobs, cycle-time p50/p90, Max interventions, wasted wakes, and productive vs. wasted USD spend.
- Update `deploy/weekly-eval.sh` and `orch dashboard push` so the latest 12 weekly JSON snapshots are included in the dashboard push directory with strict path/size validation.
- Add tests in `tests/test_dashboard_evals_view.py` verifying snapshot collection, `evals.html` rendering with 0, 1, and multiple weekly snapshots, and `factory-dashboard-http.py` allowlist serving of `/evals.html` and `/evals.json`.

## Out of scope
- Interactive editing of `config/routing.yml` from the browser (read-only display in this ticket).

Risk: low
Plan first: yes
Depends on: E2-4, E2-5
Touches: lib/dashboard.py, lib/dashboard_pages/shell.py, lib/dashboard_pages/efficiency.py, lib/dashboard_pages/evals.py, deploy/factory-dashboard-http.py, deploy/weekly-eval.sh, tests/test_dashboard_evals_view.py
```

---

### E4-3 — Extract Embedded Mission Control JS & CSS into Static Assets with Strict CSP

**Suggested labels:** `factory:ready`, `priority:p1`

```markdown
## Goal
Eliminate the 5,500+ lines of embedded ES5 JavaScript and 730+ lines of CSS stuffed inside Python string literals across `lib/dashboard_pages/`, moving them into real static `.js`/`.css` assets served by `factory-dashboard-http.py` so pages have syntax/lint checking, single-path rendering, and a strict Content-Security-Policy without `'unsafe-inline'`.

## Acceptance criteria
- Extract shared styles and page scripts from `lib/dashboard_pages/*.py` into versioned static assets under `lib/dashboard_static/` (`app.css`, `pixelstrip.js`, `cockpit.js`, and per-page entry modules) copied/pushed by `orch dashboard push`.
- Update `deploy/factory-dashboard-http.py` to serve allowlisted `/static/<filename>` `.js` and `.css` files using the existing `O_NOFOLLOW` regular-file descriptor checks and drop `'unsafe-inline'` from `script-src` in the CSP header.
- Keep `tests/test_dashboard_pixelstrip.py`, `tests/test_dashboard_shell.py`, and `tests/test_dashboard_http.py` passing against the extracted assets.
- Add a Node syntax/parse check test (`tests/test_dashboard_static.py`) that parses every shipped `.js` file cleanly and verifies no `<script>` tag contains inline JavaScript in rendered HTML pages.

## Out of scope
- Adding a heavy frontend build toolchain (npm/webpack/React); keep clean, dependency-free browser JS + CSS.

Risk: high
Plan first: yes
Depends on: none
Touches: lib/dashboard_static/, lib/dashboard_pages/shell.py, lib/dashboard_pages/index.py, lib/dashboard_pages/ticket.py, lib/dashboard_pages/activity.py, lib/dashboard_pages/efficiency.py, lib/dashboard_pages/health.py, lib/dashboard_pages/job.py, lib/dashboard_pages/settings.py, lib/dashboard.py, deploy/factory-dashboard-http.py, tests/test_dashboard_static.py
```

---

## Epic 5: Architectural De-Bloating, TLA+ Model Resync & Modular Docs

### E5-1 — Collapse Review-Stop Policies to `convergence` & Decompose 23-Mixin `Dispatcher`

**Suggested labels:** `factory:ready`, `priority:p2`

```markdown
## Goal
Remove overlapping legacy review-stop knobs (`auto_extra_rounds`, overlapping `rule_engine` cap/repeat rules) in favor of a single clean `convergence` review policy, and begin decomposing the 23-mixin `Dispatcher` inheritance hierarchy into explicit, typed collaborator objects.

## Acceptance criteria
- Consolidate review round continuation/stopping in `lib/dispatch/verdict/convergence.py` and `lib/rule_engine.py`, deprecating redundant `auto_extra_rounds` when `convergence` is active (emitting a clear `check-config` warning instead of silent shadowing).
- Extract at least 4 major `Dispatcher` mixin groups (`ShipCoordinator`, `QaCoordinator`, `HoldCoordinator`, `ReviewCoordinator`) into composed collaborator classes held on `Dispatcher` with explicit constructor dependencies rather than implicit `self` mixin state.
- Reduce `Dispatcher` base class count from 23 mixins to <= 12 in this step while keeping the public `Dispatcher` API and all existing `tests/test_dispatcher*.py`, `tests/test_convergence*.py`, and `tests/test_ship*.py` suites green.
- Add `tests/test_dispatcher_composition.py` verifying collaborator wiring and single-policy convergence stop behavior.

## Out of scope
- Splitting `poll_source.py`, `feeder.py`, and `cli.py` — handled in E5-2.
- Updating `model/Orchestrator.tla` — handled in E5-3.

Risk: high
Plan first: yes
Depends on: none
Touches: lib/dispatcher.py, lib/dispatch/core.py, lib/dispatch/ship.py, lib/dispatch/qa.py, lib/dispatch/holds.py, lib/dispatch/verdict/convergence.py, lib/rule_engine.py, lib/factory_md.py, tests/test_dispatcher_composition.py
```

---

### E5-2 — Split Oversized Runtime Modules (`poll_source`, `feeder`, `cli`) with GraphQL Poll Batching & Modular Docs

**Suggested labels:** `factory:ready`, `priority:p2`

```markdown
## Goal
Break up the three largest monolithic modules (`lib/poll_source.py` ~3.9K LOC, `lib/feeder.py` ~3.4K LOC, `lib/cli.py` ~3.0K LOC) into cohesive subpackages, batch GitHub PR/check-run reads via GraphQL to cut per-cycle REST API calls, and reorganize the 455 KB append-only `docs/SPEC.md` into navigable `docs/architecture/*.md` modules.

## Acceptance criteria
- Split `lib/poll_source.py` into a `lib/poll/` subpackage (`github.py`, `linear.py`, `cache.py`, `graphql.py`) with a GraphQL batch query fetching open tracked PR heads, labels, mergeability, and check rollups in 1 call per repo per cycle (falling back cleanly to REST on GraphQL error).
- Split `lib/feeder.py` into `lib/feeder/` (`template.py`, `gates.py`, `runner.py`) and `lib/cli.py` command handlers into `lib/commands/` while keeping `lib.poll_source`, `lib.feeder`, and `lib.cli` public imports backward-compatible.
- Split `docs/SPEC.md` into clean modular guides under `docs/architecture/` (`overview.md`, `statechart.md`, `handoff-format.md`, `security.md`, `qa.md`, `routing.md`), keeping `docs/SPEC.md` as a concise index linking to them and archiving the obsolete `docs/PLAN.md` under `docs/archive/PLAN-2026-11.md`.
- All existing CLI, poller, and feeder tests pass plus `tests/test_poll_graphql.py` verifies batched GraphQL parsing and REST fallback.

## Out of scope
- Changing external CLI flags or `orch:v1` comment wire format.

Risk: high
Plan first: yes
Depends on: E5-1
Touches: lib/poll_source.py, lib/poll/, lib/feeder.py, lib/feeder/, lib/cli.py, lib/commands/, docs/SPEC.md, docs/architecture/, docs/archive/, tests/test_poll_graphql.py
```

---

### E5-3 — Resync `model/Orchestrator.tla` and `model/chart.mmd` with Live Dispatcher Semantics

**Suggested labels:** `factory:ready`, `priority:p2`

```markdown
## Goal
Bring the TLA+ formal specification (`model/Orchestrator.tla`) and generated Mermaid diagram (`model/chart.mmd`) back into exact parity with live dispatcher behavior so `model/check.sh` and weekly eval transition checks catch real drift instead of checking outdated assumptions.

## Acceptance criteria
- Update `model/Orchestrator.tla` and `.cfg` files to model: (a) `convergence` review continuation/stop (replacing the phantom `acUnmet` cap rule), (b) `review.start_before_ci` concurrent review+CI, (c) concurrent QA execution with `visual-qa-review` auto-hold into `awaiting-ok`, and (d) `TR` update-from-main / rebase transitions.
- Regenerate `model/chart.mmd` via `model/check.sh` using the vendored `skills/pstack/skills/statechart/scripts/statechart` script and confirm `model/check.sh` exits 0 with all invariants and negative counterexample fixtures passing.
- Confirm `lib/eval_report.py`'s `load_chart_transitions()` parses the regenerated `model/chart.mmd` cleanly and `tests/test_eval_report.py` passes with zero unknown or broken transitions.

## Out of scope
- Changing the vendored pstack statechart script itself.

Risk: high
Plan first: yes
Depends on: E1-2, E5-1
Touches: model/Orchestrator.tla, model/Orchestrator.cfg, model/OrchestratorAll.cfg, model/chart.cfg, model/chart-all.cfg, model/chart.mmd, model/view.mjs, model/check.sh, model/fixtures/
```
