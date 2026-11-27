- **Decision:** Factory CI (`.github/workflows/ci.yml`) runs on self-hosted runners. Linux jobs use the `factory-linux` pool, the 3 PC WSL instances `pc-wsl-bots-1` to `-3`, named in `factory.md` as `ci_runner: factory-linux`. Each run routes its own Linux jobs. The hosted `changes` job runs `lib/ci_route.py`, and each Linux job runs on `ubuntu-latest` only when no pool slot is free for it. The `macos` job uses the Mac runner (`maxs-macbook-pro`, label `factory-mac`) and never overflows. Merges still need green checks on the current head. Cleffy stays on hosted runners.
- **When:** 2026-11-17 9:30 AM ET, by Max
- **Source:** https://github.com/maxharris1/factory/pull/712#issuecomment-6951041562 (the go), on the plan at https://github.com/maxharris1/factory/pull/712#issuecomment-6950231924. Overflow: Max, 10:47 AM ET, relayed in https://github.com/maxharris1/factory/pull/712#issuecomment-6953668097

Max approved the plan's defaults and changed one of them. The box runner comes now, not after a week on the PC. The aut0 and Tracy follow-ups were not approved, and nothing here touches #708 or moves WSL. At 10:47 AM ET Max said: "Do whatever you need to do on either of my machines to set up the runners and get them wired into the factory, so we default to the local runners before we overflow to GitHub." Under that grant, Grok Bot registered `pc-wsl-bots-1` to `-3`, a box runner and `maxs-macbook-pro` with short-lived registration tokens piped straight into `config.sh`. No token was printed or stored. Later registrations follow [docs/F2-03-RUNNER-CHECKLIST.md](../F2-03-RUNNER-CHECKLIST.md), by Max.

