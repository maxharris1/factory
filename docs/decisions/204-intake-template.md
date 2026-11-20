# #204: feeder intake template

These are the defaults the builder picked for issue #204. They are listed in the plan on PR #285, and no product question is open on them. Each one says what Max could ask for instead.

- **Decision:** All four template fields are required: an `Acceptance criteria` section with at least one builder criterion, a `Risk: low|high` line, a `Plan first: yes|no` line and a `Depends on:` line (`Depends on: none` is fine). A ticket that is missing any of them is `refused-intake`, posts nothing and takes no room. Max could ask for a missing `Plan first` or `Depends on:` to only warn in the brief.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/285

- **Decision:** Risk is never defaulted to low. It comes from the `Risk:` line. A `risk:high` label can raise it and never lowers it. A missing line, or a line saying anything other than low or high, refuses the ticket. The brief says where the risk came from.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/285

- **Decision:** A criterion is refused as being about other PRs (SF-5, the N-02 case) when it names PRs in general ("every other open PR", "merges cleanly with") or names one specific PR (`PR #N`, `PR N`, `pull request #N` or a `/pull/N` URL). A criterion that wraps over several lines is read as one item. A plain `#N` is not refused. The refusal tells the author to move PR order into `Blocked by` / `Conflicts with`, which the #156 gates enforce.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/285

- **Decision:** The other-PRs check reads each criterion as CommonMark (plus strikethrough) renders it: the text and code-span content, with markup and inline HTML dropped and entities and escapes decoded. Each link or image target is checked on its own, so a `/pull/N` link or autolink still counts. So `PR **#157**`, `[PR](https://example.com) #157` and `` `PR` #157 `` are refused like `PR #157`, while code such as `` `pr*2` `` stays literal and is not read as `pr2`. The stored criterion, the refusal reason and the brief keep the text as written.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/319

- **Decision:** A criterion that needs an operator action never refuses a ticket. Such a criterion is either tagged `ops:` / `(ops)` / `[ops]`, or reads as an operator step: it starts with "Manually", "By hand", "Restart the" or "Install the", says "on the box", or has Max or the operator as the subject ("Max restarts", "the operator installs"). Functionality a user performs by hand ("Users can manually trigger the retry") stays a builder criterion; tag a doubtful case `ops:`. The brief lists it under its own "Ops step" section, apart from the builder's criteria. A ticket whose only criteria are ops criteria has no builder criteria, so it is refused.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/285

- **Decision:** There is one Grok `refused` wake per ticket per distinct set of defects, deduped in the job queue, so it holds across restarts. Editing a ticket without fixing it does not wake Grok again; a different set of defects wakes it once more. The ticket also raises needs-attention (`intake-refused`), which clears once the ticket is fixed or loses its signal.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/285

- **Decision:** Only tickets that have not started are checked. A feed already `feeding` or `error` resumes even if its body is later edited out of shape, and its recorded risk is kept.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/285

- **Decision:** Rollout: on the first poll after merge, each labelled ticket that has not been fed yet and lacks the template is refused, with one wake each. Tickets already fed are unaffected.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/285

- **Decision:** `orch status` and the dashboard show the raw state `refused-intake` until they get a label for it. That label is left for a follow-up, because `lib/status.py` and `lib/dashboard_page.py` are outside this ticket's Touches list, and #205 works in the same lane.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/285

- **Decision:** A ticket can name QA journeys on an optional `Features:` line, such as `Features: create-note, search, new:bulk-export`. The journeys come from the project's feature map, the `features/` folder next to its `qa.verify` skill's `SKILL.md` (`<skill-folder>/features/<id>.md`). The skill folder is found on the base branch the way the QA lane finds it, `*/<qa.verify>/SKILL.md` at any depth, such as `.claude/skills/`, `.cursor/skills/` or `.agents/skills/`; when several match, the first `SKILL.md` path in `git ls-files` order wins. Ids match `[a-z0-9][a-z0-9-]{0,63}`, split on commas or spaces. `Features: none`, or no line, names no journey. A plain id must be in the map on the base branch. `new:<id>` is how a ticket adds a journey: the id must not be in the map yet, and the builder writes `features/<id>.md` (the four H2s) and its README index row in the PR.
- **When:** 2026-11-19 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1093

