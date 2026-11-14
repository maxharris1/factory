# Skill loading and helper agents, per device and harness (#119)

This page records how each harness on each device actually takes in a pstack skill, and whether a CLI job can start a helper agent. The evidence comes from the harness's own transcript, never from what the model says. `bin/skill-probe` writes the table below; T06 (router skills) and T09 (brief wiring) build on the style it shows works.

## Status

**Measured** on box, pc and Mac for both harnesses, on Nov 13, 2026 between 8:08 and 8:15 PM ET (the table dates are UTC). The run went from the box with the live `machines.yml` (pc and Mac enabled), each bot's own waiter argv, and `--timeout 600`. The Codex rows are from a rerun after two probe fixes: the locator now skips a sub-agent's rollout, and a `spawn_agent` output that names a `task_name` counts as a started helper. No launch timed out and every launch left a transcript. The three pc control rows come from the earlier pc run, which pointed `read-path` at the repo's pinned copy with `--skill-md`.

What the run shows:

- **pstack's poteto-mode is installed for no bot on any device.** `~/.claude/skills/poteto-mode/` and `~/.agents/skills/poteto-mode/` are missing on box, pc and Mac. On the box, `~/.claude/skills` and `~/.agents/skills` hold other skills but not poteto-mode. So no style can load the installed skill today, and every `read-path` row is `no` with "File does not exist" (Claude `Read`) or "No such file or directory" (Codex `cat`).
- **Claude never reads the skill on its own.** `run-as` makes no tool call for the skill and `slash` does not expand. With a file present (the pc control rows), `read-path` scores `yes` via `Read`. So `read-path` is the style that works on Claude once the skill is installed. `run-as` and `slash` score `no` either way.
- **Codex goes looking for it.** Under `run-as` and `codex-skill`, Codex searches the disk for `poteto-mode/SKILL.md`. It reads the first match, which is some factory checkout's pinned copy, not an installed skill. On the box it found `/home/box/factory`, and on the pc an old job's repo. Nothing on the Mac matched, and the Mac has no `rg`. These `yes` rows show that Codex will read a skill it can find. They don't show that the installed path works, because there isn't one yet.
- **Helper agents start everywhere.** Claude (2.1.318 on the box, 2.1.322 on pc and Mac): the `Agent` call of type `general-purpose` returns the sub-agent's report. `poteto-agent` is `missing` ("Agent type 'poteto-agent' not found"), as expected until T07. Codex (0.162.1 on box and pc, 0.167.2 on the Mac): `spawn_agent` returns `{"task_name":"/root/<name>"}`, and the sub-agent's reply arrives through `wait_agent`.
- Trimmed live transcripts are the fixtures `tests/fixtures/skill_probe/claude_live_read_path.jsonl` (pc control `read-path`) and `codex_live_run_as.jsonl` (box `run-as`).

## Results

| Date | Device | Harness | Version | Style | Read | Read via | Helper | poteto-agent | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-14 | box | claude | 2.1.318 | run-as | no |  | yes | missing |  |
| 2026-11-14 | box | claude | 2.1.318 | read-path | no |  | yes | missing |  |
| 2026-11-14 | box | claude | 2.1.318 | slash | no |  | yes | missing |  |
| | box | claude | | codex-skill | n/a | | n/a | n/a | style does not apply |
| 2026-11-14 | box | codex | 0.162.1 | run-as | yes | exec | yes | n/a | found by search: `/home/box/factory/skills/pstack/...` |
| 2026-11-14 | box | codex | 0.162.1 | read-path | no |  | yes | n/a |  |
| | box | codex | | slash | n/a | | n/a | n/a | style does not apply |
| 2026-11-14 | box | codex | 0.162.1 | codex-skill | yes | exec | yes | n/a | found by search: `/home/box/factory/skills/pstack/...` |
| 2026-11-14 | mac | claude | 2.1.322 | run-as | no |  | yes | missing |  |
| 2026-11-14 | mac | claude | 2.1.322 | read-path | no |  | yes | missing |  |
| 2026-11-14 | mac | claude | 2.1.322 | slash | no |  | yes | missing |  |
| | mac | claude | | codex-skill | n/a | | n/a | n/a | style does not apply |
| 2026-11-14 | mac | codex | 0.167.2 | run-as | no |  | yes | n/a |  |
| 2026-11-14 | mac | codex | 0.167.2 | read-path | no |  | yes | n/a |  |
| | mac | codex | | slash | n/a | | n/a | n/a | style does not apply |
| 2026-11-14 | mac | codex | 0.167.2 | codex-skill | no |  | yes | n/a |  |
| 2026-11-14 | pc | claude | 2.1.322 | run-as | no |  | yes | missing |  |
| 2026-11-14 | pc | claude | 2.1.322 | read-path | no |  | yes | missing |  |
| 2026-11-14 | pc | claude | 2.1.322 | slash | no |  | yes | missing |  |
| | pc | claude | | codex-skill | n/a | | n/a | n/a | style does not apply |
| 2026-11-14 | pc | codex | 0.162.1 | run-as | yes | exec | yes | n/a | found by search: an old job checkout's `skills/pstack/...` |
| 2026-11-14 | pc | codex | 0.162.1 | read-path | no |  | yes | n/a |  |
| | pc | codex | | slash | n/a | | n/a | n/a | style does not apply |
| 2026-11-14 | pc | codex | 0.162.1 | codex-skill | no |  | yes | n/a |  |
| 2026-11-14 | pc | claude | 2.1.322 | run-as | no |  | yes | missing | control: `--skill-md` = repo's pinned copy |
| 2026-11-14 | pc | claude | 2.1.322 | read-path | yes | Read | yes | missing | control: `--skill-md` = repo's pinned copy |
| 2026-11-14 | pc | claude | 2.1.322 | slash | no |  | yes | missing | control: `--skill-md` = repo's pinned copy |

