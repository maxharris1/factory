- **Decision:** `factory.md` sets `review.pre_ready` to `"$ORCH_PYTHON" bin/orch check-config --orch orch.yml --machines machines.yml --factory factory.md --allow-placeholders && "$ORCH_PYTHON" -m pytest -q -p no:cacheprovider tests/test_eval_report.py` with `timeout_seconds: 300`.
- **When:** 2026-11-16, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/510

The check runs the whole `tests/test_eval_report.py` file, not a `-k chart` selector. The file holds the chart-consistency tests: `test_chart_edges_match_section_4_1`, `test_chart_mmd_edges_match_transitions`, `test_t2_self_loops_are_charted` and `test_chart_edges_fail_on_an_unparsed_line`. A selector would quietly run fewer tests after a rename. The command runs `$ORCH_PYTHON`, the interpreter orch runs under, not `python3` from PATH (review round 1, f-1). The waiter and `orch job done` both run under the install venv, and a cron-started waiter's PATH finds the system `python3`, which has no PyYAML. `requirements.txt` now pins pytest and pytest-timeout, so `deploy/install.sh` provisions them in that venv. `-p no:cacheprovider` keeps `.pytest_cache` out of the worktree.

Measured on device pc with `env -u ORCH_DB sh -c "<command>"`, six runs on this branch: 4.06 s, 4.16 s and 4.26 s on main `803691e`, then 4.21 s, 4.86 s and 4.64 s after the rebase on main `7ddcd4d`. Each ended `ok` and `49 passed`. The pytest run is about 4 s of that. That is far under the 300-second limit.

After the round-1 fix, measured on device pc with a venv holding only `requirements.txt`, PATH `/usr/bin:/bin`, through `lib.pre_ready.run`: 7.22 s, `ok` and `49 passed`.
