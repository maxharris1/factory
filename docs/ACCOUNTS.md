# Sign in an extra harness account

This guide signs one extra Claude or Codex account in on each device, so the
factory can run that account's jobs from its own folder. Max does step 1.
Step 2 runs as each bot on each listed device. Step 3 runs on the orch host.

The factory never runs a login and never reads a credential file.
`orch accounts prepare` makes folders and links. `orch accounts doctor` runs
`claude auth status` or `codex login status` and keeps only the status, the
auth method, the plan name and a 12-character hash of the org and email.

## Manage accounts from Settings

The Accounts panel on the Settings page (`settings.html`) is the usual way
to add, change and remove an account. It shows one card per account. The
login accounts of the devices come first, then each account in `accounts:`.

A card shows these facts:

- `harness`, `max_jobs` and `paused`.
- The 5-hour and 7-day usage, and the headroom beside the floor of 5%.
- The running jobs, each with its ticket, kind and device.
- One row per device the account is signed in on. Each row has the doctor's
  status (`unchecked` before the first check), the auth method, the plan,
  the check time and any other account with the same sign-in. A row that is
  not `ok` shows the sign-in command and the login to run it as.
- The account's last 5 settings results. A change that a later change
  replaced reads `superseded by <id>`.

Only the approver can sign in to the page. Signed in, the panel shows an
Add account form. Each card shows Apply max_jobs, Apply devices, Pause or
Resume, and Remove, plus Prepare, Run doctor and Sign out for each of its devices. A
card marked `read-only: default account` or `read-only: external account`
shows no controls. A device's login account is read-only when `accounts:`
does not declare it. The server refuses every `<harness>-default` account,
declared or not, and every `external` account. The applier refuses an edit
to either.

### Requests

Each control POSTs `/api/settings` with `{target: "account", op, account,
...}` and the op's keys. The server adds `id`, `actor` and `requested_at`
and writes the request to the settings spool. The card then shows
`queued (applies within ~2 min)`, or `error: <code>` when the server refuses.

| Op | Keys | Control | What it does |
| --- | --- | --- | --- |
| `add` | `harness`, `max_jobs`, `signed_in_on` | Add account | Appends the account to `accounts:`. `harness` is `claude`, `codex` or `cursor`. `max_jobs` is 1 to 64. `signed_in_on` lists devices that list the harness. |
| `set` | `field`, `expect`, `value` | Apply max_jobs, Apply devices, Pause or Resume | Sets one `field`, which is `max_jobs` (1 to 64), `signed_in_on` (a list of devices) or `paused` (true or false). `expect` is the value the card showed. Pause writes `paused: true`. Resume deletes the line. |
| `remove` | `expect` | Remove | Deletes the account's block and keeps its comments. `expect` is the account as the card showed it, with `harness`, `max_jobs`, `paused` and `signed_in_on`. |
| `prepare` | `device` | Prepare | Makes the account's folder on `device`, then runs the doctor. It edits nothing. |
| `doctor` | `device` | Run doctor | Makes the account's folder on `device`, as Prepare does, then runs the doctor. It edits nothing. |
| `signout` | `device` | Sign out | Runs `claude auth logout` or `codex logout` as the bot in the account's folder on `device`, with the five token variables and `CLAUDE_SECURESTORAGE_CONFIG_DIR` unset, then runs the doctor. It records `signed-out`, `failed`, or `refused` with `running-job` while the pair runs a job, or `signing-in` while the sign-in broker holds the pair. From the running-job check until its own doctor pass ends, a row in `signout_locks` keeps placement off the pair. The row names the request that took it, and only that request renews or deletes it. A sign-in attempt takes the same row, so a sign-out and a sign-in never hold one pair at once. A doctor pass never clears that row, and placement ignores one older than ten minutes. |

### Refusals by the server

The server checks each request against the `status.json` the page was
drawn from. A refused request writes nothing.

