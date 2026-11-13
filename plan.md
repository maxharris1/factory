# Plan for #134: one Linear webhook listener and one tunnel, however many times `ensure` runs

PR #136, branch `factory/issue-134`, risk low. Claude builds and Codex reviews.

## What went wrong

`~/.local/share/factory/linear-webhook-service.sh` lives only on the box. Every bot shell runs its `ensure` in the background. `ensure` decides whether a loop is running only from a pidfile. Each new `_listener-loop` / `_tunnel-loop` then writes its own pid into that pidfile at startup, so older loops become invisible. `stop` kills only the pid in the pidfile.

The log on the box shows why the pidfile check fails. `linear-webhook-service.log` has lines like `started _listener-loop (pid 43)` and `started _tunnel-loop (pid 75)`. Pids that small come from a separate PID namespace: a sandboxed bot shell running the same profile hook against the same state directory. A pid written from one namespace means nothing, or names an unrelated process, in another. So each namespace decided that nothing was running and started its own loops. `service_lock` serializes the `ensure` calls, but it can't help while the check itself is pid-based.

`flock` locks belong to the open file, not to a pid, so they work across PID namespaces on a shared filesystem. That is why the fix below rests on a per-loop lock and not on the pidfile. `orch poll` already works this way: `_poll_lock` in `lib/cli.py` takes `orch.db.lock` and writes the pidfile only after it holds the lock.

`poller-ensure-loop.sh` has the same flaw. It lives only in `~/.local/state/factory/`, and it overwrites its pidfile at startup with no lock. The `another orch poll owns .../orch.db.lock` lines in `poller.log` (162 so far) come from `poller-service.sh ensure`. When its `running_pid` check can't see the live poller's pid, it launches a second `orch poll`, and the poll lock refuses it. That refusal is harmless, but every refused launch is a wasted process start and a log line.

## Scaffold (units of work)

### Unit 1: bring the script into the repo as it is
- Add `deploy/linear-webhook-service.sh` as a byte-for-byte copy of the installed script, in its own commit. The review diff then shows only the behaviour changes from Units 2 to 4.

### Unit 2: each loop runs as a singleton
- New lock files beside the state: `${STATE}/linear-webhook-listener.lock` and `${STATE}/linear-webhook-tunnel.lock`.
- `_listener-loop` and `_tunnel-loop` first open their lock on a dedicated fd (`exec 8>>"$LOCK"`) and run `flock -n 8`. If the lock is held, the loop logs `<mode> already running (lock held); exiting` and exits 0 before it touches the pidfile, ssh or serve. The loop holds the lock for its whole life.
- The pidfile is written only after the lock is taken, the same rule `_poll_lock` follows. It is kept for display and for `status`; it no longer decides anything.
- Child processes (`orch linear-webhook serve`, `ssh`, `sleep`) run with `8>&-`, so the lock lives exactly as long as the loop. A SIGKILLed loop frees its lock at once. Any orphaned `serve` still holding the port is handled by Unit 3's port check.
- `ensure` and `start`, still under `service_lock`, probe each loop lock with `flock -n` on a separate fd. If the lock is held, they report `already running` and spawn nothing. If it is free, they release the probe and launch. `launch_one` treats "lock now held" (not "pidfile names a live pid") as the sign that the loop started. The loop's own `flock -n` is the final guard against a race in the probe-release-launch gap.
- If there is no `flock(1)`, the loops log the reason and exit 1, so `ensure` reports a failed start. The box has util-linux. The old pidfile-only behaviour is what caused this incident, so there is no silent fallback to it.

