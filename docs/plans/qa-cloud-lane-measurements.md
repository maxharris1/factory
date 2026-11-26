# QA: cloud lane measurements (#780)

Operator task #780, recorded by Grok Bot (Factory) on 2026-11-25 and extended by Claude Bot the same night. Times below are America/New_York (ET). This file is what #785 (the Claude cloud runner) and #786 (the Cursor cloud runner) should build to. No secret value was read or recorded.

Sources:

- **Claude, first run:** Claude Bot, on `pc-wsl-claude-bot`, from a clean `maxharris1/factory` `main` checkout at `01a2f63cd7c5a1dead2c82efe0ac40c115080f99`, 8:40 PM to 8:54 PM ET. The temporary checkout was removed afterward. No commits, PRs, or comments were made from that session.
- **Claude, second run (fresh session for the connector check):** Claude Bot, same host, local CLI `2.1.322`, from a clean `main` checkout at `3789cd3ec3921d37478e4443d289e5b5c1063eb0`, 10:38 PM to 10:43 PM ET. Same cleanup.
- **Cursor:** Cursor Bot, about 8:39 PM ET, through Grok Bot's built-in cloud-agent launcher, and Claude Bot, 10:44 PM ET, calling `https://api.cursor.com/v1/agents` directly without a key. No Cursor API key exists on the factory's host yet, so every REST call stops at authentication. The failing step is recorded per call below.

## Scripted launch: Claude

Host: `ssh pc-wsl-claude-bot` (user `claude-bot`). Working directory for every launch: a throwaway clone of `maxharris1/factory` on `main`. The `--cloud` argument / task used in the first run:

```text
List the repo's top-level files. Read-only: do not edit, commit, push or open PRs.
```

Every command below ran with the host's default Claude Code config (`~/.claude`, no `CLAUDE_CONFIG_DIR`). Under the factory's per-account `CLAUDE_CONFIG_DIR`, interactive `claude` opened first-run onboarding (the theme picker) instead of the trust prompt, so the runner for #785 has to pick a config directory that has completed onboarding.

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

`timeout` supplies the deadline and `script` supplies the TTY. Run from a checkout whose folder Claude Code has not yet been told to trust. This is the second run's command, at 10:44 PM ET against a new untrusted checkout; the first run (8:40 PM ET) kept only its output files, which show the same exit code, the same `script` termination line and the same dialog:

```bash
timeout 180s script -qec 'claude --cloud "List the repo'\''s top-level files. Read-only: do not edit, commit, push or open PRs."' /tmp/qa780b/untrusted.typescript > /tmp/qa780b/untrusted.out 2> /tmp/qa780b/untrusted.err
```

- Exit code: `124` (killed by `timeout` at 180s: started 10:44:03 PM ET, killed 10:47:05 PM ET. The typescript trailer still says `COMMAND_EXIT_CODE="0"`, so read the exit code from `timeout`, not from `script`)
- stderr: `Session terminated, killing shell... ...killed.` (written by `script` when `timeout` sent it SIGTERM)
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

### Accepting folder trust

Folder trust is a local Claude Code write for one folder path: no account, environment, connector, or spend change. It was accepted from a `tmux` pane so the keystrokes could be scripted. The second run used plain `claude` for this step so that accepting trust could not itself create a cloud session:

```bash
tmux new-session -d -s qa780-trust -x 140 -y 50 -c "$WORKDIR" "claude"
sleep 8
tmux send-keys -t qa780-trust Down
tmux send-keys -t qa780-trust Enter
sleep 8
tmux send-keys -t qa780-trust "/exit" Enter
```

`Down` moves the selection from `No, exit` to `Yes, I trust this folder`. The pane showed the trust dialog after the first sleep and the normal prompt after the second, and `claude` exited `0` on `/exit`. The first run accepted trust the same way but inside `claude --cloud "<task>"`; that process exited `0` with a blank pane, so whether it also created a session was not captured. Prefer the plain `claude` form.

### Successful launch after pre-accepting folder trust

```bash
timeout 180s script -qec 'claude --cloud "List the repo'\''s top-level files. Read-only: do not edit, commit, push or open PRs."' /tmp/qa780-launch-trusted.typescript
```

- Exit code: `0`
- Started: 8:50:05 PM ET; finished: 8:50:14 PM ET (**about 9s**)
- stdout (typescript, ANSI stripped):

```text
Created cloud session: Repository top-level files
View: https://claude.ai/code/session_01Fy3jwSBohiKjUVSCVbDbid?from=cli&m=0
Resume with: claude --teleport session_01Fy3jwSBohiKjUVSCVbDbid
```

The CLI creates the session, prints the ID and URL, and exits. It does not stream the session's work. After `claude --teleport` and `/export`, the conversation showed a top-level listing of this SHA (no edits, commits, or pushes; the local checkout stayed clean).

