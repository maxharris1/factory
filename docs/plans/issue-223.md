# Plan: #223 place jobs by lowest utilization, with ties off-box (PR #247)

`choose_device` stops picking the device with the most free slots. It picks the eligible device with the lowest
utilization (`used / slot_max`). A tie goes to remote devices first, in `machines.yml` order, and the local device
(the box) goes last. Eligibility does not change: the device is enabled, has no placeholder, lists the harness
and has a free slot.

This plan is not at the repo root: `tests/test_no_root_plan_md.py` (#158) forbids a root `plan.md`. The plan
handoff comment on PR #247 links here.

## Architect scaffold

```
DevicePool.claim(harness)                       lib/device_pool.py:230 (unchanged)
  BEGIN IMMEDIATE
    used = _used(connection, config)            {device name -> slots held}  (unchanged)
    device = choose_device(config, harness, used)   <-- the only behaviour change
        candidates = devices that are eligible(harness) and have used < slot_max
        key(device) = (Fraction(used, slot_max),   # 1. lowest utilization, exact (no float ties)
                       device.local,               # 2. remotes (False) before the box (True)
                       index in machines.yml)      # 3. file order
        return min(candidates, key) or None
    INSERT device_slots row + job -> running    (unchanged, same transaction)
```

Nothing else calls `choose_device` (grep: one call site, `lib/device_pool.py:230`). `free_slots`, the feeder's
capacity gate, the waiter and the dispatcher only see which device comes back, so none of them change. Factory
code stays project-agnostic: there are no device names in the rule, only `device.local` and file order.

Exact `Fraction` keys mean 1/3 and 2/6 tie exactly and fall through to the off-box rule. Float division could
break that tie by a rounding error. `slot_max` is never 0 among the candidates, because a device needs
`used < slot_max` to be a candidate. That check runs before the division, so there is no divide-by-zero.

## Files

| File | Change |
|---|---|
| `lib/device_pool.py` | New `choose_device` body and docstring as sketched above. The module docstring (lines 5-8) describes the new rule. |
| `tests/test_routing.py` | The #85 placement tests are rewritten for the new rule (AC1-AC4, below). |
| `tests/test_device_pool.py` | `test_choose_device_skips_full_ineligible_placeholder_and_disabled` stays as it is (it already covers AC3). One extra assert checks that a disabled or placeholder device at 0% still loses to an eligible device at 50%. |
| `tests/test_remote_start.py` (`machines_file`), `tests/test_job_state.py` (`Poller`), `tests/test_waiter_remote.py` (its machines writer) | These fixtures tie the box with the PC and their tests expect the box. Each fixture gets a `pc: bool = True` switch, and `False` writes the PC as `enabled: false`. Tests about box-only behaviour pass `pc=False`, so they keep testing what they were written for. The full list is in "Tests the new rule breaks". |
| `docs/SPEC.md` section 13 | The sentence "(the most free slots; a tie goes to the device listed first in `machines.yml`)" becomes "(the lowest utilization, slots held over `slot_max`; a tie goes to a remote device before the local box, remotes in `machines.yml` order; issue #223)". |
| `README.md` lines 167-168 | The same wording change, so the README does not contradict the SPEC. |
| `docs/decisions/223-placement-lowest-utilization.md` | New. One decision, Max's words from the ticket ("File and build: place jobs by lowest utilization, ties off-box", 2026-11-14 9:28 AM ET). It replaces the #85 "most free slots" rule. The SPEC decision log is frozen (`docs/decisions/README.md`), so the decision goes here. |

The ticket's Touches line is `lib/device_pool.py, tests/test_routing.py, docs/SPEC.md`. The extra test fixtures,
README and decision file are needed because the new rule breaks 25 tests outside `test_routing.py`. I checked
this with a prototype, described below. The README and decision file are small doc edits.

## Tests the new rule breaks (prototype run)

I applied the sketched `choose_device` locally (not pushed) and ran the 23 test files that build a device pool:
**26 failed, 1270 passed**. Only one failure is in `test_routing.py`. The other 25 come from two shared fixtures.
Each fixture has a box and a PC with equal `slot_max` (or a box with fewer slots), so the first claim is a 0%
tie. That tie used to go to the box (first in the file) and now goes to the PC.

- `tests/test_routing.py::test_pool_claims_spread_jobs_across_devices`: rewritten (AC4).
- `tests/test_remote_start.py`: `test_orch_wait_claims_a_box_slot_and_names_the_device`,
  `test_box_job_releases_its_slot_once_the_command_has_ended[done|failed|killed]`,
  `test_a_second_runner_is_refused_while_the_first_still_runs`,
  `test_monitor_releases_a_finished_box_slot_only_once_runner_and_worker_are_gone`,
  `test_r1_f13_box_takeover_is_refused_while_the_dead_runners_worker_lives`: these get `pc=False`.
  `test_orch_wait_places_on_the_remote_device_once_the_box_is_full` encodes the old order. It becomes
  `..._places_on_the_box_once_the_remote_is_full`: with box 1 and pc 1, the first job goes to the PC and the
  second to the box. The comment above `machines_file` ("an empty pool places on the box, which comes first")
  is updated.
- `tests/test_liveness_requeue.py` (8 tests, through `Poller(box_slots=3)` and the "most free slots: the box"
  comments): `Poller(..., pc=False)`, and the comments are updated.
- `tests/test_timeout_remote_worker.py::test_a_box_job_keeps_the_plain_timeout`,
  `tests/test_remote_checkout.py` (2 box-worktree tests),
  `tests/test_rounds_and_caps.py::test_claude_print_mode_gets_the_bg_wait_ceiling_on_the_box[claude-0|codex-None]`:
  `pc=False`.
- `tests/test_waiter.py::test_the_autostart_waiter_claims_remote_slots_too` (box 1, pc 3): the slots become
  `{first: "pc", second: "box"}`, because the PC at 1/3 loses to the box at 0/1. The comment says why.
- `tests/test_waiter_remote.py`: `test_a_box_placement_still_gets_a_worktree` and
  `test_box_jobs_still_finish_themselves` get `pc=False`.
  `test_autostart_places_on_a_remote_device_and_runs_without_worktree` no longer has to fill the box first:
  an empty pool now places on the PC. The test keeps asserting the remote path.

None of these tests changes what it asserts about the behaviour it was written for. Each one only stops relying
on the old tie-break to land a job on the box. Tests that are about placement order assert the new order.

## Acceptance criteria -> work

| # | Acceptance criterion | Work that meets it | Proof |
|---|---|---|---|
| AC1 | With all devices empty, tied at 0% utilization, the first job goes to the first remote in `machines.yml` (not the box). The next tied remote is chosen before the box. | Key parts 2 (`device.local`) and 3 (file order) in `choose_device`. | New `test_routing.py::test_a_tie_goes_off_box_to_remotes_in_machines_yml_order`. With box 4 listed first, pc 10 and mac 4: `{}` picks pc. `{"box": 2, "pc": 5, "mac": 2}` (all at 50%) picks pc. `{"pc": 1}` (mac 0% vs box 0%) picks mac. `{"pc": 10, "mac": 4}` picks box. A second config lists the box in the middle (pc, box, mac) to show that the rule follows `local`, not position. |
| AC2 | A device at lower utilization wins even when it has fewer free slots (for example, 0/4 beats 1/10). | Key part 1, an exact `Fraction(used, slot_max)`. | New `test_routing.py::test_choose_device_picks_the_lowest_utilization`. With box 4, pc 10, mac 4: `{"pc": 1}` picks mac (0/4 beats 1/10, even though the PC has 9 free). `{"pc": 1, "mac": 1}` picks box (0/4). `{"pc": 3, "mac": 1, "box": 1}` picks mac (25% vs 30% vs 25%, mac before box). Also an exact-tie case, box 2/3, pc 2/6 and mac 1/3, where the tie between pc and mac goes to pc by file order. The ticket's 9:23 AM burst replay (pc 1/10, box 0/4, mac 0/4, five jobs) gives mac, box, pc, pc, mac instead of five on the PC. |
| AC3 | A full device is never chosen. Disabled, placeholder and wrong-harness devices stay ineligible. | `device.eligible(harness)` plus `used < slot_max` filter the candidates before the key is computed. | `test_routing.py::test_a_full_device_is_never_chosen` (kept, plus one case where the only 0% device is full at 0 slots free). `test_device_pool.py::test_choose_device_skips_full_ineligible_placeholder_and_disabled` (kept) and its new assert that an ineligible device at 0% loses to an eligible device at 50%. `test_device_pool.py::test_placeholder_and_disabled_devices_refuse_to_schedule` (kept). |
| AC4 | The #85 routing tests describe the new rule and pass. `orch check-config` still passes. | `test_choose_device_picks_the_device_with_the_most_free_slots` is replaced by the AC2 test. `test_ties_go_to_the_earlier_device_in_machines_yml` is replaced by the AC1 test. `test_pool_claims_spread_jobs_across_devices` is rewritten. The 25 fixture-dependent tests are fixed as listed above. The SPEC and README wording are updated. No `machines.yml` or schema change. | Rewritten `test_pool_claims_spread_jobs_across_devices`: box 3, pc 6, mac 4, six claims through `DevicePool.claim` give pc, mac, box, pc, mac, pc. `used()` is `{"pc": 3, "mac": 2, "box": 1}`, and the trace is spelled out in a comment. The whole suite passes with `env -u ORCH_DB python3 -m pytest`, and so do `bin/orch check-config` and the CI check-config and statechart steps (`.github/workflows/ci.yml`). |

Every criterion is mapped.

## Risks and conflicts

- **High risk (placement).** A wrong key would send every job to one device again, or put a job on a full
  device. Two things guard against that: the `used < slot_max` filter runs before the key is computed, and the
  slot insert still happens inside the same `BEGIN IMMEDIATE` transaction. The concurrency test
  (`test_concurrent_claims_never_take_the_same_last_slot`) does not change and must still pass.
- **Behaviour change on the live pool.** Work spreads across devices sooner: an empty pool now puts the first
  job on the PC, not the box. That is the ticket's intent.
- **Conflicts with:** #112, #113, #131 and #218 (from the ticket). #112 and #131 are already merged into `main`
  (`bf535af`, `d92c62f`) and this branch has them. If `main` moves, I bring it in with a merge commit, never a
  rebase.

## Out of scope

- Weighting by device speed or link health, and the PC link drops. The ticket says this is placement, not an
  outage.
- `free_slots` and the feeder's capacity gate. They count total free slots and do not depend on which device
  is chosen.