## What each column means

- **Style.** How the prompt opens:
  - `run-as`: "Run as poteto-mode." (today's briefs)
  - `read-path`: "Read `<abs path>/poteto-mode/SKILL.md` and follow it." (the pstack Claude adapter's own instruction). The path is the bot's installed copy on that device: `~/.claude/skills/poteto-mode/SKILL.md` for Claude, `~/.agents/skills/poteto-mode/SKILL.md` for Codex. `--skill-md` overrides it.
  - `slash`: `/poteto-mode` as the first line. Claude only, since `codex exec` has no slash commands.
  - `codex-skill`: `$poteto-mode`, which Codex resolves from `$HOME/.agents/skills`. Codex only.
  - A style that does not apply to a harness shows `n/a`.
- **Read.** `yes` only when the canary appears in a record the model did not write. The canary is the first body line of the pinned `SKILL.md` after its title, never the frontmatter `description`, which Claude lists without opening the file. A record the model did not write is a tool result that is not an error, or an injected user, system or attachment record such as a slash expansion. Assistant text, thinking and Codex reasoning never count, so a model that says it loaded the skill without reading it scores `no`. `unknown` means no transcript was found.
- **Read via.** The tool whose result held the canary (`Read`, `Bash`, `Skill`, `shell`, ...) or `injected`.
- **Helper.** Claude: an `Agent` (or legacy `Task`) call of type `general-purpose` whose result is not an error. Codex: a `spawn_agent` call whose output, decoded from its JSON text when the rollout stores it that way, names the started agent (`task_name` on Codex 0.162 and 0.167, `agent_id` on older builds) and is not a failure. A plain-text or serialized error scores `no`.
- **poteto-agent.** Claude only. `missing` when the call failed because the agent type is not installed (T07 vendors it later). `n/a` on Codex.
- **pstack:poteto-agent, pstack:comment-sicko.** Only in a `--plugin-dir` run: the agent names the Claude adapter uses (`skills/pstack/docs/harness/claude-code.md:20`), scored on the exact `subagent_type`. `yes` when the call's result is not an error. `missing` when the call failed because the name did not resolve, which is a no. `no` when no such call was made. `n/a` in a run without `--plugin-dir`. The rows above predate these columns and show `n/a` when re-rendered.
- **Version.** From the transcript: Claude's record `version`, Codex's `session_meta.cli_version`.

## How the probe launches

`bin/skill-probe run` reads the bot's own waiter config (`~/.config/factory/waiter-<bot>.yml`, through `load_harness_command`) and runs that argv unchanged, with only `harness_env` added and the prompt on stdin, as the waiter does. Each launch gets a fresh, empty run directory. On a remote device it goes through `remote.start_script` over the bot's own SSH alias with `-l <bot>`, and a codex argv is made device-neutral by `codex_sandbox.for_device`, exactly as a placed job. It never goes through `orch job run`, so nothing enters the live queue. Launches run one at a time.

Transcripts are read only from `~/.claude/projects/<slug>/` (or `$CLAUDE_CONFIG_DIR`) and `$CODEX_HOME/sessions/` (default `~/.codex/sessions/`), and only the run's own file: the Claude slug of its run directory, or the rollout whose `session_meta.cwd` is that directory. A Codex sub-agent writes its own rollout with the same `cwd`. Its `session_meta` names a `parent_thread_id`, and the probe skips it. Symlinked transcript files, and folders that resolve outside those roots, are refused. On a remote device one fixed script under the bot's login shell applies the same rules, then prints that single file. No credential file is opened.

The Codex record names (`spawn_agent`, the rollout layout) are confirmed by the live runs on Codex 0.162.1 and 0.167.2. A renamed tool is one edit to `CODEX_HELPER_TOOLS` in `lib/skill_probe.py`.

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

### Run with `--plugin-dir`

```sh
bin/skill-probe run --bot claude-bot --machines <live machines.yml> --device box \
  --plugin-dir <abs path to the pinned pstack tree> --out /tmp/skill-probe-plugin
```

Claude only: a codex bot or a relative path is refused. The waiter argv gets `--plugin-dir <path>` right after its executable and nothing else changes, so Claude loads the tree in place. The probe runs no `claude plugin install` or `marketplace` command, writes no settings, and refuses an `--out` or `--work` inside `~/.claude` or `~/.agents`, so nothing is written into the bot home.

- The canary is the first body line of `<plugin-dir>/skills/poteto-mode/SKILL.md`, the tree actually loaded. A local run refuses a tree without that file. A remote-only run reads the canary from the repo pin, so run it from a checkout pinned to the same tree.
- `read-path` names the tree's own `SKILL.md`, `slash` opens with `/pstack:poteto-mode`, and the helpers line also asks for `pstack:poteto-agent` and `pstack:comment-sicko`.
- Each row's Note says `plugin-dir`, so these rows can sit next to the installed-path rows.
- The `pstack:` prefix depends on Claude naming the plugin after its directory, since pstack ships no `.claude-plugin/plugin.json`. The probe records what happens either way.

No live `--plugin-dir` rows yet. The live run on each device is an operator follow-up, and its rows go in the Results table above.
