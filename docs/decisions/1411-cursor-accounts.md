# 1411: Cursor Ultra account on every device

Issue #1411 asks for Max's Cursor Ultra account signed in on box, mac and pc,
checked by the doctor and shown on Efficiency and Settings. This is the plan
record the ticket asks for. It names the isolation model, the usage decision,
the proposed live config, and the build/review harness decision.

## Isolation model: each device's cursor-bot login

The ticket allows `signed_in_on` only if Cursor has a real per-folder login,
like `CLAUDE_CONFIG_DIR` or `CODEX_HOME`. It does not. The [configuration
reference](https://cursor.com/docs/cli/reference/configuration) documents
`CURSOR_CONFIG_DIR` for `cli-config.json` only. Review round 1 of PR #1556
ran the credential-store code of CLI 2026.11.22-e9042b2 with two account
folders: both resolved to `~/.config/cursor/auth.json` on Linux and to the
same `cursor-user`, `cursor-access-token` and `cursor-refresh-token`
keychain items on macOS.

So the ticket's fallback applies: each device's `cursor-bot` login is the
named Ultra account. `machines.yml` names it inline on each device as
`cursor: {bot: cursor-bot, account: cursor-ultra-1}`. That form is not
deprecated for Cursor (`machines_yml.DEVICE_LOGIN_ONLY`). `signed_in_on` on
a Cursor account stays refused, and `ACCOUNT_ISOLATION` has no Cursor entry.
One device runs one Cursor account.

## Doctor

`orch accounts doctor` runs `cursor-agent status` as `cursor-bot` with
`CURSOR_API_KEY`, `CURSOR_AUTH_TOKEN` and `CURSOR_CONFIG_DIR` unset, and
reads the text reply of that CLI version's status renderer:

| Reply | Status | Fingerprint |
| --- | --- | --- |
| `✓ Logged in as <email>` | `ok` | hash of the email |
| `✓ Logged in as user id <id>`, then `Logged in (user details not available)` | `ok` | hash of the user id |
| `✓ Login successful!`, then `Logged in (user details not available)` or `Logged in (unable to fetch user details)` | `ok` | none |
| `Not logged in`, `Partially authenticated (missing refresh token)` | `signed-out` | none |
| `Status check error: ...` or anything else | `signed-out`, not understood | none |

It never opens a credential file and never stores the email or user id.

## Usage: not tracked

The CLI shows its included-usage meters only in the interactive `/usage`
view. No documented non-interactive command or API reports used and reset
without reading the login's credentials, so Cursor stays in
`usage_readings.NOT_TRACKED`, and Efficiency and Settings show the reason
from `usage_readings.NOT_TRACKED_REASONS`. The Ultra plan's included usage is
one weekly pool shared with Grok. The `external` account `cursor-grok-ultra`
keeps recording that pool from `orch usage record`; `cursor-ultra-1` is the
CLI login that draws on it.

## Proposed machines.yml (not applied)

The ticket's config rule: `machines.yml` and `orch.yml` change only on
Max's own words. This PR leaves both unchanged. Once every device runs code
that reads `accounts:` (the shipped file's header) and Max has run
`cursor-agent login` as `cursor-bot` on each device, this diff declares the
account on box, mac and pc. Mac stays disabled until Max provisions its bot
users. `tests/test_cursor_driver.py` applies this exact diff to the shipped
file and loads it.

```diff
--- a/machines.yml
+++ b/machines.yml
@@ -20,6 +20,8 @@
 # accounts: section and {bot, account} values only after every device runs
 # code that reads them (README "Usage accounts"); older code refuses the key.
 orch_host: pc
+accounts:
+  - {id: cursor-ultra-1, harness: cursor, plan: ultra, label: "Cursor Ultra"}
 devices:
   # The WSL Ubuntu node on the PC, not the Windows host.
   - name: pc
@@ -28,7 +30,7 @@
     harnesses:
       claude: claude-bot
       codex: codex-bot
-      cursor: cursor-bot
+      cursor: {bot: cursor-bot, account: cursor-ultra-1}
     # Each bot's own home, e.g. /home/codex-bot/factory-work. Jobs stay off
     # the Windows C: drive; /mnt/ is refused.
     workdir_root: /home/{bot}/factory-work
@@ -42,7 +44,7 @@
     harnesses:
       claude: claude-bot
       codex: codex-bot
-      cursor: cursor-bot
+      cursor: {bot: cursor-bot, account: cursor-ultra-1}
     workdir_root: /home/box/factory-work
     ssh_alias: box-{bot}
     factory_root: /home/box/factory
@@ -52,7 +54,8 @@
     enabled: false
     tailscale_name: maxs-macbook-pro.tail68624b.ts.net
     slot_max: 2
-    harnesses: {}
+    harnesses:
+      cursor: {bot: cursor-bot, account: cursor-ultra-1}
     workdir_root: REPLACE_WITH_MAC_WORKDIR_ROOT
     ssh_alias: REPLACE_WITH_MAC_SSH_ALIAS
     factory_root: /Users/{bot}/factory
```

`config/machines.example.yml` carries the same shape on its pc and box.

## Build/review harness decision

Max has not yet decided whether Cursor becomes a build or review harness.
Until his own words say otherwise, Cursor stays in `orch.yml`
`refused_harnesses`, and no Cursor build or review job runs. The account,
doctor, usage and UI work above does not depend on that decision.
