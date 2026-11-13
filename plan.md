# Plan: #107 Ops, health and self-heal (PR #111)

Ticket: https://github.com/maxharris1/factory/issues/107 (folds in #79 and #69).
Branch `factory/issue-107` off `main`. Risk high (it changes the poller install).
Builder: claude-bot. Reviewer: codex-bot.

## Architect scaffold

**Goal.** The factory finds and fixes its own down or stale-code components with
a deterministic timer that uses no LLM and no tokens. It also removes the drift
and false alarms listed in the ticket: three poller launchers, a red cursor-bot
row, a tunnel row with no tunnel, the Linear silence warning, bare poll errors,
uncommitted live config, CI running twice, and "running 0s".

**Shape.** The new self-heal splits into a thin shell timer and a pure Python
decision engine:

```
systemd user timer (or the profile.sh-supervised loop), every 120 s
  -> deploy/self-heal.sh once                (flock: runs never overlap)
       orch health --json  ──stdin──>  orch heal --health-json -
                                         lib/heal.py
                                           decide(report, state, facts, now) -> [Action]   (pure)
                                           run each Action through its service script
                                           append to self-heal.log, save self-heal.state.json
                                           escalate: POST JSON to FACTORY_WEBHOOK_URL
```

`decide()` is a pure function, so tests cover every rule (down, stale code,
idle, held-stopped, not owned, backoff, flap, escalate) without processes.
`orch heal` runs only the service scripts in `deploy/`, with fixed argv lists.
It never sends a signal itself and never touches `orch job run`.

**Invariants.**
1. Self-heal never kills an `orch job run`. It only calls `poller-service.sh`,
   `waiter-service.sh <bot>` and `linear-webhook-service.sh`. The waiter
   script already stops only its supervisor and the lock-holding
   `orch wait <bot> --autostart` pid. Each job runs in its own session.
2. Self-heal never overrides an operator stop. A component held by its
   `*.stopped` marker is skipped and logged once.
3. One account restarts only what it owns. Ownership comes from the autostart
   flag files in `~/.config/factory` (`poller-autostart`,
   `waiter-autostart-<bot>`, `linear-webhook-autostart`), and only for rows
   on the local device. Waiters run as their own bot users, and this rule
   keeps one user from acting on another user's services.
4. No secret reaches a log, stdout, a heartbeat row or the escalation body.
   The webhook URL and auth values are read from the environment only. Errors
   name the env variable or an HTTP status, never a value.
