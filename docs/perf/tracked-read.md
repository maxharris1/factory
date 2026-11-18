# Tracked-read time before and after #870

#870 (PR #900) made the poller skip unchanged PRs in the `tracked-read` phase. One GraphQL probe per repo fingerprints the tracked PRs. A PR whose fingerprint matches the one taken before its last full read reuses that read when the read is less than 10 minutes old. This page records the phase time from live `orch poll: cycle end` lines before and after that change, for #904.

Both numbers come from `scripts/issue-870-tracked-read-times.py`, run on the orch host against the live poller log, `~/.local/state/factory/poller.log`. The p90 is the nearest-rank value.

## Results

| | Before | After |
| --- | --- | --- |
| Commit measured | Checkout moved `08117c8d` → `4a9af0e1` → `cec59a84` → `f0c4d599`, none containing `1780e70f` (see below) | `1780e70fb0810616d7b1d8899f0e4cc8ccd99013` (#870), then `5f292534` from 12:25:05 PM ET |
| Window, Nov 18 2026, ET | 11:34:46 AM to 12:03:37 PM | 12:19:59 PM to 12:34:22 PM |
| Cycle-end lines | 20 (`--last 20`) | 10 (`--last 10`) |
| Tracked-read median | 34.5 s | 28.2 s |
| Tracked-read p90 | 48.4 s | 50.8 s |
| Whole cycle median | 68.5 s | 60.5 s |
| Whole cycle p90 | 91.6 s | 123.8 s |
| Source | [#900 answer](https://github.com/maxharris1/factory/pull/900#issuecomment-6985635951), quoted in #900's body | [#915 answer](https://github.com/maxharris1/factory/pull/915#issuecomment-6986427284) |

The after run:

```text
$ git rev-parse HEAD
74344b4eb5858c9e1ee8a9d69b363ac52489ab9e
$ git merge-base --is-ancestor 1780e70fb0810616d7b1d8899f0e4cc8ccd99013 HEAD && echo contains-870
contains-870
$ python scripts/issue-870-tracked-read-times.py --last 10 ~/.local/state/factory/poller.log
10 cycles, Nov 18 12:19:59 PM ET to Nov 18 12:34:22 PM ET
tracked-read: median 28.2s, p90 50.8s
whole cycle:  median 60.5s, p90 123.8s
```

The after window is the first 10 cycle-end lines after the poller restarted on `1780e70f` at 12:18:39 PM ET. #870 was deployed at 12:14:28 PM ET. The checkout moved to `5f292534`, which contains `1780e70f`, at 12:25:05 PM ET. It moved to `74344b4e` at 12:34:24 PM ET, after the last cycle in the window. Every measured cycle ran code that includes #870.

The poller heartbeat records the commit the process loaded (`version`, the checkout's first 12 hex digits, read once at import in `lib/health.py`). The review read it on the orch host from `component_heartbeats` in `~/.local/state/factory/orch.health.db`: poller `version=74344b4eb585`, last seen Nov 18 12:43:28 PM ET. That commit contains `1780e70f`, but it was loaded after the window ended, so it does not name the commit each measured cycle ran. No heartbeat was read during either window. The evidence for the measured windows is the checkout reflog and the poller's start time.

The before window is the 20 cycle-end lines from 11:34:46 AM to 12:03:37 PM ET. The checkout reflog on the orch host (`git -C /home/box/factory reflog --since='2026-11-18 11:00:00 -0500' --until='2026-11-18 12:04:00 -0500'`, run in the review) shows the checkout moving through these merges to `main` across that window. Merge times are from `main`'s history. The checkout followed each merge within about 20 s in the after window.

| Commit | Merged to `main`, ET | PR |
| --- | --- | --- |
| `08117c8d4962de71ee235ff613555b49ef04db67` | 11:31:13 AM | #884 |
| `4a9af0e1d68df119d8613057b5bba506c35d53bc` | 11:34:33 AM | #890 |
| `cec59a846e3eb856c6d96f0c463f8873c64f36d7` | 11:56:18 AM | #907 |
| `f0c4d59935fd837da76c161906c1cd0cee9b9b38` | 11:57:57 AM | #903 |

By the merge times, the checkout was on `08117c8d` or `4a9af0e1` when the window opened and on `f0c4d599` when it closed. The reflog times for each move were not copied into this page, and the poller's start time during the before window was not recorded. So which of these commits the running poller had loaded in each cycle is unknown: it loads code at start, and may have kept running an older commit across these moves. All four commits are ancestors of `1780e70f` on `main`, and none contains it, so no before cycle ran #870 whichever was loaded. The revision `668b82a2` in [#900's answer](https://github.com/maxharris1/factory/pull/900#issuecomment-6985635951) is the detached worktree the timing script ran from, not the poller's code.

## Is the after median under 15 s?

No. The after median is 28.2 s, 6.3 s (18%) below the before median of 34.5 s. The target was under 15 s. The p90 did not improve (48.4 s before, 50.8 s after).

## Next design step: candidate A, the per-PR REST probe

#870's plan named two follow-ups for a miss: candidate A's per-ticket REST probe, or a per-ticket offset on the 10-minute backstop. The next step is candidate A.

The backstop offset cannot bring the median under 15 s alone. Without the offset, all PRs read in the same cycle reach the 10-minute limit together. That costs one slow cycle every 10 minutes, about one cycle in this 10-cycle window. The first cycle after the restart is also slow, because no reads are stored yet. Two slow cycles out of 10 move the p90, not the median. The median of 10 values is the mean of the 5th and 6th, so at least 5 cycles had a tracked-read of 28.2 s or more.

A more likely cause is how often the fingerprint changes. This is a guess: the per-cycle values and reuse counts were not captured. The fingerprint holds `baseRefOid`, `mergeable` and `mergeStateStatus`. A merge to `main` changes `baseRefOid` for every open PR on `main`, and GitHub then recomputes mergeability, so the next one or two probes do not match. Each PR then gets the full read of about 8 `gh` calls. `main` took a merge at 12:24:48 PM ET, inside the window, and another at 12:34:08 PM ET, just before the last cycle in the window ended. With the restart cycle and the backstop wave, that can give 4 or 5 slow cycles in 10, enough to keep the median near the before value.

Candidate A keeps the REST pull read every cycle. The GraphQL probe then covers only comments, labels and CI. After a base move, an otherwise unchanged PR costs one pull read, not the full set. #870's plan estimated about 10 s for the phase with candidate A (a guess, not measured). The backstop offset can follow if the p90 still shows the 10-minute wave.

Before building A, check the cause. Print the 10 per-cycle tracked-read values and the number of PRs read in full in each cycle. Two groups of values, with the slow cycles at the restart, after each merge to `main` and at the backstop, would confirm the cause. Then measure a longer window away from a restart, for example `--last 30` some hours after a deploy. This window starts with a cold cycle, so it overstates the steady-state time.
