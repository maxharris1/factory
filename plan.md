# Plan for #156, feeder blocked-by and file-conflict gates

Max wants to label a whole chain of tickets `factory:ready` at once and walk away. Today the feeder takes every labelled ticket as soon as there is room, so a chain like #138 to #152 has to be released one ticket at a time by a bot or by Max. After this PR the feeder reads `Blocked by`, `Touches:` and `Conflicts with` lines (and Linear blocked-by relations). Each poll it feeds a ticket only once every blocker is done and nothing in flight shares a path or a conflicts-with edge with it. A ticket that waits shows why in `orch status` and on the dashboard. A cycle or a blocker that was closed without finishing raises needs-attention instead of waiting quietly.

The smallest change that meets it is one pure module that parses the lines and decides the gates, one batched and cached status read per poll, a new `waiting-blocked` feeder state with a reason column, and three new needs-attention reasons. Tickets with none of these lines go through the feeder exactly as today and cost no extra API call.

This plan is for PR #157 (branch `factory/issue-156`). Risk is high. Codex Bot reviews it, up to three rounds. Grok Bot merges on `ship: handoff`.

## How to read this

One box is one unit of work. Every box names the evidence that checks it. Check a box only when its evidence exists: a test run, a log line or a SHA. Tests alone are not sufficient verification. The PR is verified only when its unit, revert and CI boxes are all checked.

## The data shape

Everything the gates need is a fact about a ticket's body and about the status of the tickets it names. The pure module owns both shapes and every decision. `lib/feeder.py` only collects inputs, asks the module and writes rows.

```python
# lib/feeder_gates.py (new). Pure: no I/O, no SQLite, no clock.

@dataclass(frozen=True, order=True)
class Ref:
    """A ticket or PR a gate line names, or a candidate itself.
    GitHub: repo is owner/repo lowercased, key the number as text.
    Linear: repo is the project's repo lowercased, key the identifier (ENG-12)."""
    tracker: Literal["github", "linear"]
    repo: str
    key: str

    def label(self, home: str) -> str: ...   # "#138", "owner/other#5", "ENG-12"

@dataclass(frozen=True)
class GateLines:
    blocked_by: frozenset[Ref] = frozenset()
    touches: frozenset[str] = frozenset()       # normalized paths; "dir/" means the folder
    conflicts_with: frozenset[Ref] = frozenset()

def parse_gate_lines(body: str, repo: str) -> GateLines: ...

class Progress(Enum):
    DONE = "done"            # PR merged; issue closed-completed or a linked PR merged; Linear completed
    OPEN = "open"
    UNFINISHED = "unfinished"  # PR closed unmerged; issue closed not planned or duplicate; Linear canceled
    MISSING = "missing"      # no such issue or PR, or not readable

@dataclass(frozen=True)
class RefStatus:
    progress: Progress
    is_pull: bool
    detail: str = ""         # "closed without merge", "closed as not planned"

@dataclass(frozen=True)
class Wait:
    reason: str                    # "waiting on #138", "waiting on file lib/dashboard.py held by #151"
    attention: str | None = None   # "blocker-cycle" | "blocker-closed" | "blocker-missing" | None

def blocker_waits(
    gated: Mapping[Ref, GateLines],      # candidates not started yet
    statuses: Mapping[Ref, RefStatus],   # every blocker named by them
    unreadable: frozenset[Ref],          # blockers whose read failed this poll
) -> dict[Ref, Wait]:
    """Cycles first (Tarjan SCC over edges to blockers that are not DONE and are
    themselves gated candidates; a self-reference is a cycle). Then UNFINISHED or
    MISSING blockers, then OPEN or unreadable ones. Absent key: no blocker wait."""

@dataclass
class Holds:
    """Paths and conflicts-with edges held by tickets in flight. Starts from the
    feeder's fed rows whose PR is still open, then grows as this poll selects."""
    def collision(self, ref: Ref, lines: GateLines) -> Wait | None: ...
    def take(self, holder: Ref, aliases: frozenset[Ref], lines: GateLines) -> None: ...
```