5. Only `poll_errors` writes change in `lib/dispatcher.py`. Runner files
   (PR #100 and #104) stay untouched.

**Interpretation choices (technical; Factory decides, and the reviewer can push back):**
- *"Refused or not in any project's routing"* (c): routing in this repo is
  chosen per job from each project's remote `factory.md`, and health can't
  read that cheaply or deterministically. So orch.yml gets an optional
  top-level `refused_harnesses: [cursor]` list. A harness counts as routable
  when no `refused_harnesses` entry names it and some enabled device in
  machines.yml lists it.
- *Local override (i)*: the override files sit **beside the file they
  override**: `orch.local.yml` next to `orch.yml` and `machines.local.yml`
  next to `machines.yml`, both gitignored. The ticket suggested
  `~/.config/factory/`, but that is per user. The poller and each bot's
  waiter run as different users, and they must all see the same
  `machines.yml` slots and `enabled` flags.
- *linear-webhook-service.sh (e)*: the live copy is in
  `~/.local/share/factory` on the box, and the builder (pc) can't read it.
  The repo version is written fresh, modeled on `waiter-service.sh`, with the
  same commands. The reverse-tunnel shape lives in a host-local
  `~/.config/factory/linear-webhook.env` (`LINEAR_TUNNEL_SSH_ALIAS`,
  `LINEAR_TUNNEL_REMOTE_PORT`). Without them the service runs the listener
  only. At install, Factory checks the live script against the repo one
  (README install step) before retiring the live copy.

## Work items

### W1. Stale-code detection (criterion b)
- `lib/health.py`: add `LOADED_VERSION = factory_version()` at import time, so
  a beat records the HEAD the process loaded, not the HEAD at beat time.
  `Beater`, `DeviceProber`, `TunnelProber` and `beat_once` use it as their
  default version.
- `Component` gets a `stale_code` property: true when the component is
  `poller`, `waiter` or `linear-listener`, its version and the checkout's
  `factory_version(ROOT)` (read once per report) are both known, and they
  differ. Device-probe and tunnel rows are facts about their targets, and
  the dashboard writer is a fresh process for each push, so none of the
  three is ever flagged.
- `build_report` takes `checkout_version` (default
  `factory_version(ROOT)`). `report_json` adds `"code_version"` at the top
  level and `"stale_code"` for each component, plus `"started_at"` (epoch)
  for the flap rule in W3.
- `render_lines` (used by `orch health` and the Health section of
  `orch status`) appends `stale code (running <ver>, checkout <ver>)`. The
  header counts them: `Health (N components, D down, S stale code)`.
- Dashboard Health tab (`lib/dashboard_page.py`, both the JS renderer and the
  static Python renderer): the version cell shows `stale code` next to the
  version. A stale-code component is not a needs-attention item; self-heal
  owns it.

### W2. Health expectations and help text (criteria c and d)
- `lib/orch_yml.py`: optional `refused_harnesses` (a list drawn from
  `HARNESSES`, unique, default empty). Unknown names are an error.
  `config/orch.example.yml` and `orch.yml` document it, and `orch.yml` sets
  `refused_harnesses: [cursor]`.
- `expected_components()`: skip the waiter slot for any bot whose harness is
  refused. A refused bot's leftover row shows as `not configured`, not down.
- `expected_components()`: add the `tunnel` slot only when
  `orch.linear_webhook.public_url` is set. The listener slot stays whenever
  `linear_webhook` exists.
- `lib/cli.py`: the top-level help changes from "report, beat or forget" to
  "report or forget". The existing tests already require
  `orch health beat` to exit 2.
- README "Health" and SPEC section 9 are updated to match.

### W3. Self-heal (criterion a)
- **`lib/heal.py`** (new) and **`orch heal`** (wired in `lib/cli.py`):
  - `orch heal --health-json PATH|- [--state DIR] [--deploy DIR] [--dry-run]`
  - `Facts`: local device, checkout version, the owned set (from flag
    files), the held-stopped set (`poller.stopped`, `waiter-<bot>.stopped`,
    `linear-webhook.stopped` in the state dir), and listener idleness
    (`linear_webhook_health.last_received_at` older than
    `REQUEST_DEADLINE_SECONDS`).
  - `decide(report_json, state, facts, now) -> list[Action]`. The rules run
    per component key `(component, bot, device)`:
    1. Not on the local device, not owned, or `configured: false`: no action,
       not logged.
    2. Held stopped: `skip(held)`, logged once per change.
    3. `state == down`: `restart(down)`.
    4. `stale_code` and the component is idle: `restart(stale-code)`. If it
       is not idle, `defer(busy)` and try again next run. Idle means:
       - **poller**: always. W3b makes the poller's own SIGTERM wait for the
         cycle boundary.
       - **waiter**: always. `orch wait` already blocks SIGTERM through
         claim and start (`lib/wait.py`).
       - **listener**: no delivery in the last 10 s.
    5. Settling: a key restarted less than 60 s ago gets no action, so a
       restart is not counted twice before its first beat.
    6. Backoff: `MAX_ATTEMPTS = 3` restarts per `WINDOW = 30 min` per key.
       Past that, `skip(backoff)` until the oldest attempt leaves the window.
    7. Flap: 3 or more pid or `started_at` changes for a key inside the
       window that self-heal did not cause (for example, a waiter supervisor
       crash-looping) count as flapping.
    8. Escalate once per key per window when backoff trips, when a
       restart command exits non-zero three times, or when the component
       flaps.
  - Actions run through the component's own service script, with argv lists
    only:
    - poller: `deploy/poller-service.sh restart`. This goes through
      `run-poller.sh` (W6).
    - waiter: `deploy/waiter-service.sh <bot> restart`.
    - listener and tunnel: `deploy/linear-webhook-service.sh restart`.
    - The script's output goes to that service's own `*-service.log`. Only
      the exit code and duration reach `self-heal.log`.
  - Log: `${STATE}/self-heal.log`, one line per action:
    `2026-11-13T14:02:00Z self-heal: restart waiter codex-bot @ box (down: no heartbeat for 3m) exit=0 2.1s`.
    Skips and defers are logged only when a key's decision changes, so the
    log never repeats one line per run.
  - State: `${STATE}/self-heal.state.json` holds attempts, last pids and
    started_at values, escalations and the last logged decision for each
    key. It is written atomically (tmp file, then rename). A corrupt file is
    logged and replaced with a fresh one.
  - Escalation: POST to the URL in orch.yml `grok.webhook_url_env`
    (FACTORY_WEBHOOK_URL), with the auth header built the way
    `lib/grok_delivery.py` builds it. The small auth helper there is
    factored out and shared, so the behavior doesn't change. The body is
    `{"action":"self-heal","kind":"escalation","logical_key":"self-heal:<device>:<key>:<window start>","target_bot":"grok-bot","device":…,"components":[{"label","state","reason","attempts","flapping"}],"summary":"…"}`.
    A missing env value logs `escalation skipped: missing-env:FACTORY_WEBHOOK_URL`
    and a failed POST logs `escalation failed: HTTP-<n>` or the exception
    class. Neither line holds the URL or any auth value. Bot-user accounts
    have no `webhook.env`, so they log the skip. That is expected, and the
    box user's run escalates the box components.
- **`deploy/self-heal.sh`** (new): `once`, `loop`, `ensure`, `stop`,
  `status`, `install-autostart` and `remove-autostart`, in the same style
  and with the same pidfile, flock and stop-marker rules as
  `poller-service.sh`.
  - `once` takes `self-heal.lock` (a run never overlaps another), reads
    `webhook.env` into the environment only when this user can read it
    (`set -a` and never echoed), then runs
    `orch health --json | orch heal --health-json -`. Exit 3 from health is
    normal.
  - `loop` runs `once` every `ORCH_HEAL_SECONDS` (default 120) under a
    pidfile. `ensure` starts the loop unless it is already running or held
    stopped, and prints nothing when it is running.
- **Install.** The systemd user units
  `deploy/factory-self-heal.service` and `deploy/factory-self-heal.timer`
  (`OnBootSec=1min`, `OnUnitActiveSec=2min`) run `deploy/self-heal.sh once`.
  `install.sh` installs and enables them when `systemctl --user
  show-environment` succeeds. Otherwise it writes the
  `self-heal-autostart` flag, the generated `profile.sh` runs
  `self-heal.sh ensure`, and a crontab `@reboot` plus 5-minute `ensure`
  watchdog is tagged `# factory-self-heal`, replaced and never duplicated,
  with the same unreadable-crontab refusal as the poller.
- **W3b. The poller stops at the cycle boundary** (`lib/cli.py` only, no
  dispatcher edit): `_poll` installs a SIGTERM handler and passes `PollLoop`
  a `sleep` wrapper. A SIGTERM during the sleep exits 143 at once, as it
  does today. A SIGTERM during a cycle sets a flag, and the loop exits 143
  at the next sleep, so no handoff or ship is cut in half. A second SIGTERM
  exits at once. The stop timeout for self-heal restarts is raised through
  `ORCH_POLL_STOP_WAIT=300`, set by `orch heal` for that call only.

### W4. linear-webhook-service.sh in the repo (criterion e)
- `deploy/linear-webhook-service.sh` follows the `waiter-service.sh`
  pattern. A supervisor loop owns `orch linear-webhook serve`, and the
  reverse tunnel `ssh -N -o ExitOnForwardFailure=yes -o
  ServerAliveInterval=30 -R <remote_port>:<bind> <alias>` when
  `linear-webhook.env` sets them. Each child gets the same exponential
  restart pause (5 s up to 60 s).
- Commands: `start|stop|restart|status|ensure|install-autostart|remove-autostart`.
  The pidfile, `linear-webhook.stopped` and `linear-webhook-service.lock` live
  in `~/.local/state/factory`. Output goes to `linear-webhook.log`, and every
  line the script writes is `say()`-timestamped in UTC ISO 8601.
- `ensure` prints nothing and exits 0 when the service is running or held
  stopped. That stops the roughly 2,500 "already running" lines.
- `install.sh` makes it executable in place and runs it from the repo. With
  `--autostart`, or when `linear-webhook-autostart` already exists, the
  generated `profile.sh` gets an `ensure` hook.

### W5. Linear warning, #79 and #69 (criterion g)
- **Timestamps.** `orch linear-webhook serve` passes `WebhookApp` a log
  function that prefixes `YYYY-MM-DDTHH:MM:SSZ ` (UTC). The startup
  "listening on" line gets the same prefix.
- **Any signed delivery counts.** `WebhookApp._handle` updates
  `linear_webhook_health.last_received_at` as soon as the HMAC check passes,
  before the body is parsed. That covers stale-timestamp, malformed,
  ignored and Attachment events too, with event `-` until the body parses.
- **Per-issue delivery record.** A new table
  `linear_webhook_seen(issue_id PK, received_at)` is upserted for every
  signed, fresh delivery that names an issue id: `data.id` for an Issue,
  `issueId` or `issue.id` for a Comment, accepted or ignored.
- **Missed-delivery detection.** This is done by the reconcile poll in
  `lib/poll_source.py`, which may touch that file only. When the poll
  directly reads a Linear issue in webhook mode, a pure helper
  `missed_delivery(newest_comment_at, seen_at, listener_started_at, now)`
  finds a miss when all four hold:
  1. the issue's newest comment (`comments.nodes.createdAt`, already
     fetched) is at least 60 s after the listener started;
  2. that comment is at least 120 s old;
  3. no `linear_webhook_seen` row for the issue is at or after
     `createdAt - 60 s`;
  4. the issue is in a configured project.

  A miss upserts `linear_webhook_missed(issue_id PK, identifier, event_at,
  found_at)` through new `LinearDirtyQueue.seen_at()` and `record_missed()`
  methods.
