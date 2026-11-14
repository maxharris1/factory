# Plan for #226: Merge-only jobs: run touched tests, push, and never poll CI

PR #228, branch `factory/issue-226`, base `main`, risk low.

## What is there today

`lib/brief/merge_only.py` `task_lines()` returns five lines plus `builder_notes(job)`:

```
Merge-only job: bring `origin/{base}` into the PR branch, nothing else.
1. Check out the PR branch at {head} and run `git fetch origin {base}`.
2. `git merge origin/{base}`: a merge commit only, no rebase, no force-push.
3. Resolve any conflict without changing behavior, then run the tests.
4. Push, then post the ready handoff below with the head you pushed.
```

Step 3 says "run the tests" with no limit, so builders run the whole suite. Nothing says not to
wait on CI. The rendered brief is pinned byte-for-byte by `tests/snapshots/brief/merge-only.txt`
(two variants in that file, `box` and `remote (mac)`, both from `tests/regenerate_brief_snapshots.py`)
through `tests/test_brief_snapshot.py`.

## Change

### 1. `lib/brief/merge_only.py` (the only source file touched)

Replace steps 3 and 4 and add step 5. The list keeps `*builder_notes(job)` at the end unchanged.
The new task lines:

```
Merge-only job: bring `origin/{base}` into the PR branch, nothing else.
1. Check out the PR branch at {head} and run `git fetch origin {base}`.
2. `git merge origin/{base}`: a merge commit only, no rebase, no force-push.
3. Resolve any conflict without changing behavior. Change nothing besides the merge resolution.
4. Run only the tests affected by the files you touched while resolving the merge, plus a short smoke run. Do not run the full suite unless those affected tests are the full suite. With no conflicts to resolve, the smoke run is enough.
5. Push, post the ready handoff below with the head you pushed, and finish the job.
Do not poll, watch or wait on GitHub Actions or any other CI from inside this job: the factory's own review and CI gate check CI after `ready`.
```

Notes on the wording:
- "a merge commit only, no rebase, no force-push" stays word for word (step 2), so the existing
  `test_merge_only_brief_merges_the_base_branch_without_rewriting_history` in `tests/test_brief.py`
  keeps passing without being edited.
- "finish the job" points at the existing `## Finish the job` section, which the brief already
  renders (`orch job done ... --handoff`, or the `ORCH-HANDOFF:` line on a remote device).
- The do-not-poll sentence is its own line so a test can match it exactly.
- "With no conflicts to resolve, the smoke run is enough" covers the clean-merge case, where no
  files were touched by hand. Small call within the ticket's "affected tests plus a short smoke
  run"; drop it if the reviewer reads it as scope creep.
- The `builder_notes` line ("Run test suites in the foreground...") is shared with the build
  brief and stays as it is (ticket: leave `builder_notes` alone).

### 2. `tests/snapshots/brief/merge-only.txt`

Regenerate with `python tests/regenerate_brief_snapshots.py` (the file says snapshots are
regenerated, never edited by hand). Only the `## What to do` block of both variants should change;
`git diff` of the other snapshots must be empty.

### 3. `tests/test_merge_only_brief.py` (new file; `tests/test_brief.py` is not edited)

Renders the brief through `tests.regenerate_brief_snapshots.render_snapshot("merge-only")`
(both box and remote variants) and through `render_brief` for a plain merge-only job. Tests:

- `test_runs_only_affected_tests_plus_a_smoke_run`: the brief names "only the tests affected by
  the files you touched while resolving the merge", "a short smoke run" and "Do not run the full
  suite", and no longer contains the bare "then run the tests.".
- `test_says_not_to_poll_or_wait_on_ci`: the exact do-not-poll line is in the `## What to do`
  section of every variant.
- `test_pushes_posts_ready_and_finishes`: the push / ready / finish step is there, and the
  `## Finish the job` section still follows.
- `test_keeps_merge_commit_no_rebase_no_force_push`: "a merge commit only, no rebase, no
  force-push" is in the task, and the Rules line "No force-push and no rebase of a pushed
  branch. Bring a base branch in by a merge commit only." is still there.
- `test_task_steps_stay_in_order`: fetch, merge, resolve, tests, push appear in that order.

### 4. Checks before `ready`

- `env -u ORCH_DB` pytest on the touched tests: `tests/test_merge_only_brief.py`,
  `tests/test_brief_snapshot.py`, `tests/test_brief.py`, `tests/test_brief_rules.py`.
- The four `check-config` steps from `.github/workflows/ci.yml`.
- `git diff --stat origin/main` lists only `lib/brief/merge_only.py`,
  `tests/snapshots/brief/merge-only.txt`, `tests/test_merge_only_brief.py` and `plan.md`.

## Acceptance criteria map

| Criterion | Work that meets it | Proof |
|---|---|---|
| The brief tells the builder to run only the tests affected by the merge-resolution files, plus a short smoke run, not the full suite by default. | Step 4 of the new task lines (section 1). | `test_runs_only_affected_tests_plus_a_smoke_run`; regenerated snapshot. |
| The brief tells the builder to push, post `ready`, and finish the job, and explicitly not to poll or wait on CI. | Step 5 and the do-not-poll line (section 1). | `test_pushes_posts_ready_and_finishes`, `test_says_not_to_poll_or_wait_on_ci`; snapshot. |
| Merge-commit, no-rebase, and no-force-push wording remains. Snapshot and new tests pass. `orch check-config` still passes. | Step 2 kept word for word; Rules section untouched; snapshot regenerated (section 2); new test file (section 3). | `test_keeps_merge_commit_no_rebase_no_force_push`, the unchanged `tests/test_brief.py` merge-only test, `tests/test_brief_snapshot.py`, the four `check-config` runs (section 4), CI. |
| No dispatcher, runner, CI-gate, `lib/brief/common.py`, or `lib/brief/build.py` changes. | Only the three files above (plus `plan.md`) change. | `git diff --stat origin/main` in section 4; the reviewer reads the diff. |

## Out of scope

`builder_notes`, `lib/brief/common.py`, `lib/brief/build.py`, `tests/test_brief.py` and the other
brief snapshots (PR #116 owns those), the dispatcher, the CI gate and the runner.
