# Plan for #128: detached remote jobs that survive an SSH drop

Today a remote job runs inside the SSH session that starts it. `PlacedRun.argv()` builds `ssh_argv(start_script(...))`, `start_script` execs the harness in that session, and `job_run.run` waits on the local `ssh` client. When the relay cuts the link, three things go wrong. `ssh` exits 255, and `orch job run` treats that as the command's exit. `PlacedRun.finish` then finds the worker still RUNNING and SIGTERMs it. The harness has also lost its stdout channel, so its next write kills it with SIGPIPE. The job dies on both machines even though nothing on the device failed.

This plan detaches the harness from the session. A short SSH call launches it under `setsid` on the device, with its output going to files. `orch job run` follows the run with short SSH calls and retries through a drop. Each start has one run token (the worker tag the slot already records), which makes the launch idempotent, so a retry can never start a second harness.

PR #129, branch `factory/issue-128` off `main`. Claude builds and Codex reviews. Risk is high: up to three rounds, and every extra round needs Max's own words.

## How to read this

One box is one unit of work. Each box names the evidence that checks it. Check a box only when that evidence exists: a test, a log line or a SHA. The build follows `pstack/skills/poteto-mode/playbooks/feature.md` for a single backend PR with no UI. The multi-lane skeleton does not apply, but its verification rule does (see **Verify**).

## The data shape

The builder names these facts before writing any code.

1. **The run token is the worker tag.** `before_start` already records a fresh 32-hex tag on the slot (`attach_worker(remote=True, tag=...)`) before anything is sent, and #112 makes that record unable to overwrite another worker. The tag becomes the name of the run directory on the device. Every launch attempt for one `orch job run` sends the same tag. `lib/job_token.py` (`ORCH_JOB_PROOF`) stays what it is: a secret for `orch job done` that is deliberately never sent to a device. It does not change.
2. **The device keeps the run under its own directory.** That is `<workdir_root>/jobs/<job-id>.run/`, beside the existing `<job-id>.launched` and `<job-id>.brief.md`, and outside the job directory that the harness clones into.
   - `<tag>/` is made with `mkdir`, which is atomic. If it already exists, this tag has already been launched.
   - `<tag>/stdin` holds the brief, uploaded on the launch call's stdin, mode 600. The harness reads it as its stdin, as it reads the forwarded SSH stdin today.
   - `<tag>/pid` holds `<wrapper pid = process group> <harness pid> <boot id or boot session>`.
   - `<tag>/log` holds the harness's stdout and stderr, which today go to the SSH channel.
   - `<tag>/exit` holds the harness's exit status. It is written to a temporary file and then moved into place, only after the harness has ended.
   - `<tag>/handoff` holds the last non-blank log line when it is an `ORCH-HANDOFF: <id|failed>` line (the same rule as `waiter.handoff_marker`). Otherwise it is empty.
   - `current` holds the tag of the job's active run. It is the job-level fence.
   - The existing `<job-dir>/.orch-worker.pid` (Linux) and `<job-dir>/.orch-worker-<tag>.pid` (Darwin) are still written, by the wrapper, so that `check_script`, `stop_script` and `DeviceMonitor` keep their contract unchanged.
3. **A dropped SSH call is not an event.** Only three things end a follow:
   - the device's `exit` file;
   - the device confirming the worker is gone with no `exit` file (killed, or a reboot);
   - `FOLLOW_GRACE_SECONDS` (30 minutes, under the 60-minute `OFFLINE_SECONDS`) without one successful call.

   `run-died` is recorded only for the second and third. A failed `exit` status records `run-died` as it does today.

## Decisions taken here (technical, not product)

- **`setsid` plus nohup-style redirects, not `systemd-run --user`.** The ticket's acceptance criterion allows either. A `systemd --user` manager in WSL stops when the user's last session ends unless linger is on. A unit under it would therefore die exactly when the SSH session drops, which is the failure we are fixing. It also cannot be exercised by the fake ssh in CI. Detaching works like this:
  - Linux: `setsid`. util-linux is always present on WSL.
  - macOS has no `setsid` binary. Use `setsid` if one is on PATH, else `perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV'` (`/usr/bin/perl` ships with macOS).
  - With neither available, the launch refuses with 125, nothing runs, and the existing #91/#94 unstarted requeue applies.
  - The wrapper ignores SIGHUP, has `/dev/null` as stdin and writes its stdout and stderr to `log`.
