# factory

Grok Bot orchestrates pstack work across Claude Code, Codex and Cursor.

The specification is [docs/SPEC.md](docs/SPEC.md). This repo has the F1-01,
F1-03, F1-04, F1-05 and F1-09 contracts, the F1-06 blocking wait, the F1-02 pstack pin, the F1-07
`orchestrate-project` skill, the F1-08 usage reserve, the F1-13 orchestrator
model, and the F1-14 weekly eval report. Other Phase 1 items are not built.
Phase 2 has the F2-01 device pool and F2-02 remote start; the PC and Mac
still ship `enabled: false` until Max turns them on.

## Contracts

- [`orch.yml`](orch.yml) lists the projects to poll and the dispatcher settings ([docs/SPEC.md](docs/SPEC.md) section 7). [`config/orch.example.yml`](config/orch.example.yml) is the minimal file.
- `factory.md` is the per-repo policy ([docs/SPEC.md](docs/SPEC.md) section 6). The generic example is [`docs/factory.example.md`](docs/factory.example.md). Aut0, Cleffy and Tracy examples live under [`examples/`](examples/) and are not those repos.
- `orch:v1` handoff comments ([docs/SPEC.md](docs/SPEC.md) section 5) are parsed and written by [`lib/handoff.py`](lib/handoff.py).
- [`lib/dispatcher.py`](lib/dispatcher.py) applies T1, T1P and T2 through T6 (F1-05 is
  the T3 review round, T4 ship and T5 questions loop) to
  PRs discovered through `orch:state/` GitHub labels (or, on a
  `ready_starts_review` project, an open PR with a `kind=ready` comment) or
  Linear tickets found by the handoff search. It stores a ticket after the first `brief` or `go` from the trusted
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
`workdir_root`, `ssh_alias` and `enabled` (default `true`). `harnesses` maps each eligible
harness to the bot OS user whose login Max provisioned there, and that user
must be the harness's own bot (`claude: claude-bot`, `codex: codex-bot`,
`cursor: cursor-bot`). The one `local: true` device is the box that runs
`orch`; it omits `tailscale_name`, `workdir_root` and `ssh_alias`. Every other device names
a `workdir_root`, an absolute normalized path that may not be under `/mnt/`,
so jobs on a WSL node stay off the Windows drives. A whole path component may
be `{bot}`, which resolves to the harness's bot user
(`Device.workdir_for(harness)`). Every remote device also names an
`ssh_alias` (F2-02): the Host alias in the box user's own SSH client config,
which must contain `{bot}` so each bot logs in through its own alias
(`Device.ssh_alias_for(harness)`; the PC's `pc-wsl-{bot}` gives
`pc-wsl-claude-bot`, `pc-wsl-codex-bot` and `pc-wsl-cursor-bot`). It is a bare
alias: no `user@`, no leading `-`, no space or shell character, never a key
path; the host name and key stay in that SSH config. A Tailscale name is a device name, never an
IP address. Two devices may not share a `tailscale_name` (compared
case-insensitively): slots are counted per device name, so a second entry
would give one host a second `slot_max`. Release a device's held slots
before renaming it. Unknown fields are refused, and `workdir_root` may not
contain NUL or another control character.

The shipped file lists the box (all three harnesses, 3 slots, because the box
runs at most 3 heavy local agent sessions at once; slots are generic, so they
are not one per bot and 3 Claude jobs can take all 3), the PC's WSL Ubuntu node
and the Mac. The PC and Mac ship
`enabled: false` and stay disabled until Max turns them on; the Mac also
has no bot users and no SSH yet. The PC's `workdir_root` is
`/home/{bot}/factory-work` (for example `/home/codex-bot/factory-work`), off
`/mnt/c`, and its `ssh_alias` is `pc-wsl-{bot}`, the box's existing aliases
`pc-wsl-claude-bot`, `pc-wsl-codex-bot` and `pc-wsl-cursor-bot`. The Mac's
`workdir_root` and `ssh_alias` are still `REPLACE_WITH_` placeholders. Nothing
assumes a device is reachable: the poller checks it (below).

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
worker is confirmed gone (F2-02, below). `orch wait` claims through the pool.
The pool itself starts nothing and opens no SSH connection.

### Remote start (F2-02)

`orch job run` ([`lib/placed_run.py`](lib/placed_run.py)) runs a job where the
pool placed it. It refuses (exit 2, nothing started, the slot kept) a job that
holds no slot (released, or claimed before F2-02), whose slot is for another
bot user, whose device `machines.yml` no longer lists, or that another live
`orch job run` already runs; it records itself on the slot as the runner. When
deploying F2-02, a job already running with no slot is not migrated: its next
`orch job run` exits 2, so fail it with `orch job done --failed` (T6 retries it
through the pool), and `orch wait` and `orch job run` need `machines.yml`. On the
box the command runs as before, and its pid is recorded as the worker. On a
remote device the command runs there over that bot's alias
([`lib/remote.py`](lib/remote.py)):

