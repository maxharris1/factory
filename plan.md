# Plan for #122, a separate approval credential for Max-only actions

Every bot posts to GitHub as `maxharris1`, which is Max's login. The factory tells comments apart only by the `from=` label inside each one. So any bot can post the two comments that only Max should give: a `go` that grants an extra round on a capped ticket, and a `kind=ok` that clears a ship hold. Max chose a GitHub App for the bots. Once Max installs the app and Factory turns the switch on, bot comments come from the app's bot login, and the factory counts those two approvals only from a login in a new `approver_logins` list (`maxharris1`). Until then the factory behaves exactly as it does today.

This plan is for PR #126 (branch `factory/issue-122`). It changes code and config only. It does not create or install the app, and it never reads, writes or prints a key, token or anything under `~/.config/factory/`. Tests use fake app ids, fake installation ids and fake logins.

## How to read this

One box is one unit of work. Every box names the evidence that checks it. Check a box only when its evidence exists, a test run, a log line or a SHA.

The build job runs under `pstack/skills/poteto-mode/playbooks/feature.md`. There is one PR. Codex Bot reviews it (risk high, up to three rounds). Grok Bot merges it on `ship: handoff`. The multi-phase skeleton's ten cloud lanes do not apply to a single backend PR with no UI. Its verification rule does, adapted in **Verify** below.

Tests alone are not sufficient verification. The PR is verified only when its unit, revert and CI boxes are all checked.

## The data shape

One immutable value answers one question: may this login give a Max-only approval? It is built once from `orch.yml` and passed to the three places that need it: the dispatcher, the waiter and Grok's post helper. `trusted_logins` keeps its current meaning, which bots a login may speak for. Approval is a separate question with its own list.

```python
# lib/orch_yml.py

@dataclass(frozen=True)
class GitHubApp:
    """The bots' GitHub App (#122). Numbers and a path only. orch never opens
    private_key_file. enabled is the switch: false keeps today's single-login trust."""
    app_id: int
    installations: Mapping[str, int]      # account login -> installation id
    private_key_file: Path                # absolute; never opened, read or printed
    bot_login: str | None = None          # e.g. factory-bots[bot], known after the first app post
    enabled: bool = False

@dataclass(frozen=True)
class Approvals:
    """Who may give a capped-ticket go or a hold ok."""
    app_trust: bool                       # github_app present and enabled
    logins: frozenset[str]                # casefolded; approver_logins when app_trust,
                                          # else the trusted_logins keys (today's hold rule)
    gh_config_dir: Path | None            # Max's approver gh profile; a path only

    def may_approve(self, login: str) -> bool: ...
    @classmethod
    def off(cls, trusted_github_logins: Iterable[str]) -> "Approvals": ...

OrchConfig.github_app: GitHubApp | None = None
OrchConfig.approver_logins: frozenset[str] | None = None   # None when unset
OrchConfig.approver_gh_config_dir: Path | None = None

def approvals(config: OrchConfig) -> Approvals: ...
def hold_approver_findings(config: OrchConfig, hold: HoldConfig | None) -> list[Finding]: ...
```

```yaml
# orch.yml, after Max installs the app (fake numbers)
approver_logins:
  - maxharris1
approver_gh_config_dir: /home/grok/.config/factory/gh-approver   # absolute, outside every harness HOME
github_app:
  app_id: 123456
  installations:
    maxharris1: 1111111
    aut0-code: 2222222
  private_key_file: /home/box/.config/factory/github-app-bots.pem
  bot_login: factory-bots[bot]
  enabled: false        # Factory flips this in the rollout ticket, not here
trusted_logins:
  factory-bots[bot]: [grok-bot, claude-bot, codex-bot, cursor-bot]
  maxharris1: [grok-bot]
```

The switch has two states. Each Max-only check reads `approvals.app_trust` and nothing else.

