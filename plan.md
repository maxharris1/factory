# Plan for #160, T10: one helper model map for every harness

pstack tells each job which model each **helper** role runs on (feature, bug-fix, arena runners and the rest). Today that choice is spread over prose. `factory-builder` line 26 says Opus 5.5 high for Claude and Sol for Codex helpers, and `skills/factory/harness/codex.md` line 37 says the same. Neither is read by code. This ticket puts the choice in one YAML file. One module turns that file into the brief's helper model lines and into the text of `~/.claude/rules/pstack-models.md`.

This plan is for PR #213 (branch `factory/issue-160`). It adds three files and changes nothing else:

- `config/pstack-models.yml` (new)
- `lib/pstack_models.py` (new)
- `tests/test_pstack_models.py` (new)

Out of scope:

- `lib/brief.py` stays as it is. T09 (#180) puts these lines in the brief.
- The routers and `codex.md` keep their prose. T07a (#159) removes the model names there.
- Codex's main launch model stays as it is. That is operator step O6.
- Vendored files under `skills/pstack/` are never edited.
- The module writes no file. It returns text (Q2, global only, see below).

## How to read this

One box is one unit of work. Every box names the evidence that checks it. Check a box only when its evidence exists, such as a test run, a log line or a SHA. The build runs under `pstack/skills/poteto-mode/playbooks/feature.md`, careful playbook. There is one PR. Codex Bot reviews it, risk low, one round. Grok Bot merges it on `ship: handoff`.

## Architect scaffold

### Where the roles come from

The role list is never typed out in this repo. The module reads it from the pinned file `skills/pstack/skills/setup-pstack/SKILL.md`, step 5, "Write the rule". That step has one fenced block. Every line after `# budget:` up to the closing fence is `<role>: <default>`. The role is the text before the first `: `. At pin `c742dd6` (0.15.36) that gives 17 roles:

`feature, refactoring`, `bug-fix`, `perf-issue`, `hillclimb`, `judgment and prose`, `hardest tasks`, `how explorer`, `how explainer`, `why investigators`, `why synthesizer`, `reflect tooling`, `reflect judgment, divergent, synthesizer`, `arena runners`, `arena cross-judge pool`, `swarm workers`, `architect runners`, `interrogate reviewers`.

Four of these are panel roles, where the value is a list: `arena runners`, `arena cross-judge pool`, `architect runners` and `interrogate reviewers`. The panel set is read from the same block: a role is a panel when its default holds a comma. Role names such as `feature, refactoring` also contain commas, so the split is on the first `: ` and the comma test runs on the value only.

If a pin bump adds, renames or drops a role, the module raises and the test fails until the YAML is updated. That is the point of reading the pinned file.

### The data shape

```yaml
# config/pstack-models.yml
# Helper model per pstack role, one value per harness, in that harness
# adapter's own format (SF-6). Roles are the step-5 lines of the pinned
# skills/pstack/skills/setup-pstack/SKILL.md. lib/pstack_models.py reads
# this file. The brief's helper model lines and ~/.claude/rules/pstack-models.md
# both come from it.
harnesses:
  claude:
    budget: medium (high)     # setup-pstack's label for effort `high`
    default: opus high        # Max: "Claude Opus 5.5 high". Adapter model is opus|sonnet|haiku|fable.
    panel: [opus high, opus high]
  codex:
    budget: medium (high)
    default: sol high         # D12, D20: Codex builder and reviewer helpers run Sol.
    panel: [sol high, sol high]
  cursor:
    budget: medium (high)
    default: inherit-parent   # Cursor jobs are off. Helpers run on the parent model.
    panel: [inherit-parent, inherit-parent]
overrides: {}                 # optional {harness: {role: value}}, for one role that differs
```

Every role takes `default`, and every panel role takes `panel`. A panel keeps two entries, the pinned default's length, so the fan-out count does not change. `overrides` lets one role differ later without changing the shape, and it is empty today. Every override key must be a pinned role, or the module raises.

The harness keys must equal `lib.schema.HARNESSES` (`claude`, `codex`, `cursor`). So "a value per harness" covers every harness the factory can place a job on.

```python
# lib/pstack_models.py
@dataclass(frozen=True)
class RoleLine:
    role: str
    value: str | tuple[str, ...]   # a tuple for panel roles

def pinned_roles(root: Path = ROOT) -> tuple[tuple[str, bool], ...]:
    """(role, is_panel) in step-5 order, read from the pinned setup-pstack SKILL.md."""

def load_map(root: Path = ROOT) -> dict[str, tuple[RoleLine, ...]]:
    """harness -> one line per pinned role. Raises ModelMapError when a harness
    is missing, a role or override is unknown, or a value fails its harness check."""

def brief_lines(harness: str, root: Path = ROOT) -> str:
    """The brief's helper model block for one harness: one `<role>: <value>` line per role."""

def rules_text(root: Path = ROOT) -> str:
    """Full text of ~/.claude/rules/pstack-models.md for the claude harness."""
```

`load_map` reads the file with `lib.load.load_yaml`, which rejects duplicate keys. Both `brief_lines` and `rules_text` render from the same `load_map` result, so they cannot drift. Panel values render joined by `, `, the same as step 5.

### Per-harness value checks, run at load time

- **claude**: every entry matches `^(opus|sonnet|haiku|fable) (max|xhigh|high|medium|low)$` or is `inherit-parent` or `auto`. This is the adapter's own format: `claude-code.md:34` says `model` takes only `opus`, `sonnet`, `haiku` or `fable`, and `claude-code.md:38` gives the role value as `<model> <effort>`. So `opus high` passes and `opus-5.5 high` raises.
- **codex**: every entry matches `^<model> <effort>$` with the same effort ladder (`codex.md:38`), or is `inherit-parent` or `auto`.
- **all harnesses**: no `grok-*` model in any `claude` or `codex` entry, and no `claude-*` model in any `codex` entry.

### The rules text

`claude-code.md:46` gives the shape: no frontmatter, then the comment lines, the `# budget` line, then one line per role. `rules_text` writes exactly that, with one extra comment line:

```
# pstack model configuration. One line per role. Generated from config/pstack-models.yml by lib/pstack_models.py; do not edit by hand.
# `inherit-parent` or `auto` as a value: the role runs on the parent chat model (omit Task `model`). Alias entries in a panel list still count toward its fan-out.
# A factory job's brief carries its own helper model lines. Where the brief and this file differ, the brief wins.
# budget: medium (high)
feature, refactoring: opus high
...
interrogate reviewers: opus high, opus high
```

**Global only (Q2).** The text is for `~/.claude/rules/`, the bot's home. Nothing writes a project-level `.claude/rules/` file, a `CLAUDE.md` or an `AGENTS.md`. The module returns text and never writes it. Installing the file is a later operator step.

`python -m lib.pstack_models rules` prints `rules_text()`. `python -m lib.pstack_models brief <harness>` prints `brief_lines(harness)`. These let an operator inspect the output and install the file, and CI never runs them.

## Acceptance criteria to work

| # | Criterion | Work that meets it | Evidence |
| --- | --- | --- | --- |
| AC1 | Every helper role in the pinned `setup-pstack/SKILL.md` has a value per harness, read from the pinned file. | `pinned_roles` parses step 5 of the pinned SKILL.md. `load_map` raises if any harness in `HARNESSES` lacks a line for any pinned role. | `test_roles_read_from_pin`: the role list equals the step-5 block parsed from the pinned file, has 17 entries, and its path hash matches `skills.lock`. `test_every_role_every_harness`: `set(load_map())` equals `set(HARNESSES)`, and each harness has one line per pinned role in pin order. `test_new_pinned_role_fails`: a tmp root with an extra step-5 role makes `load_map` raise. |
| AC2 | Every Claude value parses in the adapter's format. | The claude value check above, run inside `load_map`. | `test_claude_values_adapter_format`: every claude entry, panel entries included, matches the regex, and `model` is in `{opus, sonnet, haiku, fable}`. `test_claude_rejects_versioned`: a tmp YAML with `opus-5.5 high` or `claude-opus-5-5-high` raises `ModelMapError`. |
| AC3 | Codex helpers (builder and reviewer) are Sol. No criterion requires changing Codex's main launch model. | `codex.default` and `codex.panel` are `sol high`. One codex map serves both builder and reviewer jobs, since the brief picks lines by harness, not by job kind. No file outside the three is touched, and `lib/brief.py`, `orch.yml` and the codex launch path are unchanged. | `test_codex_helpers_sol`: every codex role, panel entries included, has model `sol`. `brief_lines("codex")` contains no non-Sol model. `git diff --stat main...` lists only the three files and `plan.md`. |
| AC4 | No Claude line names `grok-*`, and no Codex line names `grok-*` or `claude-*`. | The cross-family check above, in `load_map`. | `test_no_foreign_families`: scan `brief_lines("claude")`, `brief_lines("codex")` and `rules_text()` for `grok-`, and the codex output for `claude-`. `test_foreign_family_rejected`: a tmp YAML with `grok-4.7-xhigh-fast` under claude, or `claude-opus-5-5-high` under codex, raises. |
| AC5 | The brief lines and the rules text come from the same YAML, and the rules text says the brief wins. | Both render from one `load_map` result. `rules_text` carries the "brief wins" comment line. | `test_brief_and_rules_same_source`: the role lines in `rules_text()` equal `brief_lines("claude")` line for line. `test_rules_follow_yaml`: change `claude.default` in a tmp YAML and both outputs change together. `test_rules_say_brief_wins`: `rules_text()` contains "the brief wins". `test_rules_shape`: no frontmatter, `# budget:` line present, one line per pinned role. |

All five criteria map to work. Status: **ac=met**.

## Work boxes

- [ ] `config/pstack-models.yml` as above. Evidence: `load_map()` loads it without error.
- [ ] `lib/pstack_models.py`: `pinned_roles`, `load_map`, value checks, `brief_lines`, `rules_text`, `__main__`. Evidence: the tests below.
- [ ] `tests/test_pstack_models.py`: the tests named in the table. Tmp-root tests copy the pinned SKILL.md and the YAML into `tmp_path` and never touch the real tree. Evidence: `env -u ORCH_DB pytest tests/test_pstack_models.py -q` green.
- [ ] Full suite: `env -u ORCH_DB pytest -q`. Evidence: the pass count.
- [ ] Revert check: break the YAML (drop one role, and set a claude value to `opus-5.5 high`). The matching tests fail, then pass again once restored. Evidence: the output pasted in the ready comment.
- [ ] CI green on the PR head (`.github/workflows/ci.yml`). Evidence: the run URL.
- [ ] The `ready` handoff names that head.

## Assumptions the reviewer should check

- **Sol's string.** Max's words in #119 were "current setting Astra and then sol subagents". The YAML writes Sol as `sol high`, in the codex adapter's `<model> <effort>` form (`codex.md:38`). If Codex's `spawn_agent` lists Sol under another slug, that change is one YAML value and one constant in the test. Effort `high` matches Max's "Opus 5.5 high" for Claude. Nothing in the ticket sets a different Codex effort.
- **Cursor row.** Cursor jobs are off, but `HARNESSES` includes `cursor`. So cursor gets `inherit-parent` for every role, which is valid in Cursor's own format and names no model.
- **Cross-judge on one family.** With every Claude entry on `opus`, `arena cross-judge pool` has no other-family entry, so Arena on Claude runs without a cross-judge. The reviewer rules (one family per harness, "same model, research only") make that intended. The same holds for codex.
