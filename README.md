# factory

Grok Bot orchestrates pstack work across Claude Code, Codex and Cursor.

The specification is [docs/SPEC.md](docs/SPEC.md). This repo has the F1-01,
F1-03, F1-04, F1-05 and F1-09 contracts, the F1-06 blocking wait, the F1-02 pstack pin, the F1-07
`orchestrate-project` skill, the F1-08 usage reserve, the F1-13 orchestrator
model, and the F1-14 weekly eval report. Other Phase 1 items are not built.
Phase 2 has the F2-01 device pool; remote start (F2-02) is not built.

## Contracts

- [`orch.yml`](orch.yml) lists the projects to poll and the dispatcher settings ([docs/SPEC.md](docs/SPEC.md) section 7). [`config/orch.example.yml`](config/orch.example.yml) is the minimal file.
- `factory.md` is the per-repo policy ([docs/SPEC.md](docs/SPEC.md) section 6). The generic example is [`docs/factory.example.md`](docs/factory.example.md). Aut0, Cleffy and Tracy examples live under [`examples/`](examples/) and are not those repos.
- `orch:v1` handoff comments ([docs/SPEC.md](docs/SPEC.md) section 5) are parsed and written by [`lib/handoff.py`](lib/handoff.py).
- [`lib/dispatcher.py`](lib/dispatcher.py) applies T1, T1P and T2 through T6 (F1-05 is
  the T3 review round, T4 ship and T5 questions loop) to
  PRs discovered through `orch:state/` GitHub labels or Linear tickets found by the handoff
  search. It stores a ticket after the first `brief` or `go` from the trusted
  Grok login. A `go` must name `state=planning` or `state=building`.
- Project hooks are passed as `Dispatcher(..., hooks=(T7Hook(), t8))` with
  `t8 = T8Hook()`, then `t8.shipper = ReviewShipper(dispatcher, source)`.
  [`lib/hook_t7.py`](lib/hook_t7.py) wakes Grok Bot once for `verdict=BLOCKED`,
  `kind=stuck` (with `state=capped`, `refused` or `blocked` for those stops),
  and trusted comments with an unparseable `orch:v` header;
  `Dispatcher.wake_grok_once` is the same wake for a later config refusal or
  a T3 cap. [`lib/hook_t8.py`](lib/hook_t8.py) handles the `after:<approver>`
  `ok` and `decline`. The previewed SHA is the full head T4 passed to
  `await_approver`. An `ok` on it, while the reviewer's latest verdict is
  still that APPROVE, calls the `Shipper` it was given at most once; a ship
  error wakes Grok Bot (`ship-failed`) instead of retrying. `ReviewShipper`
  (F1-05) fast-forwards `base_branch` to exactly that head (no merge commit,
  no squash; a non-fast-forward raises) only when T4 completed the
  `after:` ship intent, CI on it is green and it is still the PR head. It
  counts as shipped only when `base_branch`'s tip is exactly that head; a PR
  merged some other way is refused and wakes Grok Bot. `ship: merge` repos
  still squash-merge with `--match-head-commit`; `ship: handoff` repos are
  never merged by the factory.
  Hooks never see `kind=go`, `kind=plan` or `kind=ready`.

## GitHub adapter

[`lib/github_actions.py`](lib/github_actions.py) is the real `ReviewActions`.
The runtime builds one adapter per configured project; Linear projects wrap
the GitHub PR adapter with the optional Ready for Review state move. Unit tests still
pass `FakeReviewActions` to `Dispatcher(review_actions=...)`.

Every call is `env -u GH_TOKEN -u GITHUB_TOKEN -u GH_ENTERPRISE_TOKEN -u
GITHUB_ENTERPRISE_TOKEN gh api ...`, built as an argv list (no shell) and run
by `run_gh`, the one subprocess seam. gh uses only the login stored on the
device; those four token variables are cleared on every call. The adapter never
reads, passes or logs a token. Reads go to GitHub's REST API at call time,
with no local clone and no cache. A GitHub `ticket_id` is the PR number.