### Second launch: fresh session for the connector check

Same recipe, run at 10:40 PM ET from the second checkout after its trust was accepted. The task was the inventory itself, so the tool list in Connectors below is the session's reply to its launch prompt:

```bash
TASK='Read-only inventory. Change nothing: do not edit, commit, push or open PRs. 1. List your exact tool names verbatim: core tools, deferred tools, and each MCP server with its tool names. 2. Load ListConnectors through ToolSearch, call it, and paste its exact result verbatim. 3. Say whether any claude.ai account connector (for example Gmail, Slack, Linear) is available in this session.'
timeout 180s script -qec "claude --cloud \"$TASK\"" /tmp/qa780b/launch.typescript
```

- Exit code: `0`
- Started: 10:40:06 PM ET; finished: 10:40:15 PM ET (**9s**)
- stdout (typescript, ANSI stripped):

```text
Created cloud session: Inventory of tools and connectors
View: https://claude.ai/code/session_01Fy3jwPozECzNiFyuy5Z44N?from=cli&m=0
Resume with: claude --teleport session_01Fy3jwPozECzNiFyuy5Z44N
```

**What a scripted Claude launch therefore needs:** a PTY wrapper such as `script -qec '<cmd>' <typescript>`, an outer `timeout` so a trust prompt or hang cannot block the caller, and folder trust already accepted for the checkout. `-p` cannot be used to start the session.

### Stop

```bash
timeout 60s claude -p "stop" --cloud session_01Fy3jwSBohiKjUVSCVbDbid --output-format json
```

Run at 8:54 PM ET from the first checkout.

- Exit code: `0`
- stderr: empty
- stdout (full JSON):

```json
{"ok":true,"session_id":"session_01Fy3jwSBohiKjUVSCVbDbid","url":"https://claude.ai/code/session_01Fy3jwSBohiKjUVSCVbDbid?from=cli&m=0"}
```

The same command against the second session at 10:42 PM ET also exited `0` with the matching `{"ok":true,...}` body, in 5s of wall clock. Its stderr was `Warning: no stdin data received in 3s, proceeding without it. If piping from a slow command, redirect stdin explicitly: < /dev/null to skip, or wait longer.`, so a scripted stop should add `< /dev/null`.

This is fire-and-forget: the CLI queues the message and returns at once. It does not wait for the model to reply, and it does not report that the session has actually stopped. `--output-format stream-json` is rejected with `--cloud <session-id>` (stderr: `Error: --cloud <session_id> does not support --output-format stream-json`). `claude stop` / `claude kill` in `--help` are for local `--bg` sessions, not cloud sessions, and were not used.

## Scripted launch: Cursor

Two interfaces were exercised. The REST API, which #786 is specified against, has no key on the factory's host, so each of its four calls is recorded with the exact command and the authentication failure it returns. The built-in launcher substitute follows, labeled as such.

### REST API without a key: every call stops at authentication

Key check, by name only: `~/.config/factory/qa-cloud.env` does not exist on `pc-wsl-claude-bot`, and no environment variable whose name starts with `CURSOR` is set in the bot's shell. Max provisions the key and places the value himself. The commands below ran at 10:44 PM ET with no `Authorization` header and no `-u` credential, so the recorded result is the API's answer to a missing key.

Cursor's API documentation (https://cursor.com/docs/cloud-agent/api/endpoints, read 2026-11-25, not observed from a response) says: Basic (`-u "$CURSOR_API_KEY:"`) or Bearer auth; `env` (`type: cloud` plus `name`) is mutually exclusive with explicit `repos`; the launch response is `{agent, run}` with the run `id` the cancel path needs; `GET /v1/agents/{id}` carries the agent's `ACTIVE`/`IDLE`/`ARCHIVED` status and `latestRunId`, while run status (`CREATING`, `RUNNING`, `WAITING_FOR_BACKGROUND_WORK`, `FINISHED`, `ERROR`, `CANCELLED`, `EXPIRED`) is read from `GET /v1/agents/{id}/runs/{runId}`; cancelling a terminal run returns `409 run_not_cancellable`.

#### 1. `POST /v1/agents`, named-environment form

```bash
curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' --max-time 30 -X POST https://api.cursor.com/v1/agents \
  -H 'Content-Type: application/json' \
  -d '{"prompt":{"text":"List the repo top-level files. Read-only: do not edit, commit, push or open PRs."},"env":{"type":"cloud","name":"factory"},"autoCreatePR":false}'
```

```text
{"code":"error","message":"Invalid User API Key"}
HTTP 401 in 0.118985s
```

#### 2. `POST /v1/agents`, explicit-repo form

