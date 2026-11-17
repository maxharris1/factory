- **Decision:** The 28-minute no-step `macos` cancel was a hosted macOS runner availability failure. It was not `timeout-minutes` and it was not a concurrency cancel. A new `macos-pickup` job in `.github/workflows/ci.yml` watches the `macos` job. If no runner picks the job up within 10 minutes of its creation, `macos-pickup` posts a `macos runner pickup` error, cancels the run and fails.
- **When:** 2026-11-16 7:50 PM ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/628

## Evidence

The job is `macos` in run 43900570415, attempt 1, on head `ac22adab785df32c4f4024ef7b05416128c60d4a` (job 133415718662). The figures below come from `gh api repos/maxharris1/factory/actions/jobs/133415718662`, its check-run annotations, and the run's job list.

- The job was created at 15:49:22Z and completed `cancelled` at 16:18:01Z. `runner_name` is empty and `steps` is `[]`, so no runner ever took the job.
- The failure annotation reads "The job was not acquired by Runner of type hosted even after multiple attempts". Three notice annotations read "Due to capacity constraints, jobs targeting macOS arm64 runners may experience longer queue times." So GitHub gave up on the job itself because it found no hosted macOS runner.
- Not `timeout-minutes`. The job already had `timeout-minutes: 15`, and the cancel came at 28m39s. A job's `timeout-minutes` counts only once a runner picks up the job, so queue time never reaches it.
- Not a concurrency cancel. The run is the only `ci` run on branch `docs/mission-control-round2`, so no newer run took over the PR's concurrency group. The other six jobs in the same attempt got Ubuntu runners within 2 to 6 minutes and passed, and the run was not cancelled. Attempt 2 of the same run passed.

## Why a watchdog job

No GitHub Actions setting bounds how long a job waits for a runner. A job's `timeout-minutes` starts at pickup, and a queued job waits up to 24 hours unless GitHub gives up first, as it did here. So an Ubuntu job polls the run's jobs API and counts from the `macos` job's `created_at`, not from its own start, because it can wait a few minutes for its own runner. The dispatcher treats a `cancelled` check the same as a failed one (`_CHECK_FAILED` in `lib/github_actions.py`). So cancelling the run ends it at once, and a red CI still reaches the builder. To retry, rerun the failed jobs.