| Method | GitHub call | Contract |
|---|---|---|
| `is_merged` | `GET pulls/<n>` | `head.sha` must be a full SHA, open or merged. True only when `merged` and `head.sha` is exactly the head |
| `branch_tip` | `GET git/ref/heads/<branch>` | The full 40-char lowercase SHA of the remote ref, or raise |
| `fast_forward` | `PATCH git/refs/heads/<branch>` with `sha=<head>`, `force=false` | Never `force=true`. GitHub's 422 "not a fast forward" raises `NotFastForwardError`. No merge or squash fallback |
| `merge` | `PUT pulls/<n>/merge` with `merge_method=squash`, `sha=<head>` | The API form of `--squash --match-head-commit`. A 409 head mismatch raises `HeadMismatchError` |
| `list_comments` | `GET issues/<n>/comments`, `--paginate` | Ids must be positive integers (coerced to int); `created_at` must be ISO-8601 |
| `ci_status` | `GET commits/<sha>/check-runs`, `--paginate`, and `GET commits/<sha>/status` | Any failure is `failure`. `success` needs at least one check run that concluded `success`, or a `success` commit status, with nothing pending. All neutral or skipped, anything running, nothing reported, or fewer runs received than `total_count` is `pending` |
| `set_state` | `issues/<n>/labels` | Adds `orch:state/<state>`, then removes other `orch:state/` labels. Linear and Aut0 get none |
| `await_approver` | `POST issues/<n>/comments` | Tells the approver which SHA is previewed |
| `stable_patch_id` | none | Not built; a moved head queues a re-review |
| `handoff` | `GET pulls/<n>`, then, when draft, `gh api -X POST graphql` with `markPullRequestReadyForReview` on the pull's `node_id` (GitHub has no REST route for this; it is what `gh pr ready` sends) | Confirms the linked PR is unmerged and still at the approved head, and that GitHub reports `isDraft: false` at that head; never merges or requests a reviewer |

SHAs must be 40 lowercase hex, and owner, repo, branch and PR number must
match strict patterns before any call. A non-zero gh exit, malformed JSON or a
timeout raises `GitHubAdapterError`, which is a `ShipError`. A ship then
fails closed: `ship-failed` and one Grok wake. `ship_intents.error` records
the class and a safe detail, such as `GitHubAdapterError: gh api POST graphql
exited 1 (HTTP 403) graphql errors: FORBIDDEN`. It never holds gh's output, a
URL, a token or a body; the merge 409 and fast-forward 422 errors carry only
the inner safe detail. A `handoff` that failed before any write was confirmed
is re-driven once on a later poll, and only while its APPROVE is still the
latest trusted reviewer verdict on the ticket (a newer REVISE or APPROVE spends
it with no write), the PR is still at the intent's head with green CI, and the
ticket has not already been handed off, merged or finished, nor completed a
handoff at that head. So a ticket is handed off at most once per head. That
includes rows written before the fix, which recorded only the class name.

Device prerequisites: `gh` is installed and logged in as Max on the factory
device (`gh auth status`). Max provisions that login; the factory never sets
up or reads credentials.

## Queue interface

[`lib/queue.py`](lib/queue.py) is the integration boundary for waiting bots and
routing. A producer calls `enqueue`; an owning bot calls `claim`, periodically
calls `heartbeat`, then calls `finish` with the handoff comment ID. Finishing
without a handoff ID invokes T6. `claim` is intentionally non-blocking so
`orch wait` can add blocking behavior without changing the queue contract. One
clock is injected when the queue opens; all timestamps use that clock.

Jobs name one action (`plan`, `build`, `review`, `fix`, `clarify`, `stuck`,
or the Grok wakes `blocked`, `capped`, `refused`, `unparsed` and `ship-failed`),
one recipient (`claude`, `codex`, `cursor`, or `grok`), and a generation. The
dispatcher and queue use one SQLite database so terminal state and its wake
commit atomically. The dispatcher queues jobs; it never executes a harness
binary.

## Device pool

[`machines.yml`](machines.yml) is the F2-01 device inventory
([docs/SPEC.md](docs/SPEC.md) section 13);
[`config/machines.example.yml`](config/machines.example.yml) is the minimal
file. Each device has a `name`, a `tailscale_name`, `slot_max`, `harnesses`,
`workdir_root` and `enabled` (default `true`). `harnesses` maps each eligible
harness to the bot OS user whose login Max provisioned there, and that user
must be the harness's own bot (`claude: claude-bot`, `codex: codex-bot`,
`cursor: cursor-bot`). The one `local: true` device is the box that runs
`orch`; it omits `tailscale_name` and `workdir_root`. Every other device names
a `workdir_root`, an absolute normalized path that may not be under `/mnt/`,
so jobs on a WSL node stay off the Windows drives. A whole path component may
be `{bot}`, which resolves to the harness's bot user
(`Device.workdir_for(harness)`). A Tailscale name is a device name, never an
IP address. Two devices may not share a `tailscale_name` (compared
case-insensitively): slots are counted per device name, so a second entry
would give one host a second `slot_max`. Release a device's held slots
before renaming it. Unknown fields are refused, and `workdir_root` may not
contain NUL or another control character.

