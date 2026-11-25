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
`cursor: {bot: cursor-bot, account: cursor-ultra}`. That form is not
deprecated for Cursor (`machines_yml.DEVICE_LOGIN_ONLY`). `signed_in_on` on
a Cursor account stays refused, and `ACCOUNT_ISOLATION` has no Cursor entry.
One device runs one Cursor account.

## Doctor

`orch accounts doctor` runs `cursor-agent status --format json` as
`cursor-bot` with `CURSOR_API_KEY`, `CURSOR_AUTH_TOKEN` and
`CURSOR_CONFIG_DIR` unset. CLI 2026.11.22-e9042b2 renders the same status
object as text or, with `--format json`, as JSON, so the doctor reads the
JSON and never the text renderer's wording:

| `status` | Status | Fingerprint |
| --- | --- | --- |
| `authenticated`, with or without `userInfo` | `ok` | hash of `userInfo.userId`, else none |
| `unauthenticated` | `signed-out`, not logged in | none |
| `partially-authenticated` | `signed-out`, partly signed in | none |
| `error` (exits 1) or anything else | `signed-out`, inconclusive | none |

Two different user ids are two sign-ins. It never opens a credential file
and never stores the email or user id.

## Usage: not tracked

The CLI shows its included-usage meters only in the interactive `/usage`
view. No documented non-interactive command or API reports used and reset
without reading the login's credentials, so Cursor stays in
`usage_readings.NOT_TRACKED`, and Efficiency and Settings show the reason
it maps Cursor to. The Ultra plan's included usage is
one weekly pool shared with Grok. The `external` account `cursor-grok-ultra`
keeps recording that pool from `orch usage record`; `cursor-ultra` is the
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
+  - {id: cursor-ultra, harness: cursor, plan: ultra, label: "Cursor Ultra"}
 devices:
   # The WSL Ubuntu node on the PC, not the Windows host.
   - name: pc
@@ -28,7 +30,7 @@
     harnesses:
       claude: claude-bot
       codex: codex-bot
-      cursor: cursor-bot
+      cursor: {bot: cursor-bot, account: cursor-ultra}
     # Each bot's own home, e.g. /home/codex-bot/factory-work. Jobs stay off
     # the Windows C: drive; /mnt/ is refused.
     workdir_root: /home/{bot}/factory-work
@@ -42,7 +44,7 @@
     harnesses:
       claude: claude-bot
       codex: codex-bot
-      cursor: cursor-bot
+      cursor: {bot: cursor-bot, account: cursor-ultra}
     workdir_root: /home/box/factory-work
     ssh_alias: box-{bot}
     factory_root: /home/box/factory
@@ -52,7 +54,8 @@
     enabled: false
     tailscale_name: maxs-macbook-pro.tail68624b.ts.net
     slot_max: 2
-    harnesses: {}
+    harnesses:
+      cursor: {bot: cursor-bot, account: cursor-ultra}
     workdir_root: REPLACE_WITH_MAC_WORKDIR_ROOT
     ssh_alias: REPLACE_WITH_MAC_SSH_ALIAS
     factory_root: /Users/{bot}/factory
```

`config/machines.example.yml` carries the same shape on its pc and box.

## Build/review harness decision

Max decided yes on 2026-11-25: "Yes: allow Cursor to build and review, and
drop it from refused_harnesses" (his words via the Factory Internal card,
relayed in
https://github.com/maxharris1/factory/pull/1556#issuecomment-7165146292).
This PR removes `cursor` from `refused_harnesses` in the tracked `orch.yml`;
the live host `orch.yml` changes only when this PR ships. A Cursor job runs
on the device's `cursor-bot` login, so it draws on the Cursor account above.
The cross-family rule is unchanged: a Cursor harness takes its family only
from its model hint (`lib/factory_md.py`), and a route whose builder and
reviewer share a family, or whose Cursor family is not established, is
refused (`orchestrate.assert_cross_family`).