- **Health.** `WebhookHealth` gets `missed_24h` and `newest_missed`
  (identifier and found_at). `silence_warning()` is replaced by
  - `warning(now)`, which is set only when a miss was found in the last 24 h:
    `Linear webhook missed N deliver(y|ies) in 24h (newest AUT-123, found 12m ago)`;
  - `quiet_note(now)`: when nothing was missed but no signed delivery came
    in for over 1 h while Linear tickets are active:
    `quiet: no Linear webhook for 2h (nothing missed by the last reconcile)`.
- `orch status` prints `warning:` or `note:`. In the dashboard snapshot
  (`lib/dashboard.py`), `warning` holds only a miss and is the only one that
  feeds needs-attention. A new `note` field is shown muted on the webhook
  panel.
- **#69 F2.1.** `linear_project_issues` gets a `project_id` column (an
  idempotent `ALTER TABLE ... ADD COLUMN`). `_note_project` stores it, and
  `_in_configured_project` admits a marker Comment only when the stored
  project is in the current `self.project_ids`. A legacy row with a NULL
  project is not admitted. Test first: one database with project A
  configured gets a signed Issue event, then a restart with A removed. A
  marker Comment is then ignored and no dirty row is written.
- **#69 F2.2.** `test_unsigned_body_fails_the_signature_before_any_parse`
  monkeypatches `lib.linear_webhook.json.loads` with a counter. A bad
  signature gives 401 and zero calls.

