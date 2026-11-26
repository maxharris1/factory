# QA: cloud lane measurements (#780)

Operator task #780, recorded by Grok Bot (Factory) on 2026-11-25. Times below are America/New_York (ET). This file is what #785 (the Claude cloud runner) and #786 (the Cursor cloud runner) should build to. No secret value was read or recorded.

Sources:

- **Claude:** Claude Bot, on `pc-wsl-claude-bot`, Claude Code `2.1.323`, from a clean `maxharris1/factory` `main` checkout at `01a2f63cd7c5a1dead2c82efe0ac40c115080f99`. The launch, stop, and inventory ran the evening of 2026-11-25. The temporary checkout was removed afterward. No commits, PRs, or comments were made from that session.
- **Cursor:** Cursor Bot, about 8:39 PM ET on 2026-11-25. Calling Cursor's Cloud Agents REST API (`https://api.cursor.com/v1/agents`) is blocked until Max provisions a Cursor API key for the factory and places the value himself. The Cursor section is a **substitute** through Grok Bot's built-in cloud-agent launcher, not that API, and is labeled that way throughout.

## Scripted launch: Claude

Host: `ssh pc-wsl-claude-bot` (user `claude-bot`). Working directory for every launch: a throwaway clone of `maxharris1/factory` on `main` at `01a2f63cd7c5a1dead2c82efe0ac40c115080f99`. The `--cloud` argument / task used throughout:

```text
List the repo's top-level files. Read-only: do not edit, commit, push or open PRs.
```

### Plain launch, no TTY: failed

```bash
claude --cloud "List the repo's top-level files. Read-only: do not edit, commit, push or open PRs."
```

- Exit code: `1` (about 1s; did not hang)
- stdout: empty
- stderr (exact):

```text
Error: --cloud requires an interactive terminal.
Non-interactive invocations (piped stdout, --init-only, --sdk-url) run locally and would silently ignore --cloud. Drop --cloud, or run from a TTY.
```

A non-interactive shell cannot start a new Claude cloud session this way. The CLI refuses rather than silently dropping `--cloud` and running locally.

### PTY wrapper, folder not yet trusted: blocked

```bash
script -qec 'claude --cloud "List the repo'\''s top-level files. Read-only: do not edit, commit, push or open PRs."' /dev/null
```

- Exit code: `124` (killed by a 180s timeout)
- Result: `script` supplied a TTY, but the process stopped on the interactive workspace-trust dialog. The default selection was `No, exit`. No session ID or URL was printed.
- Dialog (ANSI stripped): "Accessing workspace" for the throwaway checkout; "Quick safety check: Is this a project you created or one you trust?"; choices `No, exit` (default) and `Yes, I trust this folder`.

### `-p --cloud` for a new session: rejected

`-p` / `--print` is the usual non-interactive form, so it was tried under `script` as well:

```bash
script -qec 'claude -p --output-format stream-json --cloud "List the repo'\''s top-level files. Read-only: do not edit, commit, push or open PRs."' /dev/null
```

- Exit code: `1`
- Message (ANSI stripped, exact):

```text
Error: --cloud cannot be combined with --print.
Starting a new cloud session with --cloud is interactive only: drop --print, or drop --cloud to run locally. To message an existing cloud session instead, pass its ID: `claude -p "message" --cloud <session-id>` (find IDs at claude.ai/code).
```

A new `--cloud` session is interactive-only. `claude -p "message" --cloud <session-id>` only queues a message to a session that already exists.

### Successful launch after pre-accepting folder trust

The workspace-trust prompt for this throwaway checkout was accepted locally (`Yes, I trust this folder`). That is a local Claude Code folder-trust write only: no account, environment, connector, or spend change. Then:

```bash
script -qec 'claude --cloud "List the repo'\''s top-level files. Read-only: do not edit, commit, push or open PRs."' /tmp/qa780-launch-trusted.typescript
```

- Exit code: `0`
- Started: 8:50:05 PM ET; finished: 8:50:14 PM ET (**about 9s**)
- stdout (typescript, ANSI stripped):

```text
Created cloud session: Repository top-level files
View: https://claude.ai/code/session_01Fy3jwSBohiKjUVSCVbDbid?from=cli&m=0
Resume with: claude --teleport session_01Fy3jwSBohiKjUVSCVbDbid
```

| Field | Value |
| --- | --- |
| Session ID | `session_01Fy3jwSBohiKjUVSCVbDbid` |
| URL | https://claude.ai/code/session_01Fy3jwSBohiKjUVSCVbDbid?from=cli&m=0 |
| CLI title | `Repository top-level files` (summarized from the `--cloud` description) |

