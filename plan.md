# Plan for #119, skill-loading and helper-agent probe

Factory briefs tell jobs to "Run as poteto-mode", and the jobs often never read the skill. Max wants to know, from evidence and not from what a model says, which way of asking actually makes each harness read a pstack `SKILL.md`, on each device. He also wants to know whether a CLI job can start a helper agent. This probe answers both, and T06 and T09 build on whatever loading style it shows works.

The smallest change that meets the ticket is one read-only script. It starts each harness with the waiter's own command, then reads the transcript that the harness itself writes. It writes one table to `docs/skill-loading.md`. One PR (#123, branch `factory/issue-119`). Risk low, one review round with Codex Bot. Grok Bot merges on `ship: handoff`.

## Grounding (what the probe has to fit)

These facts were checked in this plan job. Each one shapes a choice below.

- **The waiter's launch.** `lib/waiter.py` `load_harness_command` reads the bot's argv from `~/.config/factory/waiter-<bot>.yml` (an owner-only file). The brief goes on stdin. `lib/harness_env.py` adds `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` for Claude. A remote device runs `lib/remote.py` `start_script` over `ssh -l <bot> <alias>` under the bot's login shell, and `lib/codex_sandbox.py` `for_device` makes a codex argv device-neutral.
- **poteto-mode cannot be model-invoked.** The pinned `skills/pstack/skills/poteto-mode/SKILL.md` has `disable-model-invocation: true`. The pstack Claude adapter (`skills/pstack/docs/harness/claude-code.md`) says the `Skill` tool "refuses most pstack skills" and says to `Read <pstack>/skills/<name>/SKILL.md and follow it` instead. So style 1 probably does not read the skill. That is a guess, and the probe is how we find out.
- **Claude writes its own transcript in `-p` mode.** It goes to `~/.claude/projects/<slug>/<session>.jsonl`, and the slug is the cwd with each `/` and `.` changed to `-`. This plan job's own transcript has that shape (measured). Each record has `type`, `cwd`, `version` (the harness version) and `message.content` blocks. `tool_use` blocks carry `name` and `input`, and `tool_result` blocks carry `tool_use_id`, `content` and `is_error`. Subagent transcripts go in `<session>/subagents/`.
- **Codex writes its own rollout.** It goes to `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl` (`~/.codex` by default), with a `session_meta` record that has `cwd` and `cli_version`. Codex reads files through shell calls. The names of its function calls and of its sub-agent tool are not verified here, because codex is not on this Mac's claude-bot PATH. Unit 1 captures them from a real run before the parser is written.
- **The Mac today.** There is no `~/.claude/skills` and no `~/.agents/skills` for claude-bot, so pstack is not installed there (measured). The probe must not install it. It records what happens.

## Design (architect scaffold)

### Candidates

| | A. Waiter argv, harness's own transcript file | B. Add JSON output flags, parse stdout | C. Go through `orch job run` with a synthetic job |
| --- | --- | --- | --- |
| Launch | Exactly the configured argv, prompt on stdin | Configured argv plus `--output-format stream-json --verbose` or `--json` | The real runner |
| Meets "only the waiter's normal launch" | Yes | No, it changes the flags | Yes |
| Side effects | A probe directory and the harness's normal session file | Same | Writes orch.db rows, takes a device slot, heartbeats, can be retried by T6 |
| Evidence source | The harness's transcript | The harness's stream | The harness's transcript |
| Failure mode | Transcript persistence turned off, recorded as `unknown` | None new | Queue interference |

**Synthesis decision: A.** B breaks the criterion and the out-of-scope rule on launch flags. C puts a probe into the live queue. A's only risk is a config that turns off session persistence, and the probe reports that as `unknown` rather than guessing. Two shapes for the read check were also compared. One branched per tool (`Read`, `Skill`, `Bash cat`, slash expansion). The other asks one question: did a line from the skill's body reach the model's context from a source other than the model? The single question wins. It covers every path, including ones not thought of yet, and it never counts the model's own claim.

Arena runners skipped. The fork was settled by the acceptance criteria, and the remaining shape is small. Codex's review and `interrogate` give the adversarial pass.

### Data shape

