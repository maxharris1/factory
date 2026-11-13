# Plan: #106 Runner resilience (PR #112)

Ticket: https://github.com/maxharris1/factory/issues/106. It folds in #70 and #71.
Branch `factory/issue-106` off `main`. Risk is high: it changes the runner and the remote start.
Builder: claude-bot. Reviewer: codex-bot.

**Dependency.** The ticket says to start only after PR #100 (job telemetry) and
PR #104 merge. #104 is merged (`26e2266`, on this branch). **#100 is still open.**
It touches `lib/job_run.py`, `lib/placed_run.py` and `lib/remote.py` (an `output=`
stdout consumer that tees the harness stream into `events.jsonl`). This plan is
built so that hook keeps working: the remote harness's output still reaches
`orch job run` as the child's stdout. The build step merges `main` once #100
lands (a merge commit, no rebase) before it touches those three files. Phases
1, 2, 6 and 7 below do not touch them, so they can start first.

## Architect scaffold

**Goal.** A dropped Tailscale or SSH link no longer kills a remote job. The
harness runs detached on the device. `orch job run` follows it and reattaches.
The job fails only when the device stays unreachable past a grace period, or
when the device's own exit record says the harness failed. The two #70 races
are closed, and the tests replay them deterministically. Dashboard pushes fail
fast and back off, and Health shows when they keep failing. Discovery no longer
queues a review for a head that already has a verdict or is no longer the PR head.

**How it works today.** `orch job run` spawns `ssh … <start script>`. The start
script execs the harness in the sshd session, and the harness's stdout is the
SSH channel. When the link drops, ssh exits 255, so the job's command "ended".
`finish()` then finds the worker UNREACHABLE and holds the slot, and the waiter
fails the job with no handoff. That is review job b2aedcd9.

**Shape after.**

```
box: orch job run ──(job_run.run, heartbeat, signals, #100 stdout tee)──> child:
       orch job follow  (lib/remote_follow.py; recorded on the slot as the run's launcher)
         1. ssh start  (one call, never re-sent)    ──> device: claim .orch-worker-<tag>.start (O_EXCL),
                                                         save stdin, setsid/perl-setsid the wrapper, wait ≤10 s
                                                         for its pid record, print "orch-start: detached", exit 0
         2. ssh follow <offset>  (looped)           ──> device: stream .orch-worker.out from <offset>;
              stdout bytes → our stdout (offset += n)      stderr "orch-follow: alive" every 20 s;
              stderr markers parsed                         at the end "orch-follow: exit <tag> <code>"
              link drop (255 / idle 90 s / timeout)         or "orch-follow: gone" (no exit record) or
                → backoff 5,10,20,40,60,60… s, resume        "orch-follow: not-started" (it fences the tag)
                → give up only past LINK_GRACE (15 min)
         exits with the remote code | 125 not started | 1 gone without a status | 255 link lost past grace

device job dir <workdir_root>/jobs/<job-id>/:
  .orch-worker-<tag>.start   start's claim, or "fenced" by a check/follow (late delivery then refuses 125)
  .orch-worker.pid           "<session pid> <boot id>", written by the detached wrapper (session leader)
  .orch-worker.stdin (600)   the brief, harness stdin
  .orch-worker.out   (600)   harness stdout+stderr (truncated per start)
  .orch-worker.exit          "<tag> <code>", written atomically (tmp + mv) by the wrapper when the harness ends
  ../<job-id>.launched       unchanged (#99 A2): tag written just before exec into the harness
```

The wrapper is `sh`. It is the setsid'd session and group leader, and it
carries `ORCH_WORKER_TAG`. It records the pid file and runs the existing
`bash -lc` harness step as a child, so profile ordering, `command -v` and the
launch record are unchanged. It waits for the harness, forwarding TERM and HUP
to it, then writes the exit record with `128+n` for a signal and exits. The
tag scan in `check`/`stop` still finds the harness and the wrapper, because
both carry the tag.

**Invariants.**
1. The start is sent at most once per tag and is never retried. A start whose
   delivery is uncertain is settled on the device: the first follow or check
   that finds no claim for the tag writes the fence, and a late start then
   refuses (125) without running anything. This applies on Linux and on Darwin.
