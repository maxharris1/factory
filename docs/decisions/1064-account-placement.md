# #1064: account placement

Decisions from issue #1064, built in tickets #1315, #1316 and #1317. `DevicePool.claim` holds each harness job's slot on an (account, device) pair. `account_loads` and `choose_pair` in `lib/device_pool.py` hold the rule, and docs/SPEC.md section 13 ("Placement" under "Usage accounts") describes it. `choose_pair` compares the open accounts by these keys, in order, and the winner takes its best-ranked free device:

1. Whether the account's best free device is one the job should avoid (issue #218). An avoided device is used only when no other pair is free, as before accounts.
2. The higher score. Usage headroom is the reason to choose one account over another.
3. Fewer running jobs. On a tie, the account with less work in flight takes the job, the rule before #1316.
4. The rank of the account's best free device. That is utilization (slots held over `slot_max`, compared exactly), then a remote device before the local box, then `machines.yml` order (issue #223). Device load decides only between accounts the first three keys cannot separate.
5. The account id, in sort order, so the same state always gives the same pair.

- **Decision:** `FLOOR` is 0.05. An account whose fresh headroom is under 5% takes no new job. Its running jobs keep running. A new job there would likely push the account into a usage-limit pause partway through and waste the start, while another account or a later reading can take it. The floor holds only on a fresh reading. Once the low window resets or its reading is older than `STALE_SECONDS`, that window stops counting, and with no fresh window left the account scores `UNREAD_HEADROOM` and takes work again. While a floored account is the reason a queued job cannot place, the job's queue wait restarts, so the hold does not use up its timeout.
- **When:** 2026-11-22, by Factory decided (technical), for issue #1064 (ticket #1316)
- **Source:** https://github.com/maxharris1/factory/issues/1064

- **Decision:** `PENDING_CHARGE` is 0.02. Each running job placed on an account after its newest reading lowers the account's score by 0.02. Readings arrive only when a job starts and while it runs, so without the charge every claim between two readings would pick the same top account. With the charge, two accounts 0.04 apart in headroom split a burst after the higher one takes two jobs. A gap of 0.2 or more still decides on headroom for ten jobs. A job placed before the newest reading is not charged, because that reading already counts its usage.
- **When:** 2026-11-22, by Factory decided (technical), for issue #1064 (ticket #1316)
- **Source:** https://github.com/maxharris1/factory/issues/1064

- **Decision:** `UNREAD_HEADROOM` is 0.5. An account with no fresh reading, such as a new account or one idle past its windows, scores as if it had half its headroom left. It loses to an account known to have more and wins over one known to have less. Its first job takes a reading, and until that reading lands each job placed on it is charged as pending. A score of 1.0 would send every burst to an unread account first, and 0 would starve it until some other path read it.
- **When:** 2026-11-22, by Factory decided (technical), for issue #1064 (ticket #1316)
- **Source:** https://github.com/maxharris1/factory/issues/1064

- **Decision:** The score is rounded to 6 places, the precision of `usage_readings.headroom`. A pending charge that brings one account exactly to another's headroom then ties, and fewer running jobs decides, instead of a float error deciding.
- **When:** 2026-11-22, by Factory decided (technical), for issue #1064 (ticket #1316)
- **Source:** https://github.com/maxharris1/factory/issues/1064

- **Decision:** The tie-break order is the one listed above. Score leads because headroom is what #1064 places by. Running jobs and device rank keep the pre-#1316 rules as tie-breaks, and the account id makes the choice deterministic.
- **When:** 2026-11-22, by Factory decided (technical), for issue #1064 (tickets #1315 and #1316)
- **Source:** https://github.com/maxharris1/factory/issues/1064

- **Decision:** The sign-in check applies only once the `account_signins` table exists. `orch accounts doctor` (`lib/account_doctor.py`) creates it. From then on, an account other than the device's own login places on a device only with an `ok` row for that account and device, and `placement_blocked` reads the same rows. The device's own login needs no row, because it is the bot's CLI login that every device ran with before accounts existed. A factory that has never run the doctor places exactly as before, so deploying the check does not stop every side account at once. After the doctor runs, a side account whose row on a device is not `ok` stops taking jobs there, so a signed-out folder does not receive jobs.
- **When:** 2026-11-22, by Factory decided (technical), for issue #1064 (ticket #1317)
- **Source:** https://github.com/maxharris1/factory/issues/1064
