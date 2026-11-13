# Skill loading and helper agents, per device and harness (#119)

This page records how each harness on each device actually takes in a pstack skill, and whether a CLI job can start a helper agent. The evidence comes from the harness's own transcript, never from what the model says. `bin/skill-probe` writes the table below; T06 (router skills) and T09 (brief wiring) build on the style it shows works.

## Status

**No live run yet.** The table has no rows. The build job for #119 does not start harnesses (factory briefs forbid it), and the Codex half has to run as codex-bot. Max, or a session Max starts as each bot, runs the commands under "Run it", then the builder renders the results here.

Known before any run (checked on Nov 13, 2026):

- `skills/pstack/skills/poteto-mode/SKILL.md` has `disable-model-invocation: true`. The pstack Claude adapter (`skills/pstack/docs/harness/claude-code.md`) says the `Skill` tool refuses most pstack skills and says to `Read` the `SKILL.md` instead. Style `run-as` is therefore expected to score `no` on Claude. The probe decides it.
- pstack is not installed for claude-bot on the Mac: there is no `~/.claude/skills` and no `~/.agents/skills`. The probe records what happens and installs nothing.
- `machines.yml` on `main` has the Mac disabled with placeholder values. The live copy on the box must enable it for both harnesses before the Mac rows can be measured.

## Results

| Date | Device | Harness | Version | Style | Read | Read via | Helper | poteto-agent | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## What each column means

- **Style.** How the prompt opens:
  - `run-as`: "Run as poteto-mode." (today's briefs)
  - `read-path`: "Read `<abs path>/poteto-mode/SKILL.md` and follow it." (the pstack Claude adapter's own instruction). The path is the bot's installed copy on that device: `~/.claude/skills/poteto-mode/SKILL.md` for Claude, `~/.agents/skills/poteto-mode/SKILL.md` for Codex. `--skill-md` overrides it.
  - `slash`: `/poteto-mode` as the first line. Claude only, since `codex exec` has no slash commands.
  - `codex-skill`: `$poteto-mode`, which Codex resolves from `$HOME/.agents/skills`. Codex only.
  - A style that does not apply to a harness shows `n/a`.
- **Read.** `yes` only when the canary appears in a record the model did not write. The canary is the first body line of the pinned `SKILL.md` after its title, never the frontmatter `description`, which Claude lists without opening the file. A record the model did not write is a tool result that is not an error, or an injected user, system or attachment record such as a slash expansion. Assistant text, thinking and Codex reasoning never count, so a model that says it loaded the skill without reading it scores `no`. `unknown` means no transcript was found.
- **Read via.** The tool whose result held the canary (`Read`, `Bash`, `Skill`, `shell`, ...) or `injected`.
- **Helper.** Claude: an `Agent` (or legacy `Task`) call of type `general-purpose` whose result is not an error. Codex: a `spawn_agent` call whose output is not a failure.
- **poteto-agent.** Claude only. `missing` when the call failed because the agent type is not installed (T07 vendors it later). `n/a` on Codex.
- **Version.** From the transcript: Claude's record `version`, Codex's `session_meta.cli_version`.

## How the probe launches

`bin/skill-probe run` reads the bot's own waiter config (`~/.config/factory/waiter-<bot>.yml`, through `load_harness_command`) and runs that argv unchanged, with only `harness_env` added and the prompt on stdin, as the waiter does. Each launch gets a fresh, empty run directory. On a remote device it goes through `remote.start_script` over the bot's own SSH alias with `-l <bot>`, and a codex argv is made device-neutral by `codex_sandbox.for_device`, exactly as a placed job. It never goes through `orch job run`, so nothing enters the live queue. Launches run one at a time.

Transcripts are read only from `~/.claude/projects/<slug>/` (or `$CLAUDE_CONFIG_DIR`) and `$CODEX_HOME/sessions/` (default `~/.codex/sessions/`), and only the run's own file: the Claude slug of its run directory, or the rollout whose `session_meta.cwd` is that directory. Paths that resolve outside those folders are refused. On a remote device one fixed script under the bot's login shell finds and prints that single file. No credential file is opened.

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