2. A slot is released only when the worker is confirmed gone. That is the same
   rule as today, plus one more holding condition: a live recorded launcher
   (the follower that owns the start ssh) holds the slot too.
3. A worker's identity on the slot is never overwritten. Takeover is decided
   on the row read inside `attach_runner`'s transaction, and `attach_worker`
   refuses when the slot already names a different worker or another runner.
4. Output is never duplicated or lost across reattach. The follower resumes
   from the exact byte count it wrote, and the waiter's `ORCH-HANDOFF`
   last-line rule (lib/waiter.py) is unchanged.
5. Link loss alone never fails a job inside the grace period. The heartbeat
   keeps beating while the follower reconnects.
6. No new secret surface. Nothing new goes on an argv or into a log line except
   job ids, tags, device names, byte offsets and exit codes. The brief travels
   on stdin, as it does today.
7. A dashboard push never blocks the poll loop. It stays a separate child, and
   backoff is decided by the parent from the child's exit status.

**Interpretation choices** (technical; Factory decides, and the reviewer can push back):
- *Grace period*: `LINK_GRACE_SECONDS = 900` (15 minutes). It is measured from
  the last successful contact, and backoff is capped at 60 s. That is well under
  the 60-minute job timeout and the 60-minute offline rule. After grace, the
  follower exits 255. `finish()` finds the worker UNREACHABLE and holds the slot.
  The waiter leaves the job running because `remote_started` is set, and the
  existing device monitor or the 60-minute offline path settles it. No new
  failure path is added.
- *Detach tool*: the start uses `setsid` when it is on the device's PATH (WSL
  has util-linux). Otherwise it uses `perl -MPOSIX -e 'POSIX::setsid()…; exec @ARGV'`,
  which macOS ships. If neither exists, the start refuses (125). The Darwin
  record (`.orch-worker-<tag>.pid`, with pgid, boot session, tag and lstart) is
  then written by the wrapper, which is the new group leader, not by the
  foreground start.
- *#71 rule*: "no review for a head that has a verdict" can't be absolute. A
  fix that changes nothing and re-readies the same head legitimately needs
  round N+1 (see `_review_retry_waits`). The guard is therefore: a `ready` is
  stale when a trusted reviewer verdict for the same head was posted **after**
  that ready. A head that is not the current PR head is never reviewed, and
  that now includes T6 retry reviews.
- *Out of scope*: the Linear reverse-tunnel exits (they are evidence only, not
  an AC), Mac provisioning, and #103 (done in #104).

## Acceptance criteria → work