| Check | Switch off (today, the default) | Switch on |
| --- | --- | --- |
| `go` on a capped ticket | Author trusted for `grok-bot` (`_trusted`), as today | Header `from=grok-bot role=orchestrator`, and author in `approver_logins`. Anyone else gets `go-not-from-approver` and one wake |
| `go` on any other ticket | Author trusted for `grok-bot` | Same, unchanged |
| Hold `ok` and the approver's label removal | Author is `hold.approver`, and `hold.approver` is a `trusted_logins` key | Author is `hold.approver`, and `hold.approver` is in `approver_logins` |
| Harness environment | Unchanged | Never carries `GH_CONFIG_DIR` naming the approver profile. The waiter refuses to start when the profile sits inside the run's `HOME` |
| Grok posting a capped `go` or an `ok` | `env -u GH_TOKEN gh`, as today | Only through the approver profile. With no profile configured, it refuses, and Max posts by hand |

Linear authors (`linear:<id>`) are not GitHub logins, so the switch does not apply to them. A Linear ticket keeps today's rule in both states. Appendix A says why.

## Criterion-to-work map

| # | Acceptance criterion | Work that meets it | Tests that fail on revert |
| --- | --- | --- | --- |
| 1 | `orch.yml` and `config/orch.example.yml` accept a GitHub App id and installation id, plus a config path for the private key. No real key is committed, read or printed. | Unit 1 | `test_github_app_config_parses_with_fake_ids`, `test_github_app_config_rejects_bad_fields`, `test_check_config_never_opens_the_private_key_file`, `test_example_configs_carry_the_github_app_block` |
| 2 | Until the app is installed and the switch is on, a capped-ticket `go` and a `kind=ok` from `maxharris1` still work as they do today. | Units 2 and 3, the switch-off branch of each | `test_switch_off_capped_go_from_the_single_login_still_resumes`, `test_switch_off_hold_ok_from_the_single_login_still_clears`. Both are parametrized over no `github_app` block and `enabled: false` |
| 3a | Switch on, a capped-ticket `go` from the app bot login is refused with wake reason `go-not-from-approver`. | Unit 2 | `test_switch_on_capped_go_from_the_app_bot_is_refused` (one wake, no fix, `cap_extra` unchanged, state stays `capped`) |
| 3b | Switch on, a capped-ticket `go` from `maxharris1` raises `cap_extra` by 1. | Unit 2 | `test_switch_on_capped_go_from_the_approver_raises_cap_extra` |
| 3c | Switch on, a `kind=ok` from a non-approver, the app bot login included, leaves the hold in place. | Unit 3 | `test_switch_on_hold_ok_from_the_app_bot_keeps_the_hold`, `test_switch_on_hold_ok_from_a_non_approver_keeps_the_hold` |
| 3d | Switch on, a `kind=ok` from `maxharris1` in `approver_logins` clears the hold, with today's head-pin rules. | Unit 3 | `test_switch_on_hold_ok_from_the_approver_clears_at_the_head`, `test_switch_on_hold_ok_at_an_old_head_does_not_clear` |
| 4 | `check-config` fails when `hold.approver` is not in `approver_logins`. | Unit 4 | `test_check_config_fails_when_the_hold_approver_is_not_an_approver`, `test_check_config_passes_when_the_hold_approver_is_an_approver` |
| 5 | No harness user's environment or home holds Max's approver profile once the switch is on. A test inspects the waiter's environment builder with fake values only. | Unit 5 | `test_waiter_env_drops_the_approver_gh_config_dir`, `test_waiter_refuses_when_the_approver_profile_is_in_the_run_home`, `test_waiter_env_unchanged_with_the_switch_off` |
| 6 | Trust-map and decision notes are in `docs/decisions/122-approver-credential.md`. | Unit 7 | Reviewer reads the file. `test_example_configs_carry_the_github_app_block` keeps the example in step with the note |

Scope item outside the AC list: "`lib/orchestrate.py` may still post `go`/`ok` under Max's login when Max has authorized it, and must not post those two kinds as the app bot." Unit 6 covers it with `test_approval_post_env_uses_only_the_approver_profile` and `test_approval_post_env_refuses_with_no_profile_when_the_switch_is_on`.

Every criterion is mapped.

## Work units

