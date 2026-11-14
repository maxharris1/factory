# Plan for #147: Live run data LR1, the agent's task list, current task and sub-agents in job telemetry (runner)

The ticket page's Live run panel (#149, artboard `RunTape.dc.html`) needs four things for each running job: the agent's current task,
its plan (up to 12 items, `text` + `status`), its last finished task, and its sub-agents (up to 8: `kind`, `description`, `status`,
`started_at`, `finished_at`). `lib/telemetry.py` already reads both CLIs' streams and throws this data away. This ticket keeps it,
scrubbed and capped, in four new `job_telemetry` columns. #148 (LR2) reads those columns into the dashboard snapshot. This ticket
does not touch `lib/dashboard.py`, `lib/dashboard_telemetry.py` or `lib/dashboard_page.py`.

This plan is for PR #154 (branch `factory/issue-147`). Risk is high, so Codex Bot reviews it for up to three rounds, and Grok Bot
merges it on `ship: handoff`.

**What Max approved (STATUS.md decision 7, 2026-11-13 6:10 PM ET).** This narrowly reverses r10's "no text is ever stored" rule. It
covers task-list text, the current task, and a sub-agent's description and type. It does not cover sub-agent prompts, any tool
result, assistant text, command lines or paths. Those stay unstored.

## How to read this

Each box is one unit of work and names the evidence that checks it. Check a box only when that evidence exists: a test, a log
line or a SHA. The build runs under poteto-mode's feature playbook as one PR with no UI. Tests alone do not count as
verification. The PR is verified only when the unit, revert and CI boxes under **Verify** are all checked.

## What the streams carry today (evidence gathered for this plan)

These facts come from the installed Claude Code **2.1.322** bundle (its event emitters and tool schemas) and from this plan job's
own run, which started one background sub-agent on purpose. This job's `events.jsonl` on the box therefore holds a real `Agent`
call, its `task_started` event and its `task_notification` event.

