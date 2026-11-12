# F2-03 self-hosted runner checklist (for Max)

This is the registration checklist from docs/SPEC.md F2-03 and docs/PLAN.md
Phase 2 item 17. **Max does every step that touches a token.** The factory
never creates, requests, reads, stores or passes a runner registration or
removal token, never reads a runner's `.runner`, `.credentials*` or `.env`
files, and never registers or removes a runner through the API (SPEC section 10
"Security": Max registers self-hosted runners himself; never read runner
tokens).
This document contains no token and no placeholder for one: the token only
ever appears on GitHub's "New self-hosted runner" page and in the terminal
where Max pastes the command from that page.

## What goes where

| Item | Where | Who |
|---|---|---|
| Runner registration and its token | GitHub UI and the runner host's terminal | Max only |
| Runner label for a repo | that repo's `factory.md` front matter as `ci_runner:` | a normal repo PR |
| Which jobs use it | that repo's workflow `runs-on:` | a normal repo PR |
| Device inventory (hosts, Tailscale names) | `machines.yml` (F2-01) | already there |
| Credential-holding deploy workflows | GitHub-hosted runners, unchanged (D21) | nobody moves them |

Self-hosted runners are for non-credential checks only: tests, lint,
`check-config`, model checks. A workflow that holds a deploy credential,
cloud key or publish token stays on `ubuntu-latest` (or whatever hosted
runner it uses today).

## Labels

Every runner gets GitHub's automatic labels (`self-hosted`, the OS, the
architecture) plus one factory label naming the host:

| Host | `machines.yml` device | Factory label | Automatic labels |
|---|---|---|---|
| pc-wsl-bots (the PC's WSL Ubuntu node) | `pc` | `factory-pc-wsl` | `self-hosted`, `Linux`, `X64` |
| Max's Mac (later) | `mac` | `factory-mac` | `self-hosted`, `macOS`, `ARM64` (or `X64` on an Intel Mac) |

A repo names exactly one factory label in `factory.md`, for example:

```yaml
ci_runner: factory-pc-wsl
```

`ci_runner` must be one token (letters, digits, `.`, `_`, `:`, `-`);
`orch check-config` validates it. The workflow then selects that runner with
both labels, so a job can never land on some other self-hosted runner:

```yaml
jobs:
  test:
    runs-on: [self-hosted, factory-pc-wsl]
```

Today `ci_runner` is repo policy that `check-config` validates and humans and
bots read; the poller does not rewrite workflows. Changing `runs-on:` is an
ordinary PR in that repo, reviewed like any other.

## Before registering (once per host)

1. **Repo visibility.** Use self-hosted runners only on private repos
   (`maxharris1/factory` is private). On a public repo a fork PR can run
   arbitrary code on the runner. If a repo ever goes public, remove its
   runner first.
2. **Fork PR approval.** In the repo's Settings > Actions > General, keep
   "Fork pull request workflows" requiring approval, and do not use
   `pull_request_target` in any workflow that runs on a self-hosted runner.
3. **A dedicated OS user.** Run the runner as its own user (suggested:
   `gha-runner`), not as `claude-bot`, `codex-bot`, `cursor-bot` or `box`.
   That user holds no harness login, no `gh` login, no SSH key to other
   hosts and no `~/.config/factory`. A CI job then cannot read a bot's
   credentials, and the bots cannot read the runner's.
4. **Capacity.** CI jobs on pc-wsl-bots share CPU and memory with the `pc`
   device's harness slots (`slot_max: 3` in `machines.yml`). Start with one
   runner per host.

## Register on pc-wsl-bots (Max)

Run these on the pc-wsl-bots WSL node, not on the Windows host and not on
the box.

1. **systemd on WSL.** The runner service needs systemd. Check
   `/etc/wsl.conf` has `[boot]` `systemd=true` (the dashboard server unit
   needs it too), then `wsl --shutdown` from Windows if you changed it.
2. **Create the user** (no password login):
   `sudo adduser --disabled-password --gecos "" gha-runner`
3. **Open GitHub's page.** Repo > Settings > Actions > Runners > New
   self-hosted runner > Linux > x64. Keep that page open; it shows the
   download, the checksum and a `./config.sh` command that carries a
   one-time registration token valid for about an hour.
4. **Download as `gha-runner`:** `sudo -iu gha-runner`, then
   `mkdir actions-runner && cd actions-runner`, and run the page's
   `curl` and `tar` lines. Run its `shasum -a 256 -c` line and stop if the
   checksum does not match.
5. **Configure.** Paste the page's `./config.sh` line **yourself**, in that
   terminal only, and add these options to it:
   `--name pc-wsl-bots --labels factory-pc-wsl --work _work --unattended`.
   Do not paste the line into chat, a ticket, a PR, a bot session or a file.
   Accept the default runner group.
6. **Install as a service** (as your sudo-capable user, from
   `/home/gha-runner/actions-runner`):
   `sudo ./svc.sh install gha-runner`, then `sudo ./svc.sh start`, then
   `sudo ./svc.sh status`.
7. **Check in GitHub.** Settings > Actions > Runners lists `pc-wsl-bots` as
   Idle with labels `self-hosted`, `Linux`, `X64`, `factory-pc-wsl`.
8. **Keep WSL up.** WSL stops when its last session ends. Keep the node
   running the same way the bot users' sessions are kept up; a stopped node
   leaves jobs queued, not failed.

## Register on the Mac (later)

Same rules, same order, with these differences:

- The device stays `enabled: false` in `machines.yml` until F2-02; a runner
  can be registered independently, but only when Max wants Mac CI.
- Create a separate standard (non-admin) macOS user for the runner.
- On GitHub's page pick macOS and the Mac's architecture (ARM64 on Apple silicon). Add
  `--name maxs-macbook-pro --labels factory-mac --work _work --unattended`.
- `./svc.sh install` and `./svc.sh start` install a launchd agent for the
  logged-in runner user; the Mac must stay awake and that user logged in.

## Wire a repo to it

1. Repo PR: add `ci_runner: factory-pc-wsl` to `factory.md`; run
   `bin/orch check-config` on it (or let the repo's CI do so).
2. Same or follow-up PR: change only the non-credential jobs to
   `runs-on: [self-hosted, factory-pc-wsl]`. Leave every job that reads a
   deploy secret on its hosted runner.
3. Confirm a run on that PR shows "Runner name: pc-wsl-bots" in the job log.
4. Keep `permissions:` minimal (`contents: read`) on self-hosted jobs.

## Remove or rotate a runner (Max)

- Stop and uninstall the service: `sudo ./svc.sh stop` then
  `sudo ./svc.sh uninstall`.
- Settings > Actions > Runners > the runner > Remove shows a `./config.sh
  remove` command carrying a one-time removal token. Paste it yourself in
  the runner's terminal, as above.
- Remove the repo's `ci_runner:` and `runs-on:` lines in a PR before (or
  right after) removing the runner, or its jobs queue forever.

## Acceptance (SPEC F2-03)

- Max can register runners from this checklist without the factory handling
  any token: every token step is his, in GitHub's UI and his own terminal.
- Credential-holding deploy workflows stay on hosted runners.
- Labels are read from each repo's `factory.md` (`ci_runner:`).
- The device inventory stays in `machines.yml`.