```
ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=60 \
    -o ServerAliveCountMax=5 -l <bot user> -- <ssh_alias> <start script>
```

`-l` always names the job's bot user, so a mistyped alias cannot log in as
another bot; no key, `-i` or config path is ever passed. Each remote start gets
a fresh random worker tag, recorded on the slot before ssh starts, and the
start script runs under `exec env ORCH_WORKER_TAG=<tag>`, so the command and
every process it starts carry the tag; the tag, not a process number, names
the job's worker. The script refuses (exit 125, nothing run) a device without
Linux `/proc` and a `grep -z`, makes `<workdir_root>/jobs/<job-id>`, records
the worker's process group and boot id (sshd starts the command in its own
session) in `.orch-worker.pid` there, and execs the command with `ORCH_JOB_ID`,
`ORCH_JOB_BOT`, `ORCH_JOB_DEVICE` and `ORCH_JOB_WORKDIR` (not `ORCH_DB`: the
database stays on the box, and the bot runs `orch job done` there); ssh exits
with the command's status. The record is never removed. The heartbeat stays
local and beats while ssh runs; SIGTERM and SIGHUP go to ssh.
`--worktree` is box-only and refused for a remote job (exit 2): the harness
checks the repo out in `$ORCH_JOB_WORKDIR` itself.

When the command ends, done, failed or killed, the slot is released only once
the worker is confirmed gone. On the box the command has exited (and no worker
an earlier, dead runner of the job left behind still runs); the command is held
at a gate until its pid is recorded on the slot, so a runner that dies or
cannot record it never leaves an unrecorded command running. On a remote
device one SSH check looks for the tag: any process of the bot user carrying it
(a child the command left behind included) means running; otherwise a process
still in the recorded group from this boot means it cannot tell; otherwise the
worker is gone. A worker still running is sent SIGTERM (every tagged process
and each group holding one; a stored number alone is never signalled) and
checked again for up to 10 seconds. An unreachable device, a check that cannot
tell, or a worker that will not stop keeps the slot and is logged. A new
`orch job run` for a job whose runner died is refused (exit 2, nothing started)
while that runner's worker is not confirmed gone, so its identity is never
overwritten.

The poller finishes the job ([`lib/device_monitor.py`](lib/device_monitor.py),
run each poll before T6). For a slot whose job is no longer running and whose
runner is gone, it releases a box slot once the recorded command pid is gone,
releases a remote slot no worker was started for, and otherwise asks the device:
gone releases the slot, a worker still running there is sent SIGTERM (no runner
supervises it) and checked on a later poll, unreachable keeps the slot. Every
remote device still holding a slot gets at least one SSH contact each poll: the
worker checks count, and a device that needed none (its jobs still run, or their
runners still live) gets a plain round trip. The first failed check
after a good one starts that device's offline clock, a good one stops it, and a
device holding no slot is forgotten. Once a device has failed every check for
60 minutes, each job still running on it is failed with its failure already
handled (so T6 does not retry it), its ticket is marked `stuck` with reason
`device-offline`, and Grok Bot is woken once through the stuck flag. The job is
never `done`, and its slot stays held until the device confirms the worker gone.
An unreachable device counts against its offline clock; an error raised while
checking (the database, say) is recorded as a `devices` poll error and never
stops the poll.
Tests use a fake `ssh` ([`tests/fake_ssh.py`](tests/fake_ssh.py)); a
suite-wide guard puts a refusing `ssh` first on `PATH`, so no test reaches a
host.

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
projects use GraphQL search with `LINEAR_HANDOFF_QUERY`. Each poll also runs
the F2-02 device checks (slot release and the 60-minute offline check, see
"Remote start") from `--machines`, default the checkout's `machines.yml`;
without that file no device is checked, and an invalid one is refused.

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
only when a fresh read shows the issue still before review (Backlog, Triage,
unstarted, or started and before Ready for Review in the team's order), per
Max's standing rule that the factory may move approved Aut0 tickets to Ready
for Review. An issue already in Ready for Review, or moved on by a human (Done,
Canceled, or a started state after Ready for Review), is left where it is; an
unknown state fails the handoff before any write. On a team with no Ready for
Review state the PR is still marked ready and the handoff completes; the
`done` wake's detail carries `note: "linear-not-moved: team has no Ready for
Review state"` so the project bot can move the issue by hand. No other
dispatcher state writes Linear, and `ship: handoff` never merges.
`projects[].ready_starts_review` defaults off. When it is on, a trusted
`ready` from `factory.md`'s builder (`from=<builder>-bot role=builder`, by a
`trusted_logins` author) both starts tracking the PR and queues the first
review, even though no Grok `brief` or `go` was posted and no build job
exists, for a project whose PRs are built outside the queue. Discovery
tracks the PR on that `ready` only when its head names the PR head; T2 then
queues the round-1 review in the same poll once CI on the head is green.
Such a project's PRs need no `orch:state/` label to be discovered (issue
#59): nothing writes that label before tracking starts, so an open PR with a
parseable `kind=ready` comment is a candidate and the poller writes the
label once it tracks the PR. Every other rule is unchanged: projects without
the flag still need the label (and pay no comment read for unlabelled PRs),
a Grok `brief` or `go` still needs it, an untrusted or wrong-role `ready`
starts nothing, a `ready` comment is handled once and a head is reviewed
once per round, and later
fixes come back through the queue. The factory repo itself
(`maxharris1/factory`) is listed this way, GitHub-only with `ship: handoff`
in its `factory.md`: review and fix jobs go through `orch wait`, and an
APPROVE with green CI wakes Grok Bot (Factory) with `done` to merge instead
of the poller merging.

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
With `linear_webhook` in `orch.yml`, Linear discovery is driven by dirty
issues instead of a search every cycle (next section); without it, every
cycle is exactly as described above.