- **The wrapper does not exec the harness.** It stays alive as the group leader, waits for the harness and writes `exit`. The Darwin record therefore still names a live leader for the whole run. It catches SIGTERM with a handler rather than ignoring it, so the harness starts with default signal handling. After the signal it waits again, then records `128+n`.
- **The waiter does not change its parsing.** The follower relays the device's `log` byte for byte to its own stdout, which is the waiter's `run.log`. `handoff_marker` and `_finish_remote` then work unchanged. The waiter's only change is its docstring.
- **No change to `machines.yml` or `lib/machines_yml.py`.** The platform is still picked on the device with `uname -s`. Nothing is project-specific.
- **No schema change in `lib/device_pool.py`.** The slot already holds `remote_started` and `worker_tag`. The follow offset lives only in the process.

## Work

### 0. Base

- [ ] Bring in PR #112's `tests/fake_ssh.py` link faults (`.delay`, `.delay-detached`, `.drop-after`, `.offline`) and its `attach_worker(runner=...)` guard, using a merge commit only (no rebase, no force-push).
  - If #112 has merged, run `git merge origin/main`.
  - Otherwise, run `git merge origin/factory/issue-106` and say in the PR body that #112's commits show in the diff until #112 lands.
  - Do not write a second fake ssh.

  Evidence: the merge SHA, and `tests/test_fake_ssh.py` passing on the branch.
- [ ] Check whether PR #100 (telemetry) has merged. If it has, merge main and apply box 6b. If it has not, keep `job_run.run`'s body untouched, so that #100's `output` hunk still merges cleanly. Evidence: a note in the PR body.

### 1. `lib/remote.py`: launch, poll and stop scripts

- [ ] `launch_script(target, command, env, *, guarded_dirs, insert_at, cd, not_after)` replaces the session-bound `start_script`. The existing argv, env, `--add-dir` guard and `bash -lc` login and launch-record logic is reused as the wrapper's inner command. The launch does the following, in order:
  1. Run the platform checks it runs today, else exit 125.
  2. Refuse with 125 when the box clock's `not_after` deadline has passed on the device (see box 5).
  3. `mkdir -p` the run dir, then `mkdir <tag>`. If that fails because the directory exists, print `orch-launch: already <state>` and exit 0 without starting anything.
  4. Apply the job fence. If `current` names another tag whose run has no `exit` file and whose worker the existing check fragment does not call gone, exit 124 (`orch-launch: busy`).
  5. Write `stdin` from the call's stdin.
  6. Start the wrapper with `setsid` (or the perl fallback) as a background process.
  7. Wait up to 10 seconds for the wrapper to write `pid`, or `exit` if it refused. Then print `orch-launch: started` or `orch-launch: refused <code>`.

  Evidence: the launch unit tests in `tests/test_detached_remote.py`.
- [ ] The wrapper is POSIX `sh` and runs under `exec env ORCH_WORKER_TAG=<tag>`. It writes `.orch-worker.pid` (Linux) or the noclobber Darwin record, then `pid`. It runs the login shell and harness with `< stdin > log 2>&1`, waits, and writes `exit` and then `handoff`, each through a temporary file and `mv`. Evidence: tests that read `pid`, `log`, `exit` and `handoff` on the fake device after a run.
- [ ] `poll_script(target, offset, chunk)` sends nothing but reads, and never starts anything. It prints `orch-follow: <state> <log size> [<exit>]` on stderr, where the state is `running`, `exited`, `gone`, `unsure` or `none`. On stdout it prints log bytes `[offset, offset+chunk)`. It reads `exit` before and after the liveness check, so a run that ends between the two reads as `exited`, not `gone`. The liveness check is the existing Linux tag scan and pid group, or the Darwin record (`_darwin_worker`). Evidence: unit tests for each state, including a SIGKILLed wrapper (`gone`) and a missing run dir (`none`).
- [ ] `check_script`, `stop_script` and `launched_script` keep their exit-code contract. `stop_script` reaches the detached group because it finds processes by their tag, so it needs no change beyond what tests prove. Evidence: the existing `test_remote_start.py` and `test_remote_darwin.py` pass. Assertions there that encode "the harness runs in the SSH session" are edited in place, minimally. New coverage goes only in the new files.

### 2. `lib/remote_follow.py` (new): the box-side follower

