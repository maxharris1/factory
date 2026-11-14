# Skill loading and helper agents, per device and harness (#119)

This page records how each harness on each device actually takes in a pstack skill, and whether a CLI job can start a helper agent. The evidence comes from the harness's own transcript, never from what the model says. `bin/skill-probe` writes the table below; T06 (router skills) and T09 (brief wiring) build on the style it shows works.

## Status

**Partly measured.** The pc rows below are from a live run on Nov 13, 2026 (7:07 PM ET; the table dates are UTC). The box and Mac rows, and every Codex row, are still to be measured. The #119 fix job that was cleared to run them was placed on the pc. From there the box refuses SSH, the Mac refuses claude-bot's login (no key on the pc), and codex-bot's home is closed to claude-bot. So that job could reach neither device and could not run anything as codex-bot.

Known before any run (checked on Nov 13, 2026):

- `skills/pstack/skills/poteto-mode/SKILL.md` has `disable-model-invocation: true`. The pstack Claude adapter (`skills/pstack/docs/harness/claude-code.md`) says the `Skill` tool refuses most pstack skills and says to `Read` the `SKILL.md` instead. Style `run-as` is therefore expected to score `no` on Claude. The probe decides it.
- pstack is not installed for claude-bot on the Mac: there is no `~/.claude/skills` and no `~/.agents/skills`. The probe records what happens and installs nothing.
- `machines.yml` on `main` has the Mac disabled with placeholder values. The live copy on the box must enable it for both harnesses before the Mac rows can be measured.

What the pc run shows:

- pstack is not installed for claude-bot on the pc either: `~/.claude/skills/poteto-mode/` does not exist. So all three styles score `no` there. `run-as` got "Unknown skill" from the `Skill` tool, `read-path` got "File does not exist" from `Read`, and `slash` did not expand.
- The control rows point `read-path` at the repo's pinned copy with `--skill-md`. With that file present, `read-path` scores `yes` via `Read`. `run-as` and `slash` still score `no`, because they look for an installed skill, not a path.
- Helper agents start on Claude 2.1.322 under the waiter's launch (`claude -p --model opus --effort high --dangerously-skip-permissions`). The `Agent` call of type `general-purpose` returns "Async agent launched successfully". `poteto-agent` is `missing` ("Agent type 'poteto-agent' not found"), as expected until T07.
- A trimmed copy of the control `read-path` transcript is the fixture `tests/fixtures/skill_probe/claude_live_read_path.jsonl`.

## Results

| Date | Device | Harness | Version | Style | Read | Read via | Helper | poteto-agent | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-14 | pc | claude | 2.1.322 | run-as | no |  | yes | missing |  |
| 2026-11-14 | pc | claude | 2.1.322 | read-path | no |  | yes | missing |  |
| 2026-11-14 | pc | claude | 2.1.322 | slash | no |  | yes | missing |  |
| 2026-11-14 | pc | claude | 2.1.322 | run-as | no |  | yes | missing | control: `--skill-md` = repo's pinned copy |
| 2026-11-14 | pc | claude | 2.1.322 | read-path | yes | Read | yes | missing | control: `--skill-md` = repo's pinned copy |
| 2026-11-14 | pc | claude | 2.1.322 | slash | no |  | yes | missing | control: `--skill-md` = repo's pinned copy |
| | pc | codex | | all | | | | | not measured: codex-bot not reachable from the job |
| | box | claude | | all | | | | | not measured: box refused SSH from the pc |
| | box | codex | | all | | | | | not measured: box refused SSH from the pc |
| | mac | claude | | all | | | | | not measured: Mac refused SSH from the pc |
| | mac | codex | | all | | | | | not measured: Mac refused SSH from the pc |

## What each column means

