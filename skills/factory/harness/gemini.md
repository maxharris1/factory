# pstack in Antigravity `agy`

pstack skills use Cursor names, and pstack ships no Antigravity `agy` adapter. This file is the factory's own, in the shape of `<pstack>/docs/harness/claude-code.md`. A factory job runs `agy -p` headless with `--dangerously-skip-permissions`. `orch job run` adds `--output-format stream-json`, and the role router adds `--model` and `--effort`. The account is each device's `gemini-bot` login, with no account folder, and `agy models` is the doctor's sign-in check. In `agy -p`, read this file once per session, then map each Cursor name in a skill with the tables below. `<pstack>` means the folder that holds `skills/` and `docs/` inside the deployed `factory_root`. If a Cursor field has no row or the CLI tool schema rejects it, drop it and say so. Never pass a field or value the tool schema rejects.

## Tools

| Cursor | Antigravity (`agy -p`) |
| --- | --- |
| `Task` | Use the subagent tool if exposed in the session schema. If the CLI run has no subagent tool, perform the bounded check directly in the main session and note it in your summary. |
| `AskQuestion` | None. Headless `agy -p` has no live user. Post a `questions` handoff as the brief says, then stop. |
| Open a todolist | Keep a short phase checklist in your reply and update it at each phase. |
| Run another pstack skill (`/name` or "the **name** skill") | Read `<pstack>/skills/<name>/SKILL.md` with `read_file` or the shell and follow it. Do not wait for `agy` to load it on its own. |
| `Grep`, `Glob` | Built-in search/glob tools or the shell tool with `rg`. |

## Subagent fields

| Cursor | Antigravity (`agy -p`) |
| --- | --- |
| `subagent_type: generalPurpose` | Use the default helper agent if subagents are available; otherwise execute the bounded step directly. |
| `poteto-agent`, `comment-sicko` | Start a general helper (or run a dedicated pass) that first reads and follows `<pstack>/agents/poteto-agent.md` or `<pstack>/agents/comment-sicko.md`. |
| `readonly: true` | State "do not edit files" in the helper brief and check `git status` afterward. |
| `readonly: false` | Bounded code change in the job checkout. |
| `environment: "cloud"` | A separate `git worktree add` path per worker on the local host. Name the path and branch or commit in the brief. |
| `environment: "local"` | Omit. |
| `cloud_base_branch: <b>` | Tell the worker to check out `<b>` first. |
| `run_in_background: true` | Run sequentially when background helpers are unavailable. |
| `resume` | Start a new helper and paste the earlier result into its brief. |
| `file_attachments` | List the file paths in the brief. |

## Models

| Cursor | Antigravity (`agy -p`) |
| --- | --- |
| A `gemini-*` slug | Pass `model` only when the helper tool schema lists it; otherwise inherit the session model and say so. |
| A `claude-*`, `gpt-*`, `grok-*`, or other non-Google slug | Omit `model` so the helper runs on the session model, and say so. |
| `auto` or `inherit-parent` | Omit `model`. |
| The `pstack-models.mdc` rule, `~/.cursor/rules/pstack-models.mdc` | No `agy` file. A factory job takes its model lines from the brief. |

## setup-pstack

A factory job does not run `setup-pstack`. Its model lines come from the brief, and its workspace is the job checkout. Put one line in your reply, `Skipped in Antigravity agy: setup-pstack. The brief carries the model lines.`, and use the Models rows above.

## Paths

| Cursor | Antigravity (`agy -p`) |
| --- | --- |
| `.cursor/skills/`, `~/.cursor/skills/` | `.gemini/skills/`, `~/.gemini/skills/` (installed by `orch install-skills`). Always read the exact pinned paths under `<factory_root>` named in the brief, never copies inside the job's working directory. |
| `.cursor/rules/*.mdc`, always-applied rules | `GEMINI.md` or `AGENTS.md`. `agy` never reads `.mdc` files. |
| `https://github.com/cursor/plugins/blob/main/pstack/<path>` | `<pstack>/<path>` when that file exists. Read the local copy and never fetch it. |

## Cursor-only features

Antigravity `agy` has no Cursor Automations, Bugbot, Cursor cloud agents, or built-in PR tool. When a step needs one, put one line in your reply, `Skipped in Antigravity agy: <step>. <reason>.`, then continue to the next step without stopping, asking, or retrying.

| Cursor | Antigravity (`agy -p`) |
| --- | --- |
| Cursor Automations (`api2.cursor.sh/automations`, routines, `update_state`, `SendToUser`) | Skip with the line. |
| Bugbot | Triage existing Bugbot PR threads via `gh` as ordinary review comments. Skip steps that start, wait for, or configure Bugbot when no Bugbot check exists. |
| Cursor cloud agents | `environment: "cloud"` maps to a local git worktree as above. Skip steps that need the cloud agent dashboard. |
| A built-in PR tool | Use the `gh` commands the brief gives. That is not a skip, so write no line. |
