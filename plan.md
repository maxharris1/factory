# Plan: #161 Prompt refactor T27, per-device factory checkout root

PR #229, branch `factory/issue-161`, risk high. Builder claude-bot, reviewer codex.

## Goal

`machines.yml` gains a per-device `factory_root`: the absolute path of that
device's **deployed factory checkout**, the folder that holds `skills/`,
`bin/orch` and so on. It is not `workdir_root`, which is the empty folder that
job worktrees are made under. T09 (#180) will use this field to render absolute
skill paths in the brief. The brief is rendered on the box but read on the
device the job runs on.

## Architect scaffold

### What exists today
- `lib/machines_yml.py`: a closed v1 schema (`_DEVICE_KEYS`) and a frozen
  `Device` dataclass. `workdir_root` is required on remote devices, refused
  under `/mnt/`, and may use a whole `{bot}` path component that
  `Device.workdir_for(harness)` resolves. `ssh_alias` has the same shape and
  `ssh_alias_for`. Placeholders (`REPLACE_WITH_…`) make a device
  unschedulable. `check-config --allow-placeholders` lets them through as
  warnings.
- `machines.yml` (repo copy): the box (local, 3 slots), the PC (disabled, 3
  slots), the Mac (disabled, placeholders). This is the older template. The
  live file on the box has Max's slots (box 4, PC 10, Mac 4) and the PC and
  Mac turned on.
- `config/machines.example.yml`: the minimal one-box example. CI runs
  `check-config` on it, and `tests/test_cli.py` and `tests/test_machines_yml.py`
  load it.
- The box's deploy units run from `%h/factory` (`deploy/orch-poll.service`,
  `deploy/orch-waiter@.service`). A design note names the box checkout as
  `/home/box/factory`.

### Shape of the change
- A new key, `factory_root`, in `_DEVICE_KEYS`, and a new field
  `Device.factory_root: str | None`.
- Validation reuses `_workdir_problem`, so the path must be absolute, normalized
  and not under `/mnt/`, and `{bot}` may only be a whole path component. A
  per-bot checkout such as `/home/{bot}/factory` is allowed, because remote jobs
  run as the bot user and the paths have to exist for that user.
- **Remote device:** `factory_root` is required. If it is missing the device is
  refused with: `missing required field 'devices[i].factory_root' (every remote
  device names its deployed factory checkout, not workdir_root)`. A
  `REPLACE_WITH_` value goes through the existing placeholder path: an error by
  default, a warning with `--allow-placeholders`, and the device is not
  scheduled.
- **Local device:** `factory_root` is accepted and validated the same way, but
  not required. The shipped `machines.yml` and the example both set it on the
  box. This is a deliberate narrowing of the ticket's "required on every
  device": the acceptance criteria only require refusing a remote device, and
  making it required on the local device would break about 40 local-device
  fixtures across 14 test files, plus the live box entry, for no gain in
  safety. The reviewer or Max may overturn this. The change is one line plus
  fixture churn.
- One extra check: `factory_root` must not equal `workdir_root` and must not be
  inside it. Comparing them with `{bot}` resolved the same way for both is
  enough. Job worktrees under the deployed checkout, or a checkout under the
  workdir, would mix the two concepts this ticket separates.
- Helpers:
  - `Device.factory_root_for(harness) -> str | None`: the value with `{bot}`
    resolved, the same contract as `workdir_for`. It returns None when the
    field is unset or a placeholder, or when the device does not list the
    harness.
  - `MachinesConfig.factory_root(device_name, harness) -> str | None`: looks up
    the device and calls `factory_root_for`. T09 calls this with the placed
    device and harness.
- Module docstring: one paragraph on `factory_root` and how it differs from
  `workdir_root`.

