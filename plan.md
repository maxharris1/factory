# Plan: #159, router line edits (T07a)

## Goal

Make the factory builder and reviewer routers say what the factory does today. PRs stay draft until APPROVE. The trail has a per-ticket path. Fix jobs take the existing plan as Phase A. Titles have one stated shape. Model names come from the brief. The reviewer knows which poteto-mode triggers to apply. The smallest change is line edits in three Markdown files and no code.

## Scaffold (architect, Phase A: ground)

The only code that reads these files is `tests/test_router_skills.py`. Its rules shape every edit:

- Each router must stay 40 to 80 lines long. The builder has 58 lines now and the reviewer has 51. This plan adds about 2 lines to each.
- Each table row must name exactly one `<pstack>/` file and quote a line that is in that pinned file (whitespace collapsed). New rows quote lines checked below.
- The mapping table must be the last thing in each router. New rows go inside the table.
- `codex.md` must keep the same `## ` headings as the Claude adapter. This plan edits one table cell only.

Pinned lines the new rows quote (checked against `skills/pstack`):

- `<pstack>/skills/poteto-mode/playbooks/opening-a-pr.md`, "Open every PR ready, never as a draft." (Readiness paragraph)
- `<pstack>/skills/poteto-mode/SKILL.md`, "The Principles section below grounds every trigger." (line 15)
- The existing quotes in rows 56 and 58 stay as they are.

The feeder opens the draft PR at `lib/feeder.py:576` (`open_draft_pull`). `docs/decisions/README.md` sets the one-file-per-ticket rule (`<issue>-…`) that the trail path follows.

## Edits

### E1. `skills/factory/factory-builder/SKILL.md`

1. **Line 8.** Replace "Until the brief names it, it is this repo's `skills/pstack`." with "It is the deployed factory checkout's `skills/pstack`, never the job's worktree."
2. **Line 14.** Keep the sentence about "Careful playbook always". Add: "Fix and CI-fix jobs take the existing plan and acceptance criteria as Phase A and run at the lowest rigor."
3. **Line 26.** Replace the two model sentences ("Take model lines from the brief. Until the brief carries them (T10), … Sol helpers.") with "Take model lines from the brief." No model or setting names remain.
4. **Line 33.** Remove the separate title-prefix wording. The bullet becomes "Title per the table below. Use pstack's body sections and keep the `Fixes #<issue>` line." The title shape is then stated only once.
5. **Line 34.** Delete the `gh pr ready` bullet.
6. **New table row (draft until APPROVE).**
   `| <pstack>/skills/poteto-mode/playbooks/opening-a-pr.md, "Open every PR ready, never as a draft." | Leave the PR draft and never mark it ready yourself. The poller marks it ready on APPROVE. | pstack opens a PR when the work is done. The feeder opens it before any work exists (lib/feeder.py:576). On Aut0, marking ready is the handoff to people. |`
7. **Row 56 (title).** The middle cell becomes: "Title is `#<issue>: type(scope): subject`, for example `#115: fix(titles): …`. Edit the feeder's PR." The why cell: "Max's rule, factory #115. The `#<issue>: ` prefix goes before the Conventional Commits title and does not replace it. The feeder opened the PR."
8. **Row 58 (trail).** The middle cell becomes "Commit the trail to `docs/decisions/<issue>-trail.tsv`". The why cell: "One file per ticket, so two PRs never edit the same lines (`docs/decisions/README.md`)." The old "No decisions folder until T04" goes.

### E2. `skills/factory/factory-reviewer/SKILL.md`

1. **Line 8.** The same replacement as E1.1.
2. **New table row (triggers).**
   `| <pstack>/skills/poteto-mode/SKILL.md, "The Principles section below grounds every trigger." | Apply three triggers: restate the intent, prove it with a run, unslop the verdict | A reviewer writes no code, so the build triggers do not apply. These three keep the verdict tied to the criteria and to a run. |`

### E3. `skills/factory/harness/codex.md`

1. **Line 37.** The Codex cell becomes "No Codex file. A factory job takes its model lines from the brief." The Astra and Sol names go.

## Criterion map

| # | Acceptance criterion | Met by |
| --- | --- | --- |
| 1 | No `gh pr ready` instruction. The draft-until-APPROVE row with the Aut0 reason exists. | E1.5 deletes line 34. E1.6 adds the row: feeder at `lib/feeder.py:576`, Aut0 handoff reason. Check: `grep -rn "gh pr ready" skills/factory` is empty. |
| 2 | The trail row names the per-ticket path and no fixed file name. Neither router says "this repo's skills/pstack". | E1.8 (`docs/decisions/<issue>-trail.tsv`). E1.1 and E2.1. Check: `grep -rn "this repo's" skills/factory` is empty. |
| 3 | The reviewer's triggers row exists. The fix-job Phase A sentence exists. | E2.2 and E1.2. |
| 4 | No model names remain in factory-builder line 26 or in `codex.md` line 37. Both point at the brief. | E1.3 and E3.1. Check: `grep -nE "Astra|Sol|Opus|Sonnet|grok|gpt" skills/factory/factory-builder/SKILL.md skills/factory/harness/codex.md` shows no hit on those lines. |
| 5 | The title shape `#<issue>: type(scope): subject` is stated once, with the example, including in the former Conventional Commits row. | E1.7 states it in row 56 with `#115: fix(titles): …`. E1.4 removes the second statement at line 33. Check: `grep -c "type(scope): subject" skills/factory/factory-builder/SKILL.md` returns 1: row 56 holds both the pstack quote and the factory shape. |

## Verification

- `env -u ORCH_DB python -m pytest tests/test_router_skills.py` passes: line counts, quotes in the pin, table last, Codex headings.
- The full suite and the `check-config` and statechart steps from `.github/workflows/ci.yml` pass.
- The grep checks in the criterion map, with their output pasted in the `ready` handoff.

## Scope and risk

- Three Markdown files. No code and no test changes. Risk is low: the routers are read by agents, and the test above guards their structure.
- `docs/decisions/README.md` names `<issue>-<slug>.md` files. A `.tsv` trail with slug `trail` is a small extension of that rule. Updating the README is outside this ticket's file list and is left for a follow-up if the reviewer wants it.
- Rejected: adding guard assertions to `tests/test_router_skills.py` for the removed strings. The ticket lists three files, and the grep checks give the same proof for this PR.
