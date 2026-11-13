# #122: a separate approval credential (GitHub App for the bots)

## Decision

Every bot posts to GitHub as `maxharris1`, Max's own login, and the factory tells comments apart only by the `from=` label in each header. Any bot could therefore post the two comments only Max should give: a `go` that grants an extra round on a capped ticket, and a `kind=ok` that clears a ship hold.

Max, Nov 13, 2026, 2:26 to 2:50 PM ET: on how to split approvals from bot posts, he asked what to recommend, then accepted a GitHub App for the bots. Their comments show as the app's bot login, so anything under `maxharris1` is really Max. The factory trusts a capped-ticket `go` and a hold `ok` only from `maxharris1`. At 2:52 PM ET: "Go: use a GitHub App for the bots and start building wave 1 of the prompt refactor".

This change adds the config and the checks. It does not create or install the app, and it does not switch traffic onto it.

## Config (`orch.yml`)

| Field | Meaning |
| --- | --- |
| `approver_logins` | GitHub user logins whose capped-ticket `go` and hold `ok` count once the switch is on. `[bot]` logins are refused: an app never approves. Shipped as `[maxharris1]`. |
| `approver_gh_config_dir` | Absolute path to Max's approver `gh` profile, the only one Grok may use to post those two kinds. A path only; orch never opens it. No `~`, which would expand to whichever user reads it. |
| `github_app.app_id` | The app's numeric id. |
| `github_app.installations` | Account login to installation id (`maxharris1`, `aut0-code`). |
| `github_app.private_key_file` | Absolute path to the app key. orch never opens, reads, stats or prints it; `check-config` is tested not to open it. |
| `github_app.bot_login` | The app's login, `<name>[bot]`. |
| `github_app.enabled` | The switch. Default `false`. |

With `enabled: true`, `check-config` also requires `bot_login`, requires `approver_logins`, and requires `bot_login` to be a `trusted_logins` key, so the flip cannot lock the bots out.

## Trust map

| Check | Switch off (default, today) | Switch on |
| --- | --- | --- |
| `go` on a capped ticket | Header `from=grok-bot role=orchestrator`, author trusted for `grok-bot` | The same header, and the author is in `approver_logins`, whether or not it is trusted for `grok-bot`. Otherwise nothing changes and Grok is woken once with `go-not-from-approver` |
| `go` on any other ticket | Author trusted for `grok-bot` | Unchanged: an approver not trusted for `grok-bot` gets `rejected-go-author` |
| Hold `ok`, and the approver removing a label-only hold | Author is `hold.approver`, and `hold.approver` is a `trusted_logins` key | Author is `hold.approver`, and `hold.approver` is in `approver_logins` |
| Harness runs (`orch wait --autostart`) | Environment unchanged | `GH_CONFIG_DIR` at or inside the approver profile, or an `XDG_CONFIG_HOME` whose `gh` is there, is dropped. A profile inside the run's `HOME`, or a `$HOME/.config/gh` that is the profile, makes the waiter refuse to start. Paths are resolved, so a symlink or bind mount to the profile counts as the profile, and so does a `gh` directory holding a symlink or hard link to one of the profile's files (`hosts.yml`, `config.yml`); only metadata is read, never the files. When `orch.yml` has an error elsewhere, the waiter still reads these settings alone; when they do not load either, it refuses to start |
| Grok posting a capped `go` or an `ok` | `gh` with token variables dropped, as today | `lib.orchestrate.approval_post_env` points `gh` at the approver profile only; with no profile configured it refuses and Max posts by hand |
| `check-config` | `hold.approver` must be a `trusted_logins` key, and in `approver_logins` when that is set | `hold.approver` must be in `approver_logins` |

Approvers live in `orch.yml`, which lives on the box, and not in `factory.md`, which sits in a repo the bots push to.

## Rollout (Factory, after Max installs the app)

1. Fill in the app's real `app_id`, `installations`, `private_key_file` and `bot_login` in `orch.yml`.
2. Add `trusted_logins[<bot_login>]: [grok-bot, claude-bot, codex-bot, cursor-bot]` and trim `maxharris1` to `[grok-bot]`.
3. Set `approver_gh_config_dir` to Max's approver profile, outside every harness user's `HOME`.
4. Move the bots' own `gh` onto the app.
5. Set `github_app.enabled: true` and restart the poller and the waiters.

## What this change cannot check

- The factory cannot tell which account a `gh` profile holds without reading its token, so it never looks. Moving each harness user's own `gh` login off `maxharris1` is Max's setup step. The code compares paths only.
- An `orch job run` started by hand, outside a waiter, inherits that shell's environment. The guard covers every run the factory starts.
- Remote runs pass only named variables (`lib/remote.py`), never the box's `GH_CONFIG_DIR`, so they need no change.
- Linear authors (`linear:<id>`) are not GitHub logins. A capped `go` on a Linear ticket keeps today's rule in both switch states, so the gap stays open on Linear until the bots get a Linear identity of their own.
- `factory.md`'s `hold.approver` defaults to `max`, which is not a login in `trusted_logins`, so a hold that keeps the default can never clear. `check-config` now reports it. Changing the default to `maxharris1` is a one-line follow-up in `lib/factory_md.py`.