The shipped file lists the box (all three harnesses, 3 slots, because the box
runs at most 3 heavy local agent sessions at once; slots are generic, so they
are not one per bot and 3 Claude jobs can take all 3), the PC's WSL Ubuntu node
and the Mac. The PC and Mac ship
`enabled: false` and stay disabled until F2-02 remote start lands; the Mac also
has no bot users and no SSH yet. The PC's `workdir_root` is
`/home/{bot}/factory-work` (for example `/home/codex-bot/factory-work`), off
`/mnt/c`. F2-02 will reach the PC through the box's SSH aliases
`pc-wsl-claude-bot`, `pc-wsl-codex-bot` and `pc-wsl-cursor-bot`. The Mac's
`workdir_root` is still a `REPLACE_WITH_` placeholder. Nothing in this repo
checks or assumes that a device is reachable.

[`lib/device_pool.py`](lib/device_pool.py) is the scheduler. `DevicePool.claim(harness)`
moves the oldest queued job for that harness to running and holds a slot on
the first device, in `machines.yml` order, that is enabled, has no
placeholder, lists the harness, and has a free slot. Slots are generic: any
free slot on an eligible device takes any harness. The job update and the
`device_slots` row commit in one `BEGIN IMMEDIATE` transaction in the
dispatcher database, so two claims cannot take the same last slot. With no
eligible free slot the job stays queued. Grok jobs never hold a slot.
A queued job never holds a slot: requeueing a claimed job
(`SQLiteJobQueue.release`, running back to queued) frees its slot in the same
transaction, and `claim` drops any slot row left on a queued job before
counting.

Finishing, failing or timing out a job does not free its slot; apart from a
requeue, only `DevicePool.release(job_id, owner=harness)` does, once the
worker is confirmed gone (F2-02). A device that loses power mid-job keeps its slot held, its job
is never `done`, T6 times it out from the last heartbeat and retries once, and
the second failure marks the ticket `stuck`. Phase 1 `orch wait` still claims
through the queue directly and holds no device slot; F2-02 routes claims
through the pool. The pool starts nothing and opens no SSH connection.

## Poll

```sh
bin/orch init-db --db "$ORCH_DB"
bin/orch poll --db "$ORCH_DB" --once
bin/orch poll --db "$ORCH_DB"
```

`orch poll` composes `PollLoop`, `Dispatcher`, `LivePollSource`, the T7/T8
hooks, GitHub review actions, and Grok webhook delivery. `--db` falls back to
`$ORCH_DB`; `--once` performs one complete cycle and exits. GitHub reads use
the existing `gh` login with token environment variables removed. GitHub
discovery is PR-only in Phase 1; a label on an issue is not enough. Linear
projects use GraphQL search with `LINEAR_HANDOFF_QUERY`.

Runtime names live in `orch.yml`. `linear.api_key_env` names the environment
variable containing the Linear API key. `grok.webhook_url_env`,
`grok.webhook_auth_env`, and `grok.webhook_key_env` name the existing Grok
routine webhook variables; the shipped names are `FACTORY_WEBHOOK_URL`,
`FACTORY_WEBHOOK_AUTH`, and `FACTORY_WEBHOOK_KEY`. `FACTORY_WEBHOOK_AUTH` is a
full `Authorization` header value (for example `Bearer <key>`) sent verbatim;
`FACTORY_WEBHOOK_KEY` is a bare key sent as `Bearer <key>`. Set one of them;
when both are set, `FACTORY_WEBHOOK_AUTH` wins. `webhook_auth_env` defaults to
`FACTORY_WEBHOOK_AUTH` when omitted. A missing variable fails by naming every
accepted variable, and a failed delivery is recorded in `poll_errors` as
`grok-delivery:GrokDeliveryError:<detail>`, where the detail is the HTTP
status, the exception type, or the missing variable names, never the URL, key,
headers, or body.
`projects[].factory_ref` selects the trusted branch containing `factory.md`;
`projects[].linear_project_id` maps an immutable Linear project ID to its
repo. `trusted_logins` maps GitHub authors to sets of bot markers, while
`trusted_linear_users` maps immutable Linear user IDs to sets separately.
The dispatcher still requires the expected `from=` and `role=`. With one
shared GitHub login, the marker is a routing label, not an identity or
signature. The shipped config records the known shared GitHub login and leaves
the stable Linear user ID for Max to confirm. Max approved the GitHub map at
3:09 PM ET on November 11, 2026: "Yes, the factory may trust maxharris1 for
comments on factory PRs". This approves the login map, not stronger identity:
any session using that login can choose a marker. While any configured Linear
project lacks `trusted_linear_users`, `orch poll` refuses to start at all,
including for GitHub projects. Missing secrets fail by environment-variable
name; values are never printed or included in exceptions.

