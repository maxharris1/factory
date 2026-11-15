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
