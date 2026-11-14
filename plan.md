# Plan for #152, per-project ship mode and round cap in the dashboard snapshot

The System page's Rules and limits panel (`System.dc.html`, SPEC 3.6 item 5) lists each project with its ship mode and round cap. The dashboard cannot show them today, so the packet marks them `[not shown by the dashboard today]`. This ticket adds the data. It adds a `projects` field to `status.json`, one entry per project in `orch.yml`, filled from the same parsed `factory.md` the poller acts on. #141 (T8, System page) renders it. This PR changes no page, so it commits no screenshots.

This plan is for PR #155 (branch `factory/issue-152`). Risk is low, one review round by Codex Bot, `ship: handoff`.

## How to read this

One box is one unit of work. Every box names the evidence that checks it. Check a box only when its evidence exists, a test run, a log line or a SHA.

## Grounding

These facts set the design. Each was read in the code at `main` `b90558e`.

- The round caps are not constants. `factory.md` sets `review.rounds.low` (at most 1) and `review.rounds.high` (1 to 3), and `lib/factory_md.py` `_round` enforces those ceilings. The dispatcher reads `ticket.factory.rounds_low` or `rounds_high` by risk (`lib/dispatcher.py:1291`). The `risk:high` label in `lib/feeder.py` only picks which of the two applies. So the snapshot reports both numbers from `FactoryConfig`, never `1` and `3` written down again.
- Ship mode is `FactoryConfig.ship`, one of `merge`, `handoff` or `after:<approver>` (`_ship`).
- The poller gets each project's `factory.md` from GitHub at `projects[].factory_ref` through `LivePollSource._factory` (`lib/poll_source.py:985`). That call parses with `parse_factory_md`, checks `base_branch == factory_ref`, and caches the result for one cycle. Its failures are `PollSourceError`s with our own messages, for example `has no projects[].factory_ref in orch.yml` or `factory.md: ship must be merge, handoff, or after:<approver>`.
- The dashboard is a separate, read-only process. It opens the database with `mode=ro`, reads `orch.yml` locally and makes no network calls. It has no copy of any project's `factory.md`.
- The dispatcher already writes state the dashboard reads back read-only, for example `ship_holds` (written in `lib/dispatcher.py`, read by `lib/status.py` `_hold_approvers`) and the feeder tables (`lib/feeder.py` `ensure_tables` and `feeder_items`).

## Design

### Candidates

1. **The poller records what it parsed.** Each cycle the poller writes one row per project with the fields it parsed, or the reason it could not. The dashboard reads the table. One parser, one fetch path, one trust check. The dashboard stays offline and read-only.
2. **The dashboard fetches `factory.md` itself.** It would call `gh api` from the dashboard writer and parse. This puts GitHub credentials and network calls in the read-only process, and its answer could differ from what the poller acted on in the same minute. Rejected.
3. **The dashboard parses a local `factory.md`.** Only this repo's own file is on disk. The other projects have none. Rejected.

Candidate 1 is the design. It is also the shape `ship_holds` already uses, so the next reader finds a familiar pattern.

### Data shape

A new module owns the table, both its write and its read, the way `lib/feeder.py` owns the feeder tables.

```python
# lib/project_rules.py

TABLE = "project_rules"

@dataclass(frozen=True)
class FactoryRead:
    """What one poll cycle learned about one project's factory.md.
    Exactly one of config and reason is set."""
    repo: str
    config: FactoryConfig | None
    reason: str | None

def ensure_table(connection: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS project_rules (
           repo TEXT PRIMARY KEY,       -- lowercased
           read_at REAL NOT NULL,
           tickets TEXT,                -- github | linear
           ship TEXT,                   -- merge | handoff | after:<approver>
           rounds_low INTEGER,
           rounds_high INTEGER,
           reason TEXT,                 -- set only when factory.md was unreadable
           CHECK ((reason IS NULL) = (ship IS NOT NULL)))"""

def record_reads(connection: sqlite3.Connection, reads: Sequence[FactoryRead], now: float) -> None:
    """Replace every row with this cycle's reads in one transaction.
    A project dropped from orch.yml loses its row."""

def read_rows(connection: sqlite3.Connection) -> dict[str, sqlite3.Row] | None:
    """repo -> row. None when the table does not exist (an older database)."""

def snapshot_projects(
    projects: Sequence[Project], rows: dict[str, sqlite3.Row] | None, now: float
) -> list[dict]:
    """One entry per orch.yml project, in orch.yml order."""
```