```bash
curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' --max-time 30 -X POST https://api.cursor.com/v1/agents \
  -H 'Content-Type: application/json' \
  -d '{"prompt":{"text":"List the repo top-level files. Read-only: do not edit, commit, push or open PRs."},"repos":[{"url":"https://github.com/maxharris1/factory","startingRef":"main"}],"autoCreatePR":false}'
```

```text
{"code":"error","message":"Invalid User API Key"}
HTTP 401 in 0.114437s
```

#### 3. `GET /v1/agents/{id}`, status read

The id is the substitute launcher's agent, the only Cursor agent id the factory has.

```bash
curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' --max-time 30 \
  https://api.cursor.com/v1/agents/bc-32fa32e9-5b73-5193-9636-7abc8e932e9a
```

```text
{"code":"error","message":"Invalid User API Key"}
HTTP 401 in 0.082919s
```

#### 4. `POST /v1/agents/{id}/runs/{runId}/cancel`

The substitute launcher exposes no run id, so `run-unknown` stands in. With a key, the run id comes from the launch response's `run.id` or the status read's `latestRunId`.

```bash
curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' --max-time 30 -X POST \
  https://api.cursor.com/v1/agents/bc-32fa32e9-5b73-5193-9636-7abc8e932e9a/runs/run-unknown/cancel
```

```text
{"code":"error","message":"Invalid User API Key"}
HTTP 401 in 0.080446s
```

#### What the four calls establish

- Authentication is checked before the body or the path: all four calls, including a malformed run id, return the same `401` and `{"code":"error","message":"Invalid User API Key"}`. A missing key is therefore a launch failure that #786 can detect from the status code alone, without parsing the body.
- Which launch form the factory's account accepts is **unverified**: the failing step is the missing key, before the server reads `env` or `repos`. Both forms above are the exact requests to replay with `-u "$CURSOR_API_KEY:"` once Max has placed the key, and the first one that returns `200` decides #786's first criterion.
- The API answers in about 0.1s from this host, so the 30s `--max-time` is generous.

### Substitute: Grok Bot's built-in cloud-agent launcher

**Not the REST API.** These measurements came from Grok Bot's built-in cloud-agent launcher (via Cursor Bot). No Cursor API key was used or touched. The launcher does not expose HTTP status codes or raw request/response payloads, so this section cannot stand in for the API measurements #786 needs.

Run: about 8:39 PM ET on 2026-11-25. Agent id: `bc-32fa32e9-5b73-5193-9636-7abc8e932e9a`. Only the **explicit-repo** form was tried (`https://github.com/maxharris1/factory`, `starting_ref` `main`, model `grok-4.7`, `fast` false). The prompt was read-only: list files, print the head of the README, and make no edits, commits, or PRs. Timings are coarse box wall-clock between tool calls, including bookkeeping, not server-side numbers. Treat them as order-of-magnitude.

- **Launch** returned OK: the agent id above, a cursor.com URL, and status `running`. No HTTP status code or raw payload is exposed by this launcher.
- **Status read** shortly after launch returned `running`, about **12s** after the pre-launch clock mark. That 12s includes Cursor Bot's own bookkeeping, so it is an upper bound on the launch round trip.
- **Cancel** returned `Requested cancellation of the active run`. The status get plus the cancel took about **4.2s** of box wall-clock together. A second get right after showed `finished`. This interface has no separate `cancelled` state, so a cancel and a normal completion look the same at this level. The launcher has no `runId`; cancel acts on the agent's active run.
- The launcher has no `autoCreatePR` switch. Its launch notice says the agent will open a PR when done, so "do not open a PR" was enforced only by the prompt. The run was cancelled within about 15s, before any PR could be opened.
- The **named-environment** form was not tried here: no factory environment exists in this launcher, and creating one was out of scope.

## Connectors

Two sessions. The first (`session_01Fy3jwSBohiKjUVSCVbDbid`) self-reported its tool list in reply to a queued message and did not run `ListConnectors`. The second (`session_01Fy3jwPozECzNiFyuy5Z44N`) was launched fresh with the inventory as its task and ran `ListConnectors`. Both lists were read back with `claude --teleport <id>` in a `tmux` pane followed by `/export <file>`; the lists are identical.

**How the lists were obtained (session replies, not an init event).** A `stream-json` `system/init` tool/MCP schema could not be captured: a new `--cloud` session cannot be started with `-p`, and messaging an existing session with `--output-format stream-json` exits `1`:

```bash
timeout 180s claude -p "Print your exact available tool names and MCP servers/connectors verbatim. Change nothing; this is a read-only inventory." --cloud session_01Fy3jwSBohiKjUVSCVbDbid --output-format stream-json
```

- Exit code: `1`
- stderr: `Error: --cloud <session_id> does not support --output-format stream-json`

The first run's inventory was then queued with `--output-format json`, which returned `{"ok":true,...}` at once (exit `0`, 8:50 PM ET), and the reply was read back through teleport:

```bash
timeout 180s claude -p "Print your exact available tool names and MCP servers/connectors verbatim. Change nothing; this is a read-only inventory." --cloud session_01Fy3jwSBohiKjUVSCVbDbid --output-format json
```

The second run put the inventory in the launch task itself (command under "Second launch" above), so no follow-up message was needed.

**Repo observation, separate from the sessions' replies:** at both SHAs, `maxharris1/factory` has no `.mcp.json` and no `.claude/` directory, so the cloud session would not load project-scoped MCP servers or repo Claude settings from the clone.

### Core tools (21, callable immediately)

Agent, Artifact, AskUserQuestion, Bash, Edit, Glob, Grep, ListAgents, Read, ReadNotifications, ReportFindings, ScheduleWakeup, SearchPlugins, SendUserFile, ShowOnboardingRolePicker, Skill, SuggestPluginInstall, SuggestSkills, ToolSearch, Workflow, Write.

### Deferred tools (29, names known; must be loaded through ToolSearch before call)

ArtifactComments, ArtifactData, CronCreate, CronDelete, CronList, DesignSync, EnterPlanMode, EnterWorktree, ExitPlanMode, ExitWorktree, ListConnectors, ListPlugins, ListSkills, McpTaskCheck, McpTaskResumePolling, McpTaskShowQuestion, Monitor, NotebookEdit, PushNotification, SearchMcpRegistry, SearchSkills, SendMessage, SuggestConnectors, TaskCreate, TaskGet, TaskList, TaskStop, TaskUpdate, WebSearch.

### MCP servers

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

Observed in the fresh second session, which loaded `ListConnectors` through `ToolSearch` and called it as its launch task asked. Its exported reply, verbatim:

```text
2. ListConnectors result (verbatim)

[]

3. claude.ai account connectors

None are available in this session. ListConnectors returned an empty list, and
there are no Gmail, Slack, Linear or other connector tools (no
mcp__<connector>__* tools). The only MCP server is claude-code-remote, which
is the session's own infrastructure, not an account connector.
```

So a Claude cloud session **can be started without the account's connectors**: this one was, with no flag or setting on the launch command, and connector discovery inside it found none. The first session's reply agreed (no connector tools in its list) but had not run `ListConnectors`, which is why it alone could not support the conclusion.

What was not verified: whether the launching account has connectors configured in claude.ai at all, and whether an org setting could deliver them to a cloud session. Anthropic's MCP documentation (https://code.claude.com/docs/en/mcp, section "How connectors reach Claude Code", read 2026-11-25) says cloud sessions do not fetch connectors themselves: "The cloud host passes them in", governed by "Your claude.ai organization settings, plus the allowlist and denylist settings that reach the session and any `managed-mcp.json` on the host that runs it". It also says `disableClaudeAiConnectors`, `ENABLE_CLAUDEAI_MCP_SERVERS` and `allowAllClaudeAiMcps` "act only on" the connectors Claude Code fetches itself, so no CLI flag or setting turns host-delivered connectors off; `deniedMcpServers` filters them by name or URL pattern. The runner for #785 should therefore treat "no connectors" as the observed default and `ListConnectors` as the check, not rely on a launch-time switch.

## Findings for #785 and #786

- **#785 (Claude cloud runner):** a scripted launch needs a PTY wrapper, an outer `timeout`, pre-accepted folder trust for the checkout, and a Claude config directory that has completed onboarding. `claude --cloud` with no TTY exits `1`; `-p --cloud` cannot start a new session; an untrusted folder blocks on the trust dialog until the timeout kills it (exit `124`). After trust is in place, `timeout 180s script -qec 'claude --cloud "<task>"' <typescript>` returns a session ID and URL in about 9s and exits, measured twice. Stop is `timeout 60s claude -p "stop" --cloud <id> --output-format json < /dev/null`, which is fire-and-forget: it queues and returns `{ok:true,...}` immediately, and does not wait for a reply or confirm that the session has stopped. A fresh session starts with no claude.ai connectors (`ListConnectors` returned `[]`) and one MCP server, `claude-code-remote`. Tool lists in this file are session replies read through teleport and `/export`, not an observed init schema.
- **#786 (Cursor cloud runner):** the four REST calls are recorded above with their exact `curl` commands. Without a key every one returns `401 {"code":"error","message":"Invalid User API Key"}` in about 0.1s, so a missing or rejected key is detectable from the status code. Which launch form the account accepts (named environment or explicit repo) is unverified until Max places a Cursor API key; replay the two `POST` commands with `-u "$CURSOR_API_KEY:"` and build to the one that returns `200`. The substitute launcher results are a different interface (no HTTP codes, no `autoCreatePR`, no `runId`, no named-environment form, cancel and completion share `finished`). Do not build the runner to the substitute.
