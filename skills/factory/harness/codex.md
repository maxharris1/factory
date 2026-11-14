# pstack in Codex

pstack skills use Cursor names, and pstack ships no Codex adapter. This file is the factory's own, in the shape of `<pstack>/docs/harness/claude-code.md`. In Codex, read this file once per session, then map each Cursor name in a skill with the tables below. `<pstack>` means the folder that holds `skills/` and `docs/`. If a Cursor field has no row, drop it and say so. Never pass a field or value the tool schema rejects. Rows are checked against Codex CLI 0.166.0 (`codex --version`, `codex exec --help`, `codex features list`). A newer CLI can differ, so trust its tool schema over this file.

## Tools

| Cursor | Codex |
| --- | --- |
| `Task` | `spawn_agent` (the `multi_agent` feature, on by default). Collect results with `wait_agent`, then `close_agent`. Codex spawns only when instructions ask for it, and a pstack step that names a subagent is that ask. |
| `AskQuestion` | None. `codex exec` refuses `request_user_input` and has no live user. Post a `questions` handoff as the brief says, then stop. |
| Open a todolist | `update_plan`. Update it at each phase. |
| Run another pstack skill (`/name` or "the **name** skill") | Read `<pstack>/skills/<name>/SKILL.md` with the shell and follow it. Do not wait for Codex to load it on its own. |
| `Grep`, `Glob` | The shell tool with `rg`. |

## Subagent fields

| Cursor | Codex |
| --- | --- |
| `subagent_type: generalPurpose` | `agent_type: "default"` |
| `poteto-agent`, `comment-sicko` | `agent_type: "default"`. The brief's first line says to read and follow `<pstack>/agents/poteto-agent.md` or `<pstack>/agents/comment-sicko.md`. |
| `readonly: true` | `agent_type: "explorer"`. It can still run commands, so the brief also says "do not edit files", and you check `git status` afterward. |
| `readonly: false` | `agent_type: "worker"` for a bounded code change. `default` otherwise. |
| `environment: "cloud"` | A separate `git worktree add` path per worker. Name the path and the branch or commit in the brief. |
| `environment: "local"` | Omit. |
| `cloud_base_branch: <b>` | Tell the worker to check out `<b>` first. |
| `run_in_background: true` | Omit. `spawn_agent` returns at once. Wait with `wait_agent`. |
| `resume` | Spawn a new agent and paste the earlier result into its brief. |
| `file_attachments` | List the paths in the brief. |

## Models

| Cursor | Codex |
| --- | --- |
| A `gpt-*` slug | `model` on `spawn_agent` only when the schema lists that slug as an override. Otherwise omit `model` and say so. |
| `claude-<family>-<version>-<effort>[-fast]`, a `grok-*` slug, or any other non-OpenAI slug | Omit `model` so the agent runs on the session model, and say so. |
| `auto` or `inherit-parent` | Omit `model`. |
| The `pstack-models.mdc` rule, `~/.cursor/rules/pstack-models.mdc` | No Codex file. A factory job takes its model lines from the brief. |
| A role value `<model> <effort>` | `model` and the reasoning effort that `spawn_agent` lists for that model. |

## setup-pstack

A factory job does not run `setup-pstack`. Its model lines come from the brief, and its sandbox (`codex exec --sandbox workspace-write`) is the job's checkout. Put one line in your reply, `Skipped in Codex: setup-pstack. The brief carries the model lines.`, and use the Models rows above.

## Paths

| Cursor | Codex |
| --- | --- |
| `.cursor/skills/`, `~/.cursor/skills/` | `.agents/skills/`, `~/.agents/skills/`. `~/.codex/skills` is the deprecated alias. |
| `~/.cursor/plugins/`, `.cursor-plugin/plugin.json` | No row. The factory installs pstack as skills, not as a Codex plugin. |
| `.cursor/rules/*.mdc`, always-applied rules | `AGENTS.md`. Codex never reads `.mdc` files. |
| `https://github.com/cursor/plugins/blob/main/pstack/<path>` | `<pstack>/<path>` when that file exists. Read the local copy and never fetch it. Give the user the URL when they need a link. |
| Transcripts in `~/.cursor/projects/<slug>/agent-transcripts/` | `~/.codex/sessions/`, one `rollout-*.jsonl` per session in dated folders. Read only the current job's session. |

## Cursor-only features

Codex has no Cursor Automations, Bugbot, Cursor cloud agents, or built-in PR tool. When a step needs one, put one line in your reply, `Skipped in Codex: <step>. <reason>.`, then go on to the next step. Do not stop, ask, or retry. When a later step needs what the skipped step made, such as a URL or a key, leave a placeholder and name it in the line.

| Cursor | Codex |
| --- | --- |
| Cursor Automations: routines, `update_state` routine calls, `api2.cursor.sh/automations` webhooks, routine wakes, and `SendToUser` secret requests | Skip with the line. |
| Bugbot | Bugbot comments on a GitHub PR are ordinary review threads, so triage them with the forge command that the step gives. Skip a step that starts, waits for, or configures Bugbot when the PR has no Bugbot check or comment. |
| Cursor cloud agents | `environment: "cloud"` maps to a worktree, as in Subagent fields. Skip a step that needs the cloud agent itself, such as its dashboard status or its own machine. |
| A built-in PR tool | Use the forge command that the step gives, run as the brief says (`env -u GH_TOKEN gh`). That is not a skip, so write no line. |
