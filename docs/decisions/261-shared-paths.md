# #261: shared_paths for shared docs

Defaults the builder picked for issue #261, listed in the plan on PR #265 with no product question open.

- **Decision:** `factory.md` gains an optional `shared_paths` list. A `Touches:` path that matches it never causes a feeder collision wait, and in a matching file a conflict hunk kept as both sides (ours then theirs or theirs then ours, byte for byte) still counts as a trivial update. A `Conflicts with` edge still holds, and code paths are never affected.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/265

- **Decision:** No `.gitattributes` `merge=union` for shared docs. A union driver would make these hunks never conflict, so both sides would be joined with nobody looking at the result, and it would also apply to the real `update-from-main` merge. The update check sees every resolution instead.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/265

- **Decision:** A pair of `Touches:` paths is skipped when either one matches `shared_paths`, so a held `docs/SPEC.md` does not hold a ticket that touches `docs/`, and the other way round too.
- **When:** 2026-11-14 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/265
