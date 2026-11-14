# Plan: #151 subscription headroom data (PR #234)

Real 5-hour and 7-day usage per bot account, read only from the CLI's own event stream, stored on the
existing telemetry flush and exposed as a snapshot field `usage` for the Efficiency page's Subscription
headroom panel (SPEC 3.5 item 5). This ticket is data only: the panel's markup is #144, so no page,
CSS, CSP or screenshot changes here.

This plan is not at the repo root: `tests/test_no_root_plan_md.py` (#158) forbids a root `plan.md`.
The plan handoff comment holds the same text.

## Architect scaffold

```
claude -p / codex exec  ──stdout──▶ telemetry.Session.consume ──▶ Summary.feed(event)
                                                                    │ (new) usage_readings.parse_claude / parse_codex
                                                                    ▼
                                         Summary.usage: latest Reading per window (numbers + enums only)
                                                                    │
            orch job run heartbeat ──▶ Session.flush / finish ──▶ one transaction:
                                                                    job_telemetry row (unchanged)
                                                                    usage_readings rows (new table, upsert if newer)
                                                                    │
  dashboard collect_snapshot (read-only txn) ──▶ dashboard_usage.read_usage ──▶ snapshot["usage"] ──▶ _scrub
  routing (unchanged default NoUsage) ◀── usage_readings.TelemetryUsage (UsageSource), verified rule unchanged
```

New modules: `lib/usage_readings.py` (parsers, table, `TelemetryUsage`), `lib/dashboard_usage.py`
(validated read for the snapshot). Edited: `lib/telemetry.py`, `lib/job_run.py` (device into Session),
`lib/dashboard.py`, `lib/usage.py` (docstring only), `docs/design/mission-control/STATUS.md`.

## Step 1: what each harness exposes (goes in the PR body before Step 2 code)

Done once on each device that runs the harness, as that bot user, using only the CLI's own output
(no credential/token files, no provider API calls, nothing secret printed). Record the CLI version.
The source is either captured `events.jsonl` lines on the box (filtered to the usage event type
only) or a fresh minimal run's stdout.

**Claude Code, checked so far: 2.1.322 on mac (claude-bot), from the CLI's own bundled schema.**
`--output-format stream-json` (already added by `harness_argv`) emits
`{"type":"rate_limit_event","rate_limit_info":{...},"uuid":...,"session_id":...}` with
`status` (`allowed|allowed_warning|rejected`), `resetsAt` (epoch seconds), `rateLimitType`
(`five_hour|seven_day|seven_day_opus|seven_day_sonnet|seven_day_overage_included|overage`),
optional `utilization`, optional `unifiedWindows.{five_hour,seven_day}.{utilization,resetsAt,status}`,
plus overage flags. Freshness: emitted inside the run whenever the CLI gets rate-limit info, so it is as
fresh as the bot's last job. Needs nothing beyond the stream the runner already reads. To confirm on the
box's claude-bot: which fields are really present (does `utilization` come without `unifiedWindows`?),
how often the event fires, and the version. `/usage` is interactive only, so it is out of scope.

**Codex: not installed on mac. Check on box codex-bot (`codex --version`).** Check whether
`codex exec --json` (events `thread.started`, `turn.*`, `item.*`) carries any rate-limit field. Codex's
protocol `token_count` event has
`rate_limits.{primary,secondary}.{used_percent, window_minutes, resets_at|resets_in_seconds}`. Check
whether that reaches `exec --json` stdout. If it doesn't, the only other CLI-written place is the
session rollout under `$CODEX_HOME/sessions/`. That is the CLI's own output file, not a credential,
but it is not stdout, so it goes to Max as a separate option and is not assumed.

**Cursor: not tracked** (it stays off).

**Proposal to Max (one per harness):** Claude → `rate_limit_event` from stream-json. Codex →
`exec --json` rate limits if present, else option B (the rollout `token_count`, read only for that
job's own session file and only the `rate_limits` keys) or option C ("no reading"). The build job posts
the Step 1 report in the PR body, then a `kind=questions` handoff asking Max to pick. Step 2 ships
only the picked sources, and Max's pick is recorded in STATUS.md. A harness with nothing usable shows
`no reading`, never a number.

## Step 2: build

1. **Parsers** (`lib/usage_readings.py`): `parse_claude(event)` / `parse_codex(event)` return
   `Reading(harness, window, used, resets_at, status)` where `window ∈ {"five_hour","seven_day"}`.
   Claude: `unifiedWindows` when present, else `rateLimitType` plus `utilization`. `seven_day_opus`,
   `seven_day_sonnet` and overage readings are ignored (the design has two windows). Codex: windows
   mapped by `window_minutes` (300 → five_hour, 10080 → seven_day), never by primary/secondary
   position, and `used_percent/100`. Validation like `dashboard_telemetry`: no bools, finite, used
   in 0..1, `resets_at` an int epoch within ±30 days of now, status from a fixed enum. Anything else
   is dropped, so no event text is ever kept.
2. **Capture** (`lib/telemetry.py`): `Summary.feed` routes `rate_limit_event` (and the chosen Codex
   event) to the parsers and keeps the latest reading per window with `observed_at = now`. Its
   failures are caught like other parse errors. No new cadence: `Session._write` upserts into
   `usage_readings` in the same transaction as the `job_telemetry` row, on the existing flush/finish.
   `ORCH_TELEMETRY=off` means no readings.
3. **Storage**: table `usage_readings(harness, bot, device, window, used, resets_at, status,
   observed_at, job_id)`, primary key `(harness, bot, device, window)`. The upsert replaces a row
   only when `observed_at` is newer. The account is the bot's OS user on a device (machines.yml
   `harnesses`), so the key is per harness and account. `device` comes from the job's placement
   (`box` when unplaced), passed into `Session` by `lib/job_run.py`. Remote runs stream back to the
   box's Session already.
4. **Snapshot** (`lib/dashboard_usage.py` + `lib/dashboard.py`): `read_usage(connection, now,
   machines)` runs inside `collect_snapshot`'s read-only transaction (next to `read_telemetry`) and
   reads only allowlisted columns, each re-validated. Output is
   `usage: {state, bots: [{bot, harness, device, state, five_hour, seven_day, headroom, observed_at,
   age_seconds}]}` with each window `{used, resets_at, observed_at, state}`.
   - One entry per (bot, device) from machines.yml, so a bot with no row still appears.
   - Cursor: `not tracked`. A harness with no chosen source: `no reading`. Table missing (old db)
     or telemetry off: `no telemetry`.
   - A window whose `resets_at` has passed is `stale` and its `used` is null, never assumed 0. A
     reading older than 24 h is `stale`.
   - `headroom` = min over fresh windows of `1 - used`, null when no window is fresh.
   - The whole snapshot still goes through `_scrub`.
5. **UsageSource** (`TelemetryUsage` in `lib/usage_readings.py`): `read(harness)` returns
   `UsageReport(remaining=headroom, verified=<both windows fresh from the chosen source>)` or None.
   **Routing is not switched on in this PR:** `orchestrate.choose_route` keeps its default
   (`NoUsage`). `reserve_check`/`route_job` can take `TelemetryUsage` behind the existing
   "verified report" rule (`_in_reserve` ignores unverified). The PR body says so explicitly.
6. **Docs**: `lib/usage.py` docstring ("does not read a usage account" becomes "reads only via
   TelemetryUsage"), `lib/telemetry.py` docstring (usage readings: numbers only), STATUS.md (Max's
   pick, open question closed).

## Tests

- `tests/fixtures/usage/claude-2.1.322-rate_limit_event.jsonl` and `codex-<ver>-*.jsonl`: real
  captured lines with `session_id`/`uuid`/ids redacted. A note in the fixture dir records device, bot
  and CLI version.
- `tests/test_usage_readings.py`:
  - parse the fixtures; the Codex window is mapped by minutes.
  - malformed values (bool, NaN, >1, string, far-future reset, unknown window) are dropped.
  - a Session fed the fixture writes `usage_readings` on flush. An older reading never overwrites a
    newer one. Telemetry off writes nothing.
  - `TelemetryUsage` verified and unverified cases drive `reserve_check` as expected.
- `tests/test_dashboard_usage.py`:
  - snapshot shape.
  - missing table → `no telemetry`; no row → `no reading`; cursor → `not tracked`.
  - reset passed → `stale` with `used` null; old reading → `stale`; headroom math.
  - values pass `_scrub`; no artboard sample values.
- **No credential file**: during parse/flush/`read_usage`/`TelemetryUsage.read`, patch
  `builtins.open`, `io.open`, `os.open` and `Path.open` to record paths. Assert only orch.db (and the
  job's own events log) is touched, and nothing under `~/.claude`, `~/.codex`, `auth.json` or
  `*credentials*`. Plus a source check that the new modules import no `subprocess`, `urllib`,
  `http` or `requests`.
- The full suite passes with `env -u ORCH_DB`. No existing test changes.

## Acceptance criterion → work

| # | Criterion (ticket) | Work |
|---|---|---|
| 1 | Step 1: per harness, per bot account per device, every CLI usage signal with version, fields, freshness, extras; Cursor "not tracked"; no credentials | Step 1 above. The Claude Code 2.1.322 schema is already captured. Box claude-bot and codex-bot checks go into the PR body before Step 2 code. |
| 2 | PR proposes one source per harness, Max picks, the builder ships that choice; nothing usable → "no reading" | Proposal above, then a `kind=questions` handoff. Ship only the pick, record it in STATUS.md, `no reading` state (Step 2.4). |
| 3 | Real `UsageSource` fed from the chosen signal, captured on the existing telemetry flush, stored per harness and account with `observed_at` | Step 2.1–2.3 and 2.5 (`TelemetryUsage`, `usage_readings` table, same-transaction upsert in `Session._write`) |
| 4 | Snapshot `usage` per bot: 5h and 7d used, resets_at, headroom, observed_at; redacted and validated like `job_telemetry` | Step 2.4 (`dashboard_usage.read_usage`, allowlist + re-validation, `_scrub`) |
| 5 | Routing may use the reports only behind the existing verified rule; the PR says so explicitly | Step 2.5: not wired into `choose_route`, the verified rule is unchanged, tested, and stated in the PR body |
| 6 | Fixture tests from real redacted CLI output for both harnesses; stale or missing reported, never guessed | Tests: fixtures, `stale` / `no reading` / `no telemetry` cases, `used` null after reset |
| 7 | Test that no credential file is opened (reads only CLI output / telemetry rows) | Tests: the open-recording test plus the source-import check |
| G | Ground rules: real data only, read-only + redaction, CSP/assets untouched, existing tests pass, data gaps as plain text | Data-only change (no page edits, so no screenshots needed and the PR says so), `_scrub`, plain-text states, full suite |

## Risks and order

- If Codex exposes nothing on stdout, Max's pick decides between B and C. Codex is the only part that
  waits on that pick; the Claude path can be built meanwhile.
- `rate_limit_event` fields vary by plan and version. The parser accepts both shapes and drops the
  rest, and the fixture records the version.
- Merge `main` by a merge commit before pushing (#152 landed in `lib/dashboard.py`). #144 renders the
  panel after this merges.