- [ ] `Follower(transport, target, log_sink, clock, sleep)` runs the full follow, without touching any job state:
  - `launch(brief_bytes)`: retries on 255 or a call timeout with backoff (2, 4, 8 … capped at 60 seconds) until `FOLLOW_GRACE_SECONDS` have passed. It sends the same tag and the same brief every time, so `already` is a success.
  - `follow(control)`: polls every `POLL_SECONDS` (5). It accepts a poll's bytes only when the call exited 0, the marker parsed and the byte count matches. A partial transfer cut by a drop is discarded and fetched again from the same offset, so after a reattach there are no gaps and no duplicate bytes. On 255 or a timeout it backs off as above, and the clock it measures against the grace window resets after every good call. While more log is waiting it loops without sleeping.

  It returns a `FollowEnd`, which is one of:
  - `exited(code)`;
  - `gone` (confirmed, no `exit` file);
  - `lost` (the grace window passed);
  - `refused(code)` (the launch was refused, so nothing ran);
  - `stopped(code)` (a cancel).

  Evidence: follower unit tests, using fake ssh and an injected clock and sleep.
- [ ] Cancel. When `control.cancelled` is set (a signal reached `orch job run`) or `control.job_ended` is set (the heartbeat got a QueueError because the job was failed, finished or marked stuck), the follower calls `stop_worker`, retrying through drops within the grace window. It then follows until `exited` or `gone`, and returns `stopped`. Evidence: the cancel and job-ended tests (box 7).

### 3. `lib/job_run.py`: heartbeat and signals for a follow

- [ ] `Heartbeat` gains an `ended` event, set when it stops for good on a QueueError. Nothing else in it changes. Evidence: a unit test.
- [ ] `follow(*, beat, interval, log, setup, teardown, body)` shares `run`'s first beat, heartbeat thread, setup and teardown, and signal handlers. `run`'s handler install and restore is extracted into one helper used by both; `run`'s own body is otherwise unchanged. Inside `follow`, SIGTERM, SIGHUP and SIGINT set `control.cancelled`, because there is no local child for them to reach. The heartbeat runs on its own thread against the local database, so it keeps beating whatever the link does. Evidence: `test_job_run.py` unchanged and passing, and the new heartbeat-across-a-drop test.

### 4. `lib/placed_run.py`: launch instead of argv

- [ ] On a remote device `PlacedRun.argv()` is no longer used. `PlacedRun.follow(command, env, brief, control)` builds the target's `launch_script` (with `for_device` for codex, as today), runs the `Follower`, and maps its `FollowEnd` to an exit status:
  - `exited` gives its code;
  - `refused` gives 125, or 124 when the device was busy;
  - `gone` and `lost` give 255;
  - `stopped` gives the code in the device's `exit` file, or `128+signal`.

  It also records which end it was, for `ended`. Evidence: unit tests.
- [ ] `ended(code, end)` records `run-died` with a precise detail:
  - "worker gone on <device> without an exit status";
  - "lost contact with <device> for 30 minutes; worker not confirmed gone";
  - or `exit_reason(code)`.

  An SSH 255 from a poll or a launch never reaches `ended`. Evidence: the run-died tests (box 7).
- [ ] `finish` keeps its logic: a check, then a stop if RUNNING, then the slot is released only on GONE, otherwise it is held for `DeviceMonitor`. After `exited`, the check normally answers GONE at once. `unstarted` keeps using `harness_launched`; the wrapper writes the launch record just before the harness, as `_START_EXEC` does today. Evidence: the existing `test_claim_placement.py` and `test_liveness_requeue.py` pass, plus the new refused-launch requeue test.

### 5. Making a duplicate launch impossible (three layers)

- [ ] **Same tag** (a retried launch, or a late delivery of the first attempt). `mkdir <tag>` is atomic, so only one launch can create it. Every later launch prints `already` and starts nothing. The poll, check and stop scripts never start anything. Evidence: the late-delivery test (box 7).
- [ ] **Same job, different tag** (a new `orch job run` after a runner died). The existing guards still apply on the box: `attach_runner` refuses a second live runner, `_earlier_worker_gone` checks the earlier tag, and #112's `attach_worker` never overwrites a worker. The device adds the `current` fence from box 1, which refuses a second live run of the job. Evidence: a test that launches tag B while tag A runs, expecting exit 124 with A's harness count still 1 and B's at 0.
- [ ] **A launch delivered after the box gave up on it.** Each launch carries `not_after` = the box clock + `LAUNCH_TTL_SECONDS` (10 minutes). The device refuses any launch it receives after that deadline. The box gives up only after the 30-minute grace window, so a late original launch cannot start beside a T6 retry, and up to 10 minutes of clock skew is tolerated. This does not implement #70 F2.2 (holding the slot while a late start may still arrive), which stays on #117. Evidence: a test that uses `.delay-detached` with a past `not_after`, expecting a refusal and a harness count of 0.