```python
# lib/poll_source.py, LivePollSource

def factory_reads(self) -> tuple[FactoryRead, ...]:
    """One FactoryRead per orch.yml project through self._factory, so it shares
    the cycle cache and every check the poll itself applies. A PollSourceError
    keeps its message as the reason. Any other exception keeps only its class
    name, as record_poll_error does."""
```

```python
# lib/dispatcher.py, Dispatcher.poll_once, just before end_cycle

reads = getattr(source, "factory_reads", None)   # fake sources may not have it
if reads is not None:
    record the reads, or record_poll_error("factory", "-", <class name>) on failure
```

The read runs at the end of the cycle, so GitHub projects reuse the cached parse from `github_candidates`. Linear projects with no tracked ticket cost one conditional `contents/factory.md` call per cycle, which goes through the same `--cache 60s` and ETag path as the rest.

### Snapshot shape

`collect_snapshot` reads the rows in its existing read-only transaction and adds `projects`, built from the `orch` config it already receives.

```json
"projects": [
  {"repo": "maxharris1/factory", "status": "ok", "tracker": "github",
   "ship": "handoff", "approver": null,
   "rounds": {"low": 1, "high": 3}, "read_age_seconds": 12},
  {"repo": "maxharris1/tracy-harris-coaching", "status": "ok", "tracker": "github",
   "ship": "after", "approver": "tracy-site",
   "rounds": {"low": 1, "high": 3}, "read_age_seconds": 12},
  {"repo": "aut0-code/aut0", "status": "unknown", "tracker": "unknown",
   "ship": "unknown", "approver": null, "rounds": null,
   "reason": "aut0-code/aut0 factory.md: ship must be merge, handoff, or after:<approver>",
   "read_age_seconds": 12}
]
```

- `status` is `ok` or `unknown`. `unknown` always carries a `reason` and never a guessed value. With no row for the repo (table missing, or the poller has not read it yet) the reason is `not read by the poller yet` and `read_age_seconds` is null.
- `tracker` is `factory.md`'s `tickets`, the field the poller routes on. `ship` is `merge`, `handoff`, `after` or `unknown`, and `approver` is the login after `after:`. Both go through the dashboard's `_enum`, so a value outside the known set shows as `other`.
- `reason` goes through `_clean` (length cap and `redact`), and the whole snapshot still goes through `_scrub`.
- With no `orch.yml` (`orch is None`) there is no project list, and `projects` is `[]`.
- `read_age_seconds` lets #141 say how old the read is. The latest read always wins. A cycle that cannot read `factory.md` turns the row to `unknown` rather than keeping an older good value.

`SCHEMA` stays `factory-dashboard/v1`, because the field is additive.

## Acceptance criteria map

