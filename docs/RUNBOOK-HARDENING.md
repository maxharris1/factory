# Factory Operator Hardening & Cutover Runbook

This runbook covers the host, OS-user, credential, and service cutover steps that Max executes by hand after Phase 0 stabilization lands. In keeping with factory trust boundaries, the codebase never creates logins, OS accounts, GitHub Apps, or credentials automatically.

---

## 0. Before You Start (Safety Rules)

- **Remember auto-deploy:** `main` auto-deploys to `pc` and `box` every 2 minutes (`deploy/self-heal.sh`). Keep all edits on feature branches until reviewed and CI-green.
- **Keep host overrides local:** Never commit real IDs, hostnames, or app settings to tracked `orch.yml` or `machines.yml`. Put host-specific overlays in the gitignored `orch.local.yml` and `machines.local.yml` beside them.
- **Never store secrets in tracked files or bot home directories:** GitHub App `.pem` keys belong in a root/poller-only directory (`0600`) outside `/home/claude-bot`, `/home/codex-bot`, `/home/cursor-bot`, and `/home/gemini-bot`.

---

## 1. Reviewing & Merging the Stacked Phase 0 Branches

Phase 0 is organized as six stacked, independently tested branches atop `main` (`92293403`):

1. `p0/correctness-fixes` — QA-park resume crash, SQLite lock retries, `factory_md` `strict=False`, `rule_engine` cap bugs, `_redrive_handoffs` QA gate, `refused_harnesses`, feeder fixes, Linear pagination, brief/pstack wiring.
2. `p0/runtime-ssh-sqlite` — SSH mux lock-free fast path + tight keepalives, sign-in broker idle backoff, deterministic SQLite connection closing, `AdoptedRun` `0600` proof persistence, `job-detail/` backup sync, disk retention pruning, systemd unit hygiene.
3. `p0/security-and-wakes` — Codex `--add-dir` removal (settling via `ORCH-HANDOFF:` outside sandbox), `st_uid` relay owner verification, `webhook.env` subshell scoping, `secret_scan` widening, Linear webhook replay fix, genuine `from=approver role=approver` dashboard actions, `Secure` cookies + `tailscale serve` HTTPS support, HTTP 429 wake backoff & critical wake backstop.
4. `p0/unified-evals-and-cost` — Standalone `eval-report --db [--json]`, `model/chart.mmd` transition parsing, windowed operational & spend metrics, pure `lib/pricing.py` Codex/Gemini cost estimation, un-truncated 7-day dashboard spend rollups, and Monday weekly eval systemd timer.
5. `p0/subtract-dead-code` — Deletes dead `lib/usage.py` cross-harness reserve, `TelemetryUsage`, and `review_actions_for`; aligns `model/check.sh` with the vendored pstack pin; purges ~24.8 MiB of committed screenshots/mockups/trail TSVs.
6. `p0/runbook-and-roadmap` — This runbook (`docs/RUNBOOK-HARDENING.md`) and sequenced Phase 1–2 self-build tickets (`docs/ROADMAP-TICKETS.md`).

### Recommended Merge Sequence

Push the branches and merge in order (or fast-forward `main` one branch at a time after reviewing each `git diff`):

```bash
# Inspect the stack
git log --oneline main..p0/runbook-and-roadmap

# Push each branch to open PRs (or merge sequentially into main)
git push -u origin \
  p0/correctness-fixes \
  p0/runtime-ssh-sqlite \
  p0/security-and-wakes \
  p0/unified-evals-and-cost \
  p0/subtract-dead-code \
  p0/runbook-and-roadmap
```

After `main` advances and self-deploy runs (~2 minutes), verify on `pc`:

```bash
bin/orch check-config --orch orch.yml --machines machines.yml --factory factory.md
bin/orch doctor-runtime
bin/orch status
```

---

## 2. Per-Bot GitHub Apps (Eliminate Approver/Worker Login Overlap)

Today `maxharris1` is listed as both `approver_logins` and the login backing `claude-bot` / `codex-bot` / `cursor-bot`. Once `github_app.enabled: true` is active, PR 0.3's `orch check-config` enforces separation so no harness bot can lift its own cap or clear its own hold.