### W6. One poller launcher (criterion f)
- `deploy/run-poller.sh` stays the only launcher. It now checks that both
  env files are readable before it sources them. If one isn't, it exits 78
  with `run-poller: missing ~/.config/factory/<file>` (the name only) and
  never starts a poller without its env. That was the 7:13 PM
  `missing-env:FACTORY_WEBHOOK_URL` incident.
- `deploy/orch-poll.service` has `ExecStart=%h/factory/deploy/run-poller.sh`,
  so the systemd path also goes through it and its `EnvironmentFile` lines
  are dropped. `poller-service.sh` already runs `RUNNER=run-poller.sh`, and
  a test pins that.
- `install.sh` retires stale launchers. For `~/.local/share/factory/run-poller.sh`
  and `~/.local/share/factory/linear-webhook-service.sh`, it:
  1. sends SIGTERM to any process whose `/proc/<pid>/cmdline` runs exactly
     that file (it never matches on pattern);
  2. removes crontab lines that name those paths, refusing as poller-service
     does when the crontab can't be read;
  3. deletes the files;
  4. prints what it did.

  The legacy listener is stopped before the repo service's `ensure`, so
  there is never a double bind.

### W7. Full poll error messages (criterion h)
- `lib/dispatcher.py`, poll_errors writes only: `record_poll_error` and
  `_record_job_error` accept `str | BaseException`. An exception goes
  through a new `poll_error_text(exc)`. That function reuses
  `safe_ship_error`'s scrubbing (URLs, hosts, token shapes, credential
  assignments, bodies, non-printables) with `POLL_ERROR_LIMIT = 500`.
  Strings are scrubbed and capped too. The call sites that pass
  `type(exc).__name__` pass `exc` instead, and prefixes such as
  `timeout-held:` and `job-states:` are kept.