Each unit is one commit that ends green under `env -u ORCH_DB pytest`. Test first in every unit. Every new test goes in `tests/test_approver_credential.py`. Nothing is appended to `tests/test_dispatcher.py` or `tests/test_brief.py`.

### Unit 1. Parse the app, approver logins and approver profile (AC 1)

**Files.**

- [ ] Edit `lib/orch_yml.py`.
- [ ] Edit `config/orch.example.yml` and `orch.yml`.
- [ ] New `tests/test_approver_credential.py`.

**Build.**

- [ ] Add `github_app`, `approver_logins` and `approver_gh_config_dir` to `_TOP_KEYS`. Add `GitHubApp`, `Approvals`, `approvals(config)` and the three new `OrchConfig` fields from the data shape.
- [ ] `_github_app(data, findings)` checks the section is a mapping with no unknown keys. `app_id` is a positive int. `installations` is a non-empty mapping from a GitHub login to a positive int. `private_key_file` is an absolute path string. `bot_login` is `<name>[bot]`. `enabled` is a bool and defaults to false. `private_key_file` is stored as a `Path` and never opened, stat-ed or echoed in a finding.
- [ ] `_approver_logins` checks a non-empty list of GitHub logins (`_LOGIN`). A `[bot]` login is refused, because an app can never approve.
- [ ] `approver_gh_config_dir` must be an absolute path. No `~`, because `~` would expand to whichever user reads it.
- [ ] With `enabled: true`, `bot_login` and `approver_logins` are both required, `bot_login` must not be in `approver_logins`, and `bot_login` must be a `trusted_logins` key, so the bots keep working after the flip. Each broken rule is its own error.
- [ ] `config/orch.example.yml` gets a commented `approver_logins` and `github_app` block with fake numbers and `enabled: false`, in the style of its `dashboard` and `job_states` blocks. `orch.yml` gets a live `approver_logins: [maxharris1]` and the same commented `github_app` block. Max fills in the real numbers later. No path in either file points at a real key.

**You see.**

- [ ] `orch check-config --orch config/orch.example.yml` prints `ok`. A copy of the example with the block uncommented also prints `ok`, and `approvals(config)` returns `app_trust=False`.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_approver_credential.py -k "github_app or example or private_key"`.

- [ ] `test_github_app_config_parses_with_fake_ids`.
- [ ] `test_github_app_config_rejects_bad_fields` covers each error above, including `enabled: true` without `bot_login`, an app login inside `approver_logins`, and a relative key path.
- [ ] `test_check_config_never_opens_the_private_key_file` points `private_key_file` at a `tmp_path` file holding a fake sentinel. It runs `_check_config` in-process with `builtins.open`, `io.open` and `os.open` wrapped to record paths. It asserts the key path was never opened and the sentinel is in neither stdout nor stderr.
- [ ] `test_example_configs_carry_the_github_app_block` uncomments the example block and parses it.

### Unit 2. Only an approver's `go` lifts a cap once the switch is on (AC 2, 3a, 3b)

**Files.**

- [ ] Edit `lib/dispatcher.py`: `_t1`'s author check, `_resume_after_cap`, a `Dispatcher(approvals=...)` keyword, and the new reason in `GROK_WAKE_REASONS`.
- [ ] Edit `lib/queue.py`, one `ACTIONS` entry.
- [ ] Edit `lib/cli.py` `_runtime`, which passes `approvals(config)`.
- [ ] Edit `tests/test_approver_credential.py`.

**Build.**

- [ ] `Dispatcher.__init__` takes `approvals: Approvals | None = None`. `None` means `Approvals.off(<github trusted_logins keys>)`, so every existing caller and test keeps today's behavior.
- [ ] `_t1` keeps its header check (`from=grok-bot role=orchestrator`). The author check becomes "trusted for `grok-bot`, or the switch is on and the author may approve". It returns `rejected-go-author` otherwise, as today. After `_resume_after_cap` returns `None` (the ticket is not capped), a go whose author is not trusted for `grok-bot` is still `rejected-go-author`. An approver who is not a trusted login cannot start ordinary work. Nothing else in `_t1` changes.
- [ ] In `_resume_after_cap`, after it reads `workflow_state = 'capped'` and before it writes anything, it checks the switch. With the switch on and an author who may not approve, it commits nothing, wakes Grok with reason `go-not-from-approver` through `_wake_comment_problem` (once per comment), and returns `(True, "go-not-from-approver")`. With the switch off, it takes today's path. A Linear author always takes today's path.
- [ ] Add `go-not-from-approver` to `GROK_WAKE_REASONS` and `lib/queue.py` `ACTIONS`. `wake_grok_once` keys it on the comment, as it does `rejected`.

**You see.**

- [ ] With the switch on, a capped ticket and an app-bot go leave `ticket_state` at `capped` with `cap_extra=0`. `jobs` has one `grok` row, action `go-not-from-approver`. The same go from `maxharris1` gives `cap_extra=1`, `workflow_state='fixing'` and one fix job.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_approver_credential.py -k "capped_go"`.