### 2.1 Create Dedicated Worker GitHub Apps

Create two GitHub Apps now (and repeat for `factory-cursor` / `factory-gemini` when onboarding those harnesses in Epic 2):

- **`factory-claude`** (bot login: `factory-claude[bot]`)
- **`factory-codex`** (bot login: `factory-codex[bot]`)

Minimum repository permissions for each App:
- **Contents:** Read & write (branch push/checkout for builder; Read-only is sufficient for reviewer-only apps, R/W if both roles)
- **Pull requests:** Read & write (comments, draft/ready updates)
- **Issues:** Read & write (PR issue comments, labels)
- **Checks / Commit statuses:** Read-only
- **Metadata:** Read-only

Install each App on both organizations/owners (`maxharris1` and `aut0-code`), note each `app_id` and per-owner `installation_id`, and download each `.pem` private key.

### 2.2 Install `.pem` Keys Outside Harness Home Directories

On the orch host (`pc` WSL Ubuntu):

```bash
sudo install -d -m 0750 -o root -g factory-poller /etc/factory/apps
sudo install -m 0640 -o root -g factory-poller ~/Downloads/factory-claude.pem /etc/factory/apps/claude.pem
sudo install -m 0640 -o root -g factory-poller ~/Downloads/factory-codex.pem /etc/factory/apps/codex.pem
rm -f ~/Downloads/factory-claude.pem ~/Downloads/factory-codex.pem
```

*(If your poller currently runs under your operator user rather than `factory-poller`, set `-o <poller-user> -g <poller-user> -m 0600` until you cut the poller over.)*

### 2.3 Enable in `orch.local.yml`

Update `orch.local.yml` beside `orch.yml`:

```yaml
trusted_logins:
  maxharris1:
    - grok-bot
  factory-claude[bot]:
    - claude-bot
  factory-codex[bot]:
    - codex-bot

approver_logins:
  - maxharris1

poller_reads_app: claude-bot

github_app:
  enabled: true
  apps:
    claude-bot:
      bot_login: factory-claude[bot]
      app_id: REPLACE_WITH_CLAUDE_APP_ID
      installations:
        maxharris1: REPLACE_WITH_MAXHARRIS1_INSTALL_ID
        aut0-code: REPLACE_WITH_AUT0_INSTALL_ID
      private_key_file: /etc/factory/apps/claude.pem
    codex-bot:
      bot_login: factory-codex[bot]
      app_id: REPLACE_WITH_CODEX_APP_ID
      installations:
        maxharris1: REPLACE_WITH_MAXHARRIS1_INSTALL_ID
        aut0-code: REPLACE_WITH_AUT0_INSTALL_ID
      private_key_file: /etc/factory/apps/codex.pem
```

Verify zero overlap errors or warnings:

```bash
bin/orch check-config --orch orch.yml --machines machines.yml --factory factory.md
```

---

## 3. Separate OS Users on `box` (Drop Shared `ssh_user: box`)

Currently `machines.yml` runs all harnesses on `box` through a single `ssh_user: box`, which lets any harness process read peer credentials and tokens on `box`.

### 3.1 Provision Dedicated Bot Users on `box`

SSH into `box` and create isolated accounts:

```bash
for bot in claude-bot codex-bot cursor-bot; do
  sudo adduser --disabled-password --gecos "Factory ${bot}" "${bot}"
  sudo install -d -m 0700 -o "${bot}" -g "${bot}" "/home/${bot}/.ssh"
  sudo install -d -m 0750 -o "${bot}" -g "${bot}" "/home/${bot}/factory-work"
done
```

Authorize the orch host's SSH public key in each `/home/<bot>/.ssh/authorized_keys` (`0600`, owned by `<bot>`), clone `maxharris1/factory` into `/home/<bot>/factory`, and run `deploy/install.sh` + `bin/orch install-skills` as each bot user.

### 3.2 Add Per-Bot SSH Aliases on `pc`

