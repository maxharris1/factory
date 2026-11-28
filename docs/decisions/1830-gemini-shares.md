# #1830: Gemini shares of builds and reviews

- **Decision:** Send about a quarter of builds and a quarter of reviews to Gemini, and lean its builds toward harder tickets, so its work can be compared with Claude's and Codex's. `config/routing.yml` turns both shares on.
- **Max's words:** "Can we start randomly sprinkling in Gemini for builds and reviews periodically? I'm curious to see how Gemini does compared to both Claude and Codex on those respective tasks. So I want to collect some data, and then eventually down the line we can compare. Maybe throw 20 to 30% of build load at Gemini and 20 to 30% of the review load. Let's have Gemini build some of the harder tickets."
- **When:** 2026-11-27, 7:23 PM ET, by Max.
- **Source:** https://github.com/maxharris1/factory/issues/1830 (plan https://github.com/maxharris1/factory/pull/1831#issuecomment-7231433719). #1851 to #1854 built the draw, #1855 turns the shares on, and the harness report follows from #1856 on.

## Defaults

```yaml
# config/routing.yml
shares:
  gemini:
    build: 0.25
    review: 0.25
    min_headroom: 0.15
    max_builds_in_flight: 2
```

- `build` is the long-run fraction of builds that draw Gemini.
- `review` is the long-run fraction of reviews that draw Gemini.
- `min_headroom` skips the draw while the freshest five-hour or seven-day reading of the Gemini account has less headroom than this.
- `max_builds_in_flight` skips the build draw while this many other tickets already have a queued or running Gemini `plan`, `build` or `fix` job.

`lib/role_router.py` validates the block. `build`, `review` and `min_headroom` are numbers from 0 to 1, and `max_builds_in_flight` is a whole number of at least 0. Any other value, or an unknown key, is a `check-config` error.

## The list is the default

`roles.review` lists Codex first, then Claude, Gemini and Cursor. A ticket that does not win the review draw walks that list in order, so its reviewer is the first eligible candidate outside the builder's family. A won draw is the only thing that puts Gemini first. Before this, Gemini headed `roles.review` (#1713), so every Claude- or Codex-built ticket reviewed on Gemini whenever it was eligible.

The `build` lists are unchanged. A ticket that does not win the build draw builds on the first eligible candidate, today Claude for every type but `perf`.

## How the draw works

The dispatcher draws when it first routes a ticket in a repo whose `factory.md` has a `routing:` block, and only for a side whose pool comes from `config/routing.yml`. A `go` that names `builder=` or `reviewer=` routes both sides as it says, so neither side draws, even the side it leaves out. A side a `factory.md` slot overrides takes no draw either.

1. Each role draws once per ticket. The value is the first 8 bytes of `sha256("<repo>#<ticket>:<role>")` read as a fraction of 2^64, with the repo in lower case. It never uses the clock or a shared random generator, so the same ticket always gets the same value.
2. The draw wins when the value is below the role's chance and no guard holds. A won draw moves Gemini to the front of that role's pool. The rest of the pool keeps its order, and the refused, unavailable and same-family filters still apply.
3. The result is stored on the job's `detail.routing` as `sample`, one entry per role: `role`, `harness`, `seed`, `draw`, `share`, `chance`, `picked`, `skipped_reason`, and for the build its `hardness`. Plan, fix, re-build and re-review jobs reuse the stored route and never draw again.

### The tilt toward harder tickets

The build chance is `min(1, build * weight / 1.25)`. A ticket's weight is `0.5 + 0.25 * score`, from what is known when it is routed:

| Fact | Score |
| --- | --- |
| `Risk: high` | 2 |
| `Plan first: yes` | 2 |
| 4 or more distinct paths on `Touches:` | 1 |
| A type other than bug | 1 |

The weight runs from 0.5 to 2.0, and 1.25 is the reference mean, so the long-run build share stays near `build`. With `build: 0.25`, a low-risk bug fix that needs no plan wins 10% of the time, and a high-risk, plan-first feature that touches 4 or more paths wins 40% of the time.

A Gemini-built ticket never reviews on Gemini, because the reviewer must be outside the builder's family. The review chance is therefore raised for the other tickets to keep the long-run review share near `review`. It is `min(1, review / (1 - c))`, where `c` is the ticket's build chance when the build draw was not skipped by a guard, and 0 otherwise. On a Gemini-built ticket every review draw that no guard skipped, won or lost, is recorded as lost with `skipped_reason: same-family`.

### Guards

A guard skips the draw. The role keeps its list order, and the first guard that holds is recorded as `skipped_reason`. The guards, in order:

1. `refused`: `gemini` is in `orch.yml` `refused_harnesses`.
2. `signed-out`: no device that takes new Gemini jobs has a Gemini account free of a sign-in hold and a live sign-out lock.
3. `usage-held`: the signed-in Gemini accounts are held by a usage limit or the device pool's headroom floor, or are paused.
4. `low-headroom`: the freshest five-hour or seven-day reading is below `min_headroom`.
5. `builds-in-flight`: the build draw only, when `max_builds_in_flight` other tickets have a Gemini build in flight.

`orch.yml` still lists `gemini` in `refused_harnesses`, so until it comes out, every draw records `refused` and every ticket takes the list order.

## Turning a share off

- Set `shares.gemini.build: 0` to turn the build share off. No build draw is made or recorded, and builds take the `build` lists in order.
- Set `shares.gemini.review: 0` to turn the review share off. No review draw is made or recorded, and reviews take `roles.review` in order, Codex first.
- Remove the `shares:` block to turn both off. The block's absence means both shares are 0.

A share of 0 stops the draw, not Gemini. Gemini stays a fallback in the `build` and `review` lists, so it still runs a job when every candidate above it is refused, unavailable or same-family: a build when `claude` and `codex` are refused, or a review of a Claude-built ticket when `codex` is refused. To keep Gemini off every job, add `gemini` to `orch.yml` `refused_harnesses`.

A ticket already routed keeps its stored route either way.