- [ ] `test_switch_off_capped_go_from_the_single_login_still_resumes`, parametrized over no `github_app` block and `enabled: false`.
- [ ] `test_switch_on_capped_go_from_the_app_bot_is_refused`.
- [ ] `test_switch_on_capped_go_from_the_approver_raises_cap_extra`.
- [ ] `test_switch_on_go_from_an_approver_on_an_uncapped_ticket_needs_grok_trust` checks that an approver with no `trusted_logins` entry gets `rejected-go-author` on a ticket that is not capped.
- [ ] The existing capped-go tests in `tests/test_dispatcher.py` pass unchanged.

### Unit 3. Only an approver's `ok` clears a hold once the switch is on (AC 2, 3c, 3d)

**Files.**

- [ ] Edit `lib/hold.py`. Rename `decide(approver_trusted=)` to `approver_allowed=`, which has one caller, and update the module docstring.
- [ ] Edit `lib/dispatcher.py`, `_hold_decision` and `_hold_ok_outcome` only.
- [ ] Edit `tests/test_approver_credential.py`.

**Build.**

- [ ] Add `Dispatcher._may_approve(login)`. With the switch off it is today's `_trusted_login(login)`. With it on it is `approvals.may_approve(login)`. `_hold_decision` passes `approver_allowed=self._may_approve(hold.approver)`. `_hold_ok_outcome` uses the same predicate. The author must still equal `hold.approver`, and the `ok` must still name a 40-character head that covers the current head. `hold.py`'s head-pin logic does not change.
- [ ] An app-bot login can never be an approver, because Unit 1 refuses it in `approver_logins`. A repo whose `factory.md` names the app as `hold.approver` therefore fails closed at runtime, and the hold stays.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_approver_credential.py tests/test_ship_hold.py -k "hold"`. Reuse `tests/test_ship_hold.py`'s `setup`, `approve` and `ok` helpers by import.

- [ ] `test_switch_off_hold_ok_from_the_single_login_still_clears`, parametrized as in Unit 2.
- [ ] `test_switch_on_hold_ok_from_the_app_bot_keeps_the_hold`. The outcome is `rejected-hold-ok-author` and the ticket stays `awaiting-ok`.
- [ ] `test_switch_on_hold_ok_from_a_non_approver_keeps_the_hold`.
- [ ] `test_switch_on_hold_ok_from_the_approver_clears_at_the_head`. The PR merges with `--match-head-commit`.
- [ ] `test_switch_on_hold_ok_at_an_old_head_does_not_clear`.
- [ ] `tests/test_ship_hold.py` passes unchanged.

### Unit 4. `check-config` ties `hold.approver` to the approvers (AC 4)

**Files.**

- [ ] Edit `lib/orch_yml.py` (`hold_approver_findings`).
- [ ] Edit `lib/cli.py` `_check_config`, a cross-file pass after both files load.
- [ ] Edit `tests/test_approver_credential.py`, with a `tmp_path` `factory.md` fixture that has a `hold:`.

**Build.**

- [ ] `hold_approver_findings(config, hold)` returns an error `hold.approver <login> is not in approver_logins` when `approver_logins` is set and does not hold the approver. When `approver_logins` is unset, the error names `trusted_logins`, today's runtime rule. A `factory.md` with no `hold` gives no finding.
- [ ] `_check_config` runs it for every `factory.md` it loads and reports under that file's path. The example `factory.md` files have no `hold:`, so the CI steps do not change.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_approver_credential.py -k check_config`.