| # | Criterion (from #152 and the Mission Control ground rules) | Work that meets it | Evidence |
|---|---|---|---|
| AC1 | A snapshot field `projects`, one entry per project in `orch.yml`, with repo, tracker, ship mode (merge, handoff, after an approver's OK) and round cap for low and high risk. | `snapshot_projects` plus the `projects` key in `collect_snapshot`. | `test_snapshot_projects_follow_orch_yml_order` and the AC4 tests. |
| AC2 | Read from the same parsed config the dispatcher uses. No second parser, no hard-coded copies. | `factory_reads` calls `LivePollSource._factory`, the same call `github_candidates` and `tracked_tickets` use. The dashboard only reads stored fields. | `test_factory_reads_use_the_poll_cycle_cache` (one `contents/factory.md` call per repo per cycle). `test_round_caps_come_from_factory_md` (a fixture with `high: 2` shows 2). Reviewer reads that the dashboard imports no parser. |
| AC3 | An unreadable `factory.md` gives an explicit `unknown` with the reason, not a default. | `FactoryRead.reason`, the table `CHECK`, and the `unknown` entry. | `test_unreadable_factory_md_is_unknown_with_reason` covers an invalid front block, a missing `factory_ref`, a `base_branch` mismatch, and a gh failure (class name only). `test_no_row_is_unknown_not_read_yet` covers an older database with no table. |
| AC4 | Values equal the fixture configs for a merge project, a handoff project and an approver project. | Fixtures are `examples/cleffy/factory.md` (`ship: merge`), this repo's `factory.md` (`ship: handoff`) and `examples/tracy/factory.md` (`ship: after:tracy-site`), served through the existing `FakeGh` in `tests/test_github_only_project.py`. | `test_projects_match_merge_handoff_and_approver_fixtures`. It runs a real `poll_once` over `LivePollSource` and `FakeGh`, then `collect_snapshot`, and compares each entry to `load_factory_md` on the same fixture file. |
| AC5 | Redaction tests pass. | `reason` goes through `_clean` and `_scrub`. | The existing redaction tests pass unchanged. A new `test_project_reason_is_redacted` stores a token-shaped reason and checks the snapshot hides it. |
| G1 | Read-only. The dashboard never writes the database and makes no network call. | The dashboard only `SELECT`s `project_rules` inside its existing `mode=ro` transaction. | Existing read-only tests pass. Reviewer reads the diff. |
| G2 | Existing dashboard tests pass. A test changes only where this ticket's spec moves a field. | One change. `tests/test_dashboard.py:506` pins the top-level key set and gains `projects`. The PR body names it. | Full suite run with `env -u ORCH_DB`. |
| G3 | No external assets. CSP unchanged. | No page or script change in this PR. | `test_page_has_no_external_assets_or_markup_sinks` passes unchanged. |
| G4 | Screenshots per touched page. | No page is touched, so none apply. #141 renders the field and commits the System screenshots. | `git diff --stat main` shows no `lib/dashboard_page.py` change. |

## Work

- [ ] **1. Table module.** Add `lib/project_rules.py` with `FactoryRead`, `ensure_table`, `record_reads`, `read_rows` and `snapshot_projects`. Call `ensure_table` where the dispatcher creates its other tables. Evidence: unit tests for `record_reads` (replace semantics, dropped project, `CHECK` rejects a row with both a reason and values) and for `snapshot_projects` (orch.yml order, missing row, `after:` split).
- [ ] **2. Poller read.** Add `LivePollSource.factory_reads` and the `poll_once` hook. Evidence: AC2 cache test and AC3 failure tests over `FakeGh`. An existing fake source with no `factory_reads` still polls, and the existing dispatcher tests pass.
- [ ] **3. Snapshot field.** Read `project_rules` in `collect_snapshot` and add `projects`. Update the key-set test. Evidence: AC1, AC4 and AC5 tests.
- [ ] **4. Docs.** Add `projects` to the `lib/dashboard.py` docstring and the README's `status.json` field list, if it lists fields. In `docs/design/mission-control/STATUS.md`, note that D3 #152 supplies the data for SPEC 3.6 item 5. Evidence: the diff.
- [ ] **5. Verify.** Run the full suite with `env -u ORCH_DB` (chunked if the box is loaded), `orch check-config`, and the statechart check, the same three steps as CI. Generate a snapshot from a temp database after one fake poll and paste its `projects` block in the PR body. Evidence: command output in the PR body and green CI on the pushed head.

## Out of scope

- Rendering the panel. #141 (T8, System page) ports `System.dc.html` and reads `projects`.
- Showing the hold label or hold paths. The panel does not ask for them.
- Keeping a last-known-good value across failed reads. The ticket asks for `unknown` with a reason.
