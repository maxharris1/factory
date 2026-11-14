# Plan #176: probe `claude -p --plugin-dir <pinned pstack>` (T03c)

Ticket: https://github.com/maxharris1/factory/issues/176 · PR #235 · risk low · base `main`, which already has #123 (T03a) merged.

## Goal

`bin/skill-probe run --plugin-dir <abs path>` starts Claude with the vendored pstack tree loaded in place (`claude ... --plugin-dir <path>`). Nothing is installed into the bot home. It records two things from the harness's own transcript, as the current probe does:

1. Whether the `poteto-mode` skill loaded from the plugin tree (yes or no).
2. Whether the two agent names the Claude adapter uses (`skills/pstack/docs/harness/claude-code.md:20`) resolve: `pstack:poteto-agent` and `pstack:comment-sicko` (yes or no each).

The live run is out of scope. #123 covers the live run of the current probe. Factory may confirm `--plugin-dir` live later as a short operator follow-up and record it in `docs/skill-loading.md`.

## Architect scaffold

### What exists (lib/skill_probe.py)

- `plan_launches` → `Launch` (argv, env, run_dir, skill_md, remote) per device × style.
- `prompt_for(style, harness, skill_path, canary)` builds the stdin prompt. It never contains the canary.
- `claude_evidence(records, canary)` → `Evidence(read, read_via, helper, poteto_agent, version)`. Read is yes only when the canary shows up in a non-error tool result or an injected record. Agent outcomes come from `Agent`/`Task` calls keyed on `subagent_type`. A "not found" error gives `missing`.
- `ProbeResult` JSON plus `render()` → the table in `docs/skill-loading.md`.

### Shape of the change (one module, no new module)

- **`--plugin-dir PATH` on `run`** (Claude only). It must be an absolute path. The probe checks it locally when the device is local: the path must be a directory with `skills/poteto-mode/SKILL.md`. A codex bot plus `--plugin-dir` raises `ProbeError`. The flag applies on every probed device, the same way `--skill-md` does. Use `--device` to scope a run to one device.
- **argv**: `with_plugin_dir(argv, path)` inserts `--plugin-dir <path>` right after `argv[0]`. Nothing else in the waiter argv changes. A remote launch carries it through the existing `start_script` path unchanged. The probe never runs `claude plugin install` or `claude plugin marketplace`, and it writes no settings file.
- **Canary**: in plugin-dir mode the canary comes from `<plugin-dir>/skills/poteto-mode/SKILL.md`, which is the tree actually loaded, not the repo pin. Locally the probe reads that file. For a remote device the operator runs the probe from a checkout pinned to the same tree. The default canary source stays the repo pin, and the `docs/skill-loading.md` section says so.
- **Prompt**: `prompt_for(..., plugin=True)` changes two things:
  - The helpers line asks for one `general-purpose` agent, then one `pstack:poteto-agent`, then one `pstack:comment-sicko`, each replying ok.
  - `slash` opens with `/pstack:poteto-mode`, the plugin's namespaced command. `read-path` names `<plugin-dir>/skills/poteto-mode/SKILL.md`. `run-as` is unchanged.
  - The existing "never holds the canary" guard still applies.
- **Evidence**: two new fields, `pstack_poteto_agent` and `pstack_comment_sicko`, both `Outcome` with default `"n/a"`.
  - `claude_evidence(records, canary, *, plugin=False)` scores a call whose `subagent_type` is exactly `pstack:poteto-agent` or `pstack:comment-sicko`. It uses the same `_spawn_outcome`/`_better` rules. A call that never happened is `no`, and a "not found" error is `missing` (a kind of no, the same vocabulary as the existing column).
  - With `plugin=False` both fields stay `n/a`, so old runs and Codex rows don't change.
  - The existing `poteto_agent` field keeps its meaning.
- **ProbeResult** gains `plugin_dir: str | None = None`. `from_json` tolerates JSON written before this change, which has none of the new keys. `render()` adds `pstack:poteto-agent` and `pstack:comment-sicko` columns and appends `plugin-dir` to the Note when it is set, so plugin-dir rows sit next to the installed-path rows.
- **Run dir and output**: as today, a fresh run dir under `--work` (default: temp) and results under `--out`. Neither may sit under the bot home's `.claude/`. The probe refuses an `--out` or `--work` inside `~/.claude` or `~/.agents`.

### Fixtures

- `tests/fixtures/skill_probe/plugin_tree/pstack/`: a fake pstack tree with `plugin.json` (`"name": "pstack"`), `skills/poteto-mode/SKILL.md` with its own canary body line, and `agents/poteto-agent.md` and `agents/comment-sicko.md` (frontmatter `name:` only). It holds no secrets and is covered by the existing secret-shape fixture test.
- `tests/fixtures/skill_probe/claude_plugin_dir_loaded.jsonl`: a trimmed Claude transcript.
  - A `Skill` tool_use for `pstack:poteto-mode` whose tool_result holds the fake canary.
  - `Agent` calls for `general-purpose`, `pstack:poteto-agent` and `pstack:comment-sicko` with non-error results.
- `tests/fixtures/skill_probe/claude_plugin_dir_unloaded.jsonl`:
  - The slash command is not expanded and there is no canary anywhere except assistant text.
  - Both prefixed `Agent` calls return `is_error` with "Agent type 'pstack:poteto-agent' not found" (and the same for comment-sicko).

### Tests: `tests/test_skill_probe_plugin_dir.py` (new)