- [ ] `test_check_config_fails_when_the_hold_approver_is_not_an_approver` covers `approver: max` (the `factory_md` default) and `approver: factory-bots` against `approver_logins: [maxharris1]`. The exit status is 1.
- [ ] `test_check_config_passes_when_the_hold_approver_is_an_approver`.

### Unit 5. Harness runs never carry the approver profile (AC 5)

**Files.**

- [ ] Edit `lib/harness_env.py` (`harness_environ`).
- [ ] Edit `lib/waiter.py`: a `Waiter(approvals=...)` keyword and `_spawn`'s env line.
- [ ] Edit `lib/cli.py` `_wait_autostart`, which passes `approvals(config)` when `orch.yml` loaded.
- [ ] Edit `tests/test_approver_credential.py`.

**Build.**

- [ ] `harness_environ(environ, approvals) -> dict[str, str]` is pure and compares paths only. It never opens or lists a directory. With the switch off, or with no `gh_config_dir`, it returns `environ` unchanged. With the switch on:
  - it drops `GH_CONFIG_DIR` when that path equals the approver profile or lies inside it;
  - it raises `ApproverProfileExposed` when the profile lies inside the run's `HOME`, because `gh` finds `$HOME/.config/gh` with no `GH_CONFIG_DIR` set, and anything under `HOME` belongs to the harness user.
- [ ] `Waiter.__init__` calls it once and turns `ApproverProfileExposed` into a `WaiterError`. `orch wait --autostart` then exits 2 with that message and starts no harness. `_spawn` builds its env from `harness_environ(self.environ, self.approvals)`. With `approvals=None`, today's env passes through.
- [ ] Remote runs pass only named variables through `lib/remote.py` (`remote.start_script`), never the box's `GH_CONFIG_DIR`, so they need no change. The decision note says so.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_approver_credential.py -k waiter_env`. Every path is under `tmp_path`, and every login and id is fake.

- [ ] `test_waiter_env_drops_the_approver_gh_config_dir` drives `Waiter._spawn` with a fake `spawn` and asserts the captured `env` has no `GH_CONFIG_DIR`, while other variables pass through.
- [ ] `test_waiter_refuses_when_the_approver_profile_is_in_the_run_home` uses `HOME=<tmp>/claude-bot` and the profile at `<tmp>/claude-bot/.config/gh`. `Waiter(...)` raises `WaiterError`.
- [ ] `test_waiter_env_unchanged_with_the_switch_off`.
- [ ] `tests/test_waiter.py` passes unchanged.

### Unit 6. Grok posts the two approvals only through Max's profile (scope item)

**Files.**

- [ ] Edit `lib/orchestrate.py`, a new `approval_post_env` next to `go_comment`.
- [ ] Edit `grok/orchestrate-project/SKILL.md`, one sentence on posting a capped `go` or a hold `ok`, and one line on the `go-not-from-approver` wake.
- [ ] Edit `tests/test_approver_credential.py`.

**Build.**

- [ ] `approval_post_env(approvals, environ) -> dict[str, str]` returns the environment for the `gh` call that posts a capped-ticket `go` or a hold `ok`. It always drops `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN` and `GITHUB_ENTERPRISE_TOKEN`. With the switch off, that is the whole change, the same as today's `env -u GH_TOKEN gh`. With the switch on, it sets `GH_CONFIG_DIR` to `approvals.gh_config_dir`. When no profile is configured, it raises `OrchestrateError("post it from maxharris1 by hand")`. It never falls back to the bot's own `gh`, so those two kinds are never posted as the app.
- [ ] The skill tells Grok to post those two kinds with this environment, still only with Max's own words for that ticket. It also says a `go-not-from-approver` wake means a capped go came from a login that cannot approve, and to tell Max once.

