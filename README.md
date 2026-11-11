# factory

Grok Bot orchestrates pstack work across Claude Code, Codex and Cursor.

The specification is [docs/SPEC.md](docs/SPEC.md). This repo has the F1-01,
F1-03, F1-04, F1-05 and F1-09 contracts, the F1-06 blocking wait, the F1-02 pstack pin, the F1-07
`orchestrate-project` skill, the F1-08 usage reserve, the F1-13 orchestrator
model, and the F1-14 weekly eval report. Other Phase 1 items are not built.

## Contracts

- [`orch.yml`](orch.yml) lists the projects to poll and the dispatcher settings ([docs/SPEC.md](docs/SPEC.md) section 7). [`config/orch.example.yml`](config/orch.example.yml) is the minimal file.
- `factory.md` is the per-repo policy ([docs/SPEC.md](docs/SPEC.md) section 6). The generic example is [`docs/factory.example.md`](docs/factory.example.md). Aut0, Cleffy and Tracy examples live under [`examples/`](examples/) and are not those repos.
- `orch:v1` handoff comments ([docs/SPEC.md](docs/SPEC.md) section 5) are parsed and written by [`lib/handoff.py`](lib/handoff.py).
- [`lib/dispatcher.py`](lib/dispatcher.py) applies T1 through T6 (F1-05 is
  the T3 review round, T4 ship and T5 questions loop) to
  tickets discovered through `orch:state/` GitHub labels or the Linear handoff
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
  `after:` ship intent, CI on it is green and it is still the PR head.
  Every other repo keeps `ship: merge` as a squash merge.
  Hooks never see `kind=go` or `kind=ready`.

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
prints one JSON line. If that line cannot be printed, the job is finished as
failed so T6 retries it at once. `orch` never starts a harness: the bot that ran
the shell call starts its own. `job heartbeat` and `job done` refuse a job that
belongs to another bot. The database is `--db`, else `$ORCH_DB`; a missing file
is refused. Each bot's "orch jobs" paragraph is under [`bots/`](bots/).

## Check config

```sh
bin/orch check-config
```

Exit 0 when `orch.yml` and the factory.md files are valid. An unknown model name is a warning on stderr. Exit 1 when a file is refused.

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