### 6. `lib/cli.py`, `lib/waiter.py`, `docs/SPEC.md`

- [ ] `_job_run_command`: for `placed.remote`, it reads the brief from stdin (capped at 1 MiB, or refuses with exit 2), then calls `job_run.follow(body=lambda control: placed.follow(harness, remote_env, brief, control), ...)`. The box path is unchanged. The `finally` block calls `ended` and `finish` as today. Evidence: the CLI end-to-end tests (box 7).
- [ ] 6b, only if #100 has merged first: the follower feeds the relayed bytes to #100's `output` callback through a pipe. The start's `ORCH-WORKER-ENV` report must never land in the device `log`. The wrapper writes it to a mode-600 `<tag>/env` file, which the follower reads once and then removes. Evidence: a test asserting that `log` has no `ORCH-WORKER-ENV` line.
- [ ] In `lib/waiter.py`, only the module docstring changes: the output is "relayed by orch job run's follower into run.log". `job_run_argv`, `handoff_marker` and `_finish_remote` are unchanged. Evidence: `test_waiter.py` and `test_waiter_remote.py` pass.
- [ ] In `docs/SPEC.md`, update the section 10 F2-02 paragraph. The remote start is now detached under `setsid`, with the run dir files listed in fact 2. The box follows it with short calls, retries 255 with backoff, and records `run-died` only for a confirmed-gone worker or a 30-minute loss of contact. Add one decision-log row, or a `docs/decisions/` entry if PR #124 has merged by then. There are no statechart changes and no new job states. Evidence: the diff, and CI's statechart check passing.

### 7. Tests (new files only)

All tests use #112's `tests/fake_ssh.py`, with a fake harness script on `FAKE_SSH_PATH`. The harness prints lines, sleeps, appends to a harness-count file and ends with `ORCH-HANDOFF: 123456`. Darwin cases reuse the `uname` and `sysctl` shims pattern from `test_remote_darwin.py`. Helpers are imported from the existing test modules, not copied.

- `tests/test_detached_remote.py` covers the scripts and the follower:
  - launch, `already` and `busy`;
  - each poll state;
  - partial-transfer discard;
  - the backoff sequence;
  - grace expiry;
  - the perl-`setsid` fallback, with `setsid` hidden from PATH;
  - Darwin launch and poll.
- `tests/test_detached_remote_cli.py` runs `orch job run` and the waiter end to end:
  - [ ] **Survives a forced SSH kill (done-when).** In the middle of a run, SIGKILL every live fake-ssh client (pids from `FAKE_SSH_LOG`) and set `.offline` for about 3 polls, then clear it. `orch job run` exits 0, and the harness count is 1. `run.log` holds the harness output once, byte for byte, ending with the marker. There is no `run-died` event, the slot is released, and the waiter's `_finish_remote` marks the job done with handoff 123456.
  - [ ] **`.drop-after` on polls mid-run.** The same outcome, and `run.log` equals the device `log` exactly.
  - [ ] **Late delivery (done-when).** A launch with `.delay-detached` longer than the test's launch call timeout. The box retries, gets `already` or `started`, the late copy arrives and is refused, and the harness count is 1.
  - [ ] **Heartbeats across a drop.** The job's `last_activity_at` keeps advancing while `.offline` is set.
  - [ ] **Cancel.** SIGTERM `orch job run` mid-run. The device worker is gone (by tag scan), `exit` holds 143, and the slot is released.
  - [ ] **Job ended elsewhere.** Mark the job failed in the database mid-run. The heartbeat stops, the follower stops the unit, and the worker is gone.
  - [ ] **run-died timing.** An outage shorter than the (test-sized) grace window records no `run-died`. A longer one records `run-died` "lost contact", holds the slot and does not stop the worker. A SIGKILLed wrapper and harness record `run-died` "gone without an exit status".
  - [ ] **Unstarted.** A harness missing from the login PATH means no launch record, so the job is requeued as unstarted (`requeued-early-exit`) with no round charged.
  - [ ] **Brief on stdin.** The harness reads the brief bytes from its stdin on the device.

## Files the ticket lists that this plan leaves alone, and why