```python
# lib/skill_probe.py

Harness = Literal["claude", "codex"]          # Cursor stays off; not in the table
Outcome = Literal["yes", "no", "missing", "n/a", "unknown"]
# yes/no: transcript shows it. missing: the helper agent type is not installed.
# n/a: the style does not apply to this harness. unknown: no transcript found.

class Style(Enum):
    RUN_AS = "run-as"            # 1. "Run as poteto-mode"
    READ_PATH = "read-path"      # 2. "Read <abs>/poteto-mode/SKILL.md and follow it"
    SLASH = "slash"              # 3. "/poteto-mode" at the top of the prompt
    CODEX_MENTION = "codex-skill"  # 4. "$poteto-mode", resolved from $HOME/.agents/skills

@dataclass(frozen=True)
class StyleSpec:
    style: Style
    harnesses: frozenset[Harness]           # where the style applies; elsewhere n/a
    opening: Callable[[str], str]           # skill_md abs path -> first prompt line

STYLES: tuple[StyleSpec, ...]               # the registry; the only place a style is defined

@dataclass(frozen=True)
class Evidence:
    read: Outcome
    read_via: str | None                    # "Read", "Skill", "shell", "injected"
    helper: Outcome                         # Claude general-purpose, or Codex sub-agent
    poteto_agent: Outcome                   # Claude only; codex rows are n/a
    harness_version: str | None             # from the transcript, not from --version

@dataclass(frozen=True)
class Launch:
    device: str
    harness: Harness
    style: Style
    argv: tuple[str, ...]                   # from load_harness_command, unchanged
    env: Mapping[str, str]                  # harness_env(harness) only
    run_dir: str                            # fresh, empty, under the device's workdir
    remote: RemoteTarget | None             # None on the local device

@dataclass(frozen=True)
class ProbeResult:
    date: str                               # ISO date of the run, UTC
    device: str
    harness: Harness
    style: Style
    evidence: Evidence
    note: str                               # one line, such as "no transcript" or a timeout

# Pure core: the unit-tested part
def skill_canary(skill_md: str) -> str: ...                   # first body line after the H1, never frontmatter
def claude_evidence(records: Iterable[Mapping], canary: str) -> Evidence: ...
def codex_evidence(records: Iterable[Mapping], canary: str) -> Evidence: ...
PARSERS: Mapping[Harness, Callable[[Iterable[Mapping], str], Evidence]]
def prompt_for(spec: StyleSpec, harness: Harness, skill_md: str) -> str: ...
def plan_launches(machines: MachinesConfig, bot: str, command: HarnessCommand,
                  run_id: str) -> list[Launch]: ...
def render(results: Sequence[ProbeResult]) -> str: ...       # docs/skill-loading.md

# Edge: the only code that touches processes and files
def run_launch(launch: Launch, prompt: str, timeout: float) -> str: ...  # returns transcript JSONL text, "" if none
def claude_transcript(home: Path, run_dir: str) -> Path | None: ...      # ~/.claude/projects/<slug>/*.jsonl only
def codex_transcript(codex_home: Path, run_dir: str, since: float) -> Path | None: ...  # sessions/**/rollout-*.jsonl only
```

`bin/skill-probe` mirrors `bin/orch` and has these subcommands:

- `skill-probe run --bot <bot> [--machines PATH] [--device NAME] [--out DIR] [--dry-run] [--timeout SEC]`. Run it as that bot's own user on the box. It covers every enabled device that lists the bot's harness, locally on the box and over SSH elsewhere. It writes `<out>/<device>-<harness>-<style>.json` (a `ProbeResult`) and a copy of each transcript beside it. `--dry-run` prints each `Launch` and its prompt and starts nothing.
- `skill-probe render <result.json>... > docs/skill-loading.md`.

### The rules the parsers enforce