## Linear webhook

```sh
bin/orch linear-webhook serve --db "$ORCH_DB"
bin/orch linear-webhook serve --db "$ORCH_DB" --bind 127.0.0.1:8787
```

Optional, off unless `orch.yml` has a `linear_webhook` section (issue #55;
Max, Nov 12, 10:02 AM ET: "Go straight to a Linear webhook through Tailscale
Funnel"):

```yaml
linear_webhook:
  bind: 127.0.0.1:8787            # IP literal and port; --bind overrides
  path: /linear-webhook           # default
  secret_file: ~/.config/factory/linear-webhook.secret
  reconcile_seconds: 900          # 60 to 3600, default 900
```

`orch linear-webhook serve` is a small stdlib HTTP server. It opens the
existing dispatcher database (never creates it; run `init-db` first),
creates its own `linear_dirty`, `linear_webhook_health` and
`linear_project_issues` tables, and for
each request:

- serves POST on `path` only (404 elsewhere, 405 for other methods);
- when it can see the client address (the TCP peer, or the last
  `X-Forwarded-For` hop when the peer is loopback, as behind Tailscale
  Funnel) requires one of Linear's published source IPs, else 403;
- works on at most 8 requests at once (a further connection is closed
  unread) and drops a request that has not arrived in full within 10
  seconds in total, however slowly it trickles in;
- requires a Content-Length of at most 1 MiB, checked before the body is
  read (411, 413);
- verifies `Linear-Signature`, the hex HMAC-SHA256 of the raw body under the
  signing secret, with `hmac.compare_digest` (401 when missing or wrong);
- refuses a `webhookTimestamp` (inside the signed body) more than 60 seconds
  from now (401);
- for an Issue in a configured `linear_project_id` (or just moved out of
  one), or a Comment on a tracked issue, or a Comment carrying an `orch:v1`
  marker on an issue a signed Issue event last placed in a configured
  project, upserts one `linear_dirty(issue_id, identifier, event,
  received_at)` row and returns 200. Comment payloads do not name the
  issue's project and a marker alone proves nothing, so the listener keeps
  `linear_project_issues` from Issue events; a handoff on an issue it has
  not seen is found by the reconcile search. The poller still checks the
  project after its direct read. Every other signed event gets 200
  and is ignored. A database error returns 500 so Linear retries.

It never runs handoff logic and never calls Linear. Log lines are
`linear-webhook: accepted Issue.update AUT-123`-style: outcome, event type
and identifier, never bodies (PHI), headers or signatures.

The signing secret is read once, at startup, from `secret_file`. A file
readable by group or others, empty, or unreadable is refused with an error
that names the path only. The value is never logged, printed, stored or put
in an error. `check-config` validates the keys and never reads the file.
Max creates the webhook in Linear (Issue and Comment resource types, scoped
to the Aut0 team) with the Funnel URL, and saves its signing secret to
`~/.config/factory/linear-webhook.secret` with `chmod 600`. Tailscale Network
owns the Funnel that exposes only the listener path. This repository
installs no service for the listener and changes no network configuration.

With the webhook configured, `orch poll` reads the dirty rows and the
tickets with queued or running jobs each cycle. Dirty untracked issues are
read directly, oldest first, under the same Linear budget; one outside the
configured projects, completed, canceled or deleted is dropped quietly.
A tracked ticket is reread in full only when it is dirty, has an active job,
or its last read is `reconcile_seconds` old; otherwise its last read is
reused and the ticket is still rebuilt, so the GitHub PR head, CI and PR
comments stay current. The full brief/go search runs at startup and then
every `reconcile_seconds`, and stays due until it completes without a budget
or error cutoff, so a missed webhook costs at most one reconcile interval.
A dirty row is deleted only after its issue was read in that cycle and only
if no newer event arrived meanwhile; an unread row (budget deferral, error)
waits for the next cycle. A tracked ticket the dispatcher no longer polls
(merged, handed off, done or abandoned) is not read with or without a
webhook, so its row is deleted unread. A read that finds the issue gone
also drops its reused read, so the ticket is never rebuilt from it. If the dirty queue cannot be read, the cycle
records a `linear-webhook` poll error and runs as a full cycle. With no
`linear_webhook`, the poller never reads the dirty queue or the job list
for this, and the dispatcher creates no webhook tables.

## Wait

```sh
bin/orch wait claude-bot --db "$ORCH_DB"
bin/orch job run claude-bot <job-id> --worktree -- claude ...
bin/orch job heartbeat claude-bot <job-id>
bin/orch job done claude-bot <job-id> --handoff <comment-id>
bin/orch job done claude-bot <job-id> --failed
bin/orch gc [--older-than HOURS] [--dry-run]
```

[`lib/wait.py`](lib/wait.py) blocks on the queue above. `orch wait` accepts
only `claude-bot`, `codex-bot`, and `cursor-bot` (recipients `claude`,
`codex`, `cursor`). It checks for a job once a second, claims the oldest
through the device pool (`--machines`, default the checkout's `machines.yml`,
placeholders accepted and never scheduled), and prints one JSON line with the
job's `device` and `remote` and, on a remote device, its `workdir`. With no free
slot on an eligible device the job stays queued and the waiter keeps waiting. While the poller holds a write, a claim that sees
`database is locked` (after a 5-second SQLite busy timeout) keeps waiting with
backoff capped at 5 seconds; `job heartbeat` and `job done` retry the same way
up to 8 attempts. Other database errors still fail at once.
If that line cannot be printed, the job is finished as
failed so T6 retries it at once, and its slot is released (no bot got it). `orch` never chooses or starts a harness on
its own: the bot that ran the shell call starts its own, through `orch job run`. `job heartbeat` and `job done` refuse a job that
belongs to another bot. The database is `--db`, else `$ORCH_DB`; a missing file
is refused. Each bot's "orch jobs" paragraph is under [`bots/`](bots/).
Running timeouts use last activity.

### Job heartbeat and worktrees

[`lib/job_run.py`](lib/job_run.py) owns the job heartbeat (issue #49): the bot
starts its harness as `orch job run <bot> <job-id> [--worktree] -- <command>`.
It sends one heartbeat first, refusing (exit 1, command not run) a job that is
not running or (exit 2) not this bot's, then one every 15 seconds, or every
third of the job's `timeout_seconds` when that is shorter (an `--interval`
override is capped at 30 seconds, half the smallest `job_states.stale_minutes`,
and at a third of the timeout, so a live job never shows as stale; see "Job
states"). The heartbeat stops when
the command exits, when a heartbeat finds the job no longer running (for
example after the command's own `orch job done`), or when `orch job run` dies;
locked or failed writes are logged and retried at the next beat. SIGTERM and
SIGHUP are forwarded to the command, a Ctrl-C (SIGINT to the process group)
reaches the command directly, and the exit status is the command's; a signal
that arrives before the command starts means it never runs.
`orch job run` never finishes the job: `orch job done` stays the bot's call.
The command gets `ORCH_JOB_ID`, `ORCH_JOB_BOT` and `ORCH_DB`. Bots do not
hand-roll heartbeat loops; `orch job heartbeat` is a single manual beat.

With `--worktree` ([`lib/worktrees.py`](lib/worktrees.py)) the command runs in a
detached git worktree at `<root>/jobs/<job-id>` (exported as
`ORCH_JOB_WORKTREE`), made from one shared bare clone per repo at
`<root>/clones/<owner>/<name>.git`, at the job's `head` (fetched by SHA when it
is on no branch) or the remote default branch. The root is `--work-root`, else
`$ORCH_WORK_ROOT`, else `~/factory-work`; the remote is `--git-url-template`,
else `$ORCH_GIT_URL_TEMPLATE`, else `https://github.com/{repo}.git`. When the
command exits, done or failed, the worktree is removed and the clone pruned,
unless it has uncommitted changes (untracked files included; ignored build
output is not) or commits made after the commit the job started at that are on
no remote-tracking ref, in which case it is kept and the reason logged. (The
start commit itself, such as a PR head fetched by SHA, does not count.) `orch gc`
applies the same rule to worktrees older than `--older-than` hours (default
24) whose owning `orch job run` is gone, reports what it kept and why, and
prunes each clone; `--dry-run` only reports. Both decide under the clone's
lock, and gc removes only a worktree of the clone derived from the record's
repo. A URL template carrying credentials (`https://user:token@...`) is
refused; git authenticates through its credential helper.

### Job states

A running job is in one of four states, derived in one place,
[`lib/job_state.py`](lib/job_state.py), which the poller, the dashboard and
`orch status` all use (issue #75):

- **claimed**: it holds a device slot, no `orch job run` has recorded itself
  on the slot (no `runner_pid`), and nothing has heartbeated since the claim.
  The bot claimed it and never started it.
- **running**: its heartbeat is fresh.
- **stale**: no heartbeat for more than `job_states.stale_minutes` (default 2).
- **dead**: its recorded runner is gone, and so is its worker: the command's
  pid on the box, or, for a remote job, the device answers that no process
  carries the start's worker tag (`check_worker`). A worker that may still
  run is stale, not dead.

The poller requeues a job that stays claimed for more than
`job_states.release_claimed_minutes` (default 5): the same job row goes back
to `queued`, so its review round and T6 attempt are reused, not charged; its
slot is freed; and a `released-claimed` row goes into `job_events` and one
line into the poller log. Its queue-wait clock restarts at the release (it
keeps its place in line), so the job timeout never fails it for the time it
waited before the claim; the dashboard's queued age counts from the release
too. A failed Grok delivery, the other way a job goes back to `queued`, keeps
its original clock: each poll is a real delivery attempt, and the timeout is
what bounds one that keeps failing. The write is guarded, so a runner that starts or
heartbeats in between keeps the job; a late `orch job run` finds the job
queued and starts nothing. A dead job takes the existing failure path: it is
failed (a `dead` row in `job_events`) and T6 retries it once, then marks it
stuck. A stale job is only shown; the job timeout still handles it. A job
that heartbeated after its claim with no runner recorded (`orch job
heartbeat` by hand) is never `claimed`, so it is never requeued.

```yaml
job_states:                 # optional; whole minutes, defaults shown
  stale_minutes: 2
  release_claimed_minutes: 5
```

## Install after merge

Do not run these steps until Max gives the go. `deploy/install.sh` creates a
venv, installs `requirements.txt` (including PyYAML), writes an `orch` wrapper
under `$HOME/.local/bin`, initializes one shared database, and writes
`$HOME/.config/factory/profile.sh`. Without `--autostart` it does not install
or start the service; with `--autostart` it also runs `deploy/poller-service.sh
install-autostart`, which turns on autostart but does not start the poller.
Stop `orch-poll` (or `deploy/poller-service.sh stop`) before re-running
`deploy/install.sh`; both install paths replace the venv.
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
repository installs, enables, or starts the systemd unit, or writes either
environment file.

### Poller service without systemd

`orch poll` holds the database's single-poller lock (`orch.db.lock`); while it
holds it, it writes its own pid to `--pidfile`, else `$ORCH_POLL_PIDFILE`,
else `poller.pid` beside the database, and removes that file on exit,
including SIGTERM (exit 143). A second poller is refused and does not touch
the pidfile; a stale one from a killed poller is replaced. Do not edit the
pidfile by hand.

On a host with no systemd user session (the box), use
[`deploy/poller-service.sh`](deploy/poller-service.sh):
`start` (no-op when running), `stop` (SIGTERM, then hold it stopped),
`restart`, `status` (exit 3 when not running) and `ensure` (start unless
running or held stopped). It runs `deploy/run-poller.sh --pidfile
~/.local/state/factory/poller.pid` under `nohup`, appending to `poller.log`
beside it, and treats a pidfile whose process is gone or is not `orch poll`
(argv `orch poll ...`, read from `/proc`, else `ps`) as not running. `start`,
`stop`, `restart` and `ensure` serialize on `poller-service.lock` (where
`flock` exists), and `ensure` never clears a stop.
`deploy/install.sh --autostart` (or `poller-service.sh install-autostart`)
turns on autostart: it writes
`~/.config/factory/poller-autostart`, which makes the installed `profile.sh`
run `ensure` in the background whenever a bot shell starts, and, when
`crontab` exists, adds an `@reboot` line and a 5-minute `ensure` watchdog
tagged `# factory-orch-poll` (replaced, never duplicated).
`remove-autostart` undoes both. If the crontab cannot be read (anything but
"no crontab for <user>"), both change nothing and exit 1. `ORCH_CRONTAB` names
the crontab command (default `crontab` on `PATH`); the caller's `PATH` keeps
precedence over the standard directories the script appends. `deploy/install.sh` without `--autostart`
changes neither. Run `poller-service.sh stop` before re-running
`deploy/install.sh`, then `start`. `ensure` output goes to
`poller-service.log`.

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
counted once. A running job shows its state (`claimed`, `running`, `stale` or
`dead`, see "Job states") instead of `running`; `--orch` (default the
checkout's `orch.yml` when present) supplies `job_states.stale_minutes`.
`orch status` never checks a remote worker over SSH, so a remote job whose
runner died shows as `stale` until the poller confirms it dead. A running job that holds no slot (claimed before F2-02, or
whose worker already ended) is a `Slots — box` line.
Once a Linear webhook listener or a webhook-mode poller has run on the
database (the poller records `linear_webhook_polling`), a `Linear webhook`
section shows the last delivery (or `never` and the listener's age, or
`never (listener has not started)`) and the
dirty-queue depth, plus a warning after more than an hour with no delivery
while any tracked Linear ticket is not merged, handed off, done or abandoned.

## Dashboard

```sh
bin/orch dashboard snapshot --db "$ORCH_DB" --out "$HOME/.local/state/factory/dashboard"
bin/orch dashboard push --db "$ORCH_DB"
```

The box cannot take inbound tailnet connections, so there is no live
server on it. `orch dashboard snapshot` opens the dispatcher database
read-only (a SQLite `mode=ro` URI with `query_only`; a missing file is
refused, never created), reads the last 50 lines of `poller.log`, and
writes `status.json` and four self-contained pages (`index.html`,
`job.html`, `activity.html`, `efficiency.html`) to `--out`.
The database path is resolved through symlinks first, and the dashboard
never uses `immutable=1`: SQLite takes its normal shared locks and reads
the WAL, so a read stays consistent while the poller commits or
checkpoints. Like any reader of a WAL database (`sqlite3` or `orch status`
included), SQLite may create `orch.db-wal` and `orch.db-shm` beside the
database; they are SQLite's own files and the next writer removes them. If
the directory is read-only and they don't exist yet, the snapshot fails
with that explanation instead of reading without locks. Every file is
written to a temp file in the same directory and fsynced, then renamed into
place, `status.json` first; if a later rename fails, every file already
renamed is put back as it was (or removed if it had no earlier version), so
the set always comes from one snapshot.

**Pages.** Every page shares one bar: links to the pages, the link state
(`LIVE`, `STALE SNAPSHOT` after 3 minutes, `LINK LOST` when fetches fail)
and a **needs attention** badge with a count that links to the Activity
page (stuck tickets, overdue, stale or dead jobs, then paused tickets not
already listed as stuck, each once).

- **Main** (`index.html`): factory status on top (running, queued, done,
  failed, stuck or overdue), then the lanes and slots as the centerpiece:
  one lane per device from `machines.yml` (box, pc, mac; disabled devices
  dimmed and marked, placeholders flagged) with a slot per `slot_max`, and
  a lane for running Grok wakes. Each busy slot shows its job: the ticket
  (a GitHub ticket is shown as a PR, a Linear ticket by its id), bot, step
  (the job's action), its state (`claimed, not started`, `running`,
  `stale heartbeat` or `runner dead`, see "Job states"), the job's review
  round from its own detail (`round 1/1`; `—` for a job with no round),
  elapsed time and heartbeat age against its timeout. A claimed job shows
  how long ago it was claimed and `not started` instead of a timer and
  heartbeat bar. Stale and dead jobs count in stuck / overdue. A lane counts `max(held, running)` slots, so a slot still
  held in `device_slots` is never shown as idle (#63 FU-1). The queue sits
  in a side rail beside the lanes with wait times. A slot or queue item
  opens its job page.
- **Job** (`job.html`, `job.html#job-<id>` for one job): every running or
  queued job with a flow diagram of the ticket statechart, from the ticket
  (start) to shipped: build, review, the revise loop (`fixing`), ready for
  review (`handed-off`) and the ship states. The diagram is generated from
  [`model/chart.mmd`](model/chart.mmd), the chart the statechart check
  draws from `model/Orchestrator.tla` (CI fails if they differ), so a
  change to the statechart changes the diagram; only the layout and a few
  plain-word glosses are the dashboard's. The path the job's ticket took
  and its current state are highlighted, with its round and time in that
  state. The path is the start state, then the state each of the ticket's
  jobs worked in (`build` is `building`, `review` is `review`, `fix` is
  `fixing`, ...), then the ticket's current workflow state; where two of
  those have no direct transition, a bridge of at most two steps through
  the chart is filled in (intake to building goes through questions, which
  has no job of its own), and longer gaps are left as a jump with no edge.
  The database records no transition times, so time in state counts from
  when the first job of the current run of that state was queued.
- **Activity** (`activity.html`): needs attention, recent handoffs
  (scripted, bot-to-bot, failed), recent verdicts (handled `orch:v1`
  comments by outcome), every tracked ticket and the redacted log tail.
- **Efficiency** (`efficiency.html`): poll health and Linear-budget health,
  and handoff savings with a one-line definition of scripted share.

Each page is rendered in full on the box, so it reads without script; it
then reloads every `dashboard.refresh_seconds` (15 to 30, default 20;
`--refresh` overrides it), and on the job page a slot link scrolls to its
job, which `:target` marks. With script, each page fetches the relative
`status.json` every 1.5 seconds (`cache: 'no-store'`) and rebuilds its
panels in place, with no reload; ages tick every second between fetches,
rows that appear since the last snapshot flash briefly, and the job page
copies the server-drawn diagram and moves the highlight. If `status.json`
cannot be fetched at all, the server-rendered page stays and reloads as
without script. Everything is inline: no external asset, font or CDN, so it
works offline on the tailnet. The look follows Omarchy (omarchy.org): the
Tokyo Night palette, square corners, 1px elevation rings, Geist for text
and JetBrains Mono for numbers and labels. Both fonts are vendored under
`lib/fonts` (SIL OFL 1.1, licenses alongside) and travel inside the page
script as bytes, registered with `FontFace`, so nothing is fetched and the
static server's CSP is unchanged; without script the system fonts are used.
Values are only ever set as text
(`createElement`/`textContent`), never parsed as HTML. Animations (brand
caret, link pulse, heartbeat shimmer, new-row flash) stop under
`prefers-reduced-motion: reduce`, and every text colour meets WCAG AA
(4.5:1) on every panel background (`tests/test_dashboard_mission_control.py`
checks the palette). Paths: `--db`, else
`$ORCH_DB`; `--log`, else `$ORCH_POLL_LOG`, else `poller.log` beside the
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
`poll_deferrals` when that table exists, Linear webhook health
(`poller.linear_webhook`: last delivery, dirty-queue depth and the one-hour
silence warning, as in `orch status`) when a listener or webhook-mode
poller has run on the
database, the poller's last activity (the
newest of the log's modification time, poll error and deferral; `main`
records no per-cycle row) and the handoff-savings counter.

Fields the live page adds, all from allowlisted columns or enumerations:
each `running` job's `device` (its `device_slots` placement, else `box`;
`null` for a Grok wake), `elapsed_seconds` and `elapsed_from` (`placed`
from `device_slots.placed_at`, else `queued` from `created_at`, because
`jobs` records no claim time); `handoff_savings.recent`, the newest 12
finished jobs with `kind` `scripted`, `bot_to_bot`, `failed` or `done` (the
handoff comment id itself is never copied); `poller.verdicts`, the newest
12 handled `orch:v1` comments (`handled_comments`, newest by insertion) as
`outcome` (a dispatcher outcome such as `queued-review-fix`, anything else
`other`) and `kind` (`routed`, `shipped`, `stale`, `rejected`, `stuck`,
`blocked`, `waiting`, `other`), `null` without that table;
`poller.health`, `ok` while the last poll cycle ended at most 150 s ago,
`late` to 300 s, then `stale`, and `unknown` when the snapshot was not
told when the last cycle ended (only live pushes pass `--last-cycle`;
`main` records no per-cycle row); and `poller.linear_budget`, `rate_limited`
(Linear 429 or `RATELIMITED` errors in the last hour), `exhausted` (`Linear
poll budget is exhausted` errors), `deferring` (`Linear poll budget`
deferrals) or `ok`, with those hourly counts and the poller's Linear share
of the hourly budget (1250 requests, 1,500,000 complexity).

For the layout: each running and queued job's `tracker` (`github`,
`linear` or `null`), `round` (its ticket's review round, `null` when the
ticket is not tracked) and `flow` (`state`, the ticket's workflow state on
the chart; `path`; `state_age_seconds`); `attention` (`count` and `items`,
each with `kind` `stuck`, `overdue` or `paused`); `statechart` (the parsed
chart: `source` is the file name only, `start`, `states` with their colour
`group`, `edges` with their labels, `legend`; `null` when the chart cannot
be read, and the job page then says the statechart is unavailable); and
`handoff_savings.scripted_share_definition`.

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

**Redaction.** Only allowlisted columns are read: never heads, comment
bodies or other free-form job data. `jobs.detail` is read only for its
`round` field, which is kept only when it is digits/digits (`2/3`); nothing
else of it reaches the snapshot. The environment is not
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
`index.html`; `tests/test_dashboard_layout.py` checks every page. A second test turns the redactor off and checks that the
same leak check then finds every planted free-text secret.

**Push.** Push is off by default: with no `dashboard.push` section, or
`transport: none`, nothing is pushed, `orch poll` behaves exactly as
before, and `orch dashboard push` logs one line and exits 0 without
opening the database or writing anything. The one transport is `ssh`, to
the pc-wsl-bots node as cursor-bot through the box's `pc-wsl-cursor-bot`
SSH alias:

```yaml
dashboard:
  refresh_seconds: 20
  push:
    transport: ssh
    host: pc-wsl-cursor-bot              # an SSH alias or tailnet name
    ssh_config: /home/box/.ssh/config    # optional: passed as ssh/scp -F
    remote_dir: factory-dashboard        # default; relative to the remote home
    method: scp                          # or rsync (the default)
    live: true                           # optional: push status.json on every orch.db change
    # every_minutes: 5                   # omitted: push after every poll cycle
    interval_seconds: 20                 # only for the `orch dashboard push` loop
```

That is the confirmed target: the files land in
`/home/cursor-bot/factory-dashboard` on pc-wsl-bots (the same upload by hand
is `scp -F /home/box/.ssh/config status.json index.html activity.html
efficiency.html job.html pc-wsl-cursor-bot:factory-dashboard/`), and the static server below serves
them tailnet-only at <http://pc-wsl-bots.tail68624b.ts.net:8090/>.
`config/orch.example.yml` carries this section; the shipped `orch.yml` has
it commented out, so nothing pushes until Max turns it on.

*When it runs.* `orch poll`, which
[`deploy/poller-service.sh`](deploy/poller-service.sh) runs, pushes after
every poll cycle (60 seconds), or at most every `every_minutes` (1 to 1440)
when that is set. After a cycle it starts `orch dashboard push --once --db
<db> [--orch <orch.yml>]` as a separate process and does not wait for it;
if the previous push is still running it skips that cycle and logs so. The
child gets only `PATH`, `HOME`, `USER`, `LOGNAME`, locale and `TZ`,
`TMPDIR`, `SSH_AUTH_SOCK` and the three `ORCH_*` path variables: none of
`webhook.env`, no GitHub or Linear token. A failed snapshot or send is one
redacted line in `poller.log`; it never reaches the poll loop, and a hook
error there is logged by exception class only. A stop (SIGTERM) does not
start a push. `orch dashboard push` without `--once` is still a standalone
loop every `interval_seconds` (15 to 30); `--once` exits 1 if the push
failed.

*Real time (`live: true`, ssh only, default off).* `orch poll` also starts a
watcher thread that stats `orch.db` and `orch.db-wal` every 0.25 seconds
(no SQLite connection). Any committed change by any process (`orch wait`
claiming a job, a heartbeat, a job finishing, the poller recording a
verdict) and the end of every poll cycle start `orch dashboard push --once
--status-only --last-cycle <time>`: a fresh snapshot, of which only
`status.json` is sent, through the same temp-name upload and one `ssh ... mv`
rename, so a reader never sees a half-written file. Pushes start at most
once a second and never while the previous one is still running; a change
that arrives meanwhile is pushed as soon as both allow, so the last change
is never dropped. An empty `-wal` counts as none, so the push child's own
read-only snapshot never looks like a change. The full set (the four pages
too) still goes out on the first cycle and then after every cycle, or every
`every_minutes`. A failed start is one redacted line at most once a
minute. `--once` polls start no watcher, and a stop stops it before exit.
The page needs the static server to allow `connect-src 'self'`
([`deploy/factory-dashboard-http.py`](deploy/factory-dashboard-http.py)
sends it); with an older copy of that server the browser blocks the poll
and the page falls back to reloading.

*How it publishes.* Each file is first uploaded to a dot-prefixed temp name
in the served directory (`scp -q -B [-F ssh_config] -o BatchMode=yes -o
ConnectTimeout=10 -- status.json
pc-wsl-cursor-bot:factory-dashboard/.status.json.push-tmp`, then the same
for each page; with `rsync`, `rsync --quiet --timeout=30 --chmod=F644 -e
"ssh [-F ssh_config] -o BatchMode=yes -o ConnectTimeout=10" --` to the same
temp names). Only after every upload succeeds does one `ssh [-F ssh_config]
-o BatchMode=yes -o ConnectTimeout=10 -- pc-wsl-cursor-bot 'cd --
factory-dashboard && mv -f -- .status.json.push-tmp status.json && mv -f --
.index.html.push-tmp index.html && ...'` rename them all over the live
names. An interrupted upload leaves the published set as it was; the
renames run back to back on the remote host with no network round-trip
between them, so a reader never sees a half-written file and the set is
mismatched for at most the instants between local `rename(2)` calls. The static server
never serves the temp names. Every command is an argv list (no local shell)
with a 60-second timeout; the remote shell sees only the validated
`remote_dir` and fixed file names. The config holds no secrets: `host` is a
bare alias or tailnet name (no `user@`, no options); `remote_dir` is a plain
path, absolute or relative to the remote login's home, with no leading `-`,
`~`, `.` or `..` part; `ssh_config` is a plain absolute path to an
ssh_config file, and a name that looks like a key (`id_*`, `*.pem`,
`*.key`, `*.pub`, `*.ppk`) is refused. SSH uses the login Max provisions;
the factory never reads, names or passes a key, and never reads the
ssh_config file itself. `check-config` validates the section. Tests use a
fake transport and a fake spawn; nothing in this repository runs ssh, scp
or rsync against a real host.

**Static server (not deployed).**
[`deploy/factory-dashboard-http.service`](deploy/factory-dashboard-http.service)
is an example systemd user unit for the pc-wsl-bots WSL node. It runs
[`deploy/factory-dashboard-http.py`](deploy/factory-dashboard-http.py), a
small standard-library server, instead of `python3 -m http.server` (which
follows symlinks out of its directory, lists directories, and serves the
home directory when `--directory` is empty). It answers GET and HEAD only,
and only for `/`, `/status.json` and the four pages (`/index.html`,
`/job.html`, `/activity.html`, `/efficiency.html`; an older copy of the
server answers 404 for the last three until it is replaced): every other path, dot-
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