- `lib/job_token.py`: the dedupe token is the worker tag (fact 1). The proof token must never travel to a device.
- `lib/device_pool.py`: no new columns are needed. It only gains #112's `attach_worker` guard through the merge.
- `lib/job_state.py`: `RUN_DIED` and `RUN_EXITED` are unchanged. The rule about when they are recorded lives in `PlacedRun.ended`.
- `lib/machines_yml.py`: no format change and no new field.

## Out of scope (from the ticket)

- Host SSH config.
- #70 F2.2's late-start slot hold (on #117).
- #112's work.
- Cursor jobs.
- The poller restart path.
- Reattaching after the box-side `orch job run` itself dies. The device's `exit` and `handoff` files make that possible as a follow-up, but today `DeviceMonitor` still stops an orphaned worker, as before.

## Acceptance criteria → work

| # | Criterion | Met by |
|---|---|---|
| 1 | Launched detached on the device (WSL `systemd-run` or `setsid nohup`; Mac `setsid`/`nohup` or launchd/tmux). The live SSH session is not the harness's process | Box 1 launch plus wrapper: `setsid` on Linux and WSL, `setsid` or the perl `POSIX::setsid` fallback on macOS, nohup-style redirects (decision above). The launch call returns once the wrapper has recorded itself. Tests: launch and Darwin in `test_detached_remote.py`, and the forced-kill test (the harness outlives every SSH client) |
| 2 | pid, exit code, log and the `ORCH-HANDOFF` result written under the per-job directory | Fact 2 and the box 1 wrapper: `<job-id>.run/<tag>/{pid,log,exit,handoff}`. Tests read each file after a run |
| 3 | The box polls with short SSH calls (status, tail, exit), retries on 255 with backoff, and reattaches idempotently | Box 1 `poll_script` (status, tail and exit in one call) and box 2 `Follower`: backoff 2→60 seconds, offset-exact tail, partial transfers discarded. Tests: backoff, partial discard, `.drop-after` byte equality |
| 4 | Heartbeats keep working across a dropped session | Box 3: the heartbeat thread beats the local database independently of the follow. Test: `last_activity_at` advances during `.offline` |
| 5 | Cancel or timeout kills the remote unit | Box 2 cancel: a signal (`control.cancelled`) or the job leaving `running` (`control.job_ended`, as for a timeout, stuck or fail) leads to `stop_worker` and a follow until gone. Box 4 `finish` stays as a backstop. Tests: cancel and job-ended |
| 6 | A retry or reattach cannot start a second harness | Box 5: the per-tag atomic `mkdir`, the read-only poll, check and stop scripts, the job-level `current` fence, the box-side runner and worker guards, and the `not_after` deadline. Tests: late delivery, busy fence, past deadline |
| 7 | `run-died` only once the process is confirmed gone or a long grace window has passed, never on an SSH 255 mid-run | Fact 3 and box 4 `ended(code, end)`: only `exited`, `gone` and `lost` (30 minutes) reach it. Tests: run-died timing, and the forced-kill test recording no `run-died` |
| 8 | Done-when: survives a forced SSH kill and completes, and a duplicate launch is impossible. Proven in new test files with #112's fake ssh | Box 0 merges #112's helpers. Box 7 forced-kill and late-delivery tests in `tests/test_detached_remote_cli.py` |
| 9 | Project-agnostic, `machines.yml` compatible | No `machines.yml` or `machines_yml.py` change. The platform is picked on the device with `uname -s`. Nothing is keyed to a repo. Evidence: `check-config` in CI unchanged and passing |

## Verify

- [ ] `env -u ORCH_DB pytest`: the full suite is green locally. Evidence: the summary line in the ready handoff.
- [ ] CI is green on the ready head: pytest, `check-config` and the statechart check. Evidence: the run URL.
- [ ] Manual replay on the PC, optional and after review, by Factory or Max: start one real remote job, kill its box-side `ssh` processes mid-run, and confirm that it completes with one handoff. This does not gate the merge, and the builder does not touch host SSH config.

## Merge risks

- PR #100 touches `job_run.py`, `remote.py`, `placed_run.py`, `waiter.py` and `cli.py`. Keeping `run()`'s body untouched and putting the follower in a new module keeps the overlap small. Box 6b covers the case where #100 lands first.
- PR #112 touches `placed_run.py` and `device_pool.py`. It is brought in by box 0.
- PRs #113 and #114 do not overlap.