In `~/.ssh/config` on `pc`:

```sshconfig
Host box-claude-bot
  HostName grokbot-factory-box.tail68624b.ts.net
  User claude-bot
  IdentitiesOnly yes

Host box-codex-bot
  HostName grokbot-factory-box.tail68624b.ts.net
  User codex-bot
  IdentitiesOnly yes

Host box-cursor-bot
  HostName grokbot-factory-box.tail68624b.ts.net
  User cursor-bot
  IdentitiesOnly yes
```

### 3.3 Update `machines.local.yml`

Override the `box` entry in `machines.local.yml` to remove `ssh_user: box` and use per-bot paths:

```yaml
devices:
  - name: box
    tailscale_name: grokbot-factory-box.tail68624b.ts.net
    slot_max: 6
    harnesses:
      claude: claude-bot
      codex: codex-bot
      cursor: cursor-bot
    workdir_root: /home/{bot}/factory-work
    ssh_alias: box-{bot}
    factory_root: /home/{bot}/factory
```

Test SSH multiplexing and remote health:

```bash
for bot in claude-bot codex-bot; do
  ssh "box-${bot}" 'id && test -x ~/factory/bin/orch && echo OK'
done
bin/orch doctor-runtime
```

---

## 4. Harden Mission Control (Dedicated Sync User + `tailscale serve` HTTPS)

### 4.1 Move Sync & Relay Off `cursor-bot`

Never share Mission Control's push directory, settings spool, or sign-in relay with a harness bot user (`cursor-bot`). On `pc-wsl-bots`:

```bash
sudo adduser --system --group --home /var/lib/factory-dash-sync --shell /bin/bash factory-dash-sync
sudo install -d -m 0750 -o factory-dash-sync -g factory-dash /srv/factory-dashboard
sudo install -d -m 0770 -o factory-dash -g factory-dash-sync /srv/factory-dashboard-spool
sudo install -d -m 0770 -o factory-dash -g factory-dash-sync /srv/factory-signin-relay
sudo install -d -m 0750 -o factory-dash -g factory-dash /var/log/factory-dashboard
sudo touch /var/log/factory-dashboard/actions-audit.jsonl
sudo chown factory-dash:factory-dash /var/log/factory-dashboard/actions-audit.jsonl
sudo chmod 0640 /var/log/factory-dashboard/actions-audit.jsonl
```

Update `deploy/factory-dashboard-http.service` drop-in (`/etc/systemd/system/factory-dashboard-http.service.d/override.conf`) to replace `SupplementaryGroups=cursor-bot` with `SupplementaryGroups=factory-dash-sync` and bind `/srv/factory-dashboard`, `/srv/factory-dashboard-spool`, `/srv/factory-signin-relay`, and `/var/log/factory-dashboard`.

Update `orch.local.yml`:

```yaml
dashboard:
  refresh_seconds: 20
  push:
    transport: ssh
    host: pc-wsl-factory-dash-sync
    ssh_config: /home/box/.ssh/config
    remote_dir: /srv/factory-dashboard
    method: scp
    live: true
  settings:
    spool_owner: factory-dash-sync
```

