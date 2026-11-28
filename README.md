# factory

Grok Bot orchestrates pstack work across Claude Code, Codex and Cursor.

The specification is [docs/SPEC.md](docs/SPEC.md). This repo has the F1-01,
F1-03, F1-04, F1-05 and F1-09 contracts, the F1-06 blocking wait, the F1-02 pstack pin, the F1-07
`orchestrate-project` skill, the F1-08 usage reserve, the F1-13 orchestrator
model, and the F1-14 weekly eval report. Other Phase 1 items are not built.
Phase 2 has the F2-01 device pool and F2-02 remote start; the shipped
`machines.yml` runs orch on the PC with the box as a remote device, and the
Mac ships `enabled: false` until Max turns it on.

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
- A `ship: merge` or `ship: handoff` repo may add `hold:` to `factory.md`
  (issues #86 and #183, [`lib/hold.py`](lib/hold.py)). A PR carrying
  `hold.label` (default `factory:hold`), or whose diff touches a `hold.paths`
  glob, does not ship on APPROVE. A matched repo-root `factory.md` counts
  only when its `hold`, `ship`, `finish_line`, `builder` or `reviewer` front
  matter differs from base (#564). A held PR waits in `orch:state/awaiting-ok`
  ("waiting on <approver>" in `orch status` and the dashboard) until
  `hold.approver` posts `<!-- orch:v1 kind=ok head=<sha> -->` for the current
  head, or removes the label on a label-only hold. The OK may also be a
  comment whose first line is `/ok <40-char sha>`. Grok Bot is woken once per
  held head. The OK carries to a moved head with an equal
  `git patch-id --stable`, which `stable_patch_id` computes (below).
- Approver commands (#173). Max may post two plain PR comments in place of
  `orch:v1` ones.
  - `/go` is a comment whose first non-empty line is `/go`, with at most one
    more line ([`lib/approver_commands.py`](lib/approver_commands.py)). Only a
    login in orch.yml `approver_logins` (today `maxharris1`) may post it,
    whether or not `github_app.enabled` is true. On a tracked PR it resumes
    the first of these that applies. That is a `repeat=` or `unsat=` park in
    waiting-max, then a BLOCKED review, then a capped ticket (one more round),
    then a paused step. The resumed job's evidence carries the second line as
    its instruction. An edited `/go` is refused. A `/go` from another
    `trusted_logins` login wakes Grok Bot with `go-not-from-approver`, and one
    from anyone else is ignored. On a stuck ticket it only makes sure Grok Bot
    has the stuck wake. With nothing to resume it is handled as
    `slash-go-no-target` and changes nothing.
  - `/ok <sha>` is a comment whose first line is `/ok` and the full
    40-character lowercase head SHA. On a held PR (the hold label, or a diff
    that touches `hold.paths`) it does what
    `<!-- orch:v1 kind=ok head=<sha> -->` does. Only `hold.approver`
    (`maxharris1` here) counts, and only while that login may approve. That
    is a `trusted_logins` key while `github_app.enabled` is false, and a
    login in `approver_logins` once it is true
    ([`lib/dispatch/holds.py`](lib/dispatch/holds.py) `_may_approve`). It
    clears the hold for that head, or for a moved head with an equal
    `git patch-id --stable`, so the poller can ship once the rest of the
    ship rule holds. An edited `/ok` does not count.
- A repo may list shared docs in `factory.md` `shared_paths` (issue #261),
  paths or globs such as `docs/SPEC.md` or `docs/decisions/` (a trailing `/`
  is a directory). A `Touches:` path that matches never makes the feeder hold
  a ticket (a `Conflicts with` edge still does), and an update from base that
  keeps both sides of a conflict hunk in such a file, unchanged and one after
  the other, still carries the approval. Code paths are never affected.
- Up to `factory.md` `feeder.max_holders_per_path` tickets (default 2, issue
  #269) may hold one `Touches:` path at once; the serial merge and the base
  update resolve the overlap. Paths in `feeder.exclusive_paths` and
  `Conflicts with` edges keep one holder.
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
| `merge` | `GET pulls/<n>` (and for a draft the GraphQL `markPullRequestReadyForReview`), then `PUT pulls/<n>/merge` with `merge_method=squash`, `sha=<head>` | Marks a draft ready first (issue #83: GitHub refuses to merge a draft with 405). The API form of `--squash --match-head-commit`. A 409 head mismatch raises `HeadMismatchError`. A 405, a 5xx, a timeout or a mark-ready that did not land raises with `retryable=True`, so the dispatcher re-drives the ship once; anything else, or a failed re-drive, flags the ticket needs-attention |
| `list_comments` | `GET issues/<n>/comments`, `--paginate` | Ids must be positive integers (coerced to int); `created_at` must be ISO-8601 |
| `ci_status` | `GET commits/<sha>/check-runs`, `--paginate`, and `GET commits/<sha>/status` | Only the latest attempt of each check (same app, same workflow run, same name, latest start) counts, so a rerun at the same head replaces the attempt it reran (issue #84). The workflow run is the run id in an Actions check's `details_url`, else its check suite; two workflows that share a job name are two checks, and a check run with neither supersedes nothing (#87). Any failure is `failure`. `success` needs at least one check run that concluded `success`, or a `success` commit status, with nothing pending. All neutral or skipped, anything running, nothing reported, or runs received that do not match `total_count` on every page is `pending`. A short read is `pending` even with a failure in view, since a rerun on an unread page may supersede it |
| `set_state` | `issues/<n>/labels` | Adds `orch:state/<state>`, then removes other `orch:state/` labels. Linear and Aut0 get none |
| `await_approver` | `POST issues/<n>/comments` | Tells the approver which SHA is previewed |
| `stable_patch_id` | `GET pulls/<n>` for `base.ref`, then `GET compare/<base>...<head>` as a diff | `git patch-id --stable` of that diff, run with no repository. An empty diff, a diff that names a binary file, or a `git patch-id` that fails or times out raises `GitHubAdapterError` |
| `handoff` | `GET pulls/<n>`, then, when draft, `gh api -X POST graphql` with `markPullRequestReadyForReview` on the pull's `node_id` (GitHub has no REST route for this; it is what `gh pr ready` sends) | Confirms the linked PR is unmerged and still at the approved head, and that GitHub reports `isDraft: false` at that head; never merges or requests a reviewer |

SHAs must be 40 lowercase hex, and owner, repo, branch and PR number must
match strict patterns before any call. A non-zero gh exit, malformed JSON or a
timeout raises `GitHubAdapterError`, which is a `ShipError`. A ship then
fails closed: `ship-failed` and one Grok wake. `ship_intents.error` records
the class and a safe detail, such as `GitHubAdapterError: gh api POST graphql
exited 1 (HTTP 403) graphql errors: FORBIDDEN`. It never holds gh's output, a
URL, a token or a body; the merge 409 and fast-forward 422 errors carry only
the inner safe detail. A target repo's branch protection or ruleset can
reject the `fast_forward` ref update; that rejection is a
`GitHubAdapterError` too, so the ship fails closed as `ship-failed`, and the
factory never bypasses the protection. A `handoff` that failed before any
write was confirmed is re-driven once on a later poll, and only while its APPROVE is still the
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

`release`, `heartbeat` and `finish` take `owner=`, the recipient that claimed
the job. Each refuses an owner that is not a recipient, a job that belongs to
another recipient, and a job that is not running; a refused call changes
nothing. A repeated `finish` with the same outcome and handoff ID returns the
recorded job unchanged; one with a different outcome or handoff ID is refused
and the first stays. `claim` takes a job in the issue #85 order, priority tier first (#258):
urgent, then a resumed paused step, then bug, then the ticket that unblocks
more, then the oldest.

Jobs name one action (`plan`, `build`, `review`, `fix`, `clarify`, `stuck`,
or the Grok wakes `blocked`, `capped`, `refused`, `unparsed` and `ship-failed`),
one recipient (`claude`, `codex`, `cursor`, or `grok`), and a generation. The
dispatcher and queue use one SQLite database so terminal state and its wake
commit atomically. The dispatcher queues jobs; it never executes a harness
binary.

## Device pool

[`machines.yml`](machines.yml) is the F2-01 device inventory
([docs/SPEC.md](docs/SPEC.md) section 13);
[`config/machines.example.yml`](config/machines.example.yml) is an example
with orch on the PC and the box as a remote device. Each device has a `name`, a `tailscale_name`, `slot_max`, `harnesses`,
`workdir_root`, `ssh_alias`, `enabled` (default `true`) and `drain` (default
`false`). `harnesses` maps each eligible
harness to the bot OS user whose login Max provisioned there, and that user
must be the harness's own bot (`claude: claude-bot`, `codex: codex-bot`,
`cursor: cursor-bot`). The top-level `orch_host` names the device that runs
`orch`; it must be a listed device, and a `local: true` device must be it.
Without `orch_host`, the one local device is the orch host; with no local
device, `orch_host` is required. The local device omits `tailscale_name`,
`workdir_root` and `ssh_alias`. Every other device names
a `workdir_root`, an absolute normalized path that may not be under `/mnt/`,
so jobs on a WSL node stay off the Windows drives. A whole path component may
be `{bot}`, which resolves to the harness's bot user
(`Device.workdir_for(harness)`). Every remote device also names an
`ssh_alias` (F2-02): the Host alias in the orch host user's own SSH client config,
which must contain `{bot}` so each bot logs in through its own alias
(`Device.ssh_alias_for(harness)`; the PC's `pc-wsl-{bot}` gives
`pc-wsl-claude-bot`, `pc-wsl-codex-bot` and `pc-wsl-cursor-bot`). It is a bare
alias: no `user@`, no leading `-`, no space or shell character, never a key
path; the host name and key stay in that SSH config. A remote device may
name an `ssh_user`, the login (`ssh -l`) used in place of the bot user; the
bot user stays the job's identity. A Tailscale name is a device name, never an
IP address. Two devices may not share a `tailscale_name` (compared
case-insensitively): slots are counted per device name, so a second entry
would give one host a second `slot_max`. Release a device's held slots
before renaming it. Unknown fields are refused, and `workdir_root` may not
contain NUL or another control character.

The shipped file sets `orch_host: pc` and has no `local: true` device. It
lists the PC's WSL Ubuntu node first (all three harnesses, 10 slots), then the
box as a remote device (all three harnesses, 6 slots), then the Mac. Slots are
generic, so they are not one per bot. The PC's `workdir_root` is
`/home/{bot}/factory-work` (for example `/home/codex-bot/factory-work`), off
`/mnt/c`, and its `ssh_alias` is `pc-wsl-{bot}`, the aliases
`pc-wsl-claude-bot`, `pc-wsl-codex-bot` and `pc-wsl-cursor-bot`. On the box,
each bot logs in as its own user (`claude-bot`, `codex-bot` or `cursor-bot`)
through its `box-{bot}` alias; the shipped file keeps `workdir_root` at
`/home/box/factory-work` and `factory_root` at `/home/box/factory` (overridden
per host in `machines.local.yml`), and the bot user stays the job's identity.
The Mac ships `enabled: false` and stays
disabled until Max turns it on; it has no bot users and no SSH yet, and its
`workdir_root` and `ssh_alias` are still `REPLACE_WITH_` placeholders. Nothing
assumes a device is reachable: the poller checks it (below).

To stop new jobs on a device and let its running jobs finish, set
`drain: true` on it (issue #777). A draining device keeps every slot it holds
and the poller leaves its jobs alone, so they finish and hand off as usual;
only new placement skips it, and `orch status` marks it `draining`. Do not
use `enabled: false` for this: a disabled device's running jobs are lost and
stopped (issue #679). Lowering `slot_max` below the running count also stops
new placement without touching running jobs, but a slot frees as each job
ends. Waiters read `machines.yml` only at start, so after a hand edit run
`deploy/waiter-service.sh <bot> restart` for each bot and
`deploy/poller-service.sh restart`; neither stops a running job. Set
`drain: false` (or remove the line) and restart them the same way to undo it.
The Mission Control Settings page makes the same changes (`drain`, `enabled`
and `slot_max`) and runs those restarts itself; see "Device settings" under
"Dashboard".

[`lib/device_pool.py`](lib/device_pool.py) is the scheduler. `DevicePool.claim(harness)`
moves the first queued job for that harness, in claim order (a priority tier
first, then a ticket marked urgent, then a job resuming a paused step, then a
ticket marked bug, then the ticket that unblocks more, then the oldest; issues
#85, #258), to running and holds a slot for it on an (account, device)
pair (issues #1315, #1316 and #1317). Slots are generic: any free slot on an
eligible device takes any harness.

A device is free for the harness when it is enabled, is not draining, has
no placeholder, lists the harness, has a free slot, and is not held back by
its checkout, a backoff or a memory hold. A job of a project with
`projects[].devices` sees only its listed devices. Free devices rank by
utilization (slots held over `slot_max`; a tie goes to a remote device
before the local box, remotes in `machines.yml` order; issue #223). The
candidate accounts on a device are its own login, then each account whose
`signed_in_on` names it. An account takes no job while it is paused or
usage-limited, while it runs `max_jobs` jobs across every device, or while
its fresh headroom is under the floor, 0.05. Fresh headroom is the least `1 - used`
over the fresh windows of the account's newest readings. Each open account
gets a score. The score is its fresh headroom (0.5 when it has no fresh
reading) less a pending charge of 0.02 for each running job placed on it
since its newest reading. The charge covers jobs that reading has not seen
yet, so a burst of claims spreads across accounts. The claim compares open
accounts by these keys, in order, and the winner takes its best-ranked free
device:

1. A best free device that the job need not avoid (issue #218).
2. The higher score.
3. Fewer running jobs.
4. The better rank of its best free device.
5. The account id, in sort order.

After `orch accounts doctor` has written the `account_signins` table, an
account other than the device's own login places on a device only when its
row for that device is `ok`. The device's own login needs no row. Before the
table exists, placement does not check sign-ins. `placement_blocks` applies
the same rules, so a device and harness where no account can take a job
counts as blocked. A queued job whose candidate account is limited or under
the floor, and that has no free placement, restarts its queue wait instead
of timing out. [docs/decisions/1064-account-placement.md](docs/decisions/1064-account-placement.md)
records why the defaults are what they are. The job update and the
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
the job's worker. On Linux the script refuses (exit 125, nothing run) a device
without `/proc` and a `grep -z`, makes `<workdir_root>/jobs/<job-id>`, records
the worker's process group and boot id (sshd starts the command in its own
session) in `.orch-worker.pid` there, and execs the command with `ORCH_JOB_ID`,
`ORCH_JOB_BOT`, `ORCH_TEST_SUITE_MAX`, `ORCH_JOB_DEVICE` and `ORCH_JOB_WORKDIR`
(not `ORCH_DB`: the database stays on the box, and the bot runs `orch job done`
there); ssh exits
with the command's status. The record is never removed. The heartbeat stays
local and beats while ssh runs; SIGTERM and SIGHUP go to ssh.
The command runs under the bot's login shell (`bash -lc 'exec "$@"'`), so the
bot's own PATH (gh, claude, codex) is loaded, and stdin is forwarded to it.

`--worktree` works the same on every device (#94): the harness runs in the
job's checkout, `$ORCH_JOB_WORKTREE`. On a remote device `orch job run` first
checks over the same alias that `git`, `gh` and the command's binary exist
under `bash -lc`, then clones or fetches the job's repo into
`<workdir_root>/jobs/<job-id>` at the job's head (on the job's branch when
that branch's tip is the head, else detached; with no head, the base
branch), keeping orch's worker records out of `git status`, and copies
`$ORCH_JOB_BRIEF`, if set, to `<job-id>.brief.md` beside the checkout
(`ORCH_JOB_BRIEF` then names that copy on the device). An existing checkout
is reused as is, and a remote checkout is never removed. If the check or the
checkout fails (a missing tool, an unreachable device, a failed clone),
nothing starts: the job is requeued and its slot freed in one transaction,
with no round or attempt charged, a `requeued-unstarted` job event, one log
line saying why, and exit status 75. Harnesses never build their own remote
command.

A macOS device (`uname -s` is `Darwin`) has no `/proc` and cannot read another
process's environment, so there the worker is named by its record alone, one
file per start (`.orch-worker-<tag>.pid` in the job directory). The start
refuses (exit 125) unless `sysctl -n kern.bootsessionuuid` gives a boot session
id (a UUID) and the start leads its own process group, then creates the record with
noclobber (it refuses if the file exists), holding
`<group> <boot session> <tag> <leader start time>` (`ps -o lstart=`, C locale,
UTC). Just before, it starts a keeper in its own group and writes the keeper's
pid and start time as the first snapshot (below). The keeper ignores SIGTERM,
SIGINT and SIGHUP and exits once no other process is in the group, so the group
is never empty while any process of the start lives, and its number is never
reused meanwhile (Codex PR #129 round 3). A check that finds no record cannot
tell whether the start is still on its way, so it creates the record itself as
a fence (`fenced`) and cannot tell this time; the fenced start then refuses,
and later checks read the fence as gone. A start that never ran (unreachable,
refused) so frees its slot one check later. The poll that follows a detached
start (issue #128) never writes the fence: with no record it cannot tell, and
the start it follows records itself.
The check reads the record and `ps -A -o pgid=,pid=,uid=`. Gone needs
every step to succeed with a well-formed result: ps exits 0, every line it
lists is three numbers, and awk reads all of them, exits 0 and prints exactly
three 0/1 answers. Then a record from an earlier boot session or an empty group
is gone; a group holding only the bot's own processes and led by the recorded
leader is running, and so is one whose leader has exited while it still holds
a process from the last stop's snapshot (below) with its recorded start time;
a malformed record, another tag, another user's process in the group,
a group whose leader has exited with no such process (nothing ties the
processes left to this start),
a leader with a different start time (the number was reused), or any failed or
malformed step (no boot session UUID, a user id that is not a number, ps, awk)
cannot tell. The stop sends SIGTERM (SIGKILL when a cancel escalates) to the
recorded group only when the check would say running. While the leader is
verified alive, the stop first writes a snapshot of the group
(`.orch-worker-<tag>.members.pid`: each member's pid and start time, from one
`ps -A -o pgid=,pid=,lstart=` listing, kept only when the leader is verified
again after it). A snapshot member still in the group with the same start time
proves the group was never empty since, so its number was never reused: a
cancelled run whose harness dies on SIGTERM but leaves a child that ignores it
is still killed (Codex PR #129 round 2). With the keeper, so is a child born
after the snapshot, even one the harness forks while it shuts down, and a child
left in the group after a normal exit is stopped before the slot is released,
as on Linux (round 3). A process that leaves the job's group (`setsid`) is
never signalled on macOS. Every script runs under POSIX `sh` whatever the login
shell (the Mac bots use zsh).

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
checked again for up to 10 seconds. A cancelled run (a signal to `orch job
run`, or its job ended elsewhere) is not left running: the follow sends
SIGTERM, then SIGKILL to the same processes from 60 seconds on, and follows
the worker until it is gone, for up to two minutes; if it is still not
confirmed gone, the final check sends SIGKILL after its SIGTERM too (issue
#128). An unreachable device, a check that cannot
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
`job_states.unreachable_lost_minutes` (default 10), or in the first poll after
it is set `enabled: false` (issue #679, whether it answers or not), each job
still running on it is lost (issue #662): in one transaction it is failed for T6 with a
`device-lost` job event, its slot row moves to `device_orphans` (so the slot is
free at once) and the device is backed off for 5 minutes. T6 requeues a lost
job as it does a link drop, under the same budget, so it lands on another
device when one has room. A lost `update-from-main` job is not requeued; it
counts one update failure and the merge queue settles any head it pushed.
The poller loads `machines.yml` again on its first poll and whenever it or
`machines.local.yml` changes, so `enabled: false` takes effect with no restart;
a file that fails to load keeps the last good config and is recorded as a
`machines.yml:` poll error each poll until it is fixed. The box is covered too:
a job running there when it is disabled is lost the same way, and its `orch job
run` (or its worker, if that is gone) is sent SIGTERM; its `device_orphans`
row keeps both pids, a later poll sends SIGKILL to whichever still runs, and
the row is deleted once both are gone. Each poll also checks
every `device_orphans` row by its worker tag (issue #679): a lost job's worker
still running is sent SIGTERM, a gone one's row is deleted, and an unreachable
device gets one check per poll. Every check that lets a remote worker go first
makes that start's run directory on the device, so a launch still in transit
from a lost runner answers `already` and starts nothing. On Linux it also takes
the start's own record, as on macOS, so a start already launched but not yet
recorded refuses and never runs, even when the process scan missed it.
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
the F2-02 device checks (slot release and the device-lost check, see
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
repo. `projects[].linear_project_ids: [A, B]` maps several Linear projects to
one repo instead (issue #132): its tickets are fed and tracked from any of
them, and a ticket moved between them keeps its job. Set one key or the
other, not both. An empty list or a repeated ID is refused. `trusted_logins` maps GitHub authors to sets of bot markers, while
`trusted_linear_users` maps immutable Linear user IDs to sets separately.
The dispatcher still requires the expected `from=` and `role=`. With one
shared GitHub login, the marker is a routing label, not an identity or
signature. The shipped config records the known shared GitHub login and leaves
the stable Linear user ID for Max to confirm. Max approved the GitHub map at
3:09 PM ET on November 11, 2026: "Yes, the factory may trust maxharris1 for
comments on factory PRs". This approves the login map, not stronger identity:
any session using that login can choose a marker. While any configured Linear
project lacks `trusted_linear_users`, `orch poll` refuses to start at all,
including for GitHub projects. `approver_linear_users` names the Linear users
whose `go` lifts a cap on a Linear ticket
([docs/decisions/174-linear-approver.md](docs/decisions/174-linear-approver.md)). Missing secrets fail by environment-variable
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
retry next cycle. The ticket's factory PR supplies the current head and CI, and
its issue comments are joined with the Linear comments. That PR is the one the
feeder recorded, else the newest PR on the issue's own branch, open first. A
PR that only names the key or is linked from Linear is never bound (#600).
`ready`, `verdict`, and `stuck` go on PR issue comments, never reviews or
inline comments.

A tracked GitHub PR that the poller has read before is first probed, with one
GraphQL query per repo per 25 PRs. The probe returns the PR's state, head,
base, mergeability, labels, newest 100 comments, review count and the head's
CI contexts. When that matches the probe taken before the PR's last full read,
and that read is less than 10 minutes old (`FULL_READ_SECONDS`), the poller
reuses the read and makes no per-PR call (#870). A changed probe, a PR missing
from the probe, a GraphQL error under the PR or under no single PR, a failed probe (a `GitHub probe failed` deferral) or an older
read gets the full read: the PR, its comments, CI, and the hold's labels,
files and label events. A caller's fresh read never reuses. The probe's time
is the `github-graphql` source of the `tracked-read` phase on the `cycle end`
line, and `scripts/issue-870-tracked-read-times.py` prints that phase's median
and p90 over `poller.log`.
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
unknown state fails the handoff before any write. The target is the first
`linear_handoff_states` name (default Ready for Review).
`projects[].linear_rework_states` (default none) names the states a reviewer
uses to send an issue back, such as `[Changes Requested]`; the approved fix
round moves an issue in one of them back to the target, wherever the state
sits in the team's order. A name may not also be a handoff state. On a team
with no Ready for Review state the PR is still marked ready and the handoff completes; the
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
project or ticket and the long-running loop continues after the
`poll_seconds` interval; `--once` reports a cycle-level failure with a
nonzero exit.

*State loss (#66).* After every cycle `orch poll` backs the database up to
`$ORCH_DB_BACKUP`, else `~/.local/share/factory/backup/orch.db` (outside the
state directory a reboot can lose; a path inside the database's own directory
is refused). `ORCH_DB_BACKUP_EVERY` (seconds, default 0) spaces those
backups out: with 900, a cycle backs up only when 900 seconds have passed since
the last good one, and a failed one is tried again the next cycle; a value that
is not a whole number makes `orch poll` exit 1. The weekly snapshot still
checks every cycle. A failed backup is one `poll_errors` row. `deploy/poller-service.sh`
`start` and `ensure` restore a missing database with `orch restore-db` before
launching (`RESTORED orch.db from backup`), or, with no backup, create an empty
one with `orch init-db` and log a `WARNING`. On an empty or stale database the
dispatcher still queues no job for a step the thread already answered: a later
trusted `orch:v1` reply records the job as cancelled (`answered-<action>`).
Before any manual write to `orch.db`, run `orch db-backup <label>` (#654). It
writes `manual/orch-<UTC stamp>-<label>.db` beside the cycle backup, then keeps
the newest 5 labelled backups plus any under 48 hours old and deletes the rest,
with their `-wal` and `-shm` files. It prunes on each `db-backup`, and `orch
poll` prunes once a day.

*Spilling job detail.* `orch db-spill-detail` moves bulky job detail out of
`orch.db` into `job-detail/<id>.json` beside it (#653, #1777). Run it in this
order:

1. `orch db-spill-detail --dry-run` against the live database. It opens the
   database read-only, takes no lock, writes nothing, and prints the rows and
   inline bytes of live, replayable and cold jobs, the bytes the cold spill
   would move, the rows whose detail is not a JSON object, and the ten largest
   inline keys.
2. Stop the poller and the waiters (`deploy/poller-service.sh stop`,
   `deploy/waiter-service.sh <bot> stop`).
3. `orch db-spill-detail --vacuum`. It holds the poller's lock and exits 1
   while any `waiter-<bot>.lock` beside the database is held. It writes
   `manual/orch-<UTC stamp>-pre-spill.db` with the `db-backup` labelled backup
   (`--dir` moves it) and exits 1, changing no row, unless that copy passes
   `PRAGMA integrity_check`. It then moves the fixed keys of every job and
   each key over 1 KB of every cold job (finished, with its ticket merged,
   done or abandoned), runs `VACUUM` and `wal_checkpoint(TRUNCATE)`, and
   prints the rows and bytes moved, the backup path and the database size
   before and after. Without `--vacuum` it spills and leaves the file size
   alone. `orch poll` and `orch wait` never vacuum.
4. Start the poller and the waiters again.

To undo it, stop them, move `orch.db` and its `-wal` and `-shm` aside (`orch
restore-db` refuses an existing database), and run `orch restore-db --db
<path> --backup <printed backup path>`. Leave `job-detail/` in place.

Afterwards merged readers (the dispatcher, the dashboard, briefs, `move-db`)
see each job's full detail as before. A raw SQL read of `jobs.detail` sees the
slim row: its `spilled` key names the keys now in `job-detail/<id>.json`.

*Poll interval.* `poll_seconds` in `orch.yml` may be 30 or 60 (anything else is refused); the shipped value is 30. It is how long `orch poll` sleeps after a cycle finishes, so a cycle's elapsed time includes the work it does: cycle start to cycle start is that work plus `poll_seconds`. The sleep ends when either the wall clock or the monotonic clock reaches `poll_seconds`, so a box VM pause in the sleep does not add the pause on top (issue #470). When the time from one `cycle end` to the next `cycle start` runs more than 10 seconds past `poll_seconds`, poller.log gets an `orch poll: between cycles` line before `cycle start` that gives the sleep's wall and monotonic seconds and any clock jump. The `cycle end` line breaks `after-poll` into `backup`, `dashboard-push` and `self-deploy`. 30 seconds is the poll interval, not a promise that a comment is picked up and acted on within 30 seconds. GitHub is read
every cycle. Linear keeps its own 60-second cadence and budget: the
discovery search, dirty-row reads (with `linear_webhook`) and tracked Linear
reads run only on every second cycle at 30 seconds (every cycle at 60), and
the per-window Linear budget (20 requests, 25,000 complexity) still covers 60
seconds. A direct Linear read between those cycles (the fresh read before a
ship or write) counts against the same window, so a 30-second poller makes
no more Linear calls per hour than a 60-second one, and the webhook's
`reconcile_seconds` fallback is unchanged (docs/SPEC.md section 7).

Each polling `gh api` read, including PR comments, check runs and commit
status, has a 20-second timeout and is tried three times with 2- and 4-second
backoff (writes are never retried); a ref that still times out records a retryable
`gh api timed out after 3 attempts` error and the cycle moves on.
Linear budget and search page-limit deferrals are expected load shedding, not
errors: they do not fail the cycle and are written as at most one aggregated
row per cycle to `poll_deferrals`, not `poll_errors`.
A poll step's exception, and one that escapes a whole cycle, keeps its class in
`poll_errors.error` and stores its message and newest ten frames, redacted and
capped at 4,000 characters, in `poll_errors.detail`; each message in the chain
is cut at its first character outside letters, digits, spaces and `_.,:;()'/=+-`,
so no response body, escaped or not, is kept.
The same text goes to the poller's stderr. Each stuck ticket's wake is tried on its own, so one bad
`ticket_state` row records a `stuck-wake:<class>` error and the rest of the
cycle still runs.
At startup, polling refuses an invalid `orch.yml` (including placeholders), a
missing GitHub trust map, any configured Linear project with no Linear trust
map, missing Grok webhook environment-variable names, a missing initialized
database, or a lock already held by another poller. Runtime source or delivery
failures stay fail-closed and are recorded instead of weakening those checks.
With `linear_webhook` in `orch.yml`, Linear discovery is driven by dirty
issues instead of a search every cycle (next section); without it, every
cycle is exactly as described above.

### Backlog feeder (issue #85)

A project with an `intake` section in `orch.yml` is fed by
[`lib/feeder.py`](lib/feeder.py) at the start of each poll, before
discovery. The signal is per-project config, never code:

```yaml
  - repo: owner/app            # factory.md tickets: github
    factory_ref: main
    intake:
      label: factory:ready     # default; applied by a trusted login
  - repo: owner/linear-app     # a Linear project
    factory_ref: dev
    linear_project_id: ...     # or linear_project_ids: [..., ...]
    intake:
      linear_state: In Progress
      # trusted: [linear-user-id]   # default: the trusted_linear_users keys
```

An open GitHub issue carrying the label, applied by a trusted login (the
actor of the latest `labeled` event; default the `trusted_logins` keys), or a
Linear issue in the state, moved there by a trusted Linear user, is
eligible. For each eligible ticket, while its builder harness has room, the
feeder opens a branch from the base branch with an empty start commit
(`factory/issue-<n>`, or the Linear issue's own branch name), opens a draft
PR whose body says `Fixes #<n>` (or `Fixes <identifier>`), labels it
`factory` and `orch:state/<building|planning>` on a GitHub-ticket repo, and
posts a `brief` (the ticket body plus the repo's `factory.md` prose) and a
`go` (state=planning when the ticket names more than 3 files, else
building; risk=high on a `risk:high` label), both from `grok-bot` as the
orchestrator. On a Linear project brief and go go on the Linear issue in the
visible header form, and no GitHub label is written. The poll then reads the
new PR fresh and starts tracking it, so T1 queues its job in the same poll.

Every step is recorded in `feeder_items` before the next, keyed on (repo,
ticket): a re-poll never opens a second PR, a PR already open on the branch
is reused (the open one, never an older merged or closed PR on the same
branch), and a brief or go the orchestrator already posted is not posted
again. A retry on a PR already recorded first checks that PR is still open;
one closed or merged meanwhile ends the feed (`closed`) with nothing posted. Only a brief or go whose author orch.yml trusts as `grok-bot` counts as
already posted, so a forged one never stands in for the real go. A fed
ticket is never fed again. Room for a harness is the free slots on every
schedulable device that lists it, minus the queued jobs of every harness
listed on such a device (a queued codex review takes a slot claude could
use on a shared device), minus tickets fed
for it in the last 10 minutes whose job is not queued yet (so a go the
poller never acts on cannot stall the feeder). A ticket whose feed failed
midway resumes first, but takes room like a new one: it waits while the
harness has none. Tickets are fed by priority tier first (issue #258): the
project's `intake.priority_labels` is an ordered label list, a ticket's tier
is the first one it carries (a Linear project may set
`priority_from_linear: true` to use Linear priority Urgent..Low as tiers
0-3), an urgent ticket with neither is tier 0, and any other ticket with
none sorts after every tier. The rank uses the effective tier (issue #1050,
[`docs/decisions/1050-global-rank-v2.md`](docs/decisions/1050-global-rank-v2.md)):
the best tier among the ticket's own and every candidate it unblocks through
`Blocked by:` or `Depends on:`, transitively, so a p1 blocker of a p0 ticket
ranks as p0. A fed ticket passes its tier on too. Inside a tier: urgent (an `urgent`
label or Linear priority Urgent), then `bug`, then the ticket that unblocks
the most open intake tickets (through `Blocked by:` lines, plus the
unstarted tickets that share a `Touches:` path with it; `critical_path_order:
false` turns this off), then oldest. A ticket waits while its project's
`feeder.max_holders_per_path` tickets in flight (default 2) already hold one
of its `Touches:` paths, naming them (`waiting on file lib/x.py held by #1,
#2`); a path in `feeder.exclusive_paths` and a `Conflicts with` edge keep
one holder, and a ticket fed onto a held path logs `feeder: owner/app#3
shares lib/x.py with #1`. The feeder also records those marks in
`ticket_priority`, refreshed every poll (a label added after the feed still
moves the queued job), which the queue's claim order reads (effective tier,
then a finishing job (`review`, `fix`, `update-from-main`, `qa`) before a
starting one (`plan`, `build`), then urgent, then a resumed paused step, then
bug, then unblocks, then oldest). `orch status` and the dashboard show a
ticket's tier (`tier 0`), with its own tier when the effective tier is better
(`tier 0 (own 1)`, or `tier 0 (own none)` for a ticket with no tier of its
own). A project may set `intake.capacity_share` (0 to 1; all shares sum to
at most 1): while it holds fewer in-flight tickets than that share of its
builder's schedulable slots, its best eligible ticket is fed before any other
project's. With no share set the order is unchanged. An open issue in a
tracked repo that a candidate is `Blocked by:` but that is not a candidate
itself raises one `critical-path-blocker` attention flag (`gates 2
candidates, best tier 0`), listed in `orch status` under `Critical-path
blockers` and cleared once a poll reads it closed (a failed read keeps
it). The live
clients are in [`lib/intake_live.py`](lib/intake_live.py): GitHub through the
same `env -u GH_TOKEN ... gh api` seam, Linear through the poller's own
Linear request path and budget (every page of the intake state, read with
the same `gitBranchName: branchName` alias the poller uses). A feeder error is recorded in `poll_errors`
(`feeder`) and never stops the poll.

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
  tunnel_alert_minutes: 30        # tunnel down this long needs attention (README "Health")
```

`orch linear-webhook serve` is a small stdlib HTTP server. It opens the
existing dispatcher database (never creates it; run `init-db` first),
creates its own `linear_dirty`, `linear_webhook_health`,
`linear_project_issues` and `linear_webhook_seen` tables, and for
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
- for an Issue in a configured Linear project (or just moved out of
  one), or a Comment on a tracked issue, or a Comment carrying an `orch:v1`
  marker on an issue a signed Issue event last placed in a project that is
  still configured (the project is stored with it, issue #69), upserts one
  `linear_dirty(issue_id, identifier, event, received_at)` row and returns
  200. Comment payloads do not name the
  issue's project and a marker alone proves nothing, so the listener keeps
  `linear_project_issues` from Issue events; a handoff on an issue it has
  not seen is found by the reconcile search. The poller still checks the
  project after its direct read. Every other signed event gets 200
  and is ignored. A database error returns 500 so Linear retries.

It never runs handoff logic and never calls Linear. Log lines are
`2026-11-13T14:02:00Z linear-webhook: accepted Issue.update AUT-123`-style:
a UTC timestamp, outcome, event type and identifier, never bodies (PHI),
headers or signatures.

Webhook health (issues #79 and #107): every delivery whose signature
verifies counts as received, whatever happens to it next (stale timestamp,
malformed, ignored, an Attachment event), so it proves the path works. Each
signed, fresh delivery that names an issue also records when that issue was
last delivered (`linear_webhook_seen`). When the poller reads an issue in a
configured project directly, it compares the issue's newest comment with
that record: a comment made at least a minute after the listener started,
at least two minutes old, with no delivery for its issue since a minute
before it, is a missed delivery (`linear_webhook_missed`). `orch status`
and the dashboard warn only on a miss found in the last 24 hours
(`Linear webhook missed 1 delivery in 24h (newest AUT-123, found 12m
ago)`). Over an hour with no delivery while Linear tickets are active and
nothing missed is a soft note (`quiet: no Linear webhook for 2h (nothing
missed by the last reconcile)`): Linear may simply have had nothing to
send.

The signing secret is read once, at startup, from `secret_file`. A file
readable by group or others, empty, or unreadable is refused with an error
that names the path only. The value is never logged, printed, stored or put
in an error. `check-config` validates the keys and never reads the file.
Max creates the webhook in Linear (Issue and Comment resource types, scoped
to the Aut0 team) with the Funnel URL, and saves its signing secret to
`~/.config/factory/linear-webhook.secret` with `chmod 600`. Tailscale Network
owns the Funnel that exposes only the listener path. This repository
changes no network configuration.

### Listener service without systemd

[`deploy/linear-webhook-service.sh`](deploy/linear-webhook-service.sh) keeps
two loops up on the box (issue #134). `deploy/install.sh` copies it to
`~/.local/share/factory/linear-webhook-service.sh`; run that copy, not the
checkout's file, so a `git pull` never rewrites a script a running loop reads.

- `_listener-loop` runs `orch linear-webhook serve --bind 127.0.0.1:PORT`,
  adding `--orch "$ORCH_CONFIG"` when `ORCH_CONFIG` is set. It
  starts `serve` only while nothing listens on `127.0.0.1:PORT`, and logs one
  line per streak while the port is taken.
- `_tunnel-loop` runs `ssh -n -o ExitOnForwardFailure=yes -R
  127.0.0.1:PORT:127.0.0.1:PORT HOST CLAIM`. CLAIM is a short remote command.
  It writes the pid of the sshd session that holds the forward to
  `~/.cache/factory/linear-webhook-tunnel.ID` on the tunnel host, and it
  runs as long as that session. ID is a random name kept in this state
  directory's `linear-webhook-tunnel.id`, so each device that uses the same
  ssh user on the tunnel host has its own claim. A refused forward is logged as
  `forward refused: port PORT already forwarded on HOST; retry in Ns`.
  After `LINEAR_WEBHOOK_REFUSED_LIMIT` refusals in a row (default 3), the
  loop writes `linear-webhook-tunnel.held` (`PORT HOST`) beside `ORCH_DB`
  (default: the state directory's `orch.db`) and starts no tunnel while it
  stays (issue #427). `orch health` then shows the tunnel down, whatever
  answers the public URL, with `port held by another owner: PORT on HOST`, and self-heal
  raises one alert and does not restart it.
- A held tunnel recovers by itself (issue #581). At once, and then every
  `LINEAR_WEBHOOK_HELD_RETRY_SEC` (default 300, whole seconds), the loop
  reclaims and probes. The reclaim is one ssh to the tunnel host that runs
  `ss -Hltnp "sport = :PORT"` and logs each listener as the holder. It
  sends SIGTERM only to the `sshd` or `sshd-session` whose pid this device's
  claim names. That is this device's own stale session: sshd on the tunnel
  host can keep a dead tunnel's forward for hours. Any other holder is left
  alone and logged as `held by <users>`. This includes another device's
  live tunnel on the same port, so two devices never end each other's
  tunnels. The probe is
  `ssh -o ExitOnForwardFailure=yes -R 127.0.0.1:PORT:127.0.0.1:PORT HOST true`.
  When it succeeds, the loop removes the held record and starts the tunnel.
  Until then the record stays as written, so health never sees a gap.
  `status` shows the last holder. `start` removes the record and the loop
  retries at once; `restart` keeps it. To move this device to another port,
  set it and run `restart`.
- After a run shorter than 30 seconds, both wait `LINEAR_WEBHOOK_RETRY_SEC`
  (default 5), doubling up to `LINEAR_WEBHOOK_RETRY_MAX_SEC` (default 60). A
  longer run resets the wait.
- `LINEAR_WEBHOOK_PORT` is the port `serve` binds and the tunnel forwards,
  the same on the box and on the tunnel host. It defaults to the number in
  `~/.config/factory/linear-webhook.port` (this device's setting), else 8787.
  `LINEAR_WEBHOOK_TUNNEL_HOST` defaults to `pc-wsl-cursor-bot`; an empty host
  runs no tunnel. The Funnel on the tunnel host must point at the same port.
- The tunnel host may be this machine, as on the PC. The script reads the
  host's `hostname` from `ssh -G`, resolves it with `getent ahosts`, and
  checks each address. It drops an IPv6 zone such as `%eth0` and reads an
  IPv4-mapped address such as `::ffff:127.0.0.1` as its IPv4 address. A
  loopback address, `0.0.0.0` or `::`, or one of this machine's own
  addresses from `ip -o addr`, means the host is this machine. Then `start`, `restart` and
  `ensure` run the listener only, and end a tunnel loop and its `ssh` left
  running from before. `status` prints `tunnel: not needed (HOST is this
  host)` and exits 0 while the listener runs. Any
  `linear-webhook-tunnel.held` record is removed. If a check fails, the
  script runs the tunnel as before.

Each loop is a singleton per state directory: it holds an exclusive `flock`
on `linear-webhook-listener.lock` or `linear-webhook-tunnel.lock` for its
whole life and exits 0 if the lock stays held for 2 seconds, so a loop that
starts while `lock_held` probes the lock waits the probe out. Locks belong to
the open file, not a pid, so they hold across PID namespaces (sandboxed bot
shells share the state directory but not pids). `ensure`, `start` and
`status` decide from the locks; the pidfiles are for people only. `flock(1)`
is required.

Commands: `start` (clears a stop), `stop` (holds them stopped), `restart`,
`status` (exit 3 unless every loop runs), `ensure` (start what is missing
unless held stopped; silent when all run), `install-autostart` and
`remove-autostart` (write or remove `~/.config/factory/linear-webhook-autostart`,
which makes the installed `profile.sh` run `ensure` in the background in each
bot shell; where `crontab` exists, `install-autostart` also adds an `@reboot`
line and a 5-minute `ensure` watchdog tagged `# factory-linear-webhook`,
replaced and never duplicated, and changes nothing if the crontab cannot be
read). `stop` ends every loop for this state directory, from any copy
of the script (argv `.../linear-webhook-service.sh _listener-loop` or
`_tunnel-loop` whose `--state`, `ORCH_STATE_DIR` or `HOME` points here) and
every process holding a loop lock (its `/proc/PID/fdinfo` shows the lock; a
process that only has the lock file open is left alone), each with its
children. If a lock is
still held afterwards, by a loop in a PID namespace this shell cannot see,
`stop` says so and exits 1: run it from a host shell.
A `restart` whose `stop` fails drops the `linear-webhook.stopped` hold that
`stop` just wrote, so `ensure` and self-heal (issue #107, which restarts a
down or stale-code listener or tunnel with `restart`) still see a service to
recover; a hold that was already there stays.

Deploy: re-run `deploy/install.sh`, then run
`~/.local/share/factory/linear-webhook-service.sh restart` once from a host
shell, so the new `stop` clears loops started by the old copy.

With the webhook configured, `orch poll` reads the dirty rows and the
tickets with queued or running jobs each Linear cycle (every cycle at
`poll_seconds: 60`, every second cycle at 30). Dirty untracked issues are
read directly, oldest first, under the same Linear budget; one outside the
configured projects, completed, canceled or deleted is dropped quietly.
A tracked ticket is reread in full only when it is dirty, has an active job,
or its last read is `reconcile_seconds` old; otherwise its last read is
reused and the ticket is still rebuilt, so the GitHub PR head, CI and PR
comments stay current. Dirty rows are read before the full brief/go search,
which runs at startup and then every `reconcile_seconds`, and stays due
until its pass has read every hit it listed, so a missed webhook costs about
one reconcile interval.
A dirty row is deleted only after its issue was read in that cycle and only
if no newer event arrived meanwhile; an unread row (budget deferral, error)
waits for the next cycle. A tracked ticket the dispatcher no longer polls
(merged, done or abandoned; a handed-off one is still polled, issue #108)
is not read with or without a webhook, so its row is deleted unread. A read that finds the issue gone
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
bin/orch job cancel <job-id> [--reason TEXT]
bin/orch gc [--older-than HOURS] [--dry-run]
```

`orch job cancel` is an operator command (not a bot's): it moves a queued job
to `cancelled`, so it never runs and T6 never retries it, and keeps its dedupe
key, so the comment that queued it queues nothing again. A running job is
refused (stop its worker first). A merge cancels the ticket's queued harness
jobs the same way (#108).

[`lib/wait.py`](lib/wait.py) blocks on the queue above. `orch wait` accepts
only `claude-bot`, `codex-bot`, and `cursor-bot` (recipients `claude`,
`codex`, `cursor`). It checks for a job once a second, claims the next one
in the queue's claim order (tier, urgent, resumed, bug, unblocks, then oldest) through the
device pool (`--machines`, default the checkout's `machines.yml`,
placeholders accepted and never scheduled), and prints one JSON line with the
job's `device` and `remote` and, on a remote device, its `workdir`. With no free
slot on an eligible device the job stays queued and the waiter keeps waiting. While the poller holds a write, a claim that sees
`database is locked` (after a 5-second SQLite busy timeout) keeps waiting with
backoff capped at 5 seconds; `job heartbeat` and `job done` retry the same way
up to 8 attempts. Other database errors still fail at once.
SIGINT and SIGTERM are held from each claim until its line is printed, so a
signal may wait up to the 5-second busy timeout, or until stdout drains,
before it takes effect; SIGKILL always works.
If that line cannot be printed, the job is finished as
failed so T6 retries it at once, and its slot is released (no bot got it). Plain `orch wait` never starts a harness:
the bot that ran the shell call starts its own, through `orch job run`, or its autostart waiter does (below). `orch` never chooses a harness on its own. `job heartbeat` and `job done` refuse a job that
belongs to another bot. The database is `--db`, else `$ORCH_DB`; a missing file
is refused. Each bot's "orch jobs" paragraph is under [`bots/`](bots/).
Running timeouts use last activity.

### Autostart waiter

```sh
bin/orch wait claude-bot --autostart          # loops; run it as claude-bot
deploy/waiter-service.sh claude-bot start     # the same, under a restart supervisor
```

A process exit does not wake a bot, so a plain `orch wait` claim used to sit
until another bot pinged its owner (#74 r2 and #77 r2 each waited 12 to 57
minutes). [`lib/waiter.py`](lib/waiter.py) (issue #80) removes that hand-off:
`orch wait <bot> --autostart`, run as the bot's own user and login, claims
the bot's next job on the least-loaded device (issue #85), renders its standard brief and starts it
itself as `orch job run <bot> <job-id> --worktree -- <command>`, then goes
straight back to waiting. `orch job run` heartbeats before the harness starts
and records itself on the slot, so the job reads `running` within seconds of
the claim, with no model turn and no message to any bot. The harness posts its
`orch:v1` handoff and runs `orch job done` as the brief says.

- **Brief.** [`lib/brief/`](lib/brief/) renders one standard brief per
  job kind from the job row: implement (`build`, `plan`), fix, review, and
  merge-only (a `fix` whose detail has `"kind": "merge-only"`). It states the
  repo, PR or ticket, head, round and cap, risk, the section 8 review rules
  (the `factory-review-rules` contract), the `orch:v1` header the job ends
  with, and the `orch job done` lines. The repo's `factory.md` prose (Review,
  Ship, Context; `parse_factory_context` in
  [`lib/factory_md.py`](lib/factory_md.py)) is appended as "Additional
  context from factory.md", read with `gh api` from the repo's `factory_ref`
  in `orch.yml` (never a PR head) with every GitHub token variable unset. If
  it cannot be read, the brief says why and tells the harness to read it
  from the base branch itself. Job text (the brief, a CI log) only appears
  inside one fenced "Job data" block, marked as input, not instructions.
- **Command.** The harness command is the bot's own config,
  `~/.config/factory/waiter-<bot>.yml` (`--harness-config`): `bot` and
  `command`, an argv list run with no shell. The file must be a regular file
  owned by the bot's user and writable by no one else, in a directory owned
  by that user (or root) that is not group- or world-writable. It is opened
  once with `O_NOFOLLOW` and every check is made on that open file (`fstat`),
  so the file read is the file checked.
  [`config/waiter.example.yml`](config/waiter.example.yml) shows the shape.
  Nothing from a job row, a PR or a comment reaches an argv: the only job
  value in the `orch job run` argv is the job id, which must be a plain
  token. The brief goes to the harness on stdin and as
  `<state>/jobs/<job-id>/brief.md` (mode 600, path in `$ORCH_JOB_BRIEF`);
  `orch job run`'s output goes to `run.log` beside it. `--state-dir`
  defaults to `~/.local/state/factory/waiter-<bot>`.
- **Sandboxed Codex (issue #103).** `codex exec --sandbox workspace-write`
  lets the model's commands write only the job worktree, so the harness's own
  `orch job done` used to fail with "attempt to write a readonly database"
  and every Codex job ended failed (`job-failed-2`). On the box, `orch job
  run` ([`lib/codex_sandbox.py`](lib/codex_sandbox.py)) now adds
  `--add-dir <state dir>` right after `exec`, where the state dir is the
  directory of the resolved `--db`/`$ORCH_DB` (it holds `orch.db`, its
  `-wal`/`-shm` and the waiter lock; for a symlinked database, the real
  file's directory too). It does so only for a sandboxed `codex exec`:
  `argv[0]` named `codex`, the `exec` (or `e`) subcommand, and an effective
  workspace-write sandbox (`-s`/`--sandbox workspace-write`, `-c
  sandbox_mode=workspace-write`, or `--full-auto`; not with
  `--dangerously-bypass-approvals-and-sandbox`). Claude, Cursor, an
  unsandboxed or read-only Codex, and a wrapper such as `env codex` run
  exactly as configured. A directory already given with an absolute
  `--add-dir` is not added again, so a box-local `waiter-codex-bot.yml` that
  carries the manual stopgap (`--add-dir /home/box/.local/state/factory`, an
  absolute path) keeps working unchanged. The directory is added only if
  it exists. The sandboxed harness can then write anything in the state
  dir, the database included, as the bot's own user already can outside
  the sandbox.
- **One harness command for every device.** A bot's waiter config is
  shared by the box and its remote devices, so a codex review placed on the
  PC used to fail with exit 127 on the box path `/home/box/.local/bin/codex`.
  For a remote run, `orch job run` runs a codex command by its bare name
  `codex`, found on the device's own login PATH (`bash -lc`, as every placed
  command already runs), and takes each `--add-dir` out of the argv: the
  start script gives `--add-dir <dir>` back only if `test -d <dir>` holds on
  the device; a relative `<dir>` is tested under codex's `-C`/`--cd`
  directory, where codex resolves it. No box state dir is sent (the device
  has no `orch.db`; the waiter runs `orch job done` on the box), and no
  device path is made up.
  Claude and Cursor commands run exactly as configured.
- **Every device (issue #85).** The autostart waiter claims through the whole
  device pool, so the least-loaded device takes the job. On a remote device
  it runs `orch job run` without `--worktree` (the harness starts over the
  bot's own SSH alias, "Remote start"), and the brief says the working
  directory is `$ORCH_JOB_WORKDIR`. That device has no `orch.db`, so the
  harness ends its output with `ORCH-HANDOFF: <comment-id>` (or
  `ORCH-HANDOFF: failed`); the output returns over SSH into `run.log`, and
  when the run ends the waiter runs `orch job done` on the box: done with
  that id only when `orch job run` exited 0 and the marker is the last line
  of this run's output (`run.log` from where this run began; the runner's
  own `orch job run:` lines are skipped), else failed for T6 (a nonzero
  exit, or a last line that is missing, malformed or says failed). Once a
  remote worker was started, a failed run leaves the job running instead:
  the poller fails it when the device confirms the worker gone, so T6 never
  starts attempt 2 beside it. A job already finished by then is left alone. The waiter records each remote
  run in `<job-dir>/remote.json` and starts it with `orch job run
  --status-file <job-dir>/exit`, so a restarted waiter adopts the remote runs
  still running for its bot and finishes each once it ends (a runner gone
  with no status file: failed). The record goes once the job is finished,
  so no run is finished twice.
- **Failures.** A job with no standard brief (an action other than build,
  plan, fix or review), a job id that is not a plain token, or a start that
  fails is failed at once for T6 and its slot is freed. When the harness
  exits without `orch job done` (a missing binary, an expired login, a
  crash), `orch job run` fails the job for T6 before it frees the slot, and
  the waiter does the same for a run that died first, so the job never waits
  out its timeout. A waiter that dies between a claim and the start leaves a
  claimed job, which the poller requeues after `release_claimed_minutes`.
- **One handoff per job.** The claim records the waiter on the job's slot.
  From then on only that waiter's own `orch job run` may run the job, and
  only that run may `orch job done` it. The run proves it with a secret
  token (issue #96): `orch job run` mints one per run, records only its
  SHA-256 on the slot, and gives the harness the token only in
  `$ORCH_JOB_PROOF`, never on disk: the waiter and the bot's other sessions
  run as one Unix user, so a file could be found and presented by any of
  them. The name has no `KEY`, `SECRET` or `TOKEN` in it, so Codex's default
  `shell_environment_policy` passes it to the commands the harness runs.
  There is no process-tree check, so a harness sandbox with its own PID
  namespace (Codex) can finish its job, as long as it can write the orch
  database's directory. The known limit: a process of the same Unix user
  that reads the run's `/proc/<pid>/environ` can take the token; the check
  stops bot turns that were never handed it, not a hostile same-user
  process. A bot turn's `orch job
  run` or `orch job done` is refused (exit 2, nothing changed), and a plain
  `orch wait <bot>` is refused while the bot's autostart waiter runs. A
  second `orch job run` of a running job is refused. `orch job done` with the
  outcome already recorded is a no-op success; a different outcome is refused
  (exit 1) and changes nothing. The dispatcher records a verdict that repeats
  an already handled one (same author, head, round and verdict) as
  `duplicate-verdict` and takes no action for it: no job, no ship, no wake.
- **Restarts.** Each `orch job run` runs in its own session, so stopping or
  restarting a waiter never stops a running job. SIGTERM exits 0. The
  waiter holds `waiter-<bot>.lock` beside the database (with its pid) for its
  whole life, so a second waiter for the bot exits 2; a plain `orch wait`
  holds it shared.
  [`deploy/waiter-service.sh <bot>`](deploy/waiter-service.sh) runs the
  waiter under a supervisor loop (like the poller and Linear webhook
  services) with `start`, `stop`, `restart`, `status`, `ensure`,
  `install-autostart` and `remove-autostart`; a waiter that exits or is
  killed is started again after 5 seconds (doubling to 60 while it keeps
  exiting within 30 seconds). It reads `~/.config/factory/waiter.env` for
  `ORCH_DB` and other `ORCH_*` paths (default `ORCH_DB` is
  `~/.local/state/factory/orch.db`), logs to
  `~/.local/state/factory/waiter-<bot>.log`, and `stop` also stops a waiter
  left holding the lock by a supervisor that was SIGKILLed. Its autostart flag
  `waiter-autostart-<bot>` is picked up by the profile `deploy/install.sh`
  writes; with `crontab` it also adds `@reboot` and 5-minute `ensure` lines
  tagged `# factory-orch-waiter-<bot>`.
  [`deploy/orch-waiter@.service`](deploy/orch-waiter@.service) is the
  systemd user template unit (`orch-waiter@claude-bot`): `Restart=always`,
  `KillMode=process`. Nothing in this repository installs, enables or
  starts either.

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
locked or failed writes are logged and retried at the next beat. A heartbeat
error never aborts the command: it runs on, and if the job then times out, T6
retries it once (see "Job states"). SIGTERM and
SIGHUP are forwarded to the command, a Ctrl-C (SIGINT to the process group)
reaches the command directly, and the exit status is the command's; a signal
that arrives before the command starts means it never runs.
`orch job run` never finishes the job: `orch job done` stays the bot's call
(except that an autostart run fails a job its command left running; see
"Autostart waiter").
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

On the dashboard, a stale job whose recorded runner is gone while its worker
is not confirmed gone shows `runner gone, worker alive` in place of `stale
heartbeat`; the state, and what the poller does with it, stay `stale` (issue
#1211).

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
  unreachable_lost_minutes: 10
```

## Install after merge

Do not run these steps until Max gives the go. `deploy/install.sh` creates a
venv, installs `requirements.txt` (including PyYAML), writes an `orch` wrapper
under `$HOME/.local/bin`, initializes one shared database, and writes
`$HOME/.config/factory/profile.sh`. Without `--autostart` it does not install
or start the service; with `--autostart` it also runs `deploy/poller-service.sh
install-autostart`, which turns on autostart but does not start the poller,
and `deploy/linear-webhook-service.sh install-autostart` when `orch.yml` or
`orch.local.yml` has `linear_webhook`. It always installs self-heal (see
"Self-heal"). The generated `profile.sh` runs once per shell (a second
source is a no-op) and creates the state directory before any hook writes
a log there. Its hooks run only in the host PID namespace and outside
factory jobs (issue #308): `install.sh` records the host namespace in
`~/.config/factory/host-pidns` once and never replaces a record that
differs from its own namespace, and a shell whose `NSpid` has more than one
entry, whose namespace differs from that record, or that has `ORCH_JOB_PROOF`
or `CODEX_SANDBOX` set starts nothing. The poller, waiter, Linear webhook and
self-heal scripts refuse `start`, `stop`, `restart`, `ensure` and their
internal loops in a non-host namespace (exit 4), so a sandboxed shell never
writes its own pids into the host's pidfiles.

After a host or WSL restart that renumbers namespaces (issue #984), `start`,
`ensure`, `restart`, the profile hook and `install.sh --update` refresh a
stale record by themselves. They trust only the root-owned boot record
`/run/factory-host-pidns`, which WSL writes at each distro start, and refresh
only when it names their own namespace, `NSpid` has one entry, no job or
sandbox variable is set, and no process still runs in the recorded
namespace (else exit 4). To install the boot record on WSL, run once from a
host shell, then restart WSL (`wsl --shutdown` from Windows):

```sh
sudo deploy/wsl-boot.sh   # adds a [boot] command to /etc/wsl.conf
cat /run/factory-host-pidns; readlink /proc/self/ns/pid   # after the restart, the two match
```

If `/etc/wsl.conf` already has a `[boot] command`, the script changes
nothing and prints both commands; join them with `;` by hand. Without the
boot record (native Linux, or WSL before this step), a stale record still
refuses: delete it and rerun `deploy/install.sh --update` from a host shell.

It also retires the launchers that lived outside the repository (issue
#107): for `~/.local/share/factory/run-poller.sh` and, unless it is already
the installed copy of `deploy/linear-webhook-service.sh` (issue #134),
`~/.local/share/factory/linear-webhook-service.sh`, before installing that
copy, it runs the old listener script's `stop`, sends SIGTERM to each process whose argv runs
exactly that file (and to its direct children), removes the crontab lines
that name it and deletes it, printing each step. If the crontab cannot be
read it changes nothing and exits 1.
Stop `orch-poll` (or `deploy/poller-service.sh stop`) before re-running
`deploy/install.sh`; both install paths replace the venv.
Keep the checkout at
`$HOME/factory` on `main`; the service reads that checkout's `orch.yml`.
The script warns for another checkout or branch, refuses to replace a
non-factory `orch` wrapper, and safely recreates the venv on every run while
preserving the shared database. `requirements.txt` holds what the venv runs, pytest
included because this repo's `review.pre_ready` runs it under `$ORCH_PYTHON`;
`requirements-dev.txt` adds pytest-xdist for CI.
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

[`deploy/run-poller.sh`](deploy/run-poller.sh) is the one poller launcher
(issue #107): it sources `~/.config/factory/webhook.env` and
`~/.config/factory/orch-poll.env` with `set -a`, sets `PYTHONUNBUFFERED=1`,
and runs `orch poll`; redirect its output to `poller.log`. If either file
cannot be read it exits 78 with `run-poller: missing
~/.config/factory/<file>` and starts nothing, so no poller runs without its
environment. `orch poll` also line-buffers its own stdout and stderr, so
the log stays current. `poller-service.sh` and
[`deploy/orch-poll.service`](deploy/orch-poll.service), the systemd user
unit alternative, both start the poller only through it. On SIGTERM a
poller in the middle of a cycle finishes the cycle first, so no handoff or
ship is cut in half; between cycles, or on a second SIGTERM, it exits at
once. Either way it exits 143. The unit allows 300 seconds for that.
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

`orch poll` holds the database's single-poller lock (`orch.db.lock`, beside
the database file a symlinked `ORCH_DB` points to); while it
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
(argv `orch poll ...`, read from `/proc`, else `ps`) as not running. With no
live poller in that pidfile, it looks in `/proc` for the `orch poll` that has
this host's `orch.db.lock` open, so `stop` and `status` also find a poller
started by `orch-poll.service`, which writes `poller.pid` beside its own
`ORCH_DB`. `start`,
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

`start` and `ensure` also probe `orch.db.lock` with `flock` before
launching, beside the `ORCH_DB` that `run-poller.sh` ends up with after
sourcing its env files (`run-poller.sh --print-db`), else the caller's
`ORCH_DB`, with symlinks resolved as `orch poll` resolves them: while it is held, a poller runs even if its pid is
not visible from this shell, so they launch nothing (`start` says
`orch poll already running (poll lock held)`).
[`deploy/poller-ensure-loop.sh`](deploy/poller-ensure-loop.sh) runs
`poller-service.sh ensure` every `ORCH_POLL_ENSURE_SEC` seconds
(default 60). It holds `poller-ensure-loop.lock` for its whole life and exits
0 at once when another copy holds it. With `poller-autostart` on and no
`crontab` (issue #248), `ensure` starts it unless it already runs, and logs
`crontab not found` once per state directory; it can also be started by hand.
A loop that `ensure` started exits at its next pass once `remove-autostart`
has removed `poller-autostart`, and that pass launches nothing; a loop started
by hand keeps running.

Every `ensure`, held stopped or not, also runs `waiter-service.sh <bot>
ensure` for each `~/.config/factory/waiter-autostart-<bot>` (logging to
`waiter-<bot>-service.log`), so whatever keeps the poller alive keeps the
autostarted waiters alive too, even with a `profile.sh` older than #80.

Before restoring a missing `orch.db`, `start` and `ensure` wait for a box
copy-in still restoring files: while `ORCH_COPY_IN_STATUS` (default
`/tmp/sand-copy-in-status.json`) exists and its `"phase"` is not `"done"`,
they check every `ORCH_COPY_IN_POLL_SEC` (default 5) for up to
`ORCH_COPY_IN_WAIT` seconds (default 600). An `orch.db` the copy-in brings
back is kept; after the wait runs out the restore goes ahead.

### Move the orch host

`orch.db` must have one writer at a time.
[`deploy/orch-move.sh`](deploy/orch-move.sh) moves it to another host, and
moves it back. Run it from the factory checkout, as the bot user, on each host.
Before a move, re-run `deploy/install.sh` on both hosts, so the installed
`~/.local/share/factory/linear-webhook-service.sh` carries the fence
described below.

Cutover from the box to the PC:

```sh
# on the box: prints the export directory
deploy/orch-move.sh export --to pc
# on the PC
deploy/orch-move.sh import box:<export>
```

`export` writes `waiter-<bot>.drain` for the three bots, so no waiter claims a
new job. A self-heal pass (the loop, the timer or `kick`)
does nothing while any `waiter-<bot>.drain` reads `orch-move`. Before the
drain starts, `export` runs `self-heal.sh wait-idle` once, so a pass already
under way finishes first. That pass may have removed the drain files, so
`export` writes them again while `wait-idle` still holds
`self-heal-work.lock`, and every later pass finds them. Every `ORCH_MOVE_POLL_SEC` seconds (default 30) it
writes the drain files again, copies `orch.db` with `orch backup-db` and asks
`orch move-db drained` whether a job still runs or a device slot is still
held. Once none is, it writes
`~/.config/factory/orch-retired`. Only then does it stop the poller, the
waiters, self-heal and the Linear listener through their service scripts.
While `orch-retired` exists, the poller, waiter, self-heal and Linear service
scripts refuse to start (exit 79), and so does the `self-heal.sh once` pass
the systemd timer runs. `orch poll` and `orch wait` themselves exit 79 on a
retired host. After stopping self-heal, `export` runs `self-heal.sh
wait-idle`, which waits up to `ORCH_HEAL_IDLE_WAIT` seconds (default 900) for
a self-heal pass already under way, and the device deploy it started
detached, to finish before `export` takes its copy. A wait that runs out
fails the export like a failed stop. `poller-service.sh stop` finds the
poller by the `orch.db.lock` it holds open when its pidfile is not
`~/.local/state/factory/poller.pid`, as with `orch-poll.service`. Before the
copy, `orch move-db unlocked` checks that no process still holds
`orch.db.lock`, the lock every `orch poll` on that database holds, or a
`waiter-<bot>.lock` beside `orch.db`, the lock every `orch wait` holds, and
fails the export if one does. The export
directory (default `~/.local/share/factory/orch-move/<UTC time>`) holds
`orch.db`, `manifest.json` (sha256, row count per table, integrity, the
retired line) and `machines.<source>.yml`, the source host's `machines.yml`.
It also holds `job-detail/`, the per-job detail files #653 keeps beside
`orch.db`. The manifest records each file's sha256. `orch move-db manifest`
refuses a copy with a spilled job row whose file is missing.

`orch poll` and `orch wait` check `orch-retired` again once they hold their
lock, and exit 79 if it exists. `export` writes `orch-retired` before it
checks those locks. A poller or waiter that took its lock before then still
holds it, so `export` fails and writes no manifest. One that takes its lock
after sees `orch-retired` and exits (#775). A successful export therefore
leaves no `orch poll` or `orch wait` on that database able to poll or claim.

Before `import`, you can confirm this on the source host by hand. Check that
`~/.config/factory/orch-retired` exists and that no `orch poll` or `orch wait`
process runs:

```sh
# on the source host: ls lists the file, pgrep prints nothing
ls ~/.config/factory/orch-retired
pgrep -fl 'orch (poll|wait)'
```

If either check fails, do not import that export. Stop what runs with
`deploy/poller-service.sh stop` or `deploy/waiter-service.sh <bot> stop`,
remove `orch-retired` (`export` refuses on a retired host), and run `export`
again.

`import` copies the export into `~/.local/share/factory/orch-move/import-<UTC
time>` and checks it with `orch move-db check`. It refuses when the copy's
sha256 differs from the manifest, the copy fails `integrity_check`, or the
manifest has no retired line. It refuses when a job-detail file the manifest
names is missing or changed, or a spilled job's file is missing. For a
`host:path` export it also refuses when that host has no `orch-retired`. It
refuses when this host already has an `orch.db`, or its `-wal` or
`-journal`. Both commands find `orch.db` with
`deploy/run-poller.sh --print-db`, which still answers while `orch-retired`
exists, so `import` places `orch.db` at the `ORCH_DB` that
`~/.config/factory/orch-poll.env` gives the poller it then starts. When
`--print-db` fails, for example on a missing env file, they refuse. A refusal
places nothing and starts nothing. Otherwise it adds `machines.<dest>.yml`,
this host's `machines.yml`, so both hosts' `machines.yml` sit beside the
snapshot. It merges `job-detail/` into the one beside `orch.db`, and refuses,
placing nothing, when a file of the same name there differs from the export's
copy by more than keys the export's copy adds (`spill_cold` grows a finished
job's file by adding keys, so a rollback carries the grown file). Then it places
`orch.db` with `cp` and `mv`. It never uses `orch restore-db`, which would
cancel the queued jobs. It removes this host's `orch-retired`, then starts
`poller-service.sh` first, then the waiter of each bot with
`~/.config/factory/waiter-autostart-<bot>`, then self-heal and the Linear
listener.

On the new host, install autostart for what must survive a reboot:
`poller-service.sh install-autostart`, `waiter-service.sh <bot>
install-autostart`, `self-heal.sh install-autostart` and
`~/.local/share/factory/linear-webhook-service.sh install-autostart`. On the
retired host their autostart refuses to start anything (exit 79). Verify with
`poller-service.sh status`, `bin/orch status --db "$ORCH_DB"` and the row
counts `import` printed against `manifest.json`.

Rollback is the same tool reversed. On the PC, run
`deploy/orch-move.sh export --to box`. On the box, move the pre-cutover
`orch.db` aside (`import` never replaces a database), then run
`deploy/orch-move.sh import pc:<export>`. That removes the box's
`orch-retired`. Then put the box checkout's pre-cutover `machines.yml` back
from `machines.<source>.yml` in the cutover export.

An export that does not drain within `ORCH_MOVE_DRAIN_TIMEOUT` seconds
(default 9000, 150 minutes) exits 75. Self-heal is never stopped before
`orch-retired` is written. So on exit 75 (drain timeout), exit 1 (a failed
probe or idle wait) or Ctrl-C, `export` removes the drain files and their
acks and stops nothing. The host keeps running, and
self-heal resumes on its next pass by itself. An export killed with SIGKILL
leaves the drain files, and self-heal stays paused until someone removes
`~/.local/state/factory/waiter-*.drain`. A failure after
`orch-retired` is written leaves the host retired and stopped, which is the
safe state. To undo it, remove `~/.config/factory/orch-retired` and start the
services with `start`. `ORCH_MOVE_HOST` names this host (default
`hostname`). `ORCH_MOVE_SCP` and `ORCH_MOVE_SSH` name the copy and remote
commands (default `scp` and `ssh`). `ORCH_DB_BACKUP_EVERY` (see *State loss*
above) spaces out only the cycle backup. `export` takes its own copies.

## Status

```sh
bin/orch status --db "$ORCH_DB"
```

`orch status` prints a read-only summary of queued jobs, devices with one
slot count each and their running jobs, and paused tracker items. It opens the dispatcher database read-only and does
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
runner died shows as `stale` until the poller confirms it dead.
A `Needs attention` section lists tickets the poller flagged (a ship that
failed and will not be retried, a job dedupe dropped: issues #83 and #84)
until the ticket is merged, handed off or done. Once a backlog feeder has run,
a `Feeder — eligible N, fed N, waiting for capacity N` section lists each
ticket eligible at its last poll with its state and PR. Each device has one
slot count (issue #90): a running harness job that holds no slot (claimed
with no device by an older `orch wait`, or whose worker already ended) runs
on the orch host (`orch_host` in `machines.yml`; the PC in the shipped file),
so it is listed under that device, marked `no-placement`, and counted in its
slots; the device pool counts it too, so the orch host never starts more than
`slot_max`. A running grok wake holds no slot and is listed
under `Grok wakes`.
Once a Linear webhook listener or a webhook-mode poller has run on the
database (the poller records `linear_webhook_polling`), a `Linear webhook`
section shows the last delivery (or `never` and the listener's age, or
`never (listener has not started)`) and the dirty-queue depth, plus a
`warning:` when the poller found a missed delivery in the last 24 hours, or
else a `note:` after more than an hour with no delivery while any tracked
Linear ticket is not merged, handed off, done or abandoned (see "Linear
webhook"). A running job's age counts from its claim (its device slot's
`placed_at`, else when it was queued), followed by its last heartbeat and,
for a job that holds a slot, the account it runs on (issue #1251):
`aut0-code/aut0#12 build -> claude running 42m (beat 12s ago) account claude-max-1`.
The last section is `Health` (next section), and a queued job that no live
waiter can take ends with `[no waiter for <harness>]`.

## Health

```sh
bin/orch health --db "$ORCH_DB"            # exit 3 when something needs attention
bin/orch health --db "$ORCH_DB" --json     # what the hourly momentum check reads
bin/orch health forget waiter --db "$ORCH_DB" --bot cursor-bot
```

Every long-running part of the factory writes a heartbeat row every 15
seconds (issue #92; `lib/health.py`): `orch poll` (`poller`, not with
`--once`), each bot's `orch wait` (`waiter`, one row per device it claims
on), `orch linear-webhook serve` (`linear-listener`), and the dashboard
writer (`dashboard`, once per `orch dashboard push` snapshot, at the push
interval or `every_minutes`, up to a day). The poller also probes each
enabled, non-placeholder remote device in `machines.yml` over SSH every 60
seconds (`device-probe`, through the device's first bot alias, `ssh -o
BatchMode=yes ... exit 0`), each device in its own thread so one that hangs
never delays another; only a device that answered counts as seen. `orch
poll` also records whether each dashboard push it started succeeded
(`dashboard-push`, issue #106): stale after one failed push ("push
failing, last pushed … ago"), down once no push has succeeded for 8 push
intervals. It is listed once a push has finished. The
listener checks its tunnel (`tunnel`) every 15 seconds by a GET of
`linear_webhook.public_url`, its Funnel URL, when that is set: only the listener's own 405
for its path counts as seen, so a Funnel that is off, a gateway error or a
dead listener is a failed check. From the box that GET reaches the listener
through Tailscale's proxy for the Funnel.

```yaml
linear_webhook:
  public_url: https://box.tail68624b.ts.net/linear-webhook  # https, path = path
```

A row is `(component, device, bot, pid, started_at, last_seen, version,
interval_seconds, stopped_at, checked_at)`, one per process (the pid is in
the key, so one `orch wait` exiting never removes another's row), where
version is the commit the process loaded when it started (12 hex digits,
read from `.git` at import) or `unknown`. Each value must be an allowlisted name, a device `machines.yml`
lists, a number or that SHA, so nothing else can be written. A beat also
removes the rows other processes left for the same component and bot once
they are down. Rows live in `orch.health.db` beside `orch.db` (created with
`orch.db`'s permission bits and group), so the heartbeats never wake the
live dashboard push, which watches `orch.db`. Every open waits at most half
a second for a lock: a beat that cannot be written logs one line naming the
exception class and never stops or stalls its component (the dashboard push
logs it and goes on), and an `orch.health.db` that cannot be read is an
`unknown` Health section, a needs-attention item and exit 1 from `orch
health`.

A component is **up** while its last beat is at most 3 intervals old,
**stale** up to 8 intervals, then **down** (45 seconds and 2 minutes at the
15-second beat; 3 and 8 minutes for a device probe). A clean exit marks an
autostart waiter, the poller, the listener and its tunnel stopped, which is
down at once; a plain `orch wait` removes its rows when it exits, since it
exits by design after a claim. A device or tunnel whose latest check failed
is at least stale. Each (component, device, bot) shows once, as the best
of its processes' rows; a device probe or tunnel check shows its newest
check, whichever process made it. Once any heartbeat exists, every expected component
with no row is shown down (`never seen`, or `never reached` for a probe):
the poller; each bot's waiter on some device `machines.yml` makes eligible
for its harness, unless `orch.yml` lists that harness in
`refused_harnesses` (issue #107; none here), and each probed device;
the Linear listener when `orch.yml` has `linear_webhook`, and its tunnel
when that sets `public_url`; and the dashboard writer when
`dashboard.push.transport` is `ssh`. A row nothing expects (a retired bot or
device) is listed as `not configured` and needs no attention. A queued
harness job is flagged **no waiter for <harness>** when no up or stale
waiter for its bot runs on a device `machines.yml` makes eligible for the
harness. `orch health forget` removes a retired component's rows (a disabled
device's probe row goes by itself). A pushed snapshot carries how often its
writer publishes (`health.publish_seconds`: the push loop's interval,
`every_minutes`, or the poll cycle when the push is live or unscheduled),
and the page keeps every state the snapshot observed until 3 of those
intervals pass. After that the page cannot see the components: one past its
own thresholds shows **unknown**, not down, and the "every queued job has a
live waiter" line becomes unknown too. Only the dashboard writer's own row
ages on to stale and down, so a publisher that died is one needs-attention
item. A one-off snapshot (`orch dashboard snapshot`) vouches for nothing:
its components turn unknown on their own thresholds, and the old snapshot is
one needs-attention item.

**Stale code** (issue #107): a poller, waiter or Linear listener whose
heartbeat names a commit other than the checkout's current one is still
running old code. `orch health`, `orch status` and the dashboard's Health
tab mark it `stale code (running <commit>, checkout <commit>)`, and the
header counts it (`Health (9 components, 0 down, 1 stale code)`). It is not
down and needs no attention: self-heal restarts it. `orch health --json`
adds `code_version`, and `stale_code` and `started_at` for each component.

**Needs attention** (issue #581): a tunnel down longer than
`linear_webhook.tunnel_alert_minutes` (default 30) is a `tunnel-down` item.
The tunnel is down since it was last seen, or, if it was never seen, since
the first listener that checked it started, so a listener restart does not
reset the clock; while its port is held, since the held record was written
if that is earlier. The item is worked out again on every read and is not stored, so it
stays while the tunnel is down and is gone on the first read that finds it
up. `orch health` prints `  needs attention: tunnel @ box down 22h (port held
by another owner: 8787 on pc-wsl-cursor-bot)` and exits 3. `orch health
--json` lists it in `needs_attention` (`kind`, `label`, `reason`,
`down_since_et`, `down_seconds`, `anchor`) and counts it in `attention`.
`needs_attention` is the field the hourly momentum check surfaces. The item
changes no component's state or reason, so self-heal still skips a held
tunnel after its one alert.

A job time that is not epoch seconds is a `bad-job-time` item (issue #1787).
orch.db refuses such a value since #1786, but one stored before that is still
there. `orch health` reads them from orch.db and lists them. It never prints
the stored value. Its text ends with a count line,
`  needs attention: 3 job times are not epoch seconds`, then one line per
value, such as
`  needs attention: job 2b81e0489a1349e78012ee59ef76692c finished_at holds text`.
The kind is `text`, `blob`, `infinity` or `null`. It lists the first 20 by job
id and then column, and the count line adds ` (first 20 listed)` when there
are more. `orch health --json` adds one entry per listed value to
`needs_attention`, after the `tunnel-down` entries, as
`{"kind": "bad-job-time", "job": <id>, "column": <column>, "stored": <kind>}`,
and puts the full count in `bad_job_times`. Any such value makes
`orch health` exit 3. Nothing is migrated. A person repairs the row.

## Self-heal

```sh
deploy/self-heal.sh once      # one pass, what the timer runs
cat ~/.local/state/factory/self-heal.log
```

Self-heal (issue #107) restarts factory components that are down or run
stale code, with no LLM and no tokens. Every 2 minutes `deploy/self-heal.sh
once` pipes `orch health --json` into `orch heal --health-json -`
(`lib/heal.py`). For each component on this device, it acts only when this
account owns the component's service, which is marked by an autostart flag
in `~/.config/factory`: `poller-autostart`, `waiter-autostart-<bot>`,
`linear-webhook-autostart` or `signin-broker-autostart`. A waiter runs as its own bot user, so each
account heals only its own services. It restarts through the component's own
service script and never sends a signal itself:

- a down or stale-code poller: `deploy/poller-service.sh restart`, which
  starts it through `run-poller.sh`. The stop waits up to 300 seconds for
  the poller's cycle to finish.
- a waiter: `deploy/waiter-service.sh <bot> restart`. This stops only that
  bot's supervisor and its `orch wait --autostart`. Each job's `orch job
  run` has its own session and keeps running.
- the Linear listener or its tunnel: `deploy/linear-webhook-service.sh
  restart`. For stale code it waits until no delivery is in flight.
- the sign-in broker (`orch accounts signin-broker`):
  `deploy/signin-broker-service.sh restart`. A down broker restarts at once.
  For stale code it waits until no `signout_locks` row is live, so no
  sign-in or sign-out is cut off mid-attempt. The script keeps one broker per
  state directory: the broker holds `signin-broker.lock` for its whole life.
  `install-autostart` writes `signin-broker-autostart`, and the generated
  `profile.sh` then runs `ensure` in each bot shell.

The rules for each component:

- A component held by its `*.stopped` marker is skipped. Self-heal never
  overrides an operator's stop. A `restart` whose stop times out removes
  the marker its own stop wrote, so the next pass retries and, after 3
  failures, escalates.
- A component restarted less than a minute ago is left to come up.
- A restart that moves a running component onto a new checkout commit is a
  version update. It succeeds when the restart command exits 0. It is free
  only once the component was seen running the commit of its previous
  update, so components keep up with any merge rate. Otherwise it counts
  toward the restart budget. A component that was waiting (draining, busy,
  settling) restarts once onto the current checkout, not onto each commit
  it missed.
- The restart budget is 3 restarts in 30 minutes. It counts down
  restarts, failed restart commands, and version updates made before the
  previous one was seen running. Once the budget is spent, the component
  is skipped until the oldest counted restart leaves the window. The
  Linear listener and its tunnel share one restart command, so they share
  a spent budget: while either is in backoff, neither restarts.
- Self-heal escalates once per component per 30 minutes: when that backoff
  trips, when the restart command fails 3 times, or when the component
  flaps (3 process changes in the window that self-heal did not cause).
  Escalating means POSTing a short JSON summary (`"action": "self-heal",
  "kind": "escalation"`, with the components, their states and reasons) to
  the Factory webhook named by `orch.yml` `grok.webhook_url_env`, with the
  same auth as Grok delivery.

The URL and auth values come only from the environment: `self-heal.sh`
reads `webhook.env` when this user can read it, and nothing logs a value.
A bot user without `webhook.env` logs `escalation skipped:
missing-env:FACTORY_WEBHOOK_URL`, and the box user's run escalates for the
box. The service scripts run without the webhook and Linear key variables.

Each restart and escalation is one timestamped line in
`~/.local/state/factory/self-heal.log`, with the exit code and duration, for
example `2026-11-13T14:02:00Z self-heal: restart waiter codex-bot @ box
(down: no heartbeat for 3m) exit=0 2.1s`. A skip or a deferral is logged
only when its decision changes. A service script's own output goes to that
service's `*-service.log`. State is kept in `self-heal.state.json`, written
atomically; a corrupt file is logged and started fresh. `orch heal
--dry-run` logs what it would do and runs nothing.

`deploy/install.sh` runs `deploy/self-heal.sh install-autostart`. Where a
systemd user session exists (`systemctl --user show-environment` works), it
installs and enables
[`factory-self-heal.timer`](deploy/factory-self-heal.timer) and its oneshot
[service](deploy/factory-self-heal.service), which has `KillMode=process`
so the services it restarts outlive the run. Otherwise it writes
`~/.config/factory/self-heal-autostart`, so `profile.sh` starts the loop
(`self-heal.sh ensure`, every `ORCH_HEAL_SECONDS`, default 120), and adds an
`@reboot` line and a 5-minute `ensure` watchdog tagged `# factory-self-heal`.
`self-heal.sh` also has `start`, `stop` (held by `self-heal.stopped`),
`restart`, `status`, `ensure` (silent while running), `wait-idle` and
`remove-autostart`. Passes never overlap: `self-heal.lock` makes a second
pass exit at once. Each pass and the device deploy it starts detached hold
`self-heal-work.lock` shared. `wait-idle` waits up to `ORCH_HEAL_IDLE_WAIT`
seconds (default 900) until none does, and exits 1 if one still does.
`wait-idle <command>` then runs the command before releasing the lock, so no
pass starts until it returns, and exits with its status. On a
host retired as the orch host (`~/.config/factory/orch-retired`, see *Move
the orch host*), `once`, `kick`, `start`, `restart` and `ensure` exit 79 and
run nothing. `factory-self-heal.service` counts that exit 79 as success
(`SuccessExitStatus=79`), so the timer's unit is not marked failed there. A
pass skips, exiting 0, while an orch-move export drains (see *Move the orch
host*).

### Self-deploy

Merged code deploys itself (issue #219). Each `self-heal.sh once` first runs
`orch heal deploy` (`lib/self_deploy.py`), and the poller runs
`self-heal.sh kick` after a cycle that merged a PR of the factory's own
repo (a `--no-block` start of the systemd service, or a detached pass).
Only the account that owns the checkout pulls:

- `git fetch origin main`, then a fast-forward only. Local edits to
  `machines.yml`, `orch.yml` and `deploy/install.sh` are kept: each is
  backed up under `~/.local/state/factory/self-deploy/<time>/` with the
  `git diff HEAD` patch, and one upstream also changed is 3-way merged
  before anything is touched. `orch.local.yml` and `machines.local.yml`
  are untracked, so a pull never touches them; moving the box's values
  there makes every pull conflict-free.
- The checkout is left as it was, logged, and escalated once per reason
  when a kept file conflicts, the checkout is not on `main`, has local
  commits, staged changes, an operation in progress, or edits to any other
  tracked file. A failure while applying resets to the old HEAD and
  restores the backups. A kept file that is a directory or symlink (or
  sits under a symlinked directory), or any local path, tracked or not,
  where upstream adds a file, also leaves the checkout unchanged, so a
  rollback never deletes local data. So does an untracked or ignored file
  inside a directory upstream turns into a file or symlink, and a kept
  file with local edits that upstream turns (or a directory above it)
  into a symlink, so the merged edit is never written through it. So does
  a tracked file whose local state `git diff` would not show (an
  assume-unchanged or skip-worktree index flag, or bytes on disk that do
  not hash to the index entry, as with a stale fsmonitor), so the backup
  always holds every local edit a rollback would reset. A fetch failing for 30 minutes escalates; its log
  line holds only the exit code.
- A kept file whose local edits conflict with upstream holds the box at
  its commit (issue #610): a `held` box row in `device_sync` lets jobs
  keep starting on the box and on remotes at the box's commit, and one
  `deploy-conflict` needs-attention row (ticket `self-deploy`) names the
  file. Both go away on the next pass that reaches `origin/main`. Box-local
  values belong in `orch.local.yml` and `machines.local.yml`; `orch
  check-config` warns about uncommitted edits to a tracked `orch.yml` or
  `machines.yml`, and `orch check-config --digest` prints a hash of each
  effective config, so you can check that a move changed nothing.
- When the merge changed `deploy/` or `requirements.txt` it runs
  `deploy/install.sh --update`, which installs requirements into the
  existing venv instead of recreating it (`self-deploy-install.log`).
- The heal pass that follows runs as a new process, so the poller and
  waiters on the old commit show stale code and are restarted in the same
  pass: the poller through `poller-service.sh restart`, a waiter through
  `waiter-service.sh <bot> restart`. A stale waiter is drained first:
  `waiter-<bot>.drain` makes it claim nothing, and it confirms in
  `waiter-<bot>.drain.ack`. Once confirmed, the drain holds for any
  waiter the service respawns in its place, even one on the new commit,
  until the restart or a later heal pass removes the marker. It is
  restarted only after that confirmation, with no job running: an idle waiter within the same pass, a busy one once
  its last job ends. A waiter started before #219 never confirms, so it
  stays drained and escalates after 2 hours; restart it by hand once. A
  running `orch job run` is never stopped.

Each deploy, refusal and escalation is a `self-deploy:` line in
`self-heal.log`; a repeated outcome is logged once. State is
`self-deploy.state.json`.

## Dashboard

```sh
bin/orch dashboard snapshot --db "$ORCH_DB" --out "$HOME/.local/state/factory/dashboard"
bin/orch dashboard push --db "$ORCH_DB"
```

The box cannot take inbound tailnet connections, so there is no live
server on it. `orch dashboard snapshot` opens the dispatcher database
read-only (a SQLite `mode=ro` URI with `query_only`; a missing file is
refused, never created), reads the last 50 lines of `poller.log`, and
writes `status.json`, five self-contained pages (`index.html`,
`job.html`, `activity.html`, `efficiency.html`, `health.html`) and
`tickets.json` to `--out`.
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

`tickets.json` holds one detail record per tracked ticket, keyed
`<repo>#<ticket_id>` from the stored identifiers, redacted but never cut
short (issue #530). A record exists for a shipped or stuck
ticket with no job too. It has the ticket's state, time in state, round,
hops, off-chart count and its visits in order. A shipped ticket's record
says when and how it shipped, the time from intake to ship, its revise
rounds and its job, token and cost totals (null when nothing reported
them). A stopped ticket's record names the stop state, when it stopped, the
state it stopped in, the recorded reason and the chart's ways out. A
ticket's running and queued jobs ride along as their `status.json` rows.
Every string goes through the same redaction as `status.json`, and every
`<`, `>` and `&` is written as a JSON escape. `status.json` itself does not
change. The full shape is in `lib/dashboard.py`'s module docstring.

`status.json`'s `projects` lists each `orch.yml` project in order with its
tracker, ship mode (`merge`, `handoff`, or `after` plus the `approver`) and
round caps (`rounds.low`, `rounds.high`), as the poller last read its
`factory.md` (issue #152). The dashboard has no copy of any `factory.md`:
each poll cycle the dispatcher stores what `LivePollSource` parsed in the
`project_rules` table (`lib/project_rules.py`), and the snapshot reads it
back. A `factory.md` the poller could not read, or has not read yet, is
`status: unknown` with the `reason`, never a default.

`status.json`'s `usage` backs the Efficiency page's Subscription headroom
(issue #151). It has one entry per usage account (see Usage accounts
below): each account a harness uses on an enabled `machines.yml` device,
then each declared account no device uses, such as an `external` one. Each entry gives the 5-hour and 7-day
window's `used` fraction, `resets_at` and `observed_at`, plus `headroom`
(the least `1 - used` over fresh windows). Readings come only from the
CLI's own output, captured by `orch job run` for every job and stored in
`usage_readings` on the existing telemetry flush:

- **Claude:** at each job's start, `orch job run` sends Claude Code's SDK
  control request `get_usage` alone to `claude -p --input-format
  stream-json` (on the job's device, as its bot; no model turn) and keeps
  `rate_limits.five_hour` and `seven_day` (`lib/usage_probe.py`). A login
  without the profile scope answers `rate_limits_available: false`, and a
  window the reply leaves null, so both read `no reading`.
- **Codex:** `orch job run` adds `--json` to codex-bot's command, a wrapper
  included (`<wrapper> exec ...`), and keeps the `thread.started` thread id.
  It then reads only the `rate_limits` of the last `token_count` event in
  that job's own rollout, `$CODEX_HOME/sessions/.../rollout-*-<thread
  id>.jsonl` (`lib/codex_rollout.py`): on each flush for a box job, and
  once over SSH when a remote job ends. Windows are matched by
  `window_minutes` (300, 10080). A reading is dated by its event's own
  timestamp, so rereading an old event never makes it fresh. A newer
  `token_count` whose `rate_limits` is null or missing, or that leaves a
  window out, reads that window as `no reading`.
- **Gemini:** at a gemini job's start, `orch job run` runs `agy -p /usage
  --output-format json` (on the job's device, as gemini-bot, with the same
  Google/Gemini overrides the account doctor unsets; agy answers the slash
  command with no model turn). Only the `Gemini Models` group is read: its
  `5h` and `weekly` buckets are `five_hour` and `seven_day`, and `used` is
  `1 - remaining_fraction`. A bucket whose `reset_time` is missing, not an
  ISO time with its zone, or not within 30 days of now (the unused
  third-party pool reports `1970-01-01T00:00:00Z`) reads that window as
  `no reading`, as Claude and Codex do. A failed or empty probe logs one
  line and never fails the job.

A window that a reply with other limits leaves null or out, in 3 fresh
readings in a row for the account on any of its devices (none more than 24
hours after the one before, and a Codex event read twice counting once),
reads `not on plan`, shown as "not on this plan": that plan has no such limit, as
with a Codex plan that reports only its 7-day window. Its `used` stays null,
and the other window alone is then the account's headroom.

A newer `no reading` replaces older numbers. A window that has reset, or a
reading older than 24 hours, is `stale` and its `used` is null. A Cursor
account shows the reading `orch usage record` stored on it (source `usage
record`); with none it shows `not tracked: <reason>` on its own muted row
with its plan, jobs and devices: the Cursor CLI reports no usage without
reading its login token. A database without the table shows `no telemetry`.

Each row also shows the account's jobs (`2 running · max 3`, or `no max`)
and its devices (`signed in on pc, box · running on pc`), from the same read
placement uses (`account_loads` in `lib/device_pool.py`, issue #1251).

### Usage accounts

Each subscription is its own usage account (issue #772), with its own
readings, its own pause and its own Efficiency row. Declare accounts in
`machines.yml` and list the devices each one is signed in on (issue #1063):

```yaml
accounts:
  - {id: claude-max-1, harness: claude, plan: max, label: "Claude Max", signed_in_on: [pc, box]}
  - {id: claude-max-2, harness: claude, plan: max, signed_in_on: [pc], max_jobs: 2}
  - {id: grok-ultra, harness: external, plan: ultra}
devices:
  - name: pc
    home: /home/{bot}
    harnesses:
      claude: claude-bot
      codex: codex-bot
  - name: box
    home: /home/box
    ssh_user: box
    harnesses:
      claude: claude-bot
```

An `id` is a stable, non-secret name: letters, digits, `.`, `_` and `-`,
not `.` or `..`, never an email, org ID or token. `harness` is `claude`,
`codex`, `cursor` or `external`. A bare bot user, as in `codex: codex-bot`, is the account
`<harness>-default`: the CLI's own login on that device, signed in on every
device that lists the harness. It needs no entry, so a file without
`accounts:` loads unchanged.

`signed_in_on` lists the devices where Max signed the account in. Each must
be a listed device that lists the account's harness, and must name a
`home`: the bot's home folder on that device, an absolute path where
`{bot}` is the bot user (refused with `ssh_user`, since every bot then logs
in as that one user). The account's folder on that device is
`<home>/.factory-accounts/<id>`. `machines.yml` refuses a declared Claude or
Codex account no device lists, `signed_in_on` on an `external` or Cursor
account, and a device named twice for one account (twice in `signed_in_on`,
or there and as the device's own login). `max_jobs`, a positive
integer, caps the account's running jobs across every device. Placement
(see "Device pool") gives an account at its cap no new job.
`paused` (true or false, default false) gives the account no new job while
its running jobs finish. A pause or a lower `max_jobs` reaches placement at
once, and a resume or a higher `max_jobs` once the poller adopts the file.

Cursor accounts (issue #1411). Cursor has no folder setting for its login.
`CURSOR_CONFIG_DIR` moves only `cli-config.json`; the login stays in
`~/.config/cursor/auth.json` on Linux and in the `cursor-user`,
`cursor-access-token` and `cursor-refresh-token` keychain items on macOS. So
a Cursor account never runs from a folder: each device's `cursor-bot` login
is the account, named inline on every device that runs it, as in `cursor:
{bot: cursor-bot, account: cursor-ultra}`. That inline form is not
deprecated for Cursor. `machines.yml` refuses `signed_in_on` on a Cursor
account and a declared Cursor account that no device names as its login.
`orch accounts doctor` runs `cursor-agent status --format json` as
`cursor-bot` on each of those devices with `CURSOR_API_KEY`,
`CURSOR_AUTH_TOKEN` and `CURSOR_CONFIG_DIR` unset, and keeps only the status
and a 12-character hash of the user id the reply names, never the email.
Settings cannot sign a device login out. No probe reads Cursor usage: the
CLI shows its usage meters only in the interactive `/usage` view, and no
documented non-interactive command or API reports used and reset without
reading the login's credentials. Cursor's weekly used and reset are
recorded on the Cursor account itself (`orch usage record --account
cursor-ultra`), and Efficiency and Settings show that reading on the
account's row; there is no pooled row. A Cursor account with no reading
shows `not tracked` and the reason. Max allowed Cursor as a build and
review harness, so `orch.yml` no longer lists it in `refused_harnesses`
(`docs/decisions/1411-cursor-accounts.md`).

Account homes (issues #1183 and #1184). One bot user on one device can run
several accounts of its harness, each from its own folder. A job placed on
an account that is not the device's own login runs with `CLAUDE_CONFIG_DIR`
(Claude) or `CODEX_HOME` (Codex) set to `<home>/.factory-accounts/<id>`.
Its token variables are dropped by name after the login profile runs:
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
for Claude, and `OPENAI_API_KEY` and `CODEX_API_KEY` for Codex. The harness
then signs in from the folder, never from a variable, and the values never
reach the run log, the telemetry or the job detail. The job's usage probe and
`skill-probe run --account <id>` run the same way, and the skill probe reads
the run's transcript from that folder. A job on the device's own login runs
unchanged. `lib/harness_env.py` `ACCOUNT_ISOLATION` is the one table that
names these variables.
A Gemini account has no folder (issue #1641). Only a different `HOME` moves
the Antigravity `agy` login, so a Gemini account is each device's
`gemini-bot` login, named inline as a Cursor account is
(`gemini: {bot: gemini-bot, account: <id>}`). `orch accounts doctor` checks
it with `agy models`, and the sign-in is interactive `agy`. Both run with
`GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_CLOUD_ACCESS_TOKEN`,
`GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_GENAI_USE_GCA`,
`GOOGLE_GENAI_USE_VERTEXAI`, `GEMINI_CLI_USE_COMPUTE_ADC`, `CLOUD_SHELL`,
`GOOGLE_GEMINI_BASE_URL`, `GEMINI_CLI_CUSTOM_HEADERS` and `GEMINI_CLI_HOME`
unset, so no key in the login profile can stand in for the login.
Its usage is read with `agy -p /usage --output-format json` under the same
rule at each gemini job's start (Dashboard, above).
To sign an account in on each device, follow [docs/ACCOUNTS.md](docs/ACCOUNTS.md).

The Accounts panel on the Settings page (issue #1066) adds and removes
accounts, pauses and resumes them, and sets `max_jobs` and `signed_in_on`,
with no hand edit of `machines.yml`. It also runs `orch accounts prepare`
and the doctor for one account on one device. Remove needs the account
paused with no running job, and keeps its folder. Default and external
accounts are read-only. "Manage accounts from Settings" in
[docs/ACCOUNTS.md](docs/ACCOUNTS.md) has the requests, the refusals and when
each change takes effect.

The older device value `{bot: <bot user>, account: <id>}` still loads and
means that account is the device's own login. `orch check-config` reports
it as deprecated. Still, keep {bot, account} until account placement
(#1063 parts 2 and 3, then #1064) runs on every device. Until then, a bare
bot runs and records its jobs as `<harness>-default`. Replacing the mapping
with `signed_in_on` plus a bare bot today moves the device's jobs and
pauses to the default account.

Order matters. Deploy the code first, and add `accounts:` to `machines.yml`
only after every device runs it: older code refuses the key. The poller
migrates `orch.db` when it starts. To run it by hand:

```sh
bin/orch usage migrate --db ~/.local/share/factory/orch.db
```

The migration takes a labelled backup (`usage-accounts`) before any write,
adds an `account` column to `usage_readings` (filled with
`<harness>-default`) and four account-keyed tables, and does nothing on a
second run. Rollback: deploy the older code, which ignores the column and
tables. To remove them too, run `ALTER TABLE usage_readings DROP COLUMN
account` and drop `usage_account_readings`, `usage_account_observations`,
`lane_account_evidence` and `lane_account_limits`. As a last resort,
`orch restore-db` from the labelled backup loses the readings taken since.

A usage limit pauses only the account that hit it. The other accounts of
that harness keep taking jobs, and the recipient's queue pauses only when
every account serving it is limited. Readings also rank accounts for
placement through the score and the floor (see "Device pool"). They do not
change which harness builds or reviews a job.

No factory process can read Grok or Cursor usage, so an `external` account
or a `cursor` account takes readings from a command:

```sh
bin/orch usage record --account cursor-ultra --window weekly --used 0.42 --resets-at 1795467840
```

`--window` is `five_hour`, `seven_day` or `weekly` (stored as `seven_day`).
`--used` is a fraction from 0 to 1, `--resets-at` an epoch second within 30
days, and `--observed-at` (default now) may not be in the future. Only
declared accounts with `harness: external` or a harness no probe reads
(today `cursor`) are accepted; Claude, Codex and Gemini accounts are read
at each job's start and are refused. The reading is stored under the
account's own harness, and a reading older than the stored one is kept out
(`kept newer reading`). The command takes no
credential, cookie or token. A scheduled Grok Bot routine that calls
`orch usage record` is a separate step that needs Max's approval. This
ticket does not create it.

**Pages.** Every page shares one bar: links to the pages (Lanes, Jobs,
Activity, Efficiency, in that order), the link state
(`LIVE`, `STALE SNAPSHOT` after 3 minutes, `LINK LOST` when fetches fail)
and a **needs attention** badge with a count that links to the Activity
page (stuck tickets, overdue, stale or dead jobs, then paused tickets not
already listed as stuck, each once).

- **Main** (`index.html`): the lanes and slots as the centerpiece, with live
  counts in their header (running, held, idle and queued, counted from the
  tiles drawn): one lane per device from `machines.yml` (box, pc, mac; disabled devices
  dimmed and marked, placeholders flagged) with a slot per `slot_max`, and
  a lane for running Grok wakes. Each busy slot shows its job: the ticket
  (a GitHub ticket is shown as a PR, a Linear ticket by its id), bot, step
  (the job's action), its state (`claimed, not started`, `running`,
  `stale heartbeat` or `runner dead`, see "Job states"), the job's review
  round from its own detail (`round 1/1`; `—` for a job with no round),
  elapsed time and heartbeat age against its timeout. A claimed job shows
  how long ago it was claimed and `not started` instead of a timer and
  heartbeat bar. A lane counts `max(held, running)` slots, so a slot still
  held in `device_slots` is never shown as idle (#63 FU-1). The queue sits
  in a side rail beside the lanes with wait times. Clicking a slot or a
  queue item opens that job's flow (the same view as `job.html#job-<id>`)
  in a modal; Esc, the close button or a click on the backdrop closes it,
  and focus returns to the job. Without script (or with a modifier key)
  the link goes to `job.html#job-<id>`.
- **Job** (`job.html`, `job.html#job-<id>` for one job): every running or
  queued job with the path its ticket took, drawn left to right in the
  order it went: one node per visit, so a ticket that was reviewed, revised
  and reviewed again shows review, fixing, review (not a loop back). The
  current step is highlighted in the accent and breathes (a slow glow,
  2.6 s) unless the job is finished; `prefers-reduced-motion: reduce`
  leaves a static highlight. It carries the job's round and time in that
  state (`r2/3 · 33m 20s`). After it, grayed with dashed arrows, are the
  chart's direct successors of the current state, and nothing else from the
  chart. A state is finished when the chart has no way back to work from
  it (no state reachable from it lies on a cycle: done, merged, post-merge,
  handed-off and the stop states). The full statechart, with the path
  highlighted, is behind a "full chart" toggle, collapsed by default. Both
  are generated from
  [`model/chart.mmd`](model/chart.mmd), the chart the statechart check
  draws from `model/Orchestrator.tla` (CI fails if they differ), so a
  change to the statechart changes them; only the layout and a few
  plain-word glosses are the dashboard's. The path is the start state, then the state each of the ticket's
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
  and handoff savings with a one-line definition of scripted share. The
  weekly Evals table sits beside Cost at the top of the page.
- **Evals** (`evals.html`): the weekly eval reports as one table, a column
  per week, newest first, each value with its change from the previous
  available report, so a delta after a missing week spans two weeks.
  It has no nav item; Efficiency is its current tab.
- **Health** (`health.html`, issue #92): every component heartbeat as up,
  stale or down, with last seen in ET, its age, pid, device, version and
  start time, and each harness whose queued jobs have no live waiter (see
  "Health"). A down component and a harness with no waiter are
  needs-attention items on every page, linked to their Health row, and a
  queued job with no waiter carries a `no waiter for <harness>` link in the
  queue rail. With no `orch.health.db` nothing is flagged.

Each page is rendered in full on the box, so it reads without script; it
then reloads every `dashboard.refresh_seconds` (15 to 30, default 20;
`--refresh` overrides it), and on the job page a slot link scrolls to its
job, which `:target` marks. With script, each page fetches the relative
`status.json` every 1.5 seconds (`cache: 'no-store'`) and rebuilds its
panels in place, with no reload; ages tick every second between fetches,
rows that appear since the last snapshot flash briefly, and the job page
redraws each job's path (SVG nodes made with `createElementNS`, in the
namespace of the server-drawn chart) and copies the server-drawn full chart
with the highlight moved. The Evals table comes from `evals.json`, not
`status.json`: the page fetches `evals.json` for each new `status.json`
snapshot and redraws the table from any bundle newer than the table on the
page. While the bundle's `generated_at_epoch` is older than the snapshot's
(a publish still under way, or a live push that sent `status.json` alone),
it fetches again on each later poll. When that fetch fails (a 401 when
signed out, any other error, or a malformed bundle, meaning a missing or
unknown field, a value of a type or form `collect_weekly` never writes,
more than `KEEP` weeks or weeks out of order, or a state that disagrees
with its weeks or skipped count) the table already on the page stays.
If `status.json` cannot be fetched at all, the server-rendered page stays and reloads as
without script. Everything is inline: no external asset, font or CDN, so it
works offline on the tailnet. The look follows Omarchy (omarchy.org): the
Tokyo Night palette, square corners, 1px elevation rings, Geist for text
and JetBrains Mono for numbers and labels. Both fonts are vendored under
`lib/fonts` (SIL OFL 1.1, licenses alongside) and travel inside the page
script as bytes, registered with `FontFace`, so nothing is fetched and the
server's CSP is unchanged; without script the system fonts are used.
Values are only ever set as text
(`createElement`/`textContent`), never parsed as HTML. Animations (brand
caret, link pulse, heartbeat shimmer, new-row flash, the current step's
breathing) stop under
`prefers-reduced-motion: reduce`, and every text colour meets WCAG AA
(4.5:1) on every panel background (`tests/test_dashboard_mission_control.py`
checks the palette). Paths: `--db`, else
`$ORCH_DB`; `--log`, else `$ORCH_POLL_LOG`, else `poller.log` beside the
database; `--machines`, else the checkout's `machines.yml` when present.

The snapshot shows the queue (queued, running, done and failed jobs by
recipient and action), running jobs with the age of their last heartbeat,
queued jobs with their wait, box slots (every running harness job on the
box: placed there, or holding no device-pool slot, which counts in the box
row's held slots as in `orch status`, issue #90; a job placed on another
device counts on that device's row) and, when `machines.yml` (F2-01) is
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
`jobs` records no claim time), and `heartbeat_at`, the epoch of its last
heartbeat, so the ticket page beats once per real heartbeat; `handoff_savings.recent`, the newest 12
finished jobs with `kind` `scripted`, `bot_to_bot`, `failed` or `done` (the
handoff comment id itself is never copied); `poller.verdicts`, the newest
12 handled `orch:v1` comments (`handled_comments`, newest by insertion) as
`outcome` (a dispatcher outcome such as `queued-review-fix`, anything else
`other`) and `kind` (`routed`, `shipped`, `stale`, `rejected`, `stuck`,
`blocked`, `waiting`, `other`), `null` without that table;
`poller.health`, with `cycle_seconds` set to the poller's `poll_seconds`
(`P`, 30 or 60; `orch poll` passes it to each push as `--poll-seconds`,
otherwise orch.yml's value is used): `ok` while the last poll cycle ended
at most `2P + 30` seconds ago (90 s at 30, 150 s at 60), `late` to `5P`
(150 s at 30, 300 s at 60), then `stale`, and `unknown` when the snapshot
was not told when the last cycle ended (only live pushes pass
`--last-cycle`; `main` records no per-cycle row); and `poller.linear_budget`, `rate_limited`
(Linear 429 or `RATELIMITED` errors in the last hour), `exhausted` (`Linear
poll budget is exhausted` errors), `deferring` (`Linear poll budget`
deferrals) or `ok`, with those hourly counts and the poller's Linear share
of the hourly budget (1250 requests, 1,500,000 complexity).

For the layout: each running and queued job's `tracker` (`github`,
`linear` or `null`), `round` (its ticket's review round, `null` when the
ticket is not tracked) and `flow` (`state`, the ticket's workflow state on
the chart; `path`; `state_age_seconds`; `next`, the chart's direct
successors of `state`; `terminal`, whether `state` is finished); `attention` (`count` and `items`,
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
in for bot-to-bot handoffs. `engine_handled` counts granted plus landed
rulings in the `engine_decisions` log, and `engine_escalated` counts its
escalated rulings.

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
    # qa_budget_gib: 5                   # optional: QA evidence cap on the PC (docs/SPEC.md)
    interval_seconds: 20                 # only for the `orch dashboard push` loop
```

That is the confirmed target: the files land in
`/home/cursor-bot/factory-dashboard` on pc-wsl-bots (the same upload by hand
is `scp -F /home/box/.ssh/config status.json index.html activity.html
efficiency.html job.html health.html tickets.json pc-wsl-cursor-bot:factory-dashboard/`), and the server below serves
them tailnet-only at <http://pc-wsl-bots.tail68624b.ts.net:8090/>.
`config/orch.example.yml` carries this section; the shipped `orch.yml` has
it commented out, so nothing pushes until Max turns it on.

*When it runs.* `orch poll`, which
[`deploy/poller-service.sh`](deploy/poller-service.sh) runs, pushes after
every poll cycle (`poll_seconds`, 30 as shipped), or at most every `every_minutes` (1 to 1440)
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
The page needs the HTTP server to allow `connect-src 'self'`
([`deploy/factory-dashboard-http.py`](deploy/factory-dashboard-http.py)
sends it); with an older copy of that server the browser blocks the poll
and the page falls back to reloading.

*How it publishes.* Each file is first uploaded to a dot-prefixed temp name
in the served directory (`scp -q -B [-F ssh_config] <options> -- status.json
pc-wsl-cursor-bot:factory-dashboard/.status.json.push-tmp`, then the same
for each page; with `rsync`, `rsync --quiet --timeout=10 --chmod=F644 -e
"ssh [-F ssh_config] <options>" --` to the same temp names). Only after
every upload succeeds does one `ssh [-F ssh_config] <options> --
pc-wsl-cursor-bot 'cd --
factory-dashboard && mv -f -- .status.json.push-tmp status.json && mv -f --
.index.html.push-tmp index.html && ...'` rename them all over the live
names. An interrupted upload leaves the published set as it was; the
renames run back to back on the remote host with no network round-trip
between them, so a reader never sees a half-written file and the set is
mismatched for at most the instants between local `rename(2)` calls. The HTTP server
never serves the temp names. `<options>` are `-o BatchMode=yes`; connect
and keepalive timeouts are left to the host's ssh_config (issue #224), and
the 20-second per-command timeout still bounds a dead link (issue #106). Every command is an argv list (no
local shell) with a 20-second timeout; the remote shell sees only the validated
`remote_dir` and fixed file names. The config holds no secrets: `host` is a
bare alias or tailnet name (no `user@`, no options); `remote_dir` is a plain
path, absolute or relative to the remote login's home, with no leading `-`,
`~`, `.` or `..` part; `ssh_config` is a plain absolute path to an
ssh_config file, and a name that looks like a key (`id_*`, `*.pem`,
`*.key`, `*.pub`, `*.ppk`) is refused. SSH uses the login Max provisions;
the factory never reads, names or passes a key, and never reads the
ssh_config file itself. `check-config` validates the section.

*When a push fails* (issue #106), `orch poll` reads the push child's exit
status. After the k-th failure in a row it starts no push until
min(30·2^(k-1), 60) seconds after the failed one started (30 seconds after
the first failure, then 60; issue #224), with one
`dashboard push: failed (exit N), k in a row; pushes paused for Ss` line;
a success clears the backoff. A live push's pending change waits and goes
out when the backoff ends. Each push's outcome is the `dashboard-push`
Health component (below). Tests use a
fake transport and a fake spawn; nothing in this repository runs ssh, scp
or rsync against a real host.

**Live server.**
[`deploy/factory-dashboard-http.service`](deploy/factory-dashboard-http.service)
is the system unit that serves 8090 on the pc-wsl-bots WSL node. It runs
[`deploy/factory-dashboard-http.py`](deploy/factory-dashboard-http.py), a
small standard-library server, from
`/usr/local/lib/factory-dashboard/factory-dashboard-http.py` as user and
group `factory-dash`, with `cursor-bot` as a supplementary group so it can
read the pushed directory. It takes 8090 in place of
`factory-dashboard.service`, which runs `python3 -m http.server`. That
server follows symlinks out of its directory, lists directories, and serves
the working directory when `--directory` is empty.
`Conflicts=factory-dashboard.service` keeps the two from running at once.
The HTTP server answers GET and HEAD (and, with GitHub actions on, POST
under `/api`, below) only. It answers only for `/`, `/status.json`, the
eight pages (`/index.html`, `/job.html`, `/activity.html`,
`/efficiency.html`, `/evals.html`, `/health.html`, `/ticket.html`,
`/settings.html`),
`/tickets.json`, `/evals.json` and a QA run's evidence under
`/qa/runs/<run_id>`. Every other path (the weekly `weekly-*.json` reports
included), dot-file, `..` or encoded slash is a 404, and no directory is
ever listed. Each file is opened, with `O_NOFOLLOW`, relative to a
descriptor for the directory that the server opens once at startup (so
swapping the directory or an ancestor for a symlink later changes nothing),
and served only if it is a regular file with a single link, so neither a
symlink nor a hard link to a file elsewhere is served. It binds only the
node's tailnet IPv4 address, never a wildcard address. Before starting
(`ExecStartPre` runs `--check`, and again at startup) it refuses a
`TAILNET_IP` outside `100.64.0.0/10`, and a `DASHBOARD_DIR` that is unset,
empty, relative, missing, not a directory, `/`, `$HOME` or above `$HOME`
(checked after resolving symlinks). If Tailscale is down the bind fails and
systemd retries. It never falls back to another address. The unit is
sandboxed: `ProtectSystem=strict`, `ProtectHome=tmpfs` with only the pushed
directory bound back read-only (`BindReadOnlyPaths=`) and the spool, relay
and audit directories bound read-write when set (`BindPaths=`, below),
`PrivateUsers`, `PrivateTmp`, `PrivateDevices`, `NoNewPrivileges`,
`RestrictNamespaces`, IPv4 and Unix sockets only, the `@system-service`
system call filter, and `UMask=0077`. Inside it the rest of `/home` and
`/root` is empty, so no bot's `~/.ssh` is visible. WSL needs systemd enabled. Until Max runs
`install` (below), `factory-dashboard.service` still serves 8090.

**Service script.** Run
[`deploy/dashboard-http-service.sh`](deploy/dashboard-http-service.sh) as
root on the PC: `deploy/dashboard-http-service.sh install|status|restart|rollback`.

- `install` reads `http.env` first. It exits 78 before changing anything
  if the file is missing or unreadable, a required key is empty,
  `DASHBOARD_DIR` is not a directory, a path key is not an absolute path
  of only letters, digits and `. _ @ + -` with no `.` or `..` part, or
  `DASHBOARD_SESSION_STORE` is set but is not a directory (a symlink counts
  as not one) of mode 0700 owned by `factory-dash`. It adds the system user
  `factory-dash` if it is missing and checks that owner again before any
  unit changes. It then gives the group
  read and search on `DASHBOARD_DIR`, copies the app to
  `/usr/local/lib/factory-dashboard/` and the unit to
  `/etc/systemd/system/`, and writes the drop-in
  `factory-dashboard-http.service.d/paths.conf` from the path keys:
  `BindReadOnlyPaths=` for `DASHBOARD_DIR`, and `BindPaths=` for
  `DASHBOARD_SETTINGS_SPOOL`, `DASHBOARD_SIGNIN_RELAY`, the directory of
  `DASHBOARD_ACTIONS_AUDIT` and `DASHBOARD_SESSION_STORE` when each is set.
  Then it moves 8090 over. It
  disables `factory-dashboard.service`, enables and restarts the HTTP unit
  (whose `Conflicts=` stops the static server), and waits up to
  `DASHBOARD_START_WAIT` seconds for it to go live. If a step fails or the
  unit is not live by then, it disables and stops the HTTP unit, enables
  and starts `factory-dashboard.service` again, prints the HTTP unit's last
  50 journal lines, and exits 1.
- `status` exits 0 only when the HTTP unit is active,
  `factory-dashboard.service` is not, and the tailnet address in `http.env`
  answers with `Server: factory-dashboard`. Otherwise it prints `not live:`
  with the reason and exits 3. It reads the address from `http.env`, so run
  it as root or set `DASHBOARD_PROBE_ADDR`.
- `restart` restarts only a unit that is already cut over: enabled, with
  `factory-dashboard.service` inactive or failed. It then waits for
  `status`. Otherwise it refuses with exit 3 rather than cut over, because
  starting the unit would stop the static server through `Conflicts=`.
- `rollback` puts `factory-dashboard.service` back. It enables and starts
  that unit, disables and stops the HTTP unit, then confirms that the
  address answers `Server: SimpleHTTP` and that `/api/session` does not
  answer 200, so Settings is read-only. It refuses with exit 1 if
  `/etc/systemd/system/factory-dashboard.service` does not exist, since
  nothing would then serve 8090.

Exit codes: 0 live (for `rollback`, the static server answers); 1 the
cutover failed and was reverted, a `systemctl` call failed, or `rollback`
refused; 3 not live (`restart` refuses with 3; for `rollback`, the units
switched but the static server is not confirmed); 78 `http.env` is
incomplete or a path in it is unsafe; 2 usage. Overrides:
`DASHBOARD_SYSTEMCTL`, `DASHBOARD_ENV_FILE`, `DASHBOARD_PROBE_ADDR`,
`DASHBOARD_START_WAIT` (seconds, default 15), `DASHBOARD_UNIT_DIR` and
`DASHBOARD_LIB_DIR`. The cutover changes a running service, so `install`
runs only with Max's go.

**`http.env`.** The unit reads `/etc/factory-dashboard/http.env`, owner
root, group `factory-dash`, mode 0640. Max creates it before `install`.
Three keys are required: `TAILNET_IP` (the node's `tailscale ip -4`),
`DASHBOARD_DIR` (the directory `dashboard.push.remote_dir` names) and
`DASHBOARD_PORT=8090`. The GitHub actions below add optional keys:
`DASHBOARD_GITHUB_CLIENT_ID`, `DASHBOARD_APPROVER`,
`DASHBOARD_ACTION_CLIENTS`, `DASHBOARD_SETTINGS_SPOOL`,
`DASHBOARD_ACTIONS_AUDIT`, `DASHBOARD_SIGNIN_RELAY`,
`DASHBOARD_SESSION_STORE` and `DASHBOARD_SESSION_DAYS`. The file holds no
secret. The client id of an OAuth App with device flow on is public.
Any secret or OAuth value is Max's to create and set himself.
Nothing in this repository, the script or a factory job reads, creates or
writes such a value. The script parses only `TAILNET_IP`, `DASHBOARD_PORT` and the path
keys, and only the address and the paths (in the drop-in) leave the file.

**Account sign-in.** With the settings spool and `DASHBOARD_SIGNIN_RELAY`
set, the Accounts panel's **Sign in** spools an `op: signin` request for the
sign-in broker and shows the relay view: the attempt's state, the sign-in
URL the broker writes (and the code, for Codex), a box for the Claude paste
code, and **Cancel**. The relay is an absolute,
existing directory, not a symlink, that gives others no access (the group
may write, since the push login shares it). The sign-in broker on the
orch host (`orch accounts signin-broker`) takes the request, runs the
harness's login on the device and writes the view. It reads the relay that
the orch host's `dashboard.settings.relay` names, and
`deploy/signin-broker-service.sh install-autostart` keeps it running.
Without the broker the attempt stays queued. The server never signs
anything in itself. It writes only the request, the paste code and the
cancel, and it removes a code the broker has not taken after 120 seconds.
[docs/ACCOUNTS.md](docs/ACCOUNTS.md#sign-in-from-settings) has the setup,
the refusals, what is never stored, the macOS Keychain check and the CLI
fallback. [docs/decisions/1067-dashboard-signin.md](docs/decisions/1067-dashboard-signin.md)
records the 15-minute window, the 2-at-once cap, the code expiry and the
URL allowlist.

**GitHub actions (off by default).** With `DASHBOARD_GITHUB_CLIENT_ID` set
in `http.env`, the server also answers a small JSON API under `/api` so the
pages can comment on a ticket's issue or PR and give the hold OK as Max
(issue #563). Setup, by Max: register a GitHub OAuth App with device flow
on (scope `repo`); put its client id, `DASHBOARD_APPROVER=maxharris1` and
`DASHBOARD_ACTION_CLIENTS` (his phone's and laptop's tailnet IPv4
addresses, comma separated) in `http.env`; then run `install` (above),
which runs the server as `factory-dash`. `--check` and
startup refuse actions when the process user or `USER` is a bot account
(`*-bot`, `grok`, `box`), when the client list is empty, holds a
non-tailnet address or holds the node's own `TAILNET_IP`.

Threat model, in short:

- Max signs in with GitHub's device flow: the page shows a code, he enters
  it on github.com with his own 2FA. The server then reads `GET /user` and
  keeps the session only for a `User` whose login is the approver and whose
  token is an OAuth App token (`gho_`); another login, a `Bot` type, a
  `[bot]` login or a GitHub App token (`ghu_`, `ghs_`) is refused before any
  post and the token dropped. Each post's reply is checked again for the
  approver's login and no `performed_via_github_app`; a reply that fails
  that check ends the session.
- Several of the allowed devices stay signed in at once, up to 16, each
  with its own session. A 17th sign-in evicts the oldest. The browser holds
  its session in an `HttpOnly; SameSite=Strict; Path=/api` cookie
  (`Path=/` under `DASHBOARD_REQUIRE_AUTH_READS=1`).
- Without `DASHBOARD_SESSION_STORE`, sessions live only in the server's
  memory and end when the server restarts. Each lasts 12 hours from
  sign-in, or `DASHBOARD_SESSION_DAYS` days when that is set, and never
  renews.
- With `DASHBOARD_SESSION_STORE` set (a directory of mode 0700 owned by
  `factory-dash`), each device's session is sealed in its own file there.
  The key that opens it lives only in that device's cookie. No plaintext
  token, CSRF secret, session id or cookie key is written to disk. The
  session survives restarts and reboots. It slides while the device is in
  use, lasting `DASHBOARD_SESSION_DAYS` (1 to 365, default 90) from its
  last renewal.
- `POST /api/signout` ends only the calling device's session. With the
  body `{"all": true}` it signs out every device and clears the store.
- The user code appears once, in the sign-in reply;
  the device code and token never appear in a reply, a page, `status.json`
  or the log, which records only method, path, status and peer.
- Only the listed client addresses reach `/api` (everyone else gets 404),
  so nothing on the server's own node, every bot included, can call it.
  Behind `tailscale serve` the client is the last `X-Forwarded-For` hop,
  but only on a live connection from the node's `TAILNET_IP` or loopback
  whose socket the kernel lists as root's (tailscaled). Bots and every other non-root process
  on the node keep their TCP address, which is the node's own and never
  listed. The deployment enforces this by running tailscaled as root and the
  bots and `factory-dash` as non-root users (startup already refuses bot
  users). Linux only: without `/proc/net/tcp` forwarded identity is never
  used.
  Every POST needs `Content-Type: application/json` and the server's own
  `Origin`, and every POST after sign-in the session's `X-Factory-CSRF`.
- Comments and OKs go only to tickets in the served `status.json`. A comment
  whose first line is `/ok` or an `orch:v1` header, in the HTML or the
  backtick form, is refused. The OK reads
  the PR head live (the snapshot never holds heads), and the server re-reads
  it right before posting `/ok <40-char sha>`: if it moved, nothing is
  posted and the page is told the new head. The OK is refused unless the
  snapshot row is `awaiting-ok`.
- The server writes nothing but settings request files and the actions
  audit (both below) and has no chat path. Residual risk: someone who gets Max to type their device code on
  github.com gets a session as Max; only his own devices can start a sign-in.

**Device settings (issue #777).** The Settings page (`settings.html`, no
nav item; System is its current tab) lists each `machines.yml` device:
name, local or remote, enabled, `slot_max`, harnesses, slots held and
drain state, with the last results of changes to it. Signed in as the approver, Max can turn `enabled` and `drain`
on or off and set `slot_max` (1 to 64). Drain is the safe way to take a
device out of placement; turning `enabled` off on a device that holds a
running job is refused with "drain it first". That check and the file write
hold orch.db's write lock, and every claim reads the file under the same
lock, so no job lands in between. Placement reads these switches from the
shared settings row (#852). A disable, a drain or a lower `slot_max` reaches
every claim at once. An enable, a cleared drain or a higher `slot_max` waits
until the poller adopts the new file. The same page's Accounts panel manages
usage accounts ("Usage accounts" above).

The dashboard server never edits `machines.yml` or runs a script. It
cannot reach the orch host, and the orch host's credentials stay there:

1. With GitHub actions on, set `DASHBOARD_SETTINGS_SPOOL` in `http.env`.
   `POST /api/settings` `{device, field, expect, value}` checks the session,
   CSRF and approver login, the field and value, that the device is in the
   served `status.json` and that `expect` is still its value there. It then
   writes one request file, `<id>.json` (id, actor, time, device, field,
   expect, value; no token or session), and answers 202. A body with
   `target: "account"` instead names an `op` (`add`, `set`, `remove`,
   `prepare` or `doctor`) and an `account`. It is refused before anything
   is written for a bad op, keys or value (400), an account or device not
   in `status.json` (404), a `prepare` or `doctor` device the account is
   not signed in on (404), a `<harness>-default` account (declared or not)
   or an external account (403), or a stale `expect` or existing id (409).
   A `max_jobs` value must be 1-64; its `expect` may be any declared value.
   [docs/ACCOUNTS.md](docs/ACCOUNTS.md) lists every op and code. Without the
   variable, `/api/settings` is 404; setting it without
   `DASHBOARD_GITHUB_CLIENT_ID` is a configuration error. At most 20
   requests wait at once.
2. Each `self-heal.sh once` pass on the orch host runs `orch settings apply
   --state <state dir>` after `orch heal deploy` and before health. It reads
   the spool over the push's SSH alias when `dashboard.push.host` is set
   (before #708, the box reading the PC), else as a local directory (after
   #708, when the PC is the orch host). It trusts a request file only if
   `dashboard.settings.spool_owner` owns it and neither it nor the spool is
   group- or world-writable, so a bot that can read the spool cannot forge a
   request.
3. A request applies only if its actor is `dashboard.settings.approver` and
   not a bot, the device and value are valid, the field is not set in
   `machines.local.yml`, `expect` still matches the file, and the edited
   file parses with exactly that one field changed. Then it copies
   `machines.yml` to `machines.yml.bak-settings-<UTC stamp>` beside it,
   replaces the one line (comments and other devices stay byte for byte)
   and restarts the poller, then each waiter this account owns, through the
   same scripts and environment as `orch heal`. Neither restart stops a
   running job. A failed restart restores the backup and restarts again.
   Every request, applied, pending (a waiter another account runs was not
   restarted), refused or failed, adds one line to
   `settings-audit.jsonl` in the state dir (actor, request and apply times,
   device, field, previous and new values, result, error, backup), and the
   next push shows it on the page. An id is never applied twice.
   An account request takes the same path with its own checks. It is
   refused for a default or external account, an unknown account or device,
   a device that does not list the harness, a key set in
   `machines.local.yml`, a stale `expect`, or no change. A remove also
   needs the account paused (`not-paused`) with no running job on any
   device. No edit may drop a device where the account runs a job, and a
   failed restart never restores a file that would. A `prepare` or `doctor`
   request makes no edit, backup or restart. Prepare runs `orch accounts
   prepare --device <device> --account <id> --machines /dev/stdin` from the
   device's `factory_root` over the doctor's ssh path, with this
   `machines.yml` on stdin, then runs a doctor pass. Doctor does the same:
   it prepares the pair before the pass. Each records the pair's sign-in
   status before and after.

Setup, by Max: create the spool as an absolute directory, not a symlink,
owned by the dashboard user with mode 0755; the account the push SSH logs in
as needs read access. `install` binds it read-write through its drop-in, so
create it before you run `install`, or run `install` again after. `install`
adds `factory-dash` with no home directory, so create `/home/factory-dash`
too if you use the path below. Then add to the orch host's `orch.yml` (or
`orch.local.yml`):

```yaml
dashboard:
  settings:
    spool: /home/factory-dash/settings-spool
    spool_owner: factory-dash
    approver: maxharris1
```

Without this block, `orch settings apply` does nothing.

**Max's actions (issue #822).** A ticket that waits on Max carries a
`wait` (why it waits and the comment that explains it) and an `actions`
list in `status.json`. Signed in as the approver, Max acts on it from the
dashboard: merge OK, approve an oversize plan or split it, another round,
answer a question, requeue a failed job, resume a blocked ticket, or close.
The server posts the comment as Max; no bot posts on his behalf.

1. With GitHub actions on, set `DASHBOARD_ACTIONS_AUDIT` in `http.env` to
   an absolute file path. If the file exists it must be a regular file, not
   a symlink, and not group- or world-writable; its directory must exist and
   not be group- or world-writable. The server opens it once at startup
   (append only, mode 0600). The unit's sandbox hides the home
   directories, so `install` writes a `BindPaths=` line for the audit
   file's directory into its drop-in. Run `install` again after you set or
   change the variable. Without the variable, `/api/act` is 404; setting it without
   `DASHBOARD_GITHUB_CLIENT_ID` is a configuration error.
2. `POST /api/act` `{repo, ticket_id, action, expect_state, expect_why,
   words, head}` checks the session, CSRF and approver login (403
   `not-approver`), the action name (400 `bad-action`), the words (400
   `bad-words`: non-empty, at most 32768 characters) and, for the merge OK,
   a full 40-character `head` (400 `bad-head`). It then finds the ticket's
   row in the served `status.json` (404 `not-in-snapshot`) and refuses with
   409 `changed` if the row's state or wait reason is no longer what the
   page showed, `not-applicable` if the row no longer lists the action, and
   `no-target` if the row has neither a PR nor a GitHub issue. The comment
   goes on the row's PR, else its issue, so a Linear ticket's goes on its
   PR. A merge OK re-reads the PR head and answers 409 `head-changed` with
   the new head, posting nothing, if it moved. Otherwise the server builds
   the comment itself and answers 201 with its URL.
3. Each comment is an `orch:v1` header the server chooses (`kind=ok
   head=<sha>`; a `kind=go` for approve, resume and another round;
   `kind=answers` for an answer; `kind=retry` for requeue; `kind=answers
   state=abandoned` for close and split), then Max's words under
   `**Max's words** (Max <date> <time> ET)`, every line quoted with `> `
   and `<!--`/`-->` escaped. Nothing he types can become a first line, a
   `/ok` or a header.
4. Every request that reaches `/api/act` with a valid session and CSRF,
   posted or refused, appends one JSON line to the audit file: time (UTC),
   login, repo, ticket id, action, expected state, head, result (`posted`
   or the error code) and the comment URL. The words, token, cookie and
   CSRF are never written. Requests refused before that (no session, a bad
   CSRF, origin or content type) write no line.

## Local config overlay

Host-specific values live in gitignored `orch.local.yml` and
`machines.local.yml` beside `orch.yml` and `machines.yml` (issue #107), so
the checkout stays clean and `git pull` never conflicts. They sit beside
the base files rather than in a home directory because the poller and each
bot's waiter run as different users and must read the same config. An
overlay is deep-merged into the parsed base file before validation:

- mappings merge key by key;
- `projects` merge by `repo`, and `devices` by `name`. A matching entry's
  fields override the base entry's, and any other entry is appended;
- every other list, and every scalar, replaces the base value.
- `linear_project_id` and `linear_project_ids` are one field: setting
  either in an overlay entry drops the other from the base entry.

A malformed overlay is an error that names the overlay file.
`ORCH_NO_LOCAL_OVERLAY=1` turns overlays off; the test suite sets it.
`orch check-config` names the overlay in use and the keys it sets, never
their values, for example `overlay orch.local.yml sets:
linear_webhook.public_url, projects[aut0-code/aut0].linear_project_id`. A
placeholder the overlay fills is no longer reported.
[`config/orch.local.example.yml`](config/orch.local.example.yml) and
[`config/machines.local.example.yml`](config/machines.local.example.yml)
show the keys the box sets: the Linear project id, `trusted_linear_users`,
`linear_webhook` `path` and `public_url`, a project's `ready_starts_review`,
and a device's `slot_max`, `enabled` and `test_suite_max`. The tracked
`machines.yml` refuses `test_suite_max`, since it is per host. It defaults
to 1, and `orch job run` passes it to the job as `ORCH_TEST_SUITE_MAX`.

Migration on the box (Factory, once, after merge):

1. Move the uncommitted edits in `orch.yml`, `machines.yml` and
   `deploy/install.sh` into `orch.local.yml` and `machines.local.yml`. The
   `install.sh` edits are already in the repository: the profile guard,
   the `mkdir` and the listener autostart.
2. Run `orch check-config --allow-placeholders` and compare the keys it
   reports with the old diff.
3. Run `git checkout -- orch.yml machines.yml deploy/install.sh`, then
   `git pull`.
4. Compare the live `~/.local/share/factory/linear-webhook-service.sh`,
   including its reverse-tunnel command, with
   `deploy/linear-webhook-service.sh`. Its tunnel host and port are
   `LINEAR_WEBHOOK_TUNNEL_HOST` and `LINEAR_WEBHOOK_PORT` (see "Listener
   service without systemd").
5. Re-run `deploy/install.sh --autostart`. It retires the legacy launchers
   and installs self-heal.

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
`--orch`, it checks a machines file only when `--machines` names one. A device
with a `REPLACE_WITH_` value is skipped with or without `--allow-placeholders`:
a warning names the device and the field and says it will not be scheduled,
and the check still passes. Any other machines error fails it. A disabled
device is always named on stderr and does not fail the check.

## orchestrate-project

Grok Bot's skill is [`grok/orchestrate-project/SKILL.md`](grok/orchestrate-project/SKILL.md). Comment text, the questions gate, and routing go through [`lib/orchestrate.py`](lib/orchestrate.py). `choose_route` validates the factory.md pair with `assert_cross_family` and enforces a cross-family reviewer against `built_by`.

## pstack pin

[`skills/pstack`](skills/pstack) is pstack 0.15.36 from `cursor/plugins` commit `c742dd61706fbacc1b95f1a20b7b988cf096ce6c`. [`skills/skills.lock`](skills/skills.lock) records that commit and the sha256 of each file, plus the sha256 of each file under [`skills/factory`](skills/factory), the factory's router skills, in its `factory:` section. A pin bump, or any edit to `skills/factory`, regenerates that lock in the same PR.

```sh
bin/orch install-skills
bin/orch install-skills --cloud --project .
bin/orch doctor --print-canary
bin/orch doctor --home "$HOME" --responses DIR
```

`install-skills` symlinks each pinned skill, and each skill in `skills/factory` (the routers `factory-builder` and `factory-reviewer`, and `factory-qa`), into `~/.claude/skills/<name>/`, `~/.agents/skills/<name>/`, and `~/.cursor/skills/<name>/`. `--cloud` copies those skills into the project's `.claude/skills/`, `.agents/skills/`, and `.cursor/skills/`. Each copy gets an `.orch-owned` marker with the sha256 of every file it holds. A later install replaces only a copy whose files still match its marker, so an edited copy is refused and kept. A local install refuses a checkout that is a linked git worktree or sits under the temp dir, since its links would dangle once that checkout is gone; `--allow-unstable-checkout` overrides that. A link into another or removed checkout's `skills/pstack/skills/` or `skills/factory/` is re-linked to this checkout, and a missing skill is added. In `--cloud` mode, `doctor` rejects a symlink anywhere on the path from the project down to a skill.

`doctor` checks every installed skill and router against the lock, then prints a canary that asks the harness to read the installed `factory-builder` skill, name it, and quote the whole first bold passage of the pinned poteto-mode orchestrate playbook exactly as written, without the `**` markers, on one line. Claude Bot and Codex Bot each start their own harness and write the reply; Cursor is not asked. `doctor` fails if either harness did not load that version. It does not start `claude` or `codex`.

## Weekly eval report

```sh
bin/orch eval-report --input tests/fixtures/eval/week
```

The report reads `orch:v1` comments, `dispatcher.log`, `orch.yml`, and each run's `factory.md` from that directory. Each `dispatcher.log` row names `ticket=<id>` (a GitHub number or a Linear id), with an optional `pr=`, so a ticket's Intake, Questions and Planning rows log before a PR exists. Every input must sit inside the directory; a symlink out of it is refused. A `dispatcher.log` line that does not parse, such as a line a crash tore, is skipped: the report prints how many it skipped, and stderr names each one by line number. `--since` and `--until` (`YYYY-MM-DDTHH:MM:SSZ`, inclusive) select a week. The whole log is measured and then clipped to the window, so a state that crosses either bound counts only its time inside, and a state still open at the end of the log is measured to `--until` or listed under open states. A verdict whose risk can't be found is reported `cap-unverified`; a round over the cap is `round-extra-authorized` when a `capped` to `fixing` step (Max's extra round) allows it, and `round-over-cap` otherwise. A missing `model-findings.txt` is reported as none supplied. A later weekly run passes the directory that holds that week's logs. The command does not contact GitHub or Linear. `--json` prints the same report as JSON.

A bot that hands work to another bot by hand, instead of through an `orch:v1` handoff, logs it:

```sh
bin/orch log-message --repo maxharris1/factory --pr 443 --from grok-bot --to codex-bot
```

That appends `TIMESTAMP repo=<owner/name> pr=<n> message from=<bot> to=<bot> kind=handoff` to `dispatcher.log` (`--log`, else beside `$ORCH_DB`). The line is written with a leading newline in one append, so a dispatcher line torn mid-write cannot swallow it; the report skips the blank lines this leaves. It refuses a repo that is not `owner/name`, a non-numeric pr, or an empty bot, and writes nothing. The report counts these manual messages against scripted handoffs (the `comment` rows) per repo and ISO week. No input records a per-message token count yet, so it prints no token-savings figure.

```sh
bin/orch eval-report --db ~/.local/share/factory/backup/orch.db --baseline o4-orch.db
```

`--db` prints the SF-10 metrics (#209) of an `orch.db` backup beside the `--baseline` snapshot (O4's). Both are opened read-only. A metric whose source a snapshot does not record prints `unrecorded`; [`lib/eval_report.py`](lib/eval_report.py) lists where each metric comes from.

## Model family

`orch check-config` refuses a reviewer unless it is a different model family from the builder. The check fails closed.

- A `claude` harness is Anthropic. A `codex` harness is OpenAI. The harness decides the family. A hint that names the other family is refused.
- A `cursor` harness takes its family only from the model hint. `claude*`, `sonnet*`, `opus*`, and `haiku*` are Anthropic. `gpt*`, `codex*`, and `o<digit>` (`o3`, `o4-mini`) are OpenAI. The family word must end the name or be followed by `-`, `.`, `_`, `:` or a digit, so `haikus`, `claudette` and `magnum-opus` resolve to no family. `opus-mt*`, `gpt-j*` and `gpt-neo*` are other model lines and resolve to no family.
- If either side's family cannot be resolved, the check refuses. A cursor side needs one of those hints.
- If both families resolve and they are the same, the check refuses.

## Model

[`model/Orchestrator.tla`](model/Orchestrator.tla) is the section 4 ticket loop. State names are the `orch:state` labels (`waiting-max`, `post-merge`, …). T1 is the `kind=go` handoff. T9 is in the model and is off when `AllowPostMerge = FALSE` (Phase 1).

```sh
bash model/check.sh
```

That runs pstack `/statechart` (TLC, one worker). [`model/chart.mmd`](model/chart.mmd) is the section 4.1 chart, including post-merge. The Phase 1 drawing config does not reach post-merge. [`model/fixtures/omit-t1.cfg`](model/fixtures/omit-t1.cfg) drops T1 and must fail.