Linear issues are searched by ID with comments included; each hit is then
fetched directly so the search stays below Linear's complexity limit. Brief
and go are separate queries rather than relying on undocumented full-text
`OR` behavior. Searches are restricted to configured projects and workflow
states other than completed or canceled, with at most two pages per term.
Search hits already tracked are skipped. Direct issue reads are reused for the
rest of the cycle. Factory uses half of Linear's shared hourly request and
complexity limits, fair-shares discovery and tracked reads, and rotates
deferred tracked tickets across cycles. Linear 429 and `RATELIMITED` responses
pause Linear calls for `Retry-After`; T4 and T8 budget or backoff deferrals
retry next cycle. A linked GitHub PR supplies the current head and CI, and its
issue comments are joined with the Linear comments. `ready`, `verdict`, and
`stuck` go on PR issue comments, never reviews or inline comments.
`projects[].grok_bot` routes stop and terminal results. When a webhook wake's
`target_bot` is set, only that project bot handles it; when it is `null`, Grok
Bot handles it for the repo's project.
`projects[].linear_ready_for_review` defaults off and controls only the
post-APPROVE Linear Ready for Review move; Aut0 enables it. The move is made
only when a fresh read shows the issue still before review (unstarted, or
started and before Ready for Review in the team's order). An issue already in
Ready for Review, or moved on by a human (Done, Canceled, Backlog, Triage, or a
started state after Ready for Review), is left where it is; an unknown state
fails the handoff before any write. No other dispatcher state writes Linear,
and `ship: handoff` never merges.

`orch poll` opens an initialized database without creating it and holds
`<db>.lock`, so a second poller is refused. Source failures are recorded per
project or ticket and the long-running loop continues after the 60-second
interval; `--once` reports a cycle-level failure with a nonzero exit.
Each polling `gh api` read, including PR comments, check runs and commit
status, has a 20-second timeout and is tried three times with 2- and 4-second
backoff (writes are never retried); a ref that still times out records a retryable
`gh api timed out after 3 attempts` error and the cycle moves on.
Linear budget and search page-limit deferrals are expected load shedding, not
errors: they do not fail the cycle and are written as at most one aggregated
row per cycle to `poll_deferrals`, not `poll_errors`.
At startup, polling refuses an invalid `orch.yml` (including placeholders), a
missing GitHub trust map, any configured Linear project with no Linear trust
map, missing Grok webhook environment-variable names, a missing initialized
database, or a lock already held by another poller. Runtime source or delivery
failures stay fail-closed and are recorded instead of weakening those checks.

## Wait

```sh
bin/orch wait claude-bot --db "$ORCH_DB"
bin/orch job heartbeat claude-bot <job-id>
bin/orch job done claude-bot <job-id> --handoff <comment-id>
bin/orch job done claude-bot <job-id> --failed
```

[`lib/wait.py`](lib/wait.py) blocks on the queue above. `orch wait` accepts
only `claude-bot`, `codex-bot`, and `cursor-bot` (recipients `claude`,
`codex`, `cursor`). It checks for a job once a second, claims the oldest, and
prints one JSON line. While the poller holds a write, a claim that sees
`database is locked` (after a 5-second SQLite busy timeout) keeps waiting with
backoff capped at 5 seconds; `job heartbeat` and `job done` retry the same way
up to 8 attempts. Other database errors still fail at once.
If that line cannot be printed, the job is finished as
failed so T6 retries it at once. `orch` never starts a harness: the bot that ran
the shell call starts its own. `job heartbeat` and `job done` refuse a job that
belongs to another bot. The database is `--db`, else `$ORCH_DB`; a missing file
is refused. Each bot's "orch jobs" paragraph is under [`bots/`](bots/).
While a harness runs, its bot loops `orch job heartbeat <bot> <job-id>` at
least every `timeout_seconds / 3`; running timeouts use last activity.

## Install after merge

Do not run these steps until Max gives the go. `deploy/install.sh` creates a
venv, installs `requirements.txt` (including PyYAML), writes an `orch` wrapper
under `$HOME/.local/bin`, initializes one shared database, and writes
`$HOME/.config/factory/profile.sh`. It does not install or start the service.
Stop `orch-poll` before re-running `deploy/install.sh`; both install paths
replace the venv.
Keep the checkout at
`$HOME/factory` on `main`; the service reads that checkout's `orch.yml`.
The script warns for another checkout or branch, refuses to replace a
non-factory `orch` wrapper, and safely recreates the venv on every run while
preserving the shared database. Production requirements contain PyYAML only;
test tooling is in `requirements-dev.txt`.
Before initialization, fill the stable Linear-user and project-ID placeholders
with values Max confirms. Confirm that `LINEAR_API_KEY` is an
existing Max-provisioned key; this repository does not create one. Then run:

```sh
deploy/install.sh
. "$HOME/.config/factory/profile.sh"
orch doctor-runtime
```

Every bot shell sources [`deploy/profile.sh`](deploy/profile.sh).
`doctor-runtime` checks that PyYAML imports, `ORCH_DB` is set, and its SQLite
database exists, opens, and has the runtime tables, and that the factory
wrapper is the `orch` found on `PATH`.

[`deploy/run-poller.sh`](deploy/run-poller.sh) is the long-running poller
launcher: it sources `~/.config/factory/webhook.env` and
`~/.config/factory/orch-poll.env` with `set -a`, sets `PYTHONUNBUFFERED=1`,
and runs `orch poll`; redirect its output to `poller.log`. `orch poll` also
line-buffers its own stdout and stderr, so the log stays current.
[`deploy/orch-poll.service`](deploy/orch-poll.service) is the systemd user
unit alternative; it reads the same two files and sets `PYTHONUNBUFFERED=1`.
Max creates both files mode `0600`. `orch-poll.env` names `ORCH_DB` and the
Linear variable named by `orch.yml`. `webhook.env` holds `FACTORY_WEBHOOK_URL`
and either `FACTORY_WEBHOOK_AUTH` or `FACTORY_WEBHOOK_KEY`;
`deploy/install.sh` installs the names-only template
[`deploy/webhook.env.example`](deploy/webhook.env.example) as
`~/.config/factory/webhook.env.example`. Quote every value, because an
unquoted `Bearer <key>` does not set the variable when the file is sourced.
Values are not documented or committed. Copy or link the unit to
`~/.config/systemd/user/orch-poll.service`, review its checkout path, and then
use `systemctl --user daemon-reload`, `enable`, and `start`. Nothing in this
repository installs, enables, or starts the service, or writes either
environment file.

## Status

```sh
bin/orch status --db "$ORCH_DB"
```

`orch status` prints a read-only summary of queued jobs, occupied box slots,
devices, and paused tracker items. It opens the dispatcher database read-only and does
not claim, release, lease, wake, or update anything. `--machines` defaults to
the checkout's `machines.yml`, read with placeholders accepted; an invalid
file exits 1. Each device row shows the device, its eligible harnesses (`?`
for a placeholder), device-pool slots held over `slot_max`, and `disabled` or
`placeholder` when it will not be scheduled, for example
`pc [claude,codex,cursor] 0/3 slots disabled` and
`mac [none] 0/2 slots disabled placeholder`. A held slot on a
device no longer listed is shown as `not-in-machines.yml`. A running job that
holds a device-pool slot is listed under its device row only, so each job is
counted once. Phase 1 claims are the `Slots — box` lines and hold no
device-pool slot.

## Dashboard

```sh
bin/orch dashboard snapshot --db "$ORCH_DB" --out "$HOME/.local/state/factory/dashboard"
bin/orch dashboard push --db "$ORCH_DB"
```

The box cannot take inbound tailnet connections, so there is no live
server on it. `orch dashboard snapshot` opens the dispatcher database
read-only (a SQLite `mode=ro` URI with `query_only`; a missing file is
refused, never created), reads the last 50 lines of `poller.log`, and
writes `status.json` and a static, self-contained `index.html` to `--out`.
The database path is resolved through symlinks first, and the dashboard
never uses `immutable=1`: SQLite takes its normal shared locks and reads
the WAL, so a read stays consistent while the poller commits or
checkpoints. Like any reader of a WAL database (`sqlite3` or `orch status`
included), SQLite may create `orch.db-wal` and `orch.db-shm` beside the
database; they are SQLite's own files and the next writer removes them. If
the directory is read-only and they don't exist yet, the snapshot fails
with that explanation instead of reading without locks. Both files are
written to temp files in the same directory and fsynced, then renamed into
place; if the second rename fails, `status.json` is put back as it was, so
the pair always comes from one snapshot. The page reloads itself every `dashboard.refresh_seconds`
(15 to 30, default 20; `--refresh` overrides it). Paths: `--db`, else
`$ORCH_DB`; `--log`, else `$ORCH_POLL_LOG`, else `poller.log` beside the
database; `--out`, else `$ORCH_DASHBOARD_DIR`, else `dashboard/` beside the
database; `--machines`, else the checkout's `machines.yml` when present.

The snapshot shows the queue (queued, running, done and failed jobs by
recipient and action), running jobs with the age of their last heartbeat,
queued jobs with their wait, box slots (running harness jobs that hold no
device-pool slot, as in `orch status`; a job placed on a device counts on
that device's row) and, when `machines.yml` (F2-01) is
present, device rows (enabled, placeholder, harnesses, slots held from
`device_slots` when that table exists; absent or invalid files show no
device rows), stuck tickets and overdue jobs (a heartbeat or queue wait
past the job's timeout), paused tickets, every tracked ticket's state,
review round and gate count, poll errors (last hour, last day, newest 20),
`poll_deferrals` when that table exists, the poller's last activity (the
newest of the log's modification time, poll error and deferral; `main`
records no per-cycle row) and the handoff-savings counter.

**Handoff savings.** docs/SPEC.md does not define this counter; the
dashboard brief does, and this is its database form. `scripted` counts
jobs for `claude`, `codex` or `cursor` that `orch wait` claimed and the bot
finished with an `orch:v1` handoff comment (`state = 'done'` with a
`handoff_comment_id`): handoffs routed from an `orch:v1` comment to a bot
by script. `scripted_in_flight` counts such jobs still running.
`bot_to_bot` counts jobs routed to `grok`: each wakes Grok Bot to hand off
by hand. `scripted_share` is `scripted / (scripted + bot_to_bot)`. Bot
messages outside the factory are not in the database, so Grok wakes stand
in for bot-to-bot handoffs.

**Redaction.** Only allowlisted columns are read: never `jobs.detail`,
heads, comment bodies or other free-form job data. The environment is not
read apart from the three `ORCH_*` path variables. Unknown action,
recipient, job state, ticket workflow state or tracker values show as
`other`. Every free-text value (stuck reasons, poll errors, deferrals, log
lines) goes through one redactor, and the finished snapshot is walked once
more before writing. The redactor runs its rules twice: on the text as
written, and on a decoded copy with percent-encoding (repeated, so `%253A`
is seen too) and JSON string escapes (`\/`, `\"`, `\u00XX`) undone.
Whatever the decoded pass finds is cut out of the original text together
with the rest of its whitespace-separated token, so a separator that only
exists once decoded (`password=abc%20rest`, `token=abc%3Brest`, an escaped
quote inside a JSON value) can never leave part of a value visible:
decoding only widens what is hidden. Text still changing after eight
decoding rounds is replaced whole. The output is the original text with
markers in place of secrets, never a decoded copy. The rules remove
private-key blocks (`-----BEGIN ... -----` to the matching
`END`, or to the end of the text), every URL (so webhook URLs and
`user:pass@` URLs go whole), `Authorization`/`Cookie`/API-key headers,
`Bearer` and token-like `token`/`basic` values, GitHub (`ghp_`, `gho_`,
`ghu_`, `ghs_`, `ghr_`, `github_pat_`), Linear (`lin_api_`, `lin_oauth_`)
and other common token shapes, `NAME=value`, `name: value` and quoted
JSON or repr pairs (`{"password":"..."}`, `'api_key': '...'`) whose name
looks like a secret (the name stays, the value goes), email addresses,
and long opaque strings (32 or more token characters with digits, which
includes full SHAs). The log tail is also read as a whole for key blocks
that span lines, with the markers matched in decoded form (so
`-----BEGIN%20PRIVATE%20KEY-----` counts): every line from a `BEGIN` line
to its `END` line is replaced, and when the tail starts inside a block (its `BEGIN` scrolled
out), the lines up to the `END` are too. Lines are capped after
redaction, and a log line over 4096 characters is dropped rather than
cut. `tests/test_dashboard.py` plants a fake webhook URL (plain, percent-
encoded, double-encoded and JSON-escaped), a credentialed URL (plain and
percent-encoded), a Bearer token, four GitHub token shapes, a Linear key
shape, secret assignments (plain, percent-encoded and as quoted JSON keys
with varied quoting and spacing), an email and a multi-line private key in
the environment, the database (poll errors, deferrals, stuck reasons,
workflow states) and the log, and checks that none reach `status.json` or
`index.html`. A second test turns the redactor off and checks that the
same leak check then finds every planted free-text secret.

**Push.** `orch dashboard push` snapshots and then sends `status.json` and
`index.html` every `dashboard.push.interval_seconds` (15 to 30); `--once`
sends one and exits 1 if that failed. A failed snapshot or send logs one
redacted line and the loop continues. Push is off by default: with no
`dashboard.push` section, or `transport: none`, it logs one line and exits
0 without opening the database or writing anything. The one transport is
`ssh`, to the pc-wsl-bots node:

```yaml
dashboard:
  refresh_seconds: 20
  push:
    transport: ssh
    host: pc-wsl-bots.tail68624b.ts.net   # or an alias from ~/.ssh/config
    remote_dir: /path/the/server/serves   # absolute, created by Max
    method: rsync                         # or scp
    interval_seconds: 20
```

`rsync` (the default) runs `rsync --quiet --timeout=30 --chmod=F644 -e "ssh
-o BatchMode=yes -o ConnectTimeout=10" -- status.json index.html
host:remote_dir/`; rsync writes each file to a temp name and renames it,
so readers never see a partial file. `scp` has no rename of its own, so
it uploads each file to a dot-prefixed temp name (`scp -q -B -o
BatchMode=yes -o ConnectTimeout=10 -- status.json
host:remote_dir/.status.json.push-tmp`, then the same for `index.html`)
and, only after both uploads succeed, renames each into place with `ssh -o
BatchMode=yes -o ConnectTimeout=10 -- host mv -f -- temp final`. An
interrupted push leaves the published files whole, and the static server
never serves the temp names. Every command is an argv list (no local
shell) with a 60-second timeout; the remote `mv` sees only the validated
`remote_dir` and fixed file names. The config
holds no secrets: `host` is a bare alias or tailnet name (no `user@`, no
options) and `remote_dir` is a plain absolute path. SSH uses the login Max
provisions in `~/.ssh/config`; the factory never reads, names or passes a
key. `check-config` validates the section.

**Static server (not deployed).**
[`deploy/factory-dashboard-http.service`](deploy/factory-dashboard-http.service)
is an example systemd user unit for the pc-wsl-bots WSL node. It runs
[`deploy/factory-dashboard-http.py`](deploy/factory-dashboard-http.py), a
small standard-library server, instead of `python3 -m http.server` (which
follows symlinks out of its directory, lists directories, and serves the
home directory when `--directory` is empty). It answers GET and HEAD only,
and only for `/`, `/index.html` and `/status.json`: every other path, dot-
file, `..` or encoded slash is a 404, and no directory is ever listed. Each
file is opened, with `O_NOFOLLOW`, relative to a descriptor for the
directory that the server opens once at startup (so swapping the directory
or an ancestor for a symlink later changes nothing), and served only
if it is a regular file with a single link, so neither a symlink nor a hard
link to a file elsewhere is served. It binds only the node's tailnet IPv4
address, never a wildcard address. Before starting (`ExecStartPre`, and
again at startup) it refuses a `TAILNET_IP` outside `100.64.0.0/10`, and a
`DASHBOARD_DIR` that is unset, empty, relative, missing, not a directory,
`/`, `$HOME` or above `$HOME` (checked after resolving symlinks). If
Tailscale is down the bind fails and systemd retries; it never falls back
to another address. The unit is sandboxed: `ProtectSystem=strict`,
`ProtectHome=read-only`, `~/.ssh`, `~/.gnupg`, `~/.config/gh`, `~/.aws` and
`~/.netrc` inaccessible, `PrivateTmp`, `PrivateDevices`, `PrivateUsers`,
`NoNewPrivileges`, IPv4 and Unix sockets only, and the `@system-service`
system call filter. In a user unit that needs unprivileged user namespaces
(WSL2 has them); without them the unit fails to start rather than running
unsandboxed. To install it, Max puts `TAILNET_IP` (from `tailscale ip -4`
on that node), `DASHBOARD_DIR` and `DASHBOARD_PORT` in
`~/.config/factory-dashboard/http.env` (no secrets), copies
`deploy/factory-dashboard-http.py` to `~/.local/lib/factory-dashboard/` and
the unit to `~/.config/systemd/user/`, then runs `systemctl --user
daemon-reload` and `enable --now`. WSL needs systemd enabled. Nothing in
this repository installs, enables or starts it, or pushes anything.

## Check config

```sh
bin/orch check-config
bin/orch check-config --orch orch.yml --machines machines.yml --allow-placeholders
```

The shipped `orch.yml` intentionally contains install placeholders, so this
strict command exits 1 until Max fills in the confirmed values. CI checks that
file with `bin/orch check-config --allow-placeholders`; the flag permits
`REPLACE_WITH_` install values, names each accepted placeholder on stderr,
and still runs every other validation.
`init-db` and runtime commands are always strict. An unknown model name is a
warning on stderr.
With no `--orch`, `check-config` also checks the shipped `machines.yml`; with
`--orch`, it checks a machines file only when `--machines` names one. A
`REPLACE_WITH_` device value follows the same `--allow-placeholders` rule and
the warning says that device will not be scheduled. A disabled device is
always named on stderr and does not fail the check.

## orchestrate-project

Grok Bot's skill is [`grok/orchestrate-project/SKILL.md`](grok/orchestrate-project/SKILL.md). Comment text, the questions gate, and routing go through [`lib/orchestrate.py`](lib/orchestrate.py). `choose_route` calls `lib.usage.route_job(config, marked_urgent=urgent)` and builds a `FitChoice` from the route's builder and reviewer. See [`lib/usage.py`](lib/usage.py).

## pstack pin

[`skills/pstack`](skills/pstack) is pstack 0.15.36 from `cursor/plugins` commit `c742dd61706fbacc1b95f1a20b7b988cf096ce6c`. [`skills/skills.lock`](skills/skills.lock) records that commit and the sha256 of each file. A pin bump changes the vendored tree and regenerates that lock in the same PR.

```sh
bin/orch install-skills
bin/orch install-skills --cloud --project .
bin/orch doctor --print-canary
bin/orch doctor --home "$HOME" --responses DIR
```

`install-skills` symlinks each pinned skill into `~/.claude/skills/<name>/`, `~/.agents/skills/<name>/`, and `~/.cursor/skills/<name>/`. `--cloud` copies those skills into the project's `.claude/skills/`, `.agents/skills/`, and `.cursor/skills/`.

`doctor` prints a canary that asks the harness to name poteto-mode and quote the whole first bold passage of the pinned poteto-mode orchestrate playbook exactly as written. Claude Bot, Codex Bot, and Cursor Bot each start their own harness and write the reply. `doctor` fails if any harness did not load that version. It does not start `claude`, `codex`, or `cursor-agent`.

## Usage reserve

`orchestrate-project` (F1-07) calls `route_job` in [`lib/usage.py`](lib/usage.py) with the loaded `factory.md`. The best fit for a role is that role's harness and model hint; the other harnesses follow in `claude`, `codex`, `cursor` order. The reviewer is a different model family from the builder this call selected.

The last 20% of a verified usage cycle is a reserve. A non-urgent job moves to the next fit that is outside the reserve. A resumed paused step, past-ship work, or a job Max marks urgent may use the reserve. Each owning bot calls `reserve_check` on its own harness before starting, when it has a verified report. No verified report leaves the route on best fit. Routing reads no live usage; tests use [`tests/fixtures/usage/`](tests/fixtures/usage/).

## Weekly eval report

```sh
bin/orch eval-report --input tests/fixtures/eval/week
```

The report reads `orch:v1` comments, `dispatcher.log`, `orch.yml`, and each run's `factory.md` from that directory. `--since` and `--until` (`YYYY-MM-DDTHH:MM:SSZ`, inclusive) select a week. A later weekly run passes the directory that holds that week's logs. The command does not contact GitHub or Linear.

## Model family

`orch check-config` refuses a reviewer unless it is a different model family from the builder. The check fails closed.

- A `claude` harness is Anthropic. A `codex` harness is OpenAI. The harness decides the family. A hint that names the other family is refused.
- A `cursor` harness takes its family only from the model hint. `claude*`, `sonnet*`, `opus*`, and `haiku*` are Anthropic. `gpt*`, `codex*`, and `o<digit>` (`o3`, `o4-mini`) are OpenAI.
- If either side's family cannot be resolved, the check refuses. A cursor side needs one of those hints.
- If both families resolve and they are the same, the check refuses.

## Model

[`model/Orchestrator.tla`](model/Orchestrator.tla) is the section 4 ticket loop. State names are the `orch:state` labels (`waiting-max`, `post-merge`, …). T1 is the `kind=go` handoff. T9 is in the model and is off when `AllowPostMerge = FALSE` (Phase 1).

```sh
bash model/check.sh
```

That runs pstack `/statechart` (TLC, one worker). [`model/chart.mmd`](model/chart.mmd) is the section 4.1 chart, including post-merge. The Phase 1 drawing config does not reach post-merge. [`model/fixtures/omit-t1.cfg`](model/fixtures/omit-t1.cfg) drops T1 and must fail.