- **Read = yes** only when the canary line from the skill's body shows up in a record that the model did not write. That means a `tool_result` (Claude `Read`, `Skill` or `Bash`) that is not `is_error`, an injected user or system record (a slash expansion), or a Codex function-call output. Assistant text and reasoning never count, so a model that says "loaded poteto-mode" without reading it scores `no`. The canary comes from the pinned `SKILL.md` body, never from its frontmatter `description`, because Claude lists that without opening the file.
- **Helper = yes** when the transcript has a spawn call with a non-error result. For Claude that is an `Agent` (or legacy `Task`) `tool_use` with `subagent_type: general-purpose`. For Codex it is the sub-agent tool call that Unit 1 names from a real rollout, kept in a one-line table so a renamed tool is one edit. `poteto_agent` checks `poteto-agent` and `pstack:poteto-agent`. An "agent type not found" style error scores `missing`, as the ticket asks while T07 is pending.
- **Applicability.** Styles 1 and 2 run on both harnesses. Style 3 is Claude only, because `codex exec` has no slash commands. Style 4 is Codex only. A cell that does not apply renders as `n/a`. Changing that is one `StyleSpec` edit.
- **Prompt.** The style's opening line, then fixed text. It asks the harness to start one helper that replies `ok` (and, on Claude, a second of type `poteto-agent`), then to stop. It also tells the harness to read no other file, write no file and make no network or GitHub call. The prompt never contains the canary.

### Credential guard

The probe opens only these paths:

- the pinned `SKILL.md`
- the bot's waiter config, through `load_harness_command`
- `machines.yml`
- its own run directory
- the two transcript locations above

The transcript locators resolve every candidate path and refuse anything outside `~/.claude/projects/<slug>/` or `$CODEX_HOME/sessions/`. They never touch `~/.codex/auth.json`, `~/.claude/.credentials*`, `.env`, `.runner` or the SSH config. Remote transcripts come back through one fixed `sh` script that `cat`s the single located file. Every ssh call goes through `remote.ssh_argv` with the bot's own alias and `-l <bot>`, as the waiter does.

## Criterion-to-work map

| # | Acceptance criterion | Work that meets it | Evidence |
| --- | --- | --- | --- |
| 1 | The probe runs on the box and the Mac for both harnesses (Claude and Codex). | Units 3 and 5. `plan_launches` covers every enabled device whose `harnesses` lists the bot's harness: local on the box, `remote.start_script` over SSH to the Mac. Unit 5 is the live run as claude-bot and as codex-bot. | `test_plan_launches_covers_local_and_remote_devices`, `test_disabled_or_placeholder_device_is_skipped`, and the run's result files for `box` and `mac` × `claude` and `codex`, committed through the rendered doc |
| 2 | `docs/skill-loading.md` has a table of device × harness × style → read yes/no and helper yes/no, with date and harness version. | Unit 4 `render`, then Unit 5 renders the live results into the doc | `test_render_has_one_row_per_device_harness_style`, `test_render_shows_date_and_harness_version`, and the committed `docs/skill-loading.md` |
| 3 | The probe reads no credential files and uses only the waiter's normal launch. | Unit 3. The argv comes only from `load_harness_command` (and `for_device` remotely), the env only from `harness_env`, and the prompt only on stdin. The transcript locators are path-confined. | `test_local_launch_argv_is_the_waiter_command_unchanged`, `test_remote_launch_uses_start_script_and_bot_alias`, `test_prompt_goes_on_stdin_only`, `test_transcript_locator_refuses_paths_outside_the_transcript_dirs`, `test_probe_run_opens_no_credential_path` (it records every `open` during a dry run and a faked run and asserts none matches `auth.json`, `.credentials*`, `.env`, `.runner`, `id_*`, `FACTORY_WEBHOOK`) |
| 4 | A unit test covers the transcript parser with fixture logs. | Units 1 and 2. Fixtures go in `tests/fixtures/skill_probe/`, trimmed from real transcripts. | `test_claude_read_tool_counts_as_read`, `test_claude_slash_expansion_counts_as_read`, `test_claude_claim_without_read_is_no`, `test_claude_refused_skill_tool_is_no`, `test_claude_general_purpose_helper_yes_poteto_agent_missing`, `test_codex_shell_read_counts_as_read`, `test_codex_subagent_spawn_counts_as_helper`, `test_missing_transcript_is_unknown`, `test_canary_is_body_not_frontmatter` |

All four rows are mapped. Row 1 and the data in row 2 depend on Unit 5, a live run that starts harnesses. See the open decision below.

## Units

Each unit ends in a check and gets its own commit. Tests go only in `tests/test_skill_probe.py`, with fixtures in `tests/fixtures/skill_probe/`.

