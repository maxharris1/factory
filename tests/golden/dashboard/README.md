# Dashboard leaf panel goldens

Server-rendered HTML of every leaf panel, one block per fixture variant, rendered from main a2bb7247d6e85731543c23ceabbd0f72ee98e0fa.
The ticket panels render running `maxharris1/cleffy#13`, stopped `aut0-code/aut0#AUT-7` (`_why`) and shipped `maxharris1/cleffy#88`
(`_summary`, `_ended`, added by `_with_shipped`). `ticket_cockpit_html` renders waiting-max `maxharris1/tracy-harris-coaching#5`
with one open question added.

Regenerate:

    GOLDEN_UPDATE=1 python3 -m pytest -q -p no:cacheprovider tests/test_dashboard_golden.py