| # | Criterion | Work that meets it (phase) | Proof (tests) |
|---|---|---|---|
| 1 | Remote harness detached on the device (setsid, pid file and exit file in the job dir); `orch job run` follows over SSH, reattaches after a drop with bounded backoff; fails only past the grace period or on a failing exit file | P3: `lib/remote.py` detached `start_script`, `follow_script`, wrapper, exit, out and stdin files, fence, git-exclude entries, Darwin path. P4: `lib/remote_follow.py` follower and internal `orch job follow`, `placed_run.argv` spawning it, `started()` recording the launcher, exit-code mapping, grace and backoff constants | `tests/test_remote_detached.py`: harness survives the ssh exit; pid and exit records; drop mid-run → reattach, byte-exact run.log, `ORCH-HANDOFF` last line still read by the waiter → job done; outage shorter than grace → success; outage longer than grace → exit 255, slot held, job not failed by the waiter; remote exit 3 → job failed; worker SIGKILLed with no exit record → failure; SIGTERM to `orch job run` → worker stopped, slot released. Existing `test_remote_start.py`, `test_remote_darwin.py`, `test_waiter_remote.py`, `test_timeout_remote_worker.py` and `test_liveness_requeue.py` updated to the detached contract |
| 2 | #70 F2.1 and F2.2 fixed, with the deterministic overlap and delayed-delivery tests | P2 (F2.1): `DevicePool.attach_runner` returns the slot row read in its transaction; `prepare` runs `_earlier_worker_gone` and `forget_worker` on that row and keeps it as `PlacedRun.placement`; `attach_worker` guards that `runner_pid` is this run and that no other worker is recorded. P3 and P4 (F2.2): device-side per-tag claim and fence (Linux and Darwin); slot columns `launcher_pid` and `launcher_start` written in `started()` before the gate opens; `DeviceMonitor` and `liveness` treat a live launcher as RUNNING; the follower kills and reaps its start ssh on exit or parent death; `finish()` clears the launcher | `tests/test_claim_placement.py::test_overlapping_runner_refuses_after_worker_recorded`: B's read is paused (an injected `placement` hook), A records W and dies, B resumes; B is refused and W's identity is unchanged (local and remote variants). `tests/test_remote_detached.py::test_delayed_delivery_holds_slot`: fake ssh with detached delayed delivery, SIGKILL the runner, reconcile while delivery is pending → slot held; delivery then refuses on the fence (no harness marker ever appears) → next reconcile releases; the variant where delivery wins → RUNNING → orphan stopped → released only after GONE |
| 3 | Dashboard push: short connect timeout; on failure skip the cycle and back off; repeated failures shown in Health | P5: `lib/dashboard_push.py` `SSH_OPTIONS` get `ConnectTimeout=5`, `ServerAliveInterval=5` and `ServerAliveCountMax=2`; `SEND_TIMEOUT_SECONDS` 60 → 20; rsync `--timeout` 30 → 10. `PollPusher` and `LivePusher` read the child's exit status and back off after the k-th consecutive failure for `min(30·2^(k-1), 600)` s, with one log line per failure and a reset on success. `lib/health.py` gets a new component `dashboard-push` (local device): `beat` on success, `checked` on failure, written by `orch dashboard push --once` (`lib/dashboard_cli.py`); expected when the push transport is not `none`; stale after one failed push, down when the last success is older than `DOWN_BEATS` intervals, with a "failing since …" line on the Health tab and the needs-attention line | `tests/test_dashboard_push.py`: argv carries the new timeouts. `tests/test_dashboard_poll_push.py` and `test_dashboard_live.py`: a failed child → the next cycles are skipped until the backoff ends; the backoff doubles and is capped; success resets it; the live pusher honors the backoff. `tests/test_health.py`: one failure → stale with detail; repeated failures → down and needs attention; a success clears it |
| 4 | Re-check #71 on current main; test proves it fixed, or fix it | P6: write the regression tests first against current main. Then, only for the cases that fail: in `_t2`, a ready older than a trusted verdict for the same head → `stale-ready-verdict` (handled, nothing queued); in `_t6`, a failed review whose head is no longer `ticket.current_head` → `retry-skipped` (reuses `_skip_review_retry`), without waiting for a deferred fix | `tests/test_dispatcher.py`: (a) after a poller restart, an unhandled r1 ready for head A, with a REVISE verdict for A posted after it and PR head still A → no review job; (b) the same ready, PR head now B → `stale-ready-head`, no job (expected to pass already); (c) a new ready for A posted after the verdict → round 2 queued (a no-change fix is still reviewed); (d) a failed review for A whose PR head is now B, with no fix job → no T6 retry review. Each test's comment says whether it passed on main before the change |
| 5 | Tests use fake ssh with delayed and dropped links | P1: `tests/fake_ssh.py` gets per-alias control files in `$FAKE_SSH_HOSTS`: `<alias>.delay` (sleep N s before running, in the client), `<alias>.delay-detached` (the delivery is already at "sshd": it runs in its own session, so killing the client does not cancel it), `<alias>.drop-after` (the client exits 255 after N s while the remote script keeps running), and `<alias>.offline` toggled mid-test for an outage window. Helpers go in `tests/conftest.py` | Used by every test in rows 1 and 2. `tests/test_fake_ssh.py` checks each mode on its own |

## Phases (one commit each; the suite stays green after each)

1. **Fake ssh links** (`tests/fake_ssh.py`, `tests/conftest.py`, `tests/test_fake_ssh.py`).
   This phase adds modes only. Existing behavior is unchanged when no control file is present.
2. **#70 F2.1** (`lib/device_pool.py`, `lib/placed_run.py`, `tests/test_claim_placement.py`).
   Test first. `attach_runner` → `Placement`. `prepare` uses it. `attach_worker`
   gets its guard. Any caller of `attach_runner` that ignores the return value
   keeps working.