The client grows one read. Linear needs no new request.

```python
# lib/feeder.py
class GitHubIntake(Protocol):
    ...
    def ref_statuses(self, refs: Collection[Ref]) -> Mapping[Ref, RefStatus]: ...

@dataclass(frozen=True)
class IntakeIssue:
    ...
    blocked_by: tuple[tuple[str, str], ...] = ()   # Linear relations: (identifier, state type)
```

```python
# lib/attention.py (new). The needs_attention table, shared by the dispatcher and the feeder.
def ensure_table(connection) -> None: ...                 # the DDL moved from dispatcher.py
def sync(connection, owner_reasons: frozenset[str], scope: frozenset[str],
         flags: Mapping[tuple[str, str, str], str], now: float) -> None: ...
```

Feeder bookkeeping. `STATES` gains `waiting-blocked`. `feeder_items` gains `waiting_reason TEXT`, `touches TEXT` (JSON list), `conflicts TEXT` (JSON list of ref labels) and `released INTEGER NOT NULL DEFAULT 0`, added to existing databases by `PRAGMA table_info` plus `ALTER TABLE` (the `lib/device_pool.py` pattern). `feeder_counts` gains `waiting_blocked`.

### The parsing rules

- A gate line is one whole line, outside fenced code, that starts with an optional list marker and optional `**`, then `Blocked by`, `Touches` or `Conflicts with` (any case), then an optional colon and closing `**` in either order. This matches the real bodies, for example `**Blocked by:** #137, PR #111, PR #116.` on #138 and `**Conflicts with:** #152` on #151. A line that starts with a backtick (this ticket's own examples) is not a gate line.
- On a `Blocked by` or `Conflicts with` line the refs are `owner/repo#N`, `#N` (the ticket's own repo, `PR #N` and `open PR #N` included) and `https://github.com/owner/repo/issues/N` or `/pull/N`. Words between refs are ignored. Only the same line counts. Prose on the next line is never read, as the ticket asks.
- On a `Touches:` line, paths split on commas and spaces. Backticks, a leading `./` and trailing punctuation are stripped. Two paths collide when equal, or when one ends in `/` and the other is inside it.
- On a Linear ticket, `#N` resolves against the project's GitHub repo, and the relations come from Linear (below). A Linear identifier written in body text is not parsed. Linear's own relation is the way to say it.

### How a blocker is resolved

- GitHub. One GraphQL read per blocker repo per poll, with aliases (chunks of 50), each `issueOrPullRequest(number: N)`. A `PullRequest` is DONE only when `merged`. Closed unmerged is UNFINISHED. An `Issue` is DONE when closed with `stateReason` COMPLETED (or null, GitHub's old default), or when any `closedByPullRequestsReferences(includeClosedPrs: true)` PR is merged, or when the feeder's own row for that issue has a PR the same read shows merged. Closed as NOT_PLANNED or DUPLICATE is UNFINISHED. A null node is MISSING. GitHub's `__typename` decides PR or issue; the `PR` word on the line is only a hint.
- The read goes through `GitHubIntakeClient._json` with a per-call `--cache 60s` (a `cache_ttl` keyword added to `GitHubReviewActions._call` and `_json`). The client's other reads stay uncached, so `find_pull` can never return a stale "no PR" and open a second one.
- Linear. `LINEAR_INTAKE_QUERY` adds `inverseRelations(first: 10) { nodes { type issue { identifier state { type } } } }`. A relation of type `blocks` names a blocker. Its state type `completed` is DONE, `canceled` is UNFINISHED, anything else OPEN. That rides the existing intake request on `_linear_request`, so it uses the poll's Linear budget and backoff and adds no request.
- The feeder calls `ref_statuses` at most once per poll, with the deduplicated union of what the gates need. With no gate line on any candidate and no holder row, it makes no call at all.
- A failed read fails closed. Every candidate that needed it waits with `waiting on #N (status unreadable)`, the poll counts one error, and no attention flag is raised for a transient failure.

### One poll, in order

1. `_candidates`, `_record_seen` (also stores `touches` and `conflicts`), `_mark_done`, `_room`, as today.
2. Parse lines for every candidate. Only candidates not yet started (state `eligible`, `waiting-capacity` or `waiting-blocked`) are gated. A resumed feed (`feeding`, `error`) already passed its gates and is in flight.
3. Build the holder set from feeder rows in `feeding`, `error`, `fed` or `done` with `released = 0` and at least one declared path or conflicts-with ref. Their PR refs go into the same status read. A PR shown merged or closed sets `released = 1` and stops holding. Released is sticky, so a fed ticket is read at most until its PR ends.
4. `ref_statuses` once. `blocker_waits` once.
5. The feed loop keeps today's order: resumed first, then urgent, bug, oldest, then the ticket key compared as a number when both are numbers (the ticket's "lower number" tie-break; the old string compare put `10` before `9`). For a gated candidate, a blocker wait or a `Holds.collision` sets `waiting-blocked` with its reason and `continue`s **before** room is read. So a waiting ticket never takes a harness slot. Otherwise the capacity branch runs unchanged. A candidate selected to feed is added to `Holds` before `_feed`, so a later ticket in the same poll collides with it. Because the loop is in priority order, the earlier ticket always wins a collision.
6. `attention.sync` replaces the feeder's own flags (`blocker-cycle`, `blocker-closed`, `blocker-missing`) for the projects whose intake was read this poll. A flag clears by itself once the cause is gone, the ticket is fed, or it loses its signal. A project whose intake read failed keeps its flags, so they do not flap.

`_record_seen` already forgets an unfed ticket that lost its signal. `waiting-blocked` joins `eligible` and `waiting-capacity` in that delete.

### What people see

- `orch status`: `Feeder — eligible 9, fed 2, waiting for capacity 1, waiting on blockers or files 6`, then one line per item, for example `owner/app#7 waiting on #6` and `owner/app#9 waiting on file lib/dashboard.py held by #8`. Cycles and closed blockers also appear under `Needs attention`, for example `owner/app#4 blocker-cycle: #4 -> #5 -> #4`.
- Dashboard snapshot: `feeder` gains `waiting_blocked` and each item gains `reason` (cleaned, at most 200 characters). `ATTENTION_REASONS` gains the three reasons. The page's `renderFeeder` script and the server-rendered `_feeder` in `lib/dashboard_page.py` show the reason after the state.

## Criterion-to-work map

| # | Acceptance criterion | Work that meets it | Tests that fail on revert |
| --- | --- | --- | --- |
| 1 | A `factory:ready` (or Linear intake-state) ticket whose body has `Blocked by #N` / `Blocked by owner/repo#N` / a PR URL is not fed until every blocker is merged (PR) or closed-completed / linked-PR-merged (issue). | Units 1, 2, 3 | `test_parse_blocked_by_forms`, `test_parse_real_chain_lines`, `test_pr_blocker_done_only_when_merged`, `test_issue_blocker_done_when_closed_completed`, `test_issue_blocker_done_when_a_linked_pr_merged`, `test_issue_blocker_done_when_its_feeder_pr_merged`, `test_ref_statuses_reads_issue_and_pr_kinds`, `test_a_blocked_ticket_is_not_fed_until_every_blocker_is_done` |
| 2 | Linear-sourced tickets honor Linear blocked-by relations when the intake API returns them, and the same body lines on the description. | Units 1, 2, 3 | `test_linear_intake_reads_blocks_relations`, `test_linear_relation_blocker_waits_until_completed`, `test_linear_description_blocked_by_line_resolves_on_the_project_repo` |
| 3 | Re-evaluation is every feeder poll: after a blocker merges, the dependent is fed on the next `feed_once()` with no relabel and no bot handoff. | Unit 3 | `test_dependent_is_fed_on_the_poll_after_its_blocker_merges` (integration, fake client, same style as `test_waiting_items_are_fed_once_a_slot_frees`) |
| 4 | Optional `Touches:` and `Conflicts with #N` lines prevent two in-flight tickets from sharing a path or a conflicts-with edge. On collision, urgent then bug then oldest then lower number wins; the other waits with a reason. | Units 1, 4 | `test_parse_touches_and_conflicts`, `test_paths_collide_on_equal_or_folder`, `test_collision_keeps_urgent_then_bug_then_oldest_then_lower_number`, `test_a_fed_ticket_with_an_open_pr_holds_its_paths`, `test_a_holder_releases_when_its_pr_merges_or_closes`, `test_conflicts_with_is_symmetric_and_matches_issue_or_pr_number`, `test_two_tickets_sharing_a_path_are_not_selected_in_one_poll` |
| 5 | Waiting reasons (`waiting on #N`, `waiting on file X held by #M`) appear in orch status and the dashboard feeder snapshot. | Unit 5 | `test_orch_status_shows_waiting_reasons`, `test_dashboard_snapshot_shows_waiting_reasons`, `test_dashboard_page_renders_the_feeder_reason` |
| 6 | Cycles and blockers closed-not-merged are raised as needs-Max attention, not left waiting with no alert. | Units 2, 3, 5 | `test_cycle_detection_two_and_three_long_and_self`, `test_a_cycle_flags_every_participant_and_feeds_none`, `test_a_blocker_closed_unmerged_flags_needs_attention`, `test_a_blocker_closed_not_planned_flags_needs_attention`, `test_attention_clears_when_the_cause_is_gone`, `test_status_and_dashboard_list_the_blocker_flags` |
| 7 | Waiting on a blocker or a file conflict does not consume a harness slot. | Unit 3, 4 | `test_a_blocked_ticket_takes_no_room` (box of 1 slot: a blocked older ticket and an unblocked newer one; the newer is fed), `test_a_conflicting_ticket_takes_no_room` |
| 8 | Unit tests cover parsing, gate decisions, collision tie-breaking, and cycle detection; an integration test feeds the dependent on the cycle after its blocker merges. | Units 1 to 4 | Everything in rows 1, 3, 4 and 6 |
| 9 | Blocker status reads reuse the existing GitHub `gh api` cache seam and the Linear poll budget; no uncached N+1 per poll. | Units 2, 3 | `test_ref_statuses_is_one_cached_graphql_read_per_repo` (fake runner asserts one `graphql` call per repo, `--cache 60s` on it, and none on `find_pull`), `test_feeder_reads_statuses_once_per_poll_for_a_long_chain` (15-ticket chain, one `ref_statuses` call per poll), `test_linear_relations_add_no_linear_request` |
| 10 | Existing intake trust, capacity (`waiting-capacity`), feed-once idempotency, and urgent/bug/oldest ordering are unchanged for tickets with no blocker or conflict lines. | Unit 3 (gates are a no-op without lines) | Every existing test in `tests/test_feeder.py` passes untouched, plus `test_no_gate_lines_means_no_status_read` |

Every criterion is mapped.

## Work units

Each unit is one commit that ends green under `env -u ORCH_DB pytest`. Test first in every unit. New tests go in `tests/test_feeder_gates.py`, which imports the fakes from `tests/test_feeder.py`. Live-client tests go in `tests/test_intake_live.py`. Status and dashboard tests go next to the existing feeder ones.

### Unit 1. Parse gate lines (AC 1, 2, 4, 8)

- [ ] New `lib/feeder_gates.py` with `Ref`, `GateLines` and `parse_gate_lines` per the parsing rules.
- [ ] `test_parse_blocked_by_forms`: `Blocked by #4`, `Blocked by: #4`, `**Blocked by:** #4 and #5`, `Blocked by #4, PR #9, open PR #10`, `Blocked by other/repo#3`, issue and pull URLs, mixed case, a list marker.
- [ ] `test_parse_real_chain_lines`: the lines of #138, #141, #145, #150 and #151 as they read on 2026-11-13, pasted as fixtures. #150 gives `{#145, #113}`, and its trailing prose names no path.
- [ ] `test_parse_ignores_prose_backticks_and_fences`: `Open PRs #111 and #116 ...` on the next line, this ticket's own backticked examples, and a fenced block are not gate lines.
- [ ] `test_parse_touches_and_conflicts` and `test_paths_collide_on_equal_or_folder`.

### Unit 2. Resolve statuses, cheaply (AC 1, 2, 6, 9)

- [ ] `lib/github_actions.py`: a keyword-only `cache_ttl` on `_call` and `_json` that overrides `self.cache_ttl` for one call. No other caller changes.
- [ ] `lib/intake_live.py`: `GitHubIntakeClient.ref_statuses`, one aliased GraphQL read per repo in chunks of 50, with `cache_ttl="60s"`. `LINEAR_INTAKE_QUERY` gains `inverseRelations`, and `linear_intake` fills `IntakeIssue.blocked_by`.
- [ ] `lib/feeder_gates.py`: `Progress`, `RefStatus`, and `linear_status(state_type)`.
- [ ] Tests: `test_ref_statuses_reads_issue_and_pr_kinds` (merged PR, closed unmerged PR, open PR, issue closed completed, closed not planned, closed duplicate, null reason, linked PR merged, null node), `test_ref_statuses_is_one_cached_graphql_read_per_repo`, `test_linear_intake_reads_blocks_relations` (only `blocks` relations, inverse direction), `test_linear_relations_add_no_linear_request`.

### Unit 3. The blocked-by gate in the feed loop (AC 1, 2, 3, 6, 7, 9, 10)

- [ ] `lib/feeder_gates.py`: `Wait` and `blocker_waits`, with Tarjan SCC for cycles. A cycle reason lists the loop in ticket order.
- [ ] New `lib/attention.py`: `ensure_table` (the DDL moved out of `lib/dispatcher.py`, which now calls it) and `sync`.
- [ ] `lib/feeder.py`: the new state, columns and migration. `feed_once` per "One poll, in order". `_set_state` writes `waiting_reason` and clears it on any other state. `_Candidate.order()` compares numeric keys as numbers.
- [ ] `FakeGitHub` gains `statuses` and `ref_statuses`, counting calls.
- [ ] Tests: `test_pr_blocker_done_only_when_merged`, `test_issue_blocker_done_when_closed_completed`, `test_issue_blocker_done_when_a_linked_pr_merged`, `test_issue_blocker_done_when_its_feeder_pr_merged`, `test_a_blocked_ticket_is_not_fed_until_every_blocker_is_done`, `test_dependent_is_fed_on_the_poll_after_its_blocker_merges`, `test_a_blocked_ticket_takes_no_room`, `test_cycle_detection_two_and_three_long_and_self`, `test_a_cycle_flags_every_participant_and_feeds_none`, `test_a_blocker_closed_unmerged_flags_needs_attention`, `test_a_blocker_closed_not_planned_flags_needs_attention`, `test_attention_clears_when_the_cause_is_gone`, `test_an_unreadable_status_fails_closed_without_a_flag`, `test_linear_relation_blocker_waits_until_completed`, `test_linear_description_blocked_by_line_resolves_on_the_project_repo`, `test_feeder_reads_statuses_once_per_poll_for_a_long_chain`, `test_no_gate_lines_means_no_status_read`, `test_a_waiting_blocked_ticket_that_loses_its_label_is_forgotten`, `test_existing_database_gains_the_new_columns`.

### Unit 4. The file-conflict gate (AC 4, 7)

- [ ] `lib/feeder_gates.py`: `Holds`. A holder is known by its issue ref and its PR ref, so `Conflicts with #151` and `Conflicts with #<its PR>` both match. The edge counts from either side. A `Conflicts with` ref that is not a feeder ticket but resolves to an open PR also holds.
- [ ] `lib/feeder.py`: holders from fed rows, sticky `released`, `Holds.take` before `_feed`.
- [ ] Tests: `test_collision_keeps_urgent_then_bug_then_oldest_then_lower_number`, `test_a_fed_ticket_with_an_open_pr_holds_its_paths`, `test_a_holder_releases_when_its_pr_merges_or_closes`, `test_conflicts_with_is_symmetric_and_matches_issue_or_pr_number`, `test_two_tickets_sharing_a_path_are_not_selected_in_one_poll`, `test_a_conflicting_ticket_takes_no_room`.

### Unit 5. Visibility (AC 5, 6)

- [ ] `lib/feeder.py`: `feeder_items` returns `waiting_reason`; `feeder_counts` returns `waiting_blocked`.
- [ ] `lib/status.py`: `FeederState` gains `waiting_blocked`, and `items` becomes a tuple of a small frozen `FeederItem(repo, issue, state, pr, reason)`. `render` prints the count and the reason.
- [ ] `lib/dashboard.py`: snapshot count and item `reason`, `ATTENTION_REASONS` extended. `lib/dashboard_page.py`: `FEEDER_STATE`, `renderFeeder` and `_feeder` show the reason.
- [ ] Tests: `test_orch_status_shows_waiting_reasons`, `test_dashboard_snapshot_shows_waiting_reasons`, `test_dashboard_page_renders_the_feeder_reason`, `test_status_and_dashboard_list_the_blocker_flags`. The existing `test_feeder_counts_eligible_fed_and_waiting` and `test_status_and_dashboard_show_an_empty_latest_poll` assert exact dicts, so they gain `waiting_blocked: 0`. That is the only edit to an existing test.

### Unit 6. Docs

- [ ] New `docs/decisions/156-feeder-gates.md` in the README's template, one entry per call in Appendix A.
- [ ] `docs/SPEC.md` section 7, the `intake` paragraph: three sentences on the gate lines, `waiting-blocked` and the three attention reasons. The frozen decision log is not touched.

## Verify

- [ ] Unit. `env -u ORCH_DB pytest` passes at the PR head, in the foreground.
- [ ] Revert. For each row of the map, revert its unit's production change on a scratch branch and record that its tests fail. Results go in the `ready` comment.
- [ ] Real bodies. A throwaway script runs `parse_gate_lines` on the live bodies of #138 to #152 (read with `env -u GH_TOKEN gh`) and prints each ticket's blockers, touches and conflicts. The output goes in the `ready` comment, so Max can compare it with the intended chain in the ticket.
- [ ] Budget. A fake-runner run over a 15-ticket chain logs the `gh` argv per poll. It shows one `graphql` read per repo with `--cache 60s` and no per-ticket reads.
- [ ] Config. `python bin/orch check-config` passes on all four CI invocations in `.github/workflows/ci.yml`.
- [ ] CI. `.github/workflows/ci.yml` is green at the head (pytest, `check-config`, `statechart`). The feeder is not in the TLA+ model, so the model needs no edit.
- [ ] Review gate. Codex Bot's round-1 review of the `ready` head.

## Close

- [ ] Every box above is checked with its evidence.
- [ ] PR title `#156: Feeder blocked-by and file-conflict gates`.
- [ ] Post the `ready` handoff naming the pushed head, with the per-criterion test list, the revert results and the real-bodies output.

## Appendix A. Calls made without asking Max

No product question blocks this. Each call below is a default, with what Max could ask for instead.

- **One `waiting-blocked` state for blockers and file conflicts.** The reason text tells them apart. Max could ask for a separate `waiting-conflict` count.
- **Three attention reasons.** `blocker-cycle`, `blocker-closed` (closed without finishing) and `blocker-missing` (no such issue or PR, or not readable as one). The ticket names the first two. A typo like `Blocked by #9999` would otherwise wait forever with no alert.
- **A failed status read fails closed and raises no flag.** The ticket waits one poll. A flag on every GitHub blip would train Max to ignore the list.
- **Only the `Blocked by` line itself.** Prose after it is never read, as the ticket asks. So #138 and #145 now wait on their PRs only because their bodies were since edited to name them on the line.
- **A Linear identifier in body text is not parsed.** Linear's own blocks relation covers Linear-to-Linear. `#N` on a Linear description means the project's GitHub repo. Max could ask for `Blocked by ENG-12` text, which costs one extra Linear read per poll when used.
- **A closed-unmerged holder releases its paths, and reopening it does not re-take them.** `released` is sticky, so a long-fed ticket is not read forever.
- **Folder paths.** `Touches: lib/` collides with `lib/feeder.py`. Plain paths must be equal.
- **An issue closed with no reason counts as completed.** That is GitHub's old default for a plain close.
- **Only tickets not yet started are gated.** A feed that is already `feeding` or `error` resumes even if its body later gains a blocker line. It is already in flight and holds its PR.

## Appendix B. Designs compared (architect scaffold)

- **A, chosen.** A pure `lib/feeder_gates.py` that takes parsed lines and resolved statuses and returns per-ticket waits, plus a `Holds` value that grows during the existing feed loop. The feeder makes one batched read per poll. Every rule is unit-testable with no database, and the loop keeps one ordering, so "earlier ticket wins" is true by construction.
- **B, rejected.** Gate inside `_candidates`, dropping blocked tickets before the loop. It is simpler, but a dropped ticket has no row, so no reason reaches `orch status`, and `_record_seen` would forget it every poll. It also cannot see tickets selected earlier in the same poll for the conflict gate.
- **C, rejected.** Persist a blocker graph table and resolve statuses from webhook events. It would save reads, but this repo has no GitHub webhook, and a second source of truth for issue state goes stale. The batched cached read is one call per repo per poll.

The arena runners were not run for this plan. The ticket fixes the hook points and the design space is narrow. Codex should press on the holder release rule and on the GraphQL linked-PR check in review.

## Appendix C. Risks and lane notes

- **Files beyond the ticket's list.** `lib/github_actions.py` (the per-call `cache_ttl`), `lib/attention.py` and a 12-line DDL move in `lib/dispatcher.py`, `lib/dashboard_page.py` (the reason on the page), `docs/SPEC.md` and `docs/decisions/`.
- **Open PRs on the same files.** #116 changes two lines of `lib/feeder.py`. #111, #113, #114, #129, #131 and #135 touch `lib/dispatcher.py`, `lib/status.py`, `lib/dashboard.py`, `lib/queue.py` or `lib/github_actions.py`. If any lands first, bring `main` in by a merge commit before `ready`. The `dispatcher.py` hunk is kept to the DDL block to keep that merge small.
- **GraphQL cost.** GitHub prices a GraphQL request by the connection nodes it asks for, divided by 100, with a minimum of 1 (GitHub's rate-limit docs; not measured here). A 50-ref chunk with `closedByPullRequestsReferences(first: 10)` makes about 51 connection requests, so about 1 point, against 5,000 an hour. The Verify budget box records the real `rateLimit { cost }` once on the live repo.
- **Linear complexity.** `inverseRelations(first: 10)` raises the intake query's real complexity. The poller charges every non-search request a flat `LINEAR_ISSUE_COMPLEXITY`, so its budget does not see the increase. Ten relations per issue keeps it small. Max could ask for a measured complexity charge in a follow-up.
- **`gh --cache` and the poll interval.** At a 30-second poll, a blocker merge is seen at most 60 seconds plus one poll late. That meets "the next `feed_once()`" in the integration test, which uses the fake client.

## Appendix D. Reading list

- `lib/feeder.py`: `feed_once`, `_candidates`, `_room`, `_record_seen`, `_set_state`, `feeder_counts`, `feeder_items`, `_Candidate.order`.
- `lib/intake_live.py`: `GitHubIntakeClient`, `LINEAR_INTAKE_QUERY`, `LinearIntakeClient.linear_intake`.
- `lib/github_actions.py`: `GitHubReviewActions._call`, `_json`, `_is_read`.
- `lib/poll_source.py`: `_linear_request`, `_linear_request_available`, `_gh_call`.
- `lib/dispatcher.py`: `flag_attention`, `clear_attention`, the `needs_attention` DDL.
- `lib/status.py`: `FeederState`, `SQLiteStatusReader.feeder` and `attention`, `render`. `lib/dashboard.py`: `collect_snapshot`, `_attention`, `ATTENTION_REASONS`. `lib/dashboard_page.py`: `renderFeeder`, `_feeder`.
- `tests/test_feeder.py`: `FakeGitHub`, `make_feeder`, `issue`, and the five fixtures the ticket names.