The CLI creates the session, prints the ID and URL, and exits. It does not stream the session's work. After `claude --teleport` and `/export`, the conversation showed a top-level listing of this SHA (no edits, commits, or pushes; the local checkout stayed clean).

**What a scripted Claude launch therefore needs:** a PTY wrapper such as `script -qec '<cmd>' /dev/null`, plus folder trust already accepted for the checkout. `-p` cannot be used to start the session.

### Stop

```bash
claude -p "stop" --cloud session_01Fy3jwSBohiKjUVSCVbDbid --output-format json
```

Run at 8:54 PM ET from the same checkout.

- Exit code: `0`
- stderr: empty
- stdout (full JSON):

```json
{"ok":true,"session_id":"session_01Fy3jwSBohiKjUVSCVbDbid","url":"https://claude.ai/code/session_01Fy3jwSBohiKjUVSCVbDbid?from=cli&m=0"}
```

This is fire-and-forget: the CLI queues the message and returns at once. It does not wait for the model to reply, and it does not report that the session has actually stopped. `--output-format stream-json` is rejected with `--cloud <session-id>` (stderr: `Error: --cloud <session_id> does not support --output-format stream-json`). `claude stop` / `claude kill` in `--help` are for local `--bg` sessions, not cloud sessions, and were not used.

## Scripted launch: Cursor

**Substitute, not the REST API.** These measurements came from Grok Bot's built-in cloud-agent launcher (via Cursor Bot), **not** from `https://api.cursor.com/v1/agents`. No Cursor API key was used or touched. The launcher does not expose HTTP status codes or raw request/response payloads, so this section cannot stand in for the API measurements #786 needs. Those remain owed; see the list at the end of this section.

Run: about 8:39 PM ET on 2026-11-25. Agent id: `bc-32fa32e9-5b73-5193-9636-7abc8e932e9a`. Only the **explicit-repo** form was tried (`https://github.com/maxharris1/factory`, `starting_ref` `main`, model `grok-4.7`, `fast` false). The prompt was read-only: list files, print the head of the README, and make no edits, commits, or PRs. Timings are coarse box wall-clock between tool calls, including bookkeeping, not server-side numbers. Treat them as order-of-magnitude.

### Launch

Returned OK: the agent id above, a cursor.com URL, and status `running`. No HTTP status code or raw payload is exposed by this launcher.

### Status read

A get shortly after launch returned `running`, about **12s** after the pre-launch clock mark. That 12s includes Cursor Bot's own bookkeeping, so it is an upper bound on the launch round trip.

### Cancel

Cancel returned `Requested cancellation of the active run`. The status get plus the cancel took about **4.2s** of box wall-clock together. A second get right after showed `finished`. This interface has no separate `cancelled` state, so a cancel and a normal completion look the same at this level.

The launcher has no `runId`. Cancel acts on the agent's active run.

### Behavior notes

1. The launcher has no `autoCreatePR` switch. Its launch notice says the agent will open a PR when done, so "do not open a PR" was enforced only by the prompt. The run was cancelled within about 15s, before any PR could be opened.
2. The **named-environment** form was not tried: no factory environment exists in this launcher, and creating one was out of scope.
3. Timings are coarse box-clock values, not server-side measurements.

### REST API measurements still owed

Blocked until Max provisions a Cursor API key for the factory and places the value himself in a private file the script reads (for example a `0600` `~/.config/factory/qa-cloud.env`). Do not commit, print, or log the key. Once that file exists, measure all of the following against `https://api.cursor.com/v1/agents` and add the HTTP status codes, redacted request/response payloads, and latencies here:

1. `POST /v1/agents` with the **named-environment** form.
2. `POST /v1/agents` with the **explicit-repo** form (`repos` URL for `maxharris1/factory`, `startingRef: "main"`, `autoCreatePR: false`).
3. `GET /v1/agents/{id}` (status read).
4. `POST /v1/agents/{id}/runs/{runId}/cancel`.

Until those land, #786 cannot build to observed REST behavior.

## Connectors

Applies to the Claude cloud session `session_01Fy3jwSBohiKjUVSCVbDbid` above.

**How the list was obtained (self-reported, not an init event).** A `stream-json` `system/init` tool/MCP schema could not be captured: a new `--cloud` session cannot be started with `-p`, and `claude -p ... --cloud <id> --output-format stream-json` exits `1` (`--cloud <session_id> does not support --output-format stream-json`). The inventory prompt was instead queued with `--output-format json`, which returned `{ok:true,...}` immediately. The reply was read back with `claude --teleport session_01Fy3jwSBohiKjUVSCVbDbid` and `/export`. `ListConnectors` was named as a deferred tool but was **not** run.

