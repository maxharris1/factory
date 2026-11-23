# #1066: accounts on the Settings page

Decisions from issue #1066, built in tickets #1356 to #1364 and documented in #1365. The Accounts panel on the Settings page sends `target: "account"` requests to `/api/settings`. `orch settings apply` applies them to `machines.yml` (`lib/account_settings.py`). docs/ACCOUNTS.md ("Manage accounts from Settings") describes the panel, the requests and every refusal.

- **Decision:** Remove needs the account `paused` and no running job on any device. The applier refuses `not-paused` first, then `<account> holds <n> running job(s)`. A slot that records no account counts as a running job of its device's own login account. Pausing first stops new work at once, and waiting for the running jobs means a remove never strands a job on an account that no longer exists. The same running-job check refuses a `signed_in_on` change that drops a device where the account runs a job.
- **When:** 2026-11-22, by Factory decided (technical), for issue #1066 (ticket #1365)
- **Source:** https://github.com/maxharris1/factory/pull/1355#issuecomment-7087395800

- **Decision:** Remove keeps the account's folder. It edits `machines.yml` only and never deletes `~/.factory-accounts/<id>` on any device. A mistaken remove loses no sign-in, and a later add of the same id finds the folder and its login. Deleting the folder stays a manual step on each device.
- **When:** 2026-11-22, by Factory decided (technical), for issue #1066 (ticket #1365)
- **Source:** https://github.com/maxharris1/factory/pull/1355#issuecomment-7087395800

- **Decision:** Default accounts (`<harness>-default`) and `external` accounts are read-only. The page shows no controls on an external account's card, or on a default account's card while `accounts:` does not declare it. The server refuses both with 403 `read-only`, declared or not, and the applier refuses an edit to them. A default account is a device's own CLI login. It exists wherever a device lists the harness, so the panel has nothing to add, remove or sign in. An `external` account has no device and no folder, and its readings come from `orch usage record`.
- **When:** 2026-11-22, by Factory decided (technical), for issue #1066 (ticket #1365)
- **Source:** https://github.com/maxharris1/factory/pull/1355#issuecomment-7087395800
