# Sign in an extra harness account

This guide signs one extra Claude or Codex account in on each device, so the
factory can run that account's jobs from its own folder. Max does step 1.
Step 2 runs as each bot on each listed device. Step 3 runs on the orch host.

The factory never runs a login and never reads a credential file.
`orch accounts prepare` makes folders and links. `orch accounts doctor` runs
`claude auth status` or `codex login status` and keeps only the status, the
auth method, the plan name and a 12-character hash of the org and email.

## 1. Declare the account

Add the account to `accounts:` in `machines.yml`, with `signed_in_on`
listing its devices. Give each listed device `home`, the bot's home folder
there. [Usage accounts](../README.md#usage-accounts) has the full rules.

```yaml
accounts:
  - {id: claude-max-2, harness: claude, plan: max, signed_in_on: [pc, box]}
```

Merge the change. `orch accounts prepare` reads the `machines.yml` of the
checkout it runs from, so wait until each listed device's checkout has it.

## 2. Sign in as each bot on each listed device

Open a login shell on the device as the login the factory uses there for
the account's harness. That is the device's `ssh_user` when it sets one,
else the bot user. Its `~` must be the device's `home`. Make the folder:

```sh
orch accounts prepare --device <device>
```

It makes `~/.factory-accounts/<id>` with mode 700 for each extra account of
this login. For Claude it links `skills`, `agents`, `docs`, `rules` and
`settings.json` in the folder to the same names in `~/.claude`. A Codex
folder gets no links. The device's own login account gets no folder. It
prints one `made`, `mode`, `linked` or `conflict` line per change, or
`ok: nothing to change`, and you can run it again. It exits 2 when it
refuses (another user, an unknown device or account, a `home` mismatch) and
1 on a conflict, a path it will not replace. Until the Claude folder's
`skills` link is in place, every job placed on that account fails its skill
preflight with `preflight-missing: <home>/.factory-accounts/<id>/skills/<path>`.

For a Claude account, run:

```sh
env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=~/.factory-accounts/<id> claude auth login
```

`env -u CLAUDE_CODE_OAUTH_TOKEN` keeps a profile token out of the login.
zsh, the Mac's default shell, does not expand this `~`, because
`CLAUDE_CONFIG_DIR=~/...` is an argument to `env`. In zsh, write `$HOME` in
place of `~`.

For a Codex account, run:

```sh
CODEX_HOME=~/.factory-accounts/<id> codex login --device-auth
```

Sign in with ChatGPT. The doctor counts an API-key login as `signed-out`.

## 3. Check every sign-in

On the orch host, run:

```sh
orch accounts doctor
```

It reads the database from `--db` or `$ORCH_DB`. It asks every device over
ssh, as each harness's login there, with the token variables unset. It prints one line per
pair, `<account>@<device> <status> <auth method or -> <detail>`. Confirm
the new account's row on each listed device reads `ok`. The poller runs the
same check on its first poll, every 30 minutes, and after it adopts a
changed `machines.yml`.

## Why the account folder, not `claude setup-token`

`claude setup-token` makes a long-lived token that you export as
`CLAUDE_CODE_OAUTH_TOKEN`. Exported from a login profile, it reaches every
process of that login. The factory does not use it for an extra account. A
job on an account folder, its usage probe and `skill-probe run --account
<id>` drop `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN` (for Codex, `OPENAI_API_KEY` and `CODEX_API_KEY`)
after the login profile runs. `ACCOUNT_ISOLATION` in `lib/harness_env.py`
names them. The doctor unsets the same variables before it asks about any
account. A sign-in that exists only as a token reads `signed-out`.

## Move the Mac's profile token off the default account

The device's own login account, `<harness>-default`, runs with no folder and
keeps its environment, the token included. The doctor still checks it with
the token unset. While the export stays, the doctor checks a sign-in that
the default account's jobs do not use.

1. Remove the `CLAUDE_CODE_OAUTH_TOKEN` export from the bot's login profile.
2. Open a new login shell as the bot, so that the variable is gone.
3. Run `claude auth login` as the bot.
4. On the orch host, run `orch accounts doctor`.

The Mac's row appears once the Mac lists `claude` in `machines.yml`.

## What each doctor status means

| Status | Detail | Cause | Fix |
| --- | --- | --- | --- |
| `ok` | (empty) | Claude reports a `claude.ai` sign-in with an org and email. Codex reports `Logged in using ChatGPT`. | None. |
| `signed-out` | `not logged in` | The status command reports no sign-in. | Run step 2. |
| `signed-out` | `status reply not understood` | Claude's reply has no `loggedIn`, or Codex's first line is unknown. | Run the status command as the bot and read its reply. |
| `signed-out` | `status command exited nonzero` | The reply says signed in, but the command failed. | Run the status command as the bot and read its error. |
| `signed-out` | `sign-in names no org and email` | Claude's `claude.ai` sign-in names no org and email. | Run the Claude login again. |
| `signed-out` | `auth from a token variable, not the account folder` | Claude's auth method is `oauth_token`, `api_key`, `auth_token` or `apiKeyHelper`. | Remove the token from the bot's login profile, then run the Claude login. |
| `signed-out` | `api key, not a ChatGPT sign-in` | Codex signed in with an API key. | Run the Codex login and sign in with ChatGPT. |
| `signed-out` | `auth method not understood` | Claude names no auth method, or one the doctor does not know. | Run the Claude login again. |
| `mismatch` | `same sign-in as account <ids>` | Another account's folder has the same Claude org and email. | Sign this account in to its own subscription. |
| `mismatch` | `another sign-in on device <devices>` | This account has a different org and email on the named devices. | Sign the account in again on the device that has the wrong subscription. |
| `unreachable` | `device has install placeholders` | A device field still holds a `REPLACE_WITH_` value. | Fill in the device in `machines.yml`. |
| `unreachable` | `no ssh_alias for <harness> on <device>` | The device has no ssh alias for that harness. | Set `ssh_alias` on the device. |
| `unreachable` | `device unreachable over ssh` | ssh timed out after 60 seconds or exited 255. | Fix ssh to the device as the bot. |
| `unreachable` | `no status reply` | ssh ran, but the reply for this account never came back. | Run the status command in a login shell as the bot and read its output. |

A `mismatch` row that has both causes joins the two details with `; `.

## The `account-signin` flag

Each doctor pass, from the poller or by hand, raises one needs-attention
flag for each row that is not `ok`. It goes in the factory's own repo, the
`origin` of the orch checkout. The flag's key is `<account>@<device>` and
its reason is `account-signin`. Its detail is `<status>; run: <step>;
<detail>`, cut to 500 characters. The step is one of these:

- `sign in as <login> on <device>: <home><login command>, then orch accounts doctor`
- `sign <account> in to its own account as <login> on <device>: <home><login command>, then orch accounts doctor`
- `remove the token export from <login>'s login profile on <device>, then orch accounts doctor`
- `fix ssh to <device> for <harness>, then orch accounts doctor`

`<home>` is `CLAUDE_CONFIG_DIR=<folder> ` or `CODEX_HOME=<folder> `, or
`env -u CLAUDE_CONFIG_DIR ` (or `env -u CODEX_HOME `) for the device's own
login. `<login command>` is `claude auth login` or `codex login`.

The next pass that finds the row `ok` clears the flag. So does the pass
after the account leaves the device's `signed_in_on`. A flag still raised
keeps its first raise time and takes the new detail. When the checkout's
`origin` is not a GitHub repo, the doctor writes its rows and raises no
flags.