### Unit 3: port check and backoff
- `LINEAR_WEBHOOK_PORT` (default `8787`) is used for both the tunnel `-R 127.0.0.1:P:127.0.0.1:P` and `serve --bind 127.0.0.1:P`, so the port that is checked is the port that is served.
- The listener loop checks the port before each `serve` start with a bash `/dev/tcp/127.0.0.1/P` connect. If something already listens there, the loop does not start `serve`. It logs once per streak (`127.0.0.1:P already has a listener; not starting serve`) and waits with backoff. As a result, `linear_webhook_health.started_at` is no longer rewritten on every failed bind.
- Both loops use the same backoff: `LINEAR_WEBHOOK_RETRY_SEC` (default 5) doubles after each fast exit (a run under 30 s), capped at `LINEAR_WEBHOOK_RETRY_MAX_SEC` (default 60). The delay resets after a run that lasted at least 30 s.
- The tunnel loop sends ssh stderr to the tunnel log through a temporary file. When it sees `remote port forwarding failed`, it logs `forward refused: port P already forwarded on <host>; retry in Ns`, so the backoff has a stated cause. Every fast failure backs off this way, so the forward-failure case cannot hammer the PC's sshd again.

### Unit 4: `stop` finds every loop
- `stop` collects pids from two sources:
  - Any process whose argv is `<...>/linear-webhook-service.sh _listener-loop` or `_tunnel-loop`, matched by script basename plus mode in argv[1..2] and read from `/proc/*/cmdline`. Basename matching also catches loops from the old installed copy and the 27/25 kind of duplicates.
  - The holder of each loop lock, read from `/proc/locks` by matching the lock file's inode and then checking its cmdline.
- `stop` runs `kill_tree` on each pid, waits, and SIGKILLs any that remain (the existing `wait_gone`).
- `stop` then probes both locks. If a lock is still held by a process this namespace can't see, `stop` says so and exits 1. It no longer claims success.
- `status` reports "running" from the lock probe and shows the pidfile pid as information only.

### Unit 5: install the script from the repo
- `deploy/install.sh` installs `deploy/linear-webhook-service.sh` with `install -m 0755` to `${HOME}/.local/share/factory/linear-webhook-service.sh`, the path the live profile and running loops already use.
  - A copy, not a run-from-checkout, so that `git pull` never rewrites a script that a live bash loop is still reading.
  - It also keeps the `stop` cmdline match in Unit 4 pointed at the old loops.
- The profile that `install.sh` writes gains the Linear block that today exists only in the hand-edited live profile: run `ensure` in the background when `~/.config/factory/linear-webhook-autostart` exists, logging to `linear-webhook-service.log`.
- `install-autostart` in the script stays as it is (it writes the flag only).
- README "Linear webhook" section: describe the service script, its locks, `LINEAR_WEBHOOK_PORT` and the backoff knobs. Add a deploy note: re-run `deploy/install.sh`, then run `linear-webhook-service.sh restart` once, so that the new `stop` clears any old loops.

### Unit 6: audit `poller-ensure-loop.sh` and poller `ensure`
- Add `deploy/poller-ensure-loop.sh`. It is the live script, plus:
  - an exclusive `flock -n` on `${STATE}/poller-ensure-loop.lock` for its whole life (if the lock is held it exits 0);
  - the pidfile written after the lock is taken;
  - `poller-service.sh` resolved next to the script (`${HERE}`).
- Starting it stays manual, as today. Nothing new autostarts it.
- `deploy/poller-service.sh`: `ensure` and `launch` probe `$(dirname ORCH_DB)/orch.db.lock` (the lock `_poll_lock` takes) with `flock -n` before launching. If the lock is held, they report `orch poll already running (poll lock held)` and launch nothing. That ends the `another orch poll owns` spam even when the poller's pid is invisible from the caller's namespace. Without `flock(1)`, behaviour is unchanged.
- Risk note: this edits poller `ensure`, not the poller install or autostart lines that factory.md classes as high risk. The change only skips a launch that `orch poll` would refuse anyway.

