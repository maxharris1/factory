- **Decision:** `factory.md` adds `&& "$ORCH_PYTHON" -m pytest -q -p no:cacheprovider -m fast` to the `review.pre_ready` command from [510-pre-ready-check.md](510-pre-ready-check.md), keeps `timeout_seconds: 300`, and `tests/conftest.py` marks every test in `FAST_TEST_FILES` as `fast`.
- **When:** 2026-11-19, by Factory decided (technical)
- **Source:** https://github.com/maxharris1/factory/issues/1026

The 300-second limit holds on device pc (i7-13700K, 24 threads, WSL2). Each run used the install venv `/home/factory/.local/share/factory/venv/bin/python` as `$ORCH_PYTHON`, while other factory jobs and CI runners shared the machine (load average 6 to 12).

Before the rebase, at `a05ce1e`, `env -u ORCH_DB sh -c "<command>"` took 35.13 s: `ok`, `49 passed`, then `536 passed, 12731 deselected`. The same head took 20.43 s through `lib.pre_ready.run` with `check_environ`, and the result was `passed=True` and `exit 0`.

After the rebase on main `75a5324`, three shell runs took 26.37 s, 33.40 s and 29.19 s. Each ended `ok`, `49 passed` and `574 passed, 12893 deselected`.
