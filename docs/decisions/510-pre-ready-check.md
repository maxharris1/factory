- **Decision:** `factory.md` sets `review.pre_ready` to `python3 bin/orch check-config --orch orch.yml --machines machines.yml --factory factory.md --allow-placeholders && python3 -m pytest -q -p no:cacheprovider tests/test_eval_report.py` with `timeout_seconds: 300`.
- **When:** 2026-11-16, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/510

The check runs the whole `tests/test_eval_report.py` file, not a `-k chart` selector. The file holds the chart-consistency tests: `test_chart_edges_match_section_4_1`, `test_chart_mmd_edges_match_transitions`, `test_t2_self_loops_are_charted` and `test_chart_edges_fail_on_an_unparsed_line`. A selector would quietly run fewer tests after a rename. The command says `python3`, not `python`, because device pc has no `python` binary. `-p no:cacheprovider` keeps `.pytest_cache` out of the worktree.

Measured on device pc with `env -u ORCH_DB sh -c "<command>"`, six runs on this branch: 4.06 s, 4.16 s and 4.26 s on main `803691e`, then 4.21 s, 4.86 s and 4.64 s after the rebase on main `7ddcd4d`. Each ended `ok` and `49 passed`. The pytest run is about 4 s of that. That is far under the 300-second limit.
