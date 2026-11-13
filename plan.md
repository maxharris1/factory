# Plan for #121, factory router skills

Factory jobs are told to use pstack but cannot reliably load it, and factory prompts have been overriding pstack practice. Max's call is to build on pstack, not replace it. This ticket adds two small factory-owned router skills that tell a job which pstack files to read, by exact path, and how a factory job maps onto pstack's steps. It also adds a factory-owned Codex adapter, because pstack ships only a Claude one.

The smallest change that meets the ticket is four new files and no edits. PR #125, branch `factory/issue-121`, risk low, Claude builds, Codex reviews.

## How to read this

One box is one unit of work. Every box names the evidence that checks it. Check a box only when its evidence exists, a test run, a command output or a SHA.

The build job runs under `<pstack>/skills/figure-it-out/SKILL.md` (Max's "careful playbook always"). The Feature playbook steps it borrows are named where they apply.

## Architect scaffold

### Data shape

Each skill is a route table plus a mapping table. Nothing else carries meaning.

| Fact | Shape | Where it lives |
| --- | --- | --- |
| A pstack file a job must read | `<pstack>/<path>`, where `<path>` is a key in `skills/skills.lock` `files:` | Inline in each skill and in the adapter |
| `<pstack>` | The absolute folder that holds pstack's `skills/` and `docs/`. The brief gives it. Until T09 wires briefs, it is this repo's `skills/pstack`. | Opening line of each skill |
| A factory override of a pstack rule | One table row, `pstack says` / `factory job does` / `why`. The first cell is a `<pstack>/<path>` plus a verbatim quote from that file. | The last table in each skill |
| A handoff the skill tells a job to post | An `orch:v1` header that `lib/handoff.parse_header_line` accepts | Inline examples in each skill |

The mapping table's quote rule is what makes "names the pstack line it maps" checkable by a test, not only by a reader.

### Files

| Path | Lines | Role |
| --- | --- | --- |
| `skills/factory/factory-builder/SKILL.md` | 40 to 80 | Router for plan, build and fix jobs |
| `skills/factory/factory-reviewer/SKILL.md` | 40 to 80 | Router for review jobs |
| `skills/factory/harness/codex.md` | about the size of the Claude adapter | Cursor-to-Codex map, same sections as `<pstack>/docs/harness/claude-code.md` |
| `tests/test_router_skills.py` | small | The lint the first criterion names |

### Alternatives

| Option | Verdict |
| --- | --- |
| One combined factory skill for both roles | Rejected. The builder and reviewer read disjoint pstack sets, and the ticket names two files. |
| Two routers plus a Codex adapter, pointing at pinned paths | Chosen. It is what Max approved ("middle ground"). |
| Copy pstack text into the factory skills | Rejected. It drifts from the pin and reads as a replacement. |
| Edit vendored pstack to add a Codex adapter | Rejected. Out of scope, and `verify_pin` would fail. |

Skipped. The `architect` arena fan-out. The shape is concrete and the work is prose plus one lint, and figure-it-out Phase B says to skip `architect` for mechanical work whose shape is already concrete.

## Planner decisions

These are calls the plan makes so the build does not stall. None is a product question. The reviewer can push back on any of them.

1. **`repeat=` goes in the body, not the header.** `repeat` is not in `KEYS` in `lib/handoff.py`. A verdict header with `repeat=ac-3` fails with `unknown key 'repeat'` (run on this branch, `parse_header_line`). A bad header would make the poller drop the verdict. So the reviewer skill writes a normal `kind=verdict verdict=REVISE` header and puts `repeat=<ac-N|finding-id>` as the first body line, then asks Max. Adding a `repeat` header key is dispatcher code, which this ticket rules out. The handoff names it as a follow-up.
2. **Rebase on the ticket branch, and the brief still wins.** The skill allows rebase any time on the ticket branch with `--force-with-lease`, never on the base (Max). Today's rendered brief (`lib/brief.py`) still says no force-push and no rebase of a pushed branch. The skill says to follow the brief where the two differ, until T09 changes the brief. The mapping table carries this row.
3. **`deslop` is named with no path.** It is not in the pin, so a `<pstack>/` path to it would fail the lint. The builder skill names it and says it is skipped until T07 vendors it.
4. **Reviewer helpers may run code.** "Research only" means they never edit the PR branch, push, or post. They may run repros in a throwaway copy, because `interrogate` and `blast-radius` only keep findings proven by a run.
5. **No decision note file.** `docs/decisions/` is not on main yet, and the ticket says to prefer waiting for T04. Decisions live in this plan and the PR body.
6. **Model lines.** Each skill says to use the brief's model lines. Until T10 lands it records Max's words: Claude jobs use Opus 5.5 high, Codex jobs use the current setting (Astra), Codex helper subagents use Sol.

## Mapping rows the skills carry

The build writes these rows. Each first cell quotes the pinned file verbatim, and the lint checks the quote.

**factory-builder**

| pstack says | factory job does | why |
| --- | --- | --- |
| poteto-mode SKILL.md, AskQuestion on a fork, and "Gates the operator named and the Always-pause list in Autonomy still need the operator" | Post a `questions` handoff. Max's gates are in the brief. | The job has no live operator. The handoff is the only channel. |
| poteto-mode Autonomy, "force-push to shared branches" | Force-push only the ticket branch, `--force-with-lease`, and only when the brief allows it | The ticket branch has one writer. Max's rule. |
| poteto-mode Playbooks routing (Feature for features) | Always `figure-it-out` | Max, "Careful playbook always" |
| poteto-mode Subagents, default `grok-4.7-xhigh-fast` for code | Helpers run on the job's own harness and the brief's model lines | Cursor jobs are off. Max's model map, T10 later. |
| opening-a-pr.md, Conventional Commits titles | Keep the `#<issue>: ` title prefix and edit the existing PR | Max's rule, factory #115. The feeder already opened the PR. |
| opening-a-pr.md, "Babysit" | No babysit. The poller drives CI and review. | The factory owns the review loop. |
| poteto-mode, "Before commit → the `deslop` skill" | Name it and skip it until T07 vendors it | Not in the pin |
| figure-it-out Phase D, commit the decision trail | Put the trail in the handoff and the PR body | No decisions folder yet (T04) |

**factory-reviewer**

| pstack says | factory job does | why |
| --- | --- | --- |
| interrogate, "Read the `interrogate reviewers` line" (multi-model) | Adversaries run on the reviewer's own model, research only | Max, "Reviewer helpers allowed, same model, research only". Builder and reviewer are already different families. |
| blast-radius step 6, "Ask more than one model" | Same model only | Same rule |
| interrogate, "Ask the user when the intent is unclear" | Read the criteria first. If intent still is not clear, BLOCKED with what is needed. | No live user. BLOCKED means the review could not run. |
| poteto-mode Subagents, "You own every subagent's work" | The reviewer alone writes the verdict | Kept as written. The row confirms it, it does not override. |

The build may add, drop or reword rows, as long as every override in the skill body has one.

## Work

### Unit 1. The lint, written first

`tests/test_router_skills.py`. It reads the three files from `ROOT` and the pin from `lib.pstack_pin.load_pin`.

- [ ] Every `<pstack>/<path>` in the three files is a key of `load_pin(ROOT).hashes()` and exists on disk. The set is non-empty and includes at least poteto-mode `SKILL.md`, `figure-it-out`, `interrogate`, `blast-radius`, `no-comments`, `principle-prove-it-works`, `playbooks/opening-a-pr.md`, `playbooks/feature.md`, `agents/comment-sicko.md` and `docs/harness/claude-code.md`. Evidence. The test fails before units 2 to 4 exist.
- [ ] Each skill has frontmatter whose `name` is its folder, and 40 to 80 lines.
- [ ] Each skill ends with a table whose header is `pstack says | factory job does | why`. Each row's first cell names a pinned `<pstack>/` path and a quote that appears in that file after whitespace is collapsed.
- [ ] Every `orch:v1` header line in the three files parses with `lib.handoff.parse_header_line`. This turns decision 1 into a check.
- [ ] `skills/factory/harness/codex.md` has the same `##` headings, in order, as `<pstack>/docs/harness/claude-code.md`.
- [ ] Prove the lint bites. In a scratch copy, break one path and one quote, run the test, paste both failures into the PR's verification notes, and discard the copy.

### Unit 2. Codex adapter

`skills/factory/harness/codex.md`, in the shape of the Claude adapter. The same intro paragraph form and the same sections, Tools, Subagent fields, Models, setup-pstack, Paths and Cursor-only features, with a `Cursor | Codex` table in each.

- [ ] Ground each row in the Codex CLI the factory runs. Use the `openai/codex` source or docs for that version, and record the version in the file. A row the build cannot ground says so in the row, and is not guessed. The Codex reviewer checks rows against its own tool list.
- [ ] `AskQuestion` maps to a `questions` handoff, because `codex exec` has no live user.
- [ ] The skip line is `Skipped in Codex: <step>. <reason>.`
- [ ] Models. Codex jobs on the brief's model lines. Until T10, Astra for the job and Sol for helpers.
- [ ] Unit 1 heading and path checks pass for this file.

### Unit 3. factory-builder

`skills/factory/factory-builder/SKILL.md`.

- [ ] Opening line. "Read `<pstack>/skills/poteto-mode/SKILL.md` in full, then the playbook below, at the absolute paths the brief gives." Then the harness adapter, `<pstack>/docs/harness/claude-code.md` for Claude or `skills/factory/harness/codex.md` for Codex.
- [ ] Playbook. Always `<pstack>/skills/figure-it-out/SKILL.md`.
- [ ] Human touchpoints. "The operator", `AskQuestion` and Always-pause become a `questions` handoff. Max's gates are in the brief.
- [ ] `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md` as written. Small ordered commits, pstack's PR body sections, edit the existing PR, `#<issue>: ` title prefix, rebase any time on the ticket branch with `--force-with-lease`, never the base, brief wins (decision 2).
- [ ] Skip only steps that need a missing Cursor feature, with the adapter's own skip line.
- [ ] Helpers per `<pstack>/skills/poteto-mode/playbooks/feature.md` step 4, on the job's harness and the brief's model lines. Helpers never post to GitHub and never push. Only the main job pushes.
- [ ] Self-check before `ready`. `<pstack>/skills/principle-prove-it-works/SKILL.md`, `deslop` (skipped until T07), and `<pstack>/skills/no-comments/SKILL.md` with `<pstack>/agents/comment-sicko.md`.
- [ ] The handoff lists every SKILL.md read under "Skills read:".
- [ ] The mapping table closes the file. Unit 1 passes for this file.

### Unit 4. factory-reviewer

`skills/factory/factory-reviewer/SKILL.md`.

- [ ] Read the criteria first.
- [ ] Run `<pstack>/skills/interrogate/SKILL.md` and `<pstack>/skills/blast-radius/SKILL.md`. Helpers are same-model and research only (decision 4). The reviewer alone writes the verdict.
- [ ] Run the verify skill when the brief names one.
- [ ] From round 2, confirm earlier blockers are closed and review the delta. A new finding in unchanged code is minor unless it fails a listed criterion. Say T15 will spell this rule out.
- [ ] Same problem twice means the same acceptance criterion or the same finding. Write `repeat=<id>` as the first body line under a valid verdict header (decision 1), stop, and ask Max. Do not loop.
- [ ] BLOCKED only when the review could not run at all. Say what is needed, stop, and tell Max.
- [ ] The mapping table closes the file. Unit 1 passes for this file.

### Unit 5. Ship the PR

- [ ] `git diff --stat origin/main -- skills/pstack skills/skills.lock` is empty, and `tests/test_pstack_pin.py` passes (`verify_pin(ROOT) == []`).
- [ ] `env -u ORCH_DB python -m pytest` passes in full. CI is green on the head.
- [ ] Commits are small and ordered, one per unit. PR #125 title starts with `#121: `. The body uses pstack's sections.
- [ ] The `ready` handoff names the head and lists "Skills read:".

## Throughput checkpoint

- **Blocking first steps.** Unit 1, so each later unit has a check to pass.
- **Independent workstreams.** Units 2, 3 and 4 touch disjoint files and can go to three helpers on the job's harness.
- **Shared mutable state.** None besides the branch. Helpers return diffs and the main job commits, so only one writer pushes.
- **Smallest safe decomposition.** One helper per file. The main job reviews each against unit 1 before it commits.

## Acceptance criteria map

| Criterion | Met by | Evidence |
| --- | --- | --- |
| The lint test passes. Every pstack file named exists in the pin. | Unit 1, first box, with units 2 to 4 | `pytest tests/test_router_skills.py` green, and the deliberate-break run fails |
| Neither skill contradicts a pstack rule without naming the pstack line it maps, with a "pstack says / factory job does / why" table at the bottom | Units 3 and 4 tables, unit 1 quote check | Lint green. The quote check fails on an invented quote. |
| No vendored pstack file changes | Unit 5, first box | Empty `git diff --stat`, `verify_pin` clean |
| Builder skill uses `figure-it-out`, allows helper agents, allows rebase any time on the ticket branch | Unit 3 | The skill text, checked by the reviewer |
| Reviewer skill: same problem twice → `repeat=` and stop; BLOCKED = could not review; helpers same-model research only; reviewer writes the verdict | Unit 4, decision 1 | The skill text, and the header check in unit 1 |
| Skills point at exact pstack `SKILL.md` paths, not bare names | Units 3 and 4, unit 1 path check | Lint green, required-path list in unit 1 |
| Factory-owned Codex adapter at `skills/factory/harness/codex.md` in the shape of pstack's Claude adapter | Unit 2, unit 1 heading check | Lint green |

## Out of scope

Installing the skills on devices (T07). Wiring them into briefs (T09). The model map (T10). Vendoring `deslop` (T07). Any dispatcher, feeder, waiter or `lib/handoff.py` change, including a `repeat` header key, which is a follow-up.