- `lib/cli.py` `on_error` passes the exception. `lib/poll_source.py`
  `_safe_reason` keeps its `PollSourceError:` and `X:HTTP-nnn` prefixes and
  appends the scrubbed message. The dashboard's LIKE matches
  (`Linear returned HTTP 429`, `Linear poll budget is exhausted`) keep
  working, and a test pins that.
- PollCycleError's message also names the failing sources, for example
  `poll cycle recorded 2 source error(s): github x1, linear x1`.

### W8. Live config in the repo (criterion i)
- `lib/config_overlay.py` (new): `load_with_overlay(path)` reads
  `<stem>.local.yml` beside `path` when the file exists and deep-merges it
  into the parsed base before validation:
  - mappings merge recursively;
  - `projects` merge by `repo`, and `devices` by `name` (fields override,
    new entries append);
  - other lists are replaced;
  - a malformed overlay is an error finding that names the overlay file.

  `load_orch_yml` and `load_machines_yml` use it, with `overlay=True` by
  default. `ORCH_NO_LOCAL_OVERLAY=1` turns it off. `tests/conftest.py` sets
  that variable autouse, and the overlay tests clear it.
- `check-config` names the overlay in use and the keys it sets (names only,
  never values), for example
  `overlay orch.local.yml sets: linear_webhook.public_url, projects[aut0-code/aut0].linear_project_id`.
  With the overlay applied, placeholders it fills are no longer reported.
- `.gitignore` adds `orch.local.yml` and `machines.local.yml`.
  `config/orch.local.example.yml` and `config/machines.local.example.yml`
  document the shape. They show the keys the box currently edits by hand
  (Linear project id, `trusted_linear_users`, `linear_webhook`
  path/public_url, a project's `ready_starts_review`, device `slot_max` and
  `enabled`), with placeholder values.
- The `install.sh` live edits are now in the repo's `install.sh`:
  - the generated `profile.sh` has a re-source guard
    (`FACTORY_PROFILE_LOADED`) and runs `mkdir -p` on the state dir before
    any `>>` redirect;
  - the listener autostart hook (W4) and the self-heal hook (W3) are added
    to `profile.sh`.
- README "Install after merge" gets a migration step for Factory on the box.
  Move the uncommitted edits into `orch.local.yml` and `machines.local.yml`,
  run `orch check-config` and compare the reported keys, then
  `git checkout -- orch.yml machines.yml deploy/install.sh`. After that,
  `git pull` can't conflict.

### W9. CI once per push (criterion j)
- `.github/workflows/ci.yml`: `on: push: branches: [main]` plus
  `pull_request:`. The poller reads check runs by commit
  (`github_actions.py` `check-runs`), not by event, so PR heads still get
  their CI result.

### W10. Status ages (criterion k)
- `lib/status.py`: a running job's age counts from its claim, which is the
  slot's `placed_at` and is already read into `self._slots`. A job with no
  placement and a Grok wake count from `created_at`. Each line also shows
  the last heartbeat:
  `aut0-code/aut0#12 build -> claude running 42m (beat 12s ago)`.
  Queued lines are unchanged.

### W11. Docs
- README sections: Health (stale code, refused harnesses, tunnel row),
  Linear webhook (timestamps, missed and quiet), Poller service (one
  launcher), new "Self-heal" and "Local config overlay" sections, and
  Install after merge (migration and legacy cleanup).
- docs/SPEC.md: a decision-log row for #107, the section 9 row "A factory
  component stops" (it currently says "Nothing is restarted"; that becomes
  self-heal), and section 10.

## Criterion → work map