1. `test_loaded_tree_records_skill_yes` and `test_unloaded_tree_records_skill_no`: the loaded and unloaded fixtures, with the fake tree's canary, give `read == "yes"` (via `Skill`) and `read == "no"`.
2. `test_prefixed_agents_resolve_yes` and `test_prefixed_agents_missing_is_no`: the loaded fixture gives `pstack_poteto_agent == pstack_comment_sicko == "yes"`. The unloaded fixture gives `"missing"` for both. A transcript with no such calls gives `"no"` for both.
3. `test_plugin_fields_are_na_without_plugin_dir`: the existing fixtures give `n/a` for the new fields, and an old result JSON without the keys still loads.
4. `test_plugin_dir_inserted_after_executable`: for local and remote launches, the argv is `claude --plugin-dir <path> <rest of waiter argv unchanged>`. No `plugin install` or `marketplace` token appears.
5. `test_plugin_dir_rejected_for_codex_and_relative_paths`: a codex bot, a relative path, and a local path without `skills/poteto-mode/SKILL.md` each raise `ProbeError` (exit 1 from `main`).
6. `test_plugin_prompt_asks_for_prefixed_agents_and_never_holds_canary`: checks every Claude style.
7. **End-to-end fake-tree run** (`test_fake_tree_run_writes_nothing_into_bot_home`):
   - Setup: `HOME` is a temp bot home, seeded with a `.claude/skills/other/SKILL.md` and a `.claude/settings.json`. `CLAUDE_CONFIG_DIR` is a separate temp dir. A fake `claude` script is on `PATH` and named by a temp waiter config.
   - The fake script asserts that `--plugin-dir <fake tree>` is in its argv. It writes the loaded or unloaded transcript into `$CLAUDE_CONFIG_DIR/projects/<slug of cwd>/s.jsonl`, depending on whether the tree it was handed has `agents/`. It is parametrised over the full fake tree and a copy with `agents/` and `skills/` removed.
   - It runs `skill_probe.main(["run", "--bot", "claude-bot", "--plugin-dir", tree, "--device", "box", ...])`.
   - It asserts:
     - the result JSON gives yes/yes/yes for the full tree and no/missing/missing for the stripped tree;
     - a before/after snapshot of every path, size and mtime under `HOME` is identical: no `.claude/plugins/`, no new skill, no settings change;
     - the fake tree's file hashes are unchanged;
     - the refusal of `--out` under `~/.claude` holds.

### Docs

`docs/skill-loading.md` gets a short "Run with `--plugin-dir`" subsection under "Run it". It covers the flag, that nothing is installed, the canary source, and the two new columns, and says live results land here as the operator follow-up. No live rows are added in this PR.

## Acceptance criteria → work

| # | Criterion | Work that meets it | Test that proves it |
| --- | --- | --- | --- |
| AC1 | A fake-tree test records yes or no for skill load under `--plugin-dir`. | `--plugin-dir` flag, `with_plugin_dir` argv, plugin-tree canary, plugin-aware `prompt_for`. `claude_evidence` scores `read`. Fixture tree plus loaded and unloaded transcripts. | Tests 1 and 7 (full tree → `read=yes` via `Skill`; stripped tree → `read=no`). Tests 4–6 cover the launch shape. |
| AC2 | It records yes or no for the two prefixed agent names. | New `Evidence.pstack_poteto_agent` and `pstack_comment_sicko`, scored on exact `subagent_type`. Persisted in `ProbeResult` JSON and rendered as columns. | Tests 2, 3 and 7 (yes/yes, missing/missing, no/no; `n/a` without the flag; old JSON loads). |
| AC3 | Nothing is written into a bot home. | `--plugin-dir` loads the tree in place, with no `plugin install`, no marketplace and no settings write. The probe refuses `--out`/`--work` under `~/.claude` or `~/.agents`. The fake tree is read only. | Test 7 (HOME snapshot identical, tree hashes unchanged, refusal holds) and test 4 (no install tokens in argv). |

Every criterion is mapped.

## Files

- `lib/skill_probe.py`: flag, argv insert, prompt, evidence fields, result field, render columns, out/work guard.
- `tests/test_skill_probe_plugin_dir.py` (new).
- `tests/fixtures/skill_probe/plugin_tree/pstack/...` and two transcript fixtures (new).
- `docs/skill-loading.md`: short subsection.
- `tests/test_skill_probe.py`: only if `render`'s header change breaks an existing assertion, updating that one expectation.

The vendored `skills/pstack/` is not touched, because its files are hash-locked in `skills/skills.lock`. #179 (conflicts-with) touches `lib/skill_preflight.py` and `lib/waiter.py`, which do not overlap with this change.

## Risks and open points (none block)

- Claude Code reads a plugin manifest from `.claude-plugin/plugin.json`. pstack ships `plugin.json` at its root and in `.cursor-plugin/`. Without a manifest, Claude derives the plugin name from the directory name (`pstack`), which gives the `pstack:` prefix. Only the live follow-up can confirm this, and the probe records whatever happens. If agents come back `missing` live, that finding belongs to T07, not to this ticket.
- `missing` is reported as its own value. It means the call happened and the name did not resolve, which is a no. The docs say so.

## Verify

`env -u ORCH_DB env -u CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS pytest tests/test_skill_probe.py tests/test_skill_probe_plugin_dir.py tests/test_pstack_pin.py`, then the full suite in chunks, then `check-config` and the statechart check as CI runs them.