3. **Detached start on the device** (`lib/remote.py`). *(Waits for #100's merge.)*
   - `start_script`: per-tag claim (O_EXCL, refuse 125 if the claim is fenced or
     already exists); stdin → `.orch-worker.stdin`; truncate `.orch-worker.out`;
     remove any stale exit record; detach the wrapper (setsid, perl, else 125);
     poll ≤10 s for the wrapper's pid record (Linux) or Darwin record naming the
     tag. Print `orch-start: detached <pgid>` and exit 0. Exit 125 if the wrapper
     refused.
   - `check_script` and `stop_script`: when no tagged process is found and the
     tag has no claim, write the fence (O_EXCL) and exit 3. A fenced claim
     follows today's pid-record path to 0. The Darwin check keeps its record
     fence. A tagged process means 1, as now.
   - `follow_script(offset)`: POSIX sh, portable `tail -c +N` / `head -c`. It
     streams new bytes every 2 s, emits `orch-follow: alive` on stderr every
     20 s, and ends with `exit <tag> <code>` once the exit record names the tag
     and no tagged process remains (bounded 10 s wait). It ends with `gone` when
     no tagged process remains and there is no exit record, and with
     `not-started` (after writing the fence) when there is no claim.
   - The checkout's `.git/info/exclude` gets `/.orch-worker*`.
   - `SSH_FOLLOW_OPTIONS` = `SSH_OPTIONS` with `ServerAliveInterval=15`, `ServerAliveCountMax=3`.
   - Script-level tests run against fake ssh with a real `sh` (Linux path) and
     the existing Darwin stand-ins.
4. **Follower and runner wiring** (`lib/remote_follow.py` new; `lib/placed_run.py`,
   `lib/cli.py` internal `job follow`, `lib/device_pool.py` launcher columns
   added in place, `lib/device_monitor.py`, `lib/job_state.py` liveness).
   *(Waits for #100.)*
   - The follower is a pure loop over an injected transport, clock, sleep and
     output, so the backoff and grace tests run without real time.
   - It exits when its parent dies (it checks `getppid()` each wait), and it
     kills and reaps its own ssh children. On SIGTERM or SIGHUP it exits
     143/129 at once, and `finish()` then stops the worker as today.
   - Its own log lines start with `orch job run:`, so the waiter's last-line
     rule skips them.
   - `PlacedRun.started(pid)` on a remote run records the launcher. `finish()`
     clears it in the same transaction that releases or holds the slot.
5. **Dashboard push backoff and Health** (`lib/dashboard_push.py`, `lib/dashboard_cli.py`,
   `lib/health.py`, tests).
6. **#71** (`lib/dispatcher.py`: `_t2` and `_t6` only; `tests/test_dispatcher.py`).
   Regression tests first. Only the guards they show are missing get added.
7. **Docs** (`docs/SPEC.md`): the F2-02 section (detached start, follow and
   reattach, grace, fence, launcher), the T6 row (link loss inside grace is not
   a failure; no retry review for a replaced head), a dated decisions row for
   the interpretation choices above, and the Health component list.
   `README.md` is updated only where it describes the remote start.

## Risks and how the plan handles them

- **Running jobs at deploy time.** A start from before the upgrade has no claim
  file. While it runs, its tagged processes answer RUNNING as before. Once it
  ends, the first check writes the fence and answers unsure, and the next
  answers gone. Release is one poll later, and nothing else changes. A test
  covers this: an old-style start, then the new check.
- **The wrapper is killed hard (SIGKILL, reboot).** No exit record is written.
  The follower reports `gone`, which counts as a failure (exit 1), and the
  boot-id check in `.orch-worker.pid` handles a reboot as it does today.
- **The follower outlives a SIGKILLed `orch job run`.** Its live launcher row
  holds the slot. It sees its parent die within one wait, kills its ssh
  children and exits. The device fence settles any start still in flight.
- **Large output.** It is streamed in chunks. The byte offset is the only
  state. `.orch-worker.out` stays on the device, mode 600, and is truncated by
  the next start of the same job.
- **#100 conflicts.** Phases 3 and 4 start only after #100 merges into `main`
  and `main` is merged here. The follower's stdout is the harness's stream, so
  the telemetry tee needs no change.

## Verify

`env -u ORCH_DB python -m pytest -q`, the `check-config` steps and the
statechart check, as in `.github/workflows/ci.yml`. CI is the gate.