**Verify, unit.** Run `env -u ORCH_DB pytest tests/test_approver_credential.py tests/test_orchestrate.py -k "approval_post or orchestrate"`.

- [ ] `test_approval_post_env_uses_only_the_approver_profile` gives a fake env with `GH_TOKEN=fake-app-token`. The result has no token variable and `GH_CONFIG_DIR=<fake profile>`.
- [ ] `test_approval_post_env_refuses_with_no_profile_when_the_switch_is_on`.
- [ ] `test_approval_post_env_with_the_switch_off_only_drops_tokens`.

### Unit 7. Decision note and SPEC rows (AC 6)

**Files.**

- [ ] New `docs/decisions/122-approver-credential.md`.
- [ ] Edit `docs/SPEC.md`: the section 6 `hold.approver` row, a section 7 `orch.yml` field list entry, and the section 4.2 T1 row for the capped `go`. There is no decision-log row, per the ticket.

**Build.**

- [ ] The note carries Max's decision and its time (Nov 13, 2026, 2:26 to 2:52 PM ET), the trust map table from **The data shape**, the two switch states, and what Factory flips on rollout. Rollout means setting `enabled: true`, adding `trusted_logins[factory-bots[bot]]`, trimming `maxharris1` to `[grok-bot]`, and moving the bots' `gh` onto the app.
- [ ] It also records what this ticket cannot check. The factory cannot tell which account a `gh` profile holds without reading its token, so moving the harness users' own `gh` login off `maxharris1` is Max's setup step. The code checks paths only.
- [ ] It records the Linear gap (Appendix A) and the `hold.approver` default `max` (Appendix C).

**Verify, unit.**

- [ ] The note exists and names every row of the trust map. Codex reads it in review.

## Verify

Tests alone are not sufficient verification. The PR is verified only when its unit, revert and CI boxes are all checked.

- [ ] Unit. `env -u ORCH_DB pytest` passes at the PR head, run in the foreground.
- [ ] Revert. For each test in the map, revert its unit's production change on a scratch branch and record that the test fails. Note the result per criterion in the `ready` comment.
- [ ] Config. `python bin/orch check-config` passes on all four CI invocations in `.github/workflows/ci.yml`.
- [ ] Secrets. `git grep -nE "BEGIN (RSA )?PRIVATE KEY|ghs_|ghp_|github_pat_"` at the head finds nothing. `git diff origin/main --stat` lists no `.pem` and nothing under `~/.config/factory`.
- [ ] CI. `.github/workflows/ci.yml` is green at the head, covering pytest, the `check-config` steps and `statechart`. No workflow state changes, so the model needs no edit.
- [ ] Perf. None. The change adds one set lookup per `go` on a capped ticket and per hold check.
- [ ] Review gate. Codex Bot's round-1 review of the `ready` head.

## Close

- [ ] Every box above is checked with its evidence.
- [ ] PR title `#122: Separate approval credential: GitHub App for the bots`.
- [ ] Post the `ready` handoff with the pushed head, the per-criterion test list and the revert results.

## Appendix A. Calls made without asking Max

No product question blocks this. Each call below is a default, with what Max could ask for instead.

- **Linear tickets keep today's rule.** The ticket is about GitHub logins. A Linear comment's author is a Linear user id, and the app does not change who posts on Linear. So the capped `go` and the hold `ok` on a Linear ticket (Aut0) stay trusted-for-`grok-bot` and trusted-login, as today. The F-02 gap stays open on Linear until bots get their own Linear identity. Max could instead ask for an `approver_linear_users` list now. That needs a second Linear identity for the bots, which does not exist yet.
- **Approvers live in `orch.yml`, not `factory.md`.** `factory.md` sits in the repo the bots push to, so a bot could edit its own approver list. `orch.yml` lives on the box.
- **Installations are a mapping from account to id**, because Max installs the app twice (`maxharris1` and `aut0-code`).
- **A wake reason of its own.** The AC names `go-not-from-approver` as the wake reason, so it is a new reason rather than `rejected` with a detail. That costs one entry each in `GROK_WAKE_REASONS` and `ACTIONS`.
- **The waiter refuses rather than strips** when the profile sits under `HOME`. A file cannot be stripped from an environment, and failing closed matches the hold's own rule.

