# Comment for PR #100: data for the "Live run" panel

Paste the block below as a comment on PR #100. It asks for scoping only.

---

## Follow-up to scope: "Live run" data for the ticket page

**Ask:** scope follow-up tickets (do not start building) that extend job telemetry
so the dashboard can show what an agent says it is working on. Reply with a ticket
list: for each, acceptance criteria, files touched, risks, and dependencies.

### What the UI needs, per running job
1. **Current task**: one short line, the item the agent has in progress.
2. **Plan**: the agent's own checklist, up to 12 items, each `text` + `status`
   (`pending | in_progress | done`).
3. **Last finished task**: one short line.
4. **Sub-agents**: up to 8, each `kind`, `description`, `status`
   (`running | finished | failed`), `started_at`, `finished_at`.
5. Already in #100, keep as is: `tokens_in`, `tokens_cached`, `tokens_out`,
   `cost_usd`, `files_touched`, `commands`.

The UI no longer needs the step feed or the per-tool counts.

### Where the data is
Both are already in the stream `lib/telemetry.py` reads; today they are dropped.
- **Codex** (`--json`): `todo_list` items, skipped in `_codex_start`
  ("agent_message, reasoning, todo_list: not steps").
- **Claude Code** (`stream-json`): the task-list tool call and the sub-agent tool
  call arrive as `tool_use` blocks in `_claude_tool`, where only the tool name is
  kept. Confirm the exact tool names and input fields from a real `events.jsonl`
  on the current CLI version; do not rely on names from memory.
- Sub-agent finish: the matching tool result. Confirm how Codex reports
  sub-agents, if it does at all; "not available for codex" is an acceptable answer.

### Constraints
- **This reverses part of r10** ("no command line, tool argument, pattern, path,
  query or error message is ever stored"). Scope it narrowly: task-list text and
  sub-agent `description` + type only. Never the sub-agent prompt, never any
  result or assistant text, never command lines or paths.
- Every stored string goes through `Scrubber` and the dashboard `redact`, is cut
  to one line, and is length-capped (propose a cap; `LABEL_LIMIT` is 40 today and
  is too short for a task line).
- Add a setting that keeps the counters but stores no text, so the r10 behaviour
  stays available. `ORCH_TELEMETRY=off` must still run the command unchanged.
- Same failure rule as #100: a telemetry error is one log line and never stops
  the job or its heartbeat. The raw log still never leaves the machine.
- Written on the existing heartbeat flush (~15 s) and at exit. No new cadence.

### Questions to answer in the scoping reply
- Schema: new columns on `job_telemetry`, or one JSON column each for plan and
  sub-agents?
- How is "current task" derived when a harness reports no task list?
- What does the dashboard snapshot key look like, and what changes in
  `tests/test_dashboard.py`?
- Which fixtures are needed for both CLIs, including a run with sub-agents?

### Suggested split (adjust as needed)
1. Runner: capture plan and sub-agents, schema, scrubbing, the counters-only setting.
2. Dashboard data: read, redact, attach to the job payload.
3. UI: the Live run panel on the ticket page (depends on the dashboard refactor).
4. Fixtures and tests from real streams for both harnesses.