### What is out of scope
- Rendering skill paths in the brief (that is T09, #180).
- Deploying a checkout on the PC or the Mac, or checking that the path exists
  on the device. Nothing in this module checks reachability today; a preflight
  existence check belongs with T09 or remote preflight.
- The live `machines.yml` on the box. It is not in this repo, and this job runs
  on the PC.

## Work items

1. **`lib/machines_yml.py`**: add the key, the dataclass field, validation in
   `_device` (written next to the `workdir_root` block), the
   not-equal-and-not-nested check against `workdir_root`, the two helpers, and
   the docstring paragraph.
2. **`machines.yml` (repo copy)**: add `factory_root` to each device, and extend
   the header comment with one line that says what it is and that it is not
   `workdir_root`. Slot counts and `enabled` flags are left exactly as they are
   on `main`.
   - box: `factory_root: /home/box/factory` (`%h/factory` in the deploy units).
   - pc: `factory_root: /home/{bot}/factory` (each bot's own checkout, the same
     convention as the box).
   - mac: `factory_root: /Users/{bot}/factory` (the Mac is still disabled, and
     its other fields are placeholders).
3. **`config/machines.example.yml`**: add `factory_root` to the box with a
   comment that says it is the deployed checkout (skills, `bin/orch`), not
   `workdir_root` (where job worktrees go, empty at job start). Add a commented
   remote-device example showing both fields side by side, so the difference is
   plain.
4. **Fixtures**: add `factory_root:` to every remote device in the inline test
   YAML (the blocks with `workdir_root:`, about 30 across `test_cli`,
   `test_device_pool`, `test_remote_start`, `test_health`, `test_status`,
   `test_waiter*`, `test_claim_placement`, `test_detached_remote`,
   `test_remote_darwin`, `test_remote_checkout`, `test_dashboard`,
   `test_feeder`, `test_routing`, `test_codex_state_dir`, `test_machines_yml`
   and `tests/pool_helpers.py`). This is mechanical; no assertions change.
5. **`tests/test_machines_yml_factory_root.py`** (new). Every case loads YAML
   from `tmp_path` or from the repo's own files, and none reads a bot home,
   `~`, an env file or a credential. The cases:
   - a remote device without `factory_root` is refused, with the message
     naming the field and `workdir_root`;
   - a remote device with a valid `factory_root` loads;
   - the local device loads both with and without it;
   - `REPLACE_WITH_` is an error by default, a warning with
     `allow_placeholders`, and leaves the device unschedulable;
   - bad values are refused: a relative path, a path under `/mnt/`, `..`, a
     path that is not normalized, a `{bot}` that is not a whole path
     component, a value that is not a string;
   - `factory_root` equal to `workdir_root`, or nested inside it, is refused;
   - `factory_root_for` resolves `{bot}` per harness and returns None for a
     harness the device does not list;
   - `MachinesConfig.factory_root` returns `/home/box/factory`,
     `/home/claude-bot/factory` and `/Users/claude-bot/factory` for the box,
     the PC and the Mac in a three-device fixture with all values set;
   - the repo's `machines.yml` loads with `allow_placeholders=True` and
     returns the box and PC roots;
   - `config/machines.example.yml` loads cleanly, and its text names
     `factory_root` and `workdir_root` in separate comments.
6. **Verification**: `env -u ORCH_DB python -m pytest -q`, plus both CI
   `check-config` steps (`bin/orch check-config` with the defaults, and with
   `--machines config/machines.example.yml`), plus the statechart check.

## Acceptance criteria, mapped to the work

| Criterion | Work that meets it | Proof |
|---|---|---|
| `check-config` / machines load accepts a per-device `factory_root` and refuses a remote device that lacks it. | Item 1 (key, validation, remote-required rule); items 2 to 4 keep every shipped file and fixture loading. | Item 5: refused-when-missing, accepted-when-present, local optional; the CI `check-config` steps on `machines.yml` and the example. |
| The field is documented in the example file and is clearly not `workdir_root`. | Item 3 (example comments, the box value, the commented remote example side by side); item 2 header line; item 1 docstring; the not-equal-and-not-nested check in item 1. | Item 5: the example loads, its text names both fields, and equal or nested values are refused. |
| A helper on the loaded config returns the absolute factory root for the box, the PC and the Mac once values are set. | Item 1 (`Device.factory_root_for`, `MachinesConfig.factory_root`); item 2 values for all three devices. | Item 5: the three-device helper test, and the repo `machines.yml` test. |
| Tests do not require reading bot homes or credentials. | Item 5 uses only `tmp_path` and repo files. Paths such as `/home/claude-bot/factory` are compared as strings and never touched on disk. | Review of the new test file; no `Path.home()`, `expanduser` or env reads. |

## Deploy note (high risk: the live file)

When this merges, the box's live `machines.yml` must have `factory_root` on the
PC and the Mac. If it does not, `check-config` and every machines load refuse
the whole file, and the poller and waiters stop placing work. Before pulling
this onto the box, add one line per remote device to the live file and change
nothing else. Max's slots (box 4, PC 10, Mac 4) and the `enabled` flags stay as
they are. The PR description will give the exact lines. Then run
`orch check-config` on the box before restarting anything. The PC and Mac values
here (`/home/{bot}/factory`, `/Users/{bot}/factory`) follow the box's
`%h/factory` convention. If Max deploys those checkouts somewhere else, only
the live values change, not the code.
