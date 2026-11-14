# #170: four GitHub Apps, one login each, trust scoped by login

- **Decision:** each bot posts as its own GitHub App (Factory, Claude Bot, Codex Bot, Cursor Bot). With the switch on, the Factory login alone speaks for `grok-bot`, and each worker login speaks only its own bot's `plan`, `questions`, `ready`, `verdict` and `stuck`.
- **When:** 2026-11-14, ticket by Max (MF-4, D15, D26, N-05); the config shape and the `stuck` kind were Factory decided (technical) in the plan on PR #257.
- **Source:** https://github.com/maxharris1/factory/issues/170, plan https://github.com/maxharris1/factory/pull/257#issuecomment-6884989536
- **Replaces:** the single `github_app` block in [122-approver-credential.md](122-approver-credential.md) (its Config table's `github_app.*` rows and its Rollout step 2). Everything else in 122 stands.

## Why

One App login for four bots (#122's shape) still lets any bot post a header that says `from=grok-bot`, and an `answers` from anyone trusted for `grok-bot` takes a ticket out of `waiting-max` (N-05). GitHub permissions cannot tell comment kinds apart, so a separate login per bot is what lets the factory scope trust by kind.

Max creates the Apps and places the keys himself. orch never opens, reads, stats or prints a key: `check-config` is tested not to touch any of the four key paths.

## Config (`orch.yml`)

```yaml
github_app:
  enabled: false
  apps:
    grok-bot:                # Factory (Grok orchestrator)
      bot_login: factory-grok[bot]
      app_id: 111111
      installations: {maxharris1: 1000001, aut0-code: 1000002}
      private_key_file: /home/box/.config/factory/apps/factory.pem
    claude-bot: {...}        # Claude Bot
    codex-bot: {...}         # Codex Bot
    cursor-bot: {...}        # Cursor Bot
```

| Field | Meaning |
| --- | --- |
| `github_app.enabled` | The one switch. Default `false`: trust is today's `trusted_logins` rule, byte for byte. |
| `github_app.apps.<bot>` | One App per bot label: `grok-bot`, `claude-bot`, `codex-bot`, `cursor-bot`. Any other key is refused. |
| `.bot_login` | Required. The App's login, `<name>[bot]`. |
| `.app_id` | Required. Positive integer. |
| `.installations` | Required, not empty. Account login to installation id. |
| `.private_key_file` | Required. Absolute path; never opened. |

Always refused: a missing `bot_login` or `installations`, an unknown App key, two Apps sharing a `bot_login` or an `app_id` (that is MF-4 again), and #122's flat shape (`github_app.app_id` and the rest), with a message naming the new place.

With `enabled: true`, `check-config` also requires:

- `apps.grok-bot`. The other three are optional: a bot without an App keeps posting as a `trusted_logins` user login.
- each App's `bot_login` to be a `trusted_logins` key whose labels are exactly `[<its bot>]`;
- each App to have an installation for the owner of every `projects[].repo`;
- `approver_logins` (as in #122).

## Trust map

| Comment | Switch off (default) | Switch on |
| --- | --- | --- |
| `from=grok-bot` (`brief`, `go`, `answers`, `stuck`, wakes) from the Factory login | Trusted if `trusted_logins` gives it `grok-bot` | Trusted |
| `from=grok-bot` from a worker App login | Trusted if `trusted_logins` gives it `grok-bot` | Refused, even if `trusted_logins` wrongly gives it `grok-bot` |
| `answers` (incl. `state=abandoned`), `go`, `ok`, `decline`, `brief` from a worker App login | As `trusted_logins` says | Refused for any `from=` |
| `plan`, `questions`, `ready`, `verdict`, `stuck` from a worker App login, `from=` its own bot | As `trusted_logins` says | Trusted |
| The same from a worker App login, `from=` another bot | As `trusted_logins` says | Refused |
| Capped `go`, hold `ok` | #122's rule | #122's rule: only `approver_logins`, which never holds a `[bot]` login |
| A user login that is no App's `bot_login` | As `trusted_logins` says | As `trusted_logins` says |

Two layers enforce it. `check-config` keeps each App login on its own bot, so every reader of `trusted_logins` (feeder, evidence modules) sees the scoped map. The dispatcher also narrows an App login to its own bot and, for a worker App, to the five worker kinds (`Dispatcher._trusted_bots`, `_app_may_post`). The second layer holds even if `orch.yml` were wrong.

`stuck` is a worker kind because builders and reviewers report a stop with it today (`lib/hook_t7.py`). It only wakes Grok and moves no state. The ticket listed four kinds.

## Permissions per App

Repository permissions only, least privilege. Each App is installed on `maxharris1` and `aut0-code`, on the repos the factory works in. No App gets Administration, Secrets, Environments, Members or any organization permission, and no App gets webhooks: the poller stays a poller.

| App (label) | Contents | Pull requests | Issues | Metadata | Checks, Commit statuses, Actions | Workflows | Why |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Factory (`grok-bot`) | Read and write | Read and write | Read and write | Read | Read | None | The feeder opens draft PRs on `factory/issue-<n>`; the poller marks PRs ready, labels them, squash-merges approved heads and updates branches; Grok posts briefs, `go`, `answers` and labels tickets. |
| Claude Bot (`claude-bot`) | Read and write | Read and write | Read and write | Read | Read | None | Builder: pushes PR branches, opens a PR when the ticket says so, posts `plan`, `questions`, `ready`, `stuck`. |
| Codex Bot (`codex-bot`) | Read | Read and write | Read and write | Read | Read | None | Reviewer: reads the diff and CI, posts `verdict` on the PR's issue thread. Never pushes. |
| Cursor Bot (`cursor-bot`) | Read and write | Read and write | Read and write | Read | Read | None | Builder, same as Claude Bot. |

A builder that must edit `.github/workflows/` needs Workflows: read and write on its App. Leave it off until a ticket needs it.

GitHub cannot stop a worker App from merging, approving or labelling once it has Pull requests: write. That stays a rule in the job briefs, and a worker's merge would not be a factory ship.

## Rollout (Max, then Factory)

1. Max creates the four Apps with the permissions above, installs each on `maxharris1` and `aut0-code`, and places each key on the box.
2. Fill in `github_app.apps` in `orch.yml` with the real numbers and key paths; keep `enabled: false`.
3. Add `trusted_logins[<app bot_login>]: [<its bot>]` for each App.
4. Trim `maxharris1` to `[grok-bot]` (or drop it once Grok posts as Factory).
5. Move each harness user's `gh` onto its own App (#171 mints the tokens).
6. Run `orch check-config`, set `enabled: true`, run it again.
7. Restart the poller and the waiters.

## What this change cannot check

- It never reads a key, so it cannot tell whether a key file matches its App.
- It cannot tell which App a `gh` profile is logged in as. Step 5 is Max's.
- Linear authors (`linear:<id>`) are not GitHub logins and keep `trusted_linear_users` trust in both switch states.
