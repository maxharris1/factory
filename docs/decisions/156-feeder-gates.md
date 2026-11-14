# #156: feeder blocked-by and file-conflict gates

Defaults the builder picked for issue #156, listed in the plan on PR #157 (Appendix A) with no product question open. Each one says what Max could ask for instead.

- **Decision:** One feeder state, `waiting-blocked`, covers both a blocker that is not done and a path or conflicts-with edge held by a ticket in flight; the waiting reason tells them apart. Max could ask for a separate `waiting-conflict` count.
- **When:** 2026-11-13 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/157

- **Decision:** Three needs-attention reasons: `blocker-cycle`, `blocker-closed` (a PR closed without merge, an issue closed as not planned or duplicate, a Linear issue canceled) and `blocker-missing` (no such issue or PR, or not readable as one, so a typo such as `Blocked by #9999` does not wait forever with no alert).
- **When:** 2026-11-13 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/157

- **Decision:** A failed status read keeps every ticket that needed it waiting (`status unreadable`), counts one feeder error and raises no flag; flags already raised are kept, so a GitHub blip does not make them flap.
- **When:** 2026-11-13 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/157

- **Decision:** Only the `Blocked by`, `Touches:` and `Conflicts with` lines themselves are read. Prose on later lines is not, a line that starts with a backtick is not a gate line, and fenced code is skipped.
- **When:** 2026-11-13 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/157

- **Decision:** A Linear identifier in body text is not parsed. Linear's own blocks relation (read on the intake request) covers Linear-to-Linear; `#N` on a Linear description means the project's GitHub repo. Max could ask for `Blocked by ENG-12` text, at one extra Linear read per poll when used.
- **When:** 2026-11-13 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/157

- **Decision:** A fed ticket stops holding its paths once its PR is merged or closed, and stays released if the PR is reopened, so a long-fed ticket is not read forever.
- **When:** 2026-11-13 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/157

- **Decision:** A `Touches:` path ending in `/` is a folder and collides with every path inside it; other paths collide only when equal. Paths collide only within one repo; a `Conflicts with` edge holds across repos.
- **When:** 2026-11-13 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/157

- **Decision:** An issue closed with no state reason counts as completed (GitHub's old default for a plain close), and an issue with a merged linked PR is done whatever its state. Every page of an issue's linked PRs is read until a merged one is found; past the page cap the read fails and the dependent waits.
- **When:** 2026-11-13 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/157

- **Decision:** Only tickets not started yet are gated. A feed already `feeding` or `error` resumes even if its body later gains a gate line: it is in flight and holds its PR.
- **When:** 2026-11-13 ET, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/pull/157