- **Style.** How the prompt opens:
  - `run-as`: "Run as poteto-mode." (today's briefs)
  - `read-path`: "Read `<abs path>/poteto-mode/SKILL.md` and follow it." (the pstack Claude adapter's own instruction). The path is the bot's installed copy on that device: `~/.claude/skills/poteto-mode/SKILL.md` for Claude, `~/.agents/skills/poteto-mode/SKILL.md` for Codex. `--skill-md` overrides it.
  - `slash`: `/poteto-mode` as the first line. Claude only, since `codex exec` has no slash commands.
  - `codex-skill`: `$poteto-mode`, which Codex resolves from `$HOME/.agents/skills`. Codex only.
  - A style that does not apply to a harness shows `n/a`.
- **Read.** `yes` only when the canary appears in a record the model did not write. The canary is the first body line of the pinned `SKILL.md` after its title, never the frontmatter `description`, which Claude lists without opening the file. A record the model did not write is a tool result that is not an error, or an injected user, system or attachment record such as a slash expansion. Assistant text, thinking and Codex reasoning never count, so a model that says it loaded the skill without reading it scores `no`. `unknown` means no transcript was found.
- **Read via.** The tool whose result held the canary (`Read`, `Bash`, `Skill`, `shell`, ...) or `injected`.
- **Helper.** Claude: an `Agent` (or legacy `Task`) call of type `general-purpose` whose result is not an error. Codex: a `spawn_agent` call whose output, decoded from its JSON text when the rollout stores it that way, names the started agent (`agent_id`) and is not a failure. A plain-text or serialized error scores `no`.
- **poteto-agent.** Claude only. `missing` when the call failed because the agent type is not installed (T07 vendors it later). `n/a` on Codex.
- **Version.** From the transcript: Claude's record `version`, Codex's `session_meta.cli_version`.

## How the probe launches

`bin/skill-probe run` reads the bot's own waiter config (`~/.config/factory/waiter-<bot>.yml`, through `load_harness_command`) and runs that argv unchanged, with only `harness_env` added and the prompt on stdin, as the waiter does. Each launch gets a fresh, empty run directory. On a remote device it goes through `remote.start_script` over the bot's own SSH alias with `-l <bot>`, and a codex argv is made device-neutral by `codex_sandbox.for_device`, exactly as a placed job. It never goes through `orch job run`, so nothing enters the live queue. Launches run one at a time.

Transcripts are read only from `~/.claude/projects/<slug>/` (or `$CLAUDE_CONFIG_DIR`) and `$CODEX_HOME/sessions/` (default `~/.codex/sessions/`), and only the run's own file: the Claude slug of its run directory, or the rollout whose `session_meta.cwd` is that directory. Symlinked transcript files, and folders that resolve outside those roots, are refused. On a remote device one fixed script under the bot's login shell applies the same rules, then prints that single file. No credential file is opened.

The Codex record names (`spawn_agent`, the rollout layout) follow the Codex CLI rollout format and are provisional until the first codex-bot run confirms them. A renamed tool is one edit to `CODEX_HELPER_TOOLS` in `lib/skill_probe.py`.

## Run it

On the box, from a factory checkout, as each bot's own user:

```sh
# as claude-bot
bin/skill-probe run --bot claude-bot --machines <live machines.yml> --dry-run
bin/skill-probe run --bot claude-bot --machines <live machines.yml> --out /tmp/skill-probe-claude
# as codex-bot
bin/skill-probe run --bot codex-bot --machines <live machines.yml> --dry-run
bin/skill-probe run --bot codex-bot --machines <live machines.yml> --out /tmp/skill-probe-codex
```

`--dry-run` prints each launch's argv and prompt and starts nothing. A real run writes `<device>-<harness>-<style>.json`, the harness output (`.log`) and a copy of the transcript (`.transcript.jsonl`) for each launch. Each launch is cut off after `--timeout` seconds (600 by default). Then render the table:

```sh
bin/skill-probe render /tmp/skill-probe-claude/*.json /tmp/skill-probe-codex/*.json
```

Paste its output over the Results table above.