- **Claude sub-agents.** The tool is named `Agent`. CLIs before 2.1 called it `Task`, so accept both names. Its input holds
  `description`, `prompt`, `subagent_type`, `run_in_background`, `model` and `isolation`. Sub-agents run **in the background by
  default**. For a background sub-agent, the `tool_result` is only a launch acknowledgement ("Async agent launched
  successfully..."). The actual finish arrives later.
- **Claude stream-json task events** are `system` events:
  - `task_started` carries `task_id`, `tool_use_id`, `description`, `subagent_type`, `task_type`, `is_backgrounded` and
    `parent_task_id`.
  - `task_progress` carries `task_id`, `tool_use_id`, `description`, `subagent_type`, `summary`, `last_tool_name` and `usage`.
  - `task_notification` carries `task_id`, `tool_use_id`, `status` (`completed | failed | stopped`), `summary`, `output_file` and
    `result`.
  - These events also cover tasks that are not sub-agents (background shells, monitors). Telemetry counts only events whose
    `tool_use_id` matches an `Agent`/`Task` call.
  - The CLI warns that the same task can notify **more than once** when someone resumes it, so a finished sub-agent can run again.
  - Telemetry never reads `summary`, `result`, `output_file` or `prompt`.
- **Claude task list, two forms.**
  - `TodoWrite`, with `todos: [{content, status: pending|in_progress|completed, activeForm}]`. Each call sends the whole list.
  - Tasks v2 (`CLAUDE_CODE_ENABLE_TASKS`): `TaskCreate {subject, description, activeForm}` and `TaskUpdate {taskId, status, ...}`.
    The new task's id appears only in the result text `Task #<id> created successfully`.
  - **Neither tool is in claude-bot's tool list in this harness run.** Bot runs may report no task list at all, so the "current
    task" fallback below is the common case, not an edge case.
- **Codex.** `item.*` events whose `item.type == "todo_list"` carry `items: [{text, completed}]`. `_codex_start` drops them today.
  There is no `in_progress` status. Codex is not installed on this Mac, so the build confirms the item shape and whether
  `codex exec --json` reports any sub-agent item on the current Codex version on codex-bot's device. If it reports none, or reports
  one whose only free text is the prompt, the answer is **"sub-agents not available for codex"**. That answer is acceptable, and
  the PR states it together with the Codex version.

## Decisions (technical; Factory may decide these)

1. **Schema.** Four nullable `TEXT` columns on `job_telemetry`:
   - `plan`: a JSON list of up to 12 `{"text", "status"}`, where status is one of `pending | in_progress | done`.
   - `current_task`: a plain string.
   - `last_finished`: a plain string.
   - `subagents`: a JSON list of up to 8 `{"kind", "description", "status", "started_at", "finished_at"}`, where status is one of
     `running | finished | failed`.

   Plan and sub-agents are bounded lists that are always read whole, so each is one JSON column, the same pattern as `steps` and
   `tool_calls`. Current and last task are single values, so plain columns are simpler for LR2 to read and validate.
   **`NULL` means "not recorded"** (a counters-only run, or a row written by an older runner). **`[]` or an empty value means
   "recorded, and the harness reported none."** LR2 needs that difference to choose between showing `no telemetry` and an empty
   panel.
2. **Migration on the self-creating DDL path.** `ensure_table` keeps `CREATE TABLE IF NOT EXISTS` with the new columns, then
   reads `PRAGMA table_info` and runs `ALTER TABLE ... ADD COLUMN` for any column that is missing. Two runners can race on the same
   database, so a `duplicate column name` error from the loser is ignored. Every other error still falls under `_write`'s one log
   line.
3. **Text cap.** `TASK_TEXT_LIMIT = 120` characters, applied through `Scrubber(text, limit=TASK_TEXT_LIMIT)`. The scrubber already
   folds text to one line, replaces env secrets, runs `redact`, clips the result, and hides anything longer than 16 KiB. 120 fits a
   task line or sub-agent description. `LABEL_LIMIT` (40) still applies to tool names. `kind` (`subagent_type`) uses `LABEL_LIMIT`.
4. **The setting.** `ORCH_TELEMETRY=counters` keeps exactly the r10 behaviour: same flags, raw log, counters, tool-name counts and
   fixed step labels, with the four new columns left `NULL`. In this mode no task-list or sub-agent text is ever parsed into
   memory. `off` and its synonyms (`0`, `no`, `false`) are unchanged. Any other value, including unset, means full capture.
   The setting is read where `enabled()` is read today: from the environment of the process that runs `orch job run` (the
   waiter's environment on the box for remote runs).
5. **Status mapping.**
   - Claude `completed` maps to `done`.
   - A Tasks-v2 `TaskUpdate` with `status: deleted` removes the item.
   - For Codex, `completed: true` maps to `done`. Because Codex has no in-progress state, the first incomplete item becomes
     `in_progress` (derived) and the rest are `pending`.
   - For sub-agents, `task_notification` `completed` maps to `finished`, and `failed` or `stopped` maps to `failed`. A `tool_result`
     with `is_error` maps to `failed`. A later `task_started` or `task_progress` for a finished task sets it back to `running`
     (a resume) and clears `finished_at`.
6. **Sub-agent finish.** A non-background call (input `run_in_background: false`, and no `task_started` saying `is_backgrounded`)
   finishes on its `tool_result`. A background call finishes only on its `task_notification`, because its `tool_result` is just the
   launch acknowledgement. If the stream ends with a sub-agent still running, it stays `running` in the final row. The job's
   `finished_at` shows the run is over, and LR2 decides how to display that.
7. **Current task, in order of preference:**
   1. The `in_progress` item's text (`activeForm` when Claude gives one, else `content`/`subject`).
   2. When the harness reports no list, or no item is in progress: the newest **running** sub-agent's description.
   3. Otherwise empty, meaning none.

   It is never derived from a Bash `description`, a command or assistant text. **Last finished task** is the item that most
   recently moved to `done` between list snapshots. If none has moved yet, it is the last `done` item in list order, else empty.
8. **Caps and choosing what to keep.**
   - A plan longer than 12 items is stored as the 12-item window that contains the `in_progress` item, starting at most 2 items
     before it. Without an in-progress item, the window is the first 12 items.
   - Sub-agents: keep all running ones first, then the newest finished ones, up to 8. When more than 8 are running, keep the
     8 newest.
   - The Tasks-v2 id map is capped at 200 entries. Ids are kept in memory only and never stored.
9. **Failure rule.** The new parsing checks types on every field, so a malformed `todos`, `items` or `input` drops only that one
   capture. It never raises, so the counters for the same event still update. Anything unexpected falls under `Session._consume`'s
   existing one-line `parse` warning. Writes stay on the existing heartbeat `flush` and `finish`, with no new timer.

## Work

### W1. Schema and migration (`lib/telemetry.py`)
- [x] Add `plan`, `current_task`, `last_finished` and `subagents` to `COLUMNS` and `DDL`, all nullable `TEXT`.
- [x] Have `ensure_table` add any missing column with `ALTER TABLE ... ADD COLUMN`, tolerating `duplicate column name`.
  Evidence: `test_an_old_table_gains_the_live_run_columns` builds the #93 table by hand, runs a `Session.flush`, and asserts the
  row is written with the new columns. `test_two_runners_migrating_at_once_both_write` runs `ensure_table` twice on one old table.

### W2. Capture in `Summary` (`lib/telemetry.py`)
- [x] `Summary(harness, scrub, *, text=True)` gets a small `LiveRun` helper that holds the plan, a pending Tasks-v2 id map, the
  sub-agents keyed by `tool_use_id`, and the last finished task.
- [x] Claude path:
  - `_claude_tool` captures `TodoWrite` and `TaskCreate`/`TaskUpdate`, plus `Agent`/`Task` (kind = `subagent_type`, defaulting to
    `general-purpose`; description = `description`).
  - The user `tool_result` branch finishes non-background sub-agents and reads the `Task #<id> created` id with a regex. The text
    is never stored.
  - A new `system` branch handles `task_started`, `task_progress` and `task_notification` for sub-agents it knows. It reads only
    `tool_use_id`, `task_id`, `status` and `is_backgrounded`.
- [x] Codex path: `_codex_item` reads `todo_list` on `item.started`, `item.updated` and `item.completed`. A sub-agent item is added
  only if W5 finds one with a non-prompt description field.
- [x] `row()` emits the four columns. With `text=False` it emits `None` for all four and the helper is never fed.
  Evidence: the fixture tests in W5.

### W3. Text safety (`lib/telemetry.py`)
- [x] Every stored string goes through `self.scrub(value, TASK_TEXT_LIMIT)`, or `LABEL_LIMIT` for `kind`. This gives one line, env
  secrets replaced, `redact`, the cap, and hiding whole when blind or longer than 16 KiB.
- [x] Statuses are always mapped onto the fixed enums, never copied from the stream. Timestamps are the runner's `now`, never
  stream text.
- [x] Never read: `prompt`, `description` of Bash or TaskCreate, `summary`, `result`, `output_file`, `content` of tool results
  (beyond the TaskCreate id regex), or assistant text.
  Evidence: `test_live_run_text_is_scrubbed_capped_and_one_line` (a task holding `@@GH_TOKEN@@`, an env secret, a URL and a
  newline, plus a 500-character item). `test_prompts_results_and_summaries_never_reach_the_row` puts sentinel strings in every
  never-read field and asserts none appears in `json.dumps(row)`. `test_a_blind_remote_session_hides_task_text` asserts every
  new string is `[redacted-env]` before the worker environment report arrives.

### W4. The setting (`lib/telemetry.py`, `lib/cli.py`)
- [x] Add `text_enabled(environ)`: false for `ORCH_TELEMETRY=counters`.
- [x] Thread it through `start_session` → `Session` → `Summary(text=...)`.
- [x] Update the `orch job run` help text in `lib/cli.py`.
  Evidence: `test_counters_only_stores_no_live_run_text` feeds the sub-agent fixture with `ORCH_TELEMETRY=counters`. It asserts
  the four columns are `NULL`, every counter equals the full-mode run, and no fixture task or description string appears anywhere
  in the row. `test_counters_only_still_adds_the_stream_flags` covers the argv. `test_start_session_is_off_with_orch_telemetry_off`
  and the `cli job run ... telemetry off` tests stay unchanged and passing.

### W5. Fixtures from real streams (`tests/fixtures/telemetry/`)
- [x] `claude_subagents_stream.jsonl`, redacted from a real Claude Code 2.1.322 `events.jsonl`. Source: this plan job
  (`0184c2dc…`) on the box, or any recent claude-bot job that used `Agent`. Grep only tool names and event subtypes first, then
  copy only the event shapes. It contains:
  - one background `Agent` with its `task_started`, launch acknowledgement and `task_notification` `completed`;
  - one non-background `Agent` whose `tool_result` has `is_error`;
  - one resumed task that notifies twice;
  - a `TodoWrite` list in three snapshots.
  Prompt, result and summary text is replaced with sentinels; ids and paths are replaced with fakes.
- [x] `claude_tasks_v2_stream.jsonl`: `TaskCreate` ×3, `TaskUpdate` to in_progress, completed and deleted.
- [x] `codex_todo_exec.jsonl`: `todo_list` started, updated and completed. If the current Codex emits a sub-agent item, it is
  included too.
- [x] If a box `events.jsonl` with `TodoWrite` or `TaskCreate` exists, use it. If none does (this harness has neither tool
  enabled), build those lines from the 2.1.322 tool schemas above. The PR names which lines are real and which are
  schema-derived, and gives the CLI versions. Secret-shaped values use the existing `@@GH_TOKEN@@` and `@@API_KEY@@`
  placeholders, so no literal token sits in the repo.
  Evidence: the fixtures, and the PR body's "evidence" section with the CLI versions and the Codex sub-agent answer.

### W6. Docs
- [x] `lib/telemetry.py` module docstring: the r10 paragraph and the "Secrets" paragraph now list the one exception and the
  `counters` setting.
- [x] `docs/SPEC.md` §11 (line ~624 and ~640): update the same two statements, and add a decision-log row citing STATUS.md
  decision 7 (Max, 2026-11-13 6:10 PM ET).
- [x] The §4.3 table row mentions `ORCH_TELEMETRY=counters`.
  Evidence: the diff.

## Criterion → work map

| # | Criterion (from #147) | Met by |
|---|---|---|
| AC1 | Fixture tests for both harnesses, including a Claude run with sub-agents | W5 fixtures, plus `test_claude_subagents_fixture_fills_the_live_run`, `test_claude_tasks_v2_fixture_fills_the_plan`, `test_codex_todo_list_fixture_fills_the_plan_and_derives_in_progress` and `test_codex_reports_no_subagents` (or its capture test, if W5 finds a Codex sub-agent item) |
| AC1b | Secret-shaped strings in task text are scrubbed | W3: `test_live_run_text_is_scrubbed_capped_and_one_line`, `test_a_blind_remote_session_hides_task_text`, `no_secret(row)` in every new fixture test |
| AC2 | Counters-only setting stores zero text (test) | W4: `test_counters_only_stores_no_live_run_text` |
| AC3 | `ORCH_TELEMETRY=off` behaviour unchanged (existing tests pass) | W4 (the off path is untouched), with existing `test_start_session_is_off_with_orch_telemetry_off`, `test_cli_job_run_with_telemetry_off_runs_the_command_as_given` and `test_with_telemetry_off_a_remote_start_reports_no_environment` unchanged and passing in the full suite |
| B1 | Capture Codex `todo_list`, Claude task-list and sub-agent `tool_use`, and the results that finish sub-agents | W2 |
| B2 | Confirm tool names and fields from real `events.jsonl` on current CLIs; commit redacted fixtures; "not available for codex" if true | Evidence section above, W5, PR evidence section |
| B3 | Derive "current task" when no task list, or say none | Decision 7, W2; `test_current_task_falls_back_to_the_running_subagent_then_none` |
| B4 | One JSON column each for plan and sub-agents (or columns, justified), via the self-creating DDL path | Decisions 1–2, W1 |
| B5 | Never store prompts, results, assistant text, command lines or paths; `Scrubber`, one line, length cap | Decision 3, W3; `test_prompts_results_and_summaries_never_reach_the_row` |
| B6 | Counters-only setting (r10 behaviour); `off` still runs the command unchanged | Decision 4, W4 |
| B7 | Written on the ~15 s heartbeat flush and at exit; an error is one log line and never stops the job or heartbeat; raw log never leaves the machine | Decision 9; no new writer. `test_a_malformed_todo_input_is_dropped_and_counters_still_update`, plus existing `test_flush_and_finish_never_raise_whatever_the_write_raises` and `test_session_entry_points_never_raise_from_a_broken_summary` |
| G1 | Does not touch `lib/dashboard_page.py`; existing tests pass unchanged | Verify V1 and V4 (no test edits outside the new tests) |

## Verify

- [x] **V1 Unit.** `env -u ORCH_DB python -m pytest -q` passes in full. Record the counts in the PR.
- [x] **V2 Revert.** Restore `lib/telemetry.py` from `main` and keep the new tests. Every new test fails, and the reason is the
  missing capture or column, not an import error. Record this in the PR.
- [x] **V3 Old database.** In a throwaway copy, create a #93-shaped `job_telemetry` with one existing row, run
  `orch job run` with a fake stream, and confirm that the row is rewritten with the new columns and that the dashboard's current
  `read_telemetry` still renders it.
- [ ] **V4 CI.** `.github/workflows/ci.yml` is green on the PR head (pytest, `check-config`, statechart). `git diff main --stat`
  shows no change to `lib/dashboard*.py` or to existing test bodies.

## Risks

- **Stream shape drift.** Claude Code changes tool names (`Task` became `Agent`) and event shapes. Unknown shapes are ignored,
  never errors, and the fixtures pin the version they came from.
- **Text reversal scope creep.** The never-read list in W3 is enforced by a sentinel test, not by review alone.
- **Migration race** between the box's runners. Handled by the duplicate-column tolerance in W1, with a test.