- **Decision:** An acceptance criterion tagged `qa:`, `(qa)` or `[qa]` is a QA criterion. `qa(<id>, ...):` binds it to some of the journeys on the `Features:` line, and a bare tag binds it to all of them. Unlike `ops:`, the criterion stays a builder criterion and stays in the brief as written, even when its text reads as an operator action ("the operator runs"): a `qa` tag is read before that guess. Tagging is optional: an untagged criterion keeps the owner the reviewer assigns (#1023). A `Features:` line with no `qa:` criteria is accepted and gets no QA scenarios.
- **When:** 2026-11-19 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1093

- **Decision:** Five defects refuse a ticket through the same `refused-intake` flow as the others: `features-unknown` (an id outside the grammar, or a plain id the map lacks), `features-exists` (a `new:` id the map already has), `features-no-verify` (a `Features:` line on a project whose factory.md sets no `qa.verify`, so factory's own tickets are refused until F-15, #1034), `features-unbound` (a `qa(<id>)` naming an id the `Features:` line does not list) and `qa-no-features` (`qa:` criteria with no `Features:` line). Max could ask for `features-no-verify` to only warn.
- **When:** 2026-11-19 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1093

- **Decision:** The map is read only for an unstarted ticket with a `Features:` line, once per repo, base and verify skill per poll: the base branch's sha, one recursive tree read at that sha to find the verify skill, then one GitHub contents read of its features folder. When GitHub truncates a tree read, the feeder lists that folder and reads each subfolder's tree, breadth first, up to 64 tree reads, and takes the first match. A walk that hits the cap is a failed read under the retry rule below, never an empty map. A skill or features folder missing at the base is an empty map. A failed read never refuses a ticket: it stays `eligible`, is not fed that poll, and is checked again on the next one.
- **When:** 2026-11-19 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1093

- **Decision:** This replaces two clauses of the map-read entry above. Under a truncated tree the feeder walks folders in path order, not breadth first, and keeps reading until no unread folder can hold an earlier `SKILL.md` path, so it picks the same skill as the complete-tree lookup in any listing order. A verify skill that cannot be found at the base, in a complete or a truncated tree, is a failed read under the retry rule, never an empty map: the ticket stays `eligible` and an earlier intake refusal flag is cleared. A features folder missing next to a found skill is still an empty map. A mistyped `qa.verify` therefore holds `Features:` tickets in `eligible` with only a feeder log line; Max could ask for it to refuse with its own defect code instead.
- **When:** 2026-11-19 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1135
- **Decision:** A ticket can add two optional sections, `## Assumed environment` and `## Out of scope` (F-14, #1033). They are read like `Acceptance criteria`: a heading at levels 2 to 4, with the same bold and colon forms, and the section ends at the next heading of the same or a higher level. A heading that names another of the three sections always starts that section, even when it is nested deeper, so `### Out of scope` under `## Acceptance criteria` ends the criteria. Each list item is one entry, and in these two sections a plain paragraph is one entry too. The brief lists them under their own headings right after the acceptance criteria. Neither section is required, and neither can refuse a ticket.
- **When:** 2026-11-19 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1137

- **Decision:** A builder criterion (qa criteria included, ops criteria not) whose text uses an unbounded quantifier (`every`, `everything`, `everywhere`, `all`, `any`, `anything`, `each`, `always`, `never`, `whatever` or `whenever`, as whole words) on a ticket with no `Out of scope` entries gets one warning in the brief, under `### Intake warnings`. The warning names the criterion and asks for an `Out of scope` section or a narrower criterion. It is not a defect: the ticket is fed as usual, with no wake and no needs-attention. Max could ask for the warning to refuse the ticket instead, or for a different word list.
- **When:** 2026-11-19 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1137

- **Decision:** The parser and the brief live in `lib/feeder.py`, so that file and `tests/test_feeder_template.py` carry the change. The ticket's Touches line named `lib/intake_live.py` and `tests/test_intake_live.py`, which hold the GitHub and Linear readers and do not change.
- **When:** 2026-11-19 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/1137

- **Decision:** The builder-written `## Verify, live` section is retired at 2026-12-15T00:00Z (`LIVE_SECTION_CUTOFF` in `lib/brief/common.py`), compared with the plan or ready comment's own GitHub `created_at`. From then, `orch job done` refuses a plan or ready that has the heading outside a code fence, even with an empty section, with the reason "has a `## Verify, live` section, refused since 2026-12-15: QA journeys now come from the ticket's `Features:` line and `qa:` criteria (docs/decisions/204-intake-template.md)". The QA reader ignores the section in such a comment, so a later plan falls back to an earlier ready. Briefs stop asking for the section by the job's queue time (`created_at`), not the wall clock, so a job's brief and the brief snapshots do not change as the date passes. A comment with a missing or unreadable `created_at` is not refused. Deleting the legacy reader and `live_lines` is a later cleanup ticket.
- **When:** 2026-11-19 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/1106
