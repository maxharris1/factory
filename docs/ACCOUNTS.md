# Sign in an extra harness account

This guide signs one extra Claude or Codex account in on each device, so the
factory can run that account's jobs from its own folder. Max does step 1.
Step 2 runs as each bot on each listed device. Step 3 runs on the orch host.

The factory runs a login only when Max presses Sign in on the Settings
page ([Sign in from Settings](#sign-in-from-settings)). It never reads a
credential file.
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
  the check time, how old that device's usage reading is, and any other
  account with the same sign-in. A row that is not `ok` shows the sign-in
  command and the login to run it as. The reading age is one of three texts.
  `read 4m 12s ago` means a usage number from that device is still live.
  `stale, read 1d 1h ago` means its newest number is older than 24 hours or
  every window it reported has reset. `no reading` means no job there has
  reported a number yet. For Codex the age is the age of the session event
  the number came from, so an idle Codex device ages even when it is read
  again. A Cursor row shows no reading age.
- The account's last 5 settings results. A change that a later change
  replaced reads `superseded by <id>`.

Only the approver can sign in to the page. Signed in, the panel shows an
Add account form. Each card shows Apply max_jobs, Apply devices, Pause or
Resume, and Remove, plus Prepare, Run doctor, Sign in and Sign out for each of its devices. A
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
| `add` | `harness`, `max_jobs`, `signed_in_on` | Add account | Appends the account to `accounts:`. `harness` is `claude` or `codex`. Choosing `cursor` shows the machines.yml step and disables Add, and the applier refuses a `cursor` add with that step. `max_jobs` is 1 to 64. `signed_in_on` lists devices that list the harness. |
| `set` | `field`, `expect`, `value` | Apply max_jobs, Apply devices, Pause or Resume | Sets one `field`, which is `max_jobs` (1 to 64), `signed_in_on` (a list of devices) or `paused` (true or false). `expect` is the value the card showed. Pause writes `paused: true`. Resume deletes the line. |
| `remove` | `expect` | Remove | Deletes the account's block and keeps its comments. `expect` is the account as the card showed it, with `harness`, `max_jobs`, `paused` and `signed_in_on`. |
| `prepare` | `device` | Prepare | Makes the account's folder on `device`, then runs the doctor. It edits nothing. |
| `doctor` | `device` | Run doctor | Makes the account's folder on `device`, as Prepare does, then runs the doctor. It edits nothing. |
| `signin` | `device` | Sign in | Not sent to `/api/settings`, which refuses it with `bad-op`. The page POSTs `{account, device}` to `/api/accounts/signin`, and the server spools `{target: "account", op: "signin", account, device}` for the sign-in broker, not the applier. [Sign in from Settings](#sign-in-from-settings) describes it. |
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
| `a cursor account is a device's cursor login: set harnesses.cursor: {bot: cursor-bot, account: <id>} on each device in machines.yml` | An `add` with harness `cursor`. A Cursor account is a device's cursor login, which the panel cannot set, so edit `machines.yml` by hand. |
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
| `invalid after the edit: <reason>` | The edited file does not load. A Claude or Codex account needs at least one device in `signed_in_on`. |
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

## Sign in from Settings

Sign in on a card's device row signs that account in on that device from
the page. It is on only when the orch host's `orch.yml` sets
`dashboard.settings.relay` and the dashboard's `http.env` sets
`DASHBOARD_SIGNIN_RELAY`. Only a Claude or Codex card shows the button,
and the broker refuses the device's own login. Without the relay, use the
[CLI fallback](#the-cli-fallback).
[decisions/1067-dashboard-signin.md](decisions/1067-dashboard-signin.md)
records the time limit, the cap, the code expiry and the URL allowlist.

### What Max sees

1. Press Sign in. The card shows the attempt's state: `accepted`,
   `preparing` (only when the account's folder is missing, which the broker
   then makes), `launching`,
   then `awaiting-url`.
2. For Claude, the card shows a `https://claude.com/cai/oauth/authorize`
   link and a paste box (`awaiting-code`). Open the link, sign in to the
   account's own subscription, and paste the code Claude gives back. The
   page sends the code once.
3. For Codex, the card shows the `https://auth.openai.com/codex/device`
   link and a code such as `ABCD-1234` (`awaiting-approval`). Open the link
   and enter the code.
4. The card shows `verifying` while the broker runs a doctor pass, then the
   result.

Cancel ends the attempt at any step. An attempt that is not done 15 minutes
after the CLI starts ends `expired`. Each result is one line in the
settings audit and one entry in the card's last results. A relay error
before the CLI starts writes no line. The broker picks the request up again
on its next pass.

| Result | Error | Cause | Fix |
| --- | --- | --- | --- |
| `ok` | (none) | The doctor reads the pair `ok`, and every other account on the device reads as it did before. | None. |
| `mismatch` | `shared-identity` | The doctor reads the pair `mismatch`: the new sign-in has the same org and email as another account, or the account has another sign-in on another device. This wins over `disturbed-account`. | Sign in again to the account's own subscription, then read the doctor detail. |
| `mismatch` | `disturbed-account` | Another account on the device changed while this one signed in. The doctor detail reads `signing in changed account <ids>`. | See [The macOS Keychain check](#the-macos-keychain-check). |
| `failed` | `token-auth` | The CLI signed in from a token variable, not the folder. | Follow [Move the Mac's profile token off the default account](#move-the-macs-profile-token-off-the-default-account). |
| `failed` | `signed-out`, `unreachable` | The doctor pass after the CLI found no sign-in, or could not reach the device. | Sign in again, or fix ssh to the device. |
| `failed` | `unrecognized-output` | The CLI printed no allowed link (and, for Codex, no code) in its first 30 seconds. | Use the CLI fallback, then check the CLI's version. |
| `failed` | `doctor-db`, `no-doctor-row` | The broker could not write the doctor's rows, or the doctor returned no row for the pair. | Run `orch accounts doctor` on the orch host and read its output. |
| `failed` | `prepare exited <code>: ...`, `launch: <error>` | Making the folder, or starting the CLI over ssh, failed. | Run Prepare, or the CLI fallback, and read the error. |
| `expired` | (none) | 15 minutes passed. | Press Sign in again. |
| `cancelled` | (none) | Max pressed Cancel, or the broker stopped. | None. |
| `refused` | a code below | The broker refused the request before it started the CLI. | See the broker's refusals. |

### How a sign-in travels

1. The page POSTs `{account, device}` to `/api/accounts/signin`. The
   server checks it against the `status.json` it serves and writes an
   `op: signin` request into the settings spool. It never runs the CLI.
2. The sign-in broker on the orch host (`orch accounts signin-broker`)
   lists the spool every 2 seconds. Over ssh it backs off to 30 seconds
   while idle. It reads the spool and the relay the way `orch settings apply` does: over
   the push login's one multiplexed ssh connection to `dashboard.push.host`
   when the push is ssh, else as local directories.
3. The broker checks the request again against `machines.yml`, the running
   jobs and its own live attempts, then holds the pair in `signout_locks`,
   so no job is placed on it and no sign-out runs while it signs in.
4. The broker runs the harness's login with `ssh -tt` to the device as the
   harness's login there, in the account's folder. It keeps the CLI's output
   in memory and publishes only the parsed link and code to `<id>.relay`.
5. The page polls `GET /api/accounts/signin?id=<id>`. The server answers
   only the session that started the attempt, and re-checks the link and
   code against the same allowlist before it shows them.
6. A Claude paste code goes from the page to `<id>.code`, and Cancel to
   `<id>.cancel`. The broker takes each one (renames, reads and removes it)
   and types the code into the CLI once.
7. When the CLI exits, the broker runs a full doctor pass, which reads
   every pair and rewrites the `account_signins` table, and judges the
   result. It writes one audit line, releases the hold, and
   replaces `<id>.relay` with only the result and error.

### Set up the relay

1. Create the relay directory beside the settings spool, on the host that
   serves the dashboard. The dashboard user (`dashboard.settings.spool_owner`)
   owns it. Its group is the push login's group, which the server unit
   also runs in (`SupplementaryGroups=`). With no ssh push, use the group
   of the user the broker runs as. Give it mode 2770, so the group
   can read and rename each file and the world has no access. The server
   refuses to start on a relay that is relative, missing, a symlink, not a
   directory or open to the world. For example:

   ```sh
   sudo install -d -m 2770 -o factory-dash -g <push login group> /home/factory-dash/signin-relay
   ```

2. Add `DASHBOARD_SIGNIN_RELAY=/home/factory-dash/signin-relay` to
   `/etc/factory-dashboard/http.env`, then run `install` again. `install`
   writes a `BindPaths=` line for the relay into the unit's `paths.conf`
   drop-in, so the sandboxed server can write there. Setting the relay
   without `DASHBOARD_SETTINGS_SPOOL` is a configuration error.
3. Add the same path to the orch host's `orch.yml` (or `orch.local.yml`):

   ```yaml
   dashboard:
     settings:
       relay: /home/factory-dash/signin-relay
   ```

   Without `dashboard.settings.relay`, the broker prints `dashboard sign-in
   is off` and exits.
4. On the orch host, as the checkout owner, run
   `deploy/signin-broker-service.sh install-autostart`. Self-heal then keeps
   the broker running ("Self-heal" in [README.md](../README.md)). The broker
   logs to `signin-broker.log` in the state directory.

### The broker's refusals

A refusal ends the attempt `refused` and starts no CLI.

| Code | Cause | Fix |
| --- | --- | --- |
| `untrusted-owner`, `untrusted-mode`, `not-a-regular-file`, `too-large`, `spool-not-a-directory`, `spool-writable-by-others` | The spool or the request file fails the trust checks `orch settings apply` uses. `untrusted-owner` means `dashboard.settings.spool_owner` is not the user the dashboard server runs as. | Fix the spool's owner and modes, or `spool_owner`. |
| `not-approver` | The request's actor is not `dashboard.settings.approver`. | Sign in to the page as the approver. |
| `unknown-account`, `unknown-device`, `<account> is not signed in on <device>` | `machines.yml` on the orch host does not list the pair. | Add the device to the account's `signed_in_on`. |
| `machines.yml is invalid`, `no orch.db to record the doctor in` | The orch host cannot load its config or has no database yet. | Fix the orch host. |
| `default-account` | The account is the device's own login. | Use the CLI fallback with the device's own login command. |
| `device has install placeholders`, `no ssh_alias for <harness> on <device>` | The device has no ssh path for the harness. | Fill in the device in `machines.yml`. |
| `no-home` | The device gives no `home` for the account's folder. | Set `home` on the device. |
| `unreachable` | ssh to the device timed out or exited 255. | Fix ssh to the device as the bot. |
| `attempt-live` | An attempt for this pair is still live. | Wait for it, or cancel it. |
| `too-many` | 2 attempts are already live. | Wait for one to finish. |
| `running-job` | The pair runs a job. | Wait for the job to finish. |
| `signing-out` | Another request's hold on the pair is less than 10 minutes old, usually a sign-out, or self-heal is restarting the broker. | Wait, then press Sign in again. |
| `cannot read running jobs` | The running-job check could not read `orch.db`. | Fix the orch host's database. |

### The server's refusals

The server refuses a sign-in request before it writes anything. The other
codes in [Refusals by the server](#refusals-by-the-server) apply too.

| Status | Code | Cause |
| --- | --- | --- |
| 400 | `bad-keys`, `bad-value` | The body is not exactly `account` and `device` (Sign in), `id` and `code` (paste code) or `id` (Cancel), of the right shape. |
| 400 | `bad-code` | The paste code is not 8 to 512 characters of letters, digits and `._~#-`. |
| 403 | `read-only` | A default account, or an account the snapshot marks read-only. |
| 403 | `no-signin` | The account's harness has no dashboard sign-in (Cursor). |
| 404 | `not-in-snapshot` | The account, or the device in its `signed_in_on`, is not in `status.json`. |
| 404 | `no-attempt` | No attempt with that id belongs to this session. The server keeps attempts in memory for 20 minutes, at most 8, so a restart of the server or a ninth attempt also forgets one. |
| 409 | `running-job` | The snapshot shows a running job on that device. |
| 409 | `no-code`, `code-used` | A paste code for a Codex attempt, or a second paste code. |
| 500 | `relay` | The server could not write to the relay. |

### What is never stored

The CLI's output stays in the broker's memory. The sign-in link, the Codex
code, the Claude paste code and any token or email the CLI prints never
reach anything the factory keeps:

- no log. The broker's `signin-broker.log` holds error messages, exit
  codes and device names, never CLI output. The dashboard server logs only
  the method, path, status and client address.
- no `settings-audit.jsonl` line. The line holds the actor, the times, the
  account, the device, `field: signin`, the doctor status before and after,
  the result and the error.
- no `status.json`. The card shows only what the doctor keeps.
- no `orch.db` or `orch.health.db` row. `orch.db` keeps the doctor's row
  (status, auth method, plan and a 12-character hash of the org and email)
  and the pair's hold. `orch.health.db` keeps the broker's heartbeat.
- no job files. A sign-in is not a job and has no job row or folder.

The relay files are 0640 and the only files that carry the link and the
Codex code. `<id>.relay` holds them while the attempt is live, and the
broker replaces it with only the result and error when the attempt ends.
Nothing removes `<id>.relay` files. If the broker stops mid-attempt, or its
last write fails, the link and code stay in that file until Max deletes
it. The broker removes `<id>.code` and `<id>.cancel`
when it takes them. The server removes a `<id>.code` the broker has not
taken after 120 seconds, and a `<id>.cancel` after 900 seconds.

Out of scope: the CLI's own credential files in `~/.factory-accounts/<id>`
on the device, and the macOS Keychain entries the CLI writes. Those belong
to the CLI, as they do after a CLI sign-in.

### The macOS Keychain check

On a Mac, the Claude CLI keeps its sign-in in the login Keychain, not only
in the folder. The broker runs the login with the five token variables and
`CLAUDE_SECURESTORAGE_CONFIG_DIR` unset, so no inherited variable points
the CLI at another account's sign-in. The factory cannot read the Keychain
to prove the entries stay apart. It checks the effect instead. Before each
attempt the broker reads the doctor's view of every other account of the
harness on the device, and after the CLI exits it reads them again.
If any account that read `ok` before is no longer `ok` with the same org
and email, the attempt ends `mismatch` with `disturbed-account`, the pair's
row reads `signing in changed account <ids>`, and the card shows the CLI
command. Sign this account and each named account back in with the CLI
fallback, then press Run doctor for each.

Before the first sign-in on the Mac, follow
[Move the Mac's profile token off the default account](#move-the-macs-profile-token-off-the-default-account).

### The CLI fallback

Every Claude or Codex card row that is not `ok` still shows the step 2
command and the login to run it as, unless the device's home in
`machines.yml` is still an install placeholder. It is the only way to sign
in when the relay is not set up, for the device's own login, and after a
`mismatch`:

```sh
env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR=~/.factory-accounts/<id> claude auth login
CODEX_HOME=~/.factory-accounts/<id> codex login --device-auth
```

Then press Run doctor on the card, or run `orch accounts doctor` on the
orch host.

A Cursor card row shows the doctor's status and the command, but no Sign
in, because the sign-in broker covers only Claude and Codex. A Cursor
account is the device's cursor login itself, so sign it in by hand on the
device as that login:

```sh
env -u CURSOR_API_KEY -u CURSOR_AUTH_TOKEN -u CURSOR_CONFIG_DIR cursor-agent login
```

On macOS the Cursor CLI keeps that login in the Keychain, so no variable
moves it to a folder and a Cursor account has no `signed_in_on`.

The Cursor CLI reports no usage without reading its login token, and
nothing in the factory reads a credential, so no probe reads a Cursor
account. Record its weekly used and reset on the account itself:

```sh
bin/orch usage record --account cursor-ultra --window weekly --used 0.01 --resets-at <epoch>
```

A Cursor card then shows 5h used, 7d used and headroom from that reading,
and the Efficiency page shows it on the account's row with source `usage
record`. There is no pooled row. A Cursor account with no reading shows its
plan and `not tracked: <reason>` instead, on its own muted row on
Efficiency; the reason comes from `NOT_TRACKED` in `lib/usage_readings.py`.

### Cursor builds and reviews

Max allowed Cursor as a build and review harness, so `orch.yml` no longer
lists it in `refused_harnesses` (`docs/decisions/1411-cursor-accounts.md`).
A Cursor job runs on the device's `cursor-bot` login, the one Cursor account
there, and the cross-family rule still picks a reviewer of another model
family.

### Gemini starts refused

Gemini runs Google's Antigravity `agy` CLI on Max's Google AI Ultra plan
([decision](decisions/1412-gemini-harness.md)). A Gemini account is each
device's `gemini-bot` login, signed in with interactive `agy` (the command
in §2). The doctor checks each gemini login with `agy models`, and a card
row shows its sign-in command. The
card has no Sign in button: the broker covers Claude and Codex only. Max
decided that `orch.yml` lists `gemini` in `refused_harnesses` until its
accounts are signed in on the devices, so a gemini row in `account_signins`
changes no placement until then.

Gemini quota is tracked. At each gemini job's start, `orch job run` runs
`agy -p /usage --output-format json` as the device's gemini-bot with the
doctor's Google/Gemini overrides unset; agy answers with no model turn.
The card and the Efficiency row show its 5h and weekly windows from the
`Gemini Models` group.

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

For a Cursor account there is no account folder. `CURSOR_CONFIG_DIR` moves
only `cli-config.json`; the login stays in `~/.config/cursor/auth.json` on
Linux and in the Keychain on macOS. So the account is the `cursor-bot` login
on each device, named inline in `machines.yml` on every device that runs it
(`cursor: {bot: cursor-bot, account: <id>}`). Sign `cursor-bot` in on each
device with the command under "A Cursor card row" above. Settings cannot
sign a device login out; run `cursor-agent logout` as `cursor-bot` on the
device instead.

For a Gemini account there is no account folder either. Only a different
`HOME` moves the Antigravity `agy` login, so the account is the
`gemini-bot` login on each device, named inline in `machines.yml` on every
device that runs it (`gemini: {bot: gemini-bot, account: <id>}`). Sign
`gemini-bot` in on each device with interactive `agy`:

```sh
env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u GOOGLE_CLOUD_ACCESS_TOKEN -u GOOGLE_APPLICATION_CREDENTIALS -u GOOGLE_GENAI_USE_GCA -u GOOGLE_GENAI_USE_VERTEXAI -u GEMINI_CLI_USE_COMPUTE_ADC -u CLOUD_SHELL -u GOOGLE_GEMINI_BASE_URL -u GEMINI_CLI_CUSTOM_HEADERS -u GEMINI_CLI_HOME agy
```

The `-u` flags keep every API key and token override out of the login, so
no key in the login profile stands in for it. Sign in with the Google AI
Ultra account, then quit `agy`.

The doctor runs `agy models` with the same variables unset. A model list
reads `ok`. Exit 1 with `Please sign in to view available models` reads
`signed-out`. Any other reply reads `signed-out` with `status reply not
understood`. No prompt reaches a model. Gemini quota is tracked: each
gemini job's start reads `agy -p /usage --output-format json` under the
same rule, as "Gemini starts refused" above describes.

## 3. Check every sign-in

On the orch host, run:

```sh
orch accounts doctor
```

It reads the database from `--db` or `$ORCH_DB`. It asks every device over
ssh, as each harness's login there, with the token variables unset. For
Cursor it runs `cursor-agent status --format json` with `CURSOR_API_KEY` and
`CURSOR_AUTH_TOKEN` unset, and reads only the reply, never a credential
file. It keeps a hash of the Cursor user id, never the email. It prints one line per
pair, `<account>@<device> <status> <auth method or -> <detail>`. Confirm
the new account's row on each listed device reads `ok`. The poller runs the
same check on its first poll, every 30 minutes, and after it adopts a
changed `machines.yml`.

## Usage refresh

To refresh every subscription's usage at once, run on the orch host:

```sh
orch usage refresh [--db PATH] [--machines PATH] [--json]
```

It runs the same doctor pass as `orch accounts doctor`, so it rewrites the
sign-in rows and raises or clears `account-signin` flags. Then it reads
usage for each pair the doctor judged `ok`, with the scripts a job's start
uses: Claude's `get_usage`, the Codex rollout and Gemini's `agy /usage`.
The reads run in parallel under one 30 second deadline. It stores each
reading under its own account and device and creates no job row.

It prints one line per pair:

```text
claude-b@far signed-out 5h 40% 7d 40% 2h old (get_usage); signed-out: not logged in
```

The line gives the account, the device, the doctor's status and the 5-hour
and 7-day percentages. A `-` means no fresh reading. The line ends with
`read now`, or the stored reading's age and source, or the reason the pair
was not read. A count line per outcome follows. `--json` prints the pairs
as a list instead.

Cursor is not read. Its usage stays on `orch usage record`, and its line
shows the reading recorded there. The command reads no credential file and
signs nothing in. Run `orch usage migrate` first. It exits 0 after a pass,
2 on a refused argument or a missing machines file, and 1 when the
database cannot be written.

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
| `ok` | (empty) | Claude reports a `claude.ai` sign-in with an org and email. Codex reports `Logged in using ChatGPT`. Cursor reports `authenticated`. | None. |
| `signed-out` | `not logged in` | The status command reports no sign-in. | Run step 2. |
| `signed-out` | `status reply not understood` | Claude's reply has no `loggedIn`, Codex's first line is unknown, or Cursor's `status` is unknown. | Run the status command as the bot and read its reply. |
| `signed-out` | `status command exited nonzero` | The reply says signed in, but the command failed, or Cursor's reply is `error`. | Run the status command as the bot and read its error. |
| `signed-out` | `harness CLI not found` | The status command exited 127, so the CLI is not on the bot's login `PATH`. The doctor records it as a drop. | Install the harness CLI for the bot's login on the device. |
| `signed-out` | `partly signed in` | Cursor reports `partially-authenticated`. | Run the Cursor login again. |
| `signed-out` | `sign-in names no org and email` | Claude's `claude.ai` sign-in names no org and email. | Run the Claude login again. |
| `signed-out` | `auth from a token variable, not the account folder` | Claude's auth method is `oauth_token`, `api_key`, `auth_token` or `apiKeyHelper`. | Follow [Move the Mac's profile token off the default account](#move-the-macs-profile-token-off-the-default-account). |
| `signed-out` | `api key, not a ChatGPT sign-in` | Codex signed in with an API key. | Run the Codex login and sign in with ChatGPT. |
| `signed-out` | `auth method not understood` | Claude names no auth method, or one the doctor does not know. | Run the Claude login again. |
| `mismatch` | `same sign-in as account <ids>` | Another account has the same Claude org and email, or the same Cursor user. | Sign this account in to its own subscription. |
| `mismatch` | `another sign-in on device <devices>` | This account has a different org and email, or a different Cursor user, on the named devices. | Sign the account in again on the device that has the wrong subscription. |
| `mismatch` | `signing in changed account <ids>` | A dashboard sign-in of this account changed the named accounts' sign-in on the same device. | Sign this account in with the CLI command, then check the named accounts. |
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
- `install the <harness> CLI for <login> on <device>, then orch accounts doctor`
- `fix ssh to <device> for <harness>, then orch accounts doctor`

`<sign-in command>` is the step 2 command for the account's folder, with
`<folder>` in place of `~/.factory-accounts/<id>`. For the device's own
login it is `env -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDE_CONFIG_DIR claude auth login`,
`env -u CODEX_HOME codex login --device-auth` or
`env -u CURSOR_API_KEY -u CURSOR_AUTH_TOKEN -u CURSOR_CONFIG_DIR cursor-agent login`.

The next pass that finds the row `ok` clears the flag. So does the pass
after the account leaves the device's `signed_in_on`. A flag still raised
keeps its first raise time and takes the new detail. When the checkout's
`origin` is not a GitHub repo, the doctor writes its rows and raises no
flags.
