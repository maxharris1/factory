# F2-03 self-hosted runner checklist (for Max)

This is the registration checklist from docs/SPEC.md F2-03 and docs/PLAN.md
Phase 2 item 17, updated for #711 (see
[docs/decisions/711-self-hosted-runners.md](decisions/711-self-hosted-runners.md)).
**Max does every step that touches a token.** The factory never creates,
requests, reads, stores or passes a runner registration or removal token,
never reads a runner's `.runner`, `.credentials*` or `.env` files, and never
registers or removes a runner through the API (SPEC section 10 "Security":
Max registers self-hosted runners himself; never read runner tokens).
This document contains no token and no placeholder for one. The token only
ever appears on GitHub's "New self-hosted runner" page and in the terminal
where Max pastes the command from that page.

**Registered on 2026-11-17 by Grok Bot, at Max's direction.** Max, 10:47 AM
ET: "Do whatever you need to do on either of my machines to set up the
runners and get them wired into the factory, so we default to the local
runners before we overflow to GitHub." Grok Bot registered all five runners
below with short-lived registration tokens piped straight into `config.sh`.
That was a one-time grant from Max. Any later registration or removal is
Max's again unless he says otherwise. The rule did not change: no token is
ever printed, stored, or pasted into git, an issue, a comment, a chat, a
file or orch config.

## What goes where

| Item | Where | Who |
|---|---|---|
| Runner registration and its token | GitHub UI and the runner host's terminal | Max only |
| Linux pool label for a repo | that repo's `factory.md` front matter as `ci_runner:` | a normal repo PR |
| Which jobs use it | that repo's workflow `runs-on:` | a normal repo PR |
| Overflow switch for factory's Linux jobs | repo Actions variable `FACTORY_LINUX_RUNNER` | `deploy/runner-pool-sync.sh` on the box |
| Device inventory (hosts, Tailscale names) | `machines.yml` (F2-01) | already there, unchanged |
| Credential-holding deploy workflows | GitHub-hosted runners, unchanged (D21) | nobody moves them |

Self-hosted runners are for non-credential checks only: tests, lint,
`check-config`, model checks. A workflow that holds a deploy credential,
cloud key or publish token stays on its hosted runner.
`tests/test_ci_workflow.py` fails if a self-hosted job references a
secret other than `GITHUB_TOKEN`, uses an `environment:`, or has
`id-token: write`.

## Labels

Every runner gets GitHub's automatic labels (`self-hosted`, the OS, the
architecture). Max adds a pool label and a host label at `config.sh` time:

| Host | `machines.yml` device | Runner names | Labels Max adds | Automatic labels |
|---|---|---|---|---|
| pc-wsl-bots (the PC's WSL Ubuntu node) | `pc` | `pc-wsl-bots-1`, `-2`, `-3` | `factory-linux,factory-pc-wsl` | `self-hosted`, `Linux`, `X64` |
| the box (orch host, Debian 13 x64) | `box` | `box-1` | `factory-linux,factory-box` | `self-hosted`, `Linux`, `X64` |
| Max's Mac | `mac` | `maxs-macbook-pro` | `factory-mac` | `self-hosted`, `macOS`, `ARM64` (or `X64` on an Intel Mac) |

`factory-linux` is the Linux pool. A repo names it in `factory.md`:

```yaml
ci_runner: factory-linux
```

`ci_runner` must be one token (letters, digits, `.`, `_`, `:`, `-`);
`orch check-config` validates it. Linux jobs select the pool with every
label, so a job can never land on some other self-hosted runner. They read
the labels from the repo Actions variable `FACTORY_LINUX_RUNNER`, and fall
back to the pool when it is unset:

```yaml
jobs:
  test:
    runs-on: ${{ fromJSON(vars.FACTORY_LINUX_RUNNER || '["self-hosted","Linux","X64","factory-linux"]') }}
```

The fallback is exactly the pool, `[self-hosted, Linux, X64, factory-linux]`,
so local is the default. See "Overflow to hosted runners" below for what
sets the variable.

The one macOS job uses `[self-hosted, macOS, factory-mac]`. The host labels
(`factory-pc-wsl`, `factory-box`) are for pinning a job to one host if that
is ever needed. No factory job pins today.

`ci_runner` is repo policy that `check-config` validates and humans and
bots read; the poller does not rewrite workflows. `tests/test_ci_workflow.py`
checks that every factory Linux job uses that `runs-on:` expression with the
`ci_runner` pool as its fallback, and that `macos` stays on
`[self-hosted, macOS, factory-mac]`.

## Overflow to hosted runners

Max asked on 2026-11-17 that CI "default to the local runners before we
overflow to GitHub". The switch is the repo Actions variable
`FACTORY_LINUX_RUNNER`. It holds either the pool,
`["self-hosted","Linux","X64","factory-linux"]`, or `"ubuntu-latest"`.

- **What flips it.** `deploy/runner-pool-sync.sh` runs on the box as the
  orch login (installed at `~/.local/bin/runner-pool-sync.sh`). Every 30 s
  it lists the repo's runners and counts the online `factory-linux` ones
  and how many of them are idle. It sets the variable to `"ubuntu-latest"`
  only after two checks in a row with no idle pool runner (or none online),
  and back to the pool on the first check with one idle. A failed runner
  list changes nothing. It reads the runner list with the orch login's own
  `gh` (repo admin), so no secret is added to Actions.
- **When it applies.** GitHub reads the variable when it queues each job.
  Jobs queued while the pool has room stay local. Jobs queued while it is
  full go to hosted runners. A job already queued for the pool waits for a
  pool runner even if the variable flips later.
- **macOS never overflows.** `macos` stays on `factory-mac`, and
  `macos-pickup` still cancels the run red after 10 minutes with no Mac.
- **Billing.** An overflowed job is a normal hosted job. It uses included
  minutes, and while the account is billing-blocked it fails in seconds,
  which turns the run red. Rerun failed jobs once the pool has room.
- **Log.** `~/.local/state/factory/runner-pool-sync.log` on the box, one
  line per flip (`online=N idle=N -> <value>`).
- **Restarts.** `runner-pool-sync.sh ensure` starts the loop in the
  background unless a running loop holds its lock
  (`flock` on `~/.local/state/factory/runner-pool-sync.lock`). The kernel
  drops the lock when the loop exits, so a crash or restart never leaves a
  stale claim, and a second loop exits at once. It is safe to run any time.
  Run it after a box restart, from the same place that runs
  `box-runner-ensure.sh`.
- **Hold it.** `touch ~/.config/factory/runner-pool-sync-hold` stops the
  loop at its next check and makes `ensure` a no-op. Remove the file and
  run `ensure` to resume.
- **Pin local or hosted by hand.** Hold the loop, then set the variable:
  `env -u GH_TOKEN gh variable set FACTORY_LINUX_RUNNER --repo maxharris1/factory --body '"ubuntu-latest"'`.
  `gh variable delete FACTORY_LINUX_RUNNER --repo maxharris1/factory`
  returns every Linux job to the pool, since unset means local.

## Before registering (once)

1. **Repo visibility.** Use self-hosted runners only on private repos
   (`maxharris1/factory` is private). On a public repo any user can open a
   pull request and run code on the runner. If a repo ever goes public,
   remove its runner labels from its workflows first. Cleffy is public and
   stays on hosted runners.
2. **Fork pull request workflows stay off.** Repo > Settings > Actions >
   General > "Fork pull request workflows in private repositories": leave
   "Run workflows from fork pull requests" unchecked. This is the real
   control. Check it with
   `gh api repos/maxharris1/factory/actions/permissions/fork-pr-workflows-private-repos`;
   `run_workflows_from_fork_pull_requests` must be `false`. Every factory CI
   job also carries a fork guard `if:`, but a fork could edit that out of
   its own copy of the workflow, so the guard is only a backstop.
3. **No `pull_request_target` or `workflow_run`** in any workflow that
   runs on a self-hosted runner. The test enforces this.

## Per-host rules

- **A dedicated OS user per runner instance.** Never `claude-bot`,
  `codex-bot`, `cursor-bot`, `factory`, `box` or the orch login. The user
  holds no harness login, no `gh` login, no SSH key and no
  `~/.config/factory`.
- **No `docker` and no `sudo` group.** `docker` membership lets a job start a
  root container that mounts `/` and reads every bot's credentials. Factory
  CI needs no Docker.
- **Bot homes stay mode 700**, so a runner user cannot read them.
- **System packages.** The suite and `macos-pickup` call these tools, which
  hosted runners had preinstalled. Install them system-wide on each Linux
  host (Debian or Ubuntu):
  `sudo apt-get install -y git jq rsync openssh-client util-linux procps perl zsh dash curl tar`,
  plus `gh` from GitHub's apt repository
  (https://github.com/cli/cli/blob/trunk/docs/install_linux.md). `gh` must
  not be logged in for the runner user; the workflow passes the job's own
  token. Check: `sudo -u gha-runner-1 gh --version` prints a version.

## PC: pc-wsl-bots, 3 instances (Max)

Run these on the pc-wsl-bots WSL node, not on the Windows host and not on
the box. Do not move WSL or its VHDX.

1. **systemd on WSL.** Check `/etc/wsl.conf` has `[boot]` `systemd=true`
   (it does today).
2. **Create the users** (no password login), one per instance:
   `for n in 1 2 3; do sudo adduser --disabled-password --gecos "" gha-runner-$n; done`
3. **For each `n` in 1, 2, 3:**
   1. Open Repo > Settings > Actions > Runners > New self-hosted runner >
      Linux > x64. It shows the download, the checksum and a `./config.sh`
      command that carries a one-time registration token valid for about an
      hour. Open the page fresh for each instance.
   2. `sudo -iu gha-runner-$n`, then `mkdir actions-runner && cd actions-runner`,
      and run the page's `curl` and `tar` lines. Run its `shasum -a 256 -c`
      line and stop if the checksum does not match.
   3. Paste the page's `./config.sh` line **yourself**, in that terminal
      only, and add
      `--name pc-wsl-bots-$n --labels factory-linux,factory-pc-wsl --work _work --unattended`.
      Do not paste the line into chat, a ticket, a PR, a bot session or a
      file. Accept the default runner group.
   4. `exit` back to your sudo-capable user, then from
      `/home/gha-runner-$n/actions-runner`:
      `sudo ./svc.sh install gha-runner-$n`.
4. **Limits, so factory jobs win.** Find the VHDX's block device with
   `findmnt -no SOURCE /` (for example `/dev/sdc`). For each instance,
   create `/etc/systemd/system/actions.runner.maxharris1-factory.pc-wsl-bots-$n.service.d/limits.conf`:

   ```ini
   [Service]
   Nice=10
   CPUWeight=20
   CPUQuota=400%
   MemoryMax=6G
   IOReadIOPSMax=/dev/sdc 200
   IOWriteIOPSMax=/dev/sdc 200
   ```

   Use the device `findmnt` printed. Three instances then use at most 12 of
   the 24 cores and 18 of the 31 GB. `IOSchedulingClass=` is left out
   because the PC's disks use the `none` scheduler, where it does nothing.
   Then `sudo systemctl daemon-reload` and
   `sudo systemctl start actions.runner.maxharris1-factory.pc-wsl-bots-$n`
   for each `n`.
5. **Keep WSL up.** WSL stops when its last session ends. Keep the node
   running the same way the bot users' sessions are kept up; a stopped node
   leaves jobs queued, not failed.
6. **Optional, after the first week:** a 2 GB `tmpfs` for each instance's
   `_work` (not `_work/_tool`, which holds uv, the JDK and Node) to keep
   CI writes off the spinning disk. tmpfs pages count against `MemoryMax`,
   so raise it to `8G` if you do this.

## Box: box-1, 1 instance, low priority (Max)

The box runs orch, the poller, the waiters and the dashboard push. CI must
never starve them. The box is a Debian 13 x64 VM with 8 cores and 15 GB.
Its PID 1 is `tini`, not systemd, so `svc.sh` and systemd drop-ins do not
work there. The runner starts by hand under `nice`, `ionice` and `taskset`
instead.

1. Install the system packages above.
2. `sudo adduser --disabled-password --gecos "" gha-runner`
3. Open the "New self-hosted runner" page (Linux, x64), then
   `sudo -iu gha-runner`, `mkdir actions-runner && cd actions-runner`, run
   the `curl`, `tar` and `shasum -a 256 -c` lines, and paste the
   `./config.sh` line yourself with
   `--name box-1 --labels factory-linux,factory-box --work _work --unattended`.
   `exit`.
4. Start it detached, pinned to 2 of the 8 cores, at the lowest CPU
   priority and idle I/O class:

   ```sh
   sudo -u gha-runner -H sh -c 'cd ~/actions-runner && nohup taskset -c 6,7 nice -n 19 ionice -c 3 ./run.sh >> ~/runner.log 2>&1 &'
   ```

   One instance means at most one CI job at a time on the box. `taskset`
   caps it at 2 cores whatever the job asks for (the test shards run
   `pytest -n 4`, so they time-share those 2 cores). `ionice -c 3` has
   effect only under the `bfq` scheduler; the box's disks use
   `mq-deadline` today, so the core pin and the one-job limit are the real
   bounds. The box has about 9 GB of memory available today (measured); a job's peak use is not measured yet.
5. It does not restart by itself. On 2026-11-17 Grok Bot added
   `~/.local/bin/box-runner-ensure.sh`, which the box login's `.bashrc`
   runs, so the runner comes back when that login next opens a shell after
   a box restart. Run step 4 by hand if it has not.
   To stop it: `sudo pkill -u gha-runner -f Runner.Listener`.
6. If the poller or waiters slow down (poll cycles stretch, waiter logs
   show gaps), stop `box-1` first and report it on the ticket.

`grokbot-factory-box` gets no runner.

## Mac: maxs-macbook-pro (Max)

Same rules, with these differences:

- The device stays `enabled: false` in `machines.yml`. The runner is
  independent of factory jobs; turning the Mac back on for jobs is a
  separate decision.
- Create a separate **standard (non-admin)** macOS user, `gharunner`.
- **State on 2026-11-17.** `maxs-macbook-pro` runs as `gharunner`. The
  `macos` job on head `25396e56` printed
  `runner maxs-macbook-pro as gharunner` in its "Runner identity" step.
  The Mac's isolation check below still needs Max's `id` output.
- No system Python is needed. CI installs Python with `astral-sh/setup-uv`
  into the runner user's home. `actions/setup-python` fails on self-hosted
  runners other than Ubuntu (on macOS it writes to `/Users/runner`, and it
  has no Debian builds for the box).
- On GitHub's page pick macOS and the Mac's architecture (ARM64 on Apple
  silicon). Logged in as `gharunner`, run the download lines and paste the
  `./config.sh` line yourself with
  `--name maxs-macbook-pro --labels factory-mac --work _work --unattended`.
- `./svc.sh install` and `./svc.sh start` install a launchd agent for the
  logged-in runner user; the Mac must stay awake and that user logged in.
- If the Mac is asleep or offline, the `macos` job waits and `macos-pickup`
  cancels the run red after 10 minutes. A rerun of failed jobs retries it.

## Check isolation (Max, after registering)

Run on each Linux host and paste the output on the ticket. None of it shows
a token.

- `ps -o user= -C Runner.Listener | sort -u` prints only `gha-runner*` users.
- `id gha-runner-1` (`id gha-runner` on the box) lists no `docker` or `sudo` group.
- `sudo -u gha-runner-1 ls /home/claude-bot` fails with "Permission denied".
- The `checks` job's "Runner identity" step prints `runner <name> as gha-runner*`.

On the Mac: `ps -axo user,comm | grep Runner.Listener` shows `gharunner`,
`id -Gn gharunner` lists no `admin` group, and the `macos` job's "Runner
identity" step shows `gharunner`.

Settings > Actions > Runners lists all five runners as Idle with the
labels above.

## Network review on the PC (Max)

- Tailscale Funnel exposes `https://pc-wsl-bots.tail68624b.ts.net`. Check
  which local port it forwards (`tailscale funnel status`) and that a
  `gha-runner-*` user cannot bind it (a port below 1024, or a port a
  running service already holds).
- `sudo ss -ltnp` lists what listens on the PC. A CI job can reach every
  localhost and tailnet service the PC can. Check the tailnet ACL for what
  `pc-wsl-bots` may reach.

## Weekly cleanup (optional)

Runners are persistent, so a job can leave files for the next job on the
same instance. Once a week, per instance: stop the service (on the box,
`pkill` as above), remove `_work` except `_work/_tool`, remove
`~/.cache`, then start it again.

## First week

Report on the ticket, for one week after cutover:

- Median CI queue time (job `created_at` to `started_at`) and wall time per run.
- PC factory job durations, before and after.

If factory job times on the PC get worse by more than 20%, drop to 2 PC
instances. If the median queue time passes 5 minutes, raise it with Max
before adding instances.

## Remove or rotate a runner (Max)

- PC or Mac: `sudo ./svc.sh stop` then `sudo ./svc.sh uninstall`. Box:
  stop it as above.
- Settings > Actions > Runners > the runner > Remove shows a
  `./config.sh remove` command carrying a one-time removal token. Paste it
  yourself in the runner's terminal, as above.
- If you remove every Linux runner, the sync loop sees none online and
  flips `FACTORY_LINUX_RUNNER` to `"ubuntu-latest"` within about a minute,
  so new Linux jobs go to hosted runners. If the loop is held or stopped,
  set the variable by hand first (see "Overflow to hosted runners"), or the
  jobs queue for a day and then fail. `macos` has no overflow: with the Mac
  runner removed, change its `runs-on:` in a PR.

## Acceptance (SPEC F2-03)

- Max can register runners from this checklist without the factory handling
  any token: every token step is his, in GitHub's UI and his own terminal.
  The one exception is the 2026-11-17 registration he directed (see the top).
- Credential-holding deploy workflows stay on hosted runners.
- Labels are read from each repo's `factory.md` (`ci_runner:`).
- The device inventory stays in `machines.yml`.