| AC | Requirement | Work | Proof (tests) |
|---|---|---|---|
| a | Self-heal timer: reads `orch health --json`; restarts down via the right service script; stale-code restart when idle; backoff; `self-heal.log`; escalates via FACTORY_WEBHOOK_URL with no secret; never kills `orch job run` | W3, W3b, W6 (poller via run-poller.sh) | `tests/test_heal.py`: decide() for down, stale+idle, stale+busy defer, held, not-owned, remote device, settling, backoff after 3, flap by pid churn, escalate once per window, missing env skip, payload has no URL or auth and the log has none. `tests/test_self_heal_sh.py`: `self-heal.sh once` with fake service scripts records `waiter-service.sh codex-bot restart`; a fake `orch job run` process survives; `ensure` is silent when running; the lock blocks overlap. `test_deploy.py`: install writes the timer units or the profile hook and cron. `test_cli.py`: poller SIGTERM mid-cycle exits at the boundary. |
| b | Heartbeats record the loaded HEAD; Health and `orch status` show "stale code" | W1 | `test_health.py`: LOADED_VERSION is used; stale_code only for poller, waiter and listener; unknown is never stale; render and JSON. `test_status.py`: the Health section shows stale code. `test_dashboard*.py`: the Health tab cell. |
| c | No waiter expected for a refused or unrouted harness | W2 | `test_health.py`: `refused_harnesses: [cursor]` gives no cursor-bot slot, and a leftover row is not configured. `test_orch_yml.py`: validation. |
| d | Tunnel row only with `public_url`; no `orch health beat` claim | W2 | `test_health.py`: `linear_webhook` without public_url has a listener slot and no tunnel slot. `test_cli.py`: help has no "beat"; `health beat` still exits 2. |
| e | linear-webhook-service.sh in the repo, installed, quiet `ensure` | W4, W6 | `tests/test_linear_webhook_service.py` (fake orch and ssh): start, stop, restart, status; ensure prints nothing when running; the tunnel starts only when configured; log lines are timestamped. `test_deploy.py`: install wires the hook. |
| f | One launcher: poller-service → run-poller.sh (sources env); legacy retired; install removes stale launchers | W6 | `test_deploy.py`: the unit ExecStart is run-poller.sh; run-poller exits 78 naming a missing file; install with a fake HOME holding legacy files and crontab lines removes them and TERMs only the exact-cmdline process. |
| g | Timestamped webhook log; any signed delivery counts; warn only on a real miss; soft quiet note; #69 F2.1 and F2.2 | W5 | `test_linear_webhook.py` and `test_linear_webhook_health.py`: prefix format; a signed but stale or malformed delivery updates last_received_at; the seen table is written; `missed_delivery` cases; warning vs quiet; F2.1 restart test; F2.2 json.loads counter. `test_live_poll_webhook.py`: reconcile records a miss. `test_status.py` and `test_dashboard_webhook_panel.py`: warning vs note. |
| h | Full poll error message, truncated and scrubbed | W7 | `test_dispatcher.py`: GitHubAdapterError and PollCycleError rows hold the message; a token, URL and `KEY=` value are scrubbed; the cap is 500; prefixes are kept. `test_dashboard*.py`: the LIKE matches still count. |
| i | Live config committed or as a local overlay merged at load | W8 | `test_config_overlay.py`: projects merge by repo, devices by name, scalar override, list replace, placeholder filled, bad overlay finding names the file, off by env, check-config lists the key names and no values. |
| j | CI once per push | W9 | `test_deploy.py`: parse ci.yml; push is limited to `main`, and pull_request is present. |
| k | Real ages in `orch status` | W10 | `test_status.py`: a running job placed 42 m ago with a fresh heartbeat shows `running 42m (beat 12s ago)`; an unplaced job uses created_at. |
| l | Tests for self-heal decisions, health expectations, config overlay and the poll error message | W1–W10 test columns | All of the above, under `env -u ORCH_DB pytest`, plus the CI `check-config` steps and `model/check.sh`. |

Every criterion maps to work and tests: **ac=met**.

## Order of work (each step keeps CI green)
1. W9, W2 and W10 (small and independent).
2. W7 (poll errors), then W1 (stale code).
3. W8 (overlay), which W3 and W4 install on top of.
4. W5 (Linear webhook, #79 and #69).
5. W6 (one launcher), then W4 (listener service).
6. W3 and W3b (self-heal), then W11 (docs).

## Out of scope / not changed
- Dispatcher logic beyond the poll_errors writes. Runner files (PR #100 and
  #104). Rounds and caps (#105).
- No change to who may restart what. Systemd units are installed only where
  a user session exists, and only by `install.sh` (README documents it).
- Nothing reads or prints `webhook.env` values or the Linear secret.

## Risks
- **Poller SIGTERM semantics (W3b)** change for every stop. This is
  mitigated by the immediate second-SIGTERM exit and the unchanged exit
  code 143, and covered by a test.
- **Missed-delivery false positives**: the 60 s and 120 s grace windows and
  the "after listener start" gate keep them out. A false negative only hides
  the warning; it never stops work.
- **Live tunnel shape unknown to the builder (W4)**: the install step makes
  Factory compare against the live script before retiring it.
