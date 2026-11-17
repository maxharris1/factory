- **Decision:** Factory CI (`.github/workflows/ci.yml`) runs on self-hosted runners. Linux jobs use the `factory-linux` pool (3 PC WSL instances and 1 low-priority box instance), named in `factory.md` as `ci_runner: factory-linux`. The `macos` job uses the Mac runner (`factory-mac`). Merges still need green checks on the current head. Cleffy stays on hosted runners.
- **When:** 2026-11-17 9:30 AM ET, by Max
- **Source:** https://github.com/maxharris1/factory/pull/712#issuecomment-6951041562 (the go), on the plan at https://github.com/maxharris1/factory/pull/712#issuecomment-6950231924

Max approved the plan's defaults and changed one of them. The box runner comes now, not after a week on the PC. `grokbot-factory-box` gets no runner. The aut0 and Tracy follow-ups were not approved, and nothing here touches #708 or moves WSL. Max registers each runner himself from [docs/F2-03-RUNNER-CHECKLIST.md](../F2-03-RUNNER-CHECKLIST.md).

## Merge rule

Unchanged. The poller merges an approved head only when every check run on it completed, none failed and at least one succeeded (`ci_status()` in `lib/github_actions.py`). A job waiting for an offline runner is queued, so the head stays `pending` and does not merge. A fork PR's jobs are all skipped by the fork guard, which also reads as `pending`.

GitHub branch protection and rulesets are not available on this private repo on the current plan (the API returns 403), so the poller is the only enforcer.

## Security

- **Fork PRs.** The repo setting "Run workflows from fork pull requests" stays off. That is the control. A `pull_request` run uses the workflow file from the PR head, so a fork could delete the fork guard `if:` on every job. The guard covers only the case where someone turns the setting on by mistake.
- **No credentials on self-hosted jobs (D21).** No factory CI job reads a repository secret. `macos-pickup` uses the job's own short-lived `GITHUB_TOKEN` with `actions: write` to cancel its run, and Max chose to keep it. `tests/test_ci_workflow.py` fails if any self-hosted job references another secret, uses an `environment:`, or has `id-token: write`, and if a workflow with self-hosted jobs triggers on `pull_request_target` or `workflow_run`.
- **Runner users.** Each instance runs as its own user, with no `docker` or `sudo` group and no bot credentials. Bot homes are mode 700.
- **Persistent runners.** A job can leave files for the next job on the same instance, so a malicious same-repo PR could make a later run pass falsely. Ephemeral runners would need a service that holds a registration token, which the SPEC rules out. Only the bots and Max push same-repo branches, so this risk is accepted and named here.
- **Public repos** stay off self-hosted runners. Cleffy is public and its hosted Linux minutes are free.

## Billing

GitHub's docs, read 2026-11-17:

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) lists self-hosted runners as usage that "remains free independently and does not reduce the included compute usage", and says that without a valid payment method "usage is blocked once you consume the included compute usage".
- [Billing and usage](https://docs.github.com/en/actions/concepts/billing-and-usage): usage is free "for standard GitHub-hosted runners in public repositories, and for self-hosted runners."
- [2026 pricing changes](https://github.com/resources/insights/2026-pricing-changes-for-github-actions): the announced $0.002 a minute self-hosted platform charge is postponed, with no new date.

Whether GitHub starts self-hosted jobs during a payment or spending-limit block is not stated in those docs. Max fixed billing before this build, so the build PR's run cannot test that case. If the block returns, the first self-hosted run after it is the test. Pass means the job logs show a runner name and the run finishes. Fail means the jobs fail in seconds with the billing annotation and no runner name.

Cost estimate (measured run counts, Nov 1 to 16): about 116 factory runs a day at about 29 Linux job-minutes each, about 85,000 minutes a month, or about $500 a month at hosted Linux rates after the included minutes. Self-hosted, GitHub charges $0 under today's terms (about $170 a month if the postponed platform charge returns). The load moves to the PC and the box: about 112 core-hours a day, about 20% of the PC's 24 cores on average. Power is not measured.

## Capacity

Runner instances are not factory slots, and `machines.yml` and `orch.yml` are unchanged. The PC instances run under systemd limits (`Nice=10`, `CPUWeight=20`, `CPUQuota=400%`, `MemoryMax=6G`, IOPS caps on the VHDX device). The box has no systemd, so `box-1` runs pinned to 2 cores at `nice 19`, one job at a time. The test shards run `pytest -n 4`, the worker count a 2-core hosted runner used, not `nproc * 2`, which would start 48 workers per shard on the PC. The checklist's first-week numbers decide whether instances go up or down.

## Fallback merge rule (not enabled)

This applies only while hosted CI is down **and** no self-hosted runner can run factory CI, and only after Max's explicit OK on the ticket naming the PRs and a time limit.

1. Max disables the `ci` workflow (`gh workflow disable ci`), so no new check runs are created.
2. A PR to `factory.md` sets `ci: {branches: [ci-paused], local_checks: [pytest, check-config, statechart]}`, so `main` is not a CI base. CI is then `not-run`, the builder pastes each local check's command and result at the head in its `ready`, and a missing paste is a blocking review finding (#615). The poller reads `factory.md` from `main`, so Max merges this one PR himself, which is the first use of the fallback.
3. `not-run` needs a head with no check runs. Each PR that uses the fallback pushes a new commit after step 1.
4. To end it, revert step 2 and run `gh workflow enable ci`. List every PR merged under the fallback on the ticket.