### Unit 7: tests
New `tests/test_linear_webhook_service.py`. It runs `deploy/linear-webhook-service.sh` with a temporary `HOME`/`ORCH_STATE_DIR`, a dummy secret file, and fake `ssh` and `orch` in `$HOME/.local/bin` that log each call and sleep. It skips when `flock(1)` is missing, like `test_poller_service.py`.
- Concurrent `ensure`: start N=8 `ensure` processes at once and wait for them. Exactly one `_listener-loop` and one `_tunnel-loop` exist (counted from `/proc` by script path and mode), fake `orch serve` was started once, and fake `ssh` was started once. Teardown runs `stop`, and after it no loop remains.
- Held lock: the test holds `linear-webhook-listener.lock` (then the tunnel lock) with `fcntl.flock`. Running `<script> _listener-loop` (or `_tunnel-loop`) directly exits 0 within a second, the fake `orch`/`ssh` logs stay empty, and the pidfile is unchanged.
- Port held: the test binds a listening socket on a free port and sets `LINEAR_WEBHOOK_PORT` to it. The listener loop never starts fake `serve`, and the log has the "already has a listener" line.
- Tunnel backoff: fake `ssh` prints `remote port forwarding failed for listen port P` and exits 255. With `LINEAR_WEBHOOK_RETRY_SEC=1` and `LINEAR_WEBHOOK_RETRY_MAX_SEC=4`, the logged retry delays are `1, 2, 4, 4`.
- `stop` finds old loops: start a stub `linear-webhook-service.sh` in another directory that only sleeps when given `_listener-loop` (simulating the old installed copy and duplicates), several times, plus one real loop. `stop` leaves none alive and both locks free.
- `tests/test_deploy.py`: `install.sh` installs the script at `~/.local/share/factory/linear-webhook-service.sh` with mode 0755. The written profile runs its `ensure` only when `linear-webhook-autostart` exists, using the existing `_profile_env` pattern.
- `tests/test_poller_service.py`: with `orch.db.lock` held by the test and no pidfile, `ensure` launches nothing (the fake runner is never started) and says the poll lock is held. With `poller-ensure-loop.lock` held, a second `deploy/poller-ensure-loop.sh` exits 0 without calling `ensure`.
- Run the whole suite with `env -u ORCH_DB pytest`.

## Acceptance criteria map

| # | Ticket criterion | Met by |
|---|---|---|
| 1 | Move `linear-webhook-service.sh` into the repo at `deploy/linear-webhook-service.sh`, installed by `deploy/install.sh` | Unit 1 (copy in its own commit), Unit 5 (`install.sh` installs it, profile block, README); test in `test_deploy.py` |
| 2 | Each `_listener-loop` / `_tunnel-loop` takes an exclusive non-blocking `flock` on its own lock file for its whole life and exits at once if it is held; `ensure`/`start` check that lock, not just the pidfile | Unit 2; tests "concurrent `ensure`" and "held lock" |
| 3a | Listener loop doesn't start `serve` while 127.0.0.1:8787 already has a listener | Unit 3 (port probe before every `serve`); test "port held" |
| 3b | Tunnel that fails with `remote port forwarding failed` backs off (5 s → 60 s cap), not every 5 s | Unit 3 (doubling backoff, forward-failure log line); test "tunnel backoff" |
| 4 | `stop` kills every process holding the loop lock (or matching the loop's cmdline under the state dir), not only the pidfile pid | Unit 4 (cmdline scan + `/proc/locks` holder, lock re-probe); test "`stop` finds old loops" |
| 5 | Same audit for `poller-ensure-loop.sh` (logs `another orch poll owns ... orch.db.lock` every cycle) | Unit 6 (singleton `deploy/poller-ensure-loop.sh`; `poller-service.sh ensure` probes the poll lock); `test_poller_service.py` tests |
| 6a | Test: run `ensure` concurrently N times; exactly one listener loop and one tunnel loop exist | Unit 7 "concurrent `ensure`" (N=8) |
| 6b | Test: with a held lock, a second loop exits 0 without spawning ssh or serve | Unit 7 "held lock" (both loops) |

Every criterion is mapped.

## Out of scope / follow-ups
- Stopping a lock holder in another PID namespace. `stop` run from inside a sandbox can't see host pids, so it reports the held lock and exits 1. Run `stop` from a host shell.
- Whether sandboxed bot shells should run the profile autostart hooks at all. With the locks in place they are harmless. Skipping them would mean detecting the sandbox in `profile.sh`, which is a separate call.
- No change to `orch linear-webhook serve` itself or to `linear_webhook_health`.
