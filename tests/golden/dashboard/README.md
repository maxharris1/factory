# Dashboard leaf-panel goldens

These files are the leaf-panel goldens for the dashboard layout pass (#1675). Each `<leaf>.html` holds one leaf panel's markup for every fixture variant in `tests/test_dashboard_golden.py`. They were generated on `main` at `a2bb7247` (`a2bb7247d6e85731543c23ceabbd0f72ee98e0fa`).

`GOLDEN_UPDATE=1 python3 -m pytest tests/test_dashboard_golden.py` is the only writer. A layout PR never edits these files. If one changes, a panel's own markup changed.

To check that they still match that commit, regenerate them there. The last command prints nothing.

```sh
git worktree add /tmp/golden a2bb7247d6e85731543c23ceabbd0f72ee98e0fa
cp tests/test_dashboard_golden.py /tmp/golden/tests/
cp -r tests/golden/dashboard /tmp/golden/tests/golden/
(cd /tmp/golden && GOLDEN_UPDATE=1 python3 -m pytest tests/test_dashboard_golden.py)
diff -r /tmp/golden/tests/golden/dashboard tests/golden/dashboard
git worktree remove --force /tmp/golden
```