## Appendix B. Designs compared (architect scaffold)

- **A, chosen.** One `Approvals` value from `orch.yml`, passed to the dispatcher, the waiter and Grok's post helper. Identity (`trusted_logins`) and capability (`approver_logins`) stay separate lists. The switch is one field. Each of the four checks asks one predicate.
- **B, rejected.** Add a pseudo-bot `approver` to `trusted_logins` (`maxharris1: [grok-bot, approver]`) and check `_trusted(..., "approver")`. That mixes who a login speaks for with what it may approve. A copied `trusted_logins` line could grant approval by accident. It also has no place for the switch or the profile path.
- **C, rejected.** Per-repo approvers in `factory.md`. The bots can edit it (see Appendix A).

The arena runners were not run for this plan. The design space is narrow and fixed by the ticket. Codex should press on A's switch-on `_t1` gate in review.

## Appendix C. Risks and lane notes

- **Files beyond the ticket's list.** Each is the smallest hunk that meets an AC:
  - `lib/cli.py`: wiring in `_runtime` and `_wait_autostart`, and the cross-file pass in `_check_config` (AC 4).
  - `lib/waiter.py` and `lib/harness_env.py`: the env builder the AC names (AC 5).
  - `lib/queue.py` and `GROK_WAKE_REASONS`: the AC's wake reason (AC 3a).
  - `lib/dispatcher.py`: the `_t1` author check (the ticket allows it), `_hold_decision`, `_hold_ok_outcome` and the constructor keyword (AC 3c, 3d).
  - `orch.yml`: the ticket asks for it.
  - `grok/orchestrate-project/SKILL.md`: one sentence.
  - `docs/SPEC.md`: three table rows, no decision log.
- **#111 is open** and touches `lib/orch_yml.py`, `lib/dispatcher.py`, `lib/cli.py`, `orch.yml` and `config/orch.example.yml`. If it lands first, bring `main` in by a merge commit before `ready`.
- **`hold.approver` defaults to `max`** in `lib/factory_md.py`. That is not a login in `trusted_logins`, so a hold with the default never clears today. This PR leaves the default alone, since that file is outside the lane. `check-config` now reports it. Changing the default to `maxharris1` is a one-line follow-up for Factory to file.
- **`orch job run` started by hand**, outside a waiter, inherits that shell's environment (`lib/cli.py` copies `os.environ`). The guard covers every run the factory starts. A person running a job by hand owns their own shell.
- **A waiter whose `orch.yml` fails to load** runs with `approvals=None` (switch off). The poller fails on the same file, so no job is queued from it.
- **Switch on with the app not yet trusted.** Unit 1 refuses `enabled: true` unless `bot_login` is a `trusted_logins` key, so the flip cannot lock the bots out.

## Appendix D. Reading list

- `lib/dispatcher.py`: `_t1`, `_resume_after_cap`, `_hold_decision`, `_hold_ok_outcome`, `_trusted_login`, `_wake_comment_problem`, `wake_grok_once`, `GROK_WAKE_REASONS`.
- `lib/hold.py` `decide` and `_approver_ok`. `lib/factory_md.py` `_hold`.
- `lib/orch_yml.py` `_validate`, `_trusted_logins` and `_linear_webhook` (the `secret_file` is never read, and that is the pattern to copy).
- `lib/waiter.py` `Waiter.__init__` and `_spawn`. `lib/harness_env.py`. `lib/cli.py` `_check_config`, `_runtime` and `_wait_autostart`.
- `lib/orchestrate.py` `go_comment`. `grok/orchestrate-project/SKILL.md` on the `awaiting-ok` wake.
- `docs/SPEC.md` sections 4.2 (T1, T4H), 6 (`hold`) and 7.