**Update, 2026-11-19 (#996).** Max retired the box runner and moved the sync to the PC at 10:00 AM ET. The box runner is deregistered and its starter removed. It never carried the `factory-linux` label after the label change, so it was a dead fourth slot. The pool is the 3 PC instances, and the Mac runs `macos`.

**Update, 2026-11-19 (#1000).** Max asked to "fix the instability issues": the sync flipped `FACTORY_LINUX_RUNNER` 335 times in two days. The sync now publishes only the online pool size, as `FACTORY_LINUX_POOL`, and no longer writes `FACTORY_LINUX_RUNNER`. Wiring a per-run router that reads it into CI is #1012. Until then, Max deletes `FACTORY_LINUX_RUNNER` once the new loop runs, and every Linux job runs on the pool with no overflow.

**Update, 2026-11-19 (#1012).** Max asked that each factory Linux CI job take a free local `factory-linux` runner first and go to `ubuntu-latest` only when no local slot is free. A hosted `changes` job now routes each run from `FACTORY_LINUX_POOL` and the Actions runs API, and writes the slots that go hosted to its `hosted` output. This replaces the repo-wide `FACTORY_LINUX_RUNNER` switch. Nothing reads that variable any more, and Max deletes it with `gh variable delete FACTORY_LINUX_RUNNER --repo maxharris1/factory`.

**Update, 2026-11-27 (#1778).** The three local test shards at `pytest -n 4` ran 12 workers and loaded the orch host (the PC) to about 14. Local shards now run `pytest -n 2` and hosted shards keep `-n 4`. The `test` job's timeout went from 15 to 25 minutes. See Capacity.

## Overflow to hosted runners

Each Linux job takes a free local `factory-linux` runner first and runs on `ubuntu-latest` only when no local slot is free. Each Linux job has `needs: changes`. The `checks` job says:

```yaml
runs-on: ${{ contains(fromJSON(needs.changes.outputs.hosted || '[]'), 'checks') && 'ubuntu-latest' || fromJSON('["self-hosted","Linux","X64","factory-linux"]') }}
```

The test shards use `format('test-{0}', matrix.shard)` in place of `'checks'`. An empty or missing `hosted` output means the pool, so local is the default.

The `changes` job runs on `ubuntu-latest` on every event, with `actions: read` and `contents: read` and only `github.token`. Its step `route` runs `python3 -I lib/ci_route.py`. The router reads `vars.FACTORY_LINUX_POOL` and the runs API, writes the output `hosted` as a JSON list of slot names, and logs `ci_route: online=N claimed=C reserved=R free=F hosted=[...]`. `SLOTS` ranks the slots: `test-1`, `test-2`, `test-3`, `statechart`, `checks`, `py311-compile`, `macos-pickup`. The router computes `free = online - claimed - reserved`. `claimed` counts other runs' pool jobs that are queued or in progress. After `route`, the `changes` job runs one step `local <slot>` per slot, and each step succeeds only when `route` kept its slot local. `reserved` covers each other run that lists `changes` but no slot jobs yet. A run whose `local <slot>` steps have all finished reserves the slots it kept local, so a run that sent every slot hosted reserves none. An earlier run whose steps have not all finished has not routed yet and reserves all 7 slots. Jobs in the same run count by rank, so the first `free` slots run local and the rest go hosted.

[`deploy/runner-pool-sync.sh`](../../deploy/runner-pool-sync.sh) runs on the orch host (the PC) as the `factory` user, from the deployed checkout. Every 30 s it reads the runner list and counts the online `factory-linux` runners. It publishes the count as the repo Actions variable `FACTORY_LINUX_POOL`, as `{"online":N}`. It writes `FACTORY_LINUX_POOL` only when the online count changes and the new count holds for two checks in a row. It never reads whether a runner is busy. A busy or idle change never writes it, so the router counts busy runners itself from the runs API. A failed runner list writes nothing. The runner list is read with the `factory` user's own `gh` (repo admin), so no secret is added to Actions.

- **Stall guard.** If a pool job has been queued over 3 minutes and no pool job is running, `free` is 0 and every slot goes hosted.
- **Router failure.** If the router fails, for example on a missing environment variable or an API error, it writes `hosted=[]` and warns, so every job stays local. If the `changes` job itself fails, every Linux job is skipped and the run goes red.
- **Reruns.** "Re-run failed jobs" reuses the earlier `changes` output, so each job goes back where it ran. A rerun local job waits for the pool even when the pool is full, with no stall guard. "Re-run all jobs" reruns `changes` and routes every job again.
- **Pin hosted by hand.** Setting `FACTORY_LINUX_POOL` to `{"online":0}` sends every new run's Linux jobs to `ubuntu-latest`. The PC sync overwrites it at its next held change of the online count.
- **macOS** has no overflow. `macos` stays on `[self-hosted, macOS, factory-mac]`, and `macos-pickup` still cancels the run red after 10 minutes. `macos-pickup` itself is routed like the other Linux jobs.
- **Billing.** `changes` is one billed hosted job per run, about 1 minute, or about 3,500 to 6,000 minutes a month at current volume. An overflowed job is billed like any hosted job. While the account is billing-blocked, `changes` cannot start, so every run goes red even with the pool idle.
- **D21 still holds.** The credential checks in `tests/test_ci_workflow.py` apply to every job that can run on a self-hosted runner. `changes` is the one job that always runs hosted.
- **Restarts.** The `factory` user's crontab on the PC runs `runner-pool-sync.sh ensure`, which starts the loop unless a running loop holds its `flock` lock. The kernel drops the lock when the loop exits. A hold file (`~/.config/factory/runner-pool-sync-hold`) stops the loop. The checklist has the commands.

## Mac runner user

`maxs-macbook-pro` runs as `gharunner`, a dedicated runner user, not a bot login. The `macos` job on head `25396e56` printed `runner maxs-macbook-pro as gharunner` in its "Runner identity" step. Max's remaining check is the checklist's Mac isolation check: `gharunner` holds no `admin` group.

## Merge rule

Unchanged. The poller merges an approved head only when every check run on it completed, none failed and at least one succeeded (`ci_status()` in `lib/github_actions.py`). A job waiting for an offline runner is queued, so the head stays `pending` and does not merge. A fork PR's jobs are all skipped by the fork guard, which also reads as `pending`.

GitHub branch protection and rulesets are not available on this private repo on the current plan (the API returns 403), so the poller is the only enforcer.

## Security

- **Fork PRs.** The repo setting "Run workflows from fork pull requests" stays off. That is the control. A `pull_request` run uses the workflow file from the PR head, so a fork could delete the fork guard `if:` on every job. The guard covers only the case where someone turns the setting on by mistake.
- **No credentials on self-hosted jobs (D21).** No factory CI job reads a repository secret. `macos-pickup` uses the job's own short-lived `GITHUB_TOKEN` with `actions: write` to cancel its run, and Max chose to keep it. `tests/test_ci_workflow.py` fails if any self-hosted job references another secret, uses an `environment:`, or has `id-token: write`, and if a workflow with self-hosted jobs triggers on `pull_request_target` or `workflow_run`. The one job that always runs hosted is `changes`, and it holds only `github.token` with `actions: read` and `contents: read`. No self-hosted job reads `vars.FACTORY_LINUX_POOL` or calls the runners API.
- **Runner users.** Each instance runs as its own user, with no `docker` or `sudo` group and no bot credentials. Bot homes are mode 700.
- **Persistent runners.** A job can leave files for the next job on the same instance, so a malicious same-repo PR could make a later run pass falsely. Ephemeral runners would need a service that holds a registration token, which the SPEC rules out. Only the bots and Max push same-repo branches, so this risk is accepted and named here.
- **Public repos** stay off self-hosted runners. Cleffy is public and its hosted Linux minutes are free.

## Billing

GitHub's docs, read 2026-11-17:

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) lists self-hosted runners as usage that "remains free independently and does not reduce the included compute usage", and says that without a valid payment method "usage is blocked once you consume the included compute usage".
- [Billing and usage](https://docs.github.com/en/actions/concepts/billing-and-usage): usage is free "for standard GitHub-hosted runners in public repositories, and for self-hosted runners."
- [2026 pricing changes](https://github.com/resources/insights/2026-pricing-changes-for-github-actions): the announced $0.002 a minute self-hosted platform charge is postponed, with no new date.

Whether GitHub starts self-hosted jobs during a payment or spending-limit block is not stated in those docs. Max fixed billing before this build, so the build PR's run cannot test that case. If the block returns, the first self-hosted run after it is the test. Pass means the job logs show a runner name and the run finishes. Fail means the jobs fail in seconds with the billing annotation and no runner name. That live check is Max's follow-up, not a merge gate (ac-9 as Max amended it on 2026-11-17). Record its result on #711.

Cost estimate (measured run counts, Nov 1 to 16): about 116 factory runs a day at about 29 Linux job-minutes each, about 85,000 minutes a month, or about $500 a month at hosted Linux rates after the included minutes. Self-hosted, GitHub charges $0 under today's terms (about $170 a month if the postponed platform charge returns). The load moves to the PC: about 112 core-hours a day, about 20% of the PC's 24 cores on average. Power is not measured.

## Capacity

Runner instances are not factory slots, and `machines.yml` and `orch.yml` are unchanged. The PC instances run under systemd limits (`Nice=10`, `CPUWeight=20`, `CPUQuota=400%`, `MemoryMax=6G`, IOPS caps on the VHDX device). The test shards run `pytest -n 2` on the self-hosted pool and `pytest -n 4` on `ubuntu-latest`. Each shard picks its count from `runner.environment`, so the three local shards start at most 6 workers. 12 workers (3 shards at `-n 4`) were measured at a load of about 14 on the orch host. Neither count is `nproc * 2`, which would start 48 workers per shard on the PC. Local shards run slower, so the `test` job's timeout is 25 minutes. The checklist's first-week numbers decide whether instances go up or down.

## Fallback merge rule (not enabled)

This applies only while hosted CI is down **and** no self-hosted runner can run factory CI, and only after Max's explicit OK on the ticket naming the PRs and a time limit.

1. Max disables the `ci` workflow (`gh workflow disable ci`), so no new check runs are created.
2. A PR to `factory.md` sets `ci: {branches: [ci-paused], local_checks: [pytest, check-config, statechart]}`, so `main` is not a CI base. CI is then `not-run`, the builder pastes each local check's command and result at the head in its `ready`, and a missing paste is a blocking review finding (#615). The poller reads `factory.md` from `main`, so Max merges this one PR himself, which is the first use of the fallback.
3. `not-run` needs a head with no check runs. Each PR that uses the fallback pushes a new commit after step 1.
4. To end it, revert step 2 and run `gh workflow enable ci`. List every PR merged under the fallback on the ticket.