1. **Fixtures from real transcripts.** Claude fixtures are cut from real `-p` transcripts. That includes a `Read` of a `SKILL.md`, an `Agent` spawn, and a refused or not-found agent type. Codex fixtures are cut from real `codex exec` rollouts on the box, read by codex-bot. That covers a shell read and the sub-agent call, which also pins its real tool name. Trim each file to the records the parser reads. Drop prompts and any env-looking text. *Check:* each fixture is valid JSONL, and a grep for `ghp_`, `gho_`, `sk-`, `Bearer `, `BEGIN .* PRIVATE KEY` finds nothing. Until real Codex rollouts exist (Unit 5 makes them), the Codex fixtures follow the documented rollout shape and are marked provisional in a module docstring. Unit 5 replaces them.
2. **Pure core.** This covers `skill_canary`, `STYLES`, `prompt_for`, `claude_evidence`, `codex_evidence`, `PARSERS` and `render`. *Check:* the row 4 and row 2 tests above pass and fail on revert.
3. **Launch plan and edges.** This covers `plan_launches`, `run_launch` (local `subprocess` with no shell, prompt on stdin, the timeout killing the process group; remote through `remote.start_script`, `remote.ssh_argv` and `remote.new_tag`, with the run id `skill-probe-<hex>` as the plain-token job id), the two transcript locators, and the remote fetch script. *Check:* the row 1 and row 3 tests pass with fake subprocess and SSH (reusing `tests/fake_ssh.py` and `tests/procs.py`), and `bin/skill-probe run --bot claude-bot --dry-run` prints the box and Mac launches.
4. **CLI.** `bin/skill-probe` with `run` and `render`. *Check:* `env -u ORCH_DB pytest`, the `check-config` steps and `bash model/check.sh` are green locally and in CI.
5. **Live run and doc.** As claude-bot on the box, run `bin/skill-probe run --bot claude-bot --out /tmp/skill-probe-<date>`. Then as codex-bot, run `bin/skill-probe run --bot codex-bot --out ...`. That makes 2 devices × 2 harnesses × 3 applicable styles, so 12 short runs. Render all the result files into `docs/skill-loading.md`, with a short prose lead naming the winning style per harness and any `unknown`. Swap the provisional Codex fixtures for trimmed real ones. *Check:* the doc has all 12 rows plus `n/a` cells, each with a date and harness version. The `read=yes` rows have a matching transcript copy in the out dir.

## Throughput checkpoint

- **Blocking first steps.** Unit 1 fixtures pin the transcript shapes. Write no parser before them.
- **Independent workstreams.** The pure core (Unit 2) and the launch edges (Unit 3) touch disjoint functions and can be written in parallel. The CLI waits for both.
- **Shared mutable state.** n/a in code. At run time each launch gets its own fresh run directory, so no two launches share a transcript slug or a rollout cwd. Runs go one at a time, so the probe never holds more than one slot's worth of load on a device.
- **Smallest safe decomposition.** One builder owns it. The code is about 400 lines in one module, and splitting it would cost more review than it saves.

## Verify

- `env -u ORCH_DB pytest`, CI green (pytest, `check-config`, statechart).
- A dry run on the box prints the exact waiter argv per device, unchanged.
- The live run's `read=yes` cells each point to a transcript record that is not an assistant record. A reviewer can check that against the saved copies.
- `blast-radius`: only new files. No change to the waiter, `orch.yml`, `machines.yml`, the vendored pstack or the briefs.

## Out of scope (from the ticket, held)

No waiter flag changes. No skill installs. No pstack edits. No Cursor. No `poteto-agent` vendoring (T07). No brief wiring (T09). No model map (T10). Any model the launch uses is whatever the bot's waiter config already sets.

## Open decision for Max

**Who may run Unit 5's live harness launches.** Each factory brief says "Never start another harness", and Unit 5 starts twelve short ones. This ticket's whole scope is to start them, and you approved it. One builder login also cannot run both halves, since the codex half must run as codex-bot. Two parts of the box's own state also have to hold: the box's SSH aliases must reach the Mac, and the live `machines.yml` must enable the Mac for both harnesses (the copy on `main` still has it disabled with placeholders).

**Default.** The build job builds and tests Units 1 to 4 and does not start a harness. It puts the two `run` commands, with `--dry-run` output, in the PR body. You (or a session you start as each bot) run them on the box. The builder then renders the doc, swaps in the real Codex fixtures, and posts `ready`. If you would rather the Claude build job run the claude-bot half itself, say so in the go, and it will.