| Status | Code | Cause |
| --- | --- | --- |
| 400 | `bad-op` | The op is not one of the six. |
| 400 | `bad-keys` | The keys are not exactly `target`, `op`, `account` and the op's keys. |
| 400 | `bad-field` | A `set` names a field other than `max_jobs`, `signed_in_on` or `paused`. |
| 400 | `bad-value` | A bad account id, or a value of the wrong shape. Examples are a `max_jobs` outside 1 to 64, a device named twice, an unknown harness, and a remove `expect` without its four keys. |
| 400 | `bad-json`, `bad-length` | The body is not one JSON object, or it is too long. |
| 401 | `no-session` | Not signed in, or the session ended. |
| 403 | `content-type`, `origin`, `csrf` | The request did not come from the page. |
| 403 | `not-approver` | The login is not the approver. |
| 403 | `read-only` | A `<harness>-default` account, an account the snapshot marks read-only, or an `add` with harness `external`. |
| 404 | `not-in-snapshot` | The account is not in `status.json` (an `add` is exempt). Or a device is not in it, or a `prepare`, `doctor` or `signout` device is not in the account's `signed_in_on`. |
| 409 | `exists` | An `add` names an account that exists. |
| 409 | `changed` | `expect` no longer matches the snapshot. Reload the page and try again. |
| 429 | `busy` | 20 requests already wait to be applied. |
| 500 | `spool` | The server could not write the request file. |

With settings off, `/api/settings` answers 404.

### Refusals by the applier

Each `deploy/self-heal.sh once` pass on the orch host runs `orch settings
apply`. An `add`, `set` or `remove` takes the same path as a device change.
That is the claim lock, a backup, a line edit, a restart of the poller and
waiters, and one audit line ("Device settings" in [README.md](../README.md)). A
refused request leaves `machines.yml` byte-identical, writes no backup and
shows `refused` with the reason on the card.

| Reason | Cause |
| --- | --- |
| `not-approver` | The actor is not `dashboard.settings.approver`, or is a bot. |
| `machines.yml is invalid` | The current file does not load. |
| `read-only` | A `<harness>-default` or `external` account. |
| `unknown-account` | The account is not in `accounts:`. |
| `exists` | An `add` names an account that exists. |
| `bad-id`, `unknown-harness`, `bad-field`, `bad-value` | The applier's own shape checks, which repeat the server's. |
| `unknown-device`, `harness-not-on-device` | A device in `signed_in_on` is not in `machines.yml`, or does not list the account's harness. |
| `<field> is set in machines.local.yml`, `<account> is set in machines.local.yml` | The overlay sets that key, or names that account, so the edit would not take effect. |
| `changed` | `expect` no longer matches the file. |
| `no-change` | The value is already set. |
| `not-paused` | A `remove` of an account that is not paused. Pause it first. |
| `<account> holds <n> running job(s)` | A `remove` of an account with a running job on any device. Wait for its jobs to finish. |
| `<account> holds a running job on <device>` | A `signed_in_on` change that drops a device where the account runs a job. |
| `cannot read running jobs` | The running-job check could not read `orch.db`. |
| `cannot edit the account's block` | The line edit cannot find or change the account's block. |
| `invalid after the edit: <reason>` | The edited file does not load. A Claude or Codex account needs at least one device in `signed_in_on`. A Cursor account must be a device's cursor login, which the panel cannot set, so an `add` with harness `cursor` ends here. |
| `the edit changed more than that field`, `the edit changed more than that account` | The edit changed something besides the request. |
| `the edit changed a comment` | The edit changed a comment line. |

A running job counts against its slot's account. A slot that records no
account counts against the device's own login account. A restart that fails
restores the backup and records `failed`. It does not restore a file that
would drop an account, or an account's device, that now holds a running
job. The error then reads `not restored: <reason>`.

### When a change takes effect

Placement reads each account's `paused`, `max_jobs` and `signed_in_on`
from the shared settings row in `orch.db`, never from a process's own copy
of `machines.yml`. It takes the strictest value any side holds. The account
is paused if any side says so, gets the lowest `max_jobs`, and keeps only
the devices every side lists.

- A pause, a lower `max_jobs`, a dropped device or a remove reaches the
  next claim at once.
