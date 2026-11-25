# pstack in Cursor CLI

pstack skills use Cursor names, and `cursor-bot` runs headless through `cursor-agent -p` in a factory checkout rather than the interactive Cursor IDE. Read this file once per session, then apply the rules below alongside the pinned pstack router and skills. `<pstack>` means the folder that holds `skills/` and `docs/` inside the deployed `factory_root`. If a Cursor field has no row or the CLI tool schema rejects it, drop it and say so. Never pass a field or value the tool schema rejects.

## Tools

| Cursor (pstack skill) | Cursor CLI (`cursor-agent -p`) |
| --- | --- |
| `Task` | Use the subagent / task tool if exposed in the session schema. If the CLI run has no subagent tool, perform the bounded check directly in the main session and say so in your summary. |
| `AskQuestion` | None. Headless `cursor-agent -p` has no live user. Post a `questions` handoff as the brief says, then stop. |
| Open a todolist | Use the session todo tool if exposed, otherwise keep a short phase checklist in your reply and update it at each phase. |
| Run another pstack skill (`/name` or "the **name** skill") | Read `<pstack>/skills/<name>/SKILL.md` with the file-read tool or shell and follow it. Do not wait for auto-discovery to load it. |
| `Grep`, `Glob` | Use the session search/glob tools, or the shell tool with `rg`. |

## Subagent fields

| Cursor (pstack skill) | Cursor CLI (`cursor-agent -p`) |
| --- | --- |
| `subagent_type: generalPurpose` | `subagent_type: "generalPurpose"` (or omit if the schema uses a single default helper type). |
| `poteto-agent`, `comment-sicko` | Start a `generalPurpose` helper and tell it first to read and follow `<pstack>/agents/poteto-agent.md` or `<pstack>/agents/comment-sicko.md`. |
| `readonly: true` | Pass `readonly: true` if supported, and state "do not edit files" in the helper brief; verify `git status` afterward. |
| `readonly: false` | Pass `readonly: false` (or omit when default). |
| `environment: "cloud"` | A separate `git worktree add` path per worker on the local host. Name the path and the branch or commit in the brief. Do not launch Cursor Cloud Agents from a local `cursor-bot` job. |
| `environment: "local"` | Omit. |
| `cloud_base_branch: <b>` | Tell the worker to check out `<b>` first. |
| `run_in_background: true` | Use if supported by the tool schema; otherwise run sequentially and wait for completion before continuing. |
| `resume` | Start a new helper and paste the earlier result into its brief. |
| `file_attachments` | List the file paths in the brief. |

## Models

| Cursor (pstack skill) | Cursor CLI (`cursor-agent -p`) |
| --- | --- |
| The `pstack-models.mdc` rule, `~/.cursor/rules/pstack-models.mdc` | Ignored for factory jobs. The brief's helper model lines win over any local rule file. |
| `inherit-parent` or `auto` | Omit `model` so helpers inherit the job's configured session model. |
| A specific `<model>` or `<model> <effort>` from the brief | Pass only when the CLI subagent schema accepts that model slug; otherwise omit `model` and note the fallback. |

## setup-pstack

A factory job does not run `setup-pstack`. Its model lines come from the brief, and its workspace is the job's checked-out worktree. Put one line in your reply, `Skipped in Cursor CLI: setup-pstack. The brief carries the model lines.`, and use the Models rows above.

## Paths

| Cursor (pstack skill) | Cursor CLI (`cursor-agent -p`) |
| --- | --- |
| `.cursor/skills/`, `~/.cursor/skills/` | Installed by `orch install-skills`. Always read the exact pinned paths under `<factory_root>` named in the brief, never copies inside the job's working directory. |
| `.cursor/rules/*.mdc` | Keep project rules in mind when editing repo files, but brief rules and pinned factory skills take precedence. |
| `https://github.com/cursor/plugins/blob/main/pstack/<path>` | `<pstack>/<path>` when that file exists. Read the local copy and never fetch it over HTTP. |

## Cursor-only features (local CLI vs cloud)

A local `cursor-bot` job runs in `cursor-agent -p` on a fleet host (`pc` or `box`), not in Cursor Automations or the `cursor-cloud` QA runner (`docs/SPEC.md`). When a pstack step calls for an IDE-only or cloud-automation feature, put one line in your reply, `Skipped in Cursor CLI: <step>. <reason>.`, then continue to the next step without stopping or asking.

| Feature | Local Cursor CLI (`cursor-bot`) |
| --- | --- |
| Cursor Automations (`api2.cursor.sh/automations`, routines, `update_state`, `SendToUser`) | Skip with the line. |
| Bugbot | Triage existing Bugbot PR review threads via `gh` as ordinary review comments. Skip steps that start, wait for, or configure Bugbot when no Bugbot check exists. |
| Cursor Cloud Agents (`cursor-cloud` QA runner) | Separate from local `cursor-bot`. Map `environment: "cloud"` to a local git worktree as above; skip steps that poll a Cursor Cloud Agent dashboard. |
| Built-in PR tool | Use `gh` as instructed in the brief. That is not a skip, so write no line. |