*(With `spool_owner: factory-dash-sync`, PR 0.3's `lib/signin_relay.py` verifies `st_uid` / `stat -c %U` on every relay file before consuming it.)*

### 4.2 Enable HTTPS via `tailscale serve`, `Secure` Cookies & Optional Read Auth

Update `/etc/factory-dashboard/http.env` (owned `root:factory-dash`, mode `0640`):

```env
TAILNET_IP=<pc-wsl-bots tailscale ip -4>
DASHBOARD_DIR=/srv/factory-dashboard
DASHBOARD_PORT=8090
DASHBOARD_HTTPS=1
DASHBOARD_GITHUB_CLIENT_ID=<OAuth App client ID>
DASHBOARD_APPROVER=maxharris1
DASHBOARD_ACTION_CLIENTS=<Max's laptop and phone tailnet IPv4s, comma-separated>
DASHBOARD_OAUTH_SCOPE=public_repo
# Use "repo" only if private repos (e.g. aut0-code/aut0) need PR comments from Mission Control:
# DASHBOARD_OAUTH_SCOPE=repo
DASHBOARD_REQUIRE_AUTH_READS=1
DASHBOARD_SETTINGS_SPOOL=/srv/factory-dashboard-spool
DASHBOARD_SIGNIN_RELAY=/srv/factory-signin-relay
DASHBOARD_ACTIONS_AUDIT=/var/log/factory-dashboard/actions-audit.jsonl
```

Validate and restart the server, then place `tailscale serve` in front of it:

```bash
sudo -u factory-dash /usr/bin/python3 -I /usr/local/lib/factory-dashboard/factory-dashboard-http.py --check
sudo systemctl daemon-reload
sudo systemctl restart factory-dashboard-http.service

# Publish over Tailscale HTTPS (auto-provisioned TLS cert on *.ts.net)
sudo tailscale serve --bg --https=443 "http://${TAILNET_IP}:8090"
tailscale serve status
```

Verify `Set-Cookie` carries `; Secure; HttpOnly; SameSite=Strict` over `https://pc-wsl-bots.tail68624b.ts.net/`.

---

## 5. Enable Automated Monday Weekly Evals

PR 0.4 adds `deploy/weekly-eval.sh`, `deploy/factory-weekly-eval.service`, and `deploy/factory-weekly-eval.timer` (firing every Monday at `08:00 America/New_York`).

On the orch host (`pc`):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/factory-weekly-eval.service deploy/factory-weekly-eval.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now factory-weekly-eval.timer
systemctl --user list-timers factory-weekly-eval.timer

# Run one immediate smoke test
systemctl --user start factory-weekly-eval.service
journalctl --user -u factory-weekly-eval.service -n 40 --no-pager
```

Confirm reports are written under `~/.local/state/factory/evals/weekly-YYYY-MM-DD.{txt,json}`.

---

## 6. Authenticate Cursor & Gemini CLIs (Preparation for Epic 2)

Once **E2-2** (Cursor CLI driver) and **E2-3** (Gemini CLI driver) are merged:

1. **Cursor (`cursor-agent`):**
   ```bash
   sudo -u cursor-bot -H bash -lc 'cursor-agent login && cursor-agent --version'
   ```
   Remove `cursor` from `refused_harnesses` in `orch.local.yml`, enable `cursor-bot` waiter, and verify `bin/orch doctor`.

2. **Gemini (`gemini`):**
   Create `gemini-bot` user (same pattern as Section 3.1). Its sign-in waits on Max's choice of CLI (#1412). See `docs/ACCOUNTS.md`.
   Add `gemini: gemini-bot` under `devices[].harnesses` in `machines.local.yml`, run `bin/orch install-skills`, and verify `bin/orch doctor`.

3. **Additional Same-Harness Accounts (Even Usage Balancing):**
   For each secondary account (e.g., `claude-b`, `codex-b`), create its isolated directory under `/home/<bot>/accounts/<account-name>` (`0700`), sign in using `CLAUDE_CONFIG_DIR` / `CODEX_HOME`, and register it in `machines.local.yml` `accounts:`. Epic 2 ticket **E2-5** will automatically balance usage evenly across accounts of the same harness.

---

## 7. Post-Hardening Verification Checklist

Run every check below and confirm zero errors:

```bash
# 1. Strict config validation (no overlap errors, no placeholder warnings on local overlay)
bin/orch check-config --orch orch.yml --machines machines.yml --factory factory.md

# 2. Pinned pstack skills verified across installed harnesses
bin/orch doctor --home "$HOME"

# 3. Fleet SSH mux, remote checkout parity, and waiter health
bin/orch doctor-runtime

# 4. Standalone weekly eval against live backup snapshot (read-only)
bin/orch eval-report --db ~/.local/share/factory/backup/orch.db --json | head -n 40

# 5. Mission Control HTTPS + systemd services active
tailscale serve status
systemctl is-active factory-dashboard-http.service
systemctl --user is-active factory-weekly-eval.timer
```