- A resume, a higher `max_jobs`, an added device or a new account waits
  until the poller adopts the new file. The applier restarts the poller
  after each edit. Until the poller adopts the file, the result reads
  `pending` with `waiting for the poller's next cycle`.
- No change stops a running job. A paused account's running jobs finish.

### Prepare and Run doctor

Prepare runs `<factory_root>/bin/orch accounts prepare --device <device>
--account <id> --machines /dev/stdin` on the device. It goes over the
doctor's ssh path as the harness's login there, with the orch host's
`machines.yml` on stdin. A full doctor pass follows. Run doctor does the
same: it prepares the pair first, then runs the doctor pass. Neither edits
`machines.yml`, writes a backup or restarts anything.

Each writes one result. Its field is the op, and its previous and new
values are the pair's sign-in status before and after. A prepare that fails
still runs the doctor and records `failed`. The error is `prepare exited
<code>` with the last `orch accounts prepare:` line, or the reason the
device has no ssh path. The applier refuses a pair request with
`unknown-account`, `unknown-device`, `<account> is not signed in on
<device>` or `no orch.db to record the doctor in`.

Prepare does not sign the account in. Run the sign-in command the card
shows, as the login it names, then Run doctor.

### Remove keeps the folder

Remove edits `machines.yml` only. It never deletes
`~/.factory-accounts/<id>` on any device. The sign-in survives, and a later
add of the same id finds it.
[decisions/1066-settings-accounts.md](decisions/1066-settings-accounts.md)
records why remove needs a pause and no running job, why it keeps the
folder, and why default and external accounts are read-only.

## 1. Declare the account

Use Add account on the Accounts panel. Give the id, the harness, `max_jobs`
and the devices it will be signed in on. Each device must already give
`home`, the bot's home folder there. Then press Prepare for each device on
the new card. Prepare sends the orch host's `machines.yml`, so a device's
checkout does not need the change merged.
[Usage accounts](../README.md#usage-accounts) has the full rules.

To declare it by hand instead, add the account to `accounts:` in
`machines.yml`, with `signed_in_on` listing its devices.

```yaml
accounts:
  - {id: claude-max-2, harness: claude, plan: max, signed_in_on: [pc, box]}
```

Merge the change. `orch accounts prepare` run by hand reads the
`machines.yml` of the checkout it runs from, so wait until each listed
device's checkout has it.

## 2. Sign in as each bot on each listed device

Open a login shell on the device as the login the factory uses there for
the account's harness. That is the device's `ssh_user` when it sets one,
else the bot user. Its `~` must be the device's `home`. Make the folder:

```sh
orch accounts prepare --device <device>
```

Skip this command when Prepare on the panel succeeded for the device. The
command makes `~/.factory-accounts/<id>` with mode 700 for each extra account of
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
2. In the bot's shell, run `unset CLAUDE_CODE_OAUTH_TOKEN`. A new login
   shell started from a shell that already exported the token inherits it,
   so removing the export alone leaves it set.
3. In that same shell, run `claude auth login` as the bot.
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
| `signed-out` | `auth from a token variable, not the account folder` | Claude's auth method is `oauth_token`, `api_key`, `auth_token` or `apiKeyHelper`. | Follow [Move the Mac's profile token off the default account](#move-the-macs-profile-token-off-the-default-account). |
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

- `sign in as <login> on <device>: <sign-in command>, then orch accounts doctor`
- `sign <account> in to its own account as <login> on <device>: <sign-in command>, then orch accounts doctor`
- `remove the token export from <login>'s login profile on <device>, then orch accounts doctor`
- `fix ssh to <device> for <harness>, then orch accounts doctor`

`<sign-in command>` is the step 2 command for the account's folder, with
`<folder>` in place of `~/.factory-accounts/<id>`. For the device's own
login it is `env -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDE_CONFIG_DIR claude auth login`
or `env -u CODEX_HOME codex login --device-auth`.

The next pass that finds the row `ok` clears the flag. So does the pass
after the account leaves the device's `signed_in_on`. A flag still raised
keeps its first raise time and takes the new detail. When the checkout's
`origin` is not a GitHub repo, the doctor writes its rows and raises no
flags.