**Repo observation, separate from the self-report:** at this SHA, `maxharris1/factory` has no `.mcp.json` and no `.claude/` directory, so the cloud session would not load project-scoped MCP servers or repo Claude settings from the clone.

### Self-reported core tools (21, callable immediately)

Agent, Artifact, AskUserQuestion, Bash, Edit, Glob, Grep, ListAgents, Read, ReadNotifications, ReportFindings, ScheduleWakeup, SearchPlugins, SendUserFile, ShowOnboardingRolePicker, Skill, SuggestPluginInstall, SuggestSkills, ToolSearch, Workflow, Write.

### Self-reported deferred tools (29, names known; must be loaded through ToolSearch before call)

ArtifactComments, ArtifactData, CronCreate, CronDelete, CronList, DesignSync, EnterPlanMode, EnterWorktree, ExitPlanMode, ExitWorktree, ListConnectors, ListPlugins, ListSkills, McpTaskCheck, McpTaskResumePolling, McpTaskShowQuestion, Monitor, NotebookEdit, PushNotification, SearchMcpRegistry, SearchSkills, SendMessage, SuggestConnectors, TaskCreate, TaskGet, TaskList, TaskStop, TaskUpdate, WebSearch.

### Self-reported MCP servers

Only one MCP server: `claude-code-remote`, with **35** tools:

- `mcp__claude-code-remote__add_repo`
- `mcp__claude-code-remote__archive_session`
- `mcp__claude-code-remote__confirm_repository_trust`
- `mcp__claude-code-remote__create_environment`
- `mcp__claude-code-remote__create_session`
- `mcp__claude-code-remote__create_trigger`
- `mcp__claude-code-remote__delete_trigger`
- `mcp__claude-code-remote__fire_trigger`
- `mcp__claude-code-remote__get_environment`
- `mcp__claude-code-remote__get_event`
- `mcp__claude-code-remote__get_session`
- `mcp__claude-code-remote__get_trigger`
- `mcp__claude-code-remote__interrupt_session`
- `mcp__claude-code-remote__list_computer_folders`
- `mcp__claude-code-remote__list_environments`
- `mcp__claude-code-remote__list_events`
- `mcp__claude-code-remote__list_repos`
- `mcp__claude-code-remote__list_sessions`
- `mcp__claude-code-remote__list_triggers`
- `mcp__claude-code-remote__read_documentation`
- `mcp__claude-code-remote__register_repo_root`
- `mcp__claude-code-remote__request_computer_folder`
- `mcp__claude-code-remote__request_credential`
- `mcp__claude-code-remote__send_later`
- `mcp__claude-code-remote__send_message`
- `mcp__claude-code-remote__set_session_tags`
- `mcp__claude-code-remote__set_session_title`
- `mcp__claude-code-remote__subscribe_pr_activity`
- `mcp__claude-code-remote__switch_environment`
- `mcp__claude-code-remote__unarchive_session`
- `mcp__claude-code-remote__unsubscribe_pr_activity`
- `mcp__claude-code-remote__unwatch_url`
- `mcp__claude-code-remote__update_environment`
- `mcp__claude-code-remote__update_trigger`
- `mcp__claude-code-remote__watch_url`

### Account connectors

Self-reported: claude.ai account connectors (the session's examples: Gmail, Slack) did **not** appear in the tool list. They look **omitted at start**, not listed as present-but-disabled. The session said it had not run `ListConnectors`. Official docs (not observed from an init event) say cloud sessions do not fetch claude.ai connectors the way a local CLI session does; user-scope servers added with `claude mcp add` on the machine are documented as unavailable in cloud sessions.

## Findings for #785 and #786

- **#785 (Claude cloud runner):** a scripted launch needs a PTY wrapper and pre-accepted folder trust for the checkout. `claude --cloud` with no TTY exits `1`; `-p --cloud` cannot start a new session. After trust is in place, a PTY-wrapped `claude --cloud "<task>"` returns a session ID and URL in about 9s and exits. Stop is `claude -p "stop" --cloud <id> --output-format json`, which is fire-and-forget: it queues and returns `{ok:true,...}` immediately, and does not wait for a reply or confirm that the session has stopped. Tool and connector facts in this file are self-reported via teleport/`/export`, not an observed init schema.
- **#786 (Cursor cloud runner):** this runner is specified to talk to `https://api.cursor.com/v1/agents` over HTTPS. That API is still unmeasured. The substitute launcher results above are a different interface (no HTTP codes, no `autoCreatePR`, no `runId`, no named-environment form, cancel and completion share `finished`). Do not build the runner to the substitute. Re-measure the four REST calls once Max has placed a Cursor API key.
