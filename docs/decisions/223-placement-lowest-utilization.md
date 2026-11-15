# #223: place jobs by lowest utilization, ties off-box

This replaces the #85 placement rule (the most free slots; a tie goes to the device listed first in `machines.yml`), which sent every job to the device with the largest `slot_max` until its free count fell to the others'.

- **Decision:** The device pool places a job on the eligible device with the lowest utilization (slots held over `slot_max`, compared exactly). A tie goes to a remote device before the local box, remotes in `machines.yml` order. Eligibility does not change, and a full device is never chosen.
- **When:** 2026-11-14 9:28 AM ET, by Max ("File and build: place jobs by lowest utilization, ties off-box")
- **Source:** https://github.com/maxharris1/factory/issues/223
